"""Babysit reviews on agent pull requests."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NewType

from kinby.plugins import ToolContext, tool

from kinby_code_factory.checks import (
    ChecksFailed,
    ChecksFixFailed,
    ChecksPassed,
    run_checks_with_fix,
)
from kinby_code_factory.clients import (
    CodingClientError,
    CodingRun,
    ReviewReply,
    TokenUsage,
    fix_review_threads_with_codex,
)
from kinby_code_factory.config import Babysitting, Checks, factory_config
from kinby_code_factory.process import CommandError
from kinby_code_factory.pull_request import (
    Git,
    WorkspaceFileError,
    checkout_branch,
    current_commit,
    discard_branch,
    discard_branch_for_report,
    push_checked_out_branch,
    verify_committed_workspace,
)
from kinby_code_factory.report import report_json
from kinby_code_factory.repository import (
    AGENT_BRANCH_PREFIX,
    READY_FOR_HUMAN_LABEL,
    BabysitPullRequest,
    BranchName,
    CheckRun,
    CheckRunStatus,
    CommitSha,
    GitHubLogin,
    GitHubRepository,
    IssueNumber,
    LabelName,
    PullRequestNumber,
    PullRequestUrl,
    RepositoryResponseError,
    ReviewThread,
    github_environment,
)

MERGE_READY_LABEL = LabelName("merge-ready")
BabysitWarning = NewType("BabysitWarning", str)


class BabysitOutcome(StrEnum):
    """The outcome of one babysitting action."""

    FIXED = "fixed"
    MERGE_READY = "merge_ready"
    ROUND_LIMIT = "round_limit"
    FAILED = "failed"


@dataclass(frozen=True)
class BabysitReport:
    """One babysitting result."""

    pull_request_number: PullRequestNumber
    pull_request_url: PullRequestUrl
    issue_number: IssueNumber
    outcome: BabysitOutcome
    round_number: int
    threads_fixed: int
    threads_answered: int
    codex: CodingRun | None
    checks: ChecksPassed | ChecksFailed | None
    warnings: tuple[BabysitWarning, ...]
    failure_reason: str | None


@dataclass(frozen=True)
class _BabysitAction:
    """A completed outcome with the GitHub changes it still needs."""

    pull_request: BabysitPullRequest
    issue: IssueNumber
    outcome: Literal[BabysitOutcome.MERGE_READY, BabysitOutcome.ROUND_LIMIT]
    label: LabelName
    add_label: bool
    request_review: bool


def actionable_threads(
    threads: tuple[ReviewThread, ...],
    coder: GitHubLogin,
) -> tuple[ReviewThread, ...]:
    """Return unresolved threads whose last comment is not the coder's."""
    return tuple(
        thread
        for thread in threads
        if not thread.resolved and thread.comments and thread.comments[-1].author != coder
    )


def is_waiting(checks: tuple[CheckRun, ...]) -> bool:
    """Return whether any check run is queued or in progress."""
    return any(
        check.status in {CheckRunStatus.QUEUED, CheckRunStatus.IN_PROGRESS} for check in checks
    )


def is_merge_ready(pull_request: BabysitPullRequest, coder: GitHubLogin) -> bool:
    """Return whether a reviewed pull request has nothing left to answer."""
    head = pull_request.listed.head
    reviewed_head = any(review.commit == head for review in pull_request.reviews) or any(
        comment.commit == head for thread in pull_request.threads for comment in thread.comments
    )
    return (
        reviewed_head
        and not actionable_threads(pull_request.threads, coder)
        and not is_waiting(pull_request.checks)
    )


@tool(write=True)
def babysit_pull_request(signal: dict[str, object], context: ToolContext) -> str | None:
    """Scan agent pull requests and label a completed babysitting outcome.

    With babysitting off in package.yaml, every wake is a no-work turn.
    """
    config = factory_config(context)
    babysitting = config.babysitting
    if not babysitting.enabled:
        return None
    round_limit = babysitting.round_limit
    environment = github_environment(config.secrets.github_token)
    repository = GitHubRepository(
        context.workspace,
        timeout_seconds=config.commands.github_timeout_seconds,
        environment=environment,
    )
    git = Git(context.workspace, config.commands.git_timeout_seconds, environment)
    signaled_pull_request = signal_pull_request_number(signal)
    signaled_branch = (
        repository.pull_request_branch(signaled_pull_request)
        if signaled_pull_request is not None
        else None
    )
    if not signal_warrants_scan(signal, signaled_branch):
        return None
    coder = repository.current_login()
    metadata = repository.metadata()
    pull_requests = repository.babysit_pull_requests(coder, metadata)
    actions = _select_babysit_actions(pull_requests, coder, metadata.maintainer, round_limit)
    actions_by_pull_request = {action.pull_request.listed.number: action for action in actions}
    outcomes = {
        pull_request.listed.number: _label_outcome(pull_request, coder, round_limit)
        for pull_request in pull_requests
    }
    for pull_request in pull_requests:
        if _has_stale_readiness_label(
            pull_request,
            outcomes[pull_request.listed.number],
        ):
            _closed_issue(pull_request)
    reports: list[BabysitReport] = []
    for pull_request in pull_requests:
        listed = pull_request.listed
        outcome = outcomes[listed.number]
        action = actions_by_pull_request.get(listed.number)
        if action is not None and action.request_review:
            repository.request_review(listed.number, metadata.maintainer)
        if outcome is not BabysitOutcome.MERGE_READY and MERGE_READY_LABEL in listed.labels:
            repository.remove_pull_request_label(listed.number, MERGE_READY_LABEL)
        if outcome is BabysitOutcome.MERGE_READY and READY_FOR_HUMAN_LABEL in listed.labels:
            repository.remove_pull_request_label(listed.number, READY_FOR_HUMAN_LABEL)
        if action is None:
            continue
        if action.add_label:
            repository.label_pull_request(listed.number, action.label)
        reports.append(_label_report(action))
    selected = _fix_candidate(pull_requests, coder, round_limit)
    if selected is not None:
        reports.append(
            _run_fix_round(
                repository,
                git,
                selected,
                coder,
                metadata.maintainer,
                metadata.default_branch,
                babysitting,
                config.checks,
            )
        )
    return _reports_json(reports)


def signal_warrants_scan(
    signal: dict[str, object],
    abbreviated_pull_request_branch: BranchName | None = None,
) -> bool:
    """Return whether a schedule or self-contained signal warrants a scan."""
    if not signal:
        return True
    body = signal.get("body")
    if not isinstance(body, dict):
        return False
    pull_request = body.get("pull_request")
    if isinstance(pull_request, dict):
        return _pull_request_is_agent(pull_request)
    issue = body.get("issue")
    if not isinstance(issue, dict) or not isinstance(
        nested_pull_request := issue.get("pull_request"), dict
    ):
        return False
    if "head" in nested_pull_request:
        return _pull_request_is_agent(nested_pull_request)
    return (
        abbreviated_pull_request_branch is not None
        and abbreviated_pull_request_branch.startswith(AGENT_BRANCH_PREFIX)
    )


def signal_pull_request_number(signal: dict[str, object]) -> PullRequestNumber | None:
    """Return the PR number from an abbreviated pull request comment signal."""
    body = signal.get("body")
    issue = body.get("issue") if isinstance(body, dict) else None
    if (
        not isinstance(issue, dict)
        or not isinstance(issue.get("pull_request"), dict)
        or not isinstance(number := issue.get("number"), int)
    ):
        return None
    return PullRequestNumber(number)


def _pull_request_is_agent(value: dict[object, object]) -> bool:
    head = value.get("head")
    return (
        isinstance(head, dict)
        and isinstance(branch := head.get("ref"), str)
        and branch.startswith(AGENT_BRANCH_PREFIX)
    )


def _label_outcome(
    pull_request: BabysitPullRequest,
    coder: GitHubLogin,
    round_limit: int,
) -> Literal[BabysitOutcome.MERGE_READY, BabysitOutcome.ROUND_LIMIT] | None:
    if is_merge_ready(pull_request, coder):
        return BabysitOutcome.MERGE_READY
    if pull_request.round_count >= round_limit and actionable_threads(pull_request.threads, coder):
        return BabysitOutcome.ROUND_LIMIT
    return None


def _fix_candidate(
    pull_requests: tuple[BabysitPullRequest, ...],
    coder: GitHubLogin,
    round_limit: int,
) -> BabysitPullRequest | None:
    return next(
        (
            pull_request
            for pull_request in pull_requests
            if pull_request.round_count < round_limit
            and actionable_threads(pull_request.threads, coder)
            and not is_waiting(pull_request.checks)
        ),
        None,
    )


def _run_fix_round(
    repository: GitHubRepository,
    git: Git,
    pull_request: BabysitPullRequest,
    coder: GitHubLogin,
    maintainer: GitHubLogin,
    default_branch: BranchName,
    babysitting: Babysitting,
    repository_checks: Checks,
) -> BabysitReport:
    workspace = git.workspace
    round_limit = babysitting.round_limit
    listed = pull_request.listed
    issue = _closed_issue(pull_request)
    round_number = pull_request.round_count + 1
    codex: CodingRun | None = None
    checks: ChecksPassed | ChecksFailed | None = None
    try:
        threads = actionable_threads(pull_request.threads, coder)
        trusted_authors = {maintainer, "greptile-apps", "greptile-apps[bot]"}
        if any(thread.comments[-1].author not in trusted_authors for thread in threads):
            raise CodingClientError("review feedback from an untrusted author needs a human")
        repository.comment_on_pull_request(
            listed.number,
            f"Babysit round {round_number} of {round_limit}: started.",
        )
        checkout_branch(git, listed.branch)
        starting_commit = current_commit(git)
        fix = fix_review_threads_with_codex(
            workspace,
            threads=threads,
            model=babysitting.model,
            effort=babysitting.effort,
            timeout_seconds=babysitting.timeout_seconds,
        )
        codex = fix.codex
        try:
            checks, check_fix = run_checks_with_fix(
                workspace,
                repository_checks,
                thread_id=codex.thread_id,
                model=babysitting.model,
                effort=babysitting.effort,
                timeout_seconds=babysitting.checks_fix_timeout_seconds,
            )
        except ChecksFixFailed as exc:
            checks = exc.checks
            if exc.implementation is not None:
                codex = _combined_codex_run(codex, exc.implementation)
            raise
        if check_fix is not None:
            codex = _combined_codex_run(codex, check_fix)
        commit = current_commit(git)
        verify_committed_workspace(git)
        if any(reply.fixed for reply in fix.replies) and commit == starting_commit:
            raise WorkspaceFileError("Codex reported fixes without advancing HEAD")
        push_checked_out_branch(git)
    except (
        CommandError,
        ChecksFixFailed,
        CodingClientError,
        WorkspaceFileError,
        RepositoryResponseError,
    ) as exc:
        failure = str(exc)
        failure = discard_branch_for_report(
            git,
            listed.branch,
            default_branch,
            failure,
        )
        try:
            repository.label_pull_request(listed.number, READY_FOR_HUMAN_LABEL)
        except CommandError as label_error:
            failure = f"{failure}; label update failed: {label_error}"
        return BabysitReport(
            pull_request_number=listed.number,
            pull_request_url=listed.url,
            issue_number=issue,
            outcome=BabysitOutcome.FAILED,
            round_number=round_number,
            threads_fixed=0,
            threads_answered=0,
            codex=codex,
            checks=checks,
            warnings=(),
            failure_reason=failure,
        )

    fixed = sum(reply.fixed for reply in fix.replies)
    answered = len(fix.replies) - fixed
    warnings = list(_publish_replies(repository, fix.replies, commit))
    try:
        repository.comment_on_pull_request(
            listed.number,
            f"Babysit result for round {round_number}: fixed {fixed}, answered {answered}.",
        )
    except (CommandError, RepositoryResponseError) as exc:
        warnings.append(BabysitWarning(f"round comment failed: {exc}"))
    try:
        discard_branch(git, listed.branch, default_branch)
    except (CommandError, WorkspaceFileError) as exc:
        warnings.append(BabysitWarning(f"workspace cleanup failed: {exc}"))
    return BabysitReport(
        pull_request_number=listed.number,
        pull_request_url=listed.url,
        issue_number=issue,
        outcome=BabysitOutcome.FIXED,
        round_number=round_number,
        threads_fixed=fixed,
        threads_answered=answered,
        codex=codex,
        checks=checks,
        warnings=tuple(warnings),
        failure_reason=None,
    )


def _publish_replies(
    repository: GitHubRepository,
    replies: tuple[ReviewReply, ...],
    commit: CommitSha,
) -> tuple[BabysitWarning, ...]:
    warnings: list[BabysitWarning] = []
    for reply in replies:
        body = f"{commit}: {reply.reply}" if reply.fixed else reply.reply
        try:
            repository.reply_to_review_thread(reply.thread, body)
        except (CommandError, RepositoryResponseError) as exc:
            warnings.append(BabysitWarning(f"reply to thread {reply.thread} failed: {exc}"))
            continue
        if reply.fixed:
            try:
                repository.resolve_review_thread(reply.thread)
            except (CommandError, RepositoryResponseError) as exc:
                warnings.append(BabysitWarning(f"resolving thread {reply.thread} failed: {exc}"))
    return tuple(warnings)


def _combined_codex_run(first: CodingRun, second: CodingRun) -> CodingRun:
    return CodingRun(
        first.thread_id,
        TokenUsage(
            input_tokens=first.usage.input_tokens + second.usage.input_tokens,
            cached_input_tokens=(
                first.usage.cached_input_tokens + second.usage.cached_input_tokens
            ),
            output_tokens=first.usage.output_tokens + second.usage.output_tokens,
        ),
        first.duration_seconds + second.duration_seconds,
    )


def _select_babysit_actions(
    pull_requests: tuple[BabysitPullRequest, ...],
    coder: GitHubLogin,
    maintainer: GitHubLogin,
    round_limit: int,
) -> tuple[_BabysitAction, ...]:
    """Return completed outcomes that still need a label or review request."""
    actions: list[_BabysitAction] = []
    for pull_request in pull_requests:
        outcome = _label_outcome(pull_request, coder, round_limit)
        if outcome is None:
            continue
        label = (
            MERGE_READY_LABEL if outcome is BabysitOutcome.MERGE_READY else READY_FOR_HUMAN_LABEL
        )
        add_label = label not in pull_request.listed.labels
        request_review = (
            outcome is BabysitOutcome.MERGE_READY
            and coder != pull_request.listed.author
            and maintainer not in pull_request.listed.requested_reviewers
            and not any(
                review.author == maintainer and review.commit == pull_request.listed.head
                for review in pull_request.reviews
            )
        )
        if not add_label and not request_review:
            continue
        issue = _closed_issue(pull_request)
        actions.append(
            _BabysitAction(
                pull_request,
                issue,
                outcome,
                label,
                add_label,
                request_review,
            )
        )
    return tuple(actions)


def _has_stale_readiness_label(
    pull_request: BabysitPullRequest,
    outcome: Literal[BabysitOutcome.MERGE_READY, BabysitOutcome.ROUND_LIMIT] | None,
) -> bool:
    labels = pull_request.listed.labels
    return (outcome is not BabysitOutcome.MERGE_READY and MERGE_READY_LABEL in labels) or (
        outcome is BabysitOutcome.MERGE_READY and READY_FOR_HUMAN_LABEL in labels
    )


def _closed_issue(pull_request: BabysitPullRequest) -> IssueNumber:
    issue = pull_request.listed.closed_issue
    if issue is None:
        raise ValueError("agent pull request body does not close an issue")
    return issue


def _reports_json(reports: list[BabysitReport]) -> str | None:
    if not reports:
        return None
    return report_json(tuple(reports))


def _label_report(action: _BabysitAction) -> BabysitReport:
    listed = action.pull_request.listed
    return BabysitReport(
        pull_request_number=listed.number,
        pull_request_url=listed.url,
        issue_number=action.issue,
        outcome=action.outcome,
        round_number=action.pull_request.round_count,
        threads_fixed=0,
        threads_answered=0,
        codex=None,
        checks=None,
        warnings=(),
        failure_reason=None,
    )

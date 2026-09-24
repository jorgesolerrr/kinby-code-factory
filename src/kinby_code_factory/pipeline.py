"""Run the delegated issue-to-pull-request pipeline."""

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
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
    review_with_claude,
    run_implementation,
)
from kinby_code_factory.config import factory_config
from kinby_code_factory.process import CommandError
from kinby_code_factory.pull_request import (
    Git,
    WorkspaceFileError,
    branch_name,
    discard_branch_for_report,
    open_pull_request,
    prepare_branch,
)
from kinby_code_factory.report import report_json
from kinby_code_factory.repository import (
    BranchName,
    GitHubRepository,
    Issue,
    PullRequestUrl,
    RepositoryResponseError,
    github_environment,
)
from kinby_code_factory.review import ReviewLoop, ReviewRound, run_review_loop
from kinby_code_factory.scan import (
    labeled_issue_number,
    oldest_eligible_issue,
    payload_can_change_eligibility,
    sibling_pull_requests,
)
from kinby_code_factory.skills import implementation_skills, review_skill

PipelineWarning = NewType("PipelineWarning", str)


class PipelineOutcome(StrEnum):
    """The outcome of a delegated pipeline run."""

    OPENED = "opened"
    OPENED_WITH_FINDINGS = "opened_with_findings"
    FAILED = "failed"


@dataclass(frozen=True)
class PullRequestReport:
    """The pull request opened by a delegated pipeline run."""

    url: PullRequestUrl
    branch: BranchName
    base_branch: BranchName


@dataclass(frozen=True)
class OpenedPipelineReport:
    """A delegated pipeline run that opened a pull request."""

    issue: Issue
    pull_request: PullRequestReport
    checks: ChecksPassed
    implementation: CodingRun
    review: ReviewLoop | None
    check_fix: CodingRun | None
    warnings: tuple[PipelineWarning, ...]
    duration_seconds: float
    outcome: Literal[
        PipelineOutcome.OPENED,
        PipelineOutcome.OPENED_WITH_FINDINGS,
    ] = PipelineOutcome.OPENED
    failure_reason: None = None


@dataclass(frozen=True)
class FailedPipelineReport:
    """A delegated pipeline run that failed before opening a pull request."""

    issue: Issue | None
    checks: ChecksFailed | None
    implementation: CodingRun | None
    review: ReviewLoop | None
    check_fix: CodingRun | None
    duration_seconds: float
    failure_reason: str
    outcome: Literal[PipelineOutcome.FAILED] = PipelineOutcome.FAILED
    pull_request: None = None


type PipelineReport = OpenedPipelineReport | FailedPipelineReport


@tool(write=True)
def implement_ready_issue(signal: dict[str, object], context: ToolContext) -> str | None:
    """Implement the oldest ready issue and open its pull request."""
    if not payload_can_change_eligibility(signal):
        return None
    config = factory_config(context)
    implementer = config.implementation
    reviewer = config.review
    labeled_issue = labeled_issue_number(signal)
    started_at = monotonic()
    environment = github_environment(config.secrets.github_token)
    repository = GitHubRepository(
        context.workspace,
        timeout_seconds=config.commands.github_timeout_seconds,
        environment=environment,
    )
    git = Git(context.workspace, config.commands.git_timeout_seconds, environment)
    conventions = context.instance.manifest.workspace.conventions.instructions
    issue: Issue | None = None
    implementation: CodingRun | None = None
    review: ReviewLoop | None = None
    check_fix: CodingRun | None = None
    checks: ChecksFailed | None = None
    branch: BranchName | None = None
    base_branch: BranchName | None = None
    warnings: tuple[PipelineWarning, ...] = ()
    report: PipelineReport
    try:
        skills = implementation_skills(context.instance, config.skills)
        reviewing = review_skill(context.instance, config.skills) if reviewer.enabled else None
        issues = repository.ready_issues()
        if labeled_issue is not None and all(issue.number != labeled_issue for issue in issues):
            fetched_issue = repository.ready_issue(labeled_issue)
        else:
            fetched_issue = None
        if fetched_issue is not None:
            issues = tuple(sorted((*issues, fetched_issue), key=lambda issue: issue.number))
        pull_requests = repository.agent_pull_requests()
        confirmed = () if fetched_issue is None else (fetched_issue.number,)
        selected = oldest_eligible_issue(repository, issues, pull_requests, confirmed)
        if selected is None:
            return None
        issue = selected
        siblings = sibling_pull_requests(issue, issues, pull_requests)
        metadata = repository.metadata()
        base_branch = siblings[-1].branch if siblings else metadata.default_branch
        branch = branch_name(issue)
        prepare_branch(git, branch, base_branch)
        implementation = run_implementation(
            context.workspace,
            client=implementer.client,
            issue_number=issue.number,
            issue_title=issue.title,
            issue_url=issue.url,
            skills=skills,
            model=implementer.model,
            effort=implementer.effort,
            timeout_seconds=implementer.timeout_seconds,
        )
        if reviewing is not None:
            review = run_review_loop(
                context.workspace,
                base_branch=base_branch,
                ticket_body=repository.issue_body(issue.number),
                review_skill=reviewing,
                conventions=conventions,
                thread_id=implementation.thread_id,
                implementer_client=implementer.client,
                implementer_model=implementer.model,
                implementer_effort=implementer.effort,
                reviewer_model=reviewer.model,
                reviewer_effort=reviewer.effort,
                round_limit=reviewer.round_limit,
                review_timeout_seconds=reviewer.timeout_seconds,
                fix_timeout_seconds=implementer.fix_timeout_seconds,
            )
        try:
            passed_checks, check_fix = run_checks_with_fix(
                context.workspace,
                config.checks,
                client=implementer.client,
                thread_id=implementation.thread_id,
                model=implementer.model,
                effort=implementer.effort,
                timeout_seconds=implementer.fix_timeout_seconds,
            )
        except ChecksFixFailed as exc:
            checks = exc.checks
            check_fix = exc.implementation
            raise
        if check_fix is not None and review is not None and reviewing is not None:
            final_review = review_with_claude(
                context.workspace,
                base_branch=base_branch,
                ticket_body=repository.issue_body(issue.number),
                review_skill=reviewing,
                conventions=conventions,
                model=reviewer.model,
                effort=reviewer.effort,
                timeout_seconds=reviewer.timeout_seconds,
            )
            review = ReviewLoop(
                (
                    *review.rounds,
                    ReviewRound(
                        number=len(review.rounds) + 1,
                        review=final_review,
                        fix=None,
                        hard_count=len(final_review.findings.hard),
                        suggestion_count=len(final_review.findings.suggestions),
                        fix_usage=None,
                    ),
                ),
                final_review.findings,
            )
        pull_request = open_pull_request(
            repository,
            git,
            issue,
            metadata,
            branch,
            base_branch,
            review.open_findings if review is not None else None,
        )
        if siblings:
            try:
                if (stack := siblings[-1].stack) is None:
                    previous_pull_requests = tuple(sibling.number for sibling in siblings)
                    repository.create_stack((*previous_pull_requests, pull_request.number))
                else:
                    repository.extend_stack(stack, pull_request.number)
            except CommandError as exc:
                warnings = (PipelineWarning(f"stack registration failed: {exc}"),)
        has_findings = review is not None and bool(
            review.open_findings.hard or review.open_findings.suggestions
        )
        report = OpenedPipelineReport(
            issue=issue,
            pull_request=PullRequestReport(pull_request.url, branch, base_branch),
            checks=passed_checks,
            implementation=implementation,
            review=review,
            check_fix=check_fix,
            warnings=warnings,
            duration_seconds=monotonic() - started_at,
            outcome=(
                PipelineOutcome.OPENED_WITH_FINDINGS if has_findings else PipelineOutcome.OPENED
            ),
        )
    except (
        CommandError,
        ChecksFixFailed,
        CodingClientError,
        WorkspaceFileError,
        RepositoryResponseError,
    ) as exc:
        failure = str(exc)
        if branch is not None and base_branch is not None:
            failure = discard_branch_for_report(git, branch, base_branch, failure)
        if issue is not None:
            try:
                repository.mark_ready_for_human(issue.number)
            except CommandError as label_error:
                failure = f"{failure}; label update failed: {label_error}"
        report = FailedPipelineReport(
            issue=issue,
            checks=checks,
            implementation=implementation,
            review=review,
            check_fix=check_fix,
            duration_seconds=monotonic() - started_at,
            failure_reason=failure,
        )
    return report_json(report)

"""Decide whether a routine wake can change issue eligibility."""

from collections.abc import Collection

from kinby_code_factory.repository import (
    AGENT_BRANCH_PREFIX,
    READY_LABEL,
    AgentPullRequest,
    GitHubRepository,
    Issue,
    IssueNumber,
    OpenBlocker,
)


def payload_can_change_eligibility(signal: dict[str, object]) -> bool:
    """Return whether a wake warrants a GitHub scan."""
    body = signal.get("body")
    if not isinstance(body, dict):
        return True
    if "comment" in body:
        return False
    if body.get("action") in {"labeled", "unlabeled"}:
        return _names_ready_label(body)
    pull_request = body.get("pull_request")
    if not isinstance(pull_request, dict):
        return True
    head = pull_request.get("head")
    return (
        isinstance(head, dict)
        and isinstance(branch := head.get("ref"), str)
        and branch.startswith(AGENT_BRANCH_PREFIX)
    )


def labeled_issue_number(signal: dict[str, object]) -> IssueNumber | None:
    """Return the issue named by a ready-label delivery."""
    body = signal.get("body")
    if (
        not isinstance(body, dict)
        or body.get("action") != "labeled"
        or not _names_ready_label(body)
    ):
        return None
    issue = body.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue:
        return None
    number = issue.get("number")
    return IssueNumber(number) if isinstance(number, int) else None


def oldest_eligible_issue(
    repository: GitHubRepository,
    issues: tuple[Issue, ...],
    pull_requests: tuple[AgentPullRequest, ...],
    confirmed: Collection[IssueNumber] = (),
) -> Issue | None:
    """Return the lowest-numbered issue whose blockers are covered in its stack.

    The issues list can lag a close or a label change, so a listed issue is read
    again on its own before it is selected. `confirmed` names issues already read.
    """
    covered = {
        pull_request.closed_issue
        for pull_request in pull_requests
        if pull_request.closed_issue is not None
    }
    for issue in issues:
        if issue.number in covered:
            continue
        blockers = repository.open_blockers(issue.number)
        if not all(_covered_in_same_stack(issue, blocker, covered) for blocker in blockers):
            continue
        if issue.number in confirmed or repository.ready_issue(issue.number) is not None:
            return issue
    return None


def sibling_pull_requests(
    issue: Issue,
    issues: tuple[Issue, ...],
    pull_requests: tuple[AgentPullRequest, ...],
) -> tuple[AgentPullRequest, ...]:
    """Return a sub-issue's sibling pull requests from oldest to newest."""
    if issue.parent is None:
        return ()
    sibling_numbers = {sibling.number for sibling in issues if sibling.parent == issue.parent}
    newest_first = tuple(
        pull_request
        for pull_request in pull_requests
        if pull_request.closed_issue in sibling_numbers
    )
    return tuple(reversed(newest_first))


def _covered_in_same_stack(
    issue: Issue,
    blocker: OpenBlocker,
    covered: set[IssueNumber],
) -> bool:
    return blocker.number in covered and _stack(issue.number, issue.parent) == _stack(
        blocker.number, blocker.parent
    )


def _stack(issue: IssueNumber, parent: IssueNumber | None) -> IssueNumber:
    return parent if parent is not None else issue


def _names_ready_label(body: dict[str, object]) -> bool:
    label = body.get("label")
    return isinstance(label, dict) and label.get("name") == READY_LABEL

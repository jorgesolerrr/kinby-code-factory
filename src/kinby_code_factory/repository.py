"""Use the GitHub CLI as the delegated pipeline's repository boundary."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NewType

from kinby_code_factory.process import run_command

GITHUB_API_VERSION = "2026-03-10"
AGENT_BRANCH_PREFIX = "agent/"
_CLOSES_ISSUE = re.compile(r"(?im)^Closes #(\d+)\s*$")
_ISSUE_URL_NUMBER = re.compile(r"/issues/(\d+)$")
_PULL_REQUEST_URL_NUMBER = re.compile(r"/pull/(\d+)$")
_BABYSIT_ROUND_PREFIX = "Babysit round "
IssueTitle = NewType("IssueTitle", str)
IssueUrl = NewType("IssueUrl", str)
IssueNumber = NewType("IssueNumber", int)
BranchName = NewType("BranchName", str)
CommitSha = NewType("CommitSha", str)
LabelName = NewType("LabelName", str)
RepositoryName = NewType("RepositoryName", str)
PullRequestUrl = NewType("PullRequestUrl", str)
PullRequestNumber = NewType("PullRequestNumber", int)
StackNumber = NewType("StackNumber", int)
GitHubLogin = NewType("GitHubLogin", str)
ReviewThreadId = NewType("ReviewThreadId", str)
READY_LABEL = LabelName("ready-for-agent")
READY_FOR_HUMAN_LABEL = LabelName("ready-for-human")


class RepositoryResponseError(ValueError):
    """A GitHub CLI response does not match the requested shape."""


class MissingGitHubToken(RuntimeError):
    """The environment variable package.yaml names for the GitHub token is unset."""


def github_environment(token_variable: str) -> dict[str, str]:
    """The environment gh and git push run with: the configured token as GH_TOKEN."""
    token = os.environ.get(token_variable)
    if not token:
        raise MissingGitHubToken(
            f"{token_variable} is not set, so gh and git push have no GitHub token."
        )
    return {**os.environ, "GH_TOKEN": token}


class CheckRunStatus(StrEnum):
    """A lifecycle status reported for a GitHub check run."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    WAITING = "waiting"
    REQUESTED = "requested"
    PENDING = "pending"


@dataclass(frozen=True)
class Issue:
    """An open issue ready for the coding client."""

    number: IssueNumber
    title: IssueTitle
    url: IssueUrl
    parent: IssueNumber | None = None


@dataclass(frozen=True)
class OpenBlocker:
    """An open issue that blocks another issue."""

    number: IssueNumber
    parent: IssueNumber | None


@dataclass(frozen=True)
class AgentPullRequest:
    """An open pull request created by the delegated pipeline."""

    number: PullRequestNumber
    url: PullRequestUrl
    branch: BranchName
    head: CommitSha
    body: str
    stack: StackNumber | None
    author: GitHubLogin
    labels: tuple[LabelName, ...]
    requested_reviewers: tuple[GitHubLogin, ...] = ()

    @property
    def closed_issue(self) -> IssueNumber | None:
        return closed_issue_number(self.body)


@dataclass(frozen=True)
class OpenedPullRequest:
    """A pull request created by the delegated pipeline."""

    number: PullRequestNumber
    url: PullRequestUrl


@dataclass(frozen=True)
class RepositoryMetadata:
    """The identity and default branch of a GitHub repository."""

    maintainer: GitHubLogin
    name: RepositoryName
    default_branch: BranchName


@dataclass(frozen=True)
class CheckRun:
    """One check run on a pull request head."""

    status: CheckRunStatus


@dataclass(frozen=True)
class ReviewComment:
    """One comment in a pull request review thread."""

    author: GitHubLogin | None
    body: str
    commit: CommitSha | None


@dataclass(frozen=True)
class ReviewThread:
    """One GitHub pull request review thread."""

    id: ReviewThreadId
    resolved: bool
    path: str
    line: int | None
    comments: tuple[ReviewComment, ...]


@dataclass(frozen=True)
class PullRequestReview:
    """One submitted review and the commit it reviewed."""

    commit: CommitSha | None
    author: GitHubLogin | None = None


@dataclass(frozen=True)
class BabysitPullRequest:
    """An agent pull request with all state needed by the babysitter."""

    listed: AgentPullRequest
    checks: tuple[CheckRun, ...]
    threads: tuple[ReviewThread, ...]
    reviews: tuple[PullRequestReview, ...]
    round_count: int


_REVIEW_THREADS_QUERY = """query BabysitReviewThreads(
  $owner: String!, $name: String!, $number: Int!, $endCursor: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes {
          id
          isResolved
          path
          line
          comments(last: 100) {
            nodes { author { login } body commit { oid } }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
_REPLY_TO_REVIEW_THREAD_MUTATION = """mutation ReplyToReviewThread(
  $threadId: ID!, $body: String!
) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}"""
_RESOLVE_REVIEW_THREAD_MUTATION = """mutation ResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}"""


class GitHubRepository:
    """The GitHub operations owned by one pipeline run."""

    def __init__(
        self,
        workspace: Path,
        *,
        timeout_seconds: float,
        environment: Mapping[str, str],
    ) -> None:
        self._workspace = workspace
        self._timeout_seconds = timeout_seconds
        self._environment = environment

    def ready_issues(self) -> tuple[Issue, ...]:
        result = self._paginated_api(
            "repos/{owner}/{repo}/issues",
            "state=open",
            f"labels={READY_LABEL}",
        )
        return tuple(sorted(_issues(result), key=lambda issue: issue.number))

    def ready_issue(self, issue: IssueNumber) -> Issue | None:
        """Return an issue when it is currently open and ready for an agent."""
        result = self._gh(
            "api",
            "--method",
            "GET",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            f"repos/{{owner}}/{{repo}}/issues/{issue}",
        )
        return _ready_issue(result)

    def agent_pull_requests(self) -> tuple[AgentPullRequest, ...]:
        result = self._paginated_api(
            "repos/{owner}/{repo}/pulls",
            "state=open",
            "sort=created",
            "direction=desc",
        )
        return select_agent_pull_requests(tuple(_pull_requests(result)))

    def open_blockers(self, issue: IssueNumber) -> tuple[OpenBlocker, ...]:
        """Return every open issue that blocks an issue."""
        result = self._paginated_api(
            f"repos/{{owner}}/{{repo}}/issues/{issue}/dependencies/blocked_by"
        )
        return tuple(_open_blockers(result))

    def issue_body(self, issue: IssueNumber) -> str:
        """Return the source Markdown for an issue."""
        source = self._gh(
            "api",
            "--method",
            "GET",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            f"repos/{{owner}}/{{repo}}/issues/{issue}",
            "--jq",
            '.body // ""',
        )
        return source.rstrip("\n")

    def metadata(self) -> RepositoryMetadata:
        return _metadata(self._gh("repo", "view", "--json", "name,owner,defaultBranchRef"))

    def current_login(self) -> GitHubLogin:
        """Return the login used by the GitHub CLI."""
        login = self._gh("api", "user", "--jq", ".login").strip()
        if not login:
            raise RepositoryResponseError("gh api user returned an empty login")
        return GitHubLogin(login)

    def pull_request_branch(self, pull_request: PullRequestNumber) -> BranchName:
        """Return a pull request's head branch."""
        branch = self._gh(
            "pr",
            "view",
            str(pull_request),
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
        ).strip()
        if not branch:
            raise RepositoryResponseError("gh pr view returned an empty head branch")
        return BranchName(branch)

    def babysit_pull_requests(
        self,
        coder: GitHubLogin,
        metadata: RepositoryMetadata,
    ) -> tuple[BabysitPullRequest, ...]:
        """Return open agent pull requests oldest first with their review state."""
        source = self._paginated_api(
            "repos/{owner}/{repo}/pulls",
            "state=open",
            "sort=created",
            "direction=asc",
        )
        pull_requests = select_agent_pull_requests(tuple(_pull_requests(source)))
        return tuple(
            self._complete_babysit_pull_request(pull_request, coder, metadata)
            for pull_request in pull_requests
        )

    def label_pull_request(
        self,
        pull_request: PullRequestNumber,
        label: LabelName,
    ) -> None:
        """Add one repository label to a pull request."""
        self._gh("pr", "edit", str(pull_request), "--add-label", label)

    def remove_pull_request_label(
        self,
        pull_request: PullRequestNumber,
        label: LabelName,
    ) -> None:
        """Remove one repository label from a pull request."""
        self._gh("pr", "edit", str(pull_request), "--remove-label", label)

    def request_review(
        self,
        pull_request: PullRequestNumber,
        reviewer: GitHubLogin,
    ) -> None:
        """Request a pull request review from one login."""
        self._gh("pr", "edit", str(pull_request), "--add-reviewer", reviewer)

    def reply_to_review_thread(self, thread: ReviewThreadId, body: str) -> None:
        """Post one reply on a pull request review thread."""
        self._gh(
            "api",
            "graphql",
            "-f",
            f"query={_REPLY_TO_REVIEW_THREAD_MUTATION}",
            "-F",
            f"threadId={thread}",
            "-f",
            f"body={body}",
        )

    def resolve_review_thread(self, thread: ReviewThreadId) -> None:
        """Resolve one pull request review thread."""
        self._gh(
            "api",
            "graphql",
            "-f",
            f"query={_RESOLVE_REVIEW_THREAD_MUTATION}",
            "-F",
            f"threadId={thread}",
        )

    def comment_on_pull_request(
        self,
        pull_request: PullRequestNumber,
        body: str,
    ) -> None:
        """Post one issue comment on a pull request."""
        self._gh("pr", "comment", str(pull_request), "--body", body)

    def open_pull_request(
        self,
        *,
        branch: BranchName,
        base_branch: BranchName,
        title: IssueTitle,
        body_file: Path,
        reviewer: GitHubLogin,
    ) -> OpenedPullRequest:
        url = PullRequestUrl(
            self._gh(
                "pr",
                "create",
                "--head",
                branch,
                "--base",
                base_branch,
                "--title",
                title,
                "--body-file",
                str(body_file),
                "--reviewer",
                reviewer,
            ).strip()
        )
        match = _PULL_REQUEST_URL_NUMBER.search(url)
        if match is None:
            raise RepositoryResponseError("gh pr create returned an invalid pull request URL")
        return OpenedPullRequest(PullRequestNumber(int(match.group(1))), url)

    def create_stack(self, pull_requests: tuple[PullRequestNumber, ...]) -> None:
        """Create a stack from pull requests ordered bottom to top."""
        arguments = [
            "api",
            "--method",
            "POST",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            "repos/{owner}/{repo}/stacks",
        ]
        for pull_request in pull_requests:
            arguments.extend(("-F", f"pull_requests[]={pull_request}"))
        self._gh(*arguments)

    def extend_stack(
        self,
        stack: StackNumber,
        pull_request: PullRequestNumber,
    ) -> None:
        """Append one pull request to the top of an existing stack."""
        self._gh(
            "api",
            "--method",
            "POST",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            f"repos/{{owner}}/{{repo}}/stacks/{stack}/add",
            "-F",
            f"pull_requests[]={pull_request}",
        )

    def mark_ready_for_human(self, issue: IssueNumber) -> None:
        self._gh(
            "issue",
            "edit",
            str(issue),
            "--remove-label",
            READY_LABEL,
            "--add-label",
            READY_FOR_HUMAN_LABEL,
        )

    def _complete_babysit_pull_request(
        self,
        pull_request: AgentPullRequest,
        coder: GitHubLogin,
        metadata: RepositoryMetadata,
    ) -> BabysitPullRequest:
        checks = _check_runs(
            self._paginated_api(f"repos/{{owner}}/{{repo}}/commits/{pull_request.head}/check-runs")
        )
        threads = _review_threads(
            self._gh(
                "api",
                "graphql",
                "--paginate",
                "--slurp",
                "-f",
                f"query={_REVIEW_THREADS_QUERY}",
                "-F",
                f"owner={metadata.maintainer}",
                "-F",
                f"name={metadata.name}",
                "-F",
                f"number={pull_request.number}",
            )
        )
        reviews = _reviews(
            self._paginated_api(f"repos/{{owner}}/{{repo}}/pulls/{pull_request.number}/reviews")
        )
        round_count = _round_count(
            self._paginated_api(f"repos/{{owner}}/{{repo}}/issues/{pull_request.number}/comments"),
            coder,
        )
        return BabysitPullRequest(
            listed=pull_request,
            checks=checks,
            threads=threads,
            reviews=reviews,
            round_count=round_count,
        )

    def _gh(self, *arguments: str) -> str:
        return run_command(
            ("gh", *arguments),
            cwd=self._workspace,
            timeout_seconds=self._timeout_seconds,
            env=self._environment,
        ).stdout

    def _paginated_api(self, endpoint: str, *fields: str) -> str:
        arguments = [
            "api",
            "--method",
            "GET",
            "-H",
            f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            "--paginate",
            "--slurp",
            endpoint,
            "-f",
            "per_page=100",
        ]
        for field in fields:
            arguments.extend(("-f", field))
        return self._gh(*arguments)


def closed_issue_number(body: str) -> IssueNumber | None:
    """Return the issue closed by a pull request body."""
    match = _CLOSES_ISSUE.search(body)
    return IssueNumber(int(match.group(1))) if match is not None else None


def select_agent_pull_requests(
    pull_requests: tuple[AgentPullRequest, ...],
) -> tuple[AgentPullRequest, ...]:
    """Return pull requests whose head branch marks factory work."""
    return tuple(
        pull_request
        for pull_request in pull_requests
        if pull_request.branch.startswith(AGENT_BRANCH_PREFIX)
    )


def _issues(source: str) -> list[Issue]:
    values = _page_items(source, "issue list")
    issues: list[Issue] = []
    for value in values:
        if issue := _issue(value, "issue list"):
            issues.append(issue)
    return issues


def _ready_issue(source: str) -> Issue | None:
    value = json.loads(source)
    if not isinstance(value, dict):
        raise RepositoryResponseError("gh api issue returned a non-object issue")
    state = value.get("state")
    labels = value.get("labels")
    if not isinstance(state, str) or not isinstance(labels, list):
        raise RepositoryResponseError("gh api issue returned invalid state or labels")
    if state != "open" or not any(
        isinstance(label, dict) and label.get("name") == READY_LABEL for label in labels
    ):
        return None
    return _issue(value, "api issue")


def _issue(value: object, operation: str) -> Issue | None:
    if not isinstance(value, dict):
        raise RepositoryResponseError(f"gh {operation} returned a non-object issue")
    if "pull_request" in value:
        return None
    number = value.get("number")
    title = value.get("title")
    url = value.get("html_url", value.get("url"))
    if not isinstance(number, int) or not isinstance(title, str) or not isinstance(url, str):
        raise RepositoryResponseError(f"gh {operation} returned an invalid issue")
    return Issue(
        IssueNumber(number),
        IssueTitle(title),
        IssueUrl(url),
        _parent_number(value.get("parent_issue_url")),
    )


def _pull_requests(source: str) -> list[AgentPullRequest]:
    values = _page_items(source, "pull request list")
    pull_requests: list[AgentPullRequest] = []
    for value in values:
        if not isinstance(value, dict):
            raise RepositoryResponseError("gh pr list returned a non-object pull request")
        number = value.get("number")
        url = value.get("html_url", value.get("url"))
        head = value.get("head")
        body = value.get("body")
        author = value.get("user")
        labels = value.get("labels")
        requested_reviewers = value.get("requested_reviewers", [])
        if (
            not isinstance(number, int)
            or not isinstance(url, str)
            or not isinstance(head, dict)
            or not isinstance(branch := head.get("ref"), str)
            or not isinstance(sha := head.get("sha"), str)
            or not isinstance(body, str)
            or not isinstance(author, dict)
            or not isinstance(login := author.get("login"), str)
            or not isinstance(labels, list)
            or not isinstance(requested_reviewers, list)
        ):
            raise RepositoryResponseError("gh pr list returned an invalid pull request")
        pull_requests.append(
            AgentPullRequest(
                number=PullRequestNumber(number),
                url=PullRequestUrl(url),
                branch=BranchName(branch),
                head=CommitSha(sha),
                body=body,
                stack=_stack_number(value.get("stack")),
                author=GitHubLogin(login),
                labels=tuple(_label_names(labels)),
                requested_reviewers=tuple(_reviewer_logins(requested_reviewers)),
            )
        )
    return pull_requests


def _open_blockers(source: str) -> list[OpenBlocker]:
    blockers: list[OpenBlocker] = []
    for value in _page_items(source, "blocked-by list"):
        if not isinstance(value, dict):
            raise RepositoryResponseError("gh blocked-by list returned a non-object issue")
        number = value.get("number")
        state = value.get("state")
        if not isinstance(number, int) or not isinstance(state, str):
            raise RepositoryResponseError("gh blocked-by list returned an invalid issue")
        if state == "open":
            blockers.append(
                OpenBlocker(
                    IssueNumber(number),
                    _parent_number(value.get("parent_issue_url")),
                )
            )
    return blockers


def _page_items(source: str, operation: str) -> list[object]:
    values = json.loads(source)
    if not isinstance(values, list):
        raise RepositoryResponseError(f"gh {operation} returned a non-list JSON value")
    if not values or not all(isinstance(page, list) for page in values):
        return values
    return [item for page in values for item in page]


def _object_pages(source: str, operation: str) -> list[dict[str, object]]:
    values = json.loads(source)
    pages = values if isinstance(values, list) else [values]
    objects: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, dict):
            raise RepositoryResponseError(f"gh {operation} returned a non-object page")
        objects.append(page)
    return objects


def _parent_number(value: object) -> IssueNumber | None:
    if value is None:
        return None
    if not isinstance(value, str) or (match := _ISSUE_URL_NUMBER.search(value)) is None:
        raise RepositoryResponseError("gh returned an invalid parent issue URL")
    return IssueNumber(int(match.group(1)))


def _stack_number(value: object) -> StackNumber | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(number := value.get("number"), int):
        raise RepositoryResponseError("gh returned an invalid pull request stack")
    return StackNumber(number)


def _metadata(source: str) -> RepositoryMetadata:
    value = json.loads(source)
    if not isinstance(value, dict):
        raise RepositoryResponseError("gh repo view returned a non-object JSON value")
    owner = value.get("owner")
    name = value.get("name")
    default_branch = value.get("defaultBranchRef")
    if not isinstance(owner, dict) or not isinstance(default_branch, dict):
        raise RepositoryResponseError("gh repo view returned invalid repository metadata")
    maintainer = owner.get("login")
    branch = default_branch.get("name")
    if not isinstance(maintainer, str) or not isinstance(name, str) or not isinstance(branch, str):
        raise RepositoryResponseError("gh repo view returned invalid repository metadata")
    return RepositoryMetadata(
        GitHubLogin(maintainer),
        RepositoryName(name),
        BranchName(branch),
    )


def _label_names(values: list[object]) -> list[LabelName]:
    labels: list[LabelName] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(name := value.get("name"), str):
            raise RepositoryResponseError("gh pr list returned an invalid label")
        labels.append(LabelName(name))
    return labels


def _reviewer_logins(values: list[object]) -> list[GitHubLogin]:
    reviewers: list[GitHubLogin] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(login := value.get("login"), str):
            raise RepositoryResponseError("gh pr list returned an invalid requested reviewer")
        reviewers.append(GitHubLogin(login))
    return reviewers


def _check_runs(source: str) -> tuple[CheckRun, ...]:
    checks: list[CheckRun] = []
    for page in _object_pages(source, "check run list"):
        if not isinstance(runs := page.get("check_runs"), list):
            raise RepositoryResponseError("gh check run list returned an invalid response")
        for run in runs:
            if not isinstance(run, dict) or not isinstance(status := run.get("status"), str):
                raise RepositoryResponseError("gh check run list returned an invalid check run")
            try:
                parsed_status = CheckRunStatus(status)
            except ValueError as exc:
                raise RepositoryResponseError(
                    f"gh check run list returned unknown status {status!r}"
                ) from exc
            checks.append(CheckRun(parsed_status))
    return tuple(checks)


def _review_threads(source: str) -> tuple[ReviewThread, ...]:
    threads: list[ReviewThread] = []
    for page in _object_pages(source, "review thread list"):
        nodes = _review_thread_nodes(page)
        threads.extend(_review_thread(node) for node in nodes)
    return tuple(threads)


def _review_thread_nodes(page: dict[str, object]) -> list[object]:
    data = page.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    review_threads = pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
    nodes = review_threads.get("nodes") if isinstance(review_threads, dict) else None
    if not isinstance(nodes, list):
        raise RepositoryResponseError("gh review thread list returned an invalid response")
    return nodes


def _review_thread(value: object) -> ReviewThread:
    if not isinstance(value, dict):
        raise RepositoryResponseError("gh review thread list returned a non-object thread")
    thread_id = value.get("id")
    resolved = value.get("isResolved")
    path = value.get("path")
    line = value.get("line")
    comments_value = value.get("comments")
    comments = comments_value.get("nodes") if isinstance(comments_value, dict) else None
    if (
        not isinstance(thread_id, str)
        or not isinstance(resolved, bool)
        or not isinstance(path, str)
        or not (line is None or isinstance(line, int))
        or not isinstance(comments, list)
    ):
        raise RepositoryResponseError("gh review thread list returned an invalid thread")
    return ReviewThread(
        ReviewThreadId(thread_id),
        resolved,
        path,
        line,
        tuple(_review_comment(comment) for comment in comments),
    )


def _review_comment(value: object) -> ReviewComment:
    if not isinstance(value, dict) or not isinstance(body := value.get("body"), str):
        raise RepositoryResponseError("gh review thread list returned an invalid comment")
    author_value = value.get("author")
    login = author_value.get("login") if isinstance(author_value, dict) else None
    commit_value = value.get("commit")
    commit = commit_value.get("oid") if isinstance(commit_value, dict) else None
    if not (login is None or isinstance(login, str)) or not (
        commit is None or isinstance(commit, str)
    ):
        raise RepositoryResponseError("gh review thread list returned an invalid comment")
    return ReviewComment(
        GitHubLogin(login) if login is not None else None,
        body,
        CommitSha(commit) if commit is not None else None,
    )


def _reviews(source: str) -> tuple[PullRequestReview, ...]:
    reviews: list[PullRequestReview] = []
    for value in _page_items(source, "review list"):
        if not isinstance(value, dict):
            raise RepositoryResponseError("gh review list returned a non-object review")
        commit = value.get("commit_id")
        user = value.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        if not (commit is None or isinstance(commit, str)) or not (
            author is None or isinstance(author, str)
        ):
            raise RepositoryResponseError("gh review list returned an invalid review")
        reviews.append(
            PullRequestReview(
                CommitSha(commit) if commit is not None else None,
                GitHubLogin(author) if author is not None else None,
            )
        )
    return tuple(reviews)


def _round_count(source: str, coder: GitHubLogin) -> int:
    count = 0
    for value in _page_items(source, "issue comment list"):
        if not isinstance(value, dict):
            raise RepositoryResponseError("gh issue comment list returned a non-object comment")
        user = value.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        body = value.get("body")
        if not isinstance(author, str) or not isinstance(body, str):
            raise RepositoryResponseError("gh issue comment list returned an invalid comment")
        if author == coder and body.startswith(_BABYSIT_ROUND_PREFIX):
            count += 1
    return count

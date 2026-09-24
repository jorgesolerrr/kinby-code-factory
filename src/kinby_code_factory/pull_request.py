"""Prepare, check, push, and open an agent pull request."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kinby_code_factory.clients import PR_BODY, REVIEW_REPLIES, Findings
from kinby_code_factory.process import CommandError, CommandResult, run_command
from kinby_code_factory.repository import (
    AGENT_BRANCH_PREFIX,
    BranchName,
    CommitSha,
    GitHubRepository,
    Issue,
    OpenedPullRequest,
    RepositoryMetadata,
    closed_issue_number,
)

_SCRATCH_EXCLUDE = ".scratch/"


@dataclass(frozen=True)
class Git:
    """Git in the persistent workspace, with its time limit and GitHub credentials."""

    workspace: Path
    timeout_seconds: float
    environment: Mapping[str, str]

    def __call__(self, *arguments: str) -> CommandResult:
        return run_command(
            ("git", *arguments),
            cwd=self.workspace,
            timeout_seconds=self.timeout_seconds,
            env=self.environment,
        )


class WorkspaceFileError(RuntimeError):
    """The pipeline could not read, update, or clear a workspace file."""


def branch_name(issue: Issue) -> BranchName:
    """Return the stable agent branch for an issue."""
    words = re.findall(r"[a-z0-9]+", issue.title.lower())
    slug = "-".join(words) or "issue"
    return BranchName(f"{AGENT_BRANCH_PREFIX}{issue.number}-{slug}")


def prepare_branch(git: Git, branch: BranchName, base_branch: BranchName) -> None:
    """Start an agent branch from the freshly fetched base in a clean persistent workspace.

    A leftover remote branch of the same name is ignored. An eligible issue has no open
    pull request, so that branch is a merged or abandoned run and would hide what the
    base already contains.
    """
    _clean_workspace(git)
    git("fetch", "--prune", "origin")
    git("switch", "--discard-changes", "-C", branch, f"origin/{base_branch}")
    _clean_workspace(git)


def checkout_branch(git: Git, branch: BranchName) -> None:
    """Check out an existing agent pull request branch without rebasing it."""
    _clean_workspace(git)
    git("fetch", "origin")
    git(
        "switch",
        "--discard-changes",
        "-C",
        branch,
        f"origin/{branch}",
    )
    _clean_workspace(git)


def current_commit(git: Git) -> CommitSha:
    """Return the checked-out commit."""
    commit = git("rev-parse", "HEAD").stdout.strip()
    if not commit:
        raise CommandError("git rev-parse returned an empty commit")
    return CommitSha(commit)


def push_checked_out_branch(git: Git) -> None:
    """Push the checked-out pull request branch without rewriting history."""
    git("push")


def verify_committed_workspace(git: Git) -> None:
    """Reject checked changes that are absent from HEAD."""
    status = git("status", "--porcelain", "--untracked-files=all").stdout
    generated = {PR_BODY.as_posix(), REVIEW_REPLIES.as_posix()}
    if any(line[3:] not in generated for line in status.splitlines()):
        raise WorkspaceFileError("Codex left uncommitted workspace changes")


def discard_branch(
    git: Git,
    branch: BranchName,
    base_branch: BranchName,
) -> None:
    """Discard a local branch and return the clean workspace to its base."""
    _clean_workspace(git)
    git(
        "switch",
        "--discard-changes",
        "-C",
        base_branch,
        f"origin/{base_branch}",
    )
    git("branch", "-D", branch)


def discard_branch_for_report(
    git: Git,
    branch: BranchName,
    base_branch: BranchName,
    failure_reason: str,
) -> str:
    """Discard a branch and include any cleanup error in its report reason."""
    try:
        discard_branch(git, branch, base_branch)
    except (CommandError, WorkspaceFileError) as cleanup_error:
        return f"{failure_reason}; workspace cleanup failed: {cleanup_error}"
    return failure_reason


def open_pull_request(
    repository: GitHubRepository,
    git: Git,
    issue: Issue,
    metadata: RepositoryMetadata,
    branch: BranchName,
    base_branch: BranchName,
    open_findings: Findings | None,
) -> OpenedPullRequest:
    """Push the checked branch and open its pull request."""
    git("push", "--force-with-lease", "-u", "origin", branch)
    body_file = git.workspace / PR_BODY
    try:
        body = body_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkspaceFileError(f"could not read pull request body: {exc}") from exc
    if closed_issue_number(body.partition("\n")[0]) == issue.number:
        body = body.partition("\n")[2].lstrip()
    findings = _open_findings(open_findings)
    try:
        body_file.write_text(
            f"Closes #{issue.number}\n\n{body.rstrip()}{findings}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise WorkspaceFileError(f"could not update pull request body: {exc}") from exc
    return repository.open_pull_request(
        branch=branch,
        base_branch=base_branch,
        title=issue.title,
        body_file=body_file,
        reviewer=metadata.maintainer,
    )


def _open_findings(findings: Findings | None) -> str:
    if findings is None:
        return (
            "\n\n## Review status\n\n"
            "Adversarial review was not run. Review happens on this pull request."
        )
    items = tuple(f"- [hard] {item}" for item in findings.hard) + tuple(
        f"- [suggestion] {item}" for item in findings.suggestions
    )
    if not items:
        return ""
    return "\n\n## Open review findings\n\n" + "\n".join(items)


def _clean_workspace(git: Git) -> None:
    _exclude_scratch(git)
    git("reset", "--hard")
    git("clean", "-fd")
    for path in (PR_BODY, REVIEW_REPLIES):
        try:
            (git.workspace / path).unlink(missing_ok=True)
        except OSError as exc:
            raise WorkspaceFileError(f"could not clear {path}: {exc}") from exc


def _exclude_scratch(git: Git) -> None:
    """Keep the pipeline's scratch files out of any repository's commits and status."""
    exclude = git.workspace / ".git" / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if _SCRATCH_EXCLUDE not in current.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            separator = "" if not current or current.endswith("\n") else "\n"
            exclude.write_text(f"{current}{separator}{_SCRATCH_EXCLUDE}\n", encoding="utf-8")
    except OSError as exc:
        raise WorkspaceFileError(f"could not exclude {_SCRATCH_EXCLUDE}: {exc}") from exc

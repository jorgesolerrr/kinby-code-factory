"""Run coding clients and parse their machine-readable results."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NewType

from kinby.plugins import Skill

from kinby_code_factory.process import run_command
from kinby_code_factory.repository import (
    IssueNumber,
    IssueTitle,
    IssueUrl,
    ReviewThread,
    ReviewThreadId,
)

PR_BODY = Path(".scratch/pr-body.md")
REVIEW_REPLIES = Path(".scratch/review-replies.json")
TICKET_BODY = Path(".scratch/factory-ticket.md")
CodingModel = NewType("CodingModel", str)
CodingSessionId = NewType("CodingSessionId", str)
# Claude Code bills an API key over the subscription token when both are set.
_API_KEY = "ANTHROPIC_API_KEY"
_EMPHASIS = r"(?:\*\*|__|`)?"
_FINDING = re.compile(
    rf"^\s*(?:[-*+]\s*)?{_EMPHASIS}\[(hard|suggestion)\]{_EMPHASIS}\s*(.+)$",
    re.IGNORECASE,
)
_NO_FINDINGS = re.compile(r"^[\s*_`]*no findings[\s*_`.!]*$", re.IGNORECASE)
_REVIEW_EXCERPT_CHARACTERS = 400
_ANSWER_SHAPE = (
    "Answer with the findings only, one per line, plain tags without Markdown emphasis. "
    "Do not add headings, a summary, or a Reviewer line.\n"
)


class CodingClient(StrEnum):
    """The command-line client that owns an implementation session."""

    CODEX = "codex"
    CLAUDE = "claude"


class ReasoningEffort(StrEnum):
    """A reasoning effort requested from a coding client."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class CodingClientError(RuntimeError):
    """A coding client's workspace input or response is invalid."""


@dataclass(frozen=True)
class TokenUsage:
    """Tokens reported by a coding client."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class CodingRun:
    """The observable result of one coding client invocation."""

    thread_id: CodingSessionId
    usage: TokenUsage
    duration_seconds: float
    client: CodingClient = CodingClient.CODEX


@dataclass(frozen=True)
class ReviewReply:
    """Codex's answer to one actionable review thread."""

    thread: ReviewThreadId
    fixed: bool
    reply: str


@dataclass(frozen=True)
class ReviewFixRun:
    """One fresh Codex fix run and its review replies."""

    codex: CodingRun
    replies: tuple[ReviewReply, ...]


@dataclass(frozen=True)
class Findings:
    """Tagged findings returned by both review axes."""

    hard: tuple[str, ...]
    suggestions: tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class ImplementationSkills:
    """The skills an implementation prompt carries, and every other skill it may read."""

    implement: Skill
    pull_request: Skill
    available: tuple[Skill, ...]


@dataclass(frozen=True)
class ReviewRun:
    """The merged result of one parallel two-axis review."""

    findings: Findings
    duration_seconds: float


def run_implementation(
    workspace: Path,
    *,
    client: CodingClient,
    issue_number: IssueNumber,
    issue_title: IssueTitle,
    issue_url: IssueUrl,
    skills: ImplementationSkills,
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
) -> CodingRun:
    """Implement one issue with the selected coding client."""
    prompt = _prompt(issue_number, issue_title, issue_url, skills)
    if client is CodingClient.CODEX:
        return run_codex(
            workspace,
            prompt=prompt,
            model=model,
            effort=effort,
            timeout_seconds=timeout_seconds,
        )
    return _run_claude(
        workspace,
        prompt=prompt,
        model=model,
        effort=effort,
        timeout_seconds=timeout_seconds,
    )


def fix_implementation(
    workspace: Path,
    *,
    client: CodingClient,
    thread_id: CodingSessionId,
    findings: Findings,
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
) -> CodingRun:
    """Resume the same coding client session to repair a change."""
    if client is CodingClient.CODEX:
        return fix_with_codex(
            workspace,
            thread_id=thread_id,
            findings=findings,
            model=model,
            effort=effort,
            timeout_seconds=timeout_seconds,
        )
    return _run_claude(
        workspace,
        prompt=_fix_prompt(findings),
        model=model,
        effort=effort,
        timeout_seconds=timeout_seconds,
        thread_id=thread_id,
    )


def _run_claude(
    workspace: Path,
    *,
    prompt: str,
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
    thread_id: CodingSessionId | None = None,
) -> CodingRun:
    _require_claude_effort(effort)
    _clear_generated_file(workspace, PR_BODY, "pull request body")
    command = (
        "claude",
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--allowedTools",
        "Read,Write,Edit,Bash,Glob,Grep,Skill",
        "--output-format",
        "json",
    )
    if thread_id is not None:
        command += ("--resume", thread_id)
    result = run_command(
        command,
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stdin=prompt,
        env={name: value for name, value in os.environ.items() if name != _API_KEY},
    )
    session_id, usage = _claude_result(result.stdout)
    if thread_id is not None and session_id != thread_id:
        raise CodingClientError("Claude resumed a different session")
    _require_pr_body(workspace)
    return CodingRun(session_id, usage, result.duration_seconds, CodingClient.CLAUDE)


def _require_claude_effort(effort: ReasoningEffort) -> None:
    if effort not in {
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    }:
        raise CodingClientError(f"Claude does not support reasoning effort {effort}")


def _claude_result(source: str) -> tuple[CodingSessionId, TokenUsage]:
    try:
        result = json.loads(source)
    except json.JSONDecodeError as exc:
        raise CodingClientError(f"Claude returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict) or result.get("type") != "result":
        raise CodingClientError("Claude returned an invalid result")
    if result.get("is_error") is not False or result.get("subtype") != "success":
        raise CodingClientError(f"Claude failed: {str(result.get('result', 'no result'))[:400]}")
    if not isinstance(session_id := result.get("session_id"), str) or not session_id:
        raise CodingClientError("Claude returned no session id")
    if not isinstance(usage := result.get("usage"), dict):
        raise CodingClientError("Claude returned no usage")
    counts = [
        usage.get(name)
        for name in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    ]
    if any(type(count) is not int or count < 0 for count in counts):
        raise CodingClientError("Claude returned invalid usage")
    input_tokens, cached, created, output_tokens = counts
    return CodingSessionId(session_id), TokenUsage(
        input_tokens + cached + created, cached, output_tokens
    )


def run_codex(
    workspace: Path,
    *,
    prompt: str,
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
) -> CodingRun:
    """Run Codex once for one issue."""
    _clear_generated_file(workspace, PR_BODY, "pull request body")
    result = run_command(
        _fresh_codex_command(workspace, model, effort),
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stdin=prompt,
    )
    thread_id, usage = _codex_events(result.stdout)
    _require_pr_body(workspace)
    return CodingRun(thread_id, usage, result.duration_seconds)


def fix_with_codex(
    workspace: Path,
    *,
    thread_id: CodingSessionId,
    findings: Findings,
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
) -> CodingRun:
    """Resume the implementing Codex thread to address review findings."""
    _clear_generated_file(workspace, PR_BODY, "pull request body")
    result = run_command(
        (
            "codex",
            "exec",
            "resume",
            "--model",
            model,
            "--config",
            f'model_reasoning_effort="{effort}"',
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            thread_id,
            "-",
        ),
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stdin=_fix_prompt(findings),
    )
    resumed_thread_id, usage = _codex_events(result.stdout)
    if resumed_thread_id != thread_id:
        raise CodingClientError("Codex resumed a different thread")
    _require_pr_body(workspace)
    return CodingRun(resumed_thread_id, usage, result.duration_seconds)


def fix_review_threads_with_codex(
    workspace: Path,
    *,
    threads: tuple[ReviewThread, ...],
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
) -> ReviewFixRun:
    """Run a fresh Codex turn for actionable pull request threads."""
    _clear_generated_file(workspace, REVIEW_REPLIES, "review replies")
    result = run_command(
        _fresh_codex_command(workspace, model, effort),
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stdin=_review_threads_prompt(threads),
    )
    thread_id, usage = _codex_events(result.stdout)
    replies = _review_replies(workspace, threads)
    return ReviewFixRun(
        CodingRun(thread_id, usage, result.duration_seconds),
        replies,
    )


def review_with_claude(
    workspace: Path,
    *,
    base_branch: str,
    ticket_body: str,
    review_skill: Skill,
    conventions: tuple[Path, ...],
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
) -> ReviewRun:
    """Run fresh standards and spec reviews in parallel."""
    _require_claude_effort(effort)
    ticket_path = workspace / TICKET_BODY
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ticket_path.write_text(ticket_body, encoding="utf-8")
    except OSError as exc:
        raise CodingClientError(f"could not write review ticket: {exc}") from exc
    prompts = (
        _standards_prompt(workspace, base_branch, review_skill, conventions),
        _spec_prompt(workspace, base_branch, ticket_path, review_skill),
    )
    subscription_env = {name: value for name, value in os.environ.items() if name != _API_KEY}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                run_command,
                (
                    "claude",
                    "-p",
                    "--model",
                    model,
                    "--effort",
                    effort,
                    "--permission-mode",
                    "plan",
                    "--permission-prompts",
                    "none",
                    "--allowedTools",
                    "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)",
                    "--no-session-persistence",
                ),
                cwd=workspace,
                timeout_seconds=timeout_seconds,
                stdin=prompt,
                env=subscription_env,
            )
            for prompt in prompts
        ]
        results = [future.result() for future in futures]
    axes = tuple(_findings(result.stdout) for result in results)
    return ReviewRun(
        Findings(
            tuple(finding for axis in axes for finding in axis.hard),
            tuple(finding for axis in axes for finding in axis.suggestions),
            "\n\n".join(axis.raw for axis in axes),
        ),
        max(result.duration_seconds for result in results),
    )


def _prompt(
    issue_number: IssueNumber,
    issue_title: IssueTitle,
    issue_url: IssueUrl,
    skills: ImplementationSkills,
) -> str:
    return (
        f"Implement GitHub issue #{issue_number}: {issue_title}\n"
        f"Ticket: {issue_url}\n\n"
        f"Follow this {skills.implement.name} skill exactly:\n\n"
        f"{skills.implement.body}\n\n"
        f"Before finishing, write the pull request body to {PR_BODY.as_posix()}. "
        f"Follow these {skills.pull_request.name} body rules, but do not push or open "
        "the pull request. The pipeline owns those operations.\n\n"
        f"{skills.pull_request.body}\n\n"
        f"{_available_skills(skills.available)}"
    )


def _available_skills(skills: tuple[Skill, ...]) -> str:
    """Where each skill lives, so an instruction that names one can be followed."""
    listed = "\n".join(f"- {skill.name}: {skill.source} ({skill.description})" for skill in skills)
    return (
        "When an instruction names another skill, read its file from this list. "
        "Paths in a skill are relative to its own directory.\n"
        f"{listed}\n"
    )


def _fix_prompt(findings: Findings) -> str:
    hard = "\n".join(f"- {finding}" for finding in findings.hard) or "- None"
    suggestions = "\n".join(f"- {finding}" for finding in findings.suggestions) or "- None"
    return (
        "Address every hard finding below. Apply the suggestions listed here once. "
        "Run the relevant tests, commit the fixes, and rewrite .scratch/pr-body.md "
        "for the current change before finishing.\n\n"
        f"Hard findings:\n{hard}\n\nSuggestions:\n{suggestions}\n"
    )


def _review_threads_prompt(threads: tuple[ReviewThread, ...]) -> str:
    rendered = "\n\n".join(_review_thread_prompt(thread) for thread in threads)
    return (
        "Address every review thread below. Fix what you agree with and commit all fixes. "
        f"For anything you do not fix, explain why. Write {REVIEW_REPLIES.as_posix()} as "
        "one JSON object mapping every thread id to "
        '{"fixed": true|false, "reply": "your reply"}. '
        "You have no GitHub access and must not push.\n\n"
        f"{rendered}\n"
    )


def _fresh_codex_command(
    workspace: Path,
    model: CodingModel,
    effort: ReasoningEffort,
) -> tuple[str, ...]:
    return (
        "codex",
        "exec",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(workspace),
        "-",
    )


def _review_thread_prompt(thread: ReviewThread) -> str:
    comment = thread.comments[-1]
    author = comment.author or "unknown"
    line = thread.line if thread.line is not None else "?"
    return f"Thread {thread.id}\n{thread.path}:{line}, {author}: {comment.body}"


def _standards_prompt(
    workspace: Path,
    base_branch: str,
    review_skill: Skill,
    conventions: tuple[Path, ...],
) -> str:
    standards = (
        "Read the repository's conventions, "
        + ", ".join(str(path) for path in conventions)
        + ", and every standard they point to"
        if conventions
        else "Find the repository's documented coding standards yourself"
    )
    return (
        "Review axis: standards.\n"
        f"Repository: {workspace}\n"
        f"Review: git diff origin/{base_branch}...HEAD\n"
        f"Commits: git log origin/{base_branch}..HEAD --oneline\n"
        f"Read {review_skill.source} and execute only its Standards reviewer brief. "
        f"Paths in it are relative to {review_skill.source.parent}. "
        f"Do not dispatch another reviewer. {standards}, plus the skill's smell baseline. "
        "Report each documented-standard breach as [hard] and each "
        "baseline smell as [suggestion]. Start every finding with its tag and path:line. "
        "Skip anything tooling enforces. Keep the answer under 400 words. If there are "
        "no findings, answer exactly: No findings\n"
        f"{_ANSWER_SHAPE}"
    )


def _spec_prompt(
    workspace: Path,
    base_branch: str,
    ticket_path: Path,
    review_skill: Skill,
) -> str:
    return (
        "Review axis: spec.\n"
        f"Repository: {workspace}\n"
        f"Review: git diff origin/{base_branch}...HEAD\n"
        f"Commits: git log origin/{base_branch}..HEAD --oneline\n"
        f"Ticket: {ticket_path}\n"
        f"Read {review_skill.source} and execute only its Spec reviewer brief. "
        "Do not dispatch another reviewer. "
        "Report missing, extra, or wrongly implemented requirements. Tag every finding "
        "[hard] and start it with path:line. Quote the ticket requirement. Keep the answer "
        "under 400 words. If there are no findings, answer exactly: No findings\n"
        f"{_ANSWER_SHAPE}"
    )


def _findings(source: str) -> Findings:
    body = source.strip()
    hard: list[str] = []
    suggestions: list[str] = []
    for line in body.splitlines():
        match = _FINDING.match(line)
        if match is None:
            continue
        target = hard if match.group(1).lower() == "hard" else suggestions
        target.append(match.group(2).strip())
    if hard or suggestions:
        return Findings(tuple(hard), tuple(suggestions), body)
    if any(_NO_FINDINGS.match(line) for line in body.splitlines()):
        return Findings((), (), body)
    excerpt = " ".join(body.split())[:_REVIEW_EXCERPT_CHARACTERS] or "empty output"
    raise CodingClientError(f"Claude review returned no tagged findings: {excerpt}")


def _clear_generated_file(workspace: Path, path: Path, name: str) -> None:
    try:
        (workspace / path).unlink(missing_ok=True)
    except OSError as exc:
        raise CodingClientError(f"could not clear {name}: {exc}") from exc


def _review_replies(
    workspace: Path,
    threads: tuple[ReviewThread, ...],
) -> tuple[ReviewReply, ...]:
    try:
        source = (workspace / REVIEW_REPLIES).read_text(encoding="utf-8")
    except OSError as exc:
        raise CodingClientError(f"Codex did not write review replies: {exc}") from exc
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise CodingClientError(f"Codex wrote invalid review replies: {exc}") from exc
    expected = {str(thread.id) for thread in threads}
    if not isinstance(value, dict) or set(value) != expected:
        raise CodingClientError("Codex review replies do not match the actionable threads")
    replies: list[ReviewReply] = []
    for thread in threads:
        reply = value[str(thread.id)]
        if (
            not isinstance(reply, dict)
            or not isinstance(fixed := reply.get("fixed"), bool)
            or not isinstance(body := reply.get("reply"), str)
            or not body.strip()
        ):
            raise CodingClientError(f"Codex returned an invalid reply for thread {thread.id}")
        replies.append(ReviewReply(thread.id, fixed, body.strip()))
    return tuple(replies)


def _require_pr_body(workspace: Path) -> None:
    path = workspace / PR_BODY
    try:
        if not path.read_text(encoding="utf-8").strip():
            raise CodingClientError("Coding client did not write a pull request body")
    except OSError as exc:
        raise CodingClientError(f"could not read pull request body: {exc}") from exc


def _codex_events(source: str) -> tuple[CodingSessionId, TokenUsage]:
    lines = [line for line in source.splitlines() if line.strip()]
    if not lines:
        raise CodingClientError("Codex returned no JSON events")
    try:
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise CodingClientError(f"Codex returned invalid JSON events: {exc}") from exc
    if not isinstance(first, dict) or not isinstance(thread_id := first.get("thread_id"), str):
        raise CodingClientError("Codex's first JSON event has no thread id")
    if not isinstance(last, dict) or not isinstance(usage := last.get("usage"), dict):
        raise CodingClientError("Codex's last JSON event has no usage")
    input_tokens = usage.get("input_tokens")
    cached_input_tokens = usage.get("cached_input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or not isinstance(cached_input_tokens, int)
        or not isinstance(output_tokens, int)
    ):
        raise CodingClientError("Codex's last JSON event has invalid usage")
    return CodingSessionId(thread_id), TokenUsage(input_tokens, cached_input_tokens, output_tokens)

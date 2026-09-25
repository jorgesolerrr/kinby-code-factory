"""Run independent reviews and fixes before the delegated pipeline opens a PR."""

from dataclasses import dataclass
from pathlib import Path

from kinby.plugins import Skill

from kinby_code_factory.clients import (
    CodingClient,
    CodingClientError,
    CodingModel,
    CodingRun,
    CodingSessionId,
    Findings,
    ReasoningEffort,
    ReviewRun,
    TokenUsage,
    fix_implementation,
    review_with_claude,
)
from kinby_code_factory.repository import BranchName
from kinby_code_factory.runs import RunReporter


@dataclass(frozen=True)
class ReviewRound:
    """One review and the fix that answered it, when another round remains."""

    number: int
    review: ReviewRun
    fix: CodingRun | None
    hard_count: int
    suggestion_count: int
    fix_usage: TokenUsage | None


@dataclass(frozen=True)
class ReviewLoop:
    """The completed review rounds and any findings left for the maintainer."""

    rounds: tuple[ReviewRound, ...]
    open_findings: Findings


def run_review_loop(
    workspace: Path,
    *,
    base_branch: BranchName,
    ticket_body: str,
    review_skill: Skill,
    conventions: tuple[Path, ...],
    thread_id: CodingSessionId,
    implementer_model: CodingModel,
    implementer_effort: ReasoningEffort,
    reviewer_model: CodingModel,
    reviewer_effort: ReasoningEffort,
    round_limit: int,
    review_timeout_seconds: float,
    fix_timeout_seconds: float,
    reporter: RunReporter,
    implementer_client: CodingClient = CodingClient.CODEX,
) -> ReviewLoop:
    """Review and fix until clean or the configured review cap is reached."""
    if round_limit < 1:
        raise CodingClientError("review round limit must be at least one")
    rounds: list[ReviewRound] = []
    for number in range(1, round_limit + 1):
        review = review_with_claude(
            workspace,
            base_branch=base_branch,
            ticket_body=ticket_body,
            review_skill=review_skill,
            conventions=conventions,
            model=reviewer_model,
            effort=reviewer_effort,
            timeout_seconds=review_timeout_seconds,
            reporter=reporter,
        )
        findings = review.findings
        should_fix = bool(findings.hard) or bool(findings.suggestions and not rounds)
        if not should_fix or number == round_limit:
            rounds.append(
                ReviewRound(
                    number=number,
                    review=review,
                    fix=None,
                    hard_count=len(findings.hard),
                    suggestion_count=len(findings.suggestions),
                    fix_usage=None,
                )
            )
            return ReviewLoop(tuple(rounds), findings)
        fix_findings = Findings(
            findings.hard,
            findings.suggestions if not rounds else (),
            findings.raw,
        )
        fix = fix_implementation(
            workspace,
            client=implementer_client,
            thread_id=thread_id,
            findings=fix_findings,
            model=implementer_model,
            effort=implementer_effort,
            timeout_seconds=fix_timeout_seconds,
            reporter=reporter,
        )
        rounds.append(
            ReviewRound(
                number=number,
                review=review,
                fix=fix,
                hard_count=len(findings.hard),
                suggestion_count=len(findings.suggestions),
                fix_usage=fix.usage,
            )
        )
    raise AssertionError("review loop exhausted without returning")

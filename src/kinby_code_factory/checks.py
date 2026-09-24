"""Run repository checks and one coding client repair attempt."""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kinby_code_factory.clients import (
    CodingClient,
    CodingClientError,
    CodingModel,
    CodingRun,
    CodingSessionId,
    Findings,
    ReasoningEffort,
    fix_implementation,
)
from kinby_code_factory.config import Checks
from kinby_code_factory.process import CommandError, run_command


@dataclass(frozen=True)
class ChecksPassed:
    passed: Literal[True] = True
    failed: None = None


@dataclass(frozen=True)
class ChecksFailed:
    failed: str
    passed: Literal[False] = False


class RepositoryCheckFailed(RuntimeError):
    """One required repository check failed."""

    def __init__(self, command: tuple[str, ...], reason: str) -> None:
        self.command = command
        super().__init__(reason)


class ChecksFixFailed(RuntimeError):
    """Repository checks still failed after one coding client fix attempt."""

    def __init__(
        self,
        checks: ChecksFailed,
        implementation: CodingRun | None,
        reason: str,
    ) -> None:
        self.checks = checks
        self.implementation = implementation
        super().__init__(reason)


def run_checks(workspace: Path, checks: Checks) -> ChecksPassed:
    """Run all repository checks in their configured order."""
    for line in checks.commands:
        command = tuple(shlex.split(line))
        try:
            run_command(command, cwd=workspace, timeout_seconds=checks.timeout_seconds)
        except CommandError as exc:
            raise RepositoryCheckFailed(command, str(exc)) from exc
    return ChecksPassed()


def run_checks_with_fix(
    workspace: Path,
    checks: Checks,
    *,
    thread_id: CodingSessionId,
    model: CodingModel,
    effort: ReasoningEffort,
    timeout_seconds: float,
    client: CodingClient = CodingClient.CODEX,
) -> tuple[ChecksPassed, CodingRun | None]:
    """Run repository checks and make one coding client fix attempt after a failure."""
    try:
        return run_checks(workspace, checks), None
    except RepositoryCheckFailed as exc:
        failed_first = ChecksFailed(failed=" ".join(exc.command))
        failure = exc
    try:
        implementation = fix_implementation(
            workspace,
            client=client,
            thread_id=thread_id,
            findings=Findings((str(failure),), (), str(failure)),
            model=model,
            effort=effort,
            timeout_seconds=timeout_seconds,
        )
    except (CommandError, CodingClientError) as fix_error:
        raise ChecksFixFailed(failed_first, None, str(fix_error)) from fix_error
    try:
        return run_checks(workspace, checks), implementation
    except RepositoryCheckFailed as retry_error:
        failed = ChecksFailed(failed=" ".join(retry_error.command))
        raise ChecksFixFailed(failed, implementation, str(retry_error)) from retry_error

"""Run factory subprocesses with a hard wall-clock limit."""

import os
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic


@dataclass(frozen=True)
class CommandResult:
    """One completed subprocess."""

    stdout: str
    stderr: str
    duration_seconds: float


class CommandError(RuntimeError):
    """A subprocess did not complete successfully."""


class CommandFailed(CommandError):
    """A subprocess exited with a non-zero status, keeping what it wrote to stdout."""

    def __init__(
        self,
        command: tuple[str, ...],
        returncode: int,
        output: str,
        *,
        stdout: str,
        duration_seconds: float,
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.output = output
        self.stdout = stdout
        self.duration_seconds = duration_seconds
        name = command[0] if command else "command"
        super().__init__(f"{name} exited with status {returncode}: {output or 'no output'}")


class CommandTimedOut(CommandError):
    """A subprocess exceeded its wall-clock limit and was killed, keeping what it had written."""

    def __init__(self, command: tuple[str, ...], timeout_seconds: float, *, stdout: str) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        name = command[0] if command else "command"
        super().__init__(f"{name} exceeded its {timeout_seconds:g}-second limit and was killed")


class CommandStartFailed(CommandError):
    """The operating system could not start a subprocess."""

    def __init__(self, command: tuple[str, ...], reason: str) -> None:
        name = command[0] if command else "command"
        super().__init__(f"could not start {name}: {reason}")


def run_command(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one command, killing its process group when its limit expires."""
    started_at = monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise CommandStartFailed(command, str(exc)) from exc
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        stdout, _ = process.communicate()
        raise CommandTimedOut(command, timeout_seconds, stdout=stdout) from None
    duration = monotonic() - started_at
    if process.returncode:
        output = stderr.strip() or stdout.strip()
        raise CommandFailed(
            command, process.returncode, output, stdout=stdout, duration_seconds=duration
        )
    return CommandResult(stdout, stderr, duration)

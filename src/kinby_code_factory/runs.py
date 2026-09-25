"""Report every coding run to kinby as a delegated run as soon as it finishes."""

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from kinby.contracts import DelegatedRun, DelegatedRunOutcome, UsageSource
from kinby.plugins import ToolContext


@dataclass(frozen=True)
class ModelTokens:
    """One model's tokens as a coding client counts them, cached input included in input."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    def minus(self, earlier: ModelTokens) -> ModelTokens:
        return ModelTokens(
            max(self.input_tokens - earlier.input_tokens, 0),
            max(self.output_tokens - earlier.output_tokens, 0),
            max(self.cache_read_tokens - earlier.cache_read_tokens, 0),
            max(self.cache_creation_tokens - earlier.cache_creation_tokens, 0),
        )

    @property
    def spent(self) -> bool:
        return any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_creation_tokens,
            )
        )


_NO_TOKENS = ModelTokens(0, 0, 0, 0)


@dataclass(frozen=True)
class ClientRun:
    """What one coding client invocation said about itself when it ended.

    ``tokens`` is the session's running total per model, as the client reports it;
    None when the client ended before reporting any. ``model`` is the model the run asked
    for, named when no model reports tokens. A limited run carries ``resets_at``.
    """

    usage_source: UsageSource
    client: str
    model: str
    session: str | None
    tokens: Mapping[str, ModelTokens] | None
    duration_seconds: float
    client_turns: int
    outcome: DelegatedRunOutcome
    resets_at: datetime | None = None


class RunReporter:
    """Reports each coding run with its own tokens, not its session's running total."""

    def __init__(self, report: Callable[[DelegatedRun], None]) -> None:
        self._report = report
        self._readings: dict[str, Mapping[str, ModelTokens]] = {}
        self._lock = threading.Lock()

    def report(self, run: ClientRun) -> None:
        """Report *run* now; safe to call from the threads that run clients."""
        with self._lock:
            earlier = self._readings.get(run.session, {}) if run.session else {}
            tokens = {
                model: total.minus(earlier.get(model, _NO_TOKENS))
                for model, total in (run.tokens or {}).items()
            }
            if run.session and run.tokens is not None:
                self._readings[run.session] = run.tokens
        spent = {model: used for model, used in tokens.items() if used.spent}
        self._report(
            DelegatedRun(
                usage_source=run.usage_source,
                client=run.client,
                models=list(spent) or [run.model],
                input_tokens=sum(used.input_tokens for used in spent.values()),
                output_tokens=sum(used.output_tokens for used in spent.values()),
                cache_read_tokens=sum(used.cache_read_tokens for used in spent.values()),
                cache_creation_tokens=sum(used.cache_creation_tokens for used in spent.values()),
                duration_ms=round(run.duration_seconds * 1000),
                client_turns=run.client_turns,
                outcome=run.outcome,
                resets_at=run.resets_at,
            )
        )


def tool_run_reporter(context: ToolContext) -> RunReporter:
    """Report into the turn that invoked the tool, from the tool's worker threads.

    Call it on the tool's event loop; each report waits until kinby has recorded the run.
    """
    loop = asyncio.get_running_loop()
    return RunReporter(
        lambda run: asyncio.run_coroutine_threadsafe(context.report_run(run), loop).result()
    )

"""Every coding run reaches kinby as a delegated run in the turn that started it."""

import json
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from kinby.cli import main
from kinby.instance import load_instance

from tests.test_factory import (
    _coder_copy,
    _delegated_runs,
    _fake_clients,
    _mapping,
    _report,
    _RoutineModel,
    _use_routine_model,
    configure,
)

_CLAUDE_LIMIT_RESET = 1_790_000_000


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: str,
    review_round_limit: int | None = None,
) -> tuple[Path, Path]:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    instance = _coder_copy(tmp_path, client=client, review_round_limit=review_round_limit)
    _use_routine_model(monkeypatch, load_instance(instance), _RoutineModel())
    _fake_clients(tmp_path, monkeypatch)
    return instance, tmp_path / "canned"


def _run(instance: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert main(["routine", "run", "implement-ready-issue", "--instance", str(instance)]) == 0
    return _report(capsys.readouterr().out)


def _tokens(run: dict[str, str]) -> tuple[int, int, int, int]:
    return (
        int(run["input"]),
        int(run["output"]),
        int(run["cache_read"]),
        int(run["cache_creation"]),
    )


def _claude_result(canned: Path, name: str, **changes: object) -> None:
    result = json.loads((canned / "claude-result.json").read_text(encoding="utf-8"))
    result.update(changes)
    (canned / name).write_text(json.dumps(result), encoding="utf-8")


def test_codex_implementation_review_and_fix_are_each_reported_with_their_own_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, canned = _prepare(tmp_path, monkeypatch, client="codex", review_round_limit=3)
    (canned / "review-standards-0.md").write_text(
        "[hard] src/example.py:12 violates CODING-STANDARD.md\n", encoding="utf-8"
    )
    (canned / "review-standards-1.md").write_text("No findings", encoding="utf-8")
    (canned / "codex-resume-events.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-184"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":130,'
        '"cache_write_input_tokens":10,"output_tokens":60,"reasoning_output_tokens":20}}',
        encoding="utf-8",
    )

    report = _run(instance, capsys)

    assert report["outcome"] == "opened"
    # The pipeline report keeps the client's thread totals, as before.
    assert _mapping(report["implementation"])["usage"] == {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 35,
    }
    runs = _delegated_runs(instance, capsys)
    codex = [run for run in runs if run["client"] == "codex"]
    reviews = [run for run in runs if run["client"] == "claude-code"]
    assert len(codex) == 2
    assert len(reviews) == 4
    for run in codex:
        assert run["source"] == "chatgpt-subscription"
        assert run["models"] == "gpt-5.6-sol"
        assert run["outcome"] == "completed"
        assert run["client_turns"] == "1"
        assert "resets_at" not in run
    # Cached input maps to cache read, cache writes to cache creation, reasoning stays in output.
    assert _tokens(codex[0]) == (120, 35, 80, 6)
    # A resumed thread reports its running total, so the fix reports only the difference.
    assert _tokens(codex[1]) == (80, 25, 50, 4)
    for run in reviews:
        assert run["source"] == "claude-subscription"
        assert run["models"] == "claude-fable-5-1"
        assert run["outcome"] == "completed"
        assert run["client_turns"] == "5"
        assert _tokens(run) == (80, 30, 60, 16)


def test_claude_reports_model_usage_including_subagents_and_the_resumed_difference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, canned = _prepare(tmp_path, monkeypatch, client="claude")
    monkeypatch.setenv("FAKE_COMMAND_FAIL_ONCE", "run ruff check .")
    _claude_result(
        canned,
        "claude-resume-result.json",
        num_turns=3,
        modelUsage={
            "claude-opus-5-5": {
                "inputTokens": 3,
                "outputTokens": 50,
                "cacheReadInputTokens": 180,
                "cacheCreationInputTokens": 40,
            },
            "claude-haiku-4-5": {
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadInputTokens": 20,
                "cacheCreationInputTokens": 0,
            },
        },
    )

    report = _run(instance, capsys)

    assert report["outcome"] == "opened"
    assert _mapping(report["implementation"])["usage"] == {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "output_tokens": 35,
    }
    implementation, check_fix = _delegated_runs(instance, capsys)
    assert implementation["source"] == "claude-subscription"
    assert implementation["client"] == "claude-code"
    assert implementation["models"] == "claude-opus-5-5,claude-haiku-4-5"
    assert implementation["outcome"] == "completed"
    assert implementation["client_turns"] == "7"
    assert _tokens(implementation) == (150, 40, 100, 38)
    # The resumed session's model usage carries the earlier run; only the subagent-free
    # main model did new work.
    assert check_fix["models"] == "claude-opus-5-5"
    assert check_fix["client_turns"] == "3"
    assert _tokens(check_fix) == (103, 15, 100, 2)


def test_claude_stopped_by_its_plan_limit_is_reported_limited_with_its_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, canned = _prepare(tmp_path, monkeypatch, client="claude")
    (canned / "claude-events.jsonl").write_text(
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": _CLAUDE_LIMIT_RESET,
                    "rateLimitType": "five_hour",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _claude_result(
        canned,
        "claude-result.json",
        is_error=True,
        result="You've hit your limit",
        num_turns=1,
        modelUsage={},
    )

    report = _run(instance, capsys)

    assert report["outcome"] == "failed"
    assert "You've hit your limit" in str(report["failure_reason"])
    (run,) = _delegated_runs(instance, capsys)
    assert run["outcome"] == "limited"
    assert datetime.fromisoformat(run["resets_at"]) == datetime.fromtimestamp(
        _CLAUDE_LIMIT_RESET, UTC
    )
    assert _tokens(run) == (0, 0, 0, 0)


def test_a_claude_run_that_crashes_is_reported_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, _ = _prepare(tmp_path, monkeypatch, client="claude")
    monkeypatch.setenv("FAKE_CLAUDE_IMPLEMENT_EXIT", "7")

    report = _run(instance, capsys)

    assert report["outcome"] == "failed"
    (run,) = _delegated_runs(instance, capsys)
    assert run["client"] == "claude-code"
    assert run["outcome"] == "failed"
    assert "resets_at" not in run


def _assistant(message: str, model: str, usage: tuple[int, int, int, int]) -> str:
    uncached, read, created, output = usage
    return json.dumps(
        {
            "type": "assistant",
            "session_id": "claude-session-184",
            "message": {
                "id": message,
                "model": model,
                "usage": {
                    "input_tokens": uncached,
                    "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": created,
                    "output_tokens": output,
                },
            },
        }
    )


def test_a_claude_run_killed_at_its_limit_reports_the_tokens_it_streamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, canned = _prepare(tmp_path, monkeypatch, client="claude")
    configure(instance, implementation={"timeout_seconds": 1})
    monkeypatch.setenv("FAKE_CLAUDE_IMPLEMENT_SLEEP", "10")
    (canned / "claude-events.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"type": "system", "subtype": "init"}),
                # One message streams one event per content block, each with the same usage.
                _assistant("msg-1", "claude-opus-5-5", (2, 80, 38, 20)),
                _assistant("msg-1", "claude-opus-5-5", (2, 80, 38, 20)),
                _assistant("msg-2", "claude-opus-5-5", (1, 118, 5, 12)),
                # A subagent's call on another model.
                _assistant("msg-3", "claude-haiku-4-5", (10, 0, 0, 5)),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = _run(instance, capsys)

    assert report["outcome"] == "failed"
    assert "claude exceeded its 1-second limit" in str(report["failure_reason"])
    (run,) = _delegated_runs(instance, capsys)
    assert run["outcome"] == "failed"
    assert run["models"] == "claude-opus-5-5,claude-haiku-4-5"
    assert _tokens(run) == (254, 37, 198, 43)
    assert int(run["duration_ms"]) >= 1000


@pytest.mark.parametrize(
    ("retry", "resets_at"),
    [
        ("or try again at Sep 26th, 2026 3:05 PM.", datetime(2026, 9, 26, 15, 5).astimezone()),
        ("or try again at 3:05 PM.", datetime.combine(date.today(), time(15, 5)).astimezone()),
        ("or try again later.", None),
    ],
)
def test_codex_stopped_by_its_usage_limit_is_reported_limited_when_it_names_a_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    retry: str,
    resets_at: datetime | None,
) -> None:
    instance, canned = _prepare(tmp_path, monkeypatch, client="codex")
    message = (
        "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
        f"visit https://chatgpt.com/codex/settings/usage to purchase more credits {retry}"
    )
    (canned / "codex-events.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-184"}\n'
        '{"type":"turn.started"}\n'
        + json.dumps({"type": "error", "message": message})
        + "\n"
        + json.dumps({"type": "turn.failed", "error": {"message": message}}),
        encoding="utf-8",
    )

    report = _run(instance, capsys)

    assert report["outcome"] == "failed"
    (run,) = _delegated_runs(instance, capsys)
    assert run["client"] == "codex"
    assert run["client_turns"] == "1"
    if resets_at is None:
        # Kinby needs a reset time for a limited run; without one the run still counts.
        assert run["outcome"] == "failed"
        assert "resets_at" not in run
    else:
        assert run["outcome"] == "limited"
        assert datetime.fromisoformat(run["resets_at"]) == resets_at

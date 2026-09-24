"""The coder routine scans reviews on agent pull requests."""

import json
import os
from pathlib import Path

import pytest
from kinby.cli import main
from kinby.instance import load_instance

from kinby_code_factory.babysit import (
    actionable_threads,
    is_merge_ready,
    is_waiting,
    signal_pull_request_number,
    signal_warrants_scan,
)
from kinby_code_factory.repository import (
    AgentPullRequest,
    BabysitPullRequest,
    BranchName,
    CheckRun,
    CheckRunStatus,
    CommitSha,
    GitHubLogin,
    GitHubRepository,
    PullRequestNumber,
    PullRequestReview,
    PullRequestUrl,
    RepositoryMetadata,
    RepositoryName,
    ReviewComment,
    ReviewThread,
    ReviewThreadId,
    select_agent_pull_requests,
)
from tests.test_factory import (
    _arguments,
    _coder_copy,
    _mapping,
    _records,
    _RoutineModel,
    _text,
    _use_routine_model,
    _write_executable,
    _write_fake_github,
    configure,
)


def _agent_pull_request(branch: str = "agent/225-babysit") -> AgentPullRequest:
    return AgentPullRequest(
        number=PullRequestNumber(24),
        url=PullRequestUrl("https://example.test/pull/24"),
        branch=BranchName(branch),
        head=CommitSha("head-24"),
        body="Closes #225\n",
        stack=None,
        author=GitHubLogin("kinby-coder"),
        labels=(),
    )


def _babysit_pull_request(
    *,
    threads: tuple[ReviewThread, ...] = (),
    checks: tuple[CheckRun, ...] = (),
    reviews: tuple[PullRequestReview, ...] = (),
) -> BabysitPullRequest:
    return BabysitPullRequest(
        listed=_agent_pull_request(),
        checks=checks,
        threads=threads,
        reviews=reviews,
        round_count=0,
    )


def test_thread_classification_is_pure_and_ignores_empty_and_resolved_threads() -> None:
    coder = GitHubLogin("kinby-coder")
    empty = ReviewThread(ReviewThreadId("empty"), False, "src/example.py", None, ())
    actionable = ReviewThread(
        ReviewThreadId("actionable"),
        False,
        "src/example.py",
        12,
        (ReviewComment(GitHubLogin("reviewer"), "please fix", CommitSha("head-24")),),
    )
    answered = ReviewThread(
        ReviewThreadId("answered"),
        False,
        "src/example.py",
        12,
        (ReviewComment(coder, "fixed", CommitSha("head-24")),),
    )
    resolved = ReviewThread(
        ReviewThreadId("resolved"),
        True,
        "src/example.py",
        12,
        (ReviewComment(GitHubLogin("reviewer"), "please fix", CommitSha("head-24")),),
    )

    assert actionable_threads((empty, actionable, answered, resolved), coder) == (actionable,)


def test_waiting_and_merge_ready_are_pure_and_use_only_the_current_head() -> None:
    coder = GitHubLogin("kinby-coder")
    completed = CheckRun(CheckRunStatus.COMPLETED)
    running = CheckRun(CheckRunStatus.IN_PROGRESS)
    stale_review = PullRequestReview(CommitSha("old-head"))
    current_review = PullRequestReview(CommitSha("head-24"))

    assert not is_waiting((completed,))
    assert is_waiting((completed, running))
    assert not is_merge_ready(
        _babysit_pull_request(checks=(completed,), reviews=(stale_review,)), coder
    )
    assert is_merge_ready(
        _babysit_pull_request(checks=(completed,), reviews=(current_review,)), coder
    )
    assert not is_merge_ready(
        _babysit_pull_request(checks=(running,), reviews=(current_review,)), coder
    )


def test_agent_pr_and_signal_selection_are_pure() -> None:
    human = _agent_pull_request("feature/human")
    agent = _agent_pull_request()
    abbreviated_comment: dict[str, object] = {
        "body": {"issue": {"number": 24, "pull_request": {"url": "https://example.test/pulls/24"}}}
    }

    assert select_agent_pull_requests((human, agent)) == (agent,)
    assert signal_warrants_scan({})
    assert signal_warrants_scan({"body": {"pull_request": {"head": {"ref": "agent/225-babysit"}}}})
    assert not signal_warrants_scan({"body": {"pull_request": {"head": {"ref": "feature/human"}}}})
    assert not signal_warrants_scan({"body": {"issue": {"number": 225}}})
    assert not signal_warrants_scan(abbreviated_comment)
    assert signal_warrants_scan(abbreviated_comment, BranchName("agent/225-babysit"))
    assert not signal_warrants_scan(abbreviated_comment, BranchName("feature/human"))
    assert signal_pull_request_number(abbreviated_comment) == PullRequestNumber(24)


def _fake_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    canned = tmp_path / "canned"
    canned.mkdir()
    log = tmp_path / "commands.jsonl"
    _write_fake_github(binaries / "gh")
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GITHUB_LOG", str(log))
    monkeypatch.setenv("FAKE_GITHUB_RESPONSES", str(canned))
    (canned / "repository.txt").write_text(
        '{"name":"kinby","owner":{"login":"jorgesolerrr"},"defaultBranchRef":{"name":"main"}}\n',
        encoding="utf-8",
    )
    (canned / "user.txt").write_text("kinby-coder\n", encoding="utf-8")
    (canned / "signal-head.txt").write_text("agent/225-babysit\n", encoding="utf-8")
    (canned / "pull-requests.json").write_text("[]", encoding="utf-8")
    return canned, log


def _write_scan(
    canned: Path,
    *,
    threads: list[dict[str, object]],
    checks: list[dict[str, object]],
    reviews: list[dict[str, object]],
    comments: list[dict[str, object]],
    labels: list[dict[str, str]] | None = None,
    requested_reviewers: list[dict[str, str]] | None = None,
    author: str = "kinby-coder",
) -> None:
    canned.joinpath("pull-requests.json").write_text(
        json.dumps(
            [
                _pull_request(
                    number=24,
                    issue=225,
                    author=author,
                    labels=labels,
                    requested_reviewers=requested_reviewers,
                )
            ]
        ),
        encoding="utf-8",
    )
    _write_review_state(
        canned,
        number=24,
        head="head-24",
        threads=threads,
        checks=checks,
        reviews=reviews,
        comments=comments,
    )


def _pull_request(
    *,
    number: int,
    issue: int,
    author: str = "kinby-coder",
    labels: list[dict[str, str]] | None = None,
    requested_reviewers: list[dict[str, str]] | None = None,
    body: str | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://example.test/pull/{number}",
        "head": {"ref": f"agent/{issue}-babysit", "sha": f"head-{number}"},
        "body": body if body is not None else f"Closes #{issue}\n",
        "user": {"login": author},
        "labels": labels or [],
        "requested_reviewers": requested_reviewers or [],
    }


def _write_review_state(
    canned: Path,
    *,
    number: int,
    head: str,
    threads: list[dict[str, object]],
    checks: list[dict[str, object]],
    reviews: list[dict[str, object]],
    comments: list[dict[str, object]],
) -> None:
    canned.joinpath(f"check-runs-{head}.json").write_text(
        json.dumps({"check_runs": checks}), encoding="utf-8"
    )
    canned.joinpath(f"review-threads-{number}.json").write_text(
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": threads, "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    canned.joinpath(f"reviews-{number}.json").write_text(json.dumps(reviews), encoding="utf-8")
    canned.joinpath(f"issue-comments-{number}.json").write_text(
        json.dumps(comments), encoding="utf-8"
    )


def _review_thread(
    *authors: str,
    resolved: bool = False,
    thread_id: str = "PRRT_thread",
    head: str = "head-24",
    path: str = "src/example.py",
    line: int | None = 12,
    body: str = "review comment",
) -> dict[str, object]:
    trusted = {"reviewer": "greptile-apps", "maintainer": "jorgesolerrr"}
    return {
        "id": thread_id,
        "isResolved": resolved,
        "path": path,
        "line": line,
        "comments": {
            "nodes": [
                {
                    "author": {"login": trusted.get(author, author)},
                    "body": body,
                    "commit": {"oid": head},
                }
                for author in authors
            ]
        },
    }


def _fake_fix_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    canned, log = _fake_github(tmp_path, monkeypatch)
    binaries = tmp_path / "bin"
    common = """import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
record = {"command": Path(sys.argv[0]).name, "arguments": arguments, "cwd": os.getcwd()}
"""
    _write_executable(
        binaries / "codex",
        common
        + """import time

record["stdin"] = sys.stdin.read()
with Path(os.environ["FAKE_GITHUB_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
sleep_variable = "FAKE_CODEX_RESUME_SLEEP" if "resume" in arguments else "FAKE_CODEX_SLEEP"
time.sleep(float(os.environ.get(sleep_variable, "0")))
if exit_code := int(os.environ.get("FAKE_CODEX_EXIT", "0")):
    print("Codex exploded", file=sys.stderr)
    raise SystemExit(exit_code)
if os.environ.get("FAKE_CODEX_WRITE_REPLIES", "1") == "1":
    scratch = Path.cwd() / ".scratch"
    scratch.mkdir(exist_ok=True)
    (scratch / "review-replies.json").write_text(
        (Path(os.environ["FAKE_GITHUB_RESPONSES"]) / "review-replies.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
if "resume" in arguments:
    (Path.cwd() / ".scratch" / "pr-body.md").write_text("Checks fixed.\\n", encoding="utf-8")
(Path.cwd() / "fixed.py").write_text("fixed = True\\n", encoding="utf-8")
print('{"type":"thread.started","thread_id":"thread-fix-226"}')
print('{"type":"turn.completed","usage":{"input_tokens":90,'
      '"cached_input_tokens":60,"output_tokens":25}}')
""",
    )
    _write_executable(
        binaries / "git",
        common
        + """with Path(os.environ["FAKE_GITHUB_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
joined = " ".join(arguments)
if joined.startswith(os.environ.get("FAKE_GIT_FAIL", "no failure configured")):
    print("git exploded", file=sys.stderr)
    raise SystemExit(7)
if arguments[:2] == ["branch", "--remotes"]:
    print("  origin/agent/225-babysit")
elif arguments[:2] == ["rev-parse", "HEAD"]:
    print("fix-commit-sha" if (Path.cwd() / "fixed.py").exists()
          and not os.environ.get("FAKE_UNCHANGED_HEAD") else "starting-sha")
elif arguments[:2] == ["status", "--porcelain"]:
    print(os.environ.get("FAKE_DIRTY_STATUS", ""), end="")
elif arguments[:2] == ["reset", "--hard"] or arguments[:2] == ["clean", "-fd"]:
    (Path.cwd() / "fixed.py").unlink(missing_ok=True)
""",
    )
    _write_executable(
        binaries / "uv",
        common
        + """with Path(os.environ["FAKE_GITHUB_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
joined = " ".join(arguments)
fail_once = os.environ.get("FAKE_UV_FAIL_ONCE", "no one-shot failure configured")
records = [
    json.loads(line)
    for line in Path(os.environ["FAKE_GITHUB_LOG"]).read_text(encoding="utf-8").splitlines()
]
matching_calls = sum(
    item["command"] == "uv" and " ".join(item["arguments"]).startswith(fail_once)
    for item in records
)
if joined.startswith(os.environ.get("FAKE_UV_FAIL", "no failure configured")) or (
    joined.startswith(fail_once) and matching_calls == 1
):
    print("uv exploded", file=sys.stderr)
    raise SystemExit(7)
""",
    )
    canned.joinpath("review-replies.json").write_text(
        json.dumps(
            {
                "PRRT_fix": {"fixed": True, "reply": "Applied the rename."},
                "PRRT_answer": {
                    "fixed": False,
                    "reply": "Keeping this because the contract requires it.",
                },
            }
        ),
        encoding="utf-8",
    )
    return canned, log


def _run_babysit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delivery: dict[str, object] | None,
    expected_exit_code: int = 0,
    babysitting: dict[str, object] | None = None,
) -> str:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GH_TOKEN", "github-token")
    instance_path = _coder_copy(tmp_path)
    configure(instance_path, babysitting={"enabled": True, **(babysitting or {})})
    instance = load_instance(instance_path)
    _use_routine_model(monkeypatch, instance, _RoutineModel())
    arguments = [
        "routine",
        "run",
        "babysit-pull-request",
        "--instance",
        str(instance_path),
    ]
    if delivery is not None:
        payload = tmp_path / "delivery.json"
        payload.write_text(json.dumps(delivery), encoding="utf-8")
        arguments[3:3] = ["--payload", str(payload)]

    assert main(arguments) == expected_exit_code
    return capsys.readouterr().out


@pytest.mark.parametrize(
    "delivery",
    [
        {"action": "submitted", "pull_request": {"head": {"ref": "feature/human"}}},
        {"action": "created", "issue": {"number": 225}, "comment": {"body": "hello"}},
    ],
)
def test_irrelevant_delivery_returns_no_work_without_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delivery: dict[str, object],
) -> None:
    _, log = _fake_github(tmp_path, monkeypatch)

    output = _run_babysit(tmp_path, monkeypatch, capsys, delivery)

    assert "[tool.result] babysit_pull_request (ok): None" in output
    assert not log.exists()


def test_issue_comment_on_an_agent_pull_request_triggers_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, log = _fake_github(tmp_path, monkeypatch)

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {
            "action": "created",
            "issue": {"number": 24, "pull_request": {"url": "https://api.example.test/pulls/24"}},
            "comment": {"body": "please revisit this"},
        },
    )

    assert "[tool.result] babysit_pull_request (ok): None" in output
    calls = [_arguments(record) for record in _records(log)]
    assert calls[0][:3] == ["pr", "view", "24"]
    assert any(
        arguments[0] == "api" and any(item.endswith("/pulls") for item in arguments)
        for arguments in calls
    )


def test_schedule_wake_always_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, log = _fake_github(tmp_path, monkeypatch)

    output = _run_babysit(tmp_path, monkeypatch, capsys, None)

    assert "[tool.result] babysit_pull_request (ok): None" in output
    assert any(
        any(item.endswith("/pulls") for item in _arguments(record)) for record in _records(log)
    )


def test_scan_reads_review_thread_location_and_comment_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canned, _ = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_review")],
        checks=[{"status": "completed"}],
        reviews=[],
        comments=[],
    )

    repository = GitHubRepository(tmp_path, timeout_seconds=60, environment=dict(os.environ))
    (pull_request,) = repository.babysit_pull_requests(
        GitHubLogin("kinby-coder"),
        RepositoryMetadata(
            GitHubLogin("jorgesolerrr"),
            RepositoryName("kinby"),
            BranchName("main"),
        ),
    )
    (thread,) = pull_request.threads
    (comment,) = thread.comments

    assert thread.id == ReviewThreadId("PRRT_review")
    assert thread.path == "src/example.py"
    assert thread.line == 12
    assert comment.body == "review comment"


def test_answered_review_labels_the_pull_request_merge_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[
            _review_thread("reviewer", "kinby-coder"),
            _review_thread("reviewer", resolved=True),
        ],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
        author="implementing-agent",
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    assert json.loads(result_line.partition(": ")[2]) == [
        {
            "pull_request_number": 24,
            "pull_request_url": "https://example.test/pull/24",
            "issue_number": 225,
            "outcome": "merge_ready",
            "round_number": 0,
            "threads_fixed": 0,
            "threads_answered": 0,
            "codex": None,
            "checks": None,
            "warnings": [],
            "failure_reason": None,
        }
    ]
    calls = [_arguments(record) for record in _records(log)]
    assert ["pr", "edit", "24", "--add-label", "merge-ready"] in calls
    assert ["pr", "edit", "24", "--add-reviewer", "jorgesolerrr"] in calls
    assert calls.index(["pr", "edit", "24", "--add-reviewer", "jorgesolerrr"]) < calls.index(
        ["pr", "edit", "24", "--add-label", "merge-ready"]
    )
    graphql = next(arguments for arguments in calls if arguments[:2] == ["api", "graphql"])
    query = next(item.removeprefix("query=") for item in graphql if item.startswith("query="))
    assert "query BabysitReviewThreads" in query


def test_existing_merge_ready_label_does_not_hide_a_missing_review_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", "kinby-coder")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
        labels=[{"name": "merge-ready"}],
        author="implementing-agent",
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert '"outcome":"merge_ready"' in output
    calls = [_arguments(record) for record in _records(log)]
    assert ["pr", "edit", "24", "--add-reviewer", "jorgesolerrr"] in calls
    assert ["pr", "edit", "24", "--add-label", "merge-ready"] not in calls


def test_existing_review_request_and_merge_ready_label_need_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", "kinby-coder")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
        labels=[{"name": "merge-ready"}],
        requested_reviewers=[{"login": "jorgesolerrr"}],
        author="implementing-agent",
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert "[tool.result] babysit_pull_request (ok): None" in output
    assert not any(_arguments(record)[:2] == ["pr", "edit"] for record in _records(log))


def test_completed_maintainer_review_is_not_requested_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("jorgesolerrr", "kinby-coder")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24", "user": {"login": "jorgesolerrr"}}],
        comments=[],
        labels=[{"name": "merge-ready"}],
        author="implementing-agent",
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert "[tool.result] babysit_pull_request (ok): None" in output
    assert not any(_arguments(record)[:2] == ["pr", "edit"] for record in _records(log))


def test_maintainer_review_on_an_old_head_does_not_suppress_a_new_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("jorgesolerrr", "kinby-coder")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "old-head", "user": {"login": "jorgesolerrr"}}],
        comments=[],
        labels=[{"name": "merge-ready"}],
        author="implementing-agent",
    )

    _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert ["pr", "edit", "24", "--add-reviewer", "jorgesolerrr"] in [
        _arguments(record) for record in _records(log)
    ]


@pytest.mark.parametrize("result", ["started.", "fixed 1, answered 0."])
def test_round_limit_labels_the_pull_request_ready_for_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: str,
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[
            {
                "user": {"login": "kinby-coder"},
                "body": f"Babysit round {number} of 3: {result}",
            }
            for number in range(1, 4)
        ],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "created", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "round_limit"
    assert report["round_number"] == 3
    assert report["issue_number"] == 225
    assert ["pr", "edit", "24", "--add-label", "ready-for-human"] in [
        _arguments(record) for record in _records(log)
    ]


def test_one_scan_labels_every_completed_pull_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    canned.joinpath("pull-requests.json").write_text(
        json.dumps(
            [
                _pull_request(number=24, issue=225, author="implementing-agent"),
                _pull_request(number=25, issue=226),
            ]
        ),
        encoding="utf-8",
    )
    _write_review_state(
        canned,
        number=24,
        head="head-24",
        threads=[_review_thread("reviewer", "kinby-coder")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    _write_review_state(
        canned,
        number=25,
        head="head-25",
        threads=[_review_thread("reviewer", head="head-25")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-25"}],
        comments=[
            {
                "user": {"login": "kinby-coder"},
                "body": f"Babysit round {number} of 3: fixed 1, answered 0.",
            }
            for number in range(1, 4)
        ],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    calls = [_arguments(record) for record in _records(log)]
    assert ["pr", "edit", "24", "--add-label", "merge-ready"] in calls
    assert ["pr", "edit", "24", "--add-reviewer", "jorgesolerrr"] in calls
    assert ["pr", "edit", "25", "--add-label", "ready-for-human"] in calls
    assert sum(arguments[:2] == ["repo", "view"] for arguments in calls) == 1
    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    reports = json.loads(result_line.partition(": ")[2])
    assert [report["pull_request_number"] for report in reports] == [24, 25]
    assert [report["outcome"] for report in reports] == ["merge_ready", "round_limit"]


def test_scan_removes_merge_ready_when_the_pull_request_is_no_longer_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer")],
        checks=[{"status": "in_progress"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
        labels=[{"name": "merge-ready"}],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert "[tool.result] babysit_pull_request (ok): None" in output
    assert ["pr", "edit", "24", "--remove-label", "merge-ready"] in [
        _arguments(record) for record in _records(log)
    ]


def test_stale_label_is_not_removed_before_the_closing_issue_is_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    canned.joinpath("pull-requests.json").write_text(
        json.dumps(
            [
                _pull_request(
                    number=24,
                    issue=225,
                    labels=[{"name": "merge-ready"}],
                    body="No closing reference",
                )
            ]
        ),
        encoding="utf-8",
    )
    _write_review_state(
        canned,
        number=24,
        head="head-24",
        threads=[_review_thread("reviewer")],
        checks=[{"status": "in_progress"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
        expected_exit_code=1,
    )

    assert "agent pull request body does not close an issue" in output
    assert ["pr", "edit", "24", "--remove-label", "merge-ready"] not in [
        _arguments(record) for record in _records(log)
    ]


def test_scan_replaces_ready_for_human_when_the_pull_request_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", "kinby-coder")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
        labels=[{"name": "ready-for-human"}],
    )

    _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    calls = [_arguments(record) for record in _records(log)]
    assert ["pr", "edit", "24", "--remove-label", "ready-for-human"] in calls
    assert ["pr", "edit", "24", "--add-label", "merge-ready"] in calls


def test_actionable_review_waits_for_a_running_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer")],
        checks=[{"status": "in_progress"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert "[tool.result] babysit_pull_request (ok): None" in output
    assert not any(_arguments(record)[:2] == ["pr", "edit"] for record in _records(log))


@pytest.mark.parametrize("status", ["waiting", "requested", "pending"])
def test_non_running_github_check_status_does_not_block_merge_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    canned, log = _fake_github(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", "kinby-coder")],
        checks=[{"status": status}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    assert '"outcome":"merge_ready"' in output
    assert ["pr", "edit", "24", "--add-label", "merge-ready"] in [
        _arguments(record) for record in _records(log)
    ]


def test_actionable_threads_run_one_fix_round_and_leave_a_clean_default_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[
            _review_thread(
                "reviewer",
                thread_id="PRRT_fix",
                path="src/first.py",
                line=8,
                body="Rename this value.",
            ),
            _review_thread(
                "kinby-coder",
                "maintainer",
                thread_id="PRRT_answer",
                path="src/second.py",
                line=21,
                body="Remove this fallback.",
            ),
        ],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
        labels=[{"name": "merge-ready"}],
    )
    canned.joinpath("pull-requests.json").write_text(
        json.dumps(
            [
                _pull_request(
                    number=24,
                    issue=225,
                    labels=[{"name": "merge-ready"}],
                ),
                _pull_request(number=25, issue=226),
            ]
        ),
        encoding="utf-8",
    )
    _write_review_state(
        canned,
        number=25,
        head="head-25",
        threads=[
            _review_thread(
                "reviewer",
                thread_id="PRRT_later",
                head="head-25",
            )
        ],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-25"}],
        comments=[],
    )

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    assert json.loads(result_line.partition(": ")[2]) == [
        {
            "pull_request_number": 24,
            "pull_request_url": "https://example.test/pull/24",
            "issue_number": 225,
            "outcome": "fixed",
            "round_number": 1,
            "threads_fixed": 1,
            "threads_answered": 1,
            "codex": {
                "thread_id": "thread-fix-226",
                "client": "codex",
                "usage": {
                    "input_tokens": 90,
                    "cached_input_tokens": 60,
                    "output_tokens": 25,
                },
                "duration_seconds": pytest.approx(0, abs=1),
            },
            "checks": {"passed": True, "failed": None},
            "warnings": [],
            "failure_reason": None,
        }
    ]
    records = _records(log)
    codex = next(record for record in records if record["command"] == "codex")
    codex_arguments = _arguments(codex)
    assert codex_arguments[:2] == ["exec", "--model"]
    assert "resume" not in codex_arguments
    assert "gpt-5.6-sol" in codex_arguments
    assert 'model_reasoning_effort="high"' in codex_arguments
    prompt = _text(codex, "stdin")
    assert "src/first.py:8, greptile-apps: Rename this value." in prompt
    assert "src/second.py:21, jorgesolerrr: Remove this fallback." in prompt
    assert "no GitHub access" in prompt
    assert "must not push" in prompt

    calls = [_arguments(record) for record in records]
    assert ["push"] in calls
    git_calls = [_arguments(record) for record in records if record["command"] == "git"]
    assert ["fetch", "origin"] in git_calls
    assert [
        "switch",
        "--discard-changes",
        "-C",
        "agent/225-babysit",
        "origin/agent/225-babysit",
    ] in git_calls
    assert not any("rebase" in arguments for arguments in git_calls)
    assert not any("--force" in arguments or "-f" in arguments for arguments in git_calls)
    assert not any("agent/226-babysit" in arguments for arguments in git_calls)
    assert ["pr", "edit", "24", "--remove-label", "merge-ready"] in calls
    reply_calls = [
        arguments
        for arguments in calls
        if arguments[:2] == ["api", "graphql"]
        and any("mutation ReplyToReviewThread" in item for item in arguments)
    ]
    assert len(reply_calls) == 2
    assert any("body=fix-commit-sha: Applied the rename." in arguments for arguments in reply_calls)
    assert any(
        "body=Keeping this because the contract requires it." in arguments
        for arguments in reply_calls
    )
    resolve_calls = [
        arguments
        for arguments in calls
        if arguments[:2] == ["api", "graphql"]
        and any("mutation ResolveReviewThread" in item for item in arguments)
    ]
    assert len(resolve_calls) == 1
    assert any("threadId=PRRT_fix" in item for item in resolve_calls[0])
    assert [
        "pr",
        "comment",
        "24",
        "--body",
        "Babysit result for round 1: fixed 1, answered 1.",
    ] in calls
    workspace = tmp_path / "coder" / "workspace"
    assert not workspace.joinpath("fixed.py").exists()
    assert not workspace.joinpath(".scratch/review-replies.json").exists()
    assert calls[-4:] == [
        ["reset", "--hard"],
        ["clean", "-fd"],
        ["switch", "--discard-changes", "-C", "main", "origin/main"],
        ["branch", "-D", "agent/225-babysit"],
    ]


def test_checks_failure_after_one_fix_stops_the_round_and_cleans_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    canned.joinpath("review-replies.json").write_text(
        json.dumps({"PRRT_fix": {"fixed": True, "reply": "Fixed."}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_UV_FAIL", "run ruff check .")

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "failed"
    assert report["round_number"] == 1
    assert report["checks"] == {"passed": False, "failed": "uv run ruff check ."}
    assert "uv exited with status 7" in report["failure_reason"]
    assert report["threads_fixed"] == 0
    assert report["threads_answered"] == 0

    records = _records(log)
    codex_calls = [_arguments(record) for record in records if record["command"] == "codex"]
    assert len(codex_calls) == 2
    assert "resume" not in codex_calls[0]
    assert "resume" in codex_calls[1]
    assert 'model_reasoning_effort="high"' in codex_calls[1]
    calls = [_arguments(record) for record in records]
    assert ["pr", "edit", "24", "--add-label", "ready-for-human"] in calls
    assert ["push"] not in calls
    assert not any(
        arguments[:2] == ["api", "graphql"] and any("mutation " in item for item in arguments)
        for arguments in calls
    )
    assert ["pr", "comment", "24", "--body", "Babysit round 1 of 3: started."] in calls
    workspace = tmp_path / "coder" / "workspace"
    assert not workspace.joinpath("fixed.py").exists()
    assert not workspace.joinpath(".scratch/review-replies.json").exists()
    assert calls[-5:] == [
        ["reset", "--hard"],
        ["clean", "-fd"],
        ["switch", "--discard-changes", "-C", "main", "origin/main"],
        ["branch", "-D", "agent/225-babysit"],
        ["pr", "edit", "24", "--add-label", "ready-for-human"],
    ]


def test_one_checks_fix_can_recover_the_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    canned.joinpath("review-replies.json").write_text(
        json.dumps({"PRRT_fix": {"fixed": True, "reply": "Fixed."}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_UV_FAIL_ONCE", "run ruff check .")

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "fixed"
    assert report["checks"] == {"passed": True, "failed": None}
    assert _mapping(report["codex"])["usage"] == {
        "input_tokens": 180,
        "cached_input_tokens": 120,
        "output_tokens": 50,
    }
    records = _records(log)
    codex_calls = [_arguments(record) for record in records if record["command"] == "codex"]
    assert len(codex_calls) == 2
    assert "resume" not in codex_calls[0]
    assert "resume" in codex_calls[1]
    uv_calls = [_arguments(record) for record in records if record["command"] == "uv"]
    assert uv_calls == [
        ["run", "ruff", "check", "."],
        ["run", "ruff", "check", "."],
        ["run", "ruff", "format", "--check", "."],
        ["run", "ty", "check"],
        ["run", "pytest"],
    ]
    assert ["push"] in [_arguments(record) for record in records]


def test_fix_run_uses_the_routine_model_effort_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "10")

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
        babysitting={"model": "gpt-test-fix", "effort": "low", "timeout_seconds": 0.5},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "failed"
    assert "codex exceeded its 0.5-second limit" in str(report["failure_reason"])
    codex = next(record for record in _records(log) if record["command"] == "codex")
    arguments = _arguments(codex)
    assert "gpt-test-fix" in arguments
    assert 'model_reasoning_effort="low"' in arguments


def test_checks_fix_uses_its_routine_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    canned.joinpath("review-replies.json").write_text(
        json.dumps({"PRRT_fix": {"fixed": True, "reply": "Fixed."}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_UV_FAIL_ONCE", "run ruff check .")
    monkeypatch.setenv("FAKE_CODEX_RESUME_SLEEP", "10")

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
        babysitting={"checks_fix_timeout_seconds": 0.5},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "failed"
    assert "codex exceeded its 0.5-second limit" in str(report["failure_reason"])
    codex_calls = [_arguments(record) for record in _records(log) if record["command"] == "codex"]
    assert len(codex_calls) == 2
    assert "resume" in codex_calls[1]


@pytest.mark.parametrize(
    ("failure", "value", "reason", "checks"),
    [
        (
            "FAKE_CODEX_EXIT",
            "9",
            "codex exited with status 9",
            None,
        ),
        (
            "FAKE_CODEX_WRITE_REPLIES",
            "0",
            "Codex did not write review replies",
            None,
        ),
        (
            "FAKE_GIT_FAIL",
            "push",
            "git exited with status 7",
            {"passed": True, "failed": None},
        ),
        (
            "FAKE_UNCHANGED_HEAD",
            "1",
            "without advancing HEAD",
            {"passed": True, "failed": None},
        ),
        (
            "FAKE_DIRTY_STATUS",
            " M fixed.py",
            "uncommitted workspace changes",
            {"passed": True, "failed": None},
        ),
    ],
)
def test_codex_replies_or_push_failure_stops_before_changing_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    value: str,
    reason: str,
    checks: dict[str, object] | None,
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    canned.joinpath("review-replies.json").write_text(
        json.dumps({"PRRT_fix": {"fixed": True, "reply": "Fixed."}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(failure, value)

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "failed"
    assert reason in report["failure_reason"]
    assert report["checks"] == checks
    calls = [_arguments(record) for record in _records(log)]
    assert ["pr", "edit", "24", "--add-label", "ready-for-human"] in calls
    assert not any(
        arguments[:2] == ["api", "graphql"] and any("mutation " in item for item in arguments)
        for arguments in calls
    )
    assert ["pr", "comment", "24", "--body", "Babysit round 1 of 3: started."] in calls
    workspace = tmp_path / "coder" / "workspace"
    assert not workspace.joinpath("fixed.py").exists()
    assert not workspace.joinpath(".scratch/review-replies.json").exists()


@pytest.mark.parametrize(
    ("failure", "value", "warning"),
    [
        (
            "FAKE_GH_FAIL",
            "mutation ReplyToReviewThread",
            "reply to thread PRRT_fix failed",
        ),
        (
            "FAKE_GIT_FAIL",
            "branch -D",
            "workspace cleanup failed",
        ),
    ],
)
def test_post_push_failure_keeps_the_fixed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    value: str,
    warning: str,
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[{"status": "completed"}],
        reviews=[{"commit_id": "head-24"}],
        comments=[],
    )
    canned.joinpath("review-replies.json").write_text(
        json.dumps({"PRRT_fix": {"fixed": True, "reply": "Fixed."}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(failure, value)

    output = _run_babysit(
        tmp_path,
        monkeypatch,
        capsys,
        {"action": "submitted", "pull_request": {"head": {"ref": "agent/225-babysit"}}},
    )

    result_line = next(
        line
        for line in output.splitlines()
        if line.startswith("[tool.result] babysit_pull_request (ok): ")
    )
    (report,) = json.loads(result_line.partition(": ")[2])
    assert report["outcome"] == "fixed"
    assert report["threads_fixed"] == 1
    assert report["threads_answered"] == 0
    assert report["failure_reason"] is None
    assert any(warning in item for item in report["warnings"])
    calls = [_arguments(record) for record in _records(log)]
    assert ["push"] in calls
    assert calls.index(
        ["pr", "comment", "24", "--body", "Babysit round 1 of 3: started."]
    ) < calls.index(["push"])
    assert ["pr", "edit", "24", "--add-label", "ready-for-human"] not in calls


@pytest.mark.parametrize("author", ["outside-contributor", "greptile-apps-staging", ""])
def test_untrusted_review_does_not_start_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    author: str,
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread(author, thread_id="PRRT_fix")],
        checks=[],
        reviews=[],
        comments=[],
    )

    output = _run_babysit(tmp_path, monkeypatch, capsys, None)

    assert "untrusted author" in output
    assert not any(record["command"] == "codex" for record in _records(log))
    assert ["push"] not in [_arguments(record) for record in _records(log)]


def test_round_marker_failure_does_not_start_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canned, log = _fake_fix_clients(tmp_path, monkeypatch)
    _write_scan(
        canned,
        threads=[_review_thread("reviewer", thread_id="PRRT_fix")],
        checks=[],
        reviews=[],
        comments=[],
    )
    monkeypatch.setenv("FAKE_GH_FAIL", "pr comment")

    output = _run_babysit(tmp_path, monkeypatch, capsys, None)

    assert '"outcome":"failed"' in output
    assert not any(record["command"] == "codex" for record in _records(log))
    assert ["push"] not in [_arguments(record) for record in _records(log)]

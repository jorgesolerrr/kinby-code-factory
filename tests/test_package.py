"""A self-hoster initializes the factory from the installed package and runs it on any repo."""

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from kinby.cli import main
from kinby.instance import init_instance, load_instance
from kinby.packages import (
    SetupFieldKind,
    SetupTarget,
    TargetFile,
    installed_package,
    load_package,
    package_description,
    read_package_config,
)
from kinby.packages.check import check_package
from kinby.plugins.routines import load_routines

from kinby_code_factory.config import CommitIdentity, FactoryConfig
from tests.test_factory import (
    _arguments,
    _coder_copy,
    _fake_clients,
    _records,
    _report,
    _RoutineModel,
    _text,
    _use_routine_model,
    _write_executable,
    configure,
)


def _run_implementation(instance: Path, tmp_path: Path) -> int:
    payload = tmp_path / "delivery.json"
    payload.write_text(
        json.dumps({"action": "labeled", "label": {"name": "ready-for-agent"}}),
        encoding="utf-8",
    )
    return main(
        [
            "routine",
            "run",
            "implement-ready-issue",
            "--payload",
            str(payload),
            "--instance",
            str(instance),
        ]
    )


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    instance = _coder_copy(tmp_path, client="claude")
    _use_routine_model(monkeypatch, load_instance(instance), _RoutineModel())
    return instance, _fake_clients(tmp_path, monkeypatch)


def test_initialization_copies_an_editable_factory_that_matches_the_live_coder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    instance = _coder_copy(tmp_path, client="claude")

    manifest = tomllib.loads((instance / "kinby.toml").read_text(encoding="utf-8"))
    assert manifest["package"]["id"] == "coder"
    assert manifest["package"]["distribution"] == "kinby-code-factory"
    config = yaml.safe_load((instance / "package.yaml").read_text(encoding="utf-8"))
    assert config["review"]["enabled"] is False
    assert config["babysitting"]["enabled"] is False
    assert config["implementation"] == {
        "client": "claude",
        "model": "claude-opus-5-5",
        "effort": "high",
        "timeout_seconds": 3600,
        "fix_timeout_seconds": 900,
    }
    assert config["secrets"] == {"github_token": "GH_TOKEN"}
    routines, warnings = load_routines(load_instance(instance))
    assert warnings == ()
    assert {routine.name: routine.enabled for routine in routines} == {
        "babysit-pull-request": True,
        "implement-ready-issue": True,
    }
    assert all(routine.arguments == {} for routine in routines)
    assert not (instance / "skills" / "implement-ticket").exists()


# What `codex login --device-auth` prints, once the hub strips its terminal colors.
CODEX_DEVICE_PROMPT = """
Welcome to Codex [v0.154.0]
OpenAI's command-line coding agent

Follow these steps to sign in with ChatGPT using device code authorization:

1. Open this link in your browser and sign in to your account
   https://auth.openai.com/codex/device

2. Enter this one-time code (expires in 15 minutes)
   K7QD-M2XPA

Continue only if you started this login in Codex. If a website or another person gave you
this code, cancel.
"""


def test_the_wizard_asks_for_everything_a_coder_instance_needs() -> None:
    package = load_package("coder").package
    description = package_description(installed_package(load_package("coder")))
    fields = {field.name: field for field in description.setup_fields}

    assert package.required_secrets == ()
    assert fields["model"].default == "anthropic:claude-sonnet-5"
    assert fields["model"].target is None
    targets = {name: field.target for name, field in fields.items() if field.target}
    assert targets == {
        "repository": SetupTarget(file=TargetFile.KINBY_TOML, key="workspace.source"),
        "commit_name": SetupTarget(file=TargetFile.PACKAGE_YAML, key="commit.name"),
        "commit_email": SetupTarget(file=TargetFile.PACKAGE_YAML, key="commit.email"),
    }
    assert all(fields[name].kind is SetupFieldKind.CONFIG for name in targets)
    secrets = {name for name, field in fields.items() if field.kind is SetupFieldKind.SECRET}
    assert secrets == {"api_key", "GH_TOKEN", "GITHUB_WEBHOOK_SECRET", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert "claude setup-token" in fields["CLAUDE_CODE_OAUTH_TOKEN"].description


def test_codex_signs_in_through_the_app_into_its_own_volume() -> None:
    (login,) = package_description(installed_package(load_package("coder"))).logins

    assert login.id == "codex"
    assert login.command == ["codex", "login", "--device-auth"]
    assert login.volume == "/root/.codex"
    found = re.search(login.prompt_pattern, CODEX_DEVICE_PROMPT)
    assert found is not None
    assert found["url"] == "https://auth.openai.com/codex/device"
    assert found["code"] == "K7QD-M2XPA"


def test_the_package_check_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_clients(tmp_path, monkeypatch)

    failures = check_package("coder")

    # An editable install records no template files; CI checks the installed wheel in the image.
    assert [failure for failure in failures if "is not part of distribution" not in failure] == []


def test_setup_values_land_where_the_descriptor_declares_them(tmp_path: Path) -> None:
    instance = init_instance(
        tmp_path / "coder",
        "anthropic:claude-sonnet-5",
        package=installed_package(load_package("coder")),
        config={
            "repository": "https://github.com/octo/widgets.git",
            "commit_name": "Widget factory",
            "commit_email": "factory@example.test",
        },
    )

    assert load_instance(instance).manifest.workspace.source == (
        "https://github.com/octo/widgets.git"
    )
    config = read_package_config(load_package("coder").package, instance)
    assert isinstance(config, FactoryConfig)
    assert config.commit == CommitIdentity(name="Widget factory", email="factory@example.test")


def test_the_coding_clients_commit_as_the_configured_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)
    configure(instance, commit={"name": "Widget factory", "email": "factory@example.test"})

    assert _run_implementation(instance, tmp_path) == 0

    assert _report(capsys.readouterr().out)["outcome"] == "opened"
    commands = [[record["command"], *_arguments(record)] for record in _records(log)]
    before_claude = commands[: next(i for i, c in enumerate(commands) if c[0] == "claude")]
    assert ["git", "config", "user.name", "Widget factory"] in before_claude
    assert ["git", "config", "user.email", "factory@example.test"] in before_claude


def test_without_a_configured_identity_git_keeps_its_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)

    assert _run_implementation(instance, tmp_path) == 0

    assert _report(capsys.readouterr().out)["outcome"] == "opened"
    git = [_arguments(record) for record in _records(log) if record["command"] == "git"]
    assert not any(arguments[:1] == ["config"] for arguments in git)


def test_initialization_refuses_a_nonempty_destination(tmp_path: Path) -> None:
    destination = tmp_path / "coder"
    destination.mkdir()
    (destination / "notes.md").write_text("mine\n", encoding="utf-8")

    exit_code = main(
        ["init", str(destination), "--package", "coder", "--model", "anthropic:claude-sonnet-5"]
    )

    assert exit_code != 0
    assert [path.name for path in destination.iterdir()] == ["notes.md"]


def test_a_non_python_repository_runs_its_own_checks_with_packaged_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)
    configure(instance, checks={"commands": ["make lint", "npm test -- --ci"]})
    binaries = tmp_path / "bin"
    for command in ("make", "npm"):
        _write_executable(
            binaries / command,
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "record = {'command': Path(sys.argv[0]).name, 'arguments': sys.argv[1:]}\n"
            "with Path(os.environ['FACTORY_COMMAND_LOG']).open('a') as stream:\n"
            "    stream.write(json.dumps(record) + '\\n')\n",
        )

    assert _run_implementation(instance, tmp_path) == 0

    report = _report(capsys.readouterr().out)
    assert report["outcome"] == "opened"
    records = _records(log)
    checks = [
        [record["command"], *_arguments(record)]
        for record in records
        if record["command"] in {"make", "npm", "uv"}
    ]
    assert checks == [["make", "lint"], ["npm", "test", "--", "--ci"]]
    claude = next(record for record in records if record["command"] == "claude")
    prompt = _text(claude, "stdin")
    assert "Follow this implement-ticket skill exactly" in prompt
    assert "gh issue view" in prompt
    assert "Run the `unslop` skill over the body" in prompt
    assert "kinby_code_factory/skills/tdd/SKILL.md" in prompt
    assert not (instance / "workspace" / ".claude").exists()


def test_an_instance_skill_overrides_the_packaged_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)
    override = instance / "skills" / "implement-ticket"
    override.mkdir(parents=True)
    (override / "SKILL.md").write_text(
        "---\nname: implement-ticket\ndescription: House rules.\n---\nShip it our way.\n",
        encoding="utf-8",
    )

    assert _run_implementation(instance, tmp_path) == 0

    assert _report(capsys.readouterr().out)["outcome"] == "opened"
    claude = next(record for record in _records(log) if record["command"] == "claude")
    assert "Ship it our way." in _text(claude, "stdin")
    assert "gh issue view" not in _text(claude, "stdin")


def test_a_selected_skill_the_instance_lacks_fails_before_github_is_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)
    configure(instance, skills={"implement": "no-such-skill"})

    assert _run_implementation(instance, tmp_path) == 0

    report = _report(capsys.readouterr().out)
    assert report["outcome"] == "failed"
    assert 'skill "no-such-skill"' in str(report["failure_reason"])
    assert not log.exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"review": {"rounds": 2}}, "review.rounds"),
        ({"secrets": {"github_token": "OTHER_TOKEN"}}, "not a required secret"),
        ({"checks": {"commands": ["  "]}}, "a check command cannot be empty"),
        ({"implementation": {"client": "cursor"}}, "implementation.client"),
    ],
)
def test_invalid_package_config_fails_the_run_and_names_the_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, dict[str, object]],
    message: str,
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)
    configure(instance, **change)

    assert _run_implementation(instance, tmp_path) != 0

    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    assert not log.exists()


def test_an_unset_github_token_is_a_failure_not_an_empty_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, log = _prepare(tmp_path, monkeypatch)
    monkeypatch.delenv("GH_TOKEN")

    assert _run_implementation(instance, tmp_path) != 0

    captured = capsys.readouterr()
    assert "GH_TOKEN is not set" in captured.out + captured.err
    assert not log.exists()


def test_gh_and_git_push_get_the_configured_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance, _ = _prepare(tmp_path, monkeypatch)
    token_log = tmp_path / "tokens.jsonl"
    monkeypatch.setenv("TOKEN_LOG", str(token_log))
    binaries = tmp_path / "bin"
    for command in ("gh", "git"):
        fake = binaries / command
        original = fake.read_text(encoding="utf-8")
        header, _, body = original.partition("\n")
        fake.write_text(
            f"{header}\nimport json as _json, os as _os, sys as _sys\n"
            "with open(_os.environ['TOKEN_LOG'], 'a') as _stream:\n"
            "    _seen = [_sys.argv[1:3], _os.environ.get('GH_TOKEN')]\n"
            "    _stream.write(_json.dumps(_seen) + '\\n')\n"
            f"{body}",
            encoding="utf-8",
        )

    assert _run_implementation(instance, tmp_path) == 0

    assert _report(capsys.readouterr().out)["outcome"] == "opened"
    seen = [json.loads(line) for line in token_log.read_text(encoding="utf-8").splitlines()]
    assert {token for _, token in seen} == {"github-token"}
    assert ["push", "--force-with-lease"] in [arguments for arguments, _ in seen]

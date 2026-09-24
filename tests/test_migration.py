"""The existing coder moves onto the package with its settings, identity and data intact."""

import shutil
import tomllib
from pathlib import Path

import pytest
from kinby.instance import load_instance
from kinby.packages import instance_package_config
from kinby.plugins.routines import load_routines

from kinby_code_factory.config import FactoryConfig
from kinby_code_factory.migrate import main

FIXTURE = Path(__file__).parent / "fixtures" / "built-in-coder"
UNTOUCHED = (
    "SYSTEM.md",
    "RECAP.md",
    "permissions.toml",
    "memory/profile.md",
    "memory/graph/2026-09-11-trace.md",
    "skills/unslop/SKILL.md",
    ".env",
    ".state/events.jsonl",
    "workspace/README.md",
)


def _live_coder(tmp_path: Path) -> Path:
    """The built-in coder with a transcript, memory, secrets and a workspace of its own."""
    instance = tmp_path / "coder"
    shutil.copytree(FIXTURE, instance)
    (instance / "memory" / "graph").mkdir()
    (instance / "memory" / "graph" / "2026-09-11-trace.md").write_text("A trace.\n")
    (instance / ".state").mkdir()
    (instance / ".state" / "events.jsonl").write_text('{"kind": "turn.started"}\n')
    (instance / ".env").write_text("GH_TOKEN=ghp-live\nGITHUB_WEBHOOK_SECRET=live\n")
    (instance / "workspace").mkdir()
    (instance / "workspace" / "README.md").write_text("The repository.\n")
    return instance


def test_the_live_settings_move_into_package_yaml_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    instance = _live_coder(tmp_path)
    before = {name: (instance / name).read_bytes() for name in UNTOUCHED}
    manifest_before = (instance / "kinby.toml").read_text(encoding="utf-8")

    assert main([str(instance)]) == 0

    assert {name: (instance / name).read_bytes() for name in UNTOUCHED} == before
    manifest = (instance / "kinby.toml").read_text(encoding="utf-8")
    assert manifest.startswith(manifest_before)
    parsed = tomllib.loads(manifest)
    assert parsed["id"] == "coder"
    assert parsed["package"]["id"] == "coder"
    assert parsed["package"]["distribution"] == "kinby-code-factory"
    config = instance_package_config(load_instance(instance))
    assert isinstance(config, FactoryConfig)
    implementation = config.implementation
    assert (implementation.client, implementation.model, implementation.effort) == (
        "claude",
        "claude-opus-5-5",
        "high",
    )
    assert implementation.timeout_seconds == 3600
    assert implementation.fix_timeout_seconds == 900
    assert config.review.enabled is False
    assert (config.review.model, config.review.timeout_seconds) == ("claude-fable-5-1", 900)
    babysitting = config.babysitting
    assert babysitting.enabled is False
    assert (babysitting.model, babysitting.effort, babysitting.round_limit) == (
        "gpt-5.6-sol",
        "high",
        3,
    )
    assert (babysitting.timeout_seconds, babysitting.checks_fix_timeout_seconds) == (900, 900)
    assert "# Wall-clock limit for implementing one issue." in (
        instance / "package.yaml"
    ).read_text(encoding="utf-8")


def test_the_routines_run_the_package_with_no_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    instance = _live_coder(tmp_path)

    assert main([str(instance)]) == 0

    routines, warnings = load_routines(load_instance(instance))
    assert warnings == ()
    assert {routine.name: routine.enabled for routine in routines} == {
        "babysit-pull-request": True,
        "implement-ready-issue": True,
    }
    assert all(routine.arguments == {} for routine in routines)
    for name in ("implement-ready-issue", "babysit-pull-request"):
        wrapper = (instance / "routines" / name / "run.py").read_text(encoding="utf-8")
        assert wrapper.startswith("from kinby_code_factory import ")
        routine = (instance / "routines" / name / "ROUTINE.md").read_text(encoding="utf-8")
        assert "signature_header: X-Hub-Signature-256" in routine


def test_a_migrated_instance_is_refused_and_left_alone(tmp_path: Path) -> None:
    instance = _live_coder(tmp_path)
    assert main([str(instance)]) == 0
    migrated = {path: path.read_bytes() for path in instance.rglob("*") if path.is_file()}

    assert main([str(instance)]) == 1

    assert {path: path.read_bytes() for path in instance.rglob("*") if path.is_file()} == migrated

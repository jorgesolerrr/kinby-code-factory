"""Move a coder instance that ran kinby's built-in factory onto this package.

``python -m kinby_code_factory.migrate <instance-directory>`` writes package.yaml from
the routines' current arguments without changing their values, empties those arguments,
switches the babysitting on or off in package.yaml instead of in its routine, points the
routine wrappers at this package, and records the package in kinby.toml. Everything else
in the instance is left as it is. It refuses an instance that already has a package.yaml.
"""

import json
import re
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import yaml

from kinby_code_factory import ROOT

IMPLEMENT = "implement-ready-issue"
BABYSIT = "babysit-pull-request"
DISTRIBUTION = "kinby-code-factory"

#: Where each old routine argument lands in package.yaml.
_IMPLEMENT_ARGUMENTS = {
    "implementer_client": ("implementation", "client"),
    "implementer_model": ("implementation", "model"),
    "implementer_effort": ("implementation", "effort"),
    "implement_timeout_seconds": ("implementation", "timeout_seconds"),
    "fix_timeout_seconds": ("implementation", "fix_timeout_seconds"),
    "reviewer_model": ("review", "model"),
    "review_timeout_seconds": ("review", "timeout_seconds"),
}
_BABYSIT_ARGUMENTS = {
    "fix_model": ("babysitting", "model"),
    "fix_effort": ("babysitting", "effort"),
    "round_limit": ("babysitting", "round_limit"),
    "fix_timeout_seconds": ("babysitting", "timeout_seconds"),
    "checks_fix_timeout_seconds": ("babysitting", "checks_fix_timeout_seconds"),
}
#: What the built-in factory used when a routine left an argument out.
_BUILT_IN_DEFAULTS: dict[tuple[str, str], object] = {
    ("implementation", "client"): "claude",
    ("implementation", "model"): "claude-opus-5",
    ("implementation", "effort"): "high",
    ("implementation", "timeout_seconds"): 1800,
    ("implementation", "fix_timeout_seconds"): 900,
    ("review", "model"): "claude-fable-5-1",
    ("review", "timeout_seconds"): 600,
    ("babysitting", "model"): "gpt-5.6-sol",
    ("babysitting", "effort"): "high",
    ("babysitting", "round_limit"): 3,
    ("babysitting", "timeout_seconds"): 900,
    ("babysitting", "checks_fix_timeout_seconds"): 900,
}
_ARGUMENTS_LINE = re.compile(r"^arguments:\s*(.*)\n", re.MULTILINE)
_ENABLED_LINE = re.compile(r"^enabled:\s*(\S+)\s*$", re.MULTILINE)
_WRAPPER_IMPORT = "from kinby.factory import"
_SETTING = re.compile(r"^(  )([a-z_]+):( .*)?$")


class MigrationRefused(RuntimeError):
    """The instance is not a built-in factory coder, or it was already migrated."""


def migrate(instance: Path) -> None:
    """Rewrite *instance* in place to run the factory from this package."""
    config_path = instance / "package.yaml"
    manifest_path = instance / "kinby.toml"
    if config_path.exists():
        raise MigrationRefused(f"{config_path} exists; this instance was already migrated.")
    if "package" in tomllib.loads(manifest_path.read_text(encoding="utf-8")):
        raise MigrationRefused(f"{manifest_path} already records a package.")
    implement = _Routine(instance, IMPLEMENT)
    babysit = _Routine(instance, BABYSIT)

    settings = {
        **_BUILT_IN_DEFAULTS,
        **_moved(implement.arguments, _IMPLEMENT_ARGUMENTS),
        **_moved(babysit.arguments, _BABYSIT_ARGUMENTS),
    }
    review_rounds = implement.arguments.get("review_round_limit", 0)
    settings[("review", "enabled")] = bool(review_rounds)
    if review_rounds:
        settings[("review", "round_limit")] = review_rounds
    settings[("babysitting", "enabled")] = babysit.enabled

    config = _package_config(settings)
    for routine in (implement, babysit):
        routine.write_for_package()
    config_path.write_text(config, encoding="utf-8")
    with manifest_path.open("a", encoding="utf-8") as manifest:
        manifest.write(
            "\n[package]\n"
            'id = "coder"\n'
            f'distribution = "{DISTRIBUTION}"\n'
            f'version = "{version(DISTRIBUTION)}"\n'
        )


class _Routine:
    def __init__(self, instance: Path, name: str) -> None:
        self.definition = instance / "routines" / name / "ROUTINE.md"
        self.wrapper = instance / "routines" / name / "run.py"
        self.text = self.definition.read_text(encoding="utf-8")
        if _WRAPPER_IMPORT not in self.wrapper.read_text(encoding="utf-8"):
            raise MigrationRefused(f"{self.wrapper} does not import kinby's built-in factory.")
        arguments = _ARGUMENTS_LINE.search(self.text)
        self.arguments: dict[str, object] = json.loads(arguments[1]) if arguments else {}
        enabled = _ENABLED_LINE.search(self.text)
        self.enabled = enabled is None or enabled[1] == "true"

    def write_for_package(self) -> None:
        text = _ARGUMENTS_LINE.sub("", self.text, count=1)
        text = _ENABLED_LINE.sub("enabled: true", text, count=1)
        self.definition.write_text(text, encoding="utf-8")
        wrapper = self.wrapper.read_text(encoding="utf-8")
        self.wrapper.write_text(
            wrapper.replace(_WRAPPER_IMPORT, "from kinby_code_factory import"), encoding="utf-8"
        )


def _moved(
    arguments: dict[str, object], destinations: dict[str, tuple[str, str]]
) -> dict[tuple[str, str], object]:
    unknown = set(arguments) - set(destinations) - {"review_round_limit"}
    if unknown:
        raise MigrationRefused(f"Unknown routine arguments: {', '.join(sorted(unknown))}.")
    return {destinations[name]: value for name, value in arguments.items() if name in destinations}


def _package_config(settings: dict[tuple[str, str], object]) -> str:
    """The template's package.yaml, comments included, with these settings' values."""
    lines = []
    section = ""
    for line in (ROOT / "template" / "package.yaml").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith((" ", "#")):
            section = line.removesuffix(":")
        elif (match := _SETTING.match(line)) and (section, match[2]) in settings:
            value = yaml.safe_dump(settings[(section, match[2])], default_flow_style=True)
            line = f"{match[1]}{match[2]}: {value.removesuffix('\n...\n').strip()}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python -m kinby_code_factory.migrate <instance-directory>", file=sys.stderr)
        return 2
    try:
        migrate(Path(arguments[0]))
    except (MigrationRefused, OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Migrated {arguments[0]}. Review package.yaml, then update the instance's image.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

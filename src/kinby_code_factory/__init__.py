"""The software factory: implement GitHub issues and babysit their pull requests."""

from pathlib import Path

from kinby.packages import (
    Package,
    SetupField,
    SetupFieldKind,
    SetupFieldType,
    SetupTarget,
    SubscriptionLogin,
    TargetFile,
)

from kinby_code_factory.babysit import babysit_pull_request
from kinby_code_factory.config import FactoryConfig
from kinby_code_factory.pipeline import implement_ready_issue

ROOT = Path(__file__).parent


def _config(
    name: str,
    label: str,
    description: str,
    type: SetupFieldType,
    target: SetupTarget,
) -> SetupField:
    return SetupField(
        name=name,
        label=label,
        description=description,
        kind=SetupFieldKind.CONFIG,
        type=type,
        required=True,
        target=target,
    )


def _secret(name: str, label: str, description: str) -> SetupField:
    return SetupField(
        name=name,
        label=label,
        description=description,
        kind=SetupFieldKind.SECRET,
        type=SetupFieldType.TEXT,
        required=True,
    )


PACKAGE = Package(
    display_name="Software factory",
    description=(
        "Implements GitHub issues labeled ready-for-agent with a coding client "
        "and opens their pull requests."
    ),
    icon="code",
    template=ROOT / "template",
    setup_fields=(
        _config(
            "repository",
            "Repository",
            "The git URL of the repository the factory clones and works in, like "
            "https://github.com/<owner>/<repository>.git.",
            SetupFieldType.URL,
            SetupTarget(file=TargetFile.KINBY_TOML, key="workspace.source"),
        ),
        SetupField(
            name="model",
            label="Model",
            description="The model the instance calls, as provider:model.",
            kind=SetupFieldKind.CONFIG,
            type=SetupFieldType.TEXT,
            required=True,
            default="anthropic:claude-sonnet-5",
        ),
        _config(
            "commit_name",
            "Commit author name",
            "The name on the commits the coding clients make.",
            SetupFieldType.TEXT,
            SetupTarget(file=TargetFile.PACKAGE_YAML, key="commit.name"),
        ),
        _config(
            "commit_email",
            "Commit author email",
            "The email on the commits the coding clients make. Use one GitHub links to the "
            "token's account, so the commits show as its own.",
            SetupFieldType.EMAIL,
            SetupTarget(file=TargetFile.PACKAGE_YAML, key="commit.email"),
        ),
        _secret(
            "GH_TOKEN",
            "GitHub token",
            "A token with repo scope. gh, git push, issues and pull requests run as it.",
        ),
        _secret(
            "GITHUB_WEBHOOK_SECRET",
            "Webhook secret",
            "The shared secret of the repository webhook that signs each delivery. "
            "Generate one with `openssl rand -hex 32`.",
        ),
        _secret(
            "CLAUDE_CODE_OAUTH_TOKEN",
            "Claude Code token",
            "Claude Code's subscription token. Run `claude setup-token` on your own machine "
            "and paste the token it prints.",
        ),
    ),
    config=FactoryConfig,
    executables=("claude", "codex", "gh", "git"),
    logins=(
        SubscriptionLogin(
            id="codex",
            label="Codex",
            description="Sign in with your ChatGPT subscription. Codex babysits pull requests "
            "and can implement issues.",
            command=["codex", "login", "--device-auth"],
            volume="/root/.codex",
            # The URL and the code each sit on the line after their step's instruction.
            prompt_pattern=(
                r"Open this link[^\n]*\n\s*(?P<url>https://\S+)"
                r"[\s\S]*?one-time code[^\n]*\n\s*(?P<code>\S+)"
            ),
        ),
    ),
)
SKILLS = ROOT / "skills"

__all__ = ["PACKAGE", "SKILLS", "babysit_pull_request", "implement_ready_issue"]

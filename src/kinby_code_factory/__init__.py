"""The software factory: implement GitHub issues and babysit their pull requests."""

from pathlib import Path

from kinby.packages import Package, RequiredSecret

from kinby_code_factory.babysit import babysit_pull_request
from kinby_code_factory.config import FactoryConfig
from kinby_code_factory.pipeline import implement_ready_issue

ROOT = Path(__file__).parent

PACKAGE = Package(
    display_name="Software factory",
    description=(
        "Implements GitHub issues labeled ready-for-agent with a coding client "
        "and opens their pull requests."
    ),
    icon="code",
    template=ROOT / "template",
    required_secrets=(
        RequiredSecret(
            "GH_TOKEN",
            "GitHub token",
            "A token with repo scope. gh, git push, issues and pull requests run as it.",
        ),
        RequiredSecret(
            "GITHUB_WEBHOOK_SECRET",
            "Webhook secret",
            "The shared secret of the repository webhook that signs each delivery.",
        ),
    ),
    config=FactoryConfig,
    executables=("claude", "codex", "gh", "git"),
)
SKILLS = ROOT / "skills"

__all__ = ["PACKAGE", "SKILLS", "babysit_pull_request", "implement_ready_issue"]

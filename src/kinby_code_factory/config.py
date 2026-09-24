"""The factory's package.yaml: every setting its routines read, validated by kinby."""

import shlex
from typing import Annotated

from kinby.packages import PackageConfig, SecretName
from kinby.plugins import ToolContext
from pydantic import AfterValidator, Field

from kinby_code_factory.clients import CodingClient, CodingModel, ReasoningEffort

Seconds = Annotated[float, Field(gt=0)]
Name = Annotated[str, Field(min_length=1)]


def _command(line: str) -> str:
    if not shlex.split(line):
        raise ValueError("a check command cannot be empty")
    return line


class Secrets(PackageConfig):
    github_token: SecretName


class Implementation(PackageConfig):
    client: CodingClient
    model: CodingModel
    effort: ReasoningEffort
    timeout_seconds: Seconds
    fix_timeout_seconds: Seconds


class Review(PackageConfig):
    enabled: bool
    model: CodingModel
    effort: ReasoningEffort
    round_limit: Annotated[int, Field(ge=1)]
    timeout_seconds: Seconds


class Babysitting(PackageConfig):
    enabled: bool
    model: CodingModel
    effort: ReasoningEffort
    round_limit: Annotated[int, Field(ge=1)]
    timeout_seconds: Seconds
    checks_fix_timeout_seconds: Seconds


class Checks(PackageConfig):
    commands: tuple[Annotated[str, AfterValidator(_command)], ...]
    timeout_seconds: Seconds


class Skills(PackageConfig):
    implement: Name
    pull_request: Name
    review: Name


class Commands(PackageConfig):
    git_timeout_seconds: Seconds
    github_timeout_seconds: Seconds


class FactoryConfig(PackageConfig):
    """The validated contents of an instance's package.yaml."""

    secrets: Secrets
    implementation: Implementation
    review: Review
    babysitting: Babysitting
    checks: Checks
    skills: Skills
    commands: Commands


def factory_config(context: ToolContext) -> FactoryConfig:
    """The package.yaml kinby read and validated for this run."""
    config = context.package_config
    if not isinstance(config, FactoryConfig):
        raise TypeError("the software factory runs only in an instance of package coder")
    return config

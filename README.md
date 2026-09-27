# kinby-code-factory

The software factory for [kinby](https://github.com/jorgesolerrr/kinby). An instance of package `coder` watches a GitHub repository, implements issues labeled `ready-for-agent` with a coding client, runs the repository's checks, and opens the pull request. It can also answer review threads on its own pull requests.

| | |
|---|---|
| Package ID | `coder` |
| Display name | Software factory |
| Distribution | `kinby-code-factory` |

Kinby records each coding run as a delegated run in the turn that started it, with its usage source, tokens and outcome, so `kinby usage` and `kinby stats` show what the factory spent on each subscription. A run that hits its plan limit is recorded as limited, with the time the plan resets.

The distribution ships the routines' code steps, the skills they use, and an instance template. An instance copies the template once: its prompts, permissions, routines and `package.yaml` are then yours to edit, and a package update never rewrites them.

## What it needs

- kinby, from the same image. The package depends on `kinby` without a pin.
- `claude`, `codex`, `gh`, `git` and `bun` on `PATH`. [`image/recipe.Dockerfile`](image/recipe.Dockerfile) adds them to kinby's base image.
- Secrets in the instance's `.env`: `GH_TOKEN`, `GITHUB_WEBHOOK_SECRET` and `CLAUDE_CODE_OAUTH_TOKEN`. kinby's web app asks for them when it creates the instance, and signs in Codex. See [docs/setup.md](docs/setup.md).

## Configuration

Every factory setting lives in the instance's `package.yaml`: the implementing client, model and effort, adversarial review, babysitting, every timeout, the check commands, and which skills to use. kinby validates it before each run, and an unknown key is an error. The template's copy explains each setting.

The workspace repository is set in `kinby.toml` under `[workspace].source`. The template's check commands are kinby's own; replace them with your repository's.

## Documentation

- [docs/setup.md](docs/setup.md): create a factory instance from the web app, sign in, and start it.
- [docs/migration.md](docs/migration.md): move the existing kinby coder onto this package.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest
```

Development and CI run against kinby `main`. Tests replace `gh`, `git`, `claude`, `codex` and the check commands with fake executables, so they need no GitHub access or model subscription. They run on Linux and macOS.

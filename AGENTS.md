# kinby-code-factory

The software factory package for [kinby](https://github.com/jorgesolerrr/kinby): package ID `coder`, distribution `kinby-code-factory`. What it does and how to set it up live in `README.md` and `docs/`.

Vocabulary comes from kinby's [`CONTEXT.md`](https://github.com/jorgesolerrr/kinby/blob/main/CONTEXT.md): instance, package, routine, code step, signal, hub. Architecture decisions that shape the factory are kinby ADRs; record new ones there.

## How to work here

- **Code.** Follow kinby's [`CODING-STANDARD.md`](https://github.com/jorgesolerrr/kinby/blob/main/CODING-STANDARD.md). Checks pass before any commit: `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`, `uv run pytest`. The tests use fake `gh`, `git`, `claude`, `codex` and check executables, and run on Linux or macOS.
- **Boundary.** kinby owns instances, package loading, `package.yaml` validation, routines and the hub. This repository owns the pipeline, babysitting, the packaged skills, the instance template and the image recipe. Import only kinby's public API: `kinby.packages`, `kinby.plugins`, `kinby.instance`.
- **Settings.** Every factory setting lives in `package.yaml`, validated by `FactoryConfig` in `src/kinby_code_factory/config.py`. A new setting goes in both, and in the template's `package.yaml` with a comment.
- **Template.** Files under `src/kinby_code_factory/template/` are copied into an instance once and then belong to its owner. Never assume an existing instance has the current template.
- **Releases.** A merge to `main` is a release: CI builds the image, runs `kinby package check coder` inside it, and updates the coder through the hub.

## Issue tracker

GitHub Issues on `jorgesolerrr/kinby-code-factory`, operated with `gh`. Labels follow kinby's: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`, `spec`.

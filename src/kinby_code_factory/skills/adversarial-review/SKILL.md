---
name: adversarial-review
description: "Two-axis review (Standards: repo coding standards plus a code-smell baseline; Spec: does the diff do what the ticket asked) run by the other model CLI, Codex when you are Claude and Claude when you are Codex, so the reviewer never shares the implementer's context. Fixed point optional; defaults to the merge base with the default branch. Use when the user wants a review of a branch, PR, or work in progress, asks to \"review since X\", or another skill needs a review loop."
---

Two-axis review of the diff between `HEAD` and a fixed point:

- **Standards**: does the code conform to this repo's documented coding standards?
- **Spec**: does the code faithfully implement the originating issue / spec?

Both axes run as **parallel reviewer processes** in the other model's CLI, so they share neither each other's context nor yours. The session that wrote the code believes the code is right; a fresh model with no memory of writing it does not. This skill dispatches the reviewers and aggregates what they return.

The software factory runs only the reviewer briefs in step 5, one Claude Code process per axis, and passes the diff, the commits, and the ticket itself.

## Process

### 1. Pin the fixed point

Whatever the user (or the calling skill) said is the fixed point (a commit SHA, branch name, tag, `main`, `HEAD~5`, etc.). If none was given, use the merge base: `git merge-base HEAD <default-branch>`, where the default branch comes from `git symbolic-ref refs/remotes/origin/HEAD` (fall back to `main`, then `master`). Say which fixed point you chose in the report.

Capture the diff command once: `git diff <fixed-point>...HEAD` (three-dot, so the comparison is against the merge-base). Also note the list of commits via `git log <fixed-point>..HEAD --oneline`.

Before going further, confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty. A bad ref or empty diff should fail here, not inside two reviewer processes.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. The ticket passed by the user or the calling skill, fetched from the issue tracker (`gh issue view <number>` on GitHub).
2. Issue references in the commit messages (`#123`, `Closes #45`, GitLab `!67`, etc.), fetched the same way.
3. A path the user passed as an argument.
4. A spec file under `docs/`, `docs/blueprints/`, `specs/`, or `.scratch/` matching the branch name or feature.
5. If nothing is found, ask the user where the spec is. If they say there isn't one, the **Spec** reviewer will skip and report "no spec available".

Write the fetched spec to a scratch file so the reviewer can read it without tracker access.

### 3. Identify the standards sources

Anything in the repo that documents how code should be written, such as `CODING_STANDARDS.md` or `CONTRIBUTING.md`.

On top of whatever the repo documents, the Standards axis always carries the **smell baseline** in [`references/smells.md`](references/smells.md): a fixed set of Fowler code smells (_Refactoring_, ch.3) that applies even when a repo documents nothing. The reviewer reads that file by absolute path.

### 4. Pick the reviewer CLI

Detect which agent you are and dispatch the other one. Verify the binary first (`codex --version` / `claude --version`).

| You are | Reviewer command (prompt on stdin, answer to a file) |
|---|---|
| Claude Code | `codex exec -m gpt-5.6-sol -s read-only -C <repo> -o <out.md> - < <prompt.md>` |
| Codex | `claude -p --model claude-fable-5 --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)" < <prompt.md> > <out.md>` |

The reviewer is read-only: it reports, it does not edit. If the other CLI is missing or fails to start, fall back to two sub-agents of your own model and state that in the report under **Reviewer**, since a same-model review is the weaker result.

### 5. Dispatch both reviewers in parallel

Write each prompt to a scratch file, start both processes in the background, wait for both. Each prompt states the repo path, the exact diff command and commit list, and ends with the brief below. Reviewers read files themselves; the prompt carries paths, not pasted contents.

**Standards prompt** should include:

- The full diff command and commit list.
- The list of standards-source files you found in step 3, plus the absolute path of `references/smells.md`.
- The brief: "Report, per file/hunk where relevant, (a) every place the diff violates a documented standard: cite the standard (file + the rule); and (b) any baseline smell you spot: name it and quote the hunk. Tag every bullet `[hard]` or `[suggestion]`: `[hard]` is a breach of a documented standard, cited; `[suggestion]` is everything else, and baseline smells are always `[suggestion]`. A documented repo standard overrides the baseline. Skip anything tooling enforces. One finding per bullet, each starting with the tag then `path:line`. Under 400 words. If you find nothing, answer exactly `No findings`."

**Spec prompt** should include:

- The diff command and commit list.
- The path of the scratch file holding the spec.
- The brief: "Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Every spec finding is `[hard]`: a missing, extra, or wrong requirement is a defect, so tag each bullet `[hard]` and start it with the tag then `path:line`. Under 400 words. If you find nothing, answer exactly `No findings`."

If the spec is missing, skip the Spec reviewer and note this in the final report.

### 6. Aggregate

Present the two reports under `## Standards` and `## Spec` headings, verbatim or lightly cleaned. Do **not** merge or rerank findings, because the two axes are deliberately separate (see _Why two axes_).

End with one line: **Reviewer** (which CLI and model ran, or the fallback), the fixed point, hard and suggestion counts per axis, and the worst hard issue _within each axis_ (if any). Don't pick a single winner across axes: that's the reranking the separation exists to prevent. A run is **clean** when neither axis returned a `[hard]` finding; suggestions alone do not make it unclean, they are the user's call. Say `Clean` or `Not clean` as the last word so a calling loop can stop on it.

## Why two axes

A change can pass one axis and fail the other:

- Code that follows every standard but implements the wrong thing → **Standards pass, Spec fail.**
- Code that does exactly what the issue asked but breaks the project's conventions → **Spec pass, Standards fail.**

Reporting them separately stops one axis from masking the other.

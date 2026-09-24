---
name: implement-ticket
description: Implement one GitHub issue test-first and commit it on the branch the software factory checked out. The factory's implementation step.
---

Implement the ticket the prompt names. The ticket is the spec: read it with `gh issue view <number> --comments`, including any parent issue it links. Then read how this repository wants code written: its agent instructions (`AGENTS.md`, `CLAUDE.md`), contributing guide, coding standards, domain glossary (`CONTEXT.md`), and any decision record in the area you will touch, whichever of these exist.

## Process

1. **Stay on the branch.** The pipeline checked out a fresh branch for this ticket from its base. Do not switch branches, push, or open a pull request. The pipeline owns those.

2. **Build test-first.** Follow the `tdd` skill at the seams the ticket names. A ticket that names the public interface counts as seam confirmation; when it names no seam, pick the narrowest public interface that shows the behavior and say so in the commit message. Every design decision (where a seam goes, how an interface is shaped, what a thing is called, which pattern to reach for) is settled by the repository's standards; when they are silent, follow the nearest existing code and note the choice in the commit message. Run the checks the repository documents as you go, and the full suite once the ticket's behavior is in. Done when every requirement in the ticket has a passing test at a seam and the checks pass.

3. **Commit.** One commit on the branch, its message referencing the ticket (`#123`). Done when `git status` shows nothing to commit.

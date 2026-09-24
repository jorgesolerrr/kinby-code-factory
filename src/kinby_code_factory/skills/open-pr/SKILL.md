---
name: open-pr
description: Write the pull request body for a factory change. The pipeline pushes the branch and opens the pull request that closes the ticket.
---

Write the body of the pull request for the branch you are on. The pipeline pushes the branch, opens the pull request, and puts `Closes #<ticket>` on its first line.

The body, in this order:

- What changed and why, in a few sentences. Name the design decisions you took where the ticket or the repository's standards were silent.
- Which checks ran and their result.

Run the `unslop` skill over the body before writing it. Plain and specific, no headings beyond what the content needs.

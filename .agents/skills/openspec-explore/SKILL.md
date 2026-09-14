---
name: openspec-explore
description: Enter explore mode - a thinking partner for exploring ideas, investigating problems, and clarifying requirements. Use when the user wants to think through something before or during a change.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Investigate the user's question using relevant project evidence. Discussion is read-only; an explicit request to capture decisions authorizes only the named artifacts.

- Use `openspec list --json` to resolve project context; for a selected change use `openspec status --change "<name>" --json`.
- Read the effective project context and relevant source/contracts. Separate findings, proposed decisions and unresolved questions.
- When capture is authorized, scaffold a new change with `openspec new change "<name>"`, then use status and `openspec instructions <artifact-id> --change "<name>" --json` for the requested artifacts. Respect declared/conditional skips; resolve missing prerequisites before dependent writes. Ask before creating an unrequested prerequisite that cannot be skipped.
- Use returned templates, rules and concrete paths; follow delegated generators when specified. Refresh status after writing. A partial capture is not a complete proposal.
- If the user switches to implementation, follow that authorization and the apply workflow; do not require a new conversation or a redundant proposal.

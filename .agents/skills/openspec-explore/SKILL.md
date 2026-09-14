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

Explore ideas, inspect evidence, and clarify decisions. This workflow is read-only by default.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Explore

1. Use `openspec list --json` to discover the resolved root and active changes. Read that root's `openspec/config.yaml` (or `config.yml`) when present: apply `context` as project constraints; artifact-specific `rules` matter when writing that artifact.
2. For a relevant change, run `openspec status --change "<name>" --json` and use `changeRoot`, `artifactPaths`, and `actionContext`. Start with scope and cross-cutting constraints, then read the complete sections relevant to the user's question and their dependencies. Do not load unrelated changes, archives, or every existing artifact by default.
3. Inspect relevant code, tests, configuration and source evidence before asking factual questions. Distinguish verified findings, assumptions, recommendations and unresolved decisions.
4. Ask focused questions only where the answer changes the outcome, scope, compatibility or acceptance criteria. Follow dependencies; offer a grounded recommendation when possible. Ordinary discussion does not require a questionnaire, a diagram, or an artifact.
5. Explain the useful finding and its implication. Use a small diagram or example only when it clarifies the issue. Stop when the user's question is answered; do not force a proposal.

## Capturing decisions

An explicit request to capture named artifacts authorizes that scope. Otherwise name the proposed files and edits, obtain confirmation before writing, and do not treat answers to design questions as write authorization. Do not ask again for already-granted authorization.

Only when capture is requested/authorized, read [artifact capture](references/capture.md). Use CLI scaffolding for a new change and preserve the schema's dependencies, conditional skips, rules and concrete output paths. Do not write unsolicited notes or checkpoints during discussion.

Do not implement business code or edit workflow configuration as an exploration side effect. If the user explicitly switches to implementation or skill/configuration editing, follow that newly authorized workflow; no repeated approval or new proposal is needed solely to leave exploration.

---
name: openspec-propose
description: Propose a new change with all artifacts generated in one step. Use when the user wants to quickly describe what they want to build and get a complete proposal with design, specs, and tasks ready for implementation.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Create a change and its required planning artifacts. Planning does not itself authorize implementation; follow any explicit user authorization and otherwise finish by presenting the artifacts for review.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Workflow

1. Establish the requested outcome, scope and acceptance criteria. Inspect relevant source, tests, configuration and authoritative documents before drafting. Clarify material ambiguity; record reasonable minor assumptions. Derive a kebab-case name if needed. If the name already exists, resolve whether to continue it or create a different change before writing.
2. Use the configured schema unless the user selects another. If schema discovery is requested, read [schema selection](references/schema-selection.md); do not load that reference for the default workflow.
3. Scaffold with `openspec new change "<name>"` (add `--schema <schema-name>` only for an explicitly selected schema). Never create a new change directory manually.
4. Run `openspec status --change "<name>" --json`. Use its `planningHome`, `changeRoot`, `artifactPaths`, `actionContext`, `applyRequires` and each artifact's `requires` edges. The required set is `applyRequires` plus all transitive prerequisites, even when an artifact already reads `done`; existence alone does not prove dependency completion.
5. Process required artifacts in dependency order. For each missing artifact, get `openspec instructions <artifact-id> --change "<name>" --json`. Read its `instruction`, `template`, `context`, `rules`, `dependencies`, and output paths. Apply constraints without copying them into artifacts. If instructions delegate to a skill or command, use it and verify its output.
6. Check dependency files on disk for changes before drafting. Reuse previously read sections only when their content identity still matches and their content is still available in this conversation. Load each artifact's relevant dependency sections plus shared invariants, following the context policy; avoid unconditional full-file rereads between artifacts.
7. Follow the returned template and instruction. Write to `resolvedOutputPath`; for a glob, choose a concrete path as instructed and preserve existing capability subpaths. Verify the created file exists, then refresh status.
8. Treat CLI `skipped` artifacts as satisfied and do not create their files. Otherwise skip only when the artifact's own instruction explicitly makes it conditional and the condition does not apply. Record the reason, tell the user and do not reconsider without new relevant evidence. Specs are not optional merely because a change seems small. If the sole missing dependencies are deliberate conditional skips, request the dependent artifact's instructions despite `blocked` and create it; do not bypass other missing prerequisites.
9. Continue until the entire required set is done, skipped or deliberately conditionally skipped; leave artifacts outside that set alone. Run `openspec validate "<name>" --type change --strict --no-interactive`. Report created artifacts, deliberate skips, validation results and any missing evidence. Planning completion is not implemented behavior.

## Keep changes focused

Prefer one independently verifiable outcome per change. Split unrelated deliverables before scaffolding; keep coupled migrations and contracts together. Do not split or narrow an existing approved change without authorization.

For new tasks, include stable task IDs, prerequisite task IDs where needed, links to the relevant complete Requirement/design sections, and the verification command or observable result. Link authoritative project facts rather than reproducing them. This makes later implementation task-scoped without removing failure scenarios or acceptance gates.

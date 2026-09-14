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

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Create the planning artifacts for the requested change. Implementation requires user authorization; a planning request alone does not supply it.

1. Ground the scope and acceptance criteria in relevant project evidence. Use the configured schema unless the user selects another. For schema discovery, use `openspec context --json`, then `openspec schemas --json` at the returned root; fall back to the current directory only for `no_openspec_root`.
2. Scaffold with `openspec new change "<name>"` (add `--schema` only when selected). Resolve an existing-name collision before writing.
3. Run `openspec status --change "<name>" --json`. The required set is `applyRequires` plus its transitive `requires` dependencies, including dependencies of artifacts already marked `done`.
4. For each missing required artifact, run `openspec instructions <artifact-id> --change "<name>" --json`. Follow its instruction/template, relevant dependency content and any delegated generator; write a concrete output path and refresh status.
5. Skip only CLI-declared skips or when that artifact's own instruction makes it conditional and the condition does not apply. Record the reason. A dependent artifact may proceed despite `blocked` only when deliberate conditional skips are its sole missing prerequisites.
6. Finish when the entire required set is done or validly skipped, then run `openspec validate "<name>" --type change --strict --no-interactive`. Report artifacts and any unresolved validation; do not claim implementation.

Prefer an independently verifiable outcome per new change. Give tasks stable IDs, prerequisite references, related Requirement/design links and observable verification; do not split existing approved scope silently.

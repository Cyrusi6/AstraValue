---
name: openspec-archive-change
description: Archive a completed change in the experimental workflow. Use when the user wants to finalize and archive a change after implementation is complete.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Archive the selected change through the CLI, which handles spec updates and moving the change.

1. Run `openspec status --change "<name>" --json`. Check required artifacts, all tasks and applicable project acceptance evidence before archiving; resolve incomplete gates rather than treating file existence as completion.
2. Read the effective project's archive guidance. Compare every delta from `artifactPaths.specs.existingOutputPaths` with its main spec under `<planningHome.root>/openspec/specs/`; identify pending updates/conflicts before moving.
3. Run `openspec validate "<name>" --type change --strict --no-interactive`, then `openspec archive "<name>"` at the selected root. Do not bypass validation. Use `--skip-specs` only for an explicit skip choice or an already-verified sync; report which applies.
4. If a separate semantic merge is needed, use the sync workflow and verify every selected capability before archiving. Never archive while sync is in progress or failed.
5. Verify the resulting archive path and spec state. Report actual synchronization and remaining external/manual status; archive success is not evidence that those gates passed.

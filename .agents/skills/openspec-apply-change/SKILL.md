---
name: openspec-apply-change
description: Implement tasks from an OpenSpec change. Use when the user wants to start implementing, continue implementation, or work through tasks.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Implement the selected change within the user's authorized task scope.

1. Resolve the change from the request/project entry; use `openspec list --json` if ambiguous.
2. Run `openspec status --change "<name>" --json` and `openspec instructions apply --change "<name>" --json`. Follow their schema, paths and instructions. Missing prerequisites block implementation; `all_done` describes task state, not external acceptance.
3. Use `contextFiles` to locate the current task's complete requirements, design and prerequisites. It is a file index, not a full-file reading mandate.
4. Implement and check each task before marking it complete. Continue through the authorized scope; reading in groups does not limit execution to one group.
5. Report completed work, observed checks and outstanding work. Do not archive as a side effect.

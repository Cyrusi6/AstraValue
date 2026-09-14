---
name: openspec-sync-specs
description: Sync delta specs from a change to main specs. Use when the user wants to update main specs with changes from a delta spec, without archiving the change.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Merge delta specs into main specs without archiving the change.

1. Run `openspec status --change "<name>" --json`. Select only `artifactPaths.specs.existingOutputPaths`; honor an explicit subset exactly. Missing/empty selection is a no-op; reject unknown paths.
2. Main specs are under `<planningHome.root>/openspec/specs/<capability-path>/spec.md`; preserve the full capability subpath.
3. Before writing, obtain valid `openspec instructions specs --change "<name>" --json`, or reuse an inline caller's current snapshot. Lookup/JSON failure blocks writes; omitted rules in valid JSON mean no configured rules.
4. Read each selected delta and the affected main-spec content. Merge ADDED/MODIFIED/REMOVED/RENAMED requirements idempotently, preserving unmentioned scenarios and order. Read the whole file before rewriting it. Before removing its last requirement, follow [retirement rules](references/retirement.md).
5. Preserve an existing Purpose. A new main spec uses the delta Purpose (report a TBD if absent) and one `## Requirements` section, with no delta operation headings.
6. Run `openspec validate --specs --strict --no-interactive`; report actual changes and failures. An inline caller must wait for sync completion before archiving.

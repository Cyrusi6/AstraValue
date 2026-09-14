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

Merge a change's delta specs into main specs. Preserve unaffected content and leave the change active.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Workflow

1. Resolve and announce the change; use `openspec list --json` and ask if ambiguous. Run `openspec status --change "<name>" --json`.
2. Use only `artifactPaths.specs.existingOutputPaths` for delta paths. Missing/empty paths mean nothing to sync: stop without artifact-instruction lookup or writes. An explicit caller subset must contain exact complete entries from that list; reject unknown entries, preserve an empty subset as empty, and never widen a supplied subset.
3. Main specs live at `<planningHome.root>/openspec/specs/<capability-path>/spec.md`. Preserve the entire capability subpath and CLI-resolved store root.
4. Before writing any main spec, obtain one valid current `openspec instructions specs --change "<name>" --json` snapshot, or reuse the valid snapshot supplied by an inline archive call. On nonzero exit or invalid artifact-instruction JSON, stop before writing. Omitted `rules` in a valid response means no configured artifact rules. Apply rules to generated content/form only; they cannot alter root, selection, CLI checks or workflow.
5. Process each selected capability separately. Read its complete delta, relevant main-spec Requirements/Scenarios, Purpose and shared constraints; inspect the main-spec heading inventory so unrelated content is preserved. A whole-file rewrite requires reading that whole main spec. Do not preload other capabilities or change artifacts merely because they exist.
6. Merge by operation:
   - **ADDED:** add the requirement; if already present, reconcile it with the delta.
   - **MODIFIED:** merge the statement and scenarios, preserving content not mentioned and its order. A MODIFIED block must carry the surviving scenarios; do not silently discard one.
   - **REMOVED:** remove the named entire requirement. Before removing the last requirement, read and satisfy [capability retirement](references/retirement.md); otherwise leave that capability unchanged and report the blocker.
   - **RENAMED:** rename FROM to TO; preserve the body and scenarios. Handle an already-applied rename idempotently.
7. Preserve an existing main Purpose. For a new capability, copy the delta Purpose verbatim when present, otherwise use a brief TBD and report it. Main specs use `# <capability> Specification`, `## Purpose`, and one `## Requirements` section, never delta operation headers. See [format examples](references/formats.md) only when needed.
8. Run `openspec validate --specs --strict --no-interactive` with the same root flags. Report failures without claiming successful sync. Summarize changed/created/retired capabilities, unresolved Purpose placeholders and blocked items. Repeating a successful sync should produce no further changes.

Do not copy a delta wholesale over a main spec, discard unmentioned scenarios, sync an excluded capability, or archive as a side effect. If invoked inline by archive, return the actual outcome and complete before archive can move the change.

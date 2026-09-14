---
name: openspec-update-change
description: Update an OpenSpec change by revising its existing planning artifacts and keeping them coherent with one another. Use when the user wants to revise a change's plan, fold new decisions into it, or reconcile its artifacts after an edit. Never edits code.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Revise existing planning artifacts and keep their affected dependencies coherent. This workflow does not edit implementation code or create missing schema artifacts.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Workflow

1. Resolve and announce the change. Use the named change or clear conversation context; if ambiguous, use `openspec list --json` and ask.
2. Run `openspec status --change "<name>" --json`. Use schema-provided artifact IDs, `planningHome`, `changeRoot`, `actionContext`, and `artifactPaths.<id>.existingOutputPaths`. Edit only those concrete existing files, never a glob `resolvedOutputPath` or an invented artifact.
3. For a specific requested revision, read scope, shared invariants and affected sections first. Search all existing artifact headings/references for impacted contracts, then follow dependencies in either direction: a task/design change can affect an earlier proposal/spec. Read complete related Requirements/Scenarios; do not require every unrelated file body.
4. If the request is a whole-change coherence review, cover all existing artifacts, reading by groups and tracking coverage. Do not claim a full reconciliation from a targeted review. If nothing needs changing, make no edits.
5. Show the concrete revisions and reasons. Apply changes already covered by explicit authorization; obtain approval for proposed revisions outside that scope and respect rejected edits. For a substantial rewrite, obtain the current `openspec instructions <artifact-id> --change "<name>" --json` and follow its rules/template first.
6. Recheck affected references across existing artifacts and run `openspec validate "<name>" --type change --strict --no-interactive`. Do not repair a validation failure by inventing missing artifacts or silently widening the approved scope.
7. Report revised files, rejected/deferred revisions, validation results and remaining inconsistencies. If code no longer matches, explain the needed implementation delta; do not apply it as a side effect. Checked tasks alone do not establish implementation or acceptance.

For missing artifacts, use status and `openspec instructions <artifact-id> --change "<name>" --json` to explain creation; only recommend an optional continue/new skill after verifying it is installed. A changed intent may warrant a distinct change: explain that choice rather than silently replacing the existing scope.

If a derived `context.md` exists, changed source fingerprints invalidate its affected entries automatically. Do not trust or rewrite it as an authoritative artifact, and do not generate a second decision log.

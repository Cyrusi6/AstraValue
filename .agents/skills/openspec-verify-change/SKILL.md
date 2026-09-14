---
name: openspec-verify-change
description: Verify implementation matches change artifacts. Use when the user wants to validate that implementation is complete, correct, and coherent before archiving.
allowed-tools: Bash(openspec:*)
license: MIT
compatibility: Requires openspec CLI.
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.12.0"
---

Verify implementation against the selected change's complete contract. This is an advisory review; it does not archive, implement fixes, or prove external acceptance.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Workflow

1. Resolve and announce the change; use `openspec list --json` and ask if ambiguous.
2. Run `openspec status --change "<name>" --json` and `openspec instructions apply --change "<name>" --json`. Resolve schema artifacts from `contextFiles`/`artifactPaths`, and apply relevant runtime context without treating state or guidance as evidence.
3. Inventory all tasks, delta Requirements/Scenarios and design decisions before reviewing. Read scope and shared invariants first, then process the complete contract in related groups. Read all relevant original text across those groups; indexing, summaries and a task-only implementation reading set do not satisfy whole-change verification. Include applicable main-spec requirements referenced by a delta.
4. Track a compact coverage ledger: task/Requirement/Scenario or decision → implementation evidence → tests/observations → finding/unverified. Reuse unchanged sections already available in context; reopen source after compaction or source changes.
5. **Completeness:** count every checked/unchecked task; map every requirement to actual implementation. Incomplete tasks and confirmed missing implementations are CRITICAL for completion. Verify before suggesting that an unchecked task may be marked complete.
6. **Correctness:** examine implementation against every scenario, including failure/degradation cases; inspect relevant tests and observed results. Report spec divergence and uncovered scenarios with file/line evidence. Keyword matches locate evidence but do not prove behavior.
7. **Coherence:** verify affected architecture, design decisions and compatibility constraints against code. Report material inconsistencies rather than cosmetic preferences.
8. Run strict OpenSpec validation and the checks required by the project's applicable gates. Reuse existing run evidence only when its revision/configuration/input scope matches the reviewed implementation; otherwise rerun the necessary checks or explicitly mark them unverified. Never infer real online or manual golden-sample success from tests or checkboxes.
9. Report coverage counts, CRITICAL/WARNING/SUGGESTION findings with actionable evidence, actual checks and remaining gates. A partial review must name its exclusions and must not claim the whole change is verified or ready for archive.

When an artifact is absent, verify the available dimensions and identify what could not be checked. Missing required artifacts remain a completeness gap; a permitted conditional skip is different. Explain uncertain findings as uncertainty instead of inventing implementation evidence.

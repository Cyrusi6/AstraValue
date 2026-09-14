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

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Review implementation against the complete selected contract. This is advisory verification, not automatic repair or archive.

1. Resolve the change; run `openspec status --change "<name>" --json` and `openspec instructions apply --change "<name>" --json` for current paths and context.
2. Inventory every task, delta Requirement/Scenario and design decision, including referenced main-spec obligations. Review them in groups against code and test/run evidence; keyword matches only locate evidence.
3. Check completeness, behavioral correctness and design consistency. Missing required artifacts or implementations remain gaps; checkboxes do not prove behavior. Track coverage so no group disappears from the review.
4. Run strict OpenSpec validation and applicable project checks; existing evidence is reusable only for the same revision and relevant configuration/inputs.
5. Report actionable findings with source locations, actual coverage and unverified gates. A partial review or missing external acceptance cannot justify whole-change completion.

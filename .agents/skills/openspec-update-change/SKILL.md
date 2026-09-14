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

Use the project's [context rules](../../../docs/openspec_context.md); reuse them while unchanged.

Revise existing planning artifacts; do not implement code or create missing schema artifacts.

1. Resolve the change and run `openspec status --change "<name>" --json`. Only edit concrete files in `artifactPaths.<id>.existingOutputPaths`, never a glob output path.
2. Read the requested sections and follow affected references in both directions across artifacts. A whole-change coherence review must cover all artifacts; a targeted revision need not preload unrelated bodies.
3. Apply the authorized revisions. For a substantial rewrite obtain `openspec instructions <artifact-id> --change "<name>" --json` first. Missing artifacts require a separate creation step, not invented paths.
4. Run `openspec validate "<name>" --type change --strict --no-interactive`; report changed artifacts, remaining inconsistencies and any resulting implementation delta.

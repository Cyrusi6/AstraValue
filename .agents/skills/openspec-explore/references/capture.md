# Capture authorized exploration

Read only when the user requests or authorizes writing named planning artifacts. Capture only that scope; ordinary discussion remains read-only. Use the selected store flags on supported commands.

1. For a new change, run `openspec new change "<name>"` before writing artifacts. This creates `.openspec.yaml`; never scaffold a change directory manually. If the user asked only to start a change, stop after scaffolding and showing status.
2. Run `openspec status --change "<name>" --json`. Resolve paths and constraints from `changeRoot`, `artifactPaths`, `actionContext` and the schema's dependency edges.
3. Process requested artifacts in dependency order. Get `openspec instructions "<artifact-id>" --change "<name>" --json` for each, then follow `instruction`, `template`, `context`, `rules`, `dependencies` and the concrete output path. Use the project context policy to read complete relevant dependency sections and shared constraints; reuse unchanged content only while available in context.
4. A CLI `skipped` artifact must not be created. Otherwise record a conditional skip only when its own instruction says the artifact is conditional and the condition does not apply. Tell the user and do not reconsider without new relevant evidence.
5. For an unrequested missing prerequisite, get its instructions even if blocked. Deliberately skip only under the preceding conditional rule; otherwise ask before expanding capture to it. Do not create an unrequested unconditional prerequisite.
6. If a requested artifact is blocked solely by recorded conditional skips, obtain its instructions and create it anyway. Any other missing prerequisite remains a blocker.
7. If instructions delegate to another skill/command, invoke it; otherwise write the artifact to `resolvedOutputPath`, choosing a concrete path for a glob as instructed. Preserve the full existing capability subpath. Verify the actual file exists.
8. Refresh status after each write. Stop when all requested artifacts are done/skipped/deliberately conditionally skipped; report missing dependencies and skips honestly. Run strict validation when the requested capture forms a complete change; for partial capture report its limited status rather than claiming implementation readiness.

For existing artifacts, use their resolved concrete paths. Put a behavioral requirement in the relevant spec, a design decision in design, a scope decision in proposal, and an implementation item in tasks when those artifact roles exist in the schema. Do not duplicate the project's methodology or execution log.

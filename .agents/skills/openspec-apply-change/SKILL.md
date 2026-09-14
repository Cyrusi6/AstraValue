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

Implement the approved change, using the current task and its dependencies to select context.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Workflow

1. Resolve the change from the user's name or clear conversation context. If ambiguous, use `openspec list --json` and ask which change. Announce the selected change and any user-selected task range.
2. Run `openspec status --change "<name>" --json`, then `openspec instructions apply --change "<name>" --json`. Use `schemaName`, `planningHome`, `changeRoot`, `actionContext`, and the schema's concrete paths; do not assume artifact names.
3. Inspect the returned state, built-in `instruction`, required `context`, and every `operationGuidance` entry. Guidance is advisory; none of these fields proves completion or overrides user choices, resolved paths, CLI state, or project acceptance criteria. Report material conflicts without copying the inputs into artifacts.
4. If `blocked`, report the missing prerequisites and use status plus `openspec instructions <artifact-id> --change "<name>" --json` to explain the next step. Never bypass the blocked state. If `all_done`, report task status and outstanding acceptance evidence; do not automatically archive.
5. Treat `contextFiles` as a candidate-file index, not an instruction to load every file in full. Read the task inventory (IDs/status/groups; expand current and prerequisite items), scope/non-goals, project/change-wide invariants and acceptance gates first. Select the current task, its prerequisite tasks, complete related Requirements/Scenarios, design sections and relevant code/tests using the context policy. Follow references until their dependencies are understood. Do not omit applicable obligations to fit a token target.
6. For each pending task in the authorized scope: implement its specified behavior, perform the appropriate checks, and update its checkbox only when fully complete. Reuse already-read unchanged sections. Before moving to a different task group, refresh source identity and load its dependency closure. A reading batch does not narrow the user's execution scope: if no subset was requested, keep advancing until the change is complete or a real blocker remains.
7. Resolve routine failures when possible. Ask only for missing material decisions, new scope, or required external input. Never silently drop, defer, or weaken behavior to finish. Preserve unrelated user changes and ignored data; do not force-add ignored files.
8. Report completed tasks, actual checks, remaining work and blockers. Automated tests, real online samples and manual golden-sample acceptance remain separate. Planning status, checked tasks and AI review are not substitutes for evidence.

## Resuming and handing off

For an implementation session that will continue later, maintain one short `context.md` under the CLI-resolved `changeRoot`. Read [the handoff format](references/handoff.md) only when creating or consuming that record. This is a derived navigation aid, not a schema artifact, a replacement task list, or a new acceptance gate. Do not create records for unrelated changes or pure discussion.

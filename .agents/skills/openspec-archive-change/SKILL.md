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

Archive a completed change after checking its artifacts, tasks, evidence and main-spec sync state.

**Root:** Use the CLI-resolved root and paths. If a registered store is named or selected, discover it with `openspec store list --json` and keep `--store <id>` on all supported spec/change commands below. Do not add that flag to commands that do not support it.

Read the project [context policy](../../../docs/openspec_context.md) when selecting context or resuming work; reuse it in this conversation while unchanged.

## Workflow

1. Resolve and announce an active change; use `openspec list --json` and ask if ambiguous. Run `openspec status --change "<name>" --json`; use `planningHome`, `changeRoot`, `artifactPaths` and `actionContext`.
2. Try `openspec instructions archive --change "<name>" --json` once. This optional advisory lookup may be unsupported: on failure/invalid JSON continue without its extra inputs. On success, consider required `context` and every advisory `operationGuidance` entry. Neither replaces CLI checks, root paths, user choices or project gates; report material conflicts and do not copy these fields into artifacts.
3. Read artifact statuses, the full task inventory and applicable acceptance evidence. Check the project's strict validation and automated gates, plus the separately recorded real-data/manual status. Resolve incomplete artifacts/tasks according to controlling project rules and explicit user decisions; do not silently waive a gate or equate checked tasks with evidence.
4. Use only `artifactPaths.specs.existingOutputPaths` for deltas. If absent/empty, continue without a sync prompt. Otherwise compare every delta against its main spec at `<planningHome.root>/openspec/specs/<capability-path>/spec.md`, preserving full capability subpaths. Read by capability; do not preload unrelated changes or archives.
5. Show the combined sync assessment. Honor an existing explicit sync/skip choice; if not decided, ask whether to sync before archiving (or archive now when already synced). Cancel means stop; an unclear reply is not authorization to move.
6. If syncing, first obtain a valid `openspec instructions specs --change "<name>" --json` snapshot. Nonzero exit/invalid artifact-instruction JSON stops all writes and the archive. Run the available `openspec-sync-specs` workflow inline, passing the delta analysis and that snapshot for reuse. Never move while sync is in progress.
7. After sync, compare EVERY original delta capability again, including untouched ones: added requirements present; modifications applied with other scenarios intact; removals absent; renames present under TO and absent under FROM. A retired capability must be deleted under the retirement rules, not left with an empty Requirements section. Any failed sync, blocked retirement or mismatch stops the archive.
8. Run `openspec validate "<name>" --type change --strict --no-interactive` and confirm applicable project gates. Before moving, resolve the absolute source and destination inside `planningHome.changesDir` and check neither follows a link outside it. Target `archive/<name>` if the name already has a `YYYY-MM-DD-` prefix, otherwise `archive/YYYY-MM-DD-<name>` using the current date. Stop if the destination exists; never overwrite or stack date prefixes.
9. Create the archive parent if needed, then move the complete `changeRoot`, including `.openspec.yaml`. On Windows use native `New-Item`/`Move-Item -LiteralPath` in one shell after path checks. Confirm the source moved and destination exists.
10. Report archive location, actual sync outcome, validation and any explicitly unresolved acceptance status. Say specs were synced only when the comparison passed. Update the project stage log only with observed checks, never invented online/manual success.

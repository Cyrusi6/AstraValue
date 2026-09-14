# Implementation handoff

Read when creating or consuming a continuation record, not on every short task.

Use one `context.md` inside the CLI-resolved `changeRoot`, only for the implementation work actually in progress. Aim for about 40–80 lines; this is a size guide, never a reason to omit a blocker or relevant invariant. Avoid copying completed tasks, long logs or whole requirements. Update in place instead of appending history.

Record verified facts only. A checkbox is not a test result; a planned check is not a successful run. Preserve unknown, pending and failed states. Link evidence rather than promoting it into a new authoritative log.

## Suggested fields

```markdown
# Continuation context (derived; source wins)

- Change / schema / resolved root:
- Worktree / branch / HEAD / relevant dirty files:
- Recorded at:
- Current task IDs and their prerequisites:
- Authorized scope and next action:

## Required reading
- Global constraints: source path + section/Requirement name
- Current task contract: source path + complete Requirement/Scenario
- Design and code/test entry points: path + section/symbol
- Approved decisions, assumptions and blockers: distinguish each

## Source identity
| Path | SHA-256 at reading | Sections already read |
| --- | --- | --- |

## Verified state
- Completed work and original task/evidence links:
- Checks actually run: command, outcome, source/input scope, evidence path
- Automated / online / manual acceptance: separate statuses
- Unresolved work and required user/external input:
```

Include source identities for `.openspec.yaml`, the effective project config and artifact inventory, plus relevant authoritative docs/code/tests. Record all artifact paths and their hashes when practical; hashes are cheap and do not load their bodies. Never include credentials or raw research data.

On resume, refresh CLI root, schema, artifact paths and task status; compare the file set and hashes. Invalidate affected entries on any mismatch, including a changed worktree or uncommitted edit. Re-read current required source sections after a new session/compaction even when hashes match, then follow missing dependencies. Do not demand that the user approve an ordinary refresh or create a missing record before working.

The record is not a required OpenSpec artifact or proof of completion. If it conflicts with source, use current source, state the conflict if material, and refresh the record after resolving it.

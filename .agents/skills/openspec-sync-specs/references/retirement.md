# Retiring the last requirement

Read before removing the last requirement from a main spec. Inspect the entire current main spec and `.openspec.yaml`; a targeted excerpt is insufficient to establish safe retirement.

Delete `spec.md` only when ALL conditions hold:

1. Removing requirements in this run leaves no requirement blocks.
2. The rest is well formed, including an existing `## Purpose`.
3. The main spec was not already empty: if nothing was removed, change nothing.
4. Every other nonblank line belongs to the title, Purpose, Requirements header, or canonical requirement statements, scenarios or fenced examples. Additional sections block retirement.
5. The change metadata declares `retire_capabilities: true`.
6. The resolved real path is inside the real main-specs root. Do not follow a capability-directory symlink to delete an external file.

If any condition fails, leave that capability unchanged, identify the blocker and explain its resolution. Never write an empty `## Requirements` section. Plan the capability edit before writing so a blocked retirement does not leave partial removals behind.

When allowed, delete the file with a literal, verified path; remove its directory only if actually empty. Report the removed `spec.md` and its Purpose. Give checkout-scoped recovery guidance; a pasteable Git restore command is appropriate only for a tracked spec in the caller's checkout. An already-retired, absent capability is an idempotent no-op.

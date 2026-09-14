# Removing the last requirement

Read the entire main spec before retiring a capability. Delete `spec.md` only if:
- This run removes its last requirement; the file was not already empty.
- It is well formed with Purpose, and has no content beyond its title, Purpose, Requirements and canonical requirement/scenario/example blocks.
- The change declares `retire_capabilities: true`.
- The resolved real path stays inside the real specs root; do not follow an external symlink.

If any condition fails, leave that capability unchanged and report the blocker. Never leave an empty Requirements section. Remove the directory only if empty. Report the deleted spec and Purpose; already-retired absent specs are a no-op.

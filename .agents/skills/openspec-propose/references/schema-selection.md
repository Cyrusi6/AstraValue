# Explicit schema selection

Use the configured default without extra discovery unless the user requests another schema or asks to see available workflows.

For discovery, run `openspec context --json` from the current working directory, with `--store <id>` when a registered store was explicitly selected. Run `openspec schemas --json` from its returned `root.path`, preserving the explicit store flag. This also preserves roots selected by a local store pointer or global default store.

Only when context reports `no_openspec_root`, run `openspec schemas --json` from the current working directory instead. An invalid or unavailable store is not permission to silently select another root. Let the user choose; pass `--schema <schema-name>` to `openspec new change` only for the selected schema.

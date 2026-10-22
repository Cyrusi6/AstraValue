# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## AstraValue project labels

Use one or more area labels to identify the affected pipeline stage:

- `area:acquisition` — data acquisition and source access
- `area:materialization` — evidence normalization and structured facts
- `area:calculation` — indicators, valuation, and derived metrics
- `area:reporting` — report assembly and export
- `area:validation` — data, rendering, or acceptance checks

Use one scope label to distinguish the request type:

- `scope:research` — company research or evidence collection request
- `scope:engineering` — code, tooling, or workflow change

Every issue should still carry exactly one canonical category label (`bug` or `enhancement`) and one canonical state label from the table above.

## Issue intake fields

Research issues should state the security or company, cutoff date, reporting period, source scope, affected ES01–ES08 steps, evidence sufficiency, unknowns, and acceptance boundary.

Engineering issues should state the affected module, observed behavior, expected behavior, reproduction or evidence, validation command, and acceptance criteria.

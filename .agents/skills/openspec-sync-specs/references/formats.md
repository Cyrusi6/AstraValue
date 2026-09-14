# Delta and main-spec examples

Read when the distinction is needed. Use current schema instructions and rules for the actual change.

Delta:

```markdown
## ADDED Requirements

### Requirement: Traceable result
The system SHALL retain the source identity of each result.

#### Scenario: A result is exported
- **WHEN** a result is exported
- **THEN** its source identity is included
```

Main spec after merging:

```markdown
# result-export Specification

## Purpose
Preserve the provenance of exported results.

## Requirements

### Requirement: Traceable result
The system SHALL retain the source identity of each result.

#### Scenario: A result is exported
- **WHEN** a result is exported
- **THEN** its source identity is included
```

Other delta operations are `## MODIFIED Requirements`, `## REMOVED Requirements`, and `## RENAMED Requirements`. A rename uses:

```markdown
- FROM: `### Requirement: Old name`
- TO: `### Requirement: New name`
```

A MODIFIED block carries the surviving statement and scenarios. Main specs never retain these delta operation headers.

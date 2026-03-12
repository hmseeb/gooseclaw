# Learnings Schema

APPEND-ONLY log files. The agent appends entries. Never delete entries. Mark resolved with Status field.

## Rules

- Three files, one schema. Same format, different content types.
- Entries are APPEND ONLY. Never delete or modify past entries.
- Mark resolved entries with `Status: resolved` or `Status: promoted`.
- Review these before major tasks to avoid repeating past mistakes.

## Files

| File | Content | Entry prefix |
|------|---------|-------------|
| LEARNINGS.md | corrections, knowledge gaps, best practices discovered | LRN |
| ERRORS.md | command failures, API errors, unexpected behavior | ERR |
| FEATURE_REQUESTS.md | capabilities the user wanted that don't exist | FEAT |

## Entry Format

Entry ID: `TYPE-YYYYMMDD-XXX` (e.g. `LRN-20260312-001`)

### LEARNINGS.md entries

```markdown
## [LRN-YYYYMMDD-XXX] category

**Logged**: ISO-8601 timestamp
**Priority**: low | medium | high | critical
**Status**: pending | resolved | promoted
**Category**: correction | knowledge_gap | best_practice

### Summary
One-line description

### Details
Full context

### Action Taken
What was done about it
```

### ERRORS.md entries

```markdown
## [ERR-YYYYMMDD-XXX] component_name

**Logged**: ISO-8601 timestamp
**Priority**: low | medium | high | critical
**Status**: pending | resolved

### Summary
What failed

### Error
actual error output (in code block)

### Context
What was being attempted

### Resolution
How it was fixed (fill in when resolved)
```

### FEATURE_REQUESTS.md entries

```markdown
## [FEAT-YYYYMMDD-XXX] capability_name

**Logged**: ISO-8601 timestamp
**Priority**: low | medium | high
**Status**: pending | in_progress | resolved

### Requested
What the user wanted

### Context
Why they needed it

### Implementation
How it was built (fill in when resolved)
```

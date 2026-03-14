---
phase: 16-watcher-engine
plan: 01
subsystem: api
tags: [watchers, crud, template, string-template, json-persistence]

# Dependency graph
requires:
  - phase: 05-hardening
    provides: job engine CRUD pattern, JSON persistence, threading primitives
provides:
  - Watcher CRUD functions (create, delete, list, update)
  - JSON persistence (_load_watchers, _save_watchers)
  - Tier 1 passthrough template processing (_process_passthrough)
  - Dict flattening for nested webhook payloads
  - Double-brace {{var}} to ${var} syntax adapter
affects: [16-02, 16-03, watcher-api, webhook-routing, feed-polling]

# Tech tracking
tech-stack:
  added: [string.Template]
  patterns: [watcher CRUD mirroring job engine, atomic JSON persistence, template safe_substitute]

key-files:
  created: []
  modified: [docker/gateway.py, docker/test_gateway.py]

key-decisions:
  - "Watcher CRUD mirrors job engine pattern exactly (same _load/_save/_lock structure)"
  - "string.Template.safe_substitute used for passthrough: missing keys left as literals"
  - "Support both ${var} and {{var}} syntax via regex pre-processor"
  - "Flatten nested dicts with both leaf keys and full-path keys for template access"

patterns-established:
  - "Watcher data model: id, name, type, source, channel, smart, transform, prompt, enabled, created_at, poll_seconds, filter, headers, webhook_secret, last_hash, last_check, last_fired, fire_count, last_error"
  - "update_watcher allowed fields: name, enabled, transform, prompt, channel, filter, poll_seconds, headers, webhook_secret"

requirements-completed: [WATCH-01, WATCH-02, WATCH-03, WATCH-04]

# Metrics
duration: 3min
completed: 2026-03-14
---

# Phase 16 Plan 01: Watcher CRUD + Passthrough Summary

**Watcher CRUD with JSON persistence and tier-1 passthrough template processing using string.Template with nested dict flattening**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-14T16:26:43Z
- **Completed:** 2026-03-14T16:30:02Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Full watcher CRUD (create/delete/list/update) with thread-safe locking and atomic JSON persistence
- Passthrough template engine supporting ${var} and {{var}} syntax with nested payload flattening
- 21 new tests covering all CRUD operations and template processing edge cases

## Task Commits

Each task was committed atomically:

1. **Task 1: TDD watcher CRUD + persistence**
   - `4641011` (test) RED: failing tests for watcher CRUD
   - `33d7664` (feat) GREEN: implement watcher CRUD, persistence, list/update
2. **Task 2: TDD passthrough template processing**
   - `de1748f` (test) RED: failing tests for passthrough processing
   - `dadf6a8` (feat) GREEN: implement passthrough template processing

## Files Created/Modified
- `docker/gateway.py` - Watcher engine state, CRUD functions, persistence, passthrough processing
- `docker/test_gateway.py` - 21 new tests across 5 test classes

## Decisions Made
- Mirrored job engine pattern exactly for consistency and developer familiarity
- Used string.Template.safe_substitute to gracefully handle missing template keys
- Both {{var}} and ${var} syntax supported via simple regex pre-processor
- Flatten provides both leaf keys and full-path keys for maximum template flexibility

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CRUD foundation complete, ready for webhook routing (Plan 02) and feed polling (Plan 03)
- _process_passthrough ready for _fire_watcher integration
- _load_watchers/_save_watchers ready for engine loop integration

---
*Phase: 16-watcher-engine*
*Completed: 2026-03-14*

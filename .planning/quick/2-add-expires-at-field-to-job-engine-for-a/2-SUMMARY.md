---
phase: quick
plan: 2
subsystem: api
tags: [jobs, scheduling, expiry, gateway]

# Dependency graph
requires:
  - phase: none
    provides: existing job engine in gateway.py
provides:
  - expires_at field in job creation, update, engine loop, and list endpoints
  - auto-expiry of jobs past their expires_at timestamp
affects: [job-engine, api-jobs]

# Tech tracking
tech-stack:
  added: []
  patterns: [expiry-as-fired-one-shot reuse pattern]

key-files:
  created: []
  modified: [docker/gateway.py]

key-decisions:
  - "Expired jobs reuse the fired+prune pattern: set fired=True with last_status='expired', let existing 24h pruning clean them up"
  - "expires_at checked BEFORE enabled/fired checks in engine loop so disabled jobs can still expire"

patterns-established:
  - "Expiry pattern: treat expired jobs as fired one-shots, reuse existing pruning infrastructure"

requirements-completed: [QUICK-2]

# Metrics
duration: 1min
completed: 2026-03-13
---

# Quick Task 2: Add expires_at Field to Job Engine Summary

**expires_at field across create/update/engine-loop/list with auto-expiry via fired one-shot pattern**

## Performance

- **Duration:** 1 min
- **Started:** 2026-03-13T00:35:18Z
- **Completed:** 2026-03-13T00:36:45Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments
- Jobs can now auto-expire at a user-defined unix timestamp
- Expired jobs are marked fired with status "expired" and pruned within 24h by existing cleanup
- Full API support: create, update, list all handle expires_at with validation and human-readable enrichment

## Task Commits

Each task was committed atomically:

1. **Task 1: Add expires_at to job creation, update, engine loop, and API** - `ff87edd` (feat)

## Files Created/Modified
- `docker/gateway.py` - Added expires_at to create_job dict, update_job allowed set, handle_create_job validation + response enrichment, _job_engine_loop expiry check, handle_list_jobs enrichment, docstring update

## Decisions Made
- Expired jobs reuse the fired+prune pattern: set fired=True with last_status="expired", let existing 24h pruning clean them up. Zero new infrastructure needed.
- expires_at checked BEFORE enabled/fired checks in engine loop so even disabled jobs can still expire (prevents zombie jobs that are disabled but never cleaned up).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- expires_at is fully integrated and ready for use via the /api/jobs endpoints
- No blockers

---
*Phase: quick-2*
*Completed: 2026-03-13*

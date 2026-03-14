---
phase: quick
plan: 6
subsystem: gateway
tags: [commands, slash-commands, restart, session-management]

requires:
  - phase: 06
    provides: CommandRouter, SessionManager, ChannelState abstractions
provides:
  - /restart command that bounces goosed without wiping user sessions
affects: [gateway, commands]

tech-stack:
  added: []
  patterns: [command handler with ctx dict, threading.Thread daemon for engine restart]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py
    - identity/system.md

key-decisions:
  - "Restart kills relay and restarts engine but intentionally skips session pop"

patterns-established:
  - "Command handler pattern: _handle_cmd_X(ctx) with channel_state fallback to _telegram_state"

requirements-completed: [QUICK-6]

duration: 3min
completed: 2026-03-14
---

# Quick Task 6: Add /restart Command Summary

**/restart command bounces goosed engine via _restart_goose_and_prewarm without popping user session from SessionManager**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-14T21:56:27Z
- **Completed:** 2026-03-14T21:59:11Z
- **Tasks:** 2 (TDD red + green)
- **Files modified:** 3

## Accomplishments
- Added _handle_cmd_restart that kills active relay, restarts engine, preserves session
- Registered /restart on _command_router with description
- 4 new tests: restart behavior, session preservation contrast, router registration, telegram fallback
- Updated system.md user commands table

## Task Commits

Each task was committed atomically:

1. **Task 1: RED - Write failing tests** - `80c8f6f` (test)
2. **Task 2: GREEN - Implement handler, register, update docs** - `1ffefc7` (feat)

## Files Created/Modified
- `docker/gateway.py` - Added _handle_cmd_restart function and router registration
- `docker/test_gateway.py` - 4 new restart tests + updated known commands list
- `identity/system.md` - Added /restart row to user commands table

## Decisions Made
- Restart kills relay and restarts engine but intentionally skips session pop (that is /clear's job)
- Used identical pattern to _handle_cmd_clear minus the _session_manager.pop call

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed test assertion for Thread-based restart call**
- **Found during:** Task 2 (GREEN phase)
- **Issue:** Plan suggested patching _restart_goose_and_prewarm and asserting not_called, but Thread actually calls the target
- **Fix:** Patched threading.Thread instead, asserting target and daemon kwargs
- **Files modified:** docker/test_gateway.py
- **Verification:** All 4 restart tests pass
- **Committed in:** 1ffefc7

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor test implementation adjustment. No scope creep.

## Issues Encountered
None - pre-existing test failures (11) confirmed unrelated to our changes.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- /restart command fully functional and tested
- Users can now restart engine after adding MCP extensions without losing conversation history

---
*Quick Task: 6*
*Completed: 2026-03-14*

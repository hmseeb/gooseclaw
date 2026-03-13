---
phase: 10-multi-bot-lifecycle
plan: 01
subsystem: api
tags: [telegram, bot-lifecycle, hot-add, hot-remove, setup-json]

# Dependency graph
requires:
  - phase: 09-multi-bot-core
    provides: "BotInstance, BotManager, _bot_manager, notification handlers, session manager"
provides:
  - "POST /api/bots endpoint for hot-adding bots"
  - "DELETE /api/bots/<name> endpoint for hot-removing bots"
  - "unregister_notification_handler() for cleanup"
  - "Enhanced BotManager.remove_bot() with full cleanup (stop, sessions, notifications)"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns: ["hot-add/hot-remove API pattern with setup.json persistence"]

key-files:
  created: []
  modified:
    - "docker/gateway.py"
    - "docker/test_gateway.py"

key-decisions:
  - "Auth guard uses _check_local_or_auth (localhost skip, remote requires auth) same as job endpoints"
  - "Bot removal cascades: stop thread + clear sessions + unregister notifications"
  - "Duplicate bot name returns 409, duplicate token returns 409 via ValueError from add_bot"

patterns-established:
  - "Hot lifecycle API: validate input, mutate runtime state, persist to setup.json"

requirements-completed: [BOT-05, BOT-06]

# Metrics
duration: 4min
completed: 2026-03-13
---

# Phase 10 Plan 01: Hot-Add/Hot-Remove Bot API Summary

**POST /api/bots and DELETE /api/bots/<name> endpoints with full cleanup cascade and setup.json persistence**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-13T03:07:38Z
- **Completed:** 2026-03-13T03:11:48Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- POST /api/bots creates a BotInstance, starts its poll loop, and persists to setup.json bots array
- DELETE /api/bots/<name> stops the bot, clears sessions, unregisters notification handler, and removes from setup.json
- Enhanced BotManager.remove_bot() with full cleanup cascade (was just setting running=False)
- Added unregister_notification_handler() as inverse of register
- 17 new tests covering all lifecycle scenarios, auth, validation, persistence, and non-interference

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Failing tests for bot lifecycle API** - `ef3b0a2` (test)
2. **Task 2: GREEN -- Implement bot lifecycle API endpoints** - `4055f54` (feat)

## Files Created/Modified
- `docker/gateway.py` - Added unregister_notification_handler, enhanced remove_bot, handle_add_bot, handle_remove_bot, route wiring
- `docker/test_gateway.py` - TestBotLifecycleAPI class with 17 tests

## Decisions Made
- Auth guard uses _check_local_or_auth (same pattern as job endpoints, no rate limiting beyond auth)
- Bot removal cascades: stop() joins thread, clear_channel() wipes sessions, unregister removes notification handler
- Duplicate name checked explicitly before add_bot (returns 409), duplicate token caught via ValueError from add_bot
- Optional provider/model fields in POST body forwarded to setup.json bot entry

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed auth test mock pattern**
- **Found during:** Task 2
- **Issue:** MagicMock(spec=GatewayHandler) does not call real _check_local_or_auth, so auth tests passed incorrectly
- **Fix:** Wired real _check_local_or_auth via lambda on mock handler for auth tests
- **Files modified:** docker/test_gateway.py
- **Verification:** Auth tests correctly verify 401 on non-localhost without credentials
- **Committed in:** 4055f54 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor test fix for correct auth verification. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- v2.0 Multi-Channel & Multi-Bot milestone complete
- All 20 v2.0 requirements satisfied
- All 265 tests pass with zero regressions

---
## Self-Check: PASSED

All files found, all commits verified.

---
*Phase: 10-multi-bot-lifecycle*
*Completed: 2026-03-13*

---
phase: 09-multi-bot-core
plan: 02
subsystem: gateway
tags: [telegram, polling, bot-lifecycle, pairing, notifications, per-bot-state]

# Dependency graph
requires:
  - phase: 09-multi-bot-core/01
    provides: BotInstance class with name/token/channel_key/state/pair_code, BotManager
  - phase: 06-shared-infrastructure
    provides: SessionManager, ChannelState, CommandRouter
provides:
  - BotInstance._poll_loop (full port from _telegram_poll_loop with per-bot parameterization)
  - BotInstance.start/stop lifecycle (session load, notification registration, pair code gen, thread management)
  - BotInstance._do_message_relay (testable relay helper using self.state and self.channel_key)
  - BotInstance._check_pairing (per-bot pairing code check with consumption)
  - BotInstance._make_notify_handler (closure per bot for notification bus)
  - _add_pairing_to_config with platform parameter
  - get_paired_chat_ids with platform parameter for filtering
  - _get_session_id with channel parameter for session lookup
affects: [09-multi-bot-core/03, 10-multi-bot-lifecycle]

# Tech tracking
tech-stack:
  added: []
  patterns: [per-bot poll loop via self-parameterized methods, testable relay extraction]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "Extracted _do_message_relay and _check_pairing as testable helpers from poll loop body"
  - "All modified functions (get_paired_chat_ids, _add_pairing_to_config, _get_session_id) default to 'telegram' for backward compatibility"
  - "Existing _telegram_poll_loop and start_telegram_gateway left untouched (replaced in Plan 03)"

patterns-established:
  - "Per-bot method pattern: BotInstance methods use self.channel_key/self.state/self.token instead of module globals"
  - "Testable helper extraction: complex inner functions (_do_relay closure) become instance methods for direct unit testing"

requirements-completed: [BOT-02, BOT-03, BOT-04]

# Metrics
duration: 6min
completed: 2026-03-13
---

# Phase 9 Plan 2: Poll Loop Refactor Summary

**BotInstance._poll_loop with per-bot channel_key/state/pairing, plus parameterized helpers for platform-filtered pairing and per-channel sessions**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-13T02:45:19Z
- **Completed:** 2026-03-13T02:51:19Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Full port of _telegram_poll_loop into BotInstance._poll_loop with all 15+ "telegram" references replaced by self.channel_key
- BotInstance.start/stop lifecycle: loads sessions, registers notification handler, generates pair code, registers Telegram commands, manages poll thread
- _do_message_relay and _check_pairing extracted as testable instance methods from poll loop closures
- _add_pairing_to_config, get_paired_chat_ids, _get_session_id all parameterized with backward-compatible defaults

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- failing tests** - `a1f117a` (test)
2. **Task 2: GREEN -- implementation** - `f569133` (feat)

_TDD: tests written first, then implementation to pass them._

## Files Created/Modified
- `docker/gateway.py` - BotInstance with _poll_loop/start/stop/_do_message_relay/_check_pairing/_make_notify_handler/_register_commands; parameterized _add_pairing_to_config, get_paired_chat_ids, _get_session_id
- `docker/test_gateway.py` - 4 new test classes (TestBotPollLoop, TestBotStartStop, TestBotNotification, TestBotPairing) with 18 tests

## Decisions Made
- Extracted _do_message_relay and _check_pairing as instance methods rather than testing the full poll loop. This avoids needing to mock urllib/getUpdates and makes relay logic directly unit-testable.
- All modified helper functions default to "telegram" so existing callers (the old _telegram_poll_loop, start_telegram_gateway) work without changes.
- Left _telegram_poll_loop and start_telegram_gateway untouched. Plan 03 will wire BotManager.start_all() to replace them.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed slow start/stop tests due to poll loop sleep**
- **Found during:** Task 2 (GREEN)
- **Issue:** TestBotStartStop tests took 40s because bot.start() spawned real _poll_loop which hit time.sleep(5) on mock errors
- **Fix:** Mock _poll_loop with a lightweight loop that checks self.running every 10ms
- **Files modified:** docker/test_gateway.py
- **Verification:** Test suite runs in 0.16s instead of 40s
- **Committed in:** f569133 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Necessary for test performance. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- BotInstance has full poll loop capability, ready for Plan 03 to wire BotManager into startup/shutdown
- Plan 03 will: replace start_telegram_gateway with BotManager.start_all(), add API endpoints for bot status
- Existing _telegram_poll_loop still works as fallback during transition

---
*Phase: 09-multi-bot-core*
*Completed: 2026-03-13*

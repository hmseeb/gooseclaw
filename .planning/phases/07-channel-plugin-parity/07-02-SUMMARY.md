---
phase: 07-channel-plugin-parity
plan: 02
subsystem: gateway
tags: [channel-relay, concurrency, locks, typing-indicators, tdd]

# Dependency graph
requires:
  - phase: 07-channel-plugin-parity
    provides: ChannelRelay with command interception, active relay tracking, ChannelState with get_user_lock
provides:
  - Per-user lock acquisition in ChannelRelay.__call__ before relay
  - Busy message on lock contention ("Still thinking... send /stop to cancel.")
  - Typing indicator loop firing callback every 4s during relay
  - _load_channel passes typing callback from CHANNEL dict to ChannelRelay
affects: [07-03-PLAN, channel-plugins]

# Tech tracking
tech-stack:
  added: []
  patterns: [per-user lock with send_fn-aware timeout, typing loop with daemon thread and stop event, error-resilient typing callback]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "Lock timeout 2s with send_fn (sends busy message), 120s without (blocks since we can't notify user)"
  - "Typing loop uses daemon thread with 4s interval, stop event set in finally block"
  - "Typing callback receives original user_id (not user_key) for channel-specific formatting"
  - "Buggy typing callbacks caught silently with bare except to never crash relay"

patterns-established:
  - "Lock-before-relay: acquire per-user lock before any relay work, release in outermost finally"
  - "Typing loop pattern: daemon thread with stop Event, 4s wait interval, error-swallowing try/except"
  - "_load_channel validates typing callback is callable before passing to ChannelRelay"

requirements-completed: [CHAN-02, CHAN-06]

# Metrics
duration: 3min
completed: 2026-03-13
---

# Phase 7 Plan 02: Per-User Locks + Typing Indicators Summary

**Per-user concurrency locks with busy messages and typing indicator callback loop in ChannelRelay**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-13T02:07:04Z
- **Completed:** 2026-03-13T02:09:49Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- ChannelRelay acquires per-user lock before relay, preventing concurrent goose requests from same user (CHAN-02)
- Lock contention with send_fn sends "Still thinking... send /stop to cancel." and returns immediately
- Lock contention without send_fn blocks up to 120s (can't notify user)
- Typing indicator callback fires every 4s during relay via daemon thread (CHAN-06)
- Typing loop stops immediately when relay completes (stop event in finally block)
- Buggy typing callbacks caught silently, never crash the relay
- 12 new tests (6 lock, 6 typing), 163 total, all green

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Failing tests for per-user locks and typing indicators** - `f95e50e` (test)
2. **Task 2: GREEN -- Per-user lock + typing indicator implementation** - `87f3310` (feat)

## Files Created/Modified
- `docker/gateway.py` - ChannelRelay with per-user lock, typing loop, _load_channel typing_cb passthrough
- `docker/test_gateway.py` - TestChannelRelayLocks (6 tests), TestChannelRelayTyping (6 tests)

## Decisions Made
- Lock timeout varies by send_fn availability: 2s when we can tell the user "Still thinking", 120s when we can't
- Typing callback receives original user_id (not stringified user_key) so channel plugins can use their native ID format
- Typing loop interval is 4 seconds, matching Telegram's typing indicator cadence
- _load_channel validates typing is callable before passing to ChannelRelay, logs warning if not

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- ChannelRelay now has full concurrency safety (locks) and UX feedback (typing indicators)
- Ready for 07-03: custom command registration and dynamic channel validation
- All backward compatibility maintained (151 prior tests still pass)

## Self-Check: PASSED

All files found, all commits verified.

---
*Phase: 07-channel-plugin-parity*
*Completed: 2026-03-13*

---
phase: 07-channel-plugin-parity
plan: 01
subsystem: gateway
tags: [channel-relay, command-routing, cancellation, tdd]

# Dependency graph
requires:
  - phase: 06-shared-infrastructure-extraction
    provides: SessionManager, ChannelState, CommandRouter classes with module-level instances
provides:
  - Generalized command handlers using ctx["channel"] and ctx["channel_state"]
  - ChannelRelay command interception via _command_router.dispatch
  - ChannelRelay active relay tracking with sock_ref for /stop cancellation
  - Backward-compatible defaults for telegram (ctx falls back to _telegram_state and "telegram")
affects: [07-02-PLAN, 07-03-PLAN, channel-plugins]

# Tech tracking
tech-stack:
  added: []
  patterns: [ctx-based handler generalization, command interception before relay, active relay tracking with cancelled event]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "Command handlers use ctx.get('channel_state', _telegram_state) pattern for backward compat"
  - "_handle_cmd_compact now uses _session_manager.get(channel, chat_id) instead of _get_session_id which is telegram-specific"
  - "ChannelRelay uses _relay_to_goose_web directly (not _do_ws_relay) since it already dispatches to the right relay fn internally"
  - "_clear_chat() left intact for backward compat -- _handle_cmd_clear now uses inline generalized logic"

patterns-established:
  - "ctx.get pattern: handlers always do `state = ctx.get('channel_state', _telegram_state)` and `channel = ctx.get('channel', 'telegram')` for backward compat"
  - "Command interception: ChannelRelay checks is_command() before relay, dispatches via _command_router, returns '' for any slash command"
  - "Active relay tracking: sock_ref = [None, cancelled_event], set_active_relay before relay, pop_active_relay in finally block"

requirements-completed: [CHAN-01, CHAN-03]

# Metrics
duration: 3min
completed: 2026-03-13
---

# Phase 7 Plan 01: Generalize Command Handlers + ChannelRelay Command Interception Summary

**Generalized 4 command handlers to work on any channel via ctx dict, added command interception and active relay tracking to ChannelRelay for /stop cancellation**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-13T02:00:50Z
- **Completed:** 2026-03-13T02:04:12Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Command handlers (_handle_cmd_stop, _handle_cmd_clear, _handle_cmd_compact) now use ctx["channel"] and ctx["channel_state"] with backward-compatible defaults
- ChannelRelay intercepts /help, /stop, /clear, /compact before relaying to goose web
- ChannelRelay tracks active relays via its own ChannelState, enabling /stop to cancel in-flight requests
- 17 new tests covering generalization, command interception, and relay tracking (151 total, all green)

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Failing tests for generalized commands and ChannelRelay interception** - `b4b07a9` (test)
2. **Task 2: GREEN -- Generalize handlers and add command interception + relay tracking** - `a6a8677` (feat)

## Files Created/Modified
- `docker/gateway.py` - Generalized command handlers, ChannelRelay with command interception and active relay tracking
- `docker/test_gateway.py` - TestGeneralizedCommandHandlers (6 tests), TestChannelRelayCommands (7 tests), TestChannelRelayStop (4 tests)

## Decisions Made
- Command handlers use `ctx.get("channel_state", _telegram_state)` pattern so existing telegram callers that pass no channel_state continue to work unchanged
- `_handle_cmd_compact` now uses `_session_manager.get(channel, chat_id)` directly instead of `_get_session_id()` which was telegram-hardcoded. If no session exists, returns "No active session" error instead of trying to create one (channel plugins don't have prewarm infrastructure)
- `_clear_chat()` function left intact for backward compat (telegram poll loop may call it directly), but the /clear handler now uses inline generalized logic
- ChannelRelay uses `_relay_to_goose_web` directly instead of `_do_ws_relay`/`_do_ws_relay_streaming` since `_relay_to_goose_web` already dispatches to the right function internally and handles retry logic

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- ChannelRelay now has command interception and relay tracking, ready for per-user locks (07-02) and custom command registration (07-03)
- Telegram backward compat verified via existing 134 tests all passing
- _clear_chat() and _get_session_id() still exist for direct telegram usage but are no longer the only path

## Self-Check: PASSED

All files found, all commits verified.

---
*Phase: 07-channel-plugin-parity*
*Completed: 2026-03-13*

---
phase: 06-shared-infrastructure-extraction
plan: 03
subsystem: infra
tags: [session-management, command-routing, channel-state, refactoring, bug-fix]

# Dependency graph
requires:
  - phase: 06-01
    provides: SessionManager and ChannelState classes (TDD)
  - phase: 06-02
    provides: CommandRouter class (TDD)
provides:
  - Module-level _session_manager, _telegram_state, _command_router instances
  - Telegram globals fully replaced with shared abstractions
  - /clear scoping fix (INFRA-04) -- only removes requesting user's session
  - Command handler dispatch via CommandRouter instead of if/elif chain
  - ChannelRelay using SessionManager instead of own session dict
affects: [07-channel-plugins, multi-bot, multi-channel]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "SessionManager composite key (channel:user_id) for all session lookups"
    - "ChannelState for per-user locks and active relay tracking"
    - "CommandRouter register/dispatch for slash commands"
    - "Migration from old telegram_sessions.json to new sessions_telegram.json format"

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "Kept _save_telegram_sessions as wrapper around _session_manager._save for backward compat"
  - "Added old-to-new session file migration in _load_telegram_sessions"
  - "/clear scoping: only pop requesting user, goose web restart still invalidates all (documented limitation)"

patterns-established:
  - "All session access goes through _session_manager.get/set/pop(channel, user_id)"
  - "All relay tracking goes through _telegram_state.set_active_relay/pop_active_relay"
  - "All command handling goes through _command_router.dispatch(text, ctx)"

requirements-completed: [INFRA-03, INFRA-04]

# Metrics
duration: 7min
completed: 2026-03-13
---

# Phase 06 Plan 03: Wire Integration Summary

**Replaced 6 telegram globals with SessionManager/ChannelState/CommandRouter instances, fixed /clear to only remove requesting user's session (INFRA-04)**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-13T01:39:52Z
- **Completed:** 2026-03-13T01:46:25Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Removed all 6 telegram-specific module-level dicts and their locks
- Wired _session_manager, _telegram_state, _command_router as module-level instances
- Fixed INFRA-04 bug: _clear_chat now only removes the requesting user's session instead of clearing ALL sessions
- Extracted command handlers into standalone functions registered on CommandRouter
- Migrated ChannelRelay to use SessionManager, eliminating its own session dict
- All 134 tests pass (124 updated + 10 new)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create module-level instances and migrate all global references** - `1f0cf8c` (feat)
2. **Task 2: Update existing tests and add /clear scoping regression tests** - `0be567a` (test)

## Files Created/Modified
- `docker/gateway.py` - Replaced globals with class instances, extracted command handlers, fixed /clear scoping
- `docker/test_gateway.py` - Updated 10 tests to use new API, added 10 new tests (4 scoping + 6 no-globals)

## Decisions Made
- Kept `_save_telegram_sessions()` as a thin wrapper around `_session_manager._save("telegram")` to minimize blast radius for existing callers
- Added migration logic in `_load_telegram_sessions()` to convert old `telegram_sessions.json` to new `sessions_telegram.json` format on first load
- /clear still restarts goose web (invalidating all sessions), but other users' _session_manager entries remain and will auto-recover via retry logic in _relay_to_goose_web

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 06 (Shared Infrastructure Extraction) is now complete
- SessionManager, ChannelState, and CommandRouter are fully integrated
- Channel plugins can use ChannelRelay which delegates to SessionManager
- Ready for Phase 07 (channel plugin system) or multi-bot support

---
*Phase: 06-shared-infrastructure-extraction*
*Completed: 2026-03-13*

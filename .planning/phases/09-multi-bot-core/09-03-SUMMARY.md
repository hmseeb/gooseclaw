---
phase: 09-multi-bot-core
plan: 03
subsystem: gateway
tags: [telegram, multi-bot, wiring, api-endpoints, startup, shutdown, backward-compat]

# Dependency graph
requires:
  - phase: 09-multi-bot-core/01
    provides: BotInstance, BotManager classes, _resolve_bot_configs
  - phase: 09-multi-bot-core/02
    provides: BotInstance._poll_loop, start/stop lifecycle, _do_message_relay, _check_pairing
provides:
  - Module-level _bot_manager = BotManager() wired into apply_config, startup, shutdown
  - apply_config resolves bot configs and starts bots via BotManager
  - start_telegram_gateway as backward-compatible thin wrapper around BotManager
  - _is_goose_gateway_running delegates to _bot_manager.any_running
  - handle_telegram_status returns per-bot array + backward-compat top-level fields
  - handle_telegram_pair accepts optional bot= query parameter
  - Shutdown handler calls _bot_manager.stop_all()
affects: [10-multi-bot-lifecycle]

# Tech tracking
tech-stack:
  added: []
  patterns: [manager-wiring-into-existing-entrypoints, backward-compat-api-extension]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "apply_config calls _resolve_bot_configs then _bot_manager.add_bot for each config, with os.environ.setdefault for first token (backward compat)"
  - "start_telegram_gateway becomes thin wrapper: checks _bot_manager for existing default bot, delegates to add_bot + start"
  - "handle_telegram_status returns 'bots' array alongside existing top-level fields (running, bot_configured, paired_users, pairing_code) for admin.html backward compat"
  - "handle_telegram_pair accepts ?bot=name query param, defaults to 'default', returns 400 for unknown bots"
  - "Shutdown calls _bot_manager.stop_all() instead of _telegram_running = False, removing _remove_pid('telegram')"

patterns-established:
  - "BotManager wiring pattern: entrypoints (apply_config, startup, shutdown) go through _bot_manager, old functions become thin wrappers"
  - "API backward-compat pattern: add new structured data (bots array) alongside existing flat fields"

requirements-completed: [BOT-04, BOT-07]

# Metrics
duration: 5min
completed: 2026-03-13
---

# Phase 9 Plan 03: Wire BotManager into Startup, Shutdown, and API Endpoints Summary

**Module-level BotManager wired into apply_config/startup/shutdown with per-bot API endpoints and full backward compatibility for single-bot configs**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-13T02:54:50Z
- **Completed:** 2026-03-13T03:00:13Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Module-level _bot_manager instance created and wired into apply_config, startup, shutdown, _is_goose_gateway_running
- apply_config resolves multi-bot configs via _resolve_bot_configs and starts each via BotManager
- API /api/telegram/status returns per-bot "bots" array with name, running, channel_key, paired_users, pairing_code
- API /api/telegram/pair accepts optional ?bot=name parameter for named bot pairing
- Shutdown handler calls _bot_manager.stop_all() for clean multi-bot shutdown
- start_telegram_gateway remains as backward-compatible entry point for env-var-only deployments
- Default bot uses channel_key "telegram" for zero-migration backward compat
- 13 new tests (TestBotWiring: 7, TestBotAPIEndpoints: 5, TestBotShutdown: 1)

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Failing tests for BotManager wiring, API endpoints, shutdown** - `29f9cc8` (test)
2. **Task 2: GREEN -- Wire BotManager into apply_config, startup, shutdown, API endpoints** - `64a6722` (feat)

_TDD: tests written first, then implementation to pass them._

## Files Created/Modified
- `docker/gateway.py` - Module-level _bot_manager, updated apply_config, start_telegram_gateway (thin wrapper), _is_goose_gateway_running, shutdown, handle_telegram_status, handle_telegram_pair, main startup guard
- `docker/test_gateway.py` - 3 new test classes (TestBotWiring, TestBotAPIEndpoints, TestBotShutdown) with 13 tests

## Decisions Made
- apply_config uses os.environ.setdefault("TELEGRAM_BOT_TOKEN", token) so the first resolved bot's token wins for backward compat
- start_telegram_gateway checks _bot_manager.get_bot("default") before adding, making it idempotent
- handle_telegram_status preserves all top-level fields (running, bot_configured, paired_users, pairing_code) that admin.html reads
- handle_telegram_pair tries to auto-start default bot if not running (same behavior as before)
- Shutdown no longer calls _remove_pid("telegram") since BotManager handles bot lifecycle

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed env var pollution between apply_config tests**
- **Found during:** Task 2 (GREEN)
- **Issue:** apply_config's os.environ.setdefault set TELEGRAM_BOT_TOKEN, causing test_no_token_no_bots (from 09-01) to pick up the stale env var and fail
- **Fix:** Added os.environ.pop("TELEGRAM_BOT_TOKEN", None) in TestBotWiring.tearDown
- **Files modified:** docker/test_gateway.py
- **Verification:** Full 248-test suite passes
- **Committed in:** 64a6722 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Necessary for test isolation. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 9 complete: BotInstance, BotManager, poll loop, wiring all done
- Multi-bot works end-to-end: configure bots array in setup.json, each gets its own poll loop, sessions, pair codes
- Single-bot backward compat verified: telegram_bot_token config still works as "default" bot with channel_key "telegram"
- Ready for Phase 10 (hot-add/remove bots via API)
- All 248 tests pass (235 existing + 13 new), zero regressions

## Self-Check: PASSED

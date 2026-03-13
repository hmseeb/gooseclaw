---
phase: 09-multi-bot-core
plan: 01
subsystem: infra
tags: [telegram, multi-bot, threading, config-resolution, validation]

# Dependency graph
requires:
  - phase: 06-shared-infrastructure-extraction
    provides: SessionManager, ChannelState, CommandRouter classes
provides:
  - BotInstance class encapsulating per-bot runtime state
  - BotManager class with thread-safe bot registry
  - _resolve_bot_configs for backward-compatible config parsing
  - validate_setup_config extensions for bots array schema
  - _get_valid_channels extensions for bot-scoped channel keys
affects: [09-02-PLAN, 09-03-PLAN, 10-01-PLAN]

# Tech tracking
tech-stack:
  added: []
  patterns: [per-bot-state-encapsulation, thread-safe-registry, backward-compatible-config]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "BotInstance uses channel_key 'telegram:<name>' by default, 'telegram' for default bot (zero migration)"
  - "BotManager returns existing bot on duplicate name (idempotent), raises ValueError on duplicate token (safety)"
  - "_resolve_bot_configs falls back from bots array to telegram_bot_token env var chain"

patterns-established:
  - "Per-bot state isolation: each BotInstance owns its own ChannelState (locks, relays)"
  - "Config resolution chain: bots array > telegram_bot_token config > TELEGRAM_BOT_TOKEN env"

requirements-completed: [BOT-01, BOT-02, BOT-03, BOT-07]

# Metrics
duration: 4min
completed: 2026-03-13
---

# Phase 9 Plan 01: BotInstance + BotManager Summary

**BotInstance/BotManager classes with per-bot ChannelState isolation, backward-compatible config resolution, and bots array validation**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-13T02:38:29Z
- **Completed:** 2026-03-13T02:42:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- BotInstance class encapsulates per-bot state (name, token, channel_key, ChannelState, pair_code, running)
- BotManager class provides thread-safe registry with duplicate token rejection
- _resolve_bot_configs handles both bots array and legacy telegram_bot_token with env var fallback
- validate_setup_config extended with full bots array schema validation (name, token, duplicates)
- _get_valid_channels extended to include bot-scoped channel keys (telegram:<name>)
- 32 new tests covering all classes, config resolution, validation, and per-bot isolation

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Failing tests for BotInstance, BotManager, config resolution, validation, isolation** - `7b6f52b` (test)
2. **Task 2: GREEN -- Implement BotInstance, BotManager, _resolve_bot_configs, validation, valid channels** - `09596b2` (feat)

## Files Created/Modified
- `docker/gateway.py` - Added BotInstance, BotManager classes, _resolve_bot_configs, validation and channel key extensions
- `docker/test_gateway.py` - Added 6 test classes (32 tests): TestBotInstance, TestBotManager, TestResolveBotConfigs, TestBotConfigValidation, TestBotValidChannels, TestBotIsolation

## Decisions Made
- BotInstance uses channel_key "telegram:<name>" by default, "telegram" for the default bot (zero migration for existing single-bot setups)
- BotManager.add_bot returns existing bot on duplicate name (idempotent add) but raises ValueError on duplicate token (prevents accidental token reuse)
- _resolve_bot_configs falls back: bots array (non-empty) > telegram_bot_token (config) > TELEGRAM_BOT_TOKEN (env) > empty list
- _get_valid_channels loads setup.json at call time to dynamically include bot channel keys

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- BotInstance and BotManager ready for 09-02 (poll loop refactor into BotInstance)
- _resolve_bot_configs ready for 09-03 (wire into startup)
- All 217 tests pass (185 existing + 32 new), zero regressions

## Self-Check: PASSED

- FOUND: docker/gateway.py
- FOUND: docker/test_gateway.py
- FOUND: 09-01-SUMMARY.md
- FOUND: commit 7b6f52b (test RED)
- FOUND: commit 09596b2 (feat GREEN)

---
*Phase: 09-multi-bot-core*
*Completed: 2026-03-13*

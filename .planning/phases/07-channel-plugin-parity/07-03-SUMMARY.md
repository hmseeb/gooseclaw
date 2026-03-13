---
phase: 07-channel-plugin-parity
plan: 03
subsystem: gateway
tags: [channel-plugins, custom-commands, dynamic-validation, command-router, tdd]

# Dependency graph
requires:
  - phase: 07-channel-plugin-parity
    provides: CommandRouter with register/dispatch/is_command, ChannelRelay with command interception, _loaded_channels dict
provides:
  - Custom command registration from CHANNEL dict commands field during _load_channel
  - Conflict detection preventing plugin commands from overwriting built-in commands
  - _get_valid_channels() function returning dynamic set of fixed + loaded plugin names
  - All hardcoded valid_channels tuples replaced with dynamic validation
affects: [08-01-PLAN, channel-plugins, notification-bus]

# Tech tracking
tech-stack:
  added: []
  patterns: [CHANNEL dict commands field contract, dynamic channel validation via _get_valid_channels, conflict-detection-then-register pattern]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "Custom commands registered on global _command_router, not per-channel routers, for simplicity"
  - "Built-in commands always take priority: conflict detected via is_command() before register()"
  - "Also updated channel_verbosity validation in validate_setup_config (4th hardcoded location plan missed)"

patterns-established:
  - "CHANNEL dict commands field: {cmd_name: {handler: callable, description: str}} optional field"
  - "_get_valid_channels() pattern: always call function, never hardcode channel name lists"
  - "Conflict detection: check is_command before register, log warning and skip on conflict"

requirements-completed: [CHAN-04, CHAN-05]

# Metrics
duration: 5min
completed: 2026-03-13
---

# Phase 7 Plan 03: Custom Command Registration + Dynamic Channel Validation Summary

**CHANNEL dict commands field registers plugin commands on global router with conflict detection; _get_valid_channels() replaces all hardcoded channel name tuples**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-13T02:07:34Z
- **Completed:** 2026-03-13T02:12:42Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Channel plugins can now register custom commands via CHANNEL dict `commands` field (CHAN-04)
- Built-in commands (/help, /stop, /clear, /compact) protected from overwrite with conflict warning
- _get_valid_channels() replaces all 4 hardcoded valid_channels tuples with dynamic set (CHAN-05)
- 16 new tests covering registration, conflicts, dynamic validation, and setup config (179 total, all green)

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Failing tests for custom commands and dynamic validation** - `a3d536a` (test)
2. **Task 2: GREEN -- Implement custom command registration and _get_valid_channels** - `73c6179` (feat)

## Files Created/Modified
- `docker/gateway.py` - Added _get_valid_channels(), custom command registration in _load_channel, replaced 4 hardcoded tuples
- `docker/test_gateway.py` - TestCustomCommandRegistration (5 tests), TestCustomCommandConflicts (3 tests), TestDynamicChannelValidation (4 tests), TestValidateSetupDynamic (4 tests)

## Decisions Made
- Custom commands registered on the global _command_router (not per-channel routers) because the CommandRouter dispatch is already used by ChannelRelay command interception, and namespacing adds complexity without clear benefit at this stage
- Built-in commands always take priority via is_command() check before register(). First-registered-wins for plugin-vs-plugin conflicts.
- Updated a 4th hardcoded location (channel_verbosity validation in validate_setup_config line 1024) that the plan missed but was inconsistent

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Updated channel_verbosity validation in validate_setup_config**
- **Found during:** Task 2 (replacing hardcoded tuples)
- **Issue:** Plan identified 3 hardcoded valid_channels tuples, but channel_verbosity validation in validate_setup_config also had hardcoded `("web", "telegram")` check at line 1024
- **Fix:** Replaced with `valid_verb_channels = _get_valid_channels()` for consistency
- **Files modified:** docker/gateway.py
- **Verification:** All tests pass, grep confirms zero hardcoded tuples remain
- **Committed in:** 73c6179 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical)
**Impact on plan:** Essential for consistency. Without this fix, validate_setup_config would reject plugin channels in channel_verbosity while accepting them in channel_routes.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 7 (Channel Plugin Parity) is complete: all 3 plans done
- Channel plugins now have full parity with Telegram for commands, locks, typing, and custom commands
- Dynamic validation ensures new plugins are auto-recognized across routes and verbosity settings
- Ready for Phase 8 (Notification Channel Targeting)

## Self-Check: PASSED

All files found, all commits verified, 179 tests passing, 5 _get_valid_channels references, 0 hardcoded tuples.

---
*Phase: 07-channel-plugin-parity*
*Completed: 2026-03-13*

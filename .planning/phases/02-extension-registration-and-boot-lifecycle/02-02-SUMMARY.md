---
phase: 02-extension-registration-and-boot-lifecycle
plan: 02
subsystem: infra
tags: [yaml, config, goosed, entrypoint, boot-lifecycle, mcp]

requires:
  - phase: 02-extension-registration-and-boot-lifecycle
    provides: registry.py CRUD (register, list_extensions, get_config_entries)
  - phase: 01-template-engine-and-code-generation
    provides: generator.py (generate_extension function)
provides:
  - Config.yaml writer with thread-safe goose_lock and atomic writes
  - End-to-end registration orchestrator (generate + register + config + restart)
  - Boot loader in entrypoint.sh for registry-based extension injection
affects: [extension-management, detection-and-validation]

tech-stack:
  added: []
  patterns: [thread-safe-config-write, background-goosed-restart, boot-time-registry-injection]

key-files:
  created:
    - docker/tests/test_registration.py
  modified:
    - docker/gateway.py
    - docker/entrypoint.sh

key-decisions:
  - "Lazy imports in register_generated_extension() to avoid loading generator/registry until needed"
  - "Boot loader does NOT validate vault keys at boot (vault may not be ready, extension fails gracefully at runtime)"
  - "Background restart with 1s delay + session clear matches existing handle_save pattern"

patterns-established:
  - "Config.yaml writer pattern: goose_lock + yaml read-modify-write + atomic tmp+replace"
  - "Boot loader pattern: inline python in entrypoint.sh between restore and sync blocks"

requirements-completed: [REG-01, REG-03, REG-04]

duration: 8min
completed: 2026-04-01
---

# Plan 02-02: Config Wiring and Boot Lifecycle Summary

**Config.yaml thread-safe writer, end-to-end registration orchestrator, and registry-based boot loader in entrypoint.sh**

## Performance

- **Duration:** 8 min
- **Tasks:** 3
- **Files created:** 1
- **Files modified:** 2

## Accomplishments
- _register_extension_in_config() writes extensions to config.yaml thread-safely via goose_lock
- register_generated_extension() orchestrates full flow: generate -> registry -> config -> background restart
- Boot loader in entrypoint.sh reads registry.json and injects enabled extensions before goosed starts
- Boot loader skips disabled extensions and those with missing server.py
- 8 integration tests all passing, 27 total tests across all modules

## Task Commits

1. **Task 1: Config writer and registration flow in gateway.py** - `c730d21` (feat)
2. **Task 2: Registry boot loader in entrypoint.sh** - `29e05b5` (feat)
3. **Task 3: Integration tests** - `969be99` (test)

## Files Created/Modified
- `docker/gateway.py` - Added _register_extension_in_config() and register_generated_extension()
- `docker/entrypoint.sh` - Added registry.json boot loader between restore and sync blocks
- `docker/tests/test_registration.py` - 8 integration tests for config writer, boot loader, and full flow

## Decisions Made
- Lazy imports in register_generated_extension() to avoid circular imports and unnecessary module loading
- Boot loader skips vault key validation (vault may not be ready at boot, extensions fail gracefully at runtime)

## Deviations from Plan
None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Full extension lifecycle complete: generate -> register -> config -> restart -> boot persistence
- Ready for Phase 3 (Detection and Validation) which adds automatic vault credential detection

---
*Phase: 02-extension-registration-and-boot-lifecycle*
*Completed: 2026-04-01*

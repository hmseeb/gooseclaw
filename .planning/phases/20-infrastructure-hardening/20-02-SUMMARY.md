---
phase: 20-infrastructure-hardening
plan: 02
subsystem: infra
tags: [shutdown, watchdog, threading, gateway]

requires:
  - phase: 19-test-infrastructure
    provides: pytest framework and gateway_source fixture
provides:
  - Shutdown watchdog timer preventing container hang on Railway deploys
affects: [21-end-to-end-validation]

tech-stack:
  added: []
  patterns: [watchdog-timer-shutdown]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/tests/test_hardening.py

key-decisions:
  - "5-second timeout chosen to stay well under Railway's 10s SIGKILL deadline"
  - "os._exit(1) instead of sys.exit() because sys.exit raises SystemExit which can be caught"

patterns-established:
  - "Watchdog pattern: start daemon Timer before cleanup, cancel after success, force-exit on timeout"

requirements-completed: [HARD-03]

duration: 3min
completed: 2026-03-16
---

# Plan 20-02: Shutdown Watchdog Summary

**5-second threading.Timer watchdog wrapping gateway shutdown cleanup, force-killing via os._exit(1) if any step hangs**

## Performance

- **Duration:** 3 min
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Watchdog timer starts immediately in shutdown handler before any cleanup
- Force-exits process after 5 seconds if graceful cleanup hangs
- Daemon thread ensures watchdog doesn't prevent normal exit
- Watchdog cancelled on successful cleanup completion
- 4 tests validate the implementation

## Task Commits

1. **Task 1+2: Watchdog implementation and tests** - `0dd3822` (feat)

## Files Created/Modified
- `docker/gateway.py` - Shutdown handler now wraps all cleanup with watchdog timer
- `docker/tests/test_hardening.py` - 4 shutdown watchdog tests added

## Decisions Made
None - followed plan as specified

## Deviations from Plan
None - plan executed exactly as written

## Issues Encountered
None

## Next Phase Readiness
- Shutdown handler hardened, ready for JSON logging migration in plan 20-03

---
*Phase: 20-infrastructure-hardening*
*Completed: 2026-03-16*

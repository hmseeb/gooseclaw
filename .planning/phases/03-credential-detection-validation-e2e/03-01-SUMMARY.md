---
phase: 03-credential-detection-validation-e2e
plan: 01
subsystem: validation
tags: [ast, mcp, health-check, registry, subprocess]

requires:
  - phase: 02-extension-registration-and-boot-lifecycle
    provides: registry module with register/unregister/list_extensions and REGISTRY_PATH
provides:
  - validate_syntax() for checking generated .py files via ast.parse
  - health_check() for MCP JSON-RPC initialize verification
  - record_failure/clear_failures/check_and_disable for 3-strike auto-disable
affects: [03-credential-detection-validation-e2e]

tech-stack:
  added: []
  patterns: [subprocess-based MCP health check, registry failure counter integration]

key-files:
  created:
    - docker/extensions/validator.py
    - docker/tests/test_validator.py
  modified: []

key-decisions:
  - "Used select.select() for non-blocking stdout read with timeout instead of communicate(timeout=) to avoid stdin/stdout deadlock"
  - "record_failure returns 0 for nonexistent extensions rather than raising, matching graceful handling pattern"

patterns-established:
  - "Lazy imports for cross-module dependencies (import extensions.registry inside function body)"
  - "Subprocess health check pattern: spawn, send JSON-RPC, read response with timeout, always kill in finally"

requirements-completed: [VAL-01, VAL-02, VAL-03]

duration: 5min
completed: 2026-04-01
---

# Plan 03-01: Extension Validator Summary

**Syntax checking via ast.parse, MCP health check via JSON-RPC initialize, and 3-strike auto-disable via registry failure tracking**

## Performance

- **Duration:** 5 min
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- validate_syntax() catches SyntaxError and FileNotFoundError before extensions reach the registry
- health_check() spawns extension as subprocess, sends MCP initialize request, verifies valid JSON-RPC response within timeout
- record_failure/clear_failures/check_and_disable provide 3-strike auto-disable via registry integration
- 10 comprehensive unit tests covering all validation and failure tracking paths

## Task Commits

Each task was committed atomically:

1. **Task 1: Create validator.py** - `7de0f77` (feat)
2. **Task 2: Unit tests for validator module** - `9828c34` (test)

## Files Created/Modified
- `docker/extensions/validator.py` - Validation functions: syntax check, health check, failure tracking with auto-disable
- `docker/tests/test_validator.py` - 10 unit tests covering syntax validation, health check, and failure tracking

## Decisions Made
- Used select.select() for non-blocking stdout read with timeout for health check
- record_failure returns 0 for nonexistent extensions rather than raising

## Deviations from Plan
None - plan executed exactly as written

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Validator module ready for Plan 03-02 to wire into credential-to-extension pipeline
- validate_syntax() and health_check() ready to be called from gateway.py registration flow

---
*Phase: 03-credential-detection-validation-e2e*
*Completed: 2026-04-01*

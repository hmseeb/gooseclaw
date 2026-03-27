---
phase: 29-setup-wizard-dashboard-gating
plan: 01
subsystem: testing
tags: [pytest, tdd, vault, voice, gemini]

requires:
  - phase: 28-voice-infra
    provides: voice session token functions, _get_gemini_api_key
provides:
  - RED test suite for vault write (_save_vault_key)
  - RED test suite for /voice route gating (auth + key)
  - RED test suite for Gemini API key in setup config
affects: [29-02-PLAN, 29-03-PLAN]

tech-stack:
  added: []
  patterns: [VAULT_FILE patching for isolated vault tests, yaml fixture setup for HTTP gating tests]

key-files:
  created: []
  modified:
    - docker/tests/test_voice.py
    - docker/tests/test_setup.py

key-decisions:
  - "Used pytest.skip for _save_vault_key import guard instead of try/except to keep test discovery clean"
  - "Patched gateway.VAULT_FILE in autouse fixtures since it's computed at import time from DATA_DIR"

patterns-established:
  - "Vault test isolation: patch gateway.VAULT_FILE per-test via autouse fixture with tmp_path"
  - "HTTP gating tests: use live_gateway + auth_session fixtures with VAULT_FILE patch"

requirements-completed: [SETUP-01, SETUP-02, SETUP-04, UI-07]

duration: 5min
completed: 2026-03-27
---

# Plan 29-01: RED Tests Summary

**12 failing tests defining contracts for vault write, voice page gating, and Gemini key setup config**

## Performance

- **Duration:** 5 min
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- TestVaultWrite: 4 tests for _save_vault_key roundtrip, overwrite, preserve other keys, directory creation (all skip/RED)
- TestVoicePageGating: 3 tests for /voice auth redirect, gate page without key, page with key (all RED - route doesn't exist)
- TestGeminiKeyInSetup: 5 tests for save with/without key, config gemini_api_key_set true/false, reconfigure preservation (all RED)

## Task Commits

1. **Task 1+2: RED tests for all Phase 29 requirements** - `bc3fea7` (test)

## Files Created/Modified
- `docker/tests/test_voice.py` - Added TestVaultWrite (4 tests) and TestVoicePageGating (3 tests)
- `docker/tests/test_setup.py` - Added TestGeminiKeyInSetup (5 tests), added pytest and yaml imports

## Decisions Made
- Used pytest.skip guard for _save_vault_key since it doesn't exist yet (cleaner than try/except ImportError)
- Vault write tests skip cleanly; HTTP-level tests fail with wrong status codes (RED)

## Deviations from Plan
None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All 12 test contracts ready for Plans 02 and 03 to turn GREEN
- Tests define exact expected behavior for vault write, voice gating, and config integration

---
*Phase: 29-setup-wizard-dashboard-gating*
*Completed: 2026-03-27*

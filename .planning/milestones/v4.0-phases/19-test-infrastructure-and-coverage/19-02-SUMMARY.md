---
phase: 19-test-infrastructure-and-coverage
plan: 02
subsystem: testing
tags: [pytest, auth, security-headers, cors, rate-limiting]

requires:
  - phase: 19-test-infrastructure-and-coverage
    provides: live_gateway fixture, auth_session fixture
provides:
  - HTTP-level auth endpoint test coverage
  - HTTP-level security header verification
  - CORS preflight test coverage
  - Body size limit test coverage
affects: []

tech-stack:
  added: []
  patterns: [helper function for setup_password in tests]

key-files:
  created: []
  modified:
    - docker/tests/test_auth.py
    - docker/tests/test_security.py

key-decisions:
  - "Used helper _setup_password() to configure PBKDF2 passwords per-test"
  - "CORS tests use same-host origin matching gateway's strict policy"

patterns-established:
  - "Auth test pattern: set up password via gateway_module, then test HTTP behavior"

requirements-completed: [TEST-01, TEST-05]

duration: 4min
completed: 2026-03-16
---

# Plan 19-02: Auth and Security HTTP Tests Summary

**HTTP-level auth tests (login/session/rate-limit/recovery) and security header/CORS/body-limit verification across all response paths**

## Performance

- **Duration:** 4 min
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Auth login tested: correct password returns 200+cookie, wrong returns 401, rate limit triggers 429
- Auth recovery tested: valid secret resets password, invalid returns 403
- Security headers verified on health, auth, and all send_json response paths
- CORS preflight verified with same-host origin matching
- Body size limit (1MB) enforced with 413

## Task Commits

1. **Task 1+2: Auth and security HTTP tests** - `a31ed08` (feat)

## Files Created/Modified
- `docker/tests/test_auth.py` - Added 10 HTTP-level auth tests alongside existing source-inspection tests
- `docker/tests/test_security.py` - Added 9 HTTP-level security/CORS/body-limit tests

## Deviations from Plan

### Auto-fixed Issues

**1. Body size test needed password setup**
- **Found during:** Task 2 (security tests)
- **Issue:** Oversized body test returned 400 (no password configured) before reaching _read_body check
- **Fix:** Added _setup_password call before sending oversized body
- **Verification:** Test now correctly gets 413

## Issues Encountered
None

## Next Phase Readiness
- All 40 tests pass (25 prior + 10 auth + 9 security, minus 4 overlap)

---
*Phase: 19-test-infrastructure-and-coverage*
*Completed: 2026-03-16*

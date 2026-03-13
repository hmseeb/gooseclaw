---
phase: quick-4
plan: 01
subsystem: auth
tags: [password-auth, session-cookie, login-page, setup-wizard]

# Dependency graph
requires: []
provides:
  - "Password-based web auth replacing auto-generated tokens"
  - "Custom HTML login page at /login"
  - "Cookie-based session management via gooseclaw_session"
  - "Password creation step in setup wizard"
  - "Recovery flow returns temporary_password"
affects: [setup-wizard, admin-dashboard, auth-flow]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Login page redirect instead of WWW-Authenticate browser popup"
    - "Password required on first setup, optional on reconfigure"
    - "No auto-generated tokens; user always sets password explicitly"

key-files:
  created: []
  modified:
    - "docker/gateway.py"
    - "docker/setup.html"
    - "docker/test_gateway.py"

key-decisions:
  - "Removed GOOSE_WEB_AUTH_TOKEN env var support entirely (only setup.json hash matters)"
  - "Kept Basic Auth path in check_auth for API client backward compatibility, but browsers use login page"
  - "Removed all WWW-Authenticate headers to prevent native browser popup anywhere"
  - "_check_local_or_auth returns JSON 401 instead of redirect (correct for API endpoints)"

patterns-established:
  - "Login redirect: unauthenticated browser requests -> 302 /login"
  - "Password validation: min 4 chars, confirm match, required on first setup"

requirements-completed: [QUICK-4]

# Metrics
duration: 12min
completed: 2026-03-13
---

# Quick Task 4: Replace Auto-Generated Auth Token with Password Auth

**Password-based auth with custom login page, cookie sessions, and password-required setup wizard replacing the auto-generated token system**

## Performance

- **Duration:** 12 min
- **Started:** 2026-03-13T16:29:23Z
- **Completed:** 2026-03-13T16:41:23Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- POST /api/auth/login validates password, sets HttpOnly session cookie
- GET /login serves dark-themed custom login page (no browser popup)
- Unauthenticated browser requests redirect to /login (not 401 WWW-Authenticate)
- handle_save requires user-provided password on first setup (zero auto-generation)
- Setup wizard: Password + Confirm Password fields with client-side validation
- Recovery endpoint returns temporary_password instead of auth_token
- All 472 tests passing (463 existing + 9 new password auth tests)

## Task Commits

Each task was committed atomically:

1. **Task 1: Add password auth backend + login page endpoint** - `9163864` (feat)
2. **Task 2: Update setup.html wizard and dashboard** - `016c11f` (feat)

## Files Created/Modified
- `docker/gateway.py` - Login page HTML, /api/auth/login endpoint, password-required save, recovery updates, removed GOOSE_WEB_AUTH_TOKEN, removed all WWW-Authenticate headers
- `docker/setup.html` - Password/Confirm fields in wizard, removed token display, updated dashboard/recovery/summary labels
- `docker/test_gateway.py` - 9 new TestPasswordAuth tests, updated 3 existing tests for new auth behavior

## Decisions Made
- Removed GOOSE_WEB_AUTH_TOKEN env var entirely: only setup.json web_auth_token_hash matters now
- Kept Basic Auth path in check_auth() for backward compatibility with API clients using Authorization headers
- Removed ALL WWW-Authenticate headers across the codebase to prevent native browser popups
- _check_local_or_auth returns send_json(401) instead of redirect (API endpoints should not redirect)
- Login page is inline HTML constant (LOGIN_HTML) rather than a separate file, matching the error page pattern

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated existing tests expecting old auth behavior**
- **Found during:** Task 1
- **Issue:** TestBotLifecycleAPI tests expected send_response(401) with WWW-Authenticate, but _check_local_or_auth now uses send_json(401)
- **Fix:** Updated test_add_bot_requires_auth and test_remove_bot_requires_auth to check handler._json_response tuple
- **Files modified:** docker/test_gateway.py
- **Committed in:** 9163864

**2. [Rule 1 - Bug] Updated TestUXPaperCuts tests for password-based save**
- **Found during:** Task 1
- **Issue:** test_save_response_includes_pairing_code and test_save_response_no_pairing_code mocked load_setup without web_auth_token_hash, causing 400 "Password required"
- **Fix:** Added web_auth_token_hash to mocked setup return values
- **Files modified:** docker/test_gateway.py
- **Committed in:** 9163864

**3. [Rule 1 - Bug] Updated success screen test**
- **Found during:** Task 2
- **Issue:** test_setup_html_recovery_hint_near_token_box expected recover/save text in success screen, but token box was removed
- **Fix:** Renamed test to test_setup_html_success_screen_no_token_box and asserts absence of token display
- **Files modified:** docker/test_gateway.py
- **Committed in:** 016c11f

---

**Total deviations:** 3 auto-fixed (3 bugs in existing tests)
**Impact on plan:** All fixes necessary to keep existing test suite passing after auth behavior changes. No scope creep.

## Issues Encountered
- Transient "Address already in use" error in one test run (port conflict, not related to changes, passed on retry)

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Password auth fully functional, ready for production use
- Recovery flow still works via GOOSECLAW_RECOVERY_SECRET
- Legacy plaintext web_auth_token in setup.json still supported for migration

---
*Quick Task: 4*
*Completed: 2026-03-13*

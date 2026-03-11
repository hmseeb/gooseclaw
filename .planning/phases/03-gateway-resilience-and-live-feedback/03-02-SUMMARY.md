---
phase: 03-gateway-resilience-and-live-feedback
plan: 02
subsystem: ui
tags: [setup-html, startup-polling, auth-recovery, real-time-feedback, sse-polling]

# Dependency graph
requires:
  - phase: 03-gateway-resilience-and-live-feedback
    plan: 01
    provides: "goose_startup_state dict, GET /api/setup/status, POST /api/auth/recover"
provides:
  - "Real-time startup status polling UI with progress steps (config -> starting -> ready/error)"
  - "Error display showing actual stderr from goose web on failure"
  - "Auth recovery UI at /setup?recover for locked-out users"
  - "Dashboard restart status polling after config changes"
  - "retryStartup() re-POST capability with lastSavedConfig"
affects: [setup-html, end-user-experience]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "setInterval polling with max-attempt timeout for async startup monitoring"
    - "Query param route bypass for unauthenticated recovery access"
    - "Step-based progress UI with CSS state classes (active/done/error)"

key-files:
  created: []
  modified:
    - docker/setup.html
    - docker/gateway.py

key-decisions:
  - "Open Chat button starts hidden; only appears when /api/setup/status returns state=ready"
  - "Recovery page served without auth via query param bypass in gateway.py handle_setup_page()"
  - "401 response body includes recovery URL hint for discoverability"
  - "Dashboard save uses separate pollDashboardRestart() with inline text updates instead of full progress steps"
  - "lastSavedConfig captured before fetch so retryStartup() can re-POST without user re-entering data"

patterns-established:
  - "Startup polling pattern: setInterval 2s, maxAttempts 60 (2-min timeout), clearInterval on terminal state"
  - "Recovery flow: ?recover query param -> skip auth -> show recovery form -> POST /api/auth/recover -> display new token"

requirements-completed: [UX-06, GATE-03, TG-03, AUTH-01, AUTH-02]

# Metrics
duration: 5min
completed: 2026-03-10
---

# Phase 3 Plan 2: Real-time Startup Status UI and Auth Recovery Summary

**Real-time startup status polling replacing static 'give it a few seconds' with animated progress steps, stderr error display, and /setup?recover auth recovery form for locked-out users**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-10T23:32:01Z
- **Completed:** 2026-03-10T23:37:38Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Success screen now shows animated progress steps (Checking configuration -> Starting goose web -> Ready) that poll /api/setup/status every 2 seconds
- When goose web fails to start, actual stderr error text is displayed in the browser with a Retry button
- Locked-out users can visit /setup?recover to reset their auth token using the GOOSECLAW_RECOVERY_SECRET
- Dashboard save also polls for restart status, showing "Agent is restarting..." -> "Agent is ready!" transitions
- TG-03 preserved: pairBox and fetchPairCodeForSuccess() remain in the reworked success screen

## Task Commits

Each task was committed atomically:

1. **Task 1: Real-time startup status polling on success screen** - `db8cc23` (feat)
2. **Task 2: Auth recovery UI for locked-out users** - `d191a92` (feat)

**Plan metadata:** `67ab74e` (docs: complete plan)

## Files Created/Modified
- `docker/setup.html` - Added startup progress UI, pollStartupStatus(), retryStartup(), pollDashboardRestart(), auth recovery section with submitRecovery(), ?recover detection on page load
- `docker/gateway.py` - Added recovery path bypass in handle_setup_page() (skip auth for ?recover), updated 401 response body with recovery URL hint

## Decisions Made
- Open Chat button hidden until state=ready -- prevents users from clicking before goose web is actually running
- Recovery page bypass checks query param with urllib.parse.parse_qs -- safe because recovery form only calls /api/auth/recover which validates the secret
- 401 response body changed from static "Authentication required" to include "/setup?recover" hint for discoverability
- Dashboard restart uses simpler inline text polling (pollDashboardRestart) rather than full progress steps to match dashboard UX
- lastSavedConfig stored before fetch so retryStartup() can re-POST the full config without user re-entering it

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required. GOOSECLAW_RECOVERY_SECRET is optional and documented in Plan 01.

## Next Phase Readiness
- Phase 3 (Gateway Resilience and Live Feedback) complete
- All frontend and backend changes for startup status and auth recovery are in place
- Real-time feedback loop: save -> poll /api/setup/status -> show progress/error/ready

---
*Phase: 03-gateway-resilience-and-live-feedback*
*Completed: 2026-03-10*

## Self-Check: PASSED
- docker/setup.html: FOUND
- docker/gateway.py: FOUND
- 03-02-SUMMARY.md: FOUND
- Commit db8cc23: FOUND
- Commit d191a92: FOUND

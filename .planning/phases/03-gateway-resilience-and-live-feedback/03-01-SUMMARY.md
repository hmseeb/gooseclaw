---
phase: 03-gateway-resilience-and-live-feedback
plan: 01
subsystem: api
tags: [gateway, startup-state-machine, stderr-capture, auth-recovery, proxy-error-detail]

# Dependency graph
requires:
  - phase: 05-production-hardening
    provides: "goose_health_monitor with exponential backoff, rate limiters, auth token hashing, PID management"
provides:
  - "goose_startup_state dict with idle/starting/ready/error state machine"
  - "GET /api/setup/status endpoint for startup state polling"
  - "Stderr ring buffer (50 lines) with _get_recent_stderr() accessor"
  - "proxy_to_goose() 503 JSON response with startup state and stderr tail (GATE-05)"
  - "POST /api/auth/recover endpoint for locked-out user recovery"
affects: [03-02-PLAN, setup-html, frontend-startup-polling]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Thread-safe startup state machine with lock-protected dict"
    - "Stderr PIPE capture with daemon reader thread forwarding to sys.stderr and ring buffer"
    - "Recovery endpoint pattern: env-var secret + constant-time comparison + rate limiting"

key-files:
  created: []
  modified:
    - docker/gateway.py

key-decisions:
  - "Stderr captured via subprocess.PIPE with daemon reader thread; still forwarded to sys.stderr for container logs"
  - "/api/setup/status requires no auth (needed before user authenticates during startup)"
  - "proxy_to_goose() 503 returns JSON with state/message/error/retry_after instead of static text"
  - "Auth recovery gated by GOOSECLAW_RECOVERY_SECRET env var; returns 404 when not configured"
  - "secrets.compare_digest for recovery secret comparison prevents timing attacks"

patterns-established:
  - "Startup state machine: _set_startup_state() transitions through idle->starting->ready/error"
  - "Stderr ring buffer: _append_stderr() + _get_recent_stderr(n) for error surfacing"
  - "Recovery endpoint: env-var-gated, rate-limited, constant-time secret verification"

requirements-completed: [GATE-03, GATE-04, GATE-05, AUTH-01, AUTH-02]

# Metrics
duration: 3min
completed: 2026-03-10
---

# Phase 3 Plan 1: Gateway Resilience and Live Feedback Summary

**Startup state machine with stderr capture ring buffer, /api/setup/status polling endpoint, JSON proxy error responses (GATE-05), and env-var-gated auth recovery endpoint**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-10T23:23:43Z
- **Completed:** 2026-03-10T23:27:17Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments
- Goose web stderr captured into 50-line ring buffer via subprocess.PIPE + daemon reader thread, still forwarded to sys.stderr for container logs
- GET /api/setup/status returns current startup state (idle/starting/ready/error) with error text and timestamp, no auth required
- proxy_to_goose() 503 response now returns JSON with startup state, message, stderr tail, and retry_after instead of static text (GATE-05)
- POST /api/auth/recover allows locked-out users to reset auth token using GOOSECLAW_RECOVERY_SECRET env var

## Task Commits

Each task was committed atomically:

1. **Task 1: Add goose web stderr capture and startup status state machine** - `845f4f3` (feat)
2. **Task 2: Add auth recovery endpoint** - `fdd5356` (feat)

**Plan metadata:** `8634b54` (docs: complete plan)

## Files Created/Modified
- `docker/gateway.py` - Added startup state machine, stderr capture, /api/setup/status, proxy error details, /api/auth/recover

## Decisions Made
- Stderr captured via subprocess.PIPE with daemon reader thread; still forwarded to sys.stderr so container logs are preserved
- /api/setup/status requires no authentication because it is needed before users can authenticate during initial startup
- proxy_to_goose() 503 returns JSON (Content-Type: application/json) with state/message/error/retry_after fields, replacing static plaintext
- Auth recovery is gated behind GOOSECLAW_RECOVERY_SECRET env var; endpoint returns 404 when not configured (opt-in security)
- secrets.compare_digest used for recovery secret comparison to prevent timing attacks

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required. GOOSECLAW_RECOVERY_SECRET is optional and documented in the API docstring.

## Next Phase Readiness
- /api/setup/status endpoint ready for Plan 02 frontend startup status polling
- Startup state machine provides real-time feedback for the setup wizard UI
- Auth recovery endpoint available for deployment environments that set GOOSECLAW_RECOVERY_SECRET

---
*Phase: 03-gateway-resilience-and-live-feedback*
*Completed: 2026-03-10*

## Self-Check: PASSED
- docker/gateway.py: FOUND
- 03-01-SUMMARY.md: FOUND
- Commit 845f4f3: FOUND
- Commit fdd5356: FOUND

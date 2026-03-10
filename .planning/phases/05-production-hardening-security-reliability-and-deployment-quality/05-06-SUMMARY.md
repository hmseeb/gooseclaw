---
phase: 05-production-hardening-security-reliability-and-deployment-quality
plan: 06
subsystem: security
tags: [security-headers, csp, logging, input-sanitization, etag, gateway]

# Dependency graph
requires:
  - phase: 05-05
    provides: crash recovery, thread safety, atomic writes, proxy timeout, graceful shutdown

provides:
  - Security headers (X-Frame-Options, X-Content-Type-Options, X-XSS-Protection, Referrer-Policy, Permissions-Policy) on all responses
  - Content-Security-Policy on setup.html (blocks frame-ancestors, limits script/font/connect sources)
  - HSTS when RAILWAY_ENVIRONMENT is set
  - Structured request logging with timestamp, method, path, status, duration in ms
  - Sanitized error responses (no tracebacks, no internal paths exposed)
  - _internal_error() helper for consistent error handling
  - /api/version endpoint returning deployed version from VERSION file
  - ETag support in handle_setup_page() for conditional requests
  - _sanitize_string() input sanitization on all POST endpoints

affects:
  - Any future changes to gateway.py HTTP handler
  - Any new POST endpoints (should use _sanitize_string and _internal_error)
  - Deployment documentation (security headers now present)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - SECURITY_HEADERS dict applied to every response type (JSON, HTML, proxied)
    - _internal_error() helper for sanitized 500 responses with structured codes
    - _sanitize_string() for input validation on POST endpoints
    - ETag-based conditional requests for setup.html

key-files:
  created: []
  modified:
    - docker/gateway.py

key-decisions:
  - "SECURITY_HEADERS applied in send_json(), handle_setup_page(), and proxy_to_goose() — all response paths covered"
  - "unsafe-inline in CSP script-src accepted trade-off: setup.html has inline JS; CSP still blocks frame-ancestors and external sources"
  - "HSTS only when RAILWAY_ENVIRONMENT is set — Railway terminates TLS, so HSTS is correct only there"
  - "log_request() override captures timing via _request_start set at do_GET/do_POST entry — stdlib approach, no middleware needed"
  - "Error codes added to structured error responses: INTERNAL_ERROR, RATE_LIMITED, INVALID_CONFIG"
  - "_sanitize_string() strips control characters (except \\n, \\t), truncates to max_length, strips whitespace — defense-in-depth"

patterns-established:
  - "All 500 error responses use _internal_error() or inline log+sanitized message pattern — never expose str(e)"
  - "All POST endpoint inputs sanitized via _sanitize_string() before processing"
  - "Security headers centralized in SECURITY_HEADERS dict — add once, applied everywhere"

requirements-completed:
  - QUA-01
  - QUA-02
  - QUA-05
  - QUA-06
  - QUA-07
  - QUA-08
  - POL-01
  - POL-02
  - POL-03

# Metrics
duration: 4min
completed: 2026-03-11
---

# Phase 05 Plan 06: Security Headers, Logging, and Input Sanitization Summary

**Security headers on all gateway responses (CSP, X-Frame-Options, HSTS), structured request logging with duration, sanitized error responses, /api/version endpoint, and _sanitize_string() input validation on all POST endpoints**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-03-10T21:58:47Z
- **Completed:** 2026-03-10T22:02:18Z
- **Tasks:** 3
- **Files modified:** 1

## Accomplishments

- Security headers applied to all response types: JSON (send_json), HTML (handle_setup_page), and proxied responses (proxy_to_goose)
- Content-Security-Policy on setup.html blocks frame-ancestors, external scripts, restricts font/style sources; HSTS conditionally added for Railway deployments
- Structured request logging replaces suppressed log_message — every request logged with ISO timestamp, method, path, HTTP status, duration in ms
- All 500 error responses now return "Internal server error" with error code instead of exposing exception details or internal paths
- /api/version endpoint added (always accessible, before is_configured() check) returning version from VERSION file
- ETag support in handle_setup_page() for conditional GET requests (304 Not Modified)
- _sanitize_string() applied to handle_save() config fields, handle_notify() text, and handle_validate() credentials

## Task Commits

Each task was committed atomically:

1. **Task 1: Add security headers and version endpoint** - `d6dc0de` (feat)
2. **Task 2: Structured request logging and error sanitization** - `08ab17d` (feat)
3. **Task 3: Cache-Control for static files and input sanitization** - `793e295` (feat)

## Files Created/Modified

- `/Users/haseeb/nix-template/docker/gateway.py` - Security headers, CSP, HSTS, structured logging, error sanitization, /api/version, ETag, _sanitize_string()

## Decisions Made

- `unsafe-inline` in CSP `script-src` is an accepted trade-off — setup.html has inline JavaScript; moving to nonce-based CSP would require significant HTML refactoring. CSP still blocks `frame-ancestors` and restricts external origins.
- HSTS added conditionally via `RAILWAY_ENVIRONMENT` env var — Railway terminates TLS, so the header is correct and safe only in that context.
- `log_request()` override chosen over `end_headers()` wrapping — cleaner approach, captures timing through `_request_start` set at do_GET/do_POST entry.
- `re` module moved to top-level import (was previously inline in `_generate_and_store_pair_code()`).
- Structured error codes added to responses: `INTERNAL_ERROR`, `RATE_LIMITED`, `INVALID_CONFIG` — enables programmatic error handling by clients.

## Deviations from Plan

None — plan executed exactly as written. The ETag implementation was planned in Task 3 but incorporated into Task 1 (handle_setup_page) since both modified the same function; the Task 3 verification confirmed it was present.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Phase 5 (Production Hardening) is now complete — all 6 plans executed
- Gateway now has full security header coverage, request visibility, sanitized error handling, and input validation
- Deployment to Railway will include HSTS automatically via RAILWAY_ENVIRONMENT detection

---
*Phase: 05-production-hardening-security-reliability-and-deployment-quality*
*Completed: 2026-03-11*

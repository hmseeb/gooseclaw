---
phase: 16-watcher-engine
plan: 03
subsystem: api
tags: [watchers, webhook, feed, engine-loop, http-api, startup-wiring]

# Dependency graph
requires:
  - phase: 16-watcher-engine
    provides: Watcher CRUD, fire dispatch, webhook routing, feed polling
provides:
  - HTTP CRUD endpoints at /api/watchers (create/list/delete/update)
  - Webhook receiver at /api/webhooks/<name> (public, auth-exempt)
  - Watcher engine loop with 30s tick, polling feed watchers at configured intervals
  - Startup wiring for _load_watchers and start_watcher_engine
  - Graceful shutdown via stop_watcher_engine
affects: [watcher-management-ui, watcher-configuration]

# Tech tracking
tech-stack:
  added: [random]
  patterns: [engine tick extraction for testability, daemon thread per feed check, initial poll jitter]

key-files:
  created: []
  modified: [docker/gateway.py, docker/test_gateway.py]

key-decisions:
  - "Engine tick extracted as _watcher_engine_tick() for testability (same pattern as job engine)"
  - "Feed checks dispatched in daemon threads for non-blocking polling"
  - "Initial poll jitter: random offset up to min(poll_seconds, 60) to stagger first checks"
  - "Webhook endpoint auth-exempt (public), watcher CRUD requires auth via _check_local_or_auth"
  - "Startup wiring added to main(), _restart handlers, and shutdown handler"

patterns-established:
  - "Watcher API follows exact same pattern as job API (handle_create/list/delete/update)"
  - "Engine loop uses 5s sleep x 6 iterations for responsive shutdown (same as job engine)"
  - "Webhook receiver at /api/webhooks/<name> delegates to _handle_webhook_incoming from Plan 02"

requirements-completed: [WATCH-08, WATCH-09, WATCH-10]

# Metrics
duration: 7min
completed: 2026-03-14
---

# Phase 16 Plan 03: Gateway API Integration + Engine Loop Summary

**Watcher CRUD API at /api/watchers, webhook receiver at /api/webhooks/<name>, and engine loop polling feed watchers with startup integration**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-14T16:48:01Z
- **Completed:** 2026-03-14T16:55:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Full CRUD HTTP API for watchers (POST/GET/DELETE/PUT /api/watchers)
- Webhook receiver endpoint at /api/webhooks/<name> routing payloads to matching watchers
- Watcher engine loop with 30s tick interval polling feed watchers at their configured poll_seconds
- Startup wiring: _load_watchers and start_watcher_engine called in main() and restart handlers
- 16 new tests across 4 test classes (TestWatcherAPI, TestWebhookEndpoint, TestWatcherEngineLoop, TestWatcherStartupWiring)

## Task Commits

Each task was committed atomically:

1. **Task 1: TDD API endpoints + webhook receiver** - `fbd15a4` (feat)
2. **Task 2: Watcher engine loop + startup wiring** - `635bd79` (feat)

## Files Created/Modified
- `docker/gateway.py` - API handler methods, HTTP route wiring, engine loop, startup integration
- `docker/test_gateway.py` - 16 new tests across 4 test classes

## Decisions Made
- Engine tick function extracted for direct testability (avoids mocking sleep loops)
- Webhook endpoint is auth-exempt since webhooks are public (optionally HMAC-protected per watcher)
- Feed checks dispatched in daemon threads to prevent blocking the engine loop
- Initial poll jitter prevents thundering herd on startup with many feed watchers

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Complete watcher engine is now fully functional and integrated into the gateway
- All 50 watcher tests passing (34 from Plans 01-02 + 16 new)
- Phase 16 (Watcher Engine) is complete

---
*Phase: 16-watcher-engine*
*Completed: 2026-03-14*

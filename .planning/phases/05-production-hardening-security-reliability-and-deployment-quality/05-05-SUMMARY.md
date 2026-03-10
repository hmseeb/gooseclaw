---
phase: 05-production-hardening
plan: 05
subsystem: infra
tags: [subprocess, threading, reliability, crash-recovery, atomic-writes, signals]

# Dependency graph
requires:
  - phase: 05-04
    provides: rate limiting, config validation, deep health check
provides:
  - goose web health monitor thread with exponential backoff auto-restart
  - thread-safe access to goose_process and telegram_process globals
  - PID file management for subprocess lifecycle tracking
  - atomic setup.json writes via os.replace() with backup
  - configurable proxy timeout (GOOSECLAW_PROXY_TIMEOUT, default 60s)
  - SSE socket timeout removal for long-lived streams
  - graceful shutdown: stop server -> terminate children -> remove PID files
  - entrypoint.sh waits for gateway.py on SIGTERM before exiting
affects:
  - 05-06
  - any future work on subprocess management or config persistence

# Tech tracking
tech-stack:
  added: []
  patterns:
    - health-monitor-thread: daemon thread polls subprocess.poll() every 15s, restarts with exponential backoff
    - pid-file-lifecycle: write on start, remove on stop/crash, check-stale on startup
    - atomic-write-rename: write to .tmp then os.replace() to prevent partial-write corruption
    - lock-before-read: always acquire lock before reading shared subprocess handle

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/entrypoint.sh

key-decisions:
  - "goose_health_monitor runs as daemon thread checking every 15s; backoff doubles per failure capped at 120s"
  - "PROXY_TIMEOUT defaults to 60s (was hardcoded 300s); SSE connections remove timeout after detection"
  - "shutdown() stops server first (no new connections), then terminates children with wait+kill pattern"
  - "os.replace() is POSIX-atomic when source and dest are on the same filesystem (both in CONFIG_DIR)"
  - "entrypoint.sh uses wait $GATEWAY_PY_PID so gateway's SIGTERM handler can clean up children before bash exits"

patterns-established:
  - "Lock-before-read: all reads of goose_process and telegram_process acquire their respective lock first"
  - "PID files in CONFIG_DIR track subprocess lifecycle: written after Popen, removed after termination"
  - "Atomic writes: write to .tmp then os.replace() — never write directly to the target file"

requirements-completed: [REL-03, REL-04, REL-05, REL-06, REL-07, QUA-09]

# Metrics
duration: 3min
completed: 2026-03-11
---

# Phase 5 Plan 5: Reliability — Crash Recovery, Thread Safety, and Atomic Writes Summary

**goose web auto-restart via health monitor thread with exponential backoff, full goose_process/telegram_process thread safety, atomic setup.json writes with backup, 60s configurable proxy timeout, and graceful SIGTERM shutdown chain**

## Performance

- **Duration:** ~3 min
- **Started:** 2026-03-10T21:52:38Z
- **Completed:** 2026-03-10T21:55:36Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- goose web crash recovery: `goose_health_monitor` daemon thread restarts the subprocess within 15s of crash detection, with exponential backoff (5s → 10s → 20s → ... → 120s max) to handle repeated failures
- Thread safety: every read/write of `goose_process` and `telegram_process` now happens under `goose_lock`/`telegram_lock` respectively; fixes race condition where concurrent requests could observe partially-updated state
- Atomic config writes: `save_setup()` writes to `setup.json.tmp` then `os.replace()` to the final path — power loss between write and rename cannot produce a corrupt file; previous config backed up to `setup.json.bak`
- Configurable proxy timeout: `GOOSECLAW_PROXY_TIMEOUT` env var (default 60s, was hardcoded 300s); SSE streams remove socket timeout after content-type detection to keep long-lived connections alive
- PID file management: `_write_pid`/`_remove_pid`/`_check_stale_pid` track subprocess PIDs in CONFIG_DIR; stale PIDs from prior crashes are cleaned on startup
- Graceful SIGTERM: `shutdown()` stops the HTTP server first (no new connections), then terminates goose web and telegram with `wait(5)+kill` pattern, removes PID files; `entrypoint.sh` now waits for gateway.py to exit so children are cleaned up before bash exits

## Task Commits

Each task was committed atomically:

1. **Task 1: Add goose web health monitor with auto-restart and fix thread safety** - `beee82e` (feat)
2. **Task 2: Atomic config writes, request timeout, and graceful shutdown** - `aea284c` (feat)

**Plan metadata:** (pending)

## Files Created/Modified
- `/Users/haseeb/nix-template/docker/gateway.py` - Added `telegram_lock`, PID helpers, `goose_health_monitor`, fixed thread safety in `proxy_to_goose`/`handle_telegram_status`/`handle_telegram_pair`/`start_telegram_gateway`/`shutdown`, atomic `save_setup`, `PROXY_TIMEOUT`, SSE timeout removal
- `/Users/haseeb/nix-template/docker/entrypoint.sh` - `shutdown()` uses `kill -TERM` + `wait` instead of bare `kill` so gateway cleans up children before bash exits

## Decisions Made
- `goose_health_monitor` checks every 15s (balance between responsiveness and CPU overhead); backoff doubles per consecutive failure, capped at 120s to avoid permanent outage from transient crash loops
- `PROXY_TIMEOUT` default 60s (down from hardcoded 300s) — long enough for typical AI responses, short enough to release stuck connections; SSE is exempt since its streams are intentionally long-lived
- `shutdown()` stops HTTP server *first* to prevent new requests from reaching a partially-shutdown backend, then cleans up children in order
- `os.replace()` chosen over `shutil.move()` for POSIX atomicity guarantee on same-filesystem rename

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All reliability requirements (REL-03 through REL-07, QUA-09) complete
- gateway.py is now production-grade: no perpetual 502s on goose web crash, no state corruption from concurrent requests, no config corruption from power loss
- Phase 5 Plan 6 can proceed (final deployment quality items)

---
*Phase: 05-production-hardening*
*Completed: 2026-03-11*

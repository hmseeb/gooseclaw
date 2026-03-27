---
phase: 27-websocket-infrastructure
plan: 01
subsystem: infra
tags: [websocket, rfc6455, frame-parser, tdd]

requires:
  - phase: none
    provides: none
provides:
  - RFC 6455 WebSocket frame parser (ws_recv_frame, ws_send_frame)
  - WebSocket accept key computation (ws_accept_key)
  - WebSocket control frames (ws_send_ping, ws_send_close)
  - Ping loop keepalive (ws_start_ping_loop)
affects: [27-websocket-infrastructure, 28-voice-pipeline]

tech-stack:
  added: []
  patterns: [socket-pair testing for protocol functions, threaded send/recv for large payloads]

key-files:
  created:
    - docker/tests/test_websocket.py
  modified:
    - docker/gateway.py

key-decisions:
  - "Used socket.socketpair() for unit testing frame parser without network"
  - "Threaded recv for 64-bit payload tests to avoid macOS socket buffer deadlocks"
  - "Wrapped ws_recv_frame in try/except returning (None, b'') on connection errors"

patterns-established:
  - "WebSocket protocol functions live at module scope in gateway.py, before GatewayHandler class"
  - "TDD: RED commit (failing tests) then GREEN commit (implementation passes)"

requirements-completed: [VOICE-10]

duration: 5min
completed: 2026-03-27
---

# Plan 27-01: WebSocket Protocol Functions Summary

**RFC 6455 frame parser with accept key, 3 payload length encodings, masking, ping/pong, and close via TDD**

## Performance

- **Duration:** 5 min
- **Tasks:** 2 (RED + GREEN)
- **Files modified:** 2

## Accomplishments
- 14 unit tests covering all WebSocket protocol edge cases
- Frame parser handles 7-bit (<126), 16-bit (126), and 64-bit (127) payload lengths
- Masking/unmasking correctly applied per RFC 6455
- Close frame with status code and reason string
- Ping loop daemon thread for keepalive

## Task Commits

Each task was committed atomically:

1. **Task 1: Create WebSocket protocol test suite (RED)** - `c560b06` (test)
2. **Task 2: Implement WebSocket protocol functions (GREEN)** - `92cab76` (feat)

## Files Created/Modified
- `docker/tests/test_websocket.py` - 14 unit tests for frame parser, accept key, masking, ping/pong, close
- `docker/gateway.py` - ws_accept_key, ws_recv_frame, ws_send_frame, ws_send_close, ws_send_ping, ws_start_ping_loop at module scope

## Decisions Made
- Used threaded recv in test_large_payload_64bit to avoid macOS socketpair buffer deadlock on 70KB payload
- ws_recv_frame returns (None, b"") on connection errors rather than raising, matching plan spec

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Large payload test deadlock on macOS**
- **Found during:** Task 2 (GREEN phase)
- **Issue:** socket.socketpair() on macOS has small buffers. 70KB sendall blocks because recv hasn't started
- **Fix:** Used threading.Thread to run recv concurrently with sendall
- **Files modified:** docker/tests/test_websocket.py
- **Verification:** test_large_payload_64bit passes in <1s
- **Committed in:** 92cab76 (GREEN commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary for test correctness on macOS. No scope creep.

## Issues Encountered
- Pre-existing test failure in test_entrypoint_has_neo4j_startup_block (neo4j removed previously). Not related to this plan.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All protocol functions ready for Plan 27-02 (server handler, outbound client, connection tracking)
- ws_recv_frame and ws_send_frame provide the foundation for handle_voice_ws echo loop

---
*Phase: 27-websocket-infrastructure*
*Completed: 2026-03-27*

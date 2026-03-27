---
phase: 27-websocket-infrastructure
plan: 02
subsystem: infra
tags: [websocket, http-101, echo-server, tls-client, connection-tracking]

requires:
  - phase: 27-websocket-infrastructure
    provides: WebSocket protocol functions (ws_recv_frame, ws_send_frame, etc.)
provides:
  - WebSocket server handler (handle_voice_ws) with HTTP 101 upgrade on /ws/voice
  - Outbound TLS WebSocket client (ws_client_connect) for external APIs
  - Connection tracking with concurrent cap of 2
  - Echo loop scaffold for Phase 28 Gemini relay
affects: [28-voice-pipeline, 29-dashboard-gating]

tech-stack:
  added: []
  patterns: [HTTP 101 upgrade in BaseHTTPRequestHandler, connection eviction for cap enforcement]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/tests/test_websocket.py

key-decisions:
  - "Echo loop as scaffold. Phase 28 replaces echo with Gemini relay"
  - "Origin check logs warning but does not reject. Railway domain matching is complex"
  - "Connection cap at 2 for single-user app. Oldest evicted with close frame code 1001"
  - "Outbound client uses TLS by default (port 443) for Gemini Live API"

patterns-established:
  - "WebSocket upgrade detected in do_GET before route matching"
  - "Connection tracking with _ws_connections dict under lock"
  - "Integration tests use live_gateway fixture with raw socket connections"

requirements-completed: [VOICE-10]

duration: 5min
completed: 2026-03-27
---

# Plan 27-02: WebSocket Server Handler & Integration Tests Summary

**HTTP 101 upgrade on /ws/voice with echo loop, outbound TLS client, connection cap at 2, and 9 integration tests**

## Performance

- **Duration:** 5 min
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- handle_voice_ws() method in GatewayHandler with full HTTP 101 upgrade handshake
- Echo loop scaffold for Phase 28 Gemini relay (text + binary frames)
- ws_client_connect() for outbound TLS WebSocket to external APIs
- Connection tracking with _WS_MAX_CONCURRENT=2, oldest eviction
- 9 integration tests validating handshake, echo, close, cap, and ping keepalive

## Task Commits

Each task was committed atomically:

1. **Task 1: Add WebSocket server handler, outbound client, and connection tracking** - `6441a39` (feat)
2. **Task 2: Add integration tests** - `af22c15` (test)

## Files Created/Modified
- `docker/gateway.py` - handle_voice_ws(), ws_client_connect(), _ws_register/_ws_unregister/_ws_active_count, do_GET WebSocket detection
- `docker/tests/test_websocket.py` - TestWsHandshake, TestWsClose, TestWsClientConnect, TestWsConnectionCap, TestWsPingLoop

## Decisions Made
- Origin validation logs warning but does not reject (Railway domain matching complexity)
- Socket timeout set to 60s on WebSocket connections (ping loop at 25s keeps alive)
- Outbound client validates both 101 status and Sec-WebSocket-Accept header match

## Deviations from Plan
None - plan executed as specified.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- WebSocket infrastructure complete. Phase 28 can replace echo loop with Gemini relay.
- ws_client_connect() ready for outbound connection to Gemini Live API.
- Connection tracking prevents thread exhaustion with concurrent cap.

---
*Phase: 27-websocket-infrastructure*
*Completed: 2026-03-27*

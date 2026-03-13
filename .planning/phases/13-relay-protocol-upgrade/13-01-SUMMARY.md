---
phase: 13-relay-protocol-upgrade
plan: 01
subsystem: api
tags: [sse, http-client, rest, multimodal, content-blocks, streaming]

# Dependency graph
requires:
  - phase: 12-inbound-media-pipeline
    provides: MediaContent class with to_content_block(), InboundMessage envelope
provides:
  - _parse_sse_events generator for SSE data: line parsing
  - _build_content_blocks for assembling multimodal ChatRequest content arrays
  - _extract_response_content for separating text from media in responses
  - _do_rest_relay for non-streaming POST to /reply with SSE parsing
  - _do_rest_relay_streaming for incremental text delivery via flush_cb
affects: [13-02 relay wiring, 14-outbound-rich-media]

# Tech tracking
tech-stack:
  added: []
  patterns: [SSE line parsing via readline, REST /reply with Basic auth, 3-tuple return (text, error, media)]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "REST relay returns 3-tuple (text, error, media_blocks) to carry image blocks for Phase 14"
  - "_extract_response_content handles nested toolResponse images for tool screenshot capture"
  - "Streaming relay reuses _StreamBuffer pattern from WS relay with identical flush semantics"

patterns-established:
  - "SSE parsing: readline() loop with data: prefix detection and JSON parse, skip invalid"
  - "Content block assembly: text block + MediaContent.to_content_block() with empty fallback"
  - "REST auth: Basic base64(user:_INTERNAL_GOOSE_TOKEN) matching existing provider update pattern"

requirements-completed: [MEDIA-10, MEDIA-11, MEDIA-12]

# Metrics
duration: 3min
completed: 2026-03-13
---

# Phase 13 Plan 01: REST Relay Helpers Summary

**SSE parser, content block builder, response extractor, and REST relay functions for goosed /reply endpoint using TDD**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-13T12:50:36Z
- **Completed:** 2026-03-13T12:53:38Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- 5 new helper functions for REST-based relay to goosed /reply endpoint
- 28 new tests covering SSE parsing, content blocks, response extraction, relay, and streaming
- All 408 tests passing (380 existing + 28 new)

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Write failing tests for REST relay helpers** - `f7f4697` (test)
2. **Task 2: GREEN -- Implement REST relay helpers** - `c0e5a48` (feat)

## Files Created/Modified
- `docker/gateway.py` - Added _parse_sse_events, _build_content_blocks, _extract_response_content, _do_rest_relay, _do_rest_relay_streaming
- `docker/test_gateway.py` - Added TestParseSSEEvents, TestBuildContentBlocks, TestExtractResponseContent, TestRestRelay, TestRestRelayStreaming (28 tests)

## Decisions Made
- REST relay returns 3-tuple (text, error, media_blocks) instead of 2-tuple, enabling Phase 14 outbound media routing
- _extract_response_content handles nested toolResponse images for screenshot capture from tools
- Streaming relay reuses existing _StreamBuffer class with identical flush semantics to WS version
- Functions placed after _StreamBuffer, before _do_ws_relay_streaming, ready for Plan 02 wiring

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All 5 REST relay helpers exist and are tested
- Plan 02 can wire _do_rest_relay/_do_rest_relay_streaming into _relay_to_goose_web
- Plan 02 must update all 12 call sites for 3-tuple return type
- WS relay functions still present (Plan 02 will switch callers)

---
*Phase: 13-relay-protocol-upgrade*
*Completed: 2026-03-13*

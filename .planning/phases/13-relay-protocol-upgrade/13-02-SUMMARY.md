---
phase: 13-relay-protocol-upgrade
plan: 02
subsystem: gateway
tags: [rest, sse, relay, websocket-removal, multimodal, content-blocks]

# Dependency graph
requires:
  - phase: 13-relay-protocol-upgrade
    plan: 01
    provides: "_parse_sse_events, _build_content_blocks, _extract_response_content, _do_rest_relay, _do_rest_relay_streaming"
  - phase: 12
    provides: "MediaContent, InboundMessage with media, _download_telegram_file"
provides:
  - "_relay_to_goose_web uses REST relay with 3-tuple return (text, error, media_blocks)"
  - "All callers handle 3-tuple unpack"
  - "content_blocks from InboundMessage media flow through to goose"
  - "WS relay code fully removed (~280 lines)"
affects: [14-outbound-media]

# Tech tracking
tech-stack:
  added: []
  patterns: ["REST /reply + SSE for all relay paths", "3-tuple relay return for media passthrough"]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "All call sites use *_ star-unpack except BotInstance._do_message_relay which captures media"
  - "_fire_cron_job and memory_writer switched from _do_ws_relay to _do_rest_relay directly"
  - "/stop handler simplified: removed WS cancel frame send, just closes HTTP connection"
  - "Legacy _telegram_poll_loop builds InboundMessage + content_blocks from downloaded media"
  - "ChannelRelay.__call__ checks isinstance(user_id_or_msg, InboundMessage) for content block building"

patterns-established:
  - "3-tuple relay: all relay paths return (text, error, media_blocks) for Phase 14 outbound routing"
  - "content_blocks passthrough: build from InboundMessage.media, pass to _relay_to_goose_web, forward to _do_rest_relay"

requirements-completed: [MEDIA-10, MEDIA-11, MEDIA-12]

# Metrics
duration: 12min
completed: 2026-03-13
---

# Phase 13 Plan 02: Wire REST Relay Summary

**REST /reply replaces WebSocket relay across all 15 call sites, returning 3-tuple with media blocks and accepting multimodal content blocks**

## Performance

- **Duration:** 12 min
- **Started:** 2026-03-13T12:56:00Z
- **Completed:** 2026-03-13T13:08:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- _relay_to_goose_web fully switched from WS to REST relay with content_blocks kwarg and 3-tuple return
- All 12 original call sites + 3 discovered call sites (_fire_cron_job, memory_writer, debug prewarm) updated
- BotInstance._do_message_relay builds content_blocks from InboundMessage media for multimodal support
- ChannelRelay and legacy poll loop also build and pass content_blocks
- ~280 lines of WebSocket code removed (_ws_connect, _ws_send_text, _ws_recv_frame, _ws_recv_text, _do_ws_relay, _do_ws_relay_streaming)
- 416 tests passing (408 existing + 8 new)

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- failing tests** - `2a9d3ab` (test)
2. **Task 2: GREEN -- wire REST relay** - `376f754` (feat)

**Plan metadata:** (pending)

## Files Created/Modified
- `docker/gateway.py` - Rewired _relay_to_goose_web to REST, updated all callers, removed WS code
- `docker/test_gateway.py` - Added TestRelayProtocolUpgrade (8 tests), fixed 5 existing tests referencing removed WS

## Decisions Made
- Used `*_` star-unpack at call sites that don't need media (kick greeting, compact, ChannelRelay, legacy)
- BotInstance._do_message_relay captures full `media` from 3-tuple for future Phase 14 use
- ChannelRelay checks isinstance for InboundMessage to conditionally build content_blocks
- /stop handler simplified: just closes the HTTP connection (no WS cancel frame needed)
- _fire_cron_job switched to direct _do_rest_relay call (no _relay_to_goose_web wrapper needed for cron)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Updated _fire_cron_job from _do_ws_relay to _do_rest_relay**
- **Found during:** Task 2 (verification)
- **Issue:** _fire_cron_job directly called _do_ws_relay which was removed
- **Fix:** Switched to _do_rest_relay with 3-tuple unpack
- **Files modified:** docker/gateway.py
- **Verification:** TestFireCronJobStripping tests pass
- **Committed in:** 376f754

**2. [Rule 3 - Blocking] Updated memory_writer_loop from _do_ws_relay to _do_rest_relay**
- **Found during:** Task 2 (verification)
- **Issue:** _memory_writer_loop directly called _do_ws_relay which was removed
- **Fix:** Switched to _do_rest_relay with 3-tuple unpack
- **Files modified:** docker/gateway.py
- **Verification:** All memory writer tests pass
- **Committed in:** 376f754

**3. [Rule 3 - Blocking] Updated debug prewarm endpoint from _do_ws_relay to _do_rest_relay**
- **Found during:** Task 2 (verification)
- **Issue:** /api/debug/prewarm directly called _do_ws_relay which was removed
- **Fix:** Switched to _do_rest_relay with 3-tuple unpack
- **Files modified:** docker/gateway.py
- **Verification:** No functional test but code compiles and runs
- **Committed in:** 376f754

**4. [Rule 1 - Bug] Fixed 5 existing tests still referencing _do_ws_relay**
- **Found during:** Task 2 (verification)
- **Issue:** TestFireCronJobStripping and TestCronNotifyChannel mocked gateway._do_ws_relay
- **Fix:** Changed to mock gateway._do_rest_relay with 3-tuple return values
- **Files modified:** docker/test_gateway.py
- **Verification:** All 5 tests pass
- **Committed in:** 376f754

**5. [Rule 3 - Blocking] Simplified /stop handler WS cancel frame removal**
- **Found during:** Task 2 (verification)
- **Issue:** /stop handler used _ws_send_text to send cancel frame which was removed
- **Fix:** Simplified to just sock_ref[0].close() (HTTP connection close suffices)
- **Files modified:** docker/gateway.py
- **Verification:** TestClearKillsRelay passes
- **Committed in:** 376f754

---

**Total deviations:** 5 auto-fixed (1 bug, 4 blocking)
**Impact on plan:** All auto-fixes necessary for correctness. Plan only listed 12 call sites but there were 15 total. No scope creep.

## Issues Encountered
- Plan listed 12 call sites but 3 additional direct _do_ws_relay callers existed (_fire_cron_job, memory_writer, debug prewarm). Discovered during verification and fixed.
- Test for media content_blocks initially failed because test passed pre-built MediaContent objects but _do_message_relay processes file_id reference dicts. Fixed test to match real flow.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Relay protocol fully upgraded to REST. All paths return 3-tuple with media_blocks.
- Ready for Phase 14 outbound media routing (media_blocks available at all call sites that need them)
- BotInstance._do_message_relay already captures media from return for future use

---
*Phase: 13-relay-protocol-upgrade*
*Completed: 2026-03-13*

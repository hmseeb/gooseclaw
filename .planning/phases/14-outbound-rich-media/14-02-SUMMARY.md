---
phase: 14-outbound-rich-media
plan: 02
subsystem: media
tags: [telegram, outbound-media, adapter, notify, routing]

# Dependency graph
requires:
  - phase: 14-outbound-rich-media (plan 01)
    provides: TelegramOutboundAdapter, _build_multipart, _route_media_blocks, _ext_from_mime
provides:
  - Media routing wired into BotInstance._do_message_relay (after text delivery)
  - Media routing wired into ChannelRelay.__call__ (via channel adapter)
  - Media routing wired into legacy _telegram_poll_loop (both relay paths)
  - notify_all accepts optional media parameter with backward compat
  - _telegram_notify_handler routes media to all paired chats
affects: [15-reference-channel-plugin]

# Tech tracking
tech-stack:
  added: []
  patterns: [try/except TypeError for backward-compat media kwarg in notify_all, OutboundAdapter base accepts **kwargs for subclass flexibility]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "OutboundAdapter base class send_image/send_voice/send_file updated to accept **kwargs for subclass signature compatibility"
  - "notify_all uses try/except TypeError pattern to pass media to new handlers while remaining backward-compat with old text-only handlers"
  - "Media routing happens after text delivery in all paths (quiet + streaming), skipped when cancelled"

patterns-established:
  - "Media routing pattern: after text delivery, if media and not cancelled, create TelegramOutboundAdapter and call _route_media_blocks"
  - "Backward-compat handler invocation: try handler(text, media=media) except TypeError: handler(text)"

requirements-completed: [MEDIA-13, MEDIA-14]

# Metrics
duration: 14min
completed: 2026-03-13
---

# Phase 14 Plan 02: Wire Media Routing Summary

**Media blocks from goose responses route through TelegramOutboundAdapter in BotInstance, ChannelRelay, legacy poll, and notify_all with backward-compat fallback**

## Performance

- **Duration:** 14 min
- **Started:** 2026-03-13T13:26:54Z
- **Completed:** 2026-03-13T13:41:37Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- BotInstance._do_message_relay sends images via TelegramOutboundAdapter after text delivery (both quiet and streaming paths)
- ChannelRelay.__call__ captures media from _relay_to_goose_web and routes through channel adapter stored in _loaded_channels
- Legacy _telegram_poll_loop routes media blocks through TelegramOutboundAdapter in both media-only and text+media relay paths
- notify_all accepts optional media parameter, passes to new-style handlers, falls back gracefully for old text-only handlers
- _telegram_notify_handler sends media to all paired chat_ids after text delivery
- Channels without media support (LegacyOutboundAdapter) get graceful text fallback via OutboundAdapter base class

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Write failing tests for media routing wiring** - `b5c68c0` (test)
2. **Task 2: GREEN -- Wire media routing into relay paths and notify_all** - `93ad68c` (feat)

## Files Created/Modified
- `docker/gateway.py` - Media routing in _do_message_relay, ChannelRelay, legacy poll, notify_all, _telegram_notify_handler; OutboundAdapter base class updated for **kwargs
- `docker/test_gateway.py` - TestBotMediaRouting (5 tests), TestChannelRelayMedia (2 tests), TestNotifyMedia (3 tests); existing test mocks updated to 3-tuple return values

## Decisions Made
- OutboundAdapter base class send_image/send_voice/send_file updated with **kwargs to accept mime_type and other kwargs from _route_media_blocks without crashing the fallback path
- notify_all uses try/except TypeError to invoke handlers: tries media kwarg first, falls back to text-only if handler doesn't accept it
- Media routing is placed after text delivery but before the finally block in all paths, with its own try/except to prevent media errors from crashing text delivery

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] OutboundAdapter base class signature mismatch with _route_media_blocks**
- **Found during:** Task 2 (GREEN implementation)
- **Issue:** _route_media_blocks calls adapter.send_image(raw_bytes, mime_type=mime) but OutboundAdapter.send_image(url, caption="") didn't accept mime_type kwarg, causing TypeError on fallback path
- **Fix:** Updated OutboundAdapter send_image/send_voice/send_file to accept **kwargs and use generic fallback text instead of echoing data back
- **Files modified:** docker/gateway.py, docker/test_gateway.py (updated existing tests for new fallback text)
- **Verification:** TestChannelRelayMedia.test_legacy_adapter_gets_text_fallback passes
- **Committed in:** 93ad68c (Task 2 commit)

**2. [Rule 1 - Bug] Existing test mocks returning 2-tuples for 3-tuple unpack**
- **Found during:** Task 2 (GREEN implementation)
- **Issue:** ChannelRelay now does `response_text, error, _media = _relay_to_goose_web(...)` but many existing tests mocked return_value as 2-tuples ("text", ""), causing ValueError
- **Fix:** Updated all 20+ _relay_to_goose_web mock return_values from 2-tuples to 3-tuples (adding empty list for media)
- **Files modified:** docker/test_gateway.py
- **Verification:** All 447 tests pass
- **Committed in:** 93ad68c (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the deviations documented above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Full end-to-end media pipeline complete: images flow from goose tool responses to user's Telegram chat
- Phase 14 (Outbound Rich Media) fully done: send_image, _route_media_blocks, BotInstance wiring, ChannelRelay wiring, notify_all media support
- Ready for Phase 15: Reference Channel Plugin can implement OutboundAdapter with full media support

---
*Phase: 14-outbound-rich-media*
*Completed: 2026-03-13*

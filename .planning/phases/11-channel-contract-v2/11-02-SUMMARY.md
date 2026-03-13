---
phase: 11-channel-contract-v2
plan: 02
subsystem: channel-system
tags: [channel-wiring, inbound-message, outbound-adapter, backward-compat, tdd]

requires:
  - phase: 11-channel-contract-v2
    provides: "InboundMessage, OutboundAdapter, ChannelCapabilities, LegacyOutboundAdapter"
provides:
  - "_load_channel wraps v1 plugins in LegacyOutboundAdapter"
  - "ChannelRelay accepts InboundMessage (dual signature)"
  - "BotInstance._poll_loop creates InboundMessage envelopes"
  - "GET /api/channels exposes capabilities"
affects: [phase-12-media-pipeline, phase-13-telegram-media]

tech-stack:
  added: []
  patterns: [isinstance-overload, adapter-wrapping-in-loader, media-type-mapping]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "isinstance(first_arg, InboundMessage) for ChannelRelay overload detection"
  - "adapter.send_text registered with notification bus (not raw send_fn)"
  - "Media messages still get MEDIA_REPLY but also create InboundMessage + call _do_message_relay"
  - "_do_message_relay accepts optional inbound_msg kwarg for future media relay"

patterns-established:
  - "ChannelRelay dual signature: (InboundMessage, send_fn) or (user_id, text, send_fn)"
  - "_load_channel stores adapter in _loaded_channels entry"
  - "Telegram media type mapping from _MEDIA_KEYS to InboundMessage media list"

requirements-completed: [MEDIA-05]

duration: 2min
completed: 2026-03-13
---

# Phase 11 Plan 02: Wire v2 Channel Contract Summary

**v2 contract wired into _load_channel, ChannelRelay, BotInstance._poll_loop, and /api/channels with full backward compat**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-13T12:06:33Z
- **Completed:** 2026-03-13T12:08:52Z
- **Tasks:** 2 (TDD: RED then GREEN)
- **Files modified:** 2

## Accomplishments
- _load_channel wraps legacy send(text) in LegacyOutboundAdapter, uses v2 adapter directly
- Notification handler uses adapter.send_text instead of raw send_fn
- ChannelRelay.__call__ accepts InboundMessage with isinstance overload (backward compat preserved)
- BotInstance._poll_loop creates InboundMessage envelopes with media type mapping
- GET /api/channels includes capabilities dict per channel
- 10 new tests, all passing, 345 total (zero regression)

## Task Commits

1. **Task 1: RED -- Write failing tests for wiring** - part of efc9b66 (test + feat combined)
2. **Task 2: GREEN -- Wire v2 types into plugin system** - `efc9b66` (feat)

## Files Created/Modified
- `docker/gateway.py` - Updated _load_channel, ChannelRelay.__call__, BotInstance._poll_loop, handle_list_channels
- `docker/test_gateway.py` - TestLoadChannelV2, TestChannelRelayV2, TestBotInboundMessage, TestChannelsAPICapabilities

## Decisions Made
- isinstance(first_arg, InboundMessage) for ChannelRelay overload (simple, explicit, no ambiguity)
- Media messages still send MEDIA_REPLY but also create InboundMessage and pass to _do_message_relay (future Phase 12 uses media)
- _do_message_relay receives inbound_msg as optional kwarg (non-breaking extension)
- Caption text used as InboundMessage.text for media-with-caption messages

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed typing_cb(user_id) reference after ChannelRelay refactor**
- **Found during:** Task 2 (ChannelRelay.__call__ update)
- **Issue:** After renaming parameter from user_id to user_id_or_msg, the typing callback still referenced user_id
- **Fix:** Changed self._typing_cb(user_id) to self._typing_cb(user_key)
- **Files modified:** docker/gateway.py
- **Verification:** All tests pass
- **Committed in:** efc9b66

**2. [Rule 1 - Bug] Fixed GooseGatewayHandler reference in API test**
- **Found during:** Task 1 (writing tests)
- **Issue:** Test referenced gateway.GooseGatewayHandler which doesn't exist (actual class is GatewayHandler)
- **Fix:** Changed to gateway.GatewayHandler
- **Files modified:** docker/test_gateway.py
- **Verification:** Test runs correctly
- **Committed in:** efc9b66

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for correctness. No scope creep.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- v2 channel contract fully wired, ready for Phase 12 media pipeline
- InboundMessage envelopes flow through BotInstance and ChannelRelay
- Capabilities exposed via API for UI/routing decisions

---
*Phase: 11-channel-contract-v2*
*Completed: 2026-03-13*

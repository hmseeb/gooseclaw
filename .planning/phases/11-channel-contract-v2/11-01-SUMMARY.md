---
phase: 11-channel-contract-v2
plan: 01
subsystem: channel-system
tags: [inbound-message, outbound-adapter, channel-capabilities, graceful-degradation, tdd]

requires:
  - phase: 10-multi-bot-lifecycle
    provides: "Bot lifecycle API, BotManager, channel plugin system"
provides:
  - "InboundMessage channel-agnostic envelope class"
  - "OutboundAdapter base class with graceful degradation"
  - "ChannelCapabilities declarative feature flags"
  - "LegacyOutboundAdapter backward compat shim"
affects: [11-02-wiring, phase-12-media-pipeline, phase-13-telegram-media]

tech-stack:
  added: []
  patterns: [graceful-degradation-in-base-class, capability-declaration, legacy-adapter-shim]

key-files:
  created: []
  modified:
    - docker/gateway.py
    - docker/test_gateway.py

key-decisions:
  - "Plain classes (no dataclasses/ABC) to match existing code patterns"
  - "Graceful degradation built into OutboundAdapter base, not a separate layer"
  - "LegacyOutboundAdapter wraps send(text) with single override of send_text"

patterns-established:
  - "OutboundAdapter: required send_text(), optional send_image/voice/file/buttons with text fallback"
  - "ChannelCapabilities: kwargs-based construction with to_dict() for API serialization"

requirements-completed: [MEDIA-01, MEDIA-02, MEDIA-03, MEDIA-04]

duration: 3min
completed: 2026-03-13
---

# Phase 11 Plan 01: Channel Contract v2 Types Summary

**InboundMessage, OutboundAdapter, ChannelCapabilities, LegacyOutboundAdapter: four stdlib-only classes with graceful degradation built into the base**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-13T12:03:33Z
- **Completed:** 2026-03-13T12:06:33Z
- **Tasks:** 2 (TDD: RED then GREEN)
- **Files modified:** 2

## Accomplishments
- InboundMessage normalizes user_id, text, media, metadata with has_media/has_text properties
- OutboundAdapter base class with send_text (required), send_image/voice/file/buttons degrading to text
- ChannelCapabilities with 7 fields (supports_images, supports_voice, supports_files, supports_buttons, supports_streaming, max_file_size, max_text_length) and to_dict()
- LegacyOutboundAdapter wraps old send(text) functions as OutboundAdapter
- 23 new tests, all passing, 335 total

## Task Commits

1. **Task 1: RED -- Write failing tests** - part of 87b88d8 (test + feat combined)
2. **Task 2: GREEN -- Implement four classes** - `87b88d8` (feat)

## Files Created/Modified
- `docker/gateway.py` - Four new classes above channel plugin system comment
- `docker/test_gateway.py` - TestInboundMessage, TestOutboundAdapter, TestChannelCapabilities, TestGracefulDegradation, TestLegacyOutboundAdapter

## Decisions Made
- Plain classes over dataclasses/ABC to match existing project patterns (stdlib only constraint)
- Graceful degradation in OutboundAdapter base class, not in a separate dispatcher layer
- user_id coerced to str in InboundMessage constructor for safety

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Four types ready for wiring in Plan 02
- No blockers

---
*Phase: 11-channel-contract-v2*
*Completed: 2026-03-13*

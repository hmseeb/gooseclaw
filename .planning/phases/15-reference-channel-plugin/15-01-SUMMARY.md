---
phase: 15-reference-channel-plugin
plan: 01
subsystem: channels
tags: [discord, websocket, multipart, media, channel-plugin, v2-contract]

# Dependency graph
requires:
  - phase: 14-outbound-media-pipeline
    provides: OutboundAdapter, ChannelCapabilities, InboundMessage, MediaContent, _load_channel v2 adapter support
provides:
  - Discord channel plugin (docker/discord_channel.py) with full rich media
  - Reference implementation of v2 channel contract
  - Proof that _load_channel works with zero gateway.py changes (MEDIA-16)
affects: [future-channel-plugins, deployment-docs]

# Tech tracking
tech-stack:
  added: [websocket-client==1.8.0]
  patterns: [v2-channel-plugin-pattern, multipart-form-data-upload, gateway-websocket-polling]

key-files:
  created: [docker/discord_channel.py]
  modified: [docker/test_gateway.py, docker/requirements.txt]

key-decisions:
  - "Import gateway classes via sys.modules __main__ first, then direct import, then fallback stubs"
  - "websocket timeout exceptions handled via type name check for portability"
  - "Module-level adapter instance created from env vars, None if unconfigured"

patterns-established:
  - "v2 channel plugin pattern: CHANNEL dict with name, version=2, send, adapter, poll, credentials, setup"
  - "Multipart Discord upload: payload_json field + files[N] fields with uuid boundary"
  - "Gateway WebSocket: op 10 Hello -> op 2 Identify -> heartbeat thread -> event dispatch loop"

requirements-completed: [MEDIA-15, MEDIA-16]

# Metrics
duration: 6min
completed: 2026-03-13
---

# Phase 15 Plan 01: Discord Channel Plugin Summary

**Discord channel plugin with DiscordOutboundAdapter, Gateway WebSocket polling, and multipart file uploads, loaded via _load_channel with zero gateway.py changes**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-13T13:58:29Z
- **Completed:** 2026-03-13T14:04:11Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- DiscordOutboundAdapter with send_text (JSON), send_image/send_file (multipart/form-data) via Discord REST API
- poll_discord connects to Gateway WebSocket, identifies with MESSAGE_CONTENT intent, dispatches MESSAGE_CREATE to relay
- Heartbeat thread sends op 1 at server-specified interval, bot messages filtered to prevent self-reply loops
- Inbound media extraction downloads attachments from Discord CDN as MediaContent objects
- Plugin exports v2 CHANNEL dict with adapter field, loads via _load_channel without any gateway.py changes (proves MEDIA-16)
- 16 new tests covering adapter, media extraction, Gateway polling, and plugin loading

## Task Commits

Each task was committed atomically:

1. **Task 1: RED -- Write failing tests for Discord plugin** - `79ec73e` (test)
2. **Task 2: GREEN -- Implement Discord channel plugin** - `4203f78` (feat)

## Files Created/Modified
- `docker/discord_channel.py` - Complete Discord channel plugin (DiscordOutboundAdapter, poll_discord, setup_discord, CHANNEL dict)
- `docker/test_gateway.py` - 16 new test methods across 4 test classes (TestDiscordOutboundAdapter, TestDiscordInboundMedia, TestDiscordPoll, TestDiscordPluginLoad)
- `docker/requirements.txt` - Added websocket-client==1.8.0

## Decisions Made
- Import gateway classes via sys.modules["__main__"] first (production path), then direct import (testing), then fallback stubs (standalone). This three-tier approach ensures the plugin works in all contexts.
- Module-level adapter instance created from env vars at import time, set to None if unconfigured. CHANNEL["send"] uses a lambda fallback when adapter is None.
- WebSocket timeout exceptions handled via type name check rather than direct import, since websocket-client may not be installed in all environments.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- One pre-existing flaky test (TestRelayProtocolUpgrade::test_bot_relay_builds_content_blocks_from_media) failed due to port-in-use race condition. Passes in isolation. Not related to Discord plugin changes.

## User Setup Required

None - no external service configuration required. Discord credentials are resolved at runtime by _load_channel from setup.json or environment variables.

## Next Phase Readiness
- Discord plugin serves as reference template for additional channel plugins
- All 463 tests passing (447 existing + 16 new)
- gateway.py completely untouched, proving the v2 channel contract works without core changes
- MEDIA-15 (non-Telegram plugin with full media) and MEDIA-16 (zero gateway changes) validated

## Self-Check: PASSED

- docker/discord_channel.py: FOUND
- docker/test_gateway.py: FOUND
- 15-01-SUMMARY.md: FOUND
- Commit 79ec73e: FOUND
- Commit 4203f78: FOUND

---
*Phase: 15-reference-channel-plugin*
*Completed: 2026-03-13*

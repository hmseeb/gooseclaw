---
phase: 12-inbound-media-pipeline
plan: 02
subsystem: telegram-poll-relay
tags: [media, wiring, poll-loop, relay, tdd]
dependency_graph:
  requires: [12-01-MediaContent]
  provides: [media-download-in-relay, media-only-flow, no-MEDIA_REPLY]
  affects: [docker/gateway.py, docker/test_gateway.py]
tech_stack:
  added: []
  patterns: [deferred-download, file_id-references, relay-thread-download]
key_files:
  created: []
  modified: [docker/gateway.py, docker/test_gateway.py]
decisions:
  - Downloads happen in relay thread, NOT poll loop (keeps poll responsive)
  - Poll loop builds file_id reference dicts, relay thread downloads + creates MediaContent
  - MEDIA_REPLY no longer sent to paired users (constant kept for backward compat)
  - Media-only messages flow through to relay (text="" is fine)
  - Updated 3 existing tests that asserted old MEDIA_REPLY behavior
metrics:
  duration: 3min
  completed: 2026-03-13
---

# Phase 12 Plan 02: Wire Media Download into Poll/Relay Paths Summary

Replaced MEDIA_REPLY rejection with actual download pipeline in both BotInstance and legacy poll loops.

## What Was Done

### Task 1: RED -- Failing tests (9 tests)
- TestBotMediaDownload: 7 tests for photo/voice/document download, MEDIA_REPLY removal, failure handling, caption preservation, unpaired silence
- TestLegacyPollMediaDownload: 2 tests for legacy path photo download and media-only flow

### Task 2: GREEN -- Implementation
- BotInstance._poll_loop: replaced bare type stubs with file_id reference dicts using _extract_file_info
- BotInstance._poll_loop: removed MEDIA_REPLY send for paired users with media
- BotInstance._do_message_relay: downloads media at start of relay (before goose web call)
- Legacy _telegram_poll_loop: mirror changes with _do_media_relay inner function
- Legacy _do_relay: added media download block for text+media messages
- Updated 3 existing tests to match new behavior (no MEDIA_REPLY, file_id refs)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated 3 existing tests asserting removed behavior**
- **Found during:** Task 2
- **Issue:** test_paired_user_photo_gets_canned_reply, test_paired_user_voice_gets_canned_reply asserted MEDIA_REPLY was sent; test_media_message_creates_inbound_with_media checked old {"type": "image"} format
- **Fix:** Updated tests to assert MEDIA_REPLY is NOT sent and media uses file_id reference format
- **Files modified:** docker/test_gateway.py

## Test Results

- 9 new tests added, all passing
- 3 existing tests updated to match new behavior
- Total: 380 tests, all passing

## Commit

- `0c25748`: feat(12-02): wire media download into poll/relay paths, remove MEDIA_REPLY rejection

---
phase: 16-watcher-engine
plan: 02
subsystem: api
tags: [watchers, webhook, feed, rss, hmac, session-reuse, smart-processing]

# Dependency graph
requires:
  - phase: 16-watcher-engine
    provides: Watcher CRUD, JSON persistence, passthrough template processing
provides:
  - Smart processing with LLM session reuse (_process_smart)
  - Fire dispatch with tier routing (_fire_watcher)
  - Webhook routing with HMAC-SHA256 verification (_handle_webhook_incoming)
  - Feed polling with SHA-256 hash change detection (_check_feed_watcher)
  - RSS 2.0 and Atom feed parsing (_parse_rss)
affects: [16-03, watcher-engine-loop, webhook-api-endpoint]

# Tech tracking
tech-stack:
  added: [hmac, xml.etree.ElementTree]
  patterns: [session reuse per watcher, HMAC webhook verification, hash-based feed diffing, daemon thread per webhook fire]

key-files:
  created: []
  modified: [docker/gateway.py, docker/test_gateway.py]

key-decisions:
  - "Session reuse: stored _session_id in watcher dict to prevent session accumulation (Research Pitfall 3)"
  - "Stale session retry: detect session not found/expired errors, create fresh session and retry once"
  - "HMAC verification uses hmac.compare_digest for timing-safe comparison"
  - "Feed content parsing cascade: JSON > RSS/Atom > raw text"
  - "Webhook fires in daemon threads for non-blocking delivery"
  - "Regex filter applied on serialized list items for feed watchers"

patterns-established:
  - "_process_smart builds user_text from prompt + truncated payload (4000 char limit)"
  - "_fire_watcher wraps processing in try/except, tracks fire_count/last_fired/last_error"
  - "_handle_webhook_incoming matches by source suffix, verifies HMAC if webhook_secret set"
  - "_check_feed_watcher compares SHA-256 hash before firing, stores last_hash"

requirements-completed: [WATCH-05, WATCH-06, WATCH-07, WATCH-08]

# Metrics
duration: 9min
completed: 2026-03-14
---

# Phase 16 Plan 02: Smart Processing + Webhooks + Feeds Summary

**Smart LLM processing with session reuse, webhook routing with HMAC verification, and feed polling with SHA-256 change detection and RSS/Atom parsing**

## Performance

- **Duration:** 9 min
- **Started:** 2026-03-14T16:32:57Z
- **Completed:** 2026-03-14T16:41:57Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Smart processing (tier 2) with goose session reuse per watcher, preventing session accumulation
- Webhook routing with name-based matching, HMAC-SHA256 verification, daemon thread dispatch
- Feed polling with hash-based change detection, JSON/RSS/Atom/raw parsing, regex filtering
- 26 new tests across 4 test classes (TestSmartProcess, TestFireWatcher, TestWebhookRouting, TestFeedWatcher)

## Task Commits

Each task was committed atomically:

1. **Task 1: TDD smart processing + fire dispatch**
   - `83adc80` (test) RED: failing tests for smart processing and fire dispatch
   - `db0bdca` (feat) GREEN: implement smart processing with session reuse and fire dispatch
2. **Task 2: TDD webhook routing + feed polling**
   - `d82fddc` (test) RED: failing tests for webhook routing and feed polling
   - `f955c5a` (feat) GREEN: implement webhook routing, feed polling, and RSS parsing

## Files Created/Modified
- `docker/gateway.py` - Smart processing, fire dispatch, webhook routing, feed polling, RSS parsing
- `docker/test_gateway.py` - 26 new tests across 4 test classes

## Decisions Made
- Session reuse stored as _session_id in watcher dict (not a separate registry) for simplicity
- Stale session detection via error message substring matching ("session not found", "session expired")
- HMAC uses hmac.compare_digest for timing-safe comparison against X-Hub-Signature-256 header
- Feed content parsing tries JSON first, then RSS/Atom XML, then raw text truncated to 2000 chars
- Webhook fires dispatched in daemon threads so HTTP response isn't blocked

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Complete event processing pipeline ready for engine loop integration (Plan 03)
- _fire_watcher bridges input sources (webhook/feed) to notification delivery
- _handle_webhook_incoming ready for HTTP endpoint wiring
- _check_feed_watcher ready for periodic polling loop

---
*Phase: 16-watcher-engine*
*Completed: 2026-03-14*

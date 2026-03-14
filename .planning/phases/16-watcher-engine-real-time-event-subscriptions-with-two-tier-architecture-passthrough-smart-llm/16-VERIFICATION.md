---
phase: 16-watcher-engine
verified: 2026-03-14T08:30:00Z
status: passed
score: 27/27 must-haves verified
re_verification: false
---

# Phase 16: Watcher Engine Verification Report

**Phase Goal:** Real-time event subscriptions with two-tier architecture (passthrough + smart/LLM)
**Verified:** 2026-03-14T08:30:00Z
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

#### Plan 01 Truths (WATCH-01 through WATCH-04)

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 1 | `create_watcher()` validates type (webhook/feed/stream), rejects invalid, returns (watcher, error) | VERIFIED | `gateway.py:3686-3688` — explicit type check, returns `(None, error)` on invalid |
| 2 | `create_watcher()` rejects duplicate IDs | VERIFIED | `gateway.py:3682-3684` — checks `_watchers` list before append |
| 3 | `delete_watcher()` removes by ID, returns True/False | VERIFIED | `gateway.py:3722-3731` — list comprehension filter, returns bool |
| 4 | `list_watchers()` returns all watchers | VERIFIED | `gateway.py:3734-3737` — returns `[dict(w) for w in _watchers]` (defensive copy) |
| 5 | `_process_passthrough()` renders `string.Template` with `safe_substitute` on flattened payload | VERIFIED | `gateway.py:3776-3785` — full implementation with `_flatten_dict` + `safe_substitute` |
| 6 | `_process_passthrough()` returns JSON dump when no template is set | VERIFIED | `gateway.py:3779-3780` — `return json.dumps(data, indent=2)[:2000]` |
| 7 | Watchers persist to `watchers.json` and reload on `_load_watchers()` | VERIFIED | `gateway.py:3649-3675` — atomic write via `.tmp` + `os.replace`, symmetric load |

#### Plan 02 Truths (WATCH-05 through WATCH-08)

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 8 | `_process_smart()` reuses stored `session_id` from watcher dict, creates new only on first fire or stale session | VERIFIED | `gateway.py:3794-3815` — checks `watcher.get("_session_id")`, retries with new session on stale error |
| 9 | `_process_smart()` returns error string when session creation fails | VERIFIED | `gateway.py:3800` — `return "Error: could not create goose session"` |
| 10 | `_fire_watcher()` dispatches to passthrough tier when `smart=False` | VERIFIED | `gateway.py:3825-3828` — `if watcher.get("smart")` gate |
| 11 | `_fire_watcher()` dispatches to smart tier when `smart=True` | VERIFIED | `gateway.py:3825-3826` — calls `_process_smart` |
| 12 | `_fire_watcher()` calls `notify_all` with channel targeting and watcher name prefix | VERIFIED | `gateway.py:3834-3836` — `f"[{watcher_name}] {message}"` + `notify_all(..., channel=watcher.get("channel"))` |
| 13 | `_fire_watcher()` updates `fire_count` and `last_fired` on success | VERIFIED | `gateway.py:3838-3840` — increments `fire_count`, sets `last_fired`, clears `last_error` |
| 14 | `handle_webhook_incoming()` finds matching webhook watchers by path suffix | VERIFIED | `gateway.py:3872-3877` — filters by `type=="webhook"`, `enabled`, `source.endswith(webhook_name)` |
| 15 | `handle_webhook_incoming()` returns 404 when no watchers match | VERIFIED | `gateway.py:8349-8352` — HTTP handler returns 404 on `count == 0` |
| 16 | `_check_feed_watcher()` fires only when content hash changes | VERIFIED | `gateway.py:3992-4011` — SHA-256 hash comparison, fires on mismatch |
| 17 | `_check_feed_watcher()` skips when hash unchanged | VERIFIED | `gateway.py:3992-3994` — `return` early when `content_hash == last_hash` |

#### Plan 03 Truths (WATCH-08, WATCH-09, WATCH-10)

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 18 | `POST /api/watchers` creates a watcher and returns 201 with watcher JSON | VERIFIED | `gateway.py:8286-8302` — `handle_create_watcher` returns 201 |
| 19 | `GET /api/watchers` returns list of all watchers | VERIFIED | `gateway.py:8304-8311` — returns `{"watchers": ..., "count": ...}` |
| 20 | `DELETE /api/watchers/<id>` removes watcher and returns 200 | VERIFIED | `gateway.py:8313-8322` — returns 200 or 404 |
| 21 | `PUT /api/watchers/<id>` updates watcher fields and returns 200 | VERIFIED | `gateway.py:8324-8340` — returns 200 or 404 |
| 22 | `POST /api/webhooks/<name>` routes payload to matching webhook watchers and returns 200 | VERIFIED | `gateway.py:8342-8352` — returns `{"accepted": True, "watchers": count}` |
| 23 | `POST /api/webhooks/<name>` returns 404 when no watchers match | VERIFIED | `gateway.py:8351-8352` — explicit 404 with error message |
| 24 | Watcher engine loop ticks feed watchers at their `poll_seconds` interval | VERIFIED | `gateway.py:4019-4044` — `_watcher_engine_tick` checks `now - last_ts >= poll_seconds` |
| 25 | `start_watcher_engine()` starts daemon thread, called during gateway startup | VERIFIED | `gateway.py:4062-4073`, called at `8959-8960` and `7735-7736` |
| 26 | `_load_watchers()` called during gateway startup | VERIFIED | `gateway.py:8959` — alongside `_load_jobs()` in main startup |
| 27 | No regressions in existing tests from watcher engine addition | VERIFIED | 543 non-watcher tests pass; 13 failures in `TestRestRelay`/`TestBotPairing`/`TestChannelRelayLocks` predate phase 16 (introduced via `a288c73` worktree merge) |

**Score: 27/27 truths verified**

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `docker/gateway.py` | Watcher CRUD, passthrough, smart processing, fire dispatch, webhook routing, feed polling, engine loop, HTTP endpoints, startup wiring | VERIFIED | All 19 watcher functions present at lines 3649-4079; HTTP handlers at 8284-8352; startup at 8958-8960 |
| `docker/test_gateway.py` | Tests for all watcher subsystems | VERIFIED | 13 test classes (lines 6983-7793); 65 watcher tests all pass |

---

### Key Link Verification

#### Plan 01 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `docker/gateway.py` | `data/watchers.json` | `_save_watchers/_load_watchers` | WIRED | `gateway.py:3649-3675` — `_WATCHERS_FILE = os.path.join(DATA_DIR, "watchers.json")` at line 917; atomic write + load both implemented |

#### Plan 02 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `_fire_watcher` | `notify_all` | channel-targeted notification delivery | WIRED | `gateway.py:3836` — `notify_all(full_message, channel=watcher.get("channel"))` |
| `_process_smart` | `_relay_to_goose_web` | LLM relay for tier 2 processing | WIRED | `gateway.py:3804-3805` — `_relay_to_goose_web(user_text, session_id, channel=...)` |
| `_process_smart` | `watcher['_session_id']` | session reuse to prevent accumulation | WIRED | `gateway.py:3794-3813` — read, write, and retry logic all present |
| `_check_feed_watcher` | `_fire_watcher` | fires on content hash change | WIRED | `gateway.py:4011` — `_fire_watcher(watcher, data)` called after hash mismatch |

#### Plan 03 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `do_POST/do_GET/do_DELETE/do_PUT` | `create_watcher/list_watchers/delete_watcher/update_watcher` | HTTP route dispatch | WIRED | `gateway.py:7339,7413,7432,7448` — all four HTTP verbs route to watcher handlers |
| `do_POST` | `_handle_webhook_incoming` | HTTP route for `/api/webhooks/` | WIRED | `gateway.py:7415-7420` — `path.startswith("/api/webhooks/")` branches to `handle_webhook_incoming` |
| `_watcher_engine_loop` | `_check_feed_watcher` | periodic polling based on `poll_seconds` | WIRED | `gateway.py:4041` — daemon thread spawned per due feed watcher in tick |
| gateway startup | `_load_watchers/start_watcher_engine` | called alongside `_load_jobs/start_job_engine` at boot | WIRED | `gateway.py:8958-8960` (main startup) + `7735-7736` (post-config restart) |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|---------|
| WATCH-01 | 16-01 | Watcher CRUD: create, list, delete, update | SATISFIED | `create_watcher`, `list_watchers`, `delete_watcher`, `update_watcher` all implemented and tested |
| WATCH-02 | 16-01 | JSON persistence with atomic write | SATISFIED | `_load_watchers`/`_save_watchers` with `.tmp` + `os.replace` pattern |
| WATCH-03 | 16-01 | Tier 1 passthrough template processing | SATISFIED | `_process_passthrough` with `string.Template.safe_substitute`, `_flatten_dict`, double-brace conversion |
| WATCH-04 | 16-01 | Dict flattening for nested payloads | SATISFIED | `_flatten_dict` at line 3758; tested in `TestPassthroughProcess` |
| WATCH-05 | 16-02 | Tier 2 smart processing with session reuse | SATISFIED | `_process_smart` stores `_session_id` in watcher, retries on stale session |
| WATCH-06 | 16-02 | Fire dispatch to correct tier via `_fire_watcher` | SATISFIED | `_fire_watcher` routes on `watcher["smart"]` flag |
| WATCH-07 | 16-02 | Webhook routing with HMAC verification | SATISFIED | `_handle_webhook_incoming` + `_verify_webhook_signature` |
| WATCH-08 | 16-02, 16-03 | Feed polling with SHA-256 hash change detection | SATISFIED | `_check_feed_watcher` at line 3973; engine loop drives polling |
| WATCH-09 | 16-03 | HTTP API at `/api/watchers` (full CRUD) + `/api/webhooks/<name>` | SATISFIED | All endpoints in `do_GET`, `do_POST`, `do_PUT`, `do_DELETE`; handlers at lines 8284-8352 |
| WATCH-10 | 16-03 | Startup wiring and engine loop | SATISFIED | `_watcher_engine_loop`, `start_watcher_engine`, `_load_watchers` called at gateway startup |

---

### Anti-Patterns Found

None. Scanned the watcher section (lines 3646-4080 and 8284-8352) for TODO/FIXME/placeholder comments, empty implementations, stub returns, and console.log-only handlers. None found.

---

### Human Verification Required

#### 1. HMAC Webhook Signature End-to-End

**Test:** Set up a watcher with `webhook_secret`, POST to `/api/webhooks/<name>` with a valid `X-Hub-Signature-256` header.
**Expected:** Watcher fires; posting with wrong signature returns no fire (request still returns 200 if other non-secret watchers match, or 404 if none match).
**Why human:** HMAC header construction varies by client; automated test mocks the signature but can't verify the real GitHub/Stripe webhook flow works with an actual payload.

#### 2. Feed Polling Interval Jitter on Cold Start

**Test:** Create a feed watcher with `poll_seconds=300`, restart gateway, observe logs for `[watchers]` output over first 60 seconds.
**Expected:** First poll fires between `poll_seconds - 60` and `poll_seconds + 0` seconds after startup (not immediately), and subsequent polls respect the interval.
**Why human:** Jitter randomness cannot be deterministically verified at the log level without observing real timing.

#### 3. Smart Tier LLM Session Reuse Across Fires

**Test:** Create a smart watcher, fire it twice. Observe `_session_id` is retained in the watcher dict and the second fire does NOT call `_create_goose_session` again.
**Expected:** Only one session created for N fires (until session goes stale).
**Why human:** Requires live Goose backend; unit tests mock `_create_goose_session` and `_relay_to_goose_web`.

---

### Regression Note

The 13 failing tests in the full test suite (`TestRestRelay`, `TestRestRelayStreaming`, `TestChannelRelayLocks`, `TestCustomCommandRegistration`, `TestCustomCommandConflicts`, `TestBotPairing`) are **not caused by phase 16**. They were introduced by the `a288c73` worktree merge (`feat: support file attachments and multi-image media groups` + `feat: add typing indicator support`) which landed after phase 16 commits. Phase 16 watcher tests (65 tests) all pass cleanly.

---

_Verified: 2026-03-14T08:30:00Z_
_Verifier: Claude (gsd-verifier)_

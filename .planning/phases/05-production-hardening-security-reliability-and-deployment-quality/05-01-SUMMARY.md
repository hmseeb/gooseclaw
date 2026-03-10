---
phase: 05-production-hardening
plan: "01"
subsystem: security
tags: [cors, auth, first-boot, credential-masking, gateway]
dependency_graph:
  requires: []
  provides: [hardened-cors, first-boot-lockdown, credential-masking, notify-auth]
  affects: [docker/gateway.py, docker/setup.html]
tech_stack:
  added: []
  patterns:
    - Origin-aware CORS (echo same-host origin, omit for cross-origin)
    - _is_first_boot() guard pattern for pre-configuration API lockdown
    - Fixed placeholder masking ("********") + boolean _set fields
key_files:
  modified:
    - docker/gateway.py
    - docker/setup.html
decisions:
  - Origin-aware CORS echoes the request Origin only when it matches the Host header; requests with no Origin omit the CORS header entirely — no wildcard ever set
  - First-boot lockdown uses _is_first_boot() which checks both env-var providers and setup.json; any configured env-var bypasses first-boot to support Railway/Docker deployments
  - Dual credential masking approach: "********" placeholder (keeps typeof val === 'string' for frontend compat) plus boolean _set fields (canonical indicator for smarter UI)
  - telegram_bot_token removed from config response entirely; telegram_bot_token_set bool is sufficient for UI
  - handle_notify() gets unconditional check_auth() (not just post-setup); same for handle_telegram_pair()
metrics:
  duration: ~12 min
  completed: "2026-03-11"
  tasks_completed: 2
  files_modified: 2
---

# Phase 5 Plan 01: CORS, First-Boot Lockdown, and Credential Masking Summary

**One-liner:** Closed four critical attack vectors — CORS wildcard, unauthenticated first-boot APIs, partial key leakage, and unauthenticated notify — with origin-aware headers, _is_first_boot() guards, fixed-placeholder masking, and mandatory auth on notify.

## What Was Built

### Task 1: CORS wildcard fix and first-boot API lockdown (commit c180297)

**CORS:** `send_json()` no longer sets `Access-Control-Allow-Origin: *`. Instead it reads the `Origin` and `Host` headers and only echoes the origin back when they match (same-host). Cross-origin requests receive no CORS header and are blocked by the browser. `do_OPTIONS()` was also updated to handle `/api/*` preflight requests directly (not proxy to goose) with the same same-host echo logic.

**First-boot lockdown:** Added `_is_first_boot()` helper that returns `True` when `load_setup()` is `None` AND no env-var provider is configured. Applied to `handle_notify()`, `handle_notify_status()`, `handle_telegram_status()`, and `handle_telegram_pair()` — all return `{"error": "agent not configured yet"}` with 403 before setup is complete.

**Notify auth:** `handle_notify()` now has an unconditional `check_auth()` gate (not conditioned on `load_setup()` returning non-None). Same for `handle_telegram_pair()`.

### Task 2: Server-side credential masking and setup.html compatibility (commit 51f7064)

**gateway.py `handle_get_config()`:** Replaced partial masking (`val[:6] + "..." + val[-4:]`) with:
- Fixed `"********"` placeholder for all secret values (no key material ever returned)
- Boolean companion fields: `api_key_set`, `claude_setup_token_set`, `web_auth_token_set`, etc.
- `telegram_bot_token` removed from response; `telegram_bot_token_set` boolean added instead
- `saved_keys` dict: each set key returns `"********"`, complex (dict) values mask each sub-field
- `saved_keys_set` dict added with `provider_id -> bool` mapping

**setup.html:** Added `savedKeysSet = {}` state variable. `loadExistingConfig` fetch handler now populates `savedKeysSet` from `data.config.saved_keys_set`. `updateDashboardCredField()` checks `savedKeysSet[pt] === true` as primary indicator, with fallback to `typeof savedKeys[pt] === 'string'` for backwards compatibility. When a key is set, placeholder reads "key already set — leave blank to keep current".

## Deviations from Plan

None - plan executed exactly as written.

## Self-Check

- [x] `docker/gateway.py` — modified
- [x] `docker/setup.html` — modified
- [x] commit c180297 — CORS + first-boot lockdown
- [x] commit 51f7064 — credential masking + setup.html
- [x] No syntax errors (`python3 -m py_compile docker/gateway.py` passes)
- [x] Task 1 verification script: PASS
- [x] Task 2 verification script: PASS

## Self-Check: PASSED

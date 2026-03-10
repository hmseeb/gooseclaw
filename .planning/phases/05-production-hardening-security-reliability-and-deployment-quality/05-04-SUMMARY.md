---
phase: 05-production-hardening
plan: "04"
subsystem: gateway-api
tags: [rate-limiting, security, validation, health-check, reliability]
dependency_graph:
  requires: [05-01, 05-03]
  provides: [rate-limiting, config-schema-validation, deep-health-check, readiness-probe]
  affects: [docker/gateway.py]
tech_stack:
  added: []
  patterns:
    - Sliding-window per-IP rate limiter (Python stdlib, threading.Lock)
    - Config schema validation with specific error messages
    - Deep health check probing subprocess via HTTP
    - Separate liveness (/api/health) vs readiness (/api/health/ready) probes
key_files:
  created: []
  modified:
    - docker/gateway.py
decisions:
  - "Three separate rate limiter instances with different limits: api (60/min), auth (5/min), notify (10/min)"
  - "Rate limiter uses sliding window (not token bucket) for natural burst handling"
  - "validate_setup_config() placed before auth-token logic in handle_save() to fail fast"
  - "/api/health returns 200 for both 'ok' and 'setup_required' -- Railway liveness probe should not 503 during first boot"
  - "Cleanup daemon thread runs every 5 minutes to free stale IP entries from memory"
metrics:
  duration: "~2 min"
  completed: "2026-03-11"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 1
---

# Phase 05 Plan 04: Rate Limiting, Config Validation, Deep Health Check Summary

Per-IP sliding-window rate limiting (60/5/10 req/min tiers), config schema validation with specific error messages, and deep health check that probes the goose web subprocess instead of lying.

## Tasks Completed

| # | Task | Commit | Status |
|---|------|--------|--------|
| 1 | Add per-IP rate limiting to API endpoints | d9f2177 | Complete |
| 2 | Add config schema validation and deep health check | 9d0b553 | Complete |

## What Was Built

### Task 1: Per-IP Rate Limiting

Added `RateLimiter` class using Python stdlib only (`collections.defaultdict`, `threading.Lock`). Sliding-window algorithm: per-IP timestamp list, expire entries older than the window, count remaining.

Three instances at module level:
- `api_limiter`: 60 req/60s — applied to all `GET /api/*` routes
- `auth_limiter`: 5 req/60s — applied to `handle_save()` and `handle_validate()`
- `notify_limiter`: 10 req/60s — applied to `handle_notify()`

Rate-limited paths return `429 {"error": "Too many requests. Try again later."}`. Static `/setup` pages and proxy requests (goose web) are NOT rate-limited. A daemon thread calls `.cleanup()` on all three limiters every 5 minutes to prevent unbounded memory growth from stale IPs.

### Task 2: Config Schema Validation

`validate_setup_config(config)` returns `(valid: bool, errors: list[str])`. Checks:
- `provider_type` present and in `env_map` registry
- Credentials required for non-local providers (skips ollama, lm-studio, docker-model-runner, ramalama)
- `telegram_bot_token` must contain `:` if provided
- `timezone` must be `Region/City` format or `UTC`
- String fields (`api_key`, `claude_setup_token`, `custom_key`, `custom_url`, `model`) max 2000 chars

`handle_save()` calls this after JSON parse, before auth-token generation. Returns `400 {"success": false, "errors": [...]}` on failure.

### Task 2: Deep Health Check

Replaced the trivial `{"status": "ok"}` health response with `handle_health()`:
- Probes goose web subprocess via `GET /api/health` on `127.0.0.1:GOOSE_WEB_PORT`
- Returns `status: "ok"` (200) when goose web is healthy
- Returns `status: "setup_required"` (200) when unconfigured (correct for Railway liveness probe)
- Returns `status: "degraded"` (503) when goose web is down after configuration

Added `/api/health/ready` readiness probe via `handle_health_ready()`:
- 200 + `{"ready": true}` only when goose web responds to health ping
- 503 + reason otherwise (suitable for Railway/Kubernetes zero-downtime deploys)

`do_GET` now routes `/api/health` to `handle_health()` and `/api/health/ready` to `handle_health_ready()`.

## Deviations from Plan

None - plan executed exactly as written.

## Key Decisions Made

1. **Rate limit only `/api/*` GET paths, not static pages or proxy traffic.** The `/setup` page is HTML — rate-limiting it would break legitimate browser refreshes. Proxy traffic to goose web has its own rate limiting.

2. **`/api/health` returns 200 for `setup_required`.** Railway's liveness probe must not receive a 503 during first-boot unconfigured state, or the container will be killed/restarted in a loop. Only `degraded` (goose web down after successful config) returns 503.

3. **`validate_setup_config` skips credential check for local providers.** Ollama, LM Studio, Docker Model Runner, and Ramalama don't require API keys — checking for one would block legitimate saves.

## Self-Check: PASSED

- FOUND: docker/gateway.py
- FOUND commit d9f2177 (Task 1: rate limiting)
- FOUND commit 9d0b553 (Task 2: config validation + health check)

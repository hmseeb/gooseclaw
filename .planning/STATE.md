# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-10)

**Core value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try
**Current focus:** Phase 3: Gateway Resilience and Live Feedback

## Current Position

Phase: 3 of 5 (Gateway Resilience and Live Feedback) -- COMPLETE
Plan: 2 of 2 in current phase (03-02 complete)
Status: Phase 3 complete
Last activity: 2026-03-10 -- Completed 03-02 (real-time startup status UI, auth recovery form)

Progress: [######----] 60%

## Performance Metrics

**Velocity:**
- Total plans completed: 3
- Average duration: ~7 min
- Total execution time: ~0.37 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-provider-ui-expansion | 2 | ~19 min | ~10 min |

**Recent Trend:**
- Last 5 plans: 4 min, ~15 min, 2 min
- Trend: -

*Updated after each plan completion*
| Phase 05 P02 | 6 | 2 tasks | 3 files |
| Phase 05 P05 | 3 | 2 tasks | 2 files |
| Phase 05 P06 | 4 | 3 tasks | 1 file |
| Phase 02 P01 | 2 | 2 tasks | 2 files |
| Phase 02 P02 | 3 | 2 tasks | 1 file |
| Phase 02 P03 | 2 | 2 tasks | 1 file |
| Phase 03 P01 | 3 | 2 tasks | 1 file |
| Phase 03 P02 | 5 | 2 tasks | 2 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Roadmap: 4 phases derived from 39 requirements at quick depth
- Roadmap: Phase 4 (Advanced) depends on Phase 2, not Phase 3, allowing parallel execution
- 01-01: PROVIDERS registry as single source of truth for all 15 provider metadata (name, icon, desc, category, pricing, keyUrl, envVar, defaultModel, models, keyPlaceholder)
- 01-01: Data-driven renderProviderGrid() replaces static HTML -- scales to any provider count without markup changes
- 01-01: buildCredFields() fully data-driven with special branches only for claude-code, ollama, azure-openai, custom
- 01-02: Model selection promoted to dedicated step-2 with datalist from PROVIDERS registry; Save button moved to Confirmation step (step-4)
- 01-02: Provider registry expanded to 23 total (added avian, litellm, venice, ovhcloud, github-copilot, lm-studio, docker-model-runner, ramalama)
- 01-02: Compact horizontal card layout with scrollable grid (max-height 420px) to keep Continue button visible
- quick-1-01: Settings dashboard on /setup for configured agents; per-field inline editing; savedKeys in-memory + persisted in setup.json
- quick-1-01: os.environ.get guards on all 4 re-hydration export paths in entrypoint.sh; Railway/Docker env vars always win
- 05-03: mktemp+shlex.quote replaces eval "$(python3 -c ...)" in entrypoint.sh -- eliminates shell injection vector
- 05-03: re.sub sanitizes vault GOOSECLAW_* variable names (YAML hyphens -> underscores)
- 05-03: SHA-256 hashing of auth tokens before storage; plaintext never on disk; get_auth_token returns (token, is_hashed) tuple
- 05-03: gateway-owns-all-auth: goose web subprocess gets random internal token; gateway verifies users against hash, proxies with internal token
- [Phase 05-02]: Keep apt-based python3-yaml; requirements.txt as version documentation and pip alternative
- [Phase 05-02]: Container runs root by default; non-root gooseclaw user created for optional --user override
- 05-01: Origin-aware CORS echoes same-host origin only; cross-origin requests receive no CORS header (browser blocks)
- 05-01: Dual credential masking -- "********" placeholder (typeof still string) plus boolean _set fields for frontend
- 05-01: _is_first_boot() guards all non-setup API endpoints before configuration is complete
- 05-01: telegram_bot_token removed from config response; only boolean telegram_bot_token_set exposed
- 05-04: Three rate limiter tiers: api (60/min), auth (5/min), notify (10/min); sliding window, stdlib only
- 05-04: /api/health returns 200 for setup_required (unconfigured) to avoid Railway restart loops; only 503 when goose web dies after config
- 05-04: validate_setup_config skips credential check for local providers (ollama, lm-studio, docker-model-runner, ramalama)
- [Phase 05]: goose_health_monitor runs as daemon thread checking every 15s; backoff doubles per failure capped at 120s
- [Phase 05]: os.replace() atomic write for setup.json; .tmp + rename prevents corruption; .bak backup before overwrite
- [Phase 05]: PROXY_TIMEOUT defaults 60s (was 300s); SSE connections exempt from timeout; entrypoint waits for gateway on SIGTERM
- 05-06: SECURITY_HEADERS dict applied in send_json(), handle_setup_page(), and proxy_to_goose() -- all response paths covered
- 05-06: unsafe-inline in CSP script-src accepted; CSP still blocks frame-ancestors and external origins; HSTS conditional on RAILWAY_ENVIRONMENT
- 05-06: log_request() override with _request_start timing; _internal_error() helper for sanitized 500s; error codes INTERNAL_ERROR/RATE_LIMITED/INVALID_CONFIG
- 05-06: _sanitize_string() applied to all POST endpoints (strip, truncate 2000 chars, remove control chars except \\n and \\t)
- 02-01: azure-openai setup.html saves azure_key (not api_key) and azure_endpoint; field_map translates to AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT
- 02-01: github-copilot device flow at runtime; api_key -> GITHUB_TOKEN in field_map and rehydration for manual config scenarios
- 02-01: local providers (ollama/lm-studio/docker-model-runner/ramalama) all share ollama_host key; OLLAMA_HOST exported if present
- 02-01: field_map pattern in apply_config: targeted override dict preserves backward compat for single-key providers
- 02-02: validationPassed flag tracks whether credentials tested; disabled Save button until true; reset on provider switch
- 02-02: advanceFromCredentials() gate: blocks empty API keys, validates URL format for local providers, auto-passes for github-copilot
- 02-02: prefillWizard(config) centralizes reconfigure pre-fill; secrets shown as masked placeholder ('already set'), value never sent to DOM
- 02-02: saveConfig() omits empty credential fields during reconfigure so backend never receives empty string override of existing secrets
- 02-03: credential extraction triple-fallback order: ALLCAPS_ENV_VAR > snake_case_frontend_name > legacy_field (azure_endpoint, ollama_host, litellm_host added)
- 02-03: claude-code message includes 'claude setup-token' CLI command and 'Validation must be done manually after saving'
- 02-03: github-copilot attempts real GitHub API validation if GITHUB_TOKEN provided; skip_validation fallback for device flow
- 03-01: Stderr captured via subprocess.PIPE with daemon reader thread; forwarded to sys.stderr for container logs AND ring buffer for API
- 03-01: /api/setup/status requires no auth (needed before user authenticates during startup)
- 03-01: proxy_to_goose() 503 returns JSON with state/message/error/retry_after instead of static text (GATE-05)
- 03-01: Auth recovery gated by GOOSECLAW_RECOVERY_SECRET env var; returns 404 when not configured
- 03-01: secrets.compare_digest for recovery secret comparison prevents timing attacks
- 03-02: Open Chat button hidden until /api/setup/status returns state=ready
- 03-02: Recovery page (/setup?recover) served without auth via query param bypass in gateway.py
- 03-02: 401 response body includes /setup?recover hint for discoverability
- 03-02: lastSavedConfig captured before fetch so retryStartup() can re-POST without re-entry

### Pending Todos

None yet.

### Roadmap Evolution

- Phase 5 added: Production hardening: security, reliability, and deployment quality

### Blockers/Concerns

- goose web is experimental and may crash -- Phase 3 addresses resilience
- Python stdlib only constraint limits validation options in gateway.py

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 1 | Setup wizard settings dashboard with config editing, env var priority fix, and provider key persistence | 2026-03-10 | 720048c | [1-setup-wizard-settings-dashboard-with-con](./quick/1-setup-wizard-settings-dashboard-with-con/) |

## Session Continuity

Last session: 2026-03-10
Stopped at: Completed 03-02-PLAN.md -- real-time startup status UI, auth recovery form. Phase 3 complete.
Resume file: None

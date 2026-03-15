# Roadmap: GooseClaw

## Milestones

- [x] **v1.0 Setup Wizard** - Phases 1-5 (shipped 2026-03-11)
- [x] **v2.0 Multi-Channel & Multi-Bot** - Phases 6-10 (shipped 2026-03-13)
- [x] **v3.0 Rich Media & Channel Flexibility** - Phases 11-15 (shipped 2026-03-13)
- [x] **Watcher Engine** - Phase 16 (shipped 2026-03-14)
- [x] **Vector Knowledge Base** - Phase 17 (shipped 2026-03-15)
- [ ] **v4.0 Production Hardening** - Phases 18-21 (in progress)

## Phases

<details>
<summary>v1.0 Setup Wizard (Phases 1-5) - SHIPPED 2026-03-11</summary>

- [x] **Phase 1: Provider UI Expansion** - Redesign wizard with 15+ providers in categories, model selection, and full setup flow steps (completed 2026-03-10)
- [x] **Phase 2: Validation and Env Plumbing** - Every provider validates credentials, maps env vars correctly, rehydrates on restart, and pre-fills on reconfigure (completed 2026-03-10)
- [x] **Phase 3: Gateway Resilience and Live Feedback** - goose web is monitored, auto-restarted, errors surfaced to user, real-time startup status, and auth recovery (completed 2026-03-10)
- [x] **Phase 4: Advanced Multi-Model Settings** - Lead/worker multi-model configuration for power users (completed 2026-03-11)
- [x] **Phase 5: Production Hardening** - Security, reliability, and deployment quality across gateway, entrypoint, and Dockerfile (completed 2026-03-10)

</details>

<details>
<summary>v2.0 Multi-Channel & Multi-Bot (Phases 6-10) - SHIPPED 2026-03-13</summary>

- [x] **Phase 6: Shared Infrastructure Extraction** - Extract SessionManager and CommandRouter from Telegram-specific code into shared abstractions (completed 2026-03-13)
- [x] **Phase 7: Channel Plugin Parity** - Wire channel plugins to shared infrastructure for commands, locks, cancellation, and typing indicators (completed 2026-03-13)
- [x] **Phase 8: Notification Channel Targeting** - Complete /api/notify and cron scheduler support for per-channel delivery (completed 2026-03-13)
- [x] **Phase 9: Multi-Bot Core** - Multiple Telegram bots on one gateway with independent sessions, provider routing, and backward-compatible config (completed 2026-03-13)
- [x] **Phase 10: Multi-Bot Lifecycle** - Hot-add and hot-remove bots via API without container restart (completed 2026-03-13)

</details>

<details>
<summary>v3.0 Rich Media & Channel Flexibility (Phases 11-17) - SHIPPED 2026-03-15</summary>

- [x] **Phase 11: Channel Contract v2** - InboundMessage envelope, OutboundAdapter interface, ChannelCapabilities (completed 2026-03-13)
- [x] **Phase 12: Inbound Media Pipeline** - Download + normalize incoming media from Telegram (completed 2026-03-13)
- [x] **Phase 13: Relay Protocol Upgrade** - REST /reply with multimodal content blocks (completed 2026-03-13)
- [x] **Phase 14: Outbound Rich Media** - send_image, send_voice, send_file on Telegram adapter (completed 2026-03-13)
- [x] **Phase 15: Reference Channel Plugin** - Discord plugin with full rich media (completed 2026-03-13)
- [x] **Phase 16: Watcher Engine** - Event subscriptions with passthrough + smart processing (completed 2026-03-14)
- [x] **Phase 17: Vector Knowledge Base** - Semantic retrieval MCP extension (completed 2026-03-15)

</details>

### v4.0 Production Hardening (In Progress)

**Milestone Goal:** Eliminate active security vulnerabilities, harden infrastructure for production, and establish comprehensive test coverage across gateway, shell scripts, and container lifecycle.

- [x] **Phase 18: Security Foundations** - Eliminate injection vectors, upgrade password hashing with lazy migration, seal credential leaks, add body limits and missing headers (completed 2026-03-15)
- [ ] **Phase 19: Test Infrastructure and Coverage** - Establish pytest + HTTP-level test framework and cover all gateway endpoints, shell scripts, and entrypoint bootstrap
- [ ] **Phase 20: Infrastructure Hardening** - Pin dependencies, add CVE scanning, structured JSON logging, and graceful shutdown
- [ ] **Phase 21: End-to-End Validation** - Container-level integration test proving the whole system boots and works

## Phase Details

### Phase 18: Security Foundations
**Goal**: All known security vulnerabilities are eliminated. Users authenticate with production-grade password hashing, no code path allows injection, and no secrets leak to logs.
**Depends on**: Phase 17
**Requirements**: SEC-01, SEC-02, SEC-03, SEC-04, SEC-05, SEC-06, SEC-07, HARD-04
**Success Criteria** (what must be TRUE):
  1. Shell scripts (secret.sh, entrypoint.sh) pass user input via environment variables, never via string interpolation into inline Python
  2. gateway.py _run_script uses list-based subprocess execution, never shell=True with unsanitized input
  3. Existing users with SHA-256 password hashes can log in and their hash is transparently upgraded to PBKDF2 (lazy migration)
  4. New passwords are stored as PBKDF2 with 600K iterations and random salt, never as bare SHA-256
  5. Recovery secret is written to /data/recovery_secret (file only), never printed to container stdout/stderr
  6. POST requests larger than 1MB are rejected with HTTP 413 before body is read into memory
  7. All HTTP responses include Referrer-Policy, Permissions-Policy, and Cross-Origin-Opener-Policy headers
**Plans**: 4 plans

Plans:
- [ ] 18-00: Test scaffolding with failing tests for all 8 requirements (Wave 0)
- [ ] 18-01: Shell injection fixes across secret.sh, entrypoint.sh, and gateway.py _run_script (SEC-01, SEC-02, SEC-03)
- [ ] 18-02: PBKDF2 password hashing with lazy SHA-256 migration (SEC-04, SEC-05)
- [ ] 18-03: Recovery secret leak fix, request body limits, HTTP security headers (SEC-06, SEC-07, HARD-04)

### Phase 19: Test Infrastructure and Coverage
**Goal**: Every gateway HTTP endpoint, shell script, and entrypoint bootstrap path has automated test coverage running against real server instances
**Depends on**: Phase 18
**Requirements**: TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, TEST-06, TEST-07, TEST-09
**Success Criteria** (what must be TRUE):
  1. `pytest` runs from project root with requirements-dev.txt dependencies and produces a pass/fail result
  2. Auth endpoints (login, session, rate limiting, password reset) have tests that exercise real HTTP requests against a running gateway
  3. Setup, job, and health endpoints each have dedicated test files exercising their HTTP contracts
  4. Security headers and CORS are verified across all response paths (setup, API, proxy, error)
  5. Shell scripts (job.sh, remind.sh, notify.sh, secret.sh) have tests validating argument parsing and output
  6. Entrypoint bootstrap logic (directory creation, config generation, env rehydration, provider detection) is tested
**Plans**: 4 plans

Plans:
- [ ] 19-01-PLAN.md — pytest infrastructure with live_gateway fixture and health smoke tests (TEST-09)
- [ ] 19-02-PLAN.md — Auth endpoint and security header HTTP tests (TEST-01, TEST-05)
- [ ] 19-03-PLAN.md — Setup, job, and health endpoint HTTP tests (TEST-02, TEST-03, TEST-04)
- [ ] 19-04-PLAN.md — Shell script and entrypoint bootstrap tests (TEST-06, TEST-07)

### Phase 20: Infrastructure Hardening
**Goal**: The deployment pipeline catches vulnerabilities automatically, the application logs structured JSON for observability, and the container shuts down gracefully
**Depends on**: Phase 19
**Requirements**: HARD-01, HARD-02, HARD-03, HARD-05, HARD-06
**Success Criteria** (what must be TRUE):
  1. requirements.lock exists with exact versions and hashes, pip install uses --require-hashes
  2. CVE scanning runs on dependency changes and reports known vulnerabilities
  3. Container shutdown completes within 5 seconds, force-killing hung processes after grace period
  4. Security-sensitive operations (auth attempts, config changes, errors) emit structured JSON log lines
  5. GOOSECLAW_LOG_FORMAT=json enables JSON logging across gateway, with print() calls migrated incrementally
**Plans**: TBD

Plans:
- [ ] 20-01: Dependency pinning with hash verification and CVE scanning setup (HARD-01, HARD-02)
- [ ] 20-02: Graceful shutdown with timeout and force-kill (HARD-03)
- [ ] 20-03: Structured JSON logging with incremental migration (HARD-05, HARD-06)

### Phase 21: End-to-End Validation
**Goal**: A single automated test proves the entire system works from container boot to healthy goose session
**Depends on**: Phase 18, Phase 19, Phase 20
**Requirements**: TEST-08
**Success Criteria** (what must be TRUE):
  1. Test builds and boots a GooseClaw container from the project Dockerfile
  2. Test completes the setup wizard flow (provider config, password set) via HTTP
  3. Test verifies goosed starts and /api/health returns 200 with healthy status
  4. Test runs in CI without manual intervention
**Plans**: TBD

Plans:
- [ ] 21-01: E2e container integration test (TEST-08)

## Progress

**Execution Order (v4.0):**
Phases execute in order: 18 -> 19 -> 20 -> 21
Security fixes first (18), then tests validate fixed code (19), then hardening with test safety net (20), then e2e validates everything (21).

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Provider UI Expansion | v1.0 | 2/2 | Complete | 2026-03-10 |
| 2. Validation and Env Plumbing | v1.0 | 3/3 | Complete | 2026-03-10 |
| 3. Gateway Resilience and Live Feedback | v1.0 | 2/2 | Complete | 2026-03-10 |
| 4. Advanced Multi-Model Settings | v1.0 | 1/1 | Complete | 2026-03-11 |
| 5. Production Hardening | v1.0 | 6/6 | Complete | 2026-03-10 |
| 6. Shared Infrastructure Extraction | v2.0 | 3/3 | Complete | 2026-03-13 |
| 7. Channel Plugin Parity | v2.0 | 3/3 | Complete | 2026-03-13 |
| 8. Notification Channel Targeting | v2.0 | 1/1 | Complete | 2026-03-13 |
| 9. Multi-Bot Core | v2.0 | 3/3 | Complete | 2026-03-13 |
| 10. Multi-Bot Lifecycle | v2.0 | 1/1 | Complete | 2026-03-13 |
| 11. Channel Contract v2 | v3.0 | 2/2 | Complete | 2026-03-13 |
| 12. Inbound Media Pipeline | v3.0 | 2/2 | Complete | 2026-03-13 |
| 13. Relay Protocol Upgrade | v3.0 | 2/2 | Complete | 2026-03-13 |
| 14. Outbound Rich Media | v3.0 | 2/2 | Complete | 2026-03-13 |
| 15. Reference Channel Plugin | v3.0 | 1/1 | Complete | 2026-03-13 |
| 16. Watcher Engine | v3.0 | 3/3 | Complete | 2026-03-14 |
| 17. Vector Knowledge Base | v3.0 | 3/3 | Complete | 2026-03-15 |
| 18. Security Foundations | 4/4 | Complete    | 2026-03-15 | - |
| 19. Test Infrastructure and Coverage | v4.0 | 0/4 | Not started | - |
| 20. Infrastructure Hardening | v4.0 | 0/3 | Not started | - |
| 21. End-to-End Validation | v4.0 | 0/1 | Not started | - |

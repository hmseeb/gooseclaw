# GooseClaw

## What This Is

A self-hosted personal AI agent platform built on Block's Goose. Users deploy on Railway, configure via setup wizard, and interact through Telegram or any channel plugin. Gateway manages goose web lifecycle, job scheduling, notification bus, and channel plugin system. Supports 23+ LLM providers. Production-hardened with PBKDF2 auth, structured JSON logging, and 103-test automated suite.

## Core Value

A user with zero DevOps knowledge can deploy GooseClaw and configure it correctly on the first try, every time. If they can paste an API key, they can run their own AI agent.

## Requirements

### Validated

- ✓ Web-based setup wizard with 23+ providers — v1.0 phases 1-2
- ✓ API key validation, credential mapping, env var rehydration — v1.0 phase 2
- ✓ Gateway resilience: auto-restart, health monitor, stderr capture — v1.0 phase 3
- ✓ Advanced lead/worker multi-model config — v1.0 phase 4
- ✓ Production hardening: security headers, rate limiting, auth recovery — v1.0 phase 5
- ✓ Telegram gateway with session management, commands, streaming — v1.0
- ✓ Channel plugin system with hot-reload — v1.0
- ✓ Job engine with cron, timers, provider override, auto-expiry — v1.0
- ✓ Notification bus with per-job channel targeting — v1.0
- ✓ Per-channel verbosity settings — v1.0
- ✓ Shell injection eliminated across secret.sh, entrypoint.sh, gateway.py — v4.0
- ✓ PBKDF2 password hashing with lazy SHA-256 migration — v4.0
- ✓ Recovery secret leak sealed, request body limits enforced — v4.0
- ✓ Complete HTTP security headers (COOP, Referrer-Policy, Permissions-Policy) — v4.0
- ✓ Structured JSON logging (254 print() calls migrated) — v4.0
- ✓ Graceful shutdown with 5s watchdog — v4.0
- ✓ Dependency pinning with hash verification support — v4.0
- ✓ CVE scanning via Dependabot — v4.0
- ✓ 103-test automated suite (HTTP endpoints, shell scripts, entrypoint, e2e) — v4.0

- ✓ mem0 MCP extension with ChromaDB backend and shared config — v5.0
- ✓ Gateway auto-feeds conversations to mem0 after each session — v5.0
- ✓ ChromaDB migration from runtime collection to mem0 — v5.0
- ✓ Neo4j knowledge graph for entity relationships (in-container) — v5.0
- ✓ Fallback provider system with drag-to-reorder chains — v5.1

### Active

## Current Milestone: v6.0 Voice Dashboard

**Goal:** Add a real-time voice channel to GooseClaw using Gemini 3.1 Flash Live API. Users talk to their AI agent via a web dashboard from phone or PC.

**Target features:**
- Web dashboard with mic button, voice visualizer, live transcript
- Gemini 3.1 Flash Live API as the voice brain (STT + LLM + TTS in one model)
- WebSocket proxy in gateway.py relaying audio between browser and Gemini Live API
- Tool calling mid-conversation (Gmail, Calendar, memory, knowledge search, etc.)
- Gemini API key as optional provider in setup wizard
- Voice dashboard gates access on Gemini key presence
- Works on phone and desktop browsers, no app install
- Channel plugin architecture (like Telegram channel)
- Ephemeral tokens for secure browser-to-API auth

### Out of Scope

- Mobile-responsive wizard — desktop-first, Railway dashboard is desktop anyway
- Custom extension management in wizard — separate concern, goose web handles this
- OAuth flows (OpenRouter OAuth, GitHub Copilot device flow) — too complex for single HTML file
- Multiple goose web processes — single process, sessions provide isolation
- Platform-specific rich UI (cards, carousels, adaptive cards) — send_buttons is the escape hatch
- TLS termination in container — Railway handles TLS at load balancer
- WAF inside container — single-user auth-gated app, input sanitization + rate limiting sufficient
- Encrypted vault at rest — single-user, Railway volumes isolated
- Multi-factor authentication — single-user self-hosted app, Railway auth is primary gate
- RBAC / multi-user auth — personal agent, build multi-user when use case exists
- argon2 password hashing — PBKDF2 via stdlib achieves same security goal
- structlog / python-json-logger — stdlib logging + custom JSON formatter sufficient

## Context

GooseClaw is a Docker-based deployment template for Block's Goose AI agent. It runs on Railway with a persistent volume at /data. The architecture is:

- **entrypoint.sh**: Container startup, env var setup, starts gateway.py and telegram gateway
- **gateway.py**: Python HTTP server (~10K lines, stdlib only). Serves setup wizard, reverse proxies to goose web, manages lifecycle, structured JSON logging
- **setup.html**: Single-file HTML/CSS/JS wizard. No build step, no npm
- **goose web**: Experimental goose CLI command that serves a chat UI on an internal port
- **knowledge MCP**: ChromaDB-backed semantic retrieval extension
- **tests/**: 103 automated tests (pytest + requests against live server)

Key technical constraints:
- Python stdlib only (no pip install in gateway.py)
- Single HTML file (no build tooling)
- ubuntu:22.04 Docker base
- Railway volumes for persistence

## Constraints

- **No build tooling**: setup.html must be self-contained HTML/CSS/JS
- **Python stdlib only**: gateway.py cannot use pip packages
- **Railway compatible**: Must work with Railway volumes, PORT env var, health checks
- **Goose binary**: Pre-installed in Dockerfile, version may vary
- **Single provider**: Goose uses one GOOSE_PROVIDER + GOOSE_MODEL at a time
- **goose web experimental**: May crash, needs resilience layer

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| One provider + easy reconfigure | Goose only uses one provider at a time, profiles add UX complexity | ✓ Good |
| Advanced toggle for lead/worker | Power users get multi-model without cluttering main flow | ✓ Good |
| Skip OAuth flows (OpenRouter, Copilot) | Too complex for single HTML file, can add later | ✓ Good |
| Keep Python stdlib only | No pip in container, keeps gateway.py simple and portable | ✓ Good |
| Provider categories in UI | Reduces decision paralysis for new users | ✓ Good |
| PBKDF2 via stdlib (not argon2/bcrypt) | Stays stdlib-only, OWASP-approved, 600K iterations | ✓ Good |
| Lazy hash migration (SHA-256 to PBKDF2) | Prevents lockout of existing users on upgrade | ✓ Good |
| Structured logging always-on (no toggle) | Simpler than env var toggle, Railway needs JSON anyway | ✓ Good |
| HTTP-level tests (not function mocks) | 400KB monolith, real server on random port is more reliable | ✓ Good |
| Shell injection fix via os.environ pattern | Mechanical, grep-verifiable, zero string interpolation into Python | ✓ Good |
| Gemini as voice brain (not goosed proxy) | Single model handles STT+LLM+TTS, lower latency, simpler arch. Voice channel uses different LLM than text channels. | — Pending |
| Optional Gemini key in setup wizard | Users who don't want voice can skip it. Dashboard gates on key presence. | — Pending |

---
*Last updated: 2026-03-27 after v6.0 milestone start*

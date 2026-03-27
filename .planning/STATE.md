# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-27)

**Core value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try
**Current focus:** v6.0 Voice Dashboard - Phase 27 (WebSocket Infrastructure)

## Current Position

Phase: 27 of 33 (WebSocket Infrastructure)
Plan: 0 of TBD in current phase
Status: Ready to plan
Last activity: 2026-03-27 — Roadmap created for v6.0 Voice Dashboard (7 phases, 32 requirements)

Progress v1.0: [==========] 100% (shipped 2026-03-11)
Progress v2.0: [==========] 100% (shipped 2026-03-13)
Progress v3.0: [==========] 100% (shipped 2026-03-15)
Progress v4.0: [==========] 100% (shipped 2026-03-16)
Progress v5.0: [==========] 100% (shipped 2026-03-20)
Progress v5.1: [==========] 100% (shipped 2026-03-25)
Progress v6.0: [..........] 0%

## Performance Metrics

**Velocity (all milestones):**
- Total plans completed: 63
- Average duration: ~4.5 min
- Total execution time: ~4.5 hours

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- v6.0: Gemini 3.1 Flash Live as voice brain (not goosed proxy) — lower latency, single model
- v6.0: Optional Gemini API key in setup wizard, voice dashboard gates on key presence
- v6.0: Voice channel is independent brain from text channels (Gemini vs user's main provider)
- v6.0: Server-side WebSocket proxy (API key never reaches browser)
- v6.0: RFC 6455 from scratch in stdlib Python (~200 lines), no pip dependencies

### Key Research Flags

- Gemini 3.1 Flash Live is preview (day-old). Fallback to 2.5 Flash Live if unstable.
- Railway kills idle WebSocket at 10 min. Ping/pong every 25s mandatory from Phase 27.
- Tool calling is synchronous on Gemini Live. Model blocks until tool response. 2-3s timeout needed.
- Browser autoplay policy: AudioContext must be created inside user click handler.

### Pending Todos

- Lock audit: map all 17 locks and their acquisition paths
- Queue consecutive messages instead of bouncing with "Still thinking"
- Hide internal file references and tool usage from user-facing LLM output
- Revisit memory files idle poll
- Investigate Goose multi-agent spawning with goosed
- Generate requirements.lock via generate-lockfile.sh --docker
- Validate e2e tests in CI pipeline

### Blockers/Concerns

None.

## Session Continuity

Last session: 2026-03-27
Stopped at: Roadmap created for v6.0 Voice Dashboard
Resume file: None

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-27)

**Core value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try
**Current focus:** v6.0 Voice Dashboard - Phase 30 (Voice Dashboard)

## Current Position

Phase: 30 of 33 (Voice Dashboard)
Plan: 2 of 3 in current phase
Status: Executing
Last activity: 2026-03-27 — Plan 30-02 complete (Audio Capture and Playback)

Progress v1.0: [==========] 100% (shipped 2026-03-11)
Progress v2.0: [==========] 100% (shipped 2026-03-13)
Progress v3.0: [==========] 100% (shipped 2026-03-15)
Progress v4.0: [==========] 100% (shipped 2026-03-16)
Progress v5.0: [==========] 100% (shipped 2026-03-20)
Progress v5.1: [==========] 100% (shipped 2026-03-25)
Progress v6.0: [===.......] 29%

## Performance Metrics

**Velocity (all milestones):**
- Total plans completed: 65
- Average duration: ~4.5 min
- Total execution time: ~4.8 hours

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- v6.0: Gemini 3.1 Flash Live as voice brain (not goosed proxy) — lower latency, single model
- v6.0: Optional Gemini API key in setup wizard, voice dashboard gates on key presence
- v6.0: Voice channel is independent brain from text channels (Gemini vs user's main provider)
- v6.0: Server-side WebSocket proxy (API key never reaches browser)
- v6.0: RFC 6455 from scratch in stdlib Python (~200 lines), no pip dependencies
- v6.0: Voice session tokens with 5-min TTL for ephemeral WebSocket auth
- v6.0: Two relay threads (browser-to-Gemini, Gemini-to-browser) with shared stop_event
- v6.0: GoAway reconnection uses lock-protected socket swap with resumption handle

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
Stopped at: Completed 30-02-PLAN.md (Audio Capture and Playback)
Resume file: None

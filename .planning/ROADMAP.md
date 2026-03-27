# Roadmap: GooseClaw

## Milestones

- [x] **v1.0 Setup Wizard** — Phases 1-5 (shipped 2026-03-11)
- [x] **v2.0 Multi-Channel & Multi-Bot** — Phases 6-10 (shipped 2026-03-13)
- [x] **v3.0 Rich Media & Channel Flexibility** — Phases 11-17 (shipped 2026-03-15)
- [x] **v4.0 Production Hardening** — Phases 18-21 (shipped 2026-03-16)
- [x] **v5.0 mem0 Memory Layer** — Phases 22-25 (shipped 2026-03-20)
- [x] **v5.1 Fallback Provider** — Phase 26 (shipped 2026-03-25)
- [ ] **v6.0 Voice Dashboard** — Phases 27-33 (in progress)

## Phases

<details>
<summary>v1.0 Setup Wizard (Phases 1-5) — SHIPPED 2026-03-11</summary>

- [x] Phase 1: Provider UI Expansion (2/2 plans)
- [x] Phase 2: Validation and Env Plumbing (3/3 plans)
- [x] Phase 3: Gateway Resilience and Live Feedback (2/2 plans)
- [x] Phase 4: Advanced Multi-Model Settings (1/1 plan)
- [x] Phase 5: Production Hardening (6/6 plans)

</details>

<details>
<summary>v2.0 Multi-Channel & Multi-Bot (Phases 6-10) — SHIPPED 2026-03-13</summary>

- [x] Phase 6: Shared Infrastructure Extraction (3/3 plans)
- [x] Phase 7: Channel Plugin Parity (3/3 plans)
- [x] Phase 8: Notification Channel Targeting (1/1 plan)
- [x] Phase 9: Multi-Bot Core (3/3 plans)
- [x] Phase 10: Multi-Bot Lifecycle (1/1 plan)

</details>

<details>
<summary>v3.0 Rich Media & Channel Flexibility (Phases 11-17) — SHIPPED 2026-03-15</summary>

- [x] Phase 11: Channel Contract v2 (2/2 plans)
- [x] Phase 12: Inbound Media Pipeline (2/2 plans)
- [x] Phase 13: Relay Protocol Upgrade (2/2 plans)
- [x] Phase 14: Outbound Rich Media (2/2 plans)
- [x] Phase 15: Reference Channel Plugin (1/1 plan)
- [x] Phase 16: Watcher Engine (3/3 plans)
- [x] Phase 17: Vector Knowledge Base (3/3 plans)

</details>

<details>
<summary>v4.0 Production Hardening (Phases 18-21) — SHIPPED 2026-03-16</summary>

- [x] Phase 18: Security Foundations (4/4 plans)
- [x] Phase 19: Test Infrastructure and Coverage (4/4 plans)
- [x] Phase 20: Infrastructure Hardening (3/3 plans)
- [x] Phase 21: End-to-End Validation (1/1 plan)

</details>

<details>
<summary>v5.0 mem0 Memory Layer (Phases 22-25) — SHIPPED 2026-03-20</summary>

- [x] Phase 22: mem0 MCP Server + Config (2/2 plans)
- [x] Phase 23: Gateway Memory Writer Migration (2/2 plans)
- [x] Phase 24: ChromaDB Migration + Cleanup (2/2 plans)
- [x] Phase 25: Neo4j Knowledge Graph (2/2 plans)

</details>

<details>
<summary>v5.1 Fallback Provider (Phase 26) — SHIPPED 2026-03-25</summary>

- [x] Phase 26: Fallback Provider System (3/3 plans)

</details>

### v6.0 Voice Dashboard (In Progress)

**Milestone Goal:** Add a real-time voice channel to GooseClaw using Gemini 3.1 Flash Live API. Users talk to their AI agent via a web dashboard from phone or PC. Mid-conversation tool calling (the killer differentiator over ChatGPT voice) works through existing MCP extensions.

- [x] **Phase 27: WebSocket Infrastructure** - RFC 6455 frame parser, server + client handlers, ping/pong keepalive (completed 2026-03-27)
- [x] **Phase 28: Gemini Live API Integration** - Outbound Gemini connection, bidirectional relay, session management, ephemeral auth (completed 2026-03-27)
- [x] **Phase 29: Setup Wizard + Dashboard Gating** - Gemini API key in wizard, vault storage, auth reuse, dashboard access control (completed 2026-03-27)
- [x] **Phase 30: Voice Dashboard** - voice.html with mic capture, push-to-talk, streaming playback, transcript, visualizer, state indicators (completed 2026-03-27)
- [x] **Phase 31: Mobile + Keyboard UX** - Mobile-first responsive layout, keyboard shortcuts, text fallback, screen wake lock (completed 2026-03-27)
- [ ] **Phase 32: Tool Calling** - Dynamic MCP tool discovery, Gemini function declarations, mid-conversation execution, visual feedback
- [ ] **Phase 33: Intelligence + History** - Voice transcripts to mem0, session history, voice selection

## Phase Details

### Phase 27: WebSocket Infrastructure
**Goal**: Gateway can accept and maintain WebSocket connections from browsers and establish outbound WebSocket connections to external APIs, with protocol-level keepalive surviving Railway's proxy
**Depends on**: Nothing (first phase of v6.0)
**Requirements**: VOICE-10
**Success Criteria** (what must be TRUE):
  1. A WebSocket client (browser or wscat) can connect to gateway.py via HTTP 101 upgrade and exchange text/binary frames
  2. Gateway can open an outbound TLS WebSocket connection to an external server using stdlib ssl+socket
  3. WebSocket connections stay alive beyond Railway's 10-minute proxy timeout via automatic ping/pong every 25 seconds
  4. WebSocket close handshake completes cleanly from either side without orphaned threads or sockets
**Plans**: 2 plans
- [ ] 27-01-PLAN.md -- TDD: WebSocket protocol functions (frame parser, accept key, ping/close)
- [ ] 27-02-PLAN.md -- WebSocket server handler, outbound client, connection tracking, integration tests

### Phase 28: Gemini Live API Integration
**Goal**: Gateway establishes a working audio pipeline to Gemini Live API with session management that handles connection limits gracefully
**Depends on**: Phase 27
**Requirements**: VOICE-02, VOICE-11, SETUP-03
**Success Criteria** (what must be TRUE):
  1. Browser WebSocket connection proxies through gateway to Gemini Live API, with audio frames relayed bidirectionally in real-time
  2. When Gemini sends a GoAway message at its 10-minute limit, the session auto-reconnects using a resumption handle without the user noticing
  3. Context window compression is enabled so long conversations don't hit the 15-minute audio ceiling
  4. Gateway generates session-scoped tokens for WebSocket auth so the Gemini API key never reaches the browser
**Plans**: 2 plans
- [ ] 28-01-PLAN.md -- TDD: Voice session tokens, Gemini config builder, audio transcoding, message parser
- [ ] 28-02-PLAN.md -- Gemini relay (replace echo loop), token REST endpoint, GoAway reconnect, integration tests

### Phase 29: Setup Wizard + Dashboard Gating
**Goal**: Users can add their Gemini API key through the existing setup wizard, and the voice dashboard is only accessible when a valid key is configured
**Depends on**: Phase 27
**Requirements**: SETUP-01, SETUP-02, SETUP-04, UI-07
**Success Criteria** (what must be TRUE):
  1. User sees "Gemini (Voice)" as an optional provider in the setup wizard with API key input and validation
  2. Gemini API key is stored in the vault alongside other provider keys and survives container restarts
  3. Voice dashboard page returns a "configure Gemini" link instead of the voice UI when no Gemini key is present
  4. Voice dashboard reuses existing PBKDF2 cookie-based auth with no separate login flow
**Plans**: TBD

### Phase 30: Voice Dashboard
**Goal**: Users can have a real-time voice conversation with their AI agent through a web browser, seeing live transcripts and a reactive visualizer
**Depends on**: Phase 28, Phase 29
**Requirements**: VOICE-01, VOICE-03, VOICE-04, VOICE-05, VOICE-06, VOICE-07, VOICE-08, VOICE-09, UI-01, UI-02
**Success Criteria** (what must be TRUE):
  1. User opens voice.html in any modern browser, taps a push-to-talk button, speaks, and hears the AI respond with streaming audio (not buffered)
  2. User can interrupt the AI mid-sentence (barge-in) and the AI immediately stops speaking and starts listening
  3. Both user speech and AI responses appear as a scrolling live transcript in real-time
  4. A reactive voice visualizer (orb/waveform) responds to audio input and output volume
  5. User sees clear connection state (disconnected, connecting, listening, thinking, speaking) and gets actionable error messages for mic denied, WebSocket drops, and API errors
**Plans**: 3 plans
- [ ] 30-01-PLAN.md -- CSP fix + voice.html scaffold with state machine, WebSocket connection, error handling
- [ ] 30-02-PLAN.md -- AudioWorklet capture, streaming PCM playback, push-to-talk, barge-in
- [ ] 30-03-PLAN.md -- Live transcript display, reactive audio visualizer (orb)

### Phase 31: Mobile + Keyboard UX
**Goal**: Voice dashboard works great on phones with touch-friendly controls and on desktop with keyboard shortcuts, with text input as a fallback
**Depends on**: Phase 30
**Requirements**: UI-03, UI-04, UI-05, UI-06
**Success Criteria** (what must be TRUE):
  1. On mobile, the dashboard layout is touch-friendly with large tap targets and the screen stays awake during active voice sessions
  2. On desktop, user can hold Spacebar to talk and press Escape to disconnect
  3. User can type a text message in the same interface when voice isn't convenient, and the AI responds via voice
**Plans**: 1 plan
- [ ] 31-01-PLAN.md -- Static analysis tests + keyboard shortcuts, text input, mobile CSS, Wake Lock

### Phase 32: Tool Calling
**Goal**: Users can ask the AI to perform actions mid-conversation (check calendar, search memory, send email) and see tool execution happening in real-time
**Depends on**: Phase 30
**Requirements**: TOOL-01, TOOL-02, TOOL-03, TOOL-04, TOOL-05, TOOL-06
**Success Criteria** (what must be TRUE):
  1. When user says "check my calendar" or "search my memories", gateway discovers the correct MCP tool and executes it, feeding the result back to Gemini
  2. All MCP tools/extensions available to text channels are automatically available to voice (no hardcoded tool list)
  3. Tool discovery refreshes on each session start so newly installed extensions work immediately
  4. User sees visual feedback in the transcript during tool execution (tool name, spinner, result summary)
  5. Gemini speaks naturally about tool results without double-speech (SILENT scheduling prevents echo)
**Plans**: TBD

### Phase 33: Intelligence + History
**Goal**: Voice conversations feed into the memory system and users can review past sessions and customize their voice experience
**Depends on**: Phase 32
**Requirements**: INTEL-01, INTEL-02, INTEL-03, INTEL-04
**Success Criteria** (what must be TRUE):
  1. After a voice session ends, the transcript is automatically fed into the mem0 memory pipeline (same as text channel conversations)
  2. User can view a list of past voice sessions with timestamps and transcript previews
  3. User can tap a past session to read the full transcript
  4. User can select from available Gemini voices in voice dashboard settings
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 27 -> 28 -> 29 -> 30 -> 31 -> 32 -> 33

Note: Phases 28 and 29 can execute in parallel (both depend only on 27). Phase 30 requires both 28 and 29.

| Milestone | Phases | Plans | Status | Shipped |
|-----------|--------|-------|--------|---------|
| v1.0 Setup Wizard | 1-5 | 14 | Complete | 2026-03-11 |
| v2.0 Multi-Channel | 6-10 | 11 | Complete | 2026-03-13 |
| v3.0 Rich Media | 11-17 | 15 | Complete | 2026-03-15 |
| v4.0 Hardening | 18-21 | 12 | Complete | 2026-03-16 |
| v5.0 mem0 Memory | 22-25 | 8 | Complete | 2026-03-20 |
| v5.1 Fallback | 26 | 3 | Complete | 2026-03-25 |
| v6.0 Voice Dashboard | 27-33 | TBD | Not started | - |

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 27. WebSocket Infrastructure | 2/2 | Complete    | 2026-03-27 |
| 28. Gemini Live API Integration | 2/2 | Complete    | 2026-03-27 |
| 29. Setup Wizard + Dashboard Gating | 3/3 | Complete    | 2026-03-27 |
| 30. Voice Dashboard | 3/3 | Complete    | 2026-03-27 |
| 31. Mobile + Keyboard UX | 1/1 | Complete    | 2026-03-27 |
| 32. Tool Calling | 1/2 | In Progress|  |
| 33. Intelligence + History | 0/TBD | Not started | - |

**Total: 33 phases, 63+ plans across 7 milestones.**

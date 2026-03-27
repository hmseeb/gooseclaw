# Requirements: GooseClaw v6.0 Voice Dashboard

**Defined:** 2026-03-27
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v6.0 Requirements

### Voice Pipeline

- [x] **VOICE-01**: User can open voice dashboard (voice.html) in any modern browser on phone or desktop
- [x] **VOICE-02**: Browser establishes WebSocket connection to gateway.py which proxies to Gemini Live API
- [x] **VOICE-03**: User can tap a push-to-talk button to start speaking, audio streams in real-time to Gemini
- [x] **VOICE-04**: User hears AI responses played back as streaming audio chunks (not buffered full response)
- [x] **VOICE-05**: User can interrupt the AI mid-sentence (barge-in) and AI stops speaking and listens
- [x] **VOICE-06**: Voice Activity Detection automatically detects when user stops speaking (Gemini built-in VAD)
- [x] **VOICE-07**: User sees live transcript of both their speech and AI responses as scrolling chat
- [x] **VOICE-08**: User sees clear connection state indicators (disconnected, connecting, listening, thinking, speaking)
- [x] **VOICE-09**: User gets clear error messages for mic denied, WebSocket drop, API errors, quota exceeded
- [ ] **VOICE-10**: WebSocket proxy sends ping/pong keepalives to survive Railway's 10-min timeout
- [x] **VOICE-11**: Session handles Gemini's connection limits via context window compression and session resumption

### Dashboard UI

- [x] **UI-01**: Voice dashboard is a single self-contained HTML file (voice.html) with no build tooling
- [x] **UI-02**: Voice visualizer (reactive orb/waveform) responds to audio input/output volume in real-time
- [x] **UI-03**: Spacebar hold-to-talk, Escape to disconnect keyboard shortcuts work on desktop
- [x] **UI-04**: User can type messages in same interface when voice isn't convenient (text-to-voice switching)
- [x] **UI-05**: Dashboard layout is mobile-first, works great on phone browsers with touch-friendly controls
- [x] **UI-06**: Screen stays awake during active voice session on mobile (Screen Wake Lock API)
- [x] **UI-07**: Dashboard is only accessible when Gemini API key is configured, shows setup link otherwise

### Tool Calling

- [x] **TOOL-01**: Gateway dynamically discovers ALL available MCP tools/extensions from goosed and maps them as Gemini function declarations
- [x] **TOOL-02**: When Gemini calls any function mid-conversation, gateway routes it to the correct MCP tool and feeds the result back
- [x] **TOOL-03**: Tool discovery refreshes on session start so newly installed extensions are immediately available to voice
- [x] **TOOL-04**: Tool execution shows visual feedback in transcript (tool name, "running..." spinner, result summary)
- [x] **TOOL-05**: Tool responses use SILENT scheduling so Gemini speaks naturally about results (no double-speech)
- [x] **TOOL-06**: Voice channel has feature parity with text channels for tool access (everything goosed can do, voice can do)

### Intelligence

- [ ] **INTEL-01**: Voice conversation transcripts auto-feed into mem0 memory pipeline after session ends
- [ ] **INTEL-02**: User can view list of past voice sessions with timestamps and transcript previews
- [ ] **INTEL-03**: User can tap a past session to view full transcript
- [ ] **INTEL-04**: User can select from available Gemini voices in voice dashboard settings

### Setup & Auth

- [x] **SETUP-01**: Gemini API key is an optional provider in the setup wizard
- [x] **SETUP-02**: Voice dashboard reuses existing PBKDF2 cookie-based auth (no separate login)
- [x] **SETUP-03**: Gateway generates session-scoped tokens for WebSocket auth (API key never reaches browser)
- [x] **SETUP-04**: Gemini API key stored in vault alongside other provider keys

## Future Requirements (v6.x / v7+)

- **NOTIF-01**: Voice channel receives notifications from job engine during sessions
- **VIDEO-01**: Camera/video input to Gemini during voice sessions
- **MULTI-01**: Multi-language voice selection UI
- **WAKE-01**: Wake word activation ("Hey Goose")

## Out of Scope

| Feature | Reason |
|---------|--------|
| Wake word / always-on listening | Browser tabs can't run persistent background listeners. Battery killer. Privacy concern. |
| WebRTC direct connection | API key would be exposed to browser. Bypasses tool calling. |
| Multiple simultaneous voice sessions | GooseClaw is single-user. Single active voice session. |
| Voice cloning / custom TTS | Gemini handles TTS internally. 30 HD voices is plenty. |
| Offline voice mode | Entire architecture depends on Gemini Live API (cloud). |
| React / framework-based dashboard | Constraint: single HTML file, no build tooling. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| VOICE-01 | Phase 30 | Complete |
| VOICE-02 | Phase 28 | Complete |
| VOICE-03 | Phase 30 | Complete |
| VOICE-04 | Phase 30 | Complete |
| VOICE-05 | Phase 30 | Complete |
| VOICE-06 | Phase 30 | Complete |
| VOICE-07 | Phase 30 | Complete |
| VOICE-08 | Phase 30 | Complete |
| VOICE-09 | Phase 30 | Complete |
| VOICE-10 | Phase 27 | Pending |
| VOICE-11 | Phase 28 | Complete |
| UI-01 | Phase 30 | Complete |
| UI-02 | Phase 30 | Complete |
| UI-03 | Phase 31 | Complete |
| UI-04 | Phase 31 | Complete |
| UI-05 | Phase 31 | Complete |
| UI-06 | Phase 31 | Complete |
| UI-07 | Phase 29 | Complete |
| TOOL-01 | Phase 32 | Complete |
| TOOL-02 | Phase 32 | Complete |
| TOOL-03 | Phase 32 | Complete |
| TOOL-04 | Phase 32 | Complete |
| TOOL-05 | Phase 32 | Complete |
| TOOL-06 | Phase 32 | Complete |
| INTEL-01 | Phase 33 | Pending |
| INTEL-02 | Phase 33 | Pending |
| INTEL-03 | Phase 33 | Pending |
| INTEL-04 | Phase 33 | Pending |
| SETUP-01 | Phase 29 | Complete |
| SETUP-02 | Phase 29 | Complete |
| SETUP-03 | Phase 28 | Complete |
| SETUP-04 | Phase 29 | Complete |

**Coverage:**
- v6.0 requirements: 32 total
- Mapped to phases: 32
- Unmapped: 0

---
*Requirements defined: 2026-03-27*
*Last updated: 2026-03-27 after roadmap creation*

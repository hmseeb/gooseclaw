# Requirements: GooseClaw v6.0 Voice Dashboard

**Defined:** 2026-03-27
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v6.0 Requirements

### Voice Pipeline

- [ ] **VOICE-01**: User can open voice dashboard (voice.html) in any modern browser on phone or desktop
- [ ] **VOICE-02**: Browser establishes WebSocket connection to gateway.py which proxies to Gemini Live API
- [ ] **VOICE-03**: User can tap a push-to-talk button to start speaking, audio streams in real-time to Gemini
- [ ] **VOICE-04**: User hears AI responses played back as streaming audio chunks (not buffered full response)
- [ ] **VOICE-05**: User can interrupt the AI mid-sentence (barge-in) and AI stops speaking and listens
- [ ] **VOICE-06**: Voice Activity Detection automatically detects when user stops speaking (Gemini built-in VAD)
- [ ] **VOICE-07**: User sees live transcript of both their speech and AI responses as scrolling chat
- [ ] **VOICE-08**: User sees clear connection state indicators (disconnected, connecting, listening, thinking, speaking)
- [ ] **VOICE-09**: User gets clear error messages for mic denied, WebSocket drop, API errors, quota exceeded
- [ ] **VOICE-10**: WebSocket proxy sends ping/pong keepalives to survive Railway's 10-min timeout
- [ ] **VOICE-11**: Session handles Gemini's connection limits via context window compression and session resumption

### Dashboard UI

- [ ] **UI-01**: Voice dashboard is a single self-contained HTML file (voice.html) with no build tooling
- [ ] **UI-02**: Voice visualizer (reactive orb/waveform) responds to audio input/output volume in real-time
- [ ] **UI-03**: Spacebar hold-to-talk, Escape to disconnect keyboard shortcuts work on desktop
- [ ] **UI-04**: User can type messages in same interface when voice isn't convenient (text-to-voice switching)
- [ ] **UI-05**: Dashboard layout is mobile-first, works great on phone browsers with touch-friendly controls
- [ ] **UI-06**: Screen stays awake during active voice session on mobile (Screen Wake Lock API)
- [ ] **UI-07**: Dashboard is only accessible when Gemini API key is configured, shows setup link otherwise

### Tool Calling

- [ ] **TOOL-01**: Gateway dynamically discovers ALL available MCP tools/extensions from goosed and maps them as Gemini function declarations
- [ ] **TOOL-02**: When Gemini calls any function mid-conversation, gateway routes it to the correct MCP tool and feeds the result back
- [ ] **TOOL-03**: Tool discovery refreshes on session start so newly installed extensions are immediately available to voice
- [ ] **TOOL-04**: Tool execution shows visual feedback in transcript (tool name, "running..." spinner, result summary)
- [ ] **TOOL-05**: Tool responses use SILENT scheduling so Gemini speaks naturally about results (no double-speech)
- [ ] **TOOL-06**: Voice channel has feature parity with text channels for tool access (everything goosed can do, voice can do)

### Intelligence

- [ ] **INTEL-01**: Voice conversation transcripts auto-feed into mem0 memory pipeline after session ends
- [ ] **INTEL-02**: User can view list of past voice sessions with timestamps and transcript previews
- [ ] **INTEL-03**: User can tap a past session to view full transcript
- [ ] **INTEL-04**: User can select from available Gemini voices in voice dashboard settings

### Setup & Auth

- [ ] **SETUP-01**: Gemini API key is an optional provider in the setup wizard
- [ ] **SETUP-02**: Voice dashboard reuses existing PBKDF2 cookie-based auth (no separate login)
- [ ] **SETUP-03**: Gateway generates session-scoped tokens for WebSocket auth (API key never reaches browser)
- [ ] **SETUP-04**: Gemini API key stored in vault alongside other provider keys

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
| VOICE-01 | — | Pending |
| VOICE-02 | — | Pending |
| VOICE-03 | — | Pending |
| VOICE-04 | — | Pending |
| VOICE-05 | — | Pending |
| VOICE-06 | — | Pending |
| VOICE-07 | — | Pending |
| VOICE-08 | — | Pending |
| VOICE-09 | — | Pending |
| VOICE-10 | — | Pending |
| VOICE-11 | — | Pending |
| UI-01 | — | Pending |
| UI-02 | — | Pending |
| UI-03 | — | Pending |
| UI-04 | — | Pending |
| UI-05 | — | Pending |
| UI-06 | — | Pending |
| UI-07 | — | Pending |
| TOOL-01 | — | Pending |
| TOOL-02 | — | Pending |
| TOOL-03 | — | Pending |
| TOOL-04 | — | Pending |
| TOOL-05 | — | Pending |
| TOOL-06 | — | Pending |
| INTEL-01 | — | Pending |
| INTEL-02 | — | Pending |
| INTEL-03 | — | Pending |
| INTEL-04 | — | Pending |
| SETUP-01 | — | Pending |
| SETUP-02 | — | Pending |
| SETUP-03 | — | Pending |
| SETUP-04 | — | Pending |

**Coverage:**
- v6.0 requirements: 32 total
- Mapped to phases: 0
- Unmapped: 32

---
*Requirements defined: 2026-03-27*
*Last updated: 2026-03-27 after v6.0 milestone initialization*

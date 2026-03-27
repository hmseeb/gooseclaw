# Project Research Summary

**Project:** GooseClaw v6.0 Voice Dashboard
**Domain:** Real-time voice AI web interface (WebSocket audio streaming with Gemini Live API)
**Researched:** 2026-03-27
**Confidence:** MEDIUM-HIGH

## Executive Summary

GooseClaw v6.0 adds a voice channel to an existing self-hosted AI agent platform. The approach is a server-side WebSocket proxy: the browser captures raw PCM audio via Web Audio API, streams it through gateway.py (the existing stdlib-only Python HTTP server), which relays it to Gemini 3.1 Flash Live API for combined STT+LLM+TTS processing. The gateway intercepts Gemini's tool calls to execute MCP tools (memory search, Gmail, calendar) server-side, making this the killer differentiator over ChatGPT voice mode which cannot use tools during voice conversations. Everything is delivered as a single HTML file (voice.html) with no build tooling, matching the existing setup.html pattern.

The recommended approach leans heavily into Gemini's native capabilities: built-in VAD, barge-in support, audio transcription, and function calling. This means the gateway is primarily a relay/proxy with selective interception, not a media processor. The hardest engineering challenge is implementing WebSocket protocol (RFC 6455) from scratch in Python stdlib, since gateway.py allows no pip dependencies. This is approximately 200 lines of well-specified protocol code, but audio streams hit every edge case (large binary payloads, extended length fields, masking). Getting the frame parser wrong corrupts audio silently.

The key risks are: (1) Gemini 3.1 Flash Live is a day-old preview model that may be unstable, with Gemini 2.5 Flash Live as a GA fallback; (2) Railway's proxy kills idle WebSocket connections at 10 minutes, compounding Gemini's own 10-minute connection limit, requiring protocol-level ping/pong and session resumption from day one; (3) synchronous tool calling on 3.1 Flash Live means dead silence during tool execution, requiring aggressive timeouts and "thinking" UI indicators. All three risks have known mitigations but must be designed in from the start, not bolted on later.

## Key Findings

### Recommended Stack

The entire voice feature ships as additions to two existing files (gateway.py, voice.html) plus vault configuration. No new services, no pip dependencies, no npm, no infrastructure cost beyond Gemini API usage (~$0.15/M input tokens).

**Core technologies:**
- **Gemini 3.1 Flash Live API** (`gemini-3.1-flash-live-preview`): Single model handles STT + LLM + TTS. 131K context, function calling, audio transcription. Fallback to `gemini-2.5-flash-live-001` (GA) if preview is unstable.
- **Python stdlib WebSocket (RFC 6455)**: ~200 lines of handshake + frame parsing using hashlib, struct, socket, ssl, base64. Gateway is both WebSocket server (browser-facing) and WebSocket client (Gemini-facing).
- **Web Audio API (AudioWorklet)**: Browser-native mic capture as raw PCM 16kHz 16-bit mono. Blob URL trick for single-file HTML. No MediaRecorder (wrong tool for real-time streaming).
- **Server-side proxy architecture**: API key never leaves server. Gateway relays audio and intercepts tool calls. Browser only sees WebSocket frames.

**Critical version/format requirements:**
- Audio input: raw PCM, 16kHz, 16-bit signed, mono, little-endian
- Audio output: raw PCM, 24kHz, 16-bit signed, mono, little-endian
- WebSocket frames: browser-to-gateway uses binary; gateway-to-Gemini uses JSON with base64 audio
- Client WebSocket frames to Gemini MUST be masked per RFC 6455

### Expected Features

**Must have (table stakes):**
- Push-to-talk button with mic permission handling (spacebar hold-to-talk)
- Real-time bidirectional audio streaming (browser -> gateway -> Gemini -> gateway -> browser)
- Audio playback of AI responses (streaming, not buffered)
- Built-in VAD and barge-in support (Gemini handles this natively)
- Live transcript display (Gemini provides audio transcription)
- Connection state indicators (disconnected/connecting/listening/thinking/speaking)
- Ephemeral session auth (reuse existing PBKDF2 cookie auth on WebSocket upgrade)
- Dashboard gated on Gemini API key presence
- Graceful error handling (mic denied, WebSocket drop, API errors)
- Mobile-responsive layout (voice is primarily a mobile use case)
- Voice visualizer / reactive orb (visual identity, not just a static mic icon)

**Should have (differentiators -- add after voice pipeline is stable):**
- Mid-conversation tool calling (the KILLER feature: "check my calendar" actually works, unlike ChatGPT voice)
- Visual tool execution feedback in transcript
- Automatic memory extraction from voice sessions (feed transcripts into mem0)
- Keyboard shortcuts (spacebar hold-to-talk, Escape to disconnect)
- Conversation history (store and list past voice sessions)
- Gemini voice selection (30 HD voices available)
- Screen wake lock for mobile

**Defer (v7+):**
- Text-to-voice switching (unified typed + spoken thread)
- Notification bus integration during voice sessions
- Video/camera input (Gemini supports it, but separate scope)
- Wake word / always-on listening (browser can't do this well)
- WebRTC (overkill, breaks tool calling architecture)

### Architecture Approach

Gateway.py acts as a bidirectional WebSocket proxy with selective message interception. It accepts WebSocket connections from the browser on `/ws/voice`, opens an outbound WebSocket client connection to Gemini Live API using stdlib ssl+socket, and relays frames between them in a two-thread relay loop (one thread browser-to-Gemini, one Gemini-to-browser). The only messages it intercepts are `toolCall` from Gemini, which it dispatches to goosed for MCP tool execution. Everything else passes through verbatim. This means the gateway never touches audio data and new Gemini message types automatically work.

**Major components:**
1. **WebSocket protocol layer** (~200 lines) -- RFC 6455 frame read/write, handshake (server + client), ping/pong, close handling
2. **Gemini WebSocket client** (~100 lines) -- outbound TLS+WebSocket connection to Gemini, setup config, session management
3. **Bidirectional relay** (~150 lines) -- two-thread relay loop, shutdown coordination, tool call interception
4. **voice.html** (~2000 lines) -- mic capture via AudioWorklet, PCM encoding, WebSocket client, audio playback, transcript UI, visualizer, mobile layout
5. **Tool executor** (~200 lines) -- Gemini function declaration builder, tool dispatch to goosed, timeout handling, cancellation
6. **Setup wizard integration** (~100 lines) -- Gemini API key field, validation, vault storage, dashboard gating

### Critical Pitfalls

1. **Python http.server not designed for long-lived connections** -- After HTTP 101 handshake, extract the raw socket and hand it to a dedicated WebSocket management loop. Do NOT keep using BaseHTTPRequestHandler methods. Use daemon threads. Cap at 1-2 concurrent WebSocket connections.

2. **Gemini session limits kill calls silently** -- 10-minute connection lifetime (GoAway message), 15-minute audio without compression. Enable context window compression (sliding window) and session resumption from day one. Handle GoAway with auto-reconnect using resumption handle.

3. **Railway proxy kills idle WebSocket connections at 10 minutes** -- Implement WebSocket protocol-level ping/pong every 25 seconds. Application-level heartbeats do NOT satisfy the proxy. This compounds with Gemini's own timeout.

4. **Browser autoplay policy blocks audio playback** -- Create AudioContext inside the mic button click handler, not at page load. Call `audioContext.resume()` and await it. Fails silently on iOS Safari if done wrong.

5. **Synchronous tool calling blocks voice response** -- On Gemini 3.1 Flash Live, the model literally cannot speak until tool response arrives. Set 2-3 second timeouts on all tool calls. Show "thinking" UI. Track cancelled tool calls from interruptions to avoid orphaned work.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: WebSocket Infrastructure
**Rationale:** Everything depends on this. Voice, audio, tools -- none of it works without the WebSocket protocol layer and bidirectional relay. Three critical pitfalls (stdlib socket takeover, Railway proxy timeouts, frame parsing bugs) must be solved here.
**Delivers:** Working WebSocket server (browser-facing) and client (Gemini-facing) in gateway.py with RFC 6455 frame parsing, ping/pong keepalive, and clean session lifecycle.
**Addresses:** WebSocket proxy in gateway.py (P1 foundation)
**Avoids:** Pitfalls 1 (http.server long-lived connections), 3 (Railway proxy timeout), 6 (frame parsing bugs)
**Estimated scope:** ~300-400 lines of Python in gateway.py

### Phase 2: Gemini Live API Integration
**Rationale:** Once WebSocket infra works, connect to Gemini and verify the full audio pipeline end-to-end. Session management (resumption, compression, GoAway handling) must be built in here, not retrofitted.
**Delivers:** Working outbound connection to Gemini Live API with setup config, session resumption, context window compression, and GoAway auto-reconnect. Ephemeral session auth via existing cookie system.
**Addresses:** Audio streaming pipeline (P1), ephemeral token auth (P1), session management
**Avoids:** Pitfall 2 (Gemini session limits)
**Estimated scope:** ~200 lines of Python in gateway.py

### Phase 3: Voice Dashboard UI (voice.html)
**Rationale:** With the server-side pipeline working, build the browser UI. This is where mic capture, audio playback, transcript display, and visualizer live. AudioWorklet Blob URL trick and iOS autoplay policy must be handled here.
**Delivers:** Single-file voice.html with push-to-talk, AudioWorklet mic capture (PCM 16kHz), streaming audio playback (24kHz), live transcript, connection state indicators, voice visualizer, and mobile-responsive layout.
**Addresses:** Push-to-talk (P1), audio playback (P1), live transcript (P1), connection state (P1), voice visualizer (P1), mobile responsive (P1), error handling (P1)
**Avoids:** Pitfall 4 (autoplay policy), Pitfall 7 (MediaRecorder trap -- use AudioWorklet instead)
**Estimated scope:** ~2000 lines of HTML/CSS/JS

### Phase 4: Tool Calling Integration
**Rationale:** This is the killer differentiator but sits on top of all previous phases. Debugging tool calling on a flaky voice connection is hell, so the pipeline must be solid first. Synchronous blocking and cancellation handling add significant complexity.
**Delivers:** Mid-conversation tool execution via goosed MCP tools. Gemini function declarations mapped to available tools. Tool call interception in relay loop. Timeout handling (2-3s max). Cancelled call tracking. Visual feedback in transcript.
**Addresses:** Mid-conversation tool calling (P2), visual tool execution feedback (P2)
**Avoids:** Pitfall 5 (synchronous tool calling blocks voice)
**Estimated scope:** ~200-300 lines of Python, ~200 lines of JS for UI feedback

### Phase 5: Setup Wizard + Memory + Polish
**Rationale:** Polish phase. Gemini API key management through setup wizard, conversation history storage, and feeding voice transcripts into mem0 memory pipeline.
**Delivers:** Gemini key in setup wizard with validation, dashboard gating on key presence, voice session history, automatic memory extraction from voice transcripts, keyboard shortcuts, screen wake lock, voice selection.
**Addresses:** Setup wizard Gemini key (P1), dashboard gated on key (P1), memory extraction (P2), conversation history (P2), keyboard shortcuts (P2), screen wake lock (P2), voice selection (P2)

### Phase Ordering Rationale

- **Dependencies flow strictly downward:** WebSocket infra -> Gemini connection -> Browser UI -> Tool calling -> Polish. Each phase requires the previous one to be working.
- **Pitfall clustering:** Phase 1 addresses 3 of the 7 critical pitfalls because the WebSocket layer is where most things go wrong. Getting this right de-risks everything above it.
- **Differentiator deferred strategically:** Tool calling (the killer feature) is Phase 4 because it requires a stable voice pipeline. Building the differentiator on a shaky foundation wastes time debugging the wrong layer.
- **Browser UI is Phase 3 (not Phase 1)** because the server-side pipeline should be testable with a simple WebSocket client (like wscat) before adding the complexity of browser audio APIs.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 1:** WebSocket RFC 6455 edge cases (fragmentation, close frames, error recovery). Reference: Pithikos/python-websocket-server for stdlib-only implementation patterns.
- **Phase 2:** Gemini 3.1 Flash Live is a day-old preview model. May need to fall back to 2.5 Flash Live. Session resumption protocol needs validation against live API.
- **Phase 4:** Tool execution latency through goosed is unknown. May need to bypass goosed for simple tools (direct MCP dispatch) if latency exceeds 2-3 seconds. Gemini cookbook issue #906 suggests tool response format has known issues.

Phases with standard patterns (skip research-phase):
- **Phase 3:** Web Audio API, AudioWorklet, WebSocket client API are all well-documented browser standards. MDN docs are comprehensive.
- **Phase 5:** Setup wizard integration follows existing patterns in gateway.py. Memory extraction reuses existing mem0 pipeline.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All stdlib. RFC 6455 is well-specified. Web Audio API is mature. No dependency risk. |
| Features | HIGH | Feature landscape well-mapped against competitors. Clear MVP vs. differentiator separation. ChatGPT voice limitations (no tools) validated against official docs. |
| Architecture | HIGH | Server-side proxy is proven pattern. Data flows verified against Gemini Live API docs. Threading model consistent with existing gateway.py. |
| Pitfalls | MEDIUM-HIGH | Railway WebSocket timeout confirmed by community reports but not official docs. Gemini 3.1 Flash Live is one day old (preview). Tool calling sync-only limitation confirmed in official docs but workarounds (scheduling parameter) are less documented. |

**Overall confidence:** MEDIUM-HIGH

### Gaps to Address

- **Gemini 3.1 Flash Live stability:** Model released 2026-03-26 (yesterday). "Preview" suffix means it could be unstable or change behavior. Must test early and have 2.5 Flash Live fallback ready. This is the biggest unknown.
- **Ephemeral token REST endpoint:** The REST API for minting ephemeral tokens is reverse-engineered from SDK source, not officially documented. Architecture decision is to use server-side proxy instead, which sidesteps this gap entirely.
- **Tool execution latency via goosed:** Creating a goosed session + dispatching MCP tool + LLM reasoning could take 2-5 seconds. Unknown until tested with real tools. May need to pre-create goosed sessions or bypass goosed for latency-sensitive tools.
- **Railway WebSocket behavior:** Timeout confirmed by user reports but not official Railway docs. Need to validate ping/pong keepalive behavior on actual Railway deployment.
- **Concurrent session limits:** Gemini's per-API-key concurrent session limit is undocumented. Need to test. Single-user app likely means 1 session, but tab-switching behavior needs a policy.
- **Gemini tool response format:** Cookbook issue #906 suggests the documented `BidiGenerateContentToolResponse` format has issues. Workaround (FunctionResponse Part in clientContent) needs validation.

## Sources

### Primary (HIGH confidence)
- [Gemini Live API Overview](https://ai.google.dev/gemini-api/docs/live-api)
- [Gemini Live API WebSocket Reference](https://ai.google.dev/api/live)
- [Gemini Live API Getting Started (WebSocket)](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket)
- [Gemini Live API Capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
- [Gemini Live API Session Management](https://ai.google.dev/gemini-api/docs/live-session)
- [Gemini Live API Tool Calling](https://ai.google.dev/gemini-api/docs/live-api/tools)
- [Gemini Ephemeral Tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens)
- [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455)
- [MDN: Writing WebSocket Servers](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API/Writing_WebSocket_servers)
- [MDN: Web Audio API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API)
- [MDN: Autoplay Guide](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay)

### Secondary (MEDIUM confidence)
- [Railway WebSocket disconnect reports](https://station.railway.com/questions/socket-disconnects-after-10-minutes-bbceef40)
- [Gemini Live API Examples (GitHub)](https://github.com/google-gemini/gemini-live-api-examples)
- [google-gemini/live-api-web-console](https://github.com/google-gemini/live-api-web-console)
- [Pithikos/python-websocket-server](https://github.com/Pithikos/python-websocket-server)
- [ChatGPT Voice Mode Unified Interface (TechCrunch)](https://techcrunch.com/2025/11/25/chatgpts-voice-mode-is-no-longer-a-separate-interface/)
- [python-genai SDK tokens.py](https://github.com/googleapis/python-genai/blob/main/google/genai/tokens.py)

### Tertiary (LOW confidence)
- [Gemini cookbook issue #906: tool response format workaround](https://github.com/google-gemini/cookbook/issues/906)
- [Gemini Live API error 1008 during function calling](https://discuss.google.dev/t/gemini-live-api-apierror-1008-policy-violation-during-function-calling/337832)
- [python-genai issue #803: function call hangs](https://github.com/googleapis/python-genai/issues/803)

---
*Research completed: 2026-03-27*
*Ready for roadmap: yes*

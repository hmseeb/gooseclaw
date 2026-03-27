# Feature Research: Real-Time Voice AI Dashboard

**Domain:** Real-time voice AI web interface for self-hosted AI agent platform
**Researched:** 2026-03-27
**Confidence:** HIGH (Gemini Live API officially documented, competitor patterns well-established, GooseClaw constraints clear)

## Feature Landscape

### Table Stakes (Users Expect These)

Features users assume exist in a voice AI interface. Missing any of these and the dashboard feels broken or amateur.

#### Voice Interaction Core

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Push-to-talk button (mic toggle) | The most basic voice interaction pattern. Every voice app has a big obvious mic button. Users from ChatGPT, Gemini, Alexa all expect this. Without it, there's no voice product. | LOW | Single HTML button with `getUserMedia()` + Web Audio API. Spacebar should be the keyboard shortcut (hold-to-talk). This is the universal convention across Claude Code, Copilot, Discord, gaming. Mobile: big fat tap target. |
| Real-time audio streaming to Gemini | Voice input must stream continuously, not record-then-send. Users expect conversational latency (<500ms), not walkie-talkie UX. Gemini Live API is designed for this via WebSocket. | HIGH | WebSocket connection from browser -> gateway.py proxy -> Gemini Live API. Audio encoded as base64 PCM over WebSocket. Gateway proxies because API key must never reach browser. This is the hardest single feature. |
| Audio playback of AI responses | Users expect to hear the AI talk back. Gemini Live API returns audio natively (STT+LLM+TTS in one model). Must play audio chunks as they arrive, not buffer the full response. | MEDIUM | Web Audio API or AudioContext for streaming playback. Gemini sends audio chunks over WebSocket. Decode and queue for gapless playback. Handle Chrome autoplay policy (require user gesture before first play). |
| Voice Activity Detection (VAD) | Gemini Live API has built-in automatic VAD. Users expect the system to know when they've stopped talking and respond. No manual "I'm done" button. | LOW | Gemini handles this server-side by default via `AutomaticActivityDetection`. Nothing to build, just configure. If needed, can disable and do client-side VAD for lower latency, but start with Gemini's built-in. |
| Barge-in / interruption support | Users expect to interrupt the AI mid-sentence, just like ChatGPT voice mode and Gemini Live. This is how natural conversation works. Without it, voice feels robotic. | LOW | Gemini Live API handles this natively. When VAD detects user speech during AI output, it cancels ongoing generation. Browser must stop playing current audio and start streaming user input. |
| Live transcript display | Users need to see what they said and what the AI said as text. ChatGPT unified their voice and text in late 2025 specifically because the separate-screen approach was terrible UX. Transcripts are essential for: verifying the AI heard correctly, reviewing conversation, accessibility. | MEDIUM | Gemini Live API provides audio transcription. Display as a scrolling chat-style transcript. Show partial/interim transcripts while user is speaking (word-by-word appearance). Final transcript replaces interim when utterance completes. |
| Connection state indicators | Users must know: am I connected? Is it listening? Is it thinking? Is it speaking? Without clear state, users talk into the void. Every voice assistant uses visual state transitions. | LOW | State machine: DISCONNECTED -> CONNECTING -> CONNECTED/IDLE -> LISTENING -> PROCESSING -> SPEAKING -> IDLE. Show state via mic button color/animation + status text. Map to Gemini WebSocket events. |
| Graceful error handling | Network drops, API quota exceeded, mic permission denied, WebSocket disconnects. Users must get clear feedback, not a blank screen. Voice is especially unforgiving because there's no text input fallback during errors. | MEDIUM | Error states for: mic permission denied (show how-to), WebSocket disconnect (auto-reconnect with exponential backoff), API errors (display message + retry button), quota exceeded (clear message). Never fail silently. |

#### Authentication & Security

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Ephemeral token authentication | Browser cannot hold the Gemini API key. Standard pattern is: browser requests short-lived token from gateway, gateway issues scoped token, browser uses token for WebSocket session. OpenAI Realtime, xAI Voice, and Google all use this pattern. | MEDIUM | Gateway.py generates a time-limited token (e.g., JWT or HMAC-signed session ID). Browser sends token on WebSocket handshake. Gateway validates before proxying to Gemini. Tokens expire after session ends or timeout. |
| Dashboard gated on Gemini key presence | If the user hasn't configured a Gemini API key in setup wizard, the voice dashboard should not be accessible. Show a clear message directing them to setup. This prevents confusion. | LOW | Check vault for Gemini key on dashboard page load. If missing, render "Configure Gemini in Setup" message with link. Same pattern as existing provider checks. |
| Session-scoped auth (reuse existing PBKDF2 auth) | Voice dashboard must be behind the same auth as the rest of GooseClaw. No separate login. Users are already authenticated via the existing PBKDF2 session system. | LOW | Reuse existing cookie-based auth from gateway.py. WebSocket upgrade request includes the auth cookie. Gateway validates before establishing proxy. |

#### Mobile & Responsive

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Mobile-responsive voice interface | PROJECT.md says "Works on phone and desktop browsers, no app install." Voice is primarily a mobile use case (talking to your agent while away from desk). The dashboard must work on phone browsers. | MEDIUM | Responsive layout: mic button dominates mobile viewport. Transcript scrolls below. Touch-friendly controls (44px+ tap targets). Test on iOS Safari + Chrome Android. Use viewport meta tag, flexible grid. |
| Browser microphone permission handling | Mobile browsers require user gesture to grant mic access. Must handle the permission flow gracefully: request on first tap, show instructions if denied, handle "ask every time" browsers. | LOW | Call `navigator.mediaDevices.getUserMedia({audio: true})` on mic button tap. Handle `NotAllowedError` with user-friendly instructions. Store permission state. iOS Safari requires HTTPS (Railway handles TLS). |
| Screen wake lock during voice session | Phone screens dim/lock during voice conversations. Users expect the screen to stay on while talking to their AI. Without this, the session interrupts mid-conversation. | LOW | Use Screen Wake Lock API (`navigator.wakeLock.request('screen')`). Release on session end. Fallback: no-op on unsupported browsers. Well-supported on Chrome Android, partial on iOS Safari. |

### Differentiators (Competitive Advantage)

Features that make GooseClaw's voice dashboard stand out from ChatGPT/Gemini consumer apps. These align with GooseClaw's core value: self-hosted AI agent that does things for you.

#### Tool Execution During Voice Conversation

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Mid-conversation tool calling (Gmail, Calendar, memory, etc.) | THIS IS THE KILLER FEATURE. ChatGPT voice mode explicitly cannot use tools, search the web, or access history during voice. Gemini Live API supports function calling natively. GooseClaw can wire Gemini's tool calls to existing MCP tools (mem0 memory, knowledge search, job engine). "Hey, check my calendar for tomorrow" actually works. | HIGH | Gemini Live API sends `BidiGenerateContentToolCall` messages. Gateway intercepts, executes against goose/MCP tools, returns `FunctionResponse`. Must handle sync (blocking conversation) vs async (NON_BLOCKING, available on 2.5 Flash). Tool results feed back into voice response. This requires mapping Gemini function declarations to GooseClaw's MCP tool catalog. |
| Visual tool execution feedback | When a tool runs mid-conversation, show it in the transcript: "Checking your calendar..." with a spinner, then the result. Users see the agent working, not just waiting in silence. | MEDIUM | Insert tool-call events into the transcript stream. Show tool name, status (running/complete/error), and summarized result. Same pattern as ChatGPT showing "Searching..." in text mode. |
| Notification bus integration | Voice session can receive notifications from the job engine ("Your daily summary is ready"). Notification appears in transcript and can be read aloud. | LOW | Existing notification bus already supports channel targeting. Voice channel registers as a target. Incoming notifications insert into the WebSocket stream. |

#### Voice Session Intelligence

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Automatic memory extraction from voice sessions | Voice conversations auto-feed into mem0 memory, just like text sessions do. The agent remembers what you told it by voice. No other self-hosted voice assistant does this. | MEDIUM | On session end, collect full transcript. Feed to existing `_memory_writer_loop()` pipeline (same as text channels). mem0 extracts facts, updates knowledge graph. Voice conversations become first-class memory sources. |
| Voice visualizer / reactive orb | An animated visual element that reacts to audio input/output volume. Not just a static mic icon. Think: pulsing orb when AI speaks, waveform when user speaks, calm glow when idle. This is the visual identity of a voice product. ChatGPT has the blue circle, Gemini has the colored dots. | MEDIUM | Web Audio API `AnalyserNode` for real-time frequency data. CSS animations or Canvas 2D (avoid WebGL, single HTML constraint). Map audio amplitude to animation intensity. Three visual states: idle (subtle pulse), listening (waveform), speaking (reactive orb). |
| Conversation history (voice sessions list) | List of past voice conversations with timestamps and transcript previews. Users can review what they discussed. Text channels already have session management. | MEDIUM | Store voice session transcripts in /data/voice_sessions/ as JSON. Dashboard shows session list with date, duration, preview. Tap to view full transcript. Integrate with existing session management patterns in gateway.py. |

#### Desktop Power Features

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Keyboard shortcuts | Spacebar hold-to-talk, Escape to disconnect, Enter to toggle continuous mode. Power users expect keyboard control. Every desktop voice app does this. | LOW | `keydown`/`keyup` event listeners. Spacebar: hold = push-to-talk, release = stop. Escape: end session. Prevent spacebar from scrolling page. Show keyboard shortcut hints on hover. |
| Seamless text-to-voice switching | User can type a message in the same interface when voice isn't convenient. Unified thread like ChatGPT's 2025 update. Some moments you want to whisper, some you want to type. | MEDIUM | Text input field below transcript. Typed messages sent to Gemini Live API as text input (supported alongside audio). Responses come back as audio + transcript. Single conversation thread regardless of input mode. |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Always-on listening (wake word) | "Hey Goose" hands-free activation like Alexa/Google Home | Browser tabs can't run persistent background listeners. Battery killer on mobile. Privacy nightmare for self-hosted app (continuous mic streaming to server). Push-to-talk is the correct pattern for a web dashboard. Wake words require local ML models (adds enormous complexity to single-HTML constraint). | Push-to-talk with spacebar hold. Clear, intentional activation. Zero privacy concerns. |
| WebRTC peer-to-peer connection | "Skip the proxy, connect browser directly to Gemini" | Gemini API key would be exposed to the browser. GooseClaw's architecture requires the gateway as a security boundary. Direct connection also bypasses tool calling integration (gateway executes MCP tools). WebRTC adds STUN/TURN complexity that's overkill for a proxied connection. | WebSocket through gateway.py proxy. Gateway holds the API key, executes tools, and relays audio. Simple, secure, proven pattern. |
| Multiple simultaneous voice sessions | "Support two users talking at once" | GooseClaw is explicitly single-user ("personal agent"). Multiple voice streams multiply bandwidth and API costs. Goose process is single-session. PROJECT.md says no multi-user auth. | Single active voice session. If a session is active, show "Voice session in progress" to other tabs. |
| Voice cloning / custom TTS voices | "Let me use my own voice" or "Give the AI a specific voice" | Gemini Live API handles TTS internally. GooseClaw has no control over voice synthesis outside of Gemini's offered voices (30 HD voices available). Voice cloning requires separate TTS service, adds latency, and breaks the single-model-handles-everything architecture advantage. | Use Gemini's built-in voice selection. 30 HD voices in 24 languages is plenty. Expose voice picker in settings. |
| Offline voice mode | "Work without internet" | The entire architecture depends on Gemini Live API (cloud). Offline would require local STT + LLM + TTS stack, which is a completely different product. GooseClaw is a cloud-deployed agent on Railway. | Clear error message when offline. No fake offline capability. |
| Video/camera input | "Show Gemini what I'm looking at" | Gemini Live API supports video, but GooseClaw's value is agent actions (tools, memory, jobs), not visual understanding. Video adds massive bandwidth requirements, mobile battery drain, and UX complexity. Scope creep away from voice-first agent. | Defer to v7+. Voice-only for v6.0. Camera can be added later as the API supports it, but it's a separate feature scope. |
| React/framework-based dashboard | "Use React like Google's live-api-web-console" | GooseClaw constraint: single HTML file, no build tooling. React would require npm, bundler, build step. This is a hard architectural constraint from PROJECT.md. Google's example is a dev reference, not a deployment model. | Vanilla HTML/CSS/JS in a single file (voice.html). Same pattern as setup.html and admin.html. Web Audio API, WebSocket API, and CSS animations are all available without frameworks. |
| Speech-to-speech translation | "Translate my speech to another language in real-time" | Gemini 2.5+ supports this, but it's a specialized use case that adds UX complexity (language selectors, dual transcripts) for minimal value in a personal agent context. The user's agent speaks their language. | Support Gemini's multilingual understanding natively (it handles 24 languages). If user speaks Spanish, the AI understands. No explicit translation UI needed. |

## Feature Dependencies

```
[Push-to-talk button]
    └──requires──> [Browser microphone permission handling]
    └──requires──> [WebSocket connection to gateway]
                       └──requires──> [Ephemeral token auth]
                       └──requires──> [Gemini API key in vault]
                                          └──requires──> [Setup wizard Gemini key config]

[Audio streaming to Gemini]
    └──requires──> [WebSocket proxy in gateway.py]
    └──requires──> [Push-to-talk button]
    └──requires──> [Audio playback of AI responses]

[Live transcript display]
    └──requires──> [Audio streaming to Gemini] (transcripts come from Gemini API)

[Mid-conversation tool calling]
    └──requires──> [WebSocket proxy in gateway.py]
    └──requires──> [Gemini function declarations mapped to MCP tools]
    └──requires──> [Live transcript display] (show tool execution in transcript)

[Voice visualizer]
    └──requires──> [Push-to-talk button] (needs audio stream for reactive animation)
    └──enhances──> [Connection state indicators]

[Automatic memory extraction]
    └──requires──> [Live transcript display] (needs full transcript to extract from)
    └──requires──> [mem0 integration] (existing v5.0 feature)

[Conversation history]
    └──requires──> [Live transcript display]
    └──requires──> [Session storage on /data volume]

[Text-to-voice switching]
    └──requires──> [Live transcript display]
    └──enhances──> [Audio streaming to Gemini]

[Notification bus integration]
    └──requires──> [WebSocket connection to gateway]
    └──enhances──> [Live transcript display]

[Mobile responsive layout]
    └──enhances──> [Push-to-talk button]
    └──enhances──> [Live transcript display]

[Screen wake lock]
    └──enhances──> [Mobile responsive layout]
```

### Dependency Notes

- **Everything requires WebSocket proxy**: The gateway.py WebSocket proxy is the foundational infrastructure. It must exist before any voice feature works. This is the first thing to build.
- **Transcript enables intelligence features**: Memory extraction, conversation history, and tool feedback all depend on having a transcript stream. Build transcript before intelligence.
- **Tool calling is the differentiator but requires foundation**: Mid-conversation tool calling is the killer feature but sits on top of WebSocket proxy + function declaration mapping. Build the voice pipeline first, then add tools.
- **Mobile responsive is parallel work**: Layout can be designed mobile-first from the start. Not a dependency, an enhancement that should be baked in from day one.

## MVP Definition

### Launch With (v6.0)

Minimum viable voice dashboard. Enough to talk to your AI agent and have it be useful.

- [ ] **WebSocket proxy in gateway.py** -- the foundational plumbing. Without this, nothing works.
- [ ] **Push-to-talk button with mic permission handling** -- the primary interaction method.
- [ ] **Real-time audio streaming (browser -> gateway -> Gemini -> gateway -> browser)** -- the voice pipeline.
- [ ] **Audio playback of AI responses** -- hear the AI respond.
- [ ] **VAD + barge-in** -- natural conversation flow (Gemini handles this, just don't break it).
- [ ] **Live transcript display** -- see what's being said, essential for usability + accessibility.
- [ ] **Connection state indicators** -- know when the system is listening/thinking/speaking.
- [ ] **Ephemeral token auth** -- secure the WebSocket without exposing API key.
- [ ] **Dashboard gated on Gemini key** -- don't show voice if no key configured.
- [ ] **Basic error handling** -- mic denied, WebSocket drop, API error messages.
- [ ] **Mobile responsive layout** -- phone browser usability from day one.
- [ ] **Setup wizard Gemini key config** -- add Gemini as optional provider.
- [ ] **Voice visualizer** -- visual identity, makes it feel like a real product not a demo.

### Add After Validation (v6.x)

Features to add once the core voice pipeline is stable and working.

- [ ] **Mid-conversation tool calling** -- wire Gemini function declarations to MCP tools. The killer differentiator. Add after voice pipeline is solid because debugging tool calling on top of a flaky voice connection is hell.
- [ ] **Visual tool execution feedback** -- show tool calls in transcript with spinners and results.
- [ ] **Automatic memory extraction from voice** -- feed voice transcripts into mem0 pipeline. Requires stable transcript collection.
- [ ] **Keyboard shortcuts (spacebar hold-to-talk)** -- desktop power user feature.
- [ ] **Screen wake lock** -- mobile quality-of-life.
- [ ] **Gemini voice selection** -- let users pick from available voices.
- [ ] **Conversation history** -- store and list past voice sessions.

### Future Consideration (v7+)

Features to defer until voice dashboard has proven itself.

- [ ] **Text-to-voice switching** -- unified thread with typed + spoken messages. Nice but not essential for voice-first.
- [ ] **Notification bus integration** -- notifications during voice sessions. Low-priority until voice sessions are common.
- [ ] **Video/camera input** -- Gemini API supports it, but separate scope entirely.
- [ ] **Multi-language voice selection** -- Gemini supports 24 languages natively, but UI for language switching is extra work.

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| WebSocket proxy in gateway.py | HIGH | HIGH | P1 |
| Push-to-talk button | HIGH | LOW | P1 |
| Audio streaming pipeline | HIGH | HIGH | P1 |
| Audio playback | HIGH | MEDIUM | P1 |
| Live transcript | HIGH | MEDIUM | P1 |
| Connection state indicators | HIGH | LOW | P1 |
| Ephemeral token auth | HIGH | MEDIUM | P1 |
| Dashboard gated on key | MEDIUM | LOW | P1 |
| Error handling | HIGH | MEDIUM | P1 |
| Mobile responsive layout | HIGH | MEDIUM | P1 |
| Setup wizard Gemini key | MEDIUM | LOW | P1 |
| Voice visualizer | MEDIUM | MEDIUM | P1 |
| Mid-conversation tool calling | HIGH | HIGH | P2 |
| Tool execution feedback | MEDIUM | MEDIUM | P2 |
| Memory extraction from voice | HIGH | MEDIUM | P2 |
| Keyboard shortcuts | MEDIUM | LOW | P2 |
| Screen wake lock | LOW | LOW | P2 |
| Voice selection | LOW | LOW | P2 |
| Conversation history | MEDIUM | MEDIUM | P2 |
| Text-to-voice switching | MEDIUM | MEDIUM | P3 |
| Notification integration | LOW | LOW | P3 |
| Video/camera input | LOW | HIGH | P3 |

**Priority key:**
- P1: Must have for launch (v6.0)
- P2: Should have, add when voice pipeline is stable (v6.x)
- P3: Nice to have, future consideration (v7+)

## Competitor Feature Analysis

| Feature | ChatGPT Voice | Gemini Live (Consumer) | Alexa/Google Home | GooseClaw Voice (Our Approach) |
|---------|---------------|----------------------|-------------------|-------------------------------|
| Push-to-talk | Tap mic button | Tap to start, auto-listens | Wake word + always listening | Tap button or hold spacebar. No wake word (web constraint). |
| Barge-in | Yes (2025 update) | Yes (native) | Yes | Yes (Gemini handles it) |
| Live transcript | Yes (unified interface since Nov 2025) | Yes (in app) | No (voice only) | Yes, chat-style scrolling transcript |
| Tool calling during voice | NO. Cannot search, use tools, or access history in voice mode. Major limitation. | Yes (Google services: Calendar, Keep, Tasks) | Yes (Alexa Skills) | YES. MCP tools: memory, knowledge, Gmail, Calendar, jobs. This is our edge. |
| Visual feedback | Blue animated circle | Colored dots animation | Light ring | Voice-reactive orb/waveform (Web Audio API) |
| Session persistence | Conversations saved in chat history | Within app session | No history | Transcripts saved, searchable, memory-extracted |
| Mobile experience | Native iOS/Android app | Native app | Dedicated hardware | Mobile web browser (no install). PWA-capable. |
| Self-hosted | No | No | No | YES. All data on your Railway volume. |
| Multi-provider | OpenAI only | Google only | Amazon only | Gemini for voice, but text channels use any of 23+ providers |
| Memory integration | Conversation history + memory feature | Google account context | Limited routines | mem0 vector + knowledge graph. Voice feeds same memory as text. |
| Open source | No | No | No | YES. Fork, modify, self-host. |

## Sources

- [Google Gemini Live API - Tool Use](https://ai.google.dev/gemini-api/docs/live-api/tools) -- HIGH confidence, official docs
- [Google Gemini Live API - Capabilities Guide](https://ai.google.dev/gemini-api/docs/live-guide) -- HIGH confidence, official docs
- [Google live-api-web-console (React reference app)](https://github.com/google-gemini/live-api-web-console) -- HIGH confidence, official example
- [ChatGPT Voice Mode Unified Interface (TechCrunch, Nov 2025)](https://techcrunch.com/2025/11/25/chatgpts-voice-mode-is-no-longer-a-separate-interface/) -- MEDIUM confidence, verified industry reporting
- [ChatGPT Advanced Voice Mode FAQ](https://help.openai.com/en/articles/9617425-advanced-voice-mode-faq) -- HIGH confidence, official docs (confirms no tool use in voice)
- [OpenAI WebRTC Ephemeral Keys Pattern](https://www.hitek.agency/blog/webrtc-openai-realtime-guide) -- MEDIUM confidence, technical tutorial
- [Building Resilient Voice AI Systems](https://www.cekura.ai/blogs/the-silence-between-words-architecting-resilient-voice-ai-systems) -- MEDIUM confidence, industry analysis
- [MDN Web Audio API Visualizations](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API/Visualizations_with_Web_Audio_API) -- HIGH confidence, official docs
- [Building a Voice Reactive Orb in React](https://medium.com/@therealmilesjackson/building-a-voice-reactive-orb-in-react-audio-visualization-for-voice-assistants-2bee12797b93) -- LOW confidence, community tutorial
- [Voice UI Design Guide 2026](https://fuselabcreative.com/voice-user-interface-design-guide-2026/) -- MEDIUM confidence, industry guide
- [Gemini Live API Native Audio on Vertex AI](https://cloud.google.com/blog/topics/developers-practitioners/how-to-use-gemini-live-api-native-audio-in-vertex-ai) -- HIGH confidence, official blog
- [Comparing Conversational AI UIs 2025](https://intuitionlabs.ai/articles/conversational-ai-ui-comparison-2025) -- MEDIUM confidence, industry analysis

---
*Feature research for: GooseClaw v6.0 Voice Dashboard*
*Researched: 2026-03-27*

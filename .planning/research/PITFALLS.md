# Pitfalls Research

**Domain:** Real-time voice AI dashboard (adding WebSocket voice channel to existing Python stdlib HTTP server)
**Researched:** 2026-03-27
**Confidence:** MEDIUM-HIGH (Gemini Live API is preview, some findings are LOW confidence)

## Critical Pitfalls

### Pitfall 1: Python stdlib http.server Was Not Designed for Long-Lived Connections

**What goes wrong:**
`ThreadingHTTPServer` spawns one thread per request. Each thread is expected to handle a request quickly and die. WebSocket connections are long-lived (minutes to hours). With voice sessions, each connected user ties up a thread indefinitely. The `do_GET` handler returns when the request is done, but a WebSocket connection never "finishes" in the HTTP sense. You end up fighting the framework's assumptions at every turn: the handler wants to close the socket, the server wants to reap the thread, and none of the connection lifecycle hooks expect persistent bidirectional communication.

**Why it happens:**
The existing gateway.py (10K+ lines) works great as an HTTP server because it follows the request/response pattern `http.server` was built for. Developers assume "I'll just upgrade the connection and keep reading/writing" but `http.server` has no concept of WebSocket frames, ping/pong, or connection state machines.

**How to avoid:**
- Implement WebSocket as a raw socket takeover: after the HTTP 101 handshake, extract the underlying `self.request` socket from the handler and hand it to a dedicated WebSocket management loop running in its own thread. Do NOT try to keep using the `BaseHTTPRequestHandler` methods after upgrade.
- Build a minimal RFC 6455 frame parser (handshake + frame read/write + masking + ping/pong). It is ~200 lines of code. The handshake is just: read `Sec-WebSocket-Key`, SHA-1 hash with magic GUID, base64 encode, respond with 101.
- Cap concurrent WebSocket connections (this is a single-user app, so 1-2 max is fine).
- Use `threading.Thread(daemon=True)` for the WebSocket read/write loops so they die with the process.

**Warning signs:**
- Handler method returns but socket stays open (undefined behavior)
- Thread count grows without bound during testing
- `BrokenPipeError` or `ConnectionResetError` on socket operations after handler returns

**Phase to address:**
Phase 1 (WebSocket infrastructure). This is the foundation everything else depends on. Get this wrong and nothing works.

---

### Pitfall 2: Gemini Live API Session Limits Will Kill Voice Calls Silently

**What goes wrong:**
The Gemini Live API has hard session limits that are easy to miss:
- **Connection lifetime: ~10 minutes** regardless of activity (the server sends a `GoAway` message before disconnecting)
- **Audio-only sessions: 15 minutes** without context window compression (context fills up)
- **Audio+video sessions: 2 minutes** without compression
- **Context window: 128K tokens** (native audio models consume ~25 tokens/second)

At 25 tokens/second, a 15-minute voice call consumes ~22,500 tokens of context. Without compression, the session just dies. Users will be mid-sentence when the connection drops with no explanation.

**Why it happens:**
Developers build a happy-path demo that works for 5 minutes and ship it. The 10-minute connection reset is not obvious in short testing sessions. The 15-minute context limit only hits when you actually have a real conversation.

**How to avoid:**
- Enable context window compression from day one: `ContextWindowCompressionConfig(sliding_window=SlidingWindow())`. This extends sessions to unlimited duration by discarding old context.
- Implement session resumption: capture `SessionResumptionUpdate` tokens from the server (valid for 2 hours). When `GoAway` arrives, reconnect using the resumption handle within the `timeLeft` grace period.
- Monitor for `GoAway` messages and proactively reconnect before the server kills the connection.
- Show users a "reconnecting..." indicator rather than a hard error when session cycling occurs.

**Warning signs:**
- Voice calls work perfectly for 8 minutes then drop
- No error in logs, just a clean WebSocket close from Gemini
- Testing only in short sessions masks the problem entirely

**Phase to address:**
Phase 2 (Gemini Live API integration). Must be built into the connection manager from the start, not bolted on later.

---

### Pitfall 3: Railway Proxy Will Kill Idle WebSocket Connections at 10 Minutes

**What goes wrong:**
Railway's load balancer / proxy layer kills idle TCP connections. "When proxying requests at Railway scale, keep alive connections have to be killed." Users report consistent disconnections "exactly after 10 minutes" even with bidirectional traffic. This compounds with Gemini's own 10-minute connection limit, creating a double timeout problem.

**Why it happens:**
Railway's proxy infrastructure is optimized for HTTP request/response, not persistent connections. WebSocket connections that look idle (no frame-level traffic) get reaped. Even connections with application-level activity can be killed if the proxy doesn't see WebSocket ping/pong frames.

**How to avoid:**
- Implement WebSocket ping/pong at the protocol level (not application-level JSON messages) every 20-25 seconds. 25 seconds is the sweet spot: clears the 30-second cellular NAT timeout and stays under the 60-second default of most proxies.
- The server must send WebSocket ping frames (opcode 0x9) and handle pong responses (opcode 0xA). This is in addition to any application-level heartbeat.
- Add a custom domain to the Railway project (Railway docs suggest this helps).
- Handle reconnection gracefully on the client side with exponential backoff.

**Warning signs:**
- Works locally, dies in production after exactly 10 minutes
- Works on desktop but fails on mobile (cellular NATs are more aggressive)
- Intermittent disconnects that seem random but are actually timeout-aligned

**Phase to address:**
Phase 1 (WebSocket infrastructure). The keepalive mechanism must be baked into the WebSocket frame handler, not added as an afterthought.

---

### Pitfall 4: Browser Audio Autoplay Policy Blocks Voice Playback on First Interaction

**What goes wrong:**
iOS Safari (and increasingly other mobile browsers) block all audio playback unless initiated by a user gesture. You create an `AudioContext`, connect it to playback, and... silence. No error thrown. The `AudioContext` state is `suspended` and will stay that way until the user taps something. This is particularly insidious because it works perfectly on desktop during development.

**Why it happens:**
Browser vendors implemented autoplay policies to stop websites from blasting audio without permission. The Web Audio API is subject to the same policy. If you create the `AudioContext` at page load or in response to a WebSocket message (not a user click/tap), it stays suspended.

**How to avoid:**
- Create the `AudioContext` inside the mic button's click handler. A single user gesture unlocks it for the entire session.
- After creating the context, call `audioContext.resume()` explicitly and await it.
- Design the UI flow so the user MUST tap a "Start conversation" button before any audio processing begins. This is not just good UX, it is a technical requirement.
- Check `audioContext.state === 'running'` before attempting playback. If suspended, show a "tap to enable audio" prompt.
- Reuse the same `AudioContext` for the entire session. Do not create new ones.

**Warning signs:**
- Works on Chrome desktop, silent on iOS Safari
- `audioContext.state` logged as `'suspended'` in mobile console
- No errors thrown, just no audio output

**Phase to address:**
Phase 3 (browser audio UI). The mic button click handler should be the single point where AudioContext is created and mic permissions are requested.

---

### Pitfall 5: Gemini Live API Tool Calling Is Synchronous and Blocks Voice Response

**What goes wrong:**
On Gemini 3.1 Flash Live, function calling is synchronous only. "The model will not start responding until you've sent the tool response." This means if a user asks "what's on my calendar today?" and the tool call takes 3 seconds, there are 3 seconds of dead silence. Worse: if the tool call fails or times out, the conversation hangs indefinitely. The model literally cannot speak until it gets the function response.

Additionally, when VAD detects an interruption during a pending tool call, Gemini discards the pending function calls and sends their IDs as cancelled. If your proxy already dispatched the tool call to goose, you now have orphaned work running in the background.

**Why it happens:**
Developers familiar with non-realtime LLM tool calling don't realize the blocking nature in Live API. The latency of tool execution directly becomes voice latency. Async function calling (NON_BLOCKING behavior) is only available on Gemini 2.5 Flash Live, not 3.1 Flash Live.

**How to avoid:**
- Set aggressive timeouts on all tool calls (2-3 seconds max). If a tool doesn't respond in time, send a "still working on that" placeholder response.
- Use the `scheduling` parameter on function responses: `SILENT` for background updates, `WHEN_IDLE` for non-urgent results, `INTERRUPT` only for time-critical responses.
- Track cancelled tool call IDs from interruption messages. If goose already started executing, let it finish but discard the result.
- Consider pre-fetching likely tool results during conversation setup (e.g., calendar for today, recent emails).
- Test with realistic tool latencies. A 100ms mock does not reveal the problem.

**Warning signs:**
- Awkward silence after tool-triggering questions
- Conversation hangs and never resumes
- Duplicate or orphaned tool executions in goose logs
- User interrupts during tool call, causing confusing state

**Phase to address:**
Phase 4 (tool calling integration). Build the tool call proxy with timeout handling and cancellation from the start.

---

### Pitfall 6: WebSocket Frame Parsing Bugs in Stdlib Implementation

**What goes wrong:**
When implementing RFC 6455 frame parsing from scratch (required since gateway.py is stdlib-only), subtle bugs in masking, fragmentation, or payload length parsing cause silent data corruption or crashes. The three most common bugs:
1. **Masking math error:** Client frames are masked with a 4-byte key via XOR. Getting the modulo indexing wrong (`data[i] ^ mask[i % 4]`) silently corrupts audio data.
2. **Extended payload length:** Payloads 126-65535 bytes use a 2-byte extended length field. Payloads over 65535 use an 8-byte field. Audio chunks hit these thresholds regularly. Misreading the length causes the parser to read frame headers as payload data, cascading into garbage.
3. **Fragmented frames:** Large audio chunks may be split across multiple frames (FIN bit = 0 on non-final frames). If you only handle single-frame messages, you silently drop audio data.

**Why it happens:**
RFC 6455 framing looks simple but has edge cases. Most tutorials show the happy path (small text messages, no fragmentation). Audio streams hit all the edge cases: large binary payloads, continuous streaming, high throughput.

**How to avoid:**
- Use an existing minimal Python WebSocket implementation as reference. The GitHub project "Pithikos/python-websocket-server" is a clean stdlib-only implementation.
- Write explicit tests for: masked binary frames, 126-byte extended length, 65536-byte extended length, fragmented messages, ping/pong handling, close frame handling.
- Test with actual audio data sizes (PCM 16-bit at 16kHz = 32KB/second, chunks of 640-1280 bytes every 20-40ms).
- Log frame metadata (opcode, length, FIN bit) during development to catch parsing errors early.

**Warning signs:**
- Audio sounds garbled or has periodic clicks/pops
- WebSocket connection drops with close code 1002 (protocol error)
- Intermittent crashes when audio chunks cross payload length boundaries
- Works with small text messages but fails with binary audio

**Phase to address:**
Phase 1 (WebSocket infrastructure). The frame parser is the lowest-level component. A bug here poisons everything above it.

---

### Pitfall 7: Safari/iOS MediaRecorder Does Not Support WebM/Opus, and You Should Not Use MediaRecorder Anyway

**What goes wrong:**
You use `MediaRecorder` with `mimeType: 'audio/webm;codecs=opus'` (the default on Chrome) and it works great. On Safari, `MediaRecorder.isTypeSupported('audio/webm;codecs=opus')` returns false. Safari supports MP4/AAC only. But you don't actually need `MediaRecorder` at all for this use case, and using it is a trap.

The Gemini Live API wants raw PCM audio (16-bit, 16kHz, little-endian), NOT encoded audio. MediaRecorder encodes audio into container formats (WebM, MP4), which then need to be decoded back to PCM on the server. This is a pointless encode-decode cycle that adds latency and complexity.

**Why it happens:**
Developers default to MediaRecorder because it is the "standard" way to capture audio in browsers. They don't realize that for real-time voice streaming, you need raw PCM from the Web Audio API, not encoded chunks from MediaRecorder.

**How to avoid:**
- Use `getUserMedia()` + `AudioWorklet` (or `ScriptProcessorNode` as fallback) to capture raw PCM samples directly.
- Create the AudioContext with `sampleRate: 16000` to match Gemini's expected input rate. Chrome and Safari both support custom sample rates.
- Convert Float32Array samples to 16-bit PCM integers: `Math.max(-1, Math.min(1, sample)) * 0x7FFF`.
- Base64 encode the PCM buffer and send via WebSocket as JSON, or send as binary WebSocket frames (saves ~33% bandwidth).
- Skip MediaRecorder entirely. It solves a different problem (recording to file).

**Warning signs:**
- Works on Chrome, fails or produces different format on Safari
- Audio arriving at server needs decoding before sending to Gemini
- Latency spikes from encode/decode overhead
- Extra complexity handling multiple container formats

**Phase to address:**
Phase 3 (browser audio). Choose the right capture approach from the start. AudioWorklet, not MediaRecorder.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Skip session resumption | Simpler connection logic | Users lose conversation on every 10-min reconnect | Never. Gemini's 10-min limit makes this mandatory. |
| Skip WebSocket ping/pong, use app-level heartbeat | Simpler frame handler | Railway proxy kills connection, mobile NATs drop it | Never on Railway. Protocol-level pings are the only reliable keepalive. |
| ScriptProcessorNode instead of AudioWorklet | Works in all browsers including older ones | Runs on main thread, causes audio glitches, deprecated | MVP only, must migrate before production |
| Single WebSocket for everything (voice + control) | One connection to manage | Audio floods control messages, priority inversion | MVP only. Separate data/control channels or at minimum message priorities. |
| Hardcode PCM sample rate (skip resampling) | Simpler audio pipeline | Breaks if browser AudioContext refuses 16kHz (rare but possible) | MVP, but add fallback resampling path |
| Skip context window compression | Simpler Gemini config | 15-minute session hard limit, abrupt disconnection | Never. Always enable compression. |
| Base64 encode audio in JSON (vs binary frames) | Simpler parsing on both ends | ~33% bandwidth overhead, higher latency | Acceptable for single-user. Optimize if latency matters. |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Gemini Live API | Using deprecated `LiveConnectConfig.generation_config` | Set `response_modalities`, `speech_config`, etc. directly on `LiveConnectConfig`. Deprecated pattern becomes an error after Q3 2025. |
| Gemini Live API | Sending tool responses via `BidiGenerateContentToolResponse` | Use `FunctionResponse` Part wrapped in `clientContent` with `turnComplete: true`. The documented format has known issues (cookbook #906). |
| Gemini Live API | Not sending `audioStreamEnd` after pauses > 1 second | Send `audioStreamEnd` event to flush cached audio data. Without this, Gemini waits for more audio instead of processing what it has. |
| Gemini Live API | Connecting from browser directly with long-lived API key | Use server-generated ephemeral tokens. Token creation: `client.auth_tokens.create()` with `v1alpha` API. Default TTL: 1 min for new sessions, 30 min for messages. |
| Railway | Using `wss://` URL without custom domain | Add custom domain. Railway's default domain may have stricter proxy rules for WebSocket connections. |
| Web Audio API | Creating AudioContext before user gesture | Create inside click handler. Call `audioContext.resume()` and await it. Check `state === 'running'` before proceeding. |
| Web Audio API | Assuming microphone permission persists (Safari) | Safari permissions are per-session and less persistent than Chrome. Always handle permission re-request gracefully. |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Base64 encoding audio on main thread | UI jank during voice conversations, audio stutters | Move PCM-to-base64 conversion into AudioWorklet or use binary WebSocket frames | Any sustained voice conversation > 30 seconds |
| Buffering audio before sending (> 100ms chunks) | Noticeable delay between speaking and hearing response | Send 20-40ms chunks. Do not buffer beyond 100ms. | Immediately noticeable to users, destroys conversational feel |
| Not clearing audio playback queue on interruption | Model's old audio keeps playing after user speaks | When server sends `interrupted: true`, immediately clear the playback buffer and stop current audio output | Every conversation with interruptions |
| Synchronous tool execution in WebSocket thread | Entire WebSocket relay freezes while tool executes | Dispatch tool calls to goose in a separate thread. Use a queue with timeout. Return tool response asynchronously. | Any tool call > 500ms |
| Thread-per-WebSocket without connection limits | Memory leak, thread exhaustion if browser reconnects rapidly | Cap at 2 concurrent WebSocket connections. Close old connections on new connect from same auth token. | Edge case: network flapping causes rapid reconnections |
| Polling for Gemini responses instead of event-driven | Wasted CPU, added latency, complex timing code | Use blocking reads on the Gemini WebSocket with timeout. Process messages as they arrive. | Immediately. Polling adds minimum latency of poll interval. |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Shipping Gemini API key to the browser | Key exposure in browser DevTools, token theft, unauthorized usage billed to user | Generate ephemeral tokens server-side. Lock tokens to specific model and config. Set `uses: 1`. |
| No auth on voice WebSocket endpoint | Anyone with the URL can start voice sessions, consuming Gemini API credits | Reuse existing GooseClaw auth (PBKDF2 password check) before upgrading to WebSocket. Pass auth token in initial WebSocket message or query param. |
| Audio data traverses untrusted networks in plaintext | Voice content interception | Railway handles TLS termination. Ensure `wss://` (not `ws://`) is used in browser. Ephemeral tokens have built-in transport security. |
| Ephemeral token endpoint accessible without auth | Token farming, API credit theft | Gate token generation behind existing GooseClaw session auth. Rate limit token creation. |
| Not validating WebSocket origin header | Cross-site WebSocket hijacking | Check `Origin` header during WebSocket upgrade handshake. Reject connections from unexpected origins. |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| No visual feedback during tool execution silence | User thinks connection is broken, mashes buttons | Show "thinking..." animation or a subtle pulsing indicator when model is waiting for tool response |
| Hard error on session reconnect instead of seamless resume | User thinks conversation crashed, loses trust | Implement session resumption. Show brief "reconnecting..." then continue conversation naturally |
| No voice activity indicator | User unsure if mic is working, speaks louder, gets frustrated | Show real-time waveform/level meter that responds to mic input immediately |
| Latency spike with no indication | User starts repeating themselves, model hears duplicate input | Show connection quality indicator. If latency > 500ms, display warning |
| Mobile browser: no way to end call without closing tab | User can't stop voice session gracefully | Prominent stop button that stays visible, not hidden by keyboard or scroll |
| Starting audio before mic permission granted | Awkward state where audio plays but user can't respond | Request mic permission first. Only establish Gemini connection after permission granted. |
| VAD too sensitive to background noise | Model interrupts itself because it thinks user spoke | Configure `startOfSpeechSensitivity` and `endOfSpeechSensitivity`. Default may be too aggressive for noisy environments. Expose sensitivity control in UI. |

## "Looks Done But Isn't" Checklist

- [ ] **WebSocket ping/pong:** Often missing protocol-level ping. App-level heartbeat does NOT satisfy Railway's proxy or mobile NAT requirements.
- [ ] **Session resumption:** Demo works for 5 minutes. Real usage hits 10-minute connection limit. Must handle `GoAway` + reconnect with resumption token.
- [ ] **Context window compression:** Not enabled by default. Without it, sessions die at 15 minutes with no warning.
- [ ] **iOS audio unlock:** Works on desktop. Fails silently on iOS. Must create AudioContext inside user gesture handler.
- [ ] **Interrupted tool calls:** User interrupts during tool execution. Must handle cancelled tool call IDs and stop orphaned goose sessions.
- [ ] **Close frame handling:** WebSocket close frame (opcode 0x8) must be sent/received with close code. Many stdlib implementations forget this, causing "unclean close" errors.
- [ ] **Binary frame support:** Audio requires opcode 0x2 (binary). If frame handler only supports opcode 0x1 (text), audio transmission silently fails or crashes.
- [ ] **Mic permission re-request:** Safari forgets mic permissions between sessions. Must handle `NotAllowedError` gracefully every time.
- [ ] **Output audio sample rate:** Gemini outputs at 24kHz but browser AudioContext may be at 16kHz. Must handle sample rate mismatch or create separate playback context at 24kHz.
- [ ] **Ephemeral token refresh:** Token valid for 30 min message sending. Sessions that reconnect every 10 min need fresh token coordination.

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| WebSocket frame parser bug corrupts audio | LOW | Fix parser, add test with known-good audio frame. No state to repair. |
| Session drops without resumption | MEDIUM | Add resumption token capture. Requires refactoring connection manager to separate session state from connection state. |
| Thread leak from long-lived WebSocket | LOW | Add connection cap, use daemon threads, add explicit cleanup on close. |
| Safari audio silent (autoplay policy) | LOW | Move AudioContext creation to click handler. No architectural change. |
| Tool call blocks voice (sync only) | HIGH | Requires rethinking tool architecture. Must add timeout, background dispatch, and placeholder response generation. |
| MediaRecorder used instead of AudioWorklet | HIGH | Full rewrite of audio capture pipeline. Should be caught before implementation. |
| Gemini API key shipped to browser | HIGH | Architectural change to server-side proxy or ephemeral token generation. Must add token endpoint, change browser connection target. |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| stdlib http.server not designed for WebSocket | Phase 1: WebSocket infra | Verify WebSocket connection stays alive for 15+ minutes under load |
| Gemini session limits (10 min connection, 15 min audio) | Phase 2: Gemini integration | Verify voice call survives a 20-minute conversation with auto-reconnect |
| Railway proxy kills idle connections | Phase 1: WebSocket infra | Deploy to Railway, verify WebSocket survives 15 minutes of silence |
| Browser autoplay policy blocks audio | Phase 3: Browser audio UI | Test on iOS Safari. Audio must play after mic button tap. |
| Tool calling blocks voice (sync) | Phase 4: Tool integration | Time a tool call that takes 3 seconds. Verify user hears acknowledgment, not silence. |
| WebSocket frame parsing bugs | Phase 1: WebSocket infra | Run test suite with binary frames at all payload length boundaries (125, 126, 65536 bytes) |
| Safari MediaRecorder incompatibility | Phase 3: Browser audio UI | Verify on Safari that mic capture produces raw PCM, not encoded audio |
| Ephemeral token security | Phase 2: Gemini integration | Verify Gemini API key never appears in browser network tab |
| Thread leak / connection exhaustion | Phase 1: WebSocket infra | Rapid connect/disconnect 20 times, verify thread count returns to baseline |
| Interrupted tool calls (orphaned work) | Phase 4: Tool integration | Interrupt user during pending tool call, verify goose session cleans up |

## Sources

- [Railway WebSocket disconnect after 10 minutes](https://station.railway.com/questions/socket-disconnects-after-10-minutes-bbceef40) (MEDIUM confidence)
- [Railway WebSocket connection issues in production](https://station.railway.com/questions/web-socket-connection-issues-in-producti-ec8d4a69) (MEDIUM confidence)
- [Gemini Live API session management](https://ai.google.dev/gemini-api/docs/live-session) (HIGH confidence, official docs)
- [Gemini Live API capabilities guide](https://ai.google.dev/gemini-api/docs/live-api/capabilities) (HIGH confidence, official docs)
- [Gemini Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices) (HIGH confidence, official docs)
- [Gemini Live API tool calling](https://ai.google.dev/gemini-api/docs/live-api/tools) (HIGH confidence, official docs)
- [Gemini Live API ephemeral tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens) (HIGH confidence, official docs)
- [Gemini Live API WebSocket getting started](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket) (HIGH confidence, official docs)
- [Gemini cookbook issue #906: tool call response format](https://github.com/google-gemini/cookbook/issues/906) (MEDIUM confidence, community workaround)
- [Gemini Live API error 1008 during function calling](https://discuss.google.dev/t/gemini-live-api-apierror-1008-policy-violation-during-function-calling/337832) (LOW confidence, unresolved)
- [Gemini Live API hangs on function calls](https://github.com/googleapis/python-genai/issues/803) (LOW confidence, may be version-specific)
- [Safari MediaRecorder audio format issues](https://www.buildwithmatija.com/blog/iphone-safari-mediarecorder-audio-recording-transcription) (MEDIUM confidence)
- [Web Audio API best practices (MDN)](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API/Best_practices) (HIGH confidence)
- [Autoplay guide (MDN)](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay) (HIGH confidence)
- [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455) (HIGH confidence)
- [WebSocket keepalive and timeout guide](https://websocket.org/guides/troubleshooting/timeout/) (MEDIUM confidence)
- [Python http.server threading memory leak (CPython tracker)](https://bugs.python.org/issue37193) (MEDIUM confidence)
- [Pithikos/python-websocket-server (stdlib reference implementation)](https://github.com/Pithikos/python-websocket-server) (MEDIUM confidence)
- [Browser power-saving WebSocket disconnection pitfall](https://www.pixelstech.net/article/1719122489-the-pitfall-of-websocket-disconnections-caused-by-browser-power-saving-mechanisms) (MEDIUM confidence)
- [Suki: Lessons from scaling browser-based audio](https://www.suki.ai/blog/voice-first-future-lessons-from-scaling-browser-based-audio/) (MEDIUM confidence)

---
*Pitfalls research for: GooseClaw v6.0 Voice Dashboard*
*Researched: 2026-03-27*

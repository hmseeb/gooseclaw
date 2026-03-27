# Stack Research: Voice Dashboard (Gemini Live API)

**Domain:** Real-time voice AI channel (WebSocket audio streaming)
**Researched:** 2026-03-27
**Confidence:** HIGH (protocol/audio), MEDIUM (Gemini 3.1 Flash Live specifics, model is preview)

## Existing Stack (DO NOT CHANGE)

| Technology | Version | Purpose | Status |
|------------|---------|---------|--------|
| Python 3.10 | 3.10.x | Container runtime (stdlib only for gateway.py) | KEEP |
| http.server + ThreadingHTTPServer | stdlib | HTTP server, thread-per-request | KEEP |
| setup.html | single file | Self-contained HTML/CSS/JS wizard | Pattern to follow for voice.html |
| gateway.py | ~10K lines | Monolith HTTP handler, reverse proxy | Extend with WebSocket + voice endpoints |
| Docker ubuntu:22.04 | base image | Railway deployment | KEEP |

## New Stack Additions

### Core: Gemini 3.1 Flash Live API (Server-Side)

| Technology | Version/ID | Purpose | Why Recommended |
|------------|-----------|---------|-----------------|
| Gemini 3.1 Flash Live | `gemini-3.1-flash-live-preview` | STT + LLM + TTS in one model | Single model handles entire voice pipeline. No separate Whisper/TTS. 131K token context, 65K output tokens. Supports function calling during voice sessions. Released 2026-03-26. |
| Gemini Live API (WebSocket) | v1beta / v1alpha | Bi-directional audio streaming | Stateful WebSocket at `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent`. JSON messages with base64 audio. |
| Ephemeral Tokens API | v1alpha | Secure browser-to-API auth | Short-lived tokens (1min new session, 30min messages). Gateway creates via REST, browser uses to connect. Prevents exposing raw API key to client. |

**Confidence:** HIGH for protocol. MEDIUM for model stability (preview suffix, released yesterday).

**Key model facts:**
- Input: text, images, audio, video
- Output: text and audio
- Function calling: YES (sequential/synchronous only on 3.1 Flash Live)
- Audio generation pauses during tool execution, resumes after tool response
- Audio input: raw 16-bit PCM, 16kHz, little-endian
- Audio output: raw 16-bit PCM, 24kHz, little-endian
- Session limits: ~15 min audio-only (without compression), ~10 min connection lifetime
- Session resumption: tokens valid 2 hours after disconnect
- Context window compression: sliding window, extends sessions indefinitely
- Voices: configurable via `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName`
- Code execution: NOT supported on this model
- Structured outputs: NOT supported on this model
- Thinking: supported

**Fallback model:** If `gemini-3.1-flash-live-preview` is unstable (it's a day-old preview), fall back to `gemini-2.5-flash-live-001` which is GA. Note: Gemini 2.0 Flash retires June 2026, skip it entirely.

### WebSocket Server: Python stdlib RFC 6455 Implementation

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python `hashlib` + `struct` + `socket` | stdlib | WebSocket handshake + frame parsing | Gateway.py is stdlib-only. No pip allowed. WebSocket RFC 6455 handshake is ~50 lines (SHA-1 + base64 of Sec-WebSocket-Key). Frame parsing is ~100 lines (opcodes, masking, payload length). |
| Python `threading.Thread` | stdlib | Per-connection WebSocket handler | Matches existing ThreadingHTTPServer pattern. Each voice session gets its own thread. |
| Python `ssl` + `http.client` | stdlib | Outbound WebSocket client to Gemini | Gateway needs to be a WebSocket CLIENT connecting to Gemini's WSS endpoint. Use `ssl.create_default_context()` + raw socket + WebSocket handshake. |

**Confidence:** HIGH. RFC 6455 is well-specified, Python stdlib has everything needed (hashlib.sha1, struct.pack/unpack, base64, socket, ssl). The gateway already imports all these modules.

**Architecture: gateway.py is BOTH WebSocket server (browser-facing) AND WebSocket client (Gemini-facing).**

The WebSocket implementation needs two distinct components:

1. **WebSocket Server** (browser -> gateway): Upgrade HTTP connection in `GatewayHandler.do_GET` when path is `/ws/voice`. Perform RFC 6455 handshake, then hijack the socket from `http.server` for bidirectional framing.

2. **WebSocket Client** (gateway -> Gemini): Open outbound WSS connection to Gemini's endpoint using `ssl` + raw `socket`. Send setup message, then relay audio frames bidirectionally.

**WebSocket Handshake (server-side, ~30 lines):**
```python
import hashlib, base64

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def ws_accept_key(client_key):
    """Compute Sec-WebSocket-Accept from client's Sec-WebSocket-Key."""
    digest = hashlib.sha1((client_key + WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()
```

**WebSocket Frame Parsing (server-side, ~80 lines):**
```python
import struct

def ws_recv_frame(sock):
    """Read one WebSocket frame. Returns (opcode, payload_bytes)."""
    header = sock.recv(2)
    if len(header) < 2:
        return None, b""
    fin = header[0] & 0x80
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]
    if masked:
        mask_key = sock.recv(4)
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload

def ws_send_frame(sock, opcode, payload, mask=False):
    """Send one WebSocket frame. Server->client: mask=False."""
    frame = bytearray()
    frame.append(0x80 | opcode)  # FIN + opcode
    length = len(payload)
    if length < 126:
        frame.append(length | (0x80 if mask else 0))
    elif length < 65536:
        frame.append(126 | (0x80 if mask else 0))
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(127 | (0x80 if mask else 0))
        frame.extend(struct.pack(">Q", length))
    if mask:
        import os
        mask_key = os.urandom(4)
        frame.extend(mask_key)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    frame.extend(payload)
    sock.sendall(frame)
```

**Critical detail:** When acting as WebSocket CLIENT (to Gemini), frames MUST be masked (client->server per RFC 6455). When acting as WebSocket SERVER (to browser), frames MUST NOT be masked.

### Ephemeral Token Creation: REST API (No SDK)

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| `urllib.request` | stdlib | POST to Gemini auth_tokens endpoint | Gateway already uses urllib.request for outbound HTTP. No new dependency. |

**Confidence:** MEDIUM. REST endpoint path derived from SDK source code analysis. The SDK calls `POST /auth_tokens` on the v1alpha base URL.

**REST endpoint (derived from python-genai SDK source):**
```
POST https://generativelanguage.googleapis.com/v1alpha/auth_tokens?key=GEMINI_API_KEY
Content-Type: application/json

{
    "uses": 1,
    "expire_time": "2026-03-27T12:30:00Z",
    "new_session_expire_time": "2026-03-27T12:01:00Z"
}
```

Response contains a token object with a `name` field that becomes the ephemeral token.

**If REST endpoint doesn't work as derived:** Fall back to passing API key directly via query parameter on the WebSocket URL. Less secure but functional. The setup wizard already stores the Gemini API key in vault, so it's not exposed to end-users beyond the container.

**WebSocket URL with ephemeral token (v1alpha):**
```
wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=EPHEMERAL_TOKEN
```

**WebSocket URL with API key directly (v1beta, fallback):**
```
wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=GEMINI_API_KEY
```

### Browser: Web Audio API + Native WebSocket

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Web Audio API (AudioWorklet) | Browser native | Mic capture as PCM 16kHz 16-bit mono | No npm. AudioWorklet runs in separate thread for low-latency capture. Create AudioContext at sampleRate: 16000 to avoid resampling. |
| WebSocket API | Browser native | Bidirectional audio/JSON streaming | Native browser API. No library needed. Send binary (PCM) and text (JSON) frames. |
| MediaDevices.getUserMedia() | Browser native | Microphone access permission | Standard API, works on all modern browsers including mobile Safari/Chrome. |
| AudioContext | Browser native | PCM playback at 24kHz | Decode Gemini's 24kHz PCM output and queue for playback via AudioBufferSourceNode. |

**Confidence:** HIGH. All browser-native APIs, no dependencies.

**Browser audio pipeline:**
```
Mic -> getUserMedia() -> AudioContext(16kHz) -> AudioWorkletProcessor
  -> Float32 to Int16 PCM -> WebSocket binary frame -> gateway -> Gemini

Gemini -> gateway -> WebSocket binary frame -> Int16 PCM to Float32
  -> AudioContext(24kHz) -> AudioBufferSourceNode -> speakers
```

**AudioWorklet processor (inline in voice.html):**
```javascript
// Runs in audio thread, captures 128 samples at a time
class PCMProcessor extends AudioWorkletProcessor {
    process(inputs) {
        const input = inputs[0][0]; // mono channel
        if (input) {
            // Convert Float32 [-1,1] to Int16 [-32768,32767]
            const pcm16 = new Int16Array(input.length);
            for (let i = 0; i < input.length; i++) {
                pcm16[i] = Math.max(-32768, Math.min(32767, input[i] * 32768));
            }
            this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
        }
        return true;
    }
}
registerProcessor('pcm-processor', PCMProcessor);
```

**Critical constraint for single-file HTML:** The AudioWorklet processor normally loads from a separate `.js` file via `audioWorklet.addModule('processor.js')`. For single-file HTML, use a Blob URL:
```javascript
const processorCode = `class PCMProcessor extends AudioWorkletProcessor { ... }`;
const blob = new Blob([processorCode], { type: 'application/javascript' });
const url = URL.createObjectURL(blob);
await audioCtx.audioWorklet.addModule(url);
```

### Gemini Live API Protocol Messages

**Setup message (gateway -> Gemini, first message after WS connect):**
```json
{
    "setup": {
        "model": "models/gemini-3.1-flash-live-preview",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": "Aoede"
                    }
                }
            }
        },
        "systemInstruction": {
            "parts": [{"text": "You are a helpful AI assistant..."}]
        },
        "tools": [{
            "functionDeclarations": [{
                "name": "search_gmail",
                "description": "Search user's Gmail",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING", "description": "Search query"}
                    },
                    "required": ["query"]
                }
            }]
        }],
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "disabled": false
            }
        },
        "sessionResumptionConfig": {
            "handle": null
        },
        "inputAudioTranscription": {},
        "outputAudioTranscription": {}
    }
}
```

**Audio input (gateway -> Gemini, continuous):**
```json
{
    "realtimeInput": {
        "mediaChunks": [{
            "mimeType": "audio/pcm;rate=16000",
            "data": "<base64-encoded-pcm-bytes>"
        }]
    }
}
```

**Tool call (Gemini -> gateway):**
```json
{
    "toolCall": {
        "functionCalls": [{
            "id": "call_123",
            "name": "search_gmail",
            "args": {"query": "meeting tomorrow"}
        }]
    }
}
```

**Tool response (gateway -> Gemini):**
```json
{
    "toolResponse": {
        "functionResponses": [{
            "id": "call_123",
            "response": {"results": [{"subject": "Team standup", "date": "2026-03-28"}]}
        }]
    }
}
```

## Session Management Strategy

| Concern | Solution | Confidence |
|---------|----------|------------|
| 10-min connection timeout | Enable `sessionResumptionConfig`. Save latest handle. Auto-reconnect when `goAway` received. | HIGH |
| 15-min audio session limit | Enable `contextWindowCompression` with sliding window. Triggers at 80% of 131K context. | HIGH |
| Browser disconnect/refresh | Store resumption handle in sessionStorage. Reconnect with handle on page load. | MEDIUM |
| Concurrent sessions | One voice session per user. Gateway tracks active voice WebSocket per auth token. | HIGH |

**Context window compression config:**
```json
{
    "contextWindowCompression": {
        "slidingWindow": {},
        "triggerTokens": 104858,
        "targetTokens": 65536
    }
}
```

## Tool Calling Architecture

The gateway proxies tool calls between Gemini and goosed. When Gemini sends a `toolCall`, the gateway:

1. Receives `toolCall` JSON from Gemini WebSocket
2. Executes the tool via goosed REST API (existing `_do_rest_relay` pattern)
3. Sends `toolResponse` JSON back to Gemini WebSocket
4. Gemini resumes audio generation with tool results

**Tools to expose to Gemini via function declarations:**
- `memory_search` - search mem0 memories (via goosed MCP)
- `search_gmail` - Gmail search (via goosed MCP, if configured)
- `search_calendar` - Calendar lookup (via goosed MCP, if configured)
- `web_search` - Web search (via goosed MCP)
- `knowledge_search` - ChromaDB doc search (via goosed MCP)

**Gateway is the tool executor, not the browser.** Browser just streams audio. All tool logic stays server-side.

## Alternatives Considered

| Recommended | Alternative | Why Not |
|-------------|-------------|---------|
| stdlib WebSocket (RFC 6455) | `websockets` pip package | gateway.py is stdlib-only. websockets is 16K lines of well-tested code, but violates the constraint. RFC 6455 for this use case is ~150 lines of simple frame parsing. |
| stdlib WebSocket (RFC 6455) | `asyncio` + `websockets` | Would require rewriting gateway.py from ThreadingHTTPServer to asyncio. Massive refactor for one feature. Thread-per-connection works fine for single-user voice. |
| Gemini 3.1 Flash Live | OpenAI Realtime API | Requires separate STT + TTS. More expensive. Doesn't do audio-to-audio natively. GooseClaw already supports 23+ providers, Gemini is just one more. |
| Gemini 3.1 Flash Live | Gemini 2.5 Flash Live | 2.5 is GA and more stable, but 3.1 has better latency for voice. Use 2.5 as fallback if 3.1 preview is buggy. |
| Ephemeral tokens | Direct API key in browser | Security risk. API key visible in browser DevTools. Ephemeral tokens expire in minutes. |
| Ephemeral tokens | Gateway proxies ALL audio | Higher latency (extra hop). Higher bandwidth on gateway. Ephemeral tokens let browser connect directly to Gemini, gateway just issues the token. |
| Gateway proxy (chosen) | Browser direct to Gemini | Tool calling requires server-side execution. Gateway must intercept tool calls. So gateway MUST proxy, not just issue tokens. |
| AudioWorklet | ScriptProcessorNode | ScriptProcessorNode is deprecated. Runs on main thread, causes glitches. AudioWorklet runs in audio thread. |
| Blob URL for AudioWorklet | Separate processor.js file | Single-file HTML constraint. Blob URL works in all modern browsers. |
| PCM binary WebSocket frames | Base64 JSON WebSocket frames | 33% less bandwidth with binary. But Gemini protocol uses JSON with base64 audio. Gateway-to-browser CAN use binary for efficiency. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `websockets` pip package | Violates stdlib-only constraint for gateway.py | Inline RFC 6455 implementation (~150 lines) |
| `asyncio` event loop in gateway | Would require rewriting entire gateway from threading to async. Massive scope creep. | Thread-per-connection with blocking socket I/O |
| ScriptProcessorNode | Deprecated browser API, runs on main thread, audio glitches | AudioWorklet (modern, separate audio thread) |
| MediaRecorder API | Outputs compressed formats (webm/opus), not raw PCM. Would need server-side decoding. | getUserMedia + AudioWorklet for raw PCM |
| Opus/WebM encoding | Extra complexity. Gemini accepts raw PCM. Transcoding wastes CPU. | Raw PCM 16kHz 16-bit (Gemini native format) |
| Socket.IO / SignalR | Over-engineered for single-user voice. Adds npm/pip dependencies. | Native WebSocket API (browser) + stdlib WebSocket (Python) |
| Gemini 2.0 Flash | Retiring June 2026. Don't build on a deprecated model. | Gemini 3.1 Flash Live (or 2.5 as fallback) |
| WebRTC | Designed for peer-to-peer. Overkill for client-server audio streaming. STUN/TURN complexity. | WebSocket + raw PCM |
| Flask/FastAPI/aiohttp | Adding a web framework to gateway.py violates stdlib-only. gateway.py IS the web server. | Extend existing GatewayHandler |
| npm build tooling for voice.html | Violates single-file constraint. setup.html works without it. | Inline everything in voice.html |

## Stack Patterns by Variant

**If Gemini 3.1 Flash Live is stable (happy path):**
- Model: `gemini-3.1-flash-live-preview`
- Lower latency, audio-to-audio native
- Sequential function calling (audio pauses during tool execution)

**If Gemini 3.1 Flash Live is unstable (fallback):**
- Model: `gemini-2.5-flash-live-001`
- GA, more stable
- Supports async function calling (`behavior: NON_BLOCKING`)
- Audio doesn't pause during tool execution (better UX but more complex state management)

**If ephemeral token REST endpoint doesn't work as derived:**
- Skip ephemeral tokens entirely
- Gateway proxies all audio (browser -> gateway WS -> Gemini WS)
- Slightly higher latency but simpler auth model
- API key never leaves server

**If user has no Gemini API key:**
- Voice dashboard shows "Add Gemini API key in Setup to enable voice"
- Gate on key presence (same pattern as Telegram channel gating)
- No degraded mode, voice requires Gemini specifically

## Version Compatibility

| Component | Compatible With | Notes |
|-----------|-----------------|-------|
| Python 3.10 stdlib | WebSocket RFC 6455 | hashlib.sha1, struct, socket, ssl, base64 all available |
| ThreadingHTTPServer | WebSocket upgrade | Socket hijack after HTTP 101 response. Thread stays alive for WS session. |
| Gemini 3.1 Flash Live | v1beta WebSocket endpoint | v1alpha required only for ephemeral tokens |
| AudioWorklet + Blob URL | Chrome 66+, Firefox 76+, Safari 14.1+ | All modern browsers. Mobile Safari included. |
| getUserMedia | HTTPS only | Railway provides HTTPS via TLS termination. localhost exempted for dev. |
| PCM 16kHz input | Gemini Live API | Native format, no resampling needed if AudioContext created at 16000Hz |
| PCM 24kHz output | Web Audio API | Create separate AudioContext at 24000Hz for playback, or resample in browser |

## Audio Format Reference

| Direction | Format | Sample Rate | Bit Depth | Channels | Encoding |
|-----------|--------|-------------|-----------|----------|----------|
| Browser -> Gateway | Raw PCM | 16,000 Hz | 16-bit signed | Mono | Binary WebSocket frame |
| Gateway -> Gemini | Raw PCM | 16,000 Hz | 16-bit signed | Mono | Base64 in JSON (`audio/pcm;rate=16000`) |
| Gemini -> Gateway | Raw PCM | 24,000 Hz | 16-bit signed | Mono | Base64 in JSON |
| Gateway -> Browser | Raw PCM | 24,000 Hz | 16-bit signed | Mono | Binary WebSocket frame |

**Why different formats gateway<->browser vs gateway<->Gemini:** Gemini protocol requires JSON with base64 audio. Browser WebSocket can use binary frames (more efficient, 33% less bandwidth than base64). Gateway transcodes between binary PCM (browser) and base64 JSON (Gemini).

## Infrastructure Changes

| Change | Type | Cost | When |
|--------|------|------|------|
| Gemini API key in vault | Setup wizard addition | Free (API key) | Phase 1 |
| voice.html file | New file in docker/ | Free | Phase 1 |
| WebSocket handler in gateway.py | Code addition (~300 lines) | Free | Phase 1 |
| No new Railway services | N/A | $0 | N/A |

**Total new infrastructure cost:** $0. Gemini API usage is pay-per-token (priced at ~$0.15/M input tokens for Flash models). Voice sessions use roughly 1-5M tokens/hour depending on conversation density.

## Gateway.py Integration Points

| Integration Point | Existing Pattern | Voice Addition |
|-------------------|-----------------|----------------|
| Route matching | `do_GET` path matching in GatewayHandler | Add `/voice` (serve HTML) and `/ws/voice` (WebSocket upgrade) |
| Auth check | `check_auth(self)` before serving pages | Same auth check before WebSocket upgrade |
| HTML serving | `handle_setup_page()` reads file, sends with CSP headers | `handle_voice_page()` same pattern, different CSP (needs `connect-src wss:`) |
| Outbound HTTP | `urllib.request.Request` for API calls | Same pattern for ephemeral token creation |
| Outbound WebSocket | None (new) | New: raw socket + SSL + RFC 6455 client |
| Inbound WebSocket | None (new) | New: HTTP upgrade in do_GET, socket hijack |
| Tool execution | `_do_rest_relay()` for goosed sessions | Same function, called when Gemini sends toolCall |
| Logging | `logging.getLogger("component")` | Add `_voice_log = logging.getLogger("voice")` |
| Rate limiting | `RateLimiter` class, per-IP | Add voice-specific limiter (1 concurrent session per user) |

## Sources

- [Gemini Live API Overview](https://ai.google.dev/gemini-api/docs/live-api) -- capabilities, audio format, session limits (HIGH confidence)
- [Gemini Live API WebSocket Reference](https://ai.google.dev/api/live) -- message types, JSON schema, setup config (HIGH confidence)
- [Gemini Live API Getting Started (WebSocket)](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket) -- endpoint URL, auth, code examples (HIGH confidence)
- [Gemini 3.1 Flash Live Preview Model Card](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview) -- model ID, context limits, supported features (HIGH confidence)
- [Gemini Ephemeral Tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens) -- token creation, lifetime defaults, v1alpha requirement (MEDIUM confidence, REST endpoint derived from SDK)
- [Gemini Live API Session Management](https://ai.google.dev/gemini-api/docs/live-session) -- resumption, compression, goAway handling (HIGH confidence)
- [Gemini Live API Capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) -- tool calling behavior, audio format details (HIGH confidence)
- [Firebase Live API Limits](https://firebase.google.com/docs/ai-logic/live-api/limits-and-specs) -- rate limits, audio specs, MIME types (HIGH confidence)
- [MDN: Writing WebSocket Servers](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API/Writing_WebSocket_servers) -- RFC 6455 handshake, frame format, masking (HIGH confidence)
- [web.dev: Microphone Processing](https://web.dev/patterns/media/microphone-process) -- getUserMedia, AudioWorklet pattern (HIGH confidence)
- [python-genai SDK tokens.py](https://github.com/googleapis/python-genai/blob/main/google/genai/tokens.py) -- REST endpoint path `POST /auth_tokens` (MEDIUM confidence, derived from source)
- [Gemini Live API Examples (GitHub)](https://github.com/google-gemini/gemini-live-api-examples) -- reference implementations (MEDIUM confidence)
- [Gemini 3.1 Flash Live Announcement (MarkTechPost)](https://www.marktechpost.com/2026/03/26/google-releases-gemini-3-1-flash-live/) -- release confirmation (MEDIUM confidence)

---
*Stack research for: GooseClaw v6.0 Voice Dashboard*
*Researched: 2026-03-27*

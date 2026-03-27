# Phase 30: Voice Dashboard - Research

**Researched:** 2026-03-27
**Domain:** Browser voice UI (Web Audio API, WebSocket, Canvas), gateway.py modifications (CSP, AudioWorklet support)
**Confidence:** HIGH

## Summary

Phase 30 builds the browser-side voice dashboard (voice.html) that connects to the WebSocket proxy and Gemini relay infrastructure already implemented in Phases 27-29. The gateway.py already has: RFC 6455 frame parser, outbound Gemini WebSocket client, bidirectional relay threads, session tokens, GoAway reconnection, ping/pong keepalive, voice page serving with Gemini key gating, and CSP headers. What remains is the actual voice.html file with audio capture, playback, transcript display, visualizer, and connection state management.

This is a pure browser-side implementation phase. The file must be a self-contained single HTML file (matching setup.html pattern at ~5500 lines). No build tooling, no npm, no React. All JavaScript and CSS inline. The main challenges are: AudioWorklet via Blob URL for PCM capture (requires CSP fix), streaming PCM playback at 24kHz, reactive visualizer via AnalyserNode + Canvas, and robust state machine for connection lifecycle.

**Primary recommendation:** Build voice.html in layers: WebSocket connection + state machine first, then audio capture (AudioWorklet), then audio playback (AudioContext queue), then transcript, then visualizer. Fix the gateway CSP to add `worker-src blob:` for AudioWorklet support.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| VOICE-01 | User can open voice dashboard in any modern browser on phone or desktop | voice.html served at /voice, already gated by Phase 29. Responsive layout with mobile-first CSS. |
| VOICE-03 | User can tap push-to-talk button, audio streams in real-time to Gemini | AudioWorklet captures PCM 16kHz, sends as binary WS frames. Gateway already converts binary PCM to Gemini JSON format. |
| VOICE-04 | User hears AI responses as streaming audio (not buffered) | AudioContext playback queue with look-ahead scheduling. Gateway sends PCM 24kHz as binary frames. |
| VOICE-05 | User can interrupt AI mid-sentence (barge-in), AI stops and listens | Gateway already sends `{type: "interrupted"}` message. Browser must clear playback queue immediately on receipt. |
| VOICE-06 | VAD automatically detects when user stops speaking | Gemini built-in VAD already enabled in config (`automaticActivityDetection`). No browser-side work needed. |
| VOICE-07 | Live transcript of both user speech and AI responses as scrolling chat | Gateway already sends `{type: "transcript", speaker: "user"/"ai", text: "..."}`. Browser renders as chat bubbles. |
| VOICE-08 | Clear connection state indicators (disconnected, connecting, listening, thinking, speaking) | State machine in JS. Map WebSocket events + Gemini protocol messages to visual states. |
| VOICE-09 | Clear error messages for mic denied, WebSocket drop, API errors, quota exceeded | Error handling for getUserMedia NotAllowedError, WebSocket onerror/onclose, and gateway error messages. |
| UI-01 | Voice dashboard is a single self-contained HTML file with no build tooling | Single voice.html file with inline CSS/JS. AudioWorklet via Blob URL. No external dependencies. |
| UI-02 | Voice visualizer responds to audio input/output volume in real-time | AnalyserNode + Canvas 2D. Orb/waveform animation driven by getByteTimeDomainData/getByteFrequencyData. |
</phase_requirements>

## Standard Stack

### Core (All Browser-Native, No Libraries)

| API | Spec | Purpose | Why Standard |
|-----|------|---------|--------------|
| Web Audio API | W3C | Audio capture, playback, visualization | Only way to handle raw PCM in browser. AudioWorklet for off-main-thread capture. |
| AudioWorklet | W3C | PCM capture at 16kHz without main thread jank | Replaces deprecated ScriptProcessorNode. Runs in separate thread. |
| AnalyserNode | W3C | Real-time frequency/time domain data for visualizer | Built into Web Audio API. No external library needed. |
| Canvas 2D | W3C | Visualizer rendering | Lightweight, no WebGL complexity. Sufficient for orb/waveform. |
| WebSocket API | W3C | Bidirectional communication with gateway | Native browser API. Gateway already handles /ws/voice. |
| MediaDevices.getUserMedia | W3C | Microphone access | Standard mic permission API. Must call inside user gesture. |

### Supporting

| API | Purpose | When to Use |
|-----|---------|-------------|
| URL.createObjectURL | Create Blob URL for AudioWorklet processor code | Required for single-file constraint (no external .js files) |
| requestAnimationFrame | Drive visualizer animation loop | Standard for smooth 60fps canvas rendering |
| CSS custom properties | Theme consistency, state-driven styling | Responsive design, dark theme matching GooseClaw |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| AudioWorklet via Blob URL | ScriptProcessorNode | Deprecated, runs on main thread, causes audio glitches. Only use as fallback for very old browsers. |
| Canvas 2D visualizer | CSS animations only | CSS can't do waveform visualization. Canvas provides pixel-level control. |
| Raw WebSocket API | Socket.io or similar | Adds dependency. Single-file constraint prohibits external libs. Native WS API is sufficient. |

**Installation:** None. All browser-native APIs. No npm, no CDN, no external files.

## Architecture Patterns

### Existing Infrastructure (Phases 27-29, DO NOT REBUILD)

The gateway.py already has all server-side voice infrastructure:

```
gateway.py (lines 8723-9900+):
  - ws_recv_frame / ws_send_frame     -- RFC 6455 frame parser
  - ws_client_connect                  -- outbound TLS WebSocket to Gemini
  - _gemini_connect / _gemini_build_config -- Gemini Live API setup
  - _voice_relay_browser_to_gemini     -- browser binary PCM -> Gemini JSON
  - _voice_relay_gemini_to_browser     -- Gemini JSON -> browser binary PCM + transcripts
  - _voice_parse_server_message        -- classifies: ready, transcript, interrupted, goaway, tool_call
  - _voice_handle_goaway               -- reconnects with resumption handle
  - ws_start_ping_loop                 -- 25s keepalive for Railway
  - handle_voice_ws                    -- WebSocket upgrade + auth + relay orchestration
  - handle_voice_token                 -- GET /api/voice/token mints session tokens
  - handle_voice_page                  -- serves voice.html, gates on Gemini key
  - _VOICE_GATE_HTML                   -- shown when no Gemini key
```

### Gateway Protocol (Browser <-> Gateway Messages)

**Browser sends to gateway:**
- Binary frames: raw PCM 16-bit LE 16kHz audio chunks
- Text frames: JSON control messages (future: text input)

**Gateway sends to browser:**
- Binary frames: raw PCM 16-bit LE 24kHz audio from Gemini
- Text frames: JSON protocol messages:

```javascript
// Connection ready (after Gemini setup complete)
{"type": "ready"}

// Transcription
{"type": "transcript", "speaker": "user", "text": "what's the weather"}
{"type": "transcript", "speaker": "ai", "text": "Let me check that for you"}

// Interruption (user spoke while AI was speaking)
{"type": "interrupted"}

// Tool calls (future Phase 32, but structure known)
{"type": "tool_call", "data": {...}}
{"type": "tool_cancelled", "ids": [...]}
```

### voice.html Architecture

```
voice.html (single file, ~2500-3500 lines estimated)
|
+-- <style>                    # All CSS inline
|   +-- Mobile-first responsive layout
|   +-- State-driven mic button styles
|   +-- Transcript chat bubbles
|   +-- Visualizer canvas container
|
+-- <body>
|   +-- #visualizer-canvas     # Canvas for audio visualization
|   +-- #transcript            # Scrolling chat transcript
|   +-- #status-bar            # Connection state + errors
|   +-- #mic-btn               # Push-to-talk button (primary interaction)
|   +-- #error-banner          # Dismissible error messages
|
+-- <script>
    +-- State Machine          # DISCONNECTED -> CONNECTING -> READY -> LISTENING -> SPEAKING
    +-- AudioCapture class     # AudioWorklet via Blob URL, PCM 16kHz capture
    +-- AudioPlayback class    # Queue-based PCM 24kHz streaming playback
    +-- Visualizer class       # AnalyserNode + Canvas 2D rendering
    +-- TranscriptManager      # Append/update chat messages
    +-- WebSocketManager       # Connect, reconnect, message routing
    +-- ErrorHandler           # Categorize and display errors
```

### Pattern 1: AudioWorklet via Blob URL (Single-File Constraint)

**What:** Define the AudioWorkletProcessor code as a JS string, create a Blob URL, and load it via `audioContext.audioWorklet.addModule(blobUrl)`.
**When to use:** When you can't serve a separate .js file (single HTML file constraint).
**CSP requirement:** `worker-src blob:` must be in the Content-Security-Policy header.

```javascript
// Source: MDN Web Audio API / google-gemini/live-api-web-console pattern
const WORKLET_CODE = `
class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(2048);
    this.writeIndex = 0;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const samples = input[0]; // Float32Array, 128 samples per call
    for (let i = 0; i < samples.length; i++) {
      // Float32 [-1, 1] -> Int16 [-32768, 32767]
      const s = Math.max(-1, Math.min(1, samples[i]));
      this.buffer[this.writeIndex++] = s * 0x7FFF;
      if (this.writeIndex >= this.buffer.length) {
        // Send 2048 samples = 128ms at 16kHz
        this.port.postMessage(this.buffer.buffer.slice(0));
        this.writeIndex = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PCMCaptureProcessor);
`;

const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
const workletUrl = URL.createObjectURL(blob);
await audioContext.audioWorklet.addModule(workletUrl);
const captureNode = new AudioWorkletNode(audioContext, 'pcm-capture');
captureNode.port.onmessage = (e) => {
  // e.data is ArrayBuffer of Int16 PCM, send as binary WS frame
  ws.send(new Uint8Array(e.data));
};
```

### Pattern 2: PCM Playback Queue with Look-Ahead Scheduling

**What:** Queue incoming PCM chunks, convert Int16 to Float32, schedule AudioBuffers ahead of current playback position.
**When to use:** For gapless streaming audio playback from WebSocket chunks.

```javascript
// Source: google-gemini/live-api-web-console AudioStreamer pattern
class AudioPlayback {
  constructor(ctx) {
    this.ctx = ctx;  // AudioContext at sampleRate: 24000
    this.queue = [];
    this.scheduledTime = 0;
    this.playing = false;
    this.SCHEDULE_AHEAD = 0.2; // seconds
    this.INITIAL_BUFFER = 0.1; // seconds before first play
  }

  addPCM16(chunk) {
    // chunk is Uint8Array of Int16 LE PCM at 24kHz
    const float32 = new Float32Array(chunk.length / 2);
    const view = new DataView(chunk.buffer, chunk.byteOffset, chunk.byteLength);
    for (let i = 0; i < float32.length; i++) {
      float32[i] = view.getInt16(i * 2, true) / 32768;
    }
    this.queue.push(float32);
    if (!this.playing) {
      this.playing = true;
      this.scheduledTime = this.ctx.currentTime + this.INITIAL_BUFFER;
      this._scheduleNext();
    }
  }

  _scheduleNext() {
    while (this.queue.length > 0 &&
           this.scheduledTime < this.ctx.currentTime + this.SCHEDULE_AHEAD) {
      const samples = this.queue.shift();
      const buffer = this.ctx.createBuffer(1, samples.length, 24000);
      buffer.getChannelData(0).set(samples);
      const source = this.ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(this.analyser || this.ctx.destination);
      source.start(this.scheduledTime);
      this.scheduledTime += samples.length / 24000;
    }
    if (this.queue.length > 0 || this.playing) {
      requestAnimationFrame(() => this._scheduleNext());
    }
  }

  clearQueue() {
    this.queue = [];
    this.playing = false;
    this.scheduledTime = 0;
    // Note: already-scheduled sources will play out their current buffer
    // but no new chunks will be scheduled
  }
}
```

### Pattern 3: Connection State Machine

**What:** Finite state machine driving all UI state transitions.
**When to use:** Every voice dashboard needs clear state management.

```javascript
const STATE = {
  DISCONNECTED: 'disconnected',
  CONNECTING: 'connecting',
  READY: 'ready',         // connected, waiting for user to tap mic
  LISTENING: 'listening',  // user is speaking
  THINKING: 'thinking',    // waiting for AI response
  SPEAKING: 'speaking',    // AI is speaking
};

// State transitions triggered by:
// DISCONNECTED -> CONNECTING:  user taps mic button
// CONNECTING -> READY:         receive {type: "ready"} from gateway
// READY -> LISTENING:          user taps mic button (starts capture)
// LISTENING -> THINKING:       user stops speaking (VAD triggers, no more mic input)
// THINKING -> SPEAKING:        first audio chunk received from gateway
// SPEAKING -> LISTENING:       user interrupts (barge-in), receive {type: "interrupted"}
// SPEAKING -> READY:           AI finishes speaking (turnComplete or audio queue empty)
// ANY -> DISCONNECTED:         WebSocket closes/errors
```

### Pattern 4: Visualizer with AnalyserNode

**What:** Connect AnalyserNode to audio graph, read frequency/time domain data each frame, draw on canvas.
**When to use:** For the reactive orb/waveform visualization.

```javascript
// Source: MDN Web Audio API Visualizations
const analyser = audioContext.createAnalyser();
analyser.fftSize = 256;
const dataArray = new Uint8Array(analyser.frequencyBinCount);

function drawVisualizer() {
  requestAnimationFrame(drawVisualizer);
  analyser.getByteFrequencyData(dataArray);

  // Calculate average amplitude
  let sum = 0;
  for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
  const avg = sum / dataArray.length;

  // Draw reactive orb (radius based on amplitude)
  const radius = 40 + (avg / 255) * 60;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.fillStyle = `rgba(124, 106, 239, ${0.3 + (avg / 255) * 0.7})`;
  ctx.fill();
}
```

### Anti-Patterns to Avoid

- **Using MediaRecorder for audio capture:** MediaRecorder encodes to WebM/MP4 containers. Gemini needs raw PCM. Use AudioWorklet to capture raw samples directly.
- **Creating AudioContext at page load:** Must be created inside a user gesture handler (click/tap) to satisfy browser autoplay policy. iOS Safari will silently suspend it otherwise.
- **Creating separate AudioContexts for capture and playback:** Use ONE AudioContext for both. Different sample rates can be handled by creating the capture context at 16kHz (browser handles resampling) or using a single 24kHz context and downsampling in the worklet.
- **Buffering all audio before playback:** Stream chunks as they arrive. Buffering adds perceptible latency that breaks conversational feel.
- **Polling WebSocket state:** Use WebSocket event handlers (onopen, onclose, onerror, onmessage) instead of polling readyState.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Audio capture at 16kHz | Custom resampler in JS | AudioContext({sampleRate: 16000}) | Browser handles hardware resampling. Verified works in Chrome and Safari. |
| PCM Int16 <-> Float32 conversion | Custom byte manipulation | DataView.getInt16(offset, true) and Math round to 0x7FFF | DataView handles endianness correctly. The /32768 normalization is the standard pattern. |
| Voice Activity Detection | Client-side VAD library | Gemini built-in automaticActivityDetection | Already configured in _gemini_build_config. Adding client-side VAD would fight with Gemini's. |
| WebSocket reconnection | Custom retry logic | Built-in with exponential backoff (simple setTimeout) | Gateway handles GoAway/resumption server-side. Browser just needs to reconnect with new token. |
| Audio visualization | Three.js / WebGL | AnalyserNode + Canvas 2D | Single HTML file constraint. Canvas 2D is more than sufficient for an orb/waveform. |

**Key insight:** The browser provides everything needed via native APIs. The single-file constraint means no external libraries, but that is fine because Web Audio API + Canvas 2D + WebSocket API are all the building blocks required.

## Common Pitfalls

### Pitfall 1: AudioContext Created Outside User Gesture
**What goes wrong:** AudioContext stays in 'suspended' state on iOS Safari and increasingly on Chrome mobile. No audio plays. No errors thrown.
**Why it happens:** Browser autoplay policy requires user gesture (click/tap) to create or resume AudioContext.
**How to avoid:** Create AudioContext inside the mic button's click handler. Call `audioContext.resume()` and check `audioContext.state === 'running'` before proceeding.
**Warning signs:** Works on desktop Chrome, silent on iOS Safari. `audioContext.state` is 'suspended'.

### Pitfall 2: Missing CSP worker-src blob: Directive
**What goes wrong:** `audioContext.audioWorklet.addModule(blobUrl)` fails with CSP violation. AudioWorklet cannot load.
**Why it happens:** The current CSP in gateway.py for voice.html does not include `worker-src blob:`. AudioWorklet modules loaded from blob URLs require this directive.
**How to avoid:** Add `worker-src blob:;` to the CSP header in `handle_voice_page()`. Current CSP is:
```
default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';
connect-src 'self' wss:; media-src 'self' blob:; frame-ancestors 'none'
```
Must become:
```
default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';
connect-src 'self' wss:; media-src 'self' blob:; worker-src blob:; frame-ancestors 'none'
```
**Warning signs:** Console error mentioning Content-Security-Policy and blob: URL.

### Pitfall 3: Sample Rate Mismatch Between Capture and Playback
**What goes wrong:** Audio sounds chipmunk-fast or slowed-down.
**Why it happens:** Capture is 16kHz PCM, playback is 24kHz PCM. If you use one AudioContext for both, its sample rate applies to both directions.
**How to avoid:** Two approaches:
  1. **Two AudioContexts:** Capture context at 16kHz, playback context at 24kHz. Simpler but more resource usage.
  2. **One AudioContext at default rate:** Let browser resample capture down to 16kHz in the worklet, and create playback buffers specifying 24000 as the sample rate (AudioBuffer constructor accepts explicit rate).
  Recommendation: Use approach 2. `audioContext.createBuffer(1, samples.length, 24000)` correctly plays back at 24kHz regardless of the context's sample rate.
**Warning signs:** Audio pitch is wrong. Playback is too fast or too slow.

### Pitfall 4: Playback Queue Not Cleared on Interruption
**What goes wrong:** After user interrupts (barge-in), old AI audio keeps playing from the queue while user is speaking.
**Why it happens:** Gateway sends `{type: "interrupted"}` but browser doesn't clear its playback buffer.
**How to avoid:** On receiving `interrupted` message, immediately: 1) clear the playback queue, 2) stop any scheduled AudioBufferSourceNodes, 3) transition state to LISTENING.
**Warning signs:** Overlapping audio (AI still talking after user starts speaking).

### Pitfall 5: WebSocket Binary vs Text Frame Confusion
**What goes wrong:** Browser receives text where it expects binary or vice versa, causing JSON.parse errors or garbled audio.
**Why it happens:** Gateway sends audio as binary frames (opcode 0x2) and protocol messages as text frames (opcode 0x1). Browser WebSocket API delivers these as `Blob`/`ArrayBuffer` vs `string` in the `onmessage` handler.
**How to avoid:** Set `ws.binaryType = 'arraybuffer'` on the WebSocket. Then in onmessage: `if (typeof event.data === 'string')` for JSON protocol messages, `else` for binary audio data.
**Warning signs:** "Unexpected token" JSON parse errors or audio that sounds like garbage.

### Pitfall 6: Mic Permission Denied Without Recovery Path
**What goes wrong:** User denies mic permission, gets no feedback, taps mic button again, nothing happens.
**Why it happens:** `getUserMedia` throws `NotAllowedError` once denied. Subsequent calls may also be auto-denied.
**How to avoid:** Catch `NotAllowedError` and show a clear message: "Microphone access is required. Please allow microphone access in your browser settings and reload." Include platform-specific hints (iOS: Settings > Safari > Microphone).
**Warning signs:** Mic button appears clickable but nothing happens after permission denial.

### Pitfall 7: Transcript Scroll Position Fights User
**What goes wrong:** User scrolls up to read earlier messages, new messages force scroll back to bottom.
**Why it happens:** Auto-scroll logic always scrolls to bottom on new message.
**How to avoid:** Only auto-scroll if user is already near the bottom. Check `scrollHeight - scrollTop - clientHeight < threshold` before scrolling.
**Warning signs:** Reading history is impossible during active conversation.

## Code Examples

### Audio Capture Initialization (Inside User Gesture)

```javascript
// Source: MDN getUserMedia + AudioWorklet docs, verified pattern
async function startCapture() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true }
    });

    // AudioContext MUST be created here (user gesture)
    const captureCtx = new AudioContext({ sampleRate: 16000 });
    await captureCtx.resume();

    // Load AudioWorklet via Blob URL
    const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    await captureCtx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);

    const source = captureCtx.createMediaStreamSource(stream);
    const workletNode = new AudioWorkletNode(captureCtx, 'pcm-capture');

    // Route captured PCM to WebSocket
    workletNode.port.onmessage = (e) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(new Uint8Array(e.data));
      }
    };

    source.connect(workletNode);
    workletNode.connect(captureCtx.destination); // required to keep worklet alive

    return { stream, ctx: captureCtx, workletNode };
  } catch (err) {
    if (err.name === 'NotAllowedError') {
      showError('Microphone access denied. Allow mic access in browser settings.');
    } else {
      showError(`Microphone error: ${err.message}`);
    }
    return null;
  }
}
```

### WebSocket Connection with Token Auth

```javascript
// Source: Existing gateway.py handle_voice_token + handle_voice_ws pattern
async function connectVoice() {
  setState(STATE.CONNECTING);

  // 1. Get session token from gateway
  const tokenResp = await fetch('/api/voice/token');
  if (!tokenResp.ok) {
    showError('Failed to get voice session token');
    setState(STATE.DISCONNECTED);
    return;
  }
  const { token } = await tokenResp.json();

  // 2. Open WebSocket with token
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/voice?token=${token}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    // Wait for {type: "ready"} from gateway before enabling mic
  };

  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      handleProtocolMessage(JSON.parse(event.data));
    } else {
      // Binary: PCM 24kHz audio from Gemini
      playback.addPCM16(new Uint8Array(event.data));
      if (state !== STATE.SPEAKING) setState(STATE.SPEAKING);
    }
  };

  ws.onclose = (event) => {
    setState(STATE.DISCONNECTED);
    if (event.code !== 1000) {
      showError(`Connection lost (code ${event.code}). Tap mic to reconnect.`);
    }
  };

  ws.onerror = () => {
    showError('WebSocket connection error');
  };
}

function handleProtocolMessage(msg) {
  switch (msg.type) {
    case 'ready':
      setState(STATE.READY);
      break;
    case 'transcript':
      addTranscript(msg.speaker, msg.text);
      break;
    case 'interrupted':
      playback.clearQueue();
      setState(STATE.LISTENING);
      break;
  }
}
```

### Responsive Mic Button (Mobile-First)

```css
/* Source: Mobile-first voice UI conventions */
.mic-btn {
  width: 80px; height: 80px;
  border-radius: 50%;
  border: none;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s ease;
  -webkit-tap-highlight-color: transparent; /* iOS */
  touch-action: manipulation; /* prevent double-tap zoom */
}
.mic-btn[data-state="disconnected"] { background: #333; }
.mic-btn[data-state="connecting"]   { background: #555; animation: pulse 1.5s infinite; }
.mic-btn[data-state="ready"]        { background: #7c6aef; }
.mic-btn[data-state="listening"]    { background: #ef4444; animation: pulse 0.8s infinite; }
.mic-btn[data-state="thinking"]     { background: #f59e0b; animation: pulse 1.2s infinite; }
.mic-btn[data-state="speaking"]     { background: #22c55e; }

@keyframes pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.08); }
}

@media (max-width: 768px) {
  .mic-btn { width: 100px; height: 100px; } /* larger tap target on mobile */
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| ScriptProcessorNode for audio capture | AudioWorklet | Deprecated 2022+ | AudioWorklet is off-main-thread, prevents jank. ScriptProcessorNode still works as fallback. |
| MediaRecorder for voice streaming | getUserMedia + AudioWorklet raw PCM | N/A (different use case) | MediaRecorder encodes to containers. Raw PCM avoids encode/decode roundtrip. |
| Single AudioContext for capture + playback | AudioContext.createBuffer with explicit sampleRate | Always available | createBuffer(channels, length, sampleRate) handles rate mismatch natively. |
| Polling-based WebSocket reconnect | Event-driven with exponential backoff | Best practice | WebSocket events (onclose, onerror) trigger reconnect directly. |

**Deprecated/outdated:**
- ScriptProcessorNode: Deprecated, runs on main thread. Use AudioWorklet instead. Keep as fallback only for browsers without AudioWorklet support (very rare in 2026).

## Gateway Modifications Required

### CSP Header Fix (CRITICAL)

The current CSP in `handle_voice_page()` at line ~9876 must add `worker-src blob:` for AudioWorklet:

```python
# Current (line 9876-9882):
csp = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self' wss:; "
    "media-src 'self' blob:; "
    "frame-ancestors 'none'"
)

# Required:
csp = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self' wss:; "
    "media-src 'self' blob:; "
    "worker-src blob:; "
    "frame-ancestors 'none'"
)
```

This is the ONLY gateway.py change needed for Phase 30. All WebSocket, relay, and protocol infrastructure is already complete from Phase 28.

## Open Questions

1. **AudioContext sample rate strategy**
   - What we know: Capture needs 16kHz, playback needs 24kHz. `createBuffer(1, length, 24000)` works regardless of context rate.
   - What's unclear: Whether creating AudioContext at 16kHz and using createBuffer at 24kHz causes audible artifacts on all browsers.
   - Recommendation: Use default AudioContext sample rate (usually 44.1kHz or 48kHz). Capture worklet downsamples to 16kHz. Playback uses createBuffer with explicit 24000 rate. Safest cross-browser approach.

2. **AudioWorklet Blob URL on Safari**
   - What we know: Chrome supports AudioWorklet via Blob URL. Safari's support was added later.
   - What's unclear: Safari 17+ support status for AudioWorklet.addModule with blob: URLs.
   - Recommendation: Implement AudioWorklet as primary, add ScriptProcessorNode fallback behind a feature check: `if (audioContext.audioWorklet) { ... } else { /* fallback */ }`.

3. **Transcript update frequency**
   - What we know: Gemini sends inputTranscription and outputTranscription as text appears.
   - What's unclear: Whether Gemini sends incremental (word-by-word) or complete transcripts per message.
   - Recommendation: Treat each transcript message as a complete update for that turn. Append new turns, update current turn in-place.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 8.4.2 + pytest-timeout 2.4.0 |
| Config file | `docker/pytest.ini` |
| Quick run command | `cd docker && python3 -m pytest tests/test_voice.py tests/test_websocket.py -x -q` |
| Full suite command | `cd docker && python3 -m pytest tests/ -x -q --ignore=tests/e2e` |
| Estimated runtime | ~35 seconds (ping test has 30s timeout) |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| VOICE-01 | voice.html served at /voice with Gemini key | integration | `cd docker && python3 -m pytest tests/test_voice.py::TestVoicePageGating::test_voice_with_key_serves_page -x` | YES (Phase 29) |
| VOICE-03 | Binary PCM from browser forwarded to Gemini as JSON | integration | New test: verify browser binary frame -> Gemini JSON relay | NO, Wave 0 gap |
| VOICE-04 | Gemini audio chunks forwarded as binary PCM to browser | integration | New test: verify Gemini audio -> browser binary relay | NO, Wave 0 gap |
| VOICE-05 | Interrupted message forwarded to browser | unit | `cd docker && python3 -m pytest tests/test_voice.py::TestVoiceParseServerMessage::test_interrupted -x` | YES (Phase 28) |
| VOICE-06 | VAD enabled in Gemini config | unit | `cd docker && python3 -m pytest tests/test_voice.py::TestGeminiBuildConfig -x` | YES (Phase 28) |
| VOICE-07 | Transcripts forwarded to browser | unit | `cd docker && python3 -m pytest tests/test_voice.py::TestVoiceParseServerMessage::test_output_transcription -x` | YES (Phase 28) |
| VOICE-08 | Connection state via ready message | unit | `cd docker && python3 -m pytest tests/test_voice.py::TestVoiceParseServerMessage::test_setup_complete -x` | YES (Phase 28) |
| VOICE-09 | Error handling (mic denied, WS drop) | manual-only | Browser-specific mic permission and WS error behavior cannot be unit tested | N/A |
| UI-01 | voice.html is single self-contained file | unit | New test: verify voice.html exists, is valid HTML, has no external script/link tags | NO, Wave 0 gap |
| UI-02 | Visualizer responds to audio volume | manual-only | Canvas rendering requires browser. Can verify AnalyserNode setup in browser dev console. | N/A |

### Nyquist Sampling Rate

- **Minimum sample interval:** After every committed task, run: `cd docker && python3 -m pytest tests/test_voice.py tests/test_websocket.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work`
- **Estimated feedback latency per task:** ~35 seconds

### Wave 0 Gaps (must be created before implementation)

- [ ] `docker/tests/test_voice.py::TestVoiceDashboardFile` -- covers UI-01: verify voice.html exists, is self-contained, has expected structure
- [ ] `docker/tests/test_voice.py::TestVoiceCSP` -- covers VOICE-01: verify CSP includes worker-src blob: for AudioWorklet
- [ ] `docker/tests/test_voice.py::TestVoiceRelayAudio` -- covers VOICE-03, VOICE-04: verify binary PCM relay through mock Gemini connection

Note: VOICE-09 (error handling), UI-02 (visualizer) are manual-only because they require real browser interaction (mic permissions, canvas rendering). The gateway-side tests cover protocol correctness; browser-side behavior must be validated by opening voice.html in a browser.

## Sources

### Primary (HIGH confidence)
- gateway.py source code analysis (lines 8723-9900+) -- WebSocket protocol, Gemini relay, voice page serving, CSP headers
- [MDN: Visualizations with Web Audio API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API/Visualizations_with_Web_Audio_API) -- AnalyserNode patterns, Canvas rendering
- [MDN: Using AudioWorklet](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API/Using_AudioWorklet) -- AudioWorkletProcessor, addModule, Blob URL pattern
- [MDN: Content-Security-Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy) -- worker-src directive for blob URLs
- [google-gemini/live-api-web-console](https://github.com/google-gemini/live-api-web-console) -- Reference implementation for PCM capture/playback, AudioStreamer pattern
- [DeepWiki: Audio Pipeline Analysis](https://deepwiki.com/google-gemini/live-api-web-console/2.4-audio-and-media-processing-pipeline) -- Playback scheduling, PCM16 conversion

### Secondary (MEDIUM confidence)
- [Streaming PCM via WebSocket + Web Audio API](https://medium.com/@adriendesbiaux/streaming-pcm-data-websocket-web-audio-api-part-1-2-5465e84c36ea) -- Streaming playback patterns
- [CSP worker-src blob: for AudioWorklets](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/-z9yd49rlrI) -- CSP requirements for blob-loaded worklets
- Existing test files: docker/tests/test_voice.py, docker/tests/test_websocket.py -- Phase 28-29 test patterns

### Tertiary (LOW confidence)
- Safari AudioWorklet Blob URL support -- needs runtime verification, could not find definitive current docs

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all browser-native APIs, well-documented, widely used
- Architecture: HIGH -- gateway protocol is implemented and tested, browser patterns verified against Google's reference implementation
- Pitfalls: HIGH -- CSP issue identified by code inspection, autoplay policy well-documented by MDN, sample rate handling verified against Web Audio spec
- Code examples: HIGH -- patterns verified against MDN official docs and Google's live-api-web-console

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (browser APIs are stable, Web Audio API is mature)

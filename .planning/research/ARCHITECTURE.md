# Architecture: Voice Dashboard Integration

**Domain:** Real-time voice AI dashboard for GooseClaw
**Researched:** 2026-03-27
**Confidence:** HIGH (verified against Gemini Live API docs, existing gateway.py codebase, RFC 6455)

## System Overview

### Current Architecture (Relevant Components)

```
+-------------------------------------------------------------------+
|                    Railway Container                                |
|-------------------------------------------------------------------|
|                                                                    |
|  entrypoint.sh                                                     |
|       |                                                            |
|       +-- gateway.py (ThreadingHTTPServer, stdlib only, ~10K lines)|
|       |       |                                                    |
|       |       +-- do_GET / do_POST routing                         |
|       |       |   +-- /setup -> setup.html (self-contained)        |
|       |       |   +-- /admin -> admin.html (self-contained)        |
|       |       |   +-- /login -> login page                         |
|       |       |   +-- /api/* -> JSON endpoints                     |
|       |       |   +-- /* -> proxy_to_goose() (reverse proxy)       |
|       |       |                                                    |
|       |       +-- goosed lifecycle (start, restart, health)        |
|       |       +-- channel plugins (Telegram, etc.)                 |
|       |       +-- job engine, cron, notification bus               |
|       |                                                            |
|       +-- channel plugins (/data/channels/*.py)                    |
|                                                                    |
|  /data/secrets/vault.yaml  (API keys)                              |
|  /data/config/setup.json   (provider config)                       |
+-------------------------------------------------------------------+
```

### Target Architecture (Voice Dashboard Added)

```
+-------------------------------------------------------------------+
|                    Railway Container                                |
|-------------------------------------------------------------------|
|                                                                    |
|  gateway.py                                                        |
|       |                                                            |
|       +-- do_GET routing                                           |
|       |   +-- /voice -> voice.html (NEW, self-contained)           |
|       |   +-- /api/voice/token -> mint ephemeral session (NEW)     |
|       |   +-- /api/voice/tools -> list available tools (NEW)       |
|       |   +-- /setup, /admin, /login (unchanged)                   |
|       |                                                            |
|       +-- WebSocket upgrade detection (NEW)                        |
|       |   +-- /ws/voice -> voice WebSocket handler                 |
|       |       +-- Thread-per-connection (matches ThreadingHTTP)    |
|       |       +-- Bidirectional relay:                              |
|       |           browser <-> gateway.py <-> Gemini Live API       |
|       |       +-- Tool call interception and execution             |
|       |                                                            |
|       +-- Gemini Live API client (NEW, stdlib ssl+socket)          |
|       |   +-- WebSocket client to wss://generativelanguage...      |
|       |   +-- API key from vault (never exposed to browser)        |
|       |                                                            |
|       +-- Tool executor (NEW)                                      |
|       |   +-- Dispatches Gemini function calls to goosed           |
|       |   +-- Returns results back through WebSocket               |
|       |                                                            |
|       +-- goosed lifecycle, channels, jobs (unchanged)             |
|                                                                    |
|  /data/secrets/vault.yaml  (+ GEMINI_API_KEY)                      |
|  /data/config/setup.json   (+ gemini key flag)                     |
+-------------------------------------------------------------------+
```

## Core Architectural Decision: Server-Side WebSocket Proxy

**Decision: gateway.py acts as a WebSocket proxy between browser and Gemini Live API. The browser never sees the Gemini API key.**

### Why Server-Side Proxy (Not Direct Browser-to-Gemini)

| Approach | Security | Complexity | Stdlib Compatible |
|----------|----------|------------|-------------------|
| Browser direct to Gemini (API key in JS) | BAD: key exposed | Low | N/A |
| Ephemeral token (browser direct with short-lived token) | Good | Medium | NO: requires `google-genai` SDK for token minting, REST endpoint undocumented |
| Server-side WebSocket proxy | Good: key stays server-side | Medium | YES: stdlib socket + ssl |

**Ephemeral tokens are the "right" approach per Google's docs, but the REST API for minting tokens is undocumented.** Google only provides SDK methods (`client.auth_tokens.create()`). Since gateway.py is stdlib-only (no pip), we cannot import the google-genai SDK. The underlying REST endpoint exists but is not publicly documented, making it fragile to reverse-engineer.

The server-side proxy approach:
1. Keeps the API key on the server (vault.yaml). Browser never sees it.
2. Uses stdlib `ssl` + `socket` to open a WebSocket client connection to Gemini.
3. Uses stdlib `socket` to accept a WebSocket connection from the browser.
4. Relays JSON messages bidirectionally in a dedicated thread per connection.
5. Intercepts `toolCall` messages from Gemini to execute tools server-side.

This is architecturally similar to how `proxy_to_goose()` already works (gateway.py proxies HTTP to goosed), except bidirectional and persistent.

## Component Boundaries

| Component | Responsibility | New/Modified | Communicates With |
|-----------|---------------|-------------|-------------------|
| **voice.html** | Voice dashboard UI (mic, visualizer, transcript) | NEW | gateway.py (WebSocket + REST APIs) |
| **WebSocket acceptor** | Detect WS upgrade on /ws/voice, perform handshake | NEW (in gateway.py) | Browser WebSocket |
| **WebSocket proxy loop** | Bidirectional relay: browser frames <-> Gemini frames | NEW (in gateway.py) | Browser, Gemini Live API |
| **Gemini WS client** | Open outbound WebSocket to Gemini, send setup config | NEW (in gateway.py) | Gemini Live API (wss://) |
| **Tool executor** | Intercept toolCall from Gemini, dispatch to goosed, return toolResponse | NEW (in gateway.py) | goosed (/agent/reply REST) |
| **Voice API endpoints** | /api/voice/token (session init), /api/voice/tools (tool list) | NEW (in gateway.py) | voice.html |
| **Setup wizard** | Add Gemini API key as optional provider | MODIFIED | vault.yaml |
| **Vault** | Store GEMINI_API_KEY | MODIFIED (new key) | gateway.py |
| **gateway.py routing** | Add /voice, /ws/voice, /api/voice/* routes | MODIFIED | All voice components |

## Data Flows

### Flow 1: Voice Session Establishment

```
Browser                    gateway.py                    Gemini Live API
   |                          |                              |
   |  GET /voice              |                              |
   |  (with auth cookie)      |                              |
   |------------------------->|                              |
   |  <- voice.html           |                              |
   |                          |                              |
   |  GET /api/voice/token    |                              |
   |  (check Gemini key)      |                              |
   |------------------------->|                              |
   |  <- {ready: true,        |                              |
   |      tools: [...]}       |                              |
   |                          |                              |
   |  WS Upgrade: /ws/voice   |                              |
   |  Sec-WebSocket-Key: xxx  |                              |
   |------------------------->|                              |
   |  <- 101 Switching Proto  |                              |
   |                          |                              |
   |  (WS open)               |  WS Connect to:             |
   |                          |  wss://generativelanguage..  |
   |                          |  ?key=GEMINI_API_KEY         |
   |                          |-------------------------->   |
   |                          |  <- WS open                  |
   |                          |                              |
   |                          |  Send setup config:          |
   |                          |  {config: {model: ...,       |
   |                          |   tools: [...],              |
   |                          |   responseModalities: [AUDIO]|
   |                          |   systemInstruction: ...}}   |
   |                          |-------------------------->   |
   |                          |  <- {setupComplete: ...}     |
   |                          |                              |
   |  <- {type: "ready"}      |                              |
```

### Flow 2: Audio Streaming (Ongoing)

```
Browser                    gateway.py                    Gemini Live API
   |                          |                              |
   |  mic audio chunk         |                              |
   |  (PCM 16kHz, base64)     |                              |
   |  {realtimeInput: {       |                              |
   |    audio: {data: "...",  |                              |
   |    mimeType: "audio/pcm  |                              |
   |    ;rate=16000"}}}        |                              |
   |------------------------->|                              |
   |                          |  Forward verbatim            |
   |                          |-------------------------->   |
   |                          |                              |
   |                          |  <- {serverContent: {        |
   |                          |       modelTurn: {parts: [{  |
   |                          |         inlineData: {        |
   |                          |           data: "base64...", |
   |                          |           mimeType: "audio   |
   |                          |           /pcm;rate=24000"   |
   |                          |       }}]}}}                 |
   |                          |                              |
   |  <- forward verbatim     |                              |
   |  (browser plays audio)   |                              |
   |                          |                              |
   |                          |  <- {serverContent: {        |
   |                          |       outputTranscription:   |
   |                          |       {text: "Hello!"}}}     |
   |  <- forward verbatim     |                              |
   |  (browser shows text)    |                              |
```

### Flow 3: Tool Calling (Mid-Conversation)

```
Browser                    gateway.py                    Gemini Live API
   |                          |                              |
   |                          |  <- {toolCall: {             |
   |                          |       functionCalls: [{      |
   |                          |         id: "call_123",      |
   |                          |         name: "search_email",|
   |                          |         args: {q: "meeting"} |
   |                          |       }]}}                   |
   |                          |                              |
   |  <- {type: "tool_call",  |                              |
   |      name: "search_email"|  (notify browser for UI)     |
   |      status: "executing"}|                              |
   |                          |                              |
   |                          |  Execute tool via goosed:    |
   |                          |  POST /agent/reply           |
   |                          |  session_id, tool prompt     |
   |                          |  (or direct MCP dispatch)    |
   |                          |                              |
   |                          |  tool result: {...}          |
   |                          |                              |
   |                          |  Send to Gemini:             |
   |                          |  {toolResponse: {            |
   |                          |    functionResponses: [{     |
   |                          |      id: "call_123",         |
   |                          |      name: "search_email",   |
   |                          |      response: {result: ...} |
   |                          |    }]}}                       |
   |                          |-------------------------->   |
   |                          |                              |
   |  <- {type: "tool_call",  |                              |
   |      name: "search_email"|                              |
   |      status: "complete"} |                              |
   |                          |                              |
   |                          |  <- {serverContent: ...}     |
   |                          |  (Gemini speaks the result)  |
   |  <- audio response       |                              |
```

### Flow 4: Session Cleanup

```
Browser                    gateway.py                    Gemini Live API
   |                          |                              |
   |  WS close frame          |                              |
   |------------------------->|                              |
   |                          |  WS close frame              |
   |                          |-------------------------->   |
   |                          |                              |
   |                          |  Cleanup:                    |
   |                          |  - Close Gemini socket       |
   |                          |  - Remove session from map   |
   |                          |  - Log session stats         |
   |                          |  (thread exits naturally)    |
```

## WebSocket Implementation in Python Stdlib

**This is the hardest part of the build.** Python's `http.server` module has no WebSocket support. The WebSocket protocol (RFC 6455) must be implemented from scratch using stdlib modules.

### Required stdlib Modules

| Module | Purpose |
|--------|---------|
| `socket` | Raw TCP for outbound Gemini connection |
| `ssl` | TLS wrapping for wss:// to Gemini |
| `hashlib` | SHA-1 for WebSocket handshake accept key |
| `base64` | Base64 for handshake + audio data |
| `struct` | Binary frame packing/unpacking |
| `threading` | Thread-per-connection (consistent with ThreadingHTTPServer) |
| `json` | Message serialization (Gemini protocol is JSON over WebSocket text frames) |

### WebSocket Server Handshake (Browser -> Gateway)

```python
# In GatewayHandler.do_GET, detect WebSocket upgrade:
def do_GET(self):
    if (self.headers.get("Upgrade", "").lower() == "websocket" and
        path == "/ws/voice"):
        self._handle_voice_websocket()
        return
    # ... existing routing ...

def _handle_voice_websocket(self):
    """Upgrade HTTP connection to WebSocket, then proxy to Gemini."""
    # 1. Validate auth (cookie or query param token)
    if not check_auth(self):
        self.send_error(401)
        return

    # 2. Check Gemini API key exists in vault
    gemini_key = _get_gemini_api_key()
    if not gemini_key:
        self.send_error(503, "Gemini API key not configured")
        return

    # 3. Perform WebSocket handshake
    ws_key = self.headers.get("Sec-WebSocket-Key", "")
    MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = base64.b64encode(
        hashlib.sha1((ws_key + MAGIC).encode()).digest()
    ).decode()

    self.send_response(101, "Switching Protocols")
    self.send_header("Upgrade", "websocket")
    self.send_header("Connection", "Upgrade")
    self.send_header("Sec-WebSocket-Accept", accept)
    self.end_headers()

    # 4. Connection is now WebSocket. Take over the socket.
    browser_sock = self.request  # the raw socket
    browser_sock.settimeout(None)  # no timeout for long-lived WS

    # 5. Open outbound WebSocket to Gemini
    gemini_sock = _connect_gemini_ws(gemini_key)

    # 6. Send Gemini setup config
    _ws_send_text(gemini_sock, json.dumps({
        "setup": {
            "model": "models/gemini-2.0-flash-live-001",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": "Kore"}
                    }
                }
            },
            "systemInstruction": {
                "parts": [{"text": _build_system_prompt()}]
            },
            "tools": _build_tool_declarations()
        }
    }))

    # 7. Wait for setupComplete from Gemini
    setup_resp = _ws_recv(gemini_sock)
    _ws_send_text(browser_sock, json.dumps({"type": "ready"}))

    # 8. Start bidirectional relay
    _voice_relay_loop(browser_sock, gemini_sock)
```

### WebSocket Frame Reading/Writing (Core Protocol)

```python
# ~100 lines of code for the core WebSocket frame protocol.
# This is well-understood, stable protocol code (RFC 6455).

OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

def _ws_recv_frame(sock):
    """Read one WebSocket frame. Returns (opcode, payload_bytes, fin)."""
    header = _recv_exact(sock, 2)
    fin = (header[0] >> 7) & 1
    opcode = header[0] & 0x0F
    masked = (header[1] >> 7) & 1
    length = header[1] & 0x7F

    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]

    if masked:
        mask = _recv_exact(sock, 4)
        payload = bytearray(_recv_exact(sock, length))
        for i in range(length):
            payload[i] ^= mask[i % 4]
    else:
        payload = _recv_exact(sock, length)

    return opcode, bytes(payload), fin

def _ws_send_text(sock, text, masked=False):
    """Send a text WebSocket frame."""
    payload = text.encode("utf-8")
    _ws_send_frame(sock, OPCODE_TEXT, payload, masked)

def _ws_send_frame(sock, opcode, payload, masked=False):
    """Send a WebSocket frame with proper length encoding."""
    header = bytearray()
    header.append(0x80 | opcode)  # FIN + opcode

    length = len(payload)
    if masked:
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked_payload = bytearray(payload)
        for i in range(length):
            masked_payload[i] ^= mask[i % 4]
        sock.sendall(header + masked_payload)
    else:
        if length <= 125:
            header.append(length)
        elif length <= 65535:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        sock.sendall(header + payload)
```

### Outbound WebSocket Client (Gateway -> Gemini)

```python
def _connect_gemini_ws(api_key):
    """Open a WebSocket connection to Gemini Live API using stdlib."""
    import socket as _socket

    host = "generativelanguage.googleapis.com"
    path = ("/ws/google.ai.generativelanguage.v1beta."
            "GenerativeService.BidiGenerateContent"
            f"?key={api_key}")

    # TCP + TLS
    raw = _socket.create_connection((host, 443), timeout=10)
    ctx = ssl.create_default_context()
    sock = ctx.wrap_socket(raw, server_hostname=host)

    # WebSocket upgrade handshake (client side)
    ws_key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(handshake.encode())

    # Read HTTP response (101 expected)
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)

    if b"101" not in response.split(b"\r\n")[0]:
        raise ConnectionError(f"Gemini WS handshake failed: {response[:200]}")

    return sock
```

### Bidirectional Relay Loop

**Key design: two threads per voice session. One reads browser frames, one reads Gemini frames. Shared shutdown event.**

```python
def _voice_relay_loop(browser_sock, gemini_sock):
    """Bidirectional WebSocket relay between browser and Gemini."""
    shutdown = threading.Event()

    def _browser_to_gemini():
        """Read frames from browser, forward to Gemini."""
        try:
            while not shutdown.is_set():
                opcode, payload, fin = _ws_recv_frame(browser_sock)
                if opcode == OPCODE_CLOSE:
                    shutdown.set()
                    break
                if opcode == OPCODE_PING:
                    _ws_send_frame(browser_sock, OPCODE_PONG, payload)
                    continue
                if opcode in (OPCODE_TEXT, OPCODE_BINARY):
                    # Forward to Gemini (client frames must be masked)
                    _ws_send_frame(gemini_sock, opcode, payload, masked=True)
        except Exception:
            shutdown.set()

    def _gemini_to_browser():
        """Read frames from Gemini, forward to browser (with tool interception)."""
        try:
            while not shutdown.is_set():
                opcode, payload, fin = _ws_recv_frame(gemini_sock)
                if opcode == OPCODE_CLOSE:
                    shutdown.set()
                    break
                if opcode == OPCODE_TEXT:
                    msg = json.loads(payload)
                    if "toolCall" in msg:
                        # Intercept: execute tool, send result back to Gemini
                        _handle_tool_call(msg["toolCall"], gemini_sock, browser_sock)
                    else:
                        # Forward to browser (server frames unmasked)
                        _ws_send_frame(browser_sock, opcode, payload)
                elif opcode == OPCODE_BINARY:
                    _ws_send_frame(browser_sock, opcode, payload)
        except Exception:
            shutdown.set()

    t1 = threading.Thread(target=_browser_to_gemini, daemon=True)
    t2 = threading.Thread(target=_gemini_to_browser, daemon=True)
    t1.start()
    t2.start()

    # Wait for either thread to finish (connection closed or error)
    shutdown.wait()

    # Cleanup
    try:
        _ws_send_frame(browser_sock, OPCODE_CLOSE, b"", masked=False)
    except Exception:
        pass
    try:
        _ws_send_frame(gemini_sock, OPCODE_CLOSE, b"", masked=True)
    except Exception:
        pass
    try:
        gemini_sock.close()
    except Exception:
        pass
```

## Threading Model

**gateway.py uses `ThreadingHTTPServer`, which spawns one thread per HTTP request.** This is important: the voice WebSocket connection hijacks the HTTP request thread and keeps it alive for the duration of the voice session (up to 15 minutes).

### Thread Budget

| Thread | Lifetime | Count |
|--------|----------|-------|
| HTTP request handler (normal) | ~50ms | Comes and goes |
| SSE proxy (goose web streaming) | Seconds to minutes | 0-2 concurrent |
| Voice session (main) | Up to 15 min | 1 per voice user |
| Voice browser->Gemini relay | Same as session | 1 per voice user |
| Voice Gemini->browser relay | Same as session | 1 per voice user |
| Tool execution | Seconds | 0-1 per voice session |

**Total for one voice session: 3 threads (handler + 2 relay threads).**

For a single-user personal agent, this is fine. ThreadingHTTPServer creates threads on demand. There is no thread pool exhaustion risk with one user.

### Why Threading (Not asyncio)

gateway.py is 10,700 lines of synchronous, threading-based code. Converting to asyncio would be a rewrite. The threading model works because:

1. All I/O is blocking socket reads/writes, which threads handle naturally
2. Thread-per-connection is the standard WebSocket server model (see python-websocket-server library, ~550 lines)
3. The relay loop is I/O-bound (waiting for frames), not CPU-bound
4. Python's GIL is not a bottleneck for I/O-bound work
5. Single user means 1-3 concurrent voice sessions max

## Tool Execution Architecture

### Tool Declaration to Gemini

Tools are declared in the WebSocket setup message. These map to goosed MCP tools that the user has configured.

```python
def _build_tool_declarations():
    """Build Gemini-format tool declarations from available goosed tools."""
    # Option A: Static list of known tools
    # Option B: Query goosed for available tools dynamically
    return [
        {
            "functionDeclarations": [
                {
                    "name": "search_memory",
                    "description": "Search user's long-term memory",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"}
                        },
                        "required": ["query"]
                    }
                },
                {
                    "name": "search_knowledge",
                    "description": "Search knowledge base documents",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"}
                        },
                        "required": ["query"]
                    }
                },
                # ... more tools
            ]
        }
    ]
```

### Tool Execution Flow

When Gemini sends a `toolCall`, the gateway intercepts it and executes the function. Two approaches:

**Approach A: Direct MCP Tool Dispatch (Recommended)**
- Gateway creates a goosed session
- Sends the tool call as a natural language prompt: "Call the memory_search tool with query='meeting tomorrow'"
- goosed's MCP tools execute and return the result
- Gateway extracts the result and sends `toolResponse` to Gemini

**Approach B: Direct Tool Implementation in gateway.py**
- For simple tools (memory search, knowledge search), call the underlying service directly
- Skip goosed entirely for these calls
- Faster, but duplicates tool logic

**Recommendation: Start with Approach A (goosed relay).** It reuses the existing MCP tool infrastructure. If latency is too high (goosed session creation + LLM reasoning overhead), optimize specific tools with Approach B later.

### Important: Gemini 2.0 Flash Live - Sequential Tool Calling Only

Per Google's docs: "Function calling is sequential only. The model will not start responding until you've sent the tool response."

This means:
- Gemini pauses audio generation while waiting for tool results
- The browser will experience silence during tool execution
- The browser UI should show a "thinking" or "searching" indicator
- Tool execution latency directly impacts user experience
- Keep tool implementations fast (sub-2-second target)

## Audio Pipeline Details

### Browser Side (voice.html)

```
Microphone (MediaStream API)
    |
    v
AudioWorklet / ScriptProcessorNode
    |
    v
Resample to 16kHz mono (if needed)
    |
    v
Convert to Int16 PCM
    |
    v
Base64 encode
    |
    v
WebSocket send: {realtimeInput: {audio: {data: "...", mimeType: "audio/pcm;rate=16000"}}}

---

WebSocket receive: {serverContent: {modelTurn: {parts: [{inlineData: {data: "...", mimeType: "audio/pcm;rate=24000"}}]}}}
    |
    v
Base64 decode
    |
    v
PCM Int16 at 24kHz
    |
    v
AudioContext.decodeAudioData / AudioWorklet playback
    |
    v
Speaker
```

### Audio Format Summary

| Direction | Format | Sample Rate | Encoding | Wire Format |
|-----------|--------|-------------|----------|-------------|
| Browser -> Gateway -> Gemini | Raw PCM 16-bit LE | 16 kHz | Base64 in JSON | WebSocket text frames |
| Gemini -> Gateway -> Browser | Raw PCM 16-bit LE | 24 kHz | Base64 in JSON | WebSocket text frames |

### Key Audio Considerations

1. **Sample rate mismatch**: Input is 16kHz, output is 24kHz. Browser must handle both.
2. **No transcoding needed on gateway**: Audio passes through as-is (base64 JSON). Gateway doesn't touch audio data.
3. **Chunk size**: Browser should send audio in ~100ms chunks (1600 samples at 16kHz = 3200 bytes = ~4400 base64 chars).
4. **Interruption handling**: When user speaks while Gemini is speaking, Gemini auto-interrupts. Browser must stop playback and clear audio queue on interrupt signal.

## Session Management

### Voice Session State

```python
# In-memory session tracking (gateway.py)
_voice_sessions = {}        # session_id -> {browser_sock, gemini_sock, created, thread}
_voice_sessions_lock = threading.Lock()

# Session lifecycle:
# 1. Created on WebSocket upgrade (/ws/voice)
# 2. Active during voice conversation (up to 15 min)
# 3. Destroyed on: browser disconnect, Gemini disconnect, timeout, error
```

### Session Limits

| Limit | Value | Source |
|-------|-------|--------|
| Max session duration (audio only) | 15 minutes | Gemini API limit |
| Max session duration (audio + video) | 2 minutes | Gemini API limit |
| Context window | 128k tokens (native audio) | Gemini API limit |
| Concurrent sessions per API key | Unknown, likely low | Needs testing |
| Reconnection within session | Possible via session resumption | Gemini supports `sessionResumption` handle |

### Session Timeout Strategy

```
- Browser sends periodic ping frames (every 30s)
- Gateway forwards pings as-is (Gemini handles keep-alive)
- If no frames received from browser for 60s, close session
- If Gemini sends close frame, notify browser and cleanup
- At 14 min mark, send warning to browser: {type: "session_expiring", remaining_seconds: 60}
```

## Voice Dashboard HTML (voice.html)

**Self-contained single-file HTML/CSS/JS, consistent with setup.html and admin.html pattern.**

### Page Serving

```python
VOICE_HTML = os.path.join(APP_DIR, "docker", "voice.html")

# In do_GET routing:
elif path.rstrip("/") == "/voice":
    self.handle_voice_page()

def handle_voice_page(self):
    """Serve voice dashboard. Requires auth + Gemini key."""
    if not check_auth(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()
        return
    # Gate on Gemini key presence
    if not _get_gemini_api_key():
        self.send_response(302)
        self.send_header("Location", "/setup")
        self.end_headers()
        return
    # Serve file (same pattern as handle_setup_page / handle_admin_page)
    with open(VOICE_HTML, "rb") as f:
        content = f.read()
    # ... ETag, CSP headers, etc.
```

### CSP Adjustments for Voice

```python
# voice.html needs additional CSP permissions:
csp = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' wss:; "           # WebSocket connections
    "media-src 'self' blob:; "            # Audio playback from blobs
    "worker-src 'self' blob:; "           # AudioWorklet
    "frame-ancestors 'none'"
)
```

## Integration Points with Existing Gateway

### Gemini API Key in Vault

```yaml
# /data/secrets/vault.yaml (existing format)
# New nested key for Gemini:
google:
  GEMINI_API_KEY: "AIza..."
```

Or as a flat key (consistent with how other provider keys are stored):
```yaml
GEMINI_API_KEY: "AIza..."
```

**Recommendation: Flat key.** Simpler vault read. `_get_gemini_api_key()` just reads from vault.yaml. The setup wizard already handles API key validation and storage.

### Setup Wizard Integration

The setup wizard (setup.html) already handles 23+ providers. Gemini key is added as an optional, separate entry since it serves a different purpose (voice, not text LLM):

```python
# New API endpoint
# POST /api/setup/validate with provider="gemini-voice"
# Validates key by attempting a quick Gemini API call

# Gemini key stored separately from GOOSE_PROVIDER
# It's not a goose provider, it's specifically for voice
```

### Auth Flow

Voice dashboard reuses existing auth:
- Same cookie-based session auth as admin.html
- `check_auth(self)` works identically
- WebSocket auth: pass session cookie in initial upgrade request
- Browser WebSocket API automatically sends cookies for same-origin connections

## Patterns to Follow

### Pattern 1: Self-Contained HTML Pages

**What:** voice.html follows the same pattern as setup.html and admin.html. Single file, no build step, inline CSS/JS.
**Why:** Maintains architectural consistency. No new tooling needed.
**Example:** setup.html is ~12K lines of HTML/CSS/JS serving a complex multi-step wizard with API calls.

### Pattern 2: Thread-Per-Connection WebSocket

**What:** Each voice session gets a dedicated thread (from ThreadingHTTPServer) plus 2 relay threads.
**Why:** Consistent with gateway.py's threading model. Simple to reason about. Each session is isolated.
**Caveat:** ThreadingHTTPServer may time out the initial request thread. After WebSocket upgrade, the handler method blocks until the session ends. This works because the thread is kept alive by the blocking relay loop.

### Pattern 3: Message Passthrough with Selective Interception

**What:** Most messages between browser and Gemini pass through verbatim. Gateway only intercepts `toolCall` messages.
**Why:** Minimizes gateway complexity. Audio frames don't need parsing. JSON messages are forwarded as-is except when tool execution is needed.
**Benefit:** If Gemini adds new message types, they automatically work without gateway changes.

## Anti-Patterns to Avoid

### Anti-Pattern 1: Decoding Audio on the Server

**What:** Base64-decoding audio, resampling, or transcoding on the gateway.
**Why bad:** Adds latency, CPU overhead, complexity. Audio is already in the right format (browser produces 16kHz PCM, Gemini expects 16kHz PCM).
**Instead:** Pass audio frames through verbatim. The gateway is a relay, not a media processor.

### Anti-Pattern 2: Using asyncio for WebSocket Only

**What:** Adding asyncio event loop inside the threading-based gateway for just the WebSocket feature.
**Why bad:** Mixing async and sync in the same process creates complexity. asyncio event loops don't play well inside threads. Risk of blocking the event loop from synchronous gateway code.
**Instead:** Use pure threading with blocking socket I/O. It's simpler and consistent with the rest of gateway.py.

### Anti-Pattern 3: Exposing Gemini API Key to Browser

**What:** Sending the API key to the browser (via JS variable, API endpoint, etc.) and letting the browser connect directly to Gemini.
**Why bad:** API key leaks. Anyone with browser devtools can steal the key.
**Instead:** Server-side proxy. API key never leaves the server.

### Anti-Pattern 4: Creating a New HTTP Server for WebSocket

**What:** Running a second server (e.g., on a different port) for WebSocket connections.
**Why bad:** Railway exposes one PORT. Running two servers requires port multiplexing or a second Railway service. Adds deployment complexity.
**Instead:** Handle WebSocket upgrade within the existing GatewayHandler.do_GET, same port, same server.

### Anti-Pattern 5: Persistent Gemini Connections (Connection Pooling)

**What:** Keeping Gemini WebSocket connections open between voice sessions for reuse.
**Why bad:** Gemini sessions are stateful. Each session has its own context. Reusing a session means inheriting previous conversation context. Sessions timeout after 15 min anyway.
**Instead:** One Gemini connection per voice session. Clean setup, clean teardown.

## Suggested Build Order

Dependencies flow top-down. Each phase builds on the previous one.

### Phase 1: WebSocket Protocol Layer (~200 lines)

Build the core WebSocket frame reader/writer. This is the foundation everything else needs.

- `_ws_recv_frame(sock)` - read one WebSocket frame
- `_ws_send_frame(sock, opcode, payload, masked)` - write one WebSocket frame
- `_ws_send_text(sock, text)` - convenience for text frames
- `_recv_exact(sock, n)` - read exactly n bytes
- WebSocket handshake (server side for browser, client side for Gemini)
- Unit testable in isolation

**Dependency:** None. Pure protocol code.

### Phase 2: Gemini WebSocket Client (~100 lines)

Outbound connection to Gemini Live API.

- `_connect_gemini_ws(api_key)` - TLS + WebSocket handshake
- `_send_gemini_setup(sock, config)` - send initial config
- Test with simple text prompt to verify connection works

**Dependency:** Phase 1 (frame reader/writer)

### Phase 3: Voice Route + Bidirectional Relay (~150 lines)

The proxy core.

- WebSocket upgrade detection in `do_GET`
- `_handle_voice_websocket()` - orchestrates the session
- `_voice_relay_loop()` - two-thread bidirectional relay
- Session tracking (`_voice_sessions` dict)
- Graceful close on either side disconnecting

**Dependency:** Phase 1 + Phase 2

### Phase 4: Voice Dashboard HTML (~2000 lines)

The browser UI.

- Microphone capture (MediaStream + AudioWorklet)
- PCM resampling to 16kHz
- WebSocket connection to /ws/voice
- Audio playback (PCM 24kHz from Gemini responses)
- Live transcript display
- Mic toggle button + voice visualizer
- Session timeout warnings
- Mobile-friendly layout

**Dependency:** Phase 3 (needs working WebSocket endpoint)

### Phase 5: Tool Calling (~200 lines)

Mid-conversation tool execution.

- Tool declaration builder (Gemini format)
- `_handle_tool_call()` - intercept, execute, respond
- goosed session creation for tool execution
- Browser notification of tool status
- Timeout handling for slow tools

**Dependency:** Phase 3 + Phase 4 (needs working relay + UI for testing)

### Phase 6: Setup Wizard Integration (~100 lines)

Gemini key management.

- Add Gemini API key field to setup wizard
- Key validation (test API call)
- Vault storage
- Voice dashboard gating on key presence
- Link from admin dashboard to voice page

**Dependency:** Phase 4 (needs dashboard to gate)

## Open Questions

1. **Which Gemini model?** `gemini-2.0-flash-live-001` is current, but `gemini-3.1-flash-live-preview` appears in recent docs. Need to verify which model is stable/recommended at build time.

2. **Tool execution latency:** goosed session creation + MCP tool call + LLM reasoning could take 2-5 seconds. Is this acceptable during a voice conversation? May need to pre-create a goosed session at voice session start and reuse it.

3. **Concurrent voice sessions:** What happens if the user opens /voice in two tabs? Need to decide: allow multiple sessions (each gets its own Gemini connection) or enforce single session (close previous on new connection).

4. **Session resumption:** Gemini supports resuming sessions via `sessionResumption` handles. Worth implementing to handle brief network interruptions? Or just restart the session?

5. **Ephemeral token fallback:** If Google documents the REST endpoint for ephemeral token creation in the future, switching to direct browser-to-Gemini (skipping the proxy) would reduce latency. Keep the architecture modular enough to support this later.

## Sources

- [Gemini Live API WebSocket reference](https://ai.google.dev/api/live) - HIGH confidence
- [Gemini Live API overview](https://ai.google.dev/gemini-api/docs/live-api) - HIGH confidence
- [Gemini Live API get started (WebSocket)](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket) - HIGH confidence
- [Gemini Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) - HIGH confidence
- [Gemini ephemeral tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens) - HIGH confidence
- [RFC 6455 WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455.html) - HIGH confidence
- [MDN: Writing WebSocket Servers](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API/Writing_WebSocket_servers) - HIGH confidence
- [python-websocket-server (stdlib-only reference)](https://github.com/Pithikos/python-websocket-server) - MEDIUM confidence
- [google-gemini/live-api-web-console](https://github.com/google-gemini/live-api-web-console) - MEDIUM confidence
- [google-gemini/gemini-live-api-examples](https://github.com/google-gemini/gemini-live-api-examples) - MEDIUM confidence
- GooseClaw gateway.py source code analysis - HIGH confidence

---
*Architecture research for: GooseClaw v6.0 Voice Dashboard*
*Researched: 2026-03-27*

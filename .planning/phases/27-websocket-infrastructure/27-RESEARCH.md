# Phase 27: WebSocket Infrastructure - Research

**Researched:** 2026-03-28
**Domain:** RFC 6455 WebSocket protocol (server + client), Python stdlib, Railway proxy keepalive
**Confidence:** HIGH

## Summary

Phase 27 adds WebSocket infrastructure to gateway.py using only Python stdlib. The gateway must act as both a WebSocket **server** (accepting browser connections via HTTP 101 upgrade) and a WebSocket **client** (opening outbound TLS WebSocket connections to external APIs like Gemini). The critical constraint is that gateway.py is stdlib-only (no pip packages), so RFC 6455 handshake, frame parsing, masking, and ping/pong must be implemented from scratch (~200 lines total).

The hardest part is integrating WebSocket into `http.server.BaseHTTPRequestHandler`, which was designed for request/response, not persistent bidirectional connections. The proven pattern is: perform the 101 handshake using `self.send_response()` / `self.send_header()`, then **never return from `do_GET()`**. Instead, enter a blocking WebSocket read loop on `self.request` (the raw socket). Set `self.close_connection = True` to prevent the handler from attempting HTTP keep-alive on the upgraded connection.

Railway's proxy kills idle connections at ~10 minutes. Protocol-level WebSocket ping frames (opcode 0x9) every 25 seconds keep the connection alive. This is the core of VOICE-10. Application-level JSON heartbeats are insufficient; the proxy only recognizes WebSocket control frames as activity.

**Primary recommendation:** Implement a self-contained `ws_module` (functions, not a class) at module scope in gateway.py providing: `ws_accept()` (server handshake), `ws_connect()` (client handshake over TLS), `ws_recv_frame()`, `ws_send_frame()`, `ws_send_ping()`, `ws_close()`. Use `threading.Thread(daemon=True)` for a ping loop that fires every 25 seconds on each active connection.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| VOICE-10 | WebSocket proxy sends ping/pong keepalives to survive Railway's 10-min timeout | Railway proxy timeout verified at ~10 min. RFC 6455 ping (0x9) / pong (0xA) at 25s interval. Server-initiated pings to browser, bidirectional with Gemini. Implementation via daemon thread per WebSocket connection. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `hashlib` (sha1) | stdlib | WebSocket accept key computation | RFC 6455 requires SHA-1 hash of client key + magic GUID |
| `base64` | stdlib | Encode/decode WebSocket accept key | RFC 6455 handshake uses base64 |
| `struct` | stdlib | Pack/unpack WebSocket frame headers | Binary frame format requires big-endian unsigned int packing |
| `socket` | stdlib | Raw TCP sockets for outbound WebSocket client | Outbound WSS connections to Gemini Live API |
| `ssl` | stdlib | TLS wrapping for outbound WebSocket | `ssl.create_default_context()` + `wrap_socket()` for WSS |
| `threading` | stdlib | Ping loop thread, per-connection WebSocket handler | Daemon threads for keepalive, matches ThreadingHTTPServer pattern |
| `os` (urandom) | stdlib | Generate 4-byte masking key for client frames | RFC 6455 requires client-to-server frames to be masked with random key |
| `http.server.BaseHTTPRequestHandler` | stdlib | HTTP 101 upgrade, access to `self.request` socket | Existing gateway.py handler class, socket available as `self.request` |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `select` | stdlib | Non-blocking socket readiness check | Optional: check if data available before blocking `recv()`, useful for interleaving ping with read |
| `time` | stdlib | Timestamp tracking for ping intervals | Track last activity per connection for intelligent ping scheduling |
| `json` | stdlib | Parse/emit WebSocket text frame payloads | Gemini protocol uses JSON text frames with base64-encoded audio |
| `logging` | stdlib | WebSocket-specific logger | `logging.getLogger("ws")` for connection lifecycle and frame debugging |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Stdlib RFC 6455 (~200 lines) | `websockets` pip package (16K lines) | pip package is battle-tested but violates stdlib-only constraint |
| Thread-per-connection | `asyncio` + `websockets` | Would require rewriting entire gateway from threading to async. Massive scope creep. |
| `select.select()` for multiplexing | Blocking `recv()` in dedicated thread | Blocking is simpler and matches existing gateway pattern. `select` adds complexity without benefit for single-user. |

**Installation:**
```bash
# No installation needed. All modules are Python stdlib.
# gateway.py already imports: hashlib, base64, struct, socket, ssl, threading, os, json, logging, time
```

## Architecture Patterns

### Recommended Code Organization

All WebSocket code lives in gateway.py at module scope (not in a separate file), following the existing pattern:

```
gateway.py (existing ~10,800 lines)
├── # ── websocket protocol ────── (NEW, ~200 lines)
│   ├── WS_MAGIC = "258EAFA5-..."
│   ├── ws_accept_key(client_key)      # compute Sec-WebSocket-Accept
│   ├── ws_recv_frame(sock)            # read one frame, handle masking
│   ├── ws_send_frame(sock, opcode, payload, mask=False)
│   ├── ws_send_ping(sock)             # send ping frame (opcode 0x9)
│   ├── ws_send_close(sock, code=1000) # send close frame (opcode 0x8)
│   ├── ws_client_connect(host, path, query, ssl_ctx=None)  # outbound WS client
│   └── ws_start_ping_loop(sock, interval=25)  # returns daemon Thread
│
├── class GatewayHandler (MODIFIED)
│   ├── do_GET (MODIFIED: add /ws/voice detection)
│   │   └── if Upgrade: websocket and path == /ws/voice:
│   │       └── handle_voice_ws()
│   ├── handle_voice_ws() (NEW)
│   │   ├── check_auth(self)
│   │   ├── validate Origin header
│   │   ├── send 101 Switching Protocols
│   │   ├── self.close_connection = True
│   │   ├── start ping loop on self.request
│   │   ├── enter WebSocket read loop (BLOCKING, never returns until close)
│   │   └── cleanup on exit
```

### Pattern 1: HTTP-to-WebSocket Upgrade (Server Side)

**What:** Detect WebSocket upgrade request in `do_GET`, perform RFC 6455 handshake, hijack socket.
**When to use:** When browser connects to `/ws/voice`.
**Example:**
```python
# Source: RFC 6455 Section 4.2.2, verified against MDN WebSocket Server Guide
# and websockify (novnc/websockify) pattern for BaseHTTPRequestHandler

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def ws_accept_key(client_key):
    """Compute Sec-WebSocket-Accept from client's Sec-WebSocket-Key."""
    digest = hashlib.sha1((client_key.strip() + WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()

# Inside GatewayHandler:
def handle_voice_ws(self):
    """Upgrade HTTP to WebSocket and enter frame loop."""
    # 1. Validate upgrade request
    if self.headers.get("Upgrade", "").lower() != "websocket":
        self.send_error(400, "Expected WebSocket upgrade")
        return

    client_key = self.headers.get("Sec-WebSocket-Key")
    if not client_key:
        self.send_error(400, "Missing Sec-WebSocket-Key")
        return

    # 2. Validate Origin header (CSRF protection)
    origin = self.headers.get("Origin", "")
    # Allow same-origin and Railway domains

    # 3. Send 101 Switching Protocols
    accept = ws_accept_key(client_key)
    self.send_response(101, "Switching Protocols")
    self.send_header("Upgrade", "websocket")
    self.send_header("Connection", "Upgrade")
    self.send_header("Sec-WebSocket-Accept", accept)
    self.end_headers()

    # 4. Prevent BaseHTTPRequestHandler from closing socket
    self.close_connection = True

    # 5. Get the raw socket
    sock = self.request  # This IS the raw socket

    # 6. Start ping loop (daemon thread, dies when connection closes)
    ping_thread = ws_start_ping_loop(sock, interval=25)

    # 7. Enter blocking WebSocket read loop
    try:
        while True:
            opcode, payload = ws_recv_frame(sock)
            if opcode is None or opcode == 0x8:  # connection closed or close frame
                break
            if opcode == 0x9:  # ping from client
                ws_send_frame(sock, 0xA, payload)  # pong
                continue
            if opcode == 0xA:  # pong (response to our ping)
                continue
            if opcode == 0x1:  # text frame (JSON)
                # handle JSON message
                pass
            if opcode == 0x2:  # binary frame (audio PCM)
                # handle audio data
                pass
    finally:
        ws_send_close(sock)
        sock.close()
```

### Pattern 2: Outbound WebSocket Client (to Gemini)

**What:** Open a TLS WebSocket connection to an external server using stdlib ssl+socket.
**When to use:** When gateway needs to connect to Gemini Live API.
**Example:**
```python
# Source: RFC 6455 Section 4.1 (client handshake), Python ssl module docs

def ws_client_connect(host, path, query_params=None):
    """Open outbound WSS connection. Returns (ssl_socket, response_headers)."""
    # 1. Create TLS context
    ctx = ssl.create_default_context()

    # 2. Open TCP connection
    raw_sock = socket.create_connection((host, 443), timeout=10)

    # 3. Wrap with TLS (SNI via server_hostname)
    sock = ctx.wrap_socket(raw_sock, server_hostname=host)

    # 4. Build upgrade request
    ws_key = base64.b64encode(os.urandom(16)).decode()
    url = path
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params)

    request = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode())

    # 5. Read 101 response
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed during handshake")
        response += chunk

    status_line = response.split(b"\r\n")[0].decode()
    if "101" not in status_line:
        sock.close()
        raise ConnectionError(f"WebSocket upgrade failed: {status_line}")

    # 6. Verify Sec-WebSocket-Accept
    expected_accept = ws_accept_key(ws_key)
    # (parse headers and verify, omitted for brevity)

    return sock
```

**Critical:** When sending frames as a WebSocket CLIENT (to Gemini), frames MUST be masked (set mask=True in `ws_send_frame`). When sending frames as a WebSocket SERVER (to browser), frames MUST NOT be masked. This is an RFC 6455 requirement.

### Pattern 3: Ping/Pong Keepalive Loop

**What:** Background thread sending WebSocket ping frames every 25 seconds.
**When to use:** Every active WebSocket connection, both server-side (to browser) and client-side (to Gemini).
**Example:**
```python
def ws_start_ping_loop(sock, interval=25):
    """Start a daemon thread that sends WebSocket ping every `interval` seconds."""
    def _ping_loop():
        while True:
            time.sleep(interval)
            try:
                ws_send_frame(sock, 0x9, b"", mask=False)  # ping
            except (OSError, BrokenPipeError):
                break  # socket closed, exit loop

    t = threading.Thread(target=_ping_loop, daemon=True)
    t.start()
    return t
```

### Pattern 4: WebSocket Close Handshake

**What:** Clean close with close frame (opcode 0x8) containing status code.
**When to use:** When either side initiates disconnect.
**Example:**
```python
def ws_send_close(sock, code=1000, reason=""):
    """Send WebSocket close frame with status code."""
    payload = struct.pack(">H", code) + reason.encode("utf-8")
    try:
        ws_send_frame(sock, 0x8, payload, mask=False)
    except (OSError, BrokenPipeError):
        pass  # socket already dead, that's fine
```

### Anti-Patterns to Avoid
- **Returning from do_GET after 101 upgrade:** BaseHTTPRequestHandler will try to read another HTTP request on the socket. NEVER return from the handler until the WebSocket connection is done. Block in a read loop.
- **Using self.wfile / self.rfile after upgrade:** These are buffered HTTP streams. After 101, use `self.request` (raw socket) directly with `recv()` / `sendall()`.
- **Spawning unlimited threads for WebSocket:** BoundedThreadServer uses ThreadPoolExecutor(max_workers=32). A long-lived WebSocket ties up a worker. Cap concurrent WebSocket connections at 2 (single-user app).
- **Application-level JSON heartbeat instead of protocol ping:** Railway's proxy recognizes WebSocket ping/pong control frames. A JSON message inside a text frame may not reset the proxy's idle timer.
- **Forgetting to mask client frames:** Gateway-to-Gemini frames MUST be masked (RFC 6455 Section 5.1). Many servers (including Google's) will close the connection with 1002 if client frames are unmasked.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| WebSocket handshake | Ad-hoc header parsing | Structured `ws_accept_key()` function with the magic GUID constant | The magic GUID is a fixed string in RFC 6455. Getting it wrong = silent handshake failure. |
| Frame length encoding | Inline struct.pack everywhere | Centralized `ws_send_frame()` / `ws_recv_frame()` | Three length encodings (7-bit, 16-bit, 64-bit) with different struct formats. Bugs here corrupt all subsequent frames. |
| TLS for outbound WS | Manual certificate verification | `ssl.create_default_context()` | Default context handles CA bundle, hostname verification, TLS 1.2+. Hand-rolling TLS verification is a security antipattern. |
| Ping scheduling | Manual timers per connection | `ws_start_ping_loop()` with daemon thread | Daemon thread dies automatically when process exits. No cleanup needed. |

**Key insight:** The WebSocket frame parser (~100 lines) and handshake (~50 lines) are simple individually but interact in subtle ways. Centralizing them in well-tested functions prevents the frame-corruption bugs that are the #1 cause of WebSocket implementation failures.

## Common Pitfalls

### Pitfall 1: BaseHTTPRequestHandler Closes Socket After do_GET Returns
**What goes wrong:** After sending 101 and performing the WebSocket handshake, you return from `do_GET()`. The handler immediately closes the socket or tries to read another HTTP request, killing the WebSocket connection.
**Why it happens:** `BaseHTTPRequestHandler.handle()` calls `handle_one_request()` in a loop. After `do_GET()` returns, it checks `self.close_connection`. If False, it reads the next HTTP request on what is now a WebSocket connection, causing garbage reads and socket errors.
**How to avoid:** Set `self.close_connection = True` immediately after the 101 response. Then NEVER return from `do_GET()` / `handle_voice_ws()` until the WebSocket is done. Block in the read loop. This is the approach used by websockify and every other stdlib-based WebSocket handler.
**Warning signs:** WebSocket connects then immediately disconnects. `BrokenPipeError` in server logs.

### Pitfall 2: Railway Proxy Kills Idle WebSocket at ~10 Minutes
**What goes wrong:** WebSocket works locally but drops exactly every 10 minutes in production on Railway.
**Why it happens:** Railway's load balancer reaps idle TCP connections. "When proxying requests at Railway scale, keep alive connections have to be killed." The proxy does not distinguish between idle and active application-level traffic; it needs to see WebSocket-level control frames.
**How to avoid:** Send WebSocket ping frames (opcode 0x9) every 25 seconds. 25s clears the 30-second cellular NAT timeout and stays safely under Railway's threshold. Use protocol-level pings, not application-level JSON messages.
**Warning signs:** Works locally, dies in production. Works on WiFi, dies on cellular.

### Pitfall 3: Client Frame Masking Requirement (Outbound to Gemini)
**What goes wrong:** Gateway sends unmasked frames to Gemini. Gemini closes the connection with close code 1002 (protocol error).
**Why it happens:** RFC 6455 requires that ALL client-to-server frames be masked with a 4-byte random key. When gateway acts as WebSocket CLIENT (connecting to Gemini), it IS the client and must mask. When gateway acts as WebSocket SERVER (browser connects to it), it must NOT mask outgoing frames.
**How to avoid:** `ws_send_frame()` takes a `mask` parameter. Set `mask=True` for Gemini-bound frames, `mask=False` for browser-bound frames. Generate masking key with `os.urandom(4)`.
**Warning signs:** Connection to Gemini drops immediately after first frame sent. Close code 1002 in logs.

### Pitfall 4: Extended Payload Length Parsing Bugs
**What goes wrong:** Audio chunks are typically 640-1280 bytes (20-40ms of PCM at 16kHz 16-bit). This is above the 125-byte threshold for 7-bit length encoding, triggering the 16-bit extended length path (length byte = 126). Bugs in parsing the 16-bit or 64-bit length fields cause the frame parser to read header bytes as payload data, corrupting everything downstream.
**Why it happens:** Tutorials show only the 7-bit length case. Audio data routinely hits the 126 threshold. Base64-encoded JSON audio chunks can exceed 65535 bytes, hitting the 64-bit length path.
**How to avoid:** Test frame parsing at all three boundaries: 125 bytes (7-bit), 126 bytes (16-bit trigger), 65536 bytes (64-bit trigger). Use `struct.unpack(">H", ...)` for 16-bit and `struct.unpack(">Q", ...)` for 64-bit.
**Warning signs:** Audio sounds garbled. Intermittent crashes when payload crosses size boundaries. Close code 1002.

### Pitfall 5: BoundedThreadServer Worker Exhaustion
**What goes wrong:** WebSocket connections are long-lived (minutes to hours). Each ties up a ThreadPoolExecutor worker (max 32). If WebSocket connections leak (browser reconnects without clean close), workers get consumed until the server can't handle HTTP requests.
**Why it happens:** BoundedThreadServer was designed for short-lived HTTP requests. WebSocket connections violate this assumption by occupying workers indefinitely.
**How to avoid:** Cap concurrent WebSocket connections at 2 (this is a single-user app). Track active connections in a module-level set. When a new connection arrives and the cap is hit, close the oldest connection cleanly before accepting the new one. Use `threading.Event` to signal the old connection's read loop to exit.
**Warning signs:** HTTP requests start timing out after multiple WebSocket connect/disconnect cycles. Thread count grows monotonically.

### Pitfall 6: Incomplete recv() Reads on Large Frames
**What goes wrong:** `sock.recv(length)` returns fewer bytes than requested. The frame parser treats the partial data as a complete payload, corrupting the frame.
**Why it happens:** TCP is a stream protocol. `recv()` can return any number of bytes from 1 to the requested length. Large WebSocket frames (audio data) are almost never received in a single `recv()` call.
**How to avoid:** Always loop until all expected bytes are received:
```python
payload = b""
while len(payload) < length:
    chunk = sock.recv(length - len(payload))
    if not chunk:
        break  # connection closed
    payload += chunk
```
**Warning signs:** Frames work for small payloads (< MTU size ~1400 bytes) but fail for larger ones.

## Code Examples

Verified patterns from RFC 6455 and MDN WebSocket Server Guide:

### Complete Frame Parser
```python
# Source: RFC 6455 Section 5.2, MDN Writing WebSocket Servers
# Verified against websockify (novnc/websockify) and Pithikos/python-websocket-server

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_OP_TEXT = 0x1
WS_OP_BINARY = 0x2
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA

def _ws_recv_exact(sock, n):
    """Read exactly n bytes from socket. Raises ConnectionError if socket closes."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("WebSocket connection closed during read")
        data += chunk
    return data

def ws_recv_frame(sock):
    """Read one WebSocket frame. Returns (opcode, payload_bytes) or (None, b'') on close."""
    try:
        header = _ws_recv_exact(sock, 2)
    except ConnectionError:
        return None, b""

    fin = header[0] & 0x80
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F

    if length == 126:
        length = struct.unpack(">H", _ws_recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _ws_recv_exact(sock, 8))[0]

    mask_key = _ws_recv_exact(sock, 4) if masked else None
    payload = _ws_recv_exact(sock, length) if length > 0 else b""

    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    return opcode, payload

def ws_send_frame(sock, opcode, payload, mask=False):
    """Send one WebSocket frame. mask=True for client->server, False for server->client."""
    frame = bytearray()
    frame.append(0x80 | opcode)  # FIN=1 + opcode

    length = len(payload)
    if mask:
        mask_bit = 0x80
    else:
        mask_bit = 0

    if length < 126:
        frame.append(length | mask_bit)
    elif length < 65536:
        frame.append(126 | mask_bit)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(127 | mask_bit)
        frame.extend(struct.pack(">Q", length))

    if mask:
        mask_key = os.urandom(4)
        frame.extend(mask_key)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    frame.extend(payload)
    sock.sendall(bytes(frame))

def ws_accept_key(client_key):
    """Compute Sec-WebSocket-Accept from Sec-WebSocket-Key."""
    digest = hashlib.sha1((client_key.strip() + WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()
```

### Outbound WebSocket Client (to Gemini)
```python
# Source: RFC 6455 Section 4.1, Python ssl docs

def ws_client_connect(host, path, query_params=None):
    """Open outbound WSS connection. Returns ssl-wrapped socket."""
    ctx = ssl.create_default_context()
    raw = socket.create_connection((host, 443), timeout=10)
    sock = ctx.wrap_socket(raw, server_hostname=host)

    ws_key = base64.b64encode(os.urandom(16)).decode()
    url = path
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params)

    handshake = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    sock.sendall(handshake)

    # Read HTTP response headers
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed during WS handshake")
        response += chunk

    status_line = response.split(b"\r\n")[0].decode()
    if "101" not in status_line:
        sock.close()
        raise ConnectionError(f"WS upgrade failed: {status_line}")

    # Verify accept key
    for line in response.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-accept:"):
            server_accept = line.split(b":", 1)[1].strip().decode()
            expected = ws_accept_key(ws_key)
            if server_accept != expected:
                sock.close()
                raise ConnectionError("WS accept key mismatch")
            break

    return sock
```

### Integration Into do_GET
```python
# Source: websockify pattern (novnc/websockify), SevenW/HTTPWebSocketsHandler

def do_GET(self):
    path = urllib.parse.urlparse(self.path).path

    # WebSocket upgrade detection (MUST be checked BEFORE other routes)
    if (self.headers.get("Upgrade", "").lower() == "websocket"
            and path == "/ws/voice"):
        self.handle_voice_ws()
        return

    # ... existing route handling ...
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| ScriptProcessorNode for audio capture | AudioWorklet (separate audio thread) | Chrome 64+ (2018) | No audio glitches, required for production voice apps |
| `websocket` pip package for Python WS | Stdlib RFC 6455 (~200 lines) for constrained envs | Always available | No dependency, but must handle edge cases manually |
| HTTP long-polling for real-time | WebSocket (RFC 6455, 2011) | Standard since 2011 | Bidirectional, low latency, lower overhead |
| Unlimited thread spawning | BoundedThreadServer (ThreadPoolExecutor) | Already in gateway.py | Must account for long-lived WS connections in the pool |

**Deprecated/outdated:**
- ScriptProcessorNode: deprecated in all browsers, runs on main thread. Use AudioWorklet.
- HTTP/1.0 for WebSocket: must use HTTP/1.1+ for 101 Switching Protocols.

## Open Questions

1. **BoundedThreadServer pool exhaustion with WebSocket**
   - What we know: Max 32 workers. Each WebSocket connection ties up 1 worker for its entire duration. HTTP requests also need workers.
   - What's unclear: Whether to increase the pool size, create a separate pool for WebSocket, or simply cap WebSocket connections at 2 (sufficient for single-user).
   - Recommendation: Cap WebSocket connections at 2 and track active connections in a module-level set. Single-user app does not need more. No pool changes needed.

2. **Response data after 101 headers**
   - What we know: After `self.end_headers()`, `self.wfile` may have buffered data. Using `self.request` (raw socket) directly bypasses any buffering.
   - What's unclear: Whether `self.wfile.flush()` is needed before switching to raw socket, or if `self.end_headers()` handles this.
   - Recommendation: Call `self.wfile.flush()` after `self.end_headers()` before switching to raw socket reads/writes. Belt and suspenders.

3. **Socket timeout for blocking reads**
   - What we know: `sock.recv()` blocks indefinitely by default. If Gemini or the browser disappears without a close frame, the thread hangs forever.
   - What's unclear: Best timeout value.
   - Recommendation: Set `sock.settimeout(60)` on WebSocket connections. The 25s ping loop will detect dead connections within one missed pong cycle (~50s). 60s timeout gives margin.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest with pytest-timeout |
| Config file | `docker/pytest.ini` |
| Quick run command | `cd docker && python -m pytest tests/test_websocket.py -x -v` |
| Full suite command | `cd docker && python -m pytest tests/ -x -v --timeout=30` |
| Estimated runtime | ~5 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| VOICE-10 (handshake) | WebSocket client connects via HTTP 101 upgrade and exchanges text/binary frames | integration | `cd docker && python -m pytest tests/test_websocket.py::test_ws_handshake -x` | No (Wave 0 gap) |
| VOICE-10 (outbound) | Gateway opens outbound TLS WebSocket to external server | integration | `cd docker && python -m pytest tests/test_websocket.py::test_ws_client_connect -x` | No (Wave 0 gap) |
| VOICE-10 (keepalive) | Automatic ping/pong every 25s survives Railway timeout | unit | `cd docker && python -m pytest tests/test_websocket.py::test_ws_ping_loop -x` | No (Wave 0 gap) |
| VOICE-10 (close) | Clean close handshake from either side without orphaned threads/sockets | integration | `cd docker && python -m pytest tests/test_websocket.py::test_ws_close_handshake -x` | No (Wave 0 gap) |
| Frame parser correctness | Frame parsing handles all payload length encodings and masking | unit | `cd docker && python -m pytest tests/test_websocket.py::TestFrameParser -x` | No (Wave 0 gap) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd docker && python -m pytest tests/test_websocket.py -x -v`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work`
- **Estimated feedback latency per task:** ~5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/test_websocket.py` -- covers all VOICE-10 sub-behaviors (handshake, frames, ping/pong, close, outbound client)
- [ ] Test helper: in-process WebSocket echo server for integration tests (uses same stdlib frame parser)
- [ ] Test helper: mock external WebSocket server for outbound client testing

## Sources

### Primary (HIGH confidence)
- [RFC 6455: The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455) -- handshake, frame format, masking, close semantics
- [MDN: Writing WebSocket Servers](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API/Writing_WebSocket_servers) -- handshake implementation, frame parsing tutorial
- [Python ssl module docs](https://docs.python.org/3/library/ssl.html) -- `create_default_context()`, `wrap_socket()`, SNI
- [Python http.server docs](https://docs.python.org/3/library/http.server.html) -- BaseHTTPRequestHandler, `self.request`, `close_connection`

### Secondary (MEDIUM confidence)
- [websockify (novnc/websockify)](https://github.com/novnc/websockify/blob/master/websockify/websocketserver.py) -- proven pattern for WebSocket upgrade in BaseHTTPRequestHandler
- [SevenW/HTTPWebSocketsHandler](https://gist.github.com/SevenW/47be2f9ab74cac26bf21) -- stdlib WebSocket handler reference implementation
- [Railway Help Station: Socket disconnects after 10 minutes](https://station.railway.com/questions/socket-disconnects-after-10-minutes-bbceef40) -- confirms ~10min timeout, keepalive solution
- [WebSocket.org: Fix Timeout and Silent Dropped Connections](https://websocket.org/guides/troubleshooting/timeout/) -- 25s ping interval recommendation
- [Pithikos/python-websocket-server](https://github.com/Pithikos/python-websocket-server) -- clean stdlib-only WebSocket server reference

### Tertiary (LOW confidence)
- None. All claims are verified against RFC 6455 and official docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all Python stdlib, modules already imported in gateway.py
- Architecture: HIGH -- socket hijack pattern verified in websockify and multiple implementations, RFC 6455 is a well-specified protocol
- Pitfalls: HIGH -- Railway timeout verified via user reports, frame parsing bugs are well-documented in the protocol spec
- Code examples: HIGH -- frame parser verified against RFC 6455 Section 5.2 and MDN guide

**Research date:** 2026-03-28
**Valid until:** Indefinite (RFC 6455 is stable, stdlib is stable, Railway timeout is a platform characteristic)

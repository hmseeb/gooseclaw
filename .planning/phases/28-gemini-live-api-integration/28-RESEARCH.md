# Phase 28: Gemini Live API Integration - Research

**Researched:** 2026-03-27
**Domain:** Gemini Live API WebSocket protocol, bidirectional audio relay, session management, session-scoped auth tokens
**Confidence:** HIGH

## Summary

Phase 28 replaces the echo scaffold in `handle_voice_ws()` with a working bidirectional audio pipeline to Google's Gemini Live API. The gateway acts as a WebSocket proxy: browser audio flows through gateway to Gemini via an outbound WSS connection, and Gemini's audio responses flow back. All Phase 27 WebSocket infrastructure (frame parsing, ping/pong, connection management, outbound client) is already implemented and tested.

The critical complexities in this phase are: (1) the bidirectional relay threading model (two threads per voice session: one reading from browser forwarding to Gemini, one reading from Gemini forwarding to browser), (2) session management for Gemini's hard 10-minute connection limit (GoAway message handling + session resumption), (3) context window compression to avoid the 15-minute audio ceiling, and (4) generating session-scoped tokens for WebSocket auth so the Gemini API key never reaches the browser.

The first WebSocket message to Gemini must be a `config` object (not `setup`) containing the model ID, response modalities, session resumption config, context window compression config, and transcription configs. Audio is exchanged as base64-encoded PCM in JSON text frames. The gateway transcodes between binary PCM frames (browser side) and base64 JSON (Gemini side).

**Primary recommendation:** Build the Gemini relay as a replacement for the echo loop in `handle_voice_ws()`. Use two daemon threads per session (browser-to-Gemini relay, Gemini-to-browser relay) with a shared `threading.Event` for coordinated shutdown. Implement session resumption from day one. Store the Gemini API key read from vault, generate short-lived HMAC session tokens for browser WebSocket auth, and never expose the raw key.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| VOICE-02 | Browser establishes WebSocket connection to gateway.py which proxies to Gemini Live API | Bidirectional relay architecture pattern, `ws_client_connect()` for outbound Gemini connection, `config` setup message format, JSON audio relay protocol |
| VOICE-11 | Session handles Gemini's connection limits via context window compression and session resumption | `sessionResumption` config with handle, `sessionResumptionUpdate` server message, `goAway` handling, `contextWindowCompression` with `slidingWindow`, auto-reconnect pattern |
| SETUP-03 | Gateway generates session-scoped tokens for WebSocket auth (API key never reaches browser) | HMAC-based session token generation pattern (existing `_create_auth_session` pattern), vault API key read, token passed as query param on `/ws/voice` |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `ssl` + `socket` (stdlib) | Python 3.10 | Outbound WSS to Gemini Live API | Already implemented in Phase 27 `ws_client_connect()` |
| `threading` (stdlib) | Python 3.10 | Two relay threads per voice session + ping loop | Matches existing ThreadingHTTPServer pattern |
| `json` (stdlib) | Python 3.10 | Parse/emit Gemini protocol JSON messages | Gemini protocol is JSON text frames with base64 audio |
| `base64` (stdlib) | Python 3.10 | Encode/decode PCM audio in Gemini JSON messages | Audio data is base64-encoded in JSON `data` fields |
| `secrets` + `hashlib` (stdlib) | Python 3.10 | Generate session-scoped WebSocket auth tokens | Existing pattern from `_create_auth_session()` |
| `yaml` (already available) | PyYAML | Read Gemini API key from vault.yaml | Existing vault pattern in `_inject_vault_secrets_into_env()` |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `time` (stdlib) | Python 3.10 | Track session duration, token expiry, reconnect timing | Session lifecycle management |
| `uuid` (stdlib) | Python 3.10 | Connection IDs for logging and session tracking | Already used in Phase 27 for `conn_id` |
| `logging` (stdlib) | Python 3.10 | Voice-specific logger `_voice_log` | Session lifecycle events, relay diagnostics |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Server-side proxy (chosen) | Ephemeral tokens (browser direct to Gemini) | Ephemeral token REST endpoint is undocumented (only SDK). Proxy also enables server-side tool call interception (Phase 32). |
| HMAC session tokens | JWT tokens | JWT is overkill for single-user. HMAC with secrets.token_urlsafe is simpler, no library needed. |
| Thread-per-relay | asyncio | Would require rewriting gateway from threading to async. Thread-per-connection is fine for 1-2 concurrent sessions. |

**Installation:**
```bash
# No installation needed. All modules are Python stdlib.
# PyYAML is already available in the container.
```

## Architecture Patterns

### Recommended Code Organization

```
gateway.py (extend existing ~10,800 lines)
  # ── gemini live api ────── (NEW, ~250 lines)
  +-- GEMINI_LIVE_HOST = "generativelanguage.googleapis.com"
  +-- GEMINI_LIVE_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
  +-- _gemini_connect(api_key, setup_config)    # open WS to Gemini, send config, return socket
  +-- _gemini_build_config(model, voice, ...)   # build BidiGenerateContentSetup JSON
  +-- _voice_relay_browser_to_gemini(browser_sock, gemini_sock, stop_event)
  +-- _voice_relay_gemini_to_browser(gemini_sock, browser_sock, stop_event, session_state)
  +-- _voice_handle_goaway(session_state, stop_event)  # reconnect with resumption handle
  +-- _voice_session_token_create()             # mint short-lived HMAC token
  +-- _voice_session_token_validate(token)      # check validity

  # handle_voice_ws (MODIFIED from Phase 27 echo to Gemini relay)
  +-- check auth (validate session token from query param)
  +-- read Gemini API key from vault
  +-- open outbound WS to Gemini via _gemini_connect()
  +-- start two relay threads + stop event
  +-- block until stop_event is set or browser disconnects
  +-- cleanup both sockets

  # New REST endpoint
  +-- /api/voice/token (GET, auth required) -> mint session token
```

### Pattern 1: Bidirectional WebSocket Relay

**What:** Two threads relay frames between browser and Gemini. Each thread reads from one socket and writes to the other.
**When to use:** Every voice session. This is the core of the proxy.
**Example:**
```python
# Source: Standard WebSocket proxy pattern, verified against
# dev.to/combba Go Gemini proxy and websockify relay

def _voice_relay_browser_to_gemini(browser_sock, gemini_sock, stop_event):
    """Read from browser WS, convert binary PCM to Gemini JSON, forward."""
    try:
        while not stop_event.is_set():
            opcode, payload = ws_recv_frame(browser_sock)
            if opcode is None or opcode == WS_OP_CLOSE:
                break
            if opcode == WS_OP_PING:
                ws_send_frame(browser_sock, WS_OP_PONG, payload)
                continue
            if opcode == WS_OP_PONG:
                continue
            if opcode == WS_OP_BINARY:
                # Browser sends raw PCM binary -> wrap in Gemini JSON
                audio_b64 = base64.b64encode(payload).decode()
                msg = json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "data": audio_b64,
                            "mimeType": "audio/pcm;rate=16000"
                        }
                    }
                })
                ws_send_frame(gemini_sock, WS_OP_TEXT, msg.encode(), mask=True)
            elif opcode == WS_OP_TEXT:
                # Browser sends JSON control message -> forward as-is or parse
                ws_send_frame(gemini_sock, WS_OP_TEXT, payload, mask=True)
    except (ConnectionError, OSError, socket.timeout):
        pass
    finally:
        stop_event.set()


def _voice_relay_gemini_to_browser(gemini_sock, browser_sock, stop_event, session_state):
    """Read from Gemini WS, handle protocol messages, forward audio to browser."""
    try:
        while not stop_event.is_set():
            opcode, payload = ws_recv_frame(gemini_sock)
            if opcode is None or opcode == WS_OP_CLOSE:
                break
            if opcode == WS_OP_PING:
                ws_send_frame(gemini_sock, WS_OP_PONG, payload, mask=True)
                continue
            if opcode == WS_OP_PONG:
                continue
            if opcode == WS_OP_TEXT:
                msg = json.loads(payload.decode())

                # Handle setupComplete
                if "setupComplete" in msg:
                    ws_send_frame(browser_sock, WS_OP_TEXT,
                        json.dumps({"type": "ready"}).encode())
                    continue

                # Handle sessionResumptionUpdate - save handle
                if "sessionResumptionUpdate" in msg:
                    update = msg["sessionResumptionUpdate"]
                    if update.get("newHandle"):
                        session_state["resumption_handle"] = update["newHandle"]
                    continue

                # Handle goAway - trigger reconnect
                if "goAway" in msg:
                    _voice_handle_goaway(session_state, stop_event)
                    continue

                # Handle serverContent (audio + transcripts)
                if "serverContent" in msg:
                    content = msg["serverContent"]
                    # Forward audio as binary PCM to browser
                    model_turn = content.get("modelTurn", {})
                    for part in model_turn.get("parts", []):
                        inline = part.get("inlineData", {})
                        if inline.get("data"):
                            pcm = base64.b64decode(inline["data"])
                            ws_send_frame(browser_sock, WS_OP_BINARY, pcm)
                    # Forward transcripts as JSON
                    if content.get("outputTranscription"):
                        ws_send_frame(browser_sock, WS_OP_TEXT,
                            json.dumps({
                                "type": "transcript",
                                "speaker": "ai",
                                "text": content["outputTranscription"]["text"]
                            }).encode())
                    if content.get("inputTranscription"):
                        ws_send_frame(browser_sock, WS_OP_TEXT,
                            json.dumps({
                                "type": "transcript",
                                "speaker": "user",
                                "text": content["inputTranscription"]["text"]
                            }).encode())
                    # Forward interruption signal
                    if content.get("interrupted"):
                        ws_send_frame(browser_sock, WS_OP_TEXT,
                            json.dumps({"type": "interrupted"}).encode())
                    continue

                # Handle toolCall (Phase 32 will implement execution)
                if "toolCall" in msg:
                    ws_send_frame(browser_sock, WS_OP_TEXT,
                        json.dumps({"type": "tool_call", "data": msg["toolCall"]}).encode())
                    continue

                # Handle toolCallCancellation
                if "toolCallCancellation" in msg:
                    ws_send_frame(browser_sock, WS_OP_TEXT,
                        json.dumps({"type": "tool_cancelled",
                                    "ids": msg["toolCallCancellation"]["ids"]}).encode())
                    continue

    except (ConnectionError, OSError, socket.timeout):
        pass
    finally:
        stop_event.set()
```

### Pattern 2: Gemini Connection Setup

**What:** Open outbound WSS to Gemini, send BidiGenerateContentSetup config as first message.
**When to use:** On every voice session start and on every reconnect after GoAway.
**Example:**
```python
# Source: Gemini Live API WebSocket reference
# https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket

GEMINI_LIVE_HOST = "generativelanguage.googleapis.com"
GEMINI_LIVE_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"

def _gemini_connect(api_key, resumption_handle=None):
    """Open WebSocket to Gemini Live API and send setup config. Returns socket."""
    sock = ws_client_connect(
        host=GEMINI_LIVE_HOST,
        path=GEMINI_LIVE_PATH,
        query_params={"key": api_key}
    )

    # Build setup config (first message MUST be config)
    config = {
        "config": {
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
                "parts": [{"text": "You are a helpful AI assistant."}]
            },
            "sessionResumption": {
                "handle": resumption_handle  # null for new session
            },
            "contextWindowCompression": {
                "slidingWindow": {}
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "disabled": False
                }
            }
        }
    }

    ws_send_frame(sock, WS_OP_TEXT, json.dumps(config).encode(), mask=True)
    return sock
```

### Pattern 3: GoAway Handling and Auto-Reconnect

**What:** When Gemini sends GoAway (approaching 10-minute limit), reconnect using resumption handle.
**When to use:** Automatically, transparently to the user.
**Example:**
```python
# Source: Gemini Live API session management docs
# https://ai.google.dev/gemini-api/docs/live-session

def _voice_handle_goaway(session_state, stop_event):
    """Handle GoAway by reconnecting with resumption handle."""
    handle = session_state.get("resumption_handle")
    api_key = session_state.get("api_key")

    if not handle or not api_key:
        _voice_log.warning("GoAway received but no resumption handle available")
        stop_event.set()
        return

    _voice_log.info("GoAway received, reconnecting with resumption handle")

    try:
        # Open new Gemini connection with resumption handle
        new_gemini_sock = _gemini_connect(api_key, resumption_handle=handle)

        # Start new ping loop for Gemini connection
        ws_start_ping_loop(new_gemini_sock, interval=25, mask=True)

        # Swap the Gemini socket in session state
        old_sock = session_state.get("gemini_sock")
        session_state["gemini_sock"] = new_gemini_sock

        # Close old socket
        if old_sock:
            try:
                ws_send_close(old_sock, mask=True)
                old_sock.close()
            except Exception:
                pass

        _voice_log.info("Session resumed after GoAway")
    except Exception as e:
        _voice_log.error(f"Failed to reconnect after GoAway: {e}")
        stop_event.set()
```

### Pattern 4: Session-Scoped Token for WebSocket Auth

**What:** Gateway generates short-lived tokens for browser to authenticate WebSocket connections. Gemini API key stays server-side.
**When to use:** Browser requests token via REST, then connects WebSocket with token as query param.
**Example:**
```python
# Source: Existing _create_auth_session() pattern in gateway.py

# Module-level state
_voice_tokens = {}  # token -> {"created": float, "api_key": str}
_voice_tokens_lock = threading.Lock()
_VOICE_TOKEN_TTL = 300  # 5 minutes

def _voice_session_token_create(api_key):
    """Mint a short-lived token for WebSocket auth. Returns token string."""
    token = secrets.token_urlsafe(32)
    with _voice_tokens_lock:
        # Clean expired tokens
        now = time.time()
        expired = [k for k, v in _voice_tokens.items()
                   if now - v["created"] > _VOICE_TOKEN_TTL]
        for k in expired:
            del _voice_tokens[k]
        _voice_tokens[token] = {"created": now, "api_key": api_key}
    return token

def _voice_session_token_validate(token):
    """Validate token, return api_key if valid, None if expired/invalid."""
    with _voice_tokens_lock:
        entry = _voice_tokens.get(token)
        if not entry:
            return None
        if time.time() - entry["created"] > _VOICE_TOKEN_TTL:
            del _voice_tokens[token]
            return None
        return entry["api_key"]
```

**Browser flow:**
1. `GET /api/voice/token` (with auth cookie) -> gateway reads Gemini key from vault, mints token, returns `{"token": "..."}`
2. Browser opens `ws://host/ws/voice?token=...`
3. `handle_voice_ws()` validates token, extracts API key from token store, connects to Gemini

### Anti-Patterns to Avoid
- **Forwarding raw Gemini API key to browser:** Never. The token pattern keeps the key server-side. Even the session token only indexes into the server-side store.
- **Single thread for bidirectional relay:** Blocking on `ws_recv_frame()` from one socket blocks reads from the other. Use two threads.
- **Not handling GoAway:** Sessions WILL die at ~10 minutes. GoAway handling is not optional.
- **Skipping context window compression:** Without it, sessions die at ~15 minutes of audio. Always enable `slidingWindow`.
- **Using `mediaChunks` field:** Deprecated. Use `audio` field in `realtimeInput` instead.
- **Forgetting mask=True for Gemini-bound frames:** Gateway is a WebSocket CLIENT to Gemini. RFC 6455 requires client frames to be masked. Gemini will close with 1002 if unmasked.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| WebSocket frame parsing | Custom parser | Existing `ws_recv_frame()` / `ws_send_frame()` from Phase 27 | Already tested, handles masking and all length encodings |
| Outbound WSS connection | Manual TLS+handshake | Existing `ws_client_connect()` from Phase 27 | Already handles TLS, handshake, accept key verification |
| Ping/pong keepalive | Manual timers | Existing `ws_start_ping_loop()` from Phase 27 | Daemon thread, auto-dies on socket error |
| Session token generation | Custom crypto | `secrets.token_urlsafe(32)` | Cryptographically secure, URL-safe, stdlib |
| JSON message construction | String interpolation | `json.dumps()` | Handles escaping, Unicode, nested structures |
| Base64 audio encoding | Manual byte conversion | `base64.b64encode()` / `base64.b64decode()` | Correct padding handling, stdlib |

**Key insight:** Phase 27 already built all the WebSocket primitives. Phase 28 is purely about the Gemini protocol layer and relay logic on top of those primitives.

## Common Pitfalls

### Pitfall 1: First Message Must Be Config
**What goes wrong:** Sending audio before the config message causes Gemini to close the connection with no error.
**Why it happens:** The API requires `BidiGenerateContentSetup` as the very first WebSocket message. Any other message type before setup causes silent rejection.
**How to avoid:** In `_gemini_connect()`, send config immediately after WebSocket handshake, before returning the socket. Wait for `setupComplete` response before starting audio relay.
**Warning signs:** Connection opens then immediately closes. No error message from Gemini.

### Pitfall 2: GoAway Comes With Limited Time
**What goes wrong:** GoAway includes `timeLeft` indicating how long before ABORT. If reconnect takes too long, the session data is lost.
**Why it happens:** The 10-minute connection limit is hard. GoAway is a warning, not a suggestion.
**How to avoid:** Reconnect immediately on GoAway. Pre-compute the next config message. Keep the resumption handle always up-to-date (save every `sessionResumptionUpdate`). The reconnect should take < 2 seconds.
**Warning signs:** Voice calls work for 8-10 minutes then drop. Reconnect succeeds but context is lost.

### Pitfall 3: Gemini Socket Must Use mask=True
**What goes wrong:** Gateway connects to Gemini as a WebSocket CLIENT. Per RFC 6455, client-to-server frames MUST be masked. Forgetting `mask=True` causes Gemini to close with code 1002.
**Why it happens:** Phase 27 `ws_send_frame()` defaults to `mask=False` (correct for server-to-browser). For Gemini-bound traffic, must explicitly set `mask=True`.
**How to avoid:** Wrap Gemini socket operations in a helper that always passes `mask=True`. Or create a thin wrapper: `gemini_send(sock, opcode, payload)` that always masks.
**Warning signs:** Connection to Gemini drops immediately after first frame sent. Close code 1002 in logs.

### Pitfall 4: Audio Format Mismatch Between Browser and Gemini
**What goes wrong:** Browser sends raw PCM binary frames. Gemini expects base64-encoded PCM inside JSON text frames. If the gateway forwards binary directly, Gemini can't parse it.
**Why it happens:** Different WebSocket frame types. Browser uses binary (opcode 0x2) for efficiency. Gemini protocol requires JSON text (opcode 0x1) with base64 audio.
**How to avoid:** Gateway MUST transcode: binary PCM from browser -> base64 encode -> wrap in `{"realtimeInput": {"audio": {"data": "...", "mimeType": "audio/pcm;rate=16000"}}}` -> send as text frame to Gemini. Reverse for Gemini responses: JSON text frame -> extract base64 audio -> decode to binary PCM -> send as binary frame to browser.
**Warning signs:** Gemini returns errors or silence. Browser receives JSON instead of audio data.

### Pitfall 5: Thread Cleanup on Disconnect
**What goes wrong:** When browser disconnects, the browser-to-Gemini relay thread detects it and stops, but the Gemini-to-browser thread is blocked on `ws_recv_frame(gemini_sock)` and doesn't know the browser is gone. It tries to `ws_send_frame(browser_sock, ...)` and gets `BrokenPipeError`.
**Why it happens:** Two relay threads run independently. Disconnection of one socket doesn't automatically notify the other thread.
**How to avoid:** Use a shared `threading.Event` (`stop_event`). When either thread detects disconnect, it calls `stop_event.set()`. The other thread checks `stop_event.is_set()` in its loop or catches the write error.
**Warning signs:** Orphaned threads, socket errors in logs after clean disconnect, thread count growing.

### Pitfall 6: Resumption Handle Not Saved Before GoAway
**What goes wrong:** GoAway arrives but no resumption handle was saved, so reconnect starts a fresh session losing all context.
**Why it happens:** `sessionResumptionUpdate` messages arrive periodically during the session. If they're not captured and stored, there's nothing to reconnect with.
**How to avoid:** Store every `sessionResumptionUpdate.newHandle` in session state. Check that a handle exists before attempting reconnect.
**Warning signs:** Reconnect succeeds but AI has no memory of previous conversation.

## Code Examples

### Complete handle_voice_ws Relay (replacing echo loop)
```python
# Source: Gemini Live API WebSocket docs + Phase 27 infrastructure

def handle_voice_ws(self):
    """Upgrade HTTP to WebSocket and relay audio to/from Gemini Live API."""
    # --- Phase 27 handshake (unchanged) ---
    if self.headers.get("Upgrade", "").lower() != "websocket":
        self.send_error(400, "Expected WebSocket upgrade")
        return
    client_key = self.headers.get("Sec-WebSocket-Key")
    if not client_key:
        self.send_error(400, "Missing Sec-WebSocket-Key")
        return

    # --- Phase 28: validate session token ---
    qs = urllib.parse.urlparse(self.path).query
    params = urllib.parse.parse_qs(qs)
    token = params.get("token", [None])[0]
    api_key = _voice_session_token_validate(token) if token else None
    if not api_key:
        self.send_error(403, "Invalid or expired voice session token")
        return

    # --- complete WebSocket handshake ---
    accept = ws_accept_key(client_key)
    self.send_response(101, "Switching Protocols")
    self.send_header("Upgrade", "websocket")
    self.send_header("Connection", "Upgrade")
    self.send_header("Sec-WebSocket-Accept", accept)
    self.end_headers()
    self.wfile.flush()
    self.close_connection = True

    browser_sock = self.request
    browser_sock.settimeout(60)
    conn_id = uuid.uuid4().hex[:8]

    # Start browser-side ping loop
    browser_ping = ws_start_ping_loop(browser_sock, interval=25)
    _ws_register(conn_id, browser_sock, browser_ping)

    _voice_log.info("Voice session started", extra={"event": "voice_start", "conn_id": conn_id})

    try:
        # Connect to Gemini
        gemini_sock = _gemini_connect(api_key)
        gemini_ping = ws_start_ping_loop(gemini_sock, interval=25, mask=True)

        # Wait for setupComplete
        opcode, payload = ws_recv_frame(gemini_sock)
        if opcode == WS_OP_TEXT:
            msg = json.loads(payload.decode())
            if "setupComplete" in msg:
                ws_send_frame(browser_sock, WS_OP_TEXT,
                    json.dumps({"type": "ready"}).encode())

        # Session state shared between relay threads
        session_state = {
            "resumption_handle": None,
            "api_key": api_key,
            "gemini_sock": gemini_sock,
        }
        stop_event = threading.Event()

        # Start relay threads
        b2g = threading.Thread(
            target=_voice_relay_browser_to_gemini,
            args=(browser_sock, gemini_sock, stop_event),
            daemon=True
        )
        g2b = threading.Thread(
            target=_voice_relay_gemini_to_browser,
            args=(gemini_sock, browser_sock, stop_event, session_state),
            daemon=True
        )
        b2g.start()
        g2b.start()

        # Block until either thread signals stop
        stop_event.wait()

    except Exception as e:
        _voice_log.error(f"Voice session error: {e}", extra={"conn_id": conn_id})
    finally:
        _ws_unregister(conn_id)
        for s in [browser_sock, gemini_sock]:
            try:
                ws_send_close(s)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        _voice_log.info("Voice session ended", extra={"event": "voice_end", "conn_id": conn_id})
```

### Vault Read for Gemini API Key
```python
# Source: Existing vault pattern in gateway.py

def _get_gemini_api_key():
    """Read Gemini API key from vault. Returns key string or None."""
    if not os.path.exists(VAULT_FILE):
        return None
    try:
        import yaml
        with open(VAULT_FILE) as f:
            data = yaml.safe_load(f) or {}
        return data.get("GEMINI_API_KEY")
    except Exception:
        return None
```

### Voice Token REST Endpoint
```python
# Source: Existing handle_* pattern in gateway.py

def handle_voice_token(self):
    """GET /api/voice/token - mint session-scoped token for WebSocket auth."""
    if not check_auth(self):
        return

    api_key = _get_gemini_api_key()
    if not api_key:
        self.send_json(503, {"error": "Gemini API key not configured"})
        return

    token = _voice_session_token_create(api_key)
    self.send_json(200, {"token": token})
```

### Gemini Config Message (Complete)
```json
{
    "config": {
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
            "parts": [{"text": "You are a helpful AI assistant."}]
        },
        "sessionResumption": {
            "handle": null
        },
        "contextWindowCompression": {
            "slidingWindow": {}
        },
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "disabled": false
            }
        }
    }
}
```

### Gemini Audio Input Message
```json
{
    "realtimeInput": {
        "audio": {
            "data": "<base64-encoded-pcm-16khz-16bit-mono>",
            "mimeType": "audio/pcm;rate=16000"
        }
    }
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `mediaChunks` in realtimeInput | `audio` field in realtimeInput | 2025 | `mediaChunks` is deprecated. Use `audio` field directly. |
| `LiveConnectConfig.generation_config` | Fields directly on setup config | Q3 2025 | Deprecated nesting becomes error eventually |
| No session resumption | `sessionResumption` config + handle | Gemini 2.0+ | Enables sessions longer than 10 minutes |
| No compression | `contextWindowCompression.slidingWindow` | Gemini 2.0+ | Extends sessions beyond 15-minute audio ceiling |
| Gemini 2.0 Flash Live | Gemini 3.1 Flash Live (`gemini-3.1-flash-live-preview`) | 2026-03-26 | Better latency, sequential function calling. 2.0 retires June 2026. |
| SDK-based token creation | REST API (undocumented) or server-side proxy | Current | Proxy pattern avoids undocumented REST endpoint dependency |

**Deprecated/outdated:**
- `mediaChunks`: Use `audio` field in `realtimeInput` instead
- `generation_config` nesting in `LiveConnectConfig`: Set fields directly on the config
- Gemini 2.0 Flash Live: Retires June 2026, skip entirely

## Open Questions

1. **GoAway reconnect thread coordination**
   - What we know: GoAway arrives on the Gemini-to-browser relay thread. Reconnect needs to swap the Gemini socket while the browser-to-Gemini thread may be actively writing.
   - What's unclear: Whether to stop both threads, reconnect, restart. Or swap atomically using a lock around the Gemini socket reference.
   - Recommendation: Use a lock (`threading.Lock`) around `session_state["gemini_sock"]`. The GoAway handler acquires the lock, creates a new connection, swaps the socket, releases the lock. The browser-to-Gemini thread acquires the lock for each write. Simpler than stopping/restarting threads.

2. **Fallback model behavior**
   - What we know: `gemini-3.1-flash-live-preview` is a day-old preview. May be unstable.
   - What's unclear: Whether to auto-fallback to `gemini-2.5-flash-live-001` on connection failure, or make it configurable.
   - Recommendation: Try 3.1 first. If initial connection fails with a non-retryable error, log a warning and try 2.5 as fallback. Make model configurable via setup config in future phase.

3. **Session token passed in WebSocket URL query parameter**
   - What we know: Query params are visible in server logs. The token is short-lived (5 min) and single-use is possible.
   - What's unclear: Whether to pass token in first WebSocket message instead of URL.
   - Recommendation: Query parameter is fine for MVP. It matches the Gemini API's own `?key=` pattern. The token is short-lived and the connection is over TLS. Can add first-message auth in a future phase if needed.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest with pytest-timeout |
| Config file | `docker/pytest.ini` |
| Quick run command | `cd docker && python -m pytest tests/test_voice.py -x -v` |
| Full suite command | `cd docker && python -m pytest tests/ -x -v --timeout=30` |
| Estimated runtime | ~5 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| VOICE-02 (config msg) | `_gemini_build_config()` produces valid Gemini setup JSON with required fields | unit | `cd docker && python -m pytest tests/test_voice.py::test_gemini_config_structure -x` | No (Wave 0 gap) |
| VOICE-02 (relay) | Browser binary PCM -> base64 JSON transcoding works correctly | unit | `cd docker && python -m pytest tests/test_voice.py::test_pcm_to_gemini_json -x` | No (Wave 0 gap) |
| VOICE-02 (relay reverse) | Gemini JSON audio -> binary PCM transcoding works correctly | unit | `cd docker && python -m pytest tests/test_voice.py::test_gemini_json_to_pcm -x` | No (Wave 0 gap) |
| VOICE-11 (resumption) | `sessionResumptionUpdate` message handler saves handle to session state | unit | `cd docker && python -m pytest tests/test_voice.py::test_session_resumption_handle_saved -x` | No (Wave 0 gap) |
| VOICE-11 (goaway) | GoAway message triggers reconnect with saved handle | unit | `cd docker && python -m pytest tests/test_voice.py::test_goaway_triggers_reconnect -x` | No (Wave 0 gap) |
| VOICE-11 (compression) | Config message includes `contextWindowCompression.slidingWindow` | unit | `cd docker && python -m pytest tests/test_voice.py::test_config_has_compression -x` | No (Wave 0 gap) |
| SETUP-03 (token create) | `_voice_session_token_create()` returns valid token that validates | unit | `cd docker && python -m pytest tests/test_voice.py::test_voice_token_create_validate -x` | No (Wave 0 gap) |
| SETUP-03 (token expire) | Token expires after TTL | unit | `cd docker && python -m pytest tests/test_voice.py::test_voice_token_expiry -x` | No (Wave 0 gap) |
| SETUP-03 (token endpoint) | `GET /api/voice/token` returns token when authed with Gemini key | integration | `cd docker && python -m pytest tests/test_voice.py::test_voice_token_endpoint -x` | No (Wave 0 gap) |
| SETUP-03 (ws auth) | WebSocket connection rejected without valid token | integration | `cd docker && python -m pytest tests/test_voice.py::test_ws_voice_rejects_no_token -x` | No (Wave 0 gap) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd docker && python -m pytest tests/test_voice.py -x -v`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work`
- **Estimated feedback latency per task:** ~5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/test_voice.py` -- covers all VOICE-02, VOICE-11, SETUP-03 behaviors listed above
- [ ] Test helper: mock Gemini WebSocket server (accepts config, echoes audio, sends GoAway/sessionResumptionUpdate on demand)
- [ ] Test helper: vault fixture that writes a test Gemini API key to vault.yaml

## Sources

### Primary (HIGH confidence)
- [Gemini Live API WebSocket Reference](https://ai.google.dev/api/live) -- BidiGenerateContentSetup fields, server message types, SessionResumptionConfig, ContextWindowCompressionConfig
- [Gemini Live API Getting Started (WebSocket)](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket) -- WebSocket URL format, config message structure, audio format, realtimeInput
- [Gemini Live API Session Management](https://ai.google.dev/gemini-api/docs/live-session) -- GoAway handling, session resumption, context window compression, resumption handle validity (2 hours)
- [Gemini Ephemeral Tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens) -- Token lifetime defaults (1min new session, 30min messages), v1alpha requirement, undocumented REST endpoint
- Phase 27 RESEARCH.md -- WebSocket infrastructure already implemented (ws_recv_frame, ws_send_frame, ws_client_connect, ws_start_ping_loop, handle_voice_ws echo scaffold)
- gateway.py source code -- Existing vault pattern, auth session pattern, connection management pattern

### Secondary (MEDIUM confidence)
- [Go Gemini Live API proxy](https://dev.to/combba/making-go-speak-real-time-our-gemini-live-api-websocket-proxy-41of) -- Bidirectional relay pattern, resumption handle storage, MIME type importance
- [Gemini Cookbook Issue #906](https://github.com/google-gemini/cookbook/issues/906) -- Tool response format workarounds
- STACK.md and PITFALLS.md research -- Comprehensive protocol details, session limits, audio format reference

### Tertiary (LOW confidence)
- Ephemeral token REST endpoint (`POST /auth_tokens` on v1alpha) -- derived from python-genai SDK source, not officially documented. Recommendation: skip ephemeral tokens, use server-side proxy instead.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all Python stdlib, existing Phase 27 primitives, no new dependencies
- Architecture: HIGH -- bidirectional relay is standard WebSocket proxy pattern, Gemini protocol well-documented
- Pitfalls: HIGH -- verified against official docs, Phase 27 research, and community reports
- Session management: HIGH -- GoAway, resumption, and compression are officially documented with clear JSON structures
- Session-scoped tokens: HIGH -- follows existing `_create_auth_session` pattern in gateway.py

**Research date:** 2026-03-27
**Valid until:** 2026-04-10 (Gemini 3.1 Flash Live is preview, may change. Core protocol is stable.)

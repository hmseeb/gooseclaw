# Phase 13: Relay Protocol Upgrade - Research

**Researched:** 2026-03-13
**Domain:** goosed REST /reply API, SSE streaming, multimodal content blocks, relay architecture
**Confidence:** HIGH

## Summary

Phase 13 switches the gateway's communication with goosed from a custom WebSocket protocol to the official REST `/reply` endpoint with SSE streaming. The current WS relay sends `{"type":"message","content":"<text>","session_id":"..."}` and receives text-only `{"type":"response","content":"<text chunk>"}` events. The new approach sends a POST to `/reply` with a `ChatRequest` containing a `Message` object whose `content` array holds typed blocks (text, image, etc.) and reads SSE events back. This is also the approach the goose desktop UI uses. no other client uses the WS protocol.

The goosed `/reply` endpoint accepts `ChatRequest` with a `user_message` field containing a `Message` object. The `Message.content` is an array of `MessageContent` variants: `text`, `image`, `toolRequest`, `toolResponse`, `thinking`, `reasoning`, etc. For multimodal input, images are `{"type":"image","data":"<base64>","mimeType":"image/jpeg"}` blocks alongside `{"type":"text","text":"..."}` blocks. Responses stream back as SSE with event types: `Message` (contains full assistant `Message` with typed content array), `Error`, `Finish`, `Ping`, `ModelChange`, `Notification`, `UpdateConversation`.

The critical architectural change: the relay return type needs to evolve from `(str, str)` (text, error) to something that can carry typed content blocks. However, for backward compatibility and to keep Phase 13 focused, the relay should still return text as the primary response (extracted from text blocks) but additionally surface non-text content blocks for Phase 14's outbound media routing.

**Primary recommendation:** Replace `_do_ws_relay` and `_do_ws_relay_streaming` with `_do_rest_relay` and `_do_rest_relay_streaming` that POST to `/reply` and parse SSE. Update `_relay_to_goose_web` signature to accept optional `content_blocks` (list of dicts) alongside `user_text`. Keep the return type as `(str, str)` for now but add a third return value for non-text response blocks: `(str, str, list)`.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MEDIA-10 | Gateway relay sends multimodal content blocks to goose (images as base64 in content array) instead of text-only strings | ChatRequest format verified from goose source + integration tests. Content array supports `{"type":"text","text":"..."}` and `{"type":"image","data":"<base64>","mimeType":"..."}` blocks. POST /reply endpoint confirmed. |
| MEDIA-11 | Gateway parses typed content blocks in goose responses (text, image, audio) and routes to outbound adapter | SSE Message events contain `message.content` array with typed MessageContent variants. Text extracted by filtering `type=="text"`, images by `type=="image"`, tool results may contain embedded images. |
| MEDIA-12 | Relay upgrade is backward-compatible: text-only messages still work identically | When content_blocks is None/empty, send single text block `[{"type":"text","text":"<user_text>"}]`. Return text string as primary value. All existing callers continue to work unchanged. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.x | Everything | Project constraint: no pip packages |

### Key Stdlib Modules Used
| Module | Purpose | Already Imported |
|--------|---------|-----------------|
| `http.client` | HTTP POST to goosed /reply on localhost:3001 | Yes (used by _update_goose_session_provider, _create_goose_session) |
| `json` | Serialize ChatRequest, parse SSE event data | Yes |
| `base64` | Encode internal auth token (Basic auth) | Yes |
| `urllib.parse` | URL encoding | Yes |
| `time` | Timestamps, performance logging | Yes |
| `socket` | Low-level timeout control (may not need for http.client) | Yes |
| `threading` | Cancellation events, typing loops | Yes |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `http.client` for SSE | Raw socket + manual HTTP | http.client handles chunked transfer-encoding. Raw sockets would need manual HTTP parsing. Use http.client. |
| Keeping WS for text, REST for multimodal | Full switch to REST | Full switch is cleaner, one code path. WS protocol is not part of goosed's official API. Switch entirely. |
| Third return value for media blocks | Separate callback | Callback adds complexity. Tuple expansion is simpler and backward-compatible (callers destructure only what they need). |

## Architecture Patterns

### Current Relay Flow (WS, being replaced)
```
_relay_to_goose_web(user_text, session_id, ...)
  -> _do_ws_relay(user_text, session_id, ...)
     -> _ws_connect("127.0.0.1", 3001, "/ws?token=...")
     -> _ws_send_text(sock, {"type":"message","content":"<text>",...})
     -> loop: _ws_recv_text(sock) -> parse JSON events
        - "response" -> collect content string chunks
        - "tool_request" -> auto-approve
        - "complete" -> break
     -> return ("".join(collected), "")
```

### New Relay Flow (REST + SSE)
```
_relay_to_goose_web(user_text, session_id, ..., content_blocks=None)
  -> _do_rest_relay(user_text, session_id, content_blocks=None, ...)
     -> http.client.HTTPConnection("127.0.0.1", 3001)
     -> POST /reply with ChatRequest JSON body
     -> Read SSE stream: parse "data: " lines
        - "Message" -> extract message.content array
          - text blocks -> collect text
          - image blocks -> collect as media blocks
        - "Error" -> return error
        - "Finish" -> break
     -> return (text, "", media_blocks)
```

### ChatRequest Format (verified from goose source + integration tests)
```python
# Source: goose desktop integration test + reply.rs handler
chat_request = {
    "session_id": session_id,
    "user_message": {
        "role": "user",
        "created": int(time.time()),
        "content": content_blocks,  # array of typed content blocks
        "metadata": {
            "userVisible": True,
            "agentVisible": True,
        }
    }
}
```

Where `content_blocks` for a text-only message:
```python
[{"type": "text", "text": "Hello, what's in this image?"}]
```

For a multimodal message (text + image):
```python
[
    {"type": "text", "text": "What's in this image?"},
    {"type": "image", "data": "<base64>", "mimeType": "image/jpeg"}
]
```

### SSE Response Format (verified from reply.rs)
```
data: {"type":"Message","message":{"role":"assistant","created":1710000000,"content":[{"type":"text","text":"I can see..."}],"metadata":{"userVisible":true,"agentVisible":true}},"token_state":{...}}

data: {"type":"Message","message":{"role":"assistant","created":1710000001,"content":[{"type":"toolRequest","id":"abc","tool_call":...}],...},"token_state":{...}}

data: {"type":"Finish","reason":"endTurn","token_state":{...}}
```

Each SSE event is a single `data: ` line with a JSON object. Events are separated by blank lines.

### MessageContent Variants in Responses (verified from goose message.rs + types.gen.ts)
```python
# Content block types we need to handle in responses:
# (serde tag = "type" field)

# Text block
{"type": "text", "text": "response text here"}

# Image block (from tool results like screenshots)
{"type": "image", "data": "<base64>", "mimeType": "image/png"}

# Tool request (agent wants to call a tool)
{"type": "toolRequest", "id": "...", "tool_call": {...}}

# Tool response (tool execution result, may contain nested content)
{"type": "toolResponse", "id": "...", "tool_result": {...}}

# Thinking/reasoning (extended thinking)
{"type": "thinking", "thinking": "...", "signature": "..."}
{"type": "reasoning", "text": "..."}

# Action required (tool confirmation needed)
{"type": "actionRequired", "data": {...}}

# System notification
{"type": "systemNotification", "msg": "...", "notificationType": "..."}
```

### Where Changes Go

```
gateway.py modifications:
  1. _relay_to_goose_web()       # Add content_blocks param, pass through
  2. _do_rest_relay()            # NEW: Replace _do_ws_relay
  3. _do_rest_relay_streaming()  # NEW: Replace _do_ws_relay_streaming
  4. _parse_sse_events()         # NEW: SSE line parser generator
  5. _build_chat_request()       # NEW: Construct ChatRequest from text + blocks
  6. BotInstance._do_message_relay()  # Pass media content blocks to relay
  7. Legacy _telegram_poll_loop relay # Pass media content blocks to relay
  8. ChannelRelay.__call__()     # Pass content blocks from InboundMessage
```

### Pattern 1: SSE Line Parser

**What:** A generator that reads http.client response line-by-line and yields parsed SSE events.

**Why:** SSE is simple text protocol. Each event is `data: <json>\n\n`. http.client can read line-by-line from the response.

```python
# Source: SSE specification + goose reply.rs
def _parse_sse_events(response):
    """Yield parsed SSE events from an http.client HTTPResponse.

    SSE format: lines starting with "data: " followed by JSON.
    Events separated by blank lines.
    """
    buf = ""
    while True:
        line = response.readline()
        if not line:
            break  # connection closed
        line = line.decode("utf-8", errors="replace").rstrip("\r\n")

        if line.startswith("data: "):
            data_str = line[6:]
            try:
                yield json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
        # blank lines separate events, ignore other lines (comments, etc.)
```

### Pattern 2: Content Block Assembly for Outbound (MEDIA-10)

**What:** Build the content array from InboundMessage's text and media.

```python
def _build_content_blocks(user_text, media_list=None):
    """Build content block array for ChatRequest.

    user_text: the user's text message (may be empty for media-only)
    media_list: list of MediaContent objects from InboundMessage
    Returns: list of content block dicts
    """
    blocks = []
    if user_text and user_text.strip():
        blocks.append({"type": "text", "text": user_text})
    if media_list:
        for mc in media_list:
            block = mc.to_content_block()
            if block:
                blocks.append(block)
    # fallback: if no blocks at all, send empty text
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks
```

### Pattern 3: Response Content Extraction (MEDIA-11)

**What:** Parse assistant message content array into text + media blocks.

```python
def _extract_response_content(content_array):
    """Extract text and media from a Message content array.

    Returns: (text_str, media_blocks_list)
    text_str: concatenated text from all text blocks
    media_blocks_list: list of non-text content block dicts (image, etc.)
    """
    text_parts = []
    media_blocks = []
    for block in content_array:
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "image":
            media_blocks.append(block)
        elif btype == "toolResponse":
            # tool results may contain nested content with images
            result = block.get("tool_result", {})
            if isinstance(result, dict):
                nested = result.get("value", {}).get("content", [])
                if isinstance(nested, list):
                    for item in nested:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                            elif item.get("type") == "image":
                                media_blocks.append(item)
        # thinking, reasoning, systemNotification: skip for user output
    return "\n".join(text_parts), media_blocks
```

### Pattern 4: Backward-Compatible Relay Signature (MEDIA-12)

**What:** Evolve `_relay_to_goose_web` without breaking any callers.

```python
# BEFORE (current signature):
def _relay_to_goose_web(user_text, session_id, chat_id=None, channel=None,
                        flush_cb=None, verbosity=None, sock_ref=None, flush_interval=4.0):
    # Returns: (response_text, error_string)

# AFTER (new signature, backward-compatible):
def _relay_to_goose_web(user_text, session_id, chat_id=None, channel=None,
                        flush_cb=None, verbosity=None, sock_ref=None, flush_interval=4.0,
                        content_blocks=None):
    # Returns: (response_text, error_string, media_blocks)
    # media_blocks defaults to [] when no media in response
    # Callers that destructure (text, err) = ... will get a ValueError
    # SO: update all callers to (text, err, *_) = ... or (text, err, media) = ...
```

**IMPORTANT:** The return type change from 2-tuple to 3-tuple will break existing callers that destructure as `text, err = _relay_to_goose_web(...)`. All ~12 call sites must be updated. Options:
1. **Update all callers** to `text, err, *_ = ...` (safest, explicit)
2. Return a namedtuple (overkill for this)
3. Use `text, err, media = ...` where we need media, `text, err, *_ = ...` where we don't

Recommendation: Option 1 for most callers, option 3 for the Telegram _do_message_relay and ChannelRelay.__call__ where media matters.

### Pattern 5: Tool Confirmation via REST

**What:** The current WS relay auto-approves tool_request events by sending tool_confirmation messages back. With REST SSE, tool confirmations work differently.

The `/reply` endpoint handles tool execution internally. The agent processes tools and streams results. The SSE events include `toolRequest` blocks in Message events for visibility, but the agent doesn't wait for client confirmation through the SSE stream. Tool confirmation is handled via the separate `/action_required` endpoint if the agent has `needs_confirmation: true` tools.

For the GooseClaw gateway, tools are auto-approved (no human-in-the-loop). The REST /reply endpoint handles this by default, no special confirmation logic needed. This is SIMPLER than the WS approach.

### Anti-Patterns to Avoid

- **Keeping the WS code as fallback:** Don't maintain two relay paths. The REST approach is the official API. Remove or comment-out WS relay functions.
- **Parsing SSE with regex:** Use simple line-by-line reading. SSE is trivially parseable.
- **Buffering entire SSE response before processing:** Stream and process event-by-event, just like the WS relay does now.
- **Changing InboundMessage or MediaContent:** Phase 12 already provides exactly what Phase 13 needs. Don't modify the inbound data model.
- **Sending image data as URL instead of base64:** Goose expects inline base64 data, not URLs. The `to_content_block()` method already returns base64.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SSE parsing | Custom event-stream parser with buffering | Simple line-by-line reader with `data: ` prefix detection | SSE is trivial, goosed sends single-line data events. No need for multi-line data handling. |
| HTTP POST with streaming response | Raw socket HTTP | `http.client.HTTPConnection` | Already used elsewhere in gateway.py (session creation, provider update). Handles chunked encoding. |
| Content block serialization | Custom serializer | `json.dumps()` on dicts | Content blocks are plain dicts, no special serialization needed. |
| Auth header construction | Custom auth logic | Copy pattern from `_update_goose_session_provider` (line 4829) | Basic auth with internal token, already proven pattern. |

**Key insight:** The REST /reply approach is simpler than the WS approach. No WebSocket handshake, no frame masking, no ping/pong handling, no manual connection upgrade. Just HTTP POST + read SSE lines.

## Common Pitfalls

### Pitfall 1: http.client Response Reading for SSE
**What goes wrong:** `http.client.HTTPResponse` may buffer the entire response before returning, defeating streaming.
**Why it happens:** By default, `response.read()` reads everything. Need to use `response.readline()` or `response.read1()` for streaming.
**How to avoid:** Use `response.readline()` in a loop. Set `response.fp` buffering appropriately. Alternatively, use raw socket reading after sending HTTP request via http.client.
**Warning signs:** First chunk takes as long as the entire response (no incremental delivery).

### Pitfall 2: Chunked Transfer-Encoding
**What goes wrong:** SSE responses from goosed use chunked transfer-encoding. http.client handles chunk decoding transparently for `read()` but need to verify `readline()` also works.
**Why it happens:** HTTP/1.1 servers commonly use chunked encoding for streaming responses.
**How to avoid:** Test that `response.readline()` decodes chunks correctly. If not, read raw and split on newlines manually.
**Warning signs:** Partial JSON or garbled data in SSE events.

### Pitfall 3: Return Type Change Breaking Callers
**What goes wrong:** Changing `_relay_to_goose_web` return from 2-tuple to 3-tuple causes `ValueError: too many values to unpack` at all existing call sites.
**Why it happens:** Python tuple destructuring is strict on count.
**How to avoid:** Update ALL call sites simultaneously. There are ~12 call sites (see grep results). Use `text, err, *_ = ...` pattern for sites that don't need media blocks.
**Warning signs:** Any caller not updated will crash at runtime.

### Pitfall 4: Session ID Format Mismatch
**What goes wrong:** The REST /reply endpoint expects the session_id to match an existing agent session created via `/agent/start` or GET `/` redirect.
**Why it happens:** The current WS relay creates sessions via GET `/` (which redirects and returns session_id). The REST relay needs the same session management.
**How to avoid:** Keep `_create_goose_session()` and `_get_session_id()` unchanged. They already create valid sessions. The session_id from these functions works with /reply.
**Warning signs:** 404 or "session not found" errors from /reply.

### Pitfall 5: Large Base64 Images in POST Body
**What goes wrong:** A 20MB Telegram file becomes ~27MB base64. The POST body could be very large.
**Why it happens:** Base64 encoding adds ~33% overhead.
**How to avoid:** The /reply endpoint has a 50MB body limit (`DefaultBodyLimit::max(50 * 1024 * 1024)` in reply.rs). Telegram limits downloads to 20MB. So worst case ~27MB, well within limits. Still, log the content size for debugging.
**Warning signs:** 413 (Payload Too Large) responses. Won't happen with Telegram files but could with future channels.

### Pitfall 6: Timeout Handling
**What goes wrong:** The current WS relay uses `sock.settimeout(300)` (5 minutes) for long tool calls. http.client timeout works differently.
**Why it happens:** http.client timeout applies to individual socket operations, not the entire request.
**How to avoid:** Set `timeout=300` on HTTPConnection (applies to connect + each read). For SSE streaming, each readline should return within this timeout. Long agent processing will send Ping events every 500ms, keeping the connection alive.
**Warning signs:** Timeouts during long tool executions.

### Pitfall 7: SSE Event Type Casing
**What goes wrong:** Goose SSE events use PascalCase types (`Message`, `Error`, `Finish`) not the camelCase or lowercase used in the WS protocol.
**Why it happens:** Rust serde serialization of the MessageEvent enum uses the variant name directly.
**How to avoid:** Match on PascalCase: `event.get("type") == "Message"` not `"message"`.
**Warning signs:** All events fall through to the "unknown type" branch.

## Code Examples

### Example 1: REST Relay (non-streaming)

```python
# Source: goose reply.rs, goose integration test, existing _update_goose_session_provider pattern
def _do_rest_relay(user_text, session_id, content_blocks=None, sock_ref=None):
    """POST to goosed /reply, parse SSE response.

    Returns (response_text, error_string, media_blocks).
    """
    t0 = time.time()
    blocks = content_blocks or [{"type": "text", "text": user_text}]

    chat_request = json.dumps({
        "session_id": session_id,
        "user_message": {
            "role": "user",
            "created": int(time.time()),
            "content": blocks,
            "metadata": {"userVisible": True, "agentVisible": True},
        }
    }).encode("utf-8")

    auth_value = base64.b64encode(f"user:{_INTERNAL_GOOSE_TOKEN}".encode()).decode()

    conn = http.client.HTTPConnection("127.0.0.1", GOOSE_WEB_PORT, timeout=300)
    if sock_ref is not None:
        sock_ref[0] = conn  # for external cancellation

    conn.request("POST", "/reply", body=chat_request, headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_value}",
        "Accept": "text/event-stream",
    })

    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return "", f"goosed /reply returned {resp.status}: {body[:200]}", []

    text_parts = []
    media_blocks = []

    for event in _parse_sse_events(resp):
        etype = event.get("type", "")

        if etype == "Message":
            msg = event.get("message", {})
            content = msg.get("content", [])
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "image":
                    media_blocks.append(block)
                # toolRequest, toolResponse, thinking: logged but not user-facing

        elif etype == "Error":
            err_msg = event.get("error", "Unknown error")
            conn.close()
            return "", f"Goose error: {err_msg}", []

        elif etype == "Finish":
            break

    conn.close()
    full_text = "\n".join(text_parts).strip()
    return full_text, "", media_blocks
```

### Example 2: Building Content Blocks from InboundMessage

```python
# Source: Phase 12 MediaContent.to_content_block() + goose MessageContent types
def _build_content_blocks(user_text, inbound_msg=None):
    """Build content block array from text and optional InboundMessage media."""
    blocks = []
    if user_text and user_text.strip():
        blocks.append({"type": "text", "text": user_text})

    if inbound_msg and inbound_msg.has_media:
        for mc in inbound_msg.media:
            if isinstance(mc, MediaContent):
                block = mc.to_content_block()
                if block:
                    blocks.append(block)

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    return blocks
```

### Example 3: Updated _do_message_relay

```python
# In BotInstance._do_message_relay, after downloading media:
content_blocks = None
if inbound_msg and inbound_msg.has_media:
    content_blocks = _build_content_blocks(text, inbound_msg)

# Then pass to relay:
response_text, error, media_blocks = _relay_to_goose_web(
    text, session_id, chat_id=chat_id, channel=self.channel_key,
    sock_ref=_sock_ref, content_blocks=content_blocks,
)
# media_blocks available for Phase 14 (outbound media routing)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Custom WS protocol (`/ws?token=...`) | REST POST `/reply` + SSE streaming | Phase 13 (this phase) | Aligns with official goose API, enables multimodal |
| Text-only relay content | Content block arrays (text, image, audio) | Phase 13 (this phase) | Images can be sent to and received from goose |
| 2-tuple return `(text, error)` | 3-tuple return `(text, error, media_blocks)` | Phase 13 (this phase) | Enables Phase 14 outbound media routing |
| Manual WS frame masking | stdlib http.client | Phase 13 (this phase) | Simpler, less code, fewer bugs |
| Auto-approve tool_request via WS | Tool auto-approval built into /reply | Phase 13 (this phase) | No manual confirmation logic needed |

**Deprecated/removed:**
- `_ws_connect`, `_ws_send_text`, `_ws_recv_frame`, `_ws_recv_text`: WS helpers no longer needed after switch to REST. Can be removed or commented out.
- `_do_ws_relay`, `_do_ws_relay_streaming`: Replaced by REST equivalents.
- `_StreamBuffer`: The streaming flush logic should be preserved but adapted for SSE events instead of WS frames.

## Critical Design Decisions

### 1. Full Switch vs. Hybrid (WS for text, REST for multimodal)

**Decision: Full switch to REST.**

Rationale:
- The `/ws` endpoint does not appear in goose-server's official API (no OpenAPI entry, no route registration found in source)
- The goose desktop UI exclusively uses REST `/reply` with SSE
- Maintaining two relay paths doubles testing and debugging surface
- REST is simpler (no WS handshake, frame masking, ping/pong)

### 2. Streaming Architecture

The current streaming relay (`_do_ws_relay_streaming`) uses `_StreamBuffer` to batch text chunks and flush to a callback on time/size triggers. The REST streaming relay should:
- Read SSE events line by line
- Extract text content from Message events
- Feed text to the same `_StreamBuffer` mechanism
- Emit tool status messages the same way

The `_StreamBuffer` class and the flush_cb pattern remain unchanged. Only the source of events changes (SSE lines instead of WS frames).

### 3. Cancellation Mechanism

The current WS relay supports cancellation via `sock_ref[0].close()` (closing the socket). For REST:
- Store the `http.client.HTTPConnection` in `sock_ref[0]`
- Cancellation: call `conn.close()` from another thread
- The readline loop will raise an exception, same as WS socket close
- The cancelled Event in `sock_ref[1]` remains unchanged

### 4. Session Management Unchanged

`_create_goose_session()` creates sessions via `GET /` redirect. This still works for REST /reply. The session_id is valid for both WS and REST. No changes to session management.

## Open Questions

1. **SSE readline behavior with http.client**
   - What we know: http.client.HTTPResponse supports readline(). Chunked encoding is decoded transparently.
   - What's unclear: Whether readline() blocks correctly on slow SSE streams or returns empty on chunk boundaries.
   - Recommendation: Test empirically. If readline() has issues, fall back to reading raw bytes and splitting on `\n`. LOW risk since http.client has handled chunked encoding for years.

2. **Goose response images (MEDIA-11 depth)**
   - What we know: MessageContent::Image exists in the response type. Tool results can contain images (screenshots, generated images).
   - What's unclear: How commonly goose actually returns image blocks. Whether the base64 data is always inline or sometimes a URL.
   - Recommendation: Handle inline base64 images. If we encounter URL references, log and skip for now. Phase 14 will handle outbound delivery.

3. **Tool confirmation via REST**
   - What we know: The /reply endpoint handles tool execution internally. There's a separate /action_required endpoint.
   - What's unclear: Whether some tools require confirmation even via REST, and if so, how that surfaces in SSE events.
   - Recommendation: For now, assume tools are auto-approved (matching current behavior). If ActionRequired events appear, log them. Can be addressed later if needed.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (stdlib) + pytest runner |
| Config file | None (tests run with pytest discovery) |
| Quick run command | `python3 -m pytest docker/test_gateway.py -x -q` |
| Full suite command | `python3 -m pytest docker/test_gateway.py -v` |
| Estimated runtime | ~15 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MEDIA-10 | _build_content_blocks creates correct array for text-only | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_build_content_blocks_text_only"` | No, Wave 0 gap |
| MEDIA-10 | _build_content_blocks creates correct array for text+image | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_build_content_blocks_with_image"` | No, Wave 0 gap |
| MEDIA-10 | _build_chat_request produces valid ChatRequest JSON | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_build_chat_request"` | No, Wave 0 gap |
| MEDIA-10 | _do_rest_relay sends correct POST to /reply (mocked) | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_rest_relay_sends_post"` | No, Wave 0 gap |
| MEDIA-11 | _parse_sse_events yields correct events from SSE data | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_parse_sse_events"` | No, Wave 0 gap |
| MEDIA-11 | _extract_response_content separates text from images | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_extract_response_content"` | No, Wave 0 gap |
| MEDIA-11 | Streaming relay delivers text chunks via flush_cb | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_rest_relay_streaming"` | No, Wave 0 gap |
| MEDIA-12 | Text-only relay produces identical output to old WS relay | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_text_only_backward_compat"` | No, Wave 0 gap |
| MEDIA-12 | All existing relay callers updated (no tuple unpack errors) | unit | `python3 -m pytest docker/test_gateway.py -x -q` (full suite) | Existing tests serve as regression |
| MEDIA-12 | _relay_to_goose_web with no content_blocks sends text block | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "test_relay_default_text_block"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `python3 -m pytest docker/test_gateway.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~15 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/test_gateway.py::TestBuildContentBlocks` -- covers MEDIA-10 content block assembly
- [ ] `docker/test_gateway.py::TestParseSSEEvents` -- covers MEDIA-11 SSE parsing
- [ ] `docker/test_gateway.py::TestExtractResponseContent` -- covers MEDIA-11 response parsing
- [ ] `docker/test_gateway.py::TestRestRelay` -- covers MEDIA-10/12 relay POST (mocked http.client)
- [ ] `docker/test_gateway.py::TestRestRelayStreaming` -- covers MEDIA-11 streaming with flush_cb
- [ ] `docker/test_gateway.py::TestRelayBackwardCompat` -- covers MEDIA-12 tuple return + text fallback

## All Relay Call Sites (must update for 3-tuple return)

These are ALL places that call `_relay_to_goose_web()` and destructure the result:

| Line | Location | Current Pattern | Update To |
|------|----------|-----------------|-----------|
| 410 | `BotInstance._do_message_relay` (quiet) | `response_text, error = ...` | `response_text, error, media = ...` |
| 445 | `BotInstance._do_message_relay` (streaming) | `response_text, error = ...` | `response_text, error, media = ...` |
| 610 | `BotInstance._do_message_relay` (kick greeting) | `txt, err = ...` | `txt, err, *_ = ...` |
| 3689 | `ChannelRelay.__call__` (streaming) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 3695 | `ChannelRelay.__call__` (non-streaming) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 3708 | `ChannelRelay.__call__` (retry streaming) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 3714 | `ChannelRelay.__call__` (retry non-streaming) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 4295 | `compact` command handler | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 5390 | Legacy poll loop (media relay) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 5483 | Legacy poll loop (quiet) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 5518 | Legacy poll loop (streaming) | `response_text, error = ...` | `response_text, error, *_ = ...` |
| 5584 | Legacy poll loop (kick greeting) | `txt, err = ...` | `txt, err, *_ = ...` |

Total: 12 call sites. All must be updated atomically with the return type change.

## Sources

### Primary (HIGH confidence)
- [block/goose reply.rs](https://github.com/block/goose/blob/main/crates/goose-server/src/routes/reply.rs) - ChatRequest structure, SSE event types, 50MB body limit
- [block/goose message.rs](https://github.com/block/goose/tree/main/crates/goose/src/conversation/) - MessageContent enum: Text, Image, ToolRequest, ToolResponse, Thinking, Reasoning, etc.
- [block/goose types.gen.ts](https://github.com/block/goose/blob/main/ui/desktop/src/api/types.gen.ts) - TypeScript API types confirming content block schema
- [block/goose goosed.test.ts](https://github.com/block/goose/blob/main/ui/desktop/tests/integration/goosed.test.ts) - Integration test showing exact ChatRequest format
- gateway.py source (lines 4663-5251) - Current WS relay implementation
- gateway.py source (lines 3444-3520) - InboundMessage, MediaContent, to_content_block()
- gateway.py source (lines 4808-4846) - Existing REST pattern (_update_goose_session_provider)

### Secondary (MEDIUM confidence)
- [block/goose route registration](https://github.com/block/goose/blob/main/crates/goose-server/src/routes/mod.rs) - Confirmed no /ws route exists in goose-server routes
- [block/goose OpenAPI spec](https://github.com/block/goose/blob/main/crates/goose-server/src/openapi.rs) - No /ws endpoint in API spec

### Tertiary (LOW confidence)
- SSE readline behavior with http.client chunked encoding - assumed to work based on Python stdlib docs, needs empirical verification

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Python stdlib only, all modules already imported
- Architecture: HIGH - goose /reply endpoint verified from source + tests + types
- Pitfalls: HIGH - most are well-understood Python/HTTP patterns
- Response format: MEDIUM - Image blocks in responses not empirically verified, inferred from type definitions
- SSE streaming: MEDIUM - readline() + chunked encoding interaction not empirically tested

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (goose API is relatively stable, REST /reply is the primary interface)

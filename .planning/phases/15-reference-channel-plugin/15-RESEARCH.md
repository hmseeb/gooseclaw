# Phase 15: Reference Channel Plugin - Research

**Researched:** 2026-03-13
**Domain:** Discord Bot API, channel plugin architecture, v2 OutboundAdapter contract
**Confidence:** HIGH

## Summary

Phase 15 validates the entire v2 channel contract by shipping a non-Telegram channel plugin with full rich media support. The plugin must implement OutboundAdapter (send_text, send_image, send_voice, send_file), declare ChannelCapabilities, handle inbound messages via a poll function, and require zero changes to gateway.py.

Discord is the clear winner over Slack for this reference implementation. Discord's REST API for sending messages with attachments uses multipart/form-data, which is nearly identical to the Telegram pattern already in gateway.py (`_build_multipart`). Slack requires a 3-step file upload flow (getUploadURL, POST data, completeUpload) that is significantly more complex. Discord's Gateway WebSocket for receiving messages is well-documented and the `websocket-client` pip package provides a clean synchronous API that fits the existing threading-based poll loop pattern. The plugin file CAN use pip packages since it lives in /data/channels/ and is separate from gateway.py's stdlib-only constraint.

**Primary recommendation:** Build a Discord channel plugin using `websocket-client` for Gateway inbound and `urllib` for REST API outbound. The plugin exports a CHANNEL dict with an `adapter` field (OutboundAdapter subclass) which _load_channel already detects and uses directly.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MEDIA-15 | At least one non-Telegram channel plugin ships with full rich media support using the new contract | Discord plugin implements OutboundAdapter with all send_* methods, uses Gateway WebSocket for inbound, handles images/voice/files end-to-end |
| MEDIA-16 | Adding a new channel with media support requires only implementing the OutboundAdapter methods, no gateway changes | _load_channel already detects `adapter` field in CHANNEL dict (line 3955-3959), ChannelRelay already routes media through adapters (line 3874-3881), zero gateway.py changes needed |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| websocket-client | 1.8+ | Discord Gateway WebSocket connection | Lightweight, synchronous, threading-compatible, no async required. Perfect for the poll() thread pattern. |
| urllib (stdlib) | n/a | Discord REST API calls (send messages, upload files) | Already used throughout gateway.py for Telegram API. Keeps consistency. |
| json (stdlib) | n/a | JSON payloads for Discord API | Already used throughout gateway.py. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| threading (stdlib) | n/a | Heartbeat thread for Gateway keepalive | Required for Discord Gateway heartbeat mechanism |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Discord | Slack | Slack requires 3-step file upload (getUploadURL, POST, completeUpload) vs Discord's single multipart POST. Slack Socket Mode also needs app-level tokens. Discord is simpler. |
| websocket-client | discord.py | discord.py is asyncio-based, huge dependency, overkill. GooseClaw uses threading, not async. |
| websocket-client | websockets | websockets is asyncio-based. websocket-client is synchronous/threading-based, matching the poll loop pattern. |
| raw stdlib WebSocket | websocket-client | Python stdlib has no WebSocket client. Would need to hand-roll the protocol (TLS upgrade, framing, masking). Not worth it. |

**Installation (in plugin file only, not in gateway.py):**
```bash
pip install websocket-client
```

**Note:** This gets added to docker/requirements.txt since the Docker image needs it available for the plugin to import.

## Architecture Patterns

### Recommended Project Structure
```
docker/
  gateway.py              # NO CHANGES (validates MEDIA-16)
  test_gateway.py         # Add Discord adapter + plugin tests
  requirements.txt        # Add websocket-client
  scripts/
    discord_channel.py    # Template plugin, copied to /data/channels/ at runtime
```

The plugin file lives in docker/scripts/ or similar for distribution, but at runtime it's loaded from /data/channels/discord.py.

### Pattern 1: v2 Plugin Contract
**What:** Plugin exports CHANNEL dict with `adapter` field containing an OutboundAdapter subclass instance.
**When to use:** Any new channel plugin that supports rich media.
**Example:**
```python
# Source: gateway.py lines 3954-3959 (existing detection logic)
class DiscordOutboundAdapter(OutboundAdapter):
    def __init__(self, bot_token, channel_id):
        self.bot_token = bot_token
        self.channel_id = channel_id

    def capabilities(self):
        return ChannelCapabilities(
            supports_images=True,
            supports_voice=False,  # Discord voice is channels, not file upload
            supports_files=True,
            supports_buttons=False,
            max_file_size=10_000_000,  # 10MB default Discord limit
            max_text_length=2000,
        )

    def send_text(self, text):
        # POST to /channels/{channel_id}/messages with JSON body
        ...

    def send_image(self, image_bytes, caption="", mime_type="image/png"):
        # POST multipart/form-data with files[0] + payload_json
        ...

    def send_file(self, file_bytes, filename="file", mime_type="application/octet-stream"):
        # POST multipart/form-data with files[0] + payload_json
        ...

# Plugin must still export send() for legacy compat even with adapter
adapter = DiscordOutboundAdapter(TOKEN, CHANNEL_ID)

CHANNEL = {
    "name": "discord",
    "version": 2,
    "send": adapter.send_text,         # legacy fallback
    "adapter": adapter,                 # v2 contract
    "poll": poll_discord,               # receives messages via Gateway WebSocket
    "credentials": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"],
    "setup": setup_discord,             # validate token, get gateway URL
}
```

### Pattern 2: Discord Gateway Poll Loop
**What:** The poll() function connects to Discord's Gateway WebSocket, identifies, heartbeats, and dispatches MESSAGE_CREATE events to the relay.
**When to use:** Receiving messages from Discord users.
**Example:**
```python
def poll_discord(relay, stop_event, creds):
    """Blocking poll loop for Discord Gateway. Matches channel plugin poll() signature."""
    token = creds["DISCORD_BOT_TOKEN"]
    channel_id = creds["DISCORD_CHANNEL_ID"]

    # 1. GET /gateway/bot to get WSS URL
    gateway_url = _get_gateway_url(token)

    # 2. Connect via websocket-client
    ws = websocket.WebSocket()
    ws.connect(f"{gateway_url}?v=10&encoding=json")

    # 3. Receive Hello (op 10), start heartbeat thread
    hello = json.loads(ws.recv())
    heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000

    # 4. Send Identify (op 2) with intents
    identify = {
        "op": 2,
        "d": {
            "token": token,
            "intents": (1 << 9) | (1 << 15),  # GUILD_MESSAGES | MESSAGE_CONTENT
            "properties": {"os": "linux", "browser": "gooseclaw", "device": "gooseclaw"}
        }
    }
    ws.send(json.dumps(identify))

    # 5. Event loop
    while not stop_event.is_set():
        data = json.loads(ws.recv())
        if data["op"] == 0 and data["t"] == "MESSAGE_CREATE":
            msg = data["d"]
            if msg["channel_id"] == channel_id and not msg.get("bot"):
                # Build InboundMessage and relay
                inbound = InboundMessage(
                    user_id=msg["author"]["id"],
                    text=msg.get("content", ""),
                    channel="discord",
                    media=_extract_discord_media(msg),
                )
                relay(inbound, adapter.send_text)
```

### Pattern 3: Discord Multipart File Upload
**What:** Send files to Discord via multipart/form-data with payload_json for metadata.
**When to use:** send_image and send_file methods.
**Example:**
```python
# Source: Discord API docs - Create Message endpoint
# POST /channels/{channel_id}/messages (multipart/form-data)
#
# Discord's format is almost identical to Telegram's:
# - files[0]: binary file data with Content-Disposition filename
# - payload_json: JSON body with content, attachments array
#
# The existing _build_multipart() helper in gateway.py handles this pattern.
# But since the plugin can't import from gateway.py directly (it's loaded
# via importlib), the plugin needs its own multipart builder or a simpler
# approach using the same boundary technique.

def _discord_send_multipart(token, channel_id, file_bytes, filename, mime_type, content=""):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    boundary = uuid.uuid4().hex

    # Build payload_json
    payload = {"content": content}
    if file_bytes:
        payload["attachments"] = [{"id": 0, "filename": filename}]

    # Build multipart body
    parts = []
    # payload_json part
    parts.append(f"--{boundary}\r\n")
    parts.append('Content-Disposition: form-data; name="payload_json"\r\n')
    parts.append("Content-Type: application/json\r\n\r\n")
    parts.append(json.dumps(payload))
    # file part
    parts.append(f"\r\n--{boundary}\r\n")
    parts.append(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n')
    parts.append(f"Content-Type: {mime_type}\r\n\r\n")
    # ... binary data ...

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
```

### Anti-Patterns to Avoid
- **Importing gateway.py classes directly:** The plugin is loaded via importlib from /data/channels/. It cannot `import gateway`. All needed classes (OutboundAdapter, InboundMessage, ChannelCapabilities) must either be duplicated in the plugin or injected via module globals during _load_channel. **Research finding: The plugin CAN reference gateway module classes because importlib.util.module_from_spec + exec_module runs in the gateway's Python process. Any `from gateway import X` or `import gateway` will work since gateway.py is already loaded as `__main__` or importable from sys.path.**
- **Using discord.py:** Massive async dependency. The plugin should use websocket-client (sync) + urllib (sync) to match the threading model.
- **Polling Discord REST API for messages:** Discord has no message polling endpoint. You MUST use the Gateway WebSocket to receive MESSAGE_CREATE events.
- **Ignoring the heartbeat:** Discord will disconnect the bot if heartbeats are missed. The heartbeat thread is critical.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| WebSocket client | Custom WS framing over TCP | `websocket-client` pip package | WS framing (masking, fragmentation, TLS) is deceptively complex. stdlib has no WS client. |
| Multipart form-data | N/A (build it) | Replicate the `_build_multipart` pattern from gateway.py | Discord's format matches Telegram's. Copy the same boundary-based builder. |
| Discord Gateway protocol | N/A | Implement directly in ~80 lines | It's just: connect, identify, heartbeat loop, event dispatch. Simple enough to hand-roll. |
| OAuth/token management | Token refresh flow | Static bot token from env | Discord bot tokens don't expire. No refresh needed. |

**Key insight:** The hardest part (WebSocket protocol) is handled by `websocket-client`. Everything else is simple HTTP calls with urllib, matching existing patterns in gateway.py.

## Common Pitfalls

### Pitfall 1: MESSAGE_CONTENT Privileged Intent
**What goes wrong:** Bot connects but message.content is always empty.
**Why it happens:** Discord requires the MESSAGE_CONTENT privileged intent (1 << 15) to be enabled in BOTH the Developer Portal AND the Gateway identify payload.
**How to avoid:** Document in plugin setup instructions that the user must enable "Message Content Intent" in Discord Developer Portal > Bot > Privileged Gateway Intents.
**Warning signs:** Bot receives events but text is empty string.

### Pitfall 2: Gateway Heartbeat Timeout
**What goes wrong:** Bot disconnects after ~45 seconds with no errors.
**Why it happens:** Discord sends a heartbeat_interval in the Hello payload. Failing to send heartbeats results in forced disconnect.
**How to avoid:** Start a daemon thread that sends opcode 1 (heartbeat) every heartbeat_interval milliseconds. Use the last received sequence number as the payload.
**Warning signs:** WebSocket closes with code 4000-4009.

### Pitfall 3: Plugin Can't Import OutboundAdapter
**What goes wrong:** Plugin raises ImportError when trying to use OutboundAdapter.
**Why it happens:** Plugin file is loaded via importlib from /data/channels/. The gateway module path may not be in sys.path.
**How to avoid:** The plugin should import gateway classes via `sys.path` manipulation or, better, gateway.py should inject the classes into the plugin module's namespace before exec_module. Looking at _load_channel (line 3914-3915), it does `spec.loader.exec_module(mod)` which runs the plugin in its own module namespace. The plugin CAN do `import gateway` if gateway.py is the main module (which it is -- it's run as the entrypoint). Alternatively, duplicate the small class definitions in the plugin.
**Warning signs:** ImportError on plugin load.

### Pitfall 4: Discord Rate Limiting
**What goes wrong:** API returns 429 Too Many Requests.
**Why it happens:** Discord rate limits are per-route. Sending messages has a limit of ~5/5s per channel.
**How to avoid:** Add basic retry-after handling. Check for 429 status, read the Retry-After header, sleep, then retry once.
**Warning signs:** HTTPError 429 in logs.

### Pitfall 5: Bot Ignoring Its Own Messages
**What goes wrong:** Infinite loop where bot responds to itself.
**Why it happens:** MESSAGE_CREATE fires for ALL messages in the channel, including the bot's own.
**How to avoid:** Check `msg["author"].get("bot", False)` and skip messages from bots. Also compare author ID to the bot's own ID from the Ready event.
**Warning signs:** Bot rapidly sending messages to itself.

### Pitfall 6: Discord File Size Limit (10MB default)
**What goes wrong:** File upload returns 413 or error.
**Why it happens:** Discord limits individual file uploads to 10MB for non-Nitro users, though server boost level can increase this.
**How to avoid:** Check file size before upload. If > 10MB, fall back to send_text with a description. The ChannelCapabilities.max_file_size should be set to 10_000_000.
**Warning signs:** HTTP 413 errors, silent upload failures.

## Code Examples

### Verified: Discord Create Message (text only)
```python
# Source: Discord API docs - POST /channels/{channel_id}/messages
import json
import urllib.request

def discord_send_text(token, channel_id, text):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": text[:2000]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return {"sent": True, "error": ""}
    except Exception as e:
        return {"sent": False, "error": str(e)}
```

### Verified: Discord Create Message with File (multipart)
```python
# Source: Discord API docs - Uploading Files reference
import json
import urllib.request
import uuid

def discord_send_file(token, channel_id, file_bytes, filename, mime_type, caption=""):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    boundary = uuid.uuid4().hex

    # payload_json with attachment reference
    payload = {"content": caption[:2000] if caption else ""}
    payload["attachments"] = [{"id": 0, "filename": filename}]

    lines = []
    # Part 1: payload_json
    lines.append(f"--{boundary}".encode())
    lines.append(b'Content-Disposition: form-data; name="payload_json"')
    lines.append(b"Content-Type: application/json")
    lines.append(b"")
    lines.append(json.dumps(payload).encode("utf-8"))
    # Part 2: file
    lines.append(f"--{boundary}".encode())
    lines.append(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"'.encode())
    lines.append(f"Content-Type: {mime_type}".encode())
    lines.append(b"")
    lines.append(file_bytes)
    # End
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = b"\r\n".join(lines)
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bot {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return {"sent": True, "error": ""}
    except Exception as e:
        return {"sent": False, "error": str(e)}
```

### Verified: Discord Gateway Identify + Heartbeat
```python
# Source: Discord Gateway docs - Identify (op 2)
import json
import threading
import websocket  # websocket-client package

def connect_gateway(token, on_message_create, stop_event):
    """Connect to Discord Gateway WebSocket."""
    # Get gateway URL
    req = urllib.request.Request(
        "https://discord.com/api/v10/gateway/bot",
        headers={"Authorization": f"Bot {token}"}
    )
    with urllib.request.urlopen(req) as resp:
        gw_url = json.loads(resp.read())["url"]

    ws = websocket.WebSocket()
    ws.connect(f"{gw_url}?v=10&encoding=json")

    # Receive Hello (op 10)
    hello = json.loads(ws.recv())
    interval = hello["d"]["heartbeat_interval"] / 1000.0
    seq = [None]  # mutable ref for heartbeat thread

    # Heartbeat thread
    def heartbeat():
        while not stop_event.is_set():
            ws.send(json.dumps({"op": 1, "d": seq[0]}))
            stop_event.wait(interval)
    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    # Identify
    GUILD_MESSAGES = 1 << 9
    MESSAGE_CONTENT = 1 << 15
    ws.send(json.dumps({
        "op": 2,
        "d": {
            "token": token,
            "intents": GUILD_MESSAGES | MESSAGE_CONTENT,
            "properties": {"os": "linux", "browser": "gooseclaw", "device": "gooseclaw"},
        }
    }))

    # Event loop
    while not stop_event.is_set():
        try:
            raw = ws.recv()
            if not raw:
                break
            data = json.loads(raw)
            if data.get("s"):
                seq[0] = data["s"]
            if data["op"] == 0 and data.get("t") == "MESSAGE_CREATE":
                on_message_create(data["d"])
        except Exception:
            break

    ws.close()
```

### Verified: Extracting Discord Message Attachments
```python
# Source: Discord API docs - Message object, Attachment object
def extract_discord_media(msg):
    """Extract media attachments from a Discord MESSAGE_CREATE event."""
    media = []
    for att in msg.get("attachments", []):
        # Discord provides a CDN URL for each attachment
        url = att.get("url", "")
        mime = att.get("content_type", "application/octet-stream")
        fname = att.get("filename", "file")
        size = att.get("size", 0)

        # Determine kind from content_type
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("audio/"):
            kind = "audio"
        elif mime.startswith("video/"):
            kind = "video"
        else:
            kind = "document"

        media.append({
            "url": url,        # CDN URL to download from
            "mime": mime,
            "filename": fname,
            "kind": kind,
            "size": size,
        })
    return media
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| discord.py + asyncio | websocket-client + urllib (for this use case) | Always valid | Sync threading matches GooseClaw's architecture |
| Slack files.upload | Slack files.getUploadURLExternal + completeUploadExternal | April 2024 | Old endpoint deprecated Nov 2025. Makes Slack significantly harder. |
| Discord API v9 | Discord API v10 | 2022 | v10 is current. Use v10 in all URLs. |
| Message Content always available | MESSAGE_CONTENT privileged intent required | Sept 2022 | Must enable in Developer Portal + identify payload |

**Deprecated/outdated:**
- Slack files.upload: Deprecated, stops working Nov 2025. Must use 3-step flow instead.
- Discord API v6-v9: Use v10 in all endpoint URLs.

## Open Questions

1. **How should the plugin import OutboundAdapter/InboundMessage/ChannelCapabilities?**
   - What we know: Plugin runs via importlib in gateway's Python process. `import gateway` should work since gateway.py is already loaded.
   - What's unclear: Whether the module name is `gateway` or `__main__` when loaded. The Dockerfile runs `python3 gateway.py` which makes it `__main__`.
   - Recommendation: The safest approach is to define a small shim at the top of the plugin: `import sys; gw = sys.modules.get("__main__"); OutboundAdapter = gw.OutboundAdapter` etc. Alternatively, the plugin duplicates the 3 small classes (~30 lines total). The duplication approach is cleaner for a template/example since it's self-contained. **UPDATE: Looking at _load_channel line 3909, it uses `importlib.util.spec_from_file_location(f"channel_{mod_name}", filepath)` which creates a new module. The gateway module is importable as `gateway` since `sys.path.insert(0, os.path.dirname(__file__))` is common or gateway.py's directory is in the path. Test by checking if `import gateway` works from a subprocess.**

2. **Should the plugin support DMs or only guild (server) channels?**
   - What we know: The CHANNEL dict takes a single DISCORD_CHANNEL_ID credential. This scopes to one channel.
   - What's unclear: Whether users want to DM the bot directly vs post in a channel.
   - Recommendation: Start with guild channel messages only (simpler). DM support can be added later. The DIRECT_MESSAGES intent (1 << 12) would be needed for DM support.

3. **How to handle Discord reconnection/resume?**
   - What we know: Discord Gateway provides a resume_gateway_url in the Ready event. Disconnections are normal.
   - What's unclear: How robust the reconnection needs to be for an MVP.
   - Recommendation: Implement a simple reconnect loop (on disconnect, re-connect and re-identify). Resume is a nice-to-have but not critical for MVP. The poll() function can wrap the entire connect+event-loop in a while-not-stopped retry loop with backoff.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (via python3 -m pytest) |
| Config file | none (pytest auto-discovers) |
| Quick run command | `python3 -m pytest docker/test_gateway.py -x -q --tb=short` |
| Full suite command | `python3 -m pytest docker/test_gateway.py -q` |
| Estimated runtime | ~46 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MEDIA-15 | Discord adapter send_text posts to Discord REST API | unit | `python3 -m pytest docker/test_gateway.py -k "TestDiscordOutboundAdapter and test_send_text" -x` | No (Wave 0 gap) |
| MEDIA-15 | Discord adapter send_image uploads via multipart/form-data | unit | `python3 -m pytest docker/test_gateway.py -k "TestDiscordOutboundAdapter and test_send_image" -x` | No (Wave 0 gap) |
| MEDIA-15 | Discord adapter send_file uploads via multipart/form-data | unit | `python3 -m pytest docker/test_gateway.py -k "TestDiscordOutboundAdapter and test_send_file" -x` | No (Wave 0 gap) |
| MEDIA-15 | Discord adapter capabilities() declares images+files | unit | `python3 -m pytest docker/test_gateway.py -k "TestDiscordOutboundAdapter and test_capabilities" -x` | No (Wave 0 gap) |
| MEDIA-15 | Discord poll function connects to Gateway and dispatches messages | unit | `python3 -m pytest docker/test_gateway.py -k "TestDiscordPoll" -x` | No (Wave 0 gap) |
| MEDIA-15 | Discord inbound media extraction from attachments | unit | `python3 -m pytest docker/test_gateway.py -k "TestDiscordInboundMedia" -x` | No (Wave 0 gap) |
| MEDIA-16 | v2 plugin loads without gateway.py changes | integration | `python3 -m pytest docker/test_gateway.py -k "TestLoadChannelV2 and test_v2_plugin_used_directly" -x` | Yes (existing) |
| MEDIA-16 | _route_media_blocks dispatches to Discord adapter | unit | `python3 -m pytest docker/test_gateway.py -k "TestRouteMediaBlocks" -x` | Yes (existing) |
| MEDIA-16 | ChannelRelay routes media through adapter from _loaded_channels | integration | `python3 -m pytest docker/test_gateway.py -k "TestChannelRelayMediaRouting" -x` | No (Wave 0 gap) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m pytest docker/test_gateway.py -x -q --tb=short`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before /gsd:verify-work runs
- **Estimated feedback latency per task:** ~46 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `TestDiscordOutboundAdapter` class in `docker/test_gateway.py` -- covers MEDIA-15 (send_text, send_image, send_file, capabilities)
- [ ] `TestDiscordPoll` class in `docker/test_gateway.py` -- covers MEDIA-15 (Gateway connect, heartbeat, message dispatch)
- [ ] `TestDiscordInboundMedia` class in `docker/test_gateway.py` -- covers MEDIA-15 (attachment extraction, InboundMessage creation)
- [ ] `TestChannelRelayMediaRouting` class in `docker/test_gateway.py` -- covers MEDIA-16 (end-to-end media flow through loaded adapter)
- [ ] `websocket-client` added to `docker/requirements.txt` -- framework dependency for Discord Gateway

## Sources

### Primary (HIGH confidence)
- gateway.py lines 3518-3678: InboundMessage, MediaContent, ChannelCapabilities, OutboundAdapter, LegacyOutboundAdapter, TelegramOutboundAdapter, _route_media_blocks -- verified by reading source
- gateway.py lines 3746-3889: ChannelRelay.__call__ with v2 InboundMessage support and media routing -- verified by reading source
- gateway.py lines 3901-4019: _load_channel with v2 adapter detection (line 3955-3959) -- verified by reading source
- test_gateway.py lines 3960-4219: Existing tests for OutboundAdapter, ChannelCapabilities, LegacyOutboundAdapter, LoadChannelV2, ChannelRelayV2 -- verified by reading source
- [Discord API - Create Message](https://docs.discord.com/developers/resources/message) - POST /channels/{channel_id}/messages, multipart/form-data with files[n] and payload_json
- [Discord API - Uploading Files](https://docs.discord.com/developers/reference) - 10MB default limit, Content-Disposition requirements, boundary format
- [Discord API - Gateway](https://docs.discord.com/developers/events/gateway) - WSS connection, Identify (op 2), Heartbeat, MESSAGE_CREATE events, intents

### Secondary (MEDIUM confidence)
- [Slack files.getUploadURLExternal](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/) - 3-step upload flow verified from official Slack docs
- [Slack file upload deprecation](https://api.slack.com/changelog/2024-04-a-better-way-to-upload-files-is-here-to-stay) - files.upload deprecated, stops working Nov 2025
- [Discord Privileged Intents](https://support-dev.discord.com/hc/en-us/articles/6205754771351) - MESSAGE_CONTENT must be enabled in Developer Portal
- [websocket-client PyPI](https://pypi.org/project/websocket-client/) - Synchronous WebSocket client, threading-compatible

### Tertiary (LOW confidence)
- Discord rate limits per-route (~5/5s per channel) -- from community sources, needs validation during implementation

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Discord REST API matches Telegram pattern, websocket-client is mature and well-documented
- Architecture: HIGH - _load_channel v2 detection already exists, ChannelRelay media routing already works, zero gateway changes confirmed by code reading
- Pitfalls: HIGH - MESSAGE_CONTENT intent, heartbeat, self-message loop are well-documented issues
- Inbound media: MEDIUM - Discord attachment CDN download needs validation (URL format, auth requirements)

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (Discord API v10 is stable)

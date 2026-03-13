# Phase 14: Outbound Rich Media - Research

**Researched:** 2026-03-13
**Domain:** Telegram Bot API file sending, outbound media routing, adapter pattern, notification bus media support
**Confidence:** HIGH

## Summary

Phase 14 closes the outbound leg of the media pipeline. Phase 13 already returns `media_blocks` from `_relay_to_goose_web` as the third tuple element. Both `BotInstance._do_message_relay` and `ChannelRelay.__call__` receive these blocks but currently discard them. The job is to: (1) create a `TelegramOutboundAdapter` that actually sends images/voice/files via Telegram's sendPhoto/sendVoice/sendDocument APIs, (2) wire `_do_message_relay` to route media_blocks through the adapter after sending text, (3) wire `ChannelRelay.__call__` to return media_blocks alongside text so channel plugins can handle them, and (4) extend `notify_all` to accept optional media attachments.

The media_blocks from goose responses are dicts like `{"type":"image","data":"<base64>","mimeType":"image/jpeg"}`. These need to be decoded from base64 to bytes and sent via Telegram's multipart/form-data upload API. The Telegram Bot API does NOT accept base64 directly. Files must be uploaded as binary in multipart form-data. Since the project is stdlib-only (no requests, no pip packages), multipart form-data must be constructed manually using Python's stdlib. This is a well-understood pattern using uuid-based boundaries and manual byte concatenation.

The OutboundAdapter base class already provides graceful degradation. If a channel's adapter doesn't override `send_image`, it falls back to `send_text` with the URL as text. This means channels that don't support media (legacy plugins using LegacyOutboundAdapter) automatically get the text fallback with zero changes.

**Primary recommendation:** Build `TelegramOutboundAdapter(OutboundAdapter)` with real sendPhoto/sendVoice/sendDocument implementations using stdlib multipart form-data. Wire `_do_message_relay` to iterate media_blocks and call the adapter after text delivery. Extend `ChannelRelay.__call__` to capture and return media_blocks. Extend `notify_all` to accept an optional `media` parameter.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MEDIA-13 | Telegram adapter implements send_image (sendPhoto), send_voice (sendVoice), send_file (sendDocument) | Telegram Bot API supports multipart/form-data upload for all three. Stdlib-only construction is well-documented. OutboundAdapter base class defines the interface. |
| MEDIA-14 | notify_all supports media attachments alongside text | Current notify_all signature is `notify_all(text, channel=None)`. Extend to `notify_all(text, channel=None, media=None)`. Notification handlers need an optional media parameter. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.x | Everything | Project constraint: no pip packages |

### Key Stdlib Modules Used
| Module | Purpose | Already Imported |
|--------|---------|-----------------|
| `urllib.request` | HTTP POST to Telegram API (sendPhoto, sendVoice, sendDocument) | Yes |
| `uuid` | Generate multipart boundary strings | Yes |
| `base64` | Decode base64 image data from goose response blocks | Yes |
| `mimetypes` | Guess file extensions from MIME types | Yes |
| `json` | Parse Telegram API responses | Yes |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Manual multipart construction | `email.mime.multipart` | email.mime is designed for email, not HTTP multipart. Manual construction is simpler and more predictable for file upload. Use manual. |
| Sending base64 to Telegram | Decode to bytes + multipart upload | Telegram API does NOT accept base64. Must decode first. No choice here. |
| URL passthrough for goose images | Always upload as bytes | Goose returns base64 data, not URLs. Must upload as bytes. If a URL variant appears later, could pass through, but the current contract is base64 data. |

## Architecture Patterns

### Media Block Format (from goose responses)

Goose response media blocks are dicts extracted by `_extract_response_content`:

```python
# Image block (most common from goose tool responses like screenshots)
{"type": "image", "data": "<base64-encoded-bytes>", "mimeType": "image/png"}

# These come from toolResponse nested content too:
{"type": "image", "data": "<base64-encoded-bytes>", "mimeType": "image/jpeg"}
```

Key facts:
- `data` is base64-encoded string (not raw bytes)
- `mimeType` is the MIME type string
- `type` is always `"image"` for now (goose doesn't produce audio/video output blocks yet)
- No `url` field. Always base64 inline data.

### Pattern 1: Multipart Form-Data Construction (stdlib)

**What:** Build multipart/form-data body for Telegram file upload APIs using only stdlib.
**When to use:** Every sendPhoto/sendVoice/sendDocument call that uploads binary data.
**Example:**

```python
def _build_multipart(fields, files):
    """Build a multipart/form-data body from fields and files.

    fields: dict of {name: value} for text fields
    files: list of (field_name, filename, content_type, data_bytes)

    Returns (body_bytes, content_type_header).
    """
    boundary = uuid.uuid4().hex
    parts = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"\r\n"
            f"{value}\r\n"
        )

    body = "".join(parts).encode("utf-8")

    for field_name, filename, content_type, data in files:
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n"
            f"\r\n"
        ).encode("utf-8")
        body += header + data + b"\r\n"

    body += f"--{boundary}--\r\n".encode("utf-8")
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type
```

### Pattern 2: TelegramOutboundAdapter

**What:** Concrete OutboundAdapter subclass that sends media via Telegram Bot API.
**When to use:** Every telegram bot instance should use this adapter for outbound media.

```python
class TelegramOutboundAdapter(OutboundAdapter):
    """Sends text and media to a specific Telegram chat via Bot API."""

    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def capabilities(self):
        return ChannelCapabilities(
            supports_images=True,
            supports_voice=True,
            supports_files=True,
            max_file_size=50_000_000,  # 50MB telegram limit
            max_text_length=4096,
        )

    def send_text(self, text):
        ok, err = send_telegram_message(self.bot_token, self.chat_id, text)
        return {"sent": ok, "error": err or ""}

    def send_image(self, image_bytes, caption="", mime_type="image/png"):
        """Send image via Telegram sendPhoto. image_bytes is raw bytes."""
        return self._send_media("sendPhoto", "photo", image_bytes,
                               _ext_from_mime(mime_type), mime_type, caption)

    def send_voice(self, audio_bytes, caption="", mime_type="audio/ogg"):
        """Send voice via Telegram sendVoice. audio_bytes is raw bytes."""
        return self._send_media("sendVoice", "voice", audio_bytes,
                               _ext_from_mime(mime_type), mime_type, caption)

    def send_file(self, file_bytes, filename="file", mime_type="application/octet-stream"):
        """Send file via Telegram sendDocument. file_bytes is raw bytes."""
        return self._send_media("sendDocument", "document", file_bytes,
                               filename, mime_type, "")

    def _send_media(self, method, field, data, filename, mime_type, caption):
        """Generic Telegram media upload via multipart/form-data."""
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        fields = {"chat_id": str(self.chat_id)}
        if caption:
            fields["caption"] = caption[:1024]  # Telegram caption limit
        files = [(field, filename, mime_type, data)]
        body, content_type = _build_multipart(fields, files)
        try:
            req = urllib.request.Request(url, data=body,
                                        headers={"Content-Type": content_type})
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return {"sent": result.get("ok", False), "error": ""}
        except Exception as e:
            return {"sent": False, "error": str(e)}
```

### Pattern 3: Media Block Routing in _do_message_relay

**What:** After delivering text response, iterate media_blocks and send each via adapter.
**When to use:** In `BotInstance._do_message_relay` after text delivery completes.

```python
# After text is sent, route media blocks
if media and not _cancelled.is_set():
    adapter = TelegramOutboundAdapter(bot_token, chat_id)
    for block in media:
        if block.get("type") == "image":
            raw_bytes = base64.b64decode(block["data"])
            mime = block.get("mimeType", "image/png")
            adapter.send_image(raw_bytes, mime_type=mime)
        # future: handle audio, video blocks when goose produces them
```

### Pattern 4: ChannelRelay Media Return

**What:** ChannelRelay.__call__ currently returns only text string. Extend to also pass media_blocks to the channel plugin.
**When to use:** For channel plugins that need to handle media from goose responses.

The current ChannelRelay uses `*_` to discard media_blocks. The change captures them and makes them available. However, channel plugins currently only receive text from relay. The cleanest approach: store media_blocks on the relay instance for the plugin to query after relay returns, or extend the relay call signature.

Recommended approach: ChannelRelay stores the last media_blocks, and the relay function returns a tuple `(text, media_blocks)` when the caller is a v2 plugin. For backward compat, legacy callers still get text string only.

### Pattern 5: notify_all Media Extension

**What:** Add optional `media` parameter to notify_all.
**When to use:** When API or cron wants to send media alongside notification text.

```python
def notify_all(text, channel=None, media=None):
    """media: list of dicts with type/data/mimeType, or None."""
    # ... existing handler iteration ...
    # pass media to handler if it accepts it
    result = target["handler"](text, media=media)
```

The notification handler signature changes from `handler(text)` to `handler(text, media=None)`. For backward compat, catch TypeError and retry without media if old-style handlers don't accept the kwarg.

### Anti-Patterns to Avoid
- **Encoding base64 AGAIN before sending to Telegram:** Goose gives base64, Telegram wants raw bytes. Decode once, send as multipart binary.
- **Using URL passthrough for goose images:** Goose image data is inline base64, not URLs. There's no URL to pass through.
- **Building a separate media send path in the poll loop:** Use the OutboundAdapter pattern. Don't add standalone `send_telegram_photo()` functions. The adapter abstraction is the whole point of Phase 11.
- **Breaking notify handler backward compatibility:** Old handlers only accept `(text)`. New handlers accept `(text, media=None)`. Must handle both gracefully.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| MIME to file extension mapping | Custom dict | `mimetypes.guess_extension()` | Stdlib handles edge cases. Supplement with a small fallback map for common types (image/jpeg -> .jpg) since guess_extension can return None. |
| Multipart form-data encoding | `email.mime` or `http.client` body builder | Manual boundary-based construction | It's 20 lines. email.mime adds overhead and is for email, not HTTP uploads. Manual is cleaner for this use case. |
| Media type detection from blocks | Complex type inference | Simple `block["type"]` + `block["mimeType"]` check | The goose response blocks already have explicit type and mimeType fields. |

## Common Pitfalls

### Pitfall 1: Telegram sendPhoto File Size Limit
**What goes wrong:** sendPhoto has a 10MB limit for photos. Larger images fail silently or with unhelpful errors.
**Why it happens:** Goose tool screenshots (especially full-page screenshots from browser tools) can exceed 10MB.
**How to avoid:** Check image bytes size before sending. If > 10MB, fall back to sendDocument (50MB limit) or compress.
**Warning signs:** HTTP 400 from Telegram API with "photo is too big" or "Bad Request".

### Pitfall 2: sendVoice Requires audio/ogg with Opus Codec
**What goes wrong:** sendVoice fails if the audio is not in OGG Opus format.
**Why it happens:** Telegram enforces this format for the sendVoice method specifically.
**How to avoid:** For non-OGG audio, use sendDocument instead of sendVoice. Or sendAudio for regular audio files. Since goose doesn't currently produce audio output, this is future-proofing.
**Warning signs:** "Bad Request: VOICE_MESSAGES_FORBIDDEN" or "wrong file type" errors.

### Pitfall 3: Caption Length Limit
**What goes wrong:** Telegram captions are limited to 1024 characters. Longer captions get rejected.
**Why it happens:** Sending response text as caption when it should be a separate message.
**How to avoid:** Truncate captions to 1024 chars. If the text response is longer, send as separate text message first, then the media.
**Warning signs:** HTTP 400 with "caption is too long".

### Pitfall 4: Multipart Boundary in Binary Data
**What goes wrong:** If the boundary string appears in the file data, the multipart body is malformed.
**Why it happens:** Extremely unlikely with UUID-based boundaries, but possible.
**How to avoid:** Use `uuid.uuid4().hex` for boundary. 32 hex chars makes collision statistically impossible.
**Warning signs:** Telegram returns parse errors or "bad request".

### Pitfall 5: notify_all Handler Backward Compatibility
**What goes wrong:** Adding `media` parameter to handler signature breaks existing handlers that only accept `(text)`.
**Why it happens:** Channel plugins register handlers with `handler(text)` signature. Telegram's `_make_notify_handler` also uses `handler(text)`.
**How to avoid:** Use `**kwargs` or try/except TypeError. Recommended: handlers accept `(text, **kwargs)` and extract media from kwargs. For backward compat, wrap calls in try/except.
**Warning signs:** TypeError in notification delivery.

### Pitfall 6: ChannelRelay Return Type Change
**What goes wrong:** Changing ChannelRelay.__call__ return type from str to tuple breaks all channel plugins.
**Why it happens:** Channel plugins poll loop calls `relay(msg)` and expects a string back.
**How to avoid:** Keep returning string for backward compat. Store media_blocks as instance state that plugins can optionally query, OR add an `on_media` callback that plugins can register.
**Warning signs:** Channel plugins crash with "cannot iterate over str" or similar.

## Code Examples

### Example 1: Multipart Form-Data Helper (verified stdlib pattern)

```python
# Source: Python stdlib documentation + Telegram Bot API docs
import uuid
import urllib.request

def _build_multipart(fields, files):
    """Build multipart/form-data body for Telegram API uploads.

    fields: dict of {name: value} for text form fields
    files: list of (field_name, filename, content_type, data_bytes)
    Returns: (body_bytes, content_type_header_value)
    """
    boundary = uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))
    for field_name, filename, content_type, data in files:
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"'.encode()
        )
        lines.append(f"Content-Type: {content_type}".encode())
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"
```

### Example 2: Telegram sendPhoto with Bytes

```python
# Source: Telegram Bot API https://core.telegram.org/bots/api#sendphoto
def _send_telegram_photo(bot_token, chat_id, image_bytes, mime_type="image/png", caption=""):
    """Upload a photo to Telegram via sendPhoto multipart/form-data."""
    ext = mimetypes.guess_extension(mime_type) or ".png"
    filename = f"image{ext}"
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    files = [("photo", filename, mime_type, image_bytes)]
    body, content_type = _build_multipart(fields, files)
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())
```

### Example 3: Media Block Routing

```python
# Source: existing _extract_response_content + goose response format
def _route_media_blocks(media_blocks, adapter):
    """Route goose response media blocks to the appropriate adapter methods.

    media_blocks: list of dicts from _extract_response_content
    adapter: OutboundAdapter instance
    """
    for block in media_blocks:
        btype = block.get("type", "")
        if btype == "image":
            data_b64 = block.get("data", "")
            if not data_b64:
                continue
            raw_bytes = base64.b64decode(data_b64)
            mime = block.get("mimeType", "image/png")
            # sendPhoto limit is 10MB, fall back to sendDocument for large images
            if len(raw_bytes) > 10_000_000:
                ext = mimetypes.guess_extension(mime) or ".png"
                adapter.send_file(raw_bytes, filename=f"image{ext}", mime_type=mime)
            else:
                adapter.send_image(raw_bytes, mime_type=mime)
        # future: elif btype == "audio": adapter.send_voice(...)
```

### Example 4: MIME Extension Helper

```python
# mimetypes.guess_extension can return weird results. Supplement with known mappings.
_MIME_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
}

def _ext_from_mime(mime_type):
    """Get file extension from MIME type. Falls back to stdlib."""
    return _MIME_EXT_MAP.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| media_blocks discarded with `*_` | Route through OutboundAdapter | Phase 14 (now) | Goose-generated images actually reach the user |
| `notify_all(text, channel)` | `notify_all(text, channel, media)` | Phase 14 (now) | Notifications can include images/files |
| `send_telegram_message()` text-only | `TelegramOutboundAdapter` with full media | Phase 14 (now) | Telegram gets native image/voice/file delivery |
| Notification handler `(text)` signature | `(text, **kwargs)` with optional media | Phase 14 (now) | Backward-compatible media support in notification bus |

**No deprecated items:** All new code. Extends existing contracts.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (via python3 -m pytest) |
| Config file | None (discovered automatically) |
| Quick run command | `python3 -m pytest docker/test_gateway.py -x -q` |
| Full suite command | `python3 -m pytest docker/test_gateway.py -q` |
| Estimated runtime | ~45 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MEDIA-13a | TelegramOutboundAdapter.send_image calls sendPhoto with multipart body | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestTelegramOutboundAdapter and send_image"` | No, Wave 0 gap |
| MEDIA-13b | TelegramOutboundAdapter.send_voice calls sendVoice with multipart body | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestTelegramOutboundAdapter and send_voice"` | No, Wave 0 gap |
| MEDIA-13c | TelegramOutboundAdapter.send_file calls sendDocument with multipart body | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestTelegramOutboundAdapter and send_file"` | No, Wave 0 gap |
| MEDIA-13d | TelegramOutboundAdapter.capabilities declares image/voice/file support | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestTelegramOutboundAdapter and capabilities"` | No, Wave 0 gap |
| MEDIA-13e | BotInstance._do_message_relay routes media_blocks through adapter after text | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestBotMediaRouting"` | No, Wave 0 gap |
| MEDIA-13f | _build_multipart produces valid multipart/form-data body | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestBuildMultipart"` | No, Wave 0 gap |
| MEDIA-13g | _route_media_blocks routes image blocks to adapter.send_image | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestRouteMediaBlocks"` | No, Wave 0 gap |
| MEDIA-13h | Large images (>10MB) fall back to send_file | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestRouteMediaBlocks and large"` | No, Wave 0 gap |
| MEDIA-14a | notify_all accepts media parameter and passes to handlers | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestNotifyMedia"` | No, Wave 0 gap |
| MEDIA-14b | Old-style notification handlers (text-only) still work with media parameter | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestNotifyMedia and backward"` | No, Wave 0 gap |
| MEDIA-14c | ChannelRelay captures media_blocks for plugin consumption | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestChannelRelayMedia"` | No, Wave 0 gap |
| MEDIA-14d | Channels without media support get graceful text fallback | unit | `python3 -m pytest docker/test_gateway.py -x -q -k "TestGracefulMediaFallback"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m pytest docker/test_gateway.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~45 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `TestBuildMultipart` class in `docker/test_gateway.py` - tests for `_build_multipart()` helper
- [ ] `TestTelegramOutboundAdapter` class in `docker/test_gateway.py` - tests for send_image, send_voice, send_file, capabilities
- [ ] `TestRouteMediaBlocks` class in `docker/test_gateway.py` - tests for `_route_media_blocks()` dispatch logic
- [ ] `TestBotMediaRouting` class in `docker/test_gateway.py` - tests that `_do_message_relay` sends media after text
- [ ] `TestNotifyMedia` class in `docker/test_gateway.py` - tests for notify_all media parameter + backward compat
- [ ] `TestChannelRelayMedia` class in `docker/test_gateway.py` - tests that ChannelRelay captures media_blocks
- [ ] `TestGracefulMediaFallback` class in `docker/test_gateway.py` - tests that channels without media support get text fallback

## Open Questions

1. **Should TelegramOutboundAdapter be per-chat or per-bot?**
   - What we know: The adapter needs both `bot_token` and `chat_id` to send messages. Creating per-chat is most natural since each media send targets a specific chat.
   - What's unclear: Should the adapter be created once per relay call and discarded, or cached?
   - Recommendation: Create per-relay-call (in `_do_message_relay`). It's just a data holder, no expensive init. Creating per-call avoids stale state issues.

2. **Should ChannelRelay return media_blocks or use a callback?**
   - What we know: ChannelRelay.__call__ currently returns a string. Changing to tuple would break channel plugins.
   - What's unclear: Best pattern for passing media to plugins without breaking the signature.
   - Recommendation: Add an `on_media` callback parameter to ChannelRelay.__call__. Plugins that support media pass a callback. Plugins that don't, don't. No return type change needed.

3. **Does goose produce non-image media output blocks?**
   - What we know: Current goose responses only contain `{"type":"image"}` media blocks (screenshots, generated images from tools). No audio or video output blocks observed.
   - What's unclear: Whether future goose versions will produce audio/video output.
   - Recommendation: Handle `type=="image"` now. Add a simple print/log for unknown media types. Future phases can add handlers.

## Sources

### Primary (HIGH confidence)
- **Codebase inspection** - gateway.py lines 3504-3531 (OutboundAdapter), 3489-3501 (ChannelCapabilities), 263-478 (BotInstance._do_message_relay), 3609-3741 (ChannelRelay.__call__), 1264-1321 (send_telegram_message), 4911-4941 (_extract_response_content), 1406-1462 (notify_all)
- **Phase 13 RESEARCH.md** - confirmed media_blocks format and relay 3-tuple return
- [Telegram Bot API](https://core.telegram.org/bots/api) - sendPhoto, sendVoice, sendDocument parameters and limits

### Secondary (MEDIUM confidence)
- [Telegram Bot API file sending via multipart](https://community.latenode.com/t/uploading-local-images-to-telegram-using-python-bot-multipart-form-data-approach/10486) - multipart upload examples
- [Telegram Bot API sendPhoto examples](https://copyprogramming.com/howto/how-to-send-photo-by-telegram-bot-using-multipart-form-data) - confirmed multipart/form-data is required for binary upload
- [Telegram Bot API sending files guide](https://dev.to/rizkyrajitha/sending-images-and-more-with-telegram-bot-4c0h) - confirmed 10MB photo limit, 50MB document limit

### Tertiary (LOW confidence)
- None. All findings are from codebase + official docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - stdlib-only, same as every prior phase
- Architecture: HIGH - extending existing OutboundAdapter pattern established in Phase 11, wiring already half-done in Phase 13
- Pitfalls: HIGH - Telegram API limits and multipart encoding are well-documented, edge cases identified from official docs

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (stable domain, Telegram Bot API changes rarely)

# Phase 12: Inbound Media Pipeline - Research

**Researched:** 2026-03-13
**Domain:** Telegram Bot API media download, media normalization, base64 encoding for multimodal relay
**Confidence:** HIGH

## Summary

Phase 12 replaces the canned MEDIA_REPLY rejection with an actual download-and-normalize pipeline. When a user sends a photo, voice message, document, video, sticker, or audio to a Telegram bot, the gateway downloads the file via Telegram's getFile API, wraps it in a new `MediaContent` class (kind, mime_type, raw bytes, optional filename), and populates `InboundMessage.media` with real data instead of bare type stubs.

For images specifically, the raw bytes are base64-encoded and prepared as multimodal content blocks matching goose's `MessageContent::Image` format: `{"type": "image", "data": "<base64>", "mimeType": "image/jpeg"}`. This prepares the data for Phase 13's relay protocol upgrade but does NOT change the relay wire format yet. Phase 12 only builds the pipeline up to the point where `InboundMessage.media` contains downloadable, normalized `MediaContent` objects and images are base64-ready.

The legacy `_telegram_poll_loop` also needs updating to match `BotInstance._poll_loop`, but with a simpler approach since it's the backward-compat path.

**Primary recommendation:** Add `MediaContent` class next to `InboundMessage` (around line 3377), add `_download_telegram_file(bot_token, file_id)` helper near the existing `send_telegram_message` function, update `BotInstance._poll_loop` to call download before creating `InboundMessage`, and remove the MEDIA_REPLY early-return for media messages from paired users.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MEDIA-06 | Telegram adapter downloads media (photo, voice, document, video, sticker, audio) via getFile API and buffers as bytes | Telegram getFile API flow, _download_telegram_file helper design, PhotoSize selection, error handling |
| MEDIA-07 | MediaContent class normalizes media with kind (image/audio/video/document), mime_type, data (bytes), and optional filename | MediaContent class design, kind mapping, MIME type detection via mimetypes stdlib |
| MEDIA-08 | Voice messages downloaded and normalized as MediaContent(kind="audio"), no built-in STT | Voice message handling, kind="audio" normalization, ogg mime type |
| MEDIA-09 | Images base64-encoded and sent to goose as multimodal content blocks | Goose MessageContent::Image format verified from source, base64 encoding pattern, content block JSON structure |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.x | Everything | Project constraint: no pip packages |

### Key Stdlib Modules Used
| Module | Purpose | Already Imported |
|--------|---------|-----------------|
| `urllib.request` | HTTP calls for Telegram getFile + download | Yes (line 58) |
| `base64` | Base64 encoding for image content blocks | Yes (line 38) |
| `mimetypes` | MIME type guessing from file_path | No, needs import |
| `json` | Serialization | Yes (line 45) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `mimetypes` | Manual MIME map | mimetypes is stdlib, handles edge cases, extensible. Manual map is simpler but incomplete. Use mimetypes with a fallback map for Telegram-specific types. |
| buffering full file in memory | streaming to temp file | Files are max 20MB (Telegram limit). Buffering is fine for this scale. Temp files add complexity and cleanup concerns. |

## Architecture Patterns

### Where to Place New Types and Functions

```
gateway.py additions:
  line ~38:     import mimetypes          # new import
  line ~3377:   class MediaContent: ...   # after InboundMessage, before ChannelCapabilities
  line ~1170:   _download_telegram_file() # near existing send_telegram_message and _has_media
```

### Pattern 1: MediaContent Class

**What:** A plain class that normalizes downloaded media into a channel-agnostic format with actual bytes.

**Design:**
```python
class MediaContent:
    """Normalized media attachment with actual data.

    kind: "image", "audio", "video", "document"
    mime_type: MIME type string, e.g. "image/jpeg"
    data: raw bytes of the file
    filename: optional original filename
    """
    def __init__(self, kind, mime_type, data, filename=None):
        self.kind = kind           # "image" | "audio" | "video" | "document"
        self.mime_type = mime_type  # e.g. "image/jpeg"
        self.data = data           # bytes
        self.filename = filename   # optional str

    @property
    def size(self):
        return len(self.data) if self.data else 0

    def to_base64(self):
        """Return base64-encoded string of data."""
        return base64.b64encode(self.data).decode("ascii") if self.data else ""

    def to_content_block(self):
        """Return goose-compatible content block dict for images.

        Format matches goose MessageContent::Image:
        {"type": "image", "data": "<base64>", "mimeType": "<mime>"}
        """
        if self.kind == "image":
            return {
                "type": "image",
                "data": self.to_base64(),
                "mimeType": self.mime_type,
            }
        return None
```

**Why these fields:**
- `kind`: Coarse category for routing decisions. Maps Telegram's 8 media types to 4 kinds: photo/sticker/animation -> "image", voice/audio -> "audio", video/video_note -> "video", document -> "document".
- `mime_type`: Required for content block headers and for goose to know the format.
- `data`: Raw bytes. Kept as bytes (not base64) because only images need base64. Other types may be forwarded differently.
- `filename`: Telegram documents have filenames. Photos don't. Optional.

### Pattern 2: Telegram getFile Download Flow

**What:** Two-step process to download files from Telegram.

**Step 1: getFile API call**
```
GET https://api.telegram.org/bot{token}/getFile?file_id={file_id}
Response: {"ok": true, "result": {"file_id": "...", "file_path": "photos/file_1.jpg", "file_size": 12345}}
```

**Step 2: Download the actual file**
```
GET https://api.telegram.org/file/bot{token}/{file_path}
Response: raw bytes
```

**Design:**
```python
def _download_telegram_file(bot_token, file_id, timeout=15):
    """Download a file from Telegram via getFile API.

    Returns (bytes, file_path) or (None, error_string).
    file_path is the Telegram-relative path, useful for MIME guessing.
    """
    # Step 1: get file info
    url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={urllib.parse.quote(file_id)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            return None, f"getFile failed: {data}"
        file_path = data["result"].get("file_path", "")
        if not file_path:
            return None, "getFile returned no file_path"
    except Exception as e:
        return None, f"getFile error: {e}"

    # Step 2: download file bytes
    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    try:
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            file_bytes = resp.read()
        return file_bytes, file_path
    except Exception as e:
        return None, f"download error: {e}"
```

### Pattern 3: PhotoSize Selection (Pick Largest)

**What:** Telegram sends photos as an array of PhotoSize objects at different resolutions. Always pick the last element (largest resolution).

```python
# Telegram photo array is sorted ascending by resolution
# Last element = highest resolution
photo_sizes = msg["photo"]  # list of {"file_id": "...", "width": N, "height": N, "file_size": N}
best = photo_sizes[-1]
file_id = best["file_id"]
```

**Confidence:** HIGH. This is documented behavior in the Telegram Bot API and confirmed by multiple sources. The array is always sorted by ascending resolution.

### Pattern 4: Kind and MIME Mapping

**What:** Map Telegram media keys to MediaContent kinds and infer MIME types.

```python
_TELEGRAM_KIND_MAP = {
    "photo": "image",
    "sticker": "image",
    "animation": "image",
    "voice": "audio",
    "audio": "audio",
    "video": "video",
    "video_note": "video",
    "document": "document",
}

_TELEGRAM_MIME_FALLBACK = {
    "photo": "image/jpeg",       # Telegram always serves photos as JPEG
    "sticker": "image/webp",     # Telegram stickers are WebP
    "animation": "video/mp4",    # GIF animations served as MP4
    "voice": "audio/ogg",        # Voice messages are Ogg Opus
    "audio": "audio/mpeg",       # Audio files default to MP3
    "video": "video/mp4",        # Videos default to MP4
    "video_note": "video/mp4",   # Round videos are MP4
    "document": "application/octet-stream",
}
```

**MIME type resolution priority:**
1. Telegram message object's `mime_type` field (when present, e.g., documents always have it)
2. `mimetypes.guess_type(file_path)` from the getFile response path
3. Fallback map above

### Pattern 5: Goose Content Block Format (MEDIA-09)

**What:** Goose's `MessageContent` enum uses `#[serde(tag = "type", rename_all = "camelCase")]`. The Image variant wraps `ImageContent` with `data` (base64 string) and `mime_type` (camelCase `mimeType` in JSON).

**Verified from goose source** (`crates/goose/src/conversation/message.rs`):
```rust
#[serde(tag = "type", rename_all = "camelCase")]
pub enum MessageContent {
    Text(TextContent),
    Image(ImageContent),
    // ... other variants
}
```

**JSON format for image content block:**
```json
{
    "type": "image",
    "data": "<base64-encoded-image-bytes>",
    "mimeType": "image/jpeg"
}
```

**JSON format for text content block:**
```json
{
    "type": "text",
    "text": "user message here"
}
```

**Current relay sends:**
```json
{
    "type": "message",
    "content": "plain text string",
    "session_id": "...",
    "timestamp": 123
}
```

**Phase 12 prepares** the content blocks but does NOT change the relay wire format. That's Phase 13 (MEDIA-10). Phase 12's job is to have `MediaContent.to_content_block()` return the correct dict so Phase 13 can just serialize it.

### Pattern 6: Poll Loop Evolution

**Current flow (lines 491-523):**
```
1. Detect media via _has_media(msg)
2. Build media_list with bare type stubs: [{"type": "image"}]
3. If media-only (no text): send MEDIA_REPLY, pass empty inbound_msg to relay
4. If text+media: relay text only, media info attached but unused
```

**Phase 12 flow:**
```
1. Detect media via _has_media(msg)
2. For each media key in message:
   a. Extract file_id (handle photo array: pick last)
   b. Call _download_telegram_file(token, file_id)
   c. Determine kind and mime_type
   d. Create MediaContent(kind, mime_type, data, filename)
3. Build InboundMessage with media=[MediaContent, ...]
4. If media-only: still relay (no more MEDIA_REPLY), text="" is fine
5. If text+media: relay normally
```

**Key change:** Remove the MEDIA_REPLY early-return. Media messages from paired users now flow through to the relay like text messages. The relay itself doesn't use the media yet (Phase 13), but the InboundMessage is fully populated.

### Anti-Patterns to Avoid

- **Downloading in the main poll loop thread:** Downloads can take seconds. The poll loop must stay responsive for /stop commands. Downloads should happen in the relay thread (inside `_do_message_relay` or just before the relay call in the threaded handler).
- **Blocking on large files:** Telegram allows up to 20MB for bot downloads. Use a timeout (30s) on the download request. Don't retry failed downloads, just log and proceed without media.
- **Modifying `_do_ws_relay` or the WebSocket message format:** That's Phase 13. Phase 12 only populates `InboundMessage.media` with `MediaContent` objects.
- **Adding STT for voice messages:** MEDIA-08 explicitly says no built-in STT. Voice is just `MediaContent(kind="audio")`.
- **Storing media on disk:** Buffer in memory only. Files are ephemeral, used once for relay, then garbage collected.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| MIME type guessing | Custom extension-to-MIME map | `mimetypes.guess_type()` + fallback map | stdlib handles hundreds of types. Only need fallback for Telegram-specific defaults. |
| Base64 encoding | Custom encoder | `base64.b64encode()` | Already imported (line 38). Standard, fast, correct. |
| File ID extraction from photo array | Complex selection logic | `msg["photo"][-1]["file_id"]` | Telegram guarantees ascending sort. Last = best. |
| HTTP download | Custom socket code | `urllib.request.urlopen()` | Already used throughout gateway.py. Handles redirects, timeouts, etc. |

## Common Pitfalls

### Pitfall 1: Blocking the Poll Loop with Downloads
**What goes wrong:** Downloading a 10MB video in the poll loop blocks all message processing for that bot. /stop commands don't work during the download.
**Why it happens:** Natural impulse to download media where it's detected.
**How to avoid:** Download media in the relay thread, not in the poll loop. The poll loop builds InboundMessage with file_id references. The relay thread (or a pre-relay step) does the actual download.
**Warning signs:** Poll loop has urllib.request.urlopen calls for file downloads.

**Alternative approach:** Download in the poll loop but in a separate thread before spawning the relay. This is simpler and matches the current pattern where the relay is already threaded.

**Recommended approach:** Download in the relay thread, inside `_do_message_relay`. Add `file_id` and `media_key` to the InboundMessage.media dicts from the poll loop, then download + create MediaContent at the start of _do_message_relay. This keeps the poll loop fast and the download close to where the data is needed.

### Pitfall 2: Photo Array Empty or Malformed
**What goes wrong:** `msg["photo"][-1]` throws IndexError if the photo array is somehow empty.
**Why it happens:** Defensive programming oversight.
**How to avoid:** Check `if msg.get("photo")` before indexing. Telegram always sends at least one PhotoSize, but be defensive.
**Warning signs:** Uncaught IndexError in production logs.

### Pitfall 3: getFile Returns No file_path
**What goes wrong:** Very large files (near 20MB limit) or server errors can cause getFile to succeed but return no file_path.
**Why it happens:** Telegram server-side issues, file too large for bot download.
**How to avoid:** Check `file_path` in the getFile response. If missing, log warning and skip this media attachment. Don't crash the relay.

### Pitfall 4: Forgetting to Update Legacy _telegram_poll_loop
**What goes wrong:** Multi-bot instances get media downloads but the legacy single-bot path still sends MEDIA_REPLY.
**Why it happens:** Two separate poll loops exist in the codebase.
**How to avoid:** Update `_telegram_poll_loop` (line 5204) to match BotInstance._poll_loop. Or, since the legacy loop is already a backward-compat shim, add minimal media handling there too.
**Warning signs:** Legacy single-bot users report media still gets rejected.

### Pitfall 5: Memory Pressure from Large Media
**What goes wrong:** Multiple users sending 20MB videos simultaneously could use 100MB+ of memory.
**Why it happens:** Buffering full files as bytes in InboundMessage.
**How to avoid:** Not a real concern at GooseClaw scale (1-5 bots, handful of users). But if it were: limit to one concurrent download per bot, or skip files over a configurable threshold. For now, the 20MB Telegram limit is a natural cap.

### Pitfall 6: MIME Type Wrong for Stickers
**What goes wrong:** Stickers can be static WebP, animated TGS (gzip Lottie), or video WebM. Treating all as "image/webp" is wrong for animated stickers.
**Why it happens:** Sticker format depends on `is_animated` and `is_video` fields in the Sticker object.
**How to avoid:** Check sticker type: `is_video` -> "video/webm", `is_animated` -> skip (TGS format is proprietary Lottie, not useful for goose), regular -> "image/webp". Or simpler: use `mime_type` from the sticker object if present, fall back to guessing from file_path.

## Code Examples

### Telegram getFile API Response
```json
{
    "ok": true,
    "result": {
        "file_id": "AgACAgIAAxkBAAI...",
        "file_unique_id": "AQADAgAT...",
        "file_size": 28926,
        "file_path": "photos/file_0.jpg"
    }
}
```

### Telegram Photo Message
```json
{
    "message_id": 123,
    "chat": {"id": 42},
    "photo": [
        {"file_id": "small_id", "width": 90, "height": 90, "file_size": 1234},
        {"file_id": "medium_id", "width": 320, "height": 320, "file_size": 15678},
        {"file_id": "large_id", "width": 800, "height": 800, "file_size": 56789}
    ]
}
```
Pick `photo[-1]["file_id"]` = "large_id" for highest resolution.

### Telegram Voice Message
```json
{
    "message_id": 124,
    "chat": {"id": 42},
    "voice": {
        "file_id": "voice_id",
        "file_unique_id": "unique_voice",
        "duration": 5,
        "mime_type": "audio/ogg",
        "file_size": 23456
    }
}
```

### Telegram Document Message
```json
{
    "message_id": 125,
    "chat": {"id": 42},
    "document": {
        "file_id": "doc_id",
        "file_unique_id": "unique_doc",
        "file_name": "report.pdf",
        "mime_type": "application/pdf",
        "file_size": 102400
    }
}
```

### Telegram Video Message
```json
{
    "message_id": 126,
    "chat": {"id": 42},
    "video": {
        "file_id": "video_id",
        "file_unique_id": "unique_video",
        "width": 1280,
        "height": 720,
        "duration": 30,
        "mime_type": "video/mp4",
        "file_size": 5000000
    }
}
```

### Telegram Audio Message
```json
{
    "message_id": 127,
    "chat": {"id": 42},
    "audio": {
        "file_id": "audio_id",
        "file_unique_id": "unique_audio",
        "duration": 180,
        "mime_type": "audio/mpeg",
        "title": "Song Title",
        "performer": "Artist",
        "file_size": 3000000
    }
}
```

### Extracting file_id Per Media Type
```python
def _extract_file_info(msg, media_key):
    """Extract file_id, mime_type, and filename from a Telegram message for a given media key.

    Returns (file_id, mime_type_hint, filename) or (None, None, None) if extraction fails.
    """
    obj = msg.get(media_key)
    if not obj:
        return None, None, None

    if media_key == "photo":
        # photo is a list of PhotoSize, pick largest (last)
        if not obj:
            return None, None, None
        best = obj[-1]
        return best.get("file_id"), None, None  # photos have no mime_type or filename

    # voice, audio, document, video, video_note, sticker, animation are dicts
    file_id = obj.get("file_id")
    mime_type = obj.get("mime_type")
    filename = obj.get("file_name")  # only documents reliably have this
    return file_id, mime_type, filename
```

### Building MediaContent from Downloaded Bytes
```python
def _make_media_content(media_key, file_bytes, file_path, mime_hint=None, filename=None):
    """Create a MediaContent from downloaded Telegram file bytes."""
    kind = _TELEGRAM_KIND_MAP.get(media_key, "document")

    # resolve MIME type: hint from Telegram > guess from path > fallback
    mime_type = mime_hint
    if not mime_type and file_path:
        mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = _TELEGRAM_MIME_FALLBACK.get(media_key, "application/octet-stream")

    return MediaContent(
        kind=kind,
        mime_type=mime_type,
        data=file_bytes,
        filename=filename,
    )
```

### Goose Content Block for Image (MEDIA-09)
```python
# Given a MediaContent with kind="image"
mc = MediaContent(kind="image", mime_type="image/jpeg", data=raw_bytes)

# For Phase 13's relay upgrade, this produces:
block = mc.to_content_block()
# {"type": "image", "data": "/9j/4AAQ...", "mimeType": "image/jpeg"}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Reject media with MEDIA_REPLY | Download and normalize into MediaContent | Phase 12 (this phase) | Media flows through the pipeline |
| Bare type stubs in InboundMessage.media | Full MediaContent objects with actual bytes | Phase 12 (this phase) | Relay can access actual media data |
| Text-only relay to goose | Text relay + base64-ready image content blocks | Phase 12 (this phase) | Prepares for Phase 13 multimodal relay |

## Critical Integration Points

### 1. BotInstance._poll_loop (line 455)
**Must change:** Replace bare type stubs with file_id references. Remove MEDIA_REPLY early-return for paired users. Instead, pass InboundMessage with media info to `_do_message_relay`.
**Risk:** MEDIUM. Download needs to happen in the right thread.

### 2. BotInstance._do_message_relay (line 356)
**Must change:** At the start of relay, if inbound_msg has media references, download and create MediaContent objects. This keeps downloads in the relay thread (already background-threaded).
**Risk:** MEDIUM. Need to handle download failures gracefully.

### 3. _telegram_poll_loop (line 5204)
**Must change:** Mirror the BotInstance changes for the legacy single-bot path. Remove MEDIA_REPLY, add file_id extraction. Download happens in the legacy _do_relay inner function (already threaded).
**Risk:** LOW. Same pattern, just applied to legacy code.

### 4. MediaContent class (new, near line 3377)
**Must add:** New class between InboundMessage and ChannelCapabilities.
**Risk:** LOW. Additive, no existing code changes.

### 5. _download_telegram_file (new, near line 1170)
**Must add:** New function near existing Telegram API helpers.
**Risk:** LOW. Self-contained HTTP call.

### 6. InboundMessage.media type change
**Note:** Currently InboundMessage.media is `list[dict]` with bare stubs like `{"type": "image"}`. After Phase 12, it will contain `MediaContent` objects. This is backward-compatible because nothing currently reads the media list contents (it's just passed through). The Phase 11 tests check `msg.media[0]["type"]` but those tests will need updating to check MediaContent objects instead.

## Open Questions

1. **Should downloads happen in poll loop or relay thread?**
   - What we know: Poll loop must stay responsive. Relay is already threaded.
   - Recommendation: Download in `_do_message_relay` at the start, before the actual relay call. Pass file_id references from poll loop, download in relay thread.
   - This is a planner decision, not a blocker.

2. **Should InboundMessage.media contain MediaContent objects or dicts?**
   - What we know: Currently dicts. MediaContent objects are richer but not JSON-serializable directly.
   - Recommendation: Use MediaContent objects. They're only passed in-process (not serialized). The `to_content_block()` method handles serialization when needed.

3. **How to handle download failures?**
   - What we know: Telegram files can occasionally fail to download. getFile can return errors.
   - Recommendation: Log warning, skip that media attachment, proceed with remaining media and text. Never fail the entire message relay because one attachment failed to download.

4. **Does the goose web /ws endpoint accept content as an array?**
   - What we know: Goose's ChatRequest uses `Message { content: Vec<MessageContent> }`, but the WS endpoint may have a different schema. Current gateway sends `{"type": "message", "content": "text string"}`.
   - What's unclear: Whether the /ws handler accepts content as an array of content blocks or only as a string.
   - Recommendation: Phase 12 does NOT change the relay format. Phase 13 investigates and implements the multimodal relay. Phase 12 just prepares the data.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (stdlib) + pytest runner |
| Config file | None (tests run with pytest discovery) |
| Quick run command | `python3 -m pytest docker/test_gateway.py -x -q` |
| Full suite command | `python3 -m pytest docker/test_gateway.py -v` |
| Estimated runtime | ~2 seconds (345 tests currently) |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MEDIA-06 | _download_telegram_file calls getFile API and returns bytes | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestDownloadTelegramFile"` | No, Wave 0 gap |
| MEDIA-06 | _extract_file_info picks largest photo, extracts file_id for all types | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestExtractFileInfo"` | No, Wave 0 gap |
| MEDIA-06 | BotInstance._poll_loop downloads media for paired users | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestBotMediaDownload"` | No, Wave 0 gap |
| MEDIA-07 | MediaContent class stores kind, mime_type, data, filename | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestMediaContent"` | No, Wave 0 gap |
| MEDIA-07 | _make_media_content resolves MIME type with fallback chain | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestMakeMediaContent"` | No, Wave 0 gap |
| MEDIA-08 | Voice messages normalize as MediaContent(kind="audio") | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestVoiceMediaContent"` | No, Wave 0 gap |
| MEDIA-09 | MediaContent.to_content_block() returns goose-compatible image dict | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestMediaContentBlock"` | No, Wave 0 gap |
| MEDIA-09 | MediaContent.to_base64() correctly encodes data | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestMediaBase64"` | No, Wave 0 gap |
| ALL | MEDIA_REPLY no longer sent for media messages from paired users | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestMediaReplyRemoved"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m pytest docker/test_gateway.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work`
- **Estimated feedback latency per task:** ~2 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] Test classes in `docker/test_gateway.py`: TestMediaContent, TestDownloadTelegramFile, TestExtractFileInfo, TestMakeMediaContent, TestVoiceMediaContent, TestMediaContentBlock, TestMediaBase64, TestBotMediaDownload, TestMediaReplyRemoved
- [ ] All tests go in existing `docker/test_gateway.py` (project convention: single test file)
- [ ] Framework already installed (pytest + unittest), no setup needed
- [ ] Existing TestMediaMessageHandling and TestBotInboundMessage tests may need updates to reflect MediaContent objects instead of bare dicts

## Sources

### Primary (HIGH confidence)
- `/Users/haseeb/nix-template/docker/gateway.py` - Direct code analysis:
  - InboundMessage class (lines 3361-3376)
  - BotInstance._poll_loop media handling (lines 491-523)
  - BotInstance._do_message_relay (lines 356-453)
  - _has_media, MEDIA_REPLY, _MEDIA_KEYS (lines 1162-1172)
  - _relay_to_goose_web (lines 4744-4788)
  - _do_ws_relay message format (lines 4849-4855)
  - _telegram_poll_loop legacy path (lines 5204-5323)
  - send_telegram_message for API pattern (lines 1175-1219)
  - base64 import (line 38), urllib imports (lines 56-58)
- `/Users/haseeb/nix-template/docker/test_gateway.py` - 345 existing tests, media test patterns
- `/Users/haseeb/nix-template/.planning/phases/11-channel-contract-v2/11-RESEARCH.md` - Phase 11 context
- `/Users/haseeb/nix-template/.planning/REQUIREMENTS.md` - MEDIA-06 through MEDIA-09 definitions

### Secondary (MEDIUM confidence)
- [Telegram Bot API docs](https://core.telegram.org/bots/api) - getFile method, PhotoSize object, Voice/Document/Video/Audio object fields, 20MB bot download limit
- [Goose source: crates/goose/src/conversation/message.rs](https://github.com/block/goose/tree/main/crates/goose/src/conversation) - MessageContent enum with `#[serde(tag = "type", rename_all = "camelCase")]`, Image(ImageContent) variant with data + mime_type fields. Verified from GitHub raw content.

### Tertiary (LOW confidence)
- None. All findings verified from direct code or official sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - stdlib only, verified from imports and project constraint
- Architecture: HIGH - direct analysis of all integration points, Telegram API verified via docs
- Goose content blocks: HIGH - verified from goose source code (message.rs serde attributes)
- Pitfalls: HIGH - identified from reading actual code paths and Telegram API behavior
- Test map: HIGH - verified pytest runs, existing test patterns

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (stable Telegram API, stable goose message format)

# Phase 11: Channel Contract v2 - Research

**Researched:** 2026-03-13
**Domain:** Channel plugin architecture, rich media normalization, backward-compatible interface evolution
**Confidence:** HIGH

## Summary

The current channel plugin system uses a flat CHANNEL dict with a `send(text)` function as its core contract. Media messages from Telegram are explicitly rejected with a canned reply (`MEDIA_REPLY`). The relay path (`ChannelRelay.__call__`) passes raw text strings through `_relay_to_goose_web`, and responses come back as plain text. There is no concept of structured inbound messages, typed outbound adapters, or capability declarations.

Phase 11 introduces three new abstractions: `InboundMessage` (normalizes all inbound messages), `OutboundAdapter` (typed send methods per media type), and `ChannelCapabilities` (declarative feature flags). The critical constraint is backward compatibility: existing text-only channel plugins with `send(text)` must work unchanged, and the Telegram `BotInstance` (which predates the plugin system) must be adapted without breaking.

**Primary recommendation:** Define the new types as plain classes (no dataclasses, stdlib only), place them above the CHANNEL dict contract comment block around line 3330, wrap legacy `send(text)` plugins in an `OutboundAdapter` shim inside `_load_channel()`, and thread `InboundMessage` through `ChannelRelay.__call__` with a backward-compat overload that still accepts `(user_id, text)`.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MEDIA-01 | InboundMessage envelope normalizes all incoming messages (text, media, metadata) into a channel-agnostic format before reaching the relay | InboundMessage class design, ChannelRelay.__call__ evolution, BotInstance._poll_loop integration |
| MEDIA-02 | OutboundAdapter interface defines send_text (required), send_image, send_voice, send_file, send_buttons (all optional) per channel | OutboundAdapter base class, method signatures, LegacyOutboundAdapter shim |
| MEDIA-03 | ChannelCapabilities declaration per channel (supports_images, supports_voice, supports_files, max_file_size, etc.) | ChannelCapabilities class, integration with OutboundAdapter, API exposure |
| MEDIA-04 | Graceful degradation: if a channel doesn't support a media type, fall back to text (image URL, transcript, file link) | Degradation dispatch logic in OutboundAdapter base, fallback text generation |
| MEDIA-05 | Existing channel plugins with text-only send() continue to work unchanged (backward compatible) | LegacyOutboundAdapter wrapping in _load_channel(), ChannelRelay overload detection |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.x | Everything | Project constraint: no pip packages |

### Key Stdlib Modules Used
| Module | Purpose | Already Imported |
|--------|---------|-----------------|
| `threading` | Thread safety for adapter state | Yes (line 54) |
| `json` | Serialization | Yes (line 45) |
| `urllib.request` | HTTP calls for Telegram API | Yes (line 58) |
| `collections` | Potential use for frozen capability sets | Yes (line 39) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Plain classes | `dataclasses` | dataclasses is stdlib but adds import; plain classes keep consistency with existing code (SessionManager, ChannelState, CommandRouter all use plain classes) |
| ABC | Duck typing | ABC requires `import abc`; existing code uses duck typing exclusively (callable checks). Stay consistent. |
| TypedDict | Plain dict | TypedDict requires `typing` import; project doesn't use type annotations. Use plain classes with clear docstrings. |

## Architecture Patterns

### Where to Place New Types

The new types should be defined in gateway.py between the existing class definitions and the CHANNEL dict contract comment. Specifically:

```
Line 3330:  # -- channel plugin system --  (existing comment block)
```

Insert the three new classes ABOVE this line (around line 3325), after `start_cron_scheduler()` and before the CHANNEL dict docstring. This keeps them co-located with the plugin system they serve.

```
# -- channel contract v2 types --
class InboundMessage: ...
class ChannelCapabilities: ...
class OutboundAdapter: ...

# -- channel plugin system -- (existing, updated)
```

### Pattern 1: InboundMessage Envelope

**What:** A plain class that normalizes ALL incoming messages into a channel-agnostic format.

**Current state:** BotInstance._poll_loop (line 484-485) extracts `chat_id` and `text` from raw Telegram JSON. Media messages are rejected at line 491. Channel plugins pass `(user_id, text)` to `ChannelRelay.__call__` (line 3407).

**Design:**
```python
class InboundMessage:
    """Channel-agnostic inbound message envelope.

    Normalizes text, media, and metadata from any channel into a
    unified format before reaching ChannelRelay.
    """
    def __init__(self, user_id, text="", channel=None, media=None, metadata=None):
        self.user_id = str(user_id)
        self.text = text or ""
        self.channel = channel or ""
        self.media = media or []      # list of MediaAttachment dicts
        self.metadata = metadata or {} # channel-specific extras (msg_id, thread_id, etc.)

    @property
    def has_media(self):
        return bool(self.media)

    @property
    def has_text(self):
        return bool(self.text.strip())
```

**MediaAttachment dict shape:**
```python
{
    "type": "image" | "voice" | "file" | "video" | "sticker" | "animation",
    "url": "https://...",           # download URL (optional, resolved by channel)
    "file_id": "telegram_abc123",   # platform-native ID (optional)
    "mime_type": "image/jpeg",      # MIME type (optional)
    "file_name": "photo.jpg",       # original filename (optional)
    "file_size": 12345,             # bytes (optional)
    "caption": "user caption",      # text caption on media (optional)
}
```

Using plain dicts (not a class) for MediaAttachment keeps it simple and JSON-serializable. The planner can decide if a helper class is warranted.

### Pattern 2: OutboundAdapter

**What:** A base class with required `send_text()` and optional `send_image()`, `send_voice()`, `send_file()`, `send_buttons()` methods.

**Current state:** Plugins expose `send(text) -> {"sent": bool, "error": str}`. Telegram uses `send_telegram_message(bot_token, chat_id, text)` directly (line 1147).

**Design:**
```python
class OutboundAdapter:
    """Base class for channel output. Subclass per channel.

    send_text() is required. All other send_* methods are optional.
    The base class provides graceful degradation: if send_image() is not
    overridden, it falls back to send_text() with the image URL.
    """

    def capabilities(self):
        """Return ChannelCapabilities for this adapter."""
        return ChannelCapabilities()  # text-only defaults

    def send_text(self, text):
        """REQUIRED. Send text message. Returns {"sent": bool, "error": str}."""
        raise NotImplementedError("send_text() is required")

    def send_image(self, url, caption=""):
        """Send an image. Default: degrade to text with URL."""
        fallback = f"{caption}\n{url}" if caption else url
        return self.send_text(fallback.strip())

    def send_voice(self, url, transcript=""):
        """Send a voice message. Default: degrade to text with transcript."""
        fallback = transcript or f"[Voice message: {url}]"
        return self.send_text(fallback)

    def send_file(self, url, filename=""):
        """Send a file. Default: degrade to text with link."""
        fallback = f"[File: {filename}] {url}" if filename else url
        return self.send_text(fallback)

    def send_buttons(self, text, buttons):
        """Send text with action buttons. Default: degrade to numbered list."""
        lines = [text, ""]
        for i, btn in enumerate(buttons, 1):
            label = btn.get("label", btn.get("text", f"Option {i}"))
            lines.append(f"{i}. {label}")
        return self.send_text("\n".join(lines))
```

**Key insight:** Graceful degradation is built into the BASE CLASS, not a separate layer. Each `send_*` method has a sensible text fallback. Channels that support a media type override the method. This eliminates the need for a separate degradation dispatcher.

### Pattern 3: ChannelCapabilities

**What:** Declarative feature flags so the system knows what a channel supports without trial and error.

**Design:**
```python
class ChannelCapabilities:
    """Declares what a channel supports. Used for routing decisions and UI hints."""

    def __init__(self, **kwargs):
        self.supports_images = kwargs.get("supports_images", False)
        self.supports_voice = kwargs.get("supports_voice", False)
        self.supports_files = kwargs.get("supports_files", False)
        self.supports_buttons = kwargs.get("supports_buttons", False)
        self.supports_streaming = kwargs.get("supports_streaming", False)
        self.max_file_size = kwargs.get("max_file_size", 0)  # 0 = no limit or N/A
        self.max_text_length = kwargs.get("max_text_length", 0)  # 0 = no limit

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
```

### Pattern 4: LegacyOutboundAdapter (backward compat shim)

**What:** Wraps an old-style `send(text)` function in the OutboundAdapter interface.

**Design:**
```python
class LegacyOutboundAdapter(OutboundAdapter):
    """Wraps a legacy send(text) function as an OutboundAdapter.

    Used by _load_channel() when a plugin has CHANNEL["send"] but no
    CHANNEL["adapter"]. All media falls back to text automatically
    via OutboundAdapter base class.
    """

    def __init__(self, send_fn):
        self._send_fn = send_fn

    def send_text(self, text):
        return self._send_fn(text)
```

This is the linchpin of MEDIA-05. `_load_channel()` checks for `CHANNEL.get("adapter")` first; if missing, it wraps `CHANNEL["send"]` in `LegacyOutboundAdapter`. Everything downstream works with `OutboundAdapter` uniformly.

### Pattern 5: ChannelRelay Evolution

**Current:** `ChannelRelay.__call__(self, user_id, text, send_fn=None)` at line 3407.

**Target:** Accept either the old signature or an `InboundMessage`:
```python
def __call__(self, user_id_or_msg, text=None, send_fn=None):
    """Relay a message to goose web.

    Accepts either:
      - relay(user_id, text, send_fn)     # legacy (MEDIA-05)
      - relay(InboundMessage, send_fn)    # new v2 path
    """
    if isinstance(user_id_or_msg, InboundMessage):
        msg = user_id_or_msg
        send_fn = text  # second arg is send_fn in new signature
    else:
        msg = InboundMessage(user_id=user_id_or_msg, text=text)
    # ... rest uses msg.user_id, msg.text, msg.media, etc.
```

This overload pattern keeps backward compat: existing plugins that call `relay(user_id, text, send_fn)` still work. New plugins pass `relay(InboundMessage(...), adapter)`.

### CHANNEL Dict Evolution

**Current contract (v1):**
```python
CHANNEL = {
    "name": "slack",              # REQUIRED
    "version": 1,                 # REQUIRED
    "send": send_fn,              # REQUIRED: (text) -> {"sent": bool, "error": str}
    "poll": poll_fn,              # OPTIONAL
    "setup": setup_fn,            # OPTIONAL
    "teardown": teardown_fn,      # OPTIONAL
    "typing": typing_fn,          # OPTIONAL
    "credentials": ["TOKEN"],     # OPTIONAL
    "commands": {...},            # OPTIONAL
}
```

**Target contract (v2), additive:**
```python
CHANNEL = {
    # ... all v1 fields still work ...
    "version": 2,                  # bumped
    "adapter": OutboundAdapter(),  # NEW: replaces "send" when present
    "capabilities": {...},         # NEW: or provided via adapter.capabilities()
}
```

**Detection logic in _load_channel():**
1. Has `"adapter"` and it's an `OutboundAdapter` instance? Use directly.
2. Has `"send"` and it's callable? Wrap in `LegacyOutboundAdapter`.
3. Neither? Skip plugin (error).

### Anti-Patterns to Avoid
- **Separate degradation layer:** Putting fallback logic in ChannelRelay or a dispatcher. Put it in OutboundAdapter base class methods instead. Simpler, testable, overridable.
- **Breaking the notification handler signature:** `register_notification_handler` expects `handler_fn(text) -> {"sent": bool}`. The adapter's `send_text()` fulfills this. Do NOT change the notification bus signature in this phase.
- **Modifying BotInstance._do_message_relay in this phase:** BotInstance is Telegram-specific and will get its own media adapter in Phase 13 (MEDIA-13). Phase 11 only needs BotInstance to produce InboundMessage envelopes from the poll loop. The relay path from BotInstance does NOT go through ChannelRelay (it calls `_relay_to_goose_web` directly), so that's a separate concern.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Media type detection | Custom string matching | Check against a frozenset of known types | Already done: `_MEDIA_KEYS` at line 1136. Extend this pattern. |
| MIME type guessing | Custom extension mapping | Could use stdlib `mimetypes` module | Already in stdlib, accurate, maintained |
| Message chunking | New chunker for media | Extend existing `_chunk()` in send_telegram_message | Already handles 4096 char limit (line 1157). Don't duplicate. |

## Common Pitfalls

### Pitfall 1: Breaking the Notification Bus
**What goes wrong:** Changing the notification handler signature from `handler(text)` to `handler(adapter_msg)` breaks all registered handlers.
**Why it happens:** Temptation to push rich media through the notification bus immediately.
**How to avoid:** Keep notification bus at `handler(text)` for Phase 11. Rich media notifications are a Phase 12+ concern. The adapter's `send_text()` is the handler.
**Warning signs:** If you're touching `register_notification_handler` or `notify_all` signature, stop.

### Pitfall 2: BotInstance Divergence
**What goes wrong:** BotInstance has its own relay path (lines 356-452) that bypasses ChannelRelay entirely. If you only update ChannelRelay, BotInstance's Telegram adapter won't produce InboundMessage envelopes.
**Why it happens:** BotInstance predates the channel plugin system. It does NOT use ChannelRelay.
**How to avoid:** Phase 11 scope is: (1) define the types, (2) update _load_channel/ChannelRelay for plugins, (3) make BotInstance._poll_loop create InboundMessage envelopes (even if it only fills text for now). The full Telegram media pipeline is Phase 12-13.
**Warning signs:** Large changes to BotInstance._do_message_relay.

### Pitfall 3: Overloaded __call__ Ambiguity
**What goes wrong:** When ChannelRelay.__call__ accepts both `(user_id, text, send_fn)` and `(InboundMessage, send_fn)`, there's ambiguity if someone passes a string as first arg.
**Why it happens:** Python doesn't have real overloading.
**How to avoid:** Use `isinstance(first_arg, InboundMessage)` check. If it's a string, assume legacy mode. Document clearly.
**Warning signs:** Tests that work in isolation but fail when integrated.

### Pitfall 4: ChannelCapabilities Drift
**What goes wrong:** Capabilities declared at plugin load time become stale if the channel's actual capabilities change (e.g., file size limits from API).
**Why it happens:** Static declaration at load time.
**How to avoid:** Capabilities are meant to be static declarations. Dynamic capability queries (like checking current rate limits) are a different concern. Keep it simple: capabilities = what the channel CAN do, not what it WILL do right now.
**Warning signs:** If you're adding refresh/update logic to ChannelCapabilities, you're overcomplicating it.

### Pitfall 5: Forgetting to Wrap send_fn for Notifications
**What goes wrong:** `_load_channel()` registers `send_fn` directly with the notification bus (line 3586). After Phase 11, it should register `adapter.send_text` instead.
**Why it happens:** The notification handler wrapping at line 3578-3584 uses the raw `send_fn`.
**How to avoid:** Update `_load_channel()` to wrap `adapter.send_text` instead of raw `send_fn` for notification registration. For LegacyOutboundAdapter this is equivalent. For v2 adapters this is correct.

## Code Examples

### Current Media Rejection (to be replaced)
```python
# gateway.py line 490-494 (BotInstance._poll_loop)
if not text and _has_media(msg):
    paired_ids = get_paired_chat_ids(platform=self.channel_key)
    if chat_id in paired_ids:
        send_telegram_message(self.token, chat_id, MEDIA_REPLY)
    continue
```

This will be replaced with InboundMessage creation that includes media attachments.

### Current _load_channel Registration (to be evolved)
```python
# gateway.py line 3577-3586
def _make_handler(fn):
    def handler(text):
        try:
            return fn(text)
        except Exception as e:
            return {"sent": False, "error": str(e)}
    return handler

register_notification_handler(f"channel:{name}", _make_handler(send_fn))
```

This becomes:
```python
# After wrapping send_fn in adapter
adapter = channel.get("adapter") or LegacyOutboundAdapter(send_fn)
register_notification_handler(f"channel:{name}", _make_handler(adapter.send_text))
```

### Current ChannelRelay call site in _load_channel
```python
# gateway.py line 3598
relay_fn = ChannelRelay(name, typing_cb=typing_cb)
```

This stays. ChannelRelay is constructed the same way. But its `__call__` method now accepts InboundMessage in addition to the old (user_id, text) signature.

### Current notify_all (NOT changed in this phase)
```python
# gateway.py line 1289-1329
def notify_all(text, channel=None):
    # ... iterates _notification_handlers, calls handler(text)
```

This remains text-only for Phase 11. The handler signature doesn't change.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Flat send(text) | OutboundAdapter with typed methods | Phase 11 (this phase) | Enables rich media per channel |
| Raw text relay | InboundMessage envelope | Phase 11 (this phase) | Normalizes media before relay |
| No capability declaration | ChannelCapabilities | Phase 11 (this phase) | Enables routing and degradation decisions |
| _has_media -> reject | _has_media -> InboundMessage.media | Phase 11 (this phase) | Media flows through instead of being rejected |

## Critical Integration Points

### 1. _load_channel() (line 3524)
**Must change:** Detect v2 adapter, wrap v1 send in LegacyOutboundAdapter, register adapter.send_text with notification bus.
**Risk:** MEDIUM. Well-contained. Existing tests in TestDynamicChannelValidation cover the validation path.

### 2. ChannelRelay.__call__ (line 3407)
**Must change:** Accept InboundMessage as first argument (with backward compat). For now, only text field is forwarded to _relay_to_goose_web.
**Risk:** LOW. The relay path itself doesn't change. We're just normalizing the input.

### 3. BotInstance._poll_loop (line 454)
**Must change:** Create InboundMessage envelopes from Telegram update JSON. For Phase 11, text messages produce InboundMessage with text field. Media messages produce InboundMessage with media list (but BotInstance still only forwards text to relay, since the relay doesn't handle media yet in Phase 11).
**Risk:** MEDIUM. BotInstance has its own relay path. Changes here need careful testing.

### 4. notify_all (line 1289)
**Must NOT change in Phase 11.** Text-only. The handler(text) signature stays.

### 5. GET /api/channels (line 6757)
**Should extend:** Include capabilities in the channel list response.
**Risk:** LOW. Additive JSON field.

## Open Questions

1. **How should media attachments reach _relay_to_goose_web?**
   - What we know: _relay_to_goose_web sends `{"type": "message", "content": user_text}` over WebSocket. goose web only understands text content currently.
   - What's unclear: Should InboundMessage.media be serialized into the WS message? Or should media be converted to text descriptions before relay?
   - Recommendation: For Phase 11, convert media to text descriptions (e.g., "[Image attached: photo.jpg]") before relay. Phase 12 (MEDIA-06 through MEDIA-12) handles the actual media pipeline to goose web.

2. **Should OutboundAdapter hold a reference to target user/chat?**
   - What we know: Current send_fn in plugins is already scoped to a target (via closure or plugin-level state). Telegram's send_telegram_message takes (bot_token, chat_id, text) as separate args.
   - What's unclear: Should OutboundAdapter be per-channel or per-user?
   - Recommendation: Per-channel. The adapter's send_* methods should accept a `target` parameter (user_id/chat_id). The notification handler wraps this with a specific target. This matches how send_fn currently works in plugins (the plugin manages its own targeting).

3. **How to expose capabilities via the API?**
   - What we know: GET /api/channels returns plugin info (line 6757-6773).
   - What's unclear: Should capabilities be flat fields or a nested object?
   - Recommendation: Nested object under `"capabilities"` key. Clean, extensible, matches the ChannelCapabilities.to_dict() method.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (stdlib) + pytest runner |
| Config file | None (tests run with pytest discovery) |
| Quick run command | `python3 -m pytest docker/test_gateway.py -x -q` |
| Full suite command | `python3 -m pytest docker/test_gateway.py -v` |
| Estimated runtime | ~2 seconds (312 tests currently) |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MEDIA-01 | InboundMessage normalizes text, media, metadata | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestInboundMessage"` | No, Wave 0 gap |
| MEDIA-01 | BotInstance._poll_loop creates InboundMessage from Telegram JSON | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestBotInboundMessage"` | No, Wave 0 gap |
| MEDIA-02 | OutboundAdapter.send_text is required, send_image/voice/file/buttons are optional | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestOutboundAdapter"` | No, Wave 0 gap |
| MEDIA-03 | ChannelCapabilities declares supports_images, supports_voice, etc. | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestChannelCapabilities"` | No, Wave 0 gap |
| MEDIA-04 | Graceful degradation: send_image falls back to send_text with URL | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestGracefulDegradation"` | No, Wave 0 gap |
| MEDIA-05 | Legacy send(text) plugins wrapped in LegacyOutboundAdapter | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestLegacyOutboundAdapter"` | No, Wave 0 gap |
| MEDIA-05 | _load_channel wraps v1 plugins automatically | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestLoadChannelV2"` | No, Wave 0 gap |
| MEDIA-05 | ChannelRelay.__call__ accepts both old and new signatures | unit | `python3 -m pytest docker/test_gateway.py -x -k "TestChannelRelayV2"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m pytest docker/test_gateway.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work`
- **Estimated feedback latency per task:** ~2 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] Test classes in `docker/test_gateway.py`: TestInboundMessage, TestOutboundAdapter, TestChannelCapabilities, TestGracefulDegradation, TestLegacyOutboundAdapter, TestLoadChannelV2, TestChannelRelayV2, TestBotInboundMessage
- [ ] All tests go in existing `docker/test_gateway.py` (project convention: single test file)
- [ ] Framework already installed (pytest + unittest), no setup needed

## Sources

### Primary (HIGH confidence)
- `/Users/haseeb/nix-template/docker/gateway.py` - Direct code analysis of all critical sections:
  - CHANNEL dict contract (lines 3330-3345)
  - _load_channel() (lines 3524-3639)
  - ChannelRelay (lines 3396-3515)
  - BotInstance (lines 262-599)
  - send_telegram_message (lines 1147-1205)
  - notify_all (lines 1289-1329)
  - _has_media, MEDIA_REPLY, _MEDIA_KEYS (lines 1134-1144)
  - Notification handler system (lines 694-721)
  - _relay_to_goose_web (lines 4624-4668)
  - _do_ws_relay (lines 4709-4768)
- `/Users/haseeb/nix-template/.planning/REQUIREMENTS.md` - MEDIA-01 through MEDIA-05 definitions
- `/Users/haseeb/nix-template/docker/test_gateway.py` - 312 existing tests, including media handling tests

### Secondary (MEDIUM confidence)
- Telegram Bot API patterns (sendPhoto, sendVoice, sendDocument) - based on training data, verified against existing code patterns in gateway.py

### Tertiary (LOW confidence)
- None. All findings are based on direct codebase analysis.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - stdlib only, verified from imports and project constraint
- Architecture: HIGH - direct analysis of all integration points in gateway.py
- Pitfalls: HIGH - identified from reading the actual code paths and their interdependencies
- Test map: HIGH - verified pytest runs, existing test patterns in test_gateway.py

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (stable internal codebase, no external dependencies)

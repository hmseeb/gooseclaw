# Requirements: GooseClaw v2.0

**Defined:** 2026-03-13
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v2.0 Requirements

### Channel Parity

- [x] **CHAN-01**: Channel plugins receive /help, /stop, /clear, /compact commands identical to telegram
- [x] **CHAN-02**: Channel plugins have per-user relay locks preventing concurrent goose requests from same user
- [x] **CHAN-03**: Channel plugins can cancel in-flight requests via /stop (active relay tracking + socket close)
- [x] **CHAN-04**: Channel plugins can register custom commands via CHANNEL dict `commands` field
- [x] **CHAN-05**: Notification bus validates channel names dynamically from loaded plugins, not hardcoded list
- [x] **CHAN-06**: Channel plugins can signal typing/activity indicators via optional `typing` callback in CHANNEL dict
- [x] **CHAN-07**: POST /api/notify accepts optional `channel` parameter for targeted delivery
- [x] **CHAN-08**: Cron scheduler passes `notify_channel` to notify_all when job specifies it
- [x] **CHAN-09**: remind.sh accepts --notify-channel flag matching job.sh behavior

### Multi-Bot

- [x] **BOT-01**: User can configure multiple telegram bots in setup.json `bots` array with name, token, and optional provider/model
- [x] **BOT-02**: Each telegram bot runs its own poll loop with independent session store and pair codes
- [x] **BOT-03**: Each bot has per-user session locks and active relay tracking (not shared across bots)
- [x] **BOT-04**: Each bot routes to its own LLM provider/model via extended channel_routes keyed by bot name
- [x] **BOT-05**: User can add a new bot via API without container restart (hot-add)
- [x] **BOT-06**: User can remove a bot via API without container restart (hot-remove)
- [x] **BOT-07**: Existing single-bot `telegram_bot_token` config remains backward-compatible as default bot

### Infrastructure

- [x] **INFRA-01**: SessionManager class with composite key (channel:user_id) replaces per-channel session dicts
- [x] **INFRA-02**: CommandRouter class dispatches /help /stop /clear /compact to shared handlers
- [x] **INFRA-03**: Telegram globals (_telegram_sessions, _telegram_active_relays, _telegram_chat_locks) refactored into per-instance state
- [x] **INFRA-04**: /clear scoped to requesting channel's sessions only, not global goose web restart (or documented limitation)

## v3.0 Requirements

### Rich Media & Channel Flexibility

**Goal:** Make channels truly flexible. Any media type (images, voice, files) flows seamlessly in both directions across any channel. The agent never knows which platform it's on.

#### Channel Contract v2

- **MEDIA-01**: InboundMessage envelope normalizes all incoming messages (text, media, metadata) into a channel-agnostic format before reaching the relay
- **MEDIA-02**: OutboundAdapter interface defines send_text (required), send_image, send_voice, send_file, send_buttons (all optional) per channel
- **MEDIA-03**: ChannelCapabilities declaration per channel (supports_images, supports_voice, supports_files, max_file_size, etc.)
- **MEDIA-04**: Graceful degradation: if a channel doesn't support a media type, fall back to text (image URL, transcript, file link)
- **MEDIA-05**: Existing channel plugins with text-only send() continue to work unchanged (backward compatible)

#### Inbound Media Pipeline

- **MEDIA-06**: Telegram adapter downloads media (photo, voice, document, video, sticker, audio) via getFile API and buffers as bytes
- **MEDIA-07**: MediaContent class normalizes media with kind (image/audio/video/document), mime_type, data (bytes), and optional filename
- **MEDIA-08**: Voice messages are downloaded and normalized as MediaContent(kind="audio") like any other media, no built-in STT (users configure their own)
- **MEDIA-09**: Images are base64-encoded and sent to goose as multimodal content blocks

#### Relay Protocol Upgrade

- **MEDIA-10**: Gateway relay sends multimodal content blocks to goose (images as base64 in content array) instead of text-only strings
- **MEDIA-11**: Gateway parses typed content blocks in goose responses (text, image, audio) and routes to outbound adapter
- **MEDIA-12**: Relay upgrade is backward-compatible: text-only messages still work identically

#### Outbound Rich Media

- [x] **MEDIA-13**: Telegram adapter implements send_image (sendPhoto), send_voice (sendVoice), send_file (sendDocument)
- [x] **MEDIA-14**: notify_all supports media attachments alongside text

#### Reference Plugin

- **MEDIA-15**: At least one non-Telegram channel plugin (Slack or Discord) ships with full rich media support using the new contract
- **MEDIA-16**: Adding a new channel with media support requires only implementing the OutboundAdapter methods, no gateway changes

### Deferred (v4.0+)

- **CROSS-01**: Cross-channel session continuity (same goose session across telegram + discord)
- **CROSS-02**: Unified user identity layer mapping platform IDs to GooseClaw user
- **PERF-01**: Webhook mode for telegram bots (polling fine for 1-5 bots)
- **PERS-01**: Per-bot personality/system prompt (each bot gets its own soul.md)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Cross-channel message bridging | GooseClaw is an AI gateway, not a chat bridge. Use Matterbridge for that. |
| OAuth/SSO for channels | Too complex per-platform. Stick with API tokens and pairing codes. |
| Per-message provider switching | Goose sessions are tied to a provider. Switching mid-session breaks context. |
| Auto-downloading plugins from registry | Massive attack surface. Manual .py file drops only. |
| Multiple goose web processes | Single process, sessions provide isolation. |
| Platform-specific rich UI (cards, carousels) | Beyond v3.0 scope. send_buttons is the escape hatch for now. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| INFRA-01 | Phase 6 | Complete |
| INFRA-02 | Phase 6 | Complete |
| INFRA-03 | Phase 6 | Complete |
| INFRA-04 | Phase 6 | Complete |
| CHAN-01 | Phase 7 | Complete |
| CHAN-02 | Phase 7 | Complete |
| CHAN-03 | Phase 7 | Complete |
| CHAN-04 | Phase 7 | Complete |
| CHAN-05 | Phase 7 | Complete |
| CHAN-06 | Phase 7 | Complete |
| CHAN-07 | Phase 8 | Complete |
| CHAN-08 | Phase 8 | Complete |
| CHAN-09 | Phase 8 | Complete |
| BOT-01 | Phase 9 | Complete |
| BOT-02 | Phase 9 | Complete |
| BOT-03 | Phase 9 | Complete |
| BOT-04 | Phase 9 | Complete |
| BOT-07 | Phase 9 | Complete |
| BOT-05 | Phase 10 | Complete |
| BOT-06 | Phase 10 | Complete |

**Coverage (v2.0):**
- v2.0 requirements: 20 total
- Mapped to phases: 20
- Unmapped: 0

## v3.0 Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| MEDIA-01 | Phase 11 | Complete |
| MEDIA-02 | Phase 11 | Complete |
| MEDIA-03 | Phase 11 | Complete |
| MEDIA-04 | Phase 11 | Complete |
| MEDIA-05 | Phase 11 | Complete |
| MEDIA-06 | Phase 12 | Complete |
| MEDIA-07 | Phase 12 | Complete |
| MEDIA-08 | Phase 12 | Complete |
| MEDIA-09 | Phase 12 | Complete |
| MEDIA-10 | Phase 13 | Complete |
| MEDIA-11 | Phase 13 | Complete |
| MEDIA-12 | Phase 13 | Complete |
| MEDIA-13 | Phase 14 | Complete |
| MEDIA-14 | Phase 14 | Complete |
| MEDIA-15 | Phase 15 | Pending |
| MEDIA-16 | Phase 15 | Pending |

**Coverage (v3.0):**
- v3.0 requirements: 16 total
- Mapped to phases: 16
- Unmapped: 0

---
*Requirements defined: 2026-03-13*
*Last updated: 2026-03-13 after v3.0 milestone scoping*

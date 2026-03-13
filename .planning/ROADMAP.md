# Roadmap: GooseClaw

## Milestones

- [x] **v1.0 Setup Wizard** - Phases 1-5 (shipped 2026-03-11)
- [x] **v2.0 Multi-Channel & Multi-Bot** - Phases 6-10 (shipped 2026-03-13)
- [ ] **v3.0 Rich Media & Channel Flexibility** - Phases 11-15

## Phases

<details>
<summary>v1.0 Setup Wizard (Phases 1-5) - SHIPPED 2026-03-11</summary>

- [x] **Phase 1: Provider UI Expansion** - Redesign wizard with 15+ providers in categories, model selection, and full setup flow steps (completed 2026-03-10)
- [x] **Phase 2: Validation and Env Plumbing** - Every provider validates credentials, maps env vars correctly, rehydrates on restart, and pre-fills on reconfigure (completed 2026-03-10)
- [x] **Phase 3: Gateway Resilience and Live Feedback** - goose web is monitored, auto-restarted, errors surfaced to user, real-time startup status, and auth recovery (completed 2026-03-10)
- [x] **Phase 4: Advanced Multi-Model Settings** - Lead/worker multi-model configuration for power users (completed 2026-03-11)
- [x] **Phase 5: Production Hardening** - Security, reliability, and deployment quality across gateway, entrypoint, and Dockerfile (completed 2026-03-10)

</details>

### v2.0 Multi-Channel & Multi-Bot

**Milestone Goal:** Make channel plugins first-class citizens with full parity to Telegram, and support multiple Telegram bots with independent provider/model configs on a single gateway.

- [x] **Phase 6: Shared Infrastructure Extraction** - Extract SessionManager and CommandRouter from Telegram-specific code into shared abstractions (completed 2026-03-13)
- [x] **Phase 7: Channel Plugin Parity** - Wire channel plugins to shared infrastructure for commands, locks, cancellation, and typing indicators (completed 2026-03-13)
- [x] **Phase 8: Notification Channel Targeting** - Complete /api/notify and cron scheduler support for per-channel delivery (completed 2026-03-13)
- [x] **Phase 9: Multi-Bot Core** - Multiple Telegram bots on one gateway with independent sessions, provider routing, and backward-compatible config (completed 2026-03-13)
- [x] **Phase 10: Multi-Bot Lifecycle** - Hot-add and hot-remove bots via API without container restart (completed 2026-03-13)

## Phase Details

### Phase 6: Shared Infrastructure Extraction
**Goal**: Telegram's session management, command routing, and concurrency primitives are extracted into reusable shared components with zero behavior change
**Depends on**: Phase 5 (v1.0 complete)
**Requirements**: INFRA-01, INFRA-02, INFRA-03, INFRA-04
**Success Criteria** (what must be TRUE):
  1. SessionManager class exists with composite key (channel:user_id) and all Telegram sessions are managed through it
  2. CommandRouter class dispatches /help, /stop, /clear, /compact and Telegram's command handling delegates to it
  3. Telegram globals (_telegram_sessions, _telegram_active_relays, _telegram_chat_locks) no longer exist as module-level dicts -- they live as per-instance state
  4. /clear clears only the requesting channel's sessions, not all sessions across all channels (or this limitation is documented with a scoping decision)
  5. All existing Telegram behavior passes existing tests -- zero functional regression
**Plans:** 3 plans

Plans:
- [ ] 06-01-PLAN.md -- TDD: SessionManager and ChannelState classes (INFRA-01, INFRA-03)
- [ ] 06-02-PLAN.md -- TDD: CommandRouter class (INFRA-02)
- [ ] 06-03-PLAN.md -- Wire classes into gateway, remove globals, fix /clear scoping (INFRA-03, INFRA-04)

### Phase 7: Channel Plugin Parity
**Goal**: Channel plugins have identical capabilities to Telegram for commands, per-user concurrency safety, cancellation, and activity indicators
**Depends on**: Phase 6
**Requirements**: CHAN-01, CHAN-02, CHAN-03, CHAN-04, CHAN-05, CHAN-06
**Success Criteria** (what must be TRUE):
  1. A channel plugin user can send /help, /stop, /clear, /compact and get the same behavior as Telegram users
  2. Two messages from the same user on a channel plugin are serialized (second waits for first relay to complete)
  3. A channel plugin user can cancel an in-flight request via /stop and the active WebSocket relay is closed
  4. A channel plugin can register custom commands via the `commands` field in its CHANNEL dict and users can invoke them
  5. Notification bus validates channel names from loaded plugins dynamically, not from a hardcoded list
  6. Channel plugins can signal typing/activity indicators via optional `typing` callback in CHANNEL dict
**Plans:** 3 plans

Plans:
- [ ] 07-01-PLAN.md -- TDD: Generalize command handlers + ChannelRelay command interception and relay tracking (CHAN-01, CHAN-03)
- [ ] 07-02-PLAN.md -- TDD: Per-user locks and typing indicators in ChannelRelay (CHAN-02, CHAN-06)
- [ ] 07-03-PLAN.md -- TDD: Custom command registration and dynamic channel validation (CHAN-04, CHAN-05)

### Phase 8: Notification Channel Targeting
**Goal**: The notification pipeline (API, cron, remind.sh) can deliver to specific channels instead of broadcasting to all
**Depends on**: Phase 7
**Requirements**: CHAN-07, CHAN-08, CHAN-09
**Success Criteria** (what must be TRUE):
  1. POST /api/notify with a `channel` parameter delivers only to that channel, not all channels
  2. A cron job with `notify_channel` set delivers its output to only that channel
  3. remind.sh accepts --notify-channel flag and the reminder is delivered to the specified channel only
**Plans:** 1 plan

Plans:
- [x] 08-01-PLAN.md -- Wire channel targeting through API, cron scheduler, and remind.sh (CHAN-07, CHAN-08, CHAN-09)

### Phase 9: Multi-Bot Core
**Goal**: Users can run multiple Telegram bots on a single GooseClaw gateway, each with its own sessions, provider, and model
**Depends on**: Phase 6
**Requirements**: BOT-01, BOT-02, BOT-03, BOT-04, BOT-07
**Success Criteria** (what must be TRUE):
  1. User can configure multiple bots in setup.json with distinct names, tokens, and optional provider/model overrides
  2. Each bot runs its own poll loop and maintains independent session stores and pair codes
  3. Per-user session locks and active relay tracking are scoped per-bot (one bot's lock does not block another bot's users)
  4. Each bot routes to its own LLM provider/model via extended channel_routes keyed by bot name
  5. Existing single-bot `telegram_bot_token` config continues to work as the default bot with no migration required
**Plans:** 3 plans

Plans:
- [x] 09-01-PLAN.md -- TDD: BotInstance and BotManager classes, config resolution, validation (BOT-01, BOT-02, BOT-03, BOT-07)
- [x] 09-02-PLAN.md -- TDD: Poll loop refactor into BotInstance, notification handlers, per-bot pairing (BOT-02, BOT-03, BOT-04)
- [x] 09-03-PLAN.md -- TDD: Wire BotManager into startup, shutdown, and API endpoints (BOT-04, BOT-07)

### Phase 10: Multi-Bot Lifecycle
**Goal**: Operators can dynamically add and remove bots without restarting the container
**Depends on**: Phase 9
**Requirements**: BOT-05, BOT-06
**Success Criteria** (what must be TRUE):
  1. User can add a new bot via API call and it begins polling immediately without container restart
  2. User can remove a bot via API call and its poll loop stops, sessions are cleaned up, without affecting other bots
  3. Adding or removing a bot does not interrupt active conversations on other bots
**Plans:** 1 plan

Plans:
- [x] 10-01-PLAN.md -- TDD: Hot-add and hot-remove bot API endpoints with setup.json persistence (BOT-05, BOT-06)

### v3.0 Rich Media & Channel Flexibility

**Milestone Goal:** Make channels truly flexible for rich media. Images, voice, files flow seamlessly in both directions across any channel. The agent is channel-agnostic. Adding a new channel with full media support requires only implementing adapter methods.

- [x] **Phase 11: Channel Contract v2** - Define InboundMessage envelope, OutboundAdapter interface, ChannelCapabilities. Refactor existing send(text) to send_text() with backward compatibility. (completed 2026-03-13)
- [x] **Phase 12: Inbound Media Pipeline** - Download + normalize incoming media from Telegram (getFile API). MediaContent class. Base64 encoding for images. Replace MEDIA_REPLY with actual processing. (completed 2026-03-13)
- [x] **Phase 13: Relay Protocol Upgrade** - Switch from custom WS text-only to goosed REST /reply with multimodal content blocks. Parse typed content blocks in responses. (completed 2026-03-13)
- [x] **Phase 14: Outbound Rich Media** - Implement send_image, send_voice, send_file on Telegram adapter. Graceful degradation. Media-aware notify_all. (completed 2026-03-13)
- [ ] **Phase 15: Reference Channel Plugin** - Build Slack or Discord plugin with full rich media using the v2 contract. Validates the abstraction.

## Phase Details (v3.0)

### Phase 11: Channel Contract v2
**Goal**: The channel plugin interface supports rich media (images, voice, files) with declarative capabilities and graceful degradation, while remaining backward-compatible with text-only plugins
**Depends on**: Phase 10 (v2.0 complete)
**Requirements**: MEDIA-01, MEDIA-02, MEDIA-03, MEDIA-04, MEDIA-05
**Success Criteria** (what must be TRUE):
  1. InboundMessage dataclass normalizes text + media + metadata from any platform into one format
  2. OutboundAdapter protocol defines send_text (required), send_image, send_voice, send_file (optional)
  3. ChannelCapabilities dict declares what each channel supports (images, voice, files, buttons, max sizes)
  4. If a channel doesn't implement send_image, sending an image gracefully falls back to text (URL or description)
  5. Existing channel plugins with only send(text) continue to work with zero changes
**Plans:** 2/2 plans complete

### Phase 12: Inbound Media Pipeline
**Goal**: Media messages from users (photos, voice, documents, videos) are downloaded, normalized into MediaContent, and prepared for relay to goose
**Depends on**: Phase 11
**Requirements**: MEDIA-06, MEDIA-07, MEDIA-09
**Success Criteria** (what must be TRUE):
  1. Telegram adapter calls getFile API and downloads media bytes for all 8 media types
  2. MediaContent(kind, mime_type, data, filename) is populated correctly for each media type
  3. Images are base64-encoded and packaged as goose-compatible content blocks
  4. Media with captions preserves both the text and media content
  5. Download failures are handled gracefully (user gets error message, not silence)
**Plans:** 2/2 plans complete

### Phase 13: Relay Protocol Upgrade
**Goal**: The gateway relay supports multimodal content (images, audio) in both directions instead of text-only strings
**Depends on**: Phase 12
**Requirements**: MEDIA-10, MEDIA-11, MEDIA-12
**Success Criteria** (what must be TRUE):
  1. Gateway sends multimodal content blocks to goosed /reply endpoint (text + image in content array)
  2. Gateway parses typed content blocks in goose responses and separates text from media
  3. Text-only messages relay identically to current behavior (zero regression)
  4. Goose receives and processes user-sent images (vision model sees the image)
  5. Tool responses containing images/audio are captured and routed to outbound adapter
**Plans:** 2/2 plans complete

Plans:
- [x] 13-01-PLAN.md -- TDD: REST relay helpers (SSE parser, content blocks, _do_rest_relay) (MEDIA-10, MEDIA-11, MEDIA-12)
- [x] 13-02-PLAN.md -- Wire REST relay into _relay_to_goose_web, update all callers, remove WS code (MEDIA-10, MEDIA-11, MEDIA-12)

### Phase 14: Outbound Rich Media
**Goal**: The agent can send images, voice notes, and files back to users through any channel that supports them
**Depends on**: Phase 13
**Requirements**: MEDIA-13, MEDIA-14, MEDIA-15
**Success Criteria** (what must be TRUE):
  1. Telegram adapter sends images via sendPhoto, voice via sendVoice, files via sendDocument
  2. Agent-generated media (from goose tools or TTS) routes through the correct send method
  3. notify_all supports optional media attachment alongside text
  4. Channels without media support get graceful text fallback (not errors)
**Plans:** 2/2 plans complete

### Phase 15: Reference Channel Plugin
**Goal**: A non-Telegram channel plugin (Slack or Discord) ships with full rich media support, validating the v2 contract
**Depends on**: Phase 14
**Requirements**: MEDIA-15, MEDIA-16
**Success Criteria** (what must be TRUE):
  1. Plugin implements OutboundAdapter with send_text, send_image, send_voice, send_file
  2. Plugin declares ChannelCapabilities accurately for the platform
  3. Media flows end-to-end (user sends image on platform → goose sees it → goose responds with image → user sees it)
  4. Adding this plugin required zero changes to gateway core code
  5. Plugin serves as a template for other channels
**Plans:** TBD during phase planning

## Progress

**Execution Order (v2.0):**
Phases execute in numeric order: 6 -> 7 -> 8 -> 9 -> 10
Phase 9 depends on Phase 6 (not Phase 7/8), so Phase 9 could start after Phase 6 if needed.

**Execution Order (v3.0):**
Phases execute: 11 -> 12 -> 13 -> 14 -> 15
Phase 15 (reference plugin) depends on Phase 14 (outbound media).

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Provider UI Expansion | v1.0 | 2/2 | Complete | 2026-03-10 |
| 2. Validation and Env Plumbing | v1.0 | 3/3 | Complete | 2026-03-10 |
| 3. Gateway Resilience and Live Feedback | v1.0 | 2/2 | Complete | 2026-03-10 |
| 4. Advanced Multi-Model Settings | v1.0 | 1/1 | Complete | 2026-03-11 |
| 5. Production Hardening | v1.0 | 6/6 | Complete | 2026-03-10 |
| 6. Shared Infrastructure Extraction | v2.0 | 3/3 | Complete | 2026-03-13 |
| 7. Channel Plugin Parity | v2.0 | 3/3 | Complete | 2026-03-13 |
| 8. Notification Channel Targeting | v2.0 | 1/1 | Complete | 2026-03-13 |
| 9. Multi-Bot Core | v2.0 | 3/3 | Complete | 2026-03-13 |
| 10. Multi-Bot Lifecycle | v2.0 | 1/1 | Complete | 2026-03-13 |
| 11. Channel Contract v2 | 2/2 | Complete   | 2026-03-13 | - |
| 12. Inbound Media Pipeline | v3.0 | 2/2 | Complete | 2026-03-13 |
| 13. Relay Protocol Upgrade | 2/2 | Complete   | 2026-03-13 | - |
| 14. Outbound Rich Media | v3.0 | 2/2 | Complete | 2026-03-13 |
| 15. Reference Channel Plugin | v3.0 | 0/? | Pending | - |

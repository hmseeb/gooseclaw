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
- [ ] **BOT-05**: User can add a new bot via API without container restart (hot-add)
- [ ] **BOT-06**: User can remove a bot via API without container restart (hot-remove)
- [x] **BOT-07**: Existing single-bot `telegram_bot_token` config remains backward-compatible as default bot

### Infrastructure

- [x] **INFRA-01**: SessionManager class with composite key (channel:user_id) replaces per-channel session dicts
- [x] **INFRA-02**: CommandRouter class dispatches /help /stop /clear /compact to shared handlers
- [x] **INFRA-03**: Telegram globals (_telegram_sessions, _telegram_active_relays, _telegram_chat_locks) refactored into per-instance state
- [x] **INFRA-04**: /clear scoped to requesting channel's sessions only, not global goose web restart (or documented limitation)

## v3.0 Requirements

### Deferred

- **CROSS-01**: Cross-channel session continuity (same goose session across telegram + discord)
- **CROSS-02**: Unified user identity layer mapping platform IDs to GooseClaw user
- **PERF-01**: Webhook mode for telegram bots (polling fine for 1-5 bots)
- **PERS-01**: Per-bot personality/system prompt (each bot gets its own soul.md)
- **COMM-01**: Reference channel plugins for Discord, Slack, WhatsApp

## Out of Scope

| Feature | Reason |
|---------|--------|
| Cross-channel message bridging | GooseClaw is an AI gateway, not a chat bridge. Use Matterbridge for that. |
| Platform-specific rich UI abstraction | Unified "rich message" format is a rabbit hole. Plugins handle their own formatting in send(). |
| OAuth/SSO for channels | Too complex per-platform. Stick with API tokens and pairing codes. |
| Per-message provider switching | Goose sessions are tied to a provider. Switching mid-session breaks context. |
| Auto-downloading plugins from registry | Massive attack surface. Manual .py file drops only. |
| Multiple goose web processes | Single process, sessions provide isolation. |

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
| BOT-05 | Phase 10 | Pending |
| BOT-06 | Phase 10 | Pending |

**Coverage:**
- v2.0 requirements: 20 total
- Mapped to phases: 20
- Unmapped: 0

---
*Requirements defined: 2026-03-13*
*Last updated: 2026-03-13 after roadmap creation*

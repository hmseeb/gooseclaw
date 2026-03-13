# Requirements: GooseClaw v2.0

**Defined:** 2026-03-13
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v2.0 Requirements

### Channel Parity

- [ ] **CHAN-01**: Channel plugins receive /help, /stop, /clear, /compact commands identical to telegram
- [ ] **CHAN-02**: Channel plugins have per-user relay locks preventing concurrent goose requests from same user
- [ ] **CHAN-03**: Channel plugins can cancel in-flight requests via /stop (active relay tracking + socket close)
- [ ] **CHAN-04**: Channel plugins can register custom commands via CHANNEL dict `commands` field
- [ ] **CHAN-05**: Notification bus validates channel names dynamically from loaded plugins, not hardcoded list
- [ ] **CHAN-06**: Channel plugins can signal typing/activity indicators via optional `typing` callback in CHANNEL dict
- [ ] **CHAN-07**: POST /api/notify accepts optional `channel` parameter for targeted delivery
- [ ] **CHAN-08**: Cron scheduler passes `notify_channel` to notify_all when job specifies it
- [ ] **CHAN-09**: remind.sh accepts --notify-channel flag matching job.sh behavior

### Multi-Bot

- [ ] **BOT-01**: User can configure multiple telegram bots in setup.json `bots` array with name, token, and optional provider/model
- [ ] **BOT-02**: Each telegram bot runs its own poll loop with independent session store and pair codes
- [ ] **BOT-03**: Each bot has per-user session locks and active relay tracking (not shared across bots)
- [ ] **BOT-04**: Each bot routes to its own LLM provider/model via extended channel_routes keyed by bot name
- [ ] **BOT-05**: User can add a new bot via API without container restart (hot-add)
- [ ] **BOT-06**: User can remove a bot via API without container restart (hot-remove)
- [ ] **BOT-07**: Existing single-bot `telegram_bot_token` config remains backward-compatible as default bot

### Infrastructure

- [ ] **INFRA-01**: SessionManager class with composite key (channel:user_id) replaces per-channel session dicts
- [ ] **INFRA-02**: CommandRouter class dispatches /help /stop /clear /compact to shared handlers
- [ ] **INFRA-03**: Telegram globals (_telegram_sessions, _telegram_active_relays, _telegram_chat_locks) refactored into per-instance state
- [ ] **INFRA-04**: /clear scoped to requesting channel's sessions only, not global goose web restart (or documented limitation)

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
| CHAN-01 | — | Pending |
| CHAN-02 | — | Pending |
| CHAN-03 | — | Pending |
| CHAN-04 | — | Pending |
| CHAN-05 | — | Pending |
| CHAN-06 | — | Pending |
| CHAN-07 | — | Pending |
| CHAN-08 | — | Pending |
| CHAN-09 | — | Pending |
| BOT-01 | — | Pending |
| BOT-02 | — | Pending |
| BOT-03 | — | Pending |
| BOT-04 | — | Pending |
| BOT-05 | — | Pending |
| BOT-06 | — | Pending |
| BOT-07 | — | Pending |
| INFRA-01 | — | Pending |
| INFRA-02 | — | Pending |
| INFRA-03 | — | Pending |
| INFRA-04 | — | Pending |

**Coverage:**
- v2.0 requirements: 20 total
- Mapped to phases: 0
- Unmapped: 20

---
*Requirements defined: 2026-03-13*
*Last updated: 2026-03-13 after initial definition*

# Roadmap: GooseClaw

## Milestones

- [x] **v1.0 Setup Wizard** - Phases 1-5 (shipped 2026-03-11)
- [ ] **v2.0 Multi-Channel & Multi-Bot** - Phases 6-10 (in progress)

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

- [ ] **Phase 6: Shared Infrastructure Extraction** - Extract SessionManager and CommandRouter from Telegram-specific code into shared abstractions
- [ ] **Phase 7: Channel Plugin Parity** - Wire channel plugins to shared infrastructure for commands, locks, cancellation, and typing indicators
- [ ] **Phase 8: Notification Channel Targeting** - Complete /api/notify and cron scheduler support for per-channel delivery
- [ ] **Phase 9: Multi-Bot Core** - Multiple Telegram bots on one gateway with independent sessions, provider routing, and backward-compatible config
- [ ] **Phase 10: Multi-Bot Lifecycle** - Hot-add and hot-remove bots via API without container restart

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
**Plans**: TBD

Plans:
- [ ] 08-01: TBD

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
**Plans**: TBD

Plans:
- [ ] 09-01: TBD
- [ ] 09-02: TBD

### Phase 10: Multi-Bot Lifecycle
**Goal**: Operators can dynamically add and remove bots without restarting the container
**Depends on**: Phase 9
**Requirements**: BOT-05, BOT-06
**Success Criteria** (what must be TRUE):
  1. User can add a new bot via API call and it begins polling immediately without container restart
  2. User can remove a bot via API call and its poll loop stops, sessions are cleaned up, without affecting other bots
  3. Adding or removing a bot does not interrupt active conversations on other bots
**Plans**: TBD

Plans:
- [ ] 10-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 6 -> 7 -> 8 -> 9 -> 10
Phase 9 depends on Phase 6 (not Phase 7/8), so Phase 9 could start after Phase 6 if needed.

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Provider UI Expansion | v1.0 | 2/2 | Complete | 2026-03-10 |
| 2. Validation and Env Plumbing | v1.0 | 3/3 | Complete | 2026-03-10 |
| 3. Gateway Resilience and Live Feedback | v1.0 | 2/2 | Complete | 2026-03-10 |
| 4. Advanced Multi-Model Settings | v1.0 | 1/1 | Complete | 2026-03-11 |
| 5. Production Hardening | v1.0 | 6/6 | Complete | 2026-03-10 |
| 6. Shared Infrastructure Extraction | v2.0 | 0/3 | Planned | - |
| 7. Channel Plugin Parity | v2.0 | 0/3 | Planned | - |
| 8. Notification Channel Targeting | v2.0 | 0/? | Not started | - |
| 9. Multi-Bot Core | v2.0 | 0/? | Not started | - |
| 10. Multi-Bot Lifecycle | v2.0 | 0/? | Not started | - |

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-13)

**Core value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try
**Current focus:** v3.0 Rich Media & Channel Flexibility

## Current Position

Phase: 15 of 15 (complete)
Plan: 1 of 1 (done)
Status: Phase 15 complete, Discord channel plugin with full media support proves v2 contract
Last activity: 2026-03-14 - Completed quick task 8: /status command

Progress v2.0: [==========] 100% (10/10 phases complete, shipped)
Progress v3.0: [==========] 100% (5/5 phases complete)

## Performance Metrics

**Velocity (v1.0):**
- Total plans completed: 14
- Average duration: ~6 min
- Total execution time: ~1.4 hours

**By Phase (v1.0):**

| Phase | Plans | Status |
|-------|-------|--------|
| 1. Provider UI | 2 | Complete |
| 2. Validation | 3 | Complete |
| 3. Gateway | 2 | Complete |
| 4. Advanced | 1 | Complete |
| 5. Hardening | 6 | Complete |
| Phase 06 P01 | 3min | 2 tasks | 2 files |
| Phase 06 P02 | 2min | 2 tasks | 2 files |
| Phase 06 P03 | 7min | 2 tasks | 2 files |
| Phase 07 P01 | 3min | 2 tasks | 2 files |
| Phase 07 P02 | 3min | 2 tasks | 2 files |
| Phase 07 P03 | 5min | 2 tasks | 2 files |
| Phase 08 P01 | 3min | 2 tasks | 3 files |
| Phase 09 P01 | 4min | 2 tasks | 2 files |
| Phase 09 P02 | 6min | 2 tasks | 2 files |
| Phase 09 P03 | 5min | 2 tasks | 2 files |
| Phase 10 P01 | 4min | 2 tasks | 2 files |
| Phase 11 P01 | 3min | 2 tasks | 2 files |
| Phase 11 P02 | 2min | 2 tasks | 2 files |
| Phase 12 P01 | 3min | 2 tasks | 2 files |
| Phase 12 P02 | 3min | 2 tasks | 2 files |
| Phase 13 P01 | 3min | 2 tasks | 2 files |
| Phase 13 P02 | 12min | 2 tasks | 2 files |
| Phase 14 P01 | 4min | 2 tasks | 2 files |
| Phase 14 P02 | 14min | 2 tasks | 2 files |
| Phase 15 P01 | 6min | 2 tasks | 3 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Single goose web process shared across all channels/bots (constraint)
- /clear currently restarts entire goose web -- needs scoping decision in Phase 6
- 17 threading.Lock() instances with no ordering hierarchy -- lock audit needed
- 132 Telegram-specific references need refactoring into shared abstractions
- [Phase 08]: All notification paths (API, cron, remind.sh) now support per-channel targeting via notify_all(text, channel=...)
- [Phase 06]: CommandRouter uses register/dispatch pattern with case-insensitive matching, no module-level instance yet
- [Phase 06]: SessionManager uses composite key channel:user_id with atomic disk persistence, ChannelState provides per-user locks and relay kill
- [Phase 06]: /clear scoping fixed: only removes requesting user's session (INFRA-04), goose web restart still documented limitation
- [Phase 06]: All telegram globals replaced with SessionManager/ChannelState/CommandRouter instances (INFRA-03)
- [Phase 07]: Command handlers use ctx.get("channel_state", _telegram_state) for backward-compatible generalization
- [Phase 07]: ChannelRelay has command interception and active relay tracking via its own ChannelState instance
- [Phase 07]: _handle_cmd_compact uses _session_manager.get(channel) instead of telegram-specific _get_session_id
- [Phase 07]: ChannelRelay acquires per-user lock (timeout 2s/120s) before relay, sends busy message on contention
- [Phase 07]: Typing indicator loop fires callback every 4s during relay, stops in finally block
- [Phase 07]: Custom commands registered on global _command_router with conflict detection (built-in wins)
- [Phase 07]: _get_valid_channels() replaces all hardcoded valid_channels tuples dynamically
- [Phase 09]: BotInstance uses channel_key "telegram:<name>" by default, "telegram" for default bot (zero migration)
- [Phase 09]: BotManager returns existing bot on duplicate name (idempotent), raises ValueError on duplicate token
- [Phase 09]: _resolve_bot_configs falls back: bots array > telegram_bot_token > TELEGRAM_BOT_TOKEN env > empty
- [Phase 09]: _do_message_relay and _check_pairing extracted as testable BotInstance methods from poll loop closures
- [Phase 09]: get_paired_chat_ids, _add_pairing_to_config, _get_session_id all parameterized with backward-compatible defaults
- [Phase 09]: Module-level _bot_manager wired into apply_config, shutdown, _is_goose_gateway_running, and API endpoints
- [Phase 09]: start_telegram_gateway becomes thin wrapper around _bot_manager.add_bot("default", token)
- [Phase 09]: handle_telegram_status returns "bots" array alongside backward-compat top-level fields
- [Phase 09]: handle_telegram_pair accepts ?bot=name query param, defaults to "default"
- [Phase 10]: POST /api/bots hot-adds bot with validation, start, and setup.json persistence
- [Phase 10]: DELETE /api/bots/<name> hot-removes bot with stop, session clear, notification unregister, and setup.json persistence
- [Phase 10]: BotManager.remove_bot() enhanced with full cleanup cascade (stop + sessions + notifications)
- [Phase 11]: Plain classes (no dataclasses/ABC) for InboundMessage, OutboundAdapter, ChannelCapabilities (stdlib only)
- [Phase 11]: Graceful degradation built into OutboundAdapter base class, not a separate dispatcher
- [Phase 11]: LegacyOutboundAdapter wraps send(text) with single send_text override
- [Phase 11]: ChannelRelay.__call__ uses isinstance(first_arg, InboundMessage) for dual-signature overload
- [Phase 11]: _load_channel stores adapter in _loaded_channels, registers adapter.send_text for notifications
- [Phase 11]: BotInstance._poll_loop creates InboundMessage envelopes, passes as inbound_msg kwarg to _do_message_relay
- [Phase 11]: Media messages still get MEDIA_REPLY but InboundMessage envelope is created for future Phase 12 use
- [Phase 12]: MediaContent class normalizes media with kind/mime_type/data/filename, to_base64(), to_content_block()
- [Phase 12]: Downloads happen in relay thread (not poll loop) to keep poll responsive
- [Phase 12]: Poll loop builds file_id reference dicts, relay thread downloads + creates MediaContent
- [Phase 12]: MEDIA_REPLY no longer sent to paired users, media flows through relay
- [Phase 12]: MIME resolution: Telegram hint > mimetypes.guess_type(file_path) > fallback map
- [Phase 12]: Legacy _telegram_poll_loop updated with same media download pattern
- [Phase 13]: REST relay returns 3-tuple (text, error, media_blocks) to carry image blocks for Phase 14
- [Phase 13]: _extract_response_content handles nested toolResponse images for tool screenshot capture
- [Phase 13]: Streaming relay reuses _StreamBuffer pattern from WS relay with identical flush semantics
- [Phase 13]: _relay_to_goose_web returns 3-tuple (text, error, media_blocks), all 15 call sites updated
- [Phase 13]: WS relay code fully removed (~280 lines), REST /reply + SSE is the sole relay path
- [Phase 13]: content_blocks from InboundMessage media flow through BotInstance, ChannelRelay, and legacy poll loop
- [Phase 14]: Multipart boundary uses uuid.uuid4().hex, caption truncated to 1024 in _send_media, images >10MB route to send_file
- [Phase 14]: OutboundAdapter base class send_image/send_voice/send_file accept **kwargs for subclass signature compat
- [Phase 14]: notify_all uses try/except TypeError for backward-compat media kwarg passing to old handlers
- [Phase 14]: Media routing placed after text delivery in all relay paths, own try/except prevents media errors from crashing text delivery
- [Phase 15]: Import gateway classes via sys.modules __main__ then direct import then fallback stubs for cross-context compatibility
- [Phase 15]: v2 channel plugin pattern: CHANNEL dict with name, version=2, send, adapter, poll, credentials, setup

### Pending Todos

- Lock audit: map all 17 locks and their acquisition paths before Phase 6 refactor
- /clear scoping: decide per-user session clear vs documented limitation
- Test threading scenarios before extraction (relay+clear, relay+stop)
- Auto-detect timezone from location in setup wizard (ui)
- Queue consecutive messages instead of bouncing with "Still thinking" (general)

### Blockers/Concerns

- /clear restarts goose web, nuking ALL sessions -- must scope before multi-bot ships
- Session model state is in-memory only, lost on goose web restart
- Python stdlib only constraint limits concurrency primitives

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 1 | Setup wizard settings dashboard | 2026-03-10 | 720048c | quick/1-.../ |
| 2 | Add expires_at to job engine | 2026-03-13 | ff87edd | quick/2-.../ |
| 3 | Make pairing codes single-use + rotate | 2026-03-13 | 4c09ee4 | quick/3-.../ |
| 4 | Memory writer dedup, section routing, learnings format | 2026-03-13 | a4e298a | (parallel agent) |
| 5 | Media message reply (canned text for non-text input) | 2026-03-13 | a4e298a | (parallel agent) |
| 6 | Extract onboarding flow to separate file | 2026-03-13 | 26b4ef2 | (parallel agent) |
| 7 | Replace auto-generated auth token with user-set password | 2026-03-13 | 016c11f | [4-replace-auto-gen...](./quick/4-replace-auto-generated-auth-token-with-u/) |
| 7 | Replace auto-generated auth token with password auth | 2026-03-13 | 016c11f | quick/4-.../ |
| 8 | Add /status command showing context window, provider, session info | 2026-03-14 | 1dfacbe | [5-add-status...](./quick/5-add-status-command-showing-context-windo/) |

## Session Continuity

Last session: 2026-03-13
Stopped at: Completed quick task 4 (password auth). 472 tests passing. Password login page, cookie sessions, no more auto-generated tokens.
Resume file: None

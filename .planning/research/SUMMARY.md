# Project Research Summary

**Project:** GooseClaw v2.0 Multi-Channel & Multi-Bot Support
**Domain:** AI agent gateway with multi-platform messaging and multi-bot routing
**Researched:** 2026-03-13
**Confidence:** HIGH

## Executive Summary

GooseClaw v2.0 is a refactoring story, not a greenfield build. The existing gateway already has the core patterns needed for multi-channel and multi-bot support: session isolation via goose web sessions, per-user locks and cancellation for Telegram, a channel plugin system with hot-reload, and per-channel model routing. The problem is that these capabilities are hardcoded in the Telegram poll loop rather than extracted into shared infrastructure. Channel plugins have zero command routing, no per-user locks, and no cancellation support. The v2 goal is to extract Telegram's battle-tested patterns into shared components (SessionManager, CommandRouter) that both Telegram and channel plugins can use, then layer multi-bot on top.

The recommended approach is a strict extraction-first strategy. No new libraries are needed (Python stdlib only). The architecture stays single-process, multi-threaded, with one goose web instance providing session isolation via session IDs. Multi-bot support means N Telegram polling loops, each with its own token and model route, sharing the same SessionManager and CommandRouter infrastructure. The config schema extends naturally: a `bots` array in setup.json, backward-compatible with the existing single `telegram_bot_token` field.

The top risks are concurrency-related. The codebase has 17 threading locks with no documented ordering hierarchy. The recent deadlock bug (commit `071fb00`) proves this is a real threat, not theoretical. Additionally, `/clear` currently restarts the entire goose web process, nuking ALL sessions across ALL channels. This must be scoped to per-user session clearing before multi-bot ships, or one user's `/clear` will destroy every other user's conversation. Session model state is also fragile: it lives in memory and is lost on goose web restarts, causing silent model misrouting.

## Key Findings

### Recommended Stack

No new dependencies. The entire v2 is built with Python stdlib already in use. The "stack" is three new internal abstractions: a `CommandRouter` (function registry dispatching /help, /stop, /clear, /compact), a `SessionManager` (unified session lifecycle with composite `(channel, user_id)` keys, per-user locks, relay tracking, and cancellation), and a `BotInstance`/`BotManager` pair for multi-bot lifecycle.

**Core technologies (all already in use):**
- `threading` (stdlib): multi-bot polling loops, per-user locks. Already handles single Telegram loop. Each bot gets a daemon thread.
- `json` (stdlib): session and config persistence. setup.json schema extends to `bots` array.
- `http.client` (stdlib): WebSocket relay to goose web. Sessions are the isolation boundary.
- `dataclasses` (stdlib): BotInstance/SessionKey structs. Cleaner than raw dicts for multi-bot state.

**Critical version requirement:** Python 3.10+ (Ubuntu 22.04 default). All stdlib features used are available since 3.7.

### Expected Features

**Must have (table stakes):**
- Shared command router: extract /help, /stop, /clear, /compact from Telegram poll loop into a reusable module. Foundation for everything else.
- Per-user session locks in ChannelRelay: copy pattern from Telegram's `_telegram_chat_locks`. Without this, channel plugins race under any real load.
- Cancellation support for all channels: track active WebSocket refs per user in ChannelRelay, wire to shared /stop command.
- Multi-bot Telegram instances: N poll loops, each with own token, paired users, and session scope. The headline feature.
- Per-bot provider/model config: extend `channel_routes` to `telegram:bot_name` keys.
- Dynamic notification bus channel validation: make `valid_channels` read from loaded plugins.
- Channel plugin command registration: optional `commands` key in CHANNEL dict.
- Typing indicators for plugins: optional `typing` callback in CHANNEL dict.

**Should have (differentiators):**
- Per-bot personality/system prompt (soul.md per bot)
- Hot-add/remove bots without restart
- Channel-aware memory writer (learn from ALL channels)

**Defer to v2+:**
- Cross-channel session continuity (requires user identity layer that doesn't exist)
- Webhook mode for Telegram (only matters at 5+ bots)
- Plugin marketplace / community plugins (content work, not architecture)
- Cross-channel message bridging (anti-feature: GooseClaw is not Matterbridge)
- Platform-specific rich UI abstraction (anti-feature: keep abstraction text-only)
- Per-message provider switching (anti-feature: breaks session state)

### Architecture Approach

The architecture is extraction-based: pull existing Telegram patterns into shared components, wire both Telegram and ChannelRelay to use them, then layer multi-bot on top. Single goose web process remains the constraint. Session isolation via session IDs, model routing via per-session `_update_goose_session_provider`. No asyncio migration, no process-per-bot, no ABC inheritance. Telegram stays as built-in code (not a plugin file) but uses shared abstractions.

**Major components:**
1. **SessionManager** (NEW): unified per-user session lifecycle. Replaces `_telegram_sessions`, `_telegram_chat_locks`, `_telegram_active_relays`. Composite key `(channel, user_id)`.
2. **CommandRouter** (NEW): function registry for slash commands. Replaces hardcoded if/elif chain in `_telegram_poll_loop`. Returns `(text, should_relay)` for channel-agnostic dispatch.
3. **ChannelRelay v2** (MODIFIED): wraps channel plugins with full parity. Commands, locks, cancellation, model routing. Plugin code unchanged.
4. **BotInstance + BotManager** (NEW): per-bot config + polling loop. `BotManager` reads `bots` array from setup.json, manages N `BotInstance` objects.
5. **_relay_to_goose_web** (UNCHANGED): already channel-agnostic. Handles model routing via channel param.

### Critical Pitfalls

1. **Lock re-entrancy deadlocks** -- `threading.Lock` is NOT reentrant. Already caused a production deadlock (commit `071fb00`). Use `RLock` for any lock where nested acquisition is possible. Add `timeout=5` to all `.acquire()` calls.
2. **Global /clear nukes all channels** -- `/clear` restarts goose web, killing ALL sessions across ALL channels and bots. Must scope to per-user session clearing. Drain active relays before restart.
3. **Session model state lost on restart** -- `_session_model_cache` is in-memory only. After goose web restart, all sessions fall back to wrong model. Persist model cache to disk. Re-apply model routing before accepting messages post-restart.
4. **Telegram-hardcoded globals leak into shared layer** -- 132 references to `_telegram_` in gateway.py. Functions like `_relay_to_goose_web` do `_telegram_sessions[chat_id]` internally. Must parameterize before abstracting.
5. **Lock ordering violations with multi-channel locks** -- 17 module-level locks with no documented ordering. Define hierarchy, enforce acquisition order, use timeouts everywhere.

## Implications for Roadmap

Based on combined research, the dependency graph dictates a strict 5-phase structure. Each phase is independently deployable and testable.

### Phase 1: Extract Shared Infrastructure

**Rationale:** Every subsequent phase depends on SessionManager and CommandRouter existing. This phase changes zero behavior. Pure extraction refactor. Lowest risk, highest unlock.
**Delivers:** SessionManager class, CommandRouter class, both used by Telegram (existing behavior preserved).
**Addresses:** Foundation for shared command router (table stakes), per-user session locks (table stakes).
**Avoids:** Pitfall 4 (telegram-hardcoded globals) by parameterizing session access. Pitfall 1 (lock re-entrancy) by switching to RLock during extraction. Pitfall 5 (lock ordering) by documenting hierarchy.
**Notes:** Write threading tests BEFORE this refactor (Pitfall 13). Add concurrency tests for relay + clear, relay + stop paths.

### Phase 2: Channel Plugin Parity

**Rationale:** Depends on Phase 1 (shared components must exist). Channel plugins currently lack commands, locks, and cancellation. This makes them usable under real load.
**Delivers:** ChannelRelay v2 with full Telegram parity. Channel plugins gain /help, /stop, /clear, /compact without code changes.
**Addresses:** Per-user session locks in ChannelRelay (table stakes), cancellation support (table stakes), channel plugin command registration (table stakes), typing indicators (table stakes).
**Avoids:** Pitfall 8 (hot-reload breaks sessions) by draining relays before channel unload.

### Phase 3: Multi-Bot Telegram Support

**Rationale:** Depends on Phase 1+2 (shared SessionManager handles composite keys, CommandRouter dispatches for any channel name). This is the headline feature.
**Delivers:** BotInstance, BotManager, `bots` array in setup.json, per-bot polling loops, backward-compatible single-bot mode.
**Addresses:** Multi-bot Telegram instances (table stakes), per-bot provider/model config (table stakes).
**Avoids:** Pitfall 2 (global restart) by scoping /clear to per-user. Pitfall 3 (session pollution) by persisting model cache and using composite keys. Pitfall 7 (notification bus) by adding bot scope to handlers.

### Phase 4: Multi-Bot Ecosystem

**Rationale:** Depends on Phase 3 (bots must exist before they can have personalities, hot-reload, or scoped memory). These are differentiators, not blockers.
**Delivers:** Per-bot personality/system prompt, hot-add/remove bots, notification bus multi-bot targeting, scoped memory writer.
**Addresses:** Per-bot personality (differentiator), hot-add/remove bots (differentiator), channel-aware memory writer (differentiator).
**Avoids:** Pitfall 10 (memory writer cross-bot bleed) by scoping learnings directory by bot_id. Pitfall 9 (config quadratic growth) by using hierarchical config with defaults + overrides.

### Phase 5: Admin and Observability

**Rationale:** Polish phase. The system works without this, but operators need visibility. Can be done in parallel with Phase 4.
**Delivers:** Admin dashboard channel/bot status, dynamic notification bus validation, per-bot rate limiting, bot token masking in logs.
**Addresses:** Dynamic notification bus validation (table stakes), admin dashboard (differentiator).
**Avoids:** Pitfall 11 (token exposure) by masking in logs. Pitfall 12 (rate limiter scope) by adding per-bot limits.

### Phase Ordering Rationale

- **Phase 1 before everything:** SessionManager and CommandRouter are the dependency root. Every other phase uses them. Doing this as a zero-behavior-change extraction means it's safe to ship independently.
- **Phase 2 before Phase 3:** Channel plugin parity validates the shared abstractions with a simpler use case (upgrading existing ChannelRelay) before tackling multi-bot (adding entirely new BotInstance/BotManager). If the abstractions are wrong, Phase 2 reveals it cheaply.
- **Phase 3 is the value delivery:** Multi-bot is the headline feature. Phases 1-2 are infrastructure that enables it. Don't let Phases 4-5 block the Phase 3 ship.
- **Phases 4-5 are incremental:** Each feature in these phases is independently valuable and can ship as individual PRs.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 1:** Needs careful lock audit of all 17 locks before refactoring. Map every function's lock acquisition path. This is tedious but prevents deadlocks.
- **Phase 3:** The `/clear` scoping problem needs investigation. How does goose web handle session-level clearing vs process restart? Check goose Discussion #4389 for per-session isolation progress.
- **Phase 3:** Telegram 409 Conflict behavior with multiple tokens polling simultaneously needs validation (documented in theory, untested in this codebase).

Phases with standard patterns (skip research-phase):
- **Phase 2:** Straightforward upgrade of ChannelRelay. The target API is fully designed in ARCHITECTURE.md. Copy patterns from Telegram's existing implementation.
- **Phase 5:** Standard observability work. No novel patterns needed.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | No new dependencies. All recommendations are internal abstractions using existing stdlib. Verified against 6207-line gateway.py source. |
| Features | HIGH | Feature list derived from established multi-channel frameworks (Hubot, Matterbridge, Matrix bridges) cross-referenced with actual codebase gaps. Anti-features clearly identified. |
| Architecture | HIGH | Component design based on direct code analysis. Every proposed extraction maps to specific line ranges in gateway.py. Migration path is incremental with zero behavior changes in Phase 1. |
| Pitfalls | HIGH | Top pitfalls sourced from actual production bugs (commit `071fb00` deadlock, commit `caab970` /clear hang). Threading risks verified against Python docs and established concurrency literature. |

**Overall confidence:** HIGH

All four research files are based on primary source analysis of the actual codebase rather than hypothetical patterns. The recommendations are conservative (extract, don't rewrite) which reduces risk.

### Gaps to Address

- **/clear scoping:** The exact mechanism for per-user session clearing (vs global goose web restart) is unresolved. Goose upstream may or may not support per-session provider cleanup. Needs investigation during Phase 3 planning.
- **Goose web concurrent session limits:** Unknown how many concurrent WebSocket sessions goose web handles before degradation. With multi-bot, this could be 10-20+ concurrent sessions. Needs load testing during Phase 3.
- **Setup wizard UI for multi-bot:** The single-HTML-file constraint makes multi-bot configuration UI design non-trivial. No research was done on the setup wizard changes needed.
- **Backward compatibility testing:** The migration from single-bot to multi-bot config schema needs a concrete migration function. Pattern exists (`migrate_config_models()`) but the specific migration logic hasn't been designed.
- **Pairing flow per-bot:** Each bot needs its own pairing flow. Current pairing generates a code, user sends it to "the" bot. With multi-bot, each bot has its own pair code namespace. The UX for this is undesigned.

## Sources

### Primary (HIGH confidence)
- `/Users/haseeb/nix-template/docker/gateway.py` (6207 lines) -- all architectural claims verified against source
- `/Users/haseeb/nix-template/docker/test_gateway.py` (969 lines) -- test coverage assessment
- `/Users/haseeb/nix-template/.planning/PROJECT.md` -- project constraints, scope boundaries
- `/Users/haseeb/nix-template/.planning/REQUIREMENTS.md` -- v1/v2 requirements
- Git history (commits `071fb00`, `caab970`, `a369dfc`, `d45d9fe`) -- production bug patterns

### Secondary (MEDIUM confidence)
- [Matterbridge](https://github.com/42wim/matterbridge) -- multi-protocol bridge design patterns
- [Hubot adapter pattern](https://hubot.github.com/docs/) -- channel abstraction, command routing
- [Matrix bridge types](https://matrix.org/docs/older/types-of-bridging/) -- bridging architecture patterns
- [Botpress multi-channel](https://botpress.com/blog/botpress-vs-rasa) -- table stakes for channel abstraction
- [Rasa custom connectors](https://rasa.com/docs/reference/channels/custom-connectors/) -- plugin contract design

### Tertiary (LOW confidence)
- [Goose Discussion #4389](https://github.com/block/goose/discussions/4389) -- per-session agent isolation (upstream, unverified timeline)
- [AI Gateway architecture patterns 2026](https://www.truefoundry.com/blog/a-definitive-guide-to-ai-gateways-in-2026-competitive-landscape-comparison) -- general AI gateway patterns

---
*Research completed: 2026-03-13*
*Ready for roadmap: yes*

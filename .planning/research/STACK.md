# Stack Research: Multi-Channel & Multi-Bot Support

**Domain:** Multi-channel command routing, per-user session isolation, multi-bot process management
**Researched:** 2026-03-13
**Confidence:** HIGH

This is a purely architectural research document. GooseClaw uses Python stdlib only (no pip). Every recommendation here is about patterns and internal structure, not new dependencies.

## Executive Summary

The existing codebase is 80% of the way there. The channel plugin system (`ChannelRelay`, notification bus, hot-reload) already provides session isolation and relay for plugins. The problem is that **Telegram is a special snowflake**: it's hardcoded in gateway.py with its own session store, command handling, and relay logic rather than using the plugin system. Multi-bot support means running N Telegram polling loops, each with its own token, paired users, and (optionally) model routing.

The stack doesn't need new libraries. It needs refactoring: extract a shared command router, unify session storage behind a single interface, and make Telegram a "privileged plugin" that uses the same abstractions as channel plugins.

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python stdlib `threading` | 3.10+ | Multi-bot polling loops, per-user locks | Already used for single Telegram loop. Each bot gets its own daemon thread. No asyncio migration needed |
| Python stdlib `json` | 3.10+ | Session/config persistence | Already the persistence layer. setup.json schema extends naturally to multi-bot |
| Python stdlib `http.client` | 3.10+ | WebSocket relay to goose web | Already used for all goose web communication. Sessions are the isolation boundary, not processes |
| Python stdlib `importlib.util` | 3.10+ | Channel plugin loading | Already used. No changes needed for multi-bot |

### Internal Abstractions (New Code, Not Libraries)

| Abstraction | Purpose | Why It's Needed |
|-------------|---------|-----------------|
| `CommandRouter` | Shared slash command dispatch for all channels | Currently /help, /stop, /clear, /compact are hardcoded in `_telegram_poll_loop`. Channel plugins have NO command support. Without this, every new channel re-implements commands from scratch |
| `SessionStore` | Unified per-user session persistence | Currently split: `_telegram_sessions` (global dict + file) vs `ChannelRelay._sessions` (per-plugin dict + file). Multi-bot needs a third variant. Unify into one keyed by `(channel, user_id)` |
| `BotInstance` | Per-bot config + polling loop + session scope | Currently a single `start_telegram_gateway(token)` call. Multi-bot needs N instances, each with its own token, paired users, model route, and sessions |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `dataclasses` | stdlib 3.7+ | BotInstance/SessionKey structs | Use for config objects. Cleaner than dicts for multi-bot state. Already available in Python 3.10 |
| `enum` | stdlib | Channel types, command types | Use for valid_channels enum instead of hardcoded tuples. Prevents the current problem of `valid_channels` being repeated in 3 places |
| `typing.NamedTuple` | stdlib | Session keys `(channel, user_id)` | Composite keys for the unified session store. Hashable, immutable |

## Architecture Decisions

### Decision 1: Single Goose Web Process (Confirmed)

PROJECT.md explicitly says "Multiple goose web processes -- single process, sessions provide isolation" is out of scope. This is correct. Goose web already supports multiple concurrent sessions via `/agent/start` and session-scoped WebSocket connections. Multi-bot support means more sessions, not more processes.

**Implication:** Each bot's messages go to separate goose web sessions. Per-channel model routing (`_update_goose_session_provider`) already handles switching providers per session. Multi-bot just adds more entries to the routing table.

### Decision 2: Telegram as Privileged Plugin, Not Generic Plugin

Do NOT move Telegram into `/data/channels/`. It's too tightly integrated (pairing flow, BotFather instructions in setup wizard, goose config.yaml gateway_pairings, admin dashboard status). Instead, make Telegram's internal code use the same abstractions that plugins use.

**Pattern:** Telegram stays in gateway.py but uses `CommandRouter` and `SessionStore`. Channel plugins also use `CommandRouter` and `SessionStore` via the existing `ChannelRelay` class. Both converge on the same interfaces without Telegram becoming a plugin file.

### Decision 3: Multi-Bot via Config Array

Current setup.json has a single `telegram_bot_token`. Multi-bot extends this to a `bots` array:

```python
# Current (v1)
{
    "telegram_bot_token": "123:ABC",
    "channel_routes": {"telegram": "model_id_1"},
}

# Multi-bot (v2)
{
    "telegram_bot_token": "123:ABC",  # kept for backward compat (primary bot)
    "bots": [
        {
            "id": "primary",
            "token": "123:ABC",
            "name": "My Main Bot",
            "model_route": "claude_sonnet_4",  # model ID from models array
            "paired_users": ["chat_id_1", "chat_id_2"],
        },
        {
            "id": "code_bot",
            "token": "456:DEF",
            "name": "Code Helper",
            "model_route": "deepseek_coder",
            "paired_users": [],  # uses own pairing flow
        },
    ],
    "channel_routes": {"telegram": "model_id_1", "telegram:code_bot": "deepseek_coder"},
}
```

**Why this shape:**
- Backward compatible. If `bots` is missing, fall back to `telegram_bot_token`.
- Each bot has its own model route. No need to share channels routes for this.
- `channel_routes` extends to `telegram:<bot_id>` for per-bot routing.
- Paired users are per-bot, not global. Different bots serve different people.

### Decision 4: Command Router as a Function Registry, Not a Class Hierarchy

```python
# Pattern: register commands, dispatch by channel
_commands = {}  # command_name -> {"handler": fn, "description": str}

def register_command(name, handler, description):
    _commands[name] = {"handler": handler, "description": description}

def dispatch_command(command, context):
    """context = {"channel": str, "user_id": str, "bot_id": str|None, "send_fn": callable}"""
    cmd = _commands.get(command)
    if cmd:
        return cmd["handler"](context)
    return None  # not a command, relay to goose

# Register once at startup
register_command("help", _handle_help, "Show available commands")
register_command("stop", _handle_stop, "Cancel the current response")
register_command("clear", _handle_clear, "Wipe conversation and start fresh")
register_command("compact", _handle_compact, "Summarize history to save tokens")
```

**Why function registry over class hierarchy:** The existing codebase uses function-based patterns everywhere. No classes for telegram logic. A dict-based registry matches the existing style and is trivially extensible by channel plugins that want to add their own commands.

### Decision 5: Session Store Keyed by (channel, user_id)

```python
# Current: two separate stores
_telegram_sessions = {}           # chat_id -> session_id
ChannelRelay._sessions = {}       # user_id -> session_id (per-plugin instance)

# Unified: one store, composite key
_sessions = {}                    # (channel_name, user_id) -> session_id
# Examples:
# ("telegram", "12345") -> "tg_12345_20260313_120000"
# ("telegram:code_bot", "12345") -> "tg_code_bot_12345_20260313_120000"
# ("slack", "U1234") -> "slack_U1234_20260313_120000"
```

**Why composite key:**
- Same user on different channels gets isolated sessions (correct behavior).
- Same user talking to different bots gets isolated sessions (critical for multi-bot).
- Single persistence file instead of N scattered files.
- `ChannelRelay` becomes a thin wrapper that calls the unified store.

## What NOT to Add

### Avoid: asyncio Migration

**Why it's tempting:** Multi-bot polling means N concurrent network loops. asyncio handles this more elegantly than threading.

**Why avoid it:** Gateway.py is 6000+ lines of synchronous threading code. Migrating to asyncio would touch every function that does I/O. The threading model works fine. Python threads handle I/O-bound polling perfectly. The GIL is irrelevant for network-waiting threads.

### Avoid: Process-Per-Bot

**Why it's tempting:** Full isolation. One bot crashing doesn't affect others.

**Why avoid it:** Single goose web process is a hard constraint. Each bot's relay goes to the same goose web anyway. Process-per-bot adds IPC complexity (shared config, shared notification bus) with no real isolation benefit.

### Avoid: SQLite for Session Storage

**Why it's tempting:** Proper relational storage, query capability, ACID transactions.

**Why avoid it:** JSON file persistence is the codebase pattern. Atomic write via `os.replace()` on tmp file is already battle-tested. Session data is small (hundreds of entries max). SQLite adds the `sqlite3` import and schema migration concerns for no practical benefit.

### Avoid: Plugin Autodiscovery for Commands

**Why it's tempting:** Let plugins register their own slash commands.

**Why avoid it (for now):** The command set is small (/help, /stop, /clear, /compact). Plugin commands would need sandboxing, help text aggregation, and conflict resolution. Build the registry pattern now, but only let gateway.py register commands in v2. Plugin command registration can come in v3.

### Avoid: Abstract Base Classes (ABC)

**Why it's tempting:** Define `ChannelInterface`, `BotInterface` ABCs for type safety.

**Why avoid it:** The existing codebase uses duck typing and dict-based contracts (see `CHANNEL` dict). ABCs would be a style break. The `CHANNEL` dict contract is simple and works. Keep it.

### Avoid: Event Bus / Pub-Sub Pattern

**Why it's tempting:** Decouple command routing from channel logic.

**Why avoid it:** The notification bus (`register_notification_handler` + `notify_all`) is already the right level of decoupling. Adding a generic event bus is over-engineering for 4 command handlers and 3-5 channels.

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| Function-based command registry | Class-based command pattern (Command objects) | If commands need undo/redo or complex state. Not the case here |
| Composite key session store `(channel, user_id)` | Per-channel session files (current pattern) | If channels need fully independent session lifecycle. But they don't -- all sessions talk to the same goose web |
| `bots` array in setup.json | Separate config file per bot | If bot configs become very large. Unlikely with just token + model route + name |
| Threading for multi-bot | asyncio rewrite | If gateway.py were being written from scratch. It's not. 6000 lines of sync code |
| Telegram stays in gateway.py | Telegram as a channel plugin file | If we were willing to break the setup wizard, admin dashboard, and pairing flow. We're not |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `asyncio` | Would require rewriting 6000+ lines of synchronous code | `threading` (already works, I/O bound) |
| `sqlite3` | Overkill for session storage. Adds migration concerns | JSON files with atomic `os.replace()` |
| `multiprocessing` | Process-per-bot adds IPC for no benefit (single goose web) | `threading` (daemon threads per bot) |
| Third-party bot frameworks (python-telegram-bot, etc.) | pip is not available in the container | Raw urllib + Telegram Bot API (already works) |
| Abstract Base Classes | Style break with existing dict-based contracts | Duck typing + CHANNEL dict convention |

## Stack Patterns by Variant

**If adding just channel parity (no multi-bot):**
- Extract `CommandRouter` and have Telegram use it
- Keep `_telegram_sessions` as-is, just add command dispatch
- This is the minimal useful change

**If adding multi-bot support:**
- Must unify session storage (can't have N telegram session dicts)
- Must parameterize `_telegram_poll_loop` to accept bot config
- `BotInstance` struct becomes necessary to hold per-bot state
- `channel_routes` extends to `telegram:<bot_id>` namespace

**If adding both (recommended):**
- Do channel parity first (extract CommandRouter)
- Then multi-bot builds on the unified session store
- This ordering avoids rework

## Version Compatibility

| Component | Compatible With | Notes |
|-----------|-----------------|-------|
| Python 3.10+ | `dataclasses`, `typing.NamedTuple` | Both available since 3.7. Ubuntu 22.04 ships Python 3.10 |
| `threading.Lock` | All multi-bot state | Already used throughout. No compatibility concerns |
| `os.replace()` | Atomic writes on Linux ext4, overlay2 (Docker) | Already proven in codebase. Works on Railway's Docker |
| Telegram Bot API | Long-polling with multiple tokens | Each token polls independently. No conflicts. Telegram rate limits are per-bot-token, so N bots get N limits |

## Migration Path

The critical insight: this is a refactoring story, not a "new library" story. The existing code already does everything. It just does it in a Telegram-specific way.

### Phase ordering implication:

1. **Extract shared abstractions** (CommandRouter, unified SessionStore) without changing behavior
2. **Wire Telegram to use shared abstractions** (same behavior, cleaner code)
3. **Wire ChannelRelay to use shared abstractions** (channel parity achieved)
4. **Add multi-bot config schema** and parameterize Telegram polling loop
5. **Add per-bot model routing** via extended `channel_routes`

Each phase is independently testable and shippable.

## Key Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Session collision between bots | Medium | High (messages go to wrong bot's context) | Composite key `(channel:bot_id, user_id)` makes collisions impossible |
| Telegram rate limiting with N bots | Low | Medium (temporary 429s) | Each bot has its own rate limit. Only a concern if user runs 10+ bots |
| Config migration from v1 to v2 schema | High (guaranteed) | Medium (broken on upgrade) | Backward compat: if `bots` missing, synthesize from `telegram_bot_token`. Already done for `models` array migration |
| goose web session exhaustion | Low | High (all bots stop working) | goose web handles many sessions. Monitor via existing health check. Add session count metric |
| /clear restarts goose web (kills all bots' sessions) | High (existing behavior) | High (all users lose context) | Scope /clear to session-level clear, not process restart. Or warn user that /clear affects all connected users |

## Sources

- `/Users/haseeb/nix-template/docker/gateway.py` (lines 127-178: state declarations, 2697-2940: channel plugin system, 3188-3360: telegram session management, 4230-4528: telegram polling loop, 5387-5437: route handling) -- PRIMARY source, all claims verified against code
- `/Users/haseeb/nix-template/.planning/PROJECT.md` -- constraints and out-of-scope declarations
- Telegram Bot API documentation -- multiple bot tokens poll independently, rate limits are per-token

---
*Stack research for: GooseClaw v2.0 Multi-Channel & Multi-Bot*
*Researched: 2026-03-13*

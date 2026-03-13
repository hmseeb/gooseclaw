# Phase 9: Multi-Bot Core - Research

**Researched:** 2026-03-13
**Domain:** Telegram multi-bot architecture on single GooseClaw gateway
**Confidence:** HIGH

## Summary

GooseClaw currently runs a single Telegram bot using module-level globals: one `_telegram_running` flag, one `telegram_pair_code`, one `_telegram_state` (ChannelState), and one notification handler registered as `"telegram"`. The poll loop (`_telegram_poll_loop`) takes a `bot_token` arg but hardcodes `"telegram"` as the channel name everywhere. All session management goes through the shared `_session_manager` with channel key `"telegram"`.

To support multiple bots, we need a `BotInstance` class that encapsulates per-bot state (ChannelState, pair code, running flag, bot token, name) and a `BotManager` that creates/tracks/stops multiple BotInstance objects. The poll loop becomes a method on BotInstance, using `"telegram:<bot_name>"` as the channel key for session manager lookups. Channel routes get extended to support bot-scoped keys (e.g., `"telegram:research_bot"` maps to a specific model). Backward compatibility: when `setup.json` has `telegram_bot_token` but no `bots` array, we auto-create a default BotInstance named `"default"` using channel key `"telegram"` (no migration needed).

**Primary recommendation:** Create BotInstance class wrapping per-bot state, BotManager to coordinate them, and extend channel routing to use `"telegram:<name>"` keys. Keep the default bot using `"telegram"` as its channel key for zero-migration backward compatibility.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| BOT-01 | Configure multiple bots in setup.json `bots` array | Schema extension pattern documented below. Validation via `validate_setup_config`. |
| BOT-02 | Each bot runs its own poll loop with independent session store and pair codes | BotInstance class encapsulates poll loop, ChannelState, pair code. Session isolation via channel key `"telegram:<name>"`. |
| BOT-03 | Per-user session locks and active relay tracking scoped per-bot | Each BotInstance owns its own ChannelState (already has user locks + relay tracking). |
| BOT-04 | Each bot routes to its own LLM provider/model via channel_routes | `get_model_for_channel` already takes channel name. Use `"telegram:<name>"` as the key. |
| BOT-07 | Existing single-bot config backward-compatible as default bot | Default bot uses channel key `"telegram"` (unchanged). `bots` array absent = single-bot mode. |
</phase_requirements>

## Standard Stack

### Core
| Component | Purpose | Why Standard |
|-----------|---------|--------------|
| `BotInstance` class | Encapsulates per-bot state (token, name, ChannelState, pair code, running flag) | Natural OOP encapsulation. Same pattern as ChannelRelay per-channel state. |
| `BotManager` class | Creates, tracks, starts, stops BotInstance objects | Coordinator pattern. Needed for Phase 10 hot-add/remove. |
| `SessionManager` (existing) | Session storage with composite keys | Already supports multi-channel via `channel:user_id` keys. Use `"telegram:<name>"` as channel. |
| `ChannelState` (existing) | Per-user locks and relay tracking | Already fully encapsulated. One instance per BotInstance. |
| `CommandRouter` (existing) | Slash command dispatch | Shared across all bots. Context dict already has `channel_state` and `channel`. |

### Supporting
| Component | Purpose | When to Use |
|-----------|---------|-------------|
| `threading.Thread` | Per-bot poll loops | Each BotInstance starts its own daemon thread. |
| `threading.Lock` | BotManager bot registry | Protects concurrent access to bot dict. |

### No New Dependencies
This phase uses only stdlib threading and existing gateway classes. No new pip packages.

## Architecture Patterns

### Recommended Structure

```
# New classes added to gateway.py (no new files)

class BotInstance:
    """Encapsulates one Telegram bot's state and poll loop."""

    def __init__(self, name, token, channel_key=None):
        self.name = name                    # human-readable name from config
        self.token = token                  # Telegram bot token
        self.channel_key = channel_key or f"telegram:{name}"
        self.state = ChannelState()         # per-bot locks & relay tracking
        self.pair_code = None               # per-bot pairing code
        self.pair_lock = threading.Lock()
        self.running = False                # poll loop flag
        self._thread = None                 # poll thread reference

class BotManager:
    """Manages multiple BotInstance objects."""

    def __init__(self, session_manager):
        self._bots = {}                     # name -> BotInstance
        self._lock = threading.Lock()
        self._session_manager = session_manager

    def start_bot(self, name, token, channel_key=None): ...
    def stop_bot(self, name): ...
    def stop_all(self): ...
    def get_bot(self, name): ...
    def get_all(self): ...
```

### Pattern 1: Per-Bot Channel Key

**What:** Each bot uses a unique channel key for SessionManager lookups.
**When to use:** Always. This is the core isolation mechanism.

```python
# Default bot (backward compat): channel_key = "telegram"
# Named bots: channel_key = "telegram:<name>"

# In BotInstance poll loop:
session_id = _session_manager.get(self.channel_key, chat_id)
_session_manager.set(self.channel_key, chat_id, new_sid)

# Session persistence file: sessions_telegram.json (default), sessions_telegram:research.json (named)
```

**Why this works:** SessionManager already uses `channel:user_id` composite keys and persists per-channel. The `_save(channel)` method writes to `sessions_{channel}.json`. Using `"telegram:research"` produces `sessions_telegram:research.json` -- valid filename, distinct from `sessions_telegram.json`.

### Pattern 2: Per-Bot Notification Handlers

**What:** Each bot registers its own notification handler with a unique name.
**When to use:** Always. Enables targeted notifications per bot.

```python
# Default bot: register_notification_handler("telegram", handler)
# Named bots: register_notification_handler("telegram:<name>", handler)

def _make_bot_notify_handler(bot_instance):
    def handler(text):
        token = bot_instance.token
        # get paired chat IDs for THIS bot (filter by platform in config)
        chat_ids = get_paired_chat_ids()  # NOTE: currently not bot-scoped
        # send to all paired users of this bot
        ...
    return handler
```

### Pattern 3: Per-Bot Model Routing via channel_routes

**What:** `channel_routes` in setup.json uses bot-scoped keys for model routing.
**When to use:** When different bots should use different LLM providers/models.

```python
# setup.json example:
{
    "channel_routes": {
        "telegram": "anthropic_claude",          # default bot
        "telegram:research": "openai_gpt4o",     # research bot
        "telegram:code": "anthropic_opus",        # code bot
        "web": "anthropic_claude"
    }
}

# In _relay_to_goose_web, the channel param is already used:
model_cfg = get_model_for_channel(setup, channel)  # channel = "telegram:research"
```

**This already works** because `get_model_for_channel` does a dict lookup on `channel_routes[channel]`. No changes needed to the routing logic itself -- only the channel key needs to be correct.

### Pattern 4: Backward-Compatible Default Bot

**What:** When `setup.json` has `telegram_bot_token` but no `bots` array, create a default BotInstance.
**When to use:** Always. Zero migration required.

```python
# In apply_config / start_telegram_gateway:
def _resolve_bots(config):
    """Build bot configs from setup.json. Backward-compatible."""
    bots = config.get("bots", [])
    if bots:
        return bots

    # Legacy single-bot mode
    token = config.get("telegram_bot_token", "")
    if token:
        return [{"name": "default", "token": token}]
    return []

# Default bot uses channel_key="telegram" (not "telegram:default")
# This means existing sessions, routes, verbosity all work unchanged.
```

### Pattern 5: Per-Bot Pairing

**What:** Each BotInstance has its own pair code and pair lock.
**When to use:** Always. Users pair with a specific bot.

```python
# BotInstance generates its own pair code:
def generate_pair_code(self):
    code = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
    with self.pair_lock:
        self.pair_code = code
    return code

# Pairing writes to config.yaml with bot-specific platform tag:
# Default bot: platform: telegram
# Named bots: platform: telegram:<name>
# This distinguishes which bot a user is paired with
```

### Anti-Patterns to Avoid
- **Shared ChannelState across bots:** Each bot MUST have its own ChannelState. Otherwise one bot's `/stop` could kill another bot's relay for the same user.
- **Shared pair code:** Global `telegram_pair_code` is currently shared. Each bot needs its own.
- **Modifying SessionManager:** Don't change SessionManager internals. The channel key convention (`"telegram:<name>"`) gives isolation for free.
- **Breaking the poll loop signature:** `_telegram_poll_loop(bot_token)` becomes a method on BotInstance. Don't try to pass all state as arguments.
- **Hardcoding "telegram" channel name:** The poll loop currently hardcodes `"telegram"` in ~15 places. BotInstance.channel_key replaces all of them.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Session isolation | Custom session dict per bot | SessionManager with `"telegram:<name>"` key | Already handles persistence, thread safety, composite keys |
| Per-user locks | New lock dict per bot | ChannelState (existing class) | Already has user locks, relay tracking, kill_relay |
| Command dispatch | Per-bot command routing | Shared CommandRouter + context dict | Context already carries `channel_state` and `channel` |
| Model routing | Per-bot model lookup | `get_model_for_channel(config, "telegram:<name>")` | Already works with any channel key |

**Key insight:** Phases 6-8 built all the shared infrastructure. Multi-bot is mostly about instantiating existing classes per-bot and threading the right channel key through.

## Common Pitfalls

### Pitfall 1: Telegram 409 Conflict
**What goes wrong:** Two poll loops using the same bot token get 409 Conflict from Telegram API.
**Why it happens:** Telegram enforces one active `getUpdates` connection per token.
**How to avoid:** BotManager must enforce uniqueness -- never start two BotInstances with the same token. Validate at config time.
**Warning signs:** `[telegram] conflict (409)` in logs.

### Pitfall 2: Module-Level Globals Not Eliminated
**What goes wrong:** `_telegram_running`, `telegram_pair_code`, `_telegram_state` are still module-level. If not refactored into BotInstance, multi-bot breaks silently.
**Why it happens:** Easy to forget to update all 15+ references to these globals.
**How to avoid:** BotInstance must own these. Module-level vars become references to BotManager for backward-compat API functions like `get_bot_token()` and `_is_goose_gateway_running()`.
**Warning signs:** All bots share a pair code, or stopping one bot stops all.

### Pitfall 3: Memory Writer Hardcoded to "telegram"
**What goes wrong:** `_memory_writer_loop` at line 3730 calls `_session_manager.get("telegram", chat_id)`. For non-default bots, this returns None.
**Why it happens:** Memory writer was built before multi-bot.
**How to avoid:** Memory writer needs to iterate over all bot channel keys, or `_memory_touch` needs to record the channel key alongside chat_id.
**Warning signs:** Memory extraction works for default bot only, silently skipped for others.

### Pitfall 4: Notification Handler Name Collision
**What goes wrong:** All bots register as `"telegram"` in the notification bus. Only the last registration survives.
**Why it happens:** `register_notification_handler` does upsert by name.
**How to avoid:** Each bot registers with `"telegram:<name>"` (default bot keeps `"telegram"`).
**Warning signs:** Only one bot receives notifications.

### Pitfall 5: Pairing Platform Distinction
**What goes wrong:** `get_paired_chat_ids()` filters by `platform: telegram`. All bots return ALL paired users regardless of which bot they paired with.
**Why it happens:** Pairing entries in config.yaml don't distinguish bots.
**How to avoid:** Write pairing entries with `platform: telegram:<name>` for named bots. Default bot keeps `platform: telegram`.
**Warning signs:** A user paired with bot A can chat on bot B without pairing.

### Pitfall 6: `_get_valid_channels()` Doesn't Include Bot Channels
**What goes wrong:** `validate_setup_config` rejects `"telegram:research"` in `channel_routes` because it's not in the valid channels set.
**Why it happens:** `_get_valid_channels()` returns fixed set `{"web", "telegram", "cron", "memory"}`. Bot-scoped keys aren't included.
**How to avoid:** `_get_valid_channels()` must also include bot-scoped channel keys from setup.json `bots` array. Or: validate bot channel keys separately.
**Warning signs:** Config validation rejects valid bot routing entries.

## Code Examples

### BotInstance Class

```python
class BotInstance:
    """Encapsulates one Telegram bot's runtime state."""

    def __init__(self, name, token, channel_key=None):
        self.name = name
        self.token = token
        self.channel_key = channel_key or f"telegram:{name}"
        self.state = ChannelState()
        self.pair_code = None
        self.pair_lock = threading.Lock()
        self.running = False
        self._thread = None

    def generate_pair_code(self):
        code = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
        with self.pair_lock:
            self.pair_code = code
        print(f"[telegram:{self.name}] pairing code: {code}")
        return code

    def get_chat_lock(self, chat_id):
        return self.state.get_user_lock(chat_id)

    def start(self):
        if self.running:
            return
        _session_manager.load(self.channel_key)
        register_notification_handler(
            self.channel_key if self.channel_key != "telegram" else "telegram",
            self._make_notify_handler(),
        )
        self.generate_pair_code()
        self._register_commands()
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print(f"[telegram:{self.name}] started")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _poll_loop(self):
        """Long-poll telegram. Same logic as _telegram_poll_loop but uses self state."""
        # ... uses self.token, self.channel_key, self.state, self.pair_code
        pass

    def _make_notify_handler(self):
        def handler(text):
            chat_ids = get_paired_user_ids(self.channel_key)
            # ... send to each
        return handler

    def _register_commands(self):
        """Register slash commands with Telegram API for this bot."""
        # Same as current start_telegram_gateway command registration
        pass
```

### BotManager Class

```python
class BotManager:
    """Manages multiple BotInstance lifecycle."""

    def __init__(self):
        self._bots = {}       # name -> BotInstance
        self._lock = threading.Lock()

    def start_bot(self, name, token, channel_key=None):
        with self._lock:
            if name in self._bots:
                print(f"[bot-mgr] {name} already running")
                return self._bots[name]
            # Enforce unique tokens
            for existing in self._bots.values():
                if existing.token == token:
                    raise ValueError(f"token already in use by bot '{existing.name}'")
            bot = BotInstance(name, token, channel_key)
            self._bots[name] = bot
        bot.start()
        return bot

    def stop_bot(self, name):
        with self._lock:
            bot = self._bots.pop(name, None)
        if bot:
            bot.stop()

    def stop_all(self):
        with self._lock:
            bots = list(self._bots.values())
            self._bots.clear()
        for bot in bots:
            bot.stop()

    def get_bot(self, name):
        with self._lock:
            return self._bots.get(name)

    def get_all(self):
        with self._lock:
            return dict(self._bots)

    @property
    def any_running(self):
        with self._lock:
            return any(b.running for b in self._bots.values())
```

### Setup.json Schema Extension

```python
# New bots array in setup.json:
{
    "provider_type": "anthropic",
    "api_key": "sk-ant-...",
    "telegram_bot_token": "123:ABC",     # legacy single-bot (BOT-07)
    "bots": [                             # new multi-bot array (BOT-01)
        {
            "name": "main",
            "token": "123:ABC",
            "provider": "anthropic",      # optional override
            "model": "claude-sonnet-4-20250514"   # optional override
        },
        {
            "name": "research",
            "token": "456:DEF",
            "provider": "openai",
            "model": "gpt-4o"
        }
    ],
    "channel_routes": {
        "telegram": "anthropic_claude",          # default/main bot
        "telegram:research": "openai_gpt4o",     # research bot
        "web": "anthropic_claude"
    }
}
```

### Backward Compatibility in apply_config

```python
def _resolve_bot_configs(config):
    """Resolve bot configurations from setup.json. Backward-compatible."""
    bots = config.get("bots")
    if isinstance(bots, list) and bots:
        return bots

    # Legacy single-bot: telegram_bot_token field or env var
    token = config.get("telegram_bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return [{"name": "default", "token": token}]
    return []
```

### Validation Extension

```python
# Add to validate_setup_config:
bots = config.get("bots")
if bots is not None:
    if not isinstance(bots, list):
        errors.append("bots must be an array")
    else:
        seen_names = set()
        seen_tokens = set()
        for i, bot in enumerate(bots):
            if not isinstance(bot, dict):
                errors.append(f"bots[{i}] must be an object")
                continue
            name = bot.get("name", "")
            token = bot.get("token", "")
            if not name:
                errors.append(f"bots[{i}] missing name")
            if not token:
                errors.append(f"bots[{i}] missing token")
            if name in seen_names:
                errors.append(f"bots[{i}] duplicate name: {name!r}")
            if token in seen_tokens:
                errors.append(f"bots[{i}] duplicate token (two bots can't share a token)")
            seen_names.add(name)
            seen_tokens.add(token)
```

### _get_valid_channels Extension

```python
def _get_valid_channels():
    """Build valid channel names dynamically from fixed set + loaded plugins + bots."""
    fixed = {"web", "telegram", "cron", "memory"}
    with _channels_lock:
        plugin_names = set(_loaded_channels.keys())
    # Add bot-scoped channel keys
    bot_channels = set()
    try:
        setup = load_setup()
        if setup:
            for bot in setup.get("bots", []):
                name = bot.get("name", "")
                if name and name != "default":
                    bot_channels.add(f"telegram:{name}")
    except Exception:
        pass
    return fixed | plugin_names | bot_channels
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Module-level telegram globals | ChannelState per-instance | Phase 6 (INFRA-03) | Enables multi-bot by giving each bot its own ChannelState |
| Single `channel_routes` dict | Channel-keyed routing | Phase 4 (multi-model) | Enables per-bot model routing via `"telegram:<name>"` keys |
| Telegram-specific command handlers | Generalized handlers with context dict | Phase 7 (CHAN-01) | Command handlers already accept `channel_state` and `channel` in context |

**Key enabler from Phase 6:** ChannelState is already a self-contained class. The refactoring in Phase 6 (INFRA-03) specifically pulled telegram globals into per-instance state. This was designed to enable multi-bot.

## Open Questions

1. **Pairing Scope**
   - What we know: `get_paired_chat_ids()` reads from config.yaml `gateway_pairings` filtering by `platform: telegram`. Named bots should use `platform: telegram:<name>`.
   - What's unclear: Should a user paired with one bot automatically be paired with all bots? Or must they pair individually?
   - Recommendation: Per-bot pairing. Each bot has its own pair code and users must pair separately. This matches the security model (different bots may have different access).

2. **Memory Writer Multi-Bot Support**
   - What we know: Memory writer tracks `_memory_last_activity` by chat_id only, then looks up `_session_manager.get("telegram", chat_id)`.
   - What's unclear: Should memory extraction be per-bot or global?
   - Recommendation: Track `(channel_key, chat_id)` tuples in `_memory_last_activity` instead of just `chat_id`. This is a small change. Out of scope for this phase if desired, but the fix is trivial.

3. **API Endpoints**
   - What we know: `/api/telegram/status` returns one bot's status. `/api/telegram/pair` generates one pair code.
   - What's unclear: Should we extend these endpoints or add new ones?
   - Recommendation: Extend `/api/telegram/status` to return status of all bots (array). Add bot name to `/api/telegram/pair` as query param. Keep backward-compat: if no bot name specified, use default bot.

4. **Admin Dashboard**
   - What we know: admin.html shows one bot's status and pair code.
   - What's unclear: Does the dashboard need multi-bot UI?
   - Recommendation: Defer dashboard changes. Admin API returns data, dashboard can be updated later without code changes.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (stdlib) |
| Config file | none (no pytest.ini, tests use unittest directly) |
| Quick run command | `python3 -m unittest docker/test_gateway.py -v 2>&1 \| tail -5` |
| Full suite command | `python3 -m unittest discover -s docker -p "test_*.py"` |
| Estimated runtime | ~4 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| BOT-01 | Multi-bot config in setup.json `bots` array | unit | `python3 -m unittest docker.test_gateway.TestBotConfig -v` | No (Wave 0 gap) |
| BOT-02 | Each bot has own poll loop, session store, pair codes | unit | `python3 -m unittest docker.test_gateway.TestBotInstance -v` | No (Wave 0 gap) |
| BOT-03 | Per-bot session locks and relay tracking | unit | `python3 -m unittest docker.test_gateway.TestBotIsolation -v` | No (Wave 0 gap) |
| BOT-04 | Bot-scoped channel routes for model routing | unit | `python3 -m unittest docker.test_gateway.TestBotRouting -v` | No (Wave 0 gap) |
| BOT-07 | Backward-compatible single-bot config | unit | `python3 -m unittest docker.test_gateway.TestBotBackwardCompat -v` | No (Wave 0 gap) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m unittest discover -s docker -p "test_*.py"`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green (currently 185 tests) + new multi-bot tests all green
- **Estimated feedback latency per task:** ~4 seconds

### Wave 0 Gaps (must be created before implementation)

- [ ] `TestBotInstance` -- BotInstance init, start, stop, pair code generation, channel key
- [ ] `TestBotManager` -- start_bot, stop_bot, stop_all, duplicate token rejection, get_bot
- [ ] `TestBotConfig` -- validate `bots` array schema, duplicate name/token rejection, empty array
- [ ] `TestBotIsolation` -- different bots get different ChannelState, different session keys, one bot's lock doesn't affect another
- [ ] `TestBotRouting` -- `get_model_for_channel` with `"telegram:<name>"` key, channel_routes validation accepts bot keys
- [ ] `TestBotBackwardCompat` -- `_resolve_bot_configs` with only `telegram_bot_token`, default bot uses `"telegram"` channel key, existing routes/verbosity still work
- [ ] `TestBotNotification` -- each bot registers own notification handler, targeted delivery works
- [ ] `TestBotValidChannels` -- `_get_valid_channels()` includes bot-scoped keys from setup.json bots array
- [ ] `TestBotPairing` -- per-bot pair codes, pairing writes correct platform tag

Framework install: not needed (stdlib unittest already available)

## Sources

### Primary (HIGH confidence)
- Direct codebase analysis of `docker/gateway.py` (6435 lines)
- Direct codebase analysis of `docker/test_gateway.py` (2203 lines, 185 tests)
- Phase 6 RESEARCH.md and completed implementation (SessionManager, ChannelState, CommandRouter)
- Phase 7 implementation (ChannelRelay, generalized command handlers)

### Secondary (MEDIUM confidence)
- Telegram Bot API documentation (getUpdates 409 conflict behavior)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all classes and patterns already exist in codebase, this is assembly
- Architecture: HIGH -- pattern follows ChannelRelay precedent exactly, channel_key convention works with existing SessionManager
- Pitfalls: HIGH -- identified from direct code analysis (15+ hardcoded "telegram" references, memory writer coupling, valid channels set)

**Key metrics:**
- ~15 places in poll loop that hardcode `"telegram"` as channel name
- Module-level globals to refactor: `_telegram_running`, `telegram_pair_code`, `telegram_pair_lock`, `_telegram_state`
- Existing infrastructure that needs NO changes: SessionManager, ChannelState, CommandRouter, `get_model_for_channel`
- Existing infrastructure that needs SMALL changes: `_get_valid_channels()`, `validate_setup_config`, `apply_config`, `_memory_touch`

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (stable internal architecture)

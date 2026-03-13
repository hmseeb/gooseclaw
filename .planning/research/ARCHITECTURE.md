# Architecture Patterns: Multi-Channel & Multi-Bot Integration

**Domain:** Multi-channel command routing, per-user session isolation, multi-bot support
**Researched:** 2026-03-13
**Confidence:** HIGH (based on direct codebase analysis, no external deps needed)

## Current Architecture Summary

The gateway is a single-process, multi-threaded Python HTTP server (stdlib only). Everything runs in one `gateway.py` process with daemon threads for background work.

**Existing components and their session/command patterns:**

| Component | Session Store | Per-User Lock | Command Routing | Cancellation | Model Routing |
|-----------|--------------|---------------|-----------------|-------------|---------------|
| Telegram | `_telegram_sessions` dict | `_telegram_chat_locks` dict | Hardcoded `if/elif` chain in poll loop | `_telegram_active_relays` + cancelled Event | Via `_relay_to_goose_web` channel param |
| Channel Plugins | `ChannelRelay._sessions` per-plugin | None | None | None | Verbosity only, no model routing |
| Cron Jobs | Creates fresh session per fire | N/A | N/A | N/A | Via `_resolve_job_model` |
| Web UI | Goose web handles directly | N/A | N/A | N/A | Default model only |

**Key insight:** Telegram has 5 features that channel plugins lack. These are all implemented inline in `_telegram_poll_loop`, not extracted into reusable components. The v2 goal is to give channel plugins parity by extracting Telegram's features into shared infrastructure.

## Recommended Architecture

### Component Extraction Plan

The architecture change is fundamentally about **extracting, not rewriting**. The Telegram code already works. The job is to pull its patterns into shared components that both Telegram and channel plugins can use.

```
BEFORE (v1):
                                 +------------------+
                                 |  _telegram_poll   |
                                 |  loop (hardcoded) |
                                 |  - commands       |
                                 |  - sessions       |
                                 |  - locks          |
                                 |  - cancellation   |
                                 |  - model routing  |
                                 +--------+---------+
                                          |
  ChannelRelay (bare)                     v
  - sessions only  -------->  _do_ws_relay / _relay_to_goose_web
                              _do_ws_relay_streaming

AFTER (v2):
  +----------------+     +-------------------+
  | CommandRouter   |     | SessionManager    |
  | (shared)        |     | (shared)          |
  | - /help         |     | - get/create      |
  | - /stop         |     | - per-user locks  |
  | - /clear        |     | - active relays   |
  | - /compact      |     | - cancellation    |
  | - unknown catch |     | - prewarm         |
  +-------+--------+     +--------+----------+
          |                        |
          v                        v
  +------------------------------------------+
  |         _relay_to_goose_web              |
  |  (already handles model routing)         |
  +------------------------------------------+
          ^                        ^
          |                        |
  +-------+--------+     +--------+----------+
  | TelegramChannel |     | ChannelRelay v2   |
  | (uses shared    |     | (uses shared      |
  |  components)    |     |  components)      |
  +----------------+     +-------------------+
```

### Component Boundaries

| Component | Responsibility | Communicates With | New or Modified |
|-----------|---------------|-------------------|-----------------|
| `CommandRouter` | Match slash commands, dispatch to handlers, return response text | SessionManager, relay functions | **NEW** (extract from telegram poll loop) |
| `SessionManager` | Per-user session lifecycle: get/create/clear, per-user locks, active relay tracking, cancellation | `_relay_to_goose_web`, `_create_goose_session` | **NEW** (extract from telegram globals) |
| `ChannelRelay` v2 | Channel plugin wrapper. Uses CommandRouter + SessionManager instead of bare sessions | CommandRouter, SessionManager | **MODIFIED** (upgrade existing class) |
| `_telegram_poll_loop` | Telegram long-polling, pairing flow only. Delegates commands and relay to shared components | CommandRouter, SessionManager, Telegram API | **MODIFIED** (slim down, delegate) |
| `_relay_to_goose_web` | WebSocket relay with model routing, retry logic | goose web process | **UNCHANGED** (already handles channel param for model routing) |
| `BotInstance` | Multi-bot: holds bot token, paired chat IDs, bot-specific config | CommandRouter, SessionManager, Telegram API | **NEW** |
| `BotManager` | Registry of BotInstance objects. Start/stop/list bots | BotInstance, setup.json | **NEW** |

### Data Flow

#### Single message through upgraded ChannelRelay:

```
1. Channel plugin poll() receives message (user_id, text)
2. Channel plugin calls relay(user_id, text, send_fn)
3. ChannelRelay v2 checks: is this a slash command?
   YES -> CommandRouter.dispatch(command, channel_name, user_id, send_fn)
          CommandRouter calls SessionManager for /clear, /stop, /compact
          CommandRouter returns response text
          ChannelRelay sends response via send_fn
   NO  -> SessionManager.acquire_lock(channel_name, user_id)
          SessionManager.get_session(channel_name, user_id)
          SessionManager.register_active_relay(channel_name, user_id, sock_ref)
          _relay_to_goose_web(text, session_id, channel=channel_name)
          SessionManager.release_lock(channel_name, user_id)
```

#### Multi-bot Telegram message:

```
1. BotManager starts N poll loops (one per bot token)
2. Each poll loop receives message for its bot
3. Identifies which BotInstance owns this token
4. BotInstance resolves channel name: "telegram" or "telegram:bot_name"
5. Routes through same CommandRouter + SessionManager
6. Model routing uses channel_routes["telegram:bot_name"] from setup.json
```

## New Components: Detailed Design

### 1. SessionManager (extract from telegram globals)

**What it replaces:** `_telegram_sessions`, `_telegram_sessions_lock`, `_telegram_chat_locks`, `_telegram_active_relays`, `_prewarm_events`, and all functions that manipulate them.

```python
class SessionManager:
    """Manages sessions, per-user locks, active relays, and cancellation
    for ALL channels (telegram, plugins, future channels)."""

    def __init__(self, data_dir):
        self._sessions = {}        # (channel, user_id) -> session_id
        self._locks = {}           # (channel, user_id) -> threading.Lock
        self._active_relays = {}   # (channel, user_id) -> [socket, cancelled_event]
        self._prewarm_events = {}  # (channel, user_id) -> threading.Event
        self._data_dir = data_dir
        self._lock = threading.Lock()
        # load persisted sessions from /data/sessions_*.json

    def get_session(self, channel, user_id):
        """Get or create session. Waits for in-progress prewarm."""
        # Same logic as current _get_session_id but keyed by (channel, user_id)

    def clear_session(self, channel, user_id):
        """Kill active relay, clear session. Same as current _clear_chat."""

    def acquire_lock(self, channel, user_id, timeout=2):
        """Per-user lock. Returns True if acquired. Same as _get_chat_lock + acquire."""

    def release_lock(self, channel, user_id):
        """Release per-user lock."""

    def register_relay(self, channel, user_id, sock_ref):
        """Track active relay socket for cancellation."""

    def cancel_relay(self, channel, user_id):
        """Cancel active relay (for /stop). Sets cancelled flag, closes socket."""

    def prewarm(self, channel, user_id):
        """Background prewarm after /clear. Same as current _prewarm_session."""
```

**Key design decision:** Composite key `(channel_name, user_id)` instead of flat `chat_id`. This naturally isolates sessions across channels while sharing the infrastructure.

**Migration path:** Telegram keeps working during migration. Add SessionManager alongside existing globals. Telegram poll loop calls SessionManager methods which internally use the same logic. Once stable, remove the old globals.

### 2. CommandRouter (extract from telegram poll loop)

**What it replaces:** The hardcoded `if lower == "/help"` / `if lower == "/stop"` / etc. chain in `_telegram_poll_loop`.

```python
class CommandRouter:
    """Routes slash commands to handlers. Channel-agnostic."""

    COMMANDS = {
        "/help": "_handle_help",
        "/stop": "_handle_stop",
        "/clear": "_handle_clear",
        "/compact": "_handle_compact",
    }

    def __init__(self, session_manager):
        self._sm = session_manager

    def is_command(self, text):
        """Check if text is a slash command (known or unknown)."""
        return text.startswith("/")

    def is_known(self, text):
        """Check if text is a recognized command."""
        cmd = text.lower().split()[0]
        return cmd in self.COMMANDS

    def dispatch(self, text, channel, user_id):
        """Route command. Returns (response_text, should_relay_to_goose).

        should_relay_to_goose is True for /compact (needs goose response).
        """
        cmd = text.lower().split()[0]
        handler = self.COMMANDS.get(cmd)
        if handler:
            return getattr(self, handler)(channel, user_id, text)
        return f"Unknown command: {cmd}\nSend /help for available commands.", False

    def _handle_stop(self, channel, user_id, text):
        cancelled = self._sm.cancel_relay(channel, user_id)
        return ("Stopped." if cancelled else "Nothing running."), False

    def _handle_clear(self, channel, user_id, text):
        self._sm.clear_session(channel, user_id)
        # caller is responsible for triggering goose web restart if needed
        return "Session cleared.", False

    def _handle_compact(self, channel, user_id, text):
        # Returns the prompt to send to goose, with should_relay=True
        return ("Please summarize our conversation so far into key points, "
                "then we can continue from this summary. Be concise."), True

    def _handle_help(self, channel, user_id, text):
        return ("GooseClaw Commands\n\n"
                "/stop - cancel the current response\n"
                "/clear - wipe conversation and start fresh\n"
                "/compact - summarize history to save tokens\n"
                "/help - this message"), False
```

**Design rationale:** Returns `(text, should_relay)` instead of directly sending messages. This keeps the router channel-agnostic. The caller (Telegram loop or ChannelRelay) handles actual message delivery in the channel-specific way.

### 3. ChannelRelay v2 (upgrade existing class)

**What changes:** Add CommandRouter integration, use SessionManager instead of bare `_sessions` dict, add per-user locks and cancellation.

```python
class ChannelRelay:
    """Relay for channel plugins. Full parity with Telegram."""

    def __init__(self, channel_name, session_manager, command_router):
        self._name = channel_name
        self._sm = session_manager
        self._router = command_router

    def __call__(self, user_id, text, send_fn=None):
        """Relay a message. Handles commands, sessions, locks, cancellation."""
        user_key = str(user_id)

        # command routing (NEW)
        if self._router.is_command(text):
            if not self._router.is_known(text):
                return f"Unknown command: {text.split()[0]}\nSend /help for available commands."
            response, should_relay = self._router.dispatch(text, self._name, user_key)
            if not should_relay:
                return response
            # should_relay=True means send response text to goose (for /compact)
            text = response

        # per-user lock (NEW - prevents concurrent relays per user)
        if not self._sm.acquire_lock(self._name, user_key, timeout=2):
            return "Still thinking... send /stop to cancel."

        try:
            session_id = self._sm.get_session(self._name, user_key)

            # cancellation tracking (NEW)
            cancelled = threading.Event()
            sock_ref = [None, cancelled]
            self._sm.register_relay(self._name, user_key, sock_ref)

            try:
                # verbosity + streaming (EXISTING, preserved)
                setup = load_setup()
                verbosity = get_verbosity_for_channel(setup, self._name) if setup else "balanced"

                response_text, error = _relay_to_goose_web(
                    text, session_id, channel=self._name,
                    flush_cb=send_fn if send_fn and verbosity != "quiet" else None,
                    verbosity=verbosity, sock_ref=sock_ref,
                )

                if cancelled.is_set():
                    return ""
                if error:
                    return f"Error: {error}"
                return response_text
            finally:
                self._sm.unregister_relay(self._name, user_key)
        finally:
            self._sm.release_lock(self._name, user_key)

    def reset_session(self, user_id):
        """Reset a user's session."""
        self._sm.clear_session(self._name, str(user_id))
```

### 4. BotInstance + BotManager (NEW for multi-bot)

**Design constraint:** goose web is a single process. Multiple bots share it. Session isolation happens via session IDs, not separate goose processes (per PROJECT.md: "Multiple goose web processes" is out of scope).

```python
class BotInstance:
    """A single Telegram bot with its own token, paired users, and config."""

    def __init__(self, name, token, config, session_manager, command_router):
        self.name = name           # e.g. "main", "work", "research"
        self.token = token
        self.config = config       # provider/model overrides
        self._channel_name = f"telegram:{name}" if name != "default" else "telegram"
        self._sm = session_manager
        self._router = command_router
        self._running = False

    def start(self):
        """Start polling loop for this bot."""
        # Same pattern as current _telegram_poll_loop but uses
        # self._channel_name for session scoping

    def stop(self):
        self._running = False


class BotManager:
    """Registry of Telegram bots. Reads from setup.json."""

    def __init__(self, session_manager, command_router):
        self._bots = {}
        self._sm = session_manager
        self._router = command_router

    def load_from_config(self, setup):
        """Load bot configs from setup.json 'bots' array."""
        bots_config = setup.get("bots", [])
        if not bots_config:
            # backward compat: single bot from telegram_bot_token
            token = setup.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
            if token:
                bots_config = [{"name": "default", "token": token}]
        for bc in bots_config:
            self.add_bot(bc)

    def add_bot(self, config):
        """Add and start a bot."""
        name = config["name"]
        bot = BotInstance(name, config["token"], config, self._sm, self._router)
        self._bots[name] = bot
        bot.start()

    def stop_all(self):
        for bot in self._bots.values():
            bot.stop()
```

**Channel naming for multi-bot model routing:**
- Default (single bot): channel name = `"telegram"` (backward compatible)
- Named bots: channel name = `"telegram:bot_name"`
- `channel_routes` in setup.json: `{"telegram:work": "openai_gpt4", "telegram:research": "anthropic_opus"}`

### 5. Config Schema Extension (setup.json)

```json
{
  "models": [
    {"id": "anthropic_opus", "provider": "anthropic", "model": "claude-opus-4-6", "is_default": true},
    {"id": "openai_gpt4", "provider": "openai", "model": "gpt-4o"}
  ],
  "channel_routes": {
    "telegram": "anthropic_opus",
    "telegram:research": "openai_gpt4",
    "slack": "anthropic_opus"
  },
  "bots": [
    {"name": "default", "token": "123:ABC"},
    {"name": "research", "token": "456:DEF", "model": "openai_gpt4"}
  ]
}
```

The `bots` array is the only new top-level key. `channel_routes` already exists and naturally extends to `telegram:bot_name` keys.

## Patterns to Follow

### Pattern 1: Composite Key for Session Scoping

**What:** Use `(channel_name, user_id)` tuple as the key for all session operations.
**When:** Every SessionManager method.
**Why:** Prevents session collision between channels. A Telegram user `12345` and a Slack user `12345` get different sessions. A user talking to `telegram:work` and `telegram:research` bots also gets different sessions (different model contexts).

```python
# key generation
def _key(self, channel, user_id):
    return f"{channel}:{user_id}"

# session file per channel (backward compat)
def _sessions_file(self, channel):
    if channel == "telegram":
        return os.path.join(self._data_dir, "telegram_sessions.json")  # backward compat
    return os.path.join(self._data_dir, f"sessions_{channel}.json")
```

### Pattern 2: Backward-Compatible Telegram Migration

**What:** Keep the existing `_telegram_sessions.json` format and location.
**When:** SessionManager loads/saves telegram sessions.
**Why:** Users upgrading from v1 keep their existing sessions without migration.

```python
# SessionManager init:
# if "telegram_sessions.json" exists, load it into sessions["telegram:*"]
# new channels use "sessions_<name>.json"
```

### Pattern 3: Channel-Agnostic Command Registration

**What:** Commands registered once in CommandRouter, available to all channels.
**When:** Gateway startup.
**Why:** Adding a command once makes it work everywhere. No per-channel duplication.

### Pattern 4: Goose Web Restart Scoping

**What:** `/clear` on Telegram currently restarts the entire goose web process (kills claude-code subprocess). This affects ALL channels. For v2, this behavior should remain but be made explicit.
**When:** Any channel sends `/clear`.
**Why:** goose web is a single process. Restarting it to clear provider state is a global operation. All channels need to be aware their sessions may be invalidated.

```python
# _handle_clear notifies SessionManager to clear ALL sessions
# (same as current _clear_chat which calls _telegram_sessions.clear())
# SessionManager broadcasts "sessions invalidated" to all channels
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: Separate Process Per Bot

**What:** Running multiple goose web processes for multi-bot isolation.
**Why bad:** Memory explosion on Railway (goose web + provider subprocess per bot). Config management nightmare. Explicitly out of scope in PROJECT.md.
**Instead:** Single goose web, session isolation via session IDs, model hot-swap via `_update_goose_session_provider`.

### Anti-Pattern 2: Channel Plugin Subclassing

**What:** Making channel plugins subclass a base Channel class with abstract methods.
**Why bad:** Channel plugins are loaded from `/data/channels/*.py` via `importlib`. They're user-authored scripts with a `CHANNEL` dict contract. Adding class inheritance makes the plugin API complex and fragile.
**Instead:** ChannelRelay wraps the plugin. The plugin only needs `CHANNEL = {"name": ..., "send": ..., "poll": ...}`. The relay provides commands, locks, and cancellation around the plugin's poll function.

### Anti-Pattern 3: Extracting Telegram Into a Channel Plugin

**What:** Making Telegram just another channel plugin in `/data/channels/telegram.py`.
**Why bad:** Telegram has unique requirements (pairing flow, bot command registration, getUpdates long-polling with offset management, typing indicators, edit-in-place streaming). These are significantly more complex than a generic channel plugin. Forcing it into the plugin API would either limit Telegram's features or bloat the plugin API.
**Instead:** Telegram stays as built-in code but uses the shared SessionManager and CommandRouter. The poll loop becomes thinner because command handling and session management are extracted.

### Anti-Pattern 4: Global Model State

**What:** Setting `GOOSE_PROVIDER` / `GOOSE_MODEL` env vars per request.
**Why bad:** Env vars are process-global. With concurrent requests from different channels, they'd race. Already solved by `_update_goose_session_provider` which calls POST `/agent/update_provider` per session.
**Instead:** Continue using the per-session model routing via `_update_goose_session_provider`. Works correctly with concurrent channels.

## Migration Strategy

### Phase 1: Extract SessionManager (no behavior change)

1. Create `SessionManager` class that wraps the existing globals
2. Internally, it still uses `_telegram_sessions`, `_telegram_chat_locks`, etc.
3. `_telegram_poll_loop` calls `SessionManager` methods instead of touching globals directly
4. All existing tests pass with zero behavior change

### Phase 2: Extract CommandRouter (no behavior change)

1. Create `CommandRouter` class
2. Move the `if lower == "/help"` chain into `CommandRouter.dispatch()`
3. `_telegram_poll_loop` calls `CommandRouter.dispatch()` then sends the response
4. Existing tests pass

### Phase 3: Upgrade ChannelRelay

1. `ChannelRelay.__init__` takes `SessionManager` and `CommandRouter`
2. `ChannelRelay.__call__` checks for commands, acquires locks, tracks relays
3. Channel plugins gain /stop, /clear, /compact, /help without any plugin code changes
4. New tests for ChannelRelay command handling

### Phase 4: Multi-Bot Support

1. Add `BotInstance` and `BotManager`
2. Config schema: add `bots` array to setup.json
3. `channel_routes` extended to accept `telegram:bot_name` keys
4. Each bot runs its own poll loop, scoped by channel name
5. Setup wizard: multi-bot configuration UI

### Why This Order

- Phase 1 and 2 are pure extractions with no behavior change. Safe, testable, reversible
- Phase 3 depends on 1+2. Can't upgrade ChannelRelay without the shared components
- Phase 4 depends on 1+2+3. Multi-bot is just "multiple poll loops using the shared components"
- Each phase is independently deployable and testable

## Integration Points (Existing Code Touch Points)

### Must Modify

| File/Function | What Changes | Risk |
|---------------|-------------|------|
| `_telegram_poll_loop` | Command handling moves to CommandRouter. Session ops move to SessionManager. Loop becomes: poll, dispatch, relay | MEDIUM (core telegram path) |
| `ChannelRelay.__init__` | New params: session_manager, command_router | LOW (additive) |
| `ChannelRelay.__call__` | Add command check, lock acquire, relay tracking | MEDIUM (changes relay behavior for plugins) |
| `_load_channel` | Pass SessionManager + CommandRouter to ChannelRelay constructor | LOW (one-line change) |
| `main()` | Create SessionManager + CommandRouter, pass to components | LOW (startup wiring) |
| `_clear_chat` | Becomes `SessionManager.clear_session` | LOW (move, not rewrite) |
| `_get_session_id` | Becomes `SessionManager.get_session` | LOW (move, not rewrite) |
| `_get_chat_lock` | Becomes internal to SessionManager | LOW (move, not rewrite) |
| `setup.json` schema | Add `bots` array (Phase 4 only) | LOW (additive, backward compat) |
| `validate_setup_config` | Validate `bots` array (Phase 4 only) | LOW (additive) |
| `start_telegram_gateway` | Use BotManager instead of direct poll loop start (Phase 4) | MEDIUM |

### Must NOT Modify

| Component | Why Leave Alone |
|-----------|----------------|
| `_do_ws_relay` / `_do_ws_relay_streaming` | Already correct. Channel-agnostic websocket relay |
| `_relay_to_goose_web` | Already handles channel param for model routing |
| `_update_goose_session_provider` | Already handles per-session model hot-swap |
| `_create_goose_session` | Already channel-agnostic |
| `notify_all` | Already channel-agnostic with targeting |
| `goose_health_monitor` | Independent of channel architecture |
| `RateLimiter` | HTTP-level, orthogonal to channels |
| Job engine / cron scheduler | Uses `_relay_to_goose_web` directly, not channels |

## Scalability Considerations

| Concern | At 1 bot, 1 channel | At 3 bots, 5 channels | At 10 bots, 10 channels |
|---------|---------------------|----------------------|------------------------|
| Session dict size | ~10 entries | ~100 entries | ~500 entries (fine in memory) |
| Lock contention | None | Minimal (per-user locks) | Per-user, no cross-user contention |
| goose web load | 1 concurrent relay | 3-5 concurrent relays | 10+ concurrent. WebSocket limit may hit |
| Poll threads | 1 telegram + N plugin | 3 telegram + 5 plugin | 10 + 10 = 20 daemon threads. Fine for Python |
| Session files | 2 JSON files | 8 JSON files | 20 JSON files. /data volume handles it |
| Memory | ~50MB base | ~60MB | ~80MB. Python thread overhead is small |

**Bottleneck:** goose web is the single bottleneck. It handles one conversation at a time per session, but multiple sessions can run concurrently (multiple WebSocket connections). The limit is goose web's internal concurrency model, not the gateway.

## Sources

- Direct analysis of `/Users/haseeb/nix-template/docker/gateway.py` (6207 lines)
- Direct analysis of `/Users/haseeb/nix-template/docker/test_gateway.py` (969 lines)
- `/Users/haseeb/nix-template/.planning/PROJECT.md` (project constraints and scope)
- `/Users/haseeb/nix-template/.planning/REQUIREMENTS.md` (v1/v2 requirements)

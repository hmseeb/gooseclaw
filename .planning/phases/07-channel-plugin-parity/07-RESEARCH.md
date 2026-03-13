# Phase 7: Channel Plugin Parity - Research

**Researched:** 2026-03-13
**Domain:** Python channel plugin system, command routing, concurrency, WebSocket relay
**Confidence:** HIGH

## Summary

Phase 7 wires channel plugins into the shared infrastructure built in Phase 6 (SessionManager, CommandRouter, ChannelState). The key challenge is that the four command handlers (`_handle_cmd_stop`, `_handle_cmd_clear`, `_handle_cmd_compact`, `_handle_cmd_help`) are hardcoded to use `_telegram_state` and `_session_manager.get("telegram", ...)`. These must be generalized to accept a `channel_state` and `channel` name from the `ctx` dict so the same handlers work for any channel.

The ChannelRelay class (line 2914) currently handles message relay only. It has no command interception, no per-user locks, no active relay tracking, and no typing indicators. The `poll_fn` contract passes `(relay_fn, stop_event, creds)` to the plugin's poll function, where `relay_fn` is a ChannelRelay instance. Channel plugins call `relay(user_id, text, send_fn)` directly. Command interception must happen BEFORE the relay, either inside ChannelRelay's `__call__` or in the `_poll_wrapper` that wraps the plugin's poll function.

The notification bus channel validation has three hardcoded `valid_channels` tuples at lines 1002, 5514, and 5564. These must be made dynamic by checking `_loaded_channels` at validation time. The CHANNEL dict contract needs two new optional fields: `commands` (dict of custom command handlers) and `typing` (callback for activity indicators).

**Primary recommendation:** Generalize command handlers to accept channel/state from ctx. Give ChannelRelay its own ChannelState instance. Intercept commands inside ChannelRelay.__call__ before relay. Keep all changes in gateway.py.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CHAN-01 | Channel plugins receive /help, /stop, /clear, /compact commands identical to telegram | Command handlers exist but hardcode "telegram" in `_telegram_state` and `_session_manager.get("telegram", ...)`. Must generalize to use `ctx["channel"]` and `ctx["channel_state"]`. ChannelRelay.__call__ must intercept commands before relay. |
| CHAN-02 | Per-user relay locks preventing concurrent goose requests from same user | ChannelState class exists with `get_user_lock(user_id)`. Each ChannelRelay needs its own ChannelState instance. Lock acquisition pattern from telegram poll loop (line 4467-4471) must be replicated in ChannelRelay.__call__. |
| CHAN-03 | Cancel in-flight requests via /stop (active relay tracking + socket close) | ChannelState has `set_active_relay`, `pop_active_relay`, `kill_relay`. ChannelRelay.__call__ must pass `sock_ref` to `_relay_to_goose_web` and register it with its ChannelState. `/stop` handler must use ctx's channel_state, not `_telegram_state`. |
| CHAN-04 | Custom commands via CHANNEL dict `commands` field | New CHANNEL dict field. CommandRouter.register() already supports arbitrary commands. During `_load_channel()`, iterate `channel.get("commands", {})` and register each on a per-channel or the global router. |
| CHAN-05 | Notification bus validates channel names dynamically from loaded plugins | Three hardcoded tuples at lines 1002, 5514, 5564. Replace with function that builds valid set from `_loaded_channels.keys()` + fixed channels ("web", "telegram", "cron", "memory"). |
| CHAN-06 | Typing/activity indicators via optional `typing` callback in CHANNEL dict | New optional CHANNEL dict field. ChannelRelay.__call__ starts a typing loop thread (same pattern as telegram lines 4484-4492) if `typing` callback exists. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.x | All implementation | Project constraint: zero external dependencies in gateway.py |
| threading.Lock | stdlib | Per-user relay locks | Already used in ChannelState, same pattern as telegram |
| threading.Event | stdlib | Cancel signaling, stop events | Already used for relay cancellation and poll stop |
| threading.Thread | stdlib | Typing indicator loops, background relay | Same pattern as telegram poll loop |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| unittest | stdlib | Test framework | 134 existing tests, continue same pattern |
| unittest.mock | stdlib | Mocking relay, send_fn, socket | Already used extensively |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Per-ChannelRelay ChannelState | Single global ChannelState for all channels | Per-instance is cleaner: channel plugins don't share lock namespaces, and Phase 9 multi-bot benefits from per-instance isolation |
| Command interception in ChannelRelay.__call__ | Command interception in _poll_wrapper | __call__ is better because it works regardless of whether plugin uses poll or direct relay |

## Architecture Patterns

### Current Channel Plugin Loading Flow

```
_load_channel(filepath)
  -> import module, read CHANNEL dict
  -> validate: name (required), send (required, callable)
  -> resolve credentials
  -> call setup(creds) if provided
  -> register notification handler as "channel:<name>"
  -> if poll_fn exists:
       relay_fn = ChannelRelay(name)
       start poll thread: poll_fn(relay_fn, stop_event, creds)
  -> store in _loaded_channels[name]
```

### Current ChannelRelay.__call__ Flow (NO commands, NO locks)

```python
def __call__(self, user_id, text, send_fn=None):
    # 1. get/create session via _session_manager
    # 2. determine streaming params (verbosity)
    # 3. relay to goose web (single attempt)
    # 4. retry on failure with new session
    # 5. return response text
    # MISSING: command interception, per-user locks, active relay tracking, typing
```

### Target ChannelRelay.__call__ Flow (Phase 7)

```python
def __call__(self, user_id, text, send_fn=None):
    # 1. CHECK: is this a command? -> dispatch via _command_router
    if text and text.startswith("/"):
        ctx = {
            "channel": self._name,
            "user_id": str(user_id),
            "send_fn": send_fn or (lambda t: None),
            "channel_state": self._state,  # per-channel ChannelState
        }
        if _command_router.is_command(text):
            _command_router.dispatch(text, ctx)
            return ""  # command handled, no relay response
        if send_fn:
            send_fn(f"Unknown command: {text.split()[0]}\nSend /help for available commands.")
        return ""

    # 2. LOCK: acquire per-user lock (prevent concurrent relays)
    user_lock = self._state.get_user_lock(user_id)
    if not user_lock.acquire(timeout=2):
        if send_fn:
            send_fn("Still thinking... send /stop to cancel.")
        return ""

    try:
        # 3. TYPING: start typing indicator loop if callback exists
        typing_stop = threading.Event()
        if self._typing_cb:
            # start typing loop thread
            ...

        # 4. RELAY: same as current, but with sock_ref for cancellation
        cancelled = threading.Event()
        sock_ref = [None, cancelled]
        self._state.set_active_relay(user_id, sock_ref)

        try:
            session_id = _session_manager.get(self._name, user_key) or create_new
            response_text, error = _relay_to_goose_web(
                text, session_id, chat_id=user_id,
                channel=self._name, sock_ref=sock_ref, ...
            )
            if send_fn and not cancelled.is_set():
                send_fn(response_text or f"Error: {error}")
        finally:
            self._state.pop_active_relay(user_id)
            typing_stop.set()
    finally:
        user_lock.release()

    return response_text
```

### Command Handler Generalization Pattern

Current handlers are hardcoded to telegram:

```python
# CURRENT (hardcoded):
def _handle_cmd_stop(ctx):
    chat_id = ctx["user_id"]
    sock_ref = _telegram_state.pop_active_relay(chat_id)  # HARDCODED
    sid = _session_manager.get("telegram", chat_id)        # HARDCODED

def _handle_cmd_clear(ctx):
    chat_id = ctx["user_id"]
    old = _clear_chat(chat_id)  # calls _telegram_state.kill_relay + _session_manager.pop("telegram", ...)
```

Must become:

```python
# TARGET (generalized):
def _handle_cmd_stop(ctx):
    chat_id = ctx["user_id"]
    channel = ctx.get("channel", "telegram")
    state = ctx.get("channel_state", _telegram_state)  # backward-compat default
    sock_ref = state.pop_active_relay(chat_id)
    sid = _session_manager.get(channel, chat_id)
    # ... rest same

def _handle_cmd_clear(ctx):
    chat_id = ctx["user_id"]
    channel = ctx.get("channel", "telegram")
    state = ctx.get("channel_state", _telegram_state)
    state.kill_relay(chat_id)
    old = _session_manager.pop(channel, chat_id)
    # ... restart goose, prewarm
```

### Dynamic Channel Validation Pattern

```python
# CURRENT (hardcoded at 3 locations):
valid_channels = ("web", "telegram", "cron", "memory")

# TARGET:
def _get_valid_channels():
    """Build valid channel set dynamically from loaded plugins + fixed channels."""
    fixed = {"web", "telegram", "cron", "memory"}
    with _channels_lock:
        plugin_names = set(_loaded_channels.keys())
    return fixed | plugin_names
```

### CHANNEL Dict Contract Extension

```python
# Current contract:
CHANNEL = {
    "name": "slack",              # REQUIRED
    "version": 1,                 # REQUIRED
    "send": send_fn,              # REQUIRED
    "poll": poll_fn,              # OPTIONAL
    "setup": setup_fn,            # OPTIONAL
    "teardown": teardown_fn,      # OPTIONAL
    "credentials": ["TOKEN"],     # OPTIONAL
}

# Phase 7 additions:
CHANNEL = {
    # ... existing fields ...
    "commands": {                 # OPTIONAL (CHAN-04)
        "status": {
            "handler": status_fn,     # (ctx) -> None
            "description": "show bot status",
        },
    },
    "typing": typing_fn,          # OPTIONAL (CHAN-06): (user_id) -> None
}
```

### Anti-Patterns to Avoid

- **Creating a separate ChannelState per command dispatch:** Each ChannelRelay should own ONE ChannelState instance created at init time. All commands for that channel must use the same instance, otherwise /stop can't find relays registered by the relay path.
- **Passing ChannelRelay to poll_fn differently:** The `poll_fn(relay_fn, stop_event, creds)` contract must NOT change. The relay_fn IS the ChannelRelay instance. Commands are intercepted inside __call__, transparent to the plugin.
- **Making _clear_chat channel-aware by adding channel parameter:** Instead, generalize the function or inline the logic in the handler. _clear_chat is a telegram-specific helper that calls _restart_goose_and_prewarm. The restart affects all channels since goose web is shared.
- **Registering custom commands on a per-channel router:** Use the global _command_router. Custom commands from plugins should be namespaced (e.g., channel plugins can use any name, conflicts resolved by first-registered-wins).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-user locks | Custom lock dict | ChannelState.get_user_lock() | Already exists, thread-safe, proven in Phase 6 |
| Active relay tracking | Custom relay dict | ChannelState.set_active_relay/pop_active_relay/kill_relay | Already exists, handles socket close + cancel event |
| Command dispatch | if/elif chain in ChannelRelay | _command_router.dispatch(text, ctx) | Already exists, tested, extensible |
| Typing indicator loop | Custom timer | threading.Thread + Event (same pattern as telegram lines 4484-4492) | Proven pattern, just needs generalization |

## Common Pitfalls

### Pitfall 1: ChannelRelay Return Value Contract
**What goes wrong:** ChannelRelay.__call__ currently returns the response text string. Adding command handling changes return semantics (commands return "" or None). Plugins that check the return value may break.
**Why it happens:** Some plugins might do `response = relay(user_id, text, send_fn); if not response: handle_error()`.
**How to avoid:** Commands should return "" (empty string, not None). Document that empty string means "handled internally" (command or error). This is backward compatible since commands didn't exist before, so no plugin would be sending "/help" as a message.
**Warning signs:** Any plugin that does `if not relay(...)` to detect errors.

### Pitfall 2: /clear Restarts Goose Web for ALL Channels
**What goes wrong:** When a channel plugin user sends /clear, goose web restarts, invalidating sessions for ALL users across ALL channels (telegram + all plugins).
**Why it happens:** Goose web is a single process. _restart_goose_and_prewarm kills and restarts it.
**How to avoid:** This is a documented limitation (INFRA-04 acknowledged it). For Phase 7, the /clear handler should still restart goose web (provider subprocess cleanup requires it). BUT the prewarm should use the requesting channel, not hardcoded "telegram". Document this cross-channel impact.
**Warning signs:** User on channel A sends /clear, user on channel B loses their session unexpectedly.

### Pitfall 3: Lock Timeout in ChannelRelay
**What goes wrong:** Using `lock.acquire(timeout=2)` matches telegram's pattern, but channel plugin poll functions may not handle the "busy" response properly.
**Why it happens:** Telegram sends a message back ("Still thinking... send /stop to cancel."). Channel plugins may or may not have a send_fn available for the busy response.
**How to avoid:** If send_fn is None (send-only channel, no poll), the lock timeout should be longer (or blocking) since there's no way to tell the user. If send_fn exists, use timeout=2 and send the busy message.

### Pitfall 4: Custom Command Name Collisions
**What goes wrong:** A channel plugin registers a custom command `/stop` that conflicts with the built-in `/stop`.
**Why it happens:** Custom commands are registered on the global _command_router.
**How to avoid:** Register built-in commands first (already done at module load time, line 3491-3495). Custom commands registered during _load_channel() will fail silently (CommandRouter.register overwrites). Solution: either prefix custom commands with channel name, or check for conflicts during registration and warn.

### Pitfall 5: Typing Callback Error Handling
**What goes wrong:** A buggy typing callback raises an exception, crashing the typing loop thread or the relay thread.
**Why it happens:** Plugin-provided callbacks are untrusted code.
**How to avoid:** Wrap typing callback calls in try/except, same as the existing `_make_handler` pattern for send_fn (line 3022-3028). Log errors but don't crash.

### Pitfall 6: Command Handling When send_fn is None
**What goes wrong:** Command handlers call `ctx["send_fn"](text)` but send_fn might be None for send-only channels (channels without poll).
**Why it happens:** Send-only channels only register a send_fn for notifications. They don't have a poll loop, so relay_fn is never called, so this shouldn't happen. BUT if someone calls ChannelRelay.__call__ directly without send_fn...
**How to avoid:** Default send_fn to a no-op lambda in ctx construction: `"send_fn": send_fn or (lambda t: None)`.

## Code Examples

### Example: Generalized /stop Handler

```python
def _handle_cmd_stop(ctx):
    """Handle /stop command. Works for any channel."""
    chat_id = ctx["user_id"]
    channel = ctx.get("channel", "telegram")
    state = ctx.get("channel_state", _telegram_state)

    sock_ref = state.pop_active_relay(chat_id)
    if sock_ref and sock_ref[0]:
        try:
            if len(sock_ref) > 1 and hasattr(sock_ref[1], 'set'):
                sock_ref[1].set()
            sid = _session_manager.get(channel, chat_id)
            if sid:
                try:
                    _ws_send_text(sock_ref[0], json.dumps({
                        "type": "cancel",
                        "session_id": sid,
                    }))
                except Exception:
                    pass
            sock_ref[0].close()
        except Exception:
            pass
        ctx["send_fn"]("Stopped.")
        print(f"[{channel}] /stop killed relay for user {chat_id}")
    else:
        ctx["send_fn"]("Nothing running.")
```

### Example: ChannelRelay with Commands and Locks

```python
class ChannelRelay:
    def __init__(self, channel_name, typing_cb=None):
        self._name = channel_name
        self._state = ChannelState()  # per-channel concurrency state
        self._typing_cb = typing_cb
        _session_manager.load(channel_name)

    def __call__(self, user_id, text, send_fn=None):
        user_key = str(user_id)

        # command interception
        if text and text.strip().startswith("/"):
            ctx = {
                "channel": self._name,
                "user_id": user_key,
                "send_fn": send_fn or (lambda t: None),
                "channel_state": self._state,
            }
            if _command_router.is_command(text):
                _command_router.dispatch(text, ctx)
                return ""
            if send_fn:
                send_fn(f"Unknown command: {text.split()[0]}\nSend /help for available commands.")
            return ""

        # per-user lock
        user_lock = self._state.get_user_lock(user_key)
        if not user_lock.acquire(timeout=2 if send_fn else 120):
            if send_fn:
                send_fn("Still thinking... send /stop to cancel.")
            return ""

        try:
            # typing indicator
            typing_stop = threading.Event()
            if self._typing_cb:
                def _typing_loop():
                    while not typing_stop.is_set():
                        try:
                            self._typing_cb(user_id)
                        except Exception:
                            pass
                        typing_stop.wait(4)
                threading.Thread(target=_typing_loop, daemon=True).start()

            # relay with active tracking
            cancelled = threading.Event()
            sock_ref = [None, cancelled]
            self._state.set_active_relay(user_key, sock_ref)

            try:
                session_id = _session_manager.get(self._name, user_key)
                if not session_id:
                    session_id = f"{self._name}_{user_key}_{time.strftime('%Y%m%d_%H%M%S')}"
                    _session_manager.set(self._name, user_key, session_id)

                setup = load_setup()
                verbosity = get_verbosity_for_channel(setup, self._name) if setup else "balanced"
                use_streaming = send_fn and verbosity != "quiet"

                if use_streaming:
                    response_text, error = _relay_to_goose_web(
                        text, session_id, chat_id=user_key, channel=self._name,
                        flush_cb=send_fn, verbosity=verbosity,
                        sock_ref=sock_ref, flush_interval=2.0,
                    )
                else:
                    response_text, error = _relay_to_goose_web(
                        text, session_id, chat_id=user_key, channel=self._name,
                        sock_ref=sock_ref,
                    )

                if cancelled.is_set():
                    return ""
                if error:
                    return f"Error: {error}"
                return response_text
            finally:
                self._state.pop_active_relay(user_key)
                typing_stop.set()
        finally:
            user_lock.release()
```

### Example: Dynamic Channel Validation

```python
def _get_valid_channels():
    """Build valid channel names from fixed set + loaded plugins."""
    fixed = {"web", "telegram", "cron", "memory"}
    with _channels_lock:
        plugin_names = set(_loaded_channels.keys())
    return fixed | plugin_names

# Usage in validate_setup_config:
valid_channels = _get_valid_channels()

# Usage in handle_set_routes:
valid_channels = _get_valid_channels()
```

### Example: Custom Command Registration

```python
# In _load_channel(), after send_fn validation:
commands = channel.get("commands", {})
if isinstance(commands, dict):
    for cmd_name, cmd_info in commands.items():
        if isinstance(cmd_info, dict) and callable(cmd_info.get("handler")):
            if _command_router.is_command(f"/{cmd_name}"):
                print(f"[channels] warn: {name} command /{cmd_name} conflicts with built-in, skipping")
                continue
            _command_router.register(cmd_name, cmd_info["handler"], cmd_info.get("description", ""))
            print(f"[channels] registered custom command /{cmd_name} from {name}")
```

### Example: Channel Plugin with Custom Commands

```python
# Example channel plugin: custom_bot.py
def _handle_status(ctx):
    ctx["send_fn"]("Bot is running. All systems nominal.")

def _handle_ping(ctx):
    ctx["send_fn"]("Pong!")

CHANNEL = {
    "name": "custom_bot",
    "version": 1,
    "send": lambda text: {"sent": True, "error": ""},
    "poll": my_poll_fn,
    "commands": {
        "status": {"handler": _handle_status, "description": "show bot status"},
        "ping": {"handler": _handle_ping, "description": "ping the bot"},
    },
    "typing": lambda user_id: print(f"typing for {user_id}..."),
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| ChannelRelay: relay-only, no commands | ChannelRelay: commands + locks + cancel + typing | Phase 7 (this phase) | Channel plugins become first-class citizens |
| Command handlers hardcoded to telegram | Command handlers generalized via ctx | Phase 7 (this phase) | Any channel can use /help /stop /clear /compact |
| Hardcoded valid_channels tuples | Dynamic _get_valid_channels() | Phase 7 (this phase) | New plugins auto-register as valid channels |
| No custom commands | CHANNEL dict `commands` field | Phase 7 (this phase) | Plugins can extend the command set |

## Open Questions

1. **Should /clear on a channel plugin restart goose web?**
   - What we know: Goose web is shared. Restart kills ALL sessions. Telegram /clear already does this.
   - What's unclear: Should channel plugin /clear be more conservative (just clear session, no restart)?
   - Recommendation: Yes, restart goose web. The purpose of /clear is to truly clear provider state. Document that it affects all channels. This is the same documented limitation from INFRA-04.

2. **Should custom commands be namespaced?**
   - What we know: Two plugins could register `/status`. First wins (or overwrites).
   - What's unclear: Whether to prefix with channel name (e.g., `/slack_status`).
   - Recommendation: No namespacing. Check for conflicts at registration time and warn. Built-in commands take priority (registered first at module load). If collision between plugins, first-loaded wins with a warning log.

3. **Should ChannelRelay._state be accessible to external callers?**
   - What we know: The poll_fn gets `relay_fn` (ChannelRelay instance). If it needs to track state, it could access `relay_fn._state`.
   - What's unclear: Whether to make this a public API.
   - Recommendation: Keep it as `_state` (private). Plugin poll functions don't need direct access. They interact through the relay function and commands.

4. **How should streaming work for channel plugins with /compact?**
   - What we know: /compact currently calls _relay_to_goose_web with `channel="telegram"` and uses _get_session_id (telegram-specific).
   - What's unclear: How to get the session_id for a channel plugin user in the /compact handler.
   - Recommendation: Use `_session_manager.get(channel, chat_id)` directly. If no session exists, compact is meaningless (no history to summarize), so return an error message.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (stdlib) |
| Config file | none (direct unittest discovery) |
| Quick run command | `python3 -m unittest discover -s docker -p 'test_*.py' 2>&1` |
| Full suite command | `python3 -m unittest discover -s docker -p 'test_*.py' 2>&1` |
| Estimated runtime | ~0.6 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CHAN-01 | ChannelRelay.__call__ intercepts /help, /stop, /clear, /compact | unit | `python3 -m unittest docker.test_gateway.TestChannelRelayCommands -v` | No - Wave 0 gap |
| CHAN-01 | Command handlers use ctx channel/state, not hardcoded telegram | unit | `python3 -m unittest docker.test_gateway.TestGeneralizedCommandHandlers -v` | No - Wave 0 gap |
| CHAN-02 | Per-user lock serializes concurrent relays in ChannelRelay | unit | `python3 -m unittest docker.test_gateway.TestChannelRelayLocks -v` | No - Wave 0 gap |
| CHAN-03 | /stop on channel plugin kills active relay socket | unit | `python3 -m unittest docker.test_gateway.TestChannelRelayStop -v` | No - Wave 0 gap |
| CHAN-04 | Custom commands registered from CHANNEL dict commands field | unit | `python3 -m unittest docker.test_gateway.TestCustomCommandRegistration -v` | No - Wave 0 gap |
| CHAN-04 | Custom command conflict detection with built-in commands | unit | `python3 -m unittest docker.test_gateway.TestCustomCommandConflicts -v` | No - Wave 0 gap |
| CHAN-05 | _get_valid_channels returns dynamic set including loaded plugins | unit | `python3 -m unittest docker.test_gateway.TestDynamicChannelValidation -v` | No - Wave 0 gap |
| CHAN-05 | validate_setup_config accepts plugin channel names | unit | `python3 -m unittest docker.test_gateway.TestValidateSetupDynamic -v` | No - Wave 0 gap |
| CHAN-06 | Typing callback invoked during relay if provided | unit | `python3 -m unittest docker.test_gateway.TestChannelRelayTyping -v` | No - Wave 0 gap |
| ALL | Existing 134 tests still pass (zero regression) | regression | `python3 -m unittest discover -s docker -p 'test_*.py'` | Yes (134 tests) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m unittest discover -s docker -p 'test_*.py' 2>&1`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green (134+ tests passing) before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~0.6 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `TestChannelRelayCommands` class - covers CHAN-01 (command interception in __call__)
- [ ] `TestGeneralizedCommandHandlers` class - covers CHAN-01 (handlers use ctx not hardcoded telegram)
- [ ] `TestChannelRelayLocks` class - covers CHAN-02 (per-user lock serialization, busy message)
- [ ] `TestChannelRelayStop` class - covers CHAN-03 (active relay tracking, /stop kills socket)
- [ ] `TestCustomCommandRegistration` class - covers CHAN-04 (CHANNEL dict commands field)
- [ ] `TestCustomCommandConflicts` class - covers CHAN-04 (conflict with built-in commands)
- [ ] `TestDynamicChannelValidation` class - covers CHAN-05 (dynamic valid_channels)
- [ ] `TestValidateSetupDynamic` class - covers CHAN-05 (validate_setup_config accepts plugin channels)
- [ ] `TestChannelRelayTyping` class - covers CHAN-06 (typing callback during relay)
- [ ] Update existing telegram command/relay tests to verify backward compatibility with new ctx fields

## Sources

### Primary (HIGH confidence)
- `/Users/haseeb/nix-template/docker/gateway.py` - Full source analysis (6316 lines). All claims verified against actual code.
  - CommandRouter class: lines 98-136
  - SessionManager class: lines 147-218
  - ChannelState class: lines 223-257
  - Notification bus: lines 300-323, 855-895
  - Channel plugin system: lines 2848-3127
  - ChannelRelay class: lines 2914-2960
  - Command handlers: lines 3423-3495
  - Telegram poll loop (reference for relay pattern): lines 4420-4560
  - Hardcoded valid_channels: lines 1002, 5514, 5564
- `/Users/haseeb/nix-template/docker/test_gateway.py` - 134 tests, all passing
- `/Users/haseeb/nix-template/.planning/phases/06-shared-infrastructure-extraction/06-RESEARCH.md` - Phase 6 research (shared infra design)
- `/Users/haseeb/nix-template/.planning/phases/06-shared-infrastructure-extraction/06-03-SUMMARY.md` - Phase 6 completion summary

### Secondary (MEDIUM confidence)
- Phase 6 plan 03 key decisions (patterns-established section) confirming all session access goes through _session_manager and all command handling through _command_router

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Pure Python stdlib, same as Phase 6, zero external dependencies
- Architecture: HIGH - Direct source analysis of all 6 relevant code sections. Every line number verified. Telegram poll loop provides proven pattern to replicate.
- Pitfalls: HIGH - Based on actual code inspection (hardcoded "telegram" strings, shared goose web process, send_fn nullability)
- Command generalization: HIGH - Exact lines where "telegram" is hardcoded identified (3437, 3442, 3463, 3479, 3483)
- Dynamic validation: HIGH - All three hardcoded valid_channels tuples located (lines 1002, 5514, 5564)

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (stable domain, internal refactoring)

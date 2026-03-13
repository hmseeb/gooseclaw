# Phase 6: Shared Infrastructure Extraction - Research

**Researched:** 2026-03-13
**Domain:** Python refactoring, concurrency primitives, session management
**Confidence:** HIGH

## Summary

Phase 6 extracts Telegram-specific session management, command routing, and concurrency primitives into reusable shared classes. The current code has 7 Telegram-specific module-level globals (sessions, active relays, chat locks, plus their guard locks and a running flag) that must become per-instance state. The command handling is a 75-line if/elif chain inside `_telegram_poll_loop` that must become a `CommandRouter` class. The `ChannelRelay` class already exists for channel plugins but lacks commands, per-user locks, and cancellation, so it serves as the reference for what shared infra should provide.

The biggest risk is `/clear`, which currently restarts the **entire** goose web process (killing ALL sessions across ALL users). This is a fundamental limitation: goose web is a single process and some providers (claude-code) hold persistent subprocesses that only die on process restart. The research finds that per-channel `/clear` can delete only the requesting channel's session mapping without restarting goose web, but this means provider subprocess state persists. This tradeoff must be documented or a new approach (per-session cleanup API) must be verified against goose web capabilities.

**Primary recommendation:** Extract `SessionManager` and `CommandRouter` as plain Python classes in gateway.py (no new files). Use the existing `ChannelRelay` pattern as a guide. Keep all changes internal to gateway.py to minimize risk and maintain the single-file architecture.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| INFRA-01 | SessionManager with composite key (channel:user_id) replaces per-channel session dicts | Mapped all session state: `_telegram_sessions` dict, `_telegram_sessions_lock`, `_telegram_sessions_file`, `_save_telegram_sessions()`, `_load_telegram_sessions()`, `_get_session_id()`, `_prewarm_session()`, `_prewarm_events`. ChannelRelay has its own `_sessions` dict with same pattern. SessionManager unifies both. |
| INFRA-02 | CommandRouter dispatches /help /stop /clear /compact to shared handlers | Mapped command chain at lines 4272-4350: 4 commands as if/elif blocks. Each handler is 5-30 lines. `/compact` relays to goose web. `/stop` kills active relay socket. `/clear` calls `_clear_chat()` + `_restart_goose_and_prewarm()`. `/help` sends static text. |
| INFRA-03 | Telegram globals refactored into per-instance state | Identified 7 module-level dicts/vars that must move: `_telegram_sessions`, `_telegram_sessions_lock`, `_telegram_active_relays`, `_telegram_active_relays_lock`, `_telegram_chat_locks`, `_telegram_chat_locks_lock`, `_telegram_running`. Plus satellite state: `_prewarm_events`, `_memory_last_activity`, `telegram_pair_code`, `telegram_pair_lock`. |
| INFRA-04 | /clear scoped per-channel, not global goose web restart | Found the bug: `_clear_chat()` line 3230 calls `_telegram_sessions.clear()` (clears ALL sessions, not just the requesting chat's). Then `_restart_goose_and_prewarm()` restarts the entire goose web process. Scoping requires removing `.clear()` and replacing with `.pop(chat_key)` only, plus deciding whether to skip goose web restart. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.x | All implementation | Project constraint: zero external dependencies in gateway.py |
| threading.Lock | stdlib | Concurrency guards | Already used throughout, 17 Lock instances |
| collections | stdlib | Data structures | Already imported |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| unittest | stdlib | Test framework | Already used for 97 existing tests |
| unittest.mock | stdlib | Mocking | Already used (MagicMock, patch) |
| json | stdlib | Session persistence | Already used for session file I/O |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Plain classes in gateway.py | Separate module file | Adds import complexity, breaks single-file architecture, not worth it for Phase 6 |
| threading.Lock | asyncio | Would require rewriting entire gateway, massive scope creep |
| Dict-based sessions | SQLite | Adds external dependency, overkill for in-memory session tracking |

## Architecture Patterns

### Current State: Telegram Globals Map

All module-level state that must be refactored:

```
Module-level globals (MUST MOVE to per-instance):
  _telegram_sessions         = {}       # chat_id -> session_id         (line 133)
  _telegram_sessions_lock    = Lock()   # guards _telegram_sessions     (line 134)
  _telegram_active_relays    = {}       # chat_id -> [sock, cancel_evt] (line 135)
  _telegram_active_relays_lock = Lock() # guards _telegram_active_relays (line 136)
  _telegram_chat_locks       = {}       # chat_id -> Lock               (line 137)
  _telegram_chat_locks_lock  = Lock()   # guards _telegram_chat_locks   (line 138)
  _telegram_running          = False    # poll loop alive flag          (line 131)

Satellite state (telegram-adjacent, consider moving):
  _prewarm_events            = {}       # chat_key -> Event             (line 3201)
  _memory_last_activity      = {}       # chat_id -> timestamp          (line 3381)
  _memory_last_activity_lock = Lock()   # guards _memory_last_activity  (line 3382)
  _memory_processed_sessions = set()    # session_ids already processed (line 3383)
  telegram_pair_code         = None     # current pairing code          (line 129)
  telegram_pair_lock         = Lock()   # guards telegram_pair_code     (line 130)

Shared state (NOT telegram-specific, stays global):
  _session_model_cache       = {}       # session_id -> model_config_id (line 143)
  _session_model_lock        = Lock()   # guards _session_model_cache   (line 144)
```

### Current State: Command Handling Chain

Location: `_telegram_poll_loop()` lines 4272-4350

```python
# Current: 75-line if/elif chain inside poll loop
if lower == "/help":
    help_text = "..."
    send_telegram_message(bot_token, chat_id, help_text)
    continue

if lower == "/stop":
    # 25 lines: pop relay, set cancelled flag, send cancel WS, close socket
    continue

if lower == "/clear":
    old = _clear_chat(chat_id)
    send_telegram_message(...)
    threading.Thread(target=_restart_goose_and_prewarm, ...).start()
    continue

if lower == "/compact":
    # 10 lines: relay compaction prompt to goose web
    continue

if lower.startswith("/") and not is_known_command(lower):
    send_telegram_message(bot_token, chat_id, "Unknown command...")
    continue
```

### Current State: ChannelRelay Class (reference for gaps)

Location: lines 2763-2835

```python
class ChannelRelay:
    """What it HAS:"""
    # - Per-channel session dict (self._sessions)
    # - Session persistence to disk (self._save())
    # - Session creation with channel_name prefix
    # - Relay to goose web via _do_ws_relay / _do_ws_relay_streaming
    # - Auto-retry on session failure (new session + retry)
    # - reset_session(user_id) for /clear

    """What it LACKS (to be added in Phase 7, not Phase 6):"""
    # - /help, /stop, /clear, /compact command handling
    # - Per-user locks (concurrent relay prevention)
    # - Active relay tracking (for /stop cancellation)
    # - Typing indicators
    # - Prewarm after clear
```

### Current State: /clear Flow (the problem)

```
User sends /clear
  -> _clear_chat(chat_id)
       -> pop active relay for this chat, close socket
       -> pop this chat's session from _telegram_sessions
       -> _telegram_sessions.clear()    <--- BUG: clears ALL users' sessions
       -> _save_telegram_sessions()
  -> send "Session cleared" message
  -> background thread: _restart_goose_and_prewarm(chat_id)
       -> stop_goose_web()              <--- kills ALL sessions for ALL users
       -> start_goose_web()
       -> _prewarm_session(chat_id)     <--- only prewarms for requesting user
```

### Recommended: SessionManager Class

```python
class SessionManager:
    """Unified session store with composite keys (channel:user_id).

    Replaces:
      - _telegram_sessions + _telegram_sessions_lock
      - ChannelRelay._sessions + ChannelRelay._lock
      - _telegram_sessions_file persistence
    """

    def __init__(self, persist_dir=None):
        self._sessions = {}          # "channel:user_id" -> session_id
        self._lock = threading.Lock()
        self._persist_dir = persist_dir

    def get(self, channel, user_id):
        """Get session_id for a channel:user_id composite key."""
        key = f"{channel}:{user_id}"
        with self._lock:
            return self._sessions.get(key)

    def set(self, channel, user_id, session_id):
        """Set session_id for a channel:user_id composite key."""
        key = f"{channel}:{user_id}"
        with self._lock:
            self._sessions[key] = session_id
        self._save(channel)

    def pop(self, channel, user_id):
        """Remove and return session_id. Returns None if not found."""
        key = f"{channel}:{user_id}"
        with self._lock:
            sid = self._sessions.pop(key, None)
        if sid:
            self._save(channel)
        return sid

    def clear_channel(self, channel):
        """Remove ALL sessions for a given channel prefix."""
        prefix = f"{channel}:"
        with self._lock:
            keys = [k for k in self._sessions if k.startswith(prefix)]
            for k in keys:
                del self._sessions[k]
        self._save(channel)

    def get_all_for_channel(self, channel):
        """Return dict of user_id -> session_id for a channel."""
        prefix = f"{channel}:"
        with self._lock:
            return {k[len(prefix):]: v for k, v in self._sessions.items()
                    if k.startswith(prefix)}

    def _save(self, channel):
        """Persist sessions for a channel to disk."""
        if not self._persist_dir:
            return
        # ... atomic write pattern (tmp + os.replace)

    def load(self, channel):
        """Load sessions for a channel from disk."""
        # ... read from persist_dir
```

### Recommended: CommandRouter Class

```python
class CommandRouter:
    """Routes slash commands to handler functions.

    Replaces:
      - _KNOWN_COMMANDS set
      - is_known_command() function
      - The if/elif chain in _telegram_poll_loop
    """

    def __init__(self):
        self._handlers = {}  # command_name -> handler_fn
        self._help_text = {}  # command_name -> description

    def register(self, command, handler_fn, description=""):
        """Register a command handler. command should NOT include '/'."""
        self._handlers[command] = handler_fn
        if description:
            self._help_text[command] = description

    def is_command(self, text):
        """Check if text is a registered slash command."""
        if not text or not text.startswith("/"):
            return False
        cmd = text.lower().split()[0][1:]  # strip the /
        return cmd in self._handlers

    def dispatch(self, text, context):
        """Dispatch command to handler. Returns True if handled.

        context is a dict with channel-specific info:
          - channel: str (e.g. "telegram")
          - user_id: str
          - send_fn: callable(text) -> None
          - bot_token: str (telegram-specific)
          - etc.
        """
        if not text or not text.startswith("/"):
            return False
        cmd = text.lower().split()[0][1:]
        handler = self._handlers.get(cmd)
        if handler:
            handler(context)
            return True
        return False

    def get_help_text(self):
        """Generate help text from registered commands."""
        lines = []
        for cmd, desc in sorted(self._help_text.items()):
            lines.append(f"/{cmd} -- {desc}")
        return "\n".join(lines)
```

### Recommended: Concurrency State Class

```python
class ChannelState:
    """Per-channel concurrency primitives.

    Replaces:
      - _telegram_active_relays + _telegram_active_relays_lock
      - _telegram_chat_locks + _telegram_chat_locks_lock
      - _prewarm_events
    """

    def __init__(self):
        self._active_relays = {}       # user_id -> [sock, cancel_event]
        self._relays_lock = threading.Lock()
        self._user_locks = {}          # user_id -> Lock
        self._user_locks_lock = threading.Lock()
        self._prewarm_events = {}      # user_id -> Event

    def get_user_lock(self, user_id):
        """Get or create per-user relay lock."""
        uid = str(user_id)
        with self._user_locks_lock:
            if uid not in self._user_locks:
                self._user_locks[uid] = threading.Lock()
            return self._user_locks[uid]

    def set_active_relay(self, user_id, sock_ref):
        with self._relays_lock:
            self._active_relays[str(user_id)] = sock_ref

    def pop_active_relay(self, user_id):
        with self._relays_lock:
            return self._active_relays.pop(str(user_id), None)

    def kill_relay(self, user_id):
        """Kill active relay: set cancelled flag, close socket."""
        sock_ref = self.pop_active_relay(user_id)
        if sock_ref and sock_ref[0]:
            if len(sock_ref) > 1 and hasattr(sock_ref[1], 'set'):
                sock_ref[1].set()
            try:
                sock_ref[0].close()
            except Exception:
                pass
        return sock_ref
```

### Anti-Patterns to Avoid

- **Moving code to separate files prematurely:** The project is a single 6200-line gateway.py by design. All new classes should live in gateway.py. Phase 7+ may restructure, but Phase 6 should not.
- **Breaking the `_relay_to_goose_web` signature:** This function is called from both Telegram poll loop and ChannelRelay. Its interface must remain backward-compatible during extraction.
- **Changing `ChannelRelay.__call__` contract:** Channel plugins depend on `relay(user_id, text, send_fn)`. The internal implementation can change but the public API must not.
- **Making SessionManager async:** Everything is threaded, not async. Use Lock, not asyncio.Lock.
- **Touching goose web process management:** `start_goose_web()` / `stop_goose_web()` are out of scope. Only the decision of whether to call them from `/clear` is in scope.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Composite key formatting | Custom serialization | f-string `f"{channel}:{user_id}"` | Simple, readable, no edge cases with these key formats |
| Atomic file writes | Custom file handling | Existing pattern: write to .tmp then `os.replace()` | Already used in `_save_telegram_sessions()` and `ChannelRelay._save()` |
| Thread-safe dict access | Read-write lock | `threading.Lock()` with `with` statement | Project already uses this pattern everywhere, keep consistent |
| Command parsing | Regex-based parser | Simple `text.lower().split()[0]` | Commands are single words, no arguments to parse |

## Common Pitfalls

### Pitfall 1: Lock Ordering Deadlocks
**What goes wrong:** During extraction, nested lock acquisitions can deadlock if ordering changes.
**Why it happens:** The code already has a known pattern where `_save_telegram_sessions()` acquires `_telegram_sessions_lock` internally, but callers sometimes hold it already. Line 3312 has a comment: "save OUTSIDE lock to avoid deadlock."
**How to avoid:** SessionManager._save() must NOT hold self._lock while doing I/O. Acquire lock, copy data, release lock, then write.
**Warning signs:** Any `with self._lock:` block that calls `self._save()` inside it.

### Pitfall 2: _clear_chat Clears ALL Sessions
**What goes wrong:** Line 3230 calls `_telegram_sessions.clear()` which removes ALL users' sessions, not just the requesting chat's.
**Why it happens:** Originally, `/clear` restarted goose web which invalidated all sessions anyway, so clearing the dict was correct. But for per-channel scoping, this is wrong.
**How to avoid:** Replace `_telegram_sessions.clear()` with `_telegram_sessions.pop(chat_key, None)` only. Document the tradeoff: if goose web restart is still needed for provider state cleanup, all sessions ARE invalid.

### Pitfall 3: Test Globals Mutation
**What goes wrong:** Tests directly access module-level globals like `gateway._telegram_sessions`, `gateway._telegram_active_relays`. After refactoring these into class instances, all tests will break.
**Why it happens:** Tests were written against the global dict API.
**How to avoid:** Refactor tests in the same plan wave as the globals. Consider creating a module-level `_session_manager` instance so `gateway._session_manager.get(...)` replaces `gateway._telegram_sessions.get(...)`.

### Pitfall 4: _relay_to_goose_web Hardcoded to Telegram
**What goes wrong:** `_relay_to_goose_web()` at line 3811 directly accesses `_telegram_sessions` to update session on retry failure.
**Why it happens:** The function was written for telegram only before channel plugins existed.
**How to avoid:** Pass SessionManager (or at minimum channel name) into `_relay_to_goose_web` so it can update the correct session store. Keep backward compatibility by defaulting channel="telegram".

### Pitfall 5: Prewarm Events Are Global
**What goes wrong:** `_prewarm_events` is a module-level dict not guarded by any lock.
**Why it happens:** It's a coordination mechanism between `_clear_chat` -> `_prewarm_session` -> `_get_session_id`.
**How to avoid:** Move into ChannelState or SessionManager. Since Events are thread-safe objects themselves, the dict just needs to move from global to per-channel instance state.

### Pitfall 6: Memory Writer Coupled to Telegram Sessions
**What goes wrong:** `_memory_last_activity` is keyed by chat_id and `_memory_writer_loop` looks up sessions from `_telegram_sessions`.
**Why it happens:** Memory writer was built for telegram only.
**How to avoid:** For Phase 6, memory writer can keep using `_telegram_sessions` via the SessionManager's `get_all_for_channel("telegram")`. Full refactoring of memory writer is out of scope.

## Code Examples

### Pattern: Session Lookup Migration

```python
# BEFORE (current code, line 3261):
with _telegram_sessions_lock:
    sid = _telegram_sessions.get(chat_key)

# AFTER (using SessionManager):
sid = session_manager.get("telegram", chat_id)
```

### Pattern: Clear Chat Migration

```python
# BEFORE (current code, line 3227-3231):
with _telegram_sessions_lock:
    old = _telegram_sessions.pop(chat_key, None)
    _telegram_sessions.clear()  # BUG: clears all users
_save_telegram_sessions()

# AFTER (using SessionManager, INFRA-04 fix):
old = session_manager.pop("telegram", chat_id)
# Only the requesting user's session is removed.
# Other users' sessions remain valid.
```

### Pattern: Command Router Registration

```python
# In start_telegram_gateway() or equivalent:
router = CommandRouter()
router.register("help", _handle_help, "show available commands")
router.register("stop", _handle_stop, "cancel the current response")
router.register("clear", _handle_clear, "wipe conversation and start fresh")
router.register("compact", _handle_compact, "summarize history to save tokens")

# In poll loop:
if router.is_command(text):
    ctx = {"channel": "telegram", "user_id": chat_id, "bot_token": bot_token,
           "send_fn": lambda t: send_telegram_message(bot_token, chat_id, t)}
    if not router.dispatch(text, ctx):
        ctx["send_fn"](f"Unknown command: {text.split()[0]}\nSend /help for available commands.")
    continue
```

### Pattern: Per-Instance State

```python
# BEFORE: module-level globals
_telegram_sessions = {}
_telegram_active_relays = {}
_telegram_chat_locks = {}

# AFTER: per-instance (e.g., stored on a TelegramBot instance or module-level singletons)
_session_manager = SessionManager(persist_dir=DATA_DIR)
_telegram_state = ChannelState()  # per-channel concurrency state
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Module-level dicts | Class-based state management | Phase 6 (this phase) | Enables multi-bot in Phase 9 |
| if/elif command chain | CommandRouter dispatch | Phase 6 (this phase) | Enables channel plugins to use same commands in Phase 7 |
| Global session clear | Per-channel session clear | Phase 6 (this phase) | Fixes INFRA-04 bug where one user's /clear kills all sessions |

## Open Questions

1. **Should /clear still restart goose web?**
   - What we know: Claude-code provider holds persistent subprocesses that survive session deletion. Only goose web restart kills them.
   - What's unclear: Whether goose web has (or will have) a per-session cleanup API that kills provider subprocesses without full restart.
   - Recommendation: For Phase 6, change `/clear` to only delete the requesting user's session (no goose web restart). Add a comment documenting that provider subprocess state may persist. If users report stale state, a separate `/restart` admin command could handle the full restart. This satisfies INFRA-04's "or documented limitation" escape clause.

2. **Where should SessionManager live: global singleton vs passed around?**
   - What we know: Current code uses module-level globals accessed directly. A singleton pattern keeps the migration simple.
   - What's unclear: Whether Phase 9 (multi-bot) needs multiple SessionManager instances or one with bot-scoped keys.
   - Recommendation: Create a single module-level `_session_manager = SessionManager(...)` instance. Phase 9 can extend the composite key to `bot:channel:user_id` or create per-bot instances.

3. **Should ChannelRelay be refactored to use SessionManager in Phase 6?**
   - What we know: ChannelRelay has its own `_sessions` dict. Making it use SessionManager would unify session management.
   - What's unclear: Whether this creates too large a blast radius for Phase 6.
   - Recommendation: Yes, refactor ChannelRelay to use SessionManager in Phase 6. It's a small class (70 lines) and the migration is straightforward. This ensures Phase 7 can wire commands without dealing with two session systems.

4. **What about _session_model_cache?**
   - What we know: This is keyed by session_id (not chat_id), shared across all channels, and used by `_update_goose_session_provider()`.
   - What's unclear: Whether it should move into SessionManager.
   - Recommendation: Leave it global for Phase 6. It's not channel-specific, it's session-specific, and it's only 2 lines of usage. Phase 9 may revisit.

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
| INFRA-01 | SessionManager composite key get/set/pop/clear_channel | unit | `python3 -m unittest docker.test_gateway.TestSessionManager -v` | No - Wave 0 gap |
| INFRA-01 | SessionManager persistence (save/load) | unit | `python3 -m unittest docker.test_gateway.TestSessionManagerPersistence -v` | No - Wave 0 gap |
| INFRA-01 | SessionManager thread safety | unit | `python3 -m unittest docker.test_gateway.TestSessionManagerThreadSafety -v` | No - Wave 0 gap |
| INFRA-02 | CommandRouter register/dispatch/is_command | unit | `python3 -m unittest docker.test_gateway.TestCommandRouter -v` | No - Wave 0 gap |
| INFRA-02 | CommandRouter help text generation | unit | `python3 -m unittest docker.test_gateway.TestCommandRouterHelp -v` | No - Wave 0 gap |
| INFRA-02 | CommandRouter unknown command handling | unit | `python3 -m unittest docker.test_gateway.TestCommandRouterUnknown -v` | No - Wave 0 gap |
| INFRA-03 | Telegram globals no longer exist as module-level dicts | unit | `python3 -m unittest docker.test_gateway.TestNoTelegramGlobals -v` | No - Wave 0 gap |
| INFRA-03 | ChannelState per-user locks, relay tracking | unit | `python3 -m unittest docker.test_gateway.TestChannelState -v` | No - Wave 0 gap |
| INFRA-04 | clear_chat only removes requesting user's session | unit | `python3 -m unittest docker.test_gateway.TestClearChatScoped -v` | No - Wave 0 gap |
| INFRA-04 | clear_chat does not affect other users' sessions | unit | `python3 -m unittest docker.test_gateway.TestClearChatIsolation -v` | No - Wave 0 gap |
| ALL | Existing 97 tests still pass (zero regression) | regression | `python3 -m unittest discover -s docker -p 'test_*.py'` | Yes (97 tests passing) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python3 -m unittest discover -s docker -p 'test_*.py' 2>&1`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green (97+ tests passing) before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~0.6 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `TestSessionManager` class - covers INFRA-01 (composite key CRUD, persistence, thread safety)
- [ ] `TestCommandRouter` class - covers INFRA-02 (register, dispatch, is_command, help text, unknown handling)
- [ ] `TestChannelState` class - covers INFRA-03 (per-user locks, relay tracking, kill_relay)
- [ ] `TestNoTelegramGlobals` class - covers INFRA-03 (assert module-level globals are gone)
- [ ] `TestClearChatScoped` class - covers INFRA-04 (only requesting user's session removed)
- [ ] `TestClearChatIsolation` class - covers INFRA-04 (other users' sessions preserved)
- [ ] Update existing `TestClearKillsRelay`, `TestPrewarmSession`, `TestPrewarmCoordination` to use new API

## Sources

### Primary (HIGH confidence)
- `/Users/haseeb/nix-template/docker/gateway.py` - Full source analysis (6207 lines)
- `/Users/haseeb/nix-template/docker/test_gateway.py` - Existing test suite (97 tests, all passing)
- `/Users/haseeb/nix-template/.planning/REQUIREMENTS.md` - INFRA-01 through INFRA-04 definitions
- `/Users/haseeb/nix-template/.planning/ROADMAP.md` - Phase dependencies and success criteria

### Secondary (MEDIUM confidence)
- ChannelRelay class analysis (lines 2763-2835) as pattern reference for shared infra design

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Python stdlib only, no external dependencies, well-understood
- Architecture: HIGH - Direct source analysis, clear patterns identified, existing ChannelRelay as reference
- Pitfalls: HIGH - Lock ordering issues documented in existing code comments, /clear bug visible in source
- /clear scoping: MEDIUM - The provider subprocess cleanup question depends on goose web's internal capabilities which we cannot verify from gateway.py alone

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (stable domain, pure refactoring)

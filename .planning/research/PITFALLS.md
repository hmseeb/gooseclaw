# Domain Pitfalls

**Domain:** Multi-channel abstraction, multi-bot support, per-channel provider routing on existing single-bot gateway
**Researched:** 2026-03-13
**System:** GooseClaw gateway (Python stdlib only, single goose web process, threading-based concurrency)

## Critical Pitfalls

Mistakes that cause deadlocks, data leaks between sessions, or require rewrites.

---

### Pitfall 1: Lock Re-entrancy and Nested Lock Acquisition

**What goes wrong:** `threading.Lock()` is NOT reentrant. If thread A acquires lock X and then calls a function that also acquires lock X, thread A deadlocks against itself. This already happened: `_do_prewarm` held `_telegram_sessions_lock` then called `_save_telegram_sessions()` which also acquires the same lock. Result: permanent hang on `/clear`.

**Why it happens:** When refactoring telegram-specific code into a shared abstraction layer, you create new call paths. A method in the shared `ChannelRelay` might acquire `_sessions_lock`, then call a helper that internally acquires the same lock. The call chain gets longer and less obvious. It's particularly dangerous when you're moving existing code that was tested in one calling context into a shared layer called from multiple contexts.

**Consequences:** Permanent deadlock. No error message. No recovery except process restart. Every subsequent request that touches the lock also hangs. In production on Railway, this looks like the bot "just stopped responding" with zero diagnostic output.

**Prevention:**
- Audit every lock in the current system (there are 17 `threading.Lock()` instances). Map which functions acquire which locks and what they call while holding them.
- Use `threading.RLock()` for any lock where nested acquisition is even remotely possible. The performance difference is negligible. The safety difference is everything.
- Rule: never call a function that might acquire the same lock while holding it. If you must, acquire-then-release-then-call, or refactor to pass already-locked state as parameters.
- Rule: any lock that protects data which has a `_save_*()` companion function should be an `RLock` or the save function should accept a `_locked=False` parameter to skip acquisition when the caller already holds it.

**Detection:** Process hangs with no error output. Adding `timeout=5` to all `.acquire()` calls and logging timeouts would make these instantly visible instead of silently fatal.

**Known instance:** Commit `071fb00` fixed this exact bug. The comment in `_prewarm_session` line 3312 documents the pattern: "save OUTSIDE lock to avoid deadlock."

---

### Pitfall 2: Global Process Restart Nukes All Channels

**What goes wrong:** `/clear` restarts the entire goose web process via `_restart_goose_and_prewarm()`. This kills ALL sessions across ALL channels and ALL bots. When user A on Telegram says `/clear`, user B's active Slack conversation dies mid-response. With multi-bot, bot1's user clearing context kills bot2's user's session.

**Why it happens:** Goose's claude-code provider spawns a persistent subprocess. Clearing context requires killing that subprocess, which means restarting the entire goose web process. All sessions live inside that single process. There's no per-session provider subprocess isolation. Line 3230 confirms: `_telegram_sessions.clear()` wipes ALL sessions, not just the caller's.

**Consequences:** Cross-channel interference. Users on other channels lose their conversation mid-sentence. Active WebSocket relays get connection errors. The retry logic creates new sessions, but the user loses all context. In multi-bot scenarios, this is a complete isolation failure.

**Prevention:**
- Short term: scope `/clear` to reset only the calling user's session (just `pop(chat_key)` instead of `.clear()`). Accept that claude-code provider state may leak between sessions until goose fixes per-session isolation.
- Medium term: track which sessions are "dirty" (used claude-code provider) and only restart goose web if the clearing user was using that provider. Other providers don't have persistent subprocess state.
- Long term: advocate upstream for goose's per-session provider lifecycle management (Discussion #4389 shows goose team is working on this: "agent per session in goosed/goose-server with isolation").
- Always: before restarting goose web, drain active relays on ALL channels by setting cancelled flags and waiting (with timeout) for in-flight relays to complete.

**Detection:** If you hear "my bot stopped mid-conversation and I didn't do anything," someone on another channel or bot hit `/clear`.

---

### Pitfall 3: Session State Pollution Between Bots Sharing One Goose Web

**What goes wrong:** Multiple bots route to the same goose web process. Goose web maintains a single GOOSE_PROVIDER/GOOSE_MODEL in env vars. When bot1 sets provider to `anthropic/claude-sonnet-4-20250514` and bot2 sets provider to `openai/gpt-4o`, whichever one sets last wins for new sessions. Existing sessions may keep their model via `_update_goose_session_provider()`, but any session recovery or new session creation uses the global default.

**Why it happens:** Goose uses environment variables (GOOSE_PROVIDER, GOOSE_MODEL) as the default for new sessions. `_update_goose_session_provider()` does per-session hot-swapping, but `_session_model_cache` is an in-memory dict that doesn't survive goose web restarts. After a restart, all sessions fall back to whatever env vars are set.

**Consequences:** After goose web restart (crash, `/clear`, health monitor restart), all sessions reset to the wrong model. Bot1's users might suddenly get GPT-4o responses when they're paying for Claude. This is especially insidious because it's intermittent and depends on restart timing.

**Prevention:**
- Persist `_session_model_cache` to disk (alongside channel session files). On goose web restart, re-apply model routing for all known sessions before accepting new messages.
- Apply model routing in `_relay_to_goose_web` BEFORE every message, not just on cache miss. The cost of a redundant `update_provider` call is negligible vs. the cost of model misrouting.
- Track per-session intended model in the session mapping itself (e.g., `_telegram_sessions[chat_key] = {"sid": "...", "model_id": "..."}`), not in a separate cache.

**Detection:** Wrong model name in responses. Unexpected billing. Users reporting different behavior after brief outages.

---

### Pitfall 4: Telegram-Hardcoded Globals Leak Into Shared Abstraction

**What goes wrong:** The codebase has 132 references to `_telegram_` or `telegram_` in gateway.py. Functions like `_relay_to_goose_web` take `chat_id` as a parameter but then do `_telegram_sessions[str(chat_id)]` and `_save_telegram_sessions()` internally. If you pass a Slack user's ID into this function, it silently writes to the telegram sessions dict and corrupts the session mapping.

**Why it happens:** Telegram was first. Code was written to work, not to be channel-agnostic. The function signatures look generic (`chat_id`, `session_id`) but the implementations are telegram-specific. `_relay_to_goose_web` at line 3811 does `_telegram_sessions[str(chat_id)] = new_sid` when retrying. A channel plugin calling this with its own user IDs will pollute telegram's session store.

**Consequences:** Session cross-contamination between channels. A slack user's session gets stored as a telegram session. `/clear` from telegram clears slack sessions. Memory writer processes wrong channel's conversations.

**Prevention:**
- Step 1 (before abstracting): grep for every `_telegram_` reference. Categorize each as: (a) pure telegram logic that stays in telegram code, (b) generic session logic that needs channel parameterization, (c) notification/output logic that uses the notification bus.
- Step 2: `_relay_to_goose_web` must NOT touch `_telegram_sessions` directly. Session recovery on relay failure should call back to the channel's own session manager (which `ChannelRelay` already handles for plugins at line 2818-2824).
- Step 3: make telegram a channel plugin itself, using the same `ChannelRelay` interface. This is the forcing function. If telegram uses the same code path as slack, the abstraction must be correct.
- Incremental approach: add a `channel` parameter to every function that currently says `telegram` in its body. Then replace `_telegram_sessions` references with channel-keyed session stores. Only then remove the old globals.

**Detection:** Session file sizes growing unexpectedly. Session IDs appearing in wrong channel's session file. Users getting responses meant for another channel.

---

### Pitfall 5: Lock Ordering Violations With Multi-Channel Locks

**What goes wrong:** The system currently has 17 module-level locks. Adding multi-channel and multi-bot support will add more (per-bot locks, per-channel-instance locks, cross-channel coordination locks). If thread A acquires `_channel_sessions_lock` then `goose_lock`, and thread B acquires `goose_lock` then `_channel_sessions_lock`, deadlock.

**Why it happens:** Each channel implementation might lock its own session state, then call into shared goose web management code that locks `goose_lock`. Meanwhile, the health monitor locks `goose_lock` then tries to notify channels (which acquire channel locks). No enforced lock ordering exists.

**Consequences:** Intermittent deadlocks that depend on timing. May only manifest under load or when goose web restarts while multiple channels are active. Nearly impossible to reproduce in testing.

**Prevention:**
- Define a global lock ordering hierarchy and document it at the top of gateway.py:
  ```
  Lock ordering (always acquire in this order):
  1. goose_lock (process lifecycle)
  2. _channels_lock (channel registry)
  3. _notification_handlers_lock
  4. channel-specific session locks
  5. _telegram_active_relays_lock / per-chat locks
  6. _session_model_lock
  7. _jobs_lock
  8. other data locks
  ```
- Never acquire a higher-numbered lock while holding a lower-numbered one.
- Use `lock.acquire(timeout=10)` everywhere. Log and recover on timeout instead of hanging forever.
- Consider replacing the 17 fine-grained locks with fewer coarse locks during the refactor. Fewer locks = fewer ordering violations. Optimize later if profiling shows contention.

**Detection:** Add a debug mode that logs lock acquisitions with thread ID and lock name. Build a lock-order violation detector that runs in CI (record acquire order per thread, flag any thread that violates the hierarchy).

---

## Moderate Pitfalls

---

### Pitfall 6: /clear and /stop Race Condition With Multi-Channel

**What goes wrong:** `/stop` kills the active relay socket for a specific chat. `/clear` kills the relay, clears sessions, and restarts goose web. If a channel plugin's `/stop` handler fires while the shared `/clear` logic is mid-restart, the `/stop` tries to close a socket that was already closed by `/clear`, then the retry logic in `_relay_to_goose_web` creates a new session against a goose web that's being killed.

**Why it happens:** This was already partially fixed for telegram-only (commits `caab970`, `a369dfc`). But multi-channel makes it worse: `/clear` on telegram restarts goose web, killing slack's active relay. Slack's relay fails, triggers retry, creates a new session against a goose web that's still starting up (30s readiness wait). The retry fails. Slack retries again. Eventually goose web is ready but slack has created 3 orphan sessions.

**Prevention:**
- Use a "restart generation" counter. Increment on every goose web restart. If a relay's generation doesn't match current, skip retry entirely.
- Set a "restarting" flag checked by all relay retry paths. If goose web is restarting, don't retry, just return a "system restarting, try again shortly" error.
- Serialize goose web restarts with a dedicated restart lock (not `goose_lock`). Queue restart requests. Multiple simultaneous `/clear` calls should collapse into one restart.

---

### Pitfall 7: Notification Bus Doesn't Know About Multi-Bot

**What goes wrong:** `notify_all()` sends to every registered handler. With multi-bot, bot1's cron job output gets delivered to bot2's users. The notification bus has no concept of "which bot" a notification belongs to.

**Why it happens:** The current design registers handlers by name (`channel:telegram`, `channel:slack`). But multi-bot means you'd have `channel:telegram:bot1`, `channel:telegram:bot2`. Job targeting uses `channel: "telegram"` in the job config, which would match both bots.

**Prevention:**
- Add a `bot_id` or `scope` to notification handlers and job targeting. A job created by bot1's user should only deliver to bot1's handler.
- Notification handler registration should accept a scope: `register_notification_handler("telegram", handler, scope="bot1")`.
- `notify_all()` should accept an optional scope filter. Jobs store which bot/channel they were created from.

---

### Pitfall 8: Channel Plugin Hot-Reload Breaks Active Sessions

**What goes wrong:** `POST /api/channels/reload` calls `_unload_channel()` which sets the stop event and deregisters the notification handler, then `_load_channel()` creates a new `ChannelRelay` instance. The new `ChannelRelay` loads sessions from disk, but any in-memory session state from the old instance is lost if it hadn't been saved. Active relays using the old `ChannelRelay` instance continue referencing the old session dict.

**Why it happens:** The old relay function is captured in a closure by the polling thread. After reload, the poll thread gets a new relay function, but any background relay threads spawned before reload still reference the old `ChannelRelay` object.

**Prevention:**
- Drain active relays before unloading a channel (similar to `/stop` for all users on that channel).
- Share session state through a shared dict or file-backed store rather than per-instance dicts. The `ChannelRelay` constructor already loads from disk, so this is mostly about ensuring writes happen before unload.
- Add a "reload grace period" that waits for in-flight relays to complete (with timeout) before teardown.

---

### Pitfall 9: Per-Channel Verbosity and Model Routing Config Grows Quadratically

**What goes wrong:** With N channels and M bots, the routing config becomes N x M entries. Each bot-channel combination needs its own model and verbosity setting. The current flat `channel_routes` dict and `channel_verbosity` dict don't account for per-bot scoping.

**Why it happens:** The current config schema uses `channel_routes: {"telegram": "model_id"}` and `channel_verbosity: {"telegram": "balanced"}`. Adding multi-bot would need `channel_routes: {"telegram:bot1": "model_id_1", "telegram:bot2": "model_id_2"}`, or a nested structure. Either way, the setup wizard UI becomes complex.

**Prevention:**
- Design the config schema for multi-bot from the start. Use a hierarchical model: global defaults, then per-bot overrides, then per-channel-within-bot overrides.
- Don't store in a flat dict. Use: `bots: [{id, token, default_model, channel_overrides: {channel: model_id}}]`.
- Migrate the existing flat config to the new schema in a migration function (like `migrate_config_models()` already handles v1 to v2).

---

### Pitfall 10: Memory Writer Processes Wrong Bot's Conversations

**What goes wrong:** The memory writer (`_memory_writer_loop`) watches `_memory_last_activity` for idle chats, then extracts memories from the conversation. With multi-bot, bot1's user chats get processed by the memory writer, but the extracted memories go into a global identity directory. Bot2's users then see bot1's memories in their context.

**Why it happens:** Memory extraction writes to `IDENTITY_DIR/learnings/`, which is shared across all sessions. The memory writer doesn't track which bot or channel a session belongs to. The `_memory_processed_sessions` set prevents re-processing but doesn't scope by bot.

**Prevention:**
- Scope memory storage by bot: `IDENTITY_DIR/learnings/{bot_id}/`.
- Track `bot_id` in `_memory_last_activity` alongside the timestamp.
- For single-bot deployments (the common case), keep the flat structure for backwards compatibility. Only create subdirectories when multi-bot is configured.

---

## Minor Pitfalls

---

### Pitfall 11: Bot Token Exposure in Shared Logging

**What goes wrong:** Log lines include bot tokens in URLs: `f"https://api.telegram.org/bot{bot_token}/getUpdates"`. With multi-bot, all bot tokens appear in shared logs. If logs are exposed, all bots are compromised.

**Prevention:** Mask bot tokens in log output. Log only the last 4 characters: `bot***{token[-4:]}`.

---

### Pitfall 12: Rate Limiter is Per-IP, Not Per-Bot or Per-Channel

**What goes wrong:** The rate limiter (`api_limiter`, `auth_limiter`, `notify_limiter`) is per source IP. A busy channel plugin making API calls from localhost exhausts the rate limit for all internal calls. Multi-bot management requests from the admin dashboard compete with channel plugin requests.

**Prevention:** Exempt internal/localhost calls from rate limiting, or use per-bot rate limiting for bot-facing endpoints and per-IP only for user-facing endpoints.

---

### Pitfall 13: Test Coverage Gap for Concurrent Paths

**What goes wrong:** `test_gateway.py` tests individual functions with mocks but doesn't test concurrent execution. The deadlock bug (commit `071fb00`) wasn't caught by tests because no test exercised the lock acquisition path under concurrency.

**Prevention:**
- Add threading tests that exercise common concurrent scenarios: relay + clear, relay + stop, relay + channel reload.
- Use `threading.Barrier` to force threads to hit the critical section simultaneously.
- Add a timeout to every test that touches locks. A hung test is better than a silently passing one.

---

### Pitfall 14: Backwards Compatibility During Session File Migration

**What goes wrong:** Current session files: `telegram_sessions.json`, `channel_sessions_{name}.json`. Multi-bot needs `channel_sessions_{bot_id}_{channel}.json` or a single `sessions.json` with nested structure. If the migration doesn't handle existing files, users lose their session mappings on upgrade.

**Prevention:**
- Write a `migrate_sessions()` function (following the pattern of `migrate_config_models()`).
- On startup, check for old-format files and migrate them into the new structure.
- Keep old files for one version cycle (renamed with `.bak`) for rollback safety.

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| Channel abstraction layer | Pitfall 4 (telegram-hardcoded globals) | Extract telegram into channel plugin interface FIRST, before adding new channels |
| Multi-bot support | Pitfall 2 (global restart), Pitfall 3 (session pollution) | Scope all session state by bot_id, make `/clear` per-user not global |
| Per-channel provider routing | Pitfall 3 (model cache lost on restart), Pitfall 9 (config schema) | Persist model cache, design hierarchical config from day one |
| Command routing (/help, /stop, /clear) | Pitfall 6 (race conditions) | Add restart generation counter, serialize restart requests |
| Notification bus multi-bot | Pitfall 7 (wrong bot receives notification) | Add scope/bot_id to handler registration and job targeting |
| Memory writer | Pitfall 10 (cross-bot memory bleed) | Scope learnings directory by bot_id |
| Concurrency testing | Pitfall 13 (test gap) | Add threading tests BEFORE refactoring, not after |
| Session migration | Pitfall 14 (lost sessions on upgrade) | Write migration function, keep backup files |
| Lock refactoring | Pitfall 1 (re-entrancy), Pitfall 5 (ordering) | Switch to RLock, define lock hierarchy, add timeouts |

## Sources

- GooseClaw `gateway.py` source code analysis (primary)
- Git history: commits `071fb00` (deadlock fix), `caab970` (/clear hang), `a369dfc` (race conditions), `d45d9fe` (/clear restart)
- [Python threading.Lock documentation](https://docs.python.org/3/library/threading.html)
- [Real Python: Avoiding Deadlocks with RLock](https://realpython.com/lessons/avoiding-deadlocks-rlock/)
- [Super Fast Python: How to Identify a Deadlock](https://superfastpython.com/thread-deadlock-in-python/)
- [Goose Discussion #4389: per-session agents](https://github.com/block/goose/discussions/4389)
- [Goose GitHub: session isolation architecture](https://github.com/block/goose)
- [Python Concurrency Best Practices](https://realpython.com/ref/best-practices/concurrency/)
- [Multi-Tenancy session isolation patterns](https://www.viget.com/articles/multi-tenancy-in-django/)
- [AI Gateway architecture patterns 2026](https://www.truefoundry.com/blog/a-definitive-guide-to-ai-gateways-in-2026-competitive-landscape-comparison)

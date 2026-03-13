# Phase 10: Multi-Bot Lifecycle - Research

## Scope

Add POST /api/bots and DELETE /api/bots/<name> endpoints for hot-add and hot-remove of Telegram bots without container restart. Persist changes to setup.json so bots survive restart.

## Discovery Level: 0 (Skip)

All work follows established codebase patterns. No new external dependencies. Pure internal work using existing BotManager, SessionManager, save_setup, and API handler patterns from Phase 9.

## Current State Analysis

### What exists (Phase 9 output)

**BotInstance** (line 262): Full lifecycle -- `start()` loads sessions, registers notification handler, generates pair code, registers commands, starts poll thread. `stop()` sets `running = False` and joins thread.

**BotManager** (line 592): Registry with `add_bot()`, `remove_bot()`, `stop_all()`, `get_bot()`, `get_all()`. Thread-safe via `_lock`.

**Gaps in BotManager.remove_bot():**
- Only sets `bot.running = False`. Does NOT call `bot.stop()` (thread join).
- Does NOT clean up sessions via `_session_manager.clear_channel()`.
- Does NOT unregister notification handler (no `unregister_notification_handler` exists).
- Does NOT remove from setup.json.

**BotManager.add_bot():**
- Creates BotInstance but does NOT call `bot.start()`. Starting is done separately (in apply_config).
- Returns existing bot on duplicate name (idempotent).
- Raises ValueError on duplicate token.

**API routing patterns:**
- `do_POST` routes by exact path match, e.g., `elif path == "/api/jobs":`
- `do_DELETE` routes by prefix, e.g., `elif path.startswith("/api/jobs/"):`
- `_check_local_or_auth()` allows localhost without auth, requires auth for remote.
- `_read_body()` reads POST JSON body.
- `send_json(status, data)` sends JSON response.
- Rate limiting via `api_limiter` on all `/api/*` paths.

**setup.json I/O:**
- `load_setup()` reads setup.json, returns dict or None.
- `save_setup(config)` atomically writes (tmp + rename), backs up existing.
- `_resolve_bot_configs(config)` reads `bots` array, falls back to `telegram_bot_token`.

**Session cleanup:**
- `SessionManager.clear_channel(channel)` removes all sessions with channel prefix.
- `ChannelState` has per-user locks and active relay tracking (no explicit cleanup needed -- GC handles it).

**Notification handlers:**
- `register_notification_handler(name, handler_fn)` adds/updates handler.
- No `unregister_notification_handler` exists -- needs to be added.

## Implementation Plan

### What needs to be built

1. **`unregister_notification_handler(name)`** -- Remove handler by name from `_notification_handlers` list.

2. **`BotManager.remove_bot()` enhancement** -- Call `bot.stop()` (thread join), clear sessions via `_session_manager.clear_channel(bot.channel_key)`, unregister notification handler.

3. **`handle_add_bot()`** -- POST /api/bots handler:
   - Reads JSON body: `{name, token, provider?, model?}`
   - Validates: name required, token required, token format (contains `:`)
   - Validates: name not already running
   - Adds to BotManager, starts bot
   - Persists to setup.json bots array
   - Returns 201 with bot info

4. **`handle_remove_bot(name)`** -- DELETE /api/bots/<name> handler:
   - Validates: bot exists in BotManager
   - Calls enhanced remove_bot (stops thread, clears sessions, unregisters notifications)
   - Removes from setup.json bots array
   - Returns 200 with confirmation

5. **Route wiring** -- Add `/api/bots` to `do_POST` and `do_DELETE` routing.

### Token validation

Use same format check as `validate_setup_config`: token must contain `:`. The Telegram API itself validates the token when the poll loop starts (401 = invalid token, bot auto-stops).

### Non-interference guarantee

BotManager's `_lock` only covers registry operations. Adding/removing a bot holds the lock briefly to update `_bots` dict. Other bots' poll loops, relay threads, and session stores are completely independent. No shared state between bots except the session manager (which uses channel-scoped keys).

### Protect the default bot

The "default" bot is special -- it's the backward-compat single-bot from `telegram_bot_token` config. Removing it would break existing admin.html flows. Decision: allow removing the default bot via API (operator's choice), but log a warning.

## Risk Assessment

**Low risk.** All patterns established. BotInstance start/stop already tested. SessionManager.clear_channel already tested. Only new code is two HTTP handlers and one helper function.

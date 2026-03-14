---
phase: quick
plan: 6
type: tdd
wave: 1
depends_on: []
files_modified:
  - docker/gateway.py
  - docker/test_gateway.py
  - identity/system.md
autonomous: true
requirements: [QUICK-6]

must_haves:
  truths:
    - "User can type /restart in any channel and goosed restarts without wiping their session"
    - "User sees a 'restarting engine' message immediately after /restart"
    - "/restart calls _restart_goose_and_prewarm but does NOT pop the user's session from _session_manager"
  artifacts:
    - path: "docker/gateway.py"
      provides: "_handle_cmd_restart function and router registration"
      contains: "_handle_cmd_restart"
    - path: "docker/test_gateway.py"
      provides: "Tests for /restart command behavior"
      contains: "test_restart"
    - path: "identity/system.md"
      provides: "Updated user commands table with /restart"
      contains: "/restart"
  key_links:
    - from: "_handle_cmd_restart"
      to: "_restart_goose_and_prewarm"
      via: "threading.Thread daemon call"
      pattern: "Thread.*target=_restart_goose_and_prewarm"
    - from: "_command_router"
      to: "_handle_cmd_restart"
      via: "register call"
      pattern: '_command_router\.register\("restart"'
---

<objective>
Add /restart command that bounces goosed without wiping sessions.

Purpose: /clear currently removes the user's session AND restarts the engine. Users sometimes just want to restart the engine (e.g. after adding an MCP extension) without losing their session/conversation history.
Output: Working /restart command registered on _command_router, tested, and documented in system.md.
</objective>

<execution_context>
@/Users/haseeb/.claude/get-shit-done/workflows/execute-plan.md
@/Users/haseeb/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docker/gateway.py (specifically _handle_cmd_clear around line 5457, _restart_goose_and_prewarm around line 5341, _command_router.register calls around line 5624)
@docker/test_gateway.py (specifically TestGeneralizedCommandHandlers around line 1574 for the test pattern)
@identity/system.md (User Commands table around line 63)
</context>

<tasks>

<task type="auto">
  <name>Task 1: RED - Write failing tests for /restart command</name>
  <files>docker/test_gateway.py</files>
  <action>
Add tests to the TestGeneralizedCommandHandlers class (or a new TestRestartCommand class nearby) following the exact pattern of the existing /clear tests.

Tests to write:

1. `test_restart_calls_restart_without_session_pop` - Create a session via `_session_manager.set("slack", "user1", "sid_1")`, set up an active relay on a ChannelState, build ctx with channel/user_id/send_fn/channel_state. Call `_handle_cmd_restart(ctx)` with `_restart_goose_and_prewarm` patched. Assert:
   - `_restart_goose_and_prewarm` was called (via thread start)
   - `send_fn` was called with a message containing "restart" (case-insensitive)
   - Session is STILL in `_session_manager` (NOT popped): `_session_manager.get("slack", "user1") == "sid_1"`
   - Active relay was killed via `state.kill_relay`

2. `test_restart_does_not_pop_session` - Contrast test: set session, call /restart, verify session still exists. Set session, call /clear, verify session is gone. This makes the behavioral difference explicit.

3. `test_restart_registered_on_router` - Assert `gateway._command_router.is_command("/restart")` returns True.

4. `test_restart_falls_back_to_telegram` - Same pattern as test_clear_falls_back_to_telegram: ctx without channel/channel_state keys, verify it uses defaults.

Run: `cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -k "restart" -x`
Tests MUST fail (function does not exist yet).
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -k "restart" -x 2>&1 | tail -5</automated>
    <manual>All restart tests should fail with AttributeError or similar (RED phase)</manual>
  </verify>
  <done>4 failing tests exist that fully specify /restart behavior: restarts engine, keeps session, kills relay, sends user message</done>
</task>

<task type="auto">
  <name>Task 2: GREEN - Implement /restart handler, register, and update docs</name>
  <files>docker/gateway.py, identity/system.md</files>
  <action>
In docker/gateway.py:

1. Add `_handle_cmd_restart` function right after `_handle_cmd_clear` (around line 5477). Pattern is nearly identical to _handle_cmd_clear but WITHOUT the session pop:

```python
def _handle_cmd_restart(ctx):
    """Handle /restart command -- restart engine without clearing session."""
    chat_id = ctx["user_id"]
    state = ctx.get("channel_state", _telegram_state)
    channel = ctx.get("channel", "telegram")
    chat_key = str(chat_id)

    # kill active relay (same as /stop)
    state.kill_relay(chat_key)

    # NOTE: intentionally NOT popping the session -- that's /clear's job
    ctx["send_fn"]("\U0001f504 Restarting engine, give me ~10 seconds...")
    threading.Thread(
        target=_restart_goose_and_prewarm,
        args=(chat_id,),
        daemon=True,
    ).start()
    print(f"[{channel}] engine restart requested by chat {chat_id} (session preserved)")
```

2. Register the command on _command_router (around line 5629, after the existing registrations):
```python
_command_router.register("restart", _handle_cmd_restart, "restart the engine without clearing history")
```

3. In identity/system.md, update the User Commands table (around line 63) to add a row:
```
| `/restart` | restart the engine without clearing conversation history |
```
Add it between `/clear` and `/compact` rows.

4. Also update the TestUnknownSlashCommand test (around line 1231) to include "/restart" in the known commands list if it has a hardcoded list.

Run tests: `cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -k "restart" -x`
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -k "restart" -x && python -m pytest docker/test_gateway.py -k "unknown" -x</automated>
    <manual>All restart tests pass. Unknown command tests still pass.</manual>
  </verify>
  <done>/restart command works: restarts engine, preserves session, registered on router. system.md documents it. All tests green.</done>
</task>

</tasks>

<verification>
Full test suite passes:
```bash
cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -x -q
```

Grep confirms handler exists and is registered:
```bash
grep -n "_handle_cmd_restart\|register.*restart" docker/gateway.py
```

Grep confirms system.md updated:
```bash
grep "/restart" identity/system.md
```
</verification>

<success_criteria>
- /restart handler exists, calls _restart_goose_and_prewarm WITHOUT popping session
- Command registered on _command_router with description
- 4+ tests passing covering: restart behavior, session preservation, router registration, fallback defaults
- system.md User Commands table includes /restart
- Full test suite green (no regressions)
</success_criteria>

<output>
After completion, create `.planning/quick/6-add-restart-command-to-bounce-goosed-wit/6-SUMMARY.md`
</output>

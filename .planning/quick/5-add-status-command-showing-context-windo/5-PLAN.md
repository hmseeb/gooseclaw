---
task: "Add /status command showing context window, provider, session info"
mode: quick
---

# Plan: /status Command

## Task 1: Add /status command handler and register it

**files**: `docker/gateway.py`
**action**:
1. Add `_handle_cmd_status(ctx)` handler function after the existing command handlers (after `/compact`)
2. Register it with `_command_router.register("status", _handle_cmd_status, "show session and provider info")`

The handler will:
- Get current session ID from `_session_manager`
- Load setup.json for provider/model info
- Fetch session data from goosed `/sessions/{id}` endpoint
- Read extensions from goosed `/config` endpoint
- Calculate session uptime from session metadata
- Format and send a status message

Status message format (Telegram markdown):
```
🔧 GooseClaw Status

📡 Provider: OpenRouter
🤖 Model: anthropic/claude-sonnet-4-6
⚡ Mode: auto

💬 Session: 20260313_3
📝 Messages: 12
⏱ Uptime: 2h 34m
🧩 Extensions: 5 active

📊 Context: ~24,000 / 200,000 tokens
▓▓▓░░░░░░░░░░░░ 12%
```

**verify**: Command registered in router, handler function exists
**done**: /status returns formatted status message

---
task: "Add /status command showing context window, provider, session info"
status: complete
---

# Summary

Added `/status` command to the Telegram bot command router in `gateway.py`.

## What it shows

- Provider name and model (from setup.json)
- Goose mode (auto/normal/strict)
- Current session ID, message count, uptime
- Active extensions count (fetched from goosed /config API)
- Estimated context window usage with progress bar

## Implementation

- `_handle_cmd_status(ctx)` handler function
- `_estimate_tokens()` helper (rough ~4 chars/token)
- `_format_duration()` for human-readable uptime
- `_make_progress_bar()` for visual context usage
- `_MODEL_CONTEXT` dict with known model context limits
- Registered as `_command_router.register("status", ...)`

## Files changed

- `docker/gateway.py` - added handler + registration

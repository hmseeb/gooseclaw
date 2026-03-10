# Tools

## Platform

- Agent framework: Goose (by Block)
- Deployed on: Railway (containerized)
- Interface: Telegram
- Persistence: Railway volume at /data

## Notifications

- `notify` is on PATH. pipe text to send it to the user's telegram.
- usage: `echo "your message" | notify`
- this is how scheduled recipes deliver output. without it, headless session output vanishes.
- the gateway also exposes POST /api/notify for programmatic use.

## Identity Files

All identity and memory files live at /data/identity/:

| file | purpose | who writes |
|------|---------|------------|
| soul.md | personality, values, style | onboarding, then user |
| user.md | who the user is | onboarding, then user |
| tools.md | platform info (this file) | template |
| memory.md | long-term facts | agent |
| heartbeat.md | proactive behaviors | user |
| journal/ | session logs | agent |

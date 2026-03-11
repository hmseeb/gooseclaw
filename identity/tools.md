# Tools

## Platform

- Agent framework: Goose (by Block)
- Deployed on: Railway (containerized)
- Interface: Telegram
- Persistence: Railway volume at /data

## CLI Helpers (on PATH)

| command | what it does |
|---------|-------------|
| `notify` | send a message to user's telegram. pipe text or pass as argument |
| `remind "msg" --in 5m` | set a one-shot reminder (fires via telegram) |
| `remind "msg" --at 09:00` | set a reminder at a specific time |
| `remind "msg" --every 1h` | set a recurring reminder |
| `remind list` | list active reminders |
| `remind cancel <id>` | cancel a reminder (first 8 chars of ID ok) |
| `secret get <path>` | read a credential from vault (e.g. `secret get fireflies.api_key`) |
| `secret set <path> "<value>"` | store a credential in vault |
| `secret list` | list all stored credential paths (not values) |
| `secret delete <path>` | remove a credential |

## Notifications

- `notify` sends messages to all paired telegram users via the gateway.
- usage: `echo "your message" | notify` or `notify "your message"`
- this is how scheduled recipes deliver output. without it, headless session output vanishes.
- the gateway also exposes POST /api/notify for programmatic use.

## Reminders

- `remind` sets timers that fire directly via telegram. reliable, no goose sessions involved.
- uses the gateway's built-in reminder engine (10s tick, direct notify_all).
- persists to /data/reminders.json — survives container restarts.
- ALWAYS use `remind` for ad-hoc timers. use `goose schedule` only for complex AI tasks.
- the gateway also exposes /api/reminders for programmatic use.

## Credentials Vault

- location: /data/secrets/vault.yaml (chmod 600)
- NEVER read this file directly into chat. use the `secret` CLI.
- credentials stored here are auto-exported as env vars on container boot.
- format: simple YAML. `service.key` dot-path notation.

## Research Tools (MCP, always available)

- **Context7**: library/framework documentation lookup. no API key needed.
  Use for: React docs, Python library APIs, framework references, etc.
- **Exa**: AI-powered web search. no API key needed.
  Use for: current events, troubleshooting, company research, how-tos.

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

# Tools

## Agent: Goose (by Block)

You are running as a gooseclaw agent deployed on Railway.

### Capabilities

- Read, write, and edit files (developer extension)
- Execute shell commands
- Search the web (if extensions configured)
- Manage scheduled tasks via recipes

### Identity Files

Your identity and memory live at /data/identity/:

| file | purpose | who writes |
|------|---------|------------|
| soul.md | your personality, values, style | onboarding flow, then user |
| user.md | who your user is | onboarding flow, then user |
| tools.md | what you can do (this file) | template |
| memory.md | long-term verified facts | you (agent) |
| heartbeat.md | proactive behaviors | template, then user |
| journal/ | daily session logs | you (agent) |
| persistent-instructions.md | always-on instructions | template |

### Platform

- Deployed on: Railway (containerized)
- Interface: Telegram bot (primary)
- Health check: HTTP on $PORT (for Railway)
- Persistence: Railway volume at /data + optional git auto-push
- Provider: configured via environment variables

### Limitations

- No web UI (goose web is not available in CLI)
- Telegram messages have a 4,096 character limit
- 5-turn limit per incoming telegram message
- No direct internet browsing without browser extensions

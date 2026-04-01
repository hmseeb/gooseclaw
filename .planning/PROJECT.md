# Auto-Generated MCP Extensions

## What This Is

A system within GooseClaw that automatically generates Python MCP server extensions when users provide service credentials. When a user drops an API key, app password, or OAuth token in chat, GooseClaw auto-detects it, vaults the credential, generates a service-specific MCP extension from a template, and registers it with goosed. Future tool calls hit the extension directly (fast path) instead of the slow assistant catch-all.

## Core Value

Credentials in vault should automatically become fast, direct tool access. No manual extension setup, no slow LLM-mediated catch-all path.

## Requirements

### Validated

(None yet -- ship to validate)

### Active

- [ ] Auto-detect credentials dropped in chat and vault them
- [ ] Template system for common service types (IMAP/SMTP, CalDAV, REST API, OAuth)
- [ ] AI selects correct template based on credential type and user intent
- [ ] Generate standalone Python MCP server from template + vault creds
- [ ] Store generated extensions on /data volume (survive redeploys)
- [ ] Auto-register generated extensions with goosed on boot
- [ ] Generated extensions available to both voice and text channels
- [ ] Email template: read, search, send via IMAP/SMTP
- [ ] Calendar template: list events, create events via CalDAV
- [ ] REST API template: generic authenticated API calls (API key, bearer token)
- [ ] OAuth template: handle OAuth flow for Google, GitHub, etc.

### Out of Scope

- Browser-based OAuth consent flow UI (use device flow or manual token paste for now) -- complex frontend work, defer
- Extension marketplace/sharing between users -- single-user system
- Auto-updating extensions when upstream APIs change -- manual regeneration sufficient

## Context

GooseClaw is a self-hosted AI agent platform running on Railway. It uses goosed as the tool runtime with MCP extensions. Currently, voice tool calls that don't have a matching extension fall through to an "assistant" catch-all which is slow (extra LLM hop through goosed). Direct extensions are fast. This feature bridges the gap: every integration becomes a direct extension.

Existing infrastructure:
- Vault (`/data/secrets/vault.yaml`) for credential storage via `secret` CLI
- Goosed extension system (Python MCP servers)
- Extension auto-discovery via goosed `/config` endpoint
- Entrypoint.sh handles boot-time extension registration
- Extensions live in goosed config.yaml under `extensions:` key

## Constraints

- **Runtime**: Python MCP servers (goosed's native extension format)
- **Storage**: /data volume on Railway (persists across deploys)
- **Security**: Credentials read from vault at runtime, never hardcoded in extension code
- **Registration**: Must integrate with existing goosed extension loading mechanism
- **Boot**: Extensions must auto-register on container restart without user intervention

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| MCP server per integration | Matches existing extension pattern, isolation between services | -- Pending |
| Template-based generation | Scalable, AI picks template, avoids generating from scratch each time | -- Pending |
| Store on /data volume | Survives Railway redeploys, consistent with other persistent data | -- Pending |
| Auto-detect credentials in chat | Frictionless UX, user doesn't need to learn commands | -- Pending |

---
*Last updated: 2026-04-01 after initialization*

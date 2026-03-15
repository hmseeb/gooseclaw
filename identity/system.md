# System Reference

Platform docs, tool references, API endpoints, and procedures. Vectorized for semantic search.
Behavioral instructions are in system-core.md (loaded in session context).

---

## Platform

- **Goose**: LLM sessions, MCP extensions, tools, recipes
- **Gateway**: Telegram bots, channel plugins, jobs, notifications, setup wizard, identity files, vault

### Request Routing

| User wants | Route to |
|-----------|----------|
| MCP extension / tool | Goose config (`/data/config/config.yaml`). Engine restart ~10s. Research via Exa/Context7 first |
| Messaging platform (Slack, Discord) | Channel plugin. Write `/data/channels/<name>.py`. Hot-reloadable |
| Schedule / remind / automate | `job` or `remind` CLI exclusively |
| Change LLM provider / model | Setup wizard or `POST /api/setup/save` |
| Connect to service API | Integration flow: vault credentials, test, record via knowledge_upsert |

### Discovery Endpoints

| Endpoint | Returns |
|----------|---------|
| GET /api/telegram/status | bots, status, paired users, pairing codes |
| GET /api/channels | loaded channel plugins and status |
| GET /api/watchers | watchers, type, status, stats |
| GET /api/setup | current config including bots and channel settings |

### User Commands

`/help`, `/stop`, `/clear` (wipe + restart ~15s), `/restart` (no wipe), `/compact` (summarize to free context)

### Default MCP Extensions

developer (file/shell), context7 (docs), exa (web search), memory (auto-learn), knowledge (vector KB)

To add extensions: append to `extensions:` in config.yaml, trigger engine restart via `POST /api/setup/save`. Context resets but gateway/bots/jobs stay running.

---

## Tools

### CLI

| Command | What it does |
|---------|-------------|
| `notify "msg"` | broadcast to all channels. also: `echo "msg" \| notify` |
| `job create "name" --run "cmd" --every 1h` | recurring job (also: `--cron`, `--in 5m` for one-shot) |
| `job list` / `job cancel <id>` / `job run <id>` | manage jobs |
| `remind "msg" --in 5m` | text reminder (also: `--at 09:00`, `--every 1h`) |
| `secret set <path> "<value>"` | store credential in vault |
| `secret get <path>` / `secret list` / `secret delete <path>` | manage credentials |

Job flags: `--provider`/`--model` for LLM override, `--until` for auto-expiry, `--notify-channel <name>` for channel targeting.

### Notifications

`notify` broadcasts to all channels. `POST /api/notify` for programmatic use (optional `channel` param). Without notify, headless/scheduled output is lost.

### Jobs and Reminders

Use `job` or `remind` CLI exclusively. Never CronCreate or goose schedule (broken).

Unified engine: 10s tick, persists to /data/jobs.json, survives restarts. Max 5 concurrent. Recipes go in /data/recipes/ and MUST pipe through `notify`. `goose run --recipe` requires `--text` flag in headless mode.

### Watchers

Two-tier engine for monitoring external events.

- **Passthrough (tier 1):** No LLM. Template transform → deliver instantly. Zero cost.
- **Smart (tier 2):** LLM processes data (summarize/filter/act) → deliver.

Types: `webhook` (push), `feed` (poll), `stream` (future).

| Method | Path | What it does |
|--------|------|-------------|
| POST | /api/watchers | create |
| GET | /api/watchers | list all |
| PUT | /api/watchers/`<id>` | update |
| DELETE | /api/watchers/`<id>` | delete |
| POST | /api/watchers/batch | create multiple |
| DELETE | /api/watchers/batch | delete multiple (`{"ids": [...]}`) |
| POST | /api/webhooks/`<name>` | receive webhook events (public, no auth) |

Required: `name`, `type`. Optional: `channel`, `smart`, `transform`, `prompt`, `source`, `interval` (default 300s), `secret` (HMAC), `filter`.

**Filters** (passthrough only): `"field operator 'value'"`. Operators: contains, not_contains, equals, not_equals, matches (regex), gt, lt, gte, lte. Missing fields pass through.

**Templates**: `{{variable}}` syntax from incoming JSON.

**Feed watchers**: poll URL, hash content, fire on change. Supports RSS/Atom.

**Webhook receivers**: public at `/api/webhooks/<name>`. Optional HMAC-SHA256 verification via `secret`.

When user wants monitoring:
1. Simple condition → passthrough with `filter`
2. Needs reasoning → `smart: true`
3. Record via knowledge_upsert

### Verbosity

Per-channel: `quiet`, `balanced` (default), `verbose`. Set via setup wizard or `POST /api/setup/channels/verbosity`.

---

## Extending the Platform

### Bots

| Method | Path | What it does |
|--------|------|-------------|
| POST | /api/bots | add bot (name, token, optional provider/model) |
| DELETE | /api/bots/`<name>` | remove bot |

One container, one gateway, multiple bots and channels. Never create separate processes.

Telegram bots: gateway handles polling, sessions, pairing, isolation. Single-use pairing codes, rotate after each pair.
Channel plugins: platform-native access control, no pairing needed.

### Channel Plugin Contract

Python files in `/data/channels/` exporting `CHANNEL` dict. Required: `send(text)` → `{"sent": bool, "error": str}`. Optional: `poll()`, `setup()`/`teardown()`, `typing`, custom `commands`. Files starting with `_` skipped.

### Integrations

1. Research service (Exa/Context7)
2. Vault credentials
3. Test with simple API call
4. Record via knowledge_upsert (type: "integration")
5. Prove it's connected

### Password Recovery

Recovery secret is at `/data/.recovery_secret`. User can also find it in first-boot deploy logs or Railway env vars as `GOOSECLAW_RECOVERY_SECRET`.

Recovery endpoint: `POST /api/auth/recover` with `{"secret": "..."}`. Returns temporary password. Never store or log credentials from this flow.

---

## Schema References

@schemas/soul.schema.md
@schemas/user.schema.md
@schemas/learnings.schema.md

# System Reference

Platform docs, tool references, API endpoints, and procedures. Vectorized for semantic search.
Behavioral instructions are in system-core.md (loaded in session context).

---

## Platform

- **Goose**: LLM sessions, MCP extensions, tools, recipes
- **Gateway**: Telegram bots, plugins, jobs, notifications, setup wizard, identity files, vault

### Request Routing

| User wants | Route to |
|-----------|----------|
| MCP extension / tool | Goose config (`/data/config/config.yaml`). Engine restart required. Research via Exa/Context7 first |
| Messaging platform (Slack, Discord) | Plugin. Write `/data/plugins/<name>.py`. Hot-reloadable |
| Schedule / remind / automate | `job` or `remind` CLI exclusively |
| Change LLM provider / model | Setup wizard or `POST /api/setup/save` |
| Connect to service API | Integration flow: vault credentials, test, record via knowledge_upsert |

### Discovery Endpoints

| Endpoint | Returns |
|----------|---------|
| GET /api/telegram/status | bots, status, paired users, pairing codes |
| GET /api/plugins | loaded plugins and status |
| GET /api/watchers | watchers, type, status, stats |
| GET /api/setup | current config including bots and channel settings |

### User Commands

`/help`, `/stop`, `/clear` (wipe + restart), `/restart` (no wipe), `/compact` (summarize to free context)

### Default MCP Extensions

developer (file/shell), context7 (docs), exa (web search), memory (auto-learn), knowledge (system docs), mem0-memory (long-term memory + graph)

To add extensions: append to `extensions:` in config.yaml, trigger engine restart via `POST /api/setup/save`. Context resets but gateway/bots/jobs stay running.

---

## Tools

### CLI

| Command | What it does |
|---------|-------------|
| `notify "msg"` | broadcast to all channels. also: `echo "msg" \| notify` |
| `job create "name" --run "cmd" --weekdays 09:00` | Mon-Fri at specified time |
| `job create "name" --run "cmd" --daily 12:00` | every day at specified time |
| `job create "name" --run "cmd" --weekly mon,fri 10:00` | specific days at time |
| `job create "name" --run "cmd" --monthly 1 09:00` | day-of-month at time |
| `job create "name" --run "cmd" --every 1h` | recurring interval |
| `job create "name" --run "cmd" --in 5m` | one-shot delay |
| `job edit <id> --weekdays 10:00` | change schedule of existing job |
| `job edit <id> --run "new-cmd" --name "new-name"` | change command/name of existing job |
| `job edit <id> --disable` / `--enable` | toggle job on/off |
| `job list` / `job cancel <id>` / `job run <id>` | manage jobs |
| `remind "msg" --in 5m` | text reminder (also: `--at 09:00`, `--every 1h`) |
| `secret set <path> "<value>"` | store credential in vault |
| `secret get <path>` / `secret list` / `secret delete <path>` | manage credentials |

**Schedule flags (prefer these over --cron):** `--weekdays HH:MM`, `--daily HH:MM`, `--weekends HH:MM`, `--weekly DAYS HH:MM` (days: mon,tue,wed,thu,fri,sat,sun), `--monthly DAY HH:MM`. Fallback: `--cron "expr"` for complex patterns only.

Other job flags: `--provider`/`--model` for LLM override, `--until` for auto-expiry, `--notify-channel <name>` for per-job channel targeting.

### Notifications

**Per-job targeting:** `--notify-channel <name>` on any job/reminder to send output to a specific channel only (e.g. `--notify-channel slack`). Overrides the global default.

**Global default:** Set `default_notify_channel` in setup.json to route ALL notifications to one channel by default (e.g. `"default_notify_channel": "telegram"`). To set it: write to setup.json via `POST /api/setup/save` or edit `/data/config/setup.json` directly. Per-job `--notify-channel` always overrides the global default.

**Broadcast:** When neither per-job nor global default is set, `notify` broadcasts to all connected channels. `POST /api/notify` for programmatic use (optional `channel` param). Without notify, headless/scheduled output is lost.

When a user asks to send notifications to a specific channel, ask whether they want it for just this job (per-job) or for all notifications (global default).

### Jobs and Reminders

Use `job` or `remind` CLI exclusively. Never CronCreate or goose schedule (broken).

**Always use named schedule flags** (`--weekdays`, `--daily`, `--weekly`, `--monthly`, `--weekends`) instead of `--cron`. Named flags are unambiguous and validated server-side. Reserve `--cron` for patterns that named flags can't express (e.g. "every 15 minutes" or "every 6 hours").

**To modify an existing job, use `job edit <id>`.** Accepts the same schedule flags as `create` plus `--name`, `--run`, `--enable`, `--disable`. Never delete and recreate a job just to change its schedule or command.

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

## Persistence (Surviving Deploys)

The container rebuilds on every deploy. Only `/data` (Railway volume) survives. Runtime installs are pre-configured to persist:

| What | Persists to | How |
|------|------------|-----|
| pip packages | `/data/pip-packages` | `PIP_TARGET` set automatically |
| npm packages | `/data/npm-global` | `NPM_CONFIG_PREFIX` set automatically |
| Custom binaries | `/data/bin` | On PATH, download/compile here |
| Shared libraries | `/data/lib` | On `LD_LIBRARY_PATH` |
| apt packages, models, anything else | `/data/boot-setup.sh` | Commands re-run on every boot (as root) |
| Background processes (bridges, daemons) | `/data/boot-services.sh` | Re-run on every boot (as gooseclaw user) |

**For apt packages or anything that can't live on /data directly:** append the install command to `/data/boot-setup.sh`. It runs automatically on every boot as root. Example:
```bash
echo 'apt-get install -y ffmpeg' >> /data/boot-setup.sh
```

**For background processes** (bridges, watchers, daemons) that the gateway needs to restart/kill at runtime: put them in `/data/boot-services.sh`. It runs as the `gooseclaw` user, so the gateway can manage these processes. Example:
```bash
echo 'node /data/my-bridge/index.js &' >> /data/boot-services.sh
```

**IMPORTANT:** Never start background processes in `boot-setup.sh`. It runs as root, so the gateway (non-root) cannot kill/restart them. Always use `boot-services.sh` for processes.

**For binaries:** download to `/data/bin/` (already on PATH).

**For ML models:** download to `/data/models/` or similar.

When installing anything for the user, ALWAYS use the persistent paths above. Never install to the container filesystem directly — it will be lost on next deploy.

---

## Extending the Platform

### Bots

| Method | Path | What it does |
|--------|------|-------------|
| POST | /api/bots | add bot (name, token, optional provider/model) |
| DELETE | /api/bots/`<name>` | remove bot |

One container, one gateway, multiple bots and plugins. Never create separate processes.

Telegram bots: gateway handles polling, sessions, pairing, isolation. Single-use pairing codes, rotate after each pair.
Plugins: platform-native access control, no pairing needed.

### Plugin Contract

Python files in `/data/plugins/` exporting `CHANNEL` dict. Required: `send(text)` → `{"sent": bool, "error": str}`. Optional: `poll()`, `setup()`/`teardown()`, `typing`, custom `commands`. Files starting with `_` skipped.

v2 contract: plugins can pass `InboundMessage(user_id, text, channel, media, reply_to_text)` to the relay. When `reply_to_text` is set, the gateway prepends `[replying to: "..."]` context so the LLM knows which message the user is responding to. Truncated to 500 chars. Telegram bots extract this automatically from `reply_to_message`.

### Integrations

1. Research service (Exa/Context7)
2. Vault credentials
3. Test with simple API call
4. Record via memory_add (mem0 stores it with automatic extraction)
5. Prove it's connected

### MCP Extension Credentials

Vaulting secrets is NOT enough. Many MCP extensions have their own credential stores (files, databases, config dirs) separate from the gateway vault and env vars.

Rule: **vaulted != configured.** Always research how the extension actually reads credentials, write them in that format, and verify the extension works before telling the user it's ready.

1. Research the extension's auth mechanism (env vars? config file? credential dir? OAuth flow?)
2. Vault raw secrets for persistence across restarts
3. Write credentials in the format the extension expects
4. Test the extension actually works (call a tool, not just check status)
5. Record the setup steps via `knowledge_upsert` so you can redo it after a redeploy

### Password Recovery

Recovery secret is at `/data/.recovery_secret`. User can also find it in first-boot deploy logs or Railway env vars as `GOOSECLAW_RECOVERY_SECRET`.

Recovery endpoint: `POST /api/auth/recover` with `{"secret": "..."}`. Returns temporary password. Never store or log credentials from this flow.

---

## Voice Channel

Real-time voice via Gemini Live API (WebSocket). Available at `/voice`.

**Architecture:** Browser ↔ Gateway (WebSocket) ↔ Gemini Live API. Gateway routes tool calls directly to MCP servers via JSON-RPC (no goosed LLM hop). Platform extensions and complex tasks fall back through goosed.

**Direct MCP tools (fast, <1s):** knowledge, mem0-memory, context7, exa, gmail_email, brave_search, and all auto-generated extensions. These bypass goosed entirely.

**Goosed fallback (slower):** developer (file/shell), assistant catch-all (reminders, jobs, multi-step tasks).

**Browser support:** Chrome (recommended), Firefox (likely works). Safari/WebKit NOT supported (TCP drops during audio streaming).

**Voice LLM:** Gemini 3.1 Flash Live Preview. Voice name configurable per user (saved in preferences).

**Identity:** Voice loads the same identity files as text (soul.md, user.md, system-core.md, turn-rules.md, system.md).

## Research Tools

Context7 (docs), Exa (web search). Use proactively before guessing. If unavailable, fall back to training knowledge and disclose.

## Media

Text input only (text channels). Voice channel accepts speech input. For non-text in text channels, ask the user to describe it.

## Data Requests

- "what do you know about me?": conversational summary, never raw files
- "delete/forget my data": confirm intent, wipe all personal data, reset to onboarding state
- "export my data": summarize and send via current channel

---

## Auto-Extension Generation

When a user provides credentials (API keys, app passwords, tokens), auto-generate a dedicated MCP extension so the service becomes a fast, direct tool.

### Flow

1. User drops credential in chat (or says "set up my Gmail")
2. Ask: "Want me to set this up as a direct integration?"
3. If yes: vault via `secret set`, then POST to `/api/credential-setup`
4. System generates MCP server, validates, registers with goosed, restarts
5. Tell user what was created

### API: POST /api/credential-setup

```json
{
  "credential_value": "the-api-key-or-password",
  "service_name": "gmail",
  "credential_type": "app_password",
  "user_hint": "email",
  "base_url": "https://api.example.com/v1"
}
```

### Templates

| Template | Use for | Tools provided |
|----------|---------|----------------|
| `email_imap` | Gmail/Outlook app passwords | search, read, send |
| `rest_api` | Any API key/bearer token service (fallback) | GET, POST, PUT, DELETE |

### Backfill

For credentials already in vault (`secret list`), generate extensions without re-pasting. Reference existing vault keys.

### After Generation

Tell the user what was created and what they can now do. Example: "set up your Gmail. you can now say 'check my email' and I'll hit it directly, no roundabout."

---

## Schema References

@schemas/soul.schema.md
@schemas/user.schema.md

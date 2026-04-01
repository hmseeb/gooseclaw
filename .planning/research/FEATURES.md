# Feature Research: Auto-Generated MCP Extensions

**Domain:** AI agent platform extension auto-generation from user credentials
**Researched:** 2026-04-01
**Confidence:** HIGH

## Feature Landscape

### Table Stakes (Users Expect These)

Features users assume exist. Missing these = system is useless.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Credential detection in chat | Core value prop. User drops an API key, system recognizes it automatically. Without this, user must manually configure everything (defeats the purpose). | MEDIUM | Use regex pattern matching for known prefixes (sk-, ghp_, AKIA, xoxb-, etc.) plus high-entropy string detection for generic API keys. Existing tools like secrets-patterns-db have 1600+ patterns. Start with the top 20 most common services. LLM fallback for ambiguous credentials. |
| Vault storage of detected credentials | Credentials must be stored securely, never hardcoded in generated code. Existing vault infrastructure (`/data/secrets/vault.yaml` via `secret` CLI) already handles this. | LOW | Leverage existing `secret set service.key value` pattern. Extend to auto-derive the service/key path from detected credential type. |
| Template system for common service types | AI generating MCP servers from scratch each time is slow, error-prone, and hallucination-heavy. Templates guarantee working code. | MEDIUM | Need templates for: IMAP/SMTP (email), CalDAV (calendar), REST API (generic bearer/API-key), and OAuth services. Each template is a Python FastMCP server with vault credential injection. |
| AI-driven template selection | User says "here's my email password" or drops an app password. System must figure out: this is IMAP/SMTP, use email template. Without AI mediation, user needs to specify template manually. | MEDIUM | LLM classifies credential type + user intent into template choice. Structured output (JSON) with template_id, service_name, credential_mapping. |
| Code generation from template + credentials | The actual generation step. Template + credential references + service config = standalone Python MCP server file. | MEDIUM | Jinja2 templating against FastMCP pattern already used in codebase (knowledge/server.py, memory/server.py). Generated code reads from vault at runtime via `secret get`, never contains raw credentials. |
| Extension registration with goosed | Generated extension must appear in goosed config.yaml under `extensions:` key so goosed loads it. Without this, the generated code sits on disk doing nothing. | MEDIUM | Write YAML entry to config.yaml matching existing extension format (type: stdio, cmd: python3, args: [path], envs: {}, timeout: 300). Requires goosed restart to pick up new extension. |
| Persistent storage on /data volume | Extensions must survive Railway redeploys. `/data` is the persistent volume. | LOW | Store generated extensions at `/data/extensions/` with predictable naming. Already established pattern for persistent data in the codebase. |
| Boot-time auto-registration | On container restart, all previously generated extensions must re-register without user intervention. | MEDIUM | Extend entrypoint.sh extension sync logic to scan `/data/extensions/` and inject any found extensions into config.yaml. Pattern already exists for syncing template extensions. |
| Email template (IMAP/SMTP) | Explicitly listed as a requirement. Email is the most common integration users want. Multiple existing MCP servers prove the pattern (mcp-email-server, mail-mcp on PyPI). | HIGH | Tools: read_inbox, search_email, send_email, read_email. Use Python imaplib/smtplib. Reference existing implementations for battle-tested patterns. Needs IMAP host/port/user/pass and SMTP host/port/user/pass from vault. |
| REST API template (generic) | Catch-all for services that just need an API key + base URL. Covers dozens of services with one template. | MEDIUM | Tools: api_get, api_post, api_put, api_delete. User provides base URL and auth method (bearer token, API key header, query param). Template wraps httpx with auth injection from vault. |
| Extension availability on both channels | Both voice and text channels must be able to use generated extensions. Since extensions register with goosed (not gateway), this is automatic. | LOW | Goosed handles tool routing to extensions regardless of channel. Voice tool discovery (`_discover_voice_tools`) already queries goosed for available tools. No special work needed. |

### Differentiators (Competitive Advantage)

Features that set the product apart. Not required, but valuable.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Zero-config credential detection | User doesn't type a command. Just pastes a key in chat. AI handles the rest with confirmation. "I see you pasted an API key for Anthropic. Want me to set that up?" | MEDIUM | Pattern matching + AI confirmation flow. Conversational UX rather than command-based. |
| Credential validation before generation | Before generating an extension, validate that the credential actually works (e.g., IMAP login succeeds, API key returns 200). Saves user from getting a broken extension. | MEDIUM | Per-template validation logic. IMAP: try login. REST API: try GET /. CalDAV: try PROPFIND. Fast-fail with clear error message. |
| Extension health checking | After generation, verify the extension actually works before registering it. Catches config issues early. | MEDIUM | Run the generated MCP server in a subprocess, send a test tool call, verify response. If it fails, report the error to the user instead of silently registering a broken extension. |
| Calendar template (CalDAV) | Calendar access is high-value and CalDAV is a universal protocol. Works with Google Calendar, iCloud, Nextcloud, FastMail. Existing MCP servers exist (chronos-mcp, mcp-caldav). | HIGH | Tools: list_calendars, list_events, create_event, delete_event. Use python-caldav library. Needs CalDAV URL, username, password from vault. Requires adding caldav pip package to Docker image. |
| Extension management (CRUD) | User asks "what extensions do I have?" or "remove the email extension." Needs list, remove, update operations on generated extensions. | LOW | List: scan /data/extensions/ directory. Remove: delete file + remove from config.yaml + restart goosed. Update: regenerate from template with new credentials. |
| Template preview / dry-run | Generate the extension code but don't register it yet. Show user what tools it would provide, let them approve before activation. | LOW | Generate to temp directory, parse the @mcp.tool() decorated functions to extract tool names and descriptions, present to user, then move to /data/extensions/ on approval. |
| Auto-detection of service from credential format | Recognize that `xoxb-` is Slack, `ghp_` is GitHub, `sk-ant-` is Anthropic without user telling you. Makes the experience feel magical. | LOW | Maintain a prefix-to-service mapping table. ~30 entries covers the most common services. Fallback to LLM classification for unknown formats. |
| Multi-credential extensions | Some services need multiple credentials (e.g., AWS needs access key + secret key + region, IMAP needs host + port + user + pass). Template system handles credential groups. | MEDIUM | Template metadata defines required credential slots. AI maps user-provided credentials to slots. Vault stores as service.access_key, service.secret_key, etc. Guided flow prompts for missing pieces. |
| Hot-reload extensions without restart | Currently adding an extension requires goosed restart, which kills all active sessions. Hot-reload means zero downtime. | HIGH | Goosed doesn't natively support hot-reload of extensions. Options: (1) restart goosed + re-prewarm sessions (existing `_restart_goose_and_prewarm` pattern), (2) investigate goosed /config API for runtime extension addition, (3) use MCP proxy pattern (mcp-hmr). Start with option 1 since it's proven. |
| OAuth device flow support | For services like Google, GitHub that need OAuth. Device flow lets user authorize without a browser UI in the container. | HIGH | Implement RFC 8628 device authorization flow. Poll for token completion. Store refresh token in vault. Template handles token refresh automatically. Gateway to Google Calendar, Gmail, GitHub, Slack. |
| Extension rollback | If a newly generated extension causes problems, revert to previous state. | MEDIUM | Before registering new extension, snapshot current config.yaml. On failure detection, restore snapshot and restart. Simple file-based versioning. |
| OpenAPI-to-MCP generation | For services with published OpenAPI specs, auto-generate a full MCP server from the spec instead of using generic REST template. | HIGH | Multiple existing tools: openapi-mcp-generator, FastMCP OpenAPI integration, AWS openapi-mcp-server. Could integrate FastMCP's OpenAPI support directly. Much richer toolset than generic REST. |

### Anti-Features (Commonly Requested, Often Problematic)

Features that seem good but create problems.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| AI-generated extension code from scratch (no templates) | "Just let the AI write the whole MCP server" | LLM hallucinations in generated code are hard to debug. Non-deterministic. Testing is impossible. Security review of arbitrary generated code is a nightmare. | Use templates with parameterized slots. AI picks the template and fills parameters, never writes arbitrary code. Templates are human-reviewed and tested once. |
| Browser-based OAuth consent flow | "Build a web UI for OAuth login" | Requires frontend work, session management, redirect handling, CORS, HTTPS callbacks. Massive scope increase for a headless container. | Use OAuth device flow (RFC 8628) or manual token paste. Both work headless. |
| Extension marketplace / sharing | "Let users share extensions" | GooseClaw is single-user. Sharing implies multi-tenancy, permission models, versioning, trust boundaries. Zero current demand. | Each GooseClaw instance generates its own extensions from templates. |
| Auto-updating extensions when APIs change | "Extensions should auto-update when Slack changes their API" | API changes are rare and breaking. Auto-update could break working extensions. Detection is unreliable. | Manual regeneration. User says "rebuild my Slack extension" and gets fresh code from latest template. |
| Universal credential scanning of all messages | "Scan every message for credentials" | Performance overhead on every message. False positives (hex strings in code, UUIDs, hash outputs) would annoy users. Privacy concern of scanning all content. | Detect credentials only when user explicitly shares them or uses a trigger phrase ("here's my API key"). Context-aware detection, not blanket scanning. |
| Extension dependency management (pip install at runtime) | "Auto-install pip packages needed by generated extensions" | Opens supply chain attack surface. Package conflicts between extensions. Unreliable in containerized env with pinned deps. | Pre-install all needed packages in the Docker image. Templates only use libraries already in the image (imaplib, smtplib, httpx, caldav). Add new deps to Dockerfile when adding new template types. |
| Real-time credential rotation | "Auto-rotate API keys on a schedule" | Most services don't support programmatic rotation. Adds cron complexity per service. OAuth refresh is different from rotation. | Handle OAuth token refresh inside templates (that's real). For API keys, user replaces manually when needed. |
| Multi-account per service type | "Support multiple Gmail accounts, multiple Slack workspaces" | Namespace collisions, UI complexity for selecting which account, vault path conflicts. | One credential set per service type. User regenerates to switch accounts. Can revisit if demand emerges. |

## Feature Dependencies

```
[Credential Detection]
    └──requires──> [Vault Storage]
                       └──required by──> [Code Generation]

[AI Template Selection]
    └──requires──> [Template System]
                       └──required by──> [Code Generation]
                                             └──required by──> [Extension Registration]
                                                                    └──required by──> [Boot-time Auto-Registration]

[Credential Validation] ──enhances──> [Credential Detection]

[Extension Health Check] ──enhances──> [Extension Registration]

[Template Preview / Dry-run] ──enhances──> [Code Generation]

[Hot-reload] ──enhances──> [Extension Registration]

[Extension Management (CRUD)] ──requires──> [Extension Registration]

[OAuth Device Flow] ──requires──> [Template System] + [Vault Storage]

[OpenAPI-to-MCP] ──enhances──> [Template System]

[Multi-credential Extensions] ──enhances──> [AI Template Selection]

[Extension Rollback] ──requires──> [Extension Registration]

[Auto Service Detection] ──enhances──> [Credential Detection]

[Calendar Template] ──requires──> [Template System] + caldav pip package in Docker image
[Email Template] ──requires──> [Template System] + imaplib/smtplib (stdlib, already available)
[REST API Template] ──requires──> [Template System] + httpx (already available)
```

### Dependency Notes

- **Code Generation requires Template System + Vault Storage:** Templates define the structure, vault provides credential references. Both must exist before generation works.
- **Extension Registration requires Code Generation:** Nothing to register until code is generated. Registration also requires goosed restart mechanism (already exists as `_restart_goose_and_prewarm`).
- **Boot-time Auto-Registration requires Extension Registration:** The persistent registration pattern must be established before boot-time discovery can scan for extensions.
- **OAuth Device Flow requires Template System + Vault Storage:** OAuth templates need both the template structure and vault for token/refresh-token storage.
- **Calendar Template requires caldav pip package:** Must be added to Docker image. Email and REST templates use stdlib or already-installed packages.
- **Hot-reload enhances Extension Registration but is not required:** Can always fall back to goosed restart + session prewarm. Hot-reload is a UX improvement, not a functional blocker.

## MVP Definition

### Launch With (v1)

Minimum viable product. Proves "credentials become extensions automatically."

- [ ] **Credential detection in chat** -- regex for known prefixes + LLM classification for ambiguous credentials. Context-aware, not blanket scanning.
- [ ] **Vault storage** -- leverage existing `secret` CLI. Auto-derive service.key paths from detected credential type.
- [ ] **Template system with 2 templates** -- email (IMAP/SMTP) and generic REST API. These cover the widest range of use cases with minimum templates.
- [ ] **AI template selection** -- LLM picks template based on credential type + user context. Structured JSON output.
- [ ] **Code generation** -- Jinja2 templating producing FastMCP Python servers matching existing codebase patterns (knowledge/server.py, memory/server.py).
- [ ] **Extension registration** -- write to config.yaml, restart goosed with session prewarm via existing `_restart_goose_and_prewarm`.
- [ ] **Boot-time auto-registration** -- extend entrypoint.sh to discover /data/extensions/ and inject into config.yaml.
- [ ] **Persistent storage** -- generated extensions stored at /data/extensions/ on Railway volume.

### Add After Validation (v1.x)

Features to add once the core generation pipeline is proven.

- [ ] **Calendar template (CalDAV)** -- add when email template is proven stable. Requires adding caldav pip package to Docker image.
- [ ] **Credential validation** -- add when users report broken extensions due to bad credentials. Per-template validation (IMAP login test, API health check).
- [ ] **Extension health checking** -- add when registration reliability becomes a user concern.
- [ ] **Extension management (list/remove/update)** -- add when users accumulate multiple generated extensions and need control.
- [ ] **Auto service detection from credential format** -- build prefix-to-service mapping for top 30 services. Low effort, high polish.
- [ ] **Multi-credential extensions** -- add when AWS or similar multi-key services are requested. Template metadata defines credential slots.
- [ ] **Template preview / dry-run** -- add when users want more control before activation. Show tool list, get confirmation.

### Future Consideration (v2+)

Features to defer until the generation pipeline is mature and battle-tested.

- [ ] **OAuth device flow** -- complex but unlocks Google/GitHub/Slack. Defer until simpler auth templates are proven.
- [ ] **Hot-reload without restart** -- investigate goosed capabilities for runtime extension addition. Restart + prewarm is acceptable for v1.
- [ ] **OpenAPI-to-MCP generation** -- powerful but complex. Existing tools (FastMCP OpenAPI, openapi-mcp-codegen) could be integrated.
- [ ] **Extension rollback** -- add when extension conflicts or failures become a real problem in production use.

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Credential detection in chat | HIGH | MEDIUM | P1 |
| Vault storage (auto-derive paths) | HIGH | LOW | P1 |
| Template system (email + REST) | HIGH | MEDIUM | P1 |
| AI template selection | HIGH | MEDIUM | P1 |
| Code generation (Jinja2 + FastMCP) | HIGH | MEDIUM | P1 |
| Extension registration (config.yaml) | HIGH | MEDIUM | P1 |
| Boot-time auto-registration | HIGH | LOW | P1 |
| Persistent storage (/data/extensions/) | HIGH | LOW | P1 |
| Calendar template (CalDAV) | MEDIUM | HIGH | P2 |
| Credential validation | MEDIUM | MEDIUM | P2 |
| Extension health checking | MEDIUM | MEDIUM | P2 |
| Extension management (CRUD) | MEDIUM | LOW | P2 |
| Auto service detection | MEDIUM | LOW | P2 |
| Multi-credential extensions | MEDIUM | MEDIUM | P2 |
| Template preview / dry-run | LOW | LOW | P2 |
| OAuth device flow | HIGH | HIGH | P3 |
| Hot-reload without restart | MEDIUM | HIGH | P3 |
| OpenAPI-to-MCP generation | MEDIUM | HIGH | P3 |
| Extension rollback | LOW | MEDIUM | P3 |

**Priority key:**
- P1: Must have for launch. Without these, the system doesn't function.
- P2: Should have. Add when the core pipeline is validated and stable.
- P3: Nice to have. Complex features that unlock new service categories or improve DX.

## Competitor Feature Analysis

| Feature | Existing MCP Email Servers (mcp-email-server, mail-mcp) | Goose Recipe System | OpenAPI-to-MCP Tools (FastMCP, AWS, Stainless) | Our Approach |
|---------|----------------------------------------------------------|---------------------|-----------------------------------------------|--------------|
| Credential handling | Environment variables or config files, manual setup | Recipes reference extensions, no credential management | API key via env vars, some support OAuth | Auto-detect from chat, vault storage, runtime injection. Zero-config from user perspective. |
| Extension creation | Pre-built, install via pip/npx | YAML recipe files, no code generation | Code generation from OpenAPI specs | Template-based generation. AI picks template, Jinja2 generates FastMCP server. |
| Service coverage | Single service per server | Composable via extension lists | Any service with OpenAPI spec | Start narrow (email, REST), expand via templates. |
| Registration | Manual config.yaml editing or CLI flags | Built into goose session --with-extension | Manual or CLI registration | Auto-registration in config.yaml + goosed restart. |
| Persistence | User responsibility | Recipes are files, persist naturally | Generated code persists where user saves it | /data/extensions/ on persistent volume, boot-time discovery. |
| UX flow | Install package, configure env vars, add to config | Write YAML, run goose session | Run CLI tool, copy output | Drop credential in chat, confirm, done. |

## Sources

- [MCP Email Server (IMAP/SMTP)](https://github.com/ai-zerolab/mcp-email-server) -- existing Python IMAP/SMTP MCP implementation on PyPI (HIGH confidence)
- [Chronos MCP (CalDAV)](https://github.com/democratize-technology/chronos-mcp) -- existing CalDAV MCP server with multi-account support (HIGH confidence)
- [mcp-caldav](https://dev.to/madbonez/give-your-ai-real-calendar-superpowers-with-mcp-caldav-5h69) -- CalDAV MCP server supporting Nextcloud, iCloud, FastMail (MEDIUM confidence)
- [openapi-mcp-generator](https://github.com/harsha-iiiv/openapi-mcp-generator) -- CLI tool for OpenAPI-to-MCP generation (HIGH confidence)
- [FastMCP OpenAPI integration](https://gofastmcp.com/integrations/openapi) -- FastMCP native OpenAPI support (HIGH confidence)
- [openapi-mcp-codegen](https://github.com/cnoe-io/openapi-mcp-codegen) -- generates Python MCP packages from OpenAPI specs (HIGH confidence)
- [secrets-patterns-db](https://github.com/mazen160/secrets-patterns-db) -- 1600+ regex patterns for secret detection (HIGH confidence)
- [MCP Hot-Reload (mcp-hmr)](https://pypi.org/project/mcp-hmr/) -- hot module replacement for MCP servers, v0.0.3.3 March 2026 (MEDIUM confidence)
- [Goose extension docs](https://block.github.io/goose/docs/getting-started/using-extensions/) -- official extension configuration (HIGH confidence)
- [Goose recipe system](https://www.pulsemcp.com/building-agents-with-goose) -- YAML-based workflow packaging (MEDIUM confidence)
- [MCP authentication patterns](https://aembit.io/blog/mcp-authentication-and-authorization-patterns/) -- OAuth, API key patterns for MCP (MEDIUM confidence)
- [GitGuardian secret detection](https://blog.gitguardian.com/secrets-in-source-code-episode-3-3-building-reliable-secrets-detection/) -- regex + entropy approaches (HIGH confidence)
- [Stainless MCP from OpenAPI](https://www.stainless.com/blog/generate-mcp-servers-from-openapi-specs) -- generate MCP servers from OpenAPI specs (HIGH confidence)
- GooseClaw codebase: knowledge/server.py, memory/server.py (FastMCP patterns), entrypoint.sh (extension registration), gateway.py (goosed restart, channel hot-reload) -- PRIMARY source

---
*Feature research for: GooseClaw auto-generated MCP extensions*
*Researched: 2026-04-01*

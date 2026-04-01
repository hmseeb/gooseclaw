# Architecture Research: Auto-Generated MCP Extensions

**Domain:** MCP extension auto-generation for AI agent platform
**Researched:** 2026-04-01
**Confidence:** HIGH (based on codebase analysis of existing patterns + MCP protocol docs)

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                       User Interface Layer                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │  Web Chat     │  │  Telegram     │  │  Voice        │              │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         └──────────────────┼──────────────────┘                      │
│                            v                                         │
├──────────────────────────────────────────────────────────────────────┤
│                       Gateway (gateway.py)                           │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ Credential        │  │ Extension         │  │ Config Writer      │  │
│  │ Detector          │  │ Generator         │  │ (config.yaml)      │  │
│  │ (NEW)             │  │ (NEW)             │  │ (EXISTING)         │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬───────────┘  │
│           │  detect              │  generate           │  register    │
│           v                      v                      v             │
├──────────────────────────────────────────────────────────────────────┤
│                       Data / Storage Layer                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ Vault         │  │ Templates    │  │ Generated    │               │
│  │ vault.yaml    │  │ /app/docker/ │  │ Extensions   │               │
│  │ /data/secrets │  │ templates/   │  │ /data/       │               │
│  │ (EXISTING)    │  │ (NEW)        │  │ extensions/  │               │
│  └──────────────┘  └──────────────┘  │ (NEW)        │               │
│                                       └──────────────┘               │
├──────────────────────────────────────────────────────────────────────┤
│                       Runtime Layer                                  │
│  ┌──────────────────────────────────────────────────────────────────┐│
│  │                    goosed (tool runtime)                          ││
│  │  extensions:                                                      ││
│  │    developer (builtin)                                            ││
│  │    context7 (stdio/npx)                                           ││
│  │    knowledge (stdio/python3)     <-- existing pattern             ││
│  │    mem0-memory (stdio/python3)   <-- existing pattern             ││
│  │    email (stdio/python3)         <-- AUTO-GENERATED               ││
│  │    calendar (stdio/python3)      <-- AUTO-GENERATED               ││
│  └──────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **Credential Detector** | Recognizes API keys, app passwords, OAuth tokens in chat messages. Classifies credential type (IMAP, CalDAV, REST API, OAuth). Stores to vault via `secret` CLI. | Gateway relay, Vault, Extension Generator |
| **Extension Generator** | Selects correct template for credential type. Renders template with vault references (not raw creds). Writes generated MCP server .py file to /data/extensions/. | Templates, Vault (read-only references), Config Writer |
| **Template Registry** | Stores template files for each service type. Each template is a complete FastMCP server skeleton with placeholder credential reads. | Extension Generator (consumed by) |
| **Config Writer** | Adds generated extension entry to config.yaml. Handles the preserve/restore cycle that entrypoint.sh already uses. | goosed config.yaml, Extension Generator |
| **Boot Loader** | On container restart, re-registers all /data/extensions/*.py into config.yaml. Validates extensions still have valid vault credentials. | entrypoint.sh, config.yaml, Vault |
| **Vault** (existing) | Stores credentials at /data/secrets/vault.yaml. Read via `secret get service.key`. | All components that need credentials |

## Recommended Project Structure

```
docker/
├── extensions/                    # NEW: auto-generation system
│   ├── __init__.py
│   ├── detector.py                # Credential detection + classification
│   ├── generator.py               # Template rendering + file writing
│   ├── registry.py                # Extension registry (what's generated, status)
│   └── templates/                 # Service-specific MCP server templates
│       ├── base.py.tmpl           # Shared FastMCP boilerplate
│       ├── email_imap.py.tmpl     # IMAP/SMTP email extension
│       ├── calendar_caldav.py.tmpl # CalDAV calendar extension
│       ├── rest_api.py.tmpl       # Generic REST API extension
│       └── oauth_service.py.tmpl  # OAuth-based service extension
/data/
├── extensions/                    # Generated extensions (persists across deploys)
│   ├── registry.json              # Metadata: what was generated, when, from what creds
│   └── email_fastmail/            # One dir per generated extension
│       └── server.py              # The generated FastMCP MCP server
├── secrets/
│   └── vault.yaml                 # Credentials (existing)
└── config/
    └── config.yaml                # goosed config with extension entries (existing)
```

### Structure Rationale

- **docker/extensions/templates/**: Ships with the container image. Templates are versioned with the codebase, not user data. Uses `.py.tmpl` suffix to distinguish from executable Python.
- **/data/extensions/**: On the persistent volume. Generated servers survive redeploys. Each extension gets its own subdirectory for isolation (future: requirements.txt per extension).
- **/data/extensions/registry.json**: Tracks what was generated, from which vault keys, and when. Enables re-generation on template updates and orphan cleanup.
- **detector.py, generator.py, registry.py**: Separate files in docker/extensions/ rather than inline in gateway.py (which is already 12,000+ lines). Gateway imports and calls these modules.

## Architectural Patterns

### Pattern 1: Template-Based Code Generation (not AI-generated)

**What:** Pre-authored Python FastMCP server templates with placeholder variables. The generator does string substitution, not LLM code generation.
**When to use:** Always. Every generated extension uses a template.
**Trade-offs:** Limited to supported service types (templates must exist), but deterministic, fast, and auditable. AI selects which template, not what code to write.

**Why not LLM-generated code:** LLM-generated MCP servers would be non-deterministic, hard to debug, potential security risk (arbitrary code from model output), and slow. Templates are the right call here. The LLM's role is limited to: (1) detecting credential type from user message, (2) selecting the correct template name.

**Example template (email_imap.py.tmpl):**
```python
"""Auto-generated MCP extension: ${service_name} Email
Generated: ${generated_at}
Vault keys: ${vault_keys}
"""
import os
import sys
import subprocess
import imaplib
import smtplib
import email
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("${extension_name}")

def _vault_get(key):
    """Read credential from vault at runtime. Never hardcoded."""
    result = subprocess.run(
        ["secret", "get", key],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Vault key not found: {key}")
    return result.stdout.strip()

@mcp.tool()
def email_search(query: str, folder: str = "INBOX", limit: int = 10) -> str:
    """Search emails by subject, sender, or content.

    Args:
        query: Search query (subject, from address, or keyword)
        folder: IMAP folder to search (default: INBOX)
        limit: Maximum results to return
    """
    host = _vault_get("${vault_prefix}.imap_host")
    user = _vault_get("${vault_prefix}.username")
    password = _vault_get("${vault_prefix}.app_password")
    # ... template continues with actual IMAP logic
```

### Pattern 2: Runtime Vault Resolution (not build-time injection)

**What:** Generated extensions read credentials from vault at runtime via `secret get`, never baking secrets into the generated .py file.
**When to use:** Always. This is a security requirement.
**Trade-offs:** Slight overhead per tool call (subprocess to read vault), but credentials are never on disk in extension code. If vault key is rotated, extension picks it up on next call without regeneration.

**Implementation detail:** Use `subprocess.run(["secret", "get", "service.key"])` inside the generated server. This matches how the existing vault works (shell script with python inline). Alternative: read vault.yaml directly with PyYAML. The subprocess approach is safer (uses the existing secret CLI's permission model) but either works.

### Pattern 3: Extension-per-Service Isolation

**What:** Each generated integration gets its own MCP server process. Email is one server, calendar is another.
**When to use:** Always. Matches how goosed already manages extensions (knowledge, mem0-memory are separate processes).
**Trade-offs:** More processes, but isolation means one crashing extension doesn't take down others. goosed already manages N extension subprocesses, so this is free.

### Pattern 4: Registry-Driven Boot (entrypoint.sh integration)

**What:** A registry.json file at /data/extensions/registry.json tracks all generated extensions. On boot, entrypoint.sh reads this registry and injects entries into config.yaml before goosed starts.
**When to use:** Container restart, deploy, extension generation.
**Trade-offs:** Adds a new file to manage, but solves the core problem: config.yaml is regenerated on every boot (existing behavior), so generated extensions would be lost without a registry.

**Boot sequence (existing + new):**
```
entrypoint.sh
  1. Preserve extensions from old config.yaml    (EXISTING)
  2. Generate base config.yaml                    (EXISTING)
  3. Restore preserved extensions                 (EXISTING)
  4. Sync template extensions (context7, mem0)    (EXISTING)
  5. Load /data/extensions/registry.json          (NEW)
  6. For each registered extension:               (NEW)
     a. Verify /data/extensions/{name}/server.py exists
     b. Verify vault keys still present
     c. Add stdio extension entry to config.yaml
  7. Start goosed                                 (EXISTING)
```

## Data Flow

### Flow 1: Credential Detection and Extension Generation

```
User drops credential in chat
    |
    v
Gateway receives message via _relay_to_goosed()
    |
    v
Credential Detector scans message text
    |  (regex patterns for API keys, app passwords, IMAP configs)
    |  (LLM classification for ambiguous cases)
    |
    ├── NOT a credential -> pass through to goosed normally
    |
    v
Credential classified (type: imap, caldav, rest_api, oauth)
    |
    v
Vault stores credential via `secret set service.key value`
    |
    v
Extension Generator invoked
    |
    ├── 1. Select template based on credential type
    ├── 2. Render template with vault key references
    ├── 3. Write server.py to /data/extensions/{service_name}/
    ├── 4. Update /data/extensions/registry.json
    └── 5. Add extension entry to config.yaml
    |
    v
goosed restart (or hot-reload if supported)
    |
    v
Extension available for tool calls
    |
    v
User confirmation message sent back
```

### Flow 2: Tool Call Execution (post-generation)

```
User: "check my email"
    |
    v
goosed routes to email extension (MCP stdio)
    |
    v
email/server.py starts (or is already running)
    |
    v
email_search() tool called
    |
    ├── _vault_get("fastmail.imap_host")  -> subprocess: secret get fastmail.imap_host
    ├── _vault_get("fastmail.username")    -> subprocess: secret get fastmail.username
    └── _vault_get("fastmail.app_password") -> subprocess: secret get fastmail.app_password
    |
    v
IMAP connection with runtime credentials
    |
    v
Results returned via MCP protocol -> goosed -> gateway -> user
```

### Flow 3: Boot-time Extension Restoration

```
Container starts (deploy/restart)
    |
    v
entrypoint.sh runs
    |
    v
Existing config.yaml extensions preserved (EXISTING LOGIC)
    |
    v
Base config.yaml regenerated (EXISTING LOGIC)
    |
    v
Preserved extensions restored (EXISTING LOGIC)
    |
    v
/data/extensions/registry.json loaded (NEW LOGIC)
    |
    ├── For each entry:
    │   ├── Check server.py exists on disk
    │   ├── Check vault keys exist (secret get)
    │   └── Add to config.yaml extensions section:
    │       name: {display_name}
    │       type: stdio
    │       cmd: python3
    │       args: [/data/extensions/{name}/server.py]
    │       envs: {}
    │       timeout: 300
    |
    v
goosed starts with all extensions registered
```

## Component Boundary Details

### Credential Detector (`docker/extensions/detector.py`)

**Input:** Raw user message text (string)
**Output:** `DetectedCredential` or `None`

```python
@dataclass
class DetectedCredential:
    service_type: str       # "imap", "caldav", "rest_api", "oauth"
    service_name: str       # "fastmail", "google", "github", user-provided
    credentials: dict       # {"imap_host": "...", "username": "...", "app_password": "..."}
    vault_prefix: str       # "fastmail" -> stored as fastmail.imap_host, etc.
    template_name: str      # "email_imap" -> maps to email_imap.py.tmpl
```

**Detection strategy:** Two-phase.
1. **Regex pre-filter** (fast, no LLM cost): patterns for common credential formats. IMAP configs have host:port patterns. API keys match `sk-`, `xoxb-`, etc. App passwords are typically 16-char alphanumeric.
2. **LLM classification** (only if regex matches): Ask goosed to classify the credential type and extract structured fields. This is a single relay call, not code generation.

**Boundary rule:** The detector NEVER stores credentials. It returns a structured object. The caller (gateway) handles vault storage and triggers generation.

### Extension Generator (`docker/extensions/generator.py`)

**Input:** `DetectedCredential` + template name
**Output:** Generated server.py file on disk + registry entry

```python
def generate_extension(cred: DetectedCredential) -> GeneratedExtension:
    """Generate a FastMCP server from template + credential metadata."""
    template_path = f"/app/docker/extensions/templates/{cred.template_name}.py.tmpl"
    output_dir = f"/data/extensions/{cred.vault_prefix}"
    # ... render template, write file, update registry
```

**Template engine:** Use Python's `string.Template` (stdlib). Jinja2 is overkill for variable substitution in Python source files, and adding a dependency for `${variable}` replacement is wasteful. The templates are Python files with `${placeholders}`, not HTML. `string.Template.safe_substitute()` handles this cleanly.

**Boundary rule:** The generator NEVER reads raw credentials. It receives vault key paths (e.g., "fastmail.imap_host") and embeds those paths in the generated code. The generated server reads credentials at runtime.

### Extension Registry (`docker/extensions/registry.py`)

**Input/Output:** JSON file at `/data/extensions/registry.json`

```python
# registry.json schema
{
    "extensions": {
        "email_fastmail": {
            "template": "email_imap",
            "vault_prefix": "fastmail",
            "vault_keys": ["fastmail.imap_host", "fastmail.username", "fastmail.app_password"],
            "server_path": "/data/extensions/email_fastmail/server.py",
            "generated_at": "2026-04-01T12:00:00Z",
            "template_version": "1.0",
            "enabled": true
        }
    },
    "version": 1
}
```

**Operations:**
- `register(name, metadata)` - add/update extension entry
- `unregister(name)` - remove extension, optionally delete files
- `list_extensions()` - return all registered extensions
- `get_config_entries()` - return goosed config.yaml extension dicts for all registered extensions
- `validate()` - check all extensions have valid server.py + vault keys

**Boundary rule:** The registry is the single source of truth for "what auto-generated extensions exist." entrypoint.sh reads it on boot. gateway.py reads it for status/management APIs.

### Config Writer (integrated into existing gateway.py patterns)

No new component needed. The existing patterns in `apply_config()` and `entrypoint.sh` handle config.yaml writes. The extension generator calls the same pattern:

```python
# In gateway.py, after generating an extension:
def _register_generated_extension(name, server_path, description):
    """Add a generated extension to config.yaml (same pattern as existing code)."""
    import yaml
    config_path = os.path.join(CONFIG_DIR, "config.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    exts = config.setdefault("extensions", {})
    exts[name] = {
        "enabled": True,
        "type": "stdio",
        "name": name,
        "description": description,
        "cmd": "python3",
        "args": [server_path],
        "envs": {},
        "env_keys": [],
        "timeout": 300,
        "bundled": None,
        "available_tools": [],
    }
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: LLM-Generated Extension Code

**What people do:** Use the LLM to write the entire MCP server from scratch based on the credential type.
**Why it's wrong:** Non-deterministic output. Security risk (model could generate code that exfiltrates credentials). Slow (requires full LLM generation). Impossible to debug template issues. Can't version-control the output pattern.
**Do this instead:** Pre-authored templates with variable substitution. The LLM only classifies credentials and selects a template name.

### Anti-Pattern 2: Hardcoding Credentials in Generated Code

**What people do:** Inject the actual API key/password into the generated .py file.
**Why it's wrong:** Credentials on disk in plaintext. If the generated file is accidentally exposed (logs, error messages, backups), secrets leak. Can't rotate credentials without regenerating.
**Do this instead:** Generated code calls `secret get vault.key` at runtime. Only vault key paths appear in generated code.

### Anti-Pattern 3: Monolithic Extension Server

**What people do:** Generate one big MCP server that handles all integrations (email + calendar + APIs in one process).
**Why it's wrong:** One integration crashing kills all others. Harder to add/remove individual integrations. Doesn't match goosed's extension model.
**Do this instead:** One MCP server per integration. Matches existing pattern (knowledge and mem0-memory are separate servers).

### Anti-Pattern 4: Bypassing the Existing Extension Preserve/Restore Cycle

**What people do:** Write extensions directly to config.yaml and hope they survive reboots.
**Why it's wrong:** entrypoint.sh regenerates config.yaml on every boot. Extensions added after first boot are preserved via the EXTENSIONS_STATE_FILE mechanism, but this is fragile and depends on config.yaml existing at shutdown time. If the container crashes, the preserve step in entrypoint.sh runs on next boot from the previous config, which should work, but a dedicated registry.json is more reliable.
**Do this instead:** Use registry.json as the authoritative source. entrypoint.sh reads registry.json and injects extensions on every boot, independent of the preserve/restore cycle.

### Anti-Pattern 5: Requiring goosed Restart for Every New Extension

**What people do:** Restart the entire goosed process to pick up a new extension.
**Why it's wrong:** Kills all active sessions. Users lose conversation context. Takes 10-30 seconds. Bad UX.
**Do this instead:** First version can restart goosed (acceptable for MVP, generation is rare). Later, investigate goosed's config reload behavior: goosed re-reads config.yaml from disk on every API call (discovered in apply_config comment at line 3390-3392 of gateway.py). This means writing the extension to config.yaml and ensuring the .py file is ready may be enough without restart. Needs validation.

## Integration Points

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| Gateway -> Detector | Direct Python import, sync function call | Detector is a pure function, no side effects |
| Gateway -> Generator | Direct Python import, sync function call | Generator writes files, updates registry |
| Generator -> Templates | File read from /app/docker/extensions/templates/ | Templates ship with container image |
| Generator -> Registry | JSON file read/write at /data/extensions/registry.json | File locking needed for concurrent access |
| Generator -> Config | YAML read/write at /data/config/config.yaml | Use existing yaml dump pattern |
| entrypoint.sh -> Registry | JSON file read at boot | Shell reads JSON via inline python (existing pattern) |
| goosed -> Generated Extension | stdio MCP protocol (stdin/stdout) | Standard goosed extension lifecycle |
| Generated Extension -> Vault | subprocess: `secret get key` | Runtime credential resolution |

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| IMAP servers | Generated extension connects via imaplib | Template handles SSL/TLS, STARTTLS |
| SMTP servers | Generated extension connects via smtplib | Template handles authentication |
| CalDAV servers | Generated extension uses caldav Python library | May need pip install at generation time |
| REST APIs | Generated extension uses urllib.request | No extra dependencies needed |
| OAuth providers | Device flow or manual token paste | Browser OAuth is out of scope (per PROJECT.md) |

## Scaling Considerations

| Scale | Architecture Adjustments |
|-------|--------------------------|
| 1-5 extensions | Current design is fine. Each extension is a separate Python process managed by goosed. Memory overhead ~20-50MB per extension process. |
| 5-15 extensions | May want lazy loading. goosed starts all extensions on boot. Consider only starting extensions when first called. Check if goosed supports lazy extension startup. |
| 15+ extensions | Unlikely for single-user system. If reached, consolidate related extensions (e.g., all email accounts into one email server with account parameter). |

### First Bottleneck: Boot Time

With many generated extensions, container boot slows down (each stdio extension is a subprocess that needs to initialize). Mitigation: goosed likely starts extensions lazily on first tool call, not eagerly on boot. Verify this.

### Second Bottleneck: Memory

Each Python MCP server process uses ~20-50MB baseline. 10 extensions = 200-500MB extra. On Railway's typical plans this is fine, but worth monitoring.

## Suggested Build Order

Based on component dependencies, build in this order:

1. **Template Registry + Base Template** - Write the email_imap template first. This is the foundation everything else depends on. Can be tested standalone (run the template output as a script).

2. **Extension Generator** - Takes a template name + vault key references, renders the template, writes to /data/extensions/. Can be tested without gateway integration.

3. **Config Writer integration** - Add the generated extension to config.yaml. Test that goosed picks it up on restart.

4. **Boot Loader (entrypoint.sh)** - Add registry.json reading to entrypoint.sh. Test that generated extensions survive container restarts.

5. **Credential Detector** - The user-facing piece. Regex patterns + LLM classification. Wire into gateway message relay. This is last because it's the most complex and least critical for proving the system works (you can manually trigger generation first).

6. **Additional Templates** - CalDAV, REST API, OAuth. Each is independent and can be added incrementally.

**Rationale for this order:**
- Steps 1-4 form the "can we generate and run an extension?" proof. You can manually create a test extension without any detection logic.
- Step 5 adds the "user drops a credential" UX. Separating this lets you validate the generation pipeline before adding detection complexity.
- Step 6 is pure template authoring, the simplest work once the pipeline exists.

## Key Discovery: goosed Re-reads Config on Every Call

From gateway.py line 3390-3392 comment:
> "goose re-reads config.yaml from disk on every API call. If we strip the extensions: section, the gateway detects 'extensions changed' on every telegram message"

This is critical. It means adding a new extension to config.yaml and having the server.py ready on disk may be enough for goosed to pick it up WITHOUT a restart. The extension would appear on the next tool call. This needs validation but would eliminate the biggest UX friction (restart = lost sessions).

## Sources

- GooseClaw codebase analysis: entrypoint.sh (extension preserve/restore cycle, boot sequence), gateway.py (config management, relay, extension discovery), memory/server.py and knowledge/server.py (existing FastMCP MCP server patterns), scripts/secret.sh (vault implementation)
- [Goose Extension Documentation](https://block.github.io/goose/docs/getting-started/using-extensions/) - config.yaml format for stdio extensions
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) - FastMCP server pattern (already used by knowledge + mem0 servers)
- [FastMCP](https://github.com/jlowin/fastmcp) - Template pattern for MCP servers
- [Goose GitHub](https://github.com/block/goose) - Extension system architecture

---
*Architecture research for: GooseClaw Auto-Generated MCP Extensions*
*Researched: 2026-04-01*

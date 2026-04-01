# Phase 2: Extension Registration and Boot Lifecycle - Research

**Researched:** 2026-04-01
**Domain:** goosed config.yaml management, extension registration, container boot lifecycle
**Confidence:** HIGH

## Summary

Phase 2 turns Phase 1's generated MCP server files into live goosed extensions that persist across container restarts. The work spans three concrete areas: (1) a registry.json manifest at `/data/extensions/registry.json` that tracks all auto-generated extensions, (2) a config.yaml writer that injects registered extensions into goosed's configuration with a goosed restart to activate them, and (3) a boot loader addition to `entrypoint.sh` that reads registry.json on container start and restores extensions before goosed launches.

The codebase already has all the patterns needed. `entrypoint.sh` already preserves and restores extensions across reboots via `EXTENSIONS_STATE_FILE`. `gateway.py` already has `start_goosed()` / `stop_goosed()` for restart, `_restart_goose_and_prewarm()` for background restart, and `apply_config()` that preserves the `extensions:` section during config rewrites. The existing extension format (knowledge, mem0-memory) shows exactly how stdio extensions are declared. A critical discovery: gateway.py line 3390 documents that "goose re-reads config.yaml from disk on every API call," meaning new extensions written to config.yaml MAY become available without restart. This needs empirical validation but could eliminate the biggest UX friction.

The main risks are the config.yaml race condition (multiple concurrent writers with no locking) and goosed restart interrupting active sessions. Both have known mitigations: registry.json as source of truth decoupled from config.yaml, and user confirmation before restart with active relay detection.

**Primary recommendation:** Build registry.json as the authoritative record of generated extensions, write a config.yaml injector function in gateway.py following existing patterns, and add a registry.json reader to entrypoint.sh's boot sequence between the extension restore step and goosed startup.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| REG-01 | Generated extensions registered in goosed config.yaml automatically | Config.yaml writer pattern from `apply_config()` (line 3372). Extension format from existing knowledge/mem0 entries. `_extract_yaml_sections()` for safe preservation. |
| REG-02 | Registry file (/data/extensions/registry.json) tracks all generated extensions | New file. Schema documented below. CRUD operations via registry.py module. JSON on persistent /data volume. |
| REG-03 | Boot loader in entrypoint.sh restores generated extensions from registry on container start | Insert between extension restore (line 541) and extension sync (line 548). Inline python reads registry.json, validates server.py exists, injects into config.yaml. |
| REG-04 | Goosed restart after registration to load new extension | `start_goosed()` (line 8622), `stop_goosed()` (line 8706), `_restart_goosed()` (line 6694). Threaded restart pattern from handle_save (line 10868). |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| PyYAML | 6.0.2 | Read/write config.yaml | Already installed, used throughout gateway.py and entrypoint.sh |
| json (stdlib) | n/a | Read/write registry.json | Stdlib, no dependency. JSON is simpler than YAML for a machine-read manifest |
| threading (stdlib) | n/a | Locking for config.yaml writes, background restart | Already used extensively in gateway.py (goose_lock, daemon threads) |
| os, pathlib (stdlib) | n/a | File operations, path management | Already used in generator.py |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| ast (stdlib) | n/a | Validate generated .py files before registration | Pre-registration syntax check (ast.parse) |
| subprocess (stdlib) | n/a | Call `secret get` to validate vault keys exist | Boot-time validation of registry entries |
| fcntl (stdlib) | n/a | File locking for registry.json writes | Concurrent write protection (Linux only, fine for Docker) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| JSON registry | SQLite | Overkill for <20 entries. JSON is human-readable, debuggable with cat |
| File-level fcntl lock | threading.Lock | fcntl works across processes (entrypoint.sh + gateway.py), threading.Lock is in-process only. Use both: threading.Lock for gateway.py internal, fcntl for cross-process |
| Inline python in entrypoint.sh | Separate boot_loader.py script | Inline python matches existing entrypoint.sh patterns (see lines 138-154, 424-437). A separate script adds file management overhead for ~30 lines of logic |

**Installation:**
```bash
# No new packages needed. Everything is stdlib or already installed.
```

## Architecture Patterns

### Recommended Project Structure
```
docker/
  extensions/
    __init__.py          # existing (empty)
    generator.py         # existing (Phase 1)
    registry.py          # NEW: registry CRUD operations
    templates/           # existing (Phase 1)
      base_helpers.py.tmpl
      email_imap.py.tmpl
      rest_api.py.tmpl

/data/extensions/        # persistent volume
  registry.json          # NEW: manifest of all generated extensions
  {name}/
    server.py            # generated MCP server (Phase 1 output)
```

### Pattern 1: Registry as Source of Truth
**What:** `/data/extensions/registry.json` is the authoritative record of what auto-generated extensions exist. config.yaml is a derived artifact written from registry.json at controlled moments (boot, explicit registration).
**When to use:** Always. Never rely on config.yaml to know what generated extensions exist.
**Why:** config.yaml is rewritten by multiple code paths (`apply_config`, `entrypoint.sh`, extension sync). Using it as the source of truth for generated extensions risks data loss. registry.json is written by exactly one code path (the registration module).

**Schema:**
```json
{
  "version": 1,
  "extensions": {
    "email_fastmail": {
      "template": "email_imap",
      "extension_name": "email_fastmail",
      "vault_prefix": "fastmail",
      "vault_keys": ["fastmail.imap_host", "fastmail.username", "fastmail.app_password"],
      "server_path": "/data/extensions/email_fastmail/server.py",
      "generated_at": "2026-04-01T12:00:00Z",
      "enabled": true
    }
  }
}
```

### Pattern 2: Config.yaml Extension Entry Format
**What:** The exact YAML structure goosed expects for a stdio extension.
**When to use:** When writing to config.yaml for both boot-time injection and runtime registration.
**Source:** Verified from existing knowledge and mem0-memory entries in entrypoint.sh (lines 501-538).

```yaml
extensions:
  email_fastmail:
    enabled: true
    type: stdio
    name: email_fastmail
    description: Email access for Fastmail (IMAP/SMTP)
    cmd: python3
    args:
      - /data/extensions/email_fastmail/server.py
    envs: {}
    env_keys: []
    timeout: 300
    bundled: null
    available_tools: []
```

**Critical fields:**
- `type: stdio` -- all generated extensions use stdio transport
- `cmd: python3` -- the interpreter
- `args: [/data/extensions/{name}/server.py]` -- absolute path to generated file
- `envs: {}` -- environment variables passed to the extension process. Can inject vault secrets here via `_inject_vault_secrets_into_env` pattern
- `timeout: 300` -- 5 minute timeout, matches existing extensions

### Pattern 3: Background Restart with Session Awareness
**What:** Restart goosed in a background thread after writing config, with a brief delay.
**When to use:** After registering a new extension at runtime (not at boot -- boot starts goosed fresh).
**Source:** Verified from handle_save (line 10868) and _restart_goosed (line 6694).

```python
def _restart_after_registration():
    """Restart goosed to pick up new extension config."""
    time.sleep(1)  # brief delay for config.yaml write to flush
    stop_goosed()
    start_goosed()
    # clear stale sessions so next message creates fresh ones
    _session_manager._sessions.clear()

threading.Thread(target=_restart_after_registration, daemon=True).start()
```

### Pattern 4: Boot-time Registry Injection
**What:** Read registry.json in entrypoint.sh and merge into config.yaml before goosed starts.
**When to use:** Every container boot. Insert after line 541 (`rm -f "$EXTENSIONS_STATE_FILE"`) and before line 548 (extension sync).
**Why this location:** At line 541, the existing extension preserve/restore cycle is complete. Generated extensions from registry.json should be injected next, before the template extension sync (which adds mem0-memory etc.). This way:
1. User-customized extensions are restored (existing logic)
2. Auto-generated extensions are added from registry (new logic)
3. Template extensions are synced (existing logic)
4. Gateway state is restored (existing logic)

```bash
# ─── auto-generated extensions (from registry.json) ───────────────────────
REGISTRY_FILE="/data/extensions/registry.json"
if [ -f "$REGISTRY_FILE" ]; then
    echo "[mcp] loading auto-generated extensions from registry..."
    python3 -c "
import yaml, json, os, sys, subprocess
try:
    with open('$REGISTRY_FILE') as f:
        registry = json.load(f)
    with open('$CONFIG_DIR/config.yaml') as f:
        config = yaml.safe_load(f) or {}
    exts = config.setdefault('extensions', {})
    added = []
    skipped = []
    for name, meta in registry.get('extensions', {}).items():
        if not meta.get('enabled', True):
            skipped.append(f'{name} (disabled)')
            continue
        sp = meta.get('server_path', '')
        if not os.path.isfile(sp):
            skipped.append(f'{name} (server.py missing)')
            continue
        # validate vault keys exist
        keys_ok = True
        for vk in meta.get('vault_keys', []):
            r = subprocess.run(['secret', 'get', vk], capture_output=True, text=True)
            if r.returncode != 0:
                keys_ok = False
                skipped.append(f'{name} (vault key {vk} missing)')
                break
        if not keys_ok:
            continue
        exts[name] = {
            'enabled': True,
            'type': 'stdio',
            'name': name,
            'description': meta.get('description', f'Auto-generated {name} extension'),
            'cmd': 'python3',
            'args': [sp],
            'envs': {},
            'env_keys': [],
            'timeout': 300,
            'bundled': None,
            'available_tools': [],
        }
        added.append(name)
    if added:
        config['extensions'] = exts
        with open('$CONFIG_DIR/config.yaml', 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f'[mcp] auto-generated extensions loaded: {', '.join(added)}')
    if skipped:
        print(f'[mcp] auto-generated extensions skipped: {', '.join(skipped)}', file=sys.stderr)
except Exception as e:
    print(f'[mcp] WARN: registry load failed: {e}', file=sys.stderr)
" 2>/dev/null || true
fi
```

### Anti-Patterns to Avoid
- **Writing to config.yaml without preserving existing sections:** Use `_extract_yaml_sections()` or read-modify-write with `yaml.safe_load()` / `yaml.dump()`. Never truncate and rewrite.
- **Restarting goosed synchronously in a request handler:** Always use `threading.Thread(target=..., daemon=True).start()` with a 1-second delay, matching handle_save pattern.
- **Using config.yaml as the source of truth for generated extensions:** Config.yaml gets rewritten by multiple code paths. Registry.json is the single source of truth.
- **Modifying entrypoint.sh boot order:** The existing sequence (preserve -> regenerate -> restore -> sync -> gateway state) is carefully ordered. Insert registry loading AFTER extension restore and BEFORE template sync.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| YAML config management | Custom YAML string builder | `yaml.safe_load()` + `yaml.dump()` | String manipulation breaks on special characters, nested structures, multiline values |
| Atomic file writes | Direct `open().write()` | tmp file + `os.replace()` (see `_save_vault_key` line 8603) | Prevents partial writes on crash/power loss |
| goosed restart orchestration | Custom process management | Existing `stop_goosed()` + `start_goosed()` functions | They handle PID tracking, lock acquisition, health polling, stderr monitoring |
| Extension format | Custom config structure | Copy existing knowledge/mem0 entry format verbatim | goosed expects exact fields; missing `env_keys` or `available_tools` causes parse errors |
| File locking | Manual lockfile | `fcntl.flock()` for cross-process, `threading.Lock()` for in-process | Handles edge cases (dead process leaving stale lock, etc.) |

**Key insight:** Every building block for Phase 2 already exists in the codebase. The task is composition, not invention. Use `stop_goosed()`/`start_goosed()` for restart, `yaml.safe_load()`/`yaml.dump()` for config writes, the exact extension dict format from entrypoint.sh lines 501-538, and the inline-python-in-bash pattern from entrypoint.sh for boot loading.

## Common Pitfalls

### Pitfall 1: config.yaml Race Condition During Registration
**What goes wrong:** Gateway writes an extension to config.yaml while `apply_config()` is simultaneously rewriting it (triggered by setup wizard save or provider change). One write wins, the other is lost. Extensions or pairings disappear.
**Why it happens:** config.yaml is a shared mutable file. Multiple code paths read-modify-write: `apply_config()` (line 3372), `_re_persist_cached_pairings()` (line 1569), extension sync in entrypoint.sh, and now extension registration.
**How to avoid:** 
1. All config.yaml writes in gateway.py go through a single function that acquires `goose_lock` (the existing threading.Lock at line 1012).
2. The registration function reads config.yaml, merges the new extension, and writes atomically (tmp + rename).
3. Registry.json is never at risk because only the registration module writes it.
**Warning signs:** Extensions disappear after setup wizard save. Telegram pairings lost after extension registration.

### Pitfall 2: Goosed Restart Kills Active Sessions
**What goes wrong:** Registering an extension triggers `stop_goosed()` + `start_goosed()`, which terminates ALL active MCP server subprocesses and in-flight LLM sessions. Users on Telegram or voice lose their conversation.
**Why it happens:** goosed doesn't support hot-adding extensions. The only way to pick up new config.yaml entries is restart.
**How to avoid:**
1. Check `_active_relays` on the ChannelState before restarting. If relays are active, defer or warn.
2. Always restart in background thread with 1s delay (handle_save pattern).
3. After restart, clear `_session_manager._sessions` so next message creates fresh sessions.
4. For MVP: just restart with a warning message to the user. Session loss is acceptable if the user explicitly asked for extension generation.
**Warning signs:** Voice sessions drop. Telegram messages get "connection reset" errors. User reports conversation "forgot everything."

### Pitfall 3: Boot-time Injection Order Matters
**What goes wrong:** Registry extensions are injected at the wrong point in entrypoint.sh. Either they're overwritten by the template extension sync, or they overwrite user customizations.
**Why it happens:** entrypoint.sh has a specific order: preserve -> regenerate -> restore -> sync -> gateway state. Inserting at the wrong point breaks the chain.
**How to avoid:** Insert registry loading at exactly line 541 (after `rm -f "$EXTENSIONS_STATE_FILE"`), before the template extension sync at line 548. This preserves user customizations AND adds generated extensions.
**Warning signs:** Generated extensions disappear after reboot. Or user-disabled extensions get re-enabled.

### Pitfall 4: Extension Name Collisions
**What goes wrong:** User generates two email extensions (personal + work). Both get name `email` and the second overwrites the first in config.yaml and registry.json.
**Why it happens:** Name derivation from template_name without considering uniqueness.
**How to avoid:** Use `{template}_{vault_prefix}` as the extension key (e.g., `email_fastmail`, `email_gmail`). Registry.register() should reject duplicates with a clear error.
**Warning signs:** User sets up second email account and first one stops working.

### Pitfall 5: Stale Registry Entries After Manual Deletion
**What goes wrong:** User manually deletes `/data/extensions/foo/server.py` but registry.json still lists it. On next boot, the boot loader tries to register a nonexistent file. If validation is skipped, goosed gets a broken extension entry.
**Why it happens:** Registry.json and the filesystem can get out of sync if files are deleted outside the registration system.
**How to avoid:** Boot loader MUST validate `os.path.isfile(server_path)` before injecting. Skip with a warning if missing. Optionally: auto-clean registry entries for missing files.
**Warning signs:** goosed stderr shows "failed to start extension" for a deleted extension.

## Code Examples

Verified patterns from the existing codebase:

### registry.py - Core Module
```python
# Source: Derived from codebase patterns (atomic writes from _save_vault_key, 
# JSON from setup.json patterns)

import json
import os
import fcntl
from datetime import datetime, timezone

REGISTRY_PATH = "/data/extensions/registry.json"


def _load_registry():
    """Load registry from disk. Returns empty structure if missing."""
    if not os.path.isfile(REGISTRY_PATH):
        return {"version": 1, "extensions": {}}
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def _save_registry(data):
    """Atomic write registry to disk with file locking."""
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    tmp_path = REGISTRY_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, REGISTRY_PATH)


def register(name, template, vault_prefix, vault_keys, server_path, description=""):
    """Add or update an extension in the registry."""
    reg = _load_registry()
    reg["extensions"][name] = {
        "template": template,
        "extension_name": name,
        "vault_prefix": vault_prefix,
        "vault_keys": vault_keys,
        "server_path": server_path,
        "description": description,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": True,
    }
    _save_registry(reg)
    return reg["extensions"][name]


def unregister(name, delete_files=False):
    """Remove an extension from the registry."""
    reg = _load_registry()
    entry = reg["extensions"].pop(name, None)
    if entry and delete_files:
        server_path = entry.get("server_path", "")
        if os.path.isfile(server_path):
            os.remove(server_path)
            parent = os.path.dirname(server_path)
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
    _save_registry(reg)
    return entry


def list_extensions():
    """Return dict of all registered extensions."""
    return _load_registry().get("extensions", {})


def get_config_entries():
    """Return goosed config.yaml extension dicts for all enabled extensions."""
    entries = {}
    for name, meta in list_extensions().items():
        if not meta.get("enabled", True):
            continue
        entries[name] = {
            "enabled": True,
            "type": "stdio",
            "name": name,
            "description": meta.get("description", f"Auto-generated {name}"),
            "cmd": "python3",
            "args": [meta["server_path"]],
            "envs": {},
            "env_keys": [],
            "timeout": 300,
            "bundled": None,
            "available_tools": [],
        }
    return entries
```

### Config.yaml Writer (gateway.py integration)
```python
# Source: Follows apply_config() pattern at gateway.py:3372

def _register_extension_in_config(name, server_path, description=""):
    """Add a generated extension to config.yaml. Thread-safe."""
    import yaml
    config_path = os.path.join(CONFIG_DIR, "config.yaml")
    with goose_lock:  # existing lock at gateway.py:1012
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
        tmp_path = config_path + ".tmp"
        with open(tmp_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, config_path)
```

### Full Registration Flow (gateway.py)
```python
# Source: Combines generator.py + registry.py + config writer + restart

def register_generated_extension(template_name, extension_name, vault_prefix, 
                                  vault_keys, description="", extra_subs=None):
    """End-to-end: generate extension, register, update config, restart goosed."""
    from extensions.generator import generate_extension
    from extensions import registry
    
    # 1. Generate the server.py file (Phase 1)
    server_path = generate_extension(
        template_name=template_name,
        extension_name=extension_name,
        vault_prefix=vault_prefix,
        vault_keys=vault_keys,
        service_description=description,
        extra_subs=extra_subs,
    )
    
    # 2. Register in registry.json
    registry.register(
        name=extension_name,
        template=template_name,
        vault_prefix=vault_prefix,
        vault_keys=vault_keys,
        server_path=server_path,
        description=description,
    )
    
    # 3. Add to config.yaml
    _register_extension_in_config(extension_name, server_path, description)
    
    # 4. Restart goosed in background
    def _restart():
        time.sleep(1)
        stop_goosed()
        start_goosed()
        _session_manager._sessions.clear()
    
    threading.Thread(target=_restart, daemon=True).start()
    
    return server_path
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| config.yaml as source of truth for extensions | registry.json as source of truth, config.yaml as derived | Phase 2 (now) | Eliminates data loss from config.yaml race conditions |
| Restart goosed for every config change | goosed re-reads config.yaml on every API call (discovered in codebase) | Already present | MAY eliminate restart requirement entirely -- needs validation |
| Manual extension configuration | Auto-registration from generated files | Phase 2 (now) | Zero-config extension lifecycle |

**Key discovery -- goosed config re-read behavior:**
From gateway.py line 3390-3392:
> "goose re-reads config.yaml from disk on every API call. If we strip the extensions: section, the gateway detects 'extensions changed' on every telegram message"

This means writing an extension to config.yaml and having server.py ready on disk MAY be sufficient for goosed to pick it up WITHOUT restart. The extension would appear on the next API call. **This needs empirical validation** but would eliminate the worst UX friction (restart = lost sessions).

**Validation approach:** After writing extension to config.yaml, send a tool list request to goosed. If the new extension's tools appear without restart, the restart step can be made optional.

## Open Questions

1. **Does goosed pick up new config.yaml extensions without restart?**
   - What we know: gateway.py documents that goosed re-reads config.yaml on every API call (line 3390). The `extensions:` section is specifically mentioned.
   - What's unclear: Does "re-reads" mean it actually starts new extension subprocesses, or just detects the config changed? The comment about "evicts the agent" suggests it does react to changes.
   - Recommendation: For MVP, always restart. Add a validation step that checks if new tools appear without restart. If they do, make restart optional in a follow-up.

2. **Should we validate vault keys at registration time or only at boot?**
   - What we know: Boot loader should validate (skip broken extensions). Runtime registration has access to vault.
   - What's unclear: If vault key validation fails at registration, should we fail the entire registration or register as disabled?
   - Recommendation: Validate at both times. At registration: warn but still register (user may add vault keys later). At boot: skip extensions with missing vault keys (log warning).

3. **How to handle extension re-generation (template updates)?**
   - What we know: registry.json stores `template` name and `generated_at` timestamp. Templates ship with the container image and can change across deploys.
   - What's unclear: Should boot check if template version changed and auto-regenerate?
   - Recommendation: Defer to Phase 4 (management). For now, re-generation is manual via the same generation flow (overwrites existing server.py, updates registry timestamp).

## Sources

### Primary (HIGH confidence)
- **entrypoint.sh** (969 lines) -- Full boot sequence analyzed. Extension preserve/restore at lines 136-541. Template sync at 548-640. Gateway state restore at 642-661. Boot order verified.
- **gateway.py** -- `apply_config()` at line 3372 (config.yaml write pattern with section preservation). `_extract_yaml_sections()` at line 3284. `start_goosed()` at line 8622 (startup with health polling). `stop_goosed()` at line 8706. `_restart_goosed()` at line 6694. `_restart_goose_and_prewarm()` at line 6601. `_save_vault_key()` at line 8603 (atomic write pattern). `_inject_vault_secrets_into_env()` at line 8501. `goose_lock` at line 1012. handle_save restart at line 10868.
- **docker/extensions/generator.py** (Phase 1 output) -- 112 lines. Uses string.Template, writes to `/data/extensions/{name}/server.py`. Already handles path creation and file permissions.
- **docker/extensions/templates/** -- base_helpers.py.tmpl (stdout->stderr redirect, vault_get, FastMCP init), email_imap.py.tmpl, rest_api.py.tmpl.
- **Config.yaml extension format** -- Verified from entrypoint.sh default extensions block (lines 440-539). Exact field set: enabled, type, name, description, cmd, args, envs, env_keys, timeout, bundled, available_tools.

### Secondary (MEDIUM confidence)
- **goosed config re-read behavior** -- Documented in gateway.py comment at line 3390-3392 but needs empirical validation. The comment confirms config.yaml is re-read but the exact behavior for new extensions is unverified.
- [Goose Extension Documentation](https://block.github.io/goose/docs/getting-started/using-extensions/) -- stdio config format confirmed.

### Tertiary (LOW confidence)
- Extension count scaling behavior (no real-world data beyond memory estimates)
- Whether goosed starts extension subprocesses lazily or eagerly (assumed lazy based on comment, unverified)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - everything is stdlib or already installed. No new dependencies.
- Architecture: HIGH - all patterns derived from existing codebase. No novel architecture.
- Pitfalls: HIGH - race condition and restart issues documented in codebase comments. Boot order verified by reading full entrypoint.sh.

**Research date:** 2026-04-01
**Valid until:** 2026-05-01 (stable domain, unlikely to change)

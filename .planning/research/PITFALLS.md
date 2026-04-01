# Pitfalls Research

**Domain:** Auto-generated MCP extensions from user credentials (GooseClaw)
**Researched:** 2026-04-01
**Confidence:** HIGH (grounded in codebase analysis + MCP security research)

## Critical Pitfalls

### Pitfall 1: Credentials Leaked Into Generated Extension Source Code

**What goes wrong:**
The template system interpolates vault credentials directly into the generated Python MCP server source file. The credential value ends up as a string literal in `/data/extensions/email_server.py`. Anyone with filesystem access (or a `cat` command from the AI agent itself via the developer extension) can read plaintext secrets. Worse: credentials appear in stack traces, log files, and error messages when the extension crashes.

**Why it happens:**
It's the simplest template approach. `API_KEY = "sk-abc123"` is easier to template than vault-read-at-runtime. Developers think "it's on disk anyway in vault.yaml, what's the difference?" The difference is vault.yaml has 600 permissions and is a known secret store. A Python file at `/data/extensions/` is not treated as sensitive by anyone scanning the filesystem.

**How to avoid:**
Generated extensions must NEVER contain credential values. Two viable patterns exist in the codebase already:
1. **Environment variable pattern** (matches `_inject_vault_secrets_into_env()` in gateway.py line 8501): The extension's config.yaml `envs:` block references env vars that get populated from the vault at goosed startup.
2. **Vault CLI pattern**: The extension calls `secret get imap.password` at runtime via subprocess, matching how the existing `secret.sh` CLI works.

Pattern 1 is preferred because it matches existing extensions (knowledge, mem0-memory) and avoids subprocess overhead.

Additionally: run a post-generation lint that `grep`s the output file for strings matching credential patterns (`sk-`, `ghp_`, `Bearer `, any string >20 chars that looks like base64). Zero matches = pass.

**Warning signs:**
- `grep -rE "sk-|ghp_|Bearer |password.*=.*['\"]" /data/extensions/*.py` returns hits
- Generated .py files contain string literals longer than 20 characters that aren't URLs or descriptions
- Extension code contains `API_KEY = "..."` patterns with actual values

**Phase to address:**
Phase 1 (Template System Design). This is a foundational architectural decision. Getting it wrong means rewriting every template later.

---

### Pitfall 2: config.yaml Race Condition on Extension Registration

**What goes wrong:**
Adding a new extension requires writing to `/data/config/config.yaml` and restarting goosed. The gateway already has a **documented race condition problem** with config.yaml writes. From gateway.py lines 1548-1573:

> "Pairings are stored on disk in config.yaml under gateway_pairings:, but goose sessions or apply_config() calls can race and rewrite the file, temporarily wiping pairings. This cache survives disk rewrites so a freshly paired user never gets 'You are not paired' due to a race condition."

The `_pairing_cache` and `_re_persist_cached_pairings()` exist as workarounds for this exact problem. Adding auto-extension registration introduces another concurrent writer. Extensions, pairings, gateway configs, and provider settings can all be lost during concurrent writes.

**Why it happens:**
config.yaml is a shared mutable file with no file-level locking. Multiple code paths read-modify-write it: `apply_config()` (line 3372), `_re_persist_cached_pairings()` (line 1569), extension sync in entrypoint.sh, and the proposed auto-extension registration. The `_extract_yaml_sections()` helper (line 3284) tries to preserve sections during rewrites but is fragile (regex-based YAML parsing).

**How to avoid:**
1. **Separate registry file.** Store auto-generated extension configs in `/data/extensions/registry.json`. Only merge into config.yaml during controlled events: boot (entrypoint.sh) or explicit goosed restart.
2. **Single writer pattern.** All config.yaml writes go through one function with a `threading.Lock`. No direct file writes from multiple code paths.
3. **Atomic write.** Use the existing tmp+rename pattern (already used in `_save_vault_key()` at line 8615). But atomic writes don't solve the read-modify-write gap. The lock is still needed.

**Warning signs:**
- Telegram pairings disappear after extension generation
- config.yaml missing the `extensions:` section entirely after a restart
- goosed fails to start with "invalid config" after concurrent operations
- Intermittent "extension not found" for previously working extensions

**Phase to address:**
Phase 2 (Extension Registration). Must be solved before any dynamic registration goes live.

---

### Pitfall 3: Goosed Restart Interrupts Active Sessions

**What goes wrong:**
Registering a new extension requires restarting goosed (it reads config.yaml at startup and launches stdio extension processes). The `start_goosed()` function in gateway.py (line 8622) explicitly terminates the old process:

```python
if goosed_process and goosed_process.poll() is None:
    goosed_process.terminate()
    try:
        goosed_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        goosed_process.kill()
```

This kills ALL active MCP server processes (knowledge, mem0-memory, Context7, Exa) and terminates any in-flight LLM session. If the user is mid-conversation via Telegram or voice, the conversation dies. If a scheduled job is executing, it fails silently.

**Why it happens:**
goosed doesn't support hot-adding extensions. The existing `save` endpoint handler (line 10865) already does async restart: `threading.Thread(target=_restart).start()` with a 1-second delay. But there's no session-awareness. It restarts regardless of active work.

**How to avoid:**
1. **Defer registration to idle periods.** Queue the extension config write. Apply when no sessions are active (check `_active_relays` in ChannelState, check `goosed_startup_state`).
2. **Batch registrations.** If the user provides multiple credentials in one conversation, generate all extensions first, do a single restart at the end.
3. **Notify the user.** "I've generated your email extension. I need to restart to activate it. Want me to do that now, or after we're done chatting?"
4. **Graceful drain.** Wait for the current relay to complete before restarting. Set a flag that prevents new relays from starting, finish the active one, then restart.

**Warning signs:**
- User complains conversation "reset" or "forgot everything" after adding a credential
- Scheduled jobs fail with connection errors right after extension generation
- Voice sessions drop immediately after credential submission
- goosed startup state flaps between "ready" and "starting" rapidly

**Phase to address:**
Phase 2 (Extension Registration). The restart strategy must be designed before the first extension gets registered.

---

### Pitfall 4: AI Selects Wrong Template or Generates Malformed MCP Server

**What goes wrong:**
The AI picks IMAP/SMTP for a REST API key, or CalDAV for an IMAP password. The generated extension either crashes on startup (wrong protocol), silently does nothing (connects to wrong endpoint), or exposes tools that don't match the user's intent. If the generated Python is syntactically invalid, the MCP server process crashes, goosed may retry in a loop, burning CPU.

**Why it happens:**
Credential types are ambiguous. An "app password" could be for IMAP, CalDAV, or a proprietary API. A "token" could be OAuth, API key, or bearer token. Template selection is a classification problem that LLMs get wrong ~15-20% of the time without guardrails, because the credential itself carries no type metadata.

**How to avoid:**
1. **Always confirm with the user.** "This looks like an email credential. Should I set up email access (IMAP/SMTP)?" Never silently select a template.
2. **Template manifest with required fields.** Each template declares what inputs it needs (e.g., IMAP requires `host`, `port`, `username`, `password`). If the credential doesn't match, ask for missing info rather than guessing.
3. **Validate generated code.** Run `ast.parse()` on the generated Python. Then run the MCP server in a subprocess with a 5-second timeout and verify it responds to `initialize` + `tools/list`.
4. **Post-registration health check.** After goosed restarts, query its `/config` endpoint and verify the new extension's tools appear in the response.

**Warning signs:**
- Extension registers but no new tools appear in goosed `/config`
- goosed stderr shows repeated "failed to start extension" errors
- CPU spikes after extension registration (crash-restart loop from goosed retrying)
- User says "I gave you my email password but you set up a calendar"

**Phase to address:**
Phase 1 (Template System) for manifest + matching rules. Phase 3 (Validation) for health checks.

---

### Pitfall 5: Stdout Corruption in Generated Extensions

**What goes wrong:**
Generated extension writes to stdout via `print()` statements, library logging defaults, or imported packages that write to stdout. This corrupts the MCP JSON-RPC protocol over stdio. The extension appears to register but ALL tool calls fail with parse errors or timeouts.

**Why it happens:**
Default Python behavior is `print()` to stdout. Many libraries log to stdout unless explicitly configured. The existing codebase handles this (both `knowledge/server.py` and `memory/server.py` use `logging.basicConfig(stream=sys.stderr)`), but a template author or AI generator can easily forget.

**How to avoid:**
Every generated template MUST include this boilerplate at the top:
```python
import sys
import logging
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
```
Never use `print()` in generated code. If importing third-party libraries, redirect their loggers to stderr. Include a pre-registration test that captures the process's stdout and verifies it only contains valid JSON-RPC messages.

**Warning signs:**
- Tool calls return garbled JSON or timeout
- Extension process produces output but tools never work
- `goosed` stderr shows JSON parse errors from the extension's stdio transport

**Phase to address:**
Phase 1 (Template System). This is a template boilerplate requirement, not a per-extension concern.

---

### Pitfall 6: Credential Auto-Detection False Positives in Chat

**What goes wrong:**
The system tries to auto-detect when a user "drops a credential in chat." It interprets a long string, a code snippet, a URL with a query parameter, or a base64-encoded value as a credential and vaults it. The user didn't intend to store a credential. Now there's garbage in the vault and possibly a generated extension for a non-existent service.

Alternatively: a real credential is shared but the heuristic misses it (false negative), and the user expects the system to have stored it.

**Why it happens:**
Credential detection is inherently fuzzy. API keys look like random strings. OAuth tokens look like JWTs. App passwords look like short random strings. All of these also look like: commit hashes, UUIDs, encoded data, URL parameters, and code variables.

**How to avoid:**
1. **Never auto-vault without confirmation.** Detect candidate credentials, but always ask: "That looks like it might be an API key. Want me to store it securely?"
2. **Prefer structured input.** Point users to `secret set service.key "value"` or the setup wizard. Don't rely on free-text detection for primary flow.
3. **Use high-specificity patterns.** Only auto-detect strings matching known formats: `sk-[a-zA-Z0-9]{48}` (OpenAI), `ghp_[a-zA-Z0-9]{36}` (GitHub), `AIza[a-zA-Z0-9_-]{35}` (Google). Unknown formats require explicit user intent.
4. **Provide undo.** If the system vaults something incorrectly, the user says "that wasn't a credential, delete it" and it's cleaned up.

**Warning signs:**
- Vault fills with entries the user doesn't recognize
- Extensions get generated for non-existent services
- User pastes code and the system tries to vault variable names or constants

**Phase to address:**
Phase 1 (Credential Detection). Must be designed conservatively from the start. Overly aggressive detection destroys trust.

---

### Pitfall 7: Template Injection via User-Provided Values

**What goes wrong:**
User-provided service name, hostname, or credential value contains Jinja2 syntax (`{{ }}`, `{% %}`), Python code, or YAML special characters that get executed or misinterpreted during template rendering or config generation.

**Why it happens:**
String interpolation without sanitization. If the template uses `f"API_KEY = '{user_value}'"` and the user's password contains a single quote, the generated Python is syntactically broken. If using Jinja2 and the service name contains `{{ }}`, it gets evaluated.

**How to avoid:**
1. **Use `jinja2.sandbox.SandboxedEnvironment`** instead of regular `Environment`.
2. **Validate all user inputs** against strict patterns: service names are `[a-z0-9-]` only, hostnames are valid DNS, ports are integers.
3. **Never interpolate credentials into source code** (see Pitfall 1). This eliminates the credential-value injection vector entirely.
4. **For YAML config generation**, use `yaml.dump()` not f-strings. `yaml.dump()` properly escapes special characters.
5. **Run `ast.parse()`** on every generated Python file to catch syntax errors from bad interpolation.

**Warning signs:**
- Generated Python fails `ast.parse()` check
- Jinja2 raises `UndefinedError` or `TemplateSyntaxError` during generation
- config.yaml contains un-escaped special YAML characters (`:`, `{`, `}`, `[`, `]`)

**Phase to address:**
Phase 1 (Template System). Input validation is a day-1 requirement.

---

## Technical Debt Patterns

Shortcuts that seem reasonable but create long-term problems.

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Hardcode service logic in templates instead of configurable parameters | Faster initial development | Every new service requires a new template file. Can't customize without editing templates | MVP only. Refactor to data-driven templates when >5 templates exist |
| Store generated extensions as plain .py files without versioning | Simple, no extra infrastructure | Can't roll back if a template update breaks a working extension | Always acceptable for single-user system |
| Restart goosed synchronously in the credential-handling flow | Simpler control flow | Blocks the user's chat for 5-30 seconds during restart. Health checks fail. Railway may kill container | Never. Always async restart with status feedback |
| Skip connection validation on extension generation | Extension appears "ready" faster | User doesn't discover the credential is wrong until they try to use it. Gets a cryptic MCP error | MVP only. Add validation in Phase 3 |
| Use `subprocess.run(["secret", "get", key])` in generated extensions | Simple, reuses existing CLI | Spawns a new process per secret read. Adds 50-100ms latency per secret | Acceptable if secrets are cached on first read. Never call per-request |
| Single-account-per-service templates | Simpler template logic | User can't have work + personal email. Would need to duplicate the template or create naming hacks | Acceptable for MVP. Add multi-account support when users request it |

## Integration Gotchas

Common mistakes when connecting to external services.

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| IMAP/SMTP email | Using port 143/25 (unencrypted) or not handling STARTTLS vs implicit TLS | Default to 993 (IMAPS) and 465 (SMTPS) with TLS. Only fall back to STARTTLS on 587 if explicitly configured |
| IMAP connections | Opening connection at module import time. Connection times out after 30 min idle | Open per-tool-call with try/reconnect. MCP servers are long-running; IMAP connections are not |
| CalDAV | Hardcoding the CalDAV URL path when providers use different paths (Google vs iCloud vs Fastmail) | Use `.well-known/caldav` discovery (RFC 6764) first. Fall back to user-configured URL. Store full CalDAV URL, not just hostname |
| OAuth token paste | Accepting a refresh token but treating it as an access token (or vice versa) | Template must distinguish token types. If refresh token, generate code that exchanges it. If access token, warn about expiry |
| REST API generic | Assuming all APIs use Bearer token auth | Support multiple auth methods: `Authorization: Bearer`, `X-API-Key` header, query parameter, Basic auth. Let user/AI specify |
| Google services | Using an API key where OAuth is required (Gmail, Calendar, Drive need OAuth) | Document clearly which Google services need OAuth vs API key. Reject API key for services that require OAuth |
| IMAP mailbox search | Searching entire mailbox without constraints. User has 50k emails, tool times out | Default to last 30 days, max 50 results. Require at least one constraint (folder, date, sender) |

## Performance Traps

Patterns that work at small scale but fail as usage grows.

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| One Python process per MCP extension on boot | Boot time increases linearly. Railway container has ~512MB-1GB RAM | Monitor boot time. Cap at 10-15 extensions. Share Python interpreters if possible | >10 extensions: boot >30s, OOM kills from memory pressure |
| Reading vault.yaml on every tool call (no caching) | 50-100ms latency per vault read on every tool invocation | Cache credentials in-process on first read. Invalidate on vault write | Noticeable from first use. Not a scale issue but a UX issue |
| Synchronous goosed restart blocking gateway | Gateway unresponsive during restart. Health checks fail. Railway restarts container | Always async restart: `threading.Thread(target=_restart)` with startup state API for feedback | Any time. Already solved in `save` handler but easy to forget in new code paths |
| No max-extension limit | User adds 20+ integrations. Container runs out of memory or file descriptors | Set a configurable max (default 15). Error with clear message when exceeded | >15 extensions on a 512MB container |

## Security Mistakes

Domain-specific security issues beyond general web security.

| Mistake | Risk | Prevention |
|---------|------|------------|
| Generated code uses `os.system()`, `subprocess.run(shell=True)`, or `eval()` | Prompt injection in email content or API response could trigger arbitrary command execution | Templates must NEVER use shell=True, eval, exec, or os.system. Use parameterized API calls only. Lint generated code for dangerous patterns before registration |
| AI agent can `cat /data/secrets/vault.yaml` via developer extension | All credentials readable by the AI. A prompt injection could exfiltrate them via tool calls | Vault file permissions (600) help but goosed runs as the same user. Consider: encrypted-at-rest vault with runtime decryption, or dev extension path restrictions |
| Generated extensions inherit full goosed permissions | An email extension can also read/write files, execute commands if developer extension is active | goosed architectural limitation. Mitigate: generate minimal extensions that only import needed libraries. Never import `os` or `subprocess` except for vault reads |
| User credential values contain special characters that break template rendering | Credential becomes executable code via template injection (see Pitfall 7) | Never interpolate credentials into source code. Use `yaml.dump()` for config. Validate credential characters |
| Extension makes unrestricted outbound network calls | A compromised template could exfiltrate credentials to an attacker-controlled server | Generated extensions should connect only to the configured service host. Block arbitrary outbound connections at the template level |
| No audit trail for credential access | Can't detect if/when credentials were exfiltrated | Log every vault read with timestamp and caller. Alert on reads from unexpected processes |

## UX Pitfalls

Common user experience mistakes in this domain.

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Silent extension generation with no feedback | User drops a credential and gets no indication anything happened | Immediate acknowledgment: "Got it. Setting up email access..." then "Done! You now have: read_email, send_email, search_email" |
| Extension generation fails silently | User thinks email is set up. Tries to use it, gets "tool not available" | Surface health. After registration, verify extension is running. If not: "Email setup failed because [error]. Want to try again?" |
| No way to list or manage extensions | User forgets what they set up. Can't disable or delete without SSH | Provide tools: `list_extensions`, `disable_extension`, `remove_extension`. Surface in admin dashboard |
| Credential update doesn't propagate | User changes password, updates vault, but running extension has old password cached | On vault update, restart affected extensions or invalidate caches. Tell user: "Password updated. Restarting email extension..." |
| Goosed restart with no warning | User is mid-conversation, system restarts for extension registration | Always warn: "I need to restart to activate the new extension. This will briefly interrupt our conversation. OK?" |
| Generated tool names are generic or confusing | User has `do_action_1`, `do_action_2` instead of `read_email`, `send_email` | Templates must define clear, specific tool names and descriptions. LLM tool selection depends on good descriptions |

## "Looks Done But Isn't" Checklist

Things that appear complete but are missing critical pieces.

- [ ] **Template generates valid Python:** Often missing error handling for network timeouts, auth failures, and malformed API responses. Verify with `ast.parse()` AND a 5-second subprocess test
- [ ] **Extension registered in config.yaml:** Often missing `envs:` block for vault references, or `timeout:` set too low for slow APIs (IMAP can take 10s+). Verify config entry matches pattern of existing extensions (knowledge, mem0-memory)
- [ ] **Extension survives container reboot:** Often missing from boot-time registration flow. Verify the extension reappears in goosed `/config` after `docker restart` without user intervention
- [ ] **Extension works via voice AND text:** Voice tool calls go through Gemini Flash, which may format parameters differently. Verify tool calls succeed from both Telegram and voice channels
- [ ] **Credential auto-detection handles the credential format:** Often tested with OpenAI `sk-` keys but fails for app passwords, OAuth tokens, generic API keys. Test with at least 5 different credential formats
- [ ] **Generated tool descriptions are LLM-usable:** Generic descriptions cause the LLM to pick the wrong tool or fall through to the slow assistant catch-all. Verify descriptions are specific enough for reliable tool selection
- [ ] **MCP SDK version matches:** Container has `mcp[cli]==1.26.0`. Template code must use API patterns compatible with this version (`FastMCP`, not older patterns). Verify imports work against pinned version
- [ ] **Concurrent extension generation works:** Two credentials provided in same conversation. Both extensions generated, both registered, no config corruption

## Recovery Strategies

When pitfalls occur despite prevention, how to recover.

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Credential leaked in generated code | MEDIUM | 1. Rotate the credential immediately. 2. Delete generated file. 3. Regenerate with vault-read pattern. 4. Audit access logs |
| config.yaml corrupted by race | LOW | 1. Restart container (entrypoint.sh regenerates base config). 2. Extensions restored from state file. 3. Pairings restored from `_pairing_cache`. Already battle-tested |
| Goosed restart kills active session | LOW | 1. User re-sends message. 2. Session context lost but mem0 preserves long-term memory. 3. Extension available next turn |
| Wrong template selected | LOW | 1. Remove the generated extension. 2. Re-run with explicit template. 3. No credential rotation needed (vault-read pattern means no credential in code) |
| Orphaned extensions accumulate | LOW | 1. Boot-time validation: check vault keys exist for each extension. 2. Auto-disable extensions with missing credentials. 3. Log for user review |
| Extension crashes in loop | MEDIUM | 1. goosed may exhaust memory retrying. 2. Auto-disable after 3 consecutive startup failures. 3. Remove from config.yaml. 4. Notify user |
| Template injection produces malicious code | HIGH | 1. Kill the extension immediately. 2. Audit vault for exfiltration. 3. Delete generated file. 4. Add input validation. 5. Regenerate with sanitized inputs |

## Pitfall-to-Phase Mapping

How roadmap phases should address these pitfalls.

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Credentials in generated code | Phase 1: Template System | `grep` generated files for credential patterns. Zero matches = pass |
| Stdout corruption | Phase 1: Template System | Every template includes stderr redirect. Subprocess test produces valid JSON-RPC only on stdout |
| Template injection | Phase 1: Template System | Fuzz test: generate extensions with special characters in all user inputs. All pass `ast.parse()` |
| Credential detection false positives | Phase 1: Credential Detection | Test with 20 non-credential strings (UUIDs, hashes, code). Zero false vaultings = pass |
| config.yaml race condition | Phase 2: Registration | Stress test: register 3 extensions while sending Telegram messages. No data loss = pass |
| Goosed restart interrupts sessions | Phase 2: Registration | Register extension during active voice session. User notified or session preserved = pass |
| Wrong template selection | Phase 1 + Phase 3: Validation | Generate extensions for 10 credential types. >90% correct template = pass |
| Extension crashes in loop | Phase 3: Validation | Register with invalid credentials. Auto-disabled after 3 failures, user notified = pass |
| Orphaned extensions | Phase 4: Lifecycle | Delete vault key, reboot. Orphaned extension disabled with warning = pass |
| Credential exfiltration via extension | Phase 1: Template System | Generated code passes security lint (no os.system, eval, exec, unrestricted HTTP) = pass |
| Extension name collisions | Phase 2: Registration | Register two extensions with same service type. Unique names assigned, both work = pass |
| Dependency missing in container | Phase 1: Template System | Template manifest declares deps. Generator checks imports before writing. Missing dep = clear error, not crash |

## Sources

- GooseClaw codebase: `docker/gateway.py` race condition documentation (lines 1548-1573), `apply_config()` (line 3372), `start_goosed()` (line 8622), `_save_vault_key()` (line 8603)
- GooseClaw codebase: `docker/entrypoint.sh` extension registration and vault hydration flow
- GooseClaw codebase: `docker/scripts/secret.sh` vault CLI implementation
- GooseClaw codebase: `docker/knowledge/server.py` and `docker/memory/server.py` as reference MCP server patterns (stderr logging, FastMCP usage)
- [MCP security best practices for credentials - Doppler](https://www.doppler.com/blog/mcp-server-credential-security-best-practices) (MEDIUM confidence)
- [State of MCP Server Security 2025 - Astrix](https://astrix.security/learn/blog/state-of-mcp-server-security-2025/) (MEDIUM confidence)
- [MCP Security Vulnerabilities - Practical DevSecOps](https://www.practical-devsecops.com/mcp-security-vulnerabilities/) (MEDIUM confidence)
- [Using Extensions - Goose docs](https://block.github.io/goose/docs/getting-started/using-extensions/) (HIGH confidence)
- [Extension Types and Configuration - DeepWiki](https://deepwiki.com/block/goose/5.3-extension-types-and-configuration) (MEDIUM confidence)
- [Stdio Transport Failure - claude-code#3487](https://github.com/anthropics/claude-code/issues/3487) (HIGH confidence)
- [Config file race condition - copilot-cli#1307](https://github.com/github/copilot-cli/issues/1307) (MEDIUM confidence)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (HIGH confidence)

---
*Pitfalls research for: Auto-generated MCP extensions from user credentials (GooseClaw)*
*Researched: 2026-04-01*

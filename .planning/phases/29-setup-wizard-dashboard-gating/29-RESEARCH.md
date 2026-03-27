# Phase 29: Setup Wizard + Dashboard Gating - Research

**Researched:** 2026-03-27
**Domain:** Setup wizard extension (HTML/JS) + gateway.py vault integration + voice dashboard gating
**Confidence:** HIGH

## Summary

Phase 29 is a pure integration phase. No new libraries, no new protocols. It wires the Gemini API key into the existing setup wizard UI, stores it in the vault (YAML at `/data/secrets/vault.yaml`), and adds a voice dashboard page that gates access on key presence. All four requirements (SETUP-01, SETUP-02, SETUP-04, UI-07) map directly onto existing patterns in the codebase.

The core challenge is that the vault currently has NO write path from the web UI. The `secret.sh` CLI is the only writer. Gateway.py only reads from vault via `_get_gemini_api_key()`. Phase 29 must add a vault write helper to gateway.py and hook it into the setup save flow. The setup wizard (setup.html) needs a new optional field for Gemini API key in step 3 (Optional Settings), and the save handler must extract it and write to vault separately from setup.json.

The voice dashboard page (`/voice`) doesn't exist yet. It needs a route in `do_GET`, a handler that checks `_get_gemini_api_key()`, and either serves voice.html (Phase 30 creates this) or returns a "configure Gemini" page. For Phase 29, voice.html won't exist yet, so the handler should serve a gating page that either shows "configure Gemini" link or a placeholder "voice dashboard coming soon" message when the key IS configured.

**Primary recommendation:** Add `gemini_api_key` field to setup.html step 3, add `_save_vault_key()` helper to gateway.py, hook it into `handle_save()`, and add `/voice` route with Gemini key gating.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SETUP-01 | Gemini API key is an optional provider in the setup wizard | Add Gemini Voice field to setup.html step 3 (Optional Settings), with validation via existing `validate_google()` pattern. NOT a primary provider, just an optional key like Telegram token. |
| SETUP-02 | Voice dashboard reuses existing PBKDF2 cookie-based auth (no separate login) | Use existing `check_auth(self)` call before serving `/voice` page. Same pattern as `handle_admin_page()` and `handle_setup_page()`. |
| SETUP-04 | Gemini API key stored in vault alongside other provider keys | Write to `VAULT_FILE` (`/data/secrets/vault.yaml`) as key `GEMINI_API_KEY`. Need new `_save_vault_key(key, value)` helper in gateway.py. |
| UI-07 | Dashboard is only accessible when Gemini API key is configured, shows setup link otherwise | `/voice` route checks `_get_gemini_api_key()`. If None, serve inline HTML with "configure Gemini" link to `/setup`. If present, serve voice.html (or placeholder until Phase 30). |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib (yaml) | 3.10 | Vault YAML read/write | Already imported in gateway.py. PyYAML is available in container. |
| setup.html inline JS | N/A | Wizard UI for Gemini key input | Single-file constraint. No build tools. Same as existing optional fields. |
| gateway.py | N/A | HTTP handler, vault integration, route gating | Monolith handler. All routes go through GatewayHandler.do_GET/do_POST. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| PyYAML | system | Vault YAML serialization | When writing GEMINI_API_KEY to vault.yaml |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Vault YAML storage | Store in setup.json | setup.json holds provider config. Vault holds service secrets. Gemini voice key is a service secret (not a primary provider). `_get_gemini_api_key()` already reads from vault. Keep consistent. |
| Inline gating page | Separate gate.html file | Extra file complexity. Inline HTML in gateway.py handler is simpler for a ~20 line page. Same pattern as login page. |

**Installation:**
```bash
# No new dependencies. Everything is stdlib + existing PyYAML.
```

## Architecture Patterns

### Relevant File Locations
```
docker/
  gateway.py           # Add: _save_vault_key(), handle_voice_page(), /voice route
  setup.html           # Add: Gemini API key field in step 3, validate function
  tests/
    test_setup.py      # Extend: Gemini key save + vault write tests
    test_voice.py      # Extend: /voice gating tests
```

### Pattern 1: Optional Key in Setup Wizard (step 3)
**What:** Add a Gemini API key input to the "Optional Settings" step, following the exact pattern of Telegram bot token and Groq extraction key.
**When to use:** Any time an optional credential needs to be collected during setup.
**Example:**
```html
<!-- In setup.html step 3, after groq_extraction_key field -->
<div class="field">
  <label>Gemini API Key <span style="color:var(--muted);font-size:12px">(optional, for voice)</span></label>
  <div class="secret-field-wrapper">
    <input type="password" id="geminiApiKey" placeholder="AI..." onblur="validateGeminiFormat(this)">
    <button type="button" class="secret-toggle-btn" onclick="toggleSecretVisibility(this)" title="Toggle visibility">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
    </button>
  </div>
  <div class="field-hint">enables voice dashboard. get key at <a href="https://aistudio.google.com/apikey" target="_blank" style="color:var(--accent)">aistudio.google.com</a></div>
  <div id="geminiFormatError" style="display:none;font-size:12px;color:var(--error);margin-top:4px"></div>
</div>
```

### Pattern 2: Vault Write Helper
**What:** A function to atomically write a key-value pair to vault.yaml.
**When to use:** When the setup wizard saves a Gemini API key.
**Example:**
```python
# Source: Existing vault patterns in gateway.py + secret.sh write logic
def _save_vault_key(key, value):
    """Write a single key-value pair to vault.yaml. Atomic write."""
    import yaml
    os.makedirs(os.path.dirname(VAULT_FILE), exist_ok=True)
    data = {}
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}
    data[key] = value
    tmp_path = VAULT_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, VAULT_FILE)  # atomic on same filesystem
```

### Pattern 3: Page Gating on Key Presence
**What:** A route handler that checks for Gemini API key and either serves the page or shows a "configure" message.
**When to use:** Voice dashboard access control.
**Example:**
```python
# Source: handle_admin_page() pattern in gateway.py
def handle_voice_page(self):
    """Serve voice dashboard. Gate on auth + Gemini key presence."""
    if not check_auth(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()
        return
    api_key = _get_gemini_api_key()
    if not api_key:
        # Serve inline gating page with link to setup
        self._serve_voice_gate_page()
        return
    # Serve voice.html (Phase 30 creates this file)
    try:
        with open(VOICE_HTML, "rb") as f:
            content = f.read()
        # ... same pattern as handle_admin_page() ...
    except FileNotFoundError:
        self._serve_voice_gate_page()
```

### Pattern 4: Gemini Key Validation
**What:** Validate Gemini API key by calling the models listing endpoint.
**When to use:** Before storing the key in vault. Called from setup wizard "Test" flow.
**Example:**
```python
# Source: existing validate_google() in gateway.py
def validate_gemini_voice(api_key):
    """Validate Gemini API key for voice. Uses models listing endpoint."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}"
    try:
        status, body = http_get(url)
        if status == 200:
            return {"valid": True, "message": "Gemini API key is valid. Voice dashboard will be enabled."}
        elif status in (400, 401, 403):
            return {"valid": False, "error": "Invalid Gemini API key."}
        else:
            return {"valid": False, "error": f"Unexpected response (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Google AI API."}
```

### Anti-Patterns to Avoid
- **Storing Gemini key in setup.json:** `_get_gemini_api_key()` reads from vault.yaml. Storing elsewhere creates inconsistency.
- **Creating a new API endpoint for vault writes:** The setup save handler is the right place. Don't expose a generic vault write API.
- **Making Gemini a primary provider:** Gemini voice is an optional service key, not the main LLM provider. It goes in step 3 alongside Telegram and Groq, not in step 0 provider grid.
- **Requiring Gemini key for setup completion:** The key must be optional. Users can add it later via reconfigure.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| YAML serialization | Custom YAML writer | PyYAML `yaml.dump()` | Edge cases in YAML (quoting, special chars). PyYAML handles all of them. Already used in vault reads. |
| API key validation | Custom HTTP client | Existing `http_get()` helper | Already handles timeouts, errors, HTTPS. Same function used by `validate_google()`. |
| Auth checking | New auth mechanism | Existing `check_auth(self)` | PBKDF2 cookie auth is already implemented and battle-tested. Just call it. |
| Redaction | Custom mask function | Existing `_REDACTED` / `SENSITIVE_KEYS` pattern | The get_safe_setup() and related code already handles redaction. Follow the pattern. |

**Key insight:** Phase 29 is integration glue. Every piece already exists (vault read, auth check, setup wizard fields, validation functions). The work is wiring them together correctly.

## Common Pitfalls

### Pitfall 1: Vault Write Race Condition
**What goes wrong:** Two concurrent save requests could clobber vault.yaml.
**Why it happens:** No locking around vault file writes.
**How to avoid:** Use atomic write (write to .tmp, then `os.replace()`). The `save_setup()` function already does this for setup.json. Copy the pattern exactly.
**Warning signs:** Vault.yaml becomes empty or has partial content after save.

### Pitfall 2: Gemini Key Not Available After Save Without Restart
**What goes wrong:** User saves Gemini key, visits /voice, but `_get_gemini_api_key()` doesn't see it because it reads from disk on every call.
**Why it happens:** Actually, this WON'T be a problem because `_get_gemini_api_key()` reads from disk every time (no caching). But verify this assumption.
**How to avoid:** Confirm `_get_gemini_api_key()` has no in-memory cache. It doesn't (it opens and reads VAULT_FILE on every call).
**Warning signs:** Key saved but /voice still shows gating page.

### Pitfall 3: Reconfigure Loses Gemini Key
**What goes wrong:** User reconfigures main provider. Gemini key field is blank (user didn't re-enter it). Save handler clears it from vault.
**Why it happens:** Same issue as Telegram token: blank field on reconfigure should mean "keep existing", not "clear".
**How to avoid:** Follow the Telegram/Groq pattern: if field is blank AND `gemini_api_key_set` is true (reconfigure mode), skip the vault write. Only write to vault if user enters a new value.
**Warning signs:** Gemini key disappears after reconfigure.

### Pitfall 4: SENSITIVE_KEYS Not Updated
**What goes wrong:** Gemini API key shows up in API responses because it's not in the redaction list.
**Why it happens:** New key added to setup flow but not added to SENSITIVE_KEYS or get_safe_setup().
**How to avoid:** Since Gemini key lives in vault (not setup.json), it won't appear in get_safe_setup() responses. BUT, if we add a `gemini_api_key` field to the config object temporarily during save, we must ensure it's stripped or redacted.
**Warning signs:** API key visible in /api/setup/config response.

### Pitfall 5: Voice Page Served Without CSP Headers
**What goes wrong:** Voice page lacks security headers.
**Why it happens:** New route handler forgets to add CSP headers.
**How to avoid:** Copy the exact header pattern from `handle_admin_page()`. Include SECURITY_HEADERS, CSP, and HSTS.
**Warning signs:** Missing X-Frame-Options, X-Content-Type-Options, etc. on /voice response.

## Code Examples

### saveConfig() JS Addition for Gemini Key
```javascript
// Source: Existing saveConfig() pattern in setup.html
// In saveConfig(), add after groq_extraction_key handling:
const geminiKey = document.getElementById('geminiApiKey')?.value || '';
if (geminiKey) {
  config.gemini_api_key = geminiKey;
} else if (isReconfigure && dashboardConfig.gemini_api_key_set) {
  /* keep existing -- omit field so backend preserves it */
} else {
  config.gemini_api_key = '';  // explicit clear
}
```

### handle_save() Addition for Vault Write
```python
# Source: Existing handle_save() pattern in gateway.py
# In handle_save(), after save_setup(config) and before apply_config(config):

# write Gemini API key to vault if provided
gemini_key = config.pop("gemini_api_key", None)
if gemini_key and gemini_key != _REDACTED:
    _save_vault_key("GEMINI_API_KEY", gemini_key)
```

### get_safe_setup() Addition for gemini_api_key_set Indicator
```python
# Source: Existing pattern for telegram_bot_token_set in handle_get_config()
# In handle_get_config(), add after telegram_bot_token_set:
safe["gemini_api_key_set"] = bool(_get_gemini_api_key())
```

### Gating Page HTML (inline in gateway.py)
```python
# Source: Derived from existing login page pattern
_VOICE_GATE_HTML = b"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice - GooseClaw</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .gate { text-align: center; max-width: 400px; padding: 40px; }
  .gate h1 { font-size: 24px; margin-bottom: 8px; }
  .gate p { color: #888; margin-bottom: 24px; }
  .gate a { color: #7c6aef; text-decoration: none; font-weight: 500; }
  .gate a:hover { text-decoration: underline; }
</style>
</head><body>
<div class="gate">
  <h1>Voice Dashboard</h1>
  <p>Voice requires a Gemini API key. Add one in setup to enable the voice dashboard.</p>
  <a href="/setup">Configure Gemini</a>
</div>
</body></html>"""
```

### Validate Gemini Key on Blur (JS)
```javascript
// Source: Derived from validateGroqFormat() pattern
function validateGeminiFormat(el) {
  const err = document.getElementById('geminiFormatError');
  const val = el.value.trim();
  if (!val) { err.style.display = 'none'; return; }
  if (val.length < 10) {
    err.textContent = 'API key appears too short';
    err.style.display = 'block';
  } else {
    err.style.display = 'none';
  }
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Vault write only via CLI (secret.sh) | Vault write from web UI via setup save | Phase 29 (now) | Users can configure Gemini without SSH access |
| No voice route in gateway.py | /voice route with key gating | Phase 29 (now) | Prerequisite for Phase 30 voice dashboard |

## Open Questions

1. **Should Gemini key validation use models list or generateContent?**
   - What we know: The context says "POST to gemini-3.1-flash-live-preview:generateContent" but existing `validate_google()` uses GET models list.
   - What's unclear: Whether models list endpoint confirms the key works for Live API specifically.
   - Recommendation: Use models list (GET `/v1beta/models?key=`) for validation. It's simpler, doesn't cost tokens, and confirms the key is valid for Gemini generally. The Live API uses the same key.

2. **What should /voice show when key IS configured but voice.html doesn't exist yet?**
   - What we know: Phase 30 creates voice.html. Phase 29 runs first.
   - What's unclear: Should we create a placeholder voice.html or handle FileNotFoundError?
   - Recommendation: Handle FileNotFoundError gracefully. Show a "Voice dashboard is being set up" message. This avoids creating a file that Phase 30 will overwrite.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (system-installed) |
| Config file | None (default discovery) |
| Quick run command | `cd /Users/haseeb/nix-template/docker && python -m pytest tests/test_voice.py tests/test_setup.py -x -q` |
| Full suite command | `cd /Users/haseeb/nix-template/docker && python -m pytest tests/ -x -q --ignore=tests/e2e` |
| Estimated runtime | ~5 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SETUP-01 | Gemini key field in setup wizard, validation on blur | manual-only | N/A (HTML/JS UI behavior) | N/A |
| SETUP-02 | /voice requires PBKDF2 auth cookie | integration | `python -m pytest tests/test_voice.py::TestVoicePageGating::test_voice_requires_auth -x` | No, Wave 0 gap |
| SETUP-04 | Gemini key saved to vault.yaml via handle_save | unit + integration | `python -m pytest tests/test_voice.py::TestVaultWrite -x` | No, Wave 0 gap |
| UI-07 | /voice shows gate page when no key, shows dashboard when key present | integration | `python -m pytest tests/test_voice.py::TestVoicePageGating -x` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd /Users/haseeb/nix-template/docker && python -m pytest tests/test_voice.py tests/test_setup.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work`
- **Estimated feedback latency per task:** ~5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/test_voice.py::TestVaultWrite` -- covers SETUP-04 (vault key save/read roundtrip)
- [ ] `docker/tests/test_voice.py::TestVoicePageGating` -- covers SETUP-02, UI-07 (auth gating + key gating on /voice)
- [ ] `docker/tests/test_setup.py::TestGeminiKeyInSetup` -- covers SETUP-01 (gemini_api_key field in save config, gemini_api_key_set in get_config)

## Sources

### Primary (HIGH confidence)
- gateway.py source code (lines 8440-8519) -- vault read functions, VAULT_FILE path, _get_gemini_api_key()
- gateway.py source code (lines 9700-9946) -- setup page handler, save handler, auth flow
- setup.html source code (lines 1640-1750) -- Optional Settings step, field patterns
- setup.html source code (lines 5377-5502) -- saveConfig() JS function, key handling
- gateway.py source code (lines 2809-2825) -- validate_google() function
- docker/scripts/secret.sh -- vault write logic (YAML read-modify-write)
- docker/tests/test_voice.py -- existing voice token + WebSocket auth tests
- docker/tests/conftest.py -- test fixtures (live_gateway, auth_session)

### Secondary (MEDIUM confidence)
- [Google AI API Key docs](https://ai.google.dev/gemini-api/docs/api-key) -- key validation approach

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all existing code, no new dependencies
- Architecture: HIGH - direct extension of existing patterns (setup wizard, vault, route handlers)
- Pitfalls: HIGH - all pitfalls derived from actual code analysis (vault race conditions, reconfigure key loss)

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (stable patterns, no external dependency changes expected)

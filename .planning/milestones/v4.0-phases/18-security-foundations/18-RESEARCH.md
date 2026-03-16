# Phase 18: Security Foundations - Research

**Researched:** 2026-03-16
**Domain:** Shell injection elimination, PBKDF2 password hashing with lazy migration, secret leak prevention, request body limits, HTTP security headers
**Confidence:** HIGH

## Summary

Phase 18 addresses 8 requirements across 3 plans. All fixes are surgical, in-place modifications to existing files. No new modules, no new pip dependencies, no architectural changes. The codebase has been analyzed at the line level and every fix location is confirmed.

The highest-risk item is PBKDF2 password hashing with lazy SHA-256 migration (SEC-04, SEC-05). The current `hash_token()` at gateway.py:1088 returns bare SHA-256 hex with no algorithm prefix, no salt. The migration path requires a versioned hash format (`$pbkdf2$salt$hash`) and dual-path verification in `verify_token()`. If the migration path is tested only with fresh hashes (not actual SHA-256 values from a live setup.json), deployed users get locked out on upgrade.

The shell injection fixes (SEC-01, SEC-02, SEC-03) are mechanical. secret.sh uses `'$VARIABLE'` interpolation into `python3 -c` strings across all 4 CRUD commands. entrypoint.sh embeds `$GOOSECLAW_RESET_PASSWORD` directly into inline Python. gateway.py `_run_script` uses `shell=True`. All three use the same fix pattern: pass data via `os.environ` reads inside Python, and switch to explicit `["/bin/sh", "-c", cmd]` for subprocess. The correct pattern already exists in job.sh `cmd_create`.

**Primary recommendation:** Fix password hashing first (auth must work before anything else is testable), then shell injection (mechanical grep-and-fix), then leak/limits/headers (one-liners).

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SEC-01 | Shell injection in secret.sh eliminated | secret.sh lines 37-116: all 4 commands use `'$DOTPATH'` and `'''$VALUE'''` interpolation. Fix: `os.environ` reads. Full file analyzed. |
| SEC-02 | Shell injection in entrypoint.sh eliminated | entrypoint.sh lines 57-73: `$GOOSECLAW_RESET_PASSWORD` and `$DATA_DIR` interpolated into Python. Fix: `os.environ` reads. |
| SEC-03 | Command injection in gateway.py `_run_script` eliminated | gateway.py line 3801-3803: `subprocess.run(command, shell=True)`. Fix: `subprocess.run(["/bin/sh", "-c", command])`. |
| SEC-04 | Password hashing upgraded to PBKDF2 with salt and 600K iterations | gateway.py lines 1088-1095: `hash_token()` and `verify_token()` use bare SHA-256. Replace with `hashlib.pbkdf2_hmac('sha256', ..., 600_000)` + 16-byte salt. |
| SEC-05 | Existing SHA-256 hashes transparently migrate to PBKDF2 on login | `verify_token()` must detect bare hex (no `$pbkdf2$` prefix) and fall back to SHA-256 verification. On success, rehash and save to setup.json. |
| SEC-06 | Recovery secret no longer printed to container logs | entrypoint.sh line 39: `echo "[init] GOOSECLAW_RECOVERY_SECRET=$RECOVERY_SECRET"`. Remove or mask. |
| SEC-07 | Request body size limited to configurable max (default 1MB) | gateway.py line 9705-9707: `_read_body()` has no size check. Add `Content-Length` check before `rfile.read()`. |
| HARD-04 | Missing HTTP security headers added | gateway.py line 860-866: `SECURITY_HEADERS` dict. Currently has Referrer-Policy and Permissions-Policy. Missing: Cross-Origin-Opener-Policy. HSTS already present conditionally. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `hashlib.pbkdf2_hmac` | stdlib (Python 3.4+) | Password hashing | OWASP-approved KDF, 600K iterations, no pip dependency |
| `os.urandom` | stdlib | Salt generation | Cryptographically secure random bytes |
| `hmac.compare_digest` | stdlib | Constant-time comparison | Prevents timing attacks on hash verification |
| `base64` | stdlib | Hash/salt encoding | Encode binary salt and hash for storage in JSON |
| `os.environ` | stdlib | Safe data passing to inline Python | Eliminates shell interpolation injection vectors |
| `subprocess.run` with list args | stdlib | Safe subprocess execution | Avoids `shell=True` platform-dependent shell selection |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `secrets.compare_digest` | stdlib | Recovery secret comparison | Already used at gateway.py:9527 |
| `json` | stdlib | setup.json read/write for hash migration | Already used throughout |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| PBKDF2 | `hashlib.scrypt()` | Memory-hard (stronger), but PBKDF2 is more widely audited and OWASP explicitly recommends it. Architecture research initially suggested scrypt, but REQUIREMENTS.md and STATE.md lock PBKDF2. |
| PBKDF2 | argon2-cffi | Strongest option but requires pip. Ruled out: stdlib-only constraint for gateway.py runtime. |

## Architecture Patterns

### Pattern 1: Versioned Hash Format with Lazy Migration

**What:** Store password hashes with algorithm prefix so verify can dispatch to the right algorithm.
**When to use:** Any time you upgrade a hash algorithm on an existing user base.

```python
# Hash format: "$pbkdf2$<base64-salt>$<base64-hash>"
# Legacy format: bare 64-char hex string (SHA-256)

import hashlib, os, base64, hmac

PBKDF2_ITERATIONS = 600_000

def hash_token(token):
    """Hash password using PBKDF2-SHA256. Returns versioned string."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', token.encode(), salt, PBKDF2_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode()
    dk_b64 = base64.b64encode(dk).decode()
    return f"$pbkdf2${salt_b64}${dk_b64}"

def verify_token(provided, stored):
    """Verify password. Supports PBKDF2 and legacy SHA-256."""
    if stored.startswith("$pbkdf2$"):
        parts = stored.split("$")  # ['', 'pbkdf2', salt_b64, dk_b64]
        salt = base64.b64decode(parts[2])
        expected = base64.b64decode(parts[3])
        dk = hashlib.pbkdf2_hmac('sha256', provided.encode(), salt, PBKDF2_ITERATIONS)
        return hmac.compare_digest(dk, expected)
    # legacy SHA-256: bare hex, no salt
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).hexdigest(),
        stored
    )
```

**Confidence:** HIGH. This is the standard lazy migration pattern used by Django, Werkzeug, and passlib.

### Pattern 2: os.environ Data Passing for Inline Python

**What:** Pass shell variables to `python3 -c` via environment variables instead of string interpolation.
**When to use:** Every `python3 -c` call that references shell variables.

```bash
# BEFORE (vulnerable):
python3 -c "keys = '$DOTPATH'.split('.')"

# AFTER (safe):
_DOTPATH="$DOTPATH" _VALUE="$VALUE" python3 -c "
import os
keys = os.environ['_DOTPATH'].split('.')
value = os.environ['_VALUE']
"
```

**Confidence:** HIGH. The correct pattern already exists in job.sh `cmd_create`.

### Pattern 3: Explicit Shell for Subprocess

**What:** Replace `shell=True` with `["/bin/sh", "-c", command]` for job execution.
**When to use:** When commands legitimately need shell interpretation (pipes, redirects) but you want explicit control.

```python
# BEFORE:
subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)

# AFTER:
subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, timeout=timeout)
```

**Confidence:** HIGH. Functionally identical but explicit about which shell runs. Avoids Python's platform-dependent shell selection.

### Pattern 4: Body Size Guard

**What:** Check Content-Length before reading request body.
**When to use:** Every POST handler.

```python
MAX_BODY_SIZE = 1_048_576  # 1 MB

def _read_body(self):
    length = int(self.headers.get("Content-Length", 0))
    if length > MAX_BODY_SIZE:
        self.send_json(413, {"error": "Request body too large", "max_bytes": MAX_BODY_SIZE})
        return None
    if length <= 0:
        return b""
    return self.rfile.read(length)
```

**Confidence:** HIGH. Note: callers of `_read_body()` must check for `None` return and bail early.

### Anti-Patterns to Avoid
- **Testing migration with fresh hashes only:** MUST test with an actual SHA-256 hex string (64 chars, no prefix) to verify the legacy path works.
- **Using `secrets.compare_digest` for hash comparison:** Use `hmac.compare_digest` for comparing derived keys (bytes). `secrets.compare_digest` works too but `hmac` is the canonical choice for HMAC/KDF outputs.
- **Naively setting `shell=False` for job commands:** Breaks pipes and redirects. Use `["/bin/sh", "-c", cmd]` instead.
- **Adding body size check only to `_read_body`:** The proxy path at line 9601-9602 reads body with a separate inline read. Must also be guarded.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Password hashing | Custom iteration loop | `hashlib.pbkdf2_hmac()` | Handles iteration count, HMAC construction, constant-time internally |
| Constant-time comparison | `==` on hash strings | `hmac.compare_digest()` | `==` leaks timing information, enables timing attacks |
| Salt generation | `random.randbytes()` | `os.urandom(16)` | `random` is not cryptographically secure |
| Hash format parsing | Regex or manual split | Prefix-based dispatch (`startswith("$pbkdf2$")`) | Simple, extensible if future algorithms needed |

## Common Pitfalls

### Pitfall 1: Password Migration Locks Out Existing Users
**What goes wrong:** New `verify_token()` doesn't recognize bare SHA-256 hex. User enters correct password. Verification fails. Locked out.
**Why it happens:** Developer tests with fresh installs only. Never tests with a pre-existing SHA-256 hash from setup.json.
**How to avoid:** `verify_token()` checks for `$pbkdf2$` prefix first. If absent, falls back to SHA-256 verification. Write explicit test: `verify_token("password", hashlib.sha256(b"password").hexdigest())` must return True.
**Warning signs:** No test named `test_legacy_sha256_migration` or similar.

### Pitfall 2: entrypoint.sh Password Reset Still Uses SHA-256
**What goes wrong:** Emergency password reset (entrypoint.sh lines 57-73) still calls `hashlib.sha256()`. New password gets stored as bare SHA-256. Next login via gateway.py sees bare hex, falls back to SHA-256, works. But the password never gets upgraded to PBKDF2 until the NEXT login after that.
**Why it happens:** Developer fixes gateway.py but forgets entrypoint.sh has its own hash_token equivalent.
**How to avoid:** entrypoint.sh password reset must also generate PBKDF2 hashes with the `$pbkdf2$` prefix. Replicate the exact same format.

### Pitfall 3: Rate Limiter Must Run Before PBKDF2
**What goes wrong:** Brute-force attack sends 100 req/s. Each triggers 600K PBKDF2 iterations (~250ms). CPU saturated even though rate limiter would reject most.
**Why it happens:** Body parsing or hash computation happens before rate limit check.
**How to avoid:** VERIFIED: `handle_auth_login()` at line 9468 calls `_check_rate_limit(auth_limiter)` FIRST (line 9470), before reading body or computing hash. This is already correct. No change needed.

### Pitfall 4: `_read_body()` Callers Must Handle None
**What goes wrong:** Adding size limit to `_read_body()` makes it return `None` on oversized requests. But existing callers don't check for `None`. They pass it to `json.loads()` which throws TypeError.
**Why it happens:** Changing return type of existing function without updating all call sites.
**How to avoid:** Grep for every `_read_body()` call site. Each must handle `None` (already sent 413, just `return`).

### Pitfall 5: Proxy Body Read Not Guarded
**What goes wrong:** The reverse proxy path at gateway.py line 9601-9602 reads body inline: `body = self.rfile.read(content_length)`. This bypasses `_read_body()` and its new size guard.
**Why it happens:** Proxy has its own body reading logic separate from the JSON API handlers.
**How to avoid:** Add size check to the proxy body read as well, or refactor to use `_read_body()`.

## Code Examples

### secret.sh Full Fix Pattern (SEC-01)

All 4 commands need the same transformation. Example for `get`:

```bash
# get command - BEFORE (vulnerable):
python3 -c "
import yaml, sys
try:
    with open('$VAULT_FILE') as f:
        data = yaml.safe_load(f) or {}
    keys = '$DOTPATH'.split('.')
    ...
"

# get command - AFTER (safe):
_VAULT_FILE="$VAULT_FILE" _DOTPATH="$DOTPATH" python3 -c "
import yaml, sys, os
try:
    with open(os.environ['_VAULT_FILE']) as f:
        data = yaml.safe_load(f) or {}
    keys = os.environ['_DOTPATH'].split('.')
    ...
"
```

For `set` command, also fix the triple-quote value injection:
```bash
# set command - the VALUE assignment is the critical fix:
# BEFORE: d[keys[-1]] = '''$VALUE'''
# AFTER:  d[keys[-1]] = os.environ['_VALUE']
```

Variables to convert per command:
- `get`: `$VAULT_FILE`, `$DOTPATH` (2 vars)
- `set`: `$VAULT_FILE`, `$DOTPATH`, `$VALUE` (3 vars)
- `list`: `$VAULT_FILE` (1 var)
- `delete`: `$VAULT_FILE`, `$DOTPATH` (2 vars)

### entrypoint.sh Password Reset Fix (SEC-02)

```bash
# BEFORE (lines 59-73):
python3 -c "
import json, hashlib, os
setup_path = os.path.join('$DATA_DIR', 'config', 'setup.json')
...
    pw = '$GOOSECLAW_RESET_PASSWORD'
    setup['web_auth_token_hash'] = hashlib.sha256(pw.encode()).hexdigest()
"

# AFTER:
_DATA_DIR="$DATA_DIR" _RESET_PW="$GOOSECLAW_RESET_PASSWORD" python3 -c "
import json, hashlib, os, base64
setup_path = os.path.join(os.environ['_DATA_DIR'], 'config', 'setup.json')
...
    pw = os.environ['_RESET_PW']
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 600_000)
    salt_b64 = base64.b64encode(salt).decode()
    dk_b64 = base64.b64encode(dk).decode()
    setup['web_auth_token_hash'] = f'\$pbkdf2\${salt_b64}\${dk_b64}'
"
```

### HARD-04: Missing Security Headers

Current `SECURITY_HEADERS` (line 860-866):
```python
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
```

Add the missing header per HARD-04:
```python
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
```

Note: HSTS is already applied conditionally when `RAILWAY_ENVIRONMENT` is set (verified at lines 8275, 8314, 9642, 9733). Referrer-Policy and Permissions-Policy are already in the dict. HARD-04 specifically calls for Cross-Origin-Opener-Policy which is the only truly missing one.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| SHA-256 bare hex | PBKDF2 with salt + versioned prefix | OWASP 2023+ | GPU cracking goes from milliseconds to years |
| `shell=True` | Explicit `["/bin/sh", "-c", cmd]` | Always been best practice | Avoids platform-dependent shell, explicit control |
| `'$VAR'` in python3 -c | `os.environ['VAR']` | Always been best practice | Zero injection surface |
| No body limits | Content-Length check before read | Always been best practice | Prevents OOM DoS |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (not currently installed, needs requirements-dev.txt) |
| Config file | None yet, needs pyproject.toml or pytest.ini |
| Quick run command | `cd docker && python -m pytest test_gateway.py -x -q` |
| Full suite command | `cd docker && python -m pytest -x -q` |
| Estimated runtime | ~5-10 seconds (existing tests use unittest, run fast) |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SEC-01 | secret.sh no longer injectable via dotpath/value with quotes | integration | `cd docker && python -m pytest tests/test_security.py::test_secret_sh_injection -x` | No, Wave 0 gap |
| SEC-02 | entrypoint.sh password reset uses os.environ, not interpolation | integration | `cd docker && python -m pytest tests/test_security.py::test_entrypoint_injection -x` | No, Wave 0 gap |
| SEC-03 | _run_script uses list args not shell=True | unit | `cd docker && python -m pytest tests/test_security.py::test_run_script_no_shell_true -x` | No, Wave 0 gap |
| SEC-04 | hash_token returns $pbkdf2$ prefixed string with salt | unit | `cd docker && python -m pytest tests/test_auth.py::test_pbkdf2_hash_format -x` | No, Wave 0 gap |
| SEC-05 | verify_token accepts legacy SHA-256 hex and triggers migration | unit | `cd docker && python -m pytest tests/test_auth.py::test_legacy_sha256_migration -x` | No, Wave 0 gap |
| SEC-06 | Recovery secret not in entrypoint stdout | integration | `cd docker && python -m pytest tests/test_security.py::test_recovery_secret_not_leaked -x` | No, Wave 0 gap |
| SEC-07 | _read_body rejects >1MB with 413 | unit | `cd docker && python -m pytest tests/test_security.py::test_body_size_limit -x` | No, Wave 0 gap |
| HARD-04 | SECURITY_HEADERS includes Cross-Origin-Opener-Policy | unit | `cd docker && python -m pytest tests/test_security.py::test_security_headers_complete -x` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd docker && python -m pytest tests/test_auth.py tests/test_security.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~3-5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/__init__.py` -- package init
- [ ] `docker/tests/test_auth.py` -- covers SEC-04, SEC-05 (PBKDF2 hashing, lazy migration)
- [ ] `docker/tests/test_security.py` -- covers SEC-01, SEC-02, SEC-03, SEC-06, SEC-07, HARD-04
- [ ] `docker/tests/conftest.py` -- shared fixtures (temp data dirs, mock setup.json with SHA-256 hash)
- [ ] No framework install needed: `python -m pytest` works if pytest is available; existing tests use unittest which runs without pytest too

## Open Questions

1. **PBKDF2 iteration count on Railway CPU**
   - What we know: 600K iterations takes ~250ms on fast hardware. Railway containers may be throttled.
   - What's unclear: Actual latency on Railway's CPU allocation.
   - Recommendation: Use 600K. If benchmarking during implementation shows >500ms, drop to 300K. Either is vastly better than bare SHA-256.

2. **Proxy body read at line 9601**
   - What we know: The proxy path reads body inline, bypassing `_read_body()`.
   - What's unclear: Whether proxied requests to goosed need the same 1MB limit or a different one.
   - Recommendation: Apply same MAX_BODY_SIZE check to proxy path. Goosed requests are typically small JSON.

## Sources

### Primary (HIGH confidence)
- Codebase analysis: gateway.py (lines 860-866, 1088-1095, 3775-3803, 7908-7923, 9468-9498, 9705-9707), entrypoint.sh (lines 33-39, 57-73), secret.sh (full file, 124 lines)
- Python hashlib docs: `pbkdf2_hmac()` confirmed stdlib since 3.4
- OWASP Password Storage Cheat Sheet: PBKDF2 with 600K iterations recommended for SHA-256

### Secondary (MEDIUM confidence)
- ARCHITECTURE.md and PITFALLS.md from project research: implementation patterns and migration strategies verified against actual codebase

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all stdlib, no external dependencies, verified against Python 3.10 docs
- Architecture: HIGH - every fix location verified at exact line numbers in current codebase
- Pitfalls: HIGH - migration lockout pattern confirmed by analyzing actual `hash_token`/`verify_token` code and entrypoint.sh reset path

**Research date:** 2026-03-16
**Valid until:** 2026-04-16 (stable domain, no fast-moving dependencies)

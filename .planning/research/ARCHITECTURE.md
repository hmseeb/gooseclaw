# Architecture Research: Production Hardening

**Domain:** Self-hosted AI agent platform (Docker, Python stdlib HTTP server)
**Researched:** 2026-03-16
**Confidence:** HIGH

## Current System Overview

```
                        Railway / Docker Host
 +-----------------------------------------------------------------+
 |  entrypoint.sh (root -> drops to gooseclaw user)                |
 |    |                                                            |
 |    +-- gateway.py (Python stdlib HTTP, port 8080)               |
 |    |     |-- setup wizard (serves setup.html)                   |
 |    |     |-- reverse proxy -> goosed (internal port)            |
 |    |     |-- notification bus (telegram, plugins)               |
 |    |     |-- job engine (cron, timers)                          |
 |    |     |-- session manager                                    |
 |    |     |-- auth (SHA-256 password hashing)                    |
 |    |     +-- rate limiter                                       |
 |    |                                                            |
 |    +-- goosed subprocess (goose AI agent)                       |
 |    +-- telegram bot(s) (managed by gateway.py)                  |
 |    +-- knowledge MCP server (ChromaDB, pip-installed)           |
 |    +-- persist loop (git push, optional)                        |
 |    +-- watchdog (process health checks)                         |
 |                                                                 |
 |  /data (Railway volume)                                         |
 |    +-- config/setup.json, config.yaml                           |
 |    +-- secrets/vault.yaml (chmod 600)                           |
 |    +-- sessions/, channels/, knowledge/                         |
 +-----------------------------------------------------------------+
```

### Component Responsibilities

| Component | Responsibility | File | Lines (approx) |
|-----------|---------------|------|-----------------|
| gateway.py | HTTP server, auth, proxy, jobs, notifications, sessions | docker/gateway.py | ~9800 |
| entrypoint.sh | Container bootstrap, env hydration, user setup, config gen | docker/entrypoint.sh | ~700 |
| secret.sh | Vault CRUD (read/write credentials) | docker/scripts/secret.sh | ~124 |
| job.sh | Job creation CLI (wraps /api/jobs) | docker/scripts/job.sh | ~387 |
| remind.sh | Reminder CLI (wraps /api/jobs) | docker/scripts/remind.sh | ~246 |
| notify.sh | Notification CLI (wraps /api/notify) | docker/scripts/notify.sh | ~46 |
| setup.html | Single-file wizard UI | docker/setup.html | large |
| Dockerfile | Image build, deps, goose install | Dockerfile | ~79 |

## Hardening Integration Map

Each fix mapped to exactly where it goes and what it touches.

### 1. Password Hashing: SHA-256 -> scrypt

**Where:** `gateway.py` lines 1086-1095 (`hash_token`, `verify_token`)
**Also:** `entrypoint.sh` line 66 (emergency password reset uses `hashlib.sha256`)

**Approach:** Use `hashlib.scrypt()` from Python stdlib. Available since Python 3.6 when compiled with OpenSSL. Ubuntu 22.04 ships Python 3.10 with OpenSSL, so scrypt is guaranteed available. No pip required.

**Implementation:**

```python
import hashlib
import os
import base64

def hash_token(token):
    """Hash password using scrypt (RFC 7914). stdlib only."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    # store as salt:hash, both base64-encoded
    return base64.b64encode(salt).decode() + ":" + base64.b64encode(dk).decode()

def verify_token(provided, stored):
    """Verify password against stored scrypt hash. Falls back to SHA-256 for migration."""
    if ":" not in stored:
        # legacy SHA-256 hash, verify and signal migration needed
        return hashlib.sha256(provided.encode()).hexdigest() == stored
    salt_b64, hash_b64 = stored.split(":", 1)
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(hash_b64)
    dk = hashlib.scrypt(provided.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    # constant-time comparison
    return hmac.compare_digest(dk, expected)
```

**Migration strategy:** `verify_token` detects old SHA-256 hashes (no ":" separator) and verifies against them. On next successful login, re-hash with scrypt and update setup.json. Zero downtime, no manual migration.

**entrypoint.sh fix:** The emergency password reset (line 59-73) must also use scrypt. Replace the inline Python with a call to a shared utility or replicate the scrypt logic.

**Confidence:** HIGH. `hashlib.scrypt` is in Python stdlib docs, Ubuntu 22.04 Python 3.10 has OpenSSL.

### 2. Shell Injection Fixes

Three distinct injection vectors, each requiring a different fix.

#### 2a. secret.sh: Direct Variable Interpolation

**Where:** `docker/scripts/secret.sh` lines 37-116
**Vulnerability:** User-controlled `$DOTPATH` and `$VALUE` are interpolated directly into Python string literals via `'$DOTPATH'` and `'''$VALUE'''`. A value containing `'''` breaks out of the Python string.

**Fix:** Pass values through environment variables instead of shell interpolation.

```bash
# BEFORE (vulnerable):
python3 -c "
keys = '$DOTPATH'.split('.')
d[keys[-1]] = '''$VALUE'''
"

# AFTER (safe):
_DOTPATH="$DOTPATH" _VALUE="$VALUE" python3 -c "
import os
keys = os.environ['_DOTPATH'].split('.')
d[keys[-1]] = os.environ['_VALUE']
"
```

**Scope:** All 4 commands in secret.sh (get, set, list, delete) use this pattern.

#### 2b. entrypoint.sh: Variable Interpolation in Inline Python

**Where:** `docker/entrypoint.sh` lines 59-73 (password reset), lines 96-111 (config.yaml generation)
**Vulnerability:** `$GOOSECLAW_RESET_PASSWORD` and `$DATA_DIR` interpolated into Python code. Less exploitable (env vars are operator-controlled, not user-controlled), but still a code smell.

**Fix:** Same pattern. Use `os.environ` reads inside the Python snippets instead of shell variable interpolation.

#### 2c. gateway.py _run_script: shell=True

**Where:** `gateway.py` line 3801-3803
**Vulnerability:** `subprocess.run(command, shell=True)` where `command` comes from job definitions. Jobs are created via authenticated API, so the attack surface is limited to authenticated users. But `shell=True` also enables accidental breakage from special characters.

**Fix:** This is the trickiest one. Job commands are intentionally shell commands (pipes, redirects, etc.). Options:

1. **Keep shell=True but sanitize** -- impractical, shell is too complex to sanitize
2. **Switch to shell=False with explicit sh -c** -- functionally identical but more explicit:
   ```python
   subprocess.run(["/bin/sh", "-c", command], ...)
   ```
   This is semantically the same but avoids Python's platform-dependent shell selection.
3. **Accept the risk** -- jobs are admin-only, behind auth. Document the trust boundary.

**Recommendation:** Option 2. It's explicit about which shell runs and avoids any platform surprises. Also add input validation on job creation: reject commands containing known-dangerous patterns or enforce a max length.

**Confidence:** HIGH. These are well-understood injection patterns.

### 3. Recovery Secret Leak

**Where:** `docker/entrypoint.sh` line 39
**Vulnerability:** `echo "[init] GOOSECLAW_RECOVERY_SECRET=$RECOVERY_SECRET"` prints the secret to stdout (container logs). Anyone with Railway log access sees it.

**Fix:** Remove the echo. The secret is already saved to `/data/.recovery_secret`. Users can retrieve it via `cat /data/.recovery_secret` in the Railway shell, or it's exposed in the setup wizard config response (gateway.py line 8431) behind auth.

Also fix: gateway.py line 8431 exposes `recovery_secret` in the setup config response. This should only be returned on first setup, not on every config fetch.

**Confidence:** HIGH.

### 4. Structured JSON Logging

**Where:** All `print()` calls across gateway.py (~100+ locations), entrypoint.sh, shell scripts
**Current pattern:** `print(f"[component] message")` -- unstructured text with bracket-prefix convention.

**Approach:** Build a minimal JSON logger using Python stdlib `logging` module with a custom Formatter. No pip packages needed.

```python
import logging
import json
import time

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "component": getattr(record, "component", record.name),
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["error"] = self.formatException(record.exc_info)
        # add extra fields
        for key in ("job_id", "user_id", "channel", "status", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        return json.dumps(log_entry)

# usage
logger = logging.getLogger("gateway")
handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)
```

**Migration path:** This is a big diff. Replace `print(f"[telegram] ...")` with `logger.info("...", extra={"component": "telegram"})`. Do it component by component:
1. First: add the JSONFormatter and logger setup
2. Then: migrate prints in batches (auth, jobs, telegram, proxy, etc.)
3. Keep entrypoint.sh and shell scripts as plain text (they run before Python starts and are short-lived)

**Toggle:** Add `GOOSECLAW_LOG_FORMAT=json|text` env var. Default to `json` for production, `text` for local dev. The Formatter class checks this.

**Confidence:** HIGH. Python stdlib `logging` module with custom Formatter is standard practice.

### 5. Request Body Size Limits

**Where:** `gateway.py` -- every `do_POST` handler that reads request body
**Current state:** Body is read via `Content-Length` header with no upper bound check.

**Fix:** Add a `MAX_BODY_SIZE` constant and check before reading:

```python
MAX_BODY_SIZE = 1_048_576  # 1 MB

def _read_body(self):
    length = int(self.headers.get("Content-Length", 0))
    if length > MAX_BODY_SIZE:
        self.send_json(413, {"error": "Request body too large"})
        return None
    return self.rfile.read(length)
```

**Where to add:** In the `GatewayHandler` class, as a shared method called by all POST handlers.

**Confidence:** HIGH.

### 6. HTTP Security Headers

**Where:** `gateway.py` lines 858-866 (`SECURITY_HEADERS` dict)
**Current state:** Already has X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, Referrer-Policy, Permissions-Policy.

**Missing:** Strict-Transport-Security (HSTS). Railway provides HTTPS termination, so the app should signal HSTS.

**Fix:** Add to `SECURITY_HEADERS`:
```python
"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
```

Also verify headers are applied to ALL responses (JSON, HTML, proxied). Current code applies them in specific handlers. Should be applied in a central `end_headers` override.

**Confidence:** HIGH.

### 7. Docker Resource Limits

**Where:** `Dockerfile` and deployment config (Railway)
**Level:** Docker/infrastructure, not application code.

**Recommendations:**

| Limit | Value | Where |
|-------|-------|-------|
| Memory | 512MB-1GB | Railway service config or `docker run --memory 1g` |
| CPU | 1-2 cores | Railway service config or `docker run --cpus 1.5` |
| PID limit | 256 | `docker run --pids-limit 256` (prevents fork bombs) |
| No-new-privileges | true | `docker run --security-opt no-new-privileges` |
| Read-only rootfs | Consider | `docker run --read-only --tmpfs /tmp` (may need exceptions) |
| File descriptors | 1024 | `docker run --ulimit nofile=1024:1024` |

**Railway-specific:** Railway uses `railway.json` or `railway.toml` for config. Resource limits are set in the Railway dashboard, not in Dockerfile. Document recommended settings.

**Dockerfile additions:**
```dockerfile
# Drop all capabilities except what's needed
# (Railway may not support --cap-drop, but document for self-hosting)
```

**Confidence:** MEDIUM. Railway's specific resource limit mechanisms need validation.

### 8. Graceful Shutdown Timeouts

**Where:** `gateway.py` lines 9805-9828 (signal handlers), `entrypoint.sh` lines 673-698 (shutdown function)

**Current state:** gateway.py catches SIGTERM/SIGINT and calls `server.shutdown()`. entrypoint.sh catches SIGTERM and sends TERM to child processes.

**Missing:**
- No timeout on `server.shutdown()`. If a handler hangs, shutdown hangs.
- No timeout on goosed subprocess termination.
- No draining of in-flight requests.

**Fix:**
```python
def shutdown(_sig, _frame):
    # Set a hard deadline
    def force_exit():
        time.sleep(30)  # 30s grace period
        os._exit(1)
    threading.Thread(target=force_exit, daemon=True).start()

    # Signal all background threads to stop
    _shutdown_event.set()

    # Shutdown HTTP server (stops accepting new connections)
    threading.Thread(target=server.shutdown, daemon=True).start()

    # Terminate goosed subprocess
    if goosed_process and goosed_process.poll() is None:
        goosed_process.terminate()
        try:
            goosed_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            goosed_process.kill()
```

**Confidence:** HIGH.

### 9. Dependency Lock Files and CVE Scanning

**Where:** `docker/requirements.txt` (currently has loose pins for chromadb)
**Current state:** `chromadb>=1.0.0,<2.0.0` is a wide range. No lock file.

**Fix:**
1. Generate `requirements.lock` with exact versions: `pip freeze > requirements.lock`
2. Use `requirements.lock` in Dockerfile instead of `requirements.txt`
3. Add a CI step for CVE scanning: `pip-audit` or `safety check`

**Dockerfile change:**
```dockerfile
COPY docker/requirements.lock /app/docker/requirements.lock
RUN pip3 install --no-cache-dir -r /app/docker/requirements.lock
```

**Confidence:** HIGH.

## Test Infrastructure Layout

### Current State

Tests exist but are ad-hoc. Four test files, all using `unittest`:

| File | Lines | Coverage |
|------|-------|----------|
| test_gateway.py | 8640 | Job model resolution, config migration, memory, telegram |
| test_server.py | 302 | Basic server functionality |
| test_knowledge.py | 572 | ChromaDB indexing, chunking |
| test_schedule_registry.py | 470 | Cron scheduling, upcoming jobs |

No conftest.py, no pytest config, no CI pipeline, no coverage reporting.

### Recommended Test Structure

```
docker/
  tests/
    conftest.py              # shared fixtures, test helpers
    test_auth.py             # password hashing, login, recovery, sessions
    test_security.py         # injection tests, header checks, body limits
    test_gateway_http.py     # HTTP endpoint tests (GET/POST handlers)
    test_jobs.py             # job creation, scheduling, execution
    test_notifications.py    # notification bus, channel delivery
    test_sessions.py         # session manager, persistence
    test_telegram.py         # telegram bot, pairing, message relay
    test_knowledge.py        # (existing, move here)
    test_schedule.py         # (existing test_schedule_registry.py, move here)
  scripts/
    tests/
      test_secret.sh         # bats tests for secret.sh
      test_job.sh            # bats tests for job.sh
      test_notify.sh         # bats tests for notify.sh
      test_remind.sh         # bats tests for remind.sh
  test_entrypoint.sh         # bats test for entrypoint bootstrap
```

### Test Framework

Use **pytest** (already in the Python environment via pip). It runs unittest-style tests unchanged, so existing tests work immediately. Add a `pyproject.toml` or `pytest.ini` for config:

```ini
[tool:pytest]
testpaths = docker/tests docker
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

For shell script tests, use **bats-core** (Bash Automated Testing System). Install in Dockerfile for dev/CI only.

### Key Test Fixtures Needed

```python
# conftest.py
import pytest
import threading
from unittest.mock import MagicMock

@pytest.fixture
def gateway_handler():
    """Create a mock GatewayHandler for endpoint testing."""
    handler = MagicMock()
    handler.headers = {"Content-Type": "application/json"}
    handler.client_address = ("127.0.0.1", 12345)
    return handler

@pytest.fixture
def temp_data_dir(tmp_path):
    """Create a temporary /data directory structure."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    return tmp_path
```

### Priority Tests for Hardening

| Test | What It Validates | Priority |
|------|-------------------|----------|
| test_scrypt_hash_verify | New password hashing works | P0 |
| test_sha256_migration | Old hashes still verify, get upgraded | P0 |
| test_secret_sh_injection | `secret set key "value with 'quotes'"` doesn't break | P0 |
| test_body_size_limit | 413 returned for oversized requests | P1 |
| test_security_headers_all_responses | Every response type has headers | P1 |
| test_graceful_shutdown_timeout | Shutdown completes within 30s | P1 |
| test_recovery_secret_not_in_logs | Recovery secret not printed to stdout | P1 |
| test_run_script_no_shell_true | _run_script uses explicit /bin/sh | P2 |

## Data Flow: Auth Request (Post-Hardening)

```
Client -> POST /api/auth/login {password: "..."}
    |
    v
GatewayHandler.do_POST()
    |-- auth_limiter.is_allowed(ip) -- 429 if exceeded
    |-- _read_body() -- 413 if too large (NEW)
    |-- get_auth_token() -> (stored_hash, is_hashed)
    |-- verify_token(provided, stored_hash) -- scrypt or SHA-256 fallback (CHANGED)
    |-- if valid: create session cookie, set-cookie header
    |-- if SHA-256 fallback succeeded: rehash with scrypt, update setup.json (NEW)
    |-- 200 {ok: true} or 401 {error: "Invalid password"}
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: Big Bang Logging Migration

**What people do:** Replace all 100+ print statements in one PR.
**Why it's wrong:** Massive diff, impossible to review, high regression risk.
**Do this instead:** Add JSON logger alongside prints. Migrate one component at a time. Each PR is small and reviewable.

### Anti-Pattern 2: Breaking stdlib-only for Password Hashing

**What people do:** pip install bcrypt or argon2-cffi into gateway.py's runtime.
**Why it's wrong:** Violates the core architectural constraint that gateway.py is stdlib-only. Creates a pip dependency for the critical auth path.
**Do this instead:** Use `hashlib.scrypt()` which is stdlib since Python 3.6. It's a proper KDF with salt, cost parameters, and constant-time comparison.

### Anti-Pattern 3: Testing Shell Scripts with Python

**What people do:** Use subprocess.run in pytest to test shell scripts.
**Why it's wrong:** Slow, fragile, can't mock shell internals, poor error messages.
**Do this instead:** Use bats-core for shell script tests. It's purpose-built, supports setup/teardown, and tests run in actual bash.

### Anti-Pattern 4: Applying Docker Limits in Dockerfile

**What people do:** Put resource limits in Dockerfile.
**Why it's wrong:** Dockerfile can't set runtime limits (memory, CPU). Those are `docker run` flags or orchestrator config.
**Do this instead:** Document recommended limits. For Railway, set in dashboard. For self-hosting, provide a `docker-compose.yml` with limits.

## Build Order (Dependency Graph)

```
Phase 1: Security Foundations (no dependencies)
  |-- Password hashing (scrypt) -- gateway.py hash_token/verify_token
  |-- Shell injection fixes -- secret.sh, entrypoint.sh
  |-- Recovery secret leak -- entrypoint.sh line 39
  |-- Request body size limits -- gateway.py _read_body

Phase 2: Infrastructure (depends on Phase 1 for testing)
  |-- Test infrastructure setup -- conftest.py, pytest config
  |-- Security tests -- validates Phase 1 fixes
  |-- Graceful shutdown timeouts -- gateway.py signal handlers

Phase 3: Observability (independent, can parallel with Phase 2)
  |-- JSON logger class -- gateway.py new module
  |-- Migrate prints to logger -- gateway.py component by component
  |-- HTTP security headers audit -- gateway.py SECURITY_HEADERS

Phase 4: Docker Hardening (independent)
  |-- Dependency lock file -- requirements.lock
  |-- CVE scanning -- CI pipeline
  |-- Resource limit documentation -- railway.json / docker-compose.yml
  |-- Remaining test coverage -- shell script tests, e2e tests
```

**Rationale for ordering:**
- Phase 1 first because security fixes are highest risk and highest value. No dependencies.
- Phase 2 next because test infrastructure validates Phase 1 and enables confident changes going forward.
- Phase 3 can run in parallel with Phase 2 since structured logging is additive (doesn't change behavior).
- Phase 4 last because Docker hardening is infrastructure-level and least likely to cause regressions.

## Integration Points

### Internal Boundaries

| Boundary | Communication | Hardening Impact |
|----------|---------------|------------------|
| gateway.py <-> goosed | HTTP proxy on localhost | Body size limits apply to proxied requests |
| gateway.py <-> shell scripts | subprocess (job execution) | shell=True -> explicit /bin/sh -c |
| entrypoint.sh <-> gateway.py | Process management (fork, signals) | Graceful shutdown timeouts |
| gateway.py <-> setup.json | File I/O (JSON) | Password hash format change (scrypt) |
| gateway.py <-> vault.yaml | File I/O via secret.sh | Injection fix in secret.sh |
| gateway.py <-> Railway | HTTP (port 8080), volume (/data) | Resource limits, HSTS header |

### External Services

| Service | Integration | Hardening Notes |
|---------|-------------|-----------------|
| Railway | Deployment platform | Set memory/CPU limits in dashboard |
| Telegram API | HTTPS outbound | Already using urllib.request with HTTPS |
| LLM providers | HTTPS outbound via goosed | No changes needed |
| GitHub (persistence) | HTTPS via git CLI | PAT stored in env vars, not in logs |

## Sources

- [Python hashlib.scrypt documentation](https://docs.python.org/3/library/hashlib.html) -- HIGH confidence
- [Docker Security - OWASP Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) -- HIGH confidence
- [Docker Security Best Practices 2025](https://docs.docker.com/engine/security/) -- HIGH confidence
- Codebase analysis of gateway.py, entrypoint.sh, secret.sh, Dockerfile -- PRIMARY source

---
*Architecture research for: GooseClaw v4.0 Production Hardening*
*Researched: 2026-03-16*

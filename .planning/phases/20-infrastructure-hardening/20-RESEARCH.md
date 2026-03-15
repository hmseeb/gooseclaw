# Phase 20: Infrastructure Hardening - Research

**Researched:** 2026-03-16
**Domain:** Dependency pinning, graceful shutdown, structured logging (Python stdlib)
**Confidence:** HIGH

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| HARD-01 | Dependencies pinned to exact versions with hashes in lock file | pip-compile --generate-hashes pattern, requirements.lock format, Dockerfile integration |
| HARD-02 | CVE scanning configured via GitHub Dependabot or equivalent | .github/dependabot.yml config for pip ecosystem, pip-audit as local alternative |
| HARD-03 | Graceful shutdown has 5-second timeout, force-kills hung processes after grace period | threading.Timer watchdog pattern, existing shutdown handler at gateway.py:9887-9910 |
| HARD-05 | Structured JSON logging replaces print() with stdlib logging + custom JSON formatter | Custom JSONFormatter class (~30 lines), logging.basicConfig setup, incremental migration |
| HARD-06 | Security-sensitive operations logged in structured format first (incremental migration) | 253 print() calls across ~10 categories, security-sensitive subset identified (~30 calls) |
</phase_requirements>

## Summary

This phase hardens three independent infrastructure concerns: supply chain security (dependency pinning + CVE scanning), container lifecycle (graceful shutdown with timeout), and observability (structured JSON logging). All three are additive changes that don't alter existing behavior, just wrap it with better guarantees.

The dependency pinning uses `pip-compile --generate-hashes` from pip-tools to generate a `requirements.lock` file from the existing `docker/requirements.txt`. The Dockerfile changes from `pip install -r requirements.txt` to `pip install --require-hashes -r requirements.lock`. CVE scanning uses GitHub Dependabot (`.github/dependabot.yml`) since the repo is on GitHub, with pip-audit as a local verification option.

The graceful shutdown already exists at gateway.py lines 9887-9910 but has no timeout. The fix adds a `threading.Timer` watchdog that calls `os._exit(1)` after 5 seconds, ensuring the container never hangs on Railway restart. The existing `stop_goosed()` already has its own 5-second terminate/kill cycle (line 7857-7861), so the outer watchdog wraps the entire shutdown sequence.

Structured logging replaces 253 print() calls with stdlib `logging` module calls, using a custom `JSONFormatter(logging.Formatter)` class. HARD-06 requires security-sensitive operations first (auth, config changes, errors), which maps to approximately 30 print() calls. The remaining ~223 calls migrate in a second pass within the same plan.

**Primary recommendation:** Use pip-tools for hash generation, threading.Timer for shutdown watchdog, and a 30-line custom JSONFormatter on stdlib logging. No new runtime dependencies.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `pip-tools` (pip-compile) | 7.x | Generate requirements.lock with hashes | Standard Python dependency locking tool, generates `--require-hashes` compatible output |
| `logging` (stdlib) | Python 3.10 | Structured log output | Already in stdlib, no pip dependency, REQUIREMENTS.md explicitly says no structlog |
| `json` (stdlib) | Python 3.10 | JSON formatting for log records | Paired with logging.Formatter for JSON output |
| `threading.Timer` (stdlib) | Python 3.10 | Shutdown watchdog timer | Simplest stdlib mechanism for delayed force-exit |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pip-audit` | 2.x | Local CVE scanning of requirements | Run locally or in CI to verify no known vulnerabilities |
| GitHub Dependabot | N/A | Automated CVE alerts + PRs | Configured via `.github/dependabot.yml`, monitors requirements.lock |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| pip-compile | pipenv / poetry | Heavier tooling, project already uses plain requirements.txt |
| Dependabot | Renovate | More configurable but more complex, Dependabot is built into GitHub |
| pip-audit | trivy / Snyk | trivy scans Docker image (broader), pip-audit scans Python deps (focused) |
| Custom JSONFormatter | python-json-logger / structlog | Pip dependencies, explicitly ruled out in REQUIREMENTS.md Out of Scope |

**Installation (build-time only):**
```bash
pip install pip-tools  # for generating requirements.lock
pip install pip-audit  # for local CVE scanning
```

## Architecture Patterns

### Pattern 1: Hash-Pinned Lock File
**What:** Two-file system: `requirements.txt` (human-edited, loose pins) and `requirements.lock` (generated, exact pins + hashes)
**When to use:** Always. The lock file is what the Dockerfile installs from.

**File structure:**
```
docker/
  requirements.txt        # human-edited: PyYAML==6.0.2, chromadb>=1.0.0,<2.0.0
  requirements.lock       # generated: every dep pinned with --hash sha256:...
  requirements-dev.txt    # dev deps (pytest, etc.) - not installed in production image
```

**Generation command:**
```bash
pip-compile --generate-hashes --output-file=docker/requirements.lock docker/requirements.txt
```

**Dockerfile change:**
```dockerfile
# Before
COPY docker/requirements.txt /app/docker/requirements.txt
RUN pip3 install --no-cache-dir -r /app/docker/requirements.txt

# After
COPY docker/requirements.txt docker/requirements.lock /app/docker/
RUN pip3 install --no-cache-dir --require-hashes -r /app/docker/requirements.lock
```

### Pattern 2: Shutdown Watchdog Timer
**What:** A threading.Timer that fires `os._exit(1)` after N seconds, wrapping the existing graceful shutdown sequence
**When to use:** In the signal handler, started before any cleanup begins

```python
import os
import threading

def shutdown(_sig, _frame):
    global _job_engine_running, _cron_scheduler_running
    print("[gateway] shutting down...")

    # Watchdog: force-exit after 5 seconds if cleanup hangs
    def _force_exit():
        print("[gateway] shutdown timeout, forcing exit")
        os._exit(1)
    watchdog = threading.Timer(5.0, _force_exit)
    watchdog.daemon = True
    watchdog.start()

    # ... existing cleanup code ...

    watchdog.cancel()  # cleanup finished in time
    print("[gateway] shutdown complete")
```

**Why `os._exit(1)` not `sys.exit(1)`:** `sys.exit()` raises SystemExit which can be caught. `os._exit()` terminates immediately, which is exactly what we want when cleanup is hung.

### Pattern 3: Custom JSON Formatter
**What:** A ~30-line `logging.Formatter` subclass that outputs JSON
**When to use:** Configured at startup, all logging calls automatically use it

```python
import logging
import json
import time

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Include any extra fields passed via logging.info("msg", extra={...})
        for key in ("event", "ip", "user", "detail"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        return json.dumps(log_entry)
```

**Setup at module level in gateway.py:**
```python
import logging

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])

# Per-component loggers
_auth_log = logging.getLogger("auth")
_gateway_log = logging.getLogger("gateway")
_jobs_log = logging.getLogger("jobs")
# etc.
```

### Pattern 4: Incremental Print Migration
**What:** Replace print() calls with logger calls one component at a time, starting with security-sensitive operations
**When to use:** HARD-06 requires security-sensitive first, then remaining

**Migration mapping:**
```python
# Before
print("[auth] password hash upgraded from SHA-256 to PBKDF2")

# After
_auth_log.info("password hash upgraded", extra={"event": "hash_migration"})
```

**Component order for HARD-06 (security-sensitive first):**
1. `auth` (1 call) - password migration, hash failures
2. `session-mgr` (2 calls) - session save/load errors
3. `telegram` (8 calls) - token failures, poll errors
4. `config` (1 call) - config writes
5. Error paths across all components (~20 calls with "error", "fail", "warn")

**Remaining components for full migration (~220 calls):**
6. `gateway` (12 calls)
7. `jobs` (2 calls)
8. `watchers` (2 calls)
9. `watcher` (2 calls)
10. `cron` (1 call)
11. `channels` (1 call)
12. `bot-mgr` (1 call)
13. `memory-writer` (5 calls)
14. All remaining print() calls without bracket prefixes

### Anti-Patterns to Avoid
- **Big-bang logging migration:** Replacing all 253 print() calls in one commit. Too risky for a 9800-line monolith. Go component-by-component.
- **LOG_FORMAT env var toggle:** The research summary mentioned this but it adds complexity. Just switch to logging. Old format is human-readable text, new format is JSON. No toggle needed for a single-user app.
- **Blocking operations in signal handler:** The existing shutdown does I/O (unloading channels, stopping bots). The watchdog timer handles this by force-killing if it takes too long.
- **Using `sys.exit()` in watchdog:** It raises SystemExit which can be caught by exception handlers. Use `os._exit(1)` for guaranteed termination.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Dependency hash generation | Manual `pip hash` per package | `pip-compile --generate-hashes` | Resolves transitive deps automatically, generates all hashes |
| CVE monitoring | Custom vulnerability checking | GitHub Dependabot + pip-audit | Maintained vulnerability databases, automatic PR creation |
| JSON log formatting | Ad-hoc `json.dumps()` in each print replacement | Custom `logging.Formatter` subclass | Single point of control, consistent format, works with all stdlib logging features |
| Lock file maintenance | Manual version pinning in requirements.txt | pip-compile regeneration | Catches transitive dependency changes, verifies resolution |

**Key insight:** The lock file generation and CVE scanning are solved problems with mature tooling. The JSON formatter is simple enough to be stdlib-only (~30 lines) but must be a proper Formatter subclass, not inline json.dumps() scattered through the codebase.

## Common Pitfalls

### Pitfall 1: pip-compile Platform Mismatch
**What goes wrong:** Lock file generated on macOS has different hashes than what Docker (Linux amd64) needs
**Why it happens:** Some packages have platform-specific wheels. Hash for `chromadb` on macOS differs from Linux.
**How to avoid:** Generate the lock file inside a Docker container or use `--platform linux/amd64` flag. Or generate in CI on Linux.
**Warning signs:** Docker build fails with hash mismatch errors

### Pitfall 2: Watchdog Timer Fires During Normal Shutdown
**What goes wrong:** 5-second timeout too short for legitimate cleanup (channel unloading, bot stopping)
**Why it happens:** `_unload_channel` and `_bot_manager.stop_all()` may each take seconds on slow connections
**How to avoid:** Profile actual shutdown time on Railway. 5 seconds is the requirement, but verify it's sufficient for the real workload. `stop_goosed()` alone has a 5-second timeout internally (line 7859).
**Warning signs:** "shutdown timeout, forcing exit" appears in logs during normal restarts

### Pitfall 3: Logging Recursion
**What goes wrong:** A logging call inside the JSON formatter triggers another logging call, creating infinite recursion
**Why it happens:** If the formatter uses any function that itself logs
**How to avoid:** Keep the JSONFormatter pure, no side effects, no calls to anything that might log
**Warning signs:** RecursionError in log output

### Pitfall 4: Dependabot Config Path
**What goes wrong:** Dependabot doesn't detect requirements.lock or detects requirements.txt instead
**Why it happens:** Dependabot for pip looks for standard file names in the configured directory
**How to avoid:** Point Dependabot at the directory containing requirements.txt (the source file), not requirements.lock. Dependabot updates the source, then CI regenerates the lock file.
**Warning signs:** No Dependabot PRs despite known vulnerabilities

### Pitfall 5: print() to stderr vs logging to stderr
**What goes wrong:** After migration, log output order changes because logging and print() have different buffering
**Why it happens:** `print()` goes to stdout (line-buffered), `logging.StreamHandler()` defaults to stderr
**How to avoid:** Configure the StreamHandler to write to `sys.stdout` explicitly, matching current print() behavior. Railway captures both stdout and stderr, but consistency matters for log ordering.
**Warning signs:** Logs appear in wrong order in Railway dashboard

### Pitfall 6: pip-tools Compatibility with pip 25.x
**What goes wrong:** `pip-compile --generate-hashes` fails with AttributeError on pip v25.1+
**Why it happens:** Known issue (github.com/jazzband/pip-tools/issues/2176), cache_clear API changed
**How to avoid:** Pin pip-tools version that's compatible with the pip version in the Docker image. Ubuntu 22.04 ships pip 22.x, so this may not be an issue inside the container. Verify.
**Warning signs:** AttributeError mentioning 'cache_clear' during pip-compile

## Code Examples

### Complete JSONFormatter Implementation
```python
# Source: stdlib logging docs + common community pattern
import logging
import json
import time
import traceback

class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging output."""

    def format(self, record):
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "component": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["error"] = traceback.format_exception(*record.exc_info)[-1].strip()
            entry["traceback"] = self.formatException(record.exc_info)
        # Extra fields for structured events
        for key in ("event", "ip", "user", "detail", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, default=str)
```

### Graceful Shutdown with Watchdog
```python
# Source: Python signal docs + threading.Timer docs
def shutdown(_sig, _frame):
    global _job_engine_running, _cron_scheduler_running

    # Start watchdog FIRST - force exit after 5s
    def _force_exit():
        print("[gateway] shutdown timeout exceeded, forcing exit", flush=True)
        os._exit(1)

    watchdog = threading.Timer(5.0, _force_exit)
    watchdog.daemon = True
    watchdog.start()

    print("[gateway] shutting down...")
    # Existing cleanup (lines 9891-9906)
    threading.Thread(target=server.shutdown, daemon=True).start()
    with _channels_lock:
        channel_names = list(_loaded_channels.keys())
    for ch_name in channel_names:
        _unload_channel(ch_name)
    stop_goosed()
    _remove_pid("goosed")
    _bot_manager.stop_all()
    _session_watcher_running = False
    _job_engine_running = False
    _cron_scheduler_running = False
    stop_watcher_engine()

    watchdog.cancel()
    print("[gateway] shutdown complete")
```

### Dependabot Configuration
```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/docker"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 5
```

### pip-compile Lock File Generation
```bash
# Generate lock file from requirements.txt
pip-compile \
  --generate-hashes \
  --output-file=docker/requirements.lock \
  docker/requirements.txt

# Verify no known CVEs
pip-audit -r docker/requirements.lock
```

### Print-to-Logging Migration Example
```python
# Before (scattered print calls)
print("[auth] password hash upgraded from SHA-256 to PBKDF2")
print(f"[auth] hash migration failed (non-fatal): {e}")
print(f"[session-mgr] warn: could not save {channel} sessions: {e}")

# After (structured logging)
_auth_log = logging.getLogger("auth")
_session_log = logging.getLogger("session-mgr")

_auth_log.info("password hash upgraded", extra={"event": "hash_migration", "detail": "SHA-256 to PBKDF2"})
_auth_log.warning("hash migration failed", exc_info=True, extra={"event": "hash_migration_error"})
_session_log.warning("could not save sessions", extra={"detail": channel}, exc_info=True)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| requirements.txt with loose pins | requirements.lock with hashes via pip-compile | pip-tools mature since 2020 | Supply chain protection |
| No CVE scanning | Dependabot + pip-audit | GitHub native since 2022 | Automated vulnerability alerts |
| print() to stdout | stdlib logging with JSON formatter | Always been available | Structured, parseable, filterable logs |
| No shutdown timeout | threading.Timer watchdog | Common pattern | Container never hangs on restart |

**Deprecated/outdated:**
- `hashin` tool: older hash-pinning approach, pip-compile subsumes it
- `safety` CLI for CVE scanning: pip-audit is the modern replacement (maintained by PyPA)

## Open Questions

1. **pip-compile inside Docker vs locally**
   - What we know: Platform-specific wheels mean hashes differ between macOS and Linux
   - What's unclear: Whether all deps in requirements.txt have universal wheels
   - Recommendation: Generate lock file using `docker run` with the same base image, or add a Makefile target that runs pip-compile inside the build container

2. **5-second shutdown timeout vs stop_goosed() internal timeout**
   - What we know: `stop_goosed()` has its own 5-second wait before SIGKILL (line 7859). The outer watchdog is also 5 seconds.
   - What's unclear: Whether both timeouts run concurrently or sequentially
   - Recommendation: The outer watchdog wraps everything including stop_goosed(). If stop_goosed() hangs for 5s, the outer watchdog fires at the same time. This is correct, but the planner should note that stop_goosed() may get SIGKILL'd by os._exit() before it can do its own SIGKILL on goosed. This is fine because os._exit() kills the whole process tree.

3. **Log output destination (stdout vs stderr)**
   - What we know: Current print() goes to stdout. logging.StreamHandler defaults to stderr.
   - What's unclear: Whether Railway treats stdout and stderr differently
   - Recommendation: Configure StreamHandler to write to sys.stdout to match current behavior

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.x |
| Config file | none (use pyproject.toml or pytest.ini if created in Phase 19) |
| Quick run command | `cd /Users/haseeb/nix-template && python -m pytest docker/tests/ -x -q --timeout=30` |
| Full suite command | `cd /Users/haseeb/nix-template && python -m pytest docker/tests/ -v --timeout=60` |
| Estimated runtime | ~10 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| HARD-01 | requirements.lock has hashes, pip install --require-hashes succeeds | integration | `python -m pytest docker/tests/test_hardening.py::test_requirements_lock_has_hashes -x` | No, Wave 0 gap |
| HARD-02 | .github/dependabot.yml exists with pip ecosystem config | unit (file inspection) | `python -m pytest docker/tests/test_hardening.py::test_dependabot_config -x` | No, Wave 0 gap |
| HARD-03 | Shutdown handler has timeout, force-exits after 5s | unit | `python -m pytest docker/tests/test_hardening.py::test_shutdown_timeout -x` | No, Wave 0 gap |
| HARD-05 | JSONFormatter produces valid JSON with required fields | unit | `python -m pytest docker/tests/test_hardening.py::test_json_formatter -x` | No, Wave 0 gap |
| HARD-06 | Security-sensitive print() calls replaced with logging calls | unit (source inspection) | `python -m pytest docker/tests/test_hardening.py::test_security_logging_migration -x` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `python -m pytest docker/tests/test_hardening.py -x -q --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/test_hardening.py` -- covers HARD-01, HARD-02, HARD-03, HARD-05, HARD-06
- [ ] No new framework install needed (pytest already in requirements-dev.txt)
- [ ] No new conftest fixtures needed (existing conftest.py has gateway_source fixture for source inspection)

## Sources

### Primary (HIGH confidence)
- Codebase analysis: gateway.py shutdown handler (lines 9887-9910), stop_goosed (lines 7853-7863), print() calls (253 total across ~10 component prefixes)
- Codebase analysis: docker/requirements.txt (4 deps: PyYAML, websocket-client, chromadb, mcp)
- Codebase analysis: Dockerfile (Ubuntu 22.04, pip install from requirements.txt, line 52)
- [pip secure installs docs](https://pip.pypa.io/en/stable/topics/secure-installs/) - --require-hashes mode
- [pip-tools docs](https://pip-tools.readthedocs.io/en/latest/) - pip-compile --generate-hashes
- Python stdlib docs: logging.Formatter, threading.Timer, signal module

### Secondary (MEDIUM confidence)
- [Dependabot configuration guide](https://til.simonwillison.net/github/dependabot-python-setup) - pip ecosystem config
- [Python graceful shutdown patterns](https://johal.in/signal-handling-in-python-custom-handlers-for-graceful-shutdowns/) - signal handler + timer patterns
- [pip-tools issue #2176](https://github.com/jazzband/pip-tools/issues/2176) - pip 25.1 compatibility issue

### Tertiary (LOW confidence)
- pip-audit as Dependabot alternative -- not verified against this specific project's CI setup

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all stdlib except pip-tools (build-time only), explicitly constrained by REQUIREMENTS.md
- Architecture: HIGH - shutdown handler, print() calls, and requirements.txt all directly inspected in codebase
- Pitfalls: MEDIUM - platform mismatch and pip-tools compatibility are known issues but not verified against this specific project

**Research date:** 2026-03-16
**Valid until:** 2026-04-16 (stable domain, stdlib-based)

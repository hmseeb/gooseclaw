# Phase 19: Test Infrastructure and Coverage - Research

**Researched:** 2026-03-16
**Domain:** pytest HTTP-level integration testing for Python stdlib HTTP server
**Confidence:** HIGH

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TEST-01 | Gateway HTTP auth endpoints tested (login, session validation, rate limiting, password reset) | HTTP fixture pattern, auth endpoint map in Architecture Patterns |
| TEST-02 | Gateway HTTP setup endpoints tested (provider config, validation, save) | HTTP fixture pattern, setup endpoint map |
| TEST-03 | Gateway HTTP job endpoints tested (create, list, cancel, run, schedule) | HTTP fixture pattern, job endpoint map |
| TEST-04 | Gateway HTTP health endpoints tested (/api/health, /api/health/ready, /api/health/jobs) | HTTP fixture, no-auth-required endpoints |
| TEST-05 | Gateway security headers and CORS tested across all response paths | Header assertion helpers, CORS origin matching |
| TEST-06 | Shell scripts tested (job.sh duration/time parsing, remind.sh flags, notify.sh message handling, secret.sh vault ops) | subprocess-based test pattern for bash scripts |
| TEST-07 | Entrypoint bootstrap tested (directory creation, config generation, env rehydration, provider detection) | subprocess + tmpdir pattern for entrypoint.sh |
| TEST-09 | pytest + requests test infrastructure established with requirements-dev.txt | Standard Stack section, conftest.py fixtures |
</phase_requirements>

## Summary

Phase 19 builds the test infrastructure and writes comprehensive tests for a ~9900-line Python stdlib HTTP gateway monolith, 4 shell scripts, and a ~700-line entrypoint bootstrap script. Phase 18 already created `docker/tests/` with `conftest.py`, `test_auth.py`, and `test_security.py` as scaffolding. These existing tests are source-inspection tests (assert patterns exist in source code), not behavioral HTTP tests. Phase 19 must add the real HTTP-level behavioral tests.

The key architectural decision is already locked: test at HTTP level against a real server on a random port, not function-level mocks on the 400KB monolith. This means spinning up `GatewayHandler` on `localhost:0` in a thread, making real HTTP requests with `requests`, and asserting on status codes, headers, and response bodies. The gateway has a `ThreadingHTTPServer` and `GatewayHandler` class that can be instantiated directly. The tricky part is isolating the server from side effects (goosed subprocess, telegram bots, file I/O to /data), which requires targeted patching of specific global state rather than mocking the entire module.

For shell scripts, the research gap between bats-core vs Python subprocess is resolved: use Python subprocess tests. bats-core would require installing an additional tool in the Docker image, and the shell scripts are thin wrappers around `curl` to the gateway API. Testing them with subprocess from pytest keeps everything in one test runner and one report. For entrypoint.sh, which does filesystem setup and env hydration, subprocess tests with a tmpdir-based DATA_DIR are the right approach.

**Primary recommendation:** Build a `live_gateway` pytest fixture that starts `GatewayHandler` on a random port in a background thread with patched globals (DATA_DIR, goosed process, telegram), returns a `base_url`, and tears down cleanly. All HTTP tests use `requests` against this fixture.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | 8.3.x | Test runner and fixtures | Runs existing unittest tests unchanged, superior fixture system, parametrize support |
| requests | 2.32.x | HTTP client for integration tests | De facto Python HTTP client, clean API for testing real HTTP endpoints |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest-cov | 6.x | Coverage reporting | Run with `--cov=docker/gateway` to measure coverage |
| pytest-timeout | 2.3.x | Per-test timeouts | Prevent hung tests from blocking CI (server startup failures) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| requests | urllib.request (stdlib) | requests is cleaner API, worth the dev-only dependency |
| bats-core for shell tests | subprocess in pytest | subprocess keeps one test runner, one report, one CI step |
| httpx | requests | httpx has async support we don't need, requests is simpler |

**Installation:**
```bash
# requirements-dev.txt
pytest>=8.3.0,<9.0.0
requests>=2.32.0,<3.0.0
pytest-cov>=6.0.0,<7.0.0
pytest-timeout>=2.3.0,<3.0.0
```

## Architecture Patterns

### Recommended Test Structure
```
docker/
  tests/
    __init__.py              # (exists)
    conftest.py              # shared fixtures: live_gateway, auth helpers, tmp data dirs
    test_auth.py             # (exists, source-inspection) + HTTP auth behavior tests
    test_security.py         # (exists, source-inspection) + HTTP header/CORS tests
    test_setup.py            # setup wizard endpoints: config, validate, save, models
    test_jobs.py             # job CRUD: create, list, cancel, run, schedule
    test_health.py           # health endpoints: /api/health, /ready, /jobs
    test_shell_scripts.py    # subprocess tests for job.sh, remind.sh, notify.sh, secret.sh
    test_entrypoint.py       # subprocess tests for entrypoint.sh bootstrap
```

### Pattern 1: Live Gateway Fixture
**What:** Start a real GatewayHandler on localhost:0 (OS-assigned port) in a background thread, patch globals for isolation, return base_url.
**When to use:** Every HTTP endpoint test.
**Example:**
```python
import threading
import time
from http.server import ThreadingHTTPServer
import requests

@pytest.fixture(scope="module")
def live_gateway(tmp_path_factory):
    """Start gateway HTTP server on random port with isolated state."""
    import docker.gateway as gw

    # Patch globals for test isolation
    data_dir = tmp_path_factory.mktemp("data")
    config_dir = data_dir / "config"
    config_dir.mkdir()
    secrets_dir = data_dir / "secrets"
    secrets_dir.mkdir(mode=0o700)

    original_data_dir = gw.DATA_DIR
    original_config_dir = gw.CONFIG_DIR
    gw.DATA_DIR = str(data_dir)
    gw.CONFIG_DIR = str(config_dir)

    server = ThreadingHTTPServer(("127.0.0.1", 0), gw.GatewayHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # Wait for server to be ready
    for _ in range(50):
        try:
            requests.get(f"{base_url}/api/health", timeout=0.1)
            break
        except requests.ConnectionError:
            time.sleep(0.05)

    yield base_url

    server.shutdown()
    gw.DATA_DIR = original_data_dir
    gw.CONFIG_DIR = original_config_dir
```

### Pattern 2: Authenticated Request Helper
**What:** Helper fixture that creates a session cookie or Basic Auth header for authenticated endpoint tests.
**When to use:** Any endpoint behind `check_auth()`.
**Example:**
```python
@pytest.fixture
def auth_session(live_gateway, tmp_data_dir_with_password):
    """Get an authenticated session cookie for the live gateway."""
    resp = requests.post(
        f"{live_gateway}/api/auth/login",
        json={"password": "testpassword"},
    )
    assert resp.status_code == 200
    return {"Cookie": resp.headers.get("Set-Cookie", "").split(";")[0]}
```

### Pattern 3: Shell Script Subprocess Tests
**What:** Run shell scripts via subprocess with controlled env vars, assert on stdout/stderr/exit code.
**When to use:** TEST-06 and TEST-07.
**Example:**
```python
import subprocess

def test_job_sh_parse_duration():
    """job.sh parse_duration converts '1h30m' to 5400 seconds."""
    result = subprocess.run(
        ["bash", "-c", 'source /path/to/job.sh; parse_duration "1h30m"'],
        capture_output=True, text=True, timeout=5
    )
    assert result.stdout.strip() == "5400"
```

### Pattern 4: Security Header Assertion
**What:** Reusable assertion that checks all SECURITY_HEADERS are present on a response.
**When to use:** TEST-05.
**Example:**
```python
EXPECTED_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

def assert_security_headers(response):
    for header, value in EXPECTED_SECURITY_HEADERS.items():
        assert response.headers.get(header) == value, f"Missing or wrong {header}"
```

### Anti-Patterns to Avoid
- **Mocking the handler internals:** Do NOT patch `do_GET`, `do_POST`, or individual handler methods. Test at HTTP level. The monolith's internal structure changes frequently.
- **Importing gateway at module level without patching:** gateway.py has module-level side effects (creates RateLimiter instances, reads env vars). Import inside fixtures, after env setup.
- **Testing with `unittest.TestCase` in new files:** Use plain pytest functions/classes. The existing unittest tests work fine, but new tests should use pytest's simpler assert style.
- **Shared mutable state between tests:** Each test module should get its own `live_gateway` instance (module-scoped fixture) or at minimum reset rate limiters between tests.
- **Forgetting to reset rate limiters:** The gateway has `auth_limiter` (5 req/60s). Tests hitting auth endpoints will get 429s unless the limiter is reset between tests.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP test client | Custom urllib wrapper | `requests` library | Cookie handling, session persistence, clean API |
| Test server lifecycle | Manual thread management | pytest fixture with `ThreadingHTTPServer` | Automatic cleanup, port allocation, scope control |
| Coverage reporting | Manual counting | `pytest-cov` | Branch coverage, HTML reports, CI integration |
| Test timeouts | `signal.alarm` hacks | `pytest-timeout` | Cross-platform, per-test configuration |
| Shell script test framework | Custom bash test runner | subprocess from pytest | One runner, one report, unified CI |

**Key insight:** The existing codebase already has `ThreadingHTTPServer` and `GatewayHandler` that can be instantiated directly. The test infrastructure is just pytest fixtures wrapping what the app already does.

## Common Pitfalls

### Pitfall 1: Module-Level Side Effects in gateway.py
**What goes wrong:** Importing `gateway` triggers module-level code that reads env vars, creates global objects, and sets up data directories.
**Why it happens:** gateway.py is a monolith, not a library. It wasn't designed for import-time isolation.
**How to avoid:** Set required env vars (DATA_DIR, PORT) BEFORE importing gateway. Use `os.environ` patching in conftest.py. Consider a module-scoped fixture that does the import.
**Warning signs:** Tests fail with FileNotFoundError for /data/config or similar paths.

### Pitfall 2: Rate Limiter Exhaustion
**What goes wrong:** After 5 auth endpoint tests, all subsequent auth tests get 429 Too Many Requests.
**Why it happens:** `auth_limiter` is a module-level singleton with 5 req/60s limit. Tests run much faster than 60 seconds.
**How to avoid:** Reset or replace the rate limiter between test modules. Add a fixture that patches `auth_limiter.requests` dict to clear state.
**Warning signs:** First 5 auth tests pass, rest return 429.

### Pitfall 3: Port Conflicts in Parallel Test Runs
**What goes wrong:** Two test modules try to bind the same port.
**Why it happens:** Using a fixed port instead of port 0.
**How to avoid:** Always use `("127.0.0.1", 0)` for the server address. The OS assigns a free port.
**Warning signs:** `OSError: [Errno 48] Address already in use`.

### Pitfall 4: Server Thread Not Cleaning Up
**What goes wrong:** Test process hangs after all tests complete. Or leftover server threads interfere with subsequent test modules.
**Why it happens:** `server.serve_forever()` blocks, and if `server.shutdown()` isn't called, the thread persists.
**How to avoid:** Use daemon threads AND call `server.shutdown()` in fixture teardown (after yield). Double insurance.
**Warning signs:** pytest hangs at the end, or CI times out.

### Pitfall 5: Testing Shell Scripts That Call curl to localhost
**What goes wrong:** Shell script tests fail because the scripts try to curl the gateway on port 8080, but no server is running.
**Why it happens:** job.sh, remind.sh, notify.sh all curl `http://127.0.0.1:${PORT}/api/...`.
**How to avoid:** For shell scripts, test the pure-bash helper functions (parse_duration, parse_time) separately via `source` + function call. For the API-calling parts, either start a live_gateway on the PORT the script expects, or just test the argument parsing and leave API integration to the HTTP endpoint tests.
**Warning signs:** `curl: (7) Failed to connect to 127.0.0.1 port 8080`.

### Pitfall 6: goosed Subprocess Starting During Tests
**What goes wrong:** Tests trigger `start_goosed()` or `is_configured()` returns True, causing the gateway to try spawning goosed.
**Why it happens:** If setup.json exists with a provider config, the gateway's `main()` path starts goosed.
**How to avoid:** Don't call `main()`. Just instantiate `ThreadingHTTPServer` + `GatewayHandler` directly. Patch `goosed_process` to None. Keep test setup.json minimal (no provider, or patch `is_configured` to return False).
**Warning signs:** Tests fail with subprocess errors about missing goosed binary.

## Code Examples

### conftest.py: Core Fixtures
```python
import os
import sys
import json
import hashlib
import threading
import time
import pytest

# Ensure docker/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="module")
def gateway_module():
    """Import gateway with test-safe env vars. Module-scoped to avoid re-import overhead."""
    # Set required env vars before import
    os.environ.setdefault("DATA_DIR", "/tmp/gooseclaw-test")
    os.environ.setdefault("PORT", "0")
    import gateway
    return gateway


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create isolated /data structure for a test."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(mode=0o700)
    (secrets_dir / "vault.yaml").touch()
    return tmp_path


@pytest.fixture
def tmp_data_with_password(tmp_data_dir):
    """tmp_data_dir with a PBKDF2-hashed password in setup.json."""
    # We'll compute this at test time using the gateway module
    # For now, create a SHA-256 hash (legacy format) for migration tests
    sha256_hash = hashlib.sha256(b"testpassword").hexdigest()
    setup = {"web_auth_token_hash": sha256_hash, "setup_complete": True}
    (tmp_data_dir / "config" / "setup.json").write_text(json.dumps(setup))
    return tmp_data_dir


@pytest.fixture
def reset_rate_limiters(gateway_module):
    """Clear all rate limiter state between tests."""
    gateway_module.auth_limiter.requests.clear()
    gateway_module.api_limiter.requests.clear()
    gateway_module.notify_limiter.requests.clear()
```

### live_gateway Fixture (Full)
```python
from http.server import ThreadingHTTPServer
import requests as req_lib

@pytest.fixture(scope="module")
def live_gateway(tmp_path_factory, gateway_module):
    """Start a real GatewayHandler on a random port."""
    gw = gateway_module
    data_dir = tmp_path_factory.mktemp("gateway_data")
    (data_dir / "config").mkdir()
    (data_dir / "secrets").mkdir(mode=0o700)
    (data_dir / "secrets" / "vault.yaml").touch()
    (data_dir / "sessions").mkdir()
    (data_dir / "channels").mkdir()

    # Patch module globals
    saved = {
        "DATA_DIR": gw.DATA_DIR,
        "CONFIG_DIR": gw.CONFIG_DIR,
    }
    gw.DATA_DIR = str(data_dir)
    gw.CONFIG_DIR = str(data_dir / "config")

    server = ThreadingHTTPServer(("127.0.0.1", 0), gw.GatewayHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    # Wait for ready
    for _ in range(50):
        try:
            req_lib.get(f"{base_url}/api/health", timeout=0.1)
            break
        except Exception:
            time.sleep(0.05)

    yield base_url

    server.shutdown()
    for k, v in saved.items():
        setattr(gw, k, v)
```

### Health Endpoint Test Example
```python
import requests

def test_health_returns_200(live_gateway):
    resp = requests.get(f"{live_gateway}/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "gooseclaw"

def test_health_ready_returns_503_when_not_configured(live_gateway):
    resp = requests.get(f"{live_gateway}/api/health/ready")
    # No goosed running in test, should be 503
    assert resp.status_code == 503
```

### Shell Script Parse Function Test Example
```python
import subprocess

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")

def test_parse_duration_hours_minutes():
    result = subprocess.run(
        ["bash", "-c", f'source {SCRIPTS_DIR}/job.sh && parse_duration "1h30m"'],
        capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "5400"

def test_parse_duration_invalid():
    result = subprocess.run(
        ["bash", "-c", f'source {SCRIPTS_DIR}/job.sh && parse_duration "xyz"'],
        capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| unittest.TestCase | pytest plain classes/functions | Stable for years | Simpler assertions, better fixtures, same test runner |
| Mock-heavy unit tests | HTTP-level integration tests | Architecture decision for this project | Tests real behavior, survives refactoring |
| Fixed port test servers | Port 0 (OS-assigned) | Standard practice | No port conflicts, parallelizable |
| Source-inspection tests | Behavioral HTTP tests | Phase 18 -> Phase 19 evolution | Phase 18 created scaffolding, Phase 19 adds real behavior tests |

**Note on existing tests:**
- `docker/tests/test_auth.py` and `test_security.py` from Phase 18 use source-code inspection (checking that patterns exist in gateway.py source). These are valid smoke tests but NOT behavioral tests. Phase 19 adds real HTTP tests alongside them.
- `docker/test_gateway.py` (375KB!) uses unittest with heavy mocking. It covers job model resolution, config migration, etc. These stay as-is, Phase 19 does not refactor them.

## Open Questions

1. **Gateway import side effects**
   - What we know: gateway.py reads env vars and creates globals on import. The module is 9900 lines.
   - What's unclear: Exactly which module-level operations will cause issues in test context. May need targeted patches.
   - Recommendation: Start with a simple import test. If it fails, add env var setup and patches incrementally. The `live_gateway` fixture approach avoids calling `main()`, which is where most side effects live.

2. **Shell script sourcing for function tests**
   - What we know: job.sh and remind.sh define reusable functions (parse_duration, parse_time) that can be tested by sourcing the script.
   - What's unclear: Whether sourcing the full script triggers side effects (set -euo pipefail, env var reads, etc.).
   - Recommendation: Source the script and call individual functions. The `set -euo pipefail` at the top is fine for sourcing. The scripts don't execute anything at source time (they use argument-based dispatch at the bottom).

3. **Coverage target**
   - What we know: No coverage baseline exists. test_gateway.py covers internal functions, not HTTP endpoints.
   - What's unclear: What coverage percentage is achievable in this phase.
   - Recommendation: Don't set a percentage target. Focus on covering every HTTP endpoint and every shell script command path. Measure coverage at the end for baseline.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.x |
| Config file | None yet, create `docker/tests/pytest.ini` or `pyproject.toml` in root |
| Quick run command | `cd docker && python -m pytest tests/ -x -q --timeout=30` |
| Full suite command | `cd docker && python -m pytest tests/ -v --timeout=60 --cov=. --cov-report=term-missing` |
| Estimated runtime | ~15-30 seconds (HTTP tests with real server startup) |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TEST-09 | pytest infrastructure with fixtures | infrastructure | `cd docker && python -m pytest tests/conftest.py --collect-only` | Partial (conftest.py exists, needs HTTP fixtures) |
| TEST-01 | Auth endpoints (login, session, rate limit, recover) | integration | `cd docker && python -m pytest tests/test_auth.py -x` | Partial (source inspection only) |
| TEST-05 | Security headers and CORS | integration | `cd docker && python -m pytest tests/test_security.py -x` | Partial (source inspection only) |
| TEST-02 | Setup endpoints (config, validate, save) | integration | `cd docker && python -m pytest tests/test_setup.py -x` | No |
| TEST-03 | Job endpoints (create, list, cancel, run) | integration | `cd docker && python -m pytest tests/test_jobs.py -x` | No |
| TEST-04 | Health endpoints (health, ready, jobs) | integration | `cd docker && python -m pytest tests/test_health.py -x` | No |
| TEST-06 | Shell scripts (job.sh, remind.sh, notify.sh, secret.sh) | integration | `cd docker && python -m pytest tests/test_shell_scripts.py -x` | No |
| TEST-07 | Entrypoint bootstrap | integration | `cd docker && python -m pytest tests/test_entrypoint.py -x` | No |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd docker && python -m pytest tests/ -x -q --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~15-30 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/requirements-dev.txt` -- pytest, requests, pytest-cov, pytest-timeout
- [ ] `docker/tests/conftest.py` -- REPLACE existing with HTTP-level fixtures (live_gateway, auth helpers, rate limiter reset)
- [ ] `docker/tests/test_setup.py` -- covers TEST-02
- [ ] `docker/tests/test_jobs.py` -- covers TEST-03
- [ ] `docker/tests/test_health.py` -- covers TEST-04
- [ ] `docker/tests/test_shell_scripts.py` -- covers TEST-06
- [ ] `docker/tests/test_entrypoint.py` -- covers TEST-07
- [ ] pytest configuration (pytest.ini or pyproject.toml section)

## Sources

### Primary (HIGH confidence)
- Codebase analysis: gateway.py (9917 lines), existing test files (conftest.py, test_auth.py, test_security.py, test_gateway.py, test_server.py, test_knowledge.py, test_schedule_registry.py)
- Codebase analysis: shell scripts (job.sh, remind.sh, notify.sh, secret.sh), entrypoint.sh
- Python stdlib: http.server.ThreadingHTTPServer, threading module
- Architecture decision from STATE.md: "Tests at HTTP level (real server on random port), not function-level mocks on 400KB monolith"

### Secondary (MEDIUM confidence)
- pytest documentation (training data, stable API): fixtures, scope, parametrize, tmp_path
- requests documentation (training data, stable API): HTTP client patterns

### Tertiary (LOW confidence)
- None. This phase uses well-established tools with stable APIs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - pytest and requests are stable, well-known tools. No version surprises expected.
- Architecture: HIGH - HTTP-level testing pattern is already decided and validated by Phase 18 scaffolding. GatewayHandler and ThreadingHTTPServer are observable in the codebase.
- Pitfalls: HIGH - Based on direct codebase analysis (rate limiters at lines 147-149, module-level globals, goosed subprocess management).

**Research date:** 2026-03-16
**Valid until:** 2026-04-16 (stable tools, no fast-moving dependencies)

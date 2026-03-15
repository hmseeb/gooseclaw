# Stack Research: Production Hardening

**Domain:** Security, hardening, and testing additions for Python Docker AI agent
**Researched:** 2026-03-16
**Confidence:** HIGH

## Existing Stack (DO NOT change)

- Python 3.10 (Ubuntu 22.04 default)
- Python stdlib HTTP server (gateway.py, ~400KB)
- Docker on Ubuntu 22.04
- Railway deployment with volumes
- ChromaDB (pip-installed, requirements.txt)
- unittest + unittest.mock (existing test framework)

## Stack Additions

### Security

| Library | Version | Why | Confidence |
|---------|---------|-----|------------|
| `hashlib.pbkdf2_hmac()` (stdlib) | Python 3.10 built-in | Password hashing upgrade from SHA-256. OWASP-approved with 600K iterations. Stays stdlib-only. | HIGH |
| `shlex.quote()` (stdlib) | Python 3.10 built-in | Shell argument escaping for entrypoint.sh and secret.sh variable interpolation | HIGH |
| `subprocess.run(..., shell=False)` (stdlib) | Python 3.10 built-in | Replace `shell=True` with list-based command execution in `_run_script` | HIGH |

**Decision: PBKDF2 vs scrypt vs argon2**

All three are viable. Recommendation: **PBKDF2** via `hashlib.pbkdf2_hmac()`.
- Stays stdlib-only (no pip dependency)
- OWASP-approved for password hashing
- Available since Python 3.4
- 600K iterations with SHA-256 is the OWASP 2023 minimum
- scrypt is also stdlib but has more complex parameter tuning
- argon2 requires pip install (argon2-cffi), breaks stdlib-only principle for gateway.py

### Logging

| Library | Version | Why | Confidence |
|---------|---------|-----|------------|
| `logging` (stdlib) | Python 3.10 built-in | Structured JSON logging via custom Formatter class. No pip dependency needed. Replace ~252 print() calls incrementally. | HIGH |
| `json` (stdlib) | Python 3.10 built-in | JSON formatter output | HIGH |

**NOT recommended:** structlog (25.5.0) or python-json-logger. Both are pip dependencies. A custom 30-line JSON formatter on top of stdlib `logging` achieves the same result for this use case.

### Testing

| Library | Version | Why | Confidence |
|---------|---------|-----|------------|
| `pytest` | 8.3.x | Test runner. Runs existing unittest tests unchanged. Better fixtures, parametrize, clearer output. Dev-only dependency. | HIGH |
| `requests` | 2.32.5 | HTTP client for integration tests against gateway.py endpoints. Dev-only. | HIGH |
| `pytest-cov` | 6.x | Coverage reporting. Dev-only. | HIGH |

**NOT recommended:**
- `httpx` -- overkill for testing a stdlib HTTP server, requests is simpler
- `pytest-httpserver` -- we're testing OUR server, not mocking external ones
- `bats-core` -- shell script testing adds complexity. Python subprocess tests are sufficient

### Docker/Hardening

| Tool | Why | Confidence |
|------|-----|------------|
| `HEALTHCHECK` directive | Already exists in Dockerfile, verify timeout values | HIGH |
| Resource limit docs | Railway sets via dashboard, document recommended values | HIGH |
| `requirements.lock` with hashes | Pin exact versions, `pip install --require-hashes` | HIGH |
| GitHub Dependabot config | `.github/dependabot.yml` for CVE scanning | MEDIUM |

## What NOT to Add

| Library | Why Not |
|---------|---------|
| argon2-cffi | Breaks stdlib-only constraint for gateway.py |
| structlog | Overkill for this use case, stdlib logging + JSON formatter is enough |
| prometheus-client | Nice-to-have, not table stakes for single-user self-hosted app |
| OpenTelemetry | Way too heavy for this deployment model |
| Sentry | External dependency, not self-hosted friendly |
| bats-core | Extra tooling, Python subprocess tests cover shell scripts fine |

## Integration Notes

1. **Password hashing**: PBKDF2 goes directly into gateway.py `hash_token()` and `verify_token()`. Lazy migration detects old SHA-256 hashes by format (bare hex vs `$pbkdf2$` prefix).

2. **Structured logging**: Custom `JsonFormatter(logging.Formatter)` class in gateway.py. Migrate print() to logging calls incrementally. Start with security-sensitive paths (auth, setup, errors).

3. **Testing**: `requirements-dev.txt` for pytest + requests + pytest-cov. Tests run against gateway.py started in a subprocess, hitting real HTTP endpoints.

4. **Shell injection fixes**: `shlex.quote()` in bash scripts, `shell=False` with `shlex.split()` in Python. Job commands need special handling (user-defined, may need shell features).

## Build Order

1. Security fixes first (injection, hashing, secret leak) -- independent, parallelizable
2. Test infrastructure (pytest setup, dev requirements) -- validates security fixes
3. Hardening (logging, headers, body limits, shutdown timeout) -- lower priority
4. Docker/CI (lock file, dependabot, resource docs) -- last, lowest risk

---
*Researched: 2026-03-16*

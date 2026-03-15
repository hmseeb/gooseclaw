# Project Research Summary

**Project:** GooseClaw v4.0 Production Hardening
**Domain:** Security hardening and test coverage for self-hosted Docker AI agent platform
**Researched:** 2026-03-16
**Confidence:** HIGH

## Executive Summary

GooseClaw is a single-user self-hosted AI agent platform running a Python stdlib HTTP gateway (~9800 lines) inside Docker on Railway. The v4.0 milestone is a hardening sprint, not a feature sprint. Research confirms the system has several active security vulnerabilities (not theoretical ones) that must be fixed before this can be called production-ready: SHA-256 password storage with no salt, shell injection in 3 separate locations, recovery secret leaking to container stdout on first boot, and no request body size limits. None of these require architectural changes, just surgical fixes to existing files.

The stack decision is resolved: stay stdlib-only for gateway.py. This means PBKDF2 via `hashlib.pbkdf2_hmac()` (600K iterations, 16-byte random salt) for password hashing, `os.environ`-based data passing for injection fixes across shell scripts, and a custom `JSONFormatter` on top of stdlib `logging` for structured logs. No new pip dependencies for the runtime path. The only new pip packages are dev-only: pytest, requests, pytest-cov for the test infrastructure.

The main risk is the migration path, not the fixes themselves. The password hash format must be versioned (`$pbkdf2$salt$hash` prefix) and `verify_token()` must support lazy migration from bare SHA-256. If this is skipped or tested only against freshly-generated hashes rather than pre-existing ones on disk, deployed users get locked out on upgrade. The second risk is testing the monolith: gateway.py is 400KB and not designed for unit testing. The correct approach is HTTP-level integration tests against a real server, not function-level tests patching module globals.

## Key Findings

### Recommended Stack

The existing stack stays intact. All security fixes use Python stdlib, maintaining the gateway.py stdlib-only constraint. New additions are exclusively dev-only. See [STACK.md](STACK.md) for full details.

**Core technologies:**
- `hashlib.pbkdf2_hmac()` (stdlib): password hashing — OWASP-approved, 600K iterations, no pip dependency, replaces SHA-256
- `hashlib.scrypt()` (stdlib): alternative KDF if PBKDF2 proves too slow on Railway CPU — memory-hard, slightly stronger
- `logging` + `json` (stdlib): structured logging — custom JSONFormatter replaces 252+ print() calls incrementally
- `shlex.quote()` + `os.environ` (stdlib): shell injection prevention — mechanical fix across secret.sh, entrypoint.sh, gateway.py
- `pytest` 8.3.x (dev-only): test runner — runs existing unittest tests unchanged, better output and fixtures
- `requests` 2.32.5 (dev-only): HTTP client for integration tests against a running gateway instance
- `pytest-cov` 6.x (dev-only): coverage reporting

### Expected Features

See [FEATURES.md](FEATURES.md) for full prioritization matrix and dependency graph.

**Must have (table stakes, v4.0):**
- Fix shell injection in secret.sh, entrypoint.sh, gateway.py `_run_script` — active RCE vectors
- Upgrade password hashing from SHA-256 to PBKDF2 with lazy migration — crackable in milliseconds on GPU
- Remove recovery secret from container stdout logs — credential leak on every first boot
- Add request body size limits (1MB max, 413 response) — DoS via memory exhaustion
- Pin dependency versions with exact hashes in requirements.lock — supply chain risk
- Add graceful shutdown timeout (30s hard deadline, SIGKILL after 10s for goosed) — container hangs on Railway restart
- Complete HTTP security headers (add HSTS, verify headers applied to ALL response types)
- Gateway HTTP endpoint tests covering auth, setup, jobs, health — zero coverage on core paths
- Shell script tests for secret.sh, job.sh, remind.sh, notify.sh — untested user-facing scripts
- Entrypoint bootstrap tests — untested first-boot and upgrade logic
- CVE scanning in CI via trivy or pip-audit — no vulnerability monitoring

**Should have (v4.x after validation):**
- Structured JSON logging (migrate 252+ print() calls component by component with LOG_FORMAT toggle)
- CSRF protection on state-changing POST endpoints
- Audit logging (config changes, auth events, vault operations) — reuses JSON log format
- Security audit endpoint (`/api/security/audit`) returning pass/fail per check
- End-to-end integration tests (boot container, complete setup, verify goose starts)
- Container health dashboard enhancements

**Defer (v5+):**
- Secrets rotation support — complex, provider-specific flows
- Dependency auto-update bot (Dependabot/Renovate)

**Anti-features (do not build):**
- TLS termination in container — Railway handles it at load balancer
- WAF inside container — wrong layer for a single-user stdlib server
- Encrypted vault at rest — key management problem negates benefit, file permissions are sufficient
- MFA — overkill for single-user PaaS deployment
- Comprehensive RBAC — no multi-user use case exists yet

### Architecture Approach

All hardening integrates in-place within the existing monolith. No new modules, no restructuring. gateway.py gets surgical changes to `hash_token()`, `verify_token()`, body reading, `SECURITY_HEADERS`, and signal handlers. secret.sh and entrypoint.sh get mechanical variable-passing fixes. See [ARCHITECTURE.md](ARCHITECTURE.md) for exact line references and drafted implementation code for every fix.

**Major components and hardening touchpoints:**
1. `gateway.py` (~9800 lines) — password hashing (lines 1086-1095), body limits (new `_read_body()` helper), security headers (lines 858-866), shutdown (lines 9805-9828), structured logging (all 252+ print() calls)
2. `entrypoint.sh` (~700 lines) — recovery secret leak (line 39), password reset injection (lines 59-73), SHA-256 reset path (line 66)
3. `secret.sh` (~124 lines) — inline Python injection (lines 37-116, all 4 CRUD commands via `'$VARIABLE'` pattern)
4. `Dockerfile` + CI — requirements.lock generation, CVE scanning integration, test runner setup
5. Test infrastructure (new) — `docker/tests/` directory with conftest.py, HTTP-level test fixtures, focused test files per concern

### Critical Pitfalls

See [PITFALLS.md](PITFALLS.md) for full details, recovery strategies, and "looks done but isn't" checklist.

1. **Password migration locks out existing users** — version new hashes with `$pbkdf2$` prefix, implement lazy migration in `verify_token()`, test with actual SHA-256 hashes from a live setup.json not freshly generated ones
2. **stdlib constraint rules out argon2/bcrypt** — use `hashlib.pbkdf2_hmac()` only, never add argon2-cffi or bcrypt even though Dockerfile has other pip packages; the runtime stdlib constraint on gateway.py is firm
3. **Shell injection via `shell=True` in job execution** — switch to explicit `["/bin/sh", "-c", command]` for job runner, add command validation on job creation; naively setting `shell=False` breaks legitimate pipe commands
4. **Inline Python injection in bash scripts** — convert every `'$VARIABLE'` pattern in python3 -c calls to `os.environ` reads; this is a mechanical grep-and-fix, job.sh cmd_create already shows the correct pattern
5. **Fragile test mocks on 400KB monolith** — test at HTTP level (real server on random port, real HTTP requests) not function level (patching module globals); one focused test file per concern, not one giant test_gateway.py

## Implications for Roadmap

Based on the dependency graph in FEATURES.md and build order in ARCHITECTURE.md, a 4-phase structure is right. Security fixes have no upstream dependencies and must come first. Everything else flows from there.

### Phase 1: Security Foundations

**Rationale:** All security fixes are independent of each other and have no upstream dependencies. They represent the highest risk (active RCE and credential leak vectors) at the lowest implementation cost. Auth must work correctly before anything else can be tested or built. These are the fixes most likely to be discovered and exploited.
**Delivers:** Production-safe auth with lazy migration, no injection vectors across 3 files, no credential leaks on startup, DoS protection via body limits, supply chain integrity via pinned hashes, complete security headers
**Addresses:** Shell injection (3 locations), PBKDF2 password hashing with lazy SHA-256 migration, recovery secret leak removal, request body limits (1MB), dependency pinning (requirements.lock), HSTS header
**Avoids:** Pitfall 1 (migration lockout), Pitfall 2 (stdlib constraint), Pitfall 3 (job injection), Pitfall 4 (bash injection), Pitfall 5 (secret in logs)

### Phase 2: Test Infrastructure

**Rationale:** Tests must be written against the fixed code, not the broken code. Establishing the correct testing pattern (HTTP-level, not function-level) before writing hundreds of tests prevents the fragile-mock technical debt trap. The test suite is the safety net for Phases 3 and 4.
**Delivers:** pytest setup with pyproject.toml/pytest.ini, conftest.py with HTTP-level fixtures (real server on random port), test_auth.py, test_security.py, test_gateway_http.py, test_jobs.py, shell script test framework, entrypoint bootstrap tests
**Uses:** pytest 8.3.x, requests 2.32.5, pytest-cov (all dev-only, requirements-dev.txt)
**Avoids:** Pitfall 6 (fragile test mocks), monolith testing anti-patterns (HTTP-level over function-level)
**Research flag:** Standard patterns, no research phase needed

### Phase 3: Observability and Defense-in-Depth

**Rationale:** Structured logging is additive (doesn't change behavior) but is a large diff touching all 252+ print() calls. Having Phase 2 tests in place means regressions are caught. CSRF protection and audit logging add defense-in-depth beyond the Phase 1 critical fixes. Structured logging must precede audit logging (same JSON format).
**Delivers:** JSONFormatter class with `GOOSECLAW_LOG_FORMAT` env var toggle, incremental print() migration component-by-component, CSRF tokens on state-changing endpoints, audit logging to /data/audit.log, graceful shutdown with 30s hard deadline
**Implements:** `logging.Formatter` subclass in gateway.py, `_shutdown_event` + timeout thread in signal handlers, append-only audit log
**Avoids:** Big bang logging migration anti-pattern (one component at a time), security headers blocking setup.html (test CSP against setup.html before deploying)

### Phase 4: Docker Hardening and CI

**Rationale:** Infrastructure changes are lowest risk and don't affect runtime behavior. CVE scanning has no correctness dependencies on code changes. Docker resource documentation is pure docs. E2e tests validate the whole system and require all prior phases to exist.
**Delivers:** requirements.lock with pip hash verification, trivy/pip-audit CVE scanning in CI with result caching, Railway resource limit documentation, docker-compose.yml with recommended limits for self-hosting, e2e integration tests, security audit endpoint
**Uses:** GitHub Actions or Railway CI config, Docker build pipeline
**Avoids:** CVE scan blocking deployment (cache results, only re-scan on Dockerfile/requirements changes)
**Research flag:** Railway-specific CI configuration syntax needs validation during planning

### Phase Ordering Rationale

- Security fixes first: no dependencies, highest risk, auth must work before anything else is testable
- Tests before observability: logging migration is a large diff, tests catch regressions; also establishes patterns before volume lands
- Structured logging before audit logging: audit log reuses JSON format, must exist first
- Docker hardening last: infrastructure changes don't affect application correctness, CVE scan on a broken app is still a broken app

### Research Flags

Phases with standard patterns (skip research-phase):
- **Phase 1 (Security):** All fixes have specific line references and implementation code already drafted in ARCHITECTURE.md
- **Phase 2 (Testing):** pytest and HTTP-level testing patterns are well-documented, fixtures already sketched
- **Phase 3 (Observability):** Python stdlib logging with custom Formatter is standard practice, migration strategy is clear

Phases likely needing deeper research during planning:
- **Phase 4 (CI):** Railway's CI/CD configuration and CVE scan caching patterns need validation. Research recommends trivy or pip-audit but Railway-specific integration steps are unconfirmed.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All decisions confirmed against Python 3.10 stdlib docs and OWASP guidelines. PBKDF2 vs scrypt debate resolved in favor of PBKDF2 (simpler, equally valid per OWASP 2023). Primary source: actual codebase. |
| Features | HIGH | Derived from direct codebase analysis with specific file and line number references. Not speculation. Competitor comparison (Dify, Open WebUI, LocalAI) confirms what security-conscious users expect. |
| Architecture | HIGH | All implementation code drafted with exact line references. Every fix maps to a specific location. Migration paths designed. Primary source: gateway.py, entrypoint.sh, secret.sh analysis. |
| Pitfalls | HIGH | Based on codebase analysis plus verified security research (OWASP, Bandit, OpenStack). Migration lockout pitfall is specific to this codebase's bare-hex hash format with no algorithm prefix. |

**Overall confidence:** HIGH

### Gaps to Address

- **PBKDF2 iteration count on Railway CPU:** 600K iterations takes ~250ms on fast hardware but could reach 1-2s on throttled Railway containers. Benchmark on actual Railway deployment in Phase 1 and reduce to 300K if needed. Still vastly better than bare SHA-256.
- **Railway CI configuration:** Research confirms what to do (CVE scan, hash-pinned deps) but not the exact Railway CI syntax. Validate during Phase 4 planning.
- **Security headers vs setup.html:** CSP headers could block setup.html's inline JavaScript, locking users out of setup. Needs manual testing against actual setup.html before Phase 3 ships. May require per-path CSP rules.
- **bats-core vs Python subprocess for shell script tests:** STACK.md says Python subprocess tests are sufficient; ARCHITECTURE.md recommends bats-core. Resolve during Phase 2 planning. Either works, just pick one and commit.
- **Rate limiter ordering relative to PBKDF2:** Rate limit check must happen BEFORE hash computation in the auth handler code path. Verify the existing code path order before Phase 1 ships. A brute-force attack consuming 250ms CPU per attempt even when rate-limited is a DoS vector.

## Sources

### Primary (HIGH confidence)
- Codebase analysis: gateway.py (406KB, 9700+ lines), entrypoint.sh (700+ lines), secret.sh, job.sh, Dockerfile — all line references verified against source
- [Python hashlib documentation](https://docs.python.org/3/library/hashlib.html) — pbkdf2_hmac and scrypt stdlib availability confirmed
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) — container hardening requirements
- [Docker Official Security Docs](https://docs.docker.com/engine/security/) — resource constraints, HEALTHCHECK patterns
- [argon2-cffi documentation](https://argon2-cffi.readthedocs.io/) — confirmed as non-stdlib, rules it out for gateway.py

### Secondary (MEDIUM confidence)
- [Password Hashing Guide: Argon2 vs Bcrypt vs Scrypt vs PBKDF2 (2026)](https://guptadeepak.com/the-complete-guide-to-password-hashing-argon2-vs-bcrypt-vs-scrypt-vs-pbkdf2-2026/) — algorithm comparison, iteration count recommendations
- [OpenStack: Avoid shell=True](https://security.openstack.org/guidelines/dg_avoid-shell-true.html) — shell injection avoidance patterns for subprocess
- [Snyk: Command Injection in Python](https://snyk.io/blog/command-injection-python-prevention-examples/) — injection prevention patterns and os.environ approach
- [New Relic: Structured Logging in Python](https://newrelic.com/blog/log/python-structured-logging) — JSON logging migration patterns
- [Why Retrofitting Tests Is Hard](https://modelephant.medium.com/software-engineering-why-retrofitting-tests-is-hard-9ea4e7af3e48) — monolith testing strategy justification

### Tertiary (LOW confidence)
- [Python Security Best Practices](https://arjancodes.com/blog/best-practices-for-securing-python-applications/) — general guidance, not GooseClaw-specific
- [Web Application Security Best Practices 2026](https://www.radware.com/cyberpedia/application-security/web-application-security-best-practices/) — industry overview

---
*Research completed: 2026-03-16*
*Ready for roadmap: yes*

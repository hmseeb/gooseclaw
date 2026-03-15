# Feature Research

**Domain:** Production hardening for self-hosted Docker AI agent platform
**Researched:** 2026-03-16
**Confidence:** HIGH

## Feature Landscape

### Table Stakes (Users Expect These)

Features that any production-ready self-hosted Docker app must have. Missing these = security incident waiting to happen or ops nightmare.

#### Security

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| No shell injection in entrypoint/scripts | Any user-controlled string hitting `shell=True` or unquoted bash vars is RCE. OWASP top 10. | MEDIUM | secret.sh interpolates `$DOTPATH` and `$VALUE` directly into Python strings inside bash. gateway.py `_run_script` uses `shell=True`. entrypoint.sh interpolates `$GOOSECLAW_RESET_PASSWORD` into inline Python. All three are injection vectors. |
| Password hashing upgrade (SHA-256 to argon2id) | SHA-256 is not a password hash. It has no salt, no work factor, and is GPU-trivially-crackable. Any security-aware user will flag this immediately. | LOW | `argon2-cffi` is pip-installable (Dockerfile already uses pip for other deps). Add to requirements.txt. hashlib.sha256 calls in gateway.py line 1090 and entrypoint.sh line 66 need replacement. |
| Stop leaking recovery secret in logs | entrypoint.sh line 39 prints `GOOSECLAW_RECOVERY_SECRET=<value>` to stdout on first boot. Anyone with log access (Railway dashboard, log drain) sees the secret. | LOW | Print only a hint ("recovery secret saved to /data/.recovery_secret") without the actual value. |
| Request body size limits | Without limits, a single POST with a 10GB body exhausts memory and crashes the process. | LOW | gateway.py reads `Content-Length` at line 9601 but has no max check. Add a MAX_BODY_SIZE constant (e.g., 1MB) and reject oversized requests with 413. |
| Dependency lock file (pinned hashes) | Without pinned hashes, `pip install` can pull compromised packages via supply chain attacks. | LOW | requirements.txt has version ranges for chromadb and mcp. Pin exact versions and add `--require-hashes` support. |
| Non-root container execution | Already partially done (gooseclaw user exists). entrypoint.sh still runs as root for setup, then drops privileges. This is correct pattern. | DONE | Verify no processes remain running as root after init. |

#### Hardening

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Docker resource limits documentation | A runaway goose session or OOM from chromadb can take down the container. Railway allows setting these but the Dockerfile should document recommended values. | LOW | Railway sets these via service config, not Dockerfile. Document recommended values (e.g., 1GB RAM, 1 CPU minimum). |
| Structured JSON logging | Text logs are unparseable by log aggregators (Datadog, Loki, Railway log drains). Every production service needs machine-readable logs. | MEDIUM | Replace print() calls with a thin `log()` wrapper that outputs JSON with timestamp, level, component. gateway.py has hundreds of print statements. |
| Graceful shutdown with timeout | Already has SIGTERM handler but no timeout. If goosed hangs on shutdown, the container never stops. Railway sends SIGKILL after 10s. | LOW | Add `timeout` to the `wait` in shutdown handler. Use `kill -9` after 5s grace period. |
| CVE scanning in CI | Users deploying self-hosted security-sensitive software expect the maintainer to scan for known vulnerabilities. | LOW | Add `trivy image` or `grype` scan to CI pipeline. ubuntu:22.04 base accumulates CVEs fast. |
| Complete HTTP security headers | Referrer-Policy and Permissions-Policy already exist. Missing: X-DNS-Prefetch-Control, Cross-Origin-Opener-Policy, Cross-Origin-Resource-Policy. | LOW | Already have most headers. Add the missing ones to the SECURITY_HEADERS dict at line 861. |
| CSRF protection | Setup wizard POSTs config changes. Without CSRF tokens, any page the user visits could silently reconfigure their agent. | MEDIUM | Session-based CSRF tokens for all state-changing POST endpoints. The cookie auth system already exists, bolt CSRF onto it. |

#### Testing

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Gateway HTTP endpoint tests | Only 1 test file exists (test_gateway.py) covering specific features. Core HTTP routing, auth flow, rate limiting, and security headers are untested. | HIGH | gateway.py is ~9800 lines. Needs systematic coverage of: auth endpoints, setup endpoints, job CRUD, health checks, proxy behavior, error handling. |
| Shell script tests | job.sh, remind.sh, notify.sh, secret.sh are untested bash scripts that handle user input. | MEDIUM | Use bats-core (Bash Automated Testing System) or simple bash test scripts. Mock the curl calls to gateway API. |
| Entrypoint bootstrap tests | entrypoint.sh handles first boot, config generation, env var rehydration, upgrade paths. All untested. | MEDIUM | Test in a Docker build context. Verify config.yaml generation, symlink creation, version upgrade logic. |
| Integration/e2e tests | No end-to-end test that boots the container and validates the full flow (setup wizard -> configure provider -> goose starts -> telegram connects). | HIGH | Docker-based e2e test. Start container, hit health endpoint, complete setup flow, verify goosed starts. |

### Differentiators (Competitive Advantage)

Features that go beyond table stakes and make GooseClaw stand out among self-hosted AI agent platforms. Not expected, but valued.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Automated security audit endpoint | Self-hosted users want confidence their deployment is secure. An `/api/security/audit` endpoint that checks password strength, header config, exposed ports, and log settings gives instant peace of mind. | MEDIUM | Check: password hash algorithm, recovery secret not in env, security headers present, body size limits set, rate limiting active. Return pass/fail per check. |
| Secrets rotation support | Vault stores API keys in plaintext YAML. Adding rotation reminders or one-click rotation (re-validate key, update vault) reduces credential staleness risk. | HIGH | Complex because each provider has different key rotation flows. Defer to later milestone. |
| Container health dashboard | `/admin` already exists. Enhance with: memory usage, uptime, goosed restart count, last error, active sessions, rate limit stats. | MEDIUM | Most data is already tracked in gateway.py globals. Surface it in the admin dashboard. |
| Audit logging | Log who did what and when: config changes, password resets, vault access, job creation. Critical for multi-user or compliance scenarios. | MEDIUM | Append-only log file at /data/audit.log. JSON format. Covers: auth events, config changes, vault operations, job lifecycle. |
| Rate limiting per-endpoint tuning | Current rate limiting is per-IP globally. Different endpoints have different sensitivity (auth=strict, health=relaxed, API=moderate). | LOW | Already partially done (auth_limiter, notify_limiter exist as separate instances). Formalize and document. |
| Dependency auto-update bot | Automated PRs for dependency updates with CVE annotations. Users forking the template get notified of security updates. | LOW | Enable Dependabot or Renovate on the GitHub repo. Configuration file only. |

### Anti-Features (Commonly Requested, Often Problematic)

Features that seem good but create problems for this specific project.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| TLS termination in container | "My app should serve HTTPS directly" | Railway (and most PaaS) terminates TLS at the load balancer. Adding TLS inside the container means managing certs, renewal (certbot cron), and double encryption. Adds complexity for zero security benefit on Railway. | Trust Railway's TLS termination. Set HSTS headers (already done). Document that direct Docker deployments need a reverse proxy (nginx/caddy). |
| WAF inside container | "Block SQL injection and XSS at the edge" | A stdlib Python HTTP server is not the right place for a WAF. WAFs need dedicated infrastructure, regular rule updates, and introduce latency. The attack surface is small (single-user, auth-gated). | Input sanitization (already exists), rate limiting (already exists), and content-type validation are sufficient for this threat model. |
| Encrypted vault at rest | "API keys should be encrypted on disk" | Encryption at rest for a single-user self-hosted app means managing encryption keys, which are stored on the same disk. If an attacker has disk access, they have the key too. Railway volumes are already isolated per deployment. | File permissions (chmod 600/700 already done). Document that Railway volumes are not shared. For paranoid users, point to external secret managers (Railway env vars, Doppler). |
| Multi-factor authentication | "MFA for the web dashboard" | Single-user self-hosted app deployed on a PaaS with its own auth. Adding TOTP/WebAuthn is significant complexity for a use case where the user already authenticated to Railway. | Strong password (enforce minimum length), session expiry (already 24hr), rate-limited auth (already 5/min). Document that Railway's own auth is the primary gate. |
| Comprehensive RBAC | "Role-based access control for different users" | GooseClaw is a single-user personal agent. RBAC adds schema complexity, migration burden, and UX friction for a use case that doesn't exist yet. | Single admin password. If multi-user is needed later, build it then. |
| Automatic certificate management | "Zero-config HTTPS with Let's Encrypt" | Only relevant for bare-metal Docker deployments. Railway/Render/Fly all handle TLS. Adding certbot adds a cron job, port 80 requirement, and failure mode. | Document reverse proxy setup for bare-metal. Provide example nginx/caddy configs. |

## Feature Dependencies

```
Password hashing upgrade
    (no dependencies, standalone fix)

Shell injection fixes
    (no dependencies, standalone fix)

Recovery secret leak fix
    (no dependencies, standalone fix)

Request body size limits
    (no dependencies, standalone fix)

Structured JSON logging
    └──enhances──> Audit logging (audit log uses same format)

Gateway HTTP tests
    └──requires──> Shell injection fixes (test the fixed code, not the broken code)

Shell script tests
    └──requires──> Shell injection fixes (secret.sh injection must be fixed first)

Entrypoint tests
    └──requires──> Recovery secret leak fix (test correct behavior)

Integration/e2e tests
    └──requires──> Gateway HTTP tests (unit tests first, then integration)
    └──requires──> All security fixes (test the secure system)

Audit logging
    └──requires──> Structured JSON logging (consistent log format)

Security audit endpoint
    └──requires──> Password hashing upgrade (needs to check hash algorithm)
    └──requires──> All security fixes (needs to verify they're in place)

CSRF protection
    └──requires──> Gateway HTTP tests (need tests before adding auth complexity)
```

### Dependency Notes

- **Security fixes are independent**: Shell injection, password hashing, secret leak, body size limits can all be done in parallel with zero dependencies on each other.
- **Tests depend on fixes**: Write tests for the fixed code. Fixing and testing can happen in the same phase, but fix first, test second within each task.
- **Structured logging before audit logging**: Audit logging should use the same JSON format as structured logging. Do structured logging first, then audit logging reuses the pattern.
- **E2e tests come last**: They validate the whole system works together, so all component-level fixes and tests must exist first.

## MVP Definition

### Launch With (v4.0 - This Milestone)

Critical security and testing work. Ship these or the product is not production-ready.

- [x] Fix shell injection in secret.sh, entrypoint.sh, gateway.py `_run_script` -- RCE vectors
- [x] Upgrade password hashing from SHA-256 to argon2id -- crackable passwords
- [x] Stop leaking recovery secret in entrypoint.sh logs -- credential leak
- [x] Add request body size limits -- DoS vector
- [x] Pin dependency versions with hashes -- supply chain risk
- [x] Add graceful shutdown timeout -- container hangs
- [x] Gateway HTTP endpoint tests (auth, setup, jobs, health) -- no test coverage on core paths
- [x] Shell script tests (secret.sh, job.sh, remind.sh, notify.sh) -- untested user-facing scripts
- [x] Entrypoint bootstrap tests -- untested init logic
- [x] CVE scanning in CI -- no vulnerability monitoring
- [x] Complete HTTP security headers -- minor gaps

### Add After Validation (v4.x)

Features to add once core security and tests are solid.

- [ ] Structured JSON logging -- enables log aggregation, needed before audit logging
- [ ] CSRF protection on state-changing endpoints -- defense in depth
- [ ] Audit logging (config changes, auth events, vault ops) -- accountability
- [ ] Security audit endpoint (`/api/security/audit`) -- user confidence
- [ ] Container health dashboard enhancements -- operational visibility
- [ ] E2e integration tests -- validate full system

### Future Consideration (v5+)

Features to defer until production usage patterns are established.

- [ ] Secrets rotation support -- complex, provider-specific
- [ ] Dependency auto-update bot (Dependabot/Renovate) -- maintenance automation

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Fix shell injection (3 locations) | HIGH | MEDIUM | P1 |
| Password hashing upgrade (SHA-256 to argon2id) | HIGH | LOW | P1 |
| Stop leaking recovery secret | HIGH | LOW | P1 |
| Request body size limits | HIGH | LOW | P1 |
| Pin dependencies with hashes | MEDIUM | LOW | P1 |
| Graceful shutdown timeout | MEDIUM | LOW | P1 |
| Complete HTTP security headers | MEDIUM | LOW | P1 |
| Gateway HTTP endpoint tests | HIGH | HIGH | P1 |
| Shell script tests | MEDIUM | MEDIUM | P1 |
| Entrypoint bootstrap tests | MEDIUM | MEDIUM | P1 |
| CVE scanning in CI | MEDIUM | LOW | P1 |
| Structured JSON logging | MEDIUM | MEDIUM | P2 |
| CSRF protection | MEDIUM | MEDIUM | P2 |
| Audit logging | MEDIUM | MEDIUM | P2 |
| Security audit endpoint | LOW | MEDIUM | P2 |
| E2e integration tests | HIGH | HIGH | P2 |
| Health dashboard enhancements | LOW | MEDIUM | P3 |
| Secrets rotation | LOW | HIGH | P3 |

**Priority key:**
- P1: Must have for v4.0 launch (security fixes + core test coverage)
- P2: Should have, add in v4.x (defense in depth + observability)
- P3: Nice to have, future consideration

## Competitor Feature Analysis

| Feature | Dify (self-hosted) | LocalAI | Open WebUI | GooseClaw Approach |
|---------|-------------------|---------|------------|-------------------|
| Password hashing | bcrypt | N/A (no auth) | bcrypt | Upgrade to argon2id (stronger than bcrypt, OWASP recommended) |
| Rate limiting | nginx-level | None | None | Built-in per-IP sliding window (already exists, per-endpoint tuning needed) |
| Security headers | nginx config | None | minimal | Comprehensive set in gateway.py (mostly done) |
| Structured logging | Yes (JSON) | Partial | Minimal | Needs implementation (currently print statements) |
| CVE scanning | GitHub Actions | Trivy | None documented | Add to CI (trivy or grype) |
| CSRF protection | Yes (framework) | N/A | Yes (SvelteKit) | Needs implementation (no framework provides it) |
| Test coverage | Moderate | Good | Good | Needs significant improvement (current: minimal) |
| Shell injection prevention | Framework handles | N/A | N/A | Manual fix needed (unique to bash+Python architecture) |
| Secrets management | Environment vars | Environment vars | Environment vars | Vault file + env vars (unique, needs injection fix) |
| Graceful shutdown | Docker Compose | Docker | Docker | Custom handler (needs timeout) |

## Sources

- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) -- HIGH confidence, authoritative
- [Docker Official Security Docs](https://docs.docker.com/engine/security/) -- HIGH confidence, authoritative
- [Docker Resource Constraints](https://docs.docker.com/engine/containers/resource_constraints/) -- HIGH confidence, authoritative
- [Password Hashing Guide: Argon2 vs Bcrypt vs Scrypt vs PBKDF2](https://guptadeepak.com/the-complete-guide-to-password-hashing-argon2-vs-bcrypt-vs-scrypt-vs-pbkdf2-2026/) -- MEDIUM confidence, comprehensive comparison
- [argon2-cffi documentation](https://argon2-cffi.readthedocs.io/) -- HIGH confidence, official library docs
- [Python Security Best Practices](https://arjancodes.com/blog/best-practices-for-securing-python-applications/) -- MEDIUM confidence, best practices guide
- [Web Application Security Best Practices 2026](https://www.radware.com/cyberpedia/application-security/web-application-security-best-practices/) -- MEDIUM confidence, industry overview
- Codebase analysis of gateway.py, entrypoint.sh, secret.sh, Dockerfile -- HIGH confidence, primary source

---
*Feature research for: production hardening of self-hosted Docker AI agent platform*
*Researched: 2026-03-16*

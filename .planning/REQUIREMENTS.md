# Requirements: GooseClaw v4.0

**Defined:** 2026-03-16
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v4.0 Requirements

### Security

- [ ] **SEC-01**: Shell injection in secret.sh eliminated (variable interpolation into Python strings replaced with safe argument passing)
- [ ] **SEC-02**: Shell injection in entrypoint.sh eliminated (GOOSECLAW_RESET_PASSWORD no longer interpolated into inline Python)
- [ ] **SEC-03**: Command injection in gateway.py `_run_script` eliminated (shell=True replaced with list-based execution)
- [ ] **SEC-04**: Password hashing upgraded from SHA-256 to PBKDF2 with salt and 600K iterations
- [ ] **SEC-05**: Existing SHA-256 password hashes transparently migrate to PBKDF2 on successful login (lazy migration)
- [ ] **SEC-06**: Recovery secret no longer printed to container logs on first boot
- [ ] **SEC-07**: Request body size limited to configurable maximum (default 1MB), oversized requests rejected with 413

### Hardening

- [ ] **HARD-01**: Dependencies pinned to exact versions with hashes in lock file
- [ ] **HARD-02**: CVE scanning configured via GitHub Dependabot or equivalent
- [ ] **HARD-03**: Graceful shutdown has 5-second timeout, force-kills hung processes after grace period
- [ ] **HARD-04**: Missing HTTP security headers added (Referrer-Policy, Permissions-Policy, Cross-Origin-Opener-Policy)
- [ ] **HARD-05**: Structured JSON logging replaces print() calls with stdlib logging module and custom JSON formatter
- [ ] **HARD-06**: Security-sensitive operations (auth, config changes, errors) logged in structured format first (incremental migration)

### Testing

- [ ] **TEST-01**: Gateway HTTP auth endpoints tested (login, session validation, rate limiting, password reset)
- [ ] **TEST-02**: Gateway HTTP setup endpoints tested (provider config, validation, save)
- [ ] **TEST-03**: Gateway HTTP job endpoints tested (create, list, cancel, run, schedule)
- [ ] **TEST-04**: Gateway HTTP health endpoints tested (/api/health, /api/health/ready, /api/health/jobs)
- [ ] **TEST-05**: Gateway security headers and CORS tested across all response paths
- [ ] **TEST-06**: Shell scripts tested (job.sh duration/time parsing, remind.sh flags, notify.sh message handling, secret.sh vault ops)
- [ ] **TEST-07**: Entrypoint bootstrap tested (directory creation, config generation, env rehydration, provider detection)
- [ ] **TEST-08**: E2e integration test boots container, completes setup wizard, verifies goosed starts and health endpoint returns 200
- [ ] **TEST-09**: pytest + requests test infrastructure established with requirements-dev.txt

## Future Requirements

### v4.x (After Core Hardening)

- **CSRF-01**: CSRF tokens on all state-changing POST endpoints
- **AUDIT-01**: Append-only audit log for auth events, config changes, vault operations
- **AUDIT-02**: Security audit endpoint (/api/security/audit) checks hash algorithm, headers, rate limiting
- **DASH-01**: Container health dashboard shows memory, uptime, restart count, active sessions

## Out of Scope

| Feature | Reason |
|---------|--------|
| TLS termination in container | Railway terminates TLS at load balancer. Adding certs inside container is unnecessary complexity |
| WAF inside container | Single-user auth-gated app. Input sanitization + rate limiting is sufficient |
| Encrypted vault at rest | Single-user, Railway volumes isolated. Encryption key on same disk provides no real benefit |
| Multi-factor authentication | Single-user self-hosted app. Railway's own auth is the primary gate |
| RBAC / multi-user auth | GooseClaw is a personal agent. Build multi-user when the use case exists |
| argon2 password hashing | Requires pip dependency. PBKDF2 via stdlib achieves same security goal |
| structlog / python-json-logger | Pip dependencies. Stdlib logging + custom JSON formatter is sufficient |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| SEC-01 | — | Pending |
| SEC-02 | — | Pending |
| SEC-03 | — | Pending |
| SEC-04 | — | Pending |
| SEC-05 | — | Pending |
| SEC-06 | — | Pending |
| SEC-07 | — | Pending |
| HARD-01 | — | Pending |
| HARD-02 | — | Pending |
| HARD-03 | — | Pending |
| HARD-04 | — | Pending |
| HARD-05 | — | Pending |
| HARD-06 | — | Pending |
| TEST-01 | — | Pending |
| TEST-02 | — | Pending |
| TEST-03 | — | Pending |
| TEST-04 | — | Pending |
| TEST-05 | — | Pending |
| TEST-06 | — | Pending |
| TEST-07 | — | Pending |
| TEST-08 | — | Pending |
| TEST-09 | — | Pending |

**Coverage:**
- v4.0 requirements: 22 total
- Mapped to phases: 0
- Unmapped: 22

---
*Requirements defined: 2026-03-16*
*Last updated: 2026-03-16 after initial definition*

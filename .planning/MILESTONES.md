# Milestones

## v4.0 Production Hardening (Shipped: 2026-03-16)

**Phases:** 18-21 (4 phases, 12 plans)
**Requirements:** 22/22 satisfied
**Tests:** 103 automated tests
**Lines:** +8,542 / -1,831 across 63 files

**Key accomplishments:**
- Eliminated 3 shell injection vectors (secret.sh, entrypoint.sh, gateway.py _run_script) via os.environ pattern
- Upgraded password hashing from bare SHA-256 to PBKDF2 (600K iterations) with transparent lazy migration
- Sealed recovery secret leak from container logs, added 1MB request body limits
- Built 103-test suite: HTTP endpoints (auth, setup, jobs, health, headers), shell scripts, entrypoint, Docker e2e
- Migrated all 254 print() calls to structured JSON logging with 12 component loggers
- Added 5-second shutdown watchdog, dependency pinning, Dependabot CVE scanning

**Tech debt accepted:**
- requirements.lock not generated (script exists, Dockerfile supports it)
- E2e tests need Docker daemon (skipped gracefully when unavailable)

---


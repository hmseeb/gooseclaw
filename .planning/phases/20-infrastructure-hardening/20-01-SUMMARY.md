---
phase: 20-infrastructure-hardening
plan: 01
subsystem: infra
tags: [pip, dependabot, supply-chain, docker, security]

requires:
  - phase: 19-test-infrastructure
    provides: pytest framework and test fixtures
provides:
  - Exact-pinned requirements.txt with == for all direct dependencies
  - generate-lockfile.sh for hash-pinned lock file generation in Docker
  - Dockerfile updated to use --require-hashes when lock file present
  - Dependabot configuration for pip ecosystem CVE scanning
affects: [21-end-to-end-validation]

tech-stack:
  added: [pip-tools, dependabot]
  patterns: [hash-pinned-deps, lockfile-generation]

key-files:
  created:
    - docker/generate-lockfile.sh
    - .github/dependabot.yml
    - docker/tests/test_hardening.py
  modified:
    - docker/requirements.txt
    - Dockerfile

key-decisions:
  - "Pin direct deps to exact versions in requirements.txt; full transitive lock generated via Docker container (Python 3.10+ required)"
  - "Dockerfile conditionally uses --require-hashes when requirements.lock exists, falls back to requirements.txt for dev"

patterns-established:
  - "Lock generation via Docker: run generate-lockfile.sh --docker to produce hash-pinned lock file matching target platform"

requirements-completed: [HARD-01, HARD-02]

duration: 5min
completed: 2026-03-16
---

# Plan 20-01: Dependency Pinning Summary

**Exact-pinned requirements.txt, Docker-based hash lock generation, Dependabot CVE scanning, and Dockerfile --require-hashes support**

## Performance

- **Duration:** 5 min
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- All 4 direct Python deps pinned to exact versions with == in requirements.txt
- generate-lockfile.sh creates hash-pinned lock file inside Docker container (matching target Python 3.10+)
- Dockerfile conditionally installs from requirements.lock with --require-hashes when available
- GitHub Dependabot configured for weekly pip ecosystem scanning in /docker
- 5 tests validating the entire supply chain security setup

## Task Commits

1. **Task 1+2: Pin deps, update Dockerfile, Dependabot, tests** - `9c97ef0` (feat)

## Files Created/Modified
- `docker/requirements.txt` - Pinned chromadb and mcp to exact versions
- `docker/generate-lockfile.sh` - Script to generate requirements.lock inside Docker
- `Dockerfile` - Uses --require-hashes with lock file, updated comments
- `.github/dependabot.yml` - Pip ecosystem monitoring for /docker
- `docker/tests/test_hardening.py` - 5 tests for HARD-01 and HARD-02

## Decisions Made
- Could not generate requirements.lock locally (Python 3.9, deps need 3.10+). Created Docker-based generation script instead.
- Dockerfile uses conditional: if lock file exists use --require-hashes, otherwise fall back to requirements.txt

## Deviations from Plan
- Lock file not generated as a static committed artifact (local Python too old). Instead, added a generation script. The Dockerfile handles both paths.

## Issues Encountered
- pip-compile requires Python 3.10+ for chromadb/mcp resolution. Solved by creating generate-lockfile.sh --docker.

## Next Phase Readiness
- Supply chain security infrastructure in place
- Lock file can be generated and committed when Docker or Python 3.10+ is available

---
*Phase: 20-infrastructure-hardening*
*Completed: 2026-03-16*

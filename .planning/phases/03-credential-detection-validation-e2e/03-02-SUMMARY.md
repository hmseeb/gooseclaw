---
phase: 03-credential-detection-validation-e2e
plan: 02
subsystem: detection
tags: [regex, credential, mcp, pipeline, gateway, vault]

requires:
  - phase: 03-credential-detection-validation-e2e
    provides: validator module with validate_syntax, health_check, check_and_disable
  - phase: 02-extension-registration-and-boot-lifecycle
    provides: registry module, register_generated_extension in gateway
  - phase: 01-template-engine-and-code-generation
    provides: generator module with generate_extension
provides:
  - detect_credentials() for scanning text for API keys, tokens, app passwords
  - classify_credential() for mapping credentials to templates
  - credential_to_extension() end-to-end pipeline (vault->generate->validate->register)
  - POST /api/credential-setup endpoint for AI-triggered credential setup
  - Validation gates in register_generated_extension (syntax check + health check)
affects: []

tech-stack:
  added: []
  patterns: [regex credential detection, lazy module imports, monkeypatch-heavy integration tests]

key-files:
  created:
    - docker/extensions/detector.py
    - docker/tests/test_detector.py
  modified:
    - docker/gateway.py

key-decisions:
  - "Credential detection uses regex with known prefixes rather than ML/heuristics for determinism and zero dependencies"
  - "Email template pipeline returns partial success asking for remaining fields (host, user) rather than blocking on all fields"
  - "Health check failure during initial setup logs warning but does not auto-disable, since vault may not be populated yet"

patterns-established:
  - "Regex-based credential detection with confidence scoring"
  - "Classification mapping from detected type + user hint to template + vault config"
  - "Monkeypatch-heavy integration tests for cross-module pipelines"

requirements-completed: [DET-01, DET-02, DET-03, DET-04]

duration: 8min
completed: 2026-04-01
---

# Plan 03-02: Credential Detection and E2E Pipeline Summary

**Regex-based credential detection, template classification via type+hint, and full vault->generate->validate->register pipeline wired to /api/credential-setup**

## Performance

- **Duration:** 8 min
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments
- detect_credentials() identifies API keys (sk-, pk-), GitHub tokens (ghp_), app passwords (16 lowercase chars), AWS keys (AKIA), Slack/GitLab tokens, and bearer tokens
- classify_credential() maps detected credentials to email_imap or rest_api templates using type + user hints
- credential_to_extension() runs the full pipeline: vault the credential, generate extension, validate syntax, health check, register
- gateway.py now validates syntax before registration and health-checks after restart
- POST /api/credential-setup endpoint for AI-triggered credential setup
- 13 comprehensive unit tests covering all detection, classification, and pipeline paths

## Task Commits

Each task was committed atomically:

1. **Task 1: Create detector.py** - `22765b4` (feat)
2. **Task 2: Wire gateway.py** - `9a3833e` (feat)
3. **Task 3: Unit tests for detector** - `bbbbb43` (test)

## Files Created/Modified
- `docker/extensions/detector.py` - Credential detection, classification, and orchestration
- `docker/gateway.py` - Validation gates in register_generated_extension + /api/credential-setup endpoint
- `docker/tests/test_detector.py` - 13 unit tests for detection, classification, and pipeline

## Decisions Made
- Used regex with known prefixes for determinism and zero external dependencies
- Email template returns partial success asking for remaining fields
- Health check failure during setup logs warning but does not auto-disable

## Deviations from Plan
None - plan executed exactly as written

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All three phases complete: template engine, registration/boot lifecycle, credential detection/validation/e2e
- Full credential-to-extension pipeline is operational

---
*Phase: 03-credential-detection-validation-e2e*
*Completed: 2026-04-01*

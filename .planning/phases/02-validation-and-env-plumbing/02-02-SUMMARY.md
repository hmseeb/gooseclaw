---
phase: 02-validation-and-env-plumbing
plan: 02
subsystem: ui
tags: [javascript, html, validation, wizard, credentials, reconfigure]

requires:
  - phase: 02-validation-and-env-plumbing-01
    provides: provider validation endpoint (/api/setup/validate) with skip_validation flag

provides:
  - Frontend credential validation gating (cannot save without Test passing)
  - Per-provider format hints on each credential field
  - Empty API key blocking on Continue from credentials step
  - Telegram token format validation on blur (regex digits:alphanumeric)
  - Wizard pre-fill on reconfigure (provider card, model, timezone, masked secret placeholders)
  - Keep-existing logic in saveConfig() (empty fields during reconfigure preserve backend secrets)

affects:
  - 02-validation-and-env-plumbing-03
  - Any phase touching setup.html UX

tech-stack:
  added: []
  patterns:
    - "validationPassed flag tracks whether credentials have been tested; reset on provider switch"
    - "updateSaveBtn() syncs save button disabled state from validationPassed"
    - "prefillWizard(config) centralizes all reconfigure pre-fill logic"
    - "saveConfig() omits empty credential fields during reconfigure so backend preserves them"

key-files:
  created: []
  modified:
    - docker/setup.html

key-decisions:
  - "Validation gate: Save button disabled until validationPassed=true; set by validateKey() success or skip_validation response; also set by prefillWizard() for reconfigure flow"
  - "Empty field blocking in advanceFromCredentials(): provider-specific checks (URL format for local, required fields for azure/claude-code/custom, non-empty for standard API keys)"
  - "Secret pre-fill uses placeholder not value: 'Key already set (leave blank to keep)' -- no masked value ever sent to DOM"
  - "Keep-existing on save: if isReconfigure && dashboardConfig.{field}_set and input is empty, omit field from POST payload entirely so backend never receives empty string override"
  - "Telegram blur validation uses /^\\d+:[A-Za-z0-9_-]+$/ showing inline error; non-blocking (optional field)"
  - "Local providers (ollama/lm-studio/docker-model-runner/ramalama) auto-pass validationPassed after URL format check in advanceFromCredentials()"
  - "github-copilot auto-passes validationPassed (no key needed)"

patterns-established:
  - "Per-provider format hints via getFormatHint(providerId) lookup table"
  - "Client-side validateFormat(providerId, value) returns {valid, error, warning} for prefix checks"

requirements-completed: [CRED-01, CRED-02, CRED-03, CRED-04, CRED-05, PROV-06, TG-02, UX-07]

duration: 3min
completed: 2026-03-10
---

# Phase 02 Plan 02: Validation Gating and Wizard Pre-fill Summary

**Frontend credential validation gating with per-provider format hints, empty-field blocking, and full wizard pre-fill on reconfigure so users cannot save without testing credentials and returning users see their existing settings**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-10T22:34:26Z
- **Completed:** 2026-03-10T22:38:12Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments

- Save button disabled until credentials are tested (or provider is local/github-copilot which auto-pass)
- Per-provider format hints (`getFormatHint`) and client-side prefix validation (`validateFormat`) on every credential field
- Telegram token validated on blur against `/^\d+:[A-Za-z0-9_-]+$/` with inline error
- `prefillWizard(config)` pre-fills all wizard fields from existing config; secrets shown as masked placeholder only
- `saveConfig()` omits empty credential fields during reconfigure so backend preserves existing secrets

## Task Commits

Each task was committed atomically:

1. **Task 1: Add validation gating and per-provider format checks** - `add3c69` (feat)
2. **Task 2: Pre-fill wizard fields on reconfigure** - `17c82cf` (feat)

## Files Created/Modified

- `/Users/haseeb/nix-template/docker/setup.html` - Added validation gating, format hints, pre-fill logic, and keep-existing save behavior

## Decisions Made

- Validation gate: Save button disabled until `validationPassed=true`; set by `validateKey()` success or `skip_validation` response; also set by `prefillWizard()` for reconfigure flow
- Empty field blocking in `advanceFromCredentials()`: provider-specific checks (URL format for local providers, required fields for azure/claude-code/custom, non-empty for standard API keys)
- Secret pre-fill uses placeholder not value: `'Key already set (leave blank to keep)'` -- no masked value ever sent to DOM
- Keep-existing on save: if `isReconfigure && dashboardConfig.{field}_set` and input is empty, omit field from POST payload entirely so backend never receives empty string override
- Local providers (ollama, lm-studio, docker-model-runner, ramalama) auto-pass `validationPassed` after URL format check in `advanceFromCredentials()`
- `github-copilot` auto-passes `validationPassed` (no key needed, device flow handles auth at runtime)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Frontend validation gating complete; all credential flows now enforce testing before save
- Reconfigure flow fully pre-fills wizard from existing config with proper secret masking
- Ready for 02-03 (environment variable plumbing or next plan in phase)

---
*Phase: 02-validation-and-env-plumbing*
*Completed: 2026-03-10*

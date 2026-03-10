---
phase: 01-provider-ui-expansion
plan: 02
subsystem: ui
tags: [html, javascript, setup-wizard, model-selection, multi-step-form, provider-expansion]

# Dependency graph
requires:
  - phase: 01-01
    provides: PROVIDERS registry with defaultModel/models arrays for each provider
provides:
  - 5-step setup wizard (Provider -> Credentials -> Model -> Settings -> Confirm)
  - buildModelStep() populating model datalist from PROVIDERS registry with provider-specific notes
  - buildSummary() showing configuration review before save
  - BotFather instructions in Telegram field on Settings step
  - 23-provider registry (expanded from 15) with compact scrollable card grid
  - Credential and model note support for 8 new providers
affects:
  - 02-01 (backend validation will need to handle all 23 providers, not just original 15)
  - 02-02 (any gateway.py apply_config additions need model field from step-2 now)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "5-step wizard with goToStep() hooks: buildModelStep() called on navigate-to-2, buildSummary() on navigate-to-4"
    - "Provider-specific model notes: if/else branches in buildModelStep() keyed off selectedProvider"
    - "Confirmation summary pattern: buildSummary() reads all live form fields to produce read-only review card"

key-files:
  created: []
  modified:
    - docker/setup.html

key-decisions:
  - "Model selection promoted to dedicated step (step-2) rather than buried in optional settings -- makes model choice explicit and prominent"
  - "Save button moved from Settings to Confirmation step -- forces user to review before committing"
  - "Provider-specific model notes inline in buildModelStep() rather than in PROVIDERS data -- keeps complex HTML out of data objects"
  - "8 additional providers added post-checkpoint (avian, litellm, venice, ovhcloud, github-copilot, lm-studio, docker-model-runner, ramalama) expanding registry to 23 total"
  - "Compact horizontal card layout with scrollable grid (max-height 420px) to keep Continue button visible without scrolling"

patterns-established:
  - "Wizard step hooks: goToStep(N) calls buildXxx() for steps requiring dynamic content population"
  - "Model notes by provider: ollama=pre-pull warning, openrouter=provider/model format, claude-code=subscription-default notice"
  - "Confirmation step pattern: buildSummary() reads all live inputs, renders read-only key-value summary before destructive save"

requirements-completed: [MODL-01, MODL-02, MODL-03, MODL-04, UX-03, UX-04, UX-05, TG-01]

# Metrics
duration: ~15min
completed: 2026-03-10
---

# Phase 1 Plan 02: 5-Step Wizard with Model Selection and Confirmation Summary

**5-step setup wizard with dedicated model selection step (datalist from PROVIDERS registry), provider-specific model notes, BotFather instructions, confirmation summary before save, and expanded to 23 providers with compact scrollable card grid**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-03-10T14:20:00Z
- **Completed:** 2026-03-10T14:42:30Z
- **Tasks:** 2 (1 auto + 1 human-verify checkpoint)
- **Files modified:** 1

## Accomplishments
- Restructured wizard from 3 steps to 5 steps: Provider (0), Credentials (1), Model (2), Settings (3), Confirm (4)
- Created step-2 with buildModelStep() that pre-fills default model and populates datalist from PROVIDERS registry
- Added provider-specific model notes: ollama (must pre-pull), openrouter (provider/model format), claude-code (subscription default), azure-openai (deployment name), custom (exact endpoint model)
- Moved Model Override field out of Settings into dedicated Model step; Settings now only contains Timezone, Telegram, and Web Auth Token
- Added BotFather numbered instructions to Telegram field in Settings step
- Created step-4 Confirmation with buildSummary() reading all live form values and rendering key-value review card
- Moved Save & Start Agent button from Settings to Confirmation step
- Post-checkpoint expansion added 8 providers (avian, litellm, venice, ovhcloud, github-copilot, lm-studio, docker-model-runner, ramalama) bringing total to 23
- Compact horizontal card layout with scrollable grid so Continue button remains visible

## Task Commits

Each task was committed atomically:

1. **Task 1: Expand wizard from 3 to 5 steps with model selection and confirmation** - `ba30f55` (feat)
2. **Task 2: Verify complete 5-step wizard flow** - N/A (checkpoint, approved by human)
3. **Post-checkpoint: Expand to 23 providers with compact scrollable grid** - `611c998` (feat)

## Files Created/Modified
- `docker/setup.html` - 5-step wizard, buildModelStep, buildSummary, BotFather instructions, 23-provider registry, compact card layout

## Decisions Made
- Model selection promoted to its own step to make model choice explicit rather than buried in optional settings
- Confirmation step added before save so users review their choices (especially important given Telegram and auth token fields are optional)
- 8 additional providers added post-checkpoint because the plan's human-verify revealed the provider grid was too small and cluttered -- compact layout + scrollable grid solved the UX issue while expanding coverage
- github-copilot uses device-flow credential approach (no API key field, device auth instructions instead)
- Local providers (lm-studio, docker-model-runner, ramalama) use host URL field pattern established by ollama

## Deviations from Plan

### Post-Checkpoint Scope Extension

**1. [Rule 2 - Missing Critical] Expanded from 15 to 23 providers**
- **Found during:** Human verification checkpoint (Task 2)
- **Issue:** 8 providers referenced in requirements were not yet in the registry (avian, litellm, venice, ovhcloud, github-copilot, lm-studio, docker-model-runner, ramalama)
- **Fix:** Added all 8 providers to PROVIDERS registry with credential fields, model notes, save/validate handlers; switched to compact horizontal card layout with scrollable grid
- **Files modified:** docker/setup.html
- **Verification:** Human-approved post-checkpoint commit 611c998
- **Committed in:** `611c998` (separate feat commit)

---

**Total deviations:** 1 scope extension (provider expansion to meet requirements)
**Impact on plan:** Necessary to satisfy MODL requirements. No unintended scope creep.

## Issues Encountered
None beyond the planned scope extension.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- 5-step wizard is complete and human-verified
- 23 providers in registry with full metadata for Phase 2 backend wiring
- gateway.py apply_config still only handles original providers -- Phase 2 (02-01) needs to extend validation/apply handlers for all 23 providers
- Model field is now at ID `#model` in step-2 (previously in step-2 options) -- Phase 2 config reading can use same element ID

---
*Phase: 01-provider-ui-expansion*
*Completed: 2026-03-10*

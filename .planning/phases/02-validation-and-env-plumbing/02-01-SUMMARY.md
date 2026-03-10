---
phase: 02-validation-and-env-plumbing
plan: 01
subsystem: infra
tags: [docker, env-vars, entrypoint, gateway, provider-config, rehydration]

# Dependency graph
requires:
  - phase: 01-provider-ui-expansion
    provides: "23 providers in PROVIDERS registry and env_map in gateway.py"
provides:
  - "Complete entrypoint.sh rehydration env_map for all 23 providers"
  - "field_map in apply_config() mapping setup.json field names to env var names"
  - "Azure OpenAI: azure_key -> AZURE_OPENAI_API_KEY, azure_endpoint -> AZURE_OPENAI_ENDPOINT"
  - "LiteLLM: api_key -> LITELLM_API_KEY, litellm_host -> LITELLM_HOST"
  - "GitHub Copilot: api_key -> GITHUB_TOKEN"
  - "Local providers: ollama_host -> OLLAMA_HOST on container restart"
affects: [03-telegram-and-notifications, provider-validation, container-restart]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "field_map pattern for env var -> setup.json field name translation in apply_config()"
    - "Explicit provider branches in entrypoint.sh rehydration for multi-credential providers"

key-files:
  created: []
  modified:
    - docker/entrypoint.sh
    - docker/gateway.py

key-decisions:
  - "azure-openai: setup.html saves azure_key (not api_key) and azure_endpoint; field_map translates these to AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT"
  - "litellm host: litellm_host is stored if provided but not captured by current setup.html; field_map covers it for future use"
  - "github-copilot: device flow at runtime; api_key -> GITHUB_TOKEN in both rehydration and field_map for manual/programmatic configuration"
  - "local providers (ollama/lm-studio/docker-model-runner/ramalama): all share ollama_host field; OLLAMA_HOST exported if present"

patterns-established:
  - "field_map in apply_config: targeted field name override dict (not a full rewrite) preserves backward compat for single-key providers"
  - "Explicit provider branches in entrypoint.sh rehydration: multi-credential providers get their own elif block with clear field->env var comments"

requirements-completed: [PROV-04, PROV-05, ENV-01, ENV-02, ENV-03, ENV-04]

# Metrics
duration: 2min
completed: 2026-03-10
---

# Phase 02 Plan 01: Env Var Mapping and Rehydration Pipeline Summary

**Complete provider env var pipeline: field_map in apply_config() + entrypoint.sh rehydration covering all 23 providers including azure-openai (key+endpoint), litellm, github-copilot, and local providers with host URLs**

## Performance

- **Duration:** ~2 min
- **Started:** 2026-03-10T22:35:34Z
- **Completed:** 2026-03-10T22:37:20Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- entrypoint.sh rehydration block now covers all 23 providers with correct field->env var mappings
- apply_config() field_map resolves azure_key->AZURE_OPENAI_API_KEY, azure_endpoint->AZURE_OPENAI_ENDPOINT, litellm_host->LITELLM_HOST, ollama_host->OLLAMA_HOST, github-copilot api_key->GITHUB_TOKEN
- Container restart now correctly restores credentials for azure-openai, litellm, and local providers
- ENV-04 confirmed: PATH export for ~/.local/bin already present in entrypoint.sh for claude CLI

## Task Commits

Each task was committed atomically:

1. **Task 1: Sync entrypoint.sh rehydration env_map with all 23 providers** - `22f7f62` (feat)
2. **Task 2: Add field_map to apply_config for multi-credential providers** - `8d35c01` (feat)

**Plan metadata:** (docs commit follows)

## Files Created/Modified
- `docker/entrypoint.sh` - Rehydration block expanded: azure-openai, litellm, github-copilot, local provider branches added
- `docker/gateway.py` - field_map dict added inside apply_config() for non-standard setup.json field names

## Decisions Made
- azure-openai in setup.html stores `azure_key` (not `api_key`) and `azure_endpoint` -- field_map uses exact setup.html field names
- github-copilot has no stored credentials in setup.html (device flow at runtime), but field_map and rehydration handle api_key->GITHUB_TOKEN for manual configuration scenarios
- litellm host not currently captured by setup.html UI but field_map handles `litellm_host` for future use
- local providers all share `ollama_host` key in setup.json; OLLAMA_HOST exported if present

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Env var pipeline complete for all 23 providers
- Rehydration covers container restart scenarios
- Ready for Phase 02-02 (provider validation logic) and 02-03 (env validation at boot)

## Self-Check: PASSED

- FOUND: docker/entrypoint.sh
- FOUND: docker/gateway.py
- FOUND: .planning/phases/02-validation-and-env-plumbing/02-01-SUMMARY.md
- FOUND commit: 22f7f62 (Task 1)
- FOUND commit: 8d35c01 (Task 2)

---
*Phase: 02-validation-and-env-plumbing*
*Completed: 2026-03-10*

---
phase: 26-fallback-provider-system
plan: 02
subsystem: ui
tags: [setup-wizard, dashboard, drag-and-drop, entrypoint, rehydration]

requires:
  - phase: 26-fallback-provider-system
    provides: fallback_providers validation in validate_setup_config
provides:
  - Fallback provider UI in setup wizard step 3 (main LLM + mem0)
  - Fallback provider section in dashboard settings
  - Drag-to-reorder for fallback chain priority
  - Entrypoint rehydration of FALLBACK_PROVIDERS and MEM0_FALLBACK_PROVIDERS env vars
affects: [26-03-fallback-wiring]

tech-stack:
  added: []
  patterns: [HTML5 drag-and-drop for list reordering, collapsible details element]

key-files:
  created: []
  modified:
    - docker/setup.html
    - docker/entrypoint.sh

key-decisions:
  - "Wizard and dashboard use separate element IDs (wiz* vs dash*) to avoid conflicts"
  - "All known providers shown as fallback options when savedKeysSet is empty (graceful fallback)"
  - "HTML5 drag-and-drop without external library for minimal footprint"

patterns-established:
  - "Fallback UI pattern: collapsible section with drag-to-reorder list + add row"

requirements-completed: [FB-06, FB-07, FB-08]

duration: 4min
completed: 2026-03-25
---

# Phase 26 Plan 02: Fallback UI + Entrypoint Rehydration Summary

**Drag-to-reorder fallback provider config in both setup wizard and dashboard, plus entrypoint env var rehydration for container restarts**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-25T12:03:00Z
- **Completed:** 2026-03-25T12:07:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Setup wizard step 3 has collapsible "Fallback Providers" section with main LLM and mem0 sub-lists
- Dashboard settings has matching fallback config section with summary/edit views
- Both UIs support drag-to-reorder for priority ordering via HTML5 drag events
- Entrypoint rehydrates FALLBACK_PROVIDERS and MEM0_FALLBACK_PROVIDERS as JSON env vars
- collectConfig and saveDashboardChanges include fallback arrays
- Prefill wizard populates fallback lists from existing config

## Task Commits

Each task was committed atomically:

1. **Task 1: Add fallback provider UI to setup wizard and dashboard** - `76d9e65` (feat)
2. **Task 2: Add fallback config rehydration to entrypoint.sh** - `a83aae5` (feat)

## Files Created/Modified
- `docker/setup.html` - CSS, wizard step 3 fallback section, dashboard fallback section, JS functions
- `docker/entrypoint.sh` - FALLBACK_PROVIDERS and MEM0_FALLBACK_PROVIDERS export in rehydration block

## Decisions Made
- Used HTML5 native drag-and-drop (no Sortable.js or other lib) for minimal footprint
- Separate element IDs for wizard vs dashboard fallback lists to avoid DOM conflicts
- When no savedKeysSet data available, show all known providers as options

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- UI ready for user configuration of fallback chains
- Config arrays persist through save/load cycle (validated in Plan 01)
- Ready for Plan 03 to wire fallback chain into live relay paths

---
*Phase: 26-fallback-provider-system*
*Completed: 2026-03-25*

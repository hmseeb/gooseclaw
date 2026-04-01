---
phase: 02-extension-registration-and-boot-lifecycle
plan: 01
subsystem: infra
tags: [json, fcntl, registry, mcp, extensions]

requires:
  - phase: 01-template-engine-and-code-generation
    provides: generator.py conventions (path constants, logging, stdlib-only)
provides:
  - Extension registry CRUD (register, unregister, list_extensions, get_config_entries)
  - Persistent JSON manifest at /data/extensions/registry.json
  - Atomic file writes with fcntl locking
  - Goosed config entry format generation
affects: [02-02, extension-management, boot-lifecycle]

tech-stack:
  added: []
  patterns: [atomic-json-writes, fcntl-file-locking, monkeypatchable-path-constants]

key-files:
  created:
    - docker/extensions/registry.py
    - docker/tests/test_registry.py
  modified: []

key-decisions:
  - "Registry uses fcntl.LOCK_EX for cross-process safety, matching research recommendation"
  - "Corrupt registry file returns empty structure rather than raising, for resilience"
  - "get_config_entries() produces exact goosed extension dict format for direct injection"

patterns-established:
  - "Registry CRUD pattern: _load_registry() / _save_registry() with atomic tmp+replace"
  - "Test fixture pattern: monkeypatch REGISTRY_PATH to tmp_path for isolation"

requirements-completed: [REG-02]

duration: 5min
completed: 2026-04-01
---

# Plan 02-01: Extension Registry Module Summary

**Persistent JSON registry with CRUD operations, fcntl locking, and goosed config format generation**

## Performance

- **Duration:** 5 min
- **Tasks:** 2
- **Files created:** 2

## Accomplishments
- registry.py with register, unregister, list_extensions, get_config_entries functions
- Atomic writes via tmp file + fcntl.flock + os.fsync + os.replace
- Goosed-compatible config entry generation (exact stdio extension format)
- 10 unit tests all passing

## Task Commits

1. **Task 1: Create registry.py with CRUD operations** - `539d9d4` (feat)
2. **Task 2: Add unit tests for registry module** - `d3e47e9` (test)

## Files Created/Modified
- `docker/extensions/registry.py` - Registry CRUD with atomic JSON writes and fcntl locking
- `docker/tests/test_registry.py` - 10 unit tests covering all operations and edge cases

## Decisions Made
None - followed plan as specified.

## Deviations from Plan
None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- registry.py ready for import by gateway.py (Plan 02-02)
- get_config_entries() produces exact format needed for config.yaml injection
- REGISTRY_PATH is monkeypatchable for integration tests

---
*Phase: 02-extension-registration-and-boot-lifecycle*
*Completed: 2026-04-01*

---
phase: 17-vector-knowledge-base
plan: 03
subsystem: infra
tags: [chromadb, mcp, docker, entrypoint, goosehints, migration]

# Dependency graph
requires:
  - phase: 17-01
    provides: "ChromaDB chunker and indexer for system namespace"
  - phase: 17-02
    provides: "FastMCP server with 4 knowledge tools"
provides:
  - "Full deployment pipeline: Dockerfile -> entrypoint -> indexer -> server -> .goosehints"
  - "Memory migration script converting memory.md sections to runtime chunks"
  - "Slim .goosehints without system.md/memory.md/onboarding.md direct loading"
  - "Knowledge MCP extension in default config.yaml"
affects: [deployment, runtime-context]

# Tech tracking
tech-stack:
  added: []
  patterns: [boot-time-indexing, first-boot-migration-flag, knowledge-search-over-file-loading]

key-files:
  created:
    - docker/knowledge/migrate_memory.py
  modified:
    - Dockerfile
    - docker/requirements.txt
    - docker/entrypoint.sh
    - .goosehints
    - docker/test_knowledge.py

key-decisions:
  - "PersistentClient in migration for production use, temp dirs in tests"
  - "Memory migration guarded by .memory_migrated flag file for one-time execution"
  - "pip install via requirements.txt (not separate Dockerfile RUN) for layer caching"
  - "Knowledge base instructions in .goosehints replace direct file loading"

patterns-established:
  - "Boot-time indexer pattern: entrypoint runs indexer.py before gateway starts"
  - "First-boot migration flag: touch file prevents re-migration on subsequent boots"
  - "Semantic search over file inclusion: .goosehints directs to knowledge_search instead of @file"

requirements-completed: [KB-07, KB-08]

# Metrics
duration: 3min
completed: 2026-03-15
---

# Phase 17 Plan 03: Deployment Pipeline Integration Summary

**Memory migration, Dockerfile deps, entrypoint boot hooks, and .goosehints rewrite from direct file loading to semantic knowledge search**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-14T22:54:48Z
- **Completed:** 2026-03-14T22:58:04Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- migrate_memory.py converts memory.md sections into typed runtime chunks with idempotent upsert
- Dockerfile installs chromadb and mcp via pip requirements.txt
- Entrypoint runs indexer on every boot (system namespace), memory migration on first boot only
- .goosehints drops ~6,000 tokens by removing system.md, memory.md, and onboarding.md direct loading
- Knowledge MCP extension registered in default config.yaml extensions block
- 10 new tests (4 TestMigration + 6 TestGoosehints), 48 total passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Memory migration script and tests** - `7c89327` (feat)
2. **Task 2: Dockerfile, entrypoint, .goosehints, and extension wiring** - `e2f118e` (feat)

**Plan metadata:** pending (docs: complete plan)

## Files Created/Modified
- `docker/knowledge/migrate_memory.py` - One-time memory.md to runtime chunks migration
- `Dockerfile` - Added pip install step for requirements.txt
- `docker/requirements.txt` - Added chromadb and mcp[cli] dependencies
- `docker/entrypoint.sh` - Knowledge indexer boot hook, memory migration, extension registration
- `.goosehints` - Removed system.md/memory.md/onboarding.md, added knowledge base instructions
- `docker/test_knowledge.py` - 10 new tests (TestMigration + TestGoosehints)

## Decisions Made
- pip install via requirements.txt in Dockerfile (not separate pip3 install lines) for layer caching
- Migration uses PersistentClient directly (not injected) since it writes to disk
- Migration guarded by .memory_migrated flag file to prevent re-running on every boot
- .goosehints knowledge instructions explain all 3 tools with examples

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed PersistentClient temp dir cleanup in tests**
- **Found during:** Task 1
- **Issue:** Tests verified ChromaDB data after tmpdir was cleaned up, causing "Error purging logs"
- **Fix:** Moved all ChromaDB assertions inside the `with tempfile.TemporaryDirectory()` block
- **Files modified:** docker/test_knowledge.py
- **Verification:** All 4 TestMigration tests pass
- **Committed in:** 7c89327

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Test isolation fix was necessary for ChromaDB PersistentClient cleanup. No scope creep.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Full knowledge base pipeline complete: chunker -> indexer -> server -> integration
- All 48 tests passing across both test files
- Phase 17 complete (3/3 plans done)

---
*Phase: 17-vector-knowledge-base*
*Completed: 2026-03-15*

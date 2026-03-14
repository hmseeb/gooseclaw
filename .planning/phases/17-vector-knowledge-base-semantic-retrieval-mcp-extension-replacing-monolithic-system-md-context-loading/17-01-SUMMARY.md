---
phase: 17-vector-knowledge-base
plan: 01
subsystem: knowledge
tags: [chromadb, vector-db, chunking, indexer, markdown-parsing]

# Dependency graph
requires:
  - phase: 16-watcher-engine
    provides: stable gateway codebase
provides:
  - chunk_file() markdown splitter with type inference and hierarchical IDs
  - run_index() deploy-time re-indexer for system namespace
  - Test scaffold covering KB-01, KB-05, KB-06, KB-09
affects: [17-02 MCP server, 17-03 integration]

# Tech tracking
tech-stack:
  added: [chromadb]
  patterns: [two-namespace architecture (system/runtime), typed chunks with metadata, hierarchical dot-notation IDs]

key-files:
  created:
    - docker/knowledge/__init__.py
    - docker/knowledge/chunker.py
    - docker/knowledge/indexer.py
    - docker/test_knowledge.py
  modified: []

key-decisions:
  - "EphemeralClient for test isolation, PersistentClient for production"
  - "Default chunk type is 'procedure' since most system.md content is procedural"
  - "Indexer accepts client/identity_dir params for testability, falls back to env vars"

patterns-established:
  - "Two-namespace pattern: system (wiped on deploy) vs runtime (never wiped)"
  - "Chunk metadata schema: type/source/section/namespace/refs/key"
  - "Hierarchical IDs: source.section.subsection in dot-notation"

requirements-completed: [KB-01, KB-05, KB-06, KB-09]

# Metrics
duration: 3min
completed: 2026-03-15
---

# Phase 17 Plan 01: Chunker Pipeline and Deploy-Time Indexer Summary

**Markdown chunker splits system.md by ## and ### boundaries into typed chunks with hierarchical IDs, indexed into ChromaDB with system/runtime namespace separation**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-14T22:45:35Z
- **Completed:** 2026-03-14T22:48:30Z
- **Tasks:** 1
- **Files modified:** 4

## Accomplishments
- chunk_file() splits markdown files by ## and ### sections with correct metadata
- _infer_type() categorizes chunks as procedure/schema/fact/preference/integration
- Indexer wipes system collection on deploy, preserves runtime collection untouched
- 21 tests covering chunking, type inference, cross-refs, and indexer behavior

## Task Commits

Each task was committed atomically:

1. **Task 1: TDD - Chunker and indexer with test scaffold** - `5180744` (feat)

**Plan metadata:** pending (docs: complete plan)

## Files Created/Modified
- `docker/knowledge/__init__.py` - Package init (empty)
- `docker/knowledge/chunker.py` - Markdown-to-chunks splitter with _make_id, _infer_type
- `docker/knowledge/indexer.py` - Deploy-time re-indexer for system namespace
- `docker/test_knowledge.py` - 21 tests for TestChunker, TestChunkerTypeInference, TestChunkerCrossRefs, TestIndexer

## Decisions Made
- Used EphemeralClient in tests for isolation (no disk), PersistentClient in production
- Default chunk type is "procedure" since most system.md content is procedural rules
- Indexer's run_index() accepts optional client and identity_dir params for testability

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed EphemeralClient test isolation in chromadb 1.5.1**
- **Found during:** Task 1 (GREEN phase)
- **Issue:** chromadb 1.5.1 EphemeralClient shares state across instances in the same process, causing create_collection to fail with "already exists"
- **Fix:** Added try/delete_collection before create_collection in test setup
- **Files modified:** docker/test_knowledge.py
- **Verification:** All 21 tests pass
- **Committed in:** 5180744

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Test isolation fix was necessary for chromadb 1.5.1 compatibility. No scope creep.

## Issues Encountered
- mcp package requires Python 3.10+, unavailable on macOS system Python 3.9. Not needed for this plan (chunker/indexer only). Will need resolution in Plan 17-02.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- chunker.py and indexer.py ready for import by Plan 17-02 (MCP server)
- Test scaffold ready for extension with KB-02, KB-03, KB-04, KB-10 tests
- chromadb installed and verified working

---
*Phase: 17-vector-knowledge-base*
*Completed: 2026-03-15*

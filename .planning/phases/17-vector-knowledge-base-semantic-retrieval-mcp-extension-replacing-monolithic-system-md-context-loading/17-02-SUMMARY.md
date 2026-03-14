---
phase: 17-vector-knowledge-base
plan: 02
subsystem: api
tags: [chromadb, mcp, fastmcp, vector-search, knowledge-base]

requires:
  - phase: 17-01
    provides: "ChromaDB chunker and indexer (parallel, no hard dependency)"
provides:
  - "FastMCP stdio server with 4 knowledge tools"
  - "knowledge_search with scored, merged results from system+runtime"
  - "knowledge_upsert for runtime chunk persistence"
  - "knowledge_get for exact key lookup"
  - "knowledge_delete with system chunk protection"
affects: [17-03, goose-config, entrypoint]

tech-stack:
  added: [chromadb, mcp, fastmcp]
  patterns: [two-namespace-collections, monkey-patch-test-isolation, stderr-only-logging]

key-files:
  created:
    - docker/knowledge/server.py
    - docker/test_server.py
  modified: []

key-decisions:
  - "EphemeralClient fallback in server.py for import safety when /data/knowledge/chroma doesn't exist"
  - "Tests use monkey-patched module-level collections with EphemeralClient for isolation"
  - "Search merges results from both collections then sorts by score descending"
  - "Delete checks system collection first and refuses before checking runtime"

patterns-established:
  - "MCP tool functions are regular Python functions testable without running MCP server"
  - "Module-level collection variables monkey-patched in tests for isolation"

requirements-completed: [KB-02, KB-03, KB-04, KB-10]

duration: 4min
completed: 2026-03-15
---

# Phase 17 Plan 02: MCP Server Tools Summary

**FastMCP stdio server with 4 knowledge tools (search, upsert, get, delete) wrapping ChromaDB two-namespace architecture, 17 tests passing**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-14T22:45:29Z
- **Completed:** 2026-03-14T22:49:31Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 3

## Accomplishments
- knowledge_search returns scored, merged results from both system and runtime collections with type filtering and limit capping
- knowledge_upsert writes typed chunks to runtime collection only with full metadata
- knowledge_get does exact key lookup across both collections with formatted output
- knowledge_delete refuses system chunks (rebuilt on deploy), removes runtime chunks
- 17 tests covering all tool behaviors including edge cases

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests for 4 MCP tools** - `c3fdc3c` (test)
2. **Task 1 GREEN: Implement FastMCP server** - `8c062d5` (feat)

## Files Created/Modified
- `docker/knowledge/__init__.py` - Package init (empty)
- `docker/knowledge/server.py` - FastMCP stdio server with 4 knowledge tools
- `docker/test_server.py` - 17 tests covering KB-02, KB-03, KB-04, KB-10

## Decisions Made
- EphemeralClient fallback when PersistentClient path doesn't exist (import safety for tests/dev)
- Tests monkey-patch module-level system_col/runtime_col with EphemeralClient collections
- Search merges both collections then sorts by score descending, takes top N
- Delete checks system collection first and refuses before checking runtime
- All logging to stderr only (MCP stdio protocol safety)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- mcp package requires Python 3.10+, system python3 is 3.9.6. Used /opt/homebrew/bin/python3.13 for testing.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Server tools ready for Plan 03 (goose config integration, .goosehints update, entrypoint wiring)
- Plan 01 (chunker/indexer) runs in parallel, no blocking dependency

---
*Phase: 17-vector-knowledge-base*
*Completed: 2026-03-15*

---
phase: 25-neo4j-knowledge-graph
plan: 01
subsystem: infra
tags: [neo4j, graph-database, docker, mem0, knowledge-graph]

requires:
  - phase: 24-chromadb-migration-cleanup
    provides: mem0 vector memory system with ChromaDB backend
provides:
  - Neo4j Community Edition installed in Docker image
  - Entrypoint starts Neo4j as background process with readiness wait
  - mem0 graph_store config gated behind NEO4J_ENABLED env var
  - Graph memory is optional and degrades gracefully
affects: [25-neo4j-knowledge-graph]

tech-stack:
  added: [neo4j, openjdk-21, mem0ai[graph], langchain-neo4j]
  patterns: [conditional-config-via-env-var, background-process-with-readiness-wait]

key-files:
  created: []
  modified:
    - Dockerfile
    - docker/entrypoint.sh
    - docker/requirements.txt
    - docker/mem0_config.py
    - docker/test_mem0_config.py
    - docker/tests/test_entrypoint.py

key-decisions:
  - "Neo4j installed via official Debian apt repo (not manual binary)"
  - "JVM heap constrained to 256m/512m to avoid OOM on Railway"
  - "NEO4J_AUTH=none since Neo4j only accessible on localhost"
  - "NEO4J_ENABLED env var gates graph_store inclusion in mem0 config"

patterns-established:
  - "Background process pattern: start, wait for readiness, export flag on success"
  - "Conditional config: check env var truthy value to include optional sections"

requirements-completed: [GRAPH-01, GRAPH-02]

duration: 5min
completed: 2026-03-20
---

# Plan 25-01: Neo4j In-Container Install Summary

**Neo4j Community with OpenJDK 21 in Docker image, entrypoint background startup with 60s Bolt readiness wait, and conditional mem0 graph_store config gated by NEO4J_ENABLED**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-20
- **Completed:** 2026-03-20
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Neo4j Community Edition installed in Docker image via official Debian repo
- Entrypoint starts Neo4j as background process with 60s readiness wait, constrained JVM heap (256m/512m), pagecache (128m)
- mem0 config conditionally includes graph_store when NEO4J_ENABLED=true
- 5 new tests covering graph config and entrypoint (all 26 total tests pass)

## Task Commits

Each task was committed atomically:

1. **Task 1: Install Neo4j in Dockerfile, add graph extras, and add entrypoint startup block** - `4149726` (feat)
2. **Task 2: Add graph_store to mem0 config and write all tests** - `d052137` (feat)

## Files Created/Modified
- `Dockerfile` - Added Neo4j apt-get install layer, wget+gpg to base packages
- `docker/requirements.txt` - Changed mem0ai to mem0ai[graph] for graph extras
- `docker/entrypoint.sh` - Added Neo4j background startup block with readiness wait
- `docker/mem0_config.py` - Conditional graph_store section when NEO4J_ENABLED=true
- `docker/test_mem0_config.py` - 4 new graph_store config tests
- `docker/tests/test_entrypoint.py` - 1 new Neo4j entrypoint source test

## Decisions Made
- Used official Neo4j Debian repo instead of manual binary download (cleaner, auto-pulls OpenJDK)
- NEO4J_AUTH=none since the database is only accessible on localhost inside the container
- Graph memory degrades gracefully: if Neo4j fails to start, NEO4J_ENABLED stays unset and mem0 runs in vector-only mode

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added wget and gpg to base Dockerfile packages**
- **Found during:** Task 1 (Dockerfile changes)
- **Issue:** Neo4j repo key import requires wget and gpg, not in base image
- **Fix:** Added wget and gpg to the apt-get install line
- **Files modified:** Dockerfile
- **Verification:** Build layer would succeed with these packages
- **Committed in:** 4149726 (Task 1 commit)

**2. [Rule 3 - Blocking] Added keyrings directory creation**
- **Found during:** Task 1 (Dockerfile changes)
- **Issue:** /etc/apt/keyrings may not exist on ubuntu:22.04
- **Fix:** Added mkdir -p /etc/apt/keyrings before gpg --dearmor
- **Files modified:** Dockerfile
- **Verification:** Directory creation is idempotent
- **Committed in:** 4149726 (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (2 blocking)
**Impact on plan:** Both auto-fixes necessary for Dockerfile correctness. No scope creep.

## Issues Encountered
None

## User Setup Required
None - Neo4j starts automatically inside the container, zero user configuration.

## Next Phase Readiness
- Neo4j infrastructure ready for Plan 25-02 to build graph-augmented search and entity/relationship MCP tools
- NEO4J_ENABLED flag is the integration point for Plan 25-02's server.py changes

---
*Phase: 25-neo4j-knowledge-graph*
*Completed: 2026-03-20*

# Requirements: GooseClaw v5.0 mem0 Memory Layer

**Defined:** 2026-03-20
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v5.0 Requirements

### Memory MCP Extension

- [x] **MEM-01**: Bot can store memories from conversations via `memory_add` MCP tool
- [x] **MEM-02**: Bot can search memories semantically via `memory_search` MCP tool
- [x] **MEM-03**: Bot can delete specific memories via `memory_delete` MCP tool
- [x] **MEM-04**: Bot can list all memories for a user via `memory_list` MCP tool
- [x] **MEM-05**: Bot can view memory evolution via `memory_history` MCP tool
- [x] **MEM-06**: mem0 MCP server runs as stdio extension registered in config.yaml

### Memory Configuration

- [x] **CFG-01**: mem0 uses ChromaDB as vector store (existing, zero new infra)
- [x] **CFG-02**: mem0 LLM extraction reuses user's existing provider from vault/setup.json
- [x] **CFG-03**: mem0 extraction routes to a cheap model automatically (not user's main model)
- [x] **CFG-04**: Shared config module builds mem0 config from environment variables

### Gateway Integration

- [x] **GW-01**: Gateway memory writer uses mem0.add() instead of manual chromadb extraction
- [x] **GW-02**: Memory extraction runs async in background thread with timeout (no blocking)
- [x] **GW-03**: Identity routing preserved — user.md/soul.md stay file-based, mem0 handles knowledge only
- [x] **GW-04**: Identity/knowledge routing rule enforced: traits stable 6+ months (name, role, preferences, communication style) → user.md via separate prompt. Everything else (projects, facts, events, integrations) → mem0 via add(). No duplication between the two.

### Migration

- [x] **MIG-01**: One-time migration script moves chromadb runtime memories to mem0
- [x] **MIG-02**: Migration bypasses mem0.add() (direct insert, no re-extraction)
- [x] **MIG-03**: ChromaDB runtime collection deprecated after migration (system collection stays)
- [x] **MIG-04**: Sentinel file prevents accidental re-migration

### Knowledge Graph

- [x] **GRAPH-01**: Neo4j runs inside the same container, started by entrypoint, data on /data volume
- [x] **GRAPH-02**: mem0 graph memory enabled for entity relationship extraction
- [x] **GRAPH-03**: Relationship-enhanced search (graph augments vector results)
- [x] **GRAPH-04**: Entity and relationship tools exposed via MCP (memory_entities, memory_relations)

## v5.1 Requirements

### Fallback Provider System

- [x] **FB-01**: Error classification distinguishes retriable errors (429, 5xx, timeout, connection) from permanent errors (401, 403, 400)
- [x] **FB-02**: Main LLM (goose agent) tries fallback providers in user-defined order when primary fails with retriable error
- [x] **FB-03**: mem0 extraction LLM tries fallback providers in user-defined order when primary fails
- [x] **FB-04**: Fallback provider config validated in `validate_setup_config()` (provider exists, model present, provider has API key)
- [x] **FB-05**: Fallback config persists in setup.json as `fallback_providers` and `mem0_fallback_providers` arrays
- [x] **FB-06**: Setup wizard (first-time setup, step 3) includes fallback provider configuration with drag-to-reorder
- [x] **FB-07**: Dashboard settings (post-setup) includes fallback provider configuration with drag-to-reorder
- [x] **FB-08**: Entrypoint rehydrates fallback provider env vars from setup.json on container restart
- [x] **FB-09**: Primary provider is always tried first on each new message (fallback is transient, not sticky)

## v5.x Requirements (Future)

### Enhanced Memory

- **ENH-01**: Per-turn memory injection via MOIM (search before each response)
- **ENH-02**: Memory categories/tagging via metadata
- **ENH-03**: Custom extraction prompts matching GooseClaw's personality

## Out of Scope

| Feature | Reason |
|---------|--------|
| mem0 cloud/managed service | Violates self-hosted principle |
| pgvector / separate PostgreSQL | ChromaDB backend avoids new infra and embedding costs |
| Neo4j as separate Railway service | Runs inside the same container instead — zero extra cost |
| Replacing user.md/soul.md with mem0 | Different access patterns: identity = always-present, memory = on-demand |
| Real-time memory streaming in chat | Violates "show results, hide plumbing" |
| OpenAI embedding key requirement | ChromaDB bundles its own embedder at zero cost |
| Circuit breaker with open/half-open/closed states | Overkill for single-container single-process. Simple ordered fallback chain sufficient. |
| Exponential backoff between fallback attempts | Switching providers, not retrying same one. Minimal delay is fine. |
| Persistent fallback state across restarts | Fallback is transient resilience. Always start from primary on restart. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| MEM-01 | Phase 22 | Complete |
| MEM-02 | Phase 22 | Complete |
| MEM-03 | Phase 22 | Complete |
| MEM-04 | Phase 22 | Complete |
| MEM-05 | Phase 22 | Complete |
| MEM-06 | Phase 22 | Complete |
| CFG-01 | Phase 22 | Complete |
| CFG-02 | Phase 22 | Complete |
| CFG-03 | Phase 22 | Complete |
| CFG-04 | Phase 22 | Complete |
| GW-01 | Phase 23 | Complete |
| GW-02 | Phase 23 | Complete |
| GW-03 | Phase 23 | Complete |
| GW-04 | Phase 23 | Complete |
| MIG-01 | Phase 24 | Complete |
| MIG-02 | Phase 24 | Complete |
| MIG-03 | Phase 24 | Complete |
| MIG-04 | Phase 24 | Complete |
| GRAPH-01 | Phase 25 | Complete |
| GRAPH-02 | Phase 25 | Complete |
| GRAPH-03 | Phase 25 | Complete |
| GRAPH-04 | Phase 25 | Complete |
| FB-01 | Phase 26 | Complete |
| FB-02 | Phase 26 | Complete |
| FB-03 | Phase 26 | Complete |
| FB-04 | Phase 26 | Complete |
| FB-05 | Phase 26 | Complete |
| FB-06 | Phase 26 | Complete |
| FB-07 | Phase 26 | Complete |
| FB-08 | Phase 26 | Complete |
| FB-09 | Phase 26 | Complete |

**Coverage:**
- v5.0 requirements: 22 total, all complete
- v5.1 fallback requirements: 9 total, all complete
- Unmapped: 0

---
*Requirements defined: 2026-03-20*
*Last updated: 2026-03-25 after Phase 26 planning*

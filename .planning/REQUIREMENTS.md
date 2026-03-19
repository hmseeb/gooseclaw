# Requirements: GooseClaw v5.0 mem0 Memory Layer

**Defined:** 2026-03-20
**Core Value:** A user with zero DevOps knowledge can deploy and configure GooseClaw correctly on the first try

## v5.0 Requirements

### Memory MCP Extension

- [ ] **MEM-01**: Bot can store memories from conversations via `memory_add` MCP tool
- [ ] **MEM-02**: Bot can search memories semantically via `memory_search` MCP tool
- [ ] **MEM-03**: Bot can delete specific memories via `memory_delete` MCP tool
- [ ] **MEM-04**: Bot can list all memories for a user via `memory_list` MCP tool
- [ ] **MEM-05**: Bot can view memory evolution via `memory_history` MCP tool
- [ ] **MEM-06**: mem0 MCP server runs as stdio extension registered in config.yaml

### Memory Configuration

- [ ] **CFG-01**: mem0 uses ChromaDB as vector store (existing, zero new infra)
- [ ] **CFG-02**: mem0 LLM extraction reuses user's existing provider from vault/setup.json
- [ ] **CFG-03**: mem0 extraction routes to a cheap model automatically (not user's main model)
- [ ] **CFG-04**: Shared config module builds mem0 config from environment variables

### Gateway Integration

- [x] **GW-01**: Gateway memory writer uses mem0.add() instead of manual chromadb extraction
- [x] **GW-02**: Memory extraction runs async in background thread with timeout (no blocking)
- [x] **GW-03**: Identity routing preserved — user.md/soul.md stay file-based, mem0 handles knowledge only
- [x] **GW-04**: Identity/knowledge routing rule enforced: traits stable 6+ months (name, role, preferences, communication style) → user.md via separate prompt. Everything else (projects, facts, events, integrations) → mem0 via add(). No duplication between the two.

### Migration

- [ ] **MIG-01**: One-time migration script moves chromadb runtime memories to mem0
- [ ] **MIG-02**: Migration bypasses mem0.add() (direct insert, no re-extraction)
- [ ] **MIG-03**: ChromaDB runtime collection deprecated after migration (system collection stays)
- [ ] **MIG-04**: Sentinel file prevents accidental re-migration

### Knowledge Graph

- [ ] **GRAPH-01**: Neo4j runs inside the same container, started by entrypoint, data on /data volume
- [ ] **GRAPH-02**: mem0 graph memory enabled for entity relationship extraction
- [ ] **GRAPH-03**: Relationship-enhanced search (graph augments vector results)
- [ ] **GRAPH-04**: Entity and relationship tools exposed via MCP (memory_entities, memory_relations)

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
| Memory UI in setup wizard | The agent IS the memory interface |
| OpenAI embedding key requirement | ChromaDB bundles its own embedder at zero cost |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| MEM-01 | Phase 22 | Pending |
| MEM-02 | Phase 22 | Pending |
| MEM-03 | Phase 22 | Pending |
| MEM-04 | Phase 22 | Pending |
| MEM-05 | Phase 22 | Pending |
| MEM-06 | Phase 22 | Pending |
| CFG-01 | Phase 22 | Pending |
| CFG-02 | Phase 22 | Pending |
| CFG-03 | Phase 22 | Pending |
| CFG-04 | Phase 22 | Pending |
| GW-01 | Phase 23 | Complete |
| GW-02 | Phase 23 | Complete |
| GW-03 | Phase 23 | Complete |
| GW-04 | Phase 23 | Complete |
| MIG-01 | Phase 24 | Pending |
| MIG-02 | Phase 24 | Pending |
| MIG-03 | Phase 24 | Pending |
| MIG-04 | Phase 24 | Pending |
| GRAPH-01 | Phase 25 | Pending |
| GRAPH-02 | Phase 25 | Pending |
| GRAPH-03 | Phase 25 | Pending |
| GRAPH-04 | Phase 25 | Pending |

**Coverage:**
- v5.0 requirements: 22 total
- Mapped to phases: 22
- Unmapped: 0 ✓

---
*Requirements defined: 2026-03-20*
*Last updated: 2026-03-20 after initial definition*

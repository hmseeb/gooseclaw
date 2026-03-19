# Project Research Summary

**Project:** GooseClaw v5.0 mem0 Memory Layer Integration
**Domain:** AI agent memory system (vector + knowledge graph) for self-hosted personal agent
**Researched:** 2026-03-19
**Confidence:** HIGH (core vector path), MEDIUM (graph memory, Neo4j maturity)

## Executive Summary

GooseClaw's current memory system stores user knowledge in ChromaDB with a hand-rolled extraction pipeline: idle detection, LLM-based JSON extraction, manual dedup, manual upsert. This works but lacks contradiction resolution (old facts accumulate instead of updating), has no relationship modeling, and the extraction code is ~400 lines of brittle JSON parsing. mem0 is an open-source Python library that replaces all of this with a single `add()` call that handles fact extraction, embedding, contradiction resolution (ADD/UPDATE/DELETE/NOOP), and optional knowledge graph storage. The recommended approach is to integrate mem0 in open-source mode (no Mem0 cloud API), backed by pgvector on a separate Railway PostgreSQL service, with Neo4j deferred to a later phase.

The architecture is a hybrid: mem0 runs as both an embedded Python library inside gateway.py (for the background memory writer) and as a new MCP server (for goose tool access during conversations). Both point at the same pgvector backend. ChromaDB stays for system docs only. The MCP server follows the exact same FastMCP stdio pattern as the existing knowledge extension. A shared config module (`mem0_config.py`) builds the mem0 configuration from environment variables, mapping the user's existing LLM provider (from vault) to mem0's provider format. No new API keys required for the core path, only OpenAI for embeddings (most users already have one).

The primary risks are: (1) mem0's `add()` blocks for 2-20 seconds due to its multi-LLM pipeline, which will stall the memory writer if not handled async with timeouts, (2) pip dependency conflicts between mem0ai and existing chromadb (protobuf version clash via qdrant-client), requiring careful dependency isolation, and (3) the ChromaDB-to-mem0 migration must bypass mem0's extraction pipeline to avoid re-processing already-extracted facts (direct pgvector insert with re-embedding, not `mem0.add()`). Neo4j is explicitly deferred because it triples Railway costs and adds 800MB-1.2GB RAM overhead for marginal value. Vector-only mem0 delivers 80% of the benefit.

## Key Findings

### Recommended Stack

mem0 in open-source mode is the only library that combines vector search, LLM-powered contradiction resolution, entity extraction, and optional graph memory in a single package. It replaces ~400 lines of custom extraction code with one function call. The infrastructure adds a Railway PostgreSQL+pgvector service ($5-15/mo) and optionally a Neo4j service ($5-10/mo, deferred). See [STACK.md](STACK.md) for full details.

**Core technologies:**
- **mem0ai 1.0.5** (`pip install mem0ai[graph]`): memory layer with vector + graph support. Handles extraction, contradiction resolution, dedup, and consolidation. Active development, v1.0.x stable line.
- **PostgreSQL + pgvector** (Railway service): vector similarity search for memory embeddings. One-click Railway template, $5-15/mo. Replaces ChromaDB for user memories.
- **OpenAI text-embedding-3-small** (1536 dims): embedding model. Best quality/cost ratio for cloud deployment. $0.02/1M tokens. Most users already have an OpenAI key.
- **FastMCP** (from existing mcp[cli] 1.26.0): MCP server framework. Same pattern as knowledge extension, no new dependency.
- **Neo4j 5.x** (Railway service, Phase 2 only): knowledge graph for entity relationships. Optional, deferred, $5-10/mo additional.

**Critical version requirements:**
- mem0ai 1.0.5 requires Python >=3.9 (our 3.10 is fine)
- `embedding_model_dims` MUST match embedder output (1536 for text-embedding-3-small). Mismatch causes DataException on every insert.
- psycopg2-binary 2.9.11 avoids needing libpq-dev in the container

### Expected Features

See [FEATURES.md](FEATURES.md) for full prioritization matrix, dependency graph, and MCP tool designs.

**Must have (table stakes, P0):**
- Semantic memory search replacing ChromaDB knowledge_search for user memories
- Automatic memory extraction via mem0.add() replacing the manual LLM+JSON pipeline
- Contradiction resolution (new facts update old ones, automatic via mem0)
- Memory deduplication (automatic via mem0 consolidation)
- Manual memory CRUD tools via MCP (search, add, get, delete, list, history)
- Identity routing to user.md preserved (soul.md/user.md remain identity source of truth)
- ChromaDB retained for system docs (separate concern, unchanged)
- Migration from ChromaDB runtime collection to mem0
- LLM extraction reuses user's existing provider from vault

**Should have (P1, include if time permits):**
- Memory categories/tagging via metadata
- Memory history/audit trail (mem0 built-in)
- Custom extraction prompts matching GooseClaw's turn-rules philosophy
- Scoped memories (user_id) for future multi-user support

**Defer (P2, requires Neo4j):**
- Entity relationship extraction and knowledge graph
- Relationship-enhanced search (graph augments vector results)
- Multi-hop entity traversal
- Entity listing tool

**Anti-features (do not build):**
- mem0 cloud/managed service (violates self-hosted principle)
- Replacing user.md/soul.md with mem0 (different access patterns: identity is always-present, memory is on-demand)
- Real-time memory streaming (clutters chat, violates "show results, hide plumbing")
- Agent-managed memory Letta-style (unpredictable, mem0's pipeline is deterministic)
- Complex memory UI in setup wizard (the agent IS the memory interface)

### Architecture Approach

Hybrid integration: mem0 embedded in gateway.py for the background memory writer, plus a separate MCP server for goose tool access. Both share config pointing at the same pgvector backend. ChromaDB narrows to system docs only. Railway multi-service topology with private networking. See [ARCHITECTURE.md](ARCHITECTURE.md) for full component diagram, data flows, code scaffolds, and Railway deployment details.

**Major components:**
1. **mem0 MCP server** (docker/mem0_mcp/server.py) -- exposes memory_search, memory_add, memory_get_all, memory_delete to goose via stdio
2. **gateway.py memory writer** (modified) -- replaces `_process_memory_extraction()` with `mem0.add(messages)`, splitting the prompt into identity-only extraction for user.md
3. **mem0_config.py** (new shared module) -- builds mem0 config dict from env vars, maps GOOSE_PROVIDER to mem0 provider names, shared by both gateway and MCP server
4. **pgvector Railway service** -- PostgreSQL with vector extension, stores embeddings and mem0 history tables
5. **ChromaDB** (scope narrowed) -- system namespace only, runtime collection deprecated after migration
6. **Migration script** (docker/mem0_mcp/migrate_from_chromadb.py) -- one-time direct pgvector insert, sentinel file prevents re-run

### Critical Pitfalls

See [PITFALLS.md](PITFALLS.md) for all 12 pitfalls with recovery strategies and "looks done but isn't" checklist.

1. **mem0.add() blocks 2-20s per call** -- use AsyncMemory or threaded with hard 30s timeout, route extraction to a cheap model (gpt-4.1-nano), start without graph memory (2x faster)
2. **Dependency conflicts between mem0ai and chromadb** -- mem0 pulls qdrant-client which conflicts with chromadb on protobuf versions. Use pgvector backend (not Qdrant), consider separate requirements files, never import mem0 in gateway.py directly
3. **Railway cost explosion with Neo4j** -- Neo4j needs 800MB-1.2GB RAM minimum (JVM). Defer to optional Phase 3+. Vector-only pgvector is one service, not two. Document cost increase clearly.
4. **ChromaDB migration loses data** -- do NOT migrate through mem0.add() (triggers re-extraction). Direct pgvector insert with re-embedding. Keep ChromaDB read-only as fallback for 1+ week.
5. **LLM token burn without attribution** -- mem0 makes 3-6 internal LLM calls per add(). Route to a cheap extraction model, log all calls with token counts, disclose cost in setup wizard.

## Implications for Roadmap

Based on the dependency graph in FEATURES.md, the build order in ARCHITECTURE.md, and the phase warnings in PITFALLS.md, a 4-phase structure is right. The critical path is: pgvector setup -> mem0 MCP server -> gateway extraction rewrite -> migration. Neo4j branches off independently after vector memory is stable.

### Phase 1: Infrastructure and MCP Server

**Rationale:** pgvector must exist before anything else works. The MCP server is the simplest integration (no gateway changes) and proves mem0 works end-to-end. This phase validates the entire stack before touching the complex memory writer code.
**Delivers:** Working mem0 MCP server with memory_search, memory_add, memory_get_all, memory_delete tools. Goose can store and search memories during conversations.
**Addresses:** pgvector setup, mem0 library installation, MCP server creation, extension registration, LLM provider mapping from vault, embedding model configuration
**Avoids:** Pitfall 2 (dependency conflicts -- resolve during requirements.txt changes), Pitfall 6 (startup race -- add retry logic), Pitfall 8 (telemetry -- set MEM0_TELEMETRY=false)
**Stack:** mem0ai[graph] 1.0.5, psycopg2-binary 2.9.11, pgvector 0.4.2, PostgreSQL+pgvector Railway service, FastMCP (existing)

### Phase 2: Gateway Memory Writer Migration

**Rationale:** With the MCP server proven, rewrite the gateway's background extraction to use mem0.add(). This is where the biggest code simplification happens (~150 lines removed). Must handle the identity routing split carefully: mem0 for knowledge, narrowed prompt for identity traits to user.md.
**Delivers:** Automatic end-of-session memory extraction via mem0, simplified identity-only extraction for user.md, ~150 lines of extraction code removed from gateway.py
**Addresses:** Gateway auto-extraction rewrite, identity routing preservation, memory_writer_enabled toggle integration
**Avoids:** Pitfall 1 (blocking add() -- use timeout and cheap model), Pitfall 5 (token burn -- configure cheap extraction model), Pitfall 11 (sync/async mismatch -- use synchronous Memory with timeout in thread)

### Phase 3: ChromaDB Migration and Cleanup

**Rationale:** Migration comes AFTER both the MCP server and gateway writer are proven working with new memories. Migrating simultaneously with building is a recipe for data loss. This phase also handles the dual-store transition period.
**Delivers:** All existing ChromaDB runtime memories migrated to mem0/pgvector, ChromaDB runtime collection deprecated (kept read-only as backup), unified memory access through mem0 only
**Addresses:** ChromaDB runtime -> mem0 migration, dual-store transition, knowledge MCP scope narrowing
**Avoids:** Pitfall 4 (migration data loss -- direct pgvector insert, dry-run first, keep ChromaDB backup), Pitfall 7 (inconsistent recall -- migrate all before removing old tools), Pitfall 9 (embedding mismatch -- re-embed everything with text-embedding-3-small)

### Phase 4: Knowledge Graph and Polish (Optional)

**Rationale:** Neo4j adds entity relationships but triples Railway costs and adds significant infrastructure complexity. Only pursue after vector memory is stable and the user wants relationship-aware search. This phase also includes Telegram commands and setup wizard integration.
**Delivers:** Neo4j Railway service, entity/relationship extraction, relationship-enhanced search, memory-related Telegram commands (/remember, /memories, /forget), setup wizard memory settings
**Addresses:** Neo4j graph store config, entity tools (memory_entities, memory_relations), Telegram integration, builtin memory extension disable
**Avoids:** Pitfall 3 (cost explosion -- make Neo4j optional, document cost, set explicit memory limits), Pitfall 10 (Bolt protocol -- use Railway private networking)

### Phase Ordering Rationale

- **Infrastructure first:** pgvector must exist before any mem0 code can run. Proving the MCP server works validates the entire dependency chain (mem0 -> pgvector -> embedder -> LLM provider) before touching gateway.py
- **Gateway second:** the memory writer rewrite is the highest-complexity change (threading, async, identity routing split). It needs the MCP server as a reference implementation.
- **Migration third:** never migrate and build simultaneously. Existing memories are safe in ChromaDB until mem0 is proven. Migration is a one-way operation that must be done carefully.
- **Neo4j last and optional:** 80% of the value comes from vector memory. Graph memory is a pure enhancement with disproportionate infrastructure cost.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 2 (Gateway Writer):** The identity routing split (mem0 for knowledge, narrowed prompt for user.md identity) needs careful prompt engineering. The threading/async model for mem0.add() in gateway.py's daemon thread needs prototyping. Existing `_memory_writer_loop()` code is ~400 lines and tightly coupled.
- **Phase 3 (Migration):** Direct pgvector insert bypassing mem0's API requires understanding mem0's internal table schema. May need to read mem0 source code to match the expected row format. Embedding re-computation cost for N existing memories needs estimation.
- **Phase 4 (Neo4j):** mem0 issue #3711 (structuredLlm hardcoded to OpenAI) may still affect non-OpenAI users. Needs validation against mem0 v1.0.5 before shipping graph memory for Anthropic users.

Phases with standard patterns (skip research-phase):
- **Phase 1 (Infrastructure + MCP):** FastMCP server pattern is identical to existing knowledge/server.py. Railway pgvector template is one-click. mem0 config is well-documented. Dependency resolution is the only risk and can be validated with a test build.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | mem0ai 1.0.5 verified on PyPI. pgvector Railway template verified (Mar 2026). All version compatibility confirmed. OpenAI embedding default is practical for cloud deployment. |
| Features | HIGH | Derived from direct codebase analysis (gateway.py memory writer, knowledge MCP, .goosehints) plus mem0 official docs and API signatures. Feature priorities grounded in actual user flows. |
| Architecture | HIGH | Hybrid pattern (embedded + MCP) validated against existing knowledge MCP architecture. Railway multi-service topology verified. Code scaffolds drafted for all new components. |
| Pitfalls | HIGH | Blocking latency validated via arxiv benchmarks (p95: 1.44s vector, 2.59s graph) and GitHub issue reports. Dependency conflicts documented in Apache NIFI issue tracker. Cost projections based on Railway pricing docs. |

**Overall confidence:** HIGH

### Gaps to Address

- **Embedder API key for Anthropic-only users:** If the user has only an Anthropic key (no OpenAI), they need to add one for embeddings. Anthropic has no embedding API. The setup wizard should prompt for this, but the UX flow needs design during Phase 1 planning.
- **mem0 internal table schema for migration:** Direct pgvector insert (bypassing mem0.add()) requires matching mem0's internal vector store schema. This is undocumented and may change between versions. Validate by reading mem0 source code during Phase 3 planning.
- **LLM extraction model selection:** Should mem0 use the user's main model (potentially expensive, e.g. opus/gpt-4.1) or force a cheaper extraction model? The config supports separation, but the UX decision (auto-select cheap model vs. let user choose) needs resolution.
- **mem0 consolidation behavior:** mem0's automatic memory consolidation (merging similar embeddings) could merge contextually different memories. Whether to disable it by default or tune the similarity threshold needs testing with real data.
- **Gateway stdlib constraint vs. mem0 import:** ARCHITECTURE.md recommends embedding mem0 directly in gateway.py for the memory writer, but PITFALLS.md warns against importing mem0 in gateway.py (dependency isolation). This tension needs resolution: either accept the import (gateway already imports chromadb) or route memory writer through a subprocess/MCP call. The pragmatic answer is to accept the import since the precedent exists with chromadb.
- **mem0 issue #3711 status:** The structuredLlm bug (hardcoded to OpenAI for graph memory) may or may not be fixed in v1.0.5. Must verify before enabling graph memory for non-OpenAI users in Phase 4.

## Sources

### Primary (HIGH confidence)
- [mem0ai on PyPI](https://pypi.org/project/mem0ai/) -- v1.0.5 verified, dependency tree
- [mem0 Open Source Overview](https://docs.mem0.ai/open-source/overview) -- config defaults, architecture
- [mem0 pgvector Configuration](https://docs.mem0.ai/components/vectordbs/dbs/pgvector) -- connection parameters
- [mem0 Anthropic LLM Config](https://docs.mem0.ai/components/llms/models/anthropic) -- provider config
- [mem0 LiteLLM Config](https://docs.mem0.ai/components/llms/models/litellm) -- universal adapter
- [mem0 Graph Memory Docs](https://docs.mem0.ai/open-source/graph_memory/overview) -- Neo4j integration
- [mem0 arxiv paper](https://arxiv.org/abs/2504.19413) -- benchmarks, latency (p95: 1.44s vector, 2.59s graph)
- [Railway pgvector Template](https://railway.com/deploy/pgvector-latest) -- one-click deploy, updated Mar 2026
- [Railway Neo4j Template](https://railway.com/deploy/asEF1B) -- APOC pre-installed
- [Railway Private Networking](https://docs.railway.com/guides/private-networking) -- service-to-service DNS
- GooseClaw codebase: gateway.py (memory writer lines 6700-7080), knowledge/server.py, .goosehints, turn-rules.md

### Secondary (MEDIUM confidence)
- [mem0 GitHub Repository](https://github.com/mem0ai/mem0) -- architecture, issues
- [mem0 Official MCP Server](https://github.com/mem0ai/mem0-mcp) -- tool signatures (cloud-only, not usable directly)
- [DeepWiki mem0 Overview](https://deepwiki.com/mem0ai/mem0/1-overview) -- architecture, dual-store model
- [mem0 Self-Host Docker Guide](https://mem0.ai/blog/self-host-mem0-docker) -- docker-compose patterns
- [mem0 Custom Update Prompt](https://docs.mem0.ai/open-source/features/custom-update-memory-prompt) -- ADD/UPDATE/DELETE/NONE
- [GitHub Issue #2813: 20s add latency](https://github.com/mem0ai/mem0/issues/2813) -- self-hosted performance
- [GitHub Issue #3711: structuredLlm hardcoded to OpenAI](https://github.com/mem0ai/mem0/issues/3711) -- graph memory bug

### Tertiary (LOW confidence)
- mem0 consolidation stats (60% storage reduction, 22% precision boost) -- from mem0's own marketing, not independently verified
- [Embedding Model Comparison](https://elephas.app/blog/best-embedding-models) -- nomic vs OpenAI benchmarks

---
*Research completed: 2026-03-19*
*Ready for roadmap: yes*

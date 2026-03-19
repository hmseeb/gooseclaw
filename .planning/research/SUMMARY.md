# Project Research Summary

**Project:** GooseClaw v5.0 mem0 Memory Layer Integration
**Domain:** AI agent memory system (vector + knowledge graph) for self-hosted personal agent
**Researched:** 2026-03-19
**Revised:** 2026-03-19 (chromadb backend instead of pgvector, zero new infra)
**Confidence:** HIGH

## Executive Summary

GooseClaw's current memory system stores user knowledge in ChromaDB with a hand-rolled extraction pipeline: idle detection, LLM-based JSON extraction, manual dedup, manual upsert. This works but lacks contradiction resolution (old facts accumulate instead of updating), has no relationship modeling, and the extraction code is ~400 lines of brittle JSON parsing. mem0 is an open-source Python library that replaces all of this with a single `add()` call that handles fact extraction, embedding, contradiction resolution (ADD/UPDATE/DELETE/NOOP), and optional knowledge graph storage.

**Revised approach:** Use ChromaDB as mem0's vector store backend instead of pgvector. ChromaDB bundles its own embedder (`all-MiniLM-L6-v2`) at zero cost. No new Railway services. No new API keys for embeddings. The only LLM calls are for extraction, which reuses the user's existing provider (anthropic/openrouter/etc from vault).

The architecture is a hybrid: mem0 runs as a new MCP server (for goose tool access during conversations) and is called from gateway.py's memory writer (for background extraction). Both point at the same ChromaDB backend at `/data/knowledge/chroma`. A shared config module builds the mem0 configuration from environment variables, mapping the user's existing LLM provider to mem0's format.

## Key Findings

### Recommended Stack

**Core technologies:**
- **mem0ai 1.0.5** (`pip install mem0ai`): memory layer with vector support. Handles extraction, contradiction resolution, dedup, consolidation.
- **ChromaDB** (existing, already installed): vector store backend for mem0. Bundles `all-MiniLM-L6-v2` embedder. Zero new cost.
- **FastMCP** (from existing mcp[cli] 1.26.0): MCP server framework. Same pattern as knowledge extension.

**What's NOT needed:**
- No pgvector / PostgreSQL Railway service (ChromaDB replaces it)
- No OpenAI API key for embeddings (ChromaDB bundles its own)
- No Neo4j (deferred to future milestone)
- No new Railway services at all

### Expected Features

**Must have (P0):**
- Semantic memory search via mem0 MCP tools
- Automatic memory extraction via mem0.add() replacing manual pipeline
- Contradiction resolution (new facts update old ones)
- Memory deduplication (automatic via mem0)
- MCP tools: memory_search, memory_add, memory_delete, memory_list
- Identity routing to user.md preserved
- ChromaDB system collection unchanged
- LLM extraction reuses user's existing provider

**Should have (P1):**
- Memory history/audit trail
- Custom extraction prompts matching GooseClaw's personality
- Scoped memories (user_id) for future multi-user

**Defer:**
- Neo4j knowledge graph (future milestone, separate infra)
- Entity relationship extraction
- Per-turn memory injection via MOIM

### Architecture Approach

Hybrid integration: mem0 as MCP server + embedded in gateway memory writer. Both share ChromaDB at `/data/knowledge/chroma` with a separate collection from system docs.

**Major components:**
1. **mem0 MCP server** (docker/mem0_mcp/server.py) -- stdio, exposes memory tools to goose
2. **gateway.py memory writer** (modified) -- replaces extraction pipeline with mem0.add()
3. **mem0_config.py** (shared module) -- builds mem0 config from vault/setup.json
4. **ChromaDB** (existing, scope expanded) -- system collection (unchanged) + mem0 collection (new)
5. **Migration script** -- one-time move from runtime collection to mem0 collection

### Critical Pitfalls

1. **mem0.add() blocks 2-20s per call** -- must be async/threaded with timeout
2. **Dependency conflicts** -- mem0ai may conflict with existing chromadb version. Test carefully.
3. **Migration must bypass mem0.add()** -- direct chromadb insert to avoid re-extraction
4. **LLM token burn** -- mem0 makes 3-6 internal LLM calls per add(). Route to cheap model.
5. **Identity routing must be preserved** -- mem0 handles knowledge, user.md/soul.md stay separate

## Implications for Roadmap

3-phase structure (down from 4, no pgvector/neo4j infra phase):

### Phase 1: mem0 MCP Server + Config
Prove mem0 works end-to-end with ChromaDB backend. Create MCP server, register extension, validate tools work during conversations. No gateway changes yet.

### Phase 2: Gateway Memory Writer Migration
Replace ~150 lines of manual extraction code with mem0.add(). Handle identity routing split. Async with timeout.

### Phase 3: ChromaDB Migration + Cleanup
Migrate existing runtime memories to mem0 collection. Deprecate old runtime collection. Narrow knowledge MCP to system docs only.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | mem0ai + chromadb backend verified in docs |
| Features | HIGH | Derived from codebase analysis + mem0 API |
| Architecture | HIGH | Follows existing knowledge MCP pattern exactly |
| Pitfalls | HIGH | Latency, dependency, migration risks well-documented |

**Overall confidence:** HIGH

**Key advantage of revised approach:** Zero new infrastructure. Zero new API keys. Zero new Railway services. Just a Python library on top of what already exists.

---
*Research completed: 2026-03-19*
*Revised: 2026-03-19 (chromadb backend, zero new infra)*
*Ready for requirements: yes*

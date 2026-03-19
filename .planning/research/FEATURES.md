# Feature Landscape: mem0 Memory Layer Integration

**Domain:** AI agent memory system (vector + knowledge graph) for personal AI agent platform
**Researched:** 2026-03-19
**Confidence:** HIGH (mem0 open-source is well-documented, existing GooseClaw memory system thoroughly analyzed)

## Table Stakes

Features users expect from a memory-enabled AI agent. Missing any of these and the agent feels lobotomized between sessions.

### Memory Core

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|--------------|------------|--------------|-------|
| Semantic memory search | Users expect "what do you know about X?" to work. mem0's `search()` replaces ChromaDB `knowledge_search`. Without this, the agent has amnesia. | LOW | mem0 initialized with pgvector | Direct replacement for existing `knowledge_search` MCP tool. mem0 handles embedding + similarity scoring internally. |
| Automatic memory extraction from conversations | The gateway already does this via `_memory_writer_loop()` after idle timeout. mem0 replaces the ChromaDB write target. Users expect the agent to remember without being told to. | MEDIUM | mem0 `add()` API, existing idle-detection loop | Current flow: idle 10min -> fetch session -> LLM extracts JSON -> route to user.md + ChromaDB. New flow: same trigger, but feed raw conversation to `mem0.add()` which handles extraction internally. Huge simplification. |
| Memory persistence across sessions | Memories survive container restarts. pgvector data on Railway volume. Current ChromaDB already persists to /data/knowledge/chroma. | LOW | pgvector on persistent volume | PostgreSQL data dir must be on /data (Railway volume). Already proven pattern with ChromaDB. |
| Contradiction resolution (new facts update old ones) | If user says "I moved to NYC" but memory says "lives in SF", the old fact must update. Current system naively appends. mem0's ADD/UPDATE/DELETE/NOOP pipeline handles this automatically. | LOW (mem0 handles it) | mem0 `add()` with LLM-powered update decisions | This is mem0's killer feature. The LLM compares incoming facts against existing memories and decides: ADD new, UPDATE existing, DELETE contradicted, or NOOP. No custom code needed. |
| Memory deduplication | Users hate seeing repeated facts. Current ChromaDB has no dedup. mem0's update pipeline prevents duplicate storage by comparing semantic similarity before adding. | LOW (mem0 handles it) | mem0 internal consolidation | mem0 consolidation merges embeddings above 0.85 similarity threshold and deduplicates clusters within 0.9 threshold. This is automatic. |
| Manual memory CRUD tools | Agent needs explicit tools to store/search/delete memories on demand (not just auto-extraction). The existing knowledge MCP exposes 5 tools. mem0 MCP must expose equivalent. | MEDIUM | mem0 MCP server (stdio, Python) | Existing tools: knowledge_search, knowledge_upsert, knowledge_get, knowledge_delete, knowledge_recent. New tools must cover equivalent functionality via mem0 API. |
| LLM extraction using existing provider | mem0 needs an LLM for fact extraction. Must use whatever provider the user already configured in vault (OpenAI, Anthropic, etc.), not require a separate API key. | MEDIUM | Vault credential access, mem0 LLM provider config | mem0 supports 20+ LLM providers. Config must read from vault at runtime and pass to mem0's `llm.provider` config. Same for embedder. |

### Identity Integration (GooseClaw-Specific)

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|--------------|------------|--------------|-------|
| Identity trait routing to user.md | Current memory writer routes "identity" facts (stable 6+ months) to user.md sections. This MUST continue working. user.md is the agent's live understanding of the user, loaded every session via .goosehints. | MEDIUM | Existing `_classify_identity_section()`, user.md file system | mem0 can't do this natively. The extraction prompt must still produce identity vs knowledge classification. Option A: keep the custom extraction prompt + routing, use mem0 only for knowledge. Option B: use mem0 for everything, add a post-extraction hook that also writes to user.md. Option A is safer. |
| soul.md / user.md remain source of truth for identity | These files are loaded via .goosehints at session start. They're the agent's "who am I" and "who are you" context. mem0 is for episodic/factual memory, not identity. | LOW | No change needed | Clear boundary: identity files = who. mem0 = what happened, what's known. This is already the split in turn-rules.md. |
| ChromaDB retained for system docs | System collection (platform docs, procedures, schemas) stays in ChromaDB. Only runtime/user memories move to mem0. Two separate concerns. | LOW | Existing knowledge MCP stays for system namespace | Per PROJECT.md: "Keep chromadb for system docs (platform reference, separate concern)". The knowledge MCP server's `system_col` continues unchanged. |

### Operational

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|--------------|------------|--------------|-------|
| Migration from ChromaDB runtime to mem0 | Existing runtime knowledge chunks must migrate to mem0 on first boot. Users shouldn't lose memories on upgrade. | MEDIUM | ChromaDB read access, mem0 `add()` or direct pgvector insert | One-time migration script. Read all runtime_col chunks from ChromaDB, insert into mem0. Similar pattern to existing `migrate_memory.py`. |
| Graceful fallback if mem0/pgvector unavailable | If PostgreSQL is down, the agent should still work. Memory tools return errors but don't crash the session. | LOW | Error handling in MCP tools | Same pattern as current knowledge MCP: try/except, return human-readable error strings. |
| Memory writer toggle (enable/disable) | Current `setup.json` has `memory_writer_enabled` flag. Must persist. Some users don't want auto-extraction. | LOW | setup.json config | Already implemented. Just needs to gate the new mem0 extraction path. |

## Differentiators

Features that go beyond table stakes. These make GooseClaw's memory system feel genuinely intelligent rather than just "has memory."

### Knowledge Graph (Neo4j)

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Entity relationship extraction | mem0 extracts entities (people, places, projects) and their relationships into a graph. Enables "who is connected to what?" queries. Current ChromaDB is flat, no relationships. | HIGH | Neo4j container, mem0 `graph_store` config, APOC plugin | Neo4j is the slowest container to start (90s+ health check). Adds Docker complexity. Worth it for relationship-rich domains (user's professional network, project dependencies). Phase this: vector-first, graph later. |
| Relationship-enhanced search | When searching memories, graph relationships augment vector results. "What do I know about Sarah?" returns not just direct mentions but also her connections, projects, and context. | MEDIUM | Neo4j running, mem0 graph_store enabled | mem0 runs graph queries in parallel with vector search, appends `relations` array to results. Big UX upgrade for users with rich relationship context. |
| Multi-hop entity traversal | "What connects project X to person Y?" traverses the graph to find indirect relationships across multiple entities. Flat vector search can't do this. | MEDIUM | Neo4j, graph queries | This is where knowledge graphs shine. Not possible with vector-only search. |

### Intelligent Memory Management

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Memory categories/tagging | mem0 supports metadata categories (e.g., "work", "personal", "health"). Enables filtered recall: "what do you remember about my work?" | LOW | mem0 metadata on `add()` | Use `metadata={"category": "work"}` on add. Filter on search with `filters`. Current ChromaDB uses `type` field (fact, procedure, etc). mem0 can do both type and category. |
| Memory history/audit trail | mem0 tracks all ADD/UPDATE/DELETE operations with timestamps via SQLite history. Users can ask "what changed about memory X?" and get an audit log. | LOW (mem0 built-in) | mem0 `history()` API | Expose as MCP tool: `memory_history`. Current system has no audit trail for memory changes. This is free with mem0. |
| Memory consolidation | Periodic merging of similar memories, relevance score updates based on usage. Cuts storage by ~60%, raises retrieval precision ~22%. | LOW (mem0 built-in) | mem0 internal consolidation | Happens automatically. No custom code needed. Current ChromaDB has no consolidation (memories accumulate forever). |
| Configurable extraction prompts | mem0 supports `custom_fact_extraction_prompt` and `custom_update_memory_prompt`. GooseClaw can customize extraction to match the identity/knowledge split in turn-rules.md. | LOW | mem0 config `custom_prompts` | This is how we customize mem0's extraction to match GooseClaw's philosophy: "save like you'll lose the session any second." |
| Scoped memories (user_id, agent_id, run_id) | mem0 supports hierarchical memory scoping. user_id for permanent, run_id for session-scoped. Enables per-bot memory isolation if multi-bot is added later. | LOW | mem0 scoping params on all API calls | Single user for now (user_id = "default" or from vault). But the architecture supports multi-user without rework. Forward-compatible. |

### User-Facing Features

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| "What do you remember about me?" command | User can ask the agent to enumerate memories. `memory_list` tool with pagination. Current system requires manual knowledge_search queries. | LOW | mem0 `get_all()` API | Expose as MCP tool. mem0 returns all memories for a user_id with metadata. Much better than current approach of searching ChromaDB with vague queries. |
| Memory deletion by user request | "Forget that I told you about X" should actually delete the memory. Current system has knowledge_delete but it's by exact key, hard to discover. | LOW | mem0 `delete()` API, search-then-delete flow | Agent searches for the memory, confirms with user, deletes by ID. mem0 handles cascade (removes from vector store, graph store, and history). |
| Entity listing | "Who do you know about?" lists all entities in the knowledge graph. Not possible with flat vector search. | LOW | mem0 `list_entities()` API | Only meaningful once graph memory is enabled. Shows people, places, projects as distinct entities. |

## Anti-Features

Features to explicitly NOT build. These seem useful but create problems for GooseClaw's architecture.

| Anti-Feature | Why Requested | Why Problematic | Alternative |
|--------------|---------------|-----------------|-------------|
| mem0 cloud/managed service | "Just use the API, no infrastructure" | GooseClaw is self-hosted for privacy. Sending all memories to mem0's cloud defeats the purpose. Users chose self-hosting specifically to own their data. | Self-hosted mem0 with pgvector + optional Neo4j. All data stays on Railway volume. |
| Separate embedding model API key | "Configure a dedicated embedding provider" | Users already struggle configuring one provider. Adding a second API key for embeddings adds confusion. Most providers (OpenAI, Anthropic, etc) offer both LLM and embedding models. | Reuse the same provider configured in vault. If the provider doesn't support embeddings, fall back to a lightweight local model (sentence-transformers) or document the requirement. |
| Real-time memory streaming | "Show memories being extracted in real-time" | Memory extraction is a background process. Surfacing it real-time adds WebSocket complexity, clutters the chat, and makes the "show results, hide plumbing" philosophy impossible. | Silent extraction. Log to structured logging for debugging. Users can ask "what did you learn?" after the fact. |
| Exposing mem0's REST API directly | "Run mem0 as a separate HTTP server with its own endpoints" | Adds another port, another auth layer, another attack surface. GooseClaw's constraint is Python stdlib only for gateway.py. The mem0 MCP is the right interface. | MCP server (stdio) is the only interface. Gateway talks to mem0 via Python API directly for auto-extraction. No HTTP server for mem0. |
| Replacing user.md/soul.md with mem0 | "Store identity in mem0 too, one system for everything" | Identity files are loaded via .goosehints at session start. They're always in context. mem0 memories are retrieved on-demand via search. These are fundamentally different access patterns. Identity needs to be guaranteed-present, not search-dependent. | Keep the split: identity files for "who", mem0 for "what happened/what's known". This matches the existing turn-rules.md routing. |
| Agent-managed memory (Letta-style) | "Let the agent decide when and how to manage its own memory store" | Adds unpredictable LLM-dependent behavior to a critical system. mem0's structured pipeline (extract -> compare -> ADD/UPDATE/DELETE) is deterministic given the same inputs. Agent-managed memory can drift, forget to consolidate, or over-remember. | mem0's pipeline handles memory management. The agent uses tools (search, add) for explicit operations. Background extraction handles implicit learning. |
| Complex memory UI in setup wizard | "Add a memory management dashboard to setup.html" | setup.html is a single HTML file (no build tooling). Adding a memory browser, search interface, and edit capability pushes it past maintainability. The agent itself IS the memory interface. | Users interact with memory through conversation: "what do you remember?", "forget X", "remember that Y". The agent uses mem0 tools. No separate UI. |

## Feature Dependencies

```
pgvector (PostgreSQL) setup
    (foundation, no dependencies)

mem0 Python library installation
    (foundation, no dependencies)

mem0 MCP server (stdio, Python)
    └──requires──> pgvector setup
    └──requires──> mem0 library
    └──requires──> LLM provider from vault

Memory search tool (memory_search)
    └──requires──> mem0 MCP server

Memory add tool (memory_add)
    └──requires──> mem0 MCP server

Memory list/get/delete/history tools
    └──requires──> mem0 MCP server

Gateway auto-extraction rewrite
    └──requires──> mem0 Python API (direct, not MCP)
    └──requires──> pgvector setup
    └──replaces──> current ChromaDB-based _process_memory_extraction()

Identity routing preservation
    └──requires──> Gateway auto-extraction rewrite
    └──depends-on──> existing _classify_identity_section()

ChromaDB runtime -> mem0 migration
    └──requires──> mem0 Python API
    └──requires──> pgvector setup
    └──reads-from──> existing ChromaDB runtime collection

Neo4j knowledge graph (optional, phased)
    └──requires──> pgvector setup (vector-first)
    └──requires──> Neo4j container
    └──enhances──> memory search (adds relations)
    └──enables──> entity listing, relationship queries

Memory categories/tagging
    └──requires──> mem0 MCP server
    └──optional──> custom extraction prompt for auto-categorization

Memory history/audit
    └──requires──> mem0 MCP server
    └──uses──> mem0 built-in SQLite history

ChromaDB system collection (unchanged)
    └──independent──> continues serving system docs
    └──coexists-with──> mem0 for user memories
```

### Key Dependency Insight

The critical path is: pgvector -> mem0 init -> MCP server -> gateway extraction rewrite -> migration. Neo4j is a separate branch that can be added after the vector-only path is stable. This means the milestone naturally splits into two phases: vector memory (essential) and graph memory (enhancement).

## MVP Recommendation

### Phase 1: Vector Memory (Must Have)

Replace ChromaDB runtime collection with mem0 + pgvector. This delivers all table stakes features.

1. **pgvector setup** - PostgreSQL with vector extension on Railway volume
2. **mem0 MCP server** - stdio server exposing memory tools to goose
3. **Gateway extraction rewrite** - Replace `_process_memory_extraction()` to use mem0
4. **Identity routing preservation** - Keep user.md routing for identity traits
5. **ChromaDB migration** - One-time migrate runtime chunks to mem0
6. **ChromaDB system collection retained** - System docs stay in ChromaDB

MCP tools for Phase 1:
- `memory_search(query, limit?, category?)` - Semantic search, replaces knowledge_search for runtime memories
- `memory_add(content, category?)` - Explicit memory storage, replaces knowledge_upsert
- `memory_get(memory_id)` - Get specific memory by ID
- `memory_delete(memory_id)` - Delete specific memory
- `memory_list(limit?, category?)` - List all memories with optional filter
- `memory_history(limit?)` - Audit trail of memory changes

### Phase 2: Knowledge Graph (Enhancement, Deferred)

Add Neo4j for entity relationship extraction. Only after Phase 1 is stable.

1. **Neo4j container** - Add to Docker setup
2. **Graph store config** - Enable mem0 graph_store with Neo4j
3. **Entity tools** - `memory_entities()`, `memory_relations(entity)`
4. **Relationship-enhanced search** - Graph results augment vector search

Defer: Neo4j graph because it adds Docker complexity (90s startup, APOC plugin, Bolt protocol), Railway resource requirements, and a separate failure mode. The vector-only path already delivers 80% of the value.

## MCP Tool Design

### Recommended Tool Signatures

These replace the existing knowledge MCP tools for user/runtime memories:

```python
# ── replaces knowledge_search for runtime memories ──
@mcp.tool()
def memory_search(query: str, limit: int = 5, category: str = "") -> str:
    """Search your memories semantically. Returns relevant memories with scores.

    Args:
        query: Natural language search (e.g., "what projects is the user working on?")
        limit: Max results (default 5, max 20)
        category: Optional filter (e.g., "work", "personal", "health")
    """

# ── replaces knowledge_upsert for runtime memories ──
@mcp.tool()
def memory_add(content: str, category: str = "") -> str:
    """Store a memory explicitly. Use for facts, preferences, integrations.

    Args:
        content: The memory to store (natural language, mem0 extracts facts automatically)
        category: Optional category tag (e.g., "work", "personal", "integration")
    """

# ── new: not possible with ChromaDB ──
@mcp.tool()
def memory_list(limit: int = 10, category: str = "") -> str:
    """List all stored memories, newest first. Use for "what do you remember?"

    Args:
        limit: Max results (default 10, max 50)
        category: Optional filter
    """

# ── replaces knowledge_get ──
@mcp.tool()
def memory_get(memory_id: str) -> str:
    """Get a specific memory by its ID.

    Args:
        memory_id: The memory identifier
    """

# ── replaces knowledge_delete ──
@mcp.tool()
def memory_delete(memory_id: str) -> str:
    """Delete a specific memory permanently.

    Args:
        memory_id: The memory identifier to delete
    """

# ── new: audit trail, not possible with ChromaDB ──
@mcp.tool()
def memory_history(limit: int = 10) -> str:
    """Show recent memory changes (adds, updates, deletes). Audit trail.

    Args:
        limit: Max entries (default 10, max 50)
    """
```

### Tool Design Rationale

- **memory_add replaces knowledge_upsert**: mem0's `add()` handles key generation, dedup, and contradiction resolution automatically. No need for user-specified keys (the biggest friction point of knowledge_upsert).
- **memory_search replaces knowledge_search for runtime**: System docs stay in ChromaDB's knowledge_search. Runtime memories move to memory_search. The .goosehints instructions must be updated to reflect this split.
- **memory_list is new**: ChromaDB's `get()` with no filter was expensive (fetched everything). mem0's `get_all()` is designed for this. Enables "what do you remember about me?" naturally.
- **memory_history is new**: ChromaDB had no history. mem0 tracks all operations. Enables "what changed?" and debugging.
- **No knowledge_recent equivalent needed**: memory_list with sort-by-date covers this. Fewer tools = less agent confusion.

### Tools NOT Exposed (Intentional)

- **delete_all_memories**: Too destructive for an MCP tool. Require user to do this via gateway API or direct request.
- **list_entities / delete_entity**: Only relevant for graph memory (Phase 2). Add later.
- **update_memory**: mem0's `add()` handles updates via its contradiction resolution pipeline. Explicit update by ID is rarely needed and adds cognitive load.

## Automatic Extraction: How It Changes

### Current Flow (ChromaDB)

```
User goes idle (10 min)
  -> _memory_writer_loop() detects idle
  -> Fetches session messages
  -> Sends to LLM with MEMORY_EXTRACT_PROMPT
  -> LLM returns JSON: { identity: [...], knowledge: [...] }
  -> Identity traits -> user.md (section-routed)
  -> Knowledge items -> ChromaDB runtime_col.upsert()
```

### New Flow (mem0)

```
User goes idle (10 min)
  -> _memory_writer_loop() detects idle (unchanged)
  -> Fetches session messages (unchanged)
  -> Two parallel paths:

  Path A: mem0 automatic extraction
    -> mem0.add(messages, user_id="default")
    -> mem0 internally: LLM extracts facts, compares with existing,
       ADD/UPDATE/DELETE as needed. Stored in pgvector.
    -> No custom prompt needed. mem0 handles dedup + contradiction.

  Path B: Identity extraction (preserved)
    -> Send to LLM with IDENTITY_EXTRACT_PROMPT (narrower prompt)
    -> LLM returns JSON: { identity: [...] }
    -> Identity traits -> user.md (section-routed, unchanged)
    -> No knowledge routing (mem0 handles that in Path A)
```

### Key Simplification

The current `MEMORY_EXTRACT_PROMPT` does two things: extracts identity AND knowledge. With mem0, knowledge extraction is handled automatically by `mem0.add()`. The gateway only needs a simpler identity-focused prompt for the user.md routing. This cuts the extraction code roughly in half.

### What Stays the Same

- Idle detection (10 min configurable)
- Session message fetching
- user.md section-routed identity writes
- `_classify_identity_section()` logic
- `_fact_already_exists()` dedup for user.md
- `memory_writer_enabled` toggle in setup.json

### What Changes

- ChromaDB `runtime_col.upsert()` -> `mem0.add(messages)`
- Complex JSON knowledge routing -> mem0 handles internally
- `MEMORY_EXTRACT_PROMPT` splits into simpler identity-only prompt
- `_get_knowledge_collection()` ChromaDB lazy-load -> mem0 client init
- `_process_memory_extraction()` simplified (identity only, knowledge removed)

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| mem0 MCP server with core tools | HIGH | MEDIUM | P0 |
| pgvector setup (PostgreSQL on Railway) | HIGH | MEDIUM | P0 |
| Gateway auto-extraction via mem0 | HIGH | MEDIUM | P0 |
| Contradiction resolution (automatic) | HIGH | FREE (mem0) | P0 |
| Memory deduplication (automatic) | HIGH | FREE (mem0) | P0 |
| Identity routing to user.md preserved | HIGH | LOW | P0 |
| ChromaDB runtime -> mem0 migration | HIGH | MEDIUM | P0 |
| LLM extraction via existing provider | HIGH | MEDIUM | P0 |
| ChromaDB system docs retained | MEDIUM | LOW | P0 |
| Memory list/enumerate | MEDIUM | LOW | P1 |
| Memory history/audit | LOW | LOW | P1 |
| Memory categories/tagging | LOW | LOW | P1 |
| Scoped memories (user_id) | LOW (single user) | LOW | P1 |
| Custom extraction prompts | MEDIUM | LOW | P1 |
| Neo4j knowledge graph | MEDIUM | HIGH | P2 |
| Entity relationship extraction | MEDIUM | HIGH (Neo4j) | P2 |
| Relationship-enhanced search | MEDIUM | MEDIUM | P2 |
| Entity listing tool | LOW | LOW | P2 |

**Priority key:**
- P0: Must have for v5.0 launch (core memory replacement)
- P1: Should have, include in v5.0 if time permits (enhanced memory features)
- P2: Defer to v5.x or v6.0 (knowledge graph, requires Neo4j)

## Sources

### HIGH confidence
- GooseClaw codebase analysis: gateway.py memory writer (lines 6700-7080), knowledge/server.py, .goosehints, turn-rules.md, identity files
- [mem0 GitHub](https://github.com/mem0ai/mem0) - open-source repository, architecture
- [mem0 Official MCP Server](https://github.com/mem0ai/mem0-mcp) - 9 tools, tool signatures
- [mem0 pgvector Configuration](https://docs.mem0.ai/components/vectordbs/dbs/pgvector) - full config dict

### MEDIUM confidence
- [DeepWiki mem0 Overview](https://deepwiki.com/mem0ai/mem0/1-overview) - architecture, dual-store model
- [DeepWiki Basic Usage](https://deepwiki.com/mem0ai/mem0/10.1-basic-usage) - API patterns, Memory class methods
- [mem0 Graph Memory Overview](https://docs.mem0.ai/open-source/graph_memory/overview) - Neo4j config, entity extraction
- [OpenClaw Memory-Mem0 Plugin](https://github.com/serenichron/openclaw-memory-mem0) - similar integration architecture
- [mem0 Self-Host Docker Guide](https://mem0.ai/blog/self-host-mem0-docker) - docker-compose patterns
- [mem0 Custom Update Prompt](https://docs.mem0.ai/open-source/features/custom-update-memory-prompt) - ADD/UPDATE/DELETE/NONE decisions
- [Mem0 Alternatives Comparison](https://vectorize.io/articles/mem0-alternatives) - Hindsight, Zep, Letta comparison
- [AI Agent Memory Best Practices 2026](https://47billion.com/blog/ai-agent-memory-types-implementation-best-practices/) - industry expectations

### LOW confidence
- mem0 consolidation stats (60% storage reduction, 22% precision boost) - from mem0's own research page, not independently verified

---
*Feature research for: mem0 memory layer integration into GooseClaw AI agent platform*
*Researched: 2026-03-19*

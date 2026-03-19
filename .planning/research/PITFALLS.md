# Domain Pitfalls: mem0 Memory Layer Integration

**Domain:** Adding mem0 vector + knowledge graph memory to existing AI agent platform (GooseClaw v5.0)
**Researched:** 2026-03-19
**Confidence:** HIGH (based on mem0 docs, arxiv paper, GitHub issues, Railway docs, codebase analysis)

## Critical Pitfalls

Mistakes that cause rewrites, data loss, or production outages.

---

### Pitfall 1: mem0 add_memory() blocks for 2-20 seconds per call, stalling the conversation relay

**What goes wrong:**
The gateway's memory writer calls `mem0.add()` after each idle session. mem0's `add()` is not a simple vector insert. It triggers an LLM-as-a-Judge pipeline: extraction LLM identifies facts, then a second LLM call classifies each fact as ADD/UPDATE/DELETE/NOOP against existing memories, then vector upsert + optional graph writes. Self-hosted users on GitHub report 20-second `add()` calls even on A100 GPUs (issue #2813). The arxiv paper shows p95 total latency of 1.44s for Mem0 and 2.59s for Mem0 with graph. If this runs synchronously in the memory writer loop, the loop stalls. If the gateway's existing goosed session is reused for extraction, it blocks the user's next message.

**Why it happens:**
mem0's architecture is intentionally LLM-heavy. Every `add()` call invokes at minimum: (1) extraction LLM call to identify facts from conversation, (2) vector similarity search against existing memories, (3) LLM call per candidate fact for ADD/UPDATE/DELETE/NOOP classification, (4) vector upsert, (5) optionally 3 more LLM calls for graph entity extraction, relationship generation, and conflict resolution. That is 3-6 LLM API calls per memory add operation.

**Consequences:**
- Memory extraction that currently takes ~5s (one goosed LLM call) balloons to 15-60s with mem0
- If graph memory is enabled, double the LLM cost per extraction
- On Railway's Hobby plan, concurrent LLM calls may exhaust the container's memory or hit provider rate limits
- Users notice their bot "going quiet" for 30+ seconds after conversations end

**Prevention:**
1. Use `AsyncMemory` with `asyncio.create_task()` so `add()` never blocks the main loop
2. Route mem0's internal LLM calls to a cheap/fast model (gpt-4.1-nano or gpt-4o-mini), not the user's main model
3. Start WITHOUT graph memory. Vector-only mem0 is 2x faster (p95: 1.44s vs 2.59s). Add graph in a later phase after validating vector performance
4. Set a hard timeout (30s) on mem0 add operations. If it exceeds, log and skip. Stale memory is better than a hung process
5. Batch: accumulate conversation turns, extract once per session (not per message)

**Detection:**
- Memory writer logs showing >10s per extraction
- LLM provider dashboard showing 3-6x more API calls than expected
- Users reporting delayed responses after idle periods

**Phase to address:** Phase 1 (foundation). Get async memory add working before anything else. If this is wrong, everything built on top fails.

---

### Pitfall 2: mem0 pip dependencies conflict with existing chromadb, bloating the image and breaking stdlib isolation

**What goes wrong:**
`pip install mem0ai` pulls in: qdrant-client (>1.9.1), pydantic (>2.7.3), openai (>1.90.0), sqlalchemy (>2.0.31), posthog (>3.5.0). Adding `mem0ai[graph]` also pulls langchain-neo4j, neo4j, kuzu, rank-bm25. The project already has chromadb==1.5.5 which has a known protobuf version conflict with qdrant-client (chromadb's opentelemetry-proto needs protobuf <5.0, qdrant-client installs protobuf 5.x). The gateway.py is stdlib-only. Installing mem0 into the same Python environment risks breaking existing chromadb imports.

**Why it happens:**
mem0's default vector store is Qdrant, not ChromaDB. When you install both mem0ai and chromadb, their transitive dependency trees collide on protobuf versions. This is a documented issue (Apache NIFI-13741, multiple chromadb GitHub issues). Additionally, mem0 brings openai, pydantic, and sqlalchemy into the environment. These are heavyweight packages that the gateway.py has deliberately avoided.

**Consequences:**
- `import chromadb` may fail at runtime with protobuf version errors
- Docker image size bloats by 200-500MB (neo4j driver, langchain, qdrant-client, pydantic, sqlalchemy)
- If any mem0 dependency is imported in gateway.py's process space (even accidentally), the stdlib-only principle is violated
- Build times increase significantly on Railway

**Prevention:**
1. Run mem0 as a SEPARATE MCP server process (like the existing knowledge server). Never import mem0 in gateway.py
2. Use mem0's ChromaDB backend (provider: "chroma") OR pgvector backend, NOT the default Qdrant. This avoids installing qdrant-client entirely
3. If using pgvector as mem0's backend, you can potentially remove chromadb from requirements.txt for user memories (keep it only for system docs)
4. Pin all dependencies strictly in requirements.txt. Test the full dependency resolution before merging
5. Consider a two-requirements pattern: `requirements.txt` (gateway runtime) and `requirements-mem0.txt` (mem0 MCP server only)

**Detection:**
- `pip install` warnings about incompatible versions
- Runtime ImportError or protobuf version mismatches
- `docker build` failing on dependency resolution
- gateway.py importing anything from mem0ai package

**Phase to address:** Phase 1 (foundation). Dependency architecture must be decided before any code is written. Wrong choice here means a full rewrite.

---

### Pitfall 3: Railway multi-service cost explosion. Neo4j alone needs 1-2GB RAM minimum

**What goes wrong:**
The current GooseClaw runs as a single Railway service. Adding mem0 with pgvector and Neo4j means 3+ Railway services: the app container, a PostgreSQL+pgvector database, and a Neo4j graph database. Neo4j's minimum recommended heap is 1GB, and it "tends to overestimate memory requirements" in Docker. Railway charges per RAM-hour and CPU-hour. A Neo4j instance idling at 1.5GB RAM costs roughly $5-10/month on top of the existing deployment. For a personal agent on a Hobby plan ($5/month credit), this triples the bill.

**Why it happens:**
Neo4j is a JVM application. The JVM pre-allocates heap memory on startup. Even with `NEO4J_server_memory_heap_initial__size=256m` and `NEO4J_server_memory_heap_max__size=512m`, the JVM + page cache + OS overhead typically lands at 800MB-1.2GB actual usage. Railway can't run Neo4j on Trial accounts at all. Hobby plan allows up to 8GB per service, but you're paying for every MB-hour.

**Consequences:**
- Monthly Railway bill goes from ~$5-7 to $15-25 for a personal agent
- If Neo4j OOMs on Railway, the service crashes and restarts in a loop
- Users who deployed GooseClaw for its "zero DevOps" promise now have 3 services to monitor
- Neo4j cold start takes 15-30 seconds, during which graph queries fail

**Prevention:**
1. Phase graph memory LAST. Start with pgvector only (vector search). pgvector runs inside PostgreSQL, which is one service, not two
2. When ready for graph, evaluate FalkorDB or Kuzu (embedded, no separate service) before Neo4j
3. If Neo4j is required, set explicit memory limits: `NEO4J_server_memory_heap_initial__size=256m`, `NEO4J_server_memory_heap_max__size=512m`, `NEO4J_server_memory_pagecache_size=128m`. Test these work for your data volume
4. Document the cost increase clearly in the setup wizard. Users should consent to the higher bill
5. Make Neo4j optional. The system should work with vector-only memory and gracefully degrade if no graph backend is configured

**Detection:**
- Railway billing dashboard showing unexpected cost increase
- Neo4j service restarting repeatedly (OOM kills in Railway logs)
- Graph queries timing out during Neo4j cold start
- Users complaining about increased Railway bills

**Phase to address:** Architecture decision in Phase 1. Deploy pgvector in Phase 2. Neo4j deferred to Phase 3+ or made entirely optional.

---

### Pitfall 4: Migrating existing ChromaDB runtime memories to mem0 loses data or creates duplicates

**What goes wrong:**
The current system stores user memories in ChromaDB's "runtime" collection with a specific metadata schema (type, source, section, namespace, refs, key, created_at, updated_at). mem0 has its own internal memory representation with different fields (user_id, agent_id, run_id, memory content, hash, metadata). There is no built-in migration tool. A naive bulk import (read from ChromaDB, call mem0.add() for each) triggers the LLM extraction pipeline for every memory, which: (a) costs LLM tokens for re-processing already-extracted facts, (b) may merge, update, or delete memories that shouldn't be touched, (c) takes hours for hundreds of memories at 2-20s per add.

**Why it happens:**
mem0's `add()` is not a raw insert. It always runs the extraction + classification pipeline. There's no "import raw memory" API. The `Memory` class doesn't expose a way to bypass extraction and directly insert a pre-extracted fact into the vector store. You'd need to go directly to mem0's underlying vector store, which couples you to internal implementation details.

**Consequences:**
- Migration of 200 existing runtime memories takes 1-2 hours and costs $0.50-2.00 in LLM API calls
- mem0's LLM may decide to merge, update, or delete memories during import, losing information
- Duplicate memories if migration is interrupted and restarted (no idempotency guarantee)
- Identity traits stored in user.md are fine (they're files), but ChromaDB runtime knowledge is at risk

**Prevention:**
1. Don't migrate through mem0's `add()`. Write a migration script that directly inserts into mem0's underlying vector store (pgvector) with pre-computed embeddings from ChromaDB
2. Export ChromaDB data with embeddings intact: `runtime_col.get(include=["documents", "metadatas", "embeddings"])`. Re-embed only if switching embedding models
3. Run migration in dry-run mode first: extract all from ChromaDB, format for mem0, log what would be inserted, review before committing
4. Keep ChromaDB read-only during migration as a fallback. Don't delete until mem0 has been validated for 1+ week
5. Track migration state: a simple JSON file listing migrated chunk IDs so interrupted migrations can resume

**Detection:**
- Memory count in mem0 doesn't match ChromaDB runtime collection count
- Users reporting "the bot forgot" things it used to know
- LLM API bill spike during migration window
- Duplicate entries when searching mem0 (same fact stored twice with slight wording differences)

**Phase to address:** Dedicated migration phase AFTER mem0 is proven working with new memories. Don't migrate and build simultaneously.

---

### Pitfall 5: mem0's LLM extraction uses the user's API key, burning their tokens without clear attribution

**What goes wrong:**
The current memory writer uses the user's configured goosed session for extraction (one LLM call per session). mem0 makes 3-6 LLM calls per memory add operation internally. If mem0 is configured to use the user's OpenAI/Anthropic API key (the same one from the vault), the user sees unexpected charges on their LLM provider dashboard. They have no visibility into which calls are "their conversation" vs "mem0 background processing." The user configured one API key for their agent. They didn't sign up for 3-6x background API calls.

**Why it happens:**
mem0 requires an LLM provider configuration. The simplest path is to reuse the user's existing provider credentials from the vault. But mem0's internal pipeline (extraction, classification, graph entity extraction, conflict resolution) generates multiple API calls that don't appear in the goose conversation history. The user sees their OpenAI bill double with no explanation.

**Consequences:**
- User's monthly LLM bill increases 30-50% from background mem0 operations
- No way for user to see "this $X was for memory processing" in their provider dashboard
- Users on rate-limited free tiers (Claude free, GPT-4 free tier) hit limits faster
- Erosion of trust: "why is my bot using so many tokens?"

**Prevention:**
1. Use a cheap dedicated model for mem0 extraction: gpt-4.1-nano (~$0.10/1M input tokens) or gpt-4o-mini. Configure separately from the user's main model
2. Log every mem0 LLM call with token count in structured logs. Add a `/status` or `/memory` command showing "X memories stored, Y tokens used for extraction this month"
3. Make memory extraction opt-in with clear cost disclosure in the setup wizard: "Memory learning uses ~X API calls per conversation session"
4. Consider Ollama for extraction if the user is self-hosting LLMs, eliminating cloud API costs entirely
5. In the setup wizard, separate "main LLM" from "memory extraction LLM" configuration

**Detection:**
- User's LLM provider dashboard showing 2-3x expected API calls
- Structured logs showing frequent mem0 LLM calls
- Users asking "why is my API bill so high?"
- Rate limit errors from the LLM provider during memory extraction

**Phase to address:** Phase 1 (configuration). Must decide LLM routing before implementing memory operations.

---

## Moderate Pitfalls

### Pitfall 6: mem0 MCP server startup order race condition with gateway

**What goes wrong:**
The new mem0 MCP server needs to connect to pgvector (and optionally Neo4j) on startup. If the gateway starts before pgvector is ready, or if the mem0 MCP server starts before its database backends are available, the server crashes or operates in a degraded state. The current knowledge MCP server uses ChromaDB PersistentClient which is local (no network dependency). mem0 with pgvector introduces a network dependency that didn't exist before.

**Why it happens:**
Railway starts services in parallel. There's no guaranteed startup order between the app container, pgvector, and Neo4j. The existing entrypoint.sh waits for goosed to be ready but doesn't wait for external databases.

**Prevention:**
1. Add connection retry logic to the mem0 MCP server: retry pgvector connection every 2s for up to 60s before failing
2. Use Railway's private networking (service.railway.internal DNS) which is zero-config but requires services to be in the same project
3. Gateway should detect mem0 MCP server health before routing memory operations to it. If unhealthy, fall back to no-memory mode (not crash)
4. Health check endpoint on the mem0 MCP server that verifies database connectivity

**Detection:**
- mem0 MCP server logs showing "connection refused" to pgvector on startup
- Memory operations silently failing (no memories stored)
- Gateway health check passing but memory features broken

**Phase to address:** Phase 2 (deployment). After the MCP server works locally, handle Railway networking.

---

### Pitfall 7: Dual memory systems during migration create inconsistent recall

**What goes wrong:**
During the transition period, the system has both ChromaDB (existing runtime memories) and mem0 (new memories). If the goose agent queries knowledge_search (ChromaDB MCP), it finds old memories but not new ones. If it queries the mem0 MCP, it finds new memories but not old ones. The agent gives inconsistent answers depending on which tool it calls. Users experience "the bot remembers some things but forgot others."

**Why it happens:**
Two separate vector stores with no cross-query capability. The goose agent has both MCP extensions available but doesn't know which one has which memories. It may call the wrong one, or call both and get confused by partial results.

**Prevention:**
1. During migration, create a unified search tool that queries BOTH stores and merges results (dedup by content similarity)
2. Set clear MCP tool descriptions: knowledge_search = "system docs and legacy memories", mem0_search = "user memories and learned facts"
3. Migrate ALL runtime memories before removing the old ChromaDB tools. Don't leave orphaned memories
4. After migration verified, remove the old knowledge_upsert tool so the agent can't write to ChromaDB runtime anymore (keep knowledge_search for system docs read-only)

**Detection:**
- Agent giving different answers to the same question at different times
- Users saying "you knew this yesterday but forgot today"
- Both MCP servers returning results for the same query with different content

**Phase to address:** Migration phase. Design the transition strategy before starting migration.

---

### Pitfall 8: mem0's posthog telemetry sends usage data to external servers from a privacy-focused self-hosted app

**What goes wrong:**
mem0ai's core dependency includes posthog (>3.5.0) for telemetry. By default, mem0 sends usage analytics to PostHog's servers. GooseClaw is positioned as a self-hosted, privacy-first personal agent. Sending telemetry about memory operations to an external analytics service contradicts this positioning and may violate user expectations.

**Why it happens:**
mem0 includes telemetry by default (common in open-source projects). The opt-out mechanism exists but isn't obvious. If you don't explicitly disable it, every `add()` and `search()` call sends anonymous usage data externally.

**Prevention:**
1. Set the environment variable `MEM0_TELEMETRY=false` in the Dockerfile or entrypoint.sh
2. Verify no outbound connections to PostHog IPs from the container
3. Document this in the setup wizard or deployment docs so users know telemetry is disabled
4. If posthog can't be disabled cleanly, consider patching it out or mocking the module

**Detection:**
- Network traffic logs showing connections to `app.posthog.com` or `us.posthog.com`
- Container firewall rules not blocking outbound analytics traffic

**Phase to address:** Phase 1. Set the env var in the first commit that adds mem0.

---

### Pitfall 9: mem0 default embedding model (text-embedding-3-small) may differ from ChromaDB's ONNX model, creating incompatible vectors

**What goes wrong:**
ChromaDB uses a local ONNX embedding model (all-MiniLM-L6-v2 by default). mem0 defaults to OpenAI's text-embedding-3-small. If you migrate ChromaDB embeddings to mem0's pgvector store, the vectors are from different embedding models and have different dimensions (384 vs 1536). Similarity search on mixed embeddings is meaningless. Queries return garbage results.

**Why it happens:**
The embedding model is configured separately in each system. ChromaDB bundles its own local embedder. mem0 defaults to a cloud API embedder. Nobody thinks to align them because both "just work" independently.

**Prevention:**
1. Choose ONE embedding model for mem0. If using OpenAI embeddings, you MUST re-embed all migrated ChromaDB content with the same model
2. If you want to keep local embeddings (no API cost), configure mem0 to use sentence-transformers with the same model ChromaDB uses, or configure a local embedding endpoint
3. During migration, always re-embed. Don't transfer ChromaDB embeddings directly to pgvector. The dimensions and model semantics won't match
4. Document which embedding model is used so future maintenance knows what to expect

**Detection:**
- Search results returning irrelevant memories after migration
- Vector dimension mismatch errors when inserting into pgvector
- Embedding API calls where you expected local embeddings (cost surprise)

**Phase to address:** Phase 1 (architecture decision). Embedding model choice affects migration, cost, and retrieval quality.

---

## Minor Pitfalls

### Pitfall 10: Neo4j Bolt protocol needs TCP proxy on Railway, not just HTTP

**What goes wrong:**
Railway's default networking exposes HTTP endpoints. Neo4j uses the Bolt protocol (binary, TCP-based) on port 7687. You need Railway's TCP proxy feature to expose Bolt externally, or use private networking (service.railway.internal) for internal access. If you only configure HTTP routing, the mem0 graph backend can't connect to Neo4j.

**Prevention:**
1. Use Railway private networking for service-to-service communication (zero config, no TCP proxy needed)
2. Connection string format: `bolt://neo4j.railway.internal:7687`
3. Only set up TCP proxy if you need external access to Neo4j (browser, debugging)

---

### Pitfall 11: mem0 Memory class is synchronous by default, but the gateway's memory writer runs in a thread

**What goes wrong:**
The gateway's `_memory_writer_loop()` runs in a daemon thread using `threading.Thread`. mem0's `Memory` class is synchronous and blocking. Using `AsyncMemory` requires an asyncio event loop, which the threading-based gateway doesn't have. You can't just await `async_mem0.add()` inside a thread.

**Prevention:**
1. Use synchronous `Memory` class in the threaded memory writer but with explicit timeouts
2. OR create a dedicated asyncio event loop in the memory writer thread: `loop = asyncio.new_event_loop(); loop.run_until_complete(async_mem0.add(...))`
3. OR refactor memory writer to use asyncio (bigger change, affects gateway architecture)
4. The simplest path: keep synchronous `Memory` in the thread, accept the blocking, set aggressive timeouts

---

### Pitfall 12: mem0's consolidation/dedup runs periodically and can merge memories the user wants kept separate

**What goes wrong:**
mem0 has a memory consolidation feature that periodically scans for similar embeddings (>0.85 cosine similarity) and merges them via LLM. This can merge memories that are similar but contextually different. Example: "Haseeb prefers TypeScript for frontend" and "Haseeb prefers TypeScript for backend" might get merged into "Haseeb prefers TypeScript" losing the nuance.

**Prevention:**
1. Disable automatic consolidation initially. Run it manually after reviewing what it would merge
2. Set the similarity threshold conservatively (0.95+ instead of 0.85)
3. Log all merge operations so they can be reviewed and reverted

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| MCP server scaffold | Dependency conflicts (Pitfall 2) | Separate requirements file, test full dependency resolution in CI |
| mem0 configuration | LLM cost surprise (Pitfall 5) | Configure cheap extraction model from day 1, log all LLM calls |
| pgvector deployment | Startup race condition (Pitfall 6) | Retry logic with exponential backoff, health check endpoint |
| Memory add integration | Blocking latency (Pitfall 1) | Async or threaded with timeout, batch extraction |
| Graph memory (Neo4j) | Cost explosion (Pitfall 3) | Defer to later phase, make optional, set memory limits |
| ChromaDB migration | Data loss/duplicates (Pitfall 4) | Direct vector store insert, dry-run first, keep ChromaDB as fallback |
| Transition period | Inconsistent recall (Pitfall 7) | Unified search tool querying both stores |
| Embedding setup | Model mismatch (Pitfall 9) | Choose one model, re-embed during migration |
| Production deploy | Telemetry leak (Pitfall 8) | MEM0_TELEMETRY=false in first commit |

## "Looks Done But Isn't" Checklist

- [ ] **mem0 add() timeout**: Verified that a stuck LLM call doesn't hang the memory writer thread forever
- [ ] **Dependency isolation**: `import mem0ai` never appears in gateway.py. It's only in the MCP server process
- [ ] **Embedding model alignment**: mem0 and any migrated data use the SAME embedding model
- [ ] **LLM cost tracking**: Structured logs show token count for every mem0 internal LLM call
- [ ] **Railway private networking**: mem0 MCP server connects to pgvector via `*.railway.internal`, not public URL
- [ ] **Neo4j memory limits**: `NEO4J_server_memory_heap_max__size` is explicitly set, not left to JVM defaults
- [ ] **Telemetry disabled**: `MEM0_TELEMETRY=false` is set before any mem0 code runs
- [ ] **Migration tested**: Migrated ChromaDB memories are searchable in mem0 with correct results
- [ ] **Fallback works**: If mem0 MCP server is down, the agent still functions (just without memory features)
- [ ] **Consolidation disabled**: Automatic memory merging is off until explicitly tested and enabled
- [ ] **ChromaDB still works**: System docs in ChromaDB "system" collection are unaffected by mem0 addition
- [ ] **Graph memory optional**: System works without Neo4j. Graph is an enhancement, not a requirement

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| mem0 add() hanging (Pitfall 1) | LOW | Kill stuck thread, disable memory writer, investigate timeout setting |
| Dependency conflict (Pitfall 2) | MEDIUM | Revert requirements.txt, rebuild Docker image, redesign dependency isolation |
| Railway cost spike (Pitfall 3) | LOW | Scale down Neo4j memory limits or remove Neo4j service entirely |
| Migration data loss (Pitfall 4) | MEDIUM | Restore from ChromaDB backup (kept during migration), re-run migration with fixes |
| API key bill shock (Pitfall 5) | LOW | Switch mem0 to cheaper model, disable graph extraction, reduce extraction frequency |
| Startup race (Pitfall 6) | LOW | Add retry logic to mem0 MCP server, restart the service |
| Inconsistent recall (Pitfall 7) | MEDIUM | Complete migration faster, or build unified search tool |
| Telemetry leak (Pitfall 8) | LOW | Set env var, restart. No data breach, just analytics |
| Embedding mismatch (Pitfall 9) | HIGH | Must re-embed ALL memories with correct model. No shortcut |

## Sources

- [mem0 Open Source Overview](https://docs.mem0.ai/open-source/overview) - Architecture, defaults, configuration (HIGH confidence)
- [mem0 Graph Memory Documentation](https://docs.mem0.ai/open-source/features/graph-memory) - Entity extraction, LLM calls, performance (HIGH confidence)
- [mem0 Breaking Changes v1.0.0](https://docs.mem0.ai/migration/breaking-changes) - API changes, response format (HIGH confidence)
- [mem0 v1.0 Migration Guide](https://github.com/mem0ai/mem0/blob/main/MIGRATION_GUIDE_v1.0.md) - Breaking changes, migration steps (HIGH confidence)
- [mem0 arxiv paper: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413) - Benchmarks, latency numbers, token costs (HIGH confidence)
- [GitHub Issue #2813: adding memory is taking 20 secs](https://github.com/mem0ai/mem0/issues/2813) - Self-hosted latency reports (HIGH confidence)
- [mem0 DeepWiki: Graph Memory Overview](https://deepwiki.com/mem0ai/mem0/4.1-graph-memory-overview) - LLM call pipeline details (MEDIUM confidence)
- [mem0 DeepWiki: Installation and Setup](https://deepwiki.com/mem0ai/mem0/1.2-installation-and-setup) - Dependencies, optional extras (MEDIUM confidence)
- [Railway Private Networking](https://docs.railway.com/guides/private-networking) - Service-to-service communication (HIGH confidence)
- [Railway Pricing](https://docs.railway.com/pricing) - Hobby plan limits, per-resource billing (HIGH confidence)
- [Railway pgvector Template](https://railway.com/deploy/pgvector-latest) - One-click pgvector deploy (HIGH confidence)
- [Railway Neo4j Template](https://railway.com/deploy/ZVljtU) - Memory requirements, Trial account limitations (HIGH confidence)
- [Neo4j Memory Configuration](https://neo4j.com/docs/operations-manual/current/performance/memory-configuration/) - Heap, page cache settings (HIGH confidence)
- [Apache NIFI-13741: qdrant-client and chromadb dependency conflict](https://issues.apache.org/jira/browse/NIFI-13741) - Protobuf version conflict documentation (HIGH confidence)
- [mem0 ChromaDB Configuration](https://docs.mem0.ai/components/vectordbs/dbs/chroma) - Provider name, config options (HIGH confidence)
- [MIF: Memory Interchange Format](https://zircote.com/blog/2026/02/introducing-mif-memory-interchange-format/) - Memory vendor lock-in discussion (MEDIUM confidence)
- [Self-Hosting Mem0 Docker Guide](https://mem0.ai/blog/self-host-mem0-docker) - Docker compose setup, security defaults (MEDIUM confidence)
- Codebase analysis: gateway.py memory writer (lines 6700-7094), knowledge/server.py (MCP server), requirements.txt, Dockerfile, entrypoint.sh

---
*Pitfalls research for: GooseClaw v5.0 mem0 Memory Layer Integration*
*Researched: 2026-03-19*

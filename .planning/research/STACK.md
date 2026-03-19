# Stack Research: mem0 Memory Layer Integration

**Domain:** AI agent memory system (vector + knowledge graph)
**Researched:** 2026-03-19
**Confidence:** HIGH (core stack), MEDIUM (Neo4j graph memory maturity)

## Existing Stack (DO NOT CHANGE)

These are validated and stay as-is:

| Technology | Version | Purpose | Status |
|------------|---------|---------|--------|
| Python 3.10 | 3.10.x | Container runtime | ubuntu:22.04 default |
| ChromaDB | 1.5.5 | System docs vectorization | KEEP for system docs |
| mcp[cli] | 1.26.0 | MCP server framework (FastMCP) | KEEP, shared by new extension |
| Node.js | 20.x | MCP tools runtime | KEEP |
| goosed | 1.27.2 | Goose AI agent server | KEEP |

## New Stack Additions

### Core: mem0ai Library

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| mem0ai | 1.0.5 | Memory layer with vector + graph support | Only library that does contradiction resolution + entity extraction + graph memory in one package. Active development (v1.0.5 released 2026-03-03). |

**Confidence:** HIGH. Verified on PyPI, v1.0.x is stable release line.

**Install:** `pip install "mem0ai[graph]"` (the `[graph]` extra pulls Neo4j dependencies)

**Key facts:**
- Python >=3.9 supported (our 3.10 is fine)
- Open-source mode requires NO Mem0 API key. You bring your own LLM, embedder, and vector store.
- Config via Python dict passed to `Memory.from_config(config)`
- Default vector store is Qdrant. We override to pgvector.
- Default LLM is OpenAI gpt-4.1-nano. We override to match user's configured provider.
- Graph memory is optional per-request via `enable_graph=False`

### Vector Store: PostgreSQL + pgvector (Railway Service)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| PostgreSQL (pgvector template) | 16 or 18 | Vector similarity search for memory embeddings | Railway has one-click pgvector templates ($5-15/mo). No need to run Postgres inside the app container. |
| psycopg2-binary | 2.9.11 | Python PostgreSQL adapter | Required by mem0's pgvector backend. Binary variant avoids needing libpq-dev in container. |
| pgvector (Python) | 0.4.2 | pgvector extension support for Python | Required by mem0 for vector operations with psycopg2. |

**Confidence:** HIGH. Railway offers dedicated pgvector templates (updated Mar 2026). Postgres is a separate Railway service, not inside the app container.

**Architecture decision: Railway service, NOT embedded in container.**
- Railway's pgvector template deploys a managed PostgreSQL with pgvector pre-installed
- The app container connects via `DATABASE_URL` environment variable
- This avoids bloating the app container with Postgres and simplifies backups
- Railway handles persistence, the app container stays stateless for databases
- Cost: ~$5-15/month depending on usage

**mem0 pgvector config:**
```python
config = {
    "vector_store": {
        "provider": "pgvector",
        "config": {
            "user": "<from DATABASE_URL>",
            "password": "<from DATABASE_URL>",
            "host": "<from DATABASE_URL>",
            "port": 5432,
            "embedding_model_dims": 1536,  # CRITICAL: must match embedder output
        }
    }
}
```

**Critical pitfall:** The `embedding_model_dims` parameter MUST match the embedding model's output dimensions. Mismatch causes `DataException: expected 1536 dimensions, not 768` on every insert. OpenAI text-embedding-3-small = 1536 dims, nomic-embed-text = 768 dims.

### Graph Store: Neo4j (Railway Service, Phase 2)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Neo4j Community | 5.x (latest) | Knowledge graph for entity relationships | mem0's graph memory backend. Stores "user prefers Python", "user works at X" relationships. Railway has one-click templates with APOC pre-installed. |

**Confidence:** MEDIUM. Neo4j works but has caveats (see pitfalls below).

**Architecture decision: Separate Railway service, deployed in Phase 2.**
- Railway has Neo4j templates with APOC plugin pre-loaded (mem0 depends on APOC)
- Default Docker memory: 512MB heap + 512MB page cache. Adequate for personal agent.
- Minimum viable: 1GB total RAM allocation
- Neo4j is OPTIONAL for mem0. Vector-only memory works fine without it.
- Phase the rollout: get vector memory working first, add graph later

**mem0 graph config:**
```python
config = {
    "graph_store": {
        "provider": "neo4j",
        "config": {
            "url": "bolt://<neo4j-host>:7687",
            "username": "neo4j",
            "password": "<from env>",
            "database": "neo4j",
        }
    }
}
```

**Critical bug (MEDIUM confidence, may be fixed by now):**
Issue #3711 reports `MemoryGraph.structuredLlm` was hardcoded to `openai_structured` provider, breaking graph memory for non-OpenAI LLM users. A fix was merged (nickrahman/mem0 PR) but unclear if it shipped in v1.0.5. If not, graph memory only works with OpenAI as the LLM provider, or requires a monkey-patch.

### LLM Configuration: Reuse User's Existing Provider

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| litellm (via mem0) | bundled | Universal LLM adapter for entity extraction | mem0 supports litellm provider which proxies to 100+ LLMs using the OpenAI format. Lets us reuse whatever provider the user already configured in their vault. |

**Confidence:** HIGH for OpenAI/Anthropic. MEDIUM for litellm with other providers.

**Architecture decision: Reuse the user's existing LLM, no separate API key.**
- GooseClaw's vault already stores the user's API key for their configured provider
- mem0's entity extraction needs an LLM. Use the same one.
- Configure mem0's `llm` section to match `GOOSE_PROVIDER` from vault
- For Anthropic users: mem0 supports Anthropic directly (`provider: "anthropic"`)
- For OpenAI users: mem0 supports OpenAI directly (`provider: "openai"`)
- For others: litellm provider acts as universal adapter

**mem0 LLM config (Anthropic example):**
```python
config = {
    "llm": {
        "provider": "anthropic",
        "config": {
            "model": "claude-sonnet-4-20250514",
            "temperature": 0.1,
            "max_tokens": 2000,
        }
    }
}
```

**For OpenAI:**
```python
config = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": "gpt-4.1-nano-2025-04-14",
            "temperature": 0.1,
            "max_tokens": 2000,
        }
    }
}
```

### Embedding Model: OpenAI text-embedding-3-small (Default)

| Technology | Dimensions | Purpose | Why Recommended |
|------------|-----------|---------|-----------------|
| text-embedding-3-small | 1536 | Vector embeddings for memory search | Default in mem0, best quality/cost ratio for cloud users. Most GooseClaw users already have an OpenAI key. |

**Confidence:** HIGH.

**Architecture decision: Use OpenAI embeddings by default, not Ollama.**
- GooseClaw runs on Railway (cloud). Ollama requires a GPU or beefy CPU container.
- Railway doesn't provide GPU instances for Ollama embedding.
- Most users already have an OpenAI API key (it's the most popular provider).
- text-embedding-3-small is $0.02/1M tokens. Cheap enough for a personal agent.
- nomic-embed-text outperforms on some benchmarks but requires Ollama infrastructure.
- If the user's provider is Anthropic-only (no OpenAI key), they need to add one for embeddings. Anthropic has no embedding API.

**Fallback strategy:**
1. User has OpenAI key in vault -> use text-embedding-3-small (1536 dims)
2. User has no OpenAI key -> require them to add one for embeddings (setup wizard prompt)
3. Future: support Voyage AI or other embedding APIs that don't need OpenAI

### MCP Extension Pattern

| Technology | Purpose | Why |
|------------|---------|-----|
| FastMCP (from mcp[cli]) | MCP server framework | Already used by knowledge extension. Same pattern, same dependency. |

**Architecture decision: Custom MCP server, NOT the official mem0-mcp-server package.**
- The official `mem0-mcp-server` package requires a Mem0 API key (cloud service)
- We run mem0 in open-source mode (self-hosted, no API key)
- Build a custom MCP server at `/app/docker/memory/server.py` following the same pattern as `/app/docker/knowledge/server.py`
- Use FastMCP with stdio transport
- Register in entrypoint.sh as a goosed extension (same YAML block pattern as knowledge)

**Extension registration pattern (entrypoint.sh):**
```yaml
memory:
  enabled: true
  type: stdio
  name: Memory
  description: Long-term memory with semantic search and entity relationships
  cmd: python3
  args:
    - /app/docker/memory/server.py
  envs:
    DATABASE_URL: ${DATABASE_URL}
    NEO4J_URL: ${NEO4J_URL}
    NEO4J_PASSWORD: ${NEO4J_PASSWORD}
    OPENAI_API_KEY: ${OPENAI_API_KEY}
  env_keys: []
```

## Supporting Libraries (pip additions)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| mem0ai[graph] | 1.0.5 | Memory layer with graph support | Always (core feature) |
| psycopg2-binary | 2.9.11 | PostgreSQL adapter | Always (pgvector backend) |
| pgvector | 0.4.2 | pgvector Python support | Always (pgvector backend) |

**Note:** `mem0ai[graph]` pulls in `neo4j` Python driver (6.1.0) automatically via the `[graph]` extra. No need to install separately.

## Installation

```bash
# Add to docker/requirements.txt (append to existing)
mem0ai[graph]==1.0.5
psycopg2-binary==2.9.11
pgvector==0.4.2
```

**No changes to apt-get dependencies.** psycopg2-binary includes compiled libpq, so no `libpq-dev` needed.

## Alternatives Considered

| Recommended | Alternative | Why Not |
|-------------|-------------|---------|
| mem0ai (open-source mode) | mem0 Platform (cloud API) | Requires Mem0 API key, sends data to third party. GooseClaw is self-hosted. |
| pgvector (Railway service) | Qdrant (embedded) | Qdrant is mem0's default but requires running Qdrant in-container or as separate service. pgvector piggybacks on PostgreSQL which Railway manages natively. |
| pgvector (Railway service) | ChromaDB (existing) | ChromaDB lacks connection pooling, runs in-process, and mem0 doesn't use it for contradiction resolution. pgvector is more production-grade. |
| Neo4j (Railway service) | Memgraph | Memgraph is faster for real-time queries but has a smaller ecosystem. Neo4j has more tutorials, better mem0 docs, and APOC support. Railway has Neo4j templates. |
| text-embedding-3-small | nomic-embed-text via Ollama | Ollama needs GPU/CPU resources Railway doesn't provide. nomic-embed-text is better on some benchmarks but impractical in this deployment model. |
| Custom MCP server | Official mem0-mcp-server | Official package requires Mem0 cloud API key. We need open-source mode. |
| litellm (via mem0) | Direct provider integration | litellm handles provider differences. Direct integration means maintaining separate code paths per provider. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| Qdrant embedded | Adds another in-memory database to the container. Railway volume persistence is awkward for Qdrant. | pgvector via Railway PostgreSQL service |
| Ollama in container | Railway has no GPU instances. CPU inference is slow and memory-hungry. A personal agent doesn't generate enough traffic to justify. | Cloud embedding API (OpenAI) |
| mem0 Platform API | Sends user data to Mem0's cloud. Violates self-hosted principle. Costs money per API call. | mem0ai open-source mode |
| Running Postgres inside app container | Mixing app + database in one container is fragile. No automatic backups. | Railway PostgreSQL service (separate) |
| neo4j-driver package | Deprecated since v6.0.0. Will receive no further updates. | `neo4j` package (installed via mem0ai[graph]) |
| Weaviate / Milvus / Pinecone | Over-engineered for single-user personal agent. All require separate infrastructure. | pgvector (already need Postgres) |

## Stack Patterns by Variant

**If user has OpenAI key (most common):**
- LLM: `provider: "openai"`, model from vault
- Embedder: `provider: "openai"`, text-embedding-3-small
- Simplest path, best tested

**If user has Anthropic key only:**
- LLM: `provider: "anthropic"`, model from vault
- Embedder: STILL needs OpenAI key for embeddings (Anthropic has no embedding API)
- Setup wizard should prompt for OpenAI key when enabling memory feature

**If user has other provider (Google, Groq, etc.):**
- LLM: `provider: "litellm"`, model string from vault
- Embedder: needs OpenAI key for embeddings
- litellm abstracts the provider differences

**If graph memory disabled (Phase 1):**
- Skip Neo4j entirely
- `enable_graph=False` on all mem0 calls
- Vector-only memory still gives contradiction resolution and semantic search

**If graph memory enabled (Phase 2):**
- Add Neo4j Railway service
- Configure graph_store in mem0 config
- Test the structuredLlm bug status before shipping

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| mem0ai 1.0.5 | Python 3.9-3.12 | Our 3.10 is in the sweet spot |
| mem0ai 1.0.5 | psycopg2 2.9.x | Uses psycopg2 connection pooling |
| psycopg2-binary 2.9.11 | PostgreSQL 10-18 | Covers all Railway PG templates |
| pgvector 0.4.2 | psycopg2 2.9.x | register_vector() for type registration |
| neo4j 6.1.0 | Neo4j 5.x server | Auto-installed by mem0ai[graph] |
| mcp[cli] 1.26.0 | mem0ai 1.0.5 | No conflicts, FastMCP used by both knowledge and memory extensions |
| chromadb 1.5.5 | mem0ai 1.0.5 | No conflicts. ChromaDB stays for system docs, mem0 uses pgvector. |

## Railway Infrastructure Changes

| Service | Type | Cost Estimate | When |
|---------|------|---------------|------|
| PostgreSQL + pgvector | Railway template (one-click) | $5-15/mo | Phase 1 (required) |
| Neo4j Community | Railway template (one-click) | $5-10/mo | Phase 2 (optional, graph memory) |

**Total new Railway cost:** ~$10-25/month for full stack, ~$5-15/month without graph memory.

**Environment variables to add:**
```
DATABASE_URL=postgresql://user:pass@host:port/dbname  (from Railway PG service)
NEO4J_URL=bolt://host:7687  (from Railway Neo4j service, Phase 2)
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<generated>
```

## Sources

- [mem0ai on PyPI](https://pypi.org/project/mem0ai/) -- version 1.0.5 verified (HIGH confidence)
- [mem0 Open Source Overview](https://docs.mem0.ai/open-source/overview) -- config defaults verified (HIGH confidence)
- [mem0 Graph Memory Overview](https://docs.mem0.ai/open-source/graph_memory/overview) -- Neo4j integration details (HIGH confidence)
- [mem0 pgvector Config](https://docs.mem0.ai/components/vectordbs/dbs/pgvector) -- connection parameters (HIGH confidence)
- [mem0 Anthropic LLM Config](https://docs.mem0.ai/components/llms/models/anthropic) -- provider config (HIGH confidence)
- [mem0 LiteLLM Config](https://docs.mem0.ai/components/llms/models/litellm) -- universal adapter (HIGH confidence)
- [mem0 Self-Host Docker Guide](https://mem0.ai/blog/self-host-mem0-docker) -- three-container architecture (MEDIUM confidence, page didn't fully render)
- [Railway pgvector Templates](https://railway.com/deploy/pgvector-latest) -- one-click deploy, updated Mar 2026 (HIGH confidence)
- [Railway Neo4j Templates](https://railway.com/deploy/asEF1B) -- APOC pre-installed (HIGH confidence)
- [mem0 MemoryGraph Bug #3711](https://github.com/mem0ai/mem0/issues/3711) -- structuredLlm hardcoded to OpenAI (MEDIUM confidence, fix merged but unsure if in v1.0.5)
- [pgvector Python on PyPI](https://pypi.org/project/pgvector/) -- v0.4.2 verified (HIGH confidence)
- [psycopg2-binary on PyPI](https://pypi.org/project/psycopg2-binary/) -- v2.9.11 verified (HIGH confidence)
- [neo4j Python driver on PyPI](https://pypi.org/project/neo4j/) -- v6.1.0 verified (HIGH confidence)
- [mem0 Ollama Companion Guide](https://docs.mem0.ai/cookbooks/companions/local-companion-ollama) -- local setup option (HIGH confidence)
- [Embedding Model Comparison](https://elephas.app/blog/best-embedding-models) -- nomic vs OpenAI benchmarks (MEDIUM confidence)

---
*Stack research for: mem0 memory layer integration into GooseClaw*
*Researched: 2026-03-19*

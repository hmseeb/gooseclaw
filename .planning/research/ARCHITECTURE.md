# Architecture Research: mem0 Memory Layer Integration

**Domain:** AI agent memory system (vector + knowledge graph) for GooseClaw
**Researched:** 2026-03-19
**Confidence:** HIGH (verified against official docs, multiple sources, existing codebase)

## System Overview: Current vs. Target

### Current Architecture (What Exists)

```
+-------------------------------------------------------------------+
|                    Railway Container (single)                      |
|-------------------------------------------------------------------|
|                                                                    |
|  entrypoint.sh                                                     |
|       |                                                            |
|       +-- gateway.py (HTTP server, stdlib only)                    |
|       |       |                                                    |
|       |       +-- memory_writer_loop() ----------+                 |
|       |       |   (idle detection,               |                 |
|       |       |    LLM extraction via            |                 |
|       |       |    goosed session,               |                 |
|       |       |    routes to user.md             |                 |
|       |       |    + ChromaDB runtime)           |                 |
|       |       |                                  |                 |
|       |       +-- goosed (lifecycle mgmt) -------+                 |
|       |               |                          |                 |
|       |               +-- knowledge MCP ---------+                 |
|       |               |   (server.py, stdio,                       |
|       |               |    ChromaDB system + runtime)              |
|       |               |                                            |
|       |               +-- memory MCP (builtin, goose-native)       |
|       |               +-- context7 MCP (npx, stdio)                |
|       |               +-- exa MCP (streamable_http)                |
|       |               +-- developer MCP (platform)                 |
|       |                                                            |
|       +-- telegram gateway (managed by gateway.py)                 |
|                                                                    |
|-------------------------------------------------------------------|
|  /data (Railway volume)                                            |
|  +-- knowledge/chroma/  (ChromaDB: system + runtime collections)   |
|  +-- identity/user.md   (identity traits, appended by memory       |
|  |                       writer)                                   |
|  +-- config/            (config.yaml, setup.json)                  |
|  +-- secrets/vault.yaml (API keys)                                 |
+-------------------------------------------------------------------+
```

### Target Architecture (What We're Building)

```
+-------------------------------------------------------------------+
|                    Railway Project (multi-service)                  |
|                                                                    |
|  +-------------------------------------------------------------+  |
|  |              GooseClaw Container (existing)                  |  |
|  |                                                              |  |
|  |  gateway.py                                                  |  |
|  |      |                                                       |  |
|  |      +-- memory_writer_loop() -- mem0 Python lib --------+  |  |
|  |      |   (MODIFIED: uses mem0.add() instead of            |  |  |
|  |      |    manual LLM extraction + ChromaDB upsert)        |  |  |
|  |      |                                                     |  |  |
|  |      +-- goosed                                            |  |  |
|  |              |                                             |  |  |
|  |              +-- mem0 MCP (NEW, stdio, Python) -----------+  |  |
|  |              |   (search, add, get tools for goose)       |  |  |
|  |              |                                             |  |  |
|  |              +-- knowledge MCP (KEPT for system docs)      |  |  |
|  |              +-- context7, exa, developer (unchanged)      |  |  |
|  |              +-- memory MCP (builtin, can disable)         |  |  |
|  |                                                            |  |  |
|  +--------------------------------------+---------------------+  |
|                                          | private network        |
|  +--------------------------------------+---------------------+  |
|  |         pgvector Service (Railway DB)                      |  |
|  |                                                            |  |
|  |  ankane/pgvector:v0.5.1                                    |  |
|  |  +-- mem0 vector embeddings                                |  |
|  |  +-- mem0 history (SQLAlchemy)                             |  |
|  |                                                            |  |
|  |  Volume: postgres_data (Railway-managed)                   |  |
|  +------------------------------------------------------------+  |
|                                                                    |
|  +------------------------------------------------------------+  |
|  |         Neo4j Service (Railway template) [PHASE 2]         |  |
|  |                                                            |  |
|  |  neo4j:5.26.4 + APOC                                      |  |
|  |  +-- entity graph (people, projects, orgs)                 |  |
|  |  +-- relationship edges                                    |  |
|  |                                                            |  |
|  |  Volume: neo4j_data (Railway-managed)                      |  |
|  +------------------------------------------------------------+  |
|                                                                    |
+-------------------------------------------------------------------+
```

## Integration Decision: MCP Server + Embedded Library (Hybrid)

**Decision: Use mem0 as BOTH an MCP server (for goose tool access) AND embedded in gateway.py (for the memory writer pipeline).**

### Why Not MCP-Only

The memory writer runs in gateway.py's background thread. It currently calls `_do_rest_relay()` to create a goosed session for LLM extraction, then manually parses JSON and upserts to ChromaDB. Replacing this with `mem0.Memory.add()` directly in gateway.py is cleaner because:

1. mem0's `add()` does LLM extraction + contradiction resolution + embedding + storage in one call
2. No need to create a goosed session just to extract memories (saves LLM tokens, avoids session overhead)
3. The memory writer already breaks the "stdlib only" rule by importing chromadb directly. Adding mem0ai is the same pattern.

### Why Not Embedded-Only

Goose needs MCP tools to search and add memories during conversations. An MCP server (stdio) provides `memory_search`, `memory_add`, etc. as tools goose can call directly. This is the standard GooseClaw extension pattern.

### The Hybrid Pattern

```
gateway.py (memory writer background thread)
    +-- from mem0 import Memory
        +-- memory.add(messages, user_id="haseeb")     # end-of-session extraction
        +-- memory.search(query, user_id="haseeb")      # internal lookups

mem0_mcp/server.py (MCP extension, stdio)
    +-- from mem0 import Memory
        +-- @mcp.tool() memory_search(...)               # goose calls during chat
        +-- @mcp.tool() memory_add(...)                  # goose stores facts live
        +-- @mcp.tool() memory_get(...)                  # goose retrieves by ID
```

Both share the same mem0 config pointing to the same pgvector + neo4j backends. No conflict because mem0 uses standard PostgreSQL connections (connection pooling handles concurrency).

## Component Responsibilities

| Component | Responsibility | New/Modified | Communicates With |
|-----------|---------------|-------------|-------------------|
| **mem0 MCP server** | Expose memory tools to goose (search, add, get, delete) | NEW | goosed (stdio), pgvector, neo4j |
| **gateway.py memory writer** | End-of-session extraction, idle detection, auto-feed conversations | MODIFIED | mem0 lib (embedded), pgvector, neo4j |
| **pgvector service** | Store vector embeddings, mem0 history tables | NEW (Railway service) | mem0 lib (via psycopg) |
| **Neo4j service** | Entity/relationship graph storage | NEW (Railway service, Phase 2) | mem0 lib (via neo4j driver) |
| **knowledge MCP** | System docs only (onboarding, schemas, platform reference) | MODIFIED (scope narrowed) | goosed (stdio), ChromaDB (local) |
| **ChromaDB** | System namespace only (no more runtime/user memories) | MODIFIED (runtime collection deprecated) | knowledge MCP server |
| **config.yaml** | mem0 MCP extension registration | MODIFIED | entrypoint.sh, goosed |
| **entrypoint.sh** | pgvector readiness check, mem0 config generation | MODIFIED | gateway.py, config files |
| **Dockerfile** | Install mem0ai + psycopg + neo4j driver | MODIFIED | pip install |
| **requirements.txt** | Add mem0ai, psycopg[binary], neo4j | MODIFIED | Dockerfile |

## Data Flow

### Flow 1: Goose Searches Memories (During Conversation)

```
User asks: "what was that project deadline?"
    |
    v
goosed receives message
    |
    v
goosed calls mem0 MCP tool: memory_search(query="project deadline", user_id="haseeb")
    |
    v
mem0 MCP server.py:
    1. memory.search(query="project deadline", user_id="haseeb", limit=5)
    2. mem0 lib embeds query via configured embedder
    3. pgvector returns top-k similar memories
    4. [Phase 2] neo4j returns related entities
    5. [optional] reranker reorders results
    |
    v
MCP returns structured results to goosed
    |
    v
goosed incorporates memories into response
```

### Flow 2: End-of-Session Memory Extraction (Background)

```
User goes idle for N minutes
    |
    v
gateway.py _memory_writer_loop() detects idle
    |
    v
Fetches conversation messages via _fetch_session_messages(sid)
    |
    v
CURRENT: creates goosed session, sends MEMORY_EXTRACT_PROMPT,
         parses JSON, manually upserts to ChromaDB + user.md

NEW: calls mem0.Memory.add(messages=conversation, user_id="haseeb")
     mem0 internally:
         1. LLM extracts facts from conversation
         2. Embeds each fact
         3. Searches pgvector for existing similar memories
         4. LLM decides ADD/UPDATE/DELETE for each fact
         5. Writes to pgvector + neo4j graph
    |
    v
Done. No goosed session needed. No manual JSON parsing.
```

### Flow 3: Goose Adds Memory Live (During Conversation)

```
User says: "remember, I switched to Cursor from VS Code"
    |
    v
goosed calls mem0 MCP tool: memory_add(
    messages=[{"role": "user", "content": "I switched to Cursor from VS Code"}],
    user_id="haseeb"
)
    |
    v
mem0 processes: extracts fact "user switched from VS Code to Cursor",
                checks for existing "user uses VS Code" memory,
                LLM decides UPDATE (contradiction resolution),
                updates pgvector embedding, updates neo4j graph
    |
    v
MCP returns confirmation to goosed
```

### Flow 4: Identity Routing (What Changes for user.md)

The current memory writer routes "identity" traits to user.md and "knowledge" to ChromaDB. With mem0, this bifurcation needs rethinking.

**Recommendation: Let mem0 handle ALL memory storage. Stop writing to user.md automatically.**

Rationale:
- mem0's contradiction resolution handles updates natively. The current user.md append-only model accumulates contradictions.
- user.md becomes a manually curated file (edited by user or goose via developer MCP), not an auto-append target.
- mem0 can store identity-type memories with metadata tags (e.g., `metadata={"category": "identity"}`) for filtering.
- Searching mem0 for identity facts is faster and more reliable than parsing a markdown file.

If user.md must remain as a read source for identity context, goose can read it via developer MCP. But writing should go through mem0.

## Railway Deployment Architecture

### Multi-Service Setup

Railway supports multiple services within one project. Each service gets its own container. Services communicate over a private network using `<service>.railway.internal` DNS.

**Service 1: GooseClaw (existing)**
- Dockerfile: existing, add mem0ai to requirements.txt
- Volume: /data (existing Railway volume)
- Env vars: add `MEM0_PGVECTOR_HOST`, `MEM0_PGVECTOR_PORT`, `MEM0_PGVECTOR_PASSWORD`, `MEM0_NEO4J_URI`, `MEM0_NEO4J_PASSWORD`
- References pgvector and neo4j via Railway service reference variables

**Service 2: pgvector**
- Image: `ankane/pgvector:v0.5.1` (or Railway's pgvector template, updated March 2026)
- Volume: Railway-managed postgres data
- Exposed internally: `pgvector.railway.internal:5432`
- Healthcheck: `pg_isready`
- Init: `CREATE EXTENSION IF NOT EXISTS vector;` (auto via image)

**Service 3: Neo4j (Phase 2)**
- Image: `neo4j:5.26.4`
- Volume: Railway-managed neo4j data
- Exposed internally: `neo4j.railway.internal:7687` (bolt)
- Env: `NEO4J_AUTH=neo4j/<password>`, `NEO4J_PLUGINS=["apoc"]`
- Memory: Neo4j needs ~512MB minimum. Railway VMs support this on paid tier.
- WARNING: Cannot run on Railway trial accounts (VMs too small for Neo4j)

### Environment Variable Wiring

```yaml
# GooseClaw service references (in Railway dashboard):
MEM0_PGVECTOR_HOST: ${{pgvector.PGHOST}}
MEM0_PGVECTOR_PORT: ${{pgvector.PGPORT}}
MEM0_PGVECTOR_USER: ${{pgvector.PGUSER}}
MEM0_PGVECTOR_PASSWORD: ${{pgvector.POSTGRES_PASSWORD}}
MEM0_PGVECTOR_DB: ${{pgvector.PGDATABASE}}

# Phase 2:
MEM0_NEO4J_URI: bolt://${{neo4j.RAILWAY_PRIVATE_DOMAIN}}:7687
MEM0_NEO4J_PASSWORD: <configured password>
```

### mem0 Config Generation (shared module)

```python
# docker/mem0_config.py
# Builds mem0 config dict from environment variables at boot time.
# Shared between gateway.py (embedded) and mem0_mcp/server.py (MCP).

import os

# Maps GOOSE_PROVIDER values to mem0 LLM provider names
GOOSE_TO_MEM0_PROVIDER = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "groq": "groq",
    "openrouter": "litellm",    # mem0 doesn't support openrouter natively
    "ollama": "ollama",
    "azure-openai": "azure_openai",
    "deepseek": "deepseek",
}

def build_mem0_config():
    """Build mem0 config dict from environment variables."""
    # detect LLM provider from goose config
    goose_provider = os.environ.get("GOOSE_PROVIDER", "anthropic")
    mem0_provider = GOOSE_TO_MEM0_PROVIDER.get(goose_provider, "openai")

    config = {
        "llm": {
            "provider": mem0_provider,
            "config": {
                "model": os.environ.get("MEM0_LLM_MODEL", _default_model(mem0_provider)),
                "temperature": 0.1,
                "max_tokens": 2000,
            }
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "text-embedding-3-small",
            }
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "host": os.environ.get("MEM0_PGVECTOR_HOST", "localhost"),
                "port": int(os.environ.get("MEM0_PGVECTOR_PORT", "5432")),
                "user": os.environ.get("MEM0_PGVECTOR_USER", "postgres"),
                "password": os.environ.get("MEM0_PGVECTOR_PASSWORD", ""),
                "dbname": os.environ.get("MEM0_PGVECTOR_DB", "postgres"),
            }
        },
    }

    # optional neo4j graph store (Phase 2)
    neo4j_uri = os.environ.get("MEM0_NEO4J_URI")
    if neo4j_uri:
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": neo4j_uri,
                "username": "neo4j",
                "password": os.environ.get("MEM0_NEO4J_PASSWORD", ""),
            }
        }

    return config


def _default_model(provider):
    """Sensible default extraction model per provider."""
    defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4.1-nano-2025-04-14",
        "groq": "llama-3.3-70b-versatile",
        "ollama": "llama3.2",
    }
    return defaults.get(provider, "gpt-4.1-nano-2025-04-14")
```

## Recommended Project Structure

```
docker/
+-- gateway.py                    # MODIFIED: memory writer uses mem0.add()
+-- knowledge/
|   +-- server.py                 # KEPT: system docs only, ChromaDB
+-- mem0_mcp/
|   +-- server.py                 # NEW: MCP server wrapping mem0 for goose
|   +-- migrate_from_chromadb.py  # NEW: one-time migration script
+-- mem0_config.py                # NEW: shared mem0 config builder
+-- requirements.txt              # MODIFIED: add mem0ai, psycopg[binary]
+-- entrypoint.sh                 # MODIFIED: pgvector readiness check
```

### Structure Rationale

- **mem0_mcp/server.py** follows the same pattern as knowledge/server.py. FastMCP, stdio transport, self-contained.
- **mem0_config.py** is shared between gateway.py and mem0_mcp/server.py to keep config DRY. Both import it.
- **knowledge/server.py** stays untouched. ChromaDB handles system docs, mem0 handles user memories. Clean separation.

### mem0 MCP Server (docker/mem0_mcp/server.py)

```python
"""MCP server wrapping mem0 for goose tool access.

Provides: memory_search, memory_add, memory_get_all, memory_delete
Transport: stdio (standard GooseClaw MCP pattern)
"""
import os
import sys
import json
import logging
from mcp.server.fastmcp import FastMCP
from mem0 import Memory

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("mem0-mcp")

# import shared config builder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mem0_config import build_mem0_config

config = build_mem0_config()
memory = Memory.from_config(config)

DEFAULT_USER_ID = os.environ.get("MEM0_USER_ID", "haseeb")

mcp = FastMCP("mem0-memory")


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search memories semantically. Returns relevant stored memories.

    Args:
        query: Natural language search query
        limit: Max results (default 5)
    """
    results = memory.search(query=query, user_id=DEFAULT_USER_ID, limit=limit)
    if not results:
        return "No matching memories found."
    lines = []
    for r in results:
        score = r.get("score", "?")
        text = r.get("memory", r.get("text", ""))
        mid = r.get("id", "?")
        lines.append(f"[{score}] {text} (id: {mid})")
    return "\n".join(lines)


@mcp.tool()
def memory_add(text: str) -> str:
    """Store a new memory. Use for facts, preferences, project details.

    Args:
        text: The memory to store
    """
    result = memory.add(
        messages=[{"role": "user", "content": text}],
        user_id=DEFAULT_USER_ID,
    )
    return json.dumps(result, default=str)


@mcp.tool()
def memory_get_all(limit: int = 20) -> str:
    """List all stored memories, newest first.

    Args:
        limit: Max results (default 20)
    """
    results = memory.get_all(user_id=DEFAULT_USER_ID)
    if not results:
        return "No memories stored yet."
    lines = []
    for r in results[:limit]:
        text = r.get("memory", r.get("text", ""))
        mid = r.get("id", "?")
        lines.append(f"- {text} (id: {mid})")
    return "\n".join(lines)


@mcp.tool()
def memory_delete(memory_id: str) -> str:
    """Delete a specific memory by ID.

    Args:
        memory_id: The ID of the memory to delete
    """
    memory.delete(memory_id=memory_id)
    return f"Deleted memory: {memory_id}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Config Extension Registration (in entrypoint.sh default extensions)

```yaml
mem0-memory:
  enabled: true
  type: stdio
  name: mem0-memory
  description: Long-term memory with semantic search and contradiction resolution
  cmd: python3
  args:
    - /app/docker/mem0_mcp/server.py
  envs:
    MEM0_USER_ID: haseeb
  env_keys: []
  timeout: 300
  bundled: null
  available_tools: []
```

## Architectural Patterns

### Pattern 1: Shared Config, Separate Processes

**What:** Both gateway.py and mem0 MCP server instantiate their own `Memory` object from the same config, pointing to the same pgvector/neo4j backends.
**When to use:** When two processes need the same data but run in separate Python processes (gateway.py thread vs. goosed subprocess).
**Trade-offs:** Simple, no inter-process communication needed. Slightly higher memory usage (two mem0 instances). pgvector handles concurrent connections natively via connection pooling.

### Pattern 2: Phased Graph Integration

**What:** Deploy pgvector first (Phase 1), add Neo4j later (Phase 2). mem0 config simply omits `graph_store` in Phase 1.
**When to use:** When one dependency is heavy (Neo4j: 512MB+ RAM, APOC plugin, separate Railway service) and the core value (vector search + contradiction resolution) works without it.
**Trade-offs:** Phase 1 gets 80% of the value (semantic memory search, LLM extraction, contradiction resolution). Phase 2 adds entity relationships (20% incremental value, high setup cost).

### Pattern 3: LLM Provider Reuse

**What:** mem0's LLM for fact extraction reuses the same provider/API key the user already configured for goose. No additional API key required.
**When to use:** Always. GooseClaw users already have an LLM provider configured.
**Trade-offs:** Extraction uses the user's LLM tokens. Could add a "use smaller model for extraction" option later. Must handle provider mapping (GOOSE_PROVIDER to mem0 provider name).

### Pattern 4: Embedder Strategy

**What:** mem0 needs an embedder for vector storage. Default is OpenAI text-embedding-3-small (1536 dims).

| Option | Pros | Cons |
|--------|------|------|
| OpenAI text-embedding-3-small | Best quality, 1536 dims, industry standard | Requires OpenAI API key even if user uses Anthropic |
| ChromaDB default (ONNX) | No API key, free, already in container | Lower quality, 384 dims, incompatible with pgvector setup |
| Provider-matched embedder | No extra key needed | Quality varies, not all providers offer embedders |

**Recommendation:** Default to OpenAI embeddings if OPENAI_API_KEY is available (from vault or env). Fall back to the provider's own embedder if available. Last resort: require an OpenAI key for embeddings (document this clearly in setup).

Critical constraint: embedding dimensions MUST match across all memory operations. Changing the embedder after initial setup requires re-embedding all memories. Pin the embedder in config and warn loudly if it changes.

## Migration Plan: ChromaDB Runtime to mem0

### What Moves

| Data | From | To | Migration |
|------|------|----|-----------|
| User memories (runtime collection) | ChromaDB /data/knowledge/chroma | pgvector via mem0 | One-time migration script |
| System docs (system collection) | ChromaDB | ChromaDB (stays) | No change |
| Identity traits | user.md (auto-appended) | mem0 pgvector (with metadata) | Optional, user.md stays as manual file |

### Migration Script Approach

```python
# docker/mem0_mcp/migrate_from_chromadb.py
# Run once on first boot after mem0 is deployed.
#
# 1. Check sentinel: /data/knowledge/.mem0_migrated
# 2. Read all runtime collection entries from ChromaDB
# 3. For each entry, call mem0.add() with the content text
# 4. mem0 handles re-embedding and storage in pgvector
# 5. Touch sentinel file
#
# Safe to re-run (sentinel prevents double migration).
# ChromaDB runtime collection kept as read-only backup.
```

### Backwards Compatibility

- Knowledge MCP server continues to serve system docs from ChromaDB. No change.
- Runtime collection in ChromaDB becomes read-only after migration (kept as backup).
- ChromaDB remains installed (knowledge MCP needs it for system docs).
- The builtin "memory" goose extension can be disabled in config.yaml once mem0 MCP is proven stable.

## Scaling Considerations

| Concern | Current (single user) | Notes |
|---------|----------------------|-------|
| pgvector connections | 2-3 concurrent (MCP + gateway writer) | Fine. PostgreSQL handles hundreds. |
| Embedding cost | ~$0.0001/memory (text-embedding-3-small) | Negligible for personal use. |
| LLM extraction cost | ~$0.001/conversation (small model) | One call per idle session. |
| Neo4j memory | 512MB+ for single user | Fine on Railway paid tier. |
| pgvector storage | ~1KB/memory * 10K memories = 10MB | Grows linearly. Fine for years. |

This is a personal agent. Single user, single instance. Scaling concerns are minimal. The architecture handles tens of thousands of memories without breaking a sweat.

### First Bottleneck: Embedding API Latency

mem0.add() calls the embedding API for each extracted fact. If a conversation yields 10 facts, that is 10 embedding API calls. With OpenAI, each takes ~100ms. Total: ~1s. Acceptable for background extraction. Would only matter if doing real-time add during conversation (and even then, user doesn't wait for it).

### Second Bottleneck: LLM Extraction Cost

Each session extraction requires one LLM call to extract facts, then one more per fact for contradiction resolution. Using a cheap model (gpt-4.1-nano, claude-haiku) keeps cost under $0.01/session. If the user's primary model is expensive (gpt-4.1, opus), the config should default to a cheaper extraction model.

## Anti-Patterns

### Anti-Pattern 1: Running pgvector Inside the GooseClaw Container

**What people do:** Add postgres to the Dockerfile, run it alongside gateway.py in one container.
**Why it's wrong:** Railway mounts one volume per service. Postgres needs its own data volume. Also violates one-process-per-container. If postgres crashes, gateway restarts too. If container rebuilds, postgres data gone unless separately volume-mounted.
**Do this instead:** Deploy pgvector as a separate Railway service. Use Railway's private networking. One-click template available.

### Anti-Pattern 2: Using mem0's Hosted Platform API

**What people do:** Use `MEM0_API_KEY` to hit mem0.ai's cloud API instead of self-hosting.
**Why it's wrong:** GooseClaw is self-hosted by design. Sending user memories to a third-party cloud service defeats the purpose. Also adds latency and ongoing cost.
**Do this instead:** Use mem0ai Python library in embedded mode pointing to your own pgvector.

### Anti-Pattern 3: Creating a Goosed Session for Memory Extraction

**What people do:** The current approach: create a goosed session, send MEMORY_EXTRACT_PROMPT, parse the JSON response, manually upsert to ChromaDB.
**Why it's wrong:** Wasteful. Uses goosed session overhead, MCP tool loading, prompt formatting. mem0 does all of this internally with better contradiction resolution.
**Do this instead:** Call `mem0.Memory.add(messages)` directly. One function call replaces ~100 lines of extraction logic in gateway.py.

### Anti-Pattern 4: Running Neo4j on Railway Trial Accounts

**What people do:** Deploy Neo4j on a trial Railway account.
**Why it's wrong:** Neo4j needs ~512MB RAM minimum. Trial VMs are too small. The deployment will fail or OOM constantly.
**Do this instead:** Make Neo4j optional (Phase 2). Vector-only mem0 works great without it. Gate Neo4j behind a config flag. Document that paid Railway plan is needed.

### Anti-Pattern 5: Embedding Dimension Mismatch

**What people do:** Change the embedding model without recreating the pgvector table.
**Why it's wrong:** text-embedding-3-small produces 1536 dims. If you switch to a model producing 768 dims, pgvector throws `DataException: expected 1536 dimensions, not 768` on every insert.
**Do this instead:** Pin the embedding model in mem0 config. If you must change it, provide a migration path that re-embeds all memories. Or, more practically, just wipe and rebuild.

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| pgvector | TCP via Railway private network | `pgvector.railway.internal:5432`, psycopg driver |
| Neo4j | Bolt via Railway private network | `neo4j.railway.internal:7687`, neo4j Python driver |
| LLM API | HTTPS to provider | Reuses existing GOOSE_PROVIDER API key from vault |
| Embedding API | HTTPS to OpenAI (or provider) | May need separate OPENAI_API_KEY for embeddings |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| gateway.py to mem0 lib | In-process Python import | Same process, no IPC overhead |
| goosed to mem0 MCP | stdio (MCP protocol) | Standard GooseClaw extension pattern |
| mem0 lib to pgvector | TCP (psycopg) | Private Railway network, sub-ms latency |
| mem0 lib to Neo4j | Bolt protocol (TCP) | Private Railway network, Phase 2 |
| knowledge MCP to ChromaDB | In-process (chromadb lib) | Unchanged, system docs only |

## Build Order (Suggested Phases)

### Phase 1: pgvector + mem0 Core (Highest Value)

1. Add mem0ai + psycopg[binary] to requirements.txt, rebuild Docker image
2. Deploy pgvector as Railway service (one-click template)
3. Wire Railway service reference variables to GooseClaw env
4. Create docker/mem0_config.py (shared config builder from env vars)
5. Create docker/mem0_mcp/server.py (MCP tools for goose)
6. Register mem0-memory extension in entrypoint.sh default extensions
7. Add pgvector readiness check to entrypoint.sh (wait for pg_isready before starting gateway)
8. Test: goose can search/add memories via MCP tools

### Phase 2: Memory Writer Migration

1. Modify gateway.py `_memory_writer_loop()` to use mem0.add() instead of manual extraction
2. Remove MEMORY_EXTRACT_PROMPT, `_process_memory_extraction()`, `_extract_json_from_response()`, and related functions (~150 lines)
3. Remove ChromaDB direct imports from gateway.py (`_get_knowledge_collection`, etc.)
4. Create migration script: docker/mem0_mcp/migrate_from_chromadb.py
5. Add migration to entrypoint.sh (run once, sentinel file)
6. Test: end-of-session memories land in pgvector, searchable via MCP

### Phase 3: Neo4j Graph (Entity Relationships)

1. Deploy Neo4j as Railway service (template)
2. Add neo4j env vars to GooseClaw service
3. Update mem0_config.py to include graph_store when MEM0_NEO4J_URI is set
4. Test: entity extraction populates graph, search returns related entities

### Phase 4: Polish and Cleanup

1. Add memory-related Telegram commands (/remember, /memories, /forget)
2. Memory settings in setup wizard (idle minutes, enable/disable, embedding model)
3. Disable builtin goose "memory" extension (redundant with mem0)
4. Remove runtime collection writes from knowledge MCP (if still referenced anywhere)
5. Documentation for Railway multi-service setup

## Open Questions

1. **Embedder API key**: If user only has Anthropic, do we require an OpenAI key for embeddings? Or can we use Anthropic's embedder (if mem0 supports it)? Fallback to local embeddings?
2. **LLM model for extraction**: Use the same model as GOOSE_MODEL (potentially expensive) or force a smaller/cheaper model? Should this be configurable in the setup wizard?
3. **user.md fate**: Stop auto-writing entirely, or keep as a secondary read-only identity reference that goose reads via .goosehints?
4. **mem0 version pinning**: mem0ai hit v1.0.0 recently. Pin exactly (`mem0ai==1.0.0`) or allow patches (`mem0ai>=1.0.0,<2.0.0`)?
5. **Connection health**: What happens when pgvector goes down? mem0 will throw. Does the MCP server crash (goosed restarts it) or does it return errors gracefully?

## Sources

- [mem0 official docs: pgvector config](https://docs.mem0.ai/components/vectordbs/dbs/pgvector) - HIGH confidence
- [mem0 official docs: Anthropic LLM](https://docs.mem0.ai/components/llms/models/anthropic) - HIGH confidence
- [mem0 architecture deep wiki](https://deepwiki.com/mem0ai/mem0) - HIGH confidence
- [mem0 memory configuration](https://deepwiki.com/mem0ai/mem0/3.1-memory-configuration) - HIGH confidence
- [mem0 GitHub](https://github.com/mem0ai/mem0) - HIGH confidence
- [mem0 official MCP server](https://github.com/mem0ai/mem0-mcp) - HIGH confidence
- [Self-hosting mem0 Docker guide (dev.to)](https://dev.to/mem0/self-hosting-mem0-a-complete-docker-deployment-guide-154i) - MEDIUM confidence
- [Railway pgvector template](https://railway.com/deploy/pgvector-latest) - HIGH confidence
- [Railway PostgreSQL docs](https://docs.railway.com/databases/postgresql) - HIGH confidence
- [Railway Neo4j template](https://railway.com/deploy/asEF1B) - HIGH confidence
- [Railway private networking discussion](https://station.railway.com/questions/private-internal-vs-public-ur-ls-between-cbbbbea0) - MEDIUM confidence

---
*Architecture research for: mem0 memory layer integration into GooseClaw v5.0*
*Researched: 2026-03-19*

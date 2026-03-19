# Phase 25: Neo4j Knowledge Graph - Research

**Researched:** 2026-03-20
**Domain:** Neo4j graph database in-container, mem0 graph memory, entity/relationship MCP tools
**Confidence:** MEDIUM-HIGH

## Summary

Phase 25 adds entity relationship extraction to GooseClaw's memory system by enabling mem0's graph memory feature backed by Neo4j running inside the same container. Neo4j Community Edition gets installed via apt-get in the Dockerfile (requires OpenJDK 21, ~500MB image size increase), started as a background process by entrypoint.sh before the gateway, and persists data on the /data volume. The mem0 config module (`docker/mem0_config.py`) gets a `graph_store` section pointing at `bolt://localhost:7687`, and the memory MCP server gains two new tools: `memory_entities` and `memory_relations`.

The critical architectural insight is that mem0's Python graph memory implementation is provider-agnostic. The Node.js issue #3711 (structuredLlm hardcoded to OpenAI) does NOT affect Python. The Python `graph_memory.py` correctly uses the user-configured LLM provider for entity extraction and relationship establishment. Graph operations run in parallel with vector operations via ThreadPoolExecutor, adding ~2-3 extra LLM calls per `add()` (entity extraction + relationship extraction + deletion detection) and ~1 extra LLM call per `search()` (entity extraction from query). The search return format changes: `{"results": [...], "relations": [{"source": "...", "relationship": "...", "destination": "..."}]}`.

The main risks are: (1) Neo4j JVM heap consuming 512MB-1GB RAM on Railway, (2) Docker image bloat from OpenJDK 21 + Neo4j + langchain-neo4j dependencies, (3) entrypoint.sh complexity of starting Neo4j as background process and waiting for Bolt readiness, (4) additional LLM cost from graph extraction calls (2-3 extra per add()).

**Primary recommendation:** Install Neo4j Community via apt-get in Dockerfile, start with `neo4j console &` in entrypoint.sh with a Bolt readiness wait loop, configure JVM heap to 256m/512m, persist data to `/data/neo4j`, and keep graph memory optional (system works without Neo4j if it fails to start).

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| GRAPH-01 | Neo4j runs inside the same container, started by entrypoint, data on /data volume | Dockerfile apt-get install neo4j, entrypoint.sh `neo4j console &` with wait loop, `server.directories.data=/data/neo4j` config |
| GRAPH-02 | mem0 graph memory enabled for entity relationship extraction | `graph_store` section in `build_mem0_config()`, `pip install "mem0ai[graph]"`, provider-agnostic Python implementation |
| GRAPH-03 | Relationship-enhanced search (graph augments vector results) | mem0 search returns `{"results": [...], "relations": [...]}`, parallel graph+vector search via ThreadPoolExecutor |
| GRAPH-04 | Entity and relationship tools exposed via MCP (memory_entities, memory_relations) | New tools in `docker/memory/server.py` querying Neo4j graph via mem0 or direct Cypher |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| neo4j (apt package) | 2026.02.2 | Graph database server | Official Debian package, only graph DB mem0 supports well in Python |
| openjdk-21-jre-headless | 21 | Neo4j JVM runtime | Required by Neo4j 2026.x, headless variant saves ~200MB vs full JDK |
| mem0ai[graph] | 1.0.6 | Graph memory extras | Pulls langchain-neo4j, neo4j driver, rank-bm25, kuzu |
| langchain-neo4j | >=0.4.0 | Neo4j graph integration for mem0 | Pulled by mem0ai[graph], provides Neo4jGraph class |
| neo4j (pip) | >=5.23.1 | Python Neo4j Bolt driver | Pulled by mem0ai[graph], used by langchain-neo4j |
| rank-bm25 | >=0.2.2 | BM25 reranking for graph search | Pulled by mem0ai[graph], used for relation relevance ranking |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| kuzu | >=0.11.0 | Embedded graph DB (unused but pulled by mem0ai[graph]) | Not used, comes as dependency |
| langchain-aws | >=0.2.23 | Neptune support (unused but pulled by mem0ai[graph]) | Not used, comes as dependency |
| langchain-memgraph | >=0.1.0 | Memgraph support (unused but pulled by mem0ai[graph]) | Not used, comes as dependency |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Neo4j in-container (apt) | Neo4j Docker sidecar on Railway | Separate Railway service costs $5-10/mo extra, violates "zero DevOps" principle |
| Neo4j | FalkorDB (embedded) | No mem0 integration, would need custom graph code |
| Neo4j | Kuzu (embedded, no JVM) | mem0 supports Kuzu but less mature than Neo4j integration |
| Full neo4j package | neo4j tarball | apt-get handles JDK dependency automatically, tarball requires manual Java setup |

**Installation:**
```bash
# Dockerfile additions
RUN wget -O - https://debian.neo4j.com/neotechnology.gpg.key | gpg --dearmor -o /etc/apt/keyrings/neotechnology.gpg && \
    echo 'deb [signed-by=/etc/apt/keyrings/neotechnology.gpg] https://debian.neo4j.com stable latest' > /etc/apt/sources.list.d/neo4j.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends neo4j && \
    rm -rf /var/lib/apt/lists/*

# requirements.txt change: mem0ai==1.0.6 -> mem0ai[graph]==1.0.6
# (replaces existing mem0ai line, adds graph extras)
```

## Architecture Patterns

### Recommended Project Structure
```
docker/
+-- mem0_config.py           # MODIFIED: add graph_store section
+-- memory/
|   +-- server.py            # MODIFIED: add memory_entities, memory_relations tools
+-- entrypoint.sh            # MODIFIED: start neo4j, wait for bolt, configure data dir
+-- requirements.txt         # MODIFIED: mem0ai[graph]==1.0.6
Dockerfile                   # MODIFIED: install neo4j + openjdk-21
```

### Pattern 1: Neo4j In-Container Background Process
**What:** Start Neo4j as a background process in entrypoint.sh before the gateway, with a readiness wait loop.
**When to use:** When running Neo4j in the same container as the application (no separate service).
**Example:**
```bash
# In entrypoint.sh, before gateway start
# ---- neo4j graph database (background) ----
if [ -f /usr/bin/neo4j ] || command -v neo4j &>/dev/null; then
    echo "[neo4j] starting graph database..."

    # Configure neo4j for in-container use
    NEO4J_CONF_DIR="/etc/neo4j"
    # Persist data on /data volume
    mkdir -p /data/neo4j
    chown -R neo4j:neo4j /data/neo4j

    # Set memory limits (constrained for Railway)
    # Env vars: NEO4J_ prefix, dots->underscores, underscores->double underscores
    export NEO4J_server_memory_heap_initial__size=256m
    export NEO4J_server_memory_heap_max__size=512m
    export NEO4J_server_memory_pagecache__size=128m
    export NEO4J_server_directories_data=/data/neo4j

    # Disable auth for localhost-only access (same container)
    export NEO4J_AUTH=none

    # Disable telemetry
    export NEO4J_server_analytics_enabled=false

    # Start neo4j in background (console mode, not daemon)
    neo4j console &
    NEO4J_PID=$!

    # Wait for Bolt port readiness (max 60 seconds)
    echo "[neo4j] waiting for bolt://localhost:7687..."
    for i in $(seq 1 60); do
        if neo4j status 2>/dev/null | grep -q "running"; then
            echo "[neo4j] ready (${i}s)"
            break
        fi
        if ! kill -0 "$NEO4J_PID" 2>/dev/null; then
            echo "[neo4j] FAILED to start (check logs)"
            break
        fi
        sleep 1
    done
else
    echo "[neo4j] not installed, graph memory disabled"
fi
```

### Pattern 2: Graph Store Config in mem0_config.py
**What:** Add `graph_store` section to the mem0 config dict when Neo4j is available.
**When to use:** Always, but with graceful fallback if Neo4j is not running.
**Example:**
```python
# In docker/mem0_config.py, inside build_mem0_config()
def build_mem0_config():
    # ... existing vector_store, embedder, llm config ...

    config = {
        "vector_store": { ... },  # existing
        "embedder": { ... },       # existing
        "llm": { ... },            # existing
        "version": "v1.1",
    }

    # Graph store (Neo4j) - optional, enabled when NEO4J available
    neo4j_url = os.environ.get("NEO4J_URL", "bolt://localhost:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_pass = os.environ.get("NEO4J_PASSWORD", "")

    # Only enable if neo4j URL is configured (entrypoint sets this)
    if os.environ.get("NEO4J_ENABLED", "").lower() in ("true", "1", "yes"):
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": neo4j_url,
                "username": neo4j_user,
                "password": neo4j_pass,
            }
        }

    return config
```

### Pattern 3: MCP Tools for Entities and Relations
**What:** Two new MCP tools that expose graph data: memory_entities (list known entities) and memory_relations (list relationships for an entity).
**When to use:** GRAPH-04 requirement.
**Example:**
```python
# In docker/memory/server.py

@mcp.tool()
def memory_entities(query: str = "", limit: int = 10) -> str:
    """List known entities from the knowledge graph.

    Args:
        query: Optional filter query (empty = all entities)
        limit: Max results (default 10, max 50)
    """
    limit = max(1, min(limit, 50))
    try:
        results = memory.search(query=query or "entities", user_id=USER_ID, limit=limit)
        relations = results.get("relations", [])
        if not relations:
            return "No entities found in knowledge graph."
        # Extract unique entities from relations
        entities = set()
        for r in relations:
            entities.add(r.get("source", ""))
            entities.add(r.get("destination", ""))
        entities.discard("")
        if not entities:
            return "No entities found."
        return "\n".join(f"- {e}" for e in sorted(entities)[:limit])
    except Exception as e:
        logger.error("memory_entities failed: %s", e)
        return f"Entity lookup failed: {e}"


@mcp.tool()
def memory_relations(entity: str, limit: int = 10) -> str:
    """Show relationships for a specific entity from the knowledge graph.

    Args:
        entity: Entity name to look up relationships for
        limit: Max results (default 10, max 50)
    """
    limit = max(1, min(limit, 50))
    try:
        results = memory.search(query=entity, user_id=USER_ID, limit=limit)
        relations = results.get("relations", [])
        if not relations:
            return f"No relationships found for '{entity}'."
        lines = []
        for r in relations[:limit]:
            src = r.get("source", "?")
            rel = r.get("relationship", "?")
            dst = r.get("destination", "?")
            lines.append(f"- {src} --[{rel}]--> {dst}")
        return "\n".join(lines)
    except Exception as e:
        logger.error("memory_relations failed: %s", e)
        return f"Relationship lookup failed: {e}"
```

### Pattern 4: Graph-Augmented Search Response
**What:** When graph memory is enabled, memory_search should include relation context in its response.
**When to use:** GRAPH-03 requirement, modifying existing memory_search tool.
**Example:**
```python
# Modified memory_search in docker/memory/server.py
@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search memories semantically. Returns relevant memories with scores.
    When graph memory is enabled, also shows entity relationships.

    Args:
        query: Natural language search query
        limit: Max results (default 5, max 20)
    """
    limit = max(1, min(limit, 20))
    try:
        results = memory.search(query=query, user_id=USER_ID, limit=limit)
        # Handle both dict and list response formats
        if isinstance(results, dict):
            items = results.get("results", [])
            relations = results.get("relations", [])
        elif isinstance(results, list):
            items = results
            relations = []
        else:
            items = []
            relations = []

        lines = []
        if not items and not relations:
            return "No matching memories found."

        # Vector results
        for r in items:
            score = r.get("score", "?")
            text = r.get("memory", "")
            mid = r.get("id", "?")
            lines.append(f"[{score}] {text} (id: {mid})")

        # Graph relations (if any)
        if relations:
            lines.append("\n--- Related entities ---")
            for r in relations[:10]:
                src = r.get("source", "?")
                rel = r.get("relationship", "?")
                dst = r.get("destination", "?")
                lines.append(f"  {src} --[{rel}]--> {dst}")

        return "\n".join(lines)
    except Exception as e:
        logger.error("memory_search failed: %s", e)
        return f"Search failed: {e}"
```

### Anti-Patterns to Avoid
- **Running Neo4j as a separate Railway service:** Violates the "zero DevOps" and "in-container" requirement. Costs extra money.
- **Setting Neo4j heap to JVM defaults:** JVM will grab all available RAM. Always set explicit heap limits.
- **Requiring Neo4j for basic memory to work:** Graph memory must be optional. If Neo4j fails to start, vector memory must still work.
- **Exposing Neo4j ports externally:** Only localhost Bolt access needed. Do not publish 7474 or 7687 outside the container.
- **Using `neo4j start` (daemon mode):** In Docker without systemd, use `neo4j console &` for background operation.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Entity extraction from text | Custom NER pipeline | mem0 graph memory (EXTRACT_ENTITIES_TOOL) | mem0 uses LLM-based extraction with configurable prompts |
| Relationship storage | Custom Cypher queries for writes | mem0 graph memory (auto-writes on add()) | mem0 handles graph writes, conflict resolution, dedup |
| Graph search + reranking | Custom Cypher + scoring | mem0 graph.search() with BM25 | mem0 uses entity extraction + BM25Okapi reranking |
| Neo4j health check | Custom TCP socket check | `neo4j status` command or Bolt connection test | Built-in status command, reliable |
| Graph data format | Custom entity/relation schema | mem0's `{source, relationship, destination}` | Standard format, already integrated |

**Key insight:** mem0 handles ALL graph operations internally. You configure it, and `add()` writes to both vector and graph stores. `search()` queries both. The MCP tools just expose the results.

## Common Pitfalls

### Pitfall 1: Neo4j JVM Grabs All Available RAM
**What goes wrong:** Neo4j with default JVM settings allocates 50% of available RAM for heap and another chunk for page cache. On a Railway container with 8GB, this means Neo4j takes 4GB+ leaving nothing for Python, gateway, goosed.
**Why it happens:** Neo4j's heuristic memory allocation is designed for dedicated servers.
**How to avoid:** Always set explicit limits: `NEO4J_server_memory_heap_initial__size=256m`, `NEO4J_server_memory_heap_max__size=512m`, `NEO4J_server_memory_pagecache__size=128m`. Total Neo4j RAM ~700MB.
**Warning signs:** Container OOM kills, gateway becoming unresponsive, Railway logs showing process killed.

### Pitfall 2: Neo4j Startup Race with Gateway
**What goes wrong:** Gateway starts, mem0 tries to connect to Neo4j, Bolt port not ready yet, graph memory initialization fails silently. All subsequent add() calls skip graph writes.
**Why it happens:** Neo4j cold start takes 15-30 seconds. The gateway starts in ~2 seconds.
**How to avoid:** In entrypoint.sh, start Neo4j FIRST and wait for Bolt port readiness (loop checking `neo4j status` or TCP connect to 7687) before starting the gateway. Set 60-second timeout.
**Warning signs:** mem0 logs showing Neo4j connection refused, entities/relations always empty despite active use.

### Pitfall 3: Docker Image Bloat from OpenJDK + Neo4j
**What goes wrong:** Adding Neo4j + OpenJDK 21 adds ~500-700MB to the Docker image. Build times on Railway increase significantly. Deploys slow down.
**Why it happens:** Neo4j is a JVM application. OpenJDK 21 headless alone is ~200MB. Neo4j server is ~300MB. langchain-neo4j pulls langchain-core (~100MB with dependencies).
**How to avoid:** Use `openjdk-21-jre-headless` (not full JDK). Use `--no-install-recommends`. Clean apt cache. Accept the size increase as necessary cost.
**Warning signs:** Docker build taking 10+ minutes on Railway, deploy timeouts.

### Pitfall 4: mem0ai[graph] Dependency Explosion
**What goes wrong:** `pip install "mem0ai[graph]"` pulls langchain-neo4j, langchain-core, langchain-aws, langchain-memgraph, kuzu, rank-bm25. That's a LOT of packages (langchain-core alone is heavyweight). May conflict with existing deps.
**Why it happens:** mem0's graph extras support 4 graph backends (Neo4j, Memgraph, Kuzu, Neptune). It installs all drivers regardless of which you use.
**How to avoid:** Accept the dependency size. Alternatively, install only what's needed: `pip install langchain-neo4j neo4j rank-bm25` and skip the [graph] extra. But this is fragile if mem0 changes internal imports. Safest: use [graph] extra.
**Warning signs:** pip install taking 5+ minutes, dependency resolution conflicts, ImportError on langchain modules.

### Pitfall 5: Additional LLM Cost from Graph Extraction
**What goes wrong:** With graph enabled, each `memory.add()` now makes 2-3 EXTRA LLM calls (entity extraction, relationship extraction, deletion detection) on top of the existing 3-4 vector memory calls. Total: 5-7 LLM calls per add().
**Why it happens:** Graph entity extraction is LLM-based, not rule-based. Each extraction is a separate LLM call.
**How to avoid:** Already routing to cheap model (CFG-03). The cheap model handles extraction fine. Monitor LLM costs. Consider disabling graph for high-volume users.
**Warning signs:** LLM provider bill doubling, mem0.add() latency increasing from ~5s to ~10-15s.

### Pitfall 6: Neo4j Data Not Persisting Across Deploys
**What goes wrong:** Neo4j writes data to `/var/lib/neo4j/data` by default. This is on the container filesystem, not the /data volume. Redeploy wipes all graph data.
**Why it happens:** Neo4j default data directory is within its install path, not on the persistent volume.
**How to avoid:** Set `server.directories.data=/data/neo4j` via the `NEO4J_server_directories_data` env var. Create `/data/neo4j` in entrypoint.sh. Ensure neo4j user owns it.
**Warning signs:** After redeploy, memory_entities returns empty, memory_relations returns empty, but vector memories still work.

### Pitfall 7: Neo4j Auth Mismatch
**What goes wrong:** Neo4j requires initial password setup on first boot. If `NEO4J_AUTH=none` is not set, the default requires `neo4j/neo4j` and forces a password change on first connection. mem0's connection with empty password fails.
**Why it happens:** Neo4j security defaults expect interactive password setup.
**How to avoid:** Set `NEO4J_AUTH=none` for in-container use (localhost only, no external access). Or set `NEO4J_AUTH=neo4j/yourpassword` and match in mem0 config. `none` is simpler since Neo4j is only accessible inside the container.
**Warning signs:** mem0 graph init error "authentication failed", Neo4j logs showing auth errors.

## Code Examples

### Dockerfile Neo4j Installation
```dockerfile
# Source: Neo4j official Debian installation docs
# Add AFTER existing apt-get and BEFORE pip install
RUN wget -O - https://debian.neo4j.com/neotechnology.gpg.key | \
      gpg --dearmor -o /etc/apt/keyrings/neotechnology.gpg && \
    echo 'deb [signed-by=/etc/apt/keyrings/neotechnology.gpg] https://debian.neo4j.com stable latest' \
      > /etc/apt/sources.list.d/neo4j.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends neo4j && \
    rm -rf /var/lib/apt/lists/*
```

### mem0 Config with Graph Store
```python
# Source: mem0 graph memory docs + graph_memory.py source
# In docker/mem0_config.py

def build_mem0_config():
    """Build mem0 config dict from setup.json and environment variables."""
    setup = _load_setup()
    provider = (setup.get("provider_type", "anthropic") if setup else "anthropic")
    mem0_provider = PROVIDER_MAP.get(provider, "openai")
    cheap_model = CHEAP_MODELS.get(provider, "gpt-4.1-nano")

    llm_config = {
        "model": cheap_model,
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    if provider == "openrouter":
        llm_config["api_key"] = os.environ.get("OPENROUTER_API_KEY", "")

    config = {
        "vector_store": {
            "provider": "chromadb",
            "config": {
                "collection_name": "mem0_memories",
                "path": os.environ.get("MEM0_CHROMA_PATH", "/data/knowledge/chroma"),
            }
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            }
        },
        "llm": {
            "provider": mem0_provider,
            "config": llm_config,
        },
        "version": "v1.1",
    }

    # Graph store (Neo4j) - enabled when NEO4J_ENABLED env var is set
    if os.environ.get("NEO4J_ENABLED", "").lower() in ("true", "1", "yes"):
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": os.environ.get("NEO4J_URL", "bolt://localhost:7687"),
                "username": os.environ.get("NEO4J_USERNAME", "neo4j"),
                "password": os.environ.get("NEO4J_PASSWORD", ""),
            }
        }

    return config
```

### mem0 Search Return Format (graph enabled)
```python
# Source: mem0/memory/main.py search() method
# When graph_store is configured:
{
    "results": [
        {"id": "mem_123", "memory": "Haseeb works on GooseClaw", "score": 0.92},
        {"id": "mem_456", "memory": "GooseClaw is deployed on Railway", "score": 0.85},
    ],
    "relations": [
        {"source": "Haseeb", "relationship": "WORKS_ON", "destination": "GooseClaw"},
        {"source": "GooseClaw", "relationship": "DEPLOYED_ON", "destination": "Railway"},
    ]
}

# When graph_store is NOT configured (current behavior):
{
    "results": [
        {"id": "mem_123", "memory": "Haseeb works on GooseClaw", "score": 0.92},
    ]
}
```

### Neo4j Environment Variables (Docker convention)
```bash
# Source: Neo4j Docker configuration docs
# Naming: NEO4J_ prefix, dots->underscores, literal underscores->double underscores
# Example: server.memory.heap.max_size -> NEO4J_server_memory_heap_max__size

export NEO4J_AUTH=none                                    # Disable auth (localhost only)
export NEO4J_server_memory_heap_initial__size=256m        # JVM heap start
export NEO4J_server_memory_heap_max__size=512m            # JVM heap max
export NEO4J_server_memory_pagecache__size=128m           # Page cache
export NEO4J_server_directories_data=/data/neo4j          # Persist on volume
export NEO4J_server_analytics_enabled=false               # No telemetry
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Vector-only memory search | Graph-augmented search (vector + relations) | mem0 graph feature (2024) | Search returns entity relationships alongside text matches |
| Manual entity extraction | LLM-based entity extraction via mem0 | mem0 graph feature | Automatic entity/relationship extraction on add() |
| Neo4j 5.x with Java 17 | Neo4j 2026.x with Java 21 | Neo4j 2025.10+ | Requires OpenJDK 21, improved performance |
| dbms.memory.* config names | server.memory.* config names | Neo4j 5.0+ | Old `dbms.` prefix deprecated, use `server.` prefix |
| Neo4j separate service | Neo4j in-container | Project decision | Zero extra Railway cost, simpler deployment |

**Deprecated/outdated:**
- `dbms.memory.heap.max_size`: Use `server.memory.heap.max_size` (renamed in Neo4j 5.0+)
- `dbms.connector.bolt.listen_address`: Use `server.bolt.listen_address`
- mem0 issue #3711 (structuredLlm hardcoded OpenAI): Node.js only, does NOT affect Python implementation

## Open Questions

1. **Docker image size impact of Neo4j + OpenJDK 21**
   - What we know: OpenJDK 21 headless ~200MB, Neo4j server ~300MB, langchain-neo4j + langchain-core + deps ~100-200MB. Total ~500-700MB increase.
   - What's unclear: Exact final image size. Whether Railway's build cache handles this well.
   - Recommendation: Accept the size increase. Test Docker build on Railway. If build times are problematic, consider pre-building a base image.

2. **langchain-core dependency conflicts**
   - What we know: mem0ai[graph] pulls langchain-neo4j which pulls langchain-core. langchain-core has its own pydantic requirements.
   - What's unclear: Whether langchain-core conflicts with existing deps (pydantic version, etc.).
   - Recommendation: First task should test `pip install "mem0ai[graph]"` in the Docker container to verify no conflicts.

3. **Neo4j data directory ownership when running as non-root (gooseclaw user)**
   - What we know: Entrypoint runs as root initially, then drops to gooseclaw user. Neo4j expects its data dir owned by neo4j:neo4j user.
   - What's unclear: Whether Neo4j started as root writes to /data/neo4j successfully, and whether the gooseclaw user's mem0 process can connect.
   - Recommendation: Start Neo4j as neo4j user (it creates its own user during apt install). The gooseclaw user connects via Bolt (TCP), not filesystem. Keep Neo4j running as neo4j user.

4. **Graceful degradation when Neo4j is unavailable**
   - What we know: If graph_store config is absent, mem0 returns `{"results": [...]}` without relations. If graph_store is configured but Neo4j is down, behavior is unclear.
   - What's unclear: Does mem0 throw on add() if Neo4j is unreachable? Does search() still return vector results?
   - Recommendation: Wrap graph config behind `NEO4J_ENABLED` env var. Only set it when Neo4j successfully starts. If Neo4j fails to start, leave graph disabled and log a warning.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3+ |
| Config file | docker/pytest.ini |
| Quick run command | `cd docker && python -m pytest test_mem0_config.py test_memory_server.py -x --timeout=30` |
| Full suite command | `cd docker && python -m pytest tests/ test_mem0_config.py test_memory_server.py -v --timeout=30` |
| Estimated runtime | ~15 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| GRAPH-01 | Neo4j installed, started by entrypoint, data on /data | unit (source inspection) | `cd docker && python -m pytest tests/test_entrypoint.py -x -k neo4j` | No, Wave 0 gap |
| GRAPH-02 | mem0 config has graph_store when NEO4J_ENABLED=true | unit | `cd docker && python -m pytest test_mem0_config.py -x -k graph` | No, Wave 0 gap |
| GRAPH-03 | memory_search includes relations when graph enabled | unit (mock) | `cd docker && python -m pytest test_memory_server.py -x -k "search and graph"` | No, Wave 0 gap |
| GRAPH-04 | memory_entities and memory_relations tools exist and work | unit (mock) | `cd docker && python -m pytest test_memory_server.py -x -k "entities or relations"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `cd docker && python -m pytest test_mem0_config.py test_memory_server.py -x --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~10 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/test_mem0_config.py` additions -- new tests for graph_store config (GRAPH-02): test config has graph_store when NEO4J_ENABLED=true, test config lacks graph_store when NEO4J_ENABLED absent/false
- [ ] `docker/test_memory_server.py` additions -- new tests for GRAPH-03, GRAPH-04: test memory_search includes relations, test memory_entities tool, test memory_relations tool
- [ ] `docker/tests/test_entrypoint.py` additions -- test for neo4j start block in entrypoint source (GRAPH-01)

**Testing approach:** Extend existing test files. Mock mem0.Memory.search() to return `{"results": [...], "relations": [...]}` format. Test that MCP tools correctly format graph data. Test config builder conditionally includes graph_store based on env var.

## Sources

### Primary (HIGH confidence)
- [mem0 Graph Memory docs](https://docs.mem0.ai/open-source/features/graph-memory) -- config format, graph_store keys, installation
- [mem0/memory/main.py source](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) -- search() return structure with relations key
- [mem0/memory/graph_memory.py source](https://github.com/mem0ai/mem0/blob/main/mem0/memory/graph_memory.py) -- MemoryGraph.search() returns `{source, relationship, destination}` dicts
- [mem0 pyproject.toml](https://github.com/mem0ai/mem0/blob/main/pyproject.toml) -- [graph] extras: langchain-neo4j>=0.4.0, neo4j>=5.23.1, rank-bm25>=0.2.2, kuzu>=0.11.0, langchain-aws>=0.2.23, langchain-memgraph>=0.1.0
- [Neo4j Debian installation docs](https://neo4j.com/docs/operations-manual/current/installation/linux/debian/) -- apt-get procedure, Java 21 requirement
- [Neo4j Docker configuration](https://neo4j.com/docs/operations-manual/current/docker/configuration/) -- env var naming convention, heap settings
- [Neo4j file locations](https://neo4j.com/docs/operations-manual/current/configuration/file-locations/) -- data directory config
- [Neo4j memory configuration](https://neo4j.com/docs/operations-manual/current/performance/memory-configuration/) -- heap, page cache settings

### Secondary (MEDIUM confidence)
- [DeepWiki mem0 Graph Memory Overview](https://deepwiki.com/mem0ai/mem0/4.1-graph-memory-overview) -- LLM call count (2-3 extra per add, 1 extra per search), ThreadPoolExecutor parallel execution
- [DeepWiki mem0 Graph Memory](https://deepwiki.com/mem0ai/mem0/4-graph-memory) -- search result structure, BM25 reranking
- [GitHub Issue #3711](https://github.com/mem0ai/mem0/issues/3711) -- structuredLlm hardcoded to OpenAI (Node.js only, NOT Python)

### Tertiary (LOW confidence)
- Neo4j in-container RAM usage with 256m/512m heap -- needs validation in actual Railway deployment. Estimated ~700MB total.
- langchain-core dependency size and conflict risk -- needs validation with `pip install "mem0ai[graph]"` in Docker container
- Neo4j cold start time (15-30 seconds) -- needs validation in Railway's containerized environment

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- Neo4j apt package verified in official docs, mem0 graph extras verified in pyproject.toml
- Architecture: MEDIUM-HIGH -- in-container pattern is non-standard (most run Neo4j separately), but mechanics are well-understood
- Pitfalls: HIGH -- JVM heap, startup race, data persistence, auth all documented in Neo4j docs + project pitfalls research
- Graph memory integration: HIGH -- mem0 Python graph_memory.py verified provider-agnostic, search return format verified in source

**Research date:** 2026-03-20
**Valid until:** 2026-04-05 (15 days -- Neo4j releases frequently, mem0 graph feature still maturing)

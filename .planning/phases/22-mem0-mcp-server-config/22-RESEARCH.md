# Phase 22: mem0 MCP Server + Config - Research

**Researched:** 2026-03-20
**Domain:** mem0 memory layer integration (MCP server + shared config) with ChromaDB backend
**Confidence:** HIGH

## Summary

Phase 22 builds a standalone mem0 MCP extension and shared config module. The mem0 MCP server exposes 6 memory tools (add, search, delete, list, history, get) to goose via stdio transport, following the exact same pattern as the existing `docker/knowledge/server.py`. A shared config module (`docker/mem0_config.py`) builds the mem0 config dict from setup.json credentials and environment variables, mapping the user's existing LLM provider to mem0's format.

The critical architectural revision from project-level research: use ChromaDB as mem0's vector store backend with the HuggingFace `all-MiniLM-L6-v2` local embedder. This means zero new Railway services, zero new API keys for embeddings, and zero external embedding API calls. The only LLM calls are for mem0's fact extraction pipeline, which reuses the user's existing provider but routes to a cheap model (claude-haiku, gpt-4.1-nano). mem0's core dependency on qdrant-client and protobuf 5.x is a potential conflict with the existing chromadb==1.5.5 that MUST be tested early.

**Primary recommendation:** Build the MCP server first with ChromaDB persistent backend and HuggingFace local embeddings. Test `pip install mem0ai` alongside chromadb==1.5.5 in the Docker container before writing any application code. If dependency conflict exists, resolve it before proceeding.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MEM-01 | Bot can store memories via `memory_add` MCP tool | mem0 `Memory.add()` API, FastMCP tool pattern from knowledge/server.py |
| MEM-02 | Bot can search memories semantically via `memory_search` MCP tool | mem0 `Memory.search()` API with ChromaDB vector backend |
| MEM-03 | Bot can delete specific memories via `memory_delete` MCP tool | mem0 `Memory.delete()` API |
| MEM-04 | Bot can list all memories via `memory_list` MCP tool | mem0 `Memory.get_all()` API |
| MEM-05 | Bot can view memory evolution via `memory_history` MCP tool | mem0 `Memory.history()` API (SQLite-backed audit trail) |
| MEM-06 | mem0 MCP server runs as stdio extension registered in config.yaml | Entrypoint extension registration pattern (lines 423-499) |
| CFG-01 | mem0 uses ChromaDB as vector store (zero new infra) | mem0 ChromaDB provider: `"chromadb"`, persistent local path, HuggingFace embedder |
| CFG-02 | mem0 LLM extraction reuses user's existing provider from vault/setup.json | Provider mapping from setup.json `provider_type` + credentials to mem0 config dict |
| CFG-03 | mem0 extraction routes to a cheap model automatically (not user's main model) | Cheap model defaults per provider in config module |
| CFG-04 | Shared config module builds mem0 config from environment variables | `docker/mem0_config.py` shared between MCP server and (future) gateway.py |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mem0ai | 1.0.6 | Memory layer with extraction, contradiction resolution, dedup | Only library that handles extract + compare + ADD/UPDATE/DELETE in one call. Latest stable release 2026-03-17 |
| chromadb | 1.5.5 (existing) | Vector store backend for mem0 | Already installed, zero new infra, persistent at /data/knowledge/chroma |
| mcp[cli] | 1.26.0 (existing) | FastMCP server framework | Already used by knowledge MCP extension, same pattern |
| sentence-transformers | >=5.0.0 | Local HuggingFace embedder (all-MiniLM-L6-v2) | Zero-cost local embeddings, 384 dims, bundled by chromadb already (ONNX), mem0 needs the pip package |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| qdrant-client | >=1.9.1 | Core mem0 dependency (auto-installed) | NOT used directly, mem0 core dep we cannot avoid |
| pydantic | >=2.7.3 | Core mem0 dependency (auto-installed) | NOT used directly |
| openai | >=1.90.0 | Core mem0 dependency (auto-installed) | NOT used directly unless user's provider is OpenAI |
| sqlalchemy | >=2.0.31 | Core mem0 dependency for history tracking | NOT used directly, mem0 uses SQLite for audit trail |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| HuggingFace local embedder | OpenAI text-embedding-3-small | Requires OpenAI API key even for Anthropic-only users. Higher quality but costs money per call. HuggingFace is zero-cost. |
| ChromaDB backend | pgvector (Railway PostgreSQL) | Requires new Railway service ($5-15/mo), new env vars, startup health checks. ChromaDB is already installed and working. |
| `pip install mem0ai` (core) | `pip install "mem0ai[extras]"` | [extras] includes sentence-transformers. Core does not. Must install sentence-transformers separately or use [extras] |

**Installation:**
```bash
# Add to docker/requirements.txt
mem0ai==1.0.6
sentence-transformers>=5.0.0
```

**Critical dependency test FIRST:**
```bash
pip install mem0ai==1.0.6 chromadb==1.5.5 sentence-transformers
# Verify both import without protobuf/qdrant conflicts
python3 -c "import mem0; import chromadb; print('OK')"
```

## Architecture Patterns

### Recommended Project Structure
```
docker/
+-- mem0_config.py           # NEW: shared config builder (reads setup.json + env)
+-- memory/
|   +-- __init__.py          # empty
|   +-- server.py            # NEW: FastMCP stdio server wrapping mem0
+-- knowledge/
|   +-- server.py            # UNCHANGED: system docs only, ChromaDB
+-- gateway.py               # UNCHANGED in this phase (Phase 23 modifies)
+-- requirements.txt         # MODIFIED: add mem0ai, sentence-transformers
+-- entrypoint.sh            # MODIFIED: register mem0-memory extension
```

### Pattern 1: FastMCP stdio MCP Server (existing pattern)
**What:** Python script using FastMCP with stdio transport, registered as goosed extension.
**When to use:** Every MCP extension in GooseClaw follows this exact pattern.
**Example:**
```python
# Source: docker/knowledge/server.py (existing, verified in codebase)
import os, sys, logging, json
from mcp.server.fastmcp import FastMCP
from mem0 import Memory

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("mem0-memory")

# Import shared config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mem0_config import build_mem0_config

config = build_mem0_config()
memory = Memory.from_config(config)

USER_ID = os.environ.get("MEM0_USER_ID", "default")

mcp = FastMCP("mem0-memory")

@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search memories semantically."""
    results = memory.search(query=query, user_id=USER_ID, limit=limit)
    # format results...

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Pattern 2: Shared Config Module
**What:** Single Python module that builds mem0 config dict from setup.json credentials. Used by both MCP server and (future) gateway.py memory writer.
**When to use:** When two processes need the same mem0 config.
**Example:**
```python
# docker/mem0_config.py
import os, json

def build_mem0_config():
    """Build mem0 config from setup.json and environment."""
    setup = _load_setup()
    provider = setup.get("provider_type", "anthropic") if setup else "anthropic"

    return {
        "vector_store": {
            "provider": "chromadb",
            "config": {
                "collection_name": "mem0_memories",
                "path": "/data/knowledge/chroma",
            }
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            }
        },
        "llm": {
            "provider": _map_provider(provider),
            "config": {
                "model": _cheap_model(provider),
                "temperature": 0.1,
                "max_tokens": 2000,
            }
        },
        "version": "v1.1",
    }
```

### Pattern 3: Extension Registration in entrypoint.sh
**What:** YAML block appended to config.yaml during first boot.
**When to use:** Registering new MCP extensions with goosed.
**Example:**
```yaml
# follows exact pattern of knowledge extension (entrypoint.sh line 485-498)
mem0-memory:
  enabled: true
  type: stdio
  name: mem0-memory
  description: Long-term memory with semantic search and contradiction resolution
  cmd: python3
  args:
    - /app/docker/memory/server.py
  envs:
    MEM0_USER_ID: default
    MEM0_TELEMETRY: "false"
  env_keys: []
  timeout: 300
  bundled: null
  available_tools: []
```

### Anti-Patterns to Avoid
- **Importing mem0 in gateway.py (this phase):** Phase 22 only creates the MCP server. Gateway integration is Phase 23. Do not touch gateway.py.
- **Using mem0 Platform API:** Requires Mem0 cloud API key. We use open-source mode only.
- **Using OpenAI embeddings by default:** Requires an extra API key. Use HuggingFace local embedder instead.
- **Creating a separate ChromaDB collection path:** Use the SAME ChromaDB path `/data/knowledge/chroma` but a different collection name (`mem0_memories` vs `system`/`runtime`).
- **Hardcoding provider credentials:** Config module must read from setup.json dynamically.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Fact extraction from conversations | Custom LLM prompt + JSON parsing | `mem0.Memory.add(messages)` | mem0 handles extraction, dedup, contradiction resolution internally |
| Vector similarity search | Manual ChromaDB query + scoring | `mem0.Memory.search(query)` | mem0 handles embedding, search, scoring, optional reranking |
| Memory deduplication | Custom cosine similarity checks | mem0 internal consolidation | mem0 compares new facts against existing at 0.85 threshold |
| Contradiction resolution | Custom LLM prompt for ADD/UPDATE/DELETE | mem0 internal pipeline | mem0's LLM-as-judge pipeline handles this per-fact |
| Memory audit trail | Custom logging + file storage | mem0 SQLite history via `memory.history()` | Built-in, tracks all ADD/UPDATE/DELETE operations |
| Provider credential mapping | Separate config per provider | Shared `mem0_config.py` with provider map | One module, tested once, shared everywhere |

**Key insight:** mem0's entire value is that you don't build memory infrastructure. You call `add()` and `search()`. The ~150 lines of manual extraction code in gateway.py (Phase 23) get replaced by ~5 lines.

## Common Pitfalls

### Pitfall 1: Dependency Conflict (protobuf / qdrant-client vs chromadb)
**What goes wrong:** `mem0ai` core depends on `qdrant-client>=1.9.1` and `protobuf>=5.29.6`. Historically, chromadb's opentelemetry-proto required `protobuf<5.0`. Installing both can fail.
**Why it happens:** mem0 bundles qdrant-client as a core dependency even when using ChromaDB backend.
**How to avoid:** Test `pip install mem0ai==1.0.6 chromadb==1.5.5` in a fresh Docker container BEFORE writing any code. Recent chromadb versions (1.5.x) may have relaxed protobuf constraints (local testing shows protobuf 6.33.5 working with chromadb 1.5.1). If conflict exists, pin specific protobuf version.
**Warning signs:** `pip install` errors about incompatible versions, runtime ImportError on protobuf.

### Pitfall 2: mem0 Defaults to OpenAI Embeddings
**What goes wrong:** If you don't configure an embedder, mem0 defaults to OpenAI's text-embedding-3-small, which requires OPENAI_API_KEY. Users with only Anthropic keys get errors.
**Why it happens:** mem0's default config uses OpenAI for everything (LLM and embedder).
**How to avoid:** Always explicitly configure `"embedder": {"provider": "huggingface", ...}` in the config. Never rely on mem0 defaults.
**Warning signs:** Runtime error about missing OPENAI_API_KEY.

### Pitfall 3: ChromaDB Provider Name is "chromadb" Not "chroma"
**What goes wrong:** Using `"provider": "chroma"` raises `ValueError: Unsupported VectorStore provider: chroma`.
**Why it happens:** Documentation inconsistency (fixed in issue #1681). The code expects "chromadb".
**How to avoid:** Always use `"provider": "chromadb"` in config.
**Warning signs:** ValueError on Memory.from_config().

### Pitfall 4: mem0 Telemetry Sends Data to PostHog
**What goes wrong:** Every add() and search() call sends anonymous usage data to PostHog servers. GooseClaw is privacy-focused.
**Why it happens:** mem0 enables telemetry by default.
**How to avoid:** Set `MEM0_TELEMETRY=false` environment variable in entrypoint.sh AND in the MCP extension envs block. Set it before any mem0 import.
**Warning signs:** Outbound network connections to app.posthog.com.

### Pitfall 5: mem0 add() Blocks for 2-20 Seconds
**What goes wrong:** `add()` triggers LLM extraction (3-6 API calls internally). MCP tool call hangs during this time.
**Why it happens:** mem0's pipeline: extract facts -> embed -> search existing -> LLM classify ADD/UPDATE/DELETE -> upsert.
**How to avoid:** For the MCP server, this is acceptable (goose waits for tool results). For Phase 23 gateway integration, must be async/threaded with timeout. In Phase 22, just ensure the MCP tool doesn't timeout (set tool timeout to 60s).
**Warning signs:** MCP tool calls timing out, goose reporting tool errors.

### Pitfall 6: LLM Token Burn on Extraction
**What goes wrong:** mem0 makes 3-6 LLM calls per add() operation. If routed to the user's expensive main model (claude-opus, gpt-4.1), costs spiral.
**Why it happens:** Not configuring a separate cheap extraction model.
**How to avoid:** CFG-03 requires routing to a cheap model. Config module must map each provider to its cheapest viable model (anthropic -> claude-haiku, openai -> gpt-4.1-nano, etc.).
**Warning signs:** LLM provider bill increases without proportional conversation volume.

### Pitfall 7: sentence-transformers Model Download on First Boot
**What goes wrong:** First call to mem0 with HuggingFace embedder triggers a ~90MB model download for all-MiniLM-L6-v2. If the container has no internet or download is slow, the first memory operation fails or hangs.
**Why it happens:** sentence-transformers downloads models from HuggingFace Hub on first use.
**How to avoid:** Add a warmup step to the Dockerfile (similar to the existing ChromaDB ONNX warmup on line 63). Pre-download the model during Docker build.
**Warning signs:** First memory_search call takes 30+ seconds, timeout errors on first use.

## Code Examples

### mem0 Config Builder (shared module)
```python
# docker/mem0_config.py
# Source: Verified against mem0 docs + codebase setup.json format
import os
import json

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/data/config")
SETUP_FILE = os.path.join(CONFIG_DIR, "setup.json")

# Maps setup.json provider_type -> mem0 LLM provider name
PROVIDER_MAP = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "groq": "groq",
    "openrouter": "litellm",
    "ollama": "ollama",
    "deepseek": "deepseek",
    "together": "together",
    "azure-openai": "azure_openai",
    "litellm": "litellm",
}

# Cheap extraction models per provider (CFG-03)
CHEAP_MODELS = {
    "anthropic": "claude-haiku-4-20250414",
    "openai": "gpt-4.1-nano",
    "google": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "llama3.2",
    "deepseek": "deepseek-chat",
    "together": "meta-llama/Llama-3-8b-chat-hf",
    "litellm": "gpt-4.1-nano",
    "openrouter": "anthropic/claude-3-haiku-20240307",
    "azure-openai": "gpt-4o-mini",
}

# Maps setup.json provider_type -> env var name for API key
PROVIDER_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "ollama": None,  # no key needed
}


def _load_setup():
    """Load setup.json (same logic as gateway.py load_setup)."""
    for path in (SETUP_FILE, SETUP_FILE + ".bak"):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def build_mem0_config():
    """Build mem0 config dict from setup.json and environment variables."""
    setup = _load_setup()
    provider = (setup.get("provider_type", "anthropic") if setup else "anthropic")
    mem0_provider = PROVIDER_MAP.get(provider, "openai")
    cheap_model = CHEAP_MODELS.get(provider, "gpt-4.1-nano")

    # Build LLM config
    llm_config = {
        "model": cheap_model,
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    # For litellm provider (openrouter), need api_base
    if provider == "openrouter":
        llm_config["api_key"] = os.environ.get("OPENROUTER_API_KEY", "")

    config = {
        "vector_store": {
            "provider": "chromadb",
            "config": {
                "collection_name": "mem0_memories",
                "path": os.environ.get(
                    "MEM0_CHROMA_PATH",
                    "/data/knowledge/chroma"
                ),
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

    return config
```

### mem0 MCP Server (6 tools)
```python
# docker/memory/server.py
# Source: Follows knowledge/server.py pattern exactly
import os, sys, json, logging
from mcp.server.fastmcp import FastMCP
from mem0 import Memory

# Disable telemetry BEFORE any mem0 operations
os.environ["MEM0_TELEMETRY"] = "false"

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("mem0-memory")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mem0_config import build_mem0_config

config = build_mem0_config()
memory = Memory.from_config(config)
USER_ID = os.environ.get("MEM0_USER_ID", "default")

mcp = FastMCP("mem0-memory")


@mcp.tool()
def memory_add(content: str) -> str:
    """Store a memory. mem0 extracts facts, handles dedup and contradictions.

    Args:
        content: Natural language content to remember
    """
    try:
        result = memory.add(
            messages=[{"role": "user", "content": content}],
            user_id=USER_ID,
        )
        return json.dumps(result, default=str)
    except Exception as e:
        logger.error("memory_add failed: %s", e)
        return f"Failed to store memory: {e}"


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search memories semantically. Returns relevant memories with scores.

    Args:
        query: Natural language search query
        limit: Max results (default 5, max 20)
    """
    limit = max(1, min(limit, 20))
    try:
        results = memory.search(query=query, user_id=USER_ID, limit=limit)
        if not results or not results.get("results"):
            return "No matching memories found."
        lines = []
        for r in results["results"]:
            score = r.get("score", "?")
            text = r.get("memory", "")
            mid = r.get("id", "?")
            lines.append(f"[{score}] {text} (id: {mid})")
        return "\n".join(lines)
    except Exception as e:
        logger.error("memory_search failed: %s", e)
        return f"Search failed: {e}"


@mcp.tool()
def memory_delete(memory_id: str) -> str:
    """Delete a specific memory permanently.

    Args:
        memory_id: The memory ID to delete
    """
    try:
        memory.delete(memory_id=memory_id)
        return f"Deleted memory: {memory_id}"
    except Exception as e:
        logger.error("memory_delete failed: %s", e)
        return f"Failed to delete: {e}"


@mcp.tool()
def memory_list(limit: int = 10) -> str:
    """List all stored memories, newest first.

    Args:
        limit: Max results (default 10, max 50)
    """
    limit = max(1, min(limit, 50))
    try:
        results = memory.get_all(user_id=USER_ID)
        if not results or not results.get("results"):
            return "No memories stored yet."
        lines = []
        for r in results["results"][:limit]:
            text = r.get("memory", "")
            mid = r.get("id", "?")
            lines.append(f"- {text} (id: {mid})")
        return "\n".join(lines)
    except Exception as e:
        logger.error("memory_list failed: %s", e)
        return f"Failed to list: {e}"


@mcp.tool()
def memory_history(memory_id: str) -> str:
    """Show change history for a specific memory (adds, updates, deletes).

    Args:
        memory_id: The memory ID to get history for
    """
    try:
        changes = memory.history(memory_id=memory_id)
        if not changes:
            return "No history found for this memory."
        lines = []
        for c in changes:
            lines.append(f"[{c.get('event', '?')}] {c.get('old_memory', '')} -> {c.get('new_memory', '')} ({c.get('created_at', '?')})")
        return "\n".join(lines)
    except Exception as e:
        logger.error("memory_history failed: %s", e)
        return f"History lookup failed: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Extension Registration Block
```yaml
# Added to entrypoint.sh default extensions block
mem0-memory:
  enabled: true
  type: stdio
  name: mem0-memory
  description: Long-term memory with semantic search and contradiction resolution
  cmd: python3
  args:
    - /app/docker/memory/server.py
  envs:
    MEM0_USER_ID: default
    MEM0_TELEMETRY: "false"
  env_keys: []
  timeout: 300
  bundled: null
  available_tools: []
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual LLM extraction + ChromaDB upsert | mem0.add() handles everything | mem0 v1.0.0 (2025) | ~150 lines of extraction code become ~5 lines |
| No contradiction resolution (append-only) | mem0 ADD/UPDATE/DELETE pipeline | mem0 core feature | Old facts get updated, not accumulated |
| No dedup | mem0 consolidation at 0.85 similarity | mem0 core feature | No more duplicate memories |
| pgvector as vector store (initial plan) | ChromaDB backend (revised) | 2026-03-19 revision | Zero new infrastructure, zero new costs |
| OpenAI embeddings (initial plan) | HuggingFace local embeddings (revised) | 2026-03-19 revision | Zero embedding API costs |

**Deprecated/outdated:**
- mem0 provider name `"chroma"`: Use `"chromadb"` (fixed in issue #1681)
- mem0 versions < 1.0.0: Breaking API changes in v1.0.0. The `results` field wrapping changed.
- `psycopg2-binary` + `pgvector` dependencies: Not needed with ChromaDB backend.

## Open Questions

1. **Dependency compatibility (protobuf/qdrant-client + chromadb)**
   - What we know: mem0ai core depends on qdrant-client + protobuf>=5.29.6. chromadb 1.5.5 historically had protobuf<5 constraint via opentelemetry. Local testing shows protobuf 6.33.5 works with chromadb 1.5.1.
   - What's unclear: Whether chromadb 1.5.5 specifically accepts protobuf 5.x+ in the Docker container (ubuntu:22.04, Python 3.10).
   - Recommendation: First task in the plan MUST be a dependency compatibility test in the Docker build. If it fails, options: (a) upgrade chromadb, (b) pin protobuf, (c) use separate virtual environments.

2. **mem0 response format for search/get_all**
   - What we know: DeepWiki docs show `results` wrapping. Platform API differs from OSS API format.
   - What's unclear: Exact structure of `memory.search()` return value in OSS v1.0.6.
   - Recommendation: Test the actual response format and adjust MCP tool formatters accordingly.

3. **sentence-transformers model size in Docker image**
   - What we know: all-MiniLM-L6-v2 is ~90MB. sentence-transformers pulls torch (2GB+) as dependency.
   - What's unclear: Whether torch bloat is acceptable in the Docker image.
   - Recommendation: Check if `pip install sentence-transformers` pulls full torch. If so, consider using ONNX runtime (already in container from chromadb) or a lighter embedder path. Alternative: use chromadb's own embedding function via a custom embedder wrapper.

4. **ChromaDB shared path with separate collections**
   - What we know: Both knowledge MCP (system + runtime collections) and mem0 MCP will use `/data/knowledge/chroma`.
   - What's unclear: Whether two separate processes can safely access the same ChromaDB PersistentClient path concurrently.
   - Recommendation: Test concurrent access. If issues, use a separate ChromaDB path for mem0 (e.g., `/data/memory/chroma`).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3+ |
| Config file | docker/pytest.ini |
| Quick run command | `cd docker && python -m pytest tests/ -x --timeout=30` |
| Full suite command | `cd docker && python -m pytest tests/ -v --timeout=30` |
| Estimated runtime | ~15 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MEM-01 | memory_add stores memory via mem0 | unit | `cd docker && python -m pytest test_memory_server.py::TestMemoryAdd -x` | No, Wave 0 gap |
| MEM-02 | memory_search returns semantic results | unit | `cd docker && python -m pytest test_memory_server.py::TestMemorySearch -x` | No, Wave 0 gap |
| MEM-03 | memory_delete removes memory by ID | unit | `cd docker && python -m pytest test_memory_server.py::TestMemoryDelete -x` | No, Wave 0 gap |
| MEM-04 | memory_list returns all memories | unit | `cd docker && python -m pytest test_memory_server.py::TestMemoryList -x` | No, Wave 0 gap |
| MEM-05 | memory_history returns audit trail | unit | `cd docker && python -m pytest test_memory_server.py::TestMemoryHistory -x` | No, Wave 0 gap |
| MEM-06 | Extension registered in config.yaml | unit (source inspection) | `cd docker && python -m pytest tests/test_entrypoint.py -x -k mem0` | No, Wave 0 gap |
| CFG-01 | Config uses chromadb provider | unit | `cd docker && python -m pytest test_mem0_config.py::TestConfigChromaDB -x` | No, Wave 0 gap |
| CFG-02 | Config reads provider from setup.json | unit | `cd docker && python -m pytest test_mem0_config.py::TestConfigProvider -x` | No, Wave 0 gap |
| CFG-03 | Config routes to cheap model | unit | `cd docker && python -m pytest test_mem0_config.py::TestConfigCheapModel -x` | No, Wave 0 gap |
| CFG-04 | build_mem0_config returns valid dict | unit | `cd docker && python -m pytest test_mem0_config.py::TestConfigBuild -x` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `cd docker && python -m pytest test_mem0_config.py test_memory_server.py -x --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~10 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/test_mem0_config.py` -- covers CFG-01, CFG-02, CFG-03, CFG-04 (unit tests for config builder)
- [ ] `docker/test_memory_server.py` -- covers MEM-01 through MEM-05 (unit tests calling tool functions directly, mocking mem0.Memory)
- [ ] `docker/tests/test_entrypoint.py` additions -- covers MEM-06 (source inspection for mem0-memory extension block)

**Testing approach:** Follow the pattern in `docker/test_server.py` (knowledge MCP tests). Mock the mem0 Memory object, monkey-patch the module-level `memory` variable in server.py, call tool functions directly. No actual LLM or vector store needed for unit tests.

## Sources

### Primary (HIGH confidence)
- [mem0 ChromaDB config docs](https://docs.mem0.ai/components/vectordbs/dbs/chroma) -- provider name, config options
- [mem0 ChromaDB config source](https://github.com/mem0ai/mem0/blob/main/mem0/configs/vector_stores/chroma.py) -- ChromaDbConfig class fields
- [mem0 provider name fix #1681](https://github.com/mem0ai/mem0/issues/1681) -- "chromadb" not "chroma"
- [mem0 HuggingFace embedder docs](https://docs.mem0.ai/components/embedders/models/huggingface) -- config format, model options
- [mem0 Anthropic LLM docs](https://docs.mem0.ai/components/llms/models/anthropic) -- provider config format
- [mem0ai PyPI](https://pypi.org/project/mem0ai/) -- version 1.0.6, 2026-03-17 (HIGH confidence)
- [mem0 pyproject.toml](https://github.com/mem0ai/mem0/blob/main/pyproject.toml) -- full dependency tree (HIGH confidence)
- Codebase: docker/knowledge/server.py -- existing MCP pattern (verified)
- Codebase: docker/entrypoint.sh lines 423-499 -- extension registration (verified)
- Codebase: docker/gateway.py lines 1410-1460 -- provider registry (verified)
- Codebase: docker/gateway.py lines 6709-7094 -- memory writer (verified)
- Codebase: docker/test_server.py -- test pattern for MCP tools (verified)

### Secondary (MEDIUM confidence)
- [DeepWiki mem0 basic usage](https://deepwiki.com/mem0ai/mem0/10.1-basic-usage) -- Memory class API methods
- [mem0 embedder overview](https://docs.mem0.ai/components/embedders/overview) -- 10 embedder providers, defaults to OpenAI
- [ChromaDB embedding issue #3054](https://github.com/mem0ai/mem0/issues/3054) -- mem0 does NOT use ChromaDB's bundled embedder
- [mem0 telemetry issue #2683](https://github.com/mem0ai/mem0/issues/2683) -- MEM0_TELEMETRY=false

### Tertiary (LOW confidence)
- mem0 search/get_all response format -- verified via DeepWiki but not tested against v1.0.6 directly
- sentence-transformers Docker image size impact -- needs validation in actual build

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- mem0ai 1.0.6 verified on PyPI, ChromaDB backend verified in docs, HuggingFace embedder verified
- Architecture: HIGH -- follows existing knowledge MCP pattern exactly, shared config pattern is straightforward
- Pitfalls: HIGH -- dependency conflict, telemetry, provider name, model download all verified via multiple sources

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (30 days, mem0 is actively developed but v1.0.x is stable)

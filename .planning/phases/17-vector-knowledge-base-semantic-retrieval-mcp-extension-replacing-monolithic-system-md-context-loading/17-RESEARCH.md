# Phase 17: Vector Knowledge Base - Research

**Researched:** 2026-03-15
**Domain:** Semantic retrieval, vector databases, MCP extensions, embedding models
**Confidence:** HIGH

## Summary

This phase replaces monolithic system.md (~22KB, ~408 lines, ~6,000 tokens) loading via .goosehints with a semantic retrieval MCP extension. The bot calls `knowledge_search()` on-demand instead of having all procedures dumped into context. The vector store also absorbs memory.md's role as the unified knowledge persistence layer.

The architecture is a standalone Python MCP server (stdio transport) that wraps ChromaDB in embedded/persistent mode with its built-in ONNX-based all-MiniLM-L6-v2 embeddings. The server exposes tools for search, upsert, and exact-key lookup. Deploy-time re-index wipes the "system" namespace and rebuilds from LOCKED files. Runtime-written chunks ("runtime" namespace) survive re-index.

**Primary recommendation:** Use ChromaDB PersistentClient with default embedding function (ONNX MiniLM-L6-v2, no PyTorch needed). Implement as a FastMCP stdio server with 4 tools: `knowledge_search`, `knowledge_upsert`, `knowledge_get`, `knowledge_delete`. Ship an indexer script run by entrypoint.sh on boot.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- Deploy-time full re-index: wipe and rebuild the vector store during container startup (entrypoint.sh)
- No hot reload, no diff-based updates. Clean slate each deploy
- EVOLVING files (soul.md, user.md) stay in .goosehints, not vectorized
- LOCKED files (system.md, schemas/, onboarding.md) get vectorized
- Vector store absorbs memory.md's role entirely
- Typed chunks with metadata: each chunk gets a type tag (fact, procedure, preference, integration, schema)
- Immediate vector write: bot calls an MCP tool to upsert a chunk instantly at runtime
- Cross-references between chunks: chunks can link to related chunks
- Exact key lookup supported alongside semantic search
- One-time migration: import memory.md contents as typed chunks, then remove memory.md from .goosehints. Clean break
- Explicit tool call: bot decides when to call knowledge_search(), not auto-injected per turn
- Slim .goosehints remains: identity files (soul.md, user.md) stay loaded at session start
- MOIM stays: turn-rules.md continues injecting per-turn via tom extension
- Top 3-5 chunks per retrieval query
- Similarity scores returned with each chunk so bot can judge match quality

### Claude's Discretion
- Knowledge chunking strategy (how system.md is split into retrievable pieces)
- Vector store and embedding model choice (local vs cloud, which model)
- MCP extension implementation details (tool naming, argument design)
- Chunk size optimization
- How cross-references are stored and traversed

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope

</user_constraints>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| chromadb | 1.0+ | Embedded vector store with persistent SQLite backend | Ships with ONNX MiniLM-L6-v2 embeddings (no PyTorch). Single-file persistence. Official MCP server exists as reference. Industry standard for small-scale local vector stores |
| mcp (Python SDK) | 1.7+ | FastMCP framework for building MCP servers | Official MCP SDK. Decorator-based tool registration. Handles stdio JSON-RPC protocol automatically |
| Python 3 | 3.10+ (system) | Runtime | Already in Docker image |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| onnxruntime | (bundled with chromadb) | Embedding inference | ChromaDB's default embedding function uses this internally. No separate install needed |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| ChromaDB | sqlite-vec + fastembed | More control, less abstraction. But ChromaDB bundles embeddings + storage + search in one package. sqlite-vec requires manual embedding pipeline. For ~50-100 chunks, ChromaDB's simplicity wins |
| ChromaDB | FAISS + fastembed | FAISS is faster at scale but requires numpy/scipy. Overkill for <200 chunks. No persistence built-in |
| ONNX MiniLM (ChromaDB default) | OpenAI embeddings API | Cloud dependency, costs money per call, latency. For ~50 chunks that rarely change, local is clearly better |
| FastMCP (Python) | Node.js MCP server | Project already uses Node (npx for context7). But gateway is Python, identity files are on Python-accessible paths, and ChromaDB is a Python library |

**Installation (in Dockerfile):**
```bash
pip3 install --no-cache-dir --break-system-packages chromadb mcp
```

**Docker image size impact:** ChromaDB + ONNX runtime adds ~200-300MB to the image. The ONNX MiniLM model (~22MB) downloads on first use and gets cached to the persistent volume.

## Architecture Patterns

### Recommended Project Structure
```
docker/
  knowledge/
    server.py          # FastMCP stdio server (the MCP extension)
    indexer.py         # Deploy-time indexer (called by entrypoint.sh)
    chunker.py         # system.md -> typed chunks logic
    migrate_memory.py  # One-time memory.md migration script
identity/
  system.md            # Source for vectorization (unchanged)
  schemas/             # Source for vectorization (unchanged)
  onboarding.md        # Source for vectorization (unchanged)
  soul.md              # Stays in .goosehints (EVOLVING, not vectorized)
  user.md              # Stays in .goosehints (EVOLVING, not vectorized)
  memory.md            # Migrated to vector store, then deleted from .goosehints
/data/
  knowledge/
    chroma/            # ChromaDB persistent storage (Railway volume)
    .model_cache/      # ONNX model cache
```

### Pattern 1: Two-Namespace Architecture
**What:** Separate "system" and "runtime" collections in ChromaDB
**When to use:** Always. Deploy-time re-index wipes "system" only. Runtime-written chunks survive.

```python
# System namespace: rebuilt on every deploy
system_collection = client.get_or_create_collection(
    name="system",
    metadata={"description": "LOCKED files: system.md, schemas, onboarding"}
)

# Runtime namespace: persists across deploys, replaces memory.md
runtime_collection = client.get_or_create_collection(
    name="runtime",
    metadata={"description": "Bot-written knowledge: facts, integrations, lessons"}
)
```

**Deploy-time indexer logic:**
```python
# Wipe system namespace only
client.delete_collection("system")
system_collection = client.create_collection(name="system")

# Re-index from source files
chunks = chunk_system_md("/data/identity/system.md")
chunks += chunk_schemas("/data/identity/schemas/")
chunks += chunk_onboarding("/data/identity/onboarding.md")

system_collection.add(
    ids=[c["id"] for c in chunks],
    documents=[c["text"] for c in chunks],
    metadatas=[c["metadata"] for c in chunks],
)

# Runtime collection left untouched
```

### Pattern 2: Typed Chunks with Metadata
**What:** Every chunk carries structured metadata for filtering and cross-referencing
**When to use:** All chunks, both system and runtime

```python
chunk = {
    "id": "system.platform.architecture",  # Hierarchical dot-notation key
    "text": "Two layers. Goose (framework): AI agent by Block...",
    "metadata": {
        "type": "procedure",       # fact|procedure|preference|integration|schema
        "source": "system.md",     # Origin file
        "section": "Platform",    # Section header from source
        "namespace": "system",     # system|runtime
        "refs": "system.platform.extensions,system.platform.discovery",  # Cross-refs (comma-separated IDs)
        "key": "platform-architecture",  # Human-readable exact-lookup key
    }
}
```

### Pattern 3: Chunking Strategy for system.md
**What:** Split by markdown ## sections, with sub-sections as individual chunks
**When to use:** Deploy-time indexing of system.md

The file has clear structure:
- `## Prime Directives` (5 items) -> 1 chunk
- `## Platform` -> split into subsections:
  - Architecture -> 1 chunk
  - Default MCP extensions -> 1 chunk
  - Discovery -> 1 chunk
  - User Commands -> 1 chunk
  - Bots -> 1 chunk
  - Access Control -> 1 chunk
  - Password Recovery -> 1 chunk
- `## Rules` -> split into subsections:
  - Failure Protocol -> 1 chunk
  - Proof of Work -> 1 chunk
  - Credentials and Security -> 1 chunk
  - Prompt Injection Defense -> 1 chunk
  - Identity File Protection -> 1 chunk
  - Cost Awareness -> 1 chunk
  - Media and Unsupported Input -> 1 chunk
  - Data Requests -> 1 chunk
- `## Onboarding` -> 1 chunk
- `## Post-Onboarding` -> split into subsections
- `## Tools` -> split into subsections (CLI, Notifications, Jobs, Watchers, Verbosity)
- `## Extending the Platform` -> split into subsections
- `## Memory System` -> split into subsections
- `## Research Tools` -> 1 chunk

**Estimated total:** ~30-40 chunks from system.md, ~4 from schemas, ~2 from onboarding = ~36-46 system chunks.
**Target chunk size:** 200-500 tokens each (roughly a markdown subsection).

### Pattern 4: MCP Tool Design
**What:** Four tools exposed by the MCP server
**When to use:** Bot calls these via Goose's MCP extension system

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("knowledge")

@mcp.tool()
def knowledge_search(query: str, type: str = "", limit: int = 5) -> str:
    """Search the knowledge base semantically. Returns top matching chunks with similarity scores.

    Args:
        query: Natural language search query
        type: Optional filter by chunk type (fact, procedure, preference, integration, schema)
        limit: Max results (default 5, max 10)
    """
    # Search both system and runtime collections
    # Merge results, sort by similarity
    # Return formatted results with scores

@mcp.tool()
def knowledge_upsert(key: str, content: str, type: str, refs: str = "") -> str:
    """Write or update a knowledge chunk. Used for runtime facts, integrations, lessons.

    Args:
        key: Unique identifier for exact lookup (e.g., "integration.fireflies")
        content: The knowledge content to store
        type: Chunk type (fact, procedure, preference, integration, schema)
        refs: Comma-separated keys of related chunks
    """
    # Upsert into runtime collection
    # ChromaDB handles re-embedding automatically

@mcp.tool()
def knowledge_get(key: str) -> str:
    """Get a specific chunk by exact key. Faster than semantic search when you know the key.

    Args:
        key: Exact chunk key (e.g., "system.tools.jobs", "integration.fireflies")
    """
    # Direct lookup by ID in both collections

@mcp.tool()
def knowledge_delete(key: str) -> str:
    """Delete a runtime knowledge chunk by key.

    Args:
        key: Exact chunk key to delete (only works on runtime chunks)
    """
    # Delete from runtime collection only
    # System chunks cannot be deleted (they're rebuilt on deploy)
```

### Pattern 5: Cross-Reference Traversal
**What:** Chunks store refs as comma-separated IDs. On retrieval, optionally fetch referenced chunks too.
**When to use:** When a search result references related knowledge

```python
def get_with_refs(key: str) -> list:
    """Get a chunk and its referenced chunks."""
    chunk = get_by_key(key)
    if not chunk:
        return []
    results = [chunk]
    refs = chunk["metadata"].get("refs", "").split(",")
    for ref_key in refs:
        ref_chunk = get_by_key(ref_key.strip())
        if ref_chunk:
            results.append(ref_chunk)
    return results
```

### Anti-Patterns to Avoid
- **Vectorizing EVOLVING files (soul.md, user.md):** These change frequently and need to be loaded fully at session start. Vectorizing them would create stale chunks. They stay in .goosehints.
- **Auto-injecting retrieval per turn:** The user explicitly decided against this. The bot calls knowledge_search() when it needs to, like context7 or exa.
- **Single collection for system + runtime:** System chunks get wiped on deploy. Runtime chunks must survive. Two collections prevent accidental data loss.
- **Over-chunking:** system.md is only ~408 lines. Splitting into 100+ tiny chunks would hurt retrieval quality. Target ~30-40 meaningful chunks at the subsection level.
- **Embedding at query time with external API:** Adds latency and cost. ChromaDB's built-in ONNX embeddings handle everything locally.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Vector similarity search | Custom cosine similarity over numpy arrays | ChromaDB's built-in KNN search | Handles indexing, distance metrics, filtering. Battle-tested |
| Text embeddings | Custom embedding pipeline with sentence-transformers | ChromaDB's DefaultEmbeddingFunction (ONNX MiniLM) | Ships with ChromaDB, no PyTorch dependency, ~22MB model |
| MCP protocol handling | Raw stdin/stdout JSON-RPC parsing | FastMCP from mcp Python SDK | Handles protocol negotiation, schema generation, error formatting |
| Persistent vector storage | Manual pickle/JSON serialization of embeddings | ChromaDB PersistentClient (SQLite-backed) | ACID, survives crashes, single-directory storage |

**Key insight:** ChromaDB bundles embeddings + storage + search in one library. Assembling these separately (fastembed + sqlite-vec + custom search) is more work for zero benefit at this scale (~50 chunks).

## Common Pitfalls

### Pitfall 1: ChromaDB Model Download on First Boot
**What goes wrong:** First boot takes 30-60s extra while ONNX model downloads (~22MB)
**Why it happens:** ChromaDB lazily downloads the MiniLM model on first embedding call
**How to avoid:** Pre-warm in the indexer script. Cache model to persistent volume at `/data/knowledge/.model_cache/`. Set `CHROMA_ONNX_MODEL_CACHE` env var.
**Warning signs:** Slow first deploy, fast subsequent deploys

### Pitfall 2: stdout Corruption in MCP stdio Server
**What goes wrong:** Any print() to stdout breaks JSON-RPC protocol
**Why it happens:** MCP stdio uses stdout for protocol messages. Stray print() corrupts the stream
**How to avoid:** ALL logging goes to stderr. Use `logging.basicConfig(stream=sys.stderr)`. Never use bare print().
**Warning signs:** "Parse error" in goose logs, extension stops responding

### Pitfall 3: Wipe-and-Rebuild Deleting Runtime Chunks
**What goes wrong:** Deploy-time re-index accidentally wipes bot-written knowledge
**Why it happens:** Using a single collection or calling client.reset() instead of targeted collection delete
**How to avoid:** Two separate collections: "system" (wiped on deploy) and "runtime" (never wiped). Indexer script ONLY touches system collection.
**Warning signs:** Bot "forgets" integrations and learned facts after redeploy

### Pitfall 4: ChromaDB ID Collisions
**What goes wrong:** Upserting with duplicate IDs silently overwrites existing chunks
**Why it happens:** ChromaDB uses IDs as primary keys. If indexer generates non-unique IDs, chunks overwrite each other
**How to avoid:** Use hierarchical dot-notation IDs derived from file path + section: `system.tools.jobs`, `schema.memory`
**Warning signs:** Missing chunks after indexing, chunk count lower than expected

### Pitfall 5: Embedding Model Mismatch Between Index and Query
**What goes wrong:** Search returns garbage results
**Why it happens:** If the indexer and the MCP server use different embedding functions or models
**How to avoid:** Both use the same ChromaDB PersistentClient pointing to the same directory. ChromaDB stores the embedding function config with the collection. Never mix clients.
**Warning signs:** Low similarity scores on obviously relevant queries

### Pitfall 6: .goosehints Still Loading system.md
**What goes wrong:** system.md loads both via .goosehints AND is in the vector store, defeating the purpose
**Why it happens:** Forgetting to remove the `@identity-data/system.md` line from .goosehints
**How to avoid:** Update .goosehints to remove system.md, schemas, onboarding.md, and memory.md references. Only keep soul.md, user.md, and the runtime context block.
**Warning signs:** No token reduction despite vector store being active

### Pitfall 7: Docker Image Size Bloat
**What goes wrong:** Image size grows significantly from ChromaDB + ONNX dependencies
**Why it happens:** ONNX runtime is ~50-100MB, ChromaDB + deps another ~100-150MB
**How to avoid:** Use `--no-cache-dir` with pip. Consider multi-stage build if size becomes critical. libgomp1 (OpenMP) is already in the Dockerfile.
**Warning signs:** Deploy times increase noticeably

## Code Examples

### MCP Server (server.py)
```python
# Source: MCP Python SDK docs + ChromaDB docs
import os
import sys
import logging
import chromadb
from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("knowledge")

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")

client = chromadb.PersistentClient(path=CHROMA_PATH)
system_col = client.get_or_create_collection("system")
runtime_col = client.get_or_create_collection("runtime")

mcp = FastMCP("knowledge")

@mcp.tool()
def knowledge_search(query: str, type: str = "", limit: int = 5) -> str:
    """Search the knowledge base. Returns relevant chunks with similarity scores."""
    limit = min(limit, 10)
    where_filter = {"type": type} if type else None

    results = []
    for col in [system_col, runtime_col]:
        r = col.query(
            query_texts=[query],
            n_results=limit,
            where=where_filter,
        )
        if r["documents"] and r["documents"][0]:
            for i, doc in enumerate(r["documents"][0]):
                dist = r["distances"][0][i] if r["distances"] else None
                meta = r["metadatas"][0][i] if r["metadatas"] else {}
                score = round(1 - dist, 3) if dist is not None else None
                results.append({"text": doc, "score": score, "key": r["ids"][0][i], **meta})

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    results = results[:limit]

    if not results:
        return "No matching knowledge found."

    lines = []
    for r in results:
        lines.append(f"[{r.get('type','?')}] (score: {r.get('score','?')}) {r['key']}")
        lines.append(r["text"])
        if r.get("refs"):
            lines.append(f"  refs: {r['refs']}")
        lines.append("")
    return "\n".join(lines)

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### Goose Config Extension Entry
```yaml
# Added to extensions: in /data/config/config.yaml
knowledge:
  enabled: true
  type: stdio
  name: Knowledge
  description: Semantic knowledge base for procedures, integrations, and facts
  cmd: python3
  args:
    - /app/docker/knowledge/server.py
  envs:
    KNOWLEDGE_DB_PATH: /data/knowledge/chroma
  env_keys: []
  timeout: 300
  bundled: null
  available_tools: []
```

### Indexer Script (indexer.py)
```python
# Called by entrypoint.sh on boot
import os
import chromadb

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")
IDENTITY_DIR = os.environ.get("IDENTITY_DIR", "/data/identity")

client = chromadb.PersistentClient(path=CHROMA_PATH)

# Wipe system collection (clean rebuild)
try:
    client.delete_collection("system")
except ValueError:
    pass  # Collection doesn't exist yet
system_col = client.create_collection("system")

# Ensure runtime collection exists (never wiped)
client.get_or_create_collection("runtime")

# Index LOCKED files
from chunker import chunk_file

chunks = []
chunks.extend(chunk_file(os.path.join(IDENTITY_DIR, "system.md"), "system.md"))
chunks.extend(chunk_file(os.path.join(IDENTITY_DIR, "onboarding.md"), "onboarding.md"))
for schema_file in os.listdir(os.path.join(IDENTITY_DIR, "schemas")):
    if schema_file.endswith(".schema.md"):
        path = os.path.join(IDENTITY_DIR, "schemas", schema_file)
        chunks.extend(chunk_file(path, f"schemas/{schema_file}"))

system_col.add(
    ids=[c["id"] for c in chunks],
    documents=[c["text"] for c in chunks],
    metadatas=[c["metadata"] for c in chunks],
)

print(f"[knowledge] indexed {len(chunks)} system chunks", flush=True)
```

### Chunker Logic (chunker.py)
```python
import re

def chunk_file(filepath: str, source_name: str) -> list:
    """Split a markdown file into chunks by ## and ### sections."""
    with open(filepath) as f:
        content = f.read()

    chunks = []
    # Split by ## headers
    sections = re.split(r'^## ', content, flags=re.MULTILINE)

    for section in sections[1:]:  # Skip content before first ##
        lines = section.strip().split('\n')
        section_title = lines[0].strip()
        section_body = '\n'.join(lines[1:]).strip()

        # Check for ### subsections
        subsections = re.split(r'^### ', section_body, flags=re.MULTILINE)

        if len(subsections) > 1:
            # Has subsections: each ### becomes its own chunk
            # Include any text before first ### as section intro
            intro = subsections[0].strip()
            if intro:
                chunk_id = _make_id(source_name, section_title)
                chunks.append({
                    "id": chunk_id,
                    "text": f"## {section_title}\n\n{intro}",
                    "metadata": {
                        "type": _infer_type(section_title),
                        "source": source_name,
                        "section": section_title,
                        "namespace": "system",
                        "refs": "",
                        "key": chunk_id,
                    }
                })

            for subsection in subsections[1:]:
                sub_lines = subsection.strip().split('\n')
                sub_title = sub_lines[0].strip()
                sub_body = '\n'.join(sub_lines[1:]).strip()
                chunk_id = _make_id(source_name, section_title, sub_title)
                chunks.append({
                    "id": chunk_id,
                    "text": f"## {section_title} > {sub_title}\n\n{sub_body}",
                    "metadata": {
                        "type": _infer_type(section_title, sub_title),
                        "source": source_name,
                        "section": f"{section_title} > {sub_title}",
                        "namespace": "system",
                        "refs": "",
                        "key": chunk_id,
                    }
                })
        else:
            # No subsections: entire ## section is one chunk
            chunk_id = _make_id(source_name, section_title)
            chunks.append({
                "id": chunk_id,
                "text": f"## {section_title}\n\n{section_body}",
                "metadata": {
                    "type": _infer_type(section_title),
                    "source": source_name,
                    "section": section_title,
                    "namespace": "system",
                    "refs": "",
                    "key": chunk_id,
                }
            })

    return chunks

def _make_id(source: str, *parts: str) -> str:
    """Generate hierarchical dot-notation ID."""
    base = source.replace(".md", "").replace("/", ".").replace(".schema", "")
    slug_parts = [re.sub(r'[^a-z0-9]+', '-', p.lower()).strip('-') for p in parts]
    return f"{base}.{'.'.join(slug_parts)}"

def _infer_type(section: str, subsection: str = "") -> str:
    """Infer chunk type from section names."""
    combined = f"{section} {subsection}".lower()
    if any(w in combined for w in ["rule", "protocol", "defense", "protection", "hygiene"]):
        return "procedure"
    if any(w in combined for w in ["schema", "format"]):
        return "schema"
    if any(w in combined for w in ["platform", "architecture", "extension", "endpoint"]):
        return "fact"
    if any(w in combined for w in ["preference", "verbosity", "style"]):
        return "preference"
    if any(w in combined for w in ["integration", "credential", "vault"]):
        return "integration"
    return "procedure"  # Default: most system.md content is procedural
```

### Updated .goosehints (after migration)
```
# gooseclaw session context
# loaded once at session start. identity files inlined via @file syntax.
# identity-data is a symlink to /data/identity/ (Railway volume).

# !! CRITICAL: if soul.md below contains "ONBOARDING_NEEDED", you MUST
# !! run the Onboarding Flow. search knowledge base for "onboarding flow".
# !! Do NOT respond casually. Do NOT process the user's message normally.
# !! Start with the onboarding greeting. This is your highest priority.

# identity files (agent personality + user profile -- EVOLVING, clean data only)
@identity-data/soul.md
@identity-data/user.md

# runtime context
gooseclaw is a personal autonomous AI agent deployed on Railway.
Identity files are in /data/identity/ (volume-persisted).
Journal entries go to /data/identity/journal/YYYY-MM-DD.md.
Learnings go to /data/identity/learnings/ (LEARNINGS.md, ERRORS.md, FEATURE_REQUESTS.md).
Per-turn critical rules are in turn-rules.md (injected via MOIM, always visible).

# knowledge base
System procedures, platform docs, schemas, integrations, and learned facts
are stored in the vector knowledge base. Use knowledge_search() to look up
anything you need. Use knowledge_upsert() to store new facts and integrations.
Use knowledge_get() for exact key lookup when you know the key.
```

### memory.md Migration Script (migrate_memory.py)
```python
"""One-time migration: import memory.md contents into runtime vector collection."""
import os
import re
import chromadb

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")
MEMORY_FILE = os.path.join(os.environ.get("IDENTITY_DIR", "/data/identity"), "memory.md")

def migrate():
    if not os.path.exists(MEMORY_FILE):
        print("[migrate] memory.md not found, nothing to migrate")
        return

    with open(MEMORY_FILE) as f:
        content = f.read()

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    runtime_col = client.get_or_create_collection("runtime")

    # Parse memory.md sections
    sections = re.split(r'^## ', content, flags=re.MULTILINE)
    migrated = 0

    type_map = {
        "integrations": "integration",
        "projects": "fact",
        "tools": "fact",
        "lessons learned": "fact",
    }

    for section in sections[1:]:
        lines = section.strip().split('\n')
        title = lines[0].strip().lower()
        body = '\n'.join(lines[1:]).strip()

        if not body or body == '| Service | Purpose | Status | Notes |\n|---------|---------|--------|-------|':
            continue  # Empty section

        chunk_type = type_map.get(title, "fact")
        chunk_id = f"memory.{title.replace(' ', '-')}"

        runtime_col.upsert(
            ids=[chunk_id],
            documents=[body],
            metadatas=[{
                "type": chunk_type,
                "source": "memory.md",
                "section": title,
                "namespace": "runtime",
                "refs": "",
                "key": chunk_id,
            }],
        )
        migrated += 1

    print(f"[migrate] migrated {migrated} sections from memory.md")

if __name__ == "__main__":
    migrate()
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| ChromaDB with DuckDB backend | ChromaDB with SQLite backend | ChromaDB 0.4.0 (2024) | Simpler, more portable persistence |
| sentence-transformers + PyTorch for embeddings | ONNX Runtime (ChromaDB default) | ChromaDB 0.4+ (2024) | No PyTorch dependency, ~10x smaller install |
| Custom MCP JSON-RPC parsing | FastMCP decorators (mcp SDK) | mcp SDK 1.0+ (2025) | 5 lines to define a tool vs 100+ lines of protocol handling |
| `mcp.run()` with `transport="sse"` | `transport="stdio"` for local tools | Always standard | stdio is the standard for local process-based MCP servers |

**Deprecated/outdated:**
- ChromaDB DuckDB backend: removed in 0.4.0
- sqlite-vss: replaced by sqlite-vec (same author)
- `fastmcp` package (separate from `mcp`): FastMCP is now part of the official `mcp` package as `mcp.server.fastmcp`

## Open Questions

1. **ChromaDB ONNX model caching across deploys**
   - What we know: ChromaDB downloads MiniLM model on first use (~22MB). Can be cached via `CHROMA_ONNX_MODEL_CACHE` env var.
   - What's unclear: Exact env var name may vary by ChromaDB version. Need to verify at implementation time.
   - Recommendation: Point cache to `/data/knowledge/.model_cache/` on the persistent volume. Test first boot time.

2. **Cross-reference traversal depth**
   - What we know: User wants cross-references between chunks
   - What's unclear: Should we auto-expand refs in search results? How deep? (1 level is probably fine)
   - Recommendation: Default to 1-level expansion. Include ref keys in search results. Let bot call knowledge_get() for specific refs if needed.

3. **turn-rules.md knowledge base instructions**
   - What we know: MOIM injects turn-rules.md every turn. Bot needs to know knowledge_search() exists.
   - What's unclear: Should turn-rules.md mention the knowledge base? Or is .goosehints sufficient?
   - Recommendation: Add a single line to turn-rules.md: "Use knowledge_search() to look up procedures, integrations, and facts. Do NOT guess when you can look it up." This mirrors the existing research tools guidance.

4. **Entrypoint.sh timing**
   - What we know: Indexer runs during container startup before gateway starts
   - What's unclear: How long does ChromaDB indexing of ~40 chunks take? (Likely <5s excluding model download)
   - Recommendation: Run indexer after symlinks but before gateway. Add timing output. First boot will be slower due to model download.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (Python stdlib) |
| Config file | none (tests run via `python3 -m unittest`) |
| Quick run command | `cd /Users/haseeb/nix-template && python3 -m pytest docker/test_knowledge.py -x` |
| Full suite command | `cd /Users/haseeb/nix-template && python3 -m unittest docker/test_gateway.py docker/test_knowledge.py` |
| Estimated runtime | ~3-5 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| KB-01 | System chunks indexed from system.md sections | unit | `python3 -m unittest docker/test_knowledge.py::TestChunker -v` | No -- Wave 0 gap |
| KB-02 | knowledge_search returns top-N results with scores | unit | `python3 -m unittest docker/test_knowledge.py::TestKnowledgeSearch -v` | No -- Wave 0 gap |
| KB-03 | knowledge_upsert writes to runtime collection | unit | `python3 -m unittest docker/test_knowledge.py::TestKnowledgeUpsert -v` | No -- Wave 0 gap |
| KB-04 | knowledge_get exact key lookup | unit | `python3 -m unittest docker/test_knowledge.py::TestKnowledgeGet -v` | No -- Wave 0 gap |
| KB-05 | Deploy re-index wipes system, preserves runtime | unit | `python3 -m unittest docker/test_knowledge.py::TestIndexer -v` | No -- Wave 0 gap |
| KB-06 | Typed metadata preserved through index/query cycle | unit | `python3 -m unittest docker/test_knowledge.py::TestMetadata -v` | No -- Wave 0 gap |
| KB-07 | memory.md migration produces correct runtime chunks | unit | `python3 -m unittest docker/test_knowledge.py::TestMigration -v` | No -- Wave 0 gap |
| KB-08 | .goosehints no longer references system.md/memory.md | unit | `python3 -m unittest docker/test_knowledge.py::TestGoosehints -v` | No -- Wave 0 gap |
| KB-09 | Cross-references stored and retrievable | unit | `python3 -m unittest docker/test_knowledge.py::TestCrossRefs -v` | No -- Wave 0 gap |
| KB-10 | knowledge_delete only works on runtime chunks | unit | `python3 -m unittest docker/test_knowledge.py::TestKnowledgeDelete -v` | No -- Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `cd /Users/haseeb/nix-template && python3 -m unittest docker/test_knowledge.py -v`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~3-5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/test_knowledge.py` -- covers KB-01 through KB-10
- [ ] ChromaDB install: `pip3 install --no-cache-dir --break-system-packages chromadb mcp`
- [ ] Test fixtures: in-memory ChromaDB client (EphemeralClient) for test isolation

## Sources

### Primary (HIGH confidence)
- ChromaDB official docs (trychroma.com) -- persistent client, collections, embedding functions, query API
- MCP Python SDK (github.com/modelcontextprotocol/python-sdk) -- FastMCP tool decorator pattern, stdio transport
- Existing codebase analysis -- gateway.py imports, Dockerfile, entrypoint.sh, .goosehints, system.md structure

### Secondary (MEDIUM confidence)
- ChromaDB PyPI (pypi.org/project/chromadb) -- version info, dependency chain
- FastMCP examples (multiple tutorial sources) -- tool implementation patterns
- sqlite-vec comparison (github.com/asg017/sqlite-vec) -- alternative evaluation

### Tertiary (LOW confidence)
- Docker image size estimates for ChromaDB + ONNX (~200-300MB) -- based on community reports, needs verification during implementation
- ChromaDB ONNX model cache env var name -- may vary by version

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- ChromaDB + FastMCP is the dominant pattern for local vector MCP servers. Multiple working examples exist (chroma-mcp, HumainLabs/chromaDB-mcp)
- Architecture: HIGH -- Two-namespace pattern follows directly from user's locked decisions. Chunking strategy matches system.md's clear markdown structure
- Pitfalls: HIGH -- stdout corruption, namespace separation, model caching are well-documented issues in MCP + ChromaDB communities

**Research date:** 2026-03-15
**Valid until:** 2026-04-15 (stable libraries, 30 days)

# Phase 24: ChromaDB Migration + Cleanup - Research

**Researched:** 2026-03-20
**Domain:** Data migration (ChromaDB runtime collection to mem0) + legacy code cleanup
**Confidence:** HIGH

## Summary

Phase 24 is a data migration and code cleanup phase. The existing ChromaDB "runtime" collection contains user memories stored by the old knowledge_upsert pipeline (manual extraction, key-based upsert, typed metadata). These need to be migrated into mem0's "mem0_memories" collection so that all user memories are queryable via mem0's search/list/history tools. After migration, the runtime collection is deprecated and all code references to it are removed or narrowed.

The migration must bypass mem0.add()'s default LLM extraction pipeline (MIG-02). Using `mem0.add(messages, infer=False)` is the correct approach: it stores content directly with local HuggingFace embeddings (free, no API calls), creates proper SQLite history entries, and generates UUIDs for mem0's internal tracking. Each runtime collection entry becomes one mem0 memory. A sentinel file (`/data/knowledge/.mem0_migrated`) prevents re-runs on subsequent boots.

The cleanup scope is well-defined: (1) knowledge/server.py becomes system-only (remove runtime_col references from search, get, delete, recent; remove knowledge_upsert entirely), (2) knowledge/indexer.py stops ensuring runtime collection exists, (3) entrypoint.sh memory.md migration block becomes a no-op after mem0 migration runs, (4) existing tests are updated to match the new system-only knowledge server. Gateway.py was already cleaned up in Phase 23, there is nothing to change there.

**Primary recommendation:** Write a migration script at `docker/knowledge/migrate_to_mem0.py` that reads all runtime collection entries via `col.get(include=['documents', 'metadatas'])`, inserts each into mem0 via `memory.add(content, infer=False, user_id=USER_ID)`, writes a sentinel file, and is called from entrypoint.sh before the knowledge indexer runs. Then clean up knowledge/server.py to be system-only.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| MIG-01 | One-time migration script moves chromadb runtime memories to mem0 | Migration script reads runtime collection via `col.get()`, inserts each entry via `mem0.add(content, infer=False)`. Follows existing migrate_memory.py pattern. |
| MIG-02 | Migration bypasses mem0.add() (direct insert, no re-extraction) | `mem0.add(messages, infer=False)` stores content directly, skips LLM extraction. Still creates embeddings (local HuggingFace) and SQLite history entries. Zero API cost. |
| MIG-03 | ChromaDB runtime collection deprecated after migration (system collection stays) | knowledge/server.py becomes system-only. Remove runtime_col from search/get/delete/recent. Remove knowledge_upsert tool entirely. Update indexer.py to stop creating runtime collection. |
| MIG-04 | Sentinel file prevents accidental re-migration | `/data/knowledge/.mem0_migrated` sentinel. Follows exact same pattern as existing `/data/knowledge/.memory_migrated` (entrypoint.sh lines 573-581). |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mem0ai | 1.0.6 (already installed) | Target memory store. add(infer=False) for direct insert | Already installed from Phase 22. infer=False bypasses LLM, uses local embeddings only |
| chromadb | 1.5.5 (existing) | Source data store. PersistentClient to read runtime collection | Already installed. col.get() reads all entries |
| mem0_config | local module (Phase 22) | build_mem0_config() creates mem0 config dict | Already built. Shared between MCP server, gateway, and migration script |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| os, json, time | stdlib | File I/O, sentinel file, timestamps | Migration script, entrypoint integration |
| unittest.mock | stdlib | Mocking mem0.Memory and chromadb for tests | All test files for this phase |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `mem0.add(infer=False)` | Direct chromadb collection.add() to mem0_memories | Skips SQLite history, skips hash generation, skips proper mem0 metadata structure. mem0 wouldn't track these as real memories. |
| `mem0.add(infer=True)` | Full LLM re-extraction | Costs API tokens (3-6 LLM calls per memory), takes 2-20s per entry, may produce different extracted facts than the original. Violates MIG-02. |
| Single migration script | Separate read/transform/write phases | Over-engineering. Runtime collections are typically small (10-100 entries). Single script is simpler and follows existing migrate_memory.py pattern. |

**Installation:**
```bash
# No new deps. Everything already installed from Phase 22.
```

## Architecture Patterns

### Recommended Project Structure Changes
```
docker/
+-- knowledge/
|   +-- migrate_to_mem0.py     # NEW: runtime collection -> mem0 migration
|   +-- server.py              # MODIFIED: system-only (remove runtime_col)
|   +-- indexer.py             # MODIFIED: stop ensuring runtime collection
|   +-- migrate_memory.py      # UNCHANGED: memory.md -> runtime (historical, pre-mem0)
+-- mem0_config.py             # UNCHANGED (Phase 22)
+-- memory/server.py           # UNCHANGED (Phase 22)
+-- gateway.py                 # UNCHANGED (Phase 23 already cleaned)
+-- entrypoint.sh              # MODIFIED: add mem0 migration block
+-- test_server.py             # MODIFIED: remove runtime_col tests, test system-only behavior
+-- test_knowledge.py          # MODIFIED: update indexer tests, update migration tests
```

### Pattern 1: Migration Script with Sentinel File
**What:** A standalone Python script that reads from source, writes to destination, and touches a sentinel file on success. Called from entrypoint.sh with a guard check.
**When to use:** One-time data migration that must survive container restarts.
**Example:**
```python
# docker/knowledge/migrate_to_mem0.py
# Follows the exact pattern of docker/knowledge/migrate_memory.py
import os
import sys
import chromadb

# Disable telemetry before mem0 import
os.environ["MEM0_TELEMETRY"] = "false"

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")
SENTINEL = os.path.join(os.path.dirname(CHROMA_PATH), ".mem0_migrated")


def migrate(chroma_path=None, sentinel_path=None):
    """Migrate runtime ChromaDB entries to mem0. One-time, idempotent."""
    if chroma_path is None:
        chroma_path = CHROMA_PATH
    if sentinel_path is None:
        sentinel_path = SENTINEL

    # Guard: already migrated
    if os.path.exists(sentinel_path):
        print("[mem0-migrate] already migrated, skipping", flush=True)
        return 0

    # Read source: chromadb runtime collection
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        runtime_col = client.get_collection("runtime")
    except Exception:
        print("[mem0-migrate] no runtime collection found, nothing to migrate", flush=True)
        _touch_sentinel(sentinel_path)
        return 0

    all_data = runtime_col.get(include=["documents", "metadatas"])
    if not all_data["ids"]:
        print("[mem0-migrate] runtime collection is empty, nothing to migrate", flush=True)
        _touch_sentinel(sentinel_path)
        return 0

    # Initialize mem0
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mem0 import Memory
    from mem0_config import build_mem0_config

    config = build_mem0_config()
    memory = Memory.from_config(config)
    user_id = os.environ.get("MEM0_USER_ID", "default")

    # Migrate each entry
    migrated = 0
    for i, doc_id in enumerate(all_data["ids"]):
        doc = all_data["documents"][i] if all_data["documents"] else ""
        meta = all_data["metadatas"][i] if all_data["metadatas"] else {}

        if not doc or not doc.strip():
            continue

        try:
            # infer=False: bypass LLM extraction, store content directly
            # Each document becomes one mem0 memory with local embeddings
            memory.add(
                messages=doc,
                user_id=user_id,
                infer=False,
                metadata={
                    "source": "chromadb_migration",
                    "original_key": doc_id,
                    "original_type": meta.get("type", ""),
                },
            )
            migrated += 1
        except Exception as e:
            print(f"[mem0-migrate] failed to migrate {doc_id}: {e}", flush=True)

    _touch_sentinel(sentinel_path)
    print(f"[mem0-migrate] migrated {migrated}/{len(all_data['ids'])} entries", flush=True)
    return migrated


def _touch_sentinel(path):
    """Create sentinel file to prevent re-migration."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        import time
        f.write(f"migrated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")


if __name__ == "__main__":
    migrate()
```

### Pattern 2: Entrypoint.sh Migration Block
**What:** A guarded bash block that runs the migration script before the knowledge indexer, following the exact same pattern as the existing memory.md migration.
**When to use:** Boot-time one-time migration.
**Example:**
```bash
# Place AFTER knowledge dir setup, BEFORE indexer run (around line 572-581)
# Follows the EXACT pattern of the memory.md migration block above it
if [ ! -f "$DATA_DIR/knowledge/.mem0_migrated" ]; then
    echo "[mem0-migrate] migrating runtime memories to mem0..."
    if runuser -u gooseclaw -- env PYTHONPATH=/app/docker MEM0_USER_ID=default MEM0_TELEMETRY=false python3 /app/docker/knowledge/migrate_to_mem0.py; then
        echo "[mem0-migrate] migration complete"
    else
        echo "[mem0-migrate] WARNING: migration failed (non-fatal)"
    fi
fi
```

### Pattern 3: System-Only Knowledge Server
**What:** knowledge/server.py narrowed to only search/get the system collection. No more runtime_col, no more knowledge_upsert.
**When to use:** After migration, runtime memories live in mem0. The knowledge server handles only system docs.
**Key changes:**
```python
# REMOVE: runtime_col = client.get_or_create_collection("runtime", ...)
# REMOVE: knowledge_upsert tool entirely
# MODIFY: knowledge_search to only query system_col
# MODIFY: knowledge_get to only query system_col
# REMOVE: knowledge_delete (was runtime-only, no longer needed)
# MODIFY: knowledge_recent to only query system_col
```

### Anti-Patterns to Avoid
- **Deleting the runtime collection during migration:** Leave it in place. The migration script reads from it, and other boot processes (indexer.py) may still reference it briefly. Just stop writing to it and stop reading from it in the server.
- **Using mem0.add(infer=True) for migration:** Violates MIG-02, costs API tokens, and may produce different facts than what was originally stored.
- **Running migration AFTER the knowledge indexer:** The indexer currently calls `client.get_or_create_collection("runtime")`. If we remove that from the indexer first but migration hasn't run yet, the collection reference breaks. Run migration BEFORE the indexer, then clean up the indexer.
- **Modifying gateway.py:** Phase 23 already removed all runtime collection references from gateway.py. Don't touch it.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Embedding generation for migrated entries | Manual sentence-transformers embed() calls | `mem0.add(infer=False)` | mem0 handles embedding internally with the configured HuggingFace model |
| SQLite history records for migrated entries | Manual SQLite INSERT statements | `mem0.add(infer=False)` | mem0 creates proper ADD history entries automatically |
| UUID generation for mem0 memory IDs | Manual uuid4() calls | `mem0.add(infer=False)` | mem0 generates UUIDs internally via _create_memory() |
| Sentinel file locking | File locks, atomic writes | Simple file existence check + write | Same pattern as existing .memory_migrated and .initialized. Race conditions not a concern (single process at boot time) |

**Key insight:** `mem0.add(infer=False)` is the perfect migration tool. It does everything except LLM extraction: generates embeddings (local), creates UUIDs, stores in ChromaDB, records history in SQLite. Zero API cost, zero re-extraction.

## Common Pitfalls

### Pitfall 1: mem0 Init Requires sentence-transformers Model
**What goes wrong:** First call to `Memory.from_config()` in the migration script triggers a ~90MB model download if sentence-transformers model isn't cached yet.
**Why it happens:** The migration script runs during boot, possibly before the mem0 MCP server has started (which would have cached the model).
**How to avoid:** The Dockerfile should pre-download the model (Phase 22 should have handled this). If not, the migration script will be slow on first run but will succeed. Not a blocker.
**Warning signs:** Migration script takes 30+ seconds on first boot.

### Pitfall 2: Empty Runtime Collection on Fresh Deploy
**What goes wrong:** On a fresh deploy (no existing data), there's no runtime collection. The migration script tries to get it and fails.
**Why it happens:** New users don't have any runtime memories yet.
**How to avoid:** Guard with try/except on `client.get_collection("runtime")`. If collection doesn't exist, touch sentinel and return 0. Already handled in the example code above.
**Warning signs:** Migration script errors on fresh deploys.

### Pitfall 3: Removing knowledge_upsert Breaks Goose Tool Calls
**What goes wrong:** If goose has cached tool descriptions that include `knowledge_upsert`, it may try to call it after the tool is removed. The MCP server will return an error.
**Why it happens:** Goose caches available_tools from MCP server registration. If a session persists across deploys, stale tool references linger.
**How to avoid:** This is acceptable. Goose handles unknown tool errors gracefully (retries or reports error). New sessions will get the updated tool list. The mem0-memory extension's `memory_add` is the replacement.
**Warning signs:** One-time errors in goose logs about `knowledge_upsert` not found after deploy.

### Pitfall 4: Forgetting to Update the Indexer
**What goes wrong:** `knowledge/indexer.py` line 37 calls `client.get_or_create_collection("runtime")` to "ensure runtime collection exists." After migration, this creates an empty runtime collection on every deploy, which is confusing.
**Why it happens:** Historical safety check that's no longer needed.
**How to avoid:** Remove the `get_or_create_collection("runtime")` line from indexer.py. The indexer only needs the system collection.
**Warning signs:** Empty runtime collection appears after deploy despite being deprecated.

### Pitfall 5: Test Files Reference runtime_col Extensively
**What goes wrong:** test_server.py has 15+ references to `runtime_col`. test_knowledge.py has runtime collection assertions in indexer and migration tests. All break after cleanup.
**Why it happens:** Tests were written for the two-namespace architecture.
**How to avoid:** Systematically update tests: (1) test_server.py becomes system-only, remove runtime_col setup/teardown and all runtime-related test methods, (2) test_knowledge.py indexer tests remove runtime collection assertions, (3) test_knowledge.py migration tests for migrate_memory.py still work (they test the old migration, which is preserved for historical compatibility).
**Warning signs:** Test failures after cleanup.

### Pitfall 6: Concurrent ChromaDB Access During Migration
**What goes wrong:** Migration reads runtime collection while the knowledge indexer or mem0 MCP server might be accessing the same ChromaDB path.
**Why it happens:** Multiple processes share `/data/knowledge/chroma`.
**How to avoid:** Run migration BEFORE the indexer and BEFORE starting gateway.py (which starts the MCP servers). The entrypoint.sh already has this ordering: knowledge indexer runs before gateway starts. Insert migration before the indexer.
**Warning signs:** "database is locked" SQLite errors during migration.

## Code Examples

### Reading All Entries from ChromaDB Runtime Collection
```python
# Source: Verified pattern from docker/knowledge/server.py lines 213-214
# and docker/knowledge/migrate_memory.py lines 86-93
client = chromadb.PersistentClient(path="/data/knowledge/chroma")
runtime_col = client.get_collection("runtime")

# get() with no filters returns ALL entries
all_data = runtime_col.get(include=["documents", "metadatas"])
# Returns: {"ids": [...], "documents": [...], "metadatas": [...]}

for i, doc_id in enumerate(all_data["ids"]):
    doc = all_data["documents"][i]
    meta = all_data["metadatas"][i]
    print(f"{doc_id}: {doc[:50]}... type={meta.get('type', '?')}")
```

### mem0.add() with infer=False (Direct Insert)
```python
# Source: Verified via mem0 GitHub source (memory/main.py) and official docs
# https://docs.mem0.ai/core-concepts/memory-operations/add
from mem0 import Memory
from mem0_config import build_mem0_config

config = build_mem0_config()
memory = Memory.from_config(config)

# infer=False: bypasses LLM extraction, stores content directly
# Still creates embeddings (local HuggingFace), SQLite history, UUID
result = memory.add(
    messages="User prefers dark theme and uses VSCode",  # can be string
    user_id="default",
    infer=False,
    metadata={
        "source": "chromadb_migration",
        "original_key": "memory.preferences.editor",
    },
)
# result: [{"id": "uuid-here", "memory": "User prefers...", "event": "ADD"}]
```

### System-Only Knowledge Server (After Cleanup)
```python
# docker/knowledge/server.py (after cleanup)
# Key changes: remove runtime_col, remove knowledge_upsert, remove knowledge_delete

import os, sys, logging, time, chromadb
from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("knowledge")

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")

try:
    client = chromadb.PersistentClient(path=CHROMA_PATH)
except Exception:
    client = chromadb.EphemeralClient()

system_col = client.get_or_create_collection("system", metadata={"hnsw:space": "cosine"})
# REMOVED: runtime_col = client.get_or_create_collection("runtime", ...)

mcp = FastMCP("knowledge")

@mcp.tool()
def knowledge_search(query: str, type: str = "", limit: int = 5, since: str = "") -> str:
    """Search the system knowledge base semantically."""
    limit = max(1, min(limit, 10))
    where_filter = {"type": type} if type else None
    # CHANGED: only search system_col, not [system_col, runtime_col]
    try:
        kwargs = {"query_texts": [query], "n_results": limit}
        if where_filter:
            kwargs["where"] = where_filter
        r = system_col.query(**kwargs)
    except Exception as e:
        logger.warning("query failed: %s", e)
        return "No matching knowledge found."
    # ... format results ...

# REMOVED: knowledge_upsert (was runtime-only, replaced by mem0 memory_add)
# REMOVED: knowledge_delete (was runtime-only, replaced by mem0 memory_delete)

@mcp.tool()
def knowledge_get(key: str) -> str:
    """Get a specific system chunk by exact key."""
    # CHANGED: only search system_col
    got = system_col.get(ids=[key], include=["documents", "metadatas"])
    # ...

@mcp.tool()
def knowledge_recent(limit: int = 5) -> str:
    """Get recently updated system knowledge chunks."""
    # CHANGED: only query system_col
    # ...
```

### Sentinel File Pattern (Existing Convention)
```bash
# Source: docker/entrypoint.sh lines 573-581 (existing .memory_migrated pattern)
# This EXACT pattern is what Phase 24 follows:

if [ -f "$IDENTITY_DIR/memory.md" ] && [ ! -f "$DATA_DIR/knowledge/.memory_migrated" ]; then
    echo "[knowledge] migrating memory.md to vector store..."
    if runuser -u gooseclaw -- env PYTHONPATH=/app/docker python3 /app/docker/knowledge/migrate_memory.py; then
        touch "$DATA_DIR/knowledge/.memory_migrated"
        echo "[knowledge] memory migration complete"
    else
        echo "[knowledge] WARNING: memory migration failed (non-fatal)"
    fi
fi

# Phase 24 adds an analogous block for mem0 migration:
if [ ! -f "$DATA_DIR/knowledge/.mem0_migrated" ]; then
    echo "[mem0-migrate] migrating runtime memories to mem0..."
    if runuser -u gooseclaw -- env PYTHONPATH=/app/docker MEM0_USER_ID=default MEM0_TELEMETRY=false python3 /app/docker/knowledge/migrate_to_mem0.py; then
        echo "[mem0-migrate] migration complete"
    else
        echo "[mem0-migrate] WARNING: migration failed (non-fatal)"
    fi
fi
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Two-namespace knowledge server (system + runtime) | System-only knowledge server + mem0 for memories | This phase (24) | Cleaner separation. knowledge = system docs, mem0 = user memories |
| knowledge_upsert for storing user facts | mem0 memory_add for storing user facts | Phase 23 (gateway) + Phase 24 (cleanup) | Better dedup, contradiction resolution, semantic search |
| Manual key-based memory IDs (e.g., "memory.preferences.editor") | mem0 UUID-based memory IDs | This phase (24) | mem0 manages IDs internally, no manual key management |
| memory.md -> runtime collection migration (migrate_memory.py) | runtime collection -> mem0 migration (migrate_to_mem0.py) | This phase (24) | Two-hop migration: memory.md -> runtime -> mem0. Historical. |

**Deprecated/outdated after this phase:**
- `knowledge_upsert` tool: Removed. Use `memory_add` via mem0-memory extension instead.
- `knowledge_delete` tool: Removed. Use `memory_delete` via mem0-memory extension instead.
- `runtime_col` in knowledge/server.py: Removed. System collection only.
- `get_or_create_collection("runtime")` in indexer.py: Removed. Indexer only manages system collection.
- ChromaDB runtime collection: Deprecated. Data migrated to mem0_memories collection. Collection left in place but no code reads from or writes to it.

## What Gets Modified (File-by-File)

| File | Change | Why |
|------|--------|-----|
| `docker/knowledge/migrate_to_mem0.py` | NEW: migration script | MIG-01, MIG-02, MIG-04 |
| `docker/entrypoint.sh` | ADD: mem0 migration block (before indexer) | MIG-04: boot-time migration with sentinel guard |
| `docker/knowledge/server.py` | MODIFY: remove runtime_col, knowledge_upsert, knowledge_delete | MIG-03: system-only |
| `docker/knowledge/indexer.py` | MODIFY: remove `get_or_create_collection("runtime")` line 37 | MIG-03: stop ensuring runtime exists |
| `docker/test_server.py` | MODIFY: remove runtime_col from setup/teardown, remove runtime tests | Tests match new system-only server |
| `docker/test_knowledge.py` | MODIFY: update indexer tests (no runtime assertions), add migration tests | Tests for migrate_to_mem0.py |
| `docker/test_gateway.py` | MODIFY: remove skipped chromadb tests (lines 715-819) | Dead tests from Phase 23, fully obsolete now |

## Open Questions

1. **Should we delete the runtime collection entirely or leave it?**
   - What we know: After migration, no code reads from or writes to the runtime collection. It just sits there in ChromaDB.
   - What's unclear: Whether leaving an unused collection causes any performance or storage issues.
   - Recommendation: Leave it in place. Deleting requires `client.delete_collection("runtime")` which is destructive and irreversible. The data is tiny. If cleanup is desired later, it can be done manually.

2. **Should migrate_memory.py (the old memory.md migration) still run?**
   - What we know: The old migration moves memory.md sections into the runtime collection. But after mem0 migration, the runtime collection is deprecated. On fresh deploys, a user might have memory.md but no runtime collection yet.
   - What's unclear: Whether new users should still go through memory.md -> runtime -> mem0, or skip to mem0 directly.
   - Recommendation: Keep the old migration in place. It runs before the mem0 migration. On fresh deploy: memory.md -> runtime (old migration) -> mem0 (new migration). The extra hop is harmless and keeps the migration chain clean. The sentinel files prevent re-runs.

3. **mem0.add(infer=False) with string input vs message dict**
   - What we know: mem0.add() accepts `str`, `dict`, or `list[dict]`. The runtime collection stores full text documents (not conversation messages).
   - What's unclear: Whether passing a plain string to add(infer=False) works the same as a message dict.
   - Recommendation: Pass as string. The source data is document text, not conversation messages. Test in the migration script and verify the mem0 response structure.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + unittest (existing pattern) |
| Config file | docker/pytest.ini (testpaths = tests, timeout = 30) |
| Quick run command | `cd docker && python -m pytest test_server.py test_knowledge.py -x --timeout=30` |
| Full suite command | `cd docker && python -m pytest test_server.py test_knowledge.py test_gateway.py -v --timeout=30` |
| Estimated runtime | ~15 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MIG-01 | Migration script reads runtime entries and writes to mem0 | unit | `cd docker && python -m pytest test_knowledge.py -x -k "TestMem0Migration"` | No, Wave 0 gap |
| MIG-02 | Migration uses infer=False (no LLM calls) | unit | `cd docker && python -m pytest test_knowledge.py -x -k "test_migration_uses_infer_false"` | No, Wave 0 gap |
| MIG-03 | knowledge/server.py is system-only after cleanup | unit | `cd docker && python -m pytest test_server.py -x` | Partial (existing tests need updating) |
| MIG-04 | Sentinel file prevents re-migration | unit | `cd docker && python -m pytest test_knowledge.py -x -k "test_sentinel"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `cd docker && python -m pytest test_server.py test_knowledge.py -x --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~10 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/test_knowledge.py::TestMem0Migration` -- covers MIG-01, MIG-02, MIG-04 (unit tests for migrate_to_mem0.py, mock mem0.Memory)
- [ ] Update `docker/test_server.py` -- remove runtime_col from _ServerTestBase, remove TestKnowledgeUpsert, remove TestKnowledgeDelete (they test removed functionality), update search/get/recent tests to system-only
- [ ] Update `docker/test_knowledge.py::TestIndexer` -- remove `test_runtime_collection_preserved` and `test_runtime_collection_created_if_missing` (runtime no longer ensured by indexer)
- [ ] Remove skipped chromadb tests from `docker/test_gateway.py` lines 715-819 (fully dead after Phase 23 + 24)

**Testing approach:** Mock `mem0.Memory` and `mem0.Memory.from_config` in migration tests. Use `chromadb.EphemeralClient()` for source runtime collection (same pattern as existing test_knowledge.py). Verify `memory.add()` is called with `infer=False` and correct content. Verify sentinel file is created. Verify empty/missing collection is handled gracefully.

## Sources

### Primary (HIGH confidence)
- Codebase: docker/knowledge/server.py -- current two-namespace knowledge server (verified, 264 lines)
- Codebase: docker/knowledge/indexer.py -- system indexer with runtime ensure (verified, 86 lines)
- Codebase: docker/knowledge/migrate_memory.py -- existing migration pattern (verified, 101 lines)
- Codebase: docker/entrypoint.sh lines 573-581 -- sentinel file pattern (verified)
- Codebase: docker/gateway.py lines 6777-6966 -- Phase 23 mem0 integration (verified, no chromadb refs)
- Codebase: docker/test_server.py -- knowledge server tests with runtime_col (verified, 303 lines)
- Codebase: docker/test_knowledge.py -- indexer and migration tests (verified, 573 lines)
- Codebase: docker/test_gateway.py lines 715-819 -- skipped chromadb tests from Phase 23 (verified)
- Codebase: docker/mem0_config.py -- shared config module (verified, 110 lines)
- [mem0 GitHub source: memory/main.py](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) -- add() with infer=False code path (verified)
- [mem0 GitHub source: vector_stores/chroma.py](https://github.com/mem0ai/mem0/blob/main/mem0/vector_stores/chroma.py) -- insert() method (verified)

### Secondary (MEDIUM confidence)
- [mem0 add memories docs](https://docs.mem0.ai/core-concepts/memory-operations/add) -- infer parameter documentation
- [mem0 ChromaDB config docs](https://docs.mem0.ai/components/vectordbs/dbs/chroma) -- provider name, config options
- [agno issue #3425](https://github.com/agno-agi/agno/issues/3425) -- discusses infer parameter behavior

### Tertiary (LOW confidence)
- mem0.add(infer=False) with plain string input -- verified from source code analysis but not tested locally. The code path handles str, dict, and list inputs identically for infer=False.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already installed, no new deps
- Architecture: HIGH -- follows existing migration pattern (migrate_memory.py), well-understood codebase
- Pitfalls: HIGH -- all identified from direct codebase analysis, not theoretical
- Migration approach: HIGH -- mem0.add(infer=False) verified in source code, sentinel pattern copied from existing code

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (30 days, stable codebase with no external dependencies changing)

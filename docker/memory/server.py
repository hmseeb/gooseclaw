"""FastMCP mem0 memory server with 6 tools wrapping mem0.Memory.

Provides long-term memory with semantic search, contradiction resolution,
and deduplication. All logging to stderr to avoid corrupting MCP stdio protocol.
"""

import os

# Disable telemetry BEFORE any mem0 import (Pitfall 4)
os.environ["MEM0_TELEMETRY"] = "false"

import sys
import json
import logging
from mcp.server.fastmcp import FastMCP
from mem0 import Memory

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("mem0-memory")

# Import shared config (same parent-dir pattern as knowledge/server.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mem0_config import build_mem0_config

config = build_mem0_config()
_diag = open("/data/mem0_diag.log", "w")
_diag.write(f"graph_store in config: {'graph_store' in config}\n")
_diag.write(f"llm.provider: {config.get('llm', {}).get('provider', '?')}\n")
_diag.write(f"llm.model: {config.get('llm', {}).get('config', {}).get('model', '?')}\n")
_diag.write(f"llm has api_key: {bool(config.get('llm', {}).get('config', {}).get('api_key'))}\n")
_diag.write(f"graph_store config: {config.get('graph_store', 'NONE')}\n")
_diag.flush()
try:
    memory = Memory.from_config(config)
    has_graph = hasattr(memory, 'graph') and memory.graph is not None
    _diag.write(f"mem0 init OK. has graph={has_graph}\n")
    if has_graph:
        _diag.write(f"graph type: {type(memory.graph)}\n")
    _diag.flush()
except Exception as e:
    _diag.write(f"mem0 init FAILED: {type(e).__name__}: {e}\n")
    _diag.flush()
    if "graph_store" in config:
        del config["graph_store"]
        memory = Memory.from_config(config)
        _diag.write("RUNNING IN VECTOR-ONLY MODE\n")
        _diag.flush()
    else:
        raise
_diag.close()

# Vector-only fallback instance (no graph store). Lazy-initialized on first
# graph failure so we don't waste memory if graph always works.
_vector_memory = None


def _get_vector_memory():
    global _vector_memory
    if _vector_memory is None:
        vec_config = build_mem0_config()
        vec_config.pop("graph_store", None)
        _vector_memory = Memory.from_config(vec_config)
    return _vector_memory


USER_ID = os.environ.get("MEM0_USER_ID", "default")

mcp = FastMCP("mem0-memory")


@mcp.tool()
def memory_add(content: str) -> str:
    """Store a memory. mem0 extracts facts, handles dedup and contradictions.

    Args:
        content: Natural language content to remember
    """
    import time
    last_err = None
    for attempt in range(3):
        try:
            result = memory.add(
                messages=[{"role": "user", "content": content}],
                user_id=USER_ID,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5)
                continue
    # graph memory failed 3x. fall back to vector-only save.
    try:
        vec_mem = _get_vector_memory()
        result = vec_mem.add(
            messages=[{"role": "user", "content": content}],
            user_id=USER_ID,
        )
        logger.warning("graph memory failed, saved via vector-only fallback")
        return json.dumps(result, default=str)
    except Exception as vec_err:
        # both paths failed, log everything
        import traceback
        try:
            with open("/data/mem0_debug.log", "a") as df:
                import datetime
                df.write(f"\n{datetime.datetime.utcnow().isoformat()} memory_add FAILED (graph 3x + vector)\n")
                df.write(f"graph error: {type(last_err).__name__}: {last_err}\n")
                df.write(f"vector error: {type(vec_err).__name__}: {vec_err}\n")
                df.write(traceback.format_exc())
                df.write("\n")
        except Exception:
            pass
        return f"Failed to store memory: {vec_err}"


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Search memories semantically. Returns relevant memories with scores.

    When graph memory is enabled, also returns entity relationships
    alongside vector results for richer context.

    Args:
        query: Natural language search query
        limit: Max results (default 5, max 20)
    """
    limit = max(1, min(limit, 20))
    try:
        results = memory.search(query=query, user_id=USER_ID, limit=limit, enable_graph=True)
        # Handle both dict and list response formats
        relations = []
        if isinstance(results, dict):
            items = results.get("results", [])
            relations = results.get("relations", [])
        elif isinstance(results, list):
            items = results
        else:
            items = []
        if not items and not relations:
            return "No matching memories found."
        lines = []
        for r in items:
            score = r.get("score", "?")
            text = r.get("memory", "")
            mid = r.get("id", "?")
            lines.append(f"[{score}] {text} (id: {mid})")
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
        # Handle both dict and list response formats
        if isinstance(results, dict):
            items = results.get("results", [])
        elif isinstance(results, list):
            items = results
        else:
            items = []
        if not items:
            return "No memories stored yet."
        lines = []
        for r in items[:limit]:
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
            event = c.get("event", "?")
            old = c.get("old_memory", "")
            new = c.get("new_memory", "")
            ts = c.get("created_at", "?")
            lines.append(f"[{event}] {old} -> {new} ({ts})")
        return "\n".join(lines)
    except Exception as e:
        logger.error("memory_history failed: %s", e)
        return f"History lookup failed: {e}"


@mcp.tool()
def memory_get(memory_id: str) -> str:
    """Get a specific memory by ID with full metadata.

    Args:
        memory_id: The memory ID to retrieve
    """
    try:
        result = memory.get(memory_id=memory_id)
        if not result:
            return "Memory not found."
        text = result.get("memory", "")
        mid = result.get("id", "?")
        created = result.get("created_at", "?")
        updated = result.get("updated_at", "?")
        return f"[{mid}] {text}\n  created: {created}\n  updated: {updated}"
    except Exception as e:
        logger.error("memory_get failed: %s", e)
        return f"Memory lookup failed: {e}"


@mcp.tool()
def memory_entities(query: str = "", limit: int = 10) -> str:
    """List known entities from the knowledge graph.

    Args:
        query: Optional filter query (empty = all entities)
        limit: Max results (default 10, max 50)
    """
    limit = max(1, min(limit, 50))
    try:
        results = memory.search(query=query or "entities", user_id=USER_ID, limit=limit, enable_graph=True)
        if isinstance(results, dict):
            relations = results.get("relations", [])
        else:
            relations = []
        if not relations:
            return "No entities found in knowledge graph."
        entities = set()
        for r in relations:
            src = r.get("source", "")
            dst = r.get("destination", "")
            if src:
                entities.add(src)
            if dst:
                entities.add(dst)
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
        results = memory.search(query=entity, user_id=USER_ID, limit=limit, enable_graph=True)
        if isinstance(results, dict):
            relations = results.get("relations", [])
        else:
            relations = []
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


if __name__ == "__main__":
    mcp.run(transport="stdio")

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
try:
    memory = Memory.from_config(config)
except Exception as e:
    # Graph store (Neo4j) auth failures crash the whole server.
    # Fall back to vector-only mode so tools still load.
    if "graph_store" in config:
        logger.warning("mem0 init failed with graph store (%s), retrying without it", e)
        del config["graph_store"]
        memory = Memory.from_config(config)
    else:
        raise
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
        logger.error("memory_add failed: %s (type: %s)", e, type(e).__name__)
        # log full traceback for API errors
        import traceback
        logger.error("traceback: %s", traceback.format_exc())
        return f"Failed to store memory: {e}"


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
        results = memory.search(query=query, user_id=USER_ID, limit=limit)
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
        results = memory.search(query=query or "entities", user_id=USER_ID, limit=limit)
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
        results = memory.search(query=entity, user_id=USER_ID, limit=limit)
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

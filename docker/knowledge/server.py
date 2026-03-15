"""FastMCP knowledge server with 4 tools wrapping ChromaDB.

Two-namespace architecture: system (rebuilt on deploy) + runtime (persists).
All logging to stderr to avoid corrupting MCP stdio protocol.
"""

import os
import sys
import logging
import chromadb
from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("knowledge")

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")

# PersistentClient for production, fallback to EphemeralClient for imports
try:
    client = chromadb.PersistentClient(path=CHROMA_PATH)
except Exception:
    client = chromadb.EphemeralClient()

system_col = client.get_or_create_collection("system", metadata={"hnsw:space": "cosine"})
runtime_col = client.get_or_create_collection("runtime", metadata={"hnsw:space": "cosine"})

mcp = FastMCP("knowledge")


@mcp.tool()
def knowledge_search(query: str, type: str = "", limit: int = 5) -> str:
    """Search the knowledge base semantically. Returns top matching chunks with similarity scores.

    Args:
        query: Natural language search query
        type: Optional filter by chunk type (fact, procedure, preference, integration, schema)
        limit: Max results (default 5, max 10)
    """
    limit = max(1, min(limit, 10))
    where_filter = {"type": type} if type else None

    results = []
    for col in [system_col, runtime_col]:
        try:
            kwargs = {"query_texts": [query], "n_results": limit}
            if where_filter:
                kwargs["where"] = where_filter
            r = col.query(**kwargs)
        except Exception as e:
            logger.warning("query failed on collection: %s", e)
            continue

        if r["documents"] and r["documents"][0]:
            for i, doc in enumerate(r["documents"][0]):
                dist = r["distances"][0][i] if r["distances"] else None
                meta = r["metadatas"][0][i] if r["metadatas"] else {}
                score = round(1 - dist, 3) if dist is not None else None
                results.append({
                    "text": doc,
                    "score": score,
                    "key": r["ids"][0][i],
                    "type": meta.get("type", "?"),
                    "refs": meta.get("refs", ""),
                })

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    results = results[:limit]

    if not results:
        return "No matching knowledge found."

    lines = []
    for r in results:
        lines.append(f"[{r.get('type', '?')}] (score: {r.get('score', '?')}) {r['key']}")
        lines.append(r["text"])
        if r.get("refs"):
            lines.append(f"  refs: {r['refs']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def knowledge_upsert(key: str, content: str, type: str, refs: str = "") -> str:
    """Write or update a knowledge chunk. Used for runtime facts, integrations, lessons.

    Args:
        key: Unique identifier for exact lookup (e.g., "integration.fireflies")
        content: The knowledge content to store
        type: Chunk type (fact, procedure, preference, integration, schema)
        refs: Comma-separated keys of related chunks
    """
    valid_types = ("procedure", "schema", "fact", "preference", "integration")
    if type not in valid_types:
        return f"Invalid type '{type}'. Must be one of: {', '.join(valid_types)}"

    try:
        runtime_col.upsert(
            ids=[key],
            documents=[content],
            metadatas=[{
                "type": type,
                "source": "runtime",
                "section": "",
                "namespace": "runtime",
                "refs": refs,
                "key": key,
            }],
        )
    except Exception as e:
        logger.error("upsert failed for key %s: %s", key, e)
        return f"Failed to store chunk '{key}': {e}"
    logger.info("upserted chunk: %s", key)
    return f"Stored knowledge chunk: {key}"


@mcp.tool()
def knowledge_get(key: str) -> str:
    """Get a specific chunk by exact key. Faster than semantic search when you know the key.

    Args:
        key: Exact chunk key (e.g., "system.tools.jobs", "integration.fireflies")
    """
    for col in [system_col, runtime_col]:
        got = col.get(ids=[key], include=["documents", "metadatas"])
        if got["ids"]:
            doc = got["documents"][0]
            meta = got["metadatas"][0] if got["metadatas"] else {}
            lines = [
                f"[{meta.get('type', '?')}] {key}",
                doc,
            ]
            if meta.get("refs"):
                lines.append(f"  refs: {meta['refs']}")
            return "\n".join(lines)

    return f"No chunk found with key: {key}"


@mcp.tool()
def knowledge_delete(key: str) -> str:
    """Delete a runtime knowledge chunk by key.

    Args:
        key: Exact chunk key to delete (only works on runtime chunks)
    """
    # Check system collection first - refuse deletion
    got = system_col.get(ids=[key])
    if got["ids"]:
        return f"Cannot delete system chunks (rebuilt on deploy). Key: {key}"

    # Check runtime collection
    got = runtime_col.get(ids=[key])
    if got["ids"]:
        runtime_col.delete(ids=[key])
        logger.info("deleted chunk: %s", key)
        return f"Deleted knowledge chunk: {key}"

    return f"No chunk found with key: {key}"


if __name__ == "__main__":
    mcp.run(transport="stdio")

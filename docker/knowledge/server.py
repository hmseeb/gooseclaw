"""FastMCP knowledge server with 3 tools wrapping ChromaDB.

System-only architecture: system collection rebuilt on deploy.
User memories are handled by mem0 (separate MCP server).
All chunks carry created_at/updated_at timestamps (ISO 8601 UTC).
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

mcp = FastMCP("knowledge")


@mcp.tool()
def knowledge_search(query: str, type: str = "", limit: int = 5, since: str = "") -> str:
    """Search the system knowledge base semantically. Returns top matching chunks with similarity scores.

    Args:
        query: Natural language search query
        type: Optional filter by chunk type (fact, procedure, preference, integration, schema)
        limit: Max results (default 5, max 10)
        since: Optional ISO date filter, e.g. "2026-03-17". Only returns chunks created on or after this date.
    """
    limit = max(1, min(limit, 10))
    where_filter = {"type": type} if type else None

    results = []
    try:
        kwargs = {"query_texts": [query], "n_results": limit}
        if where_filter:
            kwargs["where"] = where_filter
        r = system_col.query(**kwargs)
    except Exception as e:
        logger.warning("query failed: %s", e)
        return "No matching knowledge found."

    if r["documents"] and r["documents"][0]:
        for i, doc in enumerate(r["documents"][0]):
            dist = r["distances"][0][i] if r["distances"] else None
            meta = r["metadatas"][0][i] if r["metadatas"] else {}
            score = round(1 - dist, 3) if dist is not None else None
            created = meta.get("created_at", "")
            updated = meta.get("updated_at", "")

            # apply since filter: exclude chunks without timestamps or before cutoff
            if since:
                if not created or str(created) < since:
                    continue

            results.append({
                "text": doc,
                "score": score,
                "key": r["ids"][0][i],
                "type": meta.get("type", "?"),
                "refs": meta.get("refs", ""),
                "created_at": created,
                "updated_at": updated,
            })

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    results = results[:limit]

    if not results:
        return "No matching knowledge found."

    lines = []
    for r in results:
        ts_parts = []
        if r.get("created_at"):
            ts_parts.append(f"created: {r['created_at']}")
        if r.get("updated_at"):
            ts_parts.append(f"updated: {r['updated_at']}")
        ts_str = " | ".join(ts_parts)
        header = f"[{r.get('type', '?')}] (score: {r.get('score', '?')}) {r['key']}"
        if ts_str:
            header += f" ({ts_str})"
        lines.append(header)
        lines.append(r["text"])
        if r.get("refs"):
            lines.append(f"  refs: {r['refs']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def knowledge_get(key: str) -> str:
    """Get a specific system chunk by exact key. Faster than semantic search when you know the key.

    Args:
        key: Exact chunk key (e.g., "system.tools.jobs", "system.platform")
    """
    got = system_col.get(ids=[key], include=["documents", "metadatas"])
    if got["ids"]:
        doc = got["documents"][0]
        meta = got["metadatas"][0] if got["metadatas"] else {}
        header = f"[{meta.get('type', '?')}] {key}"
        ts_parts = []
        if meta.get("created_at"):
            ts_parts.append(f"created: {meta['created_at']}")
        if meta.get("updated_at"):
            ts_parts.append(f"updated: {meta['updated_at']}")
        if ts_parts:
            header += f" ({' | '.join(ts_parts)})"
        lines = [header, doc]
        if meta.get("refs"):
            lines.append(f"  refs: {meta['refs']}")
        return "\n".join(lines)

    return f"No chunk found with key: {key}"


@mcp.tool()
def knowledge_recent(limit: int = 5) -> str:
    """Get the most recently created or updated system knowledge chunks, sorted by time (newest first).

    Use this to answer "what did you store recently?" or "show me latest knowledge entries".

    Args:
        limit: Max results (default 5, max 20)
    """
    limit = max(1, min(limit, 20))

    all_entries = []
    try:
        # fetch all entries with metadata
        got = system_col.get(include=["documents", "metadatas"])
        if got["ids"]:
            for i, chunk_id in enumerate(got["ids"]):
                doc = got["documents"][i] if got["documents"] else ""
                meta = got["metadatas"][i] if got["metadatas"] else {}
                created = str(meta.get("created_at", ""))
                updated = str(meta.get("updated_at", ""))
                # use the most recent timestamp for sorting
                sort_ts = updated or created or ""
                all_entries.append({
                    "key": chunk_id,
                    "text": doc,
                    "type": meta.get("type", "?"),
                    "created_at": created,
                    "updated_at": updated,
                    "sort_ts": sort_ts,
                    "namespace": meta.get("namespace", "?"),
                })
    except Exception as e:
        logger.warning("failed to fetch from collection: %s", e)

    # sort by timestamp descending, entries without timestamps go last
    all_entries.sort(key=lambda x: x["sort_ts"] or "0000", reverse=True)
    all_entries = all_entries[:limit]

    if not all_entries:
        return "No knowledge entries found."

    lines = []
    for e in all_entries:
        ts_parts = []
        if e["created_at"]:
            ts_parts.append(f"created: {e['created_at']}")
        if e["updated_at"]:
            ts_parts.append(f"updated: {e['updated_at']}")
        ts_str = " | ".join(ts_parts) if ts_parts else "no timestamp"
        lines.append(f"[{e['type']}] [{e['namespace']}] {e['key']} ({ts_str})")
        # show first 200 chars of content for brevity
        preview = e["text"][:200].replace("\n", " ")
        if len(e["text"]) > 200:
            preview += "..."
        lines.append(f"  {preview}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")

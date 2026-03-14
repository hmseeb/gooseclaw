"""FastMCP knowledge server - stub for TDD RED phase."""

import os
import sys
import logging
import chromadb
from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("knowledge")

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")

# Use EphemeralClient for import safety (tests will monkey-patch these)
try:
    client = chromadb.PersistentClient(path=CHROMA_PATH)
except Exception:
    client = chromadb.EphemeralClient()

system_col = client.get_or_create_collection("system")
runtime_col = client.get_or_create_collection("runtime")

mcp = FastMCP("knowledge")


@mcp.tool()
def knowledge_search(query: str, type: str = "", limit: int = 5) -> str:
    """Search the knowledge base semantically."""
    raise NotImplementedError("stub")


@mcp.tool()
def knowledge_upsert(key: str, content: str, type: str, refs: str = "") -> str:
    """Write or update a knowledge chunk."""
    raise NotImplementedError("stub")


@mcp.tool()
def knowledge_get(key: str) -> str:
    """Get a specific chunk by exact key."""
    raise NotImplementedError("stub")


@mcp.tool()
def knowledge_delete(key: str) -> str:
    """Delete a runtime knowledge chunk by key."""
    raise NotImplementedError("stub")


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""Shared mem0 config builder. Used by memory MCP server and (future) gateway.

Reads setup.json from CONFIG_DIR to determine the user's LLM provider,
then builds a mem0 config dict with ChromaDB vector store, HuggingFace
local embedder, and a cheap extraction model.

mem0's anthropic adapter is fundamentally broken with the current API
(tool format, tool_choice, ToolUseBlock handling). We use Groq as the
extraction model instead — it's free tier, fast, and mem0's OpenAI-
compatible adapter works correctly with it.
"""

import os
import json

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/data/config")
SETUP_FILE = os.path.join(CONFIG_DIR, "setup.json")
VAULT_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "secrets", "vault.yaml")

# Groq is the universal extraction model for mem0. It's free, fast,
# and uses OpenAI-compatible API which mem0 actually supports properly.
# mem0's anthropic adapter has 4+ incompatibilities with the current API.
MEM0_LLM_PROVIDER = "groq"
MEM0_LLM_MODEL = "llama-3.1-70b-versatile"


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


def _read_vault_secret(key):
    """Read a single secret from the vault file."""
    try:
        import yaml
        if os.path.exists(VAULT_PATH):
            with open(VAULT_PATH) as f:
                vault = yaml.safe_load(f) or {}
            return vault.get(key, "")
    except Exception:
        pass
    return ""


def build_mem0_config():
    """Build mem0 config dict from setup.json and environment variables."""
    # Get Groq API key: env var > vault > setup.json saved_keys
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        api_key = _read_vault_secret("groq_api_key") or _read_vault_secret("GROQ_API_KEY")
    if not api_key:
        setup = _load_setup()
        if setup:
            saved = setup.get("saved_keys", {})
            api_key = saved.get("groq", "") if isinstance(saved, dict) else ""

    llm_config = {
        "model": MEM0_LLM_MODEL,
        "max_tokens": 2000,
    }
    if api_key:
        llm_config["api_key"] = api_key

    config = {
        "vector_store": {
            "provider": "chroma",
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
            "provider": MEM0_LLM_PROVIDER,
            "config": llm_config,
        },
        "version": "v1.1",
    }

    # Graph store (Neo4j) - enabled when entrypoint confirms neo4j is ready
    if os.environ.get("MEM0_ENABLE_GRAPH", "").lower() in ("true", "1", "yes"):
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": os.environ.get("NEO4J_URL", "bolt://localhost:7687"),
                "username": os.environ.get("NEO4J_USERNAME", "neo4j"),
                "password": os.environ.get("NEO4J_PASSWORD", "neo4j"),
            }
        }

    return config

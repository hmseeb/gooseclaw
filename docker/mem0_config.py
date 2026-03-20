"""Shared mem0 config builder. Used by memory MCP server and (future) gateway.

Reads setup.json from CONFIG_DIR to determine the user's LLM provider,
then builds a mem0 config dict with ChromaDB vector store, HuggingFace
local embedder, and a cheap extraction model for the detected provider.
"""

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

    # For litellm provider (openrouter), need api_key
    if provider == "openrouter":
        llm_config["api_key"] = os.environ.get("OPENROUTER_API_KEY", "")

    config = {
        "vector_store": {
            "provider": "chroma",
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

    # Graph store (Neo4j) - optional, enabled when NEO4J_ENABLED is set by entrypoint
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

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
    "claude-code": "anthropic",
}

# Cheap extraction models per provider (CFG-03)
# Verified March 2026 — cheapest model with tool-use support per provider
CHEAP_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",       # $0.80/$4.00 per 1M tokens
    "claude-code": "claude-haiku-4-5-20251001",      # same as anthropic
    "openai": "gpt-4.1-nano",                        # $0.10/$0.40 per 1M tokens
    "google": "gemini-2.0-flash-lite",               # $0.075/$0.30 per 1M tokens
    "groq": "llama-3.3-70b-versatile",               # free tier, $0.59/$0.79
    "ollama": "llama3.2",                            # local, free
    "deepseek": "deepseek-chat",                     # $0.014/$0.028 per 1M tokens (V3.2)
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",  # $0.88 per 1M tokens
    "litellm": "gpt-4.1-nano",                      # default passthrough
    "openrouter": "anthropic/claude-haiku-4-5-20251001",  # cheapest anthropic on OR
    "azure-openai": "gpt-4o-mini",                   # $0.15/$0.60 per 1M tokens
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
    "claude-code": "CLAUDE_CODE_OAUTH_TOKEN",
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


VAULT_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "secrets", "vault.yaml")


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


_anthropic_patched = False


def _patch_anthropic_top_p():
    """Patch anthropic SDK to strip top_p when temperature is also present.

    mem0 defaults both to 0.1, but newer Anthropic models reject having both.
    Patches at the SDK level to strip top_p from every create() call.
    """
    global _anthropic_patched
    if _anthropic_patched:
        return
    try:
        import anthropic.resources.messages
        _orig_create = anthropic.resources.messages.Messages.create

        def _patched_create(self, **kwargs):
            if "temperature" in kwargs and "top_p" in kwargs:
                kwargs.pop("top_p")
            # mem0 passes tool_choice as a string but Anthropic API
            # now requires a dict like {"type": "auto"}
            tc = kwargs.get("tool_choice")
            if isinstance(tc, str):
                kwargs["tool_choice"] = {"type": tc}
            # mem0 sends tools in OpenAI format {"type": "function", "function": {...}}
            # but Anthropic expects {"name": ..., "input_schema": ...}
            tools = kwargs.get("tools")
            if tools and isinstance(tools, list):
                fixed = []
                for t in tools:
                    if isinstance(t, dict) and t.get("type") == "function" and "function" in t:
                        fn = t["function"]
                        fixed.append({
                            "name": fn.get("name", ""),
                            "description": fn.get("description", ""),
                            "input_schema": fn.get("parameters", {}),
                        })
                    else:
                        fixed.append(t)
                kwargs["tools"] = fixed
            return _orig_create(self, **kwargs)

        anthropic.resources.messages.Messages.create = _patched_create
        _anthropic_patched = True
    except (ImportError, AttributeError):
        pass


def build_mem0_config():
    """Build mem0 config dict from setup.json and environment variables."""
    setup = _load_setup()
    provider = (setup.get("provider_type", "anthropic") if setup else "anthropic")
    mem0_provider = PROVIDER_MAP.get(provider, "openai")
    cheap_model = CHEAP_MODELS.get(provider, "gpt-4.1-nano")

    # Build LLM config
    llm_config = {
        "model": cheap_model,
        "max_tokens": 2000,
    }

    # WORKAROUND: mem0's anthropic client sends both temperature AND top_p
    # which newer anthropic models reject. Patch to strip top_p before API call.
    if mem0_provider == "anthropic":
        _patch_anthropic_top_p()

    # Set API key. claude-code uses OAuth tokens that don't work with the
    # Anthropic API directly. Check vault secrets as fallback.
    env_key = PROVIDER_ENV_KEYS.get(provider)
    api_key = ""
    if env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key and setup:
        api_key = setup.get("api_key", "")
    # claude-code OAuth tokens can't call Anthropic API directly.
    # fall back to vault's ANTHROPIC_SECRET_KEY or ANTHROPIC_API_KEY.
    if provider == "claude-code" or not api_key:
        vault_key = _read_vault_secret("ANTHROPIC_SECRET_KEY") or \
                     _read_vault_secret("ANTHROPIC_API_KEY") or \
                     os.environ.get("ANTHROPIC_API_KEY", "") or \
                     os.environ.get("ANTHROPIC_SECRET_KEY", "")
        if vault_key:
            api_key = vault_key
    # Debug: write config diagnostics to file (MCP stderr doesn't reach Railway logs)
    _debug_path = "/data/mem0_debug.log"
    try:
        key_preview = (api_key[:8] + "...") if api_key and len(api_key) > 8 else "NONE"
        with open(_debug_path, "a") as _df:
            import datetime
            _df.write(f"{datetime.datetime.utcnow().isoformat()} provider={provider} mem0_provider={mem0_provider} model={cheap_model} key={key_preview} graph={'graph_store' in config if 'graph_store' in dir() else 'N/A'}\n")
    except Exception:
        pass
    if api_key:
        llm_config["api_key"] = api_key

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

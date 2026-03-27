"""Shared mem0 config builder for memory MCP server.

Reads setup.json to determine the mem0 LLM provider and model.
Falls back to Groq if not configured.
"""

import os
import json

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/data/config")
SETUP_FILE = os.path.join(CONFIG_DIR, "setup.json")
VAULT_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "secrets", "vault.yaml")

# Default extraction model (used when setup.json has no mem0_provider)
DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Maps mem0 provider names to mem0's internal provider names
PROVIDER_MAP = {
    "groq": "groq",
    "openai": "openai",
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "together": "together",
    "openrouter": "openai",  # uses openai SDK with custom base_url
}

# Maps provider to env var name for API key
KEY_ENV_VARS = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Maps provider to vault key names to check
VAULT_KEYS = {
    "groq": ["groq_api_key", "GROQ_API_KEY"],
    "openai": ["openai_api_key", "OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_SECRET_KEY", "ANTHROPIC_API_KEY", "anthropic_api_key"],
    "deepseek": ["deepseek_api_key", "DEEPSEEK_API_KEY"],
    "together": ["together_api_key", "TOGETHER_API_KEY"],
    "openrouter": ["openrouter_api_key", "OPENROUTER_API_KEY"],
}


def _load_setup():
    for path in (SETUP_FILE, SETUP_FILE + ".bak"):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _read_vault_secret(key):
    try:
        import yaml
        if os.path.exists(VAULT_PATH):
            with open(VAULT_PATH) as f:
                vault = yaml.safe_load(f) or {}
            # flat key lookup (e.g. "groq_api_key")
            val = vault.get(key, "")
            if val and isinstance(val, str):
                return val
            # nested lookup (e.g. "groq_api_key" → vault["groq"]["api_key"])
            key_lower = key.lower()
            for service, values in vault.items():
                if isinstance(values, dict):
                    for subkey, subval in values.items():
                        flat = f"{service}_{subkey}".lower()
                        if flat == key_lower and subval:
                            return str(subval)
    except Exception:
        pass
    return ""


def _find_api_key(provider, setup):
    """Find API key for provider from env var, vault, or setup.json."""
    # 0. explicit mem0_api_key from setup.json (set via dashboard)
    if setup:
        val = setup.get("mem0_api_key", "")
        if val:
            return val

    # 1. env var
    env_key = KEY_ENV_VARS.get(provider, "")
    if env_key:
        val = os.environ.get(env_key, "")
        if val:
            return val

    # 2. vault
    for vk in VAULT_KEYS.get(provider, []):
        val = _read_vault_secret(vk)
        if val:
            return val

    # 3. setup.json groq_extraction_key (legacy)
    if provider == "groq" and setup:
        val = setup.get("groq_extraction_key", "")
        if val:
            return val

    # 4. setup.json saved_keys
    if setup:
        saved = setup.get("saved_keys", {})
        if isinstance(saved, dict):
            val = saved.get(provider, "")
            if val:
                return val

    # 5. for anthropic, also check main api_key if provider_type is anthropic
    if provider == "anthropic" and setup:
        if setup.get("provider_type") == "anthropic":
            val = setup.get("api_key", "")
            if val:
                return val

    return ""


_groq_patched = False


def _patch_groq_xml():
    """Patch Groq SDK to fix malformed XML function calls from llama models."""
    global _groq_patched
    if _groq_patched:
        return
    try:
        import re
        from groq.resources.chat import completions as _comp
        _orig = _comp.Completions.create

        def _patched(self, **kwargs):
            resp = _orig(self, **kwargs)
            if resp.choices:
                for choice in resp.choices:
                    msg = choice.message
                    if msg and msg.content and "</function>" in msg.content:
                        msg.content = re.sub(
                            r'([^>])\s*</function>',
                            r'\1></function>',
                            msg.content,
                        )
            return resp

        _comp.Completions.create = _patched
        _groq_patched = True
    except (ImportError, AttributeError):
        pass


_anthropic_patched = False


def _patch_anthropic():
    """Patch Anthropic SDK for mem0 compatibility issues."""
    global _anthropic_patched
    if _anthropic_patched:
        return
    try:
        import json as _json
        import anthropic.resources.messages
        _orig_create = anthropic.resources.messages.Messages.create

        def _patched_create(self, **kwargs):
            # Strip top_p when temperature present
            if "temperature" in kwargs and "top_p" in kwargs:
                kwargs.pop("top_p")
            # tool_choice string -> dict
            tc = kwargs.get("tool_choice")
            if isinstance(tc, str):
                kwargs["tool_choice"] = {"type": tc}
            # OpenAI tool format -> Anthropic format
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
            response = _orig_create(self, **kwargs)
            # ToolUseBlock -> TextBlock
            if response.content:
                from anthropic.types import TextBlock
                new_content = []
                for block in response.content:
                    if hasattr(block, "text"):
                        new_content.append(block)
                    elif hasattr(block, "input"):
                        new_content.append(TextBlock(
                            type="text",
                            text=_json.dumps(block.input) if isinstance(block.input, dict) else str(block.input),
                        ))
                    else:
                        new_content.append(block)
                response.content = new_content
            return response

        anthropic.resources.messages.Messages.create = _patched_create
        _anthropic_patched = True
    except (ImportError, AttributeError):
        pass


def build_mem0_config_for_provider(provider, model):
    """Build mem0 config for a specific provider/model (used by fallback chain).

    Returns a config dict suitable for Memory.from_config(), or None if the
    provider is unsupported or has no API key.
    """
    if provider not in PROVIDER_MAP:
        return None

    setup = _load_setup()
    api_key = _find_api_key(provider, setup)
    if not api_key:
        return None

    mem0_provider = PROVIDER_MAP[provider]

    # Apply provider-specific patches
    if provider == "groq":
        _patch_groq_xml()
    elif provider == "anthropic":
        _patch_anthropic()

    llm_config = {
        "model": model,
        "max_tokens": 2000,
    }
    llm_config["api_key"] = api_key

    # OpenRouter uses OpenAI SDK with custom base URL
    if provider == "openrouter":
        os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
        os.environ.setdefault("OPENAI_API_KEY", api_key)

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
            "provider": mem0_provider,
            "config": llm_config,
        },
        "version": "v1.1",
    }

    # Graph store (Kuzu) if enabled
    if os.environ.get("MEM0_ENABLE_GRAPH", "").lower() in ("true", "1", "yes"):
        config["graph_store"] = {
            "provider": "kuzu",
            "config": {
                "db": os.environ.get("MEM0_KUZU_PATH", "/data/knowledge/kuzu"),
            }
        }

    return config


def build_mem0_config():
    """Build mem0 config dict from setup.json and environment variables."""
    setup = _load_setup()

    # Read mem0 provider/model from setup.json, fallback to defaults
    provider = DEFAULT_PROVIDER
    model = DEFAULT_MODEL
    if setup:
        provider = setup.get("mem0_provider", "") or DEFAULT_PROVIDER
        model = setup.get("mem0_model", "") or DEFAULT_MODEL

    mem0_provider = PROVIDER_MAP.get(provider, provider)

    # Apply provider-specific patches
    if provider == "groq":
        _patch_groq_xml()
    elif provider == "anthropic":
        _patch_anthropic()

    # Find API key
    api_key = _find_api_key(provider, setup)

    llm_config = {
        "model": model,
        "max_tokens": 2000,
    }
    if api_key:
        llm_config["api_key"] = api_key
    # OpenRouter uses OpenAI SDK with custom base URL.
    # Set env var directly — more reliable than config keys.
    if provider == "openrouter":
        os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
        os.environ.setdefault("OPENAI_API_KEY", api_key or "")

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
            "provider": mem0_provider,
            "config": llm_config,
        },
        "version": "v1.1",
    }

    # Graph store (Kuzu — embedded, no separate service needed)
    if os.environ.get("MEM0_ENABLE_GRAPH", "").lower() in ("true", "1", "yes"):
        config["graph_store"] = {
            "provider": "kuzu",
            "config": {
                "db": os.environ.get("MEM0_KUZU_PATH", "/data/knowledge/kuzu"),
            }
        }

    return config

# Phase 26: Fallback Provider System - Research

**Researched:** 2026-03-25
**Domain:** LLM provider resilience, retry/fallback patterns, Python error handling
**Confidence:** HIGH

## Summary

This phase adds ordered fallback chains for both the main LLM (goose agent) and the mem0 extraction LLM. When the primary provider fails (rate limits, timeouts, 5xx errors), the system auto-switches to the next provider in the user-configured chain. This touches four files: `gateway.py` (relay error detection + provider switching), `mem0_config.py` (fallback chain for extraction), `entrypoint.sh` (env var hydration for fallback config), and `setup.html` (UI for configuring fallback order in both setup wizard and post-setup dashboard).

The codebase already has the building blocks: `_relay_to_goosed()` returns `(text, error, media)` tuples where errors include timeout and HTTP status info. `_update_goose_session_provider()` can hot-swap a session's provider via `/agent/update_provider`. `_do_rest_relay()` catches `socket.timeout`, `ConnectionError`, and general exceptions, returning structured error strings. The mem0 path uses `_mem0_add_with_timeout()` with a `ThreadPoolExecutor` that catches `TimeoutError` and general exceptions. All error surfaces exist. What's missing is the fallback logic that intercepts these errors and tries the next provider.

**Primary recommendation:** Build a `FallbackChain` class (pure Python, no external deps) that holds an ordered list of provider configs, detects retriable errors (429, 5xx, timeout), and yields the next provider. Wire it into both `_relay_to_goosed()` (for main LLM) and `_mem0_add_with_timeout()` (for extraction). Store fallback config in `setup.json` as `fallback_providers` (main LLM) and `mem0_fallback_providers` (extraction). Add UI to both the setup wizard step 3 and the post-setup dashboard.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.13 | Retry logic, error detection, threading | Already used exclusively in gateway.py. Zero new deps. |
| setup.json | N/A | Fallback chain persistence | Existing config store for all provider settings |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `time.sleep` | stdlib | Backoff between retries | Brief pause before trying next provider |
| `threading.Lock` | stdlib | Thread-safe fallback state | Protecting shared failure counters |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Custom FallbackChain | tenacity library | Tenacity is overkill here. We need provider switching, not just retries. Custom is ~60 lines and avoids a new pip dependency in the container. |
| In-process state | Redis circuit breaker | Single container, single process. Redis is overkill. Thread-safe dict is sufficient. |
| litellm fallbacks | litellm.Router | Would require litellm as a dependency and restructure how goosed is called. Too invasive. |

**Installation:**
```bash
# No new dependencies needed. All stdlib.
```

## Architecture Patterns

### Recommended Project Structure
```
docker/
  gateway.py          # FallbackChain class + wiring into relay + save/load
  mem0_config.py      # Fallback chain for mem0 extraction LLM
  entrypoint.sh       # Hydrate fallback_providers from setup.json
  setup.html          # UI for fallback config (wizard step 3 + dashboard)
```

### Pattern 1: Fallback Chain with Error Classification
**What:** An ordered list of provider configs. On retriable error, advance to next. On non-retriable error (401 auth, 400 bad request), stop.
**When to use:** Every outbound LLM call (main relay and mem0 extraction).
**Example:**
```python
# Fallback chain data model in setup.json
{
    "fallback_providers": [
        {"provider": "anthropic", "model": "claude-sonnet-4-5"},
        {"provider": "openai", "model": "gpt-4o"},
        {"provider": "groq", "model": "llama-3.3-70b-versatile"}
    ],
    "mem0_fallback_providers": [
        {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "openai", "model": "gpt-4.1-nano"}
    ]
}
```

### Pattern 2: Error Classification Function
**What:** Classifies errors into retriable vs. permanent. Retriable: 429 (rate limit), 5xx (server error), timeout, connection error. Permanent: 401 (auth), 400 (bad request), 403 (forbidden).
**When to use:** Before deciding whether to try the next fallback provider.
**Example:**
```python
def _is_retriable_error(error_string):
    """Classify whether an error warrants trying the next fallback provider."""
    if not error_string:
        return False
    low = error_string.lower()
    # Rate limits
    if "429" in error_string or "rate limit" in low:
        return True
    # Server errors (5xx)
    if any(f" {code}" in error_string for code in ("500", "502", "503", "504")):
        return True
    # Timeouts
    if "timeout" in low or "timed out" in low:
        return True
    # Connection failures
    if "connection" in low and ("error" in low or "refused" in low or "reset" in low):
        return True
    return False
```

### Pattern 3: Main LLM Fallback via update_provider
**What:** When `_relay_to_goosed()` gets a retriable error, call `_update_goose_session_provider()` with the next fallback provider, then retry the relay.
**When to use:** In `_relay_to_goosed()` after the initial relay fails.
**Example:**
```python
def _relay_to_goosed(user_text, session_id, ...):
    # ... existing logic: initial relay attempt ...
    text, err, media = relay_fn(user_text, session_id)

    if err and _is_retriable_error(err) and chat_id:
        # Try fallback providers
        setup = load_setup()
        fallbacks = setup.get("fallback_providers", []) if setup else []
        for fb in fallbacks:
            _gateway_log.info(f"fallback: trying {fb['provider']}/{fb['model']}")
            _update_goose_session_provider(session_id, fb)
            text, err, media = relay_fn(user_text, session_id)
            if not err or not _is_retriable_error(err):
                break

    # ... existing error handling (fatal provider error, new session creation) ...
    return text, err, media
```

### Pattern 4: mem0 Fallback via Config Rebuild
**What:** When `_mem0_add_knowledge()` fails with a retriable error, rebuild the mem0 config with the next fallback provider and reinitialize.
**When to use:** In `_mem0_add_with_timeout()` after the primary mem0 call fails.
**Example:**
```python
def _mem0_add_with_timeout(messages, user_id="default", timeout=60):
    """Run mem0.add() with fallback chain."""
    result = _try_mem0_add(messages, user_id, timeout)
    if result is not None:
        return result

    # Primary failed, try fallback providers
    setup = _load_setup()  # from mem0_config module
    fallbacks = (setup or {}).get("mem0_fallback_providers", [])
    for fb in fallbacks:
        _memory_log.info(f"mem0 fallback: trying {fb['provider']}/{fb['model']}")
        _reinit_mem0_with_provider(fb["provider"], fb["model"])
        result = _try_mem0_add(messages, user_id, timeout)
        if result is not None:
            return result

    return None
```

### Anti-Patterns to Avoid
- **Infinite retry loops:** Always bound the fallback chain to the user-configured list. Never retry the same provider.
- **Retrying auth errors:** 401/403 errors mean the API key is wrong. Trying another provider with the same bad key accomplishes nothing. Classify these as permanent.
- **Blocking the main thread with backoff:** The gateway is single-threaded HTTP. Sleeping for exponential backoff during relay would block all other requests. Use minimal delay (0.5-1s) or no delay between fallback attempts since we're switching providers, not retrying the same one.
- **Global mem0 singleton mutation without locking:** The `_mem0_instance` is shared. If fallback changes the provider, use a lock and be prepared to reset it back.
- **Persisting fallback state across restarts:** The "currently active fallback" should NOT be persisted. On restart, always start from the primary. Fallback is a transient resilience mechanism.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Provider credential lookup | Custom key resolver per fallback | Reuse existing `_find_api_key()` in mem0_config.py and env_map in gateway.py | Already handles env vars, vault, setup.json, saved_keys. Just pass the provider name. |
| Goose provider hot-swap | Direct HTTP calls to goosed | Reuse `_update_goose_session_provider()` | Already handles retry, vault sync, error logging. |
| Config validation | New validation logic | Extend existing `validate_setup_config()` | Consistent error format, already called from handle_save. |
| UI drag-and-drop | Custom drag-and-drop JS | HTML5 native drag-and-drop with `draggable` attribute | Minimal JS, works everywhere, matches existing minimal-JS pattern in setup.html. |
| Config persistence | New file format | Extend existing setup.json via `save_setup()` / `load_setup()` | Atomic writes, backup, already wired into the save flow. |

**Key insight:** The codebase already has all the primitives (provider switching, error detection, config persistence, UI patterns). This phase is about wiring them together with a fallback chain, not building new infrastructure.

## Common Pitfalls

### Pitfall 1: Stale mem0 Instance After Fallback
**What goes wrong:** `_mem0_instance` is a module-level singleton initialized once. If you change the provider for fallback, the old instance still points to the failed provider.
**Why it happens:** mem0's `Memory.from_config()` bakes the LLM provider into the instance at creation time.
**How to avoid:** After successful fallback, either: (a) create a new `Memory` instance with the fallback config, or (b) reset `_mem0_instance = None` and let `_get_mem0()` reinitialize. Option (b) is simpler but means the primary provider won't be retried until a full reinit (which is fine -- restart recovers it).
**Warning signs:** mem0 fallback succeeds once but subsequent calls still fail because the singleton wasn't updated.

### Pitfall 2: Fallback During Streaming Relay
**What goes wrong:** `_do_rest_relay_streaming()` sends chunks to the user via `flush_cb` as they arrive. If the provider fails mid-stream, partial output has already been sent.
**Why it happens:** Streaming SSE doesn't buffer the full response.
**How to avoid:** For the main LLM, only attempt fallback when the ENTIRE relay fails (error returned). If partial content was received + error, skip fallback (the user already saw partial output). The `_relay_to_goosed()` function already handles this correctly since it checks `err` after the relay returns.
**Warning signs:** User sees partial response from provider A, then gets a full response from provider B appended to it.

### Pitfall 3: API Key Cross-Contamination Between Providers
**What goes wrong:** Switching providers requires different API keys in environment variables. Setting `OPENAI_API_KEY` for provider B might overwrite the key needed for provider A's extensions.
**Why it happens:** Some providers (e.g., openrouter) set `OPENAI_API_KEY` as a side effect.
**How to avoid:** For main LLM fallback, use `_update_goose_session_provider()` which passes credentials to goosed's internal API, not via env vars. For mem0 fallback, pass the API key directly in the config dict to `Memory.from_config()`, not via env vars.
**Warning signs:** After fallback triggers, the primary provider stops working even when it recovers.

### Pitfall 4: Fallback Chain Includes Providers Without Keys
**What goes wrong:** User configures fallback to "openai" but hasn't entered an OpenAI API key. Fallback attempt fails immediately with 401.
**Why it happens:** UI allows selecting any provider for fallback without checking credentials.
**How to avoid:** In the UI, either (a) only show providers that have saved_keys entries, or (b) validate at save time that each fallback provider has an API key in saved_keys, vault, or env vars. Option (a) is better UX. Option (b) is the safety net.
**Warning signs:** Fallback always fails with auth errors. Logs show 401 for every fallback attempt.

### Pitfall 5: Drag-and-Drop State Diverging from Saved State
**What goes wrong:** User reorders the fallback chain via drag-and-drop but doesn't click Save. The UI shows the new order but setup.json has the old order.
**Why it happens:** Drag-and-drop events update the DOM but not the backend.
**How to avoid:** The existing setup.html pattern is to collect all values at save time from the DOM. Follow the same pattern: read fallback order from the DOM elements at save time, not from a JS variable that tracks drag events.
**Warning signs:** Fallback order resets after page refresh.

## Code Examples

### Error Classification (to add to gateway.py)
```python
# Source: derived from existing error handling in _relay_to_goosed() and _diagnose_empty_response()

# HTTP status codes that indicate a retriable failure
_RETRIABLE_STATUS_CODES = {"429", "500", "502", "503", "504", "529"}

def _is_retriable_provider_error(error_string):
    """Check if an error from goosed/mem0 should trigger fallback to next provider.

    Retriable: rate limits (429), server errors (5xx), timeouts, connection errors.
    NOT retriable: auth errors (401/403), bad requests (400), broken pipe (needs restart).
    """
    if not error_string:
        return False
    low = error_string.lower()

    # Check for retriable HTTP status codes
    for code in _RETRIABLE_STATUS_CODES:
        if code in error_string:
            return True

    # Timeouts
    if "timeout" in low or "timed out" in low or "took too long" in low:
        return True

    # Connection errors (transient)
    if "connection" in low and any(w in low for w in ("refused", "reset", "error", "aborted")):
        return True

    # NOT retriable: auth failures, broken pipe (handled separately by existing code)
    return False
```

### Fallback Chain Data Model (setup.json additions)
```python
# Source: pattern matching existing models array and channel_routes in setup.json

# New fields in setup.json:
# "fallback_providers": [
#     {"provider": "openai", "model": "gpt-4o"},
#     {"provider": "groq", "model": "llama-3.3-70b-versatile"}
# ]
# "mem0_fallback_providers": [
#     {"provider": "openai", "model": "gpt-4.1-nano"},
#     {"provider": "deepseek", "model": "deepseek-chat"}
# ]
```

### Validation Extension (add to validate_setup_config)
```python
# Source: pattern matching existing models array validation at line 2242

# Fallback providers validation
for field_name in ("fallback_providers", "mem0_fallback_providers"):
    fb_list = config.get(field_name)
    if fb_list is not None:
        if not isinstance(fb_list, list):
            errors.append(f"{field_name} must be an array")
        else:
            for i, fb in enumerate(fb_list):
                if not isinstance(fb, dict):
                    errors.append(f"{field_name}[{i}] must be an object")
                    continue
                if not fb.get("provider"):
                    errors.append(f"{field_name}[{i}] missing provider")
                elif fb["provider"] not in env_map:
                    errors.append(f"{field_name}[{i}] unknown provider: {fb['provider']!r}")
                if not fb.get("model"):
                    errors.append(f"{field_name}[{i}] missing model")
```

### Main LLM Fallback Integration Point
```python
# Source: existing _relay_to_goosed() at gateway.py line 7297
# Insert AFTER line 7327 (initial relay) and BEFORE existing retry logic (line 7329)

def _try_fallback_providers(relay_fn, user_text, session_id, error_string):
    """Attempt fallback providers when primary LLM fails with retriable error."""
    if not _is_retriable_provider_error(error_string):
        return None  # Not a retriable error

    setup = load_setup()
    if not setup:
        return None

    fallbacks = setup.get("fallback_providers", [])
    if not fallbacks:
        return None

    for fb in fallbacks:
        provider = fb.get("provider", "")
        model = fb.get("model", "")
        if not provider or not model:
            continue

        _gateway_log.info(f"fallback: trying {provider}/{model} after error: {error_string[:100]}")
        _update_goose_session_provider(session_id, {
            "provider": provider,
            "model": model,
            "id": f"fallback_{provider}_{model}",
        })

        text, err, media = relay_fn(user_text, session_id)
        if not err:
            _gateway_log.info(f"fallback succeeded with {provider}/{model}")
            return (text, err, media)

        if not _is_retriable_provider_error(err):
            _gateway_log.warning(f"fallback {provider}/{model} failed with non-retriable error: {err[:100]}")
            break  # Don't try more fallbacks for non-retriable errors

    return None  # All fallbacks exhausted
```

### UI Pattern: Drag-and-Drop Sortable List
```html
<!-- Source: matches existing setup.html card-based UI patterns -->
<!-- Fallback provider list with drag-to-reorder -->
<div id="fallbackProviderList" class="fallback-list">
    <!-- Each item is draggable -->
    <div class="fallback-item" draggable="true" data-provider="openai" data-model="gpt-4o">
        <span class="drag-handle">&#9776;</span>
        <span class="fb-provider">OpenAI</span>
        <span class="fb-model">gpt-4o</span>
        <button class="fb-remove" onclick="removeFallback(this)">&times;</button>
    </div>
</div>
<button onclick="addFallbackProvider()">+ Add Fallback</button>
```

```javascript
// Source: HTML5 native drag-and-drop (no library needed)
// Matches existing setup.html vanilla JS pattern (no frameworks)
let dragSrc = null;
function handleDragStart(e) {
    dragSrc = this;
    e.dataTransfer.effectAllowed = 'move';
    this.classList.add('dragging');
}
function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
}
function handleDrop(e) {
    e.preventDefault();
    if (dragSrc !== this) {
        const list = this.parentNode;
        const items = [...list.children];
        const fromIdx = items.indexOf(dragSrc);
        const toIdx = items.indexOf(this);
        if (fromIdx < toIdx) {
            list.insertBefore(dragSrc, this.nextSibling);
        } else {
            list.insertBefore(dragSrc, this);
        }
    }
    dragSrc.classList.remove('dragging');
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Single provider, manual reconfigure on failure | Ordered fallback chain with auto-switch | This phase | Users don't lose conversations when a provider goes down |
| mem0 extraction fails silently | mem0 extraction falls back to alternate provider | This phase | Memory extraction survives provider outages |
| No error classification | Retriable vs. permanent error distinction | This phase | Only appropriate errors trigger fallback |

**Existing goose "lead/worker" is NOT the same thing:**
Goose has `GOOSE_LEAD_PROVIDER` and `GOOSE_LEAD_FAILURE_THRESHOLD` for a two-tier model strategy (use expensive model for first N turns, then switch to cheaper worker). This is a cost optimization feature, not a resilience feature. The fallback provider system is about surviving provider outages, which is orthogonal.

## Open Questions

1. **Should fallback restore to primary automatically?**
   - What we know: After fallback triggers, the session uses the fallback provider. On next message, should it try primary again?
   - What's unclear: Goose sessions persist provider config. Switching back requires another `update_provider` call.
   - Recommendation: Yes, always try primary first on each new message. The session-level provider switch is cheap (one HTTP call). This ensures fallback is transient, not sticky. Implement by always calling `_update_goose_session_provider()` with the primary before relay, and only falling back on error.

2. **Should mem0 fallback be persistent or per-call?**
   - What we know: `_mem0_instance` is a singleton. Reinitializing it for fallback means creating a new `Memory` object.
   - What's unclear: Is `Memory.from_config()` expensive? Does it reload embeddings?
   - Recommendation: Make it per-call for now. `Memory.from_config()` is lightweight (just sets config, doesn't preload anything). If performance is an issue, cache fallback instances too.

3. **How to handle saved_keys for fallback providers?**
   - What we know: The existing `saved_keys` dict in setup.json stores per-provider API keys. The fallback UI needs to know which providers have keys.
   - What's unclear: Should the fallback UI allow entering new API keys inline, or require they be configured via the main provider selector first?
   - Recommendation: Show only providers that have keys in `saved_keys` (or env vars). This is simpler UX and avoids duplicating the key-entry flow. The user configures providers normally, then orders them for fallback.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | `docker/pytest.ini` |
| Quick run command | `cd /Users/haseeb/nix-template/docker && python -m pytest tests/ -x -q --timeout=30` |
| Full suite command | `cd /Users/haseeb/nix-template/docker && python -m pytest tests/ -q --timeout=30` |
| Estimated runtime | ~10 seconds |

### Phase Requirements -> Test Map

Note: Phase 26 has no formal requirement IDs in REQUIREMENTS.md yet. Using descriptive IDs based on the phase description.

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| FB-01 | Error classification (retriable vs permanent) | unit | `cd docker && python -m pytest tests/test_fallback.py::TestErrorClassification -x` | No, Wave 0 gap |
| FB-02 | Main LLM fallback chain traversal | unit | `cd docker && python -m pytest tests/test_fallback.py::TestMainLLMFallback -x` | No, Wave 0 gap |
| FB-03 | mem0 extraction fallback chain | unit | `cd docker && python -m pytest tests/test_fallback.py::TestMem0Fallback -x` | No, Wave 0 gap |
| FB-04 | Fallback config validation in validate_setup_config | unit | `cd docker && python -m pytest tests/test_fallback.py::TestFallbackValidation -x` | No, Wave 0 gap |
| FB-05 | Fallback config persistence (save/load round-trip) | unit | `cd docker && python -m pytest tests/test_fallback.py::TestFallbackPersistence -x` | No, Wave 0 gap |
| FB-06 | Setup wizard UI includes fallback config | manual-only | N/A (HTML rendering) | N/A |
| FB-07 | Dashboard settings include fallback config | manual-only | N/A (HTML rendering) | N/A |
| FB-08 | entrypoint.sh hydrates fallback config env vars | unit | `cd docker && python -m pytest tests/test_entrypoint.py::TestFallbackHydration -x` | No, Wave 0 gap |
| FB-09 | Primary provider restored after fallback resolves | unit | `cd docker && python -m pytest tests/test_fallback.py::TestPrimaryRestore -x` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd /Users/haseeb/nix-template/docker && python -m pytest tests/ -x -q --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~10 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/tests/test_fallback.py` -- covers FB-01 through FB-05, FB-09
- [ ] `docker/tests/test_entrypoint.py` extension -- covers FB-08 (file exists, needs new test class)

## Sources

### Primary (HIGH confidence)
- Codebase analysis: `docker/gateway.py` -- `_relay_to_goosed()` (line 7297), `_do_rest_relay()` (line 7536), `_update_goose_session_provider()` (line 6738), `_is_fatal_provider_error()` (line 7282), `validate_setup_config()` (line 2153), `apply_config()` (line 3316), `handle_save()` (line 9410)
- Codebase analysis: `docker/mem0_config.py` -- `build_mem0_config()`, `_find_api_key()`, `PROVIDER_MAP`, `KEY_ENV_VARS`
- Codebase analysis: `docker/entrypoint.sh` -- provider rehydration (line 228-326), config.yaml generation (line 158-355)
- Codebase analysis: `docker/setup.html` -- wizard steps 0-4, mem0 config section (line 1979), dashboard patterns

### Secondary (MEDIUM confidence)
- [LLM Fallback Pattern in Python](https://dev.to/nebulagg/how-to-add-llm-model-fallbacks-in-python-in-5-min-5200) -- MODEL_CHAIN pattern, error types to catch
- [Circuit Breaker for LLM Provider Failure](https://dev.to/sandhu93/circuit-breaker-for-llm-provider-failure-53f6) -- State management patterns (we use simpler variant)
- [Retries, Fallbacks, and Circuit Breakers in LLM Apps](https://www.getmaxim.ai/articles/retries-fallbacks-and-circuit-breakers-in-llm-apps-a-production-guide/) -- Error classification best practices

### Tertiary (LOW confidence)
- None. All findings verified against codebase or multiple web sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new dependencies, all patterns verified against existing codebase
- Architecture: HIGH -- follows existing gateway.py patterns (error detection -> retry -> new session), just adds fallback provider loop
- Pitfalls: HIGH -- derived from actual codebase analysis (singleton mem0 instance, streaming relay, env var side effects)
- UI patterns: HIGH -- matches existing setup.html patterns (lead config section, mem0 config section)

**Research date:** 2026-03-25
**Valid until:** 2026-04-25 (stable patterns, no external dependency version concerns)

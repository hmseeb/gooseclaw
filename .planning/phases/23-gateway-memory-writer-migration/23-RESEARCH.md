# Phase 23: Gateway Memory Writer Migration - Research

**Researched:** 2026-03-20
**Domain:** Gateway memory extraction pipeline migration from manual ChromaDB to mem0.add()
**Confidence:** HIGH

## Summary

Phase 23 replaces the manual memory extraction pipeline in `docker/gateway.py` (lines 6702-7094, ~390 lines) with mem0.add(). The current pipeline works like this: a daemon thread polls for idle sessions, fetches conversation history, sends it through goosed with a structured extraction prompt (MEMORY_EXTRACT_PROMPT), parses the JSON response, routes identity traits to user.md sections, and upserts knowledge to ChromaDB's runtime collection. This entire knowledge branch (ChromaDB direct upsert) gets replaced by a single mem0.add() call. The identity branch (user.md writes) must be preserved with its own separate, simpler LLM prompt.

The migration splits the current unified MEMORY_EXTRACT_PROMPT into two paths: (1) identity extraction via a slimmed-down LLM prompt that returns only identity traits for user.md routing, and (2) knowledge extraction via mem0.add(messages) which handles fact extraction, dedup, contradiction resolution, and storage internally. The key architectural change is that mem0.add() blocks for 2-20 seconds (3-6 internal LLM calls), so the existing daemon thread pattern needs enhancement with concurrent.futures.ThreadPoolExecutor and a timeout to prevent the writer loop from hanging indefinitely.

Phase 22 already built the shared config module (`docker/mem0_config.py`) and the mem0 MCP server (`docker/memory/server.py`). This phase imports `build_mem0_config()` into gateway.py, creates a lazy-loaded mem0.Memory instance, and replaces the `_process_memory_extraction` knowledge branch with mem0.add(). The identity branch stays but switches from the combined prompt to an identity-only prompt.

**Primary recommendation:** Split _process_memory_extraction into two independent paths: identity-only LLM extraction (keep goosed relay, simplify prompt, keep user.md routing) and knowledge via mem0.add(messages, user_id). Use ThreadPoolExecutor with 60s timeout for mem0.add() calls.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| GW-01 | Gateway memory writer uses mem0.add() instead of manual chromadb extraction | mem0.add() accepts messages list, handles extraction/dedup/contradiction. Replaces _get_knowledge_collection + manual upsert loop (lines 6832-7085) |
| GW-02 | Memory extraction runs async in background thread with timeout (no blocking) | concurrent.futures.ThreadPoolExecutor with Future.result(timeout=60). Existing daemon thread pattern stays, add executor for mem0.add() calls |
| GW-03 | Identity routing preserved, user.md/soul.md stay file-based, mem0 handles knowledge only | Split MEMORY_EXTRACT_PROMPT: identity-only prompt for user.md routing, mem0.add() for knowledge. Keep _classify_identity_section, _append_to_section, _fact_already_exists |
| GW-04 | Identity/knowledge routing rule enforced: traits stable 6+ months to user.md, everything else to mem0 | Identity-only prompt extracts ONLY stable traits. mem0.add() receives full conversation for knowledge extraction. No duplication because they operate on different content categories |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mem0ai | 1.0.6 (already in requirements.txt) | Knowledge extraction and storage via add() | Already installed from Phase 22. Handles extraction, dedup, contradiction resolution |
| concurrent.futures | stdlib | ThreadPoolExecutor for async mem0.add() with timeout | Python stdlib, no new deps. Cleaner than raw threading for timeout handling |
| threading | stdlib (existing) | Daemon thread for _memory_writer_loop | Already used in gateway.py |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| mem0_config | local (Phase 22) | build_mem0_config() shared config builder | Import in gateway.py to create mem0.Memory instance |
| chromadb | 1.5.5 (existing) | Still used indirectly by mem0, NOT directly by gateway anymore | mem0 manages ChromaDB internally. Gateway no longer calls chromadb directly for knowledge |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| ThreadPoolExecutor | asyncio | gateway.py is fully synchronous/threaded. Introducing asyncio would be a major refactor for no benefit. ThreadPoolExecutor fits the existing model. |
| Separate identity prompt via goosed | Local LLM call for identity | goosed relay is already the pattern. Adding a direct LLM call would require credential management that mem0_config handles for mem0 only. |

**Installation:**
```bash
# No new deps. mem0ai already in docker/requirements.txt from Phase 22.
# concurrent.futures is Python stdlib.
```

## Architecture Patterns

### Recommended Code Structure Changes
```
docker/
+-- gateway.py          # MODIFIED: replace _process_memory_extraction knowledge path
|                       #   - Add lazy mem0.Memory instance
|                       #   - Split prompt: identity-only + mem0.add()
|                       #   - Add ThreadPoolExecutor with timeout
|                       #   - Remove _get_knowledge_collection, _knowledge_runtime_col
|                       #   - Remove chromadb import for runtime collection
+-- mem0_config.py      # UNCHANGED (Phase 22)
+-- memory/server.py    # UNCHANGED (Phase 22)
```

### Pattern 1: Lazy mem0.Memory Initialization in Gateway
**What:** Create a module-level mem0.Memory instance that's lazy-loaded on first use. Avoids import-time initialization which would slow gateway startup.
**When to use:** Gateway.py initializes many subsystems. mem0 init involves loading sentence-transformers model (~90MB) and connecting to ChromaDB.
**Example:**
```python
# At module level, near other memory writer globals (line ~6706)
_mem0_instance = None
_mem0_init_lock = threading.Lock()

def _get_mem0():
    """Lazy-load mem0 Memory instance. Thread-safe."""
    global _mem0_instance
    if _mem0_instance is not None:
        return _mem0_instance
    with _mem0_init_lock:
        if _mem0_instance is not None:
            return _mem0_instance
        try:
            os.environ["MEM0_TELEMETRY"] = "false"
            from mem0 import Memory
            from mem0_config import build_mem0_config
            config = build_mem0_config()
            _mem0_instance = Memory.from_config(config)
            _memory_log.info("mem0 initialized")
        except Exception as e:
            _memory_log.error(f"mem0 init failed: {e}")
        return _mem0_instance
```

### Pattern 2: Identity-Only Extraction Prompt
**What:** Replace the combined MEMORY_EXTRACT_PROMPT with an identity-only version. This prompt asks ONLY for stable identity traits (6+ month shelf life). Knowledge extraction is handled by mem0.add() separately.
**When to use:** The identity path still needs an LLM call through goosed because user.md writes require section classification and dedup that mem0 doesn't handle.
**Example:**
```python
IDENTITY_EXTRACT_PROMPT = """You are analyzing a conversation to extract IDENTITY traits about the user.

ONLY extract traits that are stable for 6+ months:
- Name, role, relationships, people they mention regularly
- Communication style, personality traits
- Core preferences (tools, formats, habits)
- How they think, what they care about

DO NOT extract:
- Projects, deadlines, current work (those go to long-term memory separately)
- Integrations, services, technical facts
- Anything temporal or likely to change within months

Output a JSON object:
{
  "identity": ["trait1", "trait2"]
}

If there's nothing meaningful to extract, output: {"empty": true}

CONVERSATION:
"""
```

### Pattern 3: mem0.add() with Conversation Messages
**What:** Convert the fetched session messages into mem0's expected format and call mem0.add(). mem0 handles all knowledge extraction internally.
**When to use:** Replacing the manual ChromaDB upsert loop in _process_memory_extraction.
**Example:**
```python
def _mem0_add_knowledge(messages, user_id="default"):
    """Send conversation to mem0 for knowledge extraction. Blocking call.

    Args:
        messages: list of {"role": "user"/"assistant", "text": "..."} from _fetch_session_messages
        user_id: mem0 user ID for scoping
    """
    m = _get_mem0()
    if m is None:
        _memory_log.error("mem0 not available, skipping knowledge extraction")
        return

    # Convert gateway message format to mem0 format
    mem0_messages = []
    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("text", "")
        if text.strip():
            mem0_messages.append({"role": role, "content": text})

    if not mem0_messages:
        return

    result = m.add(messages=mem0_messages, user_id=user_id)
    _memory_log.info(f"mem0.add result: {result}")
    return result
```

### Pattern 4: ThreadPoolExecutor with Timeout for mem0.add()
**What:** Wrap mem0.add() in a ThreadPoolExecutor.submit() with a 60-second timeout to prevent the memory writer loop from blocking indefinitely.
**When to use:** Every mem0.add() call in the memory writer loop.
**Example:**
```python
# Module-level executor (1 worker is fine, memory extraction is sequential per session)
_mem0_executor = None

def _get_mem0_executor():
    global _mem0_executor
    if _mem0_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _mem0_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem0-add")
    return _mem0_executor

def _mem0_add_with_timeout(messages, user_id="default", timeout=60):
    """Run mem0.add() in a thread pool with timeout."""
    from concurrent.futures import TimeoutError
    executor = _get_mem0_executor()
    future = executor.submit(_mem0_add_knowledge, messages, user_id)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        _memory_log.error(f"mem0.add timed out after {timeout}s")
        return None
    except Exception as e:
        _memory_log.error(f"mem0.add failed: {e}")
        return None
```

### Pattern 5: Revised _memory_writer_loop Flow
**What:** The loop changes from "extract via goosed, parse JSON, route to identity+knowledge" to "send to mem0.add() for knowledge, extract identity via goosed with simplified prompt, route identity to user.md."
**When to use:** Replaces the entire middle section of _memory_writer_loop (lines 6809-6822).
**Example:**
```python
# Inside the idle chat processing block (line ~6807):

# 1. Knowledge extraction via mem0 (async with timeout)
_mem0_add_with_timeout(messages, user_id=MEM0_USER_ID, timeout=60)

# 2. Identity extraction via goosed (separate, simpler prompt)
convo_text = ""
for msg in messages[-40:]:
    role = msg.get("role", "unknown")
    text = msg.get("text", "")[:500]
    convo_text += f"[{role}]: {text}\n\n"

if len(convo_text.strip()) >= 50:
    extract_sid = _create_goose_session()
    if extract_sid:
        prompt = IDENTITY_EXTRACT_PROMPT + convo_text
        response, error, _media = _do_rest_relay(prompt, extract_sid)
        if not error:
            _process_identity_extraction(response)
```

### Anti-Patterns to Avoid
- **Calling mem0.add() synchronously in the main writer loop without timeout:** mem0.add() blocks 2-20s. Without timeout, a hung LLM call blocks all memory extraction forever.
- **Using mem0 for identity traits:** mem0 stores atomic facts. Identity traits need section routing in user.md, dedup against file content, and structured formatting. Keep the existing user.md pipeline for identity.
- **Removing _memory_touch or idle detection:** The idle detection mechanism is sound and unchanged. Only the extraction/storage backend changes.
- **Creating a new goosed session per extraction type:** Use ONE goosed session for identity extraction. mem0.add() doesn't need goosed at all (it makes its own LLM calls via the configured provider).
- **Importing mem0 at module top level:** mem0 imports trigger model loading. Use lazy initialization to avoid slowing gateway startup.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Knowledge extraction from conversation | Custom LLM prompt + JSON parsing + ChromaDB upsert | `mem0.add(messages)` | mem0 handles fact extraction, embedding, dedup, contradiction resolution (ADD/UPDATE/DELETE) internally |
| Knowledge deduplication | Cosine similarity checks | mem0 internal dedup | mem0 compares at 0.85 similarity threshold automatically |
| Contradiction resolution | Custom LLM-as-judge pipeline | mem0 internal pipeline | mem0 classifies each fact as ADD/UPDATE/DELETE/NOOP |
| Async with timeout pattern | Custom threading + event flags | `concurrent.futures.ThreadPoolExecutor` | stdlib, well-tested, Future.result(timeout=N) is the standard pattern |

**Key insight:** The ~100 lines of knowledge extraction code (JSON parsing, key prefixing, ChromaDB upsert, metadata management, created_at preservation) get replaced by a single mem0.add() call. The remaining ~50 lines of identity extraction stay because user.md routing is domain-specific logic mem0 can't handle.

## Common Pitfalls

### Pitfall 1: mem0.add() Blocks the Writer Loop
**What goes wrong:** mem0.add() makes 3-6 internal LLM calls. If any hangs (provider timeout, rate limit), the entire memory writer loop freezes. No more sessions get processed.
**Why it happens:** The existing loop is sequential: process one session at a time. Without timeout protection, a slow LLM call blocks everything.
**How to avoid:** Wrap mem0.add() in ThreadPoolExecutor.submit() with Future.result(timeout=60). On timeout, log and skip, do not retry.
**Warning signs:** Memory writer log shows "extracting from session X" but never "done for session X". Subsequent sessions pile up.

### Pitfall 2: Double Extraction (Identity in mem0 + user.md)
**What goes wrong:** If you pass the full conversation to both mem0.add() and the identity extraction prompt, mem0 might also extract identity-like facts (e.g., "user's name is Haseeb") and store them as knowledge memories. Now the same fact exists in both user.md and mem0.
**Why it happens:** mem0's extraction prompt is generic. It extracts ALL facts, including stable identity traits.
**How to avoid:** This is acceptable and expected. mem0's version will be a knowledge-layer backup. The identity prompt explicitly only extracts 6+ month stable traits. The two systems serve different purposes (always-present system prompt vs on-demand search). Minor duplication is fine. Do NOT try to filter mem0's extraction.
**Warning signs:** None. This is by design per GW-04.

### Pitfall 3: Removing the Goosed Session for Identity Extraction
**What goes wrong:** If you try to skip goosed and call an LLM directly for identity extraction, you'd need to handle provider credentials, API client setup, and model routing. This duplicates mem0_config.py's logic in a different context.
**Why it happens:** Temptation to "simplify" by removing goosed relay dependency.
**How to avoid:** Keep the goosed relay for identity extraction. It's already working, handles all providers transparently, and adds 5s overhead which is fine for background processing.
**Warning signs:** PR introduces direct LLM API calls in gateway.py.

### Pitfall 4: mem0 Init at Module Level
**What goes wrong:** `from mem0 import Memory; Memory.from_config(config)` at module level triggers sentence-transformers model loading (~90MB), which adds 5-15s to gateway.py import time. Every gateway restart becomes slow.
**Why it happens:** Following the MCP server pattern where module-level init is fine (MCP server is a separate process).
**How to avoid:** Use lazy initialization with double-checked locking pattern (_get_mem0()). Only init when first memory extraction runs, not at import time.
**Warning signs:** Gateway startup takes 10+ seconds longer than before.

### Pitfall 5: Forgetting to Remove Dead ChromaDB Code
**What goes wrong:** After migration, the old `_get_knowledge_collection()`, `_knowledge_runtime_col`, and direct chromadb import for runtime collection stay in gateway.py as dead code. Future devs get confused about which code path is active.
**Why it happens:** Fear of breaking things, incomplete cleanup.
**How to avoid:** Explicitly remove: `_knowledge_runtime_col` global, `_get_knowledge_collection()` function, the entire knowledge branch of `_process_memory_extraction()`, and the `import chromadb` statement from that function. Keep identity-related functions intact.
**Warning signs:** Both chromadb direct import and mem0 import exist in gateway.py after migration.

### Pitfall 6: Not Updating Tests
**What goes wrong:** Existing tests (TestProcessMemoryExtraction, especially test_knowledge_items_upserted_to_chromadb) mock `_get_knowledge_collection` and check chromadb.upsert calls. These tests will fail or become meaningless after migration.
**Why it happens:** Tests patch old code paths that no longer exist.
**How to avoid:** Update all knowledge-related tests to mock mem0.Memory.add() instead of ChromaDB. Keep identity-related tests as-is (they still test user.md routing).
**Warning signs:** Tests pass but don't actually verify mem0 integration.

## Code Examples

### Complete Revised _memory_writer_loop (core change)
```python
# Source: Derived from current gateway.py lines 6752-6830 + mem0.add() pattern

def _memory_writer_loop():
    """Background loop: check for idle sessions and extract memories."""
    global _memory_writer_running
    _memory_writer_running = True
    _memory_log.info("started")

    while True:
        try:
            time.sleep(60)  # check every minute

            setup = load_setup()
            if not setup:
                continue
            if not setup.get("memory_writer_enabled", False):
                continue
            idle_minutes = setup.get("memory_idle_minutes", 10)
            idle_threshold = idle_minutes * 60

            # find idle chats (UNCHANGED)
            now = time.time()
            idle_chats = []
            with _memory_last_activity_lock:
                for chat_id, last_time in list(_memory_last_activity.items()):
                    if now - last_time >= idle_threshold:
                        idle_chats.append(chat_id)

            for chat_id in idle_chats:
                sid = _session_manager.get("telegram", chat_id)
                if not sid or sid in _memory_processed_sessions:
                    with _memory_last_activity_lock:
                        _memory_last_activity.pop(chat_id, None)
                    continue

                _memory_processed_sessions.add(sid)
                with _memory_last_activity_lock:
                    _memory_last_activity.pop(chat_id, None)

                messages = _fetch_session_messages(sid)
                if not messages or len(messages) < 2:
                    continue

                _memory_log.info(f"extracting from session {sid} ({len(messages)} msgs)")

                # ── NEW: knowledge via mem0.add() (async with timeout) ──
                _mem0_add_with_timeout(messages[-40:], user_id=MEM0_USER_ID, timeout=60)

                # ── KEPT: identity via goosed (simplified prompt) ──
                convo_text = ""
                for msg in messages[-40:]:
                    role = msg.get("role", "unknown")
                    text = msg.get("text", "")[:500]
                    convo_text += f"[{role}]: {text}\n\n"

                if len(convo_text.strip()) >= 50:
                    try:
                        extract_sid = _create_goose_session()
                        if extract_sid:
                            prompt = IDENTITY_EXTRACT_PROMPT + convo_text
                            response, error, _media = _do_rest_relay(prompt, extract_sid)
                            if not error:
                                _process_identity_extraction(response)
                    except Exception as e:
                        _memory_log.error(f"identity extraction error: {e}")

                _memory_log.info(f"done for session {sid}")

        except Exception as e:
            _memory_log.error(f"loop error: {e}")
```

### _process_identity_extraction (renamed, identity-only)
```python
# Source: Derived from current _process_memory_extraction, identity branch only

def _process_identity_extraction(response_text):
    """Parse identity-only extraction response, route traits to user.md.

    Only handles identity items. Knowledge is handled by mem0.add() separately.
    """
    data = _extract_json_from_response(response_text)
    if data is None:
        _memory_log.info("no valid JSON found in identity response")
        return

    if data.get("empty"):
        _memory_log.info("no identity traits to extract")
        return

    identity_dir = os.path.join(DATA_DIR, "identity")
    timestamp = time.strftime("%Y-%m-%d %H:%M")

    identity_items = data.get("identity", [])
    if not identity_items:
        return

    user_file = os.path.join(identity_dir, "user.md")
    if not os.path.exists(user_file):
        _memory_log.info("user.md not found, skipping identity writes")
        return

    with open(user_file, "r") as f:
        content = f.read()

    by_section = {}
    for item in identity_items:
        if not isinstance(item, str) or not item.strip():
            continue
        if _fact_already_exists(item, content):
            _memory_log.info(f"skipping duplicate identity: {item}")
            continue
        section = _classify_identity_section(item)
        by_section.setdefault(section, []).append(item)

    added = 0
    for section_header, items in by_section.items():
        additions = "\n".join(f"- {item}" for item in items)
        if section_header in content:
            content = _append_to_section(content, section_header, additions, timestamp)
        else:
            content += f"\n\n{section_header}\n\n<!-- auto-extracted {timestamp} -->\n{additions}\n"
        added += len(items)

    if added > 0:
        with open(user_file, "w") as f:
            f.write(content)
        _memory_log.info(f"added {added} identity traits to user.md")
```

### mem0 Message Format Conversion
```python
# Source: mem0 docs + MCP server pattern from Phase 22

def _convert_to_mem0_messages(gateway_messages):
    """Convert gateway message format to mem0 format.

    Gateway: [{"role": "user", "text": "hello"}, ...]
    mem0:    [{"role": "user", "content": "hello"}, ...]

    mem0.add() accepts: str, dict, or list of dicts with role+content.
    """
    mem0_messages = []
    for msg in gateway_messages:
        role = msg.get("role", "user")
        text = msg.get("text", "")
        if text.strip():
            # Map gateway roles to standard roles
            if role not in ("user", "assistant", "system"):
                role = "user"  # safety fallback
            mem0_messages.append({"role": role, "content": text[:2000]})  # truncate per message
    return mem0_messages
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Combined extraction prompt (identity+knowledge) | Split: identity-only prompt + mem0.add() for knowledge | This phase (23) | Cleaner separation of concerns, knowledge gets contradiction resolution |
| Manual ChromaDB upsert with key-based dedup | mem0.add() with automatic dedup at 0.85 similarity | This phase (23) | ~100 lines of upsert code removed |
| No contradiction resolution (append-only knowledge) | mem0 ADD/UPDATE/DELETE pipeline | This phase (23) | Old facts get updated, not accumulated |
| Synchronous extraction (blocks loop) | ThreadPoolExecutor with 60s timeout | This phase (23) | Memory writer loop can't hang forever |

**Deprecated/outdated after this phase:**
- `_get_knowledge_collection()`: No longer needed. mem0 manages ChromaDB internally.
- `_knowledge_runtime_col`: Dead global. Remove.
- `MEMORY_EXTRACT_PROMPT` (combined): Replace with `IDENTITY_EXTRACT_PROMPT` (identity-only).
- `_process_memory_extraction()`: Rename to `_process_identity_extraction()`, remove knowledge branch.

## What Gets Removed (Lines to Delete)

| Lines | Code | Why |
|-------|------|-----|
| 6711-6744 | `MEMORY_EXTRACT_PROMPT` (combined prompt) | Replaced by IDENTITY_EXTRACT_PROMPT (identity-only) |
| 6832-6851 | `_knowledge_runtime_col`, `_get_knowledge_collection()` | mem0 manages ChromaDB internally |
| 7027-7085 | Knowledge branch of `_process_memory_extraction()` | Replaced by mem0.add() |

## What Gets Kept (Identity Pipeline)

| Lines | Code | Why |
|-------|------|-----|
| 6854-6858 | `_fact_already_exists()` | Still used for identity dedup in user.md |
| 6861-6868 | `_append_to_section()` | Still used for user.md section routing |
| 6871-6917 | `_classify_identity_section()` | Still used for routing traits to user.md sections |
| 6920-6969 | `_extract_json_from_response()` | Still used for parsing identity extraction JSON |
| 6988-7025 | Identity branch of `_process_memory_extraction()` | Becomes `_process_identity_extraction()` |

## Open Questions

1. **mem0.add() with conversation messages vs single content string**
   - What we know: mem0.add() accepts str, dict, or list of dicts. The MCP server uses `messages=[{"role": "user", "content": content}]`. For the gateway, we have a full conversation (user+assistant turns).
   - What's unclear: Whether passing the full conversation list produces better extraction than concatenating into a single string. mem0's internal prompt likely handles conversation format natively.
   - Recommendation: Pass the full conversation as a list of role/content dicts. This gives mem0 more context about who said what. If extraction quality is poor, fall back to single concatenated string.

2. **MEM0_USER_ID consistency between MCP server and gateway**
   - What we know: MCP server uses `os.environ.get("MEM0_USER_ID", "default")`. Gateway needs to use the same user_id.
   - What's unclear: Nothing, really. Both should use the same env var.
   - Recommendation: Use same `MEM0_USER_ID` env var in gateway. Default to "default".

3. **Concurrent ChromaDB access: mem0 MCP server + gateway mem0 instance**
   - What we know: Both the mem0 MCP server and gateway will have separate mem0.Memory instances pointing at the same ChromaDB path. ChromaDB PersistentClient supports concurrent read but has caveats with concurrent writes.
   - What's unclear: Whether two processes writing to the same ChromaDB path simultaneously causes corruption.
   - Recommendation: This is unlikely to be a problem in practice because: (a) MCP server add() calls are user-initiated (rare), (b) gateway add() calls happen on idle detection (also rare), (c) ChromaDB uses SQLite which handles concurrent writes with WAL. Monitor for "database is locked" errors.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (via unittest, existing pattern) |
| Config file | docker/pytest.ini (testpaths = tests, timeout = 30) |
| Quick run command | `cd docker && python -m pytest test_gateway.py -x -k "Memory" --timeout=30` |
| Full suite command | `cd docker && python -m pytest test_gateway.py -v --timeout=30` |
| Estimated runtime | ~15 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| GW-01 | mem0.add() called with converted messages instead of chromadb upsert | unit | `cd docker && python -m pytest test_gateway.py -x -k "TestProcessMemoryExtraction or TestMem0Knowledge"` | Partial (existing tests need updating) |
| GW-02 | mem0.add() runs with timeout via ThreadPoolExecutor, timeout returns None | unit | `cd docker && python -m pytest test_gateway.py -x -k "TestMem0Timeout"` | No, Wave 0 gap |
| GW-03 | Identity extraction still routes to user.md sections correctly | unit | `cd docker && python -m pytest test_gateway.py -x -k "TestProcessIdentityExtraction"` | Partial (existing TestProcessMemoryExtraction tests identity path) |
| GW-04 | IDENTITY_EXTRACT_PROMPT only requests stable traits, mem0 handles knowledge | unit (prompt inspection + mock verification) | `cd docker && python -m pytest test_gateway.py -x -k "TestIdentityPrompt or TestRoutingSplit"` | No, Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `cd docker && python -m pytest test_gateway.py -x -k "Memory or Mem0 or Identity" --timeout=30`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~10 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] Update `docker/test_gateway.py::TestProcessMemoryExtraction` -- refactor knowledge tests to mock mem0.Memory.add() instead of chromadb, keep identity tests
- [ ] Add `docker/test_gateway.py::TestMem0AddWithTimeout` -- test timeout behavior, test successful add, test exception handling
- [ ] Add `docker/test_gateway.py::TestIdentityExtractPrompt` -- verify IDENTITY_EXTRACT_PROMPT only requests identity traits
- [ ] Add `docker/test_gateway.py::TestConvertToMem0Messages` -- verify message format conversion (gateway format -> mem0 format)
- [ ] Add `docker/test_gateway.py::TestGetMem0` -- verify lazy initialization, thread safety, telemetry disabled

**Testing approach:** Follow existing test_gateway.py pattern (unittest.TestCase, unittest.mock.patch). Mock `mem0.Memory` and `mem0.Memory.from_config` at the module level. Mock `_get_mem0()` to return a mock Memory instance. Verify `.add()` called with correct args. For timeout tests, use a mock that sleeps longer than timeout.

## Sources

### Primary (HIGH confidence)
- Codebase: docker/gateway.py lines 6702-7094 -- existing memory writer (verified, read directly)
- Codebase: docker/mem0_config.py -- shared config builder from Phase 22 (verified)
- Codebase: docker/memory/server.py -- mem0 MCP server from Phase 22 (verified)
- Codebase: docker/test_gateway.py -- existing memory writer tests (verified)
- Codebase: identity/schemas/user.schema.md -- identity file schema (verified)
- [Python concurrent.futures docs](https://docs.python.org/3/library/concurrent.futures.html) -- ThreadPoolExecutor, Future.result(timeout)

### Secondary (MEDIUM confidence)
- [mem0 docs - add memories](https://docs.mem0.ai/api-reference/memory/add-memories) -- add() accepts messages as str/dict/list
- [DeepWiki mem0 history and storage](https://deepwiki.com/mem0ai/mem0/3.3-history-and-storage-management) -- add() message format, return structure
- [mem0 Python quickstart](https://docs.mem0.ai/open-source/python-quickstart) -- OSS Memory class usage pattern
- Phase 22 research (22-RESEARCH.md) -- mem0 architecture, config patterns, pitfalls

### Tertiary (LOW confidence)
- mem0.add() return value format -- documented in DeepWiki but not tested against v1.0.6 directly. Verified in MCP server tests with mock.
- Concurrent ChromaDB access safety -- no definitive source found. Based on SQLite WAL mode general knowledge.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already installed, no new deps
- Architecture: HIGH -- well-understood codebase, clear migration path from reading the actual code
- Pitfalls: HIGH -- timing, dead code, test updates are concrete and actionable
- Identity/knowledge split: HIGH -- clear requirement (GW-03, GW-04), straightforward prompt separation

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (30 days, mem0 v1.0.x is stable, gateway.py code is our own)

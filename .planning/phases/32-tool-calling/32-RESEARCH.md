# Phase 32: Tool Calling - Research

**Researched:** 2026-03-27
**Domain:** Gemini Live API function calling + goosed MCP tool discovery + voice UI feedback
**Confidence:** HIGH

## Summary

Phase 32 wires Gemini Live API tool calling to goosed MCP extensions. The voice relay (Phase 28/30) already parses `toolCall` and `toolCallCancellation` messages and forwards them to the browser, but does NOT execute them. The current `_voice_relay_gemini_to_browser` just sends `{"type": "tool_call", "data": ...}` to the browser without doing anything. The core work is: (1) intercept toolCall in the Gemini-to-browser relay, (2) discover available MCP tools from goosed at session start, (3) convert them to Gemini function declarations in the setup config, (4) execute tools via goosed `/reply` when Gemini requests them, (5) send toolResponse back to Gemini with SILENT scheduling, and (6) show visual tool feedback in voice.html.

The existing gateway.py has all the building blocks: `_gemini_build_config()` generates the setup JSON (currently without tools), `_do_rest_relay()` talks to goosed, `_voice_parse_server_message()` already classifies toolCall and toolCallCancellation messages, and `_create_goose_session()` creates isolated sessions. The config.yaml extensions data is readable via goosed `GET /config`. Tool calling on Gemini 3.1 Flash Live is synchronous only (audio pauses during execution), so SILENT scheduling on the response prevents double-speech.

**Primary recommendation:** Dynamically discover MCP tools from goosed `/config` at session start, convert to Gemini function declarations, execute via a dedicated goosed session per voice connection, and show spinner/result in voice.html transcript.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TOOL-01 | Gateway dynamically discovers ALL available MCP tools/extensions from goosed and maps them as Gemini function declarations | goosed `GET /config` returns extensions dict. Parse extension names/descriptions, build functionDeclarations array. See Architecture Pattern 1. |
| TOOL-02 | When Gemini calls any function mid-conversation, gateway routes it to the correct MCP tool and feeds the result back | Intercept toolCall in `_voice_relay_gemini_to_browser`, dispatch to goosed via `_do_rest_relay` on a voice-dedicated session, send `toolResponse` back to Gemini. See Architecture Pattern 2. |
| TOOL-03 | Tool discovery refreshes on session start so newly installed extensions are immediately available to voice | Call `_discover_voice_tools()` inside `handle_voice_ws()` before `_gemini_connect()`. Fresh discovery each session, no caching. |
| TOOL-04 | Tool execution shows visual feedback in transcript (tool name, "running..." spinner, result summary) | Add `addToolStatus(name, status, result)` function to voice.html. Insert styled tool status elements in transcript area. |
| TOOL-05 | Tool responses use SILENT scheduling so Gemini speaks naturally about results (no double-speech) | Include `scheduling: "SILENT"` in toolResponse. Gemini incorporates result silently, then speaks about it naturally on next turn. |
| TOOL-06 | Voice channel has feature parity with text channels for tool access (everything goosed can do, voice can do) | Dynamic discovery from goosed means ALL enabled extensions are available. No hardcoded tool list. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.10 | All gateway.py code | Existing constraint. No pip allowed. |
| goosed REST API | Local | Tool discovery via `GET /config`, tool execution via `POST /reply` | Already used throughout gateway.py for all goosed interaction. |
| Gemini Live API (WebSocket) | v1beta | Function calling over bidirectional WebSocket | Already connected in Phase 28. Just add `tools` to setup config. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `json` | stdlib | Parse goosed config, build Gemini messages | All tool discovery and execution |
| `threading` | stdlib | Tool execution in background thread | Dispatch tool calls without blocking relay |
| `http.client` | stdlib | REST calls to goosed | Tool discovery and execution |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| goosed `/config` for tool discovery | Read config.yaml directly | Config file may not reflect runtime state. `/config` API returns what goosed actually has loaded. |
| goosed `/reply` for tool execution | Direct MCP protocol to extensions | Would require implementing MCP client in Python stdlib. Massive scope creep. goosed is already the MCP client. |
| SILENT scheduling | WHEN_IDLE scheduling | WHEN_IDLE waits for model to finish, then speaks about result. SILENT lets model decide when to mention it. SILENT is better for voice because model integrates result into natural speech. |

**Installation:**
```bash
# No installation needed. All stdlib + existing goosed REST API.
```

## Architecture Patterns

### Recommended Change Structure
```
docker/
  gateway.py      # MODIFY: add tool discovery, execution, and Gemini setup config
  voice.html      # MODIFY: add tool status UI in transcript
docker/tests/
  test_voice.py   # MODIFY: add tool calling tests
```

### Pattern 1: Dynamic Tool Discovery from goosed
**What:** At voice session start, query goosed `GET /config` for enabled extensions, then convert each extension's name and description into Gemini function declarations.
**When to use:** Every voice WebSocket connection setup, before sending Gemini setup config.

```python
# Source: gateway.py existing goosed /config pattern (line 6847)
def _discover_voice_tools():
    """Query goosed for enabled extensions, return Gemini function declarations."""
    try:
        conn = _goosed_conn(timeout=5)
        conn.request("GET", "/config", headers={"X-Secret-Key": _INTERNAL_GOOSE_TOKEN})
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            return []
        cfg = json.loads(resp.read().decode("utf-8", errors="replace"))
        conn.close()
        extensions = cfg.get("config", {}).get("extensions", {})
        declarations = []
        for ext_name, ext_config in extensions.items():
            if not isinstance(ext_config, dict):
                continue
            if not ext_config.get("enabled", True):
                continue
            # Build a generic tool declaration for each extension
            # The extension name becomes the function name
            # goosed handles routing internally when we send the prompt
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', ext_name)
            declarations.append({
                "name": safe_name,
                "description": f"Use the {ext_name} extension/tool",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "request": {
                            "type": "STRING",
                            "description": "What to do with this tool"
                        }
                    },
                    "required": ["request"]
                }
            })
        return declarations
    except Exception as e:
        _voice_log.warning(f"Tool discovery failed: {e}")
        return []
```

**Key insight:** We do NOT need to know every MCP tool's exact parameters. We create one Gemini function per extension with a generic `request` string parameter. When Gemini calls the function, we relay the request to goosed as a natural language prompt. goosed's LLM figures out which specific MCP tool to call. This is the "layered tool pattern" used by Block themselves.

**Alternative (more precise but harder):** Parse each extension's MCP tool definitions for exact parameter schemas. This requires goosed to expose a `/tools` endpoint listing individual MCP tools with their JSON Schema parameters. goosed does not currently expose this, so the generic approach above is the pragmatic choice.

### Pattern 2: Tool Execution via goosed Session
**What:** When Gemini sends a toolCall, create/reuse a goosed session and relay the tool request as a natural language prompt.
**When to use:** Every time Gemini sends a `toolCall` message during a voice session.

```python
# Source: existing _do_rest_relay pattern (line 7393)
def _voice_execute_tool(tool_name, tool_args, session_id):
    """Execute a tool call via goosed and return the result string."""
    request = tool_args.get("request", "")
    prompt = f"Use the {tool_name} tool: {request}"
    try:
        response_text, error, _media = _do_rest_relay(
            prompt, session_id, timeout=15
        )
        if error:
            return {"error": error}
        return {"result": response_text[:2000]}  # cap result size
    except Exception as e:
        return {"error": str(e)}
```

### Pattern 3: Tool Response to Gemini with SILENT Scheduling
**What:** After tool execution, send `toolResponse` back to Gemini. Use SILENT scheduling so Gemini doesn't immediately start speaking about the result but incorporates it naturally.
**When to use:** After every successful or failed tool execution.

```python
# Source: Gemini Live API docs (ai.google.dev/api/live)
def _voice_send_tool_response(gemini_sock, call_id, call_name, result, lock):
    """Send toolResponse to Gemini via WebSocket."""
    response = {
        "toolResponse": {
            "functionResponses": [{
                "id": call_id,
                "name": call_name,
                "response": result
            }]
        }
    }
    with lock:
        ws_send_frame(gemini_sock, WS_OP_TEXT,
                      json.dumps(response).encode(), mask=True)
```

### Pattern 4: Browser Tool Status UI
**What:** Show tool execution status in the transcript area with name, spinner, and result.
**When to use:** Whenever gateway sends tool_call or tool_result messages to browser.

```javascript
// New function in voice.html
function addToolStatus(name, status, result) {
    var el = document.createElement('div');
    el.className = 'transcript-msg tool-status';
    el.id = 'tool-' + name.replace(/[^a-zA-Z0-9]/g, '-');

    var icon = status === 'running' ? '...' : (status === 'done' ? 'done' : 'error');
    el.innerHTML = '<div class="tool-badge">' +
        '<span class="tool-name">' + name + '</span>' +
        '<span class="tool-state">' + icon + '</span>' +
        '</div>' +
        (result ? '<div class="tool-result">' + result + '</div>' : '');

    // Replace existing element for same tool, or append
    var existing = document.getElementById(el.id);
    if (existing) {
        existing.replaceWith(el);
    } else {
        document.getElementById('transcript').appendChild(el);
    }
    autoScrollTranscript();
}
```

### Anti-Patterns to Avoid
- **Hardcoded tool list:** Do NOT hardcode `search_memory`, `search_gmail` etc. Discover dynamically from goosed. This ensures TOOL-06 (feature parity).
- **Executing tools in the relay thread:** Do NOT block `_voice_relay_gemini_to_browser` during tool execution. Dispatch to a separate thread so the relay can still handle GoAway, interrupts, etc.
- **Forgetting to send toolResponse:** If you intercept a toolCall but don't send toolResponse, Gemini hangs forever (sync calling). ALWAYS send a response, even on error.
- **Echoing tool results as speech:** Don't send tool results as both `toolResponse` and `clientContent`. Use `toolResponse` only. Gemini will speak about the results naturally.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| MCP tool execution | Custom MCP client in Python | goosed `POST /reply` | goosed is already the MCP client, handles all extension protocols, tool routing |
| Tool parameter schema extraction | Parse MCP tool schemas from extensions | Generic `request` string parameter | goosed's LLM handles parameter extraction from natural language |
| Audio during tool wait | Filler audio generation | Visual spinner in transcript | Gemini 3.1 Flash Live pauses audio during sync tool calls. Visual feedback is the right UX. |
| Tool result formatting | Custom result parsers per tool | Truncated raw text from goosed | goosed already formats results. Just cap at 2000 chars. |

**Key insight:** goosed is the tool execution engine. Gateway is just a bridge between Gemini's function calling protocol and goosed's natural language interface. Keep the bridge thin.

## Common Pitfalls

### Pitfall 1: Gemini Hangs Forever Without toolResponse
**What goes wrong:** Gemini sends a toolCall, gateway intercepts it but fails to send toolResponse (crash, timeout, exception swallowed). Gemini blocks indefinitely waiting for the response. Voice session freezes.
**Why it happens:** Exception handling gap in tool execution path. goosed `/reply` times out, exception caught but toolResponse never sent.
**How to avoid:** ALWAYS send toolResponse in a finally block. On error, send `{"error": "Tool execution failed"}` as the response. Set a hard timeout (15s) on goosed relay.
**Warning signs:** Voice session freezes after user asks to "check calendar" or similar. No audio, no transcript, session appears hung.

### Pitfall 2: Tool Execution Blocks Relay Thread
**What goes wrong:** Tool execution runs in the Gemini-to-browser relay thread. During the 2-10 seconds of tool execution, GoAway messages, interruptions, and other control messages from Gemini are not processed. If GoAway arrives during a tool call, the session may die.
**Why it happens:** Synchronous tool execution in the relay loop.
**How to avoid:** Dispatch tool execution to a separate thread. The relay thread sends the "executing" status to browser, spawns a tool thread, and continues reading Gemini messages. The tool thread sends toolResponse when done.
**Warning signs:** GoAway during tool call causes unrecoverable session death. Interruptions during tool calls are not processed.

### Pitfall 3: Tool Discovery Returns Empty on goosed Not Ready
**What goes wrong:** Voice session starts before goosed is fully initialized. `GET /config` returns empty or error. Gemini setup config has no tools. User asks for tools, Gemini says "I don't have any tools available."
**Why it happens:** goosed startup takes 5-15 seconds. If user opens voice immediately after container start, goosed may not be ready.
**How to avoid:** Check `goosed_startup_state["state"]` before tool discovery. If not "ready", skip tools (voice works without them) and log a warning. Alternatively, retry discovery once after a 2s delay.
**Warning signs:** Tools work sometimes but not right after deployment/restart.

### Pitfall 4: Double-Speech from Tool Results
**What goes wrong:** Gateway sends toolResponse to Gemini AND separately sends the result as clientContent text. Gemini speaks about the result twice. Or: gateway doesn't use SILENT scheduling, so Gemini immediately narrates the raw tool response before integrating it.
**Why it happens:** Confusion between `toolResponse` (gives model the data) and `clientContent` (adds to conversation). Using both causes duplication.
**How to avoid:** ONLY use `toolResponse` to send results back to Gemini. Never also send as `clientContent`. The SILENT scheduling (inside response object) lets Gemini incorporate the data naturally.
**Warning signs:** AI says "The search returned: [raw JSON]" followed by a natural summary. Two speeches about the same result.

### Pitfall 5: Tool Names with Special Characters Break Gemini
**What goes wrong:** Extension names from goosed contain hyphens, dots, or spaces (e.g., "google-calendar", "mem0.search"). Gemini function declarations require names matching `[a-zA-Z_][a-zA-Z0-9_]*`. Invalid names cause setup to fail or toolCalls to use mangled names that don't match.
**Why it happens:** goosed extension names are user-friendly, not Gemini-compatible.
**How to avoid:** Sanitize extension names: replace non-alphanumeric chars with underscores. Maintain a mapping dict (`_tool_name_map`) from sanitized names back to original extension names for routing.
**Warning signs:** Gemini setup fails with cryptic error. Or toolCall arrives with a name that doesn't match any known extension.

### Pitfall 6: Cancelled Tool Calls Create Orphaned goosed Sessions
**What goes wrong:** User interrupts while a tool is executing. Gemini sends `toolCallCancellation` with the call IDs. But goosed is already processing the request in a `/reply` call. The goosed session keeps running, consuming resources.
**Why it happens:** No cancellation mechanism for in-flight goosed requests.
**How to avoid:** Track in-flight tool calls with a dict. On cancellation, close the goosed HTTP connection (stored in `sock_ref`). goosed will stop processing. Discard the result.
**Warning signs:** goosed logs show completed tool calls that were never sent back to Gemini.

## Code Examples

### Complete Tool Call Handling in Relay

```python
# Modified _voice_relay_gemini_to_browser with tool execution
def _voice_relay_gemini_to_browser(browser_sock, session_state, stop_event):
    tool_session_id = session_state.get("tool_session_id")
    in_flight_tools = {}  # call_id -> {"conn": http_conn, "thread": Thread}

    try:
        while not stop_event.is_set():
            with session_state["_lock"]:
                gs = session_state["gemini_sock"]
            if not gs:
                break
            opcode, payload = ws_recv_frame(gs)
            if opcode is None or opcode == WS_OP_CLOSE:
                break
            if opcode == WS_OP_PING:
                ws_send_frame(gs, WS_OP_PONG, payload, mask=True)
                continue
            if opcode == WS_OP_PONG:
                continue
            if opcode == WS_OP_TEXT:
                msg = json.loads(payload.decode())
                parsed = _voice_parse_server_message(msg)
                if not parsed:
                    continue

                if parsed["type"] == "tool_call":
                    tool_data = parsed["data"]
                    for fc in tool_data.get("functionCalls", []):
                        call_id = fc.get("id", "")
                        call_name = fc.get("name", "")
                        call_args = fc.get("args", {})

                        # Notify browser: tool executing
                        ws_send_frame(browser_sock, WS_OP_TEXT,
                            json.dumps({
                                "type": "tool_status",
                                "name": call_name,
                                "status": "running"
                            }).encode())

                        # Execute in background thread
                        def _exec(cid, cname, cargs):
                            result = _voice_execute_tool(
                                cname, cargs, tool_session_id)
                            # Send result to Gemini
                            _voice_send_tool_response(
                                session_state, cid, cname, result)
                            # Notify browser: tool done
                            summary = result.get("result", result.get("error", ""))
                            ws_send_frame(browser_sock, WS_OP_TEXT,
                                json.dumps({
                                    "type": "tool_status",
                                    "name": cname,
                                    "status": "done",
                                    "result": summary[:200]
                                }).encode())

                        t = threading.Thread(
                            target=_exec,
                            args=(call_id, call_name, call_args),
                            daemon=True)
                        in_flight_tools[call_id] = t
                        t.start()

                elif parsed["type"] == "tool_cancelled":
                    for cid in parsed.get("ids", []):
                        in_flight_tools.pop(cid, None)
                        ws_send_frame(browser_sock, WS_OP_TEXT,
                            json.dumps({
                                "type": "tool_status",
                                "name": "tool",
                                "status": "cancelled"
                            }).encode())

                # ... existing handlers for ready, transcript, etc.
    except (ConnectionError, OSError, socket.timeout):
        pass
    finally:
        stop_event.set()
```

### Gemini Setup Config with Tools

```python
def _gemini_build_config(resumption_handle=None, voice_name="Aoede", tools=None):
    """Build Gemini Live API setup config JSON."""
    config = {
        "config": {
            "model": "models/gemini-3.1-flash-live-preview",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice_name}
                    }
                }
            },
            "systemInstruction": {
                "parts": [{"text": "You are a helpful AI assistant..."}]
            },
            "sessionResumption": {"handle": resumption_handle},
            "contextWindowCompression": {"slidingWindow": {}},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {"disabled": False}
            }
        }
    }
    if tools:
        config["config"]["tools"] = [{"functionDeclarations": tools}]
    return config
```

### Voice HTML Tool Status CSS

```css
.tool-status {
    background: rgba(59, 130, 246, 0.1);
    border-left: 3px solid #3b82f6;
    padding: 8px 12px;
    margin: 4px 0;
    border-radius: 4px;
    font-size: 0.85em;
}
.tool-badge {
    display: flex;
    align-items: center;
    gap: 8px;
}
.tool-name {
    font-weight: 600;
    color: #3b82f6;
}
.tool-state {
    color: #94a3b8;
}
.tool-state.running::after {
    content: '';
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid #3b82f6;
    border-top-color: transparent;
    border-radius: 50%;
    animation: tool-spin 0.8s linear infinite;
}
@keyframes tool-spin {
    to { transform: rotate(360deg); }
}
.tool-result {
    margin-top: 4px;
    color: #64748b;
    font-size: 0.9em;
    max-height: 60px;
    overflow: hidden;
    text-overflow: ellipsis;
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Hardcoded tool list | Dynamic discovery from goosed config | Phase 32 (now) | All MCP extensions automatically available to voice |
| clientContent for tool results (cookbook #906 workaround) | Top-level `toolResponse` message | Gemini API v1beta (current) | Cleaner, official format. `toolResponse` is the correct wire format. |
| Async tool calling (NON_BLOCKING) | Sync only on 3.1 Flash Live | 2026-03-26 | Audio pauses during tool execution. Must use visual feedback. |

**Deprecated/outdated:**
- `BidiGenerateContentToolResponse` via `clientContent` wrapper: The cookbook issue #906 workaround is no longer needed. The official `toolResponse` top-level field works correctly.
- `generation_config` in setup: Use `generationConfig` (camelCase) in the WebSocket JSON wire format.

## Open Questions

1. **goosed extension metadata richness**
   - What we know: `GET /config` returns extension names and enabled status
   - What's unclear: Whether goosed returns tool descriptions, parameter schemas, or just extension names
   - Recommendation: Start with extension names only and generic `request` parameter. If goosed exposes richer metadata, enhance declarations later. The generic approach works because goosed's LLM handles parameter extraction.

2. **Tool execution latency impact on voice UX**
   - What we know: goosed `/reply` creates a session + runs MCP tool + LLM reasoning = 2-10 seconds typical
   - What's unclear: Whether users find 5+ seconds of silence acceptable during voice
   - Recommendation: Pre-create a goosed session at voice session start. Show animated spinner in transcript. Consider a "let me check that for you" filler message if latency exceeds 3s. The SILENT scheduling means Gemini won't double-speak about the result.

3. **Reusing vs creating goosed session per tool call**
   - What we know: `_create_goose_session()` is ~100ms overhead. MCP tools in goosed need a session context.
   - What's unclear: Whether a single long-lived session works for all tool calls or if state accumulates and causes issues
   - Recommendation: Create ONE goosed session per voice session (at session start). Reuse it for all tool calls within that voice session. This preserves conversation context within the voice session's tool interactions.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest |
| Config file | `docker/pytest.ini` |
| Quick run command | `cd docker && python -m pytest tests/test_voice.py -x -q` |
| Full suite command | `cd docker && python -m pytest tests/ -x -q --ignore=tests/e2e` |
| Estimated runtime | ~5 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TOOL-01 | `_discover_voice_tools()` returns Gemini function declarations from goosed config | unit | `cd docker && python -m pytest tests/test_voice.py::TestToolDiscovery -x` | No (Wave 0 gap) |
| TOOL-02 | `_voice_execute_tool()` relays to goosed and returns result | unit | `cd docker && python -m pytest tests/test_voice.py::TestToolExecution -x` | No (Wave 0 gap) |
| TOOL-03 | Tool declarations refresh each session (no stale cache) | unit | `cd docker && python -m pytest tests/test_voice.py::TestToolDiscovery::test_fresh_each_session -x` | No (Wave 0 gap) |
| TOOL-04 | Gateway sends tool_status messages to browser during execution | unit | `cd docker && python -m pytest tests/test_voice.py::TestToolRelay -x` | No (Wave 0 gap) |
| TOOL-05 | toolResponse uses correct format (id, name, response) | unit | `cd docker && python -m pytest tests/test_voice.py::TestToolResponse -x` | No (Wave 0 gap) |
| TOOL-06 | All enabled extensions produce function declarations | unit | `cd docker && python -m pytest tests/test_voice.py::TestToolDiscovery::test_all_enabled_extensions -x` | No (Wave 0 gap) |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd docker && python -m pytest tests/test_voice.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `tests/test_voice.py::TestToolDiscovery` -- unit tests for `_discover_voice_tools()` with mocked goosed `/config`
- [ ] `tests/test_voice.py::TestToolExecution` -- unit tests for `_voice_execute_tool()` with mocked `_do_rest_relay`
- [ ] `tests/test_voice.py::TestToolResponse` -- unit tests for `_voice_send_tool_response()` format validation
- [ ] `tests/test_voice.py::TestToolRelay` -- unit tests for tool_call interception in relay, browser notification

## Sources

### Primary (HIGH confidence)
- [Gemini Live API Tool Use](https://ai.google.dev/gemini-api/docs/live-api/tools) -- scheduling (SILENT/WHEN_IDLE/INTERRUPT), sync vs async, function declarations
- [Gemini Live API WebSocket Reference](https://ai.google.dev/api/live) -- toolCall/toolResponse/toolCallCancellation wire format
- [Gemini Live API Get Started (WebSocket)](https://ai.google.dev/gemini-api/docs/live-api/get-started-websocket) -- setup config with tools, toolResponse format
- [Gemini Cookbook WebSocket Tools Notebook](https://github.com/google-gemini/cookbook/blob/main/quickstarts/websockets/Get_started_LiveAPI_tools.ipynb) -- working toolResponse JSON format
- gateway.py source code analysis (lines 8966-9172) -- existing voice relay, message parser, Gemini config builder
- gateway.py source code analysis (lines 6843-6856) -- existing goosed `/config` extension reading pattern
- gateway.py source code analysis (lines 7393-7465) -- existing `_do_rest_relay()` for goosed communication

### Secondary (MEDIUM confidence)
- [Gemini Cookbook Issue #906](https://github.com/google-gemini/cookbook/issues/906) -- historical clientContent workaround for toolResponse (now fixed, use top-level toolResponse)
- [Block Goose Layered Tool Pattern](https://workos.com/blog/mcp-night-block-goose-layered-tool-pattern) -- generic tool declaration approach
- [Gemini Live API Examples](https://github.com/google-gemini/gemini-live-api-examples) -- reference implementations

### Tertiary (LOW confidence)
- goosed tool listing API -- goosed may expose a `/tools` endpoint but this is unverified. Falling back to `/config` extensions dict which is verified.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all components already exist in gateway.py, just need wiring
- Architecture: HIGH - patterns verified against existing code + Gemini docs
- Pitfalls: HIGH - derived from both Gemini docs (sync tool calling) and existing codebase analysis

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (Gemini 3.1 Flash Live may change behavior as it exits preview)

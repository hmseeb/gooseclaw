# Phase 33: Intelligence + History - Research

**Researched:** 2026-03-27
**Domain:** Voice session persistence, memory extraction pipeline, voice settings UI
**Confidence:** HIGH

## Summary

Phase 33 closes the loop on voice intelligence. Currently, voice transcripts are forwarded from Gemini to the browser and discarded server-side. No transcript is collected, stored, or fed into the memory pipeline. The voice name is hardcoded to "Aoede" with no user selection. This phase adds four features: (1) server-side transcript collection during voice relay, (2) auto-feed into memory via goosed/mem0 on session end, (3) voice session history UI with list + detail views, (4) voice selection from Gemini's 30 built-in voices.

All four requirements modify exactly two files: `gateway.py` (backend) and `voice.html` (frontend). The gateway needs transcript accumulation in the relay loop, JSON persistence to `/data/voice_sessions/`, a background thread to feed transcripts into goosed/mem0, and two new REST API endpoints (`GET /api/voice/sessions`, `GET /api/voice/sessions/<id>`). The voice.html needs a history panel, session detail view, and voice picker in settings. A third endpoint `GET /api/voice/voices` can serve the available voice list, though hardcoding it client-side is equally valid since the list is static.

**Primary recommendation:** Collect transcripts in `_voice_relay_gemini_to_browser`, save as JSON on session close, then fire a daemon thread that creates a goosed session and sends the transcript for memory extraction via `_do_rest_relay`. Add two API endpoints for session listing/detail, and a settings panel for voice selection (stored in setup.json or a voice-specific config file).

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| INTEL-01 | Voice conversation transcripts auto-feed into mem0 memory pipeline after session ends | Transcript collection in relay loop + goosed session with `_do_rest_relay` to trigger mem0 `memory_add` tool. Pattern: same as text channel sessions use goosed with MCP tools. |
| INTEL-02 | User can view list of past voice sessions with timestamps and transcript previews | JSON files in `/data/voice_sessions/` + `GET /api/voice/sessions` endpoint returning sorted list with metadata. voice.html gets a history panel. |
| INTEL-03 | User can tap a past session to view full transcript | `GET /api/voice/sessions/<id>` endpoint returning full transcript. voice.html gets a detail view. |
| INTEL-04 | User can select from available Gemini voices in voice dashboard settings | Gemini has 30 prebuilt voices. Settings panel in voice.html, preference stored server-side, passed to `_gemini_build_config` via voice_name parameter (already accepts it). |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib | 3.11+ | All backend (json, os, threading, time, uuid) | Project constraint: Python stdlib ONLY for gateway.py |
| Vanilla JS/HTML/CSS | N/A | All frontend (voice.html) | Project constraint: single HTML file, no build tooling |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| mem0 (via goosed MCP) | N/A | Memory extraction from transcripts | On session end, feed transcript through goosed session |
| Gemini Live API | models/gemini-3.1-flash-live-preview | Voice synthesis with configurable voices | Voice name passed in `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| JSON files for session storage | SQLite | JSON is simpler, matches existing patterns (jobs.json, watchers.json), good enough for single-user |
| goosed relay for memory | Direct mem0 Python API | Would add pip dependency. Using goosed keeps memory extraction through same pipeline as text channels. |
| Server-side voice list endpoint | Hardcoded client-side list | Server endpoint allows future dynamic updates, but voice list is static (30 voices). Either works. Prefer server-side for consistency. |

**Installation:**
No new dependencies. Everything uses existing stdlib + goosed MCP infrastructure.

## Architecture Patterns

### Data Storage Structure
```
/data/
  voice_sessions/
    {uuid}.json              # one file per voice session
```

### Voice Session JSON Schema
```json
{
  "id": "a1b2c3d4",
  "started_at": "2026-03-27T14:30:00Z",
  "ended_at": "2026-03-27T14:35:22Z",
  "duration_seconds": 322,
  "voice_name": "Aoede",
  "transcript": [
    {"speaker": "user", "text": "What's on my calendar?", "ts": 1711545000.0},
    {"speaker": "ai", "text": "Let me check your calendar...", "ts": 1711545002.5},
    {"speaker": "tool", "name": "google_calendar", "status": "done", "ts": 1711545004.0},
    {"speaker": "ai", "text": "You have a meeting at 3pm.", "ts": 1711545006.0}
  ],
  "memory_extracted": true,
  "preview": "What's on my calendar? / Let me check your calendar..."
}
```

### Pattern 1: Transcript Collection in Relay Loop
**What:** Accumulate transcript entries in `session_state` dict during `_voice_relay_gemini_to_browser`
**When to use:** Every voice session
**Example:**
```python
# In session_state initialization (handle_voice_ws):
session_state = {
    "gemini_sock": gemini_sock,
    "resumption_handle": None,
    "api_key": api_key,
    "tool_session_id": tool_session_id,
    "tool_name_map": tool_name_map,
    "_lock": threading.Lock(),
    # NEW: transcript collection
    "transcripts": [],
    "session_id": conn_id,
    "session_start": time.time(),
    "voice_name": voice_name,
}

# In _voice_relay_gemini_to_browser, after forwarding transcript to browser:
elif parsed["type"] == "transcript":
    ws_send_frame(browser_sock, WS_OP_TEXT,
        json.dumps(parsed).encode())
    # Collect transcript server-side
    session_state["transcripts"].append({
        "speaker": parsed["speaker"],
        "text": parsed["text"],
        "ts": time.time(),
    })

# For tool calls, also append tool status:
session_state["transcripts"].append({
    "speaker": "tool",
    "name": original_name,
    "status": status,
    "ts": time.time(),
})
```

### Pattern 2: Session Save + Memory Extraction on Close
**What:** In the `finally` block of `handle_voice_ws`, save transcript to JSON and spawn memory extraction thread
**When to use:** Every voice session close
**Example:**
```python
# In handle_voice_ws finally block, after socket cleanup:
transcripts = session_state.get("transcripts", [])
if transcripts:
    session_data = {
        "id": conn_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(session_state["session_start"])),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": int(time.time() - session_state["session_start"]),
        "voice_name": session_state.get("voice_name", "Aoede"),
        "transcript": transcripts,
        "memory_extracted": False,
        "preview": _voice_build_preview(transcripts),
    }
    _voice_save_session(session_data)
    # Fire memory extraction in background
    threading.Thread(
        target=_voice_extract_memory,
        args=(session_data,),
        daemon=True,
    ).start()
```

### Pattern 3: Memory Extraction via Goosed
**What:** Create a goosed session, send formatted transcript as prompt for memory extraction
**When to use:** After saving session JSON
**Example:**
```python
def _voice_extract_memory(session_data):
    """Feed voice transcript into mem0 via goosed session."""
    try:
        sid = _create_goose_session()
        if not sid:
            _voice_log.warning("Could not create session for voice memory extraction")
            return

        # Format transcript as conversation text
        lines = []
        for entry in session_data["transcript"]:
            if entry["speaker"] == "tool":
                continue
            speaker = "User" if entry["speaker"] == "user" else "AI"
            lines.append(f"{speaker}: {entry['text']}")

        conversation = "\n".join(lines)
        prompt = (
            "Extract and remember important facts from this voice conversation. "
            "Use memory_add for each important fact or preference.\n\n"
            f"Voice conversation ({session_data['started_at']}):\n{conversation}"
        )

        _do_rest_relay(prompt, sid, timeout=30)

        # Mark session as memory-extracted
        session_data["memory_extracted"] = True
        _voice_save_session(session_data)
        _voice_log.info(f"Memory extracted for voice session {session_data['id']}")
    except Exception as e:
        _voice_log.error(f"Voice memory extraction failed: {e}")
```

### Pattern 4: Voice Selection Persistence
**What:** Store user's preferred voice in a simple config file, read on session start
**When to use:** When user changes voice in settings
**Example:**
```python
# Voice preference file
_VOICE_PREFS_FILE = os.path.join(DATA_DIR, "voice_prefs.json")

def _get_voice_preference():
    """Load saved voice preference, default to Aoede."""
    try:
        with open(_VOICE_PREFS_FILE) as f:
            prefs = json.load(f)
            return prefs.get("voice_name", "Aoede")
    except (FileNotFoundError, json.JSONDecodeError):
        return "Aoede"

def _set_voice_preference(voice_name):
    """Save voice preference."""
    os.makedirs(os.path.dirname(_VOICE_PREFS_FILE) or ".", exist_ok=True)
    with open(_VOICE_PREFS_FILE, "w") as f:
        json.dump({"voice_name": voice_name}, f)
```

### Pattern 5: Voice Selection in WebSocket Handshake
**What:** Browser sends preferred voice name during WebSocket connection, gateway passes to Gemini
**When to use:** On voice session connect
**Example:**
```python
# In handle_voice_ws, extract voice preference from query params:
voice_name = query.get("voice", [None])[0] or _get_voice_preference()

# Pass to Gemini connect:
gemini_sock = _gemini_connect(api_key, tools=tools if tools else None, voice_name=voice_name)

# Update _gemini_connect to accept voice_name:
def _gemini_connect(api_key, resumption_handle=None, tools=None, voice_name="Aoede"):
    sock = ws_client_connect(...)
    config = _gemini_build_config(resumption_handle=resumption_handle, voice_name=voice_name, tools=tools)
    ...
```

### Anti-Patterns to Avoid
- **Blocking on memory extraction:** Memory extraction via goosed can take 10-30s. NEVER do this synchronously in the voice session close path. Always use a daemon thread.
- **Storing audio data in session JSON:** Only store transcripts (text). Audio data is enormous and unnecessary for history/memory.
- **Client-side session storage:** Use server-side JSON files. Client-side localStorage would be lost across devices and browsers.
- **Polling for session list:** The session list is only needed on page load or manual refresh. No WebSocket needed for this.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Memory extraction from transcript | Custom NLP/fact extraction | goosed + mem0 MCP `memory_add` tool | mem0 handles deduplication, contradiction resolution, knowledge graph. Using goosed ensures same LLM-driven extraction as text channels. |
| Transcript preview generation | Complex truncation logic | Simple first-user + first-AI concatenation (max 100 chars) | Preview is just for the list view. Keep it dead simple. |
| Voice name validation | Custom validation against hardcoded list | Simple inclusion check against known list | Gemini returns an error for invalid voices anyway. Belt-and-suspenders. |

**Key insight:** The memory pipeline already exists end-to-end via goosed + mem0 MCP extension. The only new work is collecting transcripts and sending them through the existing pipeline. Don't try to call mem0 Python APIs directly from gateway.py (that would add pip dependencies, violating the stdlib-only constraint).

## Common Pitfalls

### Pitfall 1: Transcript Interleaving During Concurrent Tool Calls
**What goes wrong:** Tool execution runs in background threads. Transcript entries from tool results can interleave with ongoing AI speech transcripts, creating garbled conversation order.
**Why it happens:** `_voice_relay_gemini_to_browser` processes messages in a single thread, but tool execution spawns separate threads that append to `session_state["transcripts"]` concurrently.
**How to avoid:** Use the existing `session_state["_lock"]` when appending to the transcripts list, OR use a thread-safe list/queue. Since Python's list.append is GIL-protected and atomic, simply appending is safe enough. But wrapping in the lock is safer for ordering guarantees.
**Warning signs:** Transcript entries with out-of-order timestamps.

### Pitfall 2: Session Save Race with Memory Extraction
**What goes wrong:** Memory extraction thread reads the session file before the save completes, or overwrites the file to mark `memory_extracted: true` while another save is in progress.
**Why it happens:** Concurrent file writes without locking.
**How to avoid:** Use atomic write pattern (write to `.tmp`, then `os.replace`). For the `memory_extracted` update, re-read the file, update the flag, and re-save atomically.
**Warning signs:** Truncated or empty session JSON files.

### Pitfall 3: Memory Extraction for Empty/Trivial Sessions
**What goes wrong:** User connects, says nothing, disconnects. Memory extraction runs on empty transcript, wastes a goosed session.
**Why it happens:** No minimum transcript length check.
**How to avoid:** Only trigger memory extraction if transcript has at least 2 entries (one user, one AI). Skip sessions under ~5 seconds duration.
**Warning signs:** Empty goosed sessions cluttering up session list.

### Pitfall 4: Voice Preference Not Applied After GoAway Reconnect
**What goes wrong:** After a GoAway reconnection, the voice name might revert to default.
**Why it happens:** `_voice_handle_goaway` calls `_gemini_connect` without passing the voice_name from session_state.
**How to avoid:** Store `voice_name` in `session_state` and pass it to `_gemini_connect` during GoAway reconnection.
**Warning signs:** Voice changes mid-conversation after ~10 minutes.

### Pitfall 5: Large Transcript Files for Long Sessions
**What goes wrong:** A 2-hour voice session produces a massive transcript JSON that slows down the session list API.
**Why it happens:** Gemini sends incremental transcript updates (not final-only), so each partial update adds an entry.
**How to avoid:** In the relay loop, UPDATE the last entry for the same speaker instead of appending a new one (mirror the browser-side behavior where `lastTranscriptSpeaker` tracks this). Only create a new entry when the speaker changes.
**Warning signs:** Session JSON files growing to megabytes for long conversations.

### Pitfall 6: Hardcoded Voice List Getting Stale
**What goes wrong:** Gemini adds new voices but the app still shows the old list.
**Why it happens:** Voice list hardcoded in client or server code.
**How to avoid:** This is acceptable for now. Voice list changes infrequently (~annually). Document the list source and add a comment for future updates. The 30-voice list from Gemini TTS docs is current as of March 2026.
**Warning signs:** Users report missing voices they see in AI Studio.

## Code Examples

### API Endpoint: List Voice Sessions
```python
# Source: Pattern matching existing gateway.py endpoints (jobs, watchers)
def handle_voice_sessions_list(self):
    """GET /api/voice/sessions - list past voice sessions."""
    if not check_auth(self):
        return
    sessions_dir = os.path.join(DATA_DIR, "voice_sessions")
    sessions = []
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                sessions.append({
                    "id": data["id"],
                    "started_at": data["started_at"],
                    "ended_at": data.get("ended_at", ""),
                    "duration_seconds": data.get("duration_seconds", 0),
                    "voice_name": data.get("voice_name", "Aoede"),
                    "preview": data.get("preview", ""),
                    "memory_extracted": data.get("memory_extracted", False),
                })
            except (json.JSONDecodeError, KeyError):
                continue
    # Sort by start time, newest first
    sessions.sort(key=lambda s: s["started_at"], reverse=True)
    self.send_json(200, {"sessions": sessions})
```

### API Endpoint: Get Voice Session Detail
```python
def handle_voice_session_detail(self, session_id):
    """GET /api/voice/sessions/<id> - get full transcript."""
    if not check_auth(self):
        return
    fpath = os.path.join(DATA_DIR, "voice_sessions", f"{session_id}.json")
    if not os.path.isfile(fpath):
        self.send_json(404, {"error": "Session not found"})
        return
    try:
        with open(fpath) as f:
            data = json.load(f)
        self.send_json(200, data)
    except (json.JSONDecodeError, IOError) as e:
        self.send_json(500, {"error": str(e)})
```

### Voice Save Helper
```python
_VOICE_SESSIONS_DIR = os.path.join(DATA_DIR, "voice_sessions")

def _voice_save_session(session_data):
    """Save voice session JSON atomically."""
    os.makedirs(_VOICE_SESSIONS_DIR, exist_ok=True)
    fpath = os.path.join(_VOICE_SESSIONS_DIR, f"{session_data['id']}.json")
    tmp = fpath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(session_data, f, indent=2)
    os.replace(tmp, fpath)

def _voice_build_preview(transcripts, max_len=100):
    """Build a short preview string from transcript entries."""
    parts = []
    for entry in transcripts:
        if entry["speaker"] in ("user", "ai") and entry.get("text"):
            parts.append(entry["text"])
            if len(" / ".join(parts)) > max_len:
                break
    preview = " / ".join(parts)
    return preview[:max_len] + "..." if len(preview) > max_len else preview
```

### Frontend: History Panel (voice.html)
```html
<!-- History toggle button in header -->
<button id="history-btn" onclick="toggleHistory()">History</button>

<!-- History panel (hidden by default) -->
<div id="history-panel" style="display:none">
  <div id="history-list"></div>
</div>

<script>
async function loadHistory() {
  var resp = await fetch('/api/voice/sessions');
  var data = await resp.json();
  var list = document.getElementById('history-list');
  list.innerHTML = '';
  data.sessions.forEach(function(s) {
    var el = document.createElement('div');
    el.className = 'history-item';
    el.innerHTML = '<div class="history-date">' + new Date(s.started_at).toLocaleString() + '</div>' +
                   '<div class="history-preview">' + escapeHtml(s.preview) + '</div>';
    el.onclick = function() { viewSession(s.id); };
    list.appendChild(el);
  });
}

async function viewSession(id) {
  var resp = await fetch('/api/voice/sessions/' + id);
  var data = await resp.json();
  // Render full transcript in detail view
  showSessionDetail(data);
}
</script>
```

### Gemini Voice Names (Complete List)
```python
# Source: https://ai.google.dev/gemini-api/docs/speech-generation
GEMINI_VOICES = [
    {"name": "Zephyr", "style": "Bright"},
    {"name": "Puck", "style": "Upbeat"},
    {"name": "Charon", "style": "Informative"},
    {"name": "Kore", "style": "Firm"},
    {"name": "Fenrir", "style": "Excitable"},
    {"name": "Leda", "style": "Youthful"},
    {"name": "Orus", "style": "Firm"},
    {"name": "Aoede", "style": "Breezy"},
    {"name": "Callirrhoe", "style": "Easy-going"},
    {"name": "Autonoe", "style": "Bright"},
    {"name": "Enceladus", "style": "Breathy"},
    {"name": "Iapetus", "style": "Clear"},
    {"name": "Umbriel", "style": "Easy-going"},
    {"name": "Algieba", "style": "Smooth"},
    {"name": "Despina", "style": "Smooth"},
    {"name": "Erinome", "style": "Clear"},
    {"name": "Algenib", "style": "Gravelly"},
    {"name": "Rasalgethi", "style": "Informative"},
    {"name": "Laomedeia", "style": "Upbeat"},
    {"name": "Achernar", "style": "Soft"},
    {"name": "Alnilam", "style": "Firm"},
    {"name": "Schedar", "style": "Even"},
    {"name": "Gacrux", "style": "Mature"},
    {"name": "Pulcherrima", "style": "Forward"},
    {"name": "Achird", "style": "Friendly"},
    {"name": "Zubenelgenubi", "style": "Casual"},
    {"name": "Vindemiatrix", "style": "Gentle"},
    {"name": "Sadachbia", "style": "Lively"},
    {"name": "Sadaltager", "style": "Knowledgeable"},
    {"name": "Sulafat", "style": "Warm"},
]
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Transcripts forwarded to browser, discarded server-side | Collect in relay, persist as JSON, feed to memory | Phase 33 (new) | Voice sessions become first-class memory sources |
| Hardcoded voice_name="Aoede" | User-selectable from 30 voices via settings UI | Phase 33 (new) | Personalization of voice experience |
| No session history | JSON files + REST API + history panel | Phase 33 (new) | Users can review past voice conversations |

**Deprecated/outdated:**
- None. This is all new functionality.

## Open Questions

1. **Session cleanup/retention policy**
   - What we know: JSON files accumulate over time in `/data/voice_sessions/`
   - What's unclear: Should there be a max session count or age-based cleanup?
   - Recommendation: Don't implement cleanup in Phase 33. Add a TODO. Single-user with text transcripts only won't produce meaningful storage pressure. Can address in future if needed.

2. **Voice preference scope**
   - What we know: Single-user system, so one global preference is fine
   - What's unclear: Should voice preference be per-session (selectable on connect) or global (settings only)?
   - Recommendation: Global preference in settings. Can override per-session via query param if desired. Keep it simple.

3. **Memory extraction prompt quality**
   - What we know: The prompt format matters for how well mem0 extracts facts
   - What's unclear: Optimal prompt phrasing for voice conversation extraction
   - Recommendation: Start with a simple prompt (shown in code examples). Iterate based on quality of extracted memories. The goosed LLM + mem0 pipeline handles the heavy lifting.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (Python stdlib) |
| Config file | none (standard unittest discovery) |
| Quick run command | `cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -x -q --tb=short -k "voice"` |
| Full suite command | `cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -x -q --tb=short` |
| Estimated runtime | ~5 seconds |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INTEL-01 | Transcript collection in relay + memory extraction thread spawn | unit | `python -m pytest docker/test_gateway.py -x -k "test_voice_transcript_collect or test_voice_memory_extract"` | No - Wave 0 gap |
| INTEL-02 | Session list API returns sorted sessions with previews | unit | `python -m pytest docker/test_gateway.py -x -k "test_voice_sessions_list"` | No - Wave 0 gap |
| INTEL-03 | Session detail API returns full transcript | unit | `python -m pytest docker/test_gateway.py -x -k "test_voice_session_detail"` | No - Wave 0 gap |
| INTEL-04 | Voice preference save/load + passed to Gemini config | unit | `python -m pytest docker/test_gateway.py -x -k "test_voice_preference"` | No - Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task, run: `cd /Users/haseeb/nix-template && python -m pytest docker/test_gateway.py -x -q --tb=short -k "voice"`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~5 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] `docker/test_gateway.py` (append to existing) - Tests for `_voice_save_session`, `_voice_build_preview`, `_voice_extract_memory` (mocked goosed), voice preference get/set, session list/detail API handlers, transcript collection in relay, Gemini config with voice_name
- [ ] No framework install needed (unittest already available)

## Sources

### Primary (HIGH confidence)
- Codebase analysis: `docker/gateway.py` (lines 8940-9700) - voice WebSocket handler, relay loops, session state, Gemini config
- Codebase analysis: `docker/voice.html` (1166 lines) - complete frontend, transcript display, state machine
- Codebase analysis: `docker/memory/server.py` - mem0 MCP extension with `memory_add` tool
- [Gemini TTS Voice List](https://ai.google.dev/gemini-api/docs/speech-generation) - All 30 available voice names with style descriptors
- [Gemini Live API Guide](https://ai.google.dev/gemini-api/docs/live-guide) - speechConfig, prebuiltVoiceConfig configuration

### Secondary (MEDIUM confidence)
- Codebase patterns: `_do_rest_relay` (line 7393), `_create_goose_session` (line 6960) - existing patterns for goosed interaction
- Codebase patterns: Job engine (`_JOBS_FILE`), Watchers (`_WATCHERS_FILE`) - JSON persistence patterns on /data volume

### Tertiary (LOW confidence)
- None. All findings based on codebase analysis and official Gemini docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all stdlib Python + vanilla JS (project constraints), no new dependencies
- Architecture: HIGH - patterns directly derived from existing gateway.py code (jobs.json, session manager, _do_rest_relay)
- Pitfalls: HIGH - identified from reading the actual relay loop code and understanding threading model

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (30 days - stable domain, no API changes expected)

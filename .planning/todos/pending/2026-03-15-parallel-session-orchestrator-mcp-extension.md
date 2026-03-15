---
created: 2026-03-15T23:30:00.000Z
title: "Parallel session orchestrator"
area: general
files:
  - docker/gateway.py
  - docker/knowledge/server.py
---

## Problem

Goose is single-process, single-agent. It can't fan out work to multiple parallel workers. But goosed already supports multiple concurrent sessions via `/agent/start` with session-scoped isolation. This capability is untapped from the bot's perspective.

## Solution

**DECISION: Build as gateway API endpoints, NOT an MCP extension.**

The gateway already has all the session/relay machinery. An MCP extension would just duplicate it. Add new endpoints to gateway.py that reuse existing functions.

### API Design

```
POST /api/parallel_run
{
  "tasks": ["research X", "research Y", "research Z"],
  "timeout": 120,
  "max_concurrent": 3,
  "model": "optional-model-override"
}

Response:
{
  "completed": 2,
  "failed": 1,
  "total": 3,
  "results": [
    {"task": "research X", "session_id": "abc", "status": "ok", "result": "..."},
    {"task": "research Y", "session_id": "def", "status": "ok", "result": "..."},
    {"task": "research Z", "session_id": "ghi", "status": "error", "error": "timeout after 120s"}
  ]
}

POST /api/session_followup
{"session_id": "abc", "message": "dig deeper on point 3"}

POST /api/session_cleanup
{"session_ids": ["abc", "def", "ghi"]}
```

### Implementation Plan

Reuse these existing gateway functions (no reimplementation needed):
- `_create_goose_session()` (line 6259) — creates session via POST /agent/start
- `_set_session_default_provider(session_id)` (line 6222) — sets model on session
- `_update_goose_session_provider(session_id, model_config)` (line 6628) — hot-swap model
- `_do_rest_relay(session_id, ...)` (line 6882) — send message + parse SSE response
- `_parse_sse_events(response)` (line 6806) — SSE event parser
- `_extract_response_content(content)` (line 6851) — extract text/media from blocks

### Key Implementation Details

**Session creation + relay:**
```python
# create session
sid = _create_goose_session()  # POST /agent/start, returns session_id

# optionally override model
_update_goose_session_provider(sid, {"provider": "anthropic", "model": "claude-sonnet"})

# send task
text, error, media = _do_rest_relay(
    session_id=sid,
    user_text=f"TASK: {task}\n\nComplete this task concisely. Return only the result.",
    # ... timeout=120
)
```

**Fan-out pattern:**
```python
# threading.Thread per task, up to max_concurrent
# queue excess tasks, start as slots free up
# collect results into structured JSON
# return partial results + errors (don't fail-all on one failure)
```

**Auth:** `X-Secret-Key: {_INTERNAL_GOOSE_TOKEN}` on all internal calls
**Connection:** `https://127.0.0.1:{GOOSE_WEB_PORT}`, disabled cert verification

### Constraints Discovered

1. **No custom system prompt API** — `/agent/update_provider` only sets provider/model, not system prompts. Spawned sessions inherit full bot identity. Task instruction must be clear enough to keep session focused.
2. **No DELETE session endpoint** — sessions persist until goosed restart. `session_cleanup` can only clear local tracking, not kill goosed sessions. Accept the leak.
3. **No per-session locking in goosed** — serialize at caller level. One relay per session at a time.
4. **SSE relay is blocking** — but MCP/gateway bypass per-user gateway locks. Concurrent `/reply` to different sessions works fine.
5. **Sessions inherit lead/worker config** — spawned workers get same lead/worker model routing as main session. This composes nicely.

### Relationship to Existing Features

| Feature | What it does | How it relates |
|---------|-------------|----------------|
| Lead/worker (`GOOSE_LEAD_*`) | Sequential model swap after N turns in one session | Orthogonal — each spawned session inherits this |
| Channel routing (`channel_routes`) | Per-channel model assignment | Can reuse `_update_goose_session_provider` to set worker models |
| Session model cache (`_session_model_cache`) | Avoids redundant update_provider calls | Workers benefit from this automatically |

### Use Cases

- "find me 3 cool AI tools" — 3 parallel research sessions
- "check all my integrations are working" — parallel health checks
- "summarize these 5 articles" — fan-out summarization
- any task that's embarrassingly parallel

### Status

**Parked.** No real use case driving this yet. Build when there's actual demand for parallel fan-out. All research is done, implementation should be straightforward — just wire up existing gateway functions behind new API endpoints.

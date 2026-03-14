# Phase 16: Watcher Engine - Research

**Researched:** 2026-03-14
**Domain:** Real-time event subscriptions, webhook gateway, polling with diffing, template-based notification routing
**Confidence:** HIGH

## Summary

The watcher engine is a new subsystem that follows the established job engine pattern almost exactly. The codebase already has all the primitives needed: a background daemon thread with a tick loop (job engine at 10s, cron scheduler at 30s), JSON file persistence (jobs.json, schedule.json), thread-safe list management with locks, the notify_all() notification bus with per-channel targeting, OutboundAdapter for rich delivery, and the _relay_to_goose_web() function for LLM processing.

The three watcher input types map cleanly to existing patterns. Webhooks are new HTTP routes on the existing ThreadingHTTPServer. Feeds are polling loops (like the cron scheduler) with diffing via hashlib (already imported). Streams (websocket/SSE) are persistent threads like the telegram poll loop. The two-tier architecture is straightforward: tier 1 (passthrough) does string.Template substitution on incoming data and calls notify_all(); tier 2 (smart) creates a goose session, relays the data through _relay_to_goose_web() with a user-defined prompt, and notifies with the LLM response.

**Primary recommendation:** Model the watcher engine as a direct analog of the job engine. Same file structure (watchers.json), same CRUD functions (create_watcher/list_watchers/delete_watcher), same daemon thread pattern (_watcher_engine_loop), same API pattern (/api/watchers), same CLI registration on _command_router.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib only | 3.x | All implementation | Project constraint - no pip dependencies |
| threading | stdlib | Background loops, per-watcher threads | Already used for job engine, cron, telegram poll |
| json | stdlib | watchers.json persistence | Same pattern as jobs.json |
| hashlib | stdlib | Content diffing for feed watchers | Already imported in gateway.py |
| string.Template | stdlib | Tier 1 passthrough template rendering | Safe, simple, no Jinja dependency needed |
| urllib.request | stdlib | Feed polling (HTTP GET) | Already used throughout for API calls |
| xml.etree.ElementTree | stdlib | RSS/Atom feed parsing | Stdlib XML parser, no feedparser dependency |
| http.server | stdlib | Webhook endpoint routing | Already the server framework |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| uuid | stdlib | Watcher ID generation | Same as job engine |
| time | stdlib | Timestamps, tick loops | Same as job engine |
| re | stdlib | Pattern matching in feed diffing | Already imported |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| string.Template | str.format_map | Template uses $var or ${var} syntax which is safer (no KeyError on missing keys with safe_substitute) |
| xml.etree | html.parser | etree handles RSS/Atom natively, html.parser would need manual extraction |
| hashlib.sha256 for diffing | difflib | sha256 is cheaper for "did it change?" checks; difflib useful only if you need the actual diff |

## Architecture Patterns

### Recommended Project Structure
```
gateway.py additions:
  # ── watcher engine ──────────────────────────────
  _WATCHERS_FILE = os.path.join(DATA_DIR, "watchers.json")
  _watchers = []
  _watchers_lock = threading.Lock()
  _watcher_threads = {}  # id -> Thread (for stream watchers)
  _watcher_engine_running = False

  # CRUD functions
  create_watcher(data) -> (watcher, error)
  update_watcher(id, updates) -> (watcher, error)
  delete_watcher(id) -> bool
  list_watchers() -> list

  # Engine
  _watcher_engine_loop()     # background tick for feed polling
  start_watcher_engine()     # starts daemon thread

  # Webhook handling
  _handle_webhook(name, payload)  # called from do_POST

  # Tier processing
  _process_watcher_passthrough(watcher, data) -> str
  _process_watcher_smart(watcher, data) -> str
```

### Pattern 1: Watcher Data Model
**What:** The watcher config dict, persisted in watchers.json
**When to use:** All watcher operations
**Example:**
```python
watcher = {
    "id": "gh-prs",                          # unique ID (auto-generated or user-provided)
    "name": "GitHub PRs",                    # human-readable label
    "type": "webhook",                       # webhook | feed | stream
    "source": "/api/webhooks/gh",            # webhook path, feed URL, or stream URL
    "channel": "telegram:main",             # delivery target (notify_all channel param)
    "smart": False,                          # tier 1 (passthrough) vs tier 2 (LLM)
    "transform": "${repo}: ${action} on PR #${number} by ${user}",  # string.Template pattern
    "prompt": "",                            # only used if smart=True
    "enabled": True,
    "created_at": "2026-03-14T00:00:00Z",
    # feed-specific fields:
    "poll_seconds": 300,                     # polling interval for feeds (default 5min)
    "last_hash": "",                         # sha256 of last fetched content
    "last_check": None,                      # timestamp of last poll
    "filter": "",                            # optional regex filter for feed items
    # stream-specific fields:
    "headers": {},                           # custom headers for stream connections
    # runtime state:
    "last_fired": None,
    "fire_count": 0,
    "last_error": None,
}
```

### Pattern 2: Webhook Gateway
**What:** Expose `/api/webhooks/<name>` endpoints that route incoming POSTs to matching watchers
**When to use:** External services (GitHub, Vercel, Stripe) POST events
**Example:**
```python
def do_POST(self):
    path = urllib.parse.urlparse(self.path).path
    # ... existing routes ...
    elif path.startswith("/api/webhooks/"):
        webhook_name = path[len("/api/webhooks/"):]
        self.handle_webhook_incoming(webhook_name)

def handle_webhook_incoming(self, webhook_name):
    """Receive external webhook, route to matching watchers."""
    body = self._read_body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body.decode("utf-8", errors="replace")}

    # find all watchers listening on this webhook
    with _watchers_lock:
        matches = [w for w in _watchers
                   if w["type"] == "webhook"
                   and w.get("enabled", True)
                   and w["source"].rstrip("/").endswith(webhook_name)]

    if not matches:
        self.send_json(404, {"error": f"no watchers for webhook '{webhook_name}'"})
        return

    for watcher in matches:
        threading.Thread(
            target=_fire_watcher, args=(watcher, payload), daemon=True
        ).start()

    self.send_json(200, {"accepted": True, "watchers": len(matches)})
```

### Pattern 3: Tier 1 Passthrough Processing
**What:** Template substitution with no LLM involvement
**When to use:** smart=False watchers
**Example:**
```python
from string import Template

def _process_passthrough(watcher, data):
    """Tier 1: template transform, no LLM. Returns formatted string."""
    tmpl = watcher.get("transform", "")
    if not tmpl:
        # no template -> JSON dump of payload
        return json.dumps(data, indent=2)[:2000]

    # flatten nested dicts for template access
    flat = _flatten_dict(data)
    t = Template(tmpl)
    return t.safe_substitute(flat)

def _flatten_dict(d, prefix="", sep="_"):
    """Flatten nested dict: {"a": {"b": 1}} -> {"a_b": 1}"""
    items = {}
    for k, v in d.items():
        key = f"{prefix}{sep}{k}" if prefix else k
        if isinstance(v, dict):
            items.update(_flatten_dict(v, key, sep))
        else:
            items[str(k)] = str(v)  # also keep unflattened for simple templates
            items[key] = str(v)
    return items
```

### Pattern 4: Tier 2 Smart Processing
**What:** LLM processes the incoming data with a user-defined prompt
**When to use:** smart=True watchers
**Example:**
```python
def _process_smart(watcher, data):
    """Tier 2: LLM processing. Creates a session, relays, returns response."""
    prompt = watcher.get("prompt", "summarize this data")
    payload_text = json.dumps(data, indent=2)[:4000]
    user_text = f"{prompt}\n\n---\nData:\n{payload_text}"

    session_id = _create_goose_session()
    if not session_id:
        return f"[watcher:{watcher['id']}] error: could not create goose session"

    response_text, error, _ = _relay_to_goose_web(
        user_text, session_id, channel=watcher.get("channel")
    )
    if error:
        return f"[watcher:{watcher['id']}] error: {error}"
    return response_text
```

### Pattern 5: Feed Polling with Diffing
**What:** Periodically fetch a URL, detect changes, fire watcher only on change
**When to use:** type="feed" watchers (RSS, API endpoints, price checks)
**Example:**
```python
def _check_feed_watcher(watcher):
    """Poll a feed URL, fire if content changed."""
    url = watcher["source"]
    try:
        req = urllib.request.Request(url, headers=watcher.get("headers", {}))
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
    except Exception as e:
        watcher["last_error"] = str(e)
        return

    content_hash = hashlib.sha256(content).hexdigest()
    watcher["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if content_hash == watcher.get("last_hash"):
        return  # no change

    watcher["last_hash"] = content_hash

    # parse content (try JSON first, then RSS/XML, then raw text)
    data = _parse_feed_content(content, url)

    # apply filter if set
    filter_re = watcher.get("filter")
    if filter_re and isinstance(data, list):
        data = [item for item in data if re.search(filter_re, json.dumps(item), re.IGNORECASE)]
        if not data:
            return  # nothing matched filter

    _fire_watcher(watcher, data)
```

### Pattern 6: Watcher Engine Loop
**What:** Background daemon thread that ticks feed watchers
**When to use:** Always running when watchers exist
**Example:**
```python
_WATCHER_TICK_SECONDS = 30  # check feeds every 30s

def _watcher_engine_loop():
    """Background loop: poll feed watchers, manage stream threads."""
    global _watcher_engine_running
    _watcher_engine_running = True
    print(f"[watchers] engine started ({_WATCHER_TICK_SECONDS}s tick)")

    while _watcher_engine_running:
        try:
            now = time.time()
            with _watchers_lock:
                snapshot = [w for w in _watchers
                            if w["type"] == "feed" and w.get("enabled", True)]

            for watcher in snapshot:
                interval = watcher.get("poll_seconds", 300)
                last = watcher.get("last_check_ts", 0)
                if now - last >= interval:
                    watcher["last_check_ts"] = now
                    threading.Thread(
                        target=_check_feed_watcher, args=(watcher,), daemon=True
                    ).start()

        except Exception as e:
            print(f"[watchers] error: {e}")

        for _ in range(6):  # 30s sleep, checking shutdown every 5s
            if not _watcher_engine_running:
                break
            time.sleep(5)
```

### Anti-Patterns to Avoid
- **Global imports of external libraries:** No feedparser, no requests, no aiohttp. stdlib only.
- **Blocking the main thread:** All watcher processing must happen in daemon threads.
- **Unbounded memory:** Feed content and webhook payloads must be truncated (2-4KB max per event).
- **Polling too frequently:** Default feed poll interval should be 300s (5min), minimum 60s.
- **Smart watchers without rate limiting:** LLM calls cost money. Cap smart watcher fires per minute.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Template rendering | Custom regex substitution | string.Template.safe_substitute() | Handles missing keys gracefully, well-tested |
| Content hashing | Custom comparison logic | hashlib.sha256() | Already imported, deterministic, fast |
| JSON flattening | Recursive string concat | Simple _flatten_dict helper | Keep it simple, max 2 levels deep |
| RSS parsing | Custom regex on XML | xml.etree.ElementTree | Handles namespaces, encoding, malformed XML |
| HTTP requests | Raw socket | urllib.request.urlopen | Already used throughout codebase |
| Thread management | Custom thread pool | threading.Thread(daemon=True) | Matches job engine pattern exactly |

**Key insight:** The codebase already solved all the hard infrastructure problems (notification routing, channel targeting, LLM relay, session management). The watcher engine is purely a new input mechanism that feeds into existing outputs.

## Common Pitfalls

### Pitfall 1: Webhook Authentication
**What goes wrong:** External services send webhooks without verification, anyone can trigger watchers.
**Why it happens:** Webhook endpoints are public by design (GitHub/Vercel need to reach them).
**How to avoid:** Add optional webhook_secret field per watcher. If set, verify HMAC-SHA256 signature header (X-Hub-Signature-256 for GitHub, similar for others). If not set, accept all (simpler setup, user's choice).
**Warning signs:** Spam notifications from unknown sources.

### Pitfall 2: Feed Polling Thundering Herd
**What goes wrong:** All feed watchers poll at the same time, creating a burst of HTTP requests.
**Why it happens:** All feeds start with last_check_ts=0, so they all fire on the first tick.
**How to avoid:** Stagger initial polls by adding a random jitter (0 to poll_seconds) on first check.
**Warning signs:** Burst of "[watchers] error: timeout" messages at startup.

### Pitfall 3: Smart Watcher Session Accumulation
**What goes wrong:** Each smart watcher fire creates a new goose session that never gets cleaned up.
**Why it happens:** _create_goose_session() creates sessions but nothing cleans them.
**How to avoid:** Use a single reusable session per smart watcher (stored in watcher dict). Create on first fire, reuse afterward. If it goes stale, create a new one (same retry pattern as _relay_to_goose_web).
**Warning signs:** Memory growth, slow goose responses.

### Pitfall 4: Template Key Mismatches
**What goes wrong:** User writes `${repo}` but webhook payload has `repository.full_name`.
**Why it happens:** Webhook payloads are deeply nested, templates expect flat keys.
**How to avoid:** Flatten the payload dict AND provide both nested keys (repository_full_name) and leaf keys (full_name, repo). Document the flattening behavior. safe_substitute() handles missing keys by leaving them as-is.
**Warning signs:** Notifications showing literal `${repo}` instead of values.

### Pitfall 5: Stream Reconnection
**What goes wrong:** SSE/websocket stream disconnects and watcher goes silent.
**Why it happens:** Network hiccups, server restarts, connection timeouts.
**How to avoid:** Stream watcher threads should have exponential backoff reconnection (1s, 2s, 4s, 8s, max 60s). Track connection state in watcher dict.
**Warning signs:** Stream watcher shows "connected" but no events for a long time.

### Pitfall 6: Webhook Path Collisions with Existing API
**What goes wrong:** `/api/webhooks/health` accidentally shadows `/api/health`.
**Why it happens:** Webhook names chosen by users overlap with API namespace.
**How to avoid:** Webhook routes are under `/api/webhooks/` prefix, completely separate from `/api/` routes. The prefix ensures no collision. Validate that webhook names don't contain slashes.
**Warning signs:** 404s on existing endpoints.

## Code Examples

### Watcher CRUD (matching job engine pattern)
```python
# Source: modeled after gateway.py create_job() (line 3130)
_WATCHERS_FILE = os.path.join(DATA_DIR, "watchers.json")
_watchers = []
_watchers_lock = threading.Lock()

def _load_watchers():
    global _watchers
    if not os.path.exists(_WATCHERS_FILE):
        return
    try:
        with open(_WATCHERS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            with _watchers_lock:
                _watchers = data
            print(f"[watchers] loaded {len(data)} watcher(s)")
    except Exception as e:
        print(f"[watchers] warn: could not load watchers.json: {e}")

def _save_watchers():
    with _watchers_lock:
        data = list(_watchers)
    try:
        tmp = _WATCHERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _WATCHERS_FILE)
    except Exception as e:
        print(f"[watchers] warn: could not save watchers.json: {e}")

def create_watcher(data):
    """Create a new watcher. Returns (watcher_dict, error_string)."""
    watcher_id = data.get("id") or str(uuid.uuid4())[:8]
    with _watchers_lock:
        if any(w["id"] == watcher_id for w in _watchers):
            return None, f"watcher with id '{watcher_id}' already exists"

    watcher_type = data.get("type", "webhook")
    if watcher_type not in ("webhook", "feed", "stream"):
        return None, f"invalid type: {watcher_type} (must be webhook, feed, or stream)"

    if watcher_type == "feed" and not data.get("source"):
        return None, "source URL is required for feed watchers"

    watcher = {
        "id": watcher_id,
        "name": data.get("name", watcher_id),
        "type": watcher_type,
        "source": data.get("source", f"/api/webhooks/{watcher_id}"),
        "channel": data.get("channel"),  # None = broadcast to all
        "smart": data.get("smart", False),
        "transform": data.get("transform", ""),
        "prompt": data.get("prompt", ""),
        "enabled": data.get("enabled", True),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "poll_seconds": data.get("poll_seconds", 300),
        "filter": data.get("filter", ""),
        "headers": data.get("headers", {}),
        "webhook_secret": data.get("webhook_secret", ""),
        "last_hash": "",
        "last_check": None,
        "last_fired": None,
        "fire_count": 0,
        "last_error": None,
    }

    with _watchers_lock:
        _watchers.append(watcher)
    _save_watchers()
    print(f"[watchers] created: {watcher['name']} ({watcher_id}) type={watcher_type}")
    return watcher, ""
```

### Fire Watcher (tier dispatch)
```python
def _fire_watcher(watcher, data):
    """Process incoming data through watcher tier and deliver notification."""
    try:
        if watcher.get("smart"):
            message = _process_smart(watcher, data)
        else:
            message = _process_passthrough(watcher, data)

        if not message:
            return

        prefix = f"[{watcher.get('name', watcher['id'])}]"
        full_message = f"{prefix} {message}"

        notify_all(full_message, channel=watcher.get("channel"))

        watcher["last_fired"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        watcher["fire_count"] = watcher.get("fire_count", 0) + 1
        watcher["last_error"] = None
        _save_watchers()

    except Exception as e:
        watcher["last_error"] = str(e)
        print(f"[watchers] error firing {watcher['id']}: {e}")
```

### RSS Feed Parser (stdlib only)
```python
def _parse_rss(content):
    """Parse RSS/Atom feed XML into a list of item dicts."""
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(content)
        # RSS 2.0
        for item in root.iter("item"):
            items.append({
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "description": (item.findtext("description") or "").strip()[:500],
                "pubDate": (item.findtext("pubDate") or "").strip(),
            })
        # Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            link_el = entry.find("atom:link", ns)
            items.append({
                "title": (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip(),
                "link": link_el.get("href", "") if link_el is not None else "",
                "description": (entry.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()[:500],
            })
    except ET.ParseError:
        pass
    return items
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| External webhook services (Zapier, IFTTT) | Self-hosted webhook gateway | Always (GooseClaw is self-hosted) | Zero external dependency |
| pip install feedparser | xml.etree.ElementTree stdlib | Project constraint | Handles 95% of RSS/Atom feeds |
| Jinja2 templating | string.Template | Project constraint (stdlib only) | Simpler syntax ($var vs {{var}}), safe_substitute for missing keys |
| asyncio for streams | threading.Thread daemon | Project constraint | Matches existing codebase patterns |

**Note on template syntax:** The user's design doc shows `{{repo}}` Jinja-style syntax. Since we're stdlib-only, use `${repo}` (string.Template syntax) or implement a simple `{{var}}` -> value replacement with regex. Recommend supporting both: try `{{var}}` first (user-friendly), fall back to `${var}` (stdlib Template). A simple regex replace can handle `{{var}}` before passing to Template.

## Open Questions

1. **Stream watcher scope for v1**
   - What we know: The design calls for persistent websocket/SSE connections.
   - What's unclear: Which streaming APIs the user wants to support. Generic SSE is simpler than websockets (stdlib has no websocket client).
   - Recommendation: Start with SSE only (doable with urllib.request + chunked reading). Defer websocket streams to a follow-up. SSE covers most real-time APIs (GitHub events, Vercel logs, price feeds).

2. **Webhook authentication schemes**
   - What we know: GitHub uses HMAC-SHA256, Vercel uses a shared secret header, Stripe uses webhook signatures.
   - What's unclear: Whether to support multiple auth schemes or just one.
   - Recommendation: Support optional `webhook_secret` field. If set, check `X-Hub-Signature-256` header (GitHub's standard, most common). Document how to adapt for other providers. Keep it simple for v1.

3. **Template syntax: {{var}} vs ${var}**
   - What we know: User's design shows `{{var}}` syntax. stdlib Template uses `${var}`.
   - What's unclear: Whether to add a thin {{var}} adapter or just use ${var}.
   - Recommendation: Support `{{var}}` with a simple regex pre-processor that converts to `${var}` before passing to string.Template. Best of both worlds, 3 lines of code.

4. **Rate limiting smart watchers**
   - What we know: Each smart watcher fire costs an LLM call.
   - What's unclear: What rate limit is appropriate.
   - Recommendation: Default to max 1 smart fire per watcher per 60 seconds. User can override with `min_interval` field. Passthrough watchers have no rate limit.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | unittest (stdlib) |
| Config file | none (run directly) |
| Quick run command | `cd docker && python -m pytest test_gateway.py -x -q --tb=short` |
| Full suite command | `cd docker && python -m pytest test_gateway.py -q --tb=short` |
| Estimated runtime | ~15 seconds |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| WATCH-01 | create_watcher validates type, source, returns watcher dict | unit | `cd docker && python -m pytest test_gateway.py -k TestCreateWatcher -x` | No - Wave 0 gap |
| WATCH-02 | delete_watcher removes from list, saves JSON | unit | `cd docker && python -m pytest test_gateway.py -k TestDeleteWatcher -x` | No - Wave 0 gap |
| WATCH-03 | list_watchers returns enabled watchers | unit | `cd docker && python -m pytest test_gateway.py -k TestListWatchers -x` | No - Wave 0 gap |
| WATCH-04 | _process_passthrough renders templates with safe_substitute | unit | `cd docker && python -m pytest test_gateway.py -k TestPassthroughProcess -x` | No - Wave 0 gap |
| WATCH-05 | _process_smart relays to goose, returns LLM response | unit | `cd docker && python -m pytest test_gateway.py -k TestSmartProcess -x` | No - Wave 0 gap |
| WATCH-06 | handle_webhook_incoming routes payload to matching watchers | unit | `cd docker && python -m pytest test_gateway.py -k TestWebhookRouting -x` | No - Wave 0 gap |
| WATCH-07 | _check_feed_watcher detects changes via hash, fires on change | unit | `cd docker && python -m pytest test_gateway.py -k TestFeedWatcher -x` | No - Wave 0 gap |
| WATCH-08 | _fire_watcher dispatches to correct tier, calls notify_all | unit | `cd docker && python -m pytest test_gateway.py -k TestFireWatcher -x` | No - Wave 0 gap |
| WATCH-09 | Webhook endpoint returns 200 on match, 404 on no match | unit | `cd docker && python -m pytest test_gateway.py -k TestWebhookEndpoint -x` | No - Wave 0 gap |
| WATCH-10 | API endpoints CRUD /api/watchers | unit | `cd docker && python -m pytest test_gateway.py -k TestWatcherAPI -x` | No - Wave 0 gap |

### Nyquist Sampling Rate
- **Minimum sample interval:** After every committed task -> run: `cd docker && python -m pytest test_gateway.py -x -q --tb=short`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before /gsd:verify-work runs
- **Estimated feedback latency per task:** ~15 seconds

### Wave 0 Gaps (must be created before implementation)
- [ ] Test classes in `docker/test_gateway.py` for WATCH-01 through WATCH-10
- [ ] Mock helpers for _save_watchers, _relay_to_goose_web (pattern matches existing _save_jobs mock)

## Sources

### Primary (HIGH confidence)
- gateway.py source code (8400+ lines) - direct inspection of job engine, notification bus, relay functions, HTTP routing
- test_gateway.py source code (6900+ lines) - direct inspection of test patterns, mock strategies
- Python stdlib docs: string.Template, xml.etree.ElementTree, hashlib, urllib.request

### Secondary (MEDIUM confidence)
- Existing codebase patterns: job engine loop (line 3490), cron scheduler loop (line 3857), telegram poll loop, CRUD functions

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - stdlib-only constraint is clear, all libraries already used in codebase
- Architecture: HIGH - direct analog of existing job engine pattern, all primitives exist
- Pitfalls: MEDIUM - stream reconnection and smart watcher session management are less tested patterns in this codebase
- Template syntax: MEDIUM - {{var}} adapter is trivial but untested assumption

**Research date:** 2026-03-14
**Valid until:** 2026-04-14 (stable domain, no external dependencies)

# Script Job Engine for Gooseclaw Gateway - Research

**Researched:** 2026-03-12
**Domain:** Python subprocess execution, cron-like scheduling, gateway architecture
**Confidence:** HIGH

## Summary

The gooseclaw gateway already has two background engines: a **cron scheduler** (LLM-powered jobs via goose web sessions) and a **reminder engine** (lightweight timers, no AI). The script job engine is a third parallel system that runs arbitrary Python/bash scripts as subprocesses with zero LLM cost, delivering output via `notify_all()`.

The existing codebase establishes extremely clear patterns for this. The reminder engine (lines 1296-1449) is the closest analog: same tick-based loop, same atomic JSON persistence, same `notify_all()` delivery, same daemon thread model. The script engine differs only in *execution*: `subprocess.run()` instead of `notify_all()` directly, plus timeout/capture/error handling.

**Primary recommendation:** Mirror the reminder engine pattern exactly. Use `subprocess.run()` with `capture_output=True, text=True, timeout=N`. Store config in `/data/script_jobs.json`. Reuse the same cron expression parser already in gateway.py. Fire each job in a daemon thread (same as `_fire_cron_job`). Deliver captured stdout via `notify_all()`.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `subprocess` | stdlib | Execute scripts with timeout + output capture | Only sane choice in Python stdlib. Already imported in gateway.py |
| `threading` | stdlib | Background loop + per-job threads | Already used by cron scheduler and reminder engine |
| `json` | stdlib | Persist script_jobs.json | Same pattern as reminders.json |
| `os` / `time` | stdlib | File ops, timestamps | Already used everywhere |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `shlex` | stdlib | Safe command splitting if needed | Only if accepting string commands (prefer list args) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `subprocess.run` | `subprocess.Popen` | Popen gives streaming output but adds complexity. `run()` is sufficient since we capture all output at completion |
| Custom cron parser | `croniter` library | External dep. Gateway already has `_parse_cron_field` and `_cron_matches_now` that work. Reuse them |
| APScheduler | stdlib threading | External dep. Overkill for a 30s tick loop. Existing pattern is battle-tested in this codebase |

**Installation:**
```bash
# No installation needed. 100% Python stdlib.
```

## Architecture Patterns

### How the Existing Cron Scheduler Works (lines 1452-1731)

This is the pattern to mirror. Key observations:

**State management:**
- `_cron_scheduler_running` global bool controls the loop
- `_SCHEDULE_FILE` points to `~/.local/share/goose/schedule.json`
- Jobs loaded fresh each tick (no stale cache problem)

**Tick loop (`_cron_scheduler_loop`):**
1. Sleep/check every 30 seconds
2. Guard: skip if goose web isn't ready (not needed for script jobs since no LLM)
3. Load jobs from JSON
4. For each non-paused, non-running job: check cron match
5. Double-fire prevention: compare `last_run` timestamp HH:MM against current
6. Set `currently_running = True`, save, then fire in a daemon thread
7. Thread body: execute job, then always (via finally) clear `currently_running`, update `last_run`, save

**Atomic JSON writes (`_save_schedule`):**
```python
tmp = filepath + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, filepath)  # atomic on POSIX
```

**Job execution (`_fire_cron_job`):**
- Creates isolated goose web session per run
- Delivers output via `notify_all()`
- Truncates to 4000 chars
- Notifies on failure too

### How the Reminder Engine Works (lines 1296-1449)

Even closer analog since it's simpler:

**State:** `_reminders` list + `_reminders_lock` mutex + `_reminders_file` path
**Tick:** 10 seconds, fires `notify_all()` directly
**Persistence:** Atomic JSON, same as schedule
**Cleanup:** Prunes fired one-shots older than 24h

### Recommended Script Engine Structure

```
# New code goes in gateway.py, inserted between reminder engine and cron scheduler sections

# ── script job engine ──────────────────────────────────────────────────────
#
# Runs Python/bash scripts as subprocesses on cron schedules.
# Zero LLM cost. Output captured and delivered via notify_all().
# Persists to /data/script_jobs.json.
#
# Job dict shape:
#   {
#     "id": str,
#     "name": str,                # human-readable label
#     "description": str,         # what this job does
#     "command": str,             # shell command or script path
#     "cron": str,                # 5-field cron expression
#     "timeout_seconds": int,     # max execution time (default: 300)
#     "enabled": bool,            # false = skip
#     "notify": bool,             # true = send output via notify_all()
#     "last_run": str|null,       # ISO timestamp of last run
#     "last_status": str|null,    # "ok" | "error" | "timeout"
#     "last_output": str|null,    # last captured output (truncated)
#     "currently_running": bool,  # true while subprocess is active
#     "created_at": str,          # ISO timestamp
#     "env": dict|null,           # optional extra env vars
#     "working_dir": str|null,    # optional cwd (default: /data)
#   }
```

### Pattern: Subprocess Execution with Timeout

```python
# Source: Python 3.14 subprocess docs + existing gateway patterns
import subprocess

def _run_script_job(job):
    """Execute a script job as a subprocess. Capture output, enforce timeout."""
    job_id = job.get("id", "unknown")
    job_name = job.get("name", job_id)
    command = job.get("command", "")
    timeout = job.get("timeout_seconds", 300)
    working_dir = job.get("working_dir", "/data")
    extra_env = job.get("env") or {}

    print(f"[script] firing: {job_name} ({job_id})")

    # build environment: inherit current + add extras
    # NEVER pass vault secrets. only explicit env vars from job config.
    env = dict(os.environ)
    env.update(extra_env)

    try:
        result = subprocess.run(
            command,
            shell=True,  # needed for pipes, redirects in user commands
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            env=env,
        )

        output = result.stdout.strip()
        stderr = result.stderr.strip()
        exit_code = result.returncode

        if exit_code != 0:
            status = "error"
            full_output = f"exit code {exit_code}"
            if stderr:
                full_output += f"\nstderr: {stderr}"
            if output:
                full_output += f"\nstdout: {output}"
        else:
            status = "ok"
            full_output = output or "(no output)"

    except subprocess.TimeoutExpired:
        status = "timeout"
        full_output = f"killed after {timeout}s timeout"

    except Exception as e:
        status = "error"
        full_output = f"execution error: {e}"

    # truncate output
    if len(full_output) > 4000:
        full_output = full_output[:3997] + "..."

    # notify if configured
    if job.get("notify", True) and full_output:
        prefix = {
            "ok": "",
            "error": "[ERROR] ",
            "timeout": "[TIMEOUT] ",
        }.get(status, "")
        msg = f"[script:{job_name}] {prefix}{full_output}"
        notify_all(msg)

    print(f"[script] {job_name}: {status} ({len(full_output)} chars)")
    return status, full_output
```

### Pattern: Tick Loop (mirrors cron scheduler)

```python
_SCRIPT_JOBS_FILE = os.path.join(DATA_DIR, "script_jobs.json")
_SCRIPT_TICK_SECONDS = 30  # same as cron
_script_engine_running = False

def _script_engine_loop():
    """Background loop: check script_jobs.json every 30s, fire due jobs."""
    global _script_engine_running
    _script_engine_running = True
    print(f"[script] engine started ({_SCRIPT_TICK_SECONDS}s tick)")

    while _script_engine_running:
        try:
            jobs = _load_script_jobs()
            now = time.localtime()
            save_needed = False

            for job in jobs:
                if not job.get("enabled", True):
                    continue
                if job.get("currently_running"):
                    continue

                cron_expr = job.get("cron", "")
                if not cron_expr:
                    continue

                # reuse existing cron parser
                if not _cron_matches_now(cron_expr, now):
                    continue

                # double-fire prevention (same pattern as cron scheduler)
                last_run = job.get("last_run", "")
                if last_run:
                    try:
                        if "T" in last_run:
                            lr_time = last_run.split("T")[1][:5]
                            now_time = time.strftime("%H:%M", now)
                            if lr_time == now_time:
                                continue
                    except Exception:
                        pass

                # fire in a thread
                job["currently_running"] = True
                save_needed = True

                def _run(j, all_jobs):
                    try:
                        status, output = _run_script_job(j)
                        j["last_status"] = status
                        j["last_output"] = output[:500]  # keep truncated in state
                    finally:
                        j["currently_running"] = False
                        j["last_run"] = time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        )
                        _save_script_jobs(all_jobs)

                threading.Thread(
                    target=_run, args=(job, jobs), daemon=True
                ).start()

            if save_needed:
                _save_script_jobs(jobs)

        except Exception as e:
            print(f"[script] error: {e}")

        # sleep 30s, checking shutdown every 5s
        for _ in range(6):
            if not _script_engine_running:
                break
            time.sleep(5)

    print("[script] engine stopped")
```

### Anti-Patterns to Avoid

- **Using Popen for simple capture-and-deliver**: `subprocess.run()` is sufficient. Popen adds complexity for streaming output that isn't needed here (we deliver all output at once via notify_all).
- **Sharing sessions with goose web**: Script jobs must NEVER touch goose web sessions. That's the whole point. Zero LLM cost.
- **Putting script_jobs.json in goose's share dir**: Use `/data/script_jobs.json` (same as reminders.json). The goose share dir is goose's domain.
- **Running without timeout**: Every subprocess MUST have a timeout. Default 300s (5 min). A stuck script blocks the thread forever otherwise.
- **Using `check=True`**: We want to capture failures, not crash the engine. Handle non-zero exit codes manually.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Cron expression parsing | Custom parser | Existing `_cron_matches_now()` + `_parse_cron_field()` | Already tested, handles 5 and 6-field formats, edge cases covered |
| Atomic JSON writes | Raw file writes | Existing `.tmp` + `os.replace()` pattern | Race conditions on crash. Atomic replace is already proven here |
| Output delivery | Custom telegram/slack calls | `notify_all()` | Channel-agnostic. Works with all registered handlers automatically |
| Double-fire prevention | Custom logic | Existing `last_run` HH:MM comparison pattern | Already handles the 30s tick vs 60s cron minute alignment |
| Background thread lifecycle | Custom daemon management | Same `daemon=True` thread + global bool + shutdown signal pattern | Consistent with rest of gateway |

**Key insight:** This codebase has solved all the hard scheduling/delivery problems already. The script engine is literally "reminder engine execution style + cron scheduler scheduling style + subprocess.run() instead of goose web relay."

## Common Pitfalls

### Pitfall 1: Shell Injection via Command Field
**What goes wrong:** If `command` is passed to `shell=True` with user-controlled input, arbitrary code can run.
**Why it happens:** `shell=True` is needed for pipes/redirects in scripts, but it evaluates the string through `/bin/sh`.
**How to avoid:** This is acceptable because: (a) only the user/admin writes script_jobs.json, (b) the container is already their sandbox, (c) goose can already run arbitrary shell commands. Document that script_jobs.json is admin-only.
**Warning signs:** If you ever add an API endpoint that lets external callers set `command`, that's when it becomes a real vulnerability.

### Pitfall 2: Environment Variable Leakage
**What goes wrong:** `os.environ` includes vault secrets (auto-exported on boot). Scripts inherit them all.
**Why it happens:** The gateway exports vault secrets as env vars (see credentials vault docs).
**How to avoid:** For v1, this is acceptable since scripts run in the same container. For hardening later, could use a whitelist of allowed env vars. Document this tradeoff.
**Warning signs:** If script_jobs.json becomes editable via external API.

### Pitfall 3: Zombie Processes After Timeout
**What goes wrong:** `subprocess.run()` with timeout kills the main process but not its children (e.g., if the script spawns subprocesses).
**Why it happens:** `subprocess.run` sends SIGKILL to the direct child only.
**How to avoid:** Use `start_new_session=True` in subprocess.run, then on TimeoutExpired, kill the process group. Or accept it for v1 since scripts are simple.
**Warning signs:** Orphaned processes accumulating, container memory growing.

### Pitfall 4: Output Too Large for Telegram
**What goes wrong:** Script dumps 100KB of output. Telegram message limit is ~4096 chars.
**Why it happens:** No output truncation.
**How to avoid:** Truncate to 4000 chars (same as `_fire_cron_job` does). Already shown in the code pattern above.
**Warning signs:** Notification delivery failures from telegram API.

### Pitfall 5: Closure Variable Capture in Thread Loop
**What goes wrong:** The `for job in jobs` loop fires threads, but the closure captures the loop variable `job` by reference, not by value. All threads might operate on the last job.
**Why it happens:** Python closure semantics. Classic gotcha.
**How to avoid:** Pass `job` as an argument to the thread function (the existing cron scheduler does this correctly with `args=(job, jobs)`). Mirror that pattern exactly.
**Warning signs:** Same job executing multiple times, other jobs never executing.

### Pitfall 6: No Goose Web Dependency Check Needed
**What goes wrong:** Copying the cron scheduler's `goose_startup_state["state"] == "ready"` guard.
**Why it happens:** The cron scheduler waits for goose web because it needs websocket relay. Script jobs don't.
**How to avoid:** Remove the goose web readiness check from the script engine loop. Scripts can run immediately on gateway startup.
**Warning signs:** Script jobs not firing until goose web initializes (unnecessary delay).

## Code Examples

### script_jobs.json Format

```json
[
  {
    "id": "disk-usage",
    "name": "Disk Usage Report",
    "description": "Check disk space and warn if over 80%",
    "command": "df -h / /data | tail -n +2 | awk '{if ($5+0 > 80) print \"WARNING: \" $6 \" at \" $5; else print $6 \": \" $5}'",
    "cron": "0 */6 * * *",
    "timeout_seconds": 30,
    "enabled": true,
    "notify": true,
    "last_run": null,
    "last_status": null,
    "last_output": null,
    "currently_running": false,
    "created_at": "2026-03-12T10:00:00Z",
    "env": null,
    "working_dir": "/data"
  },
  {
    "id": "backup-check",
    "name": "Backup Verification",
    "description": "Verify daily backup exists and isn't empty",
    "command": "python3 /data/scripts/check_backup.py",
    "cron": "30 9 * * *",
    "timeout_seconds": 60,
    "enabled": true,
    "notify": true,
    "last_run": null,
    "last_status": null,
    "last_output": null,
    "currently_running": false,
    "created_at": "2026-03-12T10:00:00Z",
    "env": {"BACKUP_DIR": "/data/backups"},
    "working_dir": "/data"
  }
]
```

### Load/Save Pattern (mirror reminders)

```python
_SCRIPT_JOBS_FILE = os.path.join(DATA_DIR, "script_jobs.json")

def _load_script_jobs():
    """Read script_jobs.json. Returns list of job dicts."""
    try:
        if os.path.exists(_SCRIPT_JOBS_FILE):
            with open(_SCRIPT_JOBS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[script] warn: could not load script_jobs.json: {e}")
    return []


def _save_script_jobs(jobs):
    """Write script_jobs.json atomically."""
    try:
        os.makedirs(os.path.dirname(_SCRIPT_JOBS_FILE) or ".", exist_ok=True)
        tmp = _SCRIPT_JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, _SCRIPT_JOBS_FILE)
    except Exception as e:
        print(f"[script] warn: could not save script_jobs.json: {e}")
```

### Startup Integration

```python
# Add to the main() startup block, after start_cron_scheduler():
def start_script_engine():
    """Start the script job engine daemon thread."""
    global _script_engine_running
    if _script_engine_running:
        return
    threading.Thread(target=_script_engine_loop, daemon=True).start()

# In main():
start_script_engine()

# In shutdown handler:
_script_engine_running = False
```

### API Endpoints (optional, for management)

```python
# GET  /api/script-jobs         -> list all script jobs with status
# POST /api/script-jobs         -> create a new script job
# PUT  /api/script-jobs/<id>    -> update a script job
# DELETE /api/script-jobs/<id>  -> delete a script job
# POST /api/script-jobs/<id>/run -> trigger immediate run (bypass cron)
```

The immediate-run endpoint is particularly useful for testing. Pattern:

```python
# POST /api/script-jobs/<id>/run
def _handle_script_job_run(job_id):
    jobs = _load_script_jobs()
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        return 404, {"error": "job not found"}
    if job.get("currently_running"):
        return 409, {"error": "job already running"}

    # fire immediately in a thread
    def _run():
        job["currently_running"] = True
        _save_script_jobs(jobs)
        try:
            status, output = _run_script_job(job)
            job["last_status"] = status
            job["last_output"] = output[:500]
        finally:
            job["currently_running"] = False
            job["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_script_jobs(jobs)

    threading.Thread(target=_run, daemon=True).start()
    return 202, {"status": "started", "job_id": job_id}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `subprocess.call()` | `subprocess.run()` | Python 3.5+ | `run()` is the recommended high-level API. `call()` still works but `run()` returns CompletedProcess with stdout/stderr |
| `capture_output` not available | `capture_output=True` | Python 3.7+ | Convenience parameter, equivalent to `stdout=PIPE, stderr=PIPE` |
| Manual text decode | `text=True` | Python 3.7+ | Returns str instead of bytes. Cleaner. |

**Deprecated/outdated:**
- `os.system()`: Never use for anything. No output capture, shell injection risk.
- `subprocess.Popen` for simple cases: `run()` covers 95% of use cases. Only use Popen if you need streaming stdout.

## Security Considerations

### Container Context Matters

This runs inside a Docker container on Railway. The threat model is:
1. **Who writes script_jobs.json?** The user (via goose AI or direct file edit). They own the container.
2. **What can scripts access?** Everything in the container. Same as goose itself.
3. **Is this a privilege escalation?** No. Goose can already run arbitrary shell commands via its developer tool.

### Recommendations for v1

| Concern | Recommendation | Priority |
|---------|---------------|----------|
| Shell injection | Accept for v1. Admin-only config file. Same trust as goose shell access | LOW |
| Env var leakage | Accept for v1. Scripts run in same container as goose. Document it | LOW |
| Resource limits | Timeout is sufficient. Container has its own Railway resource limits | MEDIUM |
| Output size | Truncate to 4000 chars before notify_all. Critical | HIGH |
| Zombie processes | Accept for v1. Add `start_new_session=True` if it becomes a problem | LOW |
| File path traversal | Not applicable. Scripts specify their own paths. User controls them | LOW |

### Future Hardening (not v1)

- Env var whitelist instead of full inheritance
- Per-job resource limits via `resource.setrlimit` in a preexec_fn
- Script path validation (only allow scripts in `/data/scripts/`)
- Rate limiting on API endpoints for script job management

## Config Format Decision

### script_jobs.json Schema

```json
{
  "id": "string (unique, kebab-case, required)",
  "name": "string (human-readable, required)",
  "description": "string (optional, for documentation)",
  "command": "string (shell command, required)",
  "cron": "string (5-field cron expression, required)",
  "timeout_seconds": "integer (default: 300, max: 3600)",
  "enabled": "boolean (default: true)",
  "notify": "boolean (default: true, send output via notify_all)",
  "notify_on_error_only": "boolean (default: false, only notify on non-zero exit)",
  "last_run": "string|null (ISO 8601 timestamp, managed by engine)",
  "last_status": "string|null ('ok'|'error'|'timeout', managed by engine)",
  "last_output": "string|null (truncated to 500 chars, managed by engine)",
  "currently_running": "boolean (managed by engine)",
  "created_at": "string (ISO 8601 timestamp)",
  "env": "object|null (extra env vars, merged with os.environ)",
  "working_dir": "string|null (cwd for subprocess, default: /data)"
}
```

**Design decisions:**
- `id` is user-chosen (like cron scheduler's `schedule_id`), not UUID (like reminders). This makes it readable in logs: `[script:disk-usage]` vs `[script:a1b2c3d4]`.
- `notify_on_error_only` is useful for health checks that run frequently. You only want to hear about failures.
- `last_output` stored truncated (500 chars) in the JSON. Full output goes to notify_all (4000 chars).
- `enabled` instead of `paused` (cron scheduler uses `paused`). Either works, `enabled` reads more naturally.

## Open Questions

1. **CLI tool for managing script jobs?**
   - What we know: The remind CLI (docker/scripts/remind.sh) is a nice UX pattern. A `scriptjob` CLI that calls gateway API endpoints would be consistent.
   - What's unclear: Whether to build this in v1 or just have goose edit the JSON directly.
   - Recommendation: Defer CLI to v2. For v1, goose edits script_jobs.json directly + API endpoints for CRUD.

2. **Should the engine log to a file?**
   - What we know: Currently everything goes to stdout (print statements). Container logs capture these.
   - What's unclear: Whether per-job log files would be useful for debugging.
   - Recommendation: Stdout only for v1. Per-job logs add complexity with log rotation needs.

3. **Maximum concurrent script jobs?**
   - What we know: Each job runs in its own daemon thread. No limit currently.
   - What's unclear: Whether Railway containers can handle many concurrent subprocesses.
   - Recommendation: Add a `_MAX_CONCURRENT_SCRIPTS = 5` constant. Skip firing if at limit. Log a warning.

## Sources

### Primary (HIGH confidence)
- [Python subprocess docs](https://docs.python.org/3/library/subprocess.html) - subprocess.run API, timeout, capture_output, text mode
- Gateway.py source code (lines 1296-1449: reminder engine, lines 1452-1731: cron scheduler) - architecture patterns, threading model, JSON persistence, notify_all integration
- docker/scripts/remind.sh - CLI tool pattern for gateway API interaction

### Secondary (MEDIUM confidence)
- [Python subprocess best practices](https://runebook.dev/en/docs/python/library/subprocess) - deadlock prevention, shell injection warnings
- [Subprocess timeout and zombie process handling](https://alexandra-zaharia.github.io/posts/kill-subprocess-and-its-children-on-timeout-python/) - process group killing for stuck children

### Tertiary (LOW confidence)
- Container-level security considerations (threat model is mitigated by Railway's container isolation)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - 100% stdlib, no dependencies to verify
- Architecture: HIGH - directly mirrors existing proven patterns in the same codebase
- Pitfalls: HIGH - well-known Python subprocess gotchas, verified against official docs
- Config format: MEDIUM - reasonable design but may need iteration based on real usage

**Research date:** 2026-03-12
**Valid until:** 2026-06-12 (stable domain, stdlib doesn't change fast)

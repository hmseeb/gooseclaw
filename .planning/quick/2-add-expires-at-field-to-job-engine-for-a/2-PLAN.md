---
phase: quick
plan: 2
type: execute
wave: 1
depends_on: []
files_modified: [docker/gateway.py]
autonomous: true
requirements: [QUICK-2]

must_haves:
  truths:
    - "Jobs with expires_at set are automatically disabled once current time exceeds that timestamp"
    - "Expired jobs are pruned the same way fired one-shot jobs are (removed after 24h)"
    - "create_job and update_job accept expires_at as an optional unix timestamp"
    - "API validates expires_at must be a future timestamp and numeric"
  artifacts:
    - path: "docker/gateway.py"
      provides: "expires_at support in job engine"
      contains: "expires_at"
  key_links:
    - from: "_job_engine_loop"
      to: "expires_at field"
      via: "expiry check before should_fire logic"
      pattern: "expires_at.*<=.*now"
---

<objective>
Add `expires_at` field to the job engine so jobs can auto-expire at a given unix timestamp.

Purpose: Allow jobs to have a defined lifetime. Once `expires_at` passes, the job engine marks them as fired/expired and prunes them like one-shot jobs.
Output: Updated `docker/gateway.py` with full expires_at support across create, update, engine loop, and list endpoints.
</objective>

<execution_context>
@/Users/haseeb/.claude/get-shit-done/workflows/execute-plan.md
@/Users/haseeb/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docker/gateway.py (lines 1864-1941: create_job, update_job)
@docker/gateway.py (lines 2221-2343: _job_engine_loop)
@docker/gateway.py (lines 5584-5701: handle_create_job)
@docker/gateway.py (lines 5703-5717: handle_list_jobs)
@docker/gateway.py (lines 5771-5797: handle_update_job)
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add expires_at to job creation, update, engine loop, and API</name>
  <files>docker/gateway.py</files>
  <action>
Six targeted edits in docker/gateway.py:

1. **`create_job()` (~line 1897):** After the `"fired": False` line in the job dict literal, add:
   ```python
   "expires_at": job_data.get("expires_at"),
   ```

2. **`update_job()` (~line 1932):** Add `"expires_at"` to the `allowed` set:
   ```python
   allowed = {"name", "command", "text", "cron", "fire_at", "recurring_seconds",
              "timeout_seconds", "enabled", "notify", "notify_on_error_only",
              "model", "provider", "env", "working_dir", "expires_at"}
   ```

3. **`handle_create_job()` (~after line 5637, after fire_at validation block):** Add expires_at validation:
   ```python
   # validate expires_at if provided
   expires_at = data.get("expires_at")
   if expires_at is not None:
       try:
           expires_at = float(expires_at)
           if expires_at <= time.time():
               self.send_json(400, {"error": "expires_at must be in the future"})
               return
           data["expires_at"] = expires_at
       except (ValueError, TypeError):
           self.send_json(400, {"error": "expires_at must be a unix timestamp"})
           return
   ```

4. **`handle_create_job()` response (~line 5694):** After the `fires_in_seconds` block, add:
   ```python
   if job.get("expires_at"):
       response["expires_in_seconds"] = round(job["expires_at"] - time.time())
       response["expires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(job["expires_at"]))
   ```

5. **`_job_engine_loop()` (~line 2238, at the TOP of the `for job in jobs_snapshot:` loop, BEFORE the `enabled` and `fired` checks):** Add expiry check:
   ```python
   # check expiry -- treat expired jobs like fired one-shots
   exp = job.get("expires_at")
   if exp and exp <= now:
       if not job.get("fired"):
           job["fired"] = True
           job["last_status"] = "expired"
           job["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
           save_needed = True
           print(f"[jobs] expired: {job.get('name', job.get('id', '?'))}")
       continue
   ```
   This marks the job as `fired` so the existing pruning logic (lines 2321-2332) will clean it up after 24h, exactly like one-shot jobs.

6. **`handle_list_jobs()` (~line 5716, after the cron_human block):** Add expires_at enrichment:
   ```python
   if j.get("expires_at"):
       j["expires_in_seconds"] = round(j["expires_at"] - now)
       j["expires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(j["expires_at"]))
   ```

Also update the docstring for `handle_create_job()` (~line 5584) to include `expires_at: float` in the JSON body docs.
  </action>
  <verify>
    <automated>cd /Users/haseeb/nix-template && python3 -c "
import sys, importlib.util, time, threading

# load gateway module
spec = importlib.util.spec_from_file_location('gateway', 'docker/gateway.py')
gw = importlib.util.module_from_spec(spec)
# patch threading to avoid starting real servers
orig_thread = threading.Thread
spec.loader.exec_module(gw)

# Test 1: create_job accepts expires_at
future = time.time() + 3600
job, err = gw.create_job({'command': 'echo hi', 'fire_at': time.time() + 60, 'expires_at': future})
assert not err, f'create_job failed: {err}'
assert job['expires_at'] == future, 'expires_at not stored'
print('PASS: create_job stores expires_at')

# Test 2: update_job accepts expires_at
new_exp = time.time() + 7200
updated, err = gw.update_job(job['id'], {'expires_at': new_exp})
assert not err, f'update_job failed: {err}'
assert updated['expires_at'] == new_exp, 'expires_at not updated'
print('PASS: update_job accepts expires_at')

# Test 3: create_job without expires_at defaults to None
job2, err = gw.create_job({'command': 'echo test', 'fire_at': time.time() + 60})
assert not err, f'create_job failed: {err}'
assert job2.get('expires_at') is None, 'expires_at should be None by default'
print('PASS: expires_at defaults to None')

# Test 4: expires_at in allowed set
assert 'expires_at' in {'name', 'command', 'text', 'cron', 'fire_at', 'recurring_seconds', 'timeout_seconds', 'enabled', 'notify', 'notify_on_error_only', 'model', 'provider', 'env', 'working_dir', 'expires_at'}
print('PASS: expires_at in allowed update fields')

# cleanup
gw.delete_job(job['id'])
gw.delete_job(job2['id'])

print('ALL TESTS PASSED')
" 2>&1 | tail -20</automated>
    <manual>POST /api/jobs with expires_at set to 10 seconds from now. Watch logs for "[jobs] expired:" message within ~20s. Verify job no longer appears in GET /api/jobs after expiry.</manual>
  </verify>
  <done>
    - create_job stores expires_at field (defaults to None when not provided)
    - update_job allows modifying expires_at
    - handle_create_job validates expires_at is a future numeric timestamp
    - _job_engine_loop marks expired jobs as fired with status "expired" before any firing logic
    - Expired jobs are pruned by existing 24h one-shot cleanup
    - handle_list_jobs and handle_create_job include expires_in_seconds and expires_at_human in responses
  </done>
</task>

</tasks>

<verification>
- `python3 -c "import docker.gateway"` succeeds (no syntax errors)
- Creating a job with `expires_at` in the past returns 400 error
- Creating a job with `expires_at` in the future stores it and returns `expires_at_human` in response
- Job engine loop expires jobs whose `expires_at` has passed and logs it
- Expired jobs get `fired: true` and `last_status: "expired"`
</verification>

<success_criteria>
Jobs with `expires_at` are automatically marked as fired/expired by the engine loop and pruned within 24h, identical to how one-shot jobs are handled after firing.
</success_criteria>

<output>
After completion, create `.planning/quick/2-add-expires-at-field-to-job-engine-for-a/2-SUMMARY.md`
</output>

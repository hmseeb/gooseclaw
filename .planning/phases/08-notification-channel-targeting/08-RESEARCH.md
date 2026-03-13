# Phase 8 Research: Notification Channel Targeting

## Discovery Level: 0 (Skip)

All work follows established codebase patterns. No new external dependencies. Pure internal wiring of existing functionality.

## Current State Analysis

### CHAN-07: POST /api/notify channel parameter

**Status: 90% done.** The plumbing exists, just not wired.

- `notify_all(text, channel=None)` already supports targeted delivery (line 864)
- When `channel` is set, it finds the matching handler and delivers only to that channel
- Falls back to broadcast-to-all with warning if channel not found
- **Gap:** `handle_notify()` (line 5735) extracts `text` from the request body but ignores `channel`. Calls `notify_all(text)` without passing channel through.
- **Fix:** Extract `data.get("channel")` from request body, optionally validate against `_get_valid_channels()`, pass to `notify_all(text, channel=channel)`.

### CHAN-08: Cron scheduler passes notify_channel

**Status: 80% done.** Job engine already does this, cron scheduler does not.

- Job engine's `_run_script_job()` (line 2384): `notify_all(msg, channel=job.get("notify_channel"))` -- DONE
- Job engine's `_fire_reminder()` (line 2396): `notify_all(msg, channel=job.get("notify_channel"))` -- DONE
- `create_job()` stores `notify_channel` from job_data (line 2082) -- DONE
- **Gap:** `_fire_cron_job()` (line 2727) calls `notify_all()` twice (error on line 2760, output on line 2770) WITHOUT passing `channel=job.get("notify_channel")`.
- **Fix:** Add `channel=job.get("notify_channel")` to both `notify_all()` calls in `_fire_cron_job()`.
- **Note:** Cron jobs come from schedule.json (goose's format). Users would need to add `notify_channel` to their schedule.json entries manually. This is fine -- it's an optional field.

### CHAN-09: remind.sh --notify-channel flag

**Status: 0% done.** remind.sh has no --notify-channel support.

- job.sh already has `--notify-channel` flag (line 240) -- reference implementation
- job.sh passes `notify_channel` in the JSON payload to POST /api/jobs (line 314)
- remind.sh creates jobs via POST /api/jobs too, same endpoint
- **Gap:** remind.sh's `cmd_create()` does not parse `--notify-channel` and does not include it in the payload.
- **Fix:** Add `--notify-channel` to the while loop in `cmd_create()`, store in a local var, include in the JSON payload if set.

## Effort Assessment

| Requirement | Lines Changed | Complexity |
|-------------|---------------|------------|
| CHAN-07 | ~5 lines in gateway.py | Trivial |
| CHAN-08 | ~2 lines in gateway.py | Trivial |
| CHAN-09 | ~15 lines in remind.sh | Simple |

Total: ~22 lines across 2 files. One plan with 2 tasks. ~10 min Claude execution time.

## Validation Strategy

- `notify_all()` already has no unit tests (it's a side-effect function with real handler calls), but we can test `handle_notify` behavior indirectly
- Job engine tests exist for `create_job()` -- add a test for `notify_channel` being stored
- The cron `_fire_cron_job` test (`test_cron_output_strips_goose_banner`) can be extended to verify channel passthrough
- remind.sh is a shell script -- test via flag parsing validation

## Key Files

| File | Changes |
|------|---------|
| `docker/gateway.py` | `handle_notify()`: pass channel param; `_fire_cron_job()`: pass channel param |
| `docker/scripts/remind.sh` | Add `--notify-channel` flag to `cmd_create()` |
| `docker/test_gateway.py` | Tests for CHAN-07, CHAN-08 |

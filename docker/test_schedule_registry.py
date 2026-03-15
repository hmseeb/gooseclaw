"""Tests for LLM-aware schedule registry.

Covers:
  - _next_cron_occurrence: compute next fire time from cron expression
  - get_schedule_context: unified view of all scheduled jobs for LLM consumption
  - get_upcoming_jobs: query jobs firing within N hours
  - handle_schedule_upcoming: HTTP endpoint for /api/schedule/upcoming
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))
import gateway


# ── _next_cron_occurrence ───────────────────────────────────────────────────

class TestNextCronOccurrence(unittest.TestCase):
    """Tests for _next_cron_occurrence()."""

    def test_every_minute_returns_next_minute(self):
        # "* * * * *" should fire within 60 seconds
        import calendar
        after = time.struct_time((2026, 3, 15, 10, 30, 0, 6, 74, 0))
        after_ts = calendar.timegm(after)
        result = gateway._next_cron_occurrence("* * * * *", after_ts)
        assert result is not None
        # next occurrence should be 10:31
        expected = after_ts + 60
        assert result == expected, f"expected {expected}, got {result}"

    def test_specific_time_daily(self):
        # "0 9 * * *" = daily at 09:00
        import calendar
        # at 08:00, next should be 09:00 same day
        after = time.struct_time((2026, 3, 15, 8, 0, 0, 6, 74, 0))
        after_ts = calendar.timegm(after)
        result = gateway._next_cron_occurrence("0 9 * * *", after_ts)
        assert result is not None
        result_time = time.gmtime(result)
        assert result_time.tm_hour == 9
        assert result_time.tm_min == 0
        assert result_time.tm_mday == 15

    def test_specific_time_already_passed_today(self):
        # "0 9 * * *" at 10:00 should return 09:00 next day
        import calendar
        after = time.struct_time((2026, 3, 15, 10, 0, 0, 6, 74, 0))
        after_ts = calendar.timegm(after)
        result = gateway._next_cron_occurrence("0 9 * * *", after_ts)
        assert result is not None
        result_time = time.gmtime(result)
        assert result_time.tm_hour == 9
        assert result_time.tm_min == 0
        assert result_time.tm_mday == 16

    def test_weekday_filter(self):
        # "0 9 * * 1" = Mon at 09:00. 2026-03-15 is Sunday (wday=0 in cron)
        import calendar
        after = time.struct_time((2026, 3, 15, 10, 0, 0, 6, 74, 0))
        after_ts = calendar.timegm(after)
        result = gateway._next_cron_occurrence("0 9 * * 1", after_ts)
        assert result is not None
        result_time = time.gmtime(result)
        # next Monday is March 16
        assert result_time.tm_mday == 16
        assert result_time.tm_hour == 9

    def test_invalid_cron_returns_none(self):
        result = gateway._next_cron_occurrence("invalid cron", time.time())
        assert result is None

    def test_step_expression(self):
        # "*/15 * * * *" at 10:07 should fire at 10:15
        import calendar
        after = time.struct_time((2026, 3, 15, 10, 7, 0, 6, 74, 0))
        after_ts = calendar.timegm(after)
        result = gateway._next_cron_occurrence("*/15 * * * *", after_ts)
        assert result is not None
        result_time = time.gmtime(result)
        assert result_time.tm_hour == 10
        assert result_time.tm_min == 15


# ── get_upcoming_jobs ───────────────────────────────────────────────────────

class TestGetUpcomingJobs(unittest.TestCase):
    """Tests for get_upcoming_jobs()."""

    def setUp(self):
        self._orig_jobs = gateway._jobs[:]
        self._orig_lock = gateway._jobs_lock
        self._schedule_patcher = patch("gateway._load_schedule", return_value=[])
        self._schedule_patcher.start()

    def tearDown(self):
        self._schedule_patcher.stop()
        with gateway._jobs_lock:
            gateway._jobs[:] = self._orig_jobs

    def test_empty_jobs_returns_empty(self):
        with gateway._jobs_lock:
            gateway._jobs[:] = []
        result = gateway.get_upcoming_jobs(hours=24)
        assert result == []

    def test_fire_at_within_window(self):
        now = time.time()
        job = {
            "id": "test-fire",
            "name": "test reminder",
            "type": "reminder",
            "text": "do the thing",
            "command": None,
            "cron": None,
            "fire_at": now + 3600,  # 1 hour from now
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) == 1
        assert result[0]["id"] == "test-fire"
        assert "next_run" in result[0]
        assert "next_run_human" in result[0]

    def test_fire_at_outside_window_excluded(self):
        now = time.time()
        job = {
            "id": "test-far",
            "name": "far away",
            "type": "reminder",
            "text": "too far",
            "command": None,
            "cron": None,
            "fire_at": now + 86400 * 3,  # 3 days out
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) == 0

    def test_cron_job_included(self):
        job = {
            "id": "test-cron",
            "name": "hourly check",
            "type": "script",
            "text": None,
            "command": "echo hi",
            "cron": "0 * * * *",  # every hour
            "fire_at": None,
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) >= 1
        assert result[0]["id"] == "test-cron"
        assert result[0]["next_run"] is not None

    def test_disabled_jobs_excluded(self):
        job = {
            "id": "test-disabled",
            "name": "disabled",
            "type": "reminder",
            "text": "nope",
            "command": None,
            "cron": "0 * * * *",
            "fire_at": None,
            "recurring_seconds": None,
            "enabled": False,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) == 0

    def test_fired_oneshot_excluded(self):
        job = {
            "id": "test-fired",
            "name": "already done",
            "type": "reminder",
            "text": "done",
            "command": None,
            "cron": None,
            "fire_at": time.time() + 100,
            "recurring_seconds": None,
            "enabled": True,
            "fired": True,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) == 0

    def test_results_sorted_by_next_run(self):
        now = time.time()
        jobs = [
            {
                "id": "later",
                "name": "later",
                "type": "reminder",
                "text": "later",
                "command": None,
                "cron": None,
                "fire_at": now + 7200,
                "recurring_seconds": None,
                "enabled": True,
                "fired": False,
                "last_run": None,
                "last_status": None,
                "last_output": None,
                "currently_running": False,
                "created_at": "2026-03-15T10:00:00Z",
            },
            {
                "id": "sooner",
                "name": "sooner",
                "type": "reminder",
                "text": "sooner",
                "command": None,
                "cron": None,
                "fire_at": now + 1800,
                "recurring_seconds": None,
                "enabled": True,
                "fired": False,
                "last_run": None,
                "last_status": None,
                "last_output": None,
                "currently_running": False,
                "created_at": "2026-03-15T10:00:00Z",
            },
        ]
        with gateway._jobs_lock:
            gateway._jobs[:] = jobs
        result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) == 2
        assert result[0]["id"] == "sooner"
        assert result[1]["id"] == "later"


# ── get_schedule_context ────────────────────────────────────────────────────

class TestGetScheduleContext(unittest.TestCase):
    """Tests for get_schedule_context() - LLM-consumable summary."""

    def setUp(self):
        self._orig_jobs = gateway._jobs[:]

    def tearDown(self):
        with gateway._jobs_lock:
            gateway._jobs[:] = self._orig_jobs

    def test_returns_string(self):
        with gateway._jobs_lock:
            gateway._jobs[:] = []
        result = gateway.get_schedule_context()
        assert isinstance(result, str)

    def test_empty_schedule_says_nothing_scheduled(self):
        with gateway._jobs_lock:
            gateway._jobs[:] = []
        with patch("gateway._load_schedule", return_value=[]):
            result = gateway.get_schedule_context()
        assert "no" in result.lower() or "nothing" in result.lower() or "empty" in result.lower()

    def test_includes_job_name(self):
        now = time.time()
        job = {
            "id": "ctx-test",
            "name": "daily standup reminder",
            "type": "reminder",
            "text": "time for standup",
            "command": None,
            "cron": "0 9 * * *",
            "fire_at": None,
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        with patch("gateway._load_schedule", return_value=[]):
            result = gateway.get_schedule_context()
        assert "daily standup reminder" in result

    @patch("gateway._load_schedule")
    def test_includes_goose_schedule_jobs(self, mock_load):
        mock_load.return_value = [
            {
                "id": "goose-cron-1",
                "cron": "30 */6 * * *",
                "source": "/some/recipe.yaml",
                "paused": False,
            }
        ]
        with gateway._jobs_lock:
            gateway._jobs[:] = []
        result = gateway.get_schedule_context()
        assert "goose-cron-1" in result


# ── handle_schedule_upcoming (HTTP endpoint) ────────────────────────────────

class TestHandleScheduleUpcoming(unittest.TestCase):
    """Tests for GET /api/schedule/upcoming endpoint."""

    def setUp(self):
        self._orig_jobs = gateway._jobs[:]

    def tearDown(self):
        with gateway._jobs_lock:
            gateway._jobs[:] = self._orig_jobs

    def _make_handler(self, path="/api/schedule/upcoming"):
        """Create a mock handler for testing."""
        handler = MagicMock()
        handler.path = path
        handler.headers = {}
        handler.send_json = MagicMock()
        handler._check_rate_limit = MagicMock(return_value=True)
        handler._check_local_or_auth = MagicMock(return_value=True)
        return handler

    def test_returns_upcoming_jobs(self):
        now = time.time()
        job = {
            "id": "upcoming-test",
            "name": "test job",
            "type": "reminder",
            "text": "hello",
            "command": None,
            "cron": None,
            "fire_at": now + 3600,
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]

        with patch("gateway._load_schedule", return_value=[]):
            result = gateway.get_upcoming_jobs(hours=24)
        assert len(result) >= 1

    def test_context_endpoint_returns_string(self):
        with gateway._jobs_lock:
            gateway._jobs[:] = []
        with patch("gateway._load_schedule", return_value=[]):
            result = gateway.get_schedule_context()
        assert isinstance(result, str)

    def test_handler_invokes_send_json(self):
        """Call handle_schedule_upcoming() on a mock handler and verify send_json."""
        now = time.time()
        job = {
            "id": "handler-test",
            "name": "handler job",
            "type": "reminder",
            "text": "hi",
            "command": None,
            "cron": None,
            "fire_at": now + 1800,
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]

        handler = self._make_handler("/api/schedule/upcoming")
        with patch("gateway._load_schedule", return_value=[]):
            gateway.GatewayHandler.handle_schedule_upcoming(handler)

        handler.send_json.assert_called_once()
        args = handler.send_json.call_args
        assert args[0][0] == 200
        body = args[0][1]
        assert body["count"] >= 1
        assert body["upcoming"][0]["id"] == "handler-test"

    def test_deduplication_between_jobs_and_schedule(self):
        """A job present in both _jobs and schedule.json should appear only once."""
        now = time.time()
        job = {
            "id": "dup-job",
            "name": "dup job",
            "type": "script",
            "text": None,
            "command": "echo hi",
            "cron": "0 * * * *",
            "fire_at": None,
            "recurring_seconds": None,
            "enabled": True,
            "fired": False,
            "last_run": None,
            "last_status": None,
            "last_output": None,
            "currently_running": False,
            "created_at": "2026-03-15T10:00:00Z",
        }
        schedule_entry = {
            "id": "dup-job",
            "cron": "0 * * * *",
            "source": "/some/recipe.yaml",
            "paused": False,
        }
        with gateway._jobs_lock:
            gateway._jobs[:] = [job]
        with patch("gateway._load_schedule", return_value=[schedule_entry]):
            result = gateway.get_upcoming_jobs(hours=24)
        ids = [r["id"] for r in result]
        assert ids.count("dup-job") == 1


if __name__ == "__main__":
    unittest.main()

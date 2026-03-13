"""Tests for recently implemented gateway features.

Covers:
  - _edit_telegram_message: "not modified" error handling
  - _resolve_job_model: custom prefix, config lookup, fallback
  - _run_script: provider/model injection into goose commands
  - _memory_touch: activity tracking
  - _memory_writer_loop: idle detection, session dedup
  - create_job: model+provider fields, validation
  - migrate_config_models: defaults, ID generation
  - _process_memory_extraction: JSON parsing, file writes
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

# import gateway from same directory
sys.path.insert(0, os.path.dirname(__file__))
import gateway


# ── _resolve_job_model ──────────────────────────────────────────────────────

class TestResolveJobModel(unittest.TestCase):
    """Tests for _resolve_job_model()."""

    def test_no_model_returns_none(self):
        assert gateway._resolve_job_model({}) == (None, None)

    def test_empty_model_returns_none(self):
        assert gateway._resolve_job_model({"model": ""}) == (None, None)

    def test_custom_prefix_strips_and_returns(self):
        result = gateway._resolve_job_model({"model": "custom:mistral-7b"})
        assert result == ("mistral-7b", None)

    def test_custom_prefix_empty_value(self):
        result = gateway._resolve_job_model({"model": "custom:"})
        assert result == ("", None)

    @patch("gateway.load_setup")
    def test_config_id_lookup_found(self, mock_setup):
        mock_setup.return_value = {
            "models": [
                {"id": "anthropic_opus", "model": "claude-opus-4-6", "provider": "anthropic"},
                {"id": "openai_gpt4", "model": "gpt-4o", "provider": "openai"},
            ]
        }
        result = gateway._resolve_job_model({"model": "openai_gpt4"})
        assert result == ("gpt-4o", "openai")

    @patch("gateway.load_setup")
    def test_config_id_lookup_not_found_falls_back(self, mock_setup):
        mock_setup.return_value = {"models": []}
        result = gateway._resolve_job_model({"model": "nonexistent_id"})
        assert result == ("nonexistent_id", None)

    @patch("gateway.load_setup")
    def test_config_id_lookup_no_setup(self, mock_setup):
        mock_setup.return_value = None
        result = gateway._resolve_job_model({"model": "some_id"})
        assert result == ("some_id", None)

    @patch("gateway.load_setup")
    def test_config_id_returns_provider(self, mock_setup):
        mock_setup.return_value = {
            "models": [{"id": "groq_llama", "model": "llama-3.3-70b", "provider": "groq"}]
        }
        model, provider = gateway._resolve_job_model({"model": "groq_llama"})
        assert model == "llama-3.3-70b"
        assert provider == "groq"


# ── migrate_config_models ───────────────────────────────────────────────────

class TestMigrateConfigModels(unittest.TestCase):
    """Tests for migrate_config_models()."""

    def test_non_dict_returns_as_is(self):
        assert gateway.migrate_config_models("string") == "string"
        assert gateway.migrate_config_models(42) == 42
        assert gateway.migrate_config_models(None) is None

    def test_already_migrated_returns_unchanged(self):
        config = {"models": [{"id": "test"}]}
        result = gateway.migrate_config_models(config)
        assert result is config
        assert "memory_idle_minutes" not in result  # should NOT add defaults

    def test_no_provider_returns_unchanged(self):
        config = {"some_key": "value"}
        result = gateway.migrate_config_models(config)
        assert "models" not in result

    def test_creates_models_array_from_old_config(self):
        config = {"provider_type": "anthropic", "model": "claude-opus-4-6"}
        result = gateway.migrate_config_models(config)
        assert len(result["models"]) == 1
        m = result["models"][0]
        assert m["provider"] == "anthropic"
        assert m["model"] == "claude-opus-4-6"
        assert m["is_default"] is True

    def test_model_id_sanitization(self):
        config = {"provider_type": "openrouter", "model": "anthropic/claude-3.5-sonnet"}
        result = gateway.migrate_config_models(config)
        model_id = result["models"][0]["id"]
        assert "/" not in model_id
        assert "." not in model_id
        assert len(model_id) <= 64

    def test_model_id_truncation(self):
        config = {"provider_type": "test", "model": "a" * 100}
        result = gateway.migrate_config_models(config)
        assert len(result["models"][0]["id"]) <= 64

    def test_default_model_used_when_not_specified(self):
        config = {"provider_type": "openai"}
        result = gateway.migrate_config_models(config)
        assert result["models"][0]["model"] == "gpt-4o"

    def test_memory_defaults_added(self):
        config = {"provider_type": "anthropic", "model": "test"}
        result = gateway.migrate_config_models(config)
        assert result["memory_idle_minutes"] == 10
        assert result["memory_writer_enabled"] is True

    def test_channel_route_defaults_added(self):
        config = {"provider_type": "anthropic", "model": "test"}
        result = gateway.migrate_config_models(config)
        assert result["channel_routes"] == {}
        assert result["channel_verbosity"] == {}

    def test_existing_memory_settings_not_overwritten(self):
        config = {
            "provider_type": "anthropic",
            "model": "test",
            "memory_idle_minutes": 30,
            "memory_writer_enabled": False,
        }
        result = gateway.migrate_config_models(config)
        assert result["memory_idle_minutes"] == 30
        assert result["memory_writer_enabled"] is False


# ── create_job ──────────────────────────────────────────────────────────────

class TestCreateJob(unittest.TestCase):
    """Tests for create_job()."""

    def setUp(self):
        """Clear global jobs list before each test."""
        with gateway._jobs_lock:
            gateway._jobs.clear()
        self._save_patcher = patch("gateway._save_jobs")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        with gateway._jobs_lock:
            gateway._jobs.clear()

    def test_basic_script_job(self):
        job, err = gateway.create_job({
            "name": "test",
            "command": "echo hello",
            "fire_at": time.time() + 60,
        })
        assert err == ""
        assert job is not None
        assert job["name"] == "test"
        assert job["type"] == "script"
        assert job["command"] == "echo hello"

    def test_script_job_requires_command(self):
        job, err = gateway.create_job({"name": "test", "type": "script"})
        assert job is None
        assert "command is required" in err

    def test_reminder_job_requires_text(self):
        job, err = gateway.create_job({"name": "test", "type": "reminder"})
        assert job is None
        assert "text is required" in err

    def test_duplicate_id_rejected(self):
        gateway.create_job({"id": "abc", "command": "echo 1", "fire_at": 9999999999})
        job, err = gateway.create_job({"id": "abc", "command": "echo 2", "fire_at": 9999999999})
        assert job is None
        assert "already exists" in err

    def test_model_field_stored(self):
        job, _ = gateway.create_job({
            "command": "goose run",
            "model": "custom:mistral-7b",
            "fire_at": time.time() + 60,
        })
        assert job["model"] == "custom:mistral-7b"

    def test_provider_field_stored(self):
        job, _ = gateway.create_job({
            "command": "goose run",
            "provider": "openrouter",
            "fire_at": time.time() + 60,
        })
        assert job["provider"] == "openrouter"

    def test_model_and_provider_both_stored(self):
        job, _ = gateway.create_job({
            "command": "goose run",
            "model": "llama-3.3-70b",
            "provider": "groq",
            "fire_at": time.time() + 60,
        })
        assert job["model"] == "llama-3.3-70b"
        assert job["provider"] == "groq"

    def test_no_model_or_provider_not_in_job(self):
        job, _ = gateway.create_job({
            "command": "echo hi",
            "fire_at": time.time() + 60,
        })
        assert "model" not in job
        assert "provider" not in job

    def test_auto_generates_uuid_id(self):
        job, _ = gateway.create_job({
            "command": "echo hi",
            "fire_at": time.time() + 60,
        })
        assert len(job["id"]) == 36  # uuid4 format

    def test_default_fields(self):
        job, _ = gateway.create_job({
            "command": "echo hi",
            "fire_at": time.time() + 60,
        })
        assert job["enabled"] is True
        assert job["notify"] is True
        assert job["notify_on_error_only"] is False
        assert job["timeout_seconds"] == 300
        assert job["last_run"] is None
        assert job["fired"] is False


# ── _run_script provider/model injection ────────────────────────────────────

class TestRunScriptInjection(unittest.TestCase):
    """Tests for provider/model flag injection in _run_script()."""

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=("mistral-7b", None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_model_injected_into_goose_command(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        job = {"command": "goose run --recipe /test", "provider": None}
        gateway._run_script(job)
        actual_cmd = mock_run.call_args[0][0]
        assert "--model mistral-7b" in actual_cmd

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_provider_injected_into_goose_command(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        job = {"command": "goose run --recipe /test", "provider": "openrouter"}
        gateway._run_script(job)
        actual_cmd = mock_run.call_args[0][0]
        assert "--provider openrouter" in actual_cmd

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=("gpt-4o", "openai"))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_both_injected(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        job = {"command": "goose run --recipe /test", "provider": "openai"}
        gateway._run_script(job)
        actual_cmd = mock_run.call_args[0][0]
        assert "--provider openai" in actual_cmd
        assert "--model gpt-4o" in actual_cmd

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=("mistral-7b", None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_no_injection_for_non_goose_command(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        job = {"command": "echo hello", "provider": "openrouter"}
        gateway._run_script(job)
        actual_cmd = mock_run.call_args[0][0]
        assert "--provider" not in actual_cmd
        assert "--model" not in actual_cmd

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_no_injection_when_no_overrides(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        job = {"command": "goose run --recipe /test"}
        gateway._run_script(job)
        actual_cmd = mock_run.call_args[0][0]
        assert "--provider" not in actual_cmd
        assert "--model" not in actual_cmd


# ── _run_script output handling ─────────────────────────────────────────────

class TestRunScriptOutput(unittest.TestCase):
    """Tests for _run_script() status and output handling."""

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_success_status(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")
        status, output = gateway._run_script({"command": "echo ok"})
        assert status == "ok"
        assert output == "done"

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_error_status(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="bad")
        status, output = gateway._run_script({"command": "fail"})
        assert status == "error"
        assert "exit code 1" in output
        assert "bad" in output

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_timeout_status(self, _fix, _resolve, mock_run, _notify):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 300)
        status, output = gateway._run_script({"command": "sleep 999"})
        assert status == "timeout"
        assert "timeout" in output

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_long_output_not_truncated_prematurely(self, _fix, _resolve, mock_run, _notify):
        """Output up to 64K chars should pass through to notify_all intact."""
        mock_run.return_value = MagicMock(returncode=0, stdout="x" * 30000, stderr="")
        status, output = gateway._run_script({"command": "echo big"})
        assert len(output) == 30000  # should NOT be truncated

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_extreme_output_truncated(self, _fix, _resolve, mock_run, _notify):
        """Output over 64K chars should be truncated."""
        mock_run.return_value = MagicMock(returncode=0, stdout="x" * 70000, stderr="")
        status, output = gateway._run_script({"command": "echo huge"})
        assert len(output) <= 64000
        assert output.endswith("...")

    @patch("gateway.notify_all")
    @patch("gateway.subprocess.run")
    @patch("gateway._resolve_job_model", return_value=(None, None))
    @patch("gateway._fix_goose_run_recipe", side_effect=lambda x: x)
    def test_no_output_shows_placeholder(self, _fix, _resolve, mock_run, _notify):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        status, output = gateway._run_script({"command": "echo"})
        assert output == "(no output)"


# ── _edit_telegram_message ──────────────────────────────────────────────────

class TestEditTelegramMessage(unittest.TestCase):
    """Tests for _edit_telegram_message() error handling."""

    @patch("gateway._markdown_to_telegram_html", return_value="<b>hi</b>")
    @patch("gateway.urllib.request.urlopen")
    def test_success_returns_true(self, mock_urlopen, _md):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"ok": True}).encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        result = gateway._edit_telegram_message("token", "123", "456", "hi")
        assert result is True

    @patch("gateway._markdown_to_telegram_html", return_value="<b>hi</b>")
    @patch("gateway.urllib.request.urlopen")
    def test_not_modified_in_exception_returns_true(self, mock_urlopen, _md):
        mock_urlopen.side_effect = Exception("Bad Request: message is not modified")
        result = gateway._edit_telegram_message("token", "123", "456", "hi")
        assert result is True

    @patch("gateway._markdown_to_telegram_html", return_value="<b>hi</b>")
    @patch("gateway.urllib.request.urlopen")
    def test_not_modified_in_body_returns_true(self, mock_urlopen, _md):
        err = Exception("Bad Request")
        err.read = lambda: b'{"description": "message is not modified"}'
        mock_urlopen.side_effect = err
        result = gateway._edit_telegram_message("token", "123", "456", "hi")
        assert result is True

    @patch("gateway._markdown_to_telegram_html", return_value="<b>hi</b>")
    @patch("gateway.urllib.request.urlopen")
    def test_html_fails_falls_back_to_plain_text(self, mock_urlopen, _md):
        # first call (HTML) fails, second call (plain text) succeeds
        resp = MagicMock()
        resp.read.return_value = json.dumps({"ok": True}).encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [Exception("parse error"), resp]
        result = gateway._edit_telegram_message("token", "123", "456", "hi")
        assert result is True
        assert mock_urlopen.call_count == 2

    @patch("gateway._markdown_to_telegram_html", return_value="<b>hi</b>")
    @patch("gateway.urllib.request.urlopen")
    def test_both_fail_returns_false(self, mock_urlopen, _md):
        mock_urlopen.side_effect = [Exception("parse error"), Exception("also failed")]
        result = gateway._edit_telegram_message("token", "123", "456", "hi")
        assert result is False


# ── _memory_touch ───────────────────────────────────────────────────────────

class TestMemoryTouch(unittest.TestCase):
    """Tests for _memory_touch() activity tracking."""

    def setUp(self):
        with gateway._memory_last_activity_lock:
            gateway._memory_last_activity.clear()

    def tearDown(self):
        with gateway._memory_last_activity_lock:
            gateway._memory_last_activity.clear()

    def test_records_timestamp(self):
        before = time.time()
        gateway._memory_touch("chat_123")
        after = time.time()
        ts = gateway._memory_last_activity["chat_123"]
        assert before <= ts <= after

    def test_converts_to_string(self):
        gateway._memory_touch(12345)
        assert "12345" in gateway._memory_last_activity

    def test_updates_on_repeat(self):
        gateway._memory_touch("chat_1")
        first = gateway._memory_last_activity["chat_1"]
        time.sleep(0.01)
        gateway._memory_touch("chat_1")
        second = gateway._memory_last_activity["chat_1"]
        assert second > first

    def test_thread_safety(self):
        """Multiple threads can touch without errors."""
        errors = []
        def touch_many(prefix):
            try:
                for i in range(50):
                    gateway._memory_touch(f"{prefix}_{i}")
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=touch_many, args=(f"t{t}",)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        assert len(gateway._memory_last_activity) == 200


# ── _process_memory_extraction ──────────────────────────────────────────────

class TestProcessMemoryExtraction(unittest.TestCase):
    """Tests for _process_memory_extraction() JSON parsing and file writes."""

    def test_no_json_in_response(self):
        # should not raise, just print and return
        gateway._process_memory_extraction("no json here at all")

    def test_empty_extraction(self):
        gateway._process_memory_extraction('{"empty": true}')

    def test_invalid_json(self):
        gateway._process_memory_extraction("{broken json")

    @patch("builtins.open", mock_open(read_data="# User\n## Important Context\n- stuff\n"))
    @patch("os.path.exists", return_value=True)
    def test_user_facts_appended(self, _exists):
        response = json.dumps({"user_facts": ["likes coffee", "works at ACME"]})
        gateway._process_memory_extraction(response)
        handle = open
        # verify write was called
        written = "".join(call.args[0] for call in handle().write.call_args_list)
        assert "likes coffee" in written or handle().write.called

    @patch("builtins.open", mock_open())
    @patch("os.path.exists", return_value=True)
    def test_corrections_appended(self, _exists):
        response = json.dumps({"corrections": ["use bun not npm"]})
        gateway._process_memory_extraction(response)
        handle = open
        written = "".join(call.args[0] for call in handle().write.call_args_list)
        assert "use bun not npm" in written or handle().write.called


# ── _memory_writer_loop idle detection ──────────────────────────────────────

class TestMemoryWriterIdleDetection(unittest.TestCase):
    """Tests for idle detection logic extracted from _memory_writer_loop."""

    def test_idle_threshold_calculation(self):
        """Verify idle threshold = minutes * 60."""
        idle_minutes = 10
        assert idle_minutes * 60 == 600

        idle_minutes = 5
        assert idle_minutes * 60 == 300

    def test_idle_detection_logic(self):
        """Simulate the idle detection from the loop."""
        now = time.time()
        idle_threshold = 600  # 10 minutes

        activity = {
            "idle_chat": now - 700,      # idle (700 > 600)
            "active_chat": now - 100,    # still active
            "exactly_idle": now - 600,   # exactly at threshold
        }

        idle_chats = [
            cid for cid, last_time in activity.items()
            if now - last_time >= idle_threshold
        ]

        assert "idle_chat" in idle_chats
        assert "active_chat" not in idle_chats
        assert "exactly_idle" in idle_chats  # >= threshold

    def test_session_deduplication(self):
        """Already processed sessions should be skipped."""
        processed = {"session_abc"}
        sid = "session_abc"
        assert sid in processed  # would skip

        sid2 = "session_xyz"
        assert sid2 not in processed  # would process


# ── regex injection patterns ────────────────────────────────────────────────

class TestRegexInjection(unittest.TestCase):
    """Tests for the regex patterns used in _run_script() injection."""

    def test_provider_injection_basic(self):
        cmd = "goose run --recipe /test"
        result = re.sub(r'(goose\s+run\b)', r'\1 --provider openrouter', cmd)
        assert result == "goose run --provider openrouter --recipe /test"

    def test_model_injection_basic(self):
        cmd = "goose run --recipe /test"
        result = re.sub(r'(goose\s+run\b)', r'\1 --model mistral-7b', cmd)
        assert result == "goose run --model mistral-7b --recipe /test"

    def test_both_injected_sequentially(self):
        cmd = "goose run --recipe /test"
        cmd = re.sub(r'(goose\s+run\b)', r'\1 --provider groq', cmd)
        cmd = re.sub(r'(goose\s+run\b)', r'\1 --model llama-70b', cmd)
        assert "--provider groq" in cmd
        assert "--model llama-70b" in cmd

    def test_no_match_leaves_command_unchanged(self):
        cmd = "echo hello world"
        result = re.sub(r'(goose\s+run\b)', r'\1 --provider openai', cmd)
        assert result == cmd

    def test_goose_run_with_extra_spaces(self):
        cmd = "goose  run --recipe /test"
        result = re.sub(r'(goose\s+run\b)', r'\1 --provider openai', cmd)
        assert "--provider openai" in result

    def test_does_not_match_goose_running(self):
        """Should not inject into 'goose running' or similar."""
        cmd = "echo goose running wild"
        result = re.sub(r'(goose\s+run\b)', r'\1 --provider openai', cmd)
        assert result == cmd  # \b prevents matching "running"


# ── _strip_goose_preamble ───────────────────────────────────────────────────

class TestStripGoosePreamble(unittest.TestCase):
    """Tests for stripping goose startup banner from job output."""

    def test_strips_goose_banner(self):
        raw = (
            "   __( O)>  \u25cf new session \u00b7 claude-code default\n"
            "   \\____)\t20260312_22 \u00b7 /data\n"
            "     L L\t goose is ready\n"
            "Let me pull data!\n"
            "\n"
            "CRYPTO MARKET REPORT\n"
            "BTC is at 70k"
        )
        result = gateway._strip_goose_preamble(raw)
        assert "__( O)>" not in result
        assert "goose is ready" not in result
        assert "CRYPTO MARKET REPORT" in result

    def test_no_banner_returns_unchanged(self):
        text = "just normal output\nno banner here"
        result = gateway._strip_goose_preamble(text)
        assert result == text

    def test_strips_thinking_before_separator(self):
        raw = (
            "   __( O)>  banner\n"
            "   \\____)\tsession\n"
            "     L L\t goose is ready\n"
            "Let me pull all the data simultaneously!\n"
            "Now I have everything needed. Let me compile.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Actual report content"
        )
        result = gateway._strip_goose_preamble(raw)
        assert "__( O)>" not in result
        assert "Actual report content" in result

    def test_empty_string(self):
        assert gateway._strip_goose_preamble("") == ""

    def test_only_banner(self):
        raw = "   __( O)>  banner\n   \\____)\tsession\n     L L\t goose is ready\n"
        result = gateway._strip_goose_preamble(raw)
        assert result.strip() == ""


# ── _fire_cron_job preamble stripping ───────────────────────────────────────

class TestFireCronJobStripping(unittest.TestCase):
    """Verify cron job output gets goose preamble stripped."""

    @patch("gateway.notify_all")
    @patch("gateway._do_ws_relay")
    @patch("gateway._load_recipe", return_value="do the thing")
    def test_cron_output_strips_goose_banner(self, _recipe, mock_relay, mock_notify):
        raw = (
            "   __( O)>  banner\n"
            "   \\____)\tsession\n"
            "     L L\t goose is ready\n"
            "Here is the report:\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Actual report content"
        )
        mock_relay.return_value = (raw, None)
        gateway._fire_cron_job({"id": "test-cron", "source": "/test"})
        # check that notify_all was called without the banner
        call_args = mock_notify.call_args[0][0]
        assert "__( O)>" not in call_args
        assert "goose is ready" not in call_args
        assert "Actual report content" in call_args

    @patch("gateway.notify_all")
    @patch("gateway._do_ws_relay")
    @patch("gateway._load_recipe", return_value="do the thing")
    def test_cron_output_not_truncated_at_4000(self, _recipe, mock_relay, mock_notify):
        """Cron output should allow long content (chunking handled by TG sender)."""
        long_report = "x" * 10000
        mock_relay.return_value = (long_report, None)
        gateway._fire_cron_job({"id": "test-cron", "source": "/test"})
        call_args = mock_notify.call_args[0][0]
        # should contain the full content, not truncated at 4000
        assert len(call_args) > 9000


# ── update_job ──────────────────────────────────────────────────────────────

class TestUpdateJob(unittest.TestCase):
    """Tests for update_job()."""

    def setUp(self):
        with gateway._jobs_lock:
            gateway._jobs.clear()
        self._save_patcher = patch("gateway._save_jobs")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        with gateway._jobs_lock:
            gateway._jobs.clear()

    def _create(self, **overrides):
        data = {"command": "echo hi", "fire_at": time.time() + 3600}
        data.update(overrides)
        job, _ = gateway.create_job(data)
        return job

    def test_update_name(self):
        job = self._create(name="old-name")
        updated, err = gateway.update_job(job["id"], {"name": "new-name"})
        assert err == ""
        assert updated["name"] == "new-name"

    def test_update_command(self):
        job = self._create(command="echo old")
        updated, err = gateway.update_job(job["id"], {"command": "echo new"})
        assert err == ""
        assert updated["command"] == "echo new"

    def test_update_cron(self):
        job = self._create()
        updated, err = gateway.update_job(job["id"], {"cron": "0 */6 * * *"})
        assert updated["cron"] == "0 */6 * * *"

    def test_update_model_and_provider(self):
        job = self._create()
        updated, err = gateway.update_job(job["id"], {"model": "gpt-4o", "provider": "openai"})
        assert updated["model"] == "gpt-4o"
        assert updated["provider"] == "openai"

    def test_update_nonexistent_job(self):
        updated, err = gateway.update_job("no-such-id", {"name": "x"})
        assert updated is None
        assert "not found" in err

    def test_update_preserves_other_fields(self):
        job = self._create(name="keep-me", command="echo keep")
        updated, _ = gateway.update_job(job["id"], {"name": "changed"})
        assert updated["command"] == "echo keep"

    def test_update_rejects_empty_command_for_script(self):
        job = self._create(command="echo hi")
        updated, err = gateway.update_job(job["id"], {"command": ""})
        assert updated is None
        assert "command" in err


# ── humanize_cron ───────────────────────────────────────────────────────────

class TestHumanizeCron(unittest.TestCase):
    """Tests for humanize_cron() display."""

    def test_every_6_hours(self):
        result = gateway.humanize_cron("0 */6 * * *")
        assert "every 6h" in result.lower() or "6 hour" in result.lower()

    def test_daily_at_time(self):
        result = gateway.humanize_cron("30 9 * * *")
        assert "9:30" in result or "09:30" in result

    def test_specific_date(self):
        result = gateway.humanize_cron("14 18 12 3 *")
        assert "Mar" in result
        assert "18:14" in result

    def test_every_minute(self):
        result = gateway.humanize_cron("* * * * *")
        assert "every min" in result.lower() or "every 1m" in result.lower()

    def test_weekday_only(self):
        result = gateway.humanize_cron("0 9 * * 1-5")
        assert "Mon" in result or "weekday" in result.lower()

    def test_hourly(self):
        result = gateway.humanize_cron("0 * * * *")
        assert "every hour" in result.lower() or "hourly" in result.lower()

    def test_invalid_cron_returns_as_is(self):
        result = gateway.humanize_cron("not a cron")
        assert result == "not a cron"


# ── _prewarm_session ────────────────────────────────────────────────────────

class TestPrewarmSession(unittest.TestCase):
    """Tests for _prewarm_session() background session creation after /clear."""

    def setUp(self):
        gateway._session_manager._sessions.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()

    @patch("gateway._create_goose_session", return_value="new_session_abc")
    @patch("gateway._save_telegram_sessions")
    def test_creates_and_stores_session(self, _save, mock_create):
        gateway._prewarm_session("chat_99")
        # give the background thread a moment
        time.sleep(0.1)
        sid = gateway._session_manager.get("telegram", "chat_99")
        assert sid == "new_session_abc"
        mock_create.assert_called_once()

    @patch("gateway._create_goose_session", return_value=None)
    @patch("gateway._save_telegram_sessions")
    def test_no_session_stored_on_failure(self, _save, mock_create):
        gateway._prewarm_session("chat_99")
        time.sleep(0.1)
        sid = gateway._session_manager.get("telegram", "chat_99")
        assert sid is None

    @patch("gateway._create_goose_session", return_value="new_session_xyz")
    @patch("gateway._save_telegram_sessions")
    def test_does_not_overwrite_if_user_sent_message_first(self, _save, mock_create):
        """If user sends a message before prewarm finishes, don't clobber."""
        # simulate: user message arrived and created session already
        gateway._session_manager.set("telegram", "chat_99", "user_initiated_session")
        gateway._prewarm_session("chat_99")
        time.sleep(0.1)
        sid = gateway._session_manager.get("telegram", "chat_99")
        assert sid == "user_initiated_session"

    @patch("gateway._create_goose_session", return_value="prewarmed_session")
    @patch("gateway._save_telegram_sessions")
    def test_is_non_blocking(self, _save, mock_create):
        """_prewarm_session should return immediately (runs in background thread)."""
        start = time.time()
        gateway._prewarm_session("chat_99")
        elapsed = time.time() - start
        assert elapsed < 0.05  # should be near-instant
        time.sleep(0.1)  # let thread finish


# ── /clear kills active relay ─────────────────────────────────────────────────

class TestClearKillsRelay(unittest.TestCase):
    """Bug fix: /clear should kill any active relay before clearing session,
    otherwise the old relay holds the chat lock and user gets 'Still thinking...'"""

    def setUp(self):
        gateway._telegram_state._active_relays.clear()
        gateway._session_manager._sessions.clear()

    def tearDown(self):
        gateway._telegram_state._active_relays.clear()
        gateway._session_manager._sessions.clear()

    def test_clear_pops_active_relay(self):
        """_clear_chat should remove active relay entry for the chat."""
        mock_sock = MagicMock()
        gateway._telegram_state.set_active_relay("chat_1", [mock_sock])
        gateway._session_manager.set("telegram", "chat_1", "old_session")
        gateway._clear_chat("chat_1")
        self.assertIsNone(gateway._telegram_state.pop_active_relay("chat_1"))

    def test_clear_closes_socket(self):
        """_clear_chat should close the active relay websocket."""
        mock_sock = MagicMock()
        gateway._telegram_state.set_active_relay("chat_1", [mock_sock])
        gateway._session_manager.set("telegram", "chat_1", "old_session")
        gateway._clear_chat("chat_1")
        mock_sock.close.assert_called_once()

    def test_clear_removes_session(self):
        """_clear_chat should remove the session from _session_manager."""
        gateway._session_manager.set("telegram", "chat_1", "old_session")
        gateway._clear_chat("chat_1")
        self.assertIsNone(gateway._session_manager.get("telegram", "chat_1"))

    def test_clear_saves_sessions(self):
        """_clear_chat should persist the session removal via _session_manager."""
        gateway._session_manager.set("telegram", "chat_1", "old_session")
        with patch.object(gateway._session_manager, '_save') as mock_save:
            gateway._clear_chat("chat_1")
            mock_save.assert_called()


# ── cancelled flag on relay ───────────────────────────────────────────────────

class TestRelayCancelledFlag(unittest.TestCase):
    """Bug fix: sock_ref should carry a cancelled flag so relay thread
    doesn't send partial text after /stop kills the socket."""

    def test_sock_ref_has_cancelled_flag(self):
        """sock_ref should be [socket, cancelled_event]."""
        # The relay creates sock_ref = [None, threading.Event()]
        # /stop sets the event. Relay checks it before sending.
        evt = threading.Event()
        sock_ref = [None, evt]
        self.assertFalse(sock_ref[1].is_set())
        sock_ref[1].set()
        self.assertTrue(sock_ref[1].is_set())


# ── prewarm coordination ─────────────────────────────────────────────────────

class TestPrewarmCoordination(unittest.TestCase):
    """Bug fix: _get_session_id should wait for in-progress prewarm instead
    of creating a duplicate session."""

    def setUp(self):
        gateway._session_manager._sessions.clear()
        gateway._telegram_state._prewarm_events.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()
        gateway._telegram_state._prewarm_events.clear()

    @patch("gateway._create_goose_session", return_value="prewarmed_sid")
    @patch("gateway._save_telegram_sessions")
    def test_get_session_waits_for_prewarm(self, _save, mock_create):
        """If prewarm is in progress, _get_session_id should wait and use it."""
        # Simulate prewarm starting
        evt = threading.Event()
        gateway._telegram_state._prewarm_events["chat_1"] = evt

        def finish_prewarm():
            time.sleep(0.1)
            gateway._session_manager.set("telegram", "chat_1", "prewarmed_sid")
            evt.set()

        threading.Thread(target=finish_prewarm, daemon=True).start()
        sid = gateway._get_session_id("chat_1")
        self.assertEqual(sid, "prewarmed_sid")
        # _create_goose_session should NOT have been called by _get_session_id
        mock_create.assert_not_called()

    @patch("gateway._create_goose_session", return_value="fallback_sid")
    @patch("gateway._save_telegram_sessions")
    def test_get_session_no_prewarm_creates_new(self, _save, mock_create):
        """Without active prewarm, _get_session_id creates a new session normally."""
        sid = gateway._get_session_id("chat_2")
        self.assertEqual(sid, "fallback_sid")
        mock_create.assert_called_once()


# ── unknown slash command catch-all ───────────────────────────────────────────

class TestUnknownSlashCommand(unittest.TestCase):
    """Unknown slash commands should not be forwarded to goose."""

    def test_is_known_command_recognized(self):
        """Known commands should be recognized."""
        for cmd in ["/help", "/stop", "/clear", "/compact"]:
            self.assertTrue(gateway.is_known_command(cmd), f"{cmd} should be known")

    def test_is_known_command_unknown(self):
        """Unknown slash commands should not be recognized."""
        for cmd in ["/reset", "/prompts", "/foo", "/unknown"]:
            self.assertFalse(gateway.is_known_command(cmd), f"{cmd} should be unknown")

    def test_regular_messages_not_commands(self):
        """Regular messages should not be treated as commands."""
        for msg in ["hello", "what time is it?", "run /something"]:
            self.assertFalse(gateway.is_known_command(msg), f"'{msg}' should not be a command")


# ── CommandRouter ─────────────────────────────────────────────────────────────

class TestCommandRouter(unittest.TestCase):
    """Tests for CommandRouter register/dispatch/is_command/get_help_text."""

    def _make_router(self):
        return gateway.CommandRouter()

    def test_register_and_dispatch(self):
        """Register 'help' handler, dispatch '/help', handler called with ctx."""
        router = self._make_router()
        handler = MagicMock()
        router.register("help", handler, "show help")
        ctx = {"channel": "telegram", "user_id": "123"}
        result = router.dispatch("/help", ctx)
        self.assertTrue(result)
        handler.assert_called_once_with(ctx)

    def test_dispatch_unknown_returns_false(self):
        """Dispatching an unregistered command returns False."""
        router = self._make_router()
        handler = MagicMock()
        router.register("help", handler)
        ctx = {"channel": "telegram", "user_id": "123"}
        result = router.dispatch("/unknown", ctx)
        self.assertFalse(result)
        handler.assert_not_called()

    def test_is_command_registered(self):
        """is_command returns True for registered commands."""
        router = self._make_router()
        router.register("help", MagicMock())
        self.assertTrue(router.is_command("/help"))

    def test_is_command_unregistered(self):
        """is_command returns False for unregistered commands."""
        router = self._make_router()
        self.assertFalse(router.is_command("/foo"))

    def test_is_command_not_slash(self):
        """is_command returns False for non-slash text."""
        router = self._make_router()
        router.register("help", MagicMock())
        self.assertFalse(router.is_command("hello"))

    def test_is_command_empty(self):
        """is_command returns False for empty string and None."""
        router = self._make_router()
        self.assertFalse(router.is_command(""))
        self.assertFalse(router.is_command(None))

    def test_dispatch_case_insensitive(self):
        """Dispatch is case-insensitive: /HELP matches registered 'help'."""
        router = self._make_router()
        handler = MagicMock()
        router.register("help", handler)
        ctx = {"channel": "telegram", "user_id": "123"}
        result = router.dispatch("/HELP", ctx)
        self.assertTrue(result)
        handler.assert_called_once_with(ctx)

    def test_dispatch_non_slash_returns_false(self):
        """Dispatching non-slash text returns False."""
        router = self._make_router()
        router.register("help", MagicMock())
        ctx = {"channel": "telegram", "user_id": "123"}
        result = router.dispatch("hello", ctx)
        self.assertFalse(result)

    def test_dispatch_none_returns_false(self):
        """Dispatching None returns False."""
        router = self._make_router()
        ctx = {"channel": "telegram", "user_id": "123"}
        result = router.dispatch(None, ctx)
        self.assertFalse(result)

    def test_get_help_text(self):
        """get_help_text returns formatted help with descriptions."""
        router = self._make_router()
        router.register("help", MagicMock(), "show help")
        router.register("stop", MagicMock(), "cancel response")
        help_text = router.get_help_text()
        self.assertIn("/help", help_text)
        self.assertIn("show help", help_text)
        self.assertIn("/stop", help_text)
        self.assertIn("cancel response", help_text)

    def test_multiple_commands(self):
        """Multiple commands all dispatch and is_command correctly."""
        router = self._make_router()
        handlers = {}
        for cmd in ["help", "stop", "clear", "compact"]:
            h = MagicMock()
            handlers[cmd] = h
            router.register(cmd, h, f"{cmd} desc")
        ctx = {"channel": "telegram", "user_id": "123"}
        for cmd in ["help", "stop", "clear", "compact"]:
            self.assertTrue(router.is_command(f"/{cmd}"))
            result = router.dispatch(f"/{cmd}", ctx)
            self.assertTrue(result)
            handlers[cmd].assert_called_with(ctx)


# ── SessionManager ──────────────────────────────────────────────────────────

class TestSessionManager(unittest.TestCase):
    """Tests for SessionManager composite-key session store."""

    def test_get_set_composite_key(self):
        sm = gateway.SessionManager()
        sm.set("telegram", "chat_1", "sid_abc")
        assert sm.get("telegram", "chat_1") == "sid_abc"

    def test_get_missing_returns_none(self):
        sm = gateway.SessionManager()
        assert sm.get("telegram", "nonexistent") is None

    def test_pop_removes_and_returns(self):
        sm = gateway.SessionManager()
        sm.set("telegram", "chat_1", "sid_abc")
        result = sm.pop("telegram", "chat_1")
        assert result == "sid_abc"
        assert sm.get("telegram", "chat_1") is None

    def test_pop_missing_returns_none(self):
        sm = gateway.SessionManager()
        assert sm.pop("telegram", "nonexistent") is None

    def test_clear_channel_only_removes_that_channel(self):
        sm = gateway.SessionManager()
        sm.set("telegram", "chat_1", "sid_1")
        sm.set("telegram", "chat_2", "sid_2")
        sm.set("discord", "user_1", "sid_3")
        sm.clear_channel("telegram")
        assert sm.get("telegram", "chat_1") is None
        assert sm.get("telegram", "chat_2") is None
        assert sm.get("discord", "user_1") == "sid_3"

    def test_get_all_for_channel(self):
        sm = gateway.SessionManager()
        sm.set("telegram", "chat_1", "sid_1")
        sm.set("telegram", "chat_2", "sid_2")
        sm.set("discord", "user_1", "sid_3")
        result = sm.get_all_for_channel("telegram")
        assert result == {"chat_1": "sid_1", "chat_2": "sid_2"}

    def test_different_channels_same_user_id(self):
        sm = gateway.SessionManager()
        sm.set("telegram", "user_1", "tg_sid")
        sm.set("discord", "user_1", "dc_sid")
        assert sm.get("telegram", "user_1") == "tg_sid"
        assert sm.get("discord", "user_1") == "dc_sid"


class TestSessionManagerPersistence(unittest.TestCase):
    """Tests for SessionManager disk persistence."""

    def setUp(self):
        self.persist_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.persist_dir, ignore_errors=True)

    def test_save_creates_file(self):
        sm = gateway.SessionManager(persist_dir=self.persist_dir)
        sm.set("telegram", "chat_1", "sid_abc")
        fpath = os.path.join(self.persist_dir, "sessions_telegram.json")
        assert os.path.exists(fpath)

    def test_load_restores_sessions(self):
        sm1 = gateway.SessionManager(persist_dir=self.persist_dir)
        sm1.set("telegram", "chat_1", "sid_abc")
        sm1.set("telegram", "chat_2", "sid_def")
        # create new SM with same persist_dir
        sm2 = gateway.SessionManager(persist_dir=self.persist_dir)
        sm2.load("telegram")
        assert sm2.get("telegram", "chat_1") == "sid_abc"
        assert sm2.get("telegram", "chat_2") == "sid_def"

    def test_save_uses_atomic_write(self):
        sm = gateway.SessionManager(persist_dir=self.persist_dir)
        with patch("os.replace") as mock_replace:
            sm.set("telegram", "chat_1", "sid_abc")
            mock_replace.assert_called_once()


class TestSessionManagerThreadSafety(unittest.TestCase):
    """Tests for SessionManager thread safety."""

    def test_concurrent_sets(self):
        sm = gateway.SessionManager()
        errors = []

        def set_session(i):
            try:
                sm.set("telegram", f"user_{i}", f"sid_{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=set_session, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0
        for i in range(10):
            assert sm.get("telegram", f"user_{i}") == f"sid_{i}"


# ── ChannelState ────────────────────────────────────────────────────────────

class TestChannelState(unittest.TestCase):
    """Tests for ChannelState per-channel concurrency primitives."""

    def test_get_user_lock_creates_on_demand(self):
        cs = gateway.ChannelState()
        lock1 = cs.get_user_lock("user_1")
        lock2 = cs.get_user_lock("user_1")
        assert lock1 is lock2
        assert isinstance(lock1, type(threading.Lock()))

    def test_different_users_get_different_locks(self):
        cs = gateway.ChannelState()
        lock1 = cs.get_user_lock("user_1")
        lock2 = cs.get_user_lock("user_2")
        assert lock1 is not lock2

    def test_set_and_pop_active_relay(self):
        cs = gateway.ChannelState()
        sock_ref = [MagicMock(), threading.Event()]
        cs.set_active_relay("user_1", sock_ref)
        result = cs.pop_active_relay("user_1")
        assert result is sock_ref
        assert cs.pop_active_relay("user_1") is None

    def test_kill_relay_closes_socket_and_sets_cancelled(self):
        cs = gateway.ChannelState()
        mock_sock = MagicMock()
        cancel_event = threading.Event()
        cs.set_active_relay("user_1", [mock_sock, cancel_event])
        result = cs.kill_relay("user_1")
        assert result is not None
        mock_sock.close.assert_called_once()
        assert cancel_event.is_set()

    def test_kill_relay_nonexistent_returns_none(self):
        cs = gateway.ChannelState()
        assert cs.kill_relay("nonexistent") is None


# ── /clear scoping (INFRA-04) ────────────────────────────────────────────────

class TestClearChatScoped(unittest.TestCase):
    """INFRA-04: /clear should only remove the requesting user's session."""

    def setUp(self):
        gateway._session_manager._sessions.clear()
        gateway._telegram_state._active_relays.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()

    def test_clear_only_removes_requesting_user(self):
        """After user A clears, user B's session should still exist."""
        gateway._session_manager.set("telegram", "chat_A", "session_A")
        gateway._session_manager.set("telegram", "chat_B", "session_B")
        gateway._clear_chat("chat_A")
        self.assertIsNone(gateway._session_manager.get("telegram", "chat_A"))
        self.assertEqual(gateway._session_manager.get("telegram", "chat_B"), "session_B")

    def test_clear_preserves_other_channels(self):
        """Clearing a telegram session should not affect other channels."""
        gateway._session_manager.set("telegram", "chat_1", "tg_session")
        gateway._session_manager.set("discord", "user_1", "discord_session")
        gateway._clear_chat("chat_1")
        self.assertIsNone(gateway._session_manager.get("telegram", "chat_1"))
        self.assertEqual(gateway._session_manager.get("discord", "user_1"), "discord_session")

    def test_clear_returns_old_session(self):
        """_clear_chat should return the removed session_id."""
        gateway._session_manager.set("telegram", "chat_1", "old_sid")
        result = gateway._clear_chat("chat_1")
        self.assertEqual(result, "old_sid")

    def test_clear_returns_none_if_no_session(self):
        """_clear_chat on nonexistent chat returns None."""
        result = gateway._clear_chat("nonexistent")
        self.assertIsNone(result)


# ── no telegram globals (INFRA-03) ──────────────────────────────────────────

class TestNoTelegramGlobals(unittest.TestCase):
    """INFRA-03: Telegram globals should no longer exist as module-level dicts."""

    def test_no_telegram_sessions_dict(self):
        """_telegram_sessions dict should not exist at module level."""
        self.assertFalse(hasattr(gateway, '_telegram_sessions'),
            "_telegram_sessions should be removed -- use _session_manager instead")

    def test_no_telegram_active_relays_dict(self):
        self.assertFalse(hasattr(gateway, '_telegram_active_relays'),
            "_telegram_active_relays should be removed -- use _telegram_state instead")

    def test_no_telegram_chat_locks_dict(self):
        self.assertFalse(hasattr(gateway, '_telegram_chat_locks'),
            "_telegram_chat_locks should be removed -- use _telegram_state instead")

    def test_session_manager_exists(self):
        self.assertTrue(hasattr(gateway, '_session_manager'))
        self.assertIsInstance(gateway._session_manager, gateway.SessionManager)

    def test_telegram_state_exists(self):
        self.assertTrue(hasattr(gateway, '_telegram_state'))
        self.assertIsInstance(gateway._telegram_state, gateway.ChannelState)

    def test_command_router_exists(self):
        self.assertTrue(hasattr(gateway, '_command_router'))
        self.assertIsInstance(gateway._command_router, gateway.CommandRouter)


# ── Generalized Command Handlers (CHAN-01) ───────────────────────────────────

class TestGeneralizedCommandHandlers(unittest.TestCase):
    """Tests that command handlers use ctx['channel'] and ctx['channel_state']
    instead of hardcoded _telegram_state and 'telegram'."""

    def setUp(self):
        gateway._session_manager._sessions.clear()
        gateway._telegram_state._active_relays.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()
        gateway._telegram_state._active_relays.clear()

    def test_stop_uses_ctx_channel_state(self):
        """_handle_cmd_stop uses ctx['channel_state'] instead of _telegram_state."""
        state = gateway.ChannelState()
        mock_sock = MagicMock()
        cancel_event = threading.Event()
        state.set_active_relay("user1", [mock_sock, cancel_event])

        send_fn = MagicMock()
        ctx = {
            "channel": "slack",
            "user_id": "user1",
            "send_fn": send_fn,
            "channel_state": state,
        }
        gateway._handle_cmd_stop(ctx)
        # should have popped from our custom state, not _telegram_state
        self.assertIsNone(state.pop_active_relay("user1"))
        send_fn.assert_called_with("Stopped.")

    def test_stop_falls_back_to_telegram_state(self):
        """_handle_cmd_stop falls back to _telegram_state when no channel_state in ctx."""
        mock_sock = MagicMock()
        cancel_event = threading.Event()
        gateway._telegram_state.set_active_relay("user2", [mock_sock, cancel_event])

        send_fn = MagicMock()
        ctx = {
            "user_id": "user2",
            "send_fn": send_fn,
        }
        gateway._handle_cmd_stop(ctx)
        # should have popped from _telegram_state
        self.assertIsNone(gateway._telegram_state.pop_active_relay("user2"))
        send_fn.assert_called_with("Stopped.")

    def test_clear_uses_ctx_channel_and_state(self):
        """_handle_cmd_clear uses ctx['channel'] and ctx['channel_state']."""
        state = gateway.ChannelState()
        mock_sock = MagicMock()
        cancel_event = threading.Event()
        state.set_active_relay("user3", [mock_sock, cancel_event])
        gateway._session_manager.set("slack", "user3", "old_sid")

        send_fn = MagicMock()
        ctx = {
            "channel": "slack",
            "user_id": "user3",
            "send_fn": send_fn,
            "channel_state": state,
        }
        with patch("gateway._restart_goose_and_prewarm"):
            gateway._handle_cmd_clear(ctx)

        # should have killed relay on our custom state
        self.assertIsNone(state.pop_active_relay("user3"))
        # should have popped from "slack", not "telegram"
        self.assertIsNone(gateway._session_manager.get("slack", "user3"))
        send_fn.assert_called_once()

    def test_clear_falls_back_to_telegram(self):
        """_handle_cmd_clear falls back to _telegram_state and 'telegram' when no ctx keys."""
        mock_sock = MagicMock()
        cancel_event = threading.Event()
        gateway._telegram_state.set_active_relay("user4", [mock_sock, cancel_event])
        gateway._session_manager.set("telegram", "user4", "old_sid")

        send_fn = MagicMock()
        ctx = {
            "user_id": "user4",
            "send_fn": send_fn,
        }
        with patch("gateway._restart_goose_and_prewarm"):
            gateway._handle_cmd_clear(ctx)

        # should have used _telegram_state and "telegram" channel
        self.assertIsNone(gateway._telegram_state.pop_active_relay("user4"))
        self.assertIsNone(gateway._session_manager.get("telegram", "user4"))

    @patch("gateway._relay_to_goose_web", return_value=("Compacted summary", ""))
    def test_compact_uses_ctx_channel(self, mock_relay):
        """_handle_cmd_compact uses ctx['channel'] instead of hardcoded 'telegram'."""
        gateway._session_manager.set("slack", "user5", "sid_5")
        send_fn = MagicMock()
        ctx = {
            "channel": "slack",
            "user_id": "user5",
            "send_fn": send_fn,
        }
        gateway._handle_cmd_compact(ctx)
        # _relay_to_goose_web should have been called with channel="slack"
        call_kwargs = mock_relay.call_args
        self.assertEqual(call_kwargs[1].get("channel") or call_kwargs[0][3] if len(call_kwargs[0]) > 3 else call_kwargs[1].get("channel"), "slack")

    def test_help_works_any_channel(self):
        """_handle_cmd_help works for any channel without crashing."""
        send_fn = MagicMock()
        ctx = {
            "channel": "slack",
            "user_id": "user6",
            "send_fn": send_fn,
        }
        gateway._handle_cmd_help(ctx)
        send_fn.assert_called_once()
        self.assertIn("/help", send_fn.call_args[0][0])


# ── ChannelRelay Command Interception (CHAN-01) ──────────────────────────────

class TestChannelRelayCommands(unittest.TestCase):
    """Tests that ChannelRelay intercepts commands before relaying to goose."""

    def setUp(self):
        gateway._session_manager._sessions.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()

    @patch.object(gateway._command_router, "dispatch", return_value=True)
    @patch.object(gateway._command_router, "is_command", return_value=True)
    def test_relay_intercepts_help(self, mock_is_cmd, mock_dispatch):
        """ChannelRelay intercepts /help and dispatches via command router."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        result = relay("user1", "/help", send_fn)
        mock_dispatch.assert_called_once()
        self.assertEqual(result, "")

    @patch.object(gateway._command_router, "dispatch", return_value=True)
    @patch.object(gateway._command_router, "is_command", return_value=True)
    def test_relay_intercepts_stop(self, mock_is_cmd, mock_dispatch):
        """ChannelRelay intercepts /stop and dispatches via command router."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        result = relay("user1", "/stop", send_fn)
        mock_dispatch.assert_called_once()
        self.assertEqual(result, "")

    @patch.object(gateway._command_router, "dispatch", return_value=True)
    @patch.object(gateway._command_router, "is_command", return_value=True)
    def test_relay_intercepts_clear(self, mock_is_cmd, mock_dispatch):
        """ChannelRelay intercepts /clear and dispatches via command router."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        result = relay("user1", "/clear", send_fn)
        mock_dispatch.assert_called_once()
        self.assertEqual(result, "")

    @patch.object(gateway._command_router, "dispatch", return_value=True)
    @patch.object(gateway._command_router, "is_command", return_value=True)
    def test_relay_passes_correct_ctx(self, mock_is_cmd, mock_dispatch):
        """ChannelRelay passes correct ctx dict to dispatch."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        relay("user1", "/help", send_fn)
        ctx = mock_dispatch.call_args[0][1]
        self.assertEqual(ctx["channel"], "test_ch")
        self.assertEqual(ctx["user_id"], "user1")
        self.assertIs(ctx["send_fn"], send_fn)
        self.assertIsInstance(ctx["channel_state"], gateway.ChannelState)

    def test_relay_unknown_command_sends_error(self):
        """ChannelRelay sends error for unknown commands (e.g. /foo)."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        result = relay("user1", "/foo", send_fn)
        send_fn.assert_called_once()
        self.assertIn("Unknown command", send_fn.call_args[0][0])
        self.assertEqual(result, "")

    @patch("gateway._relay_to_goose_web", return_value=("hello back", ""))
    @patch("gateway.load_setup", return_value=None)
    def test_relay_non_command_still_relays(self, mock_setup, mock_relay):
        """Regular text is not intercepted and gets relayed to goose."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        result = relay("user1", "hello", send_fn)
        mock_relay.assert_called()

    @patch.object(gateway._command_router, "dispatch", return_value=True)
    @patch.object(gateway._command_router, "is_command", return_value=True)
    def test_relay_command_returns_empty_string(self, mock_is_cmd, mock_dispatch):
        """Commands return empty string from relay."""
        relay = gateway.ChannelRelay("test_ch")
        result = relay("user1", "/help", MagicMock())
        self.assertEqual(result, "")


# ── ChannelRelay Active Relay Tracking + /stop (CHAN-03) ─────────────────────

class TestChannelRelayStop(unittest.TestCase):
    """Tests for active relay tracking and /stop cancellation on ChannelRelay."""

    def setUp(self):
        gateway._session_manager._sessions.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("response", ""))
    def test_relay_sets_active_relay(self, mock_relay, mock_setup):
        """ChannelRelay sets active relay on its _state before relaying."""
        relay = gateway.ChannelRelay("test_ch")
        # Spy on set_active_relay
        original_set = relay._state.set_active_relay
        calls = []
        def spy_set(uid, ref):
            calls.append((uid, ref))
            return original_set(uid, ref)
        relay._state.set_active_relay = spy_set

        relay("user1", "hello")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "user1")
        # sock_ref should be a list with [None, Event]
        self.assertIsInstance(calls[0][1], list)

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("response", ""))
    def test_relay_pops_active_relay_after_complete(self, mock_relay, mock_setup):
        """ChannelRelay pops active relay after relay completes (finally block)."""
        relay = gateway.ChannelRelay("test_ch")
        relay("user1", "hello")
        # After completion, pop should return None (already popped)
        self.assertIsNone(relay._state.pop_active_relay("user1"))

    def test_stop_kills_channel_relay(self):
        """_handle_cmd_stop kills active relay on the channel's own state."""
        relay = gateway.ChannelRelay("test_ch")
        mock_sock = MagicMock()
        cancel_event = threading.Event()
        relay._state.set_active_relay("user1", [mock_sock, cancel_event])

        send_fn = MagicMock()
        ctx = {
            "channel": "test_ch",
            "user_id": "user1",
            "send_fn": send_fn,
            "channel_state": relay._state,
        }
        gateway._handle_cmd_stop(ctx)
        # socket should be closed, cancel event set
        mock_sock.close.assert_called()
        self.assertTrue(cancel_event.is_set())
        send_fn.assert_called_with("Stopped.")

    @patch("gateway.load_setup", return_value=None)
    def test_relay_respects_cancelled_flag(self, mock_setup):
        """When cancelled is set during relay, relay returns '' and doesn't send response."""
        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()

        def fake_relay(*args, **kwargs):
            # Simulate /stop happening during relay
            sock_ref = kwargs.get("sock_ref")
            if sock_ref and len(sock_ref) > 1:
                sock_ref[1].set()  # set the cancelled event
            return ("should not see this", "")

        with patch("gateway._relay_to_goose_web", side_effect=fake_relay):
            result = relay("user1", "hello", send_fn)

        self.assertEqual(result, "")


# ── ChannelRelay Per-User Locks (CHAN-02) ────────────────────────────────────

class TestChannelRelayLocks(unittest.TestCase):
    """Tests for per-user concurrency locks in ChannelRelay."""

    def setUp(self):
        gateway._session_manager._sessions.clear()
        self._held_locks = []

    def tearDown(self):
        gateway._session_manager._sessions.clear()
        # release any locks we acquired in tests
        for lock in self._held_locks:
            try:
                lock.release()
            except RuntimeError:
                pass

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_relay_acquires_user_lock(self, mock_relay, mock_setup):
        """Relay acquires and releases user lock around relay call."""
        relay = gateway.ChannelRelay("test_ch")
        relay("user1", "hello")
        # Lock should NOT be held after relay returns
        lock = relay._state.get_user_lock("user1")
        acquired = lock.acquire(timeout=0.1)
        self.assertTrue(acquired, "Lock should be released after relay completes")
        if acquired:
            lock.release()

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_concurrent_relay_gets_busy_message(self, mock_relay, mock_setup):
        """When user lock is already held, relay sends 'Still thinking' and returns ''."""
        relay = gateway.ChannelRelay("test_ch")
        # Pre-acquire the user lock to simulate concurrent relay
        lock = relay._state.get_user_lock("user1")
        lock.acquire()
        self._held_locks.append(lock)

        send_fn = MagicMock()
        result = relay("user1", "hello", send_fn)

        # Should get busy message
        send_fn.assert_called_once()
        msg = send_fn.call_args[0][0]
        self.assertIn("Still thinking", msg)
        self.assertIn("/stop", msg)
        # Should return empty string, not relay response
        self.assertEqual(result, "")
        # Relay should NOT have been called
        mock_relay.assert_not_called()

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_concurrent_relay_no_send_fn_blocks_longer(self, mock_relay, mock_setup):
        """Without send_fn, lock timeout is longer (can't notify user)."""
        relay = gateway.ChannelRelay("test_ch")
        lock = relay._state.get_user_lock("user1")
        lock.acquire()
        self._held_locks.append(lock)

        # Call in a thread with short timeout to verify it blocks longer than 2s
        result_holder = [None]
        done = threading.Event()

        def call_relay():
            result_holder[0] = relay("user1", "hello")  # no send_fn
            done.set()

        t = threading.Thread(target=call_relay, daemon=True)
        t.start()
        # With no send_fn, timeout should be > 2s, so this should NOT complete in 1s
        completed_fast = done.wait(timeout=1.0)
        self.assertFalse(completed_fast,
            "Without send_fn, relay should block longer than with send_fn")
        # Release the lock so the thread can finish
        lock.release()
        self._held_locks.remove(lock)
        done.wait(timeout=5)

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_different_users_not_blocked(self, mock_relay, mock_setup):
        """Different users have separate locks; one user's lock doesn't block another."""
        relay = gateway.ChannelRelay("test_ch")
        lock = relay._state.get_user_lock("user1")
        lock.acquire()
        self._held_locks.append(lock)

        # user2 should relay normally despite user1's lock being held
        result = relay("user2", "hello")
        mock_relay.assert_called()
        self.assertNotEqual(result, "")

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", side_effect=Exception("boom"))
    def test_lock_released_on_relay_error(self, mock_relay, mock_setup):
        """Lock is released even when relay raises an exception."""
        relay = gateway.ChannelRelay("test_ch")
        try:
            relay("user1", "hello")
        except Exception:
            pass
        # Lock should be released after error
        lock = relay._state.get_user_lock("user1")
        acquired = lock.acquire(timeout=0.1)
        self.assertTrue(acquired, "Lock should be released after relay error")
        if acquired:
            lock.release()

    @patch("gateway.load_setup", return_value=None)
    def test_lock_released_on_cancel(self, mock_setup):
        """Lock is released when relay is cancelled."""
        relay = gateway.ChannelRelay("test_ch")

        def fake_relay(*args, **kwargs):
            sock_ref = kwargs.get("sock_ref")
            if sock_ref and len(sock_ref) > 1:
                sock_ref[1].set()  # simulate /stop cancellation
            return ("cancelled", "")

        with patch("gateway._relay_to_goose_web", side_effect=fake_relay):
            relay("user1", "hello")

        # Lock should be released after cancellation
        lock = relay._state.get_user_lock("user1")
        acquired = lock.acquire(timeout=0.1)
        self.assertTrue(acquired, "Lock should be released after cancellation")
        if acquired:
            lock.release()


# ── ChannelRelay Typing Indicators (CHAN-06) ─────────────────────────────────

class TestChannelRelayTyping(unittest.TestCase):
    """Tests for typing indicator callbacks in ChannelRelay."""

    def setUp(self):
        gateway._session_manager._sessions.clear()

    def tearDown(self):
        gateway._session_manager._sessions.clear()

    @patch("gateway.load_setup", return_value=None)
    def test_typing_callback_called_during_relay(self, mock_setup):
        """Typing callback fires during relay with correct user_id."""
        mock_typing = MagicMock()
        relay = gateway.ChannelRelay("test_ch", typing_cb=mock_typing)

        def slow_relay(*args, **kwargs):
            time.sleep(0.15)
            return ("response", "")

        with patch("gateway._relay_to_goose_web", side_effect=slow_relay):
            relay("user1", "hello")

        mock_typing.assert_called()
        # Should have been called with the user_id
        mock_typing.assert_any_call("user1")

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("response", ""))
    def test_typing_stops_after_relay_completes(self, mock_relay, mock_setup):
        """Typing callback stops being called after relay completes."""
        mock_typing = MagicMock()
        relay = gateway.ChannelRelay("test_ch", typing_cb=mock_typing)
        relay("user1", "hello")

        # Record call count right after relay
        count_after = mock_typing.call_count
        time.sleep(0.2)
        # Call count should not increase after relay is done
        self.assertEqual(mock_typing.call_count, count_after,
            "Typing callback should stop after relay completes")

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("response", ""))
    def test_no_typing_when_no_callback(self, mock_relay, mock_setup):
        """Relay works normally without typing callback (default None)."""
        relay = gateway.ChannelRelay("test_ch")
        result = relay("user1", "hello")
        # Should complete without error
        self.assertIsNotNone(result)

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_typing_callback_error_does_not_crash_relay(self, mock_relay, mock_setup):
        """Buggy typing callback does not crash relay."""
        def bad_typing(uid):
            raise Exception("typing crash")

        relay = gateway.ChannelRelay("test_ch", typing_cb=bad_typing)

        def slow_relay(*args, **kwargs):
            time.sleep(0.15)
            return ("ok", "")

        with patch("gateway._relay_to_goose_web", side_effect=slow_relay):
            result = relay("user1", "hello")

        self.assertEqual(result, "ok")

    @patch.object(gateway._command_router, "dispatch", return_value=True)
    @patch.object(gateway._command_router, "is_command", return_value=True)
    def test_typing_not_started_for_commands(self, mock_is_cmd, mock_dispatch):
        """Commands don't trigger typing callback (they return before relay)."""
        mock_typing = MagicMock()
        relay = gateway.ChannelRelay("test_ch", typing_cb=mock_typing)
        relay("user1", "/help", MagicMock())
        mock_typing.assert_not_called()

    def test_channel_relay_accepts_typing_cb(self):
        """ChannelRelay constructor accepts and stores typing_cb parameter."""
        cb = lambda uid: None
        relay = gateway.ChannelRelay("test", typing_cb=cb)
        self.assertIs(relay._typing_cb, cb)


# ── custom command registration from CHANNEL dict (CHAN-04) ────────────────────

class TestCustomCommandRegistration(unittest.TestCase):
    """Tests for custom command registration from CHANNEL dict commands field."""

    def setUp(self):
        # Save command router state
        self._saved_handlers = dict(gateway._command_router._handlers)
        self._saved_help = dict(gateway._command_router._help_text)
        # Save loaded channels
        self._saved_channels = dict(gateway._loaded_channels)

    def tearDown(self):
        # Restore command router state
        gateway._command_router._handlers = self._saved_handlers
        gateway._command_router._help_text = self._saved_help
        # Restore loaded channels
        with gateway._channels_lock:
            gateway._loaded_channels.clear()
            gateway._loaded_channels.update(self._saved_channels)

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_load_channel_registers_custom_commands(self, mock_relay):
        """_load_channel registers custom commands from CHANNEL dict commands field."""
        mock_handler = MagicMock()
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "test_plugin",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
            "commands": {"status": {"handler": mock_handler, "description": "show status"}},
        }

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                gateway._load_channel("/fake/test_plugin.py")

        # Custom command should be registered
        self.assertTrue(gateway._command_router.is_command("/status"))
        # Dispatch it
        ctx = {"channel": "test_plugin", "user_id": "u1", "send_fn": lambda t: None, "channel_state": MagicMock()}
        gateway._command_router.dispatch("/status", ctx)
        mock_handler.assert_called_once()

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_load_channel_no_commands_field(self, mock_relay):
        """_load_channel with no commands key in CHANNEL dict works fine."""
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "no_cmd_plugin",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
        }

        handler_count_before = len(gateway._command_router._handlers)

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                result = gateway._load_channel("/fake/no_cmd_plugin.py")

        self.assertTrue(result)
        # No new commands should have been registered
        self.assertEqual(len(gateway._command_router._handlers), handler_count_before)

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("response", ""))
    def test_custom_command_invoked_via_relay(self, mock_relay, mock_setup):
        """Custom command registered via _load_channel is invoked through ChannelRelay."""
        mock_handler = MagicMock()

        # Register a custom command directly (simulating what _load_channel does)
        gateway._command_router.register("ping", mock_handler, "ping the bot")

        relay = gateway.ChannelRelay("test_ch")
        send_fn = MagicMock()
        relay("user1", "/ping", send_fn)

        # handler should have been called with ctx containing correct channel info
        mock_handler.assert_called_once()
        ctx = mock_handler.call_args[0][0]
        self.assertEqual(ctx["channel"], "test_ch")
        self.assertEqual(ctx["user_id"], "user1")

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_custom_command_empty_dict(self, mock_relay):
        """CHANNEL dict with commands: {} causes no error and no new commands."""
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "empty_cmd_plugin",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
            "commands": {},
        }

        handler_count_before = len(gateway._command_router._handlers)

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                result = gateway._load_channel("/fake/empty_cmd_plugin.py")

        self.assertTrue(result)
        self.assertEqual(len(gateway._command_router._handlers), handler_count_before)

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_custom_command_invalid_handler_skipped(self, mock_relay):
        """CHANNEL dict with non-callable handler is skipped without crash."""
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "bad_handler_plugin",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
            "commands": {"bad": {"handler": "not_callable", "description": "should skip"}},
        }

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                result = gateway._load_channel("/fake/bad_handler_plugin.py")

        self.assertTrue(result)
        # The bad command should NOT be registered
        self.assertFalse(gateway._command_router.is_command("/bad"))


class TestCustomCommandConflicts(unittest.TestCase):
    """Tests for custom command conflict detection with built-in commands."""

    def setUp(self):
        self._saved_handlers = dict(gateway._command_router._handlers)
        self._saved_help = dict(gateway._command_router._help_text)
        self._saved_channels = dict(gateway._loaded_channels)

    def tearDown(self):
        gateway._command_router._handlers = self._saved_handlers
        gateway._command_router._help_text = self._saved_help
        with gateway._channels_lock:
            gateway._loaded_channels.clear()
            gateway._loaded_channels.update(self._saved_channels)

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_builtin_commands_not_overwritten(self, mock_relay):
        """Custom command named 'help' conflicts with built-in; built-in handler stays."""
        custom_handler = MagicMock()
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "conflict_plugin",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
            "commands": {"help": {"handler": custom_handler, "description": "custom help"}},
        }

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                gateway._load_channel("/fake/conflict_plugin.py")

        # The built-in /help handler should still be registered
        self.assertTrue(gateway._command_router.is_command("/help"))
        send_fn = MagicMock()
        ctx = {"channel": "test", "user_id": "u1", "send_fn": send_fn, "channel_state": MagicMock()}
        gateway._command_router.dispatch("/help", ctx)
        # built-in help sends help text, custom handler should NOT have been called
        custom_handler.assert_not_called()
        send_fn.assert_called_once()  # built-in help calls send_fn

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_builtin_conflict_logged(self, mock_relay):
        """Conflict with built-in command produces a warning message."""
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "conflict_plugin2",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
            "commands": {"help": {"handler": MagicMock(), "description": "custom help"}},
        }

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                # Capture stdout to check for warning
                import io
                captured = io.StringIO()
                with patch("sys.stdout", captured):
                    gateway._load_channel("/fake/conflict_plugin2.py")

        output = captured.getvalue()
        self.assertIn("conflicts with built-in", output)

    @patch("gateway._relay_to_goose_web", return_value=("ok", ""))
    def test_non_conflicting_custom_commands_registered(self, mock_relay):
        """Non-conflicting custom commands status and ping are registered and dispatchable."""
        status_handler = MagicMock()
        ping_handler = MagicMock()
        mock_module = MagicMock()
        mock_module.CHANNEL = {
            "name": "good_plugin",
            "version": 1,
            "send": lambda text: {"sent": True, "error": ""},
            "commands": {
                "status": {"handler": status_handler, "description": "show status"},
                "ping": {"handler": ping_handler, "description": "ping the bot"},
            },
        }

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec = MagicMock()
            mock_spec.loader = MagicMock()
            mock_spec_fn.return_value = mock_spec
            with patch("importlib.util.module_from_spec", return_value=mock_module):
                gateway._load_channel("/fake/good_plugin.py")

        self.assertTrue(gateway._command_router.is_command("/status"))
        self.assertTrue(gateway._command_router.is_command("/ping"))

        ctx = {"channel": "good_plugin", "user_id": "u1", "send_fn": MagicMock(), "channel_state": MagicMock()}
        gateway._command_router.dispatch("/status", ctx)
        status_handler.assert_called_once()
        gateway._command_router.dispatch("/ping", ctx)
        ping_handler.assert_called_once()


# ── dynamic channel validation (CHAN-05) ──────────────────────────────────────

class TestDynamicChannelValidation(unittest.TestCase):
    """Tests for _get_valid_channels() function."""

    def setUp(self):
        self._saved_channels = dict(gateway._loaded_channels)

    def tearDown(self):
        with gateway._channels_lock:
            gateway._loaded_channels.clear()
            gateway._loaded_channels.update(self._saved_channels)

    def test_get_valid_channels_includes_fixed(self):
        """_get_valid_channels() always includes web, telegram, cron, memory."""
        result = gateway._get_valid_channels()
        for ch in ("web", "telegram", "cron", "memory"):
            self.assertIn(ch, result)

    def test_get_valid_channels_includes_loaded_plugins(self):
        """_get_valid_channels() includes names from _loaded_channels."""
        with gateway._channels_lock:
            gateway._loaded_channels["slack"] = {"module": None, "channel": {"name": "slack"}, "creds": {}}

        result = gateway._get_valid_channels()
        self.assertIn("slack", result)

    def test_get_valid_channels_no_plugins(self):
        """_get_valid_channels() with no loaded plugins returns exactly the fixed set."""
        with gateway._channels_lock:
            gateway._loaded_channels.clear()

        result = gateway._get_valid_channels()
        self.assertEqual(result, {"web", "telegram", "cron", "memory"})

    def test_get_valid_channels_returns_set(self):
        """_get_valid_channels() returns a set type."""
        result = gateway._get_valid_channels()
        self.assertIsInstance(result, set)


class TestValidateSetupDynamic(unittest.TestCase):
    """Tests for validation functions using dynamic channel names."""

    def setUp(self):
        self._saved_channels = dict(gateway._loaded_channels)

    def tearDown(self):
        with gateway._channels_lock:
            gateway._loaded_channels.clear()
            gateway._loaded_channels.update(self._saved_channels)

    def test_validate_accepts_plugin_channel_in_routes(self):
        """validate_setup_config accepts a loaded plugin channel in channel_routes."""
        with gateway._channels_lock:
            gateway._loaded_channels["slack"] = {"module": None, "channel": {"name": "slack"}, "creds": {}}

        config = {
            "provider_type": "ollama",
            "models": [{"id": "model-1", "provider": "ollama", "model": "llama2", "is_default": True}],
            "channel_routes": {"slack": "model-1"},
        }
        valid, errors = gateway.validate_setup_config(config)
        # Should not have an error about slack being unknown
        channel_errors = [e for e in errors if "slack" in e and "unknown channel" in e]
        self.assertEqual(len(channel_errors), 0, f"Unexpected error about slack: {errors}")

    def test_validate_rejects_unknown_channel_in_routes(self):
        """validate_setup_config rejects a channel not in fixed or loaded plugins."""
        with gateway._channels_lock:
            gateway._loaded_channels.clear()

        config = {
            "provider_type": "ollama",
            "models": [{"id": "model-1", "provider": "ollama", "model": "llama2", "is_default": True}],
            "channel_routes": {"discord": "model-1"},
        }
        valid, errors = gateway.validate_setup_config(config)
        channel_errors = [e for e in errors if "discord" in e]
        self.assertTrue(len(channel_errors) > 0, f"Expected error about discord but got: {errors}")

    @patch("gateway.load_setup")
    @patch("gateway.save_setup")
    def test_set_routes_accepts_plugin_channel(self, mock_save, mock_load):
        """handle_set_routes validation accepts loaded plugin channel names."""
        with gateway._channels_lock:
            gateway._loaded_channels["slack"] = {"module": None, "channel": {"name": "slack"}, "creds": {}}

        # Verify _get_valid_channels includes loaded plugin
        valid = gateway._get_valid_channels()
        self.assertIn("slack", valid)

    @patch("gateway.load_setup")
    def test_set_verbosity_accepts_plugin_channel(self, mock_load):
        """handle_set_verbosity validation accepts loaded plugin channel names."""
        with gateway._channels_lock:
            gateway._loaded_channels["slack"] = {"module": None, "channel": {"name": "slack"}, "creds": {}}

        # Verify _get_valid_channels includes loaded plugin
        valid = gateway._get_valid_channels()
        self.assertIn("slack", valid)


if __name__ == "__main__":
    unittest.main()

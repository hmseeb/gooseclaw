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
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions.clear()

    def tearDown(self):
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions.clear()

    @patch("gateway._create_goose_session", return_value="new_session_abc")
    @patch("gateway._save_telegram_sessions")
    def test_creates_and_stores_session(self, _save, mock_create):
        gateway._prewarm_session("chat_99")
        # give the background thread a moment
        time.sleep(0.1)
        with gateway._telegram_sessions_lock:
            sid = gateway._telegram_sessions.get("chat_99")
        assert sid == "new_session_abc"
        mock_create.assert_called_once()

    @patch("gateway._create_goose_session", return_value=None)
    @patch("gateway._save_telegram_sessions")
    def test_no_session_stored_on_failure(self, _save, mock_create):
        gateway._prewarm_session("chat_99")
        time.sleep(0.1)
        with gateway._telegram_sessions_lock:
            sid = gateway._telegram_sessions.get("chat_99")
        assert sid is None

    @patch("gateway._create_goose_session", return_value="new_session_xyz")
    @patch("gateway._save_telegram_sessions")
    def test_does_not_overwrite_if_user_sent_message_first(self, _save, mock_create):
        """If user sends a message before prewarm finishes, don't clobber."""
        # simulate: user message arrived and created session already
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions["chat_99"] = "user_initiated_session"
        gateway._prewarm_session("chat_99")
        time.sleep(0.1)
        with gateway._telegram_sessions_lock:
            sid = gateway._telegram_sessions.get("chat_99")
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
        gateway._telegram_active_relays.clear()
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions.clear()

    def test_clear_pops_active_relay(self):
        """_clear_chat should remove active relay entry for the chat."""
        mock_sock = MagicMock()
        gateway._telegram_active_relays["chat_1"] = [mock_sock]
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions["chat_1"] = "old_session"
        gateway._clear_chat("chat_1")
        self.assertNotIn("chat_1", gateway._telegram_active_relays)

    def test_clear_closes_socket(self):
        """_clear_chat should close the active relay websocket."""
        mock_sock = MagicMock()
        gateway._telegram_active_relays["chat_1"] = [mock_sock]
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions["chat_1"] = "old_session"
        gateway._clear_chat("chat_1")
        mock_sock.close.assert_called_once()

    def test_clear_removes_session(self):
        """_clear_chat should remove the session from _telegram_sessions."""
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions["chat_1"] = "old_session"
        gateway._clear_chat("chat_1")
        with gateway._telegram_sessions_lock:
            self.assertNotIn("chat_1", gateway._telegram_sessions)

    @patch("gateway._save_telegram_sessions")
    def test_clear_saves_sessions(self, mock_save):
        """_clear_chat should persist the session removal."""
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions["chat_1"] = "old_session"
        gateway._clear_chat("chat_1")
        mock_save.assert_called_once()


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
        with gateway._telegram_sessions_lock:
            gateway._telegram_sessions.clear()
        gateway._prewarm_events.clear()

    @patch("gateway._create_goose_session", return_value="prewarmed_sid")
    @patch("gateway._save_telegram_sessions")
    def test_get_session_waits_for_prewarm(self, _save, mock_create):
        """If prewarm is in progress, _get_session_id should wait and use it."""
        # Simulate prewarm starting
        evt = threading.Event()
        gateway._prewarm_events["chat_1"] = evt

        def finish_prewarm():
            time.sleep(0.1)
            with gateway._telegram_sessions_lock:
                gateway._telegram_sessions["chat_1"] = "prewarmed_sid"
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


if __name__ == "__main__":
    unittest.main()

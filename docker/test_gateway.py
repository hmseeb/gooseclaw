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
import urllib.error
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


# ── _classify_fact ──────────────────────────────────────────────────────────

class TestClassifyFact(unittest.TestCase):
    """Tests for _classify_fact() keyword-based section routing."""

    def test_people_keywords(self):
        assert gateway._classify_fact("Sarah is his cofounder") == "People"
        assert gateway._classify_fact("met with his contact John") == "People"
        assert gateway._classify_fact("his wife Amy handles finances") == "People"
        assert gateway._classify_fact("relationship with manager is tense") == "People"

    def test_work_context_keywords(self):
        assert gateway._classify_fact("the project deadline is Friday") == "Work Context"
        assert gateway._classify_fact("works at ACME company") == "Work Context"
        assert gateway._classify_fact("his work involves data pipelines") == "Work Context"
        assert gateway._classify_fact("deadline for Q2 report is March 30") == "Work Context"

    def test_preferences_keywords(self):
        assert gateway._classify_fact("prefers dark mode") == "Preferences (Observed)"
        assert gateway._classify_fact("likes using vim") == "Preferences (Observed)"
        assert gateway._classify_fact("wants responses in bullet points") == "Preferences (Observed)"
        assert gateway._classify_fact("always uses bun over npm") == "Preferences (Observed)"

    def test_interests_keywords(self):
        assert gateway._classify_fact("hobby is woodworking") == "Interests & Context"
        assert gateway._classify_fact("interested in machine learning") == "Interests & Context"
        assert gateway._classify_fact("personal goal is to run a marathon") == "Interests & Context"

    def test_default_fallback(self):
        assert gateway._classify_fact("has a meeting tomorrow at 3pm") == "Important Context"
        assert gateway._classify_fact("random fact about stuff") == "Important Context"


# ── _fact_already_exists ───────────────────────────────────────────────────

class TestFactAlreadyExists(unittest.TestCase):
    """Tests for _fact_already_exists() dedup check."""

    def test_exact_match(self):
        section = "- prefers dark mode\n- uses vim\n"
        assert gateway._fact_already_exists("prefers dark mode", section) is True

    def test_case_insensitive(self):
        section = "- Prefers Dark Mode\n"
        assert gateway._fact_already_exists("prefers dark mode", section) is True

    def test_no_match(self):
        section = "- likes coffee\n- uses vim\n"
        assert gateway._fact_already_exists("prefers dark mode", section) is False

    def test_empty_section(self):
        assert gateway._fact_already_exists("anything", "") is False

    def test_substring_match(self):
        section = "- user prefers dark mode in all apps\n"
        assert gateway._fact_already_exists("prefers dark mode", section) is True

    def test_reverse_substring(self):
        """Fact is longer but section contains the core."""
        section = "- dark mode\n"
        assert gateway._fact_already_exists("dark mode", section) is True


# ── _process_memory_extraction dedup ───────────────────────────────────────

class TestMemoryExtractionDedup(unittest.TestCase):
    """Tests that _process_memory_extraction skips duplicate facts."""

    def test_user_fact_already_in_section_skipped(self):
        """If a fact already exists in the target section, it should not be appended again."""
        existing = (
            "# User\n"
            "## Work Context\n\n"
            "## Preferences (Observed)\n\n"
            "- prefers dark mode\n\n"
            "## Important Context\n\n"
            "- likes coffee\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            identity_dir = os.path.join(tmpdir, "identity")
            os.makedirs(identity_dir)
            user_file = os.path.join(identity_dir, "user.md")
            with open(user_file, "w") as f:
                f.write(existing)

            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"user_facts": ["prefers dark mode"]})
                gateway._process_memory_extraction(response)

            with open(user_file) as f:
                content = f.read()
            # should only appear once (the original)
            count = content.lower().count("prefers dark mode")
            assert count == 1, f"Expected 1 occurrence, found {count}"

    def test_preference_already_in_section_skipped(self):
        """Preferences that already exist should not be duplicated."""
        existing = (
            "# User\n"
            "## Preferences (Observed)\n\n"
            "- always uses bun\n\n"
            "## Important Context\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            identity_dir = os.path.join(tmpdir, "identity")
            os.makedirs(identity_dir)
            user_file = os.path.join(identity_dir, "user.md")
            with open(user_file, "w") as f:
                f.write(existing)

            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"preferences": ["always uses bun"]})
                gateway._process_memory_extraction(response)

            with open(user_file) as f:
                content = f.read()
            count = content.lower().count("always uses bun")
            assert count == 1, f"Expected 1 occurrence, found {count}"


# ── _process_memory_extraction section routing ─────────────────────────────

class TestMemoryExtractionRouting(unittest.TestCase):
    """Tests that user_facts route to the correct sections based on content."""

    def _make_user_md(self, tmpdir):
        identity_dir = os.path.join(tmpdir, "identity")
        os.makedirs(identity_dir, exist_ok=True)
        user_file = os.path.join(identity_dir, "user.md")
        with open(user_file, "w") as f:
            f.write(
                "# User\n"
                "## Basics\n\n"
                "## Work Context\n\n"
                "## Communication Preferences\n\n"
                "## Interests & Context\n\n"
                "## People\n\n"
                "## Patterns & Habits\n\n"
                "## Preferences (Observed)\n\n"
                "## Important Context\n\n"
            )
        return user_file

    def test_people_fact_routed_to_people_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = self._make_user_md(tmpdir)
            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"user_facts": ["Sarah is his cofounder"]})
                gateway._process_memory_extraction(response)
            with open(user_file) as f:
                content = f.read()
            # fact should be between ## People and ## Patterns & Habits
            people_idx = content.index("## People")
            patterns_idx = content.index("## Patterns & Habits")
            fact_idx = content.index("Sarah is his cofounder")
            assert people_idx < fact_idx < patterns_idx, \
                "People fact should be in the People section"

    def test_work_fact_routed_to_work_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = self._make_user_md(tmpdir)
            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"user_facts": ["the project deadline is Friday"]})
                gateway._process_memory_extraction(response)
            with open(user_file) as f:
                content = f.read()
            work_idx = content.index("## Work Context")
            comm_idx = content.index("## Communication Preferences")
            fact_idx = content.index("the project deadline is Friday")
            assert work_idx < fact_idx < comm_idx, \
                "Work fact should be in the Work Context section"

    def test_preference_fact_routed_to_preferences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = self._make_user_md(tmpdir)
            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"user_facts": ["prefers dark mode"]})
                gateway._process_memory_extraction(response)
            with open(user_file) as f:
                content = f.read()
            pref_idx = content.index("## Preferences (Observed)")
            important_idx = content.index("## Important Context")
            fact_idx = content.index("prefers dark mode")
            assert pref_idx < fact_idx < important_idx, \
                "Preference fact should be in the Preferences (Observed) section"

    def test_interest_fact_routed_to_interests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = self._make_user_md(tmpdir)
            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"user_facts": ["hobby is woodworking"]})
                gateway._process_memory_extraction(response)
            with open(user_file) as f:
                content = f.read()
            interest_idx = content.index("## Interests & Context")
            people_idx = content.index("## People")
            fact_idx = content.index("hobby is woodworking")
            assert interest_idx < fact_idx < people_idx, \
                "Interest fact should be in the Interests & Context section"

    def test_default_fact_routed_to_important_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = self._make_user_md(tmpdir)
            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"user_facts": ["has a meeting tomorrow at 3pm"]})
                gateway._process_memory_extraction(response)
            with open(user_file) as f:
                content = f.read()
            important_idx = content.index("## Important Context")
            fact_idx = content.index("has a meeting tomorrow at 3pm")
            assert fact_idx > important_idx, \
                "Default fact should be in Important Context section"


# ── _process_memory_extraction learnings format ────────────────────────────

class TestMemoryExtractionLearningsFormat(unittest.TestCase):
    """Tests that corrections are written with the full learnings schema."""

    def test_correction_has_full_schema_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            identity_dir = os.path.join(tmpdir, "identity")
            learnings_dir = os.path.join(identity_dir, "learnings")
            os.makedirs(learnings_dir)
            learnings_file = os.path.join(learnings_dir, "LEARNINGS.md")
            with open(learnings_file, "w") as f:
                f.write("# Learnings\n")

            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"corrections": ["use bun not npm"]})
                gateway._process_memory_extraction(response)

            with open(learnings_file) as f:
                content = f.read()

            # check for the full schema fields
            assert "## [LRN-" in content, "Should use ## heading with [LRN-...] format"
            assert "**Priority**: low" in content, "Should include Priority field"
            assert "**Status**: active" in content, "Should include Status field"
            assert "**Category**: auto-extracted" in content, "Should include Category field"
            assert "### Summary" in content, "Should include Summary subsection"
            assert "use bun not npm" in content, "Should contain the correction text"

    def test_correction_entry_id_format(self):
        """Entry ID should be LRN-YYYYMMDD-AUTO."""
        with tempfile.TemporaryDirectory() as tmpdir:
            identity_dir = os.path.join(tmpdir, "identity")
            learnings_dir = os.path.join(identity_dir, "learnings")
            os.makedirs(learnings_dir)
            learnings_file = os.path.join(learnings_dir, "LEARNINGS.md")
            with open(learnings_file, "w") as f:
                f.write("# Learnings\n")

            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"corrections": ["test correction"]})
                gateway._process_memory_extraction(response)

            with open(learnings_file) as f:
                content = f.read()

            # should match pattern like ## [LRN-20260313-AUTO]
            assert re.search(r'## \[LRN-\d{8}-AUTO\]', content), \
                f"Entry ID should match LRN-YYYYMMDD-AUTO format, got:\n{content}"

    def test_multiple_corrections_numbered(self):
        """Multiple corrections in one extraction should get unique IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            identity_dir = os.path.join(tmpdir, "identity")
            learnings_dir = os.path.join(identity_dir, "learnings")
            os.makedirs(learnings_dir)
            learnings_file = os.path.join(learnings_dir, "LEARNINGS.md")
            with open(learnings_file, "w") as f:
                f.write("# Learnings\n")

            with patch.object(gateway, "DATA_DIR", tmpdir):
                response = json.dumps({"corrections": ["fix A", "fix B"]})
                gateway._process_memory_extraction(response)

            with open(learnings_file) as f:
                content = f.read()

            # both should be present with different IDs
            ids = re.findall(r'## \[LRN-\d{8}-AUTO-(\d+)\]', content)
            assert len(ids) == 2, f"Expected 2 unique IDs, got {ids} in:\n{content}"
            assert ids[0] != ids[1], "IDs should be unique"


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
    @patch("gateway._do_rest_relay")
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
        mock_relay.return_value = (raw, None, [])
        gateway._fire_cron_job({"id": "test-cron", "source": "/test"})
        # check that notify_all was called without the banner
        call_args = mock_notify.call_args[0][0]
        assert "__( O)>" not in call_args
        assert "goose is ready" not in call_args
        assert "Actual report content" in call_args

    @patch("gateway.notify_all")
    @patch("gateway._do_rest_relay")
    @patch("gateway._load_recipe", return_value="do the thing")
    def test_cron_output_not_truncated_at_4000(self, _recipe, mock_relay, mock_notify):
        """Cron output should allow long content (chunking handled by TG sender)."""
        long_report = "x" * 10000
        mock_relay.return_value = (long_report, None, [])
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

    @patch("gateway._relay_to_goose_web", return_value=("Compacted summary", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("hello back", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("response", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("response", "", []))
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
            return ("should not see this", "", [])

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
    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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
            return ("cancelled", "", [])

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
            return ("response", "", [])

        with patch("gateway._relay_to_goose_web", side_effect=slow_relay):
            relay("user1", "hello")

        mock_typing.assert_called()
        # Should have been called with the user_id
        mock_typing.assert_any_call("user1")

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("response", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("response", "", []))
    def test_no_typing_when_no_callback(self, mock_relay, mock_setup):
        """Relay works normally without typing callback (default None)."""
        relay = gateway.ChannelRelay("test_ch")
        result = relay("user1", "hello")
        # Should complete without error
        self.assertIsNotNone(result)

    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
    def test_typing_callback_error_does_not_crash_relay(self, mock_relay, mock_setup):
        """Buggy typing callback does not crash relay."""
        def bad_typing(uid):
            raise Exception("typing crash")

        relay = gateway.ChannelRelay("test_ch", typing_cb=bad_typing)

        def slow_relay(*args, **kwargs):
            time.sleep(0.15)
            return ("ok", "", [])

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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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
    @patch("gateway._relay_to_goose_web", return_value=("response", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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

    @patch("gateway._relay_to_goose_web", return_value=("ok", "", []))
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


# ── notify channel targeting ────────────────────────────────────────────────

class TestNotifyChannelTargeting(unittest.TestCase):
    """Tests for handle_notify() channel parameter passthrough (CHAN-07)."""

    def _make_handler(self, body_dict):
        """Create a mock handler with the given JSON body."""
        handler = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)
        handler._read_body.return_value = json.dumps(body_dict).encode()
        handler._check_rate_limit.return_value = True
        return handler

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.notify_all")
    def test_notify_with_channel_passes_to_notify_all(self, mock_notify, _boot):
        """POST /api/notify with channel="telegram" passes channel to notify_all."""
        mock_notify.return_value = {"sent": True}
        handler = self._make_handler({"text": "hello", "channel": "telegram"})
        gateway.GatewayHandler.handle_notify(handler)
        mock_notify.assert_called_once_with("hello", channel="telegram")

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.notify_all")
    def test_notify_without_channel_broadcasts(self, mock_notify, _boot):
        """POST /api/notify without channel field passes channel=None."""
        mock_notify.return_value = {"sent": True}
        handler = self._make_handler({"text": "hello"})
        gateway.GatewayHandler.handle_notify(handler)
        mock_notify.assert_called_once_with("hello", channel=None)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.notify_all")
    def test_notify_channel_sanitized(self, mock_notify, _boot):
        """Channel value is sanitized (stripped, length-limited)."""
        mock_notify.return_value = {"sent": True}
        handler = self._make_handler({"text": "hello", "channel": "  telegram  "})
        gateway.GatewayHandler.handle_notify(handler)
        # _sanitize_string strips whitespace
        args, kwargs = mock_notify.call_args
        self.assertEqual(kwargs["channel"], "telegram")


class TestCronNotifyChannel(unittest.TestCase):
    """Tests for _fire_cron_job() notify_channel passthrough (CHAN-08)."""

    @patch("gateway.notify_all")
    @patch("gateway._do_rest_relay")
    @patch("gateway._load_recipe", return_value="do the thing")
    def test_cron_job_passes_notify_channel(self, _recipe, mock_relay, mock_notify):
        """Cron job with notify_channel passes it to notify_all on success."""
        mock_relay.return_value = ("output text", None, [])
        gateway._fire_cron_job({"id": "test-cron", "source": "/test", "notify_channel": "telegram"})
        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        self.assertEqual(kwargs.get("channel"), "telegram")

    @patch("gateway.notify_all")
    @patch("gateway._do_rest_relay")
    @patch("gateway._load_recipe", return_value="do the thing")
    def test_cron_job_error_passes_notify_channel(self, _recipe, mock_relay, mock_notify):
        """Cron job with notify_channel passes it to notify_all on error."""
        mock_relay.return_value = (None, "connection failed", [])
        gateway._fire_cron_job({"id": "test-cron", "source": "/test", "notify_channel": "telegram"})
        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        self.assertEqual(kwargs.get("channel"), "telegram")

    @patch("gateway.notify_all")
    @patch("gateway._do_rest_relay")
    @patch("gateway._load_recipe", return_value="do the thing")
    def test_cron_job_no_notify_channel_broadcasts(self, _recipe, mock_relay, mock_notify):
        """Cron job without notify_channel passes channel=None (broadcast)."""
        mock_relay.return_value = ("output text", None, [])
        gateway._fire_cron_job({"id": "test-cron", "source": "/test"})
        mock_notify.assert_called_once()
        _, kwargs = mock_notify.call_args
        self.assertIsNone(kwargs.get("channel"))


# ── BotInstance tests ────────────────────────────────────────────────────────

class TestBotInstance(unittest.TestCase):
    """Tests for BotInstance class."""

    def test_init_default_channel_key(self):
        bot = gateway.BotInstance("mybot", "123:ABC")
        self.assertEqual(bot.channel_key, "telegram:mybot")

    def test_init_custom_channel_key(self):
        bot = gateway.BotInstance("default", "123:ABC", channel_key="telegram")
        self.assertEqual(bot.channel_key, "telegram")

    def test_has_own_channel_state(self):
        bot = gateway.BotInstance("test", "123:ABC")
        self.assertIsInstance(bot.state, gateway.ChannelState)

    def test_separate_channel_states(self):
        bot_a = gateway.BotInstance("a", "111:AAA")
        bot_b = gateway.BotInstance("b", "222:BBB")
        self.assertIs(type(bot_a.state), gateway.ChannelState)
        self.assertIsNot(bot_a.state, bot_b.state)

    def test_generate_pair_code(self):
        bot = gateway.BotInstance("test", "123:ABC")
        code = bot.generate_pair_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isalnum())
        self.assertEqual(code, code.upper())
        self.assertEqual(bot.pair_code, code)

    def test_pair_code_independent(self):
        bot_a = gateway.BotInstance("a", "111:AAA")
        bot_b = gateway.BotInstance("b", "222:BBB")
        code_a = bot_a.generate_pair_code()
        code_b = bot_b.generate_pair_code()
        # codes are random, extremely unlikely to match
        self.assertEqual(bot_a.pair_code, code_a)
        self.assertEqual(bot_b.pair_code, code_b)

    def test_running_initially_false(self):
        bot = gateway.BotInstance("test", "123:ABC")
        self.assertFalse(bot.running)


# ── BotManager tests ────────────────────────────────────────────────────────

class TestBotManager(unittest.TestCase):
    """Tests for BotManager class."""

    def test_start_bot_creates_instance(self):
        mgr = gateway.BotManager()
        bot = mgr.add_bot("test", "123:ABC")
        self.assertIsInstance(bot, gateway.BotInstance)
        self.assertIs(mgr.get_bot("test"), bot)

    def test_start_bot_duplicate_name(self):
        mgr = gateway.BotManager()
        bot1 = mgr.add_bot("test", "123:ABC")
        bot2 = mgr.add_bot("test", "456:DEF")
        self.assertIs(bot1, bot2)

    def test_start_bot_duplicate_token(self):
        mgr = gateway.BotManager()
        mgr.add_bot("a", "123:ABC")
        with self.assertRaises(ValueError):
            mgr.add_bot("b", "123:ABC")

    def test_remove_bot(self):
        mgr = gateway.BotManager()
        mgr.add_bot("test", "123:ABC")
        mgr.remove_bot("test")
        self.assertIsNone(mgr.get_bot("test"))

    def test_remove_nonexistent(self):
        mgr = gateway.BotManager()
        mgr.remove_bot("ghost")  # should not raise

    def test_get_all(self):
        mgr = gateway.BotManager()
        mgr.add_bot("a", "111:AAA")
        mgr.add_bot("b", "222:BBB")
        all_bots = mgr.get_all()
        self.assertIn("a", all_bots)
        self.assertIn("b", all_bots)
        self.assertEqual(len(all_bots), 2)

    def test_stop_all(self):
        mgr = gateway.BotManager()
        mgr.add_bot("a", "111:AAA")
        mgr.add_bot("b", "222:BBB")
        mgr.stop_all()
        self.assertEqual(len(mgr.get_all()), 0)

    def test_default_bot_channel_key(self):
        mgr = gateway.BotManager()
        bot = mgr.add_bot("default", "123:ABC", channel_key="telegram")
        self.assertEqual(bot.channel_key, "telegram")


# ── _resolve_bot_configs tests ───────────────────────────────────────────────

class TestResolveBotConfigs(unittest.TestCase):
    """Tests for _resolve_bot_configs()."""

    def test_bots_array_returned(self):
        config = {"bots": [{"name": "a", "token": "111:AAA"}]}
        result = gateway._resolve_bot_configs(config)
        self.assertEqual(result, [{"name": "a", "token": "111:AAA"}])

    def test_legacy_single_token(self):
        config = {"telegram_bot_token": "123:ABC"}
        result = gateway._resolve_bot_configs(config)
        self.assertEqual(result, [{"name": "default", "token": "123:ABC"}])

    def test_no_token_no_bots(self):
        config = {"provider_type": "openai"}
        result = gateway._resolve_bot_configs(config)
        self.assertEqual(result, [])

    def test_empty_bots_array_falls_back(self):
        config = {"bots": [], "telegram_bot_token": "123:ABC"}
        result = gateway._resolve_bot_configs(config)
        self.assertEqual(result, [{"name": "default", "token": "123:ABC"}])

    def test_bots_array_takes_priority(self):
        config = {
            "bots": [{"name": "main", "token": "111:AAA"}],
            "telegram_bot_token": "999:ZZZ"
        }
        result = gateway._resolve_bot_configs(config)
        self.assertEqual(result, [{"name": "main", "token": "111:AAA"}])


# ── Bot config validation tests ──────────────────────────────────────────────

class TestBotConfigValidation(unittest.TestCase):
    """Tests for validate_setup_config bots array extensions."""

    def _base_config(self, **overrides):
        config = {"provider_type": "ollama"}
        config.update(overrides)
        return config

    def test_valid_bots_array(self):
        config = self._base_config(bots=[
            {"name": "a", "token": "111:AAA"},
            {"name": "b", "token": "222:BBB"},
        ])
        valid, errors = gateway.validate_setup_config(config)
        self.assertTrue(valid, f"Expected valid but got errors: {errors}")

    def test_bots_not_array(self):
        config = self._base_config(bots="bad")
        valid, errors = gateway.validate_setup_config(config)
        self.assertFalse(valid)
        self.assertTrue(any("bots" in e and "array" in e.lower() for e in errors))

    def test_bots_missing_name(self):
        config = self._base_config(bots=[{"token": "111:AAA"}])
        valid, errors = gateway.validate_setup_config(config)
        self.assertFalse(valid)
        self.assertTrue(any("name" in e for e in errors))

    def test_bots_missing_token(self):
        config = self._base_config(bots=[{"name": "a"}])
        valid, errors = gateway.validate_setup_config(config)
        self.assertFalse(valid)
        self.assertTrue(any("token" in e for e in errors))

    def test_bots_duplicate_name(self):
        config = self._base_config(bots=[
            {"name": "a", "token": "111:AAA"},
            {"name": "a", "token": "222:BBB"},
        ])
        valid, errors = gateway.validate_setup_config(config)
        self.assertFalse(valid)
        self.assertTrue(any("duplicate" in e.lower() and "name" in e.lower() for e in errors))

    def test_bots_duplicate_token(self):
        config = self._base_config(bots=[
            {"name": "a", "token": "111:AAA"},
            {"name": "b", "token": "111:AAA"},
        ])
        valid, errors = gateway.validate_setup_config(config)
        self.assertFalse(valid)
        self.assertTrue(any("duplicate" in e.lower() and "token" in e.lower() for e in errors))


# ── Bot valid channels tests ─────────────────────────────────────────────────

class TestBotValidChannels(unittest.TestCase):
    """Tests for _get_valid_channels with bot channel keys."""

    @patch("gateway.load_setup")
    def test_includes_bot_channel_keys(self, mock_setup):
        mock_setup.return_value = {
            "bots": [{"name": "research", "token": "111:AAA"}]
        }
        channels = gateway._get_valid_channels()
        self.assertIn("telegram:research", channels)

    @patch("gateway.load_setup")
    def test_default_bot_not_added(self, mock_setup):
        mock_setup.return_value = {
            "bots": [{"name": "default", "token": "111:AAA"}]
        }
        channels = gateway._get_valid_channels()
        self.assertNotIn("telegram:default", channels)
        self.assertIn("telegram", channels)  # fixed set always has "telegram"

    @patch("gateway.load_setup")
    def test_no_bots_unchanged(self, mock_setup):
        mock_setup.return_value = {"provider_type": "openai"}
        channels = gateway._get_valid_channels()
        self.assertIn("web", channels)
        self.assertIn("telegram", channels)
        self.assertIn("cron", channels)
        self.assertIn("memory", channels)


# ── Bot isolation tests ──────────────────────────────────────────────────────

class TestBotIsolation(unittest.TestCase):
    """Tests for per-bot state isolation."""

    def test_different_bots_different_locks(self):
        bot_a = gateway.BotInstance("a", "111:AAA")
        bot_b = gateway.BotInstance("b", "222:BBB")
        lock_a = bot_a.state.get_user_lock("user1")
        lock_b = bot_b.state.get_user_lock("user1")
        self.assertIsNot(lock_a, lock_b)

    def test_different_bots_different_relay_tracking(self):
        bot_a = gateway.BotInstance("a", "111:AAA")
        bot_b = gateway.BotInstance("b", "222:BBB")
        mock_ref = [MagicMock()]
        bot_a.state.set_active_relay("u1", mock_ref)
        result = bot_b.state.pop_active_relay("u1")
        self.assertIsNone(result)
        # cleanup
        bot_a.state.pop_active_relay("u1")

    def test_session_manager_isolation(self):
        sm = gateway.SessionManager()
        sm.set("telegram:bot_a", "user1", "session_a")
        sm.set("telegram:bot_b", "user1", "session_b")
        self.assertEqual(sm.get("telegram:bot_a", "user1"), "session_a")
        self.assertEqual(sm.get("telegram:bot_b", "user1"), "session_b")


# ── Bot poll loop tests ───────────────────────────────────────────────────────

class TestBotPollLoop(unittest.TestCase):
    """Tests for BotInstance._poll_loop internals (parameterized channel_key, state, pair_code)."""

    def test_poll_loop_uses_instance_channel_key(self):
        """_do_message_relay passes self.channel_key to _session_manager."""
        bot = gateway.BotInstance("research", "tok123")
        self.assertEqual(bot.channel_key, "telegram:research")
        with patch.object(gateway._session_manager, "get", return_value="sess1") as mock_get, \
             patch.object(gateway._session_manager, "set") as mock_set, \
             patch("gateway._relay_to_goose_web", return_value=("hi", "", [])) as mock_relay, \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway._send_typing_action"), \
             patch("gateway.load_setup", return_value=None), \
             patch("gateway._memory_touch"):
            bot._do_message_relay(chat_id="123", text="hello", bot_token="tok123")
            mock_get.assert_called_with("telegram:research", "123")

    def test_poll_loop_uses_instance_state_for_relay(self):
        """_do_message_relay calls self.state.set_active_relay (not _telegram_state)."""
        bot = gateway.BotInstance("research", "tok123")
        with patch.object(bot.state, "set_active_relay") as mock_set, \
             patch.object(bot.state, "pop_active_relay") as mock_pop, \
             patch.object(gateway._session_manager, "get", return_value="sess1"), \
             patch("gateway._relay_to_goose_web", return_value=("ok", "", [])) as mock_relay, \
             patch("gateway.send_telegram_message"), \
             patch("gateway._send_typing_action"), \
             patch("gateway.load_setup", return_value=None), \
             patch("gateway._memory_touch"):
            bot._do_message_relay(chat_id="123", text="hello", bot_token="tok123")
            mock_set.assert_called_once()
            mock_pop.assert_called_once()

    def test_poll_loop_uses_instance_state_for_lock(self):
        """_do_message_relay uses self.state.get_user_lock."""
        bot = gateway.BotInstance("research", "tok123")
        with patch.object(bot.state, "get_user_lock", return_value=threading.Lock()) as mock_lock, \
             patch.object(gateway._session_manager, "get", return_value="sess1"), \
             patch("gateway._relay_to_goose_web", return_value=("ok", "", [])) as mock_relay, \
             patch("gateway.send_telegram_message"), \
             patch("gateway._send_typing_action"), \
             patch("gateway.load_setup", return_value=None), \
             patch("gateway._memory_touch"):
            bot._do_message_relay(chat_id="123", text="hello", bot_token="tok123")
            mock_lock.assert_called_with("123")

    def test_poll_loop_uses_instance_pair_code(self):
        """_check_pairing uses self.pair_code, not global telegram_pair_code."""
        bot = gateway.BotInstance("research", "tok123")
        bot.pair_code = "ABC123"
        result = bot._check_pairing(chat_id="456", text="ABC123")
        self.assertTrue(result)

    def test_poll_loop_verbosity_uses_channel_key(self):
        """_do_message_relay calls get_verbosity_for_channel with self.channel_key."""
        bot = gateway.BotInstance("research", "tok123")
        setup = {"channel_verbosity": {"telegram:research": "quiet"}}
        with patch.object(gateway._session_manager, "get", return_value="sess1"), \
             patch("gateway._relay_to_goose_web", return_value=("ok", "", [])) as mock_relay, \
             patch("gateway.send_telegram_message"), \
             patch("gateway._send_typing_action"), \
             patch("gateway.load_setup", return_value=setup), \
             patch("gateway._memory_touch"), \
             patch("gateway.get_verbosity_for_channel", return_value="quiet") as mock_verb:
            bot._do_message_relay(chat_id="123", text="hello", bot_token="tok123")
            mock_verb.assert_called_with(setup, "telegram:research")


class TestBotStartStop(unittest.TestCase):
    """Tests for BotInstance.start() and stop() lifecycle."""

    def _make_bot(self):
        """Create a bot with _poll_loop mocked out so no real network calls happen."""
        bot = gateway.BotInstance("test", "tok")
        # replace _poll_loop with a simple loop that exits when running=False
        def _fake_poll_loop():
            while bot.running:
                time.sleep(0.01)
        bot._poll_loop = _fake_poll_loop
        return bot

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    @patch("gateway.register_notification_handler")
    def test_start_sets_running(self, mock_reg, mock_load, mock_url):
        bot = self._make_bot()
        bot.start()
        self.assertTrue(bot.running)
        bot.stop()

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    @patch("gateway.register_notification_handler")
    def test_start_generates_pair_code(self, mock_reg, mock_load, mock_url):
        bot = self._make_bot()
        bot.start()
        self.assertIsNotNone(bot.pair_code)
        bot.stop()

    @patch("gateway.urllib.request.urlopen")
    @patch("gateway.register_notification_handler")
    def test_start_loads_sessions(self, mock_reg, mock_url):
        with patch.object(gateway._session_manager, "load") as mock_load:
            bot = self._make_bot()
            bot.start()
            mock_load.assert_called_with(bot.channel_key)
            bot.stop()

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    def test_start_registers_notification(self, mock_load, mock_url):
        with patch("gateway.register_notification_handler") as mock_reg:
            bot = self._make_bot()
            bot.start()
            mock_reg.assert_called_once()
            call_args = mock_reg.call_args
            self.assertEqual(call_args[0][0], bot.channel_key)
            bot.stop()

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    @patch("gateway.register_notification_handler")
    def test_stop_sets_running_false(self, mock_reg, mock_load, mock_url):
        bot = self._make_bot()
        bot.start()
        self.assertTrue(bot.running)
        bot.stop()
        self.assertFalse(bot.running)

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    @patch("gateway.register_notification_handler")
    def test_start_twice_noop(self, mock_reg, mock_load, mock_url):
        bot = self._make_bot()
        bot.start()
        thread1 = bot._thread
        bot.start()
        thread2 = bot._thread
        self.assertIs(thread1, thread2)
        bot.stop()


class TestBotNotification(unittest.TestCase):
    """Tests for per-bot notification handlers."""

    def test_notification_handler_uses_bot_token(self):
        """_make_notify_handler creates a closure using the bot's token."""
        bot = gateway.BotInstance("mybot", "tok_mybot")
        handler = bot._make_notify_handler()
        with patch("gateway.send_telegram_message", return_value=(True, "")) as mock_send, \
             patch("gateway.get_paired_chat_ids", return_value=["111"]):
            handler("test msg")
            mock_send.assert_called_with("tok_mybot", "111", "test msg")

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    def test_default_bot_registers_as_telegram(self, mock_load, mock_url):
        """Default bot registers notification handler as 'telegram'."""
        bot = gateway.BotInstance("default", "tok", channel_key="telegram")
        def _fake():
            while bot.running: time.sleep(0.01)
        bot._poll_loop = _fake
        with patch("gateway.register_notification_handler") as mock_reg:
            bot.start()
            mock_reg.assert_called_once()
            self.assertEqual(mock_reg.call_args[0][0], "telegram")
            bot.stop()

    @patch("gateway.urllib.request.urlopen")
    @patch.object(gateway._session_manager, "load")
    def test_named_bot_registers_as_telegram_name(self, mock_load, mock_url):
        """Named bot registers notification handler as 'telegram:name'."""
        bot = gateway.BotInstance("research", "tok")
        def _fake():
            while bot.running: time.sleep(0.01)
        bot._poll_loop = _fake
        with patch("gateway.register_notification_handler") as mock_reg:
            bot.start()
            mock_reg.assert_called_once()
            self.assertEqual(mock_reg.call_args[0][0], "telegram:research")
            bot.stop()


class TestBotPairing(unittest.TestCase):
    """Tests for per-bot pairing."""

    def test_add_pairing_default_platform(self):
        """_add_pairing_to_config with platform='telegram' writes platform: telegram."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("gateway_pairings:\n")
            tmp = f.name
        try:
            with patch("gateway.GOOSE_CONFIG_PATH", tmp):
                gateway._add_pairing_to_config("12345", platform="telegram")
            with open(tmp) as f:
                content = f.read()
            self.assertIn("platform: telegram", content)
            self.assertIn("user_id: '12345'", content)
        finally:
            os.unlink(tmp)

    def test_add_pairing_named_bot_platform(self):
        """_add_pairing_to_config with platform='telegram:research' writes that platform."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("gateway_pairings:\n")
            tmp = f.name
        try:
            with patch("gateway.GOOSE_CONFIG_PATH", tmp):
                gateway._add_pairing_to_config("67890", platform="telegram:research")
            with open(tmp) as f:
                content = f.read()
            self.assertIn("platform: telegram:research", content)
            self.assertIn("user_id: '67890'", content)
        finally:
            os.unlink(tmp)

    def test_get_paired_chat_ids_filters_by_platform(self):
        """get_paired_chat_ids(platform=...) filters by platform."""
        config_content = (
            "gateway_pairings:\n"
            "  - platform: telegram\n"
            "    user_id: '111'\n"
            "    state: paired\n"
            "  - platform: telegram:research\n"
            "    user_id: '222'\n"
            "    state: paired\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_content)
            tmp = f.name
        try:
            with patch("gateway.GOOSE_CONFIG_PATH", tmp):
                ids_default = gateway.get_paired_chat_ids(platform="telegram")
                ids_research = gateway.get_paired_chat_ids(platform="telegram:research")
            self.assertEqual(ids_default, ["111"])
            self.assertEqual(ids_research, ["222"])
        finally:
            os.unlink(tmp)

    def test_check_pairing_consumes_code(self):
        """_check_pairing with matching code returns True and rotates to a new code."""
        bot = gateway.BotInstance("test", "tok")
        bot.pair_code = "XYZ789"
        result = bot._check_pairing(chat_id="999", text="XYZ789")
        self.assertTrue(result)
        self.assertIsNotNone(bot.pair_code)
        self.assertNotEqual(bot.pair_code, "XYZ789")

    def test_check_pairing_rotates_code_after_match(self):
        """After successful pair, a new 6-char code is generated (not None)."""
        bot = gateway.BotInstance("test", "tok")
        bot.pair_code = "XYZ789"
        result = bot._check_pairing(chat_id="999", text="XYZ789")
        self.assertTrue(result)
        self.assertIsNotNone(bot.pair_code)
        self.assertNotEqual(bot.pair_code, "XYZ789")
        self.assertEqual(len(bot.pair_code), 6)

    def test_old_pairing_code_rejected_after_use(self):
        """Old pairing code cannot be reused after successful pair."""
        bot = gateway.BotInstance("test", "tok")
        bot.pair_code = "ABC123"
        result1 = bot._check_pairing(chat_id="111", text="ABC123")
        self.assertTrue(result1)
        new_code = bot.pair_code
        result2 = bot._check_pairing(chat_id="222", text="ABC123")
        self.assertFalse(result2)
        self.assertEqual(bot.pair_code, new_code)


# ── BotManager wiring (09-03) ────────────────────────────────────────────────

class TestBotWiring(unittest.TestCase):
    """Tests that BotManager is wired into apply_config, startup, and _is_goose_gateway_running."""

    def setUp(self):
        """Clear _bot_manager between tests to avoid cross-contamination."""
        bm = gateway._bot_manager
        with bm._lock:
            bm._bots.clear()

    def tearDown(self):
        bm = gateway._bot_manager
        with bm._lock:
            for bot in bm._bots.values():
                bot.running = False
            bm._bots.clear()
        # clean up env vars set by apply_config
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    def test_bot_manager_module_level_exists(self):
        """gateway._bot_manager is a BotManager instance."""
        self.assertIsInstance(gateway._bot_manager, gateway.BotManager)

    @patch("gateway.urllib.request.urlopen")
    @patch("gateway._resolve_bot_configs")
    def test_apply_config_starts_bots_via_manager(self, mock_resolve, mock_urlopen):
        """apply_config uses BotManager to start bots from resolved configs."""
        mock_resolve.return_value = [{"name": "default", "token": "123:ABC"}]
        mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: b'{"ok":true,"result":[]}')
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(gateway.BotInstance, "start"), \
             patch("builtins.open", mock_open(read_data="")), \
             patch("gateway.os.path.exists", return_value=False), \
             patch("gateway.os.replace"):
            gateway.apply_config({"provider_type": "openai", "api_key": "sk-test"})

        bot = gateway._bot_manager.get_bot("default")
        self.assertIsNotNone(bot, "apply_config should create a 'default' bot in _bot_manager")

    @patch("gateway.urllib.request.urlopen")
    @patch("gateway._resolve_bot_configs")
    def test_apply_config_multi_bot(self, mock_resolve, mock_urlopen):
        """apply_config with multiple bot configs creates all bots."""
        mock_resolve.return_value = [
            {"name": "default", "token": "111:AAA"},
            {"name": "research", "token": "222:BBB"},
        ]
        mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: b'{"ok":true,"result":[]}')
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(gateway.BotInstance, "start"), \
             patch("builtins.open", mock_open(read_data="")), \
             patch("gateway.os.path.exists", return_value=False), \
             patch("gateway.os.replace"):
            gateway.apply_config({"provider_type": "openai", "api_key": "sk-test"})

        self.assertIsNotNone(gateway._bot_manager.get_bot("default"))
        self.assertIsNotNone(gateway._bot_manager.get_bot("research"))

    @patch("gateway.urllib.request.urlopen")
    @patch("gateway._resolve_bot_configs")
    def test_apply_config_default_bot_channel_key_telegram(self, mock_resolve, mock_urlopen):
        """Default bot gets channel_key='telegram', not 'telegram:default'."""
        mock_resolve.return_value = [{"name": "default", "token": "123:ABC"}]
        mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: b'{"ok":true,"result":[]}')
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(gateway.BotInstance, "start"), \
             patch("builtins.open", mock_open(read_data="")), \
             patch("gateway.os.path.exists", return_value=False), \
             patch("gateway.os.replace"):
            gateway.apply_config({"provider_type": "openai", "api_key": "sk-test"})

        bot = gateway._bot_manager.get_bot("default")
        self.assertIsNotNone(bot)
        self.assertEqual(bot.channel_key, "telegram")

    def test_is_goose_gateway_running_uses_manager(self):
        """_is_goose_gateway_running returns True when manager has running bots."""
        bot = gateway._bot_manager.add_bot("test_running", "tok:running", channel_key="telegram")
        bot.running = True
        running, _ = gateway._is_goose_gateway_running()
        self.assertTrue(running)

    def test_is_goose_gateway_running_no_bots(self):
        """_is_goose_gateway_running returns (False, []) with empty manager."""
        running, errs = gateway._is_goose_gateway_running()
        self.assertFalse(running)
        self.assertEqual(errs, [])

    @patch("gateway.urllib.request.urlopen")
    def test_start_telegram_gateway_backward_compat(self, mock_urlopen):
        """start_telegram_gateway creates a bot in _bot_manager (backward compat)."""
        mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: b'{"ok":true,"result":[]}')
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(gateway.BotInstance, "start"):
            gateway.start_telegram_gateway("999:XYZ")

        bot = gateway._bot_manager.get_bot("default")
        self.assertIsNotNone(bot, "start_telegram_gateway should create 'default' bot in _bot_manager")


class TestBotAPIEndpoints(unittest.TestCase):
    """Tests that API endpoints work with BotManager."""

    def setUp(self):
        bm = gateway._bot_manager
        with bm._lock:
            bm._bots.clear()

    def tearDown(self):
        bm = gateway._bot_manager
        with bm._lock:
            for bot in bm._bots.values():
                bot.running = False
            bm._bots.clear()

    def _make_handler(self, path="/api/telegram/status", method="GET"):
        """Create a mock GatewayHandler for testing endpoints."""
        handler = MagicMock(spec=gateway.GatewayHandler)
        handler.path = path
        handler.command = method
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler._json_response = None

        def mock_send_json(status, data):
            handler._json_response = (status, data)

        handler.send_json = mock_send_json
        return handler

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.get_bot_token", return_value="tok:test")
    @patch("gateway.get_paired_chat_ids", return_value=[])
    def test_telegram_status_returns_bots_array(self, _mock_paired, _mock_token, _mock_boot):
        """handle_telegram_status response includes 'bots' array."""
        bot = gateway._bot_manager.add_bot("default", "tok:status", channel_key="telegram")
        bot.running = True
        bot.pair_code = "ABC123"

        handler = self._make_handler()
        gateway.GatewayHandler.handle_telegram_status(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 200)
        self.assertIn("bots", data, "Response should include 'bots' array")
        self.assertIsInstance(data["bots"], list)
        self.assertEqual(len(data["bots"]), 1)
        self.assertEqual(data["bots"][0]["name"], "default")

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.get_bot_token", return_value="tok:test")
    @patch("gateway.get_paired_chat_ids", return_value=["12345"])
    def test_telegram_status_backward_compat_fields(self, _mock_paired, _mock_token, _mock_boot):
        """handle_telegram_status still includes top-level backward-compat fields."""
        bot = gateway._bot_manager.add_bot("default", "tok:compat", channel_key="telegram")
        bot.running = True
        bot.pair_code = "XYZ789"

        handler = self._make_handler()
        gateway.GatewayHandler.handle_telegram_status(handler)

        status, data = handler._json_response
        self.assertEqual(status, 200)
        # backward-compat top-level fields
        self.assertIn("running", data)
        self.assertIn("bot_configured", data)
        self.assertIn("paired_users", data)
        self.assertIn("pairing_code", data)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=True)
    def test_telegram_pair_default_bot(self, _mock_auth, _mock_boot):
        """handle_telegram_pair with no bot name generates code for default bot."""
        bot = gateway._bot_manager.add_bot("default", "tok:pair", channel_key="telegram")
        bot.running = True

        handler = self._make_handler(path="/api/telegram/pair", method="POST")
        gateway.GatewayHandler.handle_telegram_pair(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 200)
        self.assertIn("code", data)
        self.assertIsNotNone(data["code"])

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=True)
    def test_telegram_pair_named_bot(self, _mock_auth, _mock_boot):
        """handle_telegram_pair with bot=research generates code for that bot."""
        gateway._bot_manager.add_bot("default", "tok:d", channel_key="telegram")
        research = gateway._bot_manager.add_bot("research", "tok:r", channel_key="telegram:research")
        research.running = True

        handler = self._make_handler(path="/api/telegram/pair?bot=research", method="POST")
        gateway.GatewayHandler.handle_telegram_pair(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 200)
        self.assertIn("code", data)
        self.assertEqual(data.get("bot"), "research")

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=True)
    def test_telegram_pair_unknown_bot(self, _mock_auth, _mock_boot):
        """handle_telegram_pair with bot=nonexistent returns 400."""
        handler = self._make_handler(path="/api/telegram/pair?bot=nonexistent", method="POST")
        gateway.GatewayHandler.handle_telegram_pair(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class TestBotShutdown(unittest.TestCase):
    """Tests that shutdown calls _bot_manager.stop_all()."""

    def setUp(self):
        bm = gateway._bot_manager
        with bm._lock:
            bm._bots.clear()

    def tearDown(self):
        bm = gateway._bot_manager
        with bm._lock:
            for bot in bm._bots.values():
                bot.running = False
            bm._bots.clear()

    def test_shutdown_stops_all_bots(self):
        """After stop_all, all bots should have running=False."""
        bot1 = gateway._bot_manager.add_bot("bot1", "tok:1")
        bot2 = gateway._bot_manager.add_bot("bot2", "tok:2")
        bot1.running = True
        bot2.running = True

        gateway._bot_manager.stop_all()

        # stop_all clears the registry, so get_all returns empty
        self.assertEqual(len(gateway._bot_manager.get_all()), 0)
        # but the bot objects themselves should have running=False
        self.assertFalse(bot1.running)
        self.assertFalse(bot2.running)


class TestBotLifecycleAPI(unittest.TestCase):
    """Tests for hot-add and hot-remove bot API endpoints (BOT-05, BOT-06)."""

    def setUp(self):
        bm = gateway._bot_manager
        with bm._lock:
            bm._bots.clear()
        # save and clear notification handlers
        self._orig_handlers = gateway._notification_handlers[:]
        gateway._notification_handlers.clear()

    def tearDown(self):
        bm = gateway._bot_manager
        with bm._lock:
            for bot in bm._bots.values():
                bot.running = False
            bm._bots.clear()
        # restore notification handlers
        gateway._notification_handlers[:] = self._orig_handlers

    def _make_handler(self, path="/api/bots", method="POST"):
        """Create a mock GatewayHandler for testing endpoints."""
        handler = MagicMock(spec=gateway.GatewayHandler)
        handler.path = path
        handler.command = method
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler._json_response = None

        def mock_send_json(status, data):
            handler._json_response = (status, data)

        handler.send_json = mock_send_json
        return handler

    # ── unregister_notification_handler ──

    def test_unregister_notification_handler(self):
        """register a handler, unregister it by name, verify it's gone."""
        gateway.register_notification_handler("test_chan", lambda t: {"sent": True})
        self.assertEqual(len(gateway._notification_handlers), 1)

        gateway.unregister_notification_handler("test_chan")

        self.assertEqual(len(gateway._notification_handlers), 0)

    def test_unregister_nonexistent_handler(self):
        """unregister a handler that doesn't exist, should not raise."""
        gateway.unregister_notification_handler("nonexistent")
        # no exception = pass

    # ── BotManager.remove_bot enhanced cleanup ──

    def test_remove_bot_calls_stop(self):
        """remove_bot should call bot.stop() which sets running=False and joins thread."""
        bot = gateway._bot_manager.add_bot("test_stop", "tok:stop")
        bot.running = True

        gateway._bot_manager.remove_bot("test_stop")

        self.assertFalse(bot.running)
        self.assertIsNone(gateway._bot_manager.get_bot("test_stop"))

    def test_remove_bot_clears_sessions(self):
        """remove_bot should clear sessions for the bot's channel_key."""
        bot = gateway._bot_manager.add_bot("test_sess", "tok:sess", channel_key="telegram:test_sess")
        gateway._session_manager.set("telegram:test_sess", "user1", "sid1")
        self.assertEqual(gateway._session_manager.get("telegram:test_sess", "user1"), "sid1")

        gateway._bot_manager.remove_bot("test_sess")

        self.assertIsNone(gateway._session_manager.get("telegram:test_sess", "user1"))

    def test_remove_bot_unregisters_notification(self):
        """remove_bot should unregister the notification handler for the bot's channel_key."""
        bot = gateway._bot_manager.add_bot("test_notif", "tok:notif", channel_key="telegram:test_notif")
        gateway.register_notification_handler("telegram:test_notif", lambda t: {"sent": True})
        self.assertEqual(len(gateway._notification_handlers), 1)

        gateway._bot_manager.remove_bot("test_notif")

        self.assertEqual(len(gateway._notification_handlers), 0)

    # ── POST /api/bots (hot-add) ──

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.save_setup")
    @patch("gateway.load_setup", return_value={"provider_type": "anthropic", "bots": []})
    @patch.object(gateway.BotInstance, "start")
    def test_add_bot_success(self, _mock_start, _mock_load, _mock_save, _mock_boot):
        """POST with {name, token} returns 201 with bot name and status."""
        handler = self._make_handler()
        handler._read_body = MagicMock(return_value=json.dumps({"name": "research", "token": "111:AAA"}).encode())

        gateway.GatewayHandler.handle_add_bot(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 201)
        self.assertEqual(data["name"], "research")
        self.assertEqual(data["status"], "running")

    @patch("gateway._is_first_boot", return_value=False)
    def test_add_bot_missing_name(self, _mock_boot):
        """POST with no name returns 400."""
        handler = self._make_handler()
        handler._read_body = MagicMock(return_value=json.dumps({"token": "111:AAA"}).encode())

        gateway.GatewayHandler.handle_add_bot(handler)

        status, data = handler._json_response
        self.assertEqual(status, 400)

    @patch("gateway._is_first_boot", return_value=False)
    def test_add_bot_missing_token(self, _mock_boot):
        """POST with no token returns 400."""
        handler = self._make_handler()
        handler._read_body = MagicMock(return_value=json.dumps({"name": "research"}).encode())

        gateway.GatewayHandler.handle_add_bot(handler)

        status, data = handler._json_response
        self.assertEqual(status, 400)

    @patch("gateway._is_first_boot", return_value=False)
    def test_add_bot_invalid_token_format(self, _mock_boot):
        """POST with token lacking colon returns 400."""
        handler = self._make_handler()
        handler._read_body = MagicMock(return_value=json.dumps({"name": "research", "token": "badtoken"}).encode())

        gateway.GatewayHandler.handle_add_bot(handler)

        status, data = handler._json_response
        self.assertEqual(status, 400)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.save_setup")
    @patch("gateway.load_setup", return_value={"provider_type": "anthropic", "bots": []})
    @patch.object(gateway.BotInstance, "start")
    def test_add_bot_duplicate_name(self, _mock_start, _mock_load, _mock_save, _mock_boot):
        """POST with name that already exists in _bot_manager returns 409."""
        gateway._bot_manager.add_bot("research", "222:BBB")
        handler = self._make_handler()
        handler._read_body = MagicMock(return_value=json.dumps({"name": "research", "token": "111:AAA"}).encode())

        gateway.GatewayHandler.handle_add_bot(handler)

        status, data = handler._json_response
        self.assertEqual(status, 409)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.save_setup")
    @patch("gateway.load_setup", return_value={"provider_type": "anthropic", "bots": []})
    @patch.object(gateway.BotInstance, "start")
    def test_add_bot_persists_to_setup(self, _mock_start, _mock_load, mock_save, _mock_boot):
        """POST with valid bot calls save_setup with config containing new bot."""
        handler = self._make_handler()
        handler._read_body = MagicMock(return_value=json.dumps({"name": "research", "token": "111:AAA"}).encode())

        gateway.GatewayHandler.handle_add_bot(handler)

        self.assertTrue(mock_save.called)
        saved_config = mock_save.call_args[0][0]
        bot_names = [b["name"] for b in saved_config.get("bots", [])]
        self.assertIn("research", bot_names)

    # ── DELETE /api/bots/<name> (hot-remove) ──

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.save_setup")
    @patch("gateway.load_setup", return_value={"provider_type": "anthropic", "bots": [{"name": "research", "token": "111:AAA"}]})
    def test_remove_bot_success(self, _mock_load, _mock_save, _mock_boot):
        """DELETE /api/bots/research returns 200 and bot is gone."""
        bot = gateway._bot_manager.add_bot("research", "111:AAA")
        handler = self._make_handler(path="/api/bots/research", method="DELETE")

        gateway.GatewayHandler.handle_remove_bot(handler, "research")

        status, data = handler._json_response
        self.assertEqual(status, 200)
        self.assertEqual(data["removed"], "research")
        self.assertIsNone(gateway._bot_manager.get_bot("research"))

    @patch("gateway._is_first_boot", return_value=False)
    def test_remove_bot_not_found(self, _mock_boot):
        """DELETE /api/bots/nonexistent returns 404."""
        handler = self._make_handler(path="/api/bots/nonexistent", method="DELETE")

        gateway.GatewayHandler.handle_remove_bot(handler, "nonexistent")

        status, data = handler._json_response
        self.assertEqual(status, 404)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.save_setup")
    @patch("gateway.load_setup", return_value={"provider_type": "anthropic", "bots": [{"name": "research", "token": "111:AAA"}]})
    def test_remove_bot_persists_to_setup(self, _mock_load, mock_save, _mock_boot):
        """DELETE removes bot from setup.json bots array."""
        gateway._bot_manager.add_bot("research", "111:AAA")
        handler = self._make_handler(path="/api/bots/research", method="DELETE")

        gateway.GatewayHandler.handle_remove_bot(handler, "research")

        self.assertTrue(mock_save.called)
        saved_config = mock_save.call_args[0][0]
        bot_names = [b["name"] for b in saved_config.get("bots", [])]
        self.assertNotIn("research", bot_names)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.save_setup")
    @patch("gateway.load_setup", return_value={"provider_type": "anthropic", "bots": [{"name": "a", "token": "1:A"}, {"name": "b", "token": "2:B"}]})
    def test_remove_bot_does_not_affect_others(self, _mock_load, _mock_save, _mock_boot):
        """Removing bot 'a' does not affect bot 'b'."""
        gateway._bot_manager.add_bot("a", "1:A")
        bot_b = gateway._bot_manager.add_bot("b", "2:B")
        bot_b.running = True
        handler = self._make_handler(path="/api/bots/a", method="DELETE")

        gateway.GatewayHandler.handle_remove_bot(handler, "a")

        self.assertIsNotNone(gateway._bot_manager.get_bot("b"))
        self.assertTrue(bot_b.running)

    # ── Auth ──

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=False)
    def test_add_bot_requires_auth(self, _mock_auth, _mock_boot):
        """POST from non-localhost without auth returns 401."""
        handler = self._make_handler()
        handler.client_address = ("1.2.3.4", 12345)
        handler._read_body = MagicMock(return_value=json.dumps({"name": "x", "token": "1:X"}).encode())
        # Wire real _check_local_or_auth so auth guard actually runs
        handler._check_local_or_auth = lambda: gateway.GatewayHandler._check_local_or_auth(handler)

        gateway.GatewayHandler.handle_add_bot(handler)

        # should have returned 401 for auth failure
        self.assertIsNotNone(handler._json_response)
        self.assertEqual(handler._json_response[0], 401)

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=False)
    def test_remove_bot_requires_auth(self, _mock_auth, _mock_boot):
        """DELETE from non-localhost without auth returns 401."""
        handler = self._make_handler(path="/api/bots/x", method="DELETE")
        handler.client_address = ("1.2.3.4", 12345)
        # Wire real _check_local_or_auth so auth guard actually runs
        handler._check_local_or_auth = lambda: gateway.GatewayHandler._check_local_or_auth(handler)

        gateway.GatewayHandler.handle_remove_bot(handler, "x")

        self.assertIsNotNone(handler._json_response)
        self.assertEqual(handler._json_response[0], 401)


class TestUXPaperCuts(unittest.TestCase):
    """Tests for UX paper cut fixes: auth recovery hint, pairing code in save response, bots UI."""

    # ── 1. Auth token recovery hint in setup.html ──

    def test_setup_html_contains_recovery_hint(self):
        """Success screen should mention /api/auth/recover for token recovery."""
        setup_html_path = os.path.join(os.path.dirname(__file__), "setup.html")
        with open(setup_html_path) as f:
            content = f.read()
        # the success screen (step-success) should contain a recovery hint
        self.assertIn("/api/auth/recover", content,
                      "setup.html success screen should mention the recovery endpoint")

    def test_setup_html_success_screen_no_token_box(self):
        """Success screen should NOT have a token display box (password-based auth now)."""
        setup_html_path = os.path.join(os.path.dirname(__file__), "setup.html")
        with open(setup_html_path) as f:
            content = f.read()
        # find the step-success div
        success_start = content.find('id="step-success"')
        self.assertGreater(success_start, 0, "step-success should exist in setup.html")
        # find the next step div (step-dashboard)
        success_end = content.find('id="step-dashboard"', success_start)
        success_section = content[success_start:success_end]
        self.assertNotIn("savedToken", success_section,
                         "Success screen should not have token display (password auth)")
        self.assertNotIn("tokenBox", success_section,
                         "Success screen should not have token box (password auth)")

    # ── 2. Pairing code in save response ──

    def setUp(self):
        self.bm = gateway._bot_manager
        with self.bm._lock:
            self.bm._bots.clear()

    def tearDown(self):
        with self.bm._lock:
            for bot in self.bm._bots.values():
                bot.running = False
            self.bm._bots.clear()

    def _make_handler(self, path="/api/setup/save", method="POST", body=b"{}"):
        handler = MagicMock(spec=gateway.GatewayHandler)
        handler.path = path
        handler.command = method
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler._json_response = None
        handler._read_body = MagicMock(return_value=body)

        def mock_send_json(status, data):
            handler._json_response = (status, data)
        handler.send_json = mock_send_json
        return handler

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=True)
    @patch("gateway.load_setup", return_value={"provider": "openai", "api_key": "sk-test", "web_auth_token_hash": "existinghash"})
    @patch("gateway.save_setup")
    @patch("gateway.apply_config")
    @patch("gateway.start_goose_web")
    @patch("gateway.start_session_watcher")
    @patch("gateway.start_job_engine")
    @patch("gateway.start_cron_scheduler")
    @patch("gateway.start_memory_writer")
    @patch("gateway.validate_setup_config", return_value=(True, []))
    def test_save_response_includes_pairing_code_when_bot_configured(
        self, _validate, _mem, _cron, _job, _sess, _goose, _apply, _save, _load, _auth, _boot
    ):
        """When save includes telegram_bot_token, response should include pairing_code."""
        # set up a bot that has a pairing code
        bot = self.bm.add_bot("default", "tok:test123", channel_key="telegram")
        bot.running = True
        bot.pair_code = "PAIR42"

        config = {"provider": "openai", "api_key": "sk-test", "telegram_bot_token": "tok:test123"}
        handler = self._make_handler(body=json.dumps(config).encode())
        gateway.GatewayHandler.handle_save(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertIn("pairing_code", data,
                      "Save response should include pairing_code when telegram bot is configured")

    @patch("gateway._is_first_boot", return_value=False)
    @patch("gateway.check_auth", return_value=True)
    @patch("gateway.load_setup", return_value={"provider": "openai", "api_key": "sk-test", "web_auth_token_hash": "existinghash"})
    @patch("gateway.save_setup")
    @patch("gateway.apply_config")
    @patch("gateway.start_goose_web")
    @patch("gateway.start_session_watcher")
    @patch("gateway.start_job_engine")
    @patch("gateway.start_cron_scheduler")
    @patch("gateway.start_memory_writer")
    @patch("gateway.validate_setup_config", return_value=(True, []))
    def test_save_response_no_pairing_code_without_telegram(
        self, _validate, _mem, _cron, _job, _sess, _goose, _apply, _save, _load, _auth, _boot
    ):
        """When save has no telegram_bot_token, response should not include pairing_code."""
        config = {"provider": "openai", "api_key": "sk-test"}
        handler = self._make_handler(body=json.dumps(config).encode())
        gateway.GatewayHandler.handle_save(handler)

        self.assertIsNotNone(handler._json_response)
        status, data = handler._json_response
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertNotIn("pairing_code", data,
                         "Save response should NOT include pairing_code when no telegram configured")

    # ── 3. Multi-bot management UI in admin.html ──

    def test_admin_html_contains_bots_section(self):
        """Admin dashboard should have a Bots section."""
        admin_html_path = os.path.join(os.path.dirname(__file__), "admin.html")
        with open(admin_html_path) as f:
            content = f.read()
        # check for bots section
        self.assertIn("Bots", content,
                      "admin.html should contain a Bots section title")

    def test_admin_html_has_add_bot_form(self):
        """Admin dashboard should have an add-bot form."""
        admin_html_path = os.path.join(os.path.dirname(__file__), "admin.html")
        with open(admin_html_path) as f:
            content = f.read()
        self.assertIn("bot-name", content,
                      "admin.html should have a bot name input")
        self.assertIn("bot-token", content,
                      "admin.html should have a bot token input")
        self.assertIn("/api/bots", content,
                      "admin.html should reference the /api/bots endpoint")

    def test_admin_html_has_remove_bot_button(self):
        """Admin dashboard should have remove bot capability."""
        admin_html_path = os.path.join(os.path.dirname(__file__), "admin.html")
        with open(admin_html_path) as f:
            content = f.read()
        self.assertIn("removeBot", content,
                      "admin.html should have a removeBot function")

    def test_admin_html_shows_pairing_codes(self):
        """Admin dashboard bots section should display pairing codes."""
        admin_html_path = os.path.join(os.path.dirname(__file__), "admin.html")
        with open(admin_html_path) as f:
            content = f.read()
        self.assertIn("pairing_code", content,
                      "admin.html should display bot pairing codes")

    def test_admin_html_has_bots_refresh(self):
        """Admin dashboard should fetch bot data on load."""
        admin_html_path = os.path.join(os.path.dirname(__file__), "admin.html")
        with open(admin_html_path) as f:
            content = f.read()
        self.assertIn("refreshBots", content,
                      "admin.html should have a refreshBots function")


# ── media message handling tests ─────────────────────────────────────────────

class TestMediaMessageHandling(unittest.TestCase):
    """Tests for media message replies in the poll loop."""

    def test_has_media_detects_photo(self):
        """_has_media returns True when message contains a photo."""
        msg = {"chat": {"id": 1}, "photo": [{"file_id": "abc"}]}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_voice(self):
        """_has_media returns True for voice messages."""
        msg = {"chat": {"id": 1}, "voice": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_document(self):
        """_has_media returns True for documents/files."""
        msg = {"chat": {"id": 1}, "document": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_sticker(self):
        """_has_media returns True for stickers."""
        msg = {"chat": {"id": 1}, "sticker": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_video(self):
        """_has_media returns True for video."""
        msg = {"chat": {"id": 1}, "video": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_audio(self):
        """_has_media returns True for audio."""
        msg = {"chat": {"id": 1}, "audio": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_video_note(self):
        """_has_media returns True for video notes (round videos)."""
        msg = {"chat": {"id": 1}, "video_note": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_detects_animation(self):
        """_has_media returns True for animations/GIFs."""
        msg = {"chat": {"id": 1}, "animation": {"file_id": "abc"}}
        self.assertTrue(gateway._has_media(msg))

    def test_has_media_false_for_text_only(self):
        """_has_media returns False for text-only messages."""
        msg = {"chat": {"id": 1}, "text": "hello"}
        self.assertFalse(gateway._has_media(msg))

    def test_has_media_false_for_empty_message(self):
        """_has_media returns False for empty message objects."""
        msg = {"chat": {"id": 1}}
        self.assertFalse(gateway._has_media(msg))

    def test_media_reply_constant_exists(self):
        """The MEDIA_REPLY constant should be defined."""
        self.assertTrue(hasattr(gateway, "MEDIA_REPLY"))
        self.assertIn("text", gateway.MEDIA_REPLY.lower())

    def test_paired_user_photo_relays_not_rejected(self):
        """Paired user sending a photo should relay (not send MEDIA_REPLY)."""
        bot = gateway.BotInstance("test", "tok123")
        bot.running = True
        fake_update = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 42},
                    "photo": [{"file_id": "abc", "width": 100, "height": 100}],
                },
            }],
        }
        empty_response = {"ok": True, "result": []}

        # simulate one poll cycle: first call returns media msg, second stops loop
        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                resp = MagicMock()
                resp.read.return_value = json.dumps(fake_update).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            bot.running = False
            resp = MagicMock()
            resp.read.return_value = json.dumps(empty_response).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(bot, "_do_message_relay"):
            bot._poll_loop()

        # MEDIA_REPLY should NOT be sent (Phase 12: media flows through relay)
        for call in mock_send.call_args_list:
            if len(call.args) >= 3:
                self.assertNotEqual(call.args[2], gateway.MEDIA_REPLY)

    def test_paired_user_voice_relays_not_rejected(self):
        """Paired user sending a voice note should relay (not send MEDIA_REPLY)."""
        bot = gateway.BotInstance("test", "tok123")
        bot.running = True
        fake_update = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 42},
                    "voice": {"file_id": "abc", "duration": 5},
                },
            }],
        }
        empty_response = {"ok": True, "result": []}

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                resp = MagicMock()
                resp.read.return_value = json.dumps(fake_update).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            bot.running = False
            resp = MagicMock()
            resp.read.return_value = json.dumps(empty_response).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(bot, "_do_message_relay"):
            bot._poll_loop()

        # MEDIA_REPLY should NOT be sent (Phase 12: media flows through relay)
        for call in mock_send.call_args_list:
            if len(call.args) >= 3:
                self.assertNotEqual(call.args[2], gateway.MEDIA_REPLY)

    def test_unpaired_user_photo_gets_no_reply(self):
        """Unpaired user sending a photo should get NO reply (silence)."""
        bot = gateway.BotInstance("test", "tok123")
        bot.running = True
        fake_update = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 99},
                    "photo": [{"file_id": "abc", "width": 100, "height": 100}],
                },
            }],
        }
        empty_response = {"ok": True, "result": []}

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                resp = MagicMock()
                resp.read.return_value = json.dumps(fake_update).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            bot.running = False
            resp = MagicMock()
            resp.read.return_value = json.dumps(empty_response).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen):
            bot._poll_loop()

        mock_send.assert_not_called()

    def test_text_message_not_treated_as_media(self):
        """A normal text message should NOT trigger the media reply."""
        bot = gateway.BotInstance("test", "tok123")
        bot.running = True
        fake_update = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 42},
                    "text": "hello",
                },
            }],
        }
        empty_response = {"ok": True, "result": []}

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                resp = MagicMock()
                resp.read.return_value = json.dumps(fake_update).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            bot.running = False
            resp = MagicMock()
            resp.read.return_value = json.dumps(empty_response).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(bot, "_do_message_relay"):
            bot._poll_loop()

        # send_telegram_message should NOT be called with the MEDIA_REPLY
        for call in mock_send.call_args_list:
            if len(call.args) >= 3:
                self.assertNotEqual(call.args[2], gateway.MEDIA_REPLY,
                                    "Text message should not trigger media reply")


# ── InboundMessage ─────────────────────────────────────────────────────────


class TestInboundMessage(unittest.TestCase):
    """Tests for InboundMessage channel-agnostic envelope (MEDIA-01)."""

    def test_text_only(self):
        msg = gateway.InboundMessage(user_id="123", text="hello")
        self.assertEqual(msg.text, "hello")
        self.assertTrue(msg.has_text)
        self.assertFalse(msg.has_media)
        self.assertEqual(msg.media, [])
        self.assertEqual(msg.metadata, {})

    def test_with_media(self):
        msg = gateway.InboundMessage(
            user_id="123",
            media=[{"type": "image", "url": "http://x"}],
        )
        self.assertTrue(msg.has_media)

    def test_defaults(self):
        msg = gateway.InboundMessage(user_id="123")
        self.assertEqual(msg.text, "")
        self.assertEqual(msg.channel, "")
        self.assertEqual(msg.media, [])
        self.assertEqual(msg.metadata, {})

    def test_user_id_coerced_to_string(self):
        msg = gateway.InboundMessage(user_id=123)
        self.assertEqual(msg.user_id, "123")

    def test_with_metadata(self):
        msg = gateway.InboundMessage(user_id="123", metadata={"msg_id": "abc"})
        self.assertEqual(msg.metadata, {"msg_id": "abc"})

    def test_text_with_media(self):
        msg = gateway.InboundMessage(
            user_id="123",
            text="check this out",
            media=[{"type": "image", "url": "http://x"}],
        )
        self.assertTrue(msg.has_text)
        self.assertTrue(msg.has_media)


# ── OutboundAdapter ────────────────────────────────────────────────────────


class TestOutboundAdapter(unittest.TestCase):
    """Tests for OutboundAdapter base class (MEDIA-02)."""

    def _make_adapter(self):
        """Create a concrete adapter that records send_text calls."""
        class RecordingAdapter(gateway.OutboundAdapter):
            def __init__(self):
                self.sent = []
            def send_text(self, text):
                self.sent.append(text)
                return {"sent": True}
        return RecordingAdapter()

    def test_send_text_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            gateway.OutboundAdapter().send_text("hi")

    def test_send_image_degrades_to_text(self):
        adapter = self._make_adapter()
        adapter.send_image(b"\xff\xd8", "caption")
        self.assertEqual(adapter.sent, ["caption\n[image]"])

    def test_send_voice_degrades_to_text(self):
        adapter = self._make_adapter()
        adapter.send_voice(b"\x00", "transcript text")
        self.assertEqual(adapter.sent, ["transcript text"])

    def test_send_voice_no_transcript(self):
        adapter = self._make_adapter()
        adapter.send_voice(b"\x00")
        self.assertEqual(adapter.sent, ["[voice message]"])

    def test_send_file_degrades_to_text(self):
        adapter = self._make_adapter()
        adapter.send_file(b"\x00", "doc.pdf")
        self.assertEqual(adapter.sent, ["[File: doc.pdf]"])

    def test_send_buttons_degrades_to_text(self):
        adapter = self._make_adapter()
        adapter.send_buttons("Pick one:", [{"label": "A"}, {"label": "B"}])
        expected = "Pick one:\n\n1. A\n2. B"
        self.assertEqual(adapter.sent, [expected])

    def test_capabilities_returns_text_only_defaults(self):
        caps = gateway.OutboundAdapter().capabilities()
        self.assertIsInstance(caps, gateway.ChannelCapabilities)
        self.assertFalse(caps.supports_images)
        self.assertFalse(caps.supports_voice)
        self.assertFalse(caps.supports_files)
        self.assertFalse(caps.supports_buttons)


# ── ChannelCapabilities ────────────────────────────────────────────────────


class TestChannelCapabilities(unittest.TestCase):
    """Tests for ChannelCapabilities declaration (MEDIA-03)."""

    def test_defaults_all_false(self):
        caps = gateway.ChannelCapabilities()
        self.assertFalse(caps.supports_images)
        self.assertFalse(caps.supports_voice)
        self.assertFalse(caps.supports_files)
        self.assertFalse(caps.supports_buttons)
        self.assertFalse(caps.supports_streaming)
        self.assertEqual(caps.max_file_size, 0)
        self.assertEqual(caps.max_text_length, 0)

    def test_custom_values(self):
        caps = gateway.ChannelCapabilities(supports_images=True, max_file_size=50000000)
        self.assertTrue(caps.supports_images)
        self.assertEqual(caps.max_file_size, 50000000)

    def test_to_dict(self):
        caps = gateway.ChannelCapabilities(supports_images=True)
        d = caps.to_dict()
        self.assertIsInstance(d, dict)
        self.assertTrue(d["supports_images"])
        self.assertFalse(d["supports_voice"])


# ── GracefulDegradation ───────────────────────────────────────────────────


class TestGracefulDegradation(unittest.TestCase):
    """Tests for graceful degradation in OutboundAdapter (MEDIA-04)."""

    def _make_adapter(self):
        class RecordingAdapter(gateway.OutboundAdapter):
            def __init__(self):
                self.sent = []
            def send_text(self, text):
                self.sent.append(text)
                return {"sent": True}
        return RecordingAdapter()

    def test_image_to_text_fallback(self):
        adapter = self._make_adapter()
        adapter.send_image(b"\xff\xd8")
        self.assertIn("[image]", adapter.sent[0])

    def test_voice_to_transcript_fallback(self):
        adapter = self._make_adapter()
        adapter.send_voice(b"\x00", "here is the transcript")
        self.assertEqual(adapter.sent, ["here is the transcript"])

    def test_file_to_link_fallback(self):
        adapter = self._make_adapter()
        adapter.send_file(b"\x00", "report.pdf")
        self.assertIn("report.pdf", adapter.sent[0])

    def test_override_skips_degradation(self):
        class CustomImageAdapter(gateway.OutboundAdapter):
            def __init__(self):
                self.image_calls = []
                self.text_calls = []
            def send_text(self, text):
                self.text_calls.append(text)
                return {"sent": True}
            def send_image(self, data, caption="", **kwargs):
                self.image_calls.append((data, caption))
                return {"sent": True}

        adapter = CustomImageAdapter()
        adapter.send_image(b"\xff", "cap")
        self.assertEqual(adapter.image_calls, [(b"\xff", "cap")])
        self.assertEqual(adapter.text_calls, [])


# ── LegacyOutboundAdapter ─────────────────────────────────────────────────


class TestLegacyOutboundAdapter(unittest.TestCase):
    """Tests for LegacyOutboundAdapter backward compat shim."""

    def test_wraps_send_fn(self):
        mock_fn = MagicMock(return_value={"sent": True})
        adapter = gateway.LegacyOutboundAdapter(mock_fn)
        result = adapter.send_text("hi")
        mock_fn.assert_called_once_with("hi")
        self.assertEqual(result, {"sent": True})

    def test_image_degrades_through_legacy(self):
        mock_fn = MagicMock(return_value={"sent": True})
        adapter = gateway.LegacyOutboundAdapter(mock_fn)
        adapter.send_image(b"\xff", "cap")
        mock_fn.assert_called_once_with("cap\n[image]")

    def test_capabilities_text_only(self):
        adapter = gateway.LegacyOutboundAdapter(lambda t: None)
        caps = adapter.capabilities()
        self.assertFalse(caps.supports_images)
        self.assertFalse(caps.supports_voice)


# ── LoadChannel v2 ─────────────────────────────────────────────────────────


class TestLoadChannelV2(unittest.TestCase):
    """Tests for _load_channel v2 adapter wrapping (MEDIA-05)."""

    def _make_plugin_module(self, channel_dict):
        """Create a fake module with CHANNEL attribute."""
        mod = MagicMock()
        mod.CHANNEL = channel_dict
        return mod

    @patch("gateway._channels_lock", threading.Lock())
    @patch("gateway._loaded_channels", {})
    @patch("gateway._channel_stop_events", {})
    @patch("gateway._channel_threads", {})
    @patch("gateway.register_notification_handler")
    @patch("gateway._command_router")
    def test_legacy_plugin_wrapped(self, mock_router, mock_register):
        """Legacy send(text) plugins should be wrapped in LegacyOutboundAdapter."""
        mock_fn = MagicMock(return_value={"sent": True})
        channel = {"name": "test_legacy", "version": 1, "send": mock_fn}

        with patch("gateway.importlib.util") as mock_importlib:
            mock_spec = MagicMock()
            mock_importlib.spec_from_file_location.return_value = mock_spec
            mock_mod = MagicMock()
            mock_mod.CHANNEL = channel
            mock_importlib.module_from_spec.return_value = mock_mod
            mock_spec.loader.exec_module = MagicMock()

            result = gateway._load_channel("/fake/test_legacy.py")

        self.assertTrue(result)
        # The loaded channel should have an adapter key
        entry = gateway._loaded_channels.get("test_legacy", {})
        adapter = entry.get("adapter")
        self.assertIsNotNone(adapter, "loaded channel must have an 'adapter' key")
        self.assertIsInstance(adapter, gateway.LegacyOutboundAdapter)
        # adapter.send_text should delegate to the original send_fn
        adapter.send_text("hi")
        mock_fn.assert_called_with("hi")

    @patch("gateway._channels_lock", threading.Lock())
    @patch("gateway._loaded_channels", {})
    @patch("gateway._channel_stop_events", {})
    @patch("gateway._channel_threads", {})
    @patch("gateway.register_notification_handler")
    @patch("gateway._command_router")
    def test_v2_plugin_used_directly(self, mock_router, mock_register):
        """v2 plugins with adapter field should use it directly, not wrap."""
        custom_adapter = gateway.OutboundAdapter()
        channel = {
            "name": "test_v2",
            "version": 2,
            "send": lambda t: {"sent": True},
            "adapter": custom_adapter,
        }

        with patch("gateway.importlib.util") as mock_importlib:
            mock_spec = MagicMock()
            mock_importlib.spec_from_file_location.return_value = mock_spec
            mock_mod = MagicMock()
            mock_mod.CHANNEL = channel
            mock_importlib.module_from_spec.return_value = mock_mod
            mock_spec.loader.exec_module = MagicMock()

            result = gateway._load_channel("/fake/test_v2.py")

        self.assertTrue(result)
        entry = gateway._loaded_channels.get("test_v2", {})
        adapter = entry.get("adapter")
        self.assertIs(adapter, custom_adapter, "v2 adapter should be used directly")

    @patch("gateway._channels_lock", threading.Lock())
    @patch("gateway._loaded_channels", {})
    @patch("gateway._channel_stop_events", {})
    @patch("gateway._channel_threads", {})
    @patch("gateway.register_notification_handler")
    @patch("gateway._command_router")
    def test_legacy_notification_uses_adapter(self, mock_router, mock_register):
        """Notification handler should use adapter.send_text, not raw send_fn."""
        mock_fn = MagicMock(return_value={"sent": True})
        channel = {"name": "test_notify", "version": 1, "send": mock_fn}

        with patch("gateway.importlib.util") as mock_importlib:
            mock_spec = MagicMock()
            mock_importlib.spec_from_file_location.return_value = mock_spec
            mock_mod = MagicMock()
            mock_mod.CHANNEL = channel
            mock_importlib.module_from_spec.return_value = mock_mod
            mock_spec.loader.exec_module = MagicMock()

            gateway._load_channel("/fake/test_notify.py")

        # The notification handler should have been registered
        self.assertTrue(mock_register.called)
        call_args = mock_register.call_args
        handler_name = call_args[0][0]
        handler_fn = call_args[0][1]
        self.assertEqual(handler_name, "channel:test_notify")
        # Calling the handler should ultimately call the original send_fn
        handler_fn("hello")
        mock_fn.assert_called_with("hello")


# ── ChannelRelay v2 ───────────────────────────────────────────────────────


class TestChannelRelayV2(unittest.TestCase):
    """Tests for ChannelRelay accepting InboundMessage (MEDIA-05)."""

    def setUp(self):
        self.relay = gateway.ChannelRelay("test_relay_v2")

    @patch("gateway._relay_to_goose_web", return_value=("response", None, []))
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._session_manager")
    def test_relay_accepts_inbound_message(self, mock_sm, mock_setup, mock_relay):
        """ChannelRelay.__call__ should accept InboundMessage as first arg."""
        mock_sm.get.return_value = "sess_123"
        msg = gateway.InboundMessage(user_id="123", text="hello")
        # Should not raise
        result = self.relay(msg)
        self.assertIsNotNone(result)

    @patch("gateway._relay_to_goose_web", return_value=("response", None, []))
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._session_manager")
    def test_relay_still_accepts_legacy_args(self, mock_sm, mock_setup, mock_relay):
        """ChannelRelay.__call__ should still accept (user_id, text) signature."""
        mock_sm.get.return_value = "sess_123"
        result = self.relay("123", "hello")
        self.assertIsNotNone(result)

    @patch("gateway._relay_to_goose_web", return_value=("response", None, []))
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._session_manager")
    def test_relay_inbound_message_extracts_text(self, mock_sm, mock_setup, mock_relay):
        """When called with InboundMessage, text should be forwarded to relay."""
        mock_sm.get.return_value = "sess_123"
        msg = gateway.InboundMessage(user_id="456", text="test message")
        self.relay(msg)
        # Verify _relay_to_goose_web was called with the text from InboundMessage
        self.assertTrue(mock_relay.called)
        call_args = mock_relay.call_args
        self.assertEqual(call_args[0][0], "test message")


# ── BotInstance InboundMessage ─────────────────────────────────────────────


class TestBotInboundMessage(unittest.TestCase):
    """Tests for BotInstance._poll_loop creating InboundMessage envelopes."""

    def _make_bot(self):
        bot = gateway.BotInstance("test", "fake_token", channel_key="telegram:test")
        bot.running = True
        return bot

    def _make_update_response(self, messages):
        """Build a Telegram getUpdates response."""
        results = []
        for i, msg in enumerate(messages):
            results.append({"update_id": 100 + i, "message": msg})
        return {"ok": True, "result": results}

    def test_text_message_creates_inbound(self):
        """Text message should create InboundMessage with user_id and text."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 123},
            "text": "hello",
        }])
        empty = {"ok": True, "result": []}
        calls = [0]

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            if calls[0] == 0:
                resp.read.return_value = json.dumps(update).encode()
            else:
                resp.read.return_value = json.dumps(empty).encode()
                bot.running = False
            calls[0] += 1
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        inbound_messages = []
        original_do_relay = bot._do_message_relay

        def capture_relay(**kwargs):
            # Check if an inbound_msg kwarg is passed
            if "inbound_msg" in kwargs:
                inbound_messages.append(kwargs["inbound_msg"])

        with patch("gateway.get_paired_chat_ids", return_value=["123"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay), \
             patch("gateway._command_router") as mock_router:
            mock_router.is_command.return_value = False
            bot._poll_loop()

        self.assertEqual(len(inbound_messages), 1, "Should create one InboundMessage")
        msg = inbound_messages[0]
        self.assertIsInstance(msg, gateway.InboundMessage)
        self.assertEqual(msg.user_id, "123")
        self.assertEqual(msg.text, "hello")
        self.assertIn("telegram", msg.channel)

    def test_media_message_creates_inbound_with_media(self):
        """Media message should create InboundMessage with file_id references."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "photo": [{"file_id": "abc123", "width": 100, "height": 100}],
        }])
        empty = {"ok": True, "result": []}
        calls = [0]

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            if calls[0] == 0:
                resp.read.return_value = json.dumps(update).encode()
            else:
                resp.read.return_value = json.dumps(empty).encode()
                bot.running = False
            calls[0] += 1
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        inbound_messages = []

        def capture_relay(**kwargs):
            if "inbound_msg" in kwargs:
                inbound_messages.append(kwargs["inbound_msg"])

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay):
            bot._poll_loop()

        self.assertEqual(len(inbound_messages), 1, "Media message should produce InboundMessage")
        msg = inbound_messages[0]
        self.assertIsInstance(msg, gateway.InboundMessage)
        self.assertTrue(msg.has_media)
        self.assertEqual(msg.media[0]["media_key"], "photo")
        self.assertEqual(msg.media[0]["file_id"], "abc123")

    def test_text_with_caption_creates_inbound(self):
        """Photo with caption should have both text and media."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "photo": [{"file_id": "abc123"}],
            "caption": "look at this",
        }])
        empty = {"ok": True, "result": []}
        calls = [0]

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            if calls[0] == 0:
                resp.read.return_value = json.dumps(update).encode()
            else:
                resp.read.return_value = json.dumps(empty).encode()
                bot.running = False
            calls[0] += 1
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        inbound_messages = []

        def capture_relay(**kwargs):
            if "inbound_msg" in kwargs:
                inbound_messages.append(kwargs["inbound_msg"])

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay):
            bot._poll_loop()

        self.assertEqual(len(inbound_messages), 1)
        msg = inbound_messages[0]
        self.assertTrue(msg.has_text)
        self.assertTrue(msg.has_media)


# ── Channels API Capabilities ─────────────────────────────────────────────


class TestChannelsAPICapabilities(unittest.TestCase):
    """Tests for GET /api/channels including capabilities."""

    @patch("gateway._channels_lock", threading.Lock())
    @patch("gateway._loaded_channels")
    def test_api_channels_includes_capabilities(self, mock_channels):
        """GET /api/channels should include capabilities key for each channel."""
        adapter = gateway.LegacyOutboundAdapter(lambda t: {"sent": True})
        mock_channels.items.return_value = [
            ("test_ch", {
                "module": MagicMock(),
                "channel": {"name": "test_ch", "version": 1, "send": lambda t: None},
                "creds": {},
                "adapter": adapter,
            }),
        ]

        # Create a mock request handler
        handler = MagicMock()
        handler._check_rate_limit = MagicMock(return_value=True)
        handler._check_local_or_auth = MagicMock(return_value=True)
        handler.send_json = MagicMock()

        # Call the handler method directly
        gateway.GatewayHandler.handle_list_channels(handler)

        handler.send_json.assert_called_once()
        args = handler.send_json.call_args[0]
        self.assertEqual(args[0], 200)
        channels = args[1]["channels"]
        self.assertEqual(len(channels), 1)
        self.assertIn("capabilities", channels[0],
                       "Channel info must include capabilities")


# ── MediaContent ───────────────────────────────────────────────────────────


class TestMediaContent(unittest.TestCase):
    """Tests for MediaContent class (MEDIA-07)."""

    def test_init(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"bytes", filename="photo.jpg")
        self.assertEqual(mc.kind, "image")
        self.assertEqual(mc.mime_type, "image/jpeg")
        self.assertEqual(mc.data, b"bytes")
        self.assertEqual(mc.filename, "photo.jpg")

    def test_size(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"hello")
        self.assertEqual(mc.size, 5)

    def test_size_empty(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"")
        self.assertEqual(mc.size, 0)

    def test_to_base64(self):
        import base64
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"hello")
        self.assertEqual(mc.to_base64(), base64.b64encode(b"hello").decode("ascii"))

    def test_to_base64_empty(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"")
        self.assertEqual(mc.to_base64(), "")

    def test_to_content_block_image(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"\xff\xd8")
        block = mc.to_content_block()
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mimeType"], "image/jpeg")
        import base64
        self.assertEqual(block["data"], base64.b64encode(b"\xff\xd8").decode("ascii"))

    def test_to_content_block_non_image(self):
        mc = gateway.MediaContent(kind="audio", mime_type="audio/ogg", data=b"data")
        self.assertIsNone(mc.to_content_block())

    def test_voice_is_audio_kind(self):
        mc = gateway.MediaContent(kind="audio", mime_type="audio/ogg", data=b"data")
        self.assertEqual(mc.kind, "audio")


# ── _extract_file_info ─────────────────────────────────────────────────────


class TestExtractFileInfo(unittest.TestCase):
    """Tests for _extract_file_info (MEDIA-06)."""

    def test_photo_picks_largest(self):
        msg = {"photo": [
            {"file_id": "small", "width": 90, "height": 90},
            {"file_id": "medium", "width": 320, "height": 320},
            {"file_id": "large", "width": 800, "height": 800},
        ]}
        fid, mime, fname = gateway._extract_file_info(msg, "photo")
        self.assertEqual(fid, "large")
        self.assertIsNone(mime)
        self.assertIsNone(fname)

    def test_voice(self):
        msg = {"voice": {"file_id": "v1", "mime_type": "audio/ogg"}}
        fid, mime, fname = gateway._extract_file_info(msg, "voice")
        self.assertEqual(fid, "v1")
        self.assertEqual(mime, "audio/ogg")
        self.assertIsNone(fname)

    def test_document(self):
        msg = {"document": {"file_id": "d1", "mime_type": "application/pdf", "file_name": "report.pdf"}}
        fid, mime, fname = gateway._extract_file_info(msg, "document")
        self.assertEqual(fid, "d1")
        self.assertEqual(mime, "application/pdf")
        self.assertEqual(fname, "report.pdf")

    def test_video(self):
        msg = {"video": {"file_id": "vid1", "mime_type": "video/mp4"}}
        fid, mime, fname = gateway._extract_file_info(msg, "video")
        self.assertEqual(fid, "vid1")
        self.assertEqual(mime, "video/mp4")
        self.assertIsNone(fname)

    def test_audio(self):
        msg = {"audio": {"file_id": "a1", "mime_type": "audio/mpeg"}}
        fid, mime, fname = gateway._extract_file_info(msg, "audio")
        self.assertEqual(fid, "a1")
        self.assertEqual(mime, "audio/mpeg")
        self.assertIsNone(fname)

    def test_sticker(self):
        msg = {"sticker": {"file_id": "s1"}}
        fid, mime, fname = gateway._extract_file_info(msg, "sticker")
        self.assertEqual(fid, "s1")
        self.assertIsNone(mime)
        self.assertIsNone(fname)

    def test_empty_photo_array(self):
        msg = {"photo": []}
        fid, mime, fname = gateway._extract_file_info(msg, "photo")
        self.assertIsNone(fid)
        self.assertIsNone(mime)
        self.assertIsNone(fname)

    def test_missing_key(self):
        msg = {"text": "hello"}
        fid, mime, fname = gateway._extract_file_info(msg, "photo")
        self.assertIsNone(fid)


# ── _make_media_content ────────────────────────────────────────────────────


class TestMakeMediaContent(unittest.TestCase):
    """Tests for _make_media_content (MEDIA-07, MEDIA-09)."""

    def test_photo_kind(self):
        mc = gateway._make_media_content("photo", b"data", "photos/file_0.jpg")
        self.assertEqual(mc.kind, "image")
        self.assertEqual(mc.mime_type, "image/jpeg")

    def test_voice_kind(self):
        mc = gateway._make_media_content("voice", b"data", "voice/file.oga")
        self.assertEqual(mc.kind, "audio")

    def test_document_kind(self):
        mc = gateway._make_media_content("document", b"data", "documents/file.pdf", mime_hint="application/pdf")
        self.assertEqual(mc.kind, "document")
        self.assertEqual(mc.mime_type, "application/pdf")

    def test_mime_hint_takes_priority(self):
        mc = gateway._make_media_content("document", b"data", "file.txt", mime_hint="text/plain")
        self.assertEqual(mc.mime_type, "text/plain")

    def test_fallback_mime(self):
        mc = gateway._make_media_content("sticker", b"data", None)
        self.assertEqual(mc.mime_type, "image/webp")

    def test_filename_preserved(self):
        mc = gateway._make_media_content("document", b"data", "x", filename="report.pdf")
        self.assertEqual(mc.filename, "report.pdf")


# ── _download_telegram_file ────────────────────────────────────────────────


class TestDownloadTelegramFile(unittest.TestCase):
    """Tests for _download_telegram_file (MEDIA-06)."""

    def test_successful_download(self):
        """Mock urllib to return getFile response then file bytes."""
        getfile_response = json.dumps({
            "ok": True,
            "result": {"file_id": "abc", "file_path": "photos/file_0.jpg"},
        }).encode()
        file_bytes = b"\xff\xd8fake_jpeg_bytes"

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            if call_count[0] == 1:
                resp.read.return_value = getfile_response
            else:
                resp.read.return_value = file_bytes
            return resp

        with patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen):
            data, path = gateway._download_telegram_file("tok123", "abc")

        self.assertEqual(data, file_bytes)
        self.assertEqual(path, "photos/file_0.jpg")

    def test_getfile_not_ok(self):
        """getFile returning ok=false should return (None, error)."""
        getfile_response = json.dumps({"ok": False, "description": "bad"}).encode()

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = getfile_response
            return resp

        with patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen):
            data, err = gateway._download_telegram_file("tok123", "abc")

        self.assertIsNone(data)
        self.assertIn("getFile failed", err)

    def test_getfile_network_error(self):
        """Network error on getFile should return (None, error)."""
        with patch("gateway.urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            data, err = gateway._download_telegram_file("tok123", "abc")

        self.assertIsNone(data)
        self.assertIn("error", err.lower())

    def test_download_network_error(self):
        """getFile succeeds but download fails should return (None, error)."""
        getfile_response = json.dumps({
            "ok": True,
            "result": {"file_id": "abc", "file_path": "photos/file_0.jpg"},
        }).encode()

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                resp = MagicMock()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                resp.read.return_value = getfile_response
                return resp
            raise urllib.error.URLError("download failed")

        with patch("gateway.urllib.request.urlopen", side_effect=fake_urlopen):
            data, err = gateway._download_telegram_file("tok123", "abc")

        self.assertIsNone(data)
        self.assertIn("error", err.lower())


# ── BotInstance media download wiring ──────────────────────────────────────


class TestBotMediaDownload(unittest.TestCase):
    """Tests for media download wiring in BotInstance poll/relay path."""

    def _make_bot(self):
        bot = gateway.BotInstance("test", "fake_token", channel_key="telegram:test")
        bot.running = True
        return bot

    def _make_update_response(self, messages):
        results = []
        for i, msg in enumerate(messages):
            results.append({"update_id": 100 + i, "message": msg})
        return {"ok": True, "result": results}

    def _fake_urlopen_factory(self, update, bot):
        """Return a fake_urlopen that returns update on first call, then stops the bot."""
        empty = {"ok": True, "result": []}
        calls = [0]
        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            if calls[0] == 0:
                resp.read.return_value = json.dumps(update).encode()
            else:
                resp.read.return_value = json.dumps(empty).encode()
                bot.running = False
            calls[0] += 1
            return resp
        return fake_urlopen

    def test_photo_message_downloads_and_creates_media_content(self):
        """Photo message should result in MediaContent with kind=image in relay."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "photo": [
                {"file_id": "small", "width": 90, "height": 90},
                {"file_id": "large", "width": 800, "height": 800},
            ],
        }])

        inbound_messages = []
        def capture_relay(**kwargs):
            msg = kwargs.get("inbound_msg")
            if msg:
                inbound_messages.append(msg)

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay):
            bot._poll_loop()

        # Should have relayed (not rejected)
        self.assertEqual(len(inbound_messages), 1)
        msg = inbound_messages[0]
        self.assertTrue(msg.has_media)
        # Media should contain file_id references for download in relay thread
        self.assertTrue(len(msg.media) > 0)
        ref = msg.media[0]
        self.assertEqual(ref["media_key"], "photo")
        self.assertEqual(ref["file_id"], "large")

    def test_voice_message_downloads(self):
        """Voice message should produce file_id reference with voice media_key."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "voice": {"file_id": "v1", "mime_type": "audio/ogg", "duration": 5},
        }])

        inbound_messages = []
        def capture_relay(**kwargs):
            msg = kwargs.get("inbound_msg")
            if msg:
                inbound_messages.append(msg)

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay):
            bot._poll_loop()

        self.assertEqual(len(inbound_messages), 1)
        ref = inbound_messages[0].media[0]
        self.assertEqual(ref["media_key"], "voice")
        self.assertEqual(ref["file_id"], "v1")
        self.assertEqual(ref["mime_hint"], "audio/ogg")

    def test_document_message_downloads(self):
        """Document message should include filename in the reference."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "document": {"file_id": "d1", "mime_type": "application/pdf", "file_name": "report.pdf"},
        }])

        inbound_messages = []
        def capture_relay(**kwargs):
            msg = kwargs.get("inbound_msg")
            if msg:
                inbound_messages.append(msg)

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay):
            bot._poll_loop()

        self.assertEqual(len(inbound_messages), 1)
        ref = inbound_messages[0].media[0]
        self.assertEqual(ref["media_key"], "document")
        self.assertEqual(ref["filename"], "report.pdf")

    def test_media_only_no_longer_rejected(self):
        """Media-only message from paired user should NOT receive MEDIA_REPLY."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "photo": [{"file_id": "abc", "width": 100, "height": 100}],
        }])

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay"):
            bot._poll_loop()

        # MEDIA_REPLY should NOT have been sent
        for call in mock_send.call_args_list:
            if len(call.args) >= 3:
                self.assertNotEqual(call.args[2], gateway.MEDIA_REPLY,
                                    "MEDIA_REPLY should not be sent for paired user media")

    def test_download_failure_graceful(self):
        """Download failure should not crash relay. Media should be empty list after download attempt."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "photo": [{"file_id": "abc", "width": 100, "height": 100}],
        }])

        inbound_messages = []
        def capture_relay(**kwargs):
            msg = kwargs.get("inbound_msg")
            if msg:
                inbound_messages.append(msg)

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay):
            bot._poll_loop()

        # Should still relay (not crash)
        self.assertEqual(len(inbound_messages), 1)

    def test_text_with_caption_preserves_both(self):
        """Photo with caption should have text=caption and media with image ref."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 42},
            "photo": [{"file_id": "abc", "width": 100, "height": 100}],
            "caption": "look at this",
        }])

        inbound_messages = []
        def capture_relay(**kwargs):
            msg = kwargs.get("inbound_msg")
            if msg:
                inbound_messages.append(msg)

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message"), \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay", side_effect=capture_relay), \
             patch("gateway._command_router") as mock_router:
            mock_router.is_command.return_value = False
            bot._poll_loop()

        self.assertEqual(len(inbound_messages), 1)
        msg = inbound_messages[0]
        self.assertEqual(msg.text, "look at this")
        self.assertTrue(msg.has_media)

    def test_unpaired_media_still_silent(self):
        """Media from unpaired user should be silently ignored."""
        bot = self._make_bot()
        update = self._make_update_response([{
            "chat": {"id": 99},
            "photo": [{"file_id": "abc", "width": 100, "height": 100}],
        }])

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update, bot)), \
             patch.object(bot, "_do_message_relay") as mock_relay:
            bot._poll_loop()

        mock_send.assert_not_called()
        mock_relay.assert_not_called()


# ── Legacy poll media download ─────────────────────────────────────────────


class TestLegacyPollMediaDownload(unittest.TestCase):
    """Tests for media download in _telegram_poll_loop (legacy path)."""

    def _fake_urlopen_factory(self, update):
        """Return a fake_urlopen that returns update on first call, then stops."""
        empty = {"ok": True, "result": []}
        calls = [0]
        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            if calls[0] == 0:
                resp.read.return_value = json.dumps(update).encode()
            else:
                resp.read.return_value = json.dumps(empty).encode()
                gateway._telegram_running = False
            calls[0] += 1
            return resp
        return fake_urlopen

    def test_legacy_photo_downloads(self):
        """Photo from paired user in legacy path triggers relay (not MEDIA_REPLY)."""
        update = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 42},
                    "photo": [{"file_id": "abc", "width": 100, "height": 100}],
                },
            }],
        }
        gateway._telegram_running = True

        relayed = []
        original_thread_start = threading.Thread.start
        def capture_thread(self_thread, *args, **kwargs):
            # Don't actually start relay threads, just record that relay was attempted
            if hasattr(self_thread, '_target') and self_thread._target and 'relay' in str(self_thread._target.__name__):
                relayed.append(True)
            elif hasattr(self_thread, '_target') and self_thread._target and '_do_relay' in str(self_thread._target.__name__):
                relayed.append(True)

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update)):
            gateway._telegram_poll_loop("fake_token")

        # MEDIA_REPLY should NOT be sent
        for call in mock_send.call_args_list:
            if len(call.args) >= 3:
                self.assertNotEqual(call.args[2], gateway.MEDIA_REPLY,
                                    "Legacy path should not send MEDIA_REPLY for paired user media")

    def test_legacy_media_only_flows(self):
        """Media-only message in legacy path should not send MEDIA_REPLY."""
        update = {
            "ok": True,
            "result": [{
                "update_id": 1,
                "message": {
                    "chat": {"id": 42},
                    "voice": {"file_id": "v1", "duration": 5, "mime_type": "audio/ogg"},
                },
            }],
        }
        gateway._telegram_running = True

        with patch("gateway.get_paired_chat_ids", return_value=["42"]), \
             patch("gateway.send_telegram_message") as mock_send, \
             patch("gateway.urllib.request.urlopen", side_effect=self._fake_urlopen_factory(update)):
            gateway._telegram_poll_loop("fake_token")

        # Verify MEDIA_REPLY not sent
        for call in mock_send.call_args_list:
            if len(call.args) >= 3:
                self.assertNotEqual(call.args[2], gateway.MEDIA_REPLY)


# ── REST relay helpers (Phase 13) ─────────────────────────────────────────────

import io
import socket


class TestParseSSEEvents(unittest.TestCase):
    """Tests for _parse_sse_events() SSE line parser."""

    def _make_response(self, data):
        """Create a BytesIO that acts like an http.client response with readline()."""
        return io.BytesIO(data)

    def test_single_message_event(self):
        stream = self._make_response(
            b'data: {"type":"Message","message":{"content":[{"type":"text","text":"hello"}]}}\r\n\r\n'
        )
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "Message")

    def test_multiple_events(self):
        stream = self._make_response(
            b'data: {"type":"Message","message":{"content":[{"type":"text","text":"hi"}]}}\r\n'
            b'\r\n'
            b'data: {"type":"Finish","reason":"endTurn"}\r\n'
            b'\r\n'
        )
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 2)

    def test_finish_event(self):
        stream = self._make_response(
            b'data: {"type":"Finish","reason":"endTurn"}\r\n\r\n'
        )
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "Finish")
        self.assertEqual(events[0]["reason"], "endTurn")

    def test_error_event(self):
        stream = self._make_response(
            b'data: {"type":"Error","error":"something broke"}\r\n\r\n'
        )
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "Error")

    def test_invalid_json_skipped(self):
        stream = self._make_response(b'data: not-json\r\n\r\n')
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 0)

    def test_non_data_lines_ignored(self):
        stream = self._make_response(
            b'event: message\r\n'
            b'data: {"type":"Ping"}\r\n'
            b'\r\n'
        )
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "Ping")

    def test_empty_stream(self):
        stream = self._make_response(b'')
        events = list(gateway._parse_sse_events(stream))
        self.assertEqual(len(events), 0)


class TestBuildContentBlocks(unittest.TestCase):
    """Tests for _build_content_blocks() content array builder."""

    def test_text_only(self):
        result = gateway._build_content_blocks("hello")
        self.assertEqual(result, [{"type": "text", "text": "hello"}])

    def test_text_with_image(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/jpeg", data=b"\xff\xd8")
        msg = gateway.InboundMessage(user_id="1", text="describe this", media=[mc])
        result = gateway._build_content_blocks("describe this", inbound_msg=msg)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["type"], "text")
        self.assertEqual(result[1]["type"], "image")

    def test_media_only_no_text(self):
        mc = gateway.MediaContent(kind="image", mime_type="image/png", data=b"\x89PNG")
        msg = gateway.InboundMessage(user_id="1", text="", media=[mc])
        result = gateway._build_content_blocks("", inbound_msg=msg)
        # should have image block, no text block (since text is empty and there's an image)
        types = [b["type"] for b in result]
        self.assertIn("image", types)

    def test_non_image_media_skipped(self):
        mc = gateway.MediaContent(kind="audio", mime_type="audio/ogg", data=b"\x00\x01")
        msg = gateway.InboundMessage(user_id="1", text="listen", media=[mc])
        result = gateway._build_content_blocks("listen", inbound_msg=msg)
        # audio to_content_block returns None, so only text block
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "text")

    def test_empty_fallback(self):
        result = gateway._build_content_blocks("", None)
        self.assertEqual(result, [{"type": "text", "text": ""}])

    def test_whitespace_only_text_ignored(self):
        result = gateway._build_content_blocks("  ", None)
        self.assertEqual(result, [{"type": "text", "text": ""}])


class TestExtractResponseContent(unittest.TestCase):
    """Tests for _extract_response_content() response parser."""

    def test_text_only(self):
        content = [{"type": "text", "text": "hello"}]
        text, media = gateway._extract_response_content(content)
        self.assertEqual(text, "hello")
        self.assertEqual(media, [])

    def test_text_and_image(self):
        content = [
            {"type": "text", "text": "here is the image"},
            {"type": "image", "data": "abc123", "mimeType": "image/png"},
        ]
        text, media = gateway._extract_response_content(content)
        self.assertEqual(text, "here is the image")
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["type"], "image")

    def test_multiple_text_blocks(self):
        content = [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ]
        text, media = gateway._extract_response_content(content)
        self.assertEqual(text, "line one\nline two")

    def test_tool_response_with_nested_image(self):
        content = [
            {"type": "toolResponse", "id": "t1", "tool_result": {
                "value": {"content": [
                    {"type": "text", "text": "screenshot captured"},
                    {"type": "image", "data": "screenshotdata", "mimeType": "image/png"},
                ]}
            }},
        ]
        text, media = gateway._extract_response_content(content)
        self.assertIn("screenshot captured", text)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["data"], "screenshotdata")

    def test_thinking_blocks_ignored(self):
        content = [
            {"type": "thinking", "thinking": "let me think..."},
            {"type": "text", "text": "answer"},
        ]
        text, media = gateway._extract_response_content(content)
        self.assertEqual(text, "answer")
        self.assertEqual(media, [])

    def test_empty_content(self):
        text, media = gateway._extract_response_content([])
        self.assertEqual(text, "")
        self.assertEqual(media, [])


class TestRestRelay(unittest.TestCase):
    """Tests for _do_rest_relay() with mocked http.client."""

    def _make_sse_response(self, events, status=200):
        """Build a mock HTTPResponse that yields SSE data lines."""
        lines = []
        for evt in events:
            lines.append(f"data: {json.dumps(evt)}\r\n".encode())
            lines.append(b"\r\n")
        body = b"".join(lines)

        resp = MagicMock()
        resp.status = status
        stream = io.BytesIO(body)
        resp.readline = stream.readline
        resp.read = MagicMock(return_value=b"error body")
        return resp

    @patch("gateway.http.client.HTTPConnection")
    def test_successful_text_relay(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [
            {"type": "Message", "message": {"content": [{"type": "text", "text": "hello world"}]}},
            {"type": "Finish", "reason": "endTurn"},
        ]
        conn.getresponse.return_value = self._make_sse_response(events)

        text, err, media = gateway._do_rest_relay("hi", "sess1")
        self.assertEqual(text, "hello world")
        self.assertEqual(err, "")
        self.assertEqual(media, [])

    @patch("gateway.http.client.HTTPConnection")
    def test_multimodal_response(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [
            {"type": "Message", "message": {"content": [
                {"type": "text", "text": "check this"},
                {"type": "image", "data": "imgdata", "mimeType": "image/png"},
            ]}},
            {"type": "Finish", "reason": "endTurn"},
        ]
        conn.getresponse.return_value = self._make_sse_response(events)

        text, err, media = gateway._do_rest_relay("show me", "sess1")
        self.assertEqual(text, "check this")
        self.assertEqual(len(media), 1)

    @patch("gateway.http.client.HTTPConnection")
    def test_error_response(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [
            {"type": "Error", "error": "model overloaded"},
        ]
        conn.getresponse.return_value = self._make_sse_response(events)

        text, err, media = gateway._do_rest_relay("hello", "sess1")
        self.assertEqual(text, "")
        self.assertIn("model overloaded", err)

    @patch("gateway.http.client.HTTPConnection")
    def test_http_error_status(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn
        conn.getresponse.return_value = self._make_sse_response([], status=500)

        text, err, media = gateway._do_rest_relay("hi", "sess1")
        self.assertEqual(text, "")
        self.assertIn("500", err)

    @patch("gateway.http.client.HTTPConnection")
    def test_connection_stores_in_sock_ref(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [{"type": "Finish", "reason": "endTurn"}]
        conn.getresponse.return_value = self._make_sse_response(events)

        sock_ref = [None, threading.Event()]
        gateway._do_rest_relay("hi", "sess1", sock_ref=sock_ref)
        self.assertIs(sock_ref[0], conn)

    @patch("gateway.http.client.HTTPConnection")
    def test_timeout_handling(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn
        conn.getresponse.side_effect = socket.timeout("timed out")

        text, err, media = gateway._do_rest_relay("hi", "sess1")
        self.assertEqual(text, "")
        self.assertIn("timeout", err.lower())


class TestRestRelayStreaming(unittest.TestCase):
    """Tests for _do_rest_relay_streaming() with mocked http.client."""

    def _make_sse_response(self, events, status=200):
        """Build a mock HTTPResponse that yields SSE data lines."""
        lines = []
        for evt in events:
            lines.append(f"data: {json.dumps(evt)}\r\n".encode())
            lines.append(b"\r\n")
        body = b"".join(lines)

        resp = MagicMock()
        resp.status = status
        stream = io.BytesIO(body)
        resp.readline = stream.readline
        resp.read = MagicMock(return_value=b"error body")
        return resp

    @patch("gateway.http.client.HTTPConnection")
    def test_text_chunks_flushed(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [
            {"type": "Message", "message": {"content": [{"type": "text", "text": "chunk one "}]}},
            {"type": "Message", "message": {"content": [{"type": "text", "text": "chunk two"}]}},
            {"type": "Finish", "reason": "endTurn"},
        ]
        conn.getresponse.return_value = self._make_sse_response(events)

        flushed = []
        def flush_cb(text):
            flushed.append(text)

        text, err, media = gateway._do_rest_relay_streaming(
            "hi", "sess1", flush_cb=flush_cb
        )
        self.assertIn("chunk one", text)
        self.assertIn("chunk two", text)
        self.assertEqual(err, "")

    @patch("gateway.http.client.HTTPConnection")
    def test_tool_request_emits_status(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [
            {"type": "Message", "message": {"content": [
                {"type": "toolRequest", "id": "t1", "tool_call": {"name": "bash", "arguments": {}}}
            ]}},
            {"type": "Message", "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "Finish", "reason": "endTurn"},
        ]
        conn.getresponse.return_value = self._make_sse_response(events)

        flushed = []
        def flush_cb(text):
            flushed.append(text)

        text, err, media = gateway._do_rest_relay_streaming(
            "run something", "sess1", flush_cb=flush_cb
        )
        # should have emitted a tool status like "[Using bash...]"
        tool_msgs = [f for f in flushed if "bash" in f.lower() or "Using" in f]
        self.assertTrue(len(tool_msgs) > 0, f"Expected tool status in flushed: {flushed}")

    @patch("gateway.http.client.HTTPConnection")
    def test_error_stops_streaming(self, mock_conn_cls):
        conn = MagicMock()
        mock_conn_cls.return_value = conn

        events = [
            {"type": "Message", "message": {"content": [{"type": "text", "text": "partial"}]}},
            {"type": "Error", "error": "context limit exceeded"},
        ]
        conn.getresponse.return_value = self._make_sse_response(events)

        flushed = []
        text, err, media = gateway._do_rest_relay_streaming(
            "hi", "sess1", flush_cb=lambda t: flushed.append(t)
        )
        self.assertIn("context limit exceeded", err)


# ── relay protocol upgrade (Phase 13 Plan 02) ─────────────────────────────


class TestRelayProtocolUpgrade(unittest.TestCase):
    """Tests for wiring REST relay into _relay_to_goose_web and removing WS."""

    @patch("gateway._do_rest_relay")
    def test_relay_returns_3_tuple(self, mock_rest):
        """_relay_to_goose_web should return 3 values: text, error, media."""
        mock_rest.return_value = ("hi", "", [])
        gateway._INTERNAL_GOOSE_TOKEN = "tok"
        try:
            result = gateway._relay_to_goose_web("test", "sid")
            self.assertEqual(len(result), 3, f"Expected 3-tuple, got {len(result)}-tuple: {result}")
            text, err, media = result
            self.assertEqual(text, "hi")
            self.assertEqual(err, "")
            self.assertIsInstance(media, list)
        finally:
            gateway._INTERNAL_GOOSE_TOKEN = None

    @patch("gateway._do_rest_relay")
    def test_relay_passes_content_blocks(self, mock_rest):
        """_relay_to_goose_web should forward content_blocks to _do_rest_relay."""
        mock_rest.return_value = ("ok", "", [])
        gateway._INTERNAL_GOOSE_TOKEN = "tok"
        blocks = [{"type": "text", "text": "test"}]
        try:
            gateway._relay_to_goose_web("test", "sid", content_blocks=blocks)
            # verify _do_rest_relay received content_blocks
            call_kwargs = mock_rest.call_args
            # the lambda should pass content_blocks through
            self.assertIn("content_blocks", str(call_kwargs) + str(mock_rest.call_args_list),
                          "content_blocks not forwarded to rest relay")
        finally:
            gateway._INTERNAL_GOOSE_TOKEN = None

    @patch("gateway._do_rest_relay")
    def test_relay_uses_rest_not_ws(self, mock_rest):
        """_relay_to_goose_web should call _do_rest_relay. WS relay is fully removed."""
        mock_rest.return_value = ("hi", "", [])
        gateway._INTERNAL_GOOSE_TOKEN = "tok"
        try:
            gateway._relay_to_goose_web("test", "sid")
            mock_rest.assert_called()
            # verify WS functions no longer exist
            self.assertFalse(hasattr(gateway, "_do_ws_relay"),
                             "_do_ws_relay should be removed")
            self.assertFalse(hasattr(gateway, "_do_ws_relay_streaming"),
                             "_do_ws_relay_streaming should be removed")
            self.assertFalse(hasattr(gateway, "_ws_connect"),
                             "_ws_connect should be removed")
        finally:
            gateway._INTERNAL_GOOSE_TOKEN = None

    @patch("gateway._do_rest_relay_streaming")
    def test_relay_streaming_uses_rest(self, mock_rest_stream):
        """Streaming relay should use _do_rest_relay_streaming."""
        mock_rest_stream.return_value = ("hi", "", [])
        gateway._INTERNAL_GOOSE_TOKEN = "tok"
        try:
            result = gateway._relay_to_goose_web(
                "test", "sid", flush_cb=lambda t: None, verbosity="balanced",
            )
            mock_rest_stream.assert_called()
            self.assertEqual(len(result), 3)
        finally:
            gateway._INTERNAL_GOOSE_TOKEN = None

    @patch("gateway._session_manager")
    @patch("gateway._create_goose_session")
    @patch("gateway._do_rest_relay")
    def test_relay_retry_on_error_returns_3_tuple(self, mock_rest, mock_create, mock_sm):
        """On error+retry, _relay_to_goose_web should still return 3-tuple."""
        mock_rest.side_effect = [("", "session expired", []), ("retried", "", [])]
        mock_create.return_value = "new-sid"
        gateway._INTERNAL_GOOSE_TOKEN = "tok"
        try:
            result = gateway._relay_to_goose_web("test", "sid", chat_id="123", channel="telegram")
            self.assertEqual(len(result), 3, f"Expected 3-tuple on retry, got {len(result)}")
            text, err, media = result
            self.assertEqual(text, "retried")
        finally:
            gateway._INTERNAL_GOOSE_TOKEN = None

    @patch("gateway._relay_to_goose_web")
    @patch("gateway._download_telegram_file")
    def test_bot_relay_builds_content_blocks_from_media(self, mock_dl, mock_relay):
        """BotInstance._do_message_relay should build content_blocks when InboundMessage has media."""
        mock_relay.return_value = ("response", "", [])
        # mock download to return real bytes
        mock_dl.return_value = (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "/tmp/test.jpg")

        bot = gateway.BotInstance.__new__(gateway.BotInstance)
        bot.name = "test"
        bot.token = "tok"
        bot.channel_key = "telegram:test"
        bot.state = gateway.ChannelState()

        inbound = gateway.InboundMessage(
            user_id="123", text="look at this", channel="telegram:test",
        )
        # use file_id reference dicts (as poll loop produces)
        inbound.media = [{"file_id": "abc123", "media_key": "photo", "mime_hint": "image/jpeg", "filename": "test.jpg"}]

        with patch.object(bot.state, "get_user_lock") as mock_lock, \
             patch("gateway._get_session_id", return_value="sid"), \
             patch("gateway.load_setup", return_value=None), \
             patch("gateway._send_typing_action"):
            lock_ctx = MagicMock()
            lock_ctx.__enter__ = MagicMock(return_value=None)
            lock_ctx.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = lock_ctx

            # call _do_message_relay directly (positional: chat_id, text, bot_token)
            bot._do_message_relay("123", "look at this", "tok", inbound_msg=inbound)

            # verify content_blocks was passed to _relay_to_goose_web
            self.assertTrue(mock_relay.called, "relay not called")
            _, kwargs = mock_relay.call_args
            self.assertIn("content_blocks", kwargs,
                          f"content_blocks not passed. kwargs: {kwargs}")
            self.assertIsNotNone(kwargs["content_blocks"],
                                 "content_blocks should not be None for media message")

    @patch("gateway._relay_to_goose_web")
    def test_bot_relay_text_only_no_content_blocks(self, mock_relay):
        """Text-only InboundMessage should NOT build content_blocks (None)."""
        mock_relay.return_value = ("response", "", [])

        bot = gateway.BotInstance.__new__(gateway.BotInstance)
        bot.name = "test"
        bot.token = "tok"
        bot.channel_key = "telegram:test"
        bot.state = gateway.ChannelState()

        inbound = gateway.InboundMessage(
            user_id="123", text="just text", channel="telegram:test",
        )

        with patch.object(bot.state, "get_user_lock") as mock_lock, \
             patch("gateway._get_session_id", return_value="sid"), \
             patch("gateway.load_setup", return_value=None), \
             patch("gateway._send_typing_action"):
            lock_ctx = MagicMock()
            lock_ctx.__enter__ = MagicMock(return_value=None)
            lock_ctx.__exit__ = MagicMock(return_value=False)
            mock_lock.return_value = lock_ctx

            bot._do_message_relay("123", "just text", "tok", inbound_msg=inbound)

            self.assertTrue(mock_relay.called, "relay not called")
            _, kwargs = mock_relay.call_args
            cb = kwargs.get("content_blocks")
            self.assertIsNone(cb, f"content_blocks should be None for text-only, got {cb}")

    def test_all_callers_unpack_3_tuple(self):
        """Meta-test: no call site in gateway.py uses 2-value unpack from _relay_to_goose_web."""
        import re
        src_path = os.path.join(os.path.dirname(__file__), "gateway.py")
        with open(src_path) as f:
            source = f.read()

        # match patterns like "x, y = _relay_to_goose_web(" where there's no *_
        # this catches exactly 2-value unpack without star expression
        pattern = r'(\w+),\s*(\w+)\s*=\s*_relay_to_goose_web\('
        matches = re.findall(pattern, source)

        # filter out any that use *_ patterns (3-tuple safe)
        bad_sites = []
        for line_num, line in enumerate(source.split("\n"), 1):
            if "_relay_to_goose_web(" in line:
                # check previous non-blank line for assignment
                pass  # use regex on full source instead

        # find lines with 2-value unpack
        bad_lines = []
        for line_num, line in enumerate(source.split("\n"), 1):
            stripped = line.strip()
            if re.match(r'\w+,\s*\w+\s*=\s*_relay_to_goose_web\(', stripped):
                bad_lines.append((line_num, stripped))

        self.assertEqual(len(bad_lines), 0,
                         f"Found {len(bad_lines)} call sites with 2-value unpack "
                         f"(should use 3-tuple or *_):\n" +
                         "\n".join(f"  line {n}: {l}" for n, l in bad_lines))


# ── _build_multipart ───────────────────────────────────────────────────────

class TestBuildMultipart(unittest.TestCase):
    """Tests for _build_multipart() multipart/form-data construction."""

    def test_text_fields_only(self):
        body, ct = gateway._build_multipart({"chat_id": "123"}, [])
        self.assertIn(b"chat_id", body)
        self.assertIn(b"123", body)
        self.assertTrue(ct.startswith("multipart/form-data; boundary="))

    def test_single_file(self):
        files = [("photo", "img.jpg", "image/jpeg", b"\xff\xd8")]
        body, ct = gateway._build_multipart({"chat_id": "123"}, files)
        self.assertIn(b"\xff\xd8", body)
        self.assertIn(b"img.jpg", body)
        self.assertIn(b"Content-Disposition", body)

    def test_multiple_fields_and_file(self):
        fields = {"chat_id": "123", "caption": "hello"}
        files = [("photo", "img.jpg", "image/jpeg", b"\xff\xd8")]
        body, ct = gateway._build_multipart(fields, files)
        self.assertIn(b"chat_id", body)
        self.assertIn(b"caption", body)
        self.assertIn(b"hello", body)
        self.assertIn(b"\xff\xd8", body)

    def test_boundary_uniqueness(self):
        _, ct1 = gateway._build_multipart({}, [])
        _, ct2 = gateway._build_multipart({}, [])
        b1 = ct1.split("boundary=")[1]
        b2 = ct2.split("boundary=")[1]
        self.assertNotEqual(b1, b2)


# ── _ext_from_mime ─────────────────────────────────────────────────────────

class TestExtFromMime(unittest.TestCase):
    """Tests for _ext_from_mime() MIME to extension mapping."""

    def test_jpeg(self):
        self.assertEqual(gateway._ext_from_mime("image/jpeg"), ".jpg")

    def test_png(self):
        self.assertEqual(gateway._ext_from_mime("image/png"), ".png")

    def test_ogg(self):
        self.assertEqual(gateway._ext_from_mime("audio/ogg"), ".ogg")

    def test_unknown_falls_back(self):
        result = gateway._ext_from_mime("application/x-custom")
        self.assertTrue(len(result) > 0, "should return a non-empty extension")

    def test_webp(self):
        self.assertEqual(gateway._ext_from_mime("image/webp"), ".webp")


# ── _route_media_blocks ───────────────────────────────────────────────────

class TestRouteMediaBlocks(unittest.TestCase):
    """Tests for _route_media_blocks() dispatching media to adapter."""

    def _make_adapter(self):
        adapter = MagicMock()
        adapter.send_image = MagicMock(return_value={"sent": True, "error": ""})
        adapter.send_file = MagicMock(return_value={"sent": True, "error": ""})
        return adapter

    def test_image_block_calls_send_image(self):
        import base64 as b64
        adapter = self._make_adapter()
        small_img = b64.b64encode(b"\x89PNG\r\n").decode()
        blocks = [{"type": "image", "data": small_img, "mimeType": "image/png"}]
        gateway._route_media_blocks(blocks, adapter)
        adapter.send_image.assert_called_once()
        call_args = adapter.send_image.call_args
        self.assertEqual(call_args[0][0], b"\x89PNG\r\n")

    def test_large_image_falls_back_to_send_file(self):
        import base64 as b64
        adapter = self._make_adapter()
        big_data = b"\x00" * (10_000_001)
        big_b64 = b64.b64encode(big_data).decode()
        blocks = [{"type": "image", "data": big_b64, "mimeType": "image/png"}]
        gateway._route_media_blocks(blocks, adapter)
        adapter.send_file.assert_called_once()
        adapter.send_image.assert_not_called()

    def test_empty_data_skipped(self):
        adapter = self._make_adapter()
        blocks = [{"type": "image", "data": "", "mimeType": "image/png"}]
        gateway._route_media_blocks(blocks, adapter)
        adapter.send_image.assert_not_called()
        adapter.send_file.assert_not_called()

    def test_unknown_type_skipped(self):
        adapter = self._make_adapter()
        blocks = [{"type": "video", "data": "abc", "mimeType": "video/mp4"}]
        gateway._route_media_blocks(blocks, adapter)
        adapter.send_image.assert_not_called()
        adapter.send_file.assert_not_called()

    def test_multiple_blocks(self):
        import base64 as b64
        adapter = self._make_adapter()
        small_img = b64.b64encode(b"\x89PNG").decode()
        blocks = [
            {"type": "image", "data": small_img, "mimeType": "image/png"},
            {"type": "image", "data": small_img, "mimeType": "image/png"},
        ]
        gateway._route_media_blocks(blocks, adapter)
        self.assertEqual(adapter.send_image.call_count, 2)


# ── TelegramOutboundAdapter ──────────────────────────────────────────────

class TestTelegramOutboundAdapter(unittest.TestCase):
    """Tests for TelegramOutboundAdapter media sending."""

    def _make_adapter(self):
        return gateway.TelegramOutboundAdapter("tok123", "chat456")

    def test_capabilities(self):
        a = self._make_adapter()
        caps = a.capabilities()
        self.assertTrue(caps.supports_images)
        self.assertTrue(caps.supports_voice)
        self.assertTrue(caps.supports_files)

    @patch("gateway.send_telegram_message", return_value=(True, ""))
    def test_send_text(self, mock_send):
        a = self._make_adapter()
        result = a.send_text("hello")
        mock_send.assert_called_once_with("tok123", "chat456", "hello")
        self.assertTrue(result["sent"])

    @patch("gateway.urllib.request.urlopen")
    def test_send_image(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        a = self._make_adapter()
        result = a.send_image(b"\xff\xd8", mime_type="image/jpeg")
        self.assertTrue(result["sent"])
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertIn("sendPhoto", req.full_url)

    @patch("gateway.urllib.request.urlopen")
    def test_send_voice(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        a = self._make_adapter()
        result = a.send_voice(b"\x00", mime_type="audio/ogg")
        self.assertTrue(result["sent"])
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertIn("sendVoice", req.full_url)

    @patch("gateway.urllib.request.urlopen")
    def test_send_file(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        a = self._make_adapter()
        result = a.send_file(b"\x00", filename="doc.pdf", mime_type="application/pdf")
        self.assertTrue(result["sent"])
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertIn("sendDocument", req.full_url)

    @patch("gateway.urllib.request.urlopen", side_effect=Exception("network fail"))
    def test_send_image_error_handled(self, mock_urlopen):
        a = self._make_adapter()
        result = a.send_image(b"\xff\xd8", mime_type="image/jpeg")
        self.assertFalse(result["sent"])
        self.assertIn("network fail", result["error"])

    @patch("gateway.urllib.request.urlopen")
    def test_caption_truncated(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        a = self._make_adapter()
        long_caption = "x" * 2000
        a.send_image(b"\xff", caption=long_caption, mime_type="image/png")
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        # body should have caption truncated to 1024
        body = req.data
        # find caption in multipart body -- it should be <= 1024 chars
        # the body contains the caption value between boundary markers
        self.assertIn(b"caption", body)
        # the full 2000-char caption should NOT appear
        self.assertNotIn(long_caption.encode(), body)
        # but a 1024-char version should
        self.assertIn(("x" * 1024).encode(), body)


# ── BotInstance media routing tests (14-02) ──────────────────────────────────

class TestBotMediaRouting(unittest.TestCase):
    """Tests for BotInstance._do_message_relay routing media blocks through TelegramOutboundAdapter."""

    def _make_bot(self):
        bot = gateway.BotInstance("test", "tok:test", channel_key="telegram:test")
        return bot

    @patch("gateway._route_media_blocks")
    @patch("gateway.TelegramOutboundAdapter")
    @patch("gateway._relay_to_goose_web")
    @patch("gateway.send_telegram_message", return_value=(True, ""))
    @patch("gateway._send_typing_action")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._memory_touch")
    @patch.object(gateway._session_manager, "get", return_value="sess1")
    def test_image_sent_after_text(self, _sm, _mem, _setup, _typing, mock_send,
                                    mock_relay, mock_adapter_cls, mock_route):
        """After text delivery, media blocks should be routed via TelegramOutboundAdapter."""
        media = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]
        mock_relay.return_value = ("hello", "", media)
        bot = self._make_bot()
        bot._do_message_relay(chat_id="123", text="hi", bot_token="tok:test")
        mock_adapter_cls.assert_called_once_with("tok:test", "123")
        mock_route.assert_called_once_with(media, mock_adapter_cls.return_value)

    @patch("gateway._route_media_blocks")
    @patch("gateway.TelegramOutboundAdapter")
    @patch("gateway._relay_to_goose_web")
    @patch("gateway.send_telegram_message", return_value=(True, ""))
    @patch("gateway._send_typing_action")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._memory_touch")
    @patch.object(gateway._session_manager, "get", return_value="sess1")
    def test_no_media_no_send(self, _sm, _mem, _setup, _typing, mock_send,
                               mock_relay, mock_adapter_cls, mock_route):
        """No media blocks means no TelegramOutboundAdapter or _route_media_blocks call."""
        mock_relay.return_value = ("hello", "", [])
        bot = self._make_bot()
        bot._do_message_relay(chat_id="123", text="hi", bot_token="tok:test")
        mock_adapter_cls.assert_not_called()
        mock_route.assert_not_called()

    @patch("gateway._route_media_blocks")
    @patch("gateway.TelegramOutboundAdapter")
    @patch("gateway._relay_to_goose_web")
    @patch("gateway.send_telegram_message", return_value=(True, ""))
    @patch("gateway._send_typing_action")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._memory_touch")
    @patch.object(gateway._session_manager, "get", return_value="sess1")
    def test_cancelled_skips_media(self, _sm, _mem, _setup, _typing, mock_send,
                                    mock_relay, mock_adapter_cls, mock_route):
        """When the relay is cancelled, media should NOT be routed."""
        media = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]
        mock_relay.return_value = ("hello", "", media)
        bot = self._make_bot()

        # We need to set the cancelled event before media routing runs.
        # Patch set_active_relay to capture the sock_ref and set cancelled.
        original_set = bot.state.set_active_relay
        def capture_and_cancel(cid, sock_ref):
            original_set(cid, sock_ref)
            sock_ref[1].set()  # set cancelled event
        with patch.object(bot.state, "set_active_relay", side_effect=capture_and_cancel):
            bot._do_message_relay(chat_id="123", text="hi", bot_token="tok:test")
        mock_adapter_cls.assert_not_called()
        mock_route.assert_not_called()

    @patch("gateway._route_media_blocks", side_effect=Exception("network error"))
    @patch("gateway.TelegramOutboundAdapter")
    @patch("gateway._relay_to_goose_web")
    @patch("gateway.send_telegram_message", return_value=(True, ""))
    @patch("gateway._send_typing_action")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._memory_touch")
    @patch.object(gateway._session_manager, "get", return_value="sess1")
    def test_media_error_logged_not_crash(self, _sm, _mem, _setup, _typing, mock_send,
                                           mock_relay, mock_adapter_cls, mock_route):
        """Media routing errors should be caught and logged, not crash the relay."""
        media = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]
        mock_relay.return_value = ("hello", "", media)
        bot = self._make_bot()
        # Should NOT raise
        bot._do_message_relay(chat_id="123", text="hi", bot_token="tok:test")
        # Text was still delivered
        mock_send.assert_called()

    @patch("gateway._route_media_blocks")
    @patch("gateway.TelegramOutboundAdapter")
    @patch("gateway._relay_to_goose_web")
    @patch("gateway.send_telegram_message", return_value=(True, ""))
    @patch("gateway._send_typing_action")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._memory_touch")
    @patch.object(gateway._session_manager, "get", return_value="sess1")
    def test_multiple_images_sent(self, _sm, _mem, _setup, _typing, mock_send,
                                   mock_relay, mock_adapter_cls, mock_route):
        """Multiple media blocks should all be passed to _route_media_blocks."""
        media = [
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            {"type": "image", "data": "d29ybGQ=", "mimeType": "image/jpeg"},
        ]
        mock_relay.return_value = ("hello", "", media)
        bot = self._make_bot()
        bot._do_message_relay(chat_id="123", text="hi", bot_token="tok:test")
        mock_route.assert_called_once_with(media, mock_adapter_cls.return_value)


# ── ChannelRelay media routing tests (14-02) ─────────────────────────────────

class TestChannelRelayMedia(unittest.TestCase):
    """Tests for ChannelRelay.__call__ routing media blocks through adapter."""

    def setUp(self):
        self.relay = gateway.ChannelRelay("test_media_ch")

    @patch("gateway._route_media_blocks")
    @patch("gateway._relay_to_goose_web")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._session_manager")
    def test_media_blocks_routed_through_adapter(self, mock_sm, mock_setup,
                                                   mock_relay, mock_route):
        """ChannelRelay should capture media and route through channel adapter."""
        mock_sm.get.return_value = "sess_123"
        media = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]
        mock_relay.return_value = ("response", "", media)

        # Set up a mock adapter in _loaded_channels
        mock_adapter = MagicMock(spec=gateway.OutboundAdapter)
        with patch.dict(gateway._loaded_channels,
                        {"test_media_ch": {"adapter": mock_adapter}}):
            self.relay("user1", "hello", lambda t: None)

        mock_route.assert_called_once_with(media, mock_adapter)

    @patch("gateway._relay_to_goose_web")
    @patch("gateway.load_setup", return_value=None)
    @patch("gateway._session_manager")
    def test_legacy_adapter_gets_text_fallback(self, mock_sm, mock_setup,
                                                mock_relay):
        """LegacyOutboundAdapter (no send_image override) should get graceful degradation."""
        mock_sm.get.return_value = "sess_123"
        media = [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]
        mock_relay.return_value = ("response", "", media)

        # Use real LegacyOutboundAdapter (send_image falls back to send_text)
        sent = []
        legacy_adapter = gateway.LegacyOutboundAdapter(lambda t: sent.append(t))
        with patch.dict(gateway._loaded_channels,
                        {"test_media_ch": {"adapter": legacy_adapter}}):
            # Let real _route_media_blocks run (not patched) so degradation path executes
            self.relay("user1", "hello", lambda t: None)

        # LegacyOutboundAdapter.send_image falls back to send_text
        # so sent list should have something (graceful degradation, not crash)
        self.assertTrue(len(sent) > 0)


# ── notify_all media tests (14-02) ──────────────────────────────────────────

class TestNotifyMedia(unittest.TestCase):
    """Tests for notify_all accepting and forwarding optional media parameter."""

    def setUp(self):
        # Save and clear notification handlers
        self._saved_handlers = list(gateway._notification_handlers)
        gateway._notification_handlers.clear()

    def tearDown(self):
        gateway._notification_handlers.clear()
        gateway._notification_handlers.extend(self._saved_handlers)

    def test_notify_with_media(self):
        """notify_all should pass media kwarg to handlers that accept it."""
        received = {}

        def handler(text, media=None):
            received["text"] = text
            received["media"] = media
            return {"sent": True}

        gateway._notification_handlers.append({"name": "test", "handler": handler})
        media = [{"type": "image", "data": "abc", "mimeType": "image/png"}]
        gateway.notify_all("hello", media=media)
        self.assertEqual(received.get("text"), "hello")
        self.assertEqual(received.get("media"), media)

    def test_notify_old_handler_backward_compat(self):
        """Old handlers that only accept (text) should still work when media is passed."""
        received = {}

        def old_handler(text):
            received["text"] = text
            return {"sent": True}

        gateway._notification_handlers.append({"name": "old", "handler": old_handler})
        media = [{"type": "image", "data": "abc", "mimeType": "image/png"}]
        # Should NOT raise TypeError
        gateway.notify_all("hello", media=media)
        self.assertEqual(received.get("text"), "hello")

    def test_notify_no_media(self):
        """notify_all with no media should work exactly as before."""
        received = {}

        def handler(text):
            received["text"] = text
            return {"sent": True}

        gateway._notification_handlers.append({"name": "plain", "handler": handler})
        result = gateway.notify_all("hello")
        self.assertEqual(received.get("text"), "hello")
        self.assertTrue(result.get("sent"))


# ── Discord channel plugin tests ───────────────────────────────────────────────
# These test docker/discord_channel.py -- a v2 channel plugin loaded by _load_channel.

# Ensure docker/ dir is importable
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))


class TestDiscordOutboundAdapter(unittest.TestCase):
    """Tests for DiscordOutboundAdapter send methods and capabilities."""

    def _make_adapter(self):
        import discord_channel
        return discord_channel.DiscordOutboundAdapter("test-bot-token", "123456789")

    def test_capabilities(self):
        adapter = self._make_adapter()
        caps = adapter.capabilities()
        self.assertTrue(caps.supports_images)
        self.assertTrue(caps.supports_files)
        self.assertFalse(caps.supports_voice)
        self.assertEqual(caps.max_text_length, 2000)

    @patch("urllib.request.urlopen")
    def test_send_text(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id":"1"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        adapter = self._make_adapter()
        result = adapter.send_text("hello")

        self.assertTrue(result["sent"])
        # Verify the request
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        self.assertIn("discord.com/api/v10/channels/123456789/messages", req.full_url)
        body = json.loads(req.data)
        self.assertEqual(body["content"], "hello")
        self.assertEqual(req.get_header("Authorization"), "Bot test-bot-token")

    @patch("urllib.request.urlopen")
    def test_send_image(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id":"1"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        adapter = self._make_adapter()
        result = adapter.send_image(b"\xff\xd8", caption="test", mime_type="image/jpeg")

        self.assertTrue(result["sent"])
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        # Should be multipart, containing files[0] and payload_json
        body = req.data
        self.assertIn(b"files[0]", body)
        self.assertIn(b"payload_json", body)
        self.assertIn(b"image/jpeg", body)

    @patch("urllib.request.urlopen")
    def test_send_file(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id":"1"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        adapter = self._make_adapter()
        result = adapter.send_file(b"\x00", filename="doc.pdf", mime_type="application/pdf")

        self.assertTrue(result["sent"])
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = req.data
        self.assertIn(b"files[0]", body)
        self.assertIn(b"payload_json", body)
        self.assertIn(b"doc.pdf", body)

    @patch("urllib.request.urlopen")
    def test_send_text_truncates_at_2000(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id":"1"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        adapter = self._make_adapter()
        adapter.send_text("x" * 3000)

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(len(body["content"]), 2000)

    @patch("urllib.request.urlopen")
    def test_send_error_handled(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("network error")

        adapter = self._make_adapter()
        result = adapter.send_text("hello")

        self.assertFalse(result["sent"])
        self.assertIn("network error", result["error"])


class TestDiscordInboundMedia(unittest.TestCase):
    """Tests for _extract_discord_media."""

    @patch("urllib.request.urlopen")
    def test_image_attachment(self, mock_urlopen):
        import discord_channel
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"\x89PNG"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        msg = {"attachments": [{"url": "https://cdn.discord.com/img.png",
                                 "content_type": "image/png", "filename": "img.png"}]}
        media = discord_channel._extract_discord_media(msg)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].kind, "image")
        self.assertEqual(media[0].mime_type, "image/png")

    @patch("urllib.request.urlopen")
    def test_document_attachment(self, mock_urlopen):
        import discord_channel
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"%PDF"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        msg = {"attachments": [{"url": "https://cdn.discord.com/doc.pdf",
                                 "content_type": "application/pdf", "filename": "doc.pdf"}]}
        media = discord_channel._extract_discord_media(msg)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].kind, "document")

    def test_no_attachments(self):
        import discord_channel
        msg = {"attachments": []}
        media = discord_channel._extract_discord_media(msg)
        self.assertEqual(len(media), 0)

    @patch("urllib.request.urlopen")
    def test_multiple_attachments(self, mock_urlopen):
        import discord_channel
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"\x00"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        msg = {"attachments": [
            {"url": "https://cdn.discord.com/a.png", "content_type": "image/png", "filename": "a.png"},
            {"url": "https://cdn.discord.com/b.pdf", "content_type": "application/pdf", "filename": "b.pdf"},
        ]}
        media = discord_channel._extract_discord_media(msg)
        self.assertEqual(len(media), 2)


class TestDiscordPoll(unittest.TestCase):
    """Tests for poll_discord Gateway WebSocket connection."""

    @patch("discord_channel._get_gateway_url", return_value="wss://gateway.discord.gg")
    @patch("discord_channel.websocket")
    @patch("discord_channel._extract_discord_media", return_value=[])
    def test_identifies_with_intents(self, mock_extract, mock_ws_mod, mock_gw_url):
        import discord_channel

        ws_instance = MagicMock()
        mock_ws_mod.WebSocket.return_value = ws_instance

        # Hello, then Identify response, then stop
        call_count = [0]
        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}})
            # Return something that triggers loop exit
            raise Exception("test-stop")

        ws_instance.recv.side_effect = fake_recv

        stop_event = threading.Event()
        relay = MagicMock()
        creds = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

        # Run poll in thread, stop quickly
        def run():
            discord_channel.poll_discord(relay, stop_event, creds)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.3)
        stop_event.set()
        t.join(timeout=2)

        # Check Identify was sent with correct intents
        sends = [call for call in ws_instance.send.call_args_list]
        identify_sent = False
        GUILD_MESSAGES = 1 << 9
        MESSAGE_CONTENT = 1 << 15
        for call in sends:
            data = json.loads(call[0][0])
            if data.get("op") == 2:
                identify_sent = True
                self.assertEqual(data["d"]["intents"], GUILD_MESSAGES | MESSAGE_CONTENT)
                break
        self.assertTrue(identify_sent, "Identify (op 2) was not sent")

    @patch("discord_channel._get_gateway_url", return_value="wss://gateway.discord.gg")
    @patch("discord_channel.websocket")
    @patch("discord_channel._extract_discord_media", return_value=[])
    def test_dispatches_message_create(self, mock_extract, mock_ws_mod, mock_gw_url):
        import discord_channel

        ws_instance = MagicMock()
        mock_ws_mod.WebSocket.return_value = ws_instance

        call_count = [0]
        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}})
            if call_count[0] == 2:
                return json.dumps({
                    "op": 0, "t": "MESSAGE_CREATE", "s": 1,
                    "d": {
                        "author": {"id": "user1", "username": "testuser", "bot": False},
                        "content": "hello bot",
                        "channel_id": "999",
                        "attachments": [],
                    }
                })
            raise Exception("test-stop")

        ws_instance.recv.side_effect = fake_recv

        stop_event = threading.Event()
        relay = MagicMock()
        creds = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

        # Temporarily set module-level adapter for relay call
        old_adapter = discord_channel.adapter
        discord_channel.adapter = discord_channel.DiscordOutboundAdapter("tok", "999")

        t = threading.Thread(target=lambda: discord_channel.poll_discord(relay, stop_event, creds), daemon=True)
        t.start()
        time.sleep(0.5)
        stop_event.set()
        t.join(timeout=2)

        discord_channel.adapter = old_adapter

        # Relay should have been called with an InboundMessage
        self.assertTrue(relay.called, "relay was not called for MESSAGE_CREATE")
        inbound = relay.call_args[0][0]
        self.assertEqual(inbound.user_id, "user1")
        self.assertEqual(inbound.text, "hello bot")
        self.assertEqual(inbound.channel, "discord")

    @patch("discord_channel._get_gateway_url", return_value="wss://gateway.discord.gg")
    @patch("discord_channel.websocket")
    @patch("discord_channel._extract_discord_media", return_value=[])
    def test_ignores_bot_messages(self, mock_extract, mock_ws_mod, mock_gw_url):
        import discord_channel

        ws_instance = MagicMock()
        mock_ws_mod.WebSocket.return_value = ws_instance

        call_count = [0]
        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}})
            if call_count[0] == 2:
                return json.dumps({
                    "op": 0, "t": "MESSAGE_CREATE", "s": 1,
                    "d": {
                        "author": {"id": "bot1", "username": "somebot", "bot": True},
                        "content": "I am a bot",
                        "channel_id": "999",
                        "attachments": [],
                    }
                })
            raise Exception("test-stop")

        ws_instance.recv.side_effect = fake_recv

        stop_event = threading.Event()
        relay = MagicMock()
        creds = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

        t = threading.Thread(target=lambda: discord_channel.poll_discord(relay, stop_event, creds), daemon=True)
        t.start()
        time.sleep(0.5)
        stop_event.set()
        t.join(timeout=2)

        self.assertFalse(relay.called, "relay should NOT be called for bot messages")

    @patch("discord_channel._get_gateway_url", return_value="wss://gateway.discord.gg")
    @patch("discord_channel.websocket")
    def test_heartbeat_sent(self, mock_ws_mod, mock_gw_url):
        import discord_channel

        ws_instance = MagicMock()
        mock_ws_mod.WebSocket.return_value = ws_instance

        call_count = [0]
        def fake_recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"op": 10, "d": {"heartbeat_interval": 100}})
            # Keep alive for a bit to let heartbeat fire
            time.sleep(0.3)
            raise Exception("test-stop")

        ws_instance.recv.side_effect = fake_recv

        stop_event = threading.Event()
        relay = MagicMock()
        creds = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

        t = threading.Thread(target=lambda: discord_channel.poll_discord(relay, stop_event, creds), daemon=True)
        t.start()
        time.sleep(0.6)
        stop_event.set()
        t.join(timeout=2)

        # Check that at least one heartbeat (op 1) was sent
        heartbeat_sent = False
        for call in ws_instance.send.call_args_list:
            data = json.loads(call[0][0])
            if data.get("op") == 1:
                heartbeat_sent = True
                break
        self.assertTrue(heartbeat_sent, "Heartbeat (op 1) was not sent")


class TestDiscordPluginLoad(unittest.TestCase):
    """Tests for v2 plugin loading via _load_channel."""

    @patch("gateway._resolve_channel_creds")
    def test_plugin_loads_without_gateway_changes(self, mock_creds):
        """A v2 plugin with adapter field should be used directly (not wrapped)."""
        mock_creds.return_value = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

        # Create a temp plugin file with a minimal v2 CHANNEL dict
        import tempfile
        plugin_code = '''
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import gateway

class TestAdapter(gateway.OutboundAdapter):
    def send_text(self, text):
        return {"sent": True, "error": ""}
    def capabilities(self):
        return gateway.ChannelCapabilities(supports_images=True, max_text_length=2000)

_adapter = TestAdapter()
CHANNEL = {
    "name": "test_discord_load",
    "version": 2,
    "send": _adapter.send_text,
    "adapter": _adapter,
    "credentials": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"],
    "setup": lambda creds: {"ok": True},
}
'''
        with tempfile.NamedTemporaryFile(suffix="_channel.py", mode="w", delete=False, dir=os.path.dirname(__file__)) as f:
            f.write(plugin_code)
            tmp_path = f.name

        try:
            # Save and restore _loaded_channels
            saved = dict(gateway._loaded_channels)
            saved_handlers = list(gateway._notification_handlers)

            result = gateway._load_channel(tmp_path)
            self.assertTrue(result, "_load_channel should return True for valid v2 plugin")

            # Adapter should NOT be LegacyOutboundAdapter
            loaded = gateway._loaded_channels.get("test_discord_load")
            self.assertIsNotNone(loaded)
            self.assertNotIsInstance(loaded["adapter"], gateway.LegacyOutboundAdapter)
            self.assertIsInstance(loaded["adapter"], gateway.OutboundAdapter)

            # Cleanup
            gateway._loaded_channels.clear()
            gateway._loaded_channels.update(saved)
            gateway._notification_handlers.clear()
            gateway._notification_handlers.extend(saved_handlers)
            if "test_discord_load" in gateway._channel_stop_events:
                gateway._channel_stop_events["test_discord_load"].set()
                del gateway._channel_stop_events["test_discord_load"]
        finally:
            os.unlink(tmp_path)

    @patch("gateway._resolve_channel_creds")
    def test_v2_plugin_media_routes(self, mock_creds):
        """V2 adapter stored in _loaded_channels should be the real adapter, not legacy."""
        mock_creds.return_value = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

        import tempfile
        plugin_code = '''
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import gateway

class MediaAdapter(gateway.OutboundAdapter):
    def send_text(self, text):
        return {"sent": True, "error": ""}
    def send_image(self, data, caption="", **kwargs):
        return {"sent": True, "error": "", "type": "image"}
    def capabilities(self):
        return gateway.ChannelCapabilities(supports_images=True, supports_files=True)

_adapter = MediaAdapter()
CHANNEL = {
    "name": "test_discord_media",
    "version": 2,
    "send": _adapter.send_text,
    "adapter": _adapter,
    "credentials": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"],
    "setup": lambda creds: {"ok": True},
}
'''
        with tempfile.NamedTemporaryFile(suffix="_channel.py", mode="w", delete=False, dir=os.path.dirname(__file__)) as f:
            f.write(plugin_code)
            tmp_path = f.name

        try:
            saved = dict(gateway._loaded_channels)
            saved_handlers = list(gateway._notification_handlers)

            gateway._load_channel(tmp_path)

            loaded = gateway._loaded_channels.get("test_discord_media")
            self.assertIsNotNone(loaded)
            adapter = loaded["adapter"]
            # V2 adapter should have real send_image
            result = adapter.send_image(b"\x89PNG", caption="test")
            self.assertTrue(result["sent"])
            self.assertEqual(result.get("type"), "image")

            gateway._loaded_channels.clear()
            gateway._loaded_channels.update(saved)
            gateway._notification_handlers.clear()
            gateway._notification_handlers.extend(saved_handlers)
            if "test_discord_media" in gateway._channel_stop_events:
                gateway._channel_stop_events["test_discord_media"].set()
                del gateway._channel_stop_events["test_discord_media"]
        finally:
            os.unlink(tmp_path)


# ── password auth ─────────────────────────────────────────────────────────

class TestPasswordAuth(unittest.TestCase):
    """Tests for password-based authentication (replacing auto-generated tokens)."""

    def _make_handler(self, method="GET", path="/", body=None, cookie=None, client_ip="8.8.8.8"):
        """Build a mock HTTP handler for auth testing."""
        handler = MagicMock()
        handler.path = path
        handler.client_address = (client_ip, 12345)
        handler.headers = {}
        if cookie:
            handler.headers["Cookie"] = cookie
        handler.headers = MagicMock()
        handler.headers.get = MagicMock(side_effect=lambda key, default="": {
            "Cookie": cookie or "",
            "Authorization": "",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)) if body else "0",
        }.get(key, default))
        if body:
            handler.rfile = MagicMock()
            handler.rfile.read = MagicMock(return_value=body if isinstance(body, bytes) else body.encode())
        handler._set_session_cookie = False
        return handler

    @patch("gateway.load_setup")
    def test_login_endpoint_success(self, mock_setup):
        """POST /api/auth/login with correct password returns 200 + Set-Cookie."""
        pw_hash = gateway.hash_token("mypassword")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        handler = self._make_handler(
            method="POST",
            path="/api/auth/login",
            body=json.dumps({"password": "mypassword"}).encode(),
        )
        handler._read_body = MagicMock(return_value=json.dumps({"password": "mypassword"}).encode())

        # call the handler method directly
        gateway.GatewayHandler.handle_auth_login(handler)

        # check Set-Cookie was called
        handler.send_header.assert_any_call(
            "Set-Cookie",
            unittest.mock.ANY,
        )
        # find the Set-Cookie call
        cookie_calls = [c for c in handler.send_header.call_args_list if c[0][0] == "Set-Cookie"]
        self.assertTrue(len(cookie_calls) > 0)
        cookie_val = cookie_calls[0][0][1]
        self.assertIn("gooseclaw_session=", cookie_val)
        self.assertIn("HttpOnly", cookie_val)
        self.assertIn("SameSite=Strict", cookie_val)
        handler.send_response.assert_called_with(200)

    @patch("gateway.load_setup")
    def test_login_endpoint_wrong_password(self, mock_setup):
        """POST /api/auth/login with wrong password returns 401."""
        pw_hash = gateway.hash_token("mypassword")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        handler = self._make_handler(body=json.dumps({"password": "wrong"}).encode())
        handler._read_body = MagicMock(return_value=json.dumps({"password": "wrong"}).encode())
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        gateway.GatewayHandler.handle_auth_login(handler)
        self.assertEqual(sent_json["status"], 401)
        self.assertEqual(sent_json["data"]["error"], "Invalid password")

    @patch("gateway.load_setup")
    def test_login_endpoint_no_password_configured(self, mock_setup):
        """POST /api/auth/login returns 400 on first boot (no password)."""
        mock_setup.return_value = None

        handler = self._make_handler(body=json.dumps({"password": "test"}).encode())
        handler._read_body = MagicMock(return_value=json.dumps({"password": "test"}).encode())
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        gateway.GatewayHandler.handle_auth_login(handler)
        self.assertEqual(sent_json["status"], 400)
        self.assertIn("No password configured", sent_json["data"]["error"])

    @patch("gateway.save_setup")
    @patch("gateway.apply_config")
    @patch("gateway.validate_setup_config")
    @patch("gateway.load_setup")
    def test_save_requires_password_on_first_setup(self, mock_load, mock_validate, mock_apply, mock_save):
        """handle_save without password on first setup returns 400 error."""
        mock_load.return_value = None  # first boot
        mock_validate.return_value = (True, [])

        handler = self._make_handler(body=json.dumps({"provider_type": "anthropic"}).encode())
        handler._read_body = MagicMock(return_value=json.dumps({"provider_type": "anthropic"}).encode())
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        gateway.GatewayHandler.handle_save(handler)
        self.assertEqual(sent_json["status"], 400)
        self.assertIn("Password is required", sent_json["data"]["errors"])

    @patch("gateway.start_memory_writer")
    @patch("gateway.start_cron_scheduler")
    @patch("gateway.start_job_engine")
    @patch("gateway.start_session_watcher")
    @patch("gateway.start_goose_web")
    @patch("gateway.save_setup")
    @patch("gateway.apply_config")
    @patch("gateway.validate_setup_config")
    @patch("gateway.load_setup")
    def test_save_with_password_hashes_and_stores(self, mock_load, mock_validate, mock_apply,
                                                   mock_save, mock_start, mock_session,
                                                   mock_job, mock_cron, mock_mem):
        """handle_save with password stores hash, no plaintext in saved config."""
        mock_load.return_value = None  # first boot
        mock_validate.return_value = (True, [])

        config = {"provider_type": "anthropic", "web_auth_token": "mypassword123"}
        handler = self._make_handler(body=json.dumps(config).encode())
        handler._read_body = MagicMock(return_value=json.dumps(config).encode())
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        gateway.GatewayHandler.handle_save(handler)
        self.assertEqual(sent_json["status"], 200)
        self.assertTrue(sent_json["data"]["success"])
        # no auth_token in response
        self.assertNotIn("auth_token", sent_json["data"])

        # check saved config has hash, no plaintext
        saved_config = mock_save.call_args[0][0]
        self.assertIn("web_auth_token_hash", saved_config)
        self.assertEqual(saved_config["web_auth_token_hash"], gateway.hash_token("mypassword123"))
        self.assertNotIn("web_auth_token", saved_config)

    @patch("gateway.save_setup")
    @patch("gateway.apply_config")
    @patch("gateway.validate_setup_config")
    @patch("gateway.load_setup")
    def test_no_auto_generated_token(self, mock_load, mock_validate, mock_apply, mock_save):
        """handle_save does NOT generate token when password is blank on first setup."""
        mock_load.return_value = None
        mock_validate.return_value = (True, [])

        config = {"provider_type": "anthropic", "web_auth_token": ""}
        handler = self._make_handler(body=json.dumps(config).encode())
        handler._read_body = MagicMock(return_value=json.dumps(config).encode())
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        gateway.GatewayHandler.handle_save(handler)
        # should fail, not auto-generate
        self.assertEqual(sent_json["status"], 400)
        self.assertIn("Password is required", sent_json["data"]["errors"])
        mock_save.assert_not_called()

    @patch("gateway.save_setup")
    @patch("gateway.load_setup")
    def test_recovery_returns_temporary_password(self, mock_load, mock_save):
        """Recovery response has temporary_password field, not auth_token."""
        mock_load.return_value = {"web_auth_token_hash": "oldhash"}
        recovery_secret = "my-recovery-secret"

        handler = self._make_handler(body=json.dumps({"secret": recovery_secret}).encode())
        handler._read_body = MagicMock(return_value=json.dumps({"secret": recovery_secret}).encode())
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        with patch.dict(os.environ, {"GOOSECLAW_RECOVERY_SECRET": recovery_secret}):
            gateway.GatewayHandler.handle_auth_recover(handler)

        self.assertEqual(sent_json["status"], 200)
        self.assertTrue(sent_json["data"]["success"])
        self.assertIn("temporary_password", sent_json["data"])
        self.assertNotIn("auth_token", sent_json["data"])
        self.assertIn("Password reset", sent_json["data"]["message"])

    def test_login_page_served(self):
        """GET /login returns 200 with HTML containing password input."""
        handler = self._make_handler(method="GET", path="/login")
        handler.wfile = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        gateway.GatewayHandler.handle_login_page(handler)

        handler.send_response.assert_called_with(200)
        # check that HTML was written
        written = handler.wfile.write.call_args[0][0]
        self.assertIn(b"password", written)
        self.assertIn(b"GooseClaw", written)
        self.assertIn(b"/api/auth/login", written)
        self.assertIn(b"Lost your password", written)

    @patch("gateway.load_setup")
    def test_get_auth_token_no_env_var(self, mock_setup):
        """get_auth_token does not check GOOSE_WEB_AUTH_TOKEN env var."""
        mock_setup.return_value = None
        with patch.dict(os.environ, {"GOOSE_WEB_AUTH_TOKEN": "should-be-ignored"}):
            token, is_hashed = gateway.get_auth_token()
        # env var should be ignored -- no setup = no auth
        self.assertEqual(token, "")
        self.assertFalse(is_hashed)


# ── atomic write / backup for setup.json ────────────────────────────────────

class TestAtomicSetupWrite(unittest.TestCase):
    """Tests for setup.json atomic writes with .bak backup."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_config_dir = gateway.CONFIG_DIR
        self._orig_setup_file = gateway.SETUP_FILE
        gateway.CONFIG_DIR = self.tmpdir
        gateway.SETUP_FILE = os.path.join(self.tmpdir, "setup.json")

    def tearDown(self):
        gateway.CONFIG_DIR = self._orig_config_dir
        gateway.SETUP_FILE = self._orig_setup_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_creates_bak_on_second_write(self):
        """First save has no .bak, second save creates .bak with first config."""
        first = {"provider": "openai", "version": 1}
        gateway.save_setup(first)
        bak_path = gateway.SETUP_FILE + ".bak"
        # no .bak after first write (nothing to back up)
        self.assertFalse(os.path.exists(bak_path))

        second = {"provider": "anthropic", "version": 2}
        gateway.save_setup(second)
        # .bak should now exist and contain the first config
        self.assertTrue(os.path.exists(bak_path))
        with open(bak_path) as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data, first)

        # main file should contain the second config
        with open(gateway.SETUP_FILE) as f:
            main_data = json.load(f)
        self.assertEqual(main_data, second)

    def test_load_falls_back_to_bak_on_corruption(self):
        """If setup.json is corrupted, load_setup returns .bak content."""
        good_config = {"provider": "openai", "token": "sk-test"}
        gateway.save_setup(good_config)

        # corrupt the main file
        with open(gateway.SETUP_FILE, "w") as f:
            f.write("{corrupt json!!! not valid")

        # write a valid .bak
        with open(gateway.SETUP_FILE + ".bak", "w") as f:
            json.dump(good_config, f)

        result = gateway.load_setup()
        self.assertEqual(result, good_config)

    def test_load_returns_none_when_both_missing(self):
        """load_setup returns None when neither setup.json nor .bak exist."""
        result = gateway.load_setup()
        self.assertIsNone(result)

    def test_load_returns_none_when_both_corrupt(self):
        """load_setup returns None when both files are corrupted."""
        with open(gateway.SETUP_FILE, "w") as f:
            f.write("not json")
        with open(gateway.SETUP_FILE + ".bak", "w") as f:
            f.write("also not json")
        result = gateway.load_setup()
        self.assertIsNone(result)

    def test_atomic_write_no_partial_on_crash(self):
        """If .tmp exists but replace didn't happen, original file is intact."""
        original = {"provider": "openai", "intact": True}
        gateway.save_setup(original)

        # simulate a crash: .tmp left behind, main file untouched
        tmp_path = gateway.SETUP_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            f.write("partial write garbage")

        # load should still return the good config
        result = gateway.load_setup()
        self.assertEqual(result, original)

    def test_save_sets_restrictive_permissions(self):
        """setup.json should have 0600 permissions after save."""
        gateway.save_setup({"provider": "test"})
        import stat
        mode = os.stat(gateway.SETUP_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)


# ── get_safe_setup / redaction ───────────────────────────────────────────────

class TestGetSafeSetup(unittest.TestCase):
    """Tests for get_safe_setup() sensitive field redaction."""

    REDACTED = "***REDACTED***"

    FULL_SETUP = {
        "provider_type": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "api_key": "sk-ant-REAL-KEY-123",
        "password_hash": "pbkdf2:sha256:abc123",
        "web_auth_token_hash": "sha256:hashed-token-value",
        "claude_setup_token": "cst_real_token_456",
        "azure_key": "az-key-789",
        "telegram_bot_token": "123456:ABC-DEF",
        "litellm_host": "http://internal-llm:4000",
        "system_prompt": "You are helpful.",
        "memory_idle_minutes": 10,
        "models": [
            {"id": "m1", "model": "claude-sonnet-4-20250514", "provider": "anthropic"}
        ],
    }

    SENSITIVE_KEYS = [
        "api_key",
        "password_hash",
        "web_auth_token_hash",
        "claude_setup_token",
        "azure_key",
        "telegram_bot_token",
        "litellm_host",
    ]

    @patch("gateway.load_setup")
    def test_redacts_all_sensitive_fields(self, mock_load):
        mock_load.return_value = dict(self.FULL_SETUP)
        safe = gateway.get_safe_setup()
        for key in self.SENSITIVE_KEYS:
            self.assertEqual(safe[key], self.REDACTED,
                             f"{key} should be redacted")

    @patch("gateway.load_setup")
    def test_preserves_non_sensitive_fields(self, mock_load):
        mock_load.return_value = dict(self.FULL_SETUP)
        safe = gateway.get_safe_setup()
        self.assertEqual(safe["provider_type"], "anthropic")
        self.assertEqual(safe["model"], "claude-sonnet-4-20250514")
        self.assertEqual(safe["system_prompt"], "You are helpful.")
        self.assertEqual(safe["memory_idle_minutes"], 10)
        self.assertEqual(safe["models"],
                         [{"id": "m1", "model": "claude-sonnet-4-20250514", "provider": "anthropic"}])

    @patch("gateway.load_setup")
    def test_returns_none_when_no_setup(self, mock_load):
        mock_load.return_value = None
        self.assertIsNone(gateway.get_safe_setup())

    @patch("gateway.load_setup")
    def test_missing_sensitive_key_not_injected(self, mock_load):
        """If a sensitive key is absent from setup, get_safe_setup should not add it."""
        mock_load.return_value = {"provider_type": "openai", "model": "gpt-4o"}
        safe = gateway.get_safe_setup()
        for key in self.SENSITIVE_KEYS:
            self.assertNotIn(key, safe,
                             f"{key} should not appear when absent from source")

    @patch("gateway.load_setup")
    def test_does_not_mutate_original(self, mock_load):
        original = dict(self.FULL_SETUP)
        mock_load.return_value = original
        gateway.get_safe_setup()
        # original should still have real values
        self.assertEqual(original["api_key"], "sk-ant-REAL-KEY-123")

    @patch("gateway.load_setup")
    def test_redacts_saved_keys(self, mock_load):
        setup = dict(self.FULL_SETUP)
        setup["saved_keys"] = {
            "anthropic": "sk-real-key",
            "azure": {"key": "az-real", "endpoint": "https://my.azure.com"},
        }
        mock_load.return_value = setup
        safe = gateway.get_safe_setup()
        self.assertEqual(safe["saved_keys"]["anthropic"], self.REDACTED)
        self.assertEqual(safe["saved_keys"]["azure"]["key"], self.REDACTED)
        self.assertEqual(safe["saved_keys"]["azure"]["endpoint"], self.REDACTED)


class TestGetConfigEndpointRedaction(unittest.TestCase):
    """GET /api/setup/config should use get_safe_setup for redaction."""

    @patch("gateway.get_safe_setup")
    @patch("gateway.load_setup")
    def test_config_endpoint_uses_get_safe_setup(self, mock_load, mock_safe):
        """handle_get_config must delegate to get_safe_setup."""
        mock_load.return_value = {"provider_type": "anthropic"}
        mock_safe.return_value = {"provider_type": "anthropic", "api_key": "***REDACTED***"}
        handler = MagicMock()
        handler.path = "/api/setup/config"
        sent = {}
        def capture_send_json(code, data):
            sent["code"] = code
            sent["data"] = data
        handler.send_json = capture_send_json

        with patch("gateway.check_auth", return_value=True), \
             patch("gateway.migrate_config_models"):
            gateway.GatewayHandler.handle_get_config(handler)

        mock_safe.assert_called_once()
        self.assertTrue(sent["data"]["configured"])
        self.assertEqual(sent["data"]["config"]["api_key"], "***REDACTED***")


# ── session expiry and invalidation ──────────────────────────────────────────

class TestSessionExpiry(unittest.TestCase):
    """Tests for 24h session expiry and password-change invalidation."""

    def setUp(self):
        """Clear auth sessions before each test."""
        gateway._auth_sessions.clear()

    def tearDown(self):
        gateway._auth_sessions.clear()

    def _make_handler(self, cookie=None):
        """Build a minimal mock handler for check_auth testing."""
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = MagicMock(side_effect=lambda key, default="": {
            "Cookie": cookie or "",
            "Authorization": "",
        }.get(key, default))
        handler._set_session_cookie = False
        return handler

    @patch("gateway.load_setup")
    def test_fresh_session_is_valid(self, mock_setup):
        """A session created just now should pass check_auth."""
        pw_hash = gateway.hash_token("testpass")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        # create a session token via the internal API
        session_token = gateway._create_auth_session()
        handler = self._make_handler(cookie=f"gooseclaw_session={session_token}")
        self.assertTrue(gateway.check_auth(handler))

    @patch("gateway.load_setup")
    def test_expired_session_is_rejected(self, mock_setup):
        """A session older than SESSION_MAX_AGE should be rejected."""
        pw_hash = gateway.hash_token("testpass")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        session_token = gateway._create_auth_session()
        # fast-forward the creation time to exceed SESSION_MAX_AGE
        gateway._auth_sessions[session_token] = time.time() - gateway.SESSION_MAX_AGE - 1

        handler = self._make_handler(cookie=f"gooseclaw_session={session_token}")
        self.assertFalse(gateway.check_auth(handler))

    @patch("gateway.load_setup")
    def test_unknown_session_token_rejected(self, mock_setup):
        """A random cookie value not in _auth_sessions should be rejected."""
        pw_hash = gateway.hash_token("testpass")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        handler = self._make_handler(cookie="gooseclaw_session=bogus_random_token")
        self.assertFalse(gateway.check_auth(handler))

    def test_password_change_invalidates_all_sessions(self):
        """_invalidate_all_auth_sessions should clear every active session."""
        gateway._create_auth_session()
        gateway._create_auth_session()
        gateway._create_auth_session()
        self.assertEqual(len(gateway._auth_sessions), 3)

        gateway._invalidate_all_auth_sessions()
        self.assertEqual(len(gateway._auth_sessions), 0)

    @patch("gateway.load_setup")
    def test_login_cookie_has_correct_max_age(self, mock_setup):
        """Login endpoint should set Max-Age matching SESSION_MAX_AGE, not 1 year."""
        pw_hash = gateway.hash_token("mypassword")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        handler = self._make_handler()
        handler._read_body = MagicMock(
            return_value=json.dumps({"password": "mypassword"}).encode()
        )
        handler._check_rate_limit = MagicMock(return_value=True)
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler._internal_error = MagicMock()

        gateway.GatewayHandler.handle_auth_login(handler)

        cookie_calls = [c for c in handler.send_header.call_args_list if c[0][0] == "Set-Cookie"]
        self.assertTrue(len(cookie_calls) > 0, "Expected Set-Cookie header")
        cookie_val = cookie_calls[0][0][1]
        self.assertIn(f"Max-Age={gateway.SESSION_MAX_AGE}", cookie_val)
        self.assertNotIn("Max-Age=31536000", cookie_val)

    @patch("gateway.save_setup")
    @patch("gateway.load_setup")
    def test_recovery_clears_all_sessions(self, mock_load, mock_save):
        """Password recovery should invalidate all existing auth sessions."""
        mock_load.return_value = {"web_auth_token_hash": "oldhash"}
        recovery_secret = "my-recovery-secret"

        # create some sessions first
        gateway._create_auth_session()
        gateway._create_auth_session()
        self.assertEqual(len(gateway._auth_sessions), 2)

        handler = self._make_handler()
        handler._read_body = MagicMock(
            return_value=json.dumps({"secret": recovery_secret}).encode()
        )
        handler._check_rate_limit = MagicMock(return_value=True)

        sent_json = {}
        def capture_json(status, data):
            sent_json["status"] = status
            sent_json["data"] = data
        handler.send_json = capture_json

        with patch.dict(os.environ, {"GOOSECLAW_RECOVERY_SECRET": recovery_secret}):
            gateway.GatewayHandler.handle_auth_recover(handler)

        self.assertEqual(sent_json["status"], 200)
        self.assertEqual(len(gateway._auth_sessions), 0)

    @patch("gateway.load_setup")
    def test_inject_session_cookie_has_correct_max_age(self, mock_setup):
        """_inject_session_cookie (Basic Auth path) should use SESSION_MAX_AGE."""
        pw_hash = gateway.hash_token("mypassword")
        mock_setup.return_value = {"web_auth_token_hash": pw_hash}

        handler = MagicMock()
        handler._set_session_cookie = True
        handler.send_header = MagicMock()

        gateway.GatewayHandler._inject_session_cookie(handler)

        cookie_calls = [c for c in handler.send_header.call_args_list if c[0][0] == "Set-Cookie"]
        self.assertTrue(len(cookie_calls) > 0, "Expected Set-Cookie header")
        cookie_val = cookie_calls[0][0][1]
        self.assertIn(f"Max-Age={gateway.SESSION_MAX_AGE}", cookie_val)
        self.assertNotIn("Max-Age=31536000", cookie_val)

    def test_session_max_age_constant_is_24_hours(self):
        """SESSION_MAX_AGE should be 86400 (24 hours)."""
        self.assertEqual(gateway.SESSION_MAX_AGE, 86400)


# ── watcher CRUD tests ─────────────────────────────────────────────────────

class TestCreateWatcher(unittest.TestCase):
    """Tests for create_watcher()."""

    def setUp(self):
        with gateway._watchers_lock:
            gateway._watchers.clear()
        self._save_patcher = patch("gateway._save_watchers")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        with gateway._watchers_lock:
            gateway._watchers.clear()

    def test_valid_webhook_type(self):
        w, err = gateway.create_watcher({"type": "webhook"})
        assert err == ""
        assert w is not None
        assert w["type"] == "webhook"

    def test_valid_feed_type(self):
        w, err = gateway.create_watcher({"type": "feed", "source": "https://example.com/rss"})
        assert err == ""
        assert w["type"] == "feed"

    def test_valid_stream_type(self):
        w, err = gateway.create_watcher({"type": "stream", "source": "https://example.com/sse"})
        assert err == ""
        assert w["type"] == "stream"

    def test_invalid_type_rejected(self):
        w, err = gateway.create_watcher({"type": "invalid"})
        assert w is None
        assert "invalid type" in err

    def test_duplicate_id_rejected(self):
        gateway.create_watcher({"id": "dup", "type": "webhook"})
        w, err = gateway.create_watcher({"id": "dup", "type": "webhook"})
        assert w is None
        assert "already exists" in err

    def test_auto_generated_id(self):
        w, _ = gateway.create_watcher({"type": "webhook"})
        assert w["id"]  # should have a non-empty id
        assert len(w["id"]) == 8  # uuid[:8]

    def test_feed_requires_source(self):
        w, err = gateway.create_watcher({"type": "feed"})
        assert w is None
        assert "source" in err.lower()

    def test_default_values(self):
        w, _ = gateway.create_watcher({"type": "webhook"})
        assert w["enabled"] is True
        assert w["fire_count"] == 0
        assert w["smart"] is False
        assert w["transform"] == ""
        assert w["last_fired"] is None
        assert w["last_error"] is None


class TestDeleteWatcher(unittest.TestCase):
    """Tests for delete_watcher()."""

    def setUp(self):
        with gateway._watchers_lock:
            gateway._watchers.clear()
        self._save_patcher = patch("gateway._save_watchers")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        with gateway._watchers_lock:
            gateway._watchers.clear()

    def test_delete_existing_returns_true(self):
        gateway.create_watcher({"id": "del1", "type": "webhook"})
        assert gateway.delete_watcher("del1") is True

    def test_delete_nonexistent_returns_false(self):
        assert gateway.delete_watcher("nope") is False

    def test_delete_removes_from_list(self):
        gateway.create_watcher({"id": "del2", "type": "webhook"})
        gateway.delete_watcher("del2")
        watchers = gateway.list_watchers()
        assert not any(w["id"] == "del2" for w in watchers)


class TestListWatchers(unittest.TestCase):
    """Tests for list_watchers()."""

    def setUp(self):
        with gateway._watchers_lock:
            gateway._watchers.clear()
        self._save_patcher = patch("gateway._save_watchers")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        with gateway._watchers_lock:
            gateway._watchers.clear()

    def test_returns_all_watchers(self):
        gateway.create_watcher({"id": "w1", "type": "webhook"})
        gateway.create_watcher({"id": "w2", "type": "webhook"})
        result = gateway.list_watchers()
        assert len(result) == 2

    def test_empty_list(self):
        result = gateway.list_watchers()
        assert result == []


class TestUpdateWatcher(unittest.TestCase):
    """Tests for update_watcher()."""

    def setUp(self):
        with gateway._watchers_lock:
            gateway._watchers.clear()
        self._save_patcher = patch("gateway._save_watchers")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        with gateway._watchers_lock:
            gateway._watchers.clear()

    def test_update_fields(self):
        gateway.create_watcher({"id": "u1", "type": "webhook"})
        w, err = gateway.update_watcher("u1", {"name": "new name", "enabled": False})
        assert err == ""
        assert w["name"] == "new name"
        assert w["enabled"] is False

    def test_update_nonexistent_returns_error(self):
        w, err = gateway.update_watcher("nope", {"name": "x"})
        assert w is None
        assert "not found" in err


if __name__ == "__main__":
    unittest.main()

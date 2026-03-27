"""Voice session token, Gemini config builder, audio transcoding, and message parser tests (Phase 28-01).

Unit tests: voice session tokens (create/validate/expire), Gemini Live API config builder,
PCM-to-JSON audio transcoding, server message classification.
"""

import base64
import os
import sys
import time
import unittest.mock

import pytest

# Add docker/ to sys.path so gateway can be imported
docker_dir = os.path.join(os.path.dirname(__file__), "..")
if docker_dir not in sys.path:
    sys.path.insert(0, docker_dir)

from gateway import (
    _voice_session_token_create,
    _voice_session_token_validate,
    _VOICE_TOKEN_TTL,
    _gemini_build_config,
    _voice_pcm_to_gemini_json,
    _voice_extract_audio_chunks,
    _voice_parse_server_message,
    GEMINI_LIVE_HOST,
    GEMINI_LIVE_PATH,
    _discover_voice_tools,
    _voice_execute_tool,
    _voice_build_tool_response,
)


class TestVoiceSessionToken:
    """Voice session token create/validate/expire."""

    def test_create_returns_string(self):
        token = _voice_session_token_create("fake-api-key")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_validate_returns_api_key(self):
        token = _voice_session_token_create("test-key-123")
        result = _voice_session_token_validate(token)
        assert result == "test-key-123"

    def test_invalid_token_returns_none(self):
        result = _voice_session_token_validate("nonexistent")
        assert result is None

    def test_expired_token_returns_none(self):
        token = _voice_session_token_create("expire-me")
        future = time.time() + _VOICE_TOKEN_TTL + 10
        with unittest.mock.patch("gateway.time") as mock_time:
            mock_time.time.return_value = future
            result = _voice_session_token_validate(token)
        assert result is None

    def test_cleanup_removes_expired(self):
        t1 = _voice_session_token_create("key1")
        t2 = _voice_session_token_create("key2")
        t3 = _voice_session_token_create("key3")
        future = time.time() + _VOICE_TOKEN_TTL + 10
        with unittest.mock.patch("gateway.time") as mock_time:
            mock_time.time.return_value = future
            # creating a new token triggers cleanup of expired ones
            _t4 = _voice_session_token_create("key4")
        # old tokens should be cleaned up
        assert _voice_session_token_validate(t1) is None
        assert _voice_session_token_validate(t2) is None
        assert _voice_session_token_validate(t3) is None


class TestGeminiBuildConfig:
    """Gemini Live API setup config builder."""

    def test_config_has_model(self):
        cfg = _gemini_build_config()
        assert cfg["config"]["model"] == "models/gemini-3.1-flash-live-preview"

    def test_config_has_audio_response(self):
        cfg = _gemini_build_config()
        assert "AUDIO" in cfg["config"]["generationConfig"]["responseModalities"]

    def test_config_has_compression(self):
        cfg = _gemini_build_config()
        assert isinstance(cfg["config"]["contextWindowCompression"]["slidingWindow"], dict)

    def test_config_has_session_resumption(self):
        cfg = _gemini_build_config()
        assert "sessionResumption" in cfg["config"]
        assert "handle" in cfg["config"]["sessionResumption"]

    def test_config_with_resumption_handle(self):
        cfg = _gemini_build_config(resumption_handle="abc123")
        assert cfg["config"]["sessionResumption"]["handle"] == "abc123"

    def test_config_has_transcription(self):
        cfg = _gemini_build_config()
        assert "inputAudioTranscription" in cfg["config"]
        assert "outputAudioTranscription" in cfg["config"]

    def test_config_has_system_instruction(self):
        cfg = _gemini_build_config()
        parts = cfg["config"]["systemInstruction"]["parts"]
        assert isinstance(parts, list)
        assert len(parts) >= 1
        assert "text" in parts[0]

    def test_config_includes_tools_when_provided(self):
        tools = [{"name": "test", "description": "test tool", "parameters": {"type": "OBJECT", "properties": {}}}]
        cfg = _gemini_build_config(tools=tools)
        assert "tools" in cfg["config"]
        assert cfg["config"]["tools"] == [{"functionDeclarations": tools}]

    def test_config_no_tools_when_none(self):
        cfg = _gemini_build_config()
        assert "tools" not in cfg["config"]


class TestAudioTranscoding:
    """PCM binary <-> Gemini JSON audio transcoding."""

    def test_pcm_to_gemini_json(self):
        pcm = b"\x00\x01\x02\x03"
        result = _voice_pcm_to_gemini_json(pcm)
        assert result["realtimeInput"]["audio"]["data"] == base64.b64encode(pcm).decode()
        assert result["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"

    def test_gemini_json_to_pcm(self):
        raw_audio = b"\x10\x20\x30\x40"
        msg = {
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {
                            "inlineData": {
                                "data": base64.b64encode(raw_audio).decode(),
                                "mimeType": "audio/pcm;rate=24000",
                            }
                        }
                    ]
                }
            }
        }
        chunks = _voice_extract_audio_chunks(msg)
        assert len(chunks) == 1
        assert chunks[0] == raw_audio

    def test_gemini_json_no_audio(self):
        msg = {
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"text": "Hello there"}
                    ]
                }
            }
        }
        chunks = _voice_extract_audio_chunks(msg)
        assert chunks == []


class TestVoiceParseServerMessage:
    """Gemini Live API server message classifier."""

    def test_setup_complete(self):
        result = _voice_parse_server_message({"setupComplete": {}})
        assert result == {"type": "ready"}

    def test_session_resumption_update(self):
        msg = {"sessionResumptionUpdate": {"newHandle": "handle-xyz"}}
        result = _voice_parse_server_message(msg)
        assert result == {"type": "resumption_update", "handle": "handle-xyz"}

    def test_goaway(self):
        result = _voice_parse_server_message({"goAway": {}})
        assert result == {"type": "goaway"}

    def test_output_transcription(self):
        msg = {"serverContent": {"outputTranscription": {"text": "Hello world"}}}
        result = _voice_parse_server_message(msg)
        assert result == {"type": "transcript", "speaker": "ai", "text": "Hello world"}

    def test_input_transcription(self):
        msg = {"serverContent": {"inputTranscription": {"text": "Hi there"}}}
        result = _voice_parse_server_message(msg)
        assert result == {"type": "transcript", "speaker": "user", "text": "Hi there"}

    def test_interrupted(self):
        msg = {"serverContent": {"interrupted": True}}
        result = _voice_parse_server_message(msg)
        assert result == {"type": "interrupted"}

    def test_tool_call(self):
        tool_data = {"functionCalls": [{"name": "search", "args": {}}]}
        msg = {"toolCall": tool_data}
        result = _voice_parse_server_message(msg)
        assert result == {"type": "tool_call", "data": tool_data}

    def test_unknown_message(self):
        result = _voice_parse_server_message({"somethingWeird": True})
        assert result is None


# ── Integration tests (Phase 28-02) ────────────────────────────────────────

import json
import socket
import threading
import urllib.request

from gateway import (
    _get_gemini_api_key,
    _voice_session_token_validate,
    ws_accept_key,
    ws_recv_frame,
    ws_send_frame,
    ws_send_close,
    WS_OP_TEXT,
    WS_OP_CLOSE,
)


def _ws_upgrade(sock, host, path="/ws/voice", token=None):
    """Send HTTP upgrade request to WebSocket endpoint. Returns (key, response_bytes)."""
    url = path
    if token:
        url = f"{path}?token={token}"
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    sock.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    return key, response


class TestVoiceTokenEndpoint:
    """Integration tests for GET /api/voice/token."""

    def test_token_endpoint_returns_token(self, live_gateway, gateway_module):
        """With Gemini key in vault, GET /api/voice/token returns 200 with token."""
        gw = gateway_module
        with unittest.mock.patch.object(gw, "_get_gemini_api_key", return_value="test-gemini-key"):
            # also need to patch module-level reference
            with unittest.mock.patch("gateway._get_gemini_api_key", return_value="test-gemini-key"):
                req = urllib.request.Request(f"{live_gateway}/api/voice/token")
                resp = urllib.request.urlopen(req, timeout=5)
                assert resp.status == 200
                data = json.loads(resp.read())
                assert "token" in data
                assert len(data["token"]) > 0
                # verify the token is actually valid
                api_key = _voice_session_token_validate(data["token"])
                assert api_key == "test-gemini-key"

    def test_token_endpoint_no_gemini_key(self, live_gateway, gateway_module):
        """Without Gemini key, GET /api/voice/token returns 503."""
        gw = gateway_module
        with unittest.mock.patch("gateway._get_gemini_api_key", return_value=None):
            req = urllib.request.Request(f"{live_gateway}/api/voice/token")
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "expected HTTP error"
            except urllib.error.HTTPError as e:
                assert e.code == 503


class TestWsVoiceAuth:
    """Integration tests for WebSocket auth gating on /ws/voice."""

    def test_ws_voice_no_token_rejected(self, live_gateway):
        """Connect to /ws/voice without token, expect 403."""
        port = int(live_gateway.split(":")[-1])
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            key, response = _ws_upgrade(sock, f"127.0.0.1:{port}")
            response_str = response.decode()
            assert "403" in response_str.split("\r\n")[0]
        finally:
            sock.close()

    def test_ws_voice_invalid_token_rejected(self, live_gateway):
        """Connect to /ws/voice with bogus token, expect 403."""
        port = int(live_gateway.split(":")[-1])
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            key, response = _ws_upgrade(sock, f"127.0.0.1:{port}", token="bogus")
            response_str = response.decode()
            assert "403" in response_str.split("\r\n")[0]
        finally:
            sock.close()

    def test_ws_voice_valid_token_accepted(self, live_gateway):
        """Connect to /ws/voice with valid token, expect 101 upgrade."""
        port = int(live_gateway.split(":")[-1])
        token = _voice_session_token_create("fake-api-key")
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            with unittest.mock.patch("gateway._gemini_connect", side_effect=ConnectionError("test")):
                key, response = _ws_upgrade(sock, f"127.0.0.1:{port}", token=token)
                response_str = response.decode()
                # should get 101 Switching Protocols (token is valid)
                assert "101" in response_str.split("\r\n")[0]
                # connection will close shortly because _gemini_connect fails,
                # but the 101 proves token auth works
        finally:
            sock.close()


# ── Vault write + voice page gating tests (Phase 29-01 RED) ─────────────────

import tempfile
import yaml
import requests

# Attempt to import _save_vault_key; it won't exist until Plan 29-02 implements it.
try:
    from gateway import _save_vault_key
except ImportError:
    _save_vault_key = None


class TestVaultWrite:
    """Tests for _save_vault_key vault write helper (SETUP-04)."""

    @pytest.fixture(autouse=True)
    def _patch_vault(self, gateway_module, tmp_path):
        """Patch gateway.VAULT_FILE to a temp path for each test."""
        self.vault_path = str(tmp_path / "secrets" / "vault.yaml")
        self._orig_vault = gateway_module.VAULT_FILE
        gateway_module.VAULT_FILE = self.vault_path
        yield
        gateway_module.VAULT_FILE = self._orig_vault

    def test_save_and_read_roundtrip(self, gateway_module):
        """Save a key to vault and read it back."""
        if _save_vault_key is None:
            pytest.skip("_save_vault_key not implemented yet")
        _save_vault_key("GEMINI_API_KEY", "test-key-123")
        result = _get_gemini_api_key()
        assert result == "test-key-123"

    def test_save_overwrites_existing(self, gateway_module):
        """Saving a key twice overwrites the old value."""
        if _save_vault_key is None:
            pytest.skip("_save_vault_key not implemented yet")
        _save_vault_key("GEMINI_API_KEY", "old")
        _save_vault_key("GEMINI_API_KEY", "new")
        result = _get_gemini_api_key()
        assert result == "new"

    def test_save_preserves_other_keys(self, gateway_module):
        """Writing one key preserves other existing keys in vault.yaml."""
        if _save_vault_key is None:
            pytest.skip("_save_vault_key not implemented yet")
        # pre-create vault with another key
        os.makedirs(os.path.dirname(self.vault_path), exist_ok=True)
        with open(self.vault_path, "w") as f:
            yaml.dump({"OTHER_KEY": "foo"}, f)
        _save_vault_key("GEMINI_API_KEY", "bar")
        with open(self.vault_path) as f:
            data = yaml.safe_load(f)
        assert data["OTHER_KEY"] == "foo"
        assert data["GEMINI_API_KEY"] == "bar"

    def test_save_creates_directory(self, gateway_module, tmp_path):
        """_save_vault_key creates parent directories if they don't exist."""
        if _save_vault_key is None:
            pytest.skip("_save_vault_key not implemented yet")
        deep_path = str(tmp_path / "deep" / "nested" / "vault.yaml")
        gateway_module.VAULT_FILE = deep_path
        _save_vault_key("GEMINI_API_KEY", "deep-key")
        assert os.path.exists(deep_path)
        with open(deep_path) as f:
            data = yaml.safe_load(f)
        assert data["GEMINI_API_KEY"] == "deep-key"


class TestVoicePageGating:
    """HTTP-level tests for /voice route gating (SETUP-02, UI-07)."""

    @pytest.fixture(autouse=True)
    def _patch_vault_file(self, gateway_module, live_gateway, tmp_path):
        """Patch VAULT_FILE to test data secrets dir for HTTP tests."""
        # The live_gateway fixture creates DATA_DIR but VAULT_FILE is computed
        # at import time. Patch it to the test data's secrets/vault.yaml.
        data_dir = gateway_module.DATA_DIR
        self.vault_path = os.path.join(data_dir, "secrets", "vault.yaml")
        self._orig_vault = gateway_module.VAULT_FILE
        gateway_module.VAULT_FILE = self.vault_path
        # Ensure secrets dir exists and vault is empty
        os.makedirs(os.path.dirname(self.vault_path), exist_ok=True)
        with open(self.vault_path, "w") as f:
            f.write("")
        yield
        gateway_module.VAULT_FILE = self._orig_vault

    def test_voice_requires_auth(self, live_gateway, gateway_module):
        """GET /voice without auth cookie should redirect to /login."""
        # Write setup so auth is required
        gw = gateway_module
        hashed = gw.hash_token("testpassword")
        setup = {"web_auth_token_hash": hashed, "setup_complete": True, "provider_type": "openai"}
        os.makedirs(os.path.dirname(gw.SETUP_FILE), exist_ok=True)
        with open(gw.SETUP_FILE, "w") as f:
            json.dump(setup, f)
        resp = requests.get(f"{live_gateway}/voice", allow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_voice_no_key_shows_gate(self, live_gateway, auth_session, gateway_module):
        """GET /voice with auth but no Gemini key shows gate page."""
        # Ensure vault has no GEMINI_API_KEY
        with open(self.vault_path, "w") as f:
            f.write("")
        resp = requests.get(
            f"{live_gateway}/voice",
            headers=auth_session,
            allow_redirects=False,
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Configure Gemini" in body or "/setup" in body

    def test_voice_with_key_serves_page(self, live_gateway, auth_session, gateway_module):
        """GET /voice with auth and Gemini key returns 200 with content."""
        # Write GEMINI_API_KEY to vault
        with open(self.vault_path, "w") as f:
            yaml.dump({"GEMINI_API_KEY": "test-gemini-key-abc"}, f)
        resp = requests.get(
            f"{live_gateway}/voice",
            headers=auth_session,
            allow_redirects=False,
        )
        assert resp.status_code == 200
        # Should serve voice.html or "coming soon" placeholder (voice.html may not exist)
        body = resp.text
        assert "Voice" in body or "voice" in body


# ── CSP and voice.html file tests (Phase 30-01) ─────────────────────────────


class TestVoiceCSP:
    """Test that CSP header includes worker-src blob: when serving voice.html."""

    @pytest.fixture(autouse=True)
    def _patch_vault_file(self, gateway_module, live_gateway, tmp_path):
        """Patch VAULT_FILE to test data secrets dir for HTTP tests."""
        data_dir = gateway_module.DATA_DIR
        self.vault_path = os.path.join(data_dir, "secrets", "vault.yaml")
        self._orig_vault = gateway_module.VAULT_FILE
        gateway_module.VAULT_FILE = self.vault_path
        os.makedirs(os.path.dirname(self.vault_path), exist_ok=True)
        yield
        gateway_module.VAULT_FILE = self._orig_vault

    def test_csp_includes_worker_src_blob(self, live_gateway, auth_session, gateway_module):
        """CSP header must include worker-src blob: for AudioWorklet support."""
        # Write Gemini key to vault so voice.html is served
        with open(self.vault_path, "w") as f:
            yaml.dump({"GEMINI_API_KEY": "test-csp-key"}, f)
        resp = requests.get(
            f"{live_gateway}/voice",
            headers=auth_session,
            allow_redirects=False,
        )
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "worker-src blob:" in csp, f"CSP missing worker-src blob:, got: {csp}"


class TestVoiceDashboardFile:
    """Test that voice.html exists and meets single-file constraints."""

    def test_voice_html_exists(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        assert os.path.exists(voice_path), "voice.html not found in docker/"

    def test_voice_html_valid_doctype(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            content = f.read()
        assert content.strip().startswith("<!DOCTYPE html>"), "voice.html must start with <!DOCTYPE html>"

    def test_voice_html_no_external_scripts(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            content = f.read()
        assert '<script src=' not in content, "voice.html must not have external script tags"

    def test_voice_html_no_external_styles(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            content = f.read()
        assert '<link rel="stylesheet" href=' not in content, "voice.html must not have external stylesheets"

    def test_voice_html_contains_state_machine(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            content = f.read()
        assert "STATE" in content, "voice.html must contain STATE machine"

    def test_voice_html_contains_websocket(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            content = f.read()
        assert "WebSocket" in content, "voice.html must reference WebSocket"

    def test_voice_html_contains_getusermedia(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            content = f.read()
        assert "getUserMedia" in content or "getusermedia" in content.lower(), \
            "voice.html must reference getUserMedia"


# ── Keyboard, text input, mobile, and wake lock tests (Phase 31-01) ──────────

import re


class TestKeyboardShortcuts:
    """Static analysis tests for keyboard shortcuts (UI-03)."""

    def _read_voice(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            return f.read()

    def test_has_keydown_listener(self):
        content = self._read_voice()
        assert re.search(r"addEventListener.*keydown|onkeydown", content), \
            "voice.html must have a keydown event listener"

    def test_has_keyup_listener(self):
        content = self._read_voice()
        assert re.search(r"addEventListener.*keyup|onkeyup", content), \
            "voice.html must have a keyup event listener"

    def test_has_space_code_check(self):
        content = self._read_voice()
        assert re.search(r"e\.code\s*===?\s*['\"]Space['\"]|code.*Space", content), \
            "voice.html must check for Space key code"

    def test_has_escape_code_check(self):
        content = self._read_voice()
        assert re.search(r"e\.code\s*===?\s*['\"]Escape['\"]|code.*Escape", content), \
            "voice.html must check for Escape key code"

    def test_has_repeat_guard(self):
        content = self._read_voice()
        assert re.search(r"e\.repeat|\.repeat", content), \
            "voice.html must guard against key repeat"

    def test_has_focus_guard(self):
        content = self._read_voice()
        assert re.search(r"INPUT|TEXTAREA|tagName|isContentEditable", content), \
            "voice.html must skip shortcuts when typing in input/textarea"


class TestTextInput:
    """Static analysis tests for text input bar (UI-04)."""

    def _read_voice(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            return f.read()

    def test_has_text_input_element(self):
        content = self._read_voice()
        assert re.search(r'id=["\']text-input["\']', content), \
            "voice.html must have an element with id='text-input'"

    def test_has_text_input_bar(self):
        content = self._read_voice()
        assert "text-input-bar" in content, \
            "voice.html must have text-input-bar"

    def test_has_send_text_function(self):
        content = self._read_voice()
        assert "sendTextMessage" in content, \
            "voice.html must have sendTextMessage function"

    def test_has_realtime_input_text(self):
        content = self._read_voice()
        assert "realtimeInput" in content and "text" in content, \
            "voice.html must use realtimeInput text format for Gemini"

    def test_has_enter_key_handler(self):
        content = self._read_voice()
        assert re.search(r"Enter", content), \
            "voice.html must handle Enter key for text submit"


class TestMobileLayout:
    """Static analysis tests for mobile-first responsive CSS (UI-05)."""

    def _read_voice(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            return f.read()

    def test_has_safe_area_inset(self):
        content = self._read_voice()
        assert "safe-area-inset" in content, \
            "voice.html must use safe-area-inset for notched phones"

    def test_has_min_tap_target_size(self):
        content = self._read_voice()
        assert re.search(r"44px|48px", content), \
            "voice.html must have minimum 44px tap targets"

    def test_has_font_size_16px_input(self):
        content = self._read_voice()
        assert re.search(r"font-size:\s*16px|font-size:16px", content), \
            "voice.html must use 16px font-size on inputs to prevent iOS zoom"

    def test_has_viewport_fit_cover(self):
        content = self._read_voice()
        assert "viewport-fit=cover" in content, \
            "voice.html must have viewport-fit=cover in viewport meta tag"


class TestWakeLock:
    """Static analysis tests for Screen Wake Lock API (UI-06)."""

    def _read_voice(self):
        voice_path = os.path.join(os.path.dirname(__file__), "..", "voice.html")
        with open(voice_path) as f:
            return f.read()

    def test_has_wake_lock_request(self):
        content = self._read_voice()
        assert "wakeLock" in content and "request" in content, \
            "voice.html must request wake lock"

    def test_has_wake_lock_release(self):
        content = self._read_voice()
        assert "wakeLock" in content and "release" in content, \
            "voice.html must release wake lock"

    def test_has_visibility_change_listener(self):
        content = self._read_voice()
        assert "visibilitychange" in content, \
            "voice.html must listen for visibilitychange events"

    def test_has_wake_lock_feature_detection(self):
        content = self._read_voice()
        assert re.search(r"['\"]wakeLock['\"]\s*in\s*navigator|wakeLock.*in.*navigator", content), \
            "voice.html must feature-detect wake lock support"


# ── Tool calling unit tests (Phase 32-01 RED) ────────────────────────────────


class TestToolDiscovery:
    """Tests for _discover_voice_tools() — queries goosed /config for MCP tools."""

    def test_returns_declarations_for_enabled_extensions(self):
        """Mock goosed /config with 2 enabled extensions, expect 2 declarations."""
        config_resp = json.dumps({
            "config": {
                "extensions": {
                    "google-calendar": {"type": "builtin", "enabled": True},
                    "mem0": {"type": "builtin", "enabled": True},
                }
            }
        }).encode()
        mock_conn = unittest.mock.MagicMock()
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = config_resp
        mock_conn.getresponse.return_value = mock_resp
        with unittest.mock.patch("gateway._goosed_conn", return_value=mock_conn):
            with unittest.mock.patch("gateway._INTERNAL_GOOSE_TOKEN", "fake-token"):
                declarations, name_map = _discover_voice_tools()
        assert len(declarations) == 2
        for decl in declarations:
            assert "name" in decl
            assert "description" in decl
            assert "parameters" in decl
            assert "request" in decl["parameters"]["properties"]

    def test_sanitizes_extension_names(self):
        """Extension 'google-calendar' should become 'google_calendar'."""
        config_resp = json.dumps({
            "config": {
                "extensions": {
                    "google-calendar": {"type": "builtin", "enabled": True},
                }
            }
        }).encode()
        mock_conn = unittest.mock.MagicMock()
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = config_resp
        mock_conn.getresponse.return_value = mock_resp
        with unittest.mock.patch("gateway._goosed_conn", return_value=mock_conn):
            with unittest.mock.patch("gateway._INTERNAL_GOOSE_TOKEN", "fake-token"):
                declarations, name_map = _discover_voice_tools()
        assert declarations[0]["name"] == "google_calendar"

    def test_returns_name_mapping(self):
        """name_map should map sanitized -> original name."""
        config_resp = json.dumps({
            "config": {
                "extensions": {
                    "google-calendar": {"type": "builtin", "enabled": True},
                }
            }
        }).encode()
        mock_conn = unittest.mock.MagicMock()
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = config_resp
        mock_conn.getresponse.return_value = mock_resp
        with unittest.mock.patch("gateway._goosed_conn", return_value=mock_conn):
            with unittest.mock.patch("gateway._INTERNAL_GOOSE_TOKEN", "fake-token"):
                declarations, name_map = _discover_voice_tools()
        assert name_map["google_calendar"] == "google-calendar"

    def test_skips_disabled_extensions(self):
        """Disabled extensions should not appear in declarations."""
        config_resp = json.dumps({
            "config": {
                "extensions": {
                    "google-calendar": {"type": "builtin", "enabled": True},
                    "slack": {"type": "builtin", "enabled": False},
                }
            }
        }).encode()
        mock_conn = unittest.mock.MagicMock()
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = config_resp
        mock_conn.getresponse.return_value = mock_resp
        with unittest.mock.patch("gateway._goosed_conn", return_value=mock_conn):
            with unittest.mock.patch("gateway._INTERNAL_GOOSE_TOKEN", "fake-token"):
                declarations, name_map = _discover_voice_tools()
        assert len(declarations) == 1
        assert declarations[0]["name"] == "google_calendar"

    def test_returns_empty_on_goosed_error(self):
        """ConnectionError from goosed should return ([], {})."""
        with unittest.mock.patch("gateway._goosed_conn", side_effect=ConnectionError("refused")):
            with unittest.mock.patch("gateway._INTERNAL_GOOSE_TOKEN", "fake-token"):
                declarations, name_map = _discover_voice_tools()
        assert declarations == []
        assert name_map == {}

    def test_returns_empty_on_non_200(self):
        """Non-200 from goosed /config should return ([], {})."""
        mock_conn = unittest.mock.MagicMock()
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status = 500
        mock_resp.read.return_value = b"error"
        mock_conn.getresponse.return_value = mock_resp
        with unittest.mock.patch("gateway._goosed_conn", return_value=mock_conn):
            with unittest.mock.patch("gateway._INTERNAL_GOOSE_TOKEN", "fake-token"):
                declarations, name_map = _discover_voice_tools()
        assert declarations == []
        assert name_map == {}


class TestToolExecution:
    """Tests for _voice_execute_tool() — relays tool requests through goosed."""

    def test_executes_via_do_rest_relay(self):
        """Successful relay returns result dict."""
        with unittest.mock.patch("gateway._do_rest_relay", return_value=("Calendar event found", "", [])):
            result = _voice_execute_tool("google_calendar", {"request": "check my schedule"}, "session-123", "google-calendar")
        assert result == {"result": "Calendar event found"}

    def test_truncates_long_results(self):
        """Results longer than 2000 chars should be truncated."""
        long_text = "x" * 5000
        with unittest.mock.patch("gateway._do_rest_relay", return_value=(long_text, "", [])):
            result = _voice_execute_tool("google_calendar", {"request": "check"}, "session-123", "google-calendar")
        assert len(result["result"]) <= 2000

    def test_returns_error_on_relay_failure(self):
        """Relay error string should be returned as error dict."""
        with unittest.mock.patch("gateway._do_rest_relay", return_value=("", "goosed timeout", [])):
            result = _voice_execute_tool("google_calendar", {"request": "check"}, "session-123", "google-calendar")
        assert result == {"error": "goosed timeout"}

    def test_returns_error_on_exception(self):
        """Exception during relay should return error dict."""
        with unittest.mock.patch("gateway._do_rest_relay", side_effect=Exception("boom")):
            result = _voice_execute_tool("google_calendar", {"request": "check"}, "session-123", "google-calendar")
        assert "error" in result


class TestToolResponse:
    """Tests for _voice_build_tool_response() — builds Gemini toolResponse JSON."""

    def test_builds_correct_json_structure(self):
        """Response must have toolResponse.functionResponses[0] with id, name, response."""
        resp = _voice_build_tool_response("call-123", "google_calendar", {"result": "Event at 3pm"})
        assert "toolResponse" in resp
        assert "functionResponses" in resp["toolResponse"]
        fr = resp["toolResponse"]["functionResponses"][0]
        assert "id" in fr
        assert "name" in fr
        assert "response" in fr

    def test_response_has_correct_fields(self):
        """Verify exact field values."""
        resp = _voice_build_tool_response("call-123", "google_calendar", {"result": "Event at 3pm"})
        fr = resp["toolResponse"]["functionResponses"][0]
        assert fr["id"] == "call-123"
        assert fr["name"] == "google_calendar"
        assert fr["response"] == {"result": "Event at 3pm"}

    def test_response_has_silent_scheduling(self):
        """SILENT scheduling prevents Gemini from double-speaking raw tool results."""
        resp = _voice_build_tool_response("call-123", "google_calendar", {"result": "Event at 3pm"})
        assert resp.get("scheduling") == "SILENT"

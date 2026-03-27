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

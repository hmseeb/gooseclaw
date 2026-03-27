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

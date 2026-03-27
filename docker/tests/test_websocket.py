"""WebSocket protocol unit tests (Phase 27-01).

Tests for RFC 6455 frame parsing, accept key computation, masking,
ping/pong, and close frame construction. Written RED-first (TDD).
"""

import base64
import os
import socket
import struct
import sys

# Add docker/ to sys.path so gateway can be imported
docker_dir = os.path.join(os.path.dirname(__file__), "..")
if docker_dir not in sys.path:
    sys.path.insert(0, docker_dir)

from gateway import (
    ws_accept_key,
    ws_recv_frame,
    ws_send_frame,
    ws_send_close,
    WS_MAGIC,
    WS_OP_TEXT,
    WS_OP_BINARY,
    WS_OP_CLOSE,
    WS_OP_PING,
    WS_OP_PONG,
)


class TestWsAcceptKey:
    """RFC 6455 handshake key computation."""

    def test_rfc6455_example_key(self):
        """Key from RFC 6455 section 4.2.2 must produce the documented accept value."""
        client_key = "dGhlIHNhbXBsZSBub25jZQ=="
        expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        assert ws_accept_key(client_key) == expected

    def test_strips_whitespace(self):
        """Trailing whitespace on key should be stripped before hashing."""
        key_clean = "dGhlIHNhbXBsZSBub25jZQ=="
        key_dirty = "dGhlIHNhbXBsZSBub25jZQ==   "
        assert ws_accept_key(key_dirty) == ws_accept_key(key_clean)


class TestFrameParser:
    """Frame reading and writing through socket pairs."""

    def test_text_frame_roundtrip(self):
        """Send a text frame with 'hello', recv it, verify opcode and payload."""
        a, b = socket.socketpair()
        try:
            ws_send_frame(a, WS_OP_TEXT, b"hello")
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_TEXT
            assert payload == b"hello"
        finally:
            a.close()
            b.close()

    def test_binary_frame_roundtrip(self):
        """Send a binary frame, verify roundtrip."""
        a, b = socket.socketpair()
        try:
            data = b"\x00\x01\x02"
            ws_send_frame(a, WS_OP_BINARY, data)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_BINARY
            assert payload == data
        finally:
            a.close()
            b.close()

    def test_small_payload_7bit(self):
        """100 bytes: 7-bit length encoding."""
        a, b = socket.socketpair()
        try:
            data = os.urandom(100)
            ws_send_frame(a, WS_OP_BINARY, data)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_BINARY
            assert payload == data
        finally:
            a.close()
            b.close()

    def test_medium_payload_16bit(self):
        """1000 bytes: 16-bit extended length (length byte = 126)."""
        a, b = socket.socketpair()
        try:
            data = os.urandom(1000)
            ws_send_frame(a, WS_OP_BINARY, data)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_BINARY
            assert payload == data
        finally:
            a.close()
            b.close()

    def test_large_payload_64bit(self):
        """70000 bytes: 64-bit extended length (length byte = 127)."""
        a, b = socket.socketpair()
        try:
            data = os.urandom(70000)
            ws_send_frame(a, WS_OP_BINARY, data)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_BINARY
            assert payload == data
        finally:
            a.close()
            b.close()

    def test_masked_frame(self):
        """Send a frame with mask=True, recv and verify unmasked payload matches."""
        a, b = socket.socketpair()
        try:
            data = b"masked payload test"
            ws_send_frame(a, WS_OP_TEXT, data, mask=True)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_TEXT
            assert payload == data
        finally:
            a.close()
            b.close()

    def test_empty_payload(self):
        """Send frame with empty payload. Should not crash."""
        a, b = socket.socketpair()
        try:
            ws_send_frame(a, WS_OP_TEXT, b"")
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_TEXT
            assert payload == b""
        finally:
            a.close()
            b.close()


class TestPingPong:
    """Control frame construction."""

    def test_ping_frame_opcode(self):
        """ws_send_frame with opcode 0x9 sends a ping frame."""
        a, b = socket.socketpair()
        try:
            ws_send_frame(a, WS_OP_PING, b"")
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_PING
        finally:
            a.close()
            b.close()

    def test_pong_response(self):
        """Send ping, verify receiving side reads correct opcode."""
        a, b = socket.socketpair()
        try:
            ws_send_frame(a, WS_OP_PING, b"ping-data")
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_PING
            assert payload == b"ping-data"
        finally:
            a.close()
            b.close()


class TestCloseFrame:
    """Close handshake."""

    def test_close_frame_default_code(self):
        """ws_send_close(sock) sends close frame with status code 1000."""
        a, b = socket.socketpair()
        try:
            ws_send_close(a)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_CLOSE
            code = struct.unpack(">H", payload[:2])[0]
            assert code == 1000
        finally:
            a.close()
            b.close()

    def test_close_frame_custom_code(self):
        """ws_send_close(sock, code=1001) sends close with 1001."""
        a, b = socket.socketpair()
        try:
            ws_send_close(a, code=1001)
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_CLOSE
            code = struct.unpack(">H", payload[:2])[0]
            assert code == 1001
        finally:
            a.close()
            b.close()

    def test_close_frame_with_reason(self):
        """Close with reason string, verify it appears after status code."""
        a, b = socket.socketpair()
        try:
            ws_send_close(a, code=1000, reason="going away")
            opcode, payload = ws_recv_frame(b)
            assert opcode == WS_OP_CLOSE
            code = struct.unpack(">H", payload[:2])[0]
            assert code == 1000
            reason = payload[2:].decode("utf-8")
            assert reason == "going away"
        finally:
            a.close()
            b.close()

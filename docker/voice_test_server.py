#!/usr/bin/env python3
"""
Voice WebSocket server using the `websockets` library (async).
Runs as a background process on port 8765 inside the container.
gateway.py raw-proxies browser WebSocket connections here.

All WebSocket framing is handled by the websockets library.
No auth required (gateway already validated the token before proxying).
"""

import asyncio
import base64
import json
import os
import re
import socket
import ssl
import struct
import urllib.parse

import websockets
import yaml

# ── config ───────────────────────────────────────────────────────────────────

PORT = 8765
APP_DIR = os.environ.get("APP_DIR", "/app")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
VAULT_FILE = os.path.join(DATA_DIR, "secrets", "vault.yaml")
GOOSE_PORT = int(os.environ.get("GOOSE_PORT", "3001"))
GOOSE_INTERNAL_TOKEN = os.environ.get("GOOSE_INTERNAL_TOKEN", "")

GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
WS_MAGIC = "258EAFA5-E914-47DA-95CA-5AB5DF085A11"


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_gemini_api_key():
    """Read Gemini API key from vault.yaml."""
    if not os.path.exists(VAULT_FILE):
        return None
    try:
        with open(VAULT_FILE) as f:
            data = yaml.safe_load(f) or {}
        return data.get("GEMINI_API_KEY")
    except Exception:
        return None


def _discover_voice_tools():
    """Query goosed for enabled extensions, return (Gemini function declarations, name_map)."""
    if not GOOSE_INTERNAL_TOKEN:
        return [], {}
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", GOOSE_PORT, timeout=5)
        conn.request("GET", "/config", headers={"X-Secret-Key": GOOSE_INTERNAL_TOKEN})
        resp = conn.getresponse()
        if resp.status != 200:
            resp.read()
            conn.close()
            return [], {}
        cfg = json.loads(resp.read().decode("utf-8", errors="replace"))
        conn.close()

        extensions = cfg.get("config", {}).get("extensions", {})
        declarations = []
        name_map = {}
        for ext_name, ext_cfg in extensions.items():
            if not isinstance(ext_cfg, dict):
                continue
            if ext_cfg.get("enabled") is False:
                continue
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', ext_name)
            if safe_name and safe_name[0].isdigit():
                safe_name = "ext_" + safe_name
            declarations.append({
                "name": safe_name,
                "description": f"Use the {ext_name} extension/tool",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "request": {
                            "type": "STRING",
                            "description": "What to do with this tool",
                        }
                    },
                    "required": ["request"],
                },
            })
            name_map[safe_name] = ext_name
        return declarations, name_map
    except Exception as e:
        print(f"[voice] tool discovery failed: {e}")
        return [], {}


# ── raw Gemini WebSocket (blocking sockets, same as before) ──────────────────

def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf.extend(chunk)
    return bytes(buf)


def _recv_frame(sock):
    try:
        h = _recv_exact(sock, 2)
        op = h[0] & 0x0F
        masked = bool(h[1] & 0x80)
        plen = h[1] & 0x7F
        if plen == 126:
            plen = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif plen == 127:
            plen = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        mk = _recv_exact(sock, 4) if masked else None
        payload = _recv_exact(sock, plen) if plen > 0 else b""
        if masked and mk:
            payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
        return op, payload
    except (ConnectionError, OSError):
        return None, b""


def _send_frame(sock, op, payload, mask=False):
    frame = bytearray([0x80 | op])
    mb = 0x80 if mask else 0
    if len(payload) < 126:
        frame.append(mb | len(payload))
    elif len(payload) < 65536:
        frame.append(mb | 126)
        frame.extend(struct.pack(">H", len(payload)))
    else:
        frame.append(mb | 127)
        frame.extend(struct.pack(">Q", len(payload)))
    if mask:
        mk = os.urandom(4)
        frame.extend(mk)
        payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
    frame.extend(payload)
    sock.sendall(bytes(frame))


def gemini_connect(api_key, voice_name="Aoede", tools=None):
    ctx = ssl.create_default_context()
    raw = socket.create_connection((GEMINI_HOST, 443), timeout=10)
    sock = ctx.wrap_socket(raw, server_hostname=GEMINI_HOST)
    url = GEMINI_PATH + "?" + urllib.parse.urlencode({"key": api_key})
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {url} HTTP/1.1\r\n"
        f"Host: {GEMINI_HOST}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = sock.recv(4096)
        if not c:
            raise ConnectionError("closed during handshake")
        resp += c
    if b"101" not in resp.split(b"\r\n")[0]:
        raise ConnectionError(resp.split(b"\r\n")[0].decode())
    leftover = resp.split(b"\r\n\r\n", 1)[1]
    if leftover:
        orig = sock.recv
        buf = [leftover]
        def patched(n, f=0):
            if buf[0]:
                d = buf[0][:n]
                buf[0] = buf[0][n:]
                return d
            return orig(n)
        sock.recv = patched
    print("[gemini] handshake OK")

    config = {
        "setup": {
            "model": "models/gemini-3.1-flash-live-preview",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice_name}
                    }
                },
            },
            "systemInstruction": {
                "parts": [{"text": "You are a helpful AI assistant. Keep responses very brief."}]
            },
            "sessionResumption": {},
            "contextWindowCompression": {"slidingWindow": {}},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {"disabled": False}
            },
        }
    }

    # add tools if discovered
    if tools:
        config["setup"]["tools"] = [{"functionDeclarations": tools}]

    _send_frame(sock, 1, json.dumps(config).encode(), mask=True)
    print("[gemini] config sent")
    sock.settimeout(15)
    op, payload = _recv_frame(sock)
    if op is None:
        raise ConnectionError("no response")
    if op == 8:
        reason = payload[2:].decode(errors="replace") if len(payload) > 2 else ""
        raise ConnectionError(f"rejected: {reason}")
    try:
        msg = json.loads(payload.decode())
        if "setupComplete" in msg:
            print("[gemini] setupComplete OK")
    except Exception:
        pass
    sock.settimeout(60)
    return sock


# ── message parsing ──────────────────────────────────────────────────────────

def extract_audio(msg):
    chunks = []
    for part in msg.get("serverContent", {}).get("modelTurn", {}).get("parts", []):
        d = part.get("inlineData", {}).get("data")
        if d:
            chunks.append(base64.b64decode(d))
    return chunks


def parse_msg(msg):
    if "setupComplete" in msg:
        return {"type": "ready"}
    if "serverContent" in msg:
        c = msg["serverContent"]
        if c.get("interrupted"):
            return {"type": "interrupted"}
        if c.get("outputTranscription"):
            return {"type": "transcript", "speaker": "ai", "text": c["outputTranscription"]["text"]}
        if c.get("inputTranscription"):
            return {"type": "transcript", "speaker": "user", "text": c["inputTranscription"]["text"]}
        return {"type": "audio"}
    return None


# ── WebSocket handler ────────────────────────────────────────────────────────

async def voice_handler(ws):
    print(f"[ws] browser connected: {ws.remote_address}")
    gemini = None

    api_key = _get_gemini_api_key()
    if not api_key:
        await ws.send(json.dumps({"type": "error", "message": "No Gemini API key configured"}))
        return

    # discover tools
    tools = []
    tool_name_map = {}
    try:
        tools, tool_name_map = _discover_voice_tools()
        if tools:
            print(f"[voice] discovered {len(tools)} tools: {[t['name'] for t in tools]}")
    except Exception as e:
        print(f"[voice] tool discovery failed: {e}")

    try:
        gemini = gemini_connect(api_key, tools=tools if tools else None)
        await ws.send(json.dumps({"type": "ready"}))
        print("[ws] sent ready")

        stop = asyncio.Event()

        async def browser_to_gemini():
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        encoded = base64.b64encode(msg).decode()
                        j = json.dumps({
                            "realtimeInput": {
                                "audio": {
                                    "data": encoded,
                                    "mimeType": "audio/pcm;rate=16000",
                                }
                            }
                        })
                        _send_frame(gemini, 1, j.encode(), mask=True)
                    else:
                        _send_frame(gemini, 1, msg.encode() if isinstance(msg, str) else msg, mask=True)
            except Exception as e:
                print(f"[b->g] {e}")
            finally:
                stop.set()

        async def gemini_to_browser():
            loop = asyncio.get_event_loop()
            try:
                while not stop.is_set():
                    op, payload = await loop.run_in_executor(None, _recv_frame, gemini)
                    if op is None or op == 8:
                        reason = payload[2:].decode(errors="replace") if payload and len(payload) > 2 else ""
                        if reason:
                            await ws.send(json.dumps({"type": "error", "message": reason}))
                        print(f"[g->b] gemini closed: {reason}")
                        break
                    if op == 9:
                        _send_frame(gemini, 10, payload, mask=True)
                        continue
                    if op == 10:
                        continue
                    if op in (1, 2):
                        try:
                            msg = json.loads(payload.decode())
                        except Exception:
                            await ws.send(payload)
                            continue
                        parsed = parse_msg(msg)
                        if not parsed:
                            continue
                        if parsed["type"] in ("transcript", "audio"):
                            for chunk in extract_audio(msg):
                                await ws.send(chunk)
                            if parsed["type"] == "transcript":
                                await ws.send(json.dumps(parsed))
                                print(f"  [{parsed['speaker']}] {parsed['text']}")
                        elif parsed["type"] == "interrupted":
                            await ws.send(json.dumps({"type": "interrupted"}))
            except Exception as e:
                print(f"[g->b] {e}")
            finally:
                stop.set()

        await asyncio.gather(browser_to_gemini(), gemini_to_browser())

    except Exception as e:
        print(f"[ws] error: {e}")
        try:
            await ws.send(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        if gemini:
            try:
                gemini.close()
            except Exception:
                pass
        print("[ws] session ended")


# ── HTTP request handler (non-WebSocket) ─────────────────────────────────────

async def process_request(connection, request):
    """Serve voice.html, sessions, and preference endpoints for non-WebSocket requests."""
    from websockets.http11 import Response

    path = request.path.split("?")[0]

    if path == "/voice" or path == "/":
        html_path = os.path.join(APP_DIR, "docker", "voice.html")
        try:
            with open(html_path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return Response(404, "Not Found", websockets.Headers({"Content-Type": "text/plain"}), b"voice.html not found")
        return Response(200, "OK", websockets.Headers({
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy":
                "default-src 'self'; script-src 'self' 'unsafe-inline' blob:; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; "
                "media-src 'self' blob:; worker-src blob:; frame-ancestors 'none'",
        }), body)

    if path == "/api/voice/sessions":
        body = json.dumps([]).encode()
        return Response(200, "OK", websockets.Headers({"Content-Type": "application/json"}), body)

    if path.startswith("/api/voice/preference"):
        body = json.dumps({"voice": "Aoede"}).encode()
        return Response(200, "OK", websockets.Headers({"Content-Type": "application/json"}), body)

    if path.startswith("/api/voice/token"):
        import secrets
        token = secrets.token_urlsafe(32)
        body = json.dumps({"token": token}).encode()
        return Response(200, "OK", websockets.Headers({"Content-Type": "application/json"}), body)

    if path == "/api/health":
        return Response(200, "OK", websockets.Headers({"Content-Type": "application/json"}), b'{"status":"ok"}')

    # Let WebSocket connections through (return None = upgrade)
    return None


# ── main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"[voice] server starting on 0.0.0.0:{PORT}")
    print(f"[voice] vault: {VAULT_FILE}")
    print(f"[voice] goosed: 127.0.0.1:{GOOSE_PORT}")
    async with websockets.serve(
        voice_handler,
        "0.0.0.0",
        PORT,
        process_request=process_request,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

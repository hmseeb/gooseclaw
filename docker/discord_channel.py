"""Discord channel plugin for GooseClaw.

Provides full rich media support via Discord Bot API.
Requires: DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID environment variables or setup.json credentials.
Requires: websocket-client pip package for Gateway WebSocket.

Setup:
1. Create a Discord Application at https://discord.com/developers/applications
2. Create a Bot, copy the token
3. Enable "Message Content Intent" under Bot > Privileged Gateway Intents
4. Invite bot to your server with message + file permissions
5. Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID in setup.json or env
"""

import json
import os
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
import base64

# Import gateway classes. Gateway is the main module when loaded via _load_channel.
# Try __main__ first (production), then direct import (testing), then fallback stubs.
try:
    _gw = sys.modules.get("__main__")
    if _gw and hasattr(_gw, "OutboundAdapter"):
        OutboundAdapter = _gw.OutboundAdapter
        ChannelCapabilities = _gw.ChannelCapabilities
        InboundMessage = _gw.InboundMessage
        MediaContent = _gw.MediaContent
    else:
        raise ImportError("gateway not in __main__")
except Exception:
    # Try direct import (when running tests or standalone)
    try:
        import gateway as _gw_mod
        OutboundAdapter = _gw_mod.OutboundAdapter
        ChannelCapabilities = _gw_mod.ChannelCapabilities
        InboundMessage = _gw_mod.InboundMessage
        MediaContent = _gw_mod.MediaContent
    except Exception:
        # Fallback: minimal class stubs for standalone use
        class OutboundAdapter:
            def capabilities(self): return {}
            def send_text(self, text): raise NotImplementedError
            def send_image(self, data, **kw): return self.send_text(f"[image] {kw.get('caption','')}")
            def send_voice(self, data, **kw): return self.send_text("[voice message]")
            def send_file(self, data, **kw): return self.send_text(f"[File: {kw.get('filename','file')}]")
        class ChannelCapabilities:
            def __init__(self, **kw):
                for k, v in kw.items(): setattr(self, k, v)
        class InboundMessage:
            def __init__(self, user_id, text="", channel="", media=None, metadata=None):
                self.user_id = str(user_id); self.text = text or ""; self.channel = channel or ""
                self.media = media if media is not None else []; self.metadata = metadata or {}
        class MediaContent:
            def __init__(self, kind, mime_type, data, filename=None):
                self.kind = kind; self.mime_type = mime_type; self.data = data; self.filename = filename
            def to_content_block(self):
                if self.kind == "image":
                    return {"type": "image", "data": base64.b64encode(self.data).decode(), "mimeType": self.mime_type}
                return None

try:
    import websocket
except ImportError:
    websocket = None

DISCORD_API = "https://discord.com/api/v10"


def _discord_request(token, method, path, body=None, headers=None, content_type="application/json"):
    """Make an authenticated request to Discord REST API."""
    url = f"{DISCORD_API}{path}"
    hdrs = {"Authorization": f"Bot {token}"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        if isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    if content_type != "application/json" and data:
        hdrs["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _build_discord_multipart(payload_json, files):
    """Build multipart/form-data for Discord file uploads.

    payload_json: dict for the payload_json form field
    files: list of (filename, content_type, data_bytes)
    Returns: (body_bytes, content_type_header)
    """
    boundary = uuid.uuid4().hex
    lines = []
    # payload_json part
    lines.append(f"--{boundary}".encode())
    lines.append(b'Content-Disposition: form-data; name="payload_json"')
    lines.append(b"Content-Type: application/json")
    lines.append(b"")
    lines.append(json.dumps(payload_json).encode("utf-8"))
    # file parts
    for i, (filename, content_type, data) in enumerate(files):
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="files[{i}]"; filename="{filename}"'.encode())
        lines.append(f"Content-Type: {content_type}".encode())
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


class DiscordOutboundAdapter(OutboundAdapter):
    """Sends text and media to a Discord channel via REST API."""

    def __init__(self, bot_token, channel_id):
        self.bot_token = bot_token
        self.channel_id = str(channel_id)

    def capabilities(self):
        return ChannelCapabilities(
            supports_images=True,
            supports_voice=False,
            supports_files=True,
            supports_buttons=False,
            max_file_size=10_000_000,
            max_text_length=2000,
        )

    def send_text(self, text):
        try:
            _discord_request(self.bot_token, "POST",
                             f"/channels/{self.channel_id}/messages",
                             body={"content": text[:2000]})
            return {"sent": True, "error": ""}
        except Exception as e:
            return {"sent": False, "error": str(e)}

    def send_image(self, image_bytes, caption="", mime_type="image/png", **kwargs):
        return self._send_file_msg(image_bytes, f"image{_ext(mime_type)}", mime_type, caption)

    def send_file(self, file_bytes, filename="file", mime_type="application/octet-stream", **kwargs):
        return self._send_file_msg(file_bytes, filename, mime_type, "")

    def _send_file_msg(self, data, filename, mime_type, caption):
        url = f"{DISCORD_API}/channels/{self.channel_id}/messages"
        payload = {"content": caption[:2000] if caption else ""}
        payload["attachments"] = [{"id": 0, "filename": filename}]
        body, ct = _build_discord_multipart(payload, [(filename, mime_type, data)])
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bot {self.bot_token}",
                "Content-Type": ct,
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                json.loads(resp.read())
                return {"sent": True, "error": ""}
        except Exception as e:
            return {"sent": False, "error": str(e)}


def _ext(mime_type):
    """Get file extension from MIME type."""
    _map = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "audio/ogg": ".ogg", "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    return _map.get(mime_type, ".bin")


def _extract_discord_media(msg):
    """Extract media from Discord message attachments. Returns list of MediaContent."""
    media = []
    for att in msg.get("attachments", []):
        url = att.get("url", "")
        mime = att.get("content_type", "application/octet-stream")
        fname = att.get("filename", "file")

        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("audio/"):
            kind = "audio"
        elif mime.startswith("video/"):
            kind = "video"
        else:
            kind = "document"

        # Download from CDN
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            media.append(MediaContent(kind=kind, mime_type=mime, data=data, filename=fname))
        except Exception as e:
            print(f"[discord] failed to download attachment {fname}: {e}")
    return media


def _download_discord_attachment(url):
    """Download file bytes from Discord CDN URL."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(), ""
    except Exception as e:
        return None, str(e)


def _get_gateway_url(token):
    """Get Discord Gateway WebSocket URL."""
    result = _discord_request(token, "GET", "/gateway/bot")
    return result["url"]


def setup_discord(creds):
    """Validate Discord credentials."""
    token = creds.get("DISCORD_BOT_TOKEN", "")
    channel_id = creds.get("DISCORD_CHANNEL_ID", "")
    if not token or not channel_id:
        return {"ok": False, "error": "DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID required"}
    try:
        _discord_request(token, "GET", f"/channels/{channel_id}")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"Discord API error: {e}"}


def poll_discord(relay, stop_event, creds):
    """Poll Discord Gateway for messages. Blocking loop."""
    if websocket is None:
        print("[discord] ERROR: websocket-client not installed. Run: pip install websocket-client")
        return

    token = creds["DISCORD_BOT_TOKEN"]
    channel_id = str(creds["DISCORD_CHANNEL_ID"])
    bot_user_id = [None]

    while not stop_event.is_set():
        try:
            gateway_url = _get_gateway_url(token)

            ws = websocket.WebSocket()
            ws.settimeout(60)
            ws.connect(f"{gateway_url}?v=10&encoding=json")

            # Hello (op 10)
            hello = json.loads(ws.recv())
            if hello.get("op") != 10:
                print(f"[discord] unexpected hello: {hello}")
                ws.close()
                time.sleep(5)
                continue

            interval = hello["d"]["heartbeat_interval"] / 1000.0
            seq = [None]

            # Heartbeat thread
            hb_stop = threading.Event()

            def _heartbeat():
                while not hb_stop.is_set() and not stop_event.is_set():
                    try:
                        ws.send(json.dumps({"op": 1, "d": seq[0]}))
                    except Exception:
                        break
                    hb_stop.wait(interval)
            hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            hb_thread.start()

            # Identify (op 2)
            GUILD_MESSAGES = 1 << 9
            MESSAGE_CONTENT = 1 << 15
            ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": token,
                    "intents": GUILD_MESSAGES | MESSAGE_CONTENT,
                    "properties": {"os": "linux", "browser": "gooseclaw", "device": "gooseclaw"},
                }
            }))

            # Event loop
            while not stop_event.is_set():
                try:
                    raw = ws.recv()
                    if not raw:
                        break
                    data = json.loads(raw)

                    if data.get("s"):
                        seq[0] = data["s"]

                    op = data.get("op", 0)

                    if op == 0:  # Dispatch
                        t = data.get("t", "")
                        d = data.get("d", {})

                        if t == "READY":
                            bot_user_id[0] = d.get("user", {}).get("id")
                            print(f"[discord] connected as {d.get('user', {}).get('username', '?')}")

                        elif t == "MESSAGE_CREATE":
                            author = d.get("author", {})
                            if author.get("bot", False):
                                continue
                            if author.get("id") == bot_user_id[0]:
                                continue
                            if d.get("channel_id") != channel_id:
                                continue

                            text = d.get("content", "")
                            media = _extract_discord_media(d)
                            inbound = InboundMessage(
                                user_id=author["id"],
                                text=text,
                                channel="discord",
                                media=media,
                                metadata={"username": author.get("username", "")},
                            )
                            try:
                                relay(inbound, adapter.send_text)
                            except Exception as e:
                                print(f"[discord] relay error: {e}")

                    elif op == 11:  # Heartbeat ACK
                        pass
                    elif op == 7:  # Reconnect
                        print("[discord] server requested reconnect")
                        break
                    elif op == 9:  # Invalid Session
                        print("[discord] invalid session, reconnecting...")
                        time.sleep(5)
                        break

                except Exception as e:
                    if "WebSocketTimeoutException" in type(e).__name__:
                        continue
                    print(f"[discord] event loop error: {e}")
                    break

            hb_stop.set()
            try:
                ws.close()
            except Exception:
                pass

        except Exception as e:
            print(f"[discord] connection error: {e}")

        if not stop_event.is_set():
            print("[discord] reconnecting in 5s...")
            stop_event.wait(5)


# Create adapter instance (credentials resolved at load time by _load_channel)
_token = os.environ.get("DISCORD_BOT_TOKEN", "")
_channel_id = os.environ.get("DISCORD_CHANNEL_ID", "")
adapter = DiscordOutboundAdapter(_token, _channel_id) if _token and _channel_id else None

CHANNEL = {
    "name": "discord",
    "version": 2,
    "send": (adapter.send_text if adapter else lambda text: {"sent": False, "error": "not configured"}),
    "adapter": adapter,
    "poll": poll_discord,
    "credentials": ["DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"],
    "setup": setup_discord,
}

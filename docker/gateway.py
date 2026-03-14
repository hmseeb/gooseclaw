#!/usr/bin/env python3
"""
gooseclaw gateway — setup wizard + notification bus + reverse proxy to goosed.

Runs on $PORT. Serves /setup directly, proxies everything else to goosed
on an internal port. Manages the goosed subprocess lifecycle.

Architecture:
  - notification bus: channel-agnostic delivery. telegram/slack/whatsapp register
    handlers via register_notification_handler(). scheduler, job engine, and session
    watcher all deliver through notify_all() without knowing which channels are active.
  - cron scheduler: reads goose schedule.json, fires jobs in isolated goosed
    sessions, delivers output via notify_all(). replaces goose's built-in scheduler
    which only runs inside `goose gateway` (not `goosed`).
  - job engine: unified timer + script runner. 10s tick, zero LLM cost.
  - session watcher: polls goosed for scheduled session output, forwards via notify.

API:
  GET  /api/health           -> health check
  GET  /api/setup/config     -> current provider config (masked)
  GET  /api/setup/status     -> goosed startup state (idle/starting/ready/error)
  POST /api/setup/validate   -> validate provider credentials
  POST /api/setup/save       -> save provider config and restart
  POST /api/notify           -> send message to all registered notification channels
  GET  /api/notify/status    -> check if notification delivery is available
  GET  /api/telegram/status  -> telegram gateway status, paired users, pairing code
  POST /api/telegram/pair    -> generate a new telegram pairing code
  POST /api/auth/recover     -> reset auth token using GOOSECLAW_RECOVERY_SECRET
  GET  /api/jobs              -> list all jobs (reminders + scripts)
  POST /api/jobs              -> create a job
  DELETE /api/jobs/<id>       -> cancel/delete a job
  POST /api/jobs/<id>/run     -> manually trigger a job
  GET  /api/schedule/upcoming -> upcoming jobs with next-run times (LLM-aware)
  GET  /api/schedule/context  -> human-readable schedule summary for LLM
  GET  /api/channels          -> list loaded channel plugins
  POST /api/channels/reload   -> hot-reload channel plugins from /data/channels/
  GET  /admin                  -> admin dashboard
"""

import base64
import collections
import glob
import hashlib
import hmac
import mimetypes
import http.client
import http.server
import importlib.util
import json
import os
import re
import string
import secrets
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer

# ── rate limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple per-IP sliding window rate limiter using stdlib only."""

    def __init__(self, max_requests=60, window_seconds=60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests = collections.defaultdict(list)  # ip -> [timestamps]
        self._lock = threading.Lock()

    def is_allowed(self, ip):
        """Check if request from IP is allowed. Cleans old entries."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            timestamps = self._requests[ip]
            # remove expired entries
            self._requests[ip] = [t for t in timestamps if t > cutoff]
            if len(self._requests[ip]) >= self.max_requests:
                return False
            self._requests[ip].append(now)
            return True

    def cleanup(self):
        """Periodic cleanup of stale IPs (call from a timer)."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            stale = [ip for ip, ts in self._requests.items() if not ts or ts[-1] < cutoff]
            for ip in stale:
                del self._requests[ip]


# ── command routing ──────────────────────────────────────────────────────────

class CommandRouter:
    """Routes slash commands to handler functions.
    Register handlers with descriptions, dispatch by command name."""

    def __init__(self):
        self._handlers = {}   # command_name (no slash) -> handler_fn
        self._help_text = {}  # command_name -> description

    def register(self, command, handler_fn, description=""):
        """Register a command handler. command should NOT include '/'."""
        self._handlers[command.lower()] = handler_fn
        if description:
            self._help_text[command.lower()] = description

    def is_command(self, text):
        """Check if text is a registered slash command."""
        if not text or not text.startswith("/"):
            return False
        cmd = text.lower().split()[0][1:]  # strip the /
        return cmd in self._handlers

    def dispatch(self, text, context):
        """Dispatch command to handler. Returns True if handled.
        context is a dict with channel-specific info (channel, user_id, send_fn, etc.)."""
        if not text or not text.startswith("/"):
            return False
        cmd = text.lower().split()[0][1:]
        handler = self._handlers.get(cmd)
        if handler:
            handler(context)
            return True
        return False

    def get_help_text(self):
        """Generate formatted help text from registered commands."""
        lines = []
        for cmd, desc in sorted(self._help_text.items()):
            lines.append(f"/{cmd} -- {desc}")
        return "\n".join(lines)


# module-level rate limiter instances
api_limiter = RateLimiter(max_requests=60, window_seconds=60)    # 1 req/sec sustained
auth_limiter = RateLimiter(max_requests=5, window_seconds=60)    # auth-sensitive endpoints
notify_limiter = RateLimiter(max_requests=10, window_seconds=60)  # notify endpoint


# ── session management (shared) ─────────────────────────────────────────────

class SessionManager:
    """Unified session store with composite keys (channel:user_id).
    Thread-safe. Optional disk persistence per channel."""

    def __init__(self, persist_dir=None):
        self._sessions = {}          # "channel:user_id" -> session_id
        self._lock = threading.Lock()
        self._persist_dir = persist_dir

    def get(self, channel, user_id):
        key = f"{channel}:{user_id}"
        with self._lock:
            return self._sessions.get(key)

    def set(self, channel, user_id, session_id):
        key = f"{channel}:{user_id}"
        with self._lock:
            self._sessions[key] = session_id
        self._save(channel)

    def pop(self, channel, user_id):
        key = f"{channel}:{user_id}"
        with self._lock:
            sid = self._sessions.pop(key, None)
        if sid is not None:
            self._save(channel)
        return sid

    def clear_channel(self, channel):
        prefix = f"{channel}:"
        with self._lock:
            keys = [k for k in self._sessions if k.startswith(prefix)]
            for k in keys:
                del self._sessions[k]
        self._save(channel)

    def get_all_for_channel(self, channel):
        prefix = f"{channel}:"
        with self._lock:
            return {k[len(prefix):]: v for k, v in self._sessions.items()
                    if k.startswith(prefix)}

    def _save(self, channel):
        if not self._persist_dir:
            return
        with self._lock:
            data = {k: v for k, v in self._sessions.items()
                    if k.startswith(f"{channel}:")}
        # Write OUTSIDE the lock to avoid deadlock (same pattern as existing _save_telegram_sessions)
        try:
            os.makedirs(self._persist_dir, exist_ok=True)
            fpath = os.path.join(self._persist_dir, f"sessions_{channel}.json")
            tmp = fpath + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, fpath)
        except Exception as e:
            print(f"[session-mgr] warn: could not save {channel} sessions: {e}")

    def load(self, channel):
        if not self._persist_dir:
            return
        fpath = os.path.join(self._persist_dir, f"sessions_{channel}.json")
        try:
            if os.path.exists(fpath):
                with open(fpath) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    with self._lock:
                        self._sessions.update(data)
        except Exception as e:
            print(f"[session-mgr] warn: could not load {channel} sessions: {e}")


# ── channel state (shared concurrency primitives) ───────────────────────────

class ChannelState:
    """Per-channel concurrency primitives: user locks, active relay tracking."""

    def __init__(self):
        self._active_relays = {}       # user_id -> [sock, cancel_event]
        self._relays_lock = threading.Lock()
        self._user_locks = {}          # user_id -> Lock
        self._user_locks_lock = threading.Lock()
        self._prewarm_events = {}      # user_id -> Event
        self._greeting_events = {}     # user_id -> Event (set when kick_greeting done)
        self._queued_messages = {}     # user_id -> list of (text, replay_fn)
        self._queue_lock = threading.Lock()

    def queue_message(self, user_id, text, replay_fn=None):
        """Queue a message for processing after the current relay completes.
        replay_fn: optional callable() that re-runs the relay for this message.
        """
        uid = str(user_id)
        with self._queue_lock:
            if uid not in self._queued_messages:
                self._queued_messages[uid] = []
            self._queued_messages[uid].append((text, replay_fn))

    def pop_queued_replay(self, user_id):
        """Pop the next queued (text, replay_fn) for a user, or None if empty."""
        uid = str(user_id)
        with self._queue_lock:
            msgs = self._queued_messages.get(uid)
            if msgs:
                return msgs.pop(0)
            return None

    def get_user_lock(self, user_id):
        uid = str(user_id)
        with self._user_locks_lock:
            if uid not in self._user_locks:
                self._user_locks[uid] = threading.Lock()
            return self._user_locks[uid]

    def set_active_relay(self, user_id, sock_ref):
        with self._relays_lock:
            self._active_relays[str(user_id)] = sock_ref

    def pop_active_relay(self, user_id):
        with self._relays_lock:
            return self._active_relays.pop(str(user_id), None)

    def kill_relay(self, user_id):
        sock_ref = self.pop_active_relay(user_id)
        if sock_ref and sock_ref[0]:
            if len(sock_ref) > 1 and hasattr(sock_ref[1], 'set'):
                sock_ref[1].set()
            try:
                sock_ref[0].close()
            except Exception:
                pass
        return sock_ref


# ── bot instance / manager ──────────────────────────────────────────────────

class BotInstance:
    """Encapsulates one Telegram bot's runtime state."""

    def __init__(self, name, token, channel_key=None):
        self.name = name
        self.token = token
        self.channel_key = channel_key or f"telegram:{name}"
        self.state = ChannelState()
        self.pair_code = None
        self.pair_lock = threading.Lock()
        self.running = False
        self._thread = None

    def generate_pair_code(self):
        code = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6))
        with self.pair_lock:
            self.pair_code = code
        print(f"[telegram:{self.name}] pairing code: {code}")
        return code

    def get_user_lock(self, user_id):
        return self.state.get_user_lock(user_id)

    def start(self):
        """Start this bot: load sessions, register notifications, generate pair code, start poll thread."""
        if self.running:
            return
        _session_manager.load(self.channel_key)
        register_notification_handler(self.channel_key, self._make_notify_handler())
        self.generate_pair_code()
        self._register_commands()
        self.running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        print(f"[telegram:{self.name}] started")

    def stop(self):
        """Stop this bot's poll loop and wait for thread to finish."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _register_commands(self):
        """Register slash commands with Telegram API for autocomplete."""
        try:
            commands = [
                {"command": "stop", "description": "Cancel the current response"},
                {"command": "clear", "description": "Wipe conversation and start fresh"},
                {"command": "compact", "description": "Summarize history to save tokens"},
                {"command": "help", "description": "Show available commands"},
            ]
            payload = json.dumps({"commands": commands}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/setMyCommands",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            print(f"[telegram:{self.name}] registered slash commands for autocomplete")
        except Exception as e:
            print(f"[telegram:{self.name}] warn: could not register commands: {e}")

    def _make_notify_handler(self):
        """Return a notification handler closure that uses this bot's token and platform."""
        bot_token = self.token
        channel_key = self.channel_key

        def handler(text):
            chat_ids = get_paired_chat_ids(platform=channel_key)
            if not chat_ids:
                return {"sent": False, "error": f"no paired users for {channel_key}"}
            ok_all = True
            for cid in chat_ids:
                ok, err = send_telegram_message(bot_token, cid, text)
                if not ok:
                    ok_all = False
            return {"sent": ok_all, "error": "" if ok_all else "some deliveries failed"}

        return handler

    def _check_pairing(self, chat_id, text):
        """Check if text matches this bot's pair code. Returns True if pairing succeeded.

        Rotates to a new code on match so the old code can never be reused.
        """
        with self.pair_lock:
            current_code = self.pair_code

        if current_code and text.upper() == current_code.upper():
            self.generate_pair_code()
            return True
        return False

    def _do_message_relay(self, chat_id, text, bot_token, inbound_msg=None):
        """Relay a user message to goosed. Uses self.state and self.channel_key.

        Extracted from the poll loop to make the relay path unit-testable.
        inbound_msg: optional InboundMessage envelope (v2 contract, Phase 12+ uses media).
        """
        _memory_touch(chat_id)
        # Change 5: wait for pending greeting before acquiring lock
        _pending_greet = self.state._greeting_events.get(str(chat_id))
        if _pending_greet:
            _pending_greet.wait(timeout=30)
        _chat_lock = self.state.get_user_lock(chat_id)
        if not _chat_lock.acquire(timeout=2):
            _replay = lambda _t=text, _c=chat_id, _bt=bot_token: self._do_message_relay(_c, _t, _bt)
            self.state.queue_message(chat_id, text, replay_fn=_replay)
            send_telegram_message(bot_token, chat_id, "got it, i'll get to this next")
            return
        try:
            # download media in relay thread (not poll loop) to keep poll responsive
            if inbound_msg and inbound_msg.media:
                downloaded = []
                for ref in inbound_msg.media:
                    if isinstance(ref, dict) and ref.get("file_id"):
                        file_bytes, file_path = _download_telegram_file(bot_token, ref["file_id"])
                        if file_bytes is not None:
                            mc = _make_media_content(
                                ref.get("media_key", "document"),
                                file_bytes, file_path,
                                mime_hint=ref.get("mime_hint"),
                                filename=ref.get("filename"),
                            )
                            downloaded.append(mc)
                        else:
                            print(f"[telegram:{self.name}] media download failed for {ref.get('media_key')}: {file_path}")
                inbound_msg.media = downloaded

            _send_typing_action(bot_token, chat_id)
            session_id = _get_session_id(chat_id, channel=self.channel_key)
            _cancelled = threading.Event()
            _sock_ref = [None, _cancelled]

            self.state.set_active_relay(chat_id, _sock_ref)

            _tg_setup = load_setup()
            _tg_verbosity = get_verbosity_for_channel(_tg_setup, self.channel_key) if _tg_setup else "balanced"

            # build content blocks from media attachments
            content_blocks = None
            if inbound_msg and inbound_msg.has_media:
                content_blocks = _build_content_blocks(text, inbound_msg)

            # typing indicator loop
            typing_stop = threading.Event()

            def _typing_loop():
                while not typing_stop.is_set():
                    _send_typing_action(bot_token, chat_id)
                    typing_stop.wait(4)

            typing_thread = threading.Thread(target=_typing_loop, daemon=True)
            typing_thread.start()

            try:
                if _tg_verbosity == "quiet":
                    response_text, error, media = _relay_to_goosed(
                        text, session_id, chat_id=chat_id, channel=self.channel_key,
                        sock_ref=_sock_ref, content_blocks=content_blocks,
                    )
                    if _cancelled.is_set():
                        pass
                    elif error:
                        send_telegram_message(bot_token, chat_id, f"Error: {error}")
                    else:
                        send_telegram_message(bot_token, chat_id, response_text)
                else:
                    # streaming: edit-in-place
                    _edit_state = {"msg_id": None, "accumulated": "", "overflow": []}

                    def _tg_flush_edit(chunk, _st=_edit_state):
                        if _cancelled.is_set():
                            return
                        _st["accumulated"] += chunk
                        txt = _st["accumulated"]
                        if _st["msg_id"] is None:
                            mid, err = _send_telegram_msg_with_id(bot_token, chat_id, txt)
                            if mid:
                                _st["msg_id"] = mid
                            else:
                                print(f"[telegram:{self.name}] edit-stream: initial send failed: {err}")
                        elif len(txt) > 3800:
                            _st["overflow"].append(_st["msg_id"])
                            _st["accumulated"] = chunk
                            _st["msg_id"] = None
                            mid, err = _send_telegram_msg_with_id(bot_token, chat_id, chunk)
                            if mid:
                                _st["msg_id"] = mid
                        else:
                            _edit_telegram_message(bot_token, chat_id, _st["msg_id"], txt)

                    response_text, error, media = _relay_to_goosed(
                        text, session_id, chat_id=chat_id, channel=self.channel_key,
                        flush_cb=_tg_flush_edit, verbosity=_tg_verbosity,
                        sock_ref=_sock_ref, flush_interval=2.0,
                        content_blocks=content_blocks,
                    )
                    if _cancelled.is_set():
                        pass
                    elif _edit_state["msg_id"] and response_text and not error:
                        final_text = _edit_state["accumulated"] or response_text
                        if final_text:
                            _edit_telegram_message(bot_token, chat_id, _edit_state["msg_id"], final_text)
                    elif error:
                        send_telegram_message(bot_token, chat_id, f"Error: {error}")
                    elif not _edit_state["msg_id"] and response_text:
                        send_telegram_message(bot_token, chat_id, response_text)

                # Route media blocks through adapter
                if media and not _cancelled.is_set():
                    try:
                        _adapter = TelegramOutboundAdapter(bot_token, chat_id)
                        _route_media_blocks(media, _adapter)
                    except Exception as _media_exc:
                        print(f"[telegram:{self.name}] media routing error: {_media_exc}")
            finally:
                typing_stop.set()
                typing_thread.join(timeout=2)
        except Exception as exc:
            print(f"[telegram:{self.name}] relay exception for chat {chat_id}: {exc}")
            if not _cancelled.is_set():
                try:
                    send_telegram_message(bot_token, chat_id, f"Error: {exc}")
                except Exception:
                    pass
        finally:
            self.state.pop_active_relay(chat_id)
            _chat_lock.release()
            # process queued messages
            _queued = self.state.pop_queued_replay(chat_id)
            if _queued:
                _, _replay_fn = _queued
                if _replay_fn:
                    threading.Thread(target=_replay_fn, daemon=True).start()

    def _poll_loop(self):
        """Long-poll Telegram for updates and relay messages to goosed.

        Runs in a daemon thread. Uses self.channel_key, self.state, self.pair_code
        instead of module-level globals.
        """
        offset = 0
        print(f"[telegram:{self.name}] polling loop started")

        while self.running:
            try:
                url = (
                    f"https://api.telegram.org/bot{self.token}/getUpdates"
                    f"?offset={offset}&timeout=30&allowed_updates=[\"message\"]"
                )
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=40) as resp:
                    data = json.loads(resp.read())

                if not data.get("ok"):
                    print(f"[telegram:{self.name}] getUpdates not ok: {data}")
                    time.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if not msg:
                        continue

                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "").strip()

                    if not chat_id:
                        continue

                    # build InboundMessage envelope (v2 contract)
                    has_media = _has_media(msg)
                    media_list = []
                    if has_media:
                        # use caption as text if no text field
                        if not text:
                            text = msg.get("caption", "").strip()
                        # build file_id references for deferred download in relay thread
                        for mkey in _MEDIA_KEYS:
                            if mkey in msg:
                                fid, mime_hint, fname = _extract_file_info(msg, mkey)
                                if fid:
                                    media_list.append({
                                        "media_key": mkey,
                                        "file_id": fid,
                                        "mime_hint": mime_hint,
                                        "filename": fname,
                                    })

                    inbound_msg = InboundMessage(
                        user_id=chat_id, text=text,
                        channel=self.channel_key, media=media_list,
                    )

                    # media-only or text+media from paired users: relay (downloads happen in relay thread)
                    # unpaired users with media: silently ignore
                    if has_media and not text:
                        paired_ids = get_paired_chat_ids(platform=self.channel_key)
                        if chat_id in paired_ids:
                            threading.Thread(
                                target=self._do_message_relay,
                                kwargs={"chat_id": chat_id, "text": "", "bot_token": self.token, "inbound_msg": inbound_msg},
                                daemon=True,
                            ).start()
                        continue

                    if not text:
                        continue

                    paired_ids = get_paired_chat_ids(platform=self.channel_key)

                    if chat_id in paired_ids:
                        # handle local slash commands before relaying
                        lower = text.lower()

                        if _command_router.is_command(lower):
                            ctx = {
                                "channel": self.channel_key,
                                "user_id": chat_id,
                                "bot_token": self.token,
                                "send_fn": lambda t, _bt=self.token, _cid=chat_id: send_telegram_message(_bt, _cid, t),
                            }
                            if not _command_router.dispatch(text, ctx):
                                send_telegram_message(self.token, chat_id,
                                    f"Unknown command: {text.split()[0]}\nSend /help for available commands.")
                            continue

                        # catch unknown slash commands
                        if lower.startswith("/"):
                            send_telegram_message(
                                self.token, chat_id,
                                f"Unknown command: {text.split()[0]}\nSend /help for available commands."
                            )
                            continue

                        # relay to goosed (runs in a background thread)
                        threading.Thread(
                            target=self._do_message_relay,
                            kwargs={"chat_id": chat_id, "text": text, "bot_token": self.token, "inbound_msg": inbound_msg},
                            daemon=True,
                        ).start()
                    else:
                        # unpaired user -- check if this is a pairing code
                        if self._check_pairing(chat_id, text):
                            _add_pairing_to_config(chat_id, platform=self.channel_key)
                            send_telegram_message(
                                self.token, chat_id,
                                "Paired successfully! You can now send messages to goose through this chat."
                            )
                            print(f"[telegram:{self.name}] chat {chat_id} paired")

                            # auto-send first message after pairing
                            try:
                                soul_path = os.path.join(IDENTITY_DIR, "soul.md")
                                needs_onboarding = False
                                try:
                                    with open(soul_path, "r") as _sf:
                                        needs_onboarding = "ONBOARDING_NEEDED" in _sf.read()
                                except FileNotFoundError:
                                    needs_onboarding = True

                                # Change 1: static welcome message right after pairing
                                if needs_onboarding:
                                    send_telegram_message(
                                        self.token, chat_id,
                                        "hey! i'm gooseclaw, your personal AI agent. i run 24/7 on your server, learn how you think, and remember everything.\n\ni'm setting up now. give me a few seconds and i'll introduce myself properly."
                                    )
                                else:
                                    send_telegram_message(
                                        self.token, chat_id,
                                        "welcome back! new device paired. give me a moment."
                                    )

                                # Change 3: inject context into LLM kick message
                                kick_msg = (
                                    "I just paired via Telegram. I've already been shown a welcome message saying I'm gooseclaw, a personal AI agent that runs 24/7 and learns. Do NOT repeat any of that. Jump straight into the onboarding flow -- ask my name. Keep your response to 2-3 short sentences. Use normal prose, no bullet points, no line breaks between words. Plain text only, no markdown formatting."
                                    if needs_onboarding else
                                    "I just paired a new device via Telegram. I've already seen a 'welcome back' message. Just say hi casually, keep it very short. Keep your response to 2-3 short sentences. Use normal prose, no bullet points, no line breaks between words. Plain text only, no markdown formatting."
                                )
                                sid = _get_session_id(chat_id, channel=self.channel_key)
                                # Change 4: skip LLM kick greeting for returning users
                                if sid and needs_onboarding:
                                    # Change 5: greeting event to prevent message collision
                                    _greet_evt = threading.Event()
                                    self.state._greeting_events[str(chat_id)] = _greet_evt

                                    def _kick_greeting(msg=kick_msg, s=sid, c=chat_id, bt=self.token, ck=self.channel_key, evt=_greet_evt):
                                        # Change 2: typing indicator during kick_greeting
                                        _typing_stop = threading.Event()

                                        def _typing_loop():
                                            while not _typing_stop.is_set():
                                                _send_typing_action(bt, c)
                                                _typing_stop.wait(4)

                                        _typing_thread = threading.Thread(target=_typing_loop, daemon=True)
                                        _typing_thread.start()
                                        try:
                                            with self.state.get_user_lock(c):
                                                txt, err, *_ = _relay_to_goosed(
                                                    msg, s, chat_id=str(c), channel=ck,
                                                )
                                                if err:
                                                    print(f"[telegram:{self.name}] kick greeting error: {err}")
                                                    send_telegram_message(bt, c, f"Error: {err}")
                                                elif txt:
                                                    send_telegram_message(bt, c, txt)
                                        except Exception as exc:
                                            print(f"[telegram:{self.name}] kick greeting exception: {exc}")
                                        finally:
                                            _typing_stop.set()
                                            evt.set()
                                            self.state._greeting_events.pop(str(c), None)
                                    threading.Thread(target=_kick_greeting, daemon=True).start()
                            except Exception as exc:
                                print(f"[telegram:{self.name}] kick greeting setup failed: {exc}")
                        else:
                            send_telegram_message(
                                self.token, chat_id,
                                "You are not paired with this goose instance. "
                                "Please enter a valid pairing code from the web dashboard."
                            )

            except urllib.error.HTTPError as e:
                if e.code == 409:
                    print(f"[telegram:{self.name}] conflict (409), backing off 10s")
                    time.sleep(10)
                elif e.code == 401:
                    print(f"[telegram:{self.name}] FATAL: invalid bot token (401). Stopping poll loop.")
                    self.running = False
                    return
                else:
                    print(f"[telegram:{self.name}] HTTP error {e.code}, retrying in 5s")
                    time.sleep(5)
            except urllib.error.URLError as e:
                print(f"[telegram:{self.name}] network error: {e.reason}, retrying in 5s")
                time.sleep(5)
            except Exception as e:
                print(f"[telegram:{self.name}] poll error: {e}, retrying in 5s")
                time.sleep(5)

        print(f"[telegram:{self.name}] polling loop stopped")


class BotManager:
    """Manages multiple BotInstance lifecycle."""

    def __init__(self):
        self._bots = {}
        self._lock = threading.Lock()

    def add_bot(self, name, token, channel_key=None):
        with self._lock:
            if name in self._bots:
                return self._bots[name]
            for existing in self._bots.values():
                if existing.token == token:
                    raise ValueError(f"token already in use by bot '{existing.name}'")
            bot = BotInstance(name, token, channel_key)
            self._bots[name] = bot
        return bot

    def remove_bot(self, name):
        with self._lock:
            bot = self._bots.pop(name, None)
        if bot:
            bot.stop()
            _session_manager.clear_channel(bot.channel_key)
            unregister_notification_handler(bot.channel_key)
            print(f"[bot-mgr] removed bot '{name}' (channel_key={bot.channel_key})")

    def stop_all(self):
        with self._lock:
            bots = list(self._bots.values())
            self._bots.clear()
        for bot in bots:
            bot.running = False

    def get_bot(self, name):
        with self._lock:
            return self._bots.get(name)

    def get_all(self):
        with self._lock:
            return dict(self._bots)

    @property
    def any_running(self):
        with self._lock:
            return any(b.running for b in self._bots.values())


# ── security headers ─────────────────────────────────────────────────────────

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# ── config ──────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_DIR = os.path.join(DATA_DIR, "config")
IDENTITY_DIR = os.path.join(DATA_DIR, "identity")
SETUP_FILE = os.path.join(CONFIG_DIR, "setup.json")
APP_DIR = os.environ.get("APP_DIR", "/app")
SETUP_HTML = os.path.join(APP_DIR, "docker", "setup.html")
ADMIN_HTML = os.path.join(APP_DIR, "docker", "admin.html")
PORT = int(os.environ.get("PORT", 8080))
GOOSE_WEB_PORT = 3001
PROXY_TIMEOUT = int(os.environ.get("GOOSECLAW_PROXY_TIMEOUT", "60"))

# goosed always enables TLS (self-signed cert on localhost).
# all gateway -> goosed connections use HTTPS with verification disabled.
_GOOSED_SSL_CTX = ssl.create_default_context()
_GOOSED_SSL_CTX.check_hostname = False
_GOOSED_SSL_CTX.verify_mode = ssl.CERT_NONE


def _goosed_conn(timeout=10):
    """Create an HTTPS connection to the local goosed server."""
    return http.client.HTTPSConnection(
        "127.0.0.1", GOOSE_WEB_PORT, timeout=timeout, context=_GOOSED_SSL_CTX
    )

goosed_process = None
goose_lock = threading.Lock()
telegram_process = None  # kept for backwards compat; no longer a subprocess
telegram_lock = threading.Lock()
telegram_pair_code = None
telegram_pair_lock = threading.Lock()
_telegram_running = False  # True while the Python polling thread is active
_session_manager = SessionManager(persist_dir=DATA_DIR)
_telegram_state = ChannelState()
_legacy_greeting_events = {}  # chat_id -> Event (legacy path kick_greeting sync)
_media_group_buffer = {}  # media_group_id -> {"chat_id", "text", "refs": [], "timer": Timer, "ts": float}
_media_group_lock = threading.Lock()
_command_router = CommandRouter()
_bot_manager = BotManager()

# ── per-channel model routing state ────────────────────────────────────────
# Tracks which model was last set on each goose session so we avoid
# redundant update_provider calls.
_session_model_cache = {}   # session_id (str) -> model_config_id (str)
_session_model_lock = threading.Lock()

# ── notification bus (channel-agnostic delivery) ─────────────────────────────
#
# Any channel (telegram, slack, whatsapp, etc.) registers a handler via
# register_notification_handler(). All delivery goes through notify_all().
# The scheduler, job engine, and session watcher don't know or care
# which channels are active.
#
# Handler signature: handler_fn(text) -> {"sent": bool, "error": str}

_notification_handlers = []    # [{"name": str, "handler": callable}, ...]
_notification_handlers_lock = threading.Lock()


def register_notification_handler(name, handler_fn):
    """Register a delivery channel. handler_fn(text) -> {"sent": bool, "error": str}."""
    with _notification_handlers_lock:
        # avoid double-registration
        for h in _notification_handlers:
            if h["name"] == name:
                h["handler"] = handler_fn
                print(f"[notify] updated handler: {name}")
                return
        _notification_handlers.append({"name": name, "handler": handler_fn})
    print(f"[notify] registered handler: {name}")


def unregister_notification_handler(name):
    """Remove a notification handler by name. No-op if not found."""
    with _notification_handlers_lock:
        _notification_handlers[:] = [h for h in _notification_handlers if h["name"] != name]
    print(f"[notify] unregistered handler: {name}")


# ── channel plugin system state ───────────────────────────────────────────────

CHANNELS_DIR = os.path.join(DATA_DIR, "channels")
_loaded_channels = {}       # name -> {"module": mod, "channel": CHANNEL dict, "creds": dict}
_channel_threads = {}       # name -> Thread
_channel_stop_events = {}   # name -> threading.Event
_channels_lock = threading.Lock()


def _get_valid_channels():
    """Build valid channel names dynamically from fixed set + loaded plugins + bot keys."""
    fixed = {"web", "telegram", "cron", "memory"}
    with _channels_lock:
        plugin_names = set(_loaded_channels.keys())
    result = fixed | plugin_names
    # add bot-scoped channel keys from setup.json
    try:
        setup = load_setup()
        if setup:
            for bot_cfg in _resolve_bot_configs(setup):
                name = bot_cfg.get("name", "")
                if name and name != "default":
                    result.add(f"telegram:{name}")
    except Exception:
        pass
    return result


# ── session watcher state (auto-forward scheduled output to telegram) ───────
_session_watcher_running = False
_session_watcher_state_file = os.path.join(DATA_DIR, "session_watcher_state.json")
_session_watcher_state = {}   # session_id -> {"forwarded_count": int, "schedule_id": str}
_session_watcher_lock = threading.Lock()

# ── job engine state ───────────────────────────────────────────────────────
_JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
_JOBS_TICK_SECONDS = 10
_MAX_CONCURRENT_JOBS = 5
_jobs = []             # list of job dicts
_jobs_lock = threading.Lock()
_job_engine_running = False

# ── watcher engine state ──────────────────────────────────────────────────────
_WATCHERS_FILE = os.path.join(DATA_DIR, "watchers.json")
_watchers = []
_watchers_lock = threading.Lock()
_watcher_threads = {}  # id -> Thread (for stream watchers)
_watcher_engine_running = False

# ── goosed startup state ──────────────────────────────────────────────────
goosed_startup_state = {
    "state": "idle",        # idle | starting | ready | error
    "message": "",          # human-readable status message
    "error": "",            # stderr output when state=error
    "timestamp": 0,         # time.time() of last state change
}
_startup_state_lock = threading.Lock()
_stderr_buffer = collections.deque(maxlen=50)  # last 50 lines of stderr
_stderr_lock = threading.Lock()

# internal token used for gateway -> goosed communication (never exposed to users)
_INTERNAL_GOOSE_TOKEN = None


def _set_startup_state(state, message="", error=""):
    """Update goosed startup state under lock."""
    with _startup_state_lock:
        goosed_startup_state["state"] = state
        goosed_startup_state["message"] = message
        goosed_startup_state["error"] = error
        goosed_startup_state["timestamp"] = time.time()


def _append_stderr(line):
    """Append a line to the stderr ring buffer under lock."""
    with _stderr_lock:
        _stderr_buffer.append(line)


def _get_recent_stderr(n=20):
    """Return the last n lines from the stderr buffer as a single string."""
    with _stderr_lock:
        lines = list(_stderr_buffer)[-n:]
    return "\n".join(lines)


def _stderr_reader(proc):
    """Read proc.stderr line by line, log with prefix, and buffer lines."""
    try:
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            # print to both stdout (Railway captures this) and stderr
            print(f"[goose-web] {line}")
            print(f"[goose-web] {line}", file=sys.stderr)
            _append_stderr(line)
    except Exception:
        pass  # process exited or pipe closed


# ── PID file management ──────────────────────────────────────────────────────

def _write_pid(name, pid):
    """Write a PID file for a managed subprocess."""
    try:
        with open(os.path.join(CONFIG_DIR, f"{name}.pid"), "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _remove_pid(name):
    """Remove a PID file for a managed subprocess."""
    try:
        os.unlink(os.path.join(CONFIG_DIR, f"{name}.pid"))
    except OSError:
        pass


def _check_stale_pid(name):
    """Check if a PID file exists for a dead process and clean it up."""
    pid_file = os.path.join(CONFIG_DIR, f"{name}.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # check if process exists (raises if not)
        except (ProcessLookupError, ValueError):
            _remove_pid(name)
        except PermissionError:
            pass  # process exists but we can't signal it


# ── auth token hashing ───────────────────────────────────────────────────────

def hash_token(token):
    """Hash an auth token using SHA-256 for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(provided, stored_hash):
    """Verify a provided token against a stored SHA-256 hash."""
    return hashlib.sha256(provided.encode()).hexdigest() == stored_hash


# ── provider registry ────────────────────────────────────────────────────────

env_map = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GOOGLE_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "cerebras": ["CEREBRAS_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "avian": ["AVIAN_API_KEY"],
    "litellm": ["LITELLM_API_KEY", "LITELLM_HOST"],
    "venice": ["VENICE_API_KEY"],
    "ovhcloud": ["OVH_AI_ENDPOINTS_ACCESS_TOKEN"],
    "claude-code": ["CLAUDE_CODE_OAUTH_TOKEN"],
    "github-copilot": ["GITHUB_TOKEN"],
    "ollama": ["OLLAMA_HOST"],
    "lm-studio": [],
    "docker-model-runner": [],
    "ramalama": [],
    "azure-openai": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"],
    "custom": [],
}

default_models = {
    "anthropic": "claude-opus-4-6",
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "anthropic/claude-3.5-sonnet",
    "mistral": "mistral-large-latest",
    "xai": "grok-2-1212",
    "deepseek": "deepseek-chat",
    "together": "meta-llama/Llama-3-70b-chat-hf",
    "cerebras": "llama3.1-70b",
    "perplexity": "llama-3.1-sonar-large-128k-online",
    "avian": "gpt-4o",
    "litellm": "gpt-4o",
    "venice": "llama-3.3-70b",
    "ovhcloud": "Meta-Llama-3.1-70B-Instruct",
    "claude-code": "claude-sonnet-4-5",
    "github-copilot": "gpt-4o",
    "ollama": "llama3.2",
    "lm-studio": "local-model",
    "docker-model-runner": "ai/llama3.2",
    "ramalama": "llama3.2",
    "azure-openai": "gpt-4o",
    "custom": "custom-model",
}

provider_names = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google AI (Gemini)",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "mistral": "Mistral AI",
    "xai": "xAI (Grok)",
    "deepseek": "DeepSeek",
    "together": "Together AI",
    "cerebras": "Cerebras",
    "perplexity": "Perplexity AI",
    "avian": "Avian",
    "litellm": "LiteLLM",
    "venice": "Venice AI",
    "ovhcloud": "OVHcloud AI",
    "claude-code": "Claude Code",
    "github-copilot": "GitHub Copilot",
    "ollama": "Ollama",
    "lm-studio": "LM Studio",
    "docker-model-runner": "Docker Model Runner",
    "ramalama": "Ramalama",
    "azure-openai": "Azure OpenAI",
    "custom": "Custom Provider",
}

key_urls = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "google": "https://aistudio.google.com/app/apikey",
    "groq": "https://console.groq.com/keys",
    "openrouter": "https://openrouter.ai/settings/keys",
    "mistral": "https://console.mistral.ai/api-keys",
    "xai": "https://console.x.ai/",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "together": "https://api.together.xyz/settings/api-keys",
    "cerebras": "https://cloud.cerebras.ai/platform",
    "perplexity": "https://www.perplexity.ai/settings/api",
    "avian": "https://avian.io/",
    "litellm": "https://docs.litellm.ai/",
    "venice": "https://venice.ai/settings/api",
    "ovhcloud": "https://endpoints.ai.cloud.ovh.net/",
}


# ── telegram notification ────────────────────────────────────────────────────

GOOSE_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.yaml")

# ── in-memory pairing cache ─────────────────────────────────────────────────
# Pairings are stored on disk in config.yaml under gateway_pairings:, but goose
# sessions or apply_config() calls can race and rewrite the file, temporarily
# wiping pairings. This cache survives disk rewrites so a freshly paired user
# never gets "You are not paired" due to a race condition.
#   key: (platform, user_id_str)  value: True
_pairing_cache = {}
_pairing_cache_lock = threading.Lock()


def _cache_pairing(user_id, platform="telegram"):
    """Record a pairing in memory so it survives config.yaml rewrites."""
    with _pairing_cache_lock:
        _pairing_cache[(platform, str(user_id))] = True


def _is_cached_paired(user_id, platform="telegram"):
    """Check if a user_id is in the in-memory pairing cache."""
    with _pairing_cache_lock:
        return _pairing_cache.get((platform, str(user_id)), False)


def _re_persist_cached_pairings(config_path=None):
    """Re-inject any in-memory cached pairings missing from config.yaml on disk.

    Called after apply_config() or any other config.yaml rewrite to guard against
    race conditions that wipe the gateway_pairings section.
    """
    if config_path is None:
        config_path = GOOSE_CONFIG_PATH
    with _pairing_cache_lock:
        if not _pairing_cache:
            return
        cached = list(_pairing_cache.keys())  # list of (platform, user_id)
    try:
        content = ""
        if os.path.exists(config_path):
            with open(config_path) as f:
                content = f.read()
        missing = [(plat, uid) for plat, uid in cached if uid not in content]
        if not missing:
            return
        for plat, uid in missing:
            pairing_entry = (
                f"  - platform: {plat}\n"
                f"    user_id: '{uid}'\n"
                f"    state: paired\n"
            )
            if "gateway_pairings:" in content:
                content = content.replace(
                    "gateway_pairings:\n",
                    "gateway_pairings:\n" + pairing_entry, 1,
                )
            else:
                content = content.rstrip("\n") + "\ngateway_pairings:\n" + pairing_entry
            print(f"[gateway] re-persisted pairing for {plat}:{uid} after config rewrite")
        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, config_path)
    except Exception as e:
        print(f"[gateway] warn: could not re-persist pairings: {e}")


def get_bot_token():
    """Get telegram bot token from env, setup.json, or goose config."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    setup = None
    if os.path.exists(SETUP_FILE):
        with open(SETUP_FILE) as f:
            setup = json.load(f)
    if setup and setup.get("telegram_bot_token"):
        return setup["telegram_bot_token"]
    return ""


def get_paired_chat_ids(platform="telegram"):
    """Read paired chat IDs from goose config.yaml, filtered by platform.

    platform: the platform tag to filter by (e.g. "telegram", "telegram:research").
    """
    chat_ids = []
    if not os.path.exists(GOOSE_CONFIG_PATH):
        return chat_ids
    try:
        with open(GOOSE_CONFIG_PATH) as f:
            content = f.read()
        # lightweight yaml parse: find gateway_pairings entries matching platform
        # goose config uses simple yaml, so we can parse with basic string matching
        in_pairings = False
        current_entry = {}
        for line in content.split("\n"):
            stripped = line.strip()
            if line.startswith("gateway_pairings:"):
                in_pairings = True
                continue
            if in_pairings:
                if line and not line[0].isspace() and not stripped.startswith("-"):
                    break  # left the pairings block
                if stripped.startswith("- platform:"):
                    if current_entry.get("platform") == platform and current_entry.get("user_id"):
                        chat_ids.append(current_entry["user_id"])
                    current_entry = {"platform": stripped.split(":", 1)[1].strip()}
                elif stripped.startswith("user_id:"):
                    val = stripped.split(":", 1)[1].strip().strip("'\"")
                    current_entry["user_id"] = val
                elif stripped.startswith("state:") and "paired" in stripped:
                    current_entry["paired"] = True
        # catch last entry
        if current_entry.get("platform") == platform and current_entry.get("user_id"):
            chat_ids.append(current_entry["user_id"])
    except Exception as e:
        print(f"[gateway] warn: could not read pairings: {e}")
    # merge in-memory cache (survives config.yaml race rewrites)
    with _pairing_cache_lock:
        for (plat, uid), _ in _pairing_cache.items():
            if plat == platform and uid not in chat_ids:
                chat_ids.append(uid)
    return chat_ids


def _markdown_to_telegram_html(text):
    """Convert standard markdown to Telegram-compatible HTML.

    Telegram HTML supports: <b>, <i>, <code>, <pre>, <a>, <s>, <blockquote>, <u>.
    This is far more reliable than Telegram's legacy Markdown or MarkdownV2 modes.
    """
    # -- Step 1: extract code blocks so they don't get mangled --
    code_blocks = []
    def _save_code_block(m):
        code = m.group(2)
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        idx = len(code_blocks)
        code_blocks.append(f"<pre>{code}</pre>")
        return f"\x00CB{idx}\x00"
    text = re.sub(r'```(\w*)\n(.*?)```', _save_code_block, text, flags=re.DOTALL)
    # also handle ``` without newline after lang
    text = re.sub(r'```(\w*)(.*?)```', _save_code_block, text, flags=re.DOTALL)

    # -- Step 2: extract inline code --
    inline_codes = []
    def _save_inline(m):
        code = m.group(1)
        code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        idx = len(inline_codes)
        inline_codes.append(f"<code>{code}</code>")
        return f"\x00IC{idx}\x00"
    text = re.sub(r'`([^`\n]+)`', _save_inline, text)

    # -- Step 3: escape HTML entities in remaining text --
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # -- Step 4: headers → bold --
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # -- Step 5: bold (**text** or __text__) --
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # -- Step 6: italic (*text* or _text_) — avoid matching mid-word underscores --
    text = re.sub(r'(?<!\w)\*([^*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)

    # -- Step 7: strikethrough ~~text~~ --
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # -- Step 8: links [text](url) --
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # -- Step 9: blockquotes (> line) --
    def _convert_blockquotes(txt):
        lines = txt.split('\n')
        out = []
        bq_buf = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('&gt; '):  # was '> ' before HTML escape
                bq_buf.append(stripped[5:])
            elif stripped == '&gt;':
                bq_buf.append('')
            else:
                if bq_buf:
                    out.append('<blockquote>' + '\n'.join(bq_buf) + '</blockquote>')
                    bq_buf = []
                out.append(line)
        if bq_buf:
            out.append('<blockquote>' + '\n'.join(bq_buf) + '</blockquote>')
        return '\n'.join(out)
    text = _convert_blockquotes(text)

    # -- Step 10: tables → preformatted block --
    lines = text.split('\n')
    result = []
    table_buf = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\|.+\|$', stripped):
            # skip separator rows  |---|---|
            if re.match(r'^\|[\s\-:|\+]+\|$', stripped):
                continue
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            table_buf.append('  '.join(cells))
        else:
            if table_buf:
                result.append('<pre>' + '\n'.join(table_buf) + '</pre>')
                table_buf = []
            result.append(line)
    if table_buf:
        result.append('<pre>' + '\n'.join(table_buf) + '</pre>')
    text = '\n'.join(result)

    # -- Step 10b: strip formatting tags from inside <pre> blocks --
    # Telegram rejects HTML with <b>/<i>/etc. nested inside <pre>.
    def _clean_pre(m):
        inner = m.group(1)
        inner = re.sub(r'</?(?:b|i|s|u|a[^>]*)>', '', inner)
        return f'<pre>{inner}</pre>'
    text = re.sub(r'<pre>(.*?)</pre>', _clean_pre, text, flags=re.DOTALL)

    # -- Step 11: horizontal rules --
    text = re.sub(r'^-{3,}$', '─' * 20, text, flags=re.MULTILINE)

    # -- Step 12: restore protected blocks --
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", block)
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    return text


def _strip_html(text):
    """Strip HTML tags for plain-text fallback."""
    return re.sub(r'<[^>]+>', '', text)


MEDIA_REPLY = "i can only handle text for now. can you type what you need?"

_MEDIA_KEYS = frozenset({
    "photo", "voice", "document", "sticker",
    "video", "audio", "video_note", "animation",
})


def _has_media(msg):
    """Return True if the Telegram message contains any media attachment."""
    return any(key in msg for key in _MEDIA_KEYS)


_TELEGRAM_KIND_MAP = {
    "photo": "image", "sticker": "image", "animation": "image",
    "voice": "audio", "audio": "audio",
    "video": "video", "video_note": "video",
    "document": "document",
}

_TELEGRAM_MIME_FALLBACK = {
    "photo": "image/jpeg", "sticker": "image/webp", "animation": "video/mp4",
    "voice": "audio/ogg", "audio": "audio/mpeg",
    "video": "video/mp4", "video_note": "video/mp4",
    "document": "application/octet-stream",
}

_MIME_EXT_MAP = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
    "video/mp4": ".mp4", "application/pdf": ".pdf",
}


def _ext_from_mime(mime_type):
    """Map a MIME type to a file extension, with stdlib fallback."""
    return _MIME_EXT_MAP.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"


def _build_multipart(fields, files):
    """Construct a multipart/form-data body from text fields and binary file parts.

    Args:
        fields: dict of {name: value} text fields
        files: list of (field_name, filename, content_type, data) tuples

    Returns:
        (body_bytes, content_type_header)
    """
    boundary = uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))
    for field_name, filename, content_type, data in files:
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode())
        lines.append(f"Content-Type: {content_type}".encode())
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def _extract_file_info(msg, media_key):
    """Extract file_id, mime_type hint, and filename from Telegram message."""
    obj = msg.get(media_key)
    if not obj:
        return None, None, None
    if media_key == "photo":
        if not obj:
            return None, None, None
        return obj[-1].get("file_id"), None, None
    return obj.get("file_id"), obj.get("mime_type"), obj.get("file_name")


def _download_telegram_file(bot_token, file_id, timeout=15):
    """Download file from Telegram via getFile API. Returns (bytes, file_path) or (None, error)."""
    url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={urllib.parse.quote(str(file_id))}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            return None, f"getFile failed: {data}"
        file_path = data["result"].get("file_path", "")
        if not file_path:
            return None, "getFile returned no file_path"
    except Exception as e:
        return None, f"getFile error: {e}"
    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    try:
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            file_bytes = resp.read()
        return file_bytes, file_path
    except Exception as e:
        return None, f"download error: {e}"


def _make_media_content(media_key, file_bytes, file_path, mime_hint=None, filename=None):
    """Create MediaContent from downloaded Telegram file bytes."""
    kind = _TELEGRAM_KIND_MAP.get(media_key, "document")
    mime_type = mime_hint
    if not mime_type and file_path:
        mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = _TELEGRAM_MIME_FALLBACK.get(media_key, "application/octet-stream")
    return MediaContent(kind=kind, mime_type=mime_type, data=file_bytes, filename=filename)


def send_telegram_message(bot_token, chat_id, text):
    """Send a message via telegram bot API. Returns (ok, error).

    Converts markdown to Telegram HTML first. Falls back to plain text if
    HTML parse fails.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_text = _markdown_to_telegram_html(text)

    # split long messages (telegram limit: 4096 chars)
    def _chunk(t, limit=4000):
        if len(t) <= limit:
            return [t]
        chunks = []
        current = ""
        for line in t.split("\n"):
            if len(current) + len(line) + 1 > limit:
                if current:
                    chunks.append(current)
                current = ""
            # hard-split lines that exceed the limit on their own
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks

    html_chunks = _chunk(html_text)
    plain_chunks = _chunk(text)  # original text for fallback

    for i, chunk in enumerate(html_chunks):
        try:
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    raise ValueError("telegram returned ok=false")
        except Exception:
            # HTML failed — fall back to plain text for this chunk
            try:
                fallback = plain_chunks[i] if i < len(plain_chunks) else _strip_html(chunk)
                payload = urllib.parse.urlencode({
                    "chat_id": chat_id,
                    "text": fallback,
                    "disable_web_page_preview": "true",
                }).encode()
                req = urllib.request.Request(url, data=payload)
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                return False, str(e)
    return True, ""


def _send_telegram_msg_with_id(bot_token, chat_id, text):
    """Send a telegram message and return (message_id, error).

    Returns the message_id on success (needed for editMessageText).
    Returns (None, error_string) on failure.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_text = _markdown_to_telegram_html(text)
    try:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": html_text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return result["result"]["message_id"], ""
            raise ValueError("telegram returned ok=false")
    except Exception:
        # HTML failed, try plain text
        try:
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": text[:4000],
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if result.get("ok"):
                    return result["result"]["message_id"], ""
        except Exception as e:
            return None, str(e)
    return None, "unknown error"


def _edit_telegram_message(bot_token, chat_id, message_id, text):
    """Edit an existing telegram message. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    html_text = _markdown_to_telegram_html(text)
    try:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html_text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=payload)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        # "message is not modified" means content is already correct — not a real error.
        # This happens when the final edit sends the same text as the last streaming edit.
        err_body = ""
        if hasattr(e, 'read'):
            try:
                err_body = e.read().decode()
            except Exception:
                pass
        if "not modified" in str(e) or "not modified" in err_body:
            return True
        # HTML actually failed, try plain text
        try:
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4000],
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=payload)
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            return False


def notify_all(text, channel=None, media=None):
    """Send a message to notification channels.

    If channel is None, broadcasts to all registered channels.
    If channel is set, targets that specific channel. Falls back to notify_all
    with an error prefix if the channel is not found.

    media: optional list of media blocks (e.g. [{"type":"image","data":"...","mimeType":"image/png"}]).
    Handlers that accept media=... receive it; old-style (text-only) handlers are called without it.

    Channel names: "telegram", or "channel:<plugin_name>" for plugins (e.g. "channel:slack").
    Shorthand: just the plugin name (e.g. "slack") is also accepted.
    """
    with _notification_handlers_lock:
        handlers = list(_notification_handlers)
    if not handlers:
        return {"sent": False, "error": "no notification channels registered"}

    def _call_handler(handler_fn, text_val):
        """Call a notification handler, trying media kwarg first for backward compat."""
        if media:
            try:
                return handler_fn(text_val, media=media)
            except TypeError:
                return handler_fn(text_val)
        return handler_fn(text_val)

    if channel:
        # find the target handler -- try exact match, then "channel:<name>"
        target = None
        for h in handlers:
            if h["name"] == channel or h["name"] == f"channel:{channel}":
                target = h
                break
        if target:
            try:
                result = _call_handler(target["handler"], text)
                return {"sent": result.get("sent", False), "channels": [{"channel": target["name"], **result}]}
            except Exception as e:
                return {"sent": False, "channels": [{"channel": target["name"], "sent": False, "error": str(e)}]}
        else:
            # channel not found -- fallback to all with warning
            print(f"[notify] warn: channel '{channel}' not found, falling back to all")
            text = f"[warn: '{channel}' channel not loaded, broadcasting]\n{text}"

    results = []
    for h in handlers:
        try:
            result = _call_handler(h["handler"], text)
            results.append({"channel": h["name"], **result})
        except Exception as e:
            results.append({"channel": h["name"], "sent": False, "error": str(e)})
    return {"sent": any(r.get("sent") for r in results), "channels": results}


def _telegram_notify_handler(text, media=None):
    """Telegram notification handler -- registered with the notification bus."""
    token = get_bot_token()
    if not token:
        return {"sent": False, "error": "no bot token configured"}
    chat_ids = get_paired_chat_ids()
    if not chat_ids:
        return {"sent": False, "error": "no paired telegram users"}
    ok_all = True
    for cid in chat_ids:
        ok, err = send_telegram_message(token, cid, text)
        if not ok:
            ok_all = False
    # Route media blocks after text delivery
    if media:
        for cid in chat_ids:
            try:
                _adapter = TelegramOutboundAdapter(token, cid)
                _route_media_blocks(media, _adapter)
            except Exception as _media_exc:
                print(f"[telegram:notify] media routing error for {cid}: {_media_exc}")
    return {"sent": ok_all, "error": "" if ok_all else "some deliveries failed"}


# ── setup config management ─────────────────────────────────────────────────

def _resolve_bot_configs(config):
    """Resolve bot configurations from setup.json. Backward-compatible."""
    bots = config.get("bots")
    if isinstance(bots, list) and bots:
        return bots
    token = config.get("telegram_bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return [{"name": "default", "token": token}]
    return []


def validate_setup_config(config):
    """Validate setup config schema. Returns (valid, errors) tuple."""
    errors = []
    if not isinstance(config, dict):
        return False, ["config must be a JSON object"]

    provider = config.get("provider_type", "")
    if not provider:
        errors.append("provider_type is required")
    elif provider not in env_map:
        errors.append(f"unknown provider_type: {provider!r}")

    # provider-specific credential validation (skip local/no-key providers)
    local_providers = ("ollama", "lm-studio", "docker-model-runner", "ramalama")
    if provider in env_map and provider not in local_providers:
        if provider != "custom" and not config.get("api_key") and not config.get("claude_setup_token"):
            # check if any provider-specific env var key is provided in config
            has_cred = False
            for env_var in env_map.get(provider, []):
                if config.get(env_var.lower()):
                    has_cred = True
                    break
            if not has_cred:
                errors.append(f"api_key or provider credentials required for {provider}")

    # telegram token format check (if provided)
    tg = config.get("telegram_bot_token", "")
    if tg and ":" not in tg:
        errors.append("telegram_bot_token must be in format digits:alphanumeric")

    # timezone format check (if provided)
    tz = config.get("timezone", "")
    if tz and "/" not in tz and tz != "UTC":
        errors.append(f"timezone should be in Region/City format (got {tz!r})")

    # string field max-length guard (prevent absurdly large values)
    for field in ("api_key", "claude_setup_token", "custom_key", "custom_url", "model",
                  "lead_provider", "lead_model"):
        val = config.get(field, "")
        if isinstance(val, str) and len(val) > 2000:
            errors.append(f"{field} exceeds maximum length (2000 chars)")

    # lead/worker multi-model validation
    lead_provider = config.get("lead_provider", "")
    if lead_provider and lead_provider not in env_map:
        errors.append(f"unknown lead_provider: {lead_provider!r}")
    lead_turn_count = config.get("lead_turn_count", "")
    if lead_turn_count:
        try:
            tc = int(lead_turn_count)
            if tc < 1 or tc > 50:
                errors.append("lead_turn_count must be between 1 and 50")
        except (ValueError, TypeError):
            errors.append("lead_turn_count must be an integer")

    # models array validation
    models = config.get("models")
    if models is not None:
        if not isinstance(models, list):
            errors.append("models must be an array")
        else:
            seen_ids = set()
            default_count = 0
            for i, m in enumerate(models):
                if not isinstance(m, dict):
                    errors.append(f"models[{i}] must be an object")
                    continue
                mid = m.get("id", "")
                if not mid:
                    errors.append(f"models[{i}] missing id")
                elif mid in seen_ids:
                    errors.append(f"duplicate model id: {mid}")
                else:
                    seen_ids.add(mid)
                if not m.get("provider"):
                    errors.append(f"models[{i}] missing provider")
                if not m.get("model"):
                    errors.append(f"models[{i}] missing model")
                if m.get("is_default"):
                    default_count += 1
            if models and default_count == 0:
                errors.append("models array must have exactly one default")
            if default_count > 1:
                errors.append("only one model can be default")

    # channel_routes validation
    valid_channels = _get_valid_channels()
    channel_routes = config.get("channel_routes")
    if channel_routes is not None:
        if not isinstance(channel_routes, dict):
            errors.append("channel_routes must be an object")
        else:
            model_ids = {m.get("id") for m in (models or []) if isinstance(m, dict)}
            for ch, mid in channel_routes.items():
                if ch not in valid_channels:
                    errors.append(f"unknown channel in channel_routes: {ch!r}")
                # allow "custom:<model>" values as well as model IDs
                if mid and not mid.startswith("custom:") and mid not in model_ids:
                    errors.append(f"channel_routes[{ch!r}] references unknown model id: {mid!r}")

    # channel_verbosity validation (only messaging channels have verbosity)
    channel_verbosity = config.get("channel_verbosity")
    if channel_verbosity is not None:
        if not isinstance(channel_verbosity, dict):
            errors.append("channel_verbosity must be an object")
        else:
            valid_levels = ("quiet", "balanced", "verbose")
            valid_verb_channels = _get_valid_channels()
            for ch, level in channel_verbosity.items():
                if ch not in valid_verb_channels:
                    errors.append(f"unknown channel in channel_verbosity: {ch!r}")
                if level not in valid_levels:
                    errors.append(f"channel_verbosity[{ch!r}] must be quiet, balanced, or verbose")

    # bots array validation
    bots = config.get("bots")
    if bots is not None:
        if not isinstance(bots, list):
            errors.append("bots must be an array")
        else:
            seen_names = set()
            seen_tokens = set()
            for i, b in enumerate(bots):
                if not isinstance(b, dict):
                    errors.append(f"bots[{i}] must be an object")
                    continue
                bname = b.get("name", "")
                btoken = b.get("token", "")
                if not bname:
                    errors.append(f"bots[{i}] missing name")
                elif bname in seen_names:
                    errors.append(f"duplicate bot name: {bname!r}")
                else:
                    seen_names.add(bname)
                if not btoken:
                    errors.append(f"bots[{i}] missing token")
                elif btoken in seen_tokens:
                    errors.append(f"duplicate bot token in bots[{i}]")
                else:
                    seen_tokens.add(btoken)
                if btoken and ":" not in btoken:
                    errors.append(f"bots[{i}] token must be in format digits:alphanumeric")

    return len(errors) == 0, errors


def migrate_config_models(config):
    """Ensure config has a models array. Converts old single-model configs."""
    if not isinstance(config, dict):
        return config
    if "models" in config and isinstance(config.get("models"), list):
        return config  # already migrated

    provider = config.get("provider_type", "")
    model = config.get("model", "") or default_models.get(provider, "")
    if not provider:
        return config

    config["models"] = [{
        "id": f"{provider}_{model}".replace("/", "_").replace(".", "_")[:64],
        "provider": provider,
        "model": model,
        "is_default": True,
    }]
    config.setdefault("channel_routes", {})
    config.setdefault("channel_verbosity", {})
    config.setdefault("memory_idle_minutes", 10)
    config.setdefault("memory_writer_enabled", True)
    return config


def get_active_model(config):
    """Return the default model dict from the models array, or None."""
    for m in config.get("models", []):
        if m.get("is_default"):
            return m
    return None


def get_model_for_channel(config, channel):
    """Return the model dict assigned to a channel, falling back to default."""
    routes = config.get("channel_routes", {})
    model_id = routes.get(channel)
    if model_id:
        for m in config.get("models", []):
            if m.get("id") == model_id:
                return m
    return get_active_model(config)


def get_verbosity_for_channel(config, channel):
    """Return verbosity level for a channel. Default: 'balanced'."""
    levels = config.get("channel_verbosity", {})
    return levels.get(channel, "balanced")


def _sync_active_model_to_config(config):
    """Keep legacy provider_type/model fields in sync with the active model."""
    active = get_active_model(config)
    if active:
        config["provider_type"] = active["provider"]
        config["model"] = active["model"]
    return config


def load_setup():
    """Load setup.json, falling back to .bak if the main file is corrupted."""
    for path in (SETUP_FILE, SETUP_FILE + ".bak"):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                continue  # try next candidate
    return None


# Keys that must never be exposed via API responses.
SENSITIVE_KEYS = [
    "api_key",
    "password_hash",
    "web_auth_token_hash",
    "claude_setup_token",
    "azure_key",
    "telegram_bot_token",
    "litellm_host",
]

_REDACTED = "***REDACTED***"


def get_safe_setup():
    """Return setup config with sensitive fields replaced by a redaction marker.

    Returns None when no setup file exists. The original dict from load_setup
    is never mutated -- a shallow copy is made before redaction.
    """
    setup = load_setup()
    if setup is None:
        return None

    safe = dict(setup)

    for key in SENSITIVE_KEYS:
        if key in safe:
            safe[key] = _REDACTED

    # redact saved_keys (per-provider credential store)
    if "saved_keys" in safe and isinstance(safe["saved_keys"], dict):
        masked = {}
        for provider_id, val in safe["saved_keys"].items():
            if isinstance(val, str) and val:
                masked[provider_id] = _REDACTED
            elif isinstance(val, dict) and val:
                masked[provider_id] = {k: _REDACTED for k, v in val.items()}
            else:
                masked[provider_id] = val
        safe["saved_keys"] = masked

    return safe


def save_setup(config):
    """Atomically write config to setup.json (write tmp, then rename)."""
    import shutil
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # back up existing config before overwrite
    if os.path.exists(SETUP_FILE):
        try:
            shutil.copy2(SETUP_FILE, SETUP_FILE + ".bak")
        except Exception:
            pass  # non-fatal
    tmp_path = SETUP_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, SETUP_FILE)  # atomic on same filesystem
    except Exception:
        # clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_configured():
    """Check if an LLM provider is configured (env vars OR setup file)."""
    if os.environ.get("CLAUDE_SETUP_TOKEN"):
        return True
    if os.environ.get("GOOSE_API_KEY"):
        return True
    if os.environ.get("CUSTOM_PROVIDER_URL"):
        return True
    setup = load_setup()
    return setup is not None and setup.get("provider_type")


def _is_first_boot():
    """Return True when no setup has been completed AND no env-var provider is configured.

    During first boot, only setup-related endpoints should be accessible. All other
    API endpoints (notify, telegram, etc.) make no sense before configuration and must
    return 403 to prevent unauthenticated access to a partially-initialised system.
    """
    if os.environ.get("CLAUDE_SETUP_TOKEN"):
        return False
    if os.environ.get("GOOSE_API_KEY"):
        return False
    if os.environ.get("CUSTOM_PROVIDER_URL"):
        return False
    return load_setup() is None


def get_auth_token():
    """Get the active auth token. Returns (token_or_hash, is_hashed) tuple.

    - setup.json web_auth_token_hash (new format) -> (hash, True)
    - setup.json web_auth_token (legacy plaintext) -> (plaintext, False)
    """
    setup = load_setup()
    if setup:
        stored_hash = setup.get("web_auth_token_hash", "")
        if stored_hash:
            return stored_hash, True
        legacy = setup.get("web_auth_token", "")
        if legacy:
            return legacy, False
    return "", False


def _make_session_cookie(token):
    """Create an HMAC-based session cookie value from the auth token."""
    return hashlib.sha256(f"gooseclaw-session:{token}".encode()).hexdigest()


# ── session expiry ───────────────────────────────────────────────────────────

SESSION_MAX_AGE = 86400  # 24 hours in seconds

# server-side session store: {session_token: creation_timestamp}
_auth_sessions = {}
_auth_sessions_lock = threading.Lock()


def _create_auth_session():
    """Create a new auth session token and store it with current timestamp."""
    token = secrets.token_urlsafe(32)
    with _auth_sessions_lock:
        _auth_sessions[token] = time.time()
    return token


def _validate_auth_session(token):
    """Check if a session token exists and hasn't expired. Returns True if valid."""
    with _auth_sessions_lock:
        created = _auth_sessions.get(token)
    if created is None:
        return False
    if time.time() - created > SESSION_MAX_AGE:
        # expired, clean it up
        with _auth_sessions_lock:
            _auth_sessions.pop(token, None)
        return False
    return True


def _invalidate_all_auth_sessions():
    """Clear all active auth sessions (e.g. on password change)."""
    with _auth_sessions_lock:
        _auth_sessions.clear()


def check_auth(handler):
    """Check HTTP Basic Auth or session cookie. Returns True if authorized."""
    stored, is_hashed = get_auth_token()
    if not stored:
        return True

    # check session cookie first (avoids re-prompting Basic Auth)
    cookie_header = handler.headers.get("Cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("gooseclaw_session="):
                cookie_val = part.split("=", 1)[1]
                if _validate_auth_session(cookie_val):
                    return True

    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            _, provided = decoded.split(":", 1)
            if is_hashed:
                if verify_token(provided, stored):
                    handler._set_session_cookie = True
                    return True
            elif provided == stored:
                handler._set_session_cookie = True
                return True
        except Exception:
            pass
    return False


# ── safe setup redaction ────────────────────────────────────────────────────

_REDACTED = "***REDACTED***"

_SENSITIVE_KEYS = (
    "api_key",
    "password_hash",
    "web_auth_token_hash",
    "claude_setup_token",
    "azure_key",
    "telegram_bot_token",
    "litellm_host",
)


def get_safe_setup():
    """Return a redacted copy of setup config, safe for API responses."""
    setup = load_setup()
    if setup is None:
        return None
    safe = {**setup}
    for key in _SENSITIVE_KEYS:
        if key in safe:
            safe[key] = _REDACTED
    # redact saved_keys (provider credentials)
    if "saved_keys" in safe and isinstance(safe["saved_keys"], dict):
        redacted_keys = {}
        for provider_id, val in safe["saved_keys"].items():
            if isinstance(val, str):
                redacted_keys[provider_id] = _REDACTED
            elif isinstance(val, dict):
                redacted_keys[provider_id] = {k: _REDACTED for k in val}
            else:
                redacted_keys[provider_id] = val
        safe["saved_keys"] = redacted_keys
    return safe


# ── login page HTML ─────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GooseClaw Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --border: rgba(255,255,255,0.08);
    --text: #e2e2e8;
    --text-secondary: #8b8b9e;
    --accent: #6c63ff;
    --accent-hover: #7b73ff;
    --error: #f87171;
    --radius: 12px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .login-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 40px 36px;
    width: 100%;
    max-width: 380px;
  }
  .login-title {
    font-size: 20px;
    font-weight: 600;
    margin-bottom: 24px;
    text-align: center;
  }
  .login-field { margin-bottom: 16px; }
  .login-field label {
    display: block;
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }
  .login-field input {
    width: 100%;
    padding: 10px 14px;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
  }
  .login-field input:focus { border-color: var(--accent); }
  .login-btn {
    width: 100%;
    padding: 10px;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    margin-top: 8px;
    transition: background 0.2s;
  }
  .login-btn:hover { background: var(--accent-hover); }
  .login-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .login-error {
    color: var(--error);
    font-size: 13px;
    margin-top: 12px;
    text-align: center;
    display: none;
  }
  .login-footer {
    margin-top: 20px;
    text-align: center;
    font-size: 12px;
  }
  .login-footer a { color: var(--text-secondary); text-decoration: none; }
  .login-footer a:hover { color: var(--text); }
</style>
</head>
<body>
<div class="login-card">
  <div class="login-title">GooseClaw</div>
  <form id="loginForm" onsubmit="return doLogin(event)">
    <div class="login-field">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" placeholder="enter your password" autofocus required>
    </div>
    <button type="submit" class="login-btn" id="loginBtn">Log In</button>
    <div class="login-error" id="loginError"></div>
  </form>
  <div class="login-footer">
    <a href="/setup?recover">Lost your password?</a>
  </div>
</div>
<script>
async function doLogin(e) {
  e.preventDefault();
  const btn = document.getElementById('loginBtn');
  const errEl = document.getElementById('loginError');
  const pw = document.getElementById('password').value;
  if (!pw) return false;
  btn.disabled = true;
  errEl.style.display = 'none';
  try {
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw}),
    });
    const data = await resp.json();
    if (data.success) {
      window.location.href = '/';
    } else {
      errEl.textContent = data.error || 'Login failed';
      errEl.style.display = 'block';
    }
  } catch(ex) {
    errEl.textContent = 'Could not reach server';
    errEl.style.display = 'block';
  }
  btn.disabled = false;
  return false;
}
</script>
</body>
</html>"""


# ── provider validation ─────────────────────────────────────────────────────

def http_get(url, headers=None, timeout=10):
    """Perform a GET request with timeout. Returns (status_code, body_text)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach {url}: {e.reason}") from e
    except Exception as e:
        raise ConnectionError(f"Request failed: {e}") from e


def validate_openai_compatible(provider_id, api_key, base_url):
    """Validate an OpenAI-compatible provider via GET /v1/models."""
    name = provider_names.get(provider_id, provider_id)
    key_url = key_urls.get(provider_id, "the provider dashboard")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        status, body = http_get(f"{base_url}/v1/models", headers=headers)
        if status == 200:
            try:
                data = json.loads(body)
                count = len(data.get("data", []))
            except (json.JSONDecodeError, KeyError):
                count = 0
            return {"valid": True, "message": f"Connected to {name}. Found {count} available models."}
        elif status in (401, 403):
            return {"valid": False, "error": f"Invalid API key for {name}. Check your key at {key_url}."}
        else:
            return {"valid": False, "error": f"Unexpected response from {name} API (HTTP {status})."}
    except ConnectionError as e:
        return {"valid": False, "error": f"Cannot reach {name} API. Check your network."}


def validate_anthropic(api_key):
    """Validate Anthropic key via GET /v1/models with x-api-key header."""
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    try:
        status, _body = http_get("https://api.anthropic.com/v1/models", headers=headers)
        if status == 200:
            return {"valid": True, "message": "Connected to Anthropic. API key is valid."}
        elif status in (401, 403):
            return {"valid": False, "error": "Invalid Anthropic API key."}
        else:
            return {"valid": False, "error": f"Unexpected response from Anthropic (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Anthropic API. Check your network."}


def validate_google(api_key):
    """Validate Google AI key via generativelanguage.googleapis.com."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}"
    try:
        status, body = http_get(url)
        if status == 200:
            try:
                count = len(json.loads(body).get("models", []))
            except (json.JSONDecodeError, KeyError):
                count = 0
            return {"valid": True, "message": f"Connected to Google AI (Gemini). Found {count} models."}
        elif status in (400, 401, 403):
            return {"valid": False, "error": "Invalid Google API key."}
        else:
            return {"valid": False, "error": f"Unexpected response from Google AI (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Google AI API. Check your network."}


def validate_perplexity(api_key):
    """Validate Perplexity via a minimal chat completions test."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = json.dumps({
        "model": "llama-3.1-sonar-small-128k-online",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"valid": True, "message": "Connected to Perplexity AI. API key is valid."}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"valid": False, "error": "Invalid Perplexity API key."}
        if e.code == 400:
            return {"valid": True, "message": "Connected to Perplexity AI. API key appears valid."}
        return {"valid": False, "error": f"Unexpected response from Perplexity AI (HTTP {e.code})."}
    except urllib.error.URLError:
        return {"valid": False, "error": "Cannot reach Perplexity AI. Check your network."}


def validate_azure_openai(api_key, endpoint):
    """Validate Azure OpenAI with key + endpoint."""
    if not endpoint or not endpoint.startswith("https://"):
        return {"valid": False, "error": "Azure OpenAI endpoint must start with 'https://'."}
    url = f"{endpoint.rstrip('/')}/openai/models?api-version=2024-02-01"
    try:
        status, _body = http_get(url, headers={"api-key": api_key})
        if status == 200:
            return {"valid": True, "message": "Connected to Azure OpenAI. Credentials are valid."}
        elif status in (401, 403):
            return {"valid": False, "error": "Invalid Azure OpenAI API key or endpoint."}
        else:
            return {"valid": False, "error": f"Unexpected response from Azure OpenAI (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": "Cannot reach Azure OpenAI endpoint."}


def validate_litellm(api_key, host):
    """Validate LiteLLM proxy via GET /v1/models."""
    if not host:
        return {"valid": False, "error": "LiteLLM host URL is required. Set LITELLM_HOST."}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        status, _body = http_get(f"{host.rstrip('/')}/v1/models", headers=headers)
        if status == 200:
            return {"valid": True, "message": "Connected to LiteLLM proxy."}
        elif status in (401, 403):
            return {"valid": False, "error": "LiteLLM proxy returned auth error. Check LITELLM_API_KEY."}
        else:
            return {"valid": False, "error": f"Unexpected response from LiteLLM (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": f"Cannot reach LiteLLM at {host}."}


def validate_local_provider(provider_id, host=None):
    """Validate a local provider (ollama, lm-studio, docker-model-runner, ramalama)."""
    name = provider_names.get(provider_id, provider_id)
    defaults = {
        "ollama": "http://localhost:11434",
        "lm-studio": "http://localhost:1234",
        "docker-model-runner": "http://localhost:12434",
        "ramalama": "http://localhost:8080",
    }
    if not host:
        return {"valid": True, "message": f"No host URL configured. Default: {defaults.get(provider_id, 'http://localhost:8080')}"}
    check_url = f"{host.rstrip('/')}/api/tags" if provider_id == "ollama" else f"{host.rstrip('/')}/v1/models"
    try:
        status, body = http_get(check_url)
        if status == 200:
            count = 0
            try:
                data = json.loads(body)
                count = len(data.get("models" if provider_id == "ollama" else "data", []))
            except (json.JSONDecodeError, KeyError):
                pass
            return {"valid": True, "message": f"Connected to {name}. {count} models available."}
        else:
            return {"valid": False, "error": f"Cannot reach {name} at {host} (HTTP {status})."}
    except ConnectionError:
        return {"valid": False, "error": f"Cannot reach {host}."}


def dispatch_validation(provider, credentials):
    """Route validation to the correct handler for the given provider."""
    # Special / skip-validation providers
    if provider == "claude-code":
        return {
            "valid": True,
            "message": "Claude Code uses OAuth authentication. Run 'claude setup-token' in your local terminal to get the token. Validation must be done manually after saving.",
            "skip_validation": True,
        }
    if provider == "github-copilot":
        token = credentials.get("GITHUB_TOKEN") or credentials.get("api_key", "")
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            try:
                status, _ = http_get("https://api.github.com/copilot_internal/v2/token", headers=headers)
                if status == 200:
                    return {"valid": True, "message": "GitHub Copilot token is valid."}
                elif status in (401, 403):
                    return {"valid": False, "error": "Invalid GitHub token. Check your token or Copilot subscription."}
            except ConnectionError:
                pass
        return {"valid": True, "message": "GitHub Copilot uses device flow authentication. No pre-validation needed.", "skip_validation": True}

    # Anthropic
    if provider == "anthropic":
        key = credentials.get("ANTHROPIC_API_KEY") or credentials.get("api_key", "")
        return validate_anthropic(key) if key else {"valid": False, "error": "API key is required."}

    # Google
    if provider == "google":
        key = credentials.get("GOOGLE_API_KEY") or credentials.get("api_key", "")
        return validate_google(key) if key else {"valid": False, "error": "API key is required."}

    # Perplexity
    if provider == "perplexity":
        key = credentials.get("PERPLEXITY_API_KEY") or credentials.get("api_key", "")
        return validate_perplexity(key) if key else {"valid": False, "error": "API key is required."}

    # Avian (format-only)
    if provider == "avian":
        key = credentials.get("AVIAN_API_KEY") or credentials.get("api_key", "")
        if not key:
            return {"valid": False, "error": "API key is required."}
        if key.startswith("avian-"):
            return {"valid": True, "message": "Avian API key format is valid."}
        return {"valid": False, "error": "Avian keys must start with 'avian-'."}

    # OVHcloud (length-only)
    if provider == "ovhcloud":
        key = credentials.get("OVH_AI_ENDPOINTS_ACCESS_TOKEN") or credentials.get("api_key", "")
        if not key:
            return {"valid": False, "error": "Access token is required."}
        if len(key) > 20:
            return {"valid": True, "message": "OVHcloud AI Endpoints token appears valid."}
        return {"valid": False, "error": "OVHcloud token appears too short."}

    # Azure OpenAI
    if provider == "azure-openai":
        key = credentials.get("AZURE_OPENAI_API_KEY") or credentials.get("api_key", "")
        endpoint = credentials.get("AZURE_OPENAI_ENDPOINT") or credentials.get("azure_endpoint") or credentials.get("endpoint", "")
        if not key or not endpoint:
            return {"valid": False, "error": "Both API key and endpoint are required."}
        return validate_azure_openai(key, endpoint)

    # LiteLLM
    if provider == "litellm":
        key = credentials.get("LITELLM_API_KEY") or credentials.get("api_key", "")
        host = credentials.get("LITELLM_HOST") or credentials.get("litellm_host") or credentials.get("host", "")
        return validate_litellm(key, host)

    # Local providers
    if provider in ("ollama", "lm-studio", "docker-model-runner", "ramalama"):
        host = credentials.get("OLLAMA_HOST") or credentials.get("ollama_host") or credentials.get("host") or credentials.get("url")
        return validate_local_provider(provider, host)

    # Custom provider
    if provider == "custom":
        key = credentials.get("api_key") or credentials.get("custom_key", "")
        url = credentials.get("url") or credentials.get("custom_url", "")
        if not url:
            return {"valid": False, "error": "Custom provider URL is required."}
        return validate_openai_compatible("custom", key, url.rstrip("/")) if key else {"valid": True, "message": f"Connected to {url} (no auth)."}

    # OpenAI-compatible providers
    openai_compat = {
        "openai": "https://api.openai.com",
        "groq": "https://api.groq.com/openai",
        "openrouter": "https://openrouter.ai/api",
        "mistral": "https://api.mistral.ai",
        "xai": "https://api.x.ai",
        "deepseek": "https://api.deepseek.com",
        "together": "https://api.together.xyz",
        "cerebras": "https://api.cerebras.ai",
        "venice": "https://api.venice.ai/api",
    }
    if provider in openai_compat:
        key = credentials.get(env_map[provider][0]) or credentials.get("api_key", "")
        if not key:
            return {"valid": False, "error": "API key is required."}
        return validate_openai_compatible(provider, key, openai_compat[provider])

    return {"valid": False, "error": f"Unknown provider: {provider!r}"}


# ── dynamic model fetching ──────────────────────────────────────────────────

def fetch_provider_models(provider, credentials):
    """Fetch available models from a provider's API. Returns {models: [{id, name}], fallback?, error?}."""
    try:
        if provider == "anthropic":
            key = credentials.get("ANTHROPIC_API_KEY") or credentials.get("api_key", "")
            if not key:
                return {"models": [], "fallback": True}
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            status, body = http_get("https://api.anthropic.com/v1/models", headers=headers)
            if status != 200:
                return {"models": [], "fallback": True, "error": f"HTTP {status}"}
            data = json.loads(body)
            models = [{"id": m["id"], "name": m.get("display_name", m["id"])} for m in data.get("data", [])]
            return {"models": sorted(models, key=lambda m: m["name"])}

        if provider == "google":
            key = credentials.get("GOOGLE_API_KEY") or credentials.get("api_key", "")
            if not key:
                return {"models": [], "fallback": True}
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(key)}"
            status, body = http_get(url)
            if status != 200:
                return {"models": [], "fallback": True, "error": f"HTTP {status}"}
            data = json.loads(body)
            models = []
            for m in data.get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    mid = m.get("name", "").replace("models/", "")
                    models.append({"id": mid, "name": m.get("displayName", mid)})
            return {"models": sorted(models, key=lambda m: m["name"])}

        if provider == "azure-openai":
            key = credentials.get("AZURE_OPENAI_API_KEY") or credentials.get("api_key", "")
            endpoint = credentials.get("AZURE_OPENAI_ENDPOINT") or credentials.get("azure_endpoint") or credentials.get("endpoint", "")
            if not key or not endpoint:
                return {"models": [], "fallback": True}
            url = f"{endpoint.rstrip('/')}/openai/models?api-version=2024-02-01"
            status, body = http_get(url, headers={"api-key": key})
            if status != 200:
                return {"models": [], "fallback": True, "error": f"HTTP {status}"}
            data = json.loads(body)
            models = []
            for m in data.get("data", []):
                caps = m.get("capabilities", {})
                if caps.get("chat_completion") is True or caps.get("chat_completion") == "true":
                    models.append({"id": m["id"], "name": m["id"]})
            return {"models": sorted(models, key=lambda m: m["id"])}

        # Local providers (ollama, lm-studio, docker-model-runner, ramalama)
        if provider in ("ollama", "lm-studio", "docker-model-runner", "ramalama"):
            host = credentials.get("OLLAMA_HOST") or credentials.get("ollama_host") or credentials.get("host") or credentials.get("url")
            defaults = {
                "ollama": "http://localhost:11434",
                "lm-studio": "http://localhost:1234",
                "docker-model-runner": "http://localhost:12434",
                "ramalama": "http://localhost:8080",
            }
            host = host or defaults.get(provider, "http://localhost:8080")
            if provider == "ollama":
                url = f"{host.rstrip('/')}/api/tags"
                status, body = http_get(url)
                if status != 200:
                    return {"models": [], "fallback": True, "error": f"HTTP {status}"}
                data = json.loads(body)
                models = [{"id": m["name"], "name": m["name"]} for m in data.get("models", [])]
            else:
                url = f"{host.rstrip('/')}/v1/models"
                status, body = http_get(url)
                if status != 200:
                    return {"models": [], "fallback": True, "error": f"HTTP {status}"}
                data = json.loads(body)
                models = [{"id": m["id"], "name": m["id"]} for m in data.get("data", [])]
            return {"models": sorted(models, key=lambda m: m["id"])}

        # LiteLLM
        if provider == "litellm":
            key = credentials.get("LITELLM_API_KEY") or credentials.get("api_key", "")
            host = credentials.get("LITELLM_HOST") or credentials.get("litellm_host") or credentials.get("host", "")
            if not host:
                return {"models": [], "fallback": True}
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            status, body = http_get(f"{host.rstrip('/')}/v1/models", headers=headers)
            if status != 200:
                return {"models": [], "fallback": True, "error": f"HTTP {status}"}
            data = json.loads(body)
            models = [{"id": m["id"], "name": m["id"]} for m in data.get("data", [])]
            return {"models": sorted(models, key=lambda m: m["id"])}

        # OpenAI-compatible providers
        openai_compat = {
            "openai": "https://api.openai.com",
            "groq": "https://api.groq.com/openai",
            "openrouter": "https://openrouter.ai/api",
            "mistral": "https://api.mistral.ai",
            "xai": "https://api.x.ai",
            "deepseek": "https://api.deepseek.com",
            "together": "https://api.together.xyz",
            "cerebras": "https://api.cerebras.ai",
            "venice": "https://api.venice.ai/api",
        }
        if provider in openai_compat:
            key = credentials.get(env_map[provider][0]) or credentials.get("api_key", "")
            if not key:
                return {"models": [], "fallback": True}
            headers = {"Authorization": f"Bearer {key}"}
            status, body = http_get(f"{openai_compat[provider]}/v1/models", headers=headers)
            if status != 200:
                return {"models": [], "fallback": True, "error": f"HTTP {status}"}
            data = json.loads(body)
            raw = data.get("data", [])
            # provider-specific filtering
            if provider == "openai":
                skip = ("dall-e", "tts", "whisper", "embedding", "davinci", "babbage")
                raw = [m for m in raw if not any(s in m.get("id", "") for s in skip)]
            elif provider == "mistral":
                raw = [m for m in raw if "embed" not in m.get("id", "")]
            elif provider == "together":
                raw = [m for m in raw if m.get("type", "chat") == "chat"]
            models = [{"id": m["id"], "name": m.get("name", m["id"])} for m in raw]
            return {"models": sorted(models, key=lambda m: m["id"])}

        # Custom provider
        if provider == "custom":
            key = credentials.get("api_key") or credentials.get("custom_key", "")
            url = credentials.get("url") or credentials.get("custom_url", "")
            if not url:
                return {"models": [], "fallback": True}
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            try:
                status, body = http_get(f"{url.rstrip('/')}/v1/models", headers=headers)
                if status != 200:
                    return {"models": [], "fallback": True}
                data = json.loads(body)
                models = [{"id": m["id"], "name": m["id"]} for m in data.get("data", [])]
                return {"models": sorted(models, key=lambda m: m["id"])}
            except Exception:
                return {"models": [], "fallback": True}

        # Providers with no list endpoint
        return {"models": [], "fallback": True}

    except (ConnectionError, json.JSONDecodeError, KeyError, TypeError) as e:
        return {"models": [], "fallback": True, "error": str(e)}


# ── goosed subprocess management ──────────────────────────────────────────

def _setup_claude_cli():
    """Install claude CLI and create config if needed (for claude-code provider)."""
    home = os.environ.get("HOME", "/root")

    # ensure ~/.local/bin is in PATH
    local_bin = os.path.join(home, ".local", "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"

    # also check /root/.local/bin (claude may have been installed as root during entrypoint)
    root_local_bin = "/root/.local/bin"
    if root_local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{os.environ.get('PATH', '')}:{root_local_bin}"

    # check if already installed
    if subprocess.run(["which", "claude"], capture_output=True).returncode == 0:
        print("[gateway] claude CLI already installed")
    else:
        print("[gateway] installing claude CLI...")
        is_root = os.getuid() == 0
        try:
            subprocess.run(
                ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
                check=True, timeout=120,
            )
        except Exception:
            if is_root:
                print("[gateway] native install failed, trying npm...")
                try:
                    subprocess.run(
                        ["bash", "-c", "apt-get update -qq && apt-get install -y -qq nodejs npm >/dev/null 2>&1 && npm install -g @anthropic-ai/claude-code 2>/dev/null"],
                        check=True, timeout=180,
                    )
                except Exception as e:
                    print(f"[gateway] ERROR: could not install claude CLI: {e}")
                    return
            else:
                print("[gateway] ERROR: claude CLI install failed (running as non-root, apt not available)")
                print("[gateway] claude CLI should be pre-installed by entrypoint.sh")
                return

    # create ~/.claude.json if missing
    claude_json = os.path.join(home, ".claude.json")
    if not os.path.exists(claude_json):
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(claude_json, "w") as f:
            json.dump({"hasCompletedOnboarding": True}, f)
        print("[gateway] created ~/.claude.json")


def _extract_yaml_sections(content, section_keys):
    """Extract multi-line YAML sections from config content.

    Returns a string containing all matched top-level sections (key + nested lines).
    Used to preserve extensions: and gateway_* sections when rewriting config.yaml.
    """
    lines = content.split("\n")
    buf = []
    in_section = False
    for line in lines:
        if any(line.startswith(k) for k in section_keys):
            in_section = True
            buf.append(line)
        elif in_section:
            # still inside the section if the line is indented or a YAML list item
            if line and (line[0].isspace() or line.strip().startswith("-")):
                buf.append(line)
            else:
                in_section = False
                # check if the new line starts another section we want
                if any(line.startswith(k) for k in section_keys):
                    in_section = True
                    buf.append(line)
    return "\n".join(buf) + "\n" if buf else ""


def _write_timezone_to_user_md(tz):
    """Write timezone to the Basics section of identity/user.md."""
    user_md = os.path.join(IDENTITY_DIR, "user.md")
    try:
        with open(user_md, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return
    # update or insert timezone line under ## Basics
    tz_line = f"- Timezone: {tz}"
    if "- Timezone:" in content:
        content = re.sub(r"- Timezone:.*", tz_line, content)
    else:
        # insert after ## Basics and its comment line
        content = re.sub(
            r"(## Basics\n<!-- .+? -->\n)",
            rf"\g<1>{tz_line}\n",
            content,
        )
    with open(user_md, "w") as f:
        f.write(content)
    print(f"[config] wrote timezone {tz} to user.md")



def apply_config(config):
    """Write goose config.yaml and set env vars from setup config."""
    provider_type = config.get("provider_type", "")
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    tz = config.get("timezone", "UTC")

    # set timezone
    os.environ["TZ"] = tz
    try:
        time.tzset()
    except AttributeError:
        pass  # not available on Windows
    _write_timezone_to_user_md(tz)

    config_path = os.path.join(CONFIG_DIR, "config.yaml")

    # ── preserve existing extensions and gateway state ──
    # goose re-reads config.yaml from disk on every API call. If we strip the
    # extensions: section, the gateway detects "extensions changed" on every
    # telegram message, evicts the agent, and the session loses continuity.
    # Similarly, gateway_pairings must survive reconfiguration.
    preserved = ""
    try:
        with open(config_path) as f:
            old_content = f.read()
        preserved = _extract_yaml_sections(old_content, [
            "extensions:",
            "gateway_pairings:",
            "gateway_configs:",
            "gateway_pending_codes:",
        ])
    except FileNotFoundError:
        pass

    # base config
    lines = [
        "keyring: false",
        "GOOSE_MODE: auto",
        "GOOSE_CONTEXT_STRATEGY: summarize",
        "GOOSE_MAX_TURNS: 50",
        "GOOSE_DISABLE_SESSION_NAMING: true",
    ]

    if provider_type == "claude-code":
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = config.get("claude_setup_token", "")
        lines.append("GOOSE_PROVIDER: claude-code")
        # ensure claude CLI is installed and configured
        _setup_claude_cli()
    elif provider_type == "custom":
        url = config.get("custom_url", "")
        custom_model = config.get("custom_model", "")
        custom_key = config.get("custom_key", "")
        custom_engine = config.get("custom_engine", "openai")
        # set env var for custom API key
        if custom_key:
            os.environ["CUSTOM_API_KEY"] = custom_key
        # write custom provider json in goose's expected format
        cp_dir = os.path.join(CONFIG_DIR, "custom_providers")
        os.makedirs(cp_dir, exist_ok=True)
        with open(os.path.join(cp_dir, "custom.json"), "w") as f:
            json.dump({
                "name": "custom",
                "engine": custom_engine,
                "display_name": "Custom Endpoint",
                "api_key_env": "CUSTOM_API_KEY" if custom_key else "",
                "base_url": url,
                "models": [{"name": custom_model, "context_limit": 128000}],
                "requires_auth": bool(custom_key),
                "supports_streaming": True,
            }, f)
        lines.append("GOOSE_PROVIDER: custom")
    elif provider_type in env_map:
        # map env var names to their setup.json field names for non-standard providers
        field_map = {
            'AZURE_OPENAI_API_KEY': 'azure_key',
            'AZURE_OPENAI_ENDPOINT': 'azure_endpoint',
            'LITELLM_HOST': 'litellm_host',
            'OLLAMA_HOST': 'ollama_host',
            'GITHUB_TOKEN': 'api_key',
        }
        # set env vars for the provider from the module-level registry
        for env_var in env_map.get(provider_type, []):
            mapped_field = field_map.get(env_var, env_var.lower())
            val = config.get(mapped_field, "") or api_key
            if val:
                os.environ[env_var] = val
        lines.append(f"GOOSE_PROVIDER: {provider_type}")

    # default models per provider if none specified (from module-level registry)
    if not model:
        model = default_models.get(provider_type, "")

    # claude-code provider: always use "default" so the CLI picks its own model
    if provider_type == "claude-code":
        model = "default"

    if model:
        lines.append(f"GOOSE_MODEL: {model}")

    # lead/worker multi-model settings
    lead_provider = config.get("lead_provider", "")
    lead_model = config.get("lead_model", "")
    lead_turn_count = config.get("lead_turn_count", "")
    if lead_provider:
        lines.append(f"GOOSE_LEAD_PROVIDER: {lead_provider}")
        if lead_model:
            lines.append(f"GOOSE_LEAD_MODEL: {lead_model}")
        if lead_turn_count:
            lines.append(f"GOOSE_LEAD_TURN_COUNT: {lead_turn_count}")

    # set env vars for all saved provider keys (needed for per-channel model routing)
    saved_keys = config.get("saved_keys", {})
    for prov_id, key_val in saved_keys.items():
        if prov_id in env_map and isinstance(key_val, str) and key_val and key_val != "********":
            env_vars = env_map.get(prov_id, [])
            if env_vars:
                os.environ[env_vars[0]] = key_val

    # write base config + preserved sections atomically
    content = "\n".join(lines) + "\n"
    if preserved:
        content += preserved
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, config_path)

    # re-persist any in-memory cached pairings that may have been lost during rewrite
    _re_persist_cached_pairings(config_path)

    # propagate GOOSE_* config to env vars so goosed subprocess inherits them
    # (goosed reads env vars with highest priority, config.yaml as fallback)
    for line in lines:
        if ":" in line and line.startswith("GOOSE_"):
            key, val = line.split(":", 1)
            os.environ[key.strip()] = val.strip()

    # start all configured bots via BotManager
    bot_configs = _resolve_bot_configs(config)
    for bot_cfg in bot_configs:
        name = bot_cfg["name"]
        token = bot_cfg["token"]
        channel_key = "telegram" if name == "default" else f"telegram:{name}"
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", token)  # first token wins for backward compat
        try:
            bot = _bot_manager.add_bot(name, token, channel_key=channel_key)
            bot.start()
        except ValueError as e:
            print(f"[bot-mgr] error starting bot {name}: {e}")


def _is_goose_gateway_running():
    """Check if any Telegram bot polling thread is running."""
    return _bot_manager.any_running, []


# ── telegram session persistence ────────────────────────────────────────────

def _load_telegram_sessions():
    """Load telegram session mapping from disk via SessionManager.
    Also handles migration from old telegram_sessions.json format."""
    _session_manager.load("telegram")
    # migrate from old file format if new file doesn't exist
    old_file = os.path.join(DATA_DIR, "telegram_sessions.json")
    if os.path.exists(old_file):
        new_file = os.path.join(DATA_DIR, "sessions_telegram.json")
        if not os.path.exists(new_file):
            try:
                with open(old_file) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for chat_key, sid in data.items():
                        _session_manager.set("telegram", chat_key, sid)
                    print(f"[telegram] migrated {len(data)} sessions from old format")
            except Exception as e:
                print(f"[telegram] warn: could not migrate old sessions: {e}")


def _save_telegram_sessions():
    """Persist telegram session mapping to disk via SessionManager.
    Kept as a wrapper for backward compatibility with existing callers."""
    _session_manager._save("telegram")


# ── job engine (unified timer + script runner) ────────────────────────────
#
# Single engine handling both text reminders and script jobs.
# Text reminders fire via notify_all() directly (no subprocess).
# Script jobs run as subprocesses with timeout and output capture.
# Supports cron expressions, one-shot timers, and recurring intervals.
# Persists to /data/jobs.json. 10s tick.
#
# Job dict shape:
#   {
#     "id": str,                   # unique identifier
#     "name": str,                 # human-readable label
#     "type": "reminder"|"script", # determines execution path
#     "text": str|null,            # reminder text (type=reminder)
#     "command": str|null,         # shell command (type=script)
#     "cron": str|null,            # 5-field cron expression
#     "fire_at": float|null,       # unix timestamp for timer-based
#     "recurring_seconds": int|null, # repeat interval (null = one-shot)
#     "timeout_seconds": int,      # max execution time (default: 300)
#     "enabled": bool,
#     "notify": bool,              # send output via notify_all()
#     "notify_on_error_only": bool,# only notify on non-zero exit
#     "last_run": str|null,        # ISO timestamp
#     "last_status": str|null,     # "ok"|"error"|"timeout"
#     "last_output": str|null,     # truncated last output
#     "currently_running": bool,
#     "created_at": str,
#     "fired": bool,               # true = one-shot completed
#   }


def _load_jobs():
    """Load jobs from disk."""
    global _jobs
    try:
        if os.path.exists(_JOBS_FILE):
            with open(_JOBS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                with _jobs_lock:
                    _jobs = data
                active = sum(1 for j in data if not j.get("fired") and j.get("enabled", True))
                print(f"[jobs] loaded {len(data)} job(s) ({active} active)")
    except Exception as e:
        print(f"[jobs] warn: could not load jobs.json: {e}")


def _save_jobs():
    """Persist jobs to disk (atomic write)."""
    with _jobs_lock:
        data = list(_jobs)
    try:
        os.makedirs(os.path.dirname(_JOBS_FILE), exist_ok=True)
        tmp = _JOBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _JOBS_FILE)
    except Exception as e:
        print(f"[jobs] warn: could not save jobs.json: {e}")


def _migrate_legacy_files():
    """Migrate reminders.json and script_jobs.json into jobs.json on first run."""
    migrated = False
    reminders_file = os.path.join(DATA_DIR, "reminders.json")
    script_jobs_file = os.path.join(DATA_DIR, "script_jobs.json")

    # migrate reminders
    if os.path.exists(reminders_file):
        try:
            with open(reminders_file) as f:
                reminders = json.load(f)
            if isinstance(reminders, list):
                for r in reminders:
                    job = {
                        "id": r.get("id", str(uuid.uuid4())),
                        "name": r.get("text", "reminder")[:80],
                        "type": "reminder",
                        "text": r.get("text", ""),
                        "command": None,
                        "cron": None,
                        "fire_at": r.get("fire_at"),
                        "recurring_seconds": r.get("recurring_seconds"),
                        "timeout_seconds": 300,
                        "enabled": True,
                        "notify": True,
                        "notify_on_error_only": False,
                        "last_run": None,
                        "last_status": None,
                        "last_output": None,
                        "currently_running": False,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.get("created_at", time.time()))),
                        "fired": r.get("fired", False),
                    }
                    with _jobs_lock:
                        _jobs.append(job)
                print(f"[jobs] migrated {len(reminders)} reminder(s) from reminders.json")
                migrated = True
            os.rename(reminders_file, reminders_file + ".migrated")
        except Exception as e:
            print(f"[jobs] warn: could not migrate reminders.json: {e}")

    # migrate script jobs
    if os.path.exists(script_jobs_file):
        try:
            with open(script_jobs_file) as f:
                scripts = json.load(f)
            if isinstance(scripts, list):
                for s in scripts:
                    job = {
                        "id": s.get("id", str(uuid.uuid4())),
                        "name": s.get("name", s.get("id", "script")),
                        "type": "script",
                        "text": None,
                        "command": s.get("command", ""),
                        "cron": s.get("cron"),
                        "fire_at": None,
                        "recurring_seconds": None,
                        "timeout_seconds": s.get("timeout_seconds", 300),
                        "enabled": s.get("enabled", True),
                        "notify": s.get("notify", True),
                        "notify_on_error_only": s.get("notify_on_error_only", False),
                        "last_run": s.get("last_run"),
                        "last_status": s.get("last_status"),
                        "last_output": s.get("last_output"),
                        "currently_running": False,
                        "created_at": s.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                        "fired": False,
                    }
                    if s.get("env"):
                        job["env"] = s["env"]
                    if s.get("working_dir"):
                        job["working_dir"] = s["working_dir"]
                    with _jobs_lock:
                        _jobs.append(job)
                print(f"[jobs] migrated {len(scripts)} script(s) from script_jobs.json")
                migrated = True
            os.rename(script_jobs_file, script_jobs_file + ".migrated")
        except Exception as e:
            print(f"[jobs] warn: could not migrate script_jobs.json: {e}")

    if migrated:
        _save_jobs()


def create_job(job_data):
    """Create a new job. Returns (job_dict, error_string)."""
    job_id = job_data.get("id") or str(uuid.uuid4())

    with _jobs_lock:
        if any(j["id"] == job_id for j in _jobs):
            return None, f"job with id '{job_id}' already exists"

    job_type = job_data.get("type", "script")
    if job_type == "reminder" and not job_data.get("text"):
        return None, "text is required for reminder jobs"
    if job_type == "script" and not job_data.get("command"):
        return None, "command is required for script jobs"

    job = {
        "id": job_id,
        "name": job_data.get("name", job_id),
        "type": job_type,
        "text": job_data.get("text"),
        "command": job_data.get("command"),
        "cron": job_data.get("cron"),
        "fire_at": job_data.get("fire_at"),
        "recurring_seconds": job_data.get("recurring_seconds"),
        "timeout_seconds": job_data.get("timeout_seconds", 300),
        "enabled": job_data.get("enabled", True),
        "notify": job_data.get("notify", True),
        "notify_on_error_only": job_data.get("notify_on_error_only", False),
        "last_run": None,
        "last_status": None,
        "last_output": None,
        "currently_running": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fired": False,
        "expires_at": job_data.get("expires_at"),
        "notify_channel": job_data.get("notify_channel"),
    }
    if job_data.get("env"):
        job["env"] = job_data["env"]
    if job_data.get("working_dir"):
        job["working_dir"] = job_data["working_dir"]
    if job_data.get("model"):
        job["model"] = job_data["model"]
    if job_data.get("provider"):
        job["provider"] = job_data["provider"]

    with _jobs_lock:
        _jobs.append(job)
    _save_jobs()

    sched = job.get("cron") or (f"fire_at={job.get('fire_at')}" if job.get("fire_at") else "")
    if job.get("recurring_seconds"):
        sched += f" (every {job['recurring_seconds']}s)"
    print(f"[jobs] created: {job['name']} ({job_id}) {sched}")
    return job, ""


def update_job(job_id, updates):
    """Update an existing job. Returns (updated_job_dict, error_string)."""
    with _jobs_lock:
        job = next((j for j in _jobs if j["id"] == job_id), None)
    if not job:
        return None, f"job '{job_id}' not found"

    # validate: script jobs must keep a command
    if job.get("type", "script") == "script" and "command" in updates and not updates["command"]:
        return None, "command cannot be empty for script jobs"
    if job.get("type") == "reminder" and "text" in updates and not updates["text"]:
        return None, "text cannot be empty for reminder jobs"

    # allowed fields to update
    allowed = {"name", "command", "text", "cron", "fire_at", "recurring_seconds",
               "timeout_seconds", "enabled", "notify", "notify_on_error_only",
               "model", "provider", "env", "working_dir", "expires_at",
               "notify_channel"}
    with _jobs_lock:
        for key, val in updates.items():
            if key in allowed:
                job[key] = val
    _save_jobs()
    print(f"[jobs] updated: {job.get('name', job_id)} ({job_id})")
    return dict(job), ""


def humanize_cron(expr):
    """Convert a 5-field cron expression to a human-readable string."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    try:
        # every minute
        if all(p == "*" for p in parts):
            return "every minute"

        # hourly: 0 * * * *
        if minute != "*" and hour == "*" and dom == "*" and month == "*" and dow == "*":
            if minute == "0":
                return "every hour"
            return f"every hour at :{minute.zfill(2)}"

        # every N hours: 0 */6 * * *
        if hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
            return f"every {hour[2:]}h at :{minute.zfill(2)}"

        # build time string
        time_str = ""
        if hour != "*" and minute != "*":
            time_str = f"{hour.zfill(2)}:{minute.zfill(2)}"
        elif hour != "*":
            time_str = f"{hour.zfill(2)}:00"

        # specific date: 14 18 12 3 *
        if dom != "*" and month != "*":
            m_idx = int(month)
            m_name = months[m_idx] if 0 < m_idx <= 12 else month
            return f"{m_name} {dom} at {time_str}" if time_str else f"{m_name} {dom}"

        # weekday filter
        if dow != "*" and dom == "*" and month == "*":
            if dow == "1-5":
                dow_str = "Mon-Fri"
            elif dow == "0,6":
                dow_str = "weekends"
            else:
                # try to map individual days
                day_parts = dow.replace(",", " ").split()
                mapped = []
                for d in day_parts:
                    if d.isdigit() and 0 <= int(d) <= 6:
                        mapped.append(days[int(d)])
                    else:
                        mapped.append(d)
                dow_str = ",".join(mapped)
            return f"{dow_str} at {time_str}" if time_str else dow_str

        # daily at time
        if dom == "*" and month == "*" and dow == "*" and time_str:
            return f"daily at {time_str}"

        return expr
    except (ValueError, IndexError):
        return expr


def delete_job(job_id):
    """Delete/cancel a job by ID. Returns True if found."""
    with _jobs_lock:
        before = len(_jobs)
        _jobs[:] = [j for j in _jobs if j["id"] != job_id]
        found = len(_jobs) < before
    if found:
        _save_jobs()
        print(f"[jobs] deleted: {job_id}")
    return found


def list_active_jobs():
    """Return list of active (not fired, enabled) jobs. Returns copies to avoid mutation."""
    with _jobs_lock:
        return [dict(j) for j in _jobs if not j.get("fired") and j.get("enabled", True)]


# ── LLM-aware schedule registry ────────────────────────────────────────────
#
# Exposes schedule/cron state to the LLM so it can reason about timing,
# avoid conflicts, and proactively inform users about upcoming events.

def _next_cron_occurrence(cron_expr, after_ts=None):
    """Compute the next time a 5-field cron expression fires after a given timestamp.

    Args:
        cron_expr: 5-field cron expression (minute hour dom month dow)
        after_ts: unix timestamp to search from (default: now)

    Returns:
        Unix timestamp of next occurrence, or None if invalid/not found within 7 days.
    """
    import calendar

    if after_ts is None:
        after_ts = time.time()

    # validate first
    valid, _ = _validate_cron(cron_expr)
    if not valid:
        return None

    fields = cron_expr.strip().split()
    if len(fields) == 6:
        fields = fields[1:]
    if len(fields) != 5:
        return None

    try:
        minutes = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        days = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        weekdays = _parse_cron_field(fields[4], 0, 6)
    except (ValueError, IndexError):
        return None

    # start from the next minute after after_ts
    candidate_ts = after_ts + 60
    # zero out seconds
    t = time.gmtime(candidate_ts)
    candidate_ts = calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, 0))

    # search up to 7 days (10080 minutes)
    max_checks = 10080
    for _ in range(max_checks):
        t = time.gmtime(candidate_ts)
        cron_wday = (t.tm_wday + 1) % 7  # Python Mon=0 -> cron Sun=0
        if (t.tm_min in minutes
                and t.tm_hour in hours
                and t.tm_mday in days
                and t.tm_mon in months
                and cron_wday in weekdays):
            return candidate_ts
        candidate_ts += 60

    return None


def get_upcoming_jobs(hours=24):
    """Return all jobs that will fire within the next N hours, sorted by next_run.

    Merges both the job engine (jobs.json) and goose cron scheduler (schedule.json).
    Each result includes next_run (unix ts), next_run_human, and source ("jobs"|"schedule").
    """
    now = time.time()
    window_end = now + hours * 3600
    upcoming = []

    # 1. job engine jobs
    with _jobs_lock:
        active_jobs = [dict(j) for j in _jobs
                       if not j.get("fired") and j.get("enabled", True)]

    for job in active_jobs:
        next_run = None

        # cron-based: compute next occurrence
        cron_expr = job.get("cron")
        if cron_expr:
            next_run = _next_cron_occurrence(cron_expr, now)

        # fire_at-based (one-shot or recurring)
        fire_at = job.get("fire_at")
        if fire_at and fire_at > now:
            if next_run is None or fire_at < next_run:
                next_run = fire_at

        # recurring: if last_run exists, compute next from last_run + interval
        recurring = job.get("recurring_seconds")
        if recurring and job.get("last_run"):
            try:
                lr = job["last_run"]
                if "T" in lr:
                    import calendar as _cal
                    lr_struct = time.strptime(lr.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")
                    lr_ts = _cal.timegm(lr_struct)
                    recur_next = lr_ts + recurring
                    while recur_next <= now:
                        recur_next += recurring
                    if next_run is None or recur_next < next_run:
                        next_run = recur_next
            except (ValueError, OverflowError):
                pass

        if next_run and next_run <= window_end:
            entry = {
                "id": job["id"],
                "name": job.get("name", job["id"]),
                "type": job.get("type", "script"),
                "next_run": next_run,
                "next_run_human": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(next_run)),
                "next_run_relative": _relative_time(next_run - now),
                "source": "jobs",
            }
            if cron_expr:
                entry["cron"] = cron_expr
                entry["cron_human"] = humanize_cron(cron_expr)
            if job.get("text"):
                entry["description"] = job["text"][:200]
            elif job.get("command"):
                entry["description"] = job["command"][:200]
            if job.get("last_run"):
                entry["last_run"] = job["last_run"]
            if job.get("last_status"):
                entry["last_status"] = job["last_status"]
            upcoming.append(entry)

    # 2. goose schedule.json cron jobs
    try:
        schedule_jobs = _load_schedule()
        for sj in schedule_jobs:
            if sj.get("paused"):
                continue
            cron_expr = sj.get("cron", "")
            if not cron_expr:
                continue
            next_run = _next_cron_occurrence(cron_expr, now)
            if next_run and next_run <= window_end:
                entry = {
                    "id": sj.get("id", "unknown"),
                    "name": sj.get("id", "goose-cron"),
                    "type": "goose-recipe",
                    "next_run": next_run,
                    "next_run_human": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(next_run)),
                    "next_run_relative": _relative_time(next_run - now),
                    "source": "schedule",
                }
                if cron_expr:
                    entry["cron"] = cron_expr
                    entry["cron_human"] = humanize_cron(cron_expr)
                if sj.get("source"):
                    entry["recipe"] = sj["source"]
                if sj.get("last_run"):
                    entry["last_run"] = sj["last_run"]
                upcoming.append(entry)
    except Exception as e:
        print(f"[schedule] warn: could not load goose schedule: {e}")

    # sort by next_run
    upcoming.sort(key=lambda x: x["next_run"])
    return upcoming


def _relative_time(seconds):
    """Convert seconds delta to human-readable relative time string."""
    if seconds < 0:
        return "overdue"
    if seconds < 60:
        return "less than a minute"
    mins = int(seconds / 60)
    if mins < 60:
        return f"{mins}m"
    hours = int(mins / 60)
    remaining_mins = mins % 60
    if hours < 24:
        if remaining_mins:
            return f"{hours}h {remaining_mins}m"
        return f"{hours}h"
    days = int(hours / 24)
    remaining_hours = hours % 24
    if remaining_hours:
        return f"{days}d {remaining_hours}h"
    return f"{days}d"


def get_schedule_context(hours=24):
    """Generate an LLM-consumable text summary of the current schedule.

    Returns a human-readable string the LLM can use to reason about timing.
    """
    upcoming = get_upcoming_jobs(hours=hours)

    if not upcoming:
        return f"No scheduled jobs in the next {hours} hours."

    lines = [f"Scheduled jobs (next {hours}h):"]
    for i, job in enumerate(upcoming, 1):
        name = job.get("name", job["id"])
        when = job.get("next_run_relative", "unknown")
        time_str = job.get("next_run_human", "")
        desc = job.get("description", "")
        cron_human = job.get("cron_human", "")

        line = f"  {i}. {name}"
        if cron_human:
            line += f" ({cron_human})"
        line += f" - fires in {when}"
        if time_str:
            line += f" [{time_str}]"
        if desc:
            line += f"\n     {desc}"
        lines.append(line)

    lines.append(f"\nTotal: {len(upcoming)} job(s) upcoming")
    return "\n".join(lines)


def _fix_goose_run_recipe(command):
    """If command is 'goose run --recipe <path>' without --text, extract recipe
    instructions and inject --text so it works in headless mode."""
    if "goose run" not in command or "--recipe" not in command:
        return command
    if "--text" in command or "--instructions" in command:
        return command  # already has text, leave it alone

    # extract recipe path from command
    parts = command.split()
    recipe_path = None
    for i, p in enumerate(parts):
        if p == "--recipe" and i + 1 < len(parts):
            recipe_path = parts[i + 1]
            break
    if not recipe_path or not os.path.exists(recipe_path):
        return command

    instructions = _load_recipe(recipe_path)
    if not instructions:
        return command

    # replace --recipe <path> with --text <instructions> (goose doesn't allow both)
    import shlex
    new_parts = []
    skip_next = False
    for i, p in enumerate(parts):
        if skip_next:
            skip_next = False
            continue
        if p == "--recipe" and i + 1 < len(parts):
            skip_next = True
            continue
        new_parts.append(p)
    new_parts.extend(["--text", shlex.quote(instructions)])
    print(f"[jobs] replaced --recipe with --text for headless goose run")
    return " ".join(new_parts)


def _resolve_job_model(job):
    """Resolve a job's model field to (model_name, provider_id) or (None, None)."""
    model_ref = job.get("model")
    if not model_ref:
        return None, None
    # custom:modelname — use that model name with the default provider
    if model_ref.startswith("custom:"):
        return model_ref[7:], None
    # model config ID — look up in setup
    setup = load_setup()
    if setup:
        for m in setup.get("models", []):
            if m.get("id") == model_ref:
                return m.get("model"), m.get("provider")
    return model_ref, None  # treat as raw model name


def _strip_goose_preamble(text):
    """Strip the goose startup banner and thinking preamble from output.

    Goose prints an ASCII duck banner + session info before actual output.
    This strips everything up to and including the 'goose is ready' line,
    plus any short "thinking" lines before the first content separator or
    substantive content.
    """
    if not text:
        return text
    lines = text.split("\n")
    # find the last banner line ("goose is ready" or the duck art)
    banner_end = -1
    for i, line in enumerate(lines):
        if "goose is ready" in line or "__( O)>" in line or "\\____)" in line or (line.strip().startswith("L L") and i < 10):
            banner_end = i
    if banner_end == -1:
        return text  # no banner found
    # skip past banner
    rest = lines[banner_end + 1:]
    # skip short "thinking" lines until we hit a separator or substantial content
    start = 0
    for i, line in enumerate(rest):
        stripped = line.strip()
        # content separator (─── or === or ---)
        if stripped and all(c in "\u2500\u2501\u2550=-" for c in stripped) and len(stripped) >= 4:
            start = i
            break
        # substantial content line (long enough, not just "Let me..." filler)
        if len(stripped) > 80:
            start = i
            break
        # heading-like content (starts with emoji + caps, or markdown #)
        if stripped and (stripped[0] == "#" or (len(stripped) > 5 and stripped[0].encode("utf-8")[0] > 127)):
            start = i
            break
    else:
        start = 0  # no separator found, keep everything after banner
    return "\n".join(rest[start:]).strip()


def _run_script(job):
    """Execute a script job as a subprocess. Capture output, enforce timeout."""
    job_id = job.get("id", "unknown")
    job_name = job.get("name", job_id)
    command = _fix_goose_run_recipe(job.get("command", ""))
    timeout = job.get("timeout_seconds", 300)
    working_dir = job.get("working_dir", "/data")
    extra_env = job.get("env") or {}

    # resolve per-job model/provider overrides
    model_name, _provider_id = _resolve_job_model(job)
    job_provider = job.get("provider")
    if "goose" in command:
        if job_provider:
            command = re.sub(r'(goose\s+run\b)', rf'\1 --provider {job_provider}', command)
            print(f"[jobs] provider override: {job_provider}")
        if model_name:
            command = re.sub(r'(goose\s+run\b)', rf'\1 --model {model_name}', command)
            print(f"[jobs] model override: {model_name}")

    print(f"[jobs] firing script: {job_name} ({job_id})")

    env = dict(os.environ)
    env.update(extra_env)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            env=env,
        )

        output = result.stdout.strip()
        stderr = result.stderr.strip()
        exit_code = result.returncode

        # strip goose startup banner from output (always safe, no-ops if no banner)
        output = _strip_goose_preamble(output)

        if exit_code != 0:
            status = "error"
            full_output = f"exit code {exit_code}"
            if stderr:
                full_output += f"\nstderr: {stderr}"
            if output:
                full_output += f"\nstdout: {output}"
        else:
            status = "ok"
            full_output = output or "(no output)"

    except subprocess.TimeoutExpired:
        status = "timeout"
        full_output = f"killed after {timeout}s timeout"

    except Exception as e:
        status = "error"
        full_output = f"execution error: {e}"

    # truncate only extreme output (send_telegram_message handles chunking)
    if len(full_output) > 64000:
        full_output = full_output[:63997] + "..."

    # notify
    should_notify = job.get("notify", True)
    error_only = job.get("notify_on_error_only", False)
    if should_notify and full_output:
        if not error_only or status != "ok":
            prefix = {"ok": "", "error": "[ERROR] ", "timeout": "[TIMEOUT] "}.get(status, "")
            msg = f"[{job_name}] {prefix}{full_output}"
            notify_all(msg, channel=job.get("notify_channel"))

    print(f"[jobs] {job_name}: {status} ({len(full_output)} chars)")
    return status, full_output


def _fire_reminder(job):
    """Fire a text reminder via notify_all. Returns (status, output)."""
    text = job.get("text", job.get("name", ""))
    emoji = "\U0001f501" if job.get("recurring_seconds") else "\U0001f514"
    msg = f"{emoji} Reminder: {text}"
    try:
        result = notify_all(msg, channel=job.get("notify_channel"))
        if result.get("sent"):
            print(f"[jobs] fired reminder: '{text}'")
            return "ok", msg
        else:
            print(f"[jobs] reminder delivery failed: {result.get('error', '?')}")
            return "error", result.get("error", "delivery failed")
    except Exception as e:
        print(f"[jobs] reminder error: {e}")
        return "error", str(e)


def _job_engine_loop():
    """Background loop: check jobs every 10s, fire when due."""
    global _job_engine_running
    _job_engine_running = True
    print(f"[jobs] engine started ({_JOBS_TICK_SECONDS}s tick)")

    while _job_engine_running:
        try:
            now = time.time()
            now_local = time.localtime(now)
            save_needed = False

            with _jobs_lock:
                jobs_snapshot = list(_jobs)

            running_count = sum(1 for j in jobs_snapshot if j.get("currently_running"))

            for job in jobs_snapshot:
                # check expiry -- treat expired jobs like fired one-shots
                exp = job.get("expires_at")
                if exp and exp <= now:
                    if not job.get("fired"):
                        job["fired"] = True
                        job["last_status"] = "expired"
                        job["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        save_needed = True
                        print(f"[jobs] expired: {job.get('name', job.get('id', '?'))}")
                    continue

                if not job.get("enabled", True):
                    continue
                if job.get("fired"):
                    continue
                if job.get("currently_running"):
                    continue

                should_fire = False

                # check cron schedule
                cron_expr = job.get("cron")
                if cron_expr:
                    if _cron_matches_now(cron_expr, now_local):
                        # double-fire prevention
                        last_run = job.get("last_run", "")
                        if last_run:
                            try:
                                if "T" in last_run:
                                    lr_time = last_run.split("T")[1][:5]
                                    now_time = time.strftime("%H:%M", now_local)
                                    if lr_time == now_time:
                                        continue
                            except Exception:
                                pass
                        should_fire = True

                # check fire_at (timer-based)
                fire_at = job.get("fire_at")
                if fire_at and not cron_expr:
                    if fire_at <= now:
                        should_fire = True

                if not should_fire:
                    continue

                # script jobs: run in thread (may be slow)
                if job.get("command"):
                    if running_count >= _MAX_CONCURRENT_JOBS:
                        print(f"[jobs] skip {job.get('id', '?')}: max concurrent ({_MAX_CONCURRENT_JOBS}) reached")
                        break

                    job["currently_running"] = True
                    running_count += 1
                    save_needed = True

                    def _run_threaded(j):
                        try:
                            status, output = _run_script(j)
                            j["last_status"] = status
                            j["last_output"] = output[:500]
                        finally:
                            j["currently_running"] = False
                            j["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            # handle scheduling for next run
                            if j.get("recurring_seconds") and j.get("fire_at"):
                                while j["fire_at"] <= time.time():
                                    j["fire_at"] += j["recurring_seconds"]
                            elif j.get("fire_at") and not j.get("cron"):
                                j["fired"] = True
                            _save_jobs()

                    threading.Thread(target=_run_threaded, args=(job,), daemon=True).start()

                else:
                    # reminder: fire inline (instant, no subprocess)
                    status, output = _fire_reminder(job)
                    job["last_status"] = status
                    job["last_output"] = output[:500]
                    job["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                    # handle scheduling for next run
                    if job.get("recurring_seconds") and job.get("fire_at"):
                        while job["fire_at"] <= now:
                            job["fire_at"] += job["recurring_seconds"]
                    elif job.get("fire_at") and not job.get("cron"):
                        job["fired"] = True

                    save_needed = True

            if save_needed:
                _save_jobs()

            # prune old fired one-shot jobs (> 24h)
            cutoff_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86400))
            with _jobs_lock:
                before = len(_jobs)
                _jobs[:] = [
                    j for j in _jobs
                    if not j.get("fired") or (j.get("last_run", "") > cutoff_ts)
                ]
                pruned = before - len(_jobs)
            if pruned > 0:
                print(f"[jobs] pruned {pruned} expired one-shot job(s)")
                _save_jobs()

        except Exception as e:
            print(f"[jobs] error: {e}")

        # sleep 10s, checking shutdown every 2s
        for _ in range(5):
            if not _job_engine_running:
                break
            time.sleep(2)

    print("[jobs] engine stopped")


def start_job_engine():
    """Start the job engine daemon thread."""
    global _job_engine_running
    if _job_engine_running:
        return
    _load_jobs()
    _migrate_legacy_files()
    threading.Thread(target=_job_engine_loop, daemon=True).start()


# ── watcher engine (event subscriptions: webhook, feed, stream) ──────────────


def _load_watchers():
    """Load watchers from disk."""
    global _watchers
    try:
        if os.path.exists(_WATCHERS_FILE):
            with open(_WATCHERS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                with _watchers_lock:
                    _watchers = data
                print(f"[watchers] loaded {len(data)} watcher(s)")
    except Exception as e:
        print(f"[watchers] warn: could not load watchers.json: {e}")


def _save_watchers():
    """Persist watchers to disk (atomic write)."""
    with _watchers_lock:
        data = list(_watchers)
    try:
        os.makedirs(os.path.dirname(_WATCHERS_FILE), exist_ok=True)
        tmp = _WATCHERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _WATCHERS_FILE)
    except Exception as e:
        print(f"[watchers] warn: could not save watchers.json: {e}")


def create_watcher(data, _save=True):
    """Create a new watcher. Returns (watcher_dict, error_string).

    When _save=False the caller is responsible for calling _save_watchers()
    after the batch is complete (used by batch-create to avoid N disk writes).
    """
    watcher_id = data.get("id") or str(uuid.uuid4())[:8]

    with _watchers_lock:
        if any(w["id"] == watcher_id for w in _watchers):
            return None, f"watcher with id '{watcher_id}' already exists"

    watcher_type = data.get("type", "webhook")
    if watcher_type not in ("webhook", "feed", "stream"):
        return None, f"invalid type: {watcher_type} (must be webhook, feed, or stream)"

    if watcher_type == "feed" and not data.get("source"):
        return None, "source URL is required for feed watchers"

    watcher = {
        "id": watcher_id,
        "name": data.get("name", watcher_id),
        "type": watcher_type,
        "source": data.get("source", f"/api/webhooks/{watcher_id}"),
        "channel": data.get("channel"),
        "smart": data.get("smart", False),
        "transform": data.get("transform", ""),
        "prompt": data.get("prompt", ""),
        "enabled": data.get("enabled", True),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "poll_seconds": data.get("poll_seconds", 300),
        "filter": data.get("filter", ""),
        "headers": data.get("headers", {}),
        "webhook_secret": data.get("webhook_secret", ""),
        "last_hash": "",
        "last_check": None,
        "last_fired": None,
        "fire_count": 0,
        "last_error": None,
    }

    with _watchers_lock:
        _watchers.append(watcher)
    if _save:
        _save_watchers()
    print(f"[watchers] created: {watcher['name']} ({watcher_id}) type={watcher_type}")
    return watcher, ""


def delete_watcher(watcher_id):
    """Delete a watcher by ID. Returns True if found."""
    with _watchers_lock:
        before = len(_watchers)
        _watchers[:] = [w for w in _watchers if w["id"] != watcher_id]
        found = len(_watchers) < before
    if found:
        _save_watchers()
        print(f"[watchers] deleted: {watcher_id}")
    return found


def list_watchers():
    """Return a copy of all watchers."""
    with _watchers_lock:
        return [dict(w) for w in _watchers]


def update_watcher(watcher_id, updates):
    """Update an existing watcher. Returns (updated_watcher_dict, error_string)."""
    with _watchers_lock:
        watcher = next((w for w in _watchers if w["id"] == watcher_id), None)
    if not watcher:
        return None, f"watcher '{watcher_id}' not found"

    allowed = {"name", "enabled", "transform", "prompt", "channel", "filter",
               "poll_seconds", "headers", "webhook_secret"}
    with _watchers_lock:
        for key, val in updates.items():
            if key in allowed:
                watcher[key] = val
    _save_watchers()
    print(f"[watchers] updated: {watcher.get('name', watcher_id)} ({watcher_id})")
    return dict(watcher), ""


def _flatten_dict(d, prefix="", sep="_"):
    """Flatten nested dict: {"a": {"b": 1}} -> {"a_b": "1", "b": "1"}."""
    items = {}
    for k, v in d.items():
        key = f"{prefix}{sep}{k}" if prefix else k
        if isinstance(v, dict):
            items.update(_flatten_dict(v, key, sep))
        else:
            items[str(k)] = str(v)  # leaf key (unflattened)
            items[key] = str(v)     # full path key
    return items


def _convert_double_braces(tmpl):
    """Convert {{var}} syntax to ${var} for string.Template."""
    return re.sub(r'\{\{(\w+)\}\}', r'${\1}', tmpl)


def _evaluate_filter(filter_str, data_dict):
    """Evaluate a filter expression against a data dict.

    Returns True if the event should pass through (be delivered).
    Returns True on empty filter, parse error, or missing field (safe default).

    Supported operators:
      contains, not_contains     - case-insensitive substring
      equals, not_equals         - exact string match
      matches                    - regex match
      gt, lt, gte, lte           - numeric comparison
    """
    if not filter_str or not filter_str.strip():
        return True

    try:
        # Parse: "field operator 'value'" or "field operator number"
        # Match quoted value or bare number
        m = re.match(
            r"(\S+)\s+(contains|not_contains|equals|not_equals|matches|gt|lt|gte|lte)\s+'([^']*)'$",
            filter_str.strip()
        )
        if not m:
            # Try bare numeric value (no quotes)
            m = re.match(
                r"(\S+)\s+(gt|lt|gte|lte)\s+([0-9.eE+-]+)$",
                filter_str.strip()
            )
        if not m:
            return True  # unparseable -> pass

        field, operator, value = m.group(1), m.group(2), m.group(3)

        # Look up field in data dict
        if field not in data_dict:
            return True  # missing field -> pass

        actual = data_dict[field]

        if operator == "contains":
            return value.lower() in actual.lower()
        elif operator == "not_contains":
            return value.lower() not in actual.lower()
        elif operator == "equals":
            return actual == value
        elif operator == "not_equals":
            return actual != value
        elif operator == "matches":
            return bool(re.search(value, actual))
        elif operator in ("gt", "lt", "gte", "lte"):
            try:
                actual_num = float(actual)
                value_num = float(value)
            except (ValueError, TypeError):
                return True  # non-numeric -> pass
            if operator == "gt":
                return actual_num > value_num
            elif operator == "lt":
                return actual_num < value_num
            elif operator == "gte":
                return actual_num >= value_num
            elif operator == "lte":
                return actual_num <= value_num

        return True  # unknown operator -> pass
    except Exception:
        return True  # any error -> pass


def _process_passthrough(watcher, data):
    """Tier 1: template transform, no LLM. Returns formatted string."""
    tmpl = watcher.get("transform", "")
    if not tmpl:
        return json.dumps(data, indent=2)[:2000]

    tmpl = _convert_double_braces(tmpl)
    flat = _flatten_dict(data)
    t = string.Template(tmpl)
    return t.safe_substitute(flat)


def _process_smart(watcher, data):
    """Tier 2: LLM processing with session reuse. Returns response string."""
    prompt = watcher.get("prompt", "")
    payload_text = json.dumps(data)[:4000]
    user_text = f"{prompt}\n\nData:\n{payload_text}" if prompt else payload_text

    session_id = watcher.get("_session_id")

    # Create session if none stored
    if not session_id:
        session_id = _create_goose_session()
        if not session_id:
            return "Error: could not create goose session"
        watcher["_session_id"] = session_id

    # Relay to LLM
    response, error, _media = _relay_to_goosed(
        user_text, session_id, channel=watcher.get("channel"))

    # Handle stale session: retry once with fresh session
    if error and any(hint in error.lower() for hint in ("session not found", "session expired")):
        watcher.pop("_session_id", None)
        session_id = _create_goose_session()
        if not session_id:
            return "Error: could not create goose session"
        watcher["_session_id"] = session_id
        response, error, _media = _relay_to_goosed(
            user_text, session_id, channel=watcher.get("channel"))

    if error:
        return f"Error: {error}"
    return response


def _fire_watcher(watcher, data):
    """Dispatch watcher event to correct tier and deliver via notify_all."""
    try:
        # Passthrough filter: evaluate before processing (smart tier skips this)
        filter_str = watcher.get("filter")
        if filter_str and not watcher.get("smart"):
            flat = _flatten_dict(data) if isinstance(data, dict) else {}
            if not _evaluate_filter(filter_str, flat):
                _save_watchers()
                return

        if watcher.get("smart"):
            message = _process_smart(watcher, data)
        else:
            message = _process_passthrough(watcher, data)

        if not message:
            _save_watchers()
            return

        watcher_name = watcher.get("name", watcher.get("id", "watcher"))
        full_message = f"[{watcher_name}] {message}"
        notify_all(full_message, channel=watcher.get("channel"))

        watcher["fire_count"] = watcher.get("fire_count", 0) + 1
        watcher["last_fired"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        watcher["last_error"] = None
    except Exception as e:
        watcher["last_error"] = str(e)
        print(f"[watchers] error firing {watcher.get('name', '?')}: {e}")

    _save_watchers()


def _verify_webhook_signature(secret, body_bytes, signature_header):
    """Verify HMAC-SHA256 webhook signature. Returns True if valid."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


def _handle_webhook_incoming(webhook_name, body, headers=None):
    """Route incoming webhook to matching watchers. Returns count of matched watchers."""
    # Parse body
    if isinstance(body, bytes):
        body_str = body.decode("utf-8", errors="replace")
    else:
        body_str = body
    body_bytes = body_str.encode("utf-8") if isinstance(body_str, str) else body

    try:
        payload = json.loads(body_str)
    except (json.JSONDecodeError, ValueError):
        payload = {"raw": body_str}

    headers = headers or {}

    # Find matching watchers
    with _watchers_lock:
        candidates = [w for w in _watchers
                      if w.get("type") == "webhook"
                      and w.get("enabled", True)
                      and w.get("source", "").endswith(webhook_name)]

    matched = []
    for w in candidates:
        # HMAC verification if secret is set
        secret = w.get("webhook_secret", "")
        if secret:
            sig = headers.get("X-Hub-Signature-256", "")
            if not _verify_webhook_signature(secret, body_bytes, sig):
                print(f"[watchers] webhook HMAC mismatch for {w.get('name')}")
                continue
        matched.append(w)

    # Fire each in a daemon thread
    for w in matched:
        t = threading.Thread(target=_fire_watcher, args=(w, payload), daemon=True)
        t.start()

    return len(matched)


def _parse_rss(content):
    """Parse RSS 2.0 or Atom feed content into list of item dicts."""
    try:
        if isinstance(content, bytes):
            root = ET.fromstring(content.decode("utf-8", errors="replace"))
        else:
            root = ET.fromstring(content)
    except ET.ParseError:
        return []

    items = []

    # RSS 2.0: channel/item elements
    for item in root.iter("item"):
        entry = {}
        title_el = item.find("title")
        if title_el is not None and title_el.text:
            entry["title"] = title_el.text
        link_el = item.find("link")
        if link_el is not None and link_el.text:
            entry["link"] = link_el.text
        desc_el = item.find("description")
        if desc_el is not None and desc_el.text:
            entry["description"] = desc_el.text[:500]
        if entry:
            items.append(entry)

    # Atom: entry elements (if no RSS items found)
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry_el in root.iter("{http://www.w3.org/2005/Atom}entry"):
            entry = {}
            title_el = entry_el.find("atom:title", ns)
            if title_el is None:
                title_el = entry_el.find("{http://www.w3.org/2005/Atom}title")
            if title_el is not None and title_el.text:
                entry["title"] = title_el.text
            link_el = entry_el.find("atom:link", ns)
            if link_el is None:
                link_el = entry_el.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                entry["link"] = link_el.get("href", "")
            summary_el = entry_el.find("atom:summary", ns)
            if summary_el is None:
                summary_el = entry_el.find("{http://www.w3.org/2005/Atom}summary")
            if summary_el is not None and summary_el.text:
                entry["description"] = summary_el.text[:500]
            if entry:
                items.append(entry)

    return items


def _parse_feed_content(content, url=""):
    """Parse feed content: try JSON, then RSS/Atom, then raw text."""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = content

    # Try JSON
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try RSS/Atom
    rss_items = _parse_rss(content)
    if rss_items:
        return rss_items

    # Fall back to raw text
    return {"text": text[:2000]}


def _check_feed_watcher(watcher):
    """Check a feed watcher for content changes. Fires if hash differs."""
    url = watcher.get("source", "")
    if not url:
        return

    try:
        resp = urllib.request.urlopen(url, timeout=30)
        content = resp.read()
    except Exception as e:
        watcher["last_error"] = str(e)
        watcher["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_watchers()
        print(f"[watchers] feed error {watcher.get('name')}: {e}")
        return

    content_hash = hashlib.sha256(content).hexdigest()
    watcher["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if content_hash == watcher.get("last_hash", ""):
        _save_watchers()
        return

    watcher["last_hash"] = content_hash

    # Parse content
    data = _parse_feed_content(content, url)

    # Apply regex filter on list items
    filter_pattern = watcher.get("filter", "")
    if filter_pattern and isinstance(data, list):
        try:
            pattern = re.compile(filter_pattern, re.IGNORECASE)
            data = [item for item in data
                    if pattern.search(json.dumps(item) if isinstance(item, dict) else str(item))]
        except re.error:
            pass  # invalid regex, skip filtering

    _fire_watcher(watcher, data)


# ── watcher engine loop ──────────────────────────────────────────────────────

_WATCHER_TICK_SECONDS = 30


def _watcher_engine_tick():
    """One iteration of the watcher engine: check due feed watchers."""
    import random as _random
    try:
        now = time.time()
        with _watchers_lock:
            candidates = [w for w in _watchers
                          if w.get("type") == "feed"
                          and w.get("enabled", True)]

        for w in candidates:
            poll_seconds = w.get("poll_seconds", 300)
            last_ts = w.get("last_check_ts", 0)

            # Jitter on first poll: stagger initial checks
            if last_ts == 0:
                jitter = _random.randint(0, min(poll_seconds, 60))
                w["last_check_ts"] = now - poll_seconds + jitter
                last_ts = w["last_check_ts"]

            if now - last_ts >= poll_seconds:
                w["last_check_ts"] = now
                t = threading.Thread(target=_check_feed_watcher, args=(w,), daemon=True)
                t.start()
    except Exception as e:
        print(f"[watchers] engine tick error: {e}", file=sys.stderr)


def _watcher_engine_loop():
    """Background loop that periodically ticks the watcher engine."""
    global _watcher_engine_running
    _watcher_engine_running = True
    print("[watchers] engine started")
    while _watcher_engine_running:
        _watcher_engine_tick()
        # Sleep in short increments so we can stop promptly
        for _ in range(6):
            if not _watcher_engine_running:
                break
            time.sleep(5)
    print("[watchers] engine stopped")


def start_watcher_engine():
    """Start the watcher engine background thread."""
    global _watcher_engine_running
    if _watcher_engine_running:
        return
    with _watchers_lock:
        count = len(_watchers)
    if count == 0:
        return
    t = threading.Thread(target=_watcher_engine_loop, daemon=True)
    t.start()
    print(f"[watchers] engine started with {count} watcher(s)")


def stop_watcher_engine():
    """Stop the watcher engine background thread."""
    global _watcher_engine_running
    _watcher_engine_running = False


# ── cron scheduler (channel-agnostic, reads goose schedule.json) ─────────────
#
# Replaces goose's built-in scheduler (which only runs inside `goose gateway`,
# not `goosed`). Reads the same schedule.json that `goose schedule add`
# writes to, so existing CLI commands work transparently.
#
# Architecture (mirrors OpenClaw's approach):
#   - scheduler loop runs inside gateway.py (not the LLM)
#   - each job fires in an isolated goosed session (fresh session per run)
#   - output delivered via notify_all() (channel-agnostic bus)
#   - anyone building a slack/whatsapp/discord gateway just registers a handler
#
# On each tick (30s):
#   1. read schedule.json
#   2. for each job where now >= next_run: fire it
#   3. firing = read recipe YAML -> relay instructions to goosed -> notify_all()
#   4. update last_run, write schedule.json back

_cron_scheduler_running = False
_GOOSE_SHARE_DIR = os.path.join(
    os.environ.get("HOME", "/home/gooseclaw"), ".local", "share", "goose"
)
_SCHEDULE_FILE = os.path.join(_GOOSE_SHARE_DIR, "schedule.json")
_CRON_TICK_SECONDS = 30


def _parse_cron_field(field, min_val, max_val):
    """Parse a single cron field into a set of valid integers."""
    values = set()
    for part in field.split(","):
        part = part.strip()
        # handle */N (step)
        if part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        elif "-" in part:
            # range: 1-5
            lo, hi = part.split("-", 1)
            values.update(range(int(lo), int(hi) + 1))
        else:
            values.add(int(part))
    return values


def _validate_cron(cron_expr):
    """Validate a 5-field cron expression. Returns (True, "") or (False, error_message)."""
    fields = cron_expr.strip().split()
    if len(fields) == 6:
        fields = fields[1:]
    if len(fields) != 5:
        return False, f"expected 5 fields (min hour dom month dow), got {len(fields)}"
    labels = ["minute (0-59)", "hour (0-23)", "day-of-month (1-31)", "month (1-12)", "day-of-week (0-6)"]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for i, (field, label, (lo, hi)) in enumerate(zip(fields, labels, ranges)):
        try:
            _parse_cron_field(field, lo, hi)
        except (ValueError, IndexError):
            return False, f"invalid {label} field: '{field}'"
    return True, ""


def _cron_matches_now(cron_expr, now=None):
    """Check if a 5-field cron expression matches the current time.

    Fields: minute hour day-of-month month day-of-week
    """
    if now is None:
        now = time.localtime()
    fields = cron_expr.strip().split()
    if len(fields) == 6:
        # 6-field: drop seconds field (first position, goose uses this)
        fields = fields[1:]
    if len(fields) != 5:
        return False
    try:
        minutes = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        days = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        weekdays = _parse_cron_field(fields[4], 0, 6)
    except (ValueError, IndexError):
        return False

    # convert Python weekday (0=Mon) to cron weekday (0=Sun)
    cron_wday = (now.tm_wday + 1) % 7
    return (
        now.tm_min in minutes
        and now.tm_hour in hours
        and now.tm_mday in days
        and now.tm_mon in months
        and cron_wday in weekdays
    )


def _load_schedule():
    """Read schedule.json. Returns list of job dicts."""
    try:
        if os.path.exists(_SCHEDULE_FILE):
            with open(_SCHEDULE_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[cron] warn: could not load schedule.json: {e}")
    return []


def _save_schedule(jobs):
    """Write schedule.json atomically."""
    try:
        os.makedirs(os.path.dirname(_SCHEDULE_FILE), exist_ok=True)
        tmp = _SCHEDULE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, _SCHEDULE_FILE)
    except Exception as e:
        print(f"[cron] warn: could not save schedule.json: {e}")


def _load_recipe(source_path):
    """Read a recipe YAML file and extract the instructions field.

    Uses a minimal parser (no PyYAML dependency) — reads the 'instructions: |'
    block which is the only field we need.
    """
    try:
        if not os.path.exists(source_path):
            return None
        with open(source_path) as f:
            content = f.read()

        # extract 'instructions: |' block (YAML literal block scalar)
        # find the line starting with 'instructions:'
        lines = content.split("\n")
        capture = False
        indent = 0
        instruction_lines = []

        for line in lines:
            if line.strip().startswith("instructions:"):
                # check if it's a block scalar (ends with |)
                rest = line.split("instructions:", 1)[1].strip()
                if rest == "|":
                    capture = True
                    continue
                elif rest:
                    # inline value
                    return rest
            elif capture:
                if line.strip() == "" and not instruction_lines:
                    continue  # skip leading blank
                # detect indent of first content line
                if not instruction_lines and line.strip():
                    indent = len(line) - len(line.lstrip())
                # block ends when we hit a line with less/equal indent that's not blank
                if line.strip() and (len(line) - len(line.lstrip())) < indent and instruction_lines:
                    break
                # strip the indent prefix
                if len(line) >= indent:
                    instruction_lines.append(line[indent:])
                else:
                    instruction_lines.append(line.lstrip())

        if instruction_lines:
            return "\n".join(instruction_lines).strip()
        return None
    except Exception as e:
        print(f"[cron] warn: could not read recipe {source_path}: {e}")
        return None


def _fire_cron_job(job):
    """Execute a cron job: relay recipe instructions to goosed, deliver output.

    Runs in a fresh isolated session (like OpenClaw's sessionTarget: "isolated").
    """
    job_id = job.get("id", "unknown")
    source = job.get("source", "")
    print(f"[cron] firing job: {job_id}")

    instructions = _load_recipe(source)
    if not instructions:
        print(f"[cron] skip {job_id}: no instructions found in {source}")
        return

    # create an isolated session ID for this run
    session_id = f"cron_{job_id}_{time.strftime('%Y%m%d_%H%M%S')}"

    # prefix with job context so the agent knows it's a cron run
    prompt = (
        f"[cron: {job_id}]\n\n"
        f"You are running as a scheduled cron job. "
        f"Complete the task below and deliver the output using the `notify` command "
        f"(pipe your output into it: echo \"YOUR_OUTPUT\" | notify). "
        f"Be concise.\n\n"
        f"{instructions}"
    )

    # relay to goosed (no timeout -- task runs until goose completes)
    response_text, error, _media = _do_rest_relay(prompt, session_id)

    if error:
        print(f"[cron] job {job_id} failed: {error}")
        # notify about the failure
        notify_all(f"[cron:{job_id}] failed: {error}", channel=job.get("notify_channel"))
        return

    # if the response contains useful output, deliver it
    # (the recipe may have already called notify via shell, but we deliver
    # the response too in case it didn't)
    if response_text:
        response_text = _strip_goose_preamble(response_text)
        if response_text:
            formatted = f"[{job_id}]\n\n{response_text}"
            notify_all(formatted, channel=job.get("notify_channel"))

    print(f"[cron] job {job_id} completed")


def _cron_scheduler_loop():
    """Background loop: check schedule.json every 30s, fire due jobs."""
    global _cron_scheduler_running
    _cron_scheduler_running = True
    print(f"[cron] scheduler started ({_CRON_TICK_SECONDS}s tick)")

    while _cron_scheduler_running:
        try:
            # wait for goosed to be ready
            with _startup_state_lock:
                ready = goosed_startup_state["state"] == "ready"
            if not ready:
                time.sleep(10)
                continue

            jobs = _load_schedule()
            now = time.localtime()
            save_needed = False

            for job in jobs:
                if job.get("paused"):
                    continue
                if job.get("currently_running"):
                    continue

                cron_expr = job.get("cron", "")
                if not cron_expr:
                    continue

                # check if this job matches the current minute
                if not _cron_matches_now(cron_expr, now):
                    continue

                # check last_run to avoid double-firing within the same minute
                last_run = job.get("last_run", "")
                if last_run:
                    try:
                        if "T" in last_run:
                            lr_time = last_run.split("T")[1][:5]  # HH:MM
                            now_time = time.strftime("%H:%M", now)
                            if lr_time == now_time:
                                continue
                    except Exception:
                        pass

                # fire it in a thread so we don't block other jobs
                job["currently_running"] = True
                save_needed = True

                def _run_job(j, all_jobs):
                    try:
                        _fire_cron_job(j)
                    finally:
                        j["currently_running"] = False
                        j["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
                        j["current_session_id"] = None
                        _save_schedule(all_jobs)

                threading.Thread(target=_run_job, args=(job, jobs), daemon=True).start()

            if save_needed:
                _save_schedule(jobs)

        except Exception as e:
            print(f"[cron] error: {e}")

        # sleep 30s, checking shutdown every 5s
        for _ in range(6):
            if not _cron_scheduler_running:
                break
            time.sleep(5)

    print("[cron] scheduler stopped")


def start_cron_scheduler():
    """Start the cron scheduler daemon thread."""
    global _cron_scheduler_running
    if _cron_scheduler_running:
        return
    threading.Thread(target=_cron_scheduler_loop, daemon=True).start()


# ── channel contract v2 types ─────────────────────────────────────────────────


class InboundMessage:
    """Channel-agnostic inbound message envelope."""
    def __init__(self, user_id, text="", channel="", media=None, metadata=None):
        self.user_id = str(user_id)
        self.text = text or ""
        self.channel = channel or ""
        self.media = media if media is not None else []
        self.metadata = metadata if metadata is not None else {}

    @property
    def has_media(self):
        return bool(self.media)

    @property
    def has_text(self):
        return bool(self.text.strip())


class MediaContent:
    """Normalized media attachment with actual data."""
    def __init__(self, kind, mime_type, data, filename=None):
        self.kind = kind
        self.mime_type = mime_type
        self.data = data
        self.filename = filename

    @property
    def size(self):
        return len(self.data) if self.data else 0

    def to_base64(self):
        return base64.b64encode(self.data).decode("ascii") if self.data else ""

    def to_content_block(self):
        if self.kind == "image":
            return {"type": "image", "data": self.to_base64(), "mimeType": self.mime_type}
        if self.kind == "document":
            return self._document_content_block()
        return None

    def _document_content_block(self):
        """Build a content block for document attachments."""
        fname = self.filename or "file"
        mime = self.mime_type or "application/octet-stream"

        # text-based files: extract content directly
        _TEXT_MIMES = {
            "text/plain", "text/csv", "text/markdown", "text/html",
            "text/x-python", "text/x-script.python",
            "application/json", "application/xml", "text/xml",
            "application/x-yaml", "text/yaml",
            "application/javascript", "text/javascript",
        }
        _TEXT_EXTENSIONS = {
            ".txt", ".py", ".md", ".csv", ".json", ".yaml", ".yml",
            ".xml", ".html", ".js", ".ts", ".sh", ".toml", ".ini",
            ".cfg", ".conf", ".log", ".rst", ".rb", ".go", ".rs",
            ".java", ".c", ".cpp", ".h", ".hpp", ".css", ".sql",
        }
        ext = os.path.splitext(fname)[1].lower() if fname else ""
        is_text = mime in _TEXT_MIMES or ext in _TEXT_EXTENSIONS or mime.startswith("text/")

        if is_text and self.data:
            try:
                text_content = self.data.decode("utf-8", errors="replace")
                return {"type": "text", "text": f"[File: {fname}]\n```\n{text_content}\n```"}
            except Exception:
                pass

        # PDFs: send as base64 document block
        if mime == "application/pdf" and self.data:
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": self.to_base64(),
                },
            }

        # archives: acknowledge but note file type
        _ARCHIVE_MIMES = {
            "application/zip", "application/x-tar", "application/gzip",
            "application/x-gzip", "application/x-bzip2",
            "application/x-7z-compressed", "application/x-rar-compressed",
        }
        _ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".tar.gz", ".tgz"}
        if mime in _ARCHIVE_MIMES or ext in _ARCHIVE_EXTENSIONS:
            size_kb = round(self.size / 1024, 1)
            return {"type": "text", "text": f"[File received: {fname} ({mime}, {size_kb} KB) - archive file, contents not extracted]"}

        # fallback: describe the file
        size_kb = round(self.size / 1024, 1)
        return {"type": "text", "text": f"[File received: {fname} ({mime}, {size_kb} KB)]"}


class ChannelCapabilities:
    """Declares what a channel supports."""
    def __init__(self, **kwargs):
        self.supports_images = kwargs.get("supports_images", False)
        self.supports_voice = kwargs.get("supports_voice", False)
        self.supports_files = kwargs.get("supports_files", False)
        self.supports_buttons = kwargs.get("supports_buttons", False)
        self.supports_streaming = kwargs.get("supports_streaming", False)
        self.typing = kwargs.get("typing", True)
        self.max_file_size = kwargs.get("max_file_size", 0)
        self.max_text_length = kwargs.get("max_text_length", 0)

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


class OutboundAdapter:
    """Base class for channel output. Override send_text (required).
    Other send_* methods degrade to send_text by default."""

    def capabilities(self):
        return ChannelCapabilities()

    def send_text(self, text):
        raise NotImplementedError("send_text() is required")

    def send_image(self, data, caption="", **kwargs):
        fallback = f"{caption}\n[image]" if caption else "[image]"
        return self.send_text(fallback.strip())

    def send_voice(self, data, caption="", **kwargs):
        fallback = caption or "[voice message]"
        return self.send_text(fallback)

    def send_file(self, data, filename="", **kwargs):
        fallback = f"[File: {filename}]" if filename else "[file]"
        return self.send_text(fallback)

    def send_typing(self, chat_id, **kwargs):
        """Send a typing indicator for the given chat/user. No-op by default.
        Override in subclasses to provide channel-specific typing feedback."""
        return None

    def send_buttons(self, text, buttons):
        lines = [text, ""]
        for i, btn in enumerate(buttons, 1):
            label = btn.get("label", btn.get("text", f"Option {i}"))
            lines.append(f"{i}. {label}")
        return self.send_text("\n".join(lines))


class LegacyOutboundAdapter(OutboundAdapter):
    """Wraps a legacy send(text) function as an OutboundAdapter."""
    def __init__(self, send_fn):
        self._send_fn = send_fn

    def send_text(self, text):
        return self._send_fn(text)


class TelegramOutboundAdapter(OutboundAdapter):
    """Telegram-specific outbound adapter with real media sending via Bot API."""

    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def capabilities(self):
        return ChannelCapabilities(
            supports_images=True, supports_voice=True, supports_files=True,
            max_file_size=50_000_000, max_text_length=4096,
        )

    def send_text(self, text):
        ok, err = send_telegram_message(self.bot_token, self.chat_id, text)
        return {"sent": ok, "error": err or ""}

    def send_image(self, image_bytes, caption="", mime_type="image/png"):
        return self._send_media("sendPhoto", "photo", image_bytes,
                                f"image{_ext_from_mime(mime_type)}", mime_type, caption)

    def send_voice(self, audio_bytes, caption="", mime_type="audio/ogg"):
        return self._send_media("sendVoice", "voice", audio_bytes,
                                f"voice{_ext_from_mime(mime_type)}", mime_type, caption)

    def send_file(self, file_bytes, filename="file", mime_type="application/octet-stream"):
        return self._send_media("sendDocument", "document", file_bytes,
                                filename, mime_type, "")

    def send_typing(self, chat_id, **kwargs):
        """Send typing indicator via Telegram Bot API."""
        _send_typing_action(self.bot_token, chat_id)

    def _send_media(self, method, field, data, filename, mime_type, caption):
        """Internal: upload media to Telegram via multipart/form-data."""
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        fields = {"chat_id": str(self.chat_id)}
        if caption:
            fields["caption"] = caption[:1024]
        files = [(field, filename, mime_type, data)]
        body, content_type = _build_multipart(fields, files)
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return {"sent": result.get("ok", False), "error": ""}
        except Exception as e:
            return {"sent": False, "error": str(e)}


def _route_media_blocks(media_blocks, adapter):
    """Dispatch goose response media blocks to the appropriate adapter method.

    Handles image blocks by decoding base64 and calling send_image (or send_file
    for images > 10MB). Unknown block types are logged and skipped.
    """
    for block in media_blocks:
        btype = block.get("type", "")
        if btype == "image":
            data_b64 = block.get("data", "")
            if not data_b64:
                continue
            raw_bytes = base64.b64decode(data_b64)
            mime = block.get("mimeType", "image/png")
            if len(raw_bytes) > 10_000_000:
                ext = _ext_from_mime(mime)
                adapter.send_file(raw_bytes, filename=f"image{ext}", mime_type=mime)
            else:
                adapter.send_image(raw_bytes, mime_type=mime)
        else:
            print(f"[media] unknown outbound media type: {btype}")


# ── channel plugin system ─────────────────────────────────────────────────────
#
# Each channel is a .py file in /data/channels/ with a CHANNEL dict:
#   CHANNEL = {
#       "name": "slack",              # REQUIRED
#       "version": 1,                 # REQUIRED
#       "send": send_fn,              # REQUIRED: (text) -> {"sent": bool, "error": str}
#       "poll": poll_fn,              # OPTIONAL: (relay_fn, stop_event, creds) -> None (blocking)
#       "setup": setup_fn,            # OPTIONAL: (creds) -> {"ok": bool, "error": str}
#       "teardown": teardown_fn,      # OPTIONAL: () -> None
#       "credentials": ["TOKEN"],     # OPTIONAL: keys resolved from env then sidecar JSON
#   }
#
# Credentials sidecar: /data/channels/<name>.json -> {"TOKEN": "value"}
# Files prefixed with _ are skipped (use for templates like _example.py).
# Hot-reload via POST /api/channels/reload.

def get_paired_user_ids(platform):
    """Read paired user IDs from config.yaml filtered by platform."""
    user_ids = []
    if not os.path.exists(GOOSE_CONFIG_PATH):
        return user_ids
    try:
        with open(GOOSE_CONFIG_PATH) as f:
            content = f.read()
        in_pairings = False
        current_entry = {}
        for line in content.split("\n"):
            stripped = line.strip()
            if line.startswith("gateway_pairings:"):
                in_pairings = True
                continue
            if in_pairings:
                if line and not line[0].isspace() and not stripped.startswith("-"):
                    break
                if stripped.startswith("- platform:"):
                    if current_entry.get("platform") == platform and current_entry.get("user_id"):
                        user_ids.append(current_entry["user_id"])
                    current_entry = {"platform": stripped.split(":", 1)[1].strip()}
                elif stripped.startswith("user_id:"):
                    val = stripped.split(":", 1)[1].strip().strip("'\"")
                    current_entry["user_id"] = val
        if current_entry.get("platform") == platform and current_entry.get("user_id"):
            user_ids.append(current_entry["user_id"])
    except Exception as e:
        print(f"[channels] warn: could not read pairings for {platform}: {e}")
    # merge in-memory cache (survives config.yaml race rewrites)
    with _pairing_cache_lock:
        for (plat, uid), _ in _pairing_cache.items():
            if plat == platform and uid not in user_ids:
                user_ids.append(uid)
    return user_ids


def _resolve_channel_creds(name, cred_keys):
    """Resolve credential keys: check os.environ first, then /data/channels/<name>.json."""
    creds = {}
    sidecar = {}
    sidecar_path = os.path.join(CHANNELS_DIR, f"{name}.json")
    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path) as f:
                sidecar = json.load(f)
        except Exception as e:
            print(f"[channels] warn: could not read {sidecar_path}: {e}")
    for key in cred_keys:
        val = os.environ.get(key, "") or sidecar.get(key, "")
        creds[key] = val
    return creds


class ChannelRelay:
    """Relay function wrapper for channel plugins. Manages per-channel sessions,
    command interception, and active relay tracking for /stop cancellation."""

    def __init__(self, channel_name, typing_cb=None, adapter=None):
        self._name = channel_name
        self._state = ChannelState()
        self._typing_cb = typing_cb
        self._adapter = adapter
        # Load any persisted sessions for this channel
        _session_manager.load(channel_name)

    def __call__(self, user_id_or_msg, text=None, send_fn=None):
        """Relay a message from channel user to goosed. Returns response text.

        Accepts either:
          - relay(user_id, text, send_fn)     # legacy signature
          - relay(InboundMessage, send_fn)    # v2 signature

        If text is a slash command, intercepts and dispatches via _command_router.
        If send_fn is provided, streams response chunks via send_fn(text) based
        on the channel's verbosity setting. Backward compatible: plugins that
        don't pass send_fn get the original single-response behavior.
        """
        if isinstance(user_id_or_msg, InboundMessage):
            msg = user_id_or_msg
            send_fn = text  # second arg is send_fn in v2 signature
            text = msg.text
            user_key = msg.user_id
        else:
            user_key = str(user_id_or_msg)

        # Command interception (CHAN-01)
        if text and text.strip().startswith("/"):
            ctx = {
                "channel": self._name,
                "user_id": user_key,
                "send_fn": send_fn or (lambda t: None),
                "channel_state": self._state,
            }
            if _command_router.is_command(text):
                _command_router.dispatch(text, ctx)
                return ""
            # unknown command
            if send_fn:
                send_fn(f"Unknown command: {text.split()[0]}\nSend /help for available commands.")
            return ""

        # Per-user lock (CHAN-02)
        user_lock = self._state.get_user_lock(user_key)
        lock_timeout = 2 if send_fn else 120  # can't notify user without send_fn
        if not user_lock.acquire(timeout=lock_timeout):
            if send_fn:
                _replay = lambda _uid=user_id_or_msg, _t=text, _sf=send_fn: self(_uid, _t, _sf)
                self._state.queue_message(user_key, text, replay_fn=_replay)
                send_fn("got it, i'll get to this next")
            return ""

        try:
            # Typing indicator loop (CHAN-06)
            typing_stop = threading.Event()
            # Resolve typing callback: explicit cb > adapter.send_typing > none
            _typing_fn = self._typing_cb
            if not _typing_fn and self._adapter:
                _caps = self._adapter.capabilities()
                if getattr(_caps, "typing", False):
                    _typing_fn = lambda uid: self._adapter.send_typing(uid)
            if _typing_fn:
                def _typing_loop():
                    while not typing_stop.is_set():
                        try:
                            _typing_fn(user_key)
                        except Exception:
                            pass  # buggy callback must not crash relay
                        typing_stop.wait(4)
                threading.Thread(target=_typing_loop, daemon=True).start()

            # Session management
            session_id = _session_manager.get(self._name, user_key)
            if not session_id:
                session_id = f"{self._name}_{user_key}_{time.strftime('%Y%m%d_%H%M%S')}"
                _session_manager.set(self._name, user_key, session_id)

            # Active relay tracking (CHAN-03)
            cancelled = threading.Event()
            sock_ref = [None, cancelled]
            self._state.set_active_relay(user_key, sock_ref)

            try:
                # determine streaming params
                setup = load_setup()
                verbosity = get_verbosity_for_channel(setup, self._name) if setup else "balanced"
                use_streaming = send_fn and verbosity != "quiet"

                # build content blocks from InboundMessage media
                _cb = None
                if isinstance(user_id_or_msg, InboundMessage) and user_id_or_msg.has_media:
                    _cb = _build_content_blocks(text, user_id_or_msg)

                if use_streaming:
                    response_text, error, _media = _relay_to_goosed(
                        text, session_id, chat_id=user_key, channel=self._name,
                        flush_cb=send_fn, verbosity=verbosity,
                        sock_ref=sock_ref, flush_interval=2.0,
                        content_blocks=_cb,
                    )
                else:
                    response_text, error, _media = _relay_to_goosed(
                        text, session_id, chat_id=user_key, channel=self._name,
                        sock_ref=sock_ref, content_blocks=_cb,
                    )

                if cancelled.is_set():
                    return ""

                if error:
                    # retry with new session
                    session_id = f"{self._name}_{user_key}_{time.strftime('%Y%m%d_%H%M%S')}"
                    _session_manager.set(self._name, user_key, session_id)
                    if use_streaming:
                        response_text, error, _media = _relay_to_goosed(
                            text, session_id, chat_id=user_key, channel=self._name,
                            flush_cb=send_fn, verbosity=verbosity,
                            sock_ref=sock_ref, flush_interval=2.0,
                            content_blocks=_cb,
                        )
                    else:
                        response_text, error, _media = _relay_to_goosed(
                            text, session_id, chat_id=user_key, channel=self._name,
                            sock_ref=sock_ref, content_blocks=_cb,
                        )

                if cancelled.is_set():
                    return ""
                if error:
                    return f"Error: {error}"

                # Route media blocks through channel adapter
                if _media and not error:
                    _ch_adapter = _loaded_channels.get(self._name, {}).get("adapter")
                    if _ch_adapter:
                        try:
                            _route_media_blocks(_media, _ch_adapter)
                        except Exception as _me:
                            print(f"[channel:{self._name}] media routing error: {_me}")

                return response_text
            finally:
                self._state.pop_active_relay(user_key)
        finally:
            typing_stop.set()  # stop typing loop
            user_lock.release()
            # process queued messages
            _queued = self._state.pop_queued_replay(user_key)
            if _queued:
                _, _replay_fn = _queued
                if _replay_fn:
                    threading.Thread(target=_replay_fn, daemon=True).start()

    def reset_session(self, user_id):
        """Reset a user's session (for /clear command)."""
        _session_manager.pop(self._name, str(user_id))


def _deregister_notification_handler(name):
    """Remove a handler from the notification bus by name."""
    with _notification_handlers_lock:
        _notification_handlers[:] = [h for h in _notification_handlers if h["name"] != name]


def _load_channel(filepath):
    """Load a single channel plugin from a .py file."""
    basename = os.path.basename(filepath)
    mod_name = basename[:-3]  # strip .py

    print(f"[channels] loading {basename}...")

    try:
        spec = importlib.util.spec_from_file_location(f"channel_{mod_name}", filepath)
        if not spec or not spec.loader:
            print(f"[channels] skip {basename}: could not create module spec")
            return False

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        channel = getattr(mod, "CHANNEL", None)
        if not isinstance(channel, dict):
            print(f"[channels] skip {basename}: no CHANNEL dict found")
            return False

        name = channel.get("name")
        if not name or not isinstance(name, str):
            print(f"[channels] skip {basename}: CHANNEL.name is required")
            return False

        send_fn = channel.get("send")
        if not callable(send_fn):
            print(f"[channels] skip {basename}: CHANNEL.send must be callable")
            return False

        # resolve credentials
        cred_keys = channel.get("credentials", [])
        creds = _resolve_channel_creds(name, cred_keys) if cred_keys else {}

        # check required creds are present
        missing = [k for k in cred_keys if not creds.get(k)]
        if missing:
            print(f"[channels] skip {name}: missing credentials: {', '.join(missing)}")
            return False

        # call setup() if provided
        setup_fn = channel.get("setup")
        if callable(setup_fn):
            try:
                result = setup_fn(creds)
                if isinstance(result, dict) and not result.get("ok", True):
                    print(f"[channels] skip {name}: setup failed: {result.get('error', '?')}")
                    return False
            except Exception as e:
                print(f"[channels] skip {name}: setup() raised: {e}")
                return False

        # wrap send_fn in adapter (v2 contract)
        adapter = channel.get("adapter")
        if isinstance(adapter, OutboundAdapter):
            pass  # v2 plugin, use adapter directly
        else:
            adapter = LegacyOutboundAdapter(send_fn)

        # register notification handler (wraps adapter.send_text)
        def _make_handler(fn):
            def handler(text):
                try:
                    return fn(text)
                except Exception as e:
                    return {"sent": False, "error": str(e)}
            return handler

        register_notification_handler(f"channel:{name}", _make_handler(adapter.send_text))

        # start poll thread if provided
        poll_fn = channel.get("poll")
        stop_event = threading.Event()
        poll_thread = None

        if callable(poll_fn):
            typing_cb = channel.get("typing")
            if typing_cb and not callable(typing_cb):
                print(f"[channels] warn: {name} typing is not callable, ignoring")
                typing_cb = None
            relay_fn = ChannelRelay(name, typing_cb=typing_cb, adapter=adapter)

            def _poll_wrapper(_fn, _relay, _stop, _creds):
                try:
                    _fn(_relay, _stop, _creds)
                except Exception as e:
                    print(f"[channels] {name} poll() crashed: {e}")

            poll_thread = threading.Thread(
                target=_poll_wrapper,
                args=(poll_fn, relay_fn, stop_event, creds),
                daemon=True,
            )
            poll_thread.start()

        # Register custom commands from CHANNEL dict (CHAN-04)
        custom_commands = channel.get("commands", {})
        if isinstance(custom_commands, dict):
            for cmd_name, cmd_info in custom_commands.items():
                if not isinstance(cmd_info, dict) or not callable(cmd_info.get("handler")):
                    print(f"[channels] warn: {name} command /{cmd_name} has invalid handler, skipping")
                    continue
                # Check for conflicts with built-in commands
                if _command_router.is_command(f"/{cmd_name}"):
                    print(f"[channels] warn: {name} command /{cmd_name} conflicts with built-in, skipping")
                    continue
                _command_router.register(cmd_name, cmd_info["handler"], cmd_info.get("description", ""))
                print(f"[channels] registered custom command /{cmd_name} from {name}")

        with _channels_lock:
            _loaded_channels[name] = {"module": mod, "channel": channel, "creds": creds, "adapter": adapter}
            _channel_stop_events[name] = stop_event
            if poll_thread:
                _channel_threads[name] = poll_thread

        has_poll = "poll" if callable(poll_fn) else "send-only"
        print(f"[channels] loaded: {name} v{channel.get('version', '?')} ({has_poll})")
        return True

    except Exception as e:
        print(f"[channels] error loading {basename}: {e}")
        return False


def _unload_channel(name):
    """Unload a channel plugin: teardown, stop poll, deregister."""
    with _channels_lock:
        entry = _loaded_channels.pop(name, None)
        stop_event = _channel_stop_events.pop(name, None)
        thread = _channel_threads.pop(name, None)

    if not entry:
        return

    # call teardown() if provided
    teardown_fn = entry["channel"].get("teardown")
    if callable(teardown_fn):
        try:
            teardown_fn()
        except Exception as e:
            print(f"[channels] {name} teardown() error: {e}")

    # stop poll thread
    if stop_event:
        stop_event.set()
    if thread:
        thread.join(timeout=5)

    # deregister from notification bus
    _deregister_notification_handler(f"channel:{name}")

    print(f"[channels] unloaded: {name}")


def _load_all_channels():
    """Discover and load all channel plugins from /data/channels/."""
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    plugins = sorted(glob.glob(os.path.join(CHANNELS_DIR, "*.py")))
    loaded = 0
    for filepath in plugins:
        basename = os.path.basename(filepath)
        if basename.startswith("_"):
            continue
        try:
            if _load_channel(filepath):
                loaded += 1
        except Exception as e:
            print(f"[channels] error loading {basename}: {e}")
    if loaded:
        print(f"[channels] {loaded} channel(s) loaded")
    else:
        print("[channels] no channel plugins found")


def _reload_channels():
    """Unload all channels and reload from disk. Returns list of loaded names."""
    with _channels_lock:
        names = list(_loaded_channels.keys())
    for name in names:
        _unload_channel(name)
    _load_all_channels()
    with _channels_lock:
        return list(_loaded_channels.keys())


# ── session watcher: persistence + API helpers + loop ────────────────────────

def _load_watcher_state():
    """Load session watcher state from disk."""
    global _session_watcher_state
    try:
        if os.path.exists(_session_watcher_state_file):
            with open(_session_watcher_state_file) as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _session_watcher_lock:
                    _session_watcher_state = data
                print(f"[watcher] loaded {len(data)} tracked session(s)")
    except Exception as e:
        print(f"[watcher] warn: could not load state: {e}")


def _save_watcher_state():
    """Persist session watcher state to disk."""
    with _session_watcher_lock:
        data = dict(_session_watcher_state)
    try:
        os.makedirs(os.path.dirname(_session_watcher_state_file), exist_ok=True)
        tmp = _session_watcher_state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _session_watcher_state_file)
    except Exception as e:
        print(f"[watcher] warn: could not save state: {e}")


def _fetch_scheduled_sessions():
    """Fetch sessions from goosed and return only scheduled ones."""
    if not _INTERNAL_GOOSE_TOKEN:
        return []
    try:
        conn = _goosed_conn(timeout=10)
        conn.request("GET", "/sessions", headers={
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status != 200:
            return []
        data = json.loads(body)
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        return [s for s in sessions if s.get("schedule_id")]
    except Exception as e:
        print(f"[watcher] error fetching sessions: {e}")
        return []


def _fetch_session_messages(session_id):
    """Fetch full conversation from a goosed session. Returns list of message dicts."""
    if not _INTERNAL_GOOSE_TOKEN:
        return []
    try:
        conn = _goosed_conn(timeout=15)
        conn.request("GET", f"/sessions/{urllib.parse.quote(str(session_id))}", headers={
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status != 200:
            return []
        session = json.loads(body)
        conversation = session.get("conversation") or []
        if isinstance(conversation, dict):
            conversation = conversation.get("messages", [])
        messages = []
        for msg in conversation:
            role = msg.get("role", "")
            content_items = msg.get("content", [])
            text_parts = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_parts.append(item)
            if text_parts:
                messages.append({"role": role, "text": "\n".join(text_parts)})
        return messages
    except Exception as e:
        print(f"[watcher] error fetching session {session_id}: {e}")
        return []


def _session_watcher_loop():
    """Poll goosed for scheduled session output and auto-forward to telegram."""
    global _session_watcher_running
    _session_watcher_running = True
    print("[watcher] session watcher started")

    while _session_watcher_running:
        try:
            # wait for goosed to be ready
            with _startup_state_lock:
                ready = goosed_startup_state["state"] == "ready"
            if not ready:
                time.sleep(10)
                continue

            scheduled = _fetch_scheduled_sessions()
            state_changed = False

            for session in scheduled:
                sid = session.get("id", "")
                schedule_id = session.get("schedule_id", "unknown")
                msg_count = session.get("message_count", 0) or 0
                if not sid:
                    continue

                with _session_watcher_lock:
                    tracked = _session_watcher_state.get(sid, {})
                    forwarded = tracked.get("forwarded_count", 0)

                if msg_count <= forwarded:
                    continue  # no new messages

                # fetch full conversation
                messages = _fetch_session_messages(sid)
                if not messages:
                    continue

                # extract new assistant messages beyond what we already forwarded
                for msg in messages[forwarded:]:
                    if msg["role"] == "assistant" and msg["text"].strip():
                        text = msg["text"].strip()
                        formatted = f"[{schedule_id}]\n\n{text}"
                        if len(formatted) > 4000:
                            formatted = formatted[:3997] + "..."
                        result = notify_all(formatted)
                        if result.get("sent"):
                            print(f"[watcher] forwarded output from {schedule_id}")
                        else:
                            print(f"[watcher] delivery failed for {schedule_id}: {result.get('error', '?')}")

                # update tracking
                with _session_watcher_lock:
                    _session_watcher_state[sid] = {
                        "forwarded_count": len(messages),
                        "schedule_id": schedule_id,
                        "last_seen": time.time(),
                    }
                state_changed = True

            # prune stale entries (> 7 days, no longer in session list)
            active_ids = {s.get("id") for s in scheduled}
            cutoff = time.time() - 7 * 86400
            with _session_watcher_lock:
                stale = [
                    sid for sid, info in _session_watcher_state.items()
                    if sid not in active_ids and info.get("last_seen", 0) < cutoff
                ]
                for sid in stale:
                    del _session_watcher_state[sid]
                    state_changed = True

            if state_changed:
                _save_watcher_state()

        except Exception as e:
            print(f"[watcher] error: {e}")

        # sleep 30s, checking shutdown flag every 5s
        for _ in range(6):
            if not _session_watcher_running:
                break
            time.sleep(5)

    print("[watcher] session watcher stopped")


def start_session_watcher():
    """Start the session watcher daemon thread."""
    global _session_watcher_running
    if _session_watcher_running:
        return
    _load_watcher_state()
    threading.Thread(target=_session_watcher_loop, daemon=True).start()


# ── telegram session management ─────────────────────────────────────────────

def _get_chat_lock(chat_id):
    """Get or create a per-chat lock. Ensures only one relay runs at a time per chat."""
    return _telegram_state.get_user_lock(chat_id)


def is_known_command(text):
    """Check if text is a recognized slash command."""
    return _command_router.is_command(text)


def _clear_chat(chat_id):
    """Kill active relay and clear session for a chat. Used by /clear.
    INFRA-04: Only removes the requesting user's session, not all sessions."""
    chat_key = str(chat_id)

    # kill active relay (same as /stop)
    _telegram_state.kill_relay(chat_key)

    # remove ONLY this user's session (not all sessions)
    # NOTE: _restart_goose_and_prewarm still restarts goosed, which invalidates
    # all goosed sessions. Other users' _session_manager entries remain but will
    # get new sessions on next message (stale session triggers retry logic in
    # _relay_to_goosed). This is a documented limitation until goosed supports
    # per-session cleanup.
    old = _session_manager.pop("telegram", chat_key)
    return old


def _restart_goose_and_prewarm(chat_id):
    """Restart goosed process to kill provider subprocesses, then prewarm.

    Called after /clear in a background thread. Restarts the entire goosed
    process so the claude-code provider's persistent subprocess dies and
    conversation history is truly cleared.
    """
    chat_key = str(chat_id)
    print(f"[clear] restarting goose web to clear provider state...")
    stop_goosed()
    ok = start_goosed()
    if not ok:
        print(f"[clear] goose web restart failed! next message will trigger health monitor restart")
        return
    print(f"[clear] goose web restarted, prewarming session for chat {chat_key}")
    _prewarm_session(chat_id)


def _get_session_id(chat_id, channel="telegram"):
    """Get or create a session_id for a chat_id on a given channel.

    For new chats, calls POST /agent/start on goosed to get a real session_id.
    For existing chats, returns the stored session_id.
    If a prewarm is in progress, waits for it instead of creating a duplicate.

    channel: the channel key for session lookup (e.g. "telegram", "telegram:research").
    """
    chat_key = str(chat_id)
    sid = _session_manager.get(channel, chat_key)
    if sid:
        return sid

    # check if prewarm is in progress -- wait for it instead of creating duplicate
    evt = _telegram_state._prewarm_events.get(chat_key)
    if evt:
        evt.wait(timeout=15)
        sid = _session_manager.get(channel, chat_key)
        if sid:
            return sid

    # create a new agent session via goosed
    sid = _create_goose_session()
    if not sid:
        # fallback to random UUID if goosed is unavailable
        sid = str(uuid.uuid4())
        print(f"[{channel}] warn: could not start agent, using random session {sid}")

    _session_manager.set(channel, chat_key, sid)
    print(f"[{channel}] new session {sid} for chat {chat_key}")
    return sid


def _prewarm_session(chat_id):
    """Create a new goose session in background thread and store it for chat_id.

    Called after /clear so the next user message doesn't pay the cold-start cost.
    Uses _telegram_state._prewarm_events so _get_session_id can wait instead of creating a duplicate.
    """
    chat_key = str(chat_id)
    evt = threading.Event()
    _telegram_state._prewarm_events[chat_key] = evt

    def _do_prewarm():
        try:
            sid = _create_goose_session()
            if not sid:
                print(f"[telegram] prewarm failed for chat {chat_key}")
                return
            existing = _session_manager.get("telegram", chat_key)
            if not existing:
                _session_manager.set("telegram", chat_key, sid)
                print(f"[telegram] prewarmed session {sid} for chat {chat_key}")
            else:
                print(f"[telegram] prewarm skipped, session exists for {chat_key}")
        finally:
            evt.set()
            _telegram_state._prewarm_events.pop(chat_key, None)

    t = threading.Thread(target=_do_prewarm, daemon=True)
    t.start()


# ── command handlers (registered on _command_router) ─────────────────────────

def _handle_cmd_help(ctx):
    """Handle /help command."""
    help_text = (
        "\U0001fabf *GooseClaw Commands*\n\n"
        + _command_router.get_help_text()
    )
    ctx["send_fn"](help_text)


def _handle_cmd_stop(ctx):
    """Handle /stop command."""
    chat_id = ctx["user_id"]
    state = ctx.get("channel_state", _telegram_state)
    channel = ctx.get("channel", "telegram")
    sock_ref = state.pop_active_relay(chat_id)
    if sock_ref and sock_ref[0]:
        try:
            if len(sock_ref) > 1 and hasattr(sock_ref[1], 'set'):
                sock_ref[1].set()
            sid = _session_manager.get(channel, chat_id)
            try:
                sock_ref[0].close()
            except Exception:
                pass
        except Exception:
            pass
        ctx["send_fn"]("Stopped.")
        print(f"[{channel}] /stop killed relay for chat {chat_id}")
    else:
        ctx["send_fn"]("Nothing running.")


def _handle_cmd_clear(ctx):
    """Handle /clear command."""
    chat_id = ctx["user_id"]
    state = ctx.get("channel_state", _telegram_state)
    channel = ctx.get("channel", "telegram")
    chat_key = str(chat_id)

    # kill active relay (same as /stop)
    state.kill_relay(chat_key)

    # remove only this user's session for the requesting channel
    old = _session_manager.pop(channel, chat_key)

    ctx["send_fn"]("\U0001f504 Session cleared. Restarting engine, give it ~15s...")
    threading.Thread(
        target=_restart_goose_and_prewarm,
        args=(chat_id,),
        daemon=True,
    ).start()
    print(f"[{channel}] session cleared for chat {chat_id} (old: {old}), restarting goose web")


def _handle_cmd_restart(ctx):
    """Handle /restart command -- restart engine without clearing session."""
    chat_id = ctx["user_id"]
    state = ctx.get("channel_state", _telegram_state)
    channel = ctx.get("channel", "telegram")
    chat_key = str(chat_id)

    # kill active relay (same as /stop)
    state.kill_relay(chat_key)

    # NOTE: intentionally NOT popping the session -- that's /clear's job
    ctx["send_fn"]("\U0001f504 Restarting engine, give me ~10 seconds...")
    threading.Thread(
        target=_restart_goose_and_prewarm,
        args=(chat_id,),
        daemon=True,
    ).start()
    print(f"[{channel}] engine restart requested by chat {chat_id} (session preserved)")


def _handle_cmd_compact(ctx):
    """Handle /compact command."""
    chat_id = ctx["user_id"]
    channel = ctx.get("channel", "telegram")
    bot_token = ctx.get("bot_token")
    if bot_token:
        _send_typing_action(bot_token, chat_id)
    session_id = _session_manager.get(channel, chat_id)
    if not session_id:
        ctx["send_fn"]("No active session. Send a message first.")
        return
    response_text, error, *_ = _relay_to_goosed(
        "Please summarize our conversation so far into key points, "
        "then we can continue from this summary. Be concise.",
        session_id, chat_id=chat_id, channel=channel
    )
    if error:
        ctx["send_fn"](f"Error: {error}")
    else:
        ctx["send_fn"](f"\U0001f4dd Compacted:\n\n{response_text}")


# known context windows per model (tokens)
_MODEL_CONTEXT = {
    "claude-opus-4-6": 200000, "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-5": 200000, "claude-haiku-4-5": 200000,
    "claude-3-5-sonnet": 200000, "claude-3-5-haiku": 200000,
    "gpt-4o": 128000, "gpt-4o-mini": 128000, "gpt-4-turbo": 128000,
    "o1": 200000, "o1-mini": 128000, "o3-mini": 200000,
    "gemini-2.0-flash": 1000000, "gemini-1.5-pro": 2000000,
    "llama-3.3-70b-versatile": 128000, "deepseek-chat": 128000,
    "mistral-large-latest": 128000,
}


def _estimate_tokens(text):
    """Rough token estimate: ~4 chars per token for English."""
    return max(1, len(text) // 4)


def _format_duration(seconds):
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def _make_progress_bar(pct, width=15):
    """Create a text progress bar."""
    filled = int(width * pct / 100)
    return "\u2593" * filled + "\u2591" * (width - filled)


def _handle_cmd_status(ctx):
    """Handle /status command -- show session, provider, and context info."""
    channel = ctx.get("channel", "telegram")
    chat_id = ctx.get("user_id", "")
    send = ctx["send_fn"]

    # provider & model
    setup = load_setup()
    provider_type = setup.get("provider_type", "unknown") if setup else "unknown"
    model = setup.get("model", "unknown") if setup else "unknown"
    display_provider = provider_names.get(provider_type, provider_type)
    goose_mode = os.environ.get("GOOSE_MODE", "auto")

    # session info
    session_id = _session_manager.get(channel, chat_id)
    msg_count = 0
    total_chars = 0
    uptime_str = "n/a"

    if session_id:
        # fetch session messages for count and token estimate
        messages = _fetch_session_messages(session_id)
        msg_count = len(messages)
        for msg in messages:
            total_chars += len(msg.get("text", ""))

        # session uptime from ID (format: YYYYMMDD_N or timestamp-based)
        try:
            date_part = session_id.split("_")[0]
            if len(date_part) == 8 and date_part.isdigit():
                from datetime import datetime
                created = datetime.strptime(date_part, "%Y%m%d")
                delta = (datetime.now() - created).total_seconds()
                uptime_str = _format_duration(delta)
        except Exception:
            pass

    # extensions count from goosed /config
    ext_count = 0
    try:
        conn = _goosed_conn(timeout=5)
        conn.request("GET", "/config", headers={"X-Secret-Key": _INTERNAL_GOOSE_TOKEN})
        resp = conn.getresponse()
        if resp.status == 200:
            cfg = json.loads(resp.read().decode("utf-8", errors="replace"))
            extensions = cfg.get("config", {}).get("extensions", {})
            ext_count = sum(1 for e in extensions.values()
                           if isinstance(e, dict) and e.get("enabled", True))
        conn.close()
    except Exception:
        pass

    # context window
    tokens_used = _estimate_tokens("x" * total_chars) if total_chars else 0
    # find context limit for current model
    context_limit = 200000  # default
    for key, limit in _MODEL_CONTEXT.items():
        if key in model:
            context_limit = limit
            break
    pct = min(100, int(tokens_used / context_limit * 100)) if context_limit else 0
    bar = _make_progress_bar(pct)

    def _fmt_tokens(n):
        if n >= 1000000:
            return f"{n / 1000000:.1f}M"
        if n >= 1000:
            return f"{n // 1000}K"
        return str(n)

    lines = [
        "\U0001f527 *GooseClaw Status*",
        "",
        f"\U0001f4e1 Provider: {display_provider}",
        f"\U0001f916 Model: `{model}`",
        f"\u26a1 Mode: {goose_mode}",
        "",
        f"\U0001f4ac Session: `{session_id or 'none'}`",
        f"\U0001f4dd Messages: {msg_count}",
        f"\u23f1 Uptime: {uptime_str}",
        f"\U0001f9e9 Extensions: {ext_count} active",
        "",
        f"\U0001f4ca Context: ~{_fmt_tokens(tokens_used)} / {_fmt_tokens(context_limit)} tokens",
        f"`{bar}` {pct}%",
    ]
    send("\n".join(lines))


# register commands on the module-level router
_command_router.register("help", _handle_cmd_help, "this message")
_command_router.register("stop", _handle_cmd_stop, "cancel the current response")
_command_router.register("clear", _handle_cmd_clear, "wipe conversation and start fresh")
_command_router.register("restart", _handle_cmd_restart, "restart the engine without clearing history")
_command_router.register("compact", _handle_cmd_compact, "summarize history to save tokens")
_command_router.register("status", _handle_cmd_status, "show session and provider info")


def _set_session_default_provider(session_id):
    """Call /agent/update_provider on a fresh session to set the default provider.

    goosed doesn't reliably inherit global config into sessions, so we must
    explicitly set the provider via API after creating each session.
    """
    setup = load_setup()
    if not setup or not _INTERNAL_GOOSE_TOKEN:
        return
    provider = setup.get("provider_type", "")
    model = setup.get("model", "")
    if not provider:
        return
    if provider == "claude-code":
        model = "default"
    if not model:
        model = default_models.get(provider, "")
    try:
        payload = json.dumps({
            "provider": provider, "model": model, "session_id": session_id,
        }).encode("utf-8")
        conn = _goosed_conn(timeout=10)
        conn.request("POST", "/agent/update_provider", body=payload, headers={
            "Content-Type": "application/json",
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status in (200, 204):
            print(f"[session] set provider {provider}/{model} on {session_id}")
        else:
            print(f"[session] WARN: update_provider returned {resp.status} for {session_id}")
    except Exception as e:
        print(f"[session] WARN: failed to set provider on {session_id}: {e}")


def _create_goose_session():
    """Create a new session via POST /agent/start on goosed.

    Returns the session_id string, or None on failure.
    """
    if not _INTERNAL_GOOSE_TOKEN:
        return None

    try:
        payload = json.dumps({"working_dir": "/data"}).encode("utf-8")
        conn = _goosed_conn(timeout=10)
        conn.request("POST", "/agent/start", body=payload, headers={
            "Content-Type": "application/json",
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()

        if resp.status == 200:
            session = json.loads(body)
            sid = session.get("id") or session.get("session_id")
            if sid:
                print(f"[telegram] created session via /agent/start: {sid}")
                # set default provider on the session immediately
                # (goosed doesn't inherit global config into sessions reliably)
                _set_session_default_provider(str(sid))
                return str(sid)

        # fallback: try GET /sessions to find the latest
        conn = _goosed_conn(timeout=10)
        conn.request("GET", "/sessions", headers={
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()

        if resp.status == 200:
            sessions = json.loads(body)
            if isinstance(sessions, list) and sessions:
                sid = sessions[-1].get("id") or sessions[-1].get("session_id")
                if sid:
                    print(f"[telegram] using latest session from /sessions: {sid}")
                    return str(sid)

        print(f"[telegram] could not create session: /agent/start returned {resp.status}")
        return None

    except Exception as e:
        print(f"[telegram] session creation failed: {e}")
        return None


# ── memory writer (end-of-session learning) ──────────────────────────────────
# Tracks last message time per chat. After N minutes of idle, fetches the
# conversation and sends it through goose for memory extraction.

_memory_last_activity = {}        # chat_id (str) -> timestamp (float)
_memory_last_activity_lock = threading.Lock()
_memory_processed_sessions = set()  # session_ids already processed
_memory_writer_running = False

MEMORY_EXTRACT_PROMPT = """You are analyzing a conversation to extract learnings about the user.
Review the conversation below and extract ANY of these:

1. **User facts**: name, role, preferences, habits, interests, people mentioned, work context
2. **Corrections**: times the user corrected the agent ("no", "actually", "not like that")
3. **Preferences**: communication style, format preferences, tool preferences
4. **Important context**: deadlines, projects, relationships, recurring topics

Output a structured JSON object with these keys (omit empty ones):
{
  "user_facts": ["fact1", "fact2"],
  "corrections": ["correction1"],
  "preferences": ["preference1"],
  "context": ["context1"]
}

If there's nothing meaningful to extract, output: {"empty": true}

CONVERSATION:
"""

def _memory_touch(chat_id):
    """Record activity timestamp for a chat."""
    with _memory_last_activity_lock:
        _memory_last_activity[str(chat_id)] = time.time()


def _memory_writer_loop():
    """Background loop: check for idle sessions and extract memories."""
    global _memory_writer_running
    _memory_writer_running = True
    print("[memory-writer] started")

    while True:
        try:
            time.sleep(60)  # check every minute

            setup = load_setup()
            if not setup:
                continue
            if not setup.get("memory_writer_enabled", True):
                continue
            idle_minutes = setup.get("memory_idle_minutes", 10)
            idle_threshold = idle_minutes * 60

            # find idle chats
            now = time.time()
            idle_chats = []
            with _memory_last_activity_lock:
                for chat_id, last_time in list(_memory_last_activity.items()):
                    if now - last_time >= idle_threshold:
                        idle_chats.append(chat_id)

            for chat_id in idle_chats:
                # get session for this chat
                sid = _session_manager.get("telegram", chat_id)
                if not sid or sid in _memory_processed_sessions:
                    # already processed or no session, clear activity tracker
                    with _memory_last_activity_lock:
                        _memory_last_activity.pop(chat_id, None)
                    continue

                # mark as processed before starting (avoid duplicate runs)
                _memory_processed_sessions.add(sid)
                with _memory_last_activity_lock:
                    _memory_last_activity.pop(chat_id, None)

                # fetch conversation
                messages = _fetch_session_messages(sid)
                if not messages or len(messages) < 2:
                    continue

                # build conversation text (truncate to keep token count reasonable)
                convo_text = ""
                for msg in messages[-40:]:  # last 40 messages max
                    role = msg.get("role", "unknown")
                    text = msg.get("text", "")[:500]
                    convo_text += f"[{role}]: {text}\n\n"

                if len(convo_text.strip()) < 50:
                    continue

                print(f"[memory-writer] extracting from session {sid} ({len(messages)} msgs)")

                # send through goosed in a separate session
                try:
                    extract_sid = _create_goose_session()
                    if not extract_sid:
                        print("[memory-writer] could not create extraction session")
                        continue

                    prompt = MEMORY_EXTRACT_PROMPT + convo_text
                    response, error, _media = _do_rest_relay(prompt, extract_sid)
                    if error:
                        print(f"[memory-writer] extraction error: {error}")
                        continue

                    _process_memory_extraction(response)
                    print(f"[memory-writer] done for session {sid}")

                except Exception as e:
                    print(f"[memory-writer] error processing session {sid}: {e}")

        except Exception as e:
            print(f"[memory-writer] loop error: {e}")


def _classify_fact(fact):
    """Classify a fact into the correct user.md section based on keyword heuristics.

    Uses word-boundary matching to avoid false positives (e.g. "woodworking"
    should not match "work").
    """
    lower = fact.lower()

    def _has_word(keywords):
        """Check if any keyword appears as a whole word (word-boundary match)."""
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', lower):
                return True
        return False

    # people/contacts/relationships (check first, most specific)
    people_kw = ["cofounder", "co-founder", "manager", "colleague", "friend",
                 "wife", "husband", "partner", "brother", "sister", "boss",
                 "contact", "relationship", "mentor", "teammate"]
    if _has_word(people_kw):
        return "People"

    # preferences (check before work, "want" and "like" are common)
    pref_kw = ["prefer", "prefers", "like", "likes", "want", "wants",
               "always use", "always uses", "rather", "favorite", "favourite"]
    if _has_word(pref_kw):
        return "Preferences (Observed)"

    # interests / personal (check before work to avoid "woodworking" -> "work")
    interest_kw = ["hobby", "hobbies", "interest", "interested", "personal",
                   "side project", "passion", "curious"]
    if _has_word(interest_kw):
        return "Interests & Context"

    # work context / projects
    work_kw = ["project", "deadline", "company", "work", "team", "sprint",
               "client", "standup", "roadmap", "milestone"]
    if _has_word(work_kw):
        return "Work Context"

    return "Important Context"


def _fact_already_exists(fact, section_content):
    """Check if a fact (or close variant) already exists in a section. Simple lowercase substring check."""
    if not section_content:
        return False
    return fact.lower() in section_content.lower()


def _get_section_content(full_content, section_header):
    """Extract the text between a section header and the next ## header."""
    pattern = re.escape(section_header) + r'\n(.*?)(?=\n## |\Z)'
    match = re.search(pattern, full_content, re.DOTALL)
    return match.group(1) if match else ""


def _append_to_section(content, section_header, additions, timestamp):
    """Append additions text right after a section header in content."""
    if section_header in content:
        content = content.replace(
            section_header,
            f"{section_header}\n\n<!-- auto-extracted {timestamp} -->\n{additions}\n"
        )
    return content


def _process_memory_extraction(response_text):
    """Parse extraction response and append to identity files."""
    # try to find JSON in the response
    json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
    if not json_match:
        print("[memory-writer] no JSON found in response")
        return

    try:
        data = json.loads(json_match.group())
    except (json.JSONDecodeError, ValueError):
        print("[memory-writer] could not parse JSON from response")
        return

    if data.get("empty"):
        print("[memory-writer] nothing to extract")
        return

    identity_dir = os.path.join(DATA_DIR, "identity")
    timestamp = time.strftime("%Y-%m-%d %H:%M")

    # append user facts to user.md, routed to correct sections
    user_facts = data.get("user_facts", [])
    if user_facts:
        user_file = os.path.join(identity_dir, "user.md")
        if os.path.exists(user_file):
            with open(user_file, "r") as f:
                content = f.read()

            # group facts by target section
            by_section = {}
            for fact in user_facts:
                section = _classify_fact(fact)
                section_header = f"## {section}"
                # dedup: check if fact already exists anywhere in the file
                if _fact_already_exists(fact, content):
                    print(f"[memory-writer] skipping duplicate fact: {fact}")
                    continue
                by_section.setdefault(section_header, []).append(fact)

            # append each group to its section
            added = 0
            for section_header, facts in by_section.items():
                additions = "\n".join(f"- {fact}" for fact in facts)
                content = _append_to_section(content, section_header, additions, timestamp)
                added += len(facts)

            if added > 0:
                with open(user_file, "w") as f:
                    f.write(content)
                print(f"[memory-writer] added {added} facts to user.md")

    # append corrections to learnings (full schema format)
    corrections = data.get("corrections", [])
    if corrections:
        learnings_file = os.path.join(identity_dir, "learnings", "LEARNINGS.md")
        if os.path.exists(learnings_file):
            with open(learnings_file, "a") as f:
                date_str = time.strftime("%Y%m%d")
                iso_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                for i, correction in enumerate(corrections):
                    entry_id = f"LRN-{date_str}-AUTO-{i + 1}" if len(corrections) > 1 else f"LRN-{date_str}-AUTO"
                    f.write(
                        f"\n## [{entry_id}] auto-extracted\n\n"
                        f"**Logged**: {iso_ts}\n"
                        f"**Priority**: low\n"
                        f"**Status**: active\n"
                        f"**Category**: auto-extracted\n\n"
                        f"### Summary\n{correction}\n"
                    )
            print(f"[memory-writer] added {len(corrections)} corrections to LEARNINGS.md")

    # append preferences to user.md Preferences section (with dedup)
    preferences = data.get("preferences", [])
    if preferences:
        user_file = os.path.join(identity_dir, "user.md")
        if os.path.exists(user_file):
            with open(user_file, "r") as f:
                content = f.read()
            section_header = "## Preferences (Observed)"
            section_content = _get_section_content(content, section_header)

            new_prefs = [p for p in preferences if not _fact_already_exists(p, content)]
            if new_prefs:
                additions = "\n".join(f"- {pref}" for pref in new_prefs)
                content = _append_to_section(content, section_header, additions, timestamp)
                with open(user_file, "w") as f:
                    f.write(content)
                print(f"[memory-writer] added {len(new_prefs)} preferences to user.md")
            else:
                print(f"[memory-writer] skipping {len(preferences)} duplicate preferences")

    # append context to memory.md
    context = data.get("context", [])
    if context:
        memory_file = os.path.join(identity_dir, "memory.md")
        if os.path.exists(memory_file):
            with open(memory_file, "r") as f:
                content = f.read()
            new_ctx = [c for c in context if not _fact_already_exists(c, content)]
            if new_ctx:
                additions = "\n".join(f"- {ctx}" for ctx in new_ctx)
                content = _append_to_section(content, "## Lessons Learned", additions, timestamp)
                with open(memory_file, "w") as f:
                    f.write(content)
                print(f"[memory-writer] added {len(new_ctx)} context items to memory.md")


def start_memory_writer():
    """Start the memory writer background thread."""
    global _memory_writer_running
    if _memory_writer_running:
        return
    threading.Thread(target=_memory_writer_loop, daemon=True).start()


# ── telegram bot API helpers ────────────────────────────────────────────────

def _send_typing_action(bot_token, chat_id):
    """Send 'typing' chat action to telegram."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "action": "typing",
        }).encode()
        req = urllib.request.Request(url, data=payload)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # non-critical


def _update_goose_session_provider(session_id, model_config):
    """Call POST /agent/update_provider on goosed to hot-swap the model on a session.

    model_config is a dict with keys: provider, model.
    Skips the call if the session already has this model set (cached).
    """
    if not _INTERNAL_GOOSE_TOKEN or not session_id or not model_config:
        return

    mid = model_config.get("id", "")
    with _session_model_lock:
        if _session_model_cache.get(session_id) == mid:
            return  # already set
    try:
        provider = model_config.get("provider", "")
        model = model_config.get("model", "")
        payload = json.dumps({
            "provider": provider,
            "model": model,
            "session_id": session_id,
        }).encode()
        conn = _goosed_conn(timeout=10)
        conn.request("POST", "/agent/update_provider", body=payload, headers={
            "Content-Type": "application/json",
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status in (200, 204):
            with _session_model_lock:
                _session_model_cache[session_id] = mid
            print(f"[routing] updated session {session_id} to {provider}/{model}")
        else:
            print(f"[routing] update_provider returned {resp.status} for session {session_id}")
    except Exception as e:
        print(f"[routing] failed to update session provider: {e}")


def _relay_to_goosed(user_text, session_id, chat_id=None, channel=None,
                        flush_cb=None, verbosity=None, sock_ref=None, flush_interval=4.0,
                        content_blocks=None):
    """Send a user message to goosed via REST /reply and return the assistant's text.

    Returns (response_text, error_string, media_blocks). On success error_string is empty.
    If chat_id is provided and the session is stale, creates a new session and retries.
    If channel is provided, applies per-channel model routing before relaying.
    If flush_cb is provided and verbosity != "quiet", uses streaming relay.
    If sock_ref is a list, sock_ref[0] is set to the active connection for cancellation.
    If content_blocks is provided, sends multimodal content blocks instead of plain text.
    """
    if not _INTERNAL_GOOSE_TOKEN:
        return "", "Goose is not ready yet (no internal token). Please try again in a moment.", []

    # apply per-channel model routing
    if channel and session_id:
        setup = load_setup()
        if setup and setup.get("channel_routes"):
            model_cfg = get_model_for_channel(setup, channel)
            if model_cfg:
                _update_goose_session_provider(session_id, model_cfg)

    # choose relay function based on streaming params
    use_streaming = flush_cb and verbosity and verbosity != "quiet"
    if use_streaming:
        relay_fn = lambda txt, sid: _do_rest_relay_streaming(txt, sid, flush_cb, verbosity, content_blocks=content_blocks, sock_ref=sock_ref, flush_interval=flush_interval)
    else:
        relay_fn = lambda txt, sid: _do_rest_relay(txt, sid, content_blocks=content_blocks, sock_ref=sock_ref)

    text, err, media = relay_fn(user_text, session_id)

    # if error or empty response, try creating a new session and retrying
    # but NOT if the relay was cancelled (e.g. /stop or /clear killed it)
    cancelled = (sock_ref and len(sock_ref) > 1
                 and hasattr(sock_ref[1], 'is_set') and sock_ref[1].is_set())
    if err and chat_id and not cancelled:
        reason = err if err else "empty response"
        ch = channel or "telegram"
        print(f"[{ch}] relay failed ({reason}), creating new session")
        new_sid = _create_goose_session()
        if new_sid:
            _session_manager.set(ch, str(chat_id), new_sid)
            print(f"[{ch}] retrying with new session {new_sid}")
            return relay_fn(user_text, new_sid)

    return text, err, media


def _diagnose_empty_response():
    """When goose returns 0 content, figure out WHY and return a useful error."""
    # 1. check goose stderr for recent errors
    stderr = _get_recent_stderr(10)
    if stderr:
        for line in stderr.strip().split("\n"):
            low = line.lower()
            if "error" in low or "failed" in low or "unauthorized" in low or "401" in low:
                return f"Goose error: {line.strip()}"

    # 2. check if goose process is alive
    if goosed_process and goosed_process.poll() is not None:
        return "Goose crashed. Restarting..."

    # 3. test the provider directly (claude-code specific)
    setup = load_setup()
    provider = setup.get("provider_type", "") if setup else ""
    if provider == "claude-code":
        try:
            r = subprocess.run(
                ["claude", "-p", "hi", "--output-format", "text", "--max-turns", "1", "--dangerously-skip-permissions"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                output = (r.stdout.strip() or r.stderr.strip())[:300]
                if "401" in output or "authentication" in output.lower() or "bearer" in output.lower():
                    return f"Claude auth failed. Your OAuth token is expired or invalid. Update it in the setup wizard."
                if "429" in output or "rate" in output.lower():
                    return f"Claude rate limited. Try again in a minute."
                return f"Claude CLI failed: {output}"
        except subprocess.TimeoutExpired:
            return "Claude CLI timed out. The API may be down."
        except Exception as e:
            return f"Could not test Claude CLI: {e}"

    return "No response from goose. Check provider configuration."


# ── streaming helpers ────────────────────────────────────────────────────────

def _truncate(text, max_len=500):
    """Truncate text with ellipsis if it exceeds max_len."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


class _StreamBuffer:
    """Accumulates text chunks and flushes to a callback on triggers."""

    def __init__(self, flush_cb, interval=4.0):
        self._buf = []
        self._flush_cb = flush_cb
        self._interval = interval
        self._last_flush = time.time()
        self._char_count = 0

    def append(self, text):
        self._buf.append(text)
        self._char_count += len(text)
        if self._should_flush(text):
            self.flush()

    def _should_flush(self, latest_text):
        if time.time() - self._last_flush >= self._interval:
            return True
        if self._char_count > 3000:
            return True
        if latest_text.endswith("\n\n"):
            return True
        return False

    def flush(self):
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._char_count = 0
        self._last_flush = time.time()
        if text.strip():
            try:
                self._flush_cb(text)
            except Exception as e:
                print(f"[stream] flush error: {e}")

    def flush_final(self):
        self.flush()


# ── REST relay helpers (Phase 13) ─────────────────────────────────────────────


def _parse_sse_events(response):
    """Yield parsed SSE events from an HTTP response with readline().

    SSE format: lines starting with "data: " followed by JSON.
    Events separated by blank lines. Invalid JSON is silently skipped.
    """
    while True:
        line = response.readline()
        if not line:
            break  # connection closed or EOF
        line = line.decode("utf-8", errors="replace").rstrip("\r\n")

        if line.startswith("data: "):
            data_str = line[6:]
            try:
                yield json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue


def _build_content_blocks(user_text, inbound_msg=None):
    """Build content block array for ChatRequest.

    user_text: the user's text message (may be empty for media-only)
    inbound_msg: optional InboundMessage with media attachments
    Returns: list of content block dicts
    """
    blocks = []
    if user_text and user_text.strip():
        blocks.append({"type": "text", "text": user_text})

    if inbound_msg and inbound_msg.has_media:
        for mc in inbound_msg.media:
            if isinstance(mc, MediaContent):
                block = mc.to_content_block()
                if block:
                    blocks.append(block)

    # fallback: if no blocks at all, send empty text
    if not blocks:
        blocks.append({"type": "text", "text": ""})

    return blocks


def _extract_response_content(content_array):
    """Extract text and media from a Message content array.

    Returns: (text_str, media_blocks_list)
    text_str: concatenated text from all text blocks (newline-joined)
    media_blocks_list: list of non-text content block dicts (image, etc.)
    """
    text_parts = []
    media_blocks = []
    for block in content_array:
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "image":
            media_blocks.append(block)
        elif btype == "toolResponse":
            # tool results may contain nested content with images
            result = block.get("tool_result", {})
            if isinstance(result, dict):
                nested = result.get("value", {}).get("content", [])
                if isinstance(nested, list):
                    for item in nested:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                            elif item.get("type") == "image":
                                media_blocks.append(item)
        # thinking, reasoning, systemNotification: skip for user output
    return "\n".join(text_parts), media_blocks


def _do_rest_relay(user_text, session_id, content_blocks=None, sock_ref=None):
    """POST to goosed /reply, parse SSE response.

    Returns (response_text, error_string, media_blocks).
    On success error_string is empty and media_blocks contains any image blocks.
    """
    t0 = time.time()
    blocks = content_blocks or [{"type": "text", "text": user_text}]

    chat_request = json.dumps({
        "session_id": session_id,
        "user_message": {
            "role": "user",
            "created": int(time.time()),
            "content": blocks,
            "metadata": {"userVisible": True, "agentVisible": True},
        }
    }).encode("utf-8")

    conn = None
    try:
        conn = _goosed_conn(timeout=300)
        if sock_ref is not None:
            sock_ref[0] = conn  # for external cancellation

        conn.request("POST", "/reply", body=chat_request, headers={
            "Content-Type": "application/json",
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
            "Accept": "text/event-stream",
        })

        resp = conn.getresponse()
        if resp.status != 200:
            body = resp.read().decode("utf-8", errors="replace")
            conn.close()
            return "", f"goosed /reply returned {resp.status}: {body[:200]}", []

        text_parts = []
        media_blocks = []

        for event in _parse_sse_events(resp):
            etype = event.get("type", "")

            if etype == "Message":
                msg = event.get("message", {})
                content = msg.get("content", [])
                t, m = _extract_response_content(content)
                if t:
                    text_parts.append(t)
                media_blocks.extend(m)

            elif etype == "Error":
                err_msg = event.get("error", "Unknown error")
                conn.close()
                return "", f"Goose error: {err_msg}", []

            elif etype == "Finish":
                break

        conn.close()
        full_text = "\n".join(text_parts).strip()
        elapsed = time.time() - t0
        print(f"[rest-relay] done in {elapsed:.1f}s ({len(full_text)} chars) session={session_id}")
        return full_text, "", media_blocks

    except socket.timeout:
        elapsed = time.time() - t0
        print(f"[rest-relay] TIMEOUT after {elapsed:.1f}s session={session_id}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return "", "Goose took too long to respond (timeout). Try again.", []

    except ConnectionError as e:
        print(f"[rest-relay] connection error after {time.time() - t0:.1f}s: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return "", f"Connection error: {e}", []

    except Exception as e:
        print(f"[rest-relay] error after {time.time() - t0:.1f}s: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return "", f"Error communicating with goose: {e}", []


def _do_rest_relay_streaming(user_text, session_id, flush_cb, verbosity="balanced",
                             content_blocks=None, sock_ref=None, flush_interval=4.0):
    """POST to goosed /reply, stream response via flush_cb.

    Like _do_rest_relay but delivers text incrementally through flush_cb and
    emits tool/thinking status based on verbosity level.
    Returns (full_text, error_string, media_blocks).
    """
    t0 = time.time()
    blocks = content_blocks or [{"type": "text", "text": user_text}]
    print(f"[rest-relay-stream] start session={session_id} verbosity={verbosity} text={user_text[:50]!r}")

    chat_request = json.dumps({
        "session_id": session_id,
        "user_message": {
            "role": "user",
            "created": int(time.time()),
            "content": blocks,
            "metadata": {"userVisible": True, "agentVisible": True},
        }
    }).encode("utf-8")

    buf = _StreamBuffer(flush_cb, interval=flush_interval)
    collected = []
    media_blocks = []
    conn = None

    try:
        conn = _goosed_conn(timeout=300)
        if sock_ref is not None:
            sock_ref[0] = conn

        conn.request("POST", "/reply", body=chat_request, headers={
            "Content-Type": "application/json",
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
            "Accept": "text/event-stream",
        })

        resp = conn.getresponse()
        if resp.status != 200:
            body = resp.read().decode("utf-8", errors="replace")
            conn.close()
            return "", f"goosed /reply returned {resp.status}: {body[:200]}", []

        for event in _parse_sse_events(resp):
            etype = event.get("type", "")

            if etype == "Message":
                msg = event.get("message", {})
                content = msg.get("content", [])
                for block in content:
                    btype = block.get("type", "")

                    if btype == "text":
                        text = block.get("text", "")
                        if text:
                            collected.append(text)
                            buf.append(text)

                    elif btype == "image":
                        media_blocks.append(block)

                    elif btype == "toolRequest":
                        # flush pending text before tool status
                        buf.flush()
                        if verbosity == "verbose":
                            tool_call = block.get("tool_call", {})
                            tool_name = tool_call.get("name", "tool")
                            args = tool_call.get("arguments", {})
                            if isinstance(args, dict):
                                parts = [f'{k}="{v}"' for k, v in list(args.items())[:3]]
                                args_str = ", ".join(parts)
                            else:
                                args_str = _truncate(str(args), 100) if args else ""
                            status = f"[Using {tool_name}({args_str})]" if args_str else f"[Using {tool_name}]"
                            try:
                                flush_cb(status)
                            except Exception as e:
                                print(f"[rest-relay-stream] tool status error: {e}")

                    elif btype == "thinking":
                        if verbosity == "verbose":
                            thinking_text = block.get("thinking", "")
                            if thinking_text:
                                buf.flush()
                                truncated = _truncate(thinking_text, 500)
                                try:
                                    flush_cb(f"_{truncated}_")
                                except Exception as e:
                                    print(f"[rest-relay-stream] thinking error: {e}")

                    elif btype == "toolResponse":
                        # extract nested text/images from tool results
                        result = block.get("tool_result", {})
                        if isinstance(result, dict):
                            nested = result.get("value", {}).get("content", [])
                            if isinstance(nested, list):
                                for item in nested:
                                    if isinstance(item, dict):
                                        if item.get("type") == "image":
                                            media_blocks.append(item)

            elif etype == "Error":
                buf.flush()
                err_msg = event.get("error", "Unknown error")
                print(f"[rest-relay-stream] error event after {time.time() - t0:.1f}s: {err_msg}")
                conn.close()
                return "".join(collected).strip(), f"Goose error: {err_msg}", media_blocks

            elif etype == "Finish":
                break

        buf.flush_final()
        conn.close()
        full_text = "".join(collected).strip()
        elapsed = time.time() - t0
        print(f"[rest-relay-stream] done in {elapsed:.1f}s ({len(full_text)} chars) session={session_id}")
        if not full_text:
            return "", _diagnose_empty_response(), media_blocks
        return full_text, "", media_blocks

    except socket.timeout:
        elapsed = time.time() - t0
        print(f"[rest-relay-stream] TIMEOUT after {elapsed:.1f}s session={session_id}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        partial = "".join(collected).strip() if collected else ""
        if partial:
            return partial, "", media_blocks
        return "", "Goose took too long to respond (timeout). Try again.", media_blocks

    except ConnectionError as e:
        print(f"[rest-relay-stream] connection error after {time.time() - t0:.1f}s: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return "", f"Connection error: {e}", media_blocks

    except Exception as e:
        print(f"[rest-relay-stream] error after {time.time() - t0:.1f}s: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return "", f"Error communicating with goose: {e}", media_blocks


# ── pairing helpers (self-contained, no Rust subprocess) ────────────────────

def _add_pairing_to_config(chat_id, platform="telegram"):
    """Add a pairing entry to goose config.yaml (gateway_pairings section).

    platform: the platform tag to write (e.g. "telegram", "telegram:research").
    """
    config_path = GOOSE_CONFIG_PATH
    chat_str = str(chat_id)
    # always cache in memory first (survives config.yaml race rewrites)
    _cache_pairing(chat_str, platform)
    try:
        content = ""
        if os.path.exists(config_path):
            with open(config_path) as f:
                content = f.read()

        # check if already paired
        if chat_str in content:
            # crude check -- good enough since chat IDs are unique numeric strings
            return

        pairing_entry = (
            f"  - platform: {platform}\n"
            f"    user_id: '{chat_str}'\n"
            f"    state: paired\n"
        )
        if "gateway_pairings:" in content:
            # append to existing section
            content = content.replace("gateway_pairings:\n", "gateway_pairings:\n" + pairing_entry, 1)
        else:
            content = content.rstrip("\n") + "\ngateway_pairings:\n" + pairing_entry

        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, config_path)
        print(f"[{platform}] paired chat_id {chat_str}")
    except Exception as e:
        print(f"[{platform}] warn: could not write pairing: {e}")


def _generate_and_store_pair_code():
    """Generate a random 6-char alphanumeric pairing code and store globally."""
    global telegram_pair_code
    # generate a 6-character uppercase alphanumeric code
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    code = "".join(secrets.choice(alphabet) for _ in range(6))
    with telegram_pair_lock:
        telegram_pair_code = code
    print(f"[telegram] pairing code: {code}")
    return code


# ── telegram polling loop ───────────────────────────────────────────────────

def _flush_media_group(group_id, bot_token):
    """Flush a buffered media group: combine all refs and relay as one message."""
    with _media_group_lock:
        group = _media_group_buffer.pop(group_id, None)
    if not group:
        return

    chat_id = group["chat_id"]
    text = group["text"]
    refs = group["refs"]

    paired_ids = get_paired_chat_ids()
    if chat_id not in paired_ids:
        return

    def _do_group_relay(_text=text, _chat_id=chat_id, _bt=bot_token, _refs=refs):
        _memory_touch(_chat_id)
        _pending_greet = _legacy_greeting_events.get(str(_chat_id))
        if _pending_greet:
            _pending_greet.wait(timeout=30)
        _chat_lock = _get_chat_lock(_chat_id)
        if not _chat_lock.acquire(timeout=2):
            _mreplay = lambda: _do_group_relay(_text, _chat_id, _bt, _refs)
            _telegram_state.queue_message(_chat_id, _text, replay_fn=_mreplay)
            send_telegram_message(_bt, _chat_id, "got it, i'll get to this next")
            return
        try:
            downloaded = []
            for ref in _refs:
                file_bytes, file_path = _download_telegram_file(_bt, ref["file_id"])
                if file_bytes is not None:
                    mc = _make_media_content(
                        ref.get("media_key", "document"),
                        file_bytes, file_path,
                        mime_hint=ref.get("mime_hint"),
                        filename=ref.get("filename"),
                    )
                    downloaded.append(mc)
                else:
                    print(f"[telegram] media group download failed for {ref.get('media_key')}: {file_path}")
            _send_typing_action(_bt, _chat_id)
            session_id = _get_session_id(_chat_id)
            _inbound = InboundMessage(user_id=_chat_id, text=_text, channel="telegram")
            _inbound.media = downloaded
            _leg_cb = _build_content_blocks(_text, _inbound) if _inbound.has_media else None

            _cancelled = threading.Event()
            _sock_ref = [None, _cancelled]
            _telegram_state.set_active_relay(_chat_id, _sock_ref)

            _tg_setup = load_setup()
            _tg_verbosity = get_verbosity_for_channel(_tg_setup, "telegram") if _tg_setup else "balanced"

            typing_stop = threading.Event()
            def _typing_loop():
                while not typing_stop.is_set():
                    _send_typing_action(_bt, _chat_id)
                    typing_stop.wait(4)
            typing_thread = threading.Thread(target=_typing_loop, daemon=True)
            typing_thread.start()

            try:
                response_text, error, _resp_media = _relay_to_goosed(
                    _text, session_id, chat_id=_chat_id, channel="telegram",
                    content_blocks=_leg_cb, sock_ref=_sock_ref,
                )
                if _cancelled.is_set():
                    pass
                elif error:
                    send_telegram_message(_bt, _chat_id, f"Error: {error}")
                elif response_text:
                    send_telegram_message(_bt, _chat_id, response_text)
                if _resp_media:
                    try:
                        _adapter = TelegramOutboundAdapter(_bt, _chat_id)
                        _route_media_blocks(_resp_media, _adapter)
                    except Exception as _media_exc:
                        print(f"[telegram] media routing error: {_media_exc}")
            finally:
                typing_stop.set()
                _telegram_state.pop_active_relay(_chat_id)
        except Exception as exc:
            print(f"[telegram] media group relay exception for chat {_chat_id}: {exc}")
        finally:
            _chat_lock.release()
            _mq = _telegram_state.pop_queued_replay(_chat_id)
            if _mq:
                _, _mq_fn = _mq
                if _mq_fn:
                    threading.Thread(target=_mq_fn, daemon=True).start()

    threading.Thread(target=_do_group_relay, daemon=True).start()


def _telegram_poll_loop(bot_token):
    """Long-poll telegram for updates and relay messages to goosed.

    Runs in a daemon thread. Handles pairing and message relay.
    """
    global _telegram_running, telegram_pair_code
    offset = 0
    _telegram_running = True
    print("[telegram] polling loop started")

    while _telegram_running:
        try:
            url = (
                f"https://api.telegram.org/bot{bot_token}/getUpdates"
                f"?offset={offset}&timeout=30&allowed_updates=[\"message\"]"
            )
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read())

            if not data.get("ok"):
                print(f"[telegram] getUpdates not ok: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()

                if not chat_id:
                    continue

                # build media file_id references for deferred download
                has_media = _has_media(msg)
                media_refs = []
                if has_media:
                    if not text:
                        text = msg.get("caption", "").strip()
                    for mkey in _MEDIA_KEYS:
                        if mkey in msg:
                            fid, mime_hint, fname = _extract_file_info(msg, mkey)
                            if fid:
                                media_refs.append({
                                    "media_key": mkey,
                                    "file_id": fid,
                                    "mime_hint": mime_hint,
                                    "filename": fname,
                                })

                # ── media group buffering ──
                # Telegram sends multi-image messages as separate updates with
                # the same media_group_id. Buffer them and flush after ~1s.
                mg_id = msg.get("media_group_id")
                if mg_id and has_media and media_refs:
                    with _media_group_lock:
                        if mg_id in _media_group_buffer:
                            # add refs to existing group, reset timer
                            group = _media_group_buffer[mg_id]
                            group["refs"].extend(media_refs)
                            if not group["text"] and text:
                                group["text"] = text
                            if group.get("timer"):
                                group["timer"].cancel()
                        else:
                            _media_group_buffer[mg_id] = {
                                "chat_id": chat_id,
                                "text": text or msg.get("caption", "").strip(),
                                "refs": list(media_refs),
                            }
                            group = _media_group_buffer[mg_id]
                        # set/reset 1s flush timer
                        timer = threading.Timer(1.0, _flush_media_group, args=(mg_id, bot_token))
                        timer.daemon = True
                        group["timer"] = timer
                        timer.start()
                    continue

                # media-only from paired users: relay (no MEDIA_REPLY)
                # unpaired users with media: silently ignore
                if has_media and not text:
                    paired_ids = get_paired_chat_ids()
                    if chat_id in paired_ids:
                        def _do_media_relay(_text="", _chat_id=chat_id, _bt=bot_token, _refs=media_refs):
                            _memory_touch(_chat_id)
                            # Change 5: wait for pending greeting before acquiring lock
                            _pending_greet = _legacy_greeting_events.get(str(_chat_id))
                            if _pending_greet:
                                _pending_greet.wait(timeout=30)
                            _chat_lock = _get_chat_lock(_chat_id)
                            if not _chat_lock.acquire(timeout=2):
                                _mreplay = lambda: _do_media_relay(_text, _chat_id, _bt, _refs)
                                _telegram_state.queue_message(_chat_id, _text, replay_fn=_mreplay)
                                send_telegram_message(_bt, _chat_id, "got it, i'll get to this next")
                                return
                            try:
                                # download media in relay thread
                                downloaded = []
                                for ref in _refs:
                                    file_bytes, file_path = _download_telegram_file(_bt, ref["file_id"])
                                    if file_bytes is not None:
                                        mc = _make_media_content(
                                            ref.get("media_key", "document"),
                                            file_bytes, file_path,
                                            mime_hint=ref.get("mime_hint"),
                                            filename=ref.get("filename"),
                                        )
                                        downloaded.append(mc)
                                    else:
                                        print(f"[telegram] media download failed for {ref.get('media_key')}: {file_path}")
                                _send_typing_action(_bt, _chat_id)
                                session_id = _get_session_id(_chat_id)
                                # build content blocks from downloaded media
                                _inbound = InboundMessage(user_id=_chat_id, text=_text, channel="telegram")
                                _inbound.media = downloaded
                                _leg_cb = _build_content_blocks(_text, _inbound) if _inbound.has_media else None
                                response_text, error, _resp_media = _relay_to_goosed(
                                    _text, session_id, chat_id=_chat_id, channel="telegram",
                                    content_blocks=_leg_cb,
                                )
                                if error:
                                    send_telegram_message(_bt, _chat_id, f"Error: {error}")
                                elif response_text:
                                    send_telegram_message(_bt, _chat_id, response_text)
                                # Route response media blocks
                                if _resp_media:
                                    try:
                                        _adapter = TelegramOutboundAdapter(_bt, _chat_id)
                                        _route_media_blocks(_resp_media, _adapter)
                                    except Exception as _media_exc:
                                        print(f"[telegram] media routing error: {_media_exc}")
                            except Exception as exc:
                                print(f"[telegram] media relay exception for chat {_chat_id}: {exc}")
                            finally:
                                _chat_lock.release()
                                # process queued messages
                                _mq = _telegram_state.pop_queued_replay(_chat_id)
                                if _mq:
                                    _, _mq_fn = _mq
                                    if _mq_fn:
                                        threading.Thread(target=_mq_fn, daemon=True).start()
                        threading.Thread(target=_do_media_relay, daemon=True).start()
                    continue

                if not text:
                    continue

                paired_ids = get_paired_chat_ids()

                if chat_id in paired_ids:
                    # ── handle local slash commands before relaying ──
                    lower = text.lower()

                    if _command_router.is_command(lower):
                        ctx = {
                            "channel": "telegram",
                            "user_id": chat_id,
                            "bot_token": bot_token,
                            "send_fn": lambda t, _bt=bot_token, _cid=chat_id: send_telegram_message(_bt, _cid, t),
                        }
                        if not _command_router.dispatch(text, ctx):
                            send_telegram_message(bot_token, chat_id,
                                f"Unknown command: {text.split()[0]}\nSend /help for available commands.")
                        continue

                    # ── catch unknown slash commands ──
                    if lower.startswith("/"):
                        send_telegram_message(
                            bot_token, chat_id,
                            f"Unknown command: {text.split()[0]}\nSend /help for available commands."
                        )
                        continue

                    # ── relay to goosed (runs in a background thread) ──
                    # threaded so the poll loop stays responsive for /stop commands.
                    # per-chat lock prevents concurrent relays per user.
                    def _do_relay(_text=text, _chat_id=chat_id, _bt=bot_token, _media_refs=media_refs):
                        _memory_touch(_chat_id)
                        # Change 5: wait for pending greeting before acquiring lock
                        _pending_greet = _legacy_greeting_events.get(str(_chat_id))
                        if _pending_greet:
                            _pending_greet.wait(timeout=30)
                        _chat_lock = _get_chat_lock(_chat_id)
                        if not _chat_lock.acquire(timeout=2):
                            # another relay is running for this chat
                            _rreplay = lambda: _do_relay(_text, _chat_id, _bt, _media_refs)
                            _telegram_state.queue_message(_chat_id, _text, replay_fn=_rreplay)
                            send_telegram_message(_bt, _chat_id, "got it, i'll get to this next")
                            return
                        try:
                            # download media in relay thread
                            if _media_refs:
                                downloaded = []
                                for ref in _media_refs:
                                    file_bytes, file_path = _download_telegram_file(_bt, ref["file_id"])
                                    if file_bytes is not None:
                                        mc = _make_media_content(
                                            ref.get("media_key", "document"),
                                            file_bytes, file_path,
                                            mime_hint=ref.get("mime_hint"),
                                            filename=ref.get("filename"),
                                        )
                                        downloaded.append(mc)
                                    else:
                                        print(f"[telegram] media download failed for {ref.get('media_key')}: {file_path}")

                            _send_typing_action(_bt, _chat_id)
                            session_id = _get_session_id(_chat_id)
                            _cancelled = threading.Event()
                            _sock_ref = [None, _cancelled]

                            _telegram_state.set_active_relay(_chat_id, _sock_ref)

                            _tg_setup = load_setup()
                            _tg_verbosity = get_verbosity_for_channel(_tg_setup, "telegram") if _tg_setup else "balanced"

                            # build content blocks from downloaded media
                            _leg_content_blocks = None
                            if _media_refs and downloaded:
                                _leg_inbound = InboundMessage(user_id=_chat_id, text=_text, channel="telegram")
                                _leg_inbound.media = downloaded
                                _leg_content_blocks = _build_content_blocks(_text, _leg_inbound)

                            # typing indicator loop
                            typing_stop = threading.Event()

                            def _typing_loop():
                                while not typing_stop.is_set():
                                    _send_typing_action(_bt, _chat_id)
                                    typing_stop.wait(4)

                            typing_thread = threading.Thread(target=_typing_loop, daemon=True)
                            typing_thread.start()

                            try:
                                if _tg_verbosity == "quiet":
                                    response_text, error, _leg_media = _relay_to_goosed(
                                        _text, session_id, chat_id=_chat_id, channel="telegram",
                                        sock_ref=_sock_ref, content_blocks=_leg_content_blocks,
                                    )
                                    if _cancelled.is_set():
                                        pass  # /stop was called, don't send anything
                                    elif error:
                                        send_telegram_message(_bt, _chat_id, f"Error: {error}")
                                    else:
                                        send_telegram_message(_bt, _chat_id, response_text)
                                else:
                                    # streaming: edit-in-place
                                    _edit_state = {"msg_id": None, "accumulated": "", "overflow": []}

                                    def _tg_flush_edit(chunk, _st=_edit_state):
                                        if _cancelled.is_set():
                                            return
                                        _st["accumulated"] += chunk
                                        txt = _st["accumulated"]
                                        if _st["msg_id"] is None:
                                            mid, err = _send_telegram_msg_with_id(_bt, _chat_id, txt)
                                            if mid:
                                                _st["msg_id"] = mid
                                            else:
                                                print(f"[telegram] edit-stream: initial send failed: {err}")
                                        elif len(txt) > 3800:
                                            _st["overflow"].append(_st["msg_id"])
                                            _st["accumulated"] = chunk
                                            _st["msg_id"] = None
                                            mid, err = _send_telegram_msg_with_id(_bt, _chat_id, chunk)
                                            if mid:
                                                _st["msg_id"] = mid
                                        else:
                                            _edit_telegram_message(_bt, _chat_id, _st["msg_id"], txt)

                                    response_text, error, _leg_media = _relay_to_goosed(
                                        _text, session_id, chat_id=_chat_id, channel="telegram",
                                        flush_cb=_tg_flush_edit, verbosity=_tg_verbosity,
                                        sock_ref=_sock_ref, flush_interval=2.0,
                                        content_blocks=_leg_content_blocks,
                                    )
                                    if _cancelled.is_set():
                                        pass  # /stop was called, don't send final edit
                                    elif _edit_state["msg_id"] and response_text and not error:
                                        final_text = _edit_state["accumulated"] or response_text
                                        if final_text:
                                            _edit_telegram_message(_bt, _chat_id, _edit_state["msg_id"], final_text)
                                    elif error:
                                        send_telegram_message(_bt, _chat_id, f"Error: {error}")
                                    elif not _edit_state["msg_id"] and response_text:
                                        send_telegram_message(_bt, _chat_id, response_text)

                                # Route media blocks through adapter
                                if _leg_media and not _cancelled.is_set():
                                    try:
                                        _adapter = TelegramOutboundAdapter(_bt, _chat_id)
                                        _route_media_blocks(_leg_media, _adapter)
                                    except Exception as _media_exc:
                                        print(f"[telegram] media routing error: {_media_exc}")
                            finally:
                                typing_stop.set()
                                typing_thread.join(timeout=2)
                        except Exception as exc:
                            print(f"[telegram] relay exception for chat {_chat_id}: {exc}")
                            if not _cancelled.is_set():
                                try:
                                    send_telegram_message(_bt, _chat_id, f"Error: {exc}")
                                except Exception:
                                    pass
                        finally:
                            _telegram_state.pop_active_relay(_chat_id)
                            _chat_lock.release()
                            # process queued messages
                            _rq = _telegram_state.pop_queued_replay(_chat_id)
                            if _rq:
                                _, _rq_fn = _rq
                                if _rq_fn:
                                    threading.Thread(target=_rq_fn, daemon=True).start()

                    threading.Thread(target=_do_relay, daemon=True).start()
                else:
                    # unpaired user — check if this is a pairing code
                    with telegram_pair_lock:
                        current_code = telegram_pair_code

                    if current_code and text.upper() == current_code.upper():
                        # valid pairing code — pair this chat
                        _add_pairing_to_config(chat_id)
                        with telegram_pair_lock:
                            # consume the code so it can't be reused
                            telegram_pair_code = None
                        send_telegram_message(
                            bot_token, chat_id,
                            "Paired successfully! You can now send messages to goose through this chat."
                        )
                        print(f"[telegram] chat {chat_id} paired via code {current_code}")

                        # auto-send first message after pairing
                        try:
                            soul_path = os.path.join(IDENTITY_DIR, "soul.md")
                            needs_onboarding = False
                            try:
                                with open(soul_path, "r") as _sf:
                                    needs_onboarding = "ONBOARDING_NEEDED" in _sf.read()
                            except FileNotFoundError:
                                needs_onboarding = True

                            # Change 1: static welcome message right after pairing
                            if needs_onboarding:
                                send_telegram_message(
                                    bot_token, chat_id,
                                    "hey! i'm gooseclaw, your personal AI agent. i run 24/7 on your server, learn how you think, and remember everything.\n\ni'm setting up now. give me a few seconds and i'll introduce myself properly."
                                )
                            else:
                                send_telegram_message(
                                    bot_token, chat_id,
                                    "welcome back! new device paired. give me a moment."
                                )

                            # Change 3: inject context into LLM kick message
                            kick_msg = (
                                "I just paired via Telegram. I've already been shown a welcome message saying I'm gooseclaw, a personal AI agent that runs 24/7 and learns. Do NOT repeat any of that. Jump straight into the onboarding flow -- ask my name. Keep your response to 2-3 short sentences. Use normal prose, no bullet points, no line breaks between words. Plain text only, no markdown formatting."
                                if needs_onboarding else
                                "I just paired a new device via Telegram. I've already seen a 'welcome back' message. Just say hi casually, keep it very short. Keep your response to 2-3 short sentences. Use normal prose, no bullet points, no line breaks between words. Plain text only, no markdown formatting."
                            )
                            sid = _get_session_id(chat_id)
                            # Change 4: skip LLM kick greeting for returning users
                            if sid and needs_onboarding:
                                # Change 5: greeting event to prevent message collision
                                _greet_evt = threading.Event()
                                _legacy_greeting_events[str(chat_id)] = _greet_evt

                                def _kick_greeting(msg=kick_msg, s=sid, c=chat_id, evt=_greet_evt):
                                    # Change 2: typing indicator during kick_greeting
                                    _typing_stop = threading.Event()

                                    def _typing_loop():
                                        while not _typing_stop.is_set():
                                            _send_typing_action(bot_token, c)
                                            _typing_stop.wait(4)

                                    _typing_thread = threading.Thread(target=_typing_loop, daemon=True)
                                    _typing_thread.start()
                                    try:
                                        with _get_chat_lock(c):
                                            txt, err, *_ = _relay_to_goosed(
                                                msg, s, chat_id=str(c), channel="telegram",
                                            )
                                            if err:
                                                print(f"[telegram] kick greeting error: {err}")
                                                send_telegram_message(bot_token, c, f"Error: {err}")
                                            elif txt:
                                                send_telegram_message(bot_token, c, txt)
                                    except Exception as exc:
                                        print(f"[telegram] kick greeting exception: {exc}")
                                    finally:
                                        _typing_stop.set()
                                        evt.set()
                                        _legacy_greeting_events.pop(str(c), None)
                                threading.Thread(target=_kick_greeting, daemon=True).start()
                        except Exception as exc:
                            print(f"[telegram] kick greeting setup failed: {exc}")
                    else:
                        send_telegram_message(
                            bot_token, chat_id,
                            "You are not paired with this goose instance. "
                            "Please enter a valid pairing code from the web dashboard."
                        )

        except urllib.error.HTTPError as e:
            if e.code == 409:
                # conflict — another getUpdates call is running; back off
                print("[telegram] conflict (409), backing off 10s")
                time.sleep(10)
            elif e.code == 401:
                print("[telegram] FATAL: invalid bot token (401). Stopping poll loop.")
                _telegram_running = False
                return
            else:
                print(f"[telegram] HTTP error {e.code}, retrying in 5s")
                time.sleep(5)
        except urllib.error.URLError as e:
            print(f"[telegram] network error: {e.reason}, retrying in 5s")
            time.sleep(5)
        except Exception as e:
            print(f"[telegram] poll error: {e}, retrying in 5s")
            time.sleep(5)

    print("[telegram] polling loop stopped")


def start_telegram_gateway(bot_token):
    """Start the default telegram bot. Backward-compatible entry point."""
    existing = _bot_manager.get_bot("default")
    if existing and existing.running:
        print("[telegram] polling already running")
        return
    try:
        bot = _bot_manager.add_bot("default", bot_token, channel_key="telegram")
        bot.start()
    except ValueError as e:
        print(f"[telegram] error: {e}")


def _configure_goosed_provider():
    """Call POST /config/set_provider on goosed to set the LLM provider.

    goosed has its own config system that ignores GOOSE_PROVIDER env vars.
    Must be called via API after goosed is ready.
    """
    setup = load_setup()
    if not setup or not _INTERNAL_GOOSE_TOKEN:
        return
    provider = setup.get("provider_type", "")
    model = setup.get("model", "")
    if not provider:
        return
    # claude-code uses "default" model
    if provider == "claude-code":
        model = "default"
    if not model:
        model = default_models.get(provider, "")
    try:
        payload = json.dumps({"provider": provider, "model": model}).encode("utf-8")
        conn = _goosed_conn(timeout=10)
        conn.request("POST", "/config/set_provider", body=payload, headers={
            "Content-Type": "application/json",
            "X-Secret-Key": _INTERNAL_GOOSE_TOKEN,
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status in (200, 204):
            print(f"[gateway] configured goosed provider: {provider}/{model}")
        else:
            print(f"[gateway] WARN: /config/set_provider returned {resp.status}: {body[:200]}")
    except Exception as e:
        print(f"[gateway] WARN: failed to configure goosed provider: {e}")


def start_goosed():
    global goosed_process, _INTERNAL_GOOSE_TOKEN
    _check_stale_pid("goosed")
    _set_startup_state("starting", "Starting goosed...")
    with goose_lock:
        if goosed_process and goosed_process.poll() is None:
            goosed_process.terminate()
            try:
                goosed_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                goosed_process.kill()

        # Generate a random internal token for goosed communication.
        # This token is never exposed to users -- gateway handles all user auth.
        # Users authenticate against the stored hash; gateway then proxies
        # requests to goosed using this internal token.
        _INTERNAL_GOOSE_TOKEN = secrets.token_urlsafe(32)
        cmd = ["goosed", "agent"]
        env = os.environ.copy()
        env["GOOSE_TLS"] = "false"
        env["GOOSE_HOST"] = "127.0.0.1"
        env["GOOSE_PORT"] = str(GOOSE_WEB_PORT)
        env["GOOSE_SERVER__SECRET_KEY"] = _INTERNAL_GOOSE_TOKEN
        env["GOOSE_DISABLE_KEYRING"] = "1"

        # ensure provider config is in env (belt-and-suspenders with apply_config)
        setup = load_setup()
        if setup:
            pt = setup.get("provider_type", "")
            if pt and "GOOSE_PROVIDER" not in env:
                env["GOOSE_PROVIDER"] = pt
            md = setup.get("model", "")
            if md and "GOOSE_MODEL" not in env:
                env["GOOSE_MODEL"] = md

        print(f"[gateway] starting goosed agent on 127.0.0.1:{GOOSE_WEB_PORT}")
        print(f"[gateway] cmd: goosed agent (TLS=false, port={GOOSE_WEB_PORT})")
        print(f"[gateway] env: GOOSE_PROVIDER={env.get('GOOSE_PROVIDER', 'NOT SET')} "
              f"GOOSE_MODEL={env.get('GOOSE_MODEL', 'NOT SET')} "
              f"GOOSE_MODE={env.get('GOOSE_MODE', 'NOT SET')}")
        goosed_process = subprocess.Popen(cmd, stdout=sys.stdout, stderr=subprocess.PIPE, env=env)
        _write_pid("goosed", goosed_process.pid)

        # Start daemon thread to read stderr line-by-line, forward to sys.stderr,
        # and buffer lines for the startup status API.
        threading.Thread(target=_stderr_reader, args=(goosed_process,), daemon=True).start()

        # wait for it to listen
        for i in range(30):
            time.sleep(1)
            # check if process exited prematurely
            if goosed_process.poll() is not None:
                exit_code = goosed_process.returncode
                _set_startup_state("error", f"goosed exited with code {exit_code}", error=_get_recent_stderr(20))
                print(f"[gateway] goosed exited during startup with code {exit_code}")
                return False
            try:
                conn = _goosed_conn(timeout=2)
                conn.request("GET", "/status")
                resp = conn.getresponse()
                if resp.status == 200:
                    _set_startup_state("ready", "goosed is running")
                    print("[gateway] goosed is ready")
                    # configure provider via API (goosed ignores env vars for provider)
                    _configure_goosed_provider()
                    return True
                conn.close()
            except Exception:
                pass

        _set_startup_state("error", "goosed did not become ready in 30s", error=_get_recent_stderr(20))
        print("[gateway] WARN: goosed did not become ready in 30s")
        return False


def stop_goosed():
    global goosed_process
    with goose_lock:
        if goosed_process and goosed_process.poll() is None:
            goosed_process.terminate()
            try:
                goosed_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                goosed_process.kill()
        goosed_process = None
    _remove_pid("goosed")


def goose_health_monitor():
    """Monitor goosed subprocess and auto-restart on crash with backoff."""
    backoff = 5  # initial backoff seconds
    max_backoff = 120
    consecutive_failures = 0

    while True:
        time.sleep(15)  # check every 15 seconds
        if not is_configured():
            continue

        with goose_lock:
            proc = goosed_process
        if proc is None:
            continue

        if proc.poll() is not None:
            # process has exited
            exit_code = proc.returncode
            consecutive_failures += 1
            wait_time = min(backoff * (2 ** (consecutive_failures - 1)), max_backoff)
            _set_startup_state("starting", f"Restarting goosed (attempt #{consecutive_failures})...")
            print(f"[health] goosed exited (code {exit_code}). "
                  f"Restart #{consecutive_failures} in {wait_time}s...")
            _remove_pid("goosed")
            time.sleep(wait_time)
            try:
                start_goosed()
                print(f"[health] goosed restarted after failure #{consecutive_failures}")
            except Exception as e:
                print(f"[health] restart failed: {e}")
        else:
            # process is running, reset backoff on sustained health
            if consecutive_failures > 0:
                # verify it's actually responding
                try:
                    conn = _goosed_conn(timeout=3)
                    conn.request("GET", "/status")
                    resp = conn.getresponse()
                    conn.close()
                    if resp.status == 200:
                        consecutive_failures = 0
                except Exception:
                    pass  # will catch on next cycle if it dies


# ── input sanitization ───────────────────────────────────────────────────────

def _sanitize_string(value, max_length=2000):
    """Sanitize a string value: strip whitespace, truncate, remove control characters."""
    if not isinstance(value, str):
        return value
    # strip leading/trailing whitespace
    value = value.strip()
    # truncate to max length
    value = value[:max_length]
    # remove control characters (except newline \n=0x0a and tab \t=0x09)
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)
    return value


# ── HTTP handler ────────────────────────────────────────────────────────────

class GatewayHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Structured request logging with timestamp and format string."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if args:
            print(f"[gateway] {timestamp} {format % args}")
        else:
            print(f"[gateway] {timestamp} {format}")

    def log_request(self, code="-", size="-"):
        """Override to log request with duration."""
        duration_ms = int((time.time() - getattr(self, "_request_start", time.time())) * 1000)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"[gateway] {timestamp} {self.command} {self.path} {code} {duration_ms}ms")

    def _check_rate_limit(self, limiter):
        """Return True if request is allowed; send 429 and return False if over limit."""
        ip = self.client_address[0]
        if not limiter.is_allowed(ip):
            self.send_json(429, {"error": "Too many requests. Try again later.", "code": "RATE_LIMITED"})
            return False
        return True

    # ── routing ──

    def do_GET(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        # rate-limit all /api/* requests; skip static /setup pages and proxy
        if path.startswith("/api/") and not self._check_rate_limit(api_limiter):
            return
        if path == "/api/health":
            self.handle_health()
        elif path == "/api/health/ready":
            self.handle_health_ready()
        elif path == "/api/debug/relay":
            # temporary: test non-streaming relay directly
            try:
                sid = _create_goose_session()
                if not sid:
                    self.send_json(500, {"error": "could not create session"})
                    return
                text, err, _media = _do_rest_relay("say hi in 5 words", sid)
                self.send_json(200, {"session_id": sid, "response": text, "error": err})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif path == "/api/debug/config":
            # temporary debug endpoint to check goose config and claude CLI
            try:
                config_path = os.path.join(CONFIG_DIR, "config.yaml")
                with open(config_path) as f:
                    content = f.read()
                # mask any sensitive values
                import re
                content = re.sub(r'(auth.token|api.key|token|secret):\s*\S+', r'\1: ****', content, flags=re.IGNORECASE)
                # check claude CLI
                claude_info = {}
                try:
                    import subprocess
                    r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
                    claude_info["version"] = r.stdout.strip() or r.stderr.strip()
                    claude_info["returncode"] = r.returncode
                    # test actual generation (lightweight)
                    r2 = subprocess.run(
                        ["claude", "-p", "say hi in 3 words", "--output-format", "text", "--dangerously-skip-permissions"],
                        capture_output=True, text=True, timeout=30,
                        env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")},
                    )
                    claude_info["test_stdout"] = r2.stdout.strip()[:200]
                    claude_info["test_stderr"] = r2.stderr.strip()[:500]
                    claude_info["test_returncode"] = r2.returncode
                except Exception as ce:
                    claude_info["error"] = str(ce)
                # check goose stderr buffer
                recent_stderr = _get_recent_stderr(10)
                self.send_json(200, {
                    "config": content,
                    "env_oauth_set": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                    "goose_mode": os.environ.get("GOOSE_MODE", "NOT SET"),
                    "claude_cli": claude_info,
                    "goose_stderr": recent_stderr,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif path == "/api/setup/status":
            self.handle_startup_status()
        elif path == "/api/version":
            self.handle_version()
        elif path.rstrip("/") == "/setup" or path.startswith("/setup/"):
            self.handle_setup_page()
        elif path == "/api/setup/config":
            self.handle_get_config()
        elif path == "/api/notify/status":
            self.handle_notify_status()
        elif path == "/api/telegram/status":
            self.handle_telegram_status()
        elif path == "/api/jobs":
            self.handle_list_jobs()
        elif path == "/api/schedule/upcoming":
            self.handle_schedule_upcoming()
        elif path == "/api/schedule/context":
            self.handle_schedule_context()
        elif path == "/api/watchers":
            self.handle_list_watchers()
        elif path == "/api/channels":
            self.handle_list_channels()
        elif path.rstrip("/") == "/login":
            self.handle_login_page()
        elif path.rstrip("/") == "/admin":
            # backward compat: redirect /admin to /
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
        elif path == "/" or path == "":
            if not is_configured():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.end_headers()
            elif not check_auth(self):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            else:
                self.handle_admin_page()
        elif not is_configured():
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.end_headers()
        else:
            self.proxy_to_goose()

    def do_POST(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/setup/save":
            self.handle_save()
        elif path == "/api/setup/validate":
            self.handle_validate()
        elif path == "/api/setup/models":
            self.handle_fetch_models()
        elif path == "/api/setup/models/add":
            self.handle_add_model()
        elif path == "/api/setup/models/remove":
            self.handle_remove_model()
        elif path == "/api/setup/models/activate":
            self.handle_activate_model()
        elif path == "/api/setup/models/route":
            self.handle_set_routes()
        elif path == "/api/notify":
            self.handle_notify()
        elif path == "/api/telegram/pair":
            self.handle_telegram_pair()
        elif path == "/api/auth/login":
            self.handle_auth_login()
        elif path == "/api/auth/recover":
            self.handle_auth_recover()
        elif path == "/api/jobs":
            self.handle_create_job()
        elif path.startswith("/api/jobs/") and path.endswith("/run"):
            job_id = path[len("/api/jobs/"):-len("/run")]
            if job_id:
                self.handle_run_job(job_id)
            else:
                self.proxy_to_goose()
        elif path == "/api/channels/reload":
            self.handle_reload_channels()
        elif path == "/api/setup/channels/verbosity":
            self.handle_set_verbosity()
        elif path == "/api/setup/agent-config":
            self.handle_agent_config()
        elif path == "/api/bots":
            self.handle_add_bot()
        elif path == "/api/watchers/batch":
            self.handle_watchers_batch_create()
        elif path == "/api/watchers":
            self.handle_create_watcher()
        elif path.startswith("/api/webhooks/"):
            webhook_name = path[len("/api/webhooks/"):]
            if webhook_name:
                self.handle_webhook_incoming(webhook_name)
            else:
                self.proxy_to_goose()
        else:
            self.proxy_to_goose()

    def do_PUT(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            if job_id:
                self.handle_update_job(job_id)
                return
        elif path.startswith("/api/watchers/"):
            watcher_id = path[len("/api/watchers/"):]
            if watcher_id:
                self.handle_update_watcher(watcher_id)
                return
        self.proxy_to_goose()

    def do_DELETE(self):
        self._request_start = time.time()
        path = urllib.parse.urlparse(self.path).path
        # DELETE /api/jobs/<id>
        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            if job_id:
                self.handle_delete_job(job_id)
                return
        elif path == "/api/watchers/batch":
            self.handle_watchers_batch_delete()
            return
        elif path.startswith("/api/watchers/"):
            watcher_id = path[len("/api/watchers/"):]
            if watcher_id:
                self.handle_delete_watcher(watcher_id)
                return
        elif path.startswith("/api/bots/"):
            bot_name = path[len("/api/bots/"):]
            if bot_name:
                self.handle_remove_bot(bot_name)
                return
        self.proxy_to_goose()

    def do_OPTIONS(self):
        # Handle CORS preflight for /api/* paths without proxying to goose.
        # Only echo Origin back if it is same-host; otherwise omit CORS headers
        # so the browser blocks the cross-origin request.
        if self.path.startswith("/api/"):
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            self.send_response(200)
            if origin and host and (
                origin == f"http://{host}" or origin == f"https://{host}"
            ):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.proxy_to_goose()

    def do_PATCH(self):
        self.proxy_to_goose()

    # ── health endpoints ──

    def _ping_goosed(self):
        """Try to ping goosed subprocess. Returns 'healthy', 'unhealthy (HTTP N)', or 'unreachable'."""
        try:
            conn = _goosed_conn(timeout=2)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            conn.close()
            return "healthy" if resp.status == 200 else f"unhealthy (HTTP {resp.status})"
        except Exception:
            return "unreachable"

    def handle_health(self):
        """GET /api/health — deep health check: liveness + goosed subprocess status."""
        status = {"service": "gooseclaw", "configured": is_configured()}

        if goosed_process and goosed_process.poll() is None:
            # process is alive — probe it
            status["goosed"] = self._ping_goosed()
        else:
            status["goosed"] = "not running" if is_configured() else "not started (unconfigured)"

        if not is_configured():
            status["status"] = "setup_required"
        elif status.get("goosed") == "healthy":
            status["status"] = "ok"
        else:
            status["status"] = "degraded"

        # 200 for ok/setup_required (healthy enough to serve traffic), 503 for degraded
        code = 200 if status["status"] in ("ok", "setup_required") else 503
        self.send_json(code, status)

    def handle_health_ready(self):
        """GET /api/health/ready — readiness probe: 200 only when goosed is up and responding."""
        if goosed_process and goosed_process.poll() is None:
            result = self._ping_goosed()
            if result == "healthy":
                self.send_json(200, {"ready": True, "goosed": "healthy"})
                return
            self.send_json(503, {"ready": False, "goosed": result})
        else:
            reason = "not started (unconfigured)" if not is_configured() else "not running"
            self.send_json(503, {"ready": False, "goosed": reason})

    def handle_version(self):
        """GET /api/version — return the deployed version from VERSION file."""
        version = "unknown"
        version_file = os.path.join(APP_DIR, "VERSION")
        if os.path.exists(version_file):
            try:
                with open(version_file) as f:
                    version = f.read().strip()
            except Exception:
                pass
        self.send_json(200, {"version": version, "service": "gooseclaw"})

    # ── startup status endpoint ──

    def handle_startup_status(self):
        """GET /api/setup/status — goosed startup state (no auth required)."""
        with _startup_state_lock:
            state_copy = dict(goosed_startup_state)
        self.send_json(200, state_copy)

    # ── setup endpoints ──

    def handle_setup_page(self):
        # first boot (no setup.json) = open. after that = require auth.
        # allow unauthenticated access to recovery page
        query = urllib.parse.urlparse(self.path).query
        is_recovery = "recover" in urllib.parse.parse_qs(query)
        if load_setup() and not is_recovery and not check_auth(self):
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        try:
            with open(SETUP_HTML, "rb") as f:
                content = f.read()
            mtime = os.path.getmtime(SETUP_HTML)
            etag = f'"{int(mtime)}"'
            # conditional request support
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            # security headers for HTML response
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            # override X-Frame-Options already covered by CSP frame-ancestors
            # Content-Security-Policy for setup.html
            # unsafe-inline for script-src is required because setup.html has inline JS
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
            self.send_header("Content-Security-Policy", csp)
            if os.environ.get("RAILWAY_ENVIRONMENT"):
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            # persist auth via session cookie after successful Basic Auth
            self._inject_session_cookie()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "Setup page not found")

    def handle_admin_page(self):
        """Serve the admin dashboard. Requires auth."""
        if not self._check_local_or_auth():
            return
        try:
            with open(ADMIN_HTML, "rb") as f:
                content = f.read()
            mtime = os.path.getmtime(ADMIN_HTML)
            etag = f'"{int(mtime)}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            for header, value in SECURITY_HEADERS.items():
                self.send_header(header, value)
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
            self.send_header("Content-Security-Policy", csp)
            if os.environ.get("RAILWAY_ENVIRONMENT"):
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self._inject_session_cookie()
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "Admin page not found")

    def handle_get_config(self):
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.end_headers()
            return
        safe = get_safe_setup()
        if safe:
            migrate_config_models(safe)

            # ── Frontend companion fields ────────────────────────────────────
            # Add boolean _set hints so the UI can show "key already set"
            # placeholders without seeing the actual value.
            _UI_SECRET_FIELDS = (
                "api_key",
                "claude_setup_token",
                "custom_key",
                "web_auth_token",
                "web_auth_token_hash",
            )
            for key in _UI_SECRET_FIELDS:
                val = safe.get(key, "")
                safe[f"{key}_set"] = bool(val)
                if not val:
                    safe.pop(key, None)

            # telegram_bot_token: only expose whether it is set, never the value
            tbt = safe.pop("telegram_bot_token", "")
            safe["telegram_bot_token_set"] = bool(tbt)

            # ── saved_keys_set indicators ────────────────────────────────────
            if "saved_keys" in safe and isinstance(safe["saved_keys"], dict):
                set_indicators = {}
                for provider_id, val in safe["saved_keys"].items():
                    if isinstance(val, str) and val:
                        set_indicators[provider_id] = True
                    elif isinstance(val, dict) and val:
                        set_indicators[provider_id] = True
                    else:
                        set_indicators[provider_id] = False
                safe["saved_keys_set"] = set_indicators

            self.send_json(200, {"configured": True, "config": safe})
        else:
            self.send_json(200, {"configured": False})

    def handle_save(self):
        if not self._check_rate_limit(auth_limiter):
            return
        if load_setup() and not check_auth(self):
            self.send_response(401)
            self.end_headers()
            return
        body = self._read_body()
        try:
            config = json.loads(body)

            # sanitize all string fields before validation
            for key, val in list(config.items()):
                if isinstance(val, str):
                    config[key] = _sanitize_string(val)

            # validate config schema before accepting
            valid, errors = validate_setup_config(config)
            if not valid:
                self.send_json(400, {"success": False, "errors": errors})
                return

            # password handling: require on first setup, optional on reconfigure
            plaintext_password = config.get("web_auth_token", "")
            existing_setup = load_setup()
            has_existing_hash = bool(existing_setup and existing_setup.get("web_auth_token_hash"))
            if not plaintext_password and not has_existing_hash:
                # first setup with no password = error
                self.send_json(400, {"success": False, "errors": ["Password is required"]})
                return

            # hash the password before storage -- plaintext never hits disk
            if plaintext_password:
                config["web_auth_token_hash"] = hash_token(plaintext_password)
            elif has_existing_hash:
                # keep existing hash during reconfigure
                config["web_auth_token_hash"] = existing_setup["web_auth_token_hash"]
            # remove plaintext from config dict before saving
            config.pop("web_auth_token", None)

            save_setup(config)
            apply_config(config)

            # restart goosed in background
            def _restart():
                time.sleep(1)
                start_goosed()
                start_session_watcher()
                start_job_engine()
                start_cron_scheduler()
                start_memory_writer()
                _load_watchers()
                start_watcher_engine()
            threading.Thread(target=_restart, daemon=True).start()

            resp = {"success": True, "message": "saved. agent is restarting..."}
            # include pairing code if a telegram bot is configured
            tg_token = config.get("telegram_bot_token", "")
            if tg_token:
                default_bot = _bot_manager.get_bot("default")
                if default_bot and default_bot.pair_code:
                    resp["pairing_code"] = default_bot.pair_code
            self.send_json(200, resp)

        except json.JSONDecodeError:
            self.send_json(400, {"success": False, "error": "invalid JSON", "code": "INVALID_CONFIG"})
        except Exception as e:
            print(f"[gateway] ERROR (handle_save): {e}", file=sys.stderr)
            self.send_json(500, {"success": False, "error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def handle_validate(self):
        if not self._check_rate_limit(auth_limiter):
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            provider = _sanitize_string(data.get("provider_type") or data.get("provider", ""))
            credentials = data.get("credentials", data)
            # sanitize credential string values
            if isinstance(credentials, dict):
                credentials = {k: _sanitize_string(v) for k, v in credentials.items()}
            result = dispatch_validation(provider, credentials)
            self.send_json(200, result)
        except Exception as e:
            print(f"[gateway] ERROR (handle_validate): {e}", file=sys.stderr)
            self.send_json(500, {"valid": False, "error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def handle_fetch_models(self):
        """POST /api/setup/models — fetch available models for a provider."""
        if not self._check_rate_limit(auth_limiter):
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            provider = _sanitize_string(data.get("provider", ""))
            credentials = data.get("credentials", {})
            if isinstance(credentials, dict):
                credentials = {k: _sanitize_string(v) for k, v in credentials.items()}
            result = fetch_provider_models(provider, credentials)
            self.send_json(200, result)
        except Exception as e:
            print(f"[gateway] ERROR (handle_fetch_models): {e}", file=sys.stderr)
            self.send_json(500, {"models": [], "fallback": True, "error": "Internal server error."})

    # ── model management endpoints ──

    def _save_and_apply(self, config):
        """Shared helper: sync legacy fields, save, apply, restart goose."""
        _sync_active_model_to_config(config)
        save_setup(config)
        apply_config(config)
        def _restart():
            time.sleep(1)
            start_goosed()
            start_session_watcher()
            start_job_engine()
            start_cron_scheduler()
            start_memory_writer()
            _load_watchers()
            start_watcher_engine()
        threading.Thread(target=_restart, daemon=True).start()
        return True

    def handle_add_model(self):
        """POST /api/setup/models/add — add a model to the models array."""
        if not self._check_rate_limit(auth_limiter):
            return
        if not check_auth(self):
            self.send_response(401); self.end_headers(); return
        body = self._read_body()
        try:
            data = json.loads(body)
            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "not configured yet"}); return

            migrate_config_models(setup)
            provider = _sanitize_string(data.get("provider", ""))
            model = _sanitize_string(data.get("model", ""))
            if not provider or not model:
                self.send_json(400, {"error": "provider and model are required"}); return
            if provider not in env_map:
                self.send_json(400, {"error": f"unknown provider: {provider!r}"}); return

            # generate stable id
            mid = f"{provider}_{model}".replace("/", "_").replace(".", "_")[:64]
            # check duplicate
            for m in setup.get("models", []):
                if m.get("id") == mid:
                    self.send_json(409, {"error": "model already exists"}); return

            # save credentials if provided
            new_key = _sanitize_string(data.get("api_key", ""))
            if new_key:
                saved = setup.get("saved_keys", {})
                saved[provider] = new_key
                setup["saved_keys"] = saved

            # save custom endpoint fields
            if provider == "custom":
                base_url = _sanitize_string(data.get("base_url", ""))
                engine = _sanitize_string(data.get("engine", "openai"))
                if not base_url:
                    self.send_json(400, {"error": "base_url is required for custom endpoint"}); return
                if engine not in ("openai", "anthropic", "ollama"):
                    engine = "openai"
                setup["custom_url"] = base_url
                setup["custom_engine"] = engine
                setup["custom_model"] = model
                if new_key:
                    setup["custom_key"] = new_key

            is_first = len(setup.get("models", [])) == 0
            new_model = {
                "id": mid,
                "provider": provider,
                "model": model,
                "is_default": is_first,
            }
            setup.setdefault("models", []).append(new_model)
            setup.setdefault("channel_routes", {})

            self._save_and_apply(setup)
            self.send_json(200, {"success": True, "model": new_model})
        except Exception as e:
            print(f"[gateway] ERROR (handle_add_model): {e}", file=sys.stderr)
            self.send_json(500, {"error": "Internal server error."})

    def handle_remove_model(self):
        """POST /api/setup/models/remove — remove a model by id."""
        if not self._check_rate_limit(auth_limiter):
            return
        if not check_auth(self):
            self.send_response(401); self.end_headers(); return
        body = self._read_body()
        try:
            data = json.loads(body)
            mid = _sanitize_string(data.get("id", ""))
            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "not configured yet"}); return

            migrate_config_models(setup)
            models = setup.get("models", [])
            target = None
            for m in models:
                if m.get("id") == mid:
                    target = m
                    break
            if not target:
                self.send_json(404, {"error": "model not found"}); return
            if len(models) <= 1:
                self.send_json(400, {"error": "cannot remove last model"}); return

            was_default = target.get("is_default")
            models.remove(target)

            # if removed the default, promote the first remaining model
            if was_default and models:
                models[0]["is_default"] = True

            # clean up channel_routes pointing to the removed model
            routes = setup.get("channel_routes", {})
            for ch, route_mid in list(routes.items()):
                if route_mid == mid:
                    del routes[ch]

            self._save_and_apply(setup)
            self.send_json(200, {"success": True, "models": models})
        except Exception as e:
            print(f"[gateway] ERROR (handle_remove_model): {e}", file=sys.stderr)
            self.send_json(500, {"error": "Internal server error."})

    def handle_activate_model(self):
        """POST /api/setup/models/activate — set a model as default."""
        if not self._check_rate_limit(auth_limiter):
            return
        if not check_auth(self):
            self.send_response(401); self.end_headers(); return
        body = self._read_body()
        try:
            data = json.loads(body)
            mid = _sanitize_string(data.get("id", ""))
            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "not configured yet"}); return

            migrate_config_models(setup)
            found = False
            for m in setup.get("models", []):
                if m.get("id") == mid:
                    m["is_default"] = True
                    found = True
                else:
                    m["is_default"] = False
            if not found:
                self.send_json(404, {"error": "model not found"}); return

            self._save_and_apply(setup)
            self.send_json(200, {"success": True})
        except Exception as e:
            print(f"[gateway] ERROR (handle_activate_model): {e}", file=sys.stderr)
            self.send_json(500, {"error": "Internal server error."})

    def handle_set_routes(self):
        """POST /api/setup/models/route — set channel routing."""
        if not self._check_rate_limit(auth_limiter):
            return
        if not check_auth(self):
            self.send_response(401); self.end_headers(); return
        body = self._read_body()
        try:
            data = json.loads(body)
            routes = data.get("channel_routes", {})
            if not isinstance(routes, dict):
                self.send_json(400, {"error": "channel_routes must be an object"}); return

            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "not configured yet"}); return

            migrate_config_models(setup)
            model_ids = {m.get("id") for m in setup.get("models", [])}
            valid_channels = _get_valid_channels()
            clean_routes = {}
            for ch, mid in routes.items():
                ch = _sanitize_string(ch)
                if ch not in valid_channels:
                    continue
                mid = _sanitize_string(mid) if mid else None
                # allow "custom:<model>" values as well as model IDs
                if mid and not mid.startswith("custom:") and mid not in model_ids:
                    self.send_json(400, {"error": f"unknown model id: {mid!r}"}); return
                if mid:
                    clean_routes[ch] = mid

            setup["channel_routes"] = clean_routes
            save_setup(setup)

            # propagate env vars for all routed providers so goose can use them
            saved_keys = setup.get("saved_keys", {})
            for mid in clean_routes.values():
                model_cfg = next((m for m in setup.get("models", []) if m.get("id") == mid), None)
                if model_cfg:
                    prov = model_cfg.get("provider", "")
                    key_val = saved_keys.get(prov, "")
                    if prov in env_map and isinstance(key_val, str) and key_val and key_val != "********":
                        env_vars = env_map.get(prov, [])
                        if env_vars:
                            os.environ[env_vars[0]] = key_val

            self.send_json(200, {"success": True, "channel_routes": clean_routes})
        except Exception as e:
            print(f"[gateway] ERROR (handle_set_routes): {e}", file=sys.stderr)
            self.send_json(500, {"error": "Internal server error."})

    def handle_set_verbosity(self):
        """POST /api/setup/channels/verbosity — set per-channel verbosity levels."""
        if not self._check_rate_limit(auth_limiter):
            return
        if not check_auth(self):
            self.send_response(401); self.end_headers(); return
        body = self._read_body()
        try:
            data = json.loads(body)
            verbosity = data.get("channel_verbosity", {})
            if not isinstance(verbosity, dict):
                self.send_json(400, {"error": "channel_verbosity must be an object"}); return

            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "not configured yet"}); return

            valid_channels = _get_valid_channels()
            valid_levels = ("quiet", "balanced", "verbose")
            clean = {}
            for ch, level in verbosity.items():
                ch = _sanitize_string(ch)
                level = _sanitize_string(level)
                if ch not in valid_channels:
                    continue
                if level not in valid_levels:
                    self.send_json(400, {"error": f"invalid verbosity level: {level!r}"}); return
                clean[ch] = level

            setup["channel_verbosity"] = clean
            save_setup(setup)
            self.send_json(200, {"success": True, "channel_verbosity": clean})
        except Exception as e:
            print(f"[gateway] ERROR (handle_set_verbosity): {e}", file=sys.stderr)
            self.send_json(500, {"error": "Internal server error."})

    def handle_agent_config(self):
        """POST /api/setup/agent-config — set memory idle minutes and other agent settings."""
        if not self._check_rate_limit(auth_limiter):
            return
        if not check_auth(self):
            self.send_response(401); self.end_headers(); return
        body = self._read_body()
        try:
            data = json.loads(body)
            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "not configured yet"}); return

            changed = {}
            if "memory_idle_minutes" in data:
                val = int(data["memory_idle_minutes"])
                if val < 1 or val > 120:
                    self.send_json(400, {"error": "memory_idle_minutes must be 1-120"}); return
                setup["memory_idle_minutes"] = val
                changed["memory_idle_minutes"] = val
            if "memory_writer_enabled" in data:
                val = bool(data["memory_writer_enabled"])
                setup["memory_writer_enabled"] = val
                changed["memory_writer_enabled"] = val

            save_setup(setup)
            self.send_json(200, {"success": True, **changed})
        except (ValueError, TypeError) as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:
            print(f"[gateway] ERROR (handle_agent_config): {e}", file=sys.stderr)
            self.send_json(500, {"error": "Internal server error."})

    # ── notify endpoints ──

    def handle_notify(self):
        """POST /api/notify — send a message to all paired telegram users."""
        if not self._check_rate_limit(notify_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        # allow unauthenticated calls from localhost (e.g. notify.sh, cron jobs)
        client_ip = self.client_address[0] if self.client_address else ""
        is_local = client_ip in ("127.0.0.1", "::1", "localhost")
        if not is_local and not check_auth(self):
            self.send_json(401, {"error": "Authentication required"})
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            text = _sanitize_string(data.get("text", ""), max_length=4000)
            if not text:
                self.send_json(400, {"sent": False, "error": "text field is required"})
                return
            channel = data.get("channel")
            if channel:
                channel = _sanitize_string(channel, max_length=100)
            result = notify_all(text, channel=channel)
            status_code = 200 if result["sent"] else 502
            self.send_json(status_code, result)
        except json.JSONDecodeError:
            self.send_json(400, {"sent": False, "error": "invalid JSON", "code": "INVALID_CONFIG"})
        except Exception as e:
            print(f"[gateway] ERROR (handle_notify): {e}", file=sys.stderr)
            self.send_json(500, {"sent": False, "error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def handle_notify_status(self):
        """GET /api/notify/status — check if notification delivery is available."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        token = get_bot_token()
        chat_ids = get_paired_chat_ids()
        self.send_json(200, {
            "available": bool(token and chat_ids),
            "bot_configured": bool(token),
            "paired_users": len(chat_ids),
        })

    # ── telegram pairing endpoints ──

    def handle_telegram_status(self):
        """GET /api/telegram/status — telegram gateway status, paired users, pairing code."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        bots = _bot_manager.get_all()
        # backward compat: use default bot or first bot
        default = _bot_manager.get_bot("default") or (list(bots.values())[0] if bots else None)
        bot_list = []
        for name, bot in bots.items():
            paired = get_paired_chat_ids(platform=bot.channel_key)
            bot_list.append({
                "name": name,
                "running": bot.running,
                "channel_key": bot.channel_key,
                "paired_users": len(paired),
                "paired_chat_ids": paired,
                "pairing_code": bot.pair_code,
            })
        # backward-compat top-level fields (admin.html reads these)
        token = get_bot_token()
        default_paired = get_paired_chat_ids() if default else []
        self.send_json(200, {
            "running": _bot_manager.any_running,
            "bot_configured": bool(token),
            "paired_users": len(default_paired),
            "paired_chat_ids": default_paired,
            "pairing_code": default.pair_code if default else None,
            "bots": bot_list,
        })

    def handle_telegram_pair(self):
        """POST /api/telegram/pair — generate a new pairing code."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not check_auth(self):
            self.send_json(401, {"error": "Authentication required"})
            return

        # determine which bot to pair
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        bot_name = params.get("bot", ["default"])[0]
        bot = _bot_manager.get_bot(bot_name)
        if not bot:
            # try starting default if no bots running
            if bot_name == "default":
                token = get_bot_token()
                if token:
                    start_telegram_gateway(token)
                    time.sleep(2)
                    bot = _bot_manager.get_bot("default")
            if not bot:
                self.send_json(400, {"error": f"bot '{bot_name}' not found", "code": None})
                return
        code = bot.generate_pair_code()
        self.send_json(200, {"code": code, "bot": bot_name, "message": f"send this code to {bot_name} bot"})

    # ── bot lifecycle endpoints ──

    def handle_add_bot(self):
        """POST /api/bots -- hot-add a new Telegram bot."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not self._check_local_or_auth():
            return
        body = self._read_body()
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_json(400, {"error": "invalid JSON"})
            return

        name = data.get("name", "").strip() if isinstance(data.get("name"), str) else ""
        token = data.get("token", "").strip() if isinstance(data.get("token"), str) else ""

        if not name:
            self.send_json(400, {"error": "name is required"})
            return
        if not token:
            self.send_json(400, {"error": "token is required"})
            return
        if ":" not in token:
            self.send_json(400, {"error": "token must be in format digits:alphanumeric"})
            return

        # check duplicate
        if _bot_manager.get_bot(name):
            self.send_json(409, {"error": "bot already exists"})
            return

        channel_key = "telegram" if name == "default" else f"telegram:{name}"
        try:
            bot = _bot_manager.add_bot(name, token, channel_key)
            bot.start()
        except ValueError as e:
            self.send_json(409, {"error": str(e)})
            return
        except Exception as e:
            self.send_json(500, {"error": f"failed to start bot: {e}"})
            return

        # persist to setup.json
        try:
            config = load_setup() or {}
            if not isinstance(config.get("bots"), list):
                config["bots"] = []
            bot_entry = {"name": name, "token": token}
            # include optional provider/model if provided
            if data.get("provider"):
                bot_entry["provider"] = data["provider"]
            if data.get("model"):
                bot_entry["model"] = data["model"]
            config["bots"].append(bot_entry)
            save_setup(config)
        except Exception as e:
            print(f"[bot-mgr] warn: could not persist bot '{name}' to setup.json: {e}")

        self.send_json(201, {"name": name, "channel_key": bot.channel_key, "status": "running"})

    def handle_remove_bot(self, name):
        """DELETE /api/bots/<name> -- hot-remove a Telegram bot."""
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not self._check_local_or_auth():
            return

        bot = _bot_manager.get_bot(name)
        if not bot:
            self.send_json(404, {"error": "bot not found"})
            return

        if name == "default":
            print("[bot-mgr] warn: removing default bot")

        _bot_manager.remove_bot(name)

        # persist removal to setup.json
        try:
            config = load_setup() or {}
            if isinstance(config.get("bots"), list):
                config["bots"] = [b for b in config["bots"] if b.get("name") != name]
            else:
                # legacy single-token config
                config["telegram_bot_token"] = ""
            save_setup(config)
        except Exception as e:
            print(f"[bot-mgr] warn: could not persist removal of '{name}' from setup.json: {e}")

        self.send_json(200, {"removed": name})

    # ── job endpoints ──

    def _check_local_or_auth(self):
        """Allow localhost without auth, require auth for remote. Returns True if allowed."""
        client_ip = self.client_address[0] if self.client_address else ""
        is_local = client_ip in ("127.0.0.1", "::1", "localhost")
        if is_local:
            return True
        if not check_auth(self):
            self.send_json(401, {"error": "Authentication required"})
            return False
        return True

    # ── watcher endpoints ──

    def handle_create_watcher(self):
        """POST /api/watchers -- create a new watcher."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        body = self._read_body()
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, Exception):
            self.send_json(400, {"error": "invalid JSON"})
            return
        watcher, err = create_watcher(data)
        if err:
            self.send_json(400, {"error": err})
        else:
            self.send_json(201, watcher)

    def handle_list_watchers(self):
        """GET /api/watchers -- list all watchers."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        watchers = list_watchers()
        self.send_json(200, {"watchers": watchers, "count": len(watchers)})

    def handle_delete_watcher(self, watcher_id):
        """DELETE /api/watchers/<id> -- delete a watcher."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        if delete_watcher(watcher_id):
            self.send_json(200, {"deleted": True, "id": watcher_id})
        else:
            self.send_json(404, {"error": "watcher not found"})

    def handle_update_watcher(self, watcher_id):
        """PUT /api/watchers/<id> -- update watcher fields."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        try:
            body = self._read_body()
            data = json.loads(body)
        except (json.JSONDecodeError, Exception):
            self.send_json(400, {"error": "invalid JSON"})
            return
        updated, err = update_watcher(watcher_id, data)
        if err:
            self.send_json(404, {"error": err})
        else:
            self.send_json(200, updated)

    def handle_watchers_batch_create(self):
        """POST /api/watchers/batch -- create multiple watchers in one call."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        body = self._read_body()
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, Exception):
            self.send_json(400, {"error": "invalid JSON"})
            return

        items = data.get("watchers")
        if not isinstance(items, list):
            self.send_json(400, {"error": "'watchers' must be a list"})
            return
        if len(items) == 0:
            self.send_json(400, {"error": "'watchers' list must not be empty"})
            return

        results = []
        created = 0
        errors = 0
        for spec in items:
            name = spec.get("name", spec.get("id", ""))
            watcher, err = create_watcher(spec, _save=False)
            if err:
                results.append({"name": name, "status": "error", "error": err})
                errors += 1
            else:
                results.append({
                    "name": watcher["name"],
                    "status": "created",
                    "id": watcher["id"],
                    "watcher": watcher,
                })
                created += 1

        _save_watchers()
        self.send_json(207, {"results": results, "created": created, "errors": errors})

    def handle_watchers_batch_delete(self):
        """DELETE /api/watchers/batch -- delete multiple watchers in one call."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        body = self._read_body()
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, Exception):
            self.send_json(400, {"error": "invalid JSON"})
            return

        ids = data.get("ids")
        if not isinstance(ids, list):
            self.send_json(400, {"error": "'ids' must be a list"})
            return
        if len(ids) == 0:
            self.send_json(400, {"error": "'ids' list must not be empty"})
            return

        results = []
        deleted = 0
        errors = 0
        for watcher_id in ids:
            with _watchers_lock:
                before = len(_watchers)
                _watchers[:] = [w for w in _watchers if w["id"] != watcher_id]
                found = len(_watchers) < before
            if found:
                results.append({"id": watcher_id, "status": "deleted"})
                deleted += 1
                print(f"[watchers] deleted: {watcher_id}")
            else:
                results.append({"id": watcher_id, "status": "error", "error": "watcher not found"})
                errors += 1

        _save_watchers()
        self.send_json(207, {"results": results, "deleted": deleted, "errors": errors})

    def handle_webhook_incoming(self, webhook_name):
        """POST /api/webhooks/<name> -- receive external webhook."""
        if not self._check_rate_limit(api_limiter):
            return
        body = self._read_body()
        headers = dict(self.headers) if self.headers else {}
        count = _handle_webhook_incoming(webhook_name, body, headers)
        if count > 0:
            self.send_json(200, {"accepted": True, "watchers": count})
        else:
            self.send_json(404, {"error": f"no watchers for webhook '{webhook_name}'"})

    def handle_create_job(self):
        """POST /api/jobs — create a new job.

        JSON body:
          type: "reminder"|"script" (default: "script")
          text: str — reminder text (required for type=reminder)
          command: str — shell command (required for type=script)
          name: str — human-readable label (optional, defaults to text or id)
          cron: str — 5-field cron expression (optional)
          delay_seconds: int — fire after N seconds (optional)
          fire_at: float — unix timestamp to fire at (optional)
          recurring_seconds: int — repeat interval (optional)
          timeout_seconds: int — max execution time for scripts (default: 300)
          enabled: bool (default: true)
          notify: bool (default: true)
          notify_on_error_only: bool (default: false)
          expires_at: float — unix timestamp after which the job auto-expires (optional)

        Must provide a schedule: cron, delay_seconds, fire_at, or recurring_seconds.
        """
        if not self._check_rate_limit(api_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured yet"})
            return
        if not self._check_local_or_auth():
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            job_type = data.get("type", "script")

            # handle delay_seconds -> fire_at conversion
            delay = data.get("delay_seconds")
            if delay is not None:
                try:
                    delay = int(delay)
                    if delay < 1:
                        self.send_json(400, {"error": "delay_seconds must be >= 1"})
                        return
                    data["fire_at"] = time.time() + delay
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "delay_seconds must be an integer"})
                    return

            # validate fire_at if provided directly
            fire_at = data.get("fire_at")
            if fire_at is not None and delay is None:
                try:
                    fire_at = float(fire_at)
                    if fire_at <= time.time():
                        self.send_json(400, {"error": "fire_at must be in the future"})
                        return
                    data["fire_at"] = fire_at
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "fire_at must be a unix timestamp"})
                    return

            # validate expires_at if provided
            expires_at = data.get("expires_at")
            if expires_at is not None:
                try:
                    expires_at = float(expires_at)
                    if expires_at <= time.time():
                        self.send_json(400, {"error": "expires_at must be in the future"})
                        return
                    data["expires_at"] = expires_at
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "expires_at must be a unix timestamp"})
                    return

            # validate recurring_seconds
            recurring = data.get("recurring_seconds")
            if recurring is not None:
                try:
                    recurring = int(recurring)
                    if recurring < 10:
                        self.send_json(400, {"error": "recurring_seconds must be >= 10"})
                        return
                    data["recurring_seconds"] = recurring
                    # if no other schedule, set fire_at to first interval from now
                    if not data.get("fire_at") and not data.get("cron"):
                        data["fire_at"] = time.time() + recurring
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "recurring_seconds must be an integer"})
                    return

            # validate cron expression if provided
            if data.get("cron"):
                valid, cron_err = _validate_cron(data["cron"])
                if not valid:
                    self.send_json(400, {"error": f"invalid cron expression: {cron_err}"})
                    return

            # must have at least one scheduling mechanism
            if not data.get("cron") and not data.get("fire_at"):
                self.send_json(400, {"error": "provide a schedule: cron, delay_seconds, fire_at, or recurring_seconds"})
                return

            # sanitize strings
            if data.get("text"):
                data["text"] = _sanitize_string(data["text"], max_length=500)
            if data.get("command"):
                data["command"] = _sanitize_string(data["command"], max_length=4000)
            if data.get("cron"):
                data["cron"] = _sanitize_string(data["cron"], max_length=100)
            if data.get("name"):
                data["name"] = _sanitize_string(data["name"], max_length=200)
            if data.get("id"):
                data["id"] = _sanitize_string(data["id"], max_length=100)
            if data.get("model"):
                data["model"] = _sanitize_string(data["model"], max_length=200)
            if data.get("provider"):
                data["provider"] = _sanitize_string(data["provider"], max_length=200)

            # default name for reminders
            if job_type == "reminder" and not data.get("name") and data.get("text"):
                data["name"] = data["text"][:80]

            data["type"] = job_type
            job, err = create_job(data)
            if err:
                self.send_json(409, {"error": err})
            else:
                response = {"created": True, "job": job}
                if job.get("fire_at"):
                    response["fires_in_seconds"] = round(job["fire_at"] - time.time())
                    response["fires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(job["fire_at"]))
                if job.get("expires_at"):
                    response["expires_in_seconds"] = round(job["expires_at"] - time.time())
                    response["expires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(job["expires_at"]))
                self.send_json(201, response)
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
        except Exception as e:
            self._internal_error(e, "handle_create_job")

    def handle_list_jobs(self):
        """GET /api/jobs — list all jobs."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        active = list_active_jobs()
        now = time.time()
        for j in active:
            if j.get("fire_at"):
                j["fires_in_seconds"] = round(j["fire_at"] - now)
                j["fires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(j["fire_at"]))
            if j.get("cron"):
                j["cron_human"] = humanize_cron(j["cron"])
            if j.get("expires_at"):
                j["expires_in_seconds"] = round(j["expires_at"] - now)
                j["expires_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(j["expires_at"]))
        self.send_json(200, {"jobs": active, "count": len(active)})

    def handle_schedule_upcoming(self):
        """GET /api/schedule/upcoming — list upcoming jobs with next-run times.

        Query params:
          hours: int — look-ahead window (default: 24, max: 168)

        Returns unified view of jobs from both job engine and goose schedule.
        Sorted by next_run time. Designed for LLM consumption.
        """
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        try:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            hours = min(int(params.get("hours", ["24"])[0]), 168)
        except (ValueError, TypeError):
            hours = 24
        upcoming = get_upcoming_jobs(hours=hours)
        self.send_json(200, {
            "upcoming": upcoming,
            "count": len(upcoming),
            "window_hours": hours,
        })

    def handle_schedule_context(self):
        """GET /api/schedule/context — human-readable schedule summary for LLM.

        Query params:
          hours: int — look-ahead window (default: 24, max: 168)

        Returns a plain-text summary the LLM can include in its reasoning.
        """
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        try:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            hours = min(int(params.get("hours", ["24"])[0]), 168)
        except (ValueError, TypeError):
            hours = 24
        context = get_schedule_context(hours=hours)
        self.send_json(200, {"context": context, "window_hours": hours})

    def handle_delete_job(self, job_id):
        """DELETE /api/jobs/<id> — delete/cancel a job."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        if delete_job(job_id):
            self.send_json(200, {"deleted": True, "id": job_id})
        else:
            self.send_json(404, {"error": "job not found"})

    def handle_run_job(self, job_id):
        """POST /api/jobs/<id>/run — trigger immediate run."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return

        with _jobs_lock:
            job = next((j for j in _jobs if j["id"] == job_id), None)

        if not job:
            self.send_json(404, {"error": "job not found"})
            return
        if job.get("currently_running"):
            self.send_json(409, {"error": "job already running"})
            return

        if job.get("command"):
            def _run(j):
                j["currently_running"] = True
                _save_jobs()
                try:
                    status, output = _run_script(j)
                    j["last_status"] = status
                    j["last_output"] = output[:500]
                finally:
                    j["currently_running"] = False
                    j["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    _save_jobs()

            threading.Thread(target=_run, args=(job,), daemon=True).start()
        else:
            # fire reminder immediately
            status, output = _fire_reminder(job)
            job["last_status"] = status
            job["last_output"] = output[:500]
            job["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_jobs()

        self.send_json(202, {"started": True, "job_id": job_id})

    def handle_update_job(self, job_id):
        """PUT /api/jobs/<id> — update job fields."""
        if not self._check_rate_limit(api_limiter):
            return
        if _is_first_boot():
            self.send_json(403, {"error": "agent not configured"})
            return
        if not self._check_local_or_auth():
            return
        try:
            body = self._read_body()
            data = json.loads(body)
        except (json.JSONDecodeError, Exception):
            self.send_json(400, {"error": "invalid JSON"})
            return

        # sanitize string fields
        for field in ("name", "command", "text", "cron", "model", "provider"):
            if data.get(field):
                data[field] = _sanitize_string(data[field], max_length=200)

        updated, err = update_job(job_id, data)
        if err:
            status_code = 404 if "not found" in err else 400
            self.send_json(status_code, {"error": err})
        else:
            self.send_json(200, {"updated": True, "job": updated})

    # ── channel plugin endpoints ──

    def handle_list_channels(self):
        """GET /api/channels — list loaded channel plugins (localhost only)."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        with _channels_lock:
            channels = []
            for name, entry in _loaded_channels.items():
                ch = entry["channel"]
                adapter = entry.get("adapter")
                caps = adapter.capabilities().to_dict() if adapter else ChannelCapabilities().to_dict()
                channels.append({
                    "name": name,
                    "version": ch.get("version", 0),
                    "has_poll": callable(ch.get("poll")),
                    "has_setup": callable(ch.get("setup")),
                    "credentials": ch.get("credentials", []),
                    "capabilities": caps,
                })
        self.send_json(200, {"channels": channels, "count": len(channels)})

    def handle_reload_channels(self):
        """POST /api/channels/reload — hot-reload all channel plugins (localhost only)."""
        if not self._check_rate_limit(api_limiter):
            return
        if not self._check_local_or_auth():
            return
        names = _reload_channels()
        self.send_json(200, {"reloaded": True, "channels": names, "count": len(names)})

    # ── login page + login endpoint ──

    def handle_login_page(self):
        """GET /login — serve custom HTML login page."""
        body = LOGIN_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for header, value in SECURITY_HEADERS.items():
            self.send_header(header, value)
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def handle_auth_login(self):
        """POST /api/auth/login — password-based login, sets session cookie."""
        if not self._check_rate_limit(auth_limiter):
            return
        stored, is_hashed = get_auth_token()
        if not stored:
            self.send_json(400, {"error": "No password configured yet"})
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            password = data.get("password", "")
            if not password:
                self.send_json(400, {"error": "Password is required"})
                return
            # verify password
            ok = False
            if is_hashed:
                ok = verify_token(password, stored)
            else:
                ok = (password == stored)
            if not ok:
                self.send_json(401, {"error": "Invalid password"})
                return
            # success: create session and set cookie
            cookie_val = _create_auth_session()
            secure_flag = "; Secure" if os.environ.get("RAILWAY_ENVIRONMENT") else ""
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Set-Cookie",
                f"gooseclaw_session={cookie_val}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_MAX_AGE}{secure_flag}",
            )
            resp_body = json.dumps({"success": True}).encode()
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
        except Exception as e:
            self._internal_error(e, "handle_auth_login")

    # ── auth recovery endpoint ──

    def handle_auth_recover(self):
        """POST /api/auth/recover — reset password using recovery secret."""
        if not self._check_rate_limit(auth_limiter):
            return
        recovery_secret = os.environ.get("GOOSECLAW_RECOVERY_SECRET", "")
        if not recovery_secret:
            self.send_json(404, {"error": "auth recovery not configured. Set GOOSECLAW_RECOVERY_SECRET env var."})
            return
        body = self._read_body()
        try:
            data = json.loads(body)
            provided = _sanitize_string(data.get("secret", ""))
            if not provided:
                self.send_json(400, {"error": "secret field is required"})
                return
            if not secrets.compare_digest(provided, recovery_secret):
                self.send_json(403, {"error": "invalid recovery secret"})
                return
            # generate temporary password for recovery
            new_token = secrets.token_urlsafe(24)
            new_hash = hash_token(new_token)
            # update setup.json
            setup = load_setup()
            if not setup:
                self.send_json(400, {"error": "no setup configuration found"})
                return
            setup["web_auth_token_hash"] = new_hash
            setup.pop("web_auth_token", None)  # remove legacy plaintext
            save_setup(setup)
            # invalidate all existing sessions on password change
            _invalidate_all_auth_sessions()
            self.send_json(200, {
                "success": True,
                "temporary_password": new_token,
                "message": "Password reset. Use this temporary password to log in, then change it in settings."
            })
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid JSON"})
        except Exception as e:
            self._internal_error(e, "handle_auth_recover")

    # ── reverse proxy to goosed ──

    def proxy_to_goose(self):
        if not is_configured():
            self.send_response(302)
            self.send_header("Location", "/setup")
            self.end_headers()
            return

        with goose_lock:
            gproc = goosed_process
        if gproc is None or gproc.poll() is not None:
            with _startup_state_lock:
                state_copy = dict(goosed_startup_state)
            stderr_tail = _get_recent_stderr(10)
            error_detail = {
                "status": state_copy["state"],
                "message": state_copy.get("message", "Agent is starting up"),
                "error": stderr_tail if state_copy["state"] == "error" else "",
                "retry_after": 5,
            }
            body = json.dumps(error_detail).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "5")
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            conn = _goosed_conn(timeout=PROXY_TIMEOUT)

            # forward headers
            headers = {}
            for key in self.headers:
                if key.lower() not in ("host", "transfer-encoding", "connection"):
                    headers[key] = self.headers[key]
            headers["Host"] = f"127.0.0.1:{GOOSE_WEB_PORT}"
            headers["Connection"] = "close"

            # replace user Authorization with internal token
            # gateway already authenticated the user in do_GET/do_POST
            if _INTERNAL_GOOSE_TOKEN:
                headers.pop("Authorization", None)
                headers["X-Secret-Key"] = _INTERNAL_GOOSE_TOKEN

            # read body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            # per-channel model routing for web requests
            if self.command == "POST" and "/reply" in self.path and body:
                try:
                    setup = load_setup()
                    if setup and setup.get("channel_routes", {}).get("web"):
                        req_data = json.loads(body)
                        sid = req_data.get("session_id", "")
                        if sid:
                            model_cfg = get_model_for_channel(setup, "web")
                            if model_cfg:
                                _update_goose_session_provider(sid, model_cfg)
                except Exception:
                    pass  # non-fatal, proceed with default model

            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            # send response status and headers
            self.send_response(resp.status)
            is_sse = False
            proxied_headers = set()
            for key, val in resp.getheaders():
                lower = key.lower()
                if lower in ("transfer-encoding", "connection"):
                    continue
                # rewrite location headers
                if lower == "location":
                    val = val.replace(f"http://127.0.0.1:{GOOSE_WEB_PORT}", "")
                self.send_header(key, val)
                proxied_headers.add(lower)
                if lower == "content-type" and "text/event-stream" in val:
                    is_sse = True
            # inject security headers into proxied responses (don't overwrite if already set)
            for header, value in SECURITY_HEADERS.items():
                if header.lower() not in proxied_headers:
                    self.send_header(header, value)
            if os.environ.get("RAILWAY_ENVIRONMENT"):
                if "strict-transport-security" not in proxied_headers:
                    self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            # persist auth via session cookie after successful Basic Auth
            self._inject_session_cookie()
            self.end_headers()

            # stream the response body
            if is_sse:
                # SSE connections are long-lived — remove socket timeout so they
                # don't get killed by PROXY_TIMEOUT during an active stream
                try:
                    conn.sock.settimeout(None)
                except Exception:
                    pass
                while True:
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                self.wfile.flush()

            conn.close()

        except OSError:
            try:
                with _startup_state_lock:
                    state_copy = dict(goosed_startup_state)
                stderr_tail = _get_recent_stderr(10)
                error_detail = {
                    "status": state_copy["state"],
                    "message": state_copy.get("message", "Agent is starting up"),
                    "error": stderr_tail if state_copy["state"] == "error" else "",
                    "retry_after": 5,
                }
                body = json.dumps(error_detail).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "5")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass  # client disconnected
        except Exception as e:
            print(f"[gateway] proxy error: {e}", file=sys.stderr)
            try:
                self.send_error(502, "Gateway error")
            except Exception:
                pass

    # ── helpers ──

    def _internal_error(self, e, context=""):
        """Log real error to stderr, return sanitized response to client."""
        print(f"[gateway] ERROR ({context}): {e}", file=sys.stderr)
        self.send_json(500, {"error": "Internal server error. Check server logs.", "code": "INTERNAL_ERROR"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _inject_session_cookie(self):
        """If Basic Auth succeeded this request, set a session cookie so the
        browser won't re-prompt on subsequent requests."""
        if getattr(self, "_set_session_cookie", False):
            stored, _ = get_auth_token()
            if stored:
                cookie_val = _create_auth_session()
                secure_flag = "; Secure" if os.environ.get("RAILWAY_ENVIRONMENT") else ""
                self.send_header(
                    "Set-Cookie",
                    f"gooseclaw_session={cookie_val}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_MAX_AGE}{secure_flag}",
                )
            self._set_session_cookie = False

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # security headers on every JSON response
        for header, value in SECURITY_HEADERS.items():
            self.send_header(header, value)
        # add HSTS only when running on Railway (which terminates TLS)
        if os.environ.get("RAILWAY_ENVIRONMENT"):
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Origin-aware CORS: only allow same-host origins, never wildcard.
        # Same-host means the Origin header matches the request Host header
        # (accounting for http/https scheme). Requests with no Origin header
        # (same-origin or server-to-server) need no CORS header at all.
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and host and (
            origin == f"http://{host}" or origin == f"https://{host}"
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
        # persist auth via session cookie after successful Basic Auth
        self._inject_session_cookie()
        self.end_headers()
        self.wfile.write(body)


# ── main ────────────────────────────────────────────────────────────────────

def main():
    print(f"[gateway] gooseclaw gateway starting on 0.0.0.0:{PORT}")

    if is_configured():
        # re-apply config from setup.json (env vars lost on container restart)
        setup = load_setup()
        if setup:
            apply_config(setup)
        print("[gateway] provider configured. starting goose web...")
        start_goosed()

        # start health monitor to auto-restart goosed on crash
        health_thread = threading.Thread(target=goose_health_monitor, daemon=True)
        health_thread.start()

        # start session watcher to auto-forward scheduled output to telegram
        start_session_watcher()

        # start job engine (unified timer + script runner, zero LLM cost)
        start_job_engine()

        # start cron scheduler (reads goose schedule.json, fires jobs via goosed)
        start_cron_scheduler()

        # start memory writer (end-of-session learning)
        start_memory_writer()

        # load watchers and start watcher engine
        _load_watchers()
        start_watcher_engine()

        # load channel plugins from /data/channels/
        _load_all_channels()

        # env-var-only deployments: start default bot if not already started by apply_config
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tg_token and not _bot_manager.get_bot("default"):
            start_telegram_gateway(tg_token)
    else:
        print("[gateway] no provider configured. serving setup wizard.")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), GatewayHandler)

    # periodic rate limiter cleanup (every 5 minutes) to free stale IP entries
    def _rate_limiter_cleanup():
        while True:
            time.sleep(300)
            api_limiter.cleanup()
            auth_limiter.cleanup()
            notify_limiter.cleanup()

    threading.Thread(target=_rate_limiter_cleanup, daemon=True).start()

    def shutdown(_sig, _frame):
        global _job_engine_running, _cron_scheduler_running
        print("[gateway] shutting down...")
        # stop accepting new connections first
        threading.Thread(target=server.shutdown, daemon=True).start()
        # unload all channel plugins (stop threads, call teardown)
        with _channels_lock:
            channel_names = list(_loaded_channels.keys())
        for ch_name in channel_names:
            _unload_channel(ch_name)
        # terminate goosed and clean up PID
        stop_goosed()
        _remove_pid("goosed")
        # stop all telegram bots via BotManager
        _bot_manager.stop_all()
        # stop session watcher, job engine, cron scheduler, watcher engine
        _session_watcher_running = False
        _job_engine_running = False
        _cron_scheduler_running = False
        stop_watcher_engine()
        print("[gateway] shutdown complete")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()
    sys.exit(0)


if __name__ == "__main__":
    main()

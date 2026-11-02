"""Authenticated local HTTP transport for durable gameplay events."""
from __future__ import annotations

import hmac
import ipaddress
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .bridge import GodBridge
from .events import MAX_EVENT_BYTES, validate_event


class EventHandler(BaseHTTPRequestHandler):
    bridge: GodBridge
    voice_service = None
    timeout = 5

    def _authorized(self) -> bool:
        expected = self.bridge.config.event_token
        if expected and not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"), f"Bearer {expected}".encode("utf-8")):
            self.send_error(401, "event authentication required")
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"ok": True, "service": "minecraft-god-bridge"})
        elif parsed.path == "/voice/healthz":
            if self._authorized():
                self._send_json(200, self.voice_service.status() if self.voice_service else {"state": "disabled"})
        elif parsed.path == "/events":
            if not self._authorized():
                return
            try:
                query = parse_qs(parsed.query)
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                if after < 0 or not 1 <= limit <= 200:
                    raise ValueError()
            except ValueError:
                self.send_error(400, "after must be nonnegative; limit must be 1 to 200")
                return
            self._send_json(200, {"events": self.bridge.events_since(after, limit), "latest": self.bridge.latest_event_id()})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path == "/voice":
            self._voice_upload()
            return
        if urlparse(self.path).path != "/events":
            self.send_error(404)
            return
        if not self._authorized():
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("chunked event bodies are unsupported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_EVENT_BYTES:
                raise ValueError("event body must be 1 to 65536 bytes")
            payload = validate_event(json.loads(self.rfile.read(length)))
            event = self.bridge.record_event(payload)
            self._send_json(202, {"ok": True, "event": event})
        except (ValueError, TypeError, RecursionError, UnicodeError) as error:
            self.send_error(400, str(error))
        except TimeoutError:
            self.send_error(408, "event body timed out")

    def _voice_upload(self) -> None:
        if not self._authorized():
            return
        from .voice import MAX_VOICE_BYTES, decode_upload
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_VOICE_BYTES:
                raise ValueError("invalid voice body length")
            if self.headers.get("Content-Type") != "application/x-minecraft-god-voice":
                raise ValueError("invalid voice content type")
            if not self.voice_service:
                self.send_error(503, "local voice transcription is disabled")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete voice upload")
            event, audio = decode_upload(body)
            if not self.voice_service.submit(event, audio):
                self.send_error(503, "voice recognizer unavailable or queue full")
                return
            self._send_json(202, {"ok": True, "utterance_id": event["event_uuid"]})
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self.send_error(400, "invalid voice metadata or audio")
        except TimeoutError:
            self.send_error(408, "voice upload timed out")

    def log_message(self, format: str, *args) -> None:
        if len(args) >= 2 and str(args[0]).startswith("GET /events") and str(args[1]) == "200":
            return
        print(f"[event-http] {format % args}", file=sys.stderr)

    def _send_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def start_event_server(bridge: GodBridge, *, voice_service=None) -> ThreadingHTTPServer:
    try:
        local = ipaddress.ip_address(bridge.config.event_host).is_loopback
    except ValueError:
        local = bridge.config.event_host == "localhost"
    if not local:
        raise ValueError("event hub must bind to loopback; use an authenticated local tunnel for remote Paper")
    if not bridge.config.event_token and not bridge.config.dry_run:
        raise ValueError("event hub requires MINECRAFT_EVENT_TOKEN_FILE; run setup first")
    handler = type("ConfiguredEventHandler", (EventHandler,), {"bridge": bridge, "voice_service": voice_service})
    server = ThreadingHTTPServer((bridge.config.event_host, bridge.config.event_port), handler)
    threading.Thread(target=server.serve_forever, name="minecraft-event-http", daemon=True).start()
    return server

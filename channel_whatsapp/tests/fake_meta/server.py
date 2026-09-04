from __future__ import annotations
import hashlib, hmac, json, threading, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse


@dataclass
class FakeMedia:
    media_id: str
    content: bytes
    sha256: str
    mime: str = "application/pdf"


@dataclass
class SentMessage:
    to: str
    kind: str
    payload: dict[str, Any]
    wamid: str


class FakeMetaState:
    def __init__(self):
        self.media: dict[str, FakeMedia] = {}
        self.sent_messages: list[SentMessage] = []
        self.app_secret = "test_app_secret"
        self.base_url = ""

    def inject_media(self, *, media_id, content, sha256=None, mime="application/pdf"):
        if sha256 is None:
            sha256 = hashlib.sha256(content).hexdigest()
        self.media[media_id] = FakeMedia(media_id=media_id, content=content, sha256=sha256, mime=mime)

    def signed_webhook_payload(self, *, phone_number_id, from_number, media_id,
                                kind="document", wamid=None, sha256=None,
                                mime="application/pdf", filename="booking.pdf"):
        if wamid is None:
            wamid = f"wamid.{uuid.uuid4().hex}"
        if sha256 is None and media_id in self.media:
            sha256 = self.media[media_id].sha256
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_number_id},
            "contacts": [{"wa_id": from_number.lstrip("+")}],
            "messages": [{"id": wamid, "type": kind, "timestamp": str(int(time.time())),
                           kind: {"id": media_id, "mime_type": mime,
                                  "filename": filename, "sha256": sha256}}]
        }}]}]}
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return body, sig


class FakeMetaHandler(BaseHTTPRequestHandler):
    state: FakeMetaState

    def log_message(self, *args): pass

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "v19.0":
            media_id = parts[1]
            if media_id not in self.state.media:
                self._respond(404, {}); return
            url = f"{self.state.base_url}/v19.0/{media_id}/download"
            self._respond(200, {"url": url, "mime_type": self.state.media[media_id].mime}); return
        if len(parts) == 3 and parts[0] == "v19.0" and parts[2] == "download":
            media_id = parts[1]
            if media_id not in self.state.media:
                self._respond(404, {}); return
            fm = self.state.media[media_id]
            self.send_response(200)
            self.send_header("Content-Type", fm.mime)
            self.send_header("Content-Length", str(len(fm.content)))
            self.end_headers()
            self.wfile.write(fm.content); return
        self._respond(404, {})

    def do_POST(self):
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/")
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)
        if len(parts) == 2 and parts[1] == "messages":
            try:
                body = json.loads(body_bytes)
            except ValueError:
                self._respond(400, {}); return
            wamid = f"wamid.out.{uuid.uuid4().hex}"
            self.state.sent_messages.append(SentMessage(to=body.get("to",""),
                kind=body.get("type","text"), payload=body, wamid=wamid))
            self._respond(200, {"messages": [{"id": wamid}]}); return
        self._respond(404, {})

    def _respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def fake_meta_server():
    state = FakeMetaState()
    class Handler(FakeMetaHandler): pass
    Handler.state = state  # type: ignore[attr-defined]
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    state.base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()

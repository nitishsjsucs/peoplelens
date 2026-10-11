"""Worker API client: never follows an Access redirect, sends an explicit user agent."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from peoplelens.dev.client import USER_AGENT, WorkerClient


@pytest.fixture()
def server():
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["user_agent"] = self.headers.get("user-agent")
            if self.path == "/api/health":
                self.send_response(302)
                self.send_header("location", "https://lumen.cloudflareaccess.com/cdn-cgi/access/login")
                self.send_header("content-length", "0")
                self.end_headers()
                return
            body = json.dumps({"role": "executive"}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


def test_an_access_redirect_is_reported_not_followed(server):
    base, _ = server
    reply = WorkerClient(base, {}).health()
    assert reply.status == 302  # following it would turn a failed login into a 200 HTML page


def test_requests_carry_an_explicit_user_agent(server):
    base, seen = server
    reply = WorkerClient(base, {"CF-Access-Client-Id": "x.access"}).me()
    assert reply.status == 200 and reply.body == {"role": "executive"}
    assert seen["user_agent"] == USER_AGENT

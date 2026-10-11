"""Small HTTP client for the Worker API, used by `pl ask`, `pl chat` and the eval runner."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "peoplelens-cli/0.1"


@dataclass
class Reply:
    status: int
    body: dict
    latency_ms: float


class WorkerClient:
    def __init__(self, base_url: str, headers: dict[str, str], timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> Reply:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        # An explicit agent: Cloudflare's browser integrity check can refuse the default "Python-urllib".
        req.add_header("user-agent", USER_AGENT)
        for k, v in self.headers.items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("content-type", "application/json")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        latency = (time.perf_counter() - started) * 1000
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw[:200].decode(errors="replace")}
        return Reply(status, parsed, latency)

    def health(self) -> Reply:
        return self._request("GET", "/api/health")

    def me(self) -> Reply:
        return self._request("GET", "/api/me")

    def session(self) -> str:
        r = self._request("POST", "/api/sessions", {})
        if r.status != 200 or "session_id" not in r.body:
            raise RuntimeError(f"could not create a session: HTTP {r.status} {r.body}")
        return r.body["session_id"]

    def ask(self, session_id: str, message: str) -> Reply:
        return self._request("POST", "/api/ask", {"session_id": session_id, "message": message})

    def choose(self, session_id: str, option_id: str) -> Reply:
        return self._request("POST", "/api/ask", {"session_id": session_id, "choose_option": option_id})


def render(body: dict) -> str:
    """Human-readable rendering of an /api/ask response for the terminal."""
    kind = body.get("type")
    if kind == "answer":
        a = body["answer"]
        cols = [c["name"] for c in a["columns"]]
        lines = [a["narrative"], "", " | ".join(cols)]
        for row in a["rows"]:
            cells = []
            for c in cols:
                v = row.get(c)
                cells.append("(hidden)" if row.get("suppressed") and v is None else str(v))
            lines.append(" | ".join(cells))
        m = body.get("meta", {})
        lines.append("")
        lines.append(f"[{m.get('catalog_key')}; {m.get('latency_ms')} ms; llm {m.get('llm_provider')}; warehouse {m.get('warehouse')}]")
        return "\n".join(lines)
    if kind == "clarification":
        c = body["clarification"]
        return c["question"] + "\n" + "\n".join(f"  {o['id']}: {o['label']}" for o in c["options"])
    if kind == "refusal":
        r = body["refusal"]
        return f"Refused ({r['code']}): {r['message']}"
    return json.dumps(body)

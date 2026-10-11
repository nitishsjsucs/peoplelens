"""Cloudflare REST client for bootstrap, doctor and smoke (custom API token).

wrangler's OAuth login has no AI Gateway or Access scopes, so the gateway, the
Access application, its policies and the service tokens are managed here with
the `peoplelens-bootstrap` API token from .secrets/cloudflare.env.
Every create is preceded by a list or get (get-or-create, or converge).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from peoplelens.dev.client import USER_AGENT

API = "https://api.cloudflare.com/client/v4"
GATEWAY_ID = "peoplelens"
WORKER_NAME = "peoplelens"
APP_NAME = "peoplelens"

GATEWAY_SETTINGS = {
    "authentication": True,
    "collect_logs": True,
    "cache_ttl": 0,
    "cache_invalidate_on_update": True,
    "rate_limiting_interval": 60,
    "rate_limiting_limit": 120,
    "rate_limiting_technique": "sliding",
    "log_management": 10000,
    "log_management_strategy": "DELETE_OLDEST",
}


class CloudflareError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Cloudflare API {status}: {message}")
        self.status = status


@dataclass
class Response:
    status: int
    body: dict


Transport = Callable[[str, str, dict | None, dict[str, str]], Response]


def urllib_transport(method: str, url: str, body: dict | None, headers: dict[str, str]) -> Response:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("user-agent", USER_AGENT)
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                return Response(resp.status, json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                # Some endpoints (AI Gateway log payloads) return the stored body as is.
                return Response(resp.status, {"_raw": raw[:4000].decode(errors="replace")})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return Response(exc.code, json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return Response(exc.code, {"errors": [{"message": raw[:200].decode(errors="replace")}]})
    except urllib.error.URLError as exc:
        return Response(0, {"errors": [{"message": str(exc.reason)}]})


class CloudflareApi:
    def __init__(self, account_id: str, token: str, transport: Transport = urllib_transport):
        self.account_id = account_id
        self.token = token
        self.transport = transport

    # ------------------------------------------------------------ plumbing
    def request(self, method: str, path: str, body: dict | None = None, *, account: bool = True,
                allow_404: bool = False) -> dict | None:
        prefix = f"/accounts/{self.account_id}" if account else ""
        res = self.transport(method, f"{API}{prefix}{path}", body, {"authorization": f"Bearer {self.token}"})
        if res.status == 404 and allow_404:
            return None
        if res.status < 200 or res.status >= 300 or res.body.get("success") is False:
            errors = res.body.get("errors") or []
            msg = "; ".join(str(e.get("message", e)) for e in errors) or f"HTTP {res.status}"
            raise CloudflareError(res.status, msg)
        return res.body

    def result(self, method: str, path: str, body: dict | None = None, **kw):
        out = self.request(method, path, body, **kw)
        return None if out is None else out.get("result")

    # ------------------------------------------------------------ token and account
    def verify_token(self) -> dict:
        return self.result("GET", "/user/tokens/verify", account=False)

    def account(self) -> dict:
        return self.result("GET", "")

    # ------------------------------------------------------------ workers
    def subdomain(self) -> str | None:
        r = self.result("GET", "/workers/subdomain", allow_404=True)
        return (r or {}).get("subdomain") or None

    def register_subdomain(self, name: str) -> str:
        r = self.result("PUT", "/workers/subdomain", {"subdomain": name})
        return r["subdomain"]

    def worker(self, name: str = WORKER_NAME) -> dict | None:
        return self.result("GET", f"/workers/workers/{name}", allow_404=True)

    # ------------------------------------------------------------ AI Gateway
    def gateway(self, gateway_id: str = GATEWAY_ID) -> dict | None:
        return self.result("GET", f"/ai-gateway/gateways/{gateway_id}", allow_404=True)

    def list_gateways(self) -> list[dict]:
        return self.result("GET", "/ai-gateway/gateways") or []

    def ensure_gateway(self, gateway_id: str = GATEWAY_ID) -> tuple[str, dict]:
        """Create the gateway, or converge its settings. Returns (action, gateway)."""
        existing = self.gateway(gateway_id)
        if existing is None:
            return "created", self.result("POST", "/ai-gateway/gateways", {"id": gateway_id, **GATEWAY_SETTINGS})
        drift = {k: v for k, v in GATEWAY_SETTINGS.items() if existing.get(k) != v}
        if not drift:
            return "unchanged", existing
        return "updated", self.result("PUT", f"/ai-gateway/gateways/{gateway_id}", {"id": gateway_id, **GATEWAY_SETTINGS})

    def gateway_log(self, log_id: str, gateway_id: str = GATEWAY_ID) -> dict | None:
        return self.result("GET", f"/ai-gateway/gateways/{gateway_id}/logs/{log_id}", allow_404=True)

    def gateway_log_part(self, log_id: str, part: str, gateway_id: str = GATEWAY_ID) -> Response:
        """Raw response of GET .../logs/{id}/request or /response. These return the stored
        payload itself (not the usual {success, result} envelope), so the caller decides."""
        url = f"{API}/accounts/{self.account_id}/ai-gateway/gateways/{gateway_id}/logs/{log_id}/{part}"
        return self.transport("GET", url, None, {"authorization": f"Bearer {self.token}"})

    # ------------------------------------------------------------ Workers AI
    def model_available(self, model: str) -> bool:
        res = self.result("GET", f"/ai/models/search?search={model.split('/')[-1]}") or []
        return any(m.get("name") == model for m in res)

    # ------------------------------------------------------------ Access
    def organization(self) -> dict | None:
        return self.result("GET", "/access/organizations", allow_404=True)

    def identity_providers(self) -> list[dict]:
        return self.result("GET", "/access/identity_providers") or []

    def access_apps(self) -> list[dict]:
        return self.result("GET", "/access/apps") or []

    def access_app(self, app_id: str) -> dict:
        return self.result("GET", f"/access/apps/{app_id}")

    def ensure_access_app(self, worker_id: str) -> tuple[str, dict]:
        for app in self.access_apps():
            if app.get("name") == APP_NAME:
                return "unchanged", app
        body = {
            "type": "self_hosted",
            "name": APP_NAME,
            "destinations": [{"type": "worker", "worker_id": worker_id}],
            "session_duration": "24h",
            "app_launcher_visible": False,
        }
        return "created", self.result("POST", "/access/apps", body)

    def app_policies(self, app_id: str) -> list[dict]:
        return self.result("GET", f"/access/apps/{app_id}/policies") or []

    def ensure_policy(self, app_id: str, name: str, decision: str, include: list[dict], precedence: int) -> tuple[str, dict]:
        body = {"name": name, "decision": decision, "include": include, "precedence": precedence}
        for p in self.app_policies(app_id):
            if p.get("name") == name:
                if p.get("decision") == decision and p.get("include") == include:
                    return "unchanged", p
                return "updated", self.result("PUT", f"/access/apps/{app_id}/policies/{p['id']}", body)
        return "created", self.result("POST", f"/access/apps/{app_id}/policies", body)

    def service_tokens(self) -> list[dict]:
        return self.result("GET", "/access/service_tokens") or []

    def create_service_token(self, name: str) -> dict:
        return self.result("POST", "/access/service_tokens", {"name": name, "duration": "8760h"})

    def rotate_service_token(self, token_id: str) -> dict:
        return self.result("POST", f"/access/service_tokens/{token_id}/rotate")

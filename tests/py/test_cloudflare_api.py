"""Cloudflare REST client: get-or-create and converge, request bodies, errors."""

from __future__ import annotations

import pytest

from peoplelens.ops.cloudflare_api import API, GATEWAY_SETTINGS, CloudflareApi, CloudflareError, Response


class FakeTransport:
    def __init__(self, routes: dict[tuple[str, str], Response]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        path = url.replace(API, "")
        return self.routes.get((method, path), Response(404, {"success": False, "errors": [{"message": "not found"}]}))


def ok(result) -> Response:
    return Response(200, {"success": True, "result": result})


ACC = "/accounts/acc123"


def test_token_verify_is_not_account_scoped_and_sends_bearer():
    t = FakeTransport({("GET", "/user/tokens/verify"): ok({"status": "active"})})
    api = CloudflareApi("acc123", "tok", t)
    assert api.verify_token() == {"status": "active"}
    assert t.calls[0][3] == {"authorization": "Bearer tok"}


def test_gateway_is_created_with_authentication_and_metadata_logging():
    t = FakeTransport({("POST", f"{ACC}/ai-gateway/gateways"): ok({"id": "peoplelens"})})
    action, _ = CloudflareApi("acc123", "tok", t).ensure_gateway()
    assert action == "created"
    method, url, body, _ = t.calls[-1]
    assert body == {"id": "peoplelens", **GATEWAY_SETTINGS}
    assert body["authentication"] is True and body["collect_logs"] is True
    assert body["rate_limiting_limit"] == 120 and body["cache_ttl"] == 0


def test_gateway_converges_only_on_drift():
    current = {"id": "peoplelens", **GATEWAY_SETTINGS}
    t = FakeTransport({("GET", f"{ACC}/ai-gateway/gateways/peoplelens"): ok(current)})
    assert CloudflareApi("acc123", "tok", t).ensure_gateway()[0] == "unchanged"
    drifted = dict(current, authentication=False)
    t2 = FakeTransport({
        ("GET", f"{ACC}/ai-gateway/gateways/peoplelens"): ok(drifted),
        ("PUT", f"{ACC}/ai-gateway/gateways/peoplelens"): ok(current),
    })
    assert CloudflareApi("acc123", "tok", t2).ensure_gateway()[0] == "updated"
    assert t2.calls[-1][2]["authentication"] is True


def test_access_app_lists_before_creating_with_a_worker_destination():
    t = FakeTransport({("GET", f"{ACC}/access/apps"): ok([{"name": "other"}]), ("POST", f"{ACC}/access/apps"): ok({"id": "app1", "aud": "aud1"})})
    action, app = CloudflareApi("acc123", "tok", t).ensure_access_app("worker-id-1")
    assert action == "created" and app["aud"] == "aud1"
    body = t.calls[-1][2]
    assert body["type"] == "self_hosted" and body["destinations"] == [{"type": "worker", "worker_id": "worker-id-1"}]
    t2 = FakeTransport({("GET", f"{ACC}/access/apps"): ok([{"name": "peoplelens", "id": "app1"}])})
    assert CloudflareApi("acc123", "tok", t2).ensure_access_app("w")[0] == "unchanged"
    assert all(c[0] == "GET" for c in t2.calls)


def test_policies_create_update_or_keep():
    include = [{"email": {"email": "a@x.example"}}]
    t = FakeTransport({
        ("GET", f"{ACC}/access/apps/app1/policies"): ok([{"id": "p1", "name": "peoplelens-users", "decision": "allow", "include": []}]),
        ("PUT", f"{ACC}/access/apps/app1/policies/p1"): ok({"id": "p1"}),
        ("POST", f"{ACC}/access/apps/app1/policies"): ok({"id": "p2"}),
    })
    api = CloudflareApi("acc123", "tok", t)
    assert api.ensure_policy("app1", "peoplelens-users", "allow", include, 1)[0] == "updated"
    assert api.ensure_policy("app1", "peoplelens-eval-service-tokens", "non_identity", [], 2)[0] == "created"
    assert t.calls[-1][2]["decision"] == "non_identity"


def test_errors_raise_with_the_api_message():
    t = FakeTransport({("GET", f"{ACC}/access/apps"): Response(403, {"success": False, "errors": [{"message": "Authentication error"}]})})
    with pytest.raises(CloudflareError, match="Authentication error"):
        CloudflareApi("acc123", "tok", t).access_apps()
    assert CloudflareApi("acc123", "tok", FakeTransport({})).worker("peoplelens") is None


def test_transport_keeps_non_json_bodies_instead_of_crashing():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from peoplelens.ops.cloudflare_api import urllib_transport

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"data: not json" if self.path == "/text" else b'{"success": true, "result": {"a": 1}}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert urllib_transport("GET", f"{base}/text", None, {}).body == {"_raw": "data: not json"}
        assert urllib_transport("GET", f"{base}/json", None, {}).body["result"] == {"a": 1}
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_create_is_retried_without_log_management_when_rejected():
    from peoplelens.ops.cloudflare_api import CORE_GATEWAY_SETTINGS

    bodies = []

    def transport(method, url, body, headers):
        if method == "GET":
            return Response(404, {"success": False, "errors": [{"message": "not found"}]})
        bodies.append(body)
        if "log_management" in body:
            return Response(400, {"success": False, "errors": [{"message": "log_management is not supported for this gateway"}]})
        return ok({"id": "peoplelens", **CORE_GATEWAY_SETTINGS})

    action, gw = CloudflareApi("acc123", "tok", transport).ensure_gateway()
    assert action == "created without log_management"
    assert bodies[0] == {"id": "peoplelens", **GATEWAY_SETTINGS}
    assert bodies[1] == {"id": "peoplelens", **CORE_GATEWAY_SETTINGS}
    assert bodies[1]["authentication"] is True and "log_management_strategy" not in bodies[1]


def test_a_gateway_without_log_fields_is_not_rewritten_forever():
    from peoplelens.ops.cloudflare_api import CORE_GATEWAY_SETTINGS

    t = FakeTransport({("GET", f"{ACC}/ai-gateway/gateways/peoplelens"): ok({"id": "peoplelens", **CORE_GATEWAY_SETTINGS})})
    assert CloudflareApi("acc123", "tok", t).ensure_gateway()[0] == "unchanged"


def test_other_gateway_errors_are_not_retried():
    def transport(method, url, body, headers):
        if method == "GET":
            return Response(404, {"success": False, "errors": [{"message": "not found"}]})
        return Response(403, {"success": False, "errors": [{"message": "Authentication error"}]})

    with pytest.raises(CloudflareError):
        CloudflareApi("acc123", "tok", transport).ensure_gateway()

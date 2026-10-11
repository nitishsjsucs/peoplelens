"""LLM clients for the text-to-SQL baseline (harness only, never part of the product).

Local: the same llama-server (OpenAI-compatible) the Worker uses.
Production: Workers AI REST with the bootstrap API token, same model and settings
as the planner. It does not go through the authenticated AI Gateway.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class LlmCallError(RuntimeError):
    pass


def _post(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("content-type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise LlmCallError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LlmCallError(str(exc)) from exc


class OpenAiCompatSql:
    provider = "openai_compat"

    def __init__(self, base_url: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict], schema: dict) -> tuple[str, float, dict]:
        started = time.perf_counter()
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": "sql_output", "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0,
            "max_tokens": 400,
        }
        out = _post(f"{self.base_url}/chat/completions", body, {}, self.timeout)
        content = (out.get("choices") or [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise LlmCallError("no content")
        return content, (time.perf_counter() - started) * 1000, out.get("usage") or {}


class WorkersAiRestSql:
    provider = "workers_ai"

    def __init__(self, account_id: str, api_token: str, model: str, timeout: float = 60.0):
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
        self.token = api_token
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict], schema: dict) -> tuple[str, float, dict]:
        started = time.perf_counter()
        body = {
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": schema},
            "temperature": 0,
            "max_tokens": 400,
        }
        out = _post(self.url, body, {"authorization": f"Bearer {self.token}"}, self.timeout)
        result = out.get("result") or {}
        response = result.get("response")
        text = response if isinstance(response, str) else json.dumps(response)
        return text, (time.perf_counter() - started) * 1000, result.get("usage") or {}

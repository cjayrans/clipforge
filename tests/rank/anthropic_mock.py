"""Mock Anthropic Messages API at the HTTP layer.

The anthropic SDK (2.x) sends requests through httpx2, which respx cannot intercept, so tests pass an
`httpx2.Client(transport=httpx2.MockTransport(...))` to `ClaudeClient(http_client=...)`. Every request body is
recorded so tests can assert exactly what would be sent to the API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2

Responder = Callable[[dict[str, Any]], httpx2.Response]


def message(
    content: list[dict[str, Any]],
    *,
    model: str = "claude-haiku-5-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    stop_details: dict[str, Any] | None = None,
) -> httpx2.Response:
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return httpx2.Response(200, json=body, headers={"request-id": "req_test"})


def tool_use(name: str, payload: dict[str, Any], **kw: Any) -> httpx2.Response:
    return message(
        [{"type": "tool_use", "id": "toolu_test", "name": name, "input": payload}], stop_reason="tool_use", **kw
    )


def json_text(payload: Any, *, thinking: bool = True, **kw: Any) -> httpx2.Response:
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    content.append({"type": "text", "text": json.dumps(payload)})
    kw.setdefault("model", "claude-sonnet-5-5")
    return message(content, **kw)


def error(status: int, kind: str = "api_error", msg: str = "boom") -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": msg}})


class FakeAnthropic:
    """Answers each request with the next queued response (or a function of the request body)."""

    def __init__(self, *responses: httpx2.Response | Responder, default: Responder | None = None) -> None:
        self.queue: list[httpx2.Response | Responder] = list(responses)
        self.default = default
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.paths: list[str] = []

    def add(self, *responses: httpx2.Response | Responder) -> None:
        self.queue.extend(responses)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        self.paths.append(request.url.path)
        if not self.queue and self.default is not None:
            return self.default(body)
        if not self.queue:
            raise AssertionError(f"unexpected Anthropic request #{len(self.requests)}")
        nxt = self.queue.pop(0)
        return nxt(body) if callable(nxt) else nxt

    def http_client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler))

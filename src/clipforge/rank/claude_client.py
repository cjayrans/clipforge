"""The one Anthropic client class (CLAUDE.md: one client per service, timeouts, retries, cost recording).

Verified 2026-11-22 against https://platform.claude.com/docs/en/models/overview and the model pages linked from it:
- `claude-haiku-5-5` and `claude-sonnet-5-5` run adaptive thinking by default and reject `temperature`, `top_p`
  and `top_k` (400), so this client never sends sampling parameters.
- Claude Sonnet 5.5 rejects forced `tool_choice` (`any`/`tool`) with a 400
  (https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide#forced-tool-use). Use `json_output`
  (structured outputs, `output_config.format`) there. Claude Haiku 5.5 accepts a forced tool; the call then skips
  thinking, which is what we want for cheap scoring.
- Structured outputs and strict tools reject `minimum`/`maximum`/`minLength`/`maxLength` and need
  `additionalProperties: false` (https://platform.claude.com/docs/en/build-with-claude/structured-outputs), so
  callers keep schemas plain and validate the result locally with pydantic.

The SDK (anthropic 2.x) sends requests through `httpx2`; tests inject `httpx2.MockTransport` via `http_client`.
SDK retries are off (`max_retries=0`); tenacity retries here so every attempt is visible and bounded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import httpx2
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from clipforge import costs
from clipforge.config import AppConfig, Settings
from clipforge.db import session_scope

log = logging.getLogger(__name__)

SERVICE = "anthropic"
DEFAULT_TIMEOUT_S = 60.0
MAX_ATTEMPTS = 3
Effort = Literal["low", "medium", "high"]

RETRYABLE: tuple[type[BaseException], ...] = (
    anthropic.RateLimitError,  # 429
    anthropic.InternalServerError,  # 5xx, incl. 529 overloaded
    anthropic.RequestTimeoutError,  # 408
    anthropic.ConflictError,  # 409
    anthropic.APIConnectionError,  # network, incl. APITimeoutError
)


class RankError(RuntimeError):
    """A model call returned nothing usable (refusal, truncation, missing tool call, bad JSON) or is misconfigured."""


@dataclass(frozen=True)
class LLMResult:
    data: Any
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    request_id: str | None = None
    attempts: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


class ClaudeClient:
    """Anthropic Messages API wrapper: spend-cap guard before, cost record after, bounded retries."""

    def __init__(
        self,
        api_key: SecretStr | str | None,
        *,
        ledger: sessionmaker[Session],
        app: AppConfig,
        http_client: httpx2.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_s: float = 1.0,
    ) -> None:
        """`ledger` makes the sessions for the spend-cap guard and cost rows. Each guard check and each cost record
        runs in its own short transaction and commits at once, so a caller's later rollback (or a crash) can never
        erase spend that already happened (COMPLIANCE #12). Don't call the client while holding an uncommitted
        write on the same SQLite file: the ledger commit would wait for that lock."""
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            raise RankError("ANTHROPIC_API_KEY is not set; ranking needs it")
        self.ledger = ledger
        self.app = app
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self._sdk = anthropic.Anthropic(api_key=key, max_retries=0, timeout=timeout_s, http_client=http_client)

    @classmethod
    def from_settings(cls, settings: Settings, ledger: sessionmaker[Session], **kwargs: Any) -> ClaudeClient:
        return cls(settings.secrets.anthropic_api_key, ledger=ledger, app=settings.app, **kwargs)

    # ----------------------------------------------------------------------------------------------

    def _create(self, **params: Any) -> tuple[anthropic.types.Message, int]:
        with session_scope(self.ledger) as s:
            costs.guard(s, self.app)  # raises SpendCapReached before spending anything
        attempts = 0
        for attempt in Retrying(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=self.backoff_s, max=30),
            reraise=True,
        ):
            with attempt:
                attempts += 1
                if attempts > 1:
                    log.warning("anthropic retry %d/%d for %s", attempts, self.max_attempts, params.get("model"))
                message = self._sdk.messages.create(**params)
        self._record(params["model"], message)
        return message, attempts

    def _record(self, model: str, message: anthropic.types.Message) -> None:
        # Priced by the model we asked for (config.prices is keyed by it). Thinking tokens are billed as output and
        # are included in output_tokens. Prompt caching is not used, so there are no cache tokens to price.
        # Committed immediately in a separate transaction (see __init__).
        usage = message.usage
        with session_scope(self.ledger) as s:
            costs.record(s, self.app, SERVICE, model, usage.input_tokens, "input_tokens")
            costs.record(s, self.app, SERVICE, model, usage.output_tokens, "output_tokens")

    @staticmethod
    def _check_stop(message: anthropic.types.Message) -> None:
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None) if message.stop_details else None
            raise RankError(f"model declined the request (refusal, category={category})")
        if message.stop_reason == "max_tokens":
            raise RankError("model output was cut off at max_tokens")

    @staticmethod
    def _result(message: anthropic.types.Message, data: Any, attempts: int) -> LLMResult:
        return LLMResult(
            data=data,
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
            request_id=getattr(message, "_request_id", None),
            attempts=attempts,
        )

    # ----------------------------------------------------------------------------------------------

    def forced_tool(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        input_schema: Mapping[str, Any],
        max_tokens: int = 4096,
        effort: Effort | None = None,
    ) -> LLMResult:
        """One call that must answer through `tool_name` (Haiku). Returns the tool input as a dict."""
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": tool_name,
                    "description": tool_description,
                    "input_schema": dict(input_schema),
                    "strict": True,
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if effort is not None:
            params["output_config"] = {"effort": effort}
        message, attempts = self._create(**params)
        self._check_stop(message)
        for block in message.content:
            if block.type == "tool_use" and block.name == tool_name:
                return self._result(message, block.input, attempts)
        raise RankError(f"model did not call {tool_name} (stop_reason={message.stop_reason})")

    def json_output(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        max_tokens: int = 8000,
        effort: Effort | None = None,
    ) -> LLMResult:
        """One call whose final text is JSON matching `schema` (structured outputs; Sonnet). Returns parsed JSON."""
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": dict(schema)}}
        if effort is not None:
            output_config["effort"] = effort
        message, attempts = self._create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        self._check_stop(message)
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise RankError(f"model returned no text (stop_reason={message.stop_reason})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RankError(f"model returned invalid JSON: {exc}") from exc
        return self._result(message, data, attempts)


def nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    """`schema` or null, in the anyOf form structured outputs accept."""
    return {"anyOf": [dict(schema), {"type": "null"}]}


def strict_object(properties: Mapping[str, Any], required: Sequence[str] | None = None) -> dict[str, Any]:
    """An object schema with additionalProperties false (required by strict tools and structured outputs)."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required if required is not None else properties),
        "additionalProperties": False,
    }

"""Any OpenAI-compatible chat completions endpoint with tool calling.

Three endpoints in the course speak this shape:

- Ollama on the Local track: `http://localhost:11434/v1`, no key.
- The LiteLLM model gateway on a platform: `NW_GATEWAY_URL` with the tenant's virtual key
  (`NW_GATEWAY_KEY`) as a bearer token. The role's model id is the gateway's model name.
- Google's managed API for open models on the Agent Platform (formerly Vertex AI), which
  serves gpt-oss through the OpenAI-compatible endpoint (verified 2026-09-29 at
  docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/...):
  `https://<region>-aiplatform.googleapis.com/v1/projects/<project>/locations/<region>`
  `/endpoints/openapi/chat/completions`, a Google access token as the bearer, and the
  model named `openai/gpt-oss-120b-maas` (regions `global` and `us-central1`) or
  `openai/gpt-oss-20b-maas` (`us-central1` only; deprecated 2026-07-21, retirement
  announced for 2026-10-21). For `global` the host has no region prefix.

It uses httpx directly rather than the openai SDK so the dependency set stays as it is,
and translates the course's types the way `anthropic_base.py` does. Assistant turns are
rebuilt from the typed fields, never replayed raw: the three endpoints disagree on the
extra fields they return (`reasoning`, `reasoning_content`) and reject each other's.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from nw.llm.errors import (
    RETRYABLE_STATUS,
    ContentFilteredError,
    RequestTimeout,
    RetryableError,
    TerminalError,
)
from nw.llm.types import Completion, Message, StopReason, ToolCall, ToolSpec, Usage

TokenSource = Callable[[], Awaitable[str | None]]

_STOP = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


def to_vendor_messages(messages: list[Message], *, system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "user" and m.tool_results:
            out.extend(
                {
                    "role": "tool",
                    "tool_call_id": r.tool_call_id,
                    "content": f"error: {r.content}" if r.is_error else r.content,
                }
                for r in m.tool_results
            )
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


def to_vendor_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def build_request(
    messages: list[Message],
    *,
    model: str,
    system: str | None,
    tools: list[ToolSpec] | None,
    max_tokens: int,
    temperature: float | None,
) -> dict[str, Any]:
    """The chat completions body. Pure, so the translation is testable without a network."""
    body: dict[str, Any] = {
        "model": model,
        "messages": to_vendor_messages(messages, system=system),
        "max_tokens": max_tokens,
        "stream": False,
    }
    vendor_tools = to_vendor_tools(tools)
    if vendor_tools:
        body["tools"] = vendor_tools
    if temperature is not None:
        body["temperature"] = temperature
    return body


def _text_of(content: Any) -> str:
    """`content` is a string, null, or a list of parts on newer servers."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") for part in content if isinstance(part, dict) and "text" in part
    )


def _arguments_of(raw: Any) -> dict[str, Any]:
    """Arguments arrive as a JSON string; Ollama sometimes sends the object. Malformed
    JSON is kept under `_raw` so the tool layer can report it instead of guessing."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def from_vendor_response(
    body: dict[str, Any], *, model: str, latency_ms: float, request_id: str | None = None
) -> Completion:
    choices = body.get("choices") or []
    if not choices:
        raise TerminalError("chat completion returned no choices", request_id=request_id)
    choice = choices[0]
    message = choice.get("message") or {}
    tool_calls: list[ToolCall] = []
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        tool_calls.append(
            ToolCall(
                id=call.get("id") or f"call_{i}",
                name=fn.get("name") or "",
                arguments=_arguments_of(fn.get("arguments")),
            )
        )
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    finish = choice.get("finish_reason") or ""
    stop = _STOP.get(finish, StopReason.OTHER)
    if stop is StopReason.OTHER and tool_calls:
        stop = StopReason.TOOL_USE
    rid = body.get("id") or request_id or ""
    completion = Completion(
        text=_text_of(message.get("content")),
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=usage.get("prompt_tokens", 0) or 0,
            output_tokens=usage.get("completion_tokens", 0) or 0,
            cache_read_tokens=details.get("cached_tokens", 0) or 0,
            latency_ms=latency_ms,
        ),
        request_id=rid,
        model=body.get("model") or model,
        stop_reason=stop,
    )
    if stop is StopReason.REFUSAL:
        err = ContentFilteredError(
            f"provider refused the request (finish_reason={finish})", request_id=rid
        )
        err.completion = completion
        raise err
    return completion


def classify_response(response: httpx.Response) -> RetryableError | TerminalError:
    """An HTTP error status onto the course's two error families."""
    status = response.status_code
    request_id = response.headers.get("x-request-id")
    message = f"HTTP {status}: {_error_message(response)}"
    if status in RETRYABLE_STATUS or status >= 500:
        return RetryableError(
            message, request_id=request_id, retry_after_s=_retry_after(response), status=status
        )
    return TerminalError(message, request_id=request_id, status=status, code=_error_code(response))


def classify_transport(exc: Exception, *, timeout_s: float) -> RetryableError | TerminalError:
    if isinstance(exc, httpx.TimeoutException):
        return RequestTimeout(f"request timed out ({exc})", timeout_s=timeout_s)
    if isinstance(exc, httpx.TransportError):
        return RetryableError(str(exc))
    return TerminalError(str(exc))


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if error:
        return str(error)
    return response.text[:200]


def _error_code(response: httpx.Response) -> str | None:
    """The vendor's error code from the body: OpenAI and LiteLLM send `error.code`
    (`model_not_found`), Azure `error.code` (`DeploymentNotFound`), Google a list whose first
    item carries `error.status` (`NOT_FOUND`)."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, list) and payload:
        payload = payload[0]
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    # LiteLLM answers a model name it has no route for with 400, code "400" and type
    # `invalid_request_error`; only the message says so. That is a missing model, what a
    # fallback is for, not a malformed request.
    message = str(error.get("message") or "")
    if "Invalid model name" in message or "no healthy deployments" in message.lower():
        return "model_not_found"
    for field in ("code", "status", "type"):
        value = error.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class OpenAICompatProvider:
    """One chat completions endpoint. `token_source` is awaited before every call when
    the bearer is short-lived (a Google access token); `api_key` is the static form."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        token_source: TokenSource | None = None,
        timeout_s: float = 60.0,
        name: str = "openai-compat",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.endpoint = base_url.rstrip("/")
        self._api_key = api_key
        self._token_source = token_source
        self._timeout_s = timeout_s
        self._client = httpx.AsyncClient(
            base_url=self.endpoint, timeout=timeout_s, transport=transport
        )

    async def _headers(self) -> dict[str, str]:
        token = self._api_key
        if self._token_source is not None:
            token = await self._token_source()
        return {"authorization": f"Bearer {token}"} if token else {}

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
    ) -> Completion:
        body = self._build(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        headers = await self._headers()
        started = time.perf_counter()
        try:
            response = await self._client.post("chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise classify_transport(exc, timeout_s=self._timeout_s) from exc
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            raise classify_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TerminalError(f"chat completion was not JSON: {response.text[:200]}") from exc
        return from_vendor_response(
            payload,
            model=model,
            latency_ms=latency_ms,
            request_id=self._request_id(response),
        )

    def _build(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        """The request body; a subclass for an endpoint with its own dialect overrides it."""
        return build_request(
            messages,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def _request_id(self, response: httpx.Response) -> str | None:
        return response.headers.get("x-request-id")

    async def aclose(self) -> None:
        await self._client.aclose()


# ----- the three endpoints ---------------------------------------------------------------


def for_ollama(url: str, *, timeout_s: float = 60.0) -> OpenAICompatProvider:
    return OpenAICompatProvider(base_url=url, timeout_s=timeout_s, name="ollama")


def for_gateway(url: str, key: str | None, *, timeout_s: float = 60.0) -> OpenAICompatProvider:
    return OpenAICompatProvider(base_url=url, api_key=key, timeout_s=timeout_s, name="gateway")


def google_maas_url(project: str, region: str) -> str:
    host = (
        "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    )
    return f"https://{host}/v1/projects/{project}/locations/{region}/endpoints/openapi"


def for_google_maas(
    project: str,
    region: str,
    *,
    timeout_s: float = 60.0,
    token_source: TokenSource | None = None,
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=google_maas_url(project, region),
        token_source=token_source or GoogleTokenSource(),
        timeout_s=timeout_s,
        name="google-maas",
    )


class GoogleTokenSource:
    """An access token from Application Default Credentials, refreshed when it expires.
    `google-auth` arrives with `anthropic[vertex]`, so no new dependency."""

    _SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

    def __init__(self) -> None:
        self._credentials: Any | None = None
        # Calls run in worker threads; without the lock eight concurrent calls at expiry
        # would refresh eight times against the metadata server.
        self._lock = threading.Lock()

    def _token(self) -> str:
        import google.auth
        from google.auth.transport.requests import Request

        with self._lock:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(scopes=list(self._SCOPES))
            if not self._credentials.valid:
                self._credentials.refresh(Request())
            return self._credentials.token

    async def __call__(self) -> str:
        return await asyncio.to_thread(self._token)

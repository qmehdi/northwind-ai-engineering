"""Errors the client raises. Providers map vendor exceptions onto these two
families, so the retry loop never has to know which vendor it is talking to."""

from __future__ import annotations

# HTTP statuses every provider treats as retryable, whatever the vendor: request timeout,
# conflict (a concurrent update or a model still loading), too early, rate limited. Every
# 5xx is retryable too. One set, so the providers cannot drift apart.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429})

# Vendor error codes that say "this model is not available here", which a fallback model can
# help with. Read from the error body or the SDK's error type, never guessed from the message:
# a 400 whose text happens to mention "model" is our bad request and would be bad on the
# fallback too.
MODEL_UNAVAILABLE_CODES = frozenset(
    {
        # Anthropic Messages API (first party, Bedrock Mantle, Vertex, Foundry)
        "not_found_error",
        # Bedrock (botocore error codes)
        "ResourceNotFoundException",
        "AccessDeniedException",  # model access not granted in this account or region
        # OpenAI-compatible endpoints: OpenAI, LiteLLM, Azure OpenAI, Foundry, Ollama
        "model_not_found",
        "DeploymentNotFound",
        "ModelNotFound",
        # Google (the canonical status of a google.rpc error)
        "NOT_FOUND",
    }
)


class LLMError(Exception):
    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class RetryableError(LLMError):
    """Rate limits, overload, 5xx, timeouts, connection resets."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        retry_after_s: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.retry_after_s = retry_after_s
        self.status = status


class RequestTimeout(RetryableError):
    """The call did not answer within the request timeout. Retryable: the next attempt may."""

    def __init__(self, message: str, *, timeout_s: float, request_id: str | None = None) -> None:
        super().__init__(message, request_id=request_id, status=None)
        self.timeout_s = timeout_s


class CircuitOpenError(RetryableError):
    """Every candidate model for the role has an open circuit: skipped without a call."""

    def __init__(self, message: str, *, models: list[str]) -> None:
        super().__init__(message, status=None)
        self.models = models


class TerminalError(LLMError):
    """Bad request, auth, not found, validation: retrying cannot help. `code` is the vendor's
    own error code or type when the provider read one (`not_found_error`,
    `ResourceNotFoundException`, `model_not_found`)."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.status = status
        self.code = code

    @property
    def model_unavailable(self) -> bool:
        """The model id is wrong, not enabled, or not served here: a 404, or a vendor code in
        `MODEL_UNAVAILABLE_CODES`. What a fallback model is for."""
        return self.status == 404 or (self.code or "") in MODEL_UNAVAILABLE_CODES


class ContentFilteredError(TerminalError):
    """The provider refused the request or the response. `completion` holds what the refusal
    cost when the provider returned usage; the client meters it and sets `cost`."""

    completion = None
    cost = None


class SpendCapExceeded(TerminalError):
    """The client's spend cap would be exceeded by this call."""


class StructuredOutputError(TerminalError):
    """The model could not produce output matching the schema, even after repair."""


class TokenBudgetExceeded(TerminalError):
    """A per-request token budget (`max_total_tokens`) was used up before this call."""


class ResidencyError(TerminalError):
    """The call must stay in a residency zone (an EU account) and the track has no model for
    the role there. Raised before any network: the data never leaves for a model outside it."""

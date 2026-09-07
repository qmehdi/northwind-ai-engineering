"""Errors the client raises. Providers map vendor exceptions onto these two
families, so the retry loop never has to know which vendor it is talking to."""

from __future__ import annotations


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


class TerminalError(LLMError):
    """Bad request, auth, not found, validation: retrying cannot help."""

    def __init__(
        self, message: str, *, request_id: str | None = None, status: int | None = None
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.status = status


class ContentFilteredError(TerminalError):
    """The provider refused the request or the response."""


class SpendCapExceeded(TerminalError):
    """The client's spend cap would be exceeded by this call."""


class StructuredOutputError(TerminalError):
    """The model could not produce output matching the schema, even after repair."""

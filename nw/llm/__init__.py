"""The shared service layer: one client in front of any model provider."""

from nw.llm.breaker import CircuitBreaker
from nw.llm.client import LLMClient
from nw.llm.cost import CostMeter
from nw.llm.errors import (
    CircuitOpenError,
    ContentFilteredError,
    LLMError,
    RequestTimeout,
    RetryableError,
    SpendCapExceeded,
    StructuredOutputError,
    TerminalError,
    TokenBudgetExceeded,
)
from nw.llm.prompts import Prompt, register
from nw.llm.provider import LLMProvider
from nw.llm.retry import RetryPolicy
from nw.llm.types import (
    Completion,
    Message,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "Completion",
    "ContentFilteredError",
    "CostMeter",
    "LLMClient",
    "LLMError",
    "LLMProvider",
    "Message",
    "Prompt",
    "RequestTimeout",
    "RetryPolicy",
    "RetryableError",
    "register",
    "SpendCapExceeded",
    "StopReason",
    "StructuredOutputError",
    "TerminalError",
    "TokenBudgetExceeded",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
]

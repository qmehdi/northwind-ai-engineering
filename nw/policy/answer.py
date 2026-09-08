"""Answer a question from retrieved policy chunks, with citations enforced in code.

Three rules the model does not get to break:
1. If retrieval is weak, refuse. Do not answer from parametric memory.
2. Every citation must be a chunk ID that was actually in the context.
3. The answer is structured, validated, and repaired once, through LLMClient.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nw.config import ModelRole
from nw.llm import LLMClient
from nw.policy.retrieval import Retrieved

REFUSAL = "I cannot answer that from Northwind's current policies. Please contact support."


class Draft(BaseModel):
    """What the model returns. Validated, then checked against the context."""

    answer: str
    citations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    answerable: bool = True


class Answer(BaseModel):
    text: str
    citations: list[str]
    confidence: float
    refused: bool
    reason: str | None = None
    context_ids: list[str]
    dropped_citations: list[str] = Field(default_factory=list)


SYSTEM = """You answer questions about Northwind Cloud's policies for a support agent.
Use only the numbered context passages. Quote numbers, limits and dates exactly as written.
Cite every passage you relied on by its id, and cite nothing else.
If the passages do not contain the answer, set answerable to false and say so briefly.
Never invent a policy, a number or a date."""


def build_context(retrieved: list[Retrieved], budget_tokens: int = 2500) -> tuple[str, list[str]]:
    """Pack the highest ranked chunks that fit the token budget, best first."""
    parts, ids, used = [], [], 0
    for r in retrieved:
        if used + r.chunk.tokens > budget_tokens:
            continue
        parts.append(
            f"[{r.chunk.id}] (effective {r.chunk.effective}, {r.chunk.section})\n{r.chunk.text}"
        )
        ids.append(r.chunk.id)
        used += r.chunk.tokens
    return "\n\n".join(parts), ids


def weak_retrieval(retrieved: list[Retrieved], min_score: float) -> bool:
    return not retrieved or retrieved[0].score < min_score


async def answer(
    client: LLMClient,
    question: str,
    retrieved: list[Retrieved],
    *,
    min_score: float = 0.0,
    role: ModelRole = ModelRole.WORKHORSE,
) -> Answer:
    raise NotImplementedError("Step 5: refuse when weak, answer, validate citations")


def as_dict(a: Answer) -> dict[str, Any]:
    return a.model_dump()

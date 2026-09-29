"""Llama Guard as the gateway's input guardrail, the open-source counterpart of Bedrock
Guardrails and Model Armor (ADR 0011).

LiteLLM loads this file from the directory of its config (`guardrail: llama_guard.LlamaGuard`).
Before every completion the last user turn goes to `llama-guard3:1b` on Ollama through the
gateway's own `guard` deployment; a verdict that starts with `unsafe` blocks the call with a
400 that names the category (S1 to S14 in the Llama Guard 3 taxonomy). The guard model's own
calls are never screened, and a guard that cannot answer fails closed unless
`NW_GUARD_FAIL_OPEN=1`, because a guardrail that silently disappears is worse than an outage.
"""

from __future__ import annotations

import os
from typing import Any

import litellm
from fastapi import HTTPException
from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_guardrail import CustomGuardrail


class LlamaGuard(CustomGuardrail):
    def __init__(self, guard_model: str = "guard", **kwargs: Any) -> None:
        self.guard_model = guard_model
        self.fail_open = os.environ.get("NW_GUARD_FAIL_OPEN", "0") == "1"
        super().__init__(**kwargs)

    @staticmethod
    def _last_user_text(data: dict) -> str | None:
        for message in reversed(data.get("messages") or []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                return "\n".join(p for p in parts if p)
        return None

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str):
        if call_type not in ("completion", "text_completion", "anthropic_messages"):
            return data
        if data.get("model") == self.guard_model:
            return data
        text = self._last_user_text(data)
        if not text:
            return data
        try:
            verdict = await litellm.acompletion(
                model=self.guard_model,
                messages=[{"role": "user", "content": text}],
                max_tokens=20,
                temperature=0,
                metadata={"nw_guard": True},
            )
            answer = (verdict.choices[0].message.content or "").strip().lower()
        except Exception as exc:  # noqa: BLE001
            verbose_proxy_logger.warning("llama-guard unavailable: %s", exc)
            if self.fail_open:
                return data
            raise HTTPException(
                status_code=503,
                detail={"error": "guardrail unavailable", "guardrail": "llama-guard"},
            ) from exc
        if answer.startswith("unsafe"):
            category = answer.split()[1] if len(answer.split()) > 1 else "unknown"
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Violated guardrail policy",
                    "guardrail": "llama-guard",
                    "category": category.upper(),
                },
            )
        return data

"""Input screening with the track's native service, in front of the loop.

AWS: Amazon Bedrock Guardrails, `ApplyGuardrail` on the customer text.
GCP: Model Armor, `sanitizeUserPrompt` on a template.

Both are optional: with no template configured, `screen()` allows everything
and says so, so the Session path runs without them and the Reference stack
turns them on with two environment variables. A blocked ticket never reaches
the model; the trajectory records the decision and the reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from nw.logging import get_logger, log_fields

log = get_logger("nw.agent.screen")


@dataclass
class Verdict:
    allowed: bool
    screener: str
    reason: str | None = None
    detail: dict[str, Any] | None = None


class Screener:
    name = "none"

    def screen(self, text: str) -> Verdict:
        return Verdict(True, self.name)


class BedrockGuardrail(Screener):
    """ApplyGuardrail with source INPUT. Request and response shapes verified against the
    installed botocore model (bedrock-runtime 2023-09-30)."""

    name = "bedrock-guardrail"

    def __init__(self, guardrail_id: str, version: str, region: str, client: Any = None) -> None:
        import boto3

        self.guardrail_id, self.version = guardrail_id, version
        self.client = client or boto3.client("bedrock-runtime", region_name=region)

    def screen(self, text: str) -> Verdict:
        r = self.client.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion=self.version,
            source="INPUT",
            content=[{"text": {"text": text}}],
            outputScope="INTERVENTIONS",
        )
        blocked = r.get("action") == "GUARDRAIL_INTERVENED"
        log.info("guardrail", extra=log_fields(action=r.get("action"), units=r.get("usage")))
        return Verdict(
            not blocked,
            self.name,
            r.get("actionReason") if blocked else None,
            {"assessments": r.get("assessments")},
        )


class ModelArmor(Screener):
    """Model Armor sanitizeUserPrompt over REST. Endpoint, body and response fields as on the
    sanitize-prompts-responses page (fetched 2026-09-08); needs roles/modelarmor.user."""

    name = "model-armor"

    def __init__(self, template: str, *, session: Any = None) -> None:
        # template: projects/{p}/locations/{l}/templates/{t}
        self.template = template
        parts = template.split("/")
        self.location = parts[3]
        self._session = session

    def _http(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._session = AuthorizedSession(creds)
        return self._session

    def screen(self, text: str) -> Verdict:
        url = f"https://modelarmor.{self.location}.rep.googleapis.com/v1/{self.template}:sanitizeUserPrompt"
        r = self._http().post(url, json={"userPromptData": {"text": text}}, timeout=20)
        r.raise_for_status()
        result = r.json().get("sanitizationResult", {})
        matched = result.get("filterMatchState") == "MATCH_FOUND"
        hits = [k for k, v in (result.get("filterResults") or {}).items() if _matched(v)]
        log.info(
            "model armor", extra=log_fields(match=result.get("filterMatchState"), filters=hits)
        )
        return Verdict(not matched, self.name, ", ".join(hits) if matched else None, result)


def _matched(v: Any) -> bool:
    text = str(v)
    return "MATCH_FOUND" in text and "NO_MATCH_FOUND" not in text


def from_env(env: dict[str, str] | None = None) -> Screener:
    e = env if env is not None else dict(os.environ)
    if e.get("NW_GUARDRAIL_ID"):
        return BedrockGuardrail(
            e["NW_GUARDRAIL_ID"],
            e.get("NW_GUARDRAIL_VERSION", "DRAFT"),
            e.get("NW_AWS_REGION", "us-east-1"),
        )
    if e.get("NW_MODEL_ARMOR_TEMPLATE"):
        return ModelArmor(e["NW_MODEL_ARMOR_TEMPLATE"])
    return Screener()

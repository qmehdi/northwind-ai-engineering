"""Input screening with the track's native service, in front of the loop.

AWS: Amazon Bedrock Guardrails, `ApplyGuardrail` on the customer text.
GCP: Model Armor, `sanitizeUserPrompt` on a template.
Azure: Azure AI Content Safety Prompt Shields, `text:shieldPrompt` on the Foundry resource.

All are optional: with no template or endpoint configured, `screen()` allows everything
and says so, so a laptop runs without them and the platform deploy
turns them on with environment variables. A blocked ticket never reaches
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


class AzurePromptShields(Screener):
    """Prompt Shields over REST: `POST {endpoint}/contentsafety/text:shieldPrompt
    ?api-version=2024-09-01` with `{"userPrompt": ..., "documents": [...]}`, answered with
    `userPromptAnalysis.attackDetected` and one `documentsAnalysis[].attackDetected` per
    document (learn.microsoft.com/azure/ai-services/content-safety/quickstart-jailbreak,
    updated 2026-06-05, fetched 2026-09-29; 2024-09-01 is the current GA version).

    The endpoint is the Foundry resource's custom subdomain,
    `https://<resource>.cognitiveservices.azure.com` (Content Safety is part of the AIServices
    account; Entra ID needs the custom subdomain, not a regional endpoint). Auth is Entra ID by
    default, a bearer for `https://cognitiveservices.azure.com/.default` from
    `DefaultAzureCredential`, which needs Cognitive Services User on the resource; a key goes in
    `Ocp-Apim-Subscription-Key` and works only where local auth is enabled."""

    name = "azure-prompt-shields"
    API_VERSION = "2024-09-01"
    SCOPE = "https://cognitiveservices.azure.com/.default"

    def __init__(
        self,
        endpoint: str,
        *,
        key: str | None = None,
        credential: Any = None,
        http: Any = None,
        timeout_s: float = 20.0,
    ) -> None:
        self.url = f"{endpoint.rstrip('/')}/contentsafety/text:shieldPrompt"
        self.key = key
        self._http = http
        self._timeout = timeout_s
        self._tokens: Any = None
        if not key:
            from nw.llm.providers.azure_foundry import EntraTokenSource

            self._tokens = EntraTokenSource(self.SCOPE, credential=credential)

    def _headers(self) -> dict[str, str]:
        if self.key:
            return {"Ocp-Apim-Subscription-Key": self.key}
        return {"authorization": f"Bearer {self._tokens.sync()}"}

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def screen(self, text: str, documents: list[str] | None = None) -> Verdict:
        r = self._client().post(
            self.url,
            params={"api-version": self.API_VERSION},
            json={"userPrompt": text, "documents": list(documents or [])},
            headers=self._headers(),
        )
        r.raise_for_status()
        body = r.json()
        prompt_attack = bool((body.get("userPromptAnalysis") or {}).get("attackDetected"))
        doc_attacks = [
            i for i, d in enumerate(body.get("documentsAnalysis") or []) if d.get("attackDetected")
        ]
        hits = (["user prompt attack"] if prompt_attack else []) + [
            f"document attack in document {i}" for i in doc_attacks
        ]
        log.info("prompt shields", extra=log_fields(attack=bool(hits), hits=hits))
        return Verdict(not hits, self.name, ", ".join(hits) if hits else None, body)


def _matched(v: Any) -> bool:
    text = str(v)
    return "MATCH_FOUND" in text and "NO_MATCH_FOUND" not in text


def _track(e: dict[str, str], use_settings: bool) -> str:
    """NW_TRACK from the environment, or from `.env` through the settings for a process that
    was not handed an explicit environment."""
    if e.get("NW_TRACK") or not use_settings:
        return e.get("NW_TRACK", "")
    from nw.config import settings

    return settings().track.value


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
    if e.get("NW_AZURE_CONTENT_SAFETY_ENDPOINT") and _track(e, env is None) == "azure":
        return AzurePromptShields(
            e["NW_AZURE_CONTENT_SAFETY_ENDPOINT"], key=e.get("NW_AZURE_CONTENT_SAFETY_KEY") or None
        )
    return Screener()

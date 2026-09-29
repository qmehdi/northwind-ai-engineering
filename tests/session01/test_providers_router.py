"""Provider selection: the router's dispatch by model id, `make_provider` by track and
gateway, the defaults of ADR 0010, and preflight's round trips under a fake transport."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from nw.config import DEFAULT_MODELS, ModelRole, ProviderMode, Settings, Track
from nw.llm.providers import RoleRouter, describe_route, is_fake, make_provider
from nw.llm.providers.fake import FakeProvider
from nw.llm.providers.openai_compat import OpenAICompatProvider
from nw.llm.types import Message

pytestmark = pytest.mark.session01

ROOT = Path(__file__).resolve().parents[2]


def _settings(**kw) -> Settings:
    kw.setdefault("provider", ProviderMode.AUTO)
    return Settings(_env_file=None, **kw)


# ----- defaults ------------------------------------------------------------------------


def test_defaults_follow_adr_0010():
    aws, gcp, local = (
        DEFAULT_MODELS[Track.AWS],
        DEFAULT_MODELS[Track.GCP],
        DEFAULT_MODELS[Track.LOCAL],
    )
    assert aws[ModelRole.WORKHORSE] == "openai.gpt-oss-120b-1:0"
    assert aws[ModelRole.ECONOMY] == "us.amazon.nova-micro-v1:0"
    assert aws[ModelRole.JUDGE] == "anthropic.claude-opus-5"
    assert gcp[ModelRole.WORKHORSE] == "openai/gpt-oss-120b-maas"
    assert (
        gcp[ModelRole.ECONOMY] == "openai/gpt-oss-120b-maas"
    )  # gpt-oss-20b-maas retires 2026-10-21
    assert gcp[ModelRole.JUDGE] == "claude-opus-5"
    assert local[ModelRole.WORKHORSE] == "gpt-oss:20b"  # 120b is 65 GB, gpu profile only
    assert local[ModelRole.ECONOMY] == "gpt-oss:20b"
    assert local[ModelRole.JUDGE] == "fake-judge"


def test_fake_mode_resolves_every_role_to_fake_models_on_any_track():
    for track in Track:
        s = _settings(track=track, provider=ProviderMode.FAKE, gcp_project="p")
        assert [s.model_for(r) for r in ModelRole] == [
            "fake-workhorse",
            "fake-judge",
            "fake-economy",
        ]
        assert isinstance(make_provider(s), FakeProvider)
        assert not s.uses_gateway


def test_local_judge_becomes_claude_when_a_cloud_key_is_present():
    assert _settings(track=Track.LOCAL).model_for(ModelRole.JUDGE) == "fake-judge"
    assert (
        _settings(track=Track.LOCAL, anthropic_api_key="k").model_for(ModelRole.JUDGE)
        == "claude-opus-5"
    )
    assert (
        _settings(track=Track.LOCAL, gateway_url="http://gw").model_for(ModelRole.JUDGE)
        == "claude-opus-5"
    )
    assert (
        _settings(track=Track.LOCAL, model_judge="mine", anthropic_api_key="k").model_for(
            ModelRole.JUDGE
        )
        == "mine"
    )


def test_new_settings_are_described_and_secrets_redacted():
    rows = {
        r["name"]: r
        for r in _settings(
            tenant="t1", environment="dev", gateway_url="http://gw", gateway_key="sk"
        ).describe(env={}, env_file=None)
    }
    assert rows["tenant"]["value"] == "t1" and rows["tenant"]["source"] == "init"
    assert rows["gateway_key"]["secret"] and rows["gateway_key"]["value"] == "set"
    assert rows["anthropic_api_key"]["secret"] and rows["anthropic_api_key"]["value"] == "unset"
    assert rows["provider"]["value"] == "auto"


# ----- the router ----------------------------------------------------------------------


async def test_router_dispatches_by_model_id():
    claude, other = FakeProvider(["from claude"]), FakeProvider(["from other"])
    router = RoleRouter([(lambda m: "claude" in m, claude)], default=other)
    assert router.route("anthropic.claude-opus-5") is claude
    assert router.route("openai.gpt-oss-120b-1:0") is other
    c = await router.complete([Message.user("q")], model="claude-opus-5")
    assert c.text == "from claude" and claude.calls[0]["model"] == "claude-opus-5"
    c = await router.complete([Message.user("q")], model="gpt-oss:20b", max_tokens=3)
    assert c.text == "from other" and other.calls[0]["max_tokens"] == 3
    assert router.providers == [claude, other]
    assert router.describe("claude-opus-5") == "fake"


# ----- make_provider -------------------------------------------------------------------


def test_aws_routes_claude_to_the_sdk_and_the_rest_to_converse():
    s = _settings(track=Track.AWS, aws_profile=None)
    p = make_provider(s)
    assert isinstance(p, RoleRouter)
    assert type(p.route(s.model_for(ModelRole.JUDGE))).__name__ == "BedrockProvider"
    assert type(p.route(s.model_for(ModelRole.WORKHORSE))).__name__ == "BedrockConverseProvider"
    assert type(p.route(s.model_for(ModelRole.ECONOMY))).__name__ == "BedrockConverseProvider"
    assert describe_route(p, s.model_for(ModelRole.ECONOMY)) == (
        "bedrock-converse at https://bedrock-runtime.us-east-1.amazonaws.com"
    )


def test_gcp_routes_claude_to_vertex_and_the_rest_to_the_managed_api():
    s = _settings(track=Track.GCP, gcp_project="proj")
    p = make_provider(s)
    assert type(p.route(s.model_for(ModelRole.JUDGE))).__name__ == "VertexProvider"
    maas = p.route(s.model_for(ModelRole.WORKHORSE))
    assert isinstance(maas, OpenAICompatProvider) and maas.name == "google-maas"
    assert maas.endpoint == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/proj/locations/us-central1"
        "/endpoints/openapi"
    )
    with pytest.raises(ValueError):
        make_provider(_settings(track=Track.GCP))


def test_local_routes_to_ollama_with_the_fake_judge():
    s = _settings(track=Track.LOCAL)
    p = make_provider(s)
    assert isinstance(p.route("fake-judge"), FakeProvider) and is_fake("fake-judge")
    ollama = p.route("gpt-oss:120b")
    assert ollama.name == "ollama" and ollama.endpoint == "http://localhost:11434/v1"
    assert type(p.route("claude-opus-5")) is OpenAICompatProvider  # no key: nothing else to try

    with_key = make_provider(_settings(track=Track.LOCAL, anthropic_api_key="k"))
    assert type(with_key.route("claude-opus-5")).__name__ == "AnthropicApiProvider"


async def test_gateway_takes_every_role_with_bearer_and_model_name():
    s = _settings(track=Track.AWS, gateway_url="http://gw.invalid/v1", gateway_key="sk-tenant")
    assert s.uses_gateway
    p = make_provider(s)
    assert isinstance(p, OpenAICompatProvider) and p.name == "gateway"
    assert describe_route(p, "anything") == "gateway at http://gw.invalid/v1"

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "m",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    p._client = httpx.AsyncClient(base_url=p.endpoint, transport=httpx.MockTransport(handler))
    for role in ModelRole:
        await p.complete([Message.user("q")], model=s.model_for(role))
    assert [json.loads(r.content)["model"] for r in seen] == [s.model_for(r) for r in ModelRole]
    assert {r.headers["authorization"] for r in seen} == {"Bearer sk-tenant"}
    assert {str(r.url) for r in seen} == {"http://gw.invalid/v1/chat/completions"}


# ----- preflight -----------------------------------------------------------------------


def _preflight():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through here
    spec.loader.exec_module(module)
    return module


async def test_preflight_round_trips_once_per_role_under_a_fake_transport():
    pf = _preflight()
    s = _settings(track=Track.LOCAL, ollama_url="http://ollama.invalid/v1")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "m",
                "choices": [{"finish_reason": "stop", "message": {"content": "ready"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    provider = RoleRouter(
        [(is_fake, FakeProvider())],
        default=OpenAICompatProvider(
            base_url=s.ollama_url, transport=httpx.MockTransport(handler), name="ollama"
        ),
    )
    checks = await pf.check_round_trips(s, provider)
    by_name = {c.name: c for c in checks}
    assert all(by_name[f"model {r.value}"].ok for r in ModelRole)
    assert seen == ["gpt-oss:20b", "gpt-oss:20b"]  # the fake judge never touches the wire
    # Ollama is free; the fake judge is priced like the real Judge (50 + 20 tokens at Opus rates)
    assert by_name["spend"].detail == "0.00075 USD"

    routes = {c.name: c.detail for c in pf.check_routes(s, provider)}
    assert routes["route workhorse"] == (
        "gpt-oss:20b via ollama at http://ollama.invalid/v1, gateway=no"
    )
    assert routes["route judge"] == "fake-judge via fake, gateway=no"


async def test_preflight_reports_a_failed_round_trip():
    pf = _preflight()
    s = _settings(track=Track.LOCAL)

    def handler(request):
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    provider = OpenAICompatProvider(
        base_url=s.ollama_url, transport=httpx.MockTransport(handler), name="ollama"
    )
    checks = {c.name: c for c in await pf.check_round_trips(s, provider)}
    assert not checks["model workhorse"].ok and "TerminalError" in checks["model workhorse"].detail


def test_preflight_route_lines_say_when_the_gateway_is_in_the_path():
    pf = _preflight()
    s = _settings(
        track=Track.GCP, gcp_project="p", gateway_url="http://gw.invalid/v1", gateway_key="k"
    )
    provider, failure = pf.make_provider_check(s)
    assert failure is None
    routes = {c.name: c.detail for c in pf.check_routes(s, provider)}
    assert routes["route judge"] == "claude-opus-5 via gateway at http://gw.invalid/v1, gateway=yes"

    provider, failure = pf.make_provider_check(_settings(track=Track.GCP))
    assert provider is None and not failure.ok and "NW_GCP_PROJECT" in failure.detail


# ----- preflight on azure ----------------------------------------------------------------------

AZ_ENDPOINT = "https://northwind-foundry.services.ai.azure.com"
AZ_APIM = "https://northwind-apim.azure-api.net"


class FakeCredential:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.scopes: list[str] = []

    def get_token(self, scope):
        import time
        from types import SimpleNamespace

        self.scopes.append(scope)
        if self.fail:
            raise self.fail
        return SimpleNamespace(token="t", expires_on=time.time() + 3600)


def _azure(**kw) -> Settings:
    kw.setdefault("track", Track.AZURE)
    kw.setdefault("azure_foundry_endpoint", AZ_ENDPOINT)
    kw.setdefault("azure_foundry_project", "northwind")
    return _settings(**kw)


def test_preflight_azure_track_line_and_entra_identity():
    pf = _preflight()
    cred = FakeCredential()
    s = _azure(azure_apim_gateway_url=AZ_APIM, gateway_key="sub")
    checks = {c.name: c for c in pf.check_track_auth("azure", s, azure_credential=cred)}
    assert checks["azure track"].ok
    assert checks["azure track"].detail == (
        f"foundry {AZ_ENDPOINT}, project northwind, region eastus2, apim yes, {AZ_APIM}"
    )
    assert checks["azure identity"].ok and checks["azure identity"].required
    assert "https://ai.azure.com/.default" in checks["azure identity"].detail
    assert cred.scopes == ["https://ai.azure.com/.default"]


def test_preflight_azure_identity_fails_without_a_login_and_passes_with_a_key():
    pf = _preflight()
    s = _azure()
    bad = {
        c.name: c
        for c in pf.check_azure(s, FakeCredential(RuntimeError("DefaultAzureCredential failed")))
    }
    assert not bad["azure identity"].ok and bad["azure identity"].required
    assert "az login" in bad["azure identity"].detail
    assert "apim no" in bad["azure track"].detail
    cred = FakeCredential()
    keyed = {c.name: c for c in pf.check_azure(_azure(azure_foundry_key="k"), cred)}
    assert keyed["azure identity"].ok and cred.scopes == [], "a key needs no token"


def test_preflight_azure_without_endpoint_or_gateway_fails_the_track_line():
    pf = _preflight()
    checks = {
        c.name: c for c in pf.check_azure(_azure(azure_foundry_endpoint=None), FakeCredential())
    }
    assert not checks["azure track"].ok and "unset" in checks["azure track"].detail


def test_preflight_azure_routes_say_gateway_yes_behind_apim():
    pf = _preflight()
    s = _azure(azure_apim_gateway_url=AZ_APIM, gateway_key="sub")
    routes = {c.name: c.detail for c in pf.check_routes(s, make_provider(s))}
    assert routes["route workhorse"].startswith(f"gpt-oss-120b via apim-openai at {AZ_APIM}")
    assert routes["route judge"].startswith(f"claude-opus-5 via apim-claude at {AZ_APIM}")
    assert all(d.endswith("gateway=yes") for d in routes.values())
    direct = _azure()
    routes = {c.name: c.detail for c in pf.check_routes(direct, make_provider(direct))}
    assert all(d.endswith("gateway=no") for d in routes.values())


def test_preflight_azure_tools_check_az(monkeypatch):
    pf = _preflight()
    monkeypatch.setattr(pf, "_run", lambda cmd, timeout=30: (0, "2.79.0"))
    (az,) = pf.check_track_tools("azure")
    assert az.name == "az" and az.ok and not az.required and az.detail == "2.79.0"

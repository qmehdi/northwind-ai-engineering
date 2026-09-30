"""The Azure track's model providers (ADR 0013): the Foundry v1 OpenAI-compatible endpoint and
Claude through the Anthropic SDK's Foundry client, direct or behind API Management. Tested
against an httpx mock transport and a fake Anthropic client. No network, no Azure login."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from nw.config import DEFAULT_MODELS, ModelRole, ProviderMode, Settings, Track, is_secret
from nw.llm.providers import RoleRouter, describe_route, make_provider
from nw.llm.providers import azure_foundry as az
from nw.llm.types import Message, ToolSpec

pytestmark = pytest.mark.session01

ENDPOINT = "https://northwind-foundry.services.ai.azure.com"


def _settings(**kw) -> Settings:
    kw.setdefault("provider", ProviderMode.AUTO)
    kw.setdefault("track", Track.AZURE)
    return Settings(_env_file=None, **kw)


def _ok(model: str = "gpt-oss-120b") -> dict:
    return {
        "id": "chatcmpl-az",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json=_ok(body["model"]), headers={"apim-request-id": "apim-1"})

    @property
    def last(self) -> tuple[httpx.Request, dict]:
        r = self.requests[-1]
        return r, json.loads(r.content)


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return f"entra-{self.calls}"


# ----- defaults and settings -----------------------------------------------------------------


def test_azure_defaults_are_the_verified_foundry_deployments():
    azure = DEFAULT_MODELS[Track.AZURE]
    assert azure[ModelRole.WORKHORSE] == "gpt-oss-120b"
    assert azure[ModelRole.ECONOMY] == "mistral-small-2503"  # gpt-oss-20b is managed compute only
    assert azure[ModelRole.JUDGE] == "claude-opus-5"  # the calibrated judge on every cloud


def test_azure_economy_is_cheaper_than_the_workhorse_on_both_sides():
    """The cheap-first router only saves money if Economy costs less per token than the
    Workhorse; gpt-5.4-nano (0.20 and 1.25) failed this against gpt-oss-120b (0.15 and 0.60)."""
    from nw.llm.prices import price_for

    azure = DEFAULT_MODELS[Track.AZURE]
    (economy, e_fallback), (workhorse, w_fallback) = (
        price_for(azure[ModelRole.ECONOMY]),
        price_for(azure[ModelRole.WORKHORSE]),
    )
    judge, j_fallback = price_for(azure[ModelRole.JUDGE])
    assert not (e_fallback or w_fallback or j_fallback), "every Azure role has a listed price"
    assert economy.input_per_mtok < workhorse.input_per_mtok
    assert economy.output_per_mtok < workhorse.output_per_mtok
    assert (workhorse.input_per_mtok, workhorse.output_per_mtok) == (0.15, 0.60)
    assert (judge.input_per_mtok, judge.output_per_mtok) == (5.00, 25.00)
    assert not az.is_reasoning(azure[ModelRole.ECONOMY]), "Economy takes a temperature"


def test_azure_settings_are_described_and_only_the_key_is_secret():
    s = _settings(
        azure_foundry_endpoint=ENDPOINT, azure_key_vault="nwnorthwindkv", azure_foundry_key="k"
    )
    rows = {r["name"]: r for r in s.describe(env={}, env_file=None)}
    assert rows["azure_key_vault"]["value"] == "nwnorthwindkv"
    assert rows["azure_key_vault"]["secret"] is False
    assert rows["azure_foundry_key"]["value"] == "set" and rows["azure_foundry_key"]["secret"]
    assert rows["azure_location"]["value"] == "eastus2"
    assert is_secret("azure_foundry_key") and not is_secret("azure_key_vault")


# ----- translation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [
        ("gpt-5.4-nano", True),
        ("gpt-5-nano", True),
        ("o4-mini", True),
        ("gpt-oss-120b", False),
        ("gpt-4.1-nano", False),
        ("DeepSeek-V3.2", False),
    ],
)
def test_reasoning_families(model, reasoning):
    assert az.is_reasoning(model) is reasoning


def test_request_uses_max_completion_tokens_and_drops_temperature_for_reasoning_models():
    tools = [ToolSpec(name="lookup", description="d", input_schema={"type": "object"})]
    nano = az.build_azure_request(
        [Message.user("q")],
        model="gpt-5.4-nano",
        system="s",
        tools=tools,
        max_tokens=64,
        temperature=0.2,
    )
    assert nano["max_completion_tokens"] == 64 and "max_tokens" not in nano
    assert "temperature" not in nano
    assert nano["tools"][0]["function"]["name"] == "lookup"
    oss = az.build_azure_request(
        [Message.user("q")],
        model="gpt-oss-120b",
        system=None,
        tools=None,
        max_tokens=32,
        temperature=0.2,
    )
    assert oss["max_completion_tokens"] == 32 and oss["temperature"] == 0.2


@pytest.mark.parametrize(
    "given",
    [
        ENDPOINT,
        ENDPOINT + "/",
        ENDPOINT + "/api/projects/northwind",
        ENDPOINT + "/openai/v1",
        ENDPOINT + "/anthropic/",
    ],
)
def test_resource_root_accepts_every_foundry_url(given):
    assert az.resource_root(given) == ENDPOINT
    assert az.openai_url(az.resource_root(given)) == ENDPOINT + "/openai/v1"
    assert az.anthropic_url(az.resource_root(given)) == ENDPOINT + "/anthropic"


def test_resource_root_rejects_a_bare_name():
    with pytest.raises(ValueError):
        az.resource_root("northwind-foundry")


# ----- the wire --------------------------------------------------------------------------------


async def test_entra_token_is_a_bearer_on_the_v1_path():
    rec, tokens = Recorder(), FakeTokens()
    provider = az.AzureOpenAIProvider(
        base_url=az.openai_url(ENDPOINT),
        token_source=tokens,
        transport=httpx.MockTransport(rec),
    )
    c1 = await provider.complete([Message.user("hi")], model="gpt-oss-120b", max_tokens=16)
    await provider.complete([Message.user("again")], model="gpt-oss-120b")
    request, body = rec.last
    assert str(request.url) == ENDPOINT + "/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer entra-2" and tokens.calls == 2
    assert "api-key" not in request.headers
    assert body["model"] == "gpt-oss-120b" and "api-version" not in str(request.url)
    assert c1.text == "ok" and c1.request_id == "chatcmpl-az"


async def test_api_key_goes_in_the_api_key_header():
    rec = Recorder()
    provider = az.AzureOpenAIProvider(
        base_url=az.openai_url(ENDPOINT), api_key="kv-secret", transport=httpx.MockTransport(rec)
    )
    await provider.complete([Message.user("hi")], model="gpt-5.4-nano", temperature=0.5)
    request, body = rec.last
    assert request.headers["api-key"] == "kv-secret"
    assert "authorization" not in request.headers
    assert "temperature" not in body


async def test_apim_route_sends_the_subscription_key_to_both_paths():
    rec = Recorder()
    captured: dict = {}

    class FakeMessages:
        async def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(
                id="msg_1",
                model=kw["model"],
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text="judged")],
                usage=SimpleNamespace(input_tokens=3, output_tokens=1),
            )

    openai, claude = az.for_apim(
        "https://northwind-apim.azure-api.net/",
        "tenant-sub-key",
        transport=httpx.MockTransport(rec),
        claude_client=SimpleNamespace(messages=FakeMessages()),
    )
    await openai.complete([Message.user("hi")], model="gpt-oss-120b")
    request, _ = rec.last
    assert str(request.url) == "https://northwind-apim.azure-api.net/openai/v1/chat/completions"
    assert request.headers["api-key"] == "tenant-sub-key"
    assert claude.endpoint == "https://northwind-apim.azure-api.net/anthropic"
    out = await claude.complete([Message.user("grade")], model="claude-opus-5", temperature=0.1)
    assert out.text == "judged"
    assert captured["model"] == "claude-opus-5" and "temperature" not in captured


def test_claude_client_is_the_foundry_client_with_entra_or_key():
    from anthropic import AsyncAnthropicFoundry

    keyed = az.FoundryClaudeProvider(base_url=az.anthropic_url(ENDPOINT), api_key="k")
    assert isinstance(keyed._client, AsyncAnthropicFoundry)
    assert str(keyed._client.base_url).rstrip("/") == ENDPOINT + "/anthropic"
    assert keyed._client.max_retries == 0
    entra = az.FoundryClaudeProvider(
        base_url=az.anthropic_url(ENDPOINT), token_source=az.EntraTokenSource(credential=object())
    )
    assert isinstance(entra._client, AsyncAnthropicFoundry)


def test_entra_token_source_caches_until_near_expiry():
    import time

    class Cred:
        def __init__(self) -> None:
            self.scopes: list[str] = []

        def get_token(self, scope):
            self.scopes.append(scope)
            return SimpleNamespace(token=f"t{len(self.scopes)}", expires_on=time.time() + 3600)

    cred = Cred()
    source = az.EntraTokenSource(credential=cred)
    assert source.sync() == "t1" and source.sync() == "t1"
    assert cred.scopes == ["https://ai.azure.com/.default"]
    source._token = SimpleNamespace(token="old", expires_on=time.time() + 60)
    assert source.sync() == "t2"


# ----- dispatch --------------------------------------------------------------------------------


def test_azure_routes_claude_to_foundry_messages_and_the_rest_to_v1():
    s = _settings(azure_foundry_endpoint=ENDPOINT + "/api/projects/northwind")
    provider = make_provider(s).default
    assert isinstance(provider, RoleRouter)
    assert describe_route(provider, s.model_for(ModelRole.WORKHORSE)) == (
        f"foundry-openai at {ENDPOINT}/openai/v1"
    )
    assert describe_route(provider, s.model_for(ModelRole.ECONOMY)).startswith("foundry-openai")
    assert describe_route(provider, s.model_for(ModelRole.JUDGE)) == (
        f"foundry-claude at {ENDPOINT}/anthropic"
    )


def test_azure_needs_an_endpoint_or_a_gateway():
    with pytest.raises(ValueError, match="NW_AZURE_FOUNDRY_ENDPOINT"):
        make_provider(_settings())


def test_apim_gateway_takes_the_roles_when_set():
    s = _settings(
        azure_foundry_endpoint=ENDPOINT,
        azure_apim_gateway_url="https://northwind-apim.azure-api.net",
        gateway_key="sub",
    )
    provider = make_provider(s)
    assert describe_route(provider, "gpt-oss-120b").startswith(
        "apim-openai at https://northwind-apim"
    )
    assert describe_route(provider, "claude-opus-5").startswith(
        "apim-claude at https://northwind-apim"
    )


def test_litellm_gateway_still_wins_on_azure():
    s = _settings(
        azure_foundry_endpoint=ENDPOINT,
        azure_apim_gateway_url="https://northwind-apim.azure-api.net",
        gateway_url="http://litellm.invalid/v1",
        gateway_key="k",
    )
    assert (
        describe_route(make_provider(s), "claude-opus-5") == "gateway at http://litellm.invalid/v1"
    )


def test_fake_mode_on_azure_needs_nothing():
    from nw.llm.providers.fake import FakeProvider

    s = _settings(provider=ProviderMode.FAKE)
    assert isinstance(make_provider(s), FakeProvider)
    assert s.model_for(ModelRole.JUDGE) == "fake-judge"


# ----- the APIM gateway from deploy/azure/outputs.json -----------------------------------------

APIM = "https://northwind-apim.azure-api.net"


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    path = tmp_path / "outputs.json"
    path.write_text(
        json.dumps(
            {
                "NW_AZURE_APIM_GATEWAY_URL": {"type": "String", "value": APIM},
                "NW_GATEWAY_URL": "",
            }
        )
    )
    monkeypatch.setenv("NW_AZURE_OUTPUTS", str(path))
    return path


def test_settings_read_the_apim_gateway_from_outputs_on_azure(outputs):
    s = _settings(azure_foundry_endpoint=ENDPOINT, gateway_key="sub")
    assert s.azure_apim_gateway_url == APIM
    assert s.gateway_url is None, "NW_GATEWAY_URL means LiteLLM and stays unset"
    assert s.uses_apim and s.uses_gateway
    assert describe_route(make_provider(s), "gpt-oss-120b").startswith(f"apim-openai at {APIM}")
    row = next(
        r for r in s.describe(env={}, env_file=None) if r["name"] == "azure_apim_gateway_url"
    )
    assert (row["value"], row["source"]) == (APIM, "outputs")


def test_an_explicit_apim_value_wins_and_empty_means_no_apim(outputs):
    assert _settings(
        azure_apim_gateway_url="https://other.azure-api.net"
    ).azure_apim_gateway_url == ("https://other.azure-api.net")
    s = _settings(azure_apim_gateway_url="", azure_foundry_endpoint=ENDPOINT)
    assert not s.uses_apim and not s.uses_gateway
    assert describe_route(make_provider(s), "gpt-oss-120b").startswith("foundry")


def test_outputs_are_read_only_on_the_azure_track(outputs):
    assert _settings(track=Track.LOCAL).azure_apim_gateway_url is None


def test_litellm_on_azure_is_the_gateway_not_apim(outputs):
    s = _settings(gateway_url="http://litellm.invalid/v1", gateway_key="k")
    assert s.uses_gateway and not s.uses_apim


def test_fake_mode_never_uses_a_gateway(outputs):
    s = _settings(provider=ProviderMode.FAKE)
    assert not s.uses_gateway and not s.uses_apim

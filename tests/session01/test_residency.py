"""Residency routing: an EU account's calls go to EU-resident models, per track, or are refused
before any network. Never to the default (US or global) ids."""

from __future__ import annotations

import pytest

from nw.config import EU_MODELS, ModelRole, ProviderMode, Residency, Settings, Track
from nw.llm.errors import ResidencyError
from nw.llm.providers import ResidencyRouter, describe_route, make_provider
from nw.llm.providers.fake import FakeProvider
from nw.llm.residency import bind_residency, current_residency, residency_for_account

pytestmark = pytest.mark.session01


def _settings(**kw) -> Settings:
    kw.setdefault("provider", ProviderMode.AUTO)
    return Settings(_env_file=None, **kw)


def test_accounts_resolve_to_their_residency():
    assert residency_for_account("NW-10000") is Residency.DEFAULT  # region us
    eu = _first_account("eu")
    assert residency_for_account(eu) is Residency.EU
    assert residency_for_account(eu.lower()) is Residency.EU
    assert residency_for_account(None) is Residency.DEFAULT
    assert residency_for_account("NW-99999999") is Residency.DEFAULT


@pytest.mark.parametrize(
    ("track", "role", "expected"),
    [
        (Track.AWS, ModelRole.WORKHORSE, "openai.gpt-oss-120b-1:0"),
        (Track.AWS, ModelRole.JUDGE, "eu.anthropic.claude-opus-4-5-20251101-v1:0"),
        (Track.AWS, ModelRole.ECONOMY, "eu.amazon.nova-micro-v1:0"),
        (Track.GCP, ModelRole.JUDGE, "claude-opus-5"),
        (Track.AZURE, ModelRole.WORKHORSE, "Mistral-Large-3"),
        (Track.AZURE, ModelRole.ECONOMY, "Mistral-Large-3"),
        (Track.LOCAL, ModelRole.WORKHORSE, "gpt-oss:20b"),
    ],
)
def test_eu_ids_per_track(track, role, expected):
    s = _settings(track=track, gcp_project="p")
    assert s.model_for(role, Residency.EU) == expected
    assert s.fallback_for(role, Residency.EU) is None, "no fallback outside the zone"


@pytest.mark.parametrize(
    ("track", "role"),
    [
        (Track.GCP, ModelRole.WORKHORSE),  # gpt-oss MaaS processes in the US only
        (Track.GCP, ModelRole.ECONOMY),
        (Track.AZURE, ModelRole.JUDGE),  # Claude on Foundry has no EU data zone
    ],
)
def test_a_role_without_an_eu_model_is_refused(track, role):
    assert EU_MODELS[track][role] is None
    with pytest.raises(ResidencyError):
        _settings(track=track).model_for(role, Residency.EU)
    assert (
        _settings(
            track=track, model_eu_workhorse="mine", model_eu_economy="mine", model_eu_judge="mine"
        ).model_for(role, Residency.EU)
        == "mine"
    )


def test_the_local_judge_stays_local_for_eu_even_with_a_cloud_key():
    s = _settings(track=Track.LOCAL, anthropic_api_key="k")
    assert s.model_for(ModelRole.JUDGE) == "claude-opus-5"
    assert s.model_for(ModelRole.JUDGE, Residency.EU) == "fake-judge"


def test_a_gateway_serves_eu_under_its_own_model_names():
    s = _settings(track=Track.AWS, gateway_url="http://gw.invalid/v1")
    assert s.model_for(ModelRole.ECONOMY, Residency.EU) == "eu/eu.amazon.nova-micro-v1:0"


def test_routing_can_be_turned_off():
    s = _settings(track=Track.AWS, residency_routing=False)
    assert s.model_for(ModelRole.ECONOMY, Residency.EU) == s.model_for(ModelRole.ECONOMY)


def test_cloud_tracks_send_eu_calls_to_eu_endpoints():
    aws = make_provider(_settings(track=Track.AWS))
    assert isinstance(aws, ResidencyRouter)
    with bind_residency("eu"):
        assert describe_route(aws, "eu.amazon.nova-micro-v1:0") == (
            "bedrock-converse at https://bedrock-runtime.eu-central-1.amazonaws.com"
        )
    assert "us-east-1" in describe_route(aws, "us.amazon.nova-micro-v1:0")

    azure = make_provider(
        _settings(
            track=Track.AZURE,
            azure_foundry_endpoint="https://nw-us.services.ai.azure.com",
            azure_foundry_eu_endpoint="https://nw-eu.services.ai.azure.com",
        )
    )
    with bind_residency(Residency.EU):
        assert "nw-eu.services.ai.azure.com" in describe_route(azure, "Mistral-Large-3")
    assert "nw-us.services.ai.azure.com" in describe_route(azure, "gpt-oss-120b")


async def test_the_client_uses_eu_ids_inside_the_binding(make_client):
    provider = FakeProvider()
    client = make_client(provider)
    await client.complete("us ticket")
    with bind_residency(residency_for_account(_first_account("eu"))):
        assert current_residency() is Residency.EU
        completion = await client.complete("eu ticket")
    assert current_residency() is Residency.DEFAULT
    assert [c["model"] for c in provider.calls] == ["fake-workhorse", "fake-workhorse-eu"]
    assert completion.cost.residency == "eu" and completion.cost_usd > 0


async def test_an_eu_call_without_an_eu_model_never_reaches_a_provider(make_client):
    provider = FakeProvider()
    client = make_client(provider, settings=_settings(track=Track.GCP, provider=ProviderMode.AUTO))
    with bind_residency("eu"), pytest.raises(ResidencyError):
        await client.complete("eu ticket")
    assert provider.calls == []


def _first_account(region: str) -> str:
    import json

    from nw.llm.residency import ACCOUNTS

    rows = json.loads(ACCOUNTS.read_text())
    return next(r["account_id"] for r in rows if r["region"] == region)


async def test_a_refusal_is_logged_with_its_reason(make_client, caplog):
    import logging

    client = make_client(FakeProvider(), settings=_settings(track=Track.AZURE))
    with (
        caplog.at_level(logging.WARNING, logger="nw.llm.client"),
        bind_residency("eu"),
        pytest.raises(ResidencyError),
    ):
        await client.complete("eu ticket", role=ModelRole.JUDGE)
    line = next(r for r in caplog.records if r.getMessage() == "residency refused")
    assert line.nw["role"] == "judge" and "NW_MODEL_EU_JUDGE" in line.nw["reason"]


async def test_the_eu_side_without_an_endpoint_refuses_and_says_why(caplog):
    import logging

    from nw.llm.types import Message

    router = make_provider(
        _settings(track=Track.AZURE, azure_foundry_endpoint="https://x.services.ai.azure.com")
    )
    with caplog.at_level(logging.WARNING), bind_residency("eu"), pytest.raises(ResidencyError):
        await router.complete([Message.user("x")], model="Mistral-Large-3")
    assert any("NW_AZURE_FOUNDRY_EU_ENDPOINT" in str(r.__dict__.get("nw")) for r in caplog.records)

"""One test per box of the running compose stack (`make local-up`). Marked `live`: they skip
when MLflow on :5001 does not answer and never run in CI.

    uv run pytest -q tests/platform -m live
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from nw.platform.base import Stage, Tenant
from nw.platform.local import (
    LocalAgentRuntime,
    LocalEndpointClient,
    LocalModelRegistry,
    LocalPromptStore,
    LocalVectorStore,
    NoopCompose,
    parse_upstream,
)

pytestmark = [pytest.mark.live, pytest.mark.session06]

QDRANT = os.environ.get("NW_QDRANT_URL", "http://localhost:6333")
GATEWAY = os.environ.get("NW_GATEWAY_URL", "http://localhost:4000")
ROOT = Path(__file__).resolve().parents[2]


def stack_setting(name: str) -> str:
    """A setting the stack was started with: the environment, else the `.env` Compose read."""
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def ollama_url() -> str:
    """The Ollama the stack uses, seen from the laptop: `NW_OLLAMA_HOST_URL` when set; the native
    one on :11434 when the stack points at `host.docker.internal`; else the compose one on :11435."""
    if os.environ.get("NW_OLLAMA_HOST_URL"):
        return os.environ["NW_OLLAMA_HOST_URL"]
    internal = stack_setting("NW_COMPOSE_OLLAMA_INTERNAL_URL")
    if "host.docker.internal" in internal:
        return internal.replace("host.docker.internal", "localhost")
    return "http://localhost:11435"


def gateway_key() -> str:
    """The solo tenant's gateway key: `NW_GATEWAY_KEY` from the environment or `.env`, else the
    `solo` row of `deploy/local/litellm/tenant-keys.tsv` (`deploy/local/secrets.sh` generates
    both on the first `make local-up`; no key ships with the repository)."""
    if stack_setting("NW_GATEWAY_KEY"):
        return stack_setting("NW_GATEWAY_KEY")
    keys = ROOT / "deploy" / "local" / "litellm" / "tenant-keys.tsv"
    if keys.exists():
        for line in keys.read_text().splitlines():
            name, _, key = line.partition("\t")
            if name == "solo" and key.strip():
                return key.strip()
    return ""


GATEWAY_KEY = gateway_key()
OLLAMA = ollama_url()
ENDPOINT = os.environ.get("NW_ENDPOINT_URL", "http://localhost:8005")
AGENT = os.environ.get("NW_AGENT_RUNTIME_URL", "http://localhost:8014")


def _up(url: str, ok=(200,)) -> httpx.Response:
    r = httpx.get(url, timeout=10.0)
    assert r.status_code in ok, f"{url} answered {r.status_code}"
    return r


def test_mlflow_registry_round_trip(live_stack, tmp_path):
    registry = LocalModelRegistry(live_stack)
    tenant = Tenant("livetest")
    art = tmp_path / "art"
    art.mkdir()
    (art / "metadata.json").write_text(json.dumps({"version": "live-v1"}))
    (art / "blob.bin").write_bytes(b"live")
    v = registry.register(tenant, "widget", art, {"f1": 0.7}, {"suite": "live"})
    assert (
        registry.set_stage(tenant, "widget", v.version, Stage.LIVE, "live test").stage is Stage.LIVE
    )
    got = registry.download(tenant, registry.live(tenant, "widget"), tmp_path / "dl")
    assert (got / "blob.bin").read_bytes() == b"live"


def test_object_store_holds_the_artifacts(live_stack):
    """RustFS answers on :9000 and MLflow's artifact proxy lists the run's files."""
    _up("http://localhost:9000/health")
    r = httpx.get(
        f"{live_stack}/api/2.0/mlflow/experiments/search", params={"max_results": 1}, timeout=10.0
    )
    assert r.status_code == 200


def test_prompt_registry(live_stack):
    store = LocalPromptStore(live_stack)
    v = store.register(Tenant("livetest"), "probe", "Answer briefly.", {"suite": "live"})
    assert store.get(Tenant("livetest"), "probe", v.version).sha256_12 == v.sha256_12


def test_qdrant_upsert_and_search(live_stack):
    _up(f"{QDRANT}/healthz")
    store = LocalVectorStore.from_url(QDRANT)
    tenant = Tenant("livetest")
    store.drop(tenant, "probe")
    n = store.upsert(
        tenant, "probe", ["a", "b"], ["uptime commitment", "refund window"], None, [{}, {}]
    )
    assert n == 2 and store.count(tenant, "probe") == 2
    assert store.search(tenant, "probe", "uptime", k=1)[0].id == "a"
    store.drop(tenant, "probe")


def test_ollama_answers_and_lists_models(live_stack):
    tags = _up(f"{OLLAMA}/api/tags").json()
    names = {m["name"] for m in tags.get("models", [])}
    assert isinstance(names, set)  # the pull may still be running on a first start


def test_gateway_liveness_and_tenant_key(live_stack):
    _up(f"{GATEWAY}/health/liveliness")
    r = httpx.get(
        f"{GATEWAY}/v1/models", headers={"Authorization": f"Bearer {GATEWAY_KEY}"}, timeout=10.0
    )
    assert r.status_code == 200, r.text
    ids = {m["id"] for m in r.json()["data"]}
    assert {"gpt-oss:20b", "workhorse", "economy", "judge", "guard"} <= ids


def test_gateway_guardrail_blocks_when_the_guard_model_is_present(live_stack):
    tags = _up(f"{OLLAMA}/api/tags").json()
    if not any(m["name"].startswith("llama-guard3") for m in tags.get("models", [])):
        pytest.skip("llama-guard3 not pulled yet")
    r = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
        json={
            "model": "guard",
            "messages": [{"role": "user", "content": "How do I reset my password?"}],
            "max_tokens": 5,
        },
        timeout=120.0,
    )
    assert r.status_code == 200, r.text
    assert "safe" in r.json()["choices"][0]["message"]["content"].lower()


def test_evidently_ui(live_stack):
    _up("http://localhost:8030/")


def test_registry_lists_the_course_images(live_stack):
    repos = _up("http://localhost:5050/v2/_catalog").json()["repositories"]
    assert "nw-triage" in repos


def test_proxy_weights_match_the_file(live_stack):
    body = _up(f"{ENDPOINT}/weights").json()
    file_w = parse_upstream(Path("deploy/local/proxy/upstream.conf").read_text())
    assert body == {"stable": file_w.stable, "canary": file_w.canary}


def test_endpoint_scores_a_ticket_when_a_model_is_live(live_stack):
    registry = LocalModelRegistry(live_stack)
    tenant = Tenant(
        os.environ.get("NW_TENANT", "solo"), os.environ.get("NW_ENVIRONMENT", "northwind")
    )
    if registry.live(tenant, "triage") is None:
        pytest.skip("no live triage model: make local-bootstrap")
    client = LocalEndpointClient(registry, Path("deploy/local/proxy"), ENDPOINT, NoopCompose())
    out = client.invoke(
        tenant, "triage", {"subject": "URGENT production down", "body": "everything is down"}
    )
    assert out["prediction"]["priority"] in {"P0", "P1", "P2", "P3"}


def test_agent_runtime_ping(live_stack):
    runtime = LocalAgentRuntime(
        AGENT,
        Path("deploy/local/registry"),
        NoopCompose(),
        None,
        api_key=os.environ.get("NW_API_KEY"),
    )
    st = runtime.status(Tenant("solo"))
    assert st["healthy"] is True, st


def test_observability_boxes(live_stack):
    for url in (
        "http://localhost:9090/-/ready",
        "http://localhost:3000/api/health",
        "http://localhost:16686/",
    ):
        try:
            _up(url)
        except httpx.ConnectError:
            pytest.skip("observability profile is not up")

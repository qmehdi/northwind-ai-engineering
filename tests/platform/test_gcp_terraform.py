"""The Google Cloud platform Terraform: formatted, valid, and every tenant-scoped name carries
the tenant prefix. `terraform plan` runs against the google provider with a fake access
token: the root uses no data sources, so planning needs no credentials and no project.
Skipped when terraform is missing or the providers cannot be downloaded (offline)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "gcp"
PLATFORM = ROOT / "platform"
FIXTURES = PLATFORM / "fixtures"
TERRAFORM = shutil.which("terraform")
pytestmark = pytest.mark.skipif(TERRAFORM is None, reason="terraform not installed")

NAME_KEYS = (
    "name",
    "display_name",
    "account_id",
    "secret_id",
    "repository_id",
    "template_id",
    "entry_group_id",
    "deployed_index_id",
)


def tf(*args: str, cwd: Path = PLATFORM) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GOOGLE_OAUTH_ACCESS_TOKEN": "fake-token", "TF_IN_AUTOMATION": "1"}
    return subprocess.run(
        [TERRAFORM, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def initialised() -> None:
    r = tf("init", "-backend=false", "-input=false")
    if r.returncode != 0:
        pytest.skip(f"terraform init failed (offline?): {r.stderr[-400:]}")


def test_fmt_check() -> None:
    r = tf("fmt", "-check", "-recursive", str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr


def test_validate(initialised: None) -> None:
    r = tf("validate", "-no-color")
    assert r.returncode == 0, r.stdout + r.stderr


def _plan(fixture: str, tmp_path: Path) -> dict:
    out = tmp_path / f"{fixture}.tfplan"
    r = tf(
        "plan",
        "-input=false",
        "-lock=false",
        "-no-color",
        f"-var-file={FIXTURES / fixture}.tfvars",
        f"-out={out}",
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    shown = tf("show", "-json", str(out))
    assert shown.returncode == 0, shown.stderr
    return json.loads(shown.stdout)


def _resources(plan: dict) -> list[dict]:
    def walk(module: dict):
        yield from module.get("resources", [])
        for child in module.get("child_modules", []):
            yield from walk(child)

    return list(walk(plan["planned_values"]["root_module"]))


def _names(resources: list[dict]) -> list[tuple[str, str]]:
    out = []
    for r in resources:
        for key in NAME_KEYS:
            value = r["values"].get(key)
            if isinstance(value, str):
                out.append((r["address"], value))
    return out


@pytest.mark.parametrize("fixture,tenants", [("cohort", ["alice", "bob"]), ("solo", ["solo"])])
def test_plan_names_carry_the_tenant_prefix(
    initialised: None, tmp_path: Path, fixture: str, tenants: list[str]
) -> None:
    plan = _plan(fixture, tmp_path)
    resources = _resources(plan)
    assert len(resources) > 100
    names = _names(resources)
    scoped = [(a, v) for a, v in names if any(t in v for t in ("alice", "bob", "solo"))]
    assert scoped, "no tenant-scoped names planned"
    for address, value in scoped:
        assert any(t in value for t in tenants), (
            f"{address} names a tenant outside the fixture: {value}"
        )
        # Service accounts use the short form (30 character limit); everything else the full prefix.
        if "google_service_account" in address:
            short = re.compile(rf"^nw-({'|'.join(tenants)})-[a-z]+$")
            assert short.match(value) or value.startswith("northwind-"), (address, value)
        else:
            assert (
                value.startswith("northwind-")
                or value.startswith("northwind_")
                or value.startswith("northwind ")
            ), (address, value)
    # Every tenant gets its three services, its agent, its pipelines identity and its corpus.
    for t in tenants:
        for kind in ("triage", "semantic", "policy"):
            assert any(
                v == f"northwind-{t}-{kind}"
                and "google_cloud_run_v2_service" in a
                and "iam" not in a
                for a, v in names
            ), (t, kind)
        assert any(
            v == f"northwind-{t}-agent" and "google_vertex_ai_reasoning_engine" in a
            for a, v in names
        )
        assert any(v == f"nw-{t}-pipelines" for _, v in names)
        assert any(v == f"northwind-{t}-policies" and "rag_corpus" in a for a, v in names)
    # Platform-level pieces exist once.
    assert sum(1 for a, _ in names if "google_clouddeploy_delivery_pipeline" in a) == 1
    assert sum(1 for r in resources if r["type"] == "google_vertex_ai_endpoint") == 2
    assert sum(1 for a, _ in names if "google_model_armor_template" in a) == 1


def test_solo_has_exactly_one_tenant(initialised: None, tmp_path: Path) -> None:
    plan = _plan("solo", tmp_path)
    engines = [r for r in _resources(plan) if r["type"] == "google_vertex_ai_reasoning_engine"]
    assert [r["values"]["display_name"] for r in engines] == ["northwind-solo-agent"]
    # The fixture passes tenants = ["ignored"]; solo mode wins.
    assert not any("ignored" in v for _, v in _names(_resources(plan)))
    # No gateway database in the solo fixture, no GitHub triggers.
    types = {r["type"] for r in _resources(plan)}
    assert "google_sql_database_instance" not in types
    assert "google_cloudbuild_trigger" not in types


def test_cohort_has_delivery_and_database(initialised: None, tmp_path: Path) -> None:
    plan = _plan("cohort", tmp_path)
    types = [r["type"] for r in _resources(plan)]
    assert types.count("google_cloudbuild_trigger") == 2
    assert "google_sql_database_instance" in types
    assert types.count("google_cloud_scheduler_job") == 2
    jobs = [r for r in _resources(plan) if r["type"] == "google_cloud_scheduler_job"]
    assert all(r["values"]["paused"] is True for r in jobs), "the retraining schedule ships paused"
    target = next(r for r in _resources(plan) if r["type"] == "google_clouddeploy_target")
    assert target["values"]["require_approval"] is True


# ----- assertions on the HCL itself, no terraform needed ---------------------------------------


def _hcl(module: str) -> str:
    return (ROOT / "modules" / module / "main.tf").read_text()


def test_every_module_exists_and_is_wired() -> None:
    main = (PLATFORM / "main.tf").read_text()
    for module in (
        "data",
        "tracking",
        "serving",
        "live",
        "prompts",
        "agents",
        "gateway",
        "delivery",
        "observability",
    ):
        assert f'module "{module}"' in main
        assert (ROOT / "modules" / module / "main.tf").exists()


def test_tenant_scoped_hcl_names_use_the_prefix() -> None:
    for module in ("tracking", "serving", "prompts", "agents", "gateway"):
        text = _hcl(module)
        for m in re.finditer(r'(display_name|name|secret_id)\s*=\s*"([^"]*each\.key[^"]*)"', text):
            # `${local.service}` is `${var.environment}-gateway`, the gateway's own secrets.
            assert m.group(2).startswith(("${var.environment}-", "${local.service}-")), (
                module,
                m.group(0),
            )
        for m in re.finditer(r'account_id\s*=\s*"([^"]*)"', text):
            assert m.group(1).startswith("nw-${each.key}") or m.group(1).startswith(
                "${var.environment}"
            ), (module, m.group(0))


def test_no_dashes_in_prose() -> None:
    for path in list(ROOT.rglob("*.tf")) + list(ROOT.rglob("*.md")) + list(ROOT.rglob("*.yaml")):
        if ".terraform" in path.parts:
            continue
        text = path.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, path


def test_scripted_resources_are_documented() -> None:
    """What has no provider resource is a null_resource with a script, and says so."""
    assert "null_resource" in _hcl("prompts") and "scripts/gcp_prompts.py" in _hcl("prompts")
    assert "null_resource" in _hcl("live") and "scripts/gcp_model_monitor.py" in _hcl("live")
    assert "agents/agents.json" in _hcl("agents")
    readme = (ROOT / "README.md").read_text()
    assert "Lower and higher environments" in readme
    assert "the Agent Platform (formerly Vertex AI)" in readme

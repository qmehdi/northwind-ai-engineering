"""The Google Cloud platform Terraform: formatted, valid, and every tenant-scoped name carries
the tenant prefix. `terraform plan` runs against the google provider with a fake access
token: the root uses no data sources, so planning needs no credentials and no project. The
root declares a GCS backend; the tests plan against a local one through a temporary
`test_backend_override.tf`, the same mechanism `NW_TF_STATE=local` uses.
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


OVERRIDE = PLATFORM / f"test_{os.getpid()}_backend_override.tf"


@pytest.fixture(scope="module")
def initialised(tmp_path_factory: pytest.TempPathFactory):
    state = tmp_path_factory.mktemp("tfstate") / "terraform.tfstate"
    OVERRIDE.write_text(f'terraform {{\n  backend "local" {{\n    path = "{state}"\n  }}\n}}\n')
    try:
        r = tf("init", "-input=false", "-reconfigure")
        if r.returncode != 0:
            pytest.skip(f"terraform init failed (offline?): {r.stderr[-400:]}")
        yield
    finally:
        OVERRIDE.unlink(missing_ok=True)


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


@pytest.fixture(scope="module")
def cohort(initialised: None, tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    return _resources(_plan("cohort", tmp_path_factory.mktemp("cohort")))


@pytest.fixture(scope="module")
def solo(initialised: None, tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    return _resources(_plan("solo", tmp_path_factory.mktemp("solo")))


def _of(resources: list[dict], kind: str) -> list[dict]:
    return [r for r in resources if r["type"] == kind]


def test_no_secret_value_in_the_plan(cohort: list[dict]) -> None:
    """Every generated secret is write-only: the plan (and so the state) never holds a value."""
    versions = _of(cohort, "google_secret_manager_secret_version")
    assert len(versions) >= 8
    for r in versions:
        assert not r["values"].get("secret_data"), r["address"]
        assert r["values"].get("secret_data_wo_version"), r["address"]
    for r in _of(cohort, "google_sql_user"):
        assert not r["values"].get("password"), r["address"]
    assert not _of(cohort, "random_password"), "random_password keeps its value in state"


def test_only_the_gateway_calls_models_directly(cohort: list[dict]) -> None:
    holders = [
        r["address"]
        for r in _of(cohort, "google_project_iam_member")
        if r["values"].get("role") == "roles/aiplatform.user"
    ]
    assert holders and all(a.startswith("module.gateway.") for a in holders), holders
    for r in _of(cohort, "google_project_iam_custom_role"):
        perms = set(r["values"]["permissions"])
        assert "aiplatform.endpoints.predict" not in perms, r["address"]
        assert "aiplatform.reasoningEngines.create" not in perms, r["address"]
        assert "aiplatform.reasoningEngines.delete" not in perms, r["address"]
    tenant = next(
        r
        for r in _of(cohort, "google_project_iam_custom_role")
        if r["values"]["role_id"].endswith("_tenant")
    )
    assert "aiplatform.reasoningEngines.update" not in tenant["values"]["permissions"]
    assert "aiplatform.customJobs.create" not in tenant["values"]["permissions"]


def test_engines_carry_no_secret_env_and_the_service_agent_reads_no_secret(
    cohort: list[dict],
) -> None:
    for r in _of(cohort, "google_vertex_ai_reasoning_engine"):
        dep = r["values"]["spec"][0]["deployment_spec"][0]
        assert not dep.get("secret_env"), r["address"]
        # Values that depend on other resources are unknown at plan time and left out.
        env = {e["name"]: e.get("value", "") for e in dep["env"]}
        tenant = r["values"]["labels"]["tenant"]
        assert env["NW_API_KEY_SECRET_NAME"].endswith(
            f"/secrets/northwind-{tenant}-api-key/versions/latest"
        )
        assert env["NW_GATEWAY_KEY_SECRET_NAME"].endswith(
            f"/secrets/northwind-{tenant}-gateway-key/versions/latest"
        )
    agent = "gcp-sa-aiplatform-re"
    for kind in ("google_project_iam_member", "google_secret_manager_secret_iam_member"):
        for r in _of(cohort, kind):
            if agent in r["values"].get("member", ""):
                assert "secretmanager" not in r["values"].get("role", ""), r["address"]
    # Each tenant may update only its own engine, through a grant on that engine.
    grants = _of(cohort, "google_vertex_ai_reasoning_engine_iam_member")
    assert len(grants) == 2
    deny = _of(cohort, "google_iam_deny_policy")
    assert len(deny) == 1
    rules = deny[0]["values"]["rules"]
    assert any(agent in p for rule in rules for p in rule["deny_rule"][0]["denied_principals"])
    assert any(rule["deny_rule"][0]["denial_condition"] for rule in rules)


def test_storage_is_isolated_by_managed_folder(cohort: list[dict]) -> None:
    folders = {r["values"]["name"] for r in _of(cohort, "google_storage_managed_folder")}
    for t in ("alice", "bob"):
        assert f"northwind-{t}/" in folders
    assert {"baselines/", "agents/", "clouddeploy/"} <= folders
    # Nothing holds a bucket-wide write role; the only bucket-wide grants are read-only.
    for r in _of(cohort, "google_storage_bucket_iam_member"):
        assert r["values"]["role"] == "roles/storage.objectViewer", r["address"]
    writes = [
        r
        for r in _of(cohort, "google_storage_managed_folder_iam_member")
        if r["values"]["role"] == "roles/storage.objectUser"
    ]
    for r in writes:
        folder = r["values"]["managed_folder"]
        assert folder.startswith(
            (
                "northwind-alice/",
                "northwind-bob/",
                "clouddeploy/",
                *(f"northwind-live/{k}/" for k in ("trajectories", "feedback", "approvals")),
                "northwind-live/endpoints/",
            )
        ), r["address"]
    # Retention: operational data deleted at 90 days, audit at 400, not only moved to Nearline.
    buckets = {r["values"]["name"].split("-")[-1]: r for r in _of(cohort, "google_storage_bucket")}
    rules = buckets["artifacts"]["values"]["lifecycle_rule"]
    ages = {
        rule["condition"][0].get("age") for rule in rules if rule["action"][0]["type"] == "Delete"
    }
    assert {90, 400} <= ages
    capture = next(
        r
        for r in _of(cohort, "google_bigquery_dataset")
        if r["values"]["dataset_id"].endswith("_capture")
    )
    assert capture["values"]["default_table_expiration_ms"] == 90 * 86400000


def test_tenant_identity_can_run_the_workflow(cohort: list[dict]) -> None:
    """The learner acts as nw-<tenant>-user: acts as its own workload accounts, operates its own
    services, reads its own keys, and the scheduler's account acts as itself."""
    acts_as = [
        r
        for r in _of(cohort, "google_service_account_iam_member")
        if r["values"]["role"] == "roles/iam.serviceAccountUser"
    ]
    assert len([r for r in acts_as if "tenant_acts_as" in r["address"]]) == 4  # pipelines, agent
    assert len([r for r in acts_as if "acts_as_self" in r["address"]]) == 2
    assert len([r for r in acts_as if ".operators[" in r["address"]]) == 6  # three services each
    impersonation = [
        r
        for r in _of(cohort, "google_service_account_iam_member")
        if r["values"]["role"] == "roles/iam.serviceAccountTokenCreator"
    ]
    assert {r["values"]["member"] for r in impersonation} == {
        "user:alice@example.com",
        "user:bob@example.com",
    }


def test_mcp_is_private_with_the_agent_as_invoker(cohort: list[dict]) -> None:
    services = {r["values"]["name"]: r for r in _of(cohort, "google_cloud_run_v2_service")}
    for t in ("alice", "bob"):
        assert f"northwind-{t}-mcp" in services
    public = [
        r["address"]
        for r in _of(cohort, "google_cloud_run_v2_service_iam_member")
        if r["values"]["member"] == "allUsers"
    ]
    assert not any("mcp" in a for a in public), public
    invokers = [
        r
        for r in _of(cohort, "google_cloud_run_v2_service_iam_member")
        if r["address"].startswith("module.agents.google_cloud_run_v2_service_iam_member.mcp")
    ]
    assert len(invokers) == 2


def test_gateway_image_is_pinned_and_quotas_cap_compute(cohort: list[dict]) -> None:
    gateway = next(
        r
        for r in _of(cohort, "google_cloud_run_v2_service")
        if r["values"]["name"] == "northwind-gateway"
    )
    assert "@sha256:" in gateway["values"]["template"][0]["containers"][0]["image"]
    quotas = {
        r["values"]["metric"]: r["values"]["override_value"]
        for r in _of(cohort, "google_service_usage_consumer_quota_override")
    }
    gpu = [v for m, v in quotas.items() if "gpus" in m]
    assert gpu and all(v == "0" for v in gpu)
    assert _of(cohort, "google_org_policy_custom_constraint")
    services = {r["values"]["service"] for r in _of(cohort, "google_project_iam_audit_config")}
    assert services == {"secretmanager.googleapis.com", "aiplatform.googleapis.com"}


def test_solo_without_an_organisation_skips_org_controls(solo: list[dict]) -> None:
    assert not _of(solo, "google_iam_deny_policy")
    assert not _of(solo, "google_org_policy_custom_constraint")
    assert _of(solo, "google_service_usage_consumer_quota_override")


def test_pull_requests_build_unprivileged() -> None:
    """The account id is known only after apply, so the HCL is checked: pull requests run as the
    logs-only account, main as the builder."""
    text = _hcl("delivery")
    pr = text[text.index('resource "google_cloudbuild_trigger" "pull_request"') :]
    pr = pr[: pr.index("\n}\n")]
    assert "service_account = google_service_account.pr_checks.id" in pr
    main = text[text.index('resource "google_cloudbuild_trigger" "main"') :]
    main = main[: main.index("\n}\n")]
    assert "service_account = google_service_account.builder.id" in main


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
        "identity",
        "guardrails",
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
    for module in ("identity", "tracking", "serving", "prompts", "agents", "gateway"):
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


def test_state_backend_and_live_manifests() -> None:
    versions = (PLATFORM / "versions.tf").read_text()
    assert 'backend "gcs"' in versions and 'required_version = ">= 1.11"' in versions
    for path in (PLATFORM / "delivery").glob("run-*.yaml"):
        text = path.read_text()
        # Deterministic Cloud Run URLs carry the project number, not the id.
        assert "-@PROJECT@.@REGION@.run.app" not in text, path
        assert "@ENVIRONMENT@-live-gateway-key" in text, path
    main = (PLATFORM / "delivery" / "cloudbuild-main.yaml").read_text()
    assert "cosign sign" in main and "cosign verify" in main


def _env(values: list[dict]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in values}


def test_ops_state_folders_and_the_approval_gate(cohort: list[dict]) -> None:
    """Durable ops state (nw/agent/opstore.py) in nested managed folders per owner and kind; a
    runtime writes trajectories/ and feedback/ only, never approvals/ (ADR 0005)."""
    folders = {r["values"]["name"] for r in _of(cohort, "google_storage_managed_folder")}
    for owner in ("alice", "bob", "live"):
        for kind in ("trajectories", "feedback", "approvals"):
            assert f"northwind-{owner}/{kind}/" in folders
    grants = _of(cohort, "google_storage_managed_folder_iam_member")
    writes = [r for r in grants if r["values"]["role"] == "roles/storage.objectUser"]
    # The tenant agent: its own folder read only, its own trajectories and feedback written.
    agent_own = [
        r
        for r in grants
        if r["address"].startswith(
            "module.agents.google_storage_managed_folder_iam_member.agent_own"
        )
    ]
    assert {r["values"]["role"] for r in agent_own} == {"roles/storage.objectViewer"}
    agent_ops = {r["values"]["managed_folder"] for r in grants if ".agent_ops[" in r["address"]}
    assert agent_ops == {
        f"northwind-{t}/{k}/" for t in ("alice", "bob") for k in ("trajectories", "feedback")
    }
    # Who may write an approvals/ folder directly: the live approvers identity, nobody else
    # (a tenant's approver holds its whole tenant folder through the parent grant).
    approvals = [
        r["address"] for r in writes if r["values"]["managed_folder"].endswith("/approvals/")
    ]
    assert approvals == [
        'module.live.google_storage_managed_folder_iam_member.approvers["approvals"]'
    ]
    live = {r["values"]["managed_folder"] for r in writes if ".live_ops[" in r["address"]}
    assert live == {"northwind-live/trajectories/", "northwind-live/feedback/"}
    policy_writes = {r["values"]["managed_folder"] for r in writes if ".write[" in r["address"]}
    assert policy_writes == {"northwind-alice/feedback/", "northwind-bob/feedback/"}
    # Retention on the kinds the store writes.
    arts = next(
        r
        for r in _of(cohort, "google_storage_bucket")
        if r["values"]["name"].endswith("-artifacts")
    )
    by_age = {}
    for rule in arts["values"]["lifecycle_rule"]:
        for prefix in rule["condition"][0].get("matches_prefix") or []:
            by_age[prefix] = rule["condition"][0].get("age")
    for owner in ("alice", "live"):
        assert by_age[f"northwind-{owner}/trajectories/"] == 90
        assert by_age[f"northwind-{owner}/feedback/"] == 90
        assert by_age[f"northwind-{owner}/approvals/"] == 400


def test_runtimes_get_ops_store_platform_auth_and_private_tool_auth(cohort: list[dict]) -> None:
    for r in _of(cohort, "google_vertex_ai_reasoning_engine"):
        env = _env(r["values"]["spec"][0]["deployment_spec"][0]["env"])
        assert env["NW_RUNTIME_AUTH"] == "platform", "Agent Engine authorises every query with IAM"
        assert env["NW_TOOL_BACKEND"] == "mcp" and env["NW_MCP_AUTH"] == "google-id-token"
        assert env["NW_TOOL_AUTH"] == "google-id-token"
        assert env["NW_REDACT_DETECTOR"] == "heuristic"
        assert env["NW_OPS_STORE"].startswith("gs://northwind-") and env["NW_OPS_STORE"].endswith(
            "-artifacts"
        )
    services = {r["values"]["name"]: r for r in _of(cohort, "google_cloud_run_v2_service")}
    for t in ("alice", "bob"):
        policy = _env(
            services[f"northwind-{t}-policy"]["values"]["template"][0]["containers"][0]["env"]
        )
        assert (
            policy["NW_OPS_STORE"].endswith("-artifacts")
            and policy["NW_REDACT_DETECTOR"] == "heuristic"
        )
        mcp = _env(services[f"northwind-{t}-mcp"]["values"]["template"][0]["containers"][0]["env"])
        assert mcp["NW_TOOL_AUTH"] == "google-id-token"
        for name, svc in services.items():
            env = _env(svc["values"]["template"][0]["containers"][0]["env"])
            assert "NW_RUNTIME_AUTH" not in env, name
    for kind in ("agent", "policy"):
        text = (PLATFORM / "delivery" / f"run-{kind}.yaml").read_text()
        assert "value: gs://@ENVIRONMENT@-@PROJECT@-artifacts" in text
        assert "NW_RUNTIME_AUTH" not in text
    assert "NW_TOOL_AUTH" in (PLATFORM / "delivery" / "run-agent.yaml").read_text()


def test_scheduled_runs_ship_the_tenant_source_and_live_moves_traffic(cohort: list[dict]) -> None:
    """The weekly run executes the tenant's bundle (nw/pipelines/source.py) with the platform
    env the champion lookup needs; the live identity may move traffic on the live endpoints and
    write the canary record, which the drill tenants write too."""
    import base64

    for r in _of(cohort, "google_cloud_scheduler_job"):
        body = json.loads(base64.b64decode(r["values"]["http_target"][0]["body"]))
        values = body["runtimeConfig"]["parameterValues"]
        tenant = values["tenant"]
        assert values["source_uri"].endswith(f"/northwind-{tenant}/source/latest.tar.gz")
        env = json.loads(values["platform_env"])
        assert env["NW_TRACK"] == "gcp" and env["NW_TENANT"] == tenant
        assert {"NW_GCP_ARTIFACTS_BUCKET", "NW_GCP_PIPELINES_BUCKET", "NW_GCP_DATA_BUCKET"} <= set(
            env
        )
    live = [
        r
        for r in _of(cohort, "google_vertex_ai_endpoint_iam_member")
        if ".live_deployer[" in r["address"]
    ]
    assert len(live) == len(_of(cohort, "google_vertex_ai_endpoint"))
    record = [
        r
        for r in _of(cohort, "google_storage_managed_folder_iam_member")
        if ".canary_record[" in r["address"]
    ]
    assert {r["values"]["managed_folder"] for r in record} == {"northwind-live/endpoints/"}
    assert len(record) == 3, "the live identity and the two drill tenants"


def test_quality_signals_are_log_metrics_with_the_slo_bars(cohort: list[dict]) -> None:
    """The metrics_snapshot fields nw/metrics_export.py emits, each a distribution metric, with
    the bars of deploy/SLO.md; the quality_alert line counted by signal."""
    metrics = {r["values"]["name"]: r["values"] for r in _of(cohort, "google_logging_metric")}
    for field in (
        "quality_level",
        "shadow_agreement",
        "p0_share_ratio",
        "refusal_ratio",
        "judge_score",
    ):
        m = metrics[f"northwind-{field.replace('_', '-')}"]
        assert f"jsonPayload.{field}:*" in m["filter"] and 'msg="metrics_snapshot"' in m["filter"]
        assert m["value_extractor"] == f"EXTRACT(jsonPayload.{field})"
    assert "northwind-p0-share" not in metrics and "northwind-refusal-rate" not in metrics
    assert "signal" in metrics["northwind-quality-alerts"]["label_extractors"]
    bars = {}
    for r in _of(cohort, "google_monitoring_alert_policy"):
        for c in r["values"]["conditions"]:
            t = c.get("condition_threshold") or []
            if (
                t
                and "user/northwind-" in t[0]["filter"]
                and "quality" in r["values"]["display_name"]
            ):
                bars[t[0]["filter"].split("user/northwind-")[1].rstrip('"'), t[0]["comparison"]] = (
                    t[0]["threshold_value"]
                )
    assert bars[("quality-level", "COMPARISON_GE")] == 2
    assert bars[("shadow-agreement", "COMPARISON_LT")] == 0.9
    assert bars[("p0-share-ratio", "COMPARISON_GE")] == 2
    assert bars[("p0-share-ratio", "COMPARISON_LE")] == 0.5
    assert bars[("refusal-ratio", "COMPARISON_GE")] == 2
    assert bars[("judge-score", "COMPARISON_LT")] == 3.5

"""The SageMaker definitions: built on a fake session, the JSON carries the processing
steps in order, a condition on the gate file, a register step into the tenant's model
package group with PendingManualApproval and a fail step, and never calls AWS."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from nw.pipelines.params import BY_PIPELINE, defaults
from nw.platform.base import Tenant

pytestmark = pytest.mark.pipelines
pytest.importorskip("sagemaker.mlops.workflow.pipeline")

from nw.pipelines.sagemaker import SageMakerConfig, definition, fake_session  # noqa: E402


@pytest.fixture(scope="module")
def config() -> SageMakerConfig:
    return SageMakerConfig(
        tenant=Tenant(name="alice", environment="northwind"),
        role_arn="arn:aws:iam::123456789012:role/northwind-pipelines",
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/nw-pipelines:test",
        serving_image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/nw-triage:test",
        bucket="nw-bucket",
    )


@pytest.fixture(scope="module")
def session():
    return fake_session()


@pytest.fixture(scope="module")
def triage(config, session):
    return definition("triage", config, session)


@pytest.fixture(scope="module")
def semantic(config, session):
    return definition("semantic", config, session)


def _steps(d):
    return [(s["Name"], s["Type"]) for s in d["Steps"]]


def test_definition_makes_no_aws_call(config, session):
    definition("triage", config, session)
    definition("semantic", config, session)
    client = session.sagemaker_client
    assert isinstance(client, MagicMock) and client.mock_calls == []


def test_triage_steps_in_order_and_the_image(triage, config):
    assert _steps(triage) == [
        ("DataCheck", "Processing"),
        ("Train", "Processing"),
        ("Evaluate", "Processing"),
        ("Gate", "Condition"),
    ]
    for step in triage["Steps"][:3]:
        app = step["Arguments"]["AppSpecification"]
        assert app["ImageUri"] == config.image_uri
        assert app["ContainerEntrypoint"][:2] == ["python", "-m"]
        assert app["ContainerEntrypoint"][2].startswith("nw.pipelines.steps.")
    train = triage["Steps"][1]
    assert train["DependsOn"] == ["DataCheck"]
    assert "--package-dir" in train["Arguments"]["AppSpecification"]["ContainerArguments"]
    evaluate = triage["Steps"][2]
    inputs = {i["InputName"]: i for i in evaluate["Arguments"]["ProcessingInputs"]}
    assert inputs["artifacts"]["S3Input"]["S3Uri"] == {
        "Get": "Steps.Train.ProcessingOutputConfig.Outputs['artifacts'].S3Output.S3Uri"
    }
    assert inputs["production"]["S3Input"]["S3Uri"] == {"Get": "Parameters.ProductionSummary"}
    assert evaluate["PropertyFiles"] == [
        {
            "PropertyFileName": "TriageGate",
            "OutputName": "artifacts",
            "FilePath": "steps/triage_evaluate.json",
        }
    ]
    env = train["Arguments"]["Environment"]
    assert env["NW_TENANT"] == "alice" and env["NW_TRACK"] == "aws"


def test_gate_condition_registers_pending_approval_or_fails(triage, config):
    gate = triage["Steps"][3]["Arguments"]
    [condition] = gate["Conditions"]
    assert condition["Type"] == "GreaterThanOrEqualTo" and condition["RightValue"] == 1
    assert condition["LeftValue"]["Std:JsonGet"]["Path"] == "passed_int"
    assert condition["LeftValue"]["Std:JsonGet"]["PropertyFile"] == {
        "Get": "Steps.Evaluate.PropertyFiles.TriageGate"
    }
    [register] = gate["IfSteps"]
    assert register["Name"] == "Register" and register["Type"] == "RegisterModel"
    args = register["Arguments"]
    assert args["ModelPackageGroupName"] == "northwind-alice-triage"
    assert args["ModelApprovalStatus"] == "PendingManualApproval"
    assert args["CustomerMetadataProperties"]["pipeline"] == "triage"
    assert args["CustomerMetadataProperties"]["tenant"] == "northwind-alice"
    [container] = args["InferenceSpecification"]["Containers"]
    assert container["Image"] == config.serving_image_uri
    assert container["ModelDataUrl"]["Std:Join"]["Values"][-1] == "model.tar.gz"
    [fail] = gate["ElseSteps"]
    assert fail["Type"] == "Fail" and "gate failed" in json.dumps(fail["Arguments"])


def test_trigger_is_declared_and_recorded_on_the_package(triage, semantic):
    for d in (triage, semantic):
        got = {p["Name"]: p for p in d["Parameters"]}
        assert got["Trigger"] == {"Name": "Trigger", "Type": "String", "DefaultValue": "manual"}
        [register] = d["Steps"][-1]["Arguments"]["IfSteps"]
        trigger = register["Arguments"]["CustomerMetadataProperties"]["trigger"]
        assert {"Get": "Parameters.Trigger"} in trigger["Std:Join"]["Values"]


def test_deployed_defaults_replace_the_repo_paths(config, session):
    from dataclasses import replace

    deployed = replace(
        config,
        defaults={
            "data_uri": "s3://d/data/tickets/",
            "output_root": "s3://a/tenants/alice/pipelines",
            "production_summary": "s3://a/baselines/triage_production.json",
        },
    )
    got = {
        p["Name"]: p.get("DefaultValue")
        for p in definition("triage", deployed, session)["Parameters"]
    }
    assert got["DataUri"] == "s3://d/data/tickets/"
    assert got["OutputRoot"] == "s3://a/tenants/alice/pipelines"
    assert got["ProductionSummary"] == "s3://a/baselines/triage_production.json"
    assert got["MinP0Recall"] == defaults("triage")["min_p0_recall"], "the rest stay as declared"


def test_parameters_are_the_declared_schema(triage, semantic):
    for name, d in (("triage", triage), ("semantic", semantic)):
        expected = {p.sagemaker_name: p for p in BY_PIPELINE[name] if p.sagemaker}
        got = {p["Name"]: p for p in d["Parameters"]}
        assert set(got) == set(expected)
        assert got["MinP0Recall"]["Type"] == "Float"
        assert got["Force"] == {"Name": "Force", "Type": "String", "DefaultValue": "false"}
        assert got["DataUri"]["Type"] == "String"
    assert semantic["Parameters"] and {"Epochs", "Subset", "Lr"} <= {
        p["Name"] for p in semantic["Parameters"]
    }
    args = " ".join(
        json.dumps(a)
        for a in triage["Steps"][2]["Arguments"]["AppSpecification"]["ContainerArguments"]
    )
    assert '"--min-p0-recall" {"Get": "Parameters.MinP0Recall"}' in args
    assert '"--force" {"Get": "Parameters.Force"}' in args


def test_semantic_steps_chain_through_the_artifacts_prefix(semantic, config):
    assert _steps(semantic) == [
        ("DataPrep", "Processing"),
        ("Train", "Processing"),
        ("Export", "Processing"),
        ("Benchmark", "Processing"),
        ("GateCheck", "Processing"),
        ("Gate", "Condition"),
    ]
    by_name = {s["Name"]: s for s in semantic["Steps"]}
    assert (
        by_name["Train"]["Arguments"]["ProcessingResources"]["ClusterConfig"]["InstanceType"]
        == config.training_instance_type
    )
    for step, previous in (
        ("Export", "Train"),
        ("Benchmark", "Export"),
        ("GateCheck", "Benchmark"),
    ):
        inputs = {i["InputName"]: i for i in by_name[step]["Arguments"]["ProcessingInputs"]}
        assert inputs["artifacts"]["S3Input"]["S3Uri"]["Get"].startswith(
            f"Steps.{previous}.ProcessingOutputConfig"
        )
    output = by_name["Train"]["Arguments"]["ProcessingOutputConfig"]["Outputs"][0]["S3Output"]
    assert output["S3Uri"]["Std:Join"]["Values"][1:] == [
        "semantic",
        {"Get": "Execution.PipelineExecutionId"},
        "artifacts",
    ]
    gate = by_name["Gate"]["Arguments"]
    assert gate["IfSteps"][0]["Arguments"]["ModelPackageGroupName"] == "northwind-alice-semantic"
    assert gate["IfSteps"][0]["Arguments"]["ModelApprovalStatus"] == "PendingManualApproval"
    model_data = gate["IfSteps"][0]["Arguments"]["InferenceSpecification"]["Containers"][0][
        "ModelDataUrl"
    ]
    assert model_data["Std:Join"]["Values"][0]["Get"].startswith("Steps.Export.")


def test_definition_cli_prints_json(capsys):
    from nw.pipelines.sagemaker.definition import main

    assert (
        main(
            [
                "--pipeline",
                "triage",
                "--tenant",
                "bob",
                "--role",
                "arn:aws:iam::123456789012:role/x",
                "--image",
                "img",
                "--bucket",
                "b",
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["Steps"][3]["Arguments"]["IfSteps"][0]["Arguments"]["ModelPackageGroupName"] == (
        "northwind-bob-triage"
    )

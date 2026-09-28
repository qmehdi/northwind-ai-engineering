"""The OpenAPI snapshots are the API contract: additive change passes, a breaking change
fails, and the committed snapshots match the apps."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from openapi_snapshot import (  # noqa: E402
    DEFAULT_DIR,
    SERVICES,
    breaking_changes,
    check_snapshots,
    load_app,
    snapshot,
    write_snapshots,
)

pytestmark = pytest.mark.session06

OLD = {
    "paths": {
        "/v1/triage": {
            "post": {
                "parameters": [{"name": "k", "in": "query", "required": False}],
                "requestBody": {},
                "responses": {"200": {}, "422": {}},
            }
        },
        "/v1/version": {"get": {"responses": {"200": {}}}},
    },
    "components": {
        "schemas": {
            "TicketIn": {
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                "required": ["body"],
            },
            "TriageResult": {
                "properties": {"priority": {"type": "string"}, "confidence": {"type": "number"}},
                "required": ["priority", "confidence"],
            },
        }
    },
}


def _new(mutate):
    new = copy.deepcopy(OLD)
    mutate(new)
    return new


def test_additive_changes_pass():
    def add(spec):
        spec["paths"]["/v1/drift"] = {"get": {"responses": {"200": {}}}}
        spec["paths"]["/v1/triage"]["post"]["responses"]["503"] = {}
        spec["paths"]["/v1/triage"]["post"]["parameters"].append(
            {"name": "verbose", "in": "query", "required": False}
        )
        spec["components"]["schemas"]["TicketIn"]["properties"]["language"] = {"type": "string"}
        spec["components"]["schemas"]["TriageResult"]["properties"]["rule"] = {"type": "string"}
        spec["components"]["schemas"]["Extra"] = {"properties": {}}
        spec["components"]["schemas"]["TicketIn"]["description"] = "longer"

    assert breaking_changes(OLD, _new(add)) == []
    assert breaking_changes(OLD, OLD) == []


@pytest.mark.parametrize(
    "reason, mutate",
    [
        ("removed path", lambda s: s["paths"].pop("/v1/version")),
        ("removed operation", lambda s: s["paths"]["/v1/triage"].pop("post")),
        (
            "removed property",
            lambda s: s["components"]["schemas"]["TriageResult"]["properties"].pop("confidence"),
        ),
        (
            "changed type",
            lambda s: s["components"]["schemas"]["TriageResult"]["properties"].__setitem__(
                "confidence", {"type": "string"}
            ),
        ),
        (
            "new required field",
            lambda s: s["components"]["schemas"]["TicketIn"]["required"].append("subject"),
        ),
        (
            "became optional",
            lambda s: s["components"]["schemas"]["TriageResult"].__setitem__(
                "required", ["priority"]
            ),
        ),
        ("removed schema", lambda s: s["components"]["schemas"].pop("TicketIn")),
        (
            "removed parameter",
            lambda s: s["paths"]["/v1/triage"]["post"].__setitem__("parameters", []),
        ),
        (
            "became required",
            lambda s: s["paths"]["/v1/triage"]["post"]["parameters"][0].__setitem__(
                "required", True
            ),
        ),
        ("removed response", lambda s: s["paths"]["/v1/triage"]["post"]["responses"].pop("200")),
    ],
)
def test_breaking_changes_are_named(reason, mutate):
    problems = breaking_changes(OLD, _new(mutate))
    assert problems and any(reason in p for p in problems), problems


def test_committed_snapshots_match_the_apps(tmp_path, capsys):
    assert set(SERVICES) == {"triage", "semantic", "policy", "agent"}
    for name, spec in SERVICES.items():
        committed = json.loads((DEFAULT_DIR / f"{name}.json").read_text())
        assert breaking_changes(committed, snapshot(load_app(spec))) == []
        assert any(p.startswith("/v1/") for p in committed["paths"])
    written = write_snapshots(tmp_path)
    assert {p.name for p in written} == {f"{n}.json" for n in SERVICES}
    assert check_snapshots(tmp_path) == 0
    # A snapshot with an extra path is a breaking change for the app that lacks it.
    triage = json.loads((tmp_path / "triage.json").read_text())
    triage["paths"]["/v1/gone"] = {"get": {"responses": {"200": {}}}}
    (tmp_path / "triage.json").write_text(json.dumps(triage))
    assert check_snapshots(tmp_path) == 1
    assert "removed path /v1/gone" in capsys.readouterr().out
    (tmp_path / "triage.json").unlink()
    assert check_snapshots(tmp_path) == 1, "a missing snapshot fails too"

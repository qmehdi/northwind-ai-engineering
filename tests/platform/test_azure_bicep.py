"""The Azure platform Bicep: it builds and lints, every tenant-scoped name carries the tenant
prefix, cohort mode yields per-tenant resources, solo mode yields one tenant, and every key of
the outputs.json contract with nw/platform/azure.py is an output.

Nothing here calls Azure. `bicep build` compiles main.bicep to an ARM template (restoring the
Azure Verified Modules from the public registry, which needs the network once), `bicep
build-params` turns the fixtures into parameter values, and `ArmEvaluator` below evaluates the
ARM template language far enough to name every resource the deployment would create: the
functions Bicep emits for names, conditions and copy loops (format, parameters, variables,
copyIndex, if, lambdas and friends). `reference()` and `list*()` are runtime only and are never
needed for a name. Skipped when no Bicep CLI is found (`make setup-azure` installs one into
~/.azure/bin) or the registry cannot be reached.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "azure"
FIXTURES = ROOT / "fixtures"
CONTRACT = (
    "NW_AZURE_SUBSCRIPTION_ID",
    "NW_AZURE_RESOURCE_GROUP",
    "NW_AZURE_LOCATION",
    "NW_AZURE_ML_WORKSPACE",
    "NW_AZURE_FOUNDRY_ENDPOINT",
    "NW_AZURE_FOUNDRY_PROJECT",
    "NW_AZURE_SEARCH_ENDPOINT",
    "NW_AZURE_KEY_VAULT",
    "NW_AZURE_ACR",
    "NW_AZURE_STORAGE_ACCOUNT",
    "NW_AZURE_APPINSIGHTS_CONNECTION_STRING",
    "NW_AZURE_APIM_GATEWAY_URL",
    "NW_AZURE_CONTAINERAPPS_ENV",
)


def bicep_cli() -> list[str] | None:
    """The Bicep CLI: $NW_BICEP, ~/.azure/bin/bicep (where `az bicep install` and `make
    setup-azure` put it), `bicep` on PATH, or `az bicep`."""
    explicit = os.environ.get("NW_BICEP")
    if explicit:
        return [explicit]
    local = Path.home() / ".azure" / "bin" / "bicep"
    if local.exists():
        return [str(local)]
    if shutil.which("bicep"):
        return ["bicep"]
    if shutil.which("az"):
        return ["az", "bicep"]
    return None


BICEP = bicep_cli()
needs_bicep = pytest.mark.skipif(BICEP is None, reason="no Bicep CLI (make setup-azure)")


def bicep(*args: str) -> subprocess.CompletedProcess[str]:
    assert BICEP is not None
    return subprocess.run(
        [*BICEP, *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=600
    )


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out = tmp_path_factory.mktemp("bicep") / "main.json"
    r = bicep("build", "main.bicep", "--outfile", str(out))
    if r.returncode != 0 and ("BCP192" in r.stderr or "restore" in r.stderr.lower()):
        pytest.skip(f"Azure Verified Modules could not be restored (offline?): {r.stderr[-400:]}")
    assert r.returncode == 0, r.stderr
    assert "Warning" not in r.stderr, r.stderr
    return json.loads(out.read_text())


def fixture_params(name: str) -> dict[str, Any]:
    r = bicep("build-params", str(FIXTURES / f"{name}.bicepparam"), "--stdout")
    assert r.returncode == 0, r.stderr
    params = json.loads(json.loads(r.stdout)["parametersJson"])["parameters"]
    return {k: v["value"] for k, v in params.items()}


# ----- a small evaluator for the ARM template language ------------------------------------------


class Unresolved(Exception):
    """A value the evaluator cannot produce at all."""


class Runtime:
    """A runtime-only value (reference, list*, a module output passed on): carried through
    containers and formats as RUNTIME, so a count or a name never depends on it silently."""

    def __getitem__(self, key: Any) -> Runtime:
        return self

    def __str__(self) -> str:
        return "RUNTIME"


RT = Runtime()
# Functions that build containers or strings keep going with a runtime value inside.
CARRIES_RUNTIME = {
    "format",
    "concat",
    "createarray",
    "createobject",
    "coalesce",
    "tryget",
    "union",
    "if",
}


@dataclass
class Lambda:
    names: list[str]
    body: Any
    scope: Scope


@dataclass
class Scope:
    template: dict
    params: dict[str, Any]
    copy: dict[str, int] = field(default_factory=dict)
    lambdas: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)

    def child(self, **copy: int) -> Scope:
        return Scope(
            self.template, self.params, {**self.copy, **copy}, dict(self.lambdas), self.cache
        )


RESOURCE_GROUP = {
    "id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-northwind",
    "name": "rg-northwind",
    "location": "eastus2",
}
SUBSCRIPTION = {
    "id": "/subscriptions/00000000-0000-0000-0000-000000000000",
    "subscriptionId": "00000000-0000-0000-0000-000000000000",
    "tenantId": "11111111-1111-1111-1111-111111111111",
}
ENVIRONMENT = {"name": "AzureCloud", "suffixes": {"storage": "core.windows.net"}}
TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(\S))")


def tokenize(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        s, n, ident, punct = m.groups()
        if s is not None:
            out.append(("str", s[1:-1].replace("''", "'")))
        elif n is not None:
            out.append(("num", n))
        elif ident is not None:
            out.append(("id", ident))
        elif punct is not None:
            out.append(("p", punct))
    return out


def parse(text: str) -> Any:
    """ARM expression to a tree: ('lit', v) | ('call', name, [args]) | ('get', obj, key)."""
    toks = tokenize(text)
    i = 0

    def peek(value: str) -> bool:
        return i < len(toks) and toks[i] == ("p", value)

    def expr() -> Any:
        nonlocal i
        kind, value = toks[i]
        i += 1
        if kind == "str":
            node: Any = ("lit", value)
        elif kind == "num":
            node = ("lit", float(value) if "." in value else int(value))
        elif kind == "id":
            args = []
            if peek("("):
                i += 1
                while not peek(")"):
                    args.append(expr())
                    if peek(","):
                        i += 1
                i += 1
            node = ("call", value.lower(), args)
        else:
            raise ValueError(f"unexpected {value!r} in {text!r}")
        while True:
            if peek("."):
                i += 1
                node = ("get", node, ("lit", toks[i][1]))
                i += 1
            elif peek("["):
                i += 1
                key = expr()
                i += 1
                node = ("get", node, key)
            else:
                return node

    return expr()


def unique_string(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:13]


class ArmEvaluator:
    def value(self, v: Any, scope: Scope) -> Any:
        """A template value: strings in brackets are expressions, containers recurse."""
        if isinstance(v, str):
            if v.startswith("[["):
                return v[1:]
            if v.startswith("[") and v.endswith("]"):
                return self.eval(parse(v[1:-1]), scope)
            return v
        if isinstance(v, list):
            return [self.value(x, scope) for x in v]
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, x in v.items():
                # A for-expression inside properties compiles to a `copy` list of named loops.
                if k == "copy" and isinstance(x, list) and all("input" in c for c in x):
                    for c in x:
                        n = self.value(c["count"], scope)
                        out[c["name"]] = [
                            self.value(c["input"], scope.child(**{c["name"]: j})) for j in range(n)
                        ]
                else:
                    out[k] = self.value(x, scope)
            return out
        return v

    def parameter(self, name: str, scope: Scope) -> Any:
        if name in scope.params:
            return scope.params[name]
        spec = scope.template["parameters"][name]
        if "defaultValue" in spec:
            return self.value(spec["defaultValue"], scope)
        if spec.get("nullable"):
            return None
        return RT

    def variable(self, name: str, scope: Scope) -> Any:
        variables = scope.template.get("variables", {})
        for loop in variables.get("copy", []):
            if loop["name"] == name:
                count = self.value(loop["count"], scope)
                return [self.value(loop["input"], scope.child(**{name: j})) for j in range(count)]
        return self.value(variables[name], scope)

    def eval(self, node: Any, scope: Scope) -> Any:  # noqa: C901 - one dispatch table
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "get":
            obj = self.eval(node[1], scope)
            key = self.eval(node[2], scope)
            if isinstance(obj, Runtime) or isinstance(key, Runtime):
                return RT
            if isinstance(obj, dict) and key not in obj:
                raise Unresolved(f"property {key}")
            return obj[key]
        _, name, args = node

        def a(k: int) -> Any:
            return self.eval(args[k], scope)

        if name == "if":
            return a(1) if a(0) else a(2)
        if name == "lambda":
            return Lambda([self.eval(x, scope) for x in args[:-1]], args[-1], scope)
        if name == "lambdavariables":
            return scope.lambdas[a(0)]
        values = [self.eval(x, scope) for x in args]
        if name not in CARRIES_RUNTIME and any(isinstance(v, Runtime) for v in values):
            return RT
        match name:
            case "parameters":
                return self.parameter(values[0], scope)
            case "variables":
                return self.variable(values[0], scope)
            case "copyindex":
                # copyIndex(), copyIndex(offset), copyIndex('loop'), copyIndex('loop', offset)
                if values and isinstance(values[0], str):
                    return scope.copy[values[0]] + (values[1] if len(values) > 1 else 0)
                return scope.copy["__resource__"] + (values[0] if values else 0)
            case "format":
                return re.sub(
                    r"\{(\d+)(:[^}]*)?\}", lambda m: str(values[1 + int(m.group(1))]), values[0]
                )
            case "concat":
                if values and isinstance(values[0], list):
                    return [x for v in values for x in v]
                return "".join(str(v) for v in values)
            case "createarray":
                return list(values)
            case "createobject":
                return {values[j]: values[j + 1] for j in range(0, len(values), 2)}
            case "equals":
                return values[0] == values[1]
            case "not":
                return not values[0]
            case "and":
                return all(values)
            case "or":
                return any(values)
            case "true":
                return True
            case "false":
                return False
            case "null":
                return None
            case "empty":
                return not values[0]
            case "length":
                return len(values[0])
            case "tolower":
                return values[0].lower()
            case "toupper":
                return values[0].upper()
            case "replace":
                return values[0].replace(values[1], values[2])
            case "take":
                return values[0][: values[1]]
            case "skip":
                return values[0][values[1] :]
            case "substring":
                return (
                    values[0][values[1] : values[1] + values[2]]
                    if len(values) > 2
                    else values[0][values[1] :]
                )
            case "string":
                return values[0] if isinstance(values[0], str) else json.dumps(values[0])
            case "int":
                return int(values[0])
            case "bool":
                return bool(values[0])
            case "json":
                return json.loads(values[0])
            case "startswith":
                return values[0].startswith(values[1])
            case "endswith":
                return values[0].endswith(values[1])
            case "contains":
                return values[1] in values[0]
            case "split":
                return values[0].split(values[1])
            case "join":
                return values[1].join(values[0])
            case "first":
                return values[0][0]
            case "last":
                return values[0][-1]
            case "range":
                return list(range(values[0], values[0] + values[1]))
            case "add":
                return values[0] + values[1]
            case "sub":
                return values[0] - values[1]
            case "greater":
                return values[0] > values[1]
            case "less":
                return values[0] < values[1]
            case "union":
                if isinstance(values[0], dict):
                    out: dict = {}
                    for v in values:
                        out.update(v or {})
                    return out
                return list(dict.fromkeys(x for v in values for x in v))
            case "coalesce":
                return next((v for v in values if v is not None), None)
            case "trytget" | "tryget":
                obj = values[0]
                for key in values[1:]:
                    if obj is None:
                        return None
                    obj = (
                        obj.get(key)
                        if isinstance(obj, dict)
                        else (obj[key] if key < len(obj) else None)
                    )
                return obj
            case "items":
                return [{"key": k, "value": v} for k, v in values[0].items()]
            case "flatten":
                return [x for v in values[0] for x in v]
            case "map":
                return [self.call(values[1], x, j) for j, x in enumerate(values[0])]
            case "filter":
                return [x for j, x in enumerate(values[0]) if self.call(values[1], x, j)]
            case "toobject":
                val = values[2] if len(values) > 2 else None
                return {
                    self.call(values[1], x): (self.call(val, x) if val else x) for x in values[0]
                }
            case "uniquestring":
                return unique_string(*values)
            case "guid":
                h = hashlib.sha256("|".join(values).encode()).hexdigest()
                return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
            case "resourcegroup":
                return RESOURCE_GROUP
            case "subscription":
                return SUBSCRIPTION
            case "environment":
                return ENVIRONMENT
            case "deployment":
                return {"name": "main"}
            case "utcnow":
                return "2026-09-29T00:00:00Z"
            case "resourceid" | "subscriptionresourceid" | "extensionresourceid":
                return "/" + "/".join(str(v) for v in values)
            case _:  # reference(), list*(), pickZones() and the rest are runtime
                return RT

    def call(self, fn: Lambda, *args: Any) -> Any:
        scope = fn.scope.child()
        for n, v in zip(fn.names, args, strict=False):
            scope.lambdas[n] = v
        return self.eval(fn.body, scope)


@dataclass
class Planned:
    type: str
    name: str
    module: str
    properties: Any


def plan(template: dict, params: dict[str, Any]) -> list[Planned]:
    """Every resource the deployment would create, with its evaluated name. Local modules
    (metadata owner = northwind) are walked; an Azure Verified Module counts as the resource
    it wraps, named by its `name` parameter."""
    ev = ArmEvaluator()
    out: list[Planned] = []

    def walk(tpl: dict, scope: Scope, module: str) -> None:
        resources = tpl.get("resources", [])
        if isinstance(resources, dict):
            resources = list(resources.values())
        for res in resources:
            if res.get("existing"):
                continue
            count = 1
            loop = res.get("copy", {}).get("name")
            if "copy" in res:
                count = ev.value(res["copy"]["count"], scope)
            for j in range(count):
                s = scope.child(**({loop: j, "__resource__": j} if loop else {}))
                if "condition" in res and not ev.value(res["condition"], s):
                    continue
                try:
                    name = str(ev.value(res["name"], s))
                except Unresolved:
                    name = "RUNTIME"
                if res["type"] == "Microsoft.Resources/deployments":
                    inner = res["properties"]["template"]
                    passed: dict[str, Any] = {}
                    raw = res["properties"].get("parameters", {})
                    if isinstance(raw, str):  # a whole-object expression
                        try:
                            raw = ev.value(raw, s)
                        except Unresolved:
                            raw = {}
                    for k, v in raw.items():
                        # `{"value": ...}`, `{"copy": [...]}` for a for-expression value, or an
                        # expression that yields `{"value": ...}` (a conditional value).
                        if not (isinstance(v, str) or ({"value", "copy"} & v.keys())):
                            continue
                        try:
                            passed[k] = ev.value(v, s)["value"]
                        except Unresolved:
                            pass
                    if inner.get("metadata", {}).get("owner") == "northwind":
                        walk(inner, Scope(inner, passed), name)
                    elif "name" in passed:
                        out.append(Planned(f"avm:{name}", str(passed["name"]), module, passed))
                    continue
                try:
                    props = ev.value(res.get("properties", {}), s)
                except (Unresolved, KeyError, TypeError, IndexError):
                    props = None
                out.append(Planned(res["type"], name, module, props))

    walk(template, Scope(template, params), "main")
    return out


# ----- tests --------------------------------------------------------------------------------------


@needs_bicep
def test_builds_and_lints_clean(template: dict) -> None:
    r = bicep("lint", "main.bicep")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Warning" not in r.stdout + r.stderr and "Error" not in r.stdout + r.stderr


@needs_bicep
def test_every_contract_key_is_an_output(template: dict) -> None:
    outputs = template["outputs"]
    for key in CONTRACT:
        assert key in outputs, key
        assert outputs[key]["type"] == "string", key


def _last(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _tenant_scoped(resources: list[Planned], tenants: list[str]) -> list[Planned]:
    return [r for r in resources if any(t in _last(r.name) for t in tenants)]


@needs_bicep
@pytest.mark.parametrize("fixture,tenants", [("cohort", ["alice", "bob"]), ("solo", ["solo"])])
def test_names_carry_the_tenant_prefix(template: dict, fixture: str, tenants: list[str]) -> None:
    resources = plan(template, fixture_params(fixture))
    assert len(resources) > 40
    scoped = _tenant_scoped(resources, ["alice", "bob", "solo", "ignored"])
    assert scoped, "no tenant-scoped names"
    for r in scoped:
        last = _last(r.name)
        # Online endpoints take the short form nw/platform/azure.py computes (32 characters,
        # unique per region): nw-<owner>-<kind>-<scope>.
        if r.type.endswith("onlineEndpoints"):
            assert any(re.fullmatch(rf"nw-{t}-(triage|semantic)-a1b2c", last) for t in tenants), (
                r.name
            )
            continue
        assert any(re.match(rf"northwind-{t}(-|$)", last) for t in tenants), (r.type, r.name)
    # Platform names: `northwind-<kind>`, `nw<kind><suffix>` where hyphens are not allowed, a
    # GUID where the type demands one, and child names inside an already prefixed parent (APIM
    # operations and policies, model deployments named by model id, environment versions).
    children = (
        "/operations",
        "/policies",
        "/diagnostics",
        "/loggers",
        "/versions",
        "/backends",
        "/apis",
        "accounts/deployments",
        "products/apis",
    )
    bad = [
        (r.type, r.name)
        for r in resources
        if not (
            _last(r.name).startswith(("northwind-", "northwind_", "nw", "Containers"))
            or re.fullmatch(r"[0-9a-f-]{36}", _last(r.name))
            or (r.type.endswith(children) and r.name.split("/")[0].startswith("northwind-"))
            # Role assignment names are GUIDs of runtime resource ids.
            or (r.type == "Microsoft.Authorization/roleAssignments" and r.name == "RUNTIME")
        )
    ]
    assert not bad, bad
    # No-hyphen names stay within their limits: storage and Key Vault 24, registry 50.
    for r in resources:
        if r.name.startswith("nw") and "-" not in r.name:
            assert re.fullmatch(r"nw[a-z]+[a-z0-9]{6}", r.name) and len(r.name) <= 24, r.name


def _names(resources: list[Planned], kind: str) -> list[str]:
    return [r.name for r in resources if kind in r.type]


@needs_bicep
def test_cohort_yields_per_tenant_resources(template: dict) -> None:
    resources = plan(template, fixture_params("cohort"))
    names = [r.name for r in resources]
    for t in ("alice", "bob", "live"):
        assert f"northwind-{t}-id" in names, t
        for kind in ("policy", "agent", "mcp"):
            assert f"northwind-{t}-{kind}" in names, (t, kind)
        endpoints = {_last(n) for n in _names(resources, "onlineEndpoints")}
        assert {f"nw-{t}-triage-a1b2c", f"nw-{t}-semantic-a1b2c"} <= endpoints, t
        assert any(
            n.endswith(f"/northwind-{t}-gateway-key") for n in _names(resources, "vaults/secrets")
        ), t
        assert any(
            n.endswith(f"/northwind-{t}") for n in _names(resources, "service/subscriptions")
        ), t
    for t in ("alice", "bob"):
        assert any(
            n.endswith(f"/northwind-{t}-retrain-triage")
            for n in _names(resources, "workspaces/schedules")
        ), t
    schedules = [r for r in resources if r.type.endswith("workspaces/schedules")]
    assert len(schedules) == 2, "one schedule per tenant, none for live"
    assert all(r.properties["isEnabled"] is False for r in schedules), "retraining ships disabled"
    # The ABAC condition confines each tenant to its prefix.
    writes = [
        r
        for r in resources
        if r.type == "Microsoft.Authorization/roleAssignments"
        and isinstance(r.properties, dict)
        and r.properties.get("condition")
    ]
    assert len(writes) == 3 * 2, "artifacts and pipelines, for alice, bob and live"
    for t in ("alice", "bob", "live"):
        assert sum(f"'northwind-{t}/'" in r.properties["condition"] for r in writes) == 2, t
    # Platform pieces exist once.
    for avm in ("northwind-ml", "northwind-logs", "northwind-apps", "northwind-appinsights"):
        assert names.count(avm) == 1, avm
    for kind in ("foundry", "apim", "search"):
        assert (
            len([n for n in names if re.fullmatch(rf"northwind-{kind}-[0-9a-z]{{6}}", n)]) == 1
        ), kind
    deployments = _names(resources, "accounts/deployments")
    assert sorted(n.rsplit("/", 1)[-1] for n in deployments) == [
        "claude-opus-5",
        "gpt-oss-120b",
        "mistral-small-2503",
        "text-embedding-3-small",
    ]
    assert len(_names(resources, "onlineEndpoints")) == 2 * 3, "triage and semantic per owner"
    # APIM carries the AI gateway policies; the live apps carry the traffic split.
    policies = [r for r in resources if r.type.endswith("apis/policies")]
    assert len(policies) == 2
    for r in policies:
        assert (
            "llm-token-limit" in r.properties["value"]
            and "llm-emit-token-metric" in r.properties["value"]
        )
        assert "authentication-managed-identity" in r.properties["value"]
    pools = [
        r
        for r in resources
        if r.type.endswith("service/backends")
        and r.properties
        and r.properties.get("type") == "Pool"
    ]
    assert len(pools) == 2


@needs_bicep
def test_solo_yields_one_tenant(template: dict) -> None:
    resources = plan(template, fixture_params("solo"))
    names = [r.name for r in resources]
    identities = sorted(
        n
        for n in names
        if re.fullmatch(r"northwind-[a-z0-9]+-id", n) and n != "northwind-gateway-id"
    )
    assert identities == ["northwind-live-id", "northwind-solo-id"]
    assert not any("ignored" in n for n in names), "solo mode ignores the tenant list"
    assert len([r for r in resources if r.type.endswith("workspaces/schedules")]) == 1
    # LiteLLM instead of APIM in this fixture: the gateway app and its database, no APIM.
    assert "northwind-gateway" in names
    assert any(n.startswith("northwind-gateway-db-") for n in names)
    assert not any(n.startswith("northwind-apim-") for n in names)
    assert not any("Microsoft.ApiManagement" in r.type for r in resources)


def test_no_dashes_in_the_azure_track() -> None:
    solution = ROOT.parents[1]
    paths = [
        *ROOT.rglob("*"),
        solution / "scripts" / "deploy_azure.sh",
        solution / "scripts" / "images_azure.sh",
    ]
    for path in paths:
        if path.is_file() and path.suffix != ".json":
            text = path.read_text()
            assert chr(0x2014) not in text and chr(0x2013) not in text, path


def test_readme_and_scripts_are_wired() -> None:
    readme = (ROOT / "README.md").read_text()
    for heading in (
        "## Prerequisites",
        "## The tenant workflow",
        "## The promotion drill",
        "## Lower and higher environments",
    ):
        assert heading in readme, heading
    script = (ROOT.parents[1] / "scripts" / "deploy_azure.sh").read_text()
    for action in (
        "build",
        "what-if",
        "deploy",
        "tenants",
        "status",
        "stop",
        "start",
        "destroy",
        "release",
        "approve",
    ):
        assert f"  {action})" in script or f"  {action}|" in script, action
    makefile = (ROOT.parents[1] / "Makefile").read_text()
    for target in (
        "setup-azure",
        "build-azure",
        "deploy-azure",
        "tenants-azure",
        "status-azure",
        "stop-azure",
        "start-azure",
        "destroy-azure",
        "images-azure",
        "release-azure",
        "approve-azure",
    ):
        assert re.search(rf"^{target}:", makefile, re.M), target
    for module in (
        "observability",
        "data",
        "tracking",
        "retrieval",
        "foundry",
        "gateway",
        "tenant",
        "agents",
        "serving",
        "delivery",
    ):
        assert (ROOT / "modules" / f"{module}.bicep").exists(), module
        assert f"modules/{module}.bicep" in (ROOT / "main.bicep").read_text(), module


_ALERTS_HARNESS = r"""
set -euo pipefail
RG=rg-northwind; ENVIRONMENT=northwind
out() { echo sub-123; }
az() { printf '%s\n' "$*" >> "$CALLS"; eval "$FAKE"; }
eval "$(sed -n '/^alerts_url()/,/^}/p;/^fired_live_alerts()/,/^}/p' "$SCRIPT")"
if FIRED="$(fired_live_alerts)"; then echo "readable:$FIRED"; else echo unreadable; fi
"""


def _fired(tmp_path: Path, fake: str) -> tuple[str, list[str]]:
    calls = tmp_path / "calls"
    calls.write_text("")
    env = {
        **os.environ,
        "SCRIPT": str(ROOT.parents[1] / "scripts" / "deploy_azure.sh"),
        "FAKE": fake,
        "CALLS": str(calls),
    }
    out = subprocess.run(
        ["bash", "-c", _ALERTS_HARNESS], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip(), calls.read_text().splitlines()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_approve_reads_alerts_at_resource_group_scope_and_fails_closed(tmp_path: Path) -> None:
    quiet, calls = _fired(tmp_path, """echo '{"value": []}'""")
    assert quiet == "readable:"
    assert "/subscriptions/sub-123/resourceGroups/rg-northwind/providers/" in calls[0]
    assert "Microsoft.AlertsManagement/alerts?api-version=2019-03-01" in calls[0]
    assert "monitorCondition=Fired" in calls[0]
    firing = """echo '{"value": [{"properties": {"essentials": {"alertRule": "northwind-live-agent-5xx"}}}, {"properties": {"essentials": {"alertRule": "northwind-alice-x"}}}]}'"""
    assert _fired(tmp_path, firing)[0] == "readable:northwind-live-agent-5xx"
    # Paged answers are followed to the end.
    paged = """case "$*" in *page2*) echo '{"value": [{"properties": {"essentials": {"alertRule": "northwind-live-policy-5xx"}}}]}';; *) echo '{"nextLink": "https://m/page2", "value": []}';; esac"""
    assert _fired(tmp_path, paged)[0] == "readable:northwind-live-policy-5xx"
    # An error, a denied read or an unreadable body is never a quiet answer.
    for broken in (
        "return 1",
        "echo 'not json'",
        """echo '{"error": {"code": "AuthorizationFailed"}}'""",
    ):
        assert _fired(tmp_path, broken)[0] == "unreadable", broken
    script = (ROOT.parents[1] / "scripts" / "deploy_azure.sh").read_text()
    assert (
        "/providers/Microsoft.AlertsManagement/alerts?api-version=2019-05-05-preview" not in script
    )
    approve = script.split("  approve)", 1)[1].split(";;", 1)[0]
    assert "fired_live_alerts" in approve and "|| true" not in approve


def test_apim_url_never_travels_as_the_litellm_variable() -> None:
    """NW_GATEWAY_URL is the LiteLLM route in nw.llm.providers: behind API Management the apps
    and outputs.json carry the APIM URL as NW_AZURE_APIM_GATEWAY_URL and leave NW_GATEWAY_URL
    empty, or the services would send LiteLLM-shaped requests to APIM."""
    main = (ROOT / "main.bicep").read_text()
    agents = (ROOT / "modules" / "agents.bicep").read_text()
    gateway = (ROOT / "modules" / "gateway.bicep").read_text()
    assert "output NW_GATEWAY_URL string = gateway.outputs.litellmUrl" in main
    assert "NW_AZURE_APIM_GATEWAY_URL: gateway.outputs.apimGatewayUrl" in main
    assert "output litellmUrl string = apim ? ''" in gateway
    assert "empty(litellmUrl) ? [] : [{ name: 'NW_GATEWAY_URL', value: litellmUrl }]" in agents
    assert "gatewayUrl" not in agents
    readme = (ROOT / "README.md").read_text()
    assert "NW_GATEWAY_URL=<NW_GATEWAY_URL" not in readme


@needs_bicep
def test_content_safety_endpoint_is_an_output(template: dict) -> None:
    assert template["outputs"]["NW_AZURE_CONTENT_SAFETY_ENDPOINT"]["type"] == "string"

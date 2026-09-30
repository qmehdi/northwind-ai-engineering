"""Parameters in, outputs out, for scripts/deploy_azure.sh. Standard library only.

    python3 deploy/azure/scripts/azure_params.py params <file>     # main.bicep parameters from NW_* env
    python3 deploy/azure/scripts/azure_params.py outputs <file>    # ARM outputs on stdin, flat JSON to file
    python3 deploy/azure/scripts/azure_params.py get <KEY>         # one value from outputs.json
    python3 deploy/azure/scripts/azure_params.py tenants           # what each tenant got
    python3 deploy/azure/scripts/azure_params.py scope <sub> <rg> <env>   # the endpoint scope hash
    python3 deploy/azure/scripts/azure_params.py check-tenants     # validate NW_TENANTS

The parameter file carries the secure values (the cohort API key, LiteLLM keys); the script
writes it with mode 600 in a temporary directory and deletes it after the deployment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent / "outputs.json"
TENANT = re.compile(r"^[a-z][a-z0-9]{1,11}$")


def tenants() -> list[str]:
    if os.environ.get("NW_MODE", "cohort") == "solo":
        return ["solo"]
    return [t.strip() for t in os.environ.get("NW_TENANTS", "").split(",") if t.strip()]


def check_tenants() -> None:
    names = tenants()
    bad = [t for t in names if not TENANT.match(t) or t == "live"]
    if bad:
        sys.exit(
            f"tenant names must be 2 to 12 lowercase letters and digits, starting with a letter, "
            f"and not `live`: {bad}"
        )
    if len(set(names)) != len(names):
        sys.exit(f"duplicate tenant names: {names}")
    if os.environ.get("NW_MODE", "cohort") == "cohort" and not names:
        print("no tenants: only `live` (a higher environment); NW_TENANTS=alice,bob for a cohort")


def scope(subscription: str, group: str, environment: str) -> str:
    """The same five hex characters nw/platform/azure.py puts in endpoint names."""
    key = f"{subscription}/{group}/{environment}".lower()
    return hashlib.sha256(key.encode()).hexdigest()[:5]


def pairs(raw: str) -> dict[str, str]:
    out = {}
    for item in raw.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def params(path: str) -> None:
    e = os.environ
    values: dict[str, object] = {
        "environment": e.get("NW_ENVIRONMENT", "northwind"),
        "location": e.get("NW_AZURE_LOCATION", "eastus2"),
        "mode": e.get("NW_MODE", "cohort"),
        "tenants": tenants() if e.get("NW_MODE", "cohort") == "cohort" else [],
        "tenantUsers": pairs(e.get("NW_TENANT_USERS", "")),
        "alertEmail": e.get("NW_ALERT_EMAIL", ""),
        "budgetUsd": int(e.get("NW_BUDGET_USD", "300")),
        "imageTag": e.get("NW_DEPLOY_IMAGE_TAG", ""),
        "gatewayKind": e.get("NW_GATEWAY_KIND", "apim"),
        "apimSku": e.get("NW_APIM_SKU", "BasicV2"),
        "searchSku": e.get("NW_SEARCH_SKU", ""),
        "purview": e.get("NW_PURVIEW", "false") == "true",
        "defenderForContainers": e.get("NW_DEFENDER", "false") == "true",
        "retrainEnabled": e.get("NW_RETRAIN_ENABLED", "false") == "true",
        "endpointScope": e["NW_ENDPOINT_SCOPE"],
        "apiKey": e["NW_API_KEY_VALUE"],
        "apiKeys": json.loads(e.get("NW_API_KEYS", "{}") or "{}"),
        "tenantTokensPerMonth": int(e.get("NW_TENANT_TOKENS_PER_MONTH", "6000000")),
        "egressControl": e.get("NW_EGRESS_CONTROL", "true") != "false",
        "euFoundry": e.get("NW_AZURE_EU", "true") != "false",
        "euLocation": e.get("NW_AZURE_EU_LOCATION", "swedencentral"),
        "adminObjectId": e.get("NW_ADMIN_OBJECT_ID", ""),
        "adminPrincipalType": e.get("NW_ADMIN_PRINCIPAL_TYPE", "User"),
        "githubRepository": e.get("NW_GITHUB_REPOSITORY", ""),
        "azureDevOpsIssuer": e.get("NW_ADO_ISSUER", ""),
        "azureDevOpsSubject": e.get("NW_ADO_SUBJECT", ""),
        "azureDevOpsBuilderSubject": e.get("NW_ADO_BUILDER_SUBJECT", ""),
        "endpointTraffic": json.loads(e.get("NW_ENDPOINT_TRAFFIC", "{}") or "{}"),
        "liveApps": json.loads(e.get("NW_LIVE_APPS", "{}") or "{}"),
    }
    if e.get("NW_SECONDARY_FOUNDRY_ENDPOINT"):
        values["secondaryFoundryEndpoint"] = e["NW_SECONDARY_FOUNDRY_ENDPOINT"]
    if values["gatewayKind"] == "litellm":
        values["litellmMasterKey"] = e["NW_LITELLM_MASTER_KEY"]
        values["postgresPassword"] = e["NW_POSTGRES_PASSWORD"]
        values["litellmSaltKey"] = e["NW_LITELLM_SALT_KEY"]
        values["gatewayKeys"] = json.loads(e["NW_GATEWAY_KEYS"])
    doc = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {k: {"value": v} for k, v in values.items()},
    }
    target = Path(path)
    target.write_text(json.dumps(doc, indent=2))
    target.chmod(0o600)


def outputs(path: str) -> None:
    """`az deployment group create --query properties.outputs` on stdin: `{name: {type, value}}`.
    Written flat, `{name: value}`, which nw/platform/azure.py reads (it accepts both forms)."""
    raw = json.load(sys.stdin)
    flat = {k: v.get("value") if isinstance(v, dict) else v for k, v in raw.items()}
    Path(path).write_text(json.dumps(flat, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path} ({len(flat)} keys)")


def load() -> dict:
    if not OUTPUTS.exists():
        sys.exit(f"{OUTPUTS} is missing: run make deploy-azure first")
    return json.loads(OUTPUTS.read_text())


def get(key: str) -> None:
    value = load().get(key, "")
    print(json.dumps(value) if isinstance(value, (list, dict)) else value)


def show_tenants() -> None:
    out = load()
    apps = out.get("NW_AZURE_APPS", [])
    for t in out.get("NW_AZURE_TENANTS", []):
        print(t["tenant"])
        for k in (
            "prefix",
            "identity_client_id",
            "endpoints",
            "retrain_schedule",
            "search_index",
            "gateway_key_secret",
            "api_key_secret",
            "artifacts_prefix",
        ):
            value = ", ".join(t[k]) if isinstance(t.get(k), list) else t.get(k, "")
            print(f"  {k:22} {value}")
        for a in apps:
            if a["owner"] == t["tenant"]:
                print(f"  {a['kind']:22} {a['url']}")
    print("live")
    print(f"  {'endpoint':22} {out.get('NW_AZURE_LIVE_ENDPOINT', '')}")
    for a in apps:
        if a["owner"] == "live":
            print(f"  {a['kind']:22} {a['url']}")


def main(argv: list[str]) -> None:
    match argv:
        case ["params", path]:
            params(path)
        case ["outputs", path]:
            outputs(path)
        case ["get", key]:
            get(key)
        case ["tenants"]:
            show_tenants()
        case ["scope", sub, group, env]:
            print(scope(sub, group, env))
        case ["check-tenants"]:
            check_tenants()
        case ["owners"]:
            print(" ".join([*tenants(), "live"]))
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

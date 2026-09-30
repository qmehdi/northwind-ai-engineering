"""`scripts/rotate_key.sh` against fake cloud CLIs: no cloud call, only the order of the calls.

Each fake (`aws`, `gcloud`, `az`) appends its arguments to a log and answers the reads the script
makes from small canned replies, so the tests check the properties the script promises: a dry
run changes nothing, a split of traffic stops the rotation before the secret moves, old versions
are disabled only after every service serves again, and the key never travels on a command line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rotate_key.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

GCLOUD = r"""#!/usr/bin/env bash
echo "gcloud $*" >> "$FAKE_LOG"
case "$*" in
  "run services describe"*--format=json*)
    if [ -n "${FAKE_SPLIT:-}" ]; then echo '{"status": {"traffic": [{"percent": 90}, {"percent": 10}]}}'
    else echo '{"status": {"traffic": [{"percent": 100}]}}'; fi ;;
  "run services describe"*) case "$*" in *-mcp*|*-triage*|*-semantic*|*-policy*|*-agent*) exit 0 ;; esac ;;
  "secrets versions list"*--limit=1*) echo 3 ;;
  "secrets versions list"*) printf '3\n2\n' ;;
  "secrets list"*) printf 'projects/p/secrets/northwind-alice-api-key\nprojects/p/secrets/northwind-bob-api-key\n' ;;
  "secrets versions add"*) cat > /dev/null ;;
esac
exit 0
"""

AZ = r"""#!/usr/bin/env bash
echo "az $*" >> "$FAKE_LOG"
case "$*" in
  "containerapp ingress traffic show"*)
    if [ -n "${FAKE_SPLIT:-}" ]; then echo '[{"weight": 90}, {"weight": 10}]'; else echo '[{"weight": 100}]'; fi ;;
  "containerapp show"*activeRevisionsMode*) case "$*" in *-live-*) echo Multiple ;; *) echo Single ;; esac ;;
  "containerapp show"*latestRevisionName*) echo rev-2 ;;
  "containerapp show"*) exit 0 ;;
  "containerapp revision show"*) echo Healthy ;;
  "keyvault secret list-versions"*)
    echo '[{"id": "https://kv/secrets/s/v1", "attributes": {"enabled": true, "created": 1}},
           {"id": "https://kv/secrets/s/v2", "attributes": {"enabled": true, "created": 2}}]' ;;
esac
exit 0
"""

AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$*" in
  "lambda get-alias"*) echo null ;;
  "lambda get-function-configuration"*) echo '{"Variables": {"NW_TRACK": "aws"}}' ;;
  "lambda publish-version"*) echo 7 ;;
  "secretsmanager describe-secret"*) echo '{"old-version": ["AWSCURRENT"]}' ;;
  "bedrock-agentcore-control list-agent-runtimes"*) echo '{"agentRuntimes": [{"agentRuntimeName": "northwind_live_resolver", "agentRuntimeId": "rt-1"}]}' ;;
  "bedrock-agentcore-control get-agent-runtime"*"--query status"*) echo READY ;;
  "bedrock-agentcore-control get-agent-runtime"*) echo '{"roleArn": "r", "environmentVariables": {"A": "1"}}' ;;
esac
exit 0
"""


def fakes(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", GCLOUD), ("az", AZ), ("aws", AWS)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "NW_GCP_PROJECT": "p",
        "NW_AZURE_KEY_VAULT": "nwkv123456",
        "NW_API_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:1:secret:live",
        "NW_AWS_TENANT_API_KEYS": "alice=arn:aws:secretsmanager:us-east-1:1:secret:alice",
    }
    return env, log


def rotate(env: dict[str, str], *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**env, **extra},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


MUTATING = (
    "versions add",
    "services update",
    "update-traffic",
    "versions disable",
    "keyvault secret set",
    "set-attributes",
    "containerapp update",
    "traffic set",
    "put-secret-value",
    "update-function-configuration",
    "publish-version",
    "update-alias",
    "update-agent-runtime",
    "update-secret-version-stage",
    "update-api-key-credential-provider",
)


@pytest.mark.parametrize("track", ["gcp", "azure", "aws"])
def test_dry_run_changes_nothing(tmp_path: Path, track: str) -> None:
    env, log = fakes(tmp_path)
    r = rotate(env, "--dry-run", TRACK=track, TENANT="all", NW_TENANTS="alice")
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert not [m for m in MUTATING if m in calls], calls
    assert "would run" in r.stdout and "new API keys" not in r.stdout


def test_gcp_rotates_one_tenant_and_disables_old_versions_last(tmp_path: Path) -> None:
    env, log = fakes(tmp_path)
    r = rotate(env, TRACK="gcp", TENANT="alice")
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    added = next(
        i for i, c in enumerate(calls) if "secrets versions add northwind-alice-api-key" in c
    )
    updates = [i for i, c in enumerate(calls) if "run services update northwind-alice-" in c]
    to_latest = [i for i, c in enumerate(calls) if "update-traffic" in c and "--to-latest" in c]
    disabled = [c for c in calls if "secrets versions disable" in c]
    assert len(updates) == 4 and len(to_latest) == 4, "triage, semantic, policy and mcp"
    assert added < min(updates)
    assert (
        disabled == [c for c in disabled if " 2 --secret northwind-alice-api-key" in c] and disabled
    )
    assert calls.index(disabled[0]) > max(to_latest), (
        "old versions go only after every service serves"
    )
    assert not any("northwind-bob" in c or "northwind-api-key" in c for c in calls)
    keys = re.findall(r"^\s+alice ([0-9a-f]{40})$", r.stdout, re.M)
    assert len(keys) == 1, r.stdout
    assert keys[0] not in log.read_text(), "the key never travels on a command line"


def test_gcp_refuses_during_a_traffic_split(tmp_path: Path) -> None:
    env, log = fakes(tmp_path)
    r = rotate(env, TRACK="gcp", TENANT="alice", FAKE_SPLIT="1")
    assert r.returncode == 1
    assert "split" in r.stderr
    assert "versions add" not in log.read_text(), "the secret never moves while traffic is split"


def test_azure_rotates_live_with_the_key_map_and_moves_all_traffic(tmp_path: Path) -> None:
    env, log = fakes(tmp_path)
    r = rotate(env, TRACK="azure", TENANT="live")
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "keyvault secret set --vault-name nwkv123456 -n northwind-live-api-key --file" in calls
    assert calls.count("containerapp update -n northwind-live-") == 3
    assert calls.count("--revision-weight latest=100") == 3
    assert "set-attributes --id https://kv/secrets/s/v1 --enabled false" in calls
    assert "s/v2 --enabled false" not in calls
    key = re.search(r"^\s+live ([0-9a-f]{40})$", r.stdout, re.M)
    assert key and key.group(1) not in calls


def test_aws_moves_the_alias_at_once_and_unlabels_the_old_version(tmp_path: Path) -> None:
    env, log = fakes(tmp_path)
    r = rotate(env, TRACK="aws", TENANT="live")
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    alias = next(i for i, c in enumerate(calls) if "update-alias" in c)
    assert "--function-version 7" in calls[alias] and "deploy create-deployment" not in "".join(
        calls
    )
    unlabel = next(i for i, c in enumerate(calls) if "update-secret-version-stage" in c)
    runtime = next(i for i, c in enumerate(calls) if "update-agent-runtime" in c)
    assert unlabel > max(alias, runtime)
    assert "--remove-from-version-id old-version" in calls[unlabel]
    assert any("update-api-key-credential-provider" in c for c in calls)
    key = re.search(r"^\s+live ([0-9a-f]{40})$", r.stdout, re.M)
    assert key and key.group(1) not in "\n".join(calls)


def test_unknown_tenant_is_refused(tmp_path: Path) -> None:
    env, _ = fakes(tmp_path)
    r = rotate(env, TRACK="gcp", TENANT="carol")
    assert r.returncode == 2 and "no tenant carol" in r.stderr

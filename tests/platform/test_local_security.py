"""The Local stack's security baseline (audit 2026-09-29, lens 02 Low and 06 L21): every
published port on 127.0.0.1, no usable secret in the repository, the secrets generated on the
first `make local-up`, and the containers that hold a secret refuse to start without one.
Offline: reads the compose files and runs deploy/local/secrets.sh in a scratch copy."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = [
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.gpu.yml",
    ROOT / "docker-compose.small.yml",
]
OLD_DEFAULTS = ("change-me", "northwind-secret", "PASSWORD:-northwind}", "S3_ACCESS_KEY:-nw}")


def test_every_published_port_is_bound_to_localhost() -> None:
    for path in COMPOSE:
        for line in path.read_text().splitlines():
            if line.strip().startswith("ports:"):
                ports = re.findall(r'"([^"]+)"', line)
                assert ports, line
                assert all(p.startswith("127.0.0.1:") for p in ports), (path.name, line)


def test_no_default_secret_in_the_repository() -> None:
    files = [
        *COMPOSE,
        ROOT / "deploy" / "local" / "litellm" / "tenants.tsv",
        ROOT / "deploy" / "local" / "README.md",
    ]
    for path in files:
        text = path.read_text()
        for old in OLD_DEFAULTS:
            if path.name == "README.md":
                continue
            assert old not in text, (path.name, old)
    for line in (ROOT / "deploy" / "local" / "litellm" / "tenants.tsv").read_text().splitlines():
        if line and not line.startswith("#"):
            assert len(line.split("\t")) == 2, line
    ignored = (ROOT / "deploy" / "local" / ".gitignore").read_text()
    assert "litellm/tenant-keys.tsv" in ignored


def test_secret_holders_refuse_to_start_without_one() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    litellm = compose.split("  litellm:\n", 1)[1].split("\n  litellm-keys:", 1)[0]
    assert 'if [ -z "$$LITELLM_MASTER_KEY" ]' in litellm and "exit 1" in litellm
    keys = (ROOT / "deploy" / "local" / "litellm" / "keys.sh").read_text()
    assert "no key" in keys and "exit 1" in keys
    assert "no authentication" in (ROOT / "deploy" / "local" / "README.md").read_text()


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_secrets_are_generated_once_and_kept(tmp_path: Path) -> None:
    (tmp_path / "deploy" / "local" / "litellm").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "local" / "secrets.sh", tmp_path / "deploy" / "local")
    shutil.copy(
        ROOT / "deploy" / "local" / "litellm" / "tenants.tsv",
        tmp_path / "deploy" / "local" / "litellm",
    )
    (tmp_path / ".env").write_text("NW_DB_PASSWORD=mine\n")
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "nw-secrets-test-no-volumes", "NW_TENANT": "alice"}
    # No docker needed: the volume check fails closed to "no earlier stack".
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    (tmp_path / "docker").write_text("#!/bin/sh\nexit 1\n")
    (tmp_path / "docker").chmod(0o755)
    script = ["sh", "deploy/local/secrets.sh"]
    subprocess.run(script, cwd=tmp_path, env=env, check=True, capture_output=True)
    first = (tmp_path / ".env").read_text()
    values = dict(line.split("=", 1) for line in first.splitlines() if "=" in line)
    assert values["NW_DB_PASSWORD"] == "mine", "a value already in .env is kept"
    for key in ("LITELLM_MASTER_KEY", "NW_S3_SECRET_KEY", "NW_GRAFANA_PASSWORD", "NW_GATEWAY_KEY"):
        assert len(values[key]) >= 20 and "change-me" not in values[key], key
    keys = (tmp_path / "deploy" / "local" / "litellm" / "tenant-keys.tsv").read_text()
    assert {line.split("\t")[0] for line in keys.splitlines()} == {"solo", "alice", "bob"}
    assert f"alice\t{values['NW_GATEWAY_KEY']}" in keys
    subprocess.run(script, cwd=tmp_path, env=env, check=True, capture_output=True)
    assert (tmp_path / ".env").read_text() == first, "a second run changes nothing"
    assert oct((tmp_path / ".env").stat().st_mode)[-3:] == "600"

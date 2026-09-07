"""Preflight: prove the machine is ready before Session 1, not during it.

Prints one PASS/FAIL table. Exit code is 1 if anything required failed.
Run with `make preflight`. Paste the table into the cohort channel 48 hours
before Session 1.

Checks:
- Python version and virtualenv
- uv, Docker, git
- the track's CLI is authenticated (aws sts get-caller-identity / gcloud auth list)
- one real round trip per model role on aws or gcp tracks (spends a fraction of a cent)
- the accelerator PyTorch would see (reported, not required; Session 3 has a fallback)
- free disk for model weights
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

REQUIRED_PY = (3, 12)
FREE_DISK_GB = 6


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def check_python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= REQUIRED_PY
    venv = sys.prefix != sys.base_prefix
    return Check(
        "python",
        ok and venv,
        f"{v.major}.{v.minor}.{v.micro}, virtualenv={'yes' if venv else 'no'}",
    )


def check_tool(name: str, args: list[str], required: bool = True) -> Check:
    code, out = _run([name, *args])
    return Check(name, code == 0, out.splitlines()[0] if out else "", required)


def check_disk() -> Check:
    free_gb = shutil.disk_usage(os.getcwd()).free / 1e9
    return Check(
        "free disk", free_gb >= FREE_DISK_GB, f"{free_gb:.1f} GB free, need {FREE_DISK_GB}"
    )


def check_accelerator() -> Check:
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_memory / 1e9
            return Check("accelerator", True, f"cuda: {name}, {mem:.0f} GB", required=False)
        if torch.backends.mps.is_available():
            return Check("accelerator", True, "mps (Apple silicon)", required=False)
        return Check("accelerator", True, "cpu only: Session 3 uses the Colab fallback", False)
    except ImportError:
        return Check("accelerator", True, "torch not installed yet (Session 3 installs it)", False)


def check_track_auth(track: str, settings) -> list[Check]:
    if track == "aws":
        code, out = _run(["aws", "sts", "get-caller-identity", "--output", "text"])
        return [Check("aws identity", code == 0, out.replace("\t", " ")[:100])]
    if track == "gcp":
        code, out = _run(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]
        )
        proj = settings.gcp_project or "(NW_GCP_PROJECT unset)"
        return [
            Check("gcloud account", code == 0 and bool(out), out[:80]),
            Check("gcp project", bool(settings.gcp_project), proj),
        ]
    return [Check("track", True, "local: fake provider, no cloud needed")]


async def check_round_trips(settings) -> list[Check]:
    from nw.config import ModelRole
    from nw.llm.client import LLMClient
    from nw.llm.providers import make_provider

    checks: list[Check] = []
    try:
        client = LLMClient(make_provider(settings), settings=settings, max_concurrency=1)
    except Exception as exc:  # noqa: BLE001
        return [Check("provider", False, f"{type(exc).__name__}: {exc}")]
    for role in ModelRole:
        model = settings.model_for(role)
        try:
            c = await client.complete("Reply with the single word: ready", role=role, max_tokens=8)
            checks.append(
                Check(
                    f"model {role.value}",
                    True,
                    f"{model}: {c.usage.latency_ms:.0f} ms, "
                    f"{c.usage.input_tokens}+{c.usage.output_tokens} tokens",
                )
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                Check(f"model {role.value}", False, f"{model}: {type(exc).__name__}: {exc}")
            )
    checks.append(Check("spend", True, f"{client.spend_usd:.5f} USD", required=False))
    return checks


def main() -> int:
    from nw.config import settings as load_settings

    settings = load_settings()
    track = settings.track.value
    checks: list[Check] = [
        check_python(),
        check_tool("uv", ["--version"]),
        check_tool("git", ["--version"]),
        check_tool("docker", ["version", "--format", "{{.Server.Version}}"]),
        check_disk(),
        check_accelerator(),
        *check_track_auth(track, settings),
    ]
    if track in {"aws", "gcp"}:
        checks.extend(asyncio.run(check_round_trips(settings)))

    width = max(len(c.name) for c in checks)
    print(f"\nPreflight, track={track}\n")
    for c in checks:
        mark = "PASS" if c.ok else ("WARN" if not c.required else "FAIL")
        print(f"  {mark}  {c.name:<{width}}  {c.detail}")
    failed = [c for c in checks if c.required and not c.ok]
    print()
    print("READY" if not failed else f"NOT READY: {', '.join(c.name for c in failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

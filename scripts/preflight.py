"""Preflight: prove the machine is ready before the first session, not during it.

Prints one PASS/FAIL table. Exit code is 1 if anything required failed.
Run with `make preflight`. Paste the table into the cohort channel 48 hours
before the first session.

Checks:
- Python version and virtualenv
- uv, Docker, git
- the track's CLI is authenticated (aws sts get-caller-identity / gcloud auth list); on azure,
  the Foundry endpoint, project, region and whether API Management is in the path, then an Entra
  ID token for https://ai.azure.com/.default from DefaultAzureCredential (or the key is set)
- per model role: the model id, the provider and endpoint it resolves to, and whether the
  model gateway is in the path (no network; `nw.llm.providers.make_provider` decides)
- one real round trip per model role on the aws and gcp tracks (spends a fraction of a
  cent) and on the local track against Ollama; NW_PROVIDER=fake skips them
- the accelerator PyTorch would see (reported, not required; the deep learning part has a fallback)
- free disk for model weights
- warned, not required: docker compose, huggingface.co reachable, application default
  credentials and terraform on gcp, node and cdk on aws, the az CLI on azure
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

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
    except OSError as exc:
        return 127, str(exc) if not isinstance(exc, FileNotFoundError) else f"{cmd[0]} not found"
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


def check_tool(name: str, args: list[str], required: bool = True, timeout: int = 30) -> Check:
    code, out = _run([*name.split(), *args], timeout=timeout)
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
        return Check(
            "accelerator", True, "cpu only: the deep learning part uses the Colab fallback", False
        )
    except ImportError:
        return Check(
            "accelerator",
            True,
            "torch not installed yet (the deep learning part installs it)",
            False,
        )


AZURE_SCOPE = "https://ai.azure.com/.default"


def check_azure(settings, credential: Any = None) -> list[Check]:
    """The Azure track line and the identity check. `credential` is any azure-identity
    credential; the default is DefaultAzureCredential (the `az login` session on a laptop).
    Tests pass a fake, so nothing here reaches the network in the suite."""
    endpoint = settings.azure_foundry_endpoint or ""
    apim = settings.azure_apim_gateway_url or ""
    fake = settings.provider.value == "fake"
    line = (
        f"foundry {endpoint or '(NW_AZURE_FOUNDRY_ENDPOINT unset)'}, "
        f"project {settings.azure_foundry_project or '(unset)'}, "
        f"region {settings.azure_location}, "
        f"apim {'yes, ' + apim if settings.uses_apim else 'no'}"
    )
    checks = [Check("azure track", fake or bool(endpoint or apim), line)]
    if settings.azure_foundry_key:
        checks.append(Check("azure identity", True, "NW_AZURE_FOUNDRY_KEY set: key auth"))
        return checks
    try:
        if credential is None:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        token = credential.get_token(AZURE_SCOPE)
        minutes = max(0, int((token.expires_on - time.time()) // 60))
        checks.append(
            Check(
                "azure identity",
                True,
                f"Entra ID token for {AZURE_SCOPE}, expires in {minutes} min",
                required=not fake,
            )
        )
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        checks.append(
            Check(
                "azure identity",
                False,
                f"no Entra ID token ({detail[:120]}); run az login or set NW_AZURE_FOUNDRY_KEY",
                required=not fake,
            )
        )
    return checks


def check_track_auth(track: str, settings, azure_credential: Any = None) -> list[Check]:
    if track == "azure":
        return check_azure(settings, azure_credential)
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
    if settings.provider.value == "fake":
        return [Check("track", True, "local: fake provider, no cloud needed")]
    return [Check("track", True, f"local: Ollama at {settings.ollama_url}, no cloud needed")]


def check_routes(settings, provider) -> list[Check]:
    """Per role: the model id, the provider and endpoint it resolves to, and whether the
    gateway is in the path. No network: this is what `make_provider` decided."""
    from nw.config import ModelRole
    from nw.llm.providers import describe_route

    gateway = "yes" if settings.uses_gateway else "no"
    return [
        Check(
            f"route {role.value}",
            True,
            f"{settings.model_for(role)} via {describe_route(provider, settings.model_for(role))}"
            f", gateway={gateway}",
            required=False,
        )
        for role in ModelRole
    ]


def make_provider_check(settings):
    """The provider for this configuration, or the Check that says why there is none."""
    from nw.llm.providers import make_provider

    try:
        return make_provider(settings), None
    except Exception as exc:  # noqa: BLE001
        return None, Check("provider", False, f"{type(exc).__name__}: {exc}")


async def check_round_trips(settings, provider) -> list[Check]:
    """One real completion per role, straight through the provider.

    Deliberately bypasses LLMClient: that class is the participant's work in
    the service layer and is a stub until they finish it.
    """
    from nw.config import ModelRole
    from nw.llm.cost import cost_usd
    from nw.llm.types import Message

    checks: list[Check] = []
    spend = 0.0
    for role in ModelRole:
        model = settings.model_for(role)
        # Running without a Judge is a supported path on Local: report it, do not fail on it.
        why = settings.judge_unavailable() if role is ModelRole.JUDGE else None
        if why:
            checks.append(Check(f"model {role.value}", True, f"{model}: {why}", required=False))
            continue
        try:
            c = await provider.complete(
                [Message.user("Reply with the single word: ready")],
                model=model,
                system=None,
                tools=None,
                max_tokens=8,
                temperature=None,
            )
            spend += cost_usd(model, c.usage)
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
    checks.append(Check("spend", True, f"{spend:.5f} USD", required=False))
    return checks


def check_hf_reachable() -> Check:
    """The deep learning part downloads a sentence-transformers checkpoint."""
    import urllib.error
    import urllib.request

    url = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return Check("huggingface.co", resp.status < 400, f"HTTP {resp.status}", False)
    except urllib.error.HTTPError as exc:
        return Check("huggingface.co", exc.code < 400, f"HTTP {exc.code}", False)
    except Exception as exc:  # noqa: BLE001
        return Check("huggingface.co", False, f"{type(exc).__name__}: {exc}", False)


def check_track_tools(track: str) -> list[Check]:
    """Toolchain the deploy part needs. Warned, not required, before the first session."""
    if track == "aws":
        return [
            check_tool("node", ["--version"], required=False),
            check_tool("npx cdk", ["--version"], required=False, timeout=120),
        ]
    if track == "azure":
        return [check_tool("az", ["version", "--query", '"azure-cli"', "-o", "tsv"], False, 60)]
    if track == "gcp":
        code, out = _run(["gcloud", "auth", "application-default", "print-access-token"])
        return [
            Check(
                "gcloud adc",
                code == 0 and bool(out),
                "application default credentials present" if code == 0 else out[:80],
                required=False,
            ),
            check_tool("terraform", ["version"], required=False),
        ]
    return []


def main() -> int:
    from nw.config import settings as load_settings

    settings = load_settings()
    track = settings.track.value
    checks: list[Check] = [
        check_python(),
        check_tool("uv", ["--version"]),
        check_tool("git", ["--version"]),
        check_tool("docker", ["version", "--format", "{{.Server.Version}}"]),
        check_tool("docker compose", ["version"], required=False),
        check_disk(),
        check_accelerator(),
        check_hf_reachable(),
        *check_track_auth(track, settings),
        *check_track_tools(track),
    ]
    provider, failure = make_provider_check(settings)
    if failure is not None:
        checks.append(failure)
    else:
        checks.extend(check_routes(settings, provider))
        if settings.provider.value != "fake":
            checks.extend(asyncio.run(check_round_trips(settings, provider)))

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

"""Start the three specialists and the orchestrator locally, one process each.

    uv run python -m nw.agent.launch

Ctrl-C stops all four. Ports: orchestrator 8010, triage 8011, policy 8012,
resolution 8013. Traces from every agent land in artifacts/traces.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

ROLES = {"triage": 8011, "policy": 8012, "resolution": 8013}


def main() -> int:
    procs: list[subprocess.Popen] = []
    env = dict(os.environ)
    for role, port in ROLES.items():
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "nw.agent.service:app", "--port", str(port)],
                env={**env, "NW_AGENT_ROLE": role},
            )
        )
    orch_env = {**env, "NW_AGENT_ROLE": "orchestrator"}
    for role, port in ROLES.items():
        orch_env[f"NW_{role.upper()}_AGENT_URL"] = f"http://127.0.0.1:{port}"
    procs.append(
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "nw.agent.service:app", "--port", "8010"],
            env=orch_env,
        )
    )
    print(
        "orchestrator :8010, triage :8011, policy :8012, resolution :8013. Ctrl-C to stop.",
        flush=True,
    )

    def stop(*_: object) -> None:
        for p in procs:
            p.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while any(p.poll() is None for p in procs):
            time.sleep(0.5)
    finally:
        stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

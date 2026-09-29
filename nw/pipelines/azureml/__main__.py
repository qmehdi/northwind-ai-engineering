"""Print an Azure ML pipeline definition as YAML, without calling Azure.

    uv run python -m nw.pipelines.azureml --pipeline triage --tenant alice \\
        --image <acr>/nw-pipelines:tag
"""

from __future__ import annotations

import argparse
import sys

from nw.pipelines import NAMES


def main(argv: list[str] | None = None) -> int:
    import yaml

    from nw.pipelines.azureml import AzureMLConfig, as_dict, definition
    from nw.platform.base import Tenant

    ap = argparse.ArgumentParser(prog="python -m nw.pipelines.azureml")
    ap.add_argument("--pipeline", choices=NAMES, default="triage")
    ap.add_argument("--tenant", default="solo")
    ap.add_argument("--environment", default="northwind")
    ap.add_argument("--image", default="nw-pipelines:latest")
    ap.add_argument("--compute", default="serverless")
    args = ap.parse_args(argv)
    config = AzureMLConfig(
        tenant=Tenant(args.tenant, args.environment), image=args.image, compute=args.compute
    )
    print(yaml.safe_dump(as_dict(definition(args.pipeline, config)), sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

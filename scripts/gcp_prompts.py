"""Register every course prompt for one tenant on the Google Cloud platform.

Run by Terraform (deploy/gcp/modules/prompts, a null_resource per tenant) at apply time and
by `make prompts-gcp`. There is no Terraform resource for prompt versions: the Gen AI SDK's
prompt management (`vertexai.preview.prompts`) creates the online version and
`nw.platform.gcp.VertexPromptStore` records the stage in the artifacts bucket.

    uv run python scripts/gcp_prompts.py --project p --region us-central1 --bucket b --tenant alice
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--bucket", required=True, help="artifacts bucket")
    ap.add_argument("--environment", default="northwind")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--dry-run", action="store_true", help="list the prompts, register nothing")
    args = ap.parse_args(argv)

    from nw.llm import prompts as registry

    registry.load_known()
    found = registry.registered()
    if args.dry_run:
        for p in found:
            print(f"{p.version}  {len(p.text)} chars")
        return 0

    os.environ.setdefault("NW_TRACK", "gcp")
    os.environ["NW_GCP_PROJECT"] = args.project
    os.environ["NW_GCP_RUN_REGION"] = args.region
    os.environ["NW_GCP_ARTIFACTS_BUCKET"] = args.bucket
    os.environ["NW_ENVIRONMENT"] = args.environment
    from nw.config import Settings, Track
    from nw.platform.base import Tenant
    from nw.platform.gcp import build

    platform = build(Settings(track=Track.GCP, gcp_project=args.project, _env_file=None))
    tenant = Tenant(name=args.tenant, environment=args.environment)
    for p in found:
        v = platform.prompts.register(tenant, p.name, p.text, {"source": "nw.llm.prompts"})
        print(f"registered {tenant.resource(p.name)}@{v.sha256_12} stage={v.stage}")
    print(f"{len(found)} prompts for tenant {args.tenant}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

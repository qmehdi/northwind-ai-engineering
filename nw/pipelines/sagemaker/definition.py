"""Print a pipeline definition as JSON, without touching AWS.

    python -m nw.pipelines.sagemaker.definition --pipeline triage --tenant alice \
        --role arn:aws:iam::123456789012:role/northwind-pipelines \
        --image 123456789012.dkr.ecr.us-east-1.amazonaws.com/nw-pipelines:latest --bucket nw-bucket
"""

from __future__ import annotations

import argparse
import json
import sys

from nw.pipelines.sagemaker.definitions import FACTORIES, SageMakerConfig, definition
from nw.platform.base import Tenant


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pipeline", choices=sorted(FACTORIES), required=True)
    ap.add_argument("--tenant", default="solo")
    ap.add_argument("--environment", default="northwind")
    ap.add_argument("--role", required=True, help="the pipeline execution role ARN")
    ap.add_argument("--image", required=True, help="the nw-pipelines image URI in ECR")
    ap.add_argument("--serving-image", default=None, help="the image the package serves with")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args(argv)
    config = SageMakerConfig(
        tenant=Tenant(name=args.tenant, environment=args.environment),
        role_arn=args.role,
        image_uri=args.image,
        serving_image_uri=args.serving_image,
        bucket=args.bucket,
        region=args.region,
    )
    print(json.dumps(definition(args.pipeline, config), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Create the artifact bucket on the object store if it is missing. Runs once per `up`."""

import os
import sys

import boto3
from botocore.exceptions import ClientError

endpoint = os.environ["MLFLOW_S3_ENDPOINT_URL"]
bucket = os.environ.get("NW_ARTIFACT_BUCKET", "mlflow")
s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
try:
    s3.head_bucket(Bucket=bucket)
    print(f"bucket {bucket} exists on {endpoint}")
except ClientError:
    s3.create_bucket(Bucket=bucket)
    print(f"created bucket {bucket} on {endpoint}")
sys.exit(0)

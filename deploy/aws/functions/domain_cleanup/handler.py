"""Remove what a SageMaker domain leaves behind when CloudFormation deletes it.

`DeleteDomain` from CloudFormation keeps the domain's home EFS file system (the API's
`RetentionPolicy.HomeEfsFileSystem` defaults to `Retain` and the resource has no property for
it), and the two security groups SageMaker created for NFS
(`security-group-for-inbound-nfs-<domain-id>`, `security-group-for-outbound-nfs-<domain-id>`)
stay in the VPC, so the VPC's deletion fails. This custom resource is deleted after the domain
and before the VPC; on Delete it removes the file systems SageMaker tagged with a domain of this
account whose mount targets sit in the VPC, then the NFS security groups. Create and Update do
nothing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)
TAG = "ManagedByAmazonSageMakerResource"
DEADLINE_S = 13 * 60


def on_event(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    vpc_id = event["ResourceProperties"]["VpcId"]
    if event["RequestType"] != "Delete":
        return {"PhysicalResourceId": f"domain-cleanup-{vpc_id}"}
    start = time.monotonic()
    efs = boto3.client("efs")
    ec2 = boto3.client("ec2")
    for fs in _domain_file_systems(efs, vpc_id):
        _delete_file_system(efs, fs, start)
    _delete_nfs_groups(ec2, vpc_id, start)
    return {"PhysicalResourceId": event.get("PhysicalResourceId", f"domain-cleanup-{vpc_id}")}


def _domain_file_systems(efs, vpc_id: str) -> list[str]:
    out = []
    for page in efs.get_paginator("describe_file_systems").paginate():
        for fs in page["FileSystems"]:
            tags = {t["Key"]: t["Value"] for t in fs.get("Tags", [])}
            if ":domain/" not in tags.get(TAG, ""):
                continue
            targets = efs.describe_mount_targets(FileSystemId=fs["FileSystemId"])["MountTargets"]
            if any(t.get("VpcId") == vpc_id for t in targets):
                out.append(fs["FileSystemId"])
    return out


def _delete_file_system(efs, fs_id: str, start: float) -> None:
    for target in efs.describe_mount_targets(FileSystemId=fs_id)["MountTargets"]:
        efs.delete_mount_target(MountTargetId=target["MountTargetId"])
    while efs.describe_mount_targets(FileSystemId=fs_id)["MountTargets"]:
        if time.monotonic() - start > DEADLINE_S:
            raise TimeoutError(f"mount targets of {fs_id} still deleting")
        time.sleep(10)
    efs.delete_file_system(FileSystemId=fs_id)
    log.info("deleted file system %s", fs_id)


def _delete_nfs_groups(ec2, vpc_id: str, start: float) -> None:
    groups = ec2.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["security-group-for-*-nfs-*"]},
        ]
    )["SecurityGroups"]
    # The two groups reference each other: revoke every rule first, then delete.
    for g in groups:
        if g.get("IpPermissions"):
            ec2.revoke_security_group_ingress(
                GroupId=g["GroupId"], IpPermissions=g["IpPermissions"]
            )
        if g.get("IpPermissionsEgress"):
            ec2.revoke_security_group_egress(
                GroupId=g["GroupId"], IpPermissions=g["IpPermissionsEgress"]
            )
    for g in groups:
        while True:
            try:
                ec2.delete_security_group(GroupId=g["GroupId"])
                log.info("deleted security group %s", g["GroupName"])
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "DependencyViolation":
                    raise
                if time.monotonic() - start > DEADLINE_S:
                    raise
                time.sleep(15)  # the file system's interfaces are still going away

"""Data and governance: the buckets, the key, the catalog.

- One KMS key for everything the platform stores (buckets, ECR, the trail, the pipeline artifacts).
- `data`: the tickets and policy corpus per tenant (`tenants/<tenant>/...`) and the shared
  training data (`data/...`). `artifacts`: model artifacts, MLflow, data capture, monitoring
  output, the gateway config. Both versioned, SSL only, access-logged to `logs`.
- A Glue Data Catalog database with the tickets table (JSON lines under `data/tickets/`), so
  Athena and the pipelines' processing steps read the same schema the repo's data contract has.
- Lake Formation registration of the data bucket is off by default (`-c lakeFormation=true`).
  Registering a location moves table access from IAM to Lake Formation grants: every tenant role
  would then need `lakeformation:GetDataAccess` and a grant per table, and the CDK deploy role
  becomes a data lake administrator. `HybridAccessEnabled` keeps IAM working alongside, which is
  what the course turns on when it teaches the permission model; the default is IAM only.
- `ops`: the durable operations state of the agents and the policy service (`nw/agent/opstore.py`,
  `NW_OPS_STORE=s3://<ops bucket>`): trajectories, approvals (claim markers, approval records,
  the escalation queue) and feedback under `<environment>-<owner>/<kind>/`. A bucket of its own
  because nothing else lives there: every runtime identity reaches its own owner prefix only
  (`grant_ops`), and the lifecycle rules are exactly the retention table's, per owner prefix (an
  S3 rule's prefix cannot wildcard the owner).
- Retention (`RETENTION`, the classes in `docs/governance`): Operational 90 days (endpoint data
  capture, trajectories, feedback, traces, monitoring output, MLflow run artifacts, pipeline
  artifacts); Audit 400 days (approvals, escalations, S3 access logs, the trail, ALB and
  CloudFront logs, Bedrock invocation metadata); application and runtime logs 30 days;
  AgentCore Memory events 30 days; superseded object versions 30 days; database backups 7 days. Lifecycle rules enforce it, so the promise in
  `data/policies/data-retention.md` is a bucket setting and not a runbook step.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lakeformation as lakeformation
from aws_cdk import aws_s3 as s3
from constructs import Construct

TICKET_COLUMNS = [
    ("ticket_id", "string"),
    ("account_id", "string"),
    ("created_at", "string"),
    ("subject", "string"),
    ("body", "string"),
    ("language", "string"),
    ("type", "string"),
    ("queue", "string"),
    ("priority", "string"),
    ("tags", "array<string>"),
    ("reference_answer", "string"),
]


# Days an object lives, per store. Prefixes are in the artifacts bucket unless noted.
RETENTION = {
    # Operational
    "capture/": 90,  # SageMaker data capture of the live endpoints (requests carry ticket text)
    "traces/": 90,  # exported traces
    "monitoring/": 90,  # Model Monitor reports and the baselines copied for them
    "mlflow/": 90,  # MLflow run artifacts (registered models live under tenants/ and live/)
    "operational": 90,  # the delivery pipeline's artifacts bucket
    # Audit
    "audit": 400,  # access logs, CloudTrail, ALB and CloudFront logs, invocation metadata
    # Versions
    "noncurrent": 30,  # superseded versions in the versioned buckets
}
OPERATIONAL_PREFIXES = ("capture/", "traces/", "monitoring/", "mlflow/")
# The ops store's kinds (`nw.agent.opstore.store_for`) and their days, per owner prefix of the
# ops bucket: trajectories and feedback are Operational, approvals (with the claim markers and
# the escalation queue) Audit.
OPS_KINDS = {"trajectories": 90, "feedback": 90, "approvals": 400}
# Who writes which kind (nw/agent/approve.py, nw/agent/northwind.py): a runtime (the agent, the
# policy service, the MCP tools) writes trajectories, which carry the proposals, and feedback;
# the approver writes the claim markers, the approval records and the escalation queue under
# approvals/, and the approval's own trajectory. The runtime never reads approvals/: the
# escalate tool's body (dedupe and queue) runs only in the approver's process.
RUNTIME_KINDS = ("trajectories", "feedback")
APPROVER_KINDS = ("trajectories", "feedback", "approvals")


class DataGovernance(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        owners: list[str],
        lake_formation: bool = False,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.prefix = prefix
        suffix = f"{stack.account}-{stack.region}"
        self.key = kms.Key(
            self,
            "Key",
            alias=f"alias/{prefix}-platform",
            description=f"{prefix} platform data key",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Server access logs for every other bucket. It cannot log to itself, and the target of
        # access logs must be SSE-S3 (S3 writes them with the service's own identity).
        self.logs = s3.Bucket(
            self,
            "AccessLogs",
            bucket_name=f"{prefix}-logs-{suffix}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(id="audit", expiration=Duration.days(RETENTION["audit"]))
            ],
        )
        self.data = self._bucket("Data", f"{prefix}-data-{suffix}", [])
        self.artifacts = self._bucket(
            "Artifacts",
            f"{prefix}-artifacts-{suffix}",
            [
                s3.LifecycleRule(
                    id=key.strip("/"), prefix=key, expiration=Duration.days(RETENTION[key])
                )
                for key in OPERATIONAL_PREFIXES
            ],
        )
        self.ops = self._bucket(
            "Ops",
            f"{prefix}-ops-{suffix}",
            [
                s3.LifecycleRule(
                    id=f"{owner}-{kind}",
                    prefix=f"{self.ops_prefix(owner)}{kind}/",
                    expiration=Duration.days(days),
                )
                for owner in owners
                for kind, days in OPS_KINDS.items()
            ],
        )
        # What `NW_OPS_STORE` is set to: the bucket alone, so a key is
        # `<environment>-<owner>/<kind>/<name>` (NW_ENVIRONMENT is the stack prefix).
        self.ops_uri = f"s3://{self.ops.bucket_name}"

        db_name = f"{prefix.replace('-', '_')}_support"
        self.database = glue.CfnDatabase(
            self,
            "Catalog",
            catalog_id=stack.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=db_name, description="Northwind support tickets and policies"
            ),
        )
        self.tickets_table = glue.CfnTable(
            self,
            "Tickets",
            catalog_id=stack.account,
            database_name=db_name,
            table_input=glue.CfnTable.TableInputProperty(
                name="tickets",
                description="Synthetic support tickets, one JSON object per line (data/tickets.jsonl)",
                table_type="EXTERNAL_TABLE",
                parameters={"classification": "json", "nw:contract": "nw.triage.data_check"},
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    columns=[
                        glue.CfnTable.ColumnProperty(name=n, type=t) for n, t in TICKET_COLUMNS
                    ],
                    location=f"s3://{self.data.bucket_name}/data/tickets/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe",
                        parameters={"ignore.malformed.json": "true"},
                    ),
                ),
            ),
        )
        self.tickets_table.add_resource_dependency(self.database)

        self.lake_formation = None
        if lake_formation:
            self.lake_formation = lakeformation.CfnResource(
                self,
                "LakeFormationData",
                resource_arn=self.data.bucket_arn,
                use_service_linked_role=True,
                hybrid_access_enabled=True,
            )

    def ops_prefix(self, owner: str) -> str:
        """`<environment>-<owner>/`, the prefix `nw.agent.opstore.tenant_prefix` computes."""
        return f"{self.prefix}-{owner}/"

    def grant_ops(
        self, role: iam.IRole, owner: str, kinds: tuple[str, ...] = RUNTIME_KINDS
    ) -> None:
        """Read and write some kinds of one owner's ops prefix and nothing else of the bucket:
        list only under them, get and put only under them (a put with `If-None-Match` is still a
        put), no delete (erasure is the platform owner's, docs/governance). The key through S3
        only. A runtime gets `RUNTIME_KINDS`; `approvals/` (claims, approval records, the
        escalation queue) is the approver's (`APPROVER_KINDS`), the data-plane half of ADR 0005:
        an agent that talked its way past the loop's gate still cannot queue an escalation."""
        top = self.ops_prefix(owner)
        sid = "".join(p.capitalize() for p in owner.split("-"))
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid=f"OpsList{sid}",
                actions=["s3:ListBucket"],
                resources=[self.ops.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [p for k in kinds for p in (f"{top}{k}/", f"{top}{k}/*")]
                    }
                },
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid=f"OpsReadWrite{sid}",
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[self.ops.arn_for_objects(f"{top}{k}/*") for k in kinds],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid=f"OpsKey{sid}",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self.key.key_arn],
                conditions={
                    "StringEquals": {"kms:ViaService": f"s3.{Stack.of(self).region}.amazonaws.com"}
                },
            )
        )

    def _bucket(self, id: str, name: str, rules: list[s3.LifecycleRule]) -> s3.Bucket:
        return s3.Bucket(
            self,
            id,
            bucket_name=name,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            enforce_ssl=True,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            server_access_logs_bucket=self.logs,
            server_access_logs_prefix=f"{id.lower()}/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                *rules,
                s3.LifecycleRule(
                    id="noncurrent",
                    noncurrent_version_expiration=Duration.days(RETENTION["noncurrent"]),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )

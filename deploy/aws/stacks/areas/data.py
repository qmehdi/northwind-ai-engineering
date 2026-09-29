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
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_glue as glue
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


class DataGovernance(Construct):
    def __init__(
        self, scope: Construct, id: str, *, prefix: str, lake_formation: bool = False
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
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
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )
        self.data = self._bucket("Data", f"{prefix}-data-{suffix}")
        self.artifacts = self._bucket("Artifacts", f"{prefix}-artifacts-{suffix}")

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

    def _bucket(self, id: str, name: str) -> s3.Bucket:
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
        )

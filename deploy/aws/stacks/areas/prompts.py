"""Prompts and retrieval: Bedrock Prompt Management and one Knowledge Base per tenant.

Prompts: every prompt in the repo registry (`nw/llm/prompts`, exported to
`deploy/aws/prompts/catalog.json` by `python -m nw.platform.aws prompts-catalog`) becomes a
Bedrock prompt with one TEXT variant and a snapshot version. The registry's `sha256_12` travels
as a tag and in the version description, so `prompt_version` in an answer, a span or an eval
row can be matched to the managed prompt.

Retrieval: an S3 Vectors bucket for the platform, one index per tenant (plus `live`), one
Knowledge Base per index with an S3 data source under `tenants/<tenant>/policies/`. The
index dimension is the embedding model's (Titan Text v2, 1024) and the two Bedrock metadata keys
are non-filterable, as the knowledge base requires (fetched 2026-09-29).
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3v
from constructs import Construct

from stacks.common import EMBEDDING_DIM, EMBEDDING_MODEL, LIVE

CATALOG = Path(__file__).resolve().parents[2] / "prompts" / "catalog.json"


def load_catalog(path: Path = CATALOG) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing: run `uv run python -m nw.platform.aws prompts-catalog` first"
        )
    rows = json.loads(path.read_text())
    for row in rows:
        for k in ("name", "sha256_12", "text"):
            if not row.get(k):
                raise ValueError(f"catalog row without {k}: {row}")
    return rows


class PromptsRetrieval(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        tenants: list[str],
        data: s3.IBucket,
        key: kms.IKey,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        embedding_arn = f"arn:aws:bedrock:{stack.region}::foundation-model/{EMBEDDING_MODEL}"

        # ----- prompts -----
        self.prompts: dict[str, bedrock.CfnPrompt] = {}
        for row in load_catalog():
            slug = row["name"].replace(".", "-").replace("_", "-")
            prompt = bedrock.CfnPrompt(
                self,
                f"Prompt{''.join(p.title() for p in slug.split('-'))}",
                name=f"{prefix}-{slug}",
                description=f"{row['name']}@{row['sha256_12']}",
                default_variant="default",
                variants=[
                    bedrock.CfnPrompt.PromptVariantProperty(
                        name="default",
                        template_type="TEXT",
                        template_configuration=bedrock.CfnPrompt.PromptTemplateConfigurationProperty(
                            text=bedrock.CfnPrompt.TextPromptTemplateConfigurationProperty(
                                text=row["text"]
                            )
                        ),
                    )
                ],
                tags={"nw:prompt": row["name"], "nw:sha256_12": row["sha256_12"]},
            )
            bedrock.CfnPromptVersion(
                self,
                f"PromptVersion{''.join(p.title() for p in slug.split('-'))}",
                prompt_arn=prompt.attr_arn,
                description=row["sha256_12"],
            )
            self.prompts[row["name"]] = prompt

        # ----- vectors and knowledge bases -----
        self.vector_bucket = s3v.CfnVectorBucket(
            self,
            "Vectors",
            vector_bucket_name=f"{prefix}-vectors-{stack.account}-{stack.region}",
        )
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            role_name=f"{prefix}-knowledge-base",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock:{stack.region}:{stack.account}:knowledge-base/*"
                    },
                },
            ),
            description="Bedrock Knowledge Bases: embed the policy corpus into the tenant indexes",
        )
        # The statements are the ones on the kb-permissions devguide page (fetched 2026-09-29).
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="EmbeddingModel", actions=["bedrock:InvokeModel"], resources=[embedding_arn]
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3ListBucketStatement",
                actions=["s3:ListBucket"],
                resources=[data.bucket_arn],
                conditions={"StringEquals": {"aws:ResourceAccount": stack.account}},
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3GetObjectStatement",
                actions=["s3:GetObject"],
                resources=[data.arn_for_objects("tenants/*/policies/*")],
                conditions={"StringEquals": {"aws:ResourceAccount": stack.account}},
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="KmsDecryptStatement",
                actions=["kms:Decrypt"],
                resources=[key.key_arn],
                conditions={"StringEquals": {"kms:ViaService": f"s3.{stack.region}.amazonaws.com"}},
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3VectorBucketReadAndWritePermission",
                actions=[
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetIndex",
                ],
                resources=[
                    f"arn:aws:s3vectors:{stack.region}:{stack.account}:bucket/{self.vector_bucket.vector_bucket_name}/index/*"
                ],
            )
        )
        self.kb_role = kb_role
        self.indexes: dict[str, s3v.CfnIndex] = {}
        self.knowledge_bases: dict[str, bedrock.CfnKnowledgeBase] = {}
        self.data_sources: dict[str, bedrock.CfnDataSource] = {}
        for owner in [*tenants, LIVE]:
            index = s3v.CfnIndex(
                self,
                f"Index{owner.title()}",
                vector_bucket_name=self.vector_bucket.vector_bucket_name,
                index_name=f"{owner}-policies",
                data_type="float32",
                dimension=EMBEDDING_DIM,
                distance_metric="cosine",
                metadata_configuration=s3v.CfnIndex.MetadataConfigurationProperty(
                    non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
                ),
            )
            index.add_resource_dependency(self.vector_bucket)
            kb = bedrock.CfnKnowledgeBase(
                self,
                f"KnowledgeBase{owner.title()}",
                name=f"{prefix}-{owner}-policies",
                description=f"Northwind policy corpus for {owner}",
                role_arn=kb_role.role_arn,
                knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                    type="VECTOR",
                    vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                        embedding_model_arn=embedding_arn
                    ),
                ),
                storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                    type="S3_VECTORS",
                    s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                        index_arn=index.attr_index_arn
                    ),
                ),
                tags={"nw:tenant": owner},
            )
            kb.add_resource_dependency(index)
            kb.node.add_dependency(kb_role)
            source = bedrock.CfnDataSource(
                self,
                f"DataSource{owner.title()}",
                name=f"{prefix}-{owner}-policy-corpus",
                knowledge_base_id=kb.attr_knowledge_base_id,
                data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                    type="S3",
                    s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                        bucket_arn=data.bucket_arn,
                        inclusion_prefixes=[f"tenants/{owner}/policies/"],
                    ),
                ),
                data_deletion_policy="DELETE",
                vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                    chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                        chunking_strategy="FIXED_SIZE",
                        fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                            max_tokens=300, overlap_percentage=20
                        ),
                    )
                ),
            )
            self.indexes[owner] = index
            self.knowledge_bases[owner] = kb
            self.data_sources[owner] = source
        CfnOutput(
            self, "OutVectorBucket", value=self.vector_bucket.vector_bucket_name
        ).override_logical_id("VectorBucket")
        CfnOutput(
            self, "OutKnowledgeBaseLive", value=self.knowledge_bases[LIVE].attr_knowledge_base_id
        ).override_logical_id("KnowledgeBaseLive")
        CfnOutput(
            self,
            "OutKnowledgeBases",
            value=",".join(
                f"{o}={kb.attr_knowledge_base_id}" for o, kb in self.knowledge_bases.items()
            ),
        ).override_logical_id("KnowledgeBases")

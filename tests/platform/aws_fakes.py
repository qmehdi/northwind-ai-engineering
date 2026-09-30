"""Stateful fakes of the SageMaker model registry, S3 and Bedrock Prompt Management: what the
AWS client asks of them, remembered across calls, so behaviour (stages, idempotency, order) is
tested the same way as on the other tracks. The Stubber tests in `test_aws_platform.py` hold
the exact request shapes; these hold the semantics."""

from __future__ import annotations

import copy
import io
from typing import Any

REGION = "us-east-1"
ACCOUNT = "123456789012"


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> dict:  # noqa: N803
        self.objects[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")
        return {}


class FakeSageMaker:
    """Model package groups with numbered packages, approval status and metadata."""

    def __init__(self) -> None:
        self.groups: dict[str, list[dict[str, Any]]] = {}
        self.updates: list[dict[str, Any]] = []

    def create_model_package(self, **kw: Any) -> dict:
        group = kw["ModelPackageGroupName"]
        packages = self.groups.setdefault(group, [])
        number = len(packages) + 1
        arn = f"arn:aws:sagemaker:{REGION}:{ACCOUNT}:model-package/{group}/{number}"
        packages.append(
            {
                "ModelPackageGroupName": group,
                "ModelPackageVersion": number,
                "ModelPackageArn": arn,
                "ModelApprovalStatus": kw.get("ModelApprovalStatus", "PendingManualApproval"),
                "CustomerMetadataProperties": dict(kw.get("CustomerMetadataProperties") or {}),
                "InferenceSpecification": copy.deepcopy(kw["InferenceSpecification"]),
                "CreationTime": number,
            }
        )
        return {"ModelPackageArn": arn}

    def _find(self, arn: str) -> dict[str, Any]:
        for packages in self.groups.values():
            for p in packages:
                if p["ModelPackageArn"] == arn:
                    return p
        raise ClientError("ValidationException")

    def describe_model_package(self, ModelPackageName: str) -> dict:  # noqa: N803
        return copy.deepcopy(self._find(ModelPackageName))

    def update_model_package(self, **kw: Any) -> dict:
        self.updates.append(kw)
        package = self._find(kw["ModelPackageArn"])
        package["ModelApprovalStatus"] = kw["ModelApprovalStatus"]
        package["CustomerMetadataProperties"] = dict(kw["CustomerMetadataProperties"])
        return {"ModelPackageArn": kw["ModelPackageArn"]}

    def list_model_packages(self, ModelPackageGroupName: str, **kw: Any) -> dict:  # noqa: N803
        packages = list(self.groups.get(ModelPackageGroupName, []))
        if kw.get("SortOrder") == "Descending":
            packages.reverse()
        return {
            "ModelPackageSummaryList": [
                {
                    "ModelPackageArn": p["ModelPackageArn"],
                    "ModelPackageVersion": p["ModelPackageVersion"],
                }
                for p in packages
            ]
        }


class FakeBedrockAgent:
    """Bedrock Prompt Management: prompts, numbered versions, tags on any ARN."""

    def __init__(self) -> None:
        self.prompts: dict[str, dict[str, Any]] = {}  # id -> prompt
        self.tags: dict[str, dict[str, str]] = {}
        self.created_versions = 0

    def _arn(self, prompt_id: str, version: str | None = None) -> str:
        base = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:prompt/{prompt_id}"
        return base if version in (None, "DRAFT") else f"{base}:{version}"

    def list_prompts(self, maxResults: int = 100, promptIdentifier: str | None = None, **kw: Any):  # noqa: N803
        if promptIdentifier is None:
            return {
                "promptSummaries": [
                    {"name": p["name"], "id": i, "arn": self._arn(i), "version": "DRAFT"}
                    for i, p in self.prompts.items()
                ]
            }
        p = self.prompts[promptIdentifier]
        rows = [{"version": "DRAFT", "arn": self._arn(promptIdentifier)}]
        rows += [
            {"version": v, "arn": self._arn(promptIdentifier, v)} for v in sorted(p["versions"])
        ]
        return {"promptSummaries": rows}

    def create_prompt(self, **kw: Any) -> dict:
        prompt_id = f"P{len(self.prompts) + 1:09d}"
        self.prompts[prompt_id] = {"name": kw["name"], "draft": kw["variants"], "versions": {}}
        self.tags[self._arn(prompt_id)] = dict(kw.get("tags") or {})
        return {"id": prompt_id, "arn": self._arn(prompt_id), "version": "DRAFT"}

    def update_prompt(self, promptIdentifier: str, **kw: Any) -> dict:  # noqa: N803
        self.prompts[promptIdentifier]["draft"] = kw["variants"]
        return {"id": promptIdentifier}

    def create_prompt_version(self, promptIdentifier: str, **kw: Any) -> dict:  # noqa: N803
        p = self.prompts[promptIdentifier]
        number = str(len(p["versions"]) + 1)
        p["versions"][number] = copy.deepcopy(p["draft"])
        self.created_versions += 1
        arn = self._arn(promptIdentifier, number)
        self.tags[arn] = dict(kw.get("tags") or {})
        return {"version": number, "arn": arn}

    def get_prompt(self, promptIdentifier: str, promptVersion: str = "DRAFT") -> dict:  # noqa: N803
        p = self.prompts[promptIdentifier]
        variants = p["draft"] if promptVersion == "DRAFT" else p["versions"][promptVersion]
        return {
            "version": promptVersion,
            "arn": self._arn(promptIdentifier, promptVersion),
            "variants": copy.deepcopy(variants),
        }

    def tag_resource(self, resourceArn: str, tags: dict[str, str]) -> dict:  # noqa: N803
        self.tags.setdefault(resourceArn, {}).update(tags)
        return {}

    def list_tags_for_resource(self, resourceArn: str) -> dict:  # noqa: N803
        return {"tags": dict(self.tags.get(resourceArn, {}))}

"""The platform layer: one contract, one implementation per track (ADR 0008, 0011).

Every service and every pipeline step talks to the platform through the protocols in
`nw.platform.base` and never imports a cloud SDK directly. `platform_for(settings)` picks the
implementation from `NW_TRACK`: `aws` (SageMaker, Bedrock, AgentCore), `gcp` (the Agent
Platform, formerly Vertex AI), `azure` (Azure Machine Learning, Microsoft Foundry, AI Search),
`local` (MLflow, Kubeflow local runner, Qdrant, Ollama, LiteLLM). The stage rules every
implementation keeps are in `nw.platform.base`; `nw.platform.bootstrap` is the mid-course
recovery and `nw.platform.lineage` what a registered version records about its code.
"""

from __future__ import annotations

from nw.platform.base import (
    AgentRuntime,
    EndpointClient,
    ModelRegistry,
    PipelineRunner,
    Platform,
    PromptStore,
    Tenant,
    VectorStore,
    platform_for,
)

__all__ = [
    "AgentRuntime",
    "EndpointClient",
    "ModelRegistry",
    "PipelineRunner",
    "Platform",
    "PromptStore",
    "Tenant",
    "VectorStore",
    "platform_for",
]

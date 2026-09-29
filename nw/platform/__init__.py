"""The platform layer: one contract, one implementation per track (ADR 0008, 0011).

Every service and every pipeline step talks to the platform through the protocols in
`nw.platform.base` and never imports a cloud SDK directly. `platform_for(settings)` picks the
implementation from `NW_TRACK`: `aws` (SageMaker, Bedrock, AgentCore), `gcp` (the Agent
Platform, formerly Vertex AI), `local` (MLflow, Kubeflow local runner, Qdrant, Ollama, LiteLLM).
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

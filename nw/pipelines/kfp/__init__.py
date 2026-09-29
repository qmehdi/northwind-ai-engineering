"""Kubeflow Pipelines SDK v2 definitions: the Local track and Vertex AI Pipelines.

    python -m nw.pipelines.kfp.compile --image nw-pipelines:latest   # artifacts/pipelines/*.yaml
    python -m nw.pipelines.kfp.run_local --pipeline triage --runner subprocess

Every component is a lightweight Python component whose body does one thing: run
`python -m nw.pipelines.steps.<name>` in the course image and read the step's result file.
That is what makes one definition serve three runtimes: the Kubeflow local runner with
`DockerRunner` (the course image), the local runner with `SubprocessRunner` (the tests, in the
checkout's own interpreter), and Vertex AI Pipelines (the compiled YAML, submitted by the
Google Cloud platform). Container components would be the direct way to run an image, but the
local SubprocessRunner cannot execute them (kfp docs, 2024-12-16), and the tests must.

`dsl.If` is not used: the local runner does not support it either. The register step reads
the gate decision itself and fails the run, with the reasons, when the gate did not pass.
"""

from __future__ import annotations

from nw.pipelines.kfp.pipelines import compile_all, semantic_pipeline, triage_pipeline

__all__ = ["compile_all", "semantic_pipeline", "triage_pipeline"]

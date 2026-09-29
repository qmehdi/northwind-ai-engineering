"""Model Monitoring v2 on a live Vertex endpoint, created and removed by Terraform
(deploy/gcp/modules/live) because the google provider (8.4.0) has no resource for it.

    uv run python scripts/gcp_model_monitor.py create --project p --region r --endpoint 100000 \
        --model-display-name northwind-live-triage --training-uri gs://.../tickets.jsonl \
        --output-uri gs://.../monitoring/triage
    uv run python scripts/gcp_model_monitor.py delete --project p --region r \
        --model-display-name northwind-live-triage

`create` is a no-op until the endpoint has a deployed model version tagged `live` (the first
promotion), so a fresh apply succeeds; rerun it after the drill, or let the promotion step do
it. The SDK calls are `vertexai.resources.preview.ml_monitoring.ModelMonitor.create` with a
`ModelMonitoringSchema`, a `TabularObjective` with a `DataDriftSpec`, and `create_schedule`
with a weekly cron; anomalies land in Cloud Logging under
`aiplatform.googleapis.com/model_monitoring_anomalies`, which the observability module alarms on.
"""

from __future__ import annotations

import argparse
import sys

# Fields Project 1 trains on (nw/triage): the schema tells the monitor what to compare.
FEATURES = {"subject": "string", "body": "string", "language": "categorical"}
PREDICTIONS = {"type": "categorical", "queue": "categorical", "priority": "categorical"}


def _live_model(aiplatform, display_name: str):
    models = aiplatform.Model.list(filter=f'display_name="{display_name}"')
    if not models:
        return None
    for v in models[0].versioning_registry.list_versions():
        if "live" in (v.version_aliases or []):
            return models[0], v.version_id
    return None


def create(args: argparse.Namespace) -> int:
    from google.cloud import aiplatform

    aiplatform.init(project=args.project, location=args.region)
    live = _live_model(aiplatform, args.model_display_name)
    if live is None:
        print(
            f"no live version of {args.model_display_name} yet; monitor not created "
            "(rerun after the promotion drill)"
        )
        return 0
    model, version_id = live
    try:
        from vertexai.resources.preview import ml_monitoring
    except ImportError:
        print(
            "google-cloud-aiplatform without vertexai.resources.preview.ml_monitoring; "
            "install the platform-gcp extra"
        )
        return 1

    existing = [
        m for m in ml_monitoring.ModelMonitor.list() if m.display_name == args.model_display_name
    ]
    if existing:
        print(f"monitor exists: {existing[0].resource_name}")
        return 0
    schema = ml_monitoring.spec.ModelMonitoringSchema(
        feature_fields=[
            ml_monitoring.spec.FieldSchema(name=k, data_type=t) for k, t in FEATURES.items()
        ],
        prediction_fields=[
            ml_monitoring.spec.FieldSchema(name=k, data_type=t) for k, t in PREDICTIONS.items()
        ],
    )
    monitor = ml_monitoring.ModelMonitor.create(
        project=args.project,
        location=args.region,
        display_name=args.model_display_name,
        model_name=model.resource_name,
        model_version_id=version_id,
        training_dataset=ml_monitoring.spec.MonitoringInput(
            gcs_uri=args.training_uri, data_format="jsonl"
        ),
        model_monitoring_schema=schema,
        tabular_objective_spec=ml_monitoring.spec.TabularObjective(
            feature_drift_spec=ml_monitoring.spec.DataDriftSpec(
                default_categorical_alert_threshold=0.3
            )
        ),
        output_spec=ml_monitoring.spec.OutputSpec(gcs_base_dir=args.output_uri),
    )
    monitor.create_schedule(
        display_name=f"{args.model_display_name}-weekly",
        cron="0 7 * * 1",
        target_dataset=ml_monitoring.spec.MonitoringInput(
            vertex_dataset=None,
            gcs_uri=None,
            endpoints=[args.endpoint],
        ),
    )
    print(f"created {monitor.resource_name} with a weekly schedule on endpoint {args.endpoint}")
    return 0


def delete(args: argparse.Namespace) -> int:
    from google.cloud import aiplatform

    aiplatform.init(project=args.project, location=args.region)
    try:
        from vertexai.resources.preview import ml_monitoring
    except ImportError:
        print("SDK missing; nothing to delete")
        return 0
    for m in ml_monitoring.ModelMonitor.list():
        if m.display_name == args.model_display_name:
            m.delete(force=True)
            print(f"deleted {m.resource_name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["create", "delete"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--model-display-name", required=True)
    ap.add_argument("--training-uri", default="")
    ap.add_argument("--output-uri", default="")
    args = ap.parse_args(argv)
    return create(args) if args.action == "create" else delete(args)


if __name__ == "__main__":
    sys.exit(main())

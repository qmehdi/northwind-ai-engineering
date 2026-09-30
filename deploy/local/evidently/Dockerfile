# Evidently: drift reports over the services' capture files, the open-source counterpart of
# SageMaker Model Monitor and Vertex Model Monitoring (ADR 0011). One image, two roles:
# the UI (`evidently ui`) and the scheduled report job (run.sh). Base digest is the one the
# course Dockerfile pins (fetched 2026-09-28).
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
# Slow or flaky networks: longer timeouts and retries for every index and wheel request.
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
RUN pip install --no-cache-dir evidently==0.7.23 && useradd --create-home --uid 10001 nw \
    && mkdir -p /workspace /reports && chown -R nw:nw /workspace /reports
COPY report.py run.sh /app/
RUN chmod +x /app/run.sh
USER nw
WORKDIR /app

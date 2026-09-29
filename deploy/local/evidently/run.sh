#!/bin/sh
# The scheduled drift job: one report per capture file every NW_DRIFT_EVERY_S seconds (15
# minutes by default), written to the shared workspace the UI reads and as HTML under
# /reports. The heartbeat file is what the health check watches.
set -u
: "${NW_DRIFT_EVERY_S:=900}"
while true; do
  python /app/report.py || echo "drift report failed; retrying next cycle"
  date +%s > /workspace/.heartbeat
  sleep "$NW_DRIFT_EVERY_S"
done

"""The `quality_alert` log line: the quality canary's contract with the platform.

`drift_alert` says the inputs or predictions moved. `quality_alert` says a signal a release
bakes on crossed its bar: the shadow model disagrees with the served one, the predicted P0
share left its range, the refusal rate doubled, or sampled Judge scores fell. The monitors in
`nw/*/monitor.py` call `QualityAlerter.check` with each snapshot; the line carries `signal`,
`value`, `bar`, `window`, `service` and the tenant and environment, so the platform's log
metric (Google Cloud `<env>-quality-alerts`, a CloudWatch metric filter, a Grafana rule)
alarms per signal and per tenant.

One line per signal per `interval_s` (default 60) per process: a snapshot is computed every
few requests and on every `/drift` call, and an alarm needs a line, not a flood of them.

| signal | services | bar |
| --- | --- | --- |
| `shadow_agreement` | triage, semantic | below 0.90 over at least 200 shadowed requests |
| `p0_share` | triage, semantic | ratio to the validation share of 2 (or 0.5), outside the 99% CI |
| `refusal_rate` | policy | ratio to the baseline of 2 or above, outside the 99% CI |
| `judge_score` | agent | mean of sampled turns below 3.5 over at least 20 judged runs |
"""

from __future__ import annotations

import time
from collections.abc import Callable

from nw.logging import get_logger, log_fields

log = get_logger("nw.quality")
SIGNALS = ("shadow_agreement", "p0_share", "refusal_rate", "judge_score")


class QualityAlerter:
    """Emits `quality_alert` for a signal in alert, at most once per interval."""

    def __init__(
        self, service: str, interval_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.service = service
        self.interval_s = interval_s
        self.clock = clock
        self.last: dict[str, float] = {}

    def check(
        self, signal: str, alert: bool, *, value: float | None, bar: float, window: int
    ) -> bool:
        """Log the line when `alert` holds and the interval has passed. True when logged."""
        if not alert:
            return False
        now = self.clock()
        if now - self.last.get(signal, -1e18) < self.interval_s:
            return False
        self.last[signal] = now
        from nw.serving.identity import identity

        log.warning(
            "quality_alert",
            extra=log_fields(
                signal=signal,
                value=value,
                bar=bar,
                window=window,
                service=self.service,
                **identity(),
            ),
        )
        return True

"""Prometheus text exposition for the metrics the SRE questions actually need.

Exposed series:

* ``incidents_open``          gauge  — unresolved incidents right now
* ``incident_ack_seconds``    histogram — MTTD distribution (open -> ack)
* ``mttr_seconds``            gauge  — mean MTTR over resolved incidents
* ``incidents_total``         counter{severity,service} — lifetime volume

Everything is computed from the ``incidents`` table, whose timestamps come from
the alert payloads and the recorded transitions — no metric is synthesised.
"""

from __future__ import annotations

from typing import Iterable, List

from .store import MetricsSnapshot

ACK_BUCKETS = (30.0, 60.0, 300.0, 900.0, 1800.0, 3600.0, 14400.0)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _float(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)


def render_metrics(snapshot: MetricsSnapshot) -> str:
    lines: List[str] = []

    lines.append("# HELP incidents_open Incidents that are neither acknowledged nor resolved.")
    lines.append("# TYPE incidents_open gauge")
    lines.append("incidents_open %d" % snapshot.open_incidents)
    lines.append(
        "# HELP incidents_acknowledged Incidents currently waiting to be resolved after ack."
    )
    lines.append("# TYPE incidents_acknowledged gauge")
    lines.append("incidents_acknowledged %d" % snapshot.acknowledged_incidents)

    lines.append(
        "# HELP incident_ack_seconds Seconds between an incident opening and its acknowledgement "
        "(MTTD)."
    )
    lines.append("# TYPE incident_ack_seconds histogram")
    for bucket in ACK_BUCKETS:
        count = sum(1 for value in snapshot.ack_durations if value <= bucket)
        lines.append('incident_ack_seconds_bucket{le="%s"} %d' % (_float(bucket), count))
    lines.append('incident_ack_seconds_bucket{le="+Inf"} %d' % len(snapshot.ack_durations))
    lines.append("incident_ack_seconds_sum %s" % _float(sum(snapshot.ack_durations)))
    lines.append("incident_ack_seconds_count %d" % len(snapshot.ack_durations))

    lines.append("# HELP mttr_seconds Mean time to resolve over resolved incidents.")
    lines.append("# TYPE mttr_seconds gauge")
    lines.append("mttr_seconds %s" % _float(snapshot.mean_mttr or 0.0))
    lines.append("# HELP mttr_seconds_last MTTR of the most recently resolved incident.")
    lines.append("# TYPE mttr_seconds_last gauge")
    lines.append(
        "mttr_seconds_last %s"
        % _float(snapshot.mttr_durations[-1] if snapshot.mttr_durations else 0.0)
    )
    lines.append("# HELP mttr_seconds_count Number of incidents with a measured MTTR.")
    lines.append("# TYPE mttr_seconds_count gauge")
    lines.append("mttr_seconds_count %d" % len(snapshot.mttr_durations))

    lines.append("# HELP incidents_total Incidents created since the database was initialised.")
    lines.append("# TYPE incidents_total counter")
    if snapshot.totals_by_severity_service:
        for severity, service, total in snapshot.totals_by_severity_service:
            lines.append(
                'incidents_total{severity="%s",service="%s"} %d'
                % (_label(severity), _label(service), total)
            )
    else:
        lines.append('incidents_total{severity="none",service="none"} 0')

    lines.append(
        "# HELP alerts_received_total Alert deliveries accepted by the webhook, "
        "deduplicated or not."
    )
    lines.append("# TYPE alerts_received_total counter")
    lines.append("alerts_received_total %d" % snapshot.alerts_received)
    lines.append(
        "# HELP alerts_deduplicated_total Deliveries recognised as repeats of an already "
        "known fingerprint."
    )
    lines.append("# TYPE alerts_deduplicated_total counter")
    lines.append("alerts_deduplicated_total %d" % snapshot.alerts_deduplicated)

    lines.append("")
    return "\n".join(lines)


def summarise(snapshot: MetricsSnapshot) -> Iterable[str]:
    """Human-readable one-liners for the CLI/demo summary."""
    yield "open incidents        : %d" % snapshot.open_incidents
    yield "acknowledged          : %d" % snapshot.acknowledged_incidents
    yield "resolved              : %d" % snapshot.resolved_incidents
    yield "alerts received       : %d" % snapshot.alerts_received
    yield "alerts deduplicated   : %d (ratio %.2f)" % (
        snapshot.alerts_deduplicated,
        snapshot.dedupe_ratio,
    )
    yield "MTTD (mean)           : %s" % _render_seconds(snapshot.mean_mttd)
    yield "MTTR (mean)           : %s" % _render_seconds(snapshot.mean_mttr)


def _render_seconds(value) -> str:
    from .models import format_duration

    if value is None:
        return "n/a"
    return "%s (%ds)" % (format_duration(value), int(round(value)))

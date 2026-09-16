"""Blameless postmortem rendering.

The generator fills everything the machines actually know — severity, owner,
route, ack/resolve timestamps, MTTD, MTTR, the real timeline, the alert
annotations — and refuses to invent the rest: fields only humans can write are
emitted as ``<!-- preencher: ... -->`` markers so the document cannot be
"finished" without someone filling them in. The action-items table is rendered
with a header and no rows, on purpose.
"""

from __future__ import annotations

from typing import List, Optional

from .config import Policy
from .models import Incident, TimelineEntry, format_duration, to_iso

PLACEHOLDER_SUMMARY = (
    "<!-- preencher: resumo executivo em 2-4 frases — o que quebrou, para quem, "
    "e como foi detectado -->"
)
PLACEHOLDER_IMPACT = "<!-- preencher: usuários/serviços afetados, volume e janela de impacto -->"
PLACEHOLDER_FACTORS = "<!-- preencher: fatores contribuintes técnicos e organizacionais -->"
PLACEHOLDER_DETECTION = (
    "<!-- preencher: como o problema foi detectado (alerta, cliente, deploy) -->"
)
PLACEHOLDER_WENT_WELL = "<!-- preencher: o que funcionou bem na resposta -->"
PLACEHOLDER_ACTIONS = (
    "<!-- preencher: uma linha por ação, com tipo (prevent/detect/mitigate), "
    'owner e prazo. Sem ação mecânica como "ter mais cuidado" -->'
)
PLACEHOLDER_ROOT_CAUSE = (
    "<!-- preencher: causa raiz — o que mudou no sistema/comportamento humano -->"
)

ACTION_ITEMS_HEADER = "| Action | Type | Owner | Due | Issue |"

CONTRIBUTING_ANNOTATIONS = (
    "contributing_factor",
    "contributing_factors",
    "cause",
)

DEADLINE_STATE_LABEL = {
    "met": "met",
    "late": "breached: acknowledged after the deadline",
    "missed": "breached: resolved without ever being acknowledged",
    "overdue": "breached: still unacknowledged past the deadline",
    "pending": "within deadline, awaiting acknowledgement",
    "no-deadline": "no deadline declared in policy",
}


def render(
    incident: Incident,
    timeline: Optional[List[TimelineEntry]] = None,
    policy: Optional[Policy] = None,
) -> str:
    """Render the full postmortem markdown for one incident."""
    timeline = timeline or []
    lines: List[str] = []
    lines.append("# Postmortem — %s (%s)" % (incident.service, incident.severity.value))
    lines.append("")
    lines.append("_Blameless: the goal is to find the systemic gap, not the person closest to it._")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    for label, value in _metadata_rows(incident, policy):
        lines.append("| %s | %s |" % (label, value))
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    summary = (incident.summary or incident.title or "").strip()
    lines.append(summary if summary else PLACEHOLDER_SUMMARY)
    if incident.description:
        lines.append("")
        lines.append(incident.description.strip())
    lines.append("")

    lines.append("## Impact")
    lines.append("")
    lines.append(
        "- Severity: **%s** (policy rule `%s`)" % (incident.severity.value, _rule_of(incident))
    )
    lines.append(
        "- Blast radius: service `%s`%s"
        % (
            incident.service,
            ", SLO `%s`" % incident.slo if incident.slo else " (no SLO declared in policy)",
        )
    )
    lines.append("- Alerts received for this fingerprint: **%d**" % incident.alert_count)
    if incident.mttr_seconds is not None:
        lines.append("- Time in degraded state: **%s**" % format_duration(incident.mttr_seconds))
    lines.append(PLACEHOLDER_IMPACT)
    lines.append("")

    lines.append("## Detection")
    lines.append("")
    if incident.opened_at:
        lines.append(
            "- Detected by the Alertmanager webhook delivery that opened the incident at %s"
            % to_iso(incident.opened_at)
        )
        if incident.acknowledged_at:
            lines.append(
                "- Acknowledged by %s at %s (MTTD %s)"
                % (
                    _ack_actor(timeline),
                    to_iso(incident.acknowledged_at),
                    format_duration(incident.mttd_seconds),
                )
            )
        else:
            lines.append("- Never acknowledged before resolution")
    lines.append(PLACEHOLDER_DETECTION)
    lines.append("")

    lines.append("## Timeline")
    lines.append("")
    if timeline:
        lines.append("_All entries come from the append-only incident timeline._")
        lines.append("")
        for entry in timeline:
            lines.append(entry.render_line())
    else:
        lines.append("<!-- preencher: timeline do incidente (nenhuma entrada registrada) -->")
    lines.append("")

    lines.append("## Contributing factors")
    lines.append("")
    factors = _contributing_factors(incident)
    if factors:
        for factor in factors:
            lines.append("- %s" % factor)
    else:
        lines.append(PLACEHOLDER_FACTORS)
    lines.append("")

    lines.append("## Root cause")
    lines.append("")
    lines.append(PLACEHOLDER_ROOT_CAUSE)
    lines.append("")

    lines.append("## What went well")
    lines.append("")
    lines.append(PLACEHOLDER_WENT_WELL)
    lines.append("")

    lines.append("## Action items")
    lines.append("")
    lines.append(PLACEHOLDER_ACTIONS)
    lines.append("")
    lines.append(ACTION_ITEMS_HEADER)
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "Generated by `incidentd` from incident `%s` (fingerprint `%s`). "
        "Machine-filled fields are exact; every `preencher` marker is for the humans."
        % (incident.id, incident.fingerprint)
    )
    lines.append("")
    return "\n".join(lines)


def required_placeholders() -> List[str]:
    """Markers every generated postmortem carries, whatever the incident state.

    Contributing factors is *not* here: it is filled from the alert annotations
    when they exist and only falls back to a marker when they do not.
    """
    return [
        PLACEHOLDER_ROOT_CAUSE,
        PLACEHOLDER_WENT_WELL,
        PLACEHOLDER_ACTIONS,
    ]


def _metadata_rows(incident: Incident, policy: Optional[Policy]):
    rows = [
        ("Incident", incident.id),
        ("Severity", incident.severity.value),
        ("Service", incident.service),
        ("Owner", incident.owner),
        (
            "Route",
            "%s via %s%s"
            % (
                incident.route or "unrouted",
                incident.channel or "unrouted",
                ", escalation after %dm" % incident.escalation_minutes
                if incident.escalation_minutes
                else "",
            ),
        ),
        ("Status", incident.status.value),
        ("SLO", incident.slo or "not declared in policy"),
        ("Opened at", to_iso(incident.opened_at) or "n/a"),
        (
            "Acknowledged at",
            "%s (MTTD %s)"
            % (
                to_iso(incident.acknowledged_at) or "n/a",
                format_duration(incident.mttd_seconds),
            ),
        ),
        (
            "Resolved at",
            "%s (MTTR %s)"
            % (to_iso(incident.resolved_at) or "n/a", format_duration(incident.mttr_seconds)),
        ),
        (
            "Ack deadline",
            "%s (%s)"
            % (
                "%dm" % incident.ack_deadline_minutes if incident.ack_deadline_minutes else "n/a",
                DEADLINE_STATE_LABEL.get(
                    incident.acknowledgement_deadline_state(),
                    incident.acknowledgement_deadline_state(),
                ),
            ),
        ),
        ("Fingerprint", incident.fingerprint),
    ]
    if policy is not None:
        rows.append(("Policy", str(policy.source_path or "built-in defaults")))
    return rows


def _rule_of(incident: Incident) -> str:
    return str(incident.labels.get("_policy_rule", "policy")) or "policy"


def _ack_actor(timeline: List[TimelineEntry]) -> str:
    for entry in timeline:
        if entry.kind == "acknowledged":
            return entry.actor
    return "unknown"


def _contributing_factors(incident: Incident) -> List[str]:
    factors: List[str] = []
    for key in CONTRIBUTING_ANNOTATIONS:
        value = (incident.annotations.get(key) or "").strip()
        if value:
            if "\n" in value:
                factors.extend(line.strip("-* \t") for line in value.splitlines() if line.strip())
            else:
                factors.append(value)
    return factors

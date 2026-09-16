"""Orchestration layer: ingest, transitions, postmortem, metrics.

Both the HTTP API and the CLI talk to this class only, so "what happens when an
alert arrives" has exactly one implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from .config import Policy
from .dedupe import ACTION_ATTACH, ACTION_CREATE, ACTION_REOPEN, DedupeEngine
from .metrics import render_metrics
from .models import (
    AlertEvent,
    Incident,
    IncidentStatus,
    TimelineKind,
    format_duration,
    parse_ts,
    to_iso,
    utcnow,
    validate_transition,
)
from .notify import Notifier, NullNotifier
from .postmortem import render as render_postmortem
from .severity import classify, describe_route
from .store import MetricsSnapshot, Store

KIND_PAGE = "page"
KIND_CHAT = "chat"
KIND_RESOLVED = "resolved"


class UnknownIncident(KeyError):
    """Raised for operations on an incident id that does not exist."""


class InvalidRequest(ValueError):
    """Raised for semantically invalid input (rendered as HTTP 400)."""


@dataclass(frozen=True)
class IngestResult:
    """Outcome of processing one alert of a webhook delivery."""

    incident_id: str
    status: str
    deduplicated: bool
    severity: str
    service: str
    action: str
    alert_status: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "status": self.status,
            "deduplicated": self.deduplicated,
            "severity": self.severity,
            "service": self.service,
            "action": self.action,
            "alert_status": self.alert_status,
        }


class IncidentService:
    """Alert in, managed incident out."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        notifier: Optional[Notifier] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.policy = policy
        self.notifier = notifier or NullNotifier()
        self.clock = clock
        self.dedupe = DedupeEngine(policy)

    # -- ingest --------------------------------------------------------------

    def ingest_payload(self, payload: Mapping[str, Any], received_at: Optional[datetime] = None):
        """Process an Alertmanager webhook payload and return per-alert results."""
        from .models import AlertmanagerWebhook

        webhook = (
            payload
            if isinstance(payload, AlertmanagerWebhook)
            else AlertmanagerWebhook.model_validate(dict(payload))
        )
        now = received_at or self.clock()
        results = [
            self.ingest(
                alert.to_event(
                    received_at=now, group_key=webhook.group_key, receiver=webhook.receiver
                )
            )
            for alert in webhook.alerts
        ]
        return webhook, results

    def ingest(self, event: AlertEvent) -> IngestResult:
        """Apply one alert: create, attach to an open incident, reopen, or resolve."""
        active = self.store.find_active_by_fingerprint(event.fingerprint)
        if event.is_resolved:
            return self._handle_resolved(event, active)

        known = active or self.store.latest_by_fingerprint(event.fingerprint)
        decision = self.dedupe.decide(event.fingerprint, event.received_at, known)

        if decision.creates_incident:
            return self._open_incident(event, previous=decision.incident)
        if decision.action == ACTION_REOPEN and decision.incident is not None:
            return self._reopen_incident(event, decision.incident, decision.reason)
        assert decision.incident is not None  # attach always carries the incident
        return self._attach_alert(event, decision.incident, decision.reason)

    def _handle_resolved(self, event: AlertEvent, active: Optional[Incident]) -> IngestResult:
        incident = active or self.store.latest_by_fingerprint(event.fingerprint)
        if incident is None:
            # Resolved alert for a fingerprint we never saw fire (incidentd
            # started after the incident): reconstruct it, do not invent a gap.
            created = self._open_incident(
                event, previous=None, note="reconstructed from a resolved"
            )
            incident = self.require_incident(created.incident_id)
            resolved = self.resolve(
                incident.id, actor="alertmanager", at=event.resolved_at, kind=KIND_RESOLVED
            )
            return _result(resolved, event, deduplicated=False, action="resolved")

        if incident.status.is_terminal:
            self._record_alert(
                event,
                incident.id,
                "duplicate resolved notification ignored (incident already resolved at %s)"
                % to_iso(incident.resolved_at),
                deduplicated=True,
            )
            return _result(incident, event, deduplicated=True, action="already_resolved")

        self._record_alert(
            event,
            incident.id,
            "resolved notification received from Alertmanager",
            deduplicated=False,
        )
        resolved = self.resolve(
            incident.id, actor="alertmanager", at=event.resolved_at, kind=KIND_RESOLVED
        )
        return _result(resolved, event, deduplicated=False, action="resolved")

    def _open_incident(
        self,
        event: AlertEvent,
        previous: Optional[Incident],
        note: str = "",
    ) -> IngestResult:
        classification = classify(event.labels, self.policy)
        opened_at = event.starts_at or event.received_at
        labels = dict(event.labels)
        labels["_policy_rule"] = classification.matched_rule
        incident_id = self.store.next_incident_id(opened_at)
        incident = Incident(
            id=incident_id,
            fingerprint=event.fingerprint,
            service=str(event.labels.get("service", "") or event.labels.get("job", "unknown")),
            severity=classification.severity,
            status=IncidentStatus.OPEN,
            title="%s on %s" % (event.alertname, event.labels.get("service", "unknown")),
            owner=classification.owner,
            route=classification.route,
            channel=classification.channel,
            escalation_minutes=classification.escalation_minutes,
            tier=classification.tier,
            slo=classification.slo,
            runbook=classification.runbook,
            summary=(event.annotations.get("summary") or "").strip(),
            description=(event.annotations.get("description") or "").strip(),
            labels=labels,
            annotations=dict(event.annotations),
            opened_at=opened_at,
            last_alert_at=event.received_at,
            alert_count=1,
            ack_deadline_minutes=classification.ack_deadline_minutes,
            resolve_deadline_minutes=classification.resolve_deadline_minutes,
        )
        self.store.create_incident(incident)
        opening_message = "incident opened from %s (%s, policy rule %s)" % (
            event.alertname,
            classification.describe(),
            classification.matched_rule,
        )
        if note:
            opening_message += " [%s]" % note
        if previous is not None:
            opening_message += "; previous incident %s" % previous.id
        self.store.append_timeline(
            incident.id,
            TimelineKind.OPENED,
            opening_message,
            ts=opened_at,
            actor="alertmanager",
            metadata={"fingerprint": incident.fingerprint, "receiver": event.receiver},
        )
        self._record_alert(
            event,
            incident.id,
            "alert %s delivered (startsAt %s)" % (event.status, to_iso(opened_at)),
            deduplicated=False,
        )
        self._notify(incident, _opening_message(incident), at=event.received_at)
        return _result(incident, event, deduplicated=False, action=ACTION_CREATE)

    def _record_alert(
        self,
        event: AlertEvent,
        incident_id: str,
        message: str,
        deduplicated: bool,
    ) -> None:
        """One timeline entry + one append-only alert event per delivery."""
        self.store.record_alert(event, incident_id, deduplicated=deduplicated)
        self.store.append_timeline(
            incident_id,
            TimelineKind.ALERT_RECEIVED,
            message,
            ts=event.received_at,
            actor="alertmanager",
            metadata={"deduplicated": deduplicated, "fingerprint": event.fingerprint},
        )

    def _attach_alert(self, event: AlertEvent, incident: Incident, reason: str) -> IngestResult:
        self._record_alert(
            event,
            incident.id,
            "duplicate alert delivery: %s" % reason,
            deduplicated=True,
        )
        self.store.update_incident(
            incident.id,
            alert_count=incident.alert_count + 1,
            last_alert_at=event.received_at,
        )
        refreshed = self.store.get_incident(incident.id) or incident
        return _result(refreshed, event, deduplicated=True, action=ACTION_ATTACH)

    def _reopen_incident(self, event: AlertEvent, incident: Incident, reason: str) -> IngestResult:
        self._record_alert(
            event,
            incident.id,
            "alert firing delivered again after resolution",
            deduplicated=True,
        )
        self.store.append_timeline(
            incident.id,
            TimelineKind.REOPENED,
            "incident reopened: %s" % reason,
            ts=event.received_at,
            actor="alertmanager",
            metadata={"fingerprint": event.fingerprint},
        )
        reopened = self.store.update_incident(
            incident.id,
            status=IncidentStatus.OPEN,
            resolved_at=None,
            last_alert_at=event.received_at,
            alert_count=incident.alert_count + 1,
            reopened_count=incident.reopened_count + 1,
        )
        self._notify(reopened, "reopened: %s" % reason, at=event.received_at)
        return _result(reopened, event, deduplicated=True, action=ACTION_REOPEN)

    # -- transitions ---------------------------------------------------------

    def ack(
        self,
        incident_id: str,
        actor: str = "operator",
        at: Optional[datetime] = None,
    ) -> Incident:
        incident = self.require_incident(incident_id)
        validate_transition(incident.status, IncidentStatus.ACKNOWLEDGED)
        moment = at or self.clock()
        if at is not None and moment < incident.opened_at:
            raise InvalidRequest(
                "acknowledgement timestamp %s precedes the incident open time %s"
                % (to_iso(moment), to_iso(incident.opened_at))
            )
        deadline = incident.ack_deadline_at
        deadline_met = deadline is None or moment <= deadline
        self.store.append_timeline(
            incident.id,
            TimelineKind.ACKNOWLEDGED,
            "acknowledged by %s (%s)"
            % (
                actor,
                "within the %dm ack deadline" % incident.ack_deadline_minutes
                if deadline_met
                else "after the %dm ack deadline" % incident.ack_deadline_minutes,
            ),
            ts=moment,
            actor=actor,
            metadata={"deadline_minutes": incident.ack_deadline_minutes},
        )
        return self.store.update_incident(
            incident.id, status=IncidentStatus.ACKNOWLEDGED, acknowledged_at=moment
        )

    def resolve(
        self,
        incident_id: str,
        actor: str = "operator",
        at: Optional[datetime] = None,
        kind: str = "resolved",
    ) -> Incident:
        incident = self.require_incident(incident_id)
        validate_transition(incident.status, IncidentStatus.RESOLVED)
        moment = at or self.clock()
        if moment < incident.opened_at:
            raise InvalidRequest(
                "resolution timestamp %s precedes the incident open time %s"
                % (to_iso(moment), to_iso(incident.opened_at))
            )
        self.store.append_timeline(
            incident.id,
            TimelineKind.RESOLVED,
            "resolved by %s" % actor,
            ts=moment,
            actor=actor,
            metadata={"kind": kind},
        )
        resolved = self.store.update_incident(
            incident.id, status=IncidentStatus.RESOLVED, resolved_at=moment
        )
        self._notify(
            resolved,
            "resolved in %s (MTTR), acknowledged=%s"
            % (
                format_duration(resolved.mttr_seconds),
                "yes" if resolved.acknowledged_at else "no",
            ),
            kind=KIND_RESOLVED,
            at=moment,
        )
        return resolved

    # -- read side -----------------------------------------------------------

    def require_incident(self, incident_id: str) -> Incident:
        incident = self.store.get_incident(incident_id)
        if incident is None:
            raise UnknownIncident(incident_id)
        return incident

    def incident_detail(self, incident_id: str) -> Dict[str, Any]:
        incident = self.require_incident(incident_id)
        timeline = self.store.list_timeline(incident.id)
        return incident.to_dict(include_timeline=True, timeline=timeline)

    def list_incidents(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        service: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if status is not None and status not in {item.value for item in IncidentStatus}:
            raise InvalidRequest(
                "unknown status %r (expected one of %s)"
                % (status, ", ".join(sorted(item.value for item in IncidentStatus)))
            )
        try:
            incidents = self.store.list_incidents(status=status, severity=severity, service=service)
        except ValueError as exc:  # Severity.coerce
            raise InvalidRequest(str(exc)) from exc
        return [
            {
                "id": incident.id,
                "status": incident.status.value,
                "severity": incident.severity.value,
                "service": incident.service,
                "owner": incident.owner,
                "title": incident.title,
                "route": incident.route,
                "opened_at": to_iso(incident.opened_at),
                "acknowledged_at": to_iso(incident.acknowledged_at),
                "resolved_at": to_iso(incident.resolved_at),
                "alert_count": incident.alert_count,
                "mttd_seconds": None
                if incident.mttd_seconds is None
                else round(incident.mttd_seconds, 3),
                "mttr_seconds": None
                if incident.mttr_seconds is None
                else round(incident.mttr_seconds, 3),
                "duration_seconds": round(incident.duration_seconds, 3),
            }
            for incident in incidents
        ]

    def postmortem(self, incident_id: str) -> str:
        incident = self.require_incident(incident_id)
        return render_postmortem(incident, self.store.list_timeline(incident.id), self.policy)

    def metrics_snapshot(self) -> MetricsSnapshot:
        return self.store.metrics_snapshot()

    def metrics_text(self) -> str:
        return render_metrics(self.metrics_snapshot())

    def policy_summary(self) -> Dict[str, Any]:
        return {
            "version": self.policy.version,
            "source": str(self.policy.source_path) if self.policy.source_path else "built-in",
            "services": self.policy.service_names(),
            "severity_rules": self.policy.rule_names(),
            "dedupe": self.dedupe.describe(),
        }

    # -- internals -----------------------------------------------------------

    def _notify(
        self,
        incident: Incident,
        message: str,
        kind: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> None:
        resolved_kind = kind or (KIND_PAGE if incident.severity.pages_humans else KIND_CHAT)
        self.notifier.notify(incident, message, resolved_kind)
        timestamp = at or incident.resolved_at or incident.last_alert_at or self.clock()
        self.store.append_timeline(
            incident.id,
            TimelineKind.NOTIFIED,
            "notified %s (%s)" % (incident.channel, resolved_kind),
            ts=timestamp,
            actor="notifier",
            metadata={"channel": incident.channel, "kind": resolved_kind},
        )


def _opening_message(incident: Incident) -> str:
    return "%s, ack within %dm via %s, runbook: %s" % (
        describe_route(incident),
        incident.ack_deadline_minutes,
        incident.channel,
        incident.runbook or "not declared",
    )


def _result(incident: Incident, event: AlertEvent, deduplicated: bool, action: str) -> IngestResult:
    return IngestResult(
        incident_id=incident.id,
        status=incident.status.value,
        deduplicated=deduplicated,
        severity=incident.severity.value,
        service=incident.service,
        action=action,
        alert_status=event.status,
    )


def parse_optional_ts(value: Any) -> Optional[datetime]:
    return parse_ts(value)

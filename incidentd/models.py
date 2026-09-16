"""Domain models and payload parsing for incidentd.

No I/O lives here: the store persists these objects, the API layer parses
incoming Alertmanager payloads into them and the CLI builds them from files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Normalise any datetime to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse an RFC3339 timestamp as emitted by Alertmanager.

    Returns ``None`` for empty/invalid/zero values. Alertmanager sends the
    Go zero time (``0001-01-01T00:00:00Z``) for ``endsAt`` of firing alerts,
    which is mapped to ``None`` by the caller.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ensure_utc(parsed)


def to_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime as second-precision RFC3339 UTC."""
    if value is None:
        return None
    return ensure_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_DURATION_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def format_duration(seconds: Optional[float]) -> str:
    """Render a duration like ``47m 12s`` (max two units)."""
    if seconds is None:
        return "n/a"
    remaining = int(round(seconds))
    if remaining <= 0:
        return "0s"
    parts: List[str] = []
    for suffix, size in _DURATION_UNITS:
        if remaining >= size:
            quantity, remaining = divmod(remaining, size)
            parts.append("%d%s" % (quantity, suffix))
    return " ".join(parts[:2])


def compute_fingerprint(labels: Mapping[str, str]) -> str:
    """Alertmanager-compatible fingerprint.

    Alertmanager hashes the alert label set (sorted ``name=value`` pairs joined
    by ``,``) with SHA-256 and serialises the first 8 bytes as hexadecimal. We
    reproduce that so payloads that omit ``fingerprint`` still dedupe against
    payloads that include it.
    """
    joined = ",".join("%s=%s" % (key, value) for key, value in sorted(labels.items()))
    digest = hashlib.sha256(joined.encode("utf-8")).digest()
    return "%016x" % int.from_bytes(digest[:8], "big")


class Severity(str, Enum):
    """Operator-facing severity, assigned by policy (never by hardcoded rules)."""

    SEV1 = "Sev1"
    SEV2 = "Sev2"
    SEV3 = "Sev3"
    SEV4 = "Sev4"

    @property
    def rank(self) -> int:
        return int(self.value[3:])

    @property
    def pages_humans(self) -> bool:
        return self.rank <= 2

    @classmethod
    def coerce(cls, value: Any) -> "Severity":
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        for candidate in cls:
            if text.lower() in {candidate.value.lower(), str(candidate.rank)}:
                return candidate
        raise ValueError("unknown severity: %r" % (value,))


class IncidentStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"

    @property
    def is_terminal(self) -> bool:
        return self is IncidentStatus.RESOLVED


ALLOWED_TRANSITIONS: Dict[IncidentStatus, frozenset] = {
    IncidentStatus.OPEN: frozenset({IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED}),
    IncidentStatus.ACKNOWLEDGED: frozenset({IncidentStatus.RESOLVED}),
    IncidentStatus.RESOLVED: frozenset(),
}


class TransitionError(RuntimeError):
    """Raised when a state transition is rejected; rendered as HTTP 409."""

    def __init__(
        self,
        reason: str,
        current: IncidentStatus,
        target: IncidentStatus,
        allowed: Optional[frozenset] = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.current = current
        self.target = target
        self.allowed = allowed if allowed is not None else ALLOWED_TRANSITIONS[current]

    def as_detail(self) -> Dict[str, Any]:
        return {
            "reason": self.reason,
            "current_status": self.current.value,
            "requested_status": self.target.value,
            "allowed_transitions": sorted(item.value for item in self.allowed),
        }


def validate_transition(current: IncidentStatus, target: IncidentStatus) -> None:
    """Raise :class:`TransitionError` when ``current -> target`` is invalid."""
    if target in ALLOWED_TRANSITIONS[current]:
        return
    if current is IncidentStatus.RESOLVED:
        if target is IncidentStatus.ACKNOWLEDGED:
            raise TransitionError(
                "incident is already resolved: an acknowledgement after resolution "
                "is rejected, reopen the incident instead",
                current,
                target,
            )
        raise TransitionError("incident is already resolved", current, target)
    if target is IncidentStatus.ACKNOWLEDGED:
        raise TransitionError("incident is already acknowledged", current, target)
    raise TransitionError(
        "transition %s -> %s is not allowed" % (current.value, target.value),
        current,
        target,
    )


class TimelineKind:
    """Known timeline entry kinds (free-form strings are allowed everywhere)."""

    OPENED = "opened"
    ALERT_RECEIVED = "alert_received"
    REOPENED = "reopened"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    NOTIFIED = "notified"


@dataclass
class AlertEvent:
    """A single alert extracted from an Alertmanager webhook delivery."""

    fingerprint: str
    status: str
    alertname: str
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    received_at: datetime = field(default_factory=utcnow)
    group_key: str = ""
    receiver: str = ""
    generator_url: Optional[str] = None

    def __post_init__(self) -> None:
        self.status = (self.status or "firing").lower()
        if self.status not in {"firing", "resolved"}:
            raise ValueError("unsupported alert status: %r" % (self.status,))
        if self.ends_at is not None and self.ends_at.year < 1970:
            # Alertmanager's Go zero time for firing alerts.
            self.ends_at = None
        self.received_at = ensure_utc(self.received_at)

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def resolved_at(self) -> Optional[datetime]:
        """When the alert stopped: explicit ``endsAt`` or the delivery time."""
        if not self.is_resolved:
            return None
        return self.ends_at or self.received_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "status": self.status,
            "alertname": self.alertname,
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
            "starts_at": to_iso(self.starts_at),
            "ends_at": to_iso(self.ends_at),
            "received_at": to_iso(self.received_at),
            "group_key": self.group_key,
            "receiver": self.receiver,
            "generator_url": self.generator_url,
        }


@dataclass
class TimelineEntry:
    """Append-only audit record. Never updated, never deleted."""

    incident_id: str
    ts: datetime
    kind: str
    message: str
    actor: str = "system"
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "ts": to_iso(self.ts),
            "kind": self.kind,
            "actor": self.actor,
            "message": self.message,
            "metadata": dict(self.metadata),
        }

    def render_line(self) -> str:
        return "- %s · `%s` · %s — %s" % (to_iso(self.ts), self.kind, self.actor, self.message)


@dataclass
class Incident:
    """A managed incident: one per fingerprint while it stays unresolved."""

    id: str
    fingerprint: str
    service: str
    severity: Severity
    status: IncidentStatus
    title: str
    owner: str
    route: str = "chat-sre"
    channel: str = "unassigned"
    escalation_minutes: int = 0
    tier: Optional[int] = None
    slo: Optional[str] = None
    runbook: Optional[str] = None
    summary: str = ""
    description: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    opened_at: datetime = field(default_factory=utcnow)
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    last_alert_at: Optional[datetime] = None
    alert_count: int = 0
    reopened_count: int = 0
    ack_deadline_minutes: int = 0
    resolve_deadline_minutes: int = 0

    def __post_init__(self) -> None:
        self.severity = Severity.coerce(self.severity)
        self.status = (
            self.status if isinstance(self.status, IncidentStatus) else IncidentStatus(self.status)
        )
        self.opened_at = ensure_utc(self.opened_at)

    @property
    def mttd_seconds(self) -> Optional[float]:
        """Mean time to detect: open -> acknowledgement."""
        if self.acknowledged_at is None:
            return None
        return (ensure_utc(self.acknowledged_at) - self.opened_at).total_seconds()

    @property
    def mttr_seconds(self) -> Optional[float]:
        """Mean time to resolve: open -> resolved."""
        if self.resolved_at is None:
            return None
        return (ensure_utc(self.resolved_at) - self.opened_at).total_seconds()

    @property
    def duration_seconds(self) -> float:
        """Elapsed time, still counting while the incident is open."""
        end = self.resolved_at or utcnow()
        return (ensure_utc(end) - self.opened_at).total_seconds()

    @property
    def ack_deadline_at(self) -> Optional[datetime]:
        if not self.ack_deadline_minutes:
            return None
        return self.opened_at + timedelta(minutes=self.ack_deadline_minutes)

    def acknowledgement_deadline_state(self, now: Optional[datetime] = None) -> str:
        """One of ``met``, ``late``, ``missed``, ``overdue``, ``pending``, ``no-deadline``."""
        deadline = self.ack_deadline_at
        if deadline is None:
            return "no-deadline"
        if self.acknowledged_at is not None:
            return "met" if ensure_utc(self.acknowledged_at) <= deadline else "late"
        if self.resolved_at is not None:
            return "missed"
        reference = ensure_utc(now) if now is not None else utcnow()
        return "overdue" if reference > deadline else "pending"

    @property
    def ack_deadline_breached(self) -> bool:
        return self.acknowledgement_deadline_state() in {"late", "missed", "overdue"}

    def to_dict(
        self, include_timeline: bool = False, timeline: Optional[List[TimelineEntry]] = None
    ):
        payload: Dict[str, Any] = {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "service": self.service,
            "severity": self.severity.value,
            "status": self.status.value,
            "title": self.title,
            "owner": self.owner,
            "route": self.route,
            "channel": self.channel,
            "tier": self.tier,
            "slo": self.slo,
            "runbook": self.runbook,
            "summary": self.summary,
            "description": self.description,
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
            "opened_at": to_iso(self.opened_at),
            "acknowledged_at": to_iso(self.acknowledged_at),
            "resolved_at": to_iso(self.resolved_at),
            "last_alert_at": to_iso(self.last_alert_at),
            "alert_count": self.alert_count,
            "reopened_count": self.reopened_count,
            "ack_deadline_minutes": self.ack_deadline_minutes,
            "resolve_deadline_minutes": self.resolve_deadline_minutes,
            "ack_deadline_breached": self.ack_deadline_breached,
            "ack_deadline_state": self.acknowledgement_deadline_state(),
            "mttd_seconds": _round(self.mttd_seconds),
            "mttr_seconds": _round(self.mttr_seconds),
            "duration_seconds": _round(self.duration_seconds),
        }
        if include_timeline:
            payload["timeline"] = [entry.to_dict() for entry in (timeline or [])]
        return payload


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 3)


class AlertmanagerAlert(BaseModel):
    """One entry of ``alerts[]`` in the Alertmanager webhook payload."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str = "firing"
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    starts_at: Optional[datetime] = Field(default=None, alias="startsAt")
    ends_at: Optional[datetime] = Field(default=None, alias="endsAt")
    generator_url: Optional[str] = Field(default=None, alias="generatorURL")
    fingerprint: Optional[str] = None

    def to_event(
        self,
        received_at: Optional[datetime] = None,
        group_key: str = "",
        receiver: str = "",
    ) -> AlertEvent:
        labels = {str(key): str(value) for key, value in self.labels.items()}
        fingerprint = (self.fingerprint or "").strip() or compute_fingerprint(labels)
        return AlertEvent(
            fingerprint=fingerprint,
            status=self.status,
            alertname=labels.get("alertname", "unnamed-alert"),
            labels=labels,
            annotations={str(k): str(v) for k, v in self.annotations.items()},
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            received_at=received_at or utcnow(),
            group_key=group_key,
            receiver=receiver,
            generator_url=self.generator_url,
        )


class AlertmanagerWebhook(BaseModel):
    """The Alertmanager webhook payload (``version: 4``)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    version: str = "4"
    group_key: str = Field(default="", alias="groupKey")
    truncated_alerts: int = Field(default=0, alias="truncatedAlerts")
    status: str = "firing"
    receiver: str = ""
    group_labels: Dict[str, str] = Field(default_factory=dict, alias="groupLabels")
    common_labels: Dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: Dict[str, str] = Field(default_factory=dict, alias="commonAnnotations")
    external_url: Optional[str] = Field(default=None, alias="externalURL")
    alerts: List[AlertmanagerAlert] = Field(default_factory=list)

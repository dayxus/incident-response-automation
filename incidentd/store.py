"""SQLite persistence for incidents, timeline and alert events.

Design notes:

* stdlib ``sqlite3`` only, no ORM — the schema is 3 tables and one meta table.
* The ``timeline`` and ``alert_events`` tables are **append-only**, enforced by
  database triggers, not by convention: an audit trail you can rewrite is not
  an audit trail. ``test_timeline.py`` proves the triggers fire.
* All writes take a re-entrant lock around a single connection, so the app can
  be served from FastAPI's threadpool without racing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .models import (
    AlertEvent,
    Incident,
    IncidentStatus,
    Severity,
    TimelineEntry,
    parse_ts,
    to_iso,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                      TEXT PRIMARY KEY,
    fingerprint             TEXT NOT NULL,
    service                 TEXT NOT NULL,
    severity                TEXT NOT NULL,
    status                  TEXT NOT NULL,
    title                   TEXT NOT NULL,
    owner                   TEXT NOT NULL,
    route                   TEXT NOT NULL DEFAULT '',
    channel                 TEXT NOT NULL DEFAULT '',
    escalation_minutes      INTEGER NOT NULL DEFAULT 0,
    tier                    INTEGER,
    slo                     TEXT,
    runbook                 TEXT,
    summary                 TEXT NOT NULL DEFAULT '',
    description             TEXT NOT NULL DEFAULT '',
    labels                  TEXT NOT NULL DEFAULT '{}',
    annotations             TEXT NOT NULL DEFAULT '{}',
    opened_at               TEXT NOT NULL,
    acknowledged_at         TEXT,
    resolved_at             TEXT,
    last_alert_at           TEXT,
    alert_count             INTEGER NOT NULL DEFAULT 0,
    reopened_count          INTEGER NOT NULL DEFAULT 0,
    ack_deadline_minutes    INTEGER NOT NULL DEFAULT 0,
    resolve_deadline_minutes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS incidents_fingerprint_idx ON incidents (fingerprint, opened_at);
CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents (status);
CREATE INDEX IF NOT EXISTS incidents_service_idx ON incidents (service);

CREATE TABLE IF NOT EXISTS timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents (id),
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    message     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS timeline_incident_idx ON timeline (incident_id, ts, id);

CREATE TABLE IF NOT EXISTS alert_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,
    incident_id   TEXT,
    alertname     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    starts_at     TEXT,
    ends_at       TEXT,
    received_at   TEXT NOT NULL,
    deduplicated  INTEGER NOT NULL DEFAULT 0,
    group_key     TEXT NOT NULL DEFAULT '',
    labels        TEXT NOT NULL DEFAULT '{}',
    annotations   TEXT NOT NULL DEFAULT '{}',
    generator_url TEXT
);

CREATE INDEX IF NOT EXISTS alert_events_fingerprint_idx ON alert_events (fingerprint, received_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS timeline_append_only_update
BEFORE UPDATE ON timeline
BEGIN
    SELECT RAISE(ABORT, 'timeline is append-only: updates are rejected');
END;

CREATE TRIGGER IF NOT EXISTS timeline_append_only_delete
BEFORE DELETE ON timeline
BEGIN
    SELECT RAISE(ABORT, 'timeline is append-only: deletes are rejected');
END;

CREATE TRIGGER IF NOT EXISTS alert_events_append_only_update
BEFORE UPDATE ON alert_events
BEGIN
    SELECT RAISE(ABORT, 'alert_events is append-only: updates are rejected');
END;

CREATE TRIGGER IF NOT EXISTS alert_events_append_only_delete
BEFORE DELETE ON alert_events
BEGIN
    SELECT RAISE(ABORT, 'alert_events is append-only: deletes are rejected');
END;
"""

INCIDENT_COLUMNS = (
    "id",
    "fingerprint",
    "service",
    "severity",
    "status",
    "title",
    "owner",
    "route",
    "channel",
    "escalation_minutes",
    "tier",
    "slo",
    "runbook",
    "summary",
    "description",
    "labels",
    "annotations",
    "opened_at",
    "acknowledged_at",
    "resolved_at",
    "last_alert_at",
    "alert_count",
    "reopened_count",
    "ack_deadline_minutes",
    "resolve_deadline_minutes",
)


@dataclass
class MetricsSnapshot:
    """Everything ``GET /metrics`` needs, computed from the stored timeline."""

    open_incidents: int
    acknowledged_incidents: int
    resolved_incidents: int
    totals_by_severity_service: List[Tuple[str, str, int]]
    ack_durations: List[float]
    mttr_durations: List[float]
    alerts_received: int = 0
    alerts_deduplicated: int = 0

    @property
    def mean_mttr(self) -> Optional[float]:
        if not self.mttr_durations:
            return None
        return sum(self.mttr_durations) / len(self.mttr_durations)

    @property
    def mean_mttd(self) -> Optional[float]:
        if not self.ack_durations:
            return None
        return sum(self.ack_durations) / len(self.ack_durations)

    @property
    def dedupe_ratio(self) -> float:
        """Share of deliveries that Alertmanager repeated (alert-quality signal)."""
        if not self.alerts_received:
            return 0.0
        return self.alerts_deduplicated / self.alerts_received


class Store:
    """Thin, explicit persistence layer over SQLite."""

    def __init__(self, path, timeout: float = 30.0) -> None:
        self.path = str(path)
        if self.path not in {":memory:"}:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None

    # -- connection handling -------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            connection = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            connection.commit()
            self._connection = connection
        return self._connection

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self.connection.cursor()
            try:
                yield cursor
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- meta helpers --------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        with self._cursor() as cursor:
            row = cursor.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def next_incident_id(self, opened_at: datetime) -> str:
        """Human-readable sequential id, e.g. ``INC-2026-0003``."""
        with self._lock:
            current = self.get_meta("incident_seq")
            sequence = int(current) + 1 if current else 1
            self.set_meta("incident_seq", str(sequence))
        return "INC-%d-%04d" % (opened_at.year, sequence)

    # -- incidents -----------------------------------------------------------

    def create_incident(self, incident: Incident) -> Incident:
        payload = _incident_to_row(incident)
        placeholders = ", ".join("?" for _ in INCIDENT_COLUMNS)
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO incidents (%s) VALUES (%s)"
                % (", ".join(INCIDENT_COLUMNS), placeholders),
                tuple(payload[column] for column in INCIDENT_COLUMNS),
            )
        return incident

    def update_incident(self, incident_id: str, **fields: Any) -> Incident:
        if not fields:
            raise ValueError("update_incident requires at least one field")
        unknown = set(fields) - set(INCIDENT_COLUMNS)
        if unknown:
            raise ValueError("unknown incident columns: %s" % ", ".join(sorted(unknown)))
        assignments = ", ".join("%s = ?" % column for column in fields)
        values = [_serialise(fields[column]) for column in fields]
        values.append(incident_id)
        with self._cursor() as cursor:
            cursor.execute("UPDATE incidents SET %s WHERE id = ?" % assignments, tuple(values))
            if cursor.rowcount == 0:
                raise KeyError("no such incident: %s" % incident_id)
        incident = self.get_incident(incident_id)
        assert incident is not None  # just updated it
        return incident

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        with self._cursor() as cursor:
            row = cursor.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return None if row is None else _row_to_incident(row)

    def find_active_by_fingerprint(self, fingerprint: str) -> Optional[Incident]:
        """Latest unresolved incident for a fingerprint, if any."""
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT * FROM incidents WHERE fingerprint = ? AND status != ? "
                "ORDER BY opened_at DESC LIMIT 1",
                (fingerprint, IncidentStatus.RESOLVED.value),
            ).fetchone()
        return None if row is None else _row_to_incident(row)

    def latest_by_fingerprint(self, fingerprint: str) -> Optional[Incident]:
        with self._cursor() as cursor:
            row = cursor.execute(
                "SELECT * FROM incidents WHERE fingerprint = ? ORDER BY opened_at DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return None if row is None else _row_to_incident(row)

    def list_incidents(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        service: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Incident]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(Severity.coerce(severity).value)
        if service:
            clauses.append("service = ?")
            params.append(service)
        query = "SELECT * FROM incidents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY opened_at DESC, id DESC"
        if limit:
            query += " LIMIT %d" % int(limit)
        with self._cursor() as cursor:
            rows = cursor.execute(query, tuple(params)).fetchall()
        return [_row_to_incident(row) for row in rows]

    # -- timeline ------------------------------------------------------------

    def append_timeline(
        self,
        incident_id: str,
        kind: str,
        message: str,
        ts: Optional[datetime] = None,
        actor: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TimelineEntry:
        entry = TimelineEntry(
            incident_id=incident_id,
            ts=ts or utcnow(),
            kind=kind,
            message=message,
            actor=actor,
            metadata=metadata or {},
        )
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO timeline (incident_id, ts, kind, actor, message, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry.incident_id,
                    to_iso(entry.ts),
                    entry.kind,
                    entry.actor,
                    entry.message,
                    json.dumps(entry.metadata, sort_keys=True),
                ),
            )
            entry.id = cursor.lastrowid
        return entry

    def list_timeline(self, incident_id: str) -> List[TimelineEntry]:
        with self._cursor() as cursor:
            rows = cursor.execute(
                "SELECT * FROM timeline WHERE incident_id = ? ORDER BY ts ASC, id ASC",
                (incident_id,),
            ).fetchall()
        return [_row_to_timeline(row) for row in rows]

    # -- alert events --------------------------------------------------------

    def record_alert(
        self, event: AlertEvent, incident_id: Optional[str], deduplicated: bool
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO alert_events (fingerprint, incident_id, alertname, status,"
                " starts_at, ends_at, received_at, deduplicated, group_key, labels,"
                " annotations, generator_url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.fingerprint,
                    incident_id,
                    event.alertname,
                    event.status,
                    to_iso(event.starts_at),
                    to_iso(event.ends_at),
                    to_iso(event.received_at),
                    1 if deduplicated else 0,
                    event.group_key,
                    json.dumps(event.labels, sort_keys=True),
                    json.dumps(event.annotations, sort_keys=True),
                    event.generator_url,
                ),
            )

    def count_alerts(self, deduplicated: Optional[bool] = None, incident_id: Optional[str] = None):
        query = "SELECT COUNT(*) AS total FROM alert_events"
        clauses: List[str] = []
        params: List[Any] = []
        if deduplicated is not None:
            clauses.append("deduplicated = ?")
            params.append(1 if deduplicated else 0)
        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._cursor() as cursor:
            row = cursor.execute(query, tuple(params)).fetchone()
        return int(row["total"])

    def list_alert_events(self, fingerprint: str) -> List[Dict[str, Any]]:
        with self._cursor() as cursor:
            rows = cursor.execute(
                "SELECT * FROM alert_events WHERE fingerprint = ? ORDER BY received_at ASC, id ASC",
                (fingerprint,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- metrics -------------------------------------------------------------

    def metrics_snapshot(self) -> MetricsSnapshot:
        counts = dict.fromkeys(IncidentStatus, 0)
        totals: Dict[Tuple[str, str], int] = {}
        ack_durations: List[float] = []
        mttr_durations: List[float] = []
        for incident in self.list_incidents():
            counts[incident.status] += 1
            totals[(incident.severity.value, incident.service)] = (
                totals.get((incident.severity.value, incident.service), 0) + 1
            )
            if incident.mttd_seconds is not None:
                ack_durations.append(incident.mttd_seconds)
            if incident.mttr_seconds is not None:
                mttr_durations.append(incident.mttr_seconds)
        return MetricsSnapshot(
            open_incidents=counts[IncidentStatus.OPEN],
            acknowledged_incidents=counts[IncidentStatus.ACKNOWLEDGED],
            resolved_incidents=counts[IncidentStatus.RESOLVED],
            totals_by_severity_service=[
                (severity, service, total) for (severity, service), total in sorted(totals.items())
            ],
            ack_durations=sorted(ack_durations),
            mttr_durations=sorted(mttr_durations),
            alerts_received=self.count_alerts(),
            alerts_deduplicated=self.count_alerts(deduplicated=True),
        )

    def healthy(self) -> bool:
        try:
            with self._cursor() as cursor:
                cursor.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:  # pragma: no cover - exercised only on corrupt DBs
            return False


def _serialise(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, (Severity, IncidentStatus)):
        return value.value
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def _incident_to_row(incident: Incident) -> Dict[str, Any]:
    return {
        "id": incident.id,
        "fingerprint": incident.fingerprint,
        "service": incident.service,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "title": incident.title,
        "owner": incident.owner,
        "route": incident.route,
        "channel": incident.channel,
        "escalation_minutes": incident.escalation_minutes,
        "tier": incident.tier,
        "slo": incident.slo,
        "runbook": incident.runbook,
        "summary": incident.summary,
        "description": incident.description,
        "labels": json.dumps(incident.labels, sort_keys=True),
        "annotations": json.dumps(incident.annotations, sort_keys=True),
        "opened_at": to_iso(incident.opened_at),
        "acknowledged_at": to_iso(incident.acknowledged_at),
        "resolved_at": to_iso(incident.resolved_at),
        "last_alert_at": to_iso(incident.last_alert_at),
        "alert_count": incident.alert_count,
        "reopened_count": incident.reopened_count,
        "ack_deadline_minutes": incident.ack_deadline_minutes,
        "resolve_deadline_minutes": incident.resolve_deadline_minutes,
    }


def _row_to_incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        fingerprint=row["fingerprint"],
        service=row["service"],
        severity=Severity.coerce(row["severity"]),
        status=IncidentStatus(row["status"]),
        title=row["title"],
        owner=row["owner"],
        route=row["route"] or "",
        channel=row["channel"] or "",
        escalation_minutes=row["escalation_minutes"] or 0,
        tier=row["tier"],
        slo=row["slo"],
        runbook=row["runbook"],
        summary=row["summary"] or "",
        description=row["description"] or "",
        labels=json.loads(row["labels"] or "{}"),
        annotations=json.loads(row["annotations"] or "{}"),
        opened_at=parse_ts(row["opened_at"]) or utcnow(),
        acknowledged_at=parse_ts(row["acknowledged_at"]),
        resolved_at=parse_ts(row["resolved_at"]),
        last_alert_at=parse_ts(row["last_alert_at"]),
        alert_count=row["alert_count"] or 0,
        reopened_count=row["reopened_count"] or 0,
        ack_deadline_minutes=row["ack_deadline_minutes"] or 0,
        resolve_deadline_minutes=row["resolve_deadline_minutes"] or 0,
    )


def _row_to_timeline(row: sqlite3.Row) -> TimelineEntry:
    return TimelineEntry(
        id=row["id"],
        incident_id=row["incident_id"],
        ts=parse_ts(row["ts"]) or utcnow(),
        kind=row["kind"],
        actor=row["actor"],
        message=row["message"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


def count_by_status(incidents: Iterable[Incident]) -> Dict[str, int]:
    counts = {status.value: 0 for status in IncidentStatus}
    for incident in incidents:
        counts[incident.status.value] += 1
    return counts

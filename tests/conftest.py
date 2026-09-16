"""Shared fixtures: policy, isolated SQLite store, injected notifier, fake clock.

No test in this suite performs network I/O: the notifier is always injected
(``RecordingNotifier``) and the API is exercised in-process with ``TestClient``.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from incidentd.api import create_app
from incidentd.config import load_policy
from incidentd.notify import RecordingNotifier
from incidentd.service import IncidentService
from incidentd.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "policy" / "policy.yaml"
EXAMPLES_DIR = REPO_ROOT / "examples" / "alertmanager"

INCIDENT_START = datetime(2026, 3, 4, 13, 2, 10, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic clock, so MTTD/MTTR assertions are exact."""

    def __init__(self, start: datetime = INCIDENT_START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now

    def set(self, moment: datetime) -> datetime:
        self.now = moment
        return self.now


def example_payload(name: str) -> Dict[str, Any]:
    """Load one of the Alertmanager payloads committed under examples/."""
    return json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))


def make_payload(
    labels: Optional[Dict[str, str]] = None,
    annotations: Optional[Dict[str, str]] = None,
    status: str = "firing",
    starts_at: Optional[str] = "2026-03-04T13:02:10Z",
    ends_at: Optional[str] = None,
    fingerprint: Optional[str] = None,
    group_key: str = 'incidentd-webhook/{alertname="LatencySLOBurn"}',
    receiver: str = "incidentd-webhook",
) -> Dict[str, Any]:
    """Build an Alertmanager webhook payload with sane lab defaults."""
    resolved_labels = {
        "alertname": "LatencySLOBurn",
        "cluster": "lab-eu-west",
        "environment": "lab",
        "instance": "checkout-api-7d9f8c:8080",
        "job": "checkout-api",
        "service": "checkout-api",
        "severity": "critical",
        "slo": "checkout-availability-99.95",
    }
    resolved_labels.update(labels or {})
    alert: Dict[str, Any] = {
        "status": status,
        "labels": resolved_labels,
        "annotations": {
            "summary": "checkout-api is burning the availability SLO budget",
            "description": "Burn rate 14.2 on checkout-availability-99.95.",
            "contributing_factor": "slow-path recommendation call enabled for all traffic",
            **(annotations or {}),
        },
        "startsAt": starts_at,
        "endsAt": ends_at if ends_at is not None else "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus.lab.local:9090/graph?g0.expr=LatencySLOBurn",
    }
    if fingerprint is not None:
        alert["fingerprint"] = fingerprint
    return {
        "version": "4",
        "groupKey": group_key,
        "truncatedAlerts": 0,
        "status": status,
        "receiver": receiver,
        "groupLabels": {"alertname": resolved_labels["alertname"]},
        "commonLabels": resolved_labels,
        "commonAnnotations": copy.deepcopy(alert["annotations"]),
        "externalURL": "http://alertmanager.lab.local:9093",
        "alerts": [alert],
    }


@pytest.fixture()
def policy():
    return load_policy(POLICY_PATH)


def seed_incident(store: Store, incident_id: str = "INC-2026-0001", **overrides):
    """Create a persisted incident so timeline/alert rows satisfy their FKs."""
    from incidentd.models import Incident

    fields = {
        "id": incident_id,
        "fingerprint": "4a7d9eb30581fff8",
        "service": "checkout-api",
        "severity": "Sev1",
        "status": "open",
        "title": "seeded incident",
        "owner": "primary-oncall",
        "opened_at": INCIDENT_START,
        "ack_deadline_minutes": 5,
        "resolve_deadline_minutes": 120,
    }
    fields.update(overrides)
    return store.create_incident(Incident(**fields))


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def store(tmp_path) -> Store:
    database = Store(tmp_path / "incidents.db")
    yield database
    database.close()


@pytest.fixture()
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture()
def service(store, policy, notifier, clock) -> IncidentService:
    return IncidentService(store, policy, notifier=notifier, clock=clock)


@pytest.fixture()
def client(tmp_path, notifier, clock) -> TestClient:
    app = create_app(
        db_path=str(tmp_path / "api.db"),
        policy_path=str(POLICY_PATH),
        notifier=notifier,
        clock=clock,
    )
    with TestClient(app) as test_client:
        yield test_client
    app.state.store.close()

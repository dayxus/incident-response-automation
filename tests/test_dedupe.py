"""Dedupe contract: one incident per fingerprint, two events on the timeline."""

from __future__ import annotations

from datetime import timedelta

import pytest

from incidentd.dedupe import ACTION_ATTACH, ACTION_CREATE, ACTION_REOPEN, DedupeEngine
from incidentd.models import (
    AlertEvent,
    Incident,
    IncidentStatus,
    Severity,
    compute_fingerprint,
    to_iso,
    utcnow,
)
from incidentd.service import IncidentService

from .conftest import INCIDENT_START, example_payload, make_payload


def _alert_received_entries(store, incident_id):
    return [entry for entry in store.list_timeline(incident_id) if entry.kind == "alert_received"]


def test_same_fingerprint_twice_is_one_incident_with_two_timeline_events(service, store):
    payload = make_payload()

    first = service.ingest_payload(payload)[1][0]
    second = service.ingest_payload(payload)[1][0]

    incidents = store.list_incidents()
    assert len(incidents) == 1
    assert first.incident_id == second.incident_id
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.action == ACTION_ATTACH

    events = _alert_received_entries(store, first.incident_id)
    assert len(events) == 2, "two deliveries must leave two timeline events"
    assert events[0].metadata["deduplicated"] is False
    assert events[1].metadata["deduplicated"] is True
    assert store.get_incident(first.incident_id).alert_count == 2
    assert store.count_alerts(deduplicated=True) == 1
    assert store.count_alerts(deduplicated=False) == 1


def test_committed_repeat_delivery_example_is_deduplicated(service, store):
    """01 and 02 are the same fingerprint: the repeat must not open incident #2."""
    first = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    second = service.ingest_payload(example_payload("02-firing-checkout-api-repeat.json"))[1][0]

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert first.incident_id == second.incident_id
    assert len(store.list_incidents()) == 1


def test_fingerprint_is_computed_when_payload_omits_it(service, store):
    labels = make_payload()["alerts"][0]["labels"]
    expected = compute_fingerprint(labels)
    assert expected == example_payload("01-firing-checkout-api.json")["alerts"][0]["fingerprint"]

    payload = make_payload()
    assert "fingerprint" not in payload["alerts"][0]
    result = service.ingest_payload(payload)[1][0]
    incident = store.get_incident(result.incident_id)
    assert incident.fingerprint == expected


def test_different_fingerprints_open_different_incidents(service, store):
    checkout = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    search = service.ingest_payload(example_payload("03-firing-search-api.json"))[1][0]
    assert checkout.incident_id != search.incident_id
    assert len(store.list_incidents()) == 2
    assert {incident.service for incident in store.list_incidents()} == {
        "checkout-api",
        "search-api",
    }


def test_refire_inside_suppression_window_reopens_the_same_incident(service, store, clock):
    opened = service.ingest_payload(make_payload())[1][0]
    clock.set(INCIDENT_START + timedelta(minutes=40))
    service.ingest_payload(
        make_payload(
            status="resolved",
            starts_at="2026-03-04T13:02:10Z",
            ends_at="2026-03-04T13:42:10Z",
        )
    )
    resolved = store.get_incident(opened.incident_id)
    assert resolved.status is IncidentStatus.RESOLVED

    clock.set(INCIDENT_START + timedelta(minutes=45))  # 5 min after resolution
    again = service.ingest_payload(make_payload(starts_at="2026-03-04T13:47:10Z"))[1][0]

    assert again.incident_id == opened.incident_id
    assert again.action == ACTION_REOPEN
    assert again.deduplicated is True
    reopened = store.get_incident(opened.incident_id)
    assert reopened.status is IncidentStatus.OPEN
    assert reopened.reopened_count == 1
    assert reopened.resolved_at is None
    assert [entry.kind for entry in store.list_timeline(reopened.id)] == [
        "opened",
        "alert_received",
        "notified",
        "alert_received",
        "resolved",
        "notified",
        "alert_received",
        "reopened",
        "notified",
    ]


def test_refire_outside_suppression_window_opens_a_new_incident(service, store, clock):
    opened = service.ingest_payload(make_payload())[1][0]
    clock.set(INCIDENT_START + timedelta(minutes=40))
    service.ingest_payload(make_payload(status="resolved", ends_at="2026-03-04T13:42:10Z"))

    clock.set(INCIDENT_START + timedelta(hours=3))  # way past suppression_seconds=900
    again = service.ingest_payload(make_payload(starts_at="2026-03-04T15:32:10Z"))[1][0]

    assert again.incident_id != opened.incident_id
    assert again.deduplicated is False
    assert again.action == ACTION_CREATE
    second = store.get_incident(again.incident_id)
    assert "previous incident %s" % opened.incident_id in store.list_timeline(second.id)[0].message
    assert len(store.list_incidents()) == 2


def test_duplicate_resolved_notification_is_idempotent(service, store, clock):
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    resolved_payload = example_payload("04-resolved-checkout-api.json")

    first = service.ingest_payload(resolved_payload)[1][0]
    timeline_length = len(store.list_timeline(opened.incident_id))

    clock.set(INCIDENT_START + timedelta(minutes=48))  # the retry arrives later
    second = service.ingest_payload(resolved_payload)[1][0]

    assert first.incident_id == second.incident_id == opened.incident_id
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.action == "already_resolved"
    assert len(store.list_timeline(opened.incident_id)) == timeline_length + 1
    assert store.get_incident(opened.incident_id).status is IncidentStatus.RESOLVED

    duplicates = [
        entry
        for entry in store.list_timeline(opened.incident_id)
        if "duplicate resolved notification ignored" in entry.message
    ]
    assert len(duplicates) == 1
    assert duplicates[0].metadata["deduplicated"] is True
    # The duplicate must not move the recorded resolution time.
    assert to_iso(store.get_incident(opened.incident_id).resolved_at) == "2026-03-04T13:49:10Z"


def test_engine_decides_attach_inside_and_outside_the_window(policy):
    engine = DedupeEngine(policy)
    labels = make_payload()["alerts"][0]["labels"]
    fingerprint = engine.fingerprint_for(labels)
    incident = Incident(
        id="INC-2026-0001",
        fingerprint=fingerprint,
        service="checkout-api",
        severity=Severity.SEV1,
        status=IncidentStatus.OPEN,
        title="t",
        owner="primary-oncall",
        opened_at=INCIDENT_START,
        last_alert_at=INCIDENT_START,
    )

    inside = engine.decide(fingerprint, INCIDENT_START + timedelta(seconds=30), incident)
    outside = engine.decide(fingerprint, INCIDENT_START + timedelta(seconds=1200), incident)

    assert inside.action == ACTION_ATTACH
    assert "dedupe window" in inside.reason
    assert outside.action == ACTION_ATTACH, "an open incident keeps absorbing alerts"
    assert "still open" in outside.reason


def test_engine_creates_when_no_incident_is_known(policy):
    engine = DedupeEngine(policy)
    decision = engine.decide("deadbeefdeadbeef", INCIDENT_START, None)
    assert decision.action == ACTION_CREATE
    assert decision.deduplicated is False
    assert "first alert" in decision.reason


def test_group_key_comes_from_policy(policy):
    engine = DedupeEngine(policy)
    key = engine.group_key(make_payload()["alerts"][0]["labels"])
    assert key == "service=checkout-api,alertname=LatencySLOBurn,cluster=lab-eu-west"
    assert engine.describe() == {
        "window_seconds": 600,
        "suppression_seconds": 900,
        "group_by": ["service", "alertname", "cluster"],
    }


def test_reopen_path_uses_the_policy_suppression_window(policy, store, notifier, clock):
    """The suppression window is policy data: shrink it and the reopen disappears."""
    from dataclasses import replace

    tight_policy = replace(policy, dedupe=replace(policy.dedupe, suppression_seconds=60))
    service = IncidentService(store, tight_policy, notifier=notifier, clock=clock)

    opened = service.ingest_payload(make_payload())[1][0]
    clock.set(INCIDENT_START + timedelta(minutes=10))
    service.ingest_payload(make_payload(status="resolved", ends_at="2026-03-04T13:12:10Z"))

    clock.set(INCIDENT_START + timedelta(minutes=15))
    again = service.ingest_payload(make_payload(starts_at="2026-03-04T13:17:10Z"))[1][0]
    assert again.incident_id != opened.incident_id, "180s > 60s window: new incident"


def test_unknown_alert_status_is_rejected():
    with pytest.raises(ValueError):
        AlertEvent(fingerprint="x", status="flapping", alertname="a")

    event = AlertEvent(fingerprint="x", status="FIRING", alertname="a", received_at=utcnow())
    assert event.status == "firing"
    assert event.is_resolved is False

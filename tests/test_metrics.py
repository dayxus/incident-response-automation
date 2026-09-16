"""MTTD/MTTR are derived from the recorded timeline, within 1s tolerance."""

from __future__ import annotations

from datetime import timedelta

import pytest

from incidentd.metrics import render_metrics
from incidentd.models import format_duration

from .conftest import INCIDENT_START, example_payload, make_payload

ONE_SECOND = 1.0


def _acked_and_resolved(service, store):
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    service.ack(opened.incident_id, actor="oncall-alice", at=INCIDENT_START + timedelta(minutes=4))
    service.ingest_payload(example_payload("04-resolved-checkout-api.json"))
    return store.get_incident(opened.incident_id)


def _delivered_stream(service, store):
    """The deliveries of the committed playbook: open, retry, ack, resolved.

    The retry (02) is the only delivery Alertmanager repeats, so it is the only
    one the dedupe counters may attribute: a resolved notification is a state
    change, not a repeat of a known fingerprint.
    """
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    service.ingest_payload(example_payload("02-firing-checkout-api-repeat.json"))
    service.ack(opened.incident_id, actor="oncall-alice", at=INCIDENT_START + timedelta(minutes=4))
    service.ingest_payload(example_payload("04-resolved-checkout-api.json"))
    return store.get_incident(opened.incident_id)


def test_mttd_is_ack_minus_open(service, store):
    incident = _acked_and_resolved(service, store)
    assert incident.mttd_seconds is not None
    assert abs(incident.mttd_seconds - 240) <= ONE_SECOND
    assert format_duration(incident.mttd_seconds) == "4m"


def test_mttr_is_resolve_minus_open(service, store):
    incident = _acked_and_resolved(service, store)
    assert incident.mttr_seconds is not None
    assert abs(incident.mttr_seconds - 2820) <= ONE_SECOND
    assert format_duration(incident.mttr_seconds) == "47m"


def test_mttd_uses_the_recorded_ack_from_the_payload_clock(service, store):
    """The resolved alert carries endsAt, so MTTR never depends on wall-clock time."""
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    service.ack(opened.incident_id, at=INCIDENT_START + timedelta(seconds=95))
    service.ingest_payload(example_payload("04-resolved-checkout-api.json"))

    incident = store.get_incident(opened.incident_id)
    assert abs(incident.mttd_seconds - 95) <= ONE_SECOND
    assert abs(incident.mttr_seconds - 2820) <= ONE_SECOND


def test_snapshot_aggregates_open_and_resolved(service, store):
    _acked_and_resolved(service, store)
    service.ingest_payload(example_payload("03-firing-search-api.json"))

    snapshot = store.metrics_snapshot()
    assert snapshot.open_incidents == 1
    assert snapshot.acknowledged_incidents == 0
    assert snapshot.resolved_incidents == 1
    assert abs(snapshot.mean_mttr - 2820) <= ONE_SECOND
    assert abs(snapshot.mean_mttd - 240) <= ONE_SECOND
    assert snapshot.totals_by_severity_service == [
        ("Sev1", "checkout-api", 1),
        ("Sev3", "search-api", 1),
    ]


def test_snapshot_without_resolved_incidents_has_no_mttr(service, store):
    service.ingest_payload(make_payload())
    snapshot = store.metrics_snapshot()
    assert snapshot.mean_mttr is None
    assert snapshot.mean_mttd is None
    assert snapshot.mttr_durations == []


def test_histogram_buckets_reflect_measured_ack(service, store):
    _acked_and_resolved(service, store)
    text = render_metrics(store.metrics_snapshot())

    assert 'incident_ack_seconds_bucket{le="60"} 0' in text
    assert 'incident_ack_seconds_bucket{le="300"} 1' in text
    assert 'incident_ack_seconds_bucket{le="+Inf"} 1' in text
    assert "incident_ack_seconds_sum 240" in text
    assert "incident_ack_seconds_count 1" in text


def test_metrics_text_exposes_all_required_series(service, store):
    _delivered_stream(service, store)
    text = render_metrics(store.metrics_snapshot())

    assert "# TYPE incidents_open gauge" in text
    assert "incidents_open 0" in text
    assert "# TYPE incident_ack_seconds histogram" in text
    assert "# TYPE mttr_seconds gauge" in text
    assert "mttr_seconds 2820" in text
    assert 'incidents_total{severity="Sev1",service="checkout-api"} 1' in text
    assert "alerts_received_total 3" in text
    assert "alerts_deduplicated_total 1" in text
    assert text.endswith("\n")


def test_dedupe_ratio_reflects_the_delivered_stream(service, store):
    _delivered_stream(service, store)
    snapshot = store.metrics_snapshot()
    assert snapshot.alerts_received == 3
    assert snapshot.alerts_deduplicated == 1
    assert snapshot.dedupe_ratio == pytest.approx(1 / 3)


def test_metrics_text_with_empty_database(store):
    text = render_metrics(store.metrics_snapshot())
    assert "incidents_open 0" in text
    assert "mttr_seconds 0" in text
    assert 'incidents_total{severity="none",service="none"} 0' in text
    assert 'incident_ack_seconds_bucket{le="+Inf"} 0' in text


def test_ack_deadline_states(service, store):
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    incident = store.get_incident(opened.incident_id)
    assert incident.ack_deadline_at == INCIDENT_START + timedelta(minutes=5)
    assert (
        incident.acknowledgement_deadline_state(INCIDENT_START + timedelta(minutes=2)) == "pending"
    )
    assert (
        incident.acknowledgement_deadline_state(INCIDENT_START + timedelta(minutes=6)) == "overdue"
    )
    assert incident.acknowledgement_deadline_state() in {"pending", "overdue"}

    service.ack(opened.incident_id, at=INCIDENT_START + timedelta(minutes=4))
    assert store.get_incident(opened.incident_id).acknowledgement_deadline_state() == "met"

    later = service.ingest_payload(example_payload("05-firing-payments-worker.json"))[1][0]
    service.ack(later.incident_id, at=INCIDENT_START + timedelta(minutes=20))
    assert store.get_incident(later.incident_id).acknowledgement_deadline_state() == "late"
    assert store.get_incident(later.incident_id).ack_deadline_breached is True

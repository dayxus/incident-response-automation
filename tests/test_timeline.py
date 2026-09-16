"""Timeline contract: chronological, complete, and append-only in the database."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from incidentd.models import Incident, IncidentStatus
from incidentd.store import Store

from .conftest import INCIDENT_START, example_payload, make_payload, seed_incident


def test_opening_an_incident_writes_open_and_alert_entries(service, store):
    result = service.ingest_payload(make_payload())[1][0]
    kinds = [entry.kind for entry in store.list_timeline(result.incident_id)]
    assert kinds == ["opened", "alert_received", "notified"]


def test_full_lifecycle_leaves_a_complete_auditable_timeline(service, store, clock):
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    service.ack(opened.incident_id, actor="oncall-alice", at=INCIDENT_START + timedelta(minutes=4))
    clock.set(INCIDENT_START + timedelta(minutes=47))  # the resolved delivery arrives now
    resolved = service.ingest_payload(example_payload("04-resolved-checkout-api.json"))[1][0]

    entries = store.list_timeline(opened.incident_id)
    assert [entry.kind for entry in entries] == [
        "opened",
        "alert_received",
        "notified",
        "acknowledged",
        "alert_received",
        "resolved",
        "notified",
    ]
    assert entries[3].actor == "oncall-alice"
    assert "within the 5m ack deadline" in entries[3].message
    assert entries[5].actor == "alertmanager"
    assert resolved.status == IncidentStatus.RESOLVED.value


def test_entries_are_returned_in_chronological_order(store):
    seed_incident(store)
    store.append_timeline(
        "INC-2026-0001", "resolved", "second", ts=INCIDENT_START + timedelta(hours=1)
    )
    store.append_timeline("INC-2026-0001", "opened", "first", ts=INCIDENT_START)
    store.append_timeline(
        "INC-2026-0001", "alert_received", "middle", ts=INCIDENT_START + timedelta(minutes=1)
    )

    messages = [entry.message for entry in store.list_timeline("INC-2026-0001")]
    assert messages == ["first", "middle", "second"]


def test_entries_of_the_same_second_keep_insertion_order(store):
    seed_incident(store)
    store.append_timeline("INC-2026-0001", "opened", "a", ts=INCIDENT_START)
    store.append_timeline("INC-2026-0001", "alert_received", "b", ts=INCIDENT_START)
    store.append_timeline("INC-2026-0001", "notified", "c", ts=INCIDENT_START)

    ids = [entry.id for entry in store.list_timeline("INC-2026-0001")]
    assert ids == sorted(ids)
    assert [entry.message for entry in store.list_timeline("INC-2026-0001")] == ["a", "b", "c"]


def test_timeline_is_append_only_updates_are_rejected(store):
    seed_incident(store)
    entry = store.append_timeline("INC-2026-0001", "opened", "cannot be rewritten")

    with pytest.raises(sqlite3.Error) as error:
        store.connection.execute(
            "UPDATE timeline SET message = 'tampered' WHERE id = ?", (entry.id,)
        )
    assert "append-only" in str(error.value)
    store.connection.rollback()

    assert store.list_timeline("INC-2026-0001")[0].message == "cannot be rewritten"


def test_timeline_is_append_only_deletes_are_rejected(store):
    seed_incident(store)
    store.append_timeline("INC-2026-0001", "opened", "stays here")

    with pytest.raises(sqlite3.Error) as error:
        store.connection.execute("DELETE FROM timeline WHERE incident_id = 'INC-2026-0001'")
    assert "append-only" in str(error.value)
    store.connection.rollback()

    assert len(store.list_timeline("INC-2026-0001")) == 1


def test_alert_events_are_append_only_too(store, service):
    result = service.ingest_payload(make_payload())[1][0]
    with pytest.raises(sqlite3.Error) as error:
        store.connection.execute("UPDATE alert_events SET status = 'resolved'")
    assert "append-only" in str(error.value)
    store.connection.rollback()

    events = store.list_alert_events(store.get_incident(result.incident_id).fingerprint)
    assert [event["status"] for event in events] == ["firing"]


def test_timeline_survives_reopening_the_database(tmp_path, policy):
    path = tmp_path / "persisted.db"
    first = Store(path)
    first.create_incident(
        Incident(
            id="INC-2026-0001",
            fingerprint="abc",
            service="checkout-api",
            severity="Sev1",
            status="open",
            title="t",
            owner="primary-oncall",
            opened_at=INCIDENT_START,
        )
    )
    first.append_timeline("INC-2026-0001", "opened", "written before restart", ts=INCIDENT_START)
    first.close()

    second = Store(path)
    try:
        entries = second.list_timeline("INC-2026-0001")
        assert [entry.message for entry in entries] == ["written before restart"]
        assert second.get_incident("INC-2026-0001").service == "checkout-api"
    finally:
        second.close()


def test_timeline_entry_render_line_is_readable(store):
    seed_incident(store)
    entry = store.append_timeline(
        "INC-2026-0001",
        "acknowledged",
        "acknowledged by oncall-alice",
        ts=datetime(2026, 3, 4, 13, 6, 10, tzinfo=timezone.utc),
        actor="oncall-alice",
    )
    assert entry.render_line() == (
        "- 2026-03-04T13:06:10Z · `acknowledged` · oncall-alice — acknowledged by oncall-alice"
    )


def test_metadata_is_stored_as_json(store):
    seed_incident(store)
    entry = store.append_timeline(
        "INC-2026-0001",
        "alert_received",
        "duplicate",
        metadata={"deduplicated": True, "fingerprint": "4a7d9eb30581fff8"},
    )
    reloaded = store.list_timeline("INC-2026-0001")[0]
    assert reloaded.id == entry.id
    assert reloaded.metadata == {"deduplicated": True, "fingerprint": "4a7d9eb30581fff8"}


def test_update_incident_rejects_unknown_columns(store):
    with pytest.raises(ValueError) as error:
        store.update_incident("INC-2026-0001", nonexistent_column=1)
    assert "unknown incident columns" in str(error.value)

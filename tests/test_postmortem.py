"""Postmortem contract: machine-filled facts, human-filled markers, no invented rows."""

from __future__ import annotations

from datetime import timedelta

from incidentd.postmortem import (
    ACTION_ITEMS_HEADER,
    render,
    required_placeholders,
)

from .conftest import INCIDENT_START, example_payload, make_payload


def _resolved_incident(service, store, ack: bool = True):
    opened = service.ingest_payload(example_payload("01-firing-checkout-api.json"))[1][0]
    if ack:
        service.ack(
            opened.incident_id,
            actor="oncall-alice",
            at=INCIDENT_START + timedelta(minutes=4),
        )
    service.ingest_payload(example_payload("04-resolved-checkout-api.json"))
    return store.get_incident(opened.incident_id)


def test_title_contains_service_and_severity(service, store):
    incident = _resolved_incident(service, store)
    document = service.postmortem(incident.id)
    assert document.startswith("# Postmortem — checkout-api (Sev1)")


def test_document_contains_the_real_timeline(service, store):
    incident = _resolved_incident(service, store)
    entries = store.list_timeline(incident.id)
    document = service.postmortem(incident.id)

    assert "## Timeline" in document
    for entry in entries:
        assert entry.render_line() in document
    assert "2026-03-04T13:02:10Z" in document
    assert "2026-03-04T13:49:10Z" in document


def test_action_items_table_is_empty_and_marked_for_humans(service, store):
    incident = _resolved_incident(service, store)
    document = service.postmortem(incident.id)
    lines = document.splitlines()

    assert ACTION_ITEMS_HEADER in document
    index = lines.index(ACTION_ITEMS_HEADER)
    assert lines[index + 1] == "| --- | --- | --- | --- | --- |"
    assert lines[index + 2] == ""
    # Nothing after the header: the action-items table is for humans to fill.
    assert [line for line in lines[index + 2 :] if line.startswith("| ")] == []
    assert "<!-- preencher: uma linha por ação" in document


def test_required_placeholders_are_present(service, store):
    incident = _resolved_incident(service, store)
    document = service.postmortem(incident.id)
    for placeholder in required_placeholders():
        assert placeholder in document
    assert document.count("<!-- preencher:") >= len(required_placeholders())
    assert "causa raiz" in document


def test_metrics_are_rendered_with_real_values(service, store):
    incident = _resolved_incident(service, store)
    document = service.postmortem(incident.id)

    assert "| Resolved at | 2026-03-04T13:49:10Z (MTTR 47m) |" in document
    assert "| Acknowledged at | 2026-03-04T13:06:10Z (MTTD 4m) |" in document
    assert "| Ack deadline | 5m (met) |" in document
    assert "MTTD 4m" in document


def test_missing_acknowledgement_is_reported_honestly(service, store):
    incident = _resolved_incident(service, store, ack=False)
    document = service.postmortem(incident.id)

    assert "- Never acknowledged before resolution" in document
    assert "breached: resolved without ever being acknowledged" in document
    assert incident.acknowledgement_deadline_state() == "missed"


def test_contributing_factors_come_from_annotations(service, store):
    incident = _resolved_incident(service, store)
    document = service.postmortem(incident.id)

    section = document.split("## Contributing factors")[1].split("## Root cause")[0]
    assert "slow-path recommendation call enabled for all traffic" in section
    assert "preencher" not in section


def test_contributing_factors_fall_back_to_a_marker(service, store):
    payload = make_payload(annotations={"contributing_factor": ""})
    payload["alerts"][0]["annotations"].pop("contributing_factor")
    result = service.ingest_payload(payload)[1][0]
    document = service.postmortem(result.incident_id)

    section = document.split("## Contributing factors")[1].split("## Root cause")[0]
    assert "preencher: fatores contribuintes" in section


def test_open_incident_postmortem_has_no_mttr(service, store):
    result = service.ingest_payload(make_payload())[1][0]
    document = service.postmortem(result.incident_id)

    assert "| Status | open |" in document
    assert "| Resolved at | n/a (MTTR n/a) |" in document
    assert "(policy rule `critical-tier1`)" in document


def test_render_is_deterministic_for_the_same_incident(service, store):
    incident = _resolved_incident(service, store)
    timeline = store.list_timeline(incident.id)
    first = render(incident, timeline)
    second = render(incident, timeline)
    assert first == second
    assert first.endswith(
        "Machine-filled fields are exact; every `preencher` marker is for the humans.\n"
    )
    assert incident.fingerprint in first


def test_unknown_incident_raises(service):
    import pytest

    with pytest.raises(KeyError):
        service.postmortem("INC-2026-9999")

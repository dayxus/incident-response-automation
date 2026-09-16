"""API contract: idempotent ingest, validated transitions (409), metrics, health."""

from __future__ import annotations

import pytest

from .conftest import example_payload, make_payload


def _create(client, payload=None):
    response = client.post("/webhook/alertmanager", json=payload or make_payload())
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz_and_readyz(client):
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "policy": "policy.yaml",
        "notifier": "recording",
    }


def test_index_lists_the_contract(client):
    body = client.get("/").json()
    assert body["service"] == "incidentd"
    assert "POST /webhook/alertmanager" in body["endpoints"]
    assert body["policy"]["services"][0] == "checkout-api"


def test_webhook_creates_one_incident_and_reports_its_shape(client):
    body = _create(client)
    assert set(body) == {"incident_id", "status", "deduplicated", "alerts_processed", "results"}
    assert body["status"] == "open"
    assert body["deduplicated"] is False
    assert body["alerts_processed"] == 1
    assert body["results"][0]["severity"] == "Sev1"
    assert body["results"][0]["service"] == "checkout-api"
    assert body["results"][0]["action"] == "create"


def test_webhook_is_idempotent_for_a_repeated_payload(client):
    first = _create(client)
    second = _create(client)

    assert second["incident_id"] == first["incident_id"]
    assert second["deduplicated"] is True
    listings = client.get("/incidents").json()
    assert listings["count"] == 1
    assert listings["incidents"][0]["alert_count"] == 2


def test_webhook_rejects_payload_without_alerts(client):
    response = client.post(
        "/webhook/alertmanager",
        json={"version": "4", "status": "firing", "alerts": []},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "payload carries no alerts"


def test_webhook_rejects_malformed_payload(client):
    response = client.post("/webhook/alertmanager", json={"alerts": "not-a-list"})
    assert response.status_code == 422


def test_list_filters_and_validation(client):
    _create(client)
    _create(client, example_payload("03-firing-search-api.json"))

    assert client.get("/incidents").json()["count"] == 2
    assert client.get("/incidents", params={"severity": "Sev3"}).json()["count"] == 1
    assert client.get("/incidents", params={"service": "checkout-api"}).json()["count"] == 1
    assert client.get("/incidents", params={"status": "open"}).json()["count"] == 2
    assert client.get("/incidents", params={"status": "resolved"}).json()["count"] == 0

    bad = client.get("/incidents", params={"status": "closed"})
    assert bad.status_code == 400
    assert "unknown status" in bad.json()["detail"]["reason"]

    bad_severity = client.get("/incidents", params={"severity": "Sev9"})
    assert bad_severity.status_code == 400


def test_list_exposes_duration_and_owner(client):
    _create(client)
    incident = client.get("/incidents").json()["incidents"][0]
    assert incident["owner"] == "primary-oncall"
    assert incident["duration_seconds"] >= 0
    assert incident["mttr_seconds"] is None
    assert incident["mttd_seconds"] is None


def test_get_incident_returns_full_timeline(client):
    created = _create(client)
    detail = client.get("/incidents/%s" % created["incident_id"]).json()

    assert detail["id"] == created["incident_id"]
    assert [entry["kind"] for entry in detail["timeline"]] == [
        "opened",
        "alert_received",
        "notified",
    ]
    assert detail["labels"]["_policy_rule"] == "critical-tier1"
    assert detail["ack_deadline_minutes"] == 5
    assert "ack_deadline_state" in detail


def test_get_unknown_incident_is_404(client):
    response = client.get("/incidents/INC-2026-9999")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "no such incident"


def test_ack_then_resolve_transitions(client):
    created = _create(client)
    incident_id = created["incident_id"]

    acked = client.post(
        "/incidents/%s/ack" % incident_id,
        json={"actor": "oncall-alice", "at": "2026-03-04T13:06:10Z"},
    )
    assert acked.status_code == 200
    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acknowledged_at"] == "2026-03-04T13:06:10Z"
    assert acked.json()["mttd_seconds"] == 240.0

    resolved = client.post(
        "/incidents/%s/resolve" % incident_id,
        json={"actor": "oncall-alice", "at": "2026-03-04T13:49:10Z"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["mttr_seconds"] == 2820.0


def test_ack_without_body_uses_the_clock(client):
    created = _create(client)
    acked = client.post("/incidents/%s/ack" % created["incident_id"])
    assert acked.status_code == 200
    assert acked.json()["acknowledged_at"] == "2026-03-04T13:02:10Z"


def test_ack_after_resolve_is_409_with_reason(client):
    created = _create(client)
    incident_id = created["incident_id"]
    client.post("/incidents/%s/resolve" % incident_id, json={"at": "2026-03-04T13:49:10Z"})

    response = client.post("/incidents/%s/ack" % incident_id)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "already resolved" in detail["reason"]
    assert detail["current_status"] == "resolved"
    assert detail["requested_status"] == "acknowledged"
    assert detail["allowed_transitions"] == []


def test_double_ack_is_409(client):
    created = _create(client)
    incident_id = created["incident_id"]
    assert client.post("/incidents/%s/ack" % incident_id).status_code == 200

    response = client.post("/incidents/%s/ack" % incident_id)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "incident is already acknowledged"
    assert response.json()["detail"]["allowed_transitions"] == ["resolved"]


def test_double_resolve_is_409(client):
    created = _create(client)
    incident_id = created["incident_id"]
    client.post("/incidents/%s/resolve" % incident_id, json={"at": "2026-03-04T13:49:10Z"})

    response = client.post("/incidents/%s/resolve" % incident_id)
    assert response.status_code == 409
    assert "already resolved" in response.json()["detail"]["reason"]


def test_transition_on_unknown_incident_is_404(client):
    assert client.post("/incidents/INC-2026-0007/ack").status_code == 404
    assert client.post("/incidents/INC-2026-0007/resolve").status_code == 404


def test_transition_with_invalid_timestamp_is_400(client):
    created = _create(client)
    incident_id = created["incident_id"]

    malformed = client.post("/incidents/%s/ack" % incident_id, json={"at": "yesterday"})
    assert malformed.status_code == 400
    assert "invalid 'at' timestamp" in malformed.json()["detail"]["reason"]

    before_open = client.post(
        "/incidents/%s/ack" % incident_id, json={"at": "2026-03-04T12:00:00Z"}
    )
    assert before_open.status_code == 400
    assert "precedes the incident open time" in before_open.json()["detail"]["reason"]


def test_postmortem_endpoint_returns_markdown(client):
    created = _create(client)
    response = client.get("/incidents/%s/postmortem.md" % created["incident_id"])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# Postmortem — checkout-api (Sev1)")
    assert "<!-- preencher:" in response.text


def test_postmortem_for_unknown_incident_is_404(client):
    assert client.get("/incidents/INC-2026-9999/postmortem.md").status_code == 404


def test_metrics_endpoint_is_prometheus_text(client):
    _create(client)
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = response.text
    assert "incidents_open 1" in body
    assert "# TYPE incident_ack_seconds histogram" in body
    assert "# TYPE mttr_seconds gauge" in body
    assert 'incidents_total{severity="Sev1",service="checkout-api"} 1' in body


def test_notifications_are_injected_not_sent(client, notifier):
    """No network: the injected notifier records what would have been paged."""
    created = _create(client)
    assert notifier.kinds() == ["page"]
    assert notifier.messages[0][0] == created["incident_id"]
    assert "pagerduty-primary" in notifier.messages[0][2]
    assert notifier.messages[0][2].startswith("Sev1 for checkout-api routed to page-primary")


def test_webhook_with_several_alerts_returns_a_result_per_alert(client):
    payload = make_payload()
    payload["alerts"].append(
        {
            "status": "firing",
            "labels": {
                "alertname": "LatencyP99High",
                "cluster": "lab-eu-west",
                "service": "search-api",
                "severity": "warning",
                "slo": "search-latency-p99",
            },
            "annotations": {"summary": "search-api p99 above SLO"},
            "startsAt": "2026-03-04T13:07:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "53d649d262c37bcc",
        }
    )
    body = _create(client, payload)

    assert body["alerts_processed"] == 2
    assert len(body["results"]) == 2
    assert {result["severity"] for result in body["results"]} == {"Sev1", "Sev3"}
    assert body["incident_id"] == body["results"][0]["incident_id"]


# --- notifier implementations: exercised with injected streams/openers, never the network ---


def test_notifier_factory_covers_every_backend():
    from incidentd.notify import NullNotifier, StdoutNotifier, WebhookNotifier, build_notifier

    assert isinstance(build_notifier("null"), NullNotifier)
    assert isinstance(build_notifier("stdout"), StdoutNotifier)
    assert isinstance(build_notifier("webhook", url="http://chat.lab.local/hook"), WebhookNotifier)
    assert isinstance(build_notifier("NULL"), NullNotifier)
    with pytest.raises(ValueError):
        build_notifier("pagerduty")
    with pytest.raises(ValueError):
        WebhookNotifier("")


def test_stdout_notifier_prints_and_records(store, policy, clock):
    import io

    from incidentd.notify import StdoutNotifier
    from incidentd.service import IncidentService

    stream = io.StringIO()
    notifier = StdoutNotifier(stream=stream)
    service = IncidentService(store, policy, notifier=notifier, clock=clock)
    created = service.ingest_payload(make_payload())[1][0]

    assert "[notify:page] Sev1 %s checkout-api" % created.incident_id in stream.getvalue()
    assert len(notifier.sent) == 1
    incident_id, kind, message = notifier.sent[0]
    assert incident_id == created.incident_id
    assert kind == "page"
    assert "ack within 5m via pagerduty-primary" in message


def test_webhook_notifier_posts_json_with_an_injected_opener(store, policy, clock):
    import io
    import json as json_module

    from incidentd.notify import WebhookNotifier
    from incidentd.service import IncidentService

    captured = {}

    class FakeResponse:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["content_type"] = request.get_header("Content-type")
        captured["payload"] = json_module.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    notifier = WebhookNotifier(
        "http://chat.lab.local/hook", timeout=3.0, opener=fake_opener, stream=io.StringIO()
    )
    service = IncidentService(store, policy, notifier=notifier, clock=clock)
    created = service.ingest_payload(make_payload())[1][0]

    assert captured["url"] == "http://chat.lab.local/hook"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 3.0
    assert captured["payload"]["kind"] == "page"
    assert captured["payload"]["incident"]["id"] == created.incident_id
    assert captured["payload"]["incident"]["severity"] == "Sev1"


def test_webhook_notifier_transport_failure_never_breaks_ingest(store, policy, clock):
    import io
    import urllib.error

    from incidentd.notify import WebhookNotifier
    from incidentd.service import IncidentService

    warnings = io.StringIO()

    def failing_opener(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    notifier = WebhookNotifier("http://chat.lab.local/hook", opener=failing_opener, stream=warnings)
    service = IncidentService(store, policy, notifier=notifier, clock=clock)
    result = service.ingest_payload(make_payload())[1][0]

    assert result.incident_id
    assert "delivery of %s failed" % result.incident_id in warnings.getvalue()
    assert store.get_incident(result.incident_id) is not None


def test_null_notifier_drops_everything(store, policy, clock):
    from incidentd.notify import NullNotifier
    from incidentd.service import IncidentService

    service = IncidentService(store, policy, notifier=NullNotifier(), clock=clock)
    created = service.ingest_payload(make_payload())[1][0]
    assert store.get_incident(created.incident_id).status == "open"

"""HTTP edge: FastAPI routes on top of :class:`incidentd.service.IncidentService`.

Contract (see ``README.md``):

``POST /webhook/alertmanager``            ingest an Alertmanager delivery (idempotent)
``GET  /incidents``                       list, filtered by status/severity/service
``GET  /incidents/{id}``                  incident + full timeline
``POST /incidents/{id}/ack``              open -> acknowledged (409 otherwise)
``POST /incidents/{id}/resolve``          open|acknowledged -> resolved (409 otherwise)
``GET  /incidents/{id}/postmortem.md``    rendered blameless postmortem
``GET  /metrics``                         Prometheus exposition
``GET  /healthz`` ``GET /readyz``         liveness / readiness
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .config import ConfigError, Policy, default_policy, load_policy
from .models import AlertmanagerWebhook, Incident, TransitionError, parse_ts, utcnow
from .notify import Notifier, build_notifier
from .service import IncidentService, InvalidRequest, UnknownIncident
from .store import Store

DEFAULT_DB = os.path.join("var", "incidents.db")
DEFAULT_POLICY = os.path.join("policy", "policy.yaml")

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
MARKDOWN_CONTENT_TYPE = "text/markdown; charset=utf-8"


class TransitionRequest(BaseModel):
    """Optional body for ack/resolve.

    ``at`` allows recording a transition that happened before the API call
    (backfill, replay, clock-skewed operator) and ``actor`` names the human or
    system that performed it. Both default to "now" and "operator".
    """

    actor: Optional[str] = None
    at: Optional[str] = None


def resolve_policy(policy_path: Optional[str]) -> Policy:
    """Load the policy, falling back to built-in defaults when absent."""
    path = policy_path or os.environ.get("INCIDENTD_POLICY") or DEFAULT_POLICY
    try:
        return load_policy(path)
    except ConfigError:
        if policy_path or os.environ.get("INCIDENTD_POLICY"):
            raise
        return default_policy()


def create_app(
    db_path: Optional[str] = None,
    policy_path: Optional[str] = None,
    notifier: Optional[Notifier] = None,
    clock: Optional[Callable[[], Any]] = None,
) -> FastAPI:
    """Build the ASGI app. Tests pass a temp DB path and a recording notifier."""
    resolved_db = db_path or os.environ.get("INCIDENTD_DB") or DEFAULT_DB
    policy = resolve_policy(policy_path)
    store = Store(resolved_db)
    if notifier is None:
        notifier = build_notifier(os.environ.get("INCIDENTD_NOTIFIER", "stdout"))
    service = IncidentService(store, policy, notifier=notifier, clock=clock or utcnow)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="incidentd",
        version=__version__,
        lifespan=lifespan,
        description=(
            "Alertmanager webhook to managed incident lifecycle: dedupe, severity policy, "
            "auditable timeline, MTTD/MTTR metrics and blameless postmortem drafts."
        ),
    )
    app.state.store = store
    app.state.policy = policy
    app.state.service = service
    app.state.notifier = notifier

    @app.get("/", tags=["meta"])
    def index() -> Dict[str, Any]:
        return {
            "service": "incidentd",
            "version": __version__,
            "endpoints": [
                "POST /webhook/alertmanager",
                "GET /incidents",
                "GET /incidents/{id}",
                "POST /incidents/{id}/ack",
                "POST /incidents/{id}/resolve",
                "GET /incidents/{id}/postmortem.md",
                "GET /metrics",
                "GET /healthz",
                "GET /readyz",
            ],
            "policy": service.policy_summary(),
        }

    @app.get("/healthz", tags=["meta"])
    def healthz() -> Dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["meta"])
    def readyz() -> Dict[str, Any]:
        if not store.healthy():
            raise HTTPException(
                status_code=503, detail={"reason": "incident store is not reachable"}
            )
        source = policy.source_path.name if policy.source_path else "built-in defaults"
        return {
            "status": "ready",
            "policy": source,
            "notifier": getattr(notifier, "name", "custom"),
        }

    @app.post("/webhook/alertmanager", tags=["ingest"])
    def alertmanager_webhook(payload: AlertmanagerWebhook) -> Dict[str, Any]:
        if not payload.alerts:
            raise HTTPException(status_code=400, detail={"reason": "payload carries no alerts"})
        _, results = service.ingest_payload(payload)
        primary = results[0]
        return {
            "incident_id": primary.incident_id,
            "status": primary.status,
            "deduplicated": primary.deduplicated,
            "alerts_processed": len(results),
            "results": [result.to_dict() for result in results],
        }

    @app.get("/incidents", tags=["incidents"])
    def list_incidents(
        status: Optional[str] = Query(default=None, description="open|acknowledged|resolved"),
        severity: Optional[str] = Query(default=None, description="Sev1..Sev4"),
        service_name: Optional[str] = Query(default=None, alias="service"),
    ) -> Dict[str, Any]:
        try:
            incidents = service.list_incidents(
                status=status, severity=severity, service=service_name
            )
        except InvalidRequest as exc:
            raise HTTPException(status_code=400, detail={"reason": str(exc)}) from exc
        return {"count": len(incidents), "incidents": incidents}

    @app.get("/incidents/{incident_id}", tags=["incidents"])
    def get_incident(incident_id: str) -> Dict[str, Any]:
        try:
            return service.incident_detail(incident_id)
        except UnknownIncident as exc:
            raise HTTPException(
                status_code=404, detail={"reason": "no such incident", "incident_id": incident_id}
            ) from exc

    @app.post("/incidents/{incident_id}/ack", tags=["lifecycle"])
    def ack_incident(
        incident_id: str, payload: Optional[TransitionRequest] = Body(default=None)
    ) -> Dict[str, Any]:
        return _transition(service.ack, incident_id, payload)

    @app.post("/incidents/{incident_id}/resolve", tags=["lifecycle"])
    def resolve_incident(
        incident_id: str, payload: Optional[TransitionRequest] = Body(default=None)
    ) -> Dict[str, Any]:
        return _transition(service.resolve, incident_id, payload)

    @app.get("/incidents/{incident_id}/postmortem.md", tags=["incidents"])
    def postmortem_markdown(incident_id: str) -> PlainTextResponse:
        try:
            document = service.postmortem(incident_id)
        except UnknownIncident as exc:
            raise HTTPException(
                status_code=404, detail={"reason": "no such incident", "incident_id": incident_id}
            ) from exc
        return PlainTextResponse(document, media_type=MARKDOWN_CONTENT_TYPE)

    @app.get("/metrics", tags=["meta"])
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(service.metrics_text(), media_type=PROMETHEUS_CONTENT_TYPE)

    return app


def _transition(
    call: Callable[..., Incident],
    incident_id: str,
    payload: Optional[TransitionRequest],
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if payload is not None:
        if payload.actor:
            kwargs["actor"] = payload.actor
        if payload.at:
            moment = parse_ts(payload.at)
            if moment is None:
                raise HTTPException(
                    status_code=400,
                    detail={"reason": "invalid 'at' timestamp %r, expected RFC3339" % payload.at},
                )
            kwargs["at"] = moment
    try:
        incident = call(incident_id, **kwargs)
    except UnknownIncident as exc:
        raise HTTPException(
            status_code=404, detail={"reason": "no such incident", "incident_id": incident_id}
        ) from exc
    except TransitionError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc
    except InvalidRequest as exc:
        raise HTTPException(status_code=400, detail={"reason": str(exc)}) from exc
    return incident.to_dict()


def policy_file_exists(path) -> bool:
    return Path(path).exists()


app = create_app()

"""argparse CLI: ``serve``, ``replay``, ``list``, ``postmortem``, ``metrics``.

``replay`` is the offline path used by CI and by anyone who wants to reproduce
an incident from stored payloads without an Alertmanager running.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .config import default_policy, load_policy
from .models import format_duration, parse_ts, to_iso, utcnow
from .notify import build_notifier
from .service import IncidentService
from .store import Store

DEFAULT_DB = "var/incidents.db"
DEFAULT_POLICY = "policy/policy.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m incidentd",
        description="Alertmanager webhook -> managed incident lifecycle.",
    )
    parser.add_argument("--version", action="version", version="incidentd %s" % __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--policy", default=DEFAULT_POLICY)
    serve.add_argument("--notifier", default="stdout", choices=["stdout", "null", "webhook"])
    serve.add_argument("--webhook-url", default=None)
    serve.add_argument("--log-level", default="info")

    replay = subparsers.add_parser("replay", help="apply stored Alertmanager payloads in order")
    replay.add_argument("--dir", required=True, help="directory with *.json payloads")
    replay.add_argument("--db", default=DEFAULT_DB)
    replay.add_argument("--policy", default=DEFAULT_POLICY)
    replay.add_argument(
        "--timing",
        default="payload",
        choices=["payload", "now"],
        help="timestamp alerts as delivered (default) or as arriving now",
    )
    replay.add_argument("--json", action="store_true", help="print the summary as JSON")

    listing = subparsers.add_parser("list", help="list incidents")
    listing.add_argument("--db", default=DEFAULT_DB)
    listing.add_argument("--status", default=None)
    listing.add_argument("--severity", default=None)
    listing.add_argument("--service", default=None)

    postmortem = subparsers.add_parser("postmortem", help="print the postmortem markdown")
    postmortem.add_argument("--id", required=True, dest="incident_id")
    postmortem.add_argument("--db", default=DEFAULT_DB)
    postmortem.add_argument("--policy", default=DEFAULT_POLICY)

    metrics = subparsers.add_parser("metrics", help="print the Prometheus exposition")
    metrics.add_argument("--db", default=DEFAULT_DB)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "replay":
        return _replay(args)
    if args.command == "list":
        return _list(args)
    if args.command == "postmortem":
        return _postmortem(args)
    if args.command == "metrics":
        return _metrics(args)
    parser.error("unknown command %r" % args.command)  # pragma: no cover - argparse guards this
    return 2  # pragma: no cover


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    app = create_app(
        db_path=args.db,
        policy_path=args.policy,
        notifier=build_notifier(args.notifier, url=args.webhook_url),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _replay(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    if not directory.is_dir():
        print("replay: %s is not a directory" % directory, file=sys.stderr)
        return 2
    payload_files = sorted(directory.glob("*.json"))
    if not payload_files:
        print("replay: no *.json payloads in %s" % directory, file=sys.stderr)
        return 2

    store = Store(args.db)
    policy = load_policy(args.policy)
    service = IncidentService(store, policy)
    created = deduplicated = resolved = alerts = 0
    try:
        for path in payload_files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            received_at = _delivery_time(payload, args.timing)
            _, results = service.ingest_payload(payload, received_at=received_at)
            alerts += len(results)
            for result in results:
                if result.action == "create":
                    created += 1
                if result.deduplicated:
                    deduplicated += 1
                if result.alert_status == "resolved":
                    resolved += 1
        snapshot = store.metrics_snapshot()
        summary = {
            "payloads": len(payload_files),
            "alerts": alerts,
            "incidents_created": created,
            "alerts_deduplicated": deduplicated,
            "resolved_alerts": resolved,
            "open_incidents": snapshot.open_incidents,
            "resolved_incidents": snapshot.resolved_incidents,
            "mttr_seconds": snapshot.mean_mttr,
            "mttr": format_duration(snapshot.mean_mttr),
            "mttd_seconds": snapshot.mean_mttd,
            "mttd": format_duration(snapshot.mean_mttd),
            "db": str(args.db),
        }
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print("replay: %d payload(s) from %s -> %s" % (len(payload_files), directory, args.db))
            print("  alerts processed      : %d" % alerts)
            print("  incidents created     : %d" % created)
            print("  alerts deduplicated   : %d" % deduplicated)
            print("  incidents resolved    : %d" % snapshot.resolved_incidents)
            print("  incidents open        : %d" % snapshot.open_incidents)
            print("  MTTR (mean)           : %s" % format_duration(snapshot.mean_mttr))
            print("  MTTD (mean)           : %s" % format_duration(snapshot.mean_mttd))
    finally:
        store.close()
    return 0


def _delivery_time(payload: Dict[str, Any], timing: str) -> datetime:
    """When was this delivery "received" for timeline purposes."""
    if timing == "now":
        return utcnow()
    alerts = payload.get("alerts") or []
    candidates: List[datetime] = []
    for alert in alerts:
        if str(alert.get("status", "firing")) == "resolved":
            candidates.append(parse_ts(alert.get("endsAt")) or parse_ts(alert.get("startsAt")))
        else:
            candidates.append(parse_ts(alert.get("startsAt")))
    stamps = [stamp for stamp in candidates if stamp is not None]
    if not stamps:
        return utcnow()
    return max(stamps)


def _list(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        incidents = store.list_incidents(
            status=args.status, severity=args.severity, service=args.service
        )
    except ValueError as exc:
        print("list: %s" % exc, file=sys.stderr)
        store.close()
        return 2
    if not incidents:
        print("no incidents match the filter")
        store.close()
        return 0
    header = ("ID", "STATUS", "SEV", "SERVICE", "OWNER", "OPENED", "MTTD", "MTTR")
    rows: List[Tuple[str, ...]] = [
        (
            incident.id,
            incident.status.value,
            incident.severity.value,
            incident.service,
            incident.owner,
            to_iso(incident.opened_at) or "",
            format_duration(incident.mttd_seconds),
            format_duration(incident.mttr_seconds),
        )
        for incident in incidents
    ]
    widths = [max(len(str(row[index])) for row in [header, *rows]) for index in range(len(header))]
    template = "  ".join("{:<%d}" % width for width in widths)
    print(template.format(*header))
    print(template.format(*["-" * width for width in widths]))
    for row in rows:
        print(template.format(*row))
    print("\n%d incident(s)" % len(rows))
    store.close()
    return 0


def _postmortem(args: argparse.Namespace) -> int:
    store = Store(args.db)
    policy = load_policy(args.policy) if Path(args.policy).exists() else default_policy()
    service = IncidentService(store, policy)
    try:
        print(service.postmortem(args.incident_id))
    except KeyError:
        print("postmortem: no such incident %s" % args.incident_id, file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _metrics(args: argparse.Namespace) -> int:
    from .metrics import render_metrics

    store = Store(args.db)
    try:
        print(render_metrics(store.metrics_snapshot()), end="")
    finally:
        store.close()
    return 0

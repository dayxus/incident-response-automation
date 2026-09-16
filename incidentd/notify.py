"""Notification fan-out.

The notifier is injected everywhere it is used, so tests never touch the
network: they pass :class:`RecordingNotifier` (or :class:`NullNotifier`) and
assert on what *would* have been paged.

``WebhookNotifier`` is the only implementation that performs I/O, and it
swallows transport errors on purpose: a broken chat integration must never
break alert ingestion.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Callable, List, Optional, Tuple

from .models import Incident, format_duration, to_iso


class Notifier(ABC):
    """Minimal interface: one incident, one message, one kind."""

    name = "notifier"

    @abstractmethod
    def notify(self, incident: Incident, message: str, kind: str = "update") -> None:
        """Deliver ``message`` about ``incident`` (kinds: page/chat/update/resolved)."""


class NullNotifier(Notifier):
    """Drops everything. The default for tests and for offline replays."""

    name = "null"

    def notify(self, incident: Incident, message: str, kind: str = "update") -> None:
        return None


class StdoutNotifier(Notifier):
    """Prints notifications to stdout; used by the demo and by ``serve`` locally."""

    name = "stdout"

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self.sent: List[Tuple[str, str, str]] = []

    def notify(self, incident: Incident, message: str, kind: str = "update") -> None:
        self.sent.append((incident.id, kind, message))
        print(
            "[notify:%s] %s %s %s :: %s"
            % (kind, incident.severity.value, incident.id, incident.service, message),
            file=self.stream,
        )


class RecordingNotifier(Notifier):
    """Keeps notifications in memory so tests can assert on them."""

    name = "recording"

    def __init__(self) -> None:
        self.messages: List[Tuple[str, str, str]] = []

    def notify(self, incident: Incident, message: str, kind: str = "update") -> None:
        self.messages.append((incident.id, kind, message))

    def kinds(self) -> List[str]:
        return [kind for _, kind, _ in self.messages]

    def for_incident(self, incident_id: str) -> List[str]:
        return [message for iid, _, message in self.messages if iid == incident_id]


class WebhookNotifier(Notifier):
    """POSTs the incident as JSON to a chat/Slack-style endpoint."""

    name = "webhook"

    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        opener: Optional[Callable] = None,
        stream=None,
    ) -> None:
        if not url:
            raise ValueError("WebhookNotifier needs a URL")
        self.url = url
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self.stream = stream or sys.stderr

    def notify(self, incident: Incident, message: str, kind: str = "update") -> None:
        body = json.dumps(
            {
                "kind": kind,
                "message": message,
                "incident": {
                    "id": incident.id,
                    "service": incident.service,
                    "severity": incident.severity.value,
                    "status": incident.status.value,
                    "owner": incident.owner,
                    "route": incident.route,
                    "channel": incident.channel,
                    "runbook": incident.runbook,
                    "opened_at": to_iso(incident.opened_at),
                    "mttr": format_duration(incident.mttr_seconds),
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:  # pragma: no cover - transport
            print(
                "[notify:webhook] delivery of %s failed: %s" % (incident.id, exc),
                file=self.stream,
            )


def build_notifier(kind: str = "stdout", url: Optional[str] = None, stream=None) -> Notifier:
    """Factory used by the CLI/API wiring."""
    normalised = (kind or "stdout").strip().lower()
    if normalised == "null":
        return NullNotifier()
    if normalised == "stdout":
        return StdoutNotifier(stream=stream)
    if normalised == "webhook":
        return WebhookNotifier(url or "", stream=stream)
    raise ValueError("unknown notifier %r (expected null, stdout or webhook)" % kind)

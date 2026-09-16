"""Dedupe engine: one incident per fingerprint, with a suppression window.

Two rules, both driven by ``policy.dedupe``:

``attach``
    The fingerprint already belongs to an unresolved incident. Every further
    delivery is appended to the timeline and counted, no new incident is born.
    This is what makes ``POST /webhook/alertmanager`` idempotent.

``reopen``
    The previous incident for that fingerprint was resolved no longer than
    ``suppression_seconds`` ago and the alert fires again (flapping). The
    existing incident is reopened instead of flooding the on-call with a new
    page.

Past the suppression window a genuinely new incident is created, with a
pointer to the previous one in the timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Mapping, Optional

from .config import Policy
from .models import Incident, compute_fingerprint, ensure_utc

ACTION_CREATE = "create"
ACTION_ATTACH = "attach"
ACTION_REOPEN = "reopen"


@dataclass(frozen=True)
class DedupeDecision:
    action: str
    deduplicated: bool
    reason: str
    incident: Optional[Incident] = None

    @property
    def creates_incident(self) -> bool:
        return self.action == ACTION_CREATE


class DedupeEngine:
    """Decides what to do with an incoming alert for a given fingerprint."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    @property
    def window_seconds(self) -> int:
        return self.policy.dedupe.window_seconds

    @property
    def suppression_seconds(self) -> int:
        return self.policy.dedupe.suppression_seconds

    def fingerprint_for(self, labels: Mapping[str, str], provided: Optional[str] = None) -> str:
        value = (provided or "").strip()
        return value or compute_fingerprint(labels)

    def group_key(self, labels: Mapping[str, str]) -> str:
        return self.policy.dedupe.group_key(labels)

    def decide(
        self,
        fingerprint: str,
        now: datetime,
        existing: Optional[Incident] = None,
    ) -> DedupeDecision:
        now = ensure_utc(now)
        if existing is None:
            return DedupeDecision(
                action=ACTION_CREATE,
                deduplicated=False,
                reason="first alert for fingerprint %s" % fingerprint,
            )

        if not existing.status.is_terminal:
            reference = existing.last_alert_at or existing.opened_at
            age = (now - ensure_utc(reference)).total_seconds()
            if age <= self.window_seconds:
                reason = "duplicate delivery within the %ds dedupe window" % self.window_seconds
            else:
                reason = "repeat delivery after %ds with the incident still open; keeping %s" % (
                    int(age),
                    existing.id,
                )
            return DedupeDecision(
                action=ACTION_ATTACH,
                deduplicated=True,
                reason=reason,
                incident=existing,
            )

        resolved_at = existing.resolved_at or existing.last_alert_at or existing.opened_at
        since_resolved = (now - ensure_utc(resolved_at)).total_seconds()
        if since_resolved <= self.suppression_seconds:
            return DedupeDecision(
                action=ACTION_REOPEN,
                deduplicated=True,
                reason="refired %ds after resolution, inside the %ds suppression window"
                % (int(since_resolved), self.suppression_seconds),
                incident=existing,
            )
        return DedupeDecision(
            action=ACTION_CREATE,
            deduplicated=False,
            reason="previous incident %s resolved %ds ago, outside the %ds suppression window"
            % (existing.id, int(since_resolved), self.suppression_seconds),
            incident=existing,
        )

    def describe(self) -> Dict[str, object]:
        return {
            "window_seconds": self.window_seconds,
            "suppression_seconds": self.suppression_seconds,
            "group_by": list(self.policy.dedupe.group_by),
        }

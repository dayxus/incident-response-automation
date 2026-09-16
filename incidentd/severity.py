"""Severity and ownership resolution driven by the policy file.

Input: the alert's labels (``severity``, ``service``, ``slo``, ...).
Output: ``Sev1..Sev4`` plus owner, route, channel and the ack/resolve deadlines
the incident will be measured against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .config import Policy
from .models import Severity


@dataclass(frozen=True)
class Classification:
    """Everything policy says about one alert."""

    severity: Severity
    owner: str
    route: str
    channel: str
    escalation_minutes: int
    ack_deadline_minutes: int
    resolve_deadline_minutes: int
    matched_rule: str
    tier: Optional[int] = None
    slo: Optional[str] = None
    runbook: Optional[str] = None

    def describe(self) -> str:
        return "%s owner=%s route=%s (%s, escalate after %dm)" % (
            self.severity.value,
            self.owner,
            self.route,
            self.channel,
            self.escalation_minutes,
        )


def rule_context(labels: Mapping[str, str], policy: Policy) -> Dict[str, Any]:
    """Flatten labels + service metadata into the namespace rules match on."""
    service_name = str(labels.get("service", "") or labels.get("job", "") or "unknown")
    service = policy.service(service_name)
    return {
        "alert_severity": str(labels.get("severity", "")).lower(),
        "service": service_name,
        "service_tier": None if service is None else service.tier,
        "slo": labels.get("slo"),
        "labels": {str(key): str(value) for key, value in labels.items()},
    }


def classify(labels: Mapping[str, str], policy: Policy) -> Classification:
    """Apply the first matching policy rule, falling back to ``defaults``."""
    context = rule_context(labels, policy)
    service_name = context["service"]
    service = policy.service(service_name)

    rule = next((candidate for candidate in policy.rules if candidate.matches(context)), None)

    severity = rule.severity if rule is not None else policy.defaults.severity
    matched_rule = rule.name if rule is not None else "defaults"

    route_name = (rule.route if rule and rule.route else policy.defaults.route) or ""
    route = policy.route(route_name)

    ack_deadline = _first_set(
        rule.ack_deadline_minutes if rule else None,
        service.ack_deadline_minutes if service else None,
        policy.defaults.ack_deadline_minutes,
    )
    resolve_deadline = _first_set(
        rule.resolve_deadline_minutes if rule else None,
        service.resolve_deadline_minutes if service else None,
        policy.defaults.resolve_deadline_minutes,
    )
    owner = _first_set(
        rule.owner if rule else None,
        service.owner if service else None,
        policy.defaults.owner,
    )

    return Classification(
        severity=severity,
        owner=str(owner),
        route=route_name or "unrouted",
        channel=route.channel if route else "unrouted",
        escalation_minutes=route.escalation_minutes if route else 0,
        ack_deadline_minutes=int(ack_deadline),
        resolve_deadline_minutes=int(resolve_deadline),
        matched_rule=matched_rule,
        tier=service.tier if service else None,
        slo=service.slo if service else labels.get("slo"),
        runbook=service.runbook if service else None,
    )


def _first_set(*candidates: Optional[Any]) -> Optional[Any]:
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def describe_route(incident: Any) -> str:
    """One-line route description used in notifications, e.g. ``Sev1 for checkout-api``."""
    return "%s for %s routed to %s" % (
        incident.severity.value,
        incident.service,
        incident.route,
    )

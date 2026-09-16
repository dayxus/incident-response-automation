"""Policy loading: severity mapping, routing, escalation and dedupe windows.

The policy is *data*: nothing in this package hardcodes ``critical + tier 1 =
Sev1``. Change ``policy/policy.yaml`` and the severity assignment changes with
it. Unknown policy keys fail loudly at load time instead of being ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

from .models import Severity


class ConfigError(ValueError):
    """Raised when the policy file is missing required structure."""


# Rule keys accepted inside ``when:`` blocks.
RULE_MATCH_KEYS = frozenset({"alert_severity", "service_tier", "service", "slo", "labels"})


@dataclass(frozen=True)
class Defaults:
    severity: Severity = Severity.SEV3
    owner: str = "lab-oncall"
    route: str = "chat-sre"
    ack_deadline_minutes: int = 60
    resolve_deadline_minutes: int = 480


@dataclass(frozen=True)
class DedupePolicy:
    window_seconds: int = 600
    suppression_seconds: int = 900
    group_by: Tuple[str, ...] = ("service", "alertname", "cluster")

    def group_key(self, labels: Mapping[str, str]) -> str:
        return ",".join("%s=%s" % (key, labels.get(key, "")) for key in self.group_by)


@dataclass(frozen=True)
class ServicePolicy:
    name: str
    tier: int
    owner: str
    slo: Optional[str] = None
    runbook: Optional[str] = None
    ack_deadline_minutes: Optional[int] = None
    resolve_deadline_minutes: Optional[int] = None


@dataclass(frozen=True)
class RoutePolicy:
    name: str
    channel: str
    escalation_minutes: int = 30


@dataclass(frozen=True)
class SeverityRule:
    name: str
    when: Mapping[str, Any]
    severity: Severity
    owner: Optional[str] = None
    route: Optional[str] = None
    ack_deadline_minutes: Optional[int] = None
    resolve_deadline_minutes: Optional[int] = None

    def matches(self, context: Mapping[str, Any]) -> bool:
        for key, expected in self.when.items():
            actual = context.get(key)
            if isinstance(expected, (list, tuple)):
                if actual not in expected:
                    return False
            elif key == "labels":
                wanted = {str(k): str(v) for k, v in dict(expected).items()}
                for label, value in wanted.items():
                    if str(context["labels"].get(label, "")) != value:
                        return False
            elif actual != expected:
                return False
        return True


@dataclass(frozen=True)
class Policy:
    version: int = 1
    defaults: Defaults = field(default_factory=Defaults)
    dedupe: DedupePolicy = field(default_factory=DedupePolicy)
    services: Mapping[str, ServicePolicy] = field(default_factory=dict)
    rules: Tuple[SeverityRule, ...] = ()
    routes: Mapping[str, RoutePolicy] = field(default_factory=dict)
    source_path: Optional[Path] = None

    def service(self, name: str) -> Optional[ServicePolicy]:
        return self.services.get(name)

    def route(self, name: str) -> Optional[RoutePolicy]:
        return self.routes.get(name)

    def service_names(self) -> List[str]:
        return sorted(self.services)

    def rule_names(self) -> List[str]:
        return [rule.name for rule in self.rules]


def default_policy() -> Policy:
    """Policy used when no file is supplied: everything lands on Sev3."""
    return Policy()


def _require_mapping(value: Any, what: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("%s must be a mapping, got %s" % (what, type(value).__name__))
    return value


def load_policy(path) -> Policy:
    """Load and validate ``policy.yaml``."""
    policy_path = Path(path)
    if not policy_path.exists():
        raise ConfigError("policy file not found: %s" % policy_path)
    raw = _require_mapping(yaml.safe_load(policy_path.read_text(encoding="utf-8")), "policy")

    defaults_raw = _require_mapping(raw.get("defaults"), "defaults")
    defaults = Defaults(
        severity=Severity.coerce(defaults_raw.get("severity", Defaults.severity.value)),
        owner=str(defaults_raw.get("owner", Defaults.owner)),
        route=str(defaults_raw.get("route", Defaults.route)),
        ack_deadline_minutes=int(
            defaults_raw.get("ack_deadline_minutes", Defaults.ack_deadline_minutes)
        ),
        resolve_deadline_minutes=int(
            defaults_raw.get("resolve_deadline_minutes", Defaults.resolve_deadline_minutes)
        ),
    )

    dedupe_raw = _require_mapping(raw.get("dedupe"), "dedupe")
    group_by = tuple(str(item) for item in dedupe_raw.get("group_by", DedupePolicy.group_by))
    dedupe = DedupePolicy(
        window_seconds=int(dedupe_raw.get("window_seconds", DedupePolicy.window_seconds)),
        suppression_seconds=int(
            dedupe_raw.get("suppression_seconds", DedupePolicy.suppression_seconds)
        ),
        group_by=group_by,
    )

    services: Dict[str, ServicePolicy] = {}
    services_raw = _require_mapping(raw.get("services"), "services")
    for name, body in services_raw.items():
        body = _require_mapping(body, "services.%s" % name)
        if "tier" not in body:
            raise ConfigError("services.%s is missing the required key 'tier'" % name)
        services[str(name)] = ServicePolicy(
            name=str(name),
            tier=int(body["tier"]),
            owner=str(body.get("owner", defaults.owner)),
            slo=body.get("slo"),
            runbook=body.get("runbook"),
            ack_deadline_minutes=body.get("ack_deadline_minutes"),
            resolve_deadline_minutes=body.get("resolve_deadline_minutes"),
        )

    routes: Dict[str, RoutePolicy] = {}
    routes_raw = _require_mapping(raw.get("routes"), "routes")
    for name, body in routes_raw.items():
        body = _require_mapping(body, "routes.%s" % name)
        if "channel" not in body:
            raise ConfigError("routes.%s is missing the required key 'channel'" % name)
        routes[str(name)] = RoutePolicy(
            name=str(name),
            channel=str(body["channel"]),
            escalation_minutes=int(body.get("escalation_minutes", RoutePolicy.escalation_minutes)),
        )

    rules: List[SeverityRule] = []
    rules_raw = raw.get("severity_rules") or []
    if not isinstance(rules_raw, list):
        raise ConfigError("severity_rules must be a list of rules")
    for index, body in enumerate(rules_raw):
        body = _require_mapping(body, "severity_rules[%d]" % index)
        name = str(body.get("name", "rule-%d" % (index + 1)))
        if "severity" not in body:
            raise ConfigError("severity_rules[%s] is missing the required key 'severity'" % name)
        when = _require_mapping(body.get("when"), "severity_rules[%s].when" % name)
        unknown = set(when) - RULE_MATCH_KEYS
        if unknown:
            raise ConfigError(
                "severity_rules[%s].when has unsupported keys: %s (allowed: %s)"
                % (name, ", ".join(sorted(unknown)), ", ".join(sorted(RULE_MATCH_KEYS)))
            )
        route_name = body.get("route")
        if route_name is not None and str(route_name) not in routes:
            raise ConfigError(
                "severity_rules[%s] routes to unknown route %r" % (name, str(route_name))
            )
        rules.append(
            SeverityRule(
                name=name,
                when=dict(when),
                severity=Severity.coerce(body["severity"]),
                owner=body.get("owner"),
                route=None if route_name is None else str(route_name),
                ack_deadline_minutes=body.get("ack_deadline_minutes"),
                resolve_deadline_minutes=body.get("resolve_deadline_minutes"),
            )
        )

    if not rules:
        raise ConfigError("severity_rules must contain at least a fallback rule")
    if not rules[-1].when:
        pass  # explicit fallback rule, nothing else to check

    return Policy(
        version=int(raw.get("version", 1)),
        defaults=defaults,
        dedupe=dedupe,
        services=services,
        rules=tuple(rules),
        routes=routes,
        source_path=policy_path,
    )

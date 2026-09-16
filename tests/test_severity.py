"""Severity mapping is policy data: change the YAML, change the classification."""

from __future__ import annotations

from dataclasses import replace

import pytest

from incidentd.config import ConfigError, load_policy
from incidentd.models import Severity
from incidentd.severity import classify, rule_context

from .conftest import POLICY_PATH, make_payload


def labels(**overrides):
    base = dict(make_payload()["alerts"][0]["labels"])
    base.update(overrides)
    return base


def test_critical_tier1_is_sev1_with_primary_owner(policy):
    result = classify(labels(severity="critical", service="checkout-api"), policy)
    assert result.severity is Severity.SEV1
    assert result.owner == "primary-oncall"
    assert result.route == "page-primary"
    assert result.channel == "pagerduty-primary"
    assert result.escalation_minutes == 5
    assert result.ack_deadline_minutes == 5
    assert result.matched_rule == "critical-tier1"
    assert result.tier == 1
    assert result.slo == "checkout-availability-99.95"
    assert result.runbook.endswith("#checkout-api")


def test_critical_tier2_is_sev2(policy):
    result = classify(labels(severity="critical", service="search-api"), policy)
    assert result.severity is Severity.SEV2
    assert result.owner == "secondary-oncall"
    assert result.route == "page-secondary"
    assert result.ack_deadline_minutes == 15
    assert result.matched_rule == "critical-tier2"


def test_warning_is_sev3_and_info_is_sev4(policy):
    warning = classify(labels(severity="warning", service="search-api"), policy)
    assert warning.severity is Severity.SEV3
    assert warning.route == "chat-sre"
    assert warning.channel == "slack-sre-lab"
    assert warning.owner == "search-oncall", "rule has no owner, service owner wins"
    assert warning.matched_rule == "warning"

    info = classify(labels(severity="info", service="recommendations"), policy)
    assert info.severity is Severity.SEV4
    assert info.owner == "growth-oncall"
    assert info.ack_deadline_minutes == 240
    assert info.matched_rule == "info"


def test_critical_on_unlisted_service_is_sev2(policy):
    result = classify(labels(severity="critical", service="legacy-batch"), policy)
    assert result.severity is Severity.SEV2
    assert result.tier is None
    assert result.matched_rule == "critical-unlisted-service"
    assert result.owner == "secondary-oncall"


def test_missing_severity_label_falls_back_to_defaults(policy):
    result = classify({"alertname": "SomethingBroke"}, policy)
    assert result.severity is Severity.SEV3
    assert result.matched_rule == "fallback"
    assert result.owner == "lab-oncall"
    assert result.route == "chat-sre"
    assert result.tier is None


def test_severity_bands_are_ordered(policy):
    severities = [
        classify(labels(severity="critical", service="checkout-api"), policy).severity,
        classify(labels(severity="critical", service="search-api"), policy).severity,
        classify(labels(severity="warning", service="search-api"), policy).severity,
        classify(labels(severity="info", service="recommendations"), policy).severity,
    ]
    assert [severity.value for severity in severities] == ["Sev1", "Sev2", "Sev3", "Sev4"]
    assert [severity.rank for severity in severities] == [1, 2, 3, 4]
    assert [severity.pages_humans for severity in severities] == [True, True, False, False]


def test_mapping_is_policy_not_code(policy):
    """Same labels, patched policy rules -> different severity, same code path."""
    custom_rule = replace(policy.rules[3], severity=Severity.SEV1, name="warning-escalated")
    patched = replace(policy, rules=(custom_rule,) + policy.rules[4:])
    result = classify(labels(severity="warning", service="search-api"), patched)
    assert result.severity is Severity.SEV1
    assert result.matched_rule == "warning-escalated"


def test_rule_context_exposes_tier_and_slo(policy):
    context = rule_context(labels(service="payments-worker"), policy)
    assert context["service"] == "payments-worker"
    assert context["service_tier"] == 1
    assert context["alert_severity"] == "critical"
    assert context["labels"]["cluster"] == "lab-eu-west"


def test_committed_policy_loads_with_expected_shape(policy):
    assert policy.version == 1
    assert policy.service_names() == [
        "checkout-api",
        "payments-worker",
        "recommendations",
        "reporting-batch",
        "search-api",
    ]
    assert policy.rule_names() == [
        "critical-tier1",
        "critical-tier2",
        "critical-unlisted-service",
        "warning",
        "info",
        "fallback",
    ]
    assert policy.source_path == POLICY_PATH
    assert policy.route("page-primary").channel == "pagerduty-primary"


def test_rule_with_unknown_match_key_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "version: 1\n"
        "severity_rules:\n"
        "  - name: broken\n"
        "    when:\n"
        "      team_priority: high\n"
        "    severity: Sev1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as error:
        load_policy(path)
    assert "unsupported keys" in str(error.value)
    assert "team_priority" in str(error.value)


def test_rule_routing_to_unknown_route_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "version: 1\n"
        "severity_rules:\n"
        "  - name: broken\n"
        "    when: {}\n"
        "    severity: Sev1\n"
        "    route: pagerduty-that-does-not-exist\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as error:
        load_policy(path)
    assert "unknown route" in str(error.value)


def test_service_without_tier_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "version: 1\n"
        "services:\n"
        "  checkout-api:\n"
        "    owner: who-knows\n"
        "severity_rules:\n"
        "  - name: fallback\n"
        "    when: {}\n"
        "    severity: Sev3\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as error:
        load_policy(path)
    assert "missing the required key 'tier'" in str(error.value)


def test_policy_without_rules_is_rejected(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\nseverity_rules: []\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(path)


def test_missing_policy_file_is_reported(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_policy(tmp_path / "nope.yaml")
    assert "policy file not found" in str(error.value)

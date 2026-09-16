# Policy reference

Everything the classifier knows lives in `policy/policy.yaml`. There is no `if severity ==
"critical"` anywhere in the code: change the file and the classification changes with it. Keys are
validated at load time — a typo fails loudly instead of being ignored.

## Top-level keys

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | int | yes | Policy schema version, currently `1`. |
| `defaults` | map | no | Fallback values for anything the rules and services do not set. |
| `dedupe` | map | no | Dedupe and suppression windows, plus the `group_by` labels. |
| `services` | map | no | Per-service metadata: tier, owner, SLO, runbook, deadlines. |
| `severity_rules` | list | yes | Evaluated top-down, **first match wins**; the last rule usually has an empty `when`. |
| `routes` | map | no | Where a route notifies and how long it waits before escalating. |

## `defaults`

```yaml
defaults:
  severity: Sev3              # used when no rule matched
  owner: lab-oncall
  route: chat-sre
  ack_deadline_minutes: 60
  resolve_deadline_minutes: 480
```

## `dedupe`

```yaml
dedupe:
  window_seconds: 600         # repeat deliveries inside this window attach to the open incident
  suppression_seconds: 900    # a resolved fingerprint firing again this soon reopens the incident
  group_by:                   # labels that build the group key reported to the notifier
    - service
    - alertname
    - cluster
```

`window_seconds` is what makes `POST /webhook/alertmanager` idempotent. `suppression_seconds` is the
flap guard: without it, an alert that resolves and fires again pages twice. Both are used by
`incidentd/dedupe.py` and covered by `tests/test_dedupe.py`.

## `services.<name>`

```yaml
services:
  checkout-api:
    tier: 1                              # 1, 2, 3 ... matched by service_tier rules
    owner: payments-oncall
    slo: checkout-availability-99.95
    runbook: examples/playbook/README.md#checkout-api
    ack_deadline_minutes: 5              # overrides defaults, overridden by the matched rule
    resolve_deadline_minutes: 120
```

The service name is taken from the alert label `service`, falling back to `job`. An alert for a
service that is not listed still works: it is classified by the rules without a tier.

## `severity_rules[]`

```yaml
severity_rules:
  - name: critical-tier1                 # shows up in the timeline and in the postmortem
    when:
      alert_severity: critical           # from the label `severity`, lower-cased
      service_tier: 1                    # from services.<name>.tier
    severity: Sev1
    owner: primary-oncall
    route: page-primary
    ack_deadline_minutes: 5
    resolve_deadline_minutes: 120
```

`when` accepts these keys, all optional, all ANDed:

| Key | Matches |
| --- | --- |
| `alert_severity` | the alert label `severity`, lower-cased (`critical`, `warning`, `info`) |
| `service_tier` | `tier` of the alert's service, or `null` when the service is unknown |
| `service` | the resolved service name |
| `slo` | the alert label `slo` verbatim |
| `labels` | a map of label name → exact value; every pair must match |

A value can also be a list, in which case any element matches:

```yaml
  - name: noisy-warning
    when:
      alert_severity: [warning, info]
      service: [recommendations, reporting-batch]
    severity: Sev4
    route: chat-sre
```

A rule that names a `route` which is not declared under `routes` fails at load time
(`severity_rules[noisy-warning] routes to unknown route 'chat-sre'`), so a typo cannot silently
produce an incident with nowhere to notify.

The final rule with an empty `when: {}` acts as the fallback. Without it, an alert that matches
nothing gets `defaults.severity` and `matched_rule: defaults`.

## `routes.<name>`

```yaml
routes:
  page-primary:
    channel: pagerduty-primary          # recorded on the incident and used by the notifier
    escalation_minutes: 5               # appears in the opening timeline entry
  chat-sre:
    channel: slack-sre-lab
    escalation_minutes: 60
```

## Precedence

For severity, owner, route and deadlines the resolution order is:

1. the matched `severity_rules[]` entry,
2. `services.<name>`,
3. `defaults`.

Deadlines are resolved field by field, so a rule can set `ack_deadline_minutes` and inherit
`resolve_deadline_minutes` from the service. Whatever the outcome, the incident records the rule
that matched (`_policy_rule` in the incident labels) so a postmortem can always explain why an alert
was Sev1.

## Validating a policy change

```bash
.venv/bin/python - <<'PY'
from incidentd.config import load_policy
from incidentd.severity import classify
policy = load_policy("policy/policy.yaml")
print("shipped policy   :", classify({"severity": "critical", "service": "checkout-api"}, policy).describe())
print("no such service  :", classify({"severity": "warning", "service": "nothing-here"}, policy).describe())
PY
```

`tests/test_severity.py` exercises the same path, including a policy where tier 1 no longer means
Sev1 — that is the point of keeping the mapping in data.

Against the shipped policy:

```
shipped policy   : Sev1 owner=primary-oncall route=page-primary (pagerduty-primary, escalate after 5m)
no such service  : Sev3 owner=lab-oncall route=chat-sre (slack-sre-lab, escalate after 60m)
```


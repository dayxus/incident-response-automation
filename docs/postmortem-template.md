# Postmortem template

`incidentd/postmortem.py` renders one markdown document per incident from the stored incident plus
its timeline. Nothing in the document is invented: every table row comes from a recorded field.

## What the generator fills

| Section | Source |
| --- | --- |
| Title `# Postmortem — <service> (<severity>)` | `incident.service`, `incident.severity` |
| Metadata table | incident id, severity, service, owner, route + channel + escalation, status, SLO, `opened_at`, `acknowledged_at` and MTTD, `resolved_at` and MTTR, ack-deadline state, fingerprint, policy path |
| Summary | the alert annotation `summary` |
| Impact (machine part) | severity and the policy rule that matched, blast radius (service + SLO), number of deliveries for the fingerprint, time in degraded state |
| Detection (machine part) | the opening delivery timestamp and the acknowledgement, if one was recorded |
| Timeline | every append-only timeline entry, in chronological order |
| Contributing factors | the alert annotation `contributing_factor` |
| Footer | incident id, fingerprint and the reminder that the human sections are marked |

## What stays for humans

Each of these is emitted as a heading plus an explicit marker, so a draft can never be mistaken for
a finished postmortem:

```
## Impact
...
<!-- preencher: usuários/serviços afetados, volume e janela de impacto -->

## Detection
...
<!-- preencher: como o problema foi detectado (alerta, cliente, deploy) -->

## Root cause

<!-- preencher: causa raiz — o que mudou no sistema/comportamento humano -->

## What went well

<!-- preencher: o que funcionou bem na resposta -->

## Action items

<!-- preencher: uma linha por ação, com tipo (prevent/detect/mitigate), owner e prazo. Sem ação mecânica como "ter mais cuidado" -->

| Action | Type | Owner | Due | Issue |
| --- | --- | --- | --- | --- |
```

The action-item table is deliberately empty. A tool that proposes follow-ups trains people to
approve them; the point of a blameless review is that the team decides what changes.

## Rendering one

```bash
.venv/bin/python -m incidentd postmortem --id INC-2026-0001 --db var/incidents.db
curl -s http://127.0.0.1:8080/incidents/INC-2026-0001/postmortem.md
```

Both paths call the same renderer (`GET /incidents/{id}/postmortem.md` answers with
`text/markdown`). `tests/test_postmortem.py` asserts the timeline is real, chronological, and that
the human markers are present.

## Adding annotations that the template understands

The postmortem reads the alert's annotations, so an alert rule can pre-fill the parts a machine can
know:

```yaml
- alert: LatencySLOBurn
  annotations:
    summary: checkout-api is burning the availability SLO budget
    description: 5m error-budget burn rate 14.2 on checkout-availability-99.95
    contributing_factor: slow-path recommendation call enabled for all traffic
    runbook_url: examples/playbook/README.md#checkout-api
```

`summary`, `description` and `contributing_factor` land in the document; anything else stays
available on the incident record for the people writing the review.

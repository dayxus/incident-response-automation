# Incident 2026-03-04 — checkout-api error budget burn

Synthetic incident used by `scripts/demo.sh`, `python3 -m incidentd replay` and
the test suite. Every timestamp, host and annotation here is fabricated for this
lab; nothing in this repository comes from a production environment.

## Scenario

`checkout-api` (tier 1, SLO `checkout-availability-99.95`) starts burning its
error budget at 13:02 UTC. Alertmanager fires `LatencySLOBurn` with
`severity=critical` and delivers the webhook twice (retry), which `incidentd`
deduplicates by fingerprint. A `search-api` warning (tier 2) lands a few minutes
later, and a second tier-1 service (`payments-worker`) starts failing while the
first incident is still open.

| Time (UTC) | Payload | What happens |
| --- | --- | --- |
| 13:02:10 | `01-firing-checkout-api.json` | `INC-…-0001` opens as Sev1, route `page-primary`, ack deadline 5m |
| 13:02:10 | `02-firing-checkout-api-repeat.json` | Same fingerprint, second delivery → deduplicated, attached to `0001` |
| 13:06:10 | — | Operator acknowledges `0001` (within the 5m deadline) |
| 13:07:00 | `03-firing-search-api.json` | `INC-…-0002` opens as Sev3 (tier 2 warning) |
| 13:09:30 | `05-firing-payments-worker.json` | `INC-…-0003` opens as Sev1 |
| 13:49:10 | `04-resolved-checkout-api.json` | Alertmanager reports `resolved` → `0001` closes, MTTR 47m (2820s) |

## What the responders did

1. `page-primary` paged the primary on-call at 13:02 and escalated to the
   payments team at 13:07 had nobody acknowledged by then.
2. The on-call acknowledged at 13:06:10 from the runbook link in the alert
   annotation, before the 5-minute deadline.
3. They confirmed the burn rate against the SLI dashboard, disabled the
   slow-path feature flag and watched the SLO recover.
4. Alertmanager reported the alert resolved at 13:49:10 and `incidentd` closed
   the incident, attached the resolved notification and generated the
   postmortem draft with a 47-minute MTTR (2820s).

## Per-service runbooks

### checkout-api

Check the SLO burn rate first, then the latency breakdown per dependency. The
2026-03-04 incident was traced to the slow-path recommendation call being
enabled for all traffic instead of 5%.

### payments-worker

Settlement jobs lag behind the queue. Look at queue depth, consumer errors and
the last successful settlement batch before restarting anything.

### search-api

Latency p99 above the SLO while error rate stays flat: usually the index
replication lag after a reindex, not the query path.

## Why this file exists

It documents the *intended* story behind the payloads, so a reader can tell
which timeline entry came from which Alertmanager delivery — and so the demo
output can be checked against something other than itself.

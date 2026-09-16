# Incident lifecycle

This document describes the states an incident moves through, who is allowed to move it and what
`incidentd` records along the way. The implementation lives in `incidentd/models.py`
(`IncidentStatus`, `validate_transition`), `incidentd/service.py` (the transitions) and
`incidentd/dedupe.py` (the decisions that come from an incoming delivery).

## States

| State | Meaning | Entered by |
| --- | --- | --- |
| `open` | An alert is firing and nobody owns the response yet. | A `firing` delivery with a fingerprint that is not attached to an open incident, or a flap inside the suppression window (reopen). |
| `acknowledged` | A human accepted the page. The clock for MTTD stops here. | `POST /incidents/{id}/ack`, or `python3 -m incidentd` operators using the service layer. |
| `resolved` | The alert stopped firing (`endsAt` in the payload) or an operator closed it. MTTR stops here. | A `resolved` delivery from Alertmanager, or `POST /incidents/{id}/resolve`. |

`open` and `acknowledged` are the live states; `resolved` is terminal. There is no `cancelled`
state: an incident that turns out to be noise still gets resolved, because the timeline entry is
the record that someone looked.

## Allowed transitions

```
       firing delivery                     /ack
             |                               |
             v                               v
          [open] ----------------------> [acknowledged]
             |                                  |
             |            /resolve              |  /resolve
             +----------------------------------+
             |                                  |
             v                                  v
          [resolved]  <-------------------- resolved delivery
             |
             |  firing delivery inside suppression_seconds
             v
          [open]   (reopen: same incident, reopened_count + 1)
```

Every other transition answers `409 Conflict` with a reason:

- acknowledging a resolved incident: `cannot acknowledge an incident that is already resolved`;
- resolving an incident that is already resolved: the second `resolved` delivery is recorded as a
  duplicate notification instead of moving the resolution timestamp;
- `ack`/`resolve` carrying an `at` timestamp earlier than `opened_at`: `400`.

## What happens on a delivery

1. The payload is validated against the Alertmanager webhook shape
   (`schemas/alertmanager-webhook.schema.json`). A payload with no alerts is rejected with `400`.
2. Each alert becomes an `AlertEvent`. Missing `fingerprint` is computed the way Alertmanager does:
   SHA-256 over the sorted `name=value` label pairs, truncated to 8 bytes.
3. `dedupe.decide` looks at the fingerprint's open incident, or its most recent one:
   - no incident yet → create;
   - open incident inside `dedupe.window_seconds` → attach (deduplicated);
   - open incident after the window → still attach, an open incident keeps absorbing its alerts;
   - resolved incident inside `suppression_seconds` → reopen the same incident (flap protection);
   - resolved incident after suppression → create a new incident, noting the previous id.
4. A `resolved` alert closes the incident using the payload's `endsAt`, not the wall clock. A
   resolved delivery for a fingerprint the service has never seen is reconstructed, so the gap in
   the timeline is explicit.
5. The timeline gets one entry per state change and one per delivery, plus one entry per
   notification. Entries are append-only: nothing updates or deletes a row.

## Who does what

| Actor | Responsibility |
| --- | --- |
| Alertmanager | Delivers groups of alerts; a retry is the normal case, not an error. |
| `incidentd` | Deduplicates, classifies, records the timeline, computes MTTD/MTTR, drafts the postmortem. |
| Primary/secondary on-call | Acknowledges inside the policy deadline, resolves, writes the postmortem's human sections. |
| Incident commander (Sev1/Sev2) | Owns communication and the decision to escalate; not modelled by the tool. |

## Clocks

- `opened_at` — the alert's `startsAt` (falling back to delivery time).
- `acknowledged_at` — the moment the ack was recorded, or the `at` an operator backfilled.
- `resolved_at` — the alert's `endsAt` for a resolved delivery.
- `received_at` — when the HTTP request arrived. It is the delivery's own clock and is what the
  `alert_received` timeline entries carry; it is deliberately not used for MTTD/MTTR.

Because MTTD and MTTR only use the first three, a replay of stored payloads produces the same
numbers a year later.

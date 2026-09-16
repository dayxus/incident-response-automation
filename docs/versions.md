# Versions and upstream tracking

## Runtime

`incidentd` targets Python ≥ 3.9 (`from __future__ import annotations`, no `match`, no
`zip(strict=)`). The CI matrix is 3.11 / 3.12 / 3.13; the suite was also run locally on 3.9.6.

| Component | Version | Where it comes from |
| --- | --- | --- |
| Python (CI) | 3.11, 3.12, 3.13 | `.github/workflows/ci.yml` matrix |
| Python (local run) | 3.9.6 | captured in the README's pytest block |
| fastapi | 0.128.8 | `requirements.txt` (`fastapi>=0.110`) |
| starlette | 0.49.3 | transitive, pulled by fastapi |
| pydantic | 2.13.5 | `requirements.txt` (`pydantic>=2.5`) |
| uvicorn | 0.39.0 | `requirements.txt` (`uvicorn>=0.27`) |
| PyYAML | 6.0.3 | `requirements.txt` (`PyYAML>=6.0`) |
| pytest / pytest-cov | 8.4.2 / 7.1.0 | `requirements-dev.txt` |
| ruff | 0.16.7 | `requirements-dev.txt` |
| httpx | 0.28.1 | test client transport |
| jsonschema | 4.25.1 | payload validation in the weekly workflow |
| pip-audit | latest on PyPI | dependency audit in the weekly workflow |

The versions above are the ones resolved on the machine that built the repository, not a promise:
`requirements*.txt` keeps lower bounds so the weekly run sees new releases and reports them.

## Alertmanager webhook payload

| Item | Value |
| --- | --- |
| Payload version accepted | `4` |
| Schema | `schemas/alertmanager-webhook.schema.json` |
| Upstream source checked | `notify/webhook/webhook.go` (`Message`) + embedded `template.Data` |
| Release checked at build time | `v0.34.0`, via `https://api.github.com/repos/prometheus/alertmanager/releases/latest` |
| Checked on | 2026-09-16 |
| Drift found | `notification_reason` and `routeLabels` (upstream `template.Data` fields) — added to the schema |

`scripts/sync_schema.py` repeats that check weekly and rewrites the schema when upstream adds,
removes or retypes a field, so the committed schema is never more than a week behind the release it
claims to describe.

## Known dependency findings

`pip-audit -r requirements.txt` reports six advisories for the versions resolvable today:
`starlette 0.49.3` (PYSEC-2026-161, -2280, -2281, -248, -249) and `click 8.1.8` (PYSEC-2026-2132).
The fixes live in starlette 1.x, which the current fastapi release does not allow yet, and in click
8.3.3. Both are transitive, so the weekly workflow opens an issue with the report instead of failing
the run; the issue stays open until an upstream release makes the bump possible.

## Keeping this file current

`docs/versions.md` is edited by hand when the tracked versions change; `reports/weekly-audit.md`
holds the machine-written numbers from the scheduled run (ingest percentiles, idempotency counts,
the Alertmanager release probed on that run).

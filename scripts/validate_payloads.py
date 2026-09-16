#!/usr/bin/env python3
"""Validate the committed Alertmanager payloads against the committed schema.

Two independent checks, because a payload can match the schema and still be a
bad fixture:

1. JSON Schema validation (``schemas/alertmanager-webhook.schema.json``).
2. Identity check: the declared ``fingerprint`` must equal the SHA-256 of the
   sorted label set truncated to 8 bytes, which is what Alertmanager computes.
   A fixture carrying a hand-written fingerprint would silently never dedupe.

Exit code 0 when every payload is valid, 1 otherwise (the weekly workflow turns
that into an issue).
"""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_SCHEMA = "schemas/alertmanager-webhook.schema.json"
DEFAULT_DIR = "examples/alertmanager"


def alertmanager_fingerprint(labels: Dict[str, str]) -> str:
    """Reproduce Alertmanager's fingerprint: sha256 of the sorted label pairs."""
    payload = ",".join("%s=%s" % (key, labels[key]) for key in sorted(labels))
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate(schema_path: Path, directory: Path) -> int:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - dev dependency missing
        print("validate_payloads: jsonschema is required (pip install -r requirements-dev.txt)")
        return 1

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    payload_files = sorted(directory.glob("*.json"))
    if not payload_files:
        print("validate_payloads: no *.json payloads in %s" % directory)
        return 1

    failures: List[str] = []
    print("payload                                   alerts  status    fingerprint")
    print("-" * 78)
    for path in payload_files:
        try:
            payload: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append("%s: not valid JSON (%s)" % (path.name, exc))
            print("%-41s  INVALID JSON" % path.name)
            continue

        problems = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
        for problem in problems:
            location = "/".join(str(part) for part in problem.path) or "<root>"
            failures.append("%s: %s: %s" % (path.name, location, problem.message))

        for index, alert in enumerate(payload.get("alerts", [])):
            declared = alert.get("fingerprint")
            computed = alertmanager_fingerprint(alert.get("labels", {}))
            if declared is not None and declared != computed:
                failures.append(
                    "%s: alerts[%d]: fingerprint %s does not match the label hash %s"
                    % (path.name, index, declared, computed)
                )

        first = (payload.get("alerts") or [{}])[0]
        print(
            "%-41s %6d  %-8s  %s"
            % (
                path.name,
                len(payload.get("alerts", [])),
                payload.get("status", "?"),
                first.get("fingerprint", alertmanager_fingerprint(first.get("labels", {}))),
            )
        )

    print("-" * 78)
    if failures:
        print("\n%d problem(s):" % len(failures))
        for failure in failures:
            print("  - %s" % failure)
        return 1
    print(
        "%d payload(s) match %s and every fingerprint matches its label hash"
        % (len(payload_files), schema_path)
    )
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=Path)
    parser.add_argument("--dir", default=DEFAULT_DIR, type=Path)
    args = parser.parse_args(argv)
    return validate(args.schema, args.dir)


if __name__ == "__main__":
    sys.exit(main())

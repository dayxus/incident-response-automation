#!/usr/bin/env python3
"""Re-check the webhook schema against the current Alertmanager release.

Alertmanager has no machine-readable description of its receiver payload, so
the source of truth is the Go struct the payload is serialised from:
``notify/webhook/webhook.go`` (``Message``) plus the struct it embeds
(``template.Data`` in ``template/template.go``). This script resolves the latest
release tag through the GitHub API, downloads those files at the tag, extracts
the JSON field names and compares them with
``schemas/alertmanager-webhook.schema.json``.

* No drift -> exit 0, ``changed=false``.
* Drift -> the local schema is rewritten with the upstream field set (existing
  descriptions are kept) and the script exits 0 with ``changed=true``, so the
  workflow can commit it as ``chore(schema): sync alertmanager webhook schema``.
* Network failure -> exit 1, so the workflow opens an issue instead of silently
  skipping the check.

The workflow passes ``--github-output`` so later steps can branch on ``changed``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RELEASES_API = "https://api.github.com/repos/prometheus/alertmanager/releases/latest"
SOURCE_URL = "https://raw.githubusercontent.com/prometheus/alertmanager/{tag}/{path}"
DEFAULT_SCHEMA = "schemas/alertmanager-webhook.schema.json"

MESSAGE_SOURCE = ("notify/webhook/webhook.go", "Message")
# Structs embedded in Message; their fields are serialised at the top level.
EMBEDDED_SOURCES = {
    "template.Data": ("template/template.go", "Data"),
    "Data": ("template/template.go", "Data"),
}

FIELD_RE = re.compile(r"^\s*(\w+)\s+(\S+)\s*`json:\"([^\",]+)")
EMBEDDED_RE = re.compile(r"^\s*\*?([\w.]+)\s*$")

GO_TYPES = {
    "string": "string",
    "bool": "boolean",
    "uint": "integer",
    "uint32": "integer",
    "uint64": "integer",
    "int": "integer",
    "int32": "integer",
    "int64": "integer",
    "KV": "object",
    "LabelSet": "object",
    "Alerts": "array",
    "[]string": "array",
    "[]*types.Alert": "array",
    "[]*alert.Alert": "array",
    "time.Time": "string",
    "time.Duration": "string",
}


def fetch(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "incidentd-schema-sync"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        return response.read().decode("utf-8")


def latest_release_tag() -> str:
    return json.loads(fetch(RELEASES_API))["tag_name"]


def parse_struct(source: str, struct_name: str) -> Tuple[Dict[str, str], List[str]]:
    """Return (json field name -> json type, embedded type names) of a Go struct."""
    fields: Dict[str, str] = {}
    embedded: List[str] = []
    depth = 0
    for line in source.splitlines():
        if depth == 0:
            if re.match(r"^type %s struct \{" % re.escape(struct_name), line):
                depth = 1
            continue
        depth += line.count("{") - line.count("}")
        if depth < 1:
            break
        match = FIELD_RE.match(line)
        if match:
            _, go_type, json_name = match.groups()
            fields[json_name] = go_type.strip()
            continue
        if line.strip().startswith("//") or not line.strip():
            continue
        inner = EMBEDDED_RE.match(line)
        if inner:
            embedded.append(inner.group(1))
    return fields, embedded


def json_type_for(go_type: str, known: Optional[str]) -> Tuple[str, Optional[str]]:
    """Map a Go type to a JSON Schema type, plus a warning when it is unknown."""
    if go_type.startswith("*"):
        go_type = go_type[1:]
    if go_type in GO_TYPES:
        return GO_TYPES[go_type], None
    if go_type.startswith("[]"):
        return "array", None
    if known is not None:
        return known, "unknown Go type %r, kept the local type %r" % (go_type, known)
    return "string", "unknown Go type %r, defaulted to string" % (go_type,)


def upstream_fields(tag: str) -> Dict[str, str]:
    """Fields of Message plus everything it embeds, as JSON name -> Go type."""
    path, struct_name = MESSAGE_SOURCE
    source = fetch(SOURCE_URL.format(tag=tag, path=path))
    fields, embedded = parse_struct(source, struct_name)
    for name in embedded:
        location = EMBEDDED_SOURCES.get(name)
        if location is None:
            fields.setdefault(name, "string")
            continue
        path, struct_name = location
        source = fetch(SOURCE_URL.format(tag=tag, path=path))
        embedded_fields, _ = parse_struct(source, struct_name)
        fields.update(embedded_fields)
    if not fields:
        raise ValueError("could not extract any field from %s" % MESSAGE_SOURCE[1])
    return fields


def property_for(json_type: str, description: Optional[str]) -> dict:
    if json_type == "array":
        return {
            "type": "array",
            "description": description
            or "Present in the upstream webhook payload (see schemas/README.md).",
        }
    if json_type == "object":
        return {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": description or "Label or annotation map.",
        }
    if json_type == "integer":
        return {"type": "integer", "minimum": 0, "description": description or ""}
    if json_type == "boolean":
        return {"type": "boolean", "description": description or ""}
    return {"type": "string", "description": description or ""}


def sync(schema_path: Path, tag: str) -> Tuple[bool, List[str]]:
    upstream = upstream_fields(tag)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    properties: Dict[str, dict] = schema.get("properties", {})

    notes: List[str] = []
    drift = False

    for name, go_type in sorted(upstream.items()):
        existing = properties.get(name)
        known = None if existing is None or "$ref" in existing else existing.get("type")
        json_type, warning = json_type_for(go_type, known)
        if warning:
            notes.append("%s: %s" % (name, warning))
        if existing is None:
            notes.append("added: %s (%s, Go %s)" % (name, json_type, go_type))
            properties[name] = property_for(json_type, None)
            drift = True
        elif "$ref" in existing:
            continue  # composed schema (label/annotation maps), already typed by reference
        elif existing.get("type") != json_type:
            notes.append(
                "type change: %s is %s upstream, schema said %s"
                % (name, json_type, existing.get("type"))
            )
            existing["type"] = json_type
            drift = True

    for name in sorted(set(properties) - set(upstream)):
        notes.append("note: %s is declared locally but not in the upstream struct" % name)

    if not drift:
        return False, notes

    schema["properties"] = {name: properties[name] for name in sorted(properties)}
    schema_path.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return True, notes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, type=Path)
    parser.add_argument("--github-output", default=None, help="path to $GITHUB_OUTPUT")
    parser.add_argument("--tag", default=None, help="override the Alertmanager release tag")
    args = parser.parse_args(argv)

    try:
        tag = args.tag or latest_release_tag()
        changed, notes = sync(args.schema, tag)
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("sync_schema: upstream check failed: %s" % exc, file=sys.stderr)
        return 1

    print("alertmanager release: %s" % tag)
    print("schema: %s" % args.schema)
    for note in notes:
        print("  - %s" % note)
    print("changed: %s" % str(changed).lower())

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write("changed=%s\n" % str(changed).lower())
            handle.write("tag=%s\n" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())

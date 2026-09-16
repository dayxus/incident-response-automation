"""CLI contract: replay applies stored payloads, postmortem/list/metrics work."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from incidentd.cli import main

from .conftest import EXAMPLES_DIR, POLICY_PATH, REPO_ROOT


def run_cli(*args: str, cwd: Path = REPO_ROOT):
    process = subprocess.run(
        [sys.executable, "-m", "incidentd", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return process


def test_replay_applies_payloads_in_order(tmp_path):
    database = tmp_path / "replay.db"
    process = run_cli(
        "replay",
        "--dir",
        str(EXAMPLES_DIR),
        "--db",
        str(database),
        "--policy",
        str(POLICY_PATH),
    )
    assert process.returncode == 0, process.stderr
    output = process.stdout
    assert "replay: 5 payload(s) from %s" % EXAMPLES_DIR in output
    assert "incidents created     : 3" in output
    assert "alerts deduplicated   : 1" in output
    assert "incidents resolved    : 1" in output
    assert "incidents open        : 2" in output
    assert "MTTR (mean)           : 47m" in output
    assert "MTTD (mean)           : n/a" in output


def test_replay_json_summary_is_machine_readable(tmp_path):
    database = tmp_path / "replay.db"
    process = run_cli(
        "replay",
        "--dir",
        str(EXAMPLES_DIR),
        "--db",
        str(database),
        "--policy",
        str(POLICY_PATH),
        "--json",
    )
    assert process.returncode == 0, process.stderr
    summary = json.loads(process.stdout)
    assert summary["payloads"] == 5
    assert summary["alerts"] == 5
    assert summary["incidents_created"] == 3
    assert summary["alerts_deduplicated"] == 1
    assert summary["resolved_incidents"] == 1
    assert summary["open_incidents"] == 2
    assert summary["mttr_seconds"] == pytest.approx(2820, abs=1)
    assert summary["mttd_seconds"] is None
    assert summary["db"] == str(database)


def test_replay_output_is_deterministic_across_runs(tmp_path):
    """Same payloads, same policy -> byte-identical summary on a fresh database."""
    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    first = run_cli(
        "replay", "--dir", str(EXAMPLES_DIR), "--db", str(first_db), "--policy", str(POLICY_PATH)
    )
    second = run_cli(
        "replay", "--dir", str(EXAMPLES_DIR), "--db", str(second_db), "--policy", str(POLICY_PATH)
    )
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout.replace(str(first_db), "DB") == second.stdout.replace(str(second_db), "DB")


def test_replay_with_outside_window_timing_creates_new_incidents(tmp_path):
    """--timing now pushes everything past the suppression window: no reopen."""
    database = tmp_path / "replay.db"
    process = run_cli(
        "replay",
        "--dir",
        str(EXAMPLES_DIR),
        "--db",
        str(database),
        "--policy",
        str(POLICY_PATH),
        "--timing",
        "now",
    )
    assert process.returncode == 0, process.stderr
    assert "incidents created     : 3" in process.stdout
    assert "resolved            : 1" not in process.stdout.split("incidents resolved")[0][-5:]


def test_replay_on_empty_directory_fails_cleanly(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    process = run_cli(
        "replay", "--dir", str(empty), "--db", str(tmp_path / "x.db"), "--policy", str(POLICY_PATH)
    )
    assert process.returncode == 2
    assert "no *.json payloads" in process.stderr


def test_postmortem_command_prints_markdown(tmp_path):
    database = tmp_path / "replay.db"
    run_cli(
        "replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)
    )
    process = run_cli("postmortem", "--id", "INC-2026-0001", "--db", str(database))
    assert process.returncode == 0, process.stderr
    assert process.stdout.startswith("# Postmortem — checkout-api (Sev1)")
    assert "## Action items" in process.stdout


def test_postmortem_command_reports_unknown_incident(tmp_path):
    database = tmp_path / "replay.db"
    run_cli(
        "replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)
    )
    process = run_cli("postmortem", "--id", "INC-2026-9999", "--db", str(database))
    assert process.returncode == 1
    assert "no such incident" in process.stderr


def test_list_command_shows_every_incident(tmp_path):
    database = tmp_path / "replay.db"
    run_cli(
        "replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)
    )
    process = run_cli("list", "--db", str(database))
    assert process.returncode == 0, process.stderr
    assert "INC-2026-0001" in process.stdout
    assert "INC-2026-0003" in process.stdout
    assert "3 incident(s)" in process.stdout

    filtered = run_cli("list", "--db", str(database), "--status", "open")
    assert "INC-2026-0001" not in filtered.stdout
    assert "INC-2026-0002" in filtered.stdout


def test_metrics_command_prints_prometheus_text(tmp_path):
    database = tmp_path / "replay.db"
    run_cli(
        "replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)
    )
    process = run_cli("metrics", "--db", str(database))
    assert process.returncode == 0, process.stderr
    assert "# TYPE mttr_seconds gauge" in process.stdout
    assert "mttr_seconds 2820" in process.stdout


def test_version_flag():
    process = run_cli("--version")
    assert process.returncode == 0
    assert process.stdout.startswith("incidentd ")


# --- in-process CLI coverage (the subprocess tests above are the end-to-end ones) ---


def test_replay_in_process(tmp_path, capsys):
    database = tmp_path / "in-process.db"
    code = main(
        ["replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "alerts deduplicated   : 1" in output
    assert "MTTR (mean)           : 47m" in output


def test_replay_now_timing_in_process(tmp_path, capsys):
    database = tmp_path / "in-process.db"
    code = main(
        [
            "replay",
            "--dir",
            str(EXAMPLES_DIR),
            "--db",
            str(database),
            "--policy",
            str(POLICY_PATH),
            "--timing",
            "now",
        ]
    )
    assert code == 0
    assert "incidents created     : 3" in capsys.readouterr().out


def test_list_postmortem_and_metrics_in_process(tmp_path, capsys):
    database = tmp_path / "in-process.db"
    main(
        ["replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)]
    )
    capsys.readouterr()

    assert main(["list", "--db", str(database)]) == 0
    listing = capsys.readouterr().out
    assert "INC-2026-0001" in listing

    assert main(["list", "--db", str(database), "--severity", "Sev3"]) == 0
    assert "INC-2026-0002" in capsys.readouterr().out

    assert main(["list", "--db", str(database), "--service", "nothing-here"]) == 0
    assert "no incidents match the filter" in capsys.readouterr().out

    assert main(["postmortem", "--id", "INC-2026-0001", "--db", str(database)]) == 0
    assert capsys.readouterr().out.startswith("# Postmortem — checkout-api (Sev1)")

    assert main(["metrics", "--db", str(database)]) == 0
    assert "mttr_seconds 2820" in capsys.readouterr().out


def test_list_with_unknown_severity_in_process(tmp_path, capsys):
    database = tmp_path / "in-process.db"
    main(
        ["replay", "--dir", str(EXAMPLES_DIR), "--db", str(database), "--policy", str(POLICY_PATH)]
    )
    capsys.readouterr()
    assert main(["list", "--db", str(database), "--severity", "Sev9"]) == 2
    assert "unknown severity" in capsys.readouterr().err


def test_replay_missing_directory_in_process(tmp_path, capsys):
    code = main(["replay", "--dir", str(tmp_path / "nope"), "--db", str(tmp_path / "x.db")])
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err


def test_serve_builds_the_app(tmp_path, monkeypatch):
    import uvicorn

    captured = {}

    def fake_run(app, host, port, log_level):
        captured["host"] = host
        captured["port"] = port
        captured["services"] = app.state.service.policy.service_names()

    monkeypatch.setattr(uvicorn, "run", fake_run)
    code = main(
        [
            "serve",
            "--db",
            str(tmp_path / "serve.db"),
            "--policy",
            str(POLICY_PATH),
            "--notifier",
            "null",
            "--port",
            "8099",
        ]
    )
    assert code == 0
    assert captured["port"] == 8099
    assert captured["host"] == "127.0.0.1"
    assert "checkout-api" in captured["services"]

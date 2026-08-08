"""Test-Aufbau: echte Bridge, aber ein gefaelschtes `claude`-Binary.

Damit laeuft die komplette Kette (HTTP -> Subprozess -> stream-json -> Job)
ohne API-Kosten und ohne Netz.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "t" * 40

FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Minimal-Nachbau von `claude -p --output-format stream-json`."""
import json, os, sys

argv = sys.argv[1:]
protokoll = os.environ.get("FAKE_CLAUDE_ARGV")
if protokoll:
    with open(protokoll, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")

prompt = argv[1] if len(argv) > 1 and argv[0] in ("-p", "--print") else ""
sid = os.environ.get("FAKE_CLAUDE_SESSION", "11111111-2222-3333-4444-555555555555")


def sende(obj):
    print(json.dumps(obj), flush=True)


sende({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-opus-5"})
sende({"type": "assistant", "parent_tool_use_id": None, "session_id": sid,
       "message": {"content": [{"type": "tool_use", "name": "Read",
                                "input": {"file_path": os.getcwd() + "/README.md"}}]}})
# Subagenten-Rauschen: darf NICHT als Schritt gezaehlt werden.
sende({"type": "assistant", "parent_tool_use_id": "toolu_1", "session_id": sid,
       "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}})

if "FEHLER" in prompt:
    sende({"type": "result", "subtype": "error_during_execution", "is_error": True,
           "result": "Fehlgeschlagen: Testfall", "session_id": sid,
           "total_cost_usd": 0.01, "num_turns": 1, "duration_ms": 12})
    sys.exit(1)

sende({"type": "assistant", "parent_tool_use_id": None, "session_id": sid,
       "message": {"content": [{"type": "text", "text": "Zwischenstand"}]}})
sende({"type": "result", "subtype": "success", "is_error": False,
       "result": "Erledigt: " + prompt[:40], "session_id": sid,
       "total_cost_usd": 0.02, "num_turns": 2, "duration_ms": 34})
'''

PROJEKTE_YAML = """
default_projekt: spielwiese
projekte:
  spielwiese:
    pfad: spielwiese
    beschreibung: "Testprojekt"
    aliase: ["spielwiese", "sandkasten"]
    permission_mode: acceptEdits
    max_turns: 5
    max_budget_usd: 0.25
    allowed_tools: ["Bash(git status *)"]
    disallowed_tools: ["Bash(rm *)"]
  kierp:
    pfad: kierp
    beschreibung: "KIERP"
    aliase: ["kierp", "ki erp"]
    permission_mode: plan
"""


@pytest.fixture
def umgebung(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    workspace = tmp_path / "workspace"
    (workspace / "spielwiese").mkdir(parents=True)
    (workspace / "kierp").mkdir(parents=True)

    config = tmp_path / "config"
    config.mkdir()
    (config / "projects.yaml").write_text(PROJEKTE_YAML, encoding="utf-8")
    (config / "systemprompt.de.md").write_text("Antworte kurz auf Deutsch.\n", encoding="utf-8")

    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    argv_log = tmp_path / "argv.ndjson"

    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("PROJECTS_FILE", str(config / "projects.yaml"))
    monkeypatch.setenv("BASE_SYSTEM_PROMPT_FILE", str(config / "systemprompt.de.md"))
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(argv_log))
    monkeypatch.setenv("PROGRESS_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.delenv("BRIDGE_ALLOW_BYPASS", raising=False)

    return {
        "workspace": workspace,
        "config": config,
        "argv_log": argv_log,
        "jobs": tmp_path / "jobs",
    }


@pytest.fixture
def client(umgebung: dict) -> TestClient:
    from app.main import app

    with TestClient(app) as testclient:
        testclient.headers.update({"X-Bridge-Token": TOKEN})
        yield testclient


def warte_auf_abschluss(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    """Pollt den Job, bis er nicht mehr laeuft."""
    ende = time.monotonic() + timeout
    while time.monotonic() < ende:
        antwort = client.get(f"/jobs/{job_id}")
        antwort.raise_for_status()
        job = antwort.json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"Job {job_id} wurde nicht fertig")


def argv_zeilen(umgebung: dict) -> list[dict]:
    if not umgebung["argv_log"].is_file():
        return []
    return [
        json.loads(z)
        for z in umgebung["argv_log"].read_text(encoding="utf-8").splitlines()
        if z.strip()
    ]


os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

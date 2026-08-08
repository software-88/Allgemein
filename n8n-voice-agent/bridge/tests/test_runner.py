"""Details des Runners: Schrittbeschriftung, Git-Blick, Callback-Signatur."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import lade_einstellungen
from app.runner import ClaudeRunner, git_zusammenfassung, schritt_beschriften
from app.store import JobStore


def test_schritt_beschriften():
    pfad = Path("/workspace/kierp")
    assert schritt_beschriften("Bash", {"command": "git status"}, pfad) == "Bash: git status"
    assert schritt_beschriften("Edit", {"file_path": "/workspace/kierp/api/app/main.py"}, pfad) == (
        "Edit: api/app/main.py"
    )
    # Datei ausserhalb des Projekts bleibt absolut, statt zu knallen
    assert schritt_beschriften("Read", {"file_path": "/etc/hosts"}, pfad) == "Read: /etc/hosts"
    assert schritt_beschriften("Agent", {"description": "Tests pruefen"}, pfad) == (
        "Subagent: Tests pruefen"
    )
    assert schritt_beschriften("Unbekannt", {}, pfad) == "Unbekannt"


def test_lange_bash_zeile_wird_gekuerzt():
    beschriftung = schritt_beschriften("Bash", {"command": "x" * 200}, Path("/workspace/p"))
    assert len(beschriftung) <= 78
    assert beschriftung.endswith("…")


async def test_git_zusammenfassung_ohne_repo(tmp_path: Path):
    assert await git_zusammenfassung(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git nicht installiert")
async def test_git_zusammenfassung_mit_repo(tmp_path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.de"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "a.txt").write_text("hallo", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "erster"], check=True)
    (tmp_path / "b.txt").write_text("offen", encoding="utf-8")

    zusammenfassung = await git_zusammenfassung(tmp_path)
    assert zusammenfassung["branch"] == "main"
    assert "erster" in zusammenfassung["letzter_commit"]
    assert any("b.txt" in z for z in zusammenfassung["offene_aenderungen"])


async def test_callback_ist_signiert(umgebung, monkeypatch):
    einst = lade_einstellungen()
    runner = ClaudeRunner(einst, JobStore(einst.jobs_dir))
    gesendet: dict = {}

    async def fake_post(url, content=None, headers=None):
        gesendet.update({"url": url, "content": content, "headers": headers})

    monkeypatch.setattr(runner._http, "post", fake_post)

    job = {
        "job_id": "j1",
        "callback_url": "http://n8n.local/webhook/x",
        "conversation_key": "slack:C1:1",
        "meta": {"kanal": "C1"},
    }
    await runner._callback(job, {"event": "finished", "ok": True})
    await runner.schliessen()

    erwartet = hmac.new(einst.token.encode(), gesendet["content"], hashlib.sha256).hexdigest()
    assert gesendet["headers"]["X-Bridge-Signature"] == f"sha256={erwartet}"

    nutzlast = json.loads(gesendet["content"])
    assert nutzlast["event"] == "finished"
    assert nutzlast["conversation_key"] == "slack:C1:1"
    assert nutzlast["meta"] == {"kanal": "C1"}


async def test_callback_fehler_bricht_den_job_nicht_ab(umgebung, monkeypatch):
    """n8n nicht erreichbar darf den Lauf nicht zerstoeren."""
    import httpx

    einst = lade_einstellungen()
    runner = ClaudeRunner(einst, JobStore(einst.jobs_dir))

    async def kaputt(*args, **kwargs):
        raise httpx.ConnectError("n8n weg")

    monkeypatch.setattr(runner._http, "post", kaputt)
    await runner._callback({"job_id": "j", "callback_url": "http://x/y"}, {"event": "started"})
    await runner.schliessen()


async def test_ohne_callback_url_passiert_nichts(umgebung):
    einst = lade_einstellungen()
    runner = ClaudeRunner(einst, JobStore(einst.jobs_dir))
    await runner._callback({"job_id": "j"}, {"event": "started"})
    await runner.schliessen()


async def test_abbrechen_unbekannter_job(umgebung):
    einst = lade_einstellungen()
    runner = ClaudeRunner(einst, JobStore(einst.jobs_dir))
    assert await runner.abbrechen("gibtsnicht") is False
    await runner.schliessen()


def test_semaphore_begrenzt_parallelitaet(umgebung, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "3")
    einst = lade_einstellungen()

    async def pruefen():
        runner = ClaudeRunner(einst, JobStore(einst.jobs_dir))
        assert runner._semaphore._value == 3
        await runner.schliessen()

    asyncio.run(pruefen())

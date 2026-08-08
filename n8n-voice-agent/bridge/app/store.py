"""Persistenz: Jobs als JSON-Dateien, Konversations-Zuordnung als eine Datei.

Bewusst ohne Datenbank — auf einer NAS soll man mit `cat` nachsehen koennen,
was der Agent gemacht hat, und ein Neustart des Containers darf laufende
Zuordnungen (Slack-Thread -> Claude-Session) nicht verlieren.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def jetzt_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _atomar_schreiben(ziel: Path, inhalt: str) -> None:
    """Schreiben ohne halbe Dateien, auch wenn der Container hart stirbt."""
    ziel.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ziel.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(inhalt)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ziel)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class JobStore:
    def __init__(self, basis: Path, aufbewahrung: int = 200) -> None:
        self.basis = basis
        self.aufbewahrung = aufbewahrung
        self.basis.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._konv_datei = self.basis / "conversations.json"

    # ---------------------------------------------------------------- Jobs
    def _job_datei(self, job_id: str) -> Path:
        return self.basis / f"{job_id}.json"

    def _log_datei(self, job_id: str) -> Path:
        return self.basis / f"{job_id}.ndjson"

    async def speichern(self, job: dict[str, Any]) -> None:
        async with self._lock:
            _atomar_schreiben(
                self._job_datei(job["job_id"]),
                json.dumps(job, ensure_ascii=False, indent=2),
            )

    async def laden(self, job_id: str) -> dict[str, Any] | None:
        datei = self._job_datei(job_id)
        if not datei.is_file():
            return None
        return json.loads(datei.read_text(encoding="utf-8"))

    async def aktualisieren(self, job_id: str, **felder: Any) -> dict[str, Any] | None:
        async with self._lock:
            datei = self._job_datei(job_id)
            if not datei.is_file():
                return None
            job = json.loads(datei.read_text(encoding="utf-8"))
            job.update(felder)
            _atomar_schreiben(datei, json.dumps(job, ensure_ascii=False, indent=2))
            return job

    async def liste(self, limit: int = 25) -> list[dict[str, Any]]:
        dateien = sorted(
            (p for p in self.basis.glob("*.json") if p.name != "conversations.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        jobs: list[dict[str, Any]] = []
        for datei in dateien[:limit]:
            try:
                jobs.append(json.loads(datei.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return jobs

    def ereignis_anhaengen(self, job_id: str, ereignis: dict[str, Any]) -> None:
        """Roh-Event aus dem stream-json-Protokoll mitschreiben (Debugging)."""
        with self._log_datei(job_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ereignis, ensure_ascii=False) + "\n")

    def log_lesen(self, job_id: str, max_zeilen: int = 500) -> list[dict[str, Any]]:
        datei = self._log_datei(job_id)
        if not datei.is_file():
            return []
        zeilen = datei.read_text(encoding="utf-8").splitlines()[-max_zeilen:]
        ereignisse = []
        for zeile in zeilen:
            try:
                ereignisse.append(json.loads(zeile))
            except json.JSONDecodeError:
                continue
        return ereignisse

    async def aufraeumen(self) -> int:
        """Aelteste Jobs ueber der Aufbewahrungsgrenze loeschen."""
        async with self._lock:
            dateien = sorted(
                (p for p in self.basis.glob("*.json") if p.name != "conversations.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            geloescht = 0
            for datei in dateien[self.aufbewahrung :]:
                datei.unlink(missing_ok=True)
                datei.with_suffix(".ndjson").unlink(missing_ok=True)
                datei.with_suffix(".system.md").unlink(missing_ok=True)
                geloescht += 1
            return geloescht

    # ------------------------------------------------------- Konversationen
    def _konversationen_lesen(self) -> dict[str, Any]:
        if not self._konv_datei.is_file():
            return {}
        try:
            return json.loads(self._konv_datei.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    async def session_fuer(self, key: str) -> str | None:
        async with self._lock:
            eintrag = self._konversationen_lesen().get(key)
            return eintrag.get("session_id") if isinstance(eintrag, dict) else None

    async def session_merken(self, key: str, session_id: str, projekt: str) -> None:
        async with self._lock:
            daten = self._konversationen_lesen()
            daten[key] = {
                "session_id": session_id,
                "projekt": projekt,
                "aktualisiert_am": jetzt_iso(),
            }
            # Deckel, damit die Datei nicht unbegrenzt waechst
            if len(daten) > 500:
                sortiert = sorted(
                    daten.items(),
                    key=lambda kv: kv[1].get("aktualisiert_am", ""),
                    reverse=True,
                )
                daten = dict(sortiert[:500])
            _atomar_schreiben(self._konv_datei, json.dumps(daten, ensure_ascii=False, indent=2))

    async def session_vergessen(self, key: str) -> bool:
        async with self._lock:
            daten = self._konversationen_lesen()
            if key not in daten:
                return False
            daten.pop(key)
            _atomar_schreiben(self._konv_datei, json.dumps(daten, ensure_ascii=False, indent=2))
            return True

"""Fuehrt Claude Code headless aus und uebersetzt den Stream in Job-Events.

Aufrufform (siehe `claude --help` bzw. CLI-Referenz):

    claude -p "<Auftrag>" --output-format stream-json --verbose \
           --permission-mode acceptEdits --append-system-prompt-file <datei> \
           [--resume <session>] [--model ...] [--max-turns N] \
           [--max-budget-usd X] [--allowedTools ...] [--disallowedTools ...]

Wichtig: Der Auftrag steht direkt hinter `-p`. `--allowedTools` nimmt mehrere
Werte entgegen — stuende der Prompt dahinter, wuerde er als Tool-Regel
verschluckt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Einstellungen, Projekt
from .store import JobStore, jetzt_iso

# Wie viel Text der Claude-Antwort wir maximal behalten (Slack/TTS-tauglich).
MAX_ERGEBNIS_ZEICHEN = 3500


def _kuerzen(text: str, laenge: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= laenge else text[: laenge - 1] + "…"


def schritt_beschriften(name: str, eingabe: dict[str, Any], projektpfad: Path) -> str:
    """Aus einem tool_use-Block eine kurze, menschenlesbare Zeile machen."""

    def rel(p: Any) -> str:
        try:
            return str(Path(str(p)).relative_to(projektpfad))
        except (ValueError, TypeError):
            return str(p)

    if name == "Bash":
        return f"Bash: {_kuerzen(eingabe.get('command', ''), 70)}"
    if name in {"Read", "Edit", "Write", "NotebookEdit"}:
        return f"{name}: {rel(eingabe.get('file_path', ''))}"
    if name in {"Grep", "Glob"}:
        return f"{name}: {_kuerzen(eingabe.get('pattern', ''), 40)}"
    if name == "Agent":
        return f"Subagent: {_kuerzen(eingabe.get('description', ''), 50)}"
    if name == "WebFetch":
        return f"WebFetch: {_kuerzen(eingabe.get('url', ''), 60)}"
    return name


class ClaudeRunner:
    """Startet, ueberwacht und beendet Claude-Code-Laeufe."""

    def __init__(self, einst: Einstellungen, store: JobStore) -> None:
        self.einst = einst
        self.store = store
        self._semaphore = asyncio.Semaphore(einst.max_parallele_jobs)
        self._prozesse: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._http = httpx.AsyncClient(timeout=15.0)

    async def schliessen(self) -> None:
        for job_id in list(self._prozesse):
            await self.abbrechen(job_id)
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._http.aclose()

    # ------------------------------------------------------------- oeffentlich
    def starten(self, job: dict[str, Any], projekt: Projekt) -> None:
        task = asyncio.create_task(self._lauf(job, projekt), name=f"job-{job['job_id']}")
        self._tasks[job["job_id"]] = task
        task.add_done_callback(lambda t: self._tasks.pop(job["job_id"], None))

    async def abbrechen(self, job_id: str) -> bool:
        prozess = self._prozesse.get(job_id)
        if prozess is None or prozess.returncode is not None:
            return False
        await self._prozess_beenden(prozess)
        return True

    @property
    def laufende(self) -> list[str]:
        return list(self._prozesse)

    # ----------------------------------------------------------------- intern
    async def _prozess_beenden(self, prozess: asyncio.subprocess.Process) -> None:
        """SIGTERM an die ganze Prozessgruppe, danach hart nachfassen."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(prozess.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(prozess.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(prozess.pid), signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(prozess.wait(), timeout=5)

    def _systemprompt_schreiben(self, job_id: str, projekt: Projekt) -> Path:
        teile = [self.einst.basis_systemprompt.strip(), projekt.zusatz_systemprompt.strip()]
        datei = self.einst.jobs_dir / f"{job_id}.system.md"
        datei.write_text("\n\n".join(t for t in teile if t) + "\n", encoding="utf-8")
        return datei

    def _argumente(
        self,
        job: dict[str, Any],
        projekt: Projekt,
        systemprompt_datei: Path,
    ) -> list[str]:
        argv = [
            self.einst.claude_bin,
            "-p",
            job["prompt"],
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            job["permission_mode"],
            "--append-system-prompt-file",
            str(systemprompt_datei),
        ]
        modell = job.get("model") or projekt.model
        if modell:
            argv += ["--model", modell]
        if job.get("session_id"):
            argv += ["--resume", job["session_id"]]
        if projekt.max_turns:
            argv += ["--max-turns", str(projekt.max_turns)]
        if projekt.max_budget_usd:
            argv += ["--max-budget-usd", str(projekt.max_budget_usd)]
        # Variadische Flags ans Ende — sie duerfen den Prompt nicht schlucken.
        if projekt.disallowed_tools:
            argv += ["--disallowedTools", *projekt.disallowed_tools]
        if projekt.allowed_tools:
            argv += ["--allowedTools", *projekt.allowed_tools]
        return argv

    async def _lauf(self, job: dict[str, Any], projekt: Projekt) -> None:
        job_id = job["job_id"]
        async with self._semaphore:
            begonnen = time.monotonic()
            systemprompt_datei = self._systemprompt_schreiben(job_id, projekt)
            argv = self._argumente(job, projekt, systemprompt_datei)

            umgebung = dict(os.environ)
            umgebung["GIT_TERMINAL_PROMPT"] = "0"
            umgebung["CI"] = "1"

            await self.store.aktualisieren(job_id, status="running", gestartet_am=jetzt_iso())
            await self._callback(job, {"event": "started", "job_id": job_id})

            try:
                prozess = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(projekt.pfad),
                    env=umgebung,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,  # eigene Prozessgruppe -> sauber killbar
                )
            except FileNotFoundError:
                await self._abschluss(
                    job, projekt, status="failed",
                    fehler=f"Claude-CLI nicht gefunden: {self.einst.claude_bin}",
                    dauer_ms=0,
                )
                return

            self._prozesse[job_id] = prozess
            zustand: dict[str, Any] = {
                "session_id": job.get("session_id"),
                "schritte": [],
                "ergebnis": None,
                "kosten_usd": None,
                "turns": None,
                "is_error": False,
                "subtype": None,
                "letzter_text": None,
            }

            try:
                await asyncio.wait_for(
                    self._stream_lesen(job, projekt, prozess, zustand),
                    timeout=self.einst.job_timeout_s,
                )
                roh_stderr = await prozess.stderr.read() if prozess.stderr else b""
                stderr = roh_stderr.decode("utf-8", "replace")
                rc = await prozess.wait()
                status = "done"
                fehler = None
                if zustand["is_error"] or rc != 0:
                    status = "failed"
                    fehler = zustand["ergebnis"] or _kuerzen(stderr, 800) or f"Exit-Code {rc}"
                if zustand["subtype"] == "error_max_turns":
                    fehler = "Turn-Limit erreicht — Auftrag war zu gross oder zu vage."
            except TimeoutError:
                await self._prozess_beenden(prozess)
                status, fehler = "timeout", (
                    f"Zeitlimit von {self.einst.job_timeout_s}s ueberschritten — Lauf abgebrochen."
                )
            except asyncio.CancelledError:
                await self._prozess_beenden(prozess)
                status, fehler = "cancelled", "Abgebrochen."
                raise
            finally:
                self._prozesse.pop(job_id, None)
                systemprompt_datei.unlink(missing_ok=True)

            if status == "done" and not zustand["ergebnis"]:
                zustand["ergebnis"] = zustand["letzter_text"] or "(keine Textantwort)"

            await self._abschluss(
                job,
                projekt,
                status=status,
                fehler=fehler,
                dauer_ms=int((time.monotonic() - begonnen) * 1000),
                session_id=zustand["session_id"],
                ergebnis=_kuerzen(zustand["ergebnis"] or "", MAX_ERGEBNIS_ZEICHEN) or None,
                kosten_usd=zustand["kosten_usd"],
                turns=zustand["turns"],
                schritte=zustand["schritte"],
            )

    async def _stream_lesen(
        self,
        job: dict[str, Any],
        projekt: Projekt,
        prozess: asyncio.subprocess.Process,
        zustand: dict[str, Any],
    ) -> None:
        job_id = job["job_id"]
        letzte_meldung = 0.0
        assert prozess.stdout is not None

        while True:
            zeile = await prozess.stdout.readline()
            if not zeile:
                break
            roh = zeile.decode("utf-8", "replace").strip()
            if not roh:
                continue
            try:
                ereignis = json.loads(roh)
            except json.JSONDecodeError:
                continue

            self.store.ereignis_anhaengen(job_id, ereignis)
            typ = ereignis.get("type")

            if typ == "system" and ereignis.get("subtype") == "init":
                zustand["session_id"] = ereignis.get("session_id") or zustand["session_id"]
                await self.store.aktualisieren(job_id, session_id=zustand["session_id"])
                if job.get("conversation_key") and zustand["session_id"]:
                    await self.store.session_merken(
                        job["conversation_key"], zustand["session_id"], projekt.key
                    )

            elif typ == "system" and ereignis.get("subtype") == "api_retry":
                zustand["schritte"].append(
                    f"API-Wiederholung ({ereignis.get('error', 'unbekannt')}), "
                    f"Versuch {ereignis.get('attempt')}"
                )

            elif typ == "assistant":
                # Nur die Hauptkonversation zaehlt; Subagenten-Rauschen ignorieren.
                if ereignis.get("parent_tool_use_id"):
                    continue
                for block in ereignis.get("message", {}).get("content", []) or []:
                    if block.get("type") == "tool_use":
                        zustand["schritte"].append(
                            schritt_beschriften(
                                block.get("name", "Tool"),
                                block.get("input", {}) or {},
                                projekt.pfad,
                            )
                        )
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        zustand["letzter_text"] = block["text"].strip()

            elif typ == "result":
                zustand["ergebnis"] = ereignis.get("result") or zustand["letzter_text"]
                zustand["kosten_usd"] = ereignis.get("total_cost_usd")
                zustand["turns"] = ereignis.get("num_turns")
                zustand["is_error"] = bool(ereignis.get("is_error"))
                zustand["subtype"] = ereignis.get("subtype")
                zustand["session_id"] = ereignis.get("session_id") or zustand["session_id"]

            # Zwischenstand nach draussen — gedrosselt, damit Slack nicht flutet.
            jetzt = time.monotonic()
            if zustand["schritte"] and jetzt - letzte_meldung >= self.einst.fortschritt_intervall_s:
                letzte_meldung = jetzt
                await self.store.aktualisieren(job_id, schritte=zustand["schritte"][-12:])
                await self._callback(
                    job,
                    {
                        "event": "progress",
                        "job_id": job_id,
                        "schritt": zustand["schritte"][-1],
                        "anzahl_schritte": len(zustand["schritte"]),
                    },
                )

    async def _abschluss(self, job: dict[str, Any], projekt: Projekt, **felder: Any) -> None:
        felder.setdefault("schritte", [])
        felder["schritte"] = felder["schritte"][-25:]
        felder["beendet_am"] = jetzt_iso()
        felder["git"] = await git_zusammenfassung(projekt.pfad)

        aktualisiert = await self.store.aktualisieren(job["job_id"], **felder)
        job = aktualisiert or {**job, **felder}

        if job.get("conversation_key") and job.get("session_id"):
            await self.store.session_merken(
                job["conversation_key"], job["session_id"], projekt.key
            )

        await self._callback(
            job,
            {
                "event": "finished",
                "job_id": job["job_id"],
                "status": job.get("status"),
                "ok": job.get("status") == "done",
                "projekt": projekt.key,
                "session_id": job.get("session_id"),
                "ergebnis": job.get("ergebnis"),
                "fehler": job.get("fehler"),
                "schritte": job.get("schritte", []),
                "kosten_usd": job.get("kosten_usd"),
                "dauer_ms": job.get("dauer_ms"),
                "turns": job.get("turns"),
                "git": job.get("git"),
            },
        )
        await self.store.aufraeumen()

    async def _callback(self, job: dict[str, Any], nutzlast: dict[str, Any]) -> None:
        url = job.get("callback_url")
        if not url:
            return
        nutzlast = {
            **nutzlast,
            "conversation_key": job.get("conversation_key"),
            "meta": job.get("meta", {}),
            "zeit": jetzt_iso(),
        }
        koerper = json.dumps(nutzlast, ensure_ascii=False).encode("utf-8")
        signatur = hmac.new(self.einst.token.encode(), koerper, hashlib.sha256).hexdigest()
        try:
            await self._http.post(
                url,
                content=koerper,
                headers={
                    "Content-Type": "application/json",
                    "X-Bridge-Signature": f"sha256={signatur}",
                },
            )
        except httpx.HTTPError as fehler:  # n8n nicht erreichbar -> Job trotzdem fertig
            print(f"[bridge] Callback fehlgeschlagen ({url}): {fehler}", flush=True)


async def git_zusammenfassung(pfad: Path) -> dict[str, Any] | None:
    """Kurzer Blick ins Repo: Branch, letzter Commit, offene Aenderungen."""

    async def git(*args: str) -> str | None:
        try:
            prozess = await asyncio.create_subprocess_exec(
                "git", "-C", str(pfad), *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            aus, _ = await asyncio.wait_for(prozess.communicate(), timeout=10)
        except (TimeoutError, FileNotFoundError, OSError):
            return None
        if prozess.returncode != 0:
            return None
        return aus.decode("utf-8", "replace").strip()

    if await git("rev-parse", "--is-inside-work-tree") != "true":
        return None
    status = await git("status", "--porcelain") or ""
    return {
        "branch": await git("rev-parse", "--abbrev-ref", "HEAD"),
        "letzter_commit": await git("log", "-1", "--pretty=%h %s"),
        "offene_aenderungen": [z for z in status.splitlines() if z][:20],
    }

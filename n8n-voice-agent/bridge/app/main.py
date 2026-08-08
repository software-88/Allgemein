"""Claude-Bridge — HTTP-Fassade fuer Claude Code auf der Synology.

n8n (oder jeder andere Client) schickt einen Auftrag an POST /jobs; die Bridge
startet Claude Code headless im freigegebenen Projektverzeichnis und meldet
Fortschritt sowie Ergebnis per Callback zurueck.

Alle Endpunkte ausser /healthz brauchen den Bridge-Token
(Header `X-Bridge-Token` oder `Authorization: Bearer <token>`).
"""

from __future__ import annotations

import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .config import Einstellungen, KonfigFehler, deckle_permission_mode, lade_einstellungen
from .models import (
    JobAngelegt,
    JobAnlegen,
    JobDetail,
    JobKurz,
    ProjektInfo,
    RouteAnfrage,
    RouteAntwort,
)
from .runner import ClaudeRunner
from .store import JobStore, jetzt_iso


def _job_id() -> str:
    stempel = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stempel}-{secrets.token_hex(3)}"


@asynccontextmanager
async def lebenszyklus(app: FastAPI):
    einst = lade_einstellungen()
    store = JobStore(einst.jobs_dir, einst.job_aufbewahrung)
    runner = ClaudeRunner(einst, store)

    app.state.einst = einst
    app.state.store = store
    app.state.runner = runner

    # Nach einem Container-Neustart gibt es keine laufenden Prozesse mehr.
    for job in await store.liste(limit=einst.job_aufbewahrung):
        if job.get("status") in {"queued", "running"}:
            await store.aktualisieren(
                job["job_id"],
                status="failed",
                fehler="Bridge wurde neu gestartet, waehrend der Job lief.",
                beendet_am=jetzt_iso(),
            )

    print(
        f"[bridge] bereit — Projekte: {', '.join(einst.projekte)} "
        f"(Standard: {einst.default_projekt or 'keins'})",
        flush=True,
    )
    try:
        yield
    finally:
        await runner.schliessen()


app = FastAPI(
    title="Claude-Bridge",
    version="1.0.0",
    summary="Steuert Claude Code headless auf der Synology (n8n-Sprachagent).",
    lifespan=lebenszyklus,
)


# ------------------------------------------------------------------ Auth
async def pruefe_token(
    request: Request,
    x_bridge_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Einstellungen:
    einst: Einstellungen = request.app.state.einst
    angeboten = x_bridge_token or ""
    if not angeboten and authorization and authorization.lower().startswith("bearer "):
        angeboten = authorization[7:]
    if not angeboten or not hmac.compare_digest(angeboten, einst.token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Ungueltiger oder fehlender Bridge-Token")
    return einst


Auth = Annotated[Einstellungen, Depends(pruefe_token)]


# ------------------------------------------------------------------ Routen
@app.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> dict:
    einst: Einstellungen = request.app.state.einst
    runner: ClaudeRunner = request.app.state.runner
    return {
        "ok": True,
        "projekte": list(einst.projekte),
        "laufende_jobs": len(runner.laufende),
        "zeit": jetzt_iso(),
    }


@app.get("/projekte", response_model=list[ProjektInfo])
async def projekte(einst: Auth) -> list[ProjektInfo]:
    return [ProjektInfo(**p.als_dict()) for p in einst.projekte.values()]


@app.post("/route", response_model=RouteAntwort)
async def route(anfrage: RouteAnfrage, einst: Auth) -> RouteAntwort:
    """Ordnet freien (diktierten) Text einem Projekt zu — Fallback: Standard."""
    projekt = einst.projekt_aus_text(anfrage.text)
    if projekt:
        return RouteAntwort(projekt=projekt.key, erkannt=True, beschreibung=projekt.beschreibung)
    if einst.default_projekt:
        standard = einst.projekte[einst.default_projekt]
        return RouteAntwort(projekt=standard.key, erkannt=False, beschreibung=standard.beschreibung)
    return RouteAntwort(projekt=None, erkannt=False)


@app.post("/jobs", response_model=JobAngelegt, status_code=status.HTTP_202_ACCEPTED)
async def job_anlegen(anfrage: JobAnlegen, request: Request, einst: Auth) -> JobAngelegt:
    store: JobStore = request.app.state.store
    runner: ClaudeRunner = request.app.state.runner

    prompt = anfrage.prompt.strip()
    if not prompt:
        raise HTTPException(422, "Leerer Auftrag")
    if len(prompt) > einst.max_prompt_zeichen:
        raise HTTPException(
            413,
            f"Auftrag zu lang ({len(prompt)} Zeichen, erlaubt {einst.max_prompt_zeichen})",
        )

    # Projekt: explizit, sonst aus dem Text geraten, sonst Standard.
    schluessel = anfrage.projekt or (
        p.key if (p := einst.projekt_aus_text(prompt)) else None
    )
    try:
        projekt = einst.projekt(schluessel)
    except KeyError as fehler:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(fehler)) from fehler

    try:
        modus = deckle_permission_mode(anfrage.permission_mode, projekt)
    except ValueError as fehler:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(fehler)) from fehler

    if not projekt.pfad.is_dir():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Projektverzeichnis fehlt im Container: {projekt.pfad}",
        )

    # Sitzung fortsetzen? Explizite ID schlaegt Konversations-Zuordnung.
    session_id = anfrage.session_id
    if anfrage.neu_starten and anfrage.conversation_key:
        await store.session_vergessen(anfrage.conversation_key)
    elif not session_id and anfrage.conversation_key:
        session_id = await store.session_fuer(anfrage.conversation_key)

    job = {
        "job_id": _job_id(),
        "status": "queued",
        "projekt": projekt.key,
        "prompt": prompt,
        "permission_mode": modus,
        "model": anfrage.model or projekt.model,
        "session_id": session_id,
        "conversation_key": anfrage.conversation_key,
        "callback_url": anfrage.callback_url,
        "meta": anfrage.meta,
        "erstellt_am": jetzt_iso(),
        "schritte": [],
    }
    await store.speichern(job)
    runner.starten(job, projekt)

    return JobAngelegt(
        job_id=job["job_id"],
        status="queued",
        projekt=projekt.key,
        fortgesetzt=bool(session_id),
    )


@app.get("/jobs", response_model=list[JobKurz])
async def jobs(request: Request, einst: Auth, limit: int = 20) -> list[JobKurz]:
    store: JobStore = request.app.state.store
    return [
        JobKurz(**{k: j.get(k) for k in JobKurz.model_fields}) for j in await store.liste(limit)
    ]


@app.get("/jobs/{job_id}", response_model=JobDetail)
async def job(job_id: str, request: Request, einst: Auth) -> JobDetail:
    store: JobStore = request.app.state.store
    daten = await store.laden(job_id)
    if daten is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job unbekannt")
    return JobDetail(**{k: daten.get(k) for k in JobDetail.model_fields if k in daten})


@app.get("/jobs/{job_id}/log")
async def job_log(job_id: str, request: Request, einst: Auth, max_zeilen: int = 300) -> dict:
    store: JobStore = request.app.state.store
    if await store.laden(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job unbekannt")
    return {"job_id": job_id, "ereignisse": store.log_lesen(job_id, max_zeilen)}


@app.post("/jobs/{job_id}/abbrechen")
async def job_abbrechen(job_id: str, request: Request, einst: Auth) -> dict:
    store: JobStore = request.app.state.store
    runner: ClaudeRunner = request.app.state.runner
    if await store.laden(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job unbekannt")
    beendet = await runner.abbrechen(job_id)
    if beendet:
        await store.aktualisieren(
            job_id, status="cancelled", fehler="Manuell abgebrochen.", beendet_am=jetzt_iso()
        )
    return {"job_id": job_id, "abgebrochen": beendet}


@app.delete("/konversationen/{key:path}")
async def konversation_zuruecksetzen(key: str, request: Request, einst: Auth) -> dict:
    """Naechster Auftrag in diesem Slack-Thread startet wieder bei null."""
    store: JobStore = request.app.state.store
    return {"key": key, "geloescht": await store.session_vergessen(key)}


def main() -> None:  # pragma: no cover - nur fuer `python -m app.main`
    import uvicorn

    try:
        lade_einstellungen()
    except KonfigFehler as fehler:
        raise SystemExit(f"Konfigurationsfehler: {fehler}") from fehler
    uvicorn.run(app, host="0.0.0.0", port=8790)


if __name__ == "__main__":  # pragma: no cover
    main()

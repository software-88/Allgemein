"""Request-/Response-Modelle der Bridge-API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "done", "failed", "cancelled", "timeout"]


class JobAnlegen(BaseModel):
    """Ein Auftrag aus n8n (in der Regel eine transkribierte Sprachnachricht)."""

    prompt: str = Field(min_length=1)
    projekt: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    # Fortsetzung: entweder direkt eine Session-ID oder ein stabiler
    # Konversationsschluessel (z. B. "slack:C123:1699.42"), den die Bridge
    # selbst auf die letzte Session abbildet.
    session_id: str | None = None
    conversation_key: str | None = None
    neu_starten: bool = False
    callback_url: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class JobKurz(BaseModel):
    job_id: str
    status: JobStatus
    projekt: str
    erstellt_am: str
    session_id: str | None = None


class JobDetail(JobKurz):
    prompt: str
    permission_mode: str
    model: str | None = None
    conversation_key: str | None = None
    beendet_am: str | None = None
    dauer_ms: int | None = None
    kosten_usd: float | None = None
    turns: int | None = None
    ergebnis: str | None = None
    fehler: str | None = None
    schritte: list[str] = Field(default_factory=list)
    git: dict[str, Any] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class JobAngelegt(BaseModel):
    job_id: str
    status: JobStatus
    projekt: str
    fortgesetzt: bool = False


class ProjektInfo(BaseModel):
    key: str
    beschreibung: str
    aliase: list[str]
    permission_mode: str
    model: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None


class RouteAnfrage(BaseModel):
    """Hilfsendpunkt fuer n8n: aus Diktat-Text das Projekt bestimmen."""

    text: str


class RouteAntwort(BaseModel):
    projekt: str | None
    erkannt: bool
    beschreibung: str = ""

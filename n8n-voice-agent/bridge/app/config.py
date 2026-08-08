"""Konfiguration der Claude-Bridge: Umgebung + Projekt-Allowlist.

Grundsatz: Die Bridge darf Claude Code NUR in Verzeichnissen starten, die in
`projects.yaml` ausdruecklich freigegeben sind und unterhalb von WORKSPACE_ROOT
liegen. Alles andere wird abgelehnt (kein Pfad aus dem Request).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# Permission-Modes von `claude --permission-mode`, sortiert nach Freizuegigkeit.
# Wird benutzt, um eine Anfrage auf das Projekt-Maximum zu deckeln.
PERMISSION_RANG: dict[str, int] = {
    "plan": 0,
    "dontAsk": 1,
    "default": 2,
    "acceptEdits": 3,
    "auto": 4,
    "bypassPermissions": 5,
}


class KonfigFehler(RuntimeError):
    """Fehlerhafte oder unsichere Konfiguration."""


@dataclass(frozen=True)
class Projekt:
    """Ein freigegebenes Arbeitsverzeichnis samt Leitplanken."""

    key: str
    pfad: Path
    beschreibung: str = ""
    aliase: tuple[str, ...] = ()
    permission_mode: str = "acceptEdits"
    model: str | None = None
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    max_turns: int | None = 40
    max_budget_usd: float | None = 2.0
    zusatz_systemprompt: str = ""

    def als_dict(self) -> dict:
        return {
            "key": self.key,
            "beschreibung": self.beschreibung,
            "aliase": list(self.aliase),
            "permission_mode": self.permission_mode,
            "model": self.model,
            "max_turns": self.max_turns,
            "max_budget_usd": self.max_budget_usd,
        }


@dataclass(frozen=True)
class Einstellungen:
    """Alles, was ueber Umgebungsvariablen gesteuert wird."""

    token: str
    workspace_root: Path
    jobs_dir: Path
    projekte: dict[str, Projekt]
    default_projekt: str | None
    claude_bin: str = "claude"
    max_parallele_jobs: int = 2
    job_timeout_s: int = 1800
    fortschritt_intervall_s: int = 25
    max_prompt_zeichen: int = 8000
    bypass_erlaubt: bool = False
    basis_systemprompt: str = ""
    job_aufbewahrung: int = 200

    def projekt(self, key: str | None) -> Projekt:
        gewaehlt = key or self.default_projekt
        if not gewaehlt:
            raise KeyError("kein Projekt angegeben und kein Standardprojekt gesetzt")
        if gewaehlt not in self.projekte:
            raise KeyError(f"unbekanntes Projekt: {gewaehlt}")
        return self.projekte[gewaehlt]

    def projekt_aus_text(self, text: str) -> Projekt | None:
        """Ordnet freien Text (Diktat) ueber Key/Alias einem Projekt zu."""
        klein = text.lower()
        treffer: list[tuple[int, Projekt]] = []
        for projekt in self.projekte.values():
            for name in (projekt.key, *projekt.aliase):
                if name and name.lower() in klein:
                    treffer.append((len(name), projekt))
        if not treffer:
            return None
        # laengster Alias gewinnt ("kierp-web" schlaegt "kierp")
        treffer.sort(key=lambda t: t[0], reverse=True)
        return treffer[0][1]


def _pfad_env(name: str, standard: str) -> Path:
    return Path(os.environ.get(name, standard)).expanduser()


def _projekt_aus_yaml(key: str, roh: dict, workspace_root: Path, bypass_erlaubt: bool) -> Projekt:
    if not isinstance(roh, dict):
        raise KonfigFehler(f"Projekt '{key}': Abschnitt muss ein Mapping sein")

    rel = str(roh.get("pfad") or key)
    pfad = (workspace_root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()

    # Ausbruch aus dem Workspace verhindern (z. B. pfad: "../../etc").
    if pfad != workspace_root and workspace_root not in pfad.parents:
        raise KonfigFehler(
            f"Projekt '{key}': Pfad {pfad} liegt ausserhalb von {workspace_root}"
        )

    modus = str(roh.get("permission_mode", "acceptEdits"))
    if modus not in PERMISSION_RANG:
        raise KonfigFehler(f"Projekt '{key}': unbekannter permission_mode '{modus}'")
    if modus == "bypassPermissions" and not bypass_erlaubt:
        raise KonfigFehler(
            f"Projekt '{key}': permission_mode 'bypassPermissions' erfordert "
            "BRIDGE_ALLOW_BYPASS=1 (bitte nur bewusst setzen)"
        )

    budget = roh.get("max_budget_usd", 2.0)
    turns = roh.get("max_turns", 40)

    return Projekt(
        key=key,
        pfad=pfad,
        beschreibung=str(roh.get("beschreibung", "")),
        aliase=tuple(str(a) for a in roh.get("aliase", []) or ()),
        permission_mode=modus,
        model=(str(roh["model"]) if roh.get("model") else None),
        allowed_tools=tuple(str(t) for t in roh.get("allowed_tools", []) or ()),
        disallowed_tools=tuple(str(t) for t in roh.get("disallowed_tools", []) or ()),
        max_turns=(int(turns) if turns is not None else None),
        max_budget_usd=(float(budget) if budget is not None else None),
        zusatz_systemprompt=str(roh.get("zusatz_systemprompt", "")),
    )


def lade_einstellungen() -> Einstellungen:
    """Liest Umgebung + projects.yaml. Wirft bei unsicherer Konfiguration."""
    token = os.environ.get("BRIDGE_TOKEN", "").strip()
    if len(token) < 24:
        raise KonfigFehler(
            "BRIDGE_TOKEN fehlt oder ist zu kurz (mind. 24 Zeichen). "
            "Erzeugen mit: openssl rand -hex 32"
        )

    workspace_root = _pfad_env("WORKSPACE_ROOT", "/workspace").resolve()
    jobs_dir = _pfad_env("JOBS_DIR", "/data/jobs")
    jobs_dir.mkdir(parents=True, exist_ok=True)

    bypass_erlaubt = os.environ.get("BRIDGE_ALLOW_BYPASS", "").lower() in {"1", "true", "yes"}

    projects_file = _pfad_env("PROJECTS_FILE", "/config/projects.yaml")
    if not projects_file.is_file():
        raise KonfigFehler(f"Projektdatei nicht gefunden: {projects_file}")
    roh = yaml.safe_load(projects_file.read_text(encoding="utf-8")) or {}

    projekt_block = roh.get("projekte") or {}
    if not projekt_block:
        raise KonfigFehler(f"{projects_file} enthaelt keine Projekte")

    projekte = {
        key: _projekt_aus_yaml(key, wert, workspace_root, bypass_erlaubt)
        for key, wert in projekt_block.items()
    }

    default_projekt = roh.get("default_projekt")
    if default_projekt and default_projekt not in projekte:
        raise KonfigFehler(f"default_projekt '{default_projekt}' ist nicht definiert")

    prompt_datei = _pfad_env("BASE_SYSTEM_PROMPT_FILE", "/config/systemprompt.de.md")
    basis_prompt = prompt_datei.read_text(encoding="utf-8") if prompt_datei.is_file() else ""

    return Einstellungen(
        token=token,
        workspace_root=workspace_root,
        jobs_dir=jobs_dir,
        projekte=projekte,
        default_projekt=default_projekt,
        claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
        max_parallele_jobs=int(os.environ.get("MAX_CONCURRENT_JOBS", "2")),
        job_timeout_s=int(os.environ.get("JOB_TIMEOUT_SECONDS", "1800")),
        fortschritt_intervall_s=int(os.environ.get("PROGRESS_INTERVAL_SECONDS", "25")),
        max_prompt_zeichen=int(os.environ.get("MAX_PROMPT_CHARS", "8000")),
        bypass_erlaubt=bypass_erlaubt,
        basis_systemprompt=basis_prompt,
        job_aufbewahrung=int(os.environ.get("JOB_RETENTION", "200")),
    )


def deckle_permission_mode(gewuenscht: str | None, projekt: Projekt) -> str:
    """Nie freizuegiger als das Projekt erlaubt."""
    if not gewuenscht:
        return projekt.permission_mode
    if gewuenscht not in PERMISSION_RANG:
        raise ValueError(f"unbekannter permission_mode: {gewuenscht}")
    if PERMISSION_RANG[gewuenscht] > PERMISSION_RANG[projekt.permission_mode]:
        return projekt.permission_mode
    return gewuenscht

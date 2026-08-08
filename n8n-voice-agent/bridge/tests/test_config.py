"""Leitplanken der Konfiguration — hier haengt die Sicherheit dran."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    KonfigFehler,
    Projekt,
    deckle_permission_mode,
    lade_einstellungen,
)


def test_projekt_ausserhalb_workspace_wird_abgelehnt(umgebung, monkeypatch):
    (umgebung["config"] / "projects.yaml").write_text(
        "projekte:\n  boese:\n    pfad: ../../etc\n", encoding="utf-8"
    )
    with pytest.raises(KonfigFehler, match="ausserhalb"):
        lade_einstellungen()


def test_absoluter_pfad_ausserhalb_workspace_wird_abgelehnt(umgebung):
    (umgebung["config"] / "projects.yaml").write_text(
        "projekte:\n  boese:\n    pfad: /etc\n", encoding="utf-8"
    )
    with pytest.raises(KonfigFehler, match="ausserhalb"):
        lade_einstellungen()


def test_bypass_braucht_ausdrueckliche_freigabe(umgebung, monkeypatch):
    (umgebung["config"] / "projects.yaml").write_text(
        "projekte:\n  frei:\n    pfad: spielwiese\n"
        "    permission_mode: bypassPermissions\n",
        encoding="utf-8",
    )
    with pytest.raises(KonfigFehler, match="BRIDGE_ALLOW_BYPASS"):
        lade_einstellungen()

    monkeypatch.setenv("BRIDGE_ALLOW_BYPASS", "1")
    einst = lade_einstellungen()
    assert einst.projekte["frei"].permission_mode == "bypassPermissions"


def test_kurzer_token_wird_abgelehnt(umgebung, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "kurz")
    with pytest.raises(KonfigFehler, match="BRIDGE_TOKEN"):
        lade_einstellungen()


def test_unbekannter_permission_mode(umgebung):
    (umgebung["config"] / "projects.yaml").write_text(
        "projekte:\n  x:\n    pfad: spielwiese\n    permission_mode: yolo\n", encoding="utf-8"
    )
    with pytest.raises(KonfigFehler, match="permission_mode"):
        lade_einstellungen()


def test_default_projekt_muss_existieren(umgebung):
    (umgebung["config"] / "projects.yaml").write_text(
        "default_projekt: gibtsnicht\nprojekte:\n  x:\n    pfad: spielwiese\n", encoding="utf-8"
    )
    with pytest.raises(KonfigFehler, match="default_projekt"):
        lade_einstellungen()


def test_permission_mode_wird_gedeckelt():
    projekt = Projekt(key="p", pfad=Path("/workspace/p"), permission_mode="acceptEdits")
    # Freizuegiger als erlaubt -> auf Projektwert zurueckgestuft
    assert deckle_permission_mode("bypassPermissions", projekt) == "acceptEdits"
    # Restriktiver ist immer erlaubt
    assert deckle_permission_mode("plan", projekt) == "plan"
    # Kein Wunsch -> Projektvorgabe
    assert deckle_permission_mode(None, projekt) == "acceptEdits"


def test_unbekannter_modus_im_request():
    projekt = Projekt(key="p", pfad=Path("/workspace/p"))
    with pytest.raises(ValueError):
        deckle_permission_mode("gottmodus", projekt)


def test_projekt_aus_diktat_text(umgebung):
    einst = lade_einstellungen()
    assert einst.projekt_aus_text("bitte im Sandkasten was bauen").key == "spielwiese"
    assert einst.projekt_aus_text("mach das in KIERP fertig").key == "kierp"
    assert einst.projekt_aus_text("nichts davon") is None

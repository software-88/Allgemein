"""Ende-zu-Ende ueber die HTTP-API, mit gefaelschtem `claude`-Binary."""

from __future__ import annotations

import time

from conftest import TOKEN, argv_zeilen, warte_auf_abschluss


def test_ohne_token_kein_zugriff(client):
    antwort = client.get("/projekte", headers={"X-Bridge-Token": ""})
    assert antwort.status_code == 401


def test_falscher_token(client):
    antwort = client.get("/projekte", headers={"X-Bridge-Token": "x" * 40})
    assert antwort.status_code == 401


def test_bearer_token_geht_auch(client):
    antwort = client.get(
        "/projekte",
        headers={"X-Bridge-Token": "", "Authorization": f"Bearer {TOKEN}"},
    )
    assert antwort.status_code == 200


def test_healthz_ohne_token(client):
    antwort = client.get("/healthz", headers={"X-Bridge-Token": ""})
    assert antwort.status_code == 200
    assert antwort.json()["ok"] is True


def test_projekte_auflisten(client):
    daten = client.get("/projekte").json()
    keys = {p["key"] for p in daten}
    assert keys == {"spielwiese", "kierp"}


def test_route_erkennt_projekt(client):
    assert client.post("/route", json={"text": "im Sandkasten bitte"}).json() == {
        "projekt": "spielwiese",
        "erkannt": True,
        "beschreibung": "Testprojekt",
    }
    # Nichts erkannt -> Standardprojekt, aber ehrlich als "nicht erkannt" markiert
    antwort = client.post("/route", json={"text": "irgendwas"}).json()
    assert antwort["projekt"] == "spielwiese"
    assert antwort["erkannt"] is False


def test_job_laeuft_durch(client, umgebung):
    antwort = client.post(
        "/jobs", json={"prompt": "Bau mir eine Testdatei", "projekt": "spielwiese"}
    )
    assert antwort.status_code == 202
    angelegt = antwort.json()
    assert angelegt["fortgesetzt"] is False

    job = warte_auf_abschluss(client, angelegt["job_id"])
    assert job["status"] == "done"
    assert job["ergebnis"].startswith("Erledigt:")
    assert job["kosten_usd"] == 0.02
    assert job["turns"] == 2
    assert job["session_id"] == "11111111-2222-3333-4444-555555555555"
    # Subagenten-Schritt darf nicht auftauchen, der Haupt-Schritt schon
    assert job["schritte"] == ["Read: README.md"]

    lauf = argv_zeilen(umgebung)[0]
    argv = lauf["argv"]
    assert argv[0] == "-p"
    assert argv[1] == "Bau mir eine Testdatei"  # Prompt direkt hinter -p
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    assert "Bash(rm *)" in argv and "Bash(git status *)" in argv
    assert lauf["cwd"].endswith("/spielwiese")


def test_fehlerhafter_lauf_wird_als_failed_gemeldet(client):
    angelegt = client.post("/jobs", json={"prompt": "FEHLER bitte", "projekt": "spielwiese"}).json()
    job = warte_auf_abschluss(client, angelegt["job_id"])
    assert job["status"] == "failed"
    assert "Fehlgeschlagen" in (job["fehler"] or "")


def test_permission_mode_wird_gedeckelt(client, umgebung):
    """kierp steht auf 'plan' — ein Request darf sich nicht hochstufen."""
    angelegt = client.post(
        "/jobs",
        json={"prompt": "mach was", "projekt": "kierp", "permission_mode": "bypassPermissions"},
    ).json()
    job = warte_auf_abschluss(client, angelegt["job_id"])
    assert job["permission_mode"] == "plan"
    argv = argv_zeilen(umgebung)[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_projekt_wird_aus_dem_text_erraten(client):
    angelegt = client.post("/jobs", json={"prompt": "In KIERP bitte aufraeumen"}).json()
    assert angelegt["projekt"] == "kierp"


def test_unbekanntes_projekt(client):
    antwort = client.post("/jobs", json={"prompt": "hallo", "projekt": "geheim"})
    assert antwort.status_code == 400


def test_zu_langer_prompt(client):
    antwort = client.post("/jobs", json={"prompt": "a" * 9000, "projekt": "spielwiese"})
    assert antwort.status_code == 413


def test_leerer_prompt(client):
    antwort = client.post("/jobs", json={"prompt": "   ", "projekt": "spielwiese"})
    assert antwort.status_code == 422


def test_konversation_wird_fortgesetzt(client, umgebung):
    schluessel = "slack:C123:1699.42"
    erster = client.post(
        "/jobs",
        json={"prompt": "Schritt eins", "projekt": "spielwiese", "conversation_key": schluessel},
    ).json()
    warte_auf_abschluss(client, erster["job_id"])

    zweiter = client.post(
        "/jobs",
        json={
            "prompt": "Und jetzt weiter",
            "projekt": "spielwiese",
            "conversation_key": schluessel,
        },
    ).json()
    assert zweiter["fortgesetzt"] is True
    warte_auf_abschluss(client, zweiter["job_id"])

    lauf = argv_zeilen(umgebung)[1]["argv"]
    assert lauf[lauf.index("--resume") + 1] == "11111111-2222-3333-4444-555555555555"


def test_konversation_zuruecksetzen(client, umgebung):
    schluessel = "slack:C123:neu"
    erster = client.post(
        "/jobs", json={"prompt": "eins", "projekt": "spielwiese", "conversation_key": schluessel}
    ).json()
    warte_auf_abschluss(client, erster["job_id"])

    assert client.delete(f"/konversationen/{schluessel}").json()["geloescht"] is True

    zweiter = client.post(
        "/jobs", json={"prompt": "zwei", "projekt": "spielwiese", "conversation_key": schluessel}
    ).json()
    assert zweiter["fortgesetzt"] is False
    warte_auf_abschluss(client, zweiter["job_id"])
    assert "--resume" not in argv_zeilen(umgebung)[1]["argv"]


def test_neu_starten_ignoriert_gemerkte_session(client, umgebung):
    schluessel = "slack:C123:reset"
    erster = client.post(
        "/jobs", json={"prompt": "eins", "projekt": "spielwiese", "conversation_key": schluessel}
    ).json()
    warte_auf_abschluss(client, erster["job_id"])

    zweiter = client.post(
        "/jobs",
        json={
            "prompt": "von vorne",
            "projekt": "spielwiese",
            "conversation_key": schluessel,
            "neu_starten": True,
        },
    ).json()
    assert zweiter["fortgesetzt"] is False


def test_job_log_abrufbar(client):
    angelegt = client.post("/jobs", json={"prompt": "hallo", "projekt": "spielwiese"}).json()
    warte_auf_abschluss(client, angelegt["job_id"])
    ereignisse = client.get(f"/jobs/{angelegt['job_id']}/log").json()["ereignisse"]
    assert ereignisse[0]["subtype"] == "init"
    assert ereignisse[-1]["type"] == "result"


def test_unbekannter_job(client):
    assert client.get("/jobs/gibtsnicht").status_code == 404
    assert client.post("/jobs/gibtsnicht/abbrechen").status_code == 404


def test_jobliste(client):
    client.post("/jobs", json={"prompt": "eins", "projekt": "spielwiese"})
    liste = client.get("/jobs?limit=5").json()
    assert liste and liste[0]["projekt"] == "spielwiese"


def test_callback_nutzlast_passt_zu_n8n(client, umgebung):
    """Der 'finished'-Callback muss genau die Felder liefern, die Workflow 2 liest."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    empfangen: list[dict] = []

    class Empfaenger(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            laenge = int(self.headers.get("Content-Length", 0))
            koerper = self.rfile.read(laenge)
            empfangen.append(
                {"json": json.loads(koerper), "signatur": self.headers.get("X-Bridge-Signature")}
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Empfaenger)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/webhook/claude-callback"

    angelegt = client.post(
        "/jobs",
        json={
            "prompt": "Bau was",
            "projekt": "spielwiese",
            "callback_url": url,
            "conversation_key": "slack:C1:99",
            "meta": {"kanal": "C1", "thread_ts": "99", "status_ts": "100"},
        },
    ).json()
    warte_auf_abschluss(client, angelegt["job_id"])

    ende = time.monotonic() + 5
    while time.monotonic() < ende and not any(e["json"]["event"] == "finished" for e in empfangen):
        time.sleep(0.05)
    server.shutdown()

    ereignisse = [e["json"] for e in empfangen]
    assert ereignisse[0]["event"] == "started"

    fertig = next(e for e in ereignisse if e["event"] == "finished")
    # Genau diese Felder wertet n8n/02_bridge-callback-slack.json aus:
    for feld in ("ok", "status", "projekt", "ergebnis", "schritte", "kosten_usd",
                 "dauer_ms", "turns", "git", "meta", "session_id", "job_id"):
        assert feld in fertig, f"Feld '{feld}' fehlt im Callback"
    assert fertig["ok"] is True
    assert fertig["meta"]["kanal"] == "C1"
    assert fertig["meta"]["status_ts"] == "100"
    assert fertig["ergebnis"].startswith("Erledigt:")
    assert all(e["signatur"].startswith("sha256=") for e in empfangen)

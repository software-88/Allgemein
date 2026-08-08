# Sprachagent: Slack → n8n → Claude Code auf der Synology

Ein geschlossener Kreislauf auf der eigenen NAS: Du sprichst eine Nachricht in
Slack (oder Telegram), Claude Code baut daraufhin in einem freigegebenen
Projektverzeichnis auf der Synology, und du bekommst das Ergebnis als Antwort
im selben Thread — auf Wunsch vorgelesen.

```
  Slack-Sprachnachricht
          │
          ▼
   ┌─────────────┐   Audio    ┌──────────────┐
   │     n8n     │──────────► │ Spracherken- │
   │ (Synology)  │ ◄──────────│ nung (STT)   │
   └──────┬──────┘  Transkript└──────────────┘
          │  POST /jobs
          ▼
   ┌──────────────────────────────────────────┐
   │  Claude-Bridge  (Docker, nur intern)     │
   │  claude -p … im Projektverzeichnis       │
   │  /volume1/docker/claude/workspace/<proj> │
   └──────┬───────────────────────────────────┘
          │  Callback (signiert): Fortschritt + Ergebnis
          ▼
   ┌─────────────┐
   │     n8n     │──► Slack-Antwort im Thread (+ optional Sprachausgabe)
   └─────────────┘
```

Der Kern ist die **Claude-Bridge**: ein kleiner Dienst, der Claude Code
headless (`claude -p`) startet, den Fortschritt mitliest und Ergebnisse
zurueckmeldet. n8n macht nur noch das, was n8n gut kann: Nachrichten
entgegennehmen, Audio durch die Spracherkennung schicken, antworten.

## Zwei Betriebsarten

|  | **Lokal** (Telegram-Abruf) | **Slack** |
| --- | --- | --- |
| Eingehende Ports | **keine** | n8n muss oeffentlich per HTTPS erreichbar sein |
| Warum | n8n fragt bei Telegram selbst nach (`getUpdates`) | Slack stellt Ereignisse per Push zu |
| Zusaetzlich noetig | nichts | Reverse-Proxy oder Tunnel, Zertifikat, Slack-App |
| Workflows | `05` + `04` | `01` + `02` |
| Anleitung | **[docs/LOKAL.md](docs/LOKAL.md)** | [docs/EINRICHTUNG.md](docs/EINRICHTUNG.md) |

Die Bridge ist in beiden Faellen nur im Docker-Netz erreichbar — nie aus dem
LAN und nie aus dem Internet. Wer maximal geschlossen fahren will, nimmt die
lokale Betriebsart und schaltet die Spracherkennung auf das Profil
`lokal-stt`; dann verlaesst nur noch der eigentliche Denkschritt das Haus
(Anthropic-API). Was genau wohin geht, steht in [docs/LOKAL.md](docs/LOKAL.md).

## Was drin ist

| Verzeichnis | Inhalt |
| --- | --- |
| `bridge/` | Die Claude-Bridge: FastAPI + Claude-CLI in einem Docker-Image, inkl. Tests |
| `bridge/config/` | `projects.yaml` (welche Projekte, welche Rechte) und der Basis-Systemprompt |
| `n8n/` | Fuenf importierbare Workflows (Slack rein/raus, Telegram per Webhook oder Abruf, Antwortweg) |
| `slack/manifest.yml` | Slack-App zum Einlesen statt Klicken |
| `docker-compose.yml` | n8n + Bridge (+ optional lokale Spracherkennung, Cloudflare-Tunnel) |
| `docs/` | [Lokal](docs/LOKAL.md) · [Einrichtung](docs/EINRICHTUNG.md) · [Sicherheit](docs/SICHERHEIT.md) · [Betrieb](docs/BETRIEB.md) |

## Was es kann

- **Sprache rein.** Slack-Audioclip oder Telegram-Sprachnachricht; Transkription
  wahlweise ueber OpenAI oder lokal (`speaches`/faster-whisper, Profil
  `lokal-stt`) — dann verlaesst kein Ton die NAS.
- **Gespraech statt Einzelbefehl.** Jeder Slack-Thread ist eine
  Claude-Code-Sitzung. „Und jetzt noch die Tests dazu" macht dort weiter, wo
  der letzte Auftrag aufgehoert hat. „Fang neu an" setzt zurueck.
- **Projekt per Zuruf.** „Bau in KIERP …" trifft das richtige Verzeichnis; die
  Zuordnung steht als Alias-Liste in `projects.yaml`. Alternativ fest pro
  Slack-Kanal.
- **Zwischenstand.** Die Statusmeldung in Slack wird waehrend der Arbeit
  aktualisiert („Edit: api/app/main.py"), am Ende ersetzt durch Ergebnis,
  Git-Stand und Kosten.
- **„Nur planen".** Wer „mach erstmal einen Plan" sagt, bekommt einen Lauf im
  Plan-Modus — Claude schaut, aendert aber nichts.
- **Leitplanken.** Pro Projekt: erlaubte Verzeichnisse, Permission-Modus,
  erlaubte und verbotene Shell-Kommandos, Turn-Limit, Kostendeckel,
  Zeitlimit. Details in [docs/SICHERHEIT.md](docs/SICHERHEIT.md).

## Voraussetzungen

- Synology mit DSM 7.2+ und **Container Manager** (x86-64; auf ARM-Modellen
  laeuft das Image nicht)
- ~2 GB RAM frei fuer n8n + Bridge, mehr bei lokaler Spracherkennung
- Ein Claude-Zugang: Abo-Token (`claude setup-token`) **oder** API-Schluessel
- Optional ein OpenAI-Schluessel fuer Spracherkennung/Sprachausgabe
- **Nur fuer die Slack-Betriebsart:** eine von aussen erreichbare
  HTTPS-Adresse fuer n8n (DSM-Reverse-Proxy mit Let's-Encrypt-Zertifikat oder
  der mitgelieferte Cloudflare-Tunnel). Im lokalen Betrieb entfaellt das.

## Schnellstart

```bash
# auf der NAS, per SSH
cd /volume1/docker
git clone <dieses-repo> claude-agent && cd claude-agent/n8n-voice-agent

cp .env.example .env && vi .env            # Tokens eintragen
mkdir -p /volume1/docker/claude/{workspace,config,jobs,home}
cp bridge/config/projects.example.yaml /volume1/docker/claude/config/projects.yaml
cp bridge/config/systemprompt.de.md     /volume1/docker/claude/config/
chown -R "$PUID:$PGID" /volume1/docker/claude

docker compose up -d --build
docker compose logs -f claude-bridge      # "[bridge] bereit — Projekte: …"
```

Danach die Workflows in n8n importieren:

- **lokal, ohne offenen Port** → **[docs/LOKAL.md](docs/LOKAL.md)**
- **mit Slack** → [docs/EINRICHTUNG.md](docs/EINRICHTUNG.md) (Schritt 6 an)

## Entwicklung an der Bridge

```bash
cd bridge
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q      # 37 Tests, ohne Netz und ohne API-Kosten
.venv/bin/python -m ruff check .
```

Die Tests fahren die komplette Kette (HTTP → Subprozess → stream-json → Job)
gegen ein gefaelschtes `claude`-Binary, inklusive der Leitplanken: Ausbruch aus
dem Workspace, Hochstufen des Permission-Modus, Sitzungsfortsetzung.

## Grenzen, ehrlich benannt

- Ein Synology-Prozessor ist langsam. Die Rechenarbeit macht die Claude-API,
  aber Tests, Builds und `npm install` laufen auf der NAS — plane bei groesseren
  Auftraegen Minuten ein, nicht Sekunden.
- Lokale Spracherkennung auf einer NAS ohne GPU ist mit `small` brauchbar und
  mit `large-v3` sehr langsam. Wer Tempo will, nimmt die Cloud-Variante.
- Der Agent pusht nichts von selbst. Das ist Absicht — siehe
  [docs/SICHERHEIT.md](docs/SICHERHEIT.md).
- Die Sprachausgabe in Slack ist standardmaessig aus (der Datei-Upload haengt
  an der Slack-App-Version). Bei Telegram ist sie an und funktioniert direkt.

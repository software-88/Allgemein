# Betrieb, Bedienung, Fehlersuche

## Sprechen mit dem Agenten

Direktnachricht an den Bot oder `@Bau-Agent …` in einem Kanal. Antworten
landen im selben Thread; **ein Thread = eine Claude-Sitzung**.

| Was du sagst | Was passiert |
| --- | --- |
| „In KIERP die Auftragsliste um eine Spalte Kunde erweitern" | Projekt `kierp` (Alias-Treffer), Auftrag laeuft |
| „Und jetzt noch die Tests dazu" (im selben Thread) | Sitzung wird fortgesetzt, Kontext bleibt |
| „Fang neu an: …" / „vergiss das" | Sitzung wird verworfen, frischer Start |
| „Mach erstmal nur einen Plan" | Lauf im Plan-Modus, keine Aenderungen |

Sprachnachrichten gehen genauso — Slack: Anhang → Audioclip. Was erkannt wurde,
steht im „🎧 Verstanden: …". Stimmt es nicht, direkt „vergiss das" hinterher.

**Formuliere wie fuer einen Kollegen am Telefon**, nicht wie eine
Kommandozeile: Ziel nennen, Ort nennen, Abschluss nennen („… und committen").

## Was in Slack zurueckkommt

Eine Nachricht, die sich dreimal veraendert:

1. `🎧 Verstanden: …` — direkt nach dem Diktat
2. `⚙️ Edit: api/app/main.py  (Schritt 7)` — alle ~25 s
3. Ergebnis: Antworttext, letzte Schritte, Git-Stand, Kosten und Dauer

## Nachsehen, was passiert ist

Alles liegt im Klartext auf der NAS unter `/volume1/docker/claude/jobs/`:

```bash
ls -t /volume1/docker/claude/jobs/*.json | head
cat /volume1/docker/claude/jobs/20260808-143012-ab12cd.json | jq
# Vollstaendiger Ereignisstrom des Laufs:
jq -c . /volume1/docker/claude/jobs/20260808-143012-ab12cd.ndjson | less
```

Ueber die API (im Container, `$BRIDGE_TOKEN` ist dort gesetzt):

```bash
docker exec claude-bridge sh -lc 'curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" localhost:8790/jobs?limit=20' | jq
docker exec claude-bridge sh -lc 'curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" localhost:8790/healthz' | jq
```

## Einen Auftrag abbrechen

```bash
docker exec claude-bridge sh -lc \
  'curl -s -X POST -H "X-Bridge-Token: $BRIDGE_TOKEN" localhost:8790/jobs/<job_id>/abbrechen'
```

Beendet die ganze Prozessgruppe, also auch laufende Tests oder Builds.

## Sitzung eines Threads zuruecksetzen

```bash
docker exec claude-bridge sh -lc \
  'curl -s -X DELETE -H "X-Bridge-Token: $BRIDGE_TOKEN" \
   localhost:8790/konversationen/slack:C01ABCDE:1699999999.123'
```

Im Alltag reicht „fang neu an" im Thread.

## Fehlersuche

**Slack meldet die Request-URL als nicht verifiziert.**
Workflow 1 ist nicht aktiv (die Test-URL `/webhook-test/` antwortet nur einmal),
oder n8n ist von aussen nicht erreichbar. `curl -i https://<host>/webhook/slack-events`
sollte nicht 404 liefern.

**In Slack passiert nichts.**
n8n → Workflow 1 → *Executions*. Keine Ausfuehrung = Slack stellt nicht zu
(Event Subscriptions, App neu installiert?). Ausfuehrung mit Ergebnis
`aktion: ignorieren` = die Nachricht wurde absichtlich verworfen; das Feld
`grund` sagt warum (oft `Nutzer … nicht freigegeben`).

**„Kein Rohkörper" im Code-Node.**
Im Webhook-Node unter *Options* die Einstellung **Raw Body** aktivieren.

**„Slack-Signatur ungültig".**
`SLACK_SIGNING_SECRET` passt nicht zur App, oder die App wurde neu erstellt.
Wert aus *Basic Information* neu uebernehmen, `docker compose up -d n8n`.

**Die Bridge antwortet 401.**
Die n8n-Credential `Claude Bridge Token` (Header Auth) muss `X-Bridge-Token`
als Namen und exakt den `BRIDGE_TOKEN` aus der `.env` als Wert haben.

**Der Job schlaegt sofort fehl mit Anmeldefehler.**
`docker exec claude-bridge claude -p "hallo" --output-format json` ausfuehren.
Meldet das einen Authentifizierungsfehler, ist `CLAUDE_CODE_OAUTH_TOKEN` bzw.
`ANTHROPIC_API_KEY` falsch oder abgelaufen. Der Token aus `claude setup-token`
haelt nicht ewig — dann neu erzeugen.

**Der Job endet mit „Turn-Limit erreicht".**
Der Auftrag war zu gross oder zu vage. Kleiner schneiden oder `max_turns` im
Projekt erhoehen.

**Der Agent aendert nichts, obwohl er soll.**
Steht das Projekt auf `permission_mode: plan`? Oder enthielt das Diktat
„nur planen"? Beides fuehrt zu einem reinen Lesevorgang.

**Der Agent bricht mit fehlender Berechtigung ab.**
In `acceptEdits` darf er Dateien schreiben, aber Shell-Kommandos nur laut
`allowed_tools`. Das benoetigte Kommando dort ergaenzen (`Bash(npm run build *)`)
und die Bridge neu starten.

**Konfiguration geaendert, nichts passiert.**
`projects.yaml` und `systemprompt.de.md` werden beim Start gelesen:
`docker compose restart claude-bridge`.

## Wartung

```bash
# Claude-CLI und n8n aktualisieren (Bridge neu bauen holt die neueste CLI):
docker compose pull
docker compose up -d --build

# Platz pruefen — Job-Protokolle werden automatisch auf die letzten 200
# begrenzt, der Whisper-Cache nicht:
du -sh /volume1/docker/claude/*
```

Sichern solltest du: `/volume1/docker/n8n` (Workflows und Credentials),
`/volume1/docker/claude/config` und natuerlich die `.env` — ohne
`N8N_ENCRYPTION_KEY` sind die gesicherten Credentials wertlos.

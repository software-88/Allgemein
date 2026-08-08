# Einrichtung Schritt fuer Schritt

Reihenfolge einhalten — Slack prueft die Webhook-Adresse sofort, deshalb muss
n8n vorher laufen und erreichbar sein.

---

## 1. Ordner auf der NAS anlegen

Per SSH auf die Synology (DSM → Systemsteuerung → Terminal & SNMP → SSH
aktivieren).

```bash
sudo -i
mkdir -p /volume1/docker/claude/{workspace,config,jobs,home}
mkdir -p /volume1/docker/n8n
```

Herausfinden, wem die Dateien gehoeren sollen (dein DSM-Benutzer):

```bash
id deinbenutzer
# uid=1026(deinbenutzer) gid=100(users) …   → PUID=1026, PGID=100
chown -R 1026:100 /volume1/docker/claude /volume1/docker/n8n
```

Diese Werte gleich in die `.env` uebernehmen — stimmen sie nicht, kann die
Bridge im Container nichts schreiben.

## 2. Projekt holen und `.env` ausfuellen

```bash
cd /volume1/docker
git clone <dieses-repo> claude-agent
cd claude-agent/n8n-voice-agent
cp .env.example .env
```

Zwei Geheimnisse erzeugen und in `.env` eintragen:

```bash
openssl rand -hex 32     # -> N8N_ENCRYPTION_KEY   (danach nie mehr aendern!)
openssl rand -hex 32     # -> BRIDGE_TOKEN
```

`N8N_HOST` / `N8N_PUBLIC_URL` auf die Adresse setzen, unter der n8n spaeter von
aussen erreichbar ist (siehe Schritt 5).

## 3. Claude-Zugang hinterlegen

**Variante a — ueber das Claude-Abo (empfohlen, keine API-Rechnung):**
auf einem Rechner, auf dem Claude Code laeuft und angemeldet ist:

```bash
claude setup-token
```

Den ausgegebenen Token als `CLAUDE_CODE_OAUTH_TOKEN` in die `.env` eintragen.

**Variante b — ueber die API:** Schluessel in der
[Claude Console](https://platform.claude.com) erzeugen und als
`ANTHROPIC_API_KEY` eintragen.

Nur eine der beiden Zeilen fuellen.

## 4. Projekte freigeben

Die Bridge arbeitet ausschliesslich in Verzeichnissen, die in `projects.yaml`
stehen. Vorlage kopieren und anpassen:

```bash
cp bridge/config/projects.example.yaml /volume1/docker/claude/config/projects.yaml
cp bridge/config/systemprompt.de.md    /volume1/docker/claude/config/
vi /volume1/docker/claude/config/projects.yaml
```

Danach die Repos dorthin legen, wo die Datei sie erwartet:

```bash
cd /volume1/docker/claude/workspace
git clone <dein-kierp-repo> kierp
mkdir -p spielwiese && echo "# Spielwiese" > spielwiese/README.md
chown -R 1026:100 /volume1/docker/claude
```

Fuer den ersten Test reicht `spielwiese`. Was die Felder in `projects.yaml`
bedeuten, steht in [SICHERHEIT.md](SICHERHEIT.md).

## 5. Stack starten

Entweder im **Container Manager** (Projekt anlegen → Pfad
`/volume1/docker/claude-agent/n8n-voice-agent` → `docker-compose.yml` auswaehlen
→ Erstellen) oder per SSH:

```bash
cd /volume1/docker/claude-agent/n8n-voice-agent
docker compose up -d --build     # der erste Build dauert ein paar Minuten
docker compose logs -f claude-bridge
```

Erwartete Zeile: `[bridge] bereit — Projekte: kierp, spielwiese (Standard: kierp)`.

Kurztest, dass Claude im Container antwortet und die Bridge erreichbar ist:

```bash
docker exec claude-bridge claude -p "Sag kurz Hallo" --output-format json
docker exec n8n wget -qO- http://claude-bridge:8790/healthz
```

Und ein echter Auftrag, ganz ohne Slack:

```bash
docker exec claude-bridge sh -lc '
curl -s -X POST localhost:8790/jobs \
  -H "X-Bridge-Token: $BRIDGE_TOKEN" -H "Content-Type: application/json" \
  -d "{\"prompt\":\"Lege eine Datei hallo.txt mit einem Gruss an\",\"projekt\":\"spielwiese\"}"'

# Ergebnis abholen (job_id aus der Antwort oben):
docker exec claude-bridge sh -lc '
curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" localhost:8790/jobs/<job_id>' | jq
```

Laeuft das, ist der schwierige Teil erledigt — der Rest ist Verkabelung.

## 6. n8n von aussen erreichbar machen

Slack stellt Ereignisse nur an oeffentliche HTTPS-Adressen zu.

**Variante a — DSM-Reverse-Proxy** (wenn du eine eigene Domain und ein
Let's-Encrypt-Zertifikat auf der NAS hast):
Systemsteuerung → Anmeldeportal → Erweitert → Reverse Proxy → Erstellen

| Feld | Wert |
| --- | --- |
| Quelle | HTTPS, `n8n.meine-domain.de`, Port 443 |
| Ziel | HTTP, `localhost`, Port 5678 |

Unter „Benutzerdefinierte Kopfzeile" die WebSocket-Vorlage hinzufuegen, sonst
bleibt der n8n-Editor leer.

**Variante b — Cloudflare-Tunnel** (kein offener Port, keine Portweiterleitung):
Tunnel in Cloudflare Zero Trust anlegen, Token in `.env` eintragen, dann

```bash
docker compose --profile tunnel up -d
```

Im Tunnel als Public Hostname `http://n8n:5678` eintragen.

Zum Schluss pruefen: `https://n8n.meine-domain.de` zeigt den n8n-Login.

## 7. Slack-App anlegen

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** →
   **From a manifest** → Workspace waehlen.
2. Inhalt von [`slack/manifest.yml`](../slack/manifest.yml) einfuegen. Vorher
   `request_url` auf `https://<deine-n8n-adresse>/webhook/slack-events` aendern.
   Slack prueft die Adresse sofort — **das schlaegt jetzt noch fehl**, das ist
   in Ordnung. Setze `event_subscriptions` zunaechst auf eine beliebige gueltige
   URL oder trage die echte erst in Schritt 10 ein.
3. **Install to Workspace** → Zugriff bestaetigen.
4. Zwei Werte notieren:
   - *OAuth & Permissions* → **Bot User OAuth Token** (`xoxb-…`)
   - *Basic Information* → **Signing Secret**
5. Signing Secret in die `.env` (`SLACK_SIGNING_SECRET=…`) und n8n neu starten:
   ```bash
   docker compose up -d n8n
   ```

## 8. Zugaenge in n8n hinterlegen

In n8n unter **Credentials → Add credential**. Die Namen muessen exakt so
lauten, sonst finden die importierten Workflows sie nicht:

| Name | Typ | Inhalt |
| --- | --- | --- |
| `Slack Bot Token` | Slack API | Access Token = `xoxb-…` |
| `Claude Bridge Token` | Header Auth | Name `X-Bridge-Token`, Wert = `BRIDGE_TOKEN` aus der `.env` |
| `OpenAI API` | OpenAI | API-Schluessel (entfaellt bei lokaler Spracherkennung) |

## 9. Workflows importieren

n8n → **Workflows → Import from File**, nacheinander:

- `n8n/01_slack-sprache-zu-claude.json`
- `n8n/02_bridge-callback-slack.json`

In Workflow 1 den Node **„Slack prüfen"** oeffnen und den Block `KONFIG` oben
anpassen:

```js
bridgeUrl:   'http://claude-bridge:8790',            // so lassen
callbackUrl: 'http://n8n:5678/webhook/claude-callback',  // so lassen
sttModell:   'gpt-4o-transcribe',
erlaubteNutzer: ['U01ABCDEFG'],   // deine Slack-User-ID — bitte eintragen!
```

Die eigene User-ID findest du in Slack ueber Profil → ⋮ → „Mitglieds-ID kopieren".

Beide Workflows **aktivieren** (Schalter oben rechts). Erst dann existiert die
produktive Webhook-Adresse.

## 10. Slack-Ereignisse verbinden

Slack-App → **Event Subscriptions** → Request URL eintragen:

```
https://<deine-n8n-adresse>/webhook/slack-events
```

Slack muss „Verified" anzeigen. Falls nicht:

- Ist Workflow 1 aktiv? (Test-URL `/webhook-test/` antwortet nur einmal nach
  Klick auf „Test workflow".)
- Kommt die Anfrage an? n8n → Workflow 1 → **Executions**.

Danach unter **Subscribe to bot events** pruefen, dass `message.im` und
`app_mention` eingetragen sind, und **Save Changes** → ggf. App neu
installieren.

## 11. Erster Sprachtest

Schreib dem Bot eine Direktnachricht in Slack:

> „Leg in der Spielwiese eine Datei notizen.md an und schreib das heutige Datum rein."

Erst als Text, dann als Sprachnachricht (Anhang → Audioclip aufnehmen).
Erwarteter Ablauf: „🎧 Verstanden: …" → Zwischenmeldungen („Write: notizen.md")
→ Ergebnis mit Git-Stand und Kosten.

Antwortet nichts, hilft die Fehlersuche in [BETRIEB.md](BETRIEB.md).

---

## Optional: Telegram statt (oder neben) Slack

Telegram ist fuers Diktieren unterwegs oft angenehmer und kann die Antwort
direkt als Sprachnachricht zurueckschicken.

1. In Telegram bei `@BotFather` mit `/newbot` einen Bot anlegen, Token notieren.
2. In n8n eine Credential **`Telegram Bot`** (Typ: Telegram) mit dem Token anlegen.
3. `n8n/03_telegram-sprache-zu-claude.json` und
   `n8n/04_bridge-callback-telegram.json` importieren.
4. Im Node **„Telegram prüfen"** die eigene Chat-ID in `erlaubteChats`
   eintragen (der Workflow verweigert den Dienst, solange die Liste leer ist —
   sonst koennte jeder, der den Botnamen kennt, auf deiner NAS bauen).
   Die eigene ID nennt dir `@userinfobot`.
5. Beide Workflows aktivieren.

## Optional: Spracherkennung lokal (kein Ton verlaesst die NAS)

```bash
docker compose --profile lokal-stt up -d
```

Dann in Workflow 1 im Node **„Transkribieren"**:

- URL auf `http://speaches:8000/v1/audio/transcriptions`
- *Authentication* auf **None**
- in `KONFIG.sttModell` z. B. `Systran/faster-whisper-small`

Der erste Lauf laedt das Modell herunter und dauert. Auf NAS-Prozessoren ohne
GPU ist `small` der brauchbare Kompromiss; `large-v3` ist genauer, aber fuer
Zurufe im Alltag zu langsam.

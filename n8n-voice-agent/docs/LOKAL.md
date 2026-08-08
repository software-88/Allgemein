# Rein lokal betreiben — ohne offenen Port

## Was ueberhaupt nach aussen zeigt

Der Agent selbst war nie von aussen erreichbar. Die Bridge hat bewusst keinen
`ports:`-Eintrag; sie existiert nur im Docker-Netz der NAS. Auch der Rueckweg
(Bridge → n8n) laeuft ueber `http://n8n:5678/…`, also containerintern.

Offen war genau eine Tuer, und die aus einem einzigen Grund: **Slack stellt
Ereignisse per Push zu.** Slack braucht eine oeffentliche HTTPS-Adresse. Das
ist eine Eigenschaft der Chat-Plattform, nicht des Agenten — bei
Telegram-*Webhooks* waere es dasselbe.

## Die Loesung: abholen statt zustellen lassen

Telegram kann auch andersherum: n8n fragt alle zehn Sekunden selbst nach, ob
etwas da ist (`getUpdates`). Nur ausgehende Verbindungen, kein Reverse-Proxy,
keine Portweiterleitung, kein Zertifikat, kein Zusatzcontainer.

Dafuer gibt es **`n8n/05_telegram-abruf-lokal.json`**.

```
  Telegram-App                    NAS
       │                    ┌──────────────────┐
       │  ◄──── alle 10 s ──│ n8n: getUpdates  │   nur ausgehend
       │                    │        ↓         │
       │                    │  Spracherkennung │
       │                    │        ↓         │
       │                    │  Claude-Bridge   │   nur im Docker-Netz
       │  ◄──── Antwort ────│                  │
       └────────────────────└──────────────────┘
```

### Einrichten

1. Schritte 1–5 aus [EINRICHTUNG.md](EINRICHTUNG.md) wie beschrieben
   (Ordner, `.env`, Claude-Zugang, `projects.yaml`, Stack starten).
   **Schritt 6 und 7 entfallen** — kein Reverse-Proxy, keine Slack-App.
   `N8N_HOST`, `N8N_PROTOCOL` und `N8N_PUBLIC_URL` bleiben leer.
2. In Telegram bei `@BotFather` mit `/newbot` einen Bot anlegen. Den Token in
   die `.env` als `TELEGRAM_BOT_TOKEN=…` eintragen, dann `docker compose up -d n8n`.
3. Eigene Chat-ID herausfinden: `@userinfobot` anschreiben.
4. In n8n eine Credential **`Telegram Bot`** (Typ: Telegram) mit demselben
   Token anlegen — die brauchen die Sende-Nodes.
   Fuer die Spracherkennung ausserdem **`OpenAI API`**, fuer die Bridge
   **`Claude Bridge Token`** (Header Auth, `X-Bridge-Token`) wie in
   [EINRICHTUNG.md](EINRICHTUNG.md) Schritt 8.
5. Importieren: `n8n/05_telegram-abruf-lokal.json` und
   `n8n/04_bridge-callback-telegram.json`.
6. In Workflow 5 den Node **„Offset lesen"** oeffnen und in `KONFIG` die
   eigene Chat-ID unter `erlaubteChats` eintragen. Ohne Eintrag verweigert der
   Workflow den Dienst — sonst koennte jeder, der den Botnamen kennt, auf
   deiner NAS bauen.
7. Beide Workflows **aktivieren**.

Ab jetzt: dem Bot eine Sprachnachricht schicken. Antwort kommt als Text und
als Sprachnachricht zurueck.

### Zwei Stolpersteine

**Der Abrufzeiger wird nur bei aktivem Workflow gespeichert.** n8n sichert die
Workflow-Daten nach jeder produktiven Ausfuehrung; bei „Test workflow" nicht.
Nach dem Testen also aktivieren, sonst kommt dieselbe Nachricht wiederholt an.

**Webhook und Abruf schliessen sich aus.** Wenn du vorher Workflow 3 (den
Telegram-Webhook) aktiv hattest, hat n8n bei Telegram einen Webhook
registriert. Solange der steht, liefert `getUpdates` einen Fehler. Workflow 3
deaktivieren und einmalig aufrufen:

```
https://api.telegram.org/bot<DEIN_TOKEN>/deleteWebhook
```

Workflow 5 erkennt diesen Fall und schreibt genau diesen Hinweis ins
Ausfuehrungsprotokoll.

## Nachpruefen, dass wirklich nichts offen ist

```bash
# Welche Container veroeffentlichen ueberhaupt Ports?
docker compose ps --format 'table {{.Service}}\t{{.Ports}}'
# Erwartet: nur n8n mit 5678. claude-bridge: leer.

# Ist die Bridge wirklich nur intern erreichbar?
curl -m 3 http://<nas-ip>:8790/healthz     # muss scheitern
docker exec n8n wget -qO- http://claude-bridge:8790/healthz   # muss antworten
```

Im Router darf keine Portweiterleitung auf 5678 stehen, und in DSM kein
Reverse-Proxy-Eintrag auf n8n. Wer ganz sicher gehen will, setzt
`N8N_BIND=127.0.0.1` in der `.env` — dann kommt nur die NAS selbst an die
Oberflaeche, der Zugriff laeuft ueber einen SSH-Tunnel:

```bash
ssh -L 5678:127.0.0.1:5678 deinbenutzer@nas    # dann http://localhost:5678
```

## Was trotzdem das Haus verlaesst

Ehrlichkeit ist hier wichtiger als ein beruhigendes „alles lokal":

| Was | Wohin | Vermeidbar? |
| --- | --- | --- |
| Der Auftragstext und der gelesene Code | Anthropic-API — dort denkt Claude Code | Nein, nur mit einem lokalen Modell als Gehirn |
| Die Sprachnachricht | Telegram-Server (dort liegt sie ohnehin) | Nur durch Verzicht auf Telegram, siehe unten |
| Die Tonspur zur Erkennung | OpenAI | **Ja** — Profil `lokal-stt` |
| Der Antworttext zur Vertonung | OpenAI | Ja, dann eben ohne Sprachausgabe |

Spracherkennung auf die NAS holen:

```bash
docker compose --profile lokal-stt up -d
```

Dann in Workflow 5 im Node **„Transkribieren"** die URL auf
`http://speaches:8000/v1/audio/transcriptions` setzen, *Authentication* auf
**None**, und `KONFIG.sttModell` auf z. B. `Systran/faster-whisper-small`.
Damit verlaesst kein Ton mehr die NAS.

## Wenn es Slack sein soll

Slack kann dasselbe — ueber **Socket Mode**: die App baut eine ausgehende
WebSocket-Verbindung zu Slack auf, Slack schickt Ereignisse durch diesen
Kanal zurueck. Kein eingehender Port.

n8n kann das nicht von sich aus (der Slack-Trigger spricht nur Events-API per
Webhook). Es braeuchte einen kleinen Relay-Container: `slack_bolt` im Socket
Mode, der eingehende Ereignisse an `http://n8n:5678/webhook/slack-events`
weiterreicht — ungefaehr hundert Zeilen, plus ein App-Level-Token
(`xapp-…`) in der Slack-App. Sag Bescheid, wenn du das willst; die
bestehenden Slack-Workflows 1 und 2 blieben dabei unveraendert.

## Wenn gar kein fremder Dienst beteiligt sein soll

Dann faellt der Chat-Anbieter weg und es braucht eine eigene Oberflaeche: eine
kleine Web-Seite auf der NAS, die im Browser aufnimmt (MediaRecorder) und
direkt an n8n schickt. Im WLAN funktioniert das sofort; unterwegs brauchst du
ein VPN auf die NAS (Tailscale oder WireGuard — beides laeuft auf DSM).

Der Preis ist Bequemlichkeit: kein Verlauf auf allen Geraeten, keine
Push-Nachrichten, und ohne VPN keine Bedienung von unterwegs. Technisch ist es
der sauberste Weg, und mit lokaler Spracherkennung verlaesst dann wirklich nur
noch der Denkschritt das Haus.

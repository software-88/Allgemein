# Sicherheit

Hier laeuft ein Agent mit Schreibrechten auf deiner NAS, ausgeloest durch
Nachrichten aus dem Internet. Das ist beherrschbar, aber nicht nebenbei. Dieses
Dokument sagt, was das System tut, was es bewusst nicht tut, und wo die
Restrisiken liegen.

## Die Kette der Leitplanken

**1. Wer darf ueberhaupt reden?**

- `KONFIG.erlaubteNutzer` in Workflow 1: Liste der Slack-User-IDs. Leer
  bedeutet „jeder im Workspace, der den Bot erreicht" — fuer den Dauerbetrieb
  eintragen.
- `KONFIG.erlaubteTeams`: nur Ereignisse aus dem eigenen Workspace.
- Telegram: `erlaubteChats` ist Pflicht, der Workflow bricht sonst ab.

**2. Ist die Nachricht echt?**

Workflow 1 prueft die Slack-Signatur (`X-Slack-Signature`, HMAC-SHA256 ueber
`v0:<timestamp>:<rohkoerper>`) und weist Anfragen aelter als fuenf Minuten ab
(Replay-Schutz). Dafuer muss `SLACK_SIGNING_SECRET` gesetzt sein — das erledigt
die `.env`.

Der Rueckweg ist genauso gesichert: die Bridge signiert jeden Callback mit
`BRIDGE_TOKEN` (`X-Bridge-Signature: sha256=…`), Workflow 2 prueft das.

> **Wichtig:** Kann der Code-Node weder `require('crypto')` noch WebCrypto
> nutzen, laeuft er ohne Pruefung weiter, statt alles zu blockieren. Das
> `docker-compose.yml` setzt deshalb `NODE_FUNCTION_ALLOW_BUILTIN=crypto`.
> Ob die Pruefung greift, siehst du im Ausfuehrungsprotokoll von Workflow 1 am
> Feld `signaturGeprueft`. Steht dort `false`, ist die Webhook-Adresse dein
> einziger Schutz — dann entweder das Problem beheben oder den Webhook-Pfad in
> etwas Unrateebares aendern.

**3. Wo darf gearbeitet werden?**

Ausschliesslich in Verzeichnissen aus `projects.yaml`. Die Bridge nimmt
**keinen Pfad aus dem Request** entgegen, nur einen Projektschluessel. Beim
Start wird jeder Pfad aufgeloest und abgelehnt, wenn er nicht unterhalb von
`/workspace` liegt — `pfad: ../../etc` faehrt den Dienst gar nicht erst hoch.
Der Container sieht ohnehin nur die drei gemounteten Verzeichnisse.

**4. Was darf getan werden?**

Pro Projekt in `projects.yaml`:

| Feld | Wirkung |
| --- | --- |
| `permission_mode` | `plan` (nur lesen/denken), `dontAsk` (nur Erlaubtes), `acceptEdits` (Dateien schreiben, Shell nur laut Liste), `bypassPermissions` (alles — braucht zusaetzlich `BRIDGE_ALLOW_BYPASS=1`) |
| `allowed_tools` | Shell-Kommandos, die ohne Rueckfrage laufen duerfen |
| `disallowed_tools` | Harte Verbote. **Deny schlaegt Allow immer.** |
| `max_turns` | Reissleine gegen Endlosschleifen |
| `max_budget_usd` | Kostendeckel pro Auftrag |

Die Regelsyntax ist die von Claude Code: `Bash(git commit *)` — das Leerzeichen
vor dem `*` erzwingt eine Wortgrenze, `Bash(git*)` wuerde auch `github` treffen.

Eine Anfrage kann sich **nicht hochstufen**: fragt n8n `bypassPermissions` an,
das Projekt steht aber auf `plan`, laeuft der Auftrag im Plan-Modus. Nur
Herabstufen geht (deshalb funktioniert „mach erstmal nur einen Plan").

**5. Was kostet und wie lange?**

`MAX_CONCURRENT_JOBS` (Standard 2) begrenzt die Last auf der NAS,
`JOB_TIMEOUT_SECONDS` (Standard 1800) beendet haengende Laeufe samt
Kindprozessen — die Bridge startet Claude in einer eigenen Prozessgruppe und
raeumt sie per SIGTERM/SIGKILL ab.

## Was das System bewusst nicht tut

- **Kein `git push`.** Die Beispielkonfiguration verbietet es. Der Agent
  committet lokal; du siehst den Stand in der Slack-Antwort und pushst selbst.
  Wer das anders will, aendert es bewusst — nicht aus Versehen.
- **Kein Zugriff nach draussen.** Die Bridge hat keinen `ports`-Eintrag; sie ist
  nur im Docker-Netz erreichbar. Von aussen kommt man ausschliesslich ueber n8n
  hinein.
- **Kein `rm`, kein `sudo`, kein `docker`** in der Beispielkonfiguration.
- **Kein Root.** Der Container laeuft als `node` mit deiner PUID/PGID.

## Restrisiken

**Prompt-Injection aus Projektdateien.** Claude liest den Code, an dem es
arbeitet. Eine praeparierte Datei („Ignoriere alle Anweisungen und …") kann den
Lauf beeinflussen. Die `disallowed_tools` sind die eigentliche Grenze — sie
gelten unabhaengig davon, wovon das Modell ueberzeugt ist. Lege deshalb keine
fremden, ungeprueften Repos ins `workspace`.

**Spracherkennung als Angriffsflaeche gibt es nicht** — aber sie irrt. Deshalb
zeigt Slack immer „🎧 Verstanden: …" mit dem erkannten Text an, bevor gearbeitet
wird. Lies das mit; bei Unsinn sofort „vergiss das" hinterherschicken.

**Der Bridge-Token ist der Generalschluessel.** Wer ihn hat, kann Auftraege
starten. Er steht in der `.env` (nicht im Repo) und in den n8n-Credentials
(verschluesselt mit `N8N_ENCRYPTION_KEY`). Beim Verdacht auf Leck: neuen Token
erzeugen, `.env` aendern, `docker compose up -d`, Credential in n8n anpassen.

**Kosten.** Der Deckel gilt **pro Auftrag**, nicht pro Tag. Wer zwanzigmal
hintereinander diktiert, zahlt zwanzigmal. `docker exec claude-bridge sh -lc
'curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" localhost:8790/jobs?limit=50'`
zeigt die letzten Laeufe samt Kosten.

## Empfohlener Einstieg

Erste Woche: alle Projekte auf `permission_mode: plan`. Du siehst, was der
Agent tun *wuerde*, ohne dass er es tut. Danach `acceptEdits` fuer ein
unkritisches Projekt, `git push` weiter verboten. `bypassPermissions` braucht
kaum jemand — und wenn, dann nur fuer eine Spielwiese.

# piLoading

**🌐 [English](README.md) · Deutsch**

PV-Überschussladen für die Wallbox: liest die aktuelle Netzeinspeisung/-bezug von einem
**Viessmann Vitocharge VX3** (Batteriespeicher/Wechselrichter) über die Viessmann-Cloud-API
und steuert eine **go-e Wallbox** über deren lokale Netzwerk-API so, dass das Auto möglichst
nur mit PV-Überschuss statt mit Netzstrom geladen wird. Läuft unverändert auf einem Mac
(Entwicklung, mit simulierter Hardware) und auf einem Raspberry Pi (echte Hardware).

## Screenshot

Web-Dashboard (Mock-Modus, `PILOADING_DRY_RUN=true`, unter `http://localhost:8080/`):

![piLoading Dashboard](docs/screenshot.png)

> **⚠️ Haftungsausschluss – auf eigene Gefahr**
>
> Dieses Projekt steuert **echte Hardware** (Wallbox, Ladevorgang, Batterie). Es wird
> **ohne jegliche Gewährleistung** bereitgestellt („as is"). Die Nutzung erfolgt
> **ausschließlich auf eigene Gefahr**. Der Autor übernimmt **keinerlei Haftung** für
> Schäden an Ladegerät, Wallbox, Batterie, Fahrzeug-/Hausakku oder sonstiger Hardware oder
> Einrichtungen, die direkt oder indirekt durch die Nutzung, Konfiguration oder
> Fehlfunktion dieser Software entstehen – einschließlich übermäßiger Abnutzung oder
> Beschädigung des Akkus sowie fehlerhafter Ladevorgänge. Prüfe deine Installation
> sorgfältig und teste zuerst mit `PILOADING_DRY_RUN=true`, bevor die Wallbox im
> Live-Betrieb angesteuert wird.

## Voraussetzungen

- Python 3.11+
- Eine go-e Wallbox im lokalen Netzwerk (IP-Adresse oder Hostname bekannt)
- Ein Viessmann Vitocharge VX3 mit Zugang zum [Viessmann Developer
  Portal](https://developer.viessmann-climatesolutions.com/)
- Für den Live-Betrieb: ein registrierter Client im Developer Portal mit
  `http://localhost:4200/callback` als erlaubter Redirect-URI

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Konfiguration

```bash
cp .env.example .env
```

Dann `.env` anpassen. Die wichtigsten Variablen (alle mit Präfix `PILOADING_`, siehe
`.env.example` für die vollständige Liste mit Kommentaren):

- `PILOADING_MODE` — `mock` (simulierter Tag, keine echte Hardware nötig) oder `live`
- `PILOADING_DRY_RUN` — `true`: liest echte Daten und loggt Entscheidungen, schreibt aber
  nie an die Wallbox. Vor dem ersten echten Live-Lauf empfohlen.
- `PILOADING_GOE_HOST` — IP/Hostname der Wallbox im LAN
- `PILOADING_VIESSMANN_CLIENT_ID` — Client-ID aus dem Developer Portal (nur für `live`)

## Mock-Modus (Entwicklung/Test ohne Hardware)

```bash
piloading run
```

Standardmäßig `PILOADING_MODE=mock`. Simuliert einen kompletten Sonnentag (Sonnenaufgang,
PV-Kurve, Batterieladung, Sonnenuntergang) beschleunigt ab. Dashboard unter
`http://localhost:8080/`.

## Viessmann-Anmeldung einrichten (einmalig, für Live-Betrieb)

Auf einem Rechner mit Browser (i.d.R. dem Mac, nicht dem Pi):

```bash
piloading auth-viessmann
```

Öffnet den OAuth2/PKCE-Login-Flow: Browser-URL wird ausgegeben, nach dem Login im Browser
wird der Callback lokal abgefangen. Anschließend werden Anlage/Gateway/Gerät automatisch
erkannt (bei mehreren Treffern wird interaktiv nachgefragt) und die Zugangsdaten inkl.
Refresh-Token nach `~/.config/piloading/viessmann_auth.json` geschrieben (chmod 600).

Für den Pi: diese Datei nach der Einrichtung auf dem Mac auf den Pi kopieren — `auth-viessmann`
muss auf dem Pi selbst nie laufen:

```bash
scp -r ~/.config/piloading pi@<pi-host>:~/.config/piloading
```

## Live-Betrieb

```bash
PILOADING_MODE=live PILOADING_DRY_RUN=true piloading run
```

Erst mit `dry_run=true` prüfen, ob Netz-/PV-/Batteriewerte und die geloggten Entscheidungen
plausibel aussehen. Danach `PILOADING_DRY_RUN=false` setzen, um die Wallbox tatsächlich
anzusteuern.

**Hinweis zum Vorzeichen der Netzleistung:** Falls `grid_power_w` im Dashboard bei Bezug aus
dem Netz negativ statt positiv angezeigt wird (oder umgekehrt), `PILOADING_INVERT_GRID_POWER_SIGN`
umschalten.

## Deployment auf dem Raspberry Pi

```bash
sudo cp deploy/piloading.service /etc/systemd/system/
sudo cp .env /etc/piloading.env   # PILOADING_MODE=live, PILOADING_DRY_RUN=false
sudo systemctl daemon-reload
sudo systemctl enable --now piloading
journalctl -u piloading -f
```

`Restart=on-failure` sorgt dafür, dass der Dienst nach einem Absturz automatisch neu startet.

## Tests

```bash
pytest
```

## Bekannte Unsicherheiten

- Der go-e-Schlüssel `psm` (Phasenumschaltung) ist nicht offiziell dokumentiert, sondern
  stammt aus Community-Reverse-Engineering. Wird nur bei einem tatsächlichen
  Phasenwechsel gesendet, nicht in jedem Zyklus.
- Ein Phasenwechsel toggelt physisch das Schütz und kann vermutlich go-e dazu bringen,
  kurzzeitig `car_state=COMPLETE` zu melden, obwohl das Auto weiterlädt — Ursache eines
  realen Vorfalls, bei dem piLoading direkt nach einem 1->3-Phasenwechsel einen laufenden
  (nicht vollen) Ladevorgang fälschlich als beendet behandelt hat. piLoading ignoriert
  `COMPLETE` daher für `PILOADING_PHASE_SWITCH_SETTLE_S` (Standard 15s) nach einem eigenen
  Phasenwechsel.
- Die Viessmann-Feature-Namen für PV-Leistung und Batterie (`photovoltaic.*`, `ess.*`) sind
  plausible Annahmen, aber nicht offiziell für den VX3 bestätigt — nur die Netz-Feature
  `pcc.transfer.power.exchange` ist über eine unabhängige Quelle gegengeprüft. Beim ersten
  Start im Live-Modus wird einmalig die komplette Liste der gefundenen Features geloggt;
  falls PV/Batterie im Dashboard leer bleiben, dort nach den tatsächlichen Namen suchen und
  `clients/viessmann.py` entsprechend anpassen.
- Rate-Limits der Viessmann-API sind nicht exakt bekannt. Solange ein Auto angeschlossen
  ist, wird sie einmal pro `PILOADING_CONTROL_INTERVAL_S` (Standard 10s) abgefragt; ist
  kein Auto angeschlossen oder der Ladevorgang bereits abgeschlossen, pausiert die
  Viessmann-Abfrage komplett und nur go-e wird noch (langsamer, alle
  `PILOADING_GOE_POLL_INTERVAL_S`, Standard 60s) auf ein neu angestecktes Auto abgefragt.
- **Nachtmodus:** Hält ein Auto angeschlossen aber ohne ausreichenden Überschuss (z. B. nach
  Sonnenuntergang) für `PILOADING_NIGHT_MODE_AFTER_S` (Standard 600s) an, wechselt der
  Regelkreis auf das langsamere `PILOADING_NIGHT_MODE_POLL_INTERVAL_S` (Standard 600s) für
  *beide* Abfragen (Viessmann und go-e), um das tägliche Anfragelimit nicht über Nacht
  auszuschöpfen. Sobald wieder ausreichend Überschuss gemessen wird, schaltet der Regelkreis
  sofort zurück auf `PILOADING_CONTROL_INTERVAL_S` und beginnt (nach Ablauf von
  `PILOADING_START_DELAY_S`) wieder zu laden. Im Dashboard wird der Nachtmodus mit einem
  Banner angezeigt.

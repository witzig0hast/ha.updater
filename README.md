# HA-Fix

Dashboard, das dein Home Assistant **vollständig einliest**, daraus ein „Gehirn" aufbaut, die Logik deiner
Automationen prüft und dir über einen Chat **Änderungsvorschläge** macht – effizient, lokal und sicher.

* Läuft als **ein Docker-Container** (nur UI + Logik). Die KI (**Ollama**) läuft weiter **auf dem Host**.
* **Standardmäßig nur lesend**: Schreibsperre ist an, es wird nichts an Home Assistant verändert.
* **Vor jeder Änderung ein Backup** (und optional bei jedem Scan).

## Start

```bash
docker compose up -d --build
# UI: http://<server>:8099
```

Dann in **Einstellungen**: HA-URL + Long-Lived Access Token (HA-Profil → Sicherheit) eintragen, „Verbindung testen",
anschließend unter **Gehirn → „Gehirn aufbauen"**.

### Ollama auf dem Host erreichbar machen
Der Container spricht Ollama über `http://host.docker.internal:11434` an. Ollama muss dafür auf mehr als
`127.0.0.1` lauschen:
```bash
# systemd: sudo systemctl edit ollama
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```
(Port 11434 nur im LAN freigeben, Ollama hat keine Authentifizierung.)

### VRAM-Budget (~10 GB von 24 GB)
Ollama kennt kein hartes VRAM-Limit; das Budget wird über Modellgröße und Kontext eingehalten:

| Modell (Q4)        | Gewichte | + KV-Cache @ 4096 | Fazit |
|--------------------|---------:|------------------:|-------|
| `hermes3:3b`       | ~2 GB    | ~0,3 GB           | sehr schnell, einfache Vorschläge |
| `hermes3:8b`       | ~4,7 GB  | ~0,6 GB           | **Empfehlung**, deutlich unter 10 GB |

* `num_ctx` (Standard 4096) und `keep_alive` (Standard 2 Min.) sind in den Einstellungen änderbar.
* Das Modell wird nur geladen, wenn gerade gechattet oder geprüft wird, und danach wieder freigegeben.
* Die Analyse ist bewusst **zweistufig**, damit die GPU wenig tun muss: Erst eine regelbasierte Prüfung ohne LLM
  (Millisekunden), das LLM wird nur für die Review einzelner Automationen und den Chat benutzt – jeweils mit
  nur den *relevanten* Automationen/Entitäten im Kontext, nie mit dem ganzen System.

## Was das „Gehirn" enthält
Entitäten (inkl. Zustand, Bereich, Gerät, Integration), Geräte, Bereiche, alle Automationen/Skripte/Szenen mit
Rohkonfiguration und die Querverweise Trigger / Bedingung / Aktion → Entität.

Regelbasierte Befunde: fehlende Entitäten/Geräte, nicht verfügbare Entitäten, nie/lange nicht ausgelöst,
deaktiviert, kein Trigger/keine Aktion, Polling (`time_pattern`, `now()` in Templates), identische Duplikate,
mögliche Konflikte (gleicher Auslöser schaltet dasselbe Ziel an *und* aus), `delay` im Modus `single`,
ungenutzte Skripte, niedrige Batterien, veraltete Syntax.

## Ablauf bei Änderungen
1. Chat: beschreiben, was stört → KI erzeugt einen **Vorschlag** (Diff alt/neu, Warnungen bei unbekannten Entitäten).
2. **Zustimmen**, **Änderung vorschlagen** (KI überarbeitet) oder **Verwerfen**.
3. Zustimmen funktioniert nur, wenn die **Schreibsperre** in den Einstellungen aufgehoben wurde. Dann:
   frisches Backup → Schreiben über die HA-API → Neu-Scan. Aus einem Backup lässt sich einzeln zurückspielen
   (`POST /api/backups/{name}/restore/{kind}/{id}`).

Nur Automationen mit `id:` sind über die API lesbar/änderbar (UI-erstellte immer; YAML-Automationen ohne `id`
werden als „nur lesbar" markiert).

## Sicherheit
* Das HA-Token liegt im Datenvolume (`./data/hafix.db`) im Klartext – Verzeichnis schützen.
* Die UI hat standardmäßig keinen Login. Mit `HAFIX_PASSWORD` wird HTTP-Basic-Auth (Benutzer beliebig) aktiviert.
  Nicht ungeschützt ins Internet stellen.

## Entwicklung
```bash
pip install -r requirements.txt pytest
python -m pytest        # Ende-zu-Ende-Test gegen Fake-HA + Fake-Ollama
DATA_DIR=./data uvicorn app.main:app --port 8099
```

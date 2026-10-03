# HA-Fix

Liest dein Home Assistant komplett ein, prüft die Logik jeder Automation und lässt eine lokale KI (Ollama)
**Verbesserungen vorschlagen**. Du klickst nur noch *Übernehmen*, *Anpassen* oder *Verwerfen*.

* Ein Container, eine Seite. Ollama läuft weiter auf dem Host.
* Nichts wird ohne deine ausdrückliche Zustimmung geändert. Vor jeder Änderung entsteht ein Backup.

## Installieren

```bash
mkdir ha-fix && cd ha-fix
curl -O https://raw.githubusercontent.com/witzig0hast/ha.updater/main/docker-compose.yml
docker compose up -d          # Oberfläche: http://<server>:8099
```
(Das Image `ghcr.io/witzig0hast/ha.updater` baut GitHub automatisch. Falls das Paket noch privat ist: unter
GitHub → Packages → Package settings auf *public* stellen, oder lokal bauen mit `docker compose up -d --build`.)

Beim ersten Öffnen: Home-Assistant-Adresse und Token eintragen (HA-Profil → ganz unten *Long-Lived Access Tokens*).
Danach läuft alles von selbst: einlesen → analysieren → die KI erzeugt Vorschläge.

### Ollama auf dem Host erreichbar machen
```ini
# sudo systemctl edit ollama
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```
Port 11434 nur im LAN erreichbar lassen (Ollama hat keine Anmeldung). Danach `sudo systemctl restart ollama`.
Das Modell wählst du in der Oberfläche (Zahnrad). Empfehlung: `hermes3:8b`.

### VRAM-Budget (ca. 10 GB von 24 GB)
| Modell (Q4)  | Gewichte | + Kontext 4096 | Bemerkung |
|--------------|---------:|---------------:|-----------|
| `hermes3:3b` | ca. 2 GB | ca. 0,3 GB     | schnell, einfache Fälle |
| `hermes3:8b` | ca. 4,7 GB | ca. 0,6 GB   | **Empfehlung** |

Ollama hat kein hartes Limit; das Budget hältst du über Modellgröße und Kontextgröße ein (beides in den
Einstellungen). Das Modell wird nach 2 Minuten Leerlauf wieder aus dem VRAM entladen. Die GPU arbeitet nur für
die auffälligen Automationen (Standard: maximal 12 pro Lauf); die eigentliche Analyse läuft ohne KI.

## Was geprüft wird (ohne KI, in Millisekunden)
Fehlende oder nicht verfügbare Entitäten/Geräte, nie oder lange nicht ausgelöste Automationen, deaktivierte,
Polling (`time_pattern`, `now()` in Templates), identische Duplikate, gegensätzliche Aktionen auf demselben
Auslöser, `delay` im Modus `single`, ungenutzte Skripte, leere Batterien, veraltete Syntax.
Wo eine Konfigurationsänderung hilft, schreibt die KI einen Vorschlag; alles andere erscheint unter
*Weitere Hinweise*. Nicht mehr aktuelle oder verworfene Vorschläge kommen nicht wieder.

## Sicherheit
* **Schreibschutz** ist standardmäßig an: jede Änderung muss einzeln bestätigt werden; danach Backup → Schreiben → Neu-Scan.
* Backups liegen als JSON in `./data/backups` und enthalten alle Automationen, Skripte und Szenen.
* Das Token liegt im Klartext in `./data/hafix.db`. Die Oberfläche hat standardmäßig keinen Login:
  mit `HAFIX_PASSWORD` schaltest du Passwortschutz ein. Nicht ungeschützt ins Internet stellen.
* Nur Automationen mit `id:` sind über die API änderbar (UI-erstellte immer; YAML ohne `id:` erscheinen nur als Hinweis).

## Entwicklung
```bash
pip install -r requirements.txt pytest
python -m pytest                               # Ende-zu-Ende gegen Fake-HA + Fake-Ollama
DATA_DIR=./data uvicorn app.main:app --port 8099
```

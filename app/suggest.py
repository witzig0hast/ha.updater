"""Automatische Vorschläge: die KI geht die auffälligen Automationen durch und schlägt Korrekturen vor."""
import datetime as dt
import hashlib
import re
from collections import defaultdict

from . import chat, db, llm, opportunities, store
from .analyzer import cfg_hash, item_key, parse_ts

# Befunde, bei denen eine Konfigurationsänderung sinnvoll ist (der Rest wird als Hinweis angezeigt)
FIXABLE = {"fehlende_entitaet", "fehlendes_geraet", "polling", "konflikt", "mode", "nicht_verfuegbar"}
ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

SYSTEM = """Du bist Experte für Home-Assistant-Automationen. Du bekommst eine Automation mit erkannten Problemen \
und sollst sie korrigieren – möglichst einfach und mit wenig Rechenleistung (State-Trigger statt Polling, keine \
unnötigen Templates). Regeln:
- Verwende NUR Entity-IDs aus dem Kontext. Erfinde keine.
- Ändere nur, was für die Behebung nötig ist. Gib die KOMPLETTE neue Konfiguration zurück (alias, description, \
triggers, conditions, actions, mode).
- WICHTIG: Behalte ALLE bisherigen Aktionen/Wirkungen der Automation bei, die nicht Teil des gemeldeten Problems \
sind. Das Beheben eines einzelnen Problems darf keine anderen, unbeteiligten Aktionen entfernen oder ihre Ziel-\
Entitäten ändern.
- `alias` und `description` in der Konfiguration beschreiben, WAS die Automation tut (für den Nutzer, der sie sich \
später in Home Assistant ansieht) – z. B. „Schaltet beim Fernseher-Start das Deckenlicht aus.“ Du darfst beide \
gerne klarer/treffender formulieren als vorher. Sie dürfen aber NIEMALS beschreiben, was DU gerade geändert hast \
(kein „optimiert“, kein „Modus von single zu restart geändert“, kein „behebt Problem X“, kein Verweis auf diesen \
Vorgang). Was sich ändert und warum gehört ausschließlich ins Feld `explanation`, nicht in die Konfiguration.
- Wenn sich das Problem nicht sicher automatisch lösen lässt, setze config auf null.
Antworte NUR mit JSON:
{"title": "kurzer Titel, max. 8 Wörter", "explanation": "1-2 Sätze für Laien: was wird geändert und warum", \
"config": { ... } oder null}"""


NEW_SYSTEM = """Du bist Experte für Home-Assistant-Automationen. Der Nutzer hat einen Bewegungsmelder, der noch in \
KEINER Automation als Auslöser benutzt wird, obwohl im selben Bereich schaltbare Geräte stehen. Schlage GENAU EINE \
einfache, sinnvolle neue Automation vor, die beim Auslösen des Bewegungsmelders eines oder mehrere der genannten \
Geräte schaltet. Regeln:
- Verwende NUR die genannten Entity-IDs. Erfinde keine.
- Bevorzuge die naheliegendste, simpelste Lösung (z. B. Licht an bei Bewegung, nach einer Wartezeit ohne Bewegung \
wieder aus). Nutze einen zweiten State-Trigger für „keine Bewegung mehr“ statt Polling oder `wait_template`.
- `alias` und `description` beschreiben, WAS die Automation tut (für den Nutzer) – niemals, dass du sie gerade \
vorgeschlagen oder erstellt hast.
- Wenn aus den gegebenen Geräten keine sinnvolle Automation hervorgeht, setze config auf null.
Antworte NUR mit JSON:
{"title": "kurzer Titel, max. 8 Wörter", "explanation": "1-2 Sätze für Laien: was die neue Automation tun würde", \
"config": { ... } oder null}"""


def similar_entities(brain: dict, missing: list[str], per: int = 5) -> list[str]:
    """Für fehlende Entitäten ähnlich benannte, existierende Entitäten derselben Domain vorschlagen."""
    out = []
    for m in missing:
        dom, obj = m.split(".", 1)
        toks = set(re.findall(r"[a-z0-9]{3,}", obj))
        scored = []
        for eid, e in brain["entities"].items():
            if not eid.startswith(dom + "."):
                continue
            hay = set(re.findall(r"[a-z0-9]{3,}", eid.split(".", 1)[1])) | set(re.findall(r"[a-zäöüß0-9]{3,}", (e["name"] or "").lower()))
            if toks & hay:
                scored.append((len(toks & hay), eid, e["name"]))
        scored.sort(key=lambda x: -x[0])
        if scored:
            out.append(f"{m} -> mögliche Ersatz-Entitäten: " + ", ".join(f"{i} ({n})" for _, i, n in scored[:per]))
    return out


def candidates(brain: dict, limit: int) -> list[tuple[dict, list[dict]]]:
    rows = db.q("SELECT * FROM findings WHERE item_kind='automation'")
    by_item: dict[str, list[dict]] = {}
    for r in rows:
        if r["category"] in FIXABLE:
            by_item.setdefault(r["item_id"], []).append(r)
    out = []
    for iid, fs in by_item.items():
        it = store.find_item(brain, "automation", iid)
        if not it or not it["editable"] or not it["config"]:
            continue
        r = brain["refs"].get(item_key("automation", iid), {})
        if not (r.get("trigger") or r.get("condition") or r.get("action")):
            continue  # komplett tot -> Löschvorschlag statt Reparaturversuch
        h = cfg_hash(it["config"])
        seen = db.q1("SELECT id FROM proposals WHERE source='auto' AND target_id=? AND cfg_hash=? "
                     "AND status IN ('pending','rejected')", (iid, h))
        if seen:
            continue
        out.append((min(ORDER[f["severity"]] for f in fs), -len(fs), it, fs))
    out.sort(key=lambda x: (x[0], x[1]))
    return [(it, fs) for _, _, it, fs in out[:limit]]


def propose_dead_deletions(brain: dict) -> int:
    """Regelbasiert (ohne KI): Automationen, deren sämtliche Entitäten nicht mehr existieren, sind tot -> Löschvorschlag."""
    n = 0
    for it in brain["items"]:
        if it["kind"] != "automation" or not it["editable"] or not it["config"]:
            continue
        r = brain["refs"].get(item_key("automation", it["id"]), {})
        alive = r.get("trigger", []) + r.get("condition", []) + r.get("action", [])
        if alive or not (r.get("missing") or r.get("missing_devices")):
            continue
        h = cfg_hash(it["config"])
        if db.q1("SELECT id FROM proposals WHERE source='auto' AND target_id=? AND cfg_hash=? "
                 "AND status IN ('pending','rejected','applied')", (it["id"], h)):
            continue
        gone = ", ".join((r.get("missing") or []) + (r.get("missing_devices") or []))[:200]
        chat.create_delete(brain, it, None, "auto",
                           f"Alle beteiligten Geräte/Entitäten existieren nicht mehr ({gone}). "
                           "Die Automation kann nie etwas auslösen.")
        n += 1
    return n


def propose_stale_entities(brain: dict, stale_days: int) -> int:
    """Regelbasiert (ohne KI, läuft daher immer auf ALLE Entitäten): verwaiste oder lange nicht verfügbare
    Entitäten zum Löschen vorschlagen. Prüft vorher, ob die Entität noch von einer Automation gebraucht wird."""
    used_by: dict[str, list[str]] = defaultdict(list)
    for it in brain["items"]:
        if it["kind"] != "automation":
            continue
        r = brain["refs"].get(item_key("automation", it["id"]), {})
        for e in set(r.get("trigger", []) + r.get("condition", []) + r.get("action", []) + r.get("other", [])):
            used_by[e].append(it["alias"])
        for e in r.get("missing", []):
            used_by[e].append(f"{it['alias']} (verweist bereits auf eine fehlende Entität)")

    now = dt.datetime.now(dt.timezone.utc)
    n = 0
    for eid, e in brain["entities"].items():
        if e.get("disabled"):
            continue
        since = None
        if e["state"] is None:
            reason = "orphaned"
        elif e["state"] == "unavailable":
            last = parse_ts(e.get("last_changed"))
            if not last or (now - last).days < stale_days:
                continue
            reason, since = "unavailable", e["last_changed"]
        else:
            continue

        h = hashlib.sha1(f"{reason}:{e['state']}".encode()).hexdigest()[:12]
        if db.q1("SELECT id FROM proposals WHERE source='auto' AND target_kind='entity' AND target_id=? "
                 "AND cfg_hash=? AND status IN ('pending', 'rejected', 'applied')", (eid, h)):
            continue

        refs = used_by.get(eid, [])
        area = brain["areas"].get(e.get("area_id")) if e.get("area_id") else None
        dev = brain["devices"].get(e.get("device_id"), {}).get("name") if e.get("device_id") else None
        label = e["name"] or eid
        state_txt = ("meldet sich gar nicht mehr (verwaist – Integration oder Gerät vermutlich entfernt)"
                    if reason == "orphaned" else f"seit {since} nicht erreichbar („unavailable“)")
        analysis_old = (f"Entität: {label} ({eid})\nBereich: {area or '–'}\nGerät: {dev or '–'}\n"
                        f"Zustand: {state_txt}\n" +
                        (f"Wird noch genutzt von: {', '.join(refs)}" if refs
                         else "Wird von keiner Automation referenziert."))
        analysis_new = ("Die Entität wird aus der Home-Assistant-Registry entfernt. Meldet sich das Gerät später "
                        "wieder, legt Home Assistant die Entität automatisch neu an – ein echtes Löschen von "
                        "Hardware passiert dabei nicht.")
        warns = ([f"Wird noch von diesen Automationen genutzt: {', '.join(refs)}. Vor dem Löschen prüfen, ob "
                  "das gewollt ist, sonst funktionieren diese Automationen danach noch weniger."] if refs else [])
        title = f"„{label}“ entfernen" + (" (verwaist)" if reason == "orphaned" else " (lange nicht erreichbar)")
        explanation = (f"Meldet sich nicht mehr – vermutlich wurde das Gerät entfernt." if reason == "orphaned"
                       else f"Seit {since} nicht erreichbar.")
        chat.create_proposal(None, eid, title, explanation, None, None, warns, "auto", h, "delete_entity",
                             analysis_old, analysis_new, "entity")
        n += 1
    return n


def _opp_hash(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def new_candidates(brain: dict, limit: int) -> list[dict]:
    """Noch nicht vorgeschlagene (oder endgültig verworfene) Automatisierungs-Lücken, regelbasiert gefunden."""
    out = []
    for opp in opportunities.find_opportunities(brain):
        h = _opp_hash(opp["key"])
        if db.q1("SELECT id FROM proposals WHERE source='auto' AND target_id='new' AND cfg_hash=? "
                 "AND status IN ('pending', 'rejected', 'applied')", (h,)):
            continue
        out.append(opp)
    return out[:limit]


async def suggest_new_automation(settings: dict, brain: dict, opp: dict) -> int | None:
    ent_txt = "\n".join(f"- {d['entity_id']}: {d['name']} (Zustand {d['state']})"
                        for d in (store.describe_entity(brain, e) for e in opp["actuators"]))
    user = (f"Bereich: {opp['area']}\nBewegungsmelder (Auslöser): {opp['trigger']} ({opp['trigger_name']})\n"
            f"Schaltbare Geräte im selben Bereich:\n{ent_txt}")
    raw = await llm.chat(settings, [{"role": "system", "content": NEW_SYSTEM}, {"role": "user", "content": user}],
                         num_predict=1200)
    data = llm.parse_json(raw)
    if not data or not isinstance(data.get("config"), dict):
        return None
    cfg = data["config"]
    ok, warns = chat.validate_config(brain, cfg)
    if not ok:
        return None
    h = _opp_hash(opp["key"])
    analysis_old = (f"Noch keine Automation vorhanden.\nBereich: {opp['area']}\n"
                    f"Bewegungsmelder {opp['trigger']} wird bisher von keiner Automation als Auslöser genutzt.")
    return chat.create_proposal(None, "new", str(data.get("title") or opp["title"])[:80],
                                str(data.get("explanation") or ""), None, cfg, warns, "auto", h, "update",
                                analysis_old, store.describe_automation(brain, cfg))


def retire_stale(brain: dict) -> None:
    """Automatische Vorschläge, deren Ziel sich inzwischen geändert hat (oder nicht mehr existiert), sind überholt."""
    new_opp_hashes = {_opp_hash(o["key"]) for o in opportunities.find_opportunities(brain)}
    for p in db.q("SELECT id, target_id, cfg_hash, target_kind FROM proposals WHERE source='auto' AND status='pending'"):
        if p["target_kind"] == "entity":
            e = brain["entities"].get(p["target_id"])
            still = e and not e["disabled"] and e["state"] in (None, "unavailable")
            h = hashlib.sha1(f"{'orphaned' if e and e['state'] is None else 'unavailable'}:"
                             f"{e['state'] if e else ''}".encode()).hexdigest()[:12]
            if not still or h != p["cfg_hash"]:
                db.x("UPDATE proposals SET status='superseded' WHERE id=?", (p["id"],))
            continue
        if p["target_id"] == "new":
            # Neue Automation (noch nicht angewendet): überholt, sobald die Lücke anderweitig geschlossen wurde
            # (z. B. jemand hat den Sensor inzwischen manuell verdrahtet) oder sie nicht mehr existiert.
            if p["cfg_hash"] not in new_opp_hashes:
                db.x("UPDATE proposals SET status='superseded' WHERE id=?", (p["id"],))
            continue
        it = store.find_item(brain, "automation", p["target_id"])
        if not it or not it["config"] or cfg_hash(it["config"]) != p["cfg_hash"]:
            db.x("UPDATE proposals SET status='superseded' WHERE id=?", (p["id"],))


async def suggest_for(settings: dict, brain: dict, item: dict, findings: list[dict]) -> int | None:
    refs = brain["refs"].get(item_key("automation", item["id"]), {})
    ents = sorted(set(refs.get("trigger", []) + refs.get("condition", []) + refs.get("action", [])))[:25]
    ent_txt = "\n".join(f"- {d['entity_id']}: {d['name']} (Zustand {d['state']}, Bereich {d['area']})"
                        for d in (store.describe_entity(brain, e) for e in ents))
    sim = "\n".join(similar_entities(brain, refs.get("missing", [])))
    user = (f"Automation (YAML):\n{store.to_yaml(item['config'])[:6000]}\n\n"
            f"Erkannte Probleme:\n" + "\n".join(f"- {f['title']}: {f['detail']}" for f in findings) +
            f"\n\nBeteiligte Entitäten:\n{ent_txt}" + (f"\n\nFehlende Entitäten:\n{sim}" if sim else ""))
    raw = await llm.chat(settings, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                         num_predict=1500)
    data = llm.parse_json(raw)
    if not data or not isinstance(data.get("config"), dict):
        return None
    cfg = data["config"]
    ok, warns = chat.validate_config(brain, cfg, item["config"])
    if not ok or cfg == item["config"]:
        return None
    return chat.create_proposal(None, item["id"], str(data.get("title") or item["alias"])[:80],
                                str(data.get("explanation") or ""), item["config"], cfg, warns,
                                "auto", cfg_hash(item["config"]), "update",
                                store.describe_automation(brain, item["config"]),
                                store.describe_automation(brain, cfg))

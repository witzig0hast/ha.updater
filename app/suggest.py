"""Automatische Vorschläge: die KI geht die auffälligen Automationen durch und schlägt Korrekturen vor."""
import re

from . import chat, db, llm, store
from .analyzer import cfg_hash, item_key

# Befunde, bei denen eine Konfigurationsänderung sinnvoll ist (der Rest wird als Hinweis angezeigt)
FIXABLE = {"fehlende_entitaet", "fehlendes_geraet", "polling", "konflikt", "mode", "nicht_verfuegbar"}
ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

SYSTEM = """Du bist Experte für Home-Assistant-Automationen. Du bekommst eine Automation mit erkannten Problemen \
und sollst sie korrigieren – möglichst einfach und mit wenig Rechenleistung (State-Trigger statt Polling, keine \
unnötigen Templates). Regeln:
- Verwende NUR Entity-IDs aus dem Kontext. Erfinde keine.
- Ändere nur, was für die Behebung nötig ist. Gib die KOMPLETTE neue Konfiguration zurück (alias, description, \
triggers, conditions, actions, mode).
- Wenn sich das Problem nicht sicher automatisch lösen lässt, setze config auf null.
Antworte NUR mit JSON:
{"title": "kurzer Titel, max. 8 Wörter", "explanation": "1-2 Sätze für Laien: was wird geändert und warum", \
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
        h = cfg_hash(it["config"])
        seen = db.q1("SELECT id FROM proposals WHERE source='auto' AND target_id=? AND cfg_hash=? "
                     "AND status IN ('pending','rejected')", (iid, h))
        if seen:
            continue
        out.append((min(ORDER[f["severity"]] for f in fs), -len(fs), it, fs))
    out.sort(key=lambda x: (x[0], x[1]))
    return [(it, fs) for _, _, it, fs in out[:limit]]


def retire_stale(brain: dict) -> None:
    """Automatische Vorschläge, deren Automation sich inzwischen geändert hat, sind überholt."""
    for p in db.q("SELECT id, target_id, cfg_hash FROM proposals WHERE source='auto' AND status='pending'"):
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
    ok, warns = chat.validate_config(brain, cfg)
    if not ok or cfg == item["config"]:
        return None
    return chat.create_proposal(None, item["id"], str(data.get("title") or item["alias"])[:80],
                                str(data.get("explanation") or ""), item["config"], cfg, warns,
                                "auto", cfg_hash(item["config"]))

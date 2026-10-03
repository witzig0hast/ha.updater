"""Gehirn-Persistenz: Snapshot laden/speichern, Suche, Kontext für das LLM."""
import json
import re

import yaml

from . import db
from .analyzer import cfg_hash, item_key

_cache: dict | None = None


def save_brain(brain: dict, refs: dict, findings: list[dict]) -> None:
    global _cache
    brain = {**brain, "refs": refs}
    db.x("INSERT INTO brain(id, ts, data) VALUES(1, ?, ?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, data=excluded.data",
         (brain["scanned_at"], json.dumps(brain)))
    db.x("DELETE FROM findings")
    db.many("INSERT INTO findings(severity, category, item_kind, item_id, title, detail) VALUES(?,?,?,?,?,?)",
            [(f["severity"], f["category"], f["item_kind"], f["item_id"], f["title"], f["detail"]) for f in findings])
    _cache = brain


def get_brain() -> dict | None:
    global _cache
    if _cache is None:
        row = db.q1("SELECT data FROM brain WHERE id = 1")
        if row:
            _cache = json.loads(row["data"])
    return _cache


def find_item(brain: dict, kind: str, item_id: str) -> dict | None:
    return next((i for i in brain["items"] if i["kind"] == kind and i["id"] == item_id), None)


def to_yaml(obj) -> str:
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)


def describe_entity(brain: dict, eid: str) -> dict:
    e = brain["entities"].get(eid)
    if not e:
        return {"entity_id": eid, "name": None, "state": None, "area": None, "exists": False}
    return {"entity_id": eid, "name": e["name"], "state": e["state"],
            "area": brain["areas"].get(e["area_id"]) if e.get("area_id") else None, "exists": True}


def get_review(kind: str, item_id: str) -> dict | None:
    row = db.q1("SELECT data, cfg_hash, ts FROM reviews WHERE item_kind=? AND item_id=?", (kind, item_id))
    if not row:
        return None
    return {**json.loads(row["data"]), "cfg_hash": row["cfg_hash"], "ts": row["ts"]}


def save_review(kind: str, item_id: str, cfg, data: dict) -> None:
    db.x("INSERT INTO reviews(item_kind, item_id, cfg_hash, data, ts) VALUES(?,?,?,?,datetime('now')) "
         "ON CONFLICT(item_kind, item_id) DO UPDATE SET cfg_hash=excluded.cfg_hash, data=excluded.data, ts=excluded.ts",
         (kind, item_id, cfg_hash(cfg), json.dumps(data)))


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zäöüß0-9]{3,}", text.lower())}


def relevant_context(brain: dict, message: str, max_items: int = 3, max_entities: int = 20) -> dict:
    """Einfache Schlagwort-Suche über das Gehirn: nur Relevantes kommt ins (kleine) Kontextfenster."""
    toks = _tokens(message)
    scored = []
    for it in brain["items"]:
        if it["kind"] != "automation" or it["config"] is None:
            continue
        refs = brain["refs"].get(item_key(it["kind"], it["id"]), {})
        ref_ents = refs.get("trigger", []) + refs.get("condition", []) + refs.get("action", [])
        hay = _tokens(it["alias"]) | set().union(*(_tokens(e) | _tokens(brain["entities"][e]["name"]) for e in ref_ents), set())
        for e in ref_ents:
            a = brain["entities"][e].get("area_id")
            if a:
                hay |= _tokens(brain["areas"].get(a, ""))
        score = 3 * len(toks & _tokens(it["alias"])) + len(toks & hay)
        if score:
            scored.append((score, it))
    scored.sort(key=lambda s: -s[0])
    items = [it for _, it in scored[:max_items]]

    ent_scored = []
    for eid, e in brain["entities"].items():
        s = len(toks & (_tokens(eid) | _tokens(e["name"] or ""))) * 2
        if e.get("area_id"):
            s += len(toks & _tokens(brain["areas"].get(e["area_id"], "")))
        if s:
            ent_scored.append((s, eid))
    ent_scored.sort(key=lambda s: -s[0])
    ents = [describe_entity(brain, eid) for _, eid in ent_scored[:max_entities]]
    return {"items": items, "entities": ents}


def brain_summary_text(brain: dict) -> str:
    n_auto = sum(1 for i in brain["items"] if i["kind"] == "automation")
    areas = ", ".join(sorted(brain["areas"].values())[:25])
    return (f"Home Assistant {brain['ha_version']}, {len(brain['entities'])} Entitäten, "
            f"{len(brain['devices'])} Geräte, {n_auto} Automationen. Bereiche: {areas}.")

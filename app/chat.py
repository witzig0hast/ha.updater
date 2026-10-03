"""Chat: Beschwerde -> Änderungsvorschlag (nur Vorschlag!) -> Zustimmung/Überarbeitung."""
import difflib
import json

from . import db, llm, store
from .analyzer import actions_of, cfg_hash, triggers_of

SYSTEM = """Du bist HA-Fix, ein Assistent für Home-Assistant-Automationen. Der Nutzer beschreibt, was ihn an einer \
Automation oder einem Ablauf stört. Du bekommst relevante Automationen (YAML) und echte Entitäten aus seinem System.
Regeln:
- Verwende NUR Entity-IDs aus dem Kontext. Erfinde keine.
- Du änderst nichts selbst. Du machst nur einen Vorschlag, den der Nutzer freigeben muss.
- Ziel: Logik korrekt, sinnvoll und mit möglichst wenig Rechenleistung (State-Trigger statt Polling, keine unnötigen Templates).
- Wenn die Angaben nicht reichen, stelle eine kurze Rückfrage und setze proposal auf null.
- Bei Änderung einer bestehenden Automation: gib die KOMPLETTE neue Konfiguration zurück (alias, description, triggers, conditions, actions, mode).
Antworte NUR mit JSON:
{"reply": "kurze Erklärung auf Deutsch",
 "proposal": null | {"target_id": "<id der bestehenden Automation oder \\"new\\">", "title": "kurzer Titel",
                     "explanation": "was ändert sich und warum", "config": { ...Automations-Konfiguration als JSON... }}}"""


def validate_config(brain: dict, cfg) -> tuple[bool, list[str]]:
    warns = []
    if not isinstance(cfg, dict):
        return False, ["Konfiguration ist kein Objekt."]
    if not isinstance(cfg.get("alias"), str) or not cfg["alias"].strip():
        return False, ["alias fehlt."]
    if not triggers_of(cfg):
        return False, ["Kein Trigger angegeben."]
    if not actions_of(cfg):
        return False, ["Keine Aktion angegeben."]
    from .analyzer import extract_refs  # lokal, vermeidet Zirkularität
    ents = brain["entities"]
    domains = {e.split(".", 1)[0] for e in ents} | set(brain.get("service_domains", []))
    reg_ids = {e["reg_id"]: eid for eid, e in ents.items() if e.get("reg_id")}
    r = extract_refs(cfg, ents, brain["devices"], domains, reg_ids)
    if r["missing"]:
        warns.append("Unbekannte Entitäten: " + ", ".join(r["missing"]))
    if r["missing_devices"]:
        warns.append("Unbekannte Geräte: " + ", ".join(r["missing_devices"]))
    return True, warns


def diff_text(old, new) -> str:
    a = store.to_yaml(old).splitlines() if old else []
    b = store.to_yaml(new).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "vorher", "nachher", lineterm="", n=3))


def create_proposal(conv_id, target, title, explanation, old, new, warns, source="chat", cfg_hash=None) -> int:
    return db.x(
        "INSERT INTO proposals(conv_id, target_id, title, explanation, old_config, new_config, warnings, source, cfg_hash) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (conv_id, target, title, explanation, json.dumps(old) if old else None, json.dumps(new),
         json.dumps(warns), source, cfg_hash))


def proposal_view(p: dict) -> dict:
    old = json.loads(p["old_config"]) if p["old_config"] else None
    new = json.loads(p["new_config"])
    return {**{k: p[k] for k in ("id", "conv_id", "target_id", "title", "explanation", "status", "error",
                                 "backup", "created", "applied", "source")},
            "warnings": json.loads(p["warnings"] or "[]"), "is_new": old is None,
            "old_yaml": store.to_yaml(old) if old else "", "new_yaml": store.to_yaml(new),
            "diff": diff_text(old, new)}


async def handle_message(settings: dict, conv_id: int | None, message: str,
                         revise_proposal: int | None = None) -> dict:
    brain = store.get_brain()
    if not brain:
        raise llm.LLMError("Noch kein Scan vorhanden – bitte zuerst „Gehirn aufbauen“ ausführen.")
    if conv_id is None:
        conv_id = db.x("INSERT INTO conversations(ts, title) VALUES(datetime('now'), ?)", (message[:60],))
    db.x("INSERT INTO messages(conv_id, role, content, proposal_id) VALUES(?,?,?,?)",
         (conv_id, "user", message, revise_proposal))

    ctx = store.relevant_context(brain, message)
    parts = [store.brain_summary_text(brain)]
    if revise_proposal:
        p = db.q1("SELECT * FROM proposals WHERE id=?", (revise_proposal,))
        if p:
            parts.append("Bisheriger Vorschlag, der überarbeitet werden soll (target_id "
                         f"{p['target_id']}):\n{store.to_yaml(json.loads(p['new_config']))}")
            ctx["items"] = [i for i in ctx["items"] if i["id"] != p["target_id"]]
    for it in ctx["items"]:
        parts.append(f"Automation id={it['id']} alias=„{it['alias']}“:\n{store.to_yaml(it['config'])[:3500]}")
    if ctx["entities"]:
        parts.append("Passende Entitäten:\n" + "\n".join(
            f"- {e['entity_id']} ({e['name']}, Zustand {e['state']}, Bereich {e['area']})" for e in ctx["entities"]))

    history = db.q("SELECT role, content FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 7", (conv_id,))[::-1]
    msgs = [{"role": "system", "content": SYSTEM + "\n\nKONTEXT:\n" + "\n\n".join(parts)}]
    msgs += [{"role": h["role"], "content": h["content"]} for h in history]

    raw = await llm.chat(settings, msgs, num_predict=1800)
    data = llm.parse_json(raw)
    reply, proposal_id = raw, None
    if data:
        reply = str(data.get("reply") or "")
        prop = data.get("proposal")
        if isinstance(prop, dict):
            cfg = prop.get("config")
            ok, warns = validate_config(brain, cfg)
            if ok:
                target = str(prop.get("target_id") or "new")
                existing = store.find_item(brain, "automation", target)
                old = existing["config"] if existing and existing["config"] else None
                if old is None:
                    target = "new"
                proposal_id = create_proposal(
                    conv_id, target, str(prop.get("title") or cfg["alias"]), str(prop.get("explanation") or ""),
                    old, cfg, warns, "chat", cfg_hash(old) if old else None)
                if revise_proposal:
                    db.x("UPDATE proposals SET status='superseded' WHERE id=? AND status='pending'", (revise_proposal,))
            else:
                reply += "\n\n(Der Vorschlag des Modells war ungültig: " + " ".join(warns) + " – bitte präzisiere.)"
    db.x("INSERT INTO messages(conv_id, role, content, proposal_id) VALUES(?,?,?,?)",
         (conv_id, "assistant", reply, proposal_id))
    return {"conv_id": conv_id, "reply": reply,
            "proposal": proposal_view(db.q1("SELECT * FROM proposals WHERE id=?", (proposal_id,))) if proposal_id else None}

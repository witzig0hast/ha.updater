"""Chat: Beschwerde -> Änderungsvorschlag (nur Vorschlag!) -> Zustimmung/Überarbeitung."""
import difflib
import json
import re

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
- WICHTIG: Behalte alle bisherigen Aktionen/Wirkungen der Automation bei, außer der Nutzer bittet ausdrücklich darum, \
genau diese Aktion zu entfernen. Das Beheben EINES Problems (z. B. eine falsche Entität) darf NICHT dazu führen, \
dass andere, unbeteiligte Aktionen verschwinden. Wenn du unsicher bist, ob eine Aktion noch gebraucht wird, behalte sie.
- `alias` und `description` in der Konfiguration beschreiben, WAS die Automation tut (für den Nutzer, der sie sich \
später in Home Assistant ansieht) – z. B. „Schaltet beim Fernseher-Start das Deckenlicht aus.“ Du darfst beide \
gerne klarer/treffender formulieren als vorher. Sie dürfen aber NIEMALS beschreiben, was DU gerade geändert hast \
(kein „optimiert“, kein „Modus von single zu restart geändert“, kein Verweis auf diesen Vorgang oder die Anfrage \
des Nutzers). Was sich ändert und warum gehört ausschließlich ins Feld `explanation`, nicht in die Konfiguration.
- Will der Nutzer eine Automation LÖSCHEN/entfernen, nimm action "delete" (ohne config).
- Die AKTUELLE ANWEISUNG des Nutzers hat immer Vorrang vor früheren Vorschlägen. Ein früherer Vorschlag ist nur \
Ausgangspunkt und darf komplett verworfen werden.
Antworte NUR mit JSON:
{"reply": "kurze Erklärung auf Deutsch",
 "proposal": null | {"action": "update" | "delete", "target_id": "<id der bestehenden Automation oder \\"new\\">",
                     "title": "kurzer Titel", "explanation": "was ändert sich und warum",
                     "config": { ...komplette Automations-Konfiguration als JSON, nur bei update... }}}"""

DELETE_RE = re.compile(r"\b(lösch\w*|entfern\w*|wegmachen|rauswerfen|abschaffen|weg(?:\s+damit)?|"
                       r"nicht mehr (?:brauch|benötig)\w*|delete|remove)\b", re.I)


def wants_delete(message: str) -> bool:
    return bool(DELETE_RE.search(message))


META_RE = re.compile(
    r"\b(optimiert?|geändert|angepasst|behoben|korrigiert|verbessert|gefixt|fix(?:ed)?|repariert|"
    r"modus von .* zu |von ['\"]?single['\"]? zu |wurde (?:auf|zu|in) )\b", re.I)


def meta_language_warning(cfg: dict) -> str | None:
    """Erkennt, wenn `alias`/`description` statt der Funktion das Vorgehen des Modells beschreiben."""
    text = f"{cfg.get('alias', '')} {cfg.get('description', '')}"
    if META_RE.search(text):
        return ("„alias“/„description“ klingen nach einem Änderungsprotokoll statt einer Funktionsbeschreibung "
                "(z. B. „optimiert“, „geändert von …“). Bitte prüfen und ggf. anpassen.")
    return None


def validate_config(brain: dict, cfg, old=None) -> tuple[bool, list[str]]:
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
    gone = store.missing_effects(brain, old, cfg)
    if gone:
        warns.append("Achtung, diese bisher gesteuerten Geräte kommen im Vorschlag nicht mehr vor: " +
                     ", ".join(gone) + ". Prüfe, ob das wirklich gewollt ist.")
    meta = meta_language_warning(cfg)
    if meta:
        warns.append(meta)
    return True, warns


def diff_text(old, new) -> str:
    a = store.to_yaml(old).splitlines() if old else []
    b = store.to_yaml(new).splitlines() if new else []
    return "\n".join(difflib.unified_diff(a, b, "vorher", "nachher", lineterm="", n=3))


def create_proposal(conv_id, target, title, explanation, old, new, warns, source="chat", cfg_hash=None,
                    action="update", analysis_old=None, analysis_new=None, target_kind="automation") -> int:
    return db.x(
        "INSERT INTO proposals(conv_id, target_id, title, explanation, old_config, new_config, warnings, source, "
        "cfg_hash, action, analysis_old, analysis_new, target_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (conv_id, target, title, explanation, json.dumps(old) if old else None,
         json.dumps(new) if new is not None else None, json.dumps(warns), source, cfg_hash, action,
         analysis_old, analysis_new, target_kind))


def create_delete(brain: dict, item: dict, conv_id=None, source="chat", reason="") -> int:
    return create_proposal(conv_id, item["id"], f"„{item['alias']}“ löschen",
                           reason or "Diese Automation wird komplett aus Home Assistant entfernt.",
                           item["config"], None, [], source, cfg_hash(item["config"]), "delete",
                           store.describe_automation(brain, item["config"]),
                           "Automation wird vollständig entfernt – keine Aktionen mehr.")


def proposal_view(p: dict) -> dict:
    is_entity = p["target_kind"] == "entity"
    old = json.loads(p["old_config"]) if p["old_config"] else None
    new = json.loads(p["new_config"]) if p["new_config"] else None
    return {**{k: p[k] for k in ("id", "conv_id", "target_id", "title", "explanation", "status", "error",
                                 "backup", "created", "applied", "source", "action", "analysis_old", "analysis_new",
                                 "target_kind")},
            "warnings": json.loads(p["warnings"] or "[]"), "is_new": old is None and new is not None,
            "old_yaml": store.to_yaml(old) if old else "", "new_yaml": store.to_yaml(new) if new else "",
            "diff": "" if is_entity else diff_text(old, new)}


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
    prev = db.q1("SELECT * FROM proposals WHERE id=?", (revise_proposal,)) if revise_proposal else None

    # Löschwunsch: braucht kein Sprachmodell (kleine Modelle klammern sich sonst an den alten Vorschlag)
    if wants_delete(message):
        target = None
        if prev and prev["target_id"] != "new":
            target = store.find_item(brain, "automation", prev["target_id"])
        elif ctx["items"] and (len(ctx["items"]) == 1 or ctx["scores"][0] >= 2 * ctx["scores"][1]):
            target = ctx["items"][0]
        elif len(ctx["items"]) > 1:
            names = "\n".join(f"- {i['alias']}" for i in ctx["items"])
            return _answer(conv_id, "Welche Automation soll gelöscht werden? Ich habe mehrere gefunden:\n" + names +
                           "\nSchreib bitte den Namen genauer.", None)
        if target and target["config"] is not None and target["editable"]:
            pid = create_delete(brain, target, conv_id)
            if prev:
                db.x("UPDATE proposals SET status='superseded' WHERE id=? AND status='pending'", (revise_proposal,))
            return _answer(conv_id, f"Verstanden, „{target['alias']}“ soll komplett gelöscht werden. "
                                    "Vorher lege ich ein Backup an, gelöscht wird erst nach deiner Bestätigung.", pid)

    parts = [store.brain_summary_text(brain)]
    if prev and prev["new_config"]:
        parts.append(f"Bisheriger Vorschlag (target_id {prev['target_id']}), den der Nutzer NICHT so wollte "
                     f"und der überarbeitet oder ersetzt werden soll:\n{store.to_yaml(json.loads(prev['new_config']))}")
        ctx["items"] = [i for i in ctx["items"] if i["id"] != prev["target_id"]]
    for it in ctx["items"]:
        parts.append(f"Automation id={it['id']} alias=„{it['alias']}“:\n{store.to_yaml(it['config'])[:3500]}")
    if ctx["entities"]:
        parts.append("Passende Entitäten:\n" + "\n".join(
            f"- {e['entity_id']} ({e['name']}, Zustand {e['state']}, Bereich {e['area']})" for e in ctx["entities"]))

    # Bei einer Überarbeitung nur die aktuelle Anweisung, sonst die letzten Nachrichten
    history = db.q("SELECT role, content FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?",
                   (conv_id, 1 if prev else 5))[::-1]
    msgs = [{"role": "system", "content": SYSTEM + "\n\nKONTEXT:\n" + "\n\n".join(parts)}]
    msgs += [{"role": h["role"], "content": h["content"]} for h in history]
    if prev:
        msgs[-1] = {"role": "user", "content": f"AKTUELLE ANWEISUNG (hat Vorrang): {message}"}

    raw = await llm.chat(settings, msgs, num_predict=1800)
    data = llm.parse_json(raw)
    reply, proposal_id = raw, None
    if data:
        reply = str(data.get("reply") or "")
        prop = data.get("proposal")
        if isinstance(prop, dict) and prop.get("action") == "delete":
            target = store.find_item(brain, "automation", str(prop.get("target_id")))
            if target and target["config"] is not None and target["editable"]:
                proposal_id = create_delete(brain, target, conv_id, reason=str(prop.get("explanation") or ""))
                if revise_proposal:
                    db.x("UPDATE proposals SET status='superseded' WHERE id=? AND status='pending'", (revise_proposal,))
            else:
                reply += "\n\n(Welche Automation gelöscht werden soll, war nicht eindeutig – bitte nenne den Namen.)"
        elif isinstance(prop, dict):
            cfg = prop.get("config")
            target = str(prop.get("target_id") or "new")
            existing = store.find_item(brain, "automation", target)
            old = existing["config"] if existing and existing["config"] else None
            ok, warns = validate_config(brain, cfg, old)
            if ok:
                if old is None:
                    target = "new"
                proposal_id = create_proposal(
                    conv_id, target, str(prop.get("title") or cfg["alias"]), str(prop.get("explanation") or ""),
                    old, cfg, warns, "chat", cfg_hash(old) if old else None, "update",
                    store.describe_automation(brain, old) if old else None, store.describe_automation(brain, cfg))
                if revise_proposal:
                    db.x("UPDATE proposals SET status='superseded' WHERE id=? AND status='pending'", (revise_proposal,))
            else:
                reply += "\n\n(Der Vorschlag des Modells war ungültig: " + " ".join(warns) + " – bitte präzisiere.)"
    return _answer(conv_id, reply, proposal_id)


def _answer(conv_id: int, reply: str, proposal_id: int | None) -> dict:
    db.x("INSERT INTO messages(conv_id, role, content, proposal_id) VALUES(?,?,?,?)",
         (conv_id, "assistant", reply, proposal_id))
    return {"conv_id": conv_id, "reply": reply,
            "proposal": proposal_view(db.q1("SELECT * FROM proposals WHERE id=?", (proposal_id,))) if proposal_id else None}

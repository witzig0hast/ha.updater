"""KI-Review einzelner Automationen (sequenziell, damit nur ein Modell/Kontext im VRAM liegt)."""
from . import llm, store
from .analyzer import cfg_hash, item_key

SYSTEM = """Du bist ein Experte für Home-Assistant-Automationen. Du bewertest eine Automation auf Logik, \
Sinnhaftigkeit und Effizienz (wenig Rechenleistung: bevorzuge State-Trigger statt Polling, keine unnötigen Templates). \
Antworte NUR mit JSON:
{"verdict": "ok" | "verbesserbar" | "problematisch", "summary": "ein Satz", \
"issues": ["konkrete Probleme"], "suggestion": "kurzer Verbesserungsvorschlag oder leer"}
Sei knapp, auf Deutsch, erfinde nichts."""


async def review_item(settings: dict, brain: dict, item: dict, findings: list[dict]) -> dict:
    refs = brain["refs"].get(item_key(item["kind"], item["id"]), {})
    ents = sorted(set(refs.get("trigger", []) + refs.get("condition", []) + refs.get("action", [])))[:25]
    ent_txt = "\n".join(f"- {d['entity_id']}: {d['name']} (Zustand {d['state']}, Bereich {d['area']})"
                        for d in (store.describe_entity(brain, e) for e in ents))
    hints = "\n".join(f"- {f['title']}" for f in findings) or "keine"
    user = (f"Automation (YAML):\n{store.to_yaml(item['config'])[:6000]}\n\n"
            f"Beteiligte Entitäten:\n{ent_txt}\n\nAutomatische Befunde:\n{hints}")
    raw = await llm.chat(settings, [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                         num_predict=600)
    data = llm.parse_json(raw) or {"verdict": "verbesserbar", "summary": raw[:300], "issues": [], "suggestion": ""}
    data.setdefault("issues", [])
    store.save_review(item["kind"], item["id"], item["config"], data)
    return data


def needs_review(item: dict) -> bool:
    if item["kind"] != "automation" or item["config"] is None:
        return False
    r = store.get_review(item["kind"], item["id"])
    return not r or r["cfg_hash"] != cfg_hash(item["config"])

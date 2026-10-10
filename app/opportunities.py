"""Regelbasiert (ohne KI): Stellen im System finden, die noch GAR NICHT automatisiert sind.

Erkennt z. B. einen Bewegungsmelder, der in keiner einzigen Automation als Auslöser vorkommt, obwohl im selben
Bereich Lampen/Schalter stehen. Daraus macht die KI dann – mit nur den echten Entitäten dieses einen Bereichs im
Kontext – einen konkreten neuen Automationsvorschlag (oder lässt es, wenn es keinen Sinn ergibt).
"""
from collections import defaultdict

MOTION_CLASSES = {"motion", "occupancy", "presence"}
ACTUATOR_DOMAINS = ("light", "switch", "fan")


def _triggered_entities(brain: dict) -> set[str]:
    """Alle Entitäten, die irgendwo bereits als Trigger einer Automation dienen."""
    out: set[str] = set()
    for r in brain["refs"].values():
        out |= set(r.get("trigger", []))
    return out


def find_opportunities(brain: dict) -> list[dict]:
    triggered = _triggered_entities(brain)
    by_area = defaultdict(list)
    for eid, e in brain["entities"].items():
        if e.get("area_id") and not e.get("disabled") and e["state"] is not None:
            by_area[e["area_id"]].append((eid, e))

    out = []
    for area_id, ents in sorted(by_area.items(), key=lambda kv: brain["areas"].get(kv[0], kv[0])):
        area_name = brain["areas"].get(area_id, area_id)
        actuators = [eid for eid, e in ents if eid.split(".", 1)[0] in ACTUATOR_DOMAINS]
        if not actuators:
            continue
        for eid, e in ents:
            if not eid.startswith("binary_sensor.") or eid in triggered:
                continue
            if (e.get("device_class") or "") not in MOTION_CLASSES:
                continue
            out.append({
                "key": f"motion_light:{area_id}:{eid}",
                "area": area_name, "trigger": eid, "trigger_name": e["name"],
                "actuators": actuators[:8],
                "title": f"Licht in „{area_name}“ bei Bewegung",
            })
    return out

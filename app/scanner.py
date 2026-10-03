"""Liest Home Assistant vollständig aus (nur lesend) und baut daraus das Gehirn."""
import asyncio
import datetime as dt

from .ha_client import HA

CONFIG_ENDPOINT = {"automation": "automation", "script": "script", "scene": "scene"}


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


async def fetch_configs(ha: HA, states: list[dict]) -> list[dict]:
    """Holt die Roh-Konfiguration aller Automationen, Skripte und Szenen."""
    sem = asyncio.Semaphore(8)

    async def one(st: dict) -> dict | None:
        eid = st["entity_id"]
        kind = eid.split(".", 1)[0]
        if kind not in CONFIG_ENDPOINT:
            return None
        attrs = st.get("attributes", {})
        cid = eid.split(".", 1)[1] if kind == "script" else attrs.get("id")
        item = {"kind": kind, "id": str(cid) if cid else eid, "entity_id": eid,
                "alias": attrs.get("friendly_name") or eid, "state": st.get("state"),
                "last_triggered": attrs.get("last_triggered"), "config": None, "editable": False}
        if cid:
            async with sem:
                cfg = await ha.get(f"/api/config/{kind}/config/{cid}", optional=True)
            if cfg is not None:
                item["config"], item["editable"] = cfg, True
        return item

    res = await asyncio.gather(*(one(s) for s in states))
    return [r for r in res if r]


async def scan(ha: HA, progress=lambda msg: None) -> dict:
    progress("Verbinde mit Home Assistant …")
    cfg = await ha.get("/api/config")
    progress("Lade Zustände …")
    states = await ha.get("/api/states")
    services = await ha.get("/api/services")
    progress("Lade Entitäts-, Geräte- und Bereichsregister …")
    reg = await ha.ws_commands(["config/entity_registry/list", "config/device_registry/list",
                                "config/area_registry/list"])
    progress("Lese alle Automationen, Skripte und Szenen …")
    items = await fetch_configs(ha, states)

    entity_reg = {e["entity_id"]: e for e in reg["config/entity_registry/list"]}
    devices = {d["id"]: d for d in reg["config/device_registry/list"]}
    areas = {a["area_id"]: a for a in reg["config/area_registry/list"]}

    entities = {}
    for st in states:
        eid = st["entity_id"]
        r = entity_reg.get(eid, {})
        dev = devices.get(r.get("device_id") or "", {})
        entities[eid] = {
            "state": st["state"],
            "name": st["attributes"].get("friendly_name") or r.get("name") or eid,
            "device_class": st["attributes"].get("device_class"),
            "unit": st["attributes"].get("unit_of_measurement"),
            "battery": st["attributes"].get("battery_level"),
            "last_changed": st.get("last_changed"),
            "device_id": r.get("device_id"),
            "area_id": r.get("area_id") or dev.get("area_id"),
            "platform": r.get("platform"),
            "disabled": bool(r.get("disabled_by")),
            "reg_id": r.get("id"),
        }
    # Registry-Entitäten ohne State (z.B. deaktiviert) trotzdem kennen
    for eid, r in entity_reg.items():
        entities.setdefault(eid, {
            "state": None, "name": r.get("name") or r.get("original_name") or eid,
            "device_class": None, "unit": None, "battery": None, "last_changed": None,
            "device_id": r.get("device_id"), "area_id": r.get("area_id"),
            "platform": r.get("platform"), "disabled": bool(r.get("disabled_by")),
            "reg_id": r.get("id")})

    return {
        "scanned_at": now_iso(),
        "ha_version": cfg.get("version"),
        "location": cfg.get("location_name"),
        "entities": entities,
        "devices": {i: {"name": d.get("name_by_user") or d.get("name"), "area_id": d.get("area_id"),
                        "manufacturer": d.get("manufacturer"), "model": d.get("model")}
                    for i, d in devices.items()},
        "areas": {i: a.get("name") for i, a in areas.items()},
        "service_domains": sorted({s["domain"] for s in services}),
        "items": items,
    }

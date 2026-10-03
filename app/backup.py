"""Backups der Automations-/Skript-/Szenen-Konfiguration (JSON-Dateien im Datenvolume)."""
import json
import re

from . import db
from .ha_client import HA
from .scanner import fetch_configs, now_iso


def _safe(name: str) -> str:
    if not re.fullmatch(r"[\w.\-]+\.json", name):
        raise ValueError("Ungültiger Backup-Name")
    return name


def write(items: list[dict], reason: str, ha_version: str | None) -> str:
    data = {"created": now_iso(), "reason": reason, "ha_version": ha_version}
    for kind in ("automation", "script", "scene"):
        data[kind] = {i["id"]: i["config"] for i in items if i["kind"] == kind and i["config"] is not None}
    name = f"{now_iso().replace(':', '-')}_{re.sub(r'[^a-z0-9]+', '-', reason.lower())}.json"
    (db.BACKUP_DIR / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return name


async def create_live(ha: HA, reason: str) -> str:
    """Frisches Backup direkt aus Home Assistant (vor jeder Änderung)."""
    states = await ha.get("/api/states")
    items = await fetch_configs(ha, states)
    cfg = await ha.get("/api/config")
    return write(items, reason, cfg.get("version"))


def list_backups() -> list[dict]:
    out = []
    for p in sorted(db.BACKUP_DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({"name": p.name, "created": d.get("created"), "reason": d.get("reason"),
                    "automations": len(d.get("automation", {})), "scripts": len(d.get("script", {})),
                    "scenes": len(d.get("scene", {})), "size": p.stat().st_size})
    return out


def read(name: str) -> dict:
    return json.loads((db.BACKUP_DIR / _safe(name)).read_text())

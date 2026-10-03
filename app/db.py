"""SQLite-Persistenz: Einstellungen, Gehirn-Snapshot, Findings, Reviews, Chat, Vorschläge."""
import json
import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BACKUP_DIR = DATA_DIR / "backups"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS brain (id INTEGER PRIMARY KEY CHECK (id = 1), ts TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, severity TEXT, category TEXT,
  item_kind TEXT, item_id TEXT, title TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  item_kind TEXT, item_id TEXT, cfg_hash TEXT, data TEXT, ts TEXT,
  PRIMARY KEY (item_kind, item_id));
CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INTEGER, role TEXT, content TEXT,
  proposal_id INTEGER, ts TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INTEGER, target_id TEXT, title TEXT,
  explanation TEXT, old_config TEXT, new_config TEXT, warnings TEXT,
  status TEXT DEFAULT 'pending', error TEXT, backup TEXT, source TEXT DEFAULT 'chat', cfg_hash TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP, applied TEXT);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DATA_DIR / "hafix.db", check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
        return _conn


def q(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql: str, args: tuple = ()) -> int:
    """Execute + commit, gibt lastrowid zurück."""
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def many(sql: str, rows: list[tuple]) -> None:
    with _lock:
        conn().executemany(sql, rows)
        conn().commit()


# ---------- Einstellungen ----------
DEFAULTS = {
    "ha_url": os.environ.get("HA_URL", ""),
    "ha_token": os.environ.get("HA_TOKEN", ""),
    "ha_verify_ssl": True,
    "ollama_url": os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
    "model": os.environ.get("OLLAMA_MODEL", "hermes3:8b"),
    "num_ctx": 4096,        # Kontextfenster -> bestimmt KV-Cache und damit VRAM
    "temperature": 0.2,
    "keep_alive": "2m",     # Modell nach 2 Min. Leerlauf aus dem VRAM werfen
    "read_only": True,      # True = es wird NIE etwas an Home Assistant geschrieben
    "backup_on_scan": True,
}


def get_settings() -> dict:
    out = dict(DEFAULTS)
    for row in q("SELECT k, v FROM kv WHERE k LIKE 'setting.%'"):
        out[row["k"][8:]] = json.loads(row["v"])
    return out


def set_settings(values: dict) -> None:
    for k, v in values.items():
        if k in DEFAULTS:
            x("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
              (f"setting.{k}", json.dumps(v)))

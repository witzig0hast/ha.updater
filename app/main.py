"""HA-Fix – FastAPI-Backend + statische UI."""
import asyncio
import base64
import hmac
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analyzer, backup, chat, db, llm, review, scanner, store, suggest
from .ha_client import HAError, from_settings

app = FastAPI(title="HA-Fix")
PASSWORD = os.environ.get("HAFIX_PASSWORD")
jobs: dict[str, dict] = {"scan": {"running": False, "msg": "", "error": None},
                         "run": {"running": False, "msg": "", "error": None, "ai_error": None, "done": 0, "total": 0},
                         "review": {"running": False, "done": 0, "total": 0, "current": "", "error": None,
                                    "cancel": False}}


@app.middleware("http")
async def auth(request: Request, call_next):
    if PASSWORD:
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = hmac.compare_digest(pw, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="HA-Fix"'})
    return await call_next(request)


@app.exception_handler(HAError)
async def ha_error(_, e: HAError):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.exception_handler(llm.LLMError)
async def llm_error(_, e: llm.LLMError):
    return JSONResponse({"detail": str(e)}, status_code=502)


def need_brain() -> dict:
    b = store.get_brain()
    if not b:
        raise HTTPException(409, "Noch kein Scan vorhanden.")
    return b


# ---------- Einstellungen ----------
@app.get("/api/settings")
async def get_settings():
    s = db.get_settings()
    s["ha_token_set"] = bool(s.pop("ha_token"))
    return s


@app.put("/api/settings")
async def put_settings(body: dict):
    if not body.get("ha_token"):
        body.pop("ha_token", None)  # leer = unverändert lassen
    db.set_settings(body)
    return await get_settings()


@app.post("/api/test/ha")
async def test_ha():
    ha = from_settings(db.get_settings())
    try:
        cfg = await ha.get("/api/config")
    finally:
        await ha.close()
    return {"ok": True, "version": cfg.get("version"), "location": cfg.get("location_name")}


@app.get("/api/ollama/models")
async def ollama_models():
    return await llm.list_models(db.get_settings())


# ---------- Scan / Gehirn ----------
_scan_lock = asyncio.Lock()


_scan_tasks = 0


def schedule_scan() -> None:
    """Hintergrund-Scan einreihen; der Status wird sofort gesetzt (die Sperre serialisiert die Läufe)."""
    global _scan_tasks
    _scan_tasks += 1
    jobs["scan"].update(running=True, error=None, msg="Starte …")
    asyncio.create_task(run_scan())


async def do_scan(progress):
    async with _scan_lock:  # nie zwei Scans gleichzeitig (der ältere würde den neueren überschreiben)
        return await _do_scan(progress)


async def _do_scan(progress):
    s = db.get_settings()
    ha = from_settings(s)
    try:
        brain = await scanner.scan(ha, progress)
        progress("Analysiere Logik …")
        refs, findings = analyzer.analyze(brain)
        if s["backup_on_scan"]:
            progress("Erstelle Backup …")
            backup.write(brain["items"], "scan", brain["ha_version"])
        store.save_brain(brain, refs, findings)
        return store.get_brain()  # inkl. Querverweisen
    finally:
        await ha.close()


async def run_scan():
    job = jobs["scan"]
    job.update(running=True, error=None, msg="Starte …")
    try:
        brain = await do_scan(lambda m: job.update(msg=m))
        job["msg"] = f"Fertig: {len(brain['entities'])} Entitäten, {len(brain['items'])} Automationen/Skripte/Szenen"
    except Exception as e:  # Fehler in der UI anzeigen
        job["error"] = str(e)
        job["msg"] = "Fehlgeschlagen"
    finally:
        global _scan_tasks
        _scan_tasks -= 1
        job["running"] = _scan_tasks > 0


MAX_SUGGESTIONS = int(os.environ.get("MAX_SUGGESTIONS", "12"))


async def run_all():
    """Alles in einem: scannen, analysieren, dann die KI Vorschläge erzeugen lassen."""
    job = jobs["run"]
    job.update(running=True, error=None, ai_error=None, msg="Starte …", done=0, total=0)
    try:
        brain = await do_scan(lambda m: job.update(msg=m))
        suggest.retire_stale(brain)
        suggest.propose_dead_deletions(brain)
        todo = suggest.candidates(brain, MAX_SUGGESTIONS)
        job["total"] = len(todo)
        s = db.get_settings()
        for n, (it, fs) in enumerate(todo, 1):
            job.update(msg=f"KI prüft „{it['alias']}“ ({n}/{len(todo)})", done=n - 1)
            try:
                await suggest.suggest_for(s, brain, it, fs)
            except llm.LLMError as e:
                job["ai_error"] = str(e)
                break
        job.update(msg="Fertig", done=len(todo))
    except Exception as e:
        job.update(error=str(e), msg="Fehlgeschlagen")
    finally:
        job["running"] = False


@app.post("/api/scan")
async def start_scan():
    if jobs["scan"]["running"]:
        raise HTTPException(409, "Scan läuft bereits.")
    schedule_scan()
    return jobs["scan"]


@app.post("/api/run")
async def start_run():
    if jobs["run"]["running"] or jobs["scan"]["running"]:
        raise HTTPException(409, "Läuft bereits.")
    jobs["run"].update(running=True, error=None, ai_error=None, msg="Starte …", done=0, total=0)
    asyncio.create_task(run_all())
    return jobs["run"]


@app.get("/api/home")
async def home():
    """Alles, was die einzige Seite der UI braucht."""
    s = db.get_settings()
    b = store.get_brain()
    out = {"configured": bool(s["ha_url"] and s["ha_token"]), "read_only": s["read_only"],
           "run": jobs["run"], "scanned_at": b["scanned_at"] if b else None, "proposals": [], "notes": []}
    if not b:
        return out
    out["counts"] = {"automations": sum(1 for i in b["items"] if i["kind"] == "automation"),
                     "entities": len(b["entities"])}
    pend = db.q("SELECT * FROM proposals WHERE status='pending' ORDER BY id DESC")
    out["proposals"] = [chat.proposal_view(p) for p in pend]
    covered = {p["target_id"] for p in pend}
    for f in db.q("SELECT * FROM findings WHERE severity != 'info' ORDER BY CASE severity WHEN 'high' THEN 0 "
                  "WHEN 'medium' THEN 1 ELSE 2 END, id"):
        if f["category"] in suggest.FIXABLE and f["item_id"] in covered:
            continue
        out["notes"].append({k: f[k] for k in ("severity", "title", "detail")})
    out["notes"] = out["notes"][:40]
    return out


@app.get("/api/scan/status")
async def scan_status():
    b = store.get_brain()
    return {**jobs["scan"], "last_scan": b["scanned_at"] if b else None}


@app.get("/api/brain")
async def brain_summary():
    b = store.get_brain()
    if not b:
        return {"scanned": False}
    by_domain: dict[str, int] = {}
    for e in b["entities"]:
        d = e.split(".", 1)[0]
        by_domain[d] = by_domain.get(d, 0) + 1
    area_counts: dict[str, int] = {}
    for e in b["entities"].values():
        if e.get("area_id"):
            n = b["areas"].get(e["area_id"], "?")
            area_counts[n] = area_counts.get(n, 0) + 1
    sev = {r["severity"]: r["n"] for r in db.q("SELECT severity, COUNT(*) n FROM findings GROUP BY severity")}
    kinds = {k: sum(1 for i in b["items"] if i["kind"] == k) for k in ("automation", "script", "scene")}
    used = {e for r in b["refs"].values() for k in ("trigger", "condition", "action", "other") for e in r[k]}
    return {"scanned": True, "scanned_at": b["scanned_at"], "ha_version": b["ha_version"], "location": b["location"],
            "entities": len(b["entities"]), "devices": len(b["devices"]), "areas": len(b["areas"]),
            "kinds": kinds, "domains": dict(sorted(by_domain.items(), key=lambda x: -x[1])[:15]),
            "area_entities": dict(sorted(area_counts.items(), key=lambda x: -x[1])),
            "findings": sev, "unreferenced_entities": len(set(b["entities"]) - used),
            "reviewed": db.q1("SELECT COUNT(*) n FROM reviews")["n"]}


@app.get("/api/findings")
async def findings(severity: str | None = None, item_id: str | None = None):
    sql, args = "SELECT * FROM findings", ()
    if severity:
        sql, args = sql + " WHERE severity=?", (severity,)
    elif item_id:
        sql, args = sql + " WHERE item_id=?", (item_id,)
    order = "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, id"
    return db.q(f"{sql} {order}", args)


@app.get("/api/items")
async def items(kind: str = "automation"):
    b = need_brain()
    fcount = {r["item_id"]: r["n"] for r in db.q(
        "SELECT item_id, COUNT(*) n FROM findings WHERE item_kind=? GROUP BY item_id", (kind,))}
    out = []
    for it in b["items"]:
        if it["kind"] != kind:
            continue
        rv = store.get_review(kind, it["id"])
        out.append({"id": it["id"], "alias": it["alias"], "state": it["state"], "editable": it["editable"],
                    "last_triggered": it["last_triggered"], "findings": fcount.get(it["id"], 0),
                    "verdict": rv["verdict"] if rv else None})
    return sorted(out, key=lambda i: (-i["findings"], i["alias"].lower()))


@app.get("/api/items/{kind}/{item_id}")
async def item_detail(kind: str, item_id: str):
    b = need_brain()
    it = store.find_item(b, kind, item_id)
    if not it:
        raise HTTPException(404)
    refs = b["refs"].get(analyzer.item_key(kind, item_id), {})
    return {**{k: it[k] for k in ("kind", "id", "alias", "state", "last_triggered", "editable", "entity_id")},
            "yaml": store.to_yaml(it["config"]) if it["config"] else None,
            "refs": {k: [store.describe_entity(b, e) for e in refs.get(k, [])]
                     for k in ("trigger", "condition", "action")},
            "missing": refs.get("missing", []),
            "findings": db.q("SELECT * FROM findings WHERE item_kind=? AND item_id=?", (kind, item_id)),
            "review": store.get_review(kind, item_id)}


@app.get("/api/entities")
async def entities(q: str = "", limit: int = 100):
    b = need_brain()
    q = q.lower()
    out = []
    for eid, e in b["entities"].items():
        if q in eid.lower() or q in (e["name"] or "").lower():
            out.append({"entity_id": eid, **{k: e[k] for k in ("name", "state", "platform")},
                        "area": b["areas"].get(e["area_id"]) if e.get("area_id") else None})
            if len(out) >= limit:
                break
    return out


# ---------- KI-Review ----------
async def run_review(force: bool):
    job = jobs["review"]
    s = db.get_settings()
    b = need_brain()
    todo = [i for i in b["items"] if i["kind"] == "automation" and i["config"] is not None
            and (force or review.needs_review(i))]
    job.update(running=True, done=0, total=len(todo), error=None, cancel=False, current="")
    try:
        for it in todo:
            if job["cancel"]:
                break
            job["current"] = it["alias"]
            fs = db.q("SELECT * FROM findings WHERE item_kind=? AND item_id=?", (it["kind"], it["id"]))
            await review.review_item(s, b, it, fs)
            job["done"] += 1
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["running"] = False


@app.post("/api/review/start")
async def review_start(force: bool = False):
    need_brain()
    if jobs["review"]["running"]:
        raise HTTPException(409, "Review läuft bereits.")
    asyncio.create_task(run_review(force))
    return {"started": True}


@app.post("/api/review/cancel")
async def review_cancel():
    jobs["review"]["cancel"] = True
    return {"ok": True}


@app.get("/api/review/status")
async def review_status():
    return jobs["review"]


# ---------- Chat & Vorschläge ----------
@app.post("/api/chat")
async def chat_post(body: dict):
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Nachricht fehlt.")
    return await chat.handle_message(db.get_settings(), body.get("conv_id"), msg, body.get("revise_proposal"))


@app.get("/api/conversations/{cid}")
async def conversation(cid: int):
    msgs = db.q("SELECT role, content, proposal_id, ts FROM messages WHERE conv_id=? ORDER BY id", (cid,))
    for m in msgs:
        p = db.q1("SELECT * FROM proposals WHERE id=?", (m["proposal_id"],)) if m["proposal_id"] else None
        m["proposal"] = chat.proposal_view(p) if p and m["role"] == "assistant" else None
    return msgs


@app.get("/api/conversations")
async def conversations():
    return db.q("SELECT * FROM conversations ORDER BY id DESC LIMIT 30")


@app.get("/api/proposals")
async def proposals(status: str | None = None):
    rows = db.q("SELECT * FROM proposals" + (" WHERE status=?" if status else "") + " ORDER BY id DESC",
                (status,) if status else ())
    return [chat.proposal_view(p) for p in rows]


def get_proposal(pid: int) -> dict:
    p = db.q1("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404)
    return p


@app.post("/api/proposals/{pid}/reject")
async def reject(pid: int):
    get_proposal(pid)
    db.x("UPDATE proposals SET status='rejected' WHERE id=? AND status='pending'", (pid,))
    return chat.proposal_view(get_proposal(pid))


@app.post("/api/proposals/{pid}/approve")
async def approve(pid: int, body: dict | None = None):
    """Backup -> schreiben. Bei aktiver Schreibsperre nur mit ausdrücklicher Bestätigung für diesen einen Vorschlag."""
    p = get_proposal(pid)
    s = db.get_settings()
    if s["read_only"] and not (body or {}).get("confirm_write"):
        raise HTTPException(403, "Schreibsperre aktiv. Es wird nichts an Home Assistant geändert.")
    if p["status"] != "pending":
        raise HTTPException(409, f"Vorschlag hat Status „{p['status']}“.")
    ha = from_settings(s)
    try:
        bname = await backup.create_live(ha, f"vor-vorschlag-{pid}")
        if p["action"] == "delete":
            await ha.delete(f"/api/config/automation/config/{p['target_id']}")
        else:
            cfg = json.loads(p["new_config"])
            target = p["target_id"] if p["target_id"] != "new" else str(int(time.time() * 1000))
            cfg["id"] = target
            await ha.post(f"/api/config/automation/config/{target}", cfg)
        db.x("UPDATE proposals SET status='applied', backup=?, applied=datetime('now'), error=NULL WHERE id=?",
             (bname, pid))
    except HAError as e:
        db.x("UPDATE proposals SET status='failed', error=? WHERE id=?", (str(e), pid))
        raise
    finally:
        await ha.close()
    db.x("UPDATE proposals SET status='superseded' WHERE target_id=? AND status='pending' AND id != ?",
         (p["target_id"], pid))
    schedule_scan()  # Gehirn auffrischen
    return chat.proposal_view(get_proposal(pid))


# ---------- Backups ----------
@app.get("/api/backups")
async def backups():
    return backup.list_backups()


@app.post("/api/backups")
async def backup_now():
    ha = from_settings(db.get_settings())
    try:
        return {"name": await backup.create_live(ha, "manuell")}
    finally:
        await ha.close()


@app.get("/api/backups/{name}")
async def backup_get(name: str):
    try:
        return backup.read(name)
    except (ValueError, OSError):
        raise HTTPException(404)


@app.post("/api/backups/{name}/restore/{kind}/{item_id}")
async def backup_restore(name: str, kind: str, item_id: str):
    s = db.get_settings()
    if s["read_only"]:
        raise HTTPException(403, "Schreibsperre aktiv.")
    if kind not in ("automation", "script", "scene"):
        raise HTTPException(400)
    try:
        cfg = backup.read(name).get(kind, {}).get(item_id)
    except (ValueError, OSError):
        raise HTTPException(404)
    if cfg is None:
        raise HTTPException(404, "Eintrag nicht im Backup.")
    ha = from_settings(s)
    try:
        await backup.create_live(ha, "vor-restore")
        await ha.post(f"/api/config/{kind}/config/{item_id}", cfg)
    finally:
        await ha.close()
    schedule_scan()
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")

"""Fake Home Assistant + Fake Ollama für Tests."""
import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request, WebSocket

STATES = [
    {"entity_id": "light.flur", "state": "on", "attributes": {"friendly_name": "Flurlicht"}},
    {"entity_id": "light.kueche", "state": "off", "attributes": {"friendly_name": "Küchenlicht"}},
    {"entity_id": "binary_sensor.flur_bewegung", "state": "off", "attributes": {"friendly_name": "Flur Bewegung"}},
    {"entity_id": "sensor.tuer_batterie", "state": "12", "attributes": {"friendly_name": "Tür", "battery_level": 12}},
    {"entity_id": "switch.kaputt", "state": "unavailable", "attributes": {"friendly_name": "Kaputt"}},
    {"entity_id": "automation.flur_an", "state": "on",
     "attributes": {"id": "a1", "friendly_name": "Flur an", "last_triggered": "2026-09-30T10:00:00+00:00"}},
    {"entity_id": "automation.flur_aus", "state": "on",
     "attributes": {"id": "a2", "friendly_name": "Flur aus", "last_triggered": None}},
    {"entity_id": "automation.polling", "state": "on",
     "attributes": {"id": "a3", "friendly_name": "Polling", "last_triggered": "2026-10-01T10:00:00+00:00"}},
    {"entity_id": "automation.defekt", "state": "off", "attributes": {"id": "a4", "friendly_name": "Defekt"}},
]
CONFIGS = {
    "automation": {
        "a1": {"id": "a1", "alias": "Flur an", "mode": "single",
               "triggers": [{"trigger": "state", "entity_id": "binary_sensor.flur_bewegung", "to": "on"}],
               "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.flur"}}]},
        "a2": {"id": "a2", "alias": "Flur aus", "mode": "single",
               "trigger": [{"platform": "state", "entity_id": "binary_sensor.flur_bewegung", "to": "on"}],
               "action": [{"service": "light.turn_off", "target": {"entity_id": "light.flur"}}]},
        "a3": {"id": "a3", "alias": "Polling",
               "trigger": [{"platform": "time_pattern", "seconds": "/10"}],
               "action": [{"service": "light.turn_on", "entity_id": "light.wohnzimmer"}]},
        "a4": {"id": "a4", "alias": "Defekt", "trigger": [], "action": []},
    }
}
POSTS = []


def make_ha():
    app = FastAPI()

    @app.get("/api/config")
    def cfg():
        return {"version": "2026.10.0", "location_name": "Zuhause"}

    @app.get("/api/states")
    def states():
        return STATES

    @app.get("/api/services")
    def services():
        return [{"domain": d, "services": {}} for d in ("light", "switch", "automation", "script")]

    @app.get("/api/config/{kind}/config/{cid}")
    def get_cfg(kind: str, cid: str):
        from fastapi import HTTPException
        c = CONFIGS.get(kind, {}).get(cid)
        if c is None:
            raise HTTPException(404)
        return c

    @app.post("/api/config/{kind}/config/{cid}")
    async def post_cfg(kind: str, cid: str, request: Request):
        body = await request.json()
        POSTS.append((kind, cid, body))
        CONFIGS.setdefault(kind, {})[cid] = body
        return {"result": "ok"}

    @app.delete("/api/config/{kind}/config/{cid}")
    def del_cfg(kind: str, cid: str):
        POSTS.append((kind, cid, "DELETE"))
        CONFIGS.get(kind, {}).pop(cid, None)
        return {"result": "ok"}

    @app.websocket("/api/websocket")
    async def ws(sock: WebSocket):
        await sock.accept()
        await sock.send_json({"type": "auth_required"})
        await sock.receive_json()
        await sock.send_json({"type": "auth_ok"})
        data = {
            "config/entity_registry/list": [
                {"entity_id": "light.flur", "id": "f" * 32, "device_id": "dev1", "area_id": "flur", "platform": "hue"}],
            "config/device_registry/list": [{"id": "dev1", "name": "Hue", "area_id": "flur"}],
            "config/area_registry/list": [{"area_id": "flur", "name": "Flur"}],
        }
        while True:
            try:
                m = await sock.receive_json()
            except Exception:
                return
            await sock.send_json({"id": m["id"], "type": "result", "success": True, "result": data[m["type"]]})

    return app


OLLAMA_CALLS = []


def make_ollama():
    app = FastAPI()

    @app.get("/api/tags")
    def tags():
        return {"models": [{"name": "hermes3:8b", "size": 4_700_000_000}]}

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        OLLAMA_CALLS.append(body)
        sys_prompt = body["messages"][0]["content"]
        if "Erkannte Probleme" in body["messages"][-1]["content"]:
            out = {"title": "Problem beheben", "explanation": "Test-Erklärung.",
                   "config": {"alias": "Repariert", "mode": "restart",
                              "triggers": [{"trigger": "state", "entity_id": "binary_sensor.flur_bewegung", "to": "on"}],
                              "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.flur"}}]}}
        elif "bewertest eine Automation" in sys_prompt:
            out = {"verdict": "verbesserbar", "summary": "Test", "issues": ["x"], "suggestion": "y"}
        else:
            out = {"reply": "Ich schlage vor, den Konflikt zu beheben.",
                   "proposal": {"target_id": "a2", "title": "Flur aus entschärfen",
                                "explanation": "Nur ausschalten, wenn keine Bewegung mehr.",
                                "config": {"alias": "Flur aus", "mode": "restart",
                                           "triggers": [{"trigger": "state", "entity_id": "binary_sensor.flur_bewegung",
                                                         "to": "off", "for": "00:05:00"}],
                                           "actions": [{"action": "light.turn_off",
                                                        "target": {"entity_id": "light.flurr"}}]}}}
        return {"message": {"role": "assistant", "content": "```json\n" + json.dumps(out) + "\n```"}}

    return app


def serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    return server

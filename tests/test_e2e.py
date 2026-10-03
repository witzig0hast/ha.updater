import os
import time

import pytest

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    from fastapi.testclient import TestClient
    from tests import fakes
    fakes.serve(fakes.make_ha(), 18123)
    fakes.serve(fakes.make_ollama(), 18434)
    from app import db
    from app.main import app
    db.set_settings({"ha_url": "http://127.0.0.1:18123", "ha_token": "t",
                     "ollama_url": "http://127.0.0.1:18434"})
    with TestClient(app) as c:
        yield c


def wait(client, url, key):
    for _ in range(100):
        s = client.get(url).json()
        if not s[key]:
            return s
        time.sleep(0.1)
    raise AssertionError("timeout")


def test_scan_and_findings(client):
    assert client.post("/api/test/ha").json()["version"] == "2026.10.0"
    client.post("/api/scan")
    s = wait(client, "/api/scan/status", "running")
    assert s["error"] is None, s
    b = client.get("/api/brain").json()
    assert b["kinds"]["automation"] == 4 and b["areas"] == 1
    titles = [f["title"] for f in client.get("/api/findings").json()]
    joined = "\n".join(titles)
    assert "nicht existierende Entitäten" in joined      # light.wohnzimmer
    assert "pollt per time_pattern" in joined
    assert "Mögliche Konflikte" in joined                # a1 an vs a2 aus
    assert "kein Trigger" in joined or "keinen Trigger" in joined
    assert "niedriger Batterie" in joined
    assert "unavailable" in joined
    assert len(client.get("/api/backups").json()) == 1


def test_item_detail(client):
    d = client.get("/api/items/automation/a1").json()
    assert d["refs"]["trigger"][0]["entity_id"] == "binary_sensor.flur_bewegung"
    assert "triggers" in d["yaml"]


def test_review(client):
    client.post("/api/review/start")
    r = wait(client, "/api/review/status", "running")
    assert r["error"] is None and r["done"] == 4
    assert client.get("/api/items/automation/a1").json()["review"]["verdict"] == "verbesserbar"


def test_chat_proposal_readonly_gate_and_apply(client):
    from tests import fakes
    r = client.post("/api/chat", json={"message": "Das Flurlicht geht aus obwohl ich noch da bin"}).json()
    p = r["proposal"]
    assert p and p["target_id"] == "a2" and p["status"] == "pending"
    assert any("light.flurr" in w for w in p["warnings"])        # Halluzinierte Entität wird markiert
    assert "-    - platform: state" in p["diff"] or "+" in p["diff"]
    # Schreibsperre standardmäßig aktiv
    assert client.post(f"/api/proposals/{p['id']}/approve").status_code == 403
    assert fakes.POSTS == []
    client.put("/api/settings", json={"read_only": False})
    a = client.post(f"/api/proposals/{p['id']}/approve").json()
    assert a["status"] == "applied" and a["backup"]
    assert fakes.POSTS[-1][1] == "a2" and fakes.POSTS[-1][2]["mode"] == "restart"
    wait(client, "/api/scan/status", "running")
    # Backup enthält den ALTEN Stand
    old = client.get(f"/api/backups/{a['backup']}").json()["automation"]["a2"]
    assert "trigger" in old and old.get("mode") == "single"


def test_revise_supersedes(client):
    r1 = client.post("/api/chat", json={"message": "Flurlicht Konflikt"}).json()
    r2 = client.post("/api/chat", json={"message": "mach 10 Minuten", "conv_id": r1["conv_id"],
                                        "revise_proposal": r1["proposal"]["id"]}).json()
    ps = {p["id"]: p["status"] for p in client.get("/api/proposals").json()}
    assert ps[r1["proposal"]["id"]] == "superseded" and ps[r2["proposal"]["id"]] == "pending"
    system = [c for c in __import__("tests.fakes", fromlist=["x"]).OLLAMA_CALLS][-1]["messages"][0]["content"]
    assert "Bisheriger Vorschlag" in system

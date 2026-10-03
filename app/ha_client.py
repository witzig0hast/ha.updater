"""Schmaler Home-Assistant-Client (REST + WebSocket)."""
import contextlib
import json
import ssl

import httpx
import websockets


class HAError(Exception):
    pass


class HA:
    def __init__(self, url: str, token: str, verify_ssl: bool = True):
        if not url or not token:
            raise HAError("Home-Assistant-URL und Token fehlen (Einstellungen).")
        self.url = url.rstrip("/")
        self.token = token
        self.verify_ssl = verify_ssl
        self.client = httpx.AsyncClient(
            base_url=self.url, verify=verify_ssl, timeout=30,
            headers={"Authorization": f"Bearer {token}"})

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, optional: bool = False):
        try:
            r = await self.client.get(path)
        except httpx.HTTPError as e:
            raise HAError(f"Verbindung zu Home Assistant fehlgeschlagen: {e}") from e
        if r.status_code == 404 and optional:
            return None
        if r.status_code == 401:
            raise HAError("Token ungültig (401).")
        if r.status_code >= 400:
            raise HAError(f"GET {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def post(self, path: str, body: dict):
        try:
            r = await self.client.post(path, json=body)
        except httpx.HTTPError as e:
            raise HAError(f"Verbindung zu Home Assistant fehlgeschlagen: {e}") from e
        if r.status_code >= 400:
            raise HAError(f"POST {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    async def delete(self, path: str):
        try:
            r = await self.client.delete(path)
        except httpx.HTTPError as e:
            raise HAError(f"Verbindung zu Home Assistant fehlgeschlagen: {e}") from e
        if r.status_code >= 400:
            raise HAError(f"DELETE {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    @contextlib.asynccontextmanager
    async def _ws(self):
        """Verbundene, authentifizierte WebSocket-Sitzung."""
        ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        kwargs = {}
        if ws_url.startswith("wss") and not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        try:
            async with websockets.connect(ws_url, max_size=None, **kwargs) as ws:
                await ws.recv()  # auth_required
                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    raise HAError("WebSocket-Authentifizierung fehlgeschlagen.")
                yield ws
        except (OSError, websockets.WebSocketException) as e:
            raise HAError(f"WebSocket-Fehler: {e}") from e

    async def ws_commands(self, types: list[str]) -> dict:
        """Führt mehrere WebSocket-Listenabfragen in einer Sitzung aus."""
        async with self._ws() as ws:
            out = {}
            for i, t in enumerate(types, 1):
                await ws.send(json.dumps({"id": i, "type": t}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == i:
                        break
                if not m.get("success"):
                    raise HAError(f"WebSocket {t}: {m.get('error')}")
                out[t] = m["result"]
            return out

    async def ws_command(self, msg: dict):
        """Einzelner WebSocket-Befehl (z. B. eine Entität aus der Registry entfernen)."""
        async with self._ws() as ws:
            await ws.send(json.dumps({"id": 1, **msg}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == 1:
                    break
            if not m.get("success"):
                raise HAError(f"WebSocket {msg.get('type')}: {m.get('error')}")
            return m.get("result")

    async def remove_entity(self, entity_id: str):
        """Entfernt eine Entität aus der Registry. Taucht sie wieder auf, legt HA sie automatisch neu an."""
        return await self.ws_command({"type": "config/entity_registry/remove", "entity_id": entity_id})


def from_settings(s: dict) -> HA:
    return HA(s["ha_url"], s["ha_token"], s.get("ha_verify_ssl", True))

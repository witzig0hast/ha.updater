"""Ollama-Anbindung (läuft auf dem Host). Alle Parameter sind auf geringen VRAM-Bedarf getrimmt."""
import json
import re

import httpx


class LLMError(Exception):
    pass


async def list_models(s: dict) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{s['ollama_url'].rstrip('/')}/api/tags")
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama nicht erreichbar ({s['ollama_url']}): {e}") from e
    return [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)} for m in r.json().get("models", [])]


async def chat(s: dict, messages: list[dict], json_mode: bool = True, num_predict: int = 1500) -> str:
    payload = {
        "model": s["model"], "messages": messages, "stream": False,
        "keep_alive": s["keep_alive"],
        "options": {"num_ctx": int(s["num_ctx"]), "temperature": float(s["temperature"]),
                    "num_predict": num_predict},
    }
    if json_mode:
        payload["format"] = "json"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as c:
            r = await c.post(f"{s['ollama_url'].rstrip('/')}/api/chat", json=payload)
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama nicht erreichbar ({s['ollama_url']}): {e}") from e
    if r.status_code != 200:
        raise LLMError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")
    return r.json()["message"]["content"]


def parse_json(text: str) -> dict | None:
    """Kleine Modelle verpacken JSON gern in Text/Codeblöcke – robust herausschälen."""
    text = text.strip()
    for cand in (text, re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()):
        try:
            v = json.loads(cand)
            return v if isinstance(v, dict) else None
        except ValueError:
            pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None

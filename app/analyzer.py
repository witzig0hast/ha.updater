"""Regelbasierte Analyse (ohne LLM, praktisch kostenlos): Querverweise + Findings."""
import datetime as dt
import hashlib
import json
import re
from collections import defaultdict

ENTITY_RE = re.compile(r"\b([a-z_]+)\.([a-z0-9_]+)\b")
EID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
TEMPLATE_REF_RE = re.compile(
    r"""(?:states|is_state|state_attr|is_state_attr|has_value|is_hidden_entity)\(\s*['"]([a-z_]+\.[a-z0-9_]+)['"]""")
STATES_DOT_RE = re.compile(r"\bstates\.([a-z_]+)\.([a-z0-9_]+)")
SECTION = {"trigger": "trigger", "triggers": "trigger", "condition": "condition",
           "conditions": "condition", "action": "action", "actions": "action",
           "sequence": "action"}
STALE_DAYS = 60


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def cfg_hash(cfg) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:12]


def item_key(kind: str, item_id: str) -> str:
    return f"{kind}:{item_id}"


def extract_refs(cfg: dict, entities: dict, devices: dict, domains: set, reg_ids: dict) -> dict:
    """Welche Entitäten kommen in Trigger/Condition/Action vor, welche existieren nicht?"""
    found = {"trigger": set(), "condition": set(), "action": set(), "other": set()}
    missing, dev_missing = set(), set()

    def add_eid(s: str, role: str):
        if s in entities:
            found[role].add(s)
        elif UUID_RE.match(s) and s in reg_ids:
            found[role].add(reg_ids[s])
        elif EID_RE.match(s) and s.split(".", 1)[0] in domains:
            missing.add(s)

    def visit(node, role):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "entity_id":
                    for s in as_list(v):
                        if isinstance(s, str) and "{{" not in s:
                            add_eid(s, role)
                        elif isinstance(s, str):
                            visit(s, role)
                elif k == "device_id" and isinstance(v, str):
                    if v not in devices:
                        dev_missing.add(v)
                else:
                    visit(v, role)
        elif isinstance(node, list):
            for v in node:
                visit(v, role)
        elif isinstance(node, str):
            for m in ENTITY_RE.finditer(node):
                if m.group(0) in entities:
                    found[role].add(m.group(0))
            for m in TEMPLATE_REF_RE.finditer(node):
                if m.group(1) not in entities:
                    missing.add(m.group(1))
            for m in STATES_DOT_RE.finditer(node):
                e = f"{m.group(1)}.{m.group(2)}"
                if e not in entities and m.group(1) in domains:
                    missing.add(e)

    for key, val in (cfg or {}).items():
        visit(val, SECTION.get(key, "other"))
    return {**{k: sorted(v) for k, v in found.items()},
            "missing": sorted(missing), "missing_devices": sorted(dev_missing)}


def service_calls(cfg) -> list[tuple[str, set]]:
    """Alle Service-Aufrufe (z.B. light.turn_on) mit ihren Ziel-Entitäten."""
    out = []

    def targets(n: dict) -> set:
        t = set()
        for src in (n, n.get("target") or {}, n.get("data") or {}):
            if isinstance(src, dict):
                for s in as_list(src.get("entity_id")):
                    if isinstance(s, str) and EID_RE.match(s):
                        t.add(s)
        return t

    def visit(n):
        if isinstance(n, dict):
            svc = n.get("action") or n.get("service")
            if isinstance(svc, str) and EID_RE.match(svc):
                out.append((svc, targets(n)))
            for v in n.values():
                visit(v)
        elif isinstance(n, list):
            for v in n:
                visit(v)

    visit(cfg)
    return out


def triggers_of(cfg: dict) -> list[dict]:
    return [t for t in as_list(cfg.get("triggers", cfg.get("trigger"))) if isinstance(t, dict)]


def actions_of(cfg: dict):
    return as_list(cfg.get("actions", cfg.get("action", cfg.get("sequence"))))


def trig_type(t: dict) -> str:
    return t.get("trigger") or t.get("platform") or ""


def parse_ts(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def analyze(brain: dict) -> tuple[dict, list[dict]]:
    """Gibt (refs, findings) zurück. refs: item_key -> Querverweise."""
    ents, devices = brain["entities"], brain["devices"]
    domains = {e.split(".", 1)[0] for e in ents} | set(brain.get("service_domains", []))
    reg_ids = {e["reg_id"]: eid for eid, e in ents.items() if e.get("reg_id")}
    now = dt.datetime.now(dt.timezone.utc)
    findings: list[dict] = []
    refs: dict = {}

    def add(sev, cat, title, detail="", item=None):
        findings.append({"severity": sev, "category": cat, "title": title, "detail": detail,
                         "item_kind": item["kind"] if item else None,
                         "item_id": item["id"] if item else None})

    sig_groups = defaultdict(list)       # identische Trigger+Aktion
    trig_entity_idx = defaultdict(list)  # Trigger-Entität -> [(item, {service: targets})]
    used_entities, called_scripts = set(), set()
    legacy = 0

    for it in brain["items"]:
        cfg = it["config"]
        name = f"„{it['alias']}“"
        key = item_key(it["kind"], it["id"])
        if cfg is None:
            refs[key] = {"trigger": [], "condition": [], "action": [], "other": [],
                         "missing": [], "missing_devices": []}
            if it["kind"] == "automation":
                add("info", "nicht_lesbar", f"{name} nicht über die API lesbar",
                    "Vermutlich in YAML ohne `id:` definiert – kann nicht automatisch bearbeitet werden.", it)
            continue

        r = refs[key] = extract_refs(cfg, ents, devices, domains, reg_ids)
        for lst in ("trigger", "condition", "action", "other"):
            used_entities.update(r[lst])
        calls = service_calls(cfg)
        for svc, _ in calls:
            if svc.startswith("script."):
                called_scripts.add(svc.split(".", 1)[1])
        for e in r["action"]:
            if e.startswith("script."):
                called_scripts.add(e.split(".", 1)[1])

        if r["missing"]:
            add("high", "fehlende_entitaet", f"{name} verweist auf nicht existierende Entitäten",
                ", ".join(r["missing"]), it)
        if r["missing_devices"]:
            add("high", "fehlendes_geraet", f"{name} verweist auf gelöschte Geräte",
                ", ".join(r["missing_devices"]), it)

        if it["kind"] != "automation":
            continue

        trigs, acts = triggers_of(cfg), actions_of(cfg)
        if not trigs:
            add("high", "kein_trigger", f"{name} hat keinen Trigger", "Läuft nie von selbst.", it)
        if not acts:
            add("high", "keine_aktion", f"{name} hat keine Aktion", "", it)
        if any("platform" in t for t in trigs) or "service" in json.dumps(cfg):
            legacy += 1

        bad = [e for e in r["trigger"] + r["action"]
               if ents[e]["state"] in ("unavailable", "unknown") and not ents[e]["disabled"]]
        if bad:
            add("medium", "nicht_verfuegbar", f"{name} nutzt nicht verfügbare Entitäten",
                ", ".join(bad[:10]), it)

        if it["state"] == "off":
            add("low", "deaktiviert", f"{name} ist deaktiviert", "Löschen oder reaktivieren?", it)
        else:
            last = parse_ts(it["last_triggered"])
            if last is None:
                add("medium", "nie_ausgeloest", f"{name} wurde noch nie ausgelöst",
                    "Trigger evtl. falsch oder Automation überflüssig.", it)
            elif (now - last).days > STALE_DAYS:
                add("low", "lange_nicht_ausgeloest", f"{name} seit {(now - last).days} Tagen nicht ausgelöst", "", it)

        for t in trigs:
            tt = trig_type(t)
            if tt == "time_pattern":
                sec, mins = str(t.get("seconds", "")), str(t.get("minutes", ""))
                if sec or mins in ("/1", "/2", "/3", "/5"):
                    add("medium", "polling", f"{name} pollt per time_pattern ({sec or mins})",
                        "Ein State-Trigger auf die auslösende Entität spart Rechenleistung.", it)
            elif tt == "template" and re.search(r"\bnow\(\)|utcnow\(\)", str(t.get("value_template", ""))):
                add("medium", "polling", f"{name} nutzt now() im Template-Trigger",
                    "Wird jede Minute neu ausgewertet. `time`/`sun`-Trigger oder Zeit-Condition nutzen.", it)

        if (any(isinstance(a, dict) and ("delay" in a or any(k.startswith("wait_") for k in a)) for a in acts)
                and cfg.get("mode", "single") == "single" and acts):
            add("low", "mode", f"{name}: delay/wait im Modus „single“",
                "Erneutes Auslösen während der Wartezeit wird verworfen – `mode: restart` prüfen.", it)

        sig = hashlib.sha1(json.dumps([cfg.get("trigger", cfg.get("triggers")), cfg.get("condition"),
                                       cfg.get("action", cfg.get("actions"))],
                                      sort_keys=True, default=str).encode()).hexdigest()
        sig_groups[sig].append(it)
        by_svc = defaultdict(set)
        for svc, tg in calls:
            by_svc[svc] |= tg
        for te in set(r["trigger"]):
            trig_entity_idx[te].append((it, by_svc))

    for group in sig_groups.values():
        if len(group) > 1:
            names = ", ".join(f"„{g['alias']}“" for g in group)
            add("medium", "duplikat", f"Identische Automationen: {names}", "Zusammenführen oder löschen.", group[0])

    # Mögliche Konflikte: gleicher Auslöser schaltet dasselbe Ziel an UND aus
    seen = set()
    for te, lst in trig_entity_idx.items():
        for i, (a, sa) in enumerate(lst):
            for b, sb in lst[i + 1:]:
                on = {t for s, tg in sa.items() if s.endswith("turn_on") for t in tg}
                off = {t for s, tg in sb.items() if s.endswith("turn_off") for t in tg}
                on2 = {t for s, tg in sb.items() if s.endswith("turn_on") for t in tg}
                off2 = {t for s, tg in sa.items() if s.endswith("turn_off") for t in tg}
                both = (on & off) | (on2 & off2)
                pair = frozenset((a["id"], b["id"]))
                if both and a["id"] != b["id"] and pair not in seen:
                    seen.add(pair)
                    add("medium", "konflikt",
                        f"Mögliche Konflikte: „{a['alias']}“ vs. „{b['alias']}“",
                        f"Gleicher Auslöser {te}, gegensätzliche Aktionen auf {', '.join(sorted(both)[:5])}", a)

    for it in brain["items"]:
        if it["kind"] == "script" and it["id"] not in called_scripts and it["entity_id"] not in used_entities:
            add("info", "ungenutztes_skript", f"Skript „{it['alias']}“ wird von keiner Automation aufgerufen",
                "Kann trotzdem von Dashboards/Sprachassistenten genutzt werden.", it)

    unavailable = [e for e, v in ents.items() if v["state"] == "unavailable" and not v["disabled"]
                   and e.split(".", 1)[0] not in ("update", "button", "scene")]
    if unavailable:
        add("medium", "nicht_verfuegbar", f"{len(unavailable)} Entitäten sind „unavailable“",
            ", ".join(sorted(unavailable)[:25]) + (" …" if len(unavailable) > 25 else ""))
    lowbat = [(e, v["battery"]) for e, v in ents.items()
              if isinstance(v.get("battery"), (int, float)) and v["battery"] < 20]
    if lowbat:
        add("medium", "batterie", f"{len(lowbat)} Geräte mit niedriger Batterie",
            ", ".join(f"{e} ({b}%)" for e, b in lowbat[:15]))
    if legacy:
        add("info", "syntax", f"{legacy} Automationen nutzen die ältere Syntax (platform:/service:)",
            "Funktioniert weiterhin; neue Schreibweise ist trigger:/action:.")

    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda f: order[f["severity"]])
    return refs, findings

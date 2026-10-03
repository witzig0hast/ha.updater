const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const SEV = {high:'Hoch', medium:'Mittel', low:'Niedrig', info:'Info'};
let settings = {}, convId = null, poll = null;

async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    method: opts.method || (opts.body ? 'POST' : 'GET'),
    headers: opts.body ? {'Content-Type': 'application/json'} : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const diffHtml = d => d.split('\n').filter(l => !l.startsWith('--- ') && !l.startsWith('+++ ')).map(l => `<span class="${l[0]==='+'?'a':l[0]==='-'?'d':l[0]==='@'?'h':''}">${esc(l)||' '}</span>`).join('');
const fmt = ts => ts ? new Date(ts).toLocaleString('de-DE') : '–';

const theme = {
  get() { try { return localStorage.getItem('theme') || 'dark'; } catch { return 'dark'; } },
  set(t) { document.documentElement.dataset.theme = t; try { localStorage.setItem('theme', t); } catch {} },
};
theme.set(theme.get());
document.addEventListener('click', e => { if (e.target.closest('#theme')) theme.set(theme.get() === 'dark' ? 'light' : 'dark'); });

async function loadSettings() {
  settings = await api('/settings');
  const l = $('#lock');
  l.className = 'lock ' + (settings.read_only ? 'ro' : 'rw');
  l.innerHTML = `<span class="dot"></span>${settings.read_only ? 'Nur lesen' : 'Schreiben erlaubt'}`;
}

const views = {chat, brain, items, proposals, backups, settings: settingsView};
async function route() {
  clearInterval(poll);
  const [name, ...rest] = (location.hash.slice(1) || 'chat').split('/');
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('on', a.dataset.v === name));
  await loadSettings();
  const v = $('#view'); v.classList.remove('enter'); void v.offsetWidth; v.classList.add('enter');
  try { await (views[name] || chat)(...rest.map(decodeURIComponent)); }
  catch (e) { $('#view').innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}
addEventListener('hashchange', route);

// ---------- Chat ----------
function proposalCard(p) {
  if (!p) return '';
  const st = {pending:'wartet auf Freigabe', applied:'angewendet', rejected:'abgelehnt', failed:'fehlgeschlagen', superseded:'überholt'}[p.status] || p.status;
  const warn = p.warnings.map(w => `<div class="note">${esc(w)}</div>`).join('');
  const acts = p.status === 'pending' ? `
    <div class="row" style="margin-top:10px">
      <button class="ok" onclick="approve(${p.id})" ${settings.read_only ? 'disabled title="Schreibsperre aktiv"' : ''}>Zustimmen &amp; anwenden</button>
      <button class="sec" onclick="revise(${p.id})">Änderung vorschlagen</button>
      <button class="bad" onclick="rejectP(${p.id})">Verwerfen</button>
    </div>${settings.read_only ? '<div class="mute" style="margin-top:6px">Schreibsperre aktiv – Anwenden ist in den Einstellungen gesperrt. Vorschlag bleibt gespeichert.</div>' : ''}` : '';
  return `<div class="card" style="margin-top:10px"><b>${p.is_new ? 'Neue Automation' : 'Änderung'}: ${esc(p.title)}</b>
    <span class="pill">${st}</span><div class="mute">${esc(p.explanation)}</div>${warn}
    <pre class="diff">${diffHtml(p.diff || p.new_yaml)}</pre>${p.error ? `<div class="err">${esc(p.error)}</div>` : ''}
    ${p.backup ? `<div class="mute">Backup vor Änderung: ${esc(p.backup)}</div>` : ''}${acts}</div>`;
}
const bubble = (role, text, p) => `<div class="msg ${role}"><div class="who">${role === 'user' ? 'Du' : 'HA-Fix'}</div><div class="txt">${esc(text)}</div>${proposalCard(p)}</div>`;

async function chat() {
  const b = await api('/brain');
  $('#view').innerHTML = `<h2>Was stört dich gerade?</h2>
    ${b.scanned ? '' : '<div class="note">Es wurde noch nichts gescannt. Lies zuerst dein System unter <a href="#brain">Gehirn</a> ein.</div>'}
    <div class="chips">${['Das Licht im Flur geht nachts zu spät aus','Eine Automation löst zu oft aus','Welche Automationen sind überflüssig?'].map(t => `<span class="chip" onclick="document.getElementById('msg').value=this.textContent">${t}</span>`).join('')}</div>
    <div class="card"><div id="chatlog"></div>
    <div id="revhint" class="mute"></div>
    <div class="row" style="margin-top:12px"><textarea id="msg" rows="2" placeholder="Beschreibe, was dich an einer Automation oder einem Ablauf stört …" style="flex:1"></textarea>
    <button id="send">Senden</button></div></div>`;
  if (convId) {
    const msgs = await api('/conversations/' + convId);
    $('#chatlog').innerHTML = msgs.map(m => bubble(m.role, m.content, m.proposal)).join('');
  }
  $('#send').onclick = send;
  $('#msg').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
}
let reviseId = null;
async function send() {
  const ta = $('#msg'), text = ta.value.trim();
  if (!text) return;
  const log = $('#chatlog'); ta.value = '';
  log.insertAdjacentHTML('beforeend', bubble('user', text));
  log.insertAdjacentHTML('beforeend', '<div class="msg assistant" id="think"><div class="who">HA-Fix</div><div class="mute">Einen Moment, das lokale Modell überlegt<span class="typing"><i></i><i></i><i></i></span></div></div>');
  log.scrollTop = log.scrollHeight; $('#send').disabled = true;
  try {
    const r = await api('/chat', {body: {message: text, conv_id: convId, revise_proposal: reviseId}});
    convId = r.conv_id; reviseId = null; $('#revhint').textContent = '';
    $('#think').outerHTML = bubble('assistant', r.reply, r.proposal);
  } catch (e) { $('#think').outerHTML = `<div class="msg assistant err">${esc(e.message)}</div>`; }
  $('#send').disabled = false; log.scrollTop = log.scrollHeight;
}
window.revise = id => { reviseId = id; $('#revhint').textContent = `Überarbeitung von Vorschlag #${id} – beschreibe, was anders sein soll.`; $('#msg').focus(); };
window.rejectP = async id => { await api(`/proposals/${id}/reject`, {method: 'POST'}); route(); };
window.approve = async id => {
  if (!confirm('Backup erstellen und diese Änderung in Home Assistant schreiben?')) return;
  try { await api(`/proposals/${id}/approve`, {method: 'POST'}); } catch (e) { alert(e.message); }
  route();
};

// ---------- Gehirn ----------
async function brain() {
  const b = await api('/brain'), st = await api('/scan/status'), rv = await api('/review/status');
  const sev = b.findings || {};
  $('#view').innerHTML = `<div class="row" style="justify-content:space-between"><h2>Gehirn</h2>
    <div class="row"><button id="scan">${b.scanned ? 'Neu scannen' : 'Gehirn aufbauen'}</button>
    <button class="sec" id="rev" ${b.scanned ? '' : 'disabled'}>KI-Review starten</button></div></div>
    <div id="jobs"></div>
    ${b.scanned ? `<div class="mute">${esc(b.location || '')} · HA ${esc(b.ha_version)} · Scan: ${fmt(b.scanned_at)}</div>
    <div class="grid" style="margin:14px 0">
      ${[['Entitäten', b.entities], ['Geräte', b.devices], ['Bereiche', b.areas], ['Automationen', b.kinds.automation],
         ['Skripte', b.kinds.script], ['Szenen', b.kinds.scene], ['Ohne Verweis', b.unreferenced_entities],
         ['KI-geprüft', `${b.reviewed}/${b.kinds.automation}`]].map(([l, v]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join('')}
    </div>
    <h3>Befunde (regelbasiert)</h3>
    <div class="chips" id="sevchips"><span class="chip" data-s="">Alle</span>${['high','medium','low','info'].map(s => `<span class="chip" data-s="${s}">${SEV[s]} (${sev[s] || 0})</span>`).join('')}</div>
    <div class="card" id="findings"></div>
    <div class="grid"><div class="card"><h3 style="margin-top:0">Entitäten je Domain</h3>${bars(b.domains)}</div>
    <div class="card"><h3 style="margin-top:0">Entitäten je Bereich</h3>${bars(b.area_entities)}</div></div>` : '<div class="card">Noch nichts gescannt. Trag zuerst in den <a href="#settings">Einstellungen</a> URL und Token ein.</div>'}`;
  $('#scan').onclick = async () => { try { await api('/scan', {method: 'POST'}); } catch (e) { alert(e.message); } tick(); };
  if ($('#rev')) $('#rev').onclick = async () => { try { await api('/review/start', {method: 'POST'}); } catch (e) { alert(e.message); } tick(); };
  document.querySelectorAll('#sevchips .chip').forEach(c => c.onclick = () => loadFindings(c.dataset.s));
  if (b.scanned) loadFindings('');
  async function tick() {
    const s = await api('/scan/status'), r = await api('/review/status');
    $('#jobs').innerHTML = (s.running || s.error || s.msg ? `<div class="card ${s.error ? 'err' : ''}">Scan: ${esc(s.error || s.msg)}</div>` : '') +
      (r.running || r.error ? `<div class="card ${r.error ? 'err' : ''}">KI-Review: ${r.error ? esc(r.error) : `${r.done}/${r.total} – ${esc(r.current)}`}
        ${r.running ? `<div class="bar"><i style="width:${r.total ? 100 * r.done / r.total : 0}%"></i></div><button class="sec" onclick="api('/review/cancel',{method:'POST'})">Abbrechen</button>` : ''}</div>` : '');
    if (s.running || r.running) { clearTimeout(poll); poll = setTimeout(tick, 1500); }
    else if (poll && location.hash.startsWith('#brain') && (s.msg.startsWith('Fertig'))) { poll = null; route(); }
  }
  tick(); if (st.running || rv.running) poll = setTimeout(tick, 1500);
}
const bars = o => { const m = Math.max(1, ...Object.values(o)); return Object.entries(o).map(([k, v]) => `<div class="row" style="margin:4px 0"><span style="width:130px;overflow:hidden;text-overflow:ellipsis">${esc(k)}</span><div class="bar" style="flex:1"><i style="width:${100 * v / m}%"></i></div><span class="mute">${v}</span></div>`).join(''); };
async function loadFindings(sev) {
  const f = await api('/findings' + (sev ? '?severity=' + sev : ''));
  $('#findings').innerHTML = f.length ? `<table>${f.map(x => `<tr class="${x.item_id ? 'click' : ''}" ${x.item_id ? `onclick="location.hash='#items/${x.item_kind}/${encodeURIComponent(x.item_id)}'"` : ''}>
    <td><span class="pill sev-${x.severity}">${SEV[x.severity]}</span></td><td>${esc(x.title)}<div class="mute">${esc(x.detail)}</div></td></tr>`).join('')}</table>` : '<span class="mute">Keine Befunde</span>';
}

// ---------- Automationen ----------
async function items(kind, id) {
  if (kind && id) return itemDetail(kind, id);
  const list = await api('/items?kind=automation');
  $('#view').innerHTML = `<h2>Automationen (${list.length})</h2><input id="flt" placeholder="Filtern …" style="margin-bottom:12px">
    <div class="card"><table><tr><th>Name</th><th>Status</th><th>Zuletzt</th><th>Befunde</th><th>KI</th></tr><tbody id="tb"></tbody></table></div>`;
  const draw = q => $('#tb').innerHTML = list.filter(i => i.alias.toLowerCase().includes(q)).map(i => `<tr class="click" onclick="location.hash='#items/automation/${encodeURIComponent(i.id)}'">
    <td>${esc(i.alias)}${i.editable ? '' : ' <span class="pill">nur lesbar</span>'}</td><td>${esc(i.state)}</td><td class="mute">${fmt(i.last_triggered)}</td>
    <td>${i.findings ? `<span class="pill sev-medium">${i.findings}</span>` : ''}</td><td>${i.verdict ? `<span class="pill v-${i.verdict}">${i.verdict}</span>` : ''}</td></tr>`).join('');
  draw(''); $('#flt').oninput = e => draw(e.target.value.toLowerCase());
}
async function itemDetail(kind, id) {
  const d = await api(`/items/${kind}/${encodeURIComponent(id)}`);
  const refs = ['trigger', 'condition', 'action'].map(k => d.refs[k].length ? `<h3>${{trigger:'Trigger',condition:'Bedingungen',action:'Aktionen'}[k]}</h3>` +
    d.refs[k].map(e => `<div><code>${esc(e.entity_id)}</code> ${esc(e.name || '')} <span class="mute">${esc(e.state)} ${e.area ? '· ' + esc(e.area) : ''}</span></div>`).join('') : '').join('');
  const rv = d.review;
  $('#view').innerHTML = `<a href="#items">Zurück zur Übersicht</a><h2>${esc(d.alias)} <span class="pill">${esc(d.state)}</span></h2>
    <div class="row"><button onclick="location.hash='#chat';convId=null;setTimeout(()=>document.getElementById('msg').value='Zu der Automation „${esc(d.alias).replace(/'/g, '')}“: ',50)">Im Chat verbessern</button></div>
    ${d.missing.length ? `<div class="note err">Fehlende Entitäten: ${esc(d.missing.join(', '))}</div>` : ''}
    ${d.findings.length ? `<h3>Befunde</h3>${d.findings.map(f => `<div><span class="pill sev-${f.severity}">${SEV[f.severity]}</span> ${esc(f.title)} <span class="mute">${esc(f.detail)}</span></div>`).join('')}` : ''}
    ${rv ? `<h3>KI-Review</h3><div class="card"><span class="pill v-${rv.verdict}">${esc(rv.verdict)}</span> ${esc(rv.summary)}<ul>${rv.issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul>${rv.suggestion ? `<b>Vorschlag:</b> ${esc(rv.suggestion)}` : ''}</div>` : ''}
    ${refs}<h3>Konfiguration</h3><pre>${esc(d.yaml || 'Nicht über die API lesbar.')}</pre>`;
}

// ---------- Vorschläge ----------
async function proposals() {
  const ps = await api('/proposals');
  $('#view').innerHTML = `<h2>Vorschläge</h2>${ps.length ? ps.map(p => proposalCard(p)).join('') : '<div class="card mute">Noch keine Vorschläge.</div>'}`;
}

// ---------- Backups ----------
async function backups() {
  const bs = await api('/backups');
  $('#view').innerHTML = `<div class="row" style="justify-content:space-between"><h2>Backups</h2><button id="bk">Jetzt Backup erstellen</button></div>
    <div class="mute">Vor jedem Scan (optional) und vor jeder Änderung wird die Konfiguration aller Automationen, Skripte und Szenen gesichert. Dateien liegen im Volume <code>/data/backups</code>.</div>
    <div class="card"><table><tr><th>Datei</th><th>Grund</th><th>Inhalt</th><th></th></tr>${bs.map(b => `<tr><td>${esc(b.name)}</td><td>${esc(b.reason)}</td>
    <td>${b.automations} Auto · ${b.scripts} Skripte · ${b.scenes} Szenen</td><td><a href="/api/backups/${encodeURIComponent(b.name)}" target="_blank">JSON</a></td></tr>`).join('')}</table></div>`;
  $('#bk').onclick = async () => { try { await api('/backups', {method: 'POST'}); } catch (e) { alert(e.message); } route(); };
}

// ---------- Einstellungen ----------
async function settingsView() {
  const s = settings, models = await api('/ollama/models').catch(() => null);
  const modelInput = models ? `<select id="model">${models.map(m => `<option ${m.name === s.model ? 'selected' : ''} value="${esc(m.name)}">${esc(m.name)} (${m.size_gb} GB)</option>`).join('')}</select>`
    : `<input id="model" value="${esc(s.model)}"><div class="err">Ollama nicht erreichbar – Modellliste nicht verfügbar.</div>`;
  $('#view').innerHTML = `<h2>Einstellungen</h2>
  <div class="card"><h3 style="margin-top:0">Home Assistant</h3>
    <label>Server-URL</label><input id="ha_url" value="${esc(s.ha_url)}" placeholder="http://homeassistant.local:8123">
    <label>Long-Lived Access Token ${s.ha_token_set ? '(gespeichert – leer lassen zum Behalten)' : ''}</label><input id="ha_token" type="password" autocomplete="off">
    <label><input type="checkbox" id="ha_verify_ssl" style="width:auto" ${s.ha_verify_ssl ? 'checked' : ''}> SSL-Zertifikat prüfen</label>
    <div class="row" style="margin-top:10px"><button class="sec" id="tha">Verbindung testen</button><span id="rha"></span></div></div>
  <div class="card"><h3 style="margin-top:0">KI (Ollama auf dem Host)</h3>
    <label>Ollama-URL</label><input id="ollama_url" value="${esc(s.ollama_url)}">
    <label>Modell</label>${modelInput}
    <label>Kontextfenster (num_ctx) – größer = mehr VRAM</label><input id="num_ctx" type="number" step="512" value="${s.num_ctx}">
    <label>Modell im VRAM behalten (keep_alive, z. B. 2m, 0 = sofort entladen)</label><input id="keep_alive" value="${esc(s.keep_alive)}">
    <label>Temperatur</label><input id="temperature" type="number" step="0.1" min="0" max="1" value="${s.temperature}">
    <div class="mute" style="margin-top:8px">Faustregel für bis 10 GB VRAM: 8B-Modell (Q4) ca. 5 GB + ~0,5 GB KV-Cache bei 4096 Kontext. Auf dem Host zusätzlich <code>OLLAMA_MAX_LOADED_MODELS=1</code> und <code>OLLAMA_NUM_PARALLEL=1</code> setzen.</div></div>
  <div class="card"><h3 style="margin-top:0">Sicherheit</h3>
    <label><input type="checkbox" id="read_only" style="width:auto" ${s.read_only ? 'checked' : ''}> Schreibsperre: nichts an Home Assistant ändern (nur analysieren & vorschlagen)</label>
    <label><input type="checkbox" id="backup_on_scan" style="width:auto" ${s.backup_on_scan ? 'checked' : ''}> Bei jedem Scan automatisch ein Backup anlegen</label></div>
  <div class="row"><button id="save">Speichern</button><span id="saved" class="okc"></span></div>`;
  const val = id => { const e = $('#' + id); return e.type === 'checkbox' ? e.checked : e.type === 'number' ? Number(e.value) : e.value.trim(); };
  const collect = () => Object.fromEntries(['ha_url','ha_token','ha_verify_ssl','ollama_url','model','num_ctx','keep_alive','temperature','read_only','backup_on_scan'].map(k => [k, val(k)]));
  $('#save').onclick = async () => { await api('/settings', {method: 'PUT', body: collect()}); await loadSettings(); $('#saved').textContent = 'Gespeichert'; $('#saved').className='okc fade'; };
  $('#tha').onclick = async () => {
    await api('/settings', {method: 'PUT', body: collect()});
    $('#rha').textContent = '…';
    try { const r = await api('/test/ha', {method: 'POST'}); $('#rha').innerHTML = `<span style="color:var(--ok)">Verbunden: HA ${esc(r.version)} (${esc(r.location)})</span>`; }
    catch (e) { $('#rha').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  };
}
route();

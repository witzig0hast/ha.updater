const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let H = null, timer = null, answer = null, convId = null;

async function api(path, body, method) {
  const r = await fetch('/api' + path, {
    method: method || (body !== undefined ? (path === '/settings' ? 'PUT' : 'POST') : 'GET'),
    headers: body !== undefined ? {'Content-Type': 'application/json'} : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `Fehler ${r.status}`);
  return d;
}
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(t.h); t.h = setTimeout(() => t.classList.remove('on'), 3800); }
function ago(ts) {
  if (!ts) return '';
  const m = Math.round((Date.now() - new Date(ts)) / 60000);
  return m < 1 ? 'gerade eben' : m < 60 ? `vor ${m} Min.` : m < 1440 ? `vor ${Math.round(m / 60)} Std.` : `vor ${Math.round(m / 1440)} Tagen`;
}
const diffHtml = d => d.split('\n').filter(l => !/^(---|\+\+\+) /.test(l))
  .map(l => `<span class="${l[0] === '+' ? 'a' : l[0] === '-' ? 'd' : l[0] === '@' ? 'h' : ''}">${esc(l) || ' '}</span>`).join('');

// ---------- Theme ----------
const theme = {
  get() { try { return localStorage.getItem('theme') || 'dark'; } catch { return 'dark'; } },
  set(t) { document.documentElement.dataset.theme = t; try { localStorage.setItem('theme', t); } catch {} },
};
theme.set(theme.get());
$('#theme').onclick = () => theme.set(theme.get() === 'dark' ? 'light' : 'dark');

// ---------- Dialoge ----------
function modal(html) { const d = $('#dlg'); d.innerHTML = html; if (!d.open) d.showModal(); return d; }
function confirmBox(title, text, okLabel) {
  return new Promise(res => {
    const d = modal(`<h2>${esc(title)}</h2><p class="mute">${text}</p><div class="row end"><button class="sec" id="n">Abbrechen</button><button id="y">${esc(okLabel)}</button></div>`);
    d.querySelector('#n').onclick = () => { d.close(); res(false); };
    d.querySelector('#y').onclick = () => { d.close(); res(true); };
    d.oncancel = () => res(false);
  });
}

// ---------- Hauptseite ----------
async function load() {
  H = await api('/home');
  const l = $('#lock');
  l.className = 'link' + (H.read_only ? '' : ' rw');
  l.innerHTML = `<span class="dot"></span>${H.read_only ? 'Nur lesen' : 'Änderungen erlaubt'}`;
  render();
  clearTimeout(timer);
  if (H.run.running) timer = setTimeout(load, 1200);
}

function suggestion(p) {
  const isEntity = p.target_kind === 'entity';
  const isDelete = p.action === 'delete' || isEntity;
  const hasOld = !!p.analysis_old;
  const oldLabel = isEntity ? 'Details zur Entität' : 'Voranalyse der bestehenden Automation';
  const newLabel = isEntity ? 'Was beim Entfernen passiert' : p.action === 'delete' ? 'Was danach fehlt' : 'Analyse des Vorschlags';
  const analysis = (p.analysis_old || p.analysis_new) ? `<details class="analysis"><summary>Analyse</summary>
    ${hasOld ? `<details><summary>${oldLabel}</summary><pre>${esc(p.analysis_old)}</pre></details>` : ''}
    <details><summary>${newLabel}</summary><pre>${esc(p.analysis_new)}</pre></details>
  </details>` : '';
  const tag = isEntity ? 'Entität' : p.action === 'delete' ? 'löschen'
    : p.is_new ? (p.source === 'auto' ? 'neue Idee' : 'neu') : 'Änderung';
  const diff = (!isEntity && (p.diff || p.new_yaml))
    ? `<details><summary>${p.action === 'delete' ? 'Gelöschte Automation ansehen' : 'Genaue Änderung ansehen'}</summary><pre class="diff">${diffHtml(p.diff || p.new_yaml)}</pre></details>` : '';
  const adjust = isEntity ? '' : `<button class="sec" data-a="adj">Anpassen</button>`;
  const form = isEntity ? '' : `<form class="adj" hidden><input placeholder="Was soll anders sein?"><button>Senden</button></form>`;
  return `<article class="card sug" data-id="${p.id}" data-kind="${p.target_kind}">
    <h3>${esc(p.title)}<span class="tag${isDelete ? ' del' : ''}">${tag}</span></h3>
    <p>${esc(p.explanation)}</p>
    ${p.warnings.map(w => `<div class="note">${esc(w)}</div>`).join('')}
    ${analysis}
    ${diff}
    <div class="row">
      <button data-a="ok"${isDelete ? ' class="danger"' : ''}>${isDelete ? 'Löschen' : 'Übernehmen'}</button>
      ${adjust}
      <button class="quiet" data-a="no">Verwerfen</button>
    </div>
    ${form}
  </article>`;
}

function render() {
  const app = $('#app'), r = H.run;
  $('#ask').hidden = !H.configured;
  if (!H.configured) {
    app.innerHTML = `<h1>Verbinde dein Home Assistant</h1>
      <p class="sub">HA-Fix liest alles ein, prüft die Logik und macht dir Vorschläge. Es ändert nichts, solange du nicht ausdrücklich zustimmst.</p>
      <form class="card" id="setup">
        <label>Adresse von Home Assistant</label><input id="u" placeholder="http://homeassistant.local:8123" required>
        <label>Long-Lived Access Token <span class="mute">(HA-Profil, ganz unten bei „Sicherheit“)</span></label><input id="t" type="password" required>
        <div class="row" style="margin-top:16px"><button>Verbinden und analysieren</button><span id="e" class="err"></span></div>
      </form>`;
    $('#setup').onsubmit = async e => {
      e.preventDefault(); $('#e').textContent = '';
      try {
        await api('/settings', {ha_url: $('#u').value.trim(), ha_token: $('#t').value.trim()});
        await api('/test/ha', {});
        await api('/run', {}); load();
      } catch (x) { $('#e').textContent = x.message; }
    };
    return;
  }

  const n = H.proposals.length;
  let head;
  if (r.running) {
    const pct = r.total ? Math.round(100 * r.done / r.total) : 0;
    head = `<h1>Ich schaue mir dein System an …</h1><div class="card"><div class="mute">${esc(r.msg)}</div>
      <div class="bar ${r.total ? '' : 'ind'}"><i style="width:${pct}%"></i></div></div>`;
  } else if (r.error) {
    head = `<h1>Das hat nicht geklappt</h1><div class="card err">${esc(r.error)}</div><button id="again">Nochmal versuchen</button>`;
  } else if (!H.scanned_at) {
    head = `<h1>Bereit</h1><p class="sub">Noch nicht analysiert.</p><button id="again">Jetzt analysieren</button>`;
  } else {
    head = `<h1>${n ? `${n} ${n === 1 ? 'Vorschlag' : 'Vorschläge'} für dich` : 'Alles in Ordnung'}</h1>
      <div class="status"><span class="txt">${H.counts.automations} Automationen und ${H.counts.entities} Geräte/Entitäten geprüft ${esc(ago(H.scanned_at))}</span>
      <button class="sec" id="again">Neu analysieren</button></div>`;
  }
  const ai = r.ai_error ? `<div class="note">Die KI ist gerade nicht erreichbar, deshalb gibt es weniger Vorschläge. ${esc(r.ai_error)}</div>` : '';
  const ans = answer ? `<article class="card"><div class="reply">${esc(answer.reply)}</div></article>` : '';
  const notes = H.notes.length ? `<details class="more"><summary>Weitere Hinweise (${H.notes.length})</summary><div class="card"><ul class="notes">${H.notes.map(x =>
    `<li><span class="sev ${x.severity}"></span>${esc(x.title)}${x.detail ? `<small>${esc(x.detail)}</small>` : ''}</li>`).join('')}</ul></div></details>` : '';
  app.innerHTML = head + ai + ans + (r.running ? '' : H.proposals.map(suggestion).join('')) + (r.running ? '' : notes);
  const again = $('#again'); if (again) again.onclick = startRun;
}

async function startRun() { answer = null; try { await api('/run', {}); } catch (e) { toast(e.message); } load(); }

// ---------- Aktionen auf Vorschlägen ----------
$('#app').addEventListener('click', async e => {
  const b = e.target.closest('button[data-a]'); if (!b) return;
  const card = b.closest('.sug'), id = +card.dataset.id, a = b.dataset.a;
  if (a === 'adj') { const f = $('.adj', card); f.hidden = !f.hidden; if (!f.hidden) $('input', f).focus(); return; }
  if (a === 'no') { await api(`/proposals/${id}/reject`, {}); card.classList.add('gone'); setTimeout(load, 300); return; }
  if (a === 'ok') {
    const ro = H.read_only, isEntity = card.dataset.kind === 'entity', del = b.textContent.trim() === 'Löschen';
    const lbl = del ? 'Löschen' : 'Übernehmen';
    const title = isEntity ? 'Entität entfernen?' : del ? 'Automation löschen?' : 'Änderung übernehmen?';
    const text = (isEntity ? 'Die Entität wird aus der Home-Assistant-Registry entfernt. Meldet sie sich später wieder, legt Home Assistant sie automatisch neu an.'
      : 'Vorher wird automatisch ein Backup deiner Automationen angelegt.') + (ro ? `<br><br>Der Schreibschutz ist aktiv. Mit „${lbl}“ erlaubst du diese eine Änderung.` : '');
    const yes = await confirmBox(title, text, lbl);
    if (!yes) return;
    b.disabled = true;
    try { const p = await api(`/proposals/${id}/approve`, {confirm_write: true}); toast((del ? 'Gelöscht. Backup: ' : 'Übernommen. Backup: ') + p.backup); card.classList.add('gone'); setTimeout(load, 300); }
    catch (x) { toast(x.message); b.disabled = false; }
  }
});
$('#app').addEventListener('submit', async e => {
  if (!e.target.classList.contains('adj')) return;
  e.preventDefault();
  const card = e.target.closest('.sug'), q = $('input', e.target).value.trim(); if (!q) return;
  const btn = $('button', e.target); btn.disabled = true; btn.textContent = 'Überlege …';
  try { await api('/chat', {message: q, revise_proposal: +card.dataset.id}); await load(); }
  catch (x) { toast(x.message); btn.disabled = false; btn.textContent = 'Senden'; }
});

// ---------- Freitext-Frage ----------
$('#ask').onsubmit = async e => {
  e.preventDefault();
  const q = $('#q').value.trim(); if (!q) return;
  const btn = $('#send'); btn.disabled = true; btn.textContent = 'Überlege …';
  try {
    const r = await api('/chat', {message: q, conv_id: convId}); convId = r.conv_id; $('#q').value = '';
    answer = {reply: r.reply, proposal: r.proposal}; await load();
    window.scrollTo({top: 0, behavior: 'smooth'});
  } catch (x) { toast(x.message); }
  btn.disabled = false; btn.textContent = 'Fragen';
};

// ---------- Einstellungen ----------
$('#lock').onclick = $('#cog').onclick = async () => {
  const s = await api('/settings');
  const models = await api('/ollama/models').catch(() => null);
  const mdl = models ? `<select id="model">${models.map(m => `<option ${m.name === s.model ? 'selected' : ''} value="${esc(m.name)}">${esc(m.name)} (${m.size_gb} GB)</option>`).join('')}</select>`
    : `<input id="model" value="${esc(s.model)}"><div class="err" style="font-size:13px">Ollama nicht erreichbar unter ${esc(s.ollama_url)}</div>`;
  const bk = await api('/backups').catch(() => []);
  const d = modal(`<h2>Einstellungen</h2>
    <label>Home Assistant</label><input id="ha_url" value="${esc(s.ha_url)}">
    <label>Token ${s.ha_token_set ? '<span class="mute">(gespeichert, leer lassen zum Behalten)</span>' : ''}</label><input id="ha_token" type="password" autocomplete="off">
    <label>Ollama-Adresse</label><input id="ollama_url" value="${esc(s.ollama_url)}">
    <label>Modell</label>${mdl}
    <label>Kontextgröße (mehr = mehr VRAM)</label><input id="num_ctx" type="number" step="512" value="${s.num_ctx}">
    <label>KI-Reparaturen pro Lauf <span class="mute">(ein Modell-Aufruf je Automation – höher = langsamer, aber mehr auf einmal)</span></label>
    <input id="max_suggestions" type="number" min="1" step="1" value="${s.max_suggestions}">
    <label>Entität gilt nach wie vielen Tagen „nicht verfügbar“ als verwaist?</label>
    <input id="stale_days" type="number" min="1" step="1" value="${s.stale_days}">
    <label style="margin-top:18px;color:var(--fg)"><input type="checkbox" id="read_only" ${s.read_only ? 'checked' : ''}>Schreibschutz (Änderungen immer extra bestätigen)</label>
    <div class="mute" style="margin-top:14px;font-size:13px">${bk.length} Backups gespeichert${bk[0] ? `, zuletzt <a href="/api/backups/${encodeURIComponent(bk[0].name)}" target="_blank">${esc(bk[0].name)}</a>` : ''}.
      <a href="#" id="bk">Jetzt Backup erstellen</a></div>
    <div class="row end"><button class="sec" id="c">Schließen</button><button id="s">Speichern</button></div>`);
  $('#c', d).onclick = () => d.close();
  $('#bk', d).onclick = async ev => { ev.preventDefault(); try { const r = await api('/backups', {}); toast('Backup erstellt: ' + r.name); } catch (x) { toast(x.message); } };
  $('#s', d).onclick = async () => {
    const g = id => $('#' + id, d);
    try {
      await api('/settings', {ha_url: g('ha_url').value.trim(), ha_token: g('ha_token').value.trim(), ollama_url: g('ollama_url').value.trim(),
        model: g('model').value, num_ctx: Number(g('num_ctx').value), read_only: g('read_only').checked,
        max_suggestions: Number(g('max_suggestions').value), stale_days: Number(g('stale_days').value)});
      d.close(); toast('Gespeichert'); load();
    } catch (x) { toast(x.message); }
  };
};

load();

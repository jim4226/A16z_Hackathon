// Morph Studio v2: shell (header, chat, parameters, pipeline, tabs, inspector, live events, auto-tour).
// Tab bodies live in panels.js. Dev mode: index.html?fixture=1 runs entirely from ./fixtures/.
import {
  I, esc, fmt, money, pretty, sleep, h, ViewerAdapter, clearCaches,
  RobotPanel, SimPanel, ElecPanel, MfgPanel, AsmPanel, BomPanel, forceWindowSVG,
} from './panels.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const QS = new URLSearchParams(location.search);
const FIXTURE = QS.has('fixture') && QS.get('fixture') !== '0';
const FIX_BASE = new URL('./fixtures/', import.meta.url).href;

const SUGGEST = [
  ['Swap to Dynamixel servos', 'cpu'], ['Use a custom PCB', 'chip'], ['Make it 6-DOF', 'axis'], ['Add a wrist camera', 'camera'],
  ['Pilot build: CNC + sheet metal', 'factory'], ['Make it work for a strawberry', 'leaf'], ['Increase reach to 380 mm', 'ruler'],
];
const TABS = ['robot', 'sim', 'elec', 'mfg', 'asm', 'bom'];
const NODE_TAB = { sizing: 'robot', gripper: 'robot', cad: 'robot', geometry: 'robot', arm: 'robot', sim: 'sim', simulate: 'sim',
  electronics: 'elec', wiring: 'elec', eplan: 'elec', manufacturing: 'mfg', mfg: 'mfg', bom: 'bom', manual: 'asm', assembly: 'asm' };
const DEFAULT_NODES = [['sizing', 'Sizing'], ['gripper', 'Gripper + grasp'], ['electronics', 'Electronics plan'], ['cad', 'Robot CAD'],
  ['sim', 'Simulation'], ['wiring', 'Schematic · PCB · FW'], ['manufacturing', 'Manufacturing'], ['bom', 'BOM + sourcing'], ['manual', 'Assembly manual']];
const METRICS = [
  ['Unit cost', 'unit_cost', '$', 'lo'], ['Pick & place', 'pick_place_success', '', 'hi'], ['Cycle time', 'cycle_time_s', 's', 'lo'],
  ['Payload at full reach', 'max_payload_g', 'g', 'hi'], ['Peak joint torque', 'max_torque_pct', '%', 'lo'],
  ['Min torque margin', 'min_torque_margin', '×', 'hi'], ['Top-down reach', 'workspace_reach_mm', 'mm', 'hi'],
  ['Robot mass', 'robot_mass_kg', 'kg', 'lo'], ['Grip force target', 'grip_target_n', 'N', null], ['Harness wires', 'harness_wires', '', 'lo'],
  ['Fab lead time', 'fab_lead_days', 'd', 'lo'], ['Tooling', 'tooling_cost', '$', 'lo'], ['Assembly time', 'assembly_time_min', 'min', 'lo'],
  ['Part count', 'parts', '', null],
];
const FAMILY = { hobby_pwm: 'Hobby PWM', smart_bus: 'Smart bus', dynamixel: 'Dynamixel' };
const ECONF = { modules: 'Off-the-shelf', custom_pcb: 'Custom PCB' };
const BUILD = { prototype: 'Prototype', pilot: 'Pilot', production: 'Production' };

// ------------------------------------------------------------------ data access
const isAbs = (u) => /^(\/|[a-z]+:)/i.test(u);
function resolve(u) {
  if (!u) return null;
  if (FIXTURE && !isAbs(u)) return new URL(u, FIX_BASE).href;
  return u;
}
function join(base, rel) {
  if (!rel) return null;
  if (isAbs(rel) || !base) return resolve(rel);
  return resolve(base.replace(/\/+$/, '') + '/' + rel.replace(/^\.?\//, ''));
}
const memo = new Map();
function fetchOnce(url, kind) {
  const key = kind + ':' + url;
  if (!memo.has(key)) {
    memo.set(key, fetch(url, { cache: FIXTURE ? 'default' : 'no-store' }).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${url}`);
      return kind === 'json' ? r.json() : r.text();
    }).catch((e) => { memo.delete(key); throw e; }));
  }
  return memo.get(key);
}
const getJSON = (u) => fetchOnce(u, 'json');
const getText = (u) => fetchOnce(u, 'text');

class ServerBackend {
  label = 'Live';
  async state() { return getFresh('/api/state'); }
  async rev(r) { return getFresh(`/api/rev?rev=${encodeURIComponent(r)}`); }
  chat(message) { return post('/api/chat', { message }); }
  params(changes) { return post('/api/params', { changes }); }
  async project(name) {
    const r = await fetch('/api/project', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    return r.ok;
  }
  connect(onEvent, onStatus) {
    const go = () => {
      const es = new EventSource('/api/events');
      es.onopen = () => onStatus('live');
      es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.warn('bad event', e); } };
      es.onerror = () => { es.close(); onStatus('off'); setTimeout(go, 1500); };
    };
    go();
  }
}
async function getFresh(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}
function post(url, body) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .catch((e) => toast(`Couldn't reach the Studio server (${e.message})`));
}

// Fixture backend: replays the pipeline locally so every animation can be exercised without a server.
const GRAPH = {
  sizing: [['arm.', 'object.mass_g', 'electronics.servo'], []], gripper: [['finger.', 'object.', 'gripper.', 'safety_factor', 'electronics.target_force_n', 'electronics.servo'], []],
  electronics: [['electronics.'], ['sizing', 'gripper']], cad: [['finger.', 'object.shape', 'object.diameter_mm', 'build.', 'name'], ['sizing', 'electronics']],
  sim: [['task.', 'object.', 'arm.', 'finger.', 'build.'], ['sizing', 'gripper']], wiring: [[], ['electronics', 'cad']],
  manufacturing: [['build.', 'fab.'], ['cad', 'wiring']], bom: [[], ['manufacturing', 'wiring']], manual: [[], ['cad', 'manufacturing', 'wiring']],
};
class FixtureBackend {
  label = 'Fixture';
  async load() {
    this.d = await getJSON(FIX_BASE + 'rev_fixture.json');
    this.busy = false;
  }
  async state() {
    const d = this.d;
    return { project: d.project, projects: d.projects || [], revs: [...d.revs], history: [...d.history], current: d.current, previous: d.previous };
  }
  async rev(r) { return this.d.revisions[r]; }
  connect(onEvent, onStatus) { this.emit = onEvent; onStatus('fixture'); }
  chat(message) {
    const { changes, reply } = fixtureIntent(message, this.d.current.design || {});
    if (!Object.keys(changes).length) {
      this.emit({ type: 'user', text: message });
      setTimeout(() => this.emit({ type: 'assistant', text: reply }), 650);
      return;
    }
    this.run(changes, message, reply, message);
  }
  params(changes) {
    const desc = Object.entries(changes).map(([k, v]) => `${k.split('.').pop().replace(/_/g, ' ')} → ${v}`).join(', ');
    this.run(changes, null, `Parameter edit: ${desc}.`, `edit: ${desc}`);
  }
  async run(changes, userText, reply, label) {
    const emit = this.emit;
    if (this.busy) { emit({ type: 'assistant', text: 'Still regenerating the last change; one moment.' }); return; }
    this.busy = true;
    try {
      if (userText) emit({ type: 'user', text: userText });
      await sleep(userText ? 900 : 250);
      emit({ type: 'assistant', text: reply, changes, source: 'fixture' });
      const d = this.d, cur = d.current;
      const keys = Object.keys(changes);
      const dirty = new Set(Object.keys(GRAPH).filter((n) => GRAPH[n][0].some((p) => keys.some((k) => (p.endsWith('.') ? k.startsWith(p) : k === p)))));
      for (let grew = true; grew;) {
        grew = false;
        for (const [n, [, ups]] of Object.entries(GRAPH)) if (!dirty.has(n) && ups.some((u) => dirty.has(u))) { dirty.add(n); grew = true; }
      }
      const rev = 'r' + String(d.revs.length + 1).padStart(3, '0');
      const order = (cur.nodes || DEFAULT_NODES).map((n) => n[0]);
      emit({ type: 'start', rev, parent: cur.rev, changes, dirty: order.filter((n) => dirty.has(n)) });
      const t0 = performance.now(), timings = {}, prov = {};
      for (const n of order) {
        if (!dirty.has(n)) { await sleep(90); prov[n] = `reused from ${cur.rev}`; emit({ type: 'node', node: n, status: 'reused' }); continue; }
        emit({ type: 'node', node: n, status: 'computing' });
        const real = (cur.timings || {})[n] ?? 0.4;
        const ms = Math.min(250 + real * 160, 1300);
        await sleep(ms);
        timings[n] = +(real * (0.85 + Math.random() * 0.3)).toFixed(2);
        prov[n] = 'computed';
        emit({ type: 'node', node: n, status: 'computed', seconds: timings[n] });
      }
      const next = fixtureNext(d, cur, changes, rev, label, prov, timings);
      d.revisions[rev] = next;
      d.revs.push(rev);
      d.previous = cur;
      d.current = next;
      emit({ type: 'done', current: next, previous: cur, seconds: +((performance.now() - t0) / 1000 * 2.4).toFixed(1) });
    } finally { this.busy = false; }
  }
}
function fixtureIntent(msg, design) {
  const t = msg.toLowerCase(), ch = {}, said = [];
  if (/dynamixel|robotis/.test(t)) { ch['arm.actuator_family'] = 'dynamixel'; said.push('actuators → ROBOTIS Dynamixel (XM430 shoulder, XL430 elsewhere)'); }
  else if (/smart|bus servo|feetech|sts3215/.test(t)) { ch['arm.actuator_family'] = 'smart_bus'; said.push('actuators → Feetech STS3215 smart bus'); }
  else if (/hobby|pwm/.test(t)) { ch['arm.actuator_family'] = 'hobby_pwm'; said.push('actuators → hobby PWM servos'); }
  if (/custom pcb|pcba|custom board/.test(t)) { ch['electronics.config'] = 'custom_pcb'; said.push('electronics → one custom controller PCBA'); }
  else if (/off[- ]the[- ]shelf|modules|dev ?board/.test(t)) { ch['electronics.config'] = 'modules'; said.push('electronics → off-the-shelf modules'); }
  let m = t.match(/\b([456])\s*-?\s*(dof|axis|axes)/);
  if (m) { ch['arm.dof'] = +m[1]; said.push(`${m[1]}-DOF arm`); }
  m = t.match(/reach (?:to |of )?(\d{3})/);
  if (m) { ch['arm.reach_mm'] = +m[1]; said.push(`reach → ${m[1]} mm`); }
  if (/camera/.test(t)) { ch['electronics.extras'] = 'wrist_camera'; said.push('added a wrist camera (USB UVC, 32 mm)'); }
  if (/pilot|cnc|sheet[- ]metal/.test(t)) { ch['build.config'] = 'pilot'; said.push('build → pilot (CNC aluminium + sheet metal + PCBA)'); }
  else if (/production|injection|mold/.test(t)) { ch['build.config'] = 'production'; said.push('build → production (molded covers + sheet metal)'); }
  else if (/prototype|3d print/.test(t)) { ch['build.config'] = 'prototype'; said.push('build → prototype (3D printed)'); }
  if (/strawberry/.test(t)) { Object.assign(ch, { 'object.name': 'strawberry', 'object.mass_g': 20, 'object.max_force_n': 4 }); said.push('object → strawberry (≈20 g, bruises above ~4 N)'); }
  else if (/egg/.test(t)) { Object.assign(ch, { 'object.name': 'raw egg', 'object.mass_g': 60, 'object.max_force_n': 25 }); said.push('object → raw egg'); }
  for (const k of Object.keys(ch)) if (design[k] === ch[k]) delete ch[k];
  return { changes: ch, reply: said.length ? said.map((s) => s[0].toUpperCase() + s.slice(1)).join('; ') + '.' :
    "I couldn't map that to a design parameter. Try the suggestions below, or edit a parameter directly." };
}
function fixtureNext(d, cur, changes, rev, label, prov, timings) {
  const n = structuredClone(cur);
  Object.assign(n, { rev, parent: cur.rev, label, provenance: prov, timings, changes: {} });
  for (const [k, v] of Object.entries(changes)) { n.changes[k] = [cur.design?.[k], v]; n.design[k] = v; }
  for (const p of n.params || []) if (p.key in changes) p.value = changes[p.key];
  const ec = n.design['electronics.config'], bc = n.design['build.config'];
  if ('electronics.config' in changes) {
    const src = Object.values(d.revisions).find((r) => r.electronics?.config === ec);
    if (src) {
      n.electronics = structuredClone(src.electronics);
      Object.assign(n.summary, { electronics_config: ec, pcb: !!n.electronics.board, harness_wires: n.electronics.harness.length,
        assembly_time_min: src.summary.assembly_time_min });
    }
  }
  const alt = d.alt?.[ec]?.[bc];
  if (alt && ('electronics.config' in changes || 'build.config' in changes)) {
    n.manufacturing = { ...structuredClone(alt.manufacturing), base: n.manufacturing?.base || alt.manufacturing.base };
    n.bom = structuredClone(alt.bom);
    const cfg = n.manufacturing.configs?.[bc] || {};
    Object.assign(n.summary, { build_config: bc, unit_cost: n.bom.totals.total, fab_lead_days: cfg.lead_days ?? n.manufacturing.totals?.lead_days,
      tooling_cost: cfg.tooling_cost ?? 0 });
  }
  if ('arm.actuator_family' in changes) {
    const fam = changes['arm.actuator_family'];
    const key = { dynamixel: 'xm430_w350', hobby_pwm: 'ds3218', smart_bus: 'sts3215' }[fam];
    if (n.sizing) n.sizing.joints.forEach((j, i) => { j.actuator = fam === 'dynamixel' && i !== 1 ? 'xl430_w250' : key; });
    n.summary.actuators = [...new Set((n.sizing?.joints || []).map((j) => j.actuator))].sort().join(', ');
    if (fam === 'dynamixel') Object.assign(n.summary, { unit_cost: +(n.summary.unit_cost + 412.6).toFixed(2), min_torque_margin: 2.1, max_torque_pct: 41.2 });
  }
  if ('arm.dof' in changes) Object.assign(n.summary, { dof: changes['arm.dof'], unit_cost: +(n.summary.unit_cost + 23.4).toFixed(2), parts: n.summary.parts + 3 });
  if ('arm.reach_mm' in changes) Object.assign(n.summary, { reach_mm: changes['arm.reach_mm'], max_payload_g: 180, max_torque_pct: 48.6 });
  if ('object.mass_g' in changes) Object.assign(n.summary, { payload_g: changes['object.mass_g'], grip_target_n: 0.6 });
  return n;
}

// ------------------------------------------------------------------ UI helpers
let toastT = null;
function toast(text) {
  const t = $('#toast');
  t.textContent = text;
  t.classList.add('on');
  clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.remove('on'), 2600);
}

// ------------------------------------------------------------------ app state
const S = { project: '', projects: [], revs: [], current: null, previous: null, latest: null, labels: {}, tab: 'robot', pending: {}, robotKey: null };
let backend, viewer, panels;

const ctx = {
  resolve, join, getJSON, getText, toast, fixture: FIXTURE,
  get current() { return S.current; }, get previous() { return S.previous; },
  get viewer() { return viewer; },
  postParams: (changes) => applyChanges(changes),
  showTab: (t, o) => showTab(t, o),
  isActive: (t) => S.tab === t,
};

function applyChanges(changes) {
  if (!backend) return;
  backend.params(changes);
  setConn('busy', 'Regenerating');
}

// ------------------------------------------------------------------ header
function renderProjects() {
  const list = S.projects || [];
  $('#projBtn').classList.toggle('multi', list.length > 1);
  $('#projMenu').innerHTML = `<div class="mh">Projects</div>` + list.map((p) => `<button type="button" data-p="${esc(p.name)}" class="${p.name === S.project ? 'on' : ''}"><b>${esc(p.name)}</b>${p.source ? `<span class="tag acc">imported · ${esc(p.source)}</span>` : '<span class="tag neutral">generated</span>'}${p.need ? `<small>${esc(p.need)}</small>` : ''}</button>`).join('');
  $$('#projMenu button[data-p]').forEach((b) => (b.onclick = async () => {
    $('#projMenu').classList.remove('on');
    if (b.dataset.p === S.project || !backend.project) return;
    toast(`Switching to ${b.dataset.p}…`);
    const ok = await backend.project(b.dataset.p).catch(() => false);
    if (!ok) toast('Still regenerating; switch projects when the pipeline is idle');
  }));
}
function setupProjects() {
  $('#projBtn').onclick = (e) => { e.stopPropagation(); if ((S.projects || []).length > 1) $('#projMenu').classList.toggle('on'); };
  document.addEventListener('click', (e) => { if (!e.target.closest('#projMenu')) $('#projMenu').classList.remove('on'); });
}
async function reloadProject() {
  memo.clear();
  clearCaches();
  for (const p of Object.values(panels)) p.resetCache?.();
  const st = await backend.state();
  S.project = st.project; S.projects = st.projects || S.projects; S.revs = st.revs || []; S.latest = st.current?.rev || null;
  S.labels = {}; S.robotKey = null; S.justAdded = null;
  $('#messages').innerHTML = '';
  for (const m of st.history || []) addMsg(m.type, m.text, m.changes);
  if (!(st.history || []).length && st.current) addMsg('assistant', `Switched to ${st.project} at ${st.current.rev}. ${st.current.label ? `“${st.current.label}”. ` : ''}Ask for a change or edit a parameter.`);
  renderProjects();
  if (st.current) await show(st.current, st.previous, { tour: false });
  for (const r of S.revs.slice(-6)) if (!S.labels[r]) backend.rev(r).then((x) => { S.labels[r] = x.label; renderRevs(); }).catch(() => {});
}

function renderHeader() {
  $('#project').textContent = S.project || '—';
  const d = S.current?.design || {}, pd = S.previous?.design || {};
  const items = [
    ['arm.actuator_family', 'Act', FAMILY, 'cpu'], ['electronics.config', 'Elec', ECONF, 'chip'], ['build.config', 'Build', BUILD, 'factory'],
  ];
  $('#badges').innerHTML = items.map(([k, lab, map, ic]) => {
    const v = d[k];
    if (v == null) return '';
    const changed = S.current?.changes && k in S.current.changes;
    return `<span class="cbadge${changed ? ' flash' : ''}" title="${esc(k)}">${I(ic, 'sm')}<span class="k">${lab}</span>${esc(map[v] || pretty(v))}</span>`;
  }).join('');
  renderRevs();
}
function renderRevs(pending) {
  const el = $('#revs');
  const parts = [];
  S.revs.forEach((r, i) => {
    if (i) parts.push('<span class="revlink"></span>');
    const lbl = S.labels[r];
    const on = S.current && r === S.current.rev;
    parts.push(`<button class="rev${on ? ' on' : ''}${r === S.justAdded ? ' new' : ''}" data-rev="${r}" title="${esc(lbl ? `${r} · ${lbl}` : r)}"><i></i>${r}${lbl && on ? `<span class="lbl">${esc(lbl)}</span>` : ''}</button>`);
  });
  if (pending) parts.push(`<span class="revlink"></span><span class="rev pending"><i></i>${pending}<span class="lbl">regenerating…</span></span>`);
  el.innerHTML = parts.join('');
  $$('.rev[data-rev]', el).forEach((b) => (b.onclick = () => openRev(b.dataset.rev)));
  requestAnimationFrame(() => (el.scrollLeft = el.scrollWidth));
}
async function openRev(r) {
  if (S.current && r === S.current.rev) return;
  try {
    const cur = await backend.rev(r);
    const prev = cur.parent ? await backend.rev(cur.parent).catch(() => null) : null;
    show(cur, prev, { tour: false });
    toast(r === S.latest ? `Back to the latest revision ${r}` : `Viewing ${r}: ${cur.label || ''}`);
  } catch (e) { toast(`Couldn't load ${r}`); console.warn(e); }
}
function setConn(kind, text) {
  const c = $('#conn');
  c.className = 'conn ' + (kind === 'live' ? '' : kind);
  $('span', c).textContent = text || { live: 'Live', off: 'Reconnecting', busy: 'Regenerating', fixture: 'Fixture data' }[kind] || kind;
}

// ------------------------------------------------------------------ chat
function addMsg(role, text, changes) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  if (role === 'assistant') {
    el.innerHTML = `<div class="av"><svg viewBox="0 0 24 24"><path d="M5 18V6l7 6 7-6v12"/></svg></div><div><div class="b"></div></div>`;
    $('.b', el).textContent = text;
    if (changes && Object.keys(changes).length) {
      const c = h('div', 'chips');
      c.innerHTML = Object.entries(changes).map(([k, v]) => `<span class="chip mono">${esc(k)} = <b>${esc(pretty(Array.isArray(v) ? v[1] : v, true))}</b></span>`).join('');
      el.lastElementChild.appendChild(c);
    }
  } else if (role === 'system') {
    el.innerHTML = `<div class="b"><span class="spin"></span><span class="t"></span></div>`;
    $('.t', el).textContent = text;
  } else {
    el.innerHTML = '<div class="b"></div>';
    $('.b', el).textContent = text;
  }
  $('#messages').appendChild(el);
  scrollChat();
  return el;
}
function scrollChat() { const m = $('#messages'); requestAnimationFrame(() => (m.scrollTop = m.scrollHeight)); }
let typingEl = null;
function typing(on) {
  if (on && !typingEl) {
    typingEl = h('div', 'msg assistant');
    typingEl.innerHTML = `<div class="av"><svg viewBox="0 0 24 24"><path d="M5 18V6l7 6 7-6v12"/></svg></div><div><div class="b"><span class="typing"><i></i><i></i><i></i></span></div></div>`;
    $('#messages').appendChild(typingEl);
    scrollChat();
  } else if (!on && typingEl) { typingEl.remove(); typingEl = null; }
}
function sendChat(text) {
  if (!text.trim() || !backend) return;
  backend.chat(text.trim());
}
function setupChat() {
  const input = $('#input');
  const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 96) + 'px'; };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#form').requestSubmit(); }
  });
  $('#form').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = input.value.trim();
    if (t) { sendChat(t); input.value = ''; grow(); }
  });
  $('#suggest').innerHTML = SUGGEST.map(([t, ic]) => `<button type="button" data-t="${esc(t)}">${I(ic, 'sm')}${esc(t)}</button>`).join('');
  $$('#suggest button').forEach((b) => (b.onclick = () => sendChat(b.dataset.t)));
}

// ------------------------------------------------------------------ parameters
function renderParams(cur) {
  const params = cur?.params || [];
  S.pending = {};
  $('#paramsCount').textContent = params.length;
  const groups = [];
  for (const p of params) {
    let g = groups.find((x) => x.name === p.group);
    if (!g) groups.push((g = { name: p.group || 'Other', items: [] }));
    g.items.push(p);
  }
  const changed = cur?.changes || {};
  $('#paramsBody').innerHTML = groups.map((g) => `<div class="pgroup"><div class="sec-t">${esc(g.name)}<span class="r">${g.items.length}</span></div>` +
    g.items.map((p) => paramRow(p, p.key in changed)).join('') + '</div>').join('') || '<div class="empty"><p>No editable parameters in this revision.</p></div>';
  $$('#paramsBody .prow').forEach((row) => {
    const p = params.find((x) => x.key === row.dataset.key);
    const rng = $('input[type=range]', row), num = $('input[type=number]', row), sel = $('select', row);
    const setP = () => { if (rng) rng.style.setProperty('--p', `${((+rng.value - +rng.min) / ((+rng.max - +rng.min) || 1)) * 100}%`); };
    setP();
    const commit = (v) => {
      const orig = p.value;
      const same = String(v) === String(orig) || (typeof orig === 'number' && +v === orig);
      if (same) delete S.pending[p.key]; else S.pending[p.key] = typeof orig === 'number' ? +v : (orig === null && v === '' ? '' : v);
      row.classList.toggle('changed', !same);
      $('label small', row).textContent = same ? '' : `was ${pretty(orig, true)}`;
      updateParamsBar();
    };
    if (rng) rng.addEventListener('input', () => { num.value = rng.value; setP(); commit(rng.value); });
    if (num) num.addEventListener('change', () => { if (rng) { rng.value = num.value; setP(); } commit(num.value); });
    if (sel) sel.addEventListener('change', () => commit(typeof p.value === 'number' ? +sel.value : sel.value));
  });
  updateParamsBar();
}
function paramRow(p, regen) {
  const lab = `<label title="${esc(p.key)}">${esc(p.label || p.key)}<small></small></label>`;
  let ctl;
  if (p.type === 'select') {
    const opts = (p.options || []).map((o) => `<option value="${esc(o)}"${String(o) === String(p.value) ? ' selected' : ''}>${esc(pretty(o, true))}</option>`).join('');
    ctl = `<div class="sel"><select>${opts}</select></div>`;
  } else {
    const min = p.min ?? 0, max = p.max ?? Math.max(+p.value * 2, 1), step = p.step ?? 'any';
    const range = max - min <= 2000 ? `<input type="range" min="${min}" max="${max}" step="${step}" value="${p.value}">` : '<span></span>';
    ctl = `<div class="pnum">${range}<div class="field"><input type="number" min="${min}" max="${max}" step="${step}" value="${p.value}"><span class="u">${esc(p.unit || '')}</span></div></div>`;
  }
  return `<div class="prow${regen ? ' regen' : ''}" data-key="${esc(p.key)}">${lab}${ctl}</div>`;
}
function updateParamsBar() {
  const n = Object.keys(S.pending).length;
  $('#params').classList.toggle('dirty', n > 0);
  $('#paramsN').innerHTML = `<b>${n}</b> pending change${n === 1 ? '' : 's'}`;
  $('#paramsPend').textContent = `${n} pending`;
}
function setupParams() {
  $('#paramsToggle').onclick = () => $('#params').classList.toggle('open');
  $('#paramsReset').onclick = () => renderParams(S.current);
  $('#paramsApply').onclick = () => {
    const ch = { ...S.pending };
    if (!Object.keys(ch).length) return;
    applyChanges(ch);
    $('#paramsApply').disabled = true;
    setTimeout(() => ($('#paramsApply').disabled = false), 1200);
  };
}

// ------------------------------------------------------------------ pipeline strip
function nodesOf(rev) { return rev?.nodes?.length ? rev.nodes : DEFAULT_NODES; }
function renderPipeline(rev, live) {
  const nodes = nodesOf(rev);
  const prov = rev?.provenance || {}, tim = rev?.timings || {}, errs = rev?.errors || {};
  $('#pipeline').innerHTML = nodes.map(([k, label], i) => {
    let cls = '', sub = '—';
    const p = live ? live[k] : prov[k];
    if (live) { cls = p || 'queued'; sub = { queued: 'queued', computing: 'running…', reused: 'reused', computed: 'rebuilt' }[cls] || ''; }
    else if (errs[k]) { cls = 'error'; sub = 'error'; }
    else if (p === 'computed') { cls = 'computed'; sub = tim[k] >= 0.005 ? `rebuilt ${fmtSec(tim[k])}` : 'rebuilt'; }
    else if (p && p.startsWith('reused')) { cls = 'reused'; sub = 'reused'; }
    const long = String(label).split(/\s+/).some((w) => w.length >= 12) ? ' long' : '';
    return `${i ? '<span class="link"></span>' : ''}<div class="node ${cls}${long}" data-node="${esc(k)}" title="${esc(label)}${errs[k] ? ' — ' + esc(errs[k]) : ''}"><div class="nt"><span>${esc(label)}</span></div><div class="ns"><i class="dot"></i><span>${esc(sub)}</span></div><div class="bar"></div></div>`;
  }).join('');
  $$('#pipeline .node').forEach((n) => (n.onclick = () => { const t = NODE_TAB[n.dataset.node]; if (t) { stopTour(); showTab(t); } }));
}
const fmtSec = (s) => (s < 0.1 ? `${Math.round(s * 1000)}ms` : `${(+s).toFixed(s < 10 ? 1 : 0)}s`);
function nodeEvent(ev) {
  const d = $(`#pipeline .node[data-node="${CSS.escape(ev.node)}"]`);
  if (!d) return;
  d.className = `node ${ev.status}${ev.status === 'computed' ? ' just' : ''}`;
  $('.ns span', d).textContent = ev.status === 'computing' ? 'running…' : ev.status === 'computed' ? (ev.seconds >= 0.005 ? `rebuilt ${fmtSec(ev.seconds)}` : 'rebuilt') : 'reused';
  const link = d.previousElementSibling;
  $$('#pipeline .link').forEach((l) => l.classList.remove('flow'));
  if (ev.status === 'computing' && link?.classList.contains('link')) link.classList.add('flow');
}

// ------------------------------------------------------------------ tabs
function showTab(name, { fromTour = false } = {}) {
  if (!TABS.includes(name)) return;
  if (!fromTour) stopTour();
  const prev = S.tab;
  S.tab = name;
  $$('#tabs .tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  $$('#views .view').forEach((v) => v.classList.toggle('on', v.dataset.view === name));
  $(`#tabs .tab[data-tab="${name}"]`)?.classList.remove('regen');
  if (prev !== name) panels[prev]?.onHide?.();
  const slot = panels[name]?.slot;
  if (slot) { viewer?.attach(slot); }
  panels[name]?.onShow?.();
  try { history.replaceState(null, '', urlWith({ tab: name })); } catch (e) { /* file:// */ }
}
function urlWith(o) {
  const u = new URL(location.href);
  for (const [k, v] of Object.entries(o)) u.searchParams.set(k, v);
  return u.toString();
}
function markTabs(cur) {
  const regen = new Set();
  for (const [n, p] of Object.entries(cur?.provenance || {})) if (p === 'computed' && NODE_TAB[n]) regen.add(NODE_TAB[n]);
  $$('#tabs .tab').forEach((b) => b.classList.toggle('regen', regen.has(b.dataset.tab) && b.dataset.tab !== S.tab && !!cur?.parent));
  const n = Object.values(cur?.provenance || {}).filter((p) => p === 'computed').length;
  const tot = nodesOf(cur).length;
  const secs = Object.values(cur?.timings || {}).reduce((a, b) => a + (+b || 0), 0);
  $('#tabsInfo').innerHTML = cur ? `<span class="chip mono">${esc(cur.rev)}</span><span title="${fmtSec(secs)} of compute">${n}/${tot} rebuilt</span>` : '';
  return regen;
}

// ------------------------------------------------------------------ inspector
function renderInspector(cur, prev) {
  if (!cur) return;
  const s = cur.summary || {}, ps = prev?.summary || {}, d = cur.design || {};
  const issues = [...(s.issues || [])];
  for (const [name, sm] of Object.entries(cur.sims || {})) {
    const m = sm.metrics || {};
    if (m.success === false) {
      const why = [m.dropped && 'object dropped', m.place_error_mm != null && `place error ${fmt(m.place_error_mm, 'mm', 0)}`, (m.slip_mm ?? m.max_slip_mm) != null && `slip ${fmt(m.slip_mm ?? m.max_slip_mm, 'mm')}`].filter(Boolean).slice(0, 2).join(', ');
      issues.push(`Simulation · ${sm.title || name} failed${why ? ` (${why})` : ''}`);
    }
  }
  const warns = [...(cur.electronics?.warnings || []), ...(cur.manufacturing?.warnings || [])];
  const errs = Object.entries(cur.errors || {});
  const prov = Object.values(cur.provenance || {});
  const nComp = prov.filter((p) => p === 'computed').length;
  const feasible = s.feasible !== false && !issues.length;
  const changes = Object.entries(cur.changes || {});
  const flash = (k) => (prev && JSON.stringify(s[k]) !== JSON.stringify(ps[k]) ? ' flash' : '');
  const acts = (s.actuators || '').split(',').map((x) => x.trim()).filter(Boolean);
  const nAct = (s.dof || +d['arm.dof'] || 0) + 1;
  const facts = [
    ['DOF', s.dof ?? d['arm.dof'], '', 'dof'], ['Reach', s.reach_mm ?? d['arm.reach_mm'], 'mm', 'reach_mm'],
    ['Payload', s.payload_g ?? d['object.mass_g'], 'g', 'payload_g'],
    ['Actuators', acts.length === 1 ? `${acts[0].toUpperCase()} <small>×${nAct}</small>` : acts.length ? `${acts.length} types <small>×${nAct}</small>` : '–', null, 'actuators'],
    ['Mass', s.robot_mass_kg != null ? (+s.robot_mass_kg).toFixed(2) : '–', 'kg', 'robot_mass_kg'],
    ['Unit cost', s.unit_cost != null ? money(s.unit_cost) : '–', null, 'unit_cost'],
  ];
  const rows = [];
  for (const [label, k, unit, better] of METRICS) {
    const v = s[k], pv = ps[k];
    if (v == null && pv == null) continue;
    let delta = '', fl = '';
    if (prev && pv != null && JSON.stringify(v) !== JSON.stringify(pv)) {
      fl = ' flash';
      let cls = 'neutral', txt;
      if (typeof v === 'number' && typeof pv === 'number') {
        const dv = v - pv;
        if (better) cls = (dv > 0) === (better === 'hi') ? 'up' : 'down';
        txt = `${dv > 0 ? '▲' : '▼'} ${unit === '$' ? money(Math.abs(dv)) : fmt(Math.abs(dv), unit === '$' ? '' : unit)}`;
      } else if (typeof v === 'boolean') {
        if (better) cls = v === (better === 'hi') ? 'up' : 'down';
        txt = `was ${pv ? 'yes' : 'no'}`;
      } else txt = `was ${pretty(pv, true)}`;
      delta = `<span class="delta ${cls}">${esc(txt)}</span>`;
    }
    const val = typeof v === 'boolean' ? (v ? '<span class="tag good">pass</span>' : '<span class="tag bad">fail</span>')
      : unit === '$' ? money(v) : fmt(v, unit);
    rows.push(`<tr class="${fl.trim()}"><td>${esc(label)}</td><td class="v">${val}${delta}</td></tr>`);
  }
  const force = cur.gripper?.force;
  $('#insp').innerHTML = `
    <div class="revcard fade-in">
      <div class="top"><span class="rv">${esc(cur.rev)}</span>${feasible ? '<span class="tag good">' + I('check', 'sm') + 'all checks pass</span>' : `<span class="tag bad">${issues.length + errs.length} issue${issues.length + errs.length === 1 ? '' : 's'}</span>`}
        <span class="grow"></span>${cur.rev !== S.latest && S.latest ? '<span class="tag neutral">history</span>' : '<span class="tag acc">latest</span>'}</div>
      <div class="lb">${esc(cur.label || 'Revision ' + cur.rev)}</div>
      <div class="mt">${cur.parent ? `from <span class="mono">${esc(cur.parent)}</span> · ` : 'first revision · '}${nComp}/${prov.length || nodesOf(cur).length} stages regenerated</div>
    </div>
    <div class="isec"><div class="sec-t">Quick facts</div><div class="facts">${facts.map(([l, v, u, k]) => `<div class="fact${flash(k)}"><div class="l">${l}</div><div class="v">${v ?? '–'}${u && v != null && v !== '–' ? `<small>${u}</small>` : ''}</div></div>`).join('')}</div></div>
    <div class="isec"><div class="sec-t">What changed${prev ? `<span class="r">vs ${esc(prev.rev)}</span>` : ''}</div>
      ${changes.length ? `<div class="chg">${changes.map(([k, [a, b]]) => `<div class="c"><div class="k">${esc(k)}</div><div class="vv"><span class="o">${esc(pretty(a, true))}</span>${I('arrow', 'sm')}<span class="n">${esc(pretty(b, true))}</span></div></div>`).join('')}</div>`
        : `<div class="muted" style="font-size:12px">${cur.parent ? 'No parameter changes.' : 'First revision: everything was generated from the request.'}</div>`}</div>
    <div class="isec"><div class="sec-t">Metrics${prev ? `<span class="r">Δ vs ${esc(prev.rev)}</span>` : ''}</div><table class="mt-table">${rows.join('')}</table></div>
    ${force?.window_n && force.window_n[0] != null && force.window_n[1] != null ? `<div class="isec"><div class="sec-t">Grip force window<span class="r">per finger</span></div><div class="fw">${forceWindowSVG(force)}</div></div>` : ''}
    <div class="isec"><div class="sec-t">Issues &amp; warnings</div>
      ${errs.map(([n, e]) => `<div class="issue bad">${I('x', 'sm')}<span><b>${esc(n)}</b>: ${esc(e)}</span></div>`).join('')}
      ${issues.map((t) => `<div class="issue bad">${I('x', 'sm')}<span>${esc(t)}</span></div>`).join('')}
      ${warns.map((t) => `<div class="issue warn">${I('alert', 'sm')}<span>${esc(t)}</span></div>`).join('')}
      ${!issues.length && !warns.length && !errs.length ? `<div class="issue ok">${I('check', 'sm')}<span>No issues: torque margins, grasp force window, DRC and DFM checks pass.</span></div>` : ''}
    </div>`;
}
function inspectorSkeleton() {
  $('#insp').innerHTML = `<div class="skel" style="height:96px;margin-bottom:16px;border-radius:12px"></div>
    <div class="facts">${'<div class="skel" style="height:50px"></div>'.repeat(6)}</div>
    <div style="margin-top:22px">${'<div class="skel" style="height:14px;margin:10px 0"></div>'.repeat(8)}</div>`;
}
function pipelineSkeleton() {
  $('#pipeline').innerHTML = DEFAULT_NODES.map(([k], i) => `${i ? '<span class="link"></span>' : ''}<div class="node skel-n"><div class="skel" style="height:11px;width:80%"></div><div class="skel" style="height:9px;width:50%;margin-top:6px"></div></div>`).join('');
}

// ------------------------------------------------------------------ show a revision
async function show(cur, prev, { tour = false } = {}) {
  if (!cur) return;
  stopTour();
  S.current = cur;
  S.previous = prev;
  if (cur.label) S.labels[cur.rev] = cur.label;
  if (prev?.label) S.labels[prev.rev] = prev.label;
  renderHeader();
  renderPipeline(cur);
  renderParams(cur);
  renderInspector(cur, prev);
  const regen = markTabs(cur);
  // 3D robot: reload when its geometry was regenerated (or first time)
  const robot = cur.robot;
  const key = robot ? `${robot.glb}|${robot.json}` : null;
  const geomChanged = cur.provenance?.cad === 'computed' || cur.provenance?.sizing === 'computed';
  if (robot && (key !== S.robotKey) && (S.robotKey == null || geomChanged || !tour)) {
    S.robotKey = key;
    viewer.load(robot, prev?.robot || null);
  } else {
    viewer.prevRobot = prev?.robot || null;
    if (viewer.st.ghost) viewer.ghost(!!viewer.prevRobot);
  }
  for (const p of Object.values(panels)) {
    try { p.update(cur, prev); } catch (e) { console.error('panel update failed', e); }
  }
  if (tour && $('#tour').checked && cur.parent) runTour(cur, regen);
}

// ------------------------------------------------------------------ auto-tour
let tour = null;
function primaryTab(changes) {
  const ks = Object.keys(changes || {});
  if (ks.some((k) => /^electronics\.(config|mcu|extras|supply_v)/.test(k))) return 'elec';
  if (ks.some((k) => /^(build|fab)\./.test(k))) return 'mfg';
  if (ks.some((k) => /^task\./.test(k))) return 'sim';
  if (ks.some((k) => /^(arm|finger|object|gripper)\./.test(k))) return 'robot';
  return null;
}
function runTour(cur, regen) {
  const first = primaryTab(cur.changes);
  const order = ['robot', 'sim', 'elec', 'mfg', 'bom', 'asm'];
  const seq = [];
  if (first && (regen.has(first) || first === 'robot')) seq.push(first);
  for (const t of order) if (regen.has(t) && !seq.includes(t)) seq.push(t);
  if (!seq.length) return;
  const steps = seq.slice(0, 5);
  tour = { steps, i: 0, timer: null };
  const pill = $('#tourPill');
  const next = () => {
    if (!tour) return;
    if (tour.i >= steps.length) { stopTour(); return; }
    const t = steps[tour.i];
    showTab(t, { fromTour: true });
    let dur = 3400;
    try { dur = panels[t].tour?.(cur) ?? dur; } catch (e) { console.warn(e); }
    pill.innerHTML = `${I('play', 'sm')}<span>Tour · ${esc(tabName(t))}</span><span class="steps">${steps.map((s, j) => `<b class="${j < tour.i ? 'done' : j === tour.i ? 'cur' : ''}" style="--d:${dur}ms"></b>`).join('')}</span><button title="Stop tour">${I('x', 'sm')}</button>`;
    pill.classList.add('on');
    $('button', pill).onclick = stopTour;
    tour.i++;
    tour.timer = setTimeout(next, dur);
  };
  tour.timer = setTimeout(next, 450);
}
function stopTour() {
  if (tour) { clearTimeout(tour.timer); tour = null; }
  $('#tourPill').classList.remove('on');
}
const tabName = (t) => $(`#tabs .tab[data-tab="${t}"]`)?.textContent.trim() || t;

// ------------------------------------------------------------------ live events
let sysMsg = null, startT = 0, liveProv = null;
function onEvent(ev) {
  switch (ev.type) {
    case 'user': addMsg('user', ev.text); typing(true); break;
    case 'assistant': typing(false); addMsg('assistant', ev.text, ev.changes); break;
    case 'start': {
      typing(false);
      stopTour();
      startT = performance.now();
      liveProv = {};
      const nodes = nodesOf(S.current);
      const dirty = new Set(ev.dirty || nodes.map((n) => n[0]));
      for (const [k] of nodes) liveProv[k] = dirty.has(k) ? 'queued' : 'queued';
      renderPipeline(S.current, liveProv);
      const nd = ev.dirty ? ev.dirty.length : nodes.length;
      sysMsg = addMsg('system', `Regenerating ${ev.rev} from ${ev.parent || 'scratch'} · ${nd} of ${nodes.length} stages affected`);
      setConn('busy', 'Regenerating');
      renderRevs(ev.rev);
      break;
    }
    case 'node': if (liveProv) liveProv[ev.node] = ev.status; nodeEvent(ev); break;
    case 'project': reloadProject().catch((e) => console.warn(e)); break;
    case 'done': {
      const cur = ev.current;
      if (!S.revs.includes(cur.rev)) S.revs.push(cur.rev);
      S.latest = cur.rev;
      S.justAdded = cur.rev;
      const n = Object.values(cur.provenance || {}).filter((p) => p === 'computed').length;
      if (sysMsg) {
        sysMsg.classList.add('ok');
        $('.b', sysMsg).innerHTML = `${I('check', 'sm')}<span>${esc(cur.rev)} ready in ${ev.seconds ?? ((performance.now() - startT) / 1000).toFixed(1)} s · ${n} of ${nodesOf(cur).length} stages regenerated</span>`;
        sysMsg = null;
      }
      liveProv = null;
      setConn(backend.label === 'Fixture' ? 'fixture' : 'live');
      show(cur, ev.previous, { tour: true });
      break;
    }
    default: break;
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  document.body.style.opacity = '1';
  setupChat();
  setupParams();
  setupProjects();
  pipelineSkeleton();
  inspectorSkeleton();
  const views = Object.fromEntries($$('#views .view').map((v) => [v.dataset.view, v]));
  viewer = new ViewerAdapter($('#viewerHost'), ctx);
  panels = {
    robot: new RobotPanel(views.robot, ctx), sim: new SimPanel(views.sim, ctx), elec: new ElecPanel(views.elec, ctx),
    mfg: new MfgPanel(views.mfg, ctx), asm: new AsmPanel(views.asm, ctx), bom: new BomPanel(views.bom, ctx),
  };
  $$('#tabs .tab').forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const i = +e.key;
    if (i >= 1 && i <= TABS.length) showTab(TABS[i - 1]);
  });
  $('#tour').addEventListener('change', (e) => { if (!e.target.checked) stopTour(); });
  if (QS.get('tour') === '0') $('#tour').checked = false;
  const start = QS.get('tab');
  showTab(TABS.includes(start) ? start : 'robot');

  try {
    if (FIXTURE) { backend = new FixtureBackend(); await backend.load(); } else backend = new ServerBackend();
    const st = await backend.state();
    S.project = st.project;
    S.projects = st.projects || [];
    S.revs = st.revs || [];
    renderProjects();
    S.latest = st.current?.rev || null;
    for (const m of st.history || []) addMsg(m.type, m.text, m.changes);
    if (!(st.history || []).length) {
      addMsg('assistant', st.current
        ? `Loaded ${st.project} at ${st.current.rev}. Ask for a change, or edit a parameter, and I'll regenerate every stage that depends on it: CAD, simulation, electronics, manufacturing, sourcing and the assembly manual.`
        : 'Describe the robot you need, e.g. “a desktop arm that picks a raw egg without cracking it”.');
    }
    if (st.current) await show(st.current, st.previous, { tour: false });
    else { renderPipeline(null); $('#insp').innerHTML = '<div class="empty"><p>No revisions yet.</p></div>'; }
    backend.connect(onEvent, (k) => setConn(k));
    if (FIXTURE) setConn('fixture');
    // labels for older revisions (shown on the timeline chips), fetched in the background
    for (const r of S.revs.slice(-6)) if (!S.labels[r]) backend.rev(r).then((x) => { S.labels[r] = x.label; renderRevs(); }).catch(() => {});
    if (QS.get('demo')) setTimeout(() => sendChat(QS.get('demo')), 600);
  } catch (e) {
    console.error(e);
    setConn('off', 'Offline');
    $('#insp').innerHTML = `<div class="empty"><div><div class="ill">${I('alert', 'lg')}</div><h4>Can't reach the Studio server</h4><p>Start it with <span class="mono">python -m morph.cli studio &lt;project&gt;</span>, or open <span class="mono">?fixture=1</span> for the offline demo data.</p></div></div>`;
  }
}

window.morph = { show, showTab, state: S, get viewer() { return viewer; }, get panels() { return panels; }, onEvent, sendChat };
boot();

// Morph Studio v2: shell (header, chat, parameters, pipeline, tabs, inspector, live events, auto-tour).
// Tab bodies live in panels.js. Dev mode: index.html?fixture=1 runs entirely from ./fixtures/.
import {
  I, esc, fmt, money, pretty, sleep, h, ViewerAdapter, clearCaches,
  RobotPanel, SimPanel, ElecPanel, MfgPanel, AsmPanel, BomPanel, forceWindowSVG,
} from './panels.js';
import { Presenter } from './present.js';
import { KeysDialog } from './keys.js';
import * as newrobot from './newrobot.js';
import * as importer from './importer.js';
import * as video from './video.js';
import * as team from './team.js';
import * as world from './world.js';
import * as study from './study.js';
import * as tools from './tools.js';

// Feature modules (static/<feature>.js) export init(studio); each adds one import line above and one entry here.
const FEATURES = [newrobot, importer, video, team, world, study, tools];

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const QS = new URLSearchParams(location.search);
const FIXTURE = QS.has('fixture') && QS.get('fixture') !== '0';
const FIX_BASE = new URL('./fixtures/', import.meta.url).href;

const SUGGEST = [
  ['Swap to Dynamixel servos', 'cpu'], ['Use a custom PCB', 'chip'], ['Make it 6-DOF', 'axis'], ['Add a wrist camera', 'camera'],
  ['Pilot build: CNC + sheet metal', 'factory'], ['Make it work for a strawberry', 'leaf'], ['Increase reach to 550 mm', 'ruler'],
];
const TABS = ['robot', 'sim', 'elec', 'mfg', 'asm', 'bom'];
const NODE_TAB = { sizing: 'robot', gripper: 'robot', cad: 'robot', geometry: 'robot', arm: 'robot', sim: 'sim', simulate: 'sim',
  electronics: 'elec', wiring: 'elec', eplan: 'elec', manufacturing: 'mfg', mfg: 'mfg', bom: 'bom', manual: 'asm', assembly: 'asm' };
const DEFAULT_NODES = [['sizing', 'Sizing'], ['gripper', 'Gripper + grasp'], ['electronics', 'Electronics plan'], ['cad', 'Robot CAD'],
  ['sim', 'Simulation'], ['wiring', 'Schematic · PCB · FW'], ['manufacturing', 'Manufacturing'], ['bom', 'BOM + sourcing'], ['manual', 'Assembly manual']];
const METRICS = [
  ['Unit cost', 'unit_cost', '$', 'lo'], ['Pick & place', 'pick_place_success', '', 'hi'], ['Cycle time', 'cycle_time_s', 's', 'lo'],
  ['Max payload, top-down', 'max_payload_g', 'g', 'hi'], ['Payload stretched out', 'stretch_payload_g', 'g', 'hi'],
  ['Peak joint torque', 'max_torque_pct', '%', 'lo'], ['Min torque margin', 'min_torque_margin', '×', 'hi'],
  ['Top-down reach', 'top_down_reach_mm', 'mm', 'hi'], ['Reach, any pose', 'workspace_reach_mm', 'mm', 'hi'],
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

// Every request names this tab's project (?project=); changes also carry X-Morph-Client, which the server requires
// on POST, PUT and DELETE (another site's page can't send it). Its value names this tab (kept across reloads), so the
// server knows a quick second edit builds on this tab's first one, still building, instead of conflicting with it.
const TAB = (() => {
  const bytes = () => { try { return crypto.getRandomValues(new Uint8Array(8)); } catch (e) { return Array.from({ length: 8 }, () => Math.random() * 256 | 0); } };
  const make = () => Array.from(bytes(), (b) => b.toString(16).padStart(2, '0')).join('');
  try {
    let t = sessionStorage.getItem('morph.tab');
    if (!/^[0-9a-f]{16}$/.test(t || '')) { t = make(); sessionStorage.setItem('morph.tab', t); }
    return t;
  } catch (e) { return make(); }
})();
class ServerBackend {
  label = 'Live';
  constructor(project) { this.name = project || ''; this.gen = 0; this.es = null; }
  q(path, params = {}) {
    const u = new URL(path, location.origin);
    for (const [k, v] of Object.entries(params || {})) if (v != null) u.searchParams.set(k, v);
    if (this.name && !u.searchParams.has('project')) u.searchParams.set('project', this.name);
    return u.pathname + u.search;
  }
  async state() { return getFresh(this.q('/api/state')); }
  async rev(r) { return getFresh(this.q('/api/rev', { rev: r })); }
  chat(message, base_rev, edit = false) { return post(this.q('/api/chat'), { message, base_rev, ...(edit ? { edit: true } : {}) }); }
  params(changes, base_rev) { return post(this.q('/api/params'), { changes, base_rev }); }
  source(rev) { return post(this.q('/api/source'), { rev }); }
  resolve(job, action) { return post(this.q('/api/jobs/resolve'), { job, action }); }
  // true, or why it was refused (another Studio on the projects folder works on a demo project), or false (busy here)
  async reset(rebuild) { const r = await send('POST', '/api/reset', { rebuild: !!rebuild }); return r.ok || (await r.json().catch(() => ({}))).error || false; }
  setProject(name) { this.name = name; return this.onEvent ? this.connect(this.onEvent, this.onStatus) : Promise.resolve(); }
  // Resolves once the stream is open (the server subscribes it before answering), or at the latest after 3 s or a
  // failed try (it keeps trying, and its next open reloads the state): load the state after it and no event is lost.
  connect(onEvent, onStatus) {
    this.onEvent = onEvent; this.onStatus = onStatus;
    const gen = ++this.gen;
    this.dropped = false;  // set when the stream drops: its next open is a reconnect that may have missed events
    this.es?.close();
    let opened;
    const ready = new Promise((res) => { opened = res; setTimeout(res, 3000); });
    const go = () => {
      if (gen !== this.gen) return;
      const es = this.es = new EventSource(this.q('/api/events'));
      es.onopen = () => { const again = this.dropped; this.dropped = false; opened(); onStatus('live', again); };
      es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.warn('bad event', e); } };
      es.onerror = () => { es.close(); opened(); if (gen !== this.gen) return; this.dropped = true; onStatus('off'); setTimeout(go, 1500); };
    };
    go();
    return ready;
  }
}
async function getFresh(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw Object.assign(new Error(`${r.status} ${url}`), { status: r.status });
  return r.json();
}
function send(method, url, body) {
  return fetch(url, { method, headers: { 'Content-Type': 'application/json', 'X-Morph-Client': TAB }, body: body === undefined ? undefined : JSON.stringify(body) });
}
function post(url, body) {
  return send('POST', url, body)
    .then(async (r) => { if (!r.ok && r.status !== 409) toast((await r.clone().json().catch(() => ({}))).error || `The Studio refused that (${r.status})`); return r; })
    .catch((e) => toast(`Couldn't reach the Studio server (${e.message})`));
}

// Fixture backend: replays the pipeline locally so every animation can be exercised without a server.
const GRAPH = {
  sizing: [['arm.', 'object.mass_g', 'electronics.servo', 'electronics.extras'], []], gripper: [['finger.', 'object.', 'gripper.', 'safety_factor', 'electronics.target_force_n', 'electronics.servo'], []],
  electronics: [['electronics.'], ['sizing', 'gripper']], cad: [['finger.', 'object.shape', 'object.diameter_mm', 'build.', 'name'], ['sizing', 'electronics']],
  sim: [['task.', 'object.', 'arm.', 'finger.', 'build.', 'electronics.extras', 'world.'], ['sizing', 'gripper']], wiring: [[], ['electronics', 'cad']],
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
    const src = Object.values(d.revisions).reverse().find((r) => r.electronics?.config === ec && r.rev <= 'r003');
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
const S = { project: '', start: '', projects: [], revs: [], current: null, previous: null, latest: null, labels: {}, tab: 'robot', pending: {}, robotKey: null, integrations: {}, busy: false, queue: [], conflicts: [], loaded: false };
let backend, viewer, panels, presenter, keysDlg;

const ctx = {
  resolve, join, getJSON, getText, toast, fixture: FIXTURE,
  get current() { return S.current; }, get previous() { return S.previous; },
  get viewer() { return viewer; },
  postParams: (changes) => applyChanges(changes),
  source: (rev) => backend?.source?.(rev),
  showTab: (t, o) => showTab(t, o),
  isActive: (t) => S.tab === t,
};

function applyChanges(changes) {
  if (!backend) return;
  backend.params(changes, S.current?.rev);
  // pills show a busy shimmer until the next revision lands; clear it if the server declined the change
  setTimeout(() => $$('.pills button.busy').forEach((b) => b.classList.remove('busy')), 20000);
}

// ------------------------------------------------------------------ header
// The project menu switches this tab only (?project= in its address); other tabs and people stay where they are.
function renderProjects() {
  const list = S.projects || [];
  $('#projBtn').classList.toggle('multi', list.length > 1);
  $('#projList').innerHTML = list.map((p) => `<button type="button" data-p="${esc(p.name)}" class="${p.name === S.project ? 'on' : ''}" title="${esc(p.need || p.name)}"><b>${esc(p.name)}</b>${p.source ? `<span class="tag acc">imported · ${esc(p.source)}</span>` : '<span class="tag neutral">generated</span>'}${p.busy ? '<span class="tag warn">building</span>' : ''}${p.need ? `<small>${esc(p.need)}</small>` : ''}</button>`).join('');
  $$('#projMenu button[data-p]').forEach((b) => (b.onclick = () => {
    $('#projMenu').classList.remove('on');
    openProject(b.dataset.p);
  }));
}
function setupProjects() {
  $('#projBtn').onclick = (e) => { e.stopPropagation(); if ($('#projMenu').classList.toggle('on')) refreshProjects(); };  // fresh "building" tags
  document.addEventListener('click', (e) => { if (!e.target.closest('#projMenu')) $('#projMenu').classList.remove('on'); });
  $('#projActions').addEventListener('click', (e) => { if (e.target.closest('button')) $('#projMenu').classList.remove('on'); });  // a feature's item
}
async function refreshProjects() {
  if (!backend?.q) return;
  const out = await getFresh(backend.q('/api/projects')).catch(() => null);
  if (out) { S.projects = out.projects || S.projects; S.start = out.start || S.start; renderProjects(); }
}
function setUrlProject(name) { try { history.replaceState(null, '', urlWith({ project: name })); } catch (e) { /* file:// */ } }
async function openProject(name) {
  if (!name || !backend?.setProject) return false;
  if (name === S.project) return true;
  const prev = S.project;
  stopTour(); typing(false); sysMsg = null; liveProv = null; S.busy = false; S.queue = []; S.conflicts = [];
  S.project = name;
  const listening = backend.setProject(name);
  setUrlProject(name);
  try {
    await listening;  // listen first: a build that ends while this tab switches is in its state or in its events
    await reloadProject();
    emitBus({ type: 'project', project: name, switched: true });  // feature modules reload what they show for the new project
    return true;
  } catch (e) {
    console.warn(e);
    toast(`Couldn't open ${name}${e.status === 404 ? ': it no longer exists' : ''}`);
    if (prev && prev !== name) { S.project = prev; await backend.setProject(prev); setUrlProject(prev); await reloadProject().catch(() => {}); }
    return false;
  }
}
let reloading = null, reloadAgain = false;
function reloadProject() {  // one at a time; a request during a reload runs once more after it
  if (reloading) { reloadAgain = true; return reloading; }
  reloading = (async () => {
    try {
      do {
        reloadAgain = false;
        const st = await backend.state();
        if (backend.name && st.project !== backend.name) { reloadAgain = true; continue; }  // the tab switched meanwhile
        await loadState(st, { fresh: true });
      } while (reloadAgain);
    } finally { reloading = null; }
  })();
  return reloading;
}
async function loadState(st, { fresh = false, first = false } = {}) {
  if (fresh) {
    memo.clear();
    clearCaches();
    for (const p of Object.values(panels)) p.resetCache?.();
    S.labels = {}; S.robotKey = null; S.justAdded = null;
    $('#messages').innerHTML = '';
    typing(false); sysMsg = null; liveProv = null; S.busy = false;
  }
  S.project = st.project; S.start = st.start || S.start; S.projects = st.projects || S.projects; S.revs = st.revs || [];
  S.latest = st.current?.rev || null; S.queue = st.queue || []; S.conflicts = st.conflicts || [];  // conflicts first: the history's questions get their buttons
  S.integrations = st.integrations || S.integrations;
  S.compute = st.compute || S.compute;
  setUrlProject(st.project);
  renderKeysDot();
  renderProjects();
  for (const m of st.history || []) addMsg(m.type, m.text, m);
  S.chatEmpty = !(st.history || []).length;
  if (S.chatEmpty) {
    addMsg('assistant', !st.current ? `${st.project} has no finished revision yet${st.building ? ': its first build is running' : (st.queue || []).length ? ': its first build is waiting for its turn' : ''}.`
      : fresh && !first ? `Opened ${st.project} at ${st.current.rev}. ${st.current.label ? `“${st.current.label}”. ` : ''}Ask for a change or edit a parameter.`
        : `Loaded ${st.project} at ${st.current.rev}. Ask for a change, or edit a parameter, and I'll regenerate every stage that depends on it: CAD, simulation, electronics, manufacturing, sourcing and the assembly manual.${S.integrations.claude?.chat ? '' : S.integrations.claude?.configured ? ' (Network lookups are off under Keys: the suggestions below and common phrasings work.)' : ' (No Anthropic key yet: the suggestions below and common phrasings work; add a key under Keys, top right, for free-form requests.)'}`);
  }
  if (st.current) await show(st.current, st.previous, { tour: false });
  else {  // a project whose first build hasn't finished: nothing of the project shown before stays as its revision
    S.current = null; S.previous = null;
    renderHeader(); renderPipeline(null); renderParams(null); markTabs(null);
    $('#insp').innerHTML = '<div class="empty"><p>No finished revision yet.</p></div>';
    $('#viewsEmpty p').textContent = `No finished revision of ${st.project} yet.`;
    $('#views').classList.add('norev');  // the robot, its simulations and the other tabs wait for one
  }
  renderQueue([...S.queue, ...S.conflicts]);
  if (st.building) {  // opened mid-build: show the run so far; its events continue the strip
    const nodes = nodesOf(S.current), b = st.building;
    liveProv = Object.fromEntries(nodes.map(([k]) => [k, b.nodes?.[k] || 'queued']));
    renderPipeline(S.current, liveProv);
    S.busy = true;
    setConn('busy', 'Regenerating');
    renderRevs(b.rev);
    sysMsg = addMsg('system', `Regenerating ${b.rev} from ${b.parent || 'scratch'} · ${(b.dirty || []).length || nodes.length} of ${nodes.length} stages affected`);
  }
  S.reloads = (S.reloads || 0) + 1;
  S.loaded = true;
  for (const r of S.revs.slice(-6)) if (!S.labels[r]) backend.rev(r).then((x) => { S.labels[r] = x.label; renderRevs(); }).catch(() => {});
}

function renderKeysDot() {
  const i = S.integrations || {};
  const set = [['Claude', i.claude?.configured ?? i.claude?.chat], ['Keewano', i.keewano?.configured ?? (i.keewano?.send && i.keewano?.query)],
    ['SubQ', i.subq?.configured && !i.subq?.disabled]].filter(([, on]) => on).map(([name]) => name);
  const d = $('#keysBtn .kdot');
  if (d) { d.className = `kdot ${set.length === 3 ? 'on' : set.length ? 'some' : ''}`; $('#keysBtn').title = `API keys: ${set.length ? `${set.join(', ')} connected` : 'none set (optional)'}`; }
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
  const c = $('#conn'), cp = S.compute;
  c.className = 'conn ' + (kind === 'live' ? '' : kind);
  $('span', c).textContent = text || { live: cp?.remote ? `Live on ${cp.host}` : 'Live', off: 'Reconnecting', busy: 'Regenerating', fixture: 'Offline demo' }[kind] || kind;
  if (cp) c.title = cp.remote ? `Morph runs on ${cp.host} (${cp.cores} CPU cores); this browser only shows it` : `Morph runs on this computer (${cp.cores} CPU cores)`;
}

// ------------------------------------------------------------------ chat
// opts: {changes, job, conflict, noop, live} (a chat event); a conflict asks, with buttons, while it waits for an answer
// (live, or listed in the state's conflicts after a reload); once anyone answers it, every tab's buttons go
const waiting = (id) => (S.conflicts || []).some((j) => j.id === id);
function conflictActs(el, conflict) {
  const c = h('div', 'qacts qconf');
  c.dataset.job = conflict.job;
  c.innerHTML = `<button class="btn primary" type="button" data-q="apply">Apply to ${esc(conflict.rev)}</button><button class="btn ghost" type="button" data-q="drop">Drop it</button><span class="qnote" hidden>An editor can apply it or drop it.</span>`;
  $$('button', c).forEach((b) => (b.onclick = () => {
    $$('button', c).forEach((x) => (x.disabled = true));
    backend.resolve(conflict.job, b.dataset.q);
  }));
  el.lastElementChild.appendChild(c);
}
function gone() {  // a conflict someone answered (or that a restart forgot): its question stays, its buttons go
  const note = h('div', 'qgone');
  note.textContent = 'No longer waiting for an answer.';
  return note;
}
const answered = (c) => c.replaceWith(gone());
function pruneConflicts() {
  $$('#messages .qacts.qconf').forEach((c) => { if (!waiting(c.dataset.job)) answered(c); });
}
function addMsg(role, text, opts = {}) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  const changes = opts.noop ? null : opts.changes;
  if (role === 'assistant') {
    el.innerHTML = `<div class="av"><svg viewBox="0 0 24 24"><path d="M5 18V6l7 6 7-6v12"/></svg></div><div><div class="b"></div></div>`;
    $('.b', el).textContent = text;
    if (changes && Object.keys(changes).length) {
      const c = h('div', 'chips');
      c.innerHTML = Object.entries(changes).map(([k, v]) => `<span class="chip mono">${esc(k)} = <b>${esc(pretty(Array.isArray(v) ? v[1] : v, true))}</b></span>`).join('');
      el.lastElementChild.appendChild(c);
    }
    if (opts.conflict?.job && backend?.resolve) {
      if (opts.live || waiting(opts.conflict.job)) conflictActs(el, opts.conflict);
      else el.lastElementChild.appendChild(gone());
    }
    if (opts.offer && opts.live && backend?.chat) offerActs(el, opts.offer);
  } else if (role === 'system') {
    el.innerHTML = `<div class="b"><span class="spin"></span><span class="t"></span></div>`;
    $('.t', el).textContent = text;
  } else {
    el.innerHTML = '<div class="b"></div>';
    $('.b', el).textContent = text;
    if (opts.job) el.dataset.job = opts.job;
  }
  $('#messages').appendChild(el);
  scrollChat();
  return el;
}
// A message that read like another robot (or importing one) was not applied: offer that feature's dialog, or the edit
// after all ({edit: true} skips the routing).
function offerActs(el, offer) {
  const c = h('div', 'qacts');
  const go = offer.kind === 'import' ? `${I('download', 'sm')}Open Import` : `${I('spark', 'sm')}Start a new robot from this`;
  c.innerHTML = `<button class="btn primary" type="button" data-o="go">${go}</button><button class="btn ghost" type="button" data-o="edit">Change ${esc(S.project)} instead</button>`;
  $('[data-o="go"]', c).onclick = () => {
    if (offer.kind === 'import') started.get(importer)?.open?.(); else newrobot.open(offer.text || '');
  };
  $('[data-o="edit"]', c).onclick = () => {
    $$('button', c).forEach((x) => (x.disabled = true));
    backend.chat(offer.text, S.current?.rev, true);
  };
  el.lastElementChild.appendChild(c);
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
  backend.chat(text.trim(), S.current?.rev);
}
// queued requests: the user's bubble says where it waits; a job's last state comes in the event after it ends
const QSTATE = { conflict: 'needs an answer', cancelled: 'cancelled', dropped: 'dropped' };
function renderQueue(jobs = S.queue) {
  for (const j of jobs || []) {
    const el = $(`#messages .msg.user[data-job="${CSS.escape(j.id)}"]`);
    if (!el) continue;
    const text = j.status === 'queued' ? `queued · ${j.position === 1 ? 'next' : `#${j.position}`}` : QSTATE[j.status] || '';
    let tag = $('.qtag', el);
    if (!text) { tag?.remove(); continue; }
    if (!tag) { tag = h('span', 'qtag'); el.appendChild(tag); }
    tag.textContent = text;
  }
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
  // sizing may build a value other than the one asked for (reach raised to fit the pick and place): say so on its row
  const built = { 'arm.reach_mm': cur?.sizing?.reach_mm ?? cur?.summary?.reach_mm, 'arm.base_height_mm': cur?.sizing?.links_mm?.base_h };
  const hint = (p) => {
    const b = built[p.key];
    return b != null && typeof p.value === 'number' && Math.abs(b - p.value) >= 1 ? `built at ${Math.round(b)} ${p.unit || ''}`.trim() : '';
  };
  $('#paramsBody').innerHTML = groups.map((g) => `<div class="pgroup"><div class="sec-t">${esc(g.name)}<span class="r">${g.items.length}</span></div>` +
    g.items.map((p) => paramRow(p, p.key in changed, hint(p))).join('') + '</div>').join('') ||
    `<div class="empty"><p>${cur ? 'No editable parameters in this revision.' : 'No finished revision yet.'}</p></div>`;
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
function paramRow(p, regen, hint = '') {
  const lab = `<label title="${esc(p.key)}">${esc(p.label || p.key)}<small></small>${hint ? `<em class="phint">${esc(hint)}</em>` : ''}</label>`;
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
const STAGE_BAD = { error: 'failed', blocked: 'not built', preview: 'preview only', pending: 'not computed' };  // shown red
function renderPipeline(rev, live) {
  const nodes = nodesOf(rev);
  const prov = rev?.provenance || {}, tim = rev?.timings || {}, errs = rev?.errors || {};
  $('#pipeline').innerHTML = nodes.map(([k, label], i) => {
    let cls = '', sub = '—';
    const p = live ? live[k] : prov[k];
    if (live) { cls = p || 'queued'; sub = { queued: 'queued', computing: 'running…', reused: 'reused', computed: 'rebuilt', ...STAGE_BAD }[cls] || ''; if (STAGE_BAD[cls]) cls = 'error'; }
    else if (errs[k] || STAGE_BAD[p]) { cls = 'error'; sub = STAGE_BAD[p] || 'failed'; }
    else if (p === 'computed') { cls = 'computed'; sub = tim[k] >= 0.005 ? `rebuilt ${fmtSec(tim[k])}` : 'rebuilt'; }
    else if (p && (p.startsWith('reused') || p.startsWith('copied'))) { cls = 'reused'; sub = p.startsWith('copied') ? 'copied' : 'reused'; }
    const long = String(label).split(/\s+/).some((w) => w.length >= 12) ? ' long' : '';
    return `${i ? '<span class="link"></span>' : ''}<div class="node ${cls}${long}" data-node="${esc(k)}" title="${esc(label)}${errs[k] ? ' — ' + esc(errs[k]) : ''}"><div class="nt"><span>${esc(label)}</span></div><div class="ns"><i class="dot"></i><span>${esc(sub)}</span></div><div class="bar"></div></div>`;
  }).join('');
  $$('#pipeline .node').forEach((n) => (n.onclick = () => { const t = NODE_TAB[n.dataset.node]; if (t) { stopTour(); showTab(t); } }));
}
const fmtSec = (s) => (s < 0.1 ? `${Math.round(s * 1000)}ms` : `${(+s).toFixed(s < 10 ? 1 : 0)}s`);
function nodeEvent(ev) {
  const d = $(`#pipeline .node[data-node="${CSS.escape(ev.node)}"]`);
  if (!d) return;
  d.className = `node ${STAGE_BAD[ev.status] ? 'error' : ev.status}${ev.status === 'computed' ? ' just' : ''}`;
  $('.ns span', d).textContent = ev.status === 'computing' ? 'running…' : ev.status === 'computed' ? (ev.seconds >= 0.005 ? `rebuilt ${fmtSec(ev.seconds)}` : 'rebuilt') : STAGE_BAD[ev.status] || 'reused';
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
  $('#tabsInfo').innerHTML = cur ? `<span class="chip mono">${esc(cur.rev)}</span><span title="${fmtSec(secs)} of compute">${n}/${tot} rebuilt</span>` +
    (FIXTURE ? '' : `<button class="btn ghost xport" type="button" data-export="${esc(cur.rev)}" title="Download ${esc(cur.rev)} as a ZIP: CAD, simulations, electronics and firmware, manufacturing files and the manual, with a manifest of which stages completed. Generated files, not bench-validated: review them before building or powering anything.">${I('download', 'sm')}Export</button>`) : '';
  return regen;
}
async function exportRev(rev) {  // ask first, so a refusal is a message rather than a broken download
  const url = backend.q ? backend.q('/api/export', { rev }) : `/api/export?rev=${encodeURIComponent(rev)}`;
  const r = await fetch(`${url}&check=1`, { cache: 'no-store' }).catch(() => null);
  const out = r ? await r.json().catch(() => ({})) : {};
  if (!r?.ok) { toast(out.error || "Couldn't reach the Studio server"); return; }
  const a = Object.assign(document.createElement('a'), { href: url, download: '' });
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast(`Downloading ${rev} as a ZIP: generated files, not bench-validated`);
}

// ------------------------------------------------------------------ inspector
function renderInspector(cur, prev) {
  if (!cur) return;
  const s = cur.summary || {}, ps = prev?.summary || {}, d = cur.design || {};
  const issues = [...(s.issues || [])];
  // the summary already counts every failed scenario (sim_failed); one saved before that counted the pick and place only
  const counted = new Set(s.sim_failed || ['pick_place']);
  for (const [name, sm] of Object.entries(cur.sims || {})) {
    const m = sm.metrics || {};
    if (m.success === false && !counted.has(name)) {
      const why = [m.dropped && 'object dropped', m.place_error_mm != null && `place error ${fmt(m.place_error_mm, 'mm', 0)}`, (m.slip_mm ?? m.max_slip_mm) != null && `slip ${fmt(m.slip_mm ?? m.max_slip_mm, 'mm')}`].filter(Boolean).slice(0, 2).join(', ');
      issues.push(`Simulation · ${sm.title || name} failed${why ? ` (${why})` : ''}`);
    }
  }
  const warns = [...(cur.sizing?.warnings || []), ...(cur.electronics?.warnings || []), ...(cur.manufacturing?.warnings || [])];
  const adjusted = s.notes || cur.sizing?.notes || [];  // what sizing changed from the request ("reach raised from 320 to 475 mm")
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
    ['Object mass', s.payload_g ?? d['object.mass_g'], 'g', 'payload_g'],
    ['Actuators', acts.length === 1 ? `${acts[0].toUpperCase()} <small>×${nAct}</small>` : acts.length ? `${acts.length} types <small>×${nAct}</small>` : '–', null, 'actuators'],
    ['Mass', s.robot_mass_kg != null ? (+s.robot_mass_kg).toFixed(2) : '–', 'kg', 'robot_mass_kg'],
    ['Unit cost', s.unit_cost != null ? money(s.unit_cost) : '–', null, 'unit_cost'],
  ];
  // stretched straight out, an arm whose servo can't hold even its own weight has no payload there: never a negative one
  const noStretch = (x) => x.stretch_holds_itself === false || (typeof x.stretch_payload_g === 'number' && x.stretch_payload_g < 0);
  const stretchJoint = String(s.stretch_limited_by || '').replace(/^j\d+_/, '').replace(/_/g, ' ');
  const rows = [];
  for (const [label, k, unit, better] of METRICS) {
    let v = s[k], pv = ps[k];
    if (k === 'stretch_payload_g') {     // a revision saved before the summary clamped it may hold the deficit
      if (typeof v === 'number' && v < 0) v = 0;
      if (typeof pv === 'number' && pv < 0) pv = 0;
    }
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
    const val = k === 'stretch_payload_g' && noStretch(s) ? '<span class="tag warn">none</span>'
      : typeof v === 'boolean' ? (v ? '<span class="tag good">pass</span>' : '<span class="tag bad">fail</span>')
      : unit === '$' ? money(v) : fmt(v, unit);
    // say where each payload was measured: the sweep's top-down pose, or stretched straight out
    const at = { max_payload_g: s.payload_test_reach_mm, stretch_payload_g: s.stretch_reach_mm }[k];
    const nq = +(d['build.quantity'] ?? 1) || 1;   // mold tooling is paid once: say how much of the unit cost it is
    const lab = k === 'min_torque_margin' && s.torque_margin_basis ? `${label} (at ${s.torque_margin_basis})`
      : k === 'unit_cost' && s.tooling_cost ? `${label} (incl. ${money(s.tooling_cost, false)} tooling over ${nq.toLocaleString('en-US')} unit${nq === 1 ? '' : 's'})`
      : at ? `${label} at ${Math.round(at)} mm` : label;
    rows.push(`<tr class="${fl.trim()}"><td>${esc(lab)}</td><td class="v">${val}${delta}</td></tr>`);
  }
  const force = cur.gripper?.force;
  $('#insp').innerHTML = `
    <div class="revcard fade-in">
      <div class="top"><span class="rv">${esc(cur.rev)}</span>${feasible ? '<span class="tag good">' + I('check', 'sm') + 'software checks pass</span>' : `<span class="tag bad">${issues.length + errs.length} issue${issues.length + errs.length === 1 ? '' : 's'}</span>`}
        <span class="grow"></span>${cur.rev !== S.latest && S.latest ? '<span class="tag neutral">history</span>' : '<span class="tag acc">latest</span>'}</div>
      <div class="lb">${esc(cur.label || 'Revision ' + cur.rev)}</div>
      <div class="mt">${cur.parent ? `from <span class="mono">${esc(cur.parent)}</span> · ` : 'first revision · '}${nComp}/${prov.length || nodesOf(cur).length} stages regenerated</div>
    </div>
    ${timeCard(cur)}
    <div class="isec"><div class="sec-t">Quick facts</div><div class="facts">${facts.map(([l, v, u, k]) => `<div class="fact${flash(k)}"><div class="l">${l}</div><div class="v">${v ?? '–'}${u && v != null && v !== '–' ? `<small>${u}</small>` : ''}</div></div>`).join('')}</div></div>
    <div class="isec"><div class="sec-t">What changed${prev ? `<span class="r">vs ${esc(prev.rev)}</span>` : ''}</div>
      ${changes.length ? `<div class="chg">${changes.map(([k, [a, b]]) => `<div class="c"><div class="k">${esc(k)}</div><div class="vv"><span class="o">${esc(pretty(a, true))}</span>${I('arrow', 'sm')}<span class="n">${esc(pretty(b, true))}</span></div></div>`).join('')}</div>`
        : `<div class="muted" style="font-size:12px">${cur.parent ? 'No parameter changes.' : 'First revision: everything was generated from the request.'}</div>`}</div>
    <div class="isec"><div class="sec-t">Metrics${prev ? `<span class="r">Δ vs ${esc(prev.rev)}</span>` : ''}</div><table class="mt-table">${rows.join('')}</table>
      ${s.max_payload_g != null || s.stretch_payload_g != null ? '<div class="mt-note">Payloads are the most the arm holds before a servo stalls, at that distance from the base axis: not a working load. Sizing keeps a torque margin over the object for that.</div>' : ''}
      ${noStretch(s) ? `<div class="mt-note stretch-none">Stretched out${s.stretch_reach_mm != null ? ` at ${Math.round(s.stretch_reach_mm)} mm` : ''} the arm can't hold even its own weight${stretchJoint ? `: its ${esc(stretchJoint)} servo stalls` : ''}. It carries payloads only closer in.</div>` : ''}</div>
    ${force?.window_n && force.window_n[0] != null && force.window_n[1] != null ? `<div class="isec"><div class="sec-t">Grip force window<span class="r">per finger</span></div><div class="fw">${forceWindowSVG(force)}</div>${keewanoLine(cur)}</div>` : ''}
    <div class="isec"><div class="sec-t">Issues &amp; warnings</div>
      ${errs.map(([n, e]) => `<div class="issue bad">${I('x', 'sm')}<span><b>${esc(n)}</b>: ${esc(e)}</span></div>`).join('')}
      ${issues.map((t) => `<div class="issue bad">${I('x', 'sm')}<span>${esc(t)}</span></div>`).join('')}
      ${warns.map((t) => `<div class="issue warn">${I('alert', 'sm')}<span>${esc(t)}</span></div>`).join('')}
      ${adjusted.map((t) => `<div class="issue info">${I('wrench', 'sm')}<span><b>Morph adjusted</b> ${esc(t)}</span></div>`).join('')}
      ${!issues.length && !warns.length && !errs.length ? `<div class="issue ok">${I('check', 'sm')}<span>No issues: torque margins, grasp force window, DRC and DFM checks pass.</span></div>` : ''}
    </div>`;
}
// "By hand" = cited engineering hours for every step this revision touched (morph/robot/time_baseline.json);
// "With Morph" = measured compute + a stated review allowance. See docs/TIME_SAVED.md.
const hrs = (m) => (m >= 120 ? `${fmt(m / 60, '', m >= 6000 ? 0 : 1)}<small>h</small>` : `${fmt(m, '', 0)}<small>min</small>`);
const mins = (m) => (m < 1 ? `${fmt(m * 60, '', 0)}<small>s</small>` : `${fmt(m, '', m < 10 ? 1 : 0)}<small>min</small>`);
function timeCard(cur) {
  const t = cur.time_study;
  if (!t || t.error || !t.revision) return '';
  const r = t.revision, p = t.project || {};
  const sp = r.speedup ? `${Math.round(r.speedup).toLocaleString('en-US')}×` : '';
  const pr = (p.revisions || []).length;
  const bar = r.before_min ? Math.max(0.6, Math.min(100, r.after_min / r.before_min * 100)) : 0;
  return `<div class="isec tcard fade-in" id="timeCard">
    <div class="sec-t">${I('clock', 'sm')}Time saved<span class="r">${r.first ? 'whole design' : 'this change'}</span></div>
    <div class="tc-main"><div class="tc-col"><div class="l">By hand</div><div class="v">${hrs(r.before_min)}</div><div class="s">${r.first ? `≈ ${fmt(r.before_min / 60 / 40, '', 1)} engineer-weeks` : `${r.rows.length} step${r.rows.length === 1 ? '' : 's'} redone`}</div></div>
      <div class="tc-x">${sp ? `<b>${sp}</b>` : ''}${I('arrow', 'sm')}</div>
      <div class="tc-col good"><div class="l">With Morph</div><div class="v">${mins(r.after_min)}</div><div class="s">${fmt(r.compute_min * 60, '', 0)} s compute + ${fmt(r.review_min, '', 0)} min review</div></div></div>
    <div class="tc-bar"><b style="width:${bar}%"></b></div>
    ${pr > 1 ? `<div class="tc-proj">Project so far · ${pr} revisions: <b>${hrs(p.before_min)}</b> by hand → <b class="g">${mins(p.after_min)}</b></div>` : ''}
    <details class="tc-d"><summary>What's counted (cited)</summary>${r.rows.map((x) => `<div class="tc-r"><span>${esc(x.label)}</span><span class="num">${hrs(x.before_min)}</span></div>`).join('')}
      <div class="tc-foot">Typical published effort per step (surveys and case studies, see docs/TIME_SAVED.md)${r.first ? '' : `; a change costs ${fmt(r.per_change_share * 100, '', 0)}% of a full design`}. Review time is ${r.review_assumed ? 'an allowance, not measured' : 'logged'}.</div></details>
  </div>`;
}
function keewanoLine(cur) {
  const f = cur.gripper?.force || {}, cal = f.hold_calibration, kw = S.integrations?.keewano || {};
  const what = `${esc(pretty(cur.design?.['finger.pad']))} · ${esc(cur.design?.['finger.material'] || '')}`;
  if (cal) return `<div class="kw on">${I('spark', 'sm')}<span><b>Keewano calibration</b>: holding force ×${fmt(cal.ratio, '', 2)} from ${cal.n} logged ${what} builds. Seeded demo data unless your Keewano project holds real bench results.</span></div>`;
  if (kw.calibration) return `<div class="kw">${I('spark', 'sm')}<span><b>Keewano calibration</b> is on but not in this revision: it needs 3 or more logged ${what} builds and applies when a robot is generated</span></div>`;
  if (kw.query) return `<div class="kw off">${I('spark', 'sm')}<span>Keewano calibration off (Keys): simulation-only target</span></div>`;
  return `<div class="kw off">${I('spark', 'sm')}<span>Keewano not connected: simulation-only target</span></div>`;
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
  $('#views').classList.remove('norev');
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
  const has = {
    robot: () => !!cur.robot, sim: () => Object.keys(cur.sims || {}).length > 0, elec: () => !!cur.electronics,
    mfg: () => !!cur.manufacturing, bom: () => !!cur.bom?.rows?.length, asm: () => !!cur.manual?.steps?.length,
  };
  const seq = [];
  if (first && (regen.has(first) || first === 'robot') && has[first]()) seq.push(first);
  for (const t of order) if (regen.has(t) && !seq.includes(t) && has[t]()) seq.push(t);
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
const GLOBAL_EVENTS = new Set(['project_created', 'project_deleted']);  // about other projects: every tab gets them
function onEvent(ev) {
  if (ev.project && S.project && ev.project !== S.project && !GLOBAL_EVENTS.has(ev.type)) return;  // a stream we just left
  switch (ev.type) {
    case 'user': addMsg('user', ev.text, ev); if (!S.busy) typing(true); break;
    case 'assistant': typing(false); addMsg('assistant', ev.text, { ...ev, live: true }); if (/^(Error|Couldn't apply)/.test(ev.text || '')) { S.busy = false; S.errorAt = performance.now(); } if (ev.noop) S.noopAt = performance.now(); break;
    case 'queue': {
      S.queue = (ev.jobs || []).filter((j) => j.position != null);
      if (ev.conflicts) { S.conflicts = ev.conflicts; pruneConflicts(); }
      renderQueue([...(ev.jobs || []), ...S.conflicts]);
      const run = (ev.jobs || []).find((j) => j.status === 'running');
      if (run?.kind === 'chat' && $(`#messages .msg.user[data-job="${CSS.escape(run.id)}"]`)) typing(true);
      break;
    }
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
      S.busy = true;
      renderRevs(ev.rev);
      break;
    }
    case 'node':
      if (!liveProv) {  // a build that started before this tab was listening, while its state can't load yet
        liveProv = Object.fromEntries(nodesOf(S.current).map(([k]) => [k, 'queued']));
        renderPipeline(S.current, liveProv);
        S.busy = true;
        setConn('busy', 'Regenerating');
        sysMsg = sysMsg || addMsg('system', 'Regenerating: a build was already running when this tab opened');
      }
      liveProv[ev.node] = ev.status;
      nodeEvent(ev);
      break;
    case 'sourcing':
      if (ev.project && ev.project !== S.project) break;  // a search still running for the other robot
      panels.bom.onSourcing(ev);
      if (ev.status === 'done' && S.current?.rev === ev.rev) S.current.sourcing = ev.sourcing;
      break;
    case 'project': case 'resync': reloadProject().catch((e) => console.warn(e)); break;
    case 'project_created': case 'project_deleted':
      if (ev.type === 'project_deleted' && ev.project === S.project) { toast(`${ev.project} was deleted`); openProject(S.start); break; }
      refreshProjects();
      break;
    case 'done': {
      typing(false);
      const cur = ev.current;
      if (!S.revs.includes(cur.rev)) S.revs.push(cur.rev);
      S.latest = cur.rev;
      S.justAdded = cur.rev;
      const n = Object.values(cur.provenance || {}).filter((p) => p === 'computed').length;
      const bad = Object.keys(cur.errors || {}).length;
      if (sysMsg) {
        sysMsg.classList.add('ok');
        $('.b', sysMsg).innerHTML = `${I(bad ? 'alert' : 'check', 'sm')}<span>${esc(cur.rev)} ${bad ? `finished with ${bad} stage${bad === 1 ? '' : 's'} not built` : 'ready'} in ${ev.seconds ?? ((performance.now() - startT) / 1000).toFixed(1)} s · ${n} of ${nodesOf(cur).length} stages regenerated</span>`;
        sysMsg = null;
      }
      liveProv = null;
      S.busy = false;
      setConn(backend.label === 'Fixture' ? 'fixture' : 'live');
      show(cur, ev.previous, { tour: true });
      break;
    }
    default: break;
  }
  emitBus(ev);  // feature modules: studio.on(type, fn)
}

// ------------------------------------------------------------------ present mode (press-through demo)
async function until(cond, timeout = 20000, onProgress) {
  const t0 = performance.now();
  while (performance.now() - t0 < timeout) {
    if (cond()) return true;
    if (onProgress && liveProv) {
      const v = Object.values(liveProv), done = v.filter((x) => x && x !== 'queued' && x !== 'computing').length;
      onProgress(`Regenerating… ${done}/${v.length} stages`);
    }
    await sleep(200);
  }
  return false;
}
function presentApi() {
  const tb = () => panels.robot.tb, b = (k) => tb().querySelector(`[data-a="${k}"]`);
  return {
    state: S, until, toast,
    tab: (t) => showTab(t),
    chat: (t) => sendChat(t),
    params: (c) => applyChanges(c),
    tourOn: () => $('#tour').checked,
    project: (name) => openProject(name),  // this tab only
    async reset(rebuild) {  // back to the start project, in this tab; true, or why not (backend.reset)
      const res = await backend.reset?.(rebuild).catch(() => false);
      if (res === true) { if (S.start && S.project !== S.start) await openProject(S.start); else await reloadProject().catch(() => {}); }
      return res;
    },
    robot(o) {
      if ('motion' in o && !!panels.robot.motionOn !== o.motion) b('motion').click();
      if ('xray' in o && !!viewer.st.xray !== o.xray) b('xray').click();
      if ('labels' in o && !!viewer.st.labels !== o.labels) b('labels').click();
    },
    explode(f) {
      const el = b('explode'), from = +el.value, to = Math.round(f * 100), t0 = performance.now(), dur = 1100;
      if (from === to) return Promise.resolve();
      return new Promise((res) => {
        const step = (t) => {
          const k = Math.min(1, (t - t0) / dur), e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
          el.value = String(Math.round(from + (to - from) * e));
          el.dispatchEvent(new Event('input'));
          if (k < 1) requestAnimationFrame(step); else res();
        };
        requestAnimationFrame(step);
      });
    },
    home: () => viewer?.home?.(),
    async sim(name) { await panels.sim.select(name); panels.sim.t = 0; panels.sim.setPlaying(true); },
    elec: (sub) => panels.elec.showSub(sub, true),
    asm() { panels.asm.go(0); panels.asm.startAuto(); },
    darwin: () => panels.bom.findAgents(),
    spotlight(sel) {
      const el = $(sel)?.closest('.isec') || $(sel);
      if (!el) return;
      // scroll only the inspector column (scrollIntoView would also nudge the whole window)
      const box = el.closest('#inspector');
      if (box) {
        const r = el.getBoundingClientRect(), b = box.getBoundingClientRect();
        box.scrollTo({ top: Math.max(0, box.scrollTop + r.top - b.top - box.clientHeight / 2 + r.height / 2), behavior: 'smooth' });
      }
      el.classList.remove('spot'); void el.offsetWidth; el.classList.add('spot');
      clearTimeout(el._spot); el._spot = setTimeout(() => el.classList.remove('spot'), 7000);
    },
  };
}

// ------------------------------------------------------------------ feature contract (docs/internal/RELEASE_CONTRACTS.md, 4)
// One object for every feature module's init(studio): state, this tab's project, the API (with ?project= and
// X-Morph-Client), live events by type, the chat, one shared dialog, and DOM slots to add buttons and panels to.
const bus = new Map();
function emitBus(ev) {
  for (const fn of [...(bus.get(ev.type) || []), ...(bus.get('*') || [])]) {
    try { fn(ev); } catch (e) { console.error(`[morph] ${ev.type} listener failed`, e); }
  }
}
async function apiCall(method, path, params, body, raw = false) {
  const url = backend?.q ? backend.q(path, params) : path;
  const headers = method === 'GET' ? {} : { 'X-Morph-Client': TAB };
  let data;
  if (body != null) {
    data = raw ? body : JSON.stringify(body);
    headers['Content-Type'] = raw ? (body.type || 'application/octet-stream') : 'application/json';
  }
  const r = await fetch(url, { method, headers, body: data, cache: 'no-store' });
  const json = (r.headers.get('Content-Type') || '').includes('application/json');
  const out = json ? await r.json().catch(() => null) : await r.text().catch(() => '');
  if (!r.ok) throw Object.assign(new Error((json && out?.error) || `${r.status} ${path}`), { status: r.status, body: out });
  return out;
}
let modalEl = null, modalClose = null;
const modal = {
  open(content, { wide = false, onClose = null } = {}) {  // one dialog at a time, styled like the Keys dialog
    if (!modalEl) {
      modalEl = h('div', 'modal');
      modalEl.id = 'featModal';
      modalEl.addEventListener('click', (e) => { if (e.target === modalEl) modal.close(); });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && modalEl.classList.contains('on')) modal.close(); });
      document.body.appendChild(modalEl);
    }
    modalClose?.();
    let dlg = content;  // a feature's own dialog element (.dlg) is used as it is, never wrapped in a second one
    if (!(content instanceof Element && content.classList.contains('dlg'))) {
      dlg = h('div', 'glass dlg');
      if (typeof content === 'string') dlg.innerHTML = content; else dlg.appendChild(content);
    }
    dlg.classList.toggle('wide', !!wide);
    dlg.setAttribute('role', 'dialog');
    modalEl.replaceChildren(dlg);
    modalEl.classList.add('on');
    modalClose = onClose;
    return dlg;
  },
  close() {
    if (!modalEl?.classList.contains('on')) return;
    modalEl.classList.remove('on');
    modalEl.replaceChildren();
    const fn = modalClose;
    modalClose = null;
    fn?.();
  },
};
const studio = {
  S,
  project: () => S.project,
  api: {
    get: (path, params) => apiCall('GET', path, params),
    post: (path, body) => apiCall('POST', path, null, body ?? {}),
    put: (path, blob, params) => apiCall('PUT', path, params, blob, true),
    del: (path, params) => apiCall('DELETE', path, params),
  },
  on(type, fn) {
    if (!bus.has(type)) bus.set(type, new Set());
    bus.get(type).add(fn);
    return () => bus.get(type)?.delete(fn);
  },
  toast, addMsg, esc, I, modal,
  openProject: (name) => openProject(name),
  reload: () => reloadProject(),
  mount: {},
};
const started = new Map();  // feature module -> what its init() returned (the chat's offers open their dialogs)
async function initFeatures() {
  for (const f of FEATURES) {
    try { started.set(f, await f.init?.(studio)); } catch (e) { console.error('[morph] a feature failed to start', e); }
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  document.body.style.opacity = '1';
  // the app is a fixed-viewport layout; never let a child's scrollIntoView shift the whole window
  const pin = () => { if (window.scrollY || document.scrollingElement?.scrollTop) window.scrollTo(0, 0); };
  window.addEventListener('scroll', pin, { passive: true });
  document.addEventListener('scroll', pin, { passive: true, capture: false });
  // a file dropped where no feature takes it (over an open dialog, say) must not make the browser leave the Studio for it
  const files = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragover', (e) => { if (files(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => { if (files(e)) e.preventDefault(); });
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
  viewer.onState = () => panels.robot.sync();
  const viewerTools = h('div', 'feat-slot');
  viewerTools.id = 'viewerTools';
  panels.robot.tb.appendChild(viewerTools);
  studio.mount = { header: $('#featHeader'), projMenu: $('#projActions'), chatTools: $('#chatTools'), inspectorTabs: $('#inspTabs'), viewerTools };
  $$('#tabs .tab').forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  $('#tabsInfo').addEventListener('click', (e) => { const b = e.target.closest('[data-export]'); if (b) exportRev(b.dataset.export); });
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const i = +e.key;
    if (i >= 1 && i <= TABS.length) showTab(TABS[i - 1]);
  });
  $('#tour').addEventListener('change', (e) => { if (!e.target.checked) stopTour(); });
  presenter = new Presenter(presentApi());
  $('#presentBtn').onclick = () => presenter.toggle();
  keysDlg = new KeysDialog({
    toast,
    reset: (rebuild) => presentApi().reset(rebuild),
    onChanged(integrations) { S.integrations = integrations || S.integrations; renderKeysDot(); if (S.current) renderInspector(S.current, S.previous); },
  });
  $('#keysBtn').onclick = () => keysDlg.open();
  const insp = (on) => { document.body.classList.toggle('insp-open', on); $('#inspBtn').setAttribute('aria-pressed', String(on)); };
  $('#inspBtn').onclick = () => insp(!document.body.classList.contains('insp-open'));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('.modal.on')) insp(false); });
  document.addEventListener('keydown', (e) => {
    if ((e.key === 'p' || e.key === 'P') && !e.target.closest('input, textarea, select') && !e.metaKey && !e.ctrlKey && !e.altKey) presenter.toggle();
  });
  if (QS.get('tour') === '0') $('#tour').checked = false;
  const start = QS.get('tab');
  showTab(TABS.includes(start) ? start : 'robot');

  if (FIXTURE) {
    try {
      backend = new FixtureBackend();
      await backend.load();
      await loadState(await backend.state());
      backend.connect(onEvent, (k) => setConn(k));
      setConn('fixture');
      opened();
    } catch (e) { console.error(e); unreachable(e); }
    await initFeatures();
    return;
  }
  // Listen first: a tab opened mid-build follows the build from its events even while its state can't load. The
  // state is retried with backoff (and as soon as the event stream connects) until the server answers.
  backend = new ServerBackend(QS.get('project'));
  backend.connect(onEvent, onConn);
  let features = null;
  for (let attempt = 0; ; attempt++) {
    try {
      const st = await firstState();
      const was = backend.name;
      if (was !== st.project) { backend.name = st.project; if (was) backend.connect(onEvent, onConn); }
      await loadState(st, { fresh: attempt > 0, first: true });
      if (backend.es?.readyState === 1) onConn('live');
      break;
    } catch (e) {
      console.warn(e);
      if (e.status === 401) { location.href = '/'; return; }  // a remote Studio restarted with a new access code
      const wait = Math.min(15, 2 ** Math.min(attempt, 4));
      unreachable(e, wait);
      features = features || initFeatures();  // features start even when the server can't be reached
      await retryPause(wait * 1000);
    }
  }
  opened();
  await (features || initFeatures());
  featureTips();
}
// A project with no conversation yet: one message naming the feature entry points this page actually has.
function featureTips() {
  if (!S.current || !S.chatEmpty || $('#messages .msg.user')) return;
  const tips = [
    [$('#featHeader .nr-headbtn'), 'New robot (top right): another robot from one sentence'],
    [$('#importBtn'), 'Import (top right): a robot folder or zip, an object mesh, a parts list or a board'],
    [$('#chatTools .vj-btn'), 'Show Morph the job (below): a phone video or photos of the task'],
    [$('#viewerTools button'), 'World (over the robot, beta): simulate it in a room'],
  ].filter(([el]) => el).map(([, t]) => `· ${t}`);
  if (tips.length) addMsg('assistant', `Also here:\n${tips.join('\n')}`);
}
async function firstState() {
  try { return await backend.state(); } catch (e) {
    if (![400, 404].includes(e.status) || !backend.name) throw e;
    toast(`There is no project called ${backend.name}; opening the start project`);  // a deleted project's link
    backend.name = '';
    return backend.state();
  }
}
function opened() {
  if (QS.get('demo')) setTimeout(() => sendChat(QS.get('demo')), 600);
  if (QS.get('present')) presenter.open();
}
let wake = null;  // ends a retry pause early: the event stream just connected, so the server is back
function retryPause(ms) { return new Promise((res) => { const t = setTimeout(res, ms); wake = () => { clearTimeout(t); res(); }; }); }
function onConn(kind, again) {
  if (kind !== 'live') return setConn(kind);
  if (wake) { wake(); wake = null; }
  if (again && S.loaded) reloadProject().catch((e) => console.warn(e));  // back after a drop: events may be missing
  emitBus({ type: 'connection', live: true, again: !!again });  // features reload what a late or dropped stream missed
  if (S.busy) setConn('busy', 'Regenerating'); else if (S.loaded) setConn('live'); else setConn('busy', 'Connecting');
}
function unreachable(e, wait = 0) {
  if (S.current || S.busy) return;  // events already show a build or a revision: keep it on screen
  setConn('off', wait ? 'Reconnecting' : 'Offline');
  const why = e?.status ? `The Studio answered ${e.status}.` : wait ? "The Studio server isn't answering." : "Couldn't load the demo data.";
  $('#insp').innerHTML = `<div class="empty"><div><div class="ill">${I('alert', 'lg')}</div><h4>Can't load the project yet</h4><p>${why}${wait ? ` Trying again in ${wait} s.` : ''} If the server isn't running, start it with <span class="mono">python -m morph.cli studio &lt;project&gt;</span>, or open <span class="mono">?fixture=1</span> for the offline demo data.</p></div></div>`;
}

window.morph = { show, showTab, state: S, studio, get viewer() { return viewer; }, get panels() { return panels; }, get presenter() { return presenter; }, onEvent, sendChat };
boot();

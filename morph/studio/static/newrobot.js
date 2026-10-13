// Morph Studio: New robot. Describe the job in one line, answer a few chip questions, confirm, build.
// A feature module (RELEASE_CONTRACTS section 4): init(studio) adds the New robot buttons and the dialog; open(text) starts it.
// Talks to POST/GET /api/brief and follows the brief, agent, project_created, start, node and done events. Styles: newrobot.css.

const ROLES = [['triage', 'Triage'], ['designer', 'Designer'], ['feasibility', 'Feasibility'], ['pipeline', 'Pipeline'],
  ['sourcing', 'Sourcing'], ['memory', 'Memory']];
const PROVIDER = { subq: 'SubQ Snap (AI)', cache: 'SubQ, cached', claude: 'Claude', rules: 'Built-in rules', code: 'Morph code',
  darwin: 'Darwin', keewano: 'Keewano', morph: 'Morph code' };
const STAGES = [['sizing', 'Sizing'], ['gripper', 'Gripper'], ['electronics', 'Electronics'], ['cad', 'CAD'], ['sim', 'Simulation'],
  ['wiring', 'PCB and firmware'], ['manufacturing', 'Manufacturing'], ['bom', 'BOM'], ['manual', 'Manual']];
const EXAMPLES = ['Pick strawberries from a tray into punnets', 'Sort rolled socks into bins', 'Move 50 ml tubes between racks in our lab',
  'Hand me screwdrivers at my workbench', 'I have an SO-101: pick up eggs'];
const BADGE = { user: 'acc', video: 'acc', photo: 'acc', inferred: 'warn', memory: 'warn', default: 'neutral' };
const KIND = { default: ['default', 'neutral'], inferred: ['assumed', 'warn'], repair: ['fixed', 'hot'], scope: ['scope', 'acc'] };
const FAMILY = { hobby_pwm: 'Hobby PWM', smart_bus: 'Smart bus', dynamixel: 'Dynamixel' };
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
const ICON = {  // used when studio.I has no such icon
  bot: '<rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 8V4M9 13h.01M15 13h.01M9 16.5h6"/><circle cx="12" cy="3.5" r="1"/>',
};

let studio = null;
let root = null;
let st = null;
let reqSeq = 0;  // every POST gets an id; a poll only answers for the request that started it
const timers = new Set();

const esc = (s) => (studio?.esc ? studio.esc(s) : String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
function I(name, size = '') {
  const own = ICON[name];
  if (own) return `<svg class="i ${size}" viewBox="0 0 24 24" aria-hidden="true">${own}</svg>`;
  return studio?.I ? studio.I(name, size) : '';
}
const later = (fn, ms) => { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); return t; };
const fresh = () => ({ view: 'describe', text: '', brief: null, id: null, busy: false, agents: {}, sel: {}, error: '', name: '',
  build: null, project: null, stages: {}, done: null, started: 0, waitedSince: 0, notice: '', req: 0, early: {} });
const stopTimers = () => { for (const t of timers) clearTimeout(t); timers.clear(); };

// ------------------------------------------------------------------ public
export function init(s) {
  studio = s;
  st = fresh();
  const css = new URL('./newrobot.css', import.meta.url).href;
  const inStyle = getComputedStyle(document.documentElement).getPropertyValue('--nr-css').trim();  // appended to style.css
  if (!inStyle && !document.querySelector(`link[href="${css}"]`)) {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = css;
    document.head.appendChild(l);
  }
  const btn = (cls) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.innerHTML = `${I('spark', 'sm')}New robot`;
    b.title = 'Describe a job; Morph designs a robot for it';
    b.onclick = (e) => { e.stopPropagation(); open(); };
    return b;
  };
  if (studio.mount?.header) studio.mount.header.appendChild(btn('btn nr-headbtn'));
  if (studio.mount?.projMenu) studio.mount.projMenu.appendChild(btn('nr-menubtn'));
  const on = (t, fn) => { try { studio.on?.(t, fn); } catch (e) { /* older shells: no events for this type */ } };
  on('brief', onBrief);
  on('agent', onAgent);
  on('project_created', onProjectCreated);
  on('start', onStart);
  on('node', onNode);
  on('done', onDone);
  const qs = new URLSearchParams(location.search);
  if (qs.has('new')) {
    open(qs.get('new') || '');
    try {  // once read, ?new= leaves the address: a reload must not start a second brief
      const u = new URL(location.href);
      u.searchParams.delete('new');
      history.replaceState(history.state, '', u.pathname + u.search + u.hash);
    } catch (e) { /* file:// pages */ }
  }
  return { open };
}

export function open(text = '') {
  if (!studio) return;
  if (st && st.id && st.view !== 'done' && st.view !== 'describe' && !text && root?.isConnected) return;  // already in progress
  st = fresh();
  st.text = text || '';
  mount();
  render();
  if (text && text.trim()) start(text.trim());
}

// ------------------------------------------------------------------ dialog
function mount() {
  root = document.createElement('div');
  root.className = 'nr';
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-labelledby', 'nr-title');
  if (studio.modal?.open) {
    studio.modal.open(root, { wide: true });
  } else {  // no shared dialog layer: a modal of our own (keys.js styles)
    const m = document.createElement('div');
    m.className = 'modal on';
    m.addEventListener('click', (e) => { if (e.target === m) close(); });
    m.appendChild(root);
    document.body.appendChild(m);
  }
  const dlg = root.parentElement?.closest('.dlg');
  (dlg || root).classList.add('nr-dlg', 'glass', 'dlg');
  if (dlg) root.classList.add('nr-inner');
  root.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !studio.modal?.open) close(); });
}

function close() {
  stopTimers();
  if (studio.modal?.close) studio.modal.close();
  else root?.closest('.modal')?.remove();
}

const alive = () => root && root.isConnected;

// ------------------------------------------------------------------ server
async function call(promise) {
  const r = await promise;
  if (r && typeof r.json === 'function') {
    const data = await r.json().catch(() => ({}));
    return { status: r.status, ok: r.ok && data.ok !== false, data };
  }
  return { status: r?.status || 200, ok: !!r && r.ok !== false, data: r || {} };
}

async function post(body) {
  st.error = '';
  try {
    const res = await call(studio.api.post('/api/brief', body));
    if (!res.ok) throw new Error(res.data?.error || `HTTP ${res.status}`);
    return res.data;
  } catch (e) {
    st.error = e.message || String(e);
    st.busy = false;
    if (alive()) render();
    return null;
  }
}

async function start(text) {
  st.text = text;
  st.busy = true;
  st.agents = {};
  st.view = 'thinking';
  const req = (st.req = ++reqSeq);
  render();
  const out = await post({ text });
  if (req !== st.req) return;
  if (!out) { st.view = 'describe'; render(); return; }
  accept(out, req);
}

async function send(body) {
  if (st.busy || !st.id) return;
  st.busy = true;
  const req = (st.req = ++reqSeq);
  render();
  const out = await post({ brief_id: st.id, ...body });
  if (out && req === st.req) accept(out, req);
}

function accept(out, req) {  // a POST reply: 202 while the turn runs (events follow), 200 when the server ran it inline
  st.id = out.brief_id || st.id;
  const held = st.early[st.id] || [];
  st.early = {};
  for (const ev of held) (ev.type === 'brief' ? onBrief : onAgent)(ev);
  if (out.build) return;
  if (out.busy) { st.waitedSince = Date.now(); later(() => poll(req), 2500); return; }
  if (out.brief) update(out.brief);
}

async function poll(req) {  // the brief event is the fast path; GET covers a dropped event stream
  if (!alive() || !st.busy || !st.id || req !== st.req) return;
  try {
    const res = await call(studio.api.get('/api/brief', { id: st.id }));
    if (req !== st.req) return;
    if (res.ok && res.data?.brief && !res.data.busy) { update(res.data.brief); return; }
  } catch (e) { /* keep waiting for the event */ }
  if (Date.now() - st.waitedSince < 90000) later(() => poll(req), 1500);
  else { st.busy = false; st.error = 'No answer from the Studio server. Try again.'; render(); }
}

function update(b) {
  if (!b || (st.id && b.id && b.id !== st.id)) return;
  if (st.brief && st.brief.id === b.id && (b.updated || 0) < (st.brief.updated || 0)) return;  // an older copy, late
  st.brief = b;
  st.id = b.id;
  st.busy = !!b.busy;
  if (!st.name || !NAME_RE.test(st.name)) st.name = b.name || '';
  const view = { asking: 'clarify', ready: 'confirm', out_of_scope: 'oos', refused: 'refused', building: 'building', built: 'done' }[b.status];
  if (view && st.view !== 'building' && st.view !== 'done') {
    if (view !== st.view) st.sel = {};
    st.view = view;
  }
  if (alive()) render();
}

// ------------------------------------------------------------------ events
function hold(id, ev) {  // a first turn is in flight: its events can beat the reply that names the brief, so keep them
  if (st.id || st.view !== 'thinking' || !id) return false;
  const q = (st.early[id] ||= []);
  if (q.length < 40) q.push(ev);
  return true;
}

function onBrief(ev) {
  if (!st || !ev || hold(ev.id, ev) || ev.id !== st.id) return;
  if (ev.status === 'cancelled') return;
  update(ev);
}

function onAgent(ev) {
  if (!st || !ev || (ev.brief && hold(ev.brief, ev))) return;
  if (ev.brief && ev.brief !== st.id) return;
  if (!ev.brief && !(st.project && (ev.project === st.project || !ev.project))) return;
  st.agents[ev.role] = ev;
  if (alive()) renderTimeline();
}

function ours(ev) { return st && st.project && (!ev.project || ev.project === st.project) && st.view === 'building'; }

function onProjectCreated(ev) {
  if (!st || st.view !== 'building' || !ev?.project) return;
  if (!st.project || ev.project === st.project) st.project = ev.project;
}

function onStart(ev) {
  if (!ours(ev)) return;
  st.agents.pipeline = { role: 'pipeline', provider: 'morph', status: 'start', text: `Generating ${ev.rev || 'r001'}: nine stages from scratch` };
  for (const [k] of STAGES) st.stages[k] = 'queued';
  if (alive()) renderTimeline();
}

function onNode(ev) {
  if (!ours(ev)) return;
  st.stages[ev.node] = ev.status;
  const n = STAGES.filter(([k]) => ['computed', 'reused'].includes(st.stages[k])).length;
  st.agents.pipeline = { role: 'pipeline', provider: 'morph', status: 'start', text: `${n} of ${STAGES.length} stages done` };
  if (alive()) renderTimeline();
}

function onDone(ev) {
  if (!ours(ev)) return;
  st.done = ev.current || {};
  // the tab may have joined the new project's room after its first stages ran: the revision says how each one ended
  for (const [k] of STAGES) if (st.done.stage_status?.[k]) st.stages[k] = st.done.stage_status[k];
  const s = st.done.summary || {};
  const errs = STAGES.filter(([k]) => st.stages[k] === 'error').length;
  st.agents.pipeline = { role: 'pipeline', provider: 'morph', status: errs ? 'error' : 'done', ms: ev.seconds ? Math.round(ev.seconds * 1000) : null,
    text: s.feasible ? 'All nine stages built; every check passed' : `${(s.issues || []).length} issue(s) to look at` };
  st.view = 'done';
  if (alive()) render();
}

// ------------------------------------------------------------------ rendering
function render() {
  if (!root) return;
  const v = st.view;
  const head = {
    describe: ['New robot', 'Describe the job in a sentence. Morph asks a few questions, then designs the whole arm: CAD, electronics, simulation, parts list and assembly manual.'],
    thinking: ['New robot', 'Reading your request'],
    clarify: ['A few questions', 'Pick an answer, or use sensible defaults and change anything later.'],
    confirm: ['Check the brief', 'This is what Morph will build. Every assumption can be changed now or after the build.'],
    building: [`Building ${st.project || st.name || 'your robot'}`, 'The whole pipeline runs from scratch: about 20 to 50 seconds.'],
    done: ['Your robot is ready', st.done?.summary?.feasible === false ? 'Built, with issues to look at below.' : 'Built and checked. Open it to explore every stage.'],
    oos: ["Morph can't build that as described", 'Here is why, and the nearest robot it can build.'],
    refused: ["Morph doesn't design that", ''],
  }[v] || ['New robot', ''];
  root.innerHTML = `
    <div class="nr-h">
      <div class="kicon nr-kicon">${I('bot')}</div>
      <div class="grow"><div class="t" id="nr-title">${esc(head[0])}</div>${head[1] ? `<div class="s">${esc(head[1])}</div>` : ''}</div>
      ${v === 'clarify' && st.brief?.round ? `<span class="tag neutral nr-round">Round ${st.brief.round} of 3</span>` : ''}
      <button class="btn icon ghost" type="button" data-a="close" title="Close (Esc)" aria-label="Close">${I('x', 'sm')}</button>
    </div>
    <div class="nr-live" aria-live="polite">${esc(liveText())}</div>
    ${st.error ? `<div class="kchk bad nr-err">${I('alert', 'sm')}<span>${esc(st.error)}</span></div>` : ''}
    <div class="nr-b">${body(v)}</div>
    <div class="dlg-f nr-f">${footer(v)}</div>`;
  bind();
  focusFirst();
}

function liveText() {
  const b = st.brief;
  if (st.busy) return 'Working on it';
  if (!b) return '';
  return { clarify: `${(b.questions || []).length} questions`, confirm: 'Ready to build', oos: 'Out of scope', refused: 'Refused',
    done: 'Built' }[st.view] || '';
}

function body(v) {
  if (v === 'describe') return describeView();
  if (v === 'thinking') {
    return `${requestBubble()}<div class="nr-thinking"><p class="nr-small">${I('spark', 'sm')}Models only propose; Morph's own code checks the
      request against what it can build and decides what to ask.</p>${timeline(['triage', 'designer', 'feasibility'])}</div>`;
  }
  if (v === 'clarify') return clarifyView();
  if (v === 'confirm') return confirmView();
  if (v === 'building') return buildingView();
  if (v === 'done') return doneView();
  if (v === 'oos') return oosView();
  if (v === 'refused') return refusedView();
  return '';
}

function footer(v) {
  const busy = st.busy ? ' disabled' : '';
  if (v === 'describe') {
    return `<span class="nr-hint">${I('alert', 'sm')}${esc(dataNote())}</span>
      <span class="grow"></span><button class="btn" type="button" data-a="close">Cancel</button>
      <button class="btn primary" type="button" data-a="start"${st.text.trim() ? '' : ' disabled'}>${I('arrow', 'sm')}Continue</button>`;
  }
  if (v === 'thinking') return '<span class="grow"></span><button class="btn" type="button" data-a="close">Cancel</button>';
  if (v === 'clarify') {
    const n = Object.keys(st.sel).length;
    return `<button class="btn ghost" type="button" data-a="defaults"${busy} title="Fill every open question with the default chip">Use sensible defaults</button>
      <button class="btn" type="button" data-a="buildnow"${busy} title="Defaults for the rest, then build">${I('bolt', 'sm')}Build now</button>
      <span class="grow"></span>${st.busy ? '<span class="spin" aria-hidden="true"></span>' : ''}
      <button class="btn primary" type="button" data-a="answer"${n && !st.busy ? '' : ' disabled'}>${I('arrow', 'sm')}Continue</button>`;
  }
  if (v === 'confirm') {
    const ok = !st.busy && NAME_RE.test(st.name || '');
    const issues = (st.brief?.preflight?.issues || []).length;
    return `<button class="btn ghost" type="button" data-a="restart">Start over</button><span class="grow"></span>
      ${st.busy ? '<span class="spin" aria-hidden="true"></span>' : ''}
      <button class="btn primary" type="button" data-a="build"${ok ? '' : ' disabled'}>${I('hammer', 'sm')}${issues ? 'Build anyway' : 'Build robot'}</button>`;
  }
  if (v === 'building') return '<span class="nr-hint">You can close this: the build keeps going and the pipeline strip shows it.</span><span class="grow"></span><button class="btn" type="button" data-a="close">Close</button>';
  if (v === 'done') {
    return `<span class="grow"></span><button class="btn" type="button" data-a="close">Close</button>
      <button class="btn primary" type="button" data-a="openproj">${I('eye', 'sm')}Open the robot</button>`;
  }
  if (v === 'oos') {
    return `<button class="btn" type="button" data-a="rephrase">Rephrase</button><span class="grow"></span>
      ${st.busy ? '<span class="spin" aria-hidden="true"></span>' : ''}
      <button class="btn primary" type="button" data-a="nearest"${st.brief?.scope?.nearest && !st.busy ? '' : ' disabled'}>${I('wrench', 'sm')}Build the nearest robot</button>`;
  }
  if (v === 'refused') return '<span class="grow"></span><button class="btn" type="button" data-a="close">Close</button><button class="btn primary" type="button" data-a="rephrase">Rephrase</button>';
  return '';
}

function describeView() {
  return `<label class="nr-label" for="nr-text">What should the robot do?</label>
    <div class="nr-input"><textarea id="nr-text" rows="3" maxlength="2000" placeholder="e.g. pick strawberries from a tray into punnets without bruising them"
      spellcheck="true">${esc(st.text)}</textarea></div>
    <div class="nr-examples" role="list" aria-label="Examples">${EXAMPLES.map((x) => `<button type="button" role="listitem" class="nr-ex" data-ex="${esc(x)}">${I('spark', 'sm')}${esc(x)}</button>`).join('')}</div>
    <div class="nr-limits">
      <div><b>What Morph builds</b><span>One stationary tabletop arm (4 to 6 joints) with a two-finger gripper that picks an object from the table and places it on the table.</span></div>
      <div><b>Objects</b><span>About 2.6 to 7.5 cm wide where the fingers close; up to 200 to 500 g depending on the servos.</span></div>
      <div><b>What you get</b><span>CAD, simulations, electronics and PCB, joint-level firmware, manufacturing plan, parts list and manual. No task program or vision code.</span></div>
    </div>`;
}

function dataNote() {
  const it = studio.S?.integrations || {};
  const who = [it.claude?.chat && 'Claude', (it.subq?.decide || it.subq?.enabled) && 'SubQ Snap'].filter(Boolean);
  return who.length ? `Your description goes to ${who.join(' and ')} with your own keys; Morph's code decides what to build.`
    : "Runs on this computer with Morph's built-in rules. Claude or SubQ keys (Keys) add free-form understanding.";
}

function doneAssumptions() {
  const all = (st.brief?.assumptions || []).filter((a) => a.text);
  const now = all.filter((a) => (a.alternatives || []).length);
  const rest = all.filter((a) => !(a.alternatives || []).length);
  return `${now.length ? `<div class="sec-t">Change now<span class="r">each choice regenerates the robot</span></div>
    ${assumptionList({ assumptions: now }, 'done', st.brief?.assumptions || [])}` : ''}
    ${rest.length ? `<details class="nr-more"><summary>${rest.length} more assumption${rest.length > 1 ? 's' : ''}</summary>
      <ul class="nr-plain">${rest.map((a) => `<li>${esc(a.text)}</li>`).join('')}</ul></details>` : ''}`;
}

function requestBubble() {
  const text = st.brief?.request || st.text;
  return text ? `<div class="nr-msg user"><div class="b">${esc(text)}</div></div>` : '';
}

function scopeNote() {
  const sc = st.brief?.scope || {};
  if (sc.status !== 'adjusted' || !(sc.notes || []).length) return '';
  return `<div class="nr-note warn">${I('alert', 'sm')}<div><b>Adjusted to what Morph can build</b>${sc.notes.map((n) => `<p>${esc(n)}</p>`).join('')}</div></div>`;
}

function chipGroup(q) {
  const id = `nr-q-${esc(q.slot)}`;
  return `<fieldset class="nr-q" data-slot="${esc(q.slot)}">
      <legend id="${id}">${esc(q.text)}${q.required ? '' : '<span class="nr-opt">optional</span>'}</legend>
      ${q.why ? `<p class="nr-why">${esc(q.why)}</p>` : ''}
      <div class="nr-chips" role="radiogroup" aria-labelledby="${id}">
        ${q.chips.map((c, i) => {
          const on = st.sel[q.slot] !== undefined && JSON.stringify(st.sel[q.slot]) === JSON.stringify(c.value);
          return `<button type="button" role="radio" aria-checked="${on}" tabindex="${on || (st.sel[q.slot] === undefined && i === 0) ? 0 : -1}"
            class="nr-chip${on ? ' on' : ''}" data-slot="${esc(q.slot)}" data-i="${i}">${on ? I('check', 'sm') : ''}<span>${esc(c.label)}</span>${c.default ? '<em>default</em>' : ''}${c.note ? `<small>${esc(c.note)}</small>` : ''}</button>`;
        }).join('')}
      </div></fieldset>`;
}

function clarifyView() {
  const b = st.brief || {};
  const qs = b.questions || [];
  const intro = b.round > 1 ? 'One more thing so the robot fits the job:' : `${qs.length === 1 ? 'One question' : `${qs.length} quick questions`} so I size it right:`;
  return `<div class="nr-two">
      <section class="nr-conv" aria-label="Questions">
        ${requestBubble()}${scopeNote()}
        <div class="nr-msg bot"><span class="av">${I('spark', 'sm')}</span><div class="b">${esc(intro)}</div></div>
        ${qs.map(chipGroup).join('')}
        <form class="nr-free" data-a="free" autocomplete="off">
          <input type="text" maxlength="2000" placeholder="Or type an answer: 6 cm, 200 g, 10 per minute, people nearby..." aria-label="Type an answer">
          <button class="btn" type="submit"${st.busy ? ' disabled' : ''}>Send</button>
        </form>
      </section>
      ${briefCard()}
    </div>`;
}

function briefCard() {
  const b = st.brief || {};
  const picked = {};
  for (const q of b.questions || []) {
    const c = q.chips.find((x) => st.sel[q.slot] !== undefined && JSON.stringify(x.value) === JSON.stringify(st.sel[q.slot]));
    if (c) picked[q.slot] = c.label;
  }
  const rows = (b.card || []).filter((r) => r.slot !== 'connectivity' || r.source !== 'default')
    .map((r) => (picked[r.slot] ? { ...r, text: picked[r.slot], badge: 'picked', source: 'user', pending: true } : r));
  return `<aside class="nr-card" aria-label="Brief so far">
      <div class="sec-t">Brief<span class="r">${esc(b.archetype?.key ? labelOf(b) : '')}</span></div>
      <dl>${rows.map((r) => `<div class="nr-row${r.pending ? ' pending' : ''}"><dt>${esc(r.label)}</dt><dd><span>${esc(r.text)}</span>
        <span class="tag ${BADGE[r.source] || 'neutral'}">${esc(r.badge)}</span></dd></div>`).join('')}</dl>
      ${agentsMini()}
    </aside>`;
}

function labelOf(b) {
  const k = b.archetype?.key || '';
  return k.replace(/_/g, ' ').replace(/^so101$/, 'your SO-101');
}

function agentsMini() {
  const rows = ['triage', 'designer', 'feasibility'].map((r) => st.agents[r]).filter(Boolean);
  if (!rows.length) return '';
  return `<div class="nr-agents-mini">${rows.map((e) => `<span title="${esc(e.text || '')}">${esc(e.role)} · ${esc(PROVIDER[e.provider] || e.provider)}</span>`).join('')}</div>`;
}

function facts(b) {
  const p = b.preflight || {};
  const ch = b.changes || {};
  const out = [];
  if (ch['arm.source'] === 'so101') out.push('Your SO-101');
  else if (p.dof) out.push(`${p.dof} joints`);
  if (ch['arm.actuator_family'] && ch['arm.source'] !== 'so101') out.push(FAMILY[ch['arm.actuator_family']] || ch['arm.actuator_family']);
  if (p.reach_mm) out.push(`${Math.round(p.reach_mm)} mm arm`);
  if (ch['object.mass_g'] != null) out.push(`${+ch['object.mass_g']} g object`);
  if (ch['task.cycle_time_s']) out.push(`one every ${+ch['task.cycle_time_s']} s`);
  if (ch['finger.sensor'] === 'fsr402' && ch['arm.source'] !== 'so101') out.push('force-sensing fingers');
  if ((ch['electronics.extras'] || '').includes('estop')) out.push('e-stop');
  if ((ch['electronics.extras'] || '').includes('wrist_camera')) out.push('wrist camera');
  if (ch['build.config'] && ch['build.config'] !== 'prototype') out.push(`${ch['build.config']} build of ${ch['build.quantity']}`);
  return out.map((f) => `<span class="chip">${esc(f)}</span>`).join('');
}

function assumptionList(b, mode, full) {
  const items = (b.assumptions || []).filter((a) => a.text);
  if (!items.length) return '<p class="muted">No assumptions: everything came from your answers.</p>';
  const all = full || st.brief?.assumptions || [];
  return `<ul class="nr-assume">${items.map((a) => {
    const i = all.indexOf(a);
    const [label, cls] = KIND[a.kind] || KIND.default;
    const opts = mode === 'confirm' ? (a.options || []) : (a.alternatives || []);
    return `<li><span class="tag ${cls}">${esc(label)}</span><div><p>${esc(a.text)}</p>${opts.length ? `<div class="nr-alts">${opts.map((o, j) => `<button type="button" class="nr-alt" data-ai="${i}" data-oi="${j}">${esc(o.label)}</button>`).join('')}</div>` : ''}</div></li>`;
  }).join('')}</ul>`;
}

function scopeBadge(sc) {
  if (sc.status === 'ok') return `<span class="tag good">${I('check', 'sm')}Inside what Morph builds</span>`;
  if (sc.status === 'adjusted') return `<span class="tag warn">${I('alert', 'sm')}Adjusted</span>`;
  return `<span class="tag bad">${I('x', 'sm')}Out of scope</span>`;
}

function confirmView() {
  const b = st.brief || {};
  const p = b.preflight || {};
  const issues = p.issues || [];
  return `<div class="nr-two">
      <section class="nr-conv">
        ${requestBubble()}
        <div class="nr-summary">${esc(b.summary || '')}</div>
        <div class="nr-facts">${facts(b)}</div>
        <div class="sec-t">Assumptions<span class="r">click an option to change it</span></div>
        ${assumptionList(b, 'confirm')}
      </section>
      <aside class="nr-card nr-confirm" aria-label="Build">
        <div class="nr-kv"><span>Scope</span>${scopeBadge(b.scope || {})}</div>
        <div class="nr-kv"><span>Preflight</span>${issues.length ? `<span class="tag bad">${issues.length} issue${issues.length > 1 ? 's' : ''}</span>`
          : `<span class="tag good">${I('check', 'sm')}passed</span>`}<small>${p.ms != null ? `${p.ms} ms, ${p.checks || 1} check${(p.checks || 1) > 1 ? 's' : ''}` : ''}</small></div>
        ${issues.length ? `<ul class="nr-issues">${issues.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        <div class="nr-kv"><span>Parts cost</span>${b.estimate ? `<b class="num">${esc(b.estimate.label)}</b>` : '<small class="muted">no estimate for this build type</small>'}</div>
        <label class="nr-label" for="nr-name">Project name</label>
        <input id="nr-name" class="nr-name${NAME_RE.test(st.name || '') ? '' : ' bad'}" value="${esc(st.name || '')}" maxlength="40" spellcheck="false"
          aria-describedby="nr-name-h"><div class="kh" id="nr-name-h">Lower-case letters, digits and dashes.</div>
        <p class="nr-small">${I('clock', 'sm')}About 20 to 50 s: sizing, gripper, electronics, CAD, simulation, PCB and firmware, manufacturing, BOM and manual.</p>
      </aside>
    </div>`;
}

function timeline(roles) {
  return `<ol class="nr-timeline">${roles.map((r) => {
    const e = st.agents[r];
    const label = (ROLES.find((x) => x[0] === r) || [r, r])[1];
    const status = e ? e.status : 'pending';
    const icon = status === 'start' ? '<span class="spin" aria-hidden="true"></span>' : status === 'done' ? I('check', 'sm')
      : status === 'error' ? I('alert', 'sm') : status === 'skipped' ? '<i class="nr-dash"></i>' : '<i class="nr-dot"></i>';
    const stages = r === 'pipeline' && Object.keys(st.stages).length ? `<div class="nr-stages">${STAGES.map(([k, l]) => `<span class="nr-st ${esc(st.stages[k] || 'queued')}" title="${esc(l)}: ${esc(st.stages[k] || 'queued')}">${esc(l)}</span>`).join('')}</div>` : '';
    return `<li class="nr-ag ${esc(status)}" data-role="${esc(r)}"><span class="nr-ic">${icon}</span>
      <div class="nr-ag-b"><div><b>${esc(label)}</b>${e ? `<span class="nr-prov">${esc(PROVIDER[e.provider] || e.provider || '')}</span>` : ''}
      ${e && e.ms != null && status !== 'start' ? `<span class="nr-ms num">${e.ms < 1000 ? `${e.ms} ms` : `${(e.ms / 1000).toFixed(1)} s`}</span>` : ''}</div>
      <p>${esc(e ? e.text : status === 'pending' ? 'waiting' : '')}</p>${stages}</div></li>`;
  }).join('')}</ol>`;
}

function renderTimeline() {
  const t = root?.querySelector('.nr-timeline');
  if (!t) { if (st.view === 'clarify' || st.view === 'confirm') { const m = root.querySelector('.nr-agents-mini'); if (m) m.outerHTML = agentsMini(); } return; }
  const roles = [...t.querySelectorAll('[data-role]')].map((x) => x.dataset.role);
  t.outerHTML = timeline(roles);
  const el = root.querySelector('.nr-elapsed');
  if (el && st.started) el.textContent = `${Math.round((Date.now() - st.started) / 1000)} s`;
  const h = root.querySelector('.nr-build-h');
  if (h) h.innerHTML = `${progress()}<span class="nr-elapsed num">${st.started ? Math.round((Date.now() - st.started) / 1000) : 0} s</span>`;
}

function buildingView() {
  return `<div class="nr-build-h">${progress()}<span class="nr-elapsed num">0 s</span></div>
    ${timeline(['triage', 'designer', 'feasibility', 'pipeline', 'sourcing', 'memory'])}`;
}

function progress() {
  const n = STAGES.filter(([k]) => ['computed', 'reused'].includes(st.stages[k])).length;
  return `<div class="nr-prog" role="progressbar" aria-valuemin="0" aria-valuemax="${STAGES.length}" aria-valuenow="${n}"
    aria-label="Stages built"><i style="width:${Math.round((100 * n) / STAGES.length)}%"></i></div><span class="nr-prog-t num">${n} of ${STAGES.length} stages</span>`;
}

function doneView() {
  const s = st.done?.summary || {};
  const m = (k, v) => `<div class="nr-metric"><span>${esc(k)}</span><b class="num">${v}</b></div>`;
  const money = (v) => (v == null ? '–' : `$${Math.round(v).toLocaleString('en-US')}`);
  return `<div class="nr-done-h">${s.feasible === false ? `<span class="tag warn">${I('alert', 'sm')}${(s.issues || []).length} issue(s)</span>`
      : `<span class="tag good">${I('check', 'sm')}Feasible</span>`}<span class="muted">${esc(st.project || st.name)} · ${esc(st.done?.rev || 'r001')}</span></div>
    <div class="nr-metrics">${m('Parts cost (est.)', money(s.unit_cost))}${m('Pick and place', s.pick_place_success == null ? '–' : s.pick_place_success ? 'succeeds' : 'fails')}
      ${m('Cycle time', s.cycle_time_s ? `${(+s.cycle_time_s).toFixed(1)} s` : '–')}${m('Payload at full reach', s.max_payload_g ? `${Math.round(s.max_payload_g)} g` : '–')}</div>
    ${(s.issues || []).length ? `<ul class="nr-issues">${s.issues.slice(0, 4).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${doneAssumptions()}
    ${timeline(['pipeline', 'sourcing', 'memory'])}`;
}

function oosView() {
  const sc = st.brief?.scope || {};
  return `${requestBubble()}
    <div class="nr-note bad">${I('alert', 'sm')}<div><b>Why not</b>${(sc.notes || []).map((n) => `<p>${esc(n)}</p>`).join('')}</div></div>
    ${sc.nearest ? `<div class="nr-nearest"><div class="sec-t">Nearest robot Morph can build</div><p>${esc(sc.nearest.summary)}</p>
      <small class="muted">You can change the object, the layout and the rest on the next screen.</small></div>` : ''}`;
}

function refusedView() {
  const n = (st.brief?.scope?.notes || [])[0] || '';
  const by = st.brief?.flags?.harm_by || 'rules';  // the local filter refuses before any request leaves this computer
  const what = by === 'rules' ? 'Nothing was designed or sent anywhere.' : `Nothing was designed, and nothing more was sent to ${PROVIDER[by] || by}.`;
  return `${requestBubble()}<div class="nr-note bad">${I('alert', 'sm')}<div><b>${esc(what)}</b><p>${esc(n)}</p>
    <p>Describe a handling, sorting, lab, kitchen or hobby task instead.</p></div></div>`;
}

// ------------------------------------------------------------------ interaction
function bind() {
  root.querySelectorAll('[data-a]').forEach((el) => {
    const a = el.dataset.a;
    if (el.tagName === 'FORM') { el.onsubmit = (e) => { e.preventDefault(); act(a, el); }; return; }
    el.onclick = () => act(a, el);
  });
  const ta = root.querySelector('#nr-text');
  if (ta) {
    ta.oninput = () => { st.text = ta.value; const b = root.querySelector('[data-a="start"]'); if (b) b.disabled = !ta.value.trim(); };
    ta.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey && ta.value.trim()) { e.preventDefault(); act('start'); } };
  }
  root.querySelectorAll('.nr-ex').forEach((b) => (b.onclick = () => { st.text = b.dataset.ex; act('start'); }));
  root.querySelectorAll('.nr-chip').forEach((b) => {
    b.onclick = () => pick(b.dataset.slot, +b.dataset.i);
    b.onkeydown = (e) => chipKeys(e, b);
  });
  root.querySelectorAll('.nr-alt').forEach((b) => (b.onclick = () => alt(+b.dataset.ai, +b.dataset.oi)));
  const nm = root.querySelector('#nr-name');
  if (nm) {
    nm.oninput = () => {
      st.name = nm.value.trim().toLowerCase();
      nm.classList.toggle('bad', !NAME_RE.test(st.name));
      const b = root.querySelector('[data-a="build"]');
      if (b) b.disabled = st.busy || !NAME_RE.test(st.name);
    };
  }
}

function focusFirst() {
  const el = root.querySelector('#nr-text') || root.querySelector('.nr-chip[tabindex="0"]')
    || root.querySelector('.nr-f .btn.primary:not([disabled])') || root.querySelector('.nr-f .btn');
  if (el && !root.contains(document.activeElement)) el.focus({ preventScroll: true });
}

function pick(slot, i) {
  const q = (st.brief?.questions || []).find((x) => x.slot === slot);
  if (!q || st.busy) return;
  st.sel[slot] = q.chips[i].value;
  const qs = st.brief.questions;
  render();
  // the last open question answered: send at once (the Continue button stays for partial answers)
  if (qs.every((x) => st.sel[x.slot] !== undefined)) later(() => act('answer'), 180);
  else root.querySelector(`.nr-chip[data-slot="${CSS.escape(slot)}"][data-i="${i}"]`)?.focus();
}

function chipKeys(e, b) {
  const g = [...b.parentElement.querySelectorAll('.nr-chip')];
  const i = g.indexOf(b);
  const k = e.key;
  if (k === 'ArrowRight' || k === 'ArrowDown') { e.preventDefault(); g[(i + 1) % g.length].focus(); }
  else if (k === 'ArrowLeft' || k === 'ArrowUp') { e.preventDefault(); g[(i - 1 + g.length) % g.length].focus(); }
  else if (k === ' ' || k === 'Enter') { e.preventDefault(); pick(b.dataset.slot, +b.dataset.i); }
}

async function alt(ai, oi) {
  const a = (st.brief?.assumptions || [])[ai];
  if (!a) return;
  if (st.view === 'confirm') {
    const o = (a.options || [])[oi];
    if (o) await send({ answers: { [a.slot]: o.value } });
    return;
  }
  const o = (a.alternatives || [])[oi];
  if (!o) return;
  try {
    const res = await call(studio.api.post('/api/params', { changes: o.changes }));
    if (!res.ok) throw new Error(res.data?.error || `HTTP ${res.status}`);
    studio.toast?.(`Regenerating: ${o.label}`);
    close();
  } catch (e) { st.error = `Couldn't apply that: ${e.message || e}`; render(); }
}

async function act(a, el) {
  if (a === 'close') { close(); return; }
  if (a === 'start') { const t = (root.querySelector('#nr-text')?.value ?? st.text).trim(); if (t) start(t); return; }
  if (a === 'answer') {
    const answers = { ...st.sel };
    if (!Object.keys(answers).length) return;
    await send({ answers });
    return;
  }
  if (a === 'free') {
    const inp = el.querySelector('input');
    const t = inp.value.trim();
    if (!t) return;
    const answers = { ...st.sel };
    await send({ text: t, ...(Object.keys(answers).length ? { answers } : {}) });
    return;
  }
  if (a === 'defaults' || a === 'nearest') { await send({ action: 'defaults' }); return; }
  if (a === 'buildnow' || a === 'build') { await build(); return; }
  if (a === 'restart' || a === 'rephrase') {
    stopTimers();
    const id = st.id;  // fire and forget: a failed cancel only leaves a draft that expires in 7 days
    if (id) Promise.resolve().then(() => call(studio.api.post('/api/brief', { brief_id: id, action: 'cancel' }))).catch(() => {});
    const t = st.brief?.request || st.text;
    st = fresh();
    st.text = t;
    render();
    return;
  }
  if (a === 'openproj') {
    const p = st.project || st.name;
    close();
    if (p && studio.openProject) studio.openProject(p);
  }
}

async function build() {
  if (st.busy || !st.id) return;
  st.busy = true;
  const req = (st.req = ++reqSeq);
  render();
  const body = { brief_id: st.id, action: 'build' };
  if (NAME_RE.test(st.name || '')) body.name = st.name;
  const out = await post(body);
  if (req !== st.req) return;
  st.busy = false;
  if (!out) return;
  st.build = out.build;
  st.name = out.build?.name || st.name;
  if (out.build?.brief) st.brief = out.build.brief;
  st.project = out.project || null;
  st.view = 'building';
  st.started = Date.now();
  for (const [k] of STAGES) st.stages[k] = st.stages[k] || 'queued';
  st.agents.pipeline = { role: 'pipeline', provider: 'morph', status: 'start', text: st.project ? 'Creating the project' : 'Waiting for the Studio to create the project' };
  render();
  const tick = () => { if (!alive() || st.view !== 'building') return; const el = root.querySelector('.nr-elapsed'); if (el) el.textContent = `${Math.round((Date.now() - st.started) / 1000)} s`; later(tick, 1000); };
  later(tick, 1000);
  if (st.project && studio.openProject) {
    try { await studio.openProject(st.project); } catch (e) { /* the tab stays where it is; events for the new room still arrive */ }
    // a build that ended before this tab joined the new project's room sent its done event elsewhere: the state has it
    const S = studio.S || {};
    if (req === st.req && S.project === st.project && S.current && !S.busy) { onDone({ project: st.project, current: S.current }); return; }
    // stages that ran before this tab joined the room (sizing is quick): the room's state says how they went
    try {
      const live = (await call(studio.api.get('/api/state'))).data?.building;
      if (req !== st.req || st.view !== 'building' || !live?.nodes) return;
      for (const [k] of STAGES) if ((st.stages[k] || 'queued') === 'queued' && live.nodes[k]) st.stages[k] = live.nodes[k];
      const n = STAGES.filter(([k]) => ['computed', 'reused'].includes(st.stages[k])).length;
      if (n && st.agents.pipeline?.status === 'start') st.agents.pipeline = { ...st.agents.pipeline, text: `${n} of ${STAGES.length} stages done` };
      if (alive()) renderTimeline();
    } catch (e) { /* the node events still come */ }
  }
}

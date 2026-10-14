// Morph Studio: agent tools (beta). A feature module (RELEASE_CONTRACTS sections 4 and 15): every agent run of this tab's
// project gets one card in the chat, with a line per tool it used (and each key added or call skipped) and what it waits
// for: Key needed (Add key opens the Keys dialog on that key, Skip, Replace key after the service refused it, "Key
// added, continuing…" once saved), Tool request (Allow once, Always, Skip) and the agent's own requests for a tool Morph
// doesn't have. Cards are rebuilt from GET /api/runs (each run's `steps`) when the page loads and whenever the chat is
// redrawn, because run events aren't kept in the chat history. "Live prices (beta)" in the BOM tab starts the price
// check; its result shows on the card and from that button. Teams: only the owner adds keys and allows tools; everyone
// else is told to ask the owner. A need no tool covers shows Darwin's suggestions and, for the owner, "Draft a tool"
// (Morph writes a tool, preview: tooldraft.js, started from here).
import * as drafts from './tooldraft.js';

const KEY = '<circle cx="7.5" cy="15.5" r="4"/><path d="M10.5 12.5L20 3M16 7l3 3M13.5 9.5l2 2"/>';
const TAGS = '<path d="M3 12.5V3h9.5L21 11.5 11.5 21z"/><circle cx="7.5" cy="7.5" r="1.4"/>';
const SIDE = {
  read: 'Read-only: it only looks things up.', write: 'It changes something at the provider.',
  upload: 'It sends files or data to the provider.', spend: 'It spends money.',
};
const host = (e) => e.host || 'the service';
const FAIL = {  // a failed call's line, from its error code and HTTP status (never from what the service said)
  network: (e) => `couldn't reach ${host(e)} (offline or blocked)`, timeout: (e) => `${host(e)} didn't answer in time`,
  http_error: (e) => `${host(e)} answered ${e.status || 'with an error'}`, rate_limited: (e) => `${host(e)}: rate limit reached, try again later`,
  not_json: (e) => `${host(e)}'s answer wasn't JSON`, invalid_json: (e) => `${host(e)}'s answer wasn't valid JSON`,
  response_too_large: (e) => `${host(e)}'s answer was too large`, blocked_destination: () => 'blocked: not a public address',
  redirect_not_followed: (e) => `${host(e)} answered with a redirect, which Morph never follows`,
  invalid_arguments: () => "the request didn't fit the tool", tool_failed: () => 'failed inside Morph',
  other: (e) => `failed${e.status ? ` (${e.status})` : ''}`,
};
const ENDED = ['done', 'failed', 'refused', 'cancelled'];
// an ended run never runs again, so a view that says otherwise left the server before it ended (a slow Allow, a list
// in flight when its last event came): it must not take the card back to Working
const stale = (cur, next) => ENDED.includes(cur?.status) && !ENDED.includes(next?.status);
const STOPPED = {  // why an interrupted run waits (its card's heading); the note under it says what to do
  restart: 'The Studio restarted while this ran', claude_key: 'Claude refused the Anthropic key', claude_unavailable: 'Claude was unavailable',
  offline: 'Network lookups were turned off', tools_off: 'Agent tools were turned off',
};
const FIX = {  // what only the owner can fix before a run continues: Keys opens on that setting or key; others ask the owner
  offline: ['network_enabled', 'Ask the owner to turn network lookups back on (Keys), then continue it.'],
  tools_off: ['agent_tools_enabled', 'Ask the owner to turn agent tools back on (Keys), then continue it.'],
  claude_key: ['ANTHROPIC_API_KEY', 'Ask the owner to fix the Anthropic key in Keys, then continue it.'],
};
const RECENT_MS = 30 * 60 * 1000;  // finished runs shown again after a reload: the last half hour's

let studio = null;
const runs = new Map();  // run id -> {view, el, keyAdded, answer}; view.steps grows with the live events
let can = { start: false, skip: false, allow: false, keys: false, tools: false }, ready = {}, catalog = {};
let refreshing = null, again = false, bomBtn = null;

const esc = (s) => (studio?.esc ? studio.esc(s) : String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
const svg = (p, size = 'sm') => `<svg class="i ${size}" viewBox="0 0 24 24" aria-hidden="true">${p}</svg>`;
const I = (name, size = 'sm') => (name === 'key' ? svg(KEY, size) : name === 'tags' ? svg(TAGS, size) : studio?.I ? studio.I(name, size) : '');
const ms = (v) => (v == null ? '' : v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(1)} s`);
const hhmm = (ts) => { const t = Date.parse(ts || ''); return t ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''; };
const amount = (v) => v.toFixed(Math.max(2, Math.min(5, (String(v).split('.')[1] || '').length)));  // 13.9 -> 13.90, 0.078 kept
const price = (v, cur) => (typeof v !== 'number' ? '' : cur === 'USD' ? `$${amount(v)}` : `${amount(v)}${cur ? ` ${cur}` : ''}`);

// ------------------------------------------------------------------ public
export function init(s) {
  studio = s;
  const on = (t, fn) => { try { studio.on?.(t, fn); } catch (e) { /* older shells */ } };
  for (const t of ['run_started', 'credential_needed', 'tool_request', 'tool_used', 'run_paused', 'run_resumed', 'run_done', 'run_failed']) on(t, onEvent);
  on('project', (ev) => { if (ev.switched || ev.reset) { runs.clear(); refresh(); } });
  on('connection', () => refresh());  // the event stream (re)opened: a run's events may have come before it did
  watchChat();
  watchBom();
  const d = drafts.init(studio, {
    changed: () => { for (const r of runs.values()) if ((r.view.requests || []).length) paint(r); },  // need cards: suggestions, drafts
    installed: () => { catalog = {}; refresh(); },  // a new tool's title and "Not yet tried" come from the catalog
    startAgent: (agent) => (agent === 'price_check' ? startPrices() : startAgent(agent)),
  });
  refresh();
  return { refresh, startPrices, drafts: d, state: () => ({ runs: [...runs.values()].map((r) => r.view), can, ready }) };
}

async function startAgent(agent) {
  try {
    const out = await studio.api.post('/api/runs/start', { agent, rev: studio.S?.current?.rev });
    studio.toast(out.already ? 'That agent is already running: see the chat' : 'Started: its card is in the chat');
    refresh();
  } catch (e) { studio.toast(e.body?.error || e.message || "Couldn't start it"); }
}

// ------------------------------------------------------------------ data
async function refresh() {  // one at a time; a call during one runs once more after it
  if (refreshing) { again = true; return refreshing; }
  refreshing = (async () => {
    try {
      do {
        again = false;
        const out = await studio.api.get('/api/runs', { limit: 12 });
        can = out.can || can; ready = out.ready || ready;
        if (!Object.keys(catalog).length) {
          const t = await studio.api.get('/api/tools').catch(() => null);
          catalog = Object.fromEntries((t?.tools || []).map((x) => [x.name, x]));
        }
        const now = Date.now();
        for (const v of [...(out.runs || [])].reverse()) {  // oldest first, so the newest card ends up lowest
          const r = runs.get(v.id);
          if (r) { if (!stale(r.view, v)) r.view = v; continue; }
          if (ENDED.includes(v.status) && now - Date.parse(v.updated || 0) > RECENT_MS) continue;
          runs.set(v.id, { view: v, el: null, keyAdded: false, answer: null });
        }
        for (const r of runs.values()) { if (r.view.status === 'done' && r.answer == null) loadAnswer(r); paint(r); }
        bomButton();
      } while (again);
    } catch (e) {
      console.warn('[tools] runs', e);
    } finally { refreshing = null; }
  })();
  return refreshing;
}

async function loadAnswer(r) {
  r.answer = '';
  const out = await studio.api.get('/api/runs/answer', { run: r.view.id }).catch(() => null);
  if (out?.text) { r.answer = out.text; paint(r); }
}

function onEvent(ev) {
  if (!ev.run) return;
  let r = runs.get(ev.run);
  if (!r) {
    r = { view: { id: ev.run, agent: ev.agent, project: ev.project, title: ev.title || 'Agent run', status: 'running', waiting_on: [], cards: [], calls: {}, requests: [], steps: [], context: {} }, el: null, keyAdded: false, answer: null };
    runs.set(ev.run, r);
  }
  const v = r.view, at = new Date().toISOString();
  v.steps = v.steps || [];
  v.calls = v.calls || {};
  if (ev.type === 'tool_used') { v.steps.push({ ...ev, at }); v.calls[ev.tool] = (v.calls[ev.tool] || 0) + 1; }
  else if (ev.type === 'run_resumed') {
    r.keyAdded = ev.trigger === 'key_saved';
    if (r.keyAdded) v.steps.push({ type: 'key_saved', keys: ev.keys || [], at });
    for (const x of ev.skipped || []) v.steps.push({ type: 'skipped', ...x, at });
    v.status = 'running'; v.waiting_on = []; v.cards = [];
  } else if (ev.type === 'tool_request' && ev.reason === 'capability') v.requests = [...(v.requests || []), ev];
  else if (ev.type === 'run_done' || ev.type === 'run_failed') { v.status = ev.status; v.note = ev.note || ''; r.keyAdded = false; }
  paint(r);
  bomButton();
  if (['run_started', 'run_paused', 'run_done', 'run_failed', 'credential_needed', 'tool_request'].includes(ev.type)) refresh();
  if (ev.type === 'run_started' && ev.agent === 'price_check') showBom();
}

async function act(r, what, arg) {
  const id = r.view.id;
  try {
    let out;
    if (what === 'stop') { out = await studio.api.post('/api/runs/cancel', { run: id }); r.stopping = !!out?.run?.cancelling; }
    else if (what === 'continue') out = await studio.api.post('/api/runs/resume', { run: id });
    else out = await studio.api.post('/api/runs/decide', { run: id, decisions: Object.fromEntries(String(arg || '').split(',').filter(Boolean).map((tu) => [tu, what])) });
    if (out?.run && !stale(r.view, out.run)) { r.view = out.run; paint(r); }
    refresh();
  } catch (e) {
    studio.toast(e.body?.error || e.message || "Couldn't reach the Studio");
    refresh();
  }
}

export async function startPrices() {
  const rev = studio.S?.current?.rev;
  if (!can.start) { studio.toast('Only editors and the owner can start Live prices.'); return; }
  try {
    const out = await studio.api.post('/api/runs/start', { agent: 'price_check', rev });
    const r = runs.get(out.run.id) || { view: out.run, el: null, keyAdded: false, answer: null };
    if (!stale(r.view, out.run)) r.view = out.run;
    runs.set(out.run.id, r);
    paint(r);  // a new card keeps the chat at its end
    studio.toast(out.already ? `Live prices for ${rev} are already running: see the chat` : `Live prices started for ${rev}`);
    if (out.already) r.el?.scrollIntoView({ block: 'end' });
  } catch (e) {
    const msg = e.body?.error || e.message || "Couldn't start Live prices";
    if (e.status === 503) {
      studio.toast(can.keys ? 'Live prices need Claude: add the Anthropic key in Keys.' : 'Live prices need the Anthropic key: ask the owner to add it in Keys.');
      if (can.keys) window.dispatchEvent(new CustomEvent('morph:keys', { detail: { key: 'ANTHROPIC_API_KEY' } }));
    } else studio.toast(msg);
  }
  bomButton();
}

// ------------------------------------------------------------------ the chat card
function watchChat() {  // the chat is redrawn (project switch, reconnect, reset): put the cards back after its history
  const box = document.querySelector('#messages');
  if (!box) return;
  let t = null;
  new MutationObserver(() => {
    const lost = [...runs.values()].some((r) => r.el && !r.el.isConnected);
    if (!lost) return;
    for (const r of runs.values()) r.el = r.el?.isConnected ? r.el : null;
    clearTimeout(t);
    t = setTimeout(() => { for (const r of runs.values()) paint(r); }, 60);
  }).observe(box, { childList: true });
}

function paint(r) {
  if (!studio || (r.view.project && studio.project?.() && r.view.project !== studio.project())) return;
  if (!r.el) {
    r.el = studio.addMsg ? studio.addMsg('assistant', '') : null;
    if (!r.el) return;
    r.el.classList.add('tl-run');
    r.el.dataset.run = r.view.id;
    r.el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-act]');
      if (!b || b.disabled) return;
      const a = b.dataset.act;
      if (a === 'key') window.dispatchEvent(new CustomEvent('morph:keys', { detail: { key: b.dataset.env } }));
      else if (a === 'prices') showPrices(r);
      else if (a === 'draft') {
        const q = (r.view.requests || []).find((x) => x.tool_use_id === b.dataset.tu) || {};
        window.dispatchEvent(new CustomEvent('morph:tool-draft', { detail: { need: q.need, why: q.why, run: r.view.id, tool_use_id: q.tool_use_id } }));
      }
      else { r.el.querySelectorAll('[data-act]').forEach((x) => { if (x.dataset.act !== 'key') x.disabled = true; }); act(r, a, b.dataset.tu); }
    });
  }
  const box = r.el.querySelector('.b'), chat = r.el.parentElement;
  if (!box) return;
  // keep the card's end in view when it is the newest message or the reader was at the end (the chat scrolls smoothly,
  // so a position read mid-scroll can't tell)
  const follow = !!chat && (!box.innerHTML || r.el === chat.lastElementChild || chat.scrollHeight - chat.scrollTop - chat.clientHeight < 90);
  box.innerHTML = cardHTML(r);
  if (follow) requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
}

function status(v, r) {
  const w = v.waiting_on || [];
  if (v.status === 'running') return ['neutral', '<span class="spin"></span>', r?.stopping ? 'Stopping' : 'Working'];  // Stop waits for the call under way
  if (v.status === 'paused') return ['warn', '', w.includes('credential') ? 'Needs a key' : w.includes('approval') ? (can.allow ? 'Needs your OK' : "Needs the owner's OK") : 'Paused'];
  return { done: ['good', '', 'Done'], failed: ['bad', '', 'Stopped'], refused: ['bad', '', 'Declined'], cancelled: ['neutral', '', 'Cancelled'] }[v.status] || ['neutral', '', v.status || ''];
}

function cardHTML(r) {
  const v = r.view, [cls, spin, text] = status(v, r), rev = v.context?.rev;
  if (ENDED.includes(v.status)) r.stopping = false;
  const who = v.context?.auto ? 'started after the build' : '';
  const steps = v.steps?.length ? v.steps : Object.entries(v.calls || {}).map(([tool, n]) => ({ type: 'calls', tool, n }));
  const lines = steps.map(lineHTML);
  const needs = v.status === 'paused' ? group(v.cards || []).map((c) => needHTML(r, c)).join('') : '';
  const acts = [];
  if (!ENDED.includes(v.status) && can.start && !r.stopping) acts.push(`<button class="btn ghost" type="button" data-act="stop">${I('x')}Stop</button>`);
  if (v.result?.lines?.length) acts.unshift(`<button class="btn" type="button" data-act="prices">${I('tags')}Show prices</button>`);
  return `<div class="tl-h"><span class="tl-t">${esc(v.title || 'Agent run')}</span><span class="tag ${cls} tl-st">${spin}${esc(text)}</span></div>
    <div class="tl-sub"><span class="tag acc">beta</span> ${esc([v.agent === 'price_check' ? 'Claude with distributor tools' : 'Claude with tools', who].filter(Boolean).join(' · '))}${rev ? ` · <span class="mono">${esc(rev)}</span>` : ''}</div>
    ${lines.length ? `<ul class="tl-lines">${lines.join('')}</ul>` : ''}
    ${needs}
    ${r.keyAdded && v.status === 'running' ? '<div class="tl-note good"><span class="spin"></span>Key added, continuing…</div>' : ''}
    ${(v.requests || []).map((q) => requestHTML(r, q)).join('')}
    ${v.result?.lines?.length ? resultHTML(v) : v.status === 'done' && v.agent === 'price_check' ? '<div class="tl-note">No prices were recorded in this run.</div>' : ''}
    ${r.answer ? `<div class="tl-sum"><div class="tl-k">Claude's summary (beta)</div><p>${esc(r.answer)}</p></div>` : ''}
    ${['failed', 'refused', 'cancelled'].includes(v.status) && v.note ? `<div class="tl-note bad">${esc(v.note)}</div>` : ''}
    ${acts.length ? `<div class="qacts">${acts.join('')}</div>` : ''}`;
}

// Claude often calls a tool for several parts at once: one card per key, and one per tool asking for its first (or
// changed) use, answers all of those calls; a tool that asks before every call keeps a card per call
function group(cards) {
  const out = [], by = new Map();
  for (const c of cards) {
    const k = c.type === 'credential_needed' ? `key ${c.env} ${c.reason}` : c.type === 'tool_request' && c.reason !== 'per_call' ? `tool ${c.tool} ${c.reason}` : '';
    const g = k && by.get(k);
    if (g) { g.ids.push(c.tool_use_id); continue; }
    const card = { ...c, ids: [c.tool_use_id] };
    if (k) by.set(k, card);
    out.push(card);
  }
  return out;
}

function title(tool) { return catalog[tool]?.title || { morph_bom_read: 'Parts list', darwin_search: 'Darwin search', morph_tools_request: 'Tool request', morph_prices_record: 'Price record' }[tool] || tool; }

function lineHTML(e) {
  const name = `<b>${esc(e.title || title(e.tool))}</b>`;
  if (e.type === 'key_saved') return `<li class="ok">${I('key')}<span><b>${esc((e.keys || []).join(', ') || 'Key')}</b> added: the run continued</span></li>`;
  if (e.type === 'skipped') return `<li class="skip">${I('x')}<span>${name} · ${e.decision === 'deny' ? 'denied' : 'skipped'}: the run went on without it</span></li>`;
  if (e.type === 'calls') return `<li class="calls">${I('check')}<span>${name}${e.n > 1 ? ` ×${e.n}` : ''}</span></li>`;  // a run from before steps
  const why = e.ok ? ms(e.ms) : (FAIL[e.error] || FAIL.other)(e);
  return `<li class="${e.ok ? 'ok' : 'bad'}">${I(e.ok ? 'check' : 'alert')}<span>${name}${e.ok && e.host ? ` · <span class="mono">${esc(e.host)}</span>` : ''}${why ? ` · ${esc(why)}` : ''}</span></li>`;
}

function untried(tool) { return catalog[tool]?.verified && catalog[tool].verified.live == null ? '<span class="tag neutral" title="Written from the service\'s documentation; Morph has not yet tried it with a real key">Not yet tried with a real key</span>' : ''; }

function origin(tool) {  // a tool that isn't one of Morph's own: drafted by Claude and approved by the owner, or the owner's manifest
  const t = catalog[tool];
  if (!t || t.curated !== false) return '';
  return t.drafted ? '<span class="tag acc" title="Drafted by Claude from the service\'s documentation and approved by the owner (Morph writes a tool, preview)">drafted</span>'
    : '<span class="tag neutral" title="From a manifest the owner added to this Studio, not one of Morph\'s own tools">your own manifest</span>';
}

function needHTML(r, c) {
  if (c.type === 'run_interrupted') {
    const fix = FIX[c.reason], ask = fix && !can.keys ? fix[1] : can.start ? '' : 'Ask an editor or the owner to continue it.';
    return `<div class="tl-card"><div class="tl-ch">${I('alert')}<span class="tl-cht">${esc(STOPPED[c.reason] || STOPPED.claude_unavailable)}</span></div>
      <p>${esc(c.note || '')}</p>${can.start ? `<div class="qacts"><button class="btn primary" type="button" data-act="continue">${I('replay')}Continue</button>
        ${fix && can.keys ? `<button class="btn ghost" type="button" data-act="key" data-env="${esc(fix[0])}">${I('key')}Open Keys</button>` : ''}</div>` : ''}
      ${ask ? `<div class="tl-ask">${esc(ask)}</div>` : ''}</div>`;
  }
  if (c.type === 'credential_needed') {
    const rejected = c.reason === 'rejected';
    const ids = esc(c.ids.join(','));
    return `<div class="tl-card key" data-tu="${ids}"><div class="tl-ch">${I('key')}<span class="tl-cht">${rejected ? `${esc(c.service)} refused the key` : `Key needed: ${esc(c.service)}`}</span>${untried(c.tool)}${origin(c.tool)}</div>
      ${rejected ? `<p>It may be mistyped, expired or not enabled for this service${can.keys ? ': copy it again or make a new one, then replace it' : ''}.</p>` : ''}
      <p>${esc(c.what || '')}${c.ids.length > 1 ? ` ${c.ids.length} lookups wait for it.` : ''}</p>
      <dl class="tl-dl">${c.cost ? `<dt>Cost</dt><dd>${esc(c.cost)}</dd>` : ''}${c.privacy ? `<dt>Privacy</dt><dd>${esc(c.privacy)}</dd>` : ''}<dt>Sent to</dt><dd class="mono">${esc((c.hosts || []).join(', '))} only</dd></dl>
      <div class="qacts">${can.keys ? `<button class="btn primary" type="button" data-act="key" data-env="${esc(c.env)}">${I('key')}${rejected ? 'Replace key' : 'Add key'}</button>` : ''}
        ${c.url && can.keys ? `<a class="btn ghost" href="${esc(c.url)}" target="_blank" rel="noopener">${I('ext')}Get a key</a>` : ''}
        ${can.skip ? `<button class="btn ghost" type="button" data-act="skip" data-tu="${ids}">Skip</button>` : ''}</div>
      ${can.keys ? '' : `<div class="tl-ask">Ask the owner to ${rejected ? 'replace' : 'add'} the ${esc(c.label || 'key')} in Keys.${can.skip ? ' Or skip it: the run carries on without it.' : ''}</div>`}</div>`;
  }
  const per = c.reason === 'per_call';
  const head = per ? `Allow this call to ${esc(c.title)}?` : c.reason === 'changed' ? `${esc(c.title)} changed since you allowed it: allow it again?` : `Allow ${esc(c.title)}?`;
  const ids = esc(c.ids.join(','));
  return `<div class="tl-card ask" data-tu="${ids}"><div class="tl-ch">${I('alert')}<span class="tl-cht">${head}</span>${untried(c.tool)}${origin(c.tool)}</div>
    <p>${esc(SIDE[c.side_effect] || '')} Sends ${esc(c.sends || 'its arguments')} to <span class="mono">${esc(c.host || '')}</span>.</p>
    <dl class="tl-dl"><dt>Cost</dt><dd>${c.cost_usd ? `About $${esc(c.cost_usd)} a call` : 'No fee per call'}${c.cost_note ? `. ${esc(c.cost_note)}` : ''}</dd>${c.privacy ? `<dt>Privacy</dt><dd>${esc(c.privacy)}</dd>` : ''}</dl>
    ${c.input ? `<pre class="tl-args mono">${esc(c.input)}</pre>` : ''}${c.ids.length > 1 ? `<div class="tl-ask">and ${c.ids.length - 1} more call${c.ids.length > 2 ? 's' : ''} of it in this step: your answer covers them all.</div>` : ''}
    <div class="qacts">${can.allow ? `<button class="btn primary" type="button" data-act="allow_once" data-tu="${ids}">Allow once</button>${c.side_effect === 'read' && !per ? `<button class="btn" type="button" data-act="allow_always" data-tu="${ids}">Always</button>` : ''}` : ''}
      ${can.skip ? `<button class="btn ghost" type="button" data-act="skip" data-tu="${ids}">Skip</button>` : ''}</div>
    ${can.allow ? '' : `<div class="tl-ask">Ask the owner to allow it.${can.skip ? ' Or skip it: the run carries on without it.' : ''}</div>`}</div>`;
}

const DRAFTED = { drafting: 'Claude is drafting a tool for it: its card is below.', ready: 'A drafted tool for it waits for review: its card is below.',
  installed: 'A tool for it is installed: the next run can use it.', refused: "Morph refused the tool drafted for it: see its card below." };

function requestHTML(r, q) {  // a need no tool covers: Darwin's suggestions (editors and the owner), and the owner may draft a tool
  const d = drafts.forRequest(r.view.id, q.tool_use_id), owner = can.draft;
  const sugg = can.start && !d ? drafts.suggestionsHTML(drafts.suggestions(q.need), 3, true) : '';
  return `<div class="tl-card cap"><div class="tl-ch">${I('spark')}<span class="tl-cht">The agent needs a tool Morph doesn't have</span></div><p>${esc(q.need || '')}</p>
    ${q.why ? `<p class="dim">${esc(q.why)}</p>` : ''}${sugg}
    ${d && DRAFTED[d.status] ? `<div class="tl-ask">${esc(DRAFTED[d.status])}</div>` : ''}
    ${owner && (!d || ['failed', 'not_possible', 'refused', 'discarded'].includes(d.status)) ? `<div class="qacts"><button class="btn" type="button" data-act="draft" data-tu="${esc(q.tool_use_id || '')}">${I('spark')}Draft a tool (preview)</button></div>` : ''}
    <div class="tl-ask">Noted in Morph's tool requests. The run carries on without it.${owner || d ? '' : ' Ask the owner to draft a tool for it (Keys, New tool).'}</div></div>`;
}

function resultHTML(v) {
  const res = v.result, rows = res.lines || [], items = new Set(rows.map((x) => x.item)).size, total = v.context?.lines;
  const svcs = [...new Set(rows.map((x) => x.service).filter(Boolean))];
  return `<div class="tl-res"><div class="tl-k">${items}${total ? ` of ${total}` : ''} purchased line${(total || items) === 1 ? '' : 's'} priced${svcs.length ? ` · ${esc(svcs.join(', '))}` : ''}${res.checked_at ? ` · ${esc(hhmm(res.checked_at))}` : ''}</div>
    <ul class="tl-prices">${rows.slice(0, 4).map((x) => `<li><span class="it" title="${esc(x.item)}">${esc(x.item)}</span><span class="pr num">${esc(price(x.unit_price, x.currency))}${x.matched ? '' : ' <span class="tag warn" title="Morph couldn\'t find this price or part number in the tool\'s answer">check</span>'}</span></li>`).join('')}</ul>
    ${rows.length > 4 ? `<div class="dim">and ${rows.length - 4} more: Show prices</div>` : ''}</div>`;
}

function showPrices(r) {
  const v = r.view, res = v.result || {}, rows = res.lines || [];
  const html = `<div class="dlg-h"><div class="kicon">${I('tags')}</div><div class="grow"><div class="t">${esc(v.title || 'Live prices')} <span class="tag acc">beta</span></div>
      <div class="s">Found by Claude with the distributor tools under Keys${res.checked_at ? `, at ${esc(hhmm(res.checked_at))}` : ''}. Prices and stock change: check with the distributor before you buy. Nothing was ordered.</div></div>
      <button class="btn icon ghost" type="button" data-x title="Close (Esc)">${I('x')}</button></div>
    <div class="tl-table"><table class="table"><thead><tr><th>Item</th><th>Distributor</th><th>Part number</th><th class="num">Unit price</th><th class="num">At qty</th><th class="num">Stock</th><th></th></tr></thead><tbody>
      ${rows.map((x) => `<tr><td class="strong">${esc(x.item)}</td><td>${esc(x.distributor)}</td><td class="mono">${esc(x.part_number)}</td><td class="num">${esc(price(x.unit_price, x.currency)) || '–'}</td>
        <td class="num">${x.quantity ?? '–'}</td><td class="num">${x.stock ?? '–'}</td><td>${x.matched ? `<span class="tag good" title="Found in ${esc(x.service)}'s answer">${I('check')}from ${esc(x.service)}</span>` : '<span class="tag warn" title="Morph couldn\'t find this price or part number in the tool\'s answer">check it</span>'}</td></tr>`).join('')}
    </tbody></table></div>
    ${r.answer ? `<div class="tl-sum"><div class="tl-k">Claude's summary (beta)</div><p>${esc(r.answer)}</p></div>` : ''}
    <div class="dlg-f"><span class="grow"></span>${can.start ? '<button class="btn" type="button" data-again>Check again</button>' : ''}<button class="btn primary" type="button" data-x>Close</button></div>`;
  const dlg = studio.modal.open(html, { wide: true });
  dlg.querySelectorAll('[data-x]').forEach((b) => (b.onclick = () => studio.modal.close()));
  const ag = dlg.querySelector('[data-again]');
  if (ag) ag.onclick = () => { studio.modal.close(); startPrices(); };
}

// ------------------------------------------------------------------ the BOM tab's button
function watchBom() {
  const view = document.querySelector('#views .view[data-view="bom"]');
  if (!view) return;
  new MutationObserver(() => bomButton()).observe(view, { childList: true, subtree: true });
  bomButton();
}

function latestPrices() {
  const rev = studio.S?.current?.rev;
  const list = [...runs.values()].map((r) => r).filter((r) => r.view.agent === 'price_check' && r.view.context?.rev === rev);
  return list.sort((a, b) => Date.parse(b.view.updated || 0) - Date.parse(a.view.updated || 0))[0] || null;
}

function bomButton() {
  const bar = document.querySelector('#views .view[data-view="bom"] .bm .filters');
  if (!bar || !studio) return;
  if (!can.start && !latestPrices()?.view.result?.lines?.length) { bar.querySelector('.tl-bom')?.remove(); return; }  // viewers: prices only
  const r = latestPrices(), v = r?.view, busy = v && !ENDED.includes(v.status), w = v?.waiting_on || [];
  const n = v?.result?.lines?.length ? new Set(v.result.lines.map((x) => x.item)).size : 0;
  const label = !busy ? (n ? `Live prices (beta) · ${n} priced` : 'Live prices (beta)') : v.status !== 'paused' ? 'Live prices: checking'
    : w.includes('credential') ? 'Live prices: key needed' : w.includes('approval') ? 'Live prices: needs an OK' : 'Live prices: paused';
  const tip = busy ? 'A price check is running for this revision: its card in the chat says what it waits for'
    : n ? `See the prices found at ${hhmm(v.result.checked_at)}, or check again` : ready.claude === false
      ? 'Live prices need Claude: add the Anthropic key in Keys' : 'Claude checks distributor prices and stock for the purchased parts (uses your Anthropic key and the tool keys under Keys)';
  let b = bar.querySelector('.tl-bom');
  if (!b) {
    b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn tl-bom';
    b.onclick = () => {
      const cur = latestPrices();
      if (cur && !ENDED.includes(cur.view.status)) { paint(cur); cur.el?.scrollIntoView({ block: 'end' }); return; }  // what it waits for is at its end
      if (cur?.view.result?.lines?.length) { showPrices(cur); return; }
      startPrices();
    };
  }
  // compared with what was last written, never with innerHTML: the browser serializes the icon differently, and a
  // rewrite on every call would wake the tab's observer again and again
  const html = `${busy && v.status === 'running' ? '<span class="spin"></span>' : busy ? I(w.includes('credential') ? 'key' : 'alert') : I('tags')}${esc(label)}`;
  if (b.tlHTML !== html) { b.innerHTML = html; b.tlHTML = html; }
  b.title = tip;
  b.classList.toggle('primary', !busy && !n);
  if (b.parentElement !== bar || bar.lastElementChild !== b) bar.appendChild(b);
  bomBtn = b;
}

function showBom() { bomButton(); return bomBtn; }

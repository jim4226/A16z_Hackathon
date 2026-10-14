// Morph Studio: Morph writes a tool (preview), part of the agent tools feature (tools.js starts it; RELEASE_CONTRACTS 15.2).
// A need no tool covers (the agent's "needs a tool Morph doesn't have" card) or Keys' "New tool (preview)" opens the draft
// dialog: what the tool should look up, and the API's official documentation. Claude reads that documentation (web_fetch,
// limited to its site) and drafts a declarative spec; Morph checks it. Each draft gets a card in the chat of the project it
// came from: drafting, then ready for review (Review opens every detail: hosts and where the documentation names them, each
// lookup, what is sent, where the key goes, cost, privacy, the spec's sha256), Approve (installs exactly that spec) or
// Discard. Darwin's public search suggests services that might cover a need, as links only. Teams: only the owner drafts,
// approves or discards; everyone else is told to ask the owner.

const STATUS = {
  drafting: ['neutral', true, 'Drafting'], ready: ['warn', false, 'Ready for review'], refused: ['bad', false, "Won't install"],
  not_possible: ['neutral', false, 'No tool for it'], failed: ['bad', false, "Couldn't draft"], installed: ['good', false, 'Installed'],
  discarded: ['neutral', false, 'Discarded'],
};
const RECENT_MS = 30 * 60 * 1000;  // finished drafts shown again after a reload: the last half hour's

let studio = null, hooks = {};
let can = { draft: false, start: false, keys: false }, ready = {};
const drafts = new Map();  // id -> {view, el}
const sugg = new Map();    // need (lower case) -> {status: loading | done | error, data, error}
const listeners = new Set();
let refreshing = null, again = false, loaded = false;

const esc = (s) => (studio?.esc ? studio.esc(s) : String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
const KEY = '<circle cx="7.5" cy="15.5" r="4"/><path d="M10.5 12.5L20 3M16 7l3 3M13.5 9.5l2 2"/>';
const I = (name, size = 'sm') => (name === 'key' ? `<svg class="i ${size}" viewBox="0 0 24 24" aria-hidden="true">${KEY}</svg>` : studio?.I ? studio.I(name, size) : '');
const hostOf = (url) => { try { return new URL(url).host; } catch { return ''; } };

// ------------------------------------------------------------------ public
export function init(s, h = {}) {
  studio = s;
  hooks = h;
  const css = new URL('./tooldraft.css', import.meta.url).href;
  if (!document.querySelector(`link[href="${css}"]`)) {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = css;
    document.head.appendChild(l);
  }
  window.addEventListener('morph:tool-draft', (e) => open(e.detail || {}));
  const on = (t, fn) => { try { studio.on?.(t, fn); } catch (e) { /* older shells */ } };
  on('tool_draft', () => refresh());
  on('project', (ev) => { if (ev.switched || ev.reset) { for (const d of drafts.values()) d.el?.remove(); drafts.clear(); refresh(); } });
  on('connection', () => refresh());
  watchChat();
  refresh();
  return { open, refresh, forRequest, suggestions, state: () => ({ drafts: [...drafts.values()].map((d) => d.view), can, ready }) };
}

// the newest draft started from a run's need card (its run and tool_use_id)
export function forRequest(run, tu) {
  return [...drafts.values()].map((d) => d.view).filter((v) => v.source?.run === run && v.source?.tool_use_id === tu)
    .sort((a, b) => Date.parse(b.created || 0) - Date.parse(a.created || 0))[0] || null;
}

// Darwin's suggestions for a need, fetched once per page and need (the Studio caches Darwin's answers too)
export function suggestions(need) {
  const k = String(need || '').trim().toLowerCase();
  if (k.length < 3) return null;
  let s = sugg.get(k);
  if (s) return s;
  s = { status: 'loading', data: null, error: '' };
  sugg.set(k, s);
  studio.api.get('/api/tools/suggest', { need: String(need).trim() })
    .then((d) => { s.status = 'done'; s.data = d; })
    .catch((e) => { s.status = 'error'; s.error = e.body?.error || e.message || ''; })
    .finally(() => { hooks.changed?.(); for (const fn of [...listeners]) fn(); });
  return s;
}

export function suggestionsHTML(s, max = 4, compact = false) {  // compact: the need card in the chat (no question, a short note)
  if (!s) return '';
  if (s.status === 'loading') return '<div class="td-sg dim"><span class="spin"></span>Asking Darwin which services might cover it…</div>';
  if (s.status === 'error') return '';
  const d = s.data || {}, list = (d.suggestions || []).slice(0, max);
  const q = d.question && !compact ? `<div class="dim">Darwin asks: "${esc(d.question)}" Answer it on Darwin if you look there; Morph never answers for you.</div>` : '';
  const note = compact ? "From Darwin's public search, not checked by Morph. Morph never connects, signs in or pays for you."
    : "Suggestions from Darwin's public search: Morph hasn't checked them and never connects, signs in or pays for you. If one has an API with documentation, a tool can be drafted from it.";
  if (!list.length) return `<div class="td-sg dim">${d.error && d.cached !== true ? "Darwin couldn't be reached for suggestions." : "Darwin's public search knows no service for it."}</div>`;
  return `<div class="td-sg"><div class="tl-k">Darwin knows</div><ul>${list.map((x) => `<li><b>${esc(x.name)}</b>${x.owner ? ` by ${esc(x.owner)}` : ''}
      ${x.website ? ` · <a href="${esc(x.website)}" target="_blank" rel="noopener noreferrer">${esc(hostOf(x.website))}</a>` : ''}
      ${x.darwin_url ? ` · <a href="${esc(x.darwin_url)}" target="_blank" rel="noopener noreferrer">on Darwin</a>` : ''}
      ${x.paid ? ' <span class="tag warn">paid per call</span>' : ''}${x.sign_in ? ' <span class="tag neutral">needs a sign-in</span>' : ''}</li>`).join('')}</ul>
    ${q}<div class="dim">${note}</div></div>`;
}

// ------------------------------------------------------------------ data
async function refresh() {  // one at a time; a call during one runs once more after it
  if (refreshing) { again = true; return refreshing; }
  refreshing = (async () => {
    try {
      do {
        again = false;
        const out = await studio.api.get('/api/tools/drafts', { limit: 10 });
        can = out.can || can; ready = out.ready || ready; loaded = true;
        const now = Date.now();
        for (const v of [...(out.drafts || [])].reverse()) {  // oldest first, so the newest card ends up lowest
          const d = drafts.get(v.id);
          if (d) {
            if (d.view.status !== 'installed' && v.status === 'installed') hooks.installed?.(v);
            d.view = v;
            continue;
          }
          if (!['drafting', 'ready'].includes(v.status) && now - Date.parse(v.updated || 0) > RECENT_MS) continue;
          drafts.set(v.id, { view: v, el: null });
        }
        for (const d of drafts.values()) paint(d);
        hooks.changed?.();
      } while (again);
    } catch (e) {
      console.warn('[tools] drafts', e);
    } finally { refreshing = null; }
  })();
  return refreshing;
}

async function act(d, what) {
  const v = d.view;
  if (what === 'review') { review(d); return; }
  if (what === 'again') { open({ need: v.need, why: v.why, docs_url: v.docs_url, run: v.source?.run, tool_use_id: v.source?.tool_use_id }); return; }
  if (what === 'key') { window.dispatchEvent(new CustomEvent('morph:keys', { detail: { key: v.spec?.credentials?.[0]?.env } })); return; }
  if (what === 'run') { hooks.startAgent?.(v.source?.agent); return; }
  try {
    if (what === 'approve') {
      const out = await studio.api.post('/api/tools/drafts/approve', { draft: v.id, sha256: v.sha256 });
      d.view = out.draft;
      studio.modal.close();
      studio.toast(`${v.card?.service || 'The tool'} is installed: the next run can use it`);
      hooks.installed?.(out.draft);
    } else if (what === 'discard') {
      d.view = (await studio.api.post('/api/tools/drafts/discard', { draft: v.id })).draft;
      studio.modal.close();
    }
    paint(d);
  } catch (e) {
    studio.toast(e.body?.error || e.message || "Couldn't reach the Studio");
    refresh();
  }
}

// ------------------------------------------------------------------ the chat card
function watchChat() {  // the chat is redrawn (project switch, reconnect, reset): put the cards back after its history
  const box = document.querySelector('#messages');
  if (!box) return;
  let t = null;
  new MutationObserver(() => {
    if (![...drafts.values()].some((d) => d.el && !d.el.isConnected)) return;
    for (const d of drafts.values()) d.el = d.el?.isConnected ? d.el : null;
    clearTimeout(t);
    t = setTimeout(() => { for (const d of drafts.values()) paint(d); }, 60);
  }).observe(box, { childList: true });
}

function paint(d) {
  if (!studio || (d.view.project && studio.project?.() && d.view.project !== studio.project())) return;
  if (!d.el) {
    d.el = studio.addMsg ? studio.addMsg('assistant', '') : null;
    if (!d.el) return;
    d.el.classList.add('tl-run', 'td-run');
    d.el.dataset.draft = d.view.id;
    d.el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-act]');
      if (!b || b.disabled) return;
      if (['approve', 'discard'].includes(b.dataset.act)) d.el.querySelectorAll('[data-act]').forEach((x) => (x.disabled = true));
      act(d, b.dataset.act);
    });
  }
  const box = d.el.querySelector('.b'), chat = d.el.parentElement;
  if (!box) return;
  const follow = !!chat && (!box.innerHTML || d.el === chat.lastElementChild || chat.scrollHeight - chat.scrollTop - chat.clientHeight < 90);
  box.innerHTML = cardHTML(d.view);
  if (follow) requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
}

function cardHTML(v) {
  const [cls, spin, text] = STATUS[v.status] || ['neutral', false, v.status || ''];
  const c = v.card, owner = can.draft, acts = [];
  const name = c?.service || (v.status === 'drafting' ? '' : v.title || '');  // while drafting the status chip says it
  let body = '';
  if (v.status === 'drafting') {
    body = `<div class="tl-note"><span class="spin"></span><span>Claude is reading <span class="mono">${esc(v.docs_url)}</span> and drafting a read-only lookup (about a minute). Nothing is installed until ${owner ? 'you approve it' : 'the owner approves it'}.</span></div>`;
    if (owner) acts.push('<button class="btn ghost" type="button" data-act="discard">Stop</button>');
  } else if (v.status === 'ready') {
    body = summaryHTML(v);
    acts.push(`<button class="btn primary" type="button" data-act="review">${I('eye')}${owner ? 'Review and approve' : 'Show the draft'}</button>`);
    if (owner) acts.push('<button class="btn ghost" type="button" data-act="discard">Discard</button>');
  } else if (v.status === 'refused') {
    body = `<div class="tl-note bad">Morph won't install this draft:</div><ul class="td-probs">${(v.problems || []).map((p) => `<li>${esc(p.message)}</li>`).join('')}</ul>`;
    if (v.card) acts.push(`<button class="btn" type="button" data-act="review">${I('eye')}Show the draft</button>`);
  } else if (v.status === 'not_possible') {
    body = `<div class="tl-note">Claude found no read-only lookup for it in that documentation: ${esc(v.reason || '')}</div>`;
  } else if (v.status === 'failed') {
    body = `<div class="tl-note bad">${esc(v.note || "Claude couldn't draft it.")}</div>`;
  } else if (v.status === 'installed') {
    const tools = (v.spec?.tools || []).map((t) => t.title), key = c?.key;
    body = `<div class="tl-note good">${I('check')}<span>Installed${v.installed?.replaced ? ', replacing the version approved before' : ''}. The next run can use ${esc(tools.join(' and '))}.${key ? ` The ${esc(key.label)} is asked for when a run first needs it, or add it in Keys now.` : ''}</span></div>`;
    if (key && can.keys) acts.push(`<button class="btn primary" type="button" data-act="key">${I('key')}Add the key</button>`);
    if (v.source?.agent && can.start) acts.push(`<button class="btn" type="button" data-act="run">${I('replay')}Run ${esc(v.source.agent === 'price_check' ? 'Live prices' : v.source.title || 'it')} again</button>`);
  } else {
    body = `<div class="tl-note">${esc(v.note || 'Discarded.')}</div>`;
  }
  if (['refused', 'not_possible', 'failed'].includes(v.status) && owner) {
    acts.push('<button class="btn" type="button" data-act="again">Draft again</button>', '<button class="btn ghost" type="button" data-act="discard">Discard</button>');
  }
  const ask = !owner && ['drafting', 'ready', 'refused'].includes(v.status) ? '<div class="tl-ask">Only the owner can approve a new tool: ask them to review it.</div>' : '';
  return `<div class="tl-h"><span class="tl-t">New tool${name ? `: ${esc(name)}` : ''}</span><span class="tag ${cls} tl-st">${spin ? '<span class="spin"></span>' : ''}${esc(text)}</span></div>
    <div class="tl-sub"><span class="tag acc">preview</span> drafted by Claude from <span class="mono">${esc(v.docs_host || '')}</span>${v.source?.title ? ` · for ${esc(v.source.title)}` : ''}</div>
    <div class="td-need"><span class="dim">Asked for:</span> ${esc(v.need)}</div>
    ${body}${ask}${acts.length ? `<div class="qacts">${acts.join('')}</div>` : ''}`;
}

function summaryHTML(v) {  // the card in the chat: what it is and where it goes; Review shows every detail
  const c = v.card;
  if (!c) return '';
  const warn = (v.warnings || []).length;
  return `<dl class="tl-dl"><dt>Lookups</dt><dd>${c.tools.map((t) => esc(t.title)).join(', ')}</dd>
      <dt>Sent to</dt><dd class="mono">${esc(c.hosts.map((h) => h.host).join(', '))} only</dd>
      <dt>Key</dt><dd>${c.key ? `${esc(c.key.label)} (${c.key.in === 'query' ? 'query parameter' : 'header'} ${esc(c.key.name)})` : 'None needed'}</dd>
      <dt>Cost</dt><dd>No fee per call</dd></dl>
    ${warn ? `<div class="tl-ask">${I('alert')} ${warn} thing${warn === 1 ? '' : 's'} to check before approving: see Review.</div>` : ''}`;
}

// ------------------------------------------------------------------ the review dialog
function query(q) {
  const parts = Object.entries(q || {}).map(([k, v]) => `${k}=${v}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

function review(d) {
  const v = d.view, c = v.card || {}, owner = can.draft && v.status === 'ready';
  const hosts = (c.hosts || []).map((h) => `<div class="td-host"><span class="mono">${esc(h.host)}</span> ${h.named ? '<span class="tag good">named in the documentation</span>' : '<span class="tag bad">not named in the documentation</span>'}
      ${h.addresses === 'public' ? '<span class="tag neutral">public address</span>' : h.addresses === 'private' ? '<span class="tag bad">private address</span>' : '<span class="tag neutral">address checked before every call</span>'}
      ${h.named ? `<div class="td-snip">${esc(h.named)}</div>` : ''}</div>`).join('');
  const tools = (c.tools || []).map((t) => `<div class="td-tool"><div><b>${esc(t.title)}</b> <span class="mono dim">${esc(t.name)}</span></div>
      <div class="mono td-url">${esc(t.method)} ${esc(t.url)}${esc(query(t.query))}</div>
      <div>${esc(t.description)}</div>
      <div class="dim">Sends ${esc(t.sends)}.${t.params.length ? ` Inputs: ${t.params.map((p) => `${esc(p.name)} (${esc(p.type)}${p.required ? '' : ', optional'}${p.choices.length ? `: ${esc(p.choices.join(', '))}` : ''})`).join('; ')}.` : ''} Keeps ${esc(t.extract.join(', ') || 'the whole answer')}.</div>
      <div class="td-snip">${t.found ? `In the documentation: ${esc(t.found)}` : "Morph didn't find this path in the documentation: check it."}</div></div>`).join('');
  const k = c.key;
  const key = k ? `<b>${esc(k.label)}</b>, in the <span class="mono">${esc(k.name)}</span> ${k.in === 'query' ? 'query parameter' : 'header'}, sent only to <span class="mono">${esc(k.hosts.join(', '))}</span>. Kept in Keys as <span class="mono">${esc(k.env)}</span>; the owner gets it at ${esc(k.where)}${k.url ? ` (<a href="${esc(k.url)}" target="_blank" rel="noopener noreferrer">${esc(hostOf(k.url))}</a>)` : ''}.${k.cost ? ` ${esc(k.cost)}.` : ''}` : 'None needed.';
  const pages = (v.pages || []).map((p) => `<div><span class="mono">${esc(p.url)}</span> ${p.ok ? `<span class="tag good">read</span>${p.title ? ` ${esc(p.title)}` : ''}` : `<span class="tag bad">${esc(p.error || 'failed')}</span>`}</div>`).join('');
  const html = `<div class="dlg-h"><div class="kicon">${I('wrench')}</div><div class="grow"><div class="t">New tool: ${esc(c.service || v.title || '')} <span class="tag acc">preview</span></div>
      <div class="s">Drafted by Claude from the documentation you named; checked by Morph. ${owner ? 'Approving installs exactly this spec. Any later change to it turns the tool off until a new draft is approved.' : v.status === 'ready' ? 'Only the owner can approve it.' : 'Morph refused it, so it can\'t be installed.'}</div></div>
      <button class="btn icon ghost" type="button" data-x title="Close (Esc)">${I('x')}</button></div>
    ${v.status === 'refused' ? `<div class="kchk bad">${I('alert')}<span>${(v.problems || []).map((p) => esc(p.message)).join('<br>')}</span></div>` : ''}
    <dl class="tl-dl td-dl"><dt>Asked for</dt><dd>${esc(v.need)}</dd>
      <dt>Service</dt><dd>${esc(c.service || '')}${c.summary ? ` · ${esc(c.summary)}` : ''}</dd>
      <dt>Hosts</dt><dd>${hosts || '–'}</dd>
      <dt>Lookups</dt><dd>${tools || '–'}</dd>
      <dt>Key</dt><dd>${key}</dd>
      <dt>Cost</dt><dd>${esc(c.cost || '')}</dd>
      <dt>Privacy</dt><dd>${esc(c.privacy || '')}</dd>
      <dt>Read</dt><dd>${pages || '–'}</dd>
      <dt>sha256</dt><dd class="mono td-sha">${esc(v.sha256 || 'none: refused drafts are never installed')}</dd></dl>
    ${(v.warnings || []).length ? `<div class="td-k">Check before approving</div><ul class="td-warn">${v.warnings.map((w) => `<li>${I('alert')}<span>${esc(w)}</span></li>`).join('')}</ul>` : ''}
    ${v.notes ? `<div class="tl-sum"><div class="tl-k">Claude's notes</div><p>${esc(v.notes)}</p></div>` : ''}
    <details class="td-spec"><summary>The spec Morph ${owner ? 'would install' : 'checked'} (JSON)</summary><pre class="tl-args mono">${esc(JSON.stringify(v.spec, null, 2))}</pre></details>
    <div class="dlg-f"><span class="grow"></span><button class="btn" type="button" data-x>Close</button>
      ${owner ? `<button class="btn ghost" type="button" data-act="discard">Discard</button><button class="btn primary" type="button" data-act="approve">${I('check')}Approve and install</button>` : ''}</div>`;
  const dlg = studio.modal.open(html, { wide: true });
  dlg.classList.add('td-review');
  dlg.querySelectorAll('[data-x]').forEach((b) => (b.onclick = () => studio.modal.close()));
  dlg.querySelectorAll('[data-act]').forEach((b) => (b.onclick = () => { dlg.querySelectorAll('[data-act]').forEach((x) => (x.disabled = true)); act(d, b.dataset.act); }));
}

// ------------------------------------------------------------------ the draft dialog
export async function open(o = {}) {
  if (!loaded) await refresh();
  if (!can.draft) { studio.toast('Only the owner can draft a new tool: ask them (Keys, New tool).'); return; }
  const st = { need: o.need || '', why: o.why || '', docs: o.docs_url || '', run: o.run || null, tu: o.tool_use_id || null, busy: false, note: '' };
  const dlg = document.createElement('div');
  dlg.className = 'glass dlg td-dlg';
  const render = () => {
    const off = ready.agent_tools === false, noClaude = ready.claude === false;
    const s = st.need.trim().length >= 3 ? sugg.get(st.need.trim().toLowerCase()) : null;
    dlg.innerHTML = `<div class="dlg-h"><div class="kicon">${I('wrench')}</div><div class="grow"><div class="t">New tool <span class="tag acc">preview</span></div>
        <div class="s">Claude reads the API documentation you name and drafts a read-only lookup tool from it. Morph never runs code from a draft: its own HTTP runner does, and only after you approve the draft.</div></div>
        <button class="btn icon ghost" type="button" data-x title="Close (Esc)">${I('x')}</button></div>
      <label class="td-f"><span>What should the tool look up?</span><textarea rows="2" maxlength="300" data-f="need" placeholder="Live price and stock for a part number at my distributor">${esc(st.need)}</textarea></label>
      <label class="td-f"><span>The API's official documentation (a page on the service's own site)</span><input type="url" maxlength="250" data-f="docs" placeholder="https://docs.example.com/api" value="${esc(st.docs)}"></label>
      <div class="td-rules"><b>What a draft may be:</b> GET lookups only, to hosts the documentation names; nothing is written, uploaded or paid. Its key stays in Keys and goes only to those hosts. Claude reads pages of that documentation site only, using your Anthropic key.</div>
      ${s ? suggestionsHTML(s) : st.need.trim().length >= 3 ? '<div class="td-sg"><button class="btn ghost" type="button" data-a="darwin">Ask Darwin which services might cover it</button></div>' : ''}
      ${st.note ? `<div class="kchk bad">${I('alert')}<span>${esc(st.note)}</span></div>` : ''}
      ${off ? `<div class="kchk bad">${I('alert')}<span>Agent tools are off: turn on "Let agents use tools" in Keys, Privacy and network, to draft a tool.</span></div>` : ''}
      ${noClaude && !off ? `<div class="kchk bad">${I('alert')}<span>Drafting a tool needs Claude: add the Anthropic key in Keys (and keep network lookups on).</span></div>` : ''}
      <div class="dlg-f"><span class="grow"></span><button class="btn" type="button" data-x>Cancel</button>
        ${noClaude || off ? `<button class="btn primary" type="button" data-a="keys">${I('key')}Open Keys</button>`
          : `<button class="btn primary" type="button" data-a="go"${st.busy ? ' disabled' : ''}>${st.busy ? '<span class="spin"></span>Starting…' : `${I('spark')}Draft the tool`}</button>`}</div>`;
    dlg.querySelectorAll('[data-x]').forEach((b) => (b.onclick = () => studio.modal.close()));
    dlg.querySelector('[data-f="need"]').oninput = (e) => { const was = st.need.trim().length >= 3; st.need = e.target.value; if (was !== (st.need.trim().length >= 3)) keep(render); };
    dlg.querySelector('[data-f="docs"]').oninput = (e) => { st.docs = e.target.value; };
    const dw = dlg.querySelector('[data-a="darwin"]');
    if (dw) dw.onclick = () => { suggestions(st.need); render(); };
    const kb = dlg.querySelector('[data-a="keys"]');
    if (kb) kb.onclick = () => { studio.modal.close(); window.dispatchEvent(new CustomEvent('morph:keys', { detail: { key: noClaude && !off ? 'ANTHROPIC_API_KEY' : '' } })); };
    const go = dlg.querySelector('[data-a="go"]');
    if (go) go.onclick = () => submit(st, render);
  };
  const keep = (fn) => {  // re-render without losing the caret in the field being typed in
    const f = document.activeElement?.dataset?.f, pos = document.activeElement?.selectionStart;
    fn();
    const el = f && dlg.querySelector(`[data-f="${f}"]`);
    if (el) { el.focus(); try { el.setSelectionRange(pos, pos); } catch { /* not a text field */ } }
  };
  const changed = () => { if (dlg.isConnected) keep(render); };
  listeners.add(changed);
  if (st.need.trim().length >= 3 && (o.run || o.suggest)) suggestions(st.need);  // from a need card: ask Darwin at once
  render();
  studio.modal.open(dlg, { onClose: () => listeners.delete(changed) });
  (st.need ? dlg.querySelector('[data-f="docs"]') : dlg.querySelector('[data-f="need"]'))?.focus();
}

async function submit(st, render) {
  st.note = '';
  const need = st.need.trim(), docs = st.docs.trim();
  if (need.length < 3) { st.note = 'Say what the tool should look up (3 to 300 characters).'; render(); return; }
  if (!/^https:\/\/[^\s/]+\.[^\s/]+/i.test(docs)) { st.note = "Give the documentation's https:// address, on the service's own site."; render(); return; }
  st.busy = true;
  render();
  try {
    const out = await studio.api.post('/api/tools/draft', { need, docs_url: docs, why: st.why || undefined, run: st.run || undefined, tool_use_id: st.tu || undefined });
    studio.modal.close();
    const d = drafts.get(out.draft.id) || { view: out.draft, el: null };
    d.view = out.draft;
    drafts.set(out.draft.id, d);
    paint(d);
    hooks.changed?.();
    studio.toast(out.already ? 'Claude is already drafting that tool: its card is in the chat' : 'Claude is drafting the tool: its card is in the chat');
  } catch (e) {
    st.busy = false;
    if (e.status === 503) ready = { ...ready, claude: false };
    else if (e.status === 409 && /Agent tools are off/.test(e.body?.error || '')) ready = { ...ready, agent_tools: false };
    else st.note = e.body?.error || e.message || "Couldn't reach the Studio";
    render();
  }
}

// Morph Teams (preview): GitHub for physical things inside the Studio. Who is here, Share (invites, members, roles,
// disciplines), comments anchored to a revision, stage, BOM line or parameter, change requests routed to the owners
// of the disciplines they touch, reviews with the release gate, branches, the activity feed and your inbox.
// init(studio) mounts into the feature slots (RELEASE_CONTRACTS section 4). When /api/me says team:false (a solo
// Studio) it adds nothing. Other panels can open a comment with
//   document.dispatchEvent(new CustomEvent('morph:comment', { detail: { kind: 'bom', item, category } }))

// ---- constants
const LEVEL = { prototype: 'Prototype', pilot: 'Pilot', production: 'Production' };
const KIND = { rev: 'Revision', stage: 'Stage', bom: 'BOM line', param: 'Parameter', file: 'File' };
const NODES = [['sizing', 'Sizing'], ['gripper', 'Gripper + grasp'], ['electronics', 'Electronics plan'], ['cad', 'Robot CAD'],
  ['sim', 'Simulation'], ['wiring', 'Schematic · PCB · FW'], ['manufacturing', 'Manufacturing'], ['bom', 'BOM + sourcing'], ['manual', 'Assembly manual']];
const ROLE_HELP = {
  viewer: 'Sees everything; comments and proposes changes',
  editor: 'Also edits, builds, imports, reviews, applies change requests and releases',
  owner: 'Everything, plus members, invites, policy, keys and the audit log',
};
const PATHS = {
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c.6-3.4 3-5.4 6-5.4s5.4 2 6 5.4"/><circle cx="17" cy="9" r="2.6"/><path d="M15.8 14.7c2.6.2 4.4 1.9 5 4.8"/>',
  bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2.2 2.2 0 0 0 4 0"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-11.5 7.5"/>',
  tag: '<path d="M3 12.5V3h9.5L21 11.5 11.5 21z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
  pr: '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/><path d="M6 8v8M18 16V9a3 3 0 0 0-3-3h-4M13 3.5L10.5 6 13 8.5"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
  link: '<path d="M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-1 1"/><path d="M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l1-1"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  key: '<circle cx="7.5" cy="15.5" r="4"/><path d="M10.5 12.5L20 3M16 7l3 3"/>',
  dot: '<circle cx="12" cy="12" r="4"/>',
  alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  pulse: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  reply: '<path d="M10 7L4 12l6 5"/><path d="M4 12h10a6 6 0 0 1 6 6"/>',
};
const icon = (n, cls = 'sm') => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${PATHS[n] || ''}</svg>`;
const escH = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const color = (c) => (/^#[0-9a-f]{6}$/i.test(c || '') ? c : '#5d6676');
const initials = (n) => { const p = String(n || '?').trim().split(/\s+/); return ((p[0] || '?')[0] + (p.length > 1 ? p[p.length - 1][0] : '')).toUpperCase(); };
function ago(ts) {
  const t = Date.parse(ts || '');
  if (!t) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString();
}
// when something runs out ("expires in 6 days"), for invites
function expiry(ts) {
  const t = Date.parse(ts || '');
  if (!t) return '';
  const s = (t - Date.now()) / 1000;
  if (s <= 0) return 'expired';
  const [n, unit] = s < 3600 ? [Math.max(1, Math.floor(s / 60)), 'min'] : s < 86400 ? [Math.floor(s / 3600), 'h'] : [Math.round(s / 86400), 'day'];
  return `expires in ${n} ${unit}${unit === 'day' && n !== 1 ? 's' : ''}`;
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
// option values as the Parameters panel names them (the server's collab.VALUES)
const VALUE = { hobby_pwm: 'Hobby PWM', smart_bus: 'Smart bus', dynamixel: 'Dynamixel', modules: 'Off-the-shelf modules', custom_pcb: 'Custom PCB',
  prototype: 'Prototype', pilot: 'Pilot', production: 'Production', fsr402: 'FSR 402', esp32_devkit: 'ESP32 DevKit', esp32_s3_devkit: 'ESP32-S3 DevKit',
  arduino_nano: 'Arduino Nano', rp2040_pico: 'RP2040 Pico', v: 'V-groove', wrist_camera: 'wrist camera', estop: 'e-stop', imu: 'IMU' };
const money = (v) => (v == null ? '' : `$${Number(v).toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 })}`);
// a unit cost spreads the mold tooling (paid once) over the build quantity: say how much of it that is, as the inspector does
const toolingNote = (t, q) => (t >= 0.5 ? `incl. ${money(t)} tooling over ${Number(q || 1).toLocaleString('en-US')} unit${Number(q || 1) === 1 ? '' : 's'}` : '');
const andList = (n) => (n.length < 2 ? n.join('') : `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`);
// a change request being written: what it changes, its words, the form's open state, the parameter being picked, and the
// last preview (kept here, so a live update that redraws the tab never takes any of it away)
const blankDraft = () => ({ changes: {}, title: '', note: '', stage: '', open: false, param: '', value: null, preview: '' });

export function init(studio) {
  const team = new Team(studio);
  team.ready = team.start();
  return team;
}

export class Team {
  constructor(studio) {
    this.s = studio;
    this.esc = studio.esc || escH;
    this.me = null;
    this.tab = 'comments';
    this.data = { team: null, comments: [], proposals: [], reviews: [], gate: null, branches: [], feed: [], inbox: { items: [], unread: 0 } };
    this.anchor = { kind: 'rev', key: '' };
    this.draft = blankDraft();
    this.compose = '';  // a comment being written
    this.proj = studio.project?.() || null;
    this.showResolved = false;
  }

  // ---- api plumbing (studio.api may return parsed JSON or a fetch Response)
  async call(method, path, arg) {
    const a = this.s.api;
    let r;
    try {
      r = method === 'GET' ? await a.get(path, arg || {}) : await a.post(path, arg || {});
    } catch (e) {
      if (e?.status === 401 && this.me) this.signedOut();  // this browser's session ended (removed, or signed out everywhere)
      throw e;
    }
    if (r && typeof r.json === 'function') {
      const data = await r.json().catch(() => ({}));
      if (r.status === 401 && this.me) this.signedOut();
      if (!r.ok || data.ok === false) throw Object.assign(new Error(data.error || `The Studio answered ${r.status}`), { status: r.status, data });
      return data;
    }
    if (r && r.ok === false) throw Object.assign(new Error(r.error || 'The Studio refused that'), { data: r });
    return r || {};
  }
  // The session ended (the owner removed this member, or they signed out everywhere): the page stops being live and the
  // Studio's sign-in page says why.
  signedOut(reason) {
    if (this.gone) return;
    this.gone = true;
    clearInterval(this.beat);
    clearInterval(this.poll);
    const text = reason === 'removed' ? 'You were removed from this team, so this browser is signed out.'
      : 'This browser is signed out of the team.';
    const el = document.createElement('div');
    el.className = 'tm-gone';
    el.setAttribute('role', 'alert');
    el.innerHTML = `<div class="glass"><b>${this.esc(text)}</b><span>Opening the sign-in page…</span></div>`;
    document.body.appendChild(el);
    setTimeout(() => location.replace('/'), 1500);
  }
  toast(t) { (this.s.toast || console.log)(t); }
  rev() { return this.s.S?.current?.rev || null; }
  can(a) { return !!this.me?.can?.[a]; }
  member(id) { return (this.data.team?.members || []).concat(this.data.team?.former || []).find((m) => m.id === id) || null; }

  // ---- lifecycle
  async start() {
    let me;
    try { me = await this.call('GET', '/api/me'); } catch (e) { return false; }
    if (!me?.team) return false;
    this.me = me;
    injectCss();
    this.mountHeader();
    this.mountInspector();
    this.applyRole();
    this.subscribe();
    document.addEventListener('morph:comment', (e) => this.commentOn(e.detail || {}));
    this.beat = setInterval(() => this.heartbeat(), 60000);
    this.poll = setInterval(() => this.loadInbox(), 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) this.heartbeat(); });
    await Promise.all([this.loadTeam(), this.loadInbox(), this.heartbeat()]);
    await this.loadTab();
    return true;
  }
  subscribe() {
    const on = this.s.on?.bind(this.s);
    if (!on) return;
    const later = debounce(() => this.loadTab(true), 250);
    on('comment', () => { if (this.tab === 'comments') later(); else this.loadComments().then(() => this.renderPills()); });
    on('proposal', () => later());
    on('review', (ev) => { if (ev.gate && ev.gate.rev === this.rev()) this.data.gate = ev.gate; later(); });
    on('activity', () => { if (this.tab === 'activity') later(); });
    on('presence', (ev) => { this.presence = ev.members || []; this.renderPresence(); });
    on('inbox', (ev) => { if (!ev.to || ev.to === this.me.member.id) this.loadInbox(); });
    on('member', (ev) => {
      if (ev.member?.id === this.me.member.id && ev.action === 'revoked') { this.signedOut('removed'); return; }
      this.loadTeam();
      if (ev.member?.id === this.me.member.id) this.refreshMe();
    });
    on('signed_out', (ev) => this.signedOut(ev.reason));  // the Studio ended this member's event stream
    on('policy', () => this.refreshMe());
    on('done', () => later());
    on('project', () => {  // this tab switched projects (or Reset demo rebuilt this one): what was being written stays with its project
      const p = this.s.project?.() || null, same = p === this.proj;
      if (!same) { this.proj = p; this.draft = blankDraft(); this.compose = ''; }
      this.anchor = { kind: 'rev', key: '' };
      this.loadTeam().then(() => this.loadTab(same));
    });
  }
  async refreshMe() {
    try { this.me = await this.call('GET', '/api/me'); this.renderHeader(); this.applyRole(); this.loadTab(true); } catch (e) { /* keep the last view */ }
  }
  // Without edit rights (viewers) the page's editing tools are off, and the chat points at change requests instead.
  applyRole() {
    const ro = !this.can('edit');
    document.body.classList.toggle('tm-readonly', ro);
    document.body.classList.toggle('tm-nokeys', !this.can('keys'));  // API keys and settings are the owners' (their dialog would only refuse)
    const input = document.querySelector('#input');
    if (input) {
      if (this.placeholder == null) this.placeholder = input.placeholder;
      input.disabled = ro;
      input.placeholder = ro ? (this.can('propose') ? 'View only: propose changes in Changes' : 'View only') : this.placeholder;
    }
    this.titles = this.titles || {};
    for (const sel of ['#send', '#paramsApply']) {
      const b = document.querySelector(sel);
      if (!b) continue;
      if (!(sel in this.titles)) this.titles[sel] = b.title;
      b.disabled = ro;
      b.title = ro ? 'Viewers propose changes in Changes on the right; an editor applies them' : this.titles[sel];
    }
  }
  async heartbeat() {
    try {
      const r = await this.call('POST', '/api/presence', { tab: this.s.S?.tab || null, rev: this.rev() });
      this.presence = r.members || [];
      this.renderPresence();
    } catch (e) { /* presence is best effort */ }
  }

  // ---- header: presence avatars, inbox, share
  mountHeader() {
    const slot = this.s.mount?.header || document.querySelector('#badges') || document.querySelector('header');
    this.hdr = document.createElement('div');
    this.hdr.className = 'tm-hdr';
    slot.appendChild(this.hdr);
    this.renderHeader();
    document.addEventListener('click', (e) => {
      if (this.pop && !e.target.closest('.tm-pop') && !e.target.closest('[data-tm="inbox"]')) this.closeInbox();
    });
  }
  renderHeader() {
    const m = this.me.member, esc = this.esc;
    this.hdr.innerHTML = `<div class="tm-avs" aria-label="Who's here"></div>
      <span class="tm-role" title="${esc(ROLE_HELP[m.role] || '')}">${esc(m.role)}</span>
      <button class="btn icon tm-bell" type="button" data-tm="inbox" title="Inbox: mentions, review requests, decisions, your jobs">${icon('bell')}<b class="tm-badge"></b></button>
      <button class="btn tm-share" type="button" data-tm="share" title="${this.can('admin') ? 'Invite people and manage the team' : 'Who is on the team, and your devices'}">${icon('users')}${this.can('admin') ? 'Share' : 'Team'}</button>`;
    this.hdr.querySelector('[data-tm="share"]').onclick = () => this.openShare();
    this.hdr.querySelector('[data-tm="inbox"]').onclick = () => (this.pop ? this.closeInbox() : this.openInbox());
    this.renderPresence();
    this.renderBadge();
  }
  renderPresence() {
    const box = this.hdr?.querySelector('.tm-avs');
    if (!box) return;
    const esc = this.esc, list = (this.presence || []).slice(0, 5), more = (this.presence || []).length - list.length;
    box.innerHTML = list.map((p) => {
      const m = this.member(p.id) || p;
      const tags = (m.disciplines || []).map((d) => this.disc(d)).join(', ');
      const where = [p.tab ? `on ${p.tab}` : '', p.rev ? `viewing ${p.rev}` : ''].filter(Boolean).join(', ');
      return `<span class="tm-av ${p.state === 'idle' ? 'idle' : ''}${p.id === this.me.member.id ? ' you' : ''}" style="--c:${color(m.color)}"
        title="${esc(m.name)}${p.id === this.me.member.id ? ' (you)' : ''}${tags ? ' · ' + esc(tags) : ''}${where ? ' · ' + esc(where) : ''} · ${p.state}">${esc(initials(m.name))}</span>`;
    }).join('') + (more > 0 ? `<span class="tm-av more">+${more}</span>` : '');
  }
  renderBadge() {
    const b = this.hdr?.querySelector('.tm-badge');
    if (!b) return;
    const n = this.data.inbox.unread || 0;
    b.textContent = n > 99 ? '99+' : n ? String(n) : '';
    b.classList.toggle('on', n > 0);
    const dot = this.ins?.querySelector('[data-tab="activity"] .tm-n');
    if (dot) dot.textContent = n ? String(n) : '';
  }
  disc(d) { return (this.me.disciplines || []).find((x) => x.key === d)?.label?.replace(' (all stages)', '') || d; }

  async loadInbox() {
    try { this.data.inbox = await this.call('GET', '/api/inbox', { limit: 30 }); } catch (e) { return; }
    this.renderBadge();
    if (this.pop) this.renderInbox();
    if (this.tab === 'activity') this.renderBody();
  }
  openInbox() {
    this.pop = document.createElement('div');
    this.pop.className = 'menu glass on tm-pop';
    this.hdr.appendChild(this.pop);
    this.renderInbox();
  }
  closeInbox() { this.pop?.remove(); this.pop = null; }
  renderInbox() {
    const esc = this.esc, box = this.data.inbox;
    this.pop.innerHTML = `<div class="mh">Inbox${box.unread ? ` · ${box.unread} new` : ''}</div>
      ${(box.items || []).length ? box.items.slice(0, 12).map((i) => `<div class="tm-note ${i.unread ? 'new' : ''}">
        <span class="tm-k">${esc(i.kind.replace('_', ' '))}</span><span>${esc(i.text)}</span><small>${esc(ago(i.ts))}${i.project ? ' · ' + esc(i.project) : ''}</small></div>`).join('')
        : '<div class="tm-empty">Nothing yet. Mentions, review requests, decisions on your changes and your finished jobs land here.</div>'}
      <div class="tm-row"><span class="grow"></span>${box.unread ? '<button class="btn" type="button" data-a="read">Mark all read</button>' : ''}</div>`;
    const b = this.pop.querySelector('[data-a="read"]');
    if (b) b.onclick = async () => { try { await this.call('POST', '/api/inbox/read', {}); } catch (e) { this.toast(e.message); } this.loadInbox(); };
  }

  // ---- Share dialog
  async loadTeam() {
    try { this.data.team = await this.call('GET', '/api/team'); } catch (e) { this.toast(`Couldn't load the team (${e.message})`); }
    if (this.shareEl?.isConnected) this.renderShare();
    this.renderPresence();
  }
  openShare() {
    this.shareEl = document.createElement('div');
    this.shareEl.className = 'glass dlg tm-share-dlg';
    this.shareEl.setAttribute('role', 'dialog');
    this.shareEl.setAttribute('aria-label', 'Share');
    this.renderShare();
    if (this.s.modal?.open) this.s.modal.open(this.shareEl, { wide: true, onClose: () => { this.lastInvite = null; this.inviteDraft = null; } });
    else {
      this.ownModal = document.createElement('div');
      this.ownModal.className = 'modal on';
      this.ownModal.appendChild(this.shareEl);
      this.ownModal.addEventListener('click', (e) => { if (e.target === this.ownModal) this.closeShare(); });
      const esc = (e) => { if (e.key === 'Escape') { this.closeShare(); document.removeEventListener('keydown', esc); } };
      document.addEventListener('keydown', esc);
      document.body.appendChild(this.ownModal);
    }
    this.loadTeam();
  }
  closeShare() {
    if (this.s.modal?.close) this.s.modal.close();
    this.ownModal?.remove();
    this.ownModal = null;
    this.lastInvite = null;
    this.inviteDraft = null;
  }
  // A teammate joining redraws the dialog: the invite being written lives in state (inviteDraft), and the field being
  // typed in keeps its focus, so a live update never takes what the owner is typing.
  renderShare() {
    const a = document.activeElement, focus = this.shareEl?.contains(a) ? (a.name || '') : '';
    const caret = focus && typeof a.selectionStart === 'number' ? a.selectionStart : null;
    this.shareHtml();
    const f = focus && this.shareEl.querySelector(`[name="${focus}"]`);
    if (f) { f.focus(); if (caret != null && f.setSelectionRange) f.setSelectionRange(caret, caret); }
  }
  shareHtml() {
    const esc = this.esc, t = this.data.team || {}, owner = this.can('admin'), me = this.me.member;
    const draft = this.inviteDraft = this.inviteDraft || { role: 'editor', hint: '', tags: [] };
    const discs = this.me.disciplines || [];
    const chips = (name, sel) => discs.map((d) => `<label class="tm-chk"><input type="checkbox" name="${name}" value="${esc(d.key)}"${sel.includes(d.key) ? ' checked' : ''}>
      <span title="${esc(d.stages.join(', ') || 'Fallback owner of every stage without a specialist')}">${esc(d.label)}</span></label>`).join('');
    const roleSel = (v, attrs = '') => `<span class="sel"><select ${attrs}>${['viewer', 'editor', 'owner'].map((r) => `<option value="${r}"${r === v ? ' selected' : ''}>${r[0].toUpperCase() + r.slice(1)}</option>`).join('')}</select></span>`;
    const member = (m) => `<div class="tm-mem" data-id="${esc(m.id)}">
        <span class="tm-av" style="--c:${color(m.color)}">${esc(initials(m.name))}</span>
        <div class="tm-who"><b>${esc(m.name)}${m.id === me.id ? ' <span class="dim">(you)</span>' : ''}</b>
          <div class="tm-tags">${(m.disciplines || []).map((d) => `<span class="tm-disc d-${esc(d)}">${esc(this.disc(d))}</span>`).join('') || '<span class="dim">no discipline</span>'}</div></div>
        ${owner && m.id !== me.id ? `${roleSel(m.role, 'data-a="role"')}<button class="btn ghost" type="button" data-a="tags">Disciplines</button><button class="btn ghost tm-danger" type="button" data-a="revoke">Remove</button>`
          : `<span class="tag neutral">${esc(m.role)}</span>`}
        <div class="tm-edit"></div></div>`;
    const invites = (t.invites || []).filter((i) => i.state === 'pending');
    this.shareEl.innerHTML = `
      <div class="dlg-h"><div class="kicon">${icon('users', '')}</div><div class="grow"><div class="t">${owner ? 'Share' : 'Team'} ${esc(t.team?.name || this.me.team_name || '')}</div>
        <div class="s">Morph Teams is a preview that runs on the team owner's computer: designs, comments and approvals stay there.
        Use a VPN or a tunnel between computers; without HTTPS the links travel as plain HTTP.</div></div>
        <button class="btn icon ghost" type="button" data-a="close" title="Close (Esc)">${icon('x')}</button></div>
      ${owner ? `<section class="ksec"><div class="ks-h"><b>Invite someone</b><span class="tag neutral">works once, for 7 days</span></div>
        <div class="tm-form">
          <label class="tm-f"><span>Role</span>${roleSel(draft.role, 'name="role"')}</label>
          <label class="tm-f grow"><span>Name (shown on the join page)</span><input name="hint" maxlength="40" placeholder="e.g. Ana" autocomplete="off" value="${esc(draft.hint)}"></label>
        </div>
        <div class="tm-roleh" data-roleh>${esc(ROLE_HELP[draft.role] || '')}</div>
        <div class="tm-f"><span>Disciplines (they review changes to these stages)</span><div class="tm-chks">${chips('inv', draft.tags)}</div></div>
        <div class="tm-row"><button class="btn primary" type="button" data-a="invite">${icon('link')}Create invite link</button><span class="grow"></span></div>
        <div class="tm-out" data-out="invite"></div></section>` : ''}
      <section class="ksec"><div class="ks-h"><b>Members</b><span class="tag neutral">${(t.members || []).length}</span></div>
        <div class="tm-mems">${(t.members || []).map(member).join('')}</div>
        ${owner && invites.length ? `<div class="sec-t" style="margin-top:12px">Pending invites</div>${invites.map((i) => `<div class="tm-inv">
          <span>${esc(i.name_hint || 'Anyone with the link')}</span><span class="tag neutral">${esc(i.role)}</span>
          <span class="dim">${esc((i.disciplines || []).map((d) => this.disc(d)).join(', ') || 'no discipline')} · ${esc(expiry(i.expires_utc) || `expires ${i.expires_utc?.slice(0, 10)}`)}</span>
          <span class="grow"></span><button class="btn ghost" type="button" data-cancel="${esc(i.id)}">Cancel</button></div>`).join('')}` : ''}</section>
      ${owner ? `<section class="ksec"><div class="ks-h"><b>Policy</b></div>${this.policyHtml(t.policy || this.me.policy || {})}</section>` : ''}
      <section class="ksec"><div class="ks-h"><b>You</b><span class="tag acc">${esc(me.role)}</span></div>
        <label class="tm-sw"><span class="switch"><input type="checkbox" data-a="watch"${me.watch ? ' checked' : ''}><i></i></span>
          <span><b>Watch all changes</b><small>Every change request and review lands in your inbox, without making you a required reviewer. On by default for generalists.</small></span></label>
        <div class="tm-row" style="margin-top:10px"><button class="btn" type="button" data-a="device">${icon('link')}Sign in on another device</button>
          <button class="btn" type="button" data-a="token">${icon('key')}New script token</button><span class="grow"></span></div>
        <div class="tm-out" data-out="you"></div>
        ${(this.me.tokens || []).length ? `<div class="tm-toks">${this.me.tokens.map((k) => `<div class="tm-inv"><span class="mono">${esc(k.label)}</span><span class="dim">created ${esc(ago(k.created_utc))}</span><span class="grow"></span><button class="btn ghost" type="button" data-tok="${esc(k.id)}">Revoke</button></div>`).join('')}</div>` : ''}</section>
      <div class="dlg-f"><span class="dim">An invite link grants its role until it's used, expires or is cancelled. Approvals help your process; they don't certify the robot as safe.</span><span class="grow"></span><button class="btn" type="button" data-a="close">Close</button></div>`;
    this.wireShare();
  }
  policyHtml(p) {
    const esc = this.esc;
    const sw = (k, title, note) => `<label class="tm-sw"><span class="switch"><input type="checkbox" data-pol="${k}"${p[k] ? ' checked' : ''}><i></i></span><span><b>${esc(title)}</b><small>${esc(note)}</small></span></label>`;
    const req = p.specialist_required || {};
    const specs = (lvl) => (this.me.disciplines || []).filter((d) => d.key !== 'generalist').map((d) => `<label class="tm-chk"><input type="checkbox" data-spec="${lvl}" value="${esc(d.key)}"${(req[lvl] || []).includes(d.key) ? ' checked' : ''}><span>${esc(d.label)}</span></label>`).join('');
    return `${sw('viewers_can_comment', 'Viewers can comment', 'Comments and @mentions from people with view access.')}
      ${sw('viewers_can_propose', 'Viewers can propose changes', 'They open change requests; an editor applies them.')}
      ${sw('viewers_can_approve', 'Viewers can approve their disciplines', 'For reviewers who never edit, like a procurement lead.')}
      <label class="tm-sw"><span class="switch"><input type="checkbox" data-pol="unreleased_downloads"${p.unreleased_downloads !== 'owner' ? ' checked' : ''}><i></i></span>
        <span><b>Editors can download draft order files</b><small>Gerbers, JLC files and manufacturing zips of a revision that isn't released yet. Logged and marked draft; owners always can.</small></span></label>
      <div class="tm-f" style="margin-top:10px"><span>Specialist approval required for pilot builds (a generalist's approval doesn't count)</span><div class="tm-chks">${specs('pilot')}</div></div>
      <div class="tm-f"><span>Specialist approval required for production builds</span><div class="tm-chks">${specs('production')}</div></div>`;
  }
  wireShare() {
    const el = this.shareEl, $ = (s) => el.querySelector(s);
    el.querySelectorAll('[data-a="close"]').forEach((b) => (b.onclick = () => this.closeShare()));
    const role = el.querySelector('select[name="role"]'), draft = this.inviteDraft;
    if (role) role.onchange = () => { draft.role = role.value; $('[data-roleh]').textContent = ROLE_HELP[role.value]; };
    const hintIn = $('input[name="hint"]');
    if (hintIn) hintIn.oninput = () => { draft.hint = hintIn.value; };
    el.querySelectorAll('input[name="inv"]').forEach((c) => (c.onchange = () => {
      draft.tags = [...el.querySelectorAll('input[name="inv"]:checked')].map((x) => x.value);
    }));
    const inv = $('[data-a="invite"]');
    if (inv) inv.onclick = async () => {
      const tags = [...el.querySelectorAll('input[name="inv"]:checked')].map((x) => x.value);
      inv.disabled = true;
      try {
        const hint = $('input[name="hint"]').value.trim();
        const r = await this.call('POST', '/api/team/invites', { role: role.value, disciplines: tags, name_hint: hint });
        // a Studio others reach over the network, opened here at 127.0.0.1: that address works on this computer only
        const local = this.s.S?.compute?.remote && /^(127\.|localhost$|\[::1\]$)/.test(location.hostname);
        const who = hint || 'the person you send it to';
        this.lastInvite = [location.origin + r.path,
          `For ${who}: ${r.invite.role}${tags.length ? `, ${tags.map((d) => this.disc(d)).join(', ')}` : ''}. Works once, until ${new Date(r.invite.expires_utc).toLocaleString()}. Send it privately: whoever opens it first joins.`
          + (local ? ` This link starts with ${location.host}, which only works on this computer: open the Studio at the address teammates use (the one \`morph studio --team --remote\` prints, or your tunnel's) and make the invite there.` : '')];
        this.data.team = await this.call('GET', '/api/team');
        this.inviteDraft = null;
        this.renderShare();  // the new invite shows under Pending invites; the form is ready for the next person
      } catch (e) { this.toast(e.message); inv.disabled = false; }
    };
    if (this.lastInvite) this.showLink($('[data-out="invite"]'), ...this.lastInvite);
    el.querySelectorAll('.tm-mem').forEach((row) => {
      const id = row.dataset.id, m = (this.data.team?.members || []).find((x) => x.id === id);
      const sel = row.querySelector('[data-a="role"]');
      if (sel) sel.onchange = () => this.updateMember(id, { role: sel.value });
      const tb = row.querySelector('[data-a="tags"]');
      if (tb) tb.onclick = () => {
        const box = row.querySelector('.tm-edit');
        if (box.innerHTML) { box.innerHTML = ''; return; }
        box.innerHTML = `<div class="tm-chks">${(this.me.disciplines || []).map((d) => `<label class="tm-chk"><input type="checkbox" value="${this.esc(d.key)}"${(m.disciplines || []).includes(d.key) ? ' checked' : ''}><span>${this.esc(d.label)}</span></label>`).join('')}</div>
          <div class="tm-row"><button class="btn primary" type="button">Save disciplines</button></div>`;
        box.querySelector('button').onclick = () => this.updateMember(id, { disciplines: [...box.querySelectorAll('input:checked')].map((x) => x.value) });
      };
      const rv = row.querySelector('[data-a="revoke"]');
      if (rv) rv.onclick = async () => {
        if (rv.dataset.sure !== '1') { rv.dataset.sure = '1'; rv.textContent = `Remove ${m.name}?`; return; }
        try { await this.call('POST', '/api/team/revoke', { member: id }); this.toast(`${m.name} is signed out everywhere. Their name stays on past work.`); } catch (e) { this.toast(e.message); }
        this.loadTeam();
      };
    });
    el.querySelectorAll('[data-cancel]').forEach((b) => (b.onclick = async () => {
      try { await this.call('POST', '/api/team/revoke', { invite: b.dataset.cancel }); } catch (e) { this.toast(e.message); }
      this.loadTeam();
    }));
    el.querySelectorAll('[data-pol]').forEach((x) => (x.onchange = () => {
      const k = x.dataset.pol;
      this.setPolicy({ [k]: k === 'unreleased_downloads' ? (x.checked ? 'warn' : 'owner') : x.checked });
    }));
    el.querySelectorAll('[data-spec]').forEach((x) => (x.onchange = () => {
      const req = { pilot: [], production: [] };
      el.querySelectorAll('[data-spec]:checked').forEach((c) => req[c.dataset.spec].push(c.value));
      this.setPolicy({ specialist_required: req });
    }));
    const w = $('[data-a="watch"]');
    if (w) w.onchange = async () => {
      try { const r = await this.call('POST', '/api/me', { watch: w.checked }); this.me.member = r.member; } catch (e) { this.toast(e.message); }
    };
    const dev = $('[data-a="device"]');
    if (dev) dev.onclick = async () => {
      try {
        const r = await this.call('POST', '/api/me/link', {});
        this.showLink($('[data-out="you"]'), location.origin + r.path, 'Open it on your other device within 15 minutes. It works once and signs that browser in as you.');
      } catch (e) { this.toast(e.message); }
    };
    const tok = $('[data-a="token"]');
    if (tok) tok.onclick = async () => {
      try {
        const r = await this.call('POST', '/api/me/tokens', { label: 'script' });
        this.me.tokens = r.tokens;
        this.showLink($('[data-out="you"]'), r.token, `${r.note} It has your role (${this.me.member.role}).`);
      } catch (e) { this.toast(e.message); }
    };
    el.querySelectorAll('[data-tok]').forEach((b) => (b.onclick = async () => {
      try { const r = await this.call('POST', '/api/me/tokens', { action: 'revoke', id: b.dataset.tok }); this.me.tokens = r.tokens; this.renderShare(); } catch (e) { this.toast(e.message); }
    }));
  }
  showLink(box, text, note) {
    box.innerHTML = `<div class="kin"><input readonly value="${this.esc(text)}" aria-label="Link"><button class="btn" type="button">${icon('copy')}Copy</button></div><div class="kh">${this.esc(note)}</div>`;
    const inp = box.querySelector('input');
    inp.focus();
    inp.select();
    box.querySelector('button').onclick = async () => {
      try { await navigator.clipboard.writeText(text); this.toast('Copied'); } catch (e) { inp.select(); document.execCommand?.('copy'); this.toast('Copied'); }
    };
  }
  async updateMember(id, ch) {
    try { await this.call('POST', '/api/team/members', { id, ...ch }); this.toast('Saved'); } catch (e) { this.toast(e.message); }
    this.loadTeam();
  }
  async setPolicy(values) {
    try { const r = await this.call('POST', '/api/team/policy', { values }); this.me.policy = r.policy; if (this.data.team) this.data.team.policy = r.policy; this.toast('Policy saved'); } catch (e) { this.toast(e.message); }
  }

  // ---- inspector: Comments | Review | Changes | Activity
  mountInspector() {
    const slot = this.s.mount?.inspectorTabs || document.querySelector('#insp');
    this.ins = document.createElement('section');
    this.ins.className = 'tm-insp';
    this.ins.innerHTML = `<div class="tm-tabs pills" role="tablist">
        <button type="button" data-tab="comments" class="on" title="Comments on this revision, its stages, BOM lines and parameters">Comments<span class="tm-n"></span></button>
        <button type="button" data-tab="review" title="Approvals and the release gate for this revision">Review<span class="tm-n"></span></button>
        <button type="button" data-tab="changes" title="Change requests: proposed edits routed to the owners of what they touch">Changes<span class="tm-n"></span></button>
        <button type="button" data-tab="activity" title="Your inbox and the project's activity">Activity<span class="tm-n"></span></button></div>
      <div class="tm-body"></div>`;
    slot.appendChild(this.ins);
    this.ins.querySelectorAll('[data-tab]').forEach((b) => (b.onclick = () => this.show(b.dataset.tab)));
    this.body = this.ins.querySelector('.tm-body');
  }
  show(tab) {
    this.tab = tab;
    if (window.matchMedia?.('(max-width: 1280px)').matches) {  // a narrow window folds the right panel away: open it
      document.body.classList.add('insp-open');
      document.querySelector('#inspBtn')?.setAttribute('aria-pressed', 'true');
    }
    this.ins.querySelectorAll('[data-tab]').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
    this.renderBody();
    this.loadTab();
  }
  async loadTab(live = false) {
    const t = this.tab;
    try {
      if (t === 'comments') await this.loadComments();
      else if (t === 'review') await Promise.all([this.loadGate(), this.loadBranches()]);
      else if (t === 'changes') await this.loadProposals();
      else if (t === 'activity') await Promise.all([this.loadFeed(), this.loadInbox()]);
    } catch (e) { /* each loader reports its own error */ }
    if (t !== this.tab) return;
    this.renderPills();
    if (live && this.typing()) { this.renderSoon(); return; }  // a teammate's activity never interrupts what you are writing
    this.renderBody(live);
  }
  // The person is in a field of this panel, or has written into a box that only lives on the page (a reply, a request for
  // changes, a release or branch name): a live update waits for them. Drafts, comments and the form are kept in state.
  typing() {
    const b = this.body, a = document.activeElement;
    if (!b) return false;
    if (a && b.contains(a) && a.matches('input, textarea, select')) return true;
    return [...b.querySelectorAll('.tm-ask textarea, .tm-replybox textarea, .tm-relbox input, .tm-branchbox input')].some((x) => x.value.trim());
  }
  renderSoon() {
    clearTimeout(this.soon);
    this.soon = setTimeout(() => { if (this.typing()) this.renderSoon(); else this.renderBody(true); }, 1000);
  }
  renderPills() {
    const set = (tab, v, cls = '') => { const n = this.ins.querySelector(`[data-tab="${tab}"] .tm-n`); if (n) { n.textContent = v || ''; n.className = `tm-n ${cls}`; } };
    set('comments', this.data.comments.filter((c) => !c.parent && !c.resolved).length || '');
    set('changes', this.data.proposals.filter((p) => p.status === 'open').length || '');
    const g = this.data.gate;
    set('review', g ? (g.ok ? 'ok' : g.missing?.length || '!') : '', g ? (g.ok ? 'good' : 'warn') : '');
    set('activity', this.data.inbox.unread || '');
  }
  renderBody(live = false) {
    clearTimeout(this.soon);
    const form = live && this.body.querySelector('.tm-new');
    if (form) this.draft.open = form.open;  // a click's toggle event comes a moment later: a live redraw keeps the form as it is
    const r = { comments: () => this.commentsHtml(), review: () => this.reviewHtml(), changes: () => this.changesHtml(), activity: () => this.activityHtml() }[this.tab];
    this.body.innerHTML = r();
    ({ comments: () => this.wireComments(), review: () => this.wireReview(), changes: () => this.wireChanges(), activity: () => this.wireActivity() }[this.tab])();
  }

  // ---- comments
  async loadComments() {
    const rev = this.rev();
    if (!rev) { this.data.comments = []; return; }
    try { this.data.comments = (await this.call('GET', '/api/comments', { rev })).comments || []; } catch (e) { this.data.comments = []; this.toast(`Comments: ${e.message}`); }
    this.data.commentsRev = rev;  // which revision they belong to: carried and outdated depend on it
  }
  anchors() {
    const cur = this.s.S?.current || {}, esc = this.esc;
    const nodes = cur.nodes || NODES;
    const rows = (cur.bom?.rows || []).slice(0, 80);
    const params = (cur.params || []).slice(0, 60);
    const opt = (v, label) => `<option value="${esc(v)}">${esc(label)}</option>`;
    return `<option value="rev:">This revision (${esc(cur.rev || '')})</option>
      <optgroup label="Stage">${nodes.map(([k, l]) => opt(`stage:${k}`, l)).join('')}</optgroup>
      ${rows.length ? `<optgroup label="BOM line">${rows.map((r, i) => opt(`bom:${i}`, `${r.item}`.slice(0, 70))).join('')}</optgroup>` : ''}
      ${params.length ? `<optgroup label="Parameter">${params.map((p) => opt(`param:${p.key}`, `${p.label}${p.value != null ? ' = ' + p.value + (p.unit ? ' ' + p.unit : '') : ''}`)).join('')}</optgroup>` : ''}`;
  }
  anchorFor(v) {
    const [kind, key] = [v.split(':')[0], v.slice(v.indexOf(':') + 1)];
    if (kind === 'bom') {
      const r = (this.s.S?.current?.bom?.rows || [])[+key];
      return r ? { kind: 'bom', item: r.item, category: r.category } : { kind: 'rev' };
    }
    return kind === 'rev' ? { kind: 'rev' } : { kind, key };
  }
  commentOn(d) {
    this.anchor = { kind: d.kind || 'rev', key: d.key || '', item: d.item, category: d.category };
    this.show('comments');
    setTimeout(() => this.body.querySelector('textarea')?.focus(), 30);
  }
  commentsHtml() {
    const esc = this.esc, rev = this.rev();
    if (!rev) return '<div class="tm-empty">No revision yet.</div>';
    const fresh = this.data.commentsRev === rev;  // a new revision's comments are on their way: not the last one's, mislabelled
    const all = fresh ? this.data.comments : [], roots = all.filter((c) => !c.parent && (this.showResolved || !c.resolved));
    const resolvedN = all.filter((c) => !c.parent && c.resolved).length;
    const canPost = this.can('comment');
    const thread = (c) => {
      const replies = all.filter((x) => x.parent === c.id);
      const a = c.anchor || {};
      const mayResolve = c.by === this.me.member.id || this.can('edit');
      return `<div class="tm-th${c.resolved ? ' resolved' : ''}" data-id="${esc(c.id)}">
        <div class="tm-anc"><span class="tm-kind">${esc(KIND[a.kind] || a.kind)}</span><span class="tm-al">${esc(a.kind === 'rev' ? a.rev : a.label || a.key)}</span>
          ${c.carried ? `<span class="tag neutral" title="Made on ${esc(a.rev)}; it follows later revisions while this ${esc((KIND[a.kind] || '').toLowerCase())} exists">from ${esc(a.rev)}</span>` : ''}
          ${c.outdated ? '<span class="tag warn" title="It changed since the comment was made">changed since</span>' : ''}
          ${c.resolved ? '<span class="tag good">resolved</span>' : ''}</div>
        ${[c, ...replies].map((x) => this.msgHtml(x)).join('')}
        <div class="tm-acts">${canPost ? `<button class="btn ghost" type="button" data-reply="${esc(c.id)}">${icon('reply')}Reply</button>` : ''}
          ${mayResolve ? `<button class="btn ghost" type="button" data-resolve="${esc(c.id)}" data-v="${c.resolved ? '0' : '1'}">${c.resolved ? 'Reopen' : `${icon('check')}Resolve`}</button>` : ''}</div>
        <div class="tm-replybox"></div></div>`;
    };
    const sel = this.anchor.kind === 'bom' ? `bom:${(this.s.S?.current?.bom?.rows || []).findIndex((r) => r.item === this.anchor.item)}` : `${this.anchor.kind}:${this.anchor.kind === 'rev' ? '' : this.anchor.key}`;
    return `<div class="tm-sec">
      ${canPost ? `<div class="tm-compose"><span class="sel"><select data-a="anchor" aria-label="What the comment is about">${this.anchors()}</select></span>
        <textarea rows="2" maxlength="4000" placeholder="Comment on ${esc(rev)}. @name to notify someone">${esc(this.compose)}</textarea>
        <div class="tm-mention"></div>
        <div class="tm-row"><span class="dim tm-hint">Stage, BOM and parameter comments follow later revisions.</span><span class="grow"></span><button class="btn primary" type="button" data-a="post">Comment</button></div></div>`
        : '<div class="tm-empty">Viewers can\'t comment on this team (owner policy).</div>'}
      ${this.can('admin') && (this.data.team?.members || []).length === 1 ? `<div class="tm-hintbox">${icon('users')}<span>You're the only one on the team so far.
        <b>Share</b> (top right) makes invite links: teammates sign in from their own browsers, see this robot live, comment and review.</span></div>` : ''}
      <div class="tm-row tm-filter"><b>${fresh ? `${roots.length} thread${roots.length === 1 ? '' : 's'} on ${esc(rev)}` : `Comments on ${esc(rev)}`}</b><span class="grow"></span>
        ${resolvedN ? `<button class="btn ghost" type="button" data-a="resolved">${this.showResolved ? 'Hide' : 'Show'} ${resolvedN} resolved</button>` : ''}</div>
      ${roots.length ? roots.map(thread).join('') : `<div class="tm-empty">${fresh ? 'No open comments here yet.' : `Loading the comments on ${esc(rev)}…`}</div>`}
      ${sel ? `<input type="hidden" data-sel="${esc(sel)}">` : ''}</div>`;
  }
  msgHtml(c) {
    const esc = this.esc, a = c.author || this.member(c.by) || { name: '?' };
    const body = esc(c.body).replace(/(^|\s)@([\w.-]{1,40})/gu, '$1<b class="tm-at">@$2</b>');
    return `<div class="tm-msg"><span class="tm-av sm" style="--c:${color(a.color)}">${esc(initials(a.name))}</span>
      <div><div class="tm-meta"><b>${esc(a.name)}</b><span>${esc(ago(c.ts))}</span></div><div class="tm-text">${body}</div></div></div>`;
  }
  wireComments() {
    const b = this.body;
    const sel = b.querySelector('[data-a="anchor"]');
    const want = b.querySelector('[data-sel]')?.dataset.sel;
    if (sel && want && [...sel.options].some((o) => o.value === want)) sel.value = want;
    if (sel) sel.onchange = () => { this.anchor = { ...this.anchorFor(sel.value) }; };
    const ta = b.querySelector('.tm-compose textarea');
    if (ta) {
      ta.addEventListener('input', () => { this.compose = ta.value; });
      this.mentions(ta, b.querySelector('.tm-mention'));
    }
    const post = b.querySelector('[data-a="post"]');
    if (post) post.onclick = async () => {
      const body = ta.value.trim();
      if (!body) { ta.focus(); return; }
      post.disabled = true;
      try {
        await this.call('POST', '/api/comments', { body, anchor: { ...this.anchorFor(sel.value), rev: this.rev() } });
        ta.value = '';
        this.compose = '';
        await this.loadComments();
        this.renderBody();
        this.renderPills();
      } catch (e) { this.toast(e.message); post.disabled = false; }
    };
    const res = b.querySelector('[data-a="resolved"]');
    if (res) res.onclick = () => { this.showResolved = !this.showResolved; this.renderBody(); };
    b.querySelectorAll('[data-resolve]').forEach((x) => (x.onclick = async () => {
      try { await this.call('POST', '/api/comments/resolve', { id: x.dataset.resolve, resolved: x.dataset.v === '1' }); } catch (e) { this.toast(e.message); }
      await this.loadComments();
      this.renderBody();
      this.renderPills();
    }));
    b.querySelectorAll('[data-reply]').forEach((x) => (x.onclick = () => {
      const box = x.closest('.tm-th').querySelector('.tm-replybox');
      if (box.innerHTML) { box.innerHTML = ''; return; }
      box.innerHTML = `<textarea rows="2" maxlength="4000" placeholder="Reply"></textarea><div class="tm-mention"></div><div class="tm-row"><span class="grow"></span><button class="btn primary" type="button">Reply</button></div>`;
      const t = box.querySelector('textarea');
      this.mentions(t, box.querySelector('.tm-mention'));
      t.focus();
      box.querySelector('button').onclick = async () => {
        if (!t.value.trim()) return;
        try { await this.call('POST', '/api/comments', { body: t.value.trim(), parent: x.dataset.reply }); } catch (e) { this.toast(e.message); return; }
        await this.loadComments();
        this.renderBody();
      };
    }));
  }
  mentions(ta, box) {
    const names = () => (this.data.team?.members || []).filter((m) => m.id !== this.me.member.id).map((m) => m.name);
    ta.addEventListener('input', () => {
      const m = /(^|\s)@([\w.-]*)$/u.exec(ta.value.slice(0, ta.selectionStart));
      if (!m) { box.innerHTML = ''; return; }
      const q = m[2].toLowerCase(), hits = names().filter((n) => n.replace(/\s+/g, '').toLowerCase().startsWith(q)).slice(0, 5);
      box.innerHTML = hits.map((n) => `<button type="button" class="tm-chip">@${this.esc(n)}</button>`).join('');
      box.querySelectorAll('button').forEach((bt, i) => (bt.onclick = () => {
        const pos = ta.selectionStart, before = ta.value.slice(0, pos).replace(/@([\w.-]*)$/u, '@' + hits[i].replace(/\s+/g, '') + ' ');
        ta.value = before + ta.value.slice(pos);
        box.innerHTML = '';
        ta.focus();
        ta.selectionStart = ta.selectionEnd = before.length;
      }));
    });
  }

  // ---- review: the gate, reviews of this revision, branches, release
  async loadGate() {
    const rev = this.rev();
    if (!rev) { this.data.gate = null; return; }
    try {
      const r = await this.call('GET', '/api/reviews', { rev });
      this.data.reviews = r.reviews || [];
      this.data.gate = r.gate || null;
      const rel = await this.call('GET', '/api/releases', {});
      this.data.releases = rel.releases || [];
    } catch (e) { this.toast(`Review: ${e.message}`); }
  }
  async loadBranches() {
    try { const r = await this.call('GET', '/api/branches', {}); this.data.branches = r.branches || []; } catch (e) { this.data.branches = []; }
  }
  myCover(review, level) {
    // which of the review's disciplines my approval covers: the server says (review.mine, the same check it makes when I
    // decide); without it, an approximation that only labels the button
    if (review?.mine) return review.mine.covers || [];
    const m = this.me.member, tags = m.disciplines || [];
    if (!review || (m.role === 'viewer' && !this.me.policy?.viewers_can_approve)) return [];
    const req = (this.me.policy?.specialist_required || {})[level] || [];
    const members = this.data.team?.members || [];
    const hasSpec = (d) => members.some((x) => (x.disciplines || []).includes(d) && !(review.authors || []).includes(x.id));
    const hasGen = members.some((x) => (x.disciplines || []).includes('generalist') && !(review.authors || []).includes(x.id));
    return (review.disciplines || []).filter((d) => tags.includes(d) || (!req.includes(d) && tags.includes('generalist'))
      || (m.role === 'owner' && !hasSpec(d) && !hasGen));
  }
  approveLabel(ds) {
    const n = ds.map((d) => this.disc(d));
    if (!n.length) return 'Approve';
    if (n.length > 3) return `Approve as ${n.slice(0, 2).join(', ')} and ${n.length - 2} more`;
    return `Approve as ${n.length === 1 ? n[0] : n.slice(0, -1).join(', ') + ' and ' + n[n.length - 1]}`;
  }
  approveTitle(ds) { return `Your approval counts for ${ds.map((d) => this.disc(d)).join(', ')}`; }
  myDecision(review, ds) {
    // my latest decision when it is the same for every discipline I cover in this review, else null
    const last = {};
    for (const d of review?.decisions || []) if (d.by === this.me.member.id) for (const x of d.disciplines || []) last[x] = d.decision;
    const v = ds.map((d) => last[d]);
    return v.length && v.every((x) => x && x === v[0]) ? v[0] : null;
  }
  // Why I can't decide on a review of work I made (above prototype): said, rather than an Approve button that wouldn't count
  whyNot(review) {
    const m = review?.mine;
    if (!m || (m.covers || []).length || !m.why || !(m.authored || []).length) return '';
    return `<div class="tm-whynot">${icon('alert')}<span>${this.esc(m.why)}</span></div>`;
  }
  decideButtons(review, ds) {
    if (!review || !ds.length) return '';
    const esc = this.esc, mine = this.myDecision(review, ds), id = esc(review.id);
    const approve = mine === 'approve'
      ? `<span class="tag good tm-done" title="${esc(this.approveTitle(ds))}">${icon('check')}You approved</span>`
      : `<button class="btn primary" type="button" data-decide="approve" data-review="${id}" title="${esc(this.approveTitle(ds))}">${icon('check')}${esc(this.approveLabel(ds))}</button>`;
    const changes = mine === 'request_changes' ? '<span class="tag bad tm-done">You requested changes</span>'
      : `<button class="btn" type="button" data-decide="request_changes" data-review="${id}">Request changes</button>`;
    return approve + changes;
  }
  reviewHtml() {
    const esc = this.esc, g = this.data.gate, rev = this.rev();
    if (!rev) return '<div class="tm-empty">No revision yet.</div>';
    if (!g) return '<div class="tm-sec"><div class="skel" style="height:140px"></div></div>';
    const open = (this.data.reviews || []).find((r) => r.state !== 'withdrawn' && r.target?.rev === rev);
    const mine = this.myCover(open, g.level);
    const st = { approved: ['good', 'check'], missing: ['neutral', 'dot'], changes_requested: ['bad', 'x'] };
    const rows = g.disciplines.map((d) => `<div class="tm-gr ${d.state}"><span class="tm-gi ${st[d.state][0]}">${icon(st[d.state][1])}</span>
        <span class="tm-gl">${esc(d.label)}</span>
        <span class="tm-gw">${d.approvers.length ? d.approvers.map((a) => `<span class="tm-person" title="${esc(a.via || '')}${a.carried ? ' · approved on ' + esc(a.anchor) + ', still current' : ''}"><span class="tm-av xs" style="--c:${color(a.color)}">${esc(initials(a.name))}</span>${esc(a.name)}${a.carried ? `<small>${esc(a.anchor)}</small>` : ''}</span>`).join('')
          : d.requested_changes.length ? `changes requested by ${d.requested_changes.map((a) => esc(a.name)).join(', ')}`
          : d.needs.length ? `needs: ${d.needs.map((a) => `${esc(a.name)}${a.via === 'generalist' ? ' (generalist)' : ''}`).join(', ')}` : `<span class="dim">nobody who didn't make this work can approve it: ${this.can('admin') ? 'add someone with this discipline in Share' : 'ask an owner to add someone with it'}</span>`}</span></div>`).join('');
    const ck = g.checklist || {};
    const released = (this.data.releases || []).filter((r) => r.rev === rev);
    const level = `<span class="tm-lvl l-${esc(g.level)}">${esc(LEVEL[g.level] || g.level)}</span>`;
    const need = { prototype: '1 approval; the author may approve', pilot: 'every discipline approved by someone who didn\'t make that change',
      production: 'every discipline, 2 people who didn\'t make the change, one of them an owner' }[g.level];
    const branchSel = this.data.branches.length > 1 ? `<span class="sel"><select data-a="branch">${this.data.branches.map((b) => `<option value="${esc(b.name)}">${esc(b.name)} (${esc(b.head_rev || '')})</option>`).join('')}</select></span>` : '';
    return `<div class="tm-sec">
      <div class="tm-gate ${g.ok ? 'ok' : 'no'}"><div class="tm-gh">${level}<b>${esc(rev)}</b><span class="grow"></span>
        <span class="tag ${g.ok ? 'good' : g.short.startsWith('changes') ? 'bad' : 'warn'}">${esc(released.length ? `released: ${released.map((r) => r.tag).join(', ')}` : g.short)}</span></div>
        <div class="tm-need">${esc(LEVEL[g.level])} builds need ${esc(need)}.</div>
        <div class="tm-grs">${rows}</div>
        <div class="tm-ck">${ck.feasible === false ? `<span class="tag bad">${icon('alert')}${(ck.issues || []).length} issue${(ck.issues || []).length === 1 ? '' : 's'}</span>` : ck.feasible ? '<span class="tag good" title="Simulation, torque margins, DRC and DFM pass; not bench-validated">software checks pass</span>' : ''}
          ${ck.pick_place_success === true ? '<span class="tag good">pick and place ok</span>' : ck.pick_place_success === false ? '<span class="tag bad">pick and place failed</span>' : ''}
          ${ck.unit_cost != null ? `<span class="tag neutral">$${Number(ck.unit_cost).toFixed(0)} per unit${toolingNote(ck.tooling_cost, ck.quantity) ? `, ${esc(toolingNote(ck.tooling_cost, ck.quantity))}` : ''} (estimated)</span>` : ''}</div>
        ${ck.feasible === false ? `<div class="tm-issues">${(ck.issues || []).slice(0, 3).map((i) => `<div>${esc(i)}</div>`).join('')}</div>` : ''}
        <div class="tm-row tm-gact">
          ${open && mine.length ? this.decideButtons(open, mine) : ''}
          ${!open && this.can('request_review') && !g.ok ? `<button class="btn primary" type="button" data-a="request" title="Asks the owners of the disciplines that aren't approved yet">Request review</button>` : ''}
          <span class="grow"></span>
          ${this.can('release') ? `<button class="btn${g.ok && !released.length ? ' primary' : ''}" type="button" data-a="release" ${g.ok ? '' : 'disabled'} title="${esc(g.ok ? 'Tag this revision and unlock its order files' : g.text)}">${icon('tag')}${released.length ? 'Release again' : 'Release'}</button>` : ''}</div>
        ${open ? this.whyNot(open) : ''}
        ${!g.ok && this.can('release') ? `<div class="tm-why">${icon('alert')}Release locked: ${esc(g.text)}.</div>` : ''}
        <div class="tm-relbox"></div>
        ${released.length ? `<div class="tm-rels">${released.map((r) => `<span class="tag acc">${icon('tag')}${esc(r.tag)}</span>`).join('')}<span class="dim">released by ${esc(this.member(released[0].by)?.name || '?')}: its order files are unlocked for everyone</span></div>` : ''}</div>
      ${open ? `<div class="sec-t" style="margin-top:14px">Review requested by ${esc(this.member(open.by)?.name || '?')} <span class="r">${esc(ago(open.ts))}</span></div>
        <div class="tm-routes">${(open.requested_from || []).map((r) => {
          const s = (open.per_discipline || []).find((x) => x.discipline === r.discipline)?.state || 'waiting';
          return `<span class="tm-chip ${s}" title="${esc(s.replace('_', ' '))}">${s === 'approved' ? icon('check') : s === 'changes_requested' ? icon('x') : ''}${esc(this.disc(r.discipline))}: ${r.members.map((id) => esc(this.member(id)?.name || '?')).join(', ') || 'nobody yet'}${r.via === 'generalist' ? ' (generalist)' : ''}</span>`;
        }).join('')}</div>
        ${(open.decisions || []).slice(-6).reverse().map((d) => `<div class="tm-dec"><b>${esc(this.member(d.by)?.name || '?')}</b> ${d.decision === 'approve' ? 'approved' : 'requested changes'} as ${esc(andList(d.disciplines.map((x) => this.disc(x))))}${d.note ? `: ${esc(d.note)}` : ''} <span class="dim">${esc(ago(d.ts))}</span></div>`).join('')}` : ''}
      <div class="sec-t" style="margin-top:16px">${icon('branch')}Branches<span class="r">${esc((this.data.branches[0] || {}).head_rev || '')} on main</span></div>
      <div class="tm-row">${branchSel}${this.can('branch') ? `<button class="btn" type="button" data-a="newbranch">${icon('plus')}Branch from ${esc(rev)}</button>` : ''}<span class="grow"></span></div>
      <div class="tm-branchbox"></div></div>`;
  }
  wireReview() {
    const b = this.body, rev = this.rev();
    this.wireDecide(b);
    const rq = b.querySelector('[data-a="request"]');
    if (rq) rq.onclick = async () => {
      try { const r = await this.call('POST', '/api/reviews', { rev }); this.toast(`Asked ${r.review.disciplines.length} discipline${r.review.disciplines.length === 1 ? '' : 's'} to review ${rev}`); } catch (e) { this.toast(e.message); }
      this.loadTab();
    };
    const rel = b.querySelector('[data-a="release"]');
    if (rel) rel.onclick = () => {
      const box = b.querySelector('.tm-relbox');
      box.innerHTML = `<div class="kin"><input maxlength="40" placeholder="Release name, e.g. ${this.esc(LEVEL[this.data.gate.level])} v1" aria-label="Release name"><button class="btn primary" type="button">Release ${this.esc(rev)}</button></div>`;
      const inp = box.querySelector('input');
      inp.focus();
      box.querySelector('button').onclick = async () => {
        try {
          const r = await this.call('POST', '/api/releases', { tag: inp.value.trim(), rev });
          this.toast(`Released ${rev} as ${r.release.tag}`);
        } catch (e) { if (e.data?.gate) this.data.gate = e.data.gate; this.toast(e.message); }
        this.loadTab();
      };
    };
    const nb = b.querySelector('[data-a="newbranch"]');
    if (nb) nb.onclick = () => {
      const box = b.querySelector('.tm-branchbox');
      box.innerHTML = `<div class="kin"><input maxlength="40" placeholder="branch-name (lowercase, hyphens)" aria-label="Branch name"><button class="btn primary" type="button">Create</button></div>
        <div class="kh">A variant line: try a different servo or build without touching main, then merge it back as a change request.</div>`;
      box.querySelector('button').onclick = async () => {
        try { await this.call('POST', '/api/branches', { action: 'create', name: box.querySelector('input').value.trim(), base_rev: rev }); this.toast('Branch created'); } catch (e) { this.toast(e.message); return; }
        this.loadTab();
      };
    };
    const bs = b.querySelector('[data-a="branch"]');
    if (bs) bs.onchange = async () => {
      const box = b.querySelector('.tm-branchbox'), name = bs.value;
      if (name === 'main') { box.innerHTML = ''; return; }
      try {
        const r = await this.call('GET', '/api/branches', { compare: `main...${name}` });
        const ch = Object.entries(r.compare.changes || {});
        box.innerHTML = `<div class="tm-diff">${ch.length ? ch.map(([k, [a, v]]) => `<div><span class="mono">${this.esc(k)}</span> ${this.esc(a)} <b>${this.esc(v)}</b></div>`).join('') : '<div class="dim">Same design as main.</div>'}</div>
          ${ch.length && this.can('branch') ? `<div class="tm-row"><span class="grow"></span><button class="btn primary" type="button" data-merge="${this.esc(name)}">${icon('pr')}Merge into main as a change request</button></div>` : ''}`;
        const mg = box.querySelector('[data-merge]');
        if (mg) mg.onclick = async () => {
          try { const p = await this.call('POST', '/api/branches', { action: 'merge', name }); this.toast(`Opened change request #${p.proposal.n}`); this.show('changes'); } catch (e) { this.toast(e.message); }
        };
      } catch (e) { this.toast(e.message); }
    };
  }

  wireDecide(root) {
    const send = async (review, decision, note) => {
      try {
        const r = await this.call('POST', '/api/reviews/decide', { review, decision, note });
        if (r.gate) this.data.gate = r.gate;
        this.toast(decision === 'approve' ? 'Approved' : 'Changes requested: the author was notified');
      } catch (e) { this.toast(e.message); }
      this.loadTab();
    };
    root.querySelectorAll('[data-decide]').forEach((x) => (x.onclick = () => {
      if (x.dataset.decide === 'approve') { send(x.dataset.review, 'approve', ''); return; }
      const row = x.closest('.tm-row');
      let box = row.nextElementSibling;
      if (box?.classList.contains('tm-ask')) { box.remove(); return; }
      box = document.createElement('div');
      box.className = 'tm-ask';
      box.innerHTML = `<textarea rows="2" maxlength="4000" placeholder="What needs to change? The author sees this."></textarea>
        <div class="tm-row"><span class="grow"></span><button class="btn ghost" type="button" data-x>Cancel</button><button class="btn primary" type="button" data-go>Request changes</button></div>`;
      row.after(box);
      const ta = box.querySelector('textarea');
      ta.focus();
      box.querySelector('[data-x]').onclick = () => box.remove();
      box.querySelector('[data-go]').onclick = () => { if (ta.value.trim()) send(x.dataset.review, 'request_changes', ta.value.trim()); else ta.focus(); };
    }));
  }

  // ---- change requests
  param(key) { return (this.s.S?.current?.params || []).find((x) => x.key === key) || null; }
  paramLabel(key) { const p = this.param(key); return p ? `${p.group} · ${p.label}` : key; }
  showValue(key, v) {
    if (v === '' || v == null) return 'none';
    const p = this.param(key);
    if (typeof v === 'number') { const n = String(+v.toFixed(3)); return p?.unit ? (p.unit === '×' ? `${n}×` : `${n} ${p.unit}`) : n; }
    return String(v).split(',').map((x) => VALUE[x] || x.replace(/_/g, ' ')).join(' + ');
  }
  // What a change does, in numbers (teams/impact.py): estimated from the pipeline's fast stages before it builds
  impactHtml(imp, open = true) {
    const esc = this.esc;
    if (!imp || !((imp.rows || []).length || imp.error)) return '';
    const rows = (imp.rows || []).map((r) => `<div class="tm-ir${r.tone ? ' t-' + esc(r.tone) : ''}"><b>${esc(r.label)}</b><span>${esc(r.text)}${(r.detail || []).length
      ? `<span class="tm-idet">${r.detail.map((d) => `<span>${esc(d)}</span>`).join('')}</span>` : ''}</span></div>`).join('');
    return `<details class="tm-imp"${open ? ' open' : ''}><summary>What changes <span class="tag neutral" title="From the pipeline's fast stages (sizing, gripper checks, electronics plan) and catalog prices: the build decides">estimated</span></summary>
      ${rows || `<div class="dim">${esc(imp.error)}</div>`}</details>`;
  }
  builtHtml(b) {
    if (!b) return '';
    const [m0, m1] = b.min_torque_margin || [], [c0, c1] = b.unit_cost || [];
    const bits = [m1 != null ? `weakest joint ${m0 != null ? `${m0}× → ` : ''}${m1}×` : '', c1 != null ? `${c0 != null ? `${money(c0)} → ` : ''}${money(c1)} per unit${toolingNote((b.tooling_cost || [])[1], (b.quantity || [])[1]) ? ` (${toolingNote((b.tooling_cost || [])[1], (b.quantity || [])[1])})` : ''}` : '',
      b.feasible === false ? `${b.issues} issue${b.issues === 1 ? '' : 's'}` : b.feasible ? 'software checks pass' : ''].filter(Boolean);
    return `<div class="tm-built ${b.feasible === false ? 'bad' : ''}">${icon(b.feasible === false ? 'alert' : 'check')}<span>Built ${this.esc(b.rev)}: ${this.esc(bits.join(', '))}</span></div>`;
  }
  previewHtml(r) {
    const esc = this.esc, pv = r.preview;
    return `${this.impactHtml(pv.impact, true)}
      <div class="tm-aff">Recomputes ${pv.stage_titles.length} of 9 stages: ${esc(pv.stage_titles.join(', ') || 'none')}</div>
      <div class="tm-needs">needs: ${r.routing.routes.map((x) => `<span class="tm-chip">${esc(x.label)}: ${x.members.map((m) => esc(m.name)).join(', ') || 'nobody yet'}${x.via === 'generalist' ? ' (generalist)' : x.via === 'owner' ? ' (owner)' : ''}</span>`).join('')}</div>`;
  }
  async loadProposals() {
    try { this.data.proposals = (await this.call('GET', '/api/proposals', {})).proposals || []; } catch (e) { this.toast(`Change requests: ${e.message}`); }
  }
  changesHtml() {
    const esc = this.esc, list = this.data.proposals || [];
    const params = this.s.S?.current?.params || [];
    const draft = Object.entries(this.draft.changes), ready = draft.length || this.draft.stage;
    const nodes = this.s.S?.current?.nodes || NODES;
    const card = (p) => {
      const rv = p.review, per = rv?.per_discipline || [];
      const mine = p.status === 'open' ? this.myCover(rv, p.level) : [];
      const routes = (p.routing?.routes || []).map((r) => {
        const st = per.find((x) => x.discipline === r.discipline)?.state || 'waiting';
        return `<span class="tm-chip ${st}" title="${esc(st.replace('_', ' '))}">${st === 'approved' ? icon('check') : st === 'changes_requested' ? icon('x') : ''}${esc(r.label)}: ${r.members.map((m) => esc(m.name)).join(', ') || 'nobody yet'}${r.via === 'generalist' ? ' (generalist)' : ''}</span>`;
      }).join('');
      return `<div class="tm-pr ${esc(p.status)}" data-id="${esc(p.id)}">
        <div class="tm-prh"><span class="tm-num">#${esc(p.n)}</span><b>${esc(p.title)}</b><span class="grow"></span><span class="tag ${p.status === 'open' ? 'acc' : p.status === 'applied' ? 'good' : 'neutral'}">${esc(p.status)}${p.applied_rev ? ' ' + esc(p.applied_rev) : ''}</span></div>
        <div class="tm-meta"><span class="tm-av xs" style="--c:${color(p.author?.color)}">${esc(initials(p.author?.name))}</span>${esc(p.author?.name || '?')} · ${esc(ago(p.ts))} · on ${esc(p.base_rev || 'start')}${p.branch !== 'main' ? ' · ' + esc(p.branch) : ''}</div>
        ${(p.shown || []).length ? `<div class="tm-diff">${p.shown.map((c) => `<div><span class="tm-dk" title="${esc(c.key)}">${esc(c.label)}</span> ${esc(c.old)} <b>${esc(c.new)}</b></div>`).join('')}</div>`
          : Object.keys(p.diff || {}).length ? `<div class="tm-diff">${Object.entries(p.diff).map(([k, [a, v]]) => `<div><span class="mono">${esc(k)}</span> ${esc(a)} <b>${esc(v)}</b></div>`).join('')}</div>` : ''}
        ${p.note ? `<div class="tm-text">${esc(p.note)}</div>` : ''}
        ${p.status === 'applied' ? this.builtHtml(p.built) : ''}
        ${this.impactHtml(p.impact, p.status === 'open')}
        <div class="tm-aff">Recomputes ${esc((p.routing?.stage_titles || []).join(', ') || 'nothing in the pipeline')}</div>
        ${p.status === 'open' ? `<div class="tm-needs">needs: ${routes || '<span class="dim">no reviewers</span>'}</div>` : ''}
        ${p.status === 'open' && rv ? this.whyNot(rv) : ''}
        ${(p.conflicts || []).length ? `<div class="tm-why">${icon('alert')}main changed ${esc(p.conflicts.join(', '))} since ${esc(p.base_rev)}: applying puts this request's values on top.</div>` : ''}
        ${p.last_error ? `<div class="tm-why">${icon('alert')}The last build failed: ${esc(p.last_error)}</div>` : ''}
        ${p.status === 'open' ? `<div class="tm-row">
          ${mine.length && rv ? this.decideButtons(rv, mine) : ''}
          <span class="grow"></span>
          ${this.can('apply') && Object.keys(p.changes || {}).length ? `<button class="btn" type="button" data-apply="${esc(p.id)}" title="Queue the edit: built by you, credited to ${esc(p.author?.name || 'the author')}">Apply</button>` : ''}
          ${p.by === this.me.member.id || this.can('edit') ? `<button class="btn ghost" type="button" data-close="${esc(p.id)}">Close</button>` : ''}</div>` : ''}</div>`;
    };
    return `<div class="tm-sec">
      ${this.can('propose') ? `<details class="tm-new"${ready || this.draft.open ? ' open' : ''}><summary>${icon('plus')}Propose a change</summary>
        <div class="tm-form"><span class="sel grow"><select data-a="param" aria-label="Parameter">${params.map((p) => `<option value="${esc(p.key)}"${p.key === this.draft.param ? ' selected' : ''}>${esc(p.group)} · ${esc(p.label)}</option>`).join('')}</select></span><span class="tm-val"></span>
          <button class="btn" type="button" data-a="add">${icon('plus')}Add</button></div>
        <div class="tm-draft">${draft.map(([k, v]) => `<span class="tm-chip" title="${esc(k)}">${esc(this.paramLabel(k))}: <b>${esc(this.showValue(k, v))}</b><button type="button" data-del="${esc(k)}" title="Remove">${icon('x')}</button></span>`).join('')}</div>
        <label class="tm-f"><span>Or a change that isn't a parameter, about a stage (the manual's wording, a drawing)</span>
          <span class="sel"><select data-a="stage"><option value="">No stage</option>${nodes.map(([k, l]) => `<option value="${esc(k)}"${this.draft.stage === k ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></span></label>
        <input class="tm-in" data-a="title" maxlength="120" placeholder="Title (optional)" value="${esc(this.draft.title)}">
        <textarea rows="2" maxlength="4000" data-a="note" placeholder="Why? @name to notify someone">${esc(this.draft.note)}</textarea>
        <div class="tm-mention"></div>
        <div class="tm-preview">${this.draft.preview || ''}</div>
        <div class="tm-row"><span class="dim">${this.can('edit') ? 'Reviewers are asked by discipline; anyone with edit rights applies it.' : 'An editor applies it once reviewers agree.'}</span><span class="grow"></span>
          <button class="btn" type="button" data-a="preview" ${ready ? '' : 'disabled'}>Preview</button><button class="btn primary" type="button" data-a="submit" ${ready ? '' : 'disabled'}>Open change request</button></div></details>` : ''}
      <div class="tm-row tm-filter"><b>Change requests</b><span class="grow"></span><span class="dim">${list.filter((p) => p.status === 'open').length} open</span></div>
      ${list.length ? list.map(card).join('') : '<div class="tm-empty">No change requests yet. Propose one above: Morph shows what it affects and asks the right people.</div>'}</div>`;
  }
  wireChanges() {
    const b = this.body, params = this.s.S?.current?.params || [];
    const sel = b.querySelector('[data-a="param"]'), val = b.querySelector('.tm-val');
    const field = () => {
      const p = params.find((x) => x.key === sel.value);
      if (!p) { val.innerHTML = ''; return; }
      const cur = this.draft.param === p.key && this.draft.value != null ? this.draft.value : p.value;
      val.innerHTML = p.type === 'select'
        ? `<span class="sel"><select data-v aria-label="New value">${(p.options || []).map((o) => `<option value="${this.esc(o)}"${String(o) === String(cur) ? ' selected' : ''}>${this.esc(this.showValue(p.key, o))}</option>`).join('')}</select></span>`
        : `<div class="field"><input data-v type="number" value="${this.esc(cur)}" step="${this.esc(p.step || 'any')}"><span class="u">${this.esc(p.unit || '')}</span></div>`;
      const v = val.querySelector('[data-v]');
      v.oninput = v.onchange = () => { this.draft.param = p.key; this.draft.value = v.value; };
    };
    if (sel) { sel.onchange = () => { this.draft.param = sel.value; this.draft.value = null; field(); }; field(); }
    const form = b.querySelector('.tm-new');
    if (form) form.addEventListener('toggle', () => { this.draft.open = form.open; });
    const keep = () => {
      const t = b.querySelector('[data-a="title"]'), n = b.querySelector('[data-a="note"]');
      if (t) this.draft.title = t.value;
      if (n) this.draft.note = n.value;
    };
    b.querySelectorAll('[data-a="title"], [data-a="note"]').forEach((x) => x.addEventListener('input', keep));
    const add = b.querySelector('[data-a="add"]');
    if (add) add.onclick = () => {
      keep();
      const p = params.find((x) => x.key === sel.value), v = val.querySelector('[data-v]');
      if (!p || !v) return;
      this.draft.changes[p.key] = p.type === 'number' ? Number(v.value) : v.value;
      this.draft.preview = '';  // the preview was of the change before this one
      this.renderBody();
    };
    b.querySelectorAll('[data-del]').forEach((x) => (x.onclick = () => { keep(); delete this.draft.changes[x.dataset.del]; this.draft.preview = ''; this.renderBody(); }));
    const stage = b.querySelector('[data-a="stage"]');
    if (stage) stage.onchange = () => { keep(); this.draft.stage = stage.value; this.draft.preview = ''; this.renderBody(); };
    const note = b.querySelector('[data-a="note"]');
    if (note) this.mentions(note, b.querySelector('.tm-new .tm-mention'));
    const body = () => ({ changes: this.draft.changes, stages: this.draft.stage ? [this.draft.stage] : [], base_rev: this.rev() });
    const pv = b.querySelector('[data-a="preview"]');
    if (pv) pv.onclick = async () => {
      keep();
      pv.disabled = true;
      const box0 = this.body.querySelector('.tm-preview');
      if (box0 && !this.draft.preview) box0.innerHTML = '<div class="dim tm-aff">Working out what it changes…</div>';
      try {
        const r = await this.call('POST', '/api/proposals', { preview: true, ...body() });
        this.draft.preview = this.previewHtml(r);
      } catch (e) { this.toast(e.message); this.draft.preview = ''; }
      const box = this.body.querySelector('.tm-preview');  // the tab may have been redrawn while the preview was asked for
      if (box) box.innerHTML = this.draft.preview;
      const again = this.body.querySelector('[data-a="preview"]');
      if (again) again.disabled = false;
    };
    const sub = b.querySelector('[data-a="submit"]');
    if (sub) sub.onclick = async () => {
      keep();
      sub.disabled = true;
      try {
        const r = await this.call('POST', '/api/proposals', { ...body(), title: this.draft.title || undefined, note: this.draft.note || undefined });
        this.draft = blankDraft();
        form?.removeAttribute('open');  // what a live redraw meanwhile reads as the form's state
        this.toast(`Opened change request #${r.proposal.n}: ${r.proposal.routing.text}`);
        await this.loadProposals();
        this.renderBody();
        this.renderPills();
      } catch (e) { this.toast(e.message); sub.disabled = false; }
    };
    this.wireDecide(b);
    b.querySelectorAll('[data-apply]').forEach((x) => (x.onclick = async () => {
      x.disabled = true;
      try {
        const r = await this.call('POST', '/api/proposals/apply', { id: x.dataset.apply });
        this.toast(r.job?.conflicts?.length ? `Queued; main had changed ${r.job.conflicts.join(', ')}` : 'Queued: the pipeline rebuilds with this change');
      } catch (e) { this.toast(e.message); }
      this.loadTab();
    }));
    b.querySelectorAll('[data-close]').forEach((x) => (x.onclick = async () => {
      try { await this.call('POST', '/api/proposals/close', { id: x.dataset.close }); } catch (e) { this.toast(e.message); }
      this.loadTab();
    }));
  }

  // ---- activity and inbox
  async loadFeed() {
    try { this.data.feed = (await this.call('GET', '/api/activity', { limit: 40 })).items || []; } catch (e) { this.data.feed = []; }
  }
  activityHtml() {
    const esc = this.esc, box = this.data.inbox, feed = this.data.feed || [];
    const glyph = { revision: 'branch', chat: 'chat', comment: 'chat', proposal: 'pr', review: 'shield', release: 'tag', branch: 'branch' };
    return `<div class="tm-sec">
      <div class="tm-row tm-filter"><b>Your inbox</b>${box.unread ? `<span class="tag hot">${box.unread} new</span>` : ''}<span class="grow"></span>${box.unread ? '<button class="btn ghost" type="button" data-a="read">Mark all read</button>' : ''}</div>
      ${(box.items || []).length ? box.items.slice(0, 8).map((i) => `<div class="tm-note ${i.unread ? 'new' : ''}"><span class="tm-k">${esc(i.kind.replace('_', ' '))}</span><span>${esc(i.text)}</span><small>${esc(ago(i.ts))}</small></div>`).join('')
        : '<div class="tm-empty">Nothing for you yet.</div>'}
      <div class="tm-row tm-filter" style="margin-top:14px"><b>Activity</b><span class="grow"></span><span class="dim">${esc(this.s.project?.() || '')}</span></div>
      ${feed.length ? `<div class="tm-feed">${feed.map((i) => {
        const m = this.member(i.by);
        return `<div class="tm-fi"><span class="tm-fg">${m ? `<span class="tm-av xs" style="--c:${color(m.color)}">${esc(initials(m.name))}</span>` : icon(glyph[i.kind] || 'dot')}</span>
          <span class="tm-ft">${esc(i.text)}</span><small>${esc(ago(i.ts))}</small></div>`;
      }).join('')}</div>` : '<div class="tm-empty">No activity yet.</div>'}</div>`;
  }
  wireActivity() {
    const r = this.body.querySelector('[data-a="read"]');
    if (r) r.onclick = async () => { try { await this.call('POST', '/api/inbox/read', {}); } catch (e) { this.toast(e.message); } this.loadInbox(); };
  }
}

// ---- styles (injected once; integration may move them into style.css under /* ---- teams ---- */)
const CSS = `
body.tm-readonly #suggest, body.tm-readonly .nr-headbtn, body.tm-readonly .nr-menubtn, body.tm-readonly #importBtn,
body.tm-readonly .imp-menu, body.tm-readonly .vj-btn, body.tm-readonly #viewerTools, body.tm-readonly #presentBtn,
body.tm-readonly #paramsBar { display: none; }
body.tm-nokeys #keysBtn { display: none; }
body.tm-readonly #form { opacity: .75; } body.tm-readonly #send { opacity: .45; cursor: not-allowed; }
body.tm-readonly #paramsBody { pointer-events: none; opacity: .8; }
.tm-gone { position: fixed; inset: 0; z-index: 90; display: grid; place-items: center; background: rgba(8, 9, 12, .72); }
.tm-gone .glass { display: grid; gap: 6px; padding: 18px 22px; border-radius: var(--r3); max-width: min(420px, calc(100vw - 32px)); font-size: 13px; }
.tm-gone span { color: var(--muted); font-size: 12px; }
.tm-imp { margin-top: 8px; padding: 6px 8px; border-radius: 7px; background: var(--bg1); border: 1px solid var(--line); font-size: 11.3px; }
.tm-imp summary { cursor: pointer; display: flex; align-items: center; gap: 6px; font-weight: 600; color: var(--ink2); list-style: none; }
.tm-imp summary::-webkit-details-marker { display: none; }
.tm-imp[open] summary { margin-bottom: 4px; }
.tm-ir { display: grid; grid-template-columns: 86px minmax(0, 1fr); gap: 8px; padding: 4px 0; border-top: 1px dashed var(--line2); color: var(--ink2); line-height: 1.4; }
.tm-ir b { font-weight: 600; color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; padding-top: 1px; }
.tm-ir span { overflow-wrap: anywhere; min-width: 0; }
.tm-ir.t-good > span { color: #9ee8c3; } .tm-ir.t-bad > span { color: #ffb3b3; } .tm-ir.t-warn > span { color: #f3d79b; }
.tm-idet { display: grid; gap: 1px; margin-top: 3px; color: var(--muted); font-size: 10.8px; }
.tm-built { display: flex; gap: 6px; align-items: flex-start; margin-top: 7px; font-size: 11.3px; color: #9ee8c3; }
.tm-built.bad { color: #ffb3b3; } .tm-built svg { flex: none; margin-top: 1px; }
.tm-whynot { display: flex; gap: 6px; align-items: flex-start; margin-top: 7px; font-size: 11.3px; color: var(--muted); line-height: 1.4; }
.tm-whynot svg { flex: none; margin-top: 1px; color: var(--dim); }
.tm-dk { color: var(--ink2); }
.tm-hintbox { display: flex; gap: 8px; align-items: flex-start; margin-top: 10px; padding: 8px 10px; border-radius: 8px; background: var(--accent-bg);
  color: var(--ink2); font-size: 11.8px; line-height: 1.45; } .tm-hintbox svg { flex: none; margin-top: 1px; color: var(--accent2); }
.tm-hdr { display: flex; align-items: center; gap: 8px; }
.tm-avs { display: flex; align-items: center; }
.tm-av { --c: #5d6676; width: 24px; height: 24px; border-radius: 50%; display: inline-grid; place-items: center; flex: none; font: 650 10px var(--sans);
  color: #0a0f24; background: var(--c); box-shadow: 0 0 0 2px var(--bg1); position: relative; letter-spacing: .02em; }
.tm-avs .tm-av + .tm-av { margin-left: -6px; }
.tm-avs .tm-av::after { content: ""; position: absolute; right: -1px; bottom: -1px; width: 7px; height: 7px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 2px var(--bg1); }
.tm-avs .tm-av.idle { opacity: .55; } .tm-avs .tm-av.idle::after { background: var(--dim); }
.tm-avs .tm-av.you { box-shadow: 0 0 0 2px var(--bg1), 0 0 0 3.5px var(--c); }
.tm-avs .tm-av.more { background: var(--bg4); color: var(--ink2); } .tm-avs .tm-av.more::after { display: none; }
.tm-av.sm { width: 22px; height: 22px; font-size: 9.5px; box-shadow: none; } .tm-av.xs { width: 16px; height: 16px; font-size: 7.5px; box-shadow: none; }
.tm-role { font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); font-weight: 650; }
.tm-bell { position: relative; }
.tm-badge { position: absolute; top: -5px; right: -6px; min-width: 15px; height: 15px; padding: 0 4px; border-radius: 99px; background: var(--hot);
  color: #1a0d05; font: 700 9.5px/15px var(--sans); display: none; }
.tm-badge.on { display: block; }
.tm-hdr { position: relative; }
.tm-pop { left: auto; right: 0; min-width: 340px; max-width: 380px; max-height: 60vh; overflow: auto; white-space: normal; }
.tm-done { height: 24px; padding: 0 9px; } .tm-done svg { width: 11px; height: 11px; }
.tm-note { display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; padding: 7px 9px; border-radius: 7px; font-size: 12px; color: var(--ink2); }
.tm-note small { grid-column: 2; color: var(--dim); font-size: 10.5px; }
.tm-note.new { background: var(--accent-bg); color: var(--ink); }
.tm-k { font-size: 9.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--accent2); font-weight: 700; padding-top: 2px; white-space: nowrap; }
.tm-empty { color: var(--muted); font-size: 12px; padding: 10px 4px; line-height: 1.45; }
.tm-row { display: flex; align-items: center; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.tm-insp { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px; min-width: 0; }
.tm-text, .tm-diff, .tm-aff, .tm-ft, .tm-note span, .tm-al, .tm-dec, .tm-why, .tm-gw { overflow-wrap: anywhere; }
.tm-tabs { width: 100%; display: flex; position: sticky; top: 0; z-index: 2; background: var(--bg1); padding: 4px 0; margin-top: -4px; }
.tm-tabs button { flex: 1 1 auto; min-width: 0; justify-content: center; padding: 0 5px; gap: 3px; font-size: 11.3px; }
.tm-insp .btn { max-width: 100%; }
.tm-insp .btn.primary, .tm-gact .btn { height: auto; min-height: 28px; white-space: normal; line-height: 1.3; padding-top: 5px; padding-bottom: 5px; text-align: left; }
.tm-hint { font-size: 11px; }
.tm-n { font: 600 9.5px var(--mono); color: var(--accent2); } .tm-n:empty { display: none; }
.tm-n.good { color: var(--good); } .tm-n.warn { color: var(--warn); }
.tm-sec { padding: 10px 0 4px; }
.tm-compose textarea, .tm-replybox textarea, .tm-new textarea, .tm-ask textarea, .tm-in { width: 100%; margin-top: 6px; resize: vertical; border-radius: 8px; border: 1px solid var(--line2);
  background: var(--bg1); color: var(--ink); padding: 7px 9px; font: 12.3px/1.45 var(--sans); outline: none; }
.tm-compose textarea:focus, .tm-replybox textarea:focus, .tm-new textarea:focus, .tm-in:focus { border-color: var(--accent-line); }
.tm-compose .sel select { height: 28px; }
.tm-mention { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; } .tm-mention:empty { display: none; }
.tm-filter { margin-top: 12px; font-size: 12px; }
.tm-th { margin-top: 8px; padding: 9px 10px; border-radius: var(--r2); border: 1px solid var(--line2); background: var(--bg2); }
.tm-th.resolved { opacity: .6; }
.tm-anc { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; font-size: 11px; }
.tm-kind { font-size: 9.5px; text-transform: uppercase; letter-spacing: .07em; color: var(--dim); font-weight: 700; }
.tm-al { color: var(--ink2); font-weight: 560; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tm-msg { display: grid; grid-template-columns: 22px 1fr; gap: 8px; margin-top: 6px; }
.tm-meta { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); flex-wrap: wrap; }
.tm-meta b { color: var(--ink); font-weight: 600; font-size: 12px; }
.tm-text { font-size: 12.3px; line-height: 1.5; color: var(--ink2); white-space: pre-wrap; word-wrap: break-word; margin-top: 2px; }
.tm-at { color: var(--accent2); font-weight: 600; }
.tm-acts { display: flex; gap: 2px; margin-top: 4px; } .tm-acts .btn { height: 24px; font-size: 11.5px; padding: 0 7px; }
.tm-replybox:empty { display: none; }
.tm-gate { padding: 11px 12px; border-radius: var(--r3); border: 1px solid var(--line2); background: var(--bg2); }
.tm-gate.ok { border-color: rgba(61, 214, 140, .35); background: linear-gradient(150deg, rgba(61, 214, 140, .08), var(--bg2) 60%); }
.tm-gate.no { border-color: rgba(245, 185, 66, .3); }
.tm-gh { display: flex; align-items: center; gap: 6px 8px; font-size: 13px; flex-wrap: wrap; } .tm-gh b, .tm-lvl { white-space: nowrap; }
.tm-lvl { font-size: 9.5px; text-transform: uppercase; letter-spacing: .08em; font-weight: 700; padding: 2px 7px; border-radius: 5px; background: var(--bg4); color: var(--ink2); }
.tm-lvl.l-pilot { background: rgba(45, 212, 212, .12); color: var(--cyan); } .tm-lvl.l-production { background: rgba(167, 139, 250, .14); color: var(--violet); }
.tm-need { color: var(--muted); font-size: 11.3px; margin: 5px 0 8px; }
.tm-grs { display: grid; gap: 1px; }
.tm-gr { display: grid; grid-template-columns: 18px 92px 1fr; gap: 6px; align-items: center; padding: 5px 0; border-top: 1px dashed var(--line2); font-size: 11.8px; }
.tm-gi { width: 16px; height: 16px; border-radius: 50%; display: grid; place-items: center; }
.tm-gi svg { width: 11px; height: 11px; stroke-width: 2.4; }
.tm-gi.good { background: var(--good-bg); color: var(--good); } .tm-gi.bad { background: var(--bad-bg); color: var(--bad); } .tm-gi.neutral { color: var(--dim); }
.tm-gl { color: var(--ink2); font-weight: 560; }
.tm-gw { color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 8px; min-width: 0; }
.tm-person { display: inline-flex; align-items: center; gap: 4px; color: var(--ink); }
.tm-person small { color: var(--dim); font: 10px var(--mono); }
.tm-ck { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 9px; }
.tm-issues { margin-top: 6px; font-size: 11px; color: #ffb3b3; display: grid; gap: 2px; }
.tm-gact { margin-top: 10px; }
.tm-why { display: flex; gap: 6px; align-items: flex-start; margin-top: 8px; font-size: 11.3px; color: #f3d79b; line-height: 1.4; }
.tm-why svg { color: var(--warn); flex: none; margin-top: 1px; }
.tm-relbox:empty { display: none; } .tm-relbox { margin-top: 8px; }
.tm-rels { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 11px; }
.tm-routes, .tm-needs, .tm-draft { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.tm-needs { margin-top: 7px; font-size: 11px; color: var(--muted); }
.tm-chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 99px; border: 1px solid var(--line2); background: var(--bg1);
  color: var(--ink2); font-size: 11px; white-space: nowrap; }
.tm-chip.approved { border-color: rgba(61, 214, 140, .45); color: #9ee8c3; } .tm-chip.changes_requested { border-color: rgba(255, 107, 107, .5); color: #ffb3b3; }
.tm-chip svg { width: 11px; height: 11px; }
.tm-chip button { border: 0; background: none; color: var(--dim); padding: 0; display: grid; place-items: center; }
button.tm-chip { cursor: pointer; } button.tm-chip:hover { border-color: var(--accent-line); color: var(--ink); }
.tm-dec { font-size: 11.5px; color: var(--ink2); margin-top: 5px; }
.tm-diff { margin-top: 7px; padding: 6px 8px; border-radius: 7px; background: var(--bg1); border: 1px solid var(--line); font-size: 11.3px; color: var(--muted); display: grid; gap: 2px; }
.tm-diff div { display: flex; flex-wrap: wrap; column-gap: 6px; } .tm-diff .mono { color: var(--ink2); } .tm-diff b { color: var(--hot); font-weight: 560; }
.tm-diff b::before { content: "\\2192  "; color: var(--dim); font-weight: 400; }
.tm-pr { margin-top: 8px; padding: 10px; border-radius: var(--r2); border: 1px solid var(--line2); background: var(--bg2); }
.tm-pr.closed, .tm-pr.applied { opacity: .7; }
.tm-prh { display: flex; align-items: center; gap: 7px; font-size: 12.5px; } .tm-prh b { font-weight: 620; }
.tm-num { font: 600 11px var(--mono); color: var(--dim); }
.tm-pr .tm-meta { margin-top: 4px; }
.tm-aff { margin-top: 6px; font-size: 11px; color: var(--muted); }
.tm-new { border: 1px dashed var(--line3); border-radius: var(--r2); padding: 8px 10px; }
.tm-new summary { cursor: pointer; display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 12.3px; color: var(--accent2); list-style: none; }
.tm-new summary::-webkit-details-marker { display: none; }
.tm-new[open] summary { margin-bottom: 8px; }
.tm-form { display: flex; gap: 6px; align-items: flex-end; flex-wrap: wrap; }
.tm-form .grow { min-width: 140px; }
.tm-val .field { width: 110px; } .tm-val .sel select { min-width: 110px; }
.tm-draft { margin-top: 6px; } .tm-draft:empty { display: none; }
.tm-preview:empty { display: none; }
.tm-feed { display: grid; gap: 2px; }
.tm-fi { display: grid; grid-template-columns: 18px 1fr; gap: 1px 8px; padding: 6px 2px; border-bottom: 1px solid var(--line); font-size: 11.8px; color: var(--ink2); }
.tm-fi small { grid-column: 2; color: var(--dim); font-size: 10.5px; }
.tm-fg { display: grid; place-items: center; color: var(--dim); height: 18px; }
.tm-share-dlg { width: min(680px, calc(100vw - 32px)); }
.tm-sw { display: flex; gap: 10px; align-items: flex-start; margin-top: 10px; cursor: pointer; font-size: 12.5px; color: var(--ink2); }
.tm-sw .switch { flex: none; margin-top: 1px; } .tm-sw b { font-weight: 600; }
.tm-sw small { display: block; color: var(--dim); font-size: 11px; margin-top: 2px; line-height: 1.4; }
.tm-f { display: grid; gap: 4px; font-size: 11.5px; color: var(--muted); margin-top: 8px; }
.tm-f input { height: 30px; border-radius: 8px; border: 1px solid var(--line3); background: var(--bg1); color: var(--ink); padding: 0 9px; font: 12.5px var(--sans); outline: none; }
.tm-f .sel select { height: 30px; min-width: 120px; }
.tm-roleh { font-size: 11.3px; color: var(--dim); margin-top: 4px; }
.tm-chks { display: flex; flex-wrap: wrap; gap: 5px; }
.tm-chk { position: relative; }
.tm-chk input { position: absolute; opacity: 0; pointer-events: none; }
.tm-chk span { display: inline-flex; align-items: center; height: 26px; padding: 0 10px; border-radius: 99px; border: 1px solid var(--line2); background: var(--bg1);
  color: var(--ink2); font-size: 11.8px; cursor: pointer; transition: all .15s; }
.tm-chk input:checked + span { background: var(--accent-bg); border-color: var(--accent-line); color: var(--accent2); }
.tm-chk input:focus-visible + span { box-shadow: 0 0 0 2px var(--accent-line); }
.tm-out:empty { display: none; } .tm-out { margin-top: 10px; }
.tm-out input { font-size: 11.5px; }
.tm-mems { display: grid; gap: 4px; margin-top: 8px; }
.tm-mem { display: grid; grid-template-columns: 24px 1fr auto auto auto; gap: 4px 8px; align-items: center; padding: 7px 8px; border-radius: 8px; background: var(--bg1); border: 1px solid var(--line); }
.tm-mem .tm-av { box-shadow: none; }
.tm-who b { font-size: 12.5px; font-weight: 600; }
.tm-tags { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 2px; }
.tm-disc { font-size: 10px; font-weight: 650; padding: 1px 6px; border-radius: 5px; background: var(--bg4); color: var(--ink2); }
.tm-disc.d-electronics { background: rgba(61, 214, 140, .12); color: var(--good); } .tm-disc.d-mechanical { background: rgba(112, 145, 255, .14); color: var(--accent2); }
.tm-disc.d-manufacturing { background: rgba(45, 212, 212, .12); color: var(--cyan); } .tm-disc.d-sourcing { background: var(--hot-bg); color: var(--hot); }
.tm-disc.d-simulation { background: rgba(167, 139, 250, .14); color: var(--violet); } .tm-disc.d-documentation { background: var(--warn-bg); color: var(--warn); }
.tm-disc.d-generalist { background: rgba(255, 255, 255, .08); color: var(--ink); }
.tm-mem .sel select { height: 26px; }
.tm-edit { grid-column: 2 / -1; } .tm-edit:empty { display: none; }
.tm-danger:hover { color: var(--bad); }
.tm-inv { display: flex; align-items: center; gap: 8px; padding: 6px 8px; margin-top: 4px; border-radius: 8px; background: var(--bg1); font-size: 12px; flex-wrap: wrap; }
.tm-toks { margin-top: 8px; }
`;
function injectCss() {
  if (document.getElementById('team-css')) return;
  const st = document.createElement('style');
  st.id = 'team-css';
  st.textContent = CSS;
  document.head.appendChild(st);
}

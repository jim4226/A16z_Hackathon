// API keys dialog: shows which integrations are connected and lets the person paste their own keys.
// Keys go to the local Studio server, which writes them to the repo's git-ignored .env and applies
// them at once. The server only ever reports whether a key is set, never its value. The privacy
// switches (network, Keewano events, Keewano calibration) are non-secret settings (/api/settings).
import { I, esc, sleep } from './panels.js';

const JSON_POST = { 'Content-Type': 'application/json', 'X-Morph-Client': '1' };  // the server refuses changes without X-Morph-Client
const KEY_ICON = '<svg class="i sm" viewBox="0 0 24 24"><circle cx="7.5" cy="15.5" r="4"/><path d="M10.5 12.5L20 3M16 7l3 3M13.5 9.5l2 2"/></svg>';
const SERVICE_NOTE = {
  Claude: 'Free-form chat. Without a key, the suggestion chips and common phrasings still work.',
  Keewano: 'Build memory: logs revisions and measurements in your Keewano project once Send build events is on (below). Grip-force calibration from that history is optional; the R Cubed demo history is seeded, not measured.',
};
const SETTINGS = [
  ['network_enabled', 'Allow network lookups (Darwin, SubQ, Claude)', 'Off: nothing leaves this computer. Chat uses the built-in rules and the BOM shows saved Darwin answers.'],
  ['telemetry_enabled', 'Send build events to Keewano', 'Off by default. On: revisions, edits and bench measurements go to your Keewano project (needs the project key).'],
  ['calibration_enabled', 'Use Keewano calibration (demo data unless you have real builds)', 'Off by default. On: newly generated robots scale the grip-force target by 3 or more logged builds with the same pad and material (synthetic-* projects ignored).'],
];
const SAVED = {
  network_enabled: ['Network lookups off: Morph now uses only what is on this computer.', 'Network lookups on.'],
  telemetry_enabled: ['No more build events go to Keewano.', 'Build events go to Keewano from the next change (with the project key set).'],
  calibration_enabled: ['Keewano calibration off: new robots use the simulated holding force.', 'Keewano calibration on for newly generated robots; existing revisions keep their grip target.'],
};

export class KeysDialog {
  constructor(api) {
    this.a = api;  // { toast, onChanged(integrations), reset(rebuild) }
    this.el = document.createElement('div');
    this.el.id = 'keysDlg';
    this.el.className = 'modal';
    this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.el.classList.contains('on')) this.close(); });
    document.body.appendChild(this.el);
  }
  async refresh() {
    const r = await fetch('/api/keys', { cache: 'no-store' });
    if (!r.ok) throw new Error(`${r.status}`);
    this.data = await r.json();
    return this.data;
  }
  async open() {
    this.el.classList.add('on');
    this.el.innerHTML = '<div class="glass dlg"><div class="skel" style="height:220px"></div></div>';
    try { await this.refresh(); } catch (e) {
      this.el.innerHTML = `<div class="glass dlg"><p>Couldn't reach the Studio server (${esc(e.message)}).</p></div>`;
      return;
    }
    this.render();
  }
  close() { this.el.classList.remove('on'); }
  render() {
    const d = this.data, keys = d.keys || [];
    const services = [...new Set(keys.map((k) => k.service))];
    const field = (k) => `<div class="kf" data-k="${esc(k.name)}">
        <label><span>${esc(k.label)}</span>${k.set ? `<span class="tag good">${I('check', 'sm')}set</span>` : '<span class="tag neutral">not set</span>'}</label>
        <div class="kin"><input type="password" autocomplete="off" spellcheck="false" placeholder="${k.set ? 'Set. Paste a new key to replace it' : 'Paste key'}" aria-label="${esc(k.label)}">
          ${k.set ? `<button class="btn ghost" type="button" data-clear="${esc(k.name)}" title="Remove this key">Remove</button>` : ''}</div>
        <div class="kh">${esc(k.what)} <a href="${esc(k.url)}" target="_blank" rel="noopener">Where: ${esc(k.where)}</a></div></div>`;
    const kw = d.integrations?.keewano || {}, st = d.settings || {};
    const toggle = ([k, title, note]) => `<label class="kt"><span class="switch"><input type="checkbox" data-s="${k}"${st[k] ? ' checked' : ''}><i></i></span>
        <span><b>${esc(title)}</b><small>${esc(note)}</small></span></label>`;
    this.el.innerHTML = `<div class="glass dlg" role="dialog" aria-label="API keys">
      <div class="dlg-h"><div class="kicon">${KEY_ICON}</div><div class="grow"><div class="t">API keys</div>
        <div class="s">Optional: everything works without them. Saved only on ${d.compute?.remote ? `<b>${esc(d.compute.host)}</b>, the computer running Morph Studio,` : 'this computer,'} in the <span class="mono">${esc(d.file || '.env')}</span> file next to <span class="mono">run_studio.bat</span> (git-ignored). Values are never shown again or sent anywhere except the service they belong to.</div></div>
        <button class="btn icon ghost" type="button" data-a="close" title="Close (Esc)">${I('x', 'sm')}</button></div>
      ${services.map((sv) => `<section class="ksec"><div class="ks-h"><b>${esc(sv)}</b>${keys.filter((k) => k.service === sv).every((k) => k.set) ? '<span class="tag good">set up</span>' : '<span class="tag neutral">not set up</span>'}</div>
        <div class="ks-n">${esc(SERVICE_NOTE[sv] || '')}</div>${keys.filter((k) => k.service === sv).map(field).join('')}</section>`).join('')}
      <section class="ksec"><div class="ks-h"><b>Darwin</b><span class="tag good">no key needed</span></div>
        <div class="ks-n">Darwin Search is public, so the BOM tab finds agents for every part without a key. Ordering through Darwin uses your own Darwin sign-in, outside Morph.</div></section>
      <section class="ksec"><div class="ks-h"><b>Privacy and network</b></div>${SETTINGS.map(toggle).join('')}
        ${st.network_enabled === false ? '<div class="ks-n">Network lookups are off, so nothing is sent to Keewano, Darwin, SubQ or Claude.</div>' : ''}</section>
      <div class="knote"></div>
      <div class="dlg-f">${keys.some((k) => k.set) ? `<button class="btn" type="button" data-a="test" title="Try each key once (read-only; nothing is sent to Keewano)">${I('check', 'sm')}Test keys</button>` : ''}
        ${kw.calibration ? `<button class="btn" type="button" data-a="rebuild" title="Keewano calibration applies when a robot is generated. This resets the demo: chat edits are deleted and both robots are rebuilt from their original design.">${I('replay', 'sm')}Reset demo and rebuild</button>` : ''}
        <span class="grow"></span><button class="btn" type="button" data-a="close">Close</button><button class="btn primary" type="button" data-a="save">Save keys</button></div>
    </div>`;
    this.el.querySelectorAll('[data-a="close"]').forEach((b) => (b.onclick = () => this.close()));
    this.el.querySelector('[data-a="save"]').onclick = () => this.save();
    this.el.querySelectorAll('[data-clear]').forEach((b) => (b.onclick = () => this.save({ [b.dataset.clear]: '' })));
    const tb = this.el.querySelector('[data-a="test"]');
    if (tb) tb.onclick = () => this.test();
    const rb = this.el.querySelector('[data-a="rebuild"]');
    if (rb) rb.onclick = () => this.rebuild(rb);
    this.el.querySelectorAll('.kf input').forEach((inp) => inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.save(); }));
    this.el.querySelectorAll('[data-s]').forEach((inp) => (inp.onchange = () => this.setting(inp.dataset.s, inp.checked, inp)));
    this.el.querySelector('.kf input')?.focus();
  }
  async setting(name, on, inp) {
    inp.disabled = true;
    const r = await fetch('/api/settings', { method: 'POST', headers: JSON_POST, body: JSON.stringify({ values: { [name]: on } }) })
      .catch((e) => ({ ok: false, json: async () => ({ error: `Couldn't reach the Studio server (${e.message})` }) }));
    const out = await r.json().catch(() => ({}));
    if (!r.ok || !out.ok) {
      inp.checked = !on;
      inp.disabled = false;
      this.note(this.row('bad', esc(out.error || 'Could not save that setting')));
      return;
    }
    this.data = { ...this.data, settings: out.settings, integrations: out.integrations };
    this.a.onChanged?.(out.integrations);
    this.render();
    this.el.querySelector(`[data-s="${name}"]`)?.focus();
    this.note(this.row('good', esc(SAVED[name]?.[on ? 1 : 0] || 'Saved.')));
  }
  // one line in the note under the fields: kind is 'good', 'bad' or 'busy'
  row(kind, html) {
    const icon = kind === 'busy' ? '<span class="spin"></span>' : I(kind === 'good' ? 'check' : 'alert', 'sm');
    return `<div class="kchk ${kind}">${icon}<span>${html}</span></div>`;
  }
  note(html) {
    const n = this.el.querySelector('.knote');
    if (!n) return;
    n.innerHTML = html || '';
    if (html) n.scrollIntoView({ block: 'end' });  // 'nearest' ignores the sticky buttons covering it
  }
  async save(extra) {
    const values = { ...(extra || {}) };
    this.el.querySelectorAll('.kf').forEach((f) => { const v = f.querySelector('input').value.trim(); if (v) values[f.dataset.k] = v; });
    if (!Object.keys(values).length) { this.a.toast('Paste a key first'); return; }
    const btn = this.el.querySelector('[data-a="save"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    const r = await fetch('/api/keys', { method: 'POST', headers: JSON_POST, body: JSON.stringify({ values }) })
      .catch((e) => ({ ok: false, json: async () => ({ error: `Couldn't reach the Studio server (${e.message})` }) }));
    const out = await r.json().catch(() => ({}));
    if (!r.ok || !out.ok) {
      if (btn) { btn.disabled = false; btn.textContent = 'Save keys'; }
      this.note(this.row('bad', esc(out.error || 'Could not save the keys')));
      return;
    }
    this.data = { ...this.data, keys: out.keys, integrations: out.integrations };
    this.a.onChanged?.(out.integrations);
    this.render();
    const added = (out.changed || []).filter((k) => values[k]), removed = (out.changed || []).filter((k) => !values[k]);
    const label = (k) => esc((out.keys || []).find((x) => x.name === k)?.label || k);
    const kwAdded = added.some((k) => k.startsWith('KEEWANO'));
    const saved = this.row('good', [
      added.length ? `Saved ${added.length} key${added.length === 1 ? '' : 's'} to ${esc(this.data.file || '.env')}; ${added.length === 1 ? 'it applies' : 'they apply'} now, no restart needed.` : '',
      removed.length ? `Removed: ${removed.map(label).join(', ')}.` : '',
      kwAdded && !out.integrations.keewano?.send ? 'Nothing goes to Keewano until <b>Send build events to Keewano</b> is on (Privacy and network, below).' : '',
      kwAdded && out.integrations.keewano?.calibration ? 'The Keewano grip calibration applies to newly generated robots: <b>Reset demo and rebuild</b> regenerates both (about a minute).' : '',
    ].filter(Boolean).join(' '));
    this.note(saved);
    this.a.toast(added.length ? 'Keys saved on this computer' : 'Key removed');
    const services = [...new Set(added.map((k) => (out.keys || []).find((x) => x.name === k)?.service).filter(Boolean))];
    if (services.length) await this.test(services, saved);
  }
  async test(services, before = '') {
    this.note(before + this.row('busy', `Checking ${services ? esc(services.join(' and ')) : 'your keys'}…`));
    const r = await fetch('/api/keys/check', { method: 'POST', headers: JSON_POST, body: JSON.stringify({ services: services || null }) })
      .catch(() => null);
    const out = r?.ok ? await r.json().catch(() => ({})) : null;
    const rows = Object.entries(out?.checks || {});
    this.note(before + (!out ? this.row('bad', "Couldn't reach the Studio server to check the keys.")
      : rows.length ? rows.map(([sv, c]) => this.row(c.ok ? 'good' : 'bad', `<b>${esc(sv)}</b>: ${esc(c.detail || '')}`)).join('')
        : this.row('bad', 'Nothing to check yet: Keewano needs both the personal access token and the project ID.')));
  }
  async rebuild(btn) {
    if (!btn.dataset.armed) {  // destructive: the first click only arms it
      btn.dataset.armed = '1';
      btn.classList.add('warnbtn');
      btn.innerHTML = `${I('alert', 'sm')}Deletes chat edits: click again`;
      setTimeout(() => { if (btn.isConnected && !btn.disabled) { delete btn.dataset.armed; btn.classList.remove('warnbtn'); btn.innerHTML = `${I('replay', 'sm')}Reset demo and rebuild`; } }, 8000);
      return;
    }
    btn.disabled = true;
    btn.classList.remove('warnbtn');
    btn.innerHTML = '<span class="spin"></span>Rebuilding both robots…';
    this.note(this.row('busy', 'Rebuilding both robots from their original design with your keys (about a minute; the terminal shows each stage)…'));
    const ok = await this.a.reset(true).catch(() => false);
    this.a.toast(ok ? 'Both robots rebuilt with your keys' : 'Still regenerating; try again when the pipeline is idle');
    await sleep(100);
    if (!this.el.classList.contains('on')) return;
    await this.refresh().catch(() => {});
    this.render();
    this.note(ok ? this.row('good', 'Rebuilt. The grip-force window (right panel) shows the Keewano calibration when your project has 3 or more logged builds for this pad and material.')
      : this.row('bad', 'The pipeline is busy with another change. Try again when the strip at the top is idle.'));
  }
}

export const keyIcon = KEY_ICON;

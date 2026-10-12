// Import dialog: drop a robot folder, a zip, a mesh, a parts list or a KiCad board (or paste a GitHub or
// Hugging Face link). Morph works out what it is, shows what it found and why (gripper, tool point, actuators,
// units, licence) with a preview, asks only what the files can't tell, then builds it as a new project.
//
//   import { init } from './importer.js';  const importer = init(studio);   // contracts section 4
//
// Uses studio.api (PUT streams each file as a raw body), studio.on('import' | 'node' | 'project_created'),
// studio.modal when present, studio.preview3d(el, glbUrl) when the host offers its 3D viewer (otherwise a
// server-rendered thumbnail, with an optional lightweight three.js view).

const MAX_FILES = 2000;
const MAX_BYTES = 300 * 1024 * 1024;
const PARALLEL = 3;
const STAGES = [['sizing', 'Sizing'], ['gripper', 'Gripper'], ['electronics', 'Electronics'], ['cad', 'CAD'], ['sim', 'Simulation'],
  ['wiring', 'Wiring'], ['manufacturing', 'Manufacturing'], ['bom', 'BOM'], ['manual', 'Manual']];
const FAMILIES = [['smart_bus', 'Smart bus', 'Feetech STS3215'], ['dynamixel', 'Dynamixel', 'ROBOTIS XL/XM'], ['hobby_pwm', 'Hobby PWM', 'MG996R, SG90']];
const SHORT = { sg90: 'SG90', mg996r: 'MG996R', ds3218: 'DS3218', sts3215: 'STS3215 12 V', sts3215_7v4: 'STS3215 7.4 V',
  xl330_m288: 'XL330-M288', xl430_w250: 'XL430-W250', xm430_w350: 'XM430-W350' };
const shortName = (k, name) => SHORT[k] || (name || k).replace(/\s*\(.*\)$/, '');
const KIND_LABEL = { robot: 'Robot', object: 'Object to grasp', bom: 'Parts list', board: 'KiCad board', project: 'Morph project' };
const UPLOAD_ICON = '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4M7 8.5l5-5 5 5M4 15v5h16v-5"/></svg>';

const CSS = `
.imp-dlg { width: min(880px, calc(100vw - 32px)) !important; }
.imp-sub { color: var(--muted, #838c9d); font-size: 12px; margin-top: 3px; line-height: 1.45; }
.imp-drop { margin-top: 12px; border: 1.5px dashed var(--line3, #343b49); border-radius: 12px; padding: 26px 18px; text-align: center;
  background: var(--bg1, #0d0f14); transition: border-color .15s, background .15s; }
.imp-drop.over { border-color: var(--accent, #7091ff); background: var(--accent-bg, rgba(112,145,255,.12)); }
.imp-drop .big { font-size: 15px; font-weight: 640; color: var(--ink, #e9ecf2); margin: 6px 0 4px; }
.imp-drop .fmt { color: var(--dim, #5d6676); font-size: 11.5px; margin-top: 8px; }
.imp-drop .btns { display: flex; gap: 8px; justify-content: center; margin-top: 12px; }
.imp-drop svg.i { width: 26px; height: 26px; color: var(--accent2, #9db2ff); stroke: currentColor; fill: none; stroke-width: 1.6; }
.imp-or { display: flex; align-items: center; gap: 10px; color: var(--dim, #5d6676); font-size: 11px; margin: 14px 0 8px; text-transform: uppercase; letter-spacing: .06em; }
.imp-or::before, .imp-or::after { content: ""; flex: 1; height: 1px; background: var(--line2, #262c38); }
.imp-url { display: flex; gap: 6px; }
.imp-url input, .imp-in { flex: 1; height: 32px; border-radius: 8px; border: 1px solid var(--line3, #343b49); background: var(--bg1, #0d0f14);
  color: var(--ink, #e9ecf2); padding: 0 10px; font: 12.5px var(--mono, monospace); outline: none; min-width: 0; }
.imp-url input:focus, .imp-in:focus, .imp select:focus { border-color: var(--accent-line, rgba(112,145,255,.45)); box-shadow: 0 0 0 3px var(--accent-bg, rgba(112,145,255,.12)); }
.imp-note { color: var(--dim, #5d6676); font-size: 11.5px; margin-top: 6px; }
.imp-pre { margin: 8px 0 0; padding: 8px 10px; border-radius: 8px; background: var(--bg1, #0d0f14); border: 1px solid var(--line2, #262c38);
  font: 11.5px var(--mono, monospace); white-space: pre-wrap; word-break: break-word; user-select: all; }
.imp-bar { height: 4px; border-radius: 3px; background: var(--bg4, #20252f); overflow: hidden; margin: 12px 0 4px; }
.imp-bar > i { display: block; height: 100%; width: 0; background: var(--accent, #7091ff); transition: width .2s; }
.imp-check { display: grid; gap: 5px; margin-top: 10px; }
.imp-row { display: flex; gap: 8px; align-items: flex-start; font-size: 12.5px; line-height: 1.45; color: var(--ink2, #b9c0cd); }
.imp-row .dot { flex: none; width: 18px; height: 18px; border-radius: 50%; display: grid; place-items: center; margin-top: 0; }
.imp-row .dot svg { width: 12px; height: 12px; stroke: currentColor; fill: none; stroke-width: 2.4; }
.imp-row.done .dot { background: var(--good-bg, rgba(61,214,140,.12)); color: var(--good, #3dd68c); }
.imp-row.warn .dot { background: var(--warn-bg, rgba(245,185,66,.12)); color: var(--warn, #f5b942); }
.imp-row.error .dot { background: var(--bad-bg, rgba(255,107,107,.12)); color: var(--bad, #ff6b6b); }
.imp-row.busy .dot, .imp-row.start .dot, .imp-row.progress .dot { color: var(--accent2, #9db2ff); }
.imp-row b { color: var(--ink, #e9ecf2); font-weight: 600; }
.imp-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; margin-top: 12px; }
@media (max-width: 760px) { .imp-grid { grid-template-columns: 1fr; } }
.imp-cands { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.imp-cand { border: 1px solid var(--line2, #262c38); background: var(--bg2, #12151c); color: var(--ink2, #b9c0cd); border-radius: 8px;
  padding: 5px 9px; font-size: 12px; cursor: pointer; display: flex; gap: 6px; align-items: center; max-width: 280px; }
.imp-cand span.f { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono, monospace); font-size: 11px; color: var(--muted, #838c9d); }
.imp-cand.on { border-color: var(--accent-line, rgba(112,145,255,.45)); background: var(--accent-bg, rgba(112,145,255,.12)); color: var(--ink, #e9ecf2); }
.imp-sec { margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--line2, #262c38); }
.imp-sec h4 { margin: 0 0 6px; font-size: 12px; font-weight: 650; color: var(--ink, #e9ecf2); display: flex; align-items: center; gap: 8px; }
.imp-sec h4 .grow { flex: 1; }
.imp-lab { display: block; font-size: 11.5px; color: var(--muted, #838c9d); margin: 10px 0 4px; }
.imp select { height: 28px; border-radius: 7px; border: 1px solid var(--line3, #343b49); background: var(--bg1, #0d0f14); color: var(--ink, #e9ecf2);
  font: 12px var(--sans, sans-serif); padding: 0 6px; outline: none; max-width: 100%; }
.imp-acts { width: 100%; border-collapse: collapse; font-size: 12px; }
.imp-acts td { padding: 4px 6px 4px 0; vertical-align: middle; border-top: 1px solid var(--line, #1c212b); }
.imp-acts td.j { font-family: var(--mono, monospace); color: var(--ink, #e9ecf2); white-space: nowrap; }
.imp-acts td.why { color: var(--dim, #5d6676); font-size: 11px; }
.imp-acts tr.need td.j::after { content: " *"; color: var(--warn, #f5b942); }
.imp-fam { display: flex; gap: 6px; flex-wrap: wrap; margin: 2px 0 8px; }
.imp-fam .btn { height: 26px; font-size: 11.5px; }
.imp-fam .btn small { color: var(--dim, #5d6676); margin-left: 4px; }
.imp-warn { margin: 8px 0 0; padding: 0; list-style: none; display: grid; gap: 4px; }
.imp-warn li { font-size: 11.8px; color: #f3d38f; background: var(--warn-bg, rgba(245,185,66,.12)); border-radius: 7px; padding: 5px 8px; }
.imp-prev { border-radius: 12px; background: var(--bg1, #0d0f14); border: 1px solid var(--line2, #262c38); overflow: hidden; position: relative;
  aspect-ratio: 4 / 3; display: grid; place-items: center; }
.imp-prev img, .imp-prev canvas { width: 100%; height: 100%; object-fit: contain; display: block; }
.imp-prev .cap { position: absolute; left: 6px; bottom: 6px; font-size: 10.5px; color: var(--muted, #838c9d);
  background: rgba(13, 15, 20, .78); padding: 1px 6px; border-radius: 5px; }
.imp-prev .tcp { position: absolute; right: 8px; top: 6px; font-size: 10.5px; color: #ffb27d; }
.imp-kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; font-size: 12px; margin-top: 8px; }
.imp-kv dt { color: var(--muted, #838c9d); } .imp-kv dd { margin: 0; color: var(--ink, #e9ecf2); font-variant-numeric: tabular-nums; }
.imp-table { width: 100%; border-collapse: collapse; font-size: 11.5px; margin-top: 6px; }
.imp-table th { text-align: left; color: var(--muted, #838c9d); font-weight: 600; padding: 4px 6px; border-bottom: 1px solid var(--line2, #262c38); }
.imp-table td { padding: 4px 6px; border-bottom: 1px solid var(--line, #1c212b); color: var(--ink2, #b9c0cd); }
.imp-table td.n { text-align: right; font-variant-numeric: tabular-nums; }
.imp-stages { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 12px; }
.imp-stage { font-size: 11px; padding: 3px 8px; border-radius: 6px; background: var(--bg3, #181c25); color: var(--dim, #5d6676); border: 1px solid var(--line2, #262c38); }
.imp-stage.computing { color: var(--accent2, #9db2ff); border-color: var(--accent-line, rgba(112,145,255,.45)); }
.imp-stage.computed, .imp-stage.reused { color: var(--good, #3dd68c); background: var(--good-bg, rgba(61,214,140,.12)); border-color: transparent; }
.imp-stage.error { color: var(--bad, #ff6b6b); background: var(--bad-bg, rgba(255,107,107,.12)); }
.imp-stage.pending { color: var(--warn, #f5b942); }
.imp-ferr { flex: 1; min-width: 0; color: #ffc4c4; font-size: 12px; line-height: 1.35; }
.imp-acts tr.miss select { border-color: var(--warn, #f5b942); box-shadow: 0 0 0 3px var(--warn-bg, rgba(245,185,66,.12)); }
.imp-acts select { width: 100%; max-width: 220px; }
.imp-acts td.why { width: 46%; }
.imp-err { margin-top: 12px; padding: 9px 12px; border-radius: 9px; background: var(--bad-bg, rgba(255,107,107,.12)); color: #ffc4c4; font-size: 12.5px; line-height: 1.45; }
.imp-overlay { position: fixed; inset: 0; z-index: 59; display: none; place-items: center; background: rgba(8, 9, 12, .72); pointer-events: none; }
.imp-overlay.on { display: grid; pointer-events: auto; }
.imp-overlay > div { border: 2px dashed var(--accent, #7091ff); border-radius: 18px; padding: 40px 56px; color: var(--ink, #e9ecf2); font-size: 17px; font-weight: 640;
  background: rgba(13, 15, 20, .85); text-align: center; }
.imp-overlay small { display: block; font-size: 12px; font-weight: 400; color: var(--muted, #838c9d); margin-top: 6px; }
`;

const ROW_ICON = {
  done: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 6v7M12 17.5v.01"/></svg>',
  error: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17"/></svg>',
};

function injectCss() {
  if (document.getElementById('importer-css')) return;
  const st = document.createElement('style');
  st.id = 'importer-css';
  st.textContent = CSS;
  document.head.appendChild(st);
}

const fmtMB = (n) => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(n >= 10 * 1024 * 1024 ? 0 : 1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
const hidden = (name) => name.startsWith('.') || name === '__MACOSX' || name === 'Thumbs.db';

// ------------------------------------------------------------------ folders dropped on the page
async function entriesToFiles(items) {
  const out = [];
  const walk = async (entry, prefix) => {
    if (out.length > MAX_FILES || hidden(entry.name)) return;
    if (entry.isFile) {
      const f = await new Promise((res) => entry.file(res, () => res(null)));
      if (f) out.push({ file: f, path: prefix + entry.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {   // readEntries returns at most 100 entries per call
        const batch = await new Promise((res) => reader.readEntries(res, () => res([])));
        if (!batch.length) break;
        for (const e of batch) await walk(e, `${prefix}${entry.name}/`);
      }
    }
  };
  for (const it of items) {
    const e = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
    if (e) await walk(e, '');
    else if (it.getAsFile && it.getAsFile()) out.push({ file: it.getAsFile(), path: it.getAsFile().name });
  }
  return out;
}

function filesFromInput(list) {
  return [...list].filter((f) => !(f.webkitRelativePath || f.name).split('/').some(hidden))
    .map((f) => ({ file: f, path: f.webkitRelativePath || f.name }));
}

// ------------------------------------------------------------------ the dialog
export class Importer {
  constructor(studio) {
    this.s = studio;
    this.esc = studio.esc || ((v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
    this.I = studio.I || (() => '');
    this.view = 'pick';
    this.subs = [];
    this.rows = [];
    this.det = null;
    this.sel = 0;
    this.answers = {};
    this.building = null;
    injectCss();
  }

  // ---- api plumbing (studio.api may return parsed JSON or a fetch Response)
  async call(method, path, body, params) {
    const a = this.s.api;
    let r;
    if (method === 'GET') r = await a.get(path, params);
    else if (method === 'POST') r = await a.post(path, body);
    else if (method === 'PUT') r = await a.put(path, body, params);
    else r = await a.del(path, params);
    if (r && typeof r.json === 'function') {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `The Studio answered ${r.status}`);
      return data;
    }
    if (r && r.ok === false) throw new Error(r.error || 'The import failed');
    return r || {};
  }

  url(path, params) {
    if (this.s.api.url) return this.s.api.url(path, params);
    const q = new URLSearchParams({ ...(params || {}) });
    const p = this.s.project?.();
    if (p) q.set('project', p);
    return `${path}?${q}`;
  }

  // ---- lifecycle
  open(opts = {}) {
    if (!this.el) {
      this.el = document.createElement('div');
      this.el.className = 'glass dlg imp-dlg imp';
      this.el.setAttribute('role', 'dialog');
      this.el.setAttribute('aria-label', 'Import');
      this.el.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); this.close(); } });
    }
    if (this.s.modal?.open) this.s.modal.open(this.el, { wide: true });
    else {
      if (!this.host) {
        this.host = document.createElement('div');
        this.host.className = 'modal';
        this.host.addEventListener('click', (e) => { if (e.target === this.host) this.close(); });
        document.body.appendChild(this.host);
      }
      this.host.appendChild(this.el);
      this.host.classList.add('on');
    }
    this.subscribe();
    if (this.view === 'building' && this.building) { this.render(); return; }
    this.reset();
    if (opts.files?.length) this.upload(opts.files);
    else if (opts.url) this.fetchUrl(opts.url);
    else this.render();
  }

  close() {
    if (this.s.modal?.close && !this.host) this.s.modal.close();
    if (this.host) this.host.classList.remove('on');
    this.disposePreview();
    if (this.view !== 'building') {
      this.unsubscribe();
      if (this.id && this.view !== 'done') this.call('DELETE', `/api/import/${this.id}`).catch(() => {});
      this.id = null;
    }
  }

  reset() {
    this.view = 'pick'; this.id = null; this.det = null; this.sel = 0; this.rows = []; this.error = ''; this.answers = {};
    this.progress = 0; this.building = null; this.result = null; this.flagMissing = false; this.nameValue = null;
  }

  subscribe() {
    if (this.subs.length || !this.s.on) return;
    this.subs.push(this.s.on('import', (ev) => this.onImport(ev)));
    this.subs.push(this.s.on('node', (ev) => this.onNode(ev)));
    this.subs.push(this.s.on('project_created', (ev) => this.onCreated(ev)));
  }

  unsubscribe() { this.subs.forEach((u) => { try { u?.(); } catch (e) { /* already gone */ } }); this.subs = []; }

  // ---- events
  onImport(ev) {
    if (ev.id && this.id && ev.id !== this.id) return;
    if (!this.id && !(this.building && ev.project === this.building.project)) return;
    const key = ev.stage === 'meshes' || ev.stage === 'gripper' || ev.stage === 'actuators' ? `${ev.stage}:${ev.text.split(':')[0]}` : ev.stage;
    const row = { stage: ev.stage, status: ev.status === 'start' || ev.status === 'progress' ? 'busy' : ev.status, text: ev.text };
    this.rows.forEach((r) => { if (r.status === 'busy' && r.key !== key && !['detect', 'upload', 'build'].includes(r.key)) r.status = 'done'; });
    const i = this.rows.findIndex((r) => r.key === key);
    if (i >= 0) this.rows[i] = { ...row, key }; else this.rows.push({ ...row, key });
    if (ev.stage === 'build' && ev.status === 'error' && this.building) { this.building.error = ev.text; }
    if (ev.stage === 'fetch' && ev.status === 'done' && this.view === 'working' && this.fetching) { this.fetching = false; this.detect(); return; }
    if (ev.stage === 'fetch' && ev.status === 'error' && this.fetching) { this.fetching = false; this.fail(ev.text); return; }
    if (['working', 'building'].includes(this.view)) this.render();
  }

  onNode(ev) {
    if (!this.building || ev.project !== this.building.project) return;
    this.building.nodes[ev.node] = ev.status;
    if (this.view === 'building') this.render();
  }

  onCreated(ev) {
    if (!this.building || ev.project !== this.building.project) return;
    this.building.done = true;
    this.view = 'done';
    this.s.toast?.(`Imported ${ev.project}`);
    this.render();
  }

  fail(msg) {
    this.error = String(msg || 'The import failed');
    this.view = this.det ? 'review' : 'pick';
    this.render();
  }

  // ---- steps
  async start() {
    if (this.id) return this.id;
    const r = await this.call('POST', '/api/import', {});
    this.id = r.id;
    return this.id;
  }

  async fetchUrl(url) {
    url = (url || '').trim();
    if (!/^https:\/\//i.test(url)) { this.fail('Paste a link that starts with https:// (github.com, raw.githubusercontent.com or huggingface.co).'); return; }
    this.error = '';
    this.view = 'working';
    this.rows = [{ key: 'fetch', stage: 'fetch', status: 'busy', text: `Fetching ${url}` }];
    this.render();
    try {
      const r = await this.call('POST', '/api/import', { url });
      this.id = r.id;
      this.fetching = true;
      if (!this.s.on) await this.pollFetch();
    } catch (e) { this.fail(e.message); }
  }

  async pollFetch() {   // only used when the host has no live events
    for (let i = 0; i < 600 && this.fetching; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const st = await this.call('GET', `/api/import/${this.id}`).catch(() => null);
      if (st?.fetch?.status === 'done') { this.fetching = false; return this.detect(); }
      if (st?.fetch?.status === 'error') { this.fetching = false; return this.fail(st.fetch.error); }
    }
  }

  async upload(items) {
    this.error = '';
    if (!items.length) { this.fail('Nothing to upload: the folder is empty or holds only hidden files.'); return; }
    if (items.length > MAX_FILES) { this.fail(`${items.length.toLocaleString()} files is over the ${MAX_FILES.toLocaleString()} file limit; drop only the robot's folder (or zip it).`); return; }
    const total = items.reduce((n, it) => n + it.file.size, 0);
    if (total > MAX_BYTES) { this.fail(`${fmtMB(total)} is over the 300 MB limit for one import.`); return; }
    this.view = 'working';
    this.rows = [{ key: 'upload', stage: 'upload', status: 'busy', text: `Uploading ${items.length} file${items.length > 1 ? 's' : ''} (${fmtMB(total)})` }];
    this.render();
    try {
      await this.start();
      let done = 0, sent = 0;
      const queue = [...items];
      const worker = async () => {
        while (queue.length) {
          const it = queue.shift();
          await this.call('PUT', `/api/import/${this.id}/file`, it.file, { path: it.path });
          done += 1; sent += it.file.size;
          this.progress = total ? sent / total : done / items.length;
          this.rows[0].text = `Uploaded ${done} of ${items.length} files (${fmtMB(sent)} of ${fmtMB(total)})`;
          this.render();
        }
      };
      await Promise.all(Array.from({ length: Math.min(PARALLEL, items.length) }, worker));
      this.rows[0].status = 'done';
      await this.detect();
    } catch (e) { this.fail(e.message); }
  }

  async detect() {
    this.view = 'working';
    this.rows.push({ key: 'detect', stage: 'detect', status: 'busy', text: 'Looking at the files…' });
    this.render();
    try {
      this.det = await this.call('POST', `/api/import/${this.id}/detect`, {});
      this.sel = 0;
      this.answers = {};
      this.view = 'review';
      this.render();
    } catch (e) { this.det = null; this.fail(e.message); }
  }

  cand() { return this.det?.candidates?.[this.sel]; }

  answersFor(c) {
    const a = {};
    if (c.kind === 'robot') {
      if (this.answers.gripper && this.answers.gripper !== c.gripper?.joint) a.gripper = this.answers.gripper;
      if (this.answers.family) a.family = this.answers.family;
      const picks = {};
      for (const [j, v] of Object.entries(this.answers.actuators || {})) if (v) picks[j] = v;
      for (const [j, info] of Object.entries(c.actuators || {})) {
        if (!info.key && !picks[j] && info.suggest && !this.answers.family) picks[j] = info.suggest;   // shown preselected
      }
      if (Object.keys(picks).length) a.actuators = picks;
      if (this.answers.mesh_scale) a.mesh_scale = this.answers.mesh_scale;
    } else if (c.kind === 'object') {
      if (this.answers.units) a.units = this.answers.units;
      if (this.answers.density_g_cc) a.density_g_cc = this.answers.density_g_cc;
    } else if (c.kind === 'bom' && this.answers.table != null) a.table = this.answers.table;
    return a;
  }

  missing(c) {
    if (c.kind !== 'robot') return [];
    const out = [];
    if (!(this.answers.gripper || c.gripper?.joint)) out.push('the gripper joint');
    const unknown = Object.entries(c.actuators || {}).filter(([j, info]) => !info.key && !info.suggest && !(this.answers.actuators || {})[j]);
    if (unknown.length && !this.answers.family) out.push(`actuators for ${unknown.map(([j]) => j).join(', ')}`);
    return out;
  }

  async commit() {
    const c = this.cand();
    if (!c) return;
    const need = this.missing(c);
    if (need.length) {
      this.error = `Pick ${need.join(' and ')} first.`;
      this.flagMissing = true;
      this.render();
      this.el.querySelector('tr.need select')?.scrollIntoView({ block: 'center' });
      return;
    }
    const name = this.el.querySelector('[data-k="name"]')?.value?.trim() || undefined;
    this.error = '';
    const btn = this.el.querySelector('[data-a="commit"]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>Saving…'; }
    try {
      const r = await this.call('POST', `/api/import/${this.id}/commit`, { candidate: c.index, name, answers: this.answersFor(c) });
      this.result = r;
      if (r.kind === 'robot' || r.kind === 'project') {
        this.building = { project: r.project, nodes: {}, done: false, title: r.title || r.project };
        this.rows = [{ key: 'commit', stage: 'commit', status: 'done', text: r.kind === 'robot' ? `Saved the robot as ${r.source}` : `Importing ${r.revisions || 1} revision(s)` },
          { key: 'build', stage: 'build', status: 'busy', text: `Building ${r.project}: about 15 to 30 seconds` }];
        this.view = 'building';
      } else {
        this.view = 'done';
        if (r.kind === 'object' && r.applied) this.s.toast?.('Object applied: the robot regenerates with it');
      }
      this.render();
    } catch (e) {
      this.error = e.message;
      this.render();
    }
  }

  // ---- rendering
  render() {
    if (!this.el) return;
    const E = this.esc;
    const head = `<div class="dlg-h"><div class="kicon">${UPLOAD_ICON}</div><div class="grow"><div class="t">Import</div>
      <div class="imp-sub">A robot (URDF, xacro, MuJoCo XML), the object to grasp (STL, OBJ, GLB, STEP), a parts list (CSV, XLSX, Markdown), a KiCad board or a Morph project. Files stay on ${this.s.S?.compute?.remote ? 'the computer running Morph' : 'this computer'}; no program in them is run (xacro macros are expanded in a sandbox).</div></div>
      <button class="btn icon ghost" type="button" data-a="close" title="Close (Esc)">${this.I('x', 'sm')}</button></div>`;
    let body = '', foot = '';
    const err = this.error ? `<div class="imp-err" role="alert">${E(this.error)}</div>` : '';
    if (this.view === 'pick') {
      body = `<div class="imp-drop" data-drop>${UPLOAD_ICON}<div class="big">Drop a folder, a zip or files here</div>
          <div class="imp-sub">A robot folder should include its meshes (and package.xml if it uses package:// paths).</div>
          <div class="btns"><button class="btn" type="button" data-a="files">Choose files</button><button class="btn" type="button" data-a="folder">Choose folder</button></div>
          <div class="fmt">Up to 300 MB and 2,000 files per import</div></div>
        <div class="imp-or">or paste a link</div>
        <form class="imp-url" data-a="url"><input type="url" name="url" placeholder="https://github.com/owner/repo  or  https://huggingface.co/owner/repo" aria-label="GitHub or Hugging Face link" autocomplete="off" spellcheck="false">
          <button class="btn primary" type="submit">Fetch</button></form>
        <div class="imp-note">github.com, raw.githubusercontent.com and huggingface.co only (public repositories).</div>
        <input type="file" data-in="files" multiple hidden><input type="file" data-in="folder" webkitdirectory multiple hidden>${err}`;
      foot = '<span class="grow"></span><button class="btn" type="button" data-a="close">Cancel</button>';
    } else if (this.view === 'working') {
      body = `<div class="imp-bar"><i style="width:${Math.round((this.progress || 0) * 100)}%"></i></div>${this.checklist(this.rows)}${err}`;
      foot = '<span class="grow"></span><button class="btn" type="button" data-a="close">Cancel</button>';
    } else if (this.view === 'review') {
      ({ body, foot } = this.review());
      if (this.error) foot = `<div class="imp-ferr" role="alert">${E(this.error)}</div>${foot.replace('<span class="grow"></span>', '')}`;
    } else if (this.view === 'building' || this.view === 'done') {
      ({ body, foot } = this.done());
      body += err;
    }
    this.disposePreview();
    this.el.innerHTML = `${head}${body}<div class="dlg-f">${foot}</div>`;
    this.wire();
  }

  checklist(rows) {
    const E = this.esc;
    return `<div class="imp-check">${rows.map((r) => {
      const st = r.status === 'busy' || r.status === 'start' || r.status === 'progress' ? 'busy' : r.status;
      const icon = st === 'busy' ? '<span class="spin"></span>' : (ROW_ICON[st] || ROW_ICON.done);
      return `<div class="imp-row ${E(st)}"><span class="dot">${icon}</span><span>${E(r.text)}</span></div>`;
    }).join('')}</div>`;
  }

  review() {
    const E = this.esc, d = this.det, c = this.cand();
    const cands = d.candidates.length > 1 ? `<div class="imp-cands" role="tablist">${d.candidates.map((x, i) => `<button type="button" role="tab" class="imp-cand${i === this.sel ? ' on' : ''}" data-cand="${i}" aria-selected="${i === this.sel}">
      <span class="tag ${x.error ? 'bad' : x.ok ? 'good' : 'warn'}">${E(KIND_LABEL[x.kind] || x.kind)}</span><span class="f">${E(x.file)}</span></button>`).join('')}</div>` : '';
    const refused = (d.refused || []).length ? `<ul class="imp-warn">${d.refused.slice(0, 3).map((t) => `<li>${E(t)}</li>`).join('')}</ul>` : '';
    let detail = '', side = '', action = '';
    if (c.error) {
      detail = `<div class="imp-err">${E(c.error)}</div>`;
    } else if (c.kind === 'robot') {
      ({ detail, side, action } = this.robotView(c));
    } else if (c.kind === 'object') {
      ({ detail, side, action } = this.objectView(c));
    } else if (c.kind === 'bom') {
      ({ detail, action } = this.bomView(c));
    } else if (c.kind === 'board') {
      ({ detail, action } = this.boardView(c));
    } else if (c.kind === 'project') {
      detail = `<label class="imp-lab" for="imp-name">Project name</label><input id="imp-name" class="imp-in" data-k="name" value="${E(c.project || '')}" maxlength="40">
        <dl class="imp-kv"><dt>Revisions</dt><dd>${E(c.revisions)}</dd><dt>Robot</dt><dd>${E(c.source || 'Morph-generated arm')}${c.robot ? ' (included)' : ''}</dd></dl>
        <div class="imp-note">The latest design is rebuilt here; results stored in the export are not trusted.</div>${this.warnings(c.warnings)}`;
      action = `<button class="btn primary" type="button" data-a="commit">Import project</button>`;
    }
    const body = `${cands}${refused}<div class="${side ? 'imp-grid' : ''}"><div>${detail}</div>${side ? `<div>${side}</div>` : ''}</div>`;
    const foot = `<button class="btn" type="button" data-a="restart">Import something else</button><span class="grow"></span><button class="btn" type="button" data-a="close">Cancel</button>${c.error ? '' : action}`;
    return { body, foot };
  }

  warnings(list) {
    const E = this.esc;
    return list?.length ? `<ul class="imp-warn">${list.slice(0, 6).map((w) => `<li>${E(w)}</li>`).join('')}</ul>` : '';
  }

  robotView(c) {
    const E = this.esc;
    const cat = this.det.catalog || [];
    const rows = [];
    const ms = c.meshes || {};
    rows.push({ status: ms.missing?.length ? 'warn' : 'done', text: ms.missing?.length ? `${ms.missing.length} of ${ms.total} meshes missing (${ms.missing.slice(0, 3).map((m) => m.ref.split('/').pop()).join(', ')}${ms.missing.length > 3 ? '…' : ''}). Searched packages: ${(ms.searched || []).join(', ') || 'none'}. Drop the folder that contains package.xml.` : `Meshes ${ms.found}/${ms.total}`, });
    if (c.format === 'xacro') rows.unshift({ status: 'done', text: 'Expanded the xacro macros (sandboxed)' });
    if (c.format === 'mjcf') rows.unshift({ status: 'done', text: 'Converted the MuJoCo model' });
    const g = c.gripper || {};
    const gj = this.answers.gripper || g.joint;
    rows.push({ status: gj ? 'done' : 'warn', text: gj ? `Gripper: ${gj}${gj === g.joint && g.why?.length ? ` (${g.why.join(', ')})` : ' (you picked it)'}` : 'No gripper found: pick the joint that opens and closes the jaws' });
    if (c.tcp) rows.push({ status: 'done', text: `Tool point: ${c.tcp.how}` });
    if (c.base) rows.push({ status: 'done', text: `Base turned ${Math.round(c.base.yaw_deg)}° so the arm reaches forward, standing on the table${c.ready ? `; ready pose found${c.ready.err_mm > 2 ? ` (${c.ready.err_mm} mm off)` : ''}` : ''}` });
    if (c.units) rows.push({ status: c.units.ok ? 'done' : 'warn', text: c.units.ok ? `Size ${Math.round(c.units.size_mm)} mm across` : `Units: ${c.units.note || 'check the size'}` });
    const lic = c.license || {};
    rows.push({ status: lic.spdx && !lic.warning ? 'done' : 'warn', text: `Licence: ${lic.summary || 'unknown'}${lic.warning ? `. ${lic.warning}` : ''}` });
    const joints = (c.joints || []).map((j) => j.name);
    const gSel = `<select data-k="gripper" aria-label="Gripper joint"><option value="">pick a joint</option>${joints.map((j) => `<option value="${E(j)}"${j === gj ? ' selected' : ''}>${E(j)}</option>`).join('')}</select>`;
    const acts = Object.entries(c.actuators || {});
    const fam = this.answers.family;
    const opt = (sel) => `<option value="">${fam ? 'sized by Morph' : 'pick a model'}</option>${FAMILIES.map(([f, label]) => `<optgroup label="${E(label)}">${cat.filter((k) => k.family === f).map((k) => `<option value="${E(k.key)}"${k.key === sel ? ' selected' : ''} title="${E(k.name)}">${E(shortName(k.key, k.name))} (${k.torque_nm} N·m)</option>`).join('')}</optgroup>`).join('')}`;
    const need = acts.some(([, a]) => !a.key);
    const actRows = acts.map(([j, a]) => {
      const pick = (this.answers.actuators || {})[j] || a.key || (fam ? '' : a.suggest) || '';
      const why = (this.answers.actuators || {})[j] ? 'you picked it' : a.key ? a.basis : fam ? `${FAMILIES.find((f) => f[0] === fam)[1]} family, sized for the worst-case pose` : a.suggest ? `suggested: ${a.suggest_basis}` : a.options ? `the files mention ${a.options.map((k) => shortName(k)).join(' and ')}` : a.basis;
      return `<tr class="${!a.key && !pick && !fam ? `need${this.flagMissing ? ' miss' : ''}` : ''}"><td class="j">${E(j)}</td><td><select data-act="${E(j)}" aria-label="Actuator for ${E(j)}">${opt(pick)}</select></td><td class="why">${E(why)}</td></tr>`;
    }).join('');
    const famBtns = need ? `<div class="imp-note" style="margin:0 0 5px">One family for every joint (Morph sizes each joint):</div><div class="imp-fam">${FAMILIES.map(([f, label, ex]) => `<button type="button" class="btn${fam === f ? ' on' : ''}" data-fam="${f}">${E(label)}<small>${E(ex)}</small></button>`).join('')}</div>` : '';
    const detail = `<label class="imp-lab" for="imp-name">Robot name</label><input id="imp-name" class="imp-in" data-k="name" value="${E(c.name || '')}" maxlength="40" spellcheck="false">
      ${this.checklist(rows)}
      <div class="imp-sec"><h4>Gripper joint<span class="grow"></span>${gSel}</h4></div>
      <div class="imp-sec"><h4>Actuators ${need ? '<span class="tag warn">your choice needed</span>' : '<span class="tag good">from the files</span>'}</h4>
        ${need ? '<div class="imp-note" style="margin:0 0 6px">The files don\'t say which motors drive the joints (URDF effort values are simulation numbers, not ratings). Pick a model per joint, or one family and Morph sizes each joint.</div>' : ''}
        ${famBtns}<table class="imp-acts">${actRows}</table></div>${this.warnings(c.warnings)}`;
    const side = `<div class="imp-prev" data-prev>${this.previewHtml(c)}<span class="cap">Ready pose · ${E(c.file.split('/').pop())}</span></div>
      <div class="imp-note">Check the size and orientation: the base sits on the table and the arm reaches to the right.${c.tcp ? ' The orange cone is the tool point, pointing the way the gripper approaches.' : ''}</div>
      ${this.s.preview3d ? '' : '<button class="btn ghost" type="button" data-a="3d" style="margin-top:6px">Rotate in 3D</button>'}`;
    return { detail, side, action: `<button class="btn primary" type="button" data-a="commit">Build robot</button>` };
  }

  objectView(c) {
    const E = this.esc, ch = c.changes || {};
    const u = this.answers.units || c.units?.unit || 'mm';
    const k = { mm: 1, cm: 10, m: 1000, in: 25.4 }[u] / ({ mm: 1, cm: 10, m: 1000, in: 25.4 }[c.units?.unit || 'mm'] || 1);
    const d = (ch['object.diameter_mm'] || 0) * k, L = (ch['object.length_mm'] || 0) * k;
    const mass = (c.mass?.g || 0) * k * k * k * (this.answers.density_g_cc || c.mass?.density_g_cc || 1) / (c.mass?.density_g_cc || 1);
    const detail = `<dl class="imp-kv"><dt>Name</dt><dd>${E(ch['object.name'])}</dd><dt>Shape</dt><dd>${E(ch['object.shape'])} (fills ${Math.round((c.fit?.fill_ratio || 0) * 100)}% of it)</dd>
        <dt>Size</dt><dd>${d.toFixed(1)} × ${L.toFixed(1)} mm</dd><dt>Mass</dt><dd>${mass.toFixed(mass < 10 ? 1 : 0)} g, estimated (volume × density)</dd></dl>
      <div class="imp-sec"><h4>Units<span class="grow"></span><select data-k="units" aria-label="Units">${['mm', 'cm', 'm', 'in'].map((x) => `<option value="${x}"${x === u ? ' selected' : ''}>${x}</option>`).join('')}</select></h4>
        <div class="imp-note">${E(c.units?.why || '')}</div></div>
      <div class="imp-sec"><h4>Density<span class="grow"></span><select data-k="density" aria-label="Density">${[['1.0', 'water-like (1.0 g/cm³)'], ['0.4', 'foam or wood (0.4)'], ['1.24', 'PLA print (1.24)'], ['2.7', 'aluminium (2.7)'], ['7.85', 'steel (7.85)']].map(([v, l]) => `<option value="${v}"${Number(v) === Number(this.answers.density_g_cc || c.mass?.density_g_cc || 1) ? ' selected' : ''}>${l}</option>`).join('')}</select></h4></div>
      ${this.warnings(c.warnings)}`;
    const side = `<div class="imp-prev" data-prev>${this.previewHtml(c)}<span class="cap">${E(c.file.split('/').pop())}</span></div>`;
    return { detail, side, action: `<button class="btn primary" type="button" data-a="commit">Use as the object</button>` };
  }

  bomView(c) {
    const E = this.esc;
    const money = (v) => (v == null ? '–' : `${c.currency === 'EUR' ? '€' : c.currency === 'GBP' ? '£' : c.currency && c.currency !== 'USD' ? `${c.currency} ` : '$'}${Number(v).toFixed(2)}`);
    const tables = (c.tables || []).length > 1 ? `<div class="imp-sec"><h4>Table<span class="grow"></span><select data-k="table">${c.tables.map((t) => `<option value="${t.index}"${t.index === (this.answers.table ?? 0) ? ' selected' : ''}>${E(t.title || `Table ${t.index + 1}`)} (${t.rows} rows)</option>`).join('')}</select></h4></div>` : '';
    const detail = `<dl class="imp-kv"><dt>Rows</dt><dd>${c.count}</dd><dt>Total</dt><dd>${money(c.total)}${c.total != null ? ' (from the listed prices)' : ''}</dd></dl>${tables}
      <table class="imp-table"><thead><tr><th>Part</th><th>Qty</th><th>Unit</th><th>Link</th></tr></thead><tbody>${(c.rows || []).slice(0, 12).map((r) => `<tr><td>${E(r.item)}${r.catalog ? ` <span class="tag acc">${E(r.catalog)}</span>` : ''}</td><td class="n">${E(r.qty)}</td><td class="n">${money(r.unit_price)}</td><td>${r.url ? `<a href="${E(r.url)}" target="_blank" rel="noopener noreferrer">${E(new URL(r.url).hostname.replace(/^www\./, ''))}</a>` : ''}</td></tr>`).join('')}</tbody></table>
      ${(c.rows || []).length > 12 ? `<div class="imp-note">and ${c.rows.length - 12} more rows</div>` : ''}${this.warnings(c.warnings)}`;
    return { detail, action: `<button class="btn primary" type="button" data-a="commit">Use this parts list</button>` };
  }

  boardView(c) {
    const E = this.esc;
    const sch = c.document === 'schematic';
    const detail = `<dl class="imp-kv"><dt>File</dt><dd>${E(c.file.split('/').pop())}</dd><dt>${sch ? 'Schematic' : 'Board'}</dt><dd>${sch ? 'no outline in a schematic' : c.board_mm ? `${E(c.board_mm[0])} × ${E(c.board_mm[1])} mm` : 'no outline on Edge.Cuts'}${c.layers ? `, ${E(c.layers)} copper layers` : ''}</dd>
        <dt>Parts</dt><dd>${E(c.footprints)} ${sch ? 'symbols' : 'placed'}, ${(c.rows || []).length} BOM lines</dd></dl>
      <table class="imp-table"><thead><tr><th>Part</th><th>Qty</th><th>Refs</th></tr></thead><tbody>${(c.rows || []).slice(0, 10).map((r) => `<tr><td>${E(r.item)}</td><td class="n">${E(r.qty)}</td><td>${E(r.refs)}</td></tr>`).join('')}</tbody></table>${this.warnings(c.warnings)}`;
    return { detail, action: `<button class="btn primary" type="button" data-a="commit">Use this board</button>` };
  }

  done() {
    const E = this.esc, b = this.building, r = this.result || {};
    if (b) {
      const st = STAGES.map(([k, label]) => `<span class="imp-stage ${E(b.nodes[k] || '')}">${E(label)}</span>`).join('');
      const fin = b.done ? `<div class="imp-row done" style="margin-top:12px"><span class="dot">${ROW_ICON.done}</span><span><b>${E(b.project)}</b> is ready: open it to check the gripper, tool point and actuators Morph derived (marked auto-calibrated).</span></div>` : '';
      const body = `${this.checklist(this.rows)}<div class="imp-stages">${st}</div>${fin}${b.error ? `<div class="imp-err">${E(b.error)}</div>` : ''}
        ${b.done ? '' : '<div class="imp-note">You can close this window; the build continues and the project appears in the project list.</div>'}`;
      const foot = `<span class="grow"></span><button class="btn" type="button" data-a="close">Close</button>${b.done ? `<button class="btn primary" type="button" data-a="openp">Open ${E(b.project)}</button>` : ''}`;
      return { body, foot };
    }
    let text = 'Done.', extra = '';
    if (r.kind === 'object') {
      const obj = `${E(r.changes?.['object.shape'])} ${E(r.changes?.['object.diameter_mm'])} × ${E(r.changes?.['object.length_mm'])} mm, about ${E(r.mass?.g)} g, estimated`;
      text = r.applied ? `The object (${obj}) is applied to this project: watch the pipeline strip regenerate it.`
        : `Read the object (${obj}). It was not applied here; type these changes in the chat to use it:`;
      if (!r.applied) extra = `<pre class="imp-pre">${E(Object.entries(r.changes || {}).map(([k, v]) => `${k}=${v}`).join(', '))}</pre>`;
    }
    if (r.kind === 'bom') text = `Read ${E((r.rows || []).length)} parts${r.total != null ? ` totalling ${E(r.total)} ${E(r.currency || '')}` : ''}.`;
    if (r.kind === 'board') text = `Read ${E((r.rows || []).length)} BOM lines${r.board_mm ? ` and a ${E(r.board_mm[0])} × ${E(r.board_mm[1])} mm outline` : ''}.`;
    const csv = (r.kind === 'bom' || r.kind === 'board') && (r.rows || []).length ? '<button class="btn" type="button" data-a="csv">Download CSV</button>' : '';
    return { body: `<div class="imp-row done" style="margin-top:14px"><span class="dot">${ROW_ICON.done}</span><span>${text}</span></div>${extra}`,
      foot: `<button class="btn" type="button" data-a="restart">Import something else</button>${csv}<span class="grow"></span><button class="btn primary" type="button" data-a="close">Close</button>` };
  }

  // the parsed rows as CSV; cells that a spreadsheet would run as a formula are quoted as text
  downloadCsv() {
    const r = this.result || {}, rows = r.rows || [];
    const cols = ['item', 'qty', 'unit_price', 'ext_price', 'currency', 'category', 'make_or_buy', 'ref', 'refs', 'value', 'footprint', 'mpn', 'manufacturer', 'supplier', 'url'];
    const cell = (v) => {
      let t = v == null ? '' : String(v);
      if (/^[=+\-@\t\r]/.test(t)) t = `'${t}`;
      return /[",\n\r]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
    };
    const used = cols.filter((k) => rows.some((x) => x[k] != null && x[k] !== ''));
    const csv = [used.join(','), ...rows.map((x) => used.map((k) => cell(x[k])).join(','))].join('\r\n') + '\r\n';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = `${(r.file || 'parts').split('/').pop().replace(/\.[^.]+$/, '') || 'parts'}-morph.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  previewHtml(c) {
    if (this.s.preview3d) return '<div data-3d style="width:100%;height:100%"></div>';
    return `<img alt="Preview of ${this.esc(c.file)}" src="${this.esc(this.url(`/api/import/${this.id}/preview.png`, { candidate: c.index }))}">`;
  }

  disposePreview() { try { this.preview?.(); } catch (e) { /* viewer gone */ } this.preview = null; }

  wire() {
    const q = (s) => this.el.querySelectorAll(s);
    q('[data-a="close"]').forEach((b) => (b.onclick = () => this.close()));
    q('[data-a="restart"]').forEach((b) => (b.onclick = () => { if (this.id) this.call('DELETE', `/api/import/${this.id}`).catch(() => {}); this.reset(); this.render(); }));
    const files = this.el.querySelector('[data-in="files"]'), folder = this.el.querySelector('[data-in="folder"]');
    q('[data-a="files"]').forEach((b) => (b.onclick = () => files?.click()));
    q('[data-a="folder"]').forEach((b) => (b.onclick = () => folder?.click()));
    [files, folder].forEach((inp) => inp && (inp.onchange = () => this.upload(filesFromInput(inp.files))));
    const form = this.el.querySelector('form[data-a="url"]');
    if (form) form.onsubmit = (e) => { e.preventDefault(); this.fetchUrl(form.url.value); };
    const drop = this.el.querySelector('[data-drop]');
    if (drop) {
      drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
      drop.ondragleave = () => drop.classList.remove('over');
      drop.ondrop = async (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove('over'); this.upload(await entriesToFiles([...e.dataTransfer.items])); };
    }
    q('[data-cand]').forEach((b) => (b.onclick = () => { this.sel = Number(b.dataset.cand); this.answers = {}; this.error = ''; this.flagMissing = false; this.render(); }));
    q('[data-k="gripper"]').forEach((s) => (s.onchange = () => { this.answers.gripper = s.value || undefined; this.render(); }));
    const settled = () => { if (this.error && !this.missing(this.cand()).length) { this.error = ''; this.flagMissing = false; } };
    q('[data-act]').forEach((s) => (s.onchange = () => { this.answers.actuators = { ...(this.answers.actuators || {}), [s.dataset.act]: s.value }; settled(); this.render(); }));
    q('[data-fam]').forEach((b) => (b.onclick = () => { this.answers.family = this.answers.family === b.dataset.fam ? undefined : b.dataset.fam; this.answers.actuators = {}; settled(); this.render(); }));
    q('[data-k="units"]').forEach((s) => (s.onchange = () => { this.answers.units = s.value; this.render(); }));
    q('[data-k="density"]').forEach((s) => (s.onchange = () => { this.answers.density_g_cc = Number(s.value); this.render(); }));
    q('[data-k="table"]').forEach((s) => (s.onchange = () => { this.answers.table = Number(s.value); this.render(); }));
    q('[data-a="commit"]').forEach((b) => (b.onclick = () => this.commit()));
    q('[data-a="openp"]').forEach((b) => (b.onclick = () => { const p = this.building?.project; this.unsubscribe(); this.building = null; this.view = 'pick'; this.close(); if (p) this.s.openProject?.(p); }));
    q('[data-a="3d"]').forEach((b) => (b.onclick = () => this.mini3d(b)));
    q('[data-a="csv"]').forEach((b) => (b.onclick = () => this.downloadCsv()));
    const host3d = this.el.querySelector('[data-3d]');
    const c = this.cand();
    if (host3d && c && this.s.preview3d) this.preview = this.s.preview3d(host3d, this.url(`/api/import/${this.id}/preview.glb`, { candidate: c.index })) || null;
    const name = this.el.querySelector('[data-k="name"]');
    if (name) name.oninput = () => { this.nameValue = name.value; };
    if (name && this.nameValue != null) name.value = this.nameValue;
    (this.el.querySelector('form[data-a="url"] input') || this.el.querySelector('[data-a="commit"]'))?.focus?.({ preventScroll: true });
  }

  // a small three.js view of the preview GLB (vendored three r160), for hosts without a viewer hook
  async mini3d(btn) {
    const box = this.el.querySelector('[data-prev]'), c = this.cand();
    if (!box || !c) return;
    btn.disabled = true;
    try {
      const base = new URL('./vendor/', import.meta.url);
      const THREE = await import(new URL('three.module.min.js', base).href);
      const { GLTFLoader } = await import(new URL('GLTFLoader.js', base).href);
      const { OrbitControls } = await import(new URL('OrbitControls.js', base).href);
      const canvas = document.createElement('canvas');
      const w = box.clientWidth, h = box.clientHeight;
      const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
      renderer.setSize(w, h, false);
      const scene = new THREE.Scene();
      scene.add(new THREE.HemisphereLight(0xffffff, 0x404050, 2.2));
      const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(1, 2, 1.5); scene.add(sun);
      const cam = new THREE.PerspectiveCamera(35, w / h, 1, 20000);
      const gltf = await new GLTFLoader().loadAsync(this.url(`/api/import/${this.id}/preview.glb`, { candidate: c.index }));
      const root = gltf.scene; root.rotation.x = -Math.PI / 2;   // robot base frame is z-up, mm
      root.traverse((o) => {
        if (!o.isMesh) return;
        if (!o.geometry.attributes.normal) o.geometry.computeVertexNormals();
        o.material = new THREE.MeshStandardMaterial({ vertexColors: !!o.geometry.attributes.color, flatShading: true, roughness: 0.65, metalness: 0.05 });
      });
      scene.add(root);
      const bb = new THREE.Box3().setFromObject(root), ctr = bb.getCenter(new THREE.Vector3()), size = bb.getSize(new THREE.Vector3()).length();
      cam.position.copy(ctr).add(new THREE.Vector3(-0.55, 0.45, 0.75).multiplyScalar(size));
      const ctl = new OrbitControls(cam, canvas); ctl.target.copy(ctr); ctl.update();
      let raf = 0;
      const loop = () => { raf = requestAnimationFrame(loop); renderer.render(scene, cam); };
      box.querySelector('img')?.remove();
      box.prepend(canvas);
      loop();
      btn.remove();
      this.preview = () => { cancelAnimationFrame(raf); ctl.dispose(); renderer.dispose(); };
    } catch (e) {
      btn.disabled = false;
      btn.textContent = '3D view unavailable here';
    }
  }
}

// ------------------------------------------------------------------ entry points
export function init(studio) {
  const imp = new Importer(studio);
  const icon = UPLOAD_ICON.replace('class="i"', 'class="i sm"');
  const header = studio.mount?.header;
  if (header) {
    const b = document.createElement('button');
    b.className = 'btn ghost';
    b.type = 'button';
    b.id = 'importBtn';
    b.title = 'Import a robot, an object, a parts list or a board';
    b.innerHTML = `${icon}<span>Import</span>`;
    b.onclick = () => imp.open();
    header.appendChild(b);
  }
  const menu = studio.mount?.projMenu;
  if (menu) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'pm-item imp-menu';
    item.innerHTML = `${icon}<span>Import robot or file…</span>`;
    item.onclick = () => imp.open();
    menu.appendChild(item);
  }
  // drop anywhere on the page (feature drop zones mark themselves with data-drop and keep their own drops)
  const overlay = document.createElement('div');
  overlay.className = 'imp-overlay';
  overlay.innerHTML = '<div>Drop to import into Morph<small>a robot folder, zip, mesh, parts list or KiCad board</small></div>';
  document.body.appendChild(overlay);
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  const busyElsewhere = () => [...document.querySelectorAll('.modal.on')].some((m) => !m.querySelector('.imp'));
  window.addEventListener('dragenter', (e) => {
    if (!hasFiles(e) || e.target.closest?.('[data-drop]') || busyElsewhere()) return;
    depth += 1;
    overlay.classList.add('on');
  });
  window.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) overlay.classList.remove('on'); });
  overlay.addEventListener('dragover', (e) => e.preventDefault());
  overlay.addEventListener('drop', async (e) => {
    e.preventDefault();
    depth = 0;
    overlay.classList.remove('on');
    const files = await entriesToFiles([...e.dataTransfer.items]);
    imp.open({ files });
  });
  return imp;
}

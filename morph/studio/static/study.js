// Robustness (beta): a study of the revision on screen, in the Simulation tab's result column under the pick-and-place
// result (contracts sections 4 and 16). A study runs the revision's own plan many times in slightly different worlds
// drawn from the project's tolerances: the card shows the success rate with its 95% interval and the honesty notes,
// each failure group with the rule that isolates it and whether it survives other simulator settings, and replays a
// failing run in the 3D view. Live progress comes from `study` events. Editors start (runs, stress), cancel, carry on
// and edit the tolerances (each range with its source); everyone else reads and replays. Talks to /api/study*
// (morph/studio/api_study.py). Without a study the card says so: one nominal run tests nothing around it.
// Feature contract: init(studio) -> { refresh(), open(), card }.

const STUDY_CSS = `
.sd-card { margin: 12px 0 14px; padding: 11px 12px 12px; border-radius: var(--r2); border: 1px solid var(--line2); background: var(--bg2); font-size: 12px; line-height: 1.45; }
.sd-h { display: flex; align-items: center; gap: 6px; margin-bottom: 7px; }
.sd-h .sd-t { font-weight: 650; font-size: 12.5px; color: var(--ink); }
.sd-h .sd-sp { flex: 1; }
.sd-link { border: 0; background: none; color: var(--accent2); font-size: 11.5px; padding: 0; }
.sd-link:hover { text-decoration: underline; }
.sd-link:disabled { color: var(--dim); text-decoration: none; cursor: default; }
.sd-none { color: var(--ink2); margin: 0 0 8px; }
.sd-head { margin: 0 0 5px; color: var(--ink); font-weight: 600; font-size: 12.4px; }
.sd-note { margin: 0 0 7px; color: var(--muted); font-size: 11.2px; }
.sd-flag { display: flex; gap: 6px; margin: 0 0 6px; padding: 6px 8px; border-radius: 7px; background: var(--warn-bg); color: #f3d79b; font-size: 11.3px; }
.sd-flag svg { flex: none; margin-top: 2px; color: var(--warn); }
.sd-g { margin: 8px 0 0; padding: 8px 9px; border-radius: 8px; border: 1px solid var(--line2); background: var(--bg1); }
.sd-g .sd-gl { font-weight: 600; color: var(--ink); font-size: 11.8px; }
.sd-g .sd-gs { color: var(--muted); font-size: 11px; margin-top: 1px; }
.sd-g .sd-rule { margin-top: 4px; color: var(--ink2); font-size: 11.3px; }
.sd-g .sd-ms { margin-top: 5px; font-size: 11px; color: var(--ink2); }
.sd-g .sd-ms .tag { margin-right: 4px; }
.sd-g .sd-ck { margin-top: 3px; color: var(--dim); font-size: 10.6px; }
.sd-g .sd-acts { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.sd-card .btn { height: 26px; padding: 0 9px; font-size: 11.5px; }
.sd-run .sd-line { display: flex; align-items: center; gap: 7px; color: var(--ink); font-weight: 560; }
.sd-bar { height: 5px; border-radius: 3px; background: var(--bg4); margin: 7px 0 6px; overflow: hidden; }
.sd-bar b { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--cyan)); transition: width .3s ease; }
.sd-so { color: var(--ink2); }
.sd-fails { color: var(--muted); font-size: 11px; margin-top: 3px; }
.sd-meta { color: var(--dim); font-size: 10.8px; margin-top: 4px; }
.sd-start { margin-top: 9px; padding-top: 9px; border-top: 1px solid var(--line2); display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; }
.sd-start select { height: 26px; border-radius: 7px; border: 1px solid var(--line3); background: var(--bg1); color: var(--ink); padding: 0 6px; font-size: 11.5px; }
.sd-start label { display: inline-flex; align-items: center; gap: 5px; color: var(--ink2); font-size: 11.5px; cursor: pointer; }
.sd-start .sd-est { flex: 1 0 100%; color: var(--dim); font-size: 10.8px; }
.sd-foot { margin-top: 8px; color: var(--dim); font-size: 10.6px; }
.sd-replay { margin: 0 0 9px; padding: 8px 9px; border-radius: 8px; border: 1px solid var(--hot-line); background: var(--hot-bg); }
.sd-replay b { color: var(--ink); }
.sd-replay .sd-rt { display: block; color: var(--muted); font: 10.8px var(--mono); margin-top: 2px; }
.sd-replay .sd-kv { margin-top: 4px; color: var(--ink2); font-size: 11px; }
.sd-replay .sd-acts { margin-top: 6px; }
.sd-stress { margin-top: 8px; color: var(--ink2); font-size: 11.2px; }
body.tm-readonly .sd-edit { display: none !important; }
.sd-dlg { width: min(980px, calc(100vw - 32px)); }
.sd-dlg h4 { margin: 16px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--dim); }
.sd-dlg .sd-head { font-size: 13.5px; }
.sd-tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.sd-tbl th { text-align: left; color: var(--dim); font-weight: 600; font-size: 11px; padding: 4px 6px; border-bottom: 1px solid var(--line2); }
.sd-tbl td { padding: 5px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
.sd-tbl td.src { color: var(--muted); font-size: 11.3px; }
.sd-tbl tr.off td:not(:last-child) { opacity: .45; }
.sd-tbl tr.edited td:first-child::after { content: " (edited)"; color: var(--warn); font-size: 10.5px; }
.sd-tbl input[type=number] { width: 74px; height: 24px; border-radius: 6px; border: 1px solid var(--line3); background: var(--bg1); color: var(--ink); padding: 0 6px; font: 11.5px var(--mono); }
.sd-tbl .u { color: var(--dim); font-size: 10.8px; margin-left: 3px; }
.sd-dlg .sd-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 10px; }
.sd-dlg .sd-g { margin-top: 8px; }
.sd-infl { display: grid; grid-template-columns: minmax(0, 1fr) 140px 104px; gap: 3px 10px; font-size: 11.5px; align-items: center; }
.sd-infl i { display: block; height: 6px; border-radius: 3px; background: var(--accent); }
.sd-infl i.dim { background: var(--line3); }
.sd-list { font-size: 11.8px; }
.sd-list div { display: flex; gap: 10px; padding: 4px 0; border-bottom: 1px solid var(--line); }
.sd-list span:first-child { font-family: var(--mono); color: var(--muted); min-width: 52px; }
@media (max-width: 720px) { .sd-tbl td.src { display: none; } .sd-tbl th.src { display: none; } }
`;

const LIVE = new Set(['queued', 'preparing', 'running', 'checking']);
const RUNS = [50, 200, 1000];
const PHASE = { approach: 'approach', grasp: 'grasp', lift: 'lift', move: 'carry', place: 'set-down', release: 'release', retreat: 'way back' };

// interval bounds rounded away from the estimate (as api_study.interval): what is shown always contains the computed one
const bound = (v, down, fine) => { const k = fine ? 10 : 1; const x = (down ? Math.floor : Math.ceil)(Math.round(v * 100 * k * 1e6) / 1e6) / k; return fine ? x.toFixed(1) : x.toFixed(0); };
const likely = (lo, hi) => { const fine = hi - lo < 0.04; return `likely between ${bound(lo, true, fine)}% and ${bound(hi, false, fine)}%`; };
const INTERVAL_NOTE = 'Likely: the 95% confidence interval. Over the tolerances shown, the true rate is in this range with 95% confidence; fewer runs give a wider range.';
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const mins = (s) => (s < 90 ? `${Math.max(5, Math.round(s / 5) * 5)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

function injectCSS() {
  if (document.getElementById('study-css')) return;
  const st = document.createElement('style');
  st.id = 'study-css';
  st.textContent = STUDY_CSS;
  document.head.appendChild(st);
}

class StudyCard {
  constructor(studio) {
    this.s = studio;
    this.esc = studio.esc || ((x) => String(x ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`));
    this.I = studio.I || (() => '');
    this.el = document.createElement('section');
    this.el.className = 'sd-card';
    this.el.id = 'studyCard';
    this.el.addEventListener('click', (e) => this.click(e));
    this.el.addEventListener('change', (e) => { if (e.target.dataset.f) this.form[e.target.dataset.f] = e.target.type === 'checkbox' ? e.target.checked : +e.target.value; this.render(); });
    this.info = null; this.ev = null; this.rp = null; this.form = { runs: 200, stress: false }; this.gen = 0; this.rev = null; this.dlg = null;
    studio.on('study', (ev) => this.onEvent(ev));
    studio.on('done', () => this.refresh());
    studio.on('project', () => { this.ev = null; this.rp = null; this.info = null; this.refresh(); });
    this.attach();
    this.refresh();
    setInterval(() => this.watch(), 1000);
  }

  // ---- where the card lives: under the Simulation tab's pick-and-place result (the tab redraws that column, so the
  // card is put back each time; it keeps its own state)
  attach() {
    const side = document.querySelector('#views .view[data-view="sim"] .sim .side');
    if (!side) return;
    const place = () => {
      const scen = document.querySelector('#views .view[data-view="sim"] [data-scen] button.on')?.dataset.n;
      const ok = scen === 'pick_place' && !!this.s.S?.current;
      if (!ok) { if (this.el.parentElement) this.el.remove(); return; }
      if (this.el.parentElement !== side || this.el.previousElementSibling !== side.querySelector(':scope > .success')) {
        const after = side.querySelector(':scope > .success');
        if (after) after.after(this.el); else side.prepend(this.el);
      }
      if (this.s.S.current.rev !== this.rev) this.refresh();
    };
    new MutationObserver(place).observe(side, { childList: true });
    place();
  }

  viewer() { return this.s.viewer || window.morph?.viewer; }

  async refresh() {
    const rev = this.s.S?.current?.rev || null;
    const gen = ++this.gen;
    this.rev = rev;
    if (!rev) { this.info = null; this.render(); return; }
    try {
      const info = await this.s.api.get('/api/study', { rev });
      if (gen !== this.gen) return;
      this.info = info;
      const ev = info.job?.event;
      if (ev && LIVE.has(info.job.status)) this.ev = ev;
      else if (this.ev && !LIVE.has(this.ev.status)) this.ev = null;
    } catch (e) {
      if (gen !== this.gen) return;
      this.info = null;                              // no study routes (the offline demo page): the note alone
    }
    this.render();
    if (this.dlg?.isConnected && !this.dlgEdited) this.renderDialog();  // never over tolerances being typed
  }

  onEvent(ev) {
    const was = this.ev;
    this.ev = ev;
    if (LIVE.has(ev.status)) { if (!(was && LIVE.has(was.status) && this.patchRun(ev))) this.render(); return; }
    if (was && LIVE.has(was.status) && ev.status === 'done' && ev.summary?.n) {
      const s = ev.summary;
      this.s.toast(`Robustness study of ${ev.rev} done: succeeds in ${s.successes} of ${plural(s.n, 'run')}`);
    }
    if (ev.status === 'error') this.s.toast(`The robustness study stopped: ${ev.error || 'an error'}`);
    this.refresh();
  }

  // ---- the card
  render() {
    const esc = this.esc, I = this.I, info = this.info, ev = this.ev, may = !!info?.may?.start;
    let h = `<div class="sd-h"><span class="sd-t">Robustness</span><span class="tag acc">beta</span><span class="sd-sp"></span>${info ? '<button type="button" class="sd-link" data-a="details">Details</button>' : ''}</div>`;
    if (this.rp) h += this.replayHTML();
    const live = ev && LIVE.has(ev.status);
    const shown = info?.latest;
    if (live) h += this.runHTML(ev, may);
    if (!shown && !live) h += `<p class="sd-none">${esc(info?.notes?.none || "One nominal run. Small differences in placement or hardware aren't tested.")}</p>`;
    if (shown && !(live && ev.rev === this.rev)) h += this.resultHTML(shown, may);   // while a study of this revision runs, its earlier result waits (Details has it)
    if (info?.latest_stress && !live) {
      const c = info.latest_stress.card;
      h += `<div class="sd-stress">${esc(c.headline || 'Stress test: no counted runs yet.')} <button type="button" class="sd-link" data-a="details">Details</button></div>`;
    }
    for (const s of info?.stopped || []) {      // stopped studies other than the one shown (that one says so itself)
      if (s.rev !== this.rev || live || s.id === shown?.id) continue;
      h += `<div class="sd-flag">${I('alert', 'sm')}<span>A ${s.stress ? 'stress ' : ''}study stopped after ${s.done} of ${plural(s.n, 'run')}.${may ? ` <button type="button" class="sd-link sd-edit" data-a="resume" data-id="${esc(s.id)}">Carry on</button>` : ''}</span></div>`;
    }
    if (info && !live && may) h += this.startHTML();
    else if (info && !live && !shown) h += '<p class="sd-note">An editor can run a study of this revision.</p>';
    this.el.innerHTML = h;
  }

  // the running study's lines; the card patches them in place on each event (a redraw would take the Cancel button
  // from under a click)
  runParts(ev) {
    const info = this.info || {}, n = ev.n || 0, done = ev.done || 0;
    const what = ev.status === 'queued' ? "Waiting: another project's study runs first (one at a time on this computer)."
      : ev.phase === 'prepare' || ev.status === 'preparing' ? 'Preparing: rebuilding the robot and its plan.'
        : ev.phase === 'checks' ? `Checking failures with other simulator settings (half the timestep, then softer friction): ${done} of ${n}.`
          : `Run ${done} of ${n}${ev.stress ? ' (stress test)' : ''}.`;
    const k = ev.successes || 0, m = k + (ev.failures || 0), labels = info.group_labels || {};
    const meta = [ev.eta_s != null && ev.status !== 'queued' ? `About ${mins(ev.eta_s)} left (estimated)` : '',
      ev.first_result_s != null ? `first result after ${(+ev.first_result_s).toFixed(1)} s` : '',
      info.host ? `on ${info.host.remote ? `${info.host.name}, the Studio's computer` : 'this computer'}` : '',
      ev.rev !== this.rev ? `study of ${ev.rev}` : ''].filter(Boolean).join(' · ');
    return {
      what, meta, bar: `${(ev.phase === 'checks' ? 100 : n ? Math.min(100, (done / n) * 100) : 0).toFixed(1)}%`,
      so: m && ev.ci ? `So far ${k} of ${plural(m, 'run')} ${k === 1 ? 'succeeds' : 'succeed'} (${Math.round((k / m) * 100)}%; ${likely(ev.ci[0], ev.ci[1])}).` : '',
      fails: Object.entries(ev.groups || {}).sort((a, b) => b[1] - a[1]).map(([g, c]) => `${labels[g] || g}: ${c}`).join(' · '),
      unstable: ev.unstable ? `${plural(ev.unstable, 'run')} numerically unstable (not counted)` : '',
    };
  }

  runHTML(ev, may) {
    const esc = this.esc, p = this.runParts(ev);
    const line = (key, cls) => `<div class="${cls}" data-p="${key}"${key === 'so' ? ` title="${esc(this.info?.notes?.interval || INTERVAL_NOTE)}"` : ''}${p[key] ? '' : ' hidden'}>${esc(p[key])}</div>`;
    return `<div class="sd-run" data-sid="${esc(ev.id)}"><div class="sd-line"><span class="spin"></span><span data-p="what">${esc(p.what)}</span></div>
      <div class="sd-bar"><b data-p="bar" style="width:${p.bar}"></b></div>
      ${line('so', 'sd-so')}${line('fails', 'sd-fails')}${line('unstable', 'sd-fails')}${line('meta', 'sd-meta')}
      ${may ? '<div class="sd-start sd-edit" style="border:0;padding-top:0"><button type="button" class="btn" data-a="cancel">Cancel</button><span class="sd-est">Finished runs are kept; the same settings carry on later.</span></div>' : ''}</div>`;
  }

  patchRun(ev) {     // true when the card already shows this study's progress and only its lines changed
    const box = this.el.querySelector('.sd-run');
    if (!box || box.dataset.sid !== String(ev.id)) return false;
    const p = this.runParts(ev);
    for (const el of box.querySelectorAll('[data-p]')) {
      const key = el.dataset.p;
      if (key === 'bar') { el.style.width = p.bar; continue; }
      if (el.textContent !== p[key]) el.textContent = p[key];
      el.hidden = !p[key];
    }
    return true;
  }

  groupHTML(g, sid, wide = false) {
    const esc = this.esc;
    const badge = g.model_sensitive ? '<span class="tag warn">Model-sensitive</span>' : g.model_sensitive === false ? '<span class="tag good">Same with other simulator settings</span>' : '';
    const busy = this.rp?.busy && this.rp.sid === sid && this.rp.run === g.example_run;
    return `<div class="sd-g"><div class="sd-gl">${esc(g.label)}</div><div class="sd-gs" title="${esc(this.info?.notes?.interval || INTERVAL_NOTE)}">${esc(g.share)}</div>
      ${g.rule ? `<div class="sd-rule">Most likely when: ${esc(g.rule)}</div>` : ''}
      <div class="sd-ms">${badge}${esc(g.sensitivity)}</div>${wide && g.checks ? `<div class="sd-ck">${esc(g.checks)}</div>` : ''}
      ${g.example_run != null ? `<div class="sd-acts"><button type="button" class="btn" data-a="replay" data-id="${esc(sid)}" data-run="${esc(g.example_run)}" ${this.rp?.busy ? 'disabled' : ''}>${busy ? '<span class="spin"></span>Preparing the replay' : `Replay run ${esc(g.example_run)}`}</button></div>` : ''}</div>`;
  }

  resultHTML(doc, may) {
    const esc = this.esc, I = this.I, c = doc.card || {};
    const groups = c.groups || [];
    let h = c.headline ? `<p class="sd-head" title="${esc(c.interval_note || INTERVAL_NOTE)}">${esc(c.headline)}</p><p class="sd-note">${esc(c.note)}</p>` : `<p class="sd-none">${esc(this.info?.notes?.none || '')}</p>`;
    const resume = may && (this.info?.stopped || []).some((s) => s.id === doc.id)
      ? ` <button type="button" class="sd-link sd-edit" data-a="resume" data-id="${esc(doc.id)}">Carry on</button>` : '';
    (c.flags || []).forEach((f, i) => { h += `<div class="sd-flag">${I('alert', 'sm')}<span>${esc(f)}${i === (c.flags.length - 1) ? resume : ''}</span></div>`; });
    h += groups.slice(0, 2).map((g) => this.groupHTML(g, doc.id)).join('');
    if (groups.length > 2) h += `<div class="sd-foot">${plural(groups.length - 2, 'more failure group')}: see Details.</div>`;
    const t = doc.timing || {};
    h += `<div class="sd-foot">${plural(doc.done || 0, 'run')} of ${esc(doc.rev)} at ${(doc.timestep * 1000).toFixed(doc.timestep < 0.001 ? 1 : 0)} ms steps, seed ${esc(doc.seed)}${t.workers ? ` · ${t.runs_per_s ? `${esc(t.runs_per_s)} runs/s on ` : ''}${plural(t.workers, 'worker process', 'worker processes')}` : ''}</div>`;
    return h;
  }

  estimate(runs) {
    const info = this.info || {}, w = info.limits?.workers || 1, per = info.rate?.per_worker || 0.7;
    const checks = Math.min(2 * runs, 110);          // failures (up to 20 a group) and 15 successes, twice
    return 6 + (runs + checks) / Math.max(0.05, per * w);
  }

  startHTML() {
    const esc = this.esc, info = this.info, f = this.form, w = info.limits?.workers || 1, cur = info.latest;
    const host = info.host?.remote ? `${info.host.name}, the Studio's computer` : 'this computer';
    // the card's studies use seed 1 and 1 ms steps: the same settings again deepen that study instead of repeating it
    const same = cur && !cur.outdated && !f.stress && cur.seed === 1 && cur.timestep === 0.001;
    const todo = same ? f.runs - cur.done : f.runs, done = todo <= 0;
    const label = f.stress ? 'Run a stress test' : !cur ? 'Run a study' : cur.outdated ? 'Run again with these tolerances'
      : same && !done ? `Run ${todo} more` : same ? `Done: ${cur.done} runs` : 'Run a study';
    const runs = RUNS.includes(f.runs) ? RUNS : [...RUNS, f.runs].sort((a, b) => a - b);
    return `<div class="sd-start sd-edit"><select data-f="runs" aria-label="Runs">${runs.map((r) => `<option value="${r}"${r === f.runs ? ' selected' : ''}>${r} runs</option>`).join('')}</select>
      <label title="Every tolerance range doubled about its nominal value"><input type="checkbox" data-f="stress"${f.stress ? ' checked' : ''}>Stress test</label>
      <button type="button" class="btn primary" data-a="start"${done ? ' disabled' : ''}>${esc(label)}</button>
      <button type="button" class="sd-link" data-a="tolerances">Tolerances</button>
      <span class="sd-est">${done ? 'This study already has these runs: choose more to deepen it.'
        : `About ${mins(this.estimate(todo))} on ${esc(host)} (${plural(w, 'worker process', 'worker processes')}, estimated).`}</span></div>`;
  }

  replayHTML() {
    const esc = this.esc, r = this.rp, rb = r.doc?.robustness;
    if (!rb) return `<div class="sd-replay"><span class="spin"></span> <b>Preparing the replay of run ${r.run}</b><span class="sd-rt">A few seconds: the run is simulated again on ${this.info?.host?.remote ? "the Studio's computer" : 'this computer'}.</span></div>`;
    const top = this.params(rb.params, r.sid).slice(0, 4).join(' · ');
    const differs = rb.matches_record === false ? `<div class="sd-kv" style="color:var(--warn)">This replay ended otherwise than the recorded run (${esc(rb.recorded?.outcome || '?')}): Morph's simulation changed since the study.</div>` : '';
    return `<div class="sd-replay"><b>Replay of run ${r.run}</b> · ${esc(rb.success ? 'placed' : rb.label || rb.outcome)}
      <span class="sd-rt" data-rt>${r.no3d ? "The 3D view can't play it here; the run's numbers are below." : '0.0 s'}</span>
      <div class="sd-kv">${esc(top)}</div>${differs}
      <div class="sd-acts"><button type="button" class="btn" data-a="stopreplay">Back to the nominal run</button></div></div>`;
  }

  // a run's world, the biggest departures from nominal first ("egg turned 6.2 deg")
  params(values, sid) {
    const info = this.info || {}, own = [info.latest, info.latest_stress].find((d) => d?.id === sid) || info.latest;
    const spec = own?.spec?.params || [];
    return spec.filter((e) => e.on && values?.[e.name] != null).map((e) => {
      const v = values[e.name], span = (e.hi - e.lo) || 1, off = Math.abs(v - Math.min(Math.max(e.nominal, e.lo), e.hi)) / span;
      const txt = e.unit === 'x' ? `${e.label.toLowerCase()} ${Math.round(v * 100)}%` : `${e.label.toLowerCase()} ${(+v).toFixed(Math.abs(v) >= 100 ? 0 : 1)} ${e.unit}`;
      return [off, txt];
    }).sort((a, b) => b[0] - a[0]).map((x) => x[1]);
  }

  watch() {  // a replay ends when the tab plays something else (another scenario, another tab, a new revision)
    if (!this.rp?.doc || this.rp.no3d) return;
    const v = this.viewer()?.v;
    if (v && v._traj?.src !== this.rp.doc) { this.rp = null; this.render(); }
  }

  frame(info) {
    const el = this.el.querySelector('[data-rt]');
    if (el) el.textContent = `${info.t.toFixed(1)} / ${info.duration.toFixed(1)} s${info.phase ? ` · ${PHASE[info.phase] || info.phase}` : ''}`;
  }

  async replay(sid, run) {
    if (this.rp?.busy) return;
    this.rp = { sid, run, busy: true };
    this.render();
    try {
      const doc = await this.s.api.get('/api/study/replay', { id: sid, run });
      if (this.rp?.sid !== sid || this.rp?.run !== run) return;
      const rb = doc.robustness || {};
      doc.title = `Study run ${run} (replay): ${rb.success ? 'placed' : rb.label || rb.outcome}`;
      const v = this.viewer();
      this.rp = { sid, run, doc };
      if (!v?.canDrive?.()) this.rp.no3d = true;
      else {
        await v.play(doc, { loop: true, speed: 1, onFrame: (f) => this.frame(f) });
        if (v.v?._traj?.src !== doc) this.rp.no3d = true;     // no robot model on screen to play it with
      }
    } catch (e) {
      this.s.toast(`Couldn't replay run ${run}: ${e.message || e}`);
      this.rp = null;
    }
    this.render();
  }

  stopReplay() {
    this.rp = null;
    this.render();
    document.querySelector('#views .view[data-view="sim"] [data-scen] button.on')?.click();  // the Simulation tab plays its own run again
  }

  async post(path, body, ok) {
    try { const r = await this.s.api.post(path, body); if (ok) this.s.toast(ok); return r; } catch (e) { this.s.toast(e.message || String(e)); return null; }
  }

  async click(e) {
    const b = e.target.closest('[data-a]');
    if (!b || b.disabled) return;
    const a = b.dataset.a;
    if (a === 'details') return this.open();
    if (a === 'tolerances') return this.open('tolerances');
    if (a === 'replay') { this.s.modal?.close?.(); return this.replay(b.dataset.id, +b.dataset.run); }
    if (a === 'stopreplay') return this.stopReplay();
    if (a === 'cancel') { b.disabled = true; await this.post('/api/study/cancel', {}, 'Stopping the study: finished runs are kept'); return; }
    if (a === 'resume') { b.disabled = true; await this.post('/api/study/resume', { id: b.dataset.id }); return this.refresh(); }
    if (a === 'start') {
      b.disabled = true;
      const r = await this.post('/api/study', { runs: this.form.runs, stress: !!this.form.stress, rev: this.rev });
      if (r?.job) { this.ev = r.job.event || { status: 'queued', n: r.job.n, done: 0, rev: r.job.rev }; this.render(); }
      else this.render();
      // the Run button may have been scrolled to below the fold: the progress and Cancel take its place in view
      this.el.querySelector('.sd-run')?.scrollIntoView({ block: 'nearest' });
    }
  }

  // ---- the dialog: the whole study, the tolerances (editable for editors) and the project's studies
  async open(focus = '') {
    if (!this.info) await this.refresh();
    if (!this.info) return;
    const dlg = document.createElement('div');
    dlg.className = 'glass dlg sd-dlg';
    dlg.addEventListener('click', (e) => this.dialogClick(e));
    dlg.addEventListener('input', () => { this.dlgEdited = true; });
    dlg.addEventListener('change', (e) => this.dialogChange(e));
    this.dlg = dlg;
    this.spec = null;
    this.s.modal.open(dlg, { wide: true, onClose: () => { this.dlg = null; } });
    this.renderDialog();
    try { this.spec = (await this.s.api.get('/api/study/tolerances', { rev: this.rev })).spec; } catch (e) { this.spec = { error: e.message }; }
    this.renderDialog();
    if (focus) dlg.querySelector(`[data-sec="${focus}"]`)?.scrollIntoView({ block: 'start' });
  }

  renderDialog() {
    const dlg = this.dlg, esc = this.esc, info = this.info || {}, doc = info.latest, c = doc?.card, may = !!info.may?.start;
    if (!dlg) return;
    this.dlgEdited = false;
    let h = `<div class="dlg-h"><div style="flex:1"><div class="t">Robustness study (beta) · ${esc(info.project || '')} ${esc(this.rev || '')}</div>
      <div class="s">${esc(info.notes?.beta || '')}</div></div><button type="button" class="btn ghost icon" data-a="close" title="Close">${this.I('x', 'sm')}</button></div>`;
    if (c) {
      h += `<p class="sd-head">${esc(c.headline || '')}</p><p class="sd-note">${esc(c.note)}</p>`;
      for (const f of c.flags || []) h += `<div class="sd-flag">${this.I('alert', 'sm')}<span>${esc(f)}</span></div>`;
      h += `<h4>Failure groups</h4>${(c.groups || []).map((g) => this.groupHTML(g, doc.id, true)).join('') || '<p class="sd-note">No run failed.</p>'}`;
      const infl = (doc.summary?.sensitivity || []).slice(0, 6);
      if (infl.length) {
        const top = Math.max(...infl.map((x) => x.J), doc.summary.chance_J || 0, 0.01);
        h += `<h4>What matters most</h4><p class="sd-note">How well one range on its own separates failures from successes (Youden's J); below the chance level (${(doc.summary.chance_J || 0).toFixed(2)}) a pattern could be noise.</p>
          <div class="sd-infl">${infl.map((x) => `<span>${esc(x.label)}</span><span><i class="${x.significant ? '' : 'dim'}" style="width:${Math.max(3, (x.J / top) * 100).toFixed(0)}%"></i></span><span class="mono">${x.J.toFixed(2)}${x.significant ? '' : ' (chance)'}</span>`).join('')}</div>`;
      }
      const t = doc.timing || {};
      if (t.workers) h += `<p class="sd-foot">${plural(doc.done || 0, 'run')}${t.checks_runs ? ` and ${t.checks_runs} check runs` : ''} on ${esc(info.host?.remote ? info.host.name : 'this computer')} with ${plural(t.workers, 'worker process', 'worker processes')}${t.runs_per_s ? ` (${esc(t.runs_per_s)} runs/s)` : ''}${t.first_result_s != null ? `, first result after ${(+t.first_result_s).toFixed(1)} s` : ''}. Seed ${esc(doc.seed)}, ${(doc.timestep * 1000).toFixed(doc.timestep < 0.001 ? 1 : 0)} ms steps.</p>`;
    } else h += `<p class="sd-none">${esc(info.notes?.none || '')}</p>`;
    if (info.latest_stress?.card) h += `<h4>Stress test</h4><p class="sd-head">${esc(info.latest_stress.card.headline || '')}</p>${(info.latest_stress.card.groups || []).map((g) => this.groupHTML(g, info.latest_stress.id, true)).join('')}`;
    h += `<h4 data-sec="tolerances">Tolerances</h4><p class="sd-note">Each run draws every value from these ranges (% means a share of the nominal value). ${may ? 'Changing one makes the studies run with the old ranges outdated; the next study uses the new ones.' : ''}</p>${this.tolerancesHTML(may)}`;
    const list = (info.studies || []).slice(0, 12);
    if (list.length) {
      h += `<h4>Studies of ${esc(info.project || 'this project')}</h4><div class="sd-list">${list.map((s) => `<div><span>${esc(s.rev)}</span><span>${esc((s.updated_utc || s.created_utc || '').replace('T', ' ').replace('Z', ' UTC'))}</span><span>${s.p != null ? `${Math.round(s.p * 100)}% of ${s.done}` : `${s.done || 0} of ${s.n}`}${s.stress ? ' · stress' : ''}</span><span>${esc(s.status)}${s.outdated ? ' · outdated' : ''}${s.rev_changed || s.rev_missing ? ' · revision gone' : ''}</span></div>`).join('')}</div>`;
    }
    dlg.innerHTML = h;
  }

  tolerancesHTML(may) {
    const esc = this.esc, spec = this.spec;
    if (!spec) return '<p class="sd-note"><span class="spin"></span> Loading the tolerances</p>';
    if (spec.error) return `<p class="sd-note">${esc(spec.error)}</p>`;
    const show = (e, v) => (e.unit === 'x' ? Math.round(v * 1000) / 10 : +(+v).toFixed(3));
    const unit = (e) => (e.unit === 'x' ? '%' : e.unit);              // x: a share of the nominal value
    const rows = spec.params.map((e) => {
      const range = may ? `<input type="number" step="any" data-t="${esc(e.name)}" data-k="lo" value="${show(e, e.lo)}" aria-label="${esc(e.label)} low"> to <input type="number" step="any" data-t="${esc(e.name)}" data-k="hi" value="${show(e, e.hi)}" aria-label="${esc(e.label)} high"><span class="u">${esc(unit(e))}</span>`
        : `${show(e, e.lo)} to ${show(e, e.hi)}<span class="u">${esc(unit(e))}</span>`;
      return `<tr class="${e.on ? '' : 'off'}${e.edited ? ' edited' : ''}"><td>${esc(e.label)}</td><td>${range}</td><td class="mono">${show(e, e.nominal)}${e.unit === 'x' ? '%' : ''}</td><td class="src">${esc(e.source)}</td>
        <td><input type="checkbox" data-t="${esc(e.name)}" data-k="on"${e.on ? ' checked' : ''}${may ? '' : ' disabled'} aria-label="Vary ${esc(e.label)}"></td></tr>`;
    }).join('');
    const presets = spec.world_preset ? (spec.presets || []) : [];
    const pl = (p) => spec.preset_labels?.[p] || p;
    const pre = presets.length > 1 && may
      ? `<div class="sd-row sd-edit"><label class="sd-note" style="margin:0">World tolerances <select data-f="preset" aria-label="World tolerances">${presets.map((p) => `<option value="${esc(p)}"${p === spec.world_preset ? ' selected' : ''}>${esc(pl(p))}</option>`).join('')}</select></label></div>`
      : (spec.world_preset ? `<p class="sd-note">World tolerances: ${esc(pl(spec.world_preset))}.</p>` : '');
    return `${pre}<table class="sd-tbl"><thead><tr><th>Tolerance</th><th>Range</th><th>Nominal</th><th class="src">Where the range comes from</th><th>Vary</th></tr></thead><tbody>${rows}</tbody></table>
      ${may ? '<div class="sd-row sd-edit"><button type="button" class="btn primary" data-a="savetol">Save tolerances</button><button type="button" class="btn" data-a="resettol">Back to the defaults</button><span class="sd-note" data-out="tol" style="margin:0"></span></div>' : ''}`;
  }

  async dialogChange(e) {
    const t = e.target;
    if (t.dataset?.f !== 'preset' || !t.value || t.value === this.spec?.world_preset) return;
    t.disabled = true;
    try {
      this.spec = (await this.s.api.post('/api/study/tolerances', { changes: { world_preset: t.value }, rev: this.rev })).spec;
      await this.refresh();
      this.renderDialog();
    } catch (err) {
      t.disabled = false;
      this.s.toast(err.message || String(err));
    }
  }

  async dialogClick(e) {
    const b = e.target.closest('[data-a]');
    if (!b || b.disabled) return;
    const a = b.dataset.a;
    if (a === 'close') return this.s.modal.close();
    if (a === 'replay') { this.s.modal.close(); return this.replay(b.dataset.id, +b.dataset.run); }
    if (a === 'savetol' || a === 'resettol') {
      let changes = { reset: true };
      if (a === 'savetol') {
        changes = {};
        for (const e of this.spec.params) {
          const q = (k) => this.dlg.querySelector(`[data-t="${CSS.escape(e.name)}"][data-k="${k}"]`);
          const lo = parseFloat(q('lo')?.value), hi = parseFloat(q('hi')?.value), on = !!q('on')?.checked;
          const k = e.unit === 'x' ? 100 : 1;
          const ch = {};
          if (Number.isFinite(lo) && Math.abs(lo / k - e.lo) > 1e-9) ch.lo = lo / k;
          if (Number.isFinite(hi) && Math.abs(hi / k - e.hi) > 1e-9) ch.hi = hi / k;
          if (on !== e.on) ch.on = on;
          if (Object.keys(ch).length) changes[e.name] = ch;
        }
        if (!Object.keys(changes).length) { this.dlg.querySelector('[data-out="tol"]').textContent = 'Nothing changed.'; return; }
      }
      b.disabled = true;
      try {
        this.spec = (await this.s.api.post('/api/study/tolerances', { changes, rev: this.rev })).spec;
        await this.refresh();
        this.renderDialog();
        this.dlg?.querySelector('[data-out="tol"]') && (this.dlg.querySelector('[data-out="tol"]').textContent = a === 'savetol' ? 'Saved.' : 'Back to the defaults.');
      } catch (err) {
        b.disabled = false;
        const out = this.dlg?.querySelector('[data-out="tol"]');
        if (out) out.textContent = err.message || String(err); else this.s.toast(err.message || String(err));
      }
    }
  }
}

export function init(studio) {
  injectCSS();
  const card = new StudyCard(studio);
  window.morphStudy = card;
  return { refresh: () => card.refresh(), open: (focus) => card.open(focus), card };
}

export default init;

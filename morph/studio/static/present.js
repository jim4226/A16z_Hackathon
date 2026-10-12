// Present mode: the demo as a press-through script. Each press of → (or Space, PageDown, a clicker)
// runs the next step for real (switches the view, types the chat request, flips a build config,
// asks Darwin) and shows a caption that says what Morph is doing. Chat and parameter steps hold
// the Next button until the pipeline has finished regenerating, so a step is never skipped mid-build.
import { I, esc, fmt, sleep } from './panels.js';

const $ = (s, r = document) => r.querySelector(s);
const ZOOM_OUT = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? 'Cmd and -' : 'Ctrl and -';

// Steps mirror docs/DEMO_SCRIPT.md. `say` may be a function of the app state (for live numbers).
const STEPS = [
  {
    title: 'One sentence in, a whole robot out', project: 'egg-picker',
    say: 'Morph generated this 5-axis egg-picking arm from one request: CAD, physics simulation, electronics, firmware, manufacturing, sourcing and an assembly manual. Every stage above was built for this robot.',
    run: async (a) => { a.tab('robot'); a.robot({ motion: false, xray: false, labels: false }); a.explode(0); a.home(); },
  },
  {
    title: 'Real kinematics, real parts inside', project: 'egg-picker',
    say: 'Everything that moves is real kinematics: five servo joints and a rack-and-pinion gripper. X-ray shows the electronics: the controller in the base, the servo bus, cables through every joint, force sensors in the fingers.',
    run: async (a, still) => { a.tab('robot'); a.robot({ motion: true }); await sleep(2200); if (still()) a.robot({ xray: true, labels: true }); },
  },
  {
    title: 'Every part is a real part', project: 'egg-picker',
    say: 'Exploded view: printed links, off-the-shelf servos, bearings, fasteners and cables, each with a part number, a vendor and an assembly step.',
    run: async (a) => { a.tab('robot'); a.robot({ motion: false, labels: false }); await a.explode(0.65); },
  },
  {
    title: 'Tested in physics simulation', project: 'egg-picker',
    say: 'MuJoCo runs the task: pick the egg with a force-limited grip and place it. Torque on every joint, live, plus a payload sweep, the workspace and a shake test.',
    run: async (a) => { a.explode(0); a.robot({ xray: false }); a.tab('sim'); await a.sim('pick_place'); },
  },
  {
    title: 'Change it in one sentence', project: 'egg-picker', chat: 'Swap to Dynamixel servos',
    say: '“Swap to Dynamixel servos.” New actuators touch everything, so Morph re-sizes the joints and regenerates the gripper, electronics, CAD, simulation, firmware, manufacturing, BOM and manual, in about half a minute.',
    after: async (a) => { a.tab('elec'); },
  },
  {
    title: 'From modules to a custom PCB', project: 'egg-picker', chat: 'Use a custom PCB',
    say: '“Use a custom PCB.” Only what depends on the electronics reruns; sizing, grasp and simulation are reused. The board is placed, routed and DRC-checked on screen, with Gerbers and JLCPCB files ready to order.',
    after: async (a) => { a.tab('elec'); a.elec('pcb'); },
  },
  {
    title: 'Same robot, another way to build it', project: 'egg-picker', params: { 'build.config': 'pilot' },
    say: 'Pilot build: CNC aluminium, sheet-metal brackets with flat patterns, nylon prints. Metal links are heavier, so Morph re-sizes the joints too. Per-part process, vendor, cost and lead time, with all three build configs compared.',
    after: async (a) => { a.tab('mfg'); },
  },
  {
    title: 'Darwin: who sells and who builds every part', project: 'egg-picker',
    say: 'Darwin searches the agentic web for an agent per purchased part and bulk material, plus PCB fab from our Gerbers, CNC from our DXFs and prints from our STLs. Starred agents are where Morph routes the work.',
    run: async (a) => { a.tab('bom'); a.darwin(); },
  },
  {
    title: 'Keewano: the build memory', project: 'egg-picker',
    say: (a) => {
      const cal = a.state.current?.gripper?.force?.hold_calibration;
      return cal
        ? `With event sending on, Keewano logs each revision, chat edit and bench measurement. Its build history for these pads (seeded for this demo, not measured) puts them at ×${fmt(cal.ratio, '', 2)} the simulated holding force over ${cal.n} builds, so with calibration on the grip target is raised before the firmware ships.`
        : 'With event sending on (Keys), Keewano logs each revision, chat edit and bench measurement. With calibration on, logged builds of the same pad and material adjust the simulated grip force before the firmware ships; here the target is the simulation value.';
    },
    run: async (a) => { a.tab('sim'); a.spotlight('.fw'); },
  },
  {
    title: 'The assembly manual writes itself', project: 'egg-picker',
    say: 'Step by step: which parts, which fasteners at what torque, which tools and how long. The printable manual is generated with every revision.',
    run: async (a) => { a.tab('asm'); a.asm(); },
  },
  {
    title: 'Import a real open-source robot', project: 'so101',
    say: 'Morph also imports existing robots. This is Hugging Face’s SO-101, loaded from its own URDF and meshes, then run through the same pipeline.',
    run: async (a) => { a.tab('robot'); a.robot({ motion: false, xray: false, labels: false }); a.home(); },
  },
  {
    title: 'Edit someone else’s robot', project: 'so101', chat: 'Swap to Dynamixel servos',
    say: '“Swap to Dynamixel servos.” The new servos go in, the electronics and firmware follow, and the printed holders that no longer fit are flagged in orange for redesign.',
    after: async (a) => { a.tab('robot'); },
  },
  {
    title: 'Add a wrist camera', project: 'so101', chat: 'Add a wrist camera',
    say: '“Add a wrist camera.” Camera mount, USB device, wiring, power budget and BOM all update together.',
    after: async (a) => { a.tab('robot'); },
  },
  {
    title: 'Time given back', project: 'egg-picker',
    say: (a) => {
      const p = a.state.current?.time_study?.project;
      const tail = p?.revisions?.length > 1 ? ` On this robot today: ${p.revisions.length} revisions, about ${fmt(p.before_min / 60, '', 0)} engineer-hours of work in ${fmt(p.after_min, '', 0)} minutes.` : '';
      return `By published numbers a robot like this takes about 241 engineer-hours to design. With Morph it takes about 15 minutes, review included, and every change after that takes minutes instead of days.${tail}`;
    },
    run: async (a) => { a.tab('robot'); a.robot({ motion: true }); await sleep(400); a.spotlight('#timeCard'); },
  },
];

export class Presenter {
  constructor(api) {
    this.a = api;
    this.i = -1;
    this.waiting = null;   // text while a step's pipeline run is in flight
    this.ran = new Set();  // chat / parameter steps already sent (going back never re-sends)
    this.el = document.createElement('div');
    this.el.id = 'present';
    this.el.className = 'glass';
    document.body.appendChild(this.el);
    this.onKey = this.onKey.bind(this);
  }
  get on() { return document.body.classList.contains('presenting'); }
  open() {
    document.body.classList.add('presenting');
    document.addEventListener('keydown', this.onKey, true);
    this.i = -1;
    this.render();
    if (this.narrow()) this.a.toast(`The window is too narrow for the right panel: zoom out once (${ZOOM_OUT})`);
  }
  narrow() { const el = document.querySelector('#inspector'); return !!el && getComputedStyle(el).display === 'none'; }
  close() {
    document.body.classList.remove('presenting');
    document.removeEventListener('keydown', this.onKey, true);
    this.gen = (this.gen || 0) + 1;  // a step still waiting on the pipeline stops waiting
    this.waiting = null;
  }
  toggle() { if (this.on) this.close(); else this.open(); }
  onKey(e) {
    if (e.target.closest?.('textarea, select, input:not([type=checkbox]):not([type=range]):not([type=radio])') || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === 'ArrowRight' || k === 'PageDown' || k === ' ' || k === 'Enter') { e.preventDefault(); e.stopImmediatePropagation(); this.next(); }
    else if (k === 'ArrowLeft' || k === 'PageUp') { e.preventDefault(); e.stopImmediatePropagation(); this.go(this.i - 1); }
    else if (k === 'Escape') { e.preventDefault(); this.close(); }
    else if (k === 'c' || k === 'C') this.el.classList.toggle('mini');
  }
  next() { if (!this.waiting && !this.resetting) this.go(this.i + 1); }
  async go(i) {
    if (this.waiting || i < -1 || i >= STEPS.length) return;
    this.i = i;
    if (i < 0) return this.render();
    const st = STEPS[i], a = this.a, gen = this.gen = (this.gen || 0) + 1;
    const still = () => this.gen === gen && this.i === i;
    this.render();
    try {
      if (st.project && a.state.project !== st.project) {
        this.wait('Opening ' + st.project + '…');
        await a.project(st.project);
        this.wait(null);
      }
      const fresh = (st.chat || st.params) && !this.ran.has(i);
      if (fresh) {
        this.ran.add(i);
        const before = a.state.latest, t0 = performance.now();
        this.wait('Regenerating…');
        if (st.chat) a.chat(st.chat); else a.params(st.params);
        // the run starts within seconds (Claude interprets the sentence first); a no-op answers at once
        const noop = () => (a.state.noopAt || 0) > t0;
        const failed = () => (a.state.errorAt || 0) > t0;
        const started = await a.until(() => a.state.busy || a.state.latest !== before || noop() || failed() || !still(), 25000);
        const ok = started && !noop() && !failed() && await a.until(
          () => (a.state.latest !== before && !a.state.busy) || failed() || !still(), 180000, (p) => still() && this.wait(p));
        if (!still()) return;
        this.wait(null);
        if (noop()) { a.toast('Already set from an earlier take: Esc, then Present → Reset demo for a clean run'); await st.after?.(a); }
        else if (failed()) a.toast('That change failed; see the chat. Press → to continue');
        else if (!ok) a.toast('The pipeline is taking longer than usual; press → when it finishes');
        else if (!a.tourOn()) await st.after?.(a);
      } else if (st.chat || st.params) {
        await st.after?.(a);
      }
      if (still()) await st.run?.(a, still);
    } catch (e) {
      console.warn('present step failed', e);
      this.wait(null);
    }
    if (still()) this.render();
  }
  wait(text) {
    if (text === this.waiting) return;
    const el = text && this.waiting ? this.el.querySelector('.pr-wait .t') : null;
    this.waiting = text;
    if (el) el.textContent = text; else this.render();  // update in place: no flicker, buttons stay put
  }
  render() {
    const i = this.i, n = STEPS.length, a = this.a;
    const dots = STEPS.map((_, k) => `<b class="${k < i ? 'done' : k === i ? 'cur' : ''}" data-k="${k}"></b>`).join('');
    if (i < 0) {
      const dirty = (a.state.projects || []).filter((p) => p.at_start === false).map((p) => p.name);
      const narrow = this.narrow();
      this.el.innerHTML = `<div class="pr-top"><span class="pr-n">${I('play', 'sm')}Present</span><span class="pr-t">The Morph demo, ${n} steps</span><span class="grow"></span>
        <button class="btn${dirty.length ? ' primary' : ''}" data-a="reset" title="Delete the chat edits from earlier takes and go back to each robot's original design">${I('replay', 'sm')}Reset demo</button>
        <button class="btn${dirty.length ? '' : ' primary'}" data-a="next">Start ${I('arrow', 'sm')}</button>
<button class="btn icon ghost" data-a="close" title="Exit (Esc)">${I('x', 'sm')}</button></div>
        ${narrow ? `<div class="pr-warn">${I('alert', 'sm')}<span>The window is too narrow to show the right panel (time card, grip force). Zoom out once (${ZOOM_OUT}), then open Present again.</span></div>` : ''}
        ${dirty.length ? `<div class="pr-warn">${I('alert', 'sm')}<span>${dirty.map(esc).join(' and ')} ${dirty.length === 1 ? 'has' : 'have'} edits from an earlier take. Click <b>Reset demo</b> first so every step changes something.</span></div>` : ''}
        <div class="pr-say">Press <kbd>→</kbd>, <kbd>Space</kbd> or a presentation clicker for each step; <kbd>←</kbd> goes back, <kbd>C</kbd> hides the caption, <kbd>Esc</kbd> exits. Steps that change the robot wait for the pipeline before you can move on.</div>
        <div class="pr-dots">${dots}</div>`;
    } else {
      const st = STEPS[i];
      const say = typeof st.say === 'function' ? st.say(a) : st.say;
      const nextT = STEPS[i + 1]?.title;
      this.el.innerHTML = `<div class="pr-top"><span class="pr-n">${i + 1}<small>/${n}</small></span><span class="pr-t">${esc(st.title)}</span><span class="grow"></span>
        ${this.waiting ? `<span class="pr-wait"><span class="spin"></span><span class="t">${esc(this.waiting)}</span></span>` : nextT ? `<span class="pr-next">next: ${esc(nextT)}</span>` : '<span class="pr-next">end</span>'}
        <button class="btn icon" data-a="prev" title="Back (←)" ${i <= 0 || this.waiting ? 'disabled' : ''}>${I('prev', 'sm')}</button>
        <button class="btn icon primary" data-a="next" title="Next (→ / Space)" ${this.waiting || i >= n - 1 ? 'disabled' : ''}>${I('next', 'sm')}</button>
        <button class="btn icon ghost" data-a="close" title="Exit (Esc)">${I('x', 'sm')}</button></div>
        <div class="pr-say">${esc(say)}</div><div class="pr-dots">${dots}</div>`;
    }
    const btn = (k) => this.el.querySelector(`[data-a="${k}"]`);
    if (btn('next')) btn('next').onclick = () => this.next();
    if (btn('prev')) btn('prev').onclick = () => this.go(this.i - 1);
    btn('close').onclick = () => this.close();
    if (btn('reset')) btn('reset').onclick = async () => {
      btn('reset').disabled = true;
      btn('reset').innerHTML = '<span class="spin"></span>Resetting (up to a minute)…';
      this.resetting = true;
      const ok = await a.reset().finally(() => { this.resetting = false; });
      this.ran.clear();
      a.toast(ok ? 'Demo reset: both robots are back at their full builds' : 'Still regenerating; reset when the pipeline is idle');
      this.render();
    };
    this.el.querySelectorAll('.pr-dots b').forEach((b) => (b.onclick = () => this.go(+b.dataset.k)));
  }
}

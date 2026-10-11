// Morph Studio v2: tab panels (Robot, Simulation, Electronics, Manufacturing, Assembly, BOM & Sourcing),
// the RobotViewer adapter (+ a small built-in fallback renderer) and shared UI helpers.

// ------------------------------------------------------------------ helpers
const ICONS = {
  cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  chip: '<rect x="4" y="6" width="16" height="12" rx="2"/><path d="M8 6V3M12 6V3M16 6V3M8 21v-3M12 21v-3M16 21v-3"/><circle cx="9" cy="12" r="1.5"/>',
  axis: '<path d="M12 3v11M12 14l-8 6M12 14l8 6"/><circle cx="12" cy="14" r="1.6"/>',
  camera: '<path d="M3 8h4l2-3h6l2 3h4v12H3z"/><circle cx="12" cy="13.5" r="3.6"/>',
  factory: '<path d="M2 20h20M4 20V10l5 3V10l5 3V6l6 4v10"/>',
  leaf: '<path d="M11 20A7 7 0 0 1 4 13c0-6 7-9 16-9 0 9-3 16-9 16z"/><path d="M4 20c4-4 7-7 10-9"/>',
  ruler: '<path d="M3 17L17 3l4 4L7 21z"/><path d="M7 13l2 2M10 10l2 2M13 7l2 2"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  play: '<path d="M7 4.5l12.5 7.5L7 19.5z"/>',
  pause: '<path d="M7 4.5h3.5v15H7zM13.5 4.5H17v15h-3.5z"/>',
  home: '<path d="M3 11l9-7.5 9 7.5"/><path d="M5 9.5V20h14V9.5"/>',
  layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  tag: '<path d="M3 12.5V3h9.5L21 11.5 11.5 21z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
  ghost: '<path d="M5 21V11a7 7 0 0 1 14 0v10l-2.4-1.6L14.3 21 12 19.4 9.7 21l-2.3-1.6z"/><path d="M9.5 11v.01M14.5 11v.01"/>',
  motion: '<path d="M4 18c3-8 7-12 16-12"/><path d="M16.5 3L20 6l-3.5 3"/><circle cx="4" cy="18" r="1.5"/>',
  prev: '<path d="M15 6l-6 6 6 6"/>', next: '<path d="M9 6l6 6-6 6"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M4 20h16"/>',
  zoomin: '<circle cx="11" cy="11" r="7"/><path d="M11 8v6M8 11h6M21 21l-5-5"/>',
  zoomout: '<circle cx="11" cy="11" r="7"/><path d="M8 11h6M21 21l-5-5"/>',
  fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  replay: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.5v5h5"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  box: '<path d="M12 2.5l8.5 4.8v9.4L12 21.5l-8.5-4.8V7.3z"/><path d="M12 21.5V12M20.5 7.3L12 12 3.5 7.3"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  gauge: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-5.5"/>',
  weight: '<path d="M6.5 8h11l2.5 12H4z"/><circle cx="12" cy="5" r="2"/>',
  wrench: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
  code: '<path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/>',
  circuit: '<circle cx="6" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 6h8M6 8v4a6 6 0 0 0 6 6h4"/>',
  harness: '<path d="M3 7h5c3 0 3 10 6 10h7M3 17h5c3 0 3-10 6-10h7"/>',
  book: '<path d="M4 4.5h6a3 3 0 0 1 3 3V21a2 2 0 0 0-2-2H4zM20 4.5h-6a3 3 0 0 0-3 3V21a2 2 0 0 1 2-2h7z"/>',
  file: '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>',
  spark: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>',
  power: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  link: '<path d="M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-1 1"/><path d="M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l1-1"/>',
  cart: '<path d="M3 4h2l2.4 11.2a2 2 0 0 0 2 1.6h7.7a2 2 0 0 0 2-1.5L21 8H6"/><circle cx="10" cy="20.5" r="1"/><circle cx="18" cy="20.5" r="1"/>',
  hammer: '<path d="M14 6l4 4M3 21l9-9M12.5 5.5l3-3 6 6-3 3z"/>',
};
export function I(name, size = '') {
  return `<svg class="i ${size}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export function h(tag, cls, html) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (html != null) el.innerHTML = html;
  return el;
}
export function fmt(v, unit = '', digits) {
  if (v == null || v === '') return '–';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v !== 'number') return esc(v);
  const a = Math.abs(v);
  const d = digits ?? (a >= 1000 ? 0 : a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 2);
  const s = (+v.toFixed(d)).toLocaleString('en-US', { maximumFractionDigits: d });
  return unit ? `${s}${unit === '%' || unit === '×' ? '' : ' '}${unit}` : s;
}
export function kmoney(v) {
  if (v == null || isNaN(v)) return '–';
  return Math.abs(v) >= 1000 ? `$${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : money(v, false);
}
export function money(v, cents) {
  if (v == null || isNaN(v)) return '–';
  const big = Math.abs(v) >= 1000;
  return (v < 0 ? '−$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: cents === false || (big && !cents) ? 0 : 2, maximumFractionDigits: cents === false || (big && !cents) ? 0 : 2 });
}
const NAMES = { hobby_pwm: 'Hobby PWM', smart_bus: 'Smart bus', dynamixel: 'Dynamixel', modules: 'Off-the-shelf modules', custom_pcb: 'Custom PCB',
  prototype: 'Prototype', pilot: 'Pilot', production: 'Production', fsr402: 'FSR 402', none: 'none', esp32_devkit: 'ESP32 DevKit', esp32_s3_devkit: 'ESP32-S3 DevKit',
  arduino_nano: 'Arduino Nano', rp2040_pico: 'RP2040 Pico', auto: 'auto', contour: 'contour', v: 'V-groove', flat: 'flat', fdm_print: 'FDM print', sla_print: 'SLA print',
  mjf_print: 'MJF print', cnc_mill: 'CNC mill', cnc_turn: 'CNC turn', laser_cut: 'Laser cut', sheet_metal: 'Sheet metal', injection_mold: 'Injection mold',
  pcb_assembly: 'PCB assembly', cable_assembly: 'Cable assembly', purchased: 'Purchased', ttl_half_duplex: 'TTL half-duplex', pwm: 'PWM',
  wrist_camera: 'wrist camera', estop: 'e-stop', imu: 'IMU' };
export function pretty(v, raw) {
  if (v === '' || v == null) return raw ? '—' : 'none';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'number') return String(+v.toFixed(3));
  if (NAMES[v]) return NAMES[v];
  if (String(v).includes(',')) return String(v).split(',').map((x) => NAMES[x] || x.replace(/_/g, ' ')).join(' + ');
  return raw ? String(v) : String(v).replace(/_/g, ' ');
}
const PROC_COL = { fdm_print: '#7091ff', sla_print: '#a78bfa', mjf_print: '#9d7cf5', cnc_mill: '#2dd4d4', cnc_turn: '#22b8c8', laser_cut: '#f5b942',
  sheet_metal: '#ffb86b', injection_mold: '#ff6b9d', pcb_assembly: '#3dd68c', cable_assembly: '#ff9152', purchased: '#838c9d' };
const CAT_COL = { actuator: '#7091ff', electronics: '#2dd4d4', pcb: '#3dd68c', cable: '#f5b942', connector: '#ff9152', fastener: '#838c9d',
  mechanical: '#a78bfa', fabricated: '#ff6b9d' };
const SERIES = ['#7091ff', '#ff9152', '#3dd68c', '#f5b942', '#a78bfa', '#2dd4d4', '#ff6b9d'];
const PHASE_COL = { approach: '#7091ff', grasp: '#ff9152', lift: '#3dd68c', move: '#2dd4d4', place: '#a78bfa', release: '#f5b942', retreat: '#838c9d' };
const MOD_NAMES = { esp32_devkit: 'ESP32-DevKitC-32E', esp32_s3_devkit: 'ESP32-S3-DevKitC-1', arduino_nano: 'Arduino Nano', rp2040_pico: 'Raspberry Pi Pico',
  pca9685_board: 'PCA9685 16-ch PWM driver', bus_servo_adapter: 'Waveshare Bus Servo Adapter', u2d2: 'ROBOTIS U2D2 + power hub', buck_5v: '5 V 3 A buck (logic)',
  buck_servo: '20 A buck (servo rail)', psu_12v: '12 V 10 A supply', psu_6v: '6 V 10 A supply', fsr402: 'FSR 402 force sensor', wrist_camera: 'USB wrist camera',
  imu: 'BNO085 IMU', estop: 'E-stop switch', morph_controller_pcba: 'Morph controller PCBA', sts3215: 'Feetech STS3215', xl430_w250: 'Dynamixel XL430',
  xm430_w350: 'Dynamixel XM430', mg996r: 'MG996R', ds3218: 'DS3218', sg90: 'SG90' };
const jname = (j) => String(j).replace(/^j\d_/, '').replace(/_/g, ' ');
export const emptyHTML = (icon, title, text) => `<div class="empty fade-in"><div><div class="ill">${I(icon, 'lg')}</div><h4>${esc(title)}</h4><p>${esc(text)}</p></div></div>`;
function nodata(root, on, html) {
  let ov = root.querySelector(':scope > .ndov');
  if (!ov) { ov = h('div', 'ndov'); root.appendChild(ov); }
  ov.classList.toggle('on', !!on);
  if (on) ov.innerHTML = html;
  root.classList.toggle('nodata', !!on);
}

// ------------------------------------------------------------------ grip force window (inspector)
export function forceWindowSVG(f) {
  const [lo, hi] = f.window_n || [0, 0];
  const stall = f.servo_stall_force_n, lim = f.object_limit_n || hi, tgt = f.target_force_n, hard = f.limit_n;
  const vals = [lo, hi, stall, lim, tgt, hard].filter((v) => v > 0);
  const vmin = Math.max(Math.min(...vals) / 2.2, 0.1), vmax = Math.max(...vals) * 1.25;
  const W = 272, x0 = 4, x1 = W - 4;
  const X = (v) => x0 + (x1 - x0) * (Math.log10(Math.max(v, vmin)) - Math.log10(vmin)) / (Math.log10(vmax) - Math.log10(vmin));
  const mk = (v, col, label, y, _a, dash = '') => {
    if (!v) return '';
    const x = X(v), half = label.length * 2.7;
    const [lx, anchor] = x + half > W - 2 ? [W - 2, 'end'] : x - half < 2 ? [2, 'start'] : [x, 'middle'];
    return `<line x1="${x}" x2="${x}" y1="13" y2="31" stroke="${col}" stroke-width="1.6" ${dash ? 'stroke-dasharray="2 2"' : ''}/><text x="${lx}" y="${y}" fill="${col}" text-anchor="${anchor}">${label}</text>`;
  };
  return `<svg viewBox="0 0 ${W} 58" font-size="9.5" font-family="Inter, system-ui, sans-serif" fill="#838c9d">
    <rect x="${x0}" y="17" width="${x1 - x0}" height="10" rx="3" fill="#20252f"/>
    <rect x="${x0}" y="17" width="${X(lo) - x0}" height="10" rx="3" fill="#ff6b6b" opacity=".55"/>
    <rect x="${X(lo)}" y="17" width="${X(lim) - X(lo)}" height="10" fill="#3dd68c" opacity=".7"/>
    <rect x="${X(lim)}" y="17" width="${x1 - X(lim)}" height="10" rx="3" fill="#ff6b6b" opacity=".75"/>
    ${mk(tgt, '#ffffff', `target ${fmt(tgt)} N`, 10)}
    ${hard ? mk(hard, '#f5b942', `FW limit ${fmt(hard)} N`, 42, 'middle', 1) : ''}
    ${mk(stall, '#ff9152', `servo ${fmt(stall, '', 0)} N`, 10)}
    <text x="${x0}" y="42">slips &lt; ${fmt(lo)} N</text>
    <text x="${X(lim)}" y="54" fill="#ff8f8f" text-anchor="middle">cracks ${fmt(lim, '', 0)} N</text>
  </svg>`;
}

// ------------------------------------------------------------------ RobotViewer adapter
// ./viewer.js (RobotViewer) owns the 3D scene; this adapter keeps the Studio's view state (x-ray, labels,
// explode, ghost, step, trajectory) and probes method names so small API drift never breaks the page.
// If the module is missing a small built-in renderer keeps the 3D views alive; if WebGL is missing a
// placeholder explains it.
export class ViewerAdapter {
  constructor(host, ctx) {
    this.host = host;
    this.ctx = ctx;
    this.v = null;
    this.kind = 'none';
    this.st = { xray: false, labels: false, explode: 0, ghost: false };
    this.mode = { step: false, traj: false, motion: false, hl: false, ws: false };
    this.loading = h('div', 'vload hide', '<span class="spin"></span><span>Loading robot…</span>');
    host.appendChild(this.loading);
    this.ready = this._init();
  }
  async _init() {
    let RV = null;
    try {
      const mod = await import('./viewer.js');
      RV = mod.RobotViewer || mod.default;
    } catch (e) {
      console.info('[morph] ./viewer.js not available; using the built-in preview renderer.', e?.message || '');
    }
    const makers = [RV && (() => new RV(this.host, { hud: true })), () => new FallbackViewer(this.host)];
    for (const make of makers) {
      if (!make) continue;
      try { this.v = make(); this.kind = this.v instanceof FallbackViewer ? 'fallback' : 'viewer'; break; } catch (e) { console.warn('[morph] viewer init failed', e); }
    }
    if (this.v?.ready) { try { await this.v.ready; } catch (e) { console.warn('[morph] viewer failed to start', e); this.v = null; } }
    if (!this.v) this.placeholder();
  }
  placeholder() {
    this.host.appendChild(h('div', 'vph', `<div class="card"><div style="margin:0 auto;width:46px;height:46px;border-radius:12px;display:grid;place-items:center;background:var(--bg3);color:var(--accent2)">${I('box', 'lg')}</div><h4>3D view unavailable</h4><p>WebGL couldn't start in this browser. Every other view still updates live.</p></div>`));
  }
  fn(names) {
    if (!this.v) return null;
    for (const n of [].concat(names)) if (typeof this.v[n] === 'function') return this.v[n].bind(this.v);
    return null;
  }
  has(names) { return !!this.fn(names); }
  call(names, ...args) {
    const f = this.fn(names);
    if (!f) return undefined;
    try {
      const r = f(...args);
      if (r && typeof r.catch === 'function') r.catch((e) => console.warn('[viewer]', [].concat(names)[0], e?.message || e));
      return r === undefined ? true : r;
    } catch (e) { console.warn('[viewer]', [].concat(names)[0], e); return undefined; }
  }
  attach(slot) {
    if (this.host.parentElement !== slot) slot.appendChild(this.host);
    requestAnimationFrame(() => this.call(['resize']));
  }
  async load(robot, prevRobot) {
    await this.ready;
    this.robot = robot;
    this.prevRobot = prevRobot || null;
    if (!this.v || !robot) return;
    this.loading.classList.remove('hide');
    try { await this.call(['load', 'loadRobot'], this.ctx.resolve(robot.json)); } catch (e) { console.warn('[viewer] load', e); }
    this.loading.classList.add('hide');
    if (this.st.xray) this.xray(true);
    if (this.st.labels) this.labels(true);
    if (this.st.explode) this.explode(this.st.explode);
    this.ghost(this.st.ghost && !!this.prevRobot);
    this.onLoad?.();
  }
  async xray(on) { this.st.xray = !!on; this.onState?.(); await this.ready; this.call(['setXray', 'xray'], !!on); }
  async labels(on) { this.st.labels = !!on; this.onState?.(); await this.ready; this.call(['setLabels', 'showLabels'], !!on); }
  async explode(t) { this.st.explode = t; await this.ready; this.call(['setExplode', 'explode'], t); }
  async ghost(on) {
    this.st.ghost = !!on && !!this.prevRobot;
    this.onState?.();
    await this.ready;
    this.call(['setGhost', 'showGhost'], this.st.ghost ? this.ctx.resolve(this.prevRobot.json) : null);
  }
  async home() {
    await this.ready;
    if (!this.call(['home', 'resetView', 'resetCamera'])) this.call(['frame'], null);
    this.call(['setAutoOrbit'], true);
  }
  async motion(on) {
    await this.ready;
    this.mode.motion = !!on;
    return on ? this.call(['playMotionDemo', 'playMotion']) : this.call(['stopMotion']);
  }
  async highlight(list) {
    await this.ready;
    this.mode.hl = !!(list && list.length);
    this.call(['highlight'], list && list.length ? list : null);
  }
  async step(i) {
    await this.ready;
    if (i == null) {
      if (!this.mode.step) return;
      this.mode.step = false;
      this.call(['showAllSteps', 'clearStep']);
    } else { this.mode.step = true; this.call(['showStep'], i); }
  }
  async workspace(points) {
    await this.ready;
    this.mode.ws = !!points;
    this.call(['showWorkspace'], points || null);
  }
  // trajectories: the viewer runs the clock and reports frames through onFrame(info)
  canDrive() { return this.has(['playTrajectory']) && this.has(['setTime']); }
  async play(sim, opts) { await this.ready; this.mode.traj = true; this.call(['playTrajectory'], sim, opts); }
  seek(t) { this.call(['setTime', 'seek'], t); }
  pauseTraj() { this.call(['pause']); }
  resumeTraj() { this.call(['resume']); }
  async stopTraj() {
    await this.ready;
    if (!this.mode.traj && !this.mode.ws) return;
    this.mode.traj = this.mode.ws = false;
    this.call(['clearOverlays', 'stopTrajectory']);
    this.resetPose();
  }
  resetPose() {
    const homeQ = this.v && typeof this.v._homeQ === 'function' ? this.v._homeQ() : null;
    if (homeQ && this.has('animateTo')) this.call(['animateTo'], homeQ, 700);
    else this.call(['resetPose']);
  }
  setPose(q) { this.call(['setJoints', 'setPose'], q); }
  async reset() {
    await this.ready;
    if (this.mode.motion) await this.motion(false);
    await this.stopTraj();
    await this.step(null);
    if (this.mode.hl) await this.highlight(null);
  }
}

// Minimal built-in renderer, used only when ./viewer.js is missing. Mirrors the RobotViewer API subset
// the Studio calls: load(jsonUrl), highlight, setXray, setExplode, showStep/showAllSteps, playTrajectory
// (+ pause/resume/setTime, onFrame(info)), showWorkspace, clearOverlays, playMotionDemo/stopMotion, frame.
class FallbackViewer {
  constructor(host) {
    this.host = host;
    this.ready = this._init();
  }
  async _init() {
    const [THREE, { GLTFLoader }, { OrbitControls }, { RoomEnvironment }] = await Promise.all([
      import('./vendor/three.module.min.js'), import('./vendor/GLTFLoader.js'), import('./vendor/OrbitControls.js'), import('./vendor/RoomEnvironment.js')]);
    this.T = THREE;
    this.loader = new GLTFLoader();
    const r = (this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }));
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.toneMapping = THREE.ACESFilmicToneMapping;
    r.outputColorSpace = THREE.SRGBColorSpace;
    Object.assign(r.domElement.style, { position: 'absolute', inset: '0', width: '100%', height: '100%' });
    this.host.prepend(r.domElement);
    this.scene = new THREE.Scene();
    const pm = new THREE.PMREMGenerator(r);
    this.scene.environment = pm.fromScene(new RoomEnvironment(r), 0.04).texture;
    this.cam = new THREE.PerspectiveCamera(32, 1, 5, 20000);
    this.cam.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.cam, r.domElement);
    this.controls.enableDamping = true;
    this.controls.autoRotate = true;
    this.controls.autoRotateSpeed = 0.55;
    this.controls.addEventListener('start', () => (this.controls.autoRotate = false));
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(400, -300, 900);
    this.scene.add(key, new THREE.HemisphereLight(0xcfd8ff, 0x1a1e27, 0.5));
    const grid = new THREE.GridHelper(1200, 48, 0x2a3140, 0x1a1f29);
    grid.rotation.x = Math.PI / 2;
    this.scene.add(grid);
    this.root = new THREE.Group();
    this.extra = new THREE.Group();
    this.scene.add(this.root, this.extra);
    this.clock = new THREE.Clock();
    const tick = () => {
      requestAnimationFrame(tick);
      const dt = Math.min(this.clock.getDelta(), 0.1);
      if (this.traj?.playing) this._advance(dt);
      else if (this.demo) this._demo(dt);
      this.controls.update();
      r.render(this.scene, this.cam);
    };
    tick();
    new ResizeObserver(() => this.resize()).observe(this.host);
  }
  resize() {
    if (!this.renderer) return;
    const w = this.host.clientWidth, hh = this.host.clientHeight;
    if (!w || !hh) return;
    this.renderer.setSize(w, hh, false);
    this.cam.aspect = w / hh;
    this.cam.updateProjectionMatrix();
  }
  async load(jsonUrl) {
    await this.ready;
    const T = this.T;
    const meta = await (await fetch(jsonUrl)).json();
    const gltf = await this.loader.loadAsync(new URL(meta.glb || 'robot.glb', new URL(jsonUrl, location.href)).href);
    this.root.clear();
    this.root.add(gltf.scene);
    this.meta = meta;
    this.links = {};
    this.parts = {};
    gltf.scene.traverse((o) => {
      if (o.name.startsWith('link__')) this.links[o.name.slice(6)] = { node: o, p0: o.position.clone(), q0: o.quaternion.clone() };
      if (o.name.startsWith('part__')) {
        const info = meta.parts.find((p) => p.name === o.name.slice(6)) || {};
        o.traverse((m) => {
          if (!m.isMesh) return;
          m.material = m.material.clone();
          m.material.side = T.DoubleSide;
          m.userData.base = m.material.color.clone();
        });
        this.parts[o.name.slice(6)] = { node: o, info, p0: o.position.clone() };
      }
    });
    this.joints = Object.fromEntries(meta.joints.map((j) => [j.name, j]));
    this.homeQ = { ...(meta.meta?.home || {}) };
    this.setJoints(this.homeQ);
    this.frame();
  }
  frame() {
    const T = this.T;
    const box = new T.Box3().setFromObject(this.root);
    if (box.isEmpty()) return;
    const c = box.getCenter(new T.Vector3()), s = box.getSize(new T.Vector3()).length();
    this.controls.target.copy(c);
    this.cam.position.set(c.x + s * 1.05, c.y - s * 1.25, c.z + s * 0.55);
  }
  setAutoOrbit(on) { this.controls.autoRotate = !!on; }
  _homeQ() { return { ...this.homeQ }; }
  animateTo(q) { this.setJoints(q); }
  setJoints(q) {
    if (!this.links) return;
    const T = this.T, vals = { ...this.homeQ, ...q };
    for (const j of Object.values(this.joints)) if (j.mimic) vals[j.name] = (vals[j.mimic.joint] ?? 0) * (j.mimic.multiplier ?? 1) + (j.mimic.offset ?? 0);
    for (const j of Object.values(this.joints)) {
      const L = this.links[j.child];
      if (!L || j.type === 'fixed') continue;
      const v = vals[j.name] ?? 0;
      const ax = new T.Vector3(...j.axis).normalize();
      if (j.type === 'revolute') { L.node.quaternion.copy(L.q0).multiply(new T.Quaternion().setFromAxisAngle(ax, v)); L.node.position.copy(L.p0); }
      else { L.node.quaternion.copy(L.q0); L.node.position.copy(L.p0).add(ax.applyQuaternion(L.q0).multiplyScalar(v)); }
    }
  }
  _paint() {
    const T = this.T, hl = this.hl, step = this.stepIdx;
    for (const p of Object.values(this.parts || {})) p.node.traverse((m) => {
      if (!m.isMesh) return;
      const i = p.info, lit = hl && (hl.has(i.kind) || hl.has(i.name) || hl.has(i.group));
      const cur = step != null && i.step === step;
      m.visible = step == null || (i.step ?? 0) <= step;
      m.material.color.copy(m.userData.base);
      m.material.emissive = new T.Color(lit || cur ? 0xff7a2a : 0x000000);
      m.material.emissiveIntensity = lit || cur ? 0.6 : 0;
      const fade = this.xr && !lit;
      m.material.transparent = fade;
      m.material.opacity = fade ? 0.2 : 1;
      m.material.depthWrite = !fade;
    });
  }
  highlight(list) { this.hl = list && list.length ? new Set(list) : null; this._paint(); }
  setXray(on) { this.xr = !!on; this._paint(); }
  showStep(i) { this.stepIdx = i; this._paint(); }
  showAllSteps() { this.stepIdx = null; this._paint(); }
  setExplode(t) {
    for (const p of Object.values(this.parts || {})) {
      const b = p.info.bbox;
      let v = new this.T.Vector3(...(p.info.explode || [0, 0, 0]));
      if (!v.lengthSq() && b) v = new this.T.Vector3((b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2, 0).multiplyScalar(0.9);
      p.node.position.copy(p.p0).addScaledVector(v, t);
    }
  }
  setLabels() {}
  setGhost() {}
  playMotionDemo() { this.demo = { t: 0 }; }
  stopMotion() { this.demo = null; this.setJoints(this.homeQ); }
  _demo(dt) {
    const t = (this.demo.t += dt), H = this.homeQ;
    this.setJoints({ j1_base_yaw: 0.7 * Math.sin(t * 0.7), j2_shoulder: (H.j2_shoulder || 0) + 0.35 * Math.sin(t * 0.9),
      j3_elbow: (H.j3_elbow || 0) + 0.3 * Math.sin(t * 1.1 + 1), j4_wrist_pitch: (H.j4_wrist_pitch || 0) + 0.25 * Math.sin(t * 1.3), gripper: 7 + 7 * Math.sin(t * 1.6) });
  }
  playTrajectory(sim, { loop = false, speed = 1, onFrame = null } = {}) {
    this.demo = null;
    if (!this.traj || this.traj.sim !== sim) {
      this.traj = { sim, t: 0, duration: Math.max(1e-3, (sim.frames.at(-1)?.t || 0) - (sim.frames[0]?.t || 0)) };
      this._object(sim);
    }
    Object.assign(this.traj, { loop, speed, onFrame, playing: true });
    this._apply();
  }
  _object(sim) {
    const T = this.T, o = sim.object;
    this.extra.clear();
    this.obj = null;
    if (o) {
      const d = o.diameter_mm || 40, l = o.length_mm || d;
      this.obj = new T.Mesh(new T.SphereGeometry(d / 2, 40, 28), new T.MeshStandardMaterial({ color: o.color || '#f3e3c8', roughness: 0.55 }));
      this.obj.scale.set(1, 1, l / d);
      this.extra.add(this.obj);
    }
    for (const [k, p] of Object.entries(sim.targets || {})) {
      const ring = new T.Mesh(new T.RingGeometry(16, 19, 48), new T.MeshBasicMaterial({ color: k === 'pick' ? 0x7091ff : 0x3dd68c, side: T.DoubleSide }));
      ring.position.set(p[0], p[1], 0.6);
      this.extra.add(ring);
    }
  }
  _advance(dt) {
    const tr = this.traj;
    tr.t += dt * (tr.speed || 1);
    if (tr.t > tr.duration) tr.t = tr.loop ? tr.t % tr.duration : tr.duration;
    this._apply();
  }
  _apply() {
    const tr = this.traj, s = tr.sim, F = s.frames, t0 = F[0].t || 0;
    let i = 0;
    while (i < F.length - 1 && F[i + 1].t - t0 <= tr.t) i++;
    const f = F[i], q = {};
    s.joints.forEach((j, k) => (q[j] = f.q[k]));
    this.setJoints(q);
    if (this.obj && f.obj) { this.obj.position.set(f.obj[0], f.obj[1], f.obj[2]); this.obj.quaternion.set(f.obj[4], f.obj[5], f.obj[6], f.obj[3]); }
    tr.onFrame?.({ t: tr.t, duration: tr.duration, index: i, phase: f.phase, grip_n: f.grip_n, tau: f.tau, q, frame: f });
  }
  setTime(t) { if (this.traj) { this.traj.t = Math.max(0, Math.min(t, this.traj.duration)); this._apply(); } }
  pause() { if (this.traj) this.traj.playing = false; }
  resume() { if (this.traj) this.traj.playing = true; }
  clearOverlays() { this.traj = null; this.extra.clear(); this.obj = null; this.showWorkspace(null); }
  showWorkspace(points) {
    if (this.ws) { this.scene.remove(this.ws); this.ws = null; }
    if (!points || !points.length) return;
    const T = this.T, g = new T.BufferGeometry();
    g.setAttribute('position', new T.Float32BufferAttribute(points.flat(), 3));
    this.ws = new T.Points(g, new T.PointsMaterial({ color: 0x7091ff, size: 4, transparent: true, opacity: 0.45, depthWrite: false }));
    this.scene.add(this.ws);
  }
}

// ------------------------------------------------------------------ shared: robot.json (parts/groups/home pose)
const robotCache = new Map();
export function clearCaches() { robotCache.clear(); }
function robotJSON(ctx) {
  const u = ctx.resolve(ctx.current?.robot?.json);
  if (!u) return Promise.resolve(null);
  if (!robotCache.has(u)) robotCache.set(u, ctx.getJSON(u).catch(() => null));
  return robotCache.get(u);
}
function changedNode(cur, ...nodes) { return nodes.some((n) => cur?.provenance?.[n] === 'computed'); }

// ------------------------------------------------------------------ 1. Robot
export class RobotPanel {
  constructor(root, ctx) {
    this.ctx = ctx;
    root.innerHTML = `<div class="robot-v vp"></div>
      <div class="ov tl tlstack"><div class="glass toolbar">
        <button class="btn" data-a="motion">${I('motion')}Motion demo</button><span class="sep"></span>
        <label class="slider">${I('layers', 'sm')}Explode<input type="range" min="0" max="100" value="0" data-a="explode"><span class="v">0%</span></label><span class="sep"></span>
        <button class="btn" data-a="xray">${I('eye')}X-ray</button>
        <button class="btn" data-a="labels">${I('tag')}Labels</button>
        <button class="btn" data-a="ghost" title="Overlay the previous revision">${I('ghost')}Ghost<span class="mono dim" data-prev></span></button><span class="sep"></span>
        <button class="btn icon" data-a="home" title="Home view">${I('home')}</button>
      </div><div class="vstat"></div></div>
      <div class="ov br glass kchain"></div>`;
    this.root = root;
    this.slot = root.querySelector('.vp');
    this.tb = root.querySelector('.toolbar');
    const btn = (a) => this.tb.querySelector(`[data-a="${a}"]`);
    btn('motion').onclick = () => { this.motionOn = !this.motionOn; btn('motion').classList.toggle('on', this.motionOn); ctx.viewer.motion(this.motionOn); };
    btn('xray').onclick = () => { const on = !ctx.viewer.st.xray; ctx.viewer.xray(on); this.sync(); };
    btn('labels').onclick = () => { const on = !ctx.viewer.st.labels; ctx.viewer.labels(on); this.sync(); };
    btn('ghost').onclick = () => { if (!ctx.viewer.prevRobot) return ctx.toast('No previous revision geometry to compare against'); ctx.viewer.ghost(!ctx.viewer.st.ghost); this.sync(); };
    btn('home').onclick = () => ctx.viewer.home();
    const ex = btn('explode');
    ex.oninput = () => { ex.nextElementSibling.textContent = `${ex.value}%`; ex.style.setProperty('--p', `${ex.value}%`); ctx.viewer.explode(ex.value / 100); };
    ex.style.setProperty('--p', '0%');
  }
  sync() {
    const st = this.ctx.viewer.st, b = (a) => this.tb.querySelector(`[data-a="${a}"]`);
    b('xray').classList.toggle('on', st.xray);
    b('labels').classList.toggle('on', st.labels);
    b('ghost').classList.toggle('on', st.ghost);
    b('ghost').disabled = !this.ctx.viewer.prevRobot && !this.ctx.previous;
  }
  update(cur, prev) {
    nodata(this.root, !cur.robot, emptyHTML('box', 'No CAD in this revision yet', 'The 3D robot appears here as soon as the Robot CAD stage produces robot.glb.'));
    const sz = cur.sizing || {}, s = cur.summary || {}, d = cur.design || {};
    const joints = sz.joints || [];
    const L = sz.links_mm || {};
    this.root.querySelector('[data-prev]').textContent = prev ? ` ${prev.rev}` : '';
    this.root.querySelector('.kchain').innerHTML = `<div class="sec-t">Kinematic chain<span class="r">${joints.length} DOF · torque used</span></div>` +
      (joints.length ? joints.map((j) => {
        const pct = Math.min(100, (j.torque_with_margin_nm / (j.torque_avail_nm || 1)) * 100);
        const cls = pct > 100 * 0.95 ? 'bad' : pct > 70 ? 'warn' : '';
        return `<div class="row" title="${esc(j.name)}: needs ${fmt(j.torque_with_margin_nm)} N·m with margin, ${esc(j.actuator)} gives ${fmt(j.torque_avail_nm)} N·m"><span class="n">${esc(j.name)}</span><span class="meter"><b class="${cls}" style="width:${pct.toFixed(1)}%"></b></span><span class="p">${pct.toFixed(0)}%</span></div>`;
      }).join('') : '<div class="skel" style="height:80px"></div>') +
      `<div class="foot"><span>${L.L1 ? `L1 ${fmt(L.L1, '', 0)} · L2 ${fmt(L.L2, '', 0)} · L3 ${fmt(L.L3, '', 0)} mm` : ''}</span><span>${esc((joints[0]?.actuator || '').toUpperCase())}</span></div>`;
    const fam = { hobby_pwm: 'Hobby PWM', smart_bus: 'Smart bus', dynamixel: 'Dynamixel' }[d['arm.actuator_family']] || '';
    this.root.querySelector('.vstat').innerHTML = [
      fam && `<span class="chip">${I('cpu', 'sm')}${esc(fam)} · ${esc((s.actuators || '').toUpperCase())}</span>`,
      s.parts != null && `<span class="chip">${I('box', 'sm')}${s.parts} parts</span>`,
      s.robot_mass_kg != null && `<span class="chip">${I('weight', 'sm')}${fmt(s.robot_mass_kg, 'kg')}</span>`,
      s.reach_mm != null && `<span class="chip">${I('ruler', 'sm')}${fmt(s.reach_mm, 'mm', 0)} reach</span>`,
    ].filter(Boolean).join('');
    this.sync();
  }
  onShow() {
    const v = this.ctx.viewer;
    v.reset().then(() => { if (this.motionOn) v.motion(true); });
    this.sync();
  }
  onHide() { if (this.motionOn) { this.ctx.viewer.motion(false); this.motionOn = false; this.tb.querySelector('[data-a="motion"]').classList.remove('on'); } }
  tour(cur) {
    if (this.ctx.previous && changedNode(cur, 'cad', 'sizing')) { this.ctx.viewer.ghost(true); this.sync(); }
    return 4200;
  }
}

// ------------------------------------------------------------------ 2. Simulation
const SCEN_ORDER = ['pick_place', 'payload_sweep', 'workspace', 'shake'];
const SCEN_LABEL = { pick_place: 'Pick & place', payload_sweep: 'Payload sweep', workspace: 'Workspace', shake: 'Shake test' };
const SCEN_CARDS = {  // [candidate metric keys, label, unit]; '@grip' / '@tau1' are live values at the playhead
  pick_place: [[['cycle_time_s'], 'Cycle time', 's'], [['payload_g', 'place_error_mm'], null, null], [['reach_mm', 'grip_target_n', 'target_grip_n'], null, null], [['@grip'], 'Grip force', 'N']],
  payload_sweep: [[['max_payload_g'], 'Max payload', 'g'], [['rated_payload_g'], 'Task payload', 'g'], [['test_reach_mm', 'far_radius_mm', 'payload_margin'], null, null], [['@tau1'], 'Shoulder torque', 'N·m']],
  workspace: [[['reach_mm'], 'Reach', 'mm'], [['min_reach_mm', 'max_height_mm'], null, null], [['volume_l'], 'Volume', 'L'], [['yaw_deg', 'valid_fraction', 'samples'], null, null]],
  shake: [[['max_accel_ms2', 'peak_accel_ms2'], 'Peak accel', 'm/s²'], [['max_slip_mm', 'slip_mm'], 'Slip', 'mm'], [['min_grip_n', 'grip_n_min'], 'Min grip', 'N'], [['@grip'], 'Grip force', 'N']],
};
const CARD_LABEL = { place_error_mm: 'Place error', grip_target_n: 'Grip target', target_grip_n: 'Grip target', far_radius_mm: 'At radius', payload_margin: 'Payload margin',
  max_height_mm: 'Max height', valid_fraction: 'Valid poses', samples: 'Samples', yaw_deg: 'Base sweep', test_reach_mm: 'At reach', min_reach_mm: 'Min reach',
  payload_g: 'Payload', reach_mm: 'Reach' };
function simCards(scen, m) {
  const out = [], used = new Set();
  for (const [keys, label, unit] of SCEN_CARDS[scen] || []) {
    const k = keys.find((x) => x.startsWith('@') || (typeof m[x] === 'number'));
    if (!k) continue;
    used.add(k);
    const [lab, u] = metricLabel(k.replace(/^@/, ''));
    out.push([k, label || CARD_LABEL[k] || lab, unit || (k === 'payload_margin' ? '×' : k === 'valid_fraction' ? '' : u)]);
  }
  for (const [k, v] of Object.entries(m)) {
    if (out.length >= 4) break;
    if (typeof v !== 'number' || used.has(k)) continue;
    const [lab, u] = metricLabel(k);
    out.push([k, CARD_LABEL[k] || lab, u]);
  }
  return out;
}
export class SimPanel {
  constructor(root, ctx) {
    this.ctx = ctx;
    root.innerHTML = `<div class="sim">
      <div class="bar"><div class="pills" data-scen></div><span class="grow"></span>
        <span class="phase"><i></i><span>–</span></span>
        <span class="tread num">0.0 <span class="dim">/ 0.0 s</span></span>
        <div class="pills" data-speed><button data-s="0.5">0.5×</button><button data-s="1" class="on">1×</button><button data-s="2">2×</button></div>
        <button class="playbtn" title="Play / pause (space)">${I('play')}</button></div>
      <div class="vp"></div>
      <div class="tl"><div class="tlh"><span class="sec-t" style="margin:0;white-space:nowrap">Torque · N·m</span><div class="legend"></div></div><canvas></canvas></div>
      <div class="side"></div></div>`;
    this.root = root;
    this.slot = root.querySelector('.vp');
    this.cv = root.querySelector('canvas');
    this.t = 0; this.playing = true; this.speed = 1;
    root.querySelector('.playbtn').onclick = () => this.toggle();
    root.querySelectorAll('[data-speed] button').forEach((b) => (b.onclick = () => {
      this.speed = +b.dataset.s;
      root.querySelectorAll('[data-speed] button').forEach((x) => x.classList.toggle('on', x === b));
      if (this.driven && this.sim) ctx.viewer.play(this.sim, { loop: true, speed: this.speed, onFrame: (info) => this.onFrame(info) });
    }));
    const scrub = (e) => {
      if (!this.sim) return;
      const r = this.cv.getBoundingClientRect();
      const x = Math.max(0, Math.min(1, (e.clientX - r.left - this.pad.l) / (r.width - this.pad.l - this.pad.r)));
      this.t = x * this.T;
      this.applyTime();
    };
    this.cv.addEventListener('pointerdown', (e) => { this.cv.setPointerCapture(e.pointerId); this.wasPlaying = this.playing; this.setPlaying(false); scrub(e); this.drag = true; });
    this.cv.addEventListener('pointermove', (e) => this.drag && scrub(e));
    this.cv.addEventListener('pointerup', () => { this.drag = false; if (this.wasPlaying) this.setPlaying(true); });
    document.addEventListener('keydown', (e) => { if (e.code === 'Space' && ctx.isActive('sim') && !e.target.closest('input,textarea,select,button')) { e.preventDefault(); this.toggle(); } });
    new ResizeObserver(() => { this.static = null; this.draw(); }).observe(this.cv);
    this.pad = { l: 34, r: 10, t: 16, b: 16 };
  }
  update(cur) {
    const sims = cur.sims || {};
    this.names = SCEN_ORDER.filter((n) => sims[n]).concat(Object.keys(sims).filter((n) => !SCEN_ORDER.includes(n)));
    const pills = this.root.querySelector('[data-scen]');
    pills.innerHTML = this.names.length ? this.names.map((n) => {
      const ok = sims[n].metrics?.success !== false;
      return `<button data-n="${esc(n)}" class="${n === this.scen ? 'on' : ''}"><i class="sdot${ok ? '' : ' bad'}"></i>${esc(SCEN_LABEL[n] || sims[n].title || n)}</button>`;
    }).join('') : `<span class="lab" style="padding:0 10px">${I('motion', 'sm')} Simulation pending</span>`;
    pills.querySelectorAll('button').forEach((b) => (b.onclick = () => this.select(b.dataset.n)));
    this.root.querySelector('.sim').classList.toggle('nosim', !this.names.length);
    if (!this.names.length) {
      this.root.querySelector('.side').innerHTML = emptyHTML('motion', 'No simulations yet', 'Pick & place, payload sweep, workspace and shake runs appear here once the simulation stage has results.');
      this.sim = null; this.scen = null; this.url = null;
      this.draw();
      return;
    }
    const keep = this.scen && sims[this.scen] && sims[this.scen].url === this.url;
    if (!keep) { this.scen = null; this.select(this.names.includes(this.pref) ? this.pref : this.names[0], true); }
  }
  resetCache() { this.url = null; this.scen = null; this.sim = null; }
  async select(name, quiet) {
    if (!name) { this.sim = null; this.renderSide(); this.draw(); return; }
    const meta = this.ctx.current.sims[name];
    this.scen = name;
    if (!quiet) this.pref = name;
    this.url = meta.url;
    this.root.querySelectorAll('[data-scen] button').forEach((b) => b.classList.toggle('on', b.dataset.n === name));
    this.sim = null;
    this.renderSide(meta);
    this.draw();
    try {
      const sim = await this.ctx.getJSON(this.ctx.resolve(meta.url));
      if (this.scen !== name) return;
      this.sim = sim;
      this.T = Math.max(1e-3, (sim.frames.at(-1)?.t || 0) - (sim.frames[0]?.t || 0));
      this.t = 0;
      this.static = null;
      this.idx = 0;
      this.avail = {};
      for (const j of this.ctx.current.sizing?.joints || []) this.avail[j.name] = j.torque_avail_nm;
      this.renderSide(meta);
      this.draw();
      if (this.ctx.isActive('sim')) this.startViewer();
    } catch (e) {
      console.warn(e);
      this.root.querySelector('.side').innerHTML = `<div class="empty"><p>Couldn't load ${esc(meta.url || name)}.</p></div>`;
    }
  }
  // The viewer owns the clock when it can (playTrajectory + onFrame + setTime); otherwise a local clock
  // poses the robot frame by frame. Either way the chart, phase and metric cards follow onFrame().
  async startViewer() {
    const v = this.ctx.viewer, sim = this.sim;
    if (!sim) return;
    await v.reset();
    if (this.sim !== sim) return;
    if (sim.points) v.workspace(sim.points);
    this.driven = v.canDrive();
    cancelAnimationFrame(this.raf);
    if (this.driven) {
      v.play(sim, { loop: true, speed: this.speed, onFrame: (info) => this.onFrame(info) });
      if (!this.playing) v.pauseTraj();
      if (this.t > 0) v.seek(this.t);
    } else this.loop();
  }
  onFrame(info) {
    if (!this.sim || !this.ctx.isActive('sim')) return;
    this.t = info.t;
    this.idx = info.index ?? this.indexAt(info.t);
    this.live(this.idx);
    this.draw();
  }
  indexAt(t) {
    const F = this.sim.frames, t0 = F[0].t || 0;
    let lo = 0, hi = F.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (F[m].t - t0 <= t) lo = m; else hi = m; }
    return F[hi].t - t0 <= t ? hi : lo;
  }
  loop() {
    cancelAnimationFrame(this.raf);
    let last = performance.now();
    const step = (now) => {
      if (!this.ctx.isActive('sim')) return;
      const dt = Math.min((now - last) / 1000, 0.1);
      last = now;
      if (this.playing && this.sim) { this.t = (this.t + dt * this.speed) % this.T; this.applyTime(); }
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }
  applyTime() {
    const sim = this.sim;
    if (!sim) return;
    const v = this.ctx.viewer;
    if (this.driven) { v.seek(this.t); return; }  // the viewer echoes the frame through onFrame
    this.idx = this.indexAt(this.t);
    const q = {};
    sim.joints.forEach((j, k) => (q[j] = sim.frames[this.idx].q[k]));
    v.setPose(q);
    this.live(this.idx);
    this.draw();
  }
  setPlaying(on) {
    this.playing = on;
    this.root.querySelector('.playbtn').innerHTML = I(on ? 'pause' : 'play');
    if (this.driven) { if (on) this.ctx.viewer.resumeTraj(); else this.ctx.viewer.pauseTraj(); }
  }
  toggle() { this.setPlaying(!this.playing); }
  live(i) {
    const f = this.sim.frames[i];
    const ph = this.root.querySelector('.phase');
    ph.querySelector('span').textContent = f.phase || '–';
    ph.querySelector('i').style.background = PHASE_COL[f.phase] || '#838c9d';
    this.root.querySelector('.tread').innerHTML = `${this.t.toFixed(1)} <span class="dim">/ ${this.T.toFixed(1)} s</span>`;
    const g = this.root.querySelector('[data-live="@grip"]');
    if (g) g.innerHTML = `${fmt(f.grip_n, '', 2)}<small>N</small>`;
    const t1 = this.root.querySelector('[data-live="@tau1"]');
    if (t1) t1.innerHTML = `${fmt(Math.abs(f.tau?.[1] ?? 0), '', 2)}<small>N·m</small>`;
    const arm = this.sim.joints.filter((j) => j !== 'gripper' && !/^gripper/.test(j));
    this.root.querySelectorAll('.tbars [data-j]').forEach((row) => {
      const k = arm.indexOf(row.dataset.j);
      const av = this.avail[row.dataset.j] || 2.9;
      const pct = k >= 0 ? Math.min(100, Math.abs(f.tau?.[k] ?? 0) / av * 100) : 0;
      row.querySelector('.meter i').style.left = `calc(${pct}% - 1px)`;
    });
    this.root.querySelectorAll('.legend [data-k]').forEach((el) => { el.textContent = fmt(f.tau?.[+el.dataset.k] ?? 0, '', 2); });
  }
  renderSide(meta) {
    const side = this.root.querySelector('.side');
    if (!meta) { side.innerHTML = '<div class="empty"><p>No simulation results.</p></div>'; return; }
    const m = meta.metrics || {}, ok = m.success !== false, sim = this.sim;
    const cardSpec = simCards(this.scen, m);
    const cards = cardSpec.map(([k, l, u]) => {
      if (k.startsWith('@')) return `<div class="mcard live"><div class="l">${l} · live</div><div class="v" data-live="${k}">–<small>${u}</small></div><div class="s">${k === '@grip' && m.target_grip_n ? `target ${fmt(m.target_grip_n)} N` : 'at playhead'}</div></div>`;
      const v = m[k];
      return `<div class="mcard"><div class="l">${l}</div><div class="v">${v == null ? '–' : Array.isArray(v) ? v.join('–') : typeof v === 'number' ? fmt(v) : esc(v)}<small>${u}</small></div></div>`;
    }).join('');
    const pct = m.max_torque_pct || {};
    const bars = Object.entries(pct).map(([j, p]) => {
      const cls = p > 85 ? 'bad' : p > 60 ? 'warn' : '';
      return `<div class="row" data-j="${esc(j)}"><span class="n" title="${esc(j)}">${esc(j)}</span><span class="meter"><b class="${cls}" style="width:${Math.min(p, 100)}%"></b><i></i></span><span class="p">${fmt(p, '', 0)}%</span></div>`;
    }).join('');
    const shown = new Set(['success', 'max_torque_pct', 'curve', ...cardSpec.map((c) => c[0])]);
    const extra = Object.entries(m).filter(([k, v]) => !shown.has(k) && (typeof v !== 'object' || v === null));
    const curve = Array.isArray(m.curve) ? `<div class="sec-t" style="margin-top:16px">Payload curve<span class="r">shoulder torque vs 2× margin</span></div>
      <div class="tbars">${m.curve.map((c) => `<div class="row"><span class="n">${fmt(c.payload_g, 'g', 0)}</span><span class="meter"><b class="${c.ok ? 'acc' : 'bad'}" style="width:${Math.min(c.shoulder_pct, 100)}%"></b></span><span class="p">${fmt(c.shoulder_pct, '', 0)}%</span></div>`).join('')}</div>` : '';
    side.innerHTML = `<div class="success${ok ? '' : ' fail'} fade-in"><div class="ic">${I(ok ? 'check' : 'x', 'lg')}</div><div><h4>${ok ? 'Success' : 'Failed'}</h4><p>${esc(sim?.title || meta.title || this.scen)}</p></div></div>
      <div class="mgrid">${cards}</div>
      ${bars || !sim ? `<div class="sec-t">Peak torque per joint<span class="r">% stall · ▏live</span></div><div class="tbars">${bars || '<div class="skel" style="height:90px"></div>'}</div>` : ''}
      ${curve}
      ${extra.length ? `<div class="sec-t" style="margin-top:16px">All metrics</div><dl class="kv" style="margin-top:0">${extra.map(([k, v]) => { const [lab, u] = metricLabel(k); const bad = /drop|fail|crush|collid|collision|saturat|slipped|damage/.test(k); return `<dt>${esc(lab)}</dt><dd>${typeof v === 'boolean' ? (v !== bad ? `<span class="tag good">${v ? 'yes' : 'no'}</span>` : `<span class="tag bad">${v ? 'yes' : 'no'}</span>`) : typeof v === 'number' ? fmt(v, u) : esc(v)}</dd>`; }).join('')}</dl>` : ''}`;
    const lg = this.root.querySelector('.legend');
    const arm = sim ? sim.joints.filter((j) => !/^gripper/.test(j)) : Object.keys(pct);
    lg.innerHTML = arm.map((j, k) => `<span><i style="background:${SERIES[k % SERIES.length]}"></i>${esc(jname(j))} <b data-k="${k}">–</b></span>`).join('');
    if (sim) this.live(this.idx || 0);
  }
  draw() {
    const cv = this.cv, r = cv.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (r.width < 4 || r.height < 4) return;
    const W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; this.static = null; }
    const c = cv.getContext('2d');
    const P = this.pad, sim = this.sim;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, W, H);
    c.scale(dpr, dpr);
    const w = r.width, hh = r.height;
    if (!sim) {
      c.fillStyle = '#181c25';
      for (let k = 0; k < 4; k++) c.fillRect(P.l, P.t + k * (hh - P.t - P.b) / 4 + 6, w - P.l - P.r, 6);
      return;
    }
    if (!this.static) this.static = this.renderStatic(w, hh, dpr);
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.drawImage(this.static, 0, 0);
    c.scale(dpr, dpr);
    const X = (t) => P.l + (w - P.l - P.r) * t / this.T;
    const x = X(this.t);
    c.strokeStyle = 'rgba(255,255,255,.85)';
    c.lineWidth = 1.2;
    c.beginPath(); c.moveTo(x, P.t - 4); c.lineTo(x, hh - P.b); c.stroke();
    const f = sim.frames[this.idx || 0];
    const { lo, hi } = this.range;
    const Y = (v) => P.t + (hh - P.t - P.b) * (1 - (v - lo) / (hi - lo));
    (f.tau || []).forEach((v, k) => { c.fillStyle = SERIES[k % SERIES.length]; c.beginPath(); c.arc(x, Y(v), 3.2, 0, 7); c.fill(); });
    c.fillStyle = '#e9ecf2';
    c.beginPath(); c.moveTo(x - 5, P.t - 9); c.lineTo(x + 5, P.t - 9); c.lineTo(x, P.t - 3); c.fill();
  }
  renderStatic(w, hh, dpr) {
    const off = document.createElement('canvas');
    off.width = Math.round(w * dpr); off.height = Math.round(hh * dpr);
    const c = off.getContext('2d');
    c.scale(dpr, dpr);
    const P = this.pad, sim = this.sim, fr = sim.frames;
    let lo = 0, hi = 0;
    for (const f of fr) for (const v of f.tau || []) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const pad = (hi - lo) * 0.12 || 0.1;
    lo -= pad; hi += pad;
    this.range = { lo, hi };
    const X = (t) => P.l + (w - P.l - P.r) * t / this.T;
    const Y = (v) => P.t + (hh - P.t - P.b) * (1 - (v - lo) / (hi - lo));
    // phase ribbon
    let s = 0;
    for (let i = 1; i <= fr.length; i++) {
      if (i === fr.length || fr[i].phase !== fr[s].phase) {
        const x0 = X(fr[s].t), x1 = X(i === fr.length ? this.T : fr[i].t);
        c.fillStyle = PHASE_COL[fr[s].phase] || '#838c9d';
        c.globalAlpha = 0.07;
        c.fillRect(x0, P.t, x1 - x0, hh - P.t - P.b);
        c.globalAlpha = 0.9;
        c.fillRect(x0 + 0.5, 2, Math.max(x1 - x0 - 1, 1), 4);
        c.globalAlpha = 1;
        s = i;
      }
    }
    // grid
    c.font = '10px "JetBrains Mono", monospace';
    c.textAlign = 'right';
    c.textBaseline = 'middle';
    const ticks = niceTicks(lo, hi, 4);
    for (const v of ticks) {
      c.strokeStyle = v === 0 ? '#343b49' : '#1c212b';
      c.lineWidth = 1;
      c.beginPath(); c.moveTo(P.l, Y(v)); c.lineTo(w - P.r, Y(v)); c.stroke();
      c.fillStyle = '#5d6676';
      c.fillText(v.toFixed(Math.abs(hi - lo) < 2 ? 1 : 0), P.l - 6, Y(v));
    }
    // markers
    c.textAlign = 'left';
    c.textBaseline = 'alphabetic';
    let lastLabel = -1e9;
    for (const m of sim.markers || []) {
      const x = X(m.t);
      const fits = x > lastLabel + 8;
      c.strokeStyle = 'rgba(255,255,255,.14)';
      c.setLineDash([3, 3]);
      c.beginPath(); c.moveTo(x, P.t); c.lineTo(x, hh - P.b); c.stroke();
      c.setLineDash([]);
      if (fits) { c.fillStyle = '#838c9d'; c.fillText(m.label, x + 3, hh - 4); lastLabel = x + 3 + c.measureText(m.label).width; }
    }
    // series
    const nJ = (fr[0].tau || []).length;
    for (let k = 0; k < nJ; k++) {
      c.strokeStyle = SERIES[k % SERIES.length];
      c.lineWidth = 1.6;
      c.lineJoin = 'round';
      c.beginPath();
      fr.forEach((f, i) => { const x = X(f.t), y = Y(f.tau[k]); i ? c.lineTo(x, y) : c.moveTo(x, y); });
      c.stroke();
    }
    return off;
  }
  onShow() {
    if (this.sim) this.startViewer();
    else this.ctx.viewer.reset();
    this.static = null;
    requestAnimationFrame(() => this.draw());
  }
  onHide() { cancelAnimationFrame(this.raf); this.ctx.viewer.stopTraj(); }
  tour() { this.t = 0; this.setPlaying(true); return 5200; }
}
const UNIT_SUFFIX = [['_ms2', 'm/s²'], ['_nm', 'N·m'], ['_mm', 'mm'], ['_pct', '%'], ['_deg', '°'], ['_n', 'N'], ['_g', 'g'], ['_s', 's'], ['_l', 'L'], ['_kg', 'kg']];
function metricLabel(k) {
  k = k.replace(/^(.*)_(n|mm|g|s|deg|pct)_(mean|max|min|avg)$/, '$1_$3_$2');
  let u = '';
  for (const [suf, unit] of UNIT_SUFFIX) if (k.endsWith(suf)) { u = unit; k = k.slice(0, -suf.length); break; }
  const lab = k.replace(/_/g, ' ');
  return [lab[0].toUpperCase() + lab.slice(1), u];
}
function niceTicks(lo, hi, n) {
  const span = hi - lo, step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

// ------------------------------------------------------------------ 3. Electronics
const NET_COL = { GND: '#8b95a7', V_BUS: '#ff6b5b', VIN_12V: '#ff6b5b', V_FUSED: '#ff8a7a', V_SERVO: '#ff6b5b', SW: '#ffa94d', '5V': '#ffa94d', '3V3': '#ffd43b',
  VCC_LOGIC: '#ffd43b', BUS_DATA: '#5b9dff', SERVO_SIG: '#5b9dff', UART_TX: '#9775fa', UART_RX: '#9775fa', BUS_DIR: '#e599f7', FSR_L: '#3ddc97', FSR_R: '#3ddc97',
  FSR_SENSE: '#3ddc97', USB_DP: '#66d9e8', USB_DM: '#66d9e8', ESTOP: '#e599f7' };
const netCol = (n) => NET_COL[n] || SERIES[[...String(n)].reduce((a, ch) => a + ch.charCodeAt(0), 0) % SERIES.length];
function svgEl(tag, attrs, parent) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}
export class ElecPanel {
  constructor(root, ctx) {
    this.ctx = ctx;
    root.innerHTML = `<div class="el">
      <div class="bar">
        <div class="pills" data-g="electronics.config"><span class="lab">Electronics</span><button data-v="modules">${I('cpu', 'sm')}Off-the-shelf modules</button><button data-v="custom_pcb">${I('chip', 'sm')}Custom PCB</button></div>
        <div class="pills" data-g="arm.actuator_family"><span class="lab">Actuators</span><button data-v="hobby_pwm">Hobby PWM</button><button data-v="smart_bus">Smart bus</button><button data-v="dynamixel">Dynamixel</button></div>
        <span class="grow"></span>
        <button class="btn" data-a="show3d">${I('box', 'sm')}Highlight in 3D</button>
      </div>
      <div class="main">
        <div class="subtabs"><button data-p="sch">${I('circuit', 'sm')}Schematic</button><button data-p="pcb">${I('chip', 'sm')}<span data-pcbl>PCB</span></button><button data-p="har">${I('harness', 'sm')}Harness</button><button data-p="fw">${I('code', 'sm')}Firmware<span data-fwn></span></button><div class="r" data-files></div></div>
        <div class="stage">
          <div class="pane" data-p="sch"><div class="panzoom"><div class="content"></div></div><div class="glass zoomctl"><button class="btn icon" data-z="in">${I('zoomin', 'sm')}</button><button class="btn icon" data-z="out">${I('zoomout', 'sm')}</button><button class="btn icon" data-z="fit">${I('fit', 'sm')}</button></div></div>
          <div class="pane" data-p="pcb"></div>
          <div class="pane" data-p="har"><div class="tscroll"></div></div>
          <div class="pane" data-p="fw"><div class="code"></div></div>
        </div>
      </div>
      <div class="side"></div></div>`;
    this.root = root;
    this.sub = 'sch';
    root.querySelectorAll('.subtabs [data-p]').forEach((b) => (b.onclick = () => this.showSub(b.dataset.p, true)));
    root.querySelectorAll('.pills[data-g] button').forEach((b) => (b.onclick = () => {
      const key = b.parentElement.dataset.g;
      if (b.classList.contains('on')) return;
      b.classList.add('busy');
      ctx.postParams({ [key]: b.dataset.v });
    }));
    root.querySelector('[data-a="show3d"]').onclick = async () => {
      ctx.showTab('robot');
      await sleep(60);
      ctx.viewer.highlight(['electronics', 'pcb', 'sensor', 'cable']);
      ctx.viewer.xray(true);
      ctx.toast('Electronics highlighted: controller, sensors and harness');
    };
    this.setupPanZoom();
  }
  resetCache() { this.schUrl = this.boardUrl = this.fwUrl = this.fwPrev = undefined; this.board = null; }
  showSub(p, user) {
    this.sub = p;
    this.root.querySelectorAll('.subtabs [data-p]').forEach((b) => b.classList.toggle('on', b.dataset.p === p));
    this.root.querySelectorAll('.pane').forEach((x) => x.classList.toggle('on', x.dataset.p === p));
    if (p === 'sch') requestAnimationFrame(() => this.fit());
    if (p === 'pcb' && user) this.playBoard();
    this.files();
  }
  update(cur, prev) {
    const e = cur.electronics, d = cur.design || {};
    this.root.querySelectorAll('.pills[data-g]').forEach((g) => g.querySelectorAll('button').forEach((b) => {
      b.classList.remove('busy');
      b.classList.toggle('on', String(d[g.dataset.g]) === b.dataset.v);
    }));
    nodata(this.root.querySelector('.stage'), !e, emptyHTML('chip', 'Electronics not generated yet', 'Schematic, PCB, harness and firmware appear here once the wiring stage has run for this revision.'));
    this.root.querySelector('.el').classList.toggle('noel', !e);
    if (!e) {
      this.schUrl = this.boardUrl = this.fwUrl = this.fwPrev = undefined;
      this.root.querySelector('.side').innerHTML = '';
      return;
    }
    const regen = changedNode(cur, 'wiring', 'electronics');
    this.base = e.base;
    this.root.querySelector('[data-pcbl]').textContent = e.board ? 'PCB' : 'Modules';
    this.renderSide(cur);
    this.renderHarness(e);
    // schematic
    const schUrl = this.ctx.join(e.base, e.schematic_svg);
    if (schUrl !== this.schUrl) {
      this.schUrl = schUrl;
      const content = this.root.querySelector('.panzoom .content');
      content.innerHTML = '';
      this.root.querySelector('.panzoom').insertAdjacentHTML('beforeend', '<div class="vload" data-sl><span class="spin"></span>Loading schematic…</div>');
      if (schUrl) this.ctx.getText(schUrl).then((txt) => {
        if (this.schUrl !== schUrl) return;
        content.innerHTML = txt.replace(/<\?xml[^>]*>/, '');
        const svg = content.querySelector('svg');
        if (svg) { const vb = svg.viewBox?.baseVal; this.sz = vb && vb.width ? [vb.width, vb.height] : [+svg.getAttribute('width') || 1000, +svg.getAttribute('height') || 700]; svg.setAttribute('width', this.sz[0]); svg.setAttribute('height', this.sz[1]); }
        this.fit();
      }).catch(() => (content.innerHTML = '<div class="empty" style="width:600px;height:300px"><p>Schematic unavailable.</p></div>'))
        .finally(() => this.root.querySelector('[data-sl]')?.remove());
    }
    // board / modules
    const pcb = this.root.querySelector('.pane[data-p="pcb"]');
    if (e.board) {
      const bu = this.ctx.join(e.base, e.board.json);
      if (bu !== this.boardUrl) {
        this.boardUrl = bu;
        this.board = null;
        pcb.innerHTML = `<div class="boardwrap"><svg></svg></div><div class="boardleg"></div><div class="vload"><span class="spin"></span>Loading board…</div>`;
        this.boardP = this.ctx.getJSON(bu).then((b) => { if (this.boardUrl === bu) { this.board = b; pcb.querySelector('.vload')?.remove(); this.drawBoardStatic(); } }).catch(() => {
          pcb.innerHTML = '<div class="empty"><p>Board file unavailable.</p></div>';
        });
      }
    } else {
      this.boardUrl = null; this.board = null;
      const mods = e.plan?.modules || [];
      pcb.innerHTML = `<div class="modgrid">${mods.map((m) => `<div class="modcard fade-in"><div class="t"><span class="ref">${esc(m.ref)}</span>${esc(MOD_NAMES[m.key] || m.key)}</div><div class="d">${esc(pretty(m.role))} · <span class="mono">${esc(m.key)}</span></div></div>`).join('')}
        <div class="modnote">${I('chip')}<span>This build uses off-the-shelf modules wired with a harness. Switch to <b>Custom PCB</b> to generate one controller board (schematic → placement → routing → Gerbers).</span></div></div>`;
    }
    this.renderFirmware(cur, prev);
    if (regen && this.ctx.isActive('elec') && this.sub === 'pcb') this.playBoard();
    this.files();
    if (!this.root.querySelector('.subtabs .on')) this.showSub(this.sub);
  }
  files() {
    const e = this.ctx.current?.electronics, f = this.root.querySelector('[data-files]');
    if (!e || !f) return;
    const L = (u, t) => (u ? `<a class="btn ghost" href="${esc(u)}" target="_blank" rel="noopener">${I('download', 'sm')}${t}</a>` : '');
    if (this.sub === 'sch') f.innerHTML = `<span class="chip mono">${(e.nets || []).length} nets</span>${L(this.ctx.join(e.base, e.schematic_svg), 'SVG')}`;
    else if (this.sub === 'pcb' && e.board) f.innerHTML = `<span class="tag ${e.board.drc?.pass === false ? 'bad' : 'good'}">${I(e.board.drc?.pass === false ? 'x' : 'check', 'sm')}DRC ${e.board.drc?.pass === false ? 'fails' : 'passes'}</span>${L(this.ctx.join(e.base, e.board.gerbers), 'Gerbers')}<button class="btn ghost" data-replay>${I('replay', 'sm')}Replay</button>`;
    else if (this.sub === 'fw') f.innerHTML = L(this.ctx.join(e.base, e.firmware), (e.firmware || '').split('/').pop());
    else if (this.sub === 'har') f.innerHTML = `<span class="chip mono">${(e.harness || []).length} wires</span>`;
    else f.innerHTML = '';
    f.querySelector('[data-replay]')?.addEventListener('click', () => this.playBoard());
  }
  renderSide(cur) {
    const e = cur.electronics, pb = e.power_budget || e.plan?.power_budget || {}, plan = e.plan || {};
    const rails = pb.rails || [];
    const cap = (r) => { const m = /([\d.]+)\s*A\b/.exec(r.source || ''); return m ? +m[1] : (r.v >= 9 && plan.supply?.a) || Math.max(r.peak_a * 1.4, 0.1); };
    const cfgs = e.configs || {};
    const acts = plan.actuators || [];
    this.root.querySelector('.side').innerHTML = `
      <div class="blk"><div class="sec-t">Power budget<span class="r">${pb.supply_ok === false ? '<span class="tag bad">over budget</span>' : '<span class="tag good">supply OK</span>'}</span></div><div class="rails">
      ${rails.map((r) => { const c = cap(r); return `<div class="rail"><div class="top"><b>${esc(r.name)}</b><span>${fmt(r.avg_a, '', 2)} / ${fmt(r.peak_a, '', 2)} A</span></div>
        <div class="meter"><b class="pk" style="width:${Math.min(100, r.peak_a / c * 100)}%"></b><b class="avg" style="width:${Math.min(100, r.avg_a / c * 100)}%"></b></div>
        <div class="src">${esc(r.source || '')} · avg / peak of ${fmt(c, 'A')}</div></div>`; }).join('') || '<div class="muted">–</div>'}</div></div>
      <div class="blk"><div class="sec-t">Configurations<span class="r">electronics cost / robot</span></div><div class="cfgcmp">
      ${['modules', 'custom_pcb'].filter((k) => cfgs[k]).map((k) => `<div class="cfgc${e.config === k ? ' on' : ''}" data-cfg="${k}"><div class="h">${I(k === 'custom_pcb' ? 'chip' : 'cpu', 'sm')}${k === 'custom_pcb' ? 'Custom PCB' : 'Off-the-shelf modules'}${e.config === k ? '<span class="tag acc">current</span>' : ''}<span class="c">${money(cfgs[k].cost)}</span></div><div class="d">${esc(cfgs[k].summary || '')}</div></div>`).join('')}
      </div></div>
      <div class="blk"><div class="sec-t">Bus &amp; IDs<span class="r">${esc(pretty(e.bus || plan.bus || ''))}</span></div><div class="idlist">
      ${acts.map((a) => `<span class="id">${a.id ?? a.channel ?? '–'}</span><span>${esc(a.joint)}</span><span class="k">${esc(a.key)}</span>`).join('')}
      ${(plan.sensors || []).map((s) => `<span class="id">${esc(s.ref)}</span><span>${esc(MOD_NAMES[s.key] || s.key)}</span><span class="k">${esc(s.where)}</span>`).join('')}
      </div></div>`;
    this.root.querySelectorAll('.cfgc[data-cfg]').forEach((c) => (c.onclick = () => { if (!c.classList.contains('on')) this.ctx.postParams({ 'electronics.config': c.dataset.cfg }); }));
  }
  renderHarness(e) {
    const rows = e.harness || [];
    this.root.querySelector('.pane[data-p="har"] .tscroll').innerHTML = `<table class="table"><thead><tr><th>Wire</th><th>From → to</th><th class="num">Length</th><th class="num">Cond.</th><th>Wire</th><th>Connectors</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="strong">${esc(r.name)}</td><td><span class="mono" style="font-size:11px">${esc(r.from)}</span> ${I('arrow', 'sm')} <span class="mono" style="font-size:11px">${esc(r.to)}</span></td><td class="num">${fmt(r.length_mm, 'mm', 0)}</td><td class="num">${r.conductors ?? '–'}</td><td>${esc(r.wire || '')}</td><td class="muted">${esc(r.connector_a || '')} · ${esc(r.connector_b || '')}</td></tr>`).join('')}
      </tbody></table>`;
  }
  async renderFirmware(cur, prev) {
    const e = cur.electronics, pe = prev?.electronics;
    const code = this.root.querySelector('.code');
    const url = this.ctx.join(e.base, e.firmware), purl = pe ? this.ctx.join(pe.base, pe.firmware) : null;
    if (url === this.fwUrl && purl === this.fwPrev) return;
    this.fwUrl = url; this.fwPrev = purl;
    code.innerHTML = Array.from({ length: 18 }, (_, i) => `<div class="skel" style="height:10px;margin:9px 20px 9px 60px;width:${30 + ((i * 37) % 55)}%"></div>`).join('');
    try {
      const [a, b] = await Promise.all([url ? this.ctx.getText(url) : '', purl ? this.ctx.getText(purl).catch(() => null) : null]);
      if (this.fwUrl !== url) return;
      const rows = b != null ? diffLines(b.split('\n'), a.split('\n')) : a.split('\n').map((t) => ({ t, k: '' }));
      let n = 0, no = 0;
      code.innerHTML = rows.map((r) => {
        if (r.k === 'add') n++;
        if (r.k !== 'del') no++;
        return `<div class="ln ${r.k}"><span class="no">${r.k === 'del' ? '' : no}</span><span class="g">${r.k === 'add' ? '+' : r.k === 'del' ? '−' : ''}</span><span>${hl(r.t)}</span></div>`;
      }).join('');
      this.fwChanged = n;
      this.root.querySelector('[data-fwn]').innerHTML = b != null && n ? ` <span class="tag hot">+${n}</span>` : '';
    } catch (err) { code.innerHTML = `<div class="empty"><p>Firmware unavailable (${esc(err.message)}).</p></div>`; }
  }
  setupPanZoom() {
    const pz = this.root.querySelector('.panzoom'), content = pz.querySelector('.content');
    this.view = { x: 0, y: 0, s: 1 };
    const apply = () => (content.style.transform = `translate(${this.view.x}px,${this.view.y}px) scale(${this.view.s})`);
    this.applyPZ = apply;
    pz.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const r = pz.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
      const k = Math.exp(-ev.deltaY * 0.0015), s = Math.max(0.15, Math.min(8, this.view.s * k));
      this.view.x = mx - (mx - this.view.x) * (s / this.view.s);
      this.view.y = my - (my - this.view.y) * (s / this.view.s);
      this.view.s = s;
      apply();
    }, { passive: false });
    let drag = null;
    pz.addEventListener('pointerdown', (ev) => { drag = { x: ev.clientX - this.view.x, y: ev.clientY - this.view.y }; pz.classList.add('drag'); pz.setPointerCapture(ev.pointerId); });
    pz.addEventListener('pointermove', (ev) => { if (drag) { this.view.x = ev.clientX - drag.x; this.view.y = ev.clientY - drag.y; apply(); } });
    pz.addEventListener('pointerup', () => { drag = null; pz.classList.remove('drag'); });
    pz.addEventListener('dblclick', () => this.fit());
    this.root.querySelectorAll('[data-z]').forEach((b) => (b.onclick = () => {
      if (b.dataset.z === 'fit') return this.fit();
      const r = pz.getBoundingClientRect(), k = b.dataset.z === 'in' ? 1.3 : 1 / 1.3, s = this.view.s * k;
      this.view.x = r.width / 2 - (r.width / 2 - this.view.x) * k;
      this.view.y = r.height / 2 - (r.height / 2 - this.view.y) * k;
      this.view.s = s;
      content.style.transition = 'transform .25s';
      apply();
      setTimeout(() => (content.style.transition = ''), 260);
    }));
  }
  fit() {
    const pz = this.root.querySelector('.panzoom');
    if (!this.sz || !pz.clientWidth) return;
    const [w, hh] = this.sz, s = Math.min(pz.clientWidth / w, pz.clientHeight / hh) * 0.94;
    this.view = { s, x: (pz.clientWidth - w * s) / 2, y: (pz.clientHeight - hh * s) / 2 };
    this.applyPZ();
  }
  drawBoardStatic() { this.playBoard(true); }
  // PCB build animation (ported from the v1 Studio): outline → parts drop in → ratsnest → traces route → GND pour → DRC.
  playBoard(instant) {
    const pane = this.root.querySelector('.pane[data-p="pcb"]');
    const svg = pane.querySelector('svg');
    const B = this.board;
    if (!svg || !B) return 0;
    svg.innerHTML = '';
    const [W, H] = B.size_mm;
    const fy = (y) => H - y;
    svg.setAttribute('viewBox', `-3 -5 ${W + 6} ${H + 10}`);
    let t = 0;
    const anim = (el, frames, dur, delay) => (instant ? null : el.animate(frames, { duration: dur, delay, fill: 'both', easing: 'cubic-bezier(.2,.8,.2,1)' }));
    const defs = svgEl('defs', {}, svg);
    const grad = svgEl('linearGradient', { id: 'mask', x1: 0, y1: 0, x2: 1, y2: 1 }, defs);
    svgEl('stop', { offset: 0, 'stop-color': '#103a2c' }, grad);
    svgEl('stop', { offset: 1, 'stop-color': '#0a2a20' }, grad);
    const outline = svgEl('rect', { x: 0, y: 0, width: W, height: H, rx: 1.6, fill: 'url(#mask)', stroke: '#c9b46a', 'stroke-width': 0.3 }, svg);
    anim(outline, [{ opacity: 0, transform: 'scale(.97)' }, { opacity: 1, transform: 'none' }], 500, t);
    for (const [x, y] of [[2.2, 2.2], [W - 2.2, 2.2], [2.2, H - 2.2], [W - 2.2, H - 2.2]]) {
      const m = svgEl('circle', { cx: x, cy: y, r: 1.25, fill: '#0b0e13', stroke: '#c9b46a', 'stroke-width': 0.35 }, svg);
      anim(m, [{ opacity: 0 }, { opacity: 1 }], 300, t + 200);
    }
    t += 380;
    const pourG = svgEl('g', {}, svg), traceG = svgEl('g', {}, svg), ratG = svgEl('g', {}, svg), compG = svgEl('g', {}, svg);
    const comps = B.components || [];
    const stepC = Math.max(60, Math.min(200, 2600 / Math.max(comps.length, 1)));
    for (const c of comps) {
      const g = svgEl('g', {}, compG);
      const [x0, y0, x1, y1] = c.outline;
      svgEl('rect', { x: x0, y: fy(y1), width: x1 - x0, height: y1 - y0, fill: 'rgba(255,255,255,.025)', stroke: '#eef1f5', 'stroke-width': 0.2, rx: 0.4 }, g);
      const lbl = svgEl('text', { x: x0, y: fy(y1) - 0.8, 'font-size': 1.45, fill: '#eef1f5', 'font-family': 'JetBrains Mono, monospace' }, g);
      lbl.textContent = `${c.ref} ${c.value}`;
      for (const p of c.pads) {
        svgEl('circle', { cx: p.x, cy: fy(p.y), r: 0.8, fill: '#d6b25e' }, g);
        svgEl('circle', { cx: p.x, cy: fy(p.y), r: 0.42, fill: '#0b0e13' }, g);
      }
      anim(g, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'translateY(0)' }], 420, t);
      t += stepC;
    }
    const rats = {};
    for (const r of B.ratsnest || []) {
      const l = svgEl('line', { x1: r.a[0], y1: fy(r.a[1]), x2: r.b[0], y2: fy(r.b[1]), stroke: netCol(r.net), 'stroke-width': 0.16, 'stroke-dasharray': '0.6 0.5', opacity: instant ? 0 : 0.9 }, ratG);
      anim(l, [{ opacity: 0 }, { opacity: 0.9 }], 300, t);
      (rats[r.net] = rats[r.net] || []).push(l);
    }
    t += 450;
    const doneAt = {};
    const traces = B.traces || [];
    const per = Math.max(110, Math.min(380, 5200 / Math.max(traces.length, 1)));
    for (const tr of traces) {
      const pts = tr.points.map(([x, y]) => `${x},${fy(y)}`).join(' ');
      const col = tr.layer === 'B.Cu' ? '#4f8fff' : '#e8615a';
      const pl = svgEl('polyline', { points: pts, fill: 'none', stroke: col, 'stroke-width': tr.width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: tr.layer === 'B.Cu' ? 0.9 : 1 }, traceG);
      let len = 0;
      for (let i = 1; i < tr.points.length; i++) len += Math.hypot(tr.points[i][0] - tr.points[i - 1][0], tr.points[i][1] - tr.points[i - 1][1]);
      len = Math.max(len, 0.1);
      const dur = Math.min(Math.max(len * 14, per), per * 2.2);
      if (!instant) {
        pl.style.strokeDasharray = `${len} ${len}`;
        anim(pl, [{ strokeDashoffset: len }, { strokeDashoffset: 0 }], dur, t);
      }
      t += per;
      doneAt[tr.net] = t + dur;
    }
    for (const [net, lines] of Object.entries(rats)) for (const l of lines) anim(l, [{ opacity: 0.9 }, { opacity: 0 }], 300, doneAt[net] || t);
    for (const ring of B.pour || []) {
      let d = 'M' + ring.exterior.map(([x, y]) => `${x},${fy(y)}`).join(' L') + ' Z';
      for (const ho of ring.holes || []) d += ' M' + ho.map(([x, y]) => `${x},${fy(y)}`).join(' L') + ' Z';
      const p = svgEl('path', { d, fill: '#2e6fd6', 'fill-opacity': 0.2, 'fill-rule': 'evenodd' }, pourG);
      anim(p, [{ opacity: 0 }, { opacity: 1 }], 800, t + 200);
    }
    t += 900;
    const ok = B.drc?.pass !== false;
    const badge = svgEl('text', { x: W, y: H + 3.6, 'text-anchor': 'end', 'font-size': 1.7, fill: ok ? '#3dd68c' : '#ff6b6b', 'font-family': 'JetBrains Mono, monospace', 'font-weight': 700 }, svg);
    badge.textContent = ok ? `DRC ✓  ${traces.length} traces · 0 unrouted · ${comps.length} parts · GND pour` : `DRC ✗ unrouted: ${(B.drc.unrouted || []).map((u) => u.net).join(', ')}`;
    anim(badge, [{ opacity: 0 }, { opacity: 1 }], 400, t);
    const size = svgEl('text', { x: 0, y: H + 3.6, 'font-size': 1.6, fill: '#838c9d', 'font-family': 'JetBrains Mono, monospace' }, svg);
    size.textContent = `${W} × ${H} mm · 2 layers · 1.6 mm FR4`;
    const nets = [...new Set(traces.map((x) => x.net))];
    pane.querySelector('.boardleg').innerHTML = `<span class="sw"><i style="background:#e8615a"></i>F.Cu</span><span class="sw"><i style="background:#4f8fff"></i>B.Cu</span><span class="sw"><i style="background:#2e6fd6;opacity:.6"></i>GND pour</span><span class="muted">·</span><span class="muted">${nets.length} nets · ratsnest colours:</span>${nets.slice(0, 6).map((n) => `<span class="sw"><i style="background:${netCol(n)}"></i>${esc(n)}</span>`).join('')}`;
    return t + 400;
  }
  onShow() {
    this.ctx.viewer.reset();
    if (this.sub === 'sch') requestAnimationFrame(() => this.fit());
  }
  tour(cur) {
    const e = cur.electronics;
    if (e?.board) {
      this.showSub('pcb');
      const go = () => this.playBoard();
      let dur = 6500;
      if (this.board) dur = go() + 1400; else this.boardP?.then(go);
      if (this.ctx.previous?.electronics) setTimeout(() => { if (this.ctx.isActive('elec')) this.showFirmwareChange(); }, dur);
      return dur + (this.ctx.previous?.electronics ? 3600 : 0);
    }
    this.showFirmwareChange();
    return 4200;
  }
  showFirmwareChange() {
    this.showSub('fw');
    const code = this.root.querySelector('.code');
    const first = code.querySelector('.ln.add, .ln.del');
    if (first) code.scrollTo({ top: Math.max(0, first.offsetTop - 80), behavior: 'smooth' });
    code.querySelectorAll('.ln.add').forEach((l) => { l.classList.remove('flash'); void l.offsetWidth; l.classList.add('flash'); });
  }
}
function diffLines(a, b) {
  const n = a.length, m = b.length;
  if (n * m > 4e6) { const A = new Set(a); return b.map((t) => ({ t, k: A.has(t) ? '' : 'add' })); }
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ t: b[j], k: '' }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push({ t: a[i++], k: 'del' });
    else out.push({ t: b[j++], k: 'add' });
  }
  while (i < n) out.push({ t: a[i++], k: 'del' });
  while (j < m) out.push({ t: b[j++], k: 'add' });
  return out;
}
function hl(line) {
  const s = esc(line);
  const ci = s.indexOf('//');
  const body = ci >= 0 ? s.slice(0, ci) : s, comment = ci >= 0 ? `<span class="c">${s.slice(ci)}</span>` : '';
  if (/^\s*#/.test(body)) return `<span class="pp">${body}</span>${comment}`;
  return body
    .replace(/(&quot;.*?&quot;)/g, '<span class="s">$1</span>')
    .replace(/\b(const|struct|void|return|if|else|for|while|bool|float|int|uint8_t|uint16_t|uint32_t|int16_t|char|static|true|false)\b/g, '<span class="k">$1</span>')
    .replace(/\b([A-Z][A-Z0-9_]{2,})\b/g, '<span class="t">$1</span>')
    .replace(/(?<![\w#">])(-?\d+\.?\d*f?)\b/g, '<span class="n">$1</span>') + comment;
}

// ------------------------------------------------------------------ 4. Manufacturing
export class MfgPanel {
  constructor(root, ctx) {
    this.ctx = ctx;
    root.innerHTML = `<div class="mf">
      <div class="bar"><div class="pills" data-g="build.config"><span class="lab">Build</span><button data-v="prototype">${I('box', 'sm')}Prototype</button><button data-v="pilot">${I('wrench', 'sm')}Pilot</button><button data-v="production">${I('factory', 'sm')}Production</button></div>
        <span class="chip" data-qty></span><span class="grow"></span><span class="muted" data-sum style="font-size:12px"></span></div>
      <div class="cfgs"></div>
      <div class="tablewrap"><div class="tscroll" style="bottom:38px"></div><div class="totbar"></div></div>
      <div class="vp"><div class="cap"></div></div></div>`;
    this.root = root;
    this.slot = root.querySelector('.vp');
    root.querySelectorAll('.pills[data-g] button').forEach((b) => (b.onclick = () => {
      if (b.classList.contains('on')) return;
      b.classList.add('busy');
      ctx.postParams({ 'build.config': b.dataset.v });
    }));
  }
  update(cur, prev) {
    const m = cur.manufacturing, d = cur.design || {};
    this.root.querySelectorAll('.pills[data-g] button').forEach((b) => { b.classList.remove('busy'); b.classList.toggle('on', (m?.config || d['build.config']) === b.dataset.v); });
    this.root.querySelector('[data-qty]').innerHTML = `${I('box', 'sm')}qty ${d['build.quantity'] ?? 1}`;
    this.root.querySelector('.mf').classList.toggle('nomfg', !m);
    nodata(this.root.querySelector('.tablewrap'), !m, emptyHTML('factory', 'No manufacturing plan yet', 'Per-part processes, vendors, costs, DFM checks and machine files appear here once the manufacturing stage runs.'));
    if (!m) { this.root.querySelector('.cfgs').innerHTML = ''; this.root.querySelector('.tscroll').innerHTML = ''; this.root.querySelector('.totbar').innerHTML = ''; return; }
    const cfgs = m.configs || {}, pc = prev?.manufacturing?.configs || {};
    const regen = changedNode(cur, 'manufacturing');
    this.root.querySelector('.cfgs').innerHTML = ['prototype', 'pilot', 'production'].filter((k) => cfgs[k]).map((k) => {
      const c = cfgs[k], on = m.config === k;
      const dl = pc[k] && pc[k].unit_cost !== c.unit_cost ? c.unit_cost - pc[k].unit_cost : null;
      return `<div class="ccard${on ? ' on' : ''}${on && regen ? ' fade-in' : ''}" data-v="${k}"><div class="t">${I(k === 'prototype' ? 'box' : k === 'pilot' ? 'wrench' : 'factory', 'sm')}${esc(pretty(k))}${on ? '<span class="tag acc">current</span>' : ''}</div>
        <div class="big">${money(c.unit_cost)}<small>/ unit</small>${dl ? `<span class="delta ${dl < 0 ? 'up' : 'down'}" style="font-size:11px;vertical-align:middle">${dl < 0 ? '▼' : '▲'} ${money(Math.abs(dl))}</span>` : ''}</div>
        <div class="row"><span>tooling <b>${money(c.tooling_cost, false)}</b></span><span>lead <b>${c.lead_days ?? '–'} d</b></span></div><div class="d">${esc(c.summary || '')}</div></div>`;
    }).join('');
    this.root.querySelectorAll('.ccard').forEach((c) => (c.onclick = () => { if (!c.classList.contains('on')) { this.ctx.postParams({ 'build.config': c.dataset.v }); } }));
    const parts = m.parts || [];
    this.root.querySelector('[data-sum]').textContent = `${parts.length} fabricated groups · ${parts.reduce((a, p) => a + (p.qty || 0), 0)} pieces`;
    const base = m.base;
    this.root.querySelector('.tscroll').innerHTML = `<table class="table"><thead><tr><th>Part</th><th>Process</th><th>Vendor</th><th class="num">Unit · lead</th><th>DFM</th><th>Files</th></tr></thead><tbody>
      ${parts.map((p, i) => `<tr data-i="${i}" class="${p.group === this.selGroup ? 'sel' : ''}${regen ? ' fade-in' : ''}" style="cursor:pointer;${regen ? `animation-delay:${i * 30}ms` : ''}">
        <td><div class="strong">${esc(p.name || p.group)}${p.qty > 1 ? ` <span class="muted" style="font-weight:500">×${p.qty}</span>` : ''}</div><div class="sub mono">${esc(p.group)}</div>${(p.dfm || []).length ? `<div class="dfmline" title="${esc(p.dfm.join('\n'))}">${I('alert', 'sm')}<span>${esc(p.dfm[0])}</span></div>` : ''}</td>
        <td><span class="proc"><i style="background:${PROC_COL[p.process] || '#838c9d'}"></i>${esc(pretty(p.process))}</span><div class="sub">${esc(p.material || '')}</div></td>
        <td>${p.vendor_url ? `<a href="${esc(p.vendor_url)}" target="_blank" rel="noopener" style="white-space:nowrap" title="Open ${esc(p.vendor)}">${esc(p.vendor)}</a>` : esc(p.vendor || '')}</td>
        <td class="num">${money(p.unit_cost)}<div class="sub">${p.lead_days ?? '–'} d${p.setup_cost ? ` · ${kmoney(p.setup_cost)} setup` : ''}</div></td>
        <td>${(p.dfm || []).length ? `<span class="tag warn" title="${esc(p.dfm.join('\n'))}">${I('alert', 'sm')}${p.dfm.length}</span>` : '<span class="tag good">' + I('check', 'sm') + 'ok</span>'}</td>
        <td><div class="files">${(p.files || []).map((f) => `<a href="${esc(this.ctx.join(base, f))}" target="_blank" rel="noopener" title="${esc(f)}">${esc(f.split('.').pop())}</a>`).join('')}</div></td></tr>`).join('')}
      </tbody></table>`;
    this.root.querySelectorAll('.tscroll tr[data-i]').forEach((tr) => (tr.onclick = () => this.select(parts[+tr.dataset.i], tr)));
    const t = m.totals || {};
    this.root.querySelector('.totbar').innerHTML = `<span>Fabricated<b>${money(t.parts_cost)}</b></span><span>Purchased<b>${money(t.purchased_cost)}</b></span><span>Tooling<b>${money(t.tooling_cost, false)}</b></span><span>Lead time<b>${t.lead_days ?? '–'} days</b></span>
      <span class="grow"></span>${(m.warnings || []).slice(0, 1).map((w) => `<span class="warnt" title="${esc(w)}">${I('alert', 'sm')}<span>${esc(w)}</span></span>`).join('')}`;
    this.renderCap();
  }
  async select(p, tr) {
    this.selGroup = p.group;
    this.root.querySelectorAll('.tscroll tr').forEach((r) => r.classList.toggle('sel', r === tr));
    this.renderCap(p);
    const rj = await robotJSON(this.ctx);
    let names = (rj?.parts || []).filter((x) => x.group === p.group || x.name === p.group).map((x) => x.name);
    if (!names.length && p.process === 'pcb_assembly') names = ['pcb', 'electronics'];
    if (!names.length && p.process === 'cable_assembly') names = ['cable'];
    if (!names.length) names = [p.group];
    this.ctx.viewer.highlight(names);
    this.ctx.viewer.xray(false);
  }
  renderCap(p) {
    const cap = this.root.querySelector('.cap');
    cap.innerHTML = p ? `<div class="glass fade-in"><div class="t">${esc(p.name || p.group)} <span class="muted">×${p.qty}</span></div><div class="d">${esc(pretty(p.process))} · ${esc(p.material || '')} · ${money(p.unit_cost)} · ${p.lead_days ?? '–'} d</div></div>`
      : `<div class="glass"><div class="t">Select a part</div><div class="d">Click a row to locate it on the robot.</div></div>`;
  }
  onShow() { const v = this.ctx.viewer; v.reset().then(() => { if (this.selGroup) { const i = (this.ctx.current?.manufacturing?.parts || []).findIndex((p) => p.group === this.selGroup); const tr = this.root.querySelector(`tr[data-i="${i}"]`); if (tr) this.select(this.ctx.current.manufacturing.parts[i], tr); } }); }
  onHide() { this.ctx.viewer.highlight(null); }
  tour(cur) {
    const parts = cur.manufacturing?.parts || [];
    const i = parts.findIndex((p) => (p.dfm || []).length);
    setTimeout(() => { const tr = this.root.querySelector(`tr[data-i="${Math.max(i, 0)}"]`); if (tr && this.ctx.isActive('mfg')) this.select(parts[Math.max(i, 0)], tr); }, 900);
    return 4200;
  }
}

// ------------------------------------------------------------------ 5. Assembly
export class AsmPanel {
  constructor(root, ctx) {
    this.ctx = ctx;
    root.innerHTML = `<div class="as"><div class="steps"></div><div class="stage"><div class="vp"></div>
      <div class="ov tl glass ctl"><button class="btn icon" data-a="prev" title="Previous step">${I('prev')}</button><button class="btn icon" data-a="auto" title="Autoplay">${I('play')}</button><button class="btn icon" data-a="next" title="Next step">${I('next')}</button><div class="prog"></div><span class="muted" data-pos style="font-size:12px;margin-right:6px;white-space:nowrap"></span></div>
      </div><div class="cardcol"><div class="stepcard"></div></div></div>`;
    this.root = root;
    this.slot = root.querySelector('.vp');
    this.i = 0;
    root.querySelector('[data-a="prev"]').onclick = () => { this.stopAuto(); this.go(this.i - 1); };
    root.querySelector('[data-a="next"]').onclick = () => { this.stopAuto(); this.go(this.i + 1); };
    root.querySelector('[data-a="auto"]').onclick = () => (this.timer ? this.stopAuto() : this.startAuto());
  }
  update(cur) {
    const m = cur.manual;
    this.steps = m?.steps || [];
    this.manual = m;
    if (this.i >= this.steps.length) this.i = 0;
    const tot = m?.time_min_total ?? this.steps.reduce((a, s) => a + (s.time_min || 0), 0);
    const html = m?.html ? this.ctx.resolve(m.html) : null;
    this.root.querySelector('.steps').innerHTML = `<div class="hd"><div><div class="t">Assembly manual</div><div class="muted" style="font-size:11.5px">${this.steps.length} steps · ${tot} min · ${(m?.tools || []).length} tools</div></div>${html ? `<a class="btn" href="${esc(html)}" target="_blank" rel="noopener">${I('book', 'sm')}Manual ${I('ext', 'sm')}</a>` : ''}</div>` +
      (this.steps.length ? this.steps.map((s, i) => `<div class="stepi" data-i="${i}"><span class="no">${s.index ?? i + 1}</span><div><div class="t">${esc(s.title)}</div><div class="m">${s.time_min ?? '–'} min · ${(s.parts || []).length} parts${(s.fasteners || []).length ? ` · ${s.fasteners.reduce((a, f) => a + (f.qty || 0), 0)} fasteners` : ''}</div></div></div>`).join('')
        : emptyHTML('wrench', 'No manual yet', 'Step-by-step assembly appears here once the manual stage runs.'));
    this.root.querySelectorAll('.stepi').forEach((el) => (el.onclick = () => { this.stopAuto(); this.go(+el.dataset.i); }));
    this.root.querySelector('.prog').innerHTML = this.steps.map(() => '<b></b>').join('');
    this.render();
  }
  go(i) {
    if (!this.steps.length) return;
    this.i = (i + this.steps.length) % this.steps.length;
    this.render();
    if (this.ctx.isActive('asm')) this.ctx.viewer.step(this.steps[this.i].index ?? this.i);
  }
  render() {
    const s = this.steps[this.i];
    this.root.querySelectorAll('.stepi').forEach((el, k) => { el.classList.toggle('on', k === this.i); el.classList.toggle('done', k < this.i); });
    this.root.querySelectorAll('.prog b').forEach((el, k) => (el.className = k < this.i ? 'done' : k === this.i ? 'on' : ''));
    this.root.querySelector('[data-pos]').textContent = s ? `Step ${this.i + 1} of ${this.steps.length}` : '';
    this.root.querySelector('.stepi.on')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    const card = this.root.querySelector('.stepcard');
    this.root.querySelector('.ctl').style.visibility = s ? '' : 'hidden';
    if (!s) { card.innerHTML = emptyHTML('book', 'Nothing to assemble yet', 'Parts, fasteners with torques, tools and timing for each step show up here.'); return; }
    card.innerHTML = `<div class="fade-in"><div class="k">STEP ${s.index ?? this.i + 1} · ${I('clock', 'sm')} ${s.time_min ?? '–'} min</div><h3>${esc(s.title)}</h3>
      ${s.instructions ? `<p class="ins">${esc(s.instructions)}</p>` : ''}
      ${(s.parts || []).length ? `<div class="sec-t">Parts<span class="r">${s.parts.length}</span></div><div class="chips">${s.parts.map((p) => `<span class="chip mono">${esc(p)}</span>`).join('')}</div>` : ''}
      ${(s.fasteners || []).length ? `<div class="sec-t">Fasteners<span class="r">torque</span></div><div class="fast">${s.fasteners.map((f) => `<span>${esc(f.spec)}</span><span class="q">×${f.qty}</span><span class="tq">${f.torque_nm ? fmt(f.torque_nm, 'N·m') : '—'}</span>`).join('')}</div>` : ''}
      ${(s.tools || []).length ? `<div class="sec-t">Tools</div><div class="chips">${s.tools.map((t) => `<span class="chip">${I('wrench', 'sm')}${esc(t)}</span>`).join('')}</div>` : ''}
      ${(s.warnings || []).map((w) => `<div class="wbox">${I('alert', 'sm')}<span>${esc(w)}</span></div>`).join('')}</div>`;
  }
  startAuto() {
    this.stopAuto();
    this.root.querySelector('[data-a="auto"]').innerHTML = I('pause');
    this.root.querySelector('[data-a="auto"]').classList.add('on');
    this.timer = setInterval(() => this.go(this.i + 1), 3200);
  }
  stopAuto() {
    clearInterval(this.timer);
    this.timer = null;
    const b = this.root.querySelector('[data-a="auto"]');
    b.innerHTML = I('play');
    b.classList.remove('on');
  }
  onShow() { this.ctx.viewer.reset().then(() => this.go(this.i)); }
  onHide() { this.stopAuto(); this.ctx.viewer.step(null); }
  tour() { this.go(0); this.startAuto(); return 4600; }
}

// ------------------------------------------------------------------ 6. BOM & sourcing
const CATS = ['actuator', 'electronics', 'pcb', 'cable', 'connector', 'fastener', 'mechanical', 'fabricated'];
const catLabel = (c) => { const t = { pcb: 'PCB' }[c] || String(c || '').replace(/_/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); };
export class BomPanel {
  constructor(root, ctx) {
    this.ctx = ctx;
    root.innerHTML = `<div class="bm"><div class="stats"></div><div class="filters"></div><div class="tablewrap"><div class="tscroll"></div></div></div>`;
    this.root = root;
    this.cat = 'all';
    this.mob = 'all';
  }
  update(cur, prev) {
    this.cur = cur;
    const b = cur.bom;
    this.root.querySelector('.bm').classList.toggle('nobom', !b);
    nodata(this.root.querySelector('.tablewrap'), !b, emptyHTML('cart', 'No BOM yet', 'Every purchased and fabricated item, with vendors, prices and make-vs-buy, appears here once the BOM stage runs.'));
    if (!b) { this.root.querySelector('.tscroll').innerHTML = ''; return; }
    const rows = b.rows || [], t = b.totals || {};
    const pt = prev?.bom?.totals || {};
    const total = t.total ?? rows.reduce((a, r) => a + (r.ext_price || 0), 0);
    const buy = t.buy ?? rows.filter((r) => r.make_or_buy === 'buy').reduce((a, r) => a + r.ext_price, 0);
    const make = t.make ?? rows.filter((r) => r.make_or_buy === 'make').reduce((a, r) => a + r.ext_price, 0);
    const pcs = rows.reduce((a, r) => a + (r.qty || 0), 0);
    const per = t.per_config || {};
    const curCfg = cur.manufacturing?.config || cur.design?.['build.config'];
    const pmax = Math.max(...Object.values(per), 1);
    const dTot = pt.total != null && pt.total !== total ? total - pt.total : null;
    const vendors = new Set(rows.flatMap((r) => (r.vendors || []).map((v) => v.vendor)));
    this.root.querySelector('.stats').innerHTML = `
      <div class="stat"><div class="l">${I('cart', 'sm')}Unit cost · ${esc(pretty(curCfg || ''))}</div><div class="v">${money(total)}</div><div class="s">${dTot ? `<span class="delta ${dTot < 0 ? 'up' : 'down'}" style="margin:0">${dTot < 0 ? '▼' : '▲'} ${money(Math.abs(dTot))}</span> vs ${esc(prev.rev)}` : `${rows.length} line items · ${pcs} pieces`}</div></div>
      <div class="stat"><div class="l"><span class="mob buy">buy</span>Purchased</div><div class="v">${money(buy)}</div><div class="s">${rows.filter((r) => r.make_or_buy === 'buy').length} items · ${vendors.size} vendors</div></div>
      <div class="stat"><div class="l"><span class="mob make">make</span>Built in-house</div><div class="v">${money(make)}</div><div class="s">${rows.filter((r) => r.make_or_buy === 'make').length} items · printed / fabricated</div></div>
      <div class="stat"><div class="l">${I('target', 'sm')}Buy vs build</div><div class="v">${total ? Math.round(buy / total * 100) : 0}<small style="font-size:13px;color:var(--muted)">% bought</small></div>
        <div class="stack"><b style="width:${total ? buy / total * 100 : 0}%;background:var(--accent)"></b><b style="width:${total ? make / total * 100 : 0}%;background:var(--cyan)"></b></div></div>
      <div class="stat pcfg"><div class="l" style="margin-bottom:4px">${I('factory', 'sm')}Per build config</div>
        ${['prototype', 'pilot', 'production'].filter((k) => per[k] != null).map((k) => `<div class="row"><span class="n${k === curCfg ? ' on' : ''}">${esc(pretty(k))}</span><span class="meter"><b class="${k === curCfg ? 'acc' : ''}" style="width:${per[k] / pmax * 100}%;${k === curCfg ? '' : 'background:var(--bg5);filter:brightness(1.6)'}"></b></span><span class="p">${money(per[k], false)}</span></div>`).join('')}</div>`;
    this.renderFilters();
    this.renderTable(changedNode(cur, 'bom'));
  }
  renderFilters() {
    const rows = this.cur.bom.rows || [];
    const cnt = (c) => rows.filter((r) => r.category === c).length;
    const cats = CATS.filter((c) => cnt(c)).concat([...new Set(rows.map((r) => r.category))].filter((c) => !CATS.includes(c)));
    this.root.querySelector('.filters').innerHTML = `<button class="fchip${this.cat === 'all' ? ' on' : ''}" data-c="all">All <span class="c">${rows.length}</span></button>` +
      cats.map((c) => `<button class="fchip${this.cat === c ? ' on' : ''}" data-c="${esc(c)}" title="${cnt(c)} items"><i style="background:${CAT_COL[c] || '#838c9d'}"></i>${esc(catLabel(c))}</button>`).join('') +
      `<span class="grow"></span><div class="pills" data-m><button data-m="all" class="${this.mob === 'all' ? 'on' : ''}">All</button><button data-m="buy" class="${this.mob === 'buy' ? 'on' : ''}">Buy</button><button data-m="make" class="${this.mob === 'make' ? 'on' : ''}">Make</button></div>`;
    this.root.querySelectorAll('.fchip').forEach((b) => (b.onclick = () => { this.cat = b.dataset.c; this.renderFilters(); this.renderTable(); }));
    this.root.querySelectorAll('[data-m] button').forEach((b) => (b.onclick = () => { this.mob = b.dataset.m; this.renderFilters(); this.renderTable(); }));
  }
  renderTable(regen) {
    const rows = (this.cur.bom.rows || []).filter((r) => (this.cat === 'all' || r.category === this.cat) && (this.mob === 'all' || r.make_or_buy === this.mob));
    const sum = rows.reduce((a, r) => a + (r.ext_price || 0), 0);
    this.root.querySelector('.tscroll').innerHTML = `<table class="table"><thead><tr><th>Item</th><th>Category</th><th class="num">Qty</th><th>Make / buy</th><th>How</th><th>Vendors</th><th class="num">Ext. price</th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr class="${regen ? 'fade-in' : ''}" style="${regen ? `animation-delay:${i * 18}ms` : ''}"><td class="strong">${esc(r.item)}</td>
        <td><span class="catd"><i style="background:${CAT_COL[r.category] || '#838c9d'}"></i>${esc(catLabel(r.category))}</span></td><td class="num">${r.qty}</td>
        <td><span class="mob ${r.make_or_buy === 'make' ? 'make' : 'buy'}">${esc(r.make_or_buy)}</span></td><td><div class="how">${esc(r.how || '')}</div></td>
        <td><div class="vend">${(r.vendors || []).map((v) => { const sel = r.selected && v.vendor === r.selected.vendor; return v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener" class="${sel ? 'selv' : ''}" title="${esc(v.note || '')}">${esc(v.vendor)} <span class="pr">${money(v.unit_price)}${v.lead_days != null ? ` · ${v.lead_days}d` : ''}</span></a>` : `<a class="${sel ? 'selv' : ''}">${esc(v.vendor)} <span class="pr">${money(v.unit_price)}</span></a>`; }).join('')}</div></td>
        <td class="num strong">${money(r.ext_price)}</td></tr>`).join('')}
      <tr><td colspan="6" style="text-align:right;color:var(--muted)">${rows.length} items shown</td><td class="num strong" style="color:var(--ink)">${money(sum)}</td></tr></tbody></table>`;
  }
  onShow() { this.ctx.viewer.reset(); }
  tour() { return 3400; }
}

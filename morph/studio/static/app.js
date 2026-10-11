import * as THREE from './vendor/three.module.min.js';
import { STLLoader } from './vendor/STLLoader.js';
import { OrbitControls } from './vendor/OrbitControls.js';

const $ = (s) => document.querySelector(s);
const NODES = [['cad', 'Finger CAD'], ['analysis', 'Checks'], ['sim', 'Simulation'], ['mech', 'Gripper mech'], ['electronics', 'Electronics + PCB'], ['fab', 'Fab files'], ['bom', 'BOM']];
const SUGGEST = ['Make it work for a strawberry', 'Use an SG90 servo instead', 'Switch to an ESP32',
  'Make the fingers 10 mm longer', 'Gentler grip', 'Use a V-groove pad', 'Go back to a raw egg'];
const METRICS = [
  ['feasible', 'Passes all checks', '', 'hi'], ['sim_success_rate', 'Sim grasp success', '', 'hi'],
  ['min_hold_force_n', 'Force needed to hold (sim)', 'N', 'lo'], ['target_force_n', 'Firmware grip target', 'N', null],
  ['servo', 'Servo', '', null], ['servo_stall_force_n', 'Servo stall force', 'N', null],
  ['open_loop_crushes', 'Open-loop would damage object', '', 'lo'], ['tip_deflection_mm', 'Finger deflection', 'mm', 'lo'],
  ['bending_sf', 'Bending safety factor', '', 'hi'], ['fab_process', 'Process', '', null],
  ['jaw_travel_mm', 'Jaw travel (each)', 'mm', 'hi'], ['servo_sweep_deg', 'Servo sweep', '°', null],
  ['fab_minutes', 'Print / fab time', 'min', 'lo'], ['fab_cost', 'Fab cost', '$', 'lo'], ['bom_cost', 'Total BOM', '$', 'lo']];
const NET_COLORS = { GND: '#9aa5b1', V_SERVO: '#ff5a5f', VCC_LOGIC: '#f5a623', SERVO_SIG: '#4aa3ff', FSR_SENSE: '#3ddc97' };

let state = { current: null, previous: null };
let tourTimer = null;

// ---------------------------------------------------------------- chat
function addMsg(role, text, changes) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.textContent = text;
  if (changes && Object.keys(changes).length) {
    const c = document.createElement('div');
    c.className = 'chips';
    for (const [k, v] of Object.entries(changes)) {
      const s = document.createElement('span');
      s.className = 'chip';
      s.textContent = `${k} = ${v}`;
      c.appendChild(s);
    }
    el.appendChild(c);
  }
  $('#messages').appendChild(el);
  $('#messages').scrollTop = 1e9;
  return el;
}
function sendChat(text) {
  fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text }) });
}
$('#form').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = $('#input').value.trim();
  if (t) { sendChat(t); $('#input').value = ''; }
});
for (const s of SUGGEST) {
  const b = document.createElement('button');
  b.textContent = s;
  b.onclick = () => sendChat(s);
  $('#suggest').appendChild(b);
}

// ---------------------------------------------------------------- pipeline strip
function renderPipeline(prov = {}, timings = {}) {
  $('#pipeline').innerHTML = '';
  for (const [k, label] of NODES) {
    const d = document.createElement('div');
    d.className = 'node';
    d.id = `node-${k}`;
    const p = prov[k] || '';
    if (p === 'computed') d.classList.add('computed');
    if (p.startsWith('reused')) d.classList.add('reused');
    d.innerHTML = `<div class="n">${label}</div><div class="s">${p === 'computed' ? `regenerated${timings[k] != null ? ' · ' + timings[k] + ' s' : ''}` : p ? 'reused' : '—'}</div>`;
    $('#pipeline').appendChild(d);
  }
}
function nodeEvent(ev) {
  const d = $(`#node-${ev.node}`);
  if (!d) return;
  d.className = `node ${ev.status}`;
  d.querySelector('.s').textContent = ev.status === 'computing' ? 'regenerating…' :
    ev.status === 'computed' ? `regenerated · ${ev.seconds} s` : 'reused (unaffected)';
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.dataset.view === name));
  if (name === 'cad') cadView.resize();
  if (name === 'fab') fabView.resize();
}
document.querySelectorAll('#tabs button').forEach((b) => (b.onclick = () => { stopTour(); showTab(b.dataset.tab); if (b.dataset.tab === 'board') playBoard(); }));

// ---------------------------------------------------------------- three.js viewers
class Viewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 1, 5000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.autoRotate = true;
    this.controls.autoRotateSpeed = 0.8;
    this.scene.add(new THREE.HemisphereLight(0xdde8ff, 0x202833, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.8);
    sun.position.set(120, 200, 160);
    this.scene.add(sun);
    this.group = new THREE.Group();
    this.scene.add(this.group);
    this.loader = new STLLoader();
    const tick = () => { requestAnimationFrame(tick); this.controls.update(); this.renderer.render(this.scene, this.camera); };
    tick();
    window.addEventListener('resize', () => this.resize());
    this.resize();
  }
  resize() {
    const w = this.el.clientWidth || 800, h = this.el.clientHeight || 600;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
  clear() { while (this.group.children.length) this.group.remove(this.group.children[0]); }
  load(url) { return new Promise((res, rej) => this.loader.load(url + '?t=' + Date.now(), res, undefined, rej)); }
  frame(dist = 1.0) {
    const box = new THREE.Box3().setFromObject(this.group);
    const size = box.getSize(new THREE.Vector3()).length() || 100;
    const c = box.getCenter(new THREE.Vector3());
    this.controls.target.copy(c);
    this.camera.position.set(c.x + size * 0.55 * dist, c.y + size * 0.3 * dist, c.z - size * 1.0 * dist);  // front: finger side
  }
}
const cadView = new Viewer($('#cad3d'));
const fabView = new Viewer($('#fab3d'));

async function renderCad(cur, prev) {
  const g = cur.geom;
  cadView.clear();
  const colors = [0xd9534f, 0xff9a4a];
  const geoms = await Promise.all(g.parts.map((u) => cadView.load(u)));
  for (const side of [-1, 1]) {
    const finger = new THREE.Group();
    geoms.forEach((geo, i) => {
      const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: colors[i % 2], metalness: 0.1, roughness: 0.55, side: THREE.DoubleSide }));
      finger.add(m);
    });
    finger.rotation.z = -Math.PI / 2;               // profile u (along finger) points down
    finger.position.set(-g.center_v, g.pad_center_u, -g.width / 2);
    const holder = new THREE.Group();
    holder.add(finger);
    if (side > 0) holder.scale.x = -1;              // mirror for the opposite jaw
    cadView.group.add(holder);
  }
  // object as gripped
  const objMat = new THREE.MeshStandardMaterial({ color: 0xffe2b8, transparent: true, opacity: 0.55, roughness: 0.4 });
  let obj;
  if (g.shape === 'sphere') obj = new THREE.Mesh(new THREE.SphereGeometry(g.R, 48, 32), objMat);
  else if (g.shape === 'box') obj = new THREE.Mesh(new THREE.BoxGeometry(g.R * 2, g.R * 2, g.obj_len), objMat);
  else {
    obj = new THREE.Mesh(new THREE.CapsuleGeometry(g.R, Math.max(g.obj_len - 2 * g.R, 1), 16, 48), objMat);
    obj.rotation.x = Math.PI / 2;
  }
  cadView.group.add(obj);
  // generated gripper mechanism (already in world coordinates)
  const MECH_COLORS = { rack_a: 0x4aa3ff, rack_b: 0x3ddc97, boss_a: 0x6f8296, boss_b: 0x6f8296, pinion: 0xf5a623,
    plate: 0x9aa7b4, servo: 0x22262b, horn: 0xf2f2f2 };
  const mechGeoms = await Promise.all((cur.mech_parts || []).map((p) => cadView.load(p.url).then((geo) => [p.name, geo])));
  for (const [name, geo] of mechGeoms) {
    const mat = new THREE.MeshStandardMaterial({ color: MECH_COLORS[name] ?? 0x888888, roughness: 0.6, metalness: name === 'servo' ? 0.2 : 0.05,
      transparent: name === 'plate', opacity: name === 'plate' ? 0.85 : 1, side: THREE.DoubleSide });
    cadView.group.add(new THREE.Mesh(geo, mat));
  }
  // previous revision's profile as a ghost outline on the left finger
  if (prev && prev.geom && JSON.stringify(prev.geom.profile) !== JSON.stringify(g.profile)) {
    const ring = prev.geom.profile[0];
    const pts = ring.map(([u, v]) => new THREE.Vector3(v - prev.geom.center_v, -u + prev.geom.pad_center_u, g.width / 2 + 0.6));
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 2, gapSize: 1.5, transparent: true, opacity: 0.7 }));
    line.computeLineDistances();
    cadView.group.add(line);
  }
  cadView.frame(1.1);
  const d = cur.design;
  $('#cadLegend').innerHTML = `<b>${g.object}</b> · finger ${d['finger.length_mm']} × ${d['finger.thickness_mm']} mm, ${d['finger.pad']} pad, ${d['finger.material']}` +
    (g.sensor !== 'none' ? ` · <b style="color:#ff9a4a">pad</b> slides into a dovetail over the ${g.sensor.toUpperCase()}` : '') +
    (cur.mech?.teeth ? `<br>gripper: ${cur.mech.servo?.toUpperCase()} → ${cur.mech.teeth}-tooth pinion → 2 racks · ${cur.mech.travel_mm} mm travel per jaw · servo ${cur.mech.servo_open_deg}°–${cur.mech.servo_closed_deg}°` : '') +
    (prev ? ' · dashed = previous revision' : '');
}

async function renderFab(cur) {
  fabView.clear();
  const f = cur.fab;
  const plate = f.files.find((u) => u.endsWith('plate.stl'));
  const bed = new THREE.Mesh(new THREE.BoxGeometry(256, 2, 256), new THREE.MeshStandardMaterial({ color: 0x1e252d, roughness: 0.9 }));
  bed.position.y = -1;
  fabView.group.add(bed);
  const grid = new THREE.GridHelper(256, 16, 0x3a4652, 0x2a333c);
  fabView.group.add(grid);
  const body = f.files.find((u) => u.endsWith('gripper_plate.stl'));
  const plates = [[plate, 0xff9a4a, -60], [body, 0x9aa7b4, 50]];
  for (const [url, color, zOff] of plates) {
    if (!url) continue;
    const geo = await fabView.load(url);
    geo.computeBoundingBox();
    const bb = geo.boundingBox;
    const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.6 }));
    m.rotation.x = -Math.PI / 2;                    // print flat: profile on the bed, extrusion = height
    m.position.set(-(bb.min.x + bb.max.x) / 2, 0, (bb.min.y + bb.max.y) / 2 + zOff - (bb.max.y - bb.min.y) / 2);
    fabView.group.add(m);
  }
  fabView.frame(0.55);
  const links = f.files.map((u) => `<a href="${u}" style="color:#4aa3ff">${u.split('/').pop()}</a>`).join(' · ');
  $('#fabLegend').innerHTML = `<b>${f.process}</b> · ${f.minutes} min · $${f.cost}<br>${links}` +
    (f.dfm.length ? `<br>⚠ ${f.dfm.join('<br>⚠ ')}` : '');
}

// ---------------------------------------------------------------- PCB build animation
let boardData = null;
async function renderBoard(cur) {
  if (!cur.board_url) { boardData = null; $('#board').innerHTML = ''; $('#boardLegend').textContent = 'No force sensor in this design, so there is no carrier board.'; return; }
  boardData = await (await fetch(cur.board_url + '?t=' + Date.now())).json();
  const e = cur.electronics;
  $('#boardLegend').innerHTML = `<b>${e.mcu}</b> + <b>${e.servo}</b> + <b>${e.sensor}</b> · divider ${e.sensing?.divider_ohm ?? '–'} Ω · ` +
    `${boardData.size_mm[0]} × ${boardData.size_mm[1]} mm, 2 layers · ` +
    `<a style="color:#4aa3ff" href="${cur.board_url.replace('board.json', 'gerbers/gerbers.zip')}">gerbers.zip</a> · ` +
    `<a style="color:#4aa3ff" href="${cur.firmware_url}">firmware</a>`;
}
function svgEl(tag, attrs, parent) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}
function playBoard() {
  const svg = $('#board');
  svg.innerHTML = '';
  if (!boardData) return 0;
  const [W, H] = boardData.size_mm;
  const fy = (y) => H - y;
  svg.setAttribute('viewBox', `-4 -4 ${W + 8} ${H + 8}`);
  let t = 0;
  const anim = (el, frames, dur, delay) => el.animate(frames, { duration: dur, delay, fill: 'both', easing: 'ease-out' });
  const outline = svgEl('rect', { x: 0, y: 0, width: W, height: H, rx: 1.2, fill: '#0b3d2e', stroke: '#d4c06b', 'stroke-width': 0.25 }, svg);
  anim(outline, [{ opacity: 0 }, { opacity: 1 }], 400, t); t += 300;
  const pourG = svgEl('g', {}, svg), traceG = svgEl('g', {}, svg), ratG = svgEl('g', {}, svg), compG = svgEl('g', {}, svg);
  // components drop in
  for (const c of boardData.components) {
    const g = svgEl('g', {}, compG);
    const [x0, y0, x1, y1] = c.outline;
    svgEl('rect', { x: x0, y: fy(y1), width: x1 - x0, height: y1 - y0, fill: 'none', stroke: '#f2f2f2', 'stroke-width': 0.18, rx: 0.4 }, g);
    const lbl = svgEl('text', { x: x0, y: fy(y1) - 0.7, 'font-size': 1.2, fill: '#f2f2f2', 'font-family': 'monospace' }, g);
    lbl.textContent = `${c.ref} ${c.value}`;
    for (const p of c.pads) {
      svgEl('circle', { cx: p.x, cy: fy(p.y), r: 0.8, fill: '#d4af37' }, g);
      svgEl('circle', { cx: p.x, cy: fy(p.y), r: 0.45, fill: '#0b3d2e' }, g);
    }
    anim(g, [{ opacity: 0, transform: 'translateY(-6px)' }, { opacity: 1, transform: 'translateY(0)' }], 350, t);
    t += 220;
  }
  // ratsnest
  const rats = {};
  for (const r of boardData.ratsnest) {
    const l = svgEl('line', { x1: r.a[0], y1: fy(r.a[1]), x2: r.b[0], y2: fy(r.b[1]), stroke: NET_COLORS[r.net] || '#fff', 'stroke-width': 0.15, 'stroke-dasharray': '0.6 0.5', opacity: 0.9 }, ratG);
    anim(l, [{ opacity: 0 }, { opacity: 0.9 }], 300, t);
    (rats[r.net] = rats[r.net] || []).push(l);
  }
  t += 500;
  // traces route one by one
  const doneAt = {};
  for (const tr of boardData.traces) {
    const pts = tr.points.map(([x, y]) => `${x},${fy(y)}`).join(' ');
    const pl = svgEl('polyline', { points: pts, fill: 'none', stroke: tr.layer === 'B.Cu' ? '#4aa3ff' : '#ff5a4f', 'stroke-width': tr.width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, traceG);
    let len = 0;
    for (let i = 1; i < tr.points.length; i++) len += Math.hypot(tr.points[i][0] - tr.points[i - 1][0], tr.points[i][1] - tr.points[i - 1][1]);
    len = Math.max(len, 0.1);
    const dur = Math.min(Math.max(len * 18, 250), 900);
    pl.style.strokeDasharray = `${len} ${len}`;
    anim(pl, [{ strokeDashoffset: len }, { strokeDashoffset: 0 }], dur, t);
    t += dur * 0.85;
    doneAt[tr.net] = t;
  }
  for (const [net, lines] of Object.entries(rats)) for (const l of lines) anim(l, [{ opacity: 0.9 }, { opacity: 0 }], 300, doneAt[net] || t);
  // ground pour
  for (const ring of boardData.pour) {
    let d = 'M' + ring.exterior.map(([x, y]) => `${x},${fy(y)}`).join(' L') + ' Z';
    for (const h of ring.holes) d += ' M' + h.map(([x, y]) => `${x},${fy(y)}`).join(' L') + ' Z';
    const p = svgEl('path', { d, fill: '#2c6fbb', 'fill-opacity': 0.32, 'fill-rule': 'evenodd' }, pourG);
    anim(p, [{ opacity: 0 }, { opacity: 1 }], 700, t);
  }
  t += 700;
  const ok = boardData.drc.pass;
  const badge = svgEl('text', { x: W, y: H + 3.2, 'text-anchor': 'end', 'font-size': 1.5, fill: ok ? '#3ddc97' : '#ff5a5f', 'font-family': 'monospace', 'font-weight': 700 }, svg);
  badge.textContent = ok ? `DRC ✓  ${boardData.traces.length} traces · 0 unrouted · GND pour` : `DRC ✗ unrouted: ${boardData.drc.unrouted.map((u) => u.net).join(', ')}`;
  anim(badge, [{ opacity: 0 }, { opacity: 1 }], 400, t);
  return t + 400;
}

// ---------------------------------------------------------------- firmware diff
function renderFirmware(cur, prev) {
  const lines = (cur.firmware || '// no firmware: this design has no force sensor').split('\n');
  const old = new Set((prev?.firmware || '').split('\n'));
  let changed = 0;
  $('#fw').innerHTML = lines.map((l) => {
    const esc = l.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    if (prev && prev.firmware && !old.has(l)) { changed++; return `<span class="chg">${esc}</span>`; }
    return esc;
  }).join('\n');
  $('#fwHead').innerHTML = `<b>morph_grip.ino</b> · generated for ${cur.electronics.mcu || '–'} · ` +
    (prev ? `<b style="color:#3ddc97">${changed} line${changed === 1 ? '' : 's'} changed</b> vs ${prev.rev}` : 'first revision') +
    (cur.firmware_url ? ` · <a style="color:#4aa3ff" href="${cur.firmware_url}">download</a>` : '');
  return changed;
}

function renderSim(cur) {
  $('#simWrap').innerHTML = cur.gif ? `<img src="${cur.gif}?t=${Date.now()}" alt="grasp simulation">` :
    'Rendering the grasp simulation… (needs an OpenGL context; results are already in the metrics)';
}

// ---------------------------------------------------------------- inspector
const fmt = (v, u) => v === null || v === undefined ? '–' : typeof v === 'boolean' ? (v ? 'yes' : 'no') :
  typeof v === 'number' ? (u === '$' ? `$${v.toFixed(2)}` : `${+v.toFixed(2)}${u ? ' ' + u : ''}`) : `${v}`;
function renderInspector(cur, prev) {
  const ch = cur.changes || {};
  $('#changes').innerHTML = Object.keys(ch).length ? Object.entries(ch).map(([k, [a, b]]) =>
    `<div>${k}: <span class="old">${a}</span> → <span class="new">${b}</span></div>`).join('') : '<span class="muted">first revision</span>';
  const rows = [];
  for (const [k, label, unit, better] of METRICS) {
    const v = cur.summary[k], pv = prev ? prev.summary[k] : undefined;
    let delta = '', flash = false;
    if (prev && JSON.stringify(v) !== JSON.stringify(pv)) {
      flash = true;
      let cls = 'neutral';
      if (typeof v === 'number' && typeof pv === 'number' && better) cls = (v > pv) === (better === 'hi') ? 'up' : 'down';
      if (typeof v === 'boolean' && better) cls = (v === (better === 'hi')) ? 'up' : 'down';
      delta = `<span class="delta ${cls}">was ${fmt(pv, unit)}</span>`;
    }
    rows.push(`<tr class="${flash ? 'flash' : ''}"><td>${label}</td><td>${fmt(v, unit)}${delta}</td></tr>`);
  }
  $('#metrics').innerHTML = rows.join('');
  const e = cur.electronics || {};
  $('#elec').innerHTML = e.mcu ? [
    ['MCU', e.mcu], ['Servo', e.servo], ['Sensor', e.sensor], ['Divider', e.sensing?.divider_ohm ? `${e.sensing.divider_ohm} Ω` : '–'],
    ['Hard force limit', e.limit_n ? `${e.limit_n} N` : '–'], ['Board', cur.board_url ? '<span class="badge ok">routed</span>' : '–'],
    ['Servo angles', cur.mech?.servo_open_deg != null ? `${cur.mech.servo_open_deg}° open → ${cur.mech.servo_closed_deg}° stop` : '–'],
  ].map(([a, b]) => `<div class="row"><span class="muted">${a}</span><span>${b}</span></div>`).join('') : '';
  $('#issues').innerHTML = (cur.summary.issues || []).map((i) => `<div class="bad">✗ ${i}</div>`).join('') +
    (e.warnings || []).map((w) => `<div class="warn">• ${w}</div>`).join('');
  // force window bar
  const [lo, hi] = e.window_n || [0, 0];
  const stall = e.servo_stall_force_n || 0, lim = e.object_limit_n, tgt = e.target_force_n || 0;
  const top = Math.max(stall, lim || 0, tgt * 3, 1) * 1.05;
  const X = (f) => 4 + 312 * Math.min(f, top) / top;
  $('#window').innerHTML = e.window_n ? `<svg viewBox="0 0 320 54" width="100%" font-size="9" fill="#8b98a5">
    <rect x="4" y="14" width="312" height="10" rx="3" fill="#26303a"/>
    <rect x="4" y="14" width="${X(lo) - 4}" height="10" fill="#ff5a5f" opacity=".5"/>
    ${hi ? `<rect x="${X(lo)}" y="14" width="${X(hi) - X(lo)}" height="10" fill="#3ddc97" opacity=".75"/>` : ''}
    ${lim ? `<rect x="${X(lim)}" y="14" width="${X(top) - X(lim)}" height="10" fill="#ff5a5f" opacity=".75"/>` : ''}
    <line x1="${X(tgt)}" x2="${X(tgt)}" y1="10" y2="28" stroke="#fff" stroke-width="1.5"/><text x="${X(tgt) + 3}" y="9" fill="#fff">target ${tgt} N</text>
    <line x1="${X(stall)}" x2="${X(stall)}" y1="10" y2="28" stroke="#ff7a45" stroke-width="1.5"/><text x="${Math.min(X(stall), 250)}" y="40" fill="#ff7a45">servo ${stall} N</text>
    ${lim ? `<text x="${Math.min(X(lim), 200)}" y="51" fill="#ff5a5f">damage ${lim} N</text>` : ''}
    <text x="4" y="40">slips &lt; ${lo} N</text></svg>` : '<span class="muted">–</span>';
}

function renderRevs(revs, current) {
  $('#revs').innerHTML = '';
  for (const r of revs) {
    const b = document.createElement('button');
    b.textContent = r;
    b.className = r === current ? 'on' : '';
    b.onclick = async () => {
      const cur = await (await fetch(`/api/rev?rev=${r}`)).json();
      const prev = cur.parent ? await (await fetch(`/api/rev?rev=${cur.parent}`)).json() : null;
      show(cur, prev, false);
      renderRevs(revs, r);
    };
    $('#revs').appendChild(b);
  }
}

async function show(cur, prev, tour) {
  state = { current: cur, previous: prev };
  renderPipeline(cur.provenance, cur.timings);
  renderInspector(cur, prev);
  renderFirmware(cur, prev);
  renderSim(cur);
  await Promise.all([renderCad(cur, prev), renderFab(cur), renderBoard(cur)]);
  if (tour && $('#tour').checked) runTour(cur.provenance || {});
  else if (document.querySelector('.view.on')?.dataset.view === 'board') playBoard();
}

// auto-tour: walk the views that this change regenerated
function stopTour() { if (tourTimer) { clearTimeout(tourTimer); tourTimer = null; } }
function runTour(prov) {
  stopTour();
  const steps = [];
  if (prov.cad === 'computed' || prov.mech === 'computed') steps.push(['cad', 3500]);
  if (prov.electronics === 'computed' && boardData) steps.push(['board', 0]);
  if (prov.electronics === 'computed' && state.current.firmware) steps.push(['fw', 3000]);
  if (prov.fab === 'computed') steps.push(['fab', 3500]);
  if (!steps.length) steps.push(['cad', 0]);
  let i = 0;
  const next = () => {
    if (i >= steps.length) return;
    const [tab, dur] = steps[i++];
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('flash', steps.some((s) => s[0] === b.dataset.tab)));
    showTab(tab);
    const d = tab === 'board' ? playBoard() + 1200 : dur;
    tourTimer = setTimeout(next, d);
  };
  next();
}

// ---------------------------------------------------------------- live events
let pendingMsg = null;
function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = async (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === 'user') addMsg('user', ev.text);
    else if (ev.type === 'assistant') addMsg('assistant', ev.text, ev.changes);
    else if (ev.type === 'start') {
      renderPipeline({}, {});
      pendingMsg = addMsg('system', `regenerating ${ev.rev} from ${ev.parent || 'scratch'}…`);
    } else if (ev.type === 'node') nodeEvent(ev);
    else if (ev.type === 'done') {
      if (pendingMsg) pendingMsg.textContent = `${ev.current.rev} ready in ${ev.seconds} s · ${Object.values(ev.current.provenance).filter((p) => p === 'computed').length} of ${NODES.length} stages regenerated`;
      const st = await (await fetch('/api/state')).json();
      renderRevs(st.revs, ev.current.rev);
      show(ev.current, ev.previous, true);
    } else if (ev.type === 'gif' && state.current && ev.rev === state.current.rev) {
      state.current.gif = ev.url;
      renderSim(state.current);
    }
  };
  es.onerror = () => { es.close(); setTimeout(connect, 1500); };
}

(async function init() {
  const st = await (await fetch('/api/state')).json();
  $('#project').textContent = st.project;
  for (const h of st.history) addMsg(h.type, h.text, h.changes);
  if (!st.history.length) addMsg('assistant', st.current ?
    `Loaded ${st.project} at ${st.current.rev}. Ask for a change and I'll regenerate the CAD, simulation, electronics, board, firmware and fab files that depend on it.` :
    'Describe what the gripper needs to pick up, e.g. “pick up a raw egg (44 mm, 60 g) without cracking it, MG996R servo, print in PETG”.');
  if (st.current) { renderRevs(st.revs, st.current.rev); show(st.current, st.previous, false); }
  else renderPipeline();
  connect();
})();

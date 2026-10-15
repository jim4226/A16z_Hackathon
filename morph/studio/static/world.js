// Morph Studio: World mode (beta). Put the robot in a room, walk through it, play the simulation there, and check
// the placement (exact checks, plus an optional AI review of six rendered views with a guided tour).
//
//   import { init } from './world.js';     init(studio);          // the Studio (feature contract, section 4)
//   import { WorldView } from './world.js';
//   const wv = new WorldView(robotViewer);  await wv.setWorld(world, { assetUrl });  wv.walk(true);
//   const views = await wv.snapshotViews(simDoc);  await wv.tour(review.tour, simDoc);
//
// Frames: the robot stays at the origin of the viewer's robot frame (mm, z up), where every overlay already lives.
// The room moves instead: worldFrame (a child of robotFrame) holds the inverse of the mount pose, its children are
// in the world frame of world.json (mm, z up), and an uploaded room (glTF: y up, metres) sits under one more group
// with the asset transform (units, up axis, two-point scale). Collisions in the simulation use the grey boxes only.

import * as THREE from './vendor/three.module.min.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';
import { PointerLockControls } from './vendor/PointerLockControls.js';
import { Octree } from './vendor/Octree.js';
import { Capsule } from './vendor/Capsule.js';
import { TransformControls } from './vendor/TransformControls.js';

// ------------------------------------------------------------------ constants
export const VIEWS = [['overview', 'Overview at eye height'], ['top_down', 'Top-down'], ['along_path', 'Along pick to place'],
  ['wrist', 'Wrist camera at the grasp'], ['walkway', 'From the nearest walkway'], ['base', 'Base close-up']];
const VIEW_TITLE = Object.fromEntries(VIEWS);
export const NOTES = {
  world: 'World mode (beta): the robot is simulated against the grey boxes, not the scanned surfaces. Anything without a box is invisible to the simulation, and scale is only as good as the length you entered. Check clearances in the real room before running the arm.',
  review: "AI review (beta) sends 6 pictures of this scene to Claude. Pass or fail comes from the simulation's measurements; the AI's comments are suggestions and can be wrong.",
  release: 'Not in this release: turning a video into a 3D world, photo-real splat rooms, or generative world-model previews. Import a room scan as .glb, or start from a built-in desk, counter or workbench.',
};
const EYE_MM = 1600;                 // eye height above the floor
const WALK_MM_S = 1400;              // walking speed
const CAPSULE_R = 220;
const GRAVITY = 9800;                // mm/s^2
const OCTREE_MAX_TRIS = 400000;      // bigger scans walk against the boxes and the floor only
const C = { box: '#9aa6b5', auto: '#f5b942', sel: '#7091ff', hit: '#ff6b6b', near: '#ff9152', surface: '#7091ff',
  walkway: '#2dd4d4', seat: '#a78bfa', reach: '#4aa3ff', cand: '#3dd68c' };
const DEG = Math.PI / 180;
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const r1 = (x) => Math.round(x * 10) / 10;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const uid = () => Math.random().toString(36).slice(2, 8);
const _v = new THREE.Vector3(), _v2 = new THREE.Vector3(), _m = new THREE.Matrix4();
const UNIT_BOX = new THREE.Box3(new THREE.Vector3(-0.5, -0.5, -0.5), new THREE.Vector3(0.5, 0.5, 0.5));

function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function yawMatrix(deg, pos) { return new THREE.Matrix4().makeRotationZ(deg * DEG).setPosition(pos[0], pos[1], pos[2]); }
function slug(s) { return String(s || '').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || `box-${uid()}`; }

let cssDone = false;
function injectCSS() {
  if (cssDone || typeof document === 'undefined') return;
  cssDone = true;
  const s = document.createElement('style');
  s.id = 'morph-world-css';
  s.textContent = `
.wv-panel{position:absolute;z-index:6;left:14px;top:96px;width:318px;max-height:calc(100% - 110px);overflow:auto;padding:10px 12px 12px;font:12px/1.4 var(--sans,Inter,system-ui,sans-serif);color:var(--ink,#e9ecf2);
  background:rgba(14,17,23,.86);border:1px solid rgba(255,255,255,.08);border-radius:10px;box-shadow:0 10px 30px -10px rgba(0,0,0,.6);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.wv-panel h3{margin:0;font-size:13px;font-weight:680;display:flex;align-items:center;gap:7px}
.wv-panel h3 .wv-beta{font:600 9.5px/1 var(--sans,system-ui);letter-spacing:.08em;text-transform:uppercase;padding:3px 6px;border-radius:5px;background:rgba(245,185,66,.14);color:var(--warn,#f5b942)}
.wv-panel h3 .wv-x{margin-left:auto}
.wv-sec{margin-top:10px;padding-top:9px;border-top:1px solid var(--line2,#262c38)}
.wv-sec>b{display:block;font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim,#5d6676);font-weight:650;margin-bottom:6px}
.wv-row{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin-top:5px}
.wv-btn{display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 9px;border-radius:7px;border:1px solid var(--line2,#262c38);background:var(--bg3,#181c25);color:var(--ink2,#b9c0cd);font:500 12px var(--sans,system-ui);cursor:pointer;white-space:nowrap}
.wv-btn:hover{background:var(--bg4,#20252f);color:var(--ink,#e9ecf2)}
.wv-btn.on{background:var(--accent-bg,rgba(112,145,255,.12));color:var(--accent2,#9db2ff);border-color:var(--accent-line,rgba(112,145,255,.45))}
.wv-btn.primary{background:var(--accent,#7091ff);color:#0a0f24;border-color:transparent;font-weight:600}
.wv-btn:disabled{opacity:.45;cursor:default}
.wv-note{margin-top:6px;font-size:11px;line-height:1.45;color:var(--muted,#838c9d)}
.wv-note.warn{color:#f3d79b}
.wv-in{height:26px;padding:0 7px;border-radius:7px;border:1px solid var(--line2,#262c38);background:var(--bg1,#0d0f14);color:var(--ink,#e9ecf2);font:12px var(--mono,monospace);width:84px}
.wv-sel{height:26px;border-radius:7px;border:1px solid var(--line2,#262c38);background:var(--bg1,#0d0f14);color:var(--ink,#e9ecf2);font:12px var(--sans,system-ui);padding:0 6px}
.wv-list{margin-top:6px;display:flex;flex-direction:column;gap:3px;max-height:150px;overflow:auto}
.wv-item{display:flex;align-items:center;gap:6px;padding:3px 6px;border-radius:6px;background:var(--bg2,#12151c);border:1px solid var(--line,#1c212b);font-size:11.5px}
.wv-item.sel{border-color:var(--accent-line,rgba(112,145,255,.45))}
.wv-item i{width:8px;height:8px;border-radius:2px;flex:none}
.wv-item span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
.wv-item small{color:var(--dim,#5d6676);font:10px var(--mono,monospace)}
.wv-x{background:none;border:0;color:var(--muted,#838c9d);cursor:pointer;font-size:14px;line-height:1;padding:2px 4px}
.wv-x:hover{color:var(--ink,#e9ecf2)}
.wv-verdict{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:99px;font-weight:650;font-size:11.5px}
.wv-verdict.ready{background:rgba(61,214,140,.12);color:#3dd68c}.wv-verdict.needs_changes{background:rgba(245,185,66,.12);color:#f5b942}.wv-verdict.blocked{background:rgba(255,107,107,.12);color:#ff6b6b}
.wv-check{display:flex;gap:7px;padding:4px 0;border-bottom:1px solid var(--line,#1c212b);font-size:11.5px}
.wv-check .st{flex:none;width:38px;font:600 9.5px/16px var(--mono,monospace);text-transform:uppercase;text-align:center;border-radius:4px}
.wv-check .st.pass{background:rgba(61,214,140,.12);color:#3dd68c}.wv-check .st.warn{background:rgba(245,185,66,.12);color:#f5b942}
.wv-check .st.fail{background:rgba(255,107,107,.12);color:#ff6b6b}.wv-check .st.skip{background:var(--bg4,#20252f);color:var(--muted,#838c9d)}
.wv-find{padding:6px 7px;margin-top:5px;border-radius:7px;background:var(--bg2,#12151c);border:1px solid var(--line,#1c212b);font-size:11.5px}
.wv-find .src{font:600 9.5px var(--mono,monospace);color:var(--violet,#a78bfa);text-transform:uppercase;letter-spacing:.05em}
.wv-find.critical{border-color:rgba(255,107,107,.4)}.wv-find.warning{border-color:rgba(245,185,66,.35)}
.wv-hint{position:absolute;z-index:6;left:50%;top:14px;transform:translateX(-50%);padding:6px 12px;border-radius:99px;background:rgba(13,15,20,.86);border:1px solid rgba(255,255,255,.08);
  color:var(--ink2,#b9c0cd);font:500 12px var(--sans,system-ui);pointer-events:none;white-space:nowrap;opacity:0;transition:opacity .25s}
.wv-hint.on{opacity:1}
.wv-cap{position:absolute;z-index:7;left:50%;bottom:18px;transform:translateX(-50%);width:min(760px,calc(100% - 40px));padding:12px 16px 12px;border-radius:12px;
  background:rgba(14,17,23,.9);border:1px solid rgba(255,255,255,.08);box-shadow:0 10px 30px -10px rgba(0,0,0,.6);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  color:var(--ink,#e9ecf2);font:14px/1.5 var(--sans,system-ui);display:none}
.wv-cap.on{display:block;animation:wvUp .3s}
.wv-cap .top{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--muted,#838c9d)}
.wv-cap .top b{font:650 12px var(--mono,monospace);color:var(--accent2,#9db2ff);background:var(--accent-bg,rgba(112,145,255,.12));padding:2px 7px;border-radius:6px}
.wv-cap .top .t{color:var(--ink,#e9ecf2);font-weight:650;font-size:13.5px}
.wv-cap .say{margin-top:6px;color:var(--ink2,#b9c0cd)}
.wv-cap .dots{display:flex;gap:4px;margin-top:9px}.wv-cap .dots i{flex:1;height:3px;border-radius:3px;background:rgba(157,178,255,.16)}
.wv-cap .dots i.done{background:rgba(157,178,255,.55)}.wv-cap .dots i.cur{background:var(--accent,#7091ff)}
.wv-cap .src{font:600 9.5px var(--mono,monospace);color:var(--violet,#a78bfa);text-transform:uppercase;letter-spacing:.05em}
@keyframes wvUp{from{opacity:0;transform:translate(-50%,6px)}}
.wv-walk{position:absolute;z-index:6;right:16px;bottom:16px;max-width:min(560px,calc(100% - 380px));padding:7px 12px;border-radius:10px;background:rgba(13,17,22,.86);
  border:1px solid rgba(255,255,255,.08);color:var(--ink2,#b9c0cd);font:500 12px var(--sans,system-ui);display:none}
.wv-walk.on{display:block}
.wv-walk kbd{font:11px var(--mono,monospace);padding:1px 5px;border-radius:4px;border:1px solid var(--line3,#343b49);color:var(--ink,#e9ecf2)}
.wv-panel.min{width:auto;padding-bottom:10px}.wv-panel.min>:not(h3){display:none}
.wv-panel.away{display:none!important}   /* the 3D view shows another tab (the Simulation tab's replay): the panel waits */
.wv-panel h3 .wv-min{margin-left:auto}.wv-panel h3 .wv-min+.wv-x{margin-left:0}
.wv-status{margin-top:6px;padding:5px 8px;border-radius:7px;background:var(--bg2,#12151c);border:1px solid var(--line,#1c212b);font-size:11.5px;color:var(--ink2,#b9c0cd)}
.wv-status.ok{border-color:rgba(61,214,140,.3)}.wv-status.bad{border-color:rgba(255,107,107,.35)}.wv-status.run{border-color:rgba(112,145,255,.35)}
.wv-edit{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px 6px;margin-top:6px;padding:6px;border-radius:7px;background:var(--bg2,#12151c);border:1px solid var(--accent-line,rgba(112,145,255,.45))}
.wv-edit label{display:flex;flex-direction:column;gap:2px;font-size:10px;color:var(--dim,#5d6676);text-transform:uppercase;letter-spacing:.06em}
.wv-edit .wv-in{width:100%;min-width:0}
.wv-edit .wide{grid-column:1/-1;text-transform:none;letter-spacing:0;font-size:11px;color:var(--muted,#838c9d)}
.wv-item.prop i{background:${C.auto}}.wv-item.prop.hl{border-color:rgba(245,185,66,.6)}
/* while walking or on a guided tour, the robot tab's kinematic-chain card (bottom right) would cover the hint and the
   tour's Back / Next buttons */
body.wv-overlay .ov.kchain{opacity:0;pointer-events:none}
@media (max-width:1100px){body.wv-open .ov.kchain{display:none}}   /* a narrow 3D view: the World panel needs the room */
`;
  document.head.appendChild(s);
}

function disposeTree(o) {
  o.traverse((x) => {
    if (x.geometry) x.geometry.dispose();
    if (x.material) (Array.isArray(x.material) ? x.material : [x.material]).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
  });
  if (o.parent) o.parent.remove(o);
}

function noRay(o) { o.traverse((x) => { x.raycast = () => {}; }); return o; }

// ================================================================== WorldView: the adapter around a RobotViewer
export class WorldView {
  constructor(viewer, opts = {}) {
    injectCSS();
    this.v = viewer;
    this.opts = opts;
    this.world = null;
    this.mode = 'orbit';
    this.proposals = [];                        // auto obstacle boxes not confirmed yet
    this.selected = null;
    this.listeners = new Set();
    this.contacts = [];
    this.frame = new THREE.Group();
    this.frame.name = 'worldFrame';
    this.frame.matrixAutoUpdate = false;
    this.room = new THREE.Group(); this.room.name = 'room';           // furniture boxes or the room scan
    this.boxes = new THREE.Group(); this.boxes.name = 'obstacles';
    this.zoneG = new THREE.Group(); this.zoneG.name = 'zones';
    this.marks = new THREE.Group(); this.marks.name = 'marks';
    this.frame.add(this.room, this.boxes, this.zoneG, this.marks);
    this.base = new THREE.Group(); this.base.name = 'worldBaseOverlays';   // robot-frame overlays: ring, reach dome
    this.frame.visible = this.base.visible = false;
    viewer.robotFrame.add(this.frame, this.base);
    this.hintEl = el('div', 'wv-hint');
    this.walkEl = el('div', 'wv-walk', 'Walk: <kbd>W</kbd><kbd>A</kbd><kbd>S</kbd><kbd>D</kbd> or arrows, <kbd>Shift</kbd> runs, drag or click to look; <kbd>Esc</kbd> frees the mouse, again to stop walking. Touch: tap the floor to go there');
    this.capEl = el('div', 'wv-cap');
    viewer.container.append(this.hintEl, this.walkEl, this.capEl);
    this._onDown = this._onDown.bind(this);
    this._onUp = this._onUp.bind(this);
    this._onMove = this._onMove.bind(this);
    const c = viewer.renderer.domElement;
    c.addEventListener('pointerdown', this._onDown, true);
    c.addEventListener('pointerup', this._onUp, true);
    c.addEventListener('pointermove', this._onMove, true);
    this._offLoaded = viewer.on('loaded', () => {
      if (this.world) {
        // the viewer just sized its camera for this model: that is what World off gives back, not the older model's
        if (this._saved) Object.assign(this._saved, { maxDist: viewer.controls.maxDistance, near: viewer.camera.near, far: viewer.camera.far });
        this._stage(true);
      }
      if (this._robotHidden) this.robotVisible(false);
    });
    this.stats = { fps: 0 };
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  _emit(type, data) { for (const f of this.listeners) { try { f(type, data); } catch (e) { console.error(e); } } }
  get active() { return !!this.world; }
  _dirty() { this.v._dirty = true; }
  hint(text) { this.hintEl.textContent = text || ''; this.hintEl.classList.toggle('on', !!text); }

  // ---------------------------------------------------------------- world in / out
  async setWorld(world, { assetUrl = null, frame = true } = {}) {
    const first = !this.world;
    this.world = JSON.parse(JSON.stringify(world));
    if (this.assetG && (!this.world.asset || assetUrl !== this.assetUrl)) { disposeTree(this.assetG); this.assetG = null; }
    this.assetUrl = assetUrl;
    this._stage(true);
    this._build();
    if (this.world.asset && assetUrl && !this.assetG) await this._loadAsset(assetUrl);
    else this._assetMatrix();
    this._applyMount(false);
    if (first && frame) this.frameOverview(0);
    this._emit('world', this.world);
    return this.world;
  }

  /** Take the room away and give the plain table back: the viewer's floor, auto-orbit and camera limits as they were,
   * the robot shown; `reframe` flies the camera back to the robot the way the viewer frames it. */
  clear({ reframe = false } = {}) {
    const had = !!this.world;
    this.walk(false);
    this.setMode('orbit');
    this.stopTour();
    for (const g of [this.room, this.boxes, this.zoneG, this.marks, this.base]) [...g.children].forEach(disposeTree);
    this.assetG = null;
    this.assetUrl = null;
    this.world = null;
    this.octree = null;
    this.proposals = [];
    this.contacts = [];
    this.selected = null;
    this.scaleRef = null;
    this.doc = null;
    this.reach = null;
    this.frame.visible = this.base.visible = false;
    this.robotVisible(true);
    this._stage(false);
    if (had && reframe) this.v.frame?.(null, { ms: 700, dir: this.v._defaultDir?.() });
    this._emit('world', null);
    this._dirty();
  }

  dispose() {
    this.clear();
    const c = this.v.renderer.domElement;
    c.removeEventListener('pointerdown', this._onDown, true);
    c.removeEventListener('pointerup', this._onUp, true);
    c.removeEventListener('pointermove', this._onMove, true);
    this._offLoaded?.();
    if (this.tc) { this.tc.detach(); this.tc.dispose(); this.tc.parent?.remove(this.tc); this.tc = null; }
    this.frame.parent?.remove(this.frame);
    this.base.parent?.remove(this.base);
    this.hintEl.remove(); this.walkEl.remove(); this.capEl.remove();
  }

  // the viewer's stage floor, grid and contact shadow belong to the plain table: hidden in a room
  _stage(on) {
    const v = this.v;
    for (const k of ['_floor', '_shadowPlane', '_blob']) if (v[k]) v[k].visible = !on;
    if (on) {
      if (this._saved == null) {
        this._saved = { orbit: v._orbitOn, maxPolar: v.controls.maxPolarAngle, maxDist: v.controls.maxDistance,
          near: v.camera.near, far: v.camera.far };
      }
      v.setAutoOrbit?.(false);
      v.controls.maxPolarAngle = Math.PI * 0.94;
      v.controls.maxDistance = 40000;
      v.camera.near = Math.min(v.camera.near, 5);
      v.camera.far = Math.max(v.camera.far, 120000);
      v.camera.updateProjectionMatrix();
    } else if (this._saved) {
      v.controls.maxPolarAngle = this._saved.maxPolar;
      v.controls.maxDistance = this._saved.maxDist;
      v.camera.near = this._saved.near;
      v.camera.far = this._saved.far;
      v.camera.updateProjectionMatrix();
      v.setAutoOrbit?.(this._saved.orbit);
      this._saved = null;
    }
    this._dirty();
  }

  // the robot itself, hidden while a just-uploaded room waits for it to be placed (it would stand at the scan's origin)
  robotVisible(on) {
    const root = this.v.model?.root;
    if (root && root.visible !== !!on) { root.visible = !!on; this._dirty(); }
    this._robotHidden = !on;
  }

  // the whole room scan in view, from above the front, so a surface can be clicked
  frameAsset(ms = 0) {
    if (!this.assetG) return Promise.resolve(false);
    this.frame.updateMatrixWorld(true);
    this.assetG.updateMatrixWorld(true);
    let box = null;
    this.assetG.traverse((o) => {
      if (!o.isMesh) return;
      if (!o.geometry.boundingBox) o.geometry.computeBoundingBox();
      const b = o.geometry.boundingBox.clone().applyMatrix4(o.matrixWorld);
      box = box ? box.union(b) : b;
    });
    if (!box || box.isEmpty()) return Promise.resolve(false);
    const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2;
    const v = this.v;
    const dir = new THREE.Vector3(Math.sin(20 * DEG) * Math.cos(38 * DEG), Math.sin(38 * DEG), Math.cos(20 * DEG) * Math.cos(38 * DEG));
    const fit = v._fitDistance ? v._fitDistance(r * 0.75) : r * 2.2;
    return v._flyTo(c, fit, dir, ms);
  }

  // ---------------------------------------------------------------- frames
  mountMatrix(w = this.world) { return yawMatrix(w.mount.yaw_deg, w.mount.position); }         // base -> world
  worldToBase(p) { return _v.fromArray(p).applyMatrix4(_m.copy(this.mountMatrix()).invert()).toArray(); }
  baseToWorld(p) { return _v.fromArray(p).applyMatrix4(this.mountMatrix()).toArray(); }
  baseToScene(p, out = new THREE.Vector3()) { this.v.robotFrame.updateMatrixWorld(true); return out.fromArray(p).applyMatrix4(this.v.robotFrame.matrixWorld); }
  sceneToBase(p) { this.v.robotFrame.updateMatrixWorld(true); return p.clone().applyMatrix4(_m.copy(this.v.robotFrame.matrixWorld).invert()).toArray(); }
  sceneToWorld(p) { this.frame.updateMatrixWorld(true); return p.clone().applyMatrix4(_m.copy(this.frame.matrixWorld).invert()).toArray(); }
  get floorZ() { return (this.world.floor_z ?? 0) - this.world.mount.position[2]; }        // base frame
  get surfaceTop() { const s = this.world.surface; return s ? s.center[2] + s.half[2] - this.world.mount.position[2] : 0; }

  _applyMount(keepCamera) {
    const prev = this.frame.matrixWorld.clone();
    this.frame.matrix.copy(this.mountMatrix()).invert();
    this.frame.matrixWorldNeedsUpdate = true;
    this.frame.updateMatrixWorld(true);
    if (keepCamera) {                                  // the room stays put on screen; the robot moves in it
      const k = this.frame.matrixWorld.clone().multiply(prev.invert());
      const v = this.v;
      v.camera.position.applyMatrix4(k);
      v.controls.target.applyMatrix4(k);
      v.camera.quaternion.premultiply(new THREE.Quaternion().setFromRotationMatrix(k));
    }
    this.frame.visible = this.base.visible = true;
    this._buildBaseOverlays();
    this._dirty();
  }

  // ---------------------------------------------------------------- content
  _build() {
    for (const g of [this.room, this.boxes, this.zoneG, this.marks]) [...g.children].filter((c) => c !== this.assetG).forEach(disposeTree);
    this.octree = null;
    const w = this.world;
    for (const b of w.scene || []) this.room.add(this._box(b, { color: b.color || '#9aa3ad', solid: true }));
    if (!w.asset && !(w.scene || []).length && w.surface) this.room.add(this._box({ ...w.surface, label: w.surface.label }, { color: '#8a7a68', solid: true }));
    this._buildBoxes();
    this._buildZones();
    this._buildSurface();
    this._buildCandidates();
  }

  _box(b, { color, solid = false, opacity = 0.18, edge = 0.65, dashed = false } = {}) {
    const g = new THREE.Group();
    g.position.fromArray(b.center);
    g.rotation.z = (b.yaw_deg || 0) * DEG;
    const size = b.half.map((h) => Math.max(2 * h, 0.5));
    const geo = new THREE.BoxGeometry(1, 1, 1);
    let mesh;
    if (solid) {
      mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.82, metalness: 0.04 }));
      mesh.receiveShadow = true;
      mesh.castShadow = size[0] * size[1] * size[2] < 4e8;
    } else {
      mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthWrite: false, toneMapped: false }));
      mesh.renderOrder = 4;
    }
    mesh.scale.fromArray(size);
    const lm = dashed ? new THREE.LineDashedMaterial({ color, dashSize: 14, gapSize: 9, transparent: true, opacity: edge, toneMapped: false })
      : new THREE.LineBasicMaterial({ color: solid ? new THREE.Color(color).multiplyScalar(0.62) : color, transparent: true, opacity: solid ? 0.5 : edge, toneMapped: false });
    const lines = new THREE.LineSegments(new THREE.EdgesGeometry(geo), lm);
    lines.scale.copy(mesh.scale);
    if (dashed) lines.computeLineDistances();
    lines.raycast = () => {};
    g.add(mesh, lines);
    g.userData = { box: b, mesh, lines };
    return g;
  }

  _buildBoxes() {
    [...this.boxes.children].forEach(disposeTree);
    const hit = new Set(this.contacts.map((c) => c.obstacle));
    for (const o of this.world.obstacles || []) {
      const color = hit.has(o.label) ? C.hit : (this.selected === o.id ? C.sel : C.box);
      const g = this._box(o, { color, opacity: hit.has(o.label) ? 0.32 : 0.16 });
      g.userData.id = o.id;
      g.name = `obstacle:${o.id}`;
      this.boxes.add(g);
    }
    for (const o of this.proposals) {
      const g = this._box(o, { color: C.auto, opacity: this.hlProposal === o.id ? 0.34 : 0.14, dashed: this.hlProposal !== o.id });
      g.userData.id = o.id;
      g.userData.proposal = true;
      this.boxes.add(g);
    }
    // the gizmo follows the selected box into the rebuilt group (a removed group would leave it on nothing)
    if (this.tc) {
      const g = this.mode === 'edit' && this.selected && this.boxes.children.find((c) => c.userData.id === this.selected && !c.userData.proposal);
      if (g) this.tc.attach(g); else this.tc.detach();
    }
    this._dirty();
  }

  _zonePoly(z) {
    if (z.box) {
      const b = z.box, c = Math.cos(b.yaw_deg * DEG), s = Math.sin(b.yaw_deg * DEG);
      return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => {
        const x = sx * b.half[0], y = sy * b.half[1];
        return [b.center[0] + c * x - s * y, b.center[1] + s * x + c * y];
      });
    }
    return z.polygon;
  }

  _buildZones() {
    [...this.zoneG.children].forEach(disposeTree);
    const fz = (this.world.floor_z ?? 0) + 3;
    for (const z of this.world.zones || []) {
      const pts = this._zonePoly(z);
      const shape = new THREE.Shape(pts.map(([x, y]) => new THREE.Vector2(x, y)));
      const color = C[z.kind] || C.walkway;
      const fill = new THREE.Mesh(new THREE.ShapeGeometry(shape), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.16, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }));
      fill.position.z = fz;
      const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts.map(([x, y]) => new THREE.Vector3(x, y, fz + 1))),
        new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.8, toneMapped: false }));
      const g = noRay(new THREE.Group());
      g.add(fill, line);
      g.userData = { zone: z };
      fill.raycast = () => {};
      this.zoneG.add(g);
    }
  }

  _buildSurface() {
    const s = this.world.surface;
    if (!s) return;
    const top = s.center[2] + s.half[2] + 1.5;
    const c = Math.cos(s.yaw_deg * DEG), sn = Math.sin(s.yaw_deg * DEG);
    const pts = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => {
      const x = sx * s.half[0], y = sy * s.half[1];
      return new THREE.Vector3(s.center[0] + c * x - sn * y, s.center[1] + sn * x + c * y, top);
    });
    const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineDashedMaterial({ color: C.surface, dashSize: 20, gapSize: 12, transparent: true, opacity: 0.85, toneMapped: false }));
    line.computeLineDistances();
    line.name = 'surface';
    noRay(line);
    this.marks.add(line);
  }

  _buildCandidates() {
    for (const c of this.world.candidates || []) {
      const g = new THREE.Group();
      g.position.fromArray(c.position);
      const ring = new THREE.Mesh(new THREE.RingGeometry(60, 72, 48), new THREE.MeshBasicMaterial({ color: C.cand, transparent: true, opacity: 0.85, side: THREE.DoubleSide, toneMapped: false }));
      ring.position.z = 2;
      g.add(ring);
      g.userData = { candidate: c.label };
      this.marks.add(noRay(g));
    }
  }

  _buildBaseOverlays() {
    [...this.base.children].forEach(disposeTree);
    // yaw ring with a handle on the robot's forward axis (+x), shown while placing
    const r = Math.max(110, (this.v.model?.footprint || 120) * 1.15);
    const ring = new THREE.Mesh(new THREE.RingGeometry(r, r + 10, 72), new THREE.MeshBasicMaterial({ color: C.sel, transparent: true, opacity: 0.7, side: THREE.DoubleSide, depthWrite: false, toneMapped: false }));
    const knob = new THREE.Mesh(new THREE.SphereGeometry(16, 20, 12), new THREE.MeshBasicMaterial({ color: '#ffffff', toneMapped: false }));
    knob.position.set(r + 5, 0, 0);
    const arrow = new THREE.Mesh(new THREE.ConeGeometry(14, 40, 16), new THREE.MeshBasicMaterial({ color: C.sel, toneMapped: false }));
    arrow.rotation.z = -Math.PI / 2;
    arrow.position.set(r + 48, 0, 0);
    this.ring = new THREE.Group();
    this.ring.add(ring, knob, arrow);
    this.ring.position.z = this.surfaceTop + 2;
    this.ring.visible = this.mode === 'place';
    this.knob = knob;
    ring.raycast = arrow.raycast = () => {};
    this.base.add(this.ring);
    this.reach = null;
  }

  showReach(radius) {
    if (this.reach) { disposeTree(this.reach); this.reach = null; }
    if (!radius) return;
    const geo = new THREE.SphereGeometry(radius, 48, 18, 0, Math.PI * 2, 0, Math.PI / 2).rotateX(Math.PI / 2);
    const g = new THREE.Group();
    g.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: C.reach, transparent: true, opacity: 0.06, depthWrite: false, side: THREE.DoubleSide, toneMapped: false })),
      new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.SphereGeometry(radius, 24, 8, 0, Math.PI * 2, 0, Math.PI / 2).rotateX(Math.PI / 2)),
        new THREE.LineBasicMaterial({ color: C.reach, transparent: true, opacity: 0.16, toneMapped: false })));
    g.position.z = this.surfaceTop;
    this.reach = noRay(g);
    this.base.add(this.reach);
    this._dirty();
  }

  async _loadAsset(url) {
    if (this.assetG) { disposeTree(this.assetG); this.assetG = null; }
    this.hint('Loading the room…');
    try {
      const gltf = await new GLTFLoader().loadAsync(url);
      const g = new THREE.Group();
      g.name = 'roomAsset';
      g.matrixAutoUpdate = false;
      this.assetG = g;
      this._assetMatrix();
      let tris = 0;
      gltf.scene.traverse((o) => {
        if (!o.isMesh) return;
        o.receiveShadow = true;
        o.castShadow = false;
        const geo = o.geometry;
        tris += (geo.index ? geo.index.count : geo.attributes.position.count) / 3;
        for (const m of Array.isArray(o.material) ? o.material : [o.material]) {
          if (!m) continue;
          m.side = THREE.DoubleSide;
          // a scan without colours comes in pure white, which glares under the studio lights
          if (!m.map && !geo.attributes.color && m.color && m.color.r > 0.95 && m.color.g > 0.95 && m.color.b > 0.95) { m.color.set('#c3c8cf'); m.roughness = 0.9; }
        }
      });
      this.assetTris = tris;
      g.add(gltf.scene);
      this.room.add(g);
      this.octree = null;
    } finally {
      this.hint('');
    }
    this._dirty();
  }

  _assetMatrix() {
    if (!this.assetG) return;
    const w = this.world, k = ({ m: 1000, cm: 10, mm: 1, in: 25.4, ft: 304.8 }[w.units] || 1000) * (w.scale || 1);
    const R = w.up === 'z' ? new THREE.Matrix4() : new THREE.Matrix4().makeRotationX(Math.PI / 2);   // glTF y up -> z up
    this.assetG.matrix.copy(R).premultiply(new THREE.Matrix4().makeScale(k, k, k));
    this.assetG.matrixWorldNeedsUpdate = true;
  }

  // ---------------------------------------------------------------- editing the world (all in the world frame)
  setMount(position, yaw = this.world.mount.yaw_deg, { keepCamera = true } = {}) {
    this.world.mount = { position: position.map(r1), yaw_deg: r1(((yaw + 180) % 360 + 360) % 360 - 180) };
    this._applyMount(keepCamera);
    this.changed('mount');
  }
  rotate(deg) { this.setMount(this.world.mount.position, this.world.mount.yaw_deg + deg); }
  changed(what) { this.world._dirty = true; this._emit('change', what); }

  applyScale(trueMM) {
    const ref = this.scaleRef;
    if (!ref || !(trueMM > 0)) return false;
    const k = trueMM / ref.measured;
    if (!(k > 0.001 && k < 1000)) return false;
    const w = this.world;
    const sc = (p) => p.map((x) => r1(x * k));
    w.scale = +(w.scale * k).toPrecision(6);
    w.mount.position = sc(w.mount.position);
    if (w.surface) { w.surface.center = sc(w.surface.center); w.surface.half = sc(w.surface.half); }
    for (const o of w.obstacles || []) {
      o.center = sc(o.center); o.half = sc(o.half);
      delete o.covers;                      // fitted to the path at the old scale: the next check measures afresh
    }
    for (const z of w.zones || []) { if (z.box) { z.box.center = sc(z.box.center); z.box.half = sc(z.box.half); } else { z.polygon = z.polygon.map(sc); z.z = z.z.map((x) => x * k); } }
    for (const c of w.candidates || []) c.position = sc(c.position);
    w.floor_z = r1((w.floor_z || 0) * k);
    w.scale_ref = { a: sc(ref.a), b: sc(ref.b), length_mm: r1(trueMM) };
    this.scaleRef = null;
    this._assetMatrix();
    this._build();
    this._applyMount(false);
    this.changed('scale');
    return true;
  }

  /** A new 10 x 10 x 12 cm box on the surface beside the work: outside the middle of pick and place (`targets`, base
   * frame, from the simulation), clear of both spots, so it shows up next to the job; move or size it from there. */
  addBox(label = 'Box', targets = null) {
    const w = this.world;
    const pick = targets?.pick || [220, -80], place = targets?.place || [160, 170];
    const mx = (pick[0] + place[0]) / 2, my = (pick[1] + place[1]) / 2, ml = Math.hypot(mx, my) || 1;
    const p = this.baseToWorld([mx + (mx / ml) * 150, my + (my / ml) * 150, this.surfaceTop + 60]);
    const o = { id: slug(`${label}-${uid()}`), label, center: p.map(r1), half: [50, 50, 60], yaw_deg: w.mount.yaw_deg, source: 'user' };
    w.obstacles = [...(w.obstacles || []), o];
    this.select(o.id);
    this.changed('boxes');
    return o;
  }
  deleteBox(id) {
    this.world.obstacles = (this.world.obstacles || []).filter((o) => o.id !== id);
    if (this.selected === id) this.selected = null;
    this._buildBoxes();
    this._emit('select', this.selected);
    this.changed('boxes');
  }

  /** A box's place and size as the panel shows them: from the robot base (mm, x forward, y left), its bottom above the
   * mount surface, its size along its own axes and its turn relative to the robot. */
  boxInBase(o) {
    const c = this.worldToBase(o.center);
    return { x: r1(c[0]), y: r1(c[1]), bottom: r1(c[2] - o.half[2] - this.surfaceTop), w: r1(2 * o.half[0]), d: r1(2 * o.half[1]),
      h: r1(2 * o.half[2]), turn: r1((((o.yaw_deg || 0) - this.world.mount.yaw_deg + 180) % 360 + 360) % 360 - 180) };
  }
  setBoxFromBase(id, vals) {
    const o = (this.world.obstacles || []).find((x) => x.id === id);
    if (!o) return false;
    const b = { ...this.boxInBase(o), ...vals };
    delete o.covers;                        // resized or moved by hand: no longer the box fitted for that place
    const size = [b.w, b.d, b.h].map((x) => clamp(Number.isFinite(x) ? x : 100, 10, 20000));
    o.half = size.map((x) => r1(x / 2));
    o.center = this.baseToWorld([b.x, b.y, this.surfaceTop + clamp(b.bottom || 0, -5000, 5000) + size[2] / 2]).map(r1);
    o.yaw_deg = r1((((this.world.mount.yaw_deg + (b.turn || 0)) + 180) % 360 + 360) % 360 - 180);
    this._buildBoxes();
    this.changed('boxes');
    return true;
  }

  /** The first grey box the robot's base would stand in (its footprint circle, up to 15 cm above the surface): a
   * placement the simulation would start in contact. Label or null. */
  baseClash() {
    if (!this.world?.surface) return null;
    const r = (this.v.model?.footprint || 120) * 0.95, top = this.surfaceTop;
    for (const o of this.world.obstacles || []) {
      const c = this.worldToBase(o.center), a = ((o.yaw_deg || 0) - this.world.mount.yaw_deg) * DEG;
      const lx = Math.cos(a) * -c[0] + Math.sin(a) * -c[1], ly = -Math.sin(a) * -c[0] + Math.cos(a) * -c[1];
      const dx = Math.max(Math.abs(lx) - o.half[0], 0), dy = Math.max(Math.abs(ly) - o.half[1], 0);
      if (Math.hypot(dx, dy) < r && c[2] - o.half[2] < top + 150 && c[2] + o.half[2] > top + 1) return o.label || 'a box';
    }
    return null;
  }
  setProposals(list) { this.proposals = (list || []).map((o) => ({ ...o, id: o.id || `auto-${uid()}` })); this.hlProposal = null; this._buildBoxes(); }
  keepProposals(ids = null) {
    const keep = this.proposals.filter((o) => !ids || ids.includes(o.id));
    const have = new Set((this.world.obstacles || []).map((o) => o.id));
    for (const o of keep) { let id = o.id; while (have.has(id)) id = `${o.id}-${uid()}`; have.add(id); this.world.obstacles.push({ ...o, id, source: 'auto' }); }
    this.proposals = this.proposals.filter((o) => !keep.includes(o));
    this._buildBoxes();
    this.changed('boxes');
  }
  dropProposals(ids = null) {
    this.proposals = ids ? this.proposals.filter((o) => !ids.includes(o.id)) : [];
    this._buildBoxes();
    this._emit('change', 'proposals');
  }
  highlightProposal(id) { this.hlProposal = this.hlProposal === id ? null : id; this._buildBoxes(); }
  addZone(kind, a, b) {
    const fz = this.world.floor_z ?? 0;
    const [x0, x1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])], [y0, y1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
    if (x1 - x0 < 100 || y1 - y0 < 100) return null;
    const z = { id: slug(`${kind}-${uid()}`), label: kind === 'seat' ? 'Seat' : 'Walkway', kind,
      polygon: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map(r1)), z: [fz, fz + 2000] };
    this.world.zones = [...(this.world.zones || []), z];
    this._buildZones();
    this.changed('zones');
    return z;
  }
  deleteZone(id) { this.world.zones = (this.world.zones || []).filter((z) => z.id !== id); this._buildZones(); this.changed('zones'); }

  select(id) {
    this.selected = id;
    if (id && this.mode === 'edit') this._ensureTC();
    this._buildBoxes();                     // recolours the selection and moves the gizmo onto it
    this._emit('select', id);
  }

  _ensureTC() {
    if (this.tc) return;
    const v = this.v;
    const tc = this.tc = new TransformControls(v.camera, v.renderer.domElement);
    tc.setSpace('local');
    tc.setSize(0.8);
    tc.addEventListener('dragging-changed', (e) => { v.controls.enabled = !e.value; });
    tc.addEventListener('change', () => this._dirty());
    tc.addEventListener('objectChange', () => {
      const g = tc.object;
      const o = g && (this.world.obstacles || []).find((x) => x.id === g.userData.id);
      if (!o) return;
      delete o.covers;                      // edited by hand: no longer the box fitted for that place
      if (tc.mode === 'scale') {           // the gizmo scales the group: fold it into the box size
        const m = g.userData.mesh, k = g.scale;
        o.half = [m.scale.x * k.x / 2, m.scale.y * k.y / 2, m.scale.z * k.z / 2].map((h) => r1(Math.max(h, 5)));
        m.scale.set(o.half[0] * 2, o.half[1] * 2, o.half[2] * 2);
        g.userData.lines.scale.copy(m.scale);
        g.scale.set(1, 1, 1);
      }
      g.rotation.x = g.rotation.y = 0;
      o.center = g.position.toArray().map(r1);
      o.yaw_deg = r1(((g.rotation.z / DEG + 180) % 360 + 360) % 360 - 180);
      this.changed('boxes');
    });
    tc.enabled = this.mode === 'edit';
    v.scene.add(tc);
  }
  setGizmo(mode) {                         // translate | rotate | scale
    this._ensureTC();
    this.tc.setMode(mode);
    this.tc.showX = this.tc.showY = mode !== 'rotate';
    this.tc.showZ = true;
  }

  // ---------------------------------------------------------------- modes and picking
  setMode(mode) {
    if (this.mode === 'walk' && mode !== 'walk') this.walk(false);
    this.mode = mode;
    this._pending = null;
    if (this.ring) this.ring.visible = mode === 'place';
    if (this.tc) {                          // a gizmo left listening would grab the pointer in the other modes
      this.tc.enabled = mode === 'edit';
      if (mode !== 'edit') this.tc.detach();
    }
    if (mode === 'edit' && this.selected) this.select(this.selected);
    const hints = { place: 'Click a level surface to put the robot there; drag the white knob to turn it',
      scale: 'Click two points a known distance apart', zone: 'Click two opposite corners on the floor',
      edit: 'Click a grey box to move or resize it' };
    this.hint(hints[mode] || '');
    this._dirty();
    this._emit('mode', mode);
  }

  _ndc(e) {
    const r = this.v.renderer.domElement.getBoundingClientRect();
    return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  }
  _ray(e) { const rc = new THREE.Raycaster(); rc.setFromCamera(this._ndc(e), this.v.camera); return rc; }   // as last drawn
  hitRoom(e, { level = false } = {}) {
    if (!this.world) return null;
    const rc = this._ray(e);
    const hits = rc.intersectObjects([this.room], true);
    this.frame.updateMatrixWorld(true);
    const inv = _m.copy(this.frame.matrixWorld).invert();
    for (const h of hits) {
      const n = h.face ? h.face.normal.clone().transformDirection(h.object.matrixWorld).transformDirection(inv) : new THREE.Vector3(0, 0, 1);
      if (level && Math.abs(n.z) < 0.9) continue;
      if (level && n.z < 0) continue;
      return { point: h.point.clone().applyMatrix4(inv).toArray(), normal: n.toArray(), object: h.object, scene: h.point.clone() };
    }
    return null;
  }

  _onDown(e) {
    this._down = { x: e.clientX, y: e.clientY, t: performance.now() };
    if (this.mode === 'place' && this.knob) {
      const hit = this._ray(e).intersectObject(this.knob, false)[0];
      if (hit) {
        this._dragYaw = { yaw0: this.world.mount.yaw_deg };
        this.v.controls.enabled = false;
        e.stopPropagation();
        this.v.renderer.domElement.setPointerCapture?.(e.pointerId);
      }
    }
    if (this.mode === 'walk') this._walkDown(e);
  }

  _onMove(e) {
    if (this._dragYaw) {
      const rc = this._ray(e);
      const plane = new THREE.Plane();
      const n = this.baseToScene([0, 0, 1]).sub(this.baseToScene([0, 0, 0])).normalize();
      plane.setFromNormalAndCoplanarPoint(n, this.baseToScene([0, 0, this.surfaceTop]));
      const p = rc.ray.intersectPlane(plane, new THREE.Vector3());
      if (p) {
        const b = this.sceneToBase(p);
        const a = Math.atan2(b[1], b[0]) / DEG;
        if (Math.abs(a) > 0.5) this.setMount(this.world.mount.position, this.world.mount.yaw_deg + a);
      }
      e.stopPropagation();
      return;
    }
    if (this.mode === 'walk') this._walkMove(e);
  }

  _onUp(e) {
    if (this._dragYaw) {
      this._dragYaw = null;
      this.v.controls.enabled = this.mode !== 'walk';
      return;
    }
    const d = this._down;
    this._down = null;
    if (!d || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 5) return;
    if (this.mode === 'walk') return this._walkTap(e);
    if (this.mode === 'place') {
      const h = this.hitRoom(e, { level: true });
      if (!h) return this.hint('That spot is not level: click a table, counter or shelf top');
      this.placeAt(h);
    } else if (this.mode === 'scale') {
      const h = this.hitRoom(e);
      if (!h) return;
      this._scaleClick(h.point);
    } else if (this.mode === 'zone') {
      const h = this.hitRoom(e);
      if (!h) return;
      if (!this._pending) { this._pending = h.point; this._marker(h.point, C.walkway); this.hint('Now the opposite corner'); }
      else { const z = this.addZone(this.zoneKind || 'walkway', this._pending, h.point); this._pending = null; this._clearTemp(); this.setMode('orbit'); if (!z) this.hint('Too small: zones are at least 10 cm across'); }
    } else if (this.mode === 'edit') {
      const hits = this._ray(e).intersectObjects(this.boxes.children, true);
      const g = hits.map((x) => { let o = x.object; while (o && !o.userData?.id) o = o.parent; return o; }).find((o) => o && !o.userData.proposal);
      this.select(g ? g.userData.id : null);
    }
  }

  placeAt(h) {
    const w = this.world;
    const scanned = !!w.asset;
    // a click on a furniture box of a box-only world makes that box the mount surface
    const b = h.object?.parent?.userData?.box;
    if (!scanned && b && b.center[2] + b.half[2] - h.point[2] < 30) {
      w.surface = { label: b.label || 'Mount surface', center: [...b.center], half: [...b.half], yaw_deg: b.yaw_deg || 0 };
    } else if (scanned) {                  // a provisional surface around the click; "Find boxes" proposes the real one
      w.surface = { label: 'Mount surface', center: [h.point[0], h.point[1], h.point[2] - 15], half: [300, 300, 15], yaw_deg: w.mount.yaw_deg };
    }
    const top = w.surface ? w.surface.center[2] + w.surface.half[2] : h.point[2];
    this.setMount([h.point[0], h.point[1], top], w.mount.yaw_deg);
    this._build();
    this._applyMount(false);
    this.robotVisible(true);
    const clash = this.baseClash();
    this.hint(clash ? `The base stands in ${clash}: click another spot, or move that box`
      : 'Placed. Drag the white knob to turn the robot, or click elsewhere to move it');
    this._emit('placed', w.mount);
  }

  _marker(p, color) {
    const m = new THREE.Mesh(new THREE.SphereGeometry(10, 16, 10), new THREE.MeshBasicMaterial({ color, toneMapped: false }));
    m.position.fromArray(p);
    m.userData.temp = true;
    this.marks.add(noRay(m));
    this._dirty();
    return m;
  }
  _clearTemp() { [...this.marks.children].filter((m) => m.userData.temp).forEach(disposeTree); this._dirty(); }

  _scaleClick(p) {
    if (!this._pending) { this._clearTemp(); this._pending = p; this._marker(p, C.near); this.hint('Now the second point'); return; }
    const a = this._pending, b = p;
    this._pending = null;
    this._marker(b, C.near);
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(...a), new THREE.Vector3(...b)]),
      new THREE.LineBasicMaterial({ color: C.near, toneMapped: false }));
    line.userData.temp = true;
    this.marks.add(noRay(line));
    const measured = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
    this.scaleRef = { a, b, measured };
    this.hint(`Measured ${Math.round(measured)} mm: enter the true length`);
    this._emit('measured', this.scaleRef);
  }

  // ---------------------------------------------------------------- walk mode
  walk(on) {
    const v = this.v;
    if (on === !!this.walking || (on && !this.world)) return;
    if (on) {
      this.setMode('orbit');
      this.mode = 'walk';
      v._cancelTween?.('camera');
      this._ctl = { update: v.controls.update, enabled: v.controls.enabled };
      v.controls.enabled = false;
      v.controls.update = () => false;          // the camera is ours while walking
      this.walking = { keys: new Set(), vel: new THREE.Vector3(), onFloor: false, t: performance.now() };
      this._buildOctree();
      const start = this._walkStart(), fy = Math.max(start.y, this.floorZ);
      this.capsule = new Capsule(new THREE.Vector3(start.x, fy + CAPSULE_R + 1, start.z), new THREE.Vector3(start.x, fy + EYE_MM - 60, start.z), CAPSULE_R);
      v.camera.position.set(start.x, fy + EYE_MM, start.z);
      v.camera.lookAt(this.baseToScene([0, 0, this.surfaceTop + 150]));
      try {
        this.plc = new PointerLockControls(v.camera, v.renderer.domElement);
        this.plc.addEventListener('change', () => this._dirty());
        // a refused pointer lock (some browsers, embeds, headless) is expected: drag-to-look takes over, no console error
        this.plc.disconnect();
        this.plc._onPointerlockError = () => { this._noLock = true; };
        this.plc.connect();
      } catch (e) { this.plc = null; }
      this._kd = (e) => this._key(e, true);
      this._ku = (e) => this._key(e, false);
      this._lost = () => this.walking?.keys.clear();    // another window or tab: its key-ups never arrive here
      document.addEventListener('keydown', this._kd, true);
      document.addEventListener('keyup', this._ku, true);
      window.addEventListener('blur', this._lost);
      document.addEventListener('visibilitychange', this._lost);
      this.walkEl.classList.add('on');
      this._tick = this._tick.bind(this);
      this._raf = requestAnimationFrame(this._tick);
      this._emit('mode', 'walk');
    } else {
      cancelAnimationFrame(this._raf);
      document.removeEventListener('keydown', this._kd, true);
      document.removeEventListener('keyup', this._ku, true);
      window.removeEventListener('blur', this._lost);
      document.removeEventListener('visibilitychange', this._lost);
      if (this.plc) { try { this.plc.unlock(); } catch (e) { /* not locked */ } this.plc.dispose(); this.plc = null; }
      v.controls.update = this._ctl.update;
      v.controls.enabled = true;
      const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(v.camera.quaternion);
      v.controls.target.copy(v.camera.position).addScaledVector(fwd, 1500);
      this.walking = null;
      this.mode = 'orbit';
      this.walkEl.classList.remove('on');
      this._emit('mode', 'orbit');
    }
    this._overlay();
    this._dirty();
  }

  _overlay() { document.body.classList.toggle('wv-overlay', !!(this.walking || this._tourNav)); }

  _walkStart() {
    // stand where people are (walkways first, then seats, then a ring around the robot) on a spot with room to move;
    // the point returned is on the floor there (scene frame, its y the floor's height)
    const zones = (this.world.zones || []).map((z) => {
      const p = this._zonePoly(z);
      const b = this.worldToBase([p.reduce((s, q) => s + q[0], 0) / p.length, p.reduce((s, q) => s + q[1], 0) / p.length, 0]);
      return [b[0], b[1], Math.hypot(b[0], b[1]), z.kind === 'walkway' ? 0 : 1];
    }).sort((a, b) => a[3] - b[3] || a[2] - b[2]);
    const cands = [];
    for (const [x, y, d] of zones) for (const k of [1, 0.85, 1.15, 0.7]) cands.push([x * k, y * k, d * k]);
    for (const r of [1500, 1900, 2400, 1100]) for (let a = 0; a < 360; a += 30) cands.push([Math.cos(a * DEG) * r, Math.sin(a * DEG) * r, r]);
    const fy = this.floorZ;
    if (!this.octree) { const c = cands.find((q) => q[2] >= 700); return this.baseToScene([c[0], c[1], fy]); }
    const ray = new THREE.Ray(), down = new THREE.Vector3(0, -1, 0);
    // the floor under a spot: a scan's own floor sits a little off floor_z (its lowest point); a counter or a box
    // there is no floor (null)
    const floorAt = (p) => {
      ray.set(new THREE.Vector3(p.x, fy + EYE_MM, p.z), down);
      const hit = this.octree.rayIntersect(ray);
      const y = hit ? hit.position.y : fy;
      return y < fy + 250 ? Math.max(y, fy) : null;
    };
    const free = (x, y) => {
      const p = this.baseToScene([x, y, fy]), f = floorAt(p);
      if (f == null) return null;
      p.y = f;
      return this.octree.capsuleIntersect(new Capsule(new THREE.Vector3(p.x, f + CAPSULE_R + 5, p.z), new THREE.Vector3(p.x, f + EYE_MM - 60, p.z), CAPSULE_R)) ? null : p;
    };
    // the robot in sight from eye height (not from behind a wall: the floor under everything reaches past the room)
    const goal = this.baseToScene([0, 0, this.surfaceTop + 150]);
    const seen = (p) => {
      const eye = new THREE.Vector3(p.x, p.y + EYE_MM, p.z), d = eye.distanceTo(goal);
      ray.set(eye, goal.clone().sub(eye).normalize());
      const hit = this.octree.rayIntersect(ray);
      return !hit || hit.distance > d - 300;
    };
    // and in a room scan, over the scan rather than outside it (a scan of just a counter has no inside: then anywhere)
    const foot = this.assetG ? new THREE.Box3().setFromObject(this.assetG) : null;
    const inside = (p) => !foot || foot.isEmpty() || (p.x > foot.min.x + CAPSULE_R && p.x < foot.max.x - CAPSULE_R
      && p.z > foot.min.z + CAPSULE_R && p.z < foot.max.z - CAPSULE_R);
    const ok = [];
    for (const [x, y, d] of cands) {
      if (d < 700 || d > 3200) continue;
      const p = free(x, y);
      if (p && [[1, 0], [-1, 0], [0, 1], [0, -1]].every(([u, v]) => free(x + u * 350, y + v * 350))) ok.push(p);
    }
    return ok.find((p) => inside(p) && seen(p)) || ok.find(seen) || ok[0] || this.baseToScene([1600, 0, fy]);
  }

  _buildOctree() {
    if (this.octree) return;
    const g = new THREE.Group();
    this.frame.updateMatrixWorld(true);
    let tris = 0;
    const add = (mesh) => {
      const c = mesh.clone();
      c.matrixAutoUpdate = false;
      c.matrix.copy(mesh.matrixWorld);
      g.add(c);
    };
    this.room.traverse((o) => {
      if (!o.isMesh) return;
      const n = (o.geometry.index ? o.geometry.index.count : o.geometry.attributes.position.count) / 3;
      if (o.parent?.userData?.box || tris + n <= OCTREE_MAX_TRIS) { tris += n; add(o); }
    });
    this.octreeNote = this.assetTris > OCTREE_MAX_TRIS ? 'This scan is large: walking collides with the floor and the grey boxes only.' : '';
    if (this.octreeNote) for (const b of this.boxes.children) if (b.userData.mesh) add(b.userData.mesh);
    // a floor under everything (scans often have holes)
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2));
    floor.scale.set(60000, 1, 60000);
    floor.position.y = this.floorZ - 2;
    floor.updateMatrixWorld(true);
    g.add(floor);
    g.updateMatrixWorld(true);
    this.octree = new Octree().fromGraphNode(g);
    disposeTree(g);
  }

  _key(e, down) {
    if (e.target.closest?.('input, textarea, select')) return;
    const k = e.code;
    if (k === 'Escape' && down && !document.pointerLockElement) { e.preventDefault(); this.walk(false); return; }
    const move = ['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ShiftLeft', 'ShiftRight'];
    if (!move.includes(k)) return;
    e.preventDefault();
    if (down) this.walking.keys.add(k); else this.walking.keys.delete(k);
  }

  _walkDown(e) {
    this._look = { x: e.clientX, y: e.clientY };
    if (e.pointerType === 'mouse' && this.plc && !this.plc.isLocked && !this._noLock) {
      try {
        const p = this.v.renderer.domElement.requestPointerLock?.();
        if (p && p.catch) p.catch(() => { this._noLock = true; });
      } catch (err) { this._noLock = true; }
    }
  }

  _walkMove(e) {
    if (!this._look || (this.plc && this.plc.isLocked)) return;
    const dx = e.clientX - this._look.x, dy = e.clientY - this._look.y;
    this._look = { x: e.clientX, y: e.clientY };
    if (!(e.buttons & 1) && e.pointerType === 'mouse') return;
    this.look(dx, dy);
  }

  look(dx, dy) {                            // drag-look: also how walk mode works without pointer lock (and in tests)
    const cam = this.v.camera;
    const eu = new THREE.Euler(0, 0, 0, 'YXZ').setFromQuaternion(cam.quaternion);
    eu.y -= dx * 0.0045;
    eu.x = clamp(eu.x - dy * 0.0045, -1.45, 1.45);
    cam.quaternion.setFromEuler(eu);
    this._dirty();
  }

  _walkTap(e) {                             // touch: tap the floor (or any level spot) to go there
    this._look = null;
    if (e.pointerType === 'mouse') return;
    const h = this.hitRoom(e, { level: true });
    if (h) this.teleport(h.scene);
  }

  teleport(p) {
    const fy = Math.max(p.y, this.floorZ);
    this.capsule.start.set(p.x, fy + CAPSULE_R + 1, p.z);
    this.capsule.end.set(p.x, fy + EYE_MM - 60, p.z);
    this.walking.vel.set(0, 0, 0);
    this.v.camera.position.copy(this.capsule.end).y += 60;
    this._dirty();
  }

  _tick(now) {
    if (!this.walking) return;
    if (!this.v._w || !this.v._h) { this.walk(false); return; }   // the 3D view was put away (another tab): stop
    this._raf = requestAnimationFrame(this._tick);
    const W = this.walking, dt = clamp((now - W.t) / 1000, 0, 0.25);      // real time even at a few frames per second
    W.t = now;
    this._fps = (this._fps || 0) * 0.95 + (dt > 0 ? 1 / dt : 0) * 0.05;
    this.stats.fps = Math.round(this._fps);
    const k = W.keys, cam = this.v.camera;
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(cam.quaternion); fwd.y = 0; fwd.normalize();
    const right = new THREE.Vector3().crossVectors(fwd, cam.up).normalize();
    const want = new THREE.Vector3();
    if (k.has('KeyW') || k.has('ArrowUp')) want.add(fwd);
    if (k.has('KeyS') || k.has('ArrowDown')) want.sub(fwd);
    if (k.has('KeyD') || k.has('ArrowRight')) want.add(right);
    if (k.has('KeyA') || k.has('ArrowLeft')) want.sub(right);
    const speed = WALK_MM_S * (k.has('ShiftLeft') || k.has('ShiftRight') ? 2 : 1);
    if (want.lengthSq()) want.normalize().multiplyScalar(speed);
    const steps = clamp(Math.ceil(dt / 0.01), 1, 25);                    // <= 14 mm per collision step
    for (let i = 0; i < steps; i++) {
      const h = dt / steps;
      W.vel.x = want.x; W.vel.z = want.z;
      if (!W.onFloor) W.vel.y -= GRAVITY * h; else W.vel.y = Math.max(W.vel.y, 0);
      this.capsule.translate(_v.copy(W.vel).multiplyScalar(h));
      const r = this.octree.capsuleIntersect(this.capsule);
      W.onFloor = false;
      if (r) {
        W.onFloor = r.normal.y > 0.5;
        if (!W.onFloor) W.vel.addScaledVector(r.normal, -r.normal.dot(W.vel));
        else W.vel.y = 0;
        this.capsule.translate(r.normal.multiplyScalar(r.depth));
      }
    }
    if (this.capsule.start.y < this.floorZ - 3000) this.teleport(this._walkStart());      // fell out of the room
    const moved = !cam.position.equals(_v2.copy(this.capsule.end).setY(this.capsule.end.y + 60));
    cam.position.copy(this.capsule.end).y += 60;
    if (moved || want.lengthSq()) this._dirty();
  }

  // ---------------------------------------------------------------- the simulation in the world
  play(doc, { loop = true } = {}) {
    if (!doc) return;
    this.doc = doc;
    this.contacts = (doc.metrics?.world_contacts || []).slice(0, 12);
    this._buildBoxes();
    this._contactMarks(doc);
    this.v.playTrajectory(doc, { loop, frame: false });
  }

  _frameAt(doc, t) {
    const F = doc.frames || [];
    let best = F[0];
    for (const f of F) { if (Math.abs(f.t - t) < Math.abs(best.t - t)) best = f; }
    return best;
  }

  _contactMarks(doc) {
    [...this.base.children].filter((o) => o.userData.contact).forEach(disposeTree);
    const m = doc.metrics || {};
    const list = [...(m.world_contacts || []).map((c) => ({ ...c, color: C.hit })),
      ...(m.min_clearance_at && !(m.world_contacts || []).length ? [{ ...m.min_clearance_at, color: C.near, near: true }] : [])];
    for (const c of list.slice(0, 8)) {
      const f = this._frameAt(doc, c.t);
      if (!f) continue;
      let p;
      if (c.link === 'object' && f.obj) p = f.obj.slice(0, 3);
      else {
        const q = {};
        (doc.joints || []).forEach((n, i) => { q[n] = f.q[i]; });
        try { p = new THREE.Vector3().setFromMatrixPosition(this.v.fk(q, c.link)).toArray(); } catch (e) { p = null; }
      }
      if (!p) continue;
      const g = new THREE.Group();
      g.position.fromArray(p);
      g.add(new THREE.Mesh(new THREE.SphereGeometry(c.near ? 9 : 13, 20, 12), new THREE.MeshBasicMaterial({ color: c.color, toneMapped: false })),
        new THREE.Mesh(new THREE.RingGeometry(20, 26, 40), new THREE.MeshBasicMaterial({ color: c.color, transparent: true, opacity: 0.7, side: THREE.DoubleSide, toneMapped: false })));
      g.userData = { contact: c, label: c.near ? `${Math.round(m.min_clearance_mm)} mm` : `hits ${c.obstacle}` };
      this.base.add(noRay(g));
    }
    this._dirty();
  }

  // ---------------------------------------------------------------- the six views
  viewCameras(doc = this.doc, names = null) {
    const tg = doc?.targets || {};
    const pick = tg.pick || [220, -80, 22], place = tg.place || [160, 170, 22];
    const top = this.surfaceTop;
    const reach = this._reachMM(doc);
    const fz = this.floorZ;
    const mid = [(pick[0] + place[0]) / 2, (pick[1] + place[1]) / 2, top + 60];
    const T = (name) => (doc?.markers || []).find((m) => (m.label || '').startsWith(name))?.t;
    const tGrasp = T('at grasp') ?? T('contact') ?? 0, tMove = T('lifted') ?? tGrasp;
    const out = {};
    let az = 35 * DEG;
    if (this.assetG) {          // a scanned room: from its open middle (a robot on a counter has a wall behind it)
      const c = this.sceneToBase(new THREE.Box3().setFromObject(this.assetG).getCenter(new THREE.Vector3()));
      if (Math.hypot(c[0], c[1]) > 200) az = Math.atan2(c[1], c[0]) + 20 * DEG;   // a little to one side of the arm
    }
    out.overview = { eye: [Math.cos(az) * reach * 2.3, Math.sin(az) * reach * 2.3, Math.max(fz + EYE_MM, top + reach * 1.1)], target: [mid[0] * 0.5, mid[1] * 0.5, top + 120], t: tMove };
    out.top_down = { eye: [mid[0] * 0.5, mid[1] * 0.5, top + reach * 3.1 + 250], target: [mid[0] * 0.5, mid[1] * 0.5, top], up: [1, 0, 0], t: tMove };
    const dx = place[0] - pick[0], dy = place[1] - pick[1], dl = Math.hypot(dx, dy) || 1;
    out.along_path = { eye: [pick[0] - dx / dl * 620, pick[1] - dy / dl * 620, top + 420], target: [mid[0] * 0.6 + place[0] * 0.4, mid[1] * 0.6 + place[1] * 0.4, top + 40], t: tMove + 0.6 };
    // a wrist camera looks past the fingers from the side of the hand, down onto the object
    const rl = Math.hypot(pick[0], pick[1]) || 1, ux = pick[0] / rl, uy = pick[1] / rl;
    out.wrist = { eye: [pick[0] - ux * 70 - uy * 120, pick[1] - uy * 70 + ux * 120, pick[2] + 120], target: [pick[0], pick[1], pick[2]],
      t: Math.max(tGrasp - 0.25, 0) };
    let wk = null;
    for (const z of this.world.zones || []) {
      const p = this._zonePoly(z);
      const c = this.worldToBase([p.reduce((s, q) => s + q[0], 0) / p.length, p.reduce((s, q) => s + q[1], 0) / p.length, 0]);
      const d = Math.hypot(c[0], c[1]);
      if (!wk || d < wk.d) wk = { c, d, z };
    }
    const near = (p) => { const d = Math.hypot(p[0], p[1]); return d > 2600 ? [p[0] * 2600 / d, p[1] * 2600 / d] : p; };
    const we = near(wk ? wk.c : [1500, -500, 0]);
    out.walkway = { eye: [we[0], we[1], fz + EYE_MM], target: [0, 0, top + 180], t: tMove };
    // other spots in that zone, nearest the robot first, if something stands between its middle and the robot
    const alts = { walkway: wk ? this._zoneSpots(wk.z).map((p) => [...near(p), fz + EYE_MM]) : [] };
    // the base from the side of its nearest surface edge, so the margin to the edge is in the picture
    const e = this._nearestEdge();
    const px = -e[1], py = e[0];
    out.base = { eye: [-e[0] * 430 + px * 260, -e[1] * 430 + py * 260, top + 300], target: [e[0] * 110, e[1] * 110, top + 25], t: 0 };
    // nothing opaque may stand in front of a view: move the eye around its target, else see through what is in the way
    const t0 = performance.now();
    const robot = [0, 0, top + 80];
    const focus = { wrist: [], top_down: [robot, [pick[0], pick[1], pick[2] + 20], [place[0], place[1], place[2] + 20]] };
    for (const k of names || Object.keys(out)) {
      if (!out[k]) continue;
      out[k] = this._clearView({ ...out[k], focus: focus[k] || [robot] }, { alts: alts[k] || [], search: k !== 'top_down', until: t0 + 1500 });
    }
    return out;
  }

  // up to 16 standing spots inside a zone (base frame, mm), nearest the robot first
  _zoneSpots(z) {
    const poly = this._zonePoly(z);
    const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const step = Math.max(250, Math.max(x1 - x0, y1 - y0) / 12);
    const inside = (x, y) => {
      let c = false;
      for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const [xi, yi] = poly[i], [xj, yj] = poly[j];
        if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) c = !c;
      }
      return c;
    };
    const out = [];
    for (let x = x0 + step / 2; x < x1; x += step) for (let y = y0 + step / 2; y < y1; y += step) if (inside(x, y)) out.push(this.worldToBase([x, y, 0]));
    return out.sort((a, b) => Math.hypot(a[0], a[1]) - Math.hypot(b[0], b[1])).slice(0, 16);
  }

  // what stands between an eye and a point (base frame, mm): opaque furniture boxes (also around the eye) and the
  // room scan; the grey obstacle boxes are see-through and never count. -> {boxes: Set of room groups, scan: mm}
  _occluders(eye, point) {
    const e = this.baseToScene(eye), p = this.baseToScene(point);
    const dir = p.clone().sub(e), dist = dir.length();
    const out = { boxes: new Set(), scan: 0 };
    if (dist < 1) return out;
    dir.divideScalar(dist);
    const ray = new THREE.Ray(e, dir), r = new THREE.Ray(), hit = new THREE.Vector3();
    for (const g of this.room.children) {
      const m = g.userData.mesh;
      if (!g.userData.box || !m || !g.visible) continue;
      m.updateMatrixWorld(true);
      r.copy(ray).applyMatrix4(_m.copy(m.matrixWorld).invert());
      if (UNIT_BOX.containsPoint(r.origin) || (r.intersectBox(UNIT_BOX, hit) && hit.applyMatrix4(m.matrixWorld).distanceTo(e) < dist - 40)) out.boxes.add(g);
    }
    if (this.assetG) {
      const rc = this._rc || (this._rc = new THREE.Raycaster());
      rc.set(e, dir);
      rc.near = 0;
      rc.far = Math.max(dist - 40, 1);
      const hits = rc.intersectObject(this.assetG, true);
      if (hits.length) out.scan = hits[hits.length - 1].distance;
    }
    return out;
  }

  /** The view as given when nothing opaque is in the way; else the eye moved around its target (or to one of
   * `alts`) until it sees the target and the `focus` points; else (or with search off) the same eye seeing through: `ghost`
   * lists the furniture boxes drawn see-through and `cut` (mm from the eye) clips the scan in front of the target. */
  _clearView(vw, { alts = [], search = true, until = Infinity } = {}) {
    const pts = [vw.target, ...(vw.focus || [])];
    const occ = (eye) => {
      const o = { boxes: new Set(), scan: 0 };
      for (const p of pts) { const r = this._occluders(eye, p); r.boxes.forEach((b) => o.boxes.add(b)); o.scan = Math.max(o.scan, r.scan); }
      return o;
    };
    const first = occ(vw.eye);
    if (!first.boxes.size && !first.scan) return vw;
    if (search) {
      const t = vw.target, d = vw.eye.map((x, i) => x - t[i]);
      const rr = Math.hypot(d[0], d[1]), a0 = Math.atan2(d[1], d[0]);
      const cands = alts.map((eye, i) => ({ eye, cost: i * 0.01 }));
      for (const k of [1, 0.8, 0.62]) for (const dz of [0, 300, 600]) for (const da of [0, 25, -25, 50, -50, 80, -80, 115, -115, 150, -150, 180]) {
        if (!da && !dz && k === 1) continue;
        const a = a0 + da * DEG;
        cands.push({ eye: [t[0] + Math.cos(a) * rr * k, t[1] + Math.sin(a) * rr * k, t[2] + d[2] * k + dz], cost: 1 + Math.abs(da) / 25 + dz / 300 + (1 - k) * 8 });
      }
      cands.sort((a, b) => a.cost - b.cost);
      for (const c of cands.slice(0, 60)) {
        if (performance.now() > until) break;          // big scans: stop searching, see through instead
        const o = occ(c.eye);
        if (!o.boxes.size && !o.scan) return { ...vw, eye: c.eye, moved: true };
      }
    }
    return { ...vw, ghost: [...first.boxes], cut: first.scan ? first.scan + 30 : 0 };
  }

  // furniture in front of a view drawn see-through (restore with the returned function)
  _seeThrough(vw) {
    const saved = [];
    for (const g of vw.ghost || []) {
      const m = g.userData.mesh?.material;
      if (!m) continue;
      saved.push([m, m.transparent, m.opacity, m.depthWrite]);
      Object.assign(m, { transparent: true, opacity: 0.16, depthWrite: false, needsUpdate: true });
    }
    this._dirty();
    return () => { for (const [m, transparent, opacity, depthWrite] of saved) Object.assign(m, { transparent, opacity, depthWrite, needsUpdate: true }); this._dirty(); };
  }

  _viewTitle(name, vw) {
    const cut = [...new Set((vw?.ghost || []).map((g) => g.userData.box?.label || 'furniture'))];
    if (vw?.cut) cut.push('part of the scan');
    return (VIEW_TITLE[name] || name) + (cut.length ? ` (see-through: ${cut.slice(0, 3).join(', ')})` : '');
  }

  _reachMM(doc) { return this.reachMM || doc?.metrics?.reach_3d_mm || 450; }

  _nearestEdge() {                          // unit vector (base frame) from the robot to the closest surface edge
    const s = this.world.surface;
    if (!s) return [-1, 0];
    const c = this.worldToBase(s.center), yaw = (s.yaw_deg - this.world.mount.yaw_deg) * DEG;
    const ax = [Math.cos(yaw), Math.sin(yaw)], ay = [-Math.sin(yaw), Math.cos(yaw)];
    const lx = -(c[0] * ax[0] + c[1] * ax[1]), ly = -(c[0] * ay[0] + c[1] * ay[1]);   // robot in the surface's axes
    const opts = [[s.half[0] - lx, ax], [s.half[0] + lx, [-ax[0], -ax[1]]], [s.half[1] - ly, ay], [s.half[1] + ly, [-ay[0], -ay[1]]]];
    opts.sort((a, b) => a[0] - b[0]);
    return opts[0][1];
  }

  _camFor(vw, aspect) {
    const cam = new THREE.PerspectiveCamera(48, aspect, Math.max(2, vw.cut || 0), 150000);
    const eye = this.baseToScene(vw.eye), tgt = this.baseToScene(vw.target);
    cam.position.copy(eye);
    if (vw.up) cam.up.copy(this.baseToScene(vw.up).sub(this.baseToScene([0, 0, 0])).normalize());
    cam.lookAt(tgt);
    cam.updateMatrixWorld(true);
    return cam;
  }

  _labels(ctx, cam, w, h, name, vw = {}) {
    const proj = (p) => { const q = p.clone().project(cam); return q.z < 1 && q.z > -1 ? [(q.x + 1) / 2 * w, (1 - q.y) / 2 * h] : null; };
    const tag = (x, y, text, color) => {
      ctx.font = '600 15px Inter, system-ui, sans-serif';
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = 'rgba(13,17,22,0.82)';
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x - tw / 2 - 7, y - 22, tw + 14, 21, 5) : ctx.rect(x - tw / 2 - 7, y - 22, tw + 14, 21); ctx.fill();
      ctx.fillStyle = color; ctx.fillRect(x - tw / 2 - 7, y - 22, 3, 21);
      ctx.fillStyle = '#e8edf2'; ctx.fillText(text, x - tw / 2, y - 6);
    };
    const seen = [];
    const rc = new THREE.Raycaster();
    const blockers = [this.room, ...(this.v.model ? [this.v.model.root] : [])];
    const seeThrough = new Set((vw.ghost || []).map((g) => g.userData.mesh));
    const hidden = (p) => {
      const d = p.distanceTo(cam.position);
      rc.set(cam.position, p.clone().sub(cam.position).normalize());
      rc.near = cam.near;
      rc.far = d - 25;
      return rc.intersectObjects(blockers, true).some((x) => x.object.visible !== false && !seeThrough.has(x.object));
    };
    const place = (p, text, color) => {
      const s = proj(p);
      if (!s || s[0] < 10 || s[0] > w - 10 || s[1] < 40 || s[1] > h - 30) return;
      if (hidden(p)) return;
      if (seen.some(([x, y]) => Math.abs(x - s[0]) < 90 && Math.abs(y - s[1]) < 22)) return;
      seen.push(s);
      tag(s[0], s[1], text, color);
    };
    this.frame.updateMatrixWorld(true);
    const tg = this.doc?.targets || {};
    if (tg.pick) place(this.baseToScene([tg.pick[0], tg.pick[1], tg.pick[2] + 40]), 'Pick', '#4aa3ff');
    if (tg.place) place(this.baseToScene([tg.place[0], tg.place[1], tg.place[2] + 40]), 'Place', '#3ddc97');
    for (const c of this.base.children.filter((o) => o.userData.contact)) place(c.getWorldPosition(new THREE.Vector3()), c.userData.label, c.userData.contact.color);
    for (const g of this.boxes.children) {
      const b = g.userData.box;
      if (!b) continue;
      place(new THREE.Vector3(b.center[0], b.center[1], b.center[2] + b.half[2]).applyMatrix4(this.frame.matrixWorld), b.label || 'Box', g.userData.proposal ? C.auto : C.box);
    }
    for (const g of this.zoneG.children) {
      const z = g.userData.zone, p = this._zonePoly(z);
      place(new THREE.Vector3(p.reduce((s, q) => s + q[0], 0) / p.length, p.reduce((s, q) => s + q[1], 0) / p.length, (this.world.floor_z ?? 0) + 20).applyMatrix4(this.frame.matrixWorld), z.label, C[z.kind]);
    }
    for (const g of this.marks.children.filter((o) => o.userData.candidate)) place(g.getWorldPosition(new THREE.Vector3()), `Mount ${g.userData.candidate}`, C.cand);
    // title bar and footer
    ctx.fillStyle = 'rgba(10,13,17,0.78)';
    ctx.fillRect(0, 0, w, 34);
    ctx.fillStyle = '#9db2ff'; ctx.font = '650 15px Inter, system-ui, sans-serif';
    ctx.fillText(this._viewTitle(name, vw), 12, 22);
    ctx.fillStyle = '#838c9d'; ctx.font = '500 13px Inter, system-ui, sans-serif';
    const right = `${this.world.name} · world check (beta)`;
    ctx.fillText(right, w - 12 - ctx.measureText(right).width, 22);
    ctx.fillStyle = 'rgba(10,13,17,0.7)'; ctx.fillRect(0, h - 24, w, 24);
    ctx.fillStyle = '#838c9d'; ctx.font = '500 12px Inter, system-ui, sans-serif';
    ctx.fillText('Grey boxes: what the simulation sees. Blue dome: reach. Orange: carry path. Red: contact.', 10, h - 8);
  }

  /** Render the six views at width x height and return [{name, title, jpeg_b64}] (labels drawn on a 2-D canvas, so
   * they are part of the picture the reviewers get). The robot is posed at each view's moment of the simulation. */
  async snapshotViews(doc = this.doc, { width = 1024, height = 768, quality = 0.85, names = VIEWS.map((x) => x[0]) } = {}) {
    if (!this.world) throw new Error('no world');
    const v = this.v, r = v.renderer;
    if (doc && v._traj?.src !== doc) this.play(doc, { loop: false });
    v.pause?.();
    this.showReach(this._reachMM(doc));
    const pr = r.getPixelRatio(), size = r.getSize(new THREE.Vector2());
    const cams = this.viewCameras(doc, names);
    const out = [];
    const cv = document.createElement('canvas');
    cv.width = width; cv.height = height;
    const ctx = cv.getContext('2d');
    const ringOn = this.ring?.visible, tcOn = this.tc?.visible;   // put back as they were (a detached gizmo stays hidden)
    if (this.ring) this.ring.visible = false;
    if (this.tc) this.tc.visible = false;
    try {
      r.setPixelRatio(1);
      r.setSize(width, height, false);
      for (const name of names) {
        const vw = cams[name];
        if (!vw) continue;
        if (v._traj && Number.isFinite(vw.t)) v.setTime(vw.t);
        if (this.reach) this.reach.visible = name !== 'wrist' && name !== 'base';
        v._update?.(0);                          // pose the robot for this moment now (the viewer's loop is async)
        const cam = this._camFor(vw, width / height);
        const undo = this._seeThrough(vw);
        try {
          r.render(v.scene, cam);
          ctx.drawImage(r.domElement, 0, 0, width, height);
          this._labels(ctx, cam, width, height, name, vw);
        } finally { undo(); }
        const url = cv.toDataURL('image/jpeg', quality);
        out.push({ name, title: this._viewTitle(name, vw), jpeg_b64: url.slice(url.indexOf(',') + 1), dataUrl: url });
        await sleep(0);
      }
    } finally {
      r.setPixelRatio(pr);
      r.setSize(size.x, size.y, false);
      v.resize?.();
      if (this.ring) this.ring.visible = ringOn;
      if (this.tc) this.tc.visible = !!tcOn;
      if (this.reach) this.reach.visible = true;
      this._dirty();
    }
    return out;
  }

  frameOverview(ms = 900) {
    const cams = this.viewCameras(this.doc, ['overview']);
    return this.flyToView(cams.overview, ms);
  }

  flyToView(vw, ms = 1200) {
    const v = this.v;
    const eye = this.baseToScene(vw.eye), tgt = this.baseToScene(vw.target);
    const dir = eye.clone().sub(tgt);
    const dist = dir.length() * Math.tan(24 * DEG) / Math.tan((v.camera.fov / 2) * DEG);
    if (vw.up) dir.add(this.baseToScene(vw.up).sub(this.baseToScene([0, 0, 0])).multiplyScalar(dist * 0.02));   // top-down: a hint of tilt
    return v._flyTo(tgt, dist, dir.normalize(), ms);
  }

  // ---------------------------------------------------------------- guided tour (Present-mode style captions)
  async tour(steps, doc = this.doc, { auto = true, source = '' } = {}) {
    if (!steps?.length || !this.world) return;
    this.stopTour();
    if (this.walking) this.walk(false);
    const gen = this._tourGen = (this._tourGen || 0) + 1;
    const live = () => this._tourGen === gen;
    const cams = this.viewCameras(doc);
    if (doc && this.v._traj?.src !== doc) this.play(doc, { loop: false });
    this.v.pause?.();
    this.showReach(this._reachMM(doc));
    let i = 0;
    const go = async (k) => {
      i = clamp(k, 0, steps.length - 1);
      const st = steps[i], vw = cams[st.viewpoint] || cams.overview;
      this._caption(steps, i, source, vw);
      if (this.v._traj && Number.isFinite(vw.t)) this.v.setTime(vw.t);
      this._uncut?.();
      await this.flyToView(vw, 1300);
      if (live() && (vw.ghost?.length || vw.cut)) this._uncut = this._cutLive(vw);
    };
    this._tourNav = { next: () => go(i + 1), prev: () => go(i - 1), stop: () => this.stopTour() };
    this._overlay();
    await go(0);
    while (live() && auto && i < steps.length - 1) {
      const words = (steps[i].caption || '').split(/\s+/).length;
      await sleep(Math.max(3800, words * 330));
      if (!live()) return;
      if (this._tourHold) { await sleep(400); continue; }
      await go(i + 1);
    }
  }

  // a tour stop whose view sees through furniture or cuts the scan: the same on the live camera until the next
  // stop, the end of the tour or the first drag
  _cutLive(vw) {
    const undo = this._seeThrough(vw);
    const cam = this.v.camera, near0 = cam.near;
    if (vw.cut) {
      const tgt = this.baseToScene(vw.target), keep = this.baseToScene(vw.eye).distanceTo(tgt) - vw.cut;
      cam.near = Math.max(near0, cam.position.distanceTo(tgt) - keep);
      cam.updateProjectionMatrix();
    }
    let done = false;
    const off = () => {
      if (done) return;
      done = true;
      undo();
      cam.near = near0;
      cam.updateProjectionMatrix();
      this.v.controls.removeEventListener('start', off);
      if (this._uncut === off) this._uncut = null;
      this._dirty();
    };
    this.v.controls.addEventListener('start', off);
    return off;
  }

  _caption(steps, i, source, vw = null) {
    const st = steps[i], c = this.capEl;
    c.innerHTML = `<div class="top"><b>${i + 1}/${steps.length}</b><span class="t">${esc(this._viewTitle(st.viewpoint, vw))}</span>
      ${source ? `<span class="src">${esc(source)}</span>` : ''}<span style="flex:1"></span>
      <button class="wv-btn" data-k="prev" ${i ? '' : 'disabled'}>Back</button><button class="wv-btn primary" data-k="next" ${i < steps.length - 1 ? '' : 'disabled'}>Next</button>
      <button class="wv-x" data-k="stop" title="End the tour">&times;</button></div>
      <div class="say">${esc(st.caption)}</div><div class="dots">${steps.map((_, k) => `<i class="${k < i ? 'done' : k === i ? 'cur' : ''}"></i>`).join('')}</div>`;
    c.classList.add('on');
    c.querySelectorAll('[data-k]').forEach((b) => { b.onclick = () => { this._tourHold = true; this._tourNav?.[b.dataset.k](); }; });
  }

  stopTour() {
    this._uncut?.();
    this._tourGen = (this._tourGen || 0) + 1;
    this._tourHold = false;
    this.capEl.classList.remove('on');
    this._tourNav = null;
    this._overlay();
  }
}

// ================================================================== WorldPanel: the Studio UI for world mode
const SEC = { room: 'room', place: 'place', boxes: 'boxes', zones: 'zones', sim: 'sim', check: 'check' };
const SRC = { claude: 'Claude (AI)', subq: 'SubQ Snap (AI)', rules: 'Measured' };
const pct = (x) => `${Math.round(x * 100)}%`;

export class WorldPanel {
  /** host: the viewer container; wv: a WorldView; api: {get, post, put} like studio.api; doc(): the current
   * pick_place simulation document (or null); toast(text); queue(): the room's running and waiting jobs (studio.S.queue);
   * onToggle(open): the panel opened or closed. */
  constructor(host, { wv, api, toast = () => {}, doc = () => null, project = () => '', queue = () => [], onToggle = () => {} } = {}) {
    this.wv = wv; this.api = api; this.toast = toast; this.doc = doc; this.project = project; this.queue = queue;
    this.onToggle = onToggle;
    this.el = el('div', 'wv-panel');
    this.el.style.display = 'none';
    host.appendChild(this.el);
    this.state = { notes: NOTES, starters: [], result: null, busy: null, active: null, saved: null, pending: null, ai: {} };
    wv.on((type) => { if (['change', 'mode', 'world', 'select', 'measured', 'placed'].includes(type)) this.render(); });
    this.el.addEventListener('click', (e) => this._click(e));
    this.el.addEventListener('change', (e) => this._change(e));
    // a redraw while a field has focus would throw away what is being typed: it waits for the field to let go
    this.el.addEventListener('focusout', () => { if (this._later) { this._later = false; setTimeout(() => this.render(), 0); } });
    this.el.addEventListener('input', (e) => { if (e.target.dataset.f === 'len') this.state.len = e.target.value; });
  }

  get open() { return this.el.style.display !== 'none'; }

  /** The shared 3D view moved to another tab (the Simulation tab's replay) or came back to the Robot tab. Away, the
   * panel stays open but out of sight, and a walk or a guided tour stops: they belong to the Robot tab. */
  setAway(away) {
    this.away = !!away;
    this.el.classList.toggle('away', this.away);
    if (this.away) { this.wv.walk(false); this.wv.stopTour(); }
    this.render();
  }
  /** World mode is on: the newest revision simulates in a room, or a room was just saved for the next one. */
  get on() { const p = this.state.pending; return p ? !p.off : !!this.state.active?.id; }

  async toggle(on = !this.open) {
    this.el.style.display = on ? 'block' : 'none';
    if (on) await this.refresh();
    else {
      const wv = this.wv;
      wv.walk(false); wv.setMode('orbit'); wv.stopTour();
      if (!this.on && wv.world) {           // world mode is off: the plain table again (an unsaved draft is kept)
        this.state.draft = wv.world._dirty ? { world: wv.world, assetUrl: wv.assetUrl } : null;
        wv.clear({ reframe: true });
      }
    }
    this.onToggle(this.open);
    this.render();
  }

  reset() {          // another project: nothing of the last one's checks, proposals or drafts carries over
    Object.assign(this.state, { result: null, active: null, saved: null, pending: null, draft: null, surfaceProposal: null,
      views: null, problem: null, sim: null, busy: null });
  }

  _busy(sec, text = '') {
    if (sec) { this.wv.stopTour(); this.wv.walk(false); }   // a tour or a walk would fight the work over the camera
    this.state.busy = sec ? { sec, text } : null;
    this.render();
  }

  // a world (or "off") the Studio queued a revision for: shown as on its way until that job ends
  _queued(r) {
    if (r?.job?.id) this.state.pending = { job: r.job.id, sha: r.sha || '', name: r.world?.name || '', off: !r.world, t: Date.now() };
  }

  /** A job of this room ended (studio `queue` event): the world it was simulating is in, or it failed. */
  jobEnded(job) {
    const p = this.state.pending;
    if (!p || job.id !== p.job) return false;
    this.state.pending = null;
    if (job.status === 'error') this.toast(p.off ? "Couldn't turn world mode off: see the chat" : `The simulation in ${p.name || 'the room'} didn't finish: see the chat`);
    return true;
  }

  async refresh() {
    try {
      const r = await this.api.get('/api/world');
      const s = this.state;
      Object.assign(s, { notes: r.notes || NOTES, starters: r.starters || [], sim: r.sim, active: r.active || { id: '', sha: '' },
        problem: r.problem, ai: r.ai || {}, saved: r.world ? { world: r.world, sha: r.sha, assetUrl: r.asset_url } : null });
      const p = s.pending;
      if (p && ((!p.off && s.active.sha === p.sha) || (p.off && !s.active.id) ||
          (Date.now() - p.t > 3000 && !(this.queue() || []).some((j) => j.id === p.job)))) s.pending = null;
      if (s.result && s.sim && s.result.rev !== s.sim.rev) s.result = { ...s.result, stale: true };
      const wv = this.wv;
      if (wv.world?._dirty) { /* being edited: keep it */ }
      else if (r.world && this.on) await wv.setWorld(r.world, { assetUrl: r.asset_url });
      else if (!this.on && s.draft) { await wv.setWorld(s.draft.world, { assetUrl: s.draft.assetUrl }); wv.world._dirty = true; s.draft = null; }
      else if (!this.on && wv.world) wv.clear({ reframe: true });
    } catch (e) { this.toast(`World mode: ${e.message || e}`); }
    this.render();
  }

  async useStarter(id) {
    if (this.state.busy) return;
    const st = this.state.starters.find((s) => s.id === id);
    this._busy(SEC.room, `Saving ${st?.name || id}…`);
    try {
      const r = await this.api.post('/api/world', { starter: id });
      this._queued(r);
      if (r.world) await this.wv.setWorld(r.world, {});
      this.state.result = null;
      this.toast(r.unchanged ? `${st?.name || id} is already simulated` : `World saved: the simulation reruns in ${r.world?.name || id}`);
    } catch (e) { this.toast(`Couldn't load the starter: ${e.message || e}`); }
    this._busy(null);
  }

  async useSaved() {         // world mode is off: simulate in the last saved room again (saved by Save and simulate)
    const s = this.state.saved;
    if (!s || this.state.busy) return;
    await this.wv.setWorld(s.world, { assetUrl: s.assetUrl });
    this.wv.world._dirty = true;
    this.wv.changed('reuse');
  }

  async upload(file) {
    if (!file || this.state.busy) return;
    if (file.size > 300 * 1024 * 1024) return this.toast('The room file is larger than 300 MB');
    this._busy(SEC.room, 'Uploading the room…');
    try {
      const r = await this.api.put('/api/world/upload', file, { name: file.name });
      const wv = this.wv;
      await wv.setWorld({ ...r.draft, mount: { position: [0, 0, 0], yaw_deg: 0 }, surface: null }, { assetUrl: r.asset_url, frame: false });
      wv.world._dirty = true;                  // not saved: Place, then Save and simulate
      wv.robotVisible(false);                  // until it is placed (it would stand at the scan's origin)
      this.state.result = null;
      await wv.frameAsset(600);
      wv.setMode('place');
      for (const n of r.draft.notes || []) this.toast(n);
      this.toast(`Room received (${r.faces.toLocaleString()} triangles). Set the scale, then click a level surface for the robot.`);
    } catch (e) { this.toast(`Upload failed: ${e.message || e}`); }
    this._busy(null);
  }

  async autoBoxes() {
    if (this.state.busy) return;
    this._busy(SEC.boxes, 'Finding boxes in the scan…');
    try {
      const r = await this.api.post('/api/world/obstacles/auto', { world: this.worldBody() });
      this.wv.setProposals(r.obstacles);
      if (r.surface) { this.state.surfaceProposal = r.surface; }
      this.toast(r.obstacles.length ? `${r.obstacles.length} boxes proposed${r.stats?.truncated ? ' (the scan was cut short)' : ''}: keep the ones that match real things`
        : 'No boxes found near the robot in the scan');
    } catch (e) { this.toast(`Couldn't box the scan: ${e.message || e}`); }
    this._busy(null);
  }

  worldBody() {
    const w = JSON.parse(JSON.stringify(this.wv.world || {}));
    delete w._dirty; delete w.notes;
    return w;
  }

  async save() {
    const wv = this.wv;
    if (this.state.busy) return;
    if (!wv.world?.surface) return this.toast('Place the robot on a surface first');
    this._busy(SEC.sim, 'Saving…');
    try {
      const r = await this.api.post('/api/world', { world: this.worldBody() });
      this._queued(r);
      if (r.world) { const keep = wv.doc; await wv.setWorld(r.world, { assetUrl: wv.assetUrl, frame: false }); wv.doc = keep; }
      wv.setMode('orbit');
      this.state.result = null;                // that check was about the world before this save
      this.toast(r.unchanged ? 'Nothing changed: this world is already simulated' : 'World saved: only the simulation reruns');
    } catch (e) { this.toast(`Couldn't save the world: ${e.message || e}`); }
    this._busy(null);
  }

  async turnOff() {
    if (this.state.busy) return;
    this._busy(SEC.sim, 'Turning world mode off…');
    try {
      const r = await this.api.post('/api/world', { off: true });
      this._queued(r);
      this.state.result = null;
      this.state.draft = null;
      this.wv.clear({ reframe: true });
      this.toast('World mode off: the simulation runs on the plain table again');
    } catch (e) { this.toast(`${e.message || e}`); }
    this._busy(null);
  }

  /** The placement check (measured), and with `ai` the AI review of six rendered views. Only for a saved world whose
   * simulation is in: an unsaved edit would be checked against the old simulation. */
  async check(ai) {
    const s = this.state, wv = this.wv;
    if (s.busy) return;
    if (wv.world?._dirty) return this.toast('Save and simulate first: the check uses the saved world\'s simulation');
    if (s.pending) return this.toast('The simulation in this world is still running');
    const doc = this.doc() || wv.doc;
    const render = (ai && s.ai.claude) || (s.subq && s.ai.subq);    // pictures only go to a reviewer that is on
    this._busy(SEC.check, render ? 'Rendering six views…' : (wv.world?.asset ? 'Checking, and measuring the path against the room scan…' : 'Checking…'));
    try {
      let views = [];
      if (render) {
        views = await wv.snapshotViews(doc);
        s.views = views;
        this._busy(SEC.check, ai && s.ai.claude ? 'AI review (up to a minute)…' : 'Asking SubQ…');
      }
      s.result = await this.api.post('/api/world/inspect', { views: views.map(({ name, jpeg_b64 }) => ({ name, jpeg_b64 })), ai: !!ai,
        subq: !!(s.subq && s.ai.subq) });
      s.result.asked = { ai: !!ai, subq: !!s.subq };
    } catch (e) { this.toast(`Check failed: ${e.message || e}`); }
    this._busy(null);
  }

  async apply(i) {
    const ch = this.state.result?.review?.suggested_changes?.[i];
    if (!ch || ch.applied || this.state.busy) return;
    const wv = this.wv;
    if (ch.kind === 'move_mount' && ch.mount) { wv.setMount(ch.mount.position, ch.mount.yaw_deg); this.toast('Moved the robot: save the world to simulate it there'); }
    else if (ch.kind === 'add_obstacle' && ch.obstacle) {
      wv.world.obstacles.push({ ...ch.obstacle, id: slug(ch.obstacle.label) + '-' + uid() }); wv._buildBoxes(); wv.changed('boxes');
      this.toast('Box added: save the world to simulate with it');
    } else if (ch.kind === 'move_target' && ch.changes) {
      try { await this.api.post('/api/params', { changes: ch.changes }); this.toast('Target moved: the arm is re-sized for it and simulated again'); }
      catch (e) { this.toast(`${e.message || e}`); return; }
    } else return;
    ch.applied = true;
    this.render();
  }

  /** "Add box here": a place where the simulated path comes close to the room scan becomes a grey box, which the
   * simulation keeps the arm clear of once the world is saved and simulated again. A box trimmed to keep clear of the
   * path keeps the place it was made for ("covers": with the distance it was fitted to and its revision), so the next
   * check offers that place again only when the path comes closer there; a box the world already holds is never added
   * twice. Editing the box by hand drops its "covers". */
  addScanBox(i) {
    const g = (this.state.result?.checks || []).find((c) => c.id === 'scan')?.regions?.[i], wv = this.wv;
    if (!g?.box || g.added || this.state.busy || !wv.world) return;
    const obs = wv.world.obstacles || (wv.world.obstacles = []);
    const same = (a, b) => a.length === b.length && a.every((v, j) => Math.abs(v - b[j]) < 0.05);
    const twin = obs.find((o) => same(o.center || [], g.box.center) && same(o.half || [], g.box.half)
      && Math.abs((o.yaw_deg || 0) - (g.box.yaw_deg || 0)) < 0.01);
    if (twin) {
      g.added = true;
      this.render();
      return this.toast(`${twin.label} is this box already: Save and simulate to test the path against it`);
    }
    if (obs.length >= 200) return this.toast('This world already has 200 grey boxes, the most it can hold');
    const ids = new Set(obs.map((o) => o.id));
    let id = g.id, k = 2;
    while (ids.has(id)) id = `${g.id}-${k++}`;
    const box = { id, label: g.label, center: [...g.box.center], half: [...g.box.half], yaw_deg: g.box.yaw_deg, source: 'auto' };
    if (g.covers) box.covers = { ...g.covers, center: [...g.covers.center], half: [...g.covers.half] };
    obs.push(box);
    wv._buildBoxes(); wv.changed('boxes');
    g.added = true;
    this.toast(`${g.label} is a grey box now: Save and simulate to test the path against it`);
    this.render();
  }

  _click(e) {
    const b = e.target.closest('[data-a]');
    if (!b || b.disabled) return;
    const a = b.dataset.a, wv = this.wv, arg = b.dataset.arg;
    const act = {
      close: () => this.toggle(false), min: () => { this.state.min = !this.state.min; this.render(); },
      mode: () => wv.setMode(wv.mode === arg ? 'orbit' : arg), rot: () => wv.rotate(+arg),
      scale: () => {
        const n = +(this.el.querySelector('[data-f="len"]')?.value || this.state.len);
        if (!wv.applyScale(n)) return this.toast('Measure two points, then enter their true distance in mm');
        this.state.len = '';
        wv.setMode('orbit');
        this.toast(`Scale set: ${Math.round(n)} mm between the two points`);
      },
      auto: () => this.autoBoxes(), keep: () => wv.keepProposals(arg ? [arg] : null), drop: () => wv.dropProposals(arg ? [arg] : null),
      hlprop: () => wv.highlightProposal(arg),
      surface: () => {
        const s = this.state.surfaceProposal;
        if (!s) return;
        wv.world.surface = { ...s };
        delete wv.world.surface.edge_clipped;
        this.state.surfaceProposal = null;
        wv.setMount([...wv.world.mount.position.slice(0, 2), s.center[2] + s.half[2]]);
        wv._build(); wv._applyMount(false);
      },
      add: () => { wv.setMode('edit'); wv.addBox('Box', (this.doc() || wv.doc)?.targets); this._scrollToSelected(); },
      del: () => wv.deleteBox(arg), sel: () => { wv.setMode('edit'); wv.select(arg); },
      gizmo: () => wv.setGizmo(arg), zone: () => { wv.zoneKind = arg; wv.setMode('zone'); }, delzone: () => wv.deleteZone(arg),
      walk: () => wv.walk(!wv.walking), play: () => { const d = this.doc() || wv.doc; if (d) wv.play(d); else this.toast('No simulation for this revision yet'); },
      save: () => this.save(), off: () => this.turnOff(), check: () => this.check(false), review: () => this.check(true),
      reuse: () => this.useSaved(),
      tour: () => { const r = this.state.result?.review; if (r) wv.tour(r.tour, this.doc() || wv.doc, { source: r.source === 'claude' ? 'Claude (AI)' : 'measured' }); },
      apply: () => this.apply(+arg), subq: () => { this.state.subq = !this.state.subq; this.render(); },
      scanbox: () => this.addScanBox(+arg),
    };
    act[a]?.();
  }

  _scrollToSelected() {
    const it = this.el.querySelector('.wv-item.sel');
    const list = it?.closest('.wv-list');
    if (list) list.scrollTop = Math.max(0, it.offsetTop - list.offsetTop - 4);
  }

  _change(e) {
    const t = e.target, f = t.dataset.f, wv = this.wv;
    if (f === 'starter' && t.value) this.useStarter(t.value);
    if (f === 'file') { this.upload(t.files?.[0]); t.value = ''; }
    if (f === 'label') { const o = wv.world.obstacles.find((x) => x.id === t.dataset.id); if (o) { o.label = t.value.slice(0, 60) || o.label; wv._buildBoxes(); wv.changed('boxes'); } }
    const box = { bx: 'x', by: 'y', bz: 'bottom', bw: 'w', bd: 'd', bh: 'h', bt: 'turn' }[f];
    if (box && t.dataset.id) {
      const v = parseFloat(t.value);
      if (Number.isFinite(v)) wv.setBoxFromBase(t.dataset.id, { [box]: v }); else this.render();
    }
  }

  // one line under the room's name: is the newest revision simulated in it, and how did it go
  _status() {
    const s = this.state, w = this.wv.world, sim = s.sim, p = s.pending;
    if (p) return ['run', p.off ? 'Turning world mode off: the simulation reruns on the plain table…'
      : `Simulating in ${p.name || 'the room'}… only the simulation stage reruns.`];
    if (w?._dirty && w.asset && w.asset_sha && w.asset_sha !== s.saved?.world?.asset_sha) {     // a scan just uploaded
      return ['', `${w.name}: a new room, not simulated yet. Place the robot, then Save and simulate.`
        + (this.on && sim?.world ? ` (${sim.rev} simulates in ${s.saved?.world?.name || sim.world}.)` : '')];
    }
    if (!this.on) return ['', 'World mode is off: the robot is simulated on the plain table.'];
    if (!sim || !sim.world) return ['', 'Not simulated in this world yet: Save and simulate.'];
    const same = sim.world === s.active?.id && (!s.saved || sim.world_sha === s.saved.sha);
    let t = `${sim.rev} in ${s.saved?.world?.name || sim.world}: pick and place ${sim.success ? 'succeeds' : 'fails'}`;
    if (!sim.success && sim.failure) t += ` (${sim.failure})`;
    if (sim.min_clearance_mm != null) t += `; closest ${Math.round(sim.min_clearance_mm)} mm, to ${sim.min_clearance_at?.obstacle || 'a grey box'}`;
    else t += '; no grey boxes to keep clear of';
    const arc = sim.arc, back = sim.detours?.retreat, there = sim.detours?.approach, rel = sim.detours?.release;
    const ways = [];
    if (arc && arc.clear && arc.usual === false) ways.push(`the carry path pulled in ${pct(arc.inward)}${arc.bump_mm ? `, raised ${Math.round(arc.bump_mm)} mm` : ''} to keep clear`);
    if (there?.via === 'from above') ways.push(`the approach comes in from ${Math.round(there.rise_mm)} mm higher`);
    else if (there?.via && there.via !== 'direct') ways.push('the approach goes along the carry path');
    if (rel?.via === 'opened less') ways.push(`the hand opens only ${Math.round(rel.open_mm)} mm past the object to let go`);
    if (back?.via === 'direct, hand closed') ways.push('the hand closes before going back to rest');
    else if (back?.via === 'straight up first') ways.push(`it rises ${Math.round(back.rise_mm)} mm before going back to rest`);
    else if (back?.via && back.via !== 'direct') ways.push('it goes back to rest along the carry path');
    const said = ways.join('; ');
    t += said ? `. ${said[0].toUpperCase()}${said.slice(1)}.` : '.';
    const tight = sim.success && sim.clearance_ok === false && !(sim.world_contacts || []).length;
    if (tight) t += ' Under 10 mm: check that spot in the real room.';
    if (!same) t += ' The saved room changed since: Save and simulate.';
    if (w?._dirty) t += ' Unsaved changes: Save and simulate to test them.';
    return [sim.success && same && !tight ? 'ok' : (sim.success ? '' : 'bad'), t];
  }

  render() {
    document.body.classList.toggle('wv-open', !!(this.open && !this.state.min && !this.away));
    if (!this.open) return;
    const f = document.activeElement;
    if (f && f !== document.body && this.el.contains(f) && f.matches('input:not([type]), input[type="text"], input[type="number"]')) {
      this._later = true;
      return;
    }
    const wv = this.wv, w = wv.world, s = this.state, N = s.notes || NOTES, busy = !!s.busy;
    this.el.classList.toggle('min', !!s.min);
    const B = (a, label, { arg = '', on = false, primary = false, dis = false, title = '', any = false } = {}) =>
      `<button type="button" class="wv-btn${on ? ' on' : ''}${primary ? ' primary' : ''}" data-a="${a}" data-arg="${esc(arg)}" ${dis || (busy && !any) ? 'disabled' : ''} title="${esc(title)}">${label}</button>`;
    const busyIn = (sec) => (s.busy?.sec === sec ? `<div class="wv-note warn">${esc(s.busy.text)}</div>` : '');
    const [cls, line] = this._status();
    let h = `<h3>World <span class="wv-beta">beta</span><button type="button" class="wv-x wv-min" data-a="min" title="${s.min ? 'Show the panel' : 'Fold the panel away'}">${s.min ? '+' : '&minus;'}</button><button type="button" class="wv-x" data-a="close" title="Close">&times;</button></h3>
      <div class="wv-note">${esc(N.world)}</div>
      <div class="wv-sec"><b>Room</b><div class="wv-row"><select class="wv-sel" data-f="starter" ${busy ? 'disabled' : ''}><option value="">Start from…</option>
      ${(s.starters || []).map((x) => `<option value="${esc(x.id)}" title="${esc(x.description)}">${esc(x.name)}</option>`).join('')}</select>
      <label class="wv-btn" title="A room scan as a single .glb file (y up, metres), up to 300 MB">Upload .glb<input type="file" accept=".glb,model/gltf-binary" data-f="file" hidden ${busy ? 'disabled' : ''}></label></div>
      ${w ? `<div class="wv-note">${esc(w.name)}${w.asset ? ` · scan, ${esc(w.units)}, scale ${(+w.scale).toFixed(3)}` : ' · built from boxes'}${s.problem ? ` · ${esc(s.problem)}` : ''}</div>`
        : `<div class="wv-note">${esc(N.release)}</div>`}
      <div class="wv-status ${cls}">${esc(line)}</div>
      ${!this.on && !w && s.saved ? `<div class="wv-row">${B('reuse', `Use ${esc(s.saved.world.name)} again`)}</div>` : ''}${busyIn(SEC.room)}</div>`;
    if (w) {
      const m = w.mount || { position: [0, 0, 0], yaw_deg: 0 };
      const clash = w.surface ? wv.baseClash() : null;
      h += `<div class="wv-sec"><b>Robot placement</b><div class="wv-row">${B('mode', 'Place', { arg: 'place', on: wv.mode === 'place', title: 'Click a level surface' })}
        ${B('rot', '-15°', { arg: -15, dis: !w.surface })}${B('rot', '+15°', { arg: 15, dis: !w.surface })}<span class="wv-note" style="margin:0">yaw ${Math.round(m.yaw_deg)}°</span></div>
        ${!w.surface ? '<div class="wv-note warn">Not placed yet: Place, then click a level surface (a table, counter or shelf top).</div>' : ''}
        ${clash ? `<div class="wv-note warn">The base stands in ${esc(clash)}: the simulation would start in contact. Place it elsewhere or move that box.</div>` : ''}
        ${w.asset ? `<div class="wv-row">${B('mode', 'Scale: 2 points', { arg: 'scale', on: wv.mode === 'scale' })}<input class="wv-in" data-f="len" type="number" min="1" placeholder="true mm" value="${esc(s.len || '')}" aria-label="True distance in mm">${B('scale', 'Apply', { dis: !wv.scaleRef })}</div>
        ${wv.scaleRef ? `<div class="wv-note">Measured ${Math.round(wv.scaleRef.measured)} mm between the two points: type their real distance.</div>` : ''}
        ${w.scale_ref ? `<div class="wv-note">Scale set from ${Math.round(w.scale_ref.length_mm)} mm between two points.</div>` : ''}` : ''}
        ${s.surfaceProposal ? `<div class="wv-row">${B('surface', `Use the scanned surface (${Math.round(s.surfaceProposal.half[0] * 2)} × ${Math.round(s.surfaceProposal.half[1] * 2)} mm)`)}</div>` : ''}${busyIn(SEC.place)}</div>`;
      const obs = w.obstacles || [];
      const sel = obs.find((o) => o.id === wv.selected);
      h += `<div class="wv-sec"><b>Grey boxes (what the simulation sees) · ${obs.length}</b><div class="wv-row">
        ${w.asset ? B('auto', 'Find boxes in the scan', { dis: !w.surface, title: w.surface ? '' : 'Place the robot first' }) : ''}${B('add', 'Add box', { dis: !w.surface })}${B('mode', 'Edit', { arg: 'edit', on: wv.mode === 'edit' })}
        ${wv.mode === 'edit' && sel ? `${B('gizmo', 'Move', { arg: 'translate' })}${B('gizmo', 'Turn', { arg: 'rotate' })}${B('gizmo', 'Size', { arg: 'scale' })}` : ''}</div>
        ${wv.proposals.length ? `<div class="wv-row"><span class="wv-note" style="margin:0;flex:1">${wv.proposals.length} proposed (amber): keep the ones that match something real</span>${B('keep', 'Keep all', { primary: true })}${B('drop', 'Drop all')}</div>
          <div class="wv-list">${wv.proposals.map((o) => `<div class="wv-item prop${wv.hlProposal === o.id ? ' hl' : ''}"><i></i><span data-a="hlprop" data-arg="${esc(o.id)}" title="Show it">${esc(o.label)}</span>
            <small>${o.half.map((x) => Math.round(2 * x)).join('×')}</small>${B('keep', 'Keep', { arg: o.id })}<button type="button" class="wv-x" data-a="drop" data-arg="${esc(o.id)}" title="Drop" ${busy ? 'disabled' : ''}>&times;</button></div>`).join('')}</div>` : ''}
        <div class="wv-list">${obs.map((o) => `<div class="wv-item${wv.selected === o.id ? ' sel' : ''}"><i style="background:${o.source === 'auto' ? C.auto : C.box}"></i>
          ${wv.selected === o.id ? `<input class="wv-in" style="flex:1;width:auto" data-f="label" data-id="${esc(o.id)}" value="${esc(o.label)}" aria-label="Box label">` : `<span data-a="sel" data-arg="${esc(o.id)}">${esc(o.label)}</span>`}
          <small>${o.half.map((x) => Math.round(2 * x)).join('×')}</small><button type="button" class="wv-x" data-a="del" data-arg="${esc(o.id)}" title="Delete" ${busy ? 'disabled' : ''}>&times;</button></div>`).join('')}</div>
        ${sel ? this._editor(sel) : ''}${busyIn(SEC.boxes)}</div>`;
      const zs = w.zones || [];
      h += `<div class="wv-sec"><b>People areas · ${zs.length}</b><div class="wv-row">${B('zone', 'Add walkway', { arg: 'walkway', on: wv.mode === 'zone' && wv.zoneKind === 'walkway' })}
        ${B('zone', 'Add seat', { arg: 'seat', on: wv.mode === 'zone' && wv.zoneKind === 'seat' })}</div>
        <div class="wv-list">${zs.map((z) => `<div class="wv-item"><i style="background:${C[z.kind]}"></i><span>${esc(z.label)}</span><small>${esc(z.kind)}</small>
          <button type="button" class="wv-x" data-a="delzone" data-arg="${esc(z.id)}" title="Delete" ${busy ? 'disabled' : ''}>&times;</button></div>`).join('')}</div></div>`;
      h += `<div class="wv-sec"><b>Simulate</b><div class="wv-row">${B('save', w._dirty ? 'Save and simulate' : 'Save world', { primary: !!w._dirty, dis: !w.surface || !!s.pending })}${B('play', 'Play in the room')}
        ${B('walk', wv.walking ? 'Stop walking' : 'Walk', { on: !!wv.walking, title: 'Walk through the room: WASD and the mouse', any: !!wv.walking })}${B('off', 'World off', { title: 'Simulate on the plain table again', dis: !this.on })}</div>
        ${wv.octreeNote ? `<div class="wv-note">${esc(wv.octreeNote)}</div>` : ''}${busyIn(SEC.sim)}</div>`;
      h += this._checkSection(B, busyIn);
    }
    this.el.innerHTML = h;
  }

  _editor(o) {
    const b = this.wv.boxInBase(o), id = esc(o.id);
    const f = (k, label, v, title = '') => `<label title="${esc(title)}">${label}<input class="wv-in" type="number" step="5" data-f="${k}" data-id="${id}" value="${v}"></label>`;
    return `<div class="wv-edit"><span class="wide">${esc(o.label)}: millimetres from the robot's base (forward, left), bottom above the surface</span>
      ${f('bx', 'Forward', b.x)}${f('by', 'Left', b.y)}${f('bz', 'Bottom', b.bottom, 'Height of its underside above the surface the robot stands on')}${f('bt', 'Turn °', b.turn)}
      ${f('bw', 'Width', b.w, 'Along its own x')}${f('bd', 'Depth', b.d, 'Along its own y')}${f('bh', 'Height', b.h)}</div>`;
  }

  _checkSection(B, busyIn) {
    const s = this.state, w = this.wv.world, N = s.notes || NOTES, r = s.result;
    const blocked = w?._dirty ? 'Save and simulate first: the check uses the saved world\'s simulation.'
      : s.pending ? 'Wait for the simulation in this world to finish.' : '';
    const noAi = !s.ai.claude ? 'AI review needs an Anthropic key (Keys, top right) and network lookups on; without one it shows the measured review.' : '';
    let h = `<div class="wv-sec"><b>Check</b><div class="wv-row">${B('check', 'Check placement', { dis: !!blocked })}${B('review', 'AI review', { dis: !!blocked, title: s.ai.claude ? 'Sends 6 pictures to Claude' : noAi })}
      <label class="wv-note" style="margin:0;display:inline-flex;gap:4px;align-items:center" title="${s.ai.subq ? 'SubQ Snap answers yes/no questions about 4 of the views' : 'Needs a SubQ key (Keys)'}"><input type="checkbox" data-a="subq" ${s.subq ? 'checked' : ''} ${s.ai.subq ? '' : 'disabled'}>also SubQ</label></div>
      ${blocked ? `<div class="wv-note warn">${esc(blocked)}</div>` : ''}
      <div class="wv-note">${esc(s.ai.claude ? N.review : noAi)}</div>${s.subq && s.ai.subq ? `<div class="wv-note">${esc(N.subq || '')}</div>` : ''}${busyIn(SEC.check)}`;
    if (r) {
      const rv = r.review || {};
      h += `<div class="wv-row"><span class="wv-verdict ${esc(r.verdict)}">${esc(r.verdict.replace('_', ' '))}</span><span class="wv-note" style="margin:0">${esc(r.rev)}, measured by geometry and the simulation</span></div>
        ${r.stale ? '<div class="wv-note warn">A newer revision is in: check again.</div>' : ''}
        ${rv.skipped ? `<div class="wv-note warn">${esc(rv.skipped)}</div>` : ''}${rv.note ? `<div class="wv-note warn">${esc(rv.note)}</div>` : ''}${rv.error ? `<div class="wv-note warn">${esc(rv.error)}</div>` : ''}
        ${r.asked?.subq && !r.subq ? '<div class="wv-note warn">SubQ didn\'t answer (no SubQ key, it is paused, or it declined the pictures): only the checks above.</div>' : ''}
        <div style="margin-top:6px">${(r.checks || []).map((c) => `<div class="wv-check"><span class="st ${esc(c.status)}">${esc(c.status)}</span><span>${esc(c.text)}</span></div>`
          + (c.id === 'scan' ? (c.regions || []).map((g, i) => `<div class="wv-row wv-scan"><span class="wv-note" style="margin:0;flex:1">${esc(g.label)}: ${g.min_clearance_mm < 0.5 ? 'touches' : `${(+g.min_clearance_mm).toFixed(1)} mm from`} the ${esc(g.link === 'object' ? 'object' : String(g.link || 'arm').replace(/_/g, ' '))} at ${(+g.t).toFixed(1)} s; ${g.box ? `box ${g.box.half.map((x) => Math.round(2 * x)).join(' × ')} mm${g.box_clearance_mm != null ? `, ${(+g.box_clearance_mm).toFixed(1)} mm from the path` : ''}${g.trimmed ? ' (trimmed to keep clear of it)' : ''}` : esc(g.no_box || 'no box')}</span>${g.box ? B('scanbox', g.added ? 'Added' : 'Add box here', { arg: i, dis: !!g.added, title: 'A grey box over this part of the scan, no closer to the path than the scan: the simulation keeps the arm clear of it' }) : ''}</div>`).join('') : '')).join('')}</div>
        ${rv.summary && rv.source === 'claude' ? `<div class="wv-note">${esc(SRC.claude)}: ${esc(rv.summary)}</div>` : ''}
        ${(rv.findings || []).length ? `<div style="margin-top:7px">${rv.findings.map((f) => `<div class="wv-find ${esc(f.severity)}"><span class="src">${esc(SRC[f.source] || f.source)} · ${esc(f.category)} · ${esc(VIEW_TITLE[f.viewpoint] || f.viewpoint)}</span>
          <div>${esc(f.evidence)}</div><div class="wv-note" style="margin-top:2px">${esc(f.recommendation)}</div></div>`).join('')}</div>` : ''}
        ${(rv.suggested_changes || []).map((c, i) => `<div class="wv-row"><span class="wv-note" style="margin:0;flex:1"><b class="src">${esc(SRC[c.source] || c.source)}</b> ${esc(c.kind.replace('_', ' '))}: ${esc(c.reason)}</span>${B('apply', c.applied ? 'Applied' : 'Apply', { arg: i, dis: !!c.applied })}</div>`).join('')}
        <div class="wv-row">${B('tour', 'Guided tour', { primary: true, dis: !(rv.tour || []).length })}<span class="wv-note" style="margin:0">${esc(SRC[rv.source] || '')}</span></div>`;
    }
    return h + '</div>';
  }
}

// ================================================================== Studio wiring (feature contract, section 4)
async function resolveViewer(studio) {
  const pick = () => studio.viewer || window.morph?.viewer;
  for (let i = 0; i < 100 && !pick(); i++) await sleep(100);
  const a = pick();
  if (!a) return null;
  if (a.robotFrame) return a;
  if (a.ready) { try { await a.ready; } catch (e) { return null; } }
  return a.v && a.v.robotFrame ? a.v : null;
}

/** World mode in the Studio: a World button in the viewer tools, the panel over the viewer, the room following the
 * project and its revisions, and `world` events (contracts sections 3 and 4). */
export async function init(studio) {
  const viewer = await resolveViewer(studio);
  if (!viewer) return;          // no WebGL (the fallback preview): world mode needs the 3D viewer
  const api = {
    get: (p, q) => studio.api.get(p, q), post: (p, b) => studio.api.post(p, b), put: (p, blob, q) => studio.api.put(p, blob, q),
  };
  const wv = new WorldView(viewer);
  const simDoc = async () => {
    const url = studio.S?.current?.sims?.pick_place?.url;
    if (!url) return null;
    if (wv._docUrl === url && wv._docCache) return wv._docCache;
    try { const r = await fetch(url); wv._docCache = r.ok ? await r.json() : null; wv._docUrl = url; } catch (e) { wv._docCache = null; }
    return wv._docCache;
  };
  const btn = el('button', 'btn', 'World <small style="opacity:.6">beta</small>');
  btn.type = 'button';
  btn.title = 'World mode (beta): put the robot in a room';
  const panel = new WorldPanel(viewer.container, { wv, api, toast: studio.toast, doc: () => wv._docCache, project: studio.project,
    queue: () => studio.S?.queue || [], onToggle: (open) => btn.classList.toggle('on', open) });
  btn.onclick = async () => { await panel.toggle(); if (panel.open) await simDoc(); };
  (studio.mount?.viewerTools || studio.mount?.header)?.appendChild(btn);
  // a revision whose design names a world shows its room (the Simulation tab then plays in it), panel open or not
  const sync = async () => {
    const id = studio.S?.current?.design?.['world.id'], p = panel.state.pending;
    if (!panel.open && !(p ? !p.off : id)) { if (wv.world) wv.clear({ reframe: true }); return; }
    await simDoc();
    if (panel.open) return panel.refresh();
    if (id && (!wv.world || (wv.world.id !== id && !wv.world._dirty))) {
      try { const r = await api.get('/api/world'); if (r.world) await wv.setWorld(r.world, { assetUrl: r.asset_url, frame: false }); } catch (e) { /* offline */ }
    }
  };
  studio.on('done', sync);
  studio.on('tab', (ev) => panel.setAway(ev.tab !== 'robot'));
  if (studio.S?.tab && studio.S.tab !== 'robot') panel.setAway(true);
  studio.on('project', () => { wv.clear(); panel.reset(); sync(); });
  studio.on('queue', (ev) => {
    for (const j of ev.jobs || []) if (j.position == null && panel.jobEnded(j) && panel.open) panel.refresh();
  });
  sync();
  studio.on('world', (ev) => { if (panel.open && ev.status === 'error') studio.toast(ev.text); });
  window.morphWorld = { wv, panel };
  return { wv, panel };
}

export default init;

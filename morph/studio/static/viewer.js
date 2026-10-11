// Morph Studio: RobotViewer, a realistic, animated 3D viewer for a generated robot.
//
//   import { RobotViewer } from './viewer.js';
//   const v = new RobotViewer(document.getElementById('cad3d'));
//   await v.load('/files/r003/robot/robot.json');     // robot.json + its GLB (json.glb, same directory)
//   v.playMotionDemo();  v.setExplode(0.7);  v.showStep(3);  v.setXray(true);  v.setLabels(true);
//   v.playTrajectory(simJson, { loop: true });  v.showWorkspace(points);  v.setGhost(prevJsonUrl);
//
// Data contract: docs/PLATFORM_CONTRACTS.md (robot.json from Assembly.to_robot_json, GLB from
// Assembly.export_glb: nodes link__<name> carry the joint origin, parts are mesh children part__<name>,
// every part mesh is in its link frame, z-up, mm). The robot base frame is mapped into the y-up scene by
// one rotation, so everything robot-related (overlays, trajectories, workspace points) is expressed in
// robot base coordinates (mm, z-up).
//
// three.js r160 is vendored next to this file; no network access is needed.

import * as THREE from './vendor/three.module.min.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';
import { OrbitControls } from './vendor/OrbitControls.js';
import { RoomEnvironment } from './vendor/RoomEnvironment.js';
import { CSS2DRenderer, CSS2DObject } from './vendor/CSS2DRenderer.js';
import { LineSegments2 } from './vendor/LineSegments2.js';
import { LineSegmentsGeometry } from './vendor/LineSegmentsGeometry.js';
import { LineMaterial } from './vendor/LineMaterial.js';
import { toCreasedNormals } from './vendor/BufferGeometryUtils.js';

// ------------------------------------------------------------------ small helpers
const DEG = Math.PI / 180;
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const lerp = (a, b, t) => a + (b - a) * t;
const damp = (cur, target, rate, dt) => target + (cur - target) * Math.exp(-rate * dt);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const Ease = {
  linear: (t) => t,
  outCubic: (t) => 1 - Math.pow(1 - t, 3),
  outQuart: (t) => 1 - Math.pow(1 - t, 4),
  inOutCubic: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  inOutSine: (t) => -(Math.cos(Math.PI * t) - 1) / 2,
  outBack: (t) => { const c1 = 1.2, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); },
};

const ACCENT = '#4aa3ff';
const HOT = '#ff9a3c';
const GOOD = '#3ddc97';
const XRAY_KINDS = new Set(['structural', 'cover', 'visual']);
const ELEC_KINDS = new Set(['electronics', 'pcb', 'sensor']);
const THEMES = {
  dark: { bgInner: '#1c232c', bgOuter: '#0a0d11', floor: '#151b22', grid: '#1d252e', gridMajor: '#27323d',
    shadow: 0.38, contact: 0.6, ghost: '#7cc4ff', exposure: 1.05 },
  light: { bgInner: '#fdfdfe', bgOuter: '#dfe4ea', floor: '#eef1f5', grid: '#dce2e9', gridMajor: '#c6cfd9',
    shadow: 0.24, contact: 0.32, ghost: '#2f7fd6', exposure: 1.0 },
};

const WORDS = { devkit: 'DevKit', pcb: 'PCB', psu: 'PSU', usb: 'USB', imu: 'IMU', fsr: 'FSR', ttl: 'TTL', pwm: 'PWM',
  mcu: 'MCU', tcp: 'TCP', bldc: 'BLDC', can: 'CAN', esc: 'ESC', led: 'LED', adc: 'ADC', i2c: 'I2C', uart: 'UART' };
function pretty(s) {
  return String(s || '').split(/[_\-\s]+/).filter(Boolean).map((w) => {
    const k = w.toLowerCase();
    if (WORDS[k]) return WORDS[k];
    if (/\d/.test(w) || w.length <= 2) return w.toUpperCase();
    return w[0].toUpperCase() + w.slice(1);
  }).join(' ');
}
function prettyJoint(name) {
  const m = /^j(\d+)_(.+)$/i.exec(name || '');
  if (m) { const rest = pretty(m[2]); return `J${m[1]} · ${rest[0]}${rest.slice(1).toLowerCase()}`; }
  return pretty(name);
}
const fmtDeg = (r) => `${Math.round(r / DEG)}°`;

function resolveUrl(u, base) {
  try { return new URL(u, base || location.href).href; } catch (e) { return u; }
}

let cssInjected = false;
function injectCSS() {
  if (cssInjected || typeof document === 'undefined') return;
  cssInjected = true;
  const s = document.createElement('style');
  s.id = 'morph-viewer-css';
  s.textContent = `
.mv-root{position:relative;overflow:hidden;isolation:isolate}
.mv-canvas{position:absolute;inset:0;display:block;width:100%!important;height:100%!important;outline:none;touch-action:none}
.mv-labels{position:absolute;inset:0;pointer-events:none;z-index:2}
.mv-label{width:0;height:0;overflow:visible;font:500 11px/1.25 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;--mv-c:${ACCENT}}
.mv-label>.mv-in{position:absolute;left:0;top:0;opacity:0;transition:opacity .35s ease}
.mv-label.on>.mv-in{opacity:1}
.mv-dot{position:absolute;left:-4px;top:-4px;width:8px;height:8px;border-radius:50%;background:var(--mv-c);box-shadow:0 0 0 3px rgba(10,13,17,.55),0 0 10px var(--mv-c)}
.mv-lead{position:absolute;left:0;top:0;height:1px;transform-origin:0 50%;background:linear-gradient(90deg,var(--mv-c),rgba(200,214,229,.55))}
.mv-pill{position:absolute;left:0;top:0;white-space:nowrap;padding:5px 10px 5px 9px;border-radius:7px;background:rgba(13,17,22,.84);
  border:1px solid rgba(255,255,255,.08);border-left:2px solid var(--mv-c);box-shadow:0 8px 22px rgba(0,0,0,.35);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);color:#e8edf2}
.mv-pill b{display:block;font-weight:600;font-size:11.5px;letter-spacing:.1px}
.mv-pill span{display:block;font:500 10px/1.35 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#8b98a5;margin-top:1px}
.mv-tag{font:650 9.5px/1 Inter,system-ui,sans-serif;letter-spacing:.9px;text-transform:uppercase;padding:4px 7px 4px;border-radius:5px;
  color:#0b0e12;background:var(--mv-c,${ACCENT});box-shadow:0 4px 14px rgba(0,0,0,.35);white-space:nowrap;pointer-events:none;margin-top:-22px}
.mv-tag.ghosty{background:rgba(13,17,22,.82);color:#cfe3f7;border:1px solid rgba(124,196,255,.35)}
.mv-hud{position:absolute;left:14px;bottom:14px;z-index:3;display:flex;align-items:center;gap:9px;padding:7px 12px 7px 11px;border-radius:10px;
  background:rgba(13,17,22,.8);border:1px solid rgba(255,255,255,.08);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);color:#e8edf2;
  font:500 12px/1.2 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;opacity:0;transform:translateY(8px);transition:opacity .35s ease,transform .35s ease;pointer-events:none;max-width:calc(100% - 28px)}
.mv-hud.on{opacity:1;transform:none}
.mv-hud i{flex:none;width:7px;height:7px;border-radius:50%;background:var(--mv-c,${ACCENT});box-shadow:0 0 10px var(--mv-c,${ACCENT});animation:mvpulse 1.6s ease-in-out infinite}
.mv-hud b{font-weight:600;white-space:nowrap}
.mv-hud span{color:#8b98a5;font:500 11px/1.2 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@keyframes mvpulse{50%{opacity:.35}}
.mv-light .mv-pill,.mv-light .mv-hud{background:rgba(255,255,255,.9);color:#18212b;border-color:rgba(10,20,30,.1);box-shadow:0 8px 22px rgba(30,40,60,.12)}
.mv-light .mv-dot{box-shadow:0 0 0 3px rgba(255,255,255,.7),0 0 8px var(--mv-c)}
.mv-light .mv-pill span,.mv-light .mv-hud span{color:#5d6b79}
`;
  document.head.appendChild(s);
}

function disposeTree(obj) {
  obj.traverse((o) => {
    if (o.isCSS2DObject && o.element && o.element.parentNode) o.element.parentNode.removeChild(o.element);
    if (o.geometry) o.geometry.dispose();
    if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => { if (m.map) m.map.dispose(); m.dispose(); });
  });
  if (obj.parent) obj.parent.remove(obj);
}

function gradientTexture(inner, outer) {
  const c = document.createElement('canvas');
  c.width = c.height = 512;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(256, 210, 10, 256, 256, 400);
  grd.addColorStop(0, inner);
  grd.addColorStop(1, outer);
  g.fillStyle = grd;
  g.fillRect(0, 0, 512, 512);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function blobTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 256;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(128, 128, 0, 128, 128, 128);
  grd.addColorStop(0, 'rgba(0,0,0,1)');
  grd.addColorStop(0.35, 'rgba(0,0,0,0.55)');
  grd.addColorStop(0.7, 'rgba(0,0,0,0.12)');
  grd.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, 256, 256);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

// colour ramp for heights (deep blue -> cyan -> green -> amber)
const RAMP = [[0, '#2f5bd3'], [0.35, '#27b3d9'], [0.65, '#6fdc8c'], [1, '#ffb347']].map(([t, c]) => [t, new THREE.Color(c)]);
function ramp(t, out) {
  t = clamp(t, 0, 1);
  for (let i = 1; i < RAMP.length; i++) {
    if (t <= RAMP[i][0]) { const [t0, c0] = RAMP[i - 1], [t1, c1] = RAMP[i]; return out.lerpColors(c0, c1, (t - t0) / (t1 - t0)); }
  }
  return out.copy(RAMP[RAMP.length - 1][1]);
}

// patch a MeshStandard/Physical material with a fresnel glow channel (highlight) and an x-ray fresnel alpha
function patchMaterial(mat, u) {
  mat.onBeforeCompile = (sh) => {
    sh.uniforms.uGlow = u.uGlow;
    sh.uniforms.uGlowColor = u.uGlowColor;
    sh.uniforms.uFres = u.uFres;
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', '#include <common>\nuniform float uGlow;\nuniform vec3 uGlowColor;\nuniform float uFres;')
      .replace('#include <emissivemap_fragment>', `#include <emissivemap_fragment>
      {
        float mvFr = 1.0 - abs(dot(normalize(vViewPosition), normal));
        totalEmissiveRadiance += uGlowColor * uGlow * (0.22 + 1.5 * pow(mvFr, 2.2));
        diffuseColor.a = clamp(diffuseColor.a + uFres * pow(mvFr, 2.4), 0.0, 1.0);
      }`);
  };
  mat.customProgramCacheKey = () => 'morph-part-v1';
}

const _m1 = new THREE.Matrix4(), _m2 = new THREE.Matrix4(), _m3 = new THREE.Matrix4();
const _v1 = new THREE.Vector3(), _v2 = new THREE.Vector3(), _v3 = new THREE.Vector3();
const _q1 = new THREE.Quaternion(), _q2 = new THREE.Quaternion();
const _box = new THREE.Box3(), _box2 = new THREE.Box3();
const _c1 = new THREE.Color();
const Z = new THREE.Vector3(0, 0, 1);

// ================================================================== RobotModel (internal)
// A loaded robot: GLB node graph + robot.json kinematics. Used for the main robot and for the ghost.
class RobotModel {
  constructor(json, gltf, { ghost = false, ghostColor = '#7cc4ff', edges = true } = {}) {
    this.json = json;
    this.ghost = ghost;
    this.fade = 0;
    this.root = new THREE.Group();
    this.root.name = `robot:${json.name || 'robot'}`;
    const scene = gltf.scene;
    this.root.add(scene);
    const nodes = new Map();
    scene.traverse((o) => { if (o.name && !nodes.has(o.name)) nodes.set(o.name, o); });
    this.rootLink = json.root || (json.links && json.links[0] && json.links[0].name) || 'base';

    // ---- links + joints
    this.links = new Map();
    for (const L of json.links || []) { const n = nodes.get(`link__${L.name}`); if (n) this.links.set(L.name, n); }
    if (!this.links.has(this.rootLink)) { const g = new THREE.Group(); g.name = `link__${this.rootLink}`; scene.add(g); this.links.set(this.rootLink, g); }
    this.joints = [];
    this.jointMap = new Map();
    const byParent = new Map();
    for (const jd of json.joints || []) {
      const origin = new THREE.Matrix4();
      if (Array.isArray(jd.origin) && jd.origin.length === 16) origin.set(...jd.origin);
      const axis = new THREE.Vector3(...(jd.axis || [0, 0, 1]));
      if (axis.lengthSq() < 1e-12) axis.set(0, 0, 1);
      axis.normalize();
      const j = { name: jd.name, type: jd.type, parent: jd.parent, child: jd.child, origin, axis,
        lower: Number.isFinite(jd.lower) ? jd.lower : -Math.PI, upper: Number.isFinite(jd.upper) ? jd.upper : Math.PI,
        home: Number.isFinite(jd.home) ? jd.home : 0, mimic: jd.mimic || null, actuator: jd.actuator || '', effort: jd.effort,
        info: jd, chainDir: new THREE.Vector3(), chainMag: 0 };
      this.joints.push(j);
      this.jointMap.set(j.name, j);
      if (!byParent.has(j.parent)) byParent.set(j.parent, []);
      byParent.get(j.parent).push(j);
    }
    // BFS order from the root so parents are always posed before children
    this.order = [];
    const seen = new Set([this.rootLink]);
    const frontier = [this.rootLink];
    while (frontier.length) {
      const l = frontier.shift();
      for (const j of byParent.get(l) || []) {
        if (seen.has(j.child)) continue;
        seen.add(j.child);
        this.order.push(j);
        frontier.push(j.child);
      }
    }
    for (const j of this.order) {
      let node = this.links.get(j.child);
      const parentNode = this.links.get(j.parent);
      if (!node) {
        node = new THREE.Group();
        node.name = `link__${j.child}`;
        this.links.set(j.child, node);
      }
      if (parentNode && node.parent !== parentNode) parentNode.add(node);
      node.matrixAutoUpdate = false;
      node.matrix.copy(j.origin);
      j.node = node;
    }
    this.children = byParent;

    // ---- parts
    this.parts = [];
    this.partMap = new Map();
    const geoCache = new Map();
    const ghostFill = ghost ? new THREE.MeshBasicMaterial({ color: ghostColor, transparent: true, opacity: 0.045, depthWrite: false, toneMapped: false }) : null;
    const ghostEdge = ghost ? new THREE.LineBasicMaterial({ color: ghostColor, transparent: true, opacity: 0.5, depthWrite: false, toneMapped: false }) : null;
    this.ghostMats = ghost ? [ghostFill, ghostEdge] : [];
    for (const p of json.parts || []) {
      const node = nodes.get(`part__${p.name}`);
      if (!node) continue;
      const rec = { name: p.name, info: p, kind: p.kind || 'structural', link: p.link, node, model: this,
        meshes: [], mats: [], edges: [], edgeMats: [], basePos: node.position.clone(),
        bbox: new THREE.Box3(), center: new THREE.Vector3(), diag: 0,
        explode: new THREE.Vector3(...(p.explode || [0, 0, 0])), fly: new THREE.Vector3(),
        stepOffset: new THREE.Vector3(), alphaStep: 1, hidden: false,
        hl: 0, stepGlow: 0, motionGlow: 0, forceGlow: 0, glow: 0, glowColor: new THREE.Color(HOT), op: -1,
        u: { uGlow: { value: 0 }, uGlowColor: { value: new THREE.Color(HOT) }, uFres: { value: 0 } } };
      node.matrixAutoUpdate = true;
      const meshes = [];
      node.traverse((o) => { if (o.isMesh) meshes.push(o); });
      for (const mesh of meshes) {
        const g0 = mesh.geometry;
        if (!g0.boundingBox) g0.computeBoundingBox();
        _box.copy(g0.boundingBox);
        if (mesh !== node) { mesh.updateMatrix(); _box.applyMatrix4(mesh.matrix); }
        rec.bbox.union(_box);
        let cached = geoCache.get(g0.uuid);
        if (!cached) {
          const nTri = (g0.index ? g0.index.count : g0.attributes.position.count) / 3;
          const tiny = g0.boundingBox.getSize(_v1).length() < 7;
          const wantEdges = edges && rec.kind !== 'fastener' && !tiny && nTri < 150000;
          let eg = null;
          if (wantEdges) eg = new THREE.EdgesGeometry(g0, ghost ? 25 : 30);
          let g1 = g0;
          try { g1 = toCreasedNormals(g0, 32 * DEG); } catch (e) { g0.computeVertexNormals(); }
          g1.computeBoundingBox();
          g1.computeBoundingSphere();
          cached = { geo: g1, eg };
          geoCache.set(g0.uuid, cached);
          if (g1 !== g0) g0.dispose();
        }
        mesh.geometry = cached.geo;
        if (ghost) {
          mesh.material = ghostFill;
          mesh.castShadow = mesh.receiveShadow = false;
          mesh.raycast = () => {};
          if (cached.eg) { const ln = new THREE.LineSegments(cached.eg, ghostEdge); ln.raycast = () => {}; mesh.add(ln); }
        } else {
          const src = Array.isArray(mesh.material) ? mesh.material[0] : mesh.material;
          const mat = src.clone();
          mat.flatShading = false;
          mat.envMapIntensity = 1.0;
          mat.polygonOffset = true;
          mat.polygonOffsetFactor = 1;
          mat.polygonOffsetUnits = 1;
          mat.side = THREE.FrontSide;
          if (p.color && !src.map) mat.color.set(p.color);
          patchMaterial(mat, rec.u);
          mesh.material = mat;
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          mesh.userData.part = rec;
          rec.mats.push(mat);
          if (cached.eg) {
            const lum = mat.color.r * 0.2126 + mat.color.g * 0.7152 + mat.color.b * 0.0722;
            const dark = lum < 0.18;
            const em = new THREE.LineBasicMaterial({ color: dark ? '#9fb1c4' : '#0c1117', transparent: true,
              opacity: dark ? 0.22 : 0.32, depthWrite: false });
            em.userData.base = { color: em.color.clone(), opacity: em.opacity };
            const ln = new THREE.LineSegments(cached.eg, em);
            ln.raycast = () => {};
            mesh.add(ln);
            rec.edges.push(ln);
            rec.edgeMats.push(em);
          }
          const sm = src;
          if (sm && !this._srcMats) this._srcMats = new Set();
          this._srcMats.add(sm);
        }
        meshes.length && rec.meshes.push(mesh);
      }
      rec.bbox.getCenter(rec.center).add(rec.basePos);
      rec.diag = rec.bbox.getSize(_v1).length();
      this.parts.push(rec);
      this.partMap.set(rec.name, rec);
    }
    // the GLB's own materials were cloned per part; free the originals
    if (this._srcMats) { for (const m of this._srcMats) m.dispose(); this._srcMats = null; }

    // ---- cables
    this.cables = [];
    if (!ghost) {
      this.cableGroup = new THREE.Group();
      this.cableGroup.name = 'cables';
      this.root.add(this.cableGroup);
      for (const c of json.cables || []) {
        if (!c.points || c.points.length < 2) continue;
        const mat = new THREE.MeshStandardMaterial({ color: c.color || '#202020', roughness: 0.48, metalness: 0.0, envMapIntensity: 0.9, transparent: false });
        const mesh = new THREE.Mesh(new THREE.BufferGeometry(), mat);
        mesh.castShadow = true;
        mesh.name = `cable__${c.name}`;
        const capMat = new THREE.MeshStandardMaterial({ color: '#e8e1cf', roughness: 0.55 });
        const r = Math.max(1.4, (c.diameter || 3) / 2);
        const capGeo = new THREE.BoxGeometry(r * 3.2, r * 2.4, r * 4.2);
        const caps = [new THREE.Mesh(capGeo, capMat), new THREE.Mesh(capGeo, capMat)];
        caps.forEach((m) => { m.castShadow = true; this.cableGroup.add(m); });
        this.cableGroup.add(mesh);
        this.cables.push({ info: c, mesh, mat, capMat, caps, r, step: 0, op: -1, xr: -1, ok: true });
      }
    }

    this.values = this.resolve({});
    this.T = new Map();
    this.fkInto(this.values, 0, this.T);
    this.measure();
  }

  isDriven(name) { const j = this.jointMap.get(name); return !!j && !j.mimic && j.type !== 'fixed'; }

  resolve(q) {
    const v = {};
    for (const j of this.joints) if (!j.mimic) v[j.name] = Number.isFinite(q[j.name]) ? q[j.name] : j.home;
    for (let pass = 0; pass < 2; pass++) {
      for (const j of this.joints) {
        if (!j.mimic) continue;
        const m = j.mimic;
        v[j.name] = (v[m.joint] ?? 0) * (m.multiplier ?? 1) + (m.offset ?? 0);
      }
    }
    return v;
  }

  local(j, value, explode, out) {
    out.copy(j.origin);
    if (explode && j.chainMag) {
      _m2.makeTranslation(j.chainDir.x * j.chainMag * explode, j.chainDir.y * j.chainMag * explode, j.chainDir.z * j.chainMag * explode);
      out.multiply(_m2);
    }
    if (j.type === 'revolute' || j.type === 'continuous') out.multiply(_m2.makeRotationAxis(j.axis, value || 0));
    else if (j.type === 'prismatic') out.multiply(_m2.makeTranslation(j.axis.x * value, j.axis.y * value, j.axis.z * value));
    return out;
  }

  fkInto(values, explode, out) {
    if (!out.has(this.rootLink)) out.set(this.rootLink, new THREE.Matrix4());
    out.get(this.rootLink).identity();
    for (const j of this.order) {
      let M = out.get(j.child);
      if (!M) { M = new THREE.Matrix4(); out.set(j.child, M); }
      M.copy(out.get(j.parent)).multiply(this.local(j, values[j.name] ?? 0, explode, _m3));
    }
    return out;
  }

  // set GLB link node matrices for a pose (values already resolved incl. mimic)
  pose(values, explode = 0) {
    this.values = values;
    for (const j of this.order) j.node.matrix.copy(this.local(j, values[j.name] ?? 0, explode, _m3));
    this.fkInto(values, explode, this.T);
    this.root.updateMatrixWorld(true);
  }

  subtreeLinks(link) {
    const out = new Set([link]);
    const stack = [link];
    while (stack.length) { const l = stack.pop(); for (const j of this.children.get(l) || []) if (!out.has(j.child)) { out.add(j.child); stack.push(j.child); } }
    return out;
  }

  // world-ish (robot base frame) box of a part using the current FK (no part offsets unless asked)
  partBox(rec, out, offset = null) {
    const T = this.T.get(rec.link) || _m1.identity();
    _m1.copy(T);
    const p = offset || rec.basePos;
    _m2.makeTranslation(p.x, p.y, p.z);
    _m1.multiply(_m2);
    return out.copy(rec.bbox).applyMatrix4(_m1);
  }

  measure() {
    // overall size at the home pose
    const box = new THREE.Box3();
    for (const r of this.parts) box.union(this.partBox(r, _box2));
    if (box.isEmpty()) box.set(new THREE.Vector3(-100, -100, 0), new THREE.Vector3(100, 100, 300));
    this.homeBox = box;
    const sz = box.getSize(new THREE.Vector3());
    const reach = (this.json.meta && this.json.meta.sizing && this.json.meta.sizing.reach_mm) || 0;
    this.R = Math.max(sz.length() / 2, reach * 0.8, 60);
    this.reach = reach || Math.max(sz.x, sz.y, sz.z);
    // footprint of the root link (contact shadow)
    const fb = new THREE.Box3();
    for (const r of this.parts) if (r.link === this.rootLink) fb.union(this.partBox(r, _box2));
    this.footprint = fb.isEmpty() ? this.R * 0.3 : Math.max(fb.max.x - fb.min.x, fb.max.y - fb.min.y) / 2;
  }

  // explode vectors: use robot.json part.explode; if the generator left them all zero, derive a tasteful
  // automatic explode (links separate along the kinematic chain, parts spread from their link centroid).
  computeExplode() {
    const R = this.R;
    this.autoExplode = this.parts.length > 0 && this.parts.every((r) => r.explode.lengthSq() < 1e-6);
    const linkBox = new Map();
    for (const r of this.parts) {
      if (!linkBox.has(r.link)) linkBox.set(r.link, new THREE.Box3());
      linkBox.get(r.link).union(_box.copy(r.bbox).translate(r.basePos));
    }
    for (const r of this.parts) {
      const lb = linkBox.get(r.link);
      const c = lb.getCenter(new THREE.Vector3());
      const lsize = lb.getSize(new THREE.Vector3()).length();
      const d = r.center.clone().sub(c);
      const v = new THREE.Vector3();
      if (ELEC_KINDS.has(r.kind)) {
        v.set(d.x * 0.5, d.y * 0.5, 0).add(new THREE.Vector3(0, 0, R * 0.16));
      } else if (r.kind === 'fastener') {
        v.copy(d).setLength(R * 0.08);
      } else if (d.length() > lsize * 0.06) {
        if (r.kind === 'actuator' || r.kind === 'horn') d.z *= 0.3;
        v.copy(d).normalize().multiplyScalar(R * 0.05 + d.length() * 0.45);
      }
      if (r.link === this.rootLink && v.z < 0) v.z = 0;
      r.autoVec = v;
      if (this.autoExplode) r.explode.copy(v);
    }
    for (const j of this.joints) {
      j.chainMag = 0;
      if (!this.autoExplode) continue;
      const t = _v1.setFromMatrixPosition(j.origin);
      if (j.type === 'prismatic') { j.chainDir.copy(j.axis); j.chainMag = R * 0.05; }
      else if (t.length() > 1) { j.chainDir.copy(t).normalize(); j.chainMag = j.type === 'fixed' ? R * 0.03 : R * 0.1; }
    }
  }

  dispose() {
    disposeTree(this.root);
    for (const m of this.ghostMats) m.dispose();
  }
}

// ================================================================== RobotViewer
export class RobotViewer {
  constructor(container, opts = {}) {
    injectCSS();
    this.container = container;
    this.opts = Object.assign({
      theme: 'dark', autoOrbit: true, idleResumeMs: 9000, orbitSpeed: 0.55, edges: true, shadows: true, hud: true,
      intro: true, pixelRatio: Math.min(window.devicePixelRatio || 1, 2), fov: 30, highlightColor: HOT, stepColor: HOT,
      preserveDrawingBuffer: false,
    }, opts);
    this.theme = THEMES[this.opts.theme] || THEMES.dark;
    if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
    container.classList.add('mv-root');
    container.classList.toggle('mv-light', this.opts.theme === 'light');

    this._ev = {};
    this._tweens = new Map();
    this._anon = 0;
    this.q = {};
    this.model = null;
    this._models = [];
    this.ghost = null;
    this._explode = { cur: 0, target: 0 };
    this._xray = { cur: 0, target: 0 };
    this._labelsOn = false;
    this._labels = [];
    this._hlSel = null;
    this._stepMode = null;
    this._traj = null;
    this._ws = null;
    this._motion = null;
    this._dirty = true;
    this._poseDirty = true;
    this._partsDirty = true;
    this._cablesDirty = true;
    this._lineMats = new Set();
    this._last = performance.now();
    this._w = 0; this._h = 0;
    this._loadToken = 0;
    this._firstLoad = true;
    this._time = 0;

    // ---- renderer
    const r = this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance',
      preserveDrawingBuffer: !!this.opts.preserveDrawingBuffer });
    r.setPixelRatio(this.opts.pixelRatio);
    r.outputColorSpace = THREE.SRGBColorSpace;
    r.toneMapping = THREE.ACESFilmicToneMapping;
    r.toneMappingExposure = this.opts.exposure ?? this.theme.exposure;
    r.shadowMap.enabled = !!this.opts.shadows;
    r.shadowMap.type = THREE.PCFSoftShadowMap;
    r.domElement.className = 'mv-canvas';
    r.domElement.tabIndex = 0;
    container.appendChild(r.domElement);

    this.labelRenderer = new CSS2DRenderer();
    this.labelRenderer.domElement.className = 'mv-labels';
    container.appendChild(this.labelRenderer.domElement);

    this._hudEl = document.createElement('div');
    this._hudEl.className = 'mv-hud';
    this._hudEl.innerHTML = '<i></i><b></b><span></span>';
    container.appendChild(this._hudEl);

    // ---- scene
    const scene = this.scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(r);
    this._envTex = pmrem.fromScene(new RoomEnvironment(r), 0.04).texture;
    scene.environment = this._envTex;
    pmrem.dispose();
    this._bgTex = gradientTexture(this.theme.bgInner, this.theme.bgOuter);
    scene.background = this._bgTex;

    this.camera = new THREE.PerspectiveCamera(this.opts.fov, 1, 2, 60000);
    this.camera.position.set(500, 380, 900);
    const ctl = this.controls = new OrbitControls(this.camera, r.domElement);
    ctl.enableDamping = true;
    ctl.dampingFactor = 0.075;
    ctl.screenSpacePanning = true;
    ctl.maxPolarAngle = Math.PI * 0.495;
    ctl.minDistance = 40;
    ctl.maxDistance = 20000;
    ctl.rotateSpeed = 0.8;
    ctl.zoomSpeed = 0.9;
    ctl.target.set(0, 150, 0);
    ctl.autoRotate = !!this.opts.autoOrbit;
    ctl.autoRotateSpeed = 0;
    this._orbitOn = !!this.opts.autoOrbit;
    this._orbitK = this.opts.intro ? 0 : 1;   // eased auto-orbit speed factor
    this._idleT = 0;
    ctl.addEventListener('start', () => {
      this._userActive = true;
      this._orbitK = 0;
      this._cancelTween('camera');
      this._idleT = performance.now();
    });
    ctl.addEventListener('end', () => { this._userActive = false; this._idleT = performance.now(); });
    ctl.addEventListener('change', () => { this._dirty = true; });

    // robot base frame (z-up, mm) -> scene (y-up)
    this.robotFrame = new THREE.Group();
    this.robotFrame.name = 'robotFrame';
    this.robotFrame.rotation.x = -Math.PI / 2;
    scene.add(this.robotFrame);
    this.robotFrame.updateMatrixWorld(true);
    this.overlays = new THREE.Group();
    this.overlays.name = 'overlays';
    this.robotFrame.add(this.overlays);

    this._buildStage();

    // ---- events
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(container);
    this.resize();
    this._onPointerDown = (e) => { this._pd = { x: e.clientX, y: e.clientY }; };
    this._onPointerUp = (e) => {
      if (!this._pd) return;
      const moved = Math.hypot(e.clientX - this._pd.x, e.clientY - this._pd.y);
      this._pd = null;
      if (moved < 5) this._pick(e);
    };
    r.domElement.addEventListener('pointerdown', this._onPointerDown);
    r.domElement.addEventListener('pointerup', this._onPointerUp);
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);
  }

  // ------------------------------------------------------------ events
  on(event, cb) {
    (this._ev[event] = this._ev[event] || new Set()).add(cb);
    return () => this.off(event, cb);
  }
  off(event, cb) { if (this._ev[event]) this._ev[event].delete(cb); }
  _emit(event, data) {
    for (const cb of this._ev[event] || []) { try { cb(data); } catch (e) { console.error(e); } }
  }

  // ------------------------------------------------------------ stage: lights, floor, shadows
  _buildStage() {
    const s = this.scene;
    this._hemi = new THREE.HemisphereLight(0xe4eeff, 0x1a1f26, 0.35);
    s.add(this._hemi);
    const key = this._key = new THREE.DirectionalLight(0xffffff, 2.3);
    key.castShadow = !!this.opts.shadows;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.bias = -0.00025;
    key.shadow.normalBias = 0.6;
    s.add(key, key.target);
    const fill = this._fill = new THREE.DirectionalLight(0xc9dcff, 0.55);
    const rim = this._rim = new THREE.DirectionalLight(0xffffff, 0.9);
    s.add(fill, rim);

    const th = this.theme;
    this._floorU = {
      uColor: { value: new THREE.Color(th.floor) }, uGrid: { value: new THREE.Color(th.grid) },
      uGridMajor: { value: new THREE.Color(th.gridMajor) }, uRadius: { value: 1000 }, uCell: { value: 25 },
      uMajor: { value: 100 }, uOpacity: { value: 1 },
    };
    const floorMat = new THREE.ShaderMaterial({
      uniforms: this._floorU, transparent: true, depthWrite: false,
      vertexShader: `varying vec3 vW; void main(){ vec4 w = modelMatrix * vec4(position, 1.0); vW = w.xyz; gl_Position = projectionMatrix * viewMatrix * w; }`,
      fragmentShader: `
        varying vec3 vW; uniform vec3 uColor; uniform vec3 uGrid; uniform vec3 uGridMajor;
        uniform float uRadius; uniform float uCell; uniform float uMajor; uniform float uOpacity;
        float gridLine(vec2 p, float cell, float w){
          vec2 c = p / cell; vec2 g = abs(fract(c - 0.5) - 0.5) / (fwidth(c) * w);
          return 1.0 - min(min(g.x, g.y), 1.0);
        }
        void main(){
          float r = length(vW.xz);
          float fade = 1.0 - smoothstep(uRadius * 0.25, uRadius, r);
          float gfade = 1.0 - smoothstep(uRadius * 0.15, uRadius * 0.75, r);
          float minor = gridLine(vW.xz, uCell, 1.0) * gfade;
          float major = gridLine(vW.xz, uMajor, 1.3) * gfade;
          vec3 col = mix(uColor, uGrid, minor * 0.9);
          col = mix(col, uGridMajor, major);
          gl_FragColor = vec4(col, fade * uOpacity);
          #include <colorspace_fragment>
        }`,
    });
    const floor = this._floor = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -0.6;
    floor.renderOrder = -10;
    floor.raycast = () => {};
    s.add(floor);
    const shadow = this._shadowPlane = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShadowMaterial({ opacity: th.shadow, transparent: true, depthWrite: false }));
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = -0.3;
    shadow.receiveShadow = true;
    shadow.renderOrder = -8;
    shadow.raycast = () => {};
    s.add(shadow);
    const blob = this._blob = new THREE.Mesh(new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ map: blobTexture(), transparent: true, depthWrite: false, opacity: th.contact, toneMapped: false }));
    blob.rotation.x = -Math.PI / 2;
    blob.position.y = -0.45;
    blob.renderOrder = -9;
    blob.raycast = () => {};
    s.add(blob);
    this._fitStage(260, 100, 300);
  }

  _fitStage(R, footprint, reach) {
    const ext = Math.max(R, reach) * 1.25;
    this._floor.scale.set(ext * 9, ext * 9, 1);
    this._floorU.uRadius.value = ext * 2.1;
    const nice = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
    const cell = nice.find((n) => n >= R / 10) || 1000;
    this._floorU.uCell.value = cell;
    this._floorU.uMajor.value = cell * 4;
    this._shadowPlane.scale.set(ext * 5, ext * 5, 1);
    this._blob.scale.set(footprint * 3.4, footprint * 3.4, 1);
    const tgt = new THREE.Vector3(0, R * 0.55, 0);
    const key = this._key;
    key.target.position.copy(tgt);
    key.position.copy(tgt).add(new THREE.Vector3(-0.55, 1.15, 0.75).normalize().multiplyScalar(R * 4));
    const sc = key.shadow.camera;
    sc.left = sc.bottom = -ext * 1.25;
    sc.right = sc.top = ext * 1.25;
    sc.near = R * 0.5;
    sc.far = R * 9;
    sc.updateProjectionMatrix();
    this._fill.position.copy(tgt).add(new THREE.Vector3(1, 0.45, -0.25).normalize().multiplyScalar(R * 4));
    this._fill.target.position.copy(tgt);
    this._rim.position.copy(tgt).add(new THREE.Vector3(-0.2, 0.8, -1).normalize().multiplyScalar(R * 4));
    this._rim.target.position.copy(tgt);
    this.scene.add(this._fill.target, this._rim.target);
    this.camera.near = Math.max(1, R * 0.01);
    this.camera.far = R * 120;
    this.camera.updateProjectionMatrix();
    this.controls.minDistance = R * 0.25;
    this.controls.maxDistance = R * 20;
  }

  setTheme(name) {
    const th = THEMES[name];
    if (!th) return;
    this.theme = th;
    this.opts.theme = name;
    this.container.classList.toggle('mv-light', name === 'light');
    this._bgTex.dispose();
    this._bgTex = gradientTexture(th.bgInner, th.bgOuter);
    if (this.scene.background) this.scene.background = this._bgTex;
    this._floorU.uColor.value.set(th.floor);
    this._floorU.uGrid.value.set(th.grid);
    this._floorU.uGridMajor.value.set(th.gridMajor);
    this._shadowPlane.material.opacity = th.shadow;
    this._blob.material.opacity = th.contact;
    this.renderer.toneMappingExposure = this.opts.exposure ?? th.exposure;
    this._dirty = true;
  }

  setAutoOrbit(on) { this._orbitOn = !!on; if (!on) this._orbitK = 0; this._dirty = true; }

  // ------------------------------------------------------------ loading
  async _fetchRobot(src) {
    let json, base;
    if (typeof src === 'string') {
      base = resolveUrl(src);
      const res = await fetch(base);
      if (!res.ok) throw new Error(`robot.json ${res.status} ${src}`);
      json = await res.json();
    } else if (src && typeof src === 'object') {
      json = src;
      base = src.baseUrl || location.href;
    } else throw new Error('load(): expected a robot.json URL or object');
    const glbUrl = json.glbUrl || resolveUrl(json.glb || 'robot.glb', base);
    const gltf = await new GLTFLoader().loadAsync(glbUrl);
    return { json, gltf, glbUrl };
  }

  async load(src, { keepPose = true } = {}) {
    const token = ++this._loadToken;
    let fetched;
    try {
      fetched = await this._fetchRobot(src);
    } catch (e) {
      console.error('[RobotViewer] load failed', e);
      this._emit('error', { error: e, url: src });
      throw e;
    }
    if (token !== this._loadToken) return null;   // a newer load superseded this one
    const { json, gltf } = fetched;
    const model = new RobotModel(json, gltf, { edges: this.opts.edges });
    model.computeExplode();
    model.cableGroup && this._initCableSteps(model);
    this.robotFrame.add(model.root);

    // pose: keep shared joint values across revisions, otherwise meta.home
    const home = (json.meta && json.meta.home) || {};
    const q = {};
    for (const j of model.joints) {
      if (!model.isDriven(j.name)) continue;
      if (keepPose && this.model && Number.isFinite(this.q[j.name]) && this.model.isDriven(j.name)) q[j.name] = this.q[j.name];
      else q[j.name] = Number.isFinite(home[j.name]) ? home[j.name] : j.home;
    }
    const prev = this.model;
    this._stopMotionNow();
    this._cancelTween('joints');
    this.model = model;
    this.json = json;
    this.q = q;
    this._stepMode = null;
    model.pose(model.resolve(q), this._explode.cur);
    model.measure();   // measure at the actual pose
    this._fitStage(model.R, model.footprint, model.reach);

    // crossfade
    model.fade = 0;
    this._models = this._models.filter((m) => m !== model);
    this._models.push(model);
    this._tween(`fade:${model.root.uuid}`, prev ? 650 : 900, Ease.inOutSine, (k) => { model.fade = k; this._partsDirty = true; });
    if (prev) {
      const f0 = prev.fade;
      this._tween(`fade:${prev.root.uuid}`, 650, Ease.inOutSine, (k) => { prev.fade = f0 * (1 - k); this._partsDirty = true; })
        .then(() => { this._models = this._models.filter((m) => m !== prev); prev.dispose(); this._dirty = true; });
    }
    this._clearLabels();
    if (this._labelsOn) this._buildLabels();
    if (this._hlSel) this.highlight(this._hlSel.sel, this._hlSel.color);
    if (this._traj) this._rebuildTrajFor(model);
    this._poseDirty = this._partsDirty = this._cablesDirty = true;

    if (this._firstLoad) {
      this._firstLoad = false;
      this._introCamera();
    }
    this._emit('loaded', { name: json.name, json, url: typeof src === 'string' ? src : null, parts: model.parts.length });
    return json;
  }

  get robot() { return this.json; }
  get parts() { return this.json ? this.json.parts : []; }
  get steps() { return this.json ? this.json.steps || [] : []; }
  get jointInfo() {
    if (!this.model) return [];
    return this.model.joints.filter((j) => this.model.isDriven(j.name)).map((j) => ({ name: j.name, type: j.type,
      lower: j.lower, upper: j.upper, home: j.home, actuator: j.actuator, value: this.model.values[j.name] }));
  }
  get armJoints() {
    const m = this.json && this.json.meta;
    if (m && Array.isArray(m.arm_joints)) return m.arm_joints.slice();
    return this.model ? this.model.joints.filter((j) => j.type === 'revolute' && !j.mimic).map((j) => j.name) : [];
  }
  get gripperJoint() { const m = this.json && this.json.meta; return (m && m.gripper_joint) || (this.model && this.model.jointMap.has('gripper') ? 'gripper' : null); }

  _homeQ() {
    const out = {};
    if (!this.model) return out;
    const home = (this.json.meta && this.json.meta.home) || {};
    for (const j of this.model.joints) if (this.model.isDriven(j.name)) out[j.name] = Number.isFinite(home[j.name]) ? home[j.name] : j.home;
    return out;
  }

  // ------------------------------------------------------------ joints
  setJoints(q) {
    if (!q || !this.model) return;
    for (const [k, v] of Object.entries(q)) if (Number.isFinite(v) && this.model.isDriven(k)) this.q[k] = v;
    this._poseDirty = true;
  }
  getJoints() { return Object.assign({}, this.q); }

  animateTo(q, ms = 800) {
    if (!this.model || !q) return Promise.resolve(false);
    const from = {}, to = {};
    for (const [k, v] of Object.entries(q)) {
      if (!Number.isFinite(v) || !this.model.isDriven(k)) continue;
      from[k] = Number.isFinite(this.q[k]) ? this.q[k] : this.model.jointMap.get(k).home;
      to[k] = v;
    }
    return this._tween('joints', ms, Ease.inOutCubic, (k) => {
      for (const n in to) this.q[n] = lerp(from[n], to[n], k);
      this._poseDirty = true;
    });
  }

  // forward kinematics helper for callers (IK in tests, overlays): Matrix4 of `link` in the robot base frame
  fk(q = null, link = null) {
    if (!this.model) return new THREE.Matrix4();
    const vals = this.model.resolve(Object.assign({}, this.q, q || {}));
    const T = this.model.fkInto(vals, 0, new Map());
    const name = link || (this.json.meta && this.json.meta.tcp_link) || 'tcp';
    return (T.get(name) || T.get(this.model.order.length ? this.model.order[this.model.order.length - 1].child : this.model.rootLink)).clone();
  }

  // ------------------------------------------------------------ motion demo
  async playMotionDemo() {
    if (!this.model) return;
    if (this._motion) return;
    this.pause();
    const token = this._motion = { stop: false };
    const model = this.model;
    const home = this._homeQ();
    const alive = () => !token.stop && this._motion === token && this.model === model;
    const dur = (from, to, speed) => clamp(Math.abs(to - from) / speed * 1000, 450, 1700);
    await this.animateTo(home, 700);
    while (alive()) {
      for (const name of this.armJoints) {
        if (!alive()) break;
        const j = model.jointMap.get(name);
        if (!j || j.type === 'fixed') continue;
        const [lo, hi] = this._safeSweep(j, home);
        this._motionCue(j, `${fmtDeg(lo - home[name])} … +${fmtDeg(hi - home[name])}`.replace('+-', '-'));
        let cur = this.q[name];
        await this.animateTo({ ...home, [name]: hi }, dur(cur, hi, 1.25)); if (!alive()) break;
        await sleep(140); if (!alive()) break;
        await this.animateTo({ ...home, [name]: lo }, dur(hi, lo, 1.25)); if (!alive()) break;
        await sleep(140); if (!alive()) break;
        await this.animateTo(home, dur(lo, home[name], 1.25)); if (!alive()) break;
        this._motionCue(null);
        await sleep(160);
      }
      const g = this.gripperJoint && model.jointMap.get(this.gripperJoint);
      if (g && alive()) {
        const open = g.upper, closed = Math.max(g.lower, Math.min(0, g.upper));
        this._motionCue(g, `${closed.toFixed(0)} … ${open.toFixed(0)} mm`);
        await this.animateTo({ ...home, [g.name]: open }, 650); if (!alive()) break;
        await sleep(160); if (!alive()) break;
        await this.animateTo({ ...home, [g.name]: closed }, 800); if (!alive()) break;
        await sleep(260); if (!alive()) break;
        await this.animateTo({ ...home, [g.name]: open }, 650); if (!alive()) break;
        await this.animateTo(home, 500); if (!alive()) break;
        this._motionCue(null);
      }
      await sleep(700);
    }
    if (this._motion === token) this._motion = null;
    this._motionCue(null);
  }

  stopMotion({ home = true } = {}) {
    if (!this._motion) return Promise.resolve(false);
    this._stopMotionNow();
    return home ? this.animateTo(this._homeQ(), 700) : Promise.resolve(true);
  }

  _stopMotionNow() {
    if (this._motion) { this._motion.stop = true; this._motion = null; }
    this._cancelTween('joints');
    this._motionCue(null);
  }

  get motionPlaying() { return !!this._motion; }

  // safe sweep: a fraction of the range on each side of home, shrunk until no moving part dips under the floor
  _safeSweep(j, home) {
    const model = this.model;
    const h = home[j.name] ?? j.home;
    const span = j.upper - j.lower;
    const lo0 = clamp(h + 0.62 * (j.lower - h), j.lower + 0.08 * span, j.upper);
    const hi0 = clamp(h + 0.62 * (j.upper - h), j.lower, j.upper - 0.08 * span);
    const moving = model.subtreeLinks(j.child);
    const parts = model.parts.filter((r) => moving.has(r.link));
    const T = new Map();
    const floorZ = 6;
    const safeAt = (v) => {
      model.fkInto(model.resolve({ ...home, [j.name]: v }), 0, T);
      for (const r of parts) {
        _m1.copy(T.get(r.link));
        const b = r.bbox;
        for (let i = 0; i < 8; i++) {
          _v1.set(i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y, i & 4 ? b.max.z : b.min.z).add(r.basePos).applyMatrix4(_m1);
          if (_v1.z < floorZ) return false;
        }
      }
      return true;
    };
    const shrink = (target) => {
      let a = 0, b = 1;
      const ok = (s) => { for (let k = 1; k <= 6; k++) if (!safeAt(h + (target - h) * s * k / 6)) return false; return true; };
      if (ok(1)) return target;
      for (let it = 0; it < 12; it++) { const m = (a + b) / 2; if (ok(m)) a = m; else b = m; }
      return h + (target - h) * a * 0.92;
    };
    return [shrink(lo0), shrink(hi0)];
  }

  _motionCue(j, rangeText = '') {
    const model = this.model;
    if (!model) return;
    for (const r of model.parts) r.motionGlow = 0;
    if (this._gizmo) {
      const g = this._gizmo;
      this._gizmo = null;
      this._tween(`gizmo:${g.uuid}`, 260, Ease.outCubic, (k) => { g.userData.mat.opacity = 0.95 * (1 - k); g.scale.setScalar(1 + 0.15 * k); })
        .then(() => disposeTree(g));
    }
    if (!j) { this._hud(null); return; }
    const sub = model.subtreeLinks(j.child);
    for (const r of model.parts) if (sub.has(r.link)) r.motionGlow = 1;
    const g = this._makeGizmo(j);
    this._gizmo = g;
    this._tween(`gizmo:${g.uuid}`, 380, Ease.outBack, (k) => { g.userData.mat.opacity = 0.95 * clamp(k, 0, 1); g.scale.setScalar(0.6 + 0.4 * k); });
    const act = j.actuator ? pretty(j.actuator) : (j.mimic ? `mimics ${j.mimic.joint}` : '');
    this._hud('Motion demo', `${prettyJoint(j.name)}${act ? ' · ' + act : ''}${rangeText ? ' · ' + rangeText : ''}`, ACCENT);
    this._emit('motion', { joint: j.name, actuator: j.actuator, range: rangeText });
  }

  _makeGizmo(j) {
    const model = this.model;
    const R = model.R;
    const mat = new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0, depthTest: false, depthWrite: false, toneMapped: false });
    const g = new THREE.Group();
    g.userData.mat = mat;
    const holder = new THREE.Group();
    holder.matrixAutoUpdate = false;
    holder.matrix.copy(j.origin);
    if (j.type === 'prismatic') {
      // double arrow along the jaw axis, at the jaws' height
      const len = R * 0.32, r = R * 0.007;
      const shaft = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 12), mat);
      const c1 = new THREE.Mesh(new THREE.ConeGeometry(r * 3.2, r * 7, 18), mat);
      const c2 = c1.clone();
      c1.position.y = len / 2; c2.position.y = -len / 2; c2.rotation.z = Math.PI;
      const arrow = new THREE.Group();
      arrow.add(shaft, c1, c2);
      arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), j.axis);
      g.add(arrow);
      const sub = model.subtreeLinks(j.child);
      const bb = new THREE.Box3();
      for (const r2 of model.parts) if (sub.has(r2.link)) bb.union(_box.copy(r2.bbox).translate(r2.basePos));
      if (!bb.isEmpty()) g.position.set(0, 0, bb.max.z + R * 0.03);
    } else {
      const rr = clamp(R * 0.11, 16, 120);
      const arc = Math.PI * 1.55;
      const torus = new THREE.Mesh(new THREE.TorusGeometry(rr, rr * 0.035, 10, 96, arc), mat);
      const cone = new THREE.Mesh(new THREE.ConeGeometry(rr * 0.11, rr * 0.3, 20), mat);
      cone.position.set(rr * Math.cos(arc), rr * Math.sin(arc), 0);
      cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), new THREE.Vector3(-Math.sin(arc), Math.cos(arc), 0));
      const ring = new THREE.Group();
      ring.add(torus, cone);
      ring.quaternion.setFromUnitVectors(Z, j.axis);
      ring.userData.spin = true;
      g.add(ring);
      const axisLine = new THREE.Mesh(new THREE.CylinderGeometry(rr * 0.012, rr * 0.012, rr * 2.6, 6),
        new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.35, depthTest: false, depthWrite: false, toneMapped: false }));
      axisLine.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), j.axis);
      g.add(axisLine);
    }
    g.traverse((o) => { o.renderOrder = 30; o.raycast = () => {}; });
    holder.add(g);
    const parentNode = model.links.get(j.parent) || model.links.get(model.rootLink);
    parentNode.add(holder);
    // disposeTree(g) detaches g from the holder; take the holder out with it
    g.addEventListener('removed', () => { if (holder.parent) holder.parent.remove(holder); });
    this._dirty = true;
    return g;
  }

  // ------------------------------------------------------------ explode
  setExplode(t, { animate = true } = {}) {
    this._explode.target = clamp(+t || 0, 0, 1);
    if (!animate) { this._explode.cur = this._explode.target; this._poseDirty = this._partsDirty = true; }
  }
  get explode() { return this._explode.target; }

  // ------------------------------------------------------------ assembly steps
  showStep(i, { animate = true } = {}) {
    const model = this.model;
    if (!model) return;
    this._stopMotionNow();
    this.setExplode(0);
    const idx = +i;
    const prevStep = this._stepMode;
    this._stepMode = idx;
    const steps = this.steps;
    const step = steps.find((s) => s.index === idx) || null;
    const R = model.R;
    let n = 0;
    for (const r of model.parts) {
      const ps = r.info.step || 0;
      const was = r._stepState;
      if (ps < idx) {
        r._stepState = 'done';
        r.hidden = false;
        r.stepGlow = 0;
        if (was !== 'done') this._tweenPart(r, 'alpha', r.alphaStep, 1, animate ? 350 : 0);
        if (r.stepOffset.lengthSq() > 0) this._tweenOffset(r, r.stepOffset.clone(), new THREE.Vector3(), animate ? 450 : 0);
      } else if (ps === idx) {
        r._stepState = 'current';
        r.hidden = false;
        r.stepGlow = 1;
        if (was !== 'current' || prevStep === null) {
          const fly = this._flyVector(r);
          const delay = animate ? Math.min(n * 90, 600) : 0;
          n++;
          r.stepOffset.copy(animate ? fly : new THREE.Vector3());
          r.alphaStep = animate ? 0 : 1;
          if (animate) {
            const from = fly.clone();
            setTimeout(() => {
              if (r._stepState !== 'current') return;
              this._tweenPart(r, 'alpha', 0, 1, 380);
              this._tweenOffset(r, from, new THREE.Vector3(), 1050, Ease.outQuart);
            }, delay);
          }
        }
      } else {
        r._stepState = 'hidden';
        r.stepGlow = 0;
        this._tweenPart(r, 'alpha', r.alphaStep, 0, animate ? 260 : 0);
      }
    }
    for (const c of model.cables) c.stepVisible = idx >= c.step;
    this._partsDirty = this._cablesDirty = true;
    // camera: the step's parts with some context
    const names = step ? step.parts : model.parts.filter((r) => r.info.step === idx).map((r) => r.name);
    if (step && step.camera && Array.isArray(step.camera.target)) {
      const t = new THREE.Vector3(...step.camera.target).applyMatrix4(this.robotFrame.matrixWorld);
      this._flyTo(t, step.camera.dist || R * 1.6, null, animate ? 1000 : 0);
    } else if (names && names.length) {
      this.frame(names, { minRadius: R * 0.38, ms: animate ? 1000 : 0, atRest: true });
    } else {
      this.frame(null, { ms: animate ? 1000 : 0 });
    }
    const title = step ? step.title : `Step ${idx}`;
    this._hud(`Step ${idx}${steps.length ? ' / ' + steps.length : ''}`, title, this.opts.stepColor);
    this._emit('step', { index: idx, title, parts: names || [], step });
  }

  showAllSteps({ animate = true } = {}) {
    const model = this.model;
    this._stepMode = null;
    if (!model) return;
    for (const r of model.parts) {
      r._stepState = null;
      r.hidden = false;
      r.stepGlow = 0;
      this._tweenPart(r, 'alpha', r.alphaStep, 1, animate ? 400 : 0);
      if (r.stepOffset.lengthSq() > 0) this._tweenOffset(r, r.stepOffset.clone(), new THREE.Vector3(), animate ? 500 : 0);
    }
    for (const c of model.cables) c.stepVisible = true;
    this._partsDirty = this._cablesDirty = true;
    this._hud(null);
    this.frame(null, { ms: animate ? 900 : 0 });
    this._emit('step', { index: null, title: 'All steps', parts: [] });
  }

  _initCableSteps(model) {
    const firstStep = new Map();
    for (const r of model.parts) {
      const s = r.info.step || 0;
      if (!firstStep.has(r.link) || s < firstStep.get(r.link)) firstStep.set(r.link, s);
    }
    for (const c of model.cables) {
      c.step = Math.max(0, ...c.info.points.map((p) => firstStep.get(p.link) ?? 0));
      c.stepVisible = true;
    }
  }

  _flyVector(r) {
    const model = this.model;
    const R = model.R;
    if (!model.autoExplode && r.info.explode && Math.hypot(...r.info.explode) > 1e-3) {
      const v = new THREE.Vector3(...r.info.explode);
      if (v.length() < R * 0.15) v.setLength(R * 0.15);
      return v;
    }
    // world "up" expressed in the link frame, plus the part's own outward direction
    const T = model.T.get(r.link) || _m1.identity();
    _q1.setFromRotationMatrix(T).invert();
    const up = new THREE.Vector3(0, 0, 1).applyQuaternion(_q1).multiplyScalar(R * 0.26);
    if (r.autoVec && r.autoVec.lengthSq() > 1e-6) up.add(r.autoVec.clone().setLength(R * 0.12));
    return up;
  }

  _tweenPart(r, what, from, to, ms) {
    const key = `p:${what}:${r.model.root.uuid}:${r.name}`;
    if (ms <= 0) { this._cancelTween(key); r.alphaStep = to; this._partsDirty = true; return; }
    this._tween(key, ms, Ease.inOutSine, (k) => { r.alphaStep = lerp(from, to, k); this._partsDirty = true; });
  }

  _tweenOffset(r, from, to, ms, ease = Ease.outCubic) {
    const key = `p:off:${r.model.root.uuid}:${r.name}`;
    if (ms <= 0) { this._cancelTween(key); r.stepOffset.copy(to); this._partsDirty = true; return; }
    this._tween(key, ms, ease, (k) => { r.stepOffset.lerpVectors(from, to, k); this._partsDirty = true; });
  }

  // ------------------------------------------------------------ highlight / x-ray / labels
  _matcher(selector) {
    if (selector == null) return null;
    const arr = Array.isArray(selector) ? selector : [selector];
    const set = new Set(arr.map(String));
    if (set.has('*') || set.has('all')) return () => true;
    return (r) => set.has(r.name) || set.has(r.kind) || set.has(r.link) || set.has(r.info.group) || (r.info.sku && set.has(r.info.sku)) ||
      set.has(r.info.material) || set.has(r.info.process);
  }

  highlight(selector, color = this.opts.highlightColor) {
    if (selector == null || (Array.isArray(selector) && !selector.length)) {
      this._hlSel = null;
      if (this.model) for (const r of this.model.parts) r.hl = 0;
      return [];
    }
    this._hlSel = { sel: selector, color };
    const match = this._matcher(selector);
    const out = [];
    if (!this.model) return out;
    const c = new THREE.Color(color);
    for (const r of this.model.parts) {
      r.hl = match(r) ? 1 : 0;
      if (r.hl) { r.hlColor = c; out.push(r.name); }
    }
    return out;
  }

  setXray(on) { this._xray.target = on ? 1 : 0; this._dirty = true; }
  get xray() { return this._xray.target > 0.5; }

  setLabels(on) {
    this._labelsOn = !!on;
    if (on && this.model && !this._labels.length) this._buildLabels();
    for (const L of this._labels) {
      if (on) { L.obj.visible = true; requestAnimationFrame(() => L.el.classList.add('on')); clearTimeout(L.hideT); }
      else { L.el.classList.remove('on'); L.hideT = setTimeout(() => { if (!this._labelsOn) L.obj.visible = false; this._dirty = true; }, 380); }
    }
    this._dirty = true;
  }
  get labels() { return this._labelsOn; }

  _clearLabels() {
    for (const L of this._labels) { clearTimeout(L.hideT); disposeTree(L.obj); }
    this._labels = [];
  }

  _makeLabel(title, sub, color, side) {
    const el = document.createElement('div');
    el.className = 'mv-label';
    el.style.setProperty('--mv-c', color);
    el.innerHTML = '<div class="mv-in"><div class="mv-lead"></div><div class="mv-dot"></div><div class="mv-pill"><b></b><span></span></div></div>';
    el.querySelector('b').textContent = title;
    el.querySelector('span').textContent = sub;
    const obj = new CSS2DObject(el);
    obj.center.set(0, 0);
    return { el, obj, pill: el.querySelector('.mv-pill'), lead: el.querySelector('.mv-lead'), side, w: 0, h: 0 };
  }

  _buildLabels() {
    const model = this.model;
    if (!model) return;
    this._clearLabels();
    // actuators: one per actuated joint, anchored on the joint axis
    for (const j of model.joints) {
      if (!j.actuator || j.mimic || j.type === 'fixed') continue;
      const eff = Number.isFinite(j.effort) && j.effort > 0 ? (j.type === 'prismatic' ? ` · ${j.effort} N·m` : ` · ${j.effort} N·m`) : '';
      const L = this._makeLabel(prettyJoint(j.name), `${pretty(j.actuator)}${eff}`, ACCENT, 1);
      if (j.type === 'prismatic') {
        const sub = model.subtreeLinks(j.child);
        const bb = new THREE.Box3();
        for (const r of model.parts) if (sub.has(r.link) || r.link === j.parent) bb.union(_box.copy(r.bbox).translate(r.basePos));
        const holder = new THREE.Group();
        holder.matrixAutoUpdate = false;
        holder.matrix.copy(j.origin);
        model.links.get(j.parent).add(holder);
        const c = bb.isEmpty() ? new THREE.Vector3() : bb.getCenter(new THREE.Vector3());
        L.obj.position.set(0, 0, c.z);
        holder.add(L.obj);
        L.holder = holder;
      } else {
        j.node.add(L.obj);
      }
      L.kind = 'joint';
      this._labels.push(L);
    }
    // electronics modules: group their parts by sku/group, anchor on top of the module
    const groups = new Map();
    for (const r of model.parts) {
      if (!ELEC_KINDS.has(r.kind)) continue;
      const m = /(?:^|_)(u\d+|j\d+|a\d+)(?:_|$)/i.exec(r.name);
      const key = `${r.link}|${r.info.meta && r.info.meta.ref ? r.info.meta.ref : (m ? m[1] : (r.info.sku || r.info.group || r.name))}`;
      if (!groups.has(key)) groups.set(key, { parts: [], ref: (r.info.meta && r.info.meta.ref) || (m ? m[1].toUpperCase() : ''), link: r.link });
      groups.get(key).parts.push(r);
    }
    for (const g of groups.values()) {
      const r0 = g.parts[0];
      const bb = new THREE.Box3();
      for (const r of g.parts) bb.union(_box.copy(r.bbox).translate(r.basePos));
      const meta = r0.info.meta || {};
      const title = meta.title || meta.label || (r0.info.sku ? pretty(r0.info.sku) : pretty(r0.info.group || r0.name));
      const sub = [g.ref, r0.kind === 'sensor' ? 'sensor' : (r0.kind === 'pcb' ? 'custom PCB' : 'module')].filter(Boolean).join(' · ');
      const L = this._makeLabel(title, sub, GOOD, -1);
      const c = bb.getCenter(new THREE.Vector3());
      L.obj.position.set(c.x, c.y, bb.max.z);
      L.kind = 'module';
      model.links.get(g.link).add(L.obj);
      this._labels.push(L);
    }
    for (const L of this._labels) L.obj.visible = this._labelsOn;
    if (this._labelsOn) requestAnimationFrame(() => this._labels.forEach((L) => L.el.classList.add('on')));
  }

  // column layout for callouts so pills never overlap (runs after the CSS2D pass)
  _layoutLabels() {
    const W = this._w, H = this._h;
    const vis = [];
    for (const L of this._labels) {
      if (!L.obj.visible || L.el.style.display === 'none') continue;
      if (!L.w && L.el.isConnected) { L.w = L.pill.offsetWidth; L.h = L.pill.offsetHeight; }
      _v1.setFromMatrixPosition(L.obj.matrixWorld).project(this.camera);
      L.ax = (_v1.x + 1) / 2 * W;
      L.ay = (1 - _v1.y) / 2 * H;
      vis.push(L);
    }
    if (!vis.length) return;
    // which side: modules go left, joints go right, unless the anchor sits far on the other side
    const cx = vis.reduce((a, L) => a + L.ax, 0) / vis.length;
    const sides = { 1: [], '-1': [] };
    for (const L of vis) {
      let s = L.side;
      if (s > 0 && L.ax < cx - W * 0.22) s = -1;
      if (s < 0 && L.ax > cx + W * 0.22) s = 1;
      sides[s].push(L);
    }
    const gap = 8;
    for (const s of [1, -1]) {
      const list = sides[s].sort((a, b) => a.ay - b.ay);
      if (!list.length) continue;
      const maxW = Math.max(...list.map((L) => L.w || 120));
      let col = s > 0 ? Math.max(...list.map((L) => L.ax)) + 46 : Math.min(...list.map((L) => L.ax)) - 46;
      if (s > 0) col = Math.min(col, W - maxW - 12); else col = Math.max(col, maxW + 12);
      // vertical packing
      const ys = list.map((L) => L.ay - 10);
      for (let i = 1; i < ys.length; i++) ys[i] = Math.max(ys[i], ys[i - 1] + (list[i - 1].h || 32) + gap);
      const over = ys[ys.length - 1] + (list[list.length - 1].h || 32) / 2 - (H - 12);
      if (over > 0) for (let i = 0; i < ys.length; i++) ys[i] -= over;
      for (let i = ys.length - 2; i >= 0; i--) ys[i] = Math.min(ys[i], ys[i + 1] - (list[i].h || 32) - gap);
      list.forEach((L, i) => {
        const h = L.h || 32, w = L.w || 120;
        const top = clamp(ys[i], 8 + h / 2, H - 8 - h / 2);
        const px = s > 0 ? col - L.ax : col - w - L.ax;
        const py = top - L.ay;
        L.pill.style.transform = `translate(${px.toFixed(1)}px,${(py - h / 2).toFixed(1)}px)`;
        const ex = s > 0 ? px : px + w, ey = py;
        const len = Math.hypot(ex, ey);
        L.lead.style.width = `${Math.max(0, len - 2).toFixed(1)}px`;
        L.lead.style.transform = `rotate(${Math.atan2(ey, ex)}rad)`;
      });
    }
  }

  // ------------------------------------------------------------ trajectory playback
  playTrajectory(traj, { loop = false, speed = 1, onFrame = null } = {}) {
    if (!traj || !Array.isArray(traj.frames) || !traj.frames.length) return Promise.resolve(false);
    this._stopMotionNow();
    if (!this._traj || this._traj.src !== traj) this._buildTraj(traj);
    const T = this._traj;
    if (!T) return Promise.resolve(false);
    T.loop = loop; T.speed = speed; T.onFrame = onFrame;
    if (T.t >= T.duration - 1e-6) T.t = 0;
    T.playing = true;
    this._applyTraj();
    return new Promise((res) => { T.resolve = res; });
  }

  pause() { if (this._traj && this._traj.playing) { this._traj.playing = false; this._traj.resolve && this._traj.resolve(false); } }
  resume() { if (this._traj && !this._traj.playing) return this.playTrajectory(this._traj.src, { loop: this._traj.loop, speed: this._traj.speed, onFrame: this._traj.onFrame }); return Promise.resolve(false); }
  setTime(t) {
    if (!this._traj) return;
    this._traj.t = clamp(+t || 0, 0, this._traj.duration);
    this._applyTraj();
  }
  get duration() { return this._traj ? this._traj.duration : 0; }
  get time() { return this._traj ? this._traj.t : 0; }
  get playing() { return !!(this._traj && this._traj.playing); }

  _buildTraj(traj) {
    this._clearTraj();
    const model = this.model;
    if (!model) return;
    const frames = traj.frames;
    const t0 = frames[0].t ?? 0;
    const T = this._traj = { src: traj, frames, t0, duration: Math.max(1e-3, (frames[frames.length - 1].t ?? 0) - t0),
      t: 0, playing: false, loop: false, speed: 1, group: new THREE.Group(), joints: traj.joints || [] };
    T.group.name = 'trajectory';
    this.overlays.add(T.group);
    T.maxGrip = Math.max(1e-3, ...frames.map((f) => f.grip_n || 0));
    // object
    const o = traj.object || {};
    const D = o.diameter_mm || 40, Lz = o.length_mm || D;
    let geo;
    const shape = (o.shape || 'sphere').toLowerCase();
    if (shape === 'egg' || shape === 'ellipsoid') {
      // ellipsoid of revolution about z; an egg gets a slightly blunter bottom end
      const asym = shape === 'egg' ? 0.09 : 0.05;
      const pts = [];
      for (let i = 0; i <= 48; i++) {
        const u = i / 48 * Math.PI;
        const rr = (D / 2) * Math.sin(u) * (1 - asym * Math.cos(u));
        pts.push(new THREE.Vector2(Math.max(rr, 1e-4), -Math.cos(u) * Lz / 2));
      }
      geo = new THREE.LatheGeometry(pts, 64).rotateX(Math.PI / 2);
    } else if (shape === 'cylinder' || shape === 'can') {
      geo = new THREE.CylinderGeometry(D / 2, D / 2, Lz, 64, 1).rotateX(Math.PI / 2);
    } else if (shape === 'box' || shape === 'cube') {
      geo = new THREE.BoxGeometry(D, D, Lz);
    } else if (shape === 'capsule') {
      geo = new THREE.CapsuleGeometry(D / 2, Math.max(1, Lz - D), 12, 48).rotateX(Math.PI / 2);
    } else {
      geo = new THREE.SphereGeometry(D / 2, 48, 32);
    }
    const objMat = new THREE.MeshPhysicalMaterial({ color: o.color || '#f2e4cc', roughness: 0.62, metalness: 0, sheen: 0.4, sheenRoughness: 0.8, sheenColor: new THREE.Color('#fff5e6') });
    T.obj = new THREE.Mesh(geo, objMat);
    T.obj.castShadow = true;
    T.obj.receiveShadow = true;
    T.obj.raycast = () => {};
    T.group.add(T.obj);
    // targets
    const tg = traj.targets || {};
    T.markers = [];
    const mk = (p, label, color) => {
      if (!Array.isArray(p)) return;
      const g = new THREE.Group();
      g.position.set(p[0], p[1], 0);
      const rr = Math.max(D * 0.75, 18);
      const ringMat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.85, depthWrite: false, toneMapped: false, side: THREE.DoubleSide });
      const ring = new THREE.Mesh(new THREE.RingGeometry(rr * 0.9, rr, 64), ringMat);
      ring.position.z = 0.6;
      const disc = new THREE.Mesh(new THREE.CircleGeometry(rr * 0.9, 64), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.1, depthWrite: false, toneMapped: false }));
      disc.position.z = 0.5;
      const ticks = new THREE.Group();
      for (let k = 0; k < 4; k++) {
        const t = new THREE.Mesh(new THREE.PlaneGeometry(rr * 0.35, 1.4), ringMat);
        const a = k * Math.PI / 2;
        t.position.set(Math.cos(a) * rr * 1.18, Math.sin(a) * rr * 1.18, 0.6);
        t.rotation.z = a;
        ticks.add(t);
      }
      const h = p[2] || 0;
      const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.5, Math.max(h, 1), 6).rotateX(Math.PI / 2),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.35, depthWrite: false, toneMapped: false }));
      stem.position.z = h / 2;
      const dot = new THREE.Mesh(new THREE.OctahedronGeometry(3.2), new THREE.MeshBasicMaterial({ color, toneMapped: false }));
      dot.position.z = h;
      const el = document.createElement('div');
      el.className = 'mv-tag';
      el.style.setProperty('--mv-c', color);
      el.textContent = label;
      const tag = new CSS2DObject(el);
      tag.position.set(0, 0, Math.max(h, 0) + rr * 0.6 + 14);
      g.add(disc, ring, ticks, stem, dot, tag);
      g.traverse((x) => { x.raycast = () => {}; x.renderOrder = 2; });
      T.group.add(g);
      T.markers.push(g);
    };
    mk(tg.pick, 'Pick', ACCENT);
    mk(tg.place, 'Place', GOOD);
    this._rebuildTrajFor(model);
    this._dirty = true;
  }

  // precompute TCP positions (depends on the robot model)
  _rebuildTrajFor(model) {
    const T = this._traj;
    if (!T) return;
    if (T.path) { disposeTree(T.path); T.path = null; }
    if (T.trail) { disposeTree(T.trail); T.trail = null; }
    const tcpName = (model.json.meta && model.json.meta.tcp_link) || 'tcp';
    const tmp = new Map();
    T.tcp = T.frames.map((f) => {
      const q = {};
      T.joints.forEach((n, i) => { q[n] = f.q ? f.q[i] : 0; });
      model.fkInto(model.resolve(q), 0, tmp);
      const M = tmp.get(tcpName) || tmp.get(model.order.length ? model.order[model.order.length - 1].child : model.rootLink);
      return new THREE.Vector3().setFromMatrixPosition(M);
    });
    // carrying flags: object moves with the TCP while the gripper holds it
    T.carry = T.frames.map((f) => (f.grip_n || 0) > T.maxGrip * 0.08 || ['grasp', 'lift', 'move', 'place'].includes(f.phase));
    // faint full path, coloured: blue free motion, orange while carrying
    const pos = [], col = [];
    const cFree = new THREE.Color(ACCENT), cCarry = new THREE.Color(HOT);
    for (let i = 1; i < T.tcp.length; i++) {
      const a = T.tcp[i - 1], b = T.tcp[i];
      pos.push(a.x, a.y, a.z, b.x, b.y, b.z);
      const c = T.carry[i] ? cCarry : cFree;
      col.push(c.r, c.g, c.b, c.r, c.g, c.b);
    }
    if (pos.length) {
      const g = new LineSegmentsGeometry();
      g.setPositions(pos);
      g.setColors(col);
      const m = new LineMaterial({ vertexColors: true, linewidth: 1.6, transparent: true, opacity: 0.5, depthWrite: false, dashed: true, dashSize: 5, gapSize: 4, toneMapped: false });
      m.resolution.set(this._w || 1, this._h || 1);
      this._lineMats.add(m);
      T.path = new LineSegments2(g, m);
      T.path.computeLineDistances();
      T.path.raycast = () => {};
      T.group.add(T.path);
    }
    // fading TCP trail (tube, alpha along its length)
    const trailMat = new THREE.ShaderMaterial({
      uniforms: { uColor: { value: new THREE.Color(ACCENT) }, uOpacity: { value: 0.95 } },
      transparent: true, depthWrite: false,
      vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
      fragmentShader: `uniform vec3 uColor; uniform float uOpacity; varying vec2 vUv;
        void main(){ float a = pow(vUv.x, 1.7) * uOpacity; gl_FragColor = vec4(uColor * (0.7 + 0.6 * vUv.x), a);
        #include <colorspace_fragment>
        }`,
    });
    T.trail = new THREE.Mesh(new THREE.BufferGeometry(), trailMat);
    T.trail.frustumCulled = false;
    T.trail.raycast = () => {};
    T.trail.renderOrder = 3;
    T.group.add(T.trail);
    T.trailR = Math.max(1.4, model.R * 0.006);
  }

  _clearTraj() {
    const T = this._traj;
    if (!T) return;
    T.playing = false;
    T.resolve && T.resolve(false);
    if (T.path) this._lineMats.delete(T.path.material);
    disposeTree(T.group);
    if (this.model) for (const r of this.model.parts) r.forceGlow = 0;
    this._traj = null;
    this._hud(null);
    this._dirty = true;
  }

  _sampleTraj(t) {
    const T = this._traj, F = T.frames;
    const tt = T.t0 + t;
    let lo = 0, hi = F.length - 1;
    if (tt <= (F[0].t ?? 0)) hi = 0;
    else if (tt >= (F[hi].t ?? 0)) lo = hi;
    else {
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if ((F[mid].t ?? 0) <= tt) lo = mid; else hi = mid; }
    }
    const a = F[lo], b = F[hi];
    const span = (b.t ?? 0) - (a.t ?? 0);
    const k = span > 1e-9 ? clamp((tt - (a.t ?? 0)) / span, 0, 1) : 0;
    return { lo, hi, k, a, b };
  }

  _applyTraj() {
    const T = this._traj;
    if (!T || !this.model) return;
    const { lo, hi, k, a, b } = this._sampleTraj(T.t);
    const q = {};
    T.joints.forEach((n, i) => { const va = a.q ? a.q[i] : 0, vb = b.q ? b.q[i] : va; q[n] = lerp(va, vb, k); });
    this.setJoints(q);
    // object pose
    if (a.obj && a.obj.length >= 3) {
      const ob = b.obj && b.obj.length >= 3 ? b.obj : a.obj;
      T.obj.visible = true;
      T.obj.position.set(lerp(a.obj[0], ob[0], k), lerp(a.obj[1], ob[1], k), lerp(a.obj[2], ob[2], k));
      if (a.obj.length >= 7) {
        _q1.set(a.obj[4], a.obj[5], a.obj[6], a.obj[3]).normalize();
        _q2.set(ob[4] ?? a.obj[4], ob[5] ?? a.obj[5], ob[6] ?? a.obj[6], ob[3] ?? a.obj[3]).normalize();
        T.obj.quaternion.slerpQuaternions(_q1, _q2, k);
      }
    } else T.obj.visible = false;
    // trail: last ~1.6 s of TCP positions + the interpolated current point
    const tNow = T.t0 + T.t;
    const pts = [];
    for (let i = lo; i >= 0 && (T.frames[i].t ?? 0) >= tNow - 1.6; i--) pts.unshift(T.tcp[i]);
    const cur = new THREE.Vector3().lerpVectors(T.tcp[lo], T.tcp[hi], k);
    pts.push(cur);
    if (T.trail.geometry) T.trail.geometry.dispose();
    const dedup = pts.filter((p, i) => i === 0 || p.distanceToSquared(pts[i - 1]) > 0.01);
    if (dedup.length >= 2) {
      const curve = new THREE.CatmullRomCurve3(dedup, false, 'centripetal');
      T.trail.geometry = new THREE.TubeGeometry(curve, Math.min(160, dedup.length * 3), T.trailR, 6, false);
      T.trail.visible = true;
    } else { T.trail.geometry = new THREE.BufferGeometry(); T.trail.visible = false; }
    const carrying = T.carry[lo];
    T.trail.material.uniforms.uColor.value.set(carrying ? HOT : ACCENT);
    // force-sensing pads glow with grip force
    const grip = lerp(a.grip_n || 0, b.grip_n || 0, k);
    const fg = clamp(grip / T.maxGrip, 0, 1);
    for (const r of this.model.parts) r.forceGlow = (r.kind === 'pad' || r.kind === 'sensor') ? fg : 0;
    const phase = a.phase || '';
    const info = { t: T.t, duration: T.duration, index: lo, phase, grip_n: grip, tau: a.tau, q, frame: a };
    this._hud(T.src.title || pretty(T.src.scenario || 'Simulation'),
      `${phase ? phase.toUpperCase() + ' · ' : ''}${T.t.toFixed(1)} / ${T.duration.toFixed(1)} s${grip > 0.01 ? ` · grip ${grip.toFixed(1)} N` : ''}`,
      carrying ? HOT : ACCENT);
    if (T.onFrame) { try { T.onFrame(info); } catch (e) { console.error(e); } }
    this._emit('frame', info);
    this._dirty = true;
  }

  // ------------------------------------------------------------ workspace
  showWorkspace(points, { shell = true, pointSize = null } = {}) {
    this._clearWorkspace();
    if (!points || !points.length) return;
    const n = points.length;
    const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
    let zmin = Infinity, zmax = -Infinity, rmax = 0;
    for (let i = 0; i < n; i++) {
      const p = points[i];
      pos[i * 3] = p[0]; pos[i * 3 + 1] = p[1]; pos[i * 3 + 2] = p[2];
      zmin = Math.min(zmin, p[2]); zmax = Math.max(zmax, p[2]);
      rmax = Math.max(rmax, Math.hypot(p[0], p[1]));
    }
    for (let i = 0; i < n; i++) {
      ramp((pos[i * 3 + 2] - zmin) / Math.max(1, zmax - zmin), _c1);
      col[i * 3] = _c1.r; col[i * 3 + 1] = _c1.g; col[i * 3 + 2] = _c1.b;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(col, 3));
    const R = this.model ? this.model.R : 300;
    const size = pointSize || clamp(Math.cbrt((Math.PI * 4 / 3 * Math.pow(rmax, 3)) / n) * 0.42, 2.5, R * 0.03);
    const ws = this._ws = { group: new THREE.Group(), u: {
      uSize: { value: size }, uScale: { value: 1 }, uReveal: { value: 0 }, uZmin: { value: zmin }, uZmax: { value: zmax }, uOpacity: { value: 0.9 } } };
    ws.group.name = 'workspace';
    const mat = new THREE.ShaderMaterial({
      uniforms: ws.u, transparent: true, depthWrite: false,
      vertexShader: `attribute vec3 aColor; uniform float uSize; uniform float uScale; uniform float uReveal; uniform float uZmin; uniform float uZmax;
        varying vec3 vC; varying float vA;
        void main(){
          float h = (position.z - uZmin) / max(uZmax - uZmin, 1.0);
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_Position = projectionMatrix * mv;
          float rv = smoothstep(h - 0.12, h, uReveal * 1.12);
          gl_PointSize = max(1.5, uSize * uScale / -mv.z * (0.3 + 0.7 * rv));
          vC = aColor; vA = rv;
        }`,
      fragmentShader: `uniform float uOpacity; varying vec3 vC; varying float vA;
        void main(){
          vec2 d = gl_PointCoord - 0.5; float r = length(d);
          if (r > 0.5) discard;
          float a = smoothstep(0.5, 0.28, r) * vA * uOpacity;
          gl_FragColor = vec4(vC * (0.9 + 0.5 * (0.5 - r)), a);
          #include <colorspace_fragment>
        }`,
    });
    const pts = new THREE.Points(g, mat);
    pts.raycast = () => {};
    pts.frustumCulled = false;
    ws.group.add(pts);
    if (shell && n >= 60) this._buildShell(points, ws, zmin, zmax);
    // reach ring on the floor
    const ringG = new THREE.BufferGeometry().setFromPoints(Array.from({ length: 181 }, (_, i) => { const a = i / 180 * Math.PI * 2; return new THREE.Vector3(Math.cos(a) * rmax, Math.sin(a) * rmax, 0.8); }));
    const ring = new THREE.Line(ringG, new THREE.LineDashedMaterial({ color: ACCENT, dashSize: rmax * 0.03, gapSize: rmax * 0.02, transparent: true, opacity: 0.0, depthWrite: false, toneMapped: false }));
    ring.computeLineDistances();
    ring.raycast = () => {};
    ws.group.add(ring);
    ws.ring = ring;
    const el = document.createElement('div');
    el.className = 'mv-tag ghosty';
    el.textContent = `Reach ${Math.round(rmax)} mm`;
    const tag = new CSS2DObject(el);
    const ta = -Math.PI * 0.3;
    tag.position.set(Math.cos(ta) * rmax, Math.sin(ta) * rmax, 0);
    ws.group.add(tag);
    ws.tag = tag;
    el.style.opacity = '0';
    el.style.transition = 'opacity .5s ease .6s';
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    this.overlays.add(ws.group);
    this._resizeOverlays();
    this._tween('ws', 1500, Ease.inOutSine, (k) => {
      ws.u.uReveal.value = k;
      if (ws.shellU) ws.shellU.uOpacity.value = clamp(k * 1.4 - 0.4, 0, 1);
      ring.material.opacity = 0.7 * k;
      this._dirty = true;
    });
    this._dirty = true;
  }

  _buildShell(points, ws, zmin, zmax) {
    // cylindrical bins -> max radius per (azimuth, height): a cheap outer envelope surface
    const A = 48, Hn = 18;
    const rmax = new Float32Array(A * Hn).fill(-1);
    for (const p of points) {
      const a = Math.floor(((Math.atan2(p[1], p[0]) + Math.PI) / (2 * Math.PI)) * A) % A;
      const h = Math.min(Hn - 1, Math.floor((p[2] - zmin) / Math.max(1, zmax - zmin) * Hn));
      const r = Math.hypot(p[0], p[1]);
      if (r > rmax[a * Hn + h]) rmax[a * Hn + h] = r;
    }
    // light smoothing (max-preserving average with neighbours)
    const sm = new Float32Array(A * Hn).fill(-1);
    for (let a = 0; a < A; a++) for (let h = 0; h < Hn; h++) {
      if (rmax[a * Hn + h] < 0) continue;
      let s = 0, w = 0;
      for (let da = -1; da <= 1; da++) for (let dh = -1; dh <= 1; dh++) {
        const aa = (a + da + A) % A, hh = h + dh;
        if (hh < 0 || hh >= Hn) continue;
        const v = rmax[aa * Hn + hh];
        if (v < 0) continue;
        const ww = (da === 0 && dh === 0) ? 2 : 1;
        s += v * ww; w += ww;
      }
      sm[a * Hn + h] = Math.max(s / w, rmax[a * Hn + h] * 0.96);
    }
    const verts = [], idx = [], vid = new Int32Array(A * Hn).fill(-1);
    for (let a = 0; a < A; a++) for (let h = 0; h < Hn; h++) {
      const r = sm[a * Hn + h];
      if (r < 0) continue;
      const th = (a + 0.5) / A * 2 * Math.PI - Math.PI;
      const z = zmin + (h + 0.5) / Hn * (zmax - zmin);
      vid[a * Hn + h] = verts.length / 3;
      verts.push(Math.cos(th) * r, Math.sin(th) * r, z);
    }
    for (let a = 0; a < A; a++) for (let h = 0; h < Hn - 1; h++) {
      const a2 = (a + 1) % A;
      const i00 = vid[a * Hn + h], i10 = vid[a2 * Hn + h], i01 = vid[a * Hn + h + 1], i11 = vid[a2 * Hn + h + 1];
      if (i00 < 0 || i10 < 0 || i01 < 0 || i11 < 0) continue;
      idx.push(i00, i10, i11, i00, i11, i01);
    }
    if (!idx.length) return;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    ws.shellU = { uColor: { value: new THREE.Color(ACCENT) }, uOpacity: { value: 0 } };
    const mat = new THREE.ShaderMaterial({
      uniforms: ws.shellU, transparent: true, depthWrite: false, side: THREE.DoubleSide,
      vertexShader: `varying vec3 vN; varying vec3 vV;
        void main(){ vN = normalize(normalMatrix * normal); vec4 mv = modelViewMatrix * vec4(position, 1.0); vV = -mv.xyz; gl_Position = projectionMatrix * mv; }`,
      fragmentShader: `uniform vec3 uColor; uniform float uOpacity; varying vec3 vN; varying vec3 vV;
        void main(){ float f = 1.0 - abs(dot(normalize(vN), normalize(vV))); float a = (0.035 + 0.30 * pow(f, 2.6)) * uOpacity;
          gl_FragColor = vec4(uColor, a);
          #include <colorspace_fragment>
        }`,
    });
    const shell = new THREE.Mesh(g, mat);
    shell.raycast = () => {};
    shell.renderOrder = 4;
    ws.group.add(shell);
    // contour rings
    const lm = new THREE.LineBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.22, depthWrite: false, toneMapped: false });
    for (let h = 1; h < Hn; h += 3) {
      const pts = [];
      let full = true;
      for (let a = 0; a <= A; a++) {
        const i = vid[(a % A) * Hn + h];
        if (i < 0) { full = false; break; }
        pts.push(new THREE.Vector3(verts[i * 3], verts[i * 3 + 1], verts[i * 3 + 2]));
      }
      if (!full) continue;
      const ln = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lm);
      ln.raycast = () => {};
      ws.group.add(ln);
    }
  }

  _clearWorkspace() {
    if (!this._ws) return;
    this._cancelTween('ws');
    disposeTree(this._ws.group);
    this._ws = null;
    this._dirty = true;
  }

  clearOverlays() {
    this._clearWorkspace();
    this._clearTraj();
    this._dirty = true;
  }

  // ------------------------------------------------------------ ghost (previous revision)
  async setGhost(src) {
    const token = this._ghostToken = (this._ghostToken || 0) + 1;
    const old = this.ghost;
    this.ghost = null;
    if (old) {
      this._tween(`ghost:${old.root.uuid}`, 350, Ease.inOutSine, (k) => this._setGhostOpacity(old, 1 - k)).then(() => old.dispose());
    }
    if (!src) { this._dirty = true; return null; }
    const { json, gltf } = await this._fetchRobot(src);
    if (token !== this._ghostToken) return null;
    const g = new RobotModel(json, gltf, { ghost: true, ghostColor: this.theme.ghost, edges: true });
    this.ghost = g;
    this.robotFrame.add(g.root);
    this._setGhostOpacity(g, 0);
    this._syncGhost();
    this._tween(`ghost:${g.root.uuid}`, 600, Ease.inOutSine, (k) => this._setGhostOpacity(g, k));
    this._dirty = true;
    return json;
  }

  _setGhostOpacity(g, k) {
    const [fill, edge] = g.ghostMats;
    if (fill) fill.opacity = 0.045 * k;
    if (edge) edge.opacity = 0.5 * k;
    g.root.visible = k > 0.002;
    this._dirty = true;
  }

  _syncGhost() {
    if (!this.ghost) return;
    const q = {};
    for (const j of this.ghost.joints) if (this.ghost.isDriven(j.name)) q[j.name] = Number.isFinite(this.q[j.name]) ? this.q[j.name] : j.home;
    this.ghost.pose(this.ghost.resolve(q), 0);
  }

  // ------------------------------------------------------------ camera
  _selectionBox(selector, { atRest = false } = {}) {
    const box = new THREE.Box3();
    if (!this.model) return box;
    const match = this._matcher(selector);
    const off = new THREE.Vector3();
    for (const r of this.model.parts) {
      if (match ? !match(r) : (r.hidden || r.alphaStep < 0.05)) continue;
      off.copy(r.basePos).addScaledVector(r.explode, this._explode.target);
      if (!atRest) off.add(r.stepOffset);
      box.union(this.model.partBox(r, _box2, off));
    }
    if (box.isEmpty()) return box;
    return box.applyMatrix4(this.robotFrame.matrixWorld);
  }

  frame(selector = null, { ms = 900, minRadius = 0, atRest = false, dir = null } = {}) {
    let box = this._selectionBox(selector, { atRest });
    if (box.isEmpty()) box = this._selectionBox(null);
    if (box.isEmpty()) return Promise.resolve(false);
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const rad = Math.max(sphere.radius, minRadius, 10);
    const dist = this._fitDistance(rad);
    return this._flyTo(sphere.center, dist, dir, ms);
  }

  _fitDistance(rad) {
    const fov = this.camera.fov * DEG;
    const aspect = this.camera.aspect || 1;
    const fh = rad / Math.sin(fov / 2);
    const fw = rad / Math.sin(Math.atan(Math.tan(fov / 2) * aspect));
    return Math.max(fh, fw) * 1.08;
  }

  _flyTo(target, dist, dir = null, ms = 900) {
    const ctl = this.controls, cam = this.camera;
    const t0 = ctl.target.clone(), p0 = cam.position.clone();
    const d0 = p0.distanceTo(t0);
    const dir0 = p0.clone().sub(t0).normalize();
    const dir1 = (dir ? dir.clone() : dir0.clone()).normalize();
    if (ms <= 0) {
      ctl.target.copy(target);
      cam.position.copy(target).addScaledVector(dir1, dist);
      this._dirty = true;
      return Promise.resolve(true);
    }
    const dtmp = new THREE.Vector3();
    return this._tween('camera', ms, Ease.inOutCubic, (k) => {
      ctl.target.lerpVectors(t0, target, k);
      dtmp.copy(dir0).lerp(dir1, k).normalize();
      cam.position.copy(ctl.target).addScaledVector(dtmp, lerp(d0, dist, k));
      this._dirty = true;
    });
  }

  _defaultDir(az = 38, el = 19) {
    return new THREE.Vector3(Math.sin(az * DEG) * Math.cos(el * DEG), Math.sin(el * DEG), Math.cos(az * DEG) * Math.cos(el * DEG));
  }

  _introCamera() {
    const box = this._selectionBox(null);
    if (box.isEmpty()) return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const dist = this._fitDistance(sphere.radius * 1.02);
    const target = sphere.center.clone();
    target.y = Math.max(target.y, sphere.radius * 0.5);
    if (this.opts.intro) {
      this.controls.target.copy(target).add(new THREE.Vector3(0, sphere.radius * 0.25, 0));
      this.camera.position.copy(this.controls.target).addScaledVector(this._defaultDir(-28, 34), dist * 1.9);
      this._flyTo(target, dist, this._defaultDir(), 1900);
    } else {
      this.controls.target.copy(target);
      this.camera.position.copy(target).addScaledVector(this._defaultDir(), dist);
    }
    this._dirty = true;
  }

  // ------------------------------------------------------------ misc public
  snapshot({ type = 'image/png', quality } = {}) {
    this._render();
    return this.renderer.domElement.toDataURL(type, quality);
  }

  resize() {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    this._w = w; this._h = h;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.labelRenderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this._resizeOverlays();
    this._dirty = true;
    this._labels.forEach((L) => { L.w = 0; });
  }

  _resizeOverlays() {
    for (const m of this._lineMats) m.resolution.set(this._w, this._h);
    if (this._ws) this._ws.u.uScale.value = (this._h * this.renderer.getPixelRatio()) / (2 * Math.tan(this.camera.fov * DEG / 2));
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    this._stopMotionNow();
    this.clearOverlays();
    this._clearLabels();
    for (const m of this._models) m.dispose();
    if (this.ghost) this.ghost.dispose();
    this.controls.dispose();
    this.renderer.dispose();
    this._envTex.dispose();
    this._bgTex.dispose();
    this.renderer.domElement.remove();
    this.labelRenderer.domElement.remove();
    this._hudEl.remove();
  }

  _hud(title, sub = '', color = ACCENT) {
    if (!this.opts.hud) return;
    const el = this._hudEl;
    if (!title) { el.classList.remove('on'); return; }
    el.style.setProperty('--mv-c', color);
    const b = el.querySelector('b'), s = el.querySelector('span');
    if (b.textContent !== title) b.textContent = title;
    if (s.textContent !== sub) s.textContent = sub;
    el.classList.add('on');
  }

  _pick(e) {
    if (!this.model) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    const rc = new THREE.Raycaster();
    rc.setFromCamera(ndc, this.camera);
    const meshes = [];
    for (const r of this.model.parts) if (r.node.visible && r.op > 0.2) meshes.push(...r.meshes);
    const hit = rc.intersectObjects(meshes, false)[0];
    const rec = hit && hit.object.userData.part;
    this._emit('select', rec ? { part: rec.name, kind: rec.kind, link: rec.link, info: rec.info, point: hit.point.clone() } : null);
  }

  // ------------------------------------------------------------ tweens
  _tween(key, ms, ease, fn) {
    key = key ?? `anon${this._anon++}`;
    const prev = this._tweens.get(key);
    if (prev) { this._tweens.delete(key); prev.resolve(false); }
    return new Promise((resolve) => {
      if (!(ms > 0)) { fn(1, 1); resolve(true); this._dirty = true; return; }
      this._tweens.set(key, { t0: performance.now(), ms, ease, fn, resolve });
      this._dirty = true;
    });
  }

  _cancelTween(key) {
    const tw = this._tweens.get(key);
    if (tw) { this._tweens.delete(key); tw.resolve(false); }
  }

  _runTweens(now) {
    if (!this._tweens.size) return false;
    for (const [key, tw] of [...this._tweens]) {
      if (this._tweens.get(key) !== tw) continue;
      const u = clamp((now - tw.t0) / tw.ms, 0, 1);
      try { tw.fn(tw.ease(u), u); } catch (e) { console.error(e); }
      if (u >= 1 && this._tweens.get(key) === tw) { this._tweens.delete(key); tw.resolve(true); }
    }
    return true;
  }

  // ------------------------------------------------------------ per-frame update
  _loop(now) {
    this._raf = requestAnimationFrame(this._loop);
    const dt = clamp((now - this._last) / 1000, 0, 0.1);
    this._last = now;
    this._time += dt;
    if (!this._w || !this._h) return;
    let active = this._runTweens(performance.now());
    active = this._update(dt) || active;
    // auto-orbit: eased in after the intro / after the user has been idle for a while
    const ctl = this.controls;
    if (this._orbitOn && !this._userActive && !this._tweens.has('camera')) {
      const idle = performance.now() - this._idleT;
      if (idle > (this._idleT ? this.opts.idleResumeMs : 0)) this._orbitK = Math.min(1, this._orbitK + dt * 0.35);
    } else if (this._tweens.has('camera')) this._orbitK = 0;
    ctl.autoRotate = this._orbitOn && this._orbitK > 0;
    ctl.autoRotateSpeed = this.opts.orbitSpeed * Ease.inOutSine(this._orbitK) * 60;  // OrbitControls uses (2pi/60 * speed) * dt
    const changed = ctl.update(dt);
    if (active || changed || this._dirty || ctl.autoRotate) {
      this._render();
      this._dirty = false;
    }
  }

  _update(dt) {
    let active = false;
    const model = this.model;
    // trajectory clock
    const T = this._traj;
    if (T && T.playing) {
      T.t += dt * T.speed;
      if (T.t >= T.duration) {
        if (T.loop) T.t %= T.duration;
        else { T.t = T.duration; T.playing = false; T.resolve && T.resolve(true); this._emit('end', { t: T.t }); }
      }
      this._applyTraj();
      active = true;
    }
    // explode spring
    const E = this._explode;
    if (Math.abs(E.cur - E.target) > 1e-4) {
      E.cur = damp(E.cur, E.target, 7, dt);
      if (Math.abs(E.cur - E.target) < 1e-4) E.cur = E.target;
      this._poseDirty = this._partsDirty = this._cablesDirty = true;
      active = true;
    }
    const X = this._xray;
    if (Math.abs(X.cur - X.target) > 1e-4) {
      X.cur = damp(X.cur, X.target, 8, dt);
      if (Math.abs(X.cur - X.target) < 1e-3) X.cur = X.target;
      this._partsDirty = this._cablesDirty = true;
      active = true;
    }
    if (model && this._poseDirty) {
      const vals = model.resolve(this.q);
      for (const m of this._models) if (m !== model) { const v2 = m.resolve(this.q); m.pose(v2, E.cur); }
      model.pose(vals, E.cur);
      this._syncGhost();
      this._poseDirty = false;
      this._cablesDirty = true;
      active = true;
    }
    if (this._partsDirty) {
      for (const m of this._models) { this._applyParts(m); this._cableOpacity(m); }
      this._partsDirty = false;
      active = true;
    }
    if (this._cablesDirty) {
      for (const m of this._models) this._updateCables(m);
      this._cablesDirty = false;
    }
    // glow channels (highlight / step / motion / force), smoothed + gentle pulse
    for (const m of this._models) if (this._updateGlow(m, dt)) active = true;
    if (this._gizmo) {
      this._gizmo.traverse((o) => { if (o.userData.spin) o.rotateZ(dt * 0.9); });
      active = true;
    }
    return active;
  }

  _applyParts(model) {
    const e = this._explode.cur, xr = this._xray.cur;
    for (const r of model.parts) {
      r.node.position.copy(r.basePos).addScaledVector(r.explode, e).add(r.stepOffset);
      const xa = XRAY_KINDS.has(r.kind) ? lerp(1, 0.12, xr) : 1;
      const op = clamp(model.fade * r.alphaStep * xa, 0, 1);
      r.u.uFres.value = XRAY_KINDS.has(r.kind) ? xr * 0.55 * model.fade * r.alphaStep : 0;
      if (Math.abs(op - r.op) > 1e-4) {
        r.op = op;
        const transparent = op < 0.999;
        for (const m of r.mats) {
          if (m.transparent !== transparent) { m.transparent = transparent; m.needsUpdate = true; }
          m.opacity = op;
          m.depthWrite = op > 0.6;
        }
        for (const ms of r.meshes) ms.castShadow = op > 0.45;
      }
      r.node.visible = op > 0.004 && !(r._stepState === 'hidden' && r.alphaStep < 0.01);
      // edges: fade with the part; brighter, bluish "blueprint" lines in x-ray
      const ea = model.fade * r.alphaStep;
      for (const em of r.edgeMats) {
        const b = em.userData.base;
        let o = b.opacity * ea;
        if (XRAY_KINDS.has(r.kind) && xr > 0) {
          em.color.copy(b.color).lerp(_c1.set('#8ec3f5'), xr);
          o = lerp(o, 0.42 * ea, xr);
        } else if (r.glow < 0.01) em.color.copy(b.color);
        em.opacity = o;
      }
    }
  }

  _updateGlow(model, dt) {
    let any = false;
    const pulse = 0.82 + 0.18 * Math.sin(this._time * 3.2);
    const xr = this._xray.cur;
    for (const r of model.parts) {
      let target = 0, color = null, pulsing = false;
      if (r.hl > 0) { target = 0.85; color = r.hlColor; pulsing = true; }
      else if (r.stepGlow > 0) { target = 0.9; color = _c1.set(this.opts.stepColor); pulsing = true; }
      else if (r.forceGlow > 0.01) { target = 1.3 * r.forceGlow; color = _c1.set(HOT); }
      else if (r.motionGlow > 0) { target = 0.32; color = _c1.set(ACCENT); }
      else if (xr > 0 && ELEC_KINDS.has(r.kind)) { target = 0.28 * xr; color = _c1.set(GOOD); }
      if (color) r.glowColor.copy(color);
      r.glow = damp(r.glow, target, 9, dt);
      if (r.glow < 1e-3 && target === 0) r.glow = 0;
      const val = r.glow * (pulsing ? pulse : 1) * model.fade;
      r.u.uGlow.value = val;
      r.u.uGlowColor.value.copy(r.glowColor);
      // tint edges toward the glow colour for a crisp "selected" outline
      if (r.glow > 0.01) {
        for (const em of r.edgeMats) { em.color.copy(em.userData.base.color).lerp(r.glowColor, clamp(r.glow, 0, 1) * 0.85); em.opacity = Math.max(em.opacity, 0.55 * r.glow * model.fade * r.alphaStep); }
      }
      if (r.glow > 0 || target > 0) any = true;
    }
    return any;
  }

  // cable tubes: geometry follows the pose (rebuilt only when the pose changes)
  _updateCables(model) {
    for (const c of model.cables) {
      const pts = [];
      for (const p of c.info.points) {
        const M = model.T.get(p.link);
        if (M) pts.push(new THREE.Vector3(...p.xyz).applyMatrix4(M));
      }
      c.ok = pts.length >= 2;
      if (!c.ok) continue;
      const curve = new THREE.CatmullRomCurve3(pts, false, 'centripetal', 0.5);
      const len = curve.getLength();
      c.mesh.geometry.dispose();
      c.mesh.geometry = new THREE.TubeGeometry(curve, clamp(Math.round(len / 5), 24, 220), c.r, 10, false);
      [0, 1].forEach((u, i) => {
        c.caps[i].position.copy(curve.getPointAt(u));
        c.caps[i].quaternion.setFromUnitVectors(Z, curve.getTangentAt(u));
      });
    }
  }

  // cable visibility follows robot fade, explode (cables tuck away when exploded) and assembly steps
  _cableOpacity(model) {
    const e = this._explode.cur, xr = this._xray.cur;
    for (const c of model.cables) {
      const op = model.fade * clamp(1 - e * 2.2, 0, 1) * (c.stepVisible === false ? 0 : 1);
      const vis = op > 0.01 && c.ok !== false;
      c.mesh.visible = vis;
      c.caps.forEach((m) => { m.visible = vis; });
      if (Math.abs(op - c.op) < 1e-4 && c.xr === xr) continue;
      c.op = op; c.xr = xr;
      const transparent = op < 0.999;
      for (const m of [c.mat, c.capMat]) {
        if (m.transparent !== transparent) { m.transparent = transparent; m.needsUpdate = true; }
        m.opacity = op;
      }
      // in x-ray the harness gets a warm tint so it reads through the translucent shells
      c.mat.emissive.set('#5a3a12').multiplyScalar(xr);
    }
  }

  _render() {
    if (!this._w || !this._h) return;
    this.renderer.render(this.scene, this.camera);
    this.labelRenderer.render(this.scene, this.camera);
    if (this._labels.length && this._labelsOn) this._layoutLabels();
    if (this._ws) this._ws.u.uScale.value = (this._h * this.renderer.getPixelRatio()) / (2 * Math.tan(this.camera.fov * DEG / 2));
  }
}

export default RobotViewer;

// "Show Morph the job": a phone video or photos of the job done by hand -> what the robot should do, as editable cards.
// Frames are picked here in the browser (a hidden <video> and a canvas); only the chosen JPEG frames are sent, and only
// after the person presses Analyze. A clip this browser can't play can be handed to the Studio computer, whose ffmpeg
// extracts the frames and deletes the video. Nothing in the design changes until Apply, which lists every change first.
// Feature contract (RELEASE_CONTRACTS section 4): init(studio) adds a button to studio.mount.chatTools.

// ---- copy (the release memo's exact wording)
export const COPY = {
  consent: "Morph picks up to 8 still frames from this video, here in your browser. Only those frames, not the video or its sound, go to Claude (Anthropic) to work out the task. Anthropic deletes API data within 30 days unless it is flagged for a policy violation or must be kept by law, and doesn't train on these images. Leave out people who haven't agreed to be filmed, and screens or documents.",
  consentYes: 'Analyze with Claude',
  consentNo: 'Keep on this computer',
  results: 'Estimated from a few frames. Sizes and positions are approximate, especially with no ruler, card or known product in view. Nothing changes until you press Apply.',
  unplayable: "This browser can't play this video (often an iPhone HEVC .mov). Export it as H.264 MP4 (iPhone: Settings > Camera > Formats > Most Compatible), or let the computer running Morph extract the frames: the video is uploaded there, used once and deleted.",
  remote: 'This Studio runs on another computer over an unencrypted (http) connection. Others on your network could see what you upload.',
  subq: 'Second opinion (beta): also sends 4 frames to SubQ. SubQ keeps these requests, images included, until they are deleted, unless your SubQ organization has a retention limit or zero data retention.',
  local: "Kept on this computer: no frame leaves this browser. Fill in the cards (the measuring tool sizes the object from a frame); Apply sends only the values.",
  noFfmpeg: "The computer running Morph has no ffmpeg, so it can't extract the frames either: install ffmpeg there (or the imageio-ffmpeg package: Morph's doctor on that computer gives the command), or export the clip as H.264 MP4.",
};
const DEFAULTS = { count: 8, max: 12, longEdge: 1280, photoEdge: 2048, maxSeconds: 180, quality: 0.8, openMs: 10000, seekMs: 15000 };
const VIDEO_EXT = /\.(mp4|m4v|mov|qt|webm|mkv|avi|3gp|3g2|ogv)$/i, IMAGE_EXT = /\.(jpe?g|png|webp|gif|bmp|heic|heif|avif)$/i;
const CONSENT_KEY = 'morph.video.consent';
const ICON = {
  film: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  crop: '<path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M2 6h14a2 2 0 0 1 2 2v14"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  hand: '<path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V12M11 11V4.5a1.5 1.5 0 0 1 3 0V12M14 11.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-1a6 6 0 0 1-5-2.7L3.5 14a1.6 1.6 0 0 1 2.6-1.8L8 14"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
};
const TASKS = [['pick_place', 'Pick and place'], ['sort', 'Sort'], ['stack', 'Stack'], ['insert', 'Insert'], ['pour', 'Pour'], ['other', 'Other']];
const SHAPES = [['cylinder', 'Cylinder'], ['box', 'Box'], ['sphere', 'Sphere'], ['irregular', 'Irregular']];
const FRAGILITY = [['robust', 'Robust'], ['firm', 'Firm'], ['delicate', 'Delicate'], ['very_delicate', 'Very delicate']];
const SURFACES = [['dry', 'Dry'], ['smooth', 'Smooth'], ['wet', 'Wet']];
const SETTINGS = [['food', 'Food'], ['lab', 'Lab'], ['office', 'Office'], ['workshop', 'Workshop'], ['warehouse', 'Warehouse'], ['home', 'Home'], ['outdoor', 'Outdoor']];
const SCALES = { ruler: 'a ruler', card: 'a card', known_product: 'a known product', hand: 'a hand', none: 'no size reference' };
const REFS = [['Card', 85.6], ['A4 width', 210], ['Coin (euro)', 23.25]];
const TABLE = { x: [80, 400], y: [-300, 300] };  // the editor's pick/place ranges (mm, robot base frame)
const TABLE_VIEW = { S: 0.33, cx: 180, by: 196 };  // diagram px per mm, base position: the 500 mm ring fits

// ---- helpers
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const escH = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const svg = (paths, cls = 'sm') => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>`;
const r2 = (v) => Math.round(v * 100) / 100;
export const fmtT = (t) => (t == null ? '' : `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`);
export const isVideo = (f) => /^video\//.test(f?.type || '') || (!f?.type && VIDEO_EXT.test(f?.name || ''));
export const isImage = (f) => /^image\//.test(f?.type || '') || (!f?.type && IMAGE_EXT.test(f?.name || ''));
const rid = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('');
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } } };

export class UnplayableError extends Error {
  constructor(message, code = 'decode') { super(message); this.name = 'UnplayableError'; this.code = code; }
}
export class PhotoError extends Error {
  constructor(message) { super(message); this.name = 'PhotoError'; }
}

// waits for one media event; a media error or the timeout rejects (codecs this browser lacks fail in milliseconds)
function waitFor(el, type, ms) {
  return new Promise((resolve, reject) => {
    const done = (fn, v) => { clearTimeout(t); el.removeEventListener(type, ok); el.removeEventListener('error', bad); fn(v); };
    const ok = () => done(resolve);
    const bad = () => done(reject, new UnplayableError(`can't decode (${el.error?.code || '?'}${el.error?.message ? `: ${el.error.message}` : ''})`, 'decode'));
    const t = setTimeout(() => done(reject, new UnplayableError(`no ${type} after ${ms} ms`, 'timeout')), ms);
    el.addEventListener(type, ok);
    el.addEventListener('error', bad);
  });
}
const toBlob = (canvas, q) => new Promise((res, rej) => canvas.toBlob((b) => (b ? res(b) : rej(new Error('JPEG encoding failed'))), 'image/jpeg', q));
export const blobToB64 = (blob) => new Promise((res, rej) => {
  const fr = new FileReader();
  fr.onload = () => res(String(fr.result).split(',')[1] || '');
  fr.onerror = () => rej(fr.error);
  fr.readAsDataURL(blob);
});
const frameDone = () => Promise.race([new Promise((r) => requestAnimationFrame(() => r())), sleep(60)]);  // Safari paints after 'seeked'

// ---- frame extraction (exported for tests and for the brief engine's photo button)
async function grab(v, t, o) {
  if (Math.abs(v.currentTime - t) > 1e-3) {
    v.currentTime = t;
    await waitFor(v, 'seeked', o.seekMs);
  }
  await frameDone();
  const s = Math.min(1, o.longEdge / Math.max(v.videoWidth, v.videoHeight));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(v.videoWidth * s));
  c.height = Math.max(1, Math.round(v.videoHeight * s));
  c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);  // the browser applies the clip's rotation
  return { t_s: r2(t), blob: await toBlob(c, o.quality), w: c.width, h: c.height, src: 'browser' };
}

async function duration(v, o) {
  if (Number.isFinite(v.duration) && v.duration > 0) return v.duration;
  try {  // recorded in a browser: no duration in the header until the end has been seen
    v.currentTime = 1e7;
    await Promise.race([waitFor(v, 'seeked', 4000), waitFor(v, 'durationchange', 4000)]);
  } catch (e) { /* fall through */ }
  const d = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : (v.seekable?.length ? v.seekable.end(v.seekable.length - 1) : 0);
  return d > 0 ? d : o.maxSeconds;
}

// A video file -> {kind: "video", frames: [{t_s, blob, w, h, src}], duration, w, h, video, url, ms}: `count` frames at
// the middle of equal slices of the first `maxSeconds`, long edge <= longEdge, JPEG. Throws UnplayableError fast when
// this browser can't decode the clip; the caller keeps `video` (to add moments) and revokes `url` when done.
export async function extractFrames(file, opts = {}) {
  const o = { ...DEFAULTS, ...opts }, t0 = performance.now();
  if (isImage(file)) return { kind: 'photo', frames: [await photoFrame(file, opts)], duration: null, ms: Math.round(performance.now() - t0) };
  const count = Math.max(1, Math.min(o.count, o.max));
  const url = URL.createObjectURL(file);
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.preload = 'auto'; v.src = url;
  try {
    await waitFor(v, 'loadedmetadata', o.openMs);
    if (!v.videoWidth || !v.videoHeight) throw new UnplayableError('no picture in this file', 'decode');
    const d = await duration(v, o);
    if (v.readyState < 2) await waitFor(v, 'loadeddata', o.openMs);
    const span = Math.min(d, o.maxSeconds), frames = [];
    for (let i = 0; i < count; i++) {
      frames.push(await grab(v, Math.min(span * (i + 0.5) / count, Math.max(0, d - 0.05)), o));
      o.onProgress?.(i + 1, count);
    }
    return { kind: 'video', frames, duration: d, w: v.videoWidth, h: v.videoHeight, video: v, url, ms: Math.round(performance.now() - t0) };
  } catch (e) {
    v.removeAttribute('src'); v.load(); URL.revokeObjectURL(url);
    throw e instanceof UnplayableError ? e : new UnplayableError(String(e?.message || e), 'decode');
  }
}

// One frame from the open video at time t (the "add this moment" button)
export async function frameAt(video, t, opts = {}) { return grab(video, t, { ...DEFAULTS, ...opts }); }

// A photo -> one frame: drawn upright (EXIF orientation applied) and re-saved as JPEG, which drops EXIF, GPS and the rest
export async function photoFrame(file, opts = {}) {
  const o = { ...DEFAULTS, ...opts }, edge = opts.longEdge || o.photoEdge;
  let src = null, url = null;
  try {
    src = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch (e) {
    url = URL.createObjectURL(file);
    const img = new Image();
    img.src = url;
    try { await img.decode(); src = img; } catch (e2) { URL.revokeObjectURL(url); throw new PhotoError("This browser can't open this photo (often an iPhone HEIC). Export it as JPEG, or take a screenshot of it."); }
  }
  const w0 = src.naturalWidth || src.width, h0 = src.naturalHeight || src.height, s = Math.min(1, edge / Math.max(w0, h0));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w0 * s)); c.height = Math.max(1, Math.round(h0 * s));
  c.getContext('2d').drawImage(src, 0, 0, c.width, c.height);
  src.close?.();
  if (url) URL.revokeObjectURL(url);
  return { t_s: null, blob: await toBlob(c, o.quality), w: c.width, h: c.height, src: 'photo', name: String(file.name || '').slice(0, 80) };
}

// The crop box (fractions of the frame) applied to one frame -> a JPEG blob of just that region
export async function cropFrame(frame, crop, quality = DEFAULTS.quality) {
  if (!crop) return frame.blob;
  const bmp = await createImageBitmap(frame.blob);
  const sx = Math.round(crop.x * bmp.width), sy = Math.round(crop.y * bmp.height);
  const sw = Math.max(1, Math.round(crop.w * bmp.width)), sh = Math.max(1, Math.round(crop.h * bmp.height));
  const c = document.createElement('canvas');
  c.width = sw; c.height = sh;
  c.getContext('2d').drawImage(bmp, sx, sy, sw, sh, 0, 0, sw, sh);
  bmp.close?.();
  return toBlob(c, quality);
}

// Answers from a studio.api call, whether it resolves to parsed JSON or a fetch Response, or throws
async function call(fn) {
  let r;
  try { r = await fn(); } catch (e) { return { ok: false, status: e?.status || 0, error: e?.body?.error || e?.message || String(e) }; }
  if (r && typeof r.json === 'function' && 'status' in r) {
    const body = await r.json().catch(() => ({}));
    return { ...body, status: r.status, ok: r.ok && body.ok !== false };
  }
  return r && typeof r === 'object' ? { status: 200, ...r, ok: r.ok !== false } : { ok: false, error: 'No answer from the Studio.' };
}

// Where the picture really is inside an <img> letterboxed by object-fit: contain (clicks and overlays map to this)
function contentRect(img) {
  const r = img.getBoundingClientRect();
  const iw = img.naturalWidth || +img.getAttribute('width'), ih = img.naturalHeight || +img.getAttribute('height');  // known before it loads
  if (!iw || !ih) return r;
  const s = Math.min(r.width / iw, r.height / ih), w = iw * s, h = ih * s;
  return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h };
}
function placeOverlay(stage) {
  const img = stage?.querySelector('.vj-img'), ov = stage?.querySelector('.vj-ov');
  if (!img || !ov) return;
  const go = () => {
    const c = contentRect(img), s = stage.getBoundingClientRect();
    Object.assign(ov.style, { left: `${c.left - s.left}px`, top: `${c.top - s.top}px`, width: `${c.width}px`, height: `${c.height}px` });
  };
  go();
  if (!img.complete) img.addEventListener('load', go, { once: true });
}
const mb = (bytes) => (bytes < 1048576 ? 'under 1 MB' : `${(bytes / 1048576).toFixed(0)} MB`);

const MAX_BODY = 16 << 20;  // the analyze route's request cap
function confTag(c, edited, manual) {
  if (edited === '@keep') return '<span class="tag neutral">kept as is</span>';
  if (edited === '@accept') return '<span class="tag acc">you confirmed this</span>';
  if (edited) return '<span class="tag acc">you set this</span>';
  if (c == null) return `<span class="tag neutral">${manual ? 'to fill in' : 'not seen'}</span>`;
  const p = Math.round(c * 100);
  return c >= 0.8 ? `<span class="tag good" title="Confidence ${p}%">high · ${p}%</span>`
    : c >= 0.6 ? `<span class="tag acc" title="Confidence ${p}%">medium · ${p}%</span>`
      : `<span class="tag warn" title="Under 60%: Morph asks before changing anything">low · ${p}%: check it</span>`;
}

// ---- the dialog
class VideoJob {
  constructor(studio) {
    this.s = studio;
    this.esc = studio.esc || escH;
    this.I = studio.I || ((n, size = 'sm') => svg(ICON[n] || '', size));
    this.onSlotsFn = null;
    this.el = document.createElement('div');
    this.el.className = 'glass dlg vjob';
    this.el.setAttribute('role', 'dialog');
    this.el.setAttribute('aria-label', 'Show Morph the job');
    this.input = Object.assign(document.createElement('input'), { type: 'file', accept: 'video/*,image/*', multiple: true, hidden: true });
    this.input.className = 'vj-file';
    this.input.addEventListener('change', () => {
      const fs = [...this.input.files];
      this.input.value = '';
      if (!fs.length) return;
      if (['loading', 'consent'].includes(this.view)) this.pending = fs;  // started once the consent is answered
      else this.start(fs);
    });
    this.reset();
    this.mountButton();
    window.addEventListener('resize', () => this.el.querySelectorAll('.vj-stage').forEach(placeOverlay));
  }

  reset() {
    this.cleanup?.();
    clearTimeout(this.pt);  // a list of changes still due for the last clip
    Object.assign(this, { files: [], kind: 'video', frames: [], sel: 0, marks: { grasp_s: null, release_s: null }, crop: null, cropping: false,
      video: null, url: null, duration: null, subq: false, result: null, mapping: null, answers: {}, why: '', jobId: null, view: null,
      busy: false, measure: null, photoIs: 'object', name: '', dirty: false });
  }

  cleanup() {
    for (const f of this.frames || []) if (f.thumb) URL.revokeObjectURL(f.thumb);
    if (this.video) { this.video.removeAttribute('src'); this.video.load(); }
    if (this.url) URL.revokeObjectURL(this.url);
    this.unsub?.();
    this.unsub = null;
  }

  mountButton() {
    const slot = this.s.mount?.chatTools;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn ghost vj-btn';
    b.title = 'Show Morph the job: a video or photos of the task, done by hand';
    b.innerHTML = `${svg(ICON.film)}<span>Show Morph the job</span>`;
    b.onclick = () => this.open();
    this.button = b;
    if (slot) { slot.appendChild(b); slot.appendChild(this.input); } else document.body.appendChild(this.input);
    const zone = slot?.closest?.('#chat, section, aside') || slot?.parentElement;
    if (zone) this.dropZone(zone);
  }

  dropZone(zone) {
    let depth = 0;
    const has = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
    const media = (e) => [...(e.dataTransfer?.items || [])].some((it) => it.kind === 'file' && /^(video|image)\//.test(it.type || ''));
    zone.addEventListener('dragenter', (e) => { if (has(e) && media(e)) { depth++; zone.classList.add('vj-drop'); } });
    zone.addEventListener('dragleave', () => { if (depth && --depth === 0) zone.classList.remove('vj-drop'); });
    zone.addEventListener('dragover', (e) => { if (has(e) && media(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
    zone.addEventListener('drop', (e) => {
      depth = 0; zone.classList.remove('vj-drop');
      const fs = [...(e.dataTransfer?.files || [])].filter((f) => isVideo(f) || isImage(f));
      if (!fs.length) return;  // not ours (a robot zip for the importer, ...)
      e.preventDefault(); e.stopPropagation();
      this.open(fs);
    });
    zone.dataset.vjDrop = '1';
  }

  // ---- public entry points
  async open(files) {
    this.reset();
    this.infoP = call(() => this.s.api.get('/api/video/info')).then((r) => (this.info = r.ok ? r
      : { claude: false, subq: false, ffmpeg: false, remote: false, manual_reason: `Couldn't reach the Studio (${r.error || 'no answer'}).` }));
    const choice = store.get(CONSENT_KEY);
    if (choice && !files) this.input.click();  // still inside the person's click, so the file chooser opens at once
    this.show();
    this.render('loading');
    await this.infoP;
    this.mode = !this.info.claude ? 'local' : choice === 'local' ? 'local' : choice === 'cloud' ? 'cloud' : null;
    this.pending = files || this.pending || null;
    if (!this.mode || (!this.info.claude && !store.get(CONSENT_KEY + '.local-seen'))) return this.render('consent');
    if (this.pending) return this.start(this.pending);
    if (this.view === 'loading') this.render('choose');
  }

  pick() {
    if (this.view === 'consent' || this.view === 'loading') this.render('choose');
    this.input.click();
  }

  afterConsent() {
    if (this.frames.length) return this.render('picker');  // changed mid-session: keep the frames
    return this.pending ? this.start(this.pending) : this.pick();
  }

  onSlots(fn) { this.onSlotsFn = fn; }

  show() {
    if (this.s.modal?.open) this.s.modal.open(this.el, { wide: true, onClose: () => this.cleanup() });
    else {
      if (!this.overlay) {
        this.overlay = document.createElement('div');
        this.overlay.className = 'modal vj-modal';
        this.overlay.addEventListener('click', (e) => { if (e.target === this.overlay) this.close(); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && this.overlay.classList.contains('on')) this.close(); });
        document.body.appendChild(this.overlay);
      }
      this.overlay.appendChild(this.el);
      this.overlay.classList.add('on');
    }
  }

  close() {
    this.jobId = null;  // an analysis still on its way is dropped
    this.cleanup();
    if (this.s.modal?.close && !this.overlay) this.s.modal.close();
    this.overlay?.classList.remove('on');
    this.view = null;
  }

  // ---- files -> frames
  async start(files) {
    this.cleanup();
    this.pending = null;
    await this.infoP;
    const vids = files.filter(isVideo), imgs = files.filter(isImage);
    if (!vids.length && !imgs.length) { this.toast('Choose a video or photos'); return; }
    this.files = vids.length ? [vids[0]] : imgs.slice(0, DEFAULTS.max);
    this.name = this.files.length === 1 ? this.files[0].name : `${this.files.length} photos`;
    this.frames = []; this.sel = 0;
    if (vids.length) {
      this.kind = 'video';
      this.render('extract', { done: 0, total: DEFAULTS.count });
      try {
        const out = await extractFrames(vids[0], { onProgress: (n, t) => this.view === 'extract' && this.progress(n, t) });
        Object.assign(this, { video: out.video, url: out.url, duration: out.duration, extractMs: out.ms });
        this.addFrames(out.frames);
      } catch (e) {
        if (e instanceof UnplayableError) return this.render('unplayable', { err: e });
        return this.render('error', { text: `Couldn't read that video (${e.message}).` });
      }
    } else {
      this.kind = imgs.length === 1 ? 'photo' : 'photos';
      this.render('extract', { done: 0, total: this.files.length, photos: true });
      const got = [];
      for (const [i, f] of this.files.entries()) {
        try { got.push(await photoFrame(f, this.kind === 'photo' ? {} : { longEdge: DEFAULTS.longEdge })); } catch (e) { this.toast(e.message || 'One photo could not be opened'); }
        this.progress(i + 1, this.files.length);
      }
      if (!got.length) return this.render('error', { text: "This browser can't open these photos (often iPhone HEIC). Export them as JPEG." });
      this.addFrames(got);
    }
    this.render('picker');
  }

  addFrames(list) {
    for (const f of list) {
      if (this.frames.length >= DEFAULTS.max) { this.toast(`At most ${DEFAULTS.max} frames`); break; }
      f.id = rid(); f.thumb = URL.createObjectURL(f.blob);
      this.frames.push(f);
    }
    this.frames.sort((a, b) => (a.t_s ?? 0) - (b.t_s ?? 0));
  }

  async uploadFallback() {
    const file = this.files[0];
    this.render('upload', { bytes: file.size });
    const id = rid();
    this.listen(id);
    const r = await call(() => this.s.api.put('/api/video/upload', file, { id, count: DEFAULTS.count }));
    this.unsub?.();
    if (!r.ok || !Array.isArray(r.frames)) return this.render('error', { text: r.error || "The Studio couldn't extract frames from this video." });
    const got = [];
    for (const f of r.frames) {
      const bin = atob(f.jpeg_b64), buf = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      got.push({ t_s: f.t_s, blob: new Blob([buf], { type: 'image/jpeg' }), w: f.w, h: f.h, src: 'ffmpeg' });
    }
    this.kind = 'video';
    this.duration = r.duration_s;
    this.addFrames(got);
    this.render('picker');
  }

  // ---- live events for one request
  listen(id) {
    this.unsub?.();
    const offs = [];
    if (typeof this.s.on === 'function') {
      const hit = (ev) => ev && (ev.id === id || (!ev.id && ev.role === 'vision'));
      offs.push(this.s.on('video', (ev) => { if (hit(ev)) this.onEvent(ev); }));
      offs.push(this.s.on('agent', (ev) => { if (hit(ev) && ev.role === 'vision') this.onEvent({ ...ev, type: 'agent' }); }));
    }
    this.unsub = () => offs.forEach((off) => typeof off === 'function' && off());
  }

  onEvent(ev) {
    const list = this.el.querySelector('.vj-prog');
    if (!list || !['analyzing', 'upload'].includes(this.view)) return;
    const key = ev.type === 'agent' ? `agent-${ev.provider}` : `video-${ev.stage}`;
    let row = list.querySelector(`[data-k="${CSS.escape(key)}"]`);
    if (!row) { row = document.createElement('div'); row.dataset.k = key; list.appendChild(row); }
    const icon = ev.status === 'done' ? this.I('check', 'sm') : ev.status === 'error' ? this.I('alert', 'sm') : ev.status === 'skipped' ? this.I('x', 'sm') : '<span class="spin"></span>';
    row.className = `vj-row ${ev.status || ''}`;
    const who = ev.provider === 'subq' ? 'SubQ Snap (AI)' : ev.provider === 'claude' ? 'Claude (AI)' : '';
    row.innerHTML = `${icon}<span>${who ? `<b>${who}</b> ` : ''}${this.esc(ev.text || '')}</span>${ev.ms ? `<span class="dim mono">${(ev.ms / 1000).toFixed(1)} s</span>` : ''}`;
  }

  progress(n, total) {
    const b = this.el.querySelector('.vj-bar b'), t = this.el.querySelector('.vj-pn');
    if (b) b.style.width = `${Math.round((100 * n) / total)}%`;
    if (t) t.textContent = `${n} of ${total}`;
  }

  toast(text) { (this.s.toast || ((t) => console.log(t)))(text); }

  // ---- views
  render(view, opts = {}) {
    const moved = this.view !== view;
    this.view = view;
    const E = this.esc, I = this.I;
    const head = (sub) => `<div class="dlg-h"><div class="kicon">${svg(ICON.film, '')}</div><div class="grow"><div class="t">Show Morph the job</div>
      <div class="s">${sub}</div></div><button class="btn icon ghost" type="button" data-a="close" title="Close (Esc)">${I('x', 'sm')}</button></div>`;
    const remote = this.info?.remote ? `<div class="kchk bad vj-remote">${I('alert', 'sm')}<span>${E(COPY.remote)}</span></div>` : '';
    let html = '';
    if (view === 'loading') html = head('Checking what this Studio can do…') + '<div class="skel" style="height:160px"></div>';
    else if (view === 'consent') {
      const noKey = !this.info.claude;
      html = head('A phone video or photos of the job, done by hand. Morph works out the task, the object and the layout, and suggests design changes for you to check.')
        + `<div class="vj-consent">${noKey ? `<p>${E(this.info.manual_reason || 'There is no Claude key on this computer.')}</p><p>${E(COPY.local)}</p>` : `<p>${E(COPY.consent)}</p>`}</div>${remote}
        <div class="dlg-f"><span class="grow"></span>${noKey ? `<button class="btn primary" type="button" data-a="local">${I('check', 'sm')}Choose a video or photos</button>`
          : `<button class="btn" type="button" data-a="local">${E(COPY.consentNo)}</button><button class="btn primary" type="button" data-a="cloud">${E(COPY.consentYes)}</button>`}</div>`;
    } else if (view === 'choose') {
      html = head(this.mode === 'local' ? 'Kept on this computer.' : 'Frames are picked here in your browser.') + `<div class="vj-choose">
        <button class="vj-pickbig" type="button" data-a="pick">${svg(ICON.upload, 'lg')}<b>Choose a video or photos</b><span>or drop them on the chat. MP4, MOV or WebM; JPEG, PNG or WebP photos.</span></button></div>${remote}`;
    } else if (view === 'extract') {
      html = head(opts.photos ? 'Re-saving the photos here in your browser (location data dropped)…' : 'Picking frames here in your browser…')
        + `<div class="vj-extract"><div class="vj-bar"><b style="width:0%"></b></div><span class="vj-pn mono">0 of ${opts.total}</span></div>`;
    } else if (view === 'unplayable') {
      const f0 = this.files[0], size = f0 ? mb(f0.size) : '?', capMb = this.info?.limits?.upload_mb || 500;
      const big = !!f0 && f0.size > capMb * 1048576, ff = this.info?.ffmpeg && !big;
      html = head(E(this.name)) + `<div class="kchk bad">${I('alert', 'sm')}<span>${E(COPY.unplayable)}</span></div>
        ${this.info?.ffmpeg ? '' : `<div class="kchk">${I('alert', 'sm')}<span>${E(COPY.noFfmpeg)}</span></div>`}
        ${big ? `<div class="kchk">${I('alert', 'sm')}<span>${E(`This video is ${size}: the Studio computer takes videos up to ${capMb} MB. Trim it, or export it as H.264 MP4.`)}</span></div>` : ''}${ff ? remote : ''}
        <div class="vj-why dim mono">${E(opts.err?.message || '')}</div>
        <div class="dlg-f"><button class="btn" type="button" data-a="pick">Choose another file</button><span class="grow"></span>
          ${ff ? `<button class="btn primary" type="button" data-a="upload">${svg(ICON.upload)}Extract on the Studio computer (${size})</button>` : ''}</div>`;
    } else if (view === 'upload') {
      html = head(`Uploading the video (${mb(opts.bytes)}) to the computer running Morph. Its ffmpeg extracts the frames, then deletes the video.`)
        + `${remote}<div class="vj-prog"><div class="vj-row" data-k="video-upload"><span class="spin"></span><span>Uploading…</span></div></div>`;
    } else if (view === 'error') {
      html = head(E(this.name || '')) + `<div class="kchk bad">${I('alert', 'sm')}<span>${E(opts.text)}</span></div>
        <div class="dlg-f"><button class="btn" type="button" data-a="pick">Choose another file</button><span class="grow"></span><button class="btn" type="button" data-a="close">Close</button></div>`;
    } else if (view === 'picker') html = this.pickerHTML(head, remote);
    else if (view === 'analyzing') {
      html = head(`Sending ${this.frames.length} frame${this.frames.length === 1 ? '' : 's'}${this.subq ? ' to Claude, and 4 to SubQ' : ' to Claude'}. This takes up to two minutes.`)
        + `<div class="vj-prog"><div class="vj-row" data-k="video-analyze"><span class="spin"></span><span>Sending the frames…</span></div></div>
        <div class="dlg-f"><span class="dim vj-el mono"></span><span class="grow"></span><button class="btn" type="button" data-a="cancel" title="Stops waiting here; an analysis already sent finishes on its own">Stop waiting</button></div>`;
    } else if (view === 'results') html = this.resultsHTML(head);
    else if (view === 'measure') html = this.measureHTML(head);
    else if (view === 'review') html = this.reviewHTML(head);
    this.el.innerHTML = html;
    if (moved) this.el.scrollTop = 0;  // a new step starts at its title, not where the last one was scrolled to
    this.bind();
    if (view === 'analyzing') this.tick();
  }

  bind() {
    const on = (a, fn) => this.el.querySelectorAll(`[data-a="${a}"]`).forEach((b) => (b.onclick = fn));
    on('close', () => this.close());
    on('cloud', () => { store.set(CONSENT_KEY, 'cloud'); this.mode = 'cloud'; this.afterConsent(); });
    on('local', () => {
      if (this.info.claude) store.set(CONSENT_KEY, 'local'); else store.set(CONSENT_KEY + '.local-seen', '1');
      this.mode = 'local'; this.afterConsent();
    });
    on('pick', () => this.pick());
    on('upload', () => this.uploadFallback());
    on('cancel', () => { this.jobId = null; this.unsub?.(); this.render('picker'); });
    if (this.view === 'picker') this.bindPicker();
    if (this.view === 'results') this.bindResults();
    if (this.view === 'measure') this.bindMeasure();
    if (this.view === 'review') this.bindReview();
  }

  async tick() {
    const t0 = performance.now(), id = this.jobId;
    while (this.view === 'analyzing' && this.jobId === id) {
      const el = this.el.querySelector('.vj-el');
      if (el) el.textContent = `${((performance.now() - t0) / 1000).toFixed(0)} s`;
      await sleep(500);
    }
  }

  // ---- picker: choose frames, mark grasp and release, crop
  pickerHTML(head, remote) {
    const E = this.esc, I = this.I, f = this.frames[this.sel] || this.frames[0];
    const n = this.frames.length, photos = this.kind !== 'video';
    const thumbs = this.frames.map((fr, i) => {
      const mk = [this.near('grasp_s') === i ? '<i class="g">grasp</i>' : '', this.near('release_s') === i ? '<i class="r">release</i>' : ''].join('');
      return `<button class="vj-th ${i === this.sel ? 'on' : ''}" type="button" data-sel="${i}" title="${fr.t_s != null ? `t = ${fr.t_s} s` : E(fr.name || 'photo')}">
        <img src="${fr.thumb}" alt="Frame ${i + 1}" draggable="false"><span class="n">${i + 1}</span><span class="t mono">${fr.t_s != null ? fmtT(fr.t_s) : 'photo'}</span>${mk}
        ${n > 1 ? `<span class="x" role="button" tabindex="0" data-rm="${i}" title="Leave this frame out">${I('x', 'sm')}</span>` : ''}</button>`;
    }).join('');
    const canAdd = this.kind === 'video' && this.video && n < DEFAULTS.max;
    const g = this.marks.grasp_s, r = this.marks.release_s;
    const markLine = photos ? '' : `<span class="vj-marks">${g != null ? `Grasp <b class="mono">${g.toFixed(1)} s</b>` : 'Grasp not marked'} · ${r != null ? `release <b class="mono">${r.toFixed(1)} s</b>` : 'release not marked'}${g != null && r != null && r > g ? ` · carry <b class="mono">${(r - g).toFixed(1)} s</b>` : ''}</span>`;
    const local = this.mode === 'local';
    const sub = this.info?.subq && !local;
    const kindLine = this.kind === 'photo' ? `<div class="pills vj-kind"><span class="lab">This photo shows</span><button type="button" class="${this.photoIs === 'object' ? 'on' : ''}" data-photo="object">the object to pick</button><button type="button" class="${this.photoIs === 'job' ? 'on' : ''}" data-photo="job">the job</button></div>` : '';
    return head(`${E(this.name)} · ${n} ${photos ? (n === 1 ? 'photo' : 'photos') : `frame${n === 1 ? '' : 's'}`}${this.duration ? ` from ${fmtT(this.duration)}` : ''}${this.frames[0]?.src === 'ffmpeg' ? ' (extracted on the Studio computer)' : this.kind === 'video' ? ', picked here in your browser' : ''}. ${this.mode === 'local' ? 'No frame leaves this browser: you fill in the cards.' : 'Nothing leaves this browser until you press Analyze.'}`)
      + `<div class="vj-pick">
        <div class="vj-stage ${this.cropping ? 'cropping' : ''}"><img class="vj-img" src="${f?.thumb || ''}" width="${f?.w || ''}" height="${f?.h || ''}" alt="Selected frame" draggable="false"><canvas class="vj-live" hidden></canvas>
          <div class="vj-ov">${this.crop ? `<div class="vj-crop" style="left:${this.crop.x * 100}%;top:${this.crop.y * 100}%;width:${this.crop.w * 100}%;height:${this.crop.h * 100}%"></div>` : ''}</div>
          ${this.cropping ? '<div class="vj-hint">Drag a box around the work area: it applies to every frame</div>' : ''}</div>
        <div class="vj-strip">${thumbs}${canAdd ? `<button class="vj-th add" type="button" data-a="addmoment" title="Add the moment shown on the slider">${svg(ICON.plus)}<span>Add moment</span></button>` : ''}</div>
        ${this.kind === 'video' && this.video ? `<div class="vj-scrub"><input type="range" min="0" max="${Math.min(this.duration || 0, DEFAULTS.maxSeconds).toFixed(2)}" step="0.05" value="${this.scrubT ?? f?.t_s ?? 0}" aria-label="Moment in the video"><span class="mono vj-st">${fmtT(this.scrubT ?? f?.t_s ?? 0)}</span></div>` : ''}
        <div class="vj-tools">${photos ? kindLine : `<button class="btn" type="button" data-a="grasp" title="The moment the hand closes on the object">${svg(ICON.hand)}Mark grasp</button><button class="btn" type="button" data-a="release" title="The moment the hand lets go">${I('target', 'sm')}Mark release</button>`}
          <button class="btn ${this.cropping ? 'on' : ''}" type="button" data-a="crop" title="One box for every frame: leave out people, screens and documents">${svg(ICON.crop)}${this.crop ? 'Redraw crop' : 'Crop'}</button>
          ${this.crop ? '<button class="btn ghost" type="button" data-a="nocrop">Clear crop</button>' : ''}<span class="grow"></span>${markLine}</div>
      </div>${remote}${sub && this.subq ? `<div class="vj-subqnote dim">${E(COPY.subq)}</div>` : ''}
      <div class="dlg-f">${local ? `<span class="vj-mode dim">${this.info?.claude ? `Kept on this computer · <a href="#" data-a="consent">change</a>` : `Kept on this computer (${this.info?.offline ? 'network lookups are off' : 'no Claude key'})`}</span>`
        : sub ? `<label class="vj-subq" title="${E(COPY.subq)}"><span class="switch"><input type="checkbox" data-a="subq" ${this.subq ? 'checked' : ''}><i></i></span>Second opinion (beta)</label>` : ''}
        <span class="grow"></span><span class="dim vj-count">${n} of up to ${DEFAULTS.max}</span>
        ${local ? `<button class="btn primary" type="button" data-a="manual">${I('check', 'sm')}Fill in the cards</button>`
          : `<button class="btn primary" type="button" data-a="analyze">${I('spark', 'sm')}Analyze ${n} ${photos ? (n === 1 ? 'photo' : 'photos') : `frame${n === 1 ? '' : 's'}`} with Claude</button>`}</div>`;
  }

  near(k) {
    const t = this.marks[k];
    if (t == null || !this.frames.length) return -1;
    let best = 0;
    this.frames.forEach((f, i) => { if (Math.abs((f.t_s ?? 0) - t) < Math.abs((this.frames[best].t_s ?? 0) - t)) best = i; });
    return best;
  }

  bindPicker() {
    const el = this.el;
    el.querySelectorAll('[data-sel]').forEach((b) => (b.onclick = (e) => {
      const rm = e.target.closest('[data-rm]');
      if (rm) { this.remove(+rm.dataset.rm); return; }
      this.sel = +b.dataset.sel; this.scrubT = null; this.render('picker');
    }));
    el.querySelectorAll('[data-rm]').forEach((x) => x.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.remove(+x.dataset.rm); } }));
    el.querySelectorAll('[data-photo]').forEach((b) => (b.onclick = () => { this.photoIs = b.dataset.photo; this.render('picker'); }));
    const on = (a, fn) => el.querySelectorAll(`[data-a="${a}"]`).forEach((b) => (b.onclick = fn));
    on('grasp', () => this.mark('grasp_s'));
    on('release', () => this.mark('release_s'));
    on('crop', () => { this.cropping = !this.cropping; this.render('picker'); });
    on('nocrop', () => { this.crop = null; this.cropping = false; this.render('picker'); });
    on('analyze', () => this.analyze());
    on('manual', () => this.manual());
    on('addmoment', () => this.addMoment());
    on('consent', (e) => { e.preventDefault(); store.set(CONSENT_KEY, ''); this.render('consent'); });
    const sq = el.querySelector('[data-a="subq"]');
    if (sq) sq.onchange = () => { this.subq = sq.checked; this.render('picker'); };
    const range = el.querySelector('.vj-scrub input');
    if (range) range.oninput = () => this.scrub(+range.value);
    if (range && this.scrubT != null) this.scrub(this.scrubT);  // keep showing the moment on the slider
    this.bindCrop(el.querySelector('.vj-stage'));
  }

  remove(i) {
    const [f] = this.frames.splice(i, 1);
    if (f?.thumb) URL.revokeObjectURL(f.thumb);
    this.sel = Math.min(this.sel, this.frames.length - 1);
    this.render('picker');
  }

  mark(k) {
    const t = this.scrubT ?? this.frames[this.sel]?.t_s;
    if (t == null) return;
    this.marks[k] = r2(t);
    if (this.marks.grasp_s != null && this.marks.release_s != null && this.marks.release_s <= this.marks.grasp_s) this.toast('Release comes after the grasp: check both marks');
    this.render('picker');
  }

  async scrub(t) {
    const v = this.video, c = this.el.querySelector('.vj-live'), img = this.el.querySelector('.vj-img'), lab = this.el.querySelector('.vj-st');
    if (!v || !c) return;
    this.scrubT = t;
    if (lab) lab.textContent = fmtT(t);
    if (this.seeking) return;  // the latest position is drawn once the current seek lands
    this.seeking = true;
    try {
      while (true) {
        const want = this.scrubT;
        v.currentTime = want;
        await waitFor(v, 'seeked', DEFAULTS.seekMs).catch(() => {});
        c.width = v.videoWidth; c.height = v.videoHeight;
        c.getContext('2d').drawImage(v, 0, 0);
        c.hidden = false; if (img) img.style.visibility = 'hidden';
        if (want === this.scrubT) break;
      }
    } finally { this.seeking = false; }
  }

  async addMoment() {
    if (!this.video || this.scrubT == null) { this.toast('Move the slider to the moment first'); return; }
    try {
      const f = await frameAt(this.video, this.scrubT);
      this.addFrames([f]);
      this.sel = this.frames.indexOf(f);
      this.scrubT = null;
      this.render('picker');
    } catch (e) { this.toast("Couldn't grab that moment"); }
  }

  bindCrop(stage) {
    placeOverlay(stage);
    if (!stage || !this.cropping) return;
    const ov = stage.querySelector('.vj-ov');
    let start = null, box = null;
    const pos = (e) => {
      const r = ov.getBoundingClientRect();
      return { x: Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1), y: Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1) };
    };
    stage.onpointerdown = (e) => {
      e.preventDefault();  // no text selection or image drag: the box follows the pointer
      start = pos(e); stage.setPointerCapture(e.pointerId);
      box = ov.querySelector('.vj-crop') || ov.appendChild(Object.assign(document.createElement('div'), { className: 'vj-crop' }));
    };
    stage.onpointermove = (e) => {
      if (!start) return;
      const p = pos(e), x = Math.min(start.x, p.x), y = Math.min(start.y, p.y), w = Math.abs(p.x - start.x), h = Math.abs(p.y - start.y);
      Object.assign(box.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
      this.draft = { x, y, w, h };
    };
    stage.onpointerup = stage.onpointercancel = () => {
      if (!start) return;
      start = null;
      if (this.draft && this.draft.w > 0.05 && this.draft.h > 0.05) { this.crop = this.draft; this.cropping = false; }
      this.draft = null;
      this.render('picker');
    };
  }

  // ---- analyze (Claude, optional SubQ) or fill in by hand
  async payload() {
    const frames = [];
    let size = 0;
    for (const f of this.frames) {
      const b = await cropFrame(f, this.crop);
      const jpeg_b64 = await blobToB64(b);
      size += jpeg_b64.length;
      frames.push({ t_s: f.t_s, jpeg_b64 });
    }
    if (size > MAX_BODY - 65536) throw new Error('These frames are too large together: leave some out, or crop.');
    return frames;
  }

  kindToSend() { return this.kind === 'photo' ? (this.photoIs === 'object' ? 'photo' : 'photos') : this.kind; }

  async analyze() {
    if (this.busy) return;
    this.busy = true;
    const id = (this.jobId = rid());
    let frames;
    try { frames = await this.payload(); } catch (e) { this.busy = false; this.toast(e.message); return; }
    this.render('analyzing');
    this.listen(id);
    const marks = this.kind === 'video' && (this.marks.grasp_s != null || this.marks.release_s != null) ? this.marks : undefined;
    const r = await call(() => this.s.api.post('/api/video/analyze', { id, frames, marks, subq: !!this.subq, kind: this.kindToSend() }));
    this.busy = false;
    if (this.jobId !== id) return;  // the person stopped waiting
    this.unsub?.();
    if (!r.ok) { this.toast(r.error || 'The analysis failed'); this.render('picker'); return; }
    this.answers = {};
    this.mapping = r.mapping || null;
    if (r.result && r.result.usable !== false) {
      this.result = r.result;
      this.why = '';
    } else {  // keyless, or nothing came back: the same cards, filled in by hand, with the reason on top
      this.result = { ...this.skeleton(), failed: r.result ? r.result.unusable_reason || 'Nothing could be estimated.' : null };
      this.why = r.result ? '' : r.why || '';
      this.mapping = null;
    }
    this.render('results');
  }

  skeleton() {  // the cards with nothing estimated: filled in by hand (marks still time the cycle)
    const marks = this.kind === 'video' && (this.marks.grasp_s != null || this.marks.release_s != null) ? { ...this.marks } : null;
    return { kind: this.kindToSend(), usable: true, frames: this.frames.length, marks, manual: true,
      task: null, object: null, motion: null, timing: null, environment: null, safety: null, questions: [], notes: [] };
  }

  async manual() {
    this.result = this.skeleton();
    this.answers = {};
    this.why = this.info?.claude ? '' : this.info?.manual_reason || '';
    await this.preview(false);
    this.render('results');
  }

  async preview(render = true) {
    const seq = (this.seq = (this.seq || 0) + 1);
    this.dirty = true;  // the list of changes is behind the cards until this answer is in
    const r = await call(() => this.s.api.post('/api/video/apply', { preview: true, result: this.result, answers: this.answers }));
    if (seq !== this.seq) return;
    this.dirty = false;
    if (r.ok && r.mapping) this.mapping = r.mapping;
    else if (!r.ok) this.toast(r.error || "Couldn't update the list of changes");
    if (render && this.view === 'results') this.renderSummary();
  }

  previewSoon() { this.dirty = true; clearTimeout(this.pt); this.pt = setTimeout(() => this.preview(), 350); }

  // ---- results: cards, questions, notes
  val(path) {
    if (path in this.answers && !String(this.answers[path]).startsWith('@')) return this.answers[path];
    let v = this.result;
    for (const p of path.split('.')) v = v == null ? undefined : v[p];
    if (path === 'timing.cycle_time_s' && v == null) {
      const m = this.result?.marks;
      if (m?.grasp_s != null && m?.release_s != null && m.release_s > m.grasp_s) return r2(2 * (m.release_s - m.grasp_s));
    }
    return v;
  }

  edited(sec, paths) {
    if (this.answers[sec]) return this.answers[sec];
    return paths.some((p) => p in this.answers) ? true : false;
  }

  resultsHTML(head) {
    const E = this.esc, res = this.result || {}, I = this.I;
    const manual = !!res.manual, failed = res.failed;
    const prov = (res.providers || []).filter((p) => p.status === 'done').map((p) => (p.provider === 'subq' ? 'SubQ Snap (AI), beta second opinion' : 'Claude (AI)'));
    const src = manual ? 'Filled in by you' : prov.length ? `${prov.join(' + ')}${res.cost_usd_est ? ` · about $${res.cost_usd_est.toFixed(2)}` : ''}${res.ms ? ` · ${(res.ms / 1000).toFixed(1)} s` : ''}` : '';
    const banner = failed ? `<div class="kchk bad">${I('alert', 'sm')}<span>${E(failed)}</span></div>`
      : manual ? `<div class="kchk">${I('check', 'sm')}<span>${E(this.why || COPY.local)}</span></div>`
        : `<div class="vj-honest">${E(COPY.results)}</div>`;
    const strip = this.frames.map((f, i) => `<button type="button" class="vj-mini" data-frame="${i}" title="Frame ${i + 1}${f.t_s != null ? `, t = ${f.t_s} s` : ''}"><img src="${f.thumb}" alt=""><span>${i + 1}</span></button>`).join('');
    return head(`What Morph understood${src ? ` · <span class="vj-src">${E(src)}</span>` : ''}`)
      + `${banner}<div class="vj-ministrip">${strip}</div>
      <div class="vj-cards">${this.cardTask()}${this.cardObject()}${this.cardLayout()}${this.cardTiming()}${this.cardEnv()}${this.cardSafety()}</div>
      <div class="vj-summary"></div>
      <div class="dlg-f"><button class="btn" type="button" data-a="back">Back to frames</button><span class="grow"></span><span class="vj-nchg dim"></span>
        <button class="btn primary" type="button" data-a="review" disabled>Review changes</button></div>`;
  }

  card(sec, title, icon, body, paths) {
    const s = this.result?.[sec];
    const ev = s?.evidence_frames?.length ? `<span class="vj-ev dim">frames ${s.evidence_frames.map((n) => `<a href="#" data-ev="${n}">${n}</a>`).join(', ')}</span>` : '';
    return `<section class="vj-card" data-sec="${sec}"><h5>${icon}${title}<span class="grow"></span>${confTag(s?.confidence ?? null, this.edited(sec, paths), this.result?.manual)}</h5>${body}${ev}</section>`;
  }

  field(path, label, kind, extra = {}) {
    const E = this.esc, v = this.val(path);
    let input;
    if (kind === 'select') {
      input = `<span class="sel"><select data-path="${path}" data-kind="enum"><option value="">${extra.none || (this.result?.manual ? 'choose' : 'not seen')}</option>${extra.options.map(([k, l]) => `<option value="${k}" ${v === k ? 'selected' : ''}>${E(l)}</option>`).join('')}</select></span>`;
    } else if (kind === 'bool') {
      input = `<label class="vj-chk"><input type="checkbox" data-path="${path}" data-kind="bool" ${v ? 'checked' : ''}><span>${E(extra.text || '')}</span></label>`;
    } else if (kind === 'text') {
      input = `<input class="vj-in" type="text" maxlength="60" data-path="${path}" data-kind="text" value="${E(v ?? '')}" placeholder="${E(extra.ph || '')}">`;
    } else {
      input = `<span class="field vj-num"><input type="number" data-path="${path}" data-kind="num" value="${v ?? ''}" step="${extra.step || 1}" min="${extra.min ?? ''}" max="${extra.max ?? ''}" placeholder="?"><span class="u">${extra.unit || ''}</span></span>`;
    }
    return `<div class="vj-f"><label>${E(label)}</label>${input}${extra.after || ''}</div>`;
  }

  cardTask() {
    const s = this.result?.task;
    return this.card('task', 'Task', this.I('play', 'sm'), `${this.field('task.type', 'What the hands do', 'select', { options: TASKS })}
      ${s?.summary ? `<p class="vj-sum">${this.esc(s.summary)}</p>` : ''}`, ['task.type']);
  }

  cardObject() {
    const o = this.result?.object || {}, E = this.esc;
    const rng = o.size_range_mm ? `${o.size_range_mm[0]} to ${o.size_range_mm[1]} mm` : '';
    const basis = o.scale_reference ? `measured against ${SCALES[o.scale_reference] || o.scale_reference}` : '';
    const sizeNote = rng || basis ? `<div class="vj-note dim">${E([rng && `range ${rng}`, basis].filter(Boolean).join(', '))}</div>` : '';
    return this.card('object', 'Object', this.I('box', 'sm'), `${this.field('object.name', 'What it is', 'text', { ph: 'e.g. egg' })}
      <div class="vj-note vj-preset"></div>
      ${this.field('object.shape', 'Shape', 'select', { options: SHAPES })}
      ${this.field('object.diameter_mm', 'Width where it is held', 'num', { unit: 'mm', min: 1, max: 300, after: `<button class="btn ghost vj-measure" type="button" data-a="measure" title="Size it from a frame with two clicks on something of known length">${this.I('ruler', 'sm')}Measure</button>` })}${sizeNote}
      ${this.field('object.length_mm', 'Length', 'num', { unit: 'mm', min: 1, max: 500 })}
      ${this.field('object.mass_g', 'Mass of one', 'num', { unit: 'g', min: 0.1, max: 1500, step: 0.1 })}
      ${this.field('object.fragility', 'Breaks how easily', 'select', { options: FRAGILITY })}
      ${this.field('object.surface', 'Surface', 'select', { options: SURFACES })}`,
    ['object.name', 'object.shape', 'object.diameter_mm', 'object.length_mm', 'object.mass_g', 'object.fragility', 'object.surface']);
  }

  cardLayout() {
    const m = this.result?.motion || {};
    const pick = this.point('pick', m), place = this.point('place', m);
    const z = Math.max(m.pick?.z_mm || 0, m.place?.z_mm || 0);
    return this.card('motion', 'Pick and place', this.I('target', 'sm'), `${this.table(pick, place)}
      <div class="vj-xy mono">${pick && place ? `pick (${pick[0]}, ${pick[1]}) · place (${place[0]}, ${place[1]}) mm` : 'Drag the green pick and blue place spots to where they are'}</div>
      ${z > 30 ? `<div class="vj-note dim">Place spot about ${z} mm above the table: simulated at table height.</div>` : ''}
      ${this.field('motion.positions_vary', '', 'bool', { text: 'Objects come from different spots (adds a wrist camera)' })}`,
    ['motion.pick.x_mm', 'motion.pick.y_mm', 'motion.place.x_mm', 'motion.place.y_mm', 'motion.positions_vary']);
  }

  point(name, m) {
    const x = this.val(`motion.${name}.x_mm`), y = this.val(`motion.${name}.y_mm`);
    if (x == null || y == null) return m[name] ? [m[name].x_mm, m[name].y_mm] : null;
    return [x, y];
  }

  // top view of the table: robot base at the bottom edge, x forward (up), y left (left), reach rings at 200 and 500 mm
  table(pick, place) {
    const { S, cx, by } = TABLE_VIEW, P = (x, y) => [cx - y * S, by - x * S];
    const ring = (r) => `<path d="M ${cx - r * S} ${by} A ${r * S} ${r * S} 0 0 1 ${cx + r * S} ${by}" class="ring"/><text x="${cx + r * S - 2}" y="${by - 4}" class="lbl" text-anchor="end">${r}</text>`;
    const [tx0, ty0] = P(TABLE.x[1], TABLE.y[1]), [tx1, ty1] = P(TABLE.x[0], TABLE.y[0]);
    const dot = (p, cls, lab) => {
      const [sx, sy] = P(...(p || (cls === 'pick' ? [220, -80] : [160, 170])));
      return `<g class="pt ${cls} ${p ? '' : 'ghost'}" data-pt="${cls}" transform="translate(${sx} ${sy})"><circle r="8"/><text y="-12" text-anchor="middle">${lab}</text></g>`;
    };
    const line = pick && place ? (() => { const [a, b] = P(...pick), [c, d] = P(...place); return `<path d="M${a} ${b}L${c} ${d}" class="path"/>`; })() : '';
    return `<svg class="vj-table" viewBox="0 0 ${2 * cx} ${by + 10}" role="img" aria-label="Table seen from above: drag the pick and place spots">
      <rect x="${tx0}" y="${ty0}" width="${tx1 - tx0}" height="${ty1 - ty0}" class="area"/>${ring(200)}${ring(500)}
      <path d="M ${cx - 16} ${by} A 16 16 0 0 1 ${cx + 16} ${by}" class="base"/><text x="${cx}" y="${by - 20}" class="lbl" text-anchor="middle">robot</text>
      ${line}${dot(pick, 'pick', 'pick')}${dot(place, 'place', 'place')}</svg>`;
  }

  cardTiming() {
    const t = this.result?.timing, E = this.esc;
    const m = this.result?.marks, fromMarks = !t && m?.grasp_s != null && m?.release_s != null && m.release_s > m.grasp_s;
    const basis = 'timing.cycle_time_s' in this.answers ? 'you entered it' : t?.basis === 'marks' || fromMarks ? `from your grasp and release marks (carry ${((t?.carry_s) ?? r2(m.release_s - m.grasp_s))} s, then back)`
      : t?.cycle_time_s != null ? `from ${t.cycles_seen || 'repeated'} cycles in the video`
        : this.kind === 'video' ? 'not seen: mark grasp and release on the frames, or enter it' : 'photos have no timing: enter it';
    return this.card('timing', 'Cycle time', this.I('clock', 'sm'), `${this.field('timing.cycle_time_s', 'One pick and place', 'num', { unit: 's', min: 1, max: 600, step: 0.1 })}
      <div class="vj-note dim">${E(basis)}</div>`, ['timing.cycle_time_s']);
  }

  cardEnv() {
    return this.card('environment', 'Where', this.I('home', 'sm'), this.field('environment.setting', 'Setting', 'select', { options: SETTINGS }), ['environment.setting']);
  }

  cardSafety() {
    const s = this.result?.safety || {}, E = this.esc;
    return this.card('safety', 'People and safety', this.I('alert', 'sm'), `${this.field('safety.other_people_visible', 'Other people in view', 'num', { min: 0, max: 100 })}
      <div class="vj-note dim">A count only: Morph never identifies people.</div>
      ${this.field('safety.hands_share_workspace', '', 'bool', { text: 'People will work within reach (adds an emergency stop)' })}
      ${s.hazards?.length ? `<div class="vj-note">Hazards seen: ${E(s.hazards.join(', '))}</div>` : ''}`, ['safety.other_people_visible', 'safety.hands_share_workspace']);
  }

  renderSummary() {
    const box = this.el.querySelector('.vj-summary');
    if (!box) return;
    const E = this.esc, mp = this.mapping || { items: [], notes: [], questions: [] };
    const qs = (mp.questions || []).map((q) => `<div class="vj-q"><div class="vj-qt">${E(q.text)}${q.why ? ` <span class="dim">(${E(q.why)})</span>` : ''}</div>
      ${q.chips?.length ? `<div class="vj-chips">${q.chips.map((c, i) => `<button type="button" class="vj-chip" data-q="${E(q.field)}" data-i="${i}">${E(c.label)}</button>`).join('')}</div>` : '<div class="dim vj-qhint">Answer it by editing the cards.</div>'}</div>`).join('');
    const notes = (mp.notes || []).map((n) => `<li>${E(n)}</li>`).join('');
    box.innerHTML = `${qs ? `<div class="vj-qs"><div class="sec-t">Morph needs your answer</div>${qs}</div>` : ''}${notes ? `<ul class="vj-notes">${notes}</ul>` : ''}`;
    box.querySelectorAll('.vj-chip').forEach((b) => (b.onclick = () => {
      const q = (mp.questions || []).find((x) => x.field === b.dataset.q), c = q?.chips?.[+b.dataset.i];
      if (!c) return;
      if (c.value === '@measure') { this.openMeasure(); return; }
      this.answers[q.field] = c.value;
      if (q.field === 'motion' && c.value === '@accept') for (const p of ['pick', 'place']) for (const a of ['x_mm', 'y_mm']) if (this.result?.motion?.[p]) delete this.answers[`motion.${p}.${a}`];
      this.render('results');
      this.preview();
    }));
    const pre = Object.fromEntries((mp.items || []).filter((i) => i.source === 'preset').map((i) => [i.key, i.new])), pn = this.el.querySelector('.vj-preset');
    if (pn) {  // a preset beats the estimates: say which values it brings
      const bits = [pre['object.shape'], pre['object.diameter_mm'] != null && `${pre['object.diameter_mm']} mm wide`, pre['object.mass_g'] != null && `${pre['object.mass_g']} g`,
        pre['object.max_force_n'] != null && (pre['object.max_force_n'] ? `damages above ${pre['object.max_force_n']} N` : 'robust')].filter(Boolean);
      pn.textContent = pre['object.name'] ? `Morph's ${pre['object.name']} preset: ${bits.join(', ') || 'already set'}` : '';
    }
    const n = (mp.items || []).length, btn = this.el.querySelector('[data-a="review"]'), lab = this.el.querySelector('.vj-nchg');
    const noProject = !this.project();
    if (btn) { btn.disabled = !n && !noProject; btn.textContent = noProject ? 'Use for a new robot' : n ? `Review ${n} change${n === 1 ? '' : 's'}` : 'Nothing to change yet'; }
    if (lab) lab.textContent = (mp.questions || []).some((q) => q.chips?.length) ? 'Low-confidence parts wait for your answer' : '';
  }

  project() { try { return typeof this.s.project === 'function' ? this.s.project() : this.s.S?.project; } catch (e) { return null; } }

  bindResults() {
    const el = this.el;
    el.querySelectorAll('[data-path]').forEach((inp) => {
      const set = () => {
        const p = inp.dataset.path, k = inp.dataset.kind;
        let v = k === 'bool' ? inp.checked : k === 'num' ? (inp.value === '' ? null : Number(inp.value)) : inp.value.trim();
        if (v === null || v === '' || (k === 'num' && !Number.isFinite(v))) delete this.answers[p]; else this.answers[p] = v;
        inp.closest('.vj-card')?.querySelector('h5 .tag')?.replaceWith(Object.assign(document.createElement('span'), { className: 'tag acc', textContent: 'you set this' }));
        const basis = p === 'timing.cycle_time_s' && p in this.answers && inp.closest('.vj-card')?.querySelector('.vj-note');
        if (basis) basis.textContent = 'you entered it';  // no longer "from your marks"
        this.previewSoon();
      };
      inp.addEventListener(inp.dataset.kind === 'bool' || inp.tagName === 'SELECT' ? 'change' : 'input', set);
    });
    el.querySelectorAll('[data-ev]').forEach((a) => (a.onclick = (e) => { e.preventDefault(); this.flash(+a.dataset.ev - 1); }));
    el.querySelectorAll('[data-frame]').forEach((b) => (b.onclick = () => this.flash(+b.dataset.frame)));
    const on = (a, fn) => el.querySelectorAll(`[data-a="${a}"]`).forEach((b) => (b.onclick = fn));
    on('back', () => this.render('picker'));
    on('review', async () => {  // an edit made a moment ago may still be on its way to the list of changes: wait for it
      if (this.dirty) { clearTimeout(this.pt); await this.preview(false); }
      if (this.view !== 'results') return;
      if (this.project()) this.render('review'); else this.handOff();
    });
    on('measure', () => this.openMeasure());
    this.bindTable(el.querySelector('.vj-table'));
    this.renderSummary();
    if (!this.mapping) this.preview();
  }

  flash(i) {
    const b = this.el.querySelectorAll('.vj-mini')[i];
    if (!b) return;
    b.classList.remove('hi'); void b.offsetWidth; b.classList.add('hi');
    b.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  bindTable(svgEl) {
    if (!svgEl) return;
    const { S, cx, by } = TABLE_VIEW;
    const toMM = (e) => {
      const pt = svgEl.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      const p = pt.matrixTransform(svgEl.getScreenCTM().inverse());
      const x = Math.round((by - p.y) / S / 10) * 10, y = Math.round((cx - p.x) / S / 10) * 10;
      return [Math.min(Math.max(x, TABLE.x[0]), TABLE.x[1]), Math.min(Math.max(y, TABLE.y[0]), TABLE.y[1])];
    };
    svgEl.querySelectorAll('[data-pt]').forEach((g) => {
      g.onpointerdown = (e) => {
        e.preventDefault(); g.setPointerCapture(e.pointerId);
        const which = g.dataset.pt;
        let moved = false;
        const fill = () => {  // dragging one spot confirms the whole layout: both spots become the person's values
          for (const n of ['pick', 'place']) {
            const p = this.point(n, this.result?.motion || {}) || (n === 'pick' ? [220, -80] : [160, 170]);
            if (this.answers[`motion.${n}.x_mm`] == null) { this.answers[`motion.${n}.x_mm`] = p[0]; this.answers[`motion.${n}.y_mm`] = p[1]; }
          }
        };
        g.onpointermove = (ev) => {
          if (!moved) { moved = true; fill(); }
          const [x, y] = toMM(ev);
          this.answers[`motion.${which}.x_mm`] = x; this.answers[`motion.${which}.y_mm`] = y;
          g.setAttribute('transform', `translate(${cx - y * S} ${by - x * S})`);
          g.classList.remove('ghost');
          const pk = this.point('pick', {}), pl = this.point('place', {}), path = svgEl.querySelector('.path'), xy = this.el.querySelector('.vj-xy');
          if (path && pk && pl) path.setAttribute('d', `M${cx - pk[1] * S} ${by - pk[0] * S}L${cx - pl[1] * S} ${by - pl[0] * S}`);
          if (xy && pk && pl) xy.textContent = `pick (${pk[0]}, ${pk[1]}) · place (${pl[0]}, ${pl[1]}) mm`;
        };
        g.onpointerup = g.onpointercancel = () => { g.onpointermove = null; if (moved) { this.render('results'); this.preview(); } };
      };
    });
  }

  // ---- measuring tool: two clicks on a known length, two across the object
  openMeasure() {
    this.measure = { i: Math.max(0, this.near('grasp_s')), pts: [], ref: REFS[0][1], refName: REFS[0][0] };
    this.render('measure');
  }

  measureParts() {
    const m = this.measure, f = this.frames[m.i] || this.frames[0];
    const px = (a, b) => Math.hypot((a.x - b.x) * (f?.w || 1), (a.y - b.y) * (f?.h || 1));
    const result = m.pts.length === 4 ? Math.round((m.ref * px(m.pts[2], m.pts[3])) / Math.max(px(m.pts[0], m.pts[1]), 1e-6)) : null;
    const lines = [[0, 1, 'ref'], [2, 3, 'obj']].filter(([, b]) => m.pts[b]).map(([a, b, c]) => `<line x1="${m.pts[a].x * 100}" y1="${m.pts[a].y * 100}" x2="${m.pts[b].x * 100}" y2="${m.pts[b].y * 100}" class="${c}"/>`).join('');
    const dots = m.pts.map((p, i) => `<circle cx="${p.x * 100}" cy="${p.y * 100}" r="1.1" class="${i < 2 ? 'ref' : 'obj'}"/>`).join('');
    const step = result != null ? `The object is about <b class="mono">${result} mm</b> wide.`
      : m.pts.length < 2 ? `Click both ends of the ${this.esc(m.refName.toLowerCase())} (${m.ref} mm).` : 'Now click both sides of the object, where the gripper would hold it.';
    return { svg: lines + dots, step, result };
  }

  measureHTML(head) {
    const E = this.esc, m = this.measure, f = this.frames[m.i] || this.frames[0], p = this.measureParts();
    return head('Measure the object on a frame: the same picture, a known length, two clicks each.')
      + `<div class="vj-mrow"><span class="lab dim">Known length</span>${REFS.map(([n, mm]) => `<button type="button" class="vj-chip ${m.refName === n ? 'on' : ''}" data-ref="${mm}" data-rn="${E(n)}">${E(n)} ${mm} mm</button>`).join('')}
        <span class="field vj-num"><input type="number" class="vj-refmm" min="1" max="2000" step="0.5" value="${m.ref}" aria-label="Known length in mm"><span class="u">mm</span></span>
        <span class="grow"></span>${this.frames.length > 1 ? `<span class="sel"><select class="vj-mframe">${this.frames.map((fr, i) => `<option value="${i}" ${i === m.i ? 'selected' : ''}>Frame ${i + 1}${fr.t_s != null ? ` (${fmtT(fr.t_s)})` : ''}</option>`).join('')}</select></span>` : ''}</div>
      <div class="vj-stage vj-measure-stage"><img class="vj-img" src="${f?.thumb || ''}" width="${f?.w || ''}" height="${f?.h || ''}" alt="Frame to measure on" draggable="false">
        <div class="vj-ov"><svg class="vj-mov" viewBox="0 0 100 100" preserveAspectRatio="none">${p.svg}</svg></div></div>
      <div class="vj-mstep">${p.step}</div>
      <div class="dlg-f"><button class="btn" type="button" data-a="mback">Back to the cards</button><button class="btn ghost" type="button" data-a="mredo">Start over</button><span class="grow"></span>
        <button class="btn primary" type="button" data-a="muse" ${p.result != null ? '' : 'disabled'}>Use ${p.result != null ? `${p.result} mm` : 'it'}</button></div>`;
  }

  bindMeasure() {
    const el = this.el, m = this.measure, stage = el.querySelector('.vj-measure-stage'), ov = stage?.querySelector('.vj-ov');
    placeOverlay(stage);
    const on = (a, fn) => el.querySelectorAll(`[data-a="${a}"]`).forEach((b) => (b.onclick = fn));
    const update = () => {  // in place: the picture and the overlay stay where the person is clicking
      const p = this.measureParts(), use = el.querySelector('[data-a="muse"]');
      el.querySelector('.vj-mov').innerHTML = p.svg;
      el.querySelector('.vj-mstep').innerHTML = p.step;
      use.disabled = p.result == null;
      use.textContent = p.result != null ? `Use ${p.result} mm` : 'Use it';
    };
    on('mback', () => this.render('results'));
    on('mredo', () => { m.pts = []; update(); });
    on('muse', () => {
      const r = this.measureParts().result;
      if (r == null) return;
      this.answers['object.diameter_mm'] = r;
      this.render('results'); this.preview();
    });
    el.querySelectorAll('[data-ref]').forEach((b) => (b.onclick = () => { m.ref = +b.dataset.ref; m.refName = b.dataset.rn; this.render('measure'); }));
    const refIn = el.querySelector('.vj-refmm');
    if (refIn) refIn.onchange = () => { const v = +refIn.value; if (v > 0) { m.ref = v; m.refName = 'Known length'; this.render('measure'); } };
    const fs = el.querySelector('.vj-mframe');
    if (fs) fs.onchange = () => { m.i = +fs.value; m.pts = []; this.render('measure'); };
    if (stage && ov) stage.onclick = (e) => {
      if (m.pts.length >= 4) return;
      const r = ov.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) return;  // a click on the black bars around the picture
      m.pts.push({ x, y });
      update();
    };
  }

  // ---- review and apply
  reviewHTML(head) {
    const E = this.esc, mp = this.mapping || { items: [], notes: [] }, project = this.project();
    const NAMED = { fsr402: 'FSR 402', fdm_print: '3D printed', auto: 'automatic' };  // values a person reads, not keys
    const fmt = (k, v) => (v == null || v === '' ? '<span class="dim">none</span>' : typeof v === 'number' ? `${+v.toFixed(3)}${/_mm$/.test(k) ? ' mm' : /_g$/.test(k) ? ' g' : /_n$/.test(k) ? ' N' : /_s$/.test(k) ? ' s' : ''}` : E(NAMED[v] || v));
    const SRC = { preset: 'Morph preset', video: 'from the video', photo: 'from the photo', marks: 'your marks', user: 'you set it', rules: "Morph's rule" };
    const why = (it) => (it.source === 'user' && it.why === 'you entered it' ? '' : it.why || '');  // the tag says so already
    const rows = (mp.items || []).map((it) => `<tr><td>${E(it.label)}<small class="mono dim">${E(it.key)}</small></td><td class="mono">${fmt(it.key, it.old)}</td><td>${this.I('arrow', 'sm')}</td>
      <td class="mono new">${fmt(it.key, it.new)}</td><td class="dim">${E(why(it))}<span class="tag neutral">${E(SRC[it.source] || it.source || '')}</span></td></tr>`).join('');
    const open = (mp.questions || []).filter((q) => q.chips?.length).length;
    return head(`These changes go to <b>${E(project || 'this project')}</b> as one new revision; all 9 stages update as usual.`)
      + `<table class="vj-items"><thead><tr><th>Setting</th><th>Now</th><th></th><th>After</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>
      ${open ? `<div class="kchk">${this.I('alert', 'sm')}<span>${open} low-confidence part${open === 1 ? '' : 's'} left out: answer ${open === 1 ? 'its question' : 'their questions'} to include ${open === 1 ? 'it' : 'them'}.</span></div>` : ''}
      ${(mp.notes || []).length ? `<ul class="vj-notes">${mp.notes.map((n) => `<li>${E(n)}</li>`).join('')}</ul>` : ''}
      <div class="vj-apperr"></div>
      <div class="dlg-f"><button class="btn" type="button" data-a="rback">Back</button><span class="grow"></span>
        <button class="btn primary" type="button" data-a="apply" ${rows ? '' : 'disabled'}>${this.I('check', 'sm')}Apply ${(mp.items || []).length} change${(mp.items || []).length === 1 ? '' : 's'}</button></div>`;
  }

  bindReview() {
    const on = (a, fn) => this.el.querySelectorAll(`[data-a="${a}"]`).forEach((b) => (b.onclick = fn));
    on('rback', () => this.render('results'));
    on('apply', async () => {
      const btn = this.el.querySelector('[data-a="apply"]');
      btn.disabled = true; btn.innerHTML = '<span class="spin"></span>Applying…';
      const label = (this.result?.object?.name || this.answers['object.name']) ? `video: ${String(this.answers['object.name'] || this.result.object.name).slice(0, 30)}` : 'from a video';
      const base = this.s.S?.current?.rev;  // the revision on screen: a newer change by someone else is asked about, not overwritten
      const r = await call(() => this.s.api.post('/api/video/apply', { changes: this.mapping.changes, label, ...(base ? { base_rev: base } : {}),
        ...(this.result?.manual ? { manual: true } : {}) }));  // filled in by hand: the chat says so, not "from the video"
      if (!r.ok) {
        btn.disabled = false; btn.textContent = 'Apply';
        const box = this.el.querySelector('.vj-apperr');
        if (box) box.innerHTML = `<div class="kchk bad">${this.I('alert', 'sm')}<span>${this.esc(r.error || 'Could not apply the changes.')}</span></div>`;
        return;
      }
      this.toast(r.job?.position > 1 ? 'Queued after the changes already waiting: the robot regenerates when its turn comes' : 'Applying the changes: the robot regenerates');
      this.close();
    });
  }

  // no project open: the brief engine (new robot) takes the slots
  handOff() {
    const detail = { slots: this.mapping?.slots || {}, result: this.result, answers: { ...this.answers }, changes: this.mapping?.changes || {} };
    if (this.onSlotsFn) this.onSlotsFn(detail);
    else window.dispatchEvent(new CustomEvent('morph:video-slots', { detail }));
    this.close();
  }
}

// ---- styles (moved into style.css under /* ---- video ---- */ at integration; injected once otherwise)
export const VIDEO_CSS = `
.vj-btn { height: 26px; font-size: 11.5px; }
.vj-btn svg { color: var(--accent2); }
#chat.vj-drop, .vj-drop { position: relative; }
.vj-drop::after { content: "Drop a video or photos to show Morph the job"; position: absolute; inset: 8px; z-index: 20; display: grid; place-items: center;
  border: 2px dashed var(--accent-line); border-radius: 12px; background: rgba(13, 15, 20, .86); color: var(--accent2); font-weight: 600; font-size: 13px; pointer-events: none; }
.modal .dlg.vjob { width: min(980px, calc(100vw - 32px)); max-height: 92vh; }
.vjob .vj-consent p { margin: 10px 0 0; font-size: 13px; line-height: 1.55; color: var(--ink2); }
.vjob .kchk { margin-top: 10px; }
.vjob .vj-choose { display: grid; place-items: center; padding: 26px 0 8px; }
.vj-pickbig { display: grid; justify-items: center; gap: 6px; width: min(460px, 100%); padding: 26px 18px; border-radius: 14px; border: 1.5px dashed var(--line3);
  background: var(--bg2); color: var(--ink); cursor: pointer; }
.vj-pickbig:hover { border-color: var(--accent-line); background: var(--accent-bg); }
.vj-pickbig svg { color: var(--accent2); } .vj-pickbig span { color: var(--muted); font-size: 12px; }
.vj-extract { display: flex; align-items: center; gap: 12px; margin: 22px 0 8px; }
.vj-bar { flex: 1; height: 6px; border-radius: 6px; background: var(--bg4); overflow: hidden; }
.vj-bar b { display: block; height: 100%; background: var(--accent); transition: width .2s; }
.vj-why { margin-top: 8px; font-size: 11px; }
.vj-pick { display: grid; gap: 8px; margin-top: 8px; }
.vj-stage { position: relative; display: grid; place-items: center; background: #000; border-radius: 10px; overflow: hidden; min-height: 180px; user-select: none; touch-action: none; }
.vj-stage .vj-img, .vj-stage .vj-live { display: block; max-width: 100%; max-height: 44vh; object-fit: contain; }
.vj-stage .vj-live { position: absolute; }
.vj-stage.cropping { cursor: crosshair; }
.vj-ov { position: absolute; left: 0; top: 0; width: 100%; height: 100%; pointer-events: none; }
.vj-crop { position: absolute; border: 2px solid var(--accent); box-shadow: 0 0 0 9999px rgba(0, 0, 0, .55); pointer-events: none; }
.vj-hint { position: absolute; left: 50%; top: 10px; transform: translateX(-50%); padding: 4px 10px; border-radius: 99px; background: rgba(13, 15, 20, .85); color: var(--ink2); font-size: 11.5px; pointer-events: none; }
.vj-strip { display: flex; gap: 6px; overflow-x: auto; padding: 2px 2px 6px; }
.vj-th { position: relative; flex: none; width: 96px; padding: 0; border-radius: 8px; border: 1.5px solid var(--line2); background: var(--bg2); overflow: hidden; color: var(--ink2); }
.vj-th img { display: block; width: 100%; height: 56px; object-fit: cover; }
.vj-th.on { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-bg); }
.vj-th .n { position: absolute; left: 4px; top: 4px; font: 600 10px var(--mono); padding: 0 4px; border-radius: 4px; background: rgba(0, 0, 0, .6); color: #fff; }
.vj-th .t { display: block; font-size: 10.5px; padding: 2px 6px; text-align: left; }
.vj-th .x { position: absolute; right: 3px; top: 3px; width: 18px; height: 18px; display: grid; place-items: center; border-radius: 50%; background: rgba(0, 0, 0, .65); color: #fff; }
.vj-th .x:hover { background: var(--bad); }
.vj-th i { position: absolute; right: 4px; bottom: 20px; font: 600 9.5px var(--sans); font-style: normal; padding: 0 5px; border-radius: 4px; color: #0a0f24; }
.vj-th i.g { background: var(--good); } .vj-th i.r { background: var(--warn); bottom: 34px; }
.vj-th.add { display: grid; place-items: center; align-content: center; gap: 4px; height: 76px; border-style: dashed; color: var(--muted); font-size: 11px; }
.vj-scrub { display: flex; align-items: center; gap: 10px; }
.vj-scrub input { flex: 1; --p: 0%; }
.vj-tools { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.vj-marks { color: var(--muted); font-size: 11.5px; } .vj-marks b { color: var(--ink); font-weight: 500; }
.vj-kind .lab { text-transform: none; letter-spacing: 0; font-weight: 500; }
.vjob .vj-remote { font-size: 12px; }
.vj-subq { display: inline-flex; align-items: center; gap: 8px; font-size: 12px; color: var(--ink2); cursor: pointer; }
.vj-subqnote { font-size: 11.5px; margin-top: 8px; }
.vj-count { font-size: 11.5px; }
.vj-mode a { color: var(--accent2); }
.vj-prog { display: grid; gap: 6px; margin: 16px 0 6px; }
.vj-row { display: flex; align-items: center; gap: 9px; padding: 8px 11px; border-radius: 9px; background: var(--bg3); font-size: 12.5px; color: var(--ink2); }
.vj-row > span:nth-child(2) { flex: 1; }
.vj-row.done svg { color: var(--good); } .vj-row.error svg { color: var(--bad); } .vj-row.skipped { opacity: .7; }
.vj-honest { margin-top: 8px; padding: 8px 11px; border-radius: 9px; background: var(--warn-bg); color: #f3d79b; font-size: 12.3px; line-height: 1.45; }
.vj-src { color: var(--ink2); }
.vj-ministrip { display: flex; gap: 4px; margin: 10px 0 2px; overflow-x: auto; }
.vj-mini { position: relative; flex: none; width: 54px; height: 34px; padding: 0; border: 1px solid var(--line2); border-radius: 5px; overflow: hidden; background: var(--bg2); }
.vj-mini img { width: 100%; height: 100%; object-fit: cover; display: block; }
.vj-mini span { position: absolute; left: 2px; top: 1px; font: 600 9.5px var(--mono); color: #fff; text-shadow: 0 1px 2px #000; }
.vj-mini.hi { animation: vjHi 1.4s ease-out; }
@keyframes vjHi { 0%, 40% { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-bg); } }
.vj-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; margin-top: 10px; }
.vj-card { padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line2); background: var(--bg2); display: grid; gap: 6px; align-content: start; }
.vj-card h5 { display: flex; align-items: center; gap: 7px; margin: 0 0 2px; font-size: 12.5px; font-weight: 650; color: var(--ink); }
.vj-card h5 svg { color: var(--accent2); }
.vj-f { display: grid; grid-template-columns: 118px minmax(0, 1fr) auto; align-items: center; gap: 8px; }
.vj-f > label { color: var(--muted); font-size: 11.8px; }
.vj-f:has(.vj-chk) { grid-template-columns: 1fr; }
.vj-in { height: 26px; border-radius: 6px; border: 1px solid var(--line2); background: var(--bg1); padding: 0 8px; font-size: 12px; outline: 0; min-width: 0; }
.vj-in:focus { border-color: var(--accent-line); }
.vj-num { min-width: 0; }
.vj-chk { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; color: var(--ink2); cursor: pointer; }
.vj-chk input { accent-color: var(--accent); margin-top: 2px; }
.vj-note { font-size: 11.5px; } .vj-sum { margin: 0; font-size: 12px; color: var(--ink2); }
.vj-preset { color: var(--accent2); } .vj-preset:empty { display: none; }
.vj-ev { font-size: 11px; } .vj-ev a { color: var(--accent2); }
.vj-measure { height: 26px; padding: 0 6px; font-size: 11.5px; }
.vj-table { width: 100%; max-width: 330px; justify-self: center; display: block; touch-action: none; }
.vj-table .area { fill: rgba(112, 145, 255, .05); stroke: var(--line3); stroke-dasharray: 3 3; }
.vj-table .ring { fill: none; stroke: var(--line2); }
.vj-table .base { fill: var(--bg4); stroke: var(--line3); }
.vj-table .lbl { fill: var(--dim); font: 9px var(--sans); }
.vj-table .path { stroke: var(--muted); stroke-dasharray: 4 3; fill: none; }
.vj-table .pt { cursor: grab; } .vj-table .pt text { fill: var(--ink2); font: 600 9.5px var(--sans); }
.vj-table .pt circle { stroke: #fff; stroke-width: 1.5; } .vj-table .pick circle { fill: var(--good); } .vj-table .place circle { fill: var(--accent); }
.vj-table .pt.ghost { opacity: .45; }
.vj-xy { font-size: 11px; color: var(--ink2); text-align: center; }
.vj-summary { margin-top: 10px; display: grid; gap: 8px; }
.vj-qs { padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(245, 185, 66, .35); background: rgba(245, 185, 66, .06); }
.vj-q + .vj-q { margin-top: 10px; }
.vj-qt { font-size: 12.8px; color: var(--ink); margin-bottom: 6px; }
.vj-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.vj-chip { height: 26px; padding: 0 10px; border-radius: 99px; border: 1px solid var(--line3); background: var(--bg3); color: var(--ink2); font-size: 12px; }
.vj-chip:hover, .vj-chip.on { border-color: var(--accent-line); background: var(--accent-bg); color: var(--ink); }
.vj-qhint { font-size: 11.5px; }
.vj-notes { margin: 0; padding: 0 0 0 18px; display: grid; gap: 3px; color: var(--ink2); font-size: 12px; }
.vj-items { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12.3px; }
.vj-items th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em; color: var(--dim); font-weight: 650; padding: 4px 8px; border-bottom: 1px solid var(--line2); }
.vj-items td { padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.vj-items td small { display: block; font-size: 10px; }
.vj-items td.new { color: var(--accent2); font-weight: 600; }
.vj-items td .tag { margin-left: 6px; }
.vj-mrow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 10px 0 8px; }
.vj-mrow .lab { font-size: 11.5px; } .vj-mrow .vj-num { width: 96px; }
.vj-measure-stage { cursor: crosshair; }
.vj-mov { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.vj-mov line { stroke-width: .5; } .vj-mov .ref { stroke: var(--warn); fill: var(--warn); } .vj-mov .obj { stroke: var(--good); fill: var(--good); }
.vj-mstep { margin-top: 8px; font-size: 12.8px; color: var(--ink2); }
@media (max-width: 720px) { .vj-f { grid-template-columns: 96px minmax(0, 1fr) auto; } .vj-th { width: 80px; } }
`;

function injectCSS() {
  if (document.getElementById('vjob-css')) return;
  const st = document.createElement('style');
  st.id = 'vjob-css';
  st.textContent = VIDEO_CSS;
  document.head.appendChild(st);
}

// Feature contract: init(studio) -> { open(files?), onSlots(fn) }. onSlots receives {slots, result, answers, changes}
// when the person uses a video with no project open (the brief engine's hook); without a handler the same detail goes
// out as a window "morph:video-slots" event. Other features open the dialog with a window "morph:video-open" event
// (detail: {files?}), so they never import this file.
export function init(studio) {
  injectCSS();
  const job = new VideoJob(studio);
  window.addEventListener('morph:video-open', (e) => job.open(e.detail?.files));
  return { open: (files) => job.open(files), onSlots: (fn) => job.onSlots(fn), job };
}

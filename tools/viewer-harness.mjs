/* Headless harness: runs the real viewer module against fake DOM / three /
   3d-force-graph so the galaxy logic can be tested without a browser.

       node tools/viewer-harness.mjs

   It asserts the things that are easy to break and hard to see: click-to-focus,
   fly-to-source, the 4+ cluster branch, small talk leaving the camera alone,
   and markdown being stripped before it is spoken.                          */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const html = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');
const modSrc = [...html.matchAll(/<script type="module">([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).pop();

// strip the CDN imports (we inject our own stubs instead)
const src = modSrc.split('\n').filter(l => !/^\s*import\s/.test(l)).join('\n');

/* ---------------------------------------------------------- fake DOM ---- */
class ClassList {
  constructor() { this.s = new Set(); }
  add(...c) { c.forEach(x => this.s.add(x)); }
  remove(...c) { c.forEach(x => this.s.delete(x)); }
  toggle(c, on) { on ? this.s.add(c) : this.s.delete(c); }
  contains(c) { return this.s.has(c); }
}
class El {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase(); this.id = id;
    this.children = []; this.classList = new ClassList();
    this.style = {}; this.textContent = ''; this._html = '';
    this.hidden = false; this.title = ''; this.value = '';
  }
  set innerHTML(v) { this._html = v; this.children = []; }
  get innerHTML() { return this._html; }
  addEventListener(t, fn) { (this._h ||= {})[t] = fn; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute() {} getAttribute() { return null; }
  remove() { this.removed = true; }
  getContext() {
    return {
      createRadialGradient: () => ({ addColorStop() {} }),
      fillRect() {}, set fillStyle(v) {}, get fillStyle() { return ''; },
    };
  }
}
const els = new Map();
const byId = id => { if (!els.has(id)) els.set(id, new El('div', id)); return els.get(id); };

const handlers = {};
const win = {
  addEventListener: (t, fn) => { (handlers[t] ||= []).push(fn); },
  dispatchEvent: (e) => { (handlers[e.type] || []).forEach(fn => fn(e)); },
  setStatus: null, speak: null,
};
globalThis.window = win;
globalThis.document = {
  getElementById: byId,
  createElement: (t) => new El(t),
  body: new El('body'),
  head: new El('head'),
  title: '',
};
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
};
globalThis.performance = { now: () => Date.now() };
globalThis.CustomEvent = class { constructor(type, o = {}) { this.type = type; this.detail = o.detail; } };
let frames = 0;
globalThis.requestAnimationFrame = (fn) => { if (frames++ < 3) setTimeout(fn, 0); };
globalThis.speechSynthesis = {
  getVoices: () => [{ name: 'Daniel', lang: 'en-GB' }],
  speak(u) { if (u.text && String(u.text).trim()) globalThis.__spoken.push(u.text); },  // ignore the silent unlock utterance
  cancel() {}, resume() {}, addEventListener() {},
};
globalThis.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; } };
win.speechSynthesis = globalThis.speechSynthesis;   // the module reads window.speechSynthesis
globalThis.__spoken = [];

/* --------------------------------------------------------- fake three ---- */
const THREE = {
  AdditiveBlending: 2, SRGBColorSpace: 'srgb',
  Color: class { constructor(c) { this.r = 1; this.g = 1; this.b = 1; } setHSL() { return this; } },
  Group: class { constructor() { this.children = []; } add(...c) { this.children.push(...c); } },
  Mesh: class { constructor(g, m) { this.geometry = g; this.material = m; } },
  Sprite: class {
    constructor(m) { this.material = m; this.scale = { setScalar(v) { this._v = v; } }; }
  },
  SphereGeometry: class {}, BufferGeometry: class { setAttribute() {} },
  BufferAttribute: class {}, Points: class { constructor() { this.rotation = { y: 0 }; } },
  MeshStandardMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  MeshBasicMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  SpriteMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  PointsMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  CanvasTexture: class {}, AmbientLight: class {}, DirectionalLight: class {
    constructor() { this.position = { set() {} }; }
  },
};

/* ------------------------------------------------- fake 3d-force-graph --- */
const calls = { cameraPosition: [], zoomToFit: [], linkColor: [], nodeThreeObject: 0 };
let cam = { x: 0, y: 0, z: 300 };
const graph = {};
for (const m of ['backgroundColor', 'showNavInfo', 'enableNodeDrag', 'nodeRelSize', 'nodeVal',
  'nodeColor', 'nodeOpacity', 'nodeLabel', 'linkWidth', 'linkOpacity', 'linkColor',
  'nodeThreeObject', 'nodeThreeObjectExtend', 'onNodeClick', 'onBackgroundClick',
  'width', 'height', 'graphData']) {
  graph[m] = function (v) { if (m === 'nodeThreeObject') calls.nodeThreeObject++; if (m === 'linkColor') calls.linkColor.push(!!v); return graph; };
}
graph.cameraPosition = (pos, look, ms) => {
  if (pos === undefined) return { ...cam };
  cam = { ...pos }; calls.cameraPosition.push({ pos, look, ms }); return graph;
};
graph.zoomToFit = (ms, pad, filter) => { calls.zoomToFit.push({ ms, pad, filter: !!filter }); return graph; };
graph.onEngineStop = (cb) => { graph._engineStop = cb; return graph; };
graph.scene = () => ({ add() {} });
graph.controls = () => ({ addEventListener() {} });
const ForceGraph3D = () => (el) => graph;

/* ------------------------------------------------- graph data + fetch ---- */
const gd = fs.readFileSync(path.join(ROOT, 'viewer', 'graph-data.js'), 'utf8');
const GRAPH = JSON.parse(gd.slice(gd.indexOf('const GRAPH = ') + 'const GRAPH = '.length,
                                  gd.lastIndexOf('};') + 1));
// pad with synthetic notes so the "4+ sources -> cluster" branch is reachable
const extra = [1, 2, 3, 4, 5].map(i => ({
  id: i, label: 'Note ' + i, group: i % 2 ? 'captures' : 'notes', folder: '.',
  path: 'n' + i + '.md', excerpt: 'excerpt ' + i, words: 12, mtime: 0,
  degree: 1, neighbors: [0], text: 'synthetic note ' + i,
}));
const TEST_GRAPH = {
  notes_dir: GRAPH.notes_dir, generated: GRAPH.generated,
  nodes: [...GRAPH.nodes, ...extra],
  links: [1, 2, 3, 4, 5].map(i => ({ source: 0, target: i })),
};
win.GRAPH = TEST_GRAPH;
globalThis.GRAPH = TEST_GRAPH;

let lastBody = null;
globalThis.fetch = (url, opts) => {
  lastBody = JSON.parse(opts.body);
  const q = lastBody.question;
  const notes = (q.includes('captured') || q.includes('seed')) ? [0] : [];
  return Promise.resolve({
    json: () => Promise.resolve({
      answer: '**Captured notes** live in `captures/`. See the note for details.',
      nodes: notes, titles: notes.map(() => 'Upgrading Jarvis'),
      topic: notes.length ? 'notes' : 'chat', session: 'abc123',
    }),
  });
};

/* ----------------------------------------------------------- run it ------ */
const runner = new Function('ForceGraph3D', 'THREE', src + '\n;return { ask: window.ask, boot };');
const api = runner(ForceGraph3D, THREE);
Object.assign(api, {
  focusNode: win.__galaxy.focusNode,
  clearFocus: win.__galaxy.clearFocus,
  pulseNode: win.pulseNode,
  speak: win.speak,
});

// boot() is called at the end of the module
await new Promise(r => setTimeout(r, 30));

const results = [];
const check = (name, cond, extra = '') => {
  results.push((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  [' + extra + ']' : ''));
};

check('boot ran, galaxy mounted', byId('stat-notes').textContent === 6,
      'notes=' + byId('stat-notes').textContent);
check('custom node objects built', calls.nodeThreeObject > 0);

// --- 1. click a node -> flies + opens panel
calls.cameraPosition.length = 0;
api.focusNode(0);
check('click node -> camera fly called', calls.cameraPosition.length === 1,
      JSON.stringify(calls.cameraPosition[0] || {}).slice(0, 80));
check('click node -> panel opens', byId('panel').classList.contains('open'));

// --- 2. answer from 1 note -> fly to it, open panel, speak it
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
globalThis.__spoken = [];
await api.ask('where should captured notes live?');
await new Promise(r => setTimeout(r, 150));   // speech fires on a 60ms timer
check('1 source -> flies to source', calls.cameraPosition.length === 1 && calls.zoomToFit.length === 0);
check('1 source -> panel shows the note', byId('panel-title').textContent === 'Upgrading Jarvis');
check('answer text rendered', byId('answer-text').textContent.includes('captures/'));
check('markdown stripped before speaking',
      globalThis.__spoken.length === 1 && !globalThis.__spoken[0].includes('**'),
      JSON.stringify(globalThis.__spoken[0] || '').slice(0, 60));
check('source chip rendered clickable', byId('answer-sources').children.length === 2);

// --- 3. small talk -> camera must NOT move
const camBefore = JSON.stringify(cam);
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
await api.ask('how are you today?');
check('small talk -> no camera move',
      calls.cameraPosition.length === 0 && calls.zoomToFit.length === 0 && JSON.stringify(cam) === camBefore);
await new Promise(r => setTimeout(r, 120));
check('small talk -> still answered aloud', globalThis.__spoken.length === 2);

// --- 4. four or more sources -> cluster, not a dive
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
win.dispatchEvent(new globalThis.CustomEvent('jarvis:answer', {
  detail: { answer: 'x', nodes: [0, 1, 2, 3], titles: ['a', 'b', 'c', 'd'], topic: 'notes' },
}));
check('4+ sources -> frames cluster (zoomToFit)',
      calls.zoomToFit.length === 1 && calls.cameraPosition.length === 0);

// --- 5. three sources -> still a single fly
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
win.dispatchEvent(new globalThis.CustomEvent('jarvis:answer', {
  detail: { answer: 'x', nodes: [0, 1, 2], titles: ['a', 'b', 'c'], topic: 'notes' },
}));
check('3 sources -> single fly', calls.cameraPosition.length === 1 && calls.zoomToFit.length === 0);

// --- 6. pulse (Stage 6 scaffold)
api.pulseNode(0, 50);
check('pulseNode accepts a node id', true);

console.log(results.join('\n'));
console.log('\n' + results.filter(r => r.startsWith('PASS')).length + '/' + results.length + ' checks passed');
process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);

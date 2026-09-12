/* Headless harness: runs the real viewer module against fake DOM / three /
   3d-force-graph so the galaxy logic can be tested without a browser.

       node tools/viewer-harness.mjs

   It asserts the things that are easy to break and hard to see: click-to-focus,
   fly-to-source, the 4+ cluster branch, small talk leaving the camera alone,
   markdown being stripped before it is spoken, the boot greeting, and the
   Stage 6 live insert (where a new note can shift every existing id).        */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');
const modSrc = [...html.matchAll(/<script type="module">([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).pop();
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
  append(...c) { c.forEach(x => this.children.push(x)); }
  setAttribute() {} getAttribute() { return null; }
  remove() { this.removed = true; }
  getContext(kind) {
    if (String(kind).startsWith('webgl')) {           // WebGL probe in boot()
      return { getExtension: () => null, getParameter: () => 'stub-gpu' };
    }
    return {
      createRadialGradient: () => ({ addColorStop() {} }),
      measureText: (t) => ({ width: String(t).length * 20 }),
      fillText() {}, clearRect() {}, fillRect() {},
      set fillStyle(v) {}, get fillStyle() { return ''; },
      font: '', textAlign: '', textBaseline: '', shadowColor: '', shadowBlur: 0,
    };
  }
}
const els = new Map();
const byIdEl = id => { if (!els.has(id)) els.set(id, new El('div', id)); return els.get(id); };

const handlers = {};
const win = {
  addEventListener: (t, fn) => { (handlers[t] ||= []).push(fn); },
  dispatchEvent: (e) => { (handlers[e.type] || []).forEach(fn => fn(e)); },
};
globalThis.window = win;
globalThis.document = {
  getElementById: byIdEl,
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
  speak(u) { if (u.text && String(u.text).trim()) globalThis.__spoken.push(u.text); },
  cancel() {}, resume() {}, addEventListener() {},
};
win.speechSynthesis = globalThis.speechSynthesis;
globalThis.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; } };
globalThis.__spoken = [];

/* --------------------------------------------------------- fake three ---- */
const THREE = {
  AdditiveBlending: 2, SRGBColorSpace: 'srgb',
  Color: class { constructor() { this.r = 1; this.g = 1; this.b = 1; } setHSL() { return this; } },
  Group: class { constructor() { this.children = []; } add(...c) { this.children.push(...c); } },
  Mesh: class { constructor(g, m) { this.geometry = g; this.material = m; } },
  Sprite: class { constructor(m) { this.material = m; this.scale = { setScalar() {}, set() {} }; this.position = { set() {} }; } },
  SphereGeometry: class {}, BufferGeometry: class { setAttribute() {} },
  BufferAttribute: class {}, Points: class { constructor() { this.rotation = { y: 0 }; } },
  MeshStandardMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  MeshBasicMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  SpriteMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  PointsMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  CanvasTexture: class {}, AmbientLight: class {},
  DirectionalLight: class { constructor() { this.position = { set() {} }; } },
};

/* ------------------------------------------------- fake 3d-force-graph --- */
const calls = { cameraPosition: [], zoomToFit: [], linkColor: [], nodeObjects: 0, graphData: 0 };
let cam = { x: 0, y: 0, z: 300 };
const graph = {};
for (const m of ['backgroundColor', 'showNavInfo', 'enableNodeDrag', 'nodeRelSize', 'nodeVal',
  'nodeColor', 'nodeOpacity', 'nodeLabel', 'linkWidth', 'linkOpacity', 'linkColor',
  'nodeThreeObjectExtend', 'onNodeClick', 'onBackgroundClick', 'width', 'height']) {
  graph[m] = function (v) { if (m === 'linkColor') calls.linkColor.push(!!v); return graph; };
}
graph.nodeThreeObject = (fn) => {
  graph._nodeObj = fn;
  if (graph._data) graph._data.nodes.forEach(n => fn(n, THREE));   // real lib rebuilds here
  return graph;
};
graph.graphData = (d) => {
  calls.graphData++;
  graph._data = d;
  // the real library builds a three.js object per node; do the same so
  // node.__three exists and the Stage 6 birth pulse has something to find
  if (graph._nodeObj) d.nodes.forEach(n => { graph._nodeObj(n, THREE); });
  return graph;
};
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
const REAL = JSON.parse(gd.slice(gd.indexOf('const GRAPH = ') + 'const GRAPH = '.length,
                                 gd.lastIndexOf('};') + 1));
// pad with synthetic notes. "zebra.md" sorts after "captures/", so it is the
// node whose id shifts when a capture is added - exactly what Stage 6 must handle.
const extra = [
  { id: 1, label: 'Zebra', group: 'notes', path: 'zebra.md' },
  { id: 2, label: 'Note 2', group: 'notes', path: 'n2.md' },
  { id: 3, label: 'Note 3', group: 'captures', path: 'n3.md' },
  { id: 4, label: 'Note 4', group: 'notes', path: 'n4.md' },
  { id: 5, label: 'Note 5', group: 'notes', path: 'n5.md' },
].map(n => ({ ...n, folder: '.', excerpt: 'excerpt', words: 10, mtime: 0, degree: 1,
              neighbors: [0], text: 'synthetic ' + n.label }));
const TEST_GRAPH = {
  notes_dir: REAL.notes_dir, generated: REAL.generated,
  nodes: [...REAL.nodes, ...extra],
  links: [1, 2, 3, 4, 5].map(i => ({ source: 0, target: i })),
};
win.GRAPH = TEST_GRAPH;
globalThis.GRAPH = TEST_GRAPH;

globalThis.fetch = (url, opts) => {
  const body = JSON.parse(opts.body);
  if (String(url).endsWith('/remember')) {
    // the server rebuilt the graph: the new capture takes id 1, everything
    // from "zebra.md" onward shifts down by one
    const newId = 1;
    const index = [
      { id: 0, path: 'Upgrading Jarvis.md' },
      { id: newId, path: 'captures/new-capture.md' },
      ...extra.map((n, i) => ({ id: i + 2, path: n.path })),
    ];
    return Promise.resolve({
      json: () => Promise.resolve({
        ok: true, id: newId,
        node: { id: newId, label: 'New Capture', group: 'captures', folder: 'captures',
                path: 'captures/new-capture.md', excerpt: 'a brand new note',
                words: 4, mtime: 0, degree: 1, neighbors: [0], text: 'new note' },
        title: 'New Capture', path: 'captures/new-capture.md',
        related: 0,
        links: [{ source: 0, target: newId }],
        index,
        said: 'Filed, sir. "New Capture" now exists in writing.',
        notes: TEST_GRAPH.nodes.length + 1,
      }),
    });
  }
  const notes = (body.question.includes('captured') || body.question.includes('seed')) ? [0] : [];
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
  nodes: win.__galaxy.nodes,
  byId: win.__galaxy.byId,
});
await new Promise(r => setTimeout(r, 30));

const results = [];
const check = (name, cond, extra = '') => {
  results.push((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  [' + extra + ']' : ''));
};

/* ---- boot -------------------------------------------------------------- */
check('boot ran, galaxy mounted', byIdEl('stat-notes').textContent === 6,
      'notes=' + byIdEl('stat-notes').textContent);
check('node objects built for every note',
      api.nodes.every(n => !!n.__three), api.nodes.filter(n => !n.__three).length + ' missing');
check('every node carries a readable label sprite',
      api.nodes.every(n => n.__three && n.__three.label),
      api.nodes.length + ' nodes, ' + api.nodes.filter(n => n.__three && n.__three.label).length + ' labelled');

/* ---- boot greeting (Stage 5) ------------------------------------------- */
const GREET = /^Good (morning|afternoon|evening), sir\. 6 notes indexed, all present and accounted for\.$/;
const greetText = byIdEl('answer-text').textContent;
check('boot greeting shown with real note count', GREET.test(greetText), JSON.stringify(greetText));
globalThis.__spoken = [];
handlers.pointerdown.forEach(fn => fn({}));            // first user gesture
await new Promise(r => setTimeout(r, 220));
check('boot greeting spoken after first click',
      globalThis.__spoken.length === 1 && GREET.test(globalThis.__spoken[0]),
      JSON.stringify(globalThis.__spoken[0] || '').slice(0, 70));

/* ---- click to focus (Stage 1) ------------------------------------------ */
calls.cameraPosition.length = 0;
api.focusNode(0);
check('click node -> camera flies', calls.cameraPosition.length === 1);
check('click node -> panel opens', byIdEl('panel').classList.contains('open'));

/* ---- fly to source (Stage 4) ------------------------------------------- */
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
globalThis.__spoken = [];
await api.ask('where should captured notes live?');
await new Promise(r => setTimeout(r, 150));   // speech fires on a 60ms timer
check('1 source -> flies to source',
      calls.cameraPosition.length === 1 && calls.zoomToFit.length === 0);
check('1 source -> panel shows the note', byIdEl('panel-title').textContent === 'Upgrading Jarvis');
check('answer text rendered', byIdEl('answer-text').textContent.includes('captures/'));
check('markdown stripped before speaking',
      globalThis.__spoken.length === 1 && !globalThis.__spoken[0].includes('**'),
      JSON.stringify(globalThis.__spoken[0] || '').slice(0, 60));
check('source chip rendered clickable', byIdEl('answer-sources').children.length === 2);

/* ---- small talk must not move the camera (Stage 5) ---------------------- */
const camBefore = JSON.stringify(cam);
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
globalThis.__spoken = [];
await api.ask('how are you today?');
await new Promise(r => setTimeout(r, 150));
check('small talk -> no camera move',
      calls.cameraPosition.length === 0 && calls.zoomToFit.length === 0 &&
      JSON.stringify(cam) === camBefore);
check('small talk -> still answered aloud', globalThis.__spoken.length === 1);

/* ---- cluster vs single fly (Stage 4) ------------------------------------ */
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
win.dispatchEvent(new globalThis.CustomEvent('jarvis:answer', {
  detail: { answer: 'x', nodes: [0, 1, 2, 3], titles: ['a', 'b', 'c', 'd'], topic: 'notes' },
}));
check('4+ sources -> frames cluster (zoomToFit)',
      calls.zoomToFit.length === 1 && calls.cameraPosition.length === 0);
calls.cameraPosition.length = 0; calls.zoomToFit.length = 0;
win.dispatchEvent(new globalThis.CustomEvent('jarvis:answer', {
  detail: { answer: 'x', nodes: [0, 1, 2], titles: ['a', 'b', 'c'], topic: 'notes' },
}));
check('3 sources -> single fly',
      calls.cameraPosition.length === 1 && calls.zoomToFit.length === 0);

/* ---- total recall: live insert with shifting ids (Stage 6) -------------- */
await new Promise(r => setTimeout(r, 150));   // drain speech timers from the tests above
globalThis.__spoken = [];
const before = api.nodes.map(n => ({ id: n.id, path: n.path }));
const zebraBefore = before.find(n => n.path === 'zebra.md').id;
calls.cameraPosition.length = 0;
const res = await api.ask('remember that the sky is blue');
await new Promise(r => setTimeout(r, 250));
const after = api.nodes.map(n => ({ id: n.id, path: n.path }));
const zebraAfter = after.find(n => n.path === 'zebra.md').id;
const fresh = api.nodes.find(n => n.path === 'captures/new-capture.md');

check('remember -> note added to the galaxy',
      api.nodes.length === before.length + 1 && !!fresh,
      'before=' + before.length + ' after=' + api.nodes.length);
check('remember -> new node takes the server id', fresh && fresh.id === 1,
      'id=' + (fresh && fresh.id));
check('remember -> existing ids re-synced with the server',
      zebraBefore === 1 && zebraAfter === 2, 'zebra ' + zebraBefore + ' -> ' + zebraAfter);
check('remember -> no duplicate ids',
      new Set(after.map(n => n.id)).size === after.length);
check('remember -> born next to its most related node',
      fresh && Number.isFinite(fresh.x) && api.byId.get(0) &&
      Math.abs(fresh.x - (api.byId.get(0).x || 0)) < 20,
      'x=' + (fresh && Math.round(fresh.x)) + ' parent.x=' + Math.round(api.byId.get(0).x || 0));
check('remember -> camera flies to the new note', calls.cameraPosition.length === 1);
check('remember -> panel opens on the new note',
      byIdEl('panel-title').textContent === 'New Capture');
check('remember -> confirmed out loud, once',
      globalThis.__spoken.length === 1 && globalThis.__spoken[0].includes('New Capture'),
      JSON.stringify(globalThis.__spoken[0] || '').slice(0, 60));
check('remember -> legend picks up the new group',
      byIdEl('legend').innerHTML.includes('captures'));
check('remember -> HUD note count updated',
      byIdEl('stat-notes').textContent === before.length + 1,
      'hud=' + byIdEl('stat-notes').textContent);
check('remember -> links rewired to the new ids',
      graph._data.links.every(l => Number.isFinite(l.source) && Number.isFinite(l.target)));

/* ---- the failure screen must actually explain itself --------------------- */
win.showBootError('Test failure screen', 'something went wrong');
const bootEl = byIdEl('boot');
check('boot error renders title + diagnostics',
      bootEl.children.length === 1 && !bootEl.classList.contains('hidden'));
const diagText = (bootEl.children[0].children[2] || {}).textContent || '';
check('diagnostics report note count, WebGL and speech support',
      diagText.includes('notes indexed') && diagText.includes('webgl') &&
      diagText.includes('speech synthesis'),
      JSON.stringify(diagText.split('\n')[0] || '').slice(0, 40));

console.log(results.join('\n'));
const passed = results.filter(r => r.startsWith('PASS')).length;
console.log('\n' + passed + '/' + results.length + ' checks passed');
process.exit(passed === results.length ? 0 : 1);

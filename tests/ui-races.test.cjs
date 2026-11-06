// Run with: node --test tests/ui-races.test.cjs
// Execute the shipped UI in a small DOM harness; fetch resolution is controlled.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../report_workbench/static/app.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const report = (id, title = `Report ${id}`, status = 'completed') => ({
  id, title, status, mode: 'rules', progress: 100, stage: status,
  sources: [], trace: [], created_at: '2026-11-06T00:00:00Z',
});

async function harness() {
  const elements = new Map(), pending = [], requests = [], downloads = [], prints = [], streams = [], timers = [];
  const checkedTemplates = ['study', 'comparison', 'overview'];
  let automaticHistory = true, uuid = 0;
  function element() {
    return { hidden: false, textContent: '', disabled: false, src: '', value: '', dataset: {}, children: [],
      replaceChildren(...values) { this.children = values; },
      append(...values) { this.children.push(...values); },
      getAttribute(name) { return this[name]; }, focus() {},
      click() { downloads.push({ name: this.download, href: this.href }); },
    };
  }
  function el(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }
  const response = body => ({ ok: true, json: async () => body, blob: async () => body });
  const context = {
    TextEncoder, Date, setTimeout: (callback, delay) => delay === 700 ? timers.push(callback) : callback(),
    URL: { createObjectURL: () => 'blob:test-report', revokeObjectURL() {} },
    crypto: { randomUUID: () => `workspace-${++uuid}` },
    localStorage: { getItem: () => null, setItem() {} },
    document: { getElementById: el, createElement: element,
      querySelectorAll: selector => {
        assert.equal(selector, 'input[name="batch-template"]:checked');
        return checkedTemplates.map(value => ({ value }));
      } },
    window: { open: (...args) => prints.push(args) },
    EventSource: class {
      constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
      addEventListener(name, callback) { this.listeners[name] = callback; }
      close() { this.closed = true; }
    },
    fetch: (url, options = {}) => {
      requests.push({ url, options });
      if (url === '/api/config') return Promise.resolve(response({ model_available: false }));
      if (url === '/api/batches' && !options.method) return Promise.resolve(response([]));
      if (url === '/api/jobs' && !options.method && automaticHistory) return Promise.resolve(response([]));
      return new Promise((resolve, reject) => pending.push({ url, options, resolve, reject }));
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  await settle(); // Complete the page's initial config/history load.
  function take(url) {
    const index = pending.findIndex(item => item.url === url);
    assert.notEqual(index, -1, `Expected an outstanding request for ${url}`);
    return pending.splice(index, 1)[0];
  }
  return { el, requests, downloads, prints, streams, pending, timers, checkedTemplates,
    run: code => vm.runInContext(code, context),
    historyManual: () => { automaticHistory = false; },
    take,
    respond: (url, body) => take(url).resolve(response(body)),
    reject: (url, error) => take(url).reject(error),
    response,
  };
}

test('late A response cannot replace B preview or change B export/print targets', async () => {
  const h = await harness();
  const a = h.run("selectJob('A')"), b = h.run("selectJob('B')");
  assert.equal(h.el('export-md').disabled, true);
  h.respond('/api/jobs/B', report('B')); await b;
  h.respond('/api/jobs/A', report('A')); await a;
  assert.equal(h.run('activeId'), 'B');
  assert.match(h.el('result-meta').textContent, /^Report B /);
  assert.match(h.el('preview').src, /^\/api\/jobs\/B\/preview\?/);
  const download = h.el('export-md').onclick();
  h.respond('/api/jobs/B/export/md', 'report B'); await download;
  assert.equal(h.downloads[0].name, '报告-B.md');
  h.el('print').onclick();
  assert.match(h.prints[0][0], /^\/api\/jobs\/B\/preview\?/);
});

test('reselecting the same ID keeps the newest request, including while loading', async () => {
  const h = await harness();
  const first = h.run("selectJob('A')");
  const old = h.take('/api/jobs/A');
  const second = h.run("selectJob('A')");
  h.respond('/api/jobs/A', report('A', 'New revision')); await second;
  old.resolve(h.response(report('A', 'Old revision'))); await first;
  assert.match(h.el('result-meta').textContent, /^New revision /);
  const loading = h.run("selectJob('B')");
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('export-md').disabled, true);
  await h.run("download('md')");
  assert.equal(h.requests.filter(item => item.url.includes('/export/')).length, 0);
  h.respond('/api/jobs/B', report('B')); await loading;
});

test('resetting the editor discards a pending selection and its late error', async () => {
  const h = await harness();
  const selecting = h.run("selectJob('A')");
  h.run('resetEditor()');
  h.reject('/api/jobs/A', new Error('obsolete error')); await selecting;
  assert.equal(h.run('activeId'), null);
  assert.equal(h.el('notice').textContent, '');
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('export-md').disabled, true);
});

test('old workspace history and selection cannot overwrite the new workspace', async () => {
  const h = await harness(); h.historyManual();
  const oldHistory = h.run('refreshHistory()'), oldSelection = h.run("selectJob('A')");
  const old = h.take('/api/jobs');
  h.el('switch-workspace').onclick();
  assert.equal(h.el('history').children.length, 0);
  h.respond('/api/jobs', [report('B', 'New workspace report')]); await settle();
  old.resolve(h.response([report('A', 'Old workspace report')])); await oldHistory;
  h.respond('/api/jobs/A', report('A')); await oldSelection;
  assert.equal(h.el('history').children[0].children[0].textContent, 'New workspace report');
  assert.equal(h.run('activeId'), null);
  assert.equal(h.el('preview').hidden, true);
  assert.equal(h.el('export-md').disabled, true);
  const historyHeaders = h.requests.filter(item => item.url === '/api/jobs').map(item => item.options.headers['X-Workspace-Id']);
  assert.notEqual(historyHeaders.at(-1), historyHeaders.at(-2));
});

test('overlapping history refreshes keep the newest response and discard stale failures', async () => {
  const h = await harness(); h.historyManual();
  const first = h.run('refreshHistory()'), old = h.take('/api/jobs');
  const second = h.run('refreshHistory()');
  h.respond('/api/jobs', [report('B')]); await second;
  old.resolve(h.response([report('A')])); await first;
  assert.equal(h.el('history').children[0].children[0].textContent, 'Report B');
  const failing = h.run('refreshHistory()'), stale = h.take('/api/jobs');
  const latest = h.run('refreshHistory()');
  h.respond('/api/jobs', [report('C')]); await latest;
  stale.reject(new Error('stale failure')); await failing;
  assert.equal(h.el('history').children[0].children[0].textContent, 'Report C');
});

test('late cancellation and closed SSE callbacks cannot replace the selected report', async () => {
  const h = await harness();
  const a = h.run("selectJob('A')"); h.respond('/api/jobs/A', report('A', 'Report A', 'running')); await a;
  const oldStream = h.streams[0], cancelling = h.el('cancel').onclick();
  const b = h.run("selectJob('B')"); h.respond('/api/jobs/B', report('B')); await b;
  h.respond('/api/jobs/A/cancel', report('A', 'Report A', 'cancelled')); await cancelling;
  oldStream.listeners.progress({ data: JSON.stringify(report('A')) });
  oldStream.onerror();
  assert.match(h.el('result-meta').textContent, /^Report B /);
  assert.equal(h.el('export-md').disabled, false);
  assert.equal(h.el('notice').textContent, '');
});

test('an export keeps its original filename when selection changes during download', async () => {
  const h = await harness();
  const a = h.run("selectJob('A')"); h.respond('/api/jobs/A', report('A')); await a;
  const downloading = h.el('export-md').onclick();
  const b = h.run("selectJob('B')"); h.respond('/api/jobs/B', report('B')); await b;
  h.respond('/api/jobs/A/export/md', 'report A'); await downloading;
  assert.equal(h.downloads[0].name, '报告-A.md');
  assert.match(h.el('preview').src, /^\/api\/jobs\/B\/preview\?/);
});

test('late job creation after switching workspace does not select the old job', async () => {
  const h = await harness();
  h.run("addSource({name:'note.txt',kind:'txt',content:'example'})");
  h.el('title').value = 'Example'; h.el('mode').value = 'rules';
  const creating = h.el('generate').onclick();
  h.el('switch-workspace').onclick();
  h.respond('/api/jobs', { id: 'A' }); await creating;
  assert.equal(h.run('activeId'), null);
  assert.equal(h.requests.some(item => item.url === '/api/jobs/A'), false);
  assert.equal(h.el('export-md').disabled, true);
});

const batch = (id, terminal = true) => ({ id, title: `Batch ${id}`, terminal,
  completed: terminal ? 1 : 0, jobs: [report(`${id}-job`, `Report ${id}`, terminal ? 'completed' : 'queued')] });

test('batch submission preserves selected templates and original source evidence', async () => {
  const h = await harness();
  h.run("addSource({name:'data.csv',kind:'csv',content:'minutes\\n15\\n25'})");
  h.el('title').value = 'Batch example'; h.el('mode').value = 'rules'; h.el('instruction').value = '核验投入';
  h.checkedTemplates.splice(1, 1);
  const creating = h.el('generate-batch').onclick();
  const request = h.take('/api/batches'), body = JSON.parse(request.options.body);
  assert.deepEqual(body.templates, ['study', 'overview']);
  assert.equal(body.sources[0].content, 'minutes\n15\n25');
  assert.equal(body.instruction, '核验投入');
  request.resolve(h.response({ id: 'B' })); await settle();
  h.respond('/api/batches/B', batch('B')); await creating;
  assert.equal(h.el('batch-title').textContent, 'Batch B');
  assert.equal(h.el('export-batch').disabled, false);
});

test('late batch creation cannot replace a newer batch selection', async () => {
  const h = await harness();
  h.run("addSource({name:'note.txt',kind:'txt',content:'example'})");
  h.el('title').value = 'Example'; h.el('mode').value = 'rules';
  const creating = h.el('generate-batch').onclick();
  const selecting = h.run("selectBatch('B')"); h.respond('/api/batches/B', batch('B')); await selecting;
  h.respond('/api/batches', { id: 'A' }); await creating;
  assert.equal(h.run('activeBatchId'), 'B');
  assert.equal(h.requests.some(item => item.url === '/api/batches/A'), false);
  assert.equal(h.el('batch-title').textContent, 'Batch B');
});

test('late batch creation after switching workspace never selects the old batch', async () => {
  const h = await harness();
  h.run("addSource({name:'note.txt',kind:'txt',content:'example'})");
  h.el('title').value = 'Example'; h.el('mode').value = 'rules';
  const creating = h.el('generate-batch').onclick();
  h.el('switch-workspace').onclick(); h.respond('/api/batches', { id: 'A' }); await creating;
  assert.equal(h.run('activeBatchId'), null);
  assert.equal(h.requests.some(item => item.url === '/api/batches/A'), false);
  assert.equal(h.el('batch-panel').hidden, true);
});

test('batch polling and ZIP cannot use an obsolete or loading batch', async () => {
  const h = await harness();
  const a = h.run("selectBatch('A')"); h.respond('/api/batches/A', batch('A', false)); await a;
  assert.equal(h.timers.length, 1); h.timers.shift()();
  const old = h.take('/api/batches/A');
  const b = h.run("selectBatch('B')");
  assert.equal(h.el('export-batch').disabled, true);
  await h.el('export-batch').onclick();
  assert.equal(h.requests.some(item => item.url.includes('/export/zip')), false);
  h.respond('/api/batches/B', batch('B')); await b;
  old.resolve(h.response(batch('A'))); await settle();
  assert.equal(h.el('batch-title').textContent, 'Batch B');
  const downloading = h.el('export-batch').onclick();
  h.respond('/api/batches/B/export/zip', 'zip B'); await downloading;
  assert.equal(h.downloads[0].name, '批次-B.zip');
});

test('late comparison results cannot cross a workspace boundary', async () => {
  const h = await harness(); h.el('compare-left').value = 'A'; h.el('compare-right').value = 'B';
  const comparing = h.el('compare').onclick(); h.el('switch-workspace').onclick();
  h.respond('/api/compare?left=A&right=B', { left:{title:'Old A'}, right:{title:'Old B'}, note:'old', sources:[], metrics:[] }); await comparing;
  assert.equal(h.el('comparison').hidden, true);
  assert.equal(h.el('comparison').children.length, 0);
});

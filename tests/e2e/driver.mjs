#!/usr/bin/env node
/**
 * driver.mjs — headless-Chromium CDP driver for the ccr web UI (SPEC.md section 9, "test_e2e.py").
 *
 * Usage: node driver.mjs <url-with-?t=token> [shots-dir]
 *
 * Talks to Chromium over the DevTools protocol using Node's built-in WebSocket (Node >= 22, no
 * dependencies) and walks the review UI through the end-to-end scenario: load with the token in
 * `?t=`, grammar check, set a cover letter through the API and comment on the whole series from the
 * "All changes" header, select the second commit, comment on a hovered line via the gutter [+], type into
 * an editor, watch it grow and switch to the Preview tab, cancel it (draft kept), see the line comment projected into
 * "All changes" with a "from <sha>" tag that leads back, check that single-key shortcuts are off, toggle split
 * view, drag a three-line range and comment on it, submit the pending comments as a round with the top-bar
 * Submit button, receive a Claude reply pushed through the API (toast + New dot that clears once the thread
 * has been on screen), and reload the page without `?t=` to prove the token survives in localStorage.
 *
 * Prints exactly one JSON line on stdout: {ok, steps:[{name, ok, detail}], consoleErrors:[…],
 * screenshots:[paths]} and exits 0 when every step passed and no console error (exceptions,
 * console.error, CSP/network log errors) was observed, 1 otherwise. Every wait polls the DOM with a
 * timeout — there are no blind sleeps — and the browser is killed in `finally` (also on SIGTERM).
 *
 * The helpers are exported so ad-hoc scripts (screenshot tours) can reuse them; the scenario runs
 * only when this file is the entry point.
 */
import { spawn, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

/** Every hljs language id from the SPEC section 3 guess table; index.html must load them all. */
export const LANGS = ['python', 'c', 'cpp', 'rust', 'go', 'javascript', 'typescript', 'java', 'kotlin', 'scala', 'ruby',
  'php', 'csharp', 'swift', 'bash', 'sql', 'xml', 'css', 'scss', 'less', 'json', 'yaml', 'ini', 'markdown', 'makefile',
  'cmake', 'dockerfile', 'protobuf', 'lua', 'perl', 'r', 'objectivec', 'erlang', 'haskell', 'ocaml', 'nix', 'groovy',
  'plaintext', 'diff', 'vbnet', 'wasm', 'graphql'];

const CHROME_CANDIDATES = ['chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable'];
const VIEWPORT = { width: 1440, height: 900 };

/** Candidate Chromium executables: `$CCR_CHROME` when set, else the well-known names. */
export function chromeCandidates() {
  return process.env.CCR_CHROME ? [process.env.CCR_CHROME] : CHROME_CANDIDATES;
}

/** True when `bin` resolves on PATH (or is an existing absolute path). */
function executableExists(bin) {
  return spawnSync('sh', ['-c', 'command -v "$1" >/dev/null 2>&1', 'sh', bin]).status === 0;
}

/** Launch headless Chromium with a throw-away profile; resolves {proc, ws, profile} or throws. */
export async function launchChrome() {
  const profile = mkdtempSync(join(tmpdir(), 'ccr-e2e-'));
  const args = ['--headless=new', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0',
    `--window-size=${VIEWPORT.width},${VIEWPORT.height}`, `--user-data-dir=${profile}`, '--no-first-run',
    '--disable-extensions', '--disable-background-networking', 'about:blank'];
  for (const bin of chromeCandidates()) {
    if (!executableExists(bin)) continue;
    const proc = spawn(bin, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    const ws = await new Promise((resolve) => {
      let buf = '';
      const onData = (chunk) => {
        buf += chunk.toString();
        const match = /DevTools listening on (ws:\/\/\S+)/.exec(buf);
        if (match) resolve(match[1]);
      };
      proc.stderr.on('data', onData);
      proc.stdout.on('data', onData);
      proc.on('error', () => resolve(null));
      proc.on('exit', () => resolve(null));
      setTimeout(() => resolve(null), 20000).unref();
    });
    if (ws) return { proc, ws, profile };
    try { proc.kill('SIGKILL'); } catch (e) { /* already gone */ }
  }
  rmSync(profile, { recursive: true, force: true });
  throw new Error('no chromium found on PATH (set CCR_CHROME)');
}

/** Minimal DevTools-protocol client over one WebSocket. */
export class CDP {
  constructor(wsUrl) { this.wsUrl = wsUrl; this.id = 0; this.pending = new Map(); this.listeners = new Map(); }

  async connect() {
    this.ws = new WebSocket(this.wsUrl);
    await new Promise((resolve, reject) => { this.ws.onopen = resolve; this.ws.onerror = () => reject(new Error('websocket failed: ' + this.wsUrl)); });
    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
      } else if (msg.method) {
        for (const fn of this.listeners.get(msg.method) || []) fn(msg.params);
      }
    };
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
  }

  close() { try { this.ws.close(); } catch (e) { /* ignore */ } }
}

/**
 * A page under test: evaluation, polling waits, synthetic input and screenshots on one CDP session.
 * `errors` collects everything that counts as a console error for the report.
 */
export class Page {
  constructor(cdp, shotsDir) { this.cdp = cdp; this.shotsDir = shotsDir; this.errors = []; this.expected = []; this.screenshots = []; }

  /** Console errors minus the ones a step announced with `expected` (each pattern excuses one error). */
  unexpectedErrors() {
    const patterns = [...this.expected];
    return this.errors.filter((error) => {
      const i = patterns.findIndex((p) => p.test(error));
      if (i < 0) return true;
      patterns.splice(i, 1);
      return false;
    });
  }

  async init() {
    await this.cdp.send('Page.enable');
    await this.cdp.send('Runtime.enable');
    await this.cdp.send('Log.enable');
    this.cdp.on('Runtime.exceptionThrown', (p) => this.errors.push('exception: ' + (p.exceptionDetails.exception?.description || p.exceptionDetails.text)));
    this.cdp.on('Runtime.consoleAPICalled', (p) => {
      if (p.type === 'error' || p.type === 'assert') this.errors.push(`console.${p.type}: ` + p.args.map((a) => a.value ?? a.description).join(' '));
    });
    this.cdp.on('Log.entryAdded', (p) => { if (p.entry.level === 'error') this.errors.push(`log(${p.entry.source}): ${p.entry.text} ${p.entry.url || ''}`.trim()); });
    await this.cdp.send('Emulation.setDeviceMetricsOverride', { ...VIEWPORT, deviceScaleFactor: 1, mobile: false });
  }

  async evaluate(expression) {
    const r = await this.cdp.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) {
      throw new Error('evaluate failed: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text) + ' in ' + expression.slice(0, 160));
    }
    return r.result.value;
  }

  /** Poll `expression` (a JS boolean expression) until truthy; throws after `timeout` ms. */
  async waitFor(expression, { timeout = 10000, label = expression } = {}) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      let ok = false;
      try { ok = await this.evaluate(`Boolean(document.body && (${expression}))`); } catch (e) { ok = false; }
      if (ok) return;
      await this.frame();
    }
    throw new Error('timeout waiting for ' + label);
  }

  /** Wait for one animation frame plus a short tick — the smallest deterministic settle. */
  frame() {
    return this.evaluate('new Promise(r => requestAnimationFrame(() => setTimeout(r, 16)))');
  }

  async box(selector) {
    const b = await this.evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
    })()`);
    if (!b) throw new Error('no element ' + selector);
    return b;
  }

  mouse(type, x, y, extra = {}) {
    return this.cdp.send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1, pointerType: 'mouse', ...extra });
  }

  async hover(selector) {
    const b = await this.box(selector);
    await this.mouse('mouseMoved', b.x, b.y, { button: 'none' });
    await this.frame();
    return b;
  }

  async click(selector, modifiers = 0) {
    const b = await this.hover(selector);
    await this.mouse('mousePressed', b.x, b.y, { modifiers });
    await this.mouse('mouseReleased', b.x, b.y, { modifiers });
    await this.frame();
  }

  type(text) { return this.cdp.send('Input.insertText', { text }); }

  /** Press one key; `text` makes it a printable keyDown (needed for keypress-driven shortcuts). */
  async key(keyName, code, keyCode, modifiers = 0, text) {
    const base = { key: keyName, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode, modifiers };
    await this.cdp.send('Input.dispatchKeyEvent', { type: text ? 'keyDown' : 'rawKeyDown', ...base, ...(text ? { text } : {}) });
    await this.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
    await this.frame();
  }

  async navigate(url) {
    await this.cdp.send('Page.navigate', { url });
    await this.waitFor(`document.body.dataset.ready === '1'`, { label: 'body[data-ready]', timeout: 20000 });
  }

  async shot(name) {
    const r = await this.cdp.send('Page.captureScreenshot', { format: 'png' });
    const file = join(this.shotsDir, name + '.png');
    writeFileSync(file, Buffer.from(r.data, 'base64'));
    this.screenshots.push(file);
    return file;
  }

  /** Call the ccr API from inside the page (same origin, token header). */
  api(path, init = {}) {
    return this.evaluate(`fetch(${JSON.stringify(path)}, Object.assign({ headers: { 'X-CCR-Token': ${JSON.stringify(this.token)}, 'Content-Type': 'application/json' } }, ${JSON.stringify(init)})).then(r => r.status === 204 ? null : r.json())`);
  }
}

/** Connect to the first page target of a freshly launched browser. */
export async function openPage(ws, shotsDir) {
  const port = new URL(ws).port;
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  const target = targets.find((t) => t.type === 'page');
  const cdp = new CDP(target.webSocketDebuggerUrl);
  await cdp.connect();
  const page = new Page(cdp, shotsDir);
  await page.init();
  return page;
}

/** Runs the scenario steps, recording each one's outcome in `report`. */
export class Runner {
  constructor(page, report) { this.page = page; this.report = report; }

  async step(name, fn) {
    try {
      const detail = await fn(this.page);
      this.report.steps.push({ name, ok: true, detail: detail == null ? '' : String(detail) });
    } catch (e) {
      this.report.ok = false;
      this.report.steps.push({ name, ok: false, detail: e.message });
      try { await this.page.shot('fail-' + name.replace(/\W+/g, '_')); } catch (e2) { /* ignore */ }
    }
  }
}

const MAIN_THREADS = '#main .thread[data-thread-id]';
/** Cover letter the scenario sets through POST /api/cover: a heading, a paragraph, a list and a code span. */
export const COVER = '# Why\n\nThe **cover** letter set by the e2e driver.\n\n- retries\n- `backoff`';
/** Controls the simplified UI no longer has (drawer, help, Viewed, collapse-all, verdicts, since-round). */
const REMOVED = ['#drawer', '#drawer-backdrop', '#btn-review', '#btn-help', '#help', '#btn-mark-seen', '#btn-collapse-all', '#btn-expand-all',
  '#chk-hide-viewed', '#files-progress', '#files-toolbar', '#review-summary', '#rounds-list', '#btn-submit-review', 'input[name=verdict]',
  '.file-header .viewed', '.other-views', '.btn-since-round', '.btn-discard-comment'];
/** The colour of `var(--success)` as the browser reports it (the Submit button must be painted with it). */
const SUCCESS_RGB = `(() => { const p = document.createElement('span'); p.style.color = 'var(--success)'; document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove(); return c; })()`;

/** The SPEC 9 scenario against the fixture repository (`main..feature --worktree`). */
export async function runScenario(page, url) {
  const origin = new URL(url).origin;
  const port = new URL(url).port;
  const token = page.token;
  const runner = new Runner(page, page.report);
  let commitSha = null;
  let filePath = null;

  await runner.step('load page', async () => {
    await page.navigate(url);
    if ((await page.evaluate('location.search')).includes('t=')) throw new Error('token still in the address bar');
    const stored = await page.evaluate(`localStorage.getItem('ccr:token:' + location.port)`);
    if (stored !== token) throw new Error('token not stored in localStorage');
    return 'ready; title=' + (await page.evaluate('document.title'));
  });

  await runner.step('hljs languages', async () => {
    const missing = await page.evaluate(`${JSON.stringify(LANGS)}.filter(l => !hljs.getLanguage(l))`);
    if (missing.length) throw new Error('missing grammars: ' + missing.join(','));
    return `${LANGS.length} grammars`;
  });

  await runner.step('first render shows All changes', async () => {
    await page.waitFor(`document.querySelectorAll('#commit-list .commit-item').length >= 3 && document.querySelector('.file-card[data-rendered="1"] table.diff')`);
    const subject = await page.evaluate(`document.querySelector('#commit-header .subject').textContent.trim()`);
    if (subject !== 'All changes') throw new Error('initial subject is ' + subject);
    const selected = await page.evaluate(`document.querySelector('#commit-list .commit-item.is-selected').dataset.sha`);
    if (selected !== 'combined') throw new Error('initial selection is ' + selected);
    const present = await page.evaluate(`${JSON.stringify(REMOVED)}.filter(s => document.querySelector(s))`);
    if (present.length) throw new Error('removed controls still in the DOM: ' + present.join(', '));
    // The top-bar Submit button: bold green, one pending badge (hidden at 0), disabled while nothing is pending.
    const submit = await page.evaluate(`(() => { const b = document.querySelector('#btn-submit'); const cs = getComputedStyle(b);
      return { disabled: b.disabled, label: b.querySelector('.label').textContent, zero: b.querySelector('.badge-pending').classList.contains('is-zero'),
        unresolved: Boolean(b.querySelector('.badge-unresolved')), bg: cs.backgroundColor, fg: cs.color, weight: getComputedStyle(b.querySelector('.label')).fontWeight, green: ${SUCCESS_RGB} }; })()`);
    if (!submit.disabled || submit.label !== 'Submit' || !submit.zero || submit.unresolved) throw new Error('submit button state: ' + JSON.stringify(submit));
    if (submit.bg !== submit.green || submit.fg !== 'rgb(255, 255, 255)' || parseInt(submit.weight, 10) < 700) throw new Error('submit button style: ' + JSON.stringify(submit));
    const label = await page.evaluate(`document.querySelector('#btn-comment-review').textContent.trim()`);
    if (!label.endsWith('Comment on the whole series')) throw new Error('review button label: ' + label);
    if (!(await page.evaluate(`document.querySelector('#outdated-note').hidden`))) throw new Error('outdated note shown without outdated comments');
    await page.shot('01-all-changes');
    return await page.evaluate(`document.querySelectorAll('#files .file-card').length + ' files'`);
  });

  await runner.step('cover letter and review comment', async () => {
    const hint = await page.evaluate(`(document.querySelector('#cover-letter') || {}).textContent || ''`);
    if (!/No cover letter/.test(hint)) throw new Error('empty-cover hint missing: ' + JSON.stringify(hint));
    const set = await page.api('/api/cover', { method: 'POST', body: JSON.stringify({ text: COVER }) });
    if (!set || set.cover !== COVER) throw new Error('cover not set: ' + JSON.stringify(set));
    // The long-poll sees the version bump, the review is refetched and the panel re-rendered; the toast marks the end of that refetch.
    await page.waitFor(`document.querySelector('#cover-letter h1') && document.querySelector('#cover-letter strong') && document.querySelector('#cover-letter li code') && document.querySelectorAll('#cover-letter li').length === 2`, { label: 'cover letter rendered', timeout: 15000 });
    await page.waitFor(`[...document.querySelectorAll('#toasts .toast')].some(t => /Cover letter updated/.test(t.textContent)) && document.querySelector('.file-card[data-rendered="1"] table.diff')`, { label: 'cover toast', timeout: 15000 });
    if (await page.evaluate(`document.querySelector('#cover-letter').textContent.includes('No cover letter')`)) throw new Error('hint still shown');
    await page.click('#btn-comment-review');
    await page.waitFor(`document.querySelector('#commit-header .editor-block form.comment-editor[data-key="review:"] textarea')`, { label: 'review editor' });
    if (!(await page.evaluate(`document.activeElement && document.activeElement.tagName === 'TEXTAREA'`))) throw new Error('textarea not focused');
    const info = await page.evaluate(`document.querySelector('#commit-header form.comment-editor .anchor-info').textContent`);
    if (info !== 'whole series') throw new Error('anchor info: ' + info);
    await page.type('Whole-series comment from the e2e driver.');
    await page.click('#commit-header form.comment-editor[data-key="review:"] .btn-submit-comment');
    await page.waitFor(`document.querySelector('#commit-header .thread-block[data-key-host="review"] .thread .comment[data-author="user"] .comment-body') && !document.querySelector('#commit-header form.comment-editor')`, { label: 'review thread rendered' });
    const kind = await page.evaluate(`ccrState.comments.get(document.querySelector('#commit-header .thread-block[data-key-host="review"] .thread').dataset.threadId).anchor.kind`);
    if (kind !== 'review') throw new Error('anchor kind is ' + kind);
    const badge = await page.evaluate(`document.querySelector('#btn-submit .badge-pending').textContent.trim() + (document.querySelector('#btn-submit').disabled ? ' disabled' : '')`);
    if (badge !== '1') throw new Error('pending badge is ' + JSON.stringify(badge));
    await page.shot('01b-cover-letter');
    return 'cover rendered; review thread kind=' + kind;
  });

  await runner.step('click 2nd commit', async () => {
    const before = await page.evaluate('location.hash');
    // A click in the Files pane scrolls to the top of that file; one the 2nd commit also touches, and that commit
    // must still open at its message rather than follow the file.
    const shared = await page.evaluate(`(async () => {
      const sha = document.querySelector('#commit-list .commit-item:nth-child(2)').dataset.sha;
      const diff = await (await fetch('/api/commits/' + sha, { headers: { 'X-CCR-Token': ccrState.token } })).json();
      const paths = new Set(diff.files.map((f) => f.path));
      return [...document.querySelectorAll('#files .file-card')].map((c) => c.dataset.path).find((p) => paths.has(p)) || null; })()`);
    if (!shared) throw new Error('the 2nd commit shares no file with All changes');
    await page.click(`#file-tree .tree-file[data-path=${JSON.stringify(shared)}]`);
    const card = `[...document.querySelectorAll('#files .file-card')].find((c) => c.dataset.path === ${JSON.stringify(shared)})`;
    await page.waitFor(`location.hash === '#combined/' + encodeURIComponent(${JSON.stringify(shared)})`, { label: 'Files-pane click navigated' });
    const off = await page.evaluate(`Math.round(${card}.getBoundingClientRect().top - document.querySelector('#main').getBoundingClientRect().top)`);
    if (Math.abs(off - 16) > 2 || !(await page.evaluate(`document.querySelector('#main').scrollTop > 0`))) throw new Error(`the clicked file's top sits ${off}px below the pane's, not 16px`);
    await page.waitFor(`ccrState.currentFile === [...document.querySelectorAll('#files .file-card')].indexOf(${card})`, { label: 'clicked file is the current one' });
    await page.click('#commit-list .commit-item:nth-child(2)');
    await page.waitFor(`document.querySelector('#commit-list .commit-item:nth-child(2)').classList.contains('is-selected') && document.querySelector('.file-card[data-rendered="1"] table.diff[data-view="unified"]')`);
    const top = await page.evaluate(`document.querySelector('#main').scrollTop`);
    if (top !== 0) throw new Error(`the commit opened scrolled to ${top}px, not at its message`);
    if (await page.evaluate(`Boolean(document.querySelector('#cover-letter') || document.querySelector('#btn-comment-review') || document.querySelector('#commit-header .thread-block[data-key-host="review"]'))`)) throw new Error('cover letter shown on a real commit');
    const hash = await page.evaluate('location.hash');
    if (hash === before || !/^#[0-9a-f]{40,64}$/.test(hash)) throw new Error('hash not updated: ' + hash);
    commitSha = hash.slice(1);
    filePath = await page.evaluate(`document.querySelector('.file-card[data-rendered="1"]').dataset.path`);
    await page.shot('02-commit-unified');
    return `${hash.slice(0, 11)} ${filePath}; subject=` + (await page.evaluate(`document.querySelector('#commit-header .subject').textContent.trim()`));
  });

  await runner.step('hover row and click [+]', async () => {
    const row = `.file-card[data-path="${filePath}"] tr.line.add`;
    await page.hover(row + ' td.code');
    await page.waitFor(`document.querySelector(${JSON.stringify(row)}).querySelector('td.num.new .btn-add-comment')`, { label: 'gutter [+] moved into the hovered row' });
    await page.click(row + ' .btn-add-comment');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor textarea')`, { label: 'editor row' });
    if (!(await page.evaluate(`document.activeElement && document.activeElement.tagName === 'TEXTAREA'`))) throw new Error('textarea not focused');
    const key = await page.evaluate(`document.querySelector('tr.editor form.comment-editor').dataset.key`);
    if (key !== `line:${commitSha}|${filePath}|new|5`) throw new Error('unexpected editor key ' + key);
    return key;
  });

  await runner.step('type and submit comment', async () => {
    await page.type('First **e2e** comment with `code`.');
    await page.click('tr.editor form.comment-editor .btn-submit-comment');
    await page.waitFor(`document.querySelector('tr.threads .thread .comment[data-author="user"] .comment-body strong')`, { label: 'thread rendered' });
    await page.waitFor(`!document.querySelector('tr.editor')`, { label: 'editor closed' });
    const badge = await page.evaluate(`document.querySelector('#btn-submit .badge-pending').textContent.trim()`);
    if (badge !== '2') throw new Error('pending badge is ' + JSON.stringify(badge)); // the review-level comment plus this one
    return 'pending badge=' + badge;
  });

  await runner.step('Write/Preview tabs, auto-grow and Cancel keeps draft', async () => {
    // SPEC 7.4: the editor shows one of the Write and Preview tabs at a time (Preview renders the same safe Markdown as
    // comment bodies), the textarea grows with its content, and Cancel keeps the text as a draft (dot on the [+]);
    // cancelling an emptied editor drops it. There is no Discard button any more.
    const row = `.file-card[data-path="${filePath}"] tr.line.ctx`;
    const text = 'Draft with `code` and **bold**' + '\nand a tail line'.repeat(8);
    await page.hover(row + ' td.code');
    await page.waitFor(`document.querySelector(${JSON.stringify(row)}).querySelector('td.num.new .btn-add-comment')`, { label: 'gutter [+] on the context row' });
    await page.click(row + ' .btn-add-comment');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor textarea')`, { label: 'editor row' });
    const key = await page.evaluate(`document.querySelector('tr.editor form.comment-editor').dataset.key`);
    const measure = `(() => { const ta = document.querySelector('tr.editor textarea');
      return { h: ta.clientHeight, scroll: ta.scrollHeight, hidden: ta.hidden,
               tab: ta.closest('form').dataset.tab, preview: document.querySelector('tr.editor .md-preview').hidden }; })()`;
    const empty = await page.evaluate(measure);
    if (empty.tab !== 'write' || empty.preview !== true || empty.hidden) throw new Error('editor does not open on Write: ' + JSON.stringify(empty));
    await page.type(text);
    await page.frame();
    const grown = await page.evaluate(measure);
    if (grown.h <= empty.h) throw new Error(`textarea did not grow with its content: ${empty.h} → ${grown.h}`);
    if (grown.scroll > grown.h + 1) throw new Error(`textarea still scrolls at ${grown.h}px for ${grown.scroll}px of text`);
    await page.click('tr.editor .editor-tab[data-tab="preview"]');
    await page.waitFor(`(() => { const pv = document.querySelector('tr.editor .md-preview'); const code = pv && pv.querySelector('p > code');
      return pv && !pv.hidden && document.querySelector('tr.editor textarea').hidden && code && code.textContent === 'code' && pv.querySelector('p > strong'); })()`,
      { label: 'Preview tab shows the rendered Markdown and hides the textarea' });
    const mono = await page.evaluate(`getComputedStyle(document.querySelector('tr.editor .md-preview code')).fontFamily`);
    if (!/mono/i.test(mono)) throw new Error('preview code is not monospace: ' + mono);
    await page.shot('02c-preview');
    await page.click('tr.editor .editor-tab[data-tab="write"]');
    await page.waitFor(`document.querySelector('tr.editor .md-preview').hidden && !document.querySelector('tr.editor textarea').hidden`, { label: 'back on Write' });
    await page.click('tr.editor .btn-cancel-comment');
    await page.waitFor(`!document.querySelector('tr.editor')`, { label: 'editor closed' });
    if ((await page.evaluate(`localStorage.getItem(${JSON.stringify('ccr:draft:' + key)})`)) !== text) throw new Error('draft not kept on Cancel');
    await page.hover(row + ' td.code');
    await page.waitFor(`document.querySelector(${JSON.stringify(row)}).querySelector('.btn-add-comment.has-draft')`, { label: 'draft dot on [+]' });
    await page.click(row + ' .btn-add-comment');
    await page.waitFor(`document.querySelector('tr.editor textarea') && document.querySelector('tr.editor textarea').value === ${JSON.stringify(text)}`, { label: 'draft restored' });
    const reopened = await page.evaluate(measure);
    if (reopened.h < grown.h) throw new Error(`reopened editor does not fit its draft: ${reopened.h} < ${grown.h}`);
    // Leaving the commit and coming back rebuilds the diff body from the draft: the editor must still fit it.
    const here = await page.evaluate(`document.querySelector('#commit-list .commit-item.is-selected').dataset.sha`);
    const away = await page.evaluate(`[...document.querySelectorAll('#commit-list .commit-item')].map((i) => i.dataset.sha).find((s) => s !== ${JSON.stringify(here)})`);
    await page.evaluate(`location.hash = '#' + ${JSON.stringify(away)}`);
    await page.waitFor(`!document.querySelector('tr.editor')`, { label: 'other commit shown' });
    await page.evaluate(`location.hash = '#' + ${JSON.stringify(here)}`);
    await page.waitFor(`document.querySelector('tr.editor textarea')`, { label: 'editor back on the original commit' });
    const returned = await page.evaluate(measure);
    if (returned.h < grown.h) throw new Error(`editor collapsed to ${returned.h}px for ${returned.scroll}px of draft after leaving the commit`);
    await page.evaluate(`(() => { const ta = document.querySelector('tr.editor textarea'); ta.value = ''; ta.dispatchEvent(new Event('input', { bubbles: true })); })()`);
    await page.click('tr.editor .editor-tab[data-tab="preview"]');
    await page.waitFor(`document.querySelector('tr.editor .md-preview .preview-empty')`, { label: 'Preview of an empty editor says so' });
    await page.click('tr.editor .editor-tab[data-tab="write"]');
    await page.click('tr.editor .btn-cancel-comment');
    await page.waitFor(`!document.querySelector('tr.editor')`, { label: 'editor closed again' });
    if ((await page.evaluate(`localStorage.getItem(${JSON.stringify('ccr:draft:' + key)})`)) !== null) throw new Error('emptied draft not dropped');
    await page.hover(row + ' td.code');
    await page.waitFor(`document.querySelector(${JSON.stringify(row)}).querySelector('.btn-add-comment:not(.has-draft)')`, { label: 'draft dot gone' });
    return key;
  });

  await runner.step('thread projected into All changes', async () => {
    // SPEC 7.3: a comment written on a commit renders in "All changes" at its git-mapped line, tagged with its origin.
    await page.click('#commit-list .commit-item[data-sha="combined"]');
    await page.waitFor(`document.querySelector('#commit-list .commit-item[data-sha="combined"]').classList.contains('is-selected') && document.querySelector('#cover-letter') && document.querySelector('.file-card[data-rendered="1"] table.diff')`);
    await page.waitFor(`document.querySelector('tr.threads .thread .comment[data-author="user"] .tag-from')`, { label: 'projected thread with .tag-from' });
    const tag = await page.evaluate(`document.querySelector('tr.threads .thread .tag-from').textContent`);
    if (tag !== 'from ' + commitSha.slice(0, 10)) throw new Error('tag text ' + JSON.stringify(tag));
    const key = await page.evaluate(`document.querySelector('tr.threads .thread .tag-from').closest('tr.threads').dataset.key`);
    if (!key.startsWith(`line:combined|${filePath}|new|`)) throw new Error('unexpected thread row key ' + key);
    if (await page.evaluate(`document.querySelectorAll('#main .thread.is-orphan').length`)) throw new Error('projected thread marked orphan');
    await page.shot('02b-projected');
    await page.click('tr.threads .thread .tag-from'); // opens the thread where it was written
    await page.waitFor(`location.hash.startsWith('#${commitSha}') && document.querySelector('#main .thread.is-current .comment[data-author="user"]') && !document.querySelector('#main .tag-from')`, { label: 'tag link opens the native view' });
    // Single-key shortcuts are off by default (KEYBOARD_SHORTCUTS in app.js): `j` must not move to the next file.
    await page.waitFor(`!document.querySelector('#main .is-flash')`, { label: 'flash settled', timeout: 4000 });
    const top = await page.evaluate(`document.querySelector('#main').scrollTop`);
    await page.key('j', 'KeyJ', 74, 0, 'j');
    const after = await page.evaluate(`({ top: document.querySelector('#main').scrollTop, flash: Boolean(document.querySelector('#main .file-card.is-flash')), thread: ccrState.currentThread })`);
    if (after.top !== top || after.flash || !after.thread) throw new Error('`j` had an effect although shortcuts are disabled: ' + JSON.stringify(after));
    return key;
  });

  await runner.step('toggle split view keeps thread', async () => {
    await page.click('#btn-viewmode');
    await page.waitFor(`document.querySelector('#btn-viewmode').textContent === 'Split' && document.querySelector('table.diff[data-view="split"]')`);
    await page.waitFor(`document.querySelector('table.diff[data-view="split"] tr.threads .thread .comment[data-author="user"]')`, { label: 'thread present in split view' });
    const half = await page.evaluate(`(() => { const tr = document.querySelector('table.diff[data-view="split"] tr.threads');
      const on = tr.querySelector('td.on-side'); const off = tr.querySelector('td.off-side'); const w = (el) => el.getBoundingClientRect().width;
      const code = document.querySelector('table.diff[data-view="split"] tr.line td.code.new');
      return { cells: [...tr.children].map((td) => td.className).join(','), thread: Boolean(on.querySelector('.thread')), empty: off.childElementCount === 0,
        right: Math.round(on.getBoundingClientRect().right) === Math.round(code.getBoundingClientRect().right), ratio: Math.round(100 * w(on) / w(tr)) }; })()`);
    if (half.cells !== 'off-side side-old,on-side side-new' || !half.thread || !half.empty || !half.right || half.ratio < 45 || half.ratio > 55) throw new Error('split thread row: ' + JSON.stringify(half));
    await page.shot('03-split');
    await page.click('#btn-viewmode');
    await page.waitFor(`document.querySelector('#btn-viewmode').textContent === 'Unified' && document.querySelector('table.diff[data-view="unified"] tr.threads .thread')`);
    return 'thread survived split → unified';
  });

  await runner.step('drag 3-line range and comment', async () => {
    const cells = await page.evaluate(`(() => {
      const rows = [...document.querySelectorAll('.file-card[data-path=${JSON.stringify(filePath)}] tr.line')].filter(r => r.dataset.n).slice(0, 3);
      return rows.map(r => { const td = r.querySelector('td.num.new'); td.scrollIntoView({ block: 'center' }); const b = td.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2, line: +td.dataset.line }; });
    })()`);
    if (cells.length < 3) throw new Error('not enough rows');
    await page.mouse('mouseMoved', cells[0].x, cells[0].y, { button: 'none' });
    await page.mouse('mousePressed', cells[0].x, cells[0].y);
    await page.mouse('mouseMoved', cells[1].x, cells[1].y, { button: 'left', buttons: 1 });
    await page.mouse('mouseMoved', cells[2].x, cells[2].y, { button: 'left', buttons: 1 });
    await page.mouse('mouseReleased', cells[2].x, cells[2].y);
    await page.waitFor(`document.querySelectorAll('tr.line.in-range').length === 3`, { label: '3 rows in range' });
    const hash = await page.evaluate('location.hash');
    if (!hash.endsWith(`:n${cells[0].line}-${cells[2].line}`)) throw new Error('range hash missing: ' + hash);
    await page.hover('#commit-header .subject'); // leaving the table re-parks the shared [+] (sticky-visible) on the range's end row
    const plus = `.file-card[data-path="${filePath}"] tr.line.in-range .btn-add-comment.is-visible`;
    await page.waitFor(`document.querySelector(${JSON.stringify(plus)})`, { label: '[+] parked on the range end row' });
    await page.click(plus);
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor textarea')`, { label: 'range editor' });
    const info = await page.evaluate(`document.querySelector('tr.editor .anchor-info').textContent`);
    await page.type('Range comment over three lines.');
    await page.key('Enter', 'Enter', 13, 2); // Ctrl+Enter posts even though single-key shortcuts are off
    await page.waitFor(`document.querySelectorAll(${JSON.stringify(MAIN_THREADS)}).length === 2 && !document.querySelector('tr.editor')`, { label: 'range thread' });
    const note = await page.evaluate(`(document.querySelector('.thread .range-note') || {}).textContent || ''`);
    if (!note.includes(`${cells[0].line}–${cells[2].line}`)) throw new Error('range note: ' + note);
    await page.shot('04-threads');
    return `anchor ${info}; ${note}`;
  });

  await runner.step('submit round from the top bar', async () => {
    const before = await page.evaluate(`({ disabled: document.querySelector('#btn-submit').disabled, badge: document.querySelector('#btn-submit .badge-pending').textContent.trim() })`);
    if (before.disabled || before.badge !== '3') throw new Error('submit button before submit: ' + JSON.stringify(before));
    await page.shot('05-pending-submit');
    await page.click('#btn-submit');
    await page.waitFor(`[...document.querySelectorAll('#toasts .toast.success')].some(t => /Round 1 submitted · 3 comments/.test(t.textContent))`, { label: 'submitted toast', timeout: 15000 });
    await page.waitFor(`document.querySelector('#btn-submit').disabled && document.querySelector('#btn-submit .badge-pending').classList.contains('is-zero')`, { label: 'submit button idle again' });
    await page.waitFor(`document.querySelectorAll('#main .tag-round').length === 2 && !document.querySelector('#main .tag-pending')`, { label: 'R1 tags on both line threads' });
    const state = await page.api('/api/state');
    if (state.rounds !== 1 || state.last_round.verdict !== 'comment' || state.last_round.summary !== '') throw new Error('round not recorded: ' + JSON.stringify(state.last_round));
    return `rounds=${state.rounds} comments=${state.last_round.comment_ids.length}`;
  });

  await runner.step('claude reply → toast + New dot', async () => {
    const rootId = await page.evaluate(`document.querySelector(${JSON.stringify(MAIN_THREADS)}).dataset.threadId`);
    // Collapse the file first: a thread that stays on screen for ~1 s is marked seen, which would clear the New dot we assert.
    await page.click(`.file-card[data-path="${filePath}"] .btn-collapse`);
    await page.waitFor(`document.querySelector('.file-card[data-path=${JSON.stringify(filePath)}]').classList.contains('is-collapsed')`, { label: 'file collapsed' });
    const reply = await page.api('/api/comments', { method: 'POST', body: JSON.stringify({ body: 'Fixed in the next commit.', parent_id: rootId, author: 'claude' }) });
    if (!reply || reply.parent_id !== rootId) throw new Error('reply not created: ' + JSON.stringify(reply));
    await page.waitFor(`[...document.querySelectorAll('#toasts .toast')].some(t => /Claude replied to 1 thread/.test(t.textContent))`, { label: 'Claude toast', timeout: 15000 });
    const dot = `#main .thread.has-new[data-thread-id=${JSON.stringify(rootId)}] .comment[data-author="claude"][data-id=${JSON.stringify(reply.id)}] .tag-new`;
    await page.waitFor(`document.querySelector(${JSON.stringify(dot)})`, { label: 'New dot on the reply' });
    await page.shot('06-claude-reply');
    await page.click('#toasts .toast .toast-action'); // "Show" navigates to the thread (expanding the file)
    await page.waitFor(`document.querySelector('#main .thread.is-current[data-thread-id=${JSON.stringify(rootId)}]') && !document.querySelector('.file-card[data-path=${JSON.stringify(filePath)}]').classList.contains('is-collapsed')`, { label: 'thread focused' });
    await page.waitFor(`!document.querySelector('#main .tag-new') && !document.querySelector('#main .thread.has-new')`, { label: 'New dot cleared after being on screen', timeout: 5000 });
    return `reply=${reply.id}; New dot cleared automatically`;
  });

  await runner.step('reload keeps token', async () => {
    await page.navigate(`${origin}/#${commitSha}`);
    if (await page.evaluate(`!document.querySelector('#notice-token').hidden`)) throw new Error('token notice shown after reload');
    await page.waitFor(`document.querySelectorAll(${JSON.stringify(MAIN_THREADS)}).length === 2`, { label: 'threads after reload' });
    const stored = await page.evaluate(`localStorage.getItem(${JSON.stringify('ccr:token:' + port)})`);
    if (stored !== token) throw new Error('token missing from localStorage after reload');
    return 'token survived; threads=' + (await page.evaluate(`document.querySelectorAll(${JSON.stringify(MAIN_THREADS)}).length`));
  });
}

/** The pull request the PR-mode server is linked to (mirrors PR_URL in test_e2e.py). */
export const PR_URL = 'https://github.com/o/r/pull/7';
/** When the review "Since your last review" starts from was submitted (mirrors REVIEWED_AT in tests/conftest.py). */
export const SINCE_AT = '2023-11-15T09:30:00Z';

/** PR mode (spec section 10) against the same fixture, linked to PR_URL: questions for Claude and GitHub comments. */
export async function runPrScenario(page, url) {
  const runner = new Runner(page, page.report);
  const card = '.file-card[data-path="src/app.py"]';
  const row = (n) => `${card} tr.line[data-n="${n}"]`;
  const isShown = (sel) => `(() => { const el = document.querySelector(${JSON.stringify(sel)}); return Boolean(el) && getComputedStyle(el).display !== 'none'; })()`;
  const editorLabel = `document.querySelector('tr.editor .btn-submit-comment').textContent`;
  const threadOf = (text) => `[...document.querySelectorAll('#main .thread')].find((t) => t.textContent.includes(${JSON.stringify(text)}))`;
  let githubId = null;
  const openFork = async (n) => {
    await page.hover(row(n) + ' td.code');
    await page.hover(row(n) + ' .btn-fork');
    await page.waitFor(isShown(row(n) + ' .btn-add-comment[data-intent="github"]'), { label: `[+] of line ${n} open` });
  };
  // the smallest screen PR mode is meant for
  await page.cdp.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });

  await runner.step('load page in PR mode', async () => {
    await page.navigate(url);
    await page.waitFor(`document.querySelector('.file-card[data-rendered="1"] table.diff')`);
    const link = await page.evaluate(`(() => { const a = document.querySelector('#pr-link'); return { hidden: a.hidden, text: a.textContent, href: a.href, target: a.target }; })()`);
    if (link.hidden || link.text !== 'PR #7' || link.href !== PR_URL || link.target !== '_blank') throw new Error('PR link: ' + JSON.stringify(link));
    return link.text;
  });

  await runner.step('question about the whole pull request', async () => {
    const label = await page.evaluate(`document.querySelector('#btn-comment-review').textContent.trim()`);
    if (!label.endsWith('Ask AI about the whole pull request')) throw new Error('review button label: ' + label);
    await page.click('#btn-comment-review');
    await page.waitFor(`document.querySelector('#commit-header form.comment-editor[data-key="review:"] textarea')`, { label: 'review editor' });
    const editor = await page.evaluate(`(() => { const f = document.querySelector('#commit-header form.comment-editor'); return { label: f.querySelector('.btn-submit-comment').textContent, intent: Boolean(f.querySelector('.editor-intent')), info: f.querySelector('.anchor-info').textContent }; })()`);
    if (editor.label !== 'Ask AI' || editor.intent || editor.info !== 'whole pull request') throw new Error('review editor: ' + JSON.stringify(editor));
    await page.type('Why does the series need two commits?');
    await page.click('#commit-header form.comment-editor .btn-submit-comment');
    await page.waitFor(`document.querySelector('#commit-header .thread-block[data-key-host="review"] .thread .tag-question')`, { label: 'review question tagged' });
    const replies = await page.evaluate(`[...document.querySelectorAll('#commit-header .thread-block[data-key-host="review"] .thread-foot .btn-reply')].map((b) => b.textContent).join(',')`);
    if (replies !== 'Ask AI') throw new Error('a thread on the whole pull request offers ' + replies);
    return 'tagged Question';
  });

  await runner.step('click 2nd commit', async () => {
    await page.click('#commit-list .commit-item:nth-child(2)');
    await page.waitFor(`document.querySelector('#commit-list .commit-item:nth-child(2)').classList.contains('is-selected') && document.querySelector(${JSON.stringify(card + '[data-rendered="1"] table.diff')})`);
    return await page.evaluate(`document.querySelector('#commit-header .subject').textContent.trim()`);
  });

  await runner.step('gutter [+] opens into Ask AI and GH comment', async () => {
    await page.hover(row(5) + ' td.code');
    const plus = row(5) + ' td.num.new .btn-fork';
    const q = row(5) + ' td.num.new .btn-add-comment[data-intent="question"]';
    const g = row(5) + ' td.num.new .btn-add-comment[data-intent="github"]';
    await page.waitFor(`${isShown(plus)} && !${isShown(q)} && !${isShown(g)}`, { label: 'a closed [+] in the hovered row' });
    const plusLeft = await page.evaluate(`document.querySelector(${JSON.stringify(plus)}).getBoundingClientRect().left`);
    await page.hover(plus);
    await page.waitFor(`${isShown(q)} && ${isShown(g)} && !${isShown(plus)}`, { label: 'hovering [+] opens it' });
    await page.hover(row(5) + ' td.code'); // off the buttons (a hovered one grows), still on the line: it stays open
    const boxes = await page.evaluate(`[${JSON.stringify(q)}, ${JSON.stringify(g)}].map((s) => { const el = document.querySelector(s); const r = el.getBoundingClientRect(); return { text: el.textContent, left: r.left, right: r.right, label: el.getAttribute('aria-label'), clipped: el.scrollWidth > el.clientWidth }; })`);
    if (boxes[0].text !== 'Ask AI' || boxes[1].text !== 'GH comment' || boxes[0].right > boxes[1].left || boxes.some((b) => b.clipped)
        || Math.abs(boxes[0].left - plusLeft) > 4) throw new Error('gutter buttons: ' + JSON.stringify({ plusLeft, boxes }));
    const tint = (sel) => `getComputedStyle(document.querySelector(${JSON.stringify(sel)})).backgroundImage !== 'none'`;
    if (!(await page.evaluate(`${tint(row(5) + ' td.code')} && !${tint(row(6) + ' td.code')}`))) throw new Error('the hovered row alone is not grey');
    if (await page.evaluate(`Boolean(document.querySelector('#main .btn-add-comment:not([data-intent]):not(.btn-fork)'))`)) throw new Error('a plain [+] is left in PR mode');
    await page.shot('pr-01-gutter');
    await page.hover(row(6) + ' td.code');
    await page.waitFor(`${isShown(row(6) + ' .btn-fork')} && !${isShown(row(6) + ' .btn-add-comment[data-intent="github"]')}`, { label: 'closed again on another line' });
    return boxes.map((b) => b.label).join(' | ');
  });

  await runner.step('split view: the buttons by the hovered side', async () => {
    await page.click('#btn-viewmode');
    await page.waitFor(`document.querySelector(${JSON.stringify(card + '[data-rendered="1"] table.diff[data-view="split"]')})`, { label: 'split view' });
    const sides = {};
    for (const side of ['old', 'new']) {
      await page.hover(`${row(6)} td.code.${side}`);
      await page.waitFor(`${isShown(`${row(6)} td.num.${side} .btn-fork`)}`, { label: `[+] by the ${side} side` });
      await page.click(`${row(6)} td.num.${side} .btn-fork`);
      await page.waitFor(`${isShown(`${row(6)} td.num.${side} .btn-add-comment[data-intent="github"]`)}`, { label: `clicking [+] opens it on the ${side} side` });
      if (await page.evaluate(`Boolean(document.querySelector('tr.editor'))`)) throw new Error('the click on [+] went to Ask AI, which took its place');
      sides[side] = await page.evaluate(`(() => { const r = document.querySelector(${JSON.stringify(row(6))});
        const grey = [...r.children].filter((td) => getComputedStyle(td).backgroundImage !== 'none').map((td) => td.className.trim()).join(',');
        return { buttons: [...r.querySelectorAll('.btn-add-comment[data-intent]')].map((b) => b.closest('td').className.trim() + ':' + b.dataset.side).join(','), grey }; })()`);
    }
    if (sides.old.buttons !== 'num old:old,num old:old' || sides.new.buttons !== 'num new:new,num new:new'
        || sides.old.grey !== 'num old,code old' || sides.new.grey !== 'num new,code new') throw new Error('split gutter: ' + JSON.stringify(sides));
    // the whole interface fits a 1080p screen in either view, and the 1280x720 one this scenario runs at
    const overflow = `[document.documentElement, document.querySelector('#main')].map((e) => e.scrollWidth - e.clientWidth).join(',')`;
    const fits = [await page.evaluate(overflow)];
    await page.cdp.send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
    fits.push(await page.evaluate(overflow));
    await page.click('#btn-viewmode');
    await page.waitFor(`document.querySelector(${JSON.stringify(card + '[data-rendered="1"] table.diff[data-view="unified"]')})`, { label: 'unified view again' });
    fits.push(await page.evaluate(overflow));
    await page.cdp.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });
    if (fits.some((f) => f !== '0,0')) throw new Error('horizontal overflow (split 1280, split 1920, unified 1920): ' + fits.join(' / '));
    return `old: ${sides.old.grey} · new: ${sides.new.grey}`;
  });

  await runner.step('GitHub comment on a line', async () => {
    await openFork(5);
    await page.click(row(5) + ' .btn-add-comment[data-intent="github"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor[data-intent="github"] textarea')`, { label: 'GitHub editor' });
    const label = await page.evaluate(editorLabel);
    if (label !== 'Add GH comment') throw new Error('submit label ' + label);
    if (!(await page.evaluate(`Boolean(document.querySelector('tr.editor .intent-btn.is-active[data-intent="github"]'))`))) throw new Error('GitHub not selected in the switch');
    await page.type('Why 500?');
    await page.click('tr.editor .btn-submit-comment');
    await page.waitFor(`!document.querySelector('tr.editor') && ${threadOf('Why 500?')} && ${threadOf('Why 500?')}.querySelector('.tag-github:not(.is-posted)')`, { label: 'GitHub thread tagged not posted' });
    githubId = await page.evaluate(`${threadOf('Why 500?')}.dataset.threadId`);
    const status = await page.evaluate(`ccrState.comments.get(${JSON.stringify(githubId)}).github.status`);
    if (status !== 'local') throw new Error('github status ' + status);
    await page.shot('pr-02-github-comment');
    return githubId;
  });

  await runner.step('question with the editor switch', async () => {
    await openFork(6);
    await page.click(row(6) + ' .btn-add-comment[data-intent="question"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor[data-channel="claude"]:not([data-intent]) textarea')`, { label: 'question editor' });
    if ((await page.evaluate(editorLabel)) !== 'Ask AI') throw new Error('question label ' + (await page.evaluate(editorLabel)));
    await page.type('What is value 6 for?');
    await page.click('tr.editor .intent-btn[data-intent="github"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor[data-intent="github"][data-channel="github"]') && document.querySelector('tr.editor textarea').value === 'What is value 6 for?'`, { label: 'switched to GitHub, text kept' });
    if ((await page.evaluate(editorLabel)) !== 'Add GH comment') throw new Error('label after switching');
    await page.click('tr.editor .intent-btn[data-intent="question"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor:not([data-intent])') && ${editorLabel} === 'Ask AI'`, { label: 'switched back' });
    await page.click('tr.editor .btn-submit-comment');
    await page.waitFor(`!document.querySelector('tr.editor') && ${threadOf('What is value 6 for?')} && ${threadOf('What is value 6 for?')}.querySelector('.comment[data-channel="claude"] .tag-question')`, { label: 'question thread' });
    return 'switch kept the text';
  });

  await runner.step('a draft keeps its kind', async () => {
    const gh = row(7) + ' .btn-add-comment[data-intent="github"]';
    const q = row(7) + ' .btn-add-comment[data-intent="question"]';
    await openFork(7);
    await page.click(gh);
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor[data-intent="github"] textarea')`, { label: 'GitHub editor on line 7' });
    const key = await page.evaluate(`document.querySelector('tr.editor form.comment-editor').dataset.key`);
    await page.type('A GitHub draft');
    await page.click('tr.editor .btn-cancel-comment');
    await page.waitFor(`!document.querySelector('tr.editor')`, { label: 'editor closed, draft kept' });
    await page.hover(row(7) + ' td.code');
    await page.waitFor(`document.querySelector(${JSON.stringify(row(7) + ' .btn-fork.has-draft')})`, { label: 'the closed [+] shows the draft' });
    await openFork(7);
    await page.waitFor(`document.querySelector(${JSON.stringify(gh + '.has-draft')}) && !document.querySelector(${JSON.stringify(q + '.has-draft')})`, { label: 'the dot is on GH only' });
    if ((await page.evaluate(`localStorage.getItem(${JSON.stringify('ccr:draft-intent:' + key)})`)) !== 'github') throw new Error('draft intent not stored');
    await page.click(gh);
    await page.waitFor(`document.querySelector('tr.editor textarea') && document.querySelector('tr.editor textarea').value === 'A GitHub draft'`, { label: 'draft restored' });
    await page.evaluate(`(() => { const ta = document.querySelector('tr.editor textarea'); ta.value = ''; ta.dispatchEvent(new Event('input', { bubbles: true })); })()`);
    await page.click('tr.editor .btn-cancel-comment');
    await page.waitFor(`!document.querySelector('tr.editor') && localStorage.getItem(${JSON.stringify('ccr:draft-intent:' + key)}) === null`, { label: 'emptied draft dropped with its kind' });
    return key;
  });

  await runner.step('a line outside the pull request diff stays a question', async () => {
    await page.click(`${card} tr.hunk[data-gap="1"] .btn-expand-all`);
    await page.waitFor(`document.querySelector(${JSON.stringify(row(10) + '[data-x="1"]')})`, { label: 'context line 10 expanded' });
    await openFork(10);
    await page.click(row(10) + ' .btn-add-comment[data-intent="github"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor[data-intent="github"] textarea')`, { label: 'GitHub editor on a context line' });
    await page.type('Unrelated to the change');
    page.expected.push(/status of 400 \(Bad Request\) .*\/api\/comments$/); // the refusal is the point of this step
    await page.click('tr.editor .btn-submit-comment');
    await page.waitFor(`[...document.querySelectorAll('#toasts .toast.error')].some((t) => /is not in the pull request diff/.test(t.textContent))`, { label: 'refusal toast' });
    if (!(await page.evaluate(`Boolean(document.querySelector('tr.editor textarea')) && !document.querySelector('tr.editor form').classList.contains('is-busy')`))) throw new Error('editor closed or stuck after the refusal');
    await page.click('tr.editor .intent-btn[data-intent="question"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor:not([data-intent])')`, { label: 'question again' });
    await page.click('tr.editor .btn-submit-comment');
    await page.waitFor(`!document.querySelector('tr.editor') && ${threadOf('Unrelated to the change')}`, { label: 'posted as a question' });
    return 'refused for GitHub, accepted as a question';
  });

  await runner.step('file header forks too', async () => {
    const buttons = await page.evaluate(`[...document.querySelectorAll(${JSON.stringify(card + ' .file-header .btn-comment-file')})].map((b) => b.dataset.intent + ':' + b.textContent)`);
    if (buttons.join(',') !== 'question:Ask AI,github:GH comment') throw new Error('file buttons ' + buttons);
    await page.click(card + ' .file-header .btn-comment-file[data-intent="github"]');
    await page.waitFor(`document.querySelector(${JSON.stringify(card + ' .thread-block[data-key-host="file"] form.comment-editor[data-intent="github"] textarea')})`, { label: 'file GitHub editor' });
    await page.type('Please split this file.');
    await page.click(card + ' .thread-block[data-key-host="file"] .btn-submit-comment');
    await page.waitFor(`document.querySelector(${JSON.stringify(card + ' .thread-block[data-key-host="file"] .thread .tag-github')})`, { label: 'file GitHub thread' });
    return buttons.join(' ');
  });

  await runner.step('posted comment links to GitHub', async () => {
    const posted = await page.api(`/api/comments/${githubId}/github`, { method: 'POST', body: JSON.stringify({ posted: { url: PR_URL + '#discussion_r42', comment_id: 42 } }) });
    if (!posted || posted.github.status !== 'posted') throw new Error('not recorded: ' + JSON.stringify(posted));
    await page.waitFor(`[...document.querySelectorAll('#toasts .toast')].some((t) => /1 GitHub comment posted to your pending review/.test(t.textContent))`, { label: 'posted toast', timeout: 15000 });
    const link = `#main .thread[data-thread-id="${githubId}"] a.tag-github.is-posted`;
    await page.waitFor(`document.querySelector(${JSON.stringify(link)})`, { label: 'GitHub link tag' });
    const tag = await page.evaluate(`(() => { const a = document.querySelector(${JSON.stringify(link)}); return { href: a.href, target: a.target, edit: Boolean(a.closest('.comment').querySelector('.act-edit')), del: Boolean(a.closest('.comment').querySelector('.act-delete')) }; })()`);
    if (tag.href !== PR_URL + '#discussion_r42' || tag.target !== '_blank' || !tag.edit || !tag.del) throw new Error('posted tag: ' + JSON.stringify(tag));
    await page.api(`/api/comments/${githubId}`, { method: 'PATCH', body: JSON.stringify({ body: 'Why 500, not 50?' }) });
    await page.waitFor(`[...document.querySelectorAll(${JSON.stringify(`#main .thread[data-thread-id="${githubId}"] .tag-github`)})].some((t) => t.textContent === 'edit not posted')`, { label: 'an edited posted comment says its edit is not posted', timeout: 15000 });
    const submitTitle = await page.evaluate(`document.querySelector('#btn-submit').title`);
    if (!submitTitle.startsWith('Send 5 pending comments to Claude')) throw new Error('submit title: ' + submitTitle);
    await page.shot('pr-03-posted');
    return tag.href;
  });

  await runner.step('GitHub threads come into ccr', async () => {
    const head = (await page.api('/api/review')).range.head;
    // the reply carries the same time as its root, as mirrored comments can: the root must still lead its thread
    const at = (id, login, body, replyTo) => ({ id, database_id: id.length, body, url: `${PR_URL}#discussion_${id}`,
      created_at: '2026-09-01T10:00:00Z', edited_at: null, state: 'SUBMITTED', login, reply_to: replyTo || null });
    const thread = (id, comments, extra) => Object.assign({ id, path: 'src/app.py', line: 5, start_line: null, original_line: 5,
      original_start_line: null, side: 'RIGHT', subject_type: 'LINE', outdated: false, resolved: false, comments }, extra);
    const synced = await page.api('/api/github/sync', { method: 'POST', body: JSON.stringify({ viewer: 'reviewer', head,
      threads: [thread('T1', [at('C1', 'nyh', 'Why does value 5 change?'), at('C2', 'radek', 'Because the spec says so.', 'C1')]),
        thread('T2', [at('C3', 'nyh', 'An old remark')], { outdated: true, line: null, original_line: 2 }),
        thread('T3', [Object.assign(at('P42', 'reviewer', 'Why 500?'), { database_id: 42, state: 'PENDING' }),
          at('C4', 'nyh', 'Because 50 is too small.', 'P42')])],
      reviews: [{ id: 'R1', database_id: 1, body: 'Please fix.', url: `${PR_URL}#pullrequestreview-1`, state: 'CHANGES_REQUESTED',
        submitted_at: '2026-09-01T10:00:00Z', login: 'nyh' }] }) });
    if (!synced || synced.added !== 5) throw new Error('sync: ' + JSON.stringify(synced));
    const nyh = `${threadOf('Why does value 5 change?')}`;
    await page.waitFor(`${nyh} && ${nyh}.querySelector('.comment[data-author="github"] .author').textContent === '@nyh'`, { label: 'mirrored thread at its line', timeout: 15000 });
    const order = await page.evaluate(`[...${nyh}.querySelectorAll('.comment .author')].map((a) => a.textContent).join(' ')`);
    if (!order.startsWith('@nyh @radek')) throw new Error('thread order: ' + order);
    const view = await page.evaluate(`(() => { const t = ${nyh}; return { row: t.closest('tr.threads').dataset.key,
      link: t.querySelector('a.tag-github').href, edit: Boolean(t.querySelector('.comment[data-author="github"] .act-edit, .comment[data-author="github"] .act-delete')),
      replies: t.querySelectorAll('.comment[data-author="github"]').length }; })()`);
    if (!view.row.endsWith('|src/app.py|new|5') || view.link !== PR_URL + '#discussion_C1' || view.edit || view.replies !== 2) throw new Error('mirrored thread: ' + JSON.stringify(view));
    if (await page.evaluate(`${nyh}.querySelectorAll('.comment:not([data-channel="github"])').length`)) throw new Error('a mirrored comment outside the GitHub channel');
    await page.waitFor(`[...document.querySelectorAll(${JSON.stringify(card + ' .thread-block[data-key-host="file"] .resolved-line')})].some((l) => /GitHub thread by @nyh · outdated/.test(l.textContent))`, { label: 'outdated thread collapsed on the file' });
    const mine = JSON.stringify(`#main .thread[data-thread-id="${githubId}"]`);
    await page.waitFor(`document.querySelector(${mine}) && document.querySelector(${mine}).textContent.includes('Because 50 is too small.')`, { label: "nyh's reply under the posted comment" });
    if (await page.evaluate(`document.querySelector(${mine}).querySelectorAll('.act-delete').length`)) throw new Error('a thread holding a GitHub reply offers Delete');
    const replies = (sel) => `[...document.querySelectorAll(${JSON.stringify(sel)})].map((b) => b.dataset.intent + ':' + b.textContent).join(',')`;
    const nyhId = await page.evaluate(`${nyh}.dataset.threadId`);
    const offers = await page.evaluate(`[${replies(`#main .thread[data-thread-id="${nyhId}"] .thread-foot .btn-reply`)},
      ${replies(`#main .thread[data-thread-id="${nyhId}"] .comment[data-author="github"] .act-reply`)},
      ${replies(`#main .thread[data-thread-id="${await page.evaluate(`${threadOf('What is value 6 for?')}.dataset.threadId`)}"] .thread-foot .btn-reply`)}]`);
    if (offers.join(' / ') !== 'question:Ask AI,github:GH reply / question:Ask AI,github:GH reply,question:Ask AI,github:GH reply / question:Ask AI,github:GH reply') throw new Error('reply buttons: ' + offers.join(' / '));
    const oldId = await page.evaluate(`[...document.querySelectorAll(${JSON.stringify(card + ' .thread-block[data-key-host="file"] .thread')})].find((t) => /GitHub thread by @nyh · outdated/.test(t.textContent)).dataset.threadId`);
    await page.click(`#main .thread[data-thread-id="${oldId}"] .resolved-line .btn-reply[data-intent="question"]`);
    await page.waitFor(`(() => { const t = document.querySelector('#main .thread[data-thread-id="${oldId}"]'); return t && t.textContent.includes('An old remark') && t.querySelector('form.comment-editor[data-mode="reply"][data-channel="claude"] textarea'); })()`, { label: 'Ask AI on a collapsed thread opens it with a question editor' });
    await page.click(`#main .thread[data-thread-id="${oldId}"] .btn-cancel-comment`);
    await page.click(`#main .thread[data-thread-id="${nyhId}"] .thread-foot .btn-reply[data-intent="question"]`);
    await page.waitFor(`document.querySelector('.editor-block form.comment-editor[data-mode="reply"] .intent-btn[data-intent="github"]')`, { label: 'reply editor with the GitHub switch' });
    const switchText = await page.evaluate(`[...document.querySelectorAll('.editor-block form.comment-editor[data-mode="reply"] .intent-btn')].map((b) => b.textContent).join('|')`);
    if (switchText !== 'Ask AI|GH reply') throw new Error('reply switch: ' + switchText);
    if (!(await page.evaluate(`Boolean(document.querySelector('.editor-block form.comment-editor[data-mode="reply"][data-channel="claude"]'))`))) throw new Error('a reply starts as a question');
    await page.type('Agreed, see the design.');
    await page.click('.editor-block form.comment-editor[data-mode="reply"] .intent-btn[data-intent="github"]');
    await page.waitFor(`document.querySelector('.editor-block form.comment-editor[data-mode="reply"][data-intent="github"][data-channel="github"] .btn-submit-comment').textContent === 'Add GH reply' && document.querySelector('.editor-block form.comment-editor[data-mode="reply"] textarea').value === 'Agreed, see the design.'`, { label: 'switched to a GitHub reply' });
    await page.click('.editor-block form.comment-editor[data-mode="reply"] .btn-submit-comment');
    await page.waitFor(`${nyh} && [...${nyh}.querySelectorAll('.comment[data-author="user"][data-channel="github"]')].some((c) => c.textContent.includes('Agreed, see the design.') && c.querySelector('.tag-github:not(.is-posted)'))`, { label: 'GitHub reply in the thread, not posted yet' });
    await page.click(`#main .thread[data-thread-id="${nyhId}"] .thread-foot .btn-reply[data-intent="github"]`);
    await page.waitFor(`document.querySelector('.editor-block form.comment-editor[data-mode="reply"][data-intent="github"] textarea')`, { label: 'GH reply opens a GitHub reply' });
    await page.click(`#main .thread[data-thread-id="${nyhId}"] .thread-foot .btn-reply[data-intent="question"]`);
    await page.waitFor(`document.querySelector('.editor-block form.comment-editor[data-mode="reply"][data-channel="claude"]:not([data-intent]) textarea')`, { label: 'Ask AI turns the open editor into a question' });
    await page.type('Which spec does radek mean?');
    await page.click('.editor-block form.comment-editor[data-mode="reply"] .btn-submit-comment');
    await page.waitFor(`${nyh} && [...${nyh}.querySelectorAll('.comment[data-author="user"][data-channel="claude"]')].some((c) => c.textContent.includes('Which spec does radek mean?') && c.querySelector('.tag-question'))`, { label: 'question in the GitHub thread' });
    const colours = await page.evaluate(`(() => { const bg = (sel) => getComputedStyle(${nyh}.querySelector(sel)).backgroundColor;
      return [bg('.comment[data-author="github"]'), bg('.comment[data-author="user"][data-channel="github"]'), bg('.comment[data-channel="claude"]')]; })()`);
    if (colours[0] !== colours[1] || colours[0] === colours[2] || colours.some((c) => /^(transparent|rgba\(0, 0, 0, 0\))$/.test(c))) throw new Error('channel colours: ' + colours.join(' / '));
    await page.shot('pr-04-github-thread');
    return view.row;
  });

  await runner.step('GH reply in a question thread', async () => {
    const q = threadOf('What is value 6 for?');
    await page.click(`#main .thread[data-thread-id="${await page.evaluate(`${q}.dataset.threadId`)}"] .thread-foot .btn-reply[data-intent="github"]`);
    await page.waitFor(`${q}.querySelector('form.comment-editor[data-mode="reply"][data-intent="github"] textarea')`, { label: 'GitHub reply editor in a question thread' });
    if ((await page.evaluate(`${q}.querySelector('.btn-submit-comment').textContent`)) !== 'Add GH reply') throw new Error('submit label');
    await page.type('Should value 6 be named?');
    await page.click('.editor-block form.comment-editor[data-mode="reply"] .btn-submit-comment');
    await page.waitFor(`[...${q}.querySelectorAll('.comment[data-author="user"][data-channel="github"]')].some((c) => c.textContent.includes('Should value 6 be named?') && c.querySelector('.tag-github:not(.is-posted)'))`, { label: 'GitHub reply in the question thread, not posted yet' });
    const tags = await page.evaluate(`[...${q}.querySelectorAll('.comment')].map((c) => c.dataset.channel + ':' + [...c.querySelectorAll('.tag-question, .tag-github')].map((t) => t.textContent).join('')).join(' | ')`);
    if (tags !== 'claude:Question | github:GitHub · not posted') throw new Error('question thread: ' + tags);
    return tags;
  });

  await runner.step('Bold and Italic on the selected text', async () => {
    await openFork(8);
    await page.click(row(8) + ' .btn-add-comment[data-intent="question"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor textarea')`, { label: 'editor on line 8' });
    const ta = `document.querySelector('tr.editor textarea')`;
    const bar = `getComputedStyle(document.querySelector('tr.editor .editor-format')).visibility === 'visible'`;
    await page.type('make it bold');
    if (await page.evaluate(bar)) throw new Error('format buttons shown with nothing selected');
    await page.key('i', 'KeyI', 73, 2);
    if ((await page.evaluate(`${ta}.value`)) !== 'make it bold') throw new Error('Ctrl+I changed text with nothing selected');
    await page.evaluate(`(() => { const t = ${ta}; t.focus(); t.setSelectionRange(8, 12); })()`);
    await page.waitFor(bar, { label: 'format buttons on a selection' });
    const states = [];
    for (const press of [() => page.key('b', 'KeyB', 66, 2), () => page.key('b', 'KeyB', 66, 2), () => page.click('tr.editor .fmt-btn[data-fmt="italic"]')]) {
      await press();
      states.push(await page.evaluate(`(() => { const t = ${ta}; return t.value + ' [' + t.value.slice(t.selectionStart, t.selectionEnd) + ']'; })()`));
    }
    if (states.join(' / ') !== 'make it **bold** [bold] / make it bold [bold] / make it _bold_ [bold]') throw new Error('formatting: ' + states.join(' / '));
    await page.evaluate(`(() => { const t = ${ta}; t.focus(); t.setSelectionRange(8, 14); })()`);
    await page.key('b', 'KeyB', 66, 2);
    if ((await page.evaluate(`${ta}.value`)) !== 'make it **_bold_**') throw new Error('bold over italic: ' + (await page.evaluate(`${ta}.value`)));
    await page.click('tr.editor .editor-tab[data-tab="preview"]');
    await page.waitFor(`document.querySelector('tr.editor .md-preview strong > em') && document.querySelector('tr.editor .md-preview em').textContent === 'bold' && !(${bar})`, { label: 'bold italic in the preview, no buttons there' });
    await page.click('tr.editor .editor-tab[data-tab="write"]');
    await page.evaluate(`(() => { const t = ${ta}; t.focus(); t.setSelectionRange(3, 3); })()`);
    await page.waitFor(`!(${bar})`, { label: 'buttons gone with the selection' });
    await page.evaluate(`(() => { const t = ${ta}; t.value = 'call snake_case_name'; t.focus(); t.setSelectionRange(11, 15); })()`);
    await page.key('i', 'KeyI', 73, 2);
    if ((await page.evaluate(`${ta}.value`)) !== 'call snake__case__name') throw new Error('an identifier lost its underscores: ' + (await page.evaluate(`${ta}.value`)));
    await page.evaluate(`(() => { const t = ${ta}; t.value = ''; t.dispatchEvent(new Event('input', { bubbles: true })); })()`);
    await page.click('tr.editor .btn-cancel-comment');
    await page.waitFor(`!document.querySelector('tr.editor')`, { label: 'editor closed' });
    return states.join(' / ');
  });
}

/** "Since your last review" (re-review, spec 2.1) against the re-reviewed pull request of tests/conftest.py, in PR mode:
 *  `base2..v2` opened since the review of `reviewed`. */
export async function runSinceScenario(page, url) {
  const runner = new Runner(page, page.report);
  const card = '.file-card[data-path="src/calc.py"]';
  const oldRow = (n) => `${card} tr.line.del[data-o="${n}"]`;
  const isShown = (sel) => `(() => { const el = document.querySelector(${JSON.stringify(sel)}); return Boolean(el) && getComputedStyle(el).display !== 'none'; })()`;

  await runner.step('Since your last review is a group of its own', async () => {
    await page.navigate(url);
    await page.waitFor(`document.querySelectorAll('#commit-list .commit-item').length === 3`);
    const list = await page.evaluate(`(() => { const items = [...document.querySelector('#commit-list').children];
      const since = items[0];
      return { order: items.map((li) => li.dataset.sha || li.className), subject: since.querySelector('.subject').textContent,
        when: since.querySelector('.since-at').textContent, size: getComputedStyle(since.querySelector('.since-at')).fontSize,
        subjectSize: getComputedStyle(since.querySelector('.subject')).fontSize }; })()`);
    const expected = await page.evaluate(`new Date(${JSON.stringify(SINCE_AT)}).toLocaleString()`);
    if (list.order.join(',') !== 'since,commit-sep,combined,' + list.order[3]) throw new Error('commit list: ' + list.order);
    if (list.subject !== 'Since your last review' || list.when !== expected) throw new Error('since item: ' + JSON.stringify(list));
    if (parseFloat(list.size) >= parseFloat(list.subjectSize)) throw new Error('the date is not in small type: ' + JSON.stringify(list));
    return list.when;
  });

  await runner.step('open Since your last review', async () => {
    await page.click('#commit-list .commit-item[data-sha="since"]');
    await page.waitFor(`document.querySelector('#commit-list .commit-item[data-sha="since"]').classList.contains('is-selected') && document.querySelector(${JSON.stringify(card + '[data-rendered="1"] table.diff')})`);
    const header = await page.evaluate(`(() => { const h = document.querySelector('#commit-header');
      return { subject: h.querySelector('.subject').textContent.trim(), tag: h.querySelector('.kind-tag').textContent,
        explain: h.querySelector('.explain').textContent, conflicts: Boolean(h.querySelector('.since-conflicts')),
        files: [...document.querySelectorAll('.file-card[data-path]')].map((c) => c.dataset.path) }; })()`);
    if (header.subject !== 'Since your last review' || header.tag !== 'Re-review' || header.conflicts) throw new Error('header: ' + JSON.stringify(header));
    if (!/rebuilt on its current base .* compared with its head/.test(header.explain)) throw new Error('explanation: ' + header.explain);
    if (header.files.join(',') !== 'src/calc.py,tests/test_calc.py') throw new Error('files: ' + header.files);
    await page.shot('since-view');
    return header.files.join(', ');
  });

  await runner.step('the reviewed side takes questions only', async () => {
    await page.hover(oldRow(25) + ' td.code');
    await page.hover(oldRow(25) + ' .btn-fork');
    await page.waitFor(isShown(oldRow(25) + ' .btn-add-comment[data-intent="question"]'), { label: '[+] of the reviewed line open' });
    if (await page.evaluate(isShown(oldRow(25) + ' .btn-add-comment[data-intent="github"]'))) throw new Error('GH comment offered on the version reviewed');
    await page.click(oldRow(25) + ' .btn-add-comment[data-intent="question"]');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor textarea')`, { label: 'editor' });
    const editor = await page.evaluate(`(() => { const f = document.querySelector('tr.editor form.comment-editor'); return { label: f.querySelector('.btn-submit-comment').textContent, intent: Boolean(f.querySelector('.editor-intent')), info: f.querySelector('.anchor-info').textContent }; })()`);
    if (editor.label !== 'Ask AI' || editor.intent || editor.info !== 'old:25') throw new Error('editor: ' + JSON.stringify(editor));
    await page.type('Why did mul change?');
    await page.click('tr.editor .btn-submit-comment');
    await page.waitFor(`!document.querySelector('tr.editor') && [...document.querySelectorAll('#main .thread')].some((t) => t.textContent.includes('Why did mul change?'))`, { label: 'question posted' });
    const replies = await page.evaluate(`[...document.querySelector(${JSON.stringify(card)}).querySelectorAll('.thread .thread-foot .btn-reply')].map((b) => b.textContent).join(',')`);
    if (replies !== 'Ask AI') throw new Error('a thread on the version reviewed offers ' + replies);
    return 'question only';
  });
}

/** Entry point: launch, run, always kill the browser, print the report. */
export async function main(argv) {
  const url = argv[2];
  const shotsDir = argv[3] || join(process.cwd(), 'shots');
  const scenario = { pr: runPrScenario, since: runSinceScenario }[argv[4]] || runScenario;
  if (!url) { process.stderr.write('usage: node driver.mjs <url-with-?t=token> [shots-dir] [review|pr|since]\n'); return 2; }
  mkdirSync(shotsDir, { recursive: true });
  const report = { ok: true, steps: [], consoleErrors: [], screenshots: [] };
  let browser = null;
  let page = null;
  const cleanup = () => {
    if (page) page.cdp.close();
    if (browser) {
      try { browser.proc.kill('SIGKILL'); } catch (e) { /* already gone */ }
      rmSync(browser.profile, { recursive: true, force: true });
    }
  };
  const onSignal = () => { cleanup(); process.exit(1); };
  process.on('SIGTERM', onSignal);
  process.on('SIGINT', onSignal);
  try {
    browser = await launchChrome();
    page = await openPage(browser.ws, shotsDir);
    page.token = new URL(url).searchParams.get('t');
    page.report = report;
    await scenario(page, url);
  } catch (e) {
    report.ok = false;
    report.steps.push({ name: 'driver', ok: false, detail: e.stack || String(e) });
  } finally {
    if (page) { report.consoleErrors = page.unexpectedErrors(); report.screenshots = page.screenshots; }
    cleanup();
  }
  if (report.consoleErrors.length) report.ok = false;
  process.stdout.write(JSON.stringify(report) + '\n');
  return report.ok ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main(process.argv);
}

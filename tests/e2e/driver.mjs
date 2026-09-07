#!/usr/bin/env node
/**
 * driver.mjs — headless-Chromium CDP driver for the ccr web UI (SPEC.md section 9, "test_e2e.py").
 *
 * Usage: node driver.mjs <url-with-?t=token> [shots-dir]
 *
 * Talks to Chromium over the DevTools protocol using Node's built-in WebSocket (Node >= 22, no
 * dependencies) and walks the review UI through the end-to-end scenario: load with the token in
 * `?t=`, grammar check, set a cover letter through the API and comment on the whole change from the
 * "All changes" header, select the second commit, comment on a hovered line via the gutter [+], see that
 * comment projected into "All changes" with a "from <sha>" tag that leads back, toggle split view, drag a
 * three-line range and comment on it, submit a review round from the
 * drawer, receive a Claude reply pushed through the API (toast + "New" tab), and reload the page
 * without `?t=` to prove the token survives in localStorage.
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
  constructor(cdp, shotsDir) { this.cdp = cdp; this.shotsDir = shotsDir; this.errors = []; this.screenshots = []; }

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

  /** The drawer slides in with a CSS transition; wait until it is fully in place before clicking inside. */
  settleDrawer() {
    return this.waitFor(`Math.abs(document.querySelector('#drawer').getBoundingClientRect().right - window.innerWidth) < 2`, { label: 'drawer slid in' });
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
    await page.type('Whole-change comment from the e2e driver.');
    await page.click('#commit-header form.comment-editor[data-key="review:"] .btn-submit-comment');
    await page.waitFor(`document.querySelector('#commit-header .thread-block[data-key-host="review"] .thread .comment[data-author="user"] .comment-body') && !document.querySelector('#commit-header form.comment-editor')`, { label: 'review thread rendered' });
    const kind = await page.evaluate(`ccrState.comments.get(document.querySelector('#commit-header .thread-block[data-key-host="review"] .thread').dataset.threadId).anchor.kind`);
    if (kind !== 'review') throw new Error('anchor kind is ' + kind);
    const badge = await page.evaluate(`document.querySelector('#btn-review .badge-pending').textContent.trim()`);
    if (badge !== '1') throw new Error('pending badge is ' + JSON.stringify(badge));
    await page.shot('01b-cover-letter');
    return 'cover rendered; review thread kind=' + kind;
  });

  await runner.step('click 2nd commit', async () => {
    const before = await page.evaluate('location.hash');
    await page.click('#commit-list .commit-item:nth-child(2)');
    await page.waitFor(`document.querySelector('#commit-list .commit-item:nth-child(2)').classList.contains('is-selected') && document.querySelector('.file-card[data-rendered="1"] table.diff[data-view="unified"]')`);
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
    const badge = await page.evaluate(`document.querySelector('#btn-review .badge-pending').textContent.trim()`);
    if (badge !== '2') throw new Error('pending badge is ' + JSON.stringify(badge)); // the review-level comment plus this one
    return 'pending badge=' + badge;
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
    if (await page.evaluate(`document.querySelectorAll('#main .thread.is-orphan, #main .tag-orphan').length`)) throw new Error('projected thread marked orphan');
    await page.shot('02b-projected');
    await page.click('tr.threads .thread .tag-from'); // opens the thread where it was written
    await page.waitFor(`location.hash.startsWith('#${commitSha}') && document.querySelector('#main .thread.is-current .comment[data-author="user"]') && !document.querySelector('#main .tag-from')`, { label: 'tag link opens the native view' });
    await page.key('Escape', 'Escape', 27); // drop the thread focus so \`c\` below comments on the selection, not the thread
    if (await page.evaluate('ccrState.currentThread')) throw new Error('thread still current after Esc');
    return key;
  });

  await runner.step('toggle split view keeps thread', async () => {
    await page.click('#btn-viewmode');
    await page.waitFor(`document.querySelector('#btn-viewmode').textContent === 'Split' && document.querySelector('table.diff[data-view="split"]')`);
    await page.waitFor(`document.querySelector('table.diff[data-view="split"] tr.threads .thread .comment[data-author="user"]')`, { label: 'thread present in split view' });
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
    await page.key('c', 'KeyC', 67, 0, 'c');
    await page.waitFor(`document.querySelector('tr.editor form.comment-editor textarea')`, { label: 'range editor' });
    const info = await page.evaluate(`document.querySelector('tr.editor .anchor-info').textContent`);
    await page.type('Range comment over three lines.');
    await page.key('Enter', 'Enter', 13, 2);
    await page.waitFor(`document.querySelectorAll(${JSON.stringify(MAIN_THREADS)}).length === 2 && !document.querySelector('tr.editor')`, { label: 'range thread' });
    const note = await page.evaluate(`(document.querySelector('.thread .range-note') || {}).textContent || ''`);
    if (!note.includes(`${cells[0].line}–${cells[2].line}`)) throw new Error('range note: ' + note);
    await page.shot('04-threads');
    return `anchor ${info}; ${note}`;
  });

  await runner.step('open drawer and submit review', async () => {
    await page.click('#btn-review');
    await page.waitFor(`document.querySelector('#drawer').classList.contains('is-open')`);
    await page.waitFor(`document.querySelectorAll('#drawer-list .drawer-item').length === 3`, { label: '3 drawer items' });
    await page.settleDrawer();
    await page.click('#drawer input[name=verdict][value="request_changes"]');
    if (!(await page.evaluate(`document.querySelector('#drawer input[name=verdict][value="request_changes"]').checked`))) throw new Error('verdict radio not checked');
    await page.click('#review-summary');
    await page.type('Two comments from the e2e driver.');
    await page.shot('05-drawer-pending');
    const before = await page.api('/api/state');
    await page.click('#btn-submit-review');
    await page.waitFor(`document.querySelectorAll('#rounds-list .round-item').length === ${before.rounds + 1}`, { label: 'round listed' });
    await page.waitFor(`document.querySelector('#btn-review .badge-pending').classList.contains('is-zero')`, { label: 'pending badge cleared' });
    const state = await page.api('/api/state');
    if (state.rounds !== 1 || state.last_round.verdict !== 'request_changes') throw new Error('round not recorded: ' + JSON.stringify(state.last_round));
    if (state.last_round.summary !== 'Two comments from the e2e driver.') throw new Error('summary not recorded: ' + state.last_round.summary);
    return `rounds=${state.rounds} verdict=${state.last_round.verdict}`;
  });

  await runner.step('claude reply → toast + New tab', async () => {
    const rootId = await page.evaluate(`document.querySelector(${JSON.stringify(MAIN_THREADS)}).dataset.threadId`);
    const reply = await page.api('/api/comments', { method: 'POST', body: JSON.stringify({ body: 'Fixed in the next commit.', parent_id: rootId, author: 'claude' }) });
    if (!reply || reply.parent_id !== rootId) throw new Error('reply not created: ' + JSON.stringify(reply));
    await page.waitFor(`[...document.querySelectorAll('#toasts .toast')].some(t => /Claude replied to 1 thread/.test(t.textContent))`, { label: 'Claude toast', timeout: 15000 });
    // Read the count right away: a visible comment is marked seen after ~1 s, which clears its "New" state.
    const count = await page.evaluate(`parseInt(document.querySelector('#drawer .tab[data-tab="new"] .count').textContent, 10)`);
    if (!(count >= 1)) throw new Error('New tab count is ' + count);
    await page.waitFor(`document.querySelector('#main .comment[data-author="claude"][data-id=${JSON.stringify(reply.id)}]')`, { label: 'claude comment rendered' });
    await page.settleDrawer();
    await page.click('#drawer .tab[data-tab="new"]');
    await page.waitFor(`document.querySelector('#drawer .tab[data-tab="new"]').classList.contains('is-active') || document.querySelector('#drawer .tab[data-tab="new"][aria-selected="true"]')`, { label: 'New tab active' });
    await page.shot('06-claude-reply');
    return `new=${count} reply=${reply.id}`;
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

/** Entry point: launch, run, always kill the browser, print the report. */
export async function main(argv) {
  const url = argv[2];
  const shotsDir = argv[3] || join(process.cwd(), 'shots');
  if (!url) { process.stderr.write('usage: node driver.mjs <url-with-?t=token> [shots-dir]\n'); return 2; }
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
    await runScenario(page, url);
  } catch (e) {
    report.ok = false;
    report.steps.push({ name: 'driver', ok: false, detail: e.stack || String(e) });
  } finally {
    if (page) { report.consoleErrors = page.errors; report.screenshots = page.screenshots; }
    cleanup();
  }
  if (report.consoleErrors.length) report.ok = false;
  process.stdout.write(JSON.stringify(report) + '\n');
  return report.ok ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main(process.argv);
}

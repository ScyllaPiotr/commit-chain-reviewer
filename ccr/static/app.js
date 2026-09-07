/* ccr — Commit Chain Reviewer — browser UI (spec section 7).
 *
 * Vanilla ES2020, no framework, no build step. One file, organised in sections:
 *   1. helpers, state, API client, boot
 *   2. top bar, sidebar (commit chain + file tree), header card, file cards
 *   3. diff model: blocks, unified/split rows, hunk expansion, highlighting, word diff
 *   4. gutter [+], pointer range selection
 *   5. comments: anchor keys, reconciliation, threads, editors, drafts, Markdown
 *   6. live updates (long-poll), seen tracking, banners
 *   7. navigation (hash), keyboard, drawer, help, event wiring
 *
 * Every piece of dynamic text passes through esc(). No inline styles: computed styles are
 * assigned through CSSOM properties or classes (strict CSP).
 */
(() => {
  'use strict';

  /* ==================================================================== 1. helpers */

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  /** Escape text for insertion into HTML markup (element content and attribute values). */
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const HEX_RE = /^[0-9a-f]{7,64}$/;
  const EXPAND_STEP = 20;
  const NEW_DOT_TITLE = 'Not yet part of a submitted round — Claude can already read it with ccr comments';

  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* quota / blocked */ } },
    del(key) { try { localStorage.removeItem(key); } catch (e) { /* ignore */ } },
    keys(prefix) {
      const out = [];
      try {
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (k && k.startsWith(prefix)) out.push(k);
        }
      } catch (e) { /* ignore */ }
      return out;
    },
    json(key, fallback) {
      const raw = storage.get(key);
      if (raw == null) return fallback;
      try { return JSON.parse(raw); } catch (e) { return fallback; }
    },
  };

  /** Stable 0..7 hash of a name → avatar colour class index. */
  function nameHash(name) {
    let h = 0;
    for (const ch of String(name || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return h % 8;
  }

  function initials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return parts.length === 1 ? parts[0].slice(0, 2).toUpperCase() : (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function avatarHtml(author, name, large) {
    const cls = large ? 'avatar lg' : 'avatar';
    if (author === 'claude') return `<span class="${cls} robot av-6" aria-hidden="true">C</span>`;
    const label = !name || name === 'user' ? 'U' : initials(name);
    return `<span class="${cls} av-${nameHash(name || author)}" aria-hidden="true">${esc(label)}</span>`;
  }

  const serverNow = () => Date.now() + (state.nowOffset || 0);

  function fmtRel(iso) {
    if (!iso) return '';
    const diff = Math.max(0, serverNow() - Date.parse(iso)) / 1000;
    if (diff < 45) return 'just now';
    if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
    if (diff < 86400) { const h = Math.round(diff / 3600); return `${h} hour${h === 1 ? '' : 's'} ago`; }
    if (diff < 86400 * 30) { const d = Math.round(diff / 86400); return `${d} day${d === 1 ? '' : 's'} ago`; }
    return new Date(iso).toLocaleDateString();
  }

  function fmtAbs(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  function timeHtml(iso, cls = 'time') {
    return `<span class="${cls}" data-ts="${esc(iso)}" title="${esc(fmtAbs(iso))}">${esc(fmtRel(iso))}</span>`;
  }

  function refreshTimes() {
    for (const el of $$('[data-ts]')) el.textContent = fmtRel(el.dataset.ts);
  }

  function shortSha(sha) { return sha && HEX_RE.test(sha) ? sha.slice(0, 10) : sha; }
  const isPseudo = (sha) => sha === 'combined' || sha === 'worktree';

  function debounce(fn, ms) {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function statsHtml(add, del) {
    return `<span class="stats"><span class="stat-add">+${esc(add)}</span><span class="stat-del">−${esc(del)}</span></span>`;
  }

  /* ---- toasts */
  function toast(message, kind = 'info', opts = {}) {
    const host = $('#toasts');
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    let html = `<span class="toast-text">${esc(message)}</span>`;
    if (opts.action) html += `<button type="button" class="toast-action">${esc(opts.action.label)}</button>`;
    html += '<button type="button" class="toast-close" aria-label="Dismiss">×</button>';
    el.innerHTML = html;
    const close = () => el.remove();
    el.querySelector('.toast-close').addEventListener('click', close);
    if (opts.action) el.querySelector('.toast-action').addEventListener('click', () => { close(); opts.action.fn(); });
    host.appendChild(el);
    while (host.children.length > 4) host.firstElementChild.remove();
    if (opts.timeout !== 0) setTimeout(close, opts.timeout || (kind === 'error' ? 8000 : 5000));
    return el;
  }

  async function copyText(text, label = 'Copied') {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(text);
      else {
        const ta = document.createElement('textarea');
        ta.value = text; ta.setAttribute('readonly', '');
        ta.className = 'sr-only';
        document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
      }
      toast(label, 'success', { timeout: 2000 });
    } catch (e) {
      toast('Copy failed — ' + text, 'error');
    }
  }

  /* ==================================================================== state */

  const state = {
    token: null, review: null, generation: null, version: 0, startedAt: null, nowOffset: 0,
    selectedSha: null, compare: null, viewSha: null,
    viewMode: 'unified', wrap: true, wsIgnore: false, theme: 'auto',
    diffs: new Map(), fileText: new Map(), hl: new Map(),
    comments: new Map(), threadsByKey: new Map(), threadOrder: [], orphans: new Set(),
    openEditors: new Map(), sel: null, currentFile: 0, currentThread: null,
    viewed: new Set(), collapsedFolders: new Set(), collapsedFiles: new Set(), expandedViewed: new Set(),
    drawer: { open: false, tab: 'pending', thisCommitOnly: false },
    seenUntil: '', perCommitScroll: new Map(),
    // derived / transient
    counts: { byCommit: new Map(), byFile: new Map(), pendingComments: 0, unresolvedThreads: 0 },
    knownIds: new Set(), expandedResolved: new Set(), fileFilter: '', hideViewed: false,
    loadedOnce: false, polling: false, pollSeq: 0, pollAbort: null, pollRole: null, disconnectedSince: null,
    rangeAnchorSha: null, currentDrawerFilterPath: null, inflight: new Set(), hoverThread: null,
  };
  window.ccrState = state;

  const repoKey = () => (state.review ? state.review.repo.path : 'unknown');
  const currentDiff = () => (state.viewSha ? state.diffs.get(state.viewSha) : null);
  const commentsDisabled = () => Boolean(state.compare);
  const lastRound = () => (state.review && state.review.rounds.length ? state.review.rounds[state.review.rounds.length - 1] : null);

  function commitMeta(sha) {
    return state.review ? state.review.commits.find((c) => c.sha === sha) : null;
  }
  /** Resolve a full/short sha or pseudo name against the listed commits. */
  function findCommit(ref) {
    if (!state.review || !ref) return null;
    const exact = commitMeta(ref);
    if (exact) return exact;
    if (!/^[0-9a-f]{4,64}$/.test(ref)) return null;
    const matches = state.review.commits.filter((c) => c.sha.startsWith(ref));
    return matches.length === 1 ? matches[0] : null;
  }
  const realCommits = () => (state.review ? state.review.commits.filter((c) => c.kind === 'commit') : []);
  /** The view (listed sha) a thread belongs to: its anchor commit, or "combined" for review-level anchors. */
  const anchorView = (a) => (a ? (a.kind === 'review' ? 'combined' : a.commit || null) : null);
  function commitIndex(sha) {
    return state.review ? state.review.commits.findIndex((c) => c.sha === sha) : -1;
  }

  /* ==================================================================== API */

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function api(path, { method = 'GET', body, signal } = {}) {
    const headers = { 'X-CCR-Token': state.token || '' };
    const init = { method, headers, signal, cache: 'no-store', credentials: 'omit' };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      throw new ApiError(0, 'network error');
    }
    if (res.status === 401) { onUnauthorized(); throw new ApiError(401, 'unauthorized'); }
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
    if (!res.ok) throw new ApiError(res.status, (data && data.error) || `HTTP ${res.status}`);
    return data;
  }

  function onUnauthorized() {
    stopPolling();
    storage.del('ccr:token:' + location.port);
    state.token = null;
    showTokenNotice();
  }

  function showTokenNotice() {
    $('#app').hidden = true;
    $('#notice-token').hidden = false;
    document.body.dataset.ready = '1';
  }

  /* ==================================================================== boot */

  function loadPrefs() {
    state.viewMode = storage.get('ccr:viewmode') === 'split' ? 'split' : 'unified';
    state.wrap = storage.get('ccr:wrap') !== '0';
    state.wsIgnore = storage.get('ccr:ws') === '1';
    state.theme = ['light', 'dark'].includes(storage.get('ccr:theme')) ? storage.get('ccr:theme') : 'auto';
    const sw = parseInt(storage.get('ccr:sidebar-w') || '', 10);
    if (sw) $('#app').style.setProperty('--sidebar-w', clamp(sw, 200, 480) + 'px');
    const dw = parseInt(storage.get('ccr:drawer-w') || '', 10);
    if (dw) $('#app').style.setProperty('--drawer-w', clamp(dw, 320, 720) + 'px');
    if (storage.get('ccr:sidebar') === 'collapsed') $('#app').classList.add('sidebar-collapsed');
    state.hideViewed = storage.get('ccr:hide-viewed') === '1';
    $('#chk-hide-viewed').checked = state.hideViewed;
    applyViewPrefs();
  }

  function loadRepoPrefs() {
    state.collapsedFolders = new Set(storage.json('ccr:folders:' + repoKey(), []));
    state.seenUntil = storage.get('ccr:seen:' + repoKey()) || '';
    state.viewed = new Set();
    const cutoff = Date.now() - 30 * 86400 * 1000;
    for (const k of storage.keys('ccr:viewed:')) {
      const t = Date.parse(storage.get(k) || '');
      if (Number.isNaN(t) || t < cutoff) { storage.del(k); continue; }
      state.viewed.add(k);
    }
  }

  function applyViewPrefs() {
    $('#btn-viewmode').textContent = state.viewMode === 'split' ? 'Split' : 'Unified';
    $('#btn-wrap').setAttribute('aria-pressed', String(state.wrap));
    $('#btn-ws').setAttribute('aria-pressed', String(state.wsIgnore));
    $('#app').classList.toggle('nowrap', !state.wrap);
    $('#btn-theme').textContent = state.theme === 'auto' ? 'Auto' : state.theme === 'light' ? 'Light' : 'Dark';
  }

  function applyTheme() {
    const dark = state.theme === 'dark' || (state.theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.themePref = state.theme;
    $('#hl-light').disabled = dark;
    $('#hl-dark').disabled = !dark;
    applyViewPrefs();
  }

  function cycleTheme() {
    state.theme = state.theme === 'auto' ? 'light' : state.theme === 'light' ? 'dark' : 'auto';
    storage.set('ccr:theme', state.theme);
    applyTheme();
  }

  async function boot() {
    const url = new URL(location.href);
    const tokenKey = 'ccr:token:' + location.port;
    const fromQuery = url.searchParams.get('t');
    const token = fromQuery || storage.get(tokenKey);
    if (url.searchParams.has('t')) {
      url.searchParams.delete('t');
      history.replaceState(null, '', url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : '') + url.hash);
    }
    if (!token) { showTokenNotice(); return; }
    state.token = token;
    storage.set(tokenKey, token);
    loadPrefs();
    applyTheme();
    wireEvents();
    await initialLoad();
  }

  async function fetchReviewWhenReady() {
    let review = await api('/api/review');
    if (review.loading) {
      document.body.dataset.loading = '1';
      $('#commit-header').innerHTML = '<div class="explain"><span class="spinner"></span> Extracting commits…</div>';
      while (review.loading) {
        await sleep(500);
        const st = await api('/api/state');
        if (!st.loading) review = await api('/api/review');
      }
      delete document.body.dataset.loading;
    }
    return review;
  }

  async function initialLoad() {
    try {
      const review = await fetchReviewWhenReady();
      applyReview(review, { initial: true });
      loadRepoPrefs();
      loadReviewDraft();
      const cs = await api('/api/comments');
      applyComments(cs.comments, { initial: true });
      state.version = Math.max(state.version, cs.version);
      await navigateFromHash();
      renderDrawer();
      document.body.dataset.ready = '1';
      state.loadedOnce = true;
      state.disconnectedSince = null;
      $('#banner-disconnected').hidden = true;
      startPolling();
    } catch (e) {
      if (e.status === 401) return;
      $('#commit-header').innerHTML = `<div class="explain">Could not load the review: ${esc(e.message)}</div>`;
      document.body.dataset.ready = '1';
      onDisconnected(e);
      startPolling();
    }
  }

  /** Store the review document, capture server identity/time and refresh the chrome. */
  function applyReview(review, { initial = false } = {}) {
    state.review = review;
    state.generation = review.generation;
    state.version = Math.max(state.version || 0, review.version || 0);
    state.nowOffset = review.now ? Date.parse(review.now) - Date.now() : 0;
    if (initial || !state.startedAt) state.startedAt = review.server ? review.server.started_at : null;
    renderTopbar();
    renderCommitList();
    renderRounds();
    updateBadges();
    renderCoverLetter();
  }

  /* ==================================================================== 2. top bar */

  function renderTopbar() {
    const r = state.review;
    $('#repo-name').textContent = r.repo.name;
    $('#repo-branch').textContent = r.repo.branch || (r.repo.bare ? 'bare' : 'detached HEAD');
    const spec = $('#range-spec');
    spec.textContent = r.range.spec || '';
    spec.title = r.range.note || (r.range.given && r.range.given !== r.range.spec ? `given: ${r.range.given}` : '');
    spec.classList.toggle('has-note', Boolean(r.range.note));
    const n = realCommits().length;
    $('#commit-count').textContent = `${n} commit${n === 1 ? '' : 's'}${r.options.worktree ? ' · +worktree' : ''}`;
    document.title = `${r.repo.name} — ccr`;
  }

  function updateBadges() {
    const c = state.counts;
    const bp = $('#btn-review .badge-pending');
    const bu = $('#btn-review .badge-unresolved');
    bp.textContent = String(c.pendingComments);
    bp.classList.toggle('is-zero', c.pendingComments === 0);
    bu.textContent = String(c.unresolvedThreads);
    bu.classList.toggle('is-zero', c.unresolvedThreads === 0);
    $('#btn-review').title = `Review drawer (r) — ${c.pendingComments} pending comments, ${c.unresolvedThreads} unresolved threads`;
    const submit = $('#btn-submit-review');
    submit.textContent = `Submit review (${c.pendingComments})`;
    const verdict = ($('input[name=verdict]:checked') || {}).value;
    submit.disabled = c.pendingComments === 0 && verdict !== 'approve' && !$('#review-summary').value.trim();
  }

  /* ==================================================================== sidebar: commit chain */

  function commitBadgesHtml(sha) {
    const c = state.counts.byCommit.get(sha);
    if (!c || !c.threads) return '';
    let html = '';
    if (c.pending) html += `<span class="badge commit-badge badge-pending" title="${esc(c.pending)} pending">${esc(c.pending)}</span>`;
    if (c.unresolved) html += `<span class="badge commit-badge badge-unresolved" title="${esc(c.unresolved)} unresolved">${esc(c.unresolved)}</span>`;
    if (!c.pending && !c.unresolved) html += `<span class="badge commit-badge" title="${esc(c.threads)} threads">${esc(c.threads)}</span>`;
    if (c.hasNew) html += '<span class="unseen-dot" title="Unseen comments"></span>';
    return html;
  }

  function renderCommitList() {
    const list = $('#commit-list');
    const lr = lastRound();
    const rangeSet = compareRangeSet();
    const real = realCommits();
    const html = state.review.commits.map((c) => {
      const pseudo = c.kind !== 'commit';
      const clean = c.kind === 'worktree' && c.stats.files === 0;
      const isNew = !pseudo && lr && !lr.commit_shas.includes(c.sha);
      const cls = ['commit-item', pseudo ? 'is-pseudo' : '', c.is_merge ? 'is-merge' : '', clean ? 'is-clean' : '',
        !state.compare && c.sha === state.selectedSha ? 'is-selected' : '', rangeSet.has(c.sha) ? 'is-range' : '',
        isNew ? 'is-new' : '', c === real[0] ? 'is-chain-start' : '', c === real[real.length - 1] ? 'is-chain-end' : ''].filter(Boolean).join(' ');
      const subject = clean ? `${c.subject} (clean)` : c.subject;
      const line2 = pseudo
        ? `<span>${c.kind === 'combined' ? esc(rangeLabel()) : 'vs HEAD'}</span> ${statsHtml(c.stats.additions, c.stats.deletions)}`
        : `${avatarHtml('user', c.author.name)} ${timeHtml(c.author_date, 'when')} ${statsHtml(c.stats.additions, c.stats.deletions)}${c.is_merge ? ' <span class="merge-glyph" title="Merge commit">⑂</span>' : ''}`;
      return `<li class="${cls}" data-sha="${esc(c.sha)}" tabindex="0" role="button" aria-current="${c.sha === state.selectedSha ? 'true' : 'false'}">
        <span class="rail"><span class="dot"></span></span>
        <span class="body"><span class="line1"><span class="sha">${esc(pseudo ? (c.kind === 'combined' ? 'all' : 'wt') : c.short_sha.slice(0, 7))}</span><span class="subject">${esc(subject)}</span><span class="new-dot" title="Not part of the last submitted round"></span></span>
        <span class="line2">${line2}</span></span>
        <span class="side">${commitBadgesHtml(c.sha)}</span></li>`;
    }).join('');
    list.innerHTML = html;
  }

  function rangeLabel() {
    const r = state.review.range;
    return r.spec || `${shortSha(r.base) || 'root'}..${shortSha(r.head)}`;
  }

  function compareRangeSet() {
    const set = new Set();
    if (!state.compare) return set;
    const commits = realCommits();
    const headIdx = commits.findIndex((c) => c.sha === state.compare.head);
    if (headIdx < 0) return set;
    for (let i = headIdx; i >= 0; i--) {
      set.add(commits[i].sha);
      if (commits[i].parents[0] === state.compare.base || (!state.compare.base && commits[i].parents.length === 0)) break;
    }
    return set;
  }

  function updateCommitSelection() {
    const rangeSet = compareRangeSet();
    for (const li of $$('#commit-list .commit-item')) {
      const sel = !state.compare && li.dataset.sha === state.selectedSha;
      li.classList.toggle('is-selected', sel);
      li.classList.toggle('is-range', rangeSet.has(li.dataset.sha));
      li.setAttribute('aria-current', sel ? 'true' : 'false');
      if (sel) li.scrollIntoView({ block: 'nearest' });
    }
  }

  function updateCommitBadges() {
    for (const li of $$('#commit-list .commit-item')) li.querySelector('.side').innerHTML = commitBadgesHtml(li.dataset.sha);
  }

  /* ---- tooltip */
  let tooltipTimer = null;
  function scheduleTooltip(item) {
    clearTimeout(tooltipTimer);
    tooltipTimer = setTimeout(() => showTooltip(item), 300);
  }
  function showTooltip(item) {
    const c = commitMeta(item.dataset.sha);
    if (!c) return;
    const tt = $('#tooltip');
    const meta = c.kind === 'commit'
      ? `${esc(c.author.name)} &lt;${esc(c.author.email)}&gt; · ${esc(fmtAbs(c.author_date))}<br>${esc(c.sha)}${c.parents.length > 1 ? `<br>merge of ${c.parents.map((p) => esc(shortSha(p))).join(' + ')}` : ''}`
      : (c.kind === 'combined' ? `git diff ${esc(rangeLabel())}` : 'staged + unstaged + untracked vs HEAD');
    tt.innerHTML = `<div class="tt-subject">${esc(c.subject)}</div>${c.body ? `<div class="tt-body">${esc(c.body)}</div>` : ''}<div class="tt-meta">${meta}</div>`;
    tt.hidden = false;
    const r = item.getBoundingClientRect();
    const w = tt.offsetWidth; const h = tt.offsetHeight;
    let left = r.right + 8;
    if (left + w > window.innerWidth - 8) left = Math.max(8, r.left);
    let top = r.top;
    if (top + h > window.innerHeight - 8) top = Math.max(8, window.innerHeight - h - 8);
    tt.style.left = left + 'px';
    tt.style.top = top + 'px';
  }
  function hideTooltip() {
    clearTimeout(tooltipTimer);
    $('#tooltip').hidden = true;
  }

  /* ==================================================================== sidebar: file tree */

  function buildTree(files) {
    const root = { name: '', dirs: new Map(), files: [] };
    for (const f of files) {
      const parts = f.path.split('/');
      let node = root;
      for (let i = 0; i < parts.length - 1; i++) {
        if (!node.dirs.has(parts[i])) node.dirs.set(parts[i], { name: parts[i], dirs: new Map(), files: [] });
        node = node.dirs.get(parts[i]);
      }
      node.files.push(f);
    }
    return root;
  }

  /** Path compression: a directory with exactly one child directory (and no files) merges with it. */
  function compress(node, prefix) {
    const out = [];
    for (const [, dir] of node.dirs) {
      let label = dir.name; let cur = dir; let full = prefix ? `${prefix}/${dir.name}` : dir.name;
      while (cur.dirs.size === 1 && cur.files.length === 0) {
        const [only] = cur.dirs.values();
        label += '/' + only.name; full += '/' + only.name; cur = only;
      }
      out.push({ label, full, node: cur });
    }
    out.sort((a, b) => a.label.localeCompare(b.label));
    return out;
  }

  function treeFileHtml(f, showFull) {
    const key = `${state.viewSha}|${f.path}`;
    const c = state.counts.byFile.get(key);
    const viewed = isViewed(f);
    const name = showFull ? f.path : f.path.split('/').pop();
    return `<div class="tree-file${f.path === currentFilePath() ? ' is-current' : ''}" data-path="${esc(f.path)}" role="treeitem" tabindex="0" title="${esc(f.old_path ? `${f.old_path} → ${f.path}` : f.path)}">
      <span class="st st-${esc(f.status)}" aria-label="${esc(statusName(f.status))}">${esc(f.status)}</span>
      <span class="name"><bdi>${esc(name)}</bdi></span>
      ${c && c.threads ? `<span class="tcount${c.hasNew ? ' has-new' : ''}" title="${esc(c.threads)} threads">${esc(c.threads)}</span>` : ''}
      ${viewed ? '<span class="tick" title="Viewed">✓</span>' : ''}
      <span class="nums">${f.binary ? 'bin' : `<span class="stat-add">+${esc(f.additions)}</span> <span class="stat-del">−${esc(f.deletions)}</span>`}</span>
    </div>`;
  }

  function treeDirHtml(entry) {
    const collapsed = state.collapsedFolders.has(entry.full);
    const children = compress(entry.node, entry.full).map(treeDirHtml).join('')
      + entry.node.files.slice().sort((a, b) => a.path.localeCompare(b.path)).map((f) => treeFileHtml(f, false)).join('');
    return `<div class="tree-folder${collapsed ? ' is-collapsed' : ''}" data-dir="${esc(entry.full)}" role="treeitem" tabindex="0" aria-expanded="${!collapsed}">
      <span class="chev" aria-hidden="true">▾</span><span class="name">${esc(entry.label)}</span></div><div class="tree-children" role="group">${children}</div>`;
  }

  function renderFileTree() {
    const diff = currentDiff();
    const tree = $('#file-tree');
    if (!diff) { tree.innerHTML = ''; $('#tree-count').textContent = ''; return; }
    const files = diff.files;
    $('#tree-count').textContent = String(files.length);
    if (!files.length) { tree.innerHTML = '<div class="tree-empty">No files</div>'; return; }
    if (state.fileFilter) {
      const q = state.fileFilter.toLowerCase();
      const matches = files.filter((f) => f.path.toLowerCase().includes(q) || (f.old_path && f.old_path.toLowerCase().includes(q)));
      tree.innerHTML = matches.length ? matches.map((f) => treeFileHtml(f, true)).join('') : '<div class="tree-empty">No files match</div>';
      return;
    }
    const root = buildTree(files);
    tree.innerHTML = compress(root, '').map(treeDirHtml).join('')
      + root.files.slice().sort((a, b) => a.path.localeCompare(b.path)).map((f) => treeFileHtml(f, false)).join('');
  }

  function toggleFolder(el) {
    const dir = el.dataset.dir;
    if (state.collapsedFolders.has(dir)) state.collapsedFolders.delete(dir); else state.collapsedFolders.add(dir);
    storage.set('ccr:folders:' + repoKey(), JSON.stringify([...state.collapsedFolders]));
    el.classList.toggle('is-collapsed');
    el.setAttribute('aria-expanded', String(!el.classList.contains('is-collapsed')));
  }

  function applyFileFilter() {
    const q = state.fileFilter.toLowerCase();
    const cards = $$('#files .file-card');
    let shown = 0;
    for (const card of cards) {
      const match = !q || card.dataset.path.toLowerCase().includes(q) || (card.dataset.oldPath || '').toLowerCase().includes(q);
      card.classList.toggle('is-filtered-out', !match);
      if (match) shown++;
    }
    const status = $('#filter-status');
    if (q) {
      status.hidden = false;
      status.innerHTML = shown ? `${esc(shown)} of ${esc(cards.length)} files — <button type="button" class="link-btn btn-clear-filter">clear</button>`
        : 'No files match — <button type="button" class="link-btn btn-clear-filter">clear</button>';
    } else status.hidden = true;
    renderFileTree();
  }

  function currentFilePath() {
    const cards = visibleCards();
    const card = cards[state.currentFile];
    return card ? card.dataset.path : null;
  }

  function statusName(s) {
    return { A: 'added', M: 'modified', D: 'deleted', R: 'renamed', C: 'copied', T: 'type changed' }[s] || s;
  }

  /* ==================================================================== header card */

  function kindExplanation(diff) {
    const r = state.review.range;
    if (diff.kind === 'combined') return `Everything in the range ${rangeLabel()} as one diff (${shortSha(r.base) || 'empty tree'} → ${shortSha(r.head)}).`;
    if (diff.kind === 'worktree') return 'Uncommitted changes vs HEAD: staged, unstaged and untracked files.';
    if (diff.kind === 'compare') return `Compare view of ${diff.sha.slice('compare:'.length)} — read-only.`;
    return '';
  }

  function renderHeader() {
    const diff = currentDiff();
    const host = $('#commit-header');
    if (!diff) { host.innerHTML = ''; return; }
    const kindTag = diff.kind === 'combined' ? 'Range' : diff.kind === 'worktree' ? 'Worktree' : diff.kind === 'compare' ? 'Compare' : diff.is_merge ? 'Merge' : '';
    const parts = [];
    parts.push(`<div class="subject-row"><h1 class="subject">${esc(diff.subject)}</h1>${kindTag ? `<span class="kind-tag">${esc(kindTag)}</span>` : ''}</div>`);
    if (diff.body) parts.push(`<pre class="body">${esc(diff.body)}</pre>`);
    const explain = kindExplanation(diff);
    if (explain) parts.push(`<p class="explain">${esc(explain)}</p>`);
    let meta = '<div class="meta">';
    if (diff.kind === 'commit') {
      meta += `<span class="author">${avatarHtml('user', diff.author.name, true)} ${esc(diff.author.name)} <span class="email">&lt;${esc(diff.author.email)}&gt;</span></span>`;
      meta += `<span class="when">committed ${timeHtml(diff.author_date)}</span>`;
      meta += `<button type="button" class="sha-copy" data-copy="${esc(diff.sha)}" title="Copy full sha">${esc(diff.short_sha)}</button>`;
      if (diff.parents.length) {
        meta += `<span class="parents">${diff.parents.length > 1 ? 'parents' : 'parent'} ${diff.parents.map((p) => {
          const listed = findCommit(p);
          return listed ? `<a href="#${esc(p)}" class="parent-link" data-sha="${esc(p)}">${esc(shortSha(p))}</a>` : `<span class="sha-copy" title="${esc(p)}">${esc(shortSha(p))}</span>`;
        }).join(' ')}</span>`;
      }
      if (diff.shallow_boundary) meta += '<span class="tag tag-outdated">shallow boundary</span>';
    }
    meta += `<span class="stats-wrap">${esc(diff.stats.files)} file${diff.stats.files === 1 ? '' : 's'} ${statsHtml(diff.stats.additions, diff.stats.deletions)}</span>`;
    meta += '<span class="header-actions">';
    if (!commentsDisabled()) {
      // "All changes" gets only the whole-change button: a commit-level comment on the combined view would just
      // duplicate a review-level one.
      if (diff.kind !== 'combined') {
        const label = diff.kind === 'worktree' ? 'Comment on the uncommitted changes' : 'Comment on this commit';
        meta += `<button type="button" id="btn-comment-commit" class="sm-btn" aria-label="${label}">💬 ${label}</button>`;
      }
      if (diff.kind === 'combined') meta += '<button type="button" id="btn-comment-review" class="sm-btn" aria-label="Comment on the whole change" title="A review-level comment about the whole change, not tied to any commit">💬 Comment on the whole change</button>';
    }
    meta += '</span></div>';
    parts.push(meta);
    if (diff.kind === 'combined') {
      // The cover letter (PR description) and review-level threads live only in the "All changes" view.
      parts.push(coverLetterHtml());
      parts.push('<div class="thread-block" data-key-host="review"></div>');
    }
    if (!diff.files.length) parts.push('<p class="explain">This commit has no file changes.</p>');
    parts.push('<div class="thread-block" data-key-host="commit"></div>');
    preserveEditors(host, () => {
      host.innerHTML = parts.join('');
      renderReviewThreads();
      renderCommitThreads();
    });
  }

  /** The cover-letter panel of the "All changes" header: safe Markdown, or a hint when the agent has not set one. */
  function coverLetterHtml() {
    const cover = (state.review && state.review.cover) || '';
    if (!cover.trim()) {
      return '<div id="cover-letter" class="cover-letter is-empty"><span class="cover-title">Cover letter</span>'
        + '<p class="cover-hint">No cover letter — the agent can set one with <code>ccr cover</code></p></div>';
    }
    return `<div id="cover-letter" class="cover-letter"><span class="cover-title">Cover letter</span><div class="cover-body md">${renderMarkdown(cover)}</div></div>`;
  }

  /** Swap the cover-letter panel in place (called on every /api/review refetch; no-op outside the combined view). */
  function renderCoverLetter() {
    const el = $('#cover-letter');
    if (!el) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = coverLetterHtml();
    el.replaceWith(tmp.firstElementChild);
  }

  /* ==================================================================== file cards */

  function viewedKey(f) {
    return `ccr:viewed:${repoKey()}|${f.path}|${f.old_blob || 'null'}..${f.new_blob || 'null'}`;
  }
  const isViewed = (f) => state.viewed.has(viewedKey(f));

  function setViewed(f, on) {
    const key = viewedKey(f);
    if (on) { state.viewed.add(key); storage.set(key, new Date().toISOString()); } else { state.viewed.delete(key); storage.del(key); }
    state.expandedViewed.delete(f.path);
    const card = cardFor(f.path);
    if (card) {
      const box = card.querySelector('.viewed input[type=checkbox]');
      if (box) box.checked = on;
      card.querySelector('.viewed').classList.toggle('is-checked', on);
      updateCardCollapse(card, f);
    }
    updateFilesProgress();
    renderFileTree();
  }

  /** collapsed = viewed (unless explicitly re-opened) || manually collapsed. */
  const isCollapsed = (f) => (isViewed(f) && !state.expandedViewed.has(f.path)) || state.collapsedFiles.has(f.path);

  function updateCardCollapse(card, f) {
    const collapsed = isCollapsed(f);
    card.classList.toggle('is-collapsed', collapsed);
    card.classList.toggle('is-hidden-viewed', state.hideViewed && isViewed(f));
    const btn = card.querySelector('.btn-collapse');
    if (btn) btn.setAttribute('aria-expanded', String(!collapsed));
  }

  function fileForPath(path) {
    const diff = currentDiff();
    return diff ? diff.files.find((f) => f.path === path) : null;
  }
  function cardFor(path) {
    return $$('#files .file-card').find((c) => c.dataset.path === path) || null;
  }
  const visibleCards = () => $$('#files .file-card').filter((c) => !c.classList.contains('is-filtered-out') && !c.classList.contains('is-hidden-viewed'));

  function fileHeaderHtml(f) {
    const key = `${state.viewSha}|${f.path}`;
    const c = state.counts.byFile.get(key);
    const other = otherViewsCount(f.path);
    const viewed = isViewed(f);
    const collapsed = isCollapsed(f);
    const pathHtml = f.old_path && f.old_path !== f.path
      ? `<span class="old">${esc(f.old_path)}</span><span class="arrow">→</span>${esc(f.path)}`
      : esc(f.path);
    const modeNote = f.old_mode && f.new_mode && f.old_mode !== f.new_mode ? `<span class="mode-note" title="File mode changed">${esc(f.old_mode)} → ${esc(f.new_mode)}</span>`
      : (f.new_mode === '120000' || f.old_mode === '120000') ? '<span class="mode-note">symlink</span>'
        : (f.new_mode === '160000' || f.old_mode === '160000') ? '<span class="mode-note">submodule</span>' : '';
    return `<div class="file-header">
      <button type="button" class="btn-collapse" aria-label="Collapse or expand this file" aria-expanded="${!collapsed}"><span class="chev">▾</span></button>
      <span class="status-badge st-${esc(f.status)}" title="${esc(statusName(f.status))}${f.score ? ` (${esc(f.score)}% similar)` : ''}">${esc(f.status)}</span>
      <span class="file-path">${pathHtml}</span>
      <button type="button" class="hdr-btn btn-copy-path" data-copy="${esc(f.path)}" aria-label="Copy path" title="Copy path">⧉</button>
      ${modeNote}
      ${f.binary ? '<span class="mode-note">binary</span>' : statsHtml(f.additions, f.deletions)}
      ${c && c.threads ? `<span class="tcount${c.hasNew ? ' has-new' : ''}" title="Threads in this view">💬 ${esc(c.threads)}</span>` : ''}
      ${other ? `<button type="button" class="other-views" data-path="${esc(f.path)}" title="Open the drawer filtered by this path">${esc(other)} thread${other === 1 ? '' : 's'} in other views</button>` : ''}
      <span class="spacer"></span>
      <label class="viewed${viewed ? ' is-checked' : ''}"><input type="checkbox" aria-label="Mark file as viewed"${viewed ? ' checked' : ''}> Viewed</label>
      ${commentsDisabled() ? '' : `<button type="button" class="hdr-btn btn-comment-file${hasDraft(`file:${state.viewSha}|${f.path}`) ? ' has-draft' : ''}" aria-label="Comment on this file" title="Comment on this file">💬</button>`}
    </div>`;
  }

  function otherViewsCount(path) {
    let n = 0;
    for (const [key, c] of state.counts.byFile) {
      const bar = key.indexOf('|');
      if (key.slice(bar + 1) === path && key.slice(0, bar) !== state.viewSha) n += c.threads;
    }
    return n;
  }

  function fileNoteHtml(f) {
    if (f.binary) return '<div class="file-note">Binary file not shown</div>';
    if (f.too_large) return `<div class="file-note"><span>Large diff hidden (${esc(f.line_count)} lines)</span><button type="button" class="sm-btn btn-load-anyway">Load anyway</button><span class="spinner"></span></div>`;
    if (!f.hunks.length) {
      if (f.ws_only) return '<div class="file-note">Only whitespace changes (hidden by “Hide whitespace”)</div>';
      if (f.old_mode && f.new_mode && f.old_mode !== f.new_mode) return `<div class="file-note">Mode changed ${esc(f.old_mode)} → ${esc(f.new_mode)}, no content changes</div>`;
      if (f.status === 'R') return `<div class="file-note">File renamed without changes (${esc(f.score)}% similar)</div>`;
      return '<div class="file-note">No content changes</div>';
    }
    return '';
  }

  function fileCardHtml(f) {
    const note = fileNoteHtml(f);
    const collapsed = isCollapsed(f);
    const hidden = state.hideViewed && isViewed(f);
    const filtered = state.fileFilter && !f.path.toLowerCase().includes(state.fileFilter.toLowerCase());
    return `<section class="file-card${collapsed ? ' is-collapsed' : ''}${hidden ? ' is-hidden-viewed' : ''}${filtered ? ' is-filtered-out' : ''}" data-path="${esc(f.path)}"${f.old_path ? ` data-old-path="${esc(f.old_path)}"` : ''} data-rendered="${note ? '1' : '0'}" id="file-${esc(fileDomId(f.path))}">
      ${fileHeaderHtml(f)}
      <div class="thread-block" data-key-host="file"></div>
      ${note}
      <div class="diff-body${note ? '' : ' is-placeholder'}"></div>
    </section>`;
  }

  function fileDomId(path) {
    return path.replace(/[^A-Za-z0-9_-]/g, (c) => '_' + c.codePointAt(0).toString(16));
  }

  let bodyObserver = null;
  function ensureBodyObserver() {
    if (bodyObserver) return bodyObserver;
    bodyObserver = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) ensureRendered(e.target);
    }, { root: $('#main'), rootMargin: '1500px 0px' });
    return bodyObserver;
  }

  function renderFiles() {
    const diff = currentDiff();
    const host = $('#files');
    if (bodyObserver) bodyObserver.disconnect();
    if (!diff) { host.innerHTML = ''; $('#files-toolbar').hidden = true; renderFileTree(); return; }
    $('#files-toolbar').hidden = diff.files.length === 0;
    host.innerHTML = diff.files.map(fileCardHtml).join('');
    const rowH = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--row-h')) || 20;
    const io = ensureBodyObserver();
    const cards = $$('#files .file-card');
    for (let i = 0; i < cards.length; i++) {
      const card = cards[i];
      const f = diff.files[i];
      if (card.dataset.rendered === '0') {
        card.querySelector('.diff-body').style.minHeight = Math.min(2000, (f.line_count + f.hunk_count) * rowH) + 'px';
        io.observe(card);
      }
      renderFileThreads(card, f);
    }
    updateFilesProgress();
    applyFileFilter();
  }

  function updateFilesProgress() {
    const diff = currentDiff();
    if (!diff) return;
    const total = diff.files.length;
    const viewed = diff.files.filter(isViewed).length;
    $('#files-progress').textContent = `${viewed} / ${total} files viewed`;
    $('#progress-fill').style.width = (total ? Math.round((viewed / total) * 100) : 0) + '%';
  }

  /** Render one file's diff body synchronously (single innerHTML), once. */
  function ensureRendered(card) {
    if (!card || card.dataset.rendered === '1') return;
    const f = fileForPath(card.dataset.path);
    if (!f) return;
    const body = card.querySelector('.diff-body');
    const sha = state.viewSha;
    body.innerHTML = buildDiffHtml(sha, f);
    body.classList.remove('is-placeholder');
    body.style.minHeight = '';
    card.dataset.rendered = '1';
    if (bodyObserver) bodyObserver.unobserve(card);
    attachGutterButton(card);
    observeThreads(card);
    restoreSelectionClasses(card);
  }

  function rerenderBody(card) {
    const f = fileForPath(card.dataset.path);
    if (!f || card.dataset.rendered !== '1') return;
    const body = card.querySelector('.diff-body');
    preserveEditors(body, () => { body.innerHTML = buildDiffHtml(state.viewSha, f); });
    attachGutterButton(card);
    observeThreads(card);
    restoreSelectionClasses(card);
  }

  async function loadTooLarge(card, btn) {
    const f = fileForPath(card.dataset.path);
    const note = card.querySelector('.file-note');
    if (!f || !note || btn.disabled) return;
    btn.disabled = true;
    note.classList.add('is-loading');
    try {
      const full = await api(`/api/commits/${encodeURIComponent(state.viewSha)}/file?path=${encodeURIComponent(f.path)}${wsQuery('&')}`);
      const diff = currentDiff();
      const idx = diff.files.indexOf(f);
      if (idx >= 0) diff.files[idx] = Object.assign({}, f, full, { too_large: false, reason: null });
      note.remove();
      card.dataset.rendered = '0';
      card.querySelector('.diff-body').classList.add('is-placeholder');
      ensureRendered(card);
      recomputeOrphans();
      renderFileThreads(card, diff.files[idx]);
    } catch (e) {
      btn.disabled = false;
      note.classList.remove('is-loading');
      toast(`Could not load the file: ${e.message}`, 'error');
    }
  }

  const wsQuery = (sep = '?') => (state.wsIgnore ? `${sep}ws=ignore` : '');

  /* ==================================================================== 3. diff model */

  /** Exclusive end / first line of a hunk on one side, honouring the "count 0 → start is the line before" convention. */
  const hStart = (h, side) => (h[side + '_count'] === 0 ? h[side + '_start'] + 1 : h[side + '_start']);
  const hEnd = (h, side) => (h[side + '_count'] === 0 ? h[side + '_start'] + 1 : h[side + '_start'] + h[side + '_count']);

  function hunkHeader(h) {
    return `@@ -${h.old_start},${h.old_count} +${h.new_start},${h.new_count} @@`;
  }

  /** Group hunk lines into ctx blocks and change blocks (dels then adds). */
  function buildBlocks(lines) {
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      if (lines[i].t === 'ctx') {
        const block = { type: 'ctx', lines: [] };
        while (i < lines.length && lines[i].t === 'ctx') block.lines.push(lines[i++]);
        blocks.push(block);
      } else {
        const block = { type: 'change', dels: [], adds: [] };
        while (i < lines.length && lines[i].t === 'del') block.dels.push(lines[i++]);
        while (i < lines.length && lines[i].t === 'add') block.adds.push(lines[i++]);
        blocks.push(block);
      }
    }
    return blocks;
  }

  /** Which side (and rev) is used to fetch context for expansion; null when disabled. */
  function expansionSource(f) {
    if (f.binary || f.too_large) return null;
    if (f.status === 'D') {
      if (!f.old_rev || f.old_mode === '160000') return null;
      return { side: 'old', rev: f.old_rev, path: f.old_path || f.path };
    }
    if (!f.new_rev || f.new_mode === '160000') return null;
    return { side: 'new', rev: f.new_rev, path: f.path };
  }

  function gapInfo(f, gapIndex, ft) {
    const hunks = f.hunks;
    const S = ft ? ft.side : (f.status === 'D' ? 'old' : 'new');
    const upper = gapIndex > 0 ? hunks[gapIndex - 1] : null;
    const lower = gapIndex < hunks.length ? hunks[gapIndex] : null;
    const from = upper ? hEnd(upper, S) : 1;
    let to;
    if (lower) to = hStart(lower, S);
    else if (ft) to = ft.count + 1;
    else to = null; // unknown until the file is fetched
    return { S, upper, lower, from, to, size: to == null ? null : Math.max(0, to - from) };
  }

  function expandButtonsHtml(f, gapIndex, source) {
    if (!source) return '';
    const ft = state.fileText.get(`${state.viewSha}|${f.path}`);
    const g = gapInfo(f, gapIndex, ft);
    if (g.size === 0) return '';
    const btn = (cls, label, title) => `<button type="button" class="expand-btn ${cls}" data-gap="${gapIndex}" title="${esc(title)}">${label}</button>`;
    if (g.size != null && g.size <= EXPAND_STEP) return btn('btn-expand-all', `expand ${g.size} line${g.size === 1 ? '' : 's'}`, 'Show the hidden lines');
    let html = '';
    if (g.lower) html += btn('btn-expand-up', `⤒ ${EXPAND_STEP}`, `Show ${EXPAND_STEP} lines above`);
    if (g.upper) html += btn('btn-expand-down', `⤓ ${EXPAND_STEP}`, `Show ${EXPAND_STEP} lines below`);
    html += btn('btn-expand-all', g.lower ? 'expand all' : 'expand to end', g.lower ? 'Show all hidden lines' : 'Show the rest of the file');
    return html;
  }

  function hunkRowHtml(f, gapIndex, source) {
    const lower = gapIndex < f.hunks.length ? f.hunks[gapIndex] : null;
    const buttons = expandButtonsHtml(f, gapIndex, source);
    if (!lower && !buttons) return '';
    if (!lower && gapIndex === 0) return '';
    const g = gapInfo(f, gapIndex, state.fileText.get(`${state.viewSha}|${f.path}`));
    if (gapIndex === 0 && g.size === 0) return '';
    const head = lower ? `<span class="hunk-head">${esc(hunkHeader(lower))}${lower.section ? `<span class="section">${esc(lower.section)}</span>` : ''}</span>` : '<span class="hunk-head"></span>';
    return `<tr class="hunk" data-gap="${gapIndex}"><td colspan="4"><div class="hunk-inner">${buttons}${head}</div></td></tr>`;
  }

  /* ---- highlighting */

  const ENT = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': "'", '&#39;': "'" };
  const decodeEntities = (s) => s.replace(/&(?:amp|lt|gt|quot|#x27|#39);/g, (m) => ENT[m]);

  const highlighter = {
    plain(lines) { return lines.map((l) => [['', l]]); },
    /** tokenize(sideText, lang) → Array<Array<[classes, text]>> — one token list per line. */
    tokenize(sideText, lang) {
      const lines = sideText.split('\n');
      if (typeof window.hljs === 'undefined' || !lang || !window.hljs.getLanguage(lang) || sideText.length > 500000) return this.plain(lines);
      const longIdx = [];
      const work = lines.map((l, i) => { if (l.length > 1000) { longIdx.push(i); return ''; } return l; });
      let html;
      try { html = window.hljs.highlight(work.join('\n'), { language: lang, ignoreIllegals: true }).value; } catch (e) { return this.plain(lines); }
      const out = [[]];
      const stack = [];
      const re = /<span class="([^"]*)">|<\/span>|\n|[^<\n]+/g;
      let m;
      while ((m = re.exec(html)) !== null) {
        if (m[1] !== undefined) stack.push(m[1]);
        else if (m[0] === '</span>') stack.pop();
        else if (m[0] === '\n') out.push([]);
        else out[out.length - 1].push([stack.join(' '), decodeEntities(m[0])]);
      }
      if (out.length !== lines.length) return this.plain(lines);
      for (const i of longIdx) out[i] = [['', lines[i]]];
      return out;
    },
  };

  function sideTexts(f) {
    const oldL = []; const newL = [];
    for (const h of f.hunks) for (const l of h.lines) { if (l.t !== 'add') oldL.push(l.s); if (l.t !== 'del') newL.push(l.s); }
    return { old: oldL, new: newL };
  }

  function tokensFor(sha, f) {
    const texts = sideTexts(f);
    const get = (side) => {
      const key = `${sha}|${f.path}|${side}`;
      let t = state.hl.get(key);
      if (!t || t.length !== texts[side].length) { t = highlighter.tokenize(texts[side].join('\n'), f.lang); state.hl.set(key, t); }
      return t;
    };
    return { old: get('old'), new: get('new') };
  }

  /* ---- word diff */

  function wordTokens(s) { return s.match(/\w+|\s+|[^\w\s]/gu) || []; }

  /** Character ranges of tokens not in the token LCS of a and b; null when > 50 % changed on either side. */
  function wordDiff(a, b) {
    if (a.length > 400 || b.length > 400 || a === b) return null;
    const ta = wordTokens(a); const tb = wordTokens(b);
    const n = ta.length; const m = tb.length;
    if (!n || !m) return null;
    const dp = new Uint16Array((n + 1) * (m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) {
      dp[i * (m + 1) + j] = ta[i] === tb[j] ? dp[(i + 1) * (m + 1) + j + 1] + 1 : Math.max(dp[(i + 1) * (m + 1) + j], dp[i * (m + 1) + j + 1]);
    }
    const inA = new Array(n).fill(false); const inB = new Array(m).fill(false);
    let i = 0; let j = 0;
    while (i < n && j < m) {
      if (ta[i] === tb[j]) { inA[i] = true; inB[j] = true; i++; j++; }
      else if (dp[(i + 1) * (m + 1) + j] >= dp[i * (m + 1) + j + 1]) i++;
      else j++;
    }
    const ranges = (toks, inLcs) => {
      const out = []; let pos = 0; let changed = 0;
      for (let k = 0; k < toks.length; k++) {
        const len = toks[k].length;
        if (!inLcs[k]) {
          changed++;
          if (out.length && out[out.length - 1][1] === pos) out[out.length - 1][1] = pos + len; else out.push([pos, pos + len]);
        }
        pos += len;
      }
      return { out, changed };
    };
    const ra = ranges(ta, inA); const rb = ranges(tb, inB);
    if (ra.changed * 2 > n || rb.changed * 2 > m) return null;
    return { a: ra.out, b: rb.out };
  }

  /** Render token segments as HTML, wrapping the changed character ranges in span.wd. */
  function renderCode(tokens, ranges, line) {
    let html = '';
    let pos = 0;
    let ri = 0;
    const rs = ranges || [];
    for (const [cls, text] of tokens) {
      let start = 0;
      while (start < text.length) {
        while (ri < rs.length && rs[ri][1] <= pos + start) ri++;
        const absPos = pos + start;
        let end = text.length;
        let marked = false;
        if (ri < rs.length) {
          const [rs0, rs1] = rs[ri];
          if (rs0 <= absPos) { marked = true; end = Math.min(text.length, rs1 - pos); }
          else end = Math.min(text.length, rs0 - pos);
        }
        const piece = esc(text.slice(start, end));
        const span = cls ? `<span class="${esc(cls)}">${piece}</span>` : piece;
        html += marked ? `<span class="wd">${span}</span>` : span;
        start = end;
      }
      pos += text.length;
    }
    if (line.trunc) html += '<span class="trunc"> … [truncated]</span>';
    if (line.cr) html += '<span class="cr" title="Line ends with CRLF">␍</span>';
    return html;
  }

  /* ---- row builders */

  function threadsAttachedHtml(sha, f, line) {
    let html = '';
    if (line.o != null) html += threadRowHtml(lineKey(sha, f.path, 'old', line.o));
    if (line.n != null) html += threadRowHtml(lineKey(sha, f.path, 'new', line.n));
    return html;
  }

  function threadRowHtml(key) {
    const ids = state.threadsByKey.get(key);
    let html = '';
    if (ids && ids.length) html += `<tr class="threads" data-key="${esc(key)}"><td colspan="4">${ids.map(threadHtml).join('')}</td></tr>`;
    if (state.openEditors.has(key)) html += `<tr class="editor" data-key="${esc(key)}"><td colspan="4">${editorHtml(key)}</td></tr>`;
    return html;
  }

  function numCell(side, line, extraCls = '') {
    if (line == null) return `<td class="num empty ${extraCls}"></td>`;
    return `<td class="num ${side} ${extraCls}" data-side="${side}" data-line="${line}">${line}</td>`;
  }

  function rowAttrs(line) {
    return `data-o="${line.o == null ? '' : line.o}" data-n="${line.n == null ? '' : line.n}"${line.x ? ' data-x="1"' : ''}`;
  }

  function unifiedRow(line, tokens, ranges) {
    const marker = line.t === 'add' ? '+' : line.t === 'del' ? '−' : '';
    return `<tr class="line ${line.t}" ${rowAttrs(line)}>${numCell('old', line.o)}${numCell('new', line.n)}<td class="marker">${marker}</td><td class="code">${renderCode(tokens, ranges, line)}</td></tr>`;
  }

  function splitRow(left, right, tl, tr, rl, rr) {
    const cls = [left && right ? (left === right ? 'ctx' : 'del add') : left ? 'del' : 'add'];
    const attrs = `data-o="${left ? left.o : ''}" data-n="${right ? right.n : ''}"${(left && left.x) || (right && right.x) ? ' data-x="1"' : ''}`;
    const lcls = left && left.t === 'del' ? 'del' : '';
    const rcls = right && right.t === 'add' ? 'add' : '';
    const lcode = left ? `<td class="code old ${lcls}">${renderCode(tl, rl, left)}</td>` : '<td class="code empty"></td>';
    const rcode = right ? `<td class="code new ${rcls}">${renderCode(tr, rr, right)}</td>` : '<td class="code empty"></td>';
    return `<tr class="line ${cls}" ${attrs}>${numCell('old', left ? left.o : null, lcls)}${lcode}${numCell('new', right ? right.n : null, rcls)}${rcode}</tr>`;
  }

  /** Build the whole diff table for a file as one HTML string. */
  function buildDiffHtml(sha, f) {
    const view = state.viewMode;
    const tok = tokensFor(sha, f);
    const source = expansionSource(f);
    let oi = 0; let ni = 0;
    const cols = view === 'split' ? '<col class="c-num"><col class="c-code"><col class="c-num"><col class="c-code">' : '<col class="c-num"><col class="c-num"><col class="c-marker"><col class="c-code">';
    const parts = [`<table class="diff" data-view="${view}"><colgroup>${cols}</colgroup><tbody>`];
    for (let hi = 0; hi <= f.hunks.length; hi++) {
      parts.push(hunkRowHtml(f, hi, source));
      if (hi === f.hunks.length) break;
      const h = f.hunks[hi];
      for (const block of buildBlocks(h.lines)) {
        if (block.type === 'ctx') {
          for (const line of block.lines) {
            const t = tok.new[ni] || [['', line.s]];
            oi++; ni++;
            parts.push(view === 'split' ? splitRow(line, line, tok.old[oi - 1] || t, t, null, null) : unifiedRow(line, t, null));
            parts.push(threadsAttachedHtml(sha, f, line));
          }
          continue;
        }
        const { dels, adds } = block;
        const pairs = Math.min(dels.length, adds.length);
        const wd = [];
        for (let i = 0; i < pairs; i++) wd.push(wordDiff(dels[i].s, adds[i].s));
        const delTok = dels.map((l) => tok.old[oi++] || [['', l.s]]);
        const addTok = adds.map((l) => tok.new[ni++] || [['', l.s]]);
        if (view === 'unified') {
          dels.forEach((l, i) => { parts.push(unifiedRow(l, delTok[i], wd[i] ? wd[i].a : null)); parts.push(threadsAttachedHtml(sha, f, l)); });
          adds.forEach((l, i) => { parts.push(unifiedRow(l, addTok[i], wd[i] ? wd[i].b : null)); parts.push(threadsAttachedHtml(sha, f, l)); });
        } else {
          const rows = Math.max(dels.length, adds.length);
          for (let i = 0; i < rows; i++) {
            const l = dels[i] || null; const r = adds[i] || null;
            parts.push(splitRow(l, r, l ? delTok[i] : null, r ? addTok[i] : null, wd[i] ? wd[i].a : null, wd[i] ? wd[i].b : null));
            if (l) parts.push(threadRowHtml(lineKey(sha, f.path, 'old', l.o)));
            if (r) parts.push(threadRowHtml(lineKey(sha, f.path, 'new', r.n)));
          }
        }
      }
    }
    parts.push('</tbody></table>');
    return parts.join('');
  }

  /* ---- expansion */

  async function ensureFileText(f) {
    const key = `${state.viewSha}|${f.path}`;
    if (state.fileText.has(key)) return state.fileText.get(key);
    const source = expansionSource(f);
    if (!source) throw new ApiError(400, 'context is not available for this file');
    const data = await api(`/api/file?rev=${encodeURIComponent(source.rev)}&path=${encodeURIComponent(source.path)}`);
    const lines = data.content.split('\n');
    if (lines.length && lines[lines.length - 1] === '' && data.content.endsWith('\n')) lines.pop();
    const ft = { side: source.side, lines: lines.map((l) => l.replace(/\r$/, '')), count: data.lines || lines.length };
    state.fileText.set(key, ft);
    return ft;
  }

  /** Splice up to `n` context lines (null = the whole gap) into the gap before hunk `gapIndex`. */
  function spliceGap(f, ft, gapIndex, dir, n) {
    const g = gapInfo(f, gapIndex, ft);
    if (!g.size) return false;
    const S = g.S; const O = S === 'new' ? 'old' : 'new';
    const take = n == null ? g.size : Math.min(n, g.size);
    const mk = (lineNo, otherNo) => ({ t: 'ctx', o: S === 'old' ? lineNo : otherNo, n: S === 'new' ? lineNo : otherNo, s: ft.lines[lineNo - 1] == null ? '' : ft.lines[lineNo - 1], x: true });
    if (dir === 'down' || (dir === 'all' && g.upper)) {
      const up = g.upper;
      const firstS = hStart(up, S); const firstO = hStart(up, O);
      const startS = hEnd(up, S); const startO = hEnd(up, O);
      const rows = [];
      for (let k = 0; k < take; k++) rows.push(mk(startS + k, startO + k));
      up.lines.push(...rows);
      up[S + '_count'] += take; up[S + '_start'] = firstS;
      up[O + '_count'] += take; up[O + '_start'] = firstO;
    } else {
      const low = g.lower;
      const startS = hStart(low, S); const startO = hStart(low, O);
      const rows = [];
      for (let k = take; k >= 1; k--) rows.push(mk(startS - k, startO - k));
      low.lines.unshift(...rows);
      low[S + '_start'] = startS - take; low[S + '_count'] += take;
      low[O + '_start'] = startO - take; low[O + '_count'] += take;
    }
    // merge when the gap is closed
    if (g.upper && g.lower && hEnd(g.upper, S) >= hStart(g.lower, S)) {
      const up = g.upper; const low = g.lower;
      up.lines.push(...low.lines);
      for (const side of ['old', 'new']) {
        const start = hStart(up, side);
        up[side + '_count'] = hEnd(low, side) - start;
        up[side + '_start'] = start;
      }
      f.hunks.splice(gapIndex, 1);
    }
    return true;
  }

  async function expandGap(card, btn) {
    const f = fileForPath(card.dataset.path);
    if (!f || btn.disabled) return;
    const gapIndex = parseInt(btn.dataset.gap, 10);
    const dir = btn.classList.contains('btn-expand-up') ? 'up' : btn.classList.contains('btn-expand-down') ? 'down' : 'all';
    const main = $('#main');
    const before = btn.getBoundingClientRect().top;
    const hunksBefore = f.hunks.length;
    btn.disabled = true;
    try {
      const ft = await ensureFileText(f);
      spliceGap(f, ft, gapIndex, dir, dir === 'all' ? null : EXPAND_STEP);
      invalidateHl(f);
      rerenderBody(card);
      recomputeOrphans();
      // Keep the clicked hunk row where it was on screen while it still exists (partial expansion).
      const row = card.querySelector(`tr.hunk[data-gap="${gapIndex}"]`);
      if (row && dir !== 'all' && f.hunks.length === hunksBefore) main.scrollTop += row.getBoundingClientRect().top - before;
      renderFileThreads(card, f);
    } catch (e) {
      btn.disabled = false;
      toast(`Could not expand: ${e.message}`, 'error');
    }
  }

  function invalidateHl(f) {
    state.hl.delete(`${state.viewSha}|${f.path}|old`);
    state.hl.delete(`${state.viewSha}|${f.path}|new`);
  }

  /** Does a line on `side` exist in the (possibly expanded) hunks of f? */
  function lineExists(f, side, line) {
    const k = side === 'new' ? 'n' : 'o';
    for (const h of f.hunks) for (const l of h.lines) if (l[k] === line) return true;
    return false;
  }

  /** Expand whichever gap contains `line` on `side` so that the row becomes renderable. */
  async function expandToInclude(card, f, side, line) {
    const source = expansionSource(f);
    if (!source || source.side !== side) return false;
    const ft = await ensureFileText(f);
    if (line < 1 || line > ft.count) return false;
    for (let gi = 0; gi <= f.hunks.length; gi++) {
      const g = gapInfo(f, gi, ft);
      if (g.size && line >= g.from && line < g.to) {
        spliceGap(f, ft, gi, 'all', null);
        invalidateHl(f);
        rerenderBody(card);
        recomputeOrphans();
        renderFileThreads(card, f);
        return true;
      }
    }
    return false;
  }

  /* ==================================================================== 4. gutter [+] and selection */

  function attachGutterButton(card) {
    const body = card.querySelector('.diff-body');
    const old = card.querySelector('.btn-add-comment');
    if (old) old.remove();
    if (commentsDisabled() || !body.querySelector('table.diff')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-add-comment is-parked';
    btn.setAttribute('aria-label', 'Add comment');
    btn.textContent = '+';
    body.prepend(btn);
  }

  /** Move a card's shared [+] into the number cell for (row, side). */
  function placeGutterButton(row, side, show) {
    const card = row.closest('.file-card');
    const btn = card && card.querySelector('.btn-add-comment');
    if (!btn) return;
    const cell = row.querySelector(`td.num.${side}[data-line]`);
    if (!cell) return;
    const line = cell.dataset.line;
    btn.dataset.side = side;
    btn.dataset.line = line;
    btn.classList.remove('is-parked');
    btn.classList.toggle('is-visible', Boolean(show));
    btn.classList.toggle('has-draft', hasDraft(lineKey(state.viewSha, card.dataset.path, side, +line)));
    if (btn.parentElement !== cell) cell.appendChild(btn);
  }

  function gutterSideFor(row, td) {
    const table = row.closest('table.diff');
    if (table.dataset.view === 'split') {
      if (!td || td.classList.contains('empty')) return null;
      return td.classList.contains('old') ? 'old' : 'new';
    }
    return row.classList.contains('del') ? 'old' : 'new';
  }

  /** Put the shared [+] back on the selected row once the pointer leaves the diff rows. */
  function reparkGutter() {
    const s = state.sel;
    if (!s || s.dragging || s.sha !== state.viewSha) return;
    const card = cardFor(s.path);
    const row = card && findRow(card, s.side, s.endLine);
    if (row) placeGutterButton(row, s.side, true);
  }

  function onGutterHover(e) {
    const row = e.target.closest('tr.line');
    if (commentsDisabled()) return;
    if (!row) { if (!e.target.closest('.btn-add-comment')) reparkGutter(); return; }
    const td = e.target.closest('td');
    const side = gutterSideFor(row, td);
    if (!side) return;
    if (state.sel && !state.sel.dragging && row.classList.contains('is-selected')) return;
    placeGutterButton(row, side, false);
  }

  const rowLine = (row, side) => { const v = row.dataset[side === 'new' ? 'n' : 'o']; return v === '' || v == null ? null : parseInt(v, 10); };

  function applySelectionClasses(scope) {
    const root = scope || document;
    for (const r of $$('tr.line.is-selected, tr.line.in-range', root)) r.classList.remove('is-selected', 'in-range');
    const sel = state.sel;
    if (!sel || sel.sha !== state.viewSha) return;
    const card = cardFor(sel.path);
    if (!card) return;
    const lo = Math.min(sel.startLine, sel.endLine); const hi = Math.max(sel.startLine, sel.endLine);
    let endRow = null;
    for (const row of $$('tr.line', card)) {
      const ln = rowLine(row, sel.side);
      if (ln == null || ln < lo || ln > hi) continue;
      row.classList.add('in-range');
      if (ln === sel.endLine) endRow = row;
    }
    if (endRow) {
      if (lo === hi) endRow.classList.add('is-selected');
      if (!sel.dragging) placeGutterButton(endRow, sel.side, true);
    }
  }

  function restoreSelectionClasses(card) {
    if (state.sel && state.sel.path === card.dataset.path) applySelectionClasses(card);
  }

  function clearSelection({ keepHash = false } = {}) {
    if (!state.sel) return;
    const table = state.sel.table;
    state.sel = null;
    if (table) table.classList.remove('selecting');
    for (const r of $$('#main tr.line.is-selected, #main tr.line.in-range')) r.classList.remove('is-selected', 'in-range');
    for (const b of $$('#main .btn-add-comment')) b.classList.remove('is-visible');
    if (!keepHash && !state.compare) writeHash({ sha: state.selectedSha, path: currentFilePath() }, true);
  }

  function selectionAnchor() {
    const s = state.sel;
    if (!s) return null;
    const lo = Math.min(s.startLine, s.endLine); const hi = Math.max(s.startLine, s.endLine);
    return { kind: 'line', commit: s.sha, path: s.path, side: s.side, line: hi, start_line: lo < hi ? lo : null };
  }

  let autoScrollTimer = null;
  let lastPointer = { x: 0, y: 0 };
  function autoScrollTick() {
    autoScrollTimer = null;
    if (!state.sel || !state.sel.dragging) return;
    const main = $('#main');
    const r = main.getBoundingClientRect();
    let dy = 0;
    if (lastPointer.y < r.top + 40) dy = -12; else if (lastPointer.y > r.bottom - 40) dy = 12;
    if (dy) { main.scrollTop += dy; extendSelectionToPoint(lastPointer.x, lastPointer.y); }
    autoScrollTimer = requestAnimationFrame(autoScrollTick);
  }

  function extendSelectionToPoint(x, y) {
    const el = document.elementFromPoint(x, y);
    const row = el && el.closest ? el.closest('tr.line') : null;
    if (!row || row.closest('table.diff') !== state.sel.table) return;
    const ln = rowLine(row, state.sel.side);
    if (ln == null || ln === state.sel.endLine) return;
    state.sel.endLine = ln;
    applySelectionClasses(row.closest('.file-card'));
  }

  function onPointerDown(e) {
    if (e.button !== 0) return;
    const table = e.target.closest('table.diff');
    if (!table) return;
    const numCell = e.target.closest('td.num[data-line]');
    if (numCell && !commentsDisabled() && !e.target.closest('.btn-add-comment')) {
      e.preventDefault();
      const card = numCell.closest('.file-card');
      const side = numCell.dataset.side;
      const line = parseInt(numCell.dataset.line, 10);
      if (e.shiftKey && state.sel && state.sel.path === card.dataset.path && state.sel.side === side && state.sel.sha === state.viewSha) {
        state.sel.endLine = line;
        state.sel.table = table;
        finishSelection();
        return;
      }
      state.sel = { sha: state.viewSha, path: card.dataset.path, side, startLine: line, endLine: line, dragging: true, table, pointerId: e.pointerId };
      table.classList.add('selecting');
      try { numCell.setPointerCapture(e.pointerId); } catch (err) { /* unsupported */ }
      applySelectionClasses(card);
      return;
    }
    const codeCell = e.target.closest('td.code.old, td.code.new');
    if (codeCell && table.dataset.view === 'split') table.classList.add(codeCell.classList.contains('old') ? 'sel-old' : 'sel-new');
  }

  function onPointerMove(e) {
    if (!state.sel || !state.sel.dragging) return;
    lastPointer = { x: e.clientX, y: e.clientY };
    extendSelectionToPoint(e.clientX, e.clientY);
    const r = $('#main').getBoundingClientRect();
    const near = e.clientY < r.top + 40 || e.clientY > r.bottom - 40;
    if (near && autoScrollTimer == null) autoScrollTimer = requestAnimationFrame(autoScrollTick);
    if (!near && autoScrollTimer != null) { cancelAnimationFrame(autoScrollTimer); autoScrollTimer = null; }
  }

  function onPointerUp(e) {
    for (const t of $$('table.diff.sel-old, table.diff.sel-new')) t.classList.remove('sel-old', 'sel-new');
    if (!state.sel || !state.sel.dragging) return;
    if (e && e.clientX != null) extendSelectionToPoint(e.clientX, e.clientY);
    finishSelection();
  }

  function finishSelection() {
    const s = state.sel;
    if (!s) return;
    if (autoScrollTimer != null) { cancelAnimationFrame(autoScrollTimer); autoScrollTimer = null; }
    s.dragging = false;
    if (s.table) s.table.classList.remove('selecting');
    const lo = Math.min(s.startLine, s.endLine); const hi = Math.max(s.startLine, s.endLine);
    s.startLine = lo; s.endLine = hi;
    applySelectionClasses(cardFor(s.path));
    writeHash({ sha: state.selectedSha, path: s.path, side: s.side, line: lo, endLine: hi > lo ? hi : null }, true);
  }

  /** Anchor for a click on the gutter [+]: the selection range when it ends on this row, else the row itself. */
  function anchorForGutter(btn) {
    const card = btn.closest('.file-card');
    const side = btn.dataset.side; const line = parseInt(btn.dataset.line, 10);
    const s = state.sel;
    if (s && s.path === card.dataset.path && s.side === side && s.endLine === line && s.sha === state.viewSha) return selectionAnchor();
    return { kind: 'line', commit: state.viewSha, path: card.dataset.path, side, line, start_line: null };
  }

  /* ==================================================================== 5. comments */

  const lineKey = (sha, path, side, line) => `line:${sha}|${path}|${side}|${line}`;

  function anchorKey(a) {
    if (!a) return 'review:';
    if (a.kind === 'line') return lineKey(a.commit, a.path, a.side, a.line);
    if (a.kind === 'file') return `file:${a.commit}|${a.path}`;
    if (a.kind === 'commit') return `commit:${a.commit}`;
    return 'review:';
  }

  function parseLineKey(key) {
    if (!key.startsWith('line:')) return null;
    const rest = key.slice(5);
    const parts = rest.split('|');
    if (parts.length < 4) return null;
    const line = parseInt(parts.pop(), 10); const side = parts.pop(); const commit = parts.shift();
    return { commit, path: parts.join('|'), side, line };
  }

  const rootOf = (c) => (c.parent_id ? state.comments.get(c.parent_id) || c : c);
  function threadMembers(rootId) {
    const out = [];
    for (const c of state.comments.values()) if (c.id === rootId || c.parent_id === rootId) out.push(c);
    out.sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id < b.id ? -1 : 1));
    return out;
  }

  function reindex() {
    const byKey = new Map();
    const roots = [...state.comments.values()].filter((c) => !c.parent_id);
    roots.sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0));
    for (const r of roots) {
      const key = anchorKey(r.anchor);
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(r.id);
    }
    state.threadsByKey = byKey;
    state.threadOrder = roots.map((r) => r.id);
  }

  function signatures() {
    const sig = new Map();
    for (const [key, ids] of state.threadsByKey) {
      sig.set(key, ids.map((id) => threadMembers(id).map((c) => `${c.id}:${c.updated_at}:${c.resolved ? 1 : 0}:${c.state}:${c.round}`).join(',')).join(';'));
    }
    return sig;
  }

  /** "New" marks comments the user has not seen yet; the user's own comments never count. */
  const isUnseen = (c) => c.author !== 'user' && c.created_at > state.seenUntil;

  function deriveCounts() {
    const byCommit = new Map(); const byFile = new Map();
    let pendingComments = 0; let unresolvedThreads = 0;
    for (const c of state.comments.values()) if (c.state === 'pending') pendingComments++;
    const bump = (map, key, m) => {
      const e = map.get(key) || { threads: 0, pending: 0, unresolved: 0, hasNew: false };
      e.threads++; if (m.pending) e.pending++; if (m.unresolved) e.unresolved++; if (m.hasNew) e.hasNew = true;
      map.set(key, e);
    };
    for (const id of state.threadOrder) {
      const root = state.comments.get(id);
      if (!root || root.outdated) continue;
      const members = threadMembers(id);
      const m = { pending: members.some((c) => c.state === 'pending'), unresolved: !root.resolved, hasNew: members.some(isUnseen) };
      if (m.unresolved) unresolvedThreads++;
      const a = root.anchor;
      const sha = anchorView(a);
      if (sha) {
        bump(byCommit, sha, m);
        if (a.path) bump(byFile, `${sha}|${a.path}`, m);
      }
    }
    state.counts = { byCommit, byFile, pendingComments, unresolvedThreads };
  }

  function recomputeOrphans() {
    const orphans = new Set();
    for (const id of state.threadOrder) {
      const root = state.comments.get(id);
      const a = root && root.anchor;
      if (!a || root.outdated || !a.commit || (a.kind !== 'line' && a.kind !== 'file')) continue;
      const diff = state.diffs.get(a.commit);
      if (!diff) continue;
      const f = diff.files.find((x) => x.path === a.path || x.old_path === a.path);
      if (!f) { orphans.add(id); continue; }
      if (a.kind === 'line' && (f.binary || f.too_large || !lineExists(f, a.side, a.line))) orphans.add(id);
    }
    state.orphans = orphans;
  }

  /** Reconcile a fresh comment list into state, re-rendering only threads whose signature changed. */
  function applyComments(list, { initial = false } = {}) {
    const before = signatures();
    const prevIds = state.knownIds;
    state.comments = new Map(list.map((c) => [c.id, c]));
    reindex();
    const after = signatures();
    const changed = new Set();
    for (const [k, v] of after) if (before.get(k) !== v) changed.add(k);
    for (const k of before.keys()) if (!after.has(k)) changed.add(k);
    afterCommentsChanged();
    for (const k of changed) patchKey(k);
    if (!initial) {
      const fresh = list.filter((c) => c.author === 'claude' && !prevIds.has(c.id));
      if (fresh.length) {
        const threads = new Set(fresh.map((c) => c.parent_id || c.id));
        const resolved = [...threads].filter((id) => { const r = state.comments.get(id); return r && r.resolved; }).length;
        toast(`Claude replied to ${threads.size} thread${threads.size === 1 ? '' : 's'}${resolved ? ` (${resolved} resolved)` : ''}`, 'info',
          { action: { label: 'Show', fn: () => openDrawer('new') }, timeout: 10000 });
      }
    }
    state.knownIds = new Set(list.map((c) => c.id));
  }

  function afterCommentsChanged() {
    deriveCounts();
    recomputeOrphans();
    updateBadges();
    updateCommitBadges();
    renderFileTree();
    updateFileHeaders();
    renderDrawer();
  }

  function upsertComment(c) {
    state.comments.set(c.id, c);
    state.knownIds.add(c.id);
    reindex();
    afterCommentsChanged();
    patchKey(anchorKey(rootOf(c).anchor));
  }

  function removeComments(ids) {
    const keys = new Set();
    for (const id of ids) {
      const c = state.comments.get(id);
      if (c) { keys.add(anchorKey(rootOf(c).anchor)); state.comments.delete(id); }
    }
    reindex();
    afterCommentsChanged();
    for (const k of keys) patchKey(k);
  }

  function updateFileHeaders() {
    const diff = currentDiff();
    if (!diff) return;
    for (const card of $$('#files .file-card')) {
      const f = diff.files.find((x) => x.path === card.dataset.path);
      if (!f) continue;
      const hdr = card.querySelector('.file-header');
      const tmp = document.createElement('div');
      tmp.innerHTML = fileHeaderHtml(f);
      hdr.replaceWith(tmp.firstElementChild);
    }
  }

  /* ---- patching threads into the DOM */

  function keyHostRow(key) {
    const p = parseLineKey(key);
    if (!p || p.commit !== state.viewSha) return null;
    const card = cardFor(p.path);
    if (!card || card.dataset.rendered !== '1') return null;
    const attr = p.side === 'new' ? 'n' : 'o';
    return card.querySelector(`tr.line[data-${attr}="${p.line}"]`);
  }

  function attachmentRank(tr) {
    const key = tr.dataset.key || '';
    const side = key.split('|')[2];
    return (side === 'old' ? 0 : 2) + (tr.classList.contains('editor') ? 1 : 0);
  }

  function insertAttachment(row, tr) {
    let ref = row; let next = row.nextElementSibling;
    while (next && (next.classList.contains('threads') || next.classList.contains('editor')) && attachmentRank(next) <= attachmentRank(tr)) {
      ref = next; next = next.nextElementSibling;
    }
    ref.after(tr);
  }

  function patchKey(key) {
    if (key.startsWith('line:')) {
      const row = keyHostRow(key);
      if (!row) return;
      const table = row.closest('table');
      preserveEditors(table, () => {
        for (const tr of $$(`tr.threads[data-key="${cssEsc(key)}"], tr.editor[data-key="${cssEsc(key)}"]`, table)) tr.remove();
        const tmp = document.createElement('table');
        tmp.innerHTML = `<tbody>${threadRowHtml(key)}</tbody>`;
        for (const tr of Array.from(tmp.querySelectorAll('tr'))) insertAttachment(row, tr);
      });
      observeThreads(row.closest('.file-card'));
    } else if (key.startsWith('file:')) {
      const bar = key.indexOf('|');
      if (key.slice(5, bar) !== state.viewSha) return;
      const card = cardFor(key.slice(bar + 1));
      if (card) renderFileThreads(card, fileForPath(card.dataset.path));
    } else if (key.startsWith('commit:')) {
      if (key.slice(7) === state.viewSha) renderCommitThreads();
    } else if (key === 'review:') renderReviewThreads(); // no-op unless the combined header is on screen
  }

  function blockHtml(key) {
    const ids = state.threadsByKey.get(key) || [];
    let html = ids.map(threadHtml).join('');
    if (state.openEditors.has(key)) html += `<div class="editor-block">${editorHtml(key)}</div>`;
    return html;
  }

  function renderFileThreads(card, f) {
    if (!card || !f) return;
    const block = card.querySelector('.thread-block[data-key-host="file"]');
    preserveEditors(block, () => { block.innerHTML = blockHtml(`file:${state.viewSha}|${f.path}`); });
    observeThreads(card);
  }

  function renderCommitThreads() {
    const block = $('#commit-header .thread-block[data-key-host="commit"]');
    if (!block) return;
    preserveEditors(block, () => { block.innerHTML = blockHtml(`commit:${state.viewSha}`); });
    observeThreads($('#commit-header'));
  }

  /** Review-level threads (anchor kind=review) sit under the cover letter of the combined header. */
  function renderReviewThreads() {
    const block = $('#commit-header .thread-block[data-key-host="review"]');
    if (!block) return;
    preserveEditors(block, () => { block.innerHTML = blockHtml('review:'); });
    observeThreads($('#commit-header'));
  }

  function patchThreadById(rootId) {
    const el = $(`#main .thread[data-thread-id="${cssEsc(rootId)}"]`);
    if (!el) { patchKey(anchorKey((state.comments.get(rootId) || {}).anchor)); return; }
    const parent = el.parentElement;
    preserveEditors(parent, () => {
      const tmp = document.createElement('div');
      tmp.innerHTML = threadHtml(rootId);
      el.replaceWith(tmp.firstElementChild);
    });
    observeThreads(parent);
  }

  const cssEsc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&'));

  /** Run fn (which rebuilds markup inside container) while keeping editor text, caret and focus. */
  function preserveEditors(container, fn) {
    const saved = new Map();
    if (container) {
      for (const form of $$('form.comment-editor', container)) {
        const ta = form.querySelector('textarea');
        if (ta) saved.set(form.dataset.key, { value: ta.value, start: ta.selectionStart, end: ta.selectionEnd, focused: document.activeElement === ta, height: ta.style.height });
      }
    }
    fn();
    if (!saved.size || !container) return;
    for (const form of $$('form.comment-editor', container)) {
      const s = saved.get(form.dataset.key);
      const ta = form.querySelector('textarea');
      if (!s || !ta) continue;
      ta.value = s.value;
      if (s.height) ta.style.height = s.height;
      if (s.focused) { ta.focus(); try { ta.setSelectionRange(s.start, s.end); } catch (e) { /* ignore */ } }
    }
  }

  /* ---- thread & comment markup */

  function commentTags(c, root) {
    const tags = [];
    if (c.state === 'pending') tags.push(`<span class="tag tag-pending" title="${esc(NEW_DOT_TITLE)}">Pending</span>`);
    else if (c.round != null) tags.push(`<span class="tag tag-round" title="Submitted in round ${esc(c.round)}">R${esc(c.round)}</span>`);
    if (c.updated_at > c.created_at) tags.push(`<span class="tag tag-edited" title="Edited ${esc(fmtAbs(c.updated_at))}">edited</span>`);
    if (c.moved_from) tags.push(`<span class="tag tag-moved" title="Re-anchored from ${esc(shortSha(c.moved_from.commit))}${c.moved_from.line ? ':' + esc(c.moved_from.line) : ''}">moved</span>`);
    if (root && c.outdated) tags.push('<span class="tag tag-outdated" title="Anchored to a commit that is no longer in the range">outdated</span>');
    if (isUnseen(c)) tags.push(`<span class="tag tag-new" data-created="${esc(c.created_at)}">New</span>`);
    return tags.join('');
  }

  function commentHtml(c, root) {
    const editKey = `edit:${c.id}`;
    const editing = state.openEditors.has(editKey);
    const name = c.author === 'claude' ? 'Claude' : 'user';
    return `<div class="comment" data-id="${esc(c.id)}" data-author="${esc(c.author)}">
      <div class="comment-meta">${avatarHtml(c.author, name)}<span class="author">${esc(name)}</span>${timeHtml(c.created_at)}${commentTags(c, root)}
        <div class="comment-actions" role="group" aria-label="Comment actions">
          <button type="button" class="act-edit" aria-label="Edit comment">Edit</button>
          <button type="button" class="act-delete" aria-label="Delete comment">Delete</button>
          <button type="button" class="act-reply" aria-label="Reply to thread">Reply</button>
          ${root ? `<button type="button" class="act-resolve" aria-label="${c.resolved ? 'Unresolve' : 'Resolve'} thread">${c.resolved ? 'Unresolve' : 'Resolve'}</button>` : ''}
        </div>
      </div>
      ${editing ? editorHtml(editKey) : `<div class="comment-body md">${renderMarkdown(c.body)}</div>`}
    </div>`;
  }

  function threadHtml(rootId) {
    const root = state.comments.get(rootId);
    if (!root) return '';
    const members = threadMembers(rootId);
    const newest = members.reduce((m, c) => (c.created_at > m ? c.created_at : m), '');
    const hasNew = members.some(isUnseen);
    const collapsed = root.resolved && !hasNew && !state.expandedResolved.has(rootId);
    const a = root.anchor || {};
    const cls = ['thread', root.resolved ? 'is-resolved' : '', hasNew ? 'has-new' : '', state.currentThread === rootId ? 'is-current' : '', state.orphans.has(rootId) ? 'is-orphan' : ''].filter(Boolean).join(' ');
    const rangeAttrs = a.kind === 'line' && a.start_line ? ` data-range-start="${esc(a.start_line)}" data-range-end="${esc(a.line)}" data-range-side="${esc(a.side)}"` : '';
    let inner;
    if (collapsed) {
      inner = `<div class="resolved-line"><span class="tick">✓</span> Resolved · ${members.length} comment${members.length === 1 ? '' : 's'} <button type="button" class="link-btn btn-show-resolved">Show</button></div>`;
    } else {
      inner = a.kind === 'line' && a.start_line ? `<div class="range-note">Lines ${esc(a.start_line)}–${esc(a.line)} (${esc(a.side)} side)</div>` : '';
      inner += members.map((c) => commentHtml(c, c.id === rootId)).join('');
      if (!commentsDisabled()) {
        inner += `<div class="thread-foot"><button type="button" class="btn-reply${hasDraft(`reply:${rootId}`) ? ' has-draft' : ''}" aria-label="Reply">Reply</button>
          <button type="button" class="btn-resolve-thread" aria-label="${root.resolved ? 'Unresolve' : 'Resolve'} thread">${root.resolved ? 'Unresolve' : 'Resolve'}</button>
          ${root.resolved ? '<button type="button" class="link-btn btn-hide-resolved">Hide</button>' : ''}</div>`;
      }
      if (state.openEditors.has(`reply:${rootId}`)) inner += `<div class="editor-block">${editorHtml(`reply:${rootId}`)}</div>`;
    }
    return `<div class="${cls}" data-thread-id="${esc(rootId)}" data-newest="${esc(newest)}"${rangeAttrs} tabindex="0">${inner}</div>`;
  }

  /* ---- editors */

  function editorHtml(key) {
    const entry = state.openEditors.get(key) || { mode: 'new' };
    const draft = getDraft(key);
    const original = entry.mode === 'edit' ? ((state.comments.get(entry.id) || {}).body || '') : '';
    const initial = draft != null ? draft : original;
    const label = entry.mode === 'edit' ? 'Save' : entry.mode === 'reply' ? 'Reply' : 'Add comment';
    let info = '';
    if (entry.anchor && entry.anchor.kind === 'line') {
      const a = entry.anchor;
      info = `${a.side}:${a.start_line ? `${a.start_line}–` : ''}${a.line}`;
    } else if (entry.anchor && entry.anchor.kind === 'file') info = 'file';
    else if (entry.anchor && entry.anchor.kind === 'commit') info = 'commit';
    else if (entry.anchor && entry.anchor.kind === 'review') info = 'whole change';
    return `<form class="comment-editor" data-key="${esc(key)}" data-mode="${esc(entry.mode)}" novalidate>
      <textarea rows="3" placeholder="${entry.mode === 'reply' ? 'Reply (Markdown)…' : 'Leave a comment (Markdown)…'}" aria-label="Comment text">${esc(initial)}</textarea>
      <div class="editor-foot"><span class="md-hint">Markdown · Ctrl+Enter to post · Esc to cancel</span>${info ? `<span class="anchor-info">${esc(info)}</span>` : ''}
        <button type="button" class="sm-btn btn-discard-comment" aria-label="Discard draft">Discard</button>
        <button type="button" class="sm-btn btn-cancel-comment" aria-label="Cancel (keeps the draft)">Cancel</button>
        <button type="submit" class="sm-btn btn-submit-comment">${label}</button></div>
    </form>`;
  }

  const draftKey = (key) => `ccr:draft:${key}`;
  const getDraft = (key) => storage.get(draftKey(key));
  const hasDraft = (key) => Boolean((getDraft(key) || '').trim());
  const delDraft = (key) => storage.del(draftKey(key));
  const draftTimers = new Map();
  function saveDraftDebounced(key, value) {
    clearTimeout(draftTimers.get(key));
    draftTimers.set(key, setTimeout(() => {
      draftTimers.delete(key);
      if (value.trim()) storage.set(draftKey(key), value); else storage.del(draftKey(key));
      renderDrafts();
    }, 300));
  }
  function allDraftKeys() {
    // Everything but the verdict/summary draft (`ccr:draft:review:<repo>`); the whole-change editor's key is the bare `review:`.
    return storage.keys('ccr:draft:').filter((k) => k !== reviewDraftKey()).map((k) => k.slice('ccr:draft:'.length));
  }

  function editorForm(key) { return $(`form.comment-editor[data-key="${cssEsc(key)}"]`); }

  function focusEditor(key) {
    const form = editorForm(key);
    if (!form) return;
    const ta = form.querySelector('textarea');
    autoGrow(ta);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    form.scrollIntoView({ block: 'nearest' });
  }

  function autoGrow(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(window.innerHeight * 0.6, ta.scrollHeight + 2) + 'px';
  }

  /** Open an editor identified by key. entry = {mode:'new', anchor} | {mode:'reply', rootId} | {mode:'edit', id, rootId}. */
  async function openEditor(key, entry) {
    if (commentsDisabled()) { toast('Comments are disabled in compare view', 'error'); return; }
    state.openEditors.set(key, entry);
    if (key.startsWith('line:')) {
      const p = parseLineKey(key);
      const card = cardFor(p.path);
      if (card) {
        expandCard(card, fileForPath(p.path));
        if (!keyHostRow(key)) await expandToInclude(card, fileForPath(p.path), p.side, p.line).catch(() => false);
        patchKey(key);
      }
    } else if (key.startsWith('reply:') || key.startsWith('edit:')) {
      patchThreadById(entry.rootId);
    } else patchKey(key);
    focusEditor(key);
  }

  function closeEditor(key, { discard = false } = {}) {
    const form = editorForm(key);
    const entry = state.openEditors.get(key);
    if (form) {
      const ta = form.querySelector('textarea');
      if (discard || !ta.value.trim()) delDraft(key);
      else storage.set(draftKey(key), ta.value);
    } else if (discard) delDraft(key);
    clearTimeout(draftTimers.get(key));
    state.openEditors.delete(key);
    if (!entry) return;
    if (key.startsWith('line:')) {
      const tr = form && form.closest('tr.editor');
      if (tr) tr.remove(); else patchKey(key);
    } else if (key.startsWith('reply:') || key.startsWith('edit:')) patchThreadById(entry.rootId);
    else patchKey(key);
    renderDrafts();
    updateDraftDots();
  }

  function updateDraftDots() {
    for (const b of $$('#main .btn-add-comment[data-line]')) {
      const card = b.closest('.file-card');
      b.classList.toggle('has-draft', hasDraft(lineKey(state.viewSha, card.dataset.path, b.dataset.side, +b.dataset.line)));
    }
    for (const b of $$('#main .btn-comment-file')) b.classList.toggle('has-draft', hasDraft(`file:${state.viewSha}|${b.closest('.file-card').dataset.path}`));
    const bc = $('#btn-comment-commit');
    if (bc) bc.classList.toggle('has-draft', hasDraft(`commit:${state.viewSha}`));
    const br = $('#btn-comment-review');
    if (br) br.classList.toggle('has-draft', hasDraft('review:'));
  }

  function setEditorBusy(form, busy) {
    for (const b of form.querySelectorAll('button')) b.disabled = busy;
    form.classList.toggle('is-busy', busy);
  }

  async function submitEditor(form) {
    const key = form.dataset.key;
    const entry = state.openEditors.get(key);
    if (!entry || state.inflight.has(key)) return;
    const ta = form.querySelector('textarea');
    const body = ta.value.trim();
    if (!body) { toast('The comment is empty', 'error'); ta.focus(); return; }
    setEditorBusy(form, true);
    state.inflight.add(key);
    try {
      let c;
      if (entry.mode === 'new') c = await api('/api/comments', { method: 'POST', body: { body, anchor: entry.anchor } });
      else if (entry.mode === 'reply') {
        const root = state.comments.get(entry.rootId);
        c = await api('/api/comments', { method: 'POST', body: { body, parent_id: entry.rootId, anchor: root ? root.anchor : undefined } });
      } else c = await api(`/api/comments/${encodeURIComponent(entry.id)}`, { method: 'PATCH', body: { body } });
      state.inflight.delete(key);
      clearTimeout(draftTimers.get(key));
      delDraft(key);
      state.openEditors.delete(key);
      upsertComment(c);
      if (entry.mode === 'new' && entry.anchor.kind === 'line') clearSelection();
      renderDrafts();
      updateDraftDots();
    } catch (e) {
      state.inflight.delete(key);
      const f = editorForm(key);
      if (f) setEditorBusy(f, false);
      toast(e.message || 'Request failed', 'error');
    }
  }

  async function deleteComment(id) {
    const c = state.comments.get(id);
    if (!c) return;
    const replies = c.parent_id ? [] : threadMembers(id).filter((m) => m.id !== id);
    const msg = replies.length ? `Delete this comment? This also deletes ${replies.length} repl${replies.length === 1 ? 'y' : 'ies'}.` : 'Delete this comment?';
    if (!window.confirm(msg)) return;
    try {
      await api(`/api/comments/${encodeURIComponent(id)}${replies.length ? '?cascade=1' : ''}`, { method: 'DELETE' });
      removeComments([id, ...replies.map((r) => r.id)]);
    } catch (e) { toast(e.message, 'error'); }
  }

  async function toggleResolved(rootId) {
    const c = state.comments.get(rootId);
    if (!c) return;
    try {
      const updated = await api(`/api/comments/${encodeURIComponent(rootId)}`, { method: 'PATCH', body: { resolved: !c.resolved } });
      if (updated.resolved) state.expandedResolved.delete(rootId);
      upsertComment(updated);
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ---- Markdown (safe subset) */

  /** Inline Markdown on one escaped line: code spans and links are parked in placeholders so later passes never touch their markup. */
  function renderInline(text) {
    const parked = [];
    const park = (html) => { parked.push(html); return `\u0000${parked.length - 1}\u0000`; };
    let s = esc(text).replace(/`([^`\n]+)`/g, (m, code) => park(`<code>${code}</code>`));
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
      const clean = url.replace(/[\u0000-\u001f\u007f-\u009f]/g, '').trim();
      return /^https?:\/\//i.test(clean) ? park(`<a href="${clean}" target="_blank" rel="noopener noreferrer">${label}</a>`) : `${label} (${url})`;
    });
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^\w/&#;])([0-9a-f]{7,40})(?![\w;])/g, (m, pre, hex) => {
      const c = findCommit(hex);
      return c ? `${pre}<a href="#${esc(c.sha)}" class="sha-link" data-sha="${esc(c.sha)}" title="${esc(c.subject)}">${hex}</a>` : m;
    });
    return s.replace(/\u0000(\d+)\u0000/g, (m, i) => parked[+i]);
  }

  function renderMarkdown(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = []; let list = null; let quote = null;
    const flush = () => {
      if (para.length) { out.push(`<p>${para.map(renderInline).join('<br>')}</p>`); para = []; }
      if (list) { out.push(`<ul>${list.map((l) => `<li>${renderInline(l)}</li>`).join('')}</ul>`); list = null; }
      if (quote) { out.push(`<blockquote>${quote.map(renderInline).join('<br>')}</blockquote>`); quote = null; }
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = /^```\s*([\w+-]*)\s*$/.exec(line);
      if (fence) {
        flush();
        const lang = fence[1];
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
        const code = buf.join('\n');
        let html = esc(code);
        if (lang && window.hljs && window.hljs.getLanguage(lang)) {
          try { html = window.hljs.highlight(code, { language: lang, ignoreIllegals: true }).value; } catch (e) { html = esc(code); }
        }
        out.push(`<pre><code${lang ? ` class="language-${esc(lang)}"` : ''}>${html}</code></pre>`);
        continue;
      }
      if (/^\s*$/.test(line)) { flush(); continue; }
      const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) { flush(); const level = heading[1].length; out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`); continue; }
      const li = /^\s*[-*]\s+(.*)$/.exec(line);
      if (li) { if (para.length || quote) flush(); (list = list || []).push(li[1]); continue; }
      const q = /^>\s?(.*)$/.exec(line);
      if (q) { if (para.length || list) flush(); (quote = quote || []).push(q[1]); continue; }
      if (list || quote) flush();
      para.push(line);
    }
    flush();
    return out.join('');
  }

  /* ==================================================================== 6. live updates */

  /* Only the visible tab polls. Among visible tabs of one origin a single "leader" holds a Web Lock
     and long-polls; the others ("followers") wait for the lock and refetch when the leader broadcasts
     a changed state. Every (re)start bumps state.pollSeq so a superseded loop winds down by itself. */

  const LOCK_NAME = 'ccr:poll:' + location.port;
  let channel;
  function eventsChannel() {
    if (channel !== undefined) return channel;
    try { channel = new BroadcastChannel('ccr:events'); channel.onmessage = onChannelMessage; } catch (e) { channel = null; }
    return channel;
  }
  function broadcast(msg) {
    const ch = eventsChannel();
    if (ch) { try { ch.postMessage(msg); } catch (e) { /* channel closed */ } }
  }
  function onChannelMessage(ev) {
    const msg = ev.data || {};
    if (msg.type !== 'state' || state.pollRole !== 'follower' || !state.loadedOnce || !msg.state) return;
    if (msg.state.version !== state.version || msg.state.generation !== state.generation) onChanged(msg.state).catch(() => {});
  }

  function startPolling() {
    if (state.polling || !state.token) return;
    state.polling = true;
    runPoller(++state.pollSeq);
  }

  function stopPolling() {
    state.polling = false;
    state.pollRole = null;
    if (state.pollAbort) { state.pollAbort.abort(); state.pollAbort = null; }
  }

  const pollActive = (id) => state.polling && Boolean(state.token) && id === state.pollSeq && document.visibilityState === 'visible';

  async function runPoller(id) {
    if (!navigator.locks || !eventsChannel()) { await pollLoop(id); return; }
    const ctrl = new AbortController();
    state.pollAbort = ctrl;
    state.pollRole = 'follower';
    try {
      await navigator.locks.request(LOCK_NAME, { signal: ctrl.signal }, async () => {
        if (state.pollAbort === ctrl) state.pollAbort = null;
        if (!pollActive(id)) return;
        state.pollRole = 'leader';
        await pollLoop(id);
      });
    } catch (e) { /* aborted while waiting for the lock (tab hidden / re-init) */ }
    if (id === state.pollSeq) state.pollRole = null;
  }

  async function pollLoop(id) {
    let backoff = 1000;
    while (pollActive(id)) {
      const ctrl = new AbortController();
      state.pollAbort = ctrl;
      try {
        if (state.disconnectedSince || !state.loadedOnce) {
          // A short request first, so a reconnect is noticed at once instead of after a full 25 s long-poll.
          const probe = await api('/api/state', { signal: ctrl.signal });
          if (!state.loadedOnce) { state.polling = false; initialLoad(); return; } // the first load failed earlier: redo it
          await onReconnected(probe);
          if (!pollActive(id)) return; // a server restart re-initialised the app and started a new loop
        }
        const st = await api(`/api/events?since=${encodeURIComponent(state.version)}&timeout=25`, { signal: ctrl.signal });
        if (state.pollAbort === ctrl) state.pollAbort = null;
        backoff = 1000;
        if (st.changed) { await onChanged(st); broadcast({ type: 'state', state: st }); }
        if (st.retry_after) await sleep(st.retry_after * 1000);
      } catch (e) {
        if (state.pollAbort === ctrl) state.pollAbort = null;
        if (e && (e.name === 'AbortError' || e.status === 401)) return;
        onDisconnected();
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 10000);
      }
    }
    if (id === state.pollSeq && document.visibilityState !== 'visible') state.polling = false; // suspended: visibilitychange restarts
  }

  async function onReconnected(st) {
    state.disconnectedSince = null;
    $('#banner-disconnected').hidden = true;
    toast('Reconnected', 'success', { timeout: 2500 });
    await syncTo(st);
  }

  function onDisconnected() {
    const banner = $('#banner-disconnected');
    if (!state.disconnectedSince) state.disconnectedSince = Date.now();
    const severe = Date.now() - state.disconnectedSince > 30000;
    banner.querySelector('.banner-text').textContent = severe ? 'Server not responding — it may have been stopped (ccr status)' : 'Disconnected — retrying…';
    banner.classList.toggle('is-severe', severe);
    banner.hidden = false;
  }

  /** Server reported version > ours: refetch comments, and the review + diff when the generation moved. */
  async function onChanged(st) {
    if (st.server && state.startedAt && st.server.started_at !== state.startedAt) { await fullReinit(); return; }
    const genChanged = st.generation !== state.generation;
    const roundsChanged = state.review && typeof st.rounds === 'number' && st.rounds !== state.review.rounds.length;
    const prev = state.review;
    if (genChanged || roundsChanged) applyReview(await api('/api/review'));
    const cs = await api('/api/comments');
    applyComments(cs.comments);
    state.version = Math.max(state.version, cs.version, st.version || 0);
    if (genChanged) {
      await reloadCurrentView();
      // `ccr cover` bumps the generation too; say so instead of announcing a chain reload when only the cover moved.
      const coverOnly = prev && prev.cover !== state.review.cover && sameChain(prev, state.review);
      toast(coverOnly ? 'Cover letter updated' : 'Chain reloaded — diffs refreshed', 'info');
    }
  }

  const sameChain = (a, b) => a.range.base === b.range.base && a.range.head === b.range.head
    && a.commits.map((c) => c.sha).join(',') === b.commits.map((c) => c.sha).join(',');

  /** Bring the UI up to date with a state() document when its version, generation or server identity differs. */
  async function syncTo(st) {
    const restarted = st.server && state.startedAt && st.server.started_at !== state.startedAt;
    if (restarted || st.version !== state.version || st.generation !== state.generation) await onChanged(st);
  }

  async function resync() { await syncTo(await api('/api/state')); }

  async function fullReinit() {
    stopPolling();
    state.startedAt = null;
    state.diffs.clear(); state.fileText.clear(); state.hl.clear(); state.perCommitScroll.clear();
    state.comments.clear(); state.threadsByKey.clear(); state.threadOrder = []; state.knownIds = new Set();
    toast('Server restarted — reloading the review', 'info');
    await initialLoad();
  }

  /** Re-fetch the selected view after a generation change, keeping scroll position, editors and selection. */
  async function reloadCurrentView() {
    const main = $('#main');
    const top = main.scrollTop;
    state.diffs.clear(); state.fileText.clear(); state.hl.clear();
    if (state.compare) {
      try { await loadCompare(state.compare.base, state.compare.head); } catch (e) { state.compare = null; }
    }
    if (!state.compare) {
      if (!commitMeta(state.selectedSha)) state.selectedSha = 'combined';
      await loadDiff(state.selectedSha);
      state.viewSha = state.selectedSha;
    }
    renderView();
    main.scrollTop = top;
  }

  /* ---- seen tracking */

  let seenObserver = null;
  const seenTimers = new Map();
  function ensureSeenObserver() {
    if (seenObserver) return seenObserver;
    seenObserver = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const el = e.target;
        if (e.isIntersecting && document.visibilityState === 'visible') {
          if (!seenTimers.has(el)) seenTimers.set(el, setTimeout(() => { seenTimers.delete(el); if (el.isConnected) markSeen(el.dataset.newest); }, 1000));
        } else if (seenTimers.has(el)) { clearTimeout(seenTimers.get(el)); seenTimers.delete(el); }
      }
    }, { root: $('#main'), threshold: 0.25 });
    return seenObserver;
  }

  function observeThreads(root) {
    if (!root) return;
    const io = ensureSeenObserver();
    for (const t of $$('.thread.has-new', root)) io.observe(t);
  }

  function markSeen(iso) {
    if (!iso || iso <= state.seenUntil) return;
    state.seenUntil = iso;
    storage.set('ccr:seen:' + repoKey(), iso);
    for (const tag of $$('.tag-new[data-created]')) if (tag.dataset.created <= iso) tag.remove();
    for (const t of $$('.thread.has-new')) if ((t.dataset.newest || '') <= iso) { t.classList.remove('has-new'); if (seenObserver) seenObserver.unobserve(t); }
    deriveCounts();
    updateCommitBadges();
    renderFileTree();
    updateFileHeaders();
    updateDrawerCounts();
    if (state.drawer.open && state.drawer.tab === 'new') renderDrawerList();
  }

  function markAllSeen() {
    let max = state.seenUntil;
    for (const c of state.comments.values()) if (c.created_at > max) max = c.created_at;
    markSeen(max || new Date(serverNow()).toISOString().replace(/\.\d{3}Z$/, 'Z'));
    renderDrawer();
  }

  /* ==================================================================== 7. navigation */

  function parseHash(h) {
    if (!h || h === '#') return null;
    const cmp = /^#compare:([0-9a-f]{0,64})\.\.([0-9a-f]{4,64})$/.exec(h);
    if (cmp) return { compare: { a: cmp[1], b: cmp[2] } };
    const m = /^#([^/:]+)(?:\/(.+?))?(?::([on])(\d+)(?:-(\d+))?)?$/.exec(h);
    if (!m) return null;
    const sha = m[1];
    if (!isPseudo(sha) && !/^[0-9a-f]{4,64}$/.test(sha)) return null;
    let path = null;
    if (m[2]) { try { path = decodeURIComponent(m[2]); } catch (e) { return null; } }
    return { sha, path, side: m[3] ? (m[3] === 'o' ? 'old' : 'new') : null, line: m[4] ? parseInt(m[4], 10) : null, endLine: m[5] ? parseInt(m[5], 10) : null };
  }

  function buildHash(t) {
    if (t.compare) return `#compare:${t.compare.base ? t.compare.base.slice(0, 10) : ''}..${t.compare.head.slice(0, 10)}`;
    let h = '#' + t.sha;
    if (t.path) h += '/' + encodeURIComponent(t.path);
    if (t.line) h += ':' + (t.side === 'old' ? 'o' : 'n') + t.line + (t.endLine && t.endLine !== t.line ? '-' + t.endLine : '');
    return h;
  }

  /** pushState/replaceState never fire hashchange, so writing the hash never re-enters navigation. */
  function writeHash(target, replace) {
    const h = buildHash(target);
    if (location.hash === h) return;
    if (replace) history.replaceState(null, '', h); else history.pushState(null, '', h);
  }

  function permalink() {
    return `${location.origin}/?t=${encodeURIComponent(state.token)}${location.hash}`;
  }

  /** Apply the current location.hash (boot, back/forward, manual edits); never pushes history itself. */
  async function navigateFromHash() {
    const t = parseHash(location.hash);
    if (!t) { await navigateTo({ sha: 'combined' }, { push: false }); return; }
    if (t.compare) {
      const head = findCommit(t.compare.b);
      let base = null;
      if (t.compare.a) {
        const listed = findCommit(t.compare.a);
        base = listed ? listed.sha : (realCommits().map((c) => c.parents[0]).find((p) => p && p.startsWith(t.compare.a)) || null);
        if (!base) { toast('Unknown compare base', 'error'); await navigateTo({ sha: 'combined' }, { push: false }); return; }
      }
      if (!head) { toast('Unknown compare head', 'error'); await navigateTo({ sha: 'combined' }, { push: false }); return; }
      await openCompare(base, head.sha, { push: false });
      return;
    }
    await navigateTo(t, { push: false });
  }

  function findRow(card, side, line) {
    return card.querySelector(`tr.line[data-${side === 'old' ? 'o' : 'n'}="${line}"]`);
  }

  function elTopInMain(el) {
    const main = $('#main');
    return el.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop;
  }

  /** Scroll #main so that el sits `offset` px below the sticky file header (instant, so callers can rely on the position). */
  function scrollToEl(el, { offset = 120 } = {}) {
    const main = $('#main');
    const card = el.closest('.file-card');
    const hdr = card && !el.classList.contains('file-header') && !el.classList.contains('file-card') ? card.querySelector('.file-header') : null;
    const headerOffset = hdr ? hdr.offsetHeight : 0;
    main.scrollTop = Math.max(0, elTopInMain(el) - headerOffset - offset);
  }

  function flash(el) {
    el.classList.remove('is-flash');
    void el.offsetWidth;
    el.classList.add('is-flash');
    setTimeout(() => el.classList.remove('is-flash'), 1500);
  }

  /** Navigate to a commit / file / line / thread; the single entry point behind hash, sidebar, drawer and keys. */
  async function navigateTo(t, { push = true } = {}) {
    let sha = t.sha;
    if (t.threadId && !sha) {
      const root = state.comments.get(t.threadId);
      if (!root) return;
      const a = root.anchor || {};
      sha = anchorView(a); // review-level threads live in the combined header
      if (!sha) { openDrawer('all'); return; }
      t = Object.assign({}, t, { sha, path: a.path, side: a.side, line: a.line, endLine: a.start_line ? a.line : null, startLine: a.start_line });
    }
    const c = findCommit(sha);
    if (!c) {
      if (sha && sha.startsWith('compare:')) return;
      toast(`Commit ${shortSha(sha)} is not in this review`, 'error');
      sha = 'combined';
    } else sha = c.sha;
    if (state.compare || sha !== state.selectedSha || !currentDiff()) {
      await selectCommit(sha, { push, keepFile: Boolean(t.path) });
      if (state.viewSha !== sha) return;
    }
    if (!t.path) {
      if (push && !state.compare) writeHash({ sha }, false);
      if (t.threadId) focusThreadById(t.threadId);
      return;
    }
    const card = cardFor(t.path) || $$('#files .file-card').find((x) => x.dataset.oldPath === t.path);
    if (!card) { toast(`${t.path} is not part of this commit`, 'error'); return; }
    const f = fileForPath(card.dataset.path);
    expandCard(card, f);
    card.classList.remove('is-hidden-viewed');
    if (t.line) {
      const side = t.side || 'new';
      let row = findRow(card, side, t.line);
      if (!row && !f.too_large && !f.binary) {
        await expandToInclude(card, f, side, t.line).catch(() => false);
        row = findRow(card, side, t.line);
      }
      if (row) {
        const lo = t.startLine || t.line; const hi = t.endLine || t.line;
        if (!t.threadId && !commentsDisabled()) {
          state.sel = { sha: state.viewSha, path: card.dataset.path, side, startLine: Math.min(lo, hi), endLine: Math.max(lo, hi), dragging: false, table: row.closest('table.diff') };
          applySelectionClasses(card);
        }
        writeHash({ sha, path: card.dataset.path, side, line: Math.min(lo, hi), endLine: hi > lo ? hi : null }, !push);
        if (!t.threadId) { scrollToEl(row); flash(row); }
      } else {
        writeHash({ sha, path: card.dataset.path }, !push);
        toast('That line is not in the current diff — see the drawer', 'info');
        scrollToEl(card, { offset: 20 });
      }
    } else {
      writeHash({ sha, path: card.dataset.path }, !push);
      scrollToEl(card, { offset: 16 });
      flash(card);
    }
    if (t.threadId) focusThreadById(t.threadId);
    if (window.innerWidth < 1280 && state.drawer.open) closeDrawer();
  }

  function focusThreadById(id) {
    let el = $(`#main .thread[data-thread-id="${cssEsc(id)}"]`);
    if (!el) { toast('Thread anchor not found in the current diff', 'info'); return; }
    if (el.querySelector('.btn-show-resolved')) { state.expandedResolved.add(id); patchThreadById(id); el = $(`#main .thread[data-thread-id="${cssEsc(id)}"]`); }
    setCurrentThread(id);
    scrollToEl(el);
    flash(el);
  }

  function setCurrentThread(id) {
    state.currentThread = id;
    for (const t of $$('#main .thread.is-current')) t.classList.remove('is-current');
    if (id) {
      const el = $(`#main .thread[data-thread-id="${cssEsc(id)}"]`);
      if (el) { el.classList.add('is-current'); el.focus({ preventScroll: true }); }
    }
  }

  async function loadDiff(sha) {
    if (state.diffs.has(sha)) return state.diffs.get(sha);
    const d = await api(`/api/commits/${encodeURIComponent(sha)}${wsQuery()}`);
    state.diffs.set(sha, d);
    recomputeOrphans();
    return d;
  }

  async function loadCompare(base, head) {
    const q = `${base ? `base=${encodeURIComponent(base)}&` : ''}head=${encodeURIComponent(head)}${wsQuery('&')}`;
    const d = await api(`/api/compare?${q}`);
    state.diffs.set(d.sha, d);
    state.compare = { base, head };
    state.viewSha = d.sha;
    return d;
  }

  async function selectCommit(sha, { push = true, keepFile = false } = {}) {
    const main = $('#main');
    if (state.viewSha && !state.compare && currentDiff()) state.perCommitScroll.set(state.selectedSha, { top: main.scrollTop, path: currentFilePath() });
    const prevPath = !state.compare && currentDiff() ? currentFilePath() : null;
    clearSelection({ keepHash: true });
    hideTooltip();
    state.compare = null;
    state.selectedSha = sha;
    state.viewSha = sha;
    state.currentThread = null;
    if (commitMeta(sha) && commitMeta(sha).kind === 'commit') state.rangeAnchorSha = sha;
    updateCommitSelection();
    if (push && !keepFile) writeHash({ sha }, false); // with a file target the caller pushes the full hash once
    try { await loadDiff(sha); } catch (e) { toast(`Could not load ${shortSha(sha)}: ${e.message}`, 'error'); return; }
    if (state.viewSha !== sha) return;
    renderView();
    if (keepFile) return;
    const diff = currentDiff();
    const target = prevPath && diff.files.find((f) => f.path === prevPath || f.old_path === prevPath);
    if (target) {
      const card = cardFor(target.path);
      if (card) { scrollToEl(card, { offset: 16 }); flash(card); }
    } else {
      const mem = state.perCommitScroll.get(sha);
      main.scrollTop = mem ? mem.top : 0;
    }
    if (window.innerWidth < 1280 && state.drawer.open) closeDrawer();
  }

  function renderView() {
    $('#banner-compare').hidden = !state.compare;
    renderHeader();
    renderFiles(); // also renders the file tree (through applyFileFilter)
    updateCommitSelection();
    state.currentFile = 0;
    updateDraftDots();
    if (state.drawer.open) renderDrawerList();
  }

  async function openCompare(base, head, { push = true, sinceRound = false } = {}) {
    const main = $('#main');
    if (!state.compare && currentDiff()) state.perCommitScroll.set(state.selectedSha, { top: main.scrollTop, path: currentFilePath() });
    clearSelection({ keepHash: true });
    hideTooltip();
    try {
      await loadCompare(base, head);
    } catch (e) {
      toast(sinceRound && e.status === 400 ? 'previous head no longer available' : `Compare failed: ${e.message}`, 'error');
      return;
    }
    state.currentThread = null;
    if (push) writeHash({ compare: { base, head } }, false);
    renderView();
    main.scrollTop = 0;
  }

  function exitCompare() {
    if (!state.compare) return;
    navigateTo({ sha: state.selectedSha || 'combined' });
  }

  function shiftClickCommit(sha) {
    const c = commitMeta(sha);
    if (!c || c.kind !== 'commit') { toast('Compare ranges use real commits only', 'info'); return; }
    const anchor = commitMeta(state.rangeAnchorSha || state.selectedSha);
    if (!anchor || anchor.kind !== 'commit') { toast('Select a real commit first, then Shift+click another', 'info'); return; }
    const list = realCommits();
    const i = list.indexOf(anchor); const j = list.indexOf(c);
    const lo = Math.min(i, j); const hi = Math.max(i, j);
    openCompare(list[lo].parents[0] || null, list[hi].sha);
  }

  function compareSinceRound(n) {
    const round = (state.review.rounds || []).find((r) => r.number === n);
    if (!round) return;
    openCompare(round.head, state.review.range.head, { sinceRound: true });
  }

  /* ---- top bar actions */

  function toggleViewMode() {
    state.viewMode = state.viewMode === 'unified' ? 'split' : 'unified';
    storage.set('ccr:viewmode', state.viewMode);
    applyViewPrefs();
    for (const card of $$('#files .file-card[data-rendered="1"]')) if (card.querySelector('table.diff')) rerenderBody(card);
    applySelectionClasses();
  }

  function toggleWrap() {
    state.wrap = !state.wrap;
    storage.set('ccr:wrap', state.wrap ? '1' : '0');
    applyViewPrefs();
  }

  async function toggleWs() {
    state.wsIgnore = !state.wsIgnore;
    storage.set('ccr:ws', state.wsIgnore ? '1' : '0');
    applyViewPrefs();
    await reloadCurrentView();
  }

  async function doReload() {
    const btn = $('#btn-reload');
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const r = await api('/api/reload', { method: 'POST', body: {} });
      applyReview(r.review);
      const cs = await api('/api/comments');
      applyComments(cs.comments);
      await reloadCurrentView();
      showReloadedBanner(r);
    } catch (e) {
      toast(e.status === 400 ? `Reload failed: ${e.message}` : e.message, 'error');
    } finally { btn.disabled = false; }
  }

  function showReloadedBanner(r) {
    const b = $('#banner-reloaded');
    b.querySelector('.banner-text').textContent = `Chain reloaded: +${r.commits_added} −${r.commits_removed} commits, ${(r.remapped || []).length} comments remapped, ${(r.outdated || []).length} now outdated`;
    const since = b.querySelector('.btn-since-round');
    const lr = lastRound();
    if (lr) { since.hidden = false; since.textContent = `Show changes since round ${lr.number}`; since.dataset.round = String(lr.number); } else since.hidden = true;
    b.hidden = false;
  }

  function toggleSidebar() {
    const app = $('#app');
    app.classList.toggle('sidebar-collapsed');
    storage.set('ccr:sidebar', app.classList.contains('sidebar-collapsed') ? 'collapsed' : 'open');
  }

  function toggleHelp(force) {
    const help = $('#help');
    const open = force != null ? force : help.hidden;
    help.hidden = !open;
    if (open) $('#btn-help-close').focus();
  }

  /* ---- files toolbar & keyboard helpers */

  function currentCard() {
    const cards = visibleCards();
    return cards[clamp(state.currentFile, 0, Math.max(0, cards.length - 1))] || null;
  }

  function updateCurrentFileFromScroll() {
    const main = $('#main');
    const top = main.getBoundingClientRect().top + 60;
    const cards = visibleCards();
    let idx = 0;
    for (let i = 0; i < cards.length; i++) { if (cards[i].getBoundingClientRect().top <= top) idx = i; else break; }
    state.currentFile = idx;
  }

  function nextFile(dir) {
    const cards = visibleCards();
    if (!cards.length) return;
    updateCurrentFileFromScroll();
    const idx = clamp(state.currentFile + dir, 0, cards.length - 1);
    if (idx === state.currentFile && ((dir > 0 && idx === cards.length - 1) || (dir < 0 && idx === 0))) { toast(dir > 0 ? 'Last file' : 'First file', 'info', { timeout: 1500 }); }
    state.currentFile = idx;
    const card = cards[idx];
    ensureRendered(card);
    scrollToEl(card, { offset: 8 });
    flash(card);
    renderFileTree();
  }

  function nextCommit(dir) {
    const list = state.review.commits;
    const next = commitIndex(state.selectedSha) + dir;
    if (next < 0 || next >= list.length) { toast(dir > 0 ? 'Last commit' : 'First commit', 'info', { timeout: 1500 }); return; }
    navigateTo({ sha: list[next].sha });
  }

  const isShown = (el) => el.offsetParent !== null;

  function threadsInView() {
    return $$('#main .thread:not(.is-orphan)').filter(isShown);
  }

  function nextThread(dir) {
    const list = threadsInView();
    if (!list.length) { toast('No threads in this view', 'info', { timeout: 1500 }); return; }
    const cur = list.findIndex((t) => t.dataset.threadId === state.currentThread);
    const next = cur < 0 ? (dir > 0 ? 0 : list.length - 1) : cur + dir;
    if (next < 0 || next >= list.length) { toast('No more threads', 'info', { timeout: 1500 }); return; }
    focusThreadById(list[next].dataset.threadId);
  }

  function unresolvedOrder() {
    const roots = state.threadOrder.map((id) => state.comments.get(id)).filter((r) => r && !r.resolved && !r.outdated && anchorView(r.anchor));
    roots.sort((a, b) => commitIndex(anchorView(a.anchor)) - commitIndex(anchorView(b.anchor)) || String(a.anchor.path || '').localeCompare(String(b.anchor.path || ''))
      || (a.anchor.line || 0) - (b.anchor.line || 0) || a.created_at.localeCompare(b.created_at));
    return roots;
  }

  function nextUnresolved(dir) {
    const list = unresolvedOrder();
    if (!list.length) { toast('No unresolved threads', 'info', { timeout: 1500 }); return; }
    const cur = list.findIndex((r) => r.id === state.currentThread);
    const next = cur < 0 ? (dir > 0 ? 0 : list.length - 1) : cur + dir;
    if (next < 0 || next >= list.length) { toast('No more threads', 'info', { timeout: 1500 }); return; }
    navigateTo({ threadId: list[next].id });
  }

  function commentShortcut() {
    if (commentsDisabled()) { toast('Comments are disabled in compare view', 'info'); return; }
    if (state.currentThread && $(`#main .thread[data-thread-id="${cssEsc(state.currentThread)}"]`)) { openEditor(`reply:${state.currentThread}`, { mode: 'reply', rootId: state.currentThread }); return; }
    const a = selectionAnchor();
    if (!a) { toast('Select a line first (click a line number)', 'info'); return; }
    openEditor(anchorKey(a), { mode: 'new', anchor: a });
  }

  function toggleViewedCurrent() {
    const card = currentCard();
    if (!card) return;
    const f = fileForPath(card.dataset.path);
    setViewed(f, !isViewed(f));
  }

  /** Open a card regardless of why it is collapsed; a viewed file stays viewed. */
  function expandCard(card, f) {
    state.collapsedFiles.delete(f.path);
    if (isViewed(f)) state.expandedViewed.add(f.path);
    updateCardCollapse(card, f);
    ensureRendered(card);
  }

  function toggleCollapseCard(card) {
    const f = fileForPath(card.dataset.path);
    if (card.classList.contains('is-collapsed')) { expandCard(card, f); return; }
    state.collapsedFiles.add(f.path);
    updateCardCollapse(card, f);
  }

  function collapseAll(on) {
    const diff = currentDiff();
    if (!diff) return;
    state.collapsedFiles = on ? new Set(diff.files.map((f) => f.path)) : new Set();
    for (const card of $$('#files .file-card')) {
      const f = fileForPath(card.dataset.path);
      if (on) updateCardCollapse(card, f); else expandCard(card, f);
    }
  }

  function toggleCollapseAll() {
    const any = $$('#files .file-card').some((c) => !c.classList.contains('is-collapsed'));
    collapseAll(any);
  }

  /* ==================================================================== drawer */

  function drawerWidth() {
    return parseInt(getComputedStyle($('#app')).getPropertyValue('--drawer-w'), 10) || 420;
  }

  function applyDrawerLayout() {
    const open = state.drawer.open;
    const d = $('#drawer');
    d.classList.toggle('is-open', open);
    d.setAttribute('aria-hidden', String(!open));
    const wide = window.innerWidth >= 1280;
    $('#drawer-backdrop').hidden = !(open && !wide);
    const pad = open && wide ? drawerWidth() + 'px' : '';
    $('#main').style.paddingRight = pad;
    $('#banners').style.paddingRight = pad;
  }

  function openDrawer(tab, { path = null } = {}) {
    if (tab) state.drawer.tab = tab;
    state.currentDrawerFilterPath = path;
    state.drawer.open = true;
    applyDrawerLayout();
    renderDrawer();
  }
  function closeDrawer() { state.drawer.open = false; applyDrawerLayout(); }
  function toggleDrawer() { if (state.drawer.open) closeDrawer(); else openDrawer(); }

  function threadMatchesTab(rootId, tab) {
    const root = state.comments.get(rootId);
    if (!root) return false;
    const members = threadMembers(rootId);
    if (tab === 'pending') return members.some((c) => c.state === 'pending');
    if (tab === 'new') return members.some(isUnseen);
    if (tab === 'unresolved') return !root.resolved && !root.outdated;
    return true;
  }

  function drawerRoots(tab) {
    let ids = state.threadOrder.filter((id) => threadMatchesTab(id, tab));
    if (state.drawer.thisCommitOnly) ids = ids.filter((id) => anchorView(state.comments.get(id).anchor) === state.selectedSha);
    if (state.currentDrawerFilterPath) ids = ids.filter((id) => { const a = state.comments.get(id).anchor; return a && a.path === state.currentDrawerFilterPath; });
    const groupIdx = (root) => {
      if (root.outdated) return 1e6;
      if (!root.anchor || root.anchor.kind === 'review') return 1e5;
      const i = commitIndex(root.anchor.commit);
      return i < 0 ? 1e6 : i;
    };
    return ids.map((id) => state.comments.get(id)).sort((a, b) => groupIdx(a) - groupIdx(b)
      || String((a.anchor || {}).path || '').localeCompare(String((b.anchor || {}).path || ''))
      || (((a.anchor || {}).line || 0) - ((b.anchor || {}).line || 0)) || a.created_at.localeCompare(b.created_at));
  }

  function updateDrawerCounts() {
    for (const tab of ['pending', 'new', 'unresolved', 'all']) {
      const n = state.threadOrder.filter((id) => threadMatchesTab(id, tab)).length;
      const el = $(`#drawer .tab[data-tab="${tab}"] .count`);
      el.textContent = String(n);
      el.classList.toggle('has-items', n > 0);
    }
  }

  function anchorLabel(a) {
    if (!a || a.kind === 'review') return 'review';
    if (a.kind === 'commit') return 'commit';
    const loc = a.kind === 'line' ? `:${a.side === 'old' ? 'o' : 'n'}${a.start_line ? `${a.start_line}–` : ''}${a.line}` : '';
    return `${a.path}${loc}`;
  }

  function drawerItemHtml(root) {
    const members = threadMembers(root.id);
    const a = root.anchor || {};
    const tags = [];
    if (root.resolved) tags.push('<span class="tag tag-resolved">resolved</span>');
    if (root.outdated) tags.push('<span class="tag tag-outdated">outdated</span>');
    if (root.moved_from) tags.push('<span class="tag tag-moved">moved</span>');
    if (state.orphans.has(root.id)) tags.push('<span class="tag tag-orphan" title="anchor not found in current diff">orphan</span>');
    if (members.some((c) => c.state === 'pending')) tags.push('<span class="tag tag-pending">pending</span>');
    if (members.some(isUnseen)) tags.push('<span class="tag tag-new">new</span>');
    const authors = [...new Set(members.map((c) => c.author))].map((au) => avatarHtml(au, au === 'claude' ? 'Claude' : 'user')).join('');
    const snippet = (root.snippet || '').split('\n')[0];
    const excerpt = root.body.replace(/\s+/g, ' ').trim().slice(0, 60);
    return `<button type="button" class="drawer-item${state.currentThread === root.id ? ' is-current' : ''}" data-thread-id="${esc(root.id)}" role="listitem">
      <span class="anchor"><span class="sha">${esc(a.commit ? shortSha(a.commit) : '')}</span><span>${esc(anchorLabel(a))}</span></span>
      ${snippet ? `<span class="snippet">${esc(snippet)}</span>` : ''}
      <span class="excerpt">${renderInline(excerpt)}${root.body.length > 60 ? '…' : ''}</span>
      <span class="row3">${authors}<span class="n">${members.length} comment${members.length === 1 ? '' : 's'}</span>${tags.join('')}${state.orphans.has(root.id) ? '<span class="orphan-note">anchor not found in current diff</span>' : ''}</span>
    </button>`;
  }

  function renderDrawerList() {
    const host = $('#drawer-list');
    const roots = drawerRoots(state.drawer.tab);
    $('#btn-jump-first').hidden = !(state.drawer.tab === 'unresolved' && roots.length);
    if (!roots.length) {
      const what = { pending: 'No pending comments', new: 'Nothing new', unresolved: 'No unresolved threads', all: 'No comments yet' }[state.drawer.tab];
      host.innerHTML = `<div class="drawer-empty">${esc(what)}${state.currentDrawerFilterPath ? ` for ${esc(state.currentDrawerFilterPath)} <button type="button" class="link-btn btn-clear-drawer-path">clear</button>` : ''}</div>`;
      return;
    }
    const groups = [];
    let cur = null;
    for (const root of roots) {
      const a = root.anchor || {};
      const gkey = root.outdated ? 'outdated' : (!a.commit ? 'review' : a.commit);
      if (!cur || cur.key !== gkey) { cur = { key: gkey, items: [] }; groups.push(cur); }
      cur.items.push(root);
    }
    let html = state.currentDrawerFilterPath ? `<div class="drawer-group-title">path: ${esc(state.currentDrawerFilterPath)} <button type="button" class="link-btn btn-clear-drawer-path">clear</button></div>` : '';
    for (const g of groups) {
      let title;
      if (g.key === 'outdated') title = 'Outdated';
      else if (g.key === 'review') title = 'Review summaries';
      else { const c = commitMeta(g.key); title = c ? `<span class="sha">${esc(c.short_sha)}</span><span class="subject" title="${esc(c.subject)}">${esc(c.subject)}</span>` : `<span class="sha">${esc(shortSha(g.key))}</span>`; }
      html += `<div class="drawer-group"><div class="drawer-group-title">${title}</div>${g.items.map(drawerItemHtml).join('')}</div>`;
    }
    host.innerHTML = html;
  }

  function renderDrafts() {
    const host = $('#drafts-list');
    const keys = allDraftKeys().filter((k) => hasDraft(k));
    if (!keys.length) { host.innerHTML = ''; return; }
    host.innerHTML = `<div class="draft-row"><strong>Drafts (${keys.length})</strong></div>` + keys.map((k) => `<div class="draft-row" data-key="${esc(k)}"><span class="dk" title="${esc(k)}">${esc(draftLabel(k))}</span>
      <button type="button" class="link-btn btn-open-draft">Open</button><button type="button" class="link-btn btn-discard-draft">Discard</button></div>`).join('');
  }

  function draftLabel(key) {
    const p = parseLineKey(key);
    if (p) return `${shortSha(p.commit)} ${p.path}:${p.side === 'old' ? 'o' : 'n'}${p.line}`;
    if (key.startsWith('file:')) { const rest = key.slice(5); const bar = rest.indexOf('|'); return `${shortSha(rest.slice(0, bar))} ${rest.slice(bar + 1)} (file)`; }
    if (key.startsWith('commit:')) return `${shortSha(key.slice(7))} (commit)`;
    if (key === 'review:') return 'whole change (review)';
    if (key.startsWith('reply:')) return `reply to ${key.slice(6)}`;
    if (key.startsWith('edit:')) return `edit ${key.slice(5)}`;
    return key;
  }

  async function openDraft(key) {
    const p = parseLineKey(key);
    if (p) {
      await navigateTo({ sha: p.commit, path: p.path, side: p.side, line: p.line });
      await openEditor(key, { mode: 'new', anchor: { kind: 'line', commit: p.commit, path: p.path, side: p.side, line: p.line, start_line: null } });
    } else if (key.startsWith('file:')) {
      const rest = key.slice(5); const bar = rest.indexOf('|');
      await navigateTo({ sha: rest.slice(0, bar), path: rest.slice(bar + 1) });
      await openEditor(key, { mode: 'new', anchor: { kind: 'file', commit: rest.slice(0, bar), path: rest.slice(bar + 1), side: null, line: null, start_line: null } });
    } else if (key.startsWith('commit:')) {
      await navigateTo({ sha: key.slice(7) });
      await openEditor(key, { mode: 'new', anchor: { kind: 'commit', commit: key.slice(7), path: null, side: null, line: null, start_line: null } });
    } else if (key === 'review:') {
      await navigateTo({ sha: 'combined' });
      await openEditor(key, { mode: 'new', anchor: { kind: 'review' } });
    } else if (key.startsWith('reply:') || key.startsWith('edit:')) {
      const id = key.slice(key.indexOf(':') + 1);
      const c = state.comments.get(id);
      if (!c) { toast('That comment no longer exists', 'error'); delDraft(key); renderDrafts(); return; }
      const rootId = c.parent_id || c.id;
      await navigateTo({ threadId: rootId });
      await openEditor(key, key.startsWith('reply:') ? { mode: 'reply', rootId } : { mode: 'edit', id, rootId });
    }
    if (window.innerWidth < 1280) closeDrawer();
  }

  const reviewDraftKey = () => `ccr:draft:review:${repoKey()}`;
  function loadReviewDraft() {
    const d = storage.json(reviewDraftKey(), null);
    if (!d) return;
    if (d.summary) $('#review-summary').value = d.summary;
    const radio = $(`input[name=verdict][value="${cssEsc(d.verdict || 'comment')}"]`);
    if (radio) radio.checked = true;
  }
  const saveReviewDraft = debounce(() => {
    const verdict = ($('input[name=verdict]:checked') || {}).value || 'comment';
    const summary = $('#review-summary').value;
    if (!summary.trim() && verdict === 'comment') storage.del(reviewDraftKey());
    else storage.set(reviewDraftKey(), JSON.stringify({ verdict, summary }));
  }, 300);

  function renderRounds() {
    const host = $('#rounds-list');
    const rounds = (state.review && state.review.rounds) || [];
    if (!rounds.length) { host.innerHTML = '<li class="rounds-empty">No rounds submitted yet</li>'; return; }
    host.innerHTML = rounds.slice().reverse().map((r) => `<li class="round-item" data-round="${esc(r.number)}">
      <span>Round ${esc(r.number)}</span><span class="verdict ${esc(r.verdict)}">${esc(r.verdict.replace('_', ' '))}</span>${timeHtml(r.submitted_at)}
      <span class="n">${esc((r.comment_ids || []).length)} comment${(r.comment_ids || []).length === 1 ? '' : 's'}</span>
      <button type="button" class="link-btn btn-since-round" data-round="${esc(r.number)}">Changes since round ${esc(r.number)}</button>
      ${r.summary ? `<span class="summary" title="${esc(r.summary)}">${esc(r.summary)}</span>` : ''}</li>`).join('');
  }

  function renderDrawer() {
    updateDrawerCounts();
    for (const t of $$('#drawer .tab')) { const on = t.dataset.tab === state.drawer.tab; t.classList.toggle('is-active', on); t.setAttribute('aria-selected', String(on)); }
    $('#chk-this-commit').checked = state.drawer.thisCommitOnly;
    if (state.drawer.open) renderDrawerList();
    renderDrafts();
    updateBadges();
  }

  async function submitReview() {
    const btn = $('#btn-submit-review');
    if (btn.disabled) return;
    const verdict = ($('input[name=verdict]:checked') || {}).value || 'comment';
    const summary = $('#review-summary').value.trim();
    const pendingBefore = state.counts.pendingComments;
    btn.disabled = true;
    try {
      const round = await api('/api/submit', { method: 'POST', body: { verdict, summary } });
      storage.del(reviewDraftKey());
      $('#review-summary').value = '';
      $('input[name=verdict][value="comment"]').checked = true;
      const n = (round.comment_ids || []).length || pendingBefore + (summary ? 1 : 0);
      toast(`Round ${round.number} submitted · ${n} comment${n === 1 ? '' : 's'}`, 'success');
      applyReview(await api('/api/review'));
      const cs = await api('/api/comments');
      applyComments(cs.comments);
      state.version = Math.max(state.version, cs.version);
    } catch (e) {
      toast(e.message, 'error');
    } finally { btn.disabled = false; updateBadges(); }
  }

  /* ==================================================================== events */

  function onKeyDown(e) {
    const t = e.target;
    const inInput = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && inInput) {
      const form = t.closest('form.comment-editor');
      if (form) { e.preventDefault(); submitEditor(form); return; }
      if (t.id === 'review-summary') { e.preventDefault(); submitReview(); }
      return;
    }
    if (e.key === 'Escape') {
      if (inInput) {
        const form = t.closest('form.comment-editor');
        if (form) { closeEditor(form.dataset.key); return; }
        if (t.id === 'file-filter' && t.value) { t.value = ''; state.fileFilter = ''; applyFileFilter(); return; }
        t.blur();
        return;
      }
      if (!$('#help').hidden) { toggleHelp(false); return; }
      if (state.drawer.open) { closeDrawer(); return; }
      if (state.sel) { clearSelection(); return; }
      if (state.currentThread) setCurrentThread(null);
      return;
    }
    if (inInput || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!state.review) return;
    const actions = {
      j: () => nextFile(1), k: () => nextFile(-1), ']': () => nextCommit(1), '[': () => nextCommit(-1),
      n: () => nextThread(1), p: () => nextThread(-1), N: () => nextUnresolved(1), P: () => nextUnresolved(-1),
      c: commentShortcut, v: toggleViewedCurrent, x: () => { const card = currentCard(); if (card) toggleCollapseCard(card); },
      X: toggleCollapseAll, r: toggleDrawer, u: toggleViewMode, w: toggleWrap, '?': () => toggleHelp(),
    };
    const fn = actions[e.key];
    if (fn) { e.preventDefault(); fn(); }
  }

  function onMainClick(e) {
    const el = e.target;
    const hit = (sel) => el.closest(sel);
    let b;
    if ((b = hit('.btn-add-comment'))) { e.preventDefault(); const a = anchorForGutter(b); openEditor(anchorKey(a), { mode: 'new', anchor: a }); return; }
    if ((b = hit('.btn-collapse'))) { toggleCollapseCard(b.closest('.file-card')); return; }
    if (el.matches('.viewed input[type=checkbox]')) { const card = el.closest('.file-card'); setViewed(fileForPath(card.dataset.path), el.checked); return; }
    if ((b = hit('.btn-comment-file'))) {
      const card = b.closest('.file-card');
      const a = { kind: 'file', commit: state.viewSha, path: card.dataset.path, side: null, line: null, start_line: null };
      expandCard(card, fileForPath(card.dataset.path));
      openEditor(anchorKey(a), { mode: 'new', anchor: a }); return;
    }
    if (hit('#btn-comment-commit')) { const a = { kind: 'commit', commit: state.viewSha, path: null, side: null, line: null, start_line: null }; openEditor(anchorKey(a), { mode: 'new', anchor: a }); return; }
    if (hit('#btn-comment-review')) { openEditor('review:', { mode: 'new', anchor: { kind: 'review' } }); return; }
    if ((b = hit('.btn-load-anyway'))) { loadTooLarge(b.closest('.file-card'), b); return; }
    if ((b = hit('.expand-btn'))) { expandGap(b.closest('.file-card'), b); return; }
    if ((b = hit('[data-copy]'))) { copyText(b.dataset.copy, 'Copied ' + (b.classList.contains('sha-copy') ? 'sha' : 'path')); return; }
    if ((b = hit('a.sha-link, a.parent-link'))) { e.preventDefault(); navigateTo({ sha: b.dataset.sha }); return; }
    if ((b = hit('.other-views'))) { openDrawer('all', { path: b.dataset.path }); return; }
    if (hit('.btn-clear-filter')) { $('#file-filter').value = ''; state.fileFilter = ''; applyFileFilter(); return; }
    if (hit('#btn-collapse-all')) { collapseAll(true); return; }
    if (hit('#btn-expand-all')) { collapseAll(false); return; }
    if (el.matches('#chk-hide-viewed')) {
      state.hideViewed = el.checked; storage.set('ccr:hide-viewed', el.checked ? '1' : '0');
      for (const card of $$('#files .file-card')) updateCardCollapse(card, fileForPath(card.dataset.path));
      return;
    }
    const comment = hit('.comment');
    const thread = hit('.thread');
    if ((b = hit('.act-edit')) && comment) { const c = state.comments.get(comment.dataset.id); openEditor(`edit:${c.id}`, { mode: 'edit', id: c.id, rootId: c.parent_id || c.id }); return; }
    if ((b = hit('.act-delete')) && comment) { deleteComment(comment.dataset.id); return; }
    if ((hit('.act-reply') || hit('.btn-reply')) && thread) { openEditor(`reply:${thread.dataset.threadId}`, { mode: 'reply', rootId: thread.dataset.threadId }); return; }
    if ((hit('.act-resolve') || hit('.btn-resolve-thread')) && thread) { toggleResolved(thread.dataset.threadId); return; }
    if (hit('.btn-show-resolved') && thread) { state.expandedResolved.add(thread.dataset.threadId); patchThreadById(thread.dataset.threadId); return; }
    if (hit('.btn-hide-resolved') && thread) { state.expandedResolved.delete(thread.dataset.threadId); patchThreadById(thread.dataset.threadId); return; }
    if ((b = hit('.btn-cancel-comment'))) { closeEditor(b.closest('form').dataset.key); return; }
    if ((b = hit('.btn-discard-comment'))) { closeEditor(b.closest('form').dataset.key, { discard: true }); return; }
    if (hit('form.comment-editor')) return;
    if (thread) { if (state.currentThread !== thread.dataset.threadId) setCurrentThread(thread.dataset.threadId); return; }
    if (hit('td.code') && state.sel && window.getSelection().isCollapsed) { clearSelection(); return; }
    if (hit('.file-header') && !hit('button, label, input, a')) { toggleCollapseCard(hit('.file-card')); }
  }

  function onMainPointerOver(e) {
    onGutterHover(e);
    const thread = e.target.closest('.thread[data-range-start]');
    if (thread !== state.hoverThread) {
      for (const r of $$('#main tr.line.in-range-hover')) r.classList.remove('in-range-hover', 'in-range');
      if (state.sel) applySelectionClasses();
      state.hoverThread = thread;
      if (thread) {
        const card = thread.closest('.file-card');
        const side = thread.dataset.rangeSide; const lo = +thread.dataset.rangeStart; const hi = +thread.dataset.rangeEnd;
        if (card) for (const row of $$('tr.line', card)) { const ln = rowLine(row, side); if (ln != null && ln >= lo && ln <= hi) row.classList.add('in-range', 'in-range-hover'); }
      }
    }
  }

  function onMainInput(e) {
    const ta = e.target;
    if (ta.tagName !== 'TEXTAREA') return;
    const form = ta.closest('form.comment-editor');
    if (!form) return;
    autoGrow(ta);
    saveDraftDebounced(form.dataset.key, ta.value);
  }

  function onSidebarClick(e) {
    const item = e.target.closest('.commit-item');
    if (item) {
      hideTooltip();
      if (e.shiftKey) { shiftClickCommit(item.dataset.sha); return; }
      navigateTo({ sha: item.dataset.sha });
      return;
    }
    const folder = e.target.closest('.tree-folder');
    if (folder) { toggleFolder(folder); return; }
    const file = e.target.closest('.tree-file');
    if (file) navigateTo({ sha: state.compare ? state.viewSha : state.selectedSha, path: file.dataset.path }, { push: !state.compare });
  }

  function onSidebarKey(e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const item = e.target.closest('.commit-item, .tree-folder, .tree-file');
    if (!item) return;
    e.preventDefault();
    item.click();
  }

  function onTopbarClick(e) {
    const b = e.target.closest('button');
    if (!b) return;
    switch (b.id) {
      case 'btn-sidebar': toggleSidebar(); break;
      case 'btn-viewmode': toggleViewMode(); break;
      case 'btn-wrap': toggleWrap(); break;
      case 'btn-ws': toggleWs(); break;
      case 'btn-theme': cycleTheme(); break;
      case 'btn-reload': doReload(); break;
      case 'btn-review': toggleDrawer(); break;
      case 'btn-copy-link': copyText(permalink(), 'Link copied (includes the session token)'); break;
      case 'btn-help': toggleHelp(); break;
      default: break;
    }
  }

  function onDrawerClick(e) {
    const el = e.target;
    let b;
    if ((b = el.closest('.tab'))) { state.drawer.tab = b.dataset.tab; renderDrawer(); return; }
    if ((b = el.closest('.drawer-item'))) { navigateTo({ threadId: b.dataset.threadId }); return; }
    if (el.closest('#btn-drawer-close')) { closeDrawer(); return; }
    if (el.closest('#btn-mark-seen')) { markAllSeen(); return; }
    if (el.closest('#btn-jump-first')) { const first = drawerRoots('unresolved')[0]; if (first) navigateTo({ threadId: first.id }); return; }
    if (el.closest('.btn-clear-drawer-path')) { state.currentDrawerFilterPath = null; renderDrawerList(); return; }
    if ((b = el.closest('.btn-open-draft'))) { openDraft(b.closest('.draft-row').dataset.key); return; }
    if ((b = el.closest('.btn-discard-draft'))) { const k = b.closest('.draft-row').dataset.key; delDraft(k); if (state.openEditors.has(k)) closeEditor(k, { discard: true }); renderDrafts(); updateDraftDots(); return; }
    if ((b = el.closest('.btn-since-round'))) { compareSinceRound(parseInt(b.dataset.round, 10)); if (window.innerWidth < 1280) closeDrawer(); }
  }

  function onBannerClick(e) {
    const el = e.target;
    if (el.closest('.banner-close')) { el.closest('.banner').hidden = true; return; }
    const since = el.closest('.btn-since-round');
    if (since) { compareSinceRound(parseInt(since.dataset.round, 10)); $('#banner-reloaded').hidden = true; return; }
    if (el.closest('.btn-exit-compare')) exitCompare();
  }

  function setupResizer(handle, { min, max, cssVar, storeKey, fromRight, after }) {
    let startX = 0; let startW = 0;
    const app = $('#app');
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      startX = e.clientX;
      startW = parseInt(getComputedStyle(app).getPropertyValue(cssVar), 10) || min;
      handle.classList.add('is-active');
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener('pointermove', (e) => {
      if (!handle.classList.contains('is-active')) return;
      const w = clamp(startW + (fromRight ? startX - e.clientX : e.clientX - startX), min, max);
      app.style.setProperty(cssVar, w + 'px');
      if (after) after(w);
    });
    const end = () => {
      if (!handle.classList.contains('is-active')) return;
      handle.classList.remove('is-active');
      storage.set(storeKey, String(parseInt(getComputedStyle(app).getPropertyValue(cssVar), 10)));
    };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  }

  function wireEvents() {
    const main = $('#main');
    main.addEventListener('click', onMainClick);
    main.addEventListener('pointerdown', onPointerDown);
    main.addEventListener('pointermove', onPointerMove);
    main.addEventListener('pointerup', onPointerUp);
    main.addEventListener('pointerover', onMainPointerOver);
    main.addEventListener('input', onMainInput);
    let scrollPending = false;
    main.addEventListener('scroll', () => {
      if (scrollPending) return;
      scrollPending = true;
      requestAnimationFrame(() => { scrollPending = false; updateCurrentFileFromScroll(); });
    }, { passive: true });
    document.addEventListener('pointerup', () => { for (const t of $$('table.diff.sel-old, table.diff.sel-new')) t.classList.remove('sel-old', 'sel-new'); });
    document.addEventListener('submit', (e) => {
      const form = e.target;
      if (form.matches('form.comment-editor')) { e.preventDefault(); submitEditor(form); }
      else if (form.id === 'review-form') { e.preventDefault(); submitReview(); }
    });
    document.addEventListener('keydown', onKeyDown);

    const sidebar = $('#sidebar');
    sidebar.addEventListener('click', onSidebarClick);
    sidebar.addEventListener('keydown', onSidebarKey);
    sidebar.addEventListener('pointerover', (e) => { const item = e.target.closest('.commit-item'); if (item) scheduleTooltip(item); });
    sidebar.addEventListener('pointerout', (e) => { if (e.target.closest('.commit-item') && !(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.commit-item') === e.target.closest('.commit-item'))) hideTooltip(); });
    sidebar.addEventListener('focusin', (e) => { const item = e.target.closest('.commit-item'); if (item) scheduleTooltip(item); });
    sidebar.addEventListener('focusout', (e) => { if (e.target.closest('.commit-item')) hideTooltip(); });
    $('#file-filter').addEventListener('input', (e) => { state.fileFilter = e.target.value.trim(); applyFileFilter(); });

    $('#topbar').addEventListener('click', onTopbarClick);
    $('#banners').addEventListener('click', onBannerClick);
    const drawer = $('#drawer');
    drawer.addEventListener('click', onDrawerClick);
    drawer.addEventListener('change', (e) => {
      if (e.target.id === 'chk-this-commit') { state.drawer.thisCommitOnly = e.target.checked; renderDrawerList(); }
      if (e.target.name === 'verdict') { saveReviewDraft(); updateBadges(); }
    });
    $('#review-summary').addEventListener('input', () => { saveReviewDraft(); updateBadges(); });
    $('#drawer-backdrop').addEventListener('click', closeDrawer);
    $('#help').addEventListener('click', (e) => { if (e.target === e.currentTarget || e.target.closest('#btn-help-close')) toggleHelp(false); });

    setupResizer($('#sidebar-resizer'), { min: 200, max: 480, cssVar: '--sidebar-w', storeKey: 'ccr:sidebar-w', fromRight: false });
    setupResizer($('#drawer-resizer'), { min: 320, max: 720, cssVar: '--drawer-w', storeKey: 'ccr:drawer-w', fromRight: true, after: applyDrawerLayout });

    window.addEventListener('hashchange', () => { navigateFromHash().catch((e) => toast(e.message, 'error')); });
    window.addEventListener('resize', applyDrawerLayout);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') { if (state.loadedOnce) resync().catch(() => {}); startPolling(); } else stopPolling();
    });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (state.theme === 'auto') applyTheme(); });
    setInterval(refreshTimes, 60000);
  }

  boot();
})();

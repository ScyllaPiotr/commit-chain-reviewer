# ccr — Commit Chain Reviewer

`ccr` is a local, zero-dependency, GitHub-like code review UI built for agentic workflows. An agent
(Claude Code) prepares a chain of commits, runs `ccr start`, and hands you a URL. You review in the
browser — the commit chain, per-commit diffs, inline comments on lines, ranges, files or whole
commits — and click **Submit review**. The agent reads every comment in one call (`ccr wait` /
`ccr comments`), fixes the code, replies to and resolves threads from the CLI, reloads the diff, and
the loop repeats until you approve. No GitHub detour, no network, no accounts.

## Requirements

| | |
|---|---|
| Python | ≥ 3.10 with the stdlib `sqlite3` module (part of the default `python3` package on Fedora and Ubuntu) |
| git | ≥ 2.24 on `PATH` |
| Browser | any current Chromium, Firefox or Safari |

Nothing else: no pip packages, no Node, no build step. `highlight.js` is vendored under `ccr/static/vendor/`.

## Install

ccr is a **Claude Code plugin**: the checkout carries `.claude-plugin/plugin.json`, the `review` skill
(`skills/review/SKILL.md`) and the `ccr` executable in `bin/`, which Claude Code puts on PATH while the
plugin is enabled. Pick one way to make Claude Code load it (all from the repo root):

```sh
# a) simplest — a skills-directory plugin, auto-loaded in every session as ccr@skills-dir
mkdir -p ~/.claude/skills && ln -s "$PWD" ~/.claude/skills/ccr

# b) through a marketplace (the repo is its own marketplace, see .claude-plugin/marketplace.json)
claude plugin marketplace add "$PWD" && claude plugin install ccr@ccr-local

# c) just for one session
claude --plugin-dir "$PWD"
```

Check with `claude plugin details ccr`. Then, in Claude Code, say *"show me the code"* or run
`/ccr:review main..HEAD`.

To use the CLI outside Claude Code, run `bin/ccr` in place (it locates the package relative to its
own location and ignores a `ccr/` directory in the current repository), add `bin/` to PATH, or
`pip install -e .` for a `ccr` console script.

`python3 -m ccr …` works as well when the repo root is on `PYTHONPATH`.

## Quick start (for a human)

```sh
cd ~/src/myrepo
ccr start                      # range defaults to @{upstream}..HEAD, else main..HEAD
# ccr: serving /home/me/src/myrepo  (main..HEAD, 7 commits)
# ccr: url http://127.0.0.1:7777/?t=3f9a…
```

1. Open the printed URL (or run `ccr open`). On a remote machine, forward the port first:
   `ssh -L 7777:127.0.0.1:7777 host`, then open the URL locally.
2. Pick a commit in the sidebar. Hover a line and click the gutter **[+]**, or drag across the line
   numbers to comment on a range. Comment on a whole file or commit from their headers.
3. Press `r` to open the Review drawer, choose a verdict, optionally write a summary, and click
   **Submit review**.
4. `ccr comments` prints every thread as Markdown. `ccr stop` exports the review to Markdown and
   shuts the server down.

## CLI reference

Invocation: `ccr <command> [options]` (also `python3 -m ccr` or `bin/ccr`). Without `--repo`, ccr
uses the git toplevel of the current directory and fails with exit 1 outside a repository.

### Global options

| Option | Meaning |
|---|---|
| `--repo PATH` | Repository to act on (default: toplevel of cwd). Linked git worktrees are separate sessions. |
| `--url URL --token T` | Talk to a specific server, bypassing session discovery (env `CCR_URL`, `CCR_TOKEN`). |
| `--json` | Machine-readable output where supported. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error — bad arguments, git failure, server crash, port in use, database conflict, … |
| 2 | `ccr wait` timed out without a new round |
| 3 | no running session for this repo (stale session files are deleted on the way) |

### Range specs (`--range SPEC` / `-n N`)

| Spec | Meaning |
|---|---|
| `A..B` | commits reachable from `B` but not from `A`; an empty side means `HEAD` (`main..`, `..HEAD`) |
| `A...B` | same, but the base is `merge-base(A, B)` |
| `A` | `A..HEAD` |
| `-n N` | `N` first-parent steps back from `HEAD` (side branches of merges included); `N` ≥ history depth → the whole history |
| *(omitted)* | `@{upstream}..HEAD` when it has commits, else the first of `main`, `master`, `origin/main`, `origin/master`, `origin/HEAD` that does |

The spec is stored in a pinned form — `-n N` becomes `<base sha>..HEAD`, `A...B` becomes
`<merge-base sha>..B`, a bare `A` becomes `A..HEAD` — so a reload re-resolves the same base while
`HEAD` moves and never drops reviewed commits. A base that is not an ancestor of head is replaced
by their merge-base, and `ccr start` prints `ccr: note: base <sha> is not an ancestor of head; using
merge-base <sha>`. Empty ranges without `--worktree` and ranges above 2000 commits are refused.

### Commands

| Command | Description |
|---|---|
| `ccr start [--range SPEC \| -n N] [--worktree \| --no-worktree] [--first-parent] [--port N] [--db PATH] [--log FILE] [--open] [--idle-timeout S] [--cover FILE]` | Start a background server and print the URL. `--cover FILE` sets the cover letter (the Markdown description of the whole change, shown above **All changes**). When a session is already running, reload it with the given options instead (`ccr: reusing running session`). `--worktree` adds uncommitted changes (staged, unstaged and untracked) as a pseudo-commit; `--first-parent` follows only first parents through merges; `--open` launches a browser; `--idle-timeout` (default 86400 s, 0 = never) stops a server nobody talks to. `--json` prints the session record plus the review counts. A bad range fails immediately with the git message; a crashing server prints `ccr: server exited with code N — last log lines:` and the log tail. |
| `ccr serve …` | Run the server in the foreground. Same options as `start`, plus `--token T` (visible in `ps` — tests only), `--verbose` (request log) and `--db-force`. |
| `ccr stop [--all] [--keep-db] [--purge]` | Export the review to Markdown, shut the server down, delete the session file and the default database. `--keep-db` keeps the SQLite file; `--purge` also removes exports and logs; `--all` does it for every live session. |
| `ccr status [--json]` | URL, range (+ note), commit count, comment counts, rounds with the last verdict, whether the UI is connected / last seen, log and db paths. |
| `ccr sessions [--json]` | Every session (repo, url, range, alive?, started_at); stale ones are deleted. Exit 3 when there is none. |
| `ccr logs [-n N] [-f]` | Tail (or follow) the server log. |
| `ccr open` | Open the session URL in the default browser. |
| `ccr cover (TEXT \| --file F \| -)` | Set or replace the cover letter of the running review; open pages update live. |
| `ccr reload [--range SPEC \| -n N] [--worktree \| --no-worktree] [--first-parent \| --no-first-parent]` | Re-extract the chain; omitted options keep their values, no options re-resolves the pinned spec so new commits appear. Prints `ccr: N commits (was M), +a −r, K comments remapped, J now outdated`, a warning when reviewed commits left the range, and one line per thread that is now outdated. |
| `ccr comments [--pending \| --submitted \| --round N \| --all] [--unresolved] [--unanswered] [--author user\|claude] [--commit SHA] [--path P] [--outdated \| --no-outdated] [--context N] [--no-snippets] [--json]` | Print threads as Markdown (format below). Filters select whole threads — a thread matches when its root or any reply does; matching comments are marked `★`. `--unanswered` = unresolved, non-outdated threads whose last comment is by the user. Defaults: `--all --context 3`. No match → `ccr: no comments match` (exit 0). |
| `ccr wait [--since-round N] [--since-version V] [--timeout S] [--any] [--json]` | Block until a round numbered > `N` exists (default `N` = the round count at call time), then print `ccr: round n — verdict — k new comments in j threads` followed by the threads touched in that round (earlier comments as context, new ones marked `★ new in round n`). Default timeout 590 s (0 = forever); on timeout stderr gets `ccr: no new round after S s (rounds: R, pending unsubmitted: P, version: V)` and the exit code is 2. `--any` returns on any change (`version > V`, default `V` = version at call time) and prints `ccr: version V→W · pending P · unresolved U · rounds R` plus the threads touched since the call. Exit 3 when the server is gone. After 30 s with no browser seen it prints `ccr: UI not opened yet` once. |
| `ccr reply ID (BODY \| --file F \| -) [--resolve] [--as claude\|user] [--force]` | Reply to a thread; `--resolve` resolves it after a successful reply. An identical reply by the same author is refused unless `--force`. |
| `ccr reply --batch (FILE \| -) [--json]` | Post many replies at once, from JSON (`[{"id","body","resolve"?}, …]`) or Markdown (`## <id> [resolve]` headings, each followed by its body). Prints `<id>: replied[, resolved]` or `<id>: ERROR …` per item, continues on error, exit 1 if any failed. |
| `ccr comment (--review \| --commit REV [--path P [--line N [--side new\|old] [--start-line M]]]) (BODY \| --file F \| -) [--as claude\|user]` | Create a comment on the whole review, a commit, a file, a line or a range (`--start-line M` < `N`). `REV` is a listed sha, a short sha, `combined`, `worktree`, or any rev that resolves to a listed commit. `--side` defaults to `new`. |
| `ccr resolve ID [ID…]` / `ccr unresolve ID [ID…]` | Set or clear the resolved flag of threads. |
| `ccr edit ID (BODY \| --file F \| -)` | Replace a comment body (shown as `edited` in the UI). |
| `ccr delete ID [ID…] [--cascade]` | Delete comments; a root with replies requires `--cascade`. |
| `ccr move ID --commit REV [--path P [--line N [--side S] [--start-line M]]]` | Re-anchor a comment (recorded as `moved`). |
| `ccr export [--json \| --md] [-o FILE]` | Dump the whole review — rounds and every thread, outdated ones included — as Markdown or JSON. `-o FILE` is created with mode 0600. |

Bodies given as `-` are read from stdin. `ccr comment` and `ccr reply` write as author `claude` by
default — the CLI is the agent's side of the conversation; pass `--as user` to write as the human.

### Comment Markdown

`ccr comments`, `ccr wait` and `ccr export --md` share one renderer designed for an LLM reader:
deterministic order (chain order, then path, then line), explicit ids, a numbered code snippet around
every anchor (`--context N` rows, anchored rows prefixed with `>`), and a HEAD-relative location —
`→ HEAD path:line` with `(moved)`, `(changed near)`, `(deleted)`, `(file deleted)` or `(live)` when
applicable — so a reply can be acted on without opening the browser.

````markdown
# Review comments — repo-name (main..HEAD) — 5 threads (3 pending, 2 unresolved, 1 unanswered)

## Rounds
- Round 1 · request_changes · 2026-09-03T13:50:12Z · 4 comments · "Overall looks good, two nits."  [id: q8x1zz]

## Commit 9fceb02d3a — "Add retry to fetcher"

### src/fetcher.py

#### [id: k3f9a2] user · new:11 → HEAD src/fetcher.py:14 (moved) · pending · unresolved · 1 reply · last: claude
```diff
  10   10    x = 1
  11        -y = 2
>      11   +y = 3
       12    return y
```
Why not use the existing backoff helper here?

  ↳ [id: p0o9i8] claude · 2026-09-03T13:52:10Z · R1
  Good catch — switched to `backoff.retry()` in 1a2b3c4.

### (file) src/other.py
#### [id: …] user · file · pending · unresolved · 0 replies · last: user
…

## All changes (combined)
## Uncommitted changes
## Outdated (anchored to commits no longer in the range)
````

`--json` prints `{"review": {…}, "rounds": […], "comments": […], "threads": [{"root", "replies",
"last_author", "answered"}, …]}` with the raw comment objects (`id`, `parent_id`, `author`, `body`,
`state`, `round`, `resolved`, `anchor`, `snippet`, `outdated`, `moved_from`, `head_location`).

## The browser UI

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ ⎇ repo · branch   main..HEAD · 7 commits · +worktree  [Unified] [Wrap] [Hide ws] [☾] [⟳] [Review ●3 · 4] [?] │
├────────────┬─────────────────────────────────────────────────────────────────────────────┤
│ COMMITS    │ Header card: subject · body · author · date · sha (copy) · +40 −12  [💬]      │
│ ● All      │ 12 / 40 files viewed ▓▓▓░░  [Collapse all] [Expand all] [☐ Hide viewed]        │
│ ○ 9fceb02 •│ ┌ src/fetcher.py  M  +10 −2   · 2 threads in other views   [☐ Viewed] [💬] [▾] ┐│
│ ○ 1a2b3c4  │ │ @@ -10,7 +10,9 @@ def foo(self):                      [⤒ 20] [expand all] [⤓ 20]│
│ ○ Worktree │ │ 10  10     x = 1                                                           │
│────────────│ │ 11        - y = 2                                                    [+]   │
│ FILES  [🔍]│ │     11    + y = 3                                                          │
│ ▾ src      │ │ ┌ thread ────────────────────────────────────────────────────────────┐    │
│   fetcher  │ │ │ U user · 2 min ago · Pending          [Edit][Delete][Reply][Resolve]│    │
└────────────┴─┴─┴───────────────────────────────────────────────────────────────────────────┘
                                                      [Review drawer slides in from the right]
```

**Top bar** — repo and branch, the range (hover for the merge-base note), commit count, then:
Unified/Split, Wrap, Hide whitespace, theme (auto → light → dark), ⟳ reload, **Review** (badges:
pending comments · unresolved threads), Copy link (the link carries the token, so it works in another
browser), `?` help, sidebar toggle.

**Sidebar** — the commit chain: **All changes** (`combined`, the whole range as one diff) first, the
real commits oldest → newest, and **Uncommitted changes** (`worktree`: staged + unstaged + untracked
vs `HEAD`) last when started with `--worktree`. Each item shows the short sha, subject, author
avatar, age, `+N −M`, thread badges (yellow pending, red unresolved) and a `•` dot for commits that
were not part of the last submitted round. Hover for the full message. **Shift+click** two commits
for a compare view of that sub-range (read-only: comments are disabled). Below it, the file tree of
the selected commit with status letters, counts, viewed ticks and a filter box that also hides
non-matching files in the main pane.

**Main pane** — the commit header card (subject, body, author, sha click-to-copy, stats, *comment on
this commit*; in **All changes** also the cover letter set by the agent and *Comment on the whole
change*), a files toolbar (`12 / 40 files viewed`, Collapse/Expand all, Hide viewed), and one
card per file with a sticky header: path (renames as `old → new`), status, mode changes, `+N −M`,
"N threads in other views", a **Viewed** checkbox (remembered per blob pair — it unticks itself when
the file changes), *comment on this file*, and collapse. Diffs come with syntax highlighting,
word-level change marks, hunk-gap expansion (`⤒ 20`, `⤓ 20`, expand all, expand to end), CRLF and
truncation markers, and "Load anyway" for very large files. Merge commits are diffed against their
first parent.

### Comments

* **Where**: a line (gutter `[+]` on hover, or the selected line with `c`), a range (drag over the line
  numbers, or Shift+click to extend, then `[+]`/`c`), a file (header button), a commit (header card),
  or the whole change — the **Comment on the whole change** button under the cover letter in **All
  changes** (the submit-time summary is such a comment too). Ranges are single-sided: in unified view a
  context line anchors to the new side, a deleted line to the old side; click the old number of a
  context line to anchor to the old side.
* **What**: Markdown — paragraphs, `code`, fenced blocks, bold, italic, `https://` links, lists,
  quotes. A 7–40 hex sha of a listed commit becomes a link to that commit. Drafts survive a reload of
  the page; Ctrl/⌘+Enter posts, Esc keeps the draft, Discard deletes it.
* **Pending vs submitted**: a new comment is **Pending**. Pending comments are *already visible to the
  agent* — `ccr comments` lists them and `ccr wait --any` wakes on them — so you can also just tell the
  agent "done". **Submit review** bundles all pending comments into a numbered **round** with a
  verdict (Comment / Approve / Request changes) and an optional summary, and wakes `ccr wait`. Later
  comments start a new pending set. Submitting with no pending comments *and* no summary is allowed
  only with the verdict *Approve*; a summary alone is enough for any verdict.
* **Threads**: one level of replies. Edit, Delete (a root with replies deletes them too), Reply,
  Resolve/Unresolve. Resolved threads collapse to one line. Comments by the agent appear live with a
  **New** dot and a toast — *Claude replied to N threads (k resolved) — Show*; `Mark all seen` clears
  the dots.
* **Drawer** (`r`): tabs **Pending / New / Unresolved / All**, *This commit only*, threads grouped by
  commit → path → line with an **Outdated** group last, a Drafts line, the verdict radios, the summary
  box, **Submit review (N)** and the list of rounds, each with a **Changes since round N** link.

### Reload, outdated and moved

Both the ⟳ button and `ccr reload` re-extract the chain without restarting the server. Comments are
never lost:

* A comment whose commit is still in the range stays where it is.
* When the commit was amended or rebased (same subject at the same position in the chain, or a
  unique commit with that subject), the comment is **re-anchored** to the new sha — for line
  comments only when the same line text is found in the file (a unique match, or the nearest one
  within ±20 lines of the old position) — and tagged **moved**.
* Otherwise it becomes **outdated**: kept in the database, listed by `ccr comments` under
  *Outdated*, and shown only in the drawer.
* After a reload a banner reports `+a −r commits, K comments remapped, J now outdated` and offers
  **Show changes since round N**: a compare view of everything the agent pushed since you last
  submitted. The `•` dot in the sidebar marks the same thing per commit.
* The UI long-polls the server, so it updates by itself; you never need to press F5. If the tab does
  reload, the token is kept in `localStorage` for that port.

### Keyboard shortcuts

Ignored while typing in an editor; `?` shows this list in the UI.

| Key | Action |
|---|---|
| `j` / `k` | next / previous file |
| `]` / `[` | next / previous commit |
| `n` / `p` | next / previous thread in the current view |
| `Shift+N` / `Shift+P` | next / previous **unresolved** thread across the whole chain |
| `c` | comment on the selected line or range (reply when a thread is focused) |
| `v` | toggle *Viewed* on the current file |
| `x` / `Shift+X` | collapse / expand the current file / all files |
| `r` | open / close the Review drawer |
| `u` | toggle Unified / Split |
| `w` | toggle line wrapping |
| `?` | help |
| `Esc` | cancel the focused editor (draft kept), else close help → drawer → selection |
| `Ctrl`/`⌘`+`Enter` | post the focused comment or reply |
| `Shift`+click (commits) | select a commit range → compare view |
| `Shift`+click (line numbers) | extend the line selection |

## The agent loop

This is what the `review` skill (`skills/review/SKILL.md`, invoked as `/ccr:review` or by asking to see the code) makes Claude Code do; you can drive it
by hand the same way.

```sh
ccr start --repo /abs/repo --range main..HEAD --worktree      # 1. start — prints the URL
ccr comment --repo /abs/repo --commit 9fceb02 --path src/fetcher.py --line 11 "Heads-up: …"   # optional
ccr wait --repo /abs/repo --since-round 0 --timeout 590      # 2. block until round 1 is submitted (exit 2 = try again)
ccr comments --repo /abs/repo --unanswered                   # 3. every thread still waiting for an answer
git commit --fixup=9fceb02 …                                 # 4. fix — new commits, no amend, so anchors stay valid
ccr reply --repo /abs/repo --batch - <<'EOF'                 # 5. answer every thread in one call
## k3f9a2 [resolve]
Switched to `backoff.retry()` in 1a2b3c4.
## m7q2rt
Kept the explicit loop — the helper has no jitter. Shall I add it there instead?
EOF
ccr reload --repo /abs/repo                                  # 6. the UI picks the new commits up live
ccr wait --repo /abs/repo --since-round 1 --timeout 590      # 7. repeat until a round says "approve"
ccr stop --repo /abs/repo                                    # 8. only when the human asks — exports Markdown first
```

* `ccr start` reuses a running session (and reloads it) instead of starting a second server.
* `ccr wait` is designed for a 10-minute tool timeout: `--timeout 590` returns exit 2 just before it,
  and the agent simply calls it again. After 30 s without a browser it prints `ccr: UI not opened yet`.
* `--since-round N` makes the wait robust: a round submitted while the agent was busy is still returned.
* The URL contains the session token — that is what authorises the browser. Hand it to the human, do
  not paste it into commit messages or issues.
* **Over SSH** the agent cannot open a browser for you; forward the port and open the URL locally:
  `ssh -L PORT:127.0.0.1:PORT host` (the port is in the URL). The Host/Origin checks allow this
  because the browser still talks to `127.0.0.1`.
* Fixup commits (`git commit --fixup=<sha>`) instead of `--amend`/rebase during the review keep every
  existing comment anchored and let you see exactly what changed via **Changes since round N**.
  Squash after approval.

## Security model

* The server binds **127.0.0.1 only** and refuses requests whose `Host` (and, when present,
  `Origin`/`Referer`) is not `127.0.0.1`, `localhost` or `[::1]` — ports are not compared, so SSH port
  forwarding works. Cross-site fetches are rejected; there are no CORS headers.
* Every `/api/*` call must carry the per-process random token in the `X-CCR-Token` header. The URL's
  `?t=` is only how the browser receives the token on the first page load; the page immediately
  stores it in `localStorage` and strips it from the address bar. Without a valid token the UI shows
  *No valid session token for this tab*.
* The token never appears on a command line: `ccr start` passes it to the server via the environment
  (`CCR_SERVE_TOKEN`), and the server redacts it from its own log. The CLI reads it from the session
  file. Only `ccr serve --token T` (meant for tests) puts it into `ps`.
* Strict CSP (`default-src 'none'`, no inline scripts or styles), `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `nosniff`, request-body limits (1 MiB).
* Session files live under `${CCR_SESSION_DIR:-~/.cache/ccr/sessions}`; the directory is `0700` and is
  refused when it is owned by someone else or group/world-writable. Session, log and database files
  are `0600`. Exports written with `-o` are `0600` too.
* `ccr stop` first writes a Markdown export to the session directory, then shuts the server down and
  deletes the session file and the default database. It only signals a process that is really the
  session's `ccr serve`. `--keep-db` keeps the database; `--purge` also removes exports and logs.
* File access from the browser is confined to the review: `/api/file` only serves blobs of revisions
  that occur in the current review, paths must be relative, `..`-free and part of the review, and
  worktree reads cannot escape the repository. Static files are served from the package directory
  only. ccr never modifies the working tree, refs or index contents (`GIT_OPTIONAL_LOCKS=0`).

## Data and persistence

* One session per repository realpath (linked worktrees are separate sessions), keyed by
  `sha1(realpath)[:16]`. Files in the session directory: `<key>.json` (pid, port, token, url, repo,
  range, started_at, log, db), `<key>.log`, `<key>.sqlite`, `<key>.lock`, and exports
  `<key>-<YYYYmmdd-HHMMSS>.md`.
* Comments and rounds live in **SQLite** in that directory. A crashed or killed server loses nothing:
  the next `ccr start` for the same repo reopens the database and keeps counting. The data is
  *session-limited*: `ccr stop` exports it to Markdown and deletes it.
* `--db :memory:` keeps everything in RAM (gone when the server exits); `--db FILE` uses an explicit
  file. A file created for another repository is refused (`db was created for <path>; pass --db-force
  to reuse` — `--db-force` is a `ccr serve` option); a second server on the same file fails with
  `db in use`.
* Diffs are never stored — they are re-extracted from git on start and reload and cached in memory.
* `ccr export --md` / `--json` dumps everything at any time, including outdated threads.
* The browser keeps its own convenience state in `localStorage`: the token (per port), theme,
  the whitespace toggle, viewed files (per blob pair, pruned after 30 days), collapsed folders,
  sidebar/drawer widths, drafts, and the "seen" marker for New dots.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `ccr: no running session for <repo>` (exit 3) | Nothing is serving this repo — or you are in a different worktree/directory. Run `ccr sessions` to see live ones, pass `--repo PATH`, or `ccr start`. A dead pid or a server that no longer answers counts as no session; its stale files are cleaned up automatically. |
| UI says *Disconnected — retrying…*, then *Server not responding — it may have been stopped* | The server exited, was stopped, or hit its idle timeout. `ccr status` exits 3 when it is gone; `ccr logs` shows why. Start it again with `ccr start` — comments are in the database and come back — and open the freshly printed URL (the token is new). |
| UI shows *No valid session token for this tab* | The server was restarted with a new token. Run `ccr status` (or `ccr open`) and open the printed URL again. |
| `ccr: port N in use` (exit 1) | Only happens with an explicit `--port`. Drop the flag (ccr picks a free port near its default) or choose another one. |
| `ccr: server exited with code N — last log lines:` | `ccr start` shows the tail of the server log; `ccr logs -n 100` shows more. Typical causes: `db was created for <path>; pass --db-force to reuse`, `db in use`, `session dir <d> is not private`. |
| `cannot infer a range; pass --range or -n` / `range X..Y is empty` | No upstream or `main`/`master` to compare against, or the range has no commits. Pass `--range base..HEAD`, `-n N`, or `--worktree` for uncommitted work. |
| `ccr wait` exits 2 (`ccr: no new round after 590 s …`) | Just no round yet. Run it again; `ccr status` tells you whether the UI is connected and when it was last seen. If it printed `ccr: UI not opened yet`, the user has not opened the URL (over SSH: is the port forwarded?). |
| `ccr wait` exits 3 (`ccr: server gone`) | The server died while waiting; the stale session file has been removed. `ccr logs`, then `ccr start` again. |
| A comment vanished from the diff | Look in the drawer: it is either **outdated** (its commit left the range) or its anchor is not in the current diff ("anchor not found"). `ccr comments --outdated` lists them; `ccr move ID --commit …` re-anchors one by hand. |
| Whitespace-only or huge files show no diff | *Hide whitespace* removed every hunk (note shown), or the file is above the size cap — click **Load anyway**. |
| Works locally, not through SSH | Forward the port from the URL: `ssh -L PORT:127.0.0.1:PORT host` and open `http://127.0.0.1:PORT/?t=…` in the local browser. Do not change the host to anything but `127.0.0.1`/`localhost`. |

Log files: `ccr logs` (or the path shown by `ccr status`). Start the server in the foreground with
`ccr serve --verbose` to see every request.

## Design notes

The complete behavioural specification is [SPEC.md](SPEC.md) — data model, git extraction rules,
store semantics, HTTP API, UI contract and test plan. The main choices in one breath:

* **Standard library only** on the server (`http.server`, `sqlite3`, `subprocess`) and vanilla ES2020
  in the browser; the only vendored asset is `highlight.js`.
* **git is the source of truth for diffs**; ccr stores only comments and rounds. Every diff is a
  single `git diff --raw -z -p` invocation parsed into plain dicts; merges are diffed against their
  first parent; range specs are stored in a pinned form so a reload never loses reviewed commits.
* **Comments survive history rewrites** through subject-based re-anchoring with line-text matching,
  and are otherwise kept as *outdated* rather than deleted.
* **Local-only by construction**: loopback bind, header token, Host/Origin checks, strict CSP,
  private session files — no configuration needed to be safe.
* **Optimised for an LLM reader**: `ccr comments` emits deterministic, id-tagged Markdown with code
  context and HEAD-relative locations, and `ccr reply --batch` answers a whole round in one call.

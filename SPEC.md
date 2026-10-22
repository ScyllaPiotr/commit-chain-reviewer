# ccr — Commit Chain Reviewer — Specification

How ccr works and why: what each part does and guarantees, and the decisions the code does not explain. Read it
before changing ccr; the exact git flags, SQL, selectors and messages are in the code (but see 8 for the messages
the agent relies on).

ccr is a local code-review tool for agentic workflows. An agent (Claude Code) prepares a chain of commits, starts
ccr and hands the user a URL. The user reviews in a GitHub-like page — the chain, per-commit diffs, comments on
lines, files, commits and the whole change — and **Submit** makes the pending comments a round. The agent reads them
from the CLI (`ccr comments`, `ccr wait`), fixes the code, answers and resolves threads, reloads, and the loop
repeats.

**PR mode** (10) points the same page at someone else's GitHub pull request: the agent changes no code, and a
comment is either a question it answers or a GitHub comment it checks and posts verbatim into the user's pending
review, which the user submits on GitHub.

Constraints:

* **Python 3.10+ standard library only** for the server and CLI; vanilla ES2020 in the browser, no build step;
  highlight.js vendored in `ccr/static/vendor/`.
* **SQLite** through the stdlib `sqlite3` (default on Fedora and Ubuntu), in the private session directory: a
  killed server or a `ccr stop` loses nothing, and the next `ccr start` resumes the review. A stopped session's
  database is pruned a week after its last change (6.2), at once with `ccr stop --purge`; `ccr restore` brings a
  review back from its export (6.4). `--db :memory:` or `--db FILE` choose another store.
* **Local only**: binds `127.0.0.1`, and every `/api/*` call carries a per-process random token.
* Any git repository (git ≥ 2.24) and any range, uncommitted work included.
* ccr never changes the working tree, refs or index contents. Diffing the worktree may let git refresh the stat
  cache in `.git/index` (`GIT_OPTIONAL_LOCKS=0` minimises that); "Since your last review" writes unreferenced
  objects (3.2).

---

## 1. Repository layout

```
README.md, LICENSE, docs/   overview, MIT licence, the README's screenshots
SPEC.md                     this file
pyproject.toml              package "ccr", console script ccr = ccr.cli:main, no dependencies
bin/ccr                     shim running the checkout's ccr without an install (the checkout first on sys.path,
                            so a reviewed repository's own ccr/ directory cannot shadow it)
ccr/
  cli.py                    argparse CLI (6)
  server.py                 HTTP server: routing, authentication, static files (5)
  gitx.py                   git subprocesses and the diff parser, returning plain dicts (3)
  github.py                 PR mode: pull request references, `gh api` calls (10)
  store.py                  ReviewStore: SQLite, comments, rounds, reviews, the version counter (4)
  session.py                session files, background start/stop, ports, tokens (6.1)
  render.py                 Markdown and JSON of comments for `ccr comments|wait|export` (6.3)
  restore.py                an export read back for `ccr restore` (6.4)
  static/                   index.html (no inline scripts or styles), theme.js (pre-paint theme), app.js (all UI
                            logic, 7), style.css, favicon.svg, vendor/ (highlight.js, BSD-3-Clause)
.claude-plugin/             plugin manifest (bin/ goes on PATH while enabled) and a one-plugin marketplace
skills/review-commit-series/SKILL.md   the agent loop (8)
tests/                      pytest suite and the headless-Chromium driver (9)
```

---

## 2. Vocabulary & data model

Timestamps are ISO-8601 UTC with second precision and a `Z` (`2026-09-03T13:45:00Z`). JSON keys are `snake_case`;
clients ignore unknown keys.

### 2.1 Revisions and pseudo-commits

A review shows a **range** `base..head` as a chain of commits, oldest first, plus pseudo-commits. Each is a **view**:

| `sha`         | View                                                                  | Present when |
|---------------|-----------------------------------------------------------------------|--------------|
| `<40/64-hex>` | a real commit, diffed against its **first parent** (a root: the empty tree) | always |
| `combined`    | "All changes": `git diff base head`                                   | always, even for one commit |
| `worktree`    | "Uncommitted changes" vs `HEAD`: staged, unstaged and **untracked**   | `--worktree` |
| `since`       | "Since your last review": what changed since an earlier review         | `--since REV` |

**Since your last review** (a re-review). `--since R` is the commit an earlier review was submitted on (GitHub's
`PullRequestReview.commit.oid`), `--since-at T` when. With `B` = `range.base` and `H` = `range.head`, `old_base =
merge-base(R, B)` and `tree = rebuild_tree(old_base, B, R)` re-applies the reviewed version onto the current base. The
view is `diff(tree, H)`: what the author changed since the review, while whatever the new base brought in cancels out. A
path that conflicts in the rebuild is compared with `R` itself (its `old_rev` is `R`), so upstream changes may show in
it; without a shared base (or with no base) every path of All changes is, and only those. It is recomputed on every
load, as `B` and `H` move. Its new side is `H`, so comments there behave as in "All changes" (they project, and can be
GitHub comments, 10.2). Its old side, the version reviewed, appears in no other view: comments there stay in this view,
and a GitHub comment there goes on those lines at the head while they are all still there, else on the removed content
(10.2).

**Range specs** (`--range SPEC` or `-n N`):

* `A...B` → `base = merge-base(A, B)`, `head = B`; `A..B` → `base = A`, `head = B`; a bare `A` → `A..HEAD`. An empty
  side means `HEAD`.
* `-n N` → `HEAD~N`, counting first-parent steps (the chain may hold more commits when merges are in it); the whole
  history when `N` is at least HEAD's first-parent count.
* Each side must name a commit; a spec starting with `-` or holding whitespace or NUL is refused.
* A base that is not an ancestor of the head is replaced by their merge base, with `range.note` saying so; no common
  ancestor is an error.
* **Pinned spec**: the resolved spec keeps the base fixed (`-n N` → `<base sha>..HEAD`, `A` → `A..HEAD`), so a
  reload without `--range` never drops reviewed commits while `HEAD` moves on. `range.given` keeps the user's text.
* Without a spec: the first of `@{upstream}`, `main`, `master`, `origin/main`, `origin/master` and `origin/HEAD`
  that has commits in `..HEAD`, else an error asking for `--range` or `-n`.
* An empty range without `--worktree` is an error, and so is one of more than 2000 commits.

The chain is `git log --reverse --topo-order base..head`, merges included and flagged; `--first-parent` is an
option.

### 2.2 CommitMeta

```jsonc
{
  "sha": "9fceb02…",               // or "combined" / "worktree" / "since"
  "short_sha": "9fceb02d3a",       // 10 chars; the name itself for pseudo-commits
  "kind": "commit",                // "commit" | "combined" | "worktree" | "since"
  "parents": ["…"],                // [] for pseudo-commits and roots
  "is_merge": false,
  "shallow_boundary": false,       // a parentless commit at a shallow clone's boundary
  "author": {"name": "…", "email": "…"},   // no mailmap
  "author_date": "…Z", "commit_date": "…Z",  // null for pseudo-commits
  "subject": "…", "body": "…",     // first line of the message, and the rest (≤ 64 KiB)
  "stats": {"files": 3, "additions": 40, "deletions": 12},
  "files": [FileStat],             // in git's order
  "comment_count": 2               // root comments on this sha, not outdated
}
```

`FileStat`: `path`, `old_path` (renames and copies only), `status` (`A M D R C T`; untracked files are `A`,
conflicted ones `M` with the markers in their content), `score` (similarity for R/C), `additions`, `deletions`
(0/0 for binary), `binary`, `old_mode`/`new_mode` (null for an absent side; `120000` symlink, `160000` submodule) and
`old_blob`/`new_blob` (null for an absent side and for a worktree file).

### 2.3 CommitDiff (`GET /api/commits/{sha}`, `GET /api/compare`)

A CommitMeta whose `files` are `FileDiff`s: a FileStat plus

```jsonc
{
  "lang": "python",                // highlight.js id guessed from the file name, or null
  "old_rev": "…", "new_rev": "…",  // what /api/file takes, see below
  "too_large": false, "reason": null,   // hunks omitted; "file" or "response" cap
  "line_count": 123, "hunk_count": 4,   // untrimmed, always present
  "ws_only": false,                // ?ws=ignore removed every hunk of a changed file
  "hunks": [{"old_start": 10, "old_count": 7, "new_start": 10, "new_count": 9, "section": "def foo(self):",
             "lines": [{"t": "ctx", "o": 10, "n": 10, "s": "    x = 1"},
                       {"t": "del", "o": 11, "n": null, "s": "    y = 2", "cr": true},   // a \r was stripped
                       {"t": "add", "o": null, "n": 11, "s": "    y = 3", "nonl": true}]}] // no newline at EOF
}
```

| kind     | `old_rev`                        | `new_rev`    |
|----------|----------------------------------|--------------|
| commit   | `parents[0]`, null for a root    | `sha`        |
| combined | `range.base` (null if none)      | `range.head` |
| worktree | `HEAD` at extraction             | `"worktree"` |
| compare  | `base` (null: the empty tree)    | `head`       |

The UI asks `/api/file` for no null rev, no submodule side, not the new side of a `D` nor the old side of an `A`;
an R/C file's old side goes by `old_path`.

Line texts are decoded as UTF-8 with replacement, one trailing `\r` stripped and recorded as `cr`; lines over 20000
characters are cut and marked `trunc`; tabs stay. A file of more than 5000 hunk lines, or with a line over 20000
characters, is sent without hunks (`too_large`, `reason: "file"`); a response of more than 30000 lines drops the
largest files first (`reason: "response"`). `?full=1` lifts both caps. The store keeps untrimmed diffs: trimming
happens on the way out, and anchors and snippets always use the full data.

### 2.4 Comment

```jsonc
{
  "id": "k3f9a2",                  // 6 base36 chars, unique per database
  "parent_id": null,               // a reply carries its root's id: threads are one level deep
  "author": "user",                // "user" | "claude" | "github" (mirrored from the pull request, 10.5)
  "body": "…",                     // Markdown, non-empty, ≤ 64 KiB
  "created_at": "…Z", "updated_at": "…Z",   // later updated_at ⇒ "edited"
  "state": "pending",              // "pending" | "submitted"; Claude's comments are created submitted
  "round": null,                   // set on submit; Claude's: the round count at creation, maybe 0
  "resolved": false,               // roots only
  "anchor": Anchor,                // a reply always has its root's
  "snippet": "    y = 3",          // the anchored lines at creation (4.2), "" otherwise
  "moved_from": null,              // {"commit", "line"} after re-anchoring or `ccr move`
  "outdated": false,               // computed on read (4.3)
  "github": null,                  // PR mode (10.1)
  "head_location": {…}             // only with ?locate=1, on root line comments (4.5)
}
```

`Anchor`: `kind` (`line` | `file` | `commit` | `review`), `commit` (a sha or pseudo name; null only for `review`),
`path` (the FileDiff `path` — the new name of a rename, the old one of a deletion; an `old_path` given is normalised
to it), and for `line` only `side` (`old` or `new`), `line` (the end line) and optional `start_line` (< `line`; a
range stays on one side). Line numbers count in the file the side shows: for a commit `new` is the commit and `old`
its first parent, for `combined` the head and the base, for `worktree` the working tree and `HEAD`.

### 2.5 Round

`{number, submitted_at, verdict, summary, base, head, commit_shas, comment_ids}`. A round is the batch of comments
submitted together; the UI always sends verdict `comment` and no summary (rounds carry no verdict). `base`, `head`
and `commit_shas` record the chain at submit time, so the sidebar can mark commits added since. `comment_ids` is
computed: the comments with that round. A non-empty `summary` becomes a review-level user comment of the round. A
submit with nothing pending and no summary is refused unless the verdict is `approve`.

### 2.6 Review (`GET /api/review`)

```jsonc
{
  "repo": {"path": "/abs/path", "name": "…", "branch": "feature/x", "bare": false},   // branch null when detached
  "range": {"spec": "main..HEAD", "given": "-n 5", "base": "…", "head": "…", "note": null, "first_parent": false},
  "review": {"id": 3, "started_at": "…Z", "resumed": true, "previous": null},   // which review is served (4.6)
  "options": {"worktree": true},
  "cover": "…",                    // the cover letter, Markdown; "" when none
  "pr": null, "since": null,       // PR mode (10.1); "Since your last review" (2.1)
  "commits": [CommitMeta],         // "since" first when open, then "combined", the chain, "worktree" last
  "version": 17, "generation": 2, "loading": false, "now": "…Z",
  "counts": {"pending": 3, "submitted": 5, "unresolved": 4, "total": 8, "outdated": 0},
  "rounds": [Round],
  "server": {"pid": 1234, "port": 7777, "started_at": "…Z", "version": "…"},
  "ui": {"connected": true, "last_seen": "…Z", "open_polls": 1}
}
```

`counts` are over root comments, except `total` and `pending`, which count all comments. `version` grows on every
mutation; `generation` on every successful load (start, reload, cover letter, PR link, "since"), which tells the page
that the diffs changed. `state()` (`/api/state`, `/api/events`) is the cheap subset: the counters, `counts`, round
and commit counts, the last round, `ui` and `pr_synced_at` (the last GitHub sync, 10.5). `ui.last_seen` is the last
`/api/events` request not made by the CLI (`User-Agent: ccr-cli/…`); `connected` means within 60 s.

### 2.7 Flag (re-review)

`{id, view, path, side, line, start_line, reason, head, created_at, stale}`. A flag marks lines of a view as a change
that needs a closer look, so the user can review just those lines in context; an agent re-reviewing a pull request
flags each change it cannot vouch for. It is not a comment: no thread, resolving, rounds or export. `view` is `since`
by default, else `combined` or a listed commit, never `worktree`; `reason` is plain text; both ends of the range must
be rows the view's diff shows on that side. A flag belongs to the head it was made on: once `range.head` moves it is
`stale` and shows nowhere, since its lines may hold other code. A new-side flag shows in its view, on the same head
lines in "All changes" and "Since your last review" (a commit's lines carried there with `map_line`), and in the
listed commit that last changed each line (`blame_lines` at the head) — in every view only on rows its diff shows.
An old-side flag shows only in its own view. `GET /api/flags` returns the flags and, per view, the rows to mark.

---

## 3. Git extraction (`ccr/gitx.py`)

gitx runs git and returns plain dicts: ranges (2.1), the chain and its stats, the diffs of each view, file contents,
blame and `map_line`. Failures raise `GitError` with an HTTP status (400; 404 unknown object; 413 too large; 415
binary). git ≥ 2.24 is assumed, not checked; only the rebuild of 3.2 checks for the 2.40 it needs.

### 3.1 Invocation rules

* No shell. The environment drops every `GIT_*` variable (except `GIT_CONFIG_NOSYSTEM`, `GIT_SSH*`, `GIT_TRACE*`) and
  sets `LC_ALL=C`, `GIT_OPTIONAL_LOCKS=0`, no terminal prompt and no pager; every call pins the config that would
  change what ccr parses (quoted paths, colour, prefixes, relative diffs, submodule format, signatures), so neither
  the user's config nor their environment can.
* Diffs run without external diff drivers or textconv, with rename and copy detection, full blob ids and 3 lines of
  context; whitespace-ignoring views add `-w`.
* Output is decoded as UTF-8 with replacement, after splitting at NULs. A non-zero exit raises with git's stderr.
* **Argv safety**: a rev from outside gitx (HTTP, CLI) is first resolved to a full sha and only that sha goes to git,
  after `--end-of-options`; paths go after `--` (after `--end-of-options` for blame and `cat-file`). The server takes
  only revs of the current review (5.1).

### 3.2 Commands

* **The chain**: one `git log -z` for all commits; one `git diff-tree --stdin --raw --numstat -z` for all their file
  stats. The record layout of the latter is checked against the installed git by a test.
* **A diff** (`diff_commit`, `diff_range`, the tracked part of the worktree): one `git diff --raw -z -p`. The raw
  records give each file's skeleton (paths, status, score, modes, blobs); the patch is split at `diff --git` lines and
  the sections are handed out in order, a type change (`T`) taking two (deletion, then creation). Counts come from
  the parsed hunks; the raw block ends at the first `\0\0`. With `?ws=ignore` a second call without `-w` finds the
  files whose changes are whitespace only (`ws_only`).
* **The patch parser** also works alone (tests): paths come from `rename`/`copy` headers, else `---`/`+++`
  (`/dev/null` → null, C-quoted names unquoted), else the `diff --git` line. A hunk consumes lines until both of its
  counters reach zero and never looks for headers meanwhile, as `--- x` can be content; `\ No newline` marks the line
  before it; an empty line inside a hunk is an empty context line; anything else is a malformed hunk.
* **Worktree**: tracked changes from `git diff HEAD` (retried once after 200 ms on `index.lock`); untracked files from
  `git ls-files --others --exclude-standard`, skipping nested repositories and anything but regular files and
  symlinks, built in Python: a symlink's content is its target, a NUL in the first 8000 bytes makes it binary, more
  than 1 MiB makes it `too_large`, else one hunk of added lines. Bare repositories have no worktree.
* **Since your last review** (2.1): `rebuild_tree` is `git merge-tree --write-tree --merge-base=<old_base>` (git ≥
  2.40), giving a tree, written even with conflicts, and the conflicted paths. The view diffs that tree against the
  head, and the conflicted paths against the reviewed commit instead.
* **Blame** (`blame_lines`, for flags): `git blame --line-porcelain` with one `-L` per run of lines.
* **File contents** (`show_file`, for expanding context): a blob through `git cat-file` (not a blob → 404), or the
  working-tree file, whose directory must resolve inside the toplevel (403); a symlink is shown as its target text and
  never followed, so nothing outside the repository can be read. Binary → 415, over 8 MiB → 413; lines are split and
  cut like diff lines.
* **Languages**: a table of about 80 extensions and file names maps to highlight.js ids; `index.html` loads a grammar
  for each of them (a test checks).

### 3.3 `map_line` (HEAD-relative locations)

`map_line(from_rev, to_rev, path, line)` says where a line is in another revision, following a rename of the file:
an unchanged line, shifted by the hunks wholly above it (`moved`, else `same`; a context line inside a hunk is `same`
even when shifted), a deleted line's place at the first added line of its hunk (`changed`, or `deleted` when the hunk
adds nothing), or `file-deleted`. Lookups between two full shas are cached for the process (up to 4096); the working
tree is never cached.

---

## 4. Store (`ccr/store.py`)

One `sqlite3` connection guarded by a lock. The default file is `<session dir>/<key>.sqlite`, created 0600, in WAL mode,
and held with an exclusive `flock` on `<file>.lock` so a second server on it refuses ("db in use"). Tables: `meta`
(schema version, repository path, `version`), `reviews` (range, cover letter, every commit it ever listed with its chain
index and subject, PR link, "since"), `comments` (with the anchor in columns, the snippet, `moved_from` and `github` as
JSON), `rounds` and `flags`, comments and rounds keyed by their review.

Opening refuses a newer schema, and a database made for another repository path unless `--db-force`. Older schemas are
migrated in place (1: one review per repository; 2 lacks the PR columns, 3 the "since" one). `version` is persisted, so
a restarted server continues the count and an open page sees the change. Untrimmed diffs are cached in memory per view
and whitespace setting, and dropped on reload.

`load()` extracts the chain and stats; on success it clears the diff cache, bumps `generation`, re-anchors comments
(4.4) and bumps `version`; on a git error it keeps the previous data. Every mutation bumps `version` and wakes the
waiters of `wait(since_version, timeout)`, which back `/api/events` and `ccr wait`.

### 4.1 Anchor validation

* `review`: no commit, path, side or line.
* `commit`: a listed commit (full or short sha, or any rev that resolves to one), `combined`, `worktree` when shown,
  `since` when open; stored as the full sha or pseudo name.
* `file`: the commit, and a `path` or `old_path` of a file in its diff (stored as `path`).
* `line`: the commit, the path, `side`, `line ≥ 1`, optional `start_line < line`. Lines in the diff's hunks on that
  side get a snippet; other lines up to 100000 are accepted with an empty one (comments on expanded context).
  Compare views take no comments.
* A reply's `parent_id` must be a root; the anchor is copied from it. Bodies are non-empty after stripping, ≤ 64 KiB.
  The API writes as `user` or `claude` only.

### 4.2 Snippet capture

A line comment's `snippet` is the text of its anchored lines on that side, joined with `\n`, at most 32 lines or 8 KiB
(the last line `…` when cut), always from the diff that keeps whitespace changes.

### 4.3 Outdated

A comment is `outdated` when its commit is none of the listed commits, `combined`, `worktree` (when shown) or `since`
(when open). Outdated comments are kept and listed; the page only counts them in the All changes header (7.2).

### 4.4 Re-anchoring on reload

A comment about to become outdated after a reload follows its commit when it can: the new commit at the same chain
position with the same subject, else the only one with that subject. A commit comment moves; a file comment moves when
the file is in the new commit's diff; a line comment moves when a line on its side reads as its snippet's first line —
the only match, or the nearest within ±20 lines, and for a range the following lines must match too. A moved comment
records `moved_from`; one that finds no place stays outdated. `load()` reports the remapped and outdated ids and the
commits added and removed.

### 4.5 HEAD locations (`locate=1`)

For a root line comment, `head_location` is `map_line` from the revision its side shows in its view (2.3) to the
current `HEAD`, read on each request. A new-side worktree comment is `live`; a failure is `unknown`.

### 4.6 Reviews

A database belongs to a repository, which is reviewed many times, and a server that is killed, times out or is stopped
leaves its database behind. So every comment and round belongs to a review, and everything the store reads — comments,
counts, `outdated`, rounds and their numbering — is scoped to the review it serves. On opening it walks the stored
reviews newest first and takes the first that is empty (no comments, rounds or cover letter) or whose remembered chain
shares a sha or a subject with the range (an amend, a rebase, a wider range, a branch reviewed before), else starts a
new one; a range that lists nothing continues the newest review; nothing is deleted. `review()` reports which (2.6), and
`ccr start` prints it (6.2).

---

## 5. HTTP server (`ccr/server.py`)

`ThreadingHTTPServer` on `127.0.0.1:<port>`, daemon threads.

### 5.0 Protocol

HTTP/1.1 with a 60 s timeout; every response has `Content-Length` and `X-Content-Type-Options: nosniff`. Request
bodies need a `Content-Length` (no `Transfer-Encoding`: 411), at most 1 MiB (413, sent before reading, then the
connection closes), and when non-empty an `application/json` object (415, 400). Every error body is
`{"error": "…"}`. A client hanging up mid-response is ignored. `--verbose` logs each request without its query.

### 5.1 Security

* **Token**: 32 random hex chars per server. Every `/api/*` request carries it in `X-CCR-Token` (constant-time
  compare; else 401). `?t=` works only on `GET /`, where the page reads it and then removes it from the address bar.
* **Host**: must be `127.0.0.1`, `localhost` or `[::1]`, on every route, which defeats DNS rebinding. `Origin` and
  `Referer`, when present, must name the same hosts, ports not compared because SSH port forwarding changes them;
  `Origin: null` is refused everywhere, `Sec-Fetch-Site: cross-site|same-site` on the API. No CORS headers.
* **Revs and paths from clients**: `/api/file` takes `worktree` or a full sha that the review itself names (a sha, a
  first parent, an `old_rev`/`new_rev`, and with "since" open its tree and reviewed commit), and a relative path of a
  file in the review, with no `..`; `/api/compare` takes listed commits and their first parents; short shas resolve
  against listed commits only.
* **Static files**: a path with an empty or `..` segment or a NUL is 404, and the resolved file must lie inside
  `static/`; a fixed MIME table. Vendored files are cached for a year (immutable), so each is linked with a `?v=` that
  `index.html` writes literally: replacing one means changing its `?v=`. The page and its own scripts and styles are
  `no-cache`, so a browser reload shows a changed UI without a server restart.
* **HTML headers**: a CSP allowing only same-origin scripts, styles, images (and `data:`), fonts and connections,
  no framing, no base or form targets; `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, same-origin COOP
  and CORP. Hence no inline scripts and no `style=` attributes anywhere (setting CSSOM properties is fine).
* **Files**: the server runs with umask 077; the session directory is 0700 and refused when another user owns it or
  group or others can write to it; session files are written atomically, 0600, like the log and the database.
* **Long polls**: at most 64 waiting `/api/events`; beyond that the answer is immediate, with `retry_after: 5`.

### 5.2 Routes

| Method | Path | Does |
|---|---|---|
| GET | `/`, `/static/<file>` | the page and its assets |
| GET | `/api/review` | Review (2.6), always whole; before the first load `loading` is true and `commits` empty |
| GET | `/api/state` | `state()` (never 503) |
| GET | `/api/events?since=N&timeout=S` | long poll: `state()` with `changed: true` once `version > N`, at once when `N > version` (a page from another server run) or on stop; `changed: false` after `S` s (≤ 30, default 25) |
| GET | `/api/commits/{sha}?full=1&ws=ignore` | CommitDiff (2.3) of a listed sha, short sha or pseudo-commit; 503 while loading |
| GET | `/api/commits/{sha}/file?path=P` | one untrimmed FileDiff |
| GET | `/api/commits/{sha}/github-lines?path=P` | PR mode: where in that file a GitHub comment cannot go, and why (10.2, 10.4) |
| GET | `/api/compare?base=X&head=Y` | the diff between two listed commits (`kind: "compare"`) |
| GET | `/api/file?rev=R&path=P` | a file's lines (for expanding context) |
| GET | `/api/comments?state=&round=&resolved=&author=&commit=&path=&outdated=include\|exclude\|only&locate=1&project=<view>` | comments; with `project`, each carries `view_anchor`, where to show it in that view (its anchor in its own view; a line carried with `map_line` from another; a file comment on the same path; null for another view's commit comment or an unmappable line), and `projected` when it comes from another view; replies follow their root |
| POST | `/api/comments` | `{body, anchor, author?, parent_id?, github?}` → 201 Comment |
| PATCH | `/api/comments/{id}` | `{body?, resolved?, anchor?, github?}` |
| DELETE | `/api/comments/{id}?cascade=1` | 204; 409 for a root with replies and no cascade |
| GET, POST | `/api/comments/{id}/github` | PR mode: where GitHub will put the comment (10.2); record a post or an update (10.3) |
| POST | `/api/submit` | `{verdict, summary}` → 201 Round |
| POST | `/api/reload` | `{range?, n?, worktree?, first_parent?}` (omitted = kept) → the review, remapped and outdated comments, commits added and removed; a git error is 400 and keeps the old data |
| POST | `/api/cover` | `{text}`: the cover letter (≤ 64 KiB) |
| POST | `/api/pr`, `/api/since` | link a pull request (10); open or close "Since your last review" (2.1) |
| GET, POST, DELETE | `/api/flags` | list, add (400 for rows the view does not show), remove all (2.7) |
| POST | `/api/github/sync` | PR mode: store the mirrored discussion (10.5) |
| POST | `/api/restore` | `{payload, dry_run?}`: fill an empty review from an export (6.4) |
| POST | `/api/shutdown` | 202, then the server stops |

The cover letter, PR link and "since" bump `generation` too, so open pages re-render. PR routes answer 409 outside PR
mode. Unknown routes are 404.

**Lifecycle** (`ccr serve`): resolve the range and open the database (errors exit 1), bind, write the session file (real
port, own pid, token), load in a thread (meanwhile the routes that need the extracted data answer 503, and reload and
"since" wait for it), serve. On shutdown, SIGTERM or SIGINT it closes the database and deletes the session file only if
it is still its own. `--idle-timeout S` (default a day, 0 never) stops a server no request reached for `S` s; long polls
count.

---

## 6. CLI (`ccr/cli.py`)

`ccr <command>` (also `python3 -m ccr`, `bin/ccr`). Global options: `--repo PATH` (default: the toplevel of the
current directory), `--url`/`--token` to skip discovery (env `CCR_URL`, `CCR_TOKEN`), `--json`. CLI requests carry
`User-Agent: ccr-cli/<version>`, so they do not count as the page being open. Exit codes: 0 ok, 1 error, 2 timeout
(`wait`), 3 no running session.

### 6.1 Sessions (`ccr/session.py`)

A session is keyed by the repository's realpath (`sha1[:16]`), so each linked worktree is its own. The session
directory (`$CCR_SESSION_DIR`, default `~/.cache/ccr/sessions`, 0700) holds `<key>.json` (pid, port, token, URL, repo,
range, log, db), `<key>.log`, `<key>.sqlite` and `<key>.lock`. A command finding a stale session file (dead pid,
refused connection, a 401) deletes it and exits 3, listing the live sessions. The default port is
`7700 + key % 300`, then the next 20 ports, then any free one; an explicit `--port` that is busy is an error.

### 6.2 Commands

* `ccr start [--range SPEC | -n N] [--worktree | --no-worktree] [--first-parent] [--port N] [--db PATH] [--open]
  [--idle-timeout S] [--cover FILE] [--pr URL] [--since REV [--since-at TIME]]`
  1. Serialises concurrent starts with `<key>.lock` (one older than 30 s is ignored).
  2. A running session is reloaded with the options given (the others kept; none: the pinned spec, so new commits
     appear): `ccr: reusing running session (pid P)`, the reload lines and the URL.
  3. Otherwise it resolves the range itself, so a bad one fails at once, prunes week-old databases (below), and spawns
     `ccr serve` detached, its output to the log, the token passed only in the environment (never in `ps`). It waits
     ≤ 10 s for the server (on failure: `ccr: server exited with code N — last log lines:` and the log tail) and
     ≤ 120 s for the load.
  4. It prints `ccr: serving /abs/repo  (main..HEAD, 7 commits, +worktree)` and `ccr: url http://127.0.0.1:7777/?t=…`,
     with `ccr: note: …` for a range note and, between them when the database held a review already (4.6),
     `ccr: resuming review #N started <ts> (C comments, R rounds)` or `ccr: new review #N — the database also holds
     review #M (<range>, C comments, R rounds) of a different change`.

  `--cover` sets the cover letter; `ccr cover (TEXT | --file F | -)` replaces it later. It shows above All changes,
  with *Ask AI about the whole series* for comments on it, and `ccr export --md` prints it under `## Cover letter`.
  `--pr URL` (or `OWNER/REPO#N`) links a pull request (10) and prints `ccr: pr <url> (<owner/repo#N>): questions for
  Claude, GitHub comments for your pending review`. `--since REV [--since-at TIME]` opens "Since your last review"
  (2.1) and prints `ccr: since your last review of <R> (<at>): N files changed`, naming the paths that were not
  rebuilt, if any. All three apply on reuse too (where only the since line is printed); a bad `--pr`
  or `--since` exits 1 before anything starts, an unreadable `--cover` only once the spawned server fails on it.
* `ccr serve` runs the server in the foreground (also `--token T`, for tests, `--verbose`, `--db-force`); a token from
  the environment is shown redacted, so logs never hold it.
* `ccr stop [--all] [--purge] [--keep-db]` checks that the session's pid is the server's (else it is stale and nothing
  is signalled), writes `ccr export --md` and `--json` to `<session dir>/<key>-<time>.md|.json` (`ccr: exported to
  <path>` each), asks the server to shut down, and SIGTERMs it after 5 s only if its command line is a `ccr serve`. The
  database stays; `--purge` deletes the default one, the exports and the logs (`--keep-db` cancels `--purge`). Like `ccr
  start`, it then removes the database of every session that is not running — no live record, its lock held by no server
  — and whose database and WAL have not changed for 7 days; `ccr start` never removes the one it resumes.
* `ccr reload [--range | -n] [--worktree | --no-worktree] [--first-parent | --no-first-parent]` prints `ccr: N commits
  (was M), +a −r, K comments remapped, J now outdated`; `warning: R reviewed commits left the
  range` when some did, and one line per outdated thread, also those outdated before.
* `ccr comments [--pending | --submitted | --round N | --all] [--unresolved] [--unanswered] [--author A] [--commit SHA]
  [--path P] [--outdated | --no-outdated] [--context N] [--no-snippets]` prints threads (6.3): a filter picks a thread
  when its root or any reply matches, and marks the matching comments `★`. `--unanswered` = unresolved, not outdated,
  last comment the user's (a posted GitHub comment answers itself). Default `--all --context 3`.
* `ccr wait [--since-round N] [--since-version V] [--timeout S] [--any]` returns once a round after `N` exists (default:
  the count at call time): `ccr: round <n> — <k> new comments in <j> threads`, in PR mode then `— <g> GitHub comments
  to check and post`, and the round's threads, earlier comments as context, the new ones marked `★ new in round n`. A
  verdict follows `<n>` only when it is not `comment`, which the page always sends. A timeout (default 590 s, so one
  wait fits the agent's 10-minute Bash call; 0 never) exits 2 with the counts on stderr. Lost connections are retried
  every second; a dead pid or 30 s of failures exit 3 (`ccr: server gone`). After 30 s without the page ever polling it
  says `ccr: UI not opened yet`, once. `--any` returns on any change, with the threads changed since the call.
* `ccr reply ID (BODY | --file F | -) [--resolve] [--as claude|user] [--force]` refuses a body the same author already
  posted in that thread, unless `--force`. `ccr reply --batch (FILE | -)` takes JSON `[{"id", "body", "resolve"}]` or
  Markdown sections `## <id> [resolve]`, posts them in turn, prints `<id>: replied[, resolved]` or `<id>: ERROR …` per
  item, goes on after errors and exits 1 if any failed.
* `ccr comment (--review | --commit REV [--path P [--line N [--side S] [--start-line M]]]) (BODY | --file F | -)
  [--as claude|user] [--github]`; `--github` makes a GitHub comment (10.2, with `--as user`).
* `ccr resolve`, `unresolve`, `edit`, `delete [--cascade]`, `move ID --commit REV [--path … --line …]`.
* `ccr export [--md | --json] [-o FILE]`: `--md` is a `# Review — …` title (with `— PR <owner/repo#N>` in PR mode),
  the rounds and every thread (6.3); `--json` is the shape of 6.3; the file is created 0600.
* `ccr flag --path P --line N [--start-line M] [--side S] [--commit VIEW] (REASON | --file F | -)` adds a flag (2.7),
  `ccr flag --clear` removes them all, and `ccr flags` lists them, saying how many are hidden as stale.
* `ccr status` (URL, range, PR, "since", counts, rounds, whether the page is connected, log and db paths), `ccr sessions`
  (every session, pruning stale ones; exit 3 if none), `ccr logs [-n N] [-f]`, `ccr open`.
* `ccr restore FILE [--dry-run]` (6.4), `ccr gh-sync` (10.5) and `ccr gh-post ID… [--dry-run]` (10.3).

`-` as a body or file reads stdin.

### 6.3 Markdown rendering of comments (`ccr/render.py`)

Written for an LLM reader: a fixed order (chain order, then path, end line and creation time), explicit ids, code
context with line numbers, HEAD-relative locations and unambiguous anchors.

```
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

### (commit)
#### [id: …] claude · commit · R0 · unresolved
…

## All changes (combined)
## Uncommitted changes
## Outdated (anchored to commits no longer in the range)
#### [id: …] user · 1b2c3d4e5f src/x.py new:10 → HEAD src/x.py:10 · R1 · unresolved
```

Groups come in this order: review-level comments, Since your last review, the commits, All changes, Uncommitted
changes, outdated. The snippet shows `--context N` rows around the anchored ones, which start with `>`. The HEAD arrow
is qualified `(moved)`, `(changed near)`, `(deleted)`, `(file deleted)` or `(live)`. Every repository- or
user-derived string is cleaned of control and bidi characters (one-line fields also of newlines, shown as `␤`); a body
line starting with `#` is written `\#…`, so a line starting with `#` is always structure; reply bodies are indented two
spaces.

In PR mode the title carries `— PR <owner/repo#N>`, and right after the author each user root says what it is:
`question`, `GitHub comment (not posted)` or `GitHub comment (posted: <url>)`, with `; edited since, the update not
posted yet` when edited after posting; mirrored comments read as 10.5 says.

`--json` prints `{"review" (without files), "rounds", "comments", "threads": [{"root", "replies", "last_author",
"answered"}]}`, threads referring to comments by id.

### 6.4 Restoring a review from its export (`ccr restore`, `ccr/restore.py`)

A review lives in its database, which `ccr stop --purge`, the weekly prune (6.2) or a lost file can drop. `ccr stop`
writes two exports first: the Markdown one, and the JSON one, which holds every stored field. `ccr restore FILE`
loads either into the running review (`POST /api/restore`); `restore.py` parses what 6.3 writes.

* The review must have no comments or rounds of its own (mirrored GitHub comments may be there); an export of a pull
  request needs the review linked to it, and an abbreviated commit (a Markdown
  export's) must resolve in the repository (409 otherwise); a full sha is taken as is, and a commit outside the range
  makes an outdated thread.
* Comments keep their ids (a new one when the id is taken in the database, reported), parents, authors, bodies,
  anchors, states, rounds, times, resolved flags and GitHub records; rounds their number, time, verdict and summary;
  an empty cover letter takes the export's.
* A Markdown export loses a root's creation time (reconstructed before its replies, edit and round), the snippets
  (captured again, 4.2), a round's base, head and commits, most of a posted comment's GitHub record, what cleaning
  stripped, and the node ids of mirrored comments.
* Mirrored comments: a JSON export's keep their node ids, so the next sync updates them as usual. A Markdown export's
  are matched to mirrored roots by login and text (and place when needed), their replies coming from the sync; one
  that matches nothing is restored marked `deleted` when it holds replies of the user or Claude, else left to the
  sync. A mirrored copy of a posted comment of the user, made by a sync before the restore, is dropped; the next sync
  joins it to the restored comment.
* The CLI dry-runs first, so a refusal changes nothing. In PR mode it syncs (10.5) before restoring a Markdown export
  with mirrored comments, and after every restore, so posted comments learn their threads again. It reports what it
  restored, matched, dropped, renamed and left outdated; `--dry-run` changes nothing.

---

## 7. Web UI (`ccr/static/`)

One page: vanilla JS in `app.js`, no framework. It must stay fast on 100 files and 10k lines, so events are
delegated (one listener per event type on `#main`, one `keydown` on the document), each file body is built as one
HTML string with every dynamic text escaped and assigned once with `innerHTML`, and threads and the header are
patched piecemeal. No `style=` attributes (CSP, 5.1): computed styles go through CSSOM properties or classes.

### 7.1 Boot & token

`theme.js` sets the theme (`auto|light|dark`, `auto` from `prefers-color-scheme`) before the first paint and enables
the matching highlight.js sheet. `app.js` takes the token from `?t=` (else from `localStorage`, per port), stores it
and removes it from the address bar. Without a valid token, or on any 401, polling stops and a full-page notice says
to open the URL `ccr status` prints, as the server may have restarted. **Copy link** and every permalink rebuild
`origin/?t=<token>#<hash>`, so links work in other tabs and browsers.

### 7.2 Layout

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ ⎇ repo · branch   main..HEAD · 7 commits   [Unified] [Wrap] [Hide ws] [☾] [⟳] [Submit ●3] │
├────────────┬─────────────────────────────────────────────────────────────────────────┤
│ COMMITS    │ Header: subject · body · author · date · sha · +40 −12   [💬 Ask AI …]   │
│ ● All      │ ┌ src/fetcher.py  M  +10 −2                              [Ask AI] [▾] ┐ │
│ ○ 9fceb02 •│ │ @@ -10,7 +10,9 @@ def foo(self):           [⤒ 20] [expand all] [⤓ 20]│ │
│ ○ 1a2b3c4  │ │ 11        - y = 2                                             [+]   │ │
│ ○ Worktree │ │     11    + y = 3                                                   │ │
│────────────│ │ ┌ thread ──────────────────────────────────────────────────────┐   │ │
│ FILES  [🔍]│ │ │ U user · 2 min ago · Pending      [Edit][Delete][Ask AI][Resolve]│   │ │
│ ▾ src/ccr  │ │ │ C Claude · 1 min ago · New                                     │   │ │
└────────────┴─┴─┴─────────────────────────────────────────────────────────────────────┘
```

**Top bar**: repository, branch, range (its note as a tooltip), commit count, in PR mode a link to the pull request;
view mode (unified/split), wrap, hide whitespace (`?ws=ignore`), theme, reload (afterwards a banner with the reload
summary), **Submit** (7.6), copy link, sidebar toggle. View settings persist in `localStorage`.

**Sidebar** (resizable, collapsible):

* *Commit chain*: All changes first, the commits oldest first, Uncommitted changes last (greyed, "(clean)", when
  empty); Since your last review, when open, is a group of its own above them, with the date of that review. An item
  shows the short sha, subject, author avatar, relative date, `+N −M`, thread badges (pending yellow, unresolved red),
  `⚑N` when the view shows flags, a merge glyph, and a `•` when the commit is new since the last round. Hovering shows
  the full message, author, date and sha. A click selects; Shift+click on another real commit opens a compare view of
  the range between them (no comments there), which any single click leaves.
* *File tree* of the selected view: single-child directories merged (`src/ccr/static`), folders expanded at first and
  their state persisted, status letters coloured, `+N −M`, thread counts and `⚑N`. The filter matches path substrings;
  while it is set the tree is a flat list and non-matching file cards are hidden too.

**Main pane** — `#main` is the only vertical scroller, so file headers can be sticky:

* *Header*: subject, body, author, date, sha (click to copy), parents, stats, and *Ask AI about this commit* (*… the
  uncommitted changes*, *… these changes*). Pseudo-commits say what they are; Since your last review names the review it
  starts from, the base the reviewed version was rebuilt on and the head, and the paths compared with the reviewed
  commit itself. A view showing flags has **⚑ Next (N)**, which selects the next flag's lines, wrapping around; flagged
  rows get orange stripes on both edges (in split view on the flag's side) and a ⚑ whose tooltip is the reason; flags
  made on an earlier head raise a banner instead. All changes has *Ask AI about the whole series* instead (review-level
  comments, shown under the cover letter), the cover letter (Markdown, or a hint that the agent can set one with `ccr
  cover`) and a note counting outdated comments. An empty commit says so; a compare view says comments are off.
* *File card*: a sticky header with the path (`old → new` for renames), status, mode change, `+N −M`, thread count,
  *Ask AI* (in PR mode also *GH comment*, 10.4), collapse and copy path. Binary files are not shown; a `too_large`
  file offers *Load anyway*; a file without hunks (mode only, whitespace only) gets a note.
* *Diff bodies* render lazily: each gets a placeholder of its estimated height (at most 2000 px), and an
  `IntersectionObserver` (1500 px ahead) renders it once, synchronously; rendered bodies stay.

### 7.3 Diff table

`table.diff`, fixed layout and full width, four columns in either view so thread rows span them alike. Wrap is on by
default; with it off the diff body scrolls sideways while its header stays sticky. Tabs are 4 wide.

* **Row model**: a hunk's lines form blocks: a run of context lines, or a change block — a run of deletions and the
  run of additions after it (either may be empty) — where the i-th deletion pairs with the i-th addition. Unified view
  has one row per line, deletions before additions; split view has one row per pair, a missing side hatched.
* **Thread and editor rows** follow the line they belong to. In split view such a row sits under its own side, the
  other half empty, so rows for both sides of one pair stack, the old side's first.
* **Hunk rows** show the header and expand the hidden lines between hunks: one button for a gap of ≤ 20 lines, else
  20 up, 20 down or all; also above the first hunk and to the end of the file. Expansion fetches the file once
  (`/api/file`, the old side for a deleted file, never a null rev or a submodule), adds ordinary context rows, which
  take comments, and re-renders only that body, keeping editors, the selection and the scroll position.
* **Highlighting** tokenises all of a side's diff lines at once (expanded context included), so multi-line strings and
  comments highlight correctly, then splits them back into lines. Plain text instead without highlight.js, for an
  unknown language, past 500000 characters, or when the line count comes out wrong; lines over 1000 characters stay
  plain. No element gets the class `hljs`, whose background would paint over the diff colours.
* **Word diff** on paired lines up to 400 characters: an LCS over word, space and punctuation tokens marks the changed
  pieces, unless more than half of either side changed. A stripped `\r` shows as a dim `␍`, a cut line as
  `… [truncated]`.
* **Gutter**: one shared set of buttons per file moves into the hovered row's number cell: a **[+]**, which opens on
  hover or click into *Ask AI* (in PR mode also *GH comment*, right of it). A click where hovering just opened it
  (within 600 ms) stays with the [+] rather than reaching *Ask AI* under the pointer. The hovered row (in split view
  its hovered side) is tinted grey. Hidden in compare views. *Ask AI* opens the editor on the selected range if one
  ends on that row, else on the line.
* **Side rule**: additions anchor to `new`, deletions to `old`; a context row to `new` through its [+], to `old`
  through its old line number (or the left [+] in split view).
* **Selecting lines** (one side only): press on a line number and drag, with auto-scroll near the edges; Shift+click
  extends; a click selects one line and puts it in the hash; a click in code or a commit switch clears it (Esc too,
  with the shortcuts of 7.6 on).
  Pressing in a code cell of split view makes only that side selectable, so copied text is one side's.
* **Threads** attach to their row by anchor key (7.8), a range under its end line; hovering a range's thread marks its
  lines. A thread whose row is not rendered (an anchor drifted in `combined` or `worktree`, whitespace-only files,
  hidden large files) is not shown; `ccr comments` still lists it. The page always fetches comments projected into
  the selected view (`project=`, 5.2) and places each root at its `view_anchor`; a projected root carries a *from …*
  tag opening the view it was written in. Replies post `parent_id` only.

### 7.4 Comments, editors, threads

* **Editor**: **Write** and **Preview** tabs. The textarea grows with its content (up to 60% of the height, then
  scrolls); Preview renders the same Markdown as comment bodies. While text is selected, **Bold** and *Italic* (and
  Ctrl/⌘+B, Ctrl/⌘+I) wrap each selected line in `**…**` / `_…_`, or unwrap it, through `execCommand('insertText')`
  so Ctrl+Z undoes it. **Add comment** (Ctrl/⌘+Enter) posts; **Cancel** (Esc) closes and keeps the draft, unless the
  text is empty. Buttons are disabled while a request runs; on success only that thread is patched, on error the
  editor stays open with a toast. Open editors and their tab are kept in state, so every re-render recreates them;
  drafts persist in `localStorage` (debounced), and a button with a draft shows a dot.
* **Thread**: comments with an avatar (user, Claude), author, relative time (from the server's `now`, as the
  browser's clock may differ) and tags: *Pending* (not in a submitted
  round yet, though Claude can read it), *R<n>*, *edited*, *moved*, *New* (7.5). Actions: Edit, Delete (confirmed;
  a root's replies go with it), *Ask AI* (a reply), Resolve. Resolved threads collapse to one line unless
  they hold unseen comments.
* **Markdown** (own renderer; everything escaped first): paragraphs and line breaks, ATX headings, `code`, fenced code
  (highlighted when the language is known), **bold**, *italic*, `- ` lists, `> ` quotes, GFM tables (alignment from
  the delimiter row, rows up to a blank line or another block, short rows padded, long ones cut, `\|` for a pipe; at
  most as wide as the comment, long words wrapping), and links only to `http(s)` URLs, opened in a new tab without
  referrer. No autolinks. A 7–40 hex token naming a listed commit links to it.
* **Counts** (per commit and file: threads, pending, unresolved over roots not outdated) are derived from the comments
  after every change; the Submit badge counts pending comments.

### 7.5 Live updates, seen-tracking, reconciliation

* Only a visible tab polls `/api/events`; one tab per origin leads (a Web Lock) and shares what it learns with the
  others over a `BroadcastChannel`. Hidden tabs re-sync when shown.
* On a change the page refetches the comments; a new `generation` also refetches the review and the selected diff
  (All changes when that commit is gone) and re-renders, keeping the scroll position, editors and selection. A new
  `server.started_at` re-initialises the page, keeping drafts.
* Comments are reconciled by id: only threads whose members, edits, resolution or states changed re-render, and
  editors are never touched. New comments by Claude raise *"Claude replied to N threads (k resolved) — Show"*.
* **Seen**: the newest comment rendered while visible for a second is remembered per repository; newer ones get a
  *New* tag, their thread a green edge and their file and commit a dot, and they keep resolved threads open.
* Disconnected: a banner that retries, and after 30 s says the server may have stopped; reconnecting refetches.

### 7.6 Navigation, hash, keyboard, submit

* **Hash**: `#<sha>`, `#<sha>/<path>`, `#<sha>/<path>:<o|n><line>[-<line>]` or `#compare:<a>..<b>` is the selection.
  Commit and file changes push history entries, line clicks replace them, so Back and Forward move between commits.
* `navigateTo` selects the commit, renders and expands what it needs (file, collapsed thread, context around a missing
  row), scrolls the target below the sticky headers and flashes it.
* **Commit switch** opens the commit at its top, so the message is read first; only a target with a file, line or
  thread scrolls further.
* **Keyboard**: Ctrl/⌘+Enter posts and Esc cancels the focused editor. Single-key shortcuts (`j`/`k` files, `]`/`[`
  commits, `n`/`p` threads, `N`/`P` unresolved threads, `c` comment, `x` collapse, `u` view, `w` wrap) exist but are
  off, behind `KEYBOARD_SHORTCUTS` at the top of `app.js`.
* **Submit** bundles every pending comment into the next round (`{verdict: "comment"}`) and wakes `ccr wait`; a toast
  confirms it. It is disabled while nothing is pending or a submit is running. Claude can read pending comments before
  that (`ccr comments`), which the skill forbids it to act on.

### 7.7 Theme

Colours are custom properties on `:root[data-theme=light|dark]`, bar a few fixed whites and shades; light resembles
GitHub and dark GitHub dark-dimmed. Animations stop under `prefers-reduced-motion`; focus rings are visible; relative
times refresh every minute.

### 7.8 Contracts shared with the e2e driver

The driver relies on the ids, classes and data attributes `app.js` renders, on `body[data-ready="1"]` after the first
full render, and on the anchor keys that name a thread's place (`data-key`): `line:<commit>|<path>|<side>|<endLine>`,
`file:<commit>|<path>`, `commit:<commit>`, `review:`. Renaming one means changing the driver too.

---

## 8. Agent loop (`skills/review-commit-series/SKILL.md`)

The skill is the agent's side of the loop and of PR mode; it is the reference, and this document does not repeat it.
It quotes ccr's exit codes, the `start`, `wait`, `reply --batch` and `gh-post` lines, errors such as `range … is
empty` and `identical reply already exists on this thread`, and the 6.3 Markdown, and says what to do on each:
changing one of them means changing the skill. `--since` and flags (2.1, 2.7) serve a separate `/re-review` skill,
outside this repository, which ccr's notices about stale flags name; changing them means checking it too.

Claude Code loads the checkout as a plugin from `~/.claude/skills/ccr` (also `claude plugin marketplace add <checkout>`
and `claude plugin install ccr@ccr-local`, or `claude --plugin-dir <checkout>`), with `bin/` on PATH while the plugin
is enabled; elsewhere use `bin/ccr` or `pip install -e .`.

---

## 9. Tests (`tests/`)

`python3 -m pytest -q`; everything passes without network access.

* `conftest.py` builds the fixture repositories: one with most kinds of change git can show (renames with and
  without edits, deletions, binary and `textconv` files, unicode and space in paths, missing final newlines, mode
  changes, symlinks, a merge, an empty commit, a file too large to show, CRLF) plus a dirty worktree (staged,
  unstaged, untracked, ignored, a nested repository), and a re-reviewed pull request rebased twice, the second time
  with a conflict.
* `test_gitx.py`: the parser on hand-written patches, the chain, stats against `--numstat`, each diff kind, file
  contents, ranges, `map_line`, the rebuild and blame.
* `test_store.py`: comments and their validation, snippets, submitting, outdated and re-anchoring, the counters,
  `wait`, head locations, reopening a file database, reviews, PR mode (targets, refusals, the mirrored discussion,
  migrations), "since" and flags; that `github_lines` blocks exactly the rows `github_target` refuses.
* `test_server.py`: a real server on port 0 — authentication, host and origin checks, body rules, every route, static
  path traversal, MIME types and headers.
* `test_cli.py`: the commands end to end through `bin/ccr` against a background server, incl. the token never in `ps`,
  file modes, reuse, stop and its exports, and restore.
* `test_render.py` (golden Markdown), `test_restore.py` (6.4), `test_github.py` (10, against `fake_gh.py`, an
  in-memory GitHub behind a fake `gh`).
* `test_e2e.py` runs `tests/e2e/driver.mjs` (headless Chromium over CDP from Node ≥ 22, no dependencies) against a live
  server in three scenarios — a review, PR mode, and Since your last review with flags — and checks the driver's
  report (no failed step, no console error) and the state the browser left on the server. Skipped without Chromium
  or Node.

---

## 10. PR mode (`--pr`, `ccr/github.py`)

PR mode makes ccr a reading aid for someone else's pull request. `ccr start --pr URL` (or `OWNER/REPO#N`) links the
review to it; the agent serves the pull request's own chain — its head checked out in a worktree of its own,
`--range <merge base>..HEAD`, so All changes is the diff GitHub shows — and changes no code. A user's comment is one
of two things:

* a **question** for Claude: the default, and the only kind on a commit, on the whole pull request, and for a reply
  in their threads, under a review body or in a deleted thread (10.5). The agent answers it in the thread, before the
  round's GitHub comments; nothing reaches GitHub.
* a **GitHub comment**: a root on a line, a range or a file written with *GH comment*, or a reply written with
  *GH reply* (10.4, 10.5). Once it is submitted in a round, the agent checks it — its claims against the code, whether
  it fits its place and asks something of this pull request — and, when it holds, posts it **verbatim** with
  `ccr gh-post` (10.3) into the user's **pending** review, starting one when there is none. Otherwise nothing is
  posted: the agent replies with the problem and a corrected wording, and the user edits the comment (or says to post
  it as it is) and submits again.

ccr never submits the review nor sets its body: the user submits it on GitHub, with the verdict and body they choose.

Only the CLI runs `gh` (`gh-sync`, `gh-post`, `restore`) and hands what it read or posted to the server
(`/api/github/sync`, `/api/comments/{id}/github`). The server never contacts GitHub, so the page's mirror is as fresh
as the last of those commands.

### 10.1 Data

`Review.pr` is `{url, host, owner, repo, number}`, kept per review. `Comment.github` is null for a question,
`{"status": "local"}` for a GitHub comment or reply not on GitHub yet, and once posted `{"status": "posted", …}` with
GitHub's ids and URL, the place it went to and the commit.

A posted comment keeps its place and kind: moving it or turning it back into a question is 409. Its body can change
while its review is pending on GitHub: the record gains `edited` until `ccr gh-post` has put the new text there
(10.3). Once a sync sees the review submitted on GitHub, a new body is 409 (change it there). Resolving and deleting
(in ccr only) stay possible, and recording a post is not an edit. A submitted GitHub comment, posted or not, goes back
to `pending` when its body or anchor is edited or a question is turned into one: what the agent checked is no longer
what would be posted, so it is a draft again until the user submits it.

### 10.2 Where GitHub anchors a comment

GitHub places review threads on the pull request diff — All changes — and only on the lines it shows (changed lines
and their context). `github_target` finds that place:

* A file comment goes on the file in All changes (by `path` or `old_path`); a file not in it is refused.
* A line comment on `combined` keeps its lines. On a commit each end is carried with `map_line` to the head (`new`)
  or the base (`old`) and must arrive `same` or `moved`, a range on one path in order. Both ends must be in one hunk
  of All changes on that side. `new` is GitHub's `RIGHT`, `old` its `LEFT`.
* Refused when those rows no longer read as the comment's snippet: All changes anchors keep their numbers across a
  reload, so after the head moved the line there may be another.
* On `since` (2.1) the new side is the head's, so lines stay. The old side is the version reviewed and must still
  read as the snippet. When none of its lines was removed since, they are all still at the head and go there
  (`RIGHT`). One that takes in a removed line goes on the removed content instead, which the pull request diff shows
  on its old side as far as the base has it: each end is carried to the base (`LEFT`), an end that is not there
  falling back to the first (last) removed line. A removed line the pull request had added itself is on neither side
  of that diff, and GitHub takes a pending comment only on the head's diff: refused. The target of an old-side
  comment carried to the head or the base says so (`carried_from`), and `ccr gh-post --dry-run` shows it.
* Refused too, with the reason: other anchor kinds, `worktree`, outdated anchors, a review without a base, and every
  failed rule above (*"src/app.py:10 (new side) is not in the pull request diff, …"*, *"line 2 of notes.txt is changed
  again later in the pull request, …; comment on it in All changes"*, *"a GitHub comment range must stay within one
  hunk of the pull request diff"*).

The rules run when a GitHub comment is created, switched from a question or moved (400 or 409 with the reason, so the
editor stays open), and again for `GET /api/comments/{id}/github`, which also returns the head, base, anchored lines and
body. `GET /api/commits/{view}/github-lines` applies them to every row of a file for a one-line comment, so the page
can grey out *GH comment* in advance (10.4). Only the user's comments can be GitHub comments.

### 10.3 Posting (`ccr gh-post`)

`ccr gh-post` first syncs the discussion as `ccr gh-sync` does (10.5), so the agent knows GitHub's latest state: when
the sync brings news — comments added, edited or deleted on GitHub since the last sync — it lists them and posts
nothing, so the agent reads them first, and a failed sync posts nothing either; `--dry-run` lists them and goes
on. It posts only GitHub comments submitted in
ccr and not posted yet, one at a time, each re-read from the server right before. GitHub is reached only through
`gh api`, as the account `gh auth` holds (`--hostname` for a host other than github.com): GraphQL operations named
`Ccr…`, which `tests/fake_gh.py` answers by name, plus one REST call for a merge base git cannot compute. Posting a
comment:

1. Read the pull request (id, head, base, its last 100 commits), the user's pending review with all its comments, and
   how many reviews the user has submitted.
2. Refuse, posting nothing, when `range.head` is not one of the pull request's commits (the wrong pull request, or a
   rewritten one; past its last 100 commits ccr cannot tell, and refuses), when the pull request diff does not start
   at `range.base` (its merge base, computed locally, else asked of GitHub), or when the pending review is on another
   commit than `range.head`. A head that moved on is only a note: the comment goes on the commit ccr reviewed.
3. A comment in the pending review with the same body at the same place counts as posted already, and is recorded.
   GitHub does not say a comment's side, so "the same place" also needs its diff hunk to end on the target's row.
4. Without a pending review, start one on `range.head` with no body and no event, so it stays pending. One GitHub
   starts on another commit is undone and refused.
5. Add the comment as a new thread (path, body, line or file, side, range). When GitHub refuses it and step 4 started
   the review, the review, still empty, is deleted again, so a refusal leaves GitHub as it was.
6. From here the comment is on GitHub, and nothing raises: the thread GitHub returns must sit where it was asked to
   (when `range.head` is the head), and a re-read must show the same pending review holding the new comment, every
   earlier comment unchanged and the submitted-review count unmoved. Each discrepancy is a `warning`; the comment is
   posted and recorded anyway.
7. Record the post. If recording fails, the output says posted but not recorded, and the next `ccr gh-post` finds it
   (step 3) and records it.

A posted comment edited since (10.1) is updated instead: refused when it is no longer in the user's pending review
(submitted, deleted or removed there), a no-op when GitHub has the new body already, else updated and re-read (the
new body there, the other comments unchanged; each discrepancy a `warning`).

The output names each post (`<id>: posted <path>:<line> (<side>) → <url>`, `already posted`, `updated`), notes and
warnings, starting a review, and finally where to submit it; a question, a pending comment, an unknown id or a refusal
is `<id>: ERROR …`, and any error or warning exits 1. `--dry-run` shows the place, the anchored lines and the verbatim
body. Nothing else on GitHub is submitted, edited or deleted.

### 10.4 UI

With a pull request linked, the top bar links to it; the gutter [+] opens into *Ask AI* and *GH comment*; the file
header has *GH comment* beside *Ask AI*; the whole-review button reads *Ask AI about the whole pull request*. A new
comment's editor on a line or a file has an *Ask AI | GH comment* switch that keeps the text; writing a GitHub comment
it is labelled **Add GH comment** and says the text goes verbatim to the pending review once Claude has checked it.
Where GitHub would refuse a GitHub comment (10.2), *GH comment* stays visible but inactive, in the gutter, the switch
and the file header, and says why on hover and on click; the page asks per file and view once a card renders, and
rows it did not ask about (expanded context) stay active and are checked when written. A draft remembers its kind.

A thread that takes GitHub replies (10.5) offers **GH reply** beside **Ask AI**; a collapsed thread's line offers only
**Ask AI**, which expands it. Roots are tagged *Question* or *GitHub · not posted*; a posted one links to GitHub
(*GitHub ↗*), stays editable while its review is pending, shows *edit not posted* until `gh-post` updated it, and its
Delete removes it from ccr only. Every comment carries a channel: GitHub (GitHub comments and replies, posted or not,
and mirrored comments) on grey, Claude (questions and Claude's comments) on blue, each with a left stripe; an editor
takes the colour of what it writes, so the two dialogues read apart even inside one thread. When comments get posted, a
toast says to submit the review on GitHub, with a link.

### 10.5 The pull request's discussion (`ccr gh-sync`)

`ccr gh-sync` mirrors the pull request's discussion into the review, read-only: every review thread with all its
comments, and the non-empty bodies of submitted reviews, stored as comments by `github` with GitHub's body, times and
author, and a `github` record (`status: "remote"`, ids, URL, login, whether the viewer wrote it, pending or submitted;
on a thread's first comment also the thread id, outdated, resolved on GitHub and its placement).

* **Placement.** A current thread with a line goes on its lines in All changes, on the side GitHub gives, if the diff
  has them; when the pull request head is not `range.head`, new-side lines are carried there with `map_line` and
  old-side ones are not trusted. Otherwise, and for outdated and file threads, it goes on its file in All changes, or
  on the whole review when the file left the pull request; review bodies go on the whole review.
* **Every sync** places threads again, updates edited bodies and changed states, adds new comments and removes the
  ones GitHub no longer has — a root with replies in ccr stays, marked `deleted`. A thread starts resolved in ccr when
  it is resolved on GitHub; after that the flag is the user's. A comment posted from ccr is recognised by its id and
  not mirrored again: its record learns its thread and GitHub state, and replies to it on GitHub join its ccr thread.
  Mirrored comments cannot be edited, moved or deleted in ccr (409: they change on GitHub). The answer lists the
  `news` that `gh-post` acts on (10.3): comments added, edited or deleted on GitHub since the last sync; a thread that
  only moved or changed state is no news.
* **GitHub replies.** The user's reply can be a GitHub reply in a review thread on GitHub — a mirrored one, or a
  posted comment once ccr knows its thread — and in a thread that can start one: a question or a GitHub comment of the
  user on a line or a file. There the thread's first GitHub comment, root or later reply, starts the thread on GitHub
  at the root's place (checked as in 10.2 when written), and later GitHub replies wait until it is posted (409, or
  *run ccr gh-sync* while ccr does not know the thread yet) and then go into it. Replies are checked and posted like
  GitHub comments, into the pending review, which may be on another commit since a reply is placed by its thread; one
  already there with the same body under the same first comment counts as posted. A reply on a commit or the whole
  pull request, under a review body or in a deleted thread stays in ccr (400 says so).
* **UI.** A mirrored comment shows `@login` (*(you)* for the viewer), a *GitHub ↗* link and the tags *outdated*,
  *resolved there*, *pending there* or *deleted there* (a review body: *review · changes requested* and the like), and
  has no Edit or Delete; a root whose thread holds a mirrored reply has no Delete, since deleting it would take the
  replies along. Outdated threads, review bodies and deleted threads start collapsed. Comments a later sync brings are
  *New*, with a toast; those older than the first sync are not.
* **For the agent** (6.3): a mirrored author reads `@login (GitHub[, you])`, a mirrored root `GitHub thread (outdated,
  was new:N, shown on the file, resolved there, pending, deleted there)` or `GitHub review (<state>)`, a mirrored reply
  `on GitHub (pending)` when it is, the user's GitHub reply `GitHub reply (not posted)` or `GitHub reply (posted:
  <url>)`; mirrored comments show `on GitHub` instead of a round.

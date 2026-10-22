# ccr — Commit Chain Reviewer — Specification

How ccr works and why: what each part does and guarantees, and the decisions the code does not explain. Read it
before changing ccr; the exact git flags, SQL, selectors and messages are in the code (but see 8 for the messages
the agent relies on).

ccr is a local code-review tool for agentic workflows. An agent (Claude Code) prepares a chain of commits, starts
ccr and hands the user a URL. The user reviews in a GitHub-like page — the chain, per-commit diffs, comments on
lines, files, commits and the whole change — and **Submit** makes the pending comments a round. The agent reads them
from the CLI (`ccr comments`, `ccr wait`), fixes the code, answers and resolves threads, reloads, and the loop
repeats. **PR mode** (10) points the same page at someone else's GitHub pull request: the agent changes no code, and a
comment is either a question it answers or a GitHub comment it checks and posts verbatim into the user's pending
review, which the user submits on GitHub.

Constraints:

* **Python 3.10+ standard library only** for the server and CLI; vanilla ES2020 in the browser, no build step;
  highlight.js vendored in `ccr/static/vendor/`.
* **SQLite** (stdlib `sqlite3`, default on Fedora and Ubuntu) in the private session directory: a killed server or a
  `ccr stop` loses nothing, and the next `ccr start` resumes the review. A stopped session's database is pruned a week
  after its last change (6.2), at once with `ccr stop --purge`; `ccr restore` brings a review back from its export
  (6.4). `--db :memory:` or `--db FILE` choose another store.
* **Local only**: binds `127.0.0.1`, and every `/api/*` call carries a per-process random token.
* Any git repository (git ≥ 2.30) and range, uncommitted work included.
* ccr never changes the working tree, refs or index contents. Diffing the worktree may let git refresh the stat
  cache in `.git/index` (`GIT_OPTIONAL_LOCKS=0` minimises that); "Since your last review" writes unreferenced
  objects (3.2).

---

## 1. Repository layout

```
README.md, LICENSE, docs/   overview, MIT licence, the README's screenshots
pyproject.toml              package "ccr", console script ccr = ccr.cli:main, no dependencies
bin/ccr                     runs the checkout's ccr without an install, the checkout first on sys.path so that a
                            reviewed repository's own ccr/ directory cannot shadow it
ccr/                        cli.py (6), server.py (5), gitx.py (3), github.py (10), store.py (4), session.py (6.1),
                            render.py (6.3), restore.py (6.4)
ccr/static/                 index.html (no inline scripts or styles), theme.js (pre-paint theme), app.js (7),
                            style.css, favicon.svg, vendor/ (highlight.js, BSD-3-Clause)
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
| `since`       | "Since your last review": what changed since an earlier review         | `--since REV` (kept with the review) |

**Since your last review** (a re-review). `--since R` is the commit an earlier review was submitted on (GitHub's
`PullRequestReview.commit.oid`), `--since-at T` when. With `B` = `range.base` and `H` = `range.head`, `old_base =
merge-base(R, B)` and `tree = rebuild_tree(old_base, B, R)` re-applies the reviewed version onto the current base. The
view is `diff(tree, H)`: what the author changed since the review, while whatever the new base brought in cancels out.
A path that conflicts in the rebuild is compared with `R` itself (its `old_rev` is `R`), so upstream changes may show
in it; without a shared base (or with no base) every path of All changes is, and only those. The view is recomputed on
every load, as `B` and `H` move. Its new side is `H`, so comments there behave as in All changes (they project, and
can be GitHub comments). Its old side, the version reviewed, is in no other view: comments there stay in this view,
and a GitHub comment there goes on those lines at the head while they are all still there, else on the removed
content (10.2).

**Range specs** (`--range SPEC` or `-n N`):

* `A...B` → `base = merge-base(A, B)`, `head = B`; `A..B` → `base = A`, `head = B`; a bare `A` → `A..HEAD`. An empty
  side means `HEAD`.
* `-n N` → `HEAD~N` in first-parent steps (the chain may hold more commits when merges are in it); the whole history
  when `N` is at least HEAD's first-parent count.
* Each side must name a commit; a spec starting with `-` or holding whitespace or NUL is refused.
* A base that is not an ancestor of the head is replaced by their merge base, with `range.note` saying so; no common
  ancestor is an error.
* **Pinned spec**: a reload without `--range` re-resolves the resolved spec, in which nothing counts from `HEAD`:
  `-n N` → `<base sha>..HEAD` (the whole history: `<empty tree>..HEAD`), `A...B` → `<merge base sha>..B`, `..B` →
  `<HEAD sha>..B`, so reviewed commits never drop out as `HEAD` moves on. A named base (`A`, `A..B`, a detected
  default) stays a name, `A` becoming `A..HEAD`, and follows that ref. `range.given` keeps the user's text.
* Without a spec: the first of `@{upstream}`, `main`, `master`, `origin/main`, `origin/master` and `origin/HEAD`
  with commits in `..HEAD`, pinned as `<ref>..HEAD`; else an error asking for `--range` or `-n`.
* An empty range without `--worktree` is an error, and so is one of more than 2000 commits.

The chain is `git log --reverse --topo-order base..head`, merges included and flagged; `--first-parent` is an
option.

### 2.2 CommitMeta

`sha` (or `combined` / `worktree` / `since`), `short_sha` (10 chars; the name for pseudo-commits), `kind` (`commit` or
the pseudo name), `parents` ([] for pseudo-commits and roots), `is_merge`, `shallow_boundary` (a parentless commit at
a shallow clone's boundary), `author` (no mailmap), `author_date`, `commit_date` (null for pseudo-commits), `subject`
and `body` (the message's first line and the rest, ≤ 64 KiB), `stats`, `files` (FileStats in git's order, the
worktree's sorted by path) and `comment_count` (roots on this sha, not outdated).

`FileStat`: `path`, `old_path` (renames and copies only), `status` (`A M D R C T`; untracked files are `A`,
conflicted ones `M` with the markers in their content), `score` (similarity for R/C), `additions`, `deletions`
(0/0 for binary), `binary`, `old_mode`/`new_mode` (null for an absent side; `120000` symlink, `160000` submodule) and
`old_blob`/`new_blob` (null for an absent side and for a worktree file).

### 2.3 CommitDiff (`GET /api/commits/{sha}`, `GET /api/compare`)

A CommitMeta whose `files` are `FileDiff`s: a FileStat plus `lang` (highlight.js id from the file name, or null),
`old_rev`/`new_rev` (below), `too_large` and its `reason`, the untrimmed `line_count` and `hunk_count`, `ws_only`
(`?ws=ignore` removed every hunk of a changed file) and `hunks`: `{old_start, old_count, new_start, new_count,
section, lines}`, each line `{t: ctx|add|del, o, n, s}` with `cr` (a `\r` was stripped), `nonl` (no newline at end of
file) or `trunc` when they apply.

| kind     | `old_rev`                        | `new_rev`    |
|----------|----------------------------------|--------------|
| commit   | `parents[0]`, null for a root    | `sha`        |
| combined | `range.base` (null if none)      | `range.head` |
| worktree | `HEAD` at extraction             | `"worktree"` |
| since    | the rebuilt tree (2.1), or `R` for a path compared with it | `range.head` |
| compare  | `base` (null: the empty tree)    | `head`       |

To expand context the UI fetches one side from `/api/file`: the new side, the old side for a `D`; never a null rev,
a submodule side, a binary or a `too_large` file.

Line texts are UTF-8 with replacement, one trailing `\r` stripped; lines over 20000 characters are cut; tabs stay. A
file of more than 5000 hunk lines, or with a line over 20000 characters, is sent without hunks (`too_large`, `reason:
"file"`); a response of more than 30000 lines drops the largest files first (`reason: "response"`). `?full=1` lifts
both caps (an untracked file over 1 MiB, 3.2, stays `too_large`). The store keeps untrimmed diffs: trimming happens
on the way out, and anchors and snippets always use the full data.

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
  "snippet": "    y = 3",          // the anchored lines at creation or `ccr move` (4.2), "" otherwise
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
its first parent, for `combined` the head and the base, for `worktree` the working tree and `HEAD`, for `since` the
head and the version reviewed (2.3's `old_rev`).

### 2.5 Round

`{number, submitted_at, verdict, summary, base, head, commit_shas, comment_ids}`: the batch of comments submitted
together. The UI always sends verdict `comment` and no summary, so its rounds carry no real verdict. `base`, `head`
and `commit_shas` record the chain at submit time, so the sidebar can mark commits added since. `comment_ids` is
computed: the comments with that round, including Claude's written after it (they take the round count at creation).
A non-empty `summary` becomes a review-level user comment of the round. A submit with nothing pending and no summary
is refused unless the verdict is `approve`.

### 2.6 Review (`GET /api/review`)

`repo` (`path`, `name`, `branch`, null when detached, `bare`), `range` (`spec` pinned, `given`, `base`, `head`,
`note`, `first_parent`), `review` (`id`, `started_at`, `resumed`, `previous`: which review is served, 4.6),
`options.worktree`, `cover` (the cover letter, Markdown, "" when none), `pr` (10.1), `since` (2.1), `commits`
(CommitMetas: `since` first when open, then `combined`, the chain, `worktree` last), `version`, `generation`,
`loading`, `now`, `counts`, `rounds`, `server` (`pid`, `port`, `started_at`, `version`) and `ui` (`connected`,
`last_seen`, `open_polls`).

`counts` (`pending`, `submitted`, `unresolved`, `total`, `outdated`) are over root comments, except `total` and
`pending`, which count all comments. `version` grows on every mutation; `generation` on every successful load (start,
reload) and when the cover letter, PR link or "since" is set or a review is restored, which tells the page to refetch
the review and its diffs. `state()` (`/api/state`, `/api/events`) is the cheap subset: the counters, `counts`, round
and commit counts, the last round, `ui` and `pr_synced_at` (the last GitHub sync, 10.5). `ui.last_seen` is the last
`/api/events` request not made by the CLI (`User-Agent: ccr-cli/…`); `connected` means within 60 s.

### 2.7 Flag (re-review)

`{id, view, path, side, line, start_line, reason, head, created_at, stale}`. A flag marks lines of a view as a change
that needs a closer look, so the user can review just those lines in context; an agent re-reviewing a pull request
flags each change it cannot vouch for. It is not a comment: no thread, resolving, rounds or export. `view` is `since`
by default, else `combined` or a listed commit, never `worktree`; `reason` is plain text; both ends of the range must
be rows the view's diff shows on that side. A flag belongs to the head it was made on: once `range.head` moves it is
`stale` and shows nowhere, since its lines may hold other code; so does a flag whose view is gone. A new-side flag
marks all its lines in its own view, and is carried to the same head lines in All changes and Since your last review
(a commit's lines through `map_line`) and to the listed commit that last changed each line (`blame_lines` at the
head), there only on rows those diffs show. An old-side flag shows only in its own view. `GET /api/flags` returns the
flags and, per view, the rows to mark.

---

## 3. Git extraction (`ccr/gitx.py`)

gitx runs git and returns plain dicts: ranges (2.1), the chain and its stats, each view's diff, file contents, blame
and `map_line`. Failures raise `GitError` with an HTTP status (400; 403 a path escaping the toplevel; 404 unknown
object; 413 too large; 415 binary). `ccr start` and `ccr serve` refuse git older than 2.30, the first whose
`rev-parse` takes `--end-of-options`; the rebuild of 3.2 needs 2.40 and checks that itself.

### 3.1 Invocation rules

* No shell. The environment drops every `GIT_*` variable (except `GIT_CONFIG_NOSYSTEM`, `GIT_SSH*`, `GIT_TRACE*`) and
  sets `LC_ALL=C`, `GIT_OPTIONAL_LOCKS=0`, no terminal prompt and no pager, and every call pins the config that would
  change what ccr parses, so neither the user's config nor their environment can.
* Diffs run without external diff drivers or textconv, with rename and copy detection, full blob ids and 3 lines of
  context; whitespace-ignoring views add `-w`. Output is decoded after splitting at NULs; a non-zero exit raises with
  git's stderr.
* **Argv safety**: a rev from outside gitx (HTTP, CLI) is first resolved to a full sha and only that sha goes to git,
  after `--end-of-options`; paths go after `--` (after `--end-of-options` for blame and `cat-file`). The server takes
  only revs of the current review (5.1).

### 3.2 Commands

* **The chain**: one `git log -z` for all commits, one `git diff-tree --stdin --raw --numstat -z` for all their file
  stats, whose record layout a test checks against the installed git.
* **A diff** (`diff_commit`, `diff_range`, the worktree's tracked part): one `git diff --raw -z -p`. The raw records
  give each file's skeleton; the patch, split at `diff --git` lines, hands its sections out in order, a type change
  (`T`) taking two. With `?ws=ignore` a second call without `-w` finds the whitespace-only files (`ws_only`).
* **The patch parser** also works alone (tests). A hunk consumes lines until both of its counters reach zero and never
  looks for headers meanwhile, as `--- x` can be content.
* **Worktree**: tracked changes from `git diff HEAD` (retried once on `index.lock`); untracked files from `git
  ls-files --others --exclude-standard`, skipping nested repositories and anything but regular files and symlinks,
  built in Python: a symlink's content is its target, a NUL in the first 8000 bytes makes it binary, more than 1 MiB
  `too_large`, else one hunk of added lines. Bare repositories have no worktree.
* **Since your last review** (2.1): `rebuild_tree` is `git merge-tree --write-tree --merge-base=<old_base>` (git ≥
  2.40), giving a tree, written even with conflicts, and the conflicted paths. The view diffs that tree against the
  head, and the conflicted paths against the reviewed commit instead.
* **Blame** (`blame_lines`, for flags): `git blame --line-porcelain`, one `-L` per run of lines.
* **File contents** (`show_file`, for expanding context): a blob through `git cat-file` (not a blob → 404), or the
  working-tree file, whose directory must resolve inside the toplevel (403); a symlink is shown as its target text and
  never followed, so nothing outside the repository can be read. Binary → 415, over 8 MiB → 413.
* **Languages**: a table of about 80 extensions and file names maps to highlight.js ids; `index.html` loads a grammar
  for each. The e2e driver checks that against its own copy of the ids (`LANGS` in `driver.mjs`), which a new id
  must be added to.

### 3.3 `map_line` (HEAD-relative locations)

`map_line(from_rev, to_rev, path, line)` says where a line is in another revision, following a rename of the file:
an unchanged line, shifted by the hunks wholly above it (`moved`, else `same`; a context line inside a hunk is `same`
even when shifted), a deleted line's place at the first added line of its hunk (`changed`, or `deleted` when the hunk
adds nothing), or `file-deleted`. Lookups between two full shas are cached for the process (4096 entries, then the
cache is cleared); the working tree is never cached.

---

## 4. Store (`ccr/store.py`)

One `sqlite3` connection guarded by a lock. A database file (by default `<session dir>/<key>.sqlite`) is created
0600, in WAL mode, and held with an exclusive `flock` on `<file>.lock`, so a second server on it refuses ("db in
use"). Tables: `meta` (schema version, repository path, `version`), `reviews` (range, cover letter, every commit it
ever listed with its chain index and subject, PR link, "since"), `comments` (the anchor in columns; snippet,
`moved_from`, `github` as JSON), `rounds` and `flags`, the last three keyed by their review.

Opening refuses a newer schema, and a database made for another repository path unless `--db-force`. Older schemas
are migrated in place (1: one review per repository; 2 lacks the PR columns, 3 the "since" one). `version` is
persisted, so a restarted server continues the count and an open page sees the change. Untrimmed diffs are cached per
view and whitespace setting, and dropped on reload.

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

On every load, a root anchored to a commit no longer listed (also one outdated by an earlier load) follows its commit
when it can: to the new commit at the same chain position with the same subject, else the only one with that subject.
A commit comment moves; a file comment moves when the file is in the new commit's diff; a line comment moves when a
row of that diff on its side reads as its snippet's first line — the only match, or the nearest within ±20 lines,
and for a range the following lines must match too (a comment without a snippet, such as one on expanded context,
never moves). A moved comment records `moved_from`; one that finds no place stays outdated. `load()` reports the
remapped and outdated ids and how many commits were added and removed.

### 4.5 HEAD locations (`locate=1`)

For a root line comment, `head_location` is `map_line` from the revision its side shows in its view (2.3) to the
current `HEAD`, read on each request. A new-side worktree comment is `live`; a failure is `unknown`.

### 4.6 Reviews

A database belongs to a repository, which is reviewed many times, and a server that is killed, times out or is stopped
leaves its database behind. So every comment and round belongs to a review, and everything the store reads — comments,
counts, `outdated`, rounds and their numbering — is scoped to the review it serves. On opening it takes the newest stored
review whose remembered chain shares a sha or a subject with the range (an amend, a rebase, a wider range, a branch
reviewed before), else the newest empty one (no comments, rounds or cover letter), else starts a new one; a range that
lists nothing continues the newest review that holds anything; nothing is deleted. An adopted review keeps its PR
link, "since" and flags, even an empty one adopted for an unrelated range. `review()` reports which (2.6), and
`ccr start` prints it (6.2).

---

## 5. HTTP server (`ccr/server.py`)

`ThreadingHTTPServer` on `127.0.0.1:<port>`, daemon threads.

### 5.0 Protocol

HTTP/1.1 with a 60 s timeout; every response has `Content-Length` and `X-Content-Type-Options: nosniff`. A request
body needs a `Content-Length` (411; no `Transfer-Encoding`), is at most 1 MiB (413, sent before reading, then the
connection closes) and, when non-empty, a JSON object (415, 400). Every error body is `{"error": "…"}`.

### 5.1 Security

* **Token**: 32 random hex chars per server. Every `/api/*` request carries it in `X-CCR-Token` (constant-time
  compare; else 401). `?t=` works only on `GET /`, where the page reads it and then removes it from the address bar.
* **Host**: must be `127.0.0.1`, `localhost` or `[::1]`, on every route, which defeats DNS rebinding. `Origin` and
  `Referer`, when present, must name one of those hosts too, ports not compared because SSH port forwarding changes
  them; `Origin: null` is refused everywhere, `Sec-Fetch-Site: cross-site|same-site` on the API. No CORS headers.
* **Revs and paths from clients**: `/api/file` takes `worktree` or a full sha the review itself names (a sha, a first
  parent, an `old_rev`/`new_rev`, with "since" open its tree and reviewed commit), and a relative path of a file in
  the review, with no `..`; `/api/compare` takes full shas of listed commits, their first parents and the range's
  base and head; `/api/commits/{sha}` resolves short shas against listed commits only.
* **Static files**: a path with an empty or `..` segment or a NUL is 404, and the resolved file must lie inside
  `static/`. Vendored files are cached for a year (immutable), so each is linked with a `?v=` that `index.html`
  writes literally: replacing one means changing its `?v=`. The page and its own scripts and styles are `no-cache`,
  so a browser reload shows a changed UI without a server restart.
* **HTML headers**: a CSP allowing only same-origin resources (images also `data:`), no framing, no base or form
  targets, plus `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and same-origin COOP and CORP. Hence no inline
  scripts and no `style=` attributes anywhere (setting CSSOM properties is fine).
* **Files**: the server runs with umask 077; the session directory is made 0700 and refused when it is not a
  directory, another user owns it, or group or others can still write to it; session files are written atomically,
  0600, like the log and the database.
* **Long polls**: at most 64 waiting `/api/events`; beyond that the answer is immediate, with `retry_after: 5`.

### 5.2 Routes

| Method | Path | Does |
|---|---|---|
| GET | `/`, `/static/<file>` | the page and its assets |
| GET | `/api/review` | Review (2.6), always whole; `loading` is true during every load, and before the first one `commits` is empty |
| GET | `/api/state` | `state()` (never 503) |
| GET | `/api/events?since=N&timeout=S` | long poll: `state()` with `changed: true` once `version > N`, at once when `N > version` (a page from another server run) or on stop; `changed: false` after `S` s (≤ 30, default 25) |
| GET | `/api/commits/{sha}?full=1&ws=ignore` | CommitDiff (2.3) of a listed sha, short sha or pseudo-commit; 503 before the first load |
| GET | `/api/commits/{sha}/file?path=P` | one untrimmed FileDiff |
| GET | `/api/commits/{sha}/github-lines?path=P` | PR mode: where in that file a GitHub comment cannot go, and why (10.2, 10.4) |
| GET | `/api/compare?base=X&head=Y` | the diff between two listed commits (`kind: "compare"`) |
| GET | `/api/file?rev=R&path=P` | a file's lines (for expanding context) |
| GET | `/api/comments?state=&round=&resolved=&author=&commit=&path=&outdated=include\|exclude\|only&locate=1&project=<view>` | `{version, generation, now, comments}`; with `project`, each also carries `view_anchor`, where it shows in that view (its own anchor there and for review comments, a line carried with `map_line` from another view, a file comment on the same path; else null), and `projected` when written in another view; a reply gets its root's |
| POST | `/api/comments` | `{body, anchor, author?, parent_id?, github?}` → 201 Comment |
| PATCH | `/api/comments/{id}` | `{body?, resolved?, anchor?, github?}` |
| DELETE | `/api/comments/{id}?cascade=1` | 204; 409 for a root with replies and no cascade |
| GET, POST | `/api/comments/{id}/github` | PR mode: where GitHub will put the comment (10.2); record a post or an update (10.3) |
| POST | `/api/submit` | `{verdict, summary}` → 201 Round |
| POST | `/api/reload` | `{range?, n?, worktree?, first_parent?}` (omitted = kept) → `{review, remapped: [ids], outdated: [root comments, also those outdated before], commits_added, commits_removed}`; a git error is 400 and keeps the old data |
| POST | `/api/cover` | `{text}`: the cover letter (≤ 64 KiB) |
| POST | `/api/pr`, `/api/since` | link a pull request (10); open or close "Since your last review" (2.1) |
| GET, POST, DELETE | `/api/flags` | list, add (400 for rows the view does not show), remove all (2.7) |
| POST | `/api/github/sync` | PR mode: store the mirrored discussion (10.5) |
| POST | `/api/restore` | `{payload, dry_run?}`: fill an empty review from an export (6.4) |
| POST | `/api/shutdown` | 202, then the server stops |

The cover letter, PR link, "since" and a restore bump `generation` too, so open pages re-render. PR routes answer 409
outside PR mode (a malformed sync payload is 400 first). Unknown routes are 404, other methods 501.

**Lifecycle** (`ccr serve`): resolve the range and open the database, applying `--cover`, `--pr` and `--since`
(errors exit 1), bind, write the session file (real port, own pid, token), load in a thread (meanwhile the routes that
need the extracted data answer 503, and reload and "since" wait for it), serve. A failed first load (an empty range,
say) prints `ccr: <error>` and exits 1, which `ccr start` shows from the log. On shutdown, SIGTERM or SIGINT the server
closes the database and deletes the session file only if it is still its own. `--idle-timeout S` (default a day, 0
never) stops a server no request reached for `S` s; long polls count.

---

## 6. CLI (`ccr/cli.py`)

`ccr <command>` (also `python3 -m ccr`, `bin/ccr`); `ccr --help` lists the options. Global options: `--repo PATH`
(default: the toplevel of the current directory), `--url`/`--token` to skip discovery (env `CCR_URL`, `CCR_TOKEN`),
`--json`. CLI requests carry `User-Agent: ccr-cli/<version>`, so they do not count as the page being open. Exit codes:
0 ok, 1 error, 2 timeout (`wait`), 3 no running session. `-` as a body or file reads stdin.

### 6.1 Sessions (`ccr/session.py`)

A session is keyed by the repository's realpath (`sha1[:16]`), so each linked worktree is its own. The session
directory (`$CCR_SESSION_DIR`, default `~/.cache/ccr/sessions`, 0700) holds `<key>.json` (the record: pid, port,
token, URL, …), `<key>.log`, `<key>.sqlite` (with its `.sqlite.lock`), the exports of `ccr stop`, and `<key>.lock`
while a start runs. A command finding a stale record (dead pid, a failed connection, a 401) deletes it and exits 3,
listing the live sessions; `ccr start` starts a new server instead. A live `ccr serve` that does not answer within
5 s is busy, not stale: the command fails and the record stays. The default port is `7700 + key %
300`, then the next 20 ports, then any free one; an explicit `--port` that is busy is an error.

### 6.2 Commands

* `ccr start`:
  1. Serialises concurrent starts with `<key>.lock` (one older than 30 s is ignored; after 35 s of waiting it fails).
  2. A running session is reloaded with the options given (the others kept; none: the pinned spec, so new commits
     appear; `--port`, `--db`, `--log` and `--idle-timeout` are ignored): `ccr: reusing running session (pid P)`, the
     reload lines, the since line when "since" is open, and the URL.
  3. Otherwise it resolves the range itself, so a bad one fails at once, prunes week-old databases (below), and spawns
     `ccr serve` detached, its output to the log, the token passed only in the environment (never in `ps`). It waits
     ≤ 10 s for the server (on failure: `ccr: server exited with code N — last log lines:` and the log tail) and
     ≤ 120 s for the load (`ccr: extracting commits…` on stderr meanwhile; past that it goes on).
  4. It prints `ccr: serving /abs/repo  (main..HEAD, 7 commits, +worktree)` and `ccr: url http://127.0.0.1:7777/?t=…`,
     then `ccr: note: …` for a range note. Between the two come, when the database held a review already (4.6),
     `ccr: resuming review #N started <ts> (C comments, R rounds)` or `ccr: new review #N — the database also holds
     review #M (<range>, C comments, R rounds) of a different change`, and the PR and since lines below.

  `--cover FILE` sets the cover letter; `ccr cover` replaces it later. It shows above All changes, with *Ask AI about
  the whole series* for comments on it, and `ccr export --md` prints it under `## Cover letter`. `--pr URL` (or
  `OWNER/REPO#N`) links a pull request (10) and prints `ccr: pr <url> (<owner/repo#N>): questions for Claude, GitHub
  comments for your pending review`. `--since REV [--since-at TIME]` opens "Since your last review" (2.1) and prints
  `ccr: since your last review of <R> (<at>): N files changed`, naming the paths that were not rebuilt, if any. All
  three apply on reuse too (where only the since line is printed); a bad `--pr` or `--since` exits 1 before anything
  starts, an unreadable `--cover` at once on reuse but on a fresh start only once the spawned server fails on it.
  Relative `--cover`, `--db` and `--log` paths name files in the caller's directory, though the server runs in the
  repository.
* `ccr serve` runs the server in the foreground (also `--token T`, for tests, `--verbose`, `--db-force`); a token from
  the environment, as `ccr start` passes it, is shown redacted, so a started server's log never holds it.
* `ccr stop` checks that the session's pid is the server's (else the record is stale: deleted, nothing signalled,
  exit 3; `--all` reports it and goes on with the others), writes `ccr export --md` and `--json` to
  `<session dir>/<key>-<time>.md|.json` (`ccr: exported to <path>` each; a failed export says `export skipped` and
  the stop goes on), asks the server to shut down, and SIGTERMs it after 5 s only if its command line is a
  `ccr serve`. The database stays. `--purge` writes no export and deletes the database, the logs and the session's
  earlier exports; `--keep-db` cancels `--purge`. Like `ccr start`, it then
  removes the database of every session not running (no live record, its lock not held) whose database and WAL have
  not changed for 7 days; `ccr start` never removes the one it resumes.
* `ccr reload` prints `ccr: N commits (was M), +a −r, K comments remapped, J now outdated`; `warning: R reviewed
  commits left the range` when some did, and one line per outdated thread, also those outdated before.
* `ccr comments` prints threads (6.3): a filter picks a thread when its root or any reply matches, and marks the
  matching comments `★`. `--unanswered` = unresolved, not outdated, last comment the user's (a posted GitHub comment
  answers itself, unless edited since). Default `--all --context 3`.
* `ccr wait` returns once a round after `--since-round N` exists (default: the count at call time): `ccr: round <n> —
  <k> new comments in <j> threads`, then `— <g> GitHub comments to check and post` when the round's threads hold some
  (PR mode), and the round's threads, earlier comments as context, the new ones marked `★ new in round n`. A verdict
  follows `<n>` only when it is not `comment`, which the page always sends. A timeout (default 590 s, so one wait fits
  the agent's 10-minute Bash call; 0 never) exits 2 with the counts on stderr. Lost connections are retried every
  second; a dead pid or 30 s of failures exit 3 (`ccr: server gone`), and so does a 401 (`server was replaced while
  waiting (new token)`). After 30 s without the page ever polling it says `ccr: UI not opened yet`, once. `--any`
  returns on any change, with the threads changed since the call.
* `ccr reply ID` refuses a body the same author already posted in that thread, unless `--force`. `ccr reply --batch`
  takes JSON `[{"id", "body", "resolve"}]` or Markdown sections `## <id> [resolve]`, posts them in turn, prints
  `<id>: replied[, resolved]` or `<id>: ERROR …` per item, goes on after errors and exits 1 if any failed.
* `ccr comment`, `resolve`, `unresolve`, `edit`, `delete`, `move` change comments; `comment --github` makes a GitHub
  comment (10.2, with `--as user`).
* `ccr export`: `--md` is a `# Review — …` title (with `— PR <owner/repo#N>` in PR mode), the cover letter, the
  rounds and every thread (6.3); `--json` is the shape of 6.3; `-o` creates the file 0600.
* `ccr flag` adds a flag (2.7), `ccr flag --clear` removes them all, and `ccr flags` lists them, saying how many are
  hidden as stale.
* `ccr status`, `sessions` (pruning stale records; exit 3 if none), `logs`, `open`; `ccr restore` (6.4), `gh-sync`
  (10.5) and `gh-post` (10.3).

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
#### [id: …] claude · commit · R0 · unresolved · 0 replies · last: claude
…

## All changes (combined)
## Uncommitted changes
## Outdated (anchored to commits no longer in the range)
#### [id: …] user · 1b2c3d4e5f src/x.py new:10 → HEAD src/x.py:10 · R1 · unresolved · 0 replies · last: user
```

Groups come in this order: review-level comments, Since your last review, the commits, All changes, Uncommitted changes,
outdated. The snippet shows `--context N` rows around the anchored ones, which start with `>`; without a diff the stored
snippet is shown instead, in a fence longer than any backtick run in it. A line thread with no snippet whose body opens
with a fence gets an empty one, so `ccr restore` never takes the body's fence for the snippet's. The HEAD arrow is
qualified `(moved)`, `(changed near)`, `(deleted)`, `(file deleted)` (no line) or `(live)`, nothing for `same`, and left
out when `unknown`. Every repository- or user-derived string is cleaned of control characters and of the bidi embedding,
override and isolate controls (one-line fields also of newlines, shown as `␤`); a body line starting with `#` is written
`\#…`, so outside code blocks a line starting with `#` is always structure (the stored-snippet block is not escaped);
reply bodies are indented two spaces.

In PR mode the title carries `— PR <owner/repo#N>`, and right after the author each user root says what it is:
`question`, `GitHub comment (not posted)` or `GitHub comment (posted: <url>)`, with `; edited since, the update not
posted yet` when edited after posting; a user's reply reads `GitHub reply (…)` the same way, or nothing for a
question. A mirrored author reads `@login (GitHub[, you])`, a mirrored root `GitHub thread (outdated, was new:N, shown
on the file, resolved there, pending, deleted there)` or `GitHub review (<state>)`, a mirrored reply `on GitHub
(pending)` when it is; mirrored comments show `on GitHub` instead of a round.

`--json` prints `{"review" (without files), "rounds", "comments", "threads": [{"root", "replies", "last_author",
"answered"}]}`, threads referring to comments by id; `comments` is every comment, only `threads` is filtered.
`ccr wait --json` adds `round` (with `--any`: `version_from`, `version_to`).

### 6.4 Restoring a review from its export (`ccr restore`, `ccr/restore.py`)

A review lives in its database, which `ccr stop --purge`, the weekly prune (6.2) or a lost file can drop. `ccr stop`
writes two exports first: the Markdown one, and the JSON one, which holds every stored field of the comments and rounds
(not flags, nor the review's chain memory, PR link or "since"). `--purge` writes none and deletes the earlier ones, so
only an export kept elsewhere survives it. `ccr restore FILE` loads either into the running review (`POST
/api/restore`); `restore.py` parses what 6.3 writes.

* The review must have no comments or rounds of its own (mirrored GitHub comments may be there); an export of a pull
  request needs the review linked to it, and an abbreviated commit (a Markdown export's) must resolve in the
  repository (409 otherwise); a full sha is taken as is, and a commit outside the range makes an outdated thread.
* Comments keep their ids (a new one when the id is taken in the database, reported), parents, authors, bodies,
  anchors, states, rounds, times, resolved flags and GitHub records (a JSON export also `moved_from`); rounds their
  number, time, verdict and summary; an empty cover letter takes the export's.
* A Markdown export loses a root's creation time (reconstructed before its replies, edit and round), the snippets
  (captured again as in 4.2, else read from the file at git), a round's base, head and commits, most of a posted
  comment's GitHub record, what cleaning stripped, and the node ids of mirrored comments.
* Mirrored comments: a JSON export's keep their node ids, so the next sync updates them as usual. A Markdown export's
  are matched to mirrored roots by login and text (and place when needed), their replies coming from the sync; one
  that matches nothing is restored with its mirrored replies, marked `deleted`, when it holds replies of the user or
  Claude, else left to the sync. A mirrored copy of the user's posted comment, made by a sync before the restore, is
  dropped; the next sync joins it to the restored comment.
* The CLI dry-runs first, so a refusal changes nothing. In PR mode it syncs (10.5) before restoring a Markdown export
  with mirrored comments, and after every restore, so posted comments learn their threads again. It reports what it
  restored, matched, dropped, renamed and left outdated.

---

## 7. Web UI (`ccr/static/`)

One page: vanilla JS in `app.js`, no framework. It must stay fast on 100 files and 10k lines, so events are delegated
(one listener per event type on `#main`, one `keydown` on the document), each file body is built as one HTML string,
every dynamic text escaped, and assigned once with `innerHTML`, and threads and the header are patched piecemeal. No
`style=` attributes (CSP, 5.1): computed styles go through CSSOM properties or classes.

### 7.1 Boot & token

`theme.js` sets the theme (`auto` follows `prefers-color-scheme`) before the first paint. `app.js` takes the token
from `?t=` (else from `localStorage`, per port), stores it and removes it from the address bar. Without a valid
token, or on any 401, polling stops, the stored token is dropped and a full-page notice says to open the URL
`ccr status` prints, as the server may have restarted. **Copy link** builds `origin/?t=<token>#<hash>`, so the link
works in another browser; links inside the page are bare `#hash` links, which rely on the token stored for the port.

### 7.2 Layout

A top bar (view mode, wrap, hide whitespace as `?ws=ignore`, theme, reload, **Submit**, copy link; view settings
persist in `localStorage`), a resizable sidebar and the main pane.

* *Commit chain*: All changes first, the commits oldest first, Uncommitted changes last with `--worktree` (greyed,
  "(clean)", when empty); Since your last review, when open, is a group of its own above them, with the date and time
  of that review (or `of <short sha>` without `--since-at`). A `•` marks a commit new since the last round, `⚑N` a
  view showing flags. Shift+click on another real commit opens a compare view of the range between them (no comments
  there), which a plain click on a commit or the banner's *Exit compare* leaves.
* *File tree* of the selected view: single-child directories merged (`src/ccr/static`), folder state persisted. While
  the filter is set the tree is a flat list and non-matching file cards are hidden too.
* *Main pane*: `#main` is the diff's only vertical scroller, so file headers can be sticky. The header holds the
  commit, *Ask AI about this commit*, and for pseudo-commits what they are: Since your last review names the review it
  starts from, the base the reviewed version was rebuilt on, the head, and the paths compared with the reviewed commit
  itself. All changes has *Ask AI about the whole series* instead (review-level comments, shown under the cover
  letter), the cover letter (or a hint that the agent can set one with `ccr cover`) and a note counting outdated
  comments.
* *Flags*: a view showing flags has **⚑ Next (N)**, which selects the next flag's lines, wrapping around; flagged rows
  get orange stripes on both edges (in split view on the flag's side) and each flag's first shown row a ⚑ whose
  tooltip is the reason; flags made on an earlier head raise a banner instead.
* *File cards*: a binary file's body says it is not shown, a `too_large` one offers *Load anyway*, a file without hunks
  (mode or whitespace only) gets a note. Bodies render lazily: each gets a placeholder of its estimated height (at most
  2000 px), and an `IntersectionObserver` (1500 px ahead) renders it once, synchronously; rendered bodies stay.

### 7.3 Diff table

`table.diff`, fixed layout and full width, four columns in either view so thread rows span them alike. With wrap off
the diff body scrolls sideways while its header stays sticky.

* **Row model**: a hunk's lines form blocks: a run of context lines, or a change block — a run of deletions and the
  run of additions after it (either may be empty) — where the i-th deletion pairs with the i-th addition. Unified view
  has one row per line, deletions before additions; split view has one row per pair, a missing side hatched.
* **Thread and editor rows** follow their line. In split view such a row sits under its own side, the other half
  empty, so rows for both sides of one pair stack, the old side's first.
* **Hunk rows** expand the hidden lines between hunks: one button for a gap of ≤ 20 lines, else 20 up, 20 down or
  all; also above the first hunk and to the end of the file (20 down or all, as that gap's size is unknown until the
  file is fetched). Expansion fetches the file once (2.3), adds ordinary context rows, which take comments, and
  re-renders only that body, keeping editors, the selection and the scroll position.
* **Highlighting** tokenises all of a side's diff lines at once (expanded context included), so multi-line strings and
  comments highlight correctly, then splits them back into lines. Plain text instead without highlight.js, for an
  unknown language, past 500000 characters, or when the line count comes out wrong; lines over 1000 characters stay
  plain. No element gets the class `hljs`, whose background would paint over the diff colours.
* **Word diff** on paired lines up to 400 characters: an LCS over word, space and punctuation tokens marks the changed
  pieces, unless more than half of either side changed.
* **Gutter**: one shared set of buttons per file moves into the hovered row's number cell: a **[+]**, which opens on
  hover or click into *Ask AI* (in PR mode also *GH comment*, right of it). A click where hovering just opened it
  (within 600 ms) stays with the [+] rather than reaching *Ask AI* under the pointer. *Ask AI* opens the editor on the
  selected range if one ends on that row, else on the line. Hidden in compare views.
* **Side rule**: additions anchor to `new`, deletions to `old`; a context row to `new` through its [+], to `old`
  through its old line number (or the left [+] in split view).
* **Selecting lines** (one side only): press on a line number and drag; Shift+click extends; a click selects one line
  and puts it in the hash. Pressing in a code cell of split view makes only that side selectable, so copied text is
  one side's.
* **Threads** attach to their row by anchor key (7.8), a range under its end line. A thread whose row is not rendered
  (an anchor drifted in `combined` or `worktree`, whitespace-only files, hidden large files) is not shown; `ccr
  comments` still lists it. The page always fetches comments projected into the selected view (`project=`, 5.2) and
  places each root at its `view_anchor`; a projected root carries a *from …* tag opening the view it was written in.
  A reply is placed by its `parent_id`: the server copies the root's anchor (the page sends the root's own anchor
  too, which is ignored).

### 7.4 Comments, editors, threads

* **Editor**: **Write** and **Preview** tabs. The textarea grows with its content (up to 60% of the viewport); Preview
  renders the same Markdown as comment bodies. While text is selected, **Bold** and *Italic* (and Ctrl/⌘+B, Ctrl/⌘+I)
  wrap each selected line in `**…**` / `_…_`, or unwrap it, through `execCommand('insertText')` so Ctrl+Z undoes it.
  The submit button (**Add comment**, **Reply**, **Save**; in PR mode **Ask AI**, **Add GH comment**, **Add GH
  reply**; Ctrl/⌘+Enter) posts; **Cancel** (Esc) closes and keeps the draft, unless the text is empty. On success only
  the threads at that place, the counts and the badges re-render; on error the editor stays open with a toast. Open
  editors are kept in state, so every re-render recreates them; drafts persist in `localStorage`, and a button with a
  draft shows a dot.
* **Thread**: relative times use the server's `now`, as the browser's clock may differ. Tags: *Pending* (not in a
  submitted round yet, though Claude can read it), *R<n>*, *edited*, *moved*, *New* (7.5). Deleting a root takes its
  replies along (confirmed). Resolved threads collapse to one line unless they hold unseen comments.
* **Markdown** (own renderer; everything escaped first): paragraphs and line breaks, ATX headings, `code`, fenced code
  (highlighted when the language is known), **bold**, *italic*, `- ` and `* ` lists, `> ` quotes, GFM tables (at most
  as wide as the comment), and links only to `http(s)` URLs, opened in a new tab without referrer. No autolinks. A
  7–40 hex token naming a listed commit links to it.
* **Counts** (threads, pending, unresolved over roots not outdated) are derived from the comments after every change:
  a commit's in the view a thread was written in, a file's where it renders (its `view_anchor`). The Submit badge
  counts pending comments.

### 7.5 Live updates, seen-tracking, reconciliation

* Only a visible tab polls `/api/events`; one tab per origin leads (a Web Lock) and shares what it learns with the
  others over a `BroadcastChannel` (without those, each visible tab polls). Hidden tabs re-sync when shown.
* On a change the page refetches the comments and the flags; a new `generation`, round count or `pr_synced_at` also
  refetches the review, and a new `generation` the selected diff (All changes when that commit is gone), re-rendering
  while keeping the scroll position, editors and selection, with a toast (*Chain reloaded*, *Cover letter updated*). A
  new `server.started_at` re-initialises the page, keeping drafts.
* Comments are reconciled by id, per place: only the threads at a place whose members, edits, resolution, states,
  rounds or GitHub status changed re-render, and editors are never touched. New comments by Claude raise *"Claude
  replied to N threads (k resolved) — Show"*.
* **Seen**: the newest comment rendered while visible for a second is remembered per repository; newer ones are
  marked *New* (on the comment, its thread, its commit, file header and file-tree count) and keep resolved threads
  open.
* Disconnected: a banner that retries, and after 30 s says the server may have stopped; reconnecting refetches what
  changed.

### 7.6 Navigation, hash, keyboard, submit

* **Hash**: `#<sha>`, `#<sha>/<path>`, `#<sha>/<path>:<o|n><line>[-<line>]` or `#compare:<a>..<b>` is the selection.
  Commit and file changes push history entries, line clicks replace them, so Back and Forward move between commits.
  Navigating to a target expands what it needs (file, collapsed thread, context around a missing row).
* **Commit switch** opens the commit at its top, so the message is read first; only a target with a file, line or
  thread scrolls further.
* **Keyboard**: Ctrl/⌘+Enter posts and Esc cancels the focused editor. Single-key shortcuts (`j`/`k` files, `]`/`[`
  commits, `n`/`p` threads, `N`/`P` unresolved threads, `c` comment, `x` collapse, `u` view, `w` wrap) exist but are
  off, behind `KEYBOARD_SHORTCUTS` at the top of `app.js`.
* **Submit** bundles every pending comment into the next round (`{verdict: "comment"}`) and wakes `ccr wait`; it is
  disabled while nothing is pending or a submit is running. Claude can read pending comments before that (`ccr
  comments`), which the skill forbids it to act on.

### 7.7 Theme

Colours are custom properties on `:root[data-theme=light|dark]`, bar a few fixed whites and shades; light resembles
GitHub and dark GitHub dark-dimmed. Syntax colours come from the two vendored highlight.js sheets, one enabled per
theme. Animations stop under `prefers-reduced-motion`.

### 7.8 Contracts shared with the e2e driver

The driver relies on the ids, classes and data attributes `app.js` renders, on `body[data-ready="1"]` after the first
full render (also set on the token notice and a failed first load), on the anchor keys that name a thread's place
(`data-key`): `line:<commit>|<path>|<side>|<endLine>`, `file:<commit>|<path>`, `commit:<commit>`, `review:`, on
`window.ccrState` (`comments`, `currentFile`, `currentThread`, `githubLines`, `token`), and on the button labels,
aria-labels and toast texts it matches. Renaming one means changing the driver too.

---

## 8. Agent loop (`skills/review-commit-series/SKILL.md`)

The skill is the agent's side of the loop and of PR mode; it is the reference, and this document does not repeat it.
It quotes ccr's exit codes, the `start`, `wait`, `reply --batch` and `gh-post` lines, errors such as `range … is
empty` and `identical reply already exists on this thread`, and the 6.3 Markdown and JSON, and says what to do on
each: changing one of them means changing the skill. `--since` and flags (2.1, 2.7) serve a separate `/re-review`
skill, outside this repository, which ccr's notices about stale flags name; changing them means checking it too.

Claude Code loads the checkout as a plugin from `~/.claude/skills/ccr` (also `claude plugin marketplace add <checkout>`
and `claude plugin install ccr@ccr-local`, or `claude --plugin-dir <checkout>`), with `bin/` on PATH while the plugin
is enabled; elsewhere use `bin/ccr` or `pip install -e .`.

---

## 9. Tests (`tests/`)

`python3 -m pytest -q`; everything passes without network access.

* `conftest.py` builds the fixture repositories: one with most kinds of change git can show, plus a dirty worktree,
  and a re-reviewed pull request rebased twice, the second time with a conflict.
* `test_gitx.py`: the parser on hand-written patches, the chain, stats against `--numstat`, each diff kind, file
  contents, ranges, `map_line`, the rebuild and blame.
* `test_store.py`: comments and their validation, snippets, submitting, outdated and re-anchoring, the counters,
  `wait`, head locations, reopening a file database, reviews, PR mode (targets, refusals, the mirrored discussion,
  migrations), "since" and flags; that `github_lines` blocks exactly the rows `github_target` refuses.
* `test_server.py`: a real server on port 0 — authentication, host and origin checks, body rules, every route, static
  path traversal, MIME types and headers.
* `test_cli.py`: the commands end to end through `bin/ccr` against a background server, incl. the token never in `ps`,
  file modes, reuse, stop and its exports, restore, `--since` and flags, and PR mode through a `gh` shim on PATH that
  runs `fake_gh.py`.
* `test_render.py` (golden Markdown), `test_restore.py` (6.4), `test_github.py` (10.3 posting and the 10.5 fetch, in
  process against `fake_gh.py`, an in-memory GitHub answering ccr's named operations; 10.2 is `test_store.py`'s).
* `test_e2e.py` runs `tests/e2e/driver.mjs` (headless Chromium over CDP from Node ≥ 22, no dependencies) against a live
  server in three scenarios — a review, PR mode, and Since your last review with flags — and checks the driver's
  report (no failed step, no console error) and the state the browser left on the server. Skipped without Chromium
  (`CCR_CHROME` names one) or Node; an older Node fails it.

---

## 10. PR mode (`--pr`, `ccr/github.py`)

PR mode makes ccr a reading aid for someone else's pull request. `ccr start --pr URL` (or `OWNER/REPO#N`) links the
review to it; the agent serves the pull request's own chain — its head checked out in a worktree of its own,
`--range <merge base>..HEAD`, so All changes is the diff GitHub shows — and changes no code. A user's comment is one
of two things:

* a **question** for Claude: the default, and the only kind on a commit, on the whole pull request (bar a GitHub
  review thread shown there because its file left the pull request), and for a reply in their threads, under a review
  body or in a deleted thread (10.5). The agent answers it in the thread, before the round's GitHub comments; nothing
  reaches GitHub.
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

`Review.pr` is `{url, host, owner, repo, number}`, kept per review, plus `synced_at` and `first_synced_at` once synced
(10.5). `Comment.github` is null for a question, `{"status": "local"}` for a GitHub comment or reply not on GitHub
yet, and once posted `{"status": "posted", …}` with GitHub's ids and URL, the place it went to and the commit.

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
  failed rule above.

The rules run when a GitHub comment is created, switched from a question or moved (400 or 409 with the reason, so the
editor stays open), and again for `GET /api/comments/{id}/github`, which also returns the head, base, anchored lines
and body. `GET /api/commits/{view}/github-lines` applies them to every row of a file for a one-line comment, so the
page can grey out *GH comment* in advance (10.4). Only the user's comments can be GitHub comments.

### 10.3 Posting (`ccr gh-post`)

`ccr gh-post` first syncs the discussion as `ccr gh-sync` does (10.5), so the agent knows GitHub's latest state: when
the sync brings news — comments added, edited or deleted on GitHub since the last sync — it lists them and posts
nothing, so the agent reads them first; a failed sync posts nothing either; `--dry-run` lists them and goes on. Every
sync, a dry run's or `gh-sync`'s too, counts as the last one, so news is listed once. It posts only GitHub comments
submitted in ccr and not posted yet, one at a time, each re-read from the server right before. GitHub is
reached only through `gh api`, as the account `gh auth` holds (`--hostname` for a host other than github.com):
GraphQL operations named `Ccr…`, which `tests/fake_gh.py` answers by name, plus one REST call for a merge base git
cannot compute. Posting a comment:

1. Read the pull request (head, base, its last 100 commits), the user's pending review with all its comments, and how
   many reviews the user has submitted.
2. Refuse, posting nothing, when `range.head` is not one of the pull request's commits (the wrong pull request, or a
   rewritten one; past its last 100 commits ccr cannot tell, and refuses), when the pull request diff does not start
   at `range.base` (its merge base, computed locally, else asked of GitHub), or when the pending review is on another
   commit than `range.head` (these two only for a new thread: a reply is placed by its thread, 10.5). A head that
   moved on is only a note: the comment goes on the commit ccr reviewed.
3. A comment in the pending review with the same body at the same place counts as posted already, and is recorded.
   GitHub does not say a comment's side, so "the same place" also needs its diff hunk to end on the target's row.
4. Without a pending review, start one on `range.head` with no body and no event, so it stays pending. One GitHub
   starts on another commit is undone and refused.
5. Add the comment as a new thread. When GitHub refuses it and step 4 started the review, the review, still empty, is
   deleted again, so a refusal leaves GitHub as it was.
6. From here the comment is on GitHub, and nothing raises: the thread GitHub returns must sit where it was asked to
   (when `range.head` is the head), and a re-read must show the same pending review holding the new comment, every
   earlier comment unchanged and the submitted-review count unmoved. Each discrepancy is a `warning`; the comment is
   posted and recorded anyway.
7. Record the post. If recording fails, the output says posted but not recorded. The comment is still `local` then,
   so the next `ccr gh-post`'s sync mirrors it as news from the user and posts nothing; the run after that finds it
   (step 3) and records it, and the sync after that drops the mirrored copy (news again).

A posted comment edited since (10.1) is updated instead: refused when it is no longer in the user's pending review
(submitted, deleted or removed there), a no-op when GitHub has the new body already, else updated and re-read (the
new body there, the other comments unchanged; each discrepancy a `warning`).

The output has a line per comment (`<id>: posted <place> → <url>`, the place `<path>:<line> (RIGHT|LEFT)`, `<path>
(file)` or `reply to @login on <path>`; also `already posted`, `found already in your pending review`, `updated`, or
`<id>: ERROR …` for a question, a pending comment, an unknown id or a refusal), its notes and warnings, and, when
something was posted or updated, where to submit the review; any error or warning exits 1. `--dry-run` shows the
place, the anchored lines and the verbatim body. Nothing else on GitHub is submitted, edited or deleted.

### 10.4 UI

With a pull request linked, the top bar links to it, the gutter [+] opens into *Ask AI* and *GH comment*, the file
header has *GH comment* beside *Ask AI*, and the whole-review button reads *Ask AI about the whole pull request*. A
new comment's editor on a line or a file has an *Ask AI | GH comment* switch that keeps the text; writing a GitHub
comment it says the text goes verbatim to the pending review once Claude has checked it. A reply editor in a thread
that takes GitHub replies has an *Ask AI | GH reply* switch. Where GitHub would refuse a GitHub comment (10.2), *GH
comment* stays visible but inactive, in the gutter, the switch and the file header, and says why on hover and on
click; the page asks per file and view once a card renders, and rows it did not ask about (expanded context) stay
active and are checked when written. A draft remembers its kind.

A thread that takes GitHub replies (10.5) offers **GH reply** beside **Ask AI**; a collapsed thread's line offers only
**Ask AI**, which expands it. The user's roots, and their replies in threads that take GitHub replies, are tagged
*Question* or *GitHub · not posted*; a posted one links to GitHub (*GitHub ↗*), stays editable while its review is
pending, shows *edit not posted* until `gh-post` updated it, and its Delete removes it from ccr only. Every comment
carries a channel: GitHub (GitHub comments and replies, posted or not, and mirrored comments) on grey, Claude
(questions and Claude's comments) on blue; an editor takes the colour of what it writes, so the two dialogues read
apart even inside one thread. When comments get posted, a toast says to submit the review on GitHub, with a link.

### 10.5 The pull request's discussion (`ccr gh-sync`)

`ccr gh-sync` mirrors the pull request's discussion into the review, read-only: every review thread with all its
comments, and the non-empty bodies of submitted reviews (not the Conversation tab's comments), stored as comments by
`github` with GitHub's body, times and author, and a `github` record (`status: "remote"`, ids, URL, login, whether
the viewer wrote it, pending or submitted; on a thread's first comment also the thread, its state on GitHub and its
placement).

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
* **GitHub replies.** The user's reply can be a GitHub reply in a review thread on GitHub (a mirrored one not deleted
  there, or a posted comment once ccr knows its thread) and in any other thread on a line or a file. There the
  thread's first GitHub comment, root or reply, starts the thread on GitHub at the root's place (checked as in 10.2
  when written); later GitHub replies wait until it is posted (409, or *run ccr gh-sync* while ccr does not know the
  thread yet), then go into it. Replies are checked and posted like GitHub comments, into the pending review, which
  may be on another commit, as a reply is placed by its thread; one already there with the same body under the same
  first comment counts as posted. Elsewhere — on a commit or the whole pull request, under a review body, in a deleted
  thread — a reply stays in ccr (400).
* **UI.** A mirrored comment shows `@login` (*(you)* for the viewer), a *GitHub ↗* link and its state there
  (*outdated*, *resolved there*, *pending there*, *deleted there*, or a review body's), and has no Edit or Delete; nor
  has Delete a root whose thread holds a mirrored reply, since it would take the replies along. Outdated threads,
  review bodies and deleted threads start collapsed unless they hold unseen comments. Comments a later sync brings
  are *New*, with a toast; those older than the first sync are not.
* **For the agent**: the labels of 6.3.

# ccr — Commit Chain Reviewer — Specification (v2)

`ccr` is a local, zero-dependency code-review tool designed for agentic workflows.
An agent (Claude Code) prepares a chain of commits, starts `ccr`, and hands the user a URL.
The user reviews in a GitHub-like browser UI: a commit chain, per-commit grouped diffs, inline
line/file/commit comments, and a **Submit** round. The agent then reads *all* comments
in one call (`ccr comments` / `ccr wait`), fixes things, replies and resolves threads via the CLI,
reloads the diff, and the loop repeats. No GitHub detour.

Design constraints:

* **Python 3.10+ standard library only** on the server/CLI side (no pip deps). Vanilla ES2020 JS,
  no build step, on the client side. `highlight.js` is vendored under `ccr/static/vendor/`.
* **Simplest possible database**: comments and rounds live in **SQLite** via the stdlib `sqlite3` module
  (part of the default `python3` package on Fedora and Ubuntu — nothing to install). The database file lives
  in the private session directory (mode 0600), so a crashed or killed background server loses nothing; it is
  deleted by `ccr stop` (after an automatic Markdown export), which makes the data **session-limited**
  (start → stop). `--db :memory:` gives a pure in-memory store; `--db FILE` an explicit file.
* **Local only**: bind `127.0.0.1`; every `/api/*` call requires a per-process random token sent in a header.
* Works on any git repository (git ≥ 2.24), on any commit range, including uncommitted work-tree changes.
* ccr never modifies the working tree, refs, or index *contents*; git itself may refresh the stat cache in
  `.git/index` while diffing the worktree (`GIT_OPTIONAL_LOCKS=0` is set to minimise even that).

---

## 1. Repository layout

```
commit-chain-reviewer/
  README.md                 user-facing docs (install, usage, agent loop, SSH forwarding)
  SPEC.md                   this file
  pyproject.toml            package "ccr", console script ccr = ccr.cli:main, no deps, python>=3.10
  bin/ccr                   shim: PYTHONPATH=<repo root> exec python3 -m ccr "$@"  (usable without install)
  ccr/
    __init__.py             __version__
    __main__.py             from .cli import main; main()
    cli.py                  argparse CLI (section 6)
    server.py               ThreadingHTTPServer + request handler, routing, auth, static files (section 5)
    gitx.py                 git subprocess wrappers + unified-diff parser -> plain dicts (section 3)
    store.py                ReviewStore: review data cache, comments, rounds, version + Condition (section 4)
    session.py              session-file discovery, background start/stop, port pick, token gen (section 6.1)
    render.py               Markdown/JSON rendering of comments for `ccr comments|wait|export` (section 6.3)
    static/
      index.html            single page app shell (no inline scripts, no inline styles)
      theme.js              tiny pre-paint theme bootstrap (loaded synchronously in <head>)
      app.js                all UI logic (section 7)
      style.css             all styles (light + dark via CSS custom properties)
      favicon.svg
      vendor/               highlight.min.js, github.min.css, github-dark.min.css, lang-*.min.js
  .claude-plugin/plugin.json  Claude Code plugin manifest (name "ccr"; bin/ is put on PATH while the plugin is enabled)
  .claude-plugin/marketplace.json  single-plugin marketplace so `claude plugin install ccr@ccr-local` works from a checkout
  skills/review-commit-series-commit-series/SKILL.md    Claude Code skill (`/review-commit-series [range]`) describing the agent loop (section 8)
  tests/                    pytest suite (section 9)
    conftest.py             fixture repo builder
    test_gitx.py test_store.py test_server.py test_cli.py test_render.py
    e2e/driver.mjs          headless-Chromium CDP driver (Node 22, no deps)
    test_e2e.py             runs the driver against a live server (skipped if chromium is missing)
```

---

## 2. Vocabulary & data model

All timestamps are ISO-8601 UTC strings, second precision, `Z` suffix (`2026-09-03T13:45:00Z`), produced by one
`utcnow()` helper. All JSON keys are `snake_case`. Unknown keys must be ignored by clients.

### 2.1 Revisions and pseudo-commits

A review shows a **range** `base..head` as an ordered chain of commits (oldest → newest) plus pseudo-commits:

| `sha` value      | Meaning                                                                | Present when |
|------------------|------------------------------------------------------------------------|--------------|
| `<40/64-hex>`    | a real commit; diff is against its **first parent** (root: empty tree) | always |
| `combined`       | `git diff base head` — everything in the range at once ("All changes") | always (even for 1 commit) |
| `worktree`       | uncommitted changes vs `HEAD`: staged + unstaged + **untracked** files  | `--worktree` given |

**Range spec grammar** (`--range SPEC` or `-n N`):

* contains `...` → `(A, B) = spec.split('...', 1)`; empty side = `HEAD`; `base = merge-base(A, B)`, `head = B`.
* contains `..` → `(A, B) = spec.split('..', 1)`; empty side = `HEAD`; `base = A`, `head = B`.
* otherwise → `base = spec`, `head = HEAD`.
* `-n N` → `depth = git rev-list --count --first-parent HEAD`; if `N >= depth` → `base = null` (whole history),
  else `base = HEAD~N`. `N` counts first-parent steps; the chain may contain more than N commits when merges are present.
* Each side is verified with `git rev-parse --verify --quiet --end-of-options <X>^{commit}`; spec strings starting
  with `-` or containing whitespace/NUL are rejected with `GitError`.
* **Non-ancestor base**: after resolving, `mb = merge_base(base, head)`; `mb is None` → `GitError("base and head have
  no common ancestor")`; `mb != base` → use `mb` as the effective base for both the commit list and `combined`, and set
  `range.note = "base <short> is not an ancestor of head; using merge-base <short>"`.
* **Pinned spec**: `resolve_range` returns `spec` in pinned form so reloads never drop reviewed commits:
  `-n N` → `<sha of base>..HEAD`; bare `A` → `A..HEAD`; default detection → `<ref>..HEAD`. `range.given` keeps the
  user's original text. Reload without `--range` re-resolves the pinned spec (base fixed, `HEAD` moves).
* **Default detection** when both are omitted: (1) `@{upstream}..HEAD` if the upstream exists and the range has ≥ 1
  commit; (2) the first of `main`, `master`, `origin/main`, `origin/master`, `origin/HEAD` that exists and whose
  `..HEAD` has ≥ 1 commit; (3) otherwise `GitError("cannot infer a range; pass --range or -n")`.
* A resolved range with zero commits and no `--worktree` is an error (`range X..Y is empty`).
* Ranges with more than 2000 commits are refused (`range too large; narrow it with --range`).

Commits listed = `git rev-list --reverse --topo-order [--first-parent] base..head --` (merges included, flagged).
`--first-parent` (option `first_parent`) is off by default.

### 2.2 CommitMeta

```jsonc
{
  "sha": "9fceb02…40hex",          // or "combined" / "worktree"
  "short_sha": "9fceb02d3a",       // 10 chars; "combined"/"worktree" for pseudo-commits
  "kind": "commit",                // "commit" | "combined" | "worktree"
  "parents": ["…"],                // [] for pseudo-commits and root commits
  "is_merge": false,
  "shallow_boundary": false,       // true for a parentless commit that is a shallow-clone boundary
  "author": {"name": "…", "email": "…"},
  "author_date": "2026-…Z",        // null for pseudo-commits
  "commit_date": "2026-…Z",        // null for pseudo-commits
  "subject": "first line of message",   // "All changes" / "Uncommitted changes" for pseudo-commits
  "body": "rest of message",       // may be empty; internal newlines kept; truncated at 64 KiB
  "stats": {"files": 3, "additions": 40, "deletions": 12},
  "files": [FileStat, …],          // ordered as git orders them
  "comment_count": 2               // root comments (any state) anchored to this sha, excluding outdated
}
```

`subject` = first line of `%B` (rstrip); `body` = the rest with leading/trailing newlines stripped. Mailmap is not applied.

`FileStat`:

```jsonc
{"path": "src/new.py", "old_path": "src/old.py",   // old_path only for R/C; else null
 "status": "M",           // A M D R C T ; untracked files → "A"; conflicted files appear as "M" (content has markers)
 "score": 92,             // similarity score for R/C, else 0
 "additions": 10, "deletions": 2,    // 0/0 for binary
 "binary": false,
 "old_mode": "100644", "new_mode": "100755",   // from --raw; "000000" (absent side) → null; 120000 symlink; 160000 submodule
 "old_blob": "<40hex|null>", "new_blob": "<40hex|null>"}   // blob ids (--abbrev=40); null for absent side; worktree new side: null
```

### 2.3 CommitDiff (`GET /api/commits/{sha}`, `GET /api/compare`)

CommitMeta plus `files: [FileDiff]` (replaces the FileStat list). A `FileDiff` is a `FileStat` plus:

```jsonc
{
  "lang": "python",                                // hljs language id guessed from extension/filename, or null
  "old_rev": "abc…", "new_rev": "def…",            // see table below; the UI passes these to /api/file
  "too_large": false, "reason": null,              // true → hunks omitted; reason "file" (per-file cap) or "response" (per-response cap)
  "line_count": 123, "hunk_count": 4,              // always present (untrimmed counts)
  "ws_only": false,                                // true when ?ws=ignore removed every hunk of a file that has changes
  "hunks": [
    {"old_start": 10, "old_count": 7, "new_start": 10, "new_count": 9,
     "section": "def foo(self):",                  // text after the second @@, may be ""
     "lines": [
       {"t": "ctx", "o": 10, "n": 10, "s": "    x = 1"},
       {"t": "del", "o": 11, "n": null, "s": "    y = 2", "cr": true},        // cr: line had a trailing \r (stripped)
       {"t": "add", "o": null, "n": 11, "s": "    y = 3", "nonl": true},      // nonl: "\ No newline at end of file"
       {"t": "add", "o": null, "n": 12, "s": "<first 20000 chars>", "trunc": true}
     ]}
  ]
}
```

| kind       | `old_rev`                         | `new_rev`     |
|------------|-----------------------------------|---------------|
| commit     | `parents[0]`, or `null` for roots | `sha`         |
| combined   | `range.base` (`null` if none)     | `range.head`  |
| worktree   | sha of `HEAD` at extraction time  | `"worktree"`  |
| compare    | `base` param (`null` = empty tree)| `head` param  |

The UI must not request `/api/file` for a `null` rev, for a side whose mode is `160000` (submodule), or for the new
side of a `D` file / the old side of an `A` file. The old side of an R/C file is fetched with `old_path`.

Line texts are the diff line without the leading marker and trailing newline, decoded as UTF-8 with
`errors="replace"`; exactly one trailing `\r` is stripped and recorded as `cr: true`. Lines longer than 20000
characters are truncated and marked `trunc: true`. Tabs are preserved (UI renders with `tab-size: 4`).

`too_large`: per file, `line_count > 5000` (untrimmed hunk lines) or any line longer than 20000 chars →
serialised with `hunks: []`, `too_large: true, reason: "file"`, other fields intact. Per response, if the untrimmed
total across files exceeds 30000 lines, trim files largest-first (`reason: "response"`) until under the cap.
`?full=1` disables both caps for the request. The store always caches untrimmed data; trimming happens at
serialisation; anchor validation and snippet capture use untrimmed data.

### 2.4 Comment

```jsonc
{
  "id": "k3f9a2",                   // 6 lowercase base36 chars, unique per database
  "parent_id": null,                // root comment; replies carry the root's id (threads are flat: one level)
  "author": "user",                 // "user" | "claude"
  "body": "Markdown text",          // non-empty, ≤ 64 KiB
  "created_at": "…Z", "updated_at": "…Z",   // updated_at > created_at ⇒ "edited"
  "state": "pending",               // "pending" | "submitted". author=claude comments are created "submitted".
  "round": null,                    // round number once submitted (null for pending; claude comments: current round count at creation, may be 0)
  "resolved": false,                // root comments only; UI shows thread as resolved
  "anchor": Anchor,                 // replies copy the root's anchor (server enforces)
  "snippet": "    y = 3",           // server-captured text of the anchored line(s) at creation; "" when not a line anchor or unresolvable; ranges joined by "\n"; capped at 32 lines / 8 KiB (last line "…" when cut)
  "moved_from": null,               // {"commit": sha, "line": n|null} after automatic re-anchoring or `ccr move`
  "outdated": false,                // computed on read: anchor.commit not in the current review. Not stored.
  "head_location": {"path": "src/fetcher.py", "line": 14, "status": "same"}   // only with ?locate=1 (section 4.5); root line anchors only
}
```

`Anchor`:

```jsonc
{"kind": "line",                    // "line" | "file" | "commit" | "review"
 "commit": "9fceb02…|combined|worktree",   // null only for kind=review
 "path": "src/new.py",              // null for kind=commit/review. Always the FileDiff `path` (new name for renames, old name for deletions); the server normalises an old_path to path.
 "side": "new",                     // "old" | "new"; null unless kind=line. "old" = left/deleted side numbering.
 "line": 11,                        // end (primary) line number on `side`; null unless kind=line
 "start_line": null                 // optional; set for multi-line ranges; start_line < line. Ranges are single-sided.
}
```

For `kind=line` on `commit=combined`, `side=new` numbers refer to the file at `head`, `side=old` to `base`.
For a real commit: `new` → the file at that commit, `old` → at its first parent. For `worktree`: `new` → working
tree file, `old` → `HEAD` at extraction.

### 2.5 Round

```jsonc
{"number": 1, "submitted_at": "…Z",
 "verdict": "request_changes",      // "approve" | "request_changes" | "comment" — the UI always sends "comment" (rounds carry no verdict)
 "summary": "Overall looks good, two nits.",   // may be ""
 "base": "<sha|null>", "head": "<sha>", "commit_shas": ["…"],   // the chain at submit time (the sidebar's "•" marks commits added since)
 "comment_ids": ["k3f9a2", "…"]}    // computed on read: comments with round == number (roots and replies, incl. the summary comment)
```

Submitting with a non-empty `summary` creates `{author:"user", anchor:{kind:"review"}, body: summary, state:"submitted", round:n}`.
Submitting with zero pending comments **and** empty summary is allowed only when verdict is `approve`; otherwise 400.

### 2.6 Review (`GET /api/review`)

```jsonc
{
  "repo": {"path": "/abs/path", "name": "repo-dir-name", "branch": "feature/x", "bare": false},   // branch null when detached
  "range": {"spec": "main..HEAD", "given": "-n 5", "base": "…|null", "head": "…", "note": null, "first_parent": false},
  "options": {"worktree": true},
  "cover": "Markdown description of the whole change (the PR cover letter); \"\" when none",
  "commits": [CommitMeta, …],        // "combined" FIRST, then real commits oldest→newest, then "worktree" LAST
  "version": 17, "generation": 2, "loading": false, "now": "…Z",
  "counts": {"pending": 3, "submitted": 5, "unresolved": 4, "total": 8, "outdated": 0},   // root comments only, except total (all comments) and pending (all pending comments)
  "rounds": [Round, …],
  "server": {"pid": 1234, "port": 7777, "started_at": "…Z", "version": "0.1.0"},
  "ui": {"connected": true, "last_seen": "…Z", "open_polls": 1}
}
```

`version` increases on **every** mutation (comment create/edit/delete/move, resolve toggle, submit, reload).
`generation` increases on every successful `load()` (start and reload) — the client uses it to know the diffs changed.

`state()` (`/api/state`, `/api/events`): `{"version", "generation", "loading", "now", "server", "counts",
"rounds": <count>, "last_round": Round|null, "commits": <count>, "ui"}`. `ui.last_seen` is the time of the last
`/api/events` request whose `User-Agent` does not start with `ccr-cli/`; `connected` = seen within 60 s.

---

## 3. Git extraction (`ccr/gitx.py`)

Public API (all functions take `repo: str` absolute toplevel path; raise `GitError(message, status=400)` on failure):

```python
class GitError(Exception): status: int      # 400 default; 404 missing object; 413 too large; 415 binary
def check_version() -> tuple                 # git --version; < 2.24 → GitError
def toplevel(path) -> dict                   # {"path": toplevel or git dir, "bare": bool}
def rev_parse(repo, rev) -> str              # full sha of rev^{commit}; GitError(404) if unknown; rejects specs starting with '-' or containing whitespace/NUL
def merge_base(repo, a, b) -> str | None
def current_branch(repo) -> str | None
def empty_tree(repo) -> str                  # git hash-object -t tree /dev/null (cached per repo; SHA-256 repos differ)
def resolve_range(repo, spec: str | None, n: int | None) -> dict   # {"base", "head", "spec" (pinned), "given", "note"}
def list_commits(repo, base, head, first_parent=False) -> list[CommitMeta w/o files/comment_count]
def commit_stats(repo, commits: list[CommitMeta]) -> dict[sha, list[FileStat]]   # ONE git invocation for all commits
def diff_commit(repo, sha, parent: str|None, ws_ignore=False) -> CommitDiff        # parent None → empty tree
def diff_range(repo, base: str|None, head, ws_ignore=False) -> CommitDiff          # combined / compare
def diff_worktree(repo, ws_ignore=False) -> CommitDiff                              # tracked changes vs HEAD + untracked
def show_file(repo, rev, path) -> dict       # {"content": str, "lines": int, "truncated_lines": [n,…]} ; rev = 40/64-hex sha or "worktree"
def map_line(repo, from_rev, to_rev, path, line) -> dict   # {"path", "line", "status"}; section 3.3
def parse_patch(text: str) -> list[FileDiff]  # pure function, unit-tested
def parse_raw_and_patch(data: bytes) -> list[FileDiff]     # output of `git diff --raw -z -p …`
```

### 3.1 Invocation rules

* Every git command is `['git', '-C', repo, '-c', 'core.quotepath=false', '-c', 'color.ui=never', '-c', 'diff.noprefix=false',
  '-c', 'diff.mnemonicPrefix=false', '-c', 'diff.suppressBlankEmpty=false', '-c', 'diff.submodule=short', '-c', 'diff.relative=false',
  '-c', 'log.showSignature=false', '--no-pager', …]` with `shell=False`, `stdin=DEVNULL` (except `--stdin` uses),
  `cwd=repo`, and an environment built from `os.environ` **minus every `GIT_*` key** (except `GIT_CONFIG_NOSYSTEM`,
  `GIT_SSH*`, `GIT_TRACE*`), plus `LC_ALL=C`, `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `PAGER=cat`.
* `FileNotFoundError` → `GitError("git executable not found on PATH")`. Non-zero exit → `GitError(stderr.strip())`
  (except where noted). Output decoded as UTF-8 `errors="replace"` **after** NUL-splitting.
* Diff-producing commands add `--no-ext-diff --no-textconv --no-color -M -C --src-prefix=a/ --dst-prefix=b/ --submodule=short
  --abbrev=40 -U3`; `ws_ignore` adds `-w`. `git log` adds `--encoding=UTF-8 --no-show-signature`.
* **Argv safety**: every rev that originates outside gitx (HTTP params, CLI flags) is first normalised with
  `rev_parse` and only the resulting full sha is passed to other git commands, after `--end-of-options`; every path
  goes after `--`. The server only accepts revs that appear in the current review (section 5.1).

### 3.2 Commands

* `list_commits`: `git log -z --reverse --topo-order [--first-parent] --encoding=UTF-8 --no-show-signature
  --format='%H%x00%P%x00%an%x00%ae%x00%at%x00%ct%x00%B' <base>..<head> --` (or `<head>` when base is null).
  With `-z` each record ends with `\0`: split on `\0`, drop the trailing empty element, take 7 fields per record.
  `parents = P.split()`, `is_merge = len(parents) > 1`, dates via `datetime.fromtimestamp(int(x), timezone.utc)`.
  `shallow_boundary`: parentless commit whose sha is listed in `<gitdir>/shallow` (if that file exists).
* `commit_stats`: write one line per commit `<sha> <parents[0]>` (or `<sha>` alone for parentless commits) to stdin of
  `git diff-tree --stdin -r --root -M -C --raw --numstat -z --abbrev=40 --no-ext-diff --no-textconv --no-color`.
  Output per commit: header `<sha>\0` (for `--stdin` git echoes the commit line as given, so parse up to the first `\0`
  and take the first token), then raw records `:<old_mode> <new_mode> <old_blob> <new_blob> <X>[<score>]\0<path>\0`
  (R/C: `<old_path>\0<new_path>\0`), then numstat records `<add>\t<del>\t<path>\0` (R/C: `<add>\t<del>\t\0<old>\0<new>\0`;
  binary: `-\t-`). Commits with an empty diff may be absent → `[]`. Zip raw and numstat by index within the commit.
  `status = X[0]`, `score = int(X[1:] or 0)`; `-` counts → `additions = deletions = 0, binary = True`; `000000` mode → null.
  Implementers **must** verify this layout empirically against the installed git in a test and adjust the state
  machine (`:` → raw, digit/`-` → numstat, otherwise header).
* `diff_commit` / `diff_range`: single invocation `git diff --raw -z -p [-w] <flags> --end-of-options <old> <new> --`
  (old = parent sha or `empty_tree`). Output = raw block (records as above, NUL-terminated) followed by the patch text;
  **verify the boundary empirically** (expected: the raw block ends with an extra `\0`, i.e. split at the first `\0\0`);
  if the installed git separates them differently, fall back to two invocations with *identical* options (`--raw -z`
  then `-p`), which are order-consistent. Parse raw records into FileDiff skeletons (path, old_path, status, score,
  modes, blob ids), split the patch at lines starting with `diff --git ` into sections and assign sections **in order**
  with the rule: a `T` (type change) skeleton consumes **two** consecutive sections (deletion then creation; hunks
  concatenated), every other skeleton consumes exactly one; a skeleton with no section gets `hunks: []`.
  `additions`/`deletions` are counted from the parsed hunks (binary → 0/0). `binary` = section contains
  `Binary files … differ` or `GIT binary patch`.
* **Section header grammar** (before the first `@@`): `old mode`, `new mode`, `deleted file mode`, `new file mode`,
  `copy from/to`, `rename from/to`, `similarity index`, `dissimilarity index`, `index`, `--- `, `+++ `,
  `Binary files … differ`, `GIT binary patch` (then skip to the next `diff --git`). When `parse_patch` is used
  standalone (no raw block): paths from `rename from/to` / `copy from/to` when present, else from `---`/`+++`
  (strip `a/`/`b/`, `/dev/null` → null, strip one trailing `\t`, C-unquote tokens starting with `"`), else from
  `diff --git a/X b/Y` (split at ` b/`).
* **Hunk parsing**: header `^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$`; omitted count = 1; count 0 means
  no lines on that side. Consume body lines until `old_remaining == 0 and new_remaining == 0`: first char ` ` → ctx
  (both), `-` → del (old), `+` → add (new), `\` → set `nonl: true` on the previously emitted line (not counted), an
  empty line while counters are non-zero → ctx with `s: ""`, anything else → `GitError("malformed hunk")`. Never look
  for `---`/`+++`/`diff --git` while a hunk has remaining lines. `o`/`n` increment from the starts.
* `diff_worktree` (cwd = toplevel; bare repos → GitError): tracked = `git diff --raw -z -p [-w] <flags> HEAD --`;
  untracked = `git ls-files --others --exclude-standard -z`; skip entries ending in `/` (nested repos) and anything
  not a regular file or symlink per `os.lstat`. Untracked FileDiffs are built in Python (no subprocess): symlink →
  content = `os.readlink()`, `new_mode: "120000"`; regular → `new_mode` `100755`/`100644` by executable bit; read at most
  1 MiB + 1: NUL in the first 8000 bytes → `binary: true`; size > 1 MiB → `too_large: true, reason: "file"` with
  `additions` = line count; else one hunk `{old_start: 0, old_count: 0, new_start: 1, new_count: N}` of `add` lines
  (`nonl` on the last when no trailing newline). `status: "A"`, `old_*: null`, `old_rev: <HEAD sha>`, `new_rev: "worktree"`,
  `new_blob: null`. Conflicted files appear as `M` with markers in the new side. Intent-to-add files come from `git diff HEAD` only.
  If `git diff HEAD` fails because of `index.lock`, retry once after 200 ms, then GitError.
* `show_file(repo, rev, path)`: sha rev → `git cat-file -t --end-of-options <sha>:<path>` must print `blob`
  (else GitError 404 "not a regular file"), then `git cat-file blob --end-of-options <sha>:<path>`; `rev == "worktree"` →
  `p = os.path.join(toplevel, path)`; `realpath(dirname(p))` must be inside `realpath(toplevel)` (else GitError 403);
  `os.lstat`: symlink → content = readlink; regular file → bytes; else 404. Bytes: NUL in first 8000 → 415 "binary";
  > 8 MiB → 413. Decode `errors="replace"`, strip one trailing `\r` per line, split on `\n` (a trailing newline does
  not create an extra line), truncate lines > 20000 chars (`truncated_lines`).
* `lang` guess table (~60 entries): py, c/h, cc/cpp/cxx/hh/hpp/hxx→cpp, rs→rust, go, js/mjs/cjs→javascript, ts/tsx→typescript,
  jsx→javascript, java, kt/kts→kotlin, scala, rb→ruby, php, cs→csharp, swift, sh/bash/zsh/fish→bash, sql, html/htm/xhtml→xml,
  xml/xsd/xsl/svg→xml, css, scss, less, json/jsonc→json, yaml/yml→yaml, toml/ini/cfg/conf→ini, md/markdown→markdown,
  Makefile/makefile/*.mk→makefile, CMakeLists.txt/*.cmake→cmake, Dockerfile/*.dockerfile→dockerfile, proto→protobuf, lua,
  pl/pm→perl, r/R→r, m/mm→objectivec, erl/hrl→erlang, hs→haskell, ml/mli→ocaml, nix, groovy/gradle→groovy, txt→plaintext,
  diff/patch→diff, vb→vbnet, wasm/wat→wasm, graphql/gql→graphql. Unknown → null. `index.html` must load grammars so
  that every id in this table satisfies `hljs.getLanguage` (test asserts).

### 3.3 `map_line` (HEAD-relative locations)

`map_line(repo, from_rev, to_rev, path, line)`: if `from_rev == to_rev` → `{"path": path, "line": line, "status": "same"}`.
Run `git diff -M --raw -z -p --end-of-options <from> <to> -- <path>` plus, to follow renames, `git diff -M --name-status -z
--end-of-options <from> <to>` restricted to entries whose old path is `path` (rename → new path). If the file is deleted →
`{"path": null, "line": null, "status": "file-deleted"}`. Otherwise walk the hunks in order accumulating
`offset += new_count - old_count` for hunks entirely above `line`; if `line` falls inside a hunk's old range: if it is a
`ctx` line in that hunk → `line + (n - o)` of that row, `status: "same"`; if it is a `del` line → `{"line": hunk.new_start
+ position-of-first-add-or-0, "status": "changed"}` (`"deleted"` when the hunk has no adds); else `line + offset`,
status `"same"` if offset == 0 else `"moved"`. Results are cached per `(from, to, path)` for the lifetime of a version.

---

## 4. Store (`ccr/store.py`)

Storage: one `sqlite3` connection (`check_same_thread=False`, guarded by a `threading.RLock`). Default path
`${CCR_SESSION_DIR}/<key>.sqlite` (created via `os.open(..., O_CREAT|O_WRONLY, 0o600)` first), `:memory:` or an
explicit file via `--db`. For file dbs: `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL`, plus an exclusive
`fcntl.flock` on `<FILE>.lock` for the process lifetime (second server → exit 1 "db in use"). `PRAGMA foreign_keys=ON`.

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);          -- schema_version, repo, version
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, parent_id TEXT REFERENCES comments(id) ON DELETE CASCADE,
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  state TEXT NOT NULL, round INTEGER, resolved INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL, commit_sha TEXT, path TEXT, side TEXT, line INTEGER, start_line INTEGER,
  snippet TEXT NOT NULL DEFAULT '', moved_from TEXT);                       -- moved_from: JSON or NULL
CREATE TABLE IF NOT EXISTS rounds (number INTEGER PRIMARY KEY, submitted_at TEXT NOT NULL, verdict TEXT NOT NULL,
  summary TEXT NOT NULL, base TEXT, head TEXT NOT NULL, commit_shas TEXT NOT NULL);   -- commit_shas: JSON array
```

On opening an existing file: `meta.schema_version` > current → exit 1 "db schema too new"; `meta.repo` ≠
`realpath(repo)` → exit 1 `db was created for <path>; pass --db-force to reuse` (unless `--db-force`). `version` is
persisted in `meta` so a restarted server continues counting (never restarts at 0). The git diff cache is a plain
dict `(sha, full, ws) → CommitDiff` (derived data), cleared on reload.

```python
class ReviewStore:
    def __init__(self, repo, spec, n, worktree: bool, first_parent: bool, db_path: str, db_force=False)
    version: int; generation: int; loading: bool; stopping: bool
    cond: threading.Condition          # notify_all on every version bump and on stopping
    def load()                          # (re)extract commits+stats; on success clear diff cache, generation += 1, remap anchors (4.4), bump version. On GitError keep previous data and re-raise.
    def review() -> dict                # section 2.6
    def state() -> dict
    def commit_diff(sha, full=False, ws_ignore=False) -> dict     # resolves short shas / pseudo-commits; KeyError → 404
    def file_diff(sha, path, ws_ignore=False) -> dict            # one untrimmed FileDiff (path, then old_path)
    def compare(base: str|None, head: str, ws_ignore=False) -> dict
    def file(rev, path) -> dict
    def list_comments(state=None, round=None, resolved=None, author=None, commit=None, path=None, include_outdated=True, outdated_only=False, locate=False) -> list
    def add_comment(body, anchor, author="user", parent_id=None) -> Comment
    def edit_comment(id, body=None, resolved=None, anchor=None) -> Comment   # anchor → validate, re-capture snippet, set moved_from
    def delete_comment(id, cascade=False)   # root with replies and not cascade → StoreError(409, "thread has replies")
    def submit(verdict, summary) -> Round
    def wait(since_version, timeout) -> dict   # cond.wait_for(lambda: version > since or stopping, timeout) with a monotonic deadline; returns state() + {"changed": bool}
    def touch_ui()                      # records ui.last_seen
```

### 4.1 Anchor validation

* `kind=review`: commit/path/side/line must be null.
* `kind=commit`: `commit` required and must resolve (full/short sha of a listed commit, `combined`, `worktree` when
  enabled, or any git rev that `rev_parse`s to a listed commit); stored as the full sha / pseudo name.
* `kind=file`: commit + path required; `path` must match `path` or `old_path` of a file in that commit's diff (normalise to `path`).
* `kind=line`: commit, path, `side ∈ {old, new}`, `line ≥ 1` required; `start_line` optional and must be `< line`.
  If `line` (and `start_line`) exist in the diff hunks for that side → capture snippet; else accept when `1 ≤ line ≤
  100000` with `snippet = ""` (expanded-context comments). Compare views are not commentable (`compare:` → 400).
* Replies: `parent_id` must reference a **root** comment; anchor copied from the root; body required.
* Body: non-empty after strip, ≤ 64 KiB. `author ∈ {user, claude}`.
* Errors → `StoreError(message, status=400|404|409)`.

### 4.2 Snippet capture

For `kind=line`, `snippet` = the `s` text of the anchored line(s) on that side in file order, joined with `\n`, capped at
32 lines / 8 KiB (last line `…` when cut). Always from the non-whitespace-ignoring diff.

### 4.3 Outdated

`outdated = anchor.commit not in {listed shas} ∪ {"combined"} ∪ ({"worktree"} if enabled)`. Outdated comments are kept,
returned by the API with `outdated: true`, listed by `ccr comments`; the UI only counts them in the *All changes* header (7.2).

### 4.4 Re-anchoring on reload

After a successful `load()`, for each root comment (and its replies) that *would become* outdated: candidate commit =
the commit at the same chain index whose `subject` equals the old commit's subject (the store remembers the previous
chain's `sha → (index, subject)`), else the unique listed commit with that subject, else none. `kind=commit`: accept
the candidate. `kind=file`: accept if `path`/`old_path` exists in its diff. `kind=line`: accept when the file exists
and a line on the same side with `s == first snippet line` exists — unique match, or the nearest to the stored line
within ±20; for ranges require the same number of following lines to match. On success update `anchor.commit` (and
`line`/`start_line`), set `moved_from = {"commit": old, "line": old_line}`. Otherwise the comment stays outdated.
`load()` returns `{"remapped": [ids], "outdated": [ids], "commits_added": n, "commits_removed": n}`.

### 4.5 HEAD locations (`locate=1`)

For root `kind=line` comments, `head_location = map_line(repo, from_rev, HEAD_sha, path, line)` where `from_rev` =
`anchor.commit` (side new) / its `parents[0]` (side old) / `range.head` or `range.base` for `combined` / `HEAD`-at-extraction
for `worktree` side old. For `worktree` side new → `{"path", "line", "status": "live"}`. `HEAD_sha` is re-read on each
request (`rev_parse("HEAD")`). Errors → `{"path": null, "line": null, "status": "unknown"}`.

---

## 5. HTTP server (`ccr/server.py`)

`http.server.ThreadingHTTPServer` bound to `127.0.0.1:<port>`, `daemon_threads = True`, `allow_reuse_address = True`.

### 5.0 Protocol

Handler attributes: `protocol_version = "HTTP/1.1"`, `timeout = 60`, `server_version = "ccr/" + __version__`,
`sys_version = ""`. Every response carries `Content-Length` and `X-Content-Type-Options: nosniff`. Request bodies:
any `Transfer-Encoding` → 411; POST/PATCH without a valid `Content-Length` → 411; `Content-Length > 1048576` → 413
sent **before** reading, `close_connection = True`; `Content-Length: 0` → body `{}` (no Content-Type check); otherwise
`Content-Type.split(';')[0].strip().lower()` must be `application/json` (415) and the body must decode as a UTF-8 JSON
object (400). `send_error` is overridden so every error body is `{"error": "…"}` (`application/json; charset=utf-8`).
Response writes are wrapped in `try/except (BrokenPipeError, ConnectionResetError, TimeoutError)`. Unknown non-API
paths → 404 JSON. Log line (only with `--verbose`): `"%s %s %d %dms"` with the path **without query**.

### 5.1 Security

* **Token**: 32 hex chars from `secrets.token_hex(16)`. Every `/api/*` request must carry `X-CCR-Token` (compared
  with `hmac.compare_digest`) else `401 {"error":"unauthorized"}`. `?t=` is **not** accepted on `/api/*`; it is only
  how the UI receives the token on `GET /` (the page reads it from `location.search`).
* **Host**: must be present and its host part (port stripped) ∈ {`127.0.0.1`, `localhost`, `[::1]`} else `400 bad host`
  for **every** route. If `Origin` is present its host part must be in the same set (**port not compared** — SSH port
  forwarding changes it; `Origin: null` → 403). Same for `Referer`. `Sec-Fetch-Site: cross-site|same-site` → 403 on
  `/api/*`. No CORS headers, ever.
* **Revs & paths from clients**: `/api/file` accepts `rev` only if it is `worktree` or a full sha that occurs as
  `old_rev`/`new_rev`/`sha`/`parents[0]` in the current review data; `path` must be non-empty, relative, NUL-free, without
  `..` segments and must be a `path`/`old_path` of some file in the review; otherwise 400. `/api/compare` accepts only
  shas that are listed commits or their `parents[0]` (or empty/absent `base` = empty tree). `/api/commits/{sha}` resolves
  short shas against the listed commits only.
* **Static**: `rel = unquote(path[len('/static/'):])`; 404 if empty, contains `\0`, or any segment is `..`/empty;
  `target = realpath(join(STATIC, rel))`; serve only if `commonpath([target, STATIC_REAL]) == STATIC_REAL` and isfile.
  Fixed MIME table (`.html`, `.js/.mjs → text/javascript`, `.css`, `.svg`, `.json`, `.woff2`, else octet-stream).
  `/favicon.ico` → `static/favicon.svg`. Vendored assets are referenced as `/static/vendor/<f>?v=<__version__>` with
  `Cache-Control: max-age=31536000, immutable`; `index.html`/`app.js`/`style.css`/`theme.js` are `no-cache`.
* **Headers on HTML**: `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`,
  `Cross-Origin-Resource-Policy: same-origin`. (No inline scripts or `style=` attributes anywhere; CSSOM property
  assignment is fine.)
* **Files**: `serve` calls `os.umask(0o077)` first. Session dir `os.makedirs(d, 0o700)` + `chmod 0700`; if owned by
  another uid or group/other-writable → exit 1 `session dir <d> is not private`. Session file written atomically
  (`O_CREAT|O_EXCL` temp, mode 0600, fsync, `os.replace`). Log and db files 0600.
* **Long-poll cap**: at most 64 concurrent `/api/events` waiters; beyond that respond immediately with
  `{"changed": false, "retry_after": 5, …}`.

### 5.2 Routes

| Method | Path | Body → Response |
|---|---|---|
| GET | `/` | `index.html` |
| GET | `/static/<file>` | static asset |
| GET | `/api/review` | Review (2.6). While `loading`: `{"loading": true, …minimal}` with 200. |
| GET | `/api/state` | `store.state()` (never 503) |
| GET | `/api/events?since=N&timeout=S` | long-poll: `state()` + `"changed": true` as soon as `version > N` **or `since > version`** (client from another server incarnation) or `stopping`; `changed: false` after `S` seconds (cap 30, default 25). Records `ui.last_seen` for non-CLI agents. |
| GET | `/api/commits/{sha}?full=1&ws=ignore` | CommitDiff (2.3); `sha` = listed sha / short sha / `combined` / `worktree`; 404 unknown; 503 `{"error":"loading"}` while loading |
| GET | `/api/commits/{sha}/file?path=P&ws=ignore` | one **untrimmed** FileDiff (path, then old_path); 404 |
| GET | `/api/compare?base=X&head=Y&ws=ignore` | CommitDiff with `sha: "compare:<X10>..<Y10>"`, `kind: "compare"`, `subject: "Compare …"` |
| GET | `/api/file?rev=R&path=P` | `{"rev","path","content","lines","truncated_lines"}`; 400 bad rev/path; 403 escape; 404 missing/not a blob; 413 too large; 415 binary |
| GET | `/api/comments?state=&round=&resolved=&author=&commit=&path=&outdated=include|exclude|only&locate=1&project=<view>` | `{"version","generation","now","comments":[…]}`. With `project` (a listed sha, `combined` or `worktree`; 404 otherwise) every comment carries `view_anchor` — the anchor to render it at **in that view** (its own anchor when native; a line mapped with `map_line` between the two views' revisions for line comments made elsewhere; the same path for file comments; `null` for other views' commit-level comments and unmappable lines; review anchors as-is) — and `projected` (true when it came from another view). Replies carry their root's `view_anchor`. |
| POST | `/api/comments` | `{body, anchor, author?, parent_id?}` → 201 Comment |
| PATCH | `/api/comments/{id}` | `{body?, resolved?, anchor?}` → Comment |
| DELETE | `/api/comments/{id}?cascade=1` | 204; 409 when a root has replies and no cascade |
| POST | `/api/submit` | `{verdict, summary}` → 201 Round |
| POST | `/api/reload` | `{range?, n?, worktree?, first_parent?}` (omitted = keep) → `{"review": Review, "remapped": [...], "outdated": [Comment], "commits_added", "commits_removed"}`; git errors → 400, previous data kept |
| POST | `/api/cover` | `{text}` → `{"cover", "version"}`; sets the cover letter (≤ 64 KiB Markdown, stored in `meta`; bumps `version` **and** `generation` so open pages re-render) |
| POST | `/api/shutdown` | 202; sets `stopping`, `cond.notify_all()`, then `threading.Thread(target=httpd.shutdown, daemon=True).start()` |

Errors are always JSON `{"error": "…"}`. Unknown `/api/*` → 404.

Server lifecycle (`ccr serve`): `os.umask(0o077)` → parse args → resolve range & open db (errors → stderr, exit 1) →
bind socket → write the session file itself (actual port, own pid, token; mode 0600) → start `store.load()` in a
thread (`loading: true` until done; other `/api/*` return 503 `loading` except `/api/state`, `/api/events`,
`/api/review`) → `serve_forever()` → on `stopping`/SIGTERM/SIGINT: `server_close()`, close sqlite, delete the session
file only if its `pid == os.getpid()`, exit 0. `--idle-timeout S` (default 86400, 0 = never): shut down when no request
has arrived for S seconds (long-polls count as activity).

---

## 6. CLI (`ccr/cli.py`)

Invocation: `ccr <command> [options]` (or `python3 -m ccr`, or `bin/ccr`). Global options: `--repo PATH` (default: git
toplevel of cwd; outside a repo without `--repo` → `ccr: <cwd> is not inside a git repository; pass --repo PATH`, exit 1),
`--url URL --token T` (bypass discovery; env `CCR_URL`, `CCR_TOKEN`), `--json`. All CLI HTTP calls send
`User-Agent: ccr-cli/<version>` and `X-CCR-Token`, 5 s timeout (`wait`: 35 s per poll). Exit codes: 0 ok, 1 error,
2 timeout (`wait`), 3 no running session.

### 6.1 Sessions (`ccr/session.py`)

`key = sha1(realpath(repo))[:16]`; session dir `${CCR_SESSION_DIR:-~/.cache/ccr/sessions}` (0700). Files:
`<key>.json` `{"pid","port","token","url","repo","range","started_at","log","db"}`, `<key>.log`, `<key>.sqlite`,
`<key>.lock`. Any command that finds a stale file (pid dead, connection refused, or 401 from `/api/state`) deletes it
and exits 3: `ccr: no running session for <repo> — use --repo PATH or run ccr start (live sessions: <repo> → <url>, …)`.
Linked git worktrees are separate sessions (different realpath). Default port: `7700 + int(key, 16) % 300`; if busy and
`--port` not given, try the next 20 ports upward, then any free port; explicit `--port N` busy → exit 1 `port N in use`.

### 6.2 Commands

* `ccr start [--range SPEC | -n N] [--worktree | --no-worktree] [--first-parent] [--port N] [--db PATH] [--log FILE] [--open] [--idle-timeout S] [--cover FILE]`
  `--cover FILE` sets the cover letter (also on reuse). `ccr cover (TEXT | --file F | -)` sets/replaces it on a running
  review. The cover letter is shown above "All changes" in the UI with a *Comment on the whole series* button
  (anchor `kind=review`), and `ccr export --md` prints it under `## Cover letter`.
  1. Take `<key>.lock` (`O_CREAT|O_EXCL`; ignore if older than 30 s) so concurrent starts serialise.
  2. If a live session exists: reload it with the given options (unchanged ones kept; no options → reload the pinned
     spec so new commits appear); print `ccr: reusing running session (pid P)` + the `reload` lines + the URL; exit 0.
  3. Resolve the range **in the parent** (`gitx.resolve_range`) — errors exit 1 immediately.
  4. Spawn `ccr serve …` with `subprocess.Popen(start_new_session=True, close_fds=True, stdin=DEVNULL,
     stdout=stderr=<log file, truncated, 0600>)`, the token passed **only** via env `CCR_SERVE_TOKEN` (never argv).
  5. Poll every 100 ms for ≤ 10 s until the session file appears and `/api/state` answers; on child exit print
     `ccr: server exited with code N — last log lines:` + last 20 log lines, exit 1; on deadline SIGTERM the child and do
     the same. Then wait ≤ 120 s for `loading: false` (print `ccr: extracting commits…` once to stderr).
  6. Print (stdout):
     ```
     ccr: serving /abs/repo  (main..HEAD, 7 commits, +worktree)
     ccr: url http://127.0.0.1:7777/?t=<token>
     ```
     plus `ccr: note: <range.note>` when set. `--json` prints the session dict + review counts.
* `ccr serve …` — foreground; same options plus `--token T` (documented: visible in `ps`; tests only), `--verbose`,
  `--db-force`. Token source order: `--token`, `CCR_SERVE_TOKEN`, generate. Prints the two lines above (token redacted
  as `<redacted>` when it came from the environment, so logs never contain it).
* `ccr stop [--all] [--keep-db] [--purge]` — 1) `GET /api/state`, require `server.pid == session.pid` (else stale, never
  signal); 2) `ccr export --md` → `<session dir>/<key>-<YYYYmmdd-HHMMSS>.md`, print `ccr: exported to <path>`;
  3) `POST /api/shutdown`; 4) wait ≤ 5 s for the pid to vanish; 5) only then SIGTERM, and only if `/proc/<pid>/cmdline`
  contains `ccr` and `serve`; 6) delete the session file and the default sqlite file (`--keep-db` keeps it; `--purge` also
  removes exports and logs). `--all` does this for every live session.
* `ccr status [--json]` — url, range (+note), commits, counts, rounds (last verdict), ui connected/last seen, log path, db path.
* `ccr sessions [--json]` — every session file (repo, url, range, alive?, started_at), deleting stale ones; exit 3 if none.
* `ccr logs [-n N] [-f]` — tail of the server log.
* `ccr open` — `webbrowser.open(url)`.
* `ccr reload [--range SPEC | -n N] [--worktree | --no-worktree] [--first-parent | --no-first-parent]` — prints
  `ccr: N commits (was M), +a −r, K comments remapped, J now outdated`; when any previously listed sha is gone:
  `warning: R reviewed commits left the range`; then one line per still-outdated thread
  `  <id> · <old short sha> <path> <side>:<line> · "<first snippet line>"`. `--json` → the reload response.
* `ccr comments [--pending | --submitted | --round N | --all] [--unresolved] [--unanswered] [--author user|claude] [--commit SHA] [--path P] [--outdated | --no-outdated] [--context N] [--no-snippets] [--json]`
  All filters select **threads**: a thread is included when its root or any reply matches; the whole thread is printed
  (matching comments marked `★`). `--unanswered` = unresolved, non-outdated threads whose last comment is by `user`.
  Default `--all --context 3`. Zero matches → `ccr: no comments match` (exit 0).
* `ccr wait [--since-round N] [--since-version V] [--timeout S] [--any] [--json]` — returns when a round with
  `number > N` exists (N defaults to the round count at call time). Stdout starts with
  `ccr: round <n> — <verdict> — <k> new comments in <j> threads` then the Markdown for `--round n` (threads with any
  comment in round n, earlier comments as context, new ones marked `★ new in round n`). Default `--timeout 590`
  (0 = forever). Timeout → stderr `ccr: no new round after S s (rounds: R, pending unsubmitted: P, version: V)`, exit 2.
  Connection errors → retry each 1 s; if the pid is dead or 30 s of consecutive failures → delete the session file,
  `ccr: server gone`, exit 3. After 30 s with `ui.last_seen == null` print `ccr: UI not opened yet` once (stderr).
  `--any`: return when `version > V` (default: version at call time), print `ccr: version V→W · pending P · unresolved U
  · rounds R` and the threads containing comments with `updated_at >= call time`.
* `ccr reply ID (BODY | --file F | -) [--resolve] [--as claude|user] [--force]` — `--resolve` resolves after a successful
  reply. Refuses `ccr: identical reply already exists on this thread (id …); use --force` (exit 1) when the same author
  already posted the same body on that thread.
* `ccr reply --batch (FILE | -) [--json]` — JSON `[{"id","body","resolve"?}, …]` **or** Markdown with `## <id> [resolve]`
  headings followed by the body; posts sequentially, prints `<id>: replied[, resolved]` / `<id>: ERROR …` per item,
  continues on error, exit 1 if any failed.
* `ccr comment (--review | --commit REV [--path P [--line N [--side new|old] [--start-line M]]]) (BODY | --file F | -) [--as claude|user]`
  `--commit` accepts a full/short listed sha, `combined`, `worktree`, or any git rev the server resolves to a listed
  commit (404 otherwise). `--side` defaults to `new`; `--start-line` requires `--line`.
* `ccr resolve ID [ID…]` / `ccr unresolve ID [ID…]` / `ccr edit ID (BODY | --file F | -)` / `ccr delete ID [ID…] [--cascade]`
* `ccr move ID --commit REV [--path P [--line N [--side S] [--start-line M]]]` — `PATCH` with a new anchor.
* `ccr export [--json | --md] [-o FILE]` — `--md` = `# Review — <repo> (<spec>, base <sha> → head <sha>) — exported <ISO>`,
  the `## Rounds` block, then the 6.3 Markdown for `--all --outdated --context 3`; `--json` =
  `{"review": Review-without-files, "rounds", "comments", "threads": [{"root","replies","last_author","answered"}]}`.
  `-o FILE` is created with mode 0600.

Bodies read from `-` take stdin.

### 6.3 Markdown rendering of comments (`ccr/render.py`)

Optimised for an LLM reader: deterministic order (chain order, then path, then end line, then `created_at`), explicit
IDs, code context with line numbers, HEAD-relative locations, and unambiguous anchors. All repo/user-derived strings
pass through `clean(s)`: remove C0/C1 controls except `\n`/`\t`, U+2028/9, bidi controls U+202A–E and U+2066–9;
single-line fields additionally replace `\n`/`\r` with `␤`. Body lines starting with `#` are emitted as `\#…` so a body
can never open a heading; reply bodies are indented two spaces.

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

#### [id: …] user · new:20-24 → HEAD src/fetcher.py:23-27 · R1 · resolved · edited 2026-09-03T14:01:00Z
…

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

Snippet block: `--context N` (default 3) rows before/after from the cached diff, each `<old#|blank> <new#|blank>
<marker> <text>`, anchored rows prefixed with `>`; `--no-snippets` drops it. The HEAD arrow shows
`→ HEAD <path>:<line>` with `(moved)`/`(changed near)`/`(deleted)`/`(file deleted)`/`(live)` as applicable. `--json` prints
`{"review": {...meta w/o files}, "rounds": [...], "comments": [...], "threads": [{"root": id, "replies": [ids], "last_author", "answered"}]}`
(threads reference comments by id; the full objects are in `comments`).

---

## 7. Web UI (`ccr/static/`)

Single page, vanilla JS, no framework, one `app.js` (≈ 2000–3000 lines is expected). Must stay fast on a diff with
100 files / 10k lines. Everything is event-delegated (one `click`, one `pointerdown/move/up`, one `pointerover`, one
`input` listener on `#main`; one `keydown` on `document`; `hashchange`/`popstate`). File bodies are built as one
HTML string (all dynamic text through `esc()`) and assigned once with `innerHTML`; threads and the header card are
patched piecemeal. No `style=` attributes in markup (CSP); computed styles use CSSOM properties or classes
(avatar colours = 8 classes `av-0…av-7` chosen by name hash).

### 7.1 Boot & token

`theme.js` (sync in `<head>`) reads `localStorage['ccr:theme']` (`auto|light|dark`), resolves `auto` via
`matchMedia('(prefers-color-scheme: dark)')`, sets `document.documentElement.dataset.theme`. Both hljs sheets are
linked (`#hl-light`, `#hl-dark`); the inactive one has `link.disabled = true`.

`app.js` boot: `token = new URL(location).searchParams.get('t') || localStorage['ccr:token:' + location.port]`; store it
under that key; `history.replaceState` to a URL **without** `?t=` (hash preserved). No token, or any `401` → stop all
polling, clear the stored token, replace the app with a full-page notice (`#notice-token`): *"No valid session token for
this tab — the server may have been restarted. Run `ccr status` (or `ccr open`) and open the printed URL."* A top-bar
**Copy link** action and every generated permalink reconstruct `origin + '/?t=' + token + hash` so links work in other
tabs/browsers.

### 7.2 Layout

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ⎇ repo · branch   main..HEAD · 7 commits · +worktree   [Unified] [Wrap] [Hide ws] [☾] [⟳] [Submit ●3] │
├────────────┬─────────────────────────────────────────────────────────────────────────────────┤
│ COMMITS    │ Header card: subject · body · author · date · sha (copy) · +40 −12  [💬 comment]  │
│ ● All      │ ┌ src/fetcher.py  M  +10 −2                                          [💬] [▾] ┐ │
│ ○ 9fceb02 •│ │ @@ -10,7 +10,9 @@ def foo(self):                       [⤒ 20] [expand all] [⤓ 20]│
│ ○ 1a2b3c4  │ │ 10  10     x = 1                                                           │ │
│ ○ …        │ │ 11        - y = 2                                                    [+]   │ │
│ ○ Worktree │ │     11    + y = 3                                                          │ │
│────────────│ │ ┌ thread ─────────────────────────────────────────────────────────────┐    │ │
│ FILES  [🔍]│ │ │ U user · 2 min ago · Pending          [Edit][Delete][Reply][Resolve]│    │ │
│ ▾ src/ccr  │ │ │ Why not use the existing backoff helper?                            │    │ │
│   fetcher  │ │ │ C Claude · 1 min ago · New                                          │    │ │
│   other    │ │ │ Good catch — switched to `backoff.retry()` in 1a2b3c4.              │    │ │
│ ▾ tests    │ │ │ [Reply] [Resolve]                                                   │    │ │
└────────────┴─┴─┴────────────────────────────────────────────────────────────────────────────┘
```

**Top bar** (`--top-h`): repo name + branch, range spec (+ note tooltip), commit count; `#btn-viewmode` (label = current
mode), `#btn-wrap`, `#btn-ws` (Hide whitespace, `?ws=ignore`, persisted), `#btn-theme` (auto→light→dark),
`#btn-reload` (POST `/api/reload` — no confirm; afterwards a dismissible `#banner-reloaded` *"Chain reloaded: +a −r
commits, K comments remapped, J now outdated"*), `#btn-submit` (bold **Submit**, green filled with white text, darker on
hover; one `.badge-pending` badge with the pending-**comment** count, hidden at 0; disabled and muted while nothing is
pending or a submit is in flight; click → POST `/api/submit` `{verdict: "comment", summary: ""}` — rounds carry no
verdict — then toast *"Round N submitted · K comments"*, errors as a red toast), `#btn-copy-link`, `#btn-sidebar`.

**Sidebar** (resizable 200–480px, persisted; collapsible):

* *Commit chain*: "All changes" first, commits oldest→newest with a rail, "Uncommitted changes" last (greyed with
  "(clean)" when it has no files). Item: short sha (mono), subject (ellipsis), author avatar (initials, `av-N` class),
  relative date, `+N −M`, badges (threads; pending yellow, unresolved red), `•` **new** dot (`.is-new`) when the sha is not
  in the last round's `commit_shas` (and a round exists), merge glyph. **Hover/focus** (300 ms) → one shared
  `#tooltip[role=tooltip]` with full subject + body (`pre-wrap`), author, absolute date, sha. **Click** → select
  (`history.pushState` to `#<sha>`). **Shift+click** → contiguous range of *real* commits (pseudo items ignored with a
  toast) → compare view (`base = parents[0]` of the first, or omitted for a root; `#compare:<a10>..<b10>`); any single
  click or `]`/`[` exits compare mode.
* *File tree* for the selected commit: path compression (a directory with exactly one child directory merges with
  it, label `src/ccr/static`); files never merge; all folders start expanded; collapse state persisted
  (`ccr:folders:<repo>`); rows show status letter (A green, M yellow, D red, R blue, T purple), `+N −M`, thread
  count. `#file-filter`: case-insensitive substring on the full path; while non-empty the tree is flat (matches only)
  **and** non-matching file cards are hidden in the main pane (`N of 40 files — clear`; zero → *"No files match"*).
  Click → `navigateTo({sha, path})`.

**Main pane** (`#main` is the **only** vertical scroller; `body`, `.file-card`, `.diff-body` keep `overflow-y: visible`):

* *Header card*: subject (h1), body (`pre-wrap`), author + relative date (title = absolute), sha click-to-copy,
  parents (merges), stats, `#btn-comment-commit`. Pseudo-commits explain what they are (range / "vs HEAD").
  The **All changes** header additionally shows the cover letter (`#cover-letter`, safe Markdown; *"No cover letter"*
  hint when empty), `#btn-comment-review` (*Comment on the whole series*, anchor `kind=review`) and a muted `#outdated-note`
  (*"N comments are anchored to commits that left the series (see `ccr comments --outdated`)"*, hidden at 0); review-level
  threads render in `#commit-header .thread-block[data-key-host=review]` under the cover letter. Empty
  commit → *"This commit has no file changes"*. Compare view → `#banner-compare` *"Compare view — comments are disabled"*.
* *File card* (`.file-card[data-path]`): sticky `.file-header` (`position: sticky; top: 0` inside `#main`) with path
  (renames `old → new`), status badge, mode-change note, `+N −M`, thread count, `.btn-comment-file`, `.btn-collapse`
  (chevron; `state.collapsedFiles`), copy path.
  Binary → *"Binary file not shown"*; `too_large` → *"Large diff hidden (N lines) — [Load anyway]"* (fetches
  `/api/commits/{sha}/file?path=`; spinner; disabled while loading); no hunks (mode-only / `ws_only`) → note.
* *Diff body*: created synchronously for every file with `min-height = min(2000px, (line_count + hunk_count) × --row-h)`
  set via CSSOM; one `IntersectionObserver({root: mainEl, rootMargin: '1500px 0px'})` calls `ensureRendered(card)`
  (sync, single `innerHTML`), which clears the min-height and unobserves. Bodies are never un-rendered.

### 7.3 Diff table

`<table class="diff" data-view="unified|split">` with `table-layout: fixed; width: 100%`; number columns `width: 1%;
min-width: 3.5em; text-align: right; user-select: none`. **Wrap on** (default): code cells `white-space: pre-wrap;
overflow-wrap: anywhere`. **Wrap off**: `.diff-body { overflow-x: auto }` (the header is outside `.diff-body`, so it
stays sticky) and `white-space: pre`. `tab-size: 4`; monospace stack `ui-monospace, SFMono-Regular, Menlo, Consolas,
"Liberation Mono", monospace`.

**Row model** (both views): for each hunk, group `lines` into blocks — a ctx block (maximal run of `ctx`), or a change
block (maximal run of `del` followed by the maximal, possibly empty, run of `add`; or a run of `add` with no preceding
`del`). In a change block with D dels and A adds, `del[i]` pairs with `add[i]` for `i < min(D, A)`.
* Unified: one `<tr class="line ctx|add|del" data-o data-n>` per diff line, dels before adds in a block. Columns:
  `td.num.old` · `td.num.new` · `td.marker` · `td.code`.
* Split: a change block yields `max(D, A)` rows; row i has `del[i]` left and `add[i]` right; a missing side renders
  `td.num.empty` + `td.code.empty` (hatched). Ctx rows show the line on both sides. Columns: `td.num.old` ·
  `td.code.old` · `td.num.new` · `td.code.new`. Selecting text: `pointerdown` in a `.code.old|new` cell adds `sel-old|sel-new`
  to the table whose CSS sets `user-select: none` on the other side; removed on `pointerup`.
* Both tables have 4 columns, so thread and editor rows are `<tr class="threads|editor" data-key><td colspan="4">`.
* `td.num[data-side][data-line]` on every number cell that has a line; `data-o`/`data-n` on the row (split rows: left/right).
* **Hunk row** `tr.hunk`: `@@ -a,b +c,d @@ section` plus expand controls: gap ≤ 20 lines → single *"expand N lines"*;
  otherwise `.btn-expand-up` (20 lines just above the lower hunk), `.btn-expand-down` (20 lines below the upper hunk),
  `.btn-expand-all`. A row above the first hunk exists while lines 1.. are hidden; a trailing *"expand to end"* row
  exists after the last hunk until EOF is shown (needs `lines` from `/api/file`). A hunk row disappears once its gap is
  fully expanded.
* **Expansion**: source side = `old_rev` for status `D`, else `new_rev` (respecting the 2.3 table; disabled for null
  revs / submodules); fetched once per file into `state.fileText`. Gap line k after hunk i: `new = new_start_i +
  new_count_i + k`, `old = old_start_i + old_count_i + k`; above the first hunk: `new = 1..new_start−1`, `old = new −
  (new_start − old_start)`; below the last hunk to EOF with the same offset (roles swapped when the fetched side is
  old). Expanded rows are ordinary `ctx` rows with `data-x="1"`, commentable. After expansion rebuild that file's
  per-side text, re-tokenize, re-render only that `.diff-body` (preserving open editors, selection and the clicked
  button's scroll offset).
* **Highlighting** — `highlighter.tokenize(sideText, lang) → Array<Array<[classes, text]>>`: (1) no `hljs`, unknown
  language, or `sideText.length > 500000` → plain; (2) lines > 1000 chars replaced by `''` (remember indices);
  (3) `hljs.highlight(text, {language, ignoreIllegals: true}).value` in try/catch; (4) scan with
  `/<span class="([^"]*)">|<\/span>|\n|[^<\n]+/g` keeping a stack of open classes; text tokens are entity-decoded (`&amp;
  &lt; &gt; &quot; &#x27;`) and appended as `[stack.join(' '), text]`; `\n` starts a new line with the stack unchanged;
  (5) line count mismatch → plain; (6) restore the long lines as single unclassed segments. Cache
  `state.hl['sha|path|side']`. Unified: ctx/add rows use `new` tokens, del rows `old` tokens. Never put class `hljs` on
  any element (the theme's `.hljs{background}` would paint over row colours).
* **Word diff** on paired rows where both sides ≤ 400 chars: tokens `/\w+|\s+|[^\w\s]/gu`, token LCS, tokens not in the
  LCS become changed character ranges; if > 50 % of tokens on either side changed → no marks for that pair.
  `renderCode(tokens, ranges)` splits segments at range boundaries and wraps changed pieces in `<span class="wd">`
  (`--diff-add-strong` / `--diff-del-strong`). A `cr: true` line shows a dim `␍` glyph (class `cr`) at the end; `trunc`
  lines append ` … [truncated]`.
* **Gutter [+]**: one shared `<button class="btn-add-comment" aria-label="Add comment">` per rendered file; a delegated
  `pointerover` moves it into the hovered row's target number cell and shows it: unified → `td.num.new` for ctx/add rows,
  `td.num.old` for del rows; split → the number cell of the side under the pointer. Also shown on `:focus-within` and
  on `tr.line.is-selected`. Hidden in compare view. `td.num{position:relative}`; the button is absolutely positioned
  overlapping the code edge. Click → `openEditor(rangeAnchor if a selection ends on this row else rowAnchor)`.
  **Side rule**: unified ctx rows anchor to `new` via `[+]`; clicking the *old* number cell of a ctx row (or the left
  `[+]` in split) anchors to `old`; del → old; add → new.
* **Selection & ranges** (single-sided): Pointer Events. `pointerdown` (button 0) on a `td.num[data-line]`:
  `preventDefault`, `setPointerCapture`, `state.sel = {sha, path, side, startLine, endLine, dragging}` with that cell's
  side; table gets class `selecting`. `pointermove`: `elementFromPoint(...).closest('tr.line')` within the same table;
  the end is the row's line on `state.sel.side` (rows lacking a line on that side are skipped); autoscroll `#main` by
  12 px/frame when within 40 px of its edge. `pointerup` finalises (normalise start ≤ end); `Esc` cancels; Shift+click
  on a `td.num` extends. Plain click selects one line and writes the hash with `history.replaceState`. Rows in the
  range get `in-range`; the single selected line `is-selected`. `[+]` on the end row or `c` opens the editor with
  `start_line` when the range spans > 1 line. Clicking a code cell, `Esc`, or switching commit clears the selection.
* **Threads** attach to a row by anchor key (7.8): for every row attach threads keyed `(new, n)` if `n` and `(old, o)` if
  `o` (old-side first). Ranges render under the **end** line; hovering the thread adds `in-range` to rows start..end.
  Threads whose row does not exist in the rendered table (drifted combined/worktree anchors, `ws_only`, too_large) go
  to `state.orphans` and are not rendered (`ccr comments` lists them). Threads render in every view the
  server projects them into: the UI always fetches `GET /api/comments?project=<selected view>` (5.2; refetched on every
  view change, compare views have no comments) and keys `threadsByKey`, thread rows and the gutter attachment by each
  root's `view_anchor` (falling back to `anchor`); a root whose `view_anchor` is `null` in the current view is not rendered
  there (not an orphan); `projected` roots carry a `.tag-from` tag (*"from &lt;short sha&gt;"*, *"from All changes"*,
  *"from uncommitted changes"*) that opens the thread in the view it was written in. Replies keep posting `parent_id`
  (the server copies the root's native anchor).

### 7.4 Comments, editors, threads

* **Editor** (`tr.editor` / `div.editor-block`): `form.comment-editor[data-key]` with auto-growing textarea, a live
  preview `div.md-preview.md` below it (the same `renderMarkdown` as comment bodies, updated on `input` debounced 150 ms,
  hidden while the text is empty), Markdown hint, **Add comment** (Ctrl/⌘+Enter), **Cancel** (Esc; keeps the draft — an
  emptied editor drops it). While a
  request is in flight the buttons are disabled; on 201/200 insert the returned Comment into state, remove the draft,
  close the editor, patch only that thread; on error keep the editor open and toast the server message. No temporary ids.
  `state.openEditors: Map<key, {mode: 'new'|'reply'|'edit', id?}>` — every re-render (view toggle, expansion,
  reconciliation) recreates editor rows from this map, so editors are never lost. Drafts `localStorage['ccr:draft:'+k]`
  (k = anchor key | `reply:<rootId>` | `edit:<id>`), debounced 300 ms, deleted on success or when cancelled empty; a
  `[+]`/Reply control whose key has a draft shows a dot.
* **Thread** (`.thread[data-thread-id]`): comments with avatar (U / C robot; distinct colours), author, relative time
  (title = absolute, using server `now` offset), tags: `Pending` (title *"Not yet part of a submitted round — Claude can
  already read it with ccr comments"*), `R<n>`, `edited` (when `updated_at > created_at`), `moved`, `New` (7.5). Body =
  safe Markdown (own renderer: paragraphs, ATX headings, `code`, fenced code (hljs when `getLanguage`), **bold**, *italic*, links only
  when the trimmed, control-stripped URL matches `/^https?:\/\//i` → `<a target=_blank rel="noopener noreferrer">`,
  `- ` lists, `> ` quotes, line breaks; everything escaped first; no autolinks). 7–40 hex tokens matching a listed sha
  render as links selecting that commit. Actions (visible on hover/focus-within/selected): Edit, Delete (confirm; a
  root with replies says *"This also deletes N replies"* and sends `?cascade=1`), Reply, Resolve/Unresolve (root).
  Resolved threads collapse to *"✓ Resolved · N comments — Show"* unless they contain unseen comments.
* **Counts** come from one `deriveCounts()` pass over `state.comments` after every `applyComments`: per commit and
  per `commit|path` `{threads, pending, unresolved}` over non-outdated roots; the Submit badge counts pending **comments**.

### 7.5 Live updates, seen-tracking, reconciliation

* Only the visible tab long-polls (`document.visibilityState`); hidden tabs suspend and re-sync on `visibilitychange`.
  (Should: elect one poller per origin with `navigator.locks` + `BroadcastChannel('ccr:events')`.) Honour `retry_after`.
* On `changed`: always refetch `/api/comments`; if `generation` differs → refetch `/api/review`, then the selected
  commit's diff (fallback to `combined` if the sha vanished; always for `combined`/`worktree`), re-render sidebar/header/
  files restoring `main.scrollTop`, `openEditors`, `sel`, and toast/banner the reload summary. If `server.started_at`
  differs from the value captured at load → full re-init (drafts kept). `state.version` is updated from every
  `/api/comments`/`/api/review` response.
* `applyComments(list)` reconciles by id: per anchor key compute a signature (member ids + `updated_at` + `resolved` +
  `state`); re-render only threads whose signature changed; never touch `tr.editor` or textareas; remove comments absent
  from the list. New `author === 'claude'` ids seen in a refetch (not the initial load) → toast *"Claude replied to N
  threads (k resolved) — Show"* → `navigateTo` the first such thread.
* **Seen**: `localStorage['ccr:seen:<repo>']` = max `created_at` of comments that have been rendered while visible
  (IntersectionObserver on `.thread` elements, 1 s). Newer comments get a `New` dot (comment,
  collapsed thread line, file header count, sidebar badge). Resolved threads with unseen comments render expanded.
* Disconnected: `#banner-disconnected` *"Disconnected — retrying…"*; after 30 s *"Server not responding — it may have
  been stopped (ccr status)"*; on reconnect refetch and toast *"Reconnected"*. 401 → 7.1 notice. `/api/reload` 400 →
  keep the view, red toast with the git message.

### 7.6 Navigation, hash, keyboard, submit

* **Hash grammar** (single source of truth for the selection): `#<sha>` | `#<sha>/<encodeURIComponent(path)>` |
  `#<sha>/<path>:<o|n><line>[-<line>]` | `#compare:<a10>..<b10>`; `<sha>` = full sha / `combined` / `worktree`. Parse with
  `/^#([^/:]+)(?:\/(.+?))?(?::([on])(\d+)(?:-(\d+))?)?$/` (validate parts; use only as map keys). Commit/file changes
  use `history.pushState`; line clicks `replaceState`; `popstate`/`hashchange` → `navigateTo`. Back/Forward move
  between commits.
* `navigateTo({sha, path, side, line, endLine, threadId})`: select the commit if needed (await diff); `ensureRendered`;
  expand a collapsed file; expand a collapsed resolved thread; if the row is missing
  try expanding context to include it (else toast); `scrollToRow` (`main.scrollTop = rowTop − headerOffsets −
  120`; instant under `prefers-reduced-motion`); flash 1.5 s. All scroll targets have `scroll-margin-top`.
* **Commit switch** remembers per sha `scrollTop` + current file; when the target commit contains the current file
  (by `path`/`old_path`) scroll to it and flash its header; else restore that commit's `scrollTop`. Selected item
  scrolled into view in the sidebar. `]`/`[` at the ends → toast *"First/Last commit"*.
* **Keyboard**: Ctrl/⌘+Enter posts and `Esc` cancels the focused editor (draft kept) — always on. Every single-key
  shortcut is **off by default** behind `const KEYBOARD_SHORTCUTS = false` at the top of `app.js` (the handlers stay in
  `onKeyDown`); set to `true`, and with focus outside input/textarea/contenteditable: `j`/`k` next/prev file header
  (`state.currentFile` maintained by a throttled scroll handler), `]`/`[` commits, `n`/`p` next/prev thread in the current
  view (document order, expanding resolved, skipping orphans; the current thread gets a focus outline and `c` opens Reply
  on it), `Shift+N`/`Shift+P` next/prev **unresolved** thread across the chain (switching commit), `c` comment on the
  selected line/range, `x` collapse/expand current file, `u` view mode, `w` wrap, `Esc` clears the selection, then the
  thread focus. Ends → toast *"No more threads"*.
* **Submit** (`#btn-submit`, top bar): POST `/api/submit` `{verdict: "comment", summary: ""}` bundles every pending comment
  into the next numbered round — a round is simply the batch of comments submitted together; rounds carry no verdict —
  and wakes `ccr wait`; success → toast *"Round N submitted · K comments"* and a refetch of the review and comments,
  error → red toast. Disabled (muted) while nothing is pending or a submit is in flight. Pending comments are already
  readable by Claude before that (`ccr comments`).

### 7.7 Theme tokens

`style.css` defines all colours as custom properties on `:root[data-theme=light]` and `:root[data-theme=dark]`:
`--bg --bg-2 --bg-3 --fg --fg-muted --border --accent --accent-fg --diff-add --diff-add-strong --diff-del --diff-del-strong
--diff-hunk --diff-num --diff-empty --line-selected --line-flash --pending --resolved --new --danger --shadow --row-h
--file-header-h --top-h`. Light resembles GitHub (`#e6ffec/#abf2bc`, `#ffebe9/rgba(255,129,130,.4)`, `#ddf4ff`), dark
resembles GitHub dark-dimmed. `prefers-reduced-motion` disables animations. Focus rings visible. Buttons have
`aria-label`s. Relative times refresh every 60 s via `[data-ts]`.

### 7.8 DOM contract (shared by app.js, style.css and the e2e driver)

| Element | Selector |
|---|---|
| Regions | `#app`, `#topbar`, `#sidebar`, `#main`, `#toasts`, `#tooltip` |
| Topbar | `#btn-viewmode` (text = current mode "Unified"/"Split"), `#btn-wrap`, `#btn-ws`, `#btn-theme`, `#btn-reload`, `#btn-submit` (`.label`, `.badge-pending`; `:disabled` while nothing is pending), `#btn-copy-link`, `#btn-sidebar` |
| Commit list | `#commit-list .commit-item[data-sha]` (`.is-selected`, `.is-range`, `.is-new`, `.commit-badge`) |
| File tree | `#file-tree .tree-folder[data-dir]`, `.tree-file[data-path]`, `#file-filter`, `#filter-status` (main pane: *"N of M files — clear"*) |
| Header | `#commit-header .subject`, `.sha-copy`, `#btn-comment-commit`, `#commit-header .thread-block[data-key-host="commit"]`; combined view only: `#outdated-note` (hidden at 0), `#cover-letter` (`.cover-body` rendered Markdown, or `.is-empty` with the *"No cover letter"* hint), `#btn-comment-review`, `#commit-header .thread-block[data-key-host="review"]` |
| File card | `.file-card[data-path][data-rendered="0|1"]` → `.file-header` (`.file-path`, `.status-badge`, `.btn-comment-file`, `.btn-collapse`), `.diff-body`, `.file-card.is-collapsed` |
| Diff table | `table.diff[data-view]`; `tr.hunk` (`.btn-expand-up`, `.btn-expand-down`, `.btn-expand-all`); `tr.line.add|del|ctx[data-o][data-n][data-x]` (`.is-selected`, `.in-range`); `td.num.old|new[data-side][data-line]`, `td.num.empty`, `td.marker`, `td.code.old|new`, `td.code.empty`, `span.wd`, `span.cr` |
| Gutter | `button.btn-add-comment[data-side][data-line]` (shared, moved into the hovered `td.num`) |
| Editor | `tr.editor` / `div.editor-block` → `form.comment-editor[data-key]` (`textarea`, `.md-preview`, `.btn-submit-comment`, `.btn-cancel-comment`) |
| Thread | `tr.threads[data-key]` / `div.thread-block` → `.thread[data-thread-id]` (`.is-resolved`, `.has-new`) → `.comment[data-id][data-author]` (`.comment-meta` `.author .time .tag-pending .tag-round .tag-edited .tag-new .tag-moved`, `a.tag-from[data-sha]` on a projected root, `.comment-body`, `.comment-actions` `.act-edit .act-delete .act-reply .act-resolve`), `button.btn-reply`, `button.btn-show-resolved` |
| Banners/toasts | `#banner-disconnected`, `#banner-compare`, `#banner-reloaded`, `#toasts .toast.info|error|success`, `#notice-token` |
| Readiness | `body[data-ready="1"]` after the first full render; `body[data-loading="1"]` while the server reports `loading` |

**Anchor keys** (`data-key`, `threadsByKey: Map<key, rootId[]>` sorted by `created_at`): `line:<commit>|<path>|<side>|<endLine>`,
`file:<commit>|<path>`, `commit:<commit>`, `review:`.

### 7.9 State shape

```js
state = { token, review, generation, version, startedAt, nowOffset, selectedSha, compare: null|{base, head},
  viewMode, wrap, wsIgnore, theme, diffs: Map<sha, CommitDiff>, fileText: Map<'sha|path', {side, lines, count}>,
  hl: Map<'sha|path|side', Line[]>, comments: Map<id, Comment>, threadsByKey: Map<key, id[]>, threadOrder: id[],
  orphans: Set<id>, openEditors: Map<key, {...}>, sel: null|{...}, currentFile: number, currentThread: id|null,
  collapsedFolders: Set<string>, collapsedFiles: Set<path>, submitting: boolean,
  seenUntil: string, perCommitScroll: Map<sha, {top, path}> }
```

---

## 8. Agent loop (`skills/review-commit-series-commit-series/SKILL.md`)

The skill (frontmatter `name: review`, `argument-hint` for an optional range passed as `$ARGUMENTS`, `description` mentioning "show me the code / review UI / commit chain
review") teaches Claude Code to:

1. **Start**: decide the range (usually `<base>..HEAD`; add `--worktree` when `git status --porcelain` is non-empty),
   run `ccr start --repo <abs path> --range <spec> [--worktree]` and hand the user the URL **on its own line**, verbatim:
   *"Open http://127.0.0.1:PORT/?t=… (over SSH: `ssh -L PORT:127.0.0.1:PORT <host>` first), leave comments, then click
   **Submit** — or just tell me when you are done."* Never use `--open` (the agent cannot see the browser; the
   user may be remote).
2. **Pre-annotate** (optional): `ccr comment --commit <sha> --path <p> --line <n> "Heads-up: …"` to explain non-obvious
   choices before the human looks.
3. **Wait**: run `ccr wait --repo <abs> --since-round <last processed> --timeout 590` (Bash timeout 600000, ideally
   `run_in_background`/Monitor); exit 2 → re-run (optionally check `ccr status` for `ui: not opened yet`); exit 3 →
   `ccr sessions` / `ccr logs`. If the user says "done" without submitting, read `ccr comments --pending`.
4. **Address**: read `ccr comments --unanswered` (or the round output) — every thread, in one pass. Make the code
   changes (**prefer new fixup commits over amend/rebase during review** so anchors stay valid; squash after approval).
   Reply to all threads at once with `ccr reply --batch -` (Markdown `## <id> [resolve]` sections), citing the fix
   commit sha; use `[resolve]` only when the fix is committed; reply without resolving to push back or ask.
5. **Reload**: `ccr reload --repo <abs>` (never with a narrower range); read the remapped/outdated list; tell the user
   what changed and that the UI is refreshed. Repeat 3–5 until the user says the review is done (rounds carry no verdict).
6. **Stop**: only when the user explicitly asks (`ccr stop` exports to Markdown first; comments are otherwise gone).
   If the conversation ends without a decision, leave the server running and say so.

Rules: always pass `--repo <absolute path>`; use `--json` when acting on ids programmatically; if working in a
separate git worktree, start ccr with `--repo` on that worktree; never `ccr stop` on your own.

Install (as a plugin): `ln -s <checkout> ~/.claude/skills/ccr` (auto-loads as `ccr@skills-dir`), or `claude plugin marketplace add <checkout> && claude plugin install ccr@ccr-local`, or `claude --plugin-dir <checkout>`. The plugin's `bin/` is on PATH while it is enabled; outside Claude Code use `bin/ccr` or `pip install -e .`.

---

## 9. Tests (`tests/`)

* `conftest.py`: `fixture_repo(tmp_path)` builds a repo with deterministic author/dates containing: 5 commits on `main`
  → branch `feature` with ≥ 4 commits: a file modified in 3 hunks, a rename with edits (`-M`), a 100 % rename, a deleted
  file, a new binary file, a file named `dir with space/ünïcode.txt`, a file lacking a trailing newline (and a hunk
  with two `\ No newline` markers), a mode change (`chmod +x`), a symlink, a merge commit (merge `main` back into
  `feature`), an empty commit (`--allow-empty`), a big generated file (6000 changed lines) for `too_large`, a CRLF file,
  `.gitattributes` `*.dat diff=hex` + `diff.hex.textconv=xxd` (must still be `binary: true`); worktree: staged change,
  unstaged change, untracked file, untracked binary, untracked symlink, ignored file, nested repo directory.
* `test_gitx.py`: `parse_patch` on hand-written patches (rename, 100 % rename, binary, mode-only, no-newline ×2,
  multi-hunk, `/dev/null`, spaces/quoted paths, `@@ -1 +1 @@`, `--- ` content line, empty context line, type change =
  two sections); `list_commits` order/fields/`%B` split; `commit_stats` vs `--numstat` for every commit incl. merge and
  root; `diff_commit` merge = first parent, root = empty tree; `diff_range`; `diff_worktree` (untracked incl. symlink/binary,
  excludes ignored/nested); `show_file` (blob, dir → 404, binary → 415, worktree symlink, escape → 403); `resolve_range`
  (`A..B`, `A...B`, `A`, `..B`, `A..`, `-n` incl. clamp, non-ancestor base → merge-base + note, default detection, empty,
  bad spec `-x`); `map_line` (same/moved/changed/deleted/file-deleted/rename); `-w`.
* `test_store.py`: CRUD, reply anchor copy, validation errors, old_path normalisation, snippet capture (single/range,
  old/new), submit (states, rounds, summary comment, approve-with-nothing, request_changes-with-nothing → 400),
  edited flag, delete 409/cascade, outdated + re-anchoring after a commit amend (same subject) and after a rebase,
  version/generation bumps, `wait` wake-up via thread, `locate` head locations, file-db reopen (version continues, ids unique).
* `test_server.py`: real server on port 0; 401 without token / `?t=` on api; 400 bad Host; 403 bad Origin (port
  mismatch allowed), 415/411/413; every route happy path; 404s; `/api/file` binary → 415, bad rev → 400; `/api/compare`
  with a non-review sha → 400; `/api/events` early return on change and on `since > version`; 503 while loading;
  shutdown; static traversal → 404; MIME table; CSP header present.
* `test_cli.py`: `bin/ccr start --repo <fixture> --range main..feature --worktree --json` (token only in env of the child —
  assert `ps` cmdline lacks it; session/log/db files 0600, dir 0700) → `status` → `comment` → `comments` (ids, snippets
  with line numbers, HEAD arrow) → `wait --timeout 5` in a thread + POST submit → output header → `reply --resolve`,
  `reply --batch` (Markdown), duplicate refusal → `comments --unanswered` → `export` → new commit + `reload` (counts,
  remap after amend) → `start` again = reuse → `sessions` → `stop` (export file written, session file gone, pid gone).
  Also: `start` with a bad range exits 1 immediately with the git message; `start` when the child crashes prints log tail.
* `test_render.py`: golden Markdown for a fixed comment set incl. `clean()` escapes (`#` bodies, control chars in subjects).
* `test_e2e.py` (skipped without `chromium-browser`/`chromium`/`google-chrome`): starts a server, runs
  `node tests/e2e/driver.mjs <url>` (CDP over Node's `WebSocket`) which: loads the page (token in `?t=`), waits for
  `body[data-ready]`, asserts every lang id from the section-3 table satisfies `hljs.getLanguage`, clicks the 2nd
  commit, hovers a diff row and clicks the gutter `[+]`, types a comment, submits it (thread appears), types into another
  editor and checks the live preview (`<code>`) and that Cancel keeps the draft, checks that `j` does nothing (shortcuts
  off), toggles split view (thread still present), drags a 3-line range and comments, submits the round with `#btn-submit`,
  then creates a claude reply via the API and asserts the toast + New dot, reloads the page and asserts the token
  survives (localStorage) — prints `{ok, steps:[…], consoleErrors:[…], screenshots:[paths]}`; the test asserts `ok`,
  zero console errors, and the API state (1 round with verdict `comment`, 3 user comments + the reply).

Run: `python3 -m pytest -q`. All tests must pass with no network access.

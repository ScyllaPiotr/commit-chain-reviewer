---
name: review
description: Show the user a GitHub-like review UI of a commit chain via ccr and process their inline comments — use when the user asks to show/review the code, see the commits or the diff, look at the changes, get a review link/URL, or leave and address review comments on a commit chain.
argument-hint: "[range] e.g. main..HEAD (optional; defaults to the branch point..HEAD)"
---

# review — run a ccr review loop

Invocation: `/ccr:review [range]` or plain language ("show me the code", "let me review the change").
`$ARGUMENTS`, when given, is the commit range to show (a spec such as `main..HEAD`, `origin/main...HEAD`
or `-n 3`); otherwise pick the range yourself as described in step 1.

`ccr` (Commit Chain Reviewer) serves a local, GitHub-like review UI for a range of commits. You start it,
hand the user a URL, wait for their review round, fix the code, answer every thread from the CLI, reload
the diff, and repeat until they approve. You never see the browser; the CLI is your whole interface, and
`ccr comments` / `ccr wait` print Markdown written for you: every thread has an id, a numbered code
snippet and a `→ HEAD path:line` location.

## Setup

- Use `ccr` from PATH (the plugin puts its `bin/` on PATH while enabled). If it is missing, use
  `${CLAUDE_PLUGIN_ROOT}/bin/ccr` (same arguments; substitute it everywhere below). Requirements: python3 ≥ 3.10
  (with its bundled sqlite3), git ≥ 2.24, a browser on the user's side — nothing to install.
- Set `REPO` to the absolute toplevel of the repository under review:
  `REPO=$(git rev-parse --show-toplevel)`. Pass `--repo "$REPO"` on **every** ccr command. If you work in
  a linked git worktree, `REPO` is that worktree's path (each worktree is its own session).
- Open the browser for the user **only** when they are on this machine with a display: `DISPLAY` or
  `WAYLAND_DISPLAY` is set and `SSH_CONNECTION` is not. Then run `ccr open --repo "$REPO"` right after
  `ccr start` (or pass `--open` to `ccr start`). Otherwise never try: you cannot see the browser and the user
  may be on another machine. In both cases hand over the URL as well (step 1.4).
- Never run `ccr stop` on your own initiative (it deletes the review data).

## The loop

### 1. Start the server and hand over the URL

1. Choose the range explicitly. Default to `<base>..HEAD` with the branch point as base (`main..HEAD`,
   `origin/main...HEAD` for merge-base semantics, or a sha the user named). Do not rely on default detection.
2. Run `git -C "$REPO" status --porcelain`; when the output is non-empty add `--worktree` so the
   uncommitted work shows up as the "Uncommitted changes" pseudo-commit.
3. Write the **cover letter** — the description of the whole change, like a pull-request body — to a
   scratch file (e.g. `$TMPDIR/ccr-cover.md`): what the change does, why, how it is split into commits,
   what to look at first, open questions. The user can comment on it as a whole ("Comment on the whole
   change") and it is the first thing they see under **All changes**. Then start:

   ```sh
   ccr start --repo "$REPO" --range main..HEAD [--worktree] --cover "$TMPDIR/ccr-cover.md"
   ```

   (`ccr cover --repo "$REPO" --file FILE` replaces the cover letter of a running review, e.g. after
   the chain changed.)

   Success prints two lines (plus an optional `ccr: note: …`):

   ```
   ccr: serving /abs/repo  (main..HEAD, 7 commits, +worktree)
   ccr: url http://127.0.0.1:7777/?t=<token>
   ```

   `ccr: reusing running session (pid P)` followed by reload lines and the URL means a session already
   existed and was reloaded with your options — that is fine, continue.
   Exit 1 with `range X..Y is empty` (no commits: fix the base or add `--worktree`) or a git error about
   the spec → fix the range and retry. Exit 1 with `ccr: server exited with code N — last log lines:` →
   read the tail, then `ccr logs --repo "$REPO" -n 100`.
4. If the user is local (`DISPLAY`/`WAYLAND_DISPLAY` set, `SSH_CONNECTION` unset) run
   `ccr open --repo "$REPO"` so the review opens in their browser, and say so. Then, in every case, hand the
   URL to the user **verbatim, on its own line** — the `?t=` token in it is what authorises the browser. Use this sentence, substituting the printed URL and its port for `PORT`:

   > Open http://127.0.0.1:PORT/?t=… (over SSH: `ssh -L PORT:127.0.0.1:PORT <host>` first), leave
   > comments, then click **Submit review** — or just tell me when you are done.

   Do not shorten the URL, wrap it in Markdown link syntax or split it across lines; the user copies
   it as is.

### 2. Pre-annotate (optional)

Explain non-obvious choices before the user looks, one short comment each:

```sh
ccr comment --repo "$REPO" --commit <sha> --path <path> --line <n> "Heads-up: …"
ccr comment --repo "$REPO" --commit <sha> "Heads-up: this commit only moves code."
ccr comment --repo "$REPO" --review "Heads-up: the first three commits are pure refactoring."
```

`--commit` takes a full or short listed sha, `combined` or `worktree`; add `--start-line M` for a range,
`--side old` for a deleted line. Do not annotate every commit — only what would otherwise puzzle a reviewer.

### 3. Wait for the review round

```sh
ccr wait --repo "$REPO" --since-round <N> --timeout 590
```

`N` is the last round you processed (`0` before the first). Always pass it explicitly: the default is
the round count at call time, which would skip a round submitted while you were working. Run it with
the Bash tool's `timeout: 600000`, preferably `run_in_background: true` (or drive it with Monitor) so
you are woken up instead of blocking. Then act on the exit code:

| Exit | Meaning | Do |
|---|---|---|
| 0 | a new round exists | stdout starts with `ccr: round <n> — <verdict> — <k> new comments in <j> threads`, then the round's threads in the Markdown format below (new comments marked `★ new in round n`). Go to step 4. |
| 2 | timeout, no new round | stderr: `ccr: no new round after 590 s (rounds: R, pending unsubmitted: P, version: V)`. Re-run the same command. If it printed `ccr: UI not opened yet` (or `ccr status --repo "$REPO"` shows the UI not connected), remind the user of the URL and the SSH forward once — do not nag. |
| 3 | `ccr: server gone` / no running session | `ccr sessions`, then `ccr logs --repo "$REPO"` for the reason. If the server crashed, `ccr start` again with the same options — the database survived, comments come back — and hand over the **new** URL (the token changed). If the user ran `ccr stop`, the review was exported to Markdown in the session directory; ask before starting a fresh one. |

If the user says they are done without clicking Submit, do not wait: read
`ccr comments --repo "$REPO" --pending` — pending comments are already visible to you.
`ccr wait --any` returns on any change (a single new comment); use it only when the user asked you to
react live.

### 4. Address every thread in one pass

1. Read everything first:

   ```sh
   ccr comments --repo "$REPO" --unanswered      # unresolved, non-outdated threads whose last comment is the user's
   ```

   Use the round output from `ccr wait` as well; `--json` when you handle ids programmatically.
   Anchors (`new:11`, `old:7`, `new:20-24`) are diff-relative; `→ HEAD path:line` is where that line is
   **now** — edit there. `(moved)`, `(changed near)`, `(deleted)`, `(file deleted)` and `(live)` qualify it.
2. Make the code changes for all threads before replying to any of them.
3. Commit as **new commits, never amend or rebase during the review**: `git commit --fixup=<sha>` (or a
   plain commit) keeps every anchor valid and lets the user see exactly what changed via "Changes since
   round N". Squash (`git rebase -i --autosquash`) only after approval, and only if the user wants it.
4. Reply to every thread in one call, citing the fix commit (a short sha of at least 7 hex chars is
   rendered as a link):

   ```sh
   ccr reply --repo "$REPO" --batch - <<'EOF'
   ## k3f9a2 [resolve]
   Switched to `backoff.retry()` in 1a2b3c4.
   ## m7q2rt
   Kept the explicit loop — the helper has no jitter. Want me to add jitter to the helper instead?
   ## z1y2x3 [resolve]
   Right, the guard was dead code. Removed in 5d6e7f8.
   EOF
   ```

   - `## <id> [resolve]` = reply and resolve; use `[resolve]` **only when the fix is committed**.
   - `## <id>` without `[resolve]` = reply and leave open: push back, ask a question, or announce a fix
     you have not made yet.
   - Output is one line per item: `<id>: replied[, resolved]` or `<id>: ERROR …`; exit 1 if any item
     failed. Re-run only the failed ids. `identical reply already exists on this thread` means it was
     already posted — do not force it.
   - JSON is accepted too: `[{"id": "k3f9a2", "body": "…", "resolve": true}, …]`.
   - Single replies: `ccr reply --repo "$REPO" <id> "text" [--resolve]`; `ccr resolve <id>…` /
     `ccr unresolve <id>…` flip threads without replying (prefer replying).

### 5. Reload and report

```sh
ccr reload --repo "$REPO"
```

Never pass a narrower `--range`; omit it so the pinned base stays and new commits appear (add
`--worktree` / `--no-worktree` only when the uncommitted state should change). It prints
`ccr: N commits (was M), +a −r, K comments remapped, J now outdated`, and — when reviewed commits
disappeared — `warning: R reviewed commits left the range` plus one line per now-outdated thread. Fix
any thread that became outdated by accident (`ccr move <id> --commit <sha> --path <p> --line <n>`).

Tell the user, briefly: the commits you added (short shas + subjects), which threads you resolved,
which you left open and why, and that the UI is already refreshed (it live-updates; no F5 needed).
Then return to step 3 with `--since-round <n>` of the round you just handled.

Repeat 3 → 5 until a round arrives with verdict `approve`. Then ask whether to squash the fixups and
whether to stop the server.

### 6. Stop — only when the user asks

```sh
ccr stop --repo "$REPO"
```

`ccr stop` exports the whole review to Markdown first (`ccr: exported to <path>`, in the session
directory) and then deletes the database, so the comments are otherwise gone. If the user wants the
export in a specific place, run `ccr export --repo "$REPO" --md -o <file>` before stopping. If the
conversation ends without a decision, leave the server running and say so:
"ccr is still serving `<url>`; run `ccr stop` when you are finished."

## Rules

- Always `--repo <absolute path>`; never depend on the current directory.
- Always pass `--range` explicitly on `ccr start`; add `--worktree` when `git status --porcelain` is non-empty.
- Always start with a cover letter (`--cover FILE`); it is the review's "PR description".
- Hand over the URL verbatim, on its own line, with the SSH hint. Open the browser (`ccr open`) only when a
  local display is available and the session is not over SSH.
- Treat pending comments as real: the user may never click Submit.
- Process every thread of a round in one pass; reply to all of them in one `ccr reply --batch -` call.
- `[resolve]` only for committed fixes; disagree or ask by replying without it.
- Fixup/new commits during the review; no amend, no rebase, no force-push until approved.
- `ccr reload` after every batch of commits, never with a narrower range.
- Use `--json` when acting on ids programmatically.
- Never `ccr stop` unless told to; when stopping, remember it exports first and then deletes the data.
- After changing ccr's own Python code, restart the server with `ccr stop --keep-db && ccr start …` (a running
  server keeps the code it started with; `--keep-db` preserves comments, rounds and the cover letter) and hand
  over the new URL.
- Do not paste the token/URL into commit messages, issues or files.

## What you will read

### `ccr comments` / `ccr wait` Markdown

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
````

How to read it:

- `#### [id: XXXXXX]` opens a thread; the id is what `ccr reply` takes. Replies are indented and start with `↳`.
- `user · new:11` = comment by the human on new-side line 11 of that commit's diff; `old:7` = deleted
  side; `new:20-24` = a range (act on the whole range); `file` / `commit` = anchored to the file / commit.
- `→ HEAD src/fetcher.py:14 (moved)` = that line is now line 14 in HEAD. Edit at the HEAD location.
- `pending` = not yet in a submitted round (still act on it); `R1` = submitted in round 1;
  `unresolved`/`resolved`; `last: user` = awaiting your answer.
- Snippet rows: `<old#> <new#> <marker> <text>`; the anchored line(s) are prefixed with `>`.
- Matching comments are marked `★` when filters are active; `ccr wait` marks `★ new in round n`.
- `## Outdated …` threads are anchored to commits that left the range; re-anchor them with `ccr move`
  or answer them by id like any other thread.
- Bodies never open headings (`#` lines are escaped as `\#`), so `##`/`####` lines are always structure.

### `--json`

`ccr comments --json` and `ccr export --json` share one shape:

```jsonc
{
  "review":  { "repo": {...}, "range": {...}, "commits": [ CommitMeta without files ], "counts": {...}, ... },
  "rounds":  [ {"number": 1, "verdict": "request_changes", "summary": "…", "submitted_at": "…Z",
                "base": "…", "head": "…", "commit_shas": ["…"], "comment_ids": ["…"]} ],
  "comments": [ {"id": "k3f9a2", "parent_id": null, "author": "user", "body": "…",
                 "state": "pending", "round": null, "resolved": false,
                 "anchor": {"kind": "line", "commit": "9fceb02…", "path": "src/fetcher.py",
                            "side": "new", "line": 11, "start_line": null},
                 "snippet": "    y = 3", "outdated": false, "moved_from": null,
                 "head_location": {"path": "src/fetcher.py", "line": 14, "status": "moved"},
                 "created_at": "…Z", "updated_at": "…Z"} ],
  "threads": [ {"root": Comment, "replies": [Comment], "last_author": "user", "answered": false} ]
}
```

`threads[].answered` is false when the last comment is the user's; together with an unresolved,
non-outdated root that is exactly what `--unanswered` selects.

## Commands cheat sheet

| Purpose | Command |
|---|---|
| Start / reuse the server | `ccr start --repo "$REPO" --range <base>..HEAD [--worktree]` |
| Where is it, is the UI connected | `ccr status --repo "$REPO"` |
| Wait for the next round | `ccr wait --repo "$REPO" --since-round <N> --timeout 590` (exit 0 round · 2 timeout · 3 server gone) |
| Wait for any change | `ccr wait --repo "$REPO" --any --timeout 590` |
| Threads awaiting my answer | `ccr comments --repo "$REPO" --unanswered` |
| Unsubmitted comments | `ccr comments --repo "$REPO" --pending` |
| One round / one file / one commit | `ccr comments --repo "$REPO" --round N` · `--path P` · `--commit SHA` |
| Everything, machine-readable | `ccr comments --repo "$REPO" --json` |
| Answer many threads | `ccr reply --repo "$REPO" --batch -` with `## <id> [resolve]` sections on stdin |
| Answer one thread | `ccr reply --repo "$REPO" <id> "text" [--resolve]` |
| Resolve / reopen without replying | `ccr resolve --repo "$REPO" <id>…` / `ccr unresolve --repo "$REPO" <id>…` |
| Pre-annotate | `ccr comment --repo "$REPO" --commit <sha> [--path P [--line N [--side old] [--start-line M]]] "text"` |
| Whole-review note | `ccr comment --repo "$REPO" --review "text"` |
| Set / replace the cover letter | `ccr cover --repo "$REPO" --file cover.md` (or `ccr start … --cover cover.md`) |
| Fix my own comment | `ccr edit --repo "$REPO" <id> "text"` · `ccr delete --repo "$REPO" <id> [--cascade]` |
| Re-anchor a thread | `ccr move --repo "$REPO" <id> --commit <sha> [--path P [--line N]]` |
| Pick up new commits | `ccr reload --repo "$REPO"` |
| Server log | `ccr logs --repo "$REPO" -n 100` |
| All sessions on this machine | `ccr sessions` |
| Save the review | `ccr export --repo "$REPO" --md -o review.md` |
| Stop (user asked; exports first, then deletes) | `ccr stop --repo "$REPO"` |

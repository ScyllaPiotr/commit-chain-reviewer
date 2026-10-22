---
name: review-commit-series
description: Show the user a GitHub-like review UI of a commit chain via ccr and process their inline comments — use when the user asks to show/review the code, see the commits or the diff, look at the changes, get a review link/URL, or leave and address review comments on a commit chain; also to read or review someone else's GitHub pull request in that UI (PR mode), with inline questions for Claude and GitHub comments posted to the user's pending review.
argument-hint: "[range] e.g. main..HEAD (optional; defaults to the branch point..HEAD)"
---

# review-commit-series — run a ccr review loop

Invoked as `/review-commit-series [range]` or in plain language ("show me the code"). `$ARGUMENTS`, when given, is
the range (`main..HEAD`, `origin/main...HEAD`, `-n 3`); otherwise step 1 picks it.

ccr serves a local, GitHub-like review page for a range of commits. A round of comments carries no verdict: each is a
question or a request for a change. You never see the page: the CLI is your whole interface ("What you will read").

## Setup

- Run `ccr` from PATH (the plugin's `bin/`), else `${CLAUDE_PLUGIN_ROOT}/bin/ccr`. It needs python3 ≥ 3.10 and
  git ≥ 2.30; there is nothing to install.
- `REPO=$(git rev-parse --show-toplevel)`, and pass `--repo "$REPO"` to **every** ccr command; never rely on the
  current directory. In a linked worktree `REPO` is the worktree, a session of its own.
- Keep scratch files in `SCRATCH`: your scratchpad directory, else `SCRATCH=$(mktemp -d)` (`$TMPDIR` may be unset).
- Open the browser (`ccr open`, or `ccr start --open`) only when the user is on this machine with a display:
  `DISPLAY` or `WAYLAND_DISPLAY` set and `SSH_CONNECTION` not. Hand over the URL either way.

## The loop

### 1. Start and hand over the URL

1. Choose the range explicitly, normally `<branch point>..HEAD`: `main..HEAD`, `origin/main...HEAD` (from the merge
   base), or a sha the user named. Do not rely on ccr's default detection.
2. If `git -C "$REPO" status --porcelain` prints anything, add `--worktree`, so the uncommitted work shows as
   "Uncommitted changes".
3. Always write a **cover letter** to `$SCRATCH/ccr-cover.md`: the future pull-request description — what the change
   does, why, how it is split into commits, what to look at first, open questions. It heads **All changes**, and the
   user can comment on it. Then:

   ```sh
   ccr start --repo "$REPO" --range main..HEAD [--worktree] --cover "$SCRATCH/ccr-cover.md"
   ```

   Comments on the cover letter are edits: apply them, and replace it after the chain changed, with
   `ccr cover --repo "$REPO" --file FILE`. Once the review is done and the series pushed, the final text is the PR
   body, verbatim: `gh pr create --body-file <cover file>`, or `gh pr edit --body-file` for an existing PR.

   It prints `ccr: serving /abs/repo  (main..HEAD, 7 commits, +worktree)` and `ccr: url http://127.0.0.1:7777/?t=…`,
   maybe a `ccr: note: …`. Between the two, `ccr: resuming review #N …` means this change's earlier review (comments,
   rounds) was reopened, `ccr: new review #N — the database also holds review #M …` that another change's review is
   kept and this one starts empty: tell the user which. `ccr: reusing running session (pid P)` means a running
   session was reloaded with your options. Exit 1 with `range X..Y is empty` (fix the base or add `--worktree`) or a
   git error about the spec: fix the range and retry. On `ccr: server exited with code N — last log lines:`, read
   them, then `ccr logs --repo "$REPO" -n 100`.
4. If the user is local (Setup), run `ccr open --repo "$REPO"` (unless `ccr start` had `--open`) and say so. Either
   way hand over the URL **verbatim, on its own line** — its `?t=` token authorises the browser, so never shorten it,
   make it a Markdown link or split it — then this sentence, with its port for `PORT`:

   > http://127.0.0.1:PORT/?t=…
   >
   > Open it (over SSH: `ssh -L PORT:127.0.0.1:PORT <host>` first), leave comments, then click **Submit** — or just
   > tell me when you are done.

### 2. Pre-annotate (optional)

Explain only what would puzzle a reviewer, one short comment each:

```sh
ccr comment --repo "$REPO" --commit <sha> --path <path> --line <n> "Heads-up: …"
ccr comment --repo "$REPO" --commit <sha> "Heads-up: this commit only moves code."
ccr comment --repo "$REPO" --review "Heads-up: the first three commits are pure refactoring."
```

`--commit` takes a listed sha (full or short), `combined` or `worktree`; `--path` without `--line` comments on the
file, `--start-line M` makes a range, `--side old` anchors to a deleted line.

### 3. Wait for the round

```sh
ccr wait --repo "$REPO" --since-round <N> --timeout 590
```

`N` is the last round you handled (`0` at first). Always pass it: the default, the round count at call time, skips a
round submitted while you worked. Give the Bash call `timeout: 600000`, preferably with `run_in_background: true` or
Monitor, then go by the exit code:

| Exit | Meaning | Do |
|---|---|---|
| 0 | a new round | stdout starts `ccr: round <n> — <k> new comments in <j> threads`, then its threads (new comments marked `★ new in round n`). Go to step 4. |
| 2 | timeout | Re-run the same command. On `ccr: UI not opened yet` (or `ccr status` showing the UI not connected), remind the user of the URL and the SSH forward once; do not nag. |
| 3 | server gone | `ccr sessions`, then `ccr logs --repo "$REPO"`. After a crash, `ccr start` with the same options (the database kept everything) and hand over the **new** URL: the token changed. If the user ran `ccr stop`, ask before starting again (with the same options within a week, that resumes the review). On `server was replaced while waiting (new token)`, hand over the URL `ccr status` prints. |

**Wait for Submit: pending comments are drafts, not instructions.** You can read them before the user is done, and
they may still change or go; acting on one answers a point the user never made, resolves threads they wanted open
and changes the review under them while they write. A draft that reads as urgent ("fix this first") is still a
draft. On exit 2 just wait again, and avoid reading `ccr comments --pending` while a round is open.

Only two things release you: `ccr wait` returns a round, or the user says in chat they are done commenting. In that
second case `ccr comments --repo "$REPO" --pending` is the round. `ccr wait --repo "$REPO" --any --timeout 590`
returns on any change; use it only when the user asked you to react live.

### 4. Address every thread in one pass

1. Read everything first: the round output and `ccr comments --repo "$REPO" --unanswered` (unresolved, non-outdated
   threads awaiting your answer); `--json` to handle ids programmatically. Edit at `→ HEAD path:line`, where the
   anchored line is now.
2. Make the code changes for all threads before replying to any.
3. Commit them as **new commits** (`git commit --fixup=<sha>`, or plain ones): no amend, rebase or force-push until
   the user declares the review done, so every anchor stays valid and the page marks the commits added since the last
   round. Squash (`GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash <base>`, which needs no editor) only after
   approval, and only if the user wants it.
4. Reply to every thread in one call, citing the fix commit (a short sha of 7+ hex chars becomes a link):

   ```sh
   ccr reply --repo "$REPO" --batch - <<'EOF'
   ## k3f9a2 [resolve]
   Switched to `backoff.retry()` in 1a2b3c4.
   ## m7q2rt
   Kept the explicit loop — the helper has no jitter. Want me to add jitter to the helper instead?
   EOF
   ```

   `[resolve]` also resolves the thread: use it **only when the fix is committed**; without it the thread stays open,
   to push back, ask, or announce a fix not made yet. Each item prints `<id>: replied[, resolved]` or
   `<id>: ERROR …`, exiting 1 if any failed: re-run only those. `identical reply already exists on this thread` means
   it is posted already; do not force it. JSON works too (`[{"id": "k3f9a2", "body": "…", "resolve": true}]`), and
   `ccr reply --repo "$REPO" <id> "text" [--resolve]` answers one thread.

### 5. Reload and report

```sh
ccr reload --repo "$REPO"
```

Run it after every batch of commits, in a round or not. Never pass a narrower `--range`: without one the range keeps
its base and the new commits appear (`--worktree` / `--no-worktree` only to change what is shown of the uncommitted
work). It prints `ccr: N commits (was M), +a −r, K comments remapped, J now outdated`, and when reviewed commits left
the range a warning and one line per thread now outdated; re-anchor any outdated by accident with
`ccr move --repo "$REPO" <id> --commit <sha> [--path <p> [--line <n>]]`.

Tell the user briefly: the commits you added (short shas and subjects), the threads you resolved, those left open and
why, and that the page refreshed itself. Then go back to step 3 with the round you just handled. When the user says
the review is done (in chat or in a comment), ask whether to squash the fixups and whether to stop the server.

### 6. Stop — only when the user asks

`ccr stop --repo "$REPO"` exports the review to Markdown and JSON (`ccr: exported to <path>`, twice) and keeps the
database a week after its last change, so a `ccr start` within the week resumes the review. For an export where the
user wants it, first run `ccr export --repo "$REPO" --md -o <file>`. Never pass `--purge` unless the user asks to
delete the review: it deletes the session's exports too.

When a review was dropped (`--purge`, the week passed, a lost file) and the user wants it back, it comes back from an
export, after `--purge` only from one saved elsewhere: `ccr start` with the same options (`--pr` too),
`ccr restore --repo "$REPO" --dry-run <export>`, then the same without `--dry-run`. Prefer the `.json` export; a
`.md` one loses the snippets (taken from git again) and a thread's creation time. A restore fills only a review with
no comments or rounds of its own.

If the conversation ends without a decision, leave the server running and say so: "ccr is still serving `<url>`; run
`ccr stop` when you are finished."

## PR mode — someone else's pull request

For reading or reviewing a GitHub pull request the user did not write ("review PR #N with ccr"). You change no code.
Every comment is either a **question** you answer, or a **GitHub comment** you check and, when it holds, post
verbatim into the user's pending GitHub review. A text you wrote goes there only as "Your wording" below allows.

### Setup

1. Give the pull request head a review ref and a worktree of its own (a session belongs to one worktree, so it never
   collides with the user's own reviews in the clone); `gh pr view <N> --json url,author,baseRefName,headRefOid,body`
   gives the rest:

   ```sh
   git -C "$CLONE" fetch <remote> pull/<N>/head:refs/review/pr-<N> <base branch>
   git -C "$CLONE" worktree add --detach "$SCRATCH/pr-<N>" refs/review/pr-<N>
   REPO="$SCRATCH/pr-<N>"; BASE=$(git -C "$REPO" merge-base <remote>/<base branch> HEAD)
   ```

   `BASE` must be the merge base GitHub's "Files changed" uses, or `ccr gh-post` refuses to post.
2. The cover letter is the pull request body verbatim, then the line
   `PR: <url> by @<author> (the cover letter above is the PR body, verbatim)`.
3. Find the user's newest review of the pull request, for "Since your last review":

   ```sh
   gh api graphql -F owner=<owner> -F repo=<repo> -F number=<N> -f query='
     query($owner: String!, $repo: String!, $number: Int!) {
       viewer { login }
       repository(owner: $owner, name: $repo) { pullRequest(number: $number) {
         reviews(last: 100) { nodes { state submittedAt author { login } commit { oid } } } } } }'
   ```

   Take the viewer's newest review that is not `PENDING`. If its `commit.oid` is not the pull request head, fetch it
   (`git -C "$CLONE" fetch <remote> <oid>`; GitHub still serves a commit a force-push dropped) and add
   `--since <oid> --since-at <submittedAt>` to `ccr start`. No such review, or one on the head: no `--since`.
4. `ccr start --repo "$REPO" --range "$BASE..HEAD" --pr <url> --cover "$SCRATCH/ccr-cover.md" [--since …]`, then
   `ccr gh-sync --repo "$REPO"`, which mirrors the pull request's review threads and review bodies into ccr (read
   only). Hand over the URL as in step 1.4 and add: "**Ask AI** asks me, also in a thread; **GH comment** drafts a
   comment for your pending GitHub review, and **GH reply** a reply in a GitHub thread; you submit that review on
   GitHub." With `--since`, add: "**Since your last review**, above All changes, shows only what the author changed
   since your review of <date>; what a rebase brought in is left out." If the start's `ccr: since your last review
   of …` line says upstream changes may show (paths not rebuilt, or no shared base), tell the user where.

### Every round

First run `ccr gh-sync --repo "$REPO" --json` and read in full each comment under its `news` (added, edited or
deleted on GitHub since the last sync; later `gh-post` runs list only what changes after it). The `ccr wait` header
ends with `— <g> GitHub comments to check and post` when the round has some; each root says what it is right after
its author. A user's reply is a question unless it reads `GitHub reply (…)`, possible in any thread on a line or a
file. In a thread not on GitHub yet, its first GitHub comment starts one at the root's place (`--dry-run` shows that
place, not `reply to …`) and later GitHub replies go into it, so post a thread's GitHub comments in the order written
(`gh-post` refuses a reply whose thread starter is not posted).

The user is waiting for the answers, and checking GitHub comments takes much longer, so handle a round in two passes,
each ending in one `ccr reply --batch -` call:

1. **Questions.** Answer from the code, citing `path:line`, and change no file. A question in a mirrored thread is
   about that GitHub discussion: read the whole thread first. Reply without `[resolve]`: the user resolves. Send every
   answer, also in threads holding a GitHub comment of this round, before you check any GitHub comment.
2. **GitHub comments and replies.** Before posting one, check that every factual claim holds at HEAD (verify each
   `file:line`), that it asks something of this pull request and fits its line (a reply: the thread it answers), and
   that it has no typo or broken Markdown. `ccr gh-post --repo "$REPO" --dry-run <id>` shows where GitHub will put it
   and its verbatim body, posting nothing. Every `gh-post`, dry runs too, first re-reads GitHub and lists the news
   since the last sync: read each in full (`ccr comments`); when someone raised or answered the same point meanwhile,
   post nothing and tell the user in the thread, quoting it. A real run with news posts nothing (`<id>: ERROR not
   posted: … changed on GitHub since the last sync`); once you have read it, run it again.
   - Sound: `ccr gh-post --repo "$REPO" <id>…` posts each body verbatim into the user's pending review, starting one if
     there is none, and records it. That ends your part: no reply and no resolve (a posted comment drops out of
     `--unanswered`).
   - `GitHub comment (posted: <url>; edited since, …)`, or the same of a `GitHub reply`: the user edited it after
     posting. Check the new text like a new comment; when sound, `gh-post` updates it in the pending review
     (`<id>: updated → <url>`), never adding a second one.
   - Not sound: do not post it, and never post a reworded version on your own. Reply with what is wrong and one
     corrected wording, without `[resolve]`. The user edits the comment (which makes it pending again), says "post it
     as it is", or says "post it after your correction" ("Your wording" below), and submits again; then check the
     text as it is now, and post it.
   - `<id>: ERROR …`: nothing was posted, unless the line says posted but not recorded; then run the same `gh-post`
     again until it prints `<id>: found already in your pending review …` (a run before that may list the comment as
     news from the user and post nothing). Otherwise say in the thread what was refused and how to fix it: a line
     outside the pull request diff, or one that moved since, goes where it belongs, in **All changes**. `warning: …`:
     posted, but tell the user what the re-read found.

   Then reply to the GitHub comments you did not post, in one more `ccr reply --batch -` call (none when you posted
   them all).

Pending comments are drafts here too: `gh-post` refuses one until the user submits it. Post only with `ccr gh-post`,
and only the user's submitted GitHub comments that passed your check; never submit, edit or delete the GitHub review
or anything in it yourself: the user submits the review on GitHub, with the verdict and the body they choose.

### Your wording, on the user's approval

You may put a text you wrote into the user's pending review — your correction of their GitHub comment, or a GitHub
comment you proposed in a reply — only when the user's comment in a submitted round explicitly approves that concrete
text ("post it", "post it after your correction", "post them with these changes", the changes spelled out).
"Looks good", "maybe" or a pending draft approve nothing; when in doubt, ask in the thread and post nothing.

- Use exactly the approved text: your proposal with the user's stated changes, nothing else.
- Put it in as the user's draft, never post it straight away: a correction with
  `ccr edit --repo "$REPO" <id> --file -`, a new GitHub comment at the place your proposal named with
  `ccr comment --repo "$REPO" --as user --github --commit <sha> --path <p> [--line <n> [--side old] [--start-line <m>]] --file -`.
  Say in the approving thread what you added and that it waits for their Submit; it then comes back in a round, to
  check and post like any GitHub comment, a dry run first.
- A reply into a thread on GitHub cannot be written from the CLI (`ccr reply` writes questions and answers only):
  give the approved text in your reply and ask the user to add it with **GH reply** in that thread.

When the user is done, ask whether to stop the server (step 6) and offer to remove the worktree and the review ref.

## Rules

- Never `ccr stop` unless the user asks, and never stop or restart a session that is not yours.
- Changes to ccr's static files need no restart: a browser reload shows them. After a change to ccr's Python code a
  running server keeps the code it started with: say so, and restart it (`ccr stop && ccr start …`, which keeps
  comments, rounds and the cover letter) only when the user asks, then hand over the new URL (a restart issues a new
  token).
- Never put the URL or its token into commit messages, issues or files.

## What you will read

### `ccr comments` / `ccr wait` Markdown

````markdown
# Review comments — repo-name (main..HEAD) — 5 threads (3 pending, 2 unresolved, 1 unanswered)

## Rounds
- Round 1 · 2026-09-03T13:50:12Z · 4 comments

## Review-level comments

#### [id: …] user · review · R1 · unresolved · 0 replies · last: user
…

## Since your last review

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

#### [id: …] user · new:20-24 → HEAD src/fetcher.py:23-27 · R1 · resolved · 1 reply · last: claude · edited 2026-09-03T14:01:00Z
…

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
````

- `#### [id: …]` opens a thread, by the id `ccr reply` takes; replies start with `↳`.
- `new:11` is new-side line 11 of that commit's diff, `old:7` the deleted side, `new:20-24` a range (act on all of
  it); `file` and `commit` anchor to the file or the commit. `→ HEAD src/fetcher.py:14` is where that line is now,
  qualified by `(moved)`, `(changed near)`, `(deleted)`, `(file deleted)` or `(live)`. In the snippet, `>` marks the
  anchored rows.
- `pending`: a draft, not in a submitted round yet; `R1`: submitted in round 1; `last: user`: awaiting your answer;
  `★`: matched the filter (`★ new in round n` in `ccr wait`).
- `## Outdated …` threads are anchored to commits that left the range: re-anchor them with `ccr move`, or answer them
  by id like any other.
- A body line starting with `#` is escaped as `\#`, so outside code blocks `##` to `####` lines are always structure.
- PR mode: the header carries `— PR <owner/repo#N>`. Right after the author a user's root reads `question`,
  `GitHub comment (not posted)` or `GitHub comment (posted: <url>)`, a user's reply nothing (a question) or
  `GitHub reply (…)`; a mirrored root, by `@login (GitHub)` (`@login (GitHub, you)` for the user), reads
  `GitHub thread` or `GitHub review (…)`.

### `--json`

`ccr comments --json` and `ccr export --json` share one shape:

```jsonc
{
  "review": {"repo": {…}, "range": {…}, "commits": [{"sha", "subject", …}], "counts": {…}, …},
  "rounds": [{"number": 1, "submitted_at": "…Z", "commit_shas": ["…"], "comment_ids": ["…"], …}],
  "comments": [{"id": "k3f9a2", "parent_id": null, "author": "user", "body": "…", "state": "pending",
                "round": null, "resolved": false, "outdated": false, "github": null,
                "anchor": {"kind": "line", "commit": "9fceb02…", "path": "src/fetcher.py", "side": "new",
                           "line": 11, "start_line": null},
                "head_location": {"path": "src/fetcher.py", "line": 14, "status": "moved"}, …}],
  "threads": [{"root": "k3f9a2", "replies": ["p0o9i8"], "last_author": "user", "answered": false}]
}
```

`comments` holds every comment, whatever the filter; `threads` name theirs by id. A thread is not `answered` while
its last comment is the user's, unless that is a posted GitHub comment not edited since; `--unanswered` selects those
whose root is unresolved and not outdated.

## Other commands

| Purpose | Command |
|---|---|
| Where it is (the URL), is the page connected | `ccr status --repo "$REPO"` |
| Drafts / one round, file or commit | `ccr comments --repo "$REPO" --pending` · `--round N` · `--path P` · `--commit SHA` |
| Resolve or reopen without replying (prefer replying) | `ccr resolve --repo "$REPO" <id>…` · `ccr unresolve --repo "$REPO" <id>…` |
| Fix your own comment | `ccr edit --repo "$REPO" <id> "text"` · `ccr delete --repo "$REPO" <id> [--cascade]` |
| Flag lines that need a closer look (⚑) | `ccr flag --repo "$REPO" --path P --line N [--start-line M] [--side old] [--commit VIEW] "why"` (view `since` by default) · `ccr flags` · `ccr flag --clear` |
| Server log / every session on this machine | `ccr logs --repo "$REPO" -n 100` · `ccr sessions` |

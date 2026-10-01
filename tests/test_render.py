"""Tests for ccr.render (SPEC.md sections 6.2 and 6.3): golden Markdown, filters, JSON shape.

The comment set is fixed and hand-built (no git involved) so the expected Markdown can be
compared verbatim.  It covers every group heading, a threaded reply, a multi-line range, a
resolved+edited thread, review-level, file, commit, combined, worktree and outdated anchors,
and the ``clean()`` escapes (control/bidi characters in subjects and bodies, ``#`` bodies).
"""

from __future__ import annotations

import random

import pytest

from ccr import render

C1 = "9fceb02d3a" + "0" * 30
C2 = "1a2b3c4d5e" + "0" * 30
OLD = "1b2c3d4e5f" + "0" * 30
BASE = "a" * 40


def _commit(sha, subject, kind="commit", parents=None):
    return {
        "sha": sha, "short_sha": sha[:10], "kind": kind, "parents": parents or [], "is_merge": False,
        "shallow_boundary": False, "author": {"name": "Ann", "email": "ann@example.com"},
        "author_date": "2026-09-01T10:00:00Z", "commit_date": "2026-09-01T10:00:00Z", "subject": subject,
        "body": "", "stats": {"files": 1, "additions": 1, "deletions": 1},
        "files": [{"path": "src/fetcher.py", "old_path": None, "status": "M"}], "comment_count": 0,
    }


REVIEW = {
    "repo": {"path": "/work/demo", "name": "demo", "branch": "feature/x", "bare": False},
    "range": {"spec": "main..HEAD", "given": "-n 2", "base": BASE, "head": C2, "note": None, "first_parent": False},
    "options": {"worktree": True},
    "commits": [
        _commit("combined", "All changes", "combined"),
        _commit(C1, "Add retry\x07 to fetcher", parents=[BASE]),
        _commit(C2, "Tidy\u202e up", parents=[C1]),
        _commit("worktree", "Uncommitted changes", "worktree"),
    ],
    "version": 17, "generation": 2, "loading": False, "now": "2026-09-03T14:05:00Z",
    "counts": {"pending": 4, "submitted": 5, "unresolved": 8, "total": 10, "outdated": 1},
    "rounds": [{
        "number": 1, "submitted_at": "2026-09-03T13:50:12Z", "verdict": "request_changes",
        "summary": "Overall looks good, two nits.", "base": BASE, "head": C2, "commit_shas": [C1, C2],
        "comment_ids": ["r4ng3x", "c2line", "outd8d", "q8x1zz", "p0o9i8"],
    }],
    "server": {"pid": 1, "port": 7777, "started_at": "2026-09-03T13:00:00Z", "version": "0.1.0"},
    "ui": {"connected": True, "last_seen": "2026-09-03T14:04:00Z", "open_polls": 1},
}


def _anchor(kind, commit=None, path=None, side=None, line=None, start_line=None):
    return {"kind": kind, "commit": commit, "path": path, "side": side, "line": line, "start_line": start_line}


def _comment(cid, author, body, created, anchor, state="submitted", round=1, parent_id=None, resolved=False,
             updated=None, snippet="", outdated=False, head_location=None):
    comment = {
        "id": cid, "parent_id": parent_id, "author": author, "body": body, "created_at": created,
        "updated_at": updated or created, "state": state, "round": round, "resolved": resolved, "anchor": anchor,
        "snippet": snippet, "moved_from": None, "outdated": outdated,
    }
    if head_location is not None:
        comment["head_location"] = head_location
    return comment


COMMENTS = [
    _comment("k3f9a2", "user", "Why not use the existing backoff helper here?", "2026-09-03T13:45:00Z",
             _anchor("line", C1, "src/fetcher.py", "new", 11), state="pending", round=None, snippet="y = 3",
             head_location={"path": "src/fetcher.py", "line": 14, "status": "moved"}),
    _comment("p0o9i8", "claude", "Good catch — switched to `backoff.retry()` in 1a2b3c4.", "2026-09-03T13:52:10Z",
             _anchor("line", C1, "src/fetcher.py", "new", 11), parent_id="k3f9a2", snippet="y = 3"),
    _comment("r4ng3x", "user", "# Not a heading\nSecond line\ttabbed\x01", "2026-09-03T13:46:00Z",
             _anchor("line", C1, "src/fetcher.py", "new", 24, 20), resolved=True, updated="2026-09-03T14:01:00Z",
             snippet="a\nb\nc\nd\ne", head_location={"path": "src/fetcher.py", "line": 27, "status": "same"}),
    _comment("f1l3aa", "user", "Please add tests for this file.", "2026-09-03T13:47:00Z",
             _anchor("file", C1, "src/other.py"), state="pending", round=None),
    _comment("c0mm1t", "claude", "Heads-up: this commit only touches the retry path.", "2026-09-03T13:40:00Z",
             _anchor("commit", C1), round=0),
    _comment("c2line", "user", "typo", "2026-09-03T13:48:00Z", _anchor("line", C2, "README.md", "new", 3),
             snippet="end", head_location={"path": "README.md", "line": 4, "status": "changed"}),
    _comment("c0mb1n", "user", "Combined view note", "2026-09-03T13:49:00Z",
             _anchor("line", "combined", "src/fetcher.py", "old", 2), state="pending", round=None,
             snippet="import os", head_location={"path": None, "line": None, "status": "unknown"}),
    _comment("w0rktr", "user", "Uncommitted note", "2026-09-03T13:49:30Z",
             _anchor("line", "worktree", "notes.txt", "new", 1), state="pending", round=None, snippet="note one",
             head_location={"path": "notes.txt", "line": 1, "status": "live"}),
    _comment("outd8d", "user", "Old anchor", "2026-09-03T13:30:00Z", _anchor("line", OLD, "src/x.py", "new", 10),
             snippet="old text", outdated=True, head_location={"path": "src/x.py", "line": 10, "status": "same"}),
    _comment("q8x1zz", "user", "Overall looks good, two nits.", "2026-09-03T13:50:12Z", _anchor("review")),
]


def _hunk(old_start, old_count, new_start, new_count, section, rows):
    return {"old_start": old_start, "old_count": old_count, "new_start": new_start, "new_count": new_count,
            "section": section, "lines": [{"t": t, "o": o, "n": n, "s": s} for t, o, n, s in rows]}


FILE_DIFFS = {
    (C1, "src/fetcher.py"): {"path": "src/fetcher.py", "hunks": [
        _hunk(10, 3, 10, 4, "def foo(self):", [("ctx", 10, 10, "x = 1"), ("del", 11, None, "y = 2"),
                                               ("add", None, 11, "y = 3"), ("ctx", 12, 12, "return y")]),
        _hunk(20, 4, 20, 5, "def bar():", [("ctx", 20, 20, "a"), ("add", None, 21, "b"), ("ctx", 21, 22, "c"),
                                           ("ctx", 22, 23, "d"), ("ctx", 23, 24, "e")])]},
    (C2, "README.md"): {"path": "README.md", "hunks": [
        _hunk(1, 3, 1, 3, "", [("ctx", 1, 1, "# Demo"), ("del", 2, None, "teh"), ("add", None, 2, "the"),
                               ("ctx", 3, 3, "end")])]},
    ("worktree", "notes.txt"): {"path": "notes.txt", "hunks": [
        _hunk(0, 0, 1, 2, "", [("add", None, 1, "note one"), ("add", None, 2, "note two")])]},
}


def fetch(commit, path):
    return FILE_DIFFS.get((commit, path))


GOLDEN = """# Review comments — demo (main..HEAD) — 9 threads (4 pending, 8 unresolved, 5 unanswered)

## Rounds
- Round 1 · request_changes · 2026-09-03T13:50:12Z · 5 comments · "Overall looks good, two nits."  [id: q8x1zz]

## Review-level comments

#### [id: q8x1zz] user · review · R1 · unresolved · 0 replies · last: user
Overall looks good, two nits.

## Commit 9fceb02d3a — "Add retry to fetcher"

### src/fetcher.py

#### [id: k3f9a2] user · new:11 → HEAD src/fetcher.py:14 (moved) · pending · unresolved · 1 reply · last: claude
```diff
  10   10    x = 1
  11        -y = 2
>      11   +y = 3
  12   12    return y
 @@ -20,4 +20,5 @@ def bar():
  20   20    a
       21   +b
```
Why not use the existing backoff helper here?

  ↳ [id: p0o9i8] claude · 2026-09-03T13:52:10Z · R1
  Good catch — switched to `backoff.retry()` in 1a2b3c4.

#### [id: r4ng3x] user · new:20-24 → HEAD src/fetcher.py:23-27 · R1 · resolved · 0 replies · last: user · edited 2026-09-03T14:01:00Z
```diff
  11        -y = 2
       11   +y = 3
  12   12    return y
 @@ -20,4 +20,5 @@ def bar():
> 20   20    a
>      21   +b
> 21   22    c
> 22   23    d
> 23   24    e
```
\\# Not a heading
Second line\ttabbed

### (file) src/other.py

#### [id: f1l3aa] user · file · pending · unresolved · 0 replies · last: user
Please add tests for this file.

### (commit)

#### [id: c0mm1t] claude · commit · R0 · unresolved · 0 replies · last: claude
Heads-up: this commit only touches the retry path.

## Commit 1a2b3c4d5e — "Tidy up"

### README.md

#### [id: c2line] user · new:3 → HEAD README.md:4 (changed near) · R1 · unresolved · 0 replies · last: user
```diff
   1    1    # Demo
   2        -teh
        2   +the
>  3    3    end
```
typo

## All changes (combined)

### src/fetcher.py

#### [id: c0mb1n] user · old:2 · pending · unresolved · 0 replies · last: user
```
import os
```
Combined view note

## Uncommitted changes

### notes.txt

#### [id: w0rktr] user · new:1 → HEAD notes.txt:1 (live) · pending · unresolved · 0 replies · last: user
```diff
>       1   +note one
        2   +note two
```
Uncommitted note

## Outdated (anchored to commits no longer in the range)

#### [id: outd8d] user · 1b2c3d4e5f src/x.py new:10 → HEAD src/x.py:10 · R1 · unresolved · 0 replies · last: user
```
old text
```
Old anchor
"""


# --------------------------------------------------------------------------- clean()

def test_clean_strips_controls_but_keeps_tab_and_newline():
    assert render.clean("a\x00b\x07c\x1fd\x7fe\x85f") == "abcdef"
    assert render.clean("tab\there\nnext") == "tab\there\nnext"
    assert render.clean("cr\r\nlf") == "cr\nlf"


def test_clean_strips_separators_and_bidi_controls():
    assert render.clean("x\u2028y\u2029z") == "xyz"
    assert render.clean("\u202aa\u202bb\u202cc\u202dd\u202ee") == "abcde"
    assert render.clean("\u2066a\u2067b\u2068c\u2069d") == "abcd"
    assert render.clean("caf\u00e9 \u2192 ok") == "caf\u00e9 \u2192 ok"


def test_clean_single_line_folds_newlines():
    assert render.clean("one\ntwo\r\nthree\rfour", True) == "one\u2424two\u2424three\u2424four"
    assert render.clean(None) == ""
    assert render.clean(12, True) == "12"


# --------------------------------------------------------------------------- golden Markdown

def test_render_comments_golden():
    assert render.render_comments(REVIEW, COMMENTS, fetch) == GOLDEN


def test_render_is_deterministic_for_any_input_order():
    shuffled = list(COMMENTS)
    random.Random(7).shuffle(shuffled)
    assert render.render_comments(REVIEW, shuffled, fetch) == GOLDEN
    assert render.render_comments(REVIEW, list(reversed(COMMENTS)), fetch) == GOLDEN


def test_body_lines_starting_with_hash_are_escaped_in_replies_too():
    comments = [
        _comment("r00t01", "user", "root", "2026-09-03T13:45:00Z", _anchor("commit", C1), state="pending", round=None),
        _comment("repl01", "claude", "## fixed\n#tag\nplain", "2026-09-03T13:46:00Z", _anchor("commit", C1),
                 parent_id="r00t01", round=0),
    ]
    text = render.render_comments(REVIEW, comments, fetch)
    assert "\n  ↳ [id: repl01] claude · 2026-09-03T13:46:00Z · R0\n  \\## fixed\n  \\#tag\n  plain\n" in text
    assert "\n## fixed" not in text and "\n#tag" not in text


def test_no_snippets_and_context_width():
    text = render.render_comments(REVIEW, COMMENTS, fetch, snippets=False)
    assert "```" not in text
    text = render.render_comments(REVIEW, COMMENTS, fetch, context=1)
    block = text.split("#### [id: k3f9a2]")[1].split("Why not")[0]
    assert block.split("\n")[1:] == [
        "```diff",
        "  11        -y = 2",
        ">      11   +y = 3",
        "  12   12    return y",
        "```",
        "",
    ]


def test_snippet_columns_widen_for_large_line_numbers():
    rows = [("ctx", 998, 1001, "p"), ("del", 999, None, "q"), ("add", None, 1002, "r"), ("ctx", 1000, 1003, "s")]
    file_diff = {"path": "big.txt", "hunks": [_hunk(998, 3, 1001, 3, "", rows)]}
    root = _comment("bigl1n", "user", "big", "2026-09-03T13:45:00Z",
                    _anchor("line", C1, "big.txt", "new", 1002), state="pending", round=None)
    text = render.render_comments(REVIEW, [root], lambda c, p: file_diff)
    lines = text.split("```diff\n")[1].split("```")[0].split("\n")[:-1]
    assert lines == [
        "  998  1001    p",
        "  999         -q",
        ">      1002   +r",
        " 1000  1003    s",
    ]
    assert [line[14:] for line in lines] == [" p", "-q", "+r", " s"], "marker and text columns stay aligned"


def test_snippet_falls_back_to_stored_snippet_and_memoises_fetches():
    calls = []

    def counting_fetch(commit, path):
        calls.append((commit, path))
        return None

    text = render.render_comments(REVIEW, COMMENTS, counting_fetch)
    assert "```\ny = 3\n```\nWhy not use" in text
    assert "```\na\nb\nc\nd\ne\n```\n\\# Not a heading" in text
    assert len(calls) == len(set(calls))
    assert (OLD, "src/x.py") not in calls, "outdated anchors are never fetched"
    assert (C1, "src/other.py") not in calls, "file anchors have no snippet"


def test_rounds_block_without_rounds_and_without_summary_id():
    review = dict(REVIEW, rounds=[])
    text = render.render_comments(review, [], fetch)
    assert text.startswith("# Review comments — demo (main..HEAD) — 0 threads (0 pending, 0 unresolved, 0 unanswered)\n\n"
                           "## Rounds\n- none yet\n")
    review = dict(REVIEW, rounds=[dict(REVIEW["rounds"][0], summary="", comment_ids=["c2line"])])
    text = render.render_comments(review, COMMENTS, fetch)
    assert "- Round 1 · request_changes · 2026-09-03T13:50:12Z · 1 comment\n" in text


def test_head_arrow_variants():
    def arrow(status, path="src/f.py", line=7, start_line=None):
        location = {"path": path, "line": line, "status": status}
        if status == "file-deleted":
            location = {"path": None, "line": None, "status": status}
        root = _comment("arrw01", "user", "b", "2026-09-03T13:45:00Z",
                        _anchor("line", C1, "src/f.py", "new", 9, start_line), head_location=location)
        header = render.render_comments(REVIEW, [root], None, snippets=False).split("#### ")[1].split("\n")[0]
        return header.split(" · ")[1]

    assert arrow("same") == "new:9 → HEAD src/f.py:7"
    assert arrow("moved") == "new:9 → HEAD src/f.py:7 (moved)"
    assert arrow("changed") == "new:9 → HEAD src/f.py:7 (changed near)"
    assert arrow("deleted") == "new:9 → HEAD src/f.py:7 (deleted)"
    assert arrow("live") == "new:9 → HEAD src/f.py:7 (live)"
    assert arrow("file-deleted") == "new:9 → HEAD src/f.py (file deleted)"
    assert arrow("unknown") == "new:9"
    assert arrow("same", start_line=5) == "new:5-9 → HEAD src/f.py:3-7"


def test_control_characters_in_paths_and_authors_are_cleaned():
    root = _comment("ctrl01", "us\x1ber", "b", "2026-09-03T13:45:00Z",
                    _anchor("line", C1, "src/we\x08ird\nname.py", "new", 3), state="pending", round=None)
    text = render.render_comments(REVIEW, [root], None, snippets=False)
    assert "### src/weird\u2424name.py\n" in text
    assert "#### [id: ctrl01] user · new:3 · pending" in text


# --------------------------------------------------------------------------- filters and marks

def _selected(**filters):
    threads = render.sort_threads(render.build_threads(COMMENTS), REVIEW)
    selected, matching = render.select_threads(threads, **filters)
    return [t["root"]["id"] for t in selected], matching


def test_select_threads_thread_level_filters():
    assert _selected()[0] == ["q8x1zz", "k3f9a2", "r4ng3x", "f1l3aa", "c0mm1t", "c2line", "c0mb1n", "w0rktr", "outd8d"]
    ids, matching = _selected(unresolved=True)
    assert "r4ng3x" not in ids and len(ids) == 8 and matching == set()
    assert _selected(unanswered=True)[0] == ["q8x1zz", "f1l3aa", "c2line", "c0mb1n", "w0rktr"]
    assert _selected(outdated="only")[0] == ["outd8d"]
    assert "outd8d" not in _selected(outdated="exclude")[0]


def test_select_threads_comment_level_filters_mark_matching_members():
    ids, matching = _selected(author="claude")
    assert ids == ["k3f9a2", "c0mm1t"] and matching == {"p0o9i8", "c0mm1t"}
    ids, matching = _selected(state="pending")
    assert ids == ["k3f9a2", "f1l3aa", "c0mb1n", "w0rktr"] and "p0o9i8" not in matching
    ids, matching = _selected(round=1)
    assert ids == ["q8x1zz", "k3f9a2", "r4ng3x", "c2line", "outd8d"] and matching == {"q8x1zz", "p0o9i8", "r4ng3x",
                                                                                      "c2line", "outd8d"}
    assert _selected(commit="9fceb02d")[0] == ["k3f9a2", "r4ng3x", "f1l3aa", "c0mm1t"]
    assert _selected(commit="combined")[0] == ["c0mb1n"]
    assert _selected(path="README.md")[0] == ["c2line"]
    assert _selected(updated_since="2026-09-03T14:00:00Z") == (["r4ng3x"], {"r4ng3x"})
    assert _selected(author="claude", unanswered=True)[0] == []


def test_round_view_marks_new_comments_and_keeps_context():
    text = render.render_comments(REVIEW, COMMENTS, fetch, round=1, mark="★ new in round 1", snippets=False)
    assert text.startswith("# Review comments — demo (main..HEAD) — 5 threads (1 pending, 4 unresolved, 2 unanswered)\n")
    assert "#### [id: k3f9a2] user · new:11 → HEAD src/fetcher.py:14 (moved) · pending · unresolved · 1 reply · last: claude\n" in text
    assert "  ↳ [id: p0o9i8] claude · 2026-09-03T13:52:10Z · R1 · ★ new in round 1\n" in text
    assert "· edited 2026-09-03T14:01:00Z · ★ new in round 1\n" in text
    assert "f1l3aa" not in text and "w0rktr" not in text


def test_default_mark_is_star_and_explicit_threads_win():
    text = render.render_comments(REVIEW, COMMENTS, fetch, author="claude", snippets=False)
    assert "#### [id: c0mm1t] claude · commit · R0 · unresolved · 0 replies · last: claude · ★\n" in text
    threads = render.sort_threads(render.build_threads(COMMENTS), REVIEW)[:1]
    text = render.render_comments(REVIEW, COMMENTS, fetch, threads=threads, matching={"q8x1zz"})
    assert "1 thread (" in text and "#### [id: q8x1zz] user · review · R1 · unresolved · 0 replies · last: user · ★\n" in text
    with pytest.raises(TypeError):
        render.render_comments(REVIEW, COMMENTS, fetch, threads=threads, author="claude")


def test_build_threads_drops_orphan_replies_and_orders_replies():
    comments = [
        _comment("r00t01", "user", "root", "2026-09-03T13:45:00Z", _anchor("commit", C1)),
        _comment("late01", "user", "late", "2026-09-03T13:47:00Z", _anchor("commit", C1), parent_id="r00t01"),
        _comment("earl01", "claude", "early", "2026-09-03T13:46:00Z", _anchor("commit", C1), parent_id="r00t01"),
        _comment("orph01", "claude", "orphan", "2026-09-03T13:46:00Z", _anchor("commit", C1), parent_id="gone00"),
    ]
    threads = render.build_threads(comments)
    assert len(threads) == 1
    assert [r["id"] for r in threads[0]["replies"]] == ["earl01", "late01"]
    assert threads[0]["last_author"] == "user" and threads[0]["answered"] is False


# --------------------------------------------------------------------------- export and JSON

def test_render_export_title_rounds_and_outdated():
    text = render.render_export(REVIEW, COMMENTS, fetch, exported_at="2026-09-03T14:05:00Z")
    lines = text.split("\n")
    assert lines[0] == "# Review — demo (main..HEAD, base aaaaaaaaaa → head 1a2b3c4d5e) — exported 2026-09-03T14:05:00Z"
    assert lines[1] == "" and lines[2] == "## Rounds"
    assert "## Outdated (anchored to commits no longer in the range)" in text
    assert text.endswith(GOLDEN.split("## Rounds\n", 1)[1])
    default = render.render_export(REVIEW, COMMENTS, fetch)
    assert default.split(" — exported ")[1].split("\n")[0].endswith("Z")
    root_review = dict(REVIEW, range=dict(REVIEW["range"], base=None, spec="%s..HEAD" % ("f" * 40)))
    assert render.render_export(root_review, [], fetch, exported_at="x").startswith(
        "# Review — demo (ffffffffff..HEAD, base root → head 1a2b3c4d5e) — exported x")


def test_to_json_shape():
    doc = render.to_json(REVIEW, COMMENTS)
    assert set(doc) == {"review", "rounds", "comments", "threads"}
    assert all("files" not in c for c in doc["review"]["commits"])
    assert [c["sha"] for c in doc["review"]["commits"]] == ["combined", C1, C2, "worktree"]
    assert "files" in REVIEW["commits"][0], "the input review is not mutated"
    assert doc["rounds"] == REVIEW["rounds"] and doc["comments"] == COMMENTS
    by_root = {t["root"]: t for t in doc["threads"]}
    assert [t["root"] for t in doc["threads"]] == ["q8x1zz", "k3f9a2", "r4ng3x", "f1l3aa", "c0mm1t", "c2line",
                                                   "c0mb1n", "w0rktr", "outd8d"]
    assert by_root["k3f9a2"] == {"root": "k3f9a2", "replies": ["p0o9i8"], "last_author": "claude", "answered": True}
    assert by_root["c2line"] == {"root": "c2line", "replies": [], "last_author": "user", "answered": False}


def test_round_without_verdict_omits_the_verdict_word():
    review = {"repo": {"name": "r"}, "range": {"spec": "main..x", "base": None, "head": "a" * 40},
              "commits": [], "rounds": [{"number": 1, "submitted_at": "2026-09-07T13:51:00Z", "verdict": "comment",
                                         "summary": "", "comment_ids": ["k3f9a2"]}]}
    text = render.render_comments(review, [], None)
    assert "- Round 1 · 2026-09-07T13:51:00Z · 1 comment\n" in text and "comment ·" not in text.split("\n")[3]


# --------------------------------------------------------------------------- the review banner

def _with_review(info):
    return dict(REVIEW, review=info, counts=dict(REVIEW["counts"], total=12), rounds=[{"number": 1}, {"number": 2}])


def test_review_line_says_nothing_about_a_plain_first_review():
    assert render.review_line(_with_review({"id": 1, "started_at": "2026-09-09T10:00:00Z",
                                            "resumed": False, "previous": None})) is None
    assert render.review_line(REVIEW) is None


def test_review_line_states_a_resumed_review():
    line = render.review_line(_with_review({"id": 3, "started_at": "2026-09-09T10:00:00Z",
                                            "resumed": True, "previous": None}))
    assert line == "ccr: resuming review #3 started 2026-09-09T10:00:00Z (12 comments, 2 rounds)"


def test_review_line_names_the_unrelated_review_left_in_the_database():
    line = render.review_line(_with_review({"id": 4, "started_at": "2026-09-17T09:00:00Z", "resumed": False,
                                            "previous": {"id": 3, "started_at": "2026-09-09T10:00:00Z",
                                                         "range": "main..other", "comments": 24, "rounds": 1}}))
    assert line == ("ccr: new review #4 — the database also holds review #3 "
                    "(main..other, 24 comments, 1 round) of a different change")


# --------------------------------------------------------------------------- PR mode (section 10)

PR = {"url": "https://github.com/o/r/pull/7", "host": "github.com", "owner": "o", "repo": "r", "number": 7}


def test_pr_mode_names_the_pull_request_and_what_every_root_is_for():
    review = dict(REVIEW, pr=PR)
    github = _comment("gh0001", "user", "Please add a test.", "2026-09-03T13:45:30Z",
                      _anchor("line", C1, "src/fetcher.py", "new", 11), snippet="y = 3")
    github["github"] = {"status": "local"}
    posted = _comment("gh0002", "user", "Typo.", "2026-09-03T13:48:30Z", _anchor("line", C2, "README.md", "new", 3),
                      snippet="end")
    posted["github"] = {"status": "posted", "url": "https://github.com/o/r/pull/7#discussion_r9"}
    text = render.render_comments(review, COMMENTS + [github, posted], fetch)
    assert text.startswith("# Review comments — demo (main..HEAD) — PR o/r#7 — 11 threads (")
    assert "#### [id: gh0001] user · GitHub comment (not posted) · new:11 → " not in text, "no head location given"
    assert "#### [id: gh0001] user · GitHub comment (not posted) · new:11 · R1 · unresolved" in text
    assert ("#### [id: gh0002] user · GitHub comment (posted: https://github.com/o/r/pull/7#discussion_r9) · new:3 · R1"
            in text)
    assert "#### [id: k3f9a2] user · question · new:11 → HEAD src/fetcher.py:14 (moved) · pending" in text
    assert "#### [id: q8x1zz] user · question · review · R1" in text
    assert "#### [id: c0mm1t] claude · commit · R0" in text, "Claude's own roots are not questions"
    assert "  ↳ [id: p0o9i8] claude · 2026-09-03T13:52:10Z · R1" in text, "replies carry no intent"
    export = render.render_export(review, COMMENTS, fetch, exported_at="2026-09-03T15:00:00Z")
    assert export.startswith("# Review — demo (main..HEAD, base aaaaaaaaaa → head 1a2b3c4d5e) — PR o/r#7 — exported ")
    assert render.pr_line(review) == ("ccr: pr https://github.com/o/r/pull/7 (o/r#7): questions for Claude, GitHub "
                                      "comments for your pending review")
    assert render.pr_line(REVIEW) is None and render.render_comments(REVIEW, COMMENTS, fetch) == GOLDEN


def test_mirrored_github_threads_and_github_replies():
    review = dict(REVIEW, pr=PR)
    root = _comment("gt0001", "github", "Why 500?", "2026-09-01T09:00:00Z", _anchor("file", "combined", "src/fetcher.py"),
                    round=None)
    root["github"] = {"status": "remote", "login": "nyh", "own": False, "state": "SUBMITTED", "thread_id": "T1",
                      "outdated": True, "original_line": 12, "side": "RIGHT", "placement": "file",
                      "resolved_on_github": True}
    theirs = _comment("gt0002", "github", "Because.", "2026-09-01T10:00:00Z", root["anchor"], round=None,
                      parent_id="gt0001")
    theirs["github"] = {"status": "remote", "login": "ScyllaPiotr", "own": True, "state": "PENDING"}
    mine = _comment("gt0003", "user", "Agreed.", "2026-09-01T11:00:00Z", root["anchor"], parent_id="gt0001")
    mine["github"] = {"status": "local"}
    body = _comment("gt0004", "github", "Please fix.", "2026-09-01T12:00:00Z", _anchor("review"), round=None)
    body["github"] = {"status": "remote", "kind": "review", "review_state": "CHANGES_REQUESTED", "login": "nyh"}
    text = render.render_comments(review, [root, theirs, mine, body], fetch)
    assert ("#### [id: gt0001] @nyh (GitHub) · GitHub thread (outdated, was new:12, shown on the file, resolved there)"
            " · file · on GitHub · unresolved · 2 replies · last: user") in text
    assert "  ↳ [id: gt0002] @ScyllaPiotr (GitHub, you) · on GitHub (pending) · 2026-09-01T10:00:00Z · on GitHub" in text
    assert "  ↳ [id: gt0003] user · GitHub reply (not posted) · 2026-09-01T11:00:00Z · R1" in text
    assert "#### [id: gt0004] @nyh (GitHub) · GitHub review (changes requested) · review · on GitHub" in text

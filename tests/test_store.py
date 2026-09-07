"""Tests for ccr.store (SPEC.md section 4) against the section-9 fixture repository.

Every store is opened on the fixture repo with the ``main..feature`` range; re-anchoring tests
amend and rebase commits in the (per-test, disposable) repository and call ``load()`` again.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import threading
import time

import pytest

from ccr import gitx
from ccr.gitx import GitError
from ccr.store import (
    COMBINED,
    WORKTREE,
    NotFoundError,
    ReviewStore,
    StoreError,
    default_db_path,
    utcnow,
)
from conftest import FEATURE_SUBJECTS, build_fixture_repo

THREE_HUNKS, RENAME, BINARY, EDIT_NONL, MERGE, EMPTY, BIG = FEATURE_SUBJECTS
INSERTED = "inserted_a = 'a'\ninserted_b = 'b'\ninserted_c = 'c'"


def line_anchor(commit, path, line, side="new", start_line=None):
    return {"kind": "line", "commit": commit, "path": path, "side": side, "line": line, "start_line": start_line}


def open_store(repo, spec="main..feature", n=None, worktree=True, first_parent=False, db_path=":memory:", **kw):
    store = ReviewStore(repo.path, spec, n, worktree=worktree, first_parent=first_parent, db_path=db_path, **kw)
    store.load()
    return store


@pytest.fixture
def store(fixture_repo):
    s = open_store(fixture_repo)
    yield s
    s.close()


def unstage_all(repo):
    """Drop the fixture's staged change so test commits contain only what they add."""
    repo.git("reset", "-q")


# --------------------------------------------------------------------------- review / state shapes

def test_review_shape_and_pseudo_commits(fixture_repo, store):
    store.set_server_info({"pid": 4242, "port": 7777, "started_at": "2026-09-03T13:00:00Z", "version": "0.1.0"})
    review = store.review()
    assert set(review) == {"repo", "range", "options", "cover", "commits", "version", "generation", "loading", "now",
                           "counts", "rounds", "server", "ui"}
    assert review["repo"] == {"path": fixture_repo.path, "name": os.path.basename(fixture_repo.path),
                              "branch": "feature", "bare": False}
    assert review["range"] == {"spec": "main..feature", "given": "main..feature", "base": fixture_repo.main,
                               "head": fixture_repo.feature, "note": None, "first_parent": False}
    assert review["options"] == {"worktree": True}
    shas = [c["sha"] for c in review["commits"]]
    assert shas == [COMBINED] + fixture_repo.feature_chain + [WORKTREE]
    assert review["version"] == 1 and review["generation"] == 1 and review["loading"] is False
    assert review["counts"] == {"pending": 0, "submitted": 0, "unresolved": 0, "total": 0, "outdated": 0}
    assert review["rounds"] == []
    assert review["server"] == {"pid": 4242, "port": 7777, "started_at": "2026-09-03T13:00:00Z", "version": "0.1.0"}
    assert review["ui"] == {"connected": False, "last_seen": None, "open_polls": 0}
    assert review["now"].endswith("Z") and len(review["now"]) == 20

    combined, worktree = review["commits"][0], review["commits"][-1]
    for meta, subject in ((combined, "All changes"), (worktree, "Uncommitted changes")):
        assert meta["kind"] == meta["sha"] == meta["short_sha"]
        assert meta["subject"] == subject and meta["body"] == ""
        assert meta["parents"] == [] and meta["is_merge"] is False and meta["shallow_boundary"] is False
        assert meta["author"] == {"name": "", "email": ""}
        assert meta["author_date"] is None and meta["commit_date"] is None
        assert meta["comment_count"] == 0
        assert meta["stats"]["files"] == len(meta["files"])
        assert all(set(f) == {"path", "old_path", "status", "score", "additions", "deletions", "binary", "old_mode",
                              "new_mode", "old_blob", "new_blob"} for f in meta["files"])
    assert {f["path"] for f in worktree["files"]} == {"README.md", "src/utils.py", "untracked.txt", "untracked.bin",
                                                       "untracked_link"}
    assert "big/generated.txt" in {f["path"] for f in combined["files"]}

    real = review["commits"][1]
    assert real["kind"] == "commit" and real["subject"] == THREE_HUNKS and len(real["short_sha"]) == 10
    assert real["stats"] == {"files": 1, "additions": 4, "deletions": 3}
    assert real["files"][0]["path"] == "src/app.py" and real["comment_count"] == 0
    merge = next(c for c in review["commits"] if c["subject"] == MERGE)
    assert merge["is_merge"] and merge["parents"] == [fixture_repo.sha(EDIT_NONL), fixture_repo.main]
    empty = next(c for c in review["commits"] if c["subject"] == EMPTY)
    assert empty["files"] == [] and empty["stats"] == {"files": 0, "additions": 0, "deletions": 0}


def test_state_shape_and_ui_tracking(store):
    state = store.state()
    assert set(state) == {"version", "generation", "loading", "now", "server", "counts", "rounds", "last_round",
                          "commits", "ui"}
    assert state["rounds"] == 0 and state["last_round"] is None and state["commits"] == 7
    assert state["server"]["pid"] == os.getpid() and state["server"]["version"]
    store.touch_ui()
    ui = store.state()["ui"]
    assert ui["connected"] is True and ui["last_seen"] == store.review()["ui"]["last_seen"]
    assert ui["last_seen"] <= utcnow()


def test_first_parent_and_depth_options(fixture_repo):
    full = open_store(fixture_repo, "%s..feature" % fixture_repo.branch_point, worktree=False)
    assert full.state()["commits"] == 8
    first = open_store(fixture_repo, "%s..feature" % fixture_repo.branch_point, worktree=False, first_parent=True)
    assert first.state()["commits"] == 7 and first.review()["range"]["first_parent"] is True
    depth = open_store(fixture_repo, None, n=2, worktree=False)
    assert [c["subject"] for c in depth.review()["commits"][1:]] == [EMPTY, BIG]
    assert depth.review()["range"]["given"] == "-n 2"
    assert depth.review()["range"]["spec"] == "%s..HEAD" % fixture_repo.merge
    for s in (full, first, depth):
        s.close()


def test_empty_range_is_an_error_unless_worktree(fixture_repo):
    empty = ReviewStore(fixture_repo.path, "feature..feature", None, worktree=False, first_parent=False, db_path=":memory:")
    with pytest.raises(GitError, match="is empty"):
        empty.load()
    assert empty.review()["commits"] == [] and empty.generation == 0
    with_worktree = open_store(fixture_repo, "feature..feature")
    assert [c["sha"] for c in with_worktree.review()["commits"]] == [COMBINED, WORKTREE]
    assert with_worktree.commit_diff(COMBINED)["files"] == []


# --------------------------------------------------------------------------- diffs and files

def test_commit_diff_resolution_and_trimming(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    diff = store.commit_diff(sha)
    assert diff["sha"] == sha and diff["kind"] == "commit" and diff["comment_count"] == 0
    assert store.commit_diff(sha[:7])["sha"] == sha
    assert [f["path"] for f in diff["files"]] == ["src/app.py"]
    assert [len(h["lines"]) for h in diff["files"][0]["hunks"]] == [8, 8, 9]
    assert (diff["files"][0]["old_rev"], diff["files"][0]["new_rev"]) == (fixture_repo.branch_point, sha)

    combined = store.commit_diff(COMBINED)
    assert combined["kind"] == COMBINED and combined["subject"] == "All changes"
    big = next(f for f in combined["files"] if f["path"] == "big/generated.txt")
    assert big["too_large"] is True and big["reason"] == "file" and big["hunks"] == [] and big["line_count"] == 6000
    big_full = next(f for f in store.commit_diff(COMBINED, full=True)["files"] if f["path"] == "big/generated.txt")
    assert big_full["too_large"] is False and len(big_full["hunks"][0]["lines"]) == 6000
    assert big_full["old_rev"] == fixture_repo.main and big_full["new_rev"] == fixture_repo.feature

    config = next(f for f in store.commit_diff(fixture_repo.big, ws_ignore=True)["files"] if f["path"] == "data/config.ini")
    assert config["ws_only"] is True and config["hunks"] == []
    config_plain = next(f for f in store.commit_diff(fixture_repo.big)["files"] if f["path"] == "data/config.ini")
    assert config_plain["ws_only"] is False and config_plain["hunks"]

    worktree = store.commit_diff(WORKTREE)
    assert worktree["kind"] == WORKTREE and all(f["new_rev"] == WORKTREE for f in worktree["files"])
    assert all(f["old_rev"] == fixture_repo.feature for f in worktree["files"])

    for bad in ("0" * 40, "deadbeef", fixture_repo.main, "nonsense"):
        with pytest.raises(NotFoundError) as info:
            store.commit_diff(bad)
        assert info.value.status == 404 and isinstance(info.value, KeyError) and isinstance(info.value, StoreError)
    with pytest.raises(StoreError) as info:
        store.commit_diff("compare:%s..%s" % (sha[:10], sha[:10]))
    assert info.value.status == 400


def test_commit_diff_is_cached_per_view(fixture_repo, store, monkeypatch):
    calls = []
    real = gitx.diff_commit

    def counting(repo, sha, parent, ws_ignore=False):
        calls.append((sha, ws_ignore))
        return real(repo, sha, parent, ws_ignore)

    monkeypatch.setattr(gitx, "diff_commit", counting)
    sha = fixture_repo.sha(THREE_HUNKS)
    store.commit_diff(sha)
    store.commit_diff(sha, full=True)
    store.file_diff(sha, "src/app.py")
    store.commit_diff(sha, ws_ignore=True)
    assert calls == [(sha, False), (sha, True)]
    store.load()
    store.commit_diff(sha)
    assert calls == [(sha, False), (sha, True), (sha, False)], "reload clears the diff cache"


def test_file_diff_is_untrimmed_and_follows_old_path(fixture_repo, store):
    big = store.file_diff(fixture_repo.big, "big/generated.txt")
    assert big["too_large"] is False and len(big["hunks"][0]["lines"]) == 6000
    renamed = store.file_diff(fixture_repo.sha(RENAME), "src/util.py")
    assert renamed["path"] == "src/utils.py" and renamed["old_path"] == "src/util.py" and renamed["status"] == "R"
    assert store.file_diff(fixture_repo.sha(RENAME)[:8], "src/utils.py") is renamed
    with pytest.raises(NotFoundError):
        store.file_diff(fixture_repo.sha(RENAME), "nope.py")
    with pytest.raises(NotFoundError):
        store.file_diff("0" * 40, "src/utils.py")


def test_compare_view(fixture_repo, store):
    base, head = fixture_repo.sha(THREE_HUNKS), fixture_repo.sha(BINARY)
    diff = store.compare(base, head)
    assert diff["sha"] == diff["short_sha"] == "compare:%s..%s" % (base[:10], head[:10])
    assert diff["kind"] == "compare" and diff["subject"] == "Compare %s..%s" % (base[:10], head[:10])
    assert diff["comment_count"] == 0
    paths = {f["path"] for f in diff["files"]}
    assert "src/utils.py" in paths and "assets/logo.png" in paths and "src/app.py" not in paths
    assert all(f["old_rev"] == base and f["new_rev"] == head for f in diff["files"])

    root_diff = store.compare(None, fixture_repo.sha(THREE_HUNKS))
    assert root_diff["sha"].startswith("compare:%s.." % gitx.empty_tree(fixture_repo.path)[:10])
    assert all(f["old_rev"] is None for f in root_diff["files"]) and len(root_diff["files"]) > 5

    since_round = store.compare(fixture_repo.merge, fixture_repo.feature)
    assert {f["path"] for f in since_round["files"]} == {"big/generated.txt", "data/config.ini"}
    assert store.compare(fixture_repo.main, fixture_repo.feature)["files"], "the range base is allowed"

    for bad_base, bad_head in ((fixture_repo.root, head), (base, fixture_repo.root), (base[:10], head), (base, "HEAD"),
                               (base, None)):
        with pytest.raises(StoreError) as info:
            store.compare(bad_base, bad_head)
        assert info.value.status == 400


def test_file_contents_and_rev_path_checks(fixture_repo, store):
    result = store.file(fixture_repo.feature, "src/app.py")
    assert set(result) == {"rev", "path", "content", "lines", "truncated_lines"}
    assert result["lines"] == 31 and "inserted_a = 'a'" in result["content"]
    assert store.file(fixture_repo.branch_point, "src/app.py")["lines"] == 30, "a listed commit's parent is allowed"
    untracked = store.file(WORKTREE, "untracked.txt")
    assert untracked["content"] == "untracked line 1\nuntracked line 2" and untracked["lines"] == 2
    assert store.file(WORKTREE, "README.md")["content"].endswith("Unstaged edit.")
    for rev, path, status in (
        (fixture_repo.root, "src/app.py", 400),          # not part of the review
        ("HEAD", "src/app.py", 400),
        (fixture_repo.feature, "Makefile", 400),           # never touched inside the review
        (fixture_repo.feature, "../etc/passwd", 400),
        (fixture_repo.feature, "src/app.py\0", 400),
        (fixture_repo.feature, "", 400),
        (fixture_repo.sha(THREE_HUNKS), "big/generated.txt", 404),   # valid rev and path, but absent there
        (fixture_repo.feature, "assets/logo.png", 415),
    ):
        with pytest.raises(StoreError) as info:
            store.file(rev, path)
        assert info.value.status == status, (rev, path)


# --------------------------------------------------------------------------- comments: create and snippets

def test_add_line_comments_capture_snippets(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    new_single = store.add_comment("changed value", line_anchor(sha, "src/app.py", 5))
    assert new_single["snippet"] == "value_05 = 500  # changed"
    assert new_single["state"] == "pending" and new_single["round"] is None and new_single["author"] == "user"
    assert new_single["resolved"] is False and new_single["parent_id"] is None and new_single["moved_from"] is None
    assert new_single["outdated"] is False and new_single["created_at"] == new_single["updated_at"]
    assert len(new_single["id"]) == 6 and new_single["id"] == new_single["id"].lower()
    assert new_single["anchor"] == line_anchor(sha, "src/app.py", 5)

    old_single = store.add_comment("old value", line_anchor(sha, "src/app.py", 5, side="old"))
    assert old_single["snippet"] == "value_05 = 5"
    new_range = store.add_comment("inserted block", line_anchor(sha, "src/app.py", 26, start_line=24))
    assert new_range["snippet"] == INSERTED and new_range["anchor"]["start_line"] == 24
    old_range = store.add_comment("deleted block", line_anchor(sha, "src/app.py", 16, side="old", start_line=15))
    assert old_range["snippet"] == "value_15 = 15\nvalue_16 = 16"

    listed = store.list_comments()
    assert [c["id"] for c in listed] == [new_single["id"], old_single["id"], new_range["id"], old_range["id"]]
    assert store.review()["counts"] == {"pending": 4, "submitted": 0, "unresolved": 4, "total": 4, "outdated": 0}
    assert store.commit_diff(sha)["comment_count"] == 4
    assert next(c for c in store.review()["commits"] if c["sha"] == sha)["comment_count"] == 4


def test_snippet_is_capped_at_32_lines(fixture_repo, store):
    comment = store.add_comment("long range", line_anchor(fixture_repo.big, "big/generated.txt", 40, start_line=1))
    lines = comment["snippet"].split("\n")
    assert len(lines) == 32 and lines[0] == "generated line 1" and lines[30] == "generated line 31" and lines[-1] == "…"


def test_expanded_context_lines_are_accepted_without_snippet(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    context = store.add_comment("outside hunks", line_anchor(sha, "src/app.py", 1))
    assert context["snippet"] == ""
    far = store.add_comment("far away", line_anchor(sha, "src/app.py", 100000))
    assert far["snippet"] == ""
    with pytest.raises(StoreError, match="anchor.line") as info:
        store.add_comment("too far", line_anchor(sha, "src/app.py", 100001))
    assert info.value.status == 400
    partial = store.add_comment("range partly outside", line_anchor(sha, "src/app.py", 26, start_line=1))
    assert partial["snippet"] == ""


def test_pseudo_commit_anchors(fixture_repo, store):
    combined = store.add_comment("all", line_anchor(COMBINED, "src/app.py", 5))
    assert combined["anchor"]["commit"] == COMBINED and combined["snippet"] == "value_05 = 500  # changed"
    untracked = store.add_comment("wt", line_anchor(WORKTREE, "untracked.txt", 2))
    assert untracked["snippet"] == "untracked line 2"
    staged = store.add_comment("staged", {"kind": "file", "commit": WORKTREE, "path": "src/utils.py"})
    assert staged["anchor"] == {"kind": "file", "commit": WORKTREE, "path": "src/utils.py", "side": None, "line": None,
                                "start_line": None}
    whole = store.add_comment("commit-level", {"kind": "commit", "commit": COMBINED})
    assert whole["anchor"]["commit"] == COMBINED and whole["snippet"] == ""
    assert store.review()["commits"][0]["comment_count"] == 2 and store.review()["commits"][-1]["comment_count"] == 2


def test_worktree_anchor_requires_the_worktree_view(fixture_repo):
    store = open_store(fixture_repo, worktree=False)
    assert [c["sha"] for c in store.review()["commits"]][-1] == fixture_repo.feature
    with pytest.raises(NotFoundError, match="worktree"):
        store.add_comment("wt", {"kind": "commit", "commit": WORKTREE})
    with pytest.raises(NotFoundError):
        store.commit_diff(WORKTREE)
    store.close()


def test_commit_ref_resolution_and_old_path_normalisation(fixture_repo, store):
    rename = fixture_repo.sha(RENAME)
    by_old_path = store.add_comment("via old path", {"kind": "file", "commit": rename[:7], "path": "src/util.py"})
    assert by_old_path["anchor"]["path"] == "src/utils.py" and by_old_path["anchor"]["commit"] == rename
    by_old_line = store.add_comment("old side", line_anchor(rename, "src/util.py", 4, side="old"))
    assert by_old_line["anchor"]["path"] == "src/utils.py" and by_old_line["snippet"] == "def add(a, b):"
    deleted = store.add_comment("gone", {"kind": "file", "commit": rename, "path": "docs/guide.txt"})
    assert deleted["anchor"]["path"] == "docs/guide.txt"

    head = store.add_comment("via HEAD", {"kind": "commit", "commit": "HEAD"})
    assert head["anchor"]["commit"] == fixture_repo.feature
    relative = store.add_comment("via rev", {"kind": "commit", "commit": "feature~2"})
    assert relative["anchor"]["commit"] == fixture_repo.merge
    branch = store.add_comment("via branch", {"kind": "commit", "commit": "feature"})
    assert branch["anchor"]["commit"] == fixture_repo.feature
    assert branch["state"] == "pending"

    for ref, status in (("main", 404), (fixture_repo.root, 404), ("0" * 40, 404), ("no-such-ref", 404),
                        ("compare:%s..%s" % (rename[:10], rename[:10]), 400), ("-x", 400), ("", 400), (7, 400)):
        with pytest.raises(StoreError) as info:
            store.add_comment("bad", {"kind": "commit", "commit": ref})
        assert info.value.status == status, ref


def test_anchor_validation_errors(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)

    def rejects(body, anchor, status=400, **kw):
        with pytest.raises(StoreError) as info:
            store.add_comment(body, anchor, **kw)
        assert info.value.status == status, (anchor, str(info.value))
        return str(info.value)

    assert "kind" in rejects("x", {"kind": "paragraph"})
    assert "anchor" in rejects("x", "not an object")
    assert "anchor" in rejects("x", None)
    rejects("x", {"kind": "review", "commit": sha})
    rejects("x", {"kind": "commit"})
    rejects("x", {"kind": "commit", "commit": sha, "path": "src/app.py"})
    assert "path" in rejects("x", {"kind": "file", "commit": sha})
    rejects("x", {"kind": "file", "commit": sha, "path": "missing.py"}, 404)
    rejects("x", {"kind": "file", "commit": sha, "path": "src/app.py", "line": 3})
    rejects("x", line_anchor(sha, "src/app.py", 5, side="left"))
    rejects("x", line_anchor(sha, "src/app.py", 0))
    rejects("x", line_anchor(sha, "src/app.py", "5"))
    rejects("x", line_anchor(sha, "src/app.py", True))
    rejects("x", line_anchor(sha, "src/app.py", None))
    rejects("x", line_anchor(sha, "src/app.py", 5, start_line=5))
    rejects("x", line_anchor(sha, "src/app.py", 5, start_line=9))
    rejects("x", line_anchor(sha, "src/app.py", 5, start_line=0))
    rejects("x", line_anchor(sha, "big/generated.txt", 5), 404)
    assert "empty" in rejects("   \n", line_anchor(sha, "src/app.py", 5))
    rejects(None, line_anchor(sha, "src/app.py", 5))
    assert "64 KiB" in rejects("x" * (64 * 1024 + 1), line_anchor(sha, "src/app.py", 5))
    assert "author" in rejects("x", line_anchor(sha, "src/app.py", 5), author="bot")
    assert store.list_comments() == [] and store.version == 1, "rejected comments leave no trace"


# --------------------------------------------------------------------------- replies

def test_replies_copy_the_root_anchor(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    root = store.add_comment("question", line_anchor(sha, "src/app.py", 26, start_line=24))
    reply = store.add_comment("answer", {"kind": "commit", "commit": COMBINED}, author="claude", parent_id=root["id"])
    assert reply["parent_id"] == root["id"] and reply["anchor"] == root["anchor"] and reply["snippet"] == INSERTED
    assert reply["state"] == "submitted" and reply["round"] == 0 and reply["author"] == "claude"
    user_reply = store.add_comment("thanks", None, parent_id=root["id"])
    assert user_reply["state"] == "pending" and user_reply["anchor"] == root["anchor"]

    with pytest.raises(StoreError, match="root") as info:
        store.add_comment("nested", None, parent_id=reply["id"])
    assert info.value.status == 400
    with pytest.raises(NotFoundError):
        store.add_comment("orphan", None, parent_id="zzzzzz")
    with pytest.raises(StoreError, match="empty"):
        store.add_comment("", None, parent_id=root["id"])
    with pytest.raises(StoreError, match="only root"):
        store.edit_comment(reply["id"], resolved=True)
    with pytest.raises(StoreError, match="cannot be moved"):
        store.edit_comment(reply["id"], anchor={"kind": "commit", "commit": sha})

    counts = store.review()["counts"]
    assert counts == {"pending": 2, "submitted": 0, "unresolved": 1, "total": 3, "outdated": 0}
    assert store.review()["commits"][1]["comment_count"] == 1, "replies do not count"


# --------------------------------------------------------------------------- edit / delete

def test_edit_body_sets_edited_and_resolve_toggles(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    root = store.add_comment("first", line_anchor(sha, "src/app.py", 5))
    assert root["updated_at"] == root["created_at"]
    same = store.edit_comment(root["id"], body="first")
    assert same["updated_at"] == same["created_at"] and store.version == 2, "an identical body is not an edit"
    edited = store.edit_comment(root["id"], body="  second  ")
    assert edited["body"] == "second" and edited["updated_at"] > edited["created_at"] and store.version == 3
    resolved = store.edit_comment(root["id"], resolved=True)
    assert resolved["resolved"] is True and store.review()["counts"]["unresolved"] == 0
    assert store.edit_comment(root["id"], resolved=False)["resolved"] is False
    with pytest.raises(StoreError, match="boolean"):
        store.edit_comment(root["id"], resolved="yes")
    with pytest.raises(StoreError, match="nothing to edit"):
        store.edit_comment(root["id"])
    with pytest.raises(StoreError, match="empty"):
        store.edit_comment(root["id"], body=" ")
    with pytest.raises(NotFoundError):
        store.edit_comment("nope00", body="x")
    with pytest.raises(NotFoundError):
        store.edit_comment(None, body="x")


def test_move_anchor_sets_moved_from_and_updates_replies(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    root = store.add_comment("misplaced", line_anchor(sha, "src/app.py", 5))
    reply = store.add_comment("reply", None, author="claude", parent_id=root["id"])
    moved = store.edit_comment(root["id"], anchor=line_anchor(sha, "src/app.py", 26, start_line=24))
    assert moved["anchor"] == line_anchor(sha, "src/app.py", 26, start_line=24)
    assert moved["snippet"] == INSERTED and moved["moved_from"] == {"commit": sha, "line": 5}
    assert moved["updated_at"] > moved["created_at"]
    reply_after = next(c for c in store.list_comments() if c["id"] == reply["id"])
    assert reply_after["anchor"] == moved["anchor"] and reply_after["snippet"] == INSERTED, "replies follow the root"

    rename = fixture_repo.sha(RENAME)
    as_commit = store.edit_comment(root["id"], anchor={"kind": "commit", "commit": rename[:8]})
    assert as_commit["anchor"] == {"kind": "commit", "commit": rename, "path": None, "side": None, "line": None,
                                   "start_line": None}
    assert as_commit["snippet"] == "" and as_commit["moved_from"] == {"commit": sha, "line": 26}
    reply_after = next(c for c in store.list_comments() if c["id"] == reply["id"])
    assert reply_after["anchor"] == as_commit["anchor"] and reply_after["snippet"] == ""
    assert store.commit_diff(rename)["comment_count"] == 1 and store.commit_diff(sha)["comment_count"] == 0

    unchanged = store.edit_comment(root["id"], anchor={"kind": "commit", "commit": rename})
    assert unchanged["moved_from"] == as_commit["moved_from"] and unchanged["updated_at"] == as_commit["updated_at"]
    with pytest.raises(StoreError):
        store.edit_comment(root["id"], anchor={"kind": "commit", "commit": "compare:abc..def"})


def test_delete_requires_cascade_for_threads(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    root = store.add_comment("root", line_anchor(sha, "src/app.py", 5))
    reply = store.add_comment("reply", None, parent_id=root["id"])
    lone = store.add_comment("lone", {"kind": "commit", "commit": sha})
    with pytest.raises(StoreError) as info:
        store.delete_comment(root["id"])
    assert info.value.status == 409 and str(info.value) == "thread has replies"
    version = store.version
    store.delete_comment(reply["id"])
    assert [c["id"] for c in store.list_comments()] == [root["id"], lone["id"]] and store.version == version + 1
    reply2 = store.add_comment("reply again", None, parent_id=root["id"])
    store.delete_comment(root["id"], cascade=True)
    assert [c["id"] for c in store.list_comments()] == [lone["id"]]
    store.delete_comment(lone["id"])
    assert store.list_comments() == []
    with pytest.raises(NotFoundError):
        store.delete_comment(reply2["id"])
    with pytest.raises(NotFoundError):
        store.delete_comment(root["id"])


# --------------------------------------------------------------------------- rounds

def test_submit_rounds(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    with pytest.raises(StoreError) as info:
        store.submit("request_changes", "")
    assert info.value.status == 400 and "nothing to submit" in str(info.value)
    with pytest.raises(StoreError):
        store.submit("comment", None)
    with pytest.raises(StoreError, match="verdict"):
        store.submit("lgtm", "x")

    approve = store.submit("approve", "")
    assert approve == {"number": 1, "submitted_at": approve["submitted_at"], "verdict": "approve", "summary": "",
                       "base": fixture_repo.main, "head": fixture_repo.feature,
                       "commit_shas": fixture_repo.feature_chain, "comment_ids": []}
    assert store.state()["rounds"] == 1 and store.state()["last_round"] == approve

    heads_up = store.add_comment("heads-up", {"kind": "commit", "commit": sha}, author="claude")
    assert heads_up["state"] == "submitted" and heads_up["round"] == 1
    first = store.add_comment("nit", line_anchor(sha, "src/app.py", 5))
    reply = store.add_comment("reply", None, parent_id=first["id"])
    second = store.add_comment("nit 2", {"kind": "file", "commit": sha, "path": "src/app.py"})
    assert store.review()["counts"]["pending"] == 3

    round2 = store.submit("request_changes", "  Two nits.  ")
    assert round2["number"] == 2 and round2["verdict"] == "request_changes" and round2["summary"] == "Two nits."
    assert len(round2["comment_ids"]) == 4
    comments = {c["id"]: c for c in store.list_comments()}
    for cid in (first["id"], reply["id"], second["id"]):
        assert comments[cid]["state"] == "submitted" and comments[cid]["round"] == 2
    summary = next(c for c in comments.values() if c["anchor"]["kind"] == "review")
    assert summary["body"] == "Two nits." and summary["author"] == "user" and summary["state"] == "submitted"
    assert summary["round"] == 2 and summary["parent_id"] is None and summary["snippet"] == ""
    assert summary["anchor"] == {"kind": "review", "commit": None, "path": None, "side": None, "line": None,
                                 "start_line": None}
    assert set(round2["comment_ids"]) == {first["id"], reply["id"], second["id"], summary["id"]}
    assert store.review()["rounds"] == [dict(approve, comment_ids=[heads_up["id"]]), round2], \
        "a claude comment created after round 1 belongs to round 1"
    assert store.review()["counts"] == {"pending": 0, "submitted": 4, "unresolved": 4, "total": 5, "outdated": 0}

    only_summary = store.submit("comment", "Just a note")
    assert only_summary["number"] == 3 and len(only_summary["comment_ids"]) == 1
    assert store.add_comment("later", {"kind": "commit", "commit": sha}, author="claude")["round"] == 3
    assert [r["number"] for r in store.review()["rounds"]] == [1, 2, 3]
    with pytest.raises(StoreError, match="64 KiB"):
        store.submit("approve", "x" * (64 * 1024 + 1))


# --------------------------------------------------------------------------- listing

def test_list_comments_filters(fixture_repo, store):
    three, rename = fixture_repo.sha(THREE_HUNKS), fixture_repo.sha(RENAME)
    a = store.add_comment("a", line_anchor(three, "src/app.py", 5))
    b = store.add_comment("b", {"kind": "file", "commit": rename, "path": "src/util.py"})
    store.submit("comment", "")
    c = store.add_comment("c", {"kind": "commit", "commit": three}, author="claude")
    d = store.add_comment("d", None, parent_id=a["id"], author="claude")
    e = store.add_comment("e", line_anchor(COMBINED, "src/app.py", 5))
    store.edit_comment(b["id"], resolved=True)

    ids = lambda **kw: [x["id"] for x in store.list_comments(**kw)]
    assert ids() == [a["id"], b["id"], c["id"], d["id"], e["id"]]
    assert ids(state="pending") == [e["id"]]
    assert ids(state="submitted") == [a["id"], b["id"], c["id"], d["id"]]
    assert ids(round=1) == [a["id"], b["id"], c["id"], d["id"]] and ids(round=0) == [] and ids(round=2) == []
    assert ids(resolved=True) == [b["id"]] and ids(resolved=False) == [a["id"], c["id"], d["id"], e["id"]]
    assert ids(author="claude") == [c["id"], d["id"]]
    assert ids(commit=three[:7]) == [a["id"], c["id"], d["id"]]
    assert ids(commit="HEAD~6") == [a["id"], c["id"], d["id"]]
    assert ids(commit=COMBINED) == [e["id"]]
    assert ids(path="src/utils.py") == [b["id"]] and ids(path="src/util.py") == []
    assert ids(author="claude", state="submitted", commit=three) == [c["id"], d["id"]]
    assert ids(outdated_only=True) == [] and ids(include_outdated=False) == ids()
    with pytest.raises(NotFoundError):
        store.list_comments(commit="main")
    assert all("head_location" not in x for x in store.list_comments())


# --------------------------------------------------------------------------- version / generation / wait

def test_version_and_generation_bumps(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    assert (store.version, store.generation) == (1, 1)
    root = store.add_comment("a", line_anchor(sha, "src/app.py", 5))
    assert store.version == 2
    store.edit_comment(root["id"], resolved=True)
    assert store.version == 3
    reply = store.add_comment("r", None, parent_id=root["id"])
    store.delete_comment(reply["id"])
    assert store.version == 5
    store.submit("approve", "")
    assert store.version == 6
    result = store.load()
    assert result == {"remapped": [], "outdated": [], "commits_added": 0, "commits_removed": 0}
    assert (store.version, store.generation) == (7, 2)
    with pytest.raises(GitError):
        store.load(spec="no-such-branch..feature")
    assert (store.version, store.generation) == (7, 2) and store.loading is False
    assert store.review()["range"]["spec"] == "main..feature" and store.state()["commits"] == 7
    assert store.review()["version"] == 7 and store.state()["generation"] == 2


def test_reload_picks_up_new_commits_with_pinned_spec(fixture_repo, store):
    unstage_all(fixture_repo)
    fixture_repo.write("new.txt", "brand new\n")
    fixture_repo.git("add", "new.txt")
    fixture_repo.git("commit", "-q", "-m", "Add new file", "--", "new.txt")
    new_head = fixture_repo.text("rev-parse", "HEAD")
    result = store.load()
    assert result["commits_added"] == 1 and result["commits_removed"] == 0
    review = store.review()
    assert review["range"]["head"] == new_head and review["range"]["spec"] == "main..feature"
    assert review["commits"][-2]["subject"] == "Add new file" and store.state()["commits"] == 8
    narrowed = store.load(n=1)
    assert narrowed["commits_added"] == 0 and narrowed["commits_removed"] == 7
    assert store.review()["range"]["given"] == "-n 1" and store.state()["commits"] == 1
    widened = store.load(spec="main..feature", worktree=False)
    assert widened["commits_added"] == 7 and store.review()["options"] == {"worktree": False}
    assert store.review()["commits"][-1]["sha"] == new_head


def test_wait_wakes_on_mutation_timeout_and_stop(fixture_repo, store):
    sha = fixture_repo.sha(THREE_HUNKS)
    version = store.version
    timer = threading.Timer(0.2, store.add_comment, ("late", line_anchor(sha, "src/app.py", 5)))
    timer.start()
    started = time.monotonic()
    result = store.wait(version, 10)
    assert result["changed"] is True and result["version"] == version + 1 and time.monotonic() - started < 5
    assert result["counts"]["pending"] == 1 and "ui" in result and result["rounds"] == 0
    timer.join()

    started = time.monotonic()
    result = store.wait(store.version, 0.2)
    assert result["changed"] is False and result["version"] == store.version and 0.15 < time.monotonic() - started < 3

    assert store.wait(store.version + 5, 0.1)["changed"] is True, "a client from another incarnation returns at once"

    polls = []
    waiter = threading.Thread(target=lambda: polls.append(store.wait(store.version, 10)))
    waiter.start()
    deadline = time.monotonic() + 5
    while store.state()["ui"]["open_polls"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.state()["ui"]["open_polls"] == 1
    store.stop()
    waiter.join(5)
    assert polls and polls[0]["changed"] is True and store.stopping is True
    assert store.state()["ui"]["open_polls"] == 0


# --------------------------------------------------------------------------- HEAD locations

def test_locate_head_locations(fixture_repo, store):
    three = fixture_repo.sha(THREE_HUNKS)
    same = store.add_comment("same", line_anchor(three, "src/app.py", 5))
    deleted = store.add_comment("deleted", line_anchor(three, "src/app.py", 16, side="old", start_line=15))
    moved = store.add_comment("moved", line_anchor(three, "src/app.py", 20, side="old"))
    changed = store.add_comment("changed", line_anchor(COMBINED, "src/app.py", 5, side="old"))
    combined_new = store.add_comment("combined new", line_anchor(COMBINED, "src/app.py", 24))
    live = store.add_comment("live", line_anchor(WORKTREE, "untracked.txt", 2))
    wt_old = store.add_comment("wt old", line_anchor(WORKTREE, "README.md", 1, side="old"))
    renamed = store.add_comment("renamed", line_anchor(three, "src/app.py", 26))
    file_deleted = store.add_comment("file deleted", line_anchor(fixture_repo.sha(BINARY), "notes/nonl.txt", 1))
    reply = store.add_comment("reply", None, parent_id=same["id"])
    file_level = store.add_comment("file", {"kind": "file", "commit": three, "path": "src/app.py"})

    located = {c["id"]: c for c in store.list_comments(locate=True)}
    assert located[same["id"]]["head_location"] == {"path": "src/app.py", "line": 5, "status": "same"}
    assert located[deleted["id"]]["head_location"] == {"path": "src/app.py", "line": 12, "status": "deleted"}
    assert located[moved["id"]]["head_location"] == {"path": "src/app.py", "line": 18, "status": "moved"}
    assert located[changed["id"]]["head_location"] == {"path": "src/app.py", "line": 5, "status": "changed"}
    assert located[combined_new["id"]]["head_location"] == {"path": "src/app.py", "line": 24, "status": "same"}
    assert located[live["id"]]["head_location"] == {"path": "untracked.txt", "line": 2, "status": "live"}
    assert located[wt_old["id"]]["head_location"] == {"path": "README.md", "line": 1, "status": "same"}
    assert located[file_deleted["id"]]["head_location"] == {"path": "notes/nonl.txt", "line": 1, "status": "same"}
    assert "head_location" not in located[reply["id"]] and "head_location" not in located[file_level["id"]]

    unstage_all(fixture_repo)
    fixture_repo.git("mv", "src/app.py", "src/application.py")
    fixture_repo.git("rm", "-q", "notes/nonl.txt")
    fixture_repo.git("commit", "-q", "-m", "Rename app and drop notes", "--", "src/app.py", "src/application.py",
                     "notes/nonl.txt")
    located = {c["id"]: c for c in store.list_comments(locate=True)}
    assert located[renamed["id"]]["head_location"] == {"path": "src/application.py", "line": 26, "status": "same"}
    assert located[file_deleted["id"]]["head_location"] == {"path": None, "line": None, "status": "file-deleted"}
    assert located[live["id"]]["head_location"]["status"] == "live"
    assert located[wt_old["id"]]["head_location"] == {"path": "README.md", "line": 1, "status": "same"}


def test_locate_is_unknown_without_a_from_revision(fixture_repo):
    store = open_store(fixture_repo, None, n=100, worktree=False)
    assert store.review()["range"]["base"] is None and store.state()["commits"] == 13
    root_old = store.add_comment("root old side", line_anchor(fixture_repo.root, "src/app.py", 1, side="old"))
    combined_old = store.add_comment("combined old", line_anchor(COMBINED, "src/app.py", 1, side="old"))
    root_new = store.add_comment("root new side", line_anchor(fixture_repo.root, "src/app.py", 1))
    located = {c["id"]: c["head_location"] for c in store.list_comments(locate=True)}
    assert located[root_old["id"]] == {"path": None, "line": None, "status": "unknown"}
    assert located[combined_old["id"]] == {"path": None, "line": None, "status": "unknown"}
    assert located[root_new["id"]] == {"path": "src/app.py", "line": 1, "status": "same"}
    assert root_old["snippet"] == "" and root_new["snippet"] == "value_01 = 1"
    store.close()


# --------------------------------------------------------------------------- outdated and re-anchoring

def test_outdated_comments_after_narrowing_the_range(fixture_repo, store):
    three, rename = fixture_repo.sha(THREE_HUNKS), fixture_repo.sha(RENAME)
    root = store.add_comment("on three", line_anchor(three, "src/app.py", 5))
    reply = store.add_comment("reply", None, parent_id=root["id"], author="claude")
    kept = store.add_comment("kept", {"kind": "commit", "commit": fixture_repo.big})
    result = store.load(spec="%s..feature" % rename)
    assert result["remapped"] == [] and result["outdated"] == [root["id"]]
    assert result["commits_added"] == 1 and result["commits_removed"] == 2, "the merge now pulls the hotfix in"

    comments = {c["id"]: c for c in store.list_comments()}
    assert comments[root["id"]]["outdated"] is True and comments[reply["id"]]["outdated"] is True
    assert comments[kept["id"]]["outdated"] is False
    assert comments[root["id"]]["anchor"]["commit"] == three and comments[root["id"]]["moved_from"] is None
    assert store.review()["counts"] == {"pending": 2, "submitted": 0, "unresolved": 2, "total": 3, "outdated": 1}
    assert [c["id"] for c in store.list_comments(include_outdated=False)] == [kept["id"]]
    assert [c["id"] for c in store.list_comments(outdated_only=True)] == [root["id"], reply["id"]]
    assert [c["id"] for c in store.list_comments(commit=three[:8])] == [root["id"], reply["id"]]
    assert [c["id"] for c in store.list_comments(commit=three)] == [root["id"], reply["id"]]
    with pytest.raises(NotFoundError):
        store.commit_diff(three)
    assert all(c["sha"] != three for c in store.review()["commits"])
    located = {c["id"]: c for c in store.list_comments(locate=True)}
    assert located[root["id"]]["head_location"] == {"path": "src/app.py", "line": 5, "status": "same"}

    again = store.load(spec="main..feature")
    assert again == {"remapped": [], "outdated": [], "commits_added": 2, "commits_removed": 1}
    assert store.list_comments(outdated_only=True) == []
    assert store.review()["counts"]["outdated"] == 0


def test_reanchoring_after_amend_with_same_subject(fixture_repo):
    unstage_all(fixture_repo)
    fixture_repo.write("dup.txt", "a\n" * 5 + "unique tail\n")
    fixture_repo.write("far.txt", "z\n" * 5)
    fixture_repo.git("add", "dup.txt", "far.txt")
    fixture_repo.git("commit", "-q", "-m", "Add dup files", "--", "dup.txt", "far.txt")
    old_sha = fixture_repo.text("rev-parse", "HEAD")
    store = open_store(fixture_repo, worktree=False)
    assert store.review()["commits"][-1]["sha"] == old_sha

    on_commit = store.add_comment("commit", {"kind": "commit", "commit": old_sha})
    on_file = store.add_comment("file", {"kind": "file", "commit": old_sha, "path": "dup.txt"})
    unique = store.add_comment("unique", line_anchor(old_sha, "dup.txt", 6))
    reply = store.add_comment("reply", None, parent_id=unique["id"], author="claude")
    nearest = store.add_comment("nearest", line_anchor(old_sha, "dup.txt", 1))
    ranged = store.add_comment("range", line_anchor(old_sha, "dup.txt", 3, start_line=2))
    too_far = store.add_comment("too far", line_anchor(old_sha, "far.txt", 1))
    gone = store.add_comment("gone", line_anchor(old_sha, "dup.txt", 1, side="old"))
    elsewhere = store.add_comment("elsewhere", line_anchor(fixture_repo.sha(THREE_HUNKS), "src/app.py", 5))
    assert unique["snippet"] == "unique tail" and nearest["snippet"] == "a" and ranged["snippet"] == "a\na"
    assert too_far["snippet"] == "z" and gone["snippet"] == ""

    fixture_repo.write("dup.txt", "b\n" * 3 + "a\n" * 5 + "unique tail\n")
    fixture_repo.write("far.txt", "y\n" * 25 + "z\n" * 5)
    fixture_repo.git("add", "dup.txt", "far.txt")
    fixture_repo.git("commit", "-q", "--amend", "--no-edit")
    new_sha = fixture_repo.text("rev-parse", "HEAD")
    assert new_sha != old_sha

    result = store.load()
    assert set(result["remapped"]) == {on_commit["id"], on_file["id"], unique["id"], nearest["id"], ranged["id"]}
    assert result["outdated"] == [too_far["id"], gone["id"]]
    assert result["commits_added"] == 1 and result["commits_removed"] == 1
    comments = {c["id"]: c for c in store.list_comments()}

    assert comments[on_commit["id"]]["anchor"]["commit"] == new_sha
    assert comments[on_commit["id"]]["moved_from"] == {"commit": old_sha, "line": None}
    assert comments[on_file["id"]]["anchor"] == {"kind": "file", "commit": new_sha, "path": "dup.txt", "side": None,
                                                 "line": None, "start_line": None}
    assert comments[unique["id"]]["anchor"] == line_anchor(new_sha, "dup.txt", 9)
    assert comments[unique["id"]]["moved_from"] == {"commit": old_sha, "line": 6}
    assert comments[unique["id"]]["snippet"] == "unique tail" and comments[unique["id"]]["outdated"] is False
    assert comments[reply["id"]]["anchor"] == comments[unique["id"]]["anchor"], "replies follow their root"
    assert comments[reply["id"]]["outdated"] is False and comments[reply["id"]]["moved_from"] is None
    assert comments[nearest["id"]]["anchor"] == line_anchor(new_sha, "dup.txt", 4)
    assert comments[nearest["id"]]["moved_from"] == {"commit": old_sha, "line": 1}
    assert comments[ranged["id"]]["anchor"] == line_anchor(new_sha, "dup.txt", 5, start_line=4)
    assert comments[ranged["id"]]["moved_from"] == {"commit": old_sha, "line": 3}
    for cid in (too_far["id"], gone["id"]):
        assert comments[cid]["outdated"] is True and comments[cid]["anchor"]["commit"] == old_sha
        assert comments[cid]["moved_from"] is None
    assert comments[elsewhere["id"]]["outdated"] is False and comments[elsewhere["id"]]["moved_from"] is None
    assert store.review()["counts"]["outdated"] == 2
    assert store.commit_diff(new_sha)["comment_count"] == 5
    assert store.generation == 2

    unchanged = store.load()
    assert unchanged["remapped"] == [] and unchanged["outdated"] == [too_far["id"], gone["id"]]
    store.close()


def test_reanchoring_after_rebase(fixture_repo):
    rename = fixture_repo.sha(RENAME)
    fixture_repo.git("reset", "-q", "--hard")
    fixture_repo.git("branch", "topic", rename)
    store = open_store(fixture_repo, "main..topic", worktree=False)
    review = store.review()
    assert review["range"]["note"] and review["range"]["base"] == fixture_repo.branch_point
    old_shas = [c["sha"] for c in review["commits"][1:]]
    assert old_shas == [fixture_repo.sha(THREE_HUNKS), rename]

    on_line = store.add_comment("line", line_anchor(old_shas[0], "src/app.py", 5))
    on_range = store.add_comment("range", line_anchor(old_shas[0], "src/app.py", 26, start_line=24))
    on_commit = store.add_comment("commit", {"kind": "commit", "commit": old_shas[1]})
    on_old_path = store.add_comment("file", {"kind": "file", "commit": old_shas[1], "path": "src/util.py"})
    on_old_side = store.add_comment("old side", line_anchor(old_shas[1], "src/utils.py", 4, side="old"))
    store.submit("request_changes", "Please rebase")

    fixture_repo.git("rebase", "-q", "main", "topic")
    new_shas = fixture_repo.text("rev-list", "--reverse", "main..topic").split("\n")
    assert len(new_shas) == 2 and not set(new_shas) & set(old_shas)

    result = store.load()
    assert set(result["remapped"]) == {on_line["id"], on_range["id"], on_commit["id"], on_old_path["id"],
                                       on_old_side["id"]}
    assert result["outdated"] == [] and result["commits_added"] == 2 and result["commits_removed"] == 2
    review = store.review()
    assert review["range"]["note"] is None and review["range"]["base"] == fixture_repo.main
    assert [c["sha"] for c in review["commits"][1:]] == new_shas
    comments = {c["id"]: c for c in store.list_comments()}
    assert comments[on_line["id"]]["anchor"] == line_anchor(new_shas[0], "src/app.py", 5)
    assert comments[on_line["id"]]["moved_from"] == {"commit": old_shas[0], "line": 5}
    assert comments[on_line["id"]]["state"] == "submitted" and comments[on_line["id"]]["round"] == 1
    assert comments[on_range["id"]]["anchor"] == line_anchor(new_shas[0], "src/app.py", 26, start_line=24)
    assert comments[on_commit["id"]]["anchor"]["commit"] == new_shas[1]
    assert comments[on_old_path["id"]]["anchor"]["commit"] == new_shas[1]
    assert comments[on_old_path["id"]]["anchor"]["path"] == "src/utils.py"
    assert comments[on_old_side["id"]]["anchor"] == line_anchor(new_shas[1], "src/utils.py", 4, side="old")
    assert all(not c["outdated"] for c in comments.values())
    assert store.review()["rounds"][0]["commit_shas"] == old_shas, "rounds keep the chain they were submitted on"
    store.close()


def test_reanchoring_falls_back_to_unique_subject(fixture_repo):
    unstage_all(fixture_repo)
    fixture_repo.write("one.txt", "one\n")
    fixture_repo.git("add", "one.txt")
    fixture_repo.git("commit", "-q", "-m", "Add one", "--", "one.txt")
    one = fixture_repo.text("rev-parse", "HEAD")
    store = open_store(fixture_repo, worktree=False)
    comment = store.add_comment("on one", line_anchor(one, "one.txt", 1))

    fixture_repo.git("reset", "-q", "--soft", "HEAD~1")
    fixture_repo.git("reset", "-q", "one.txt")
    fixture_repo.write("zero.txt", "zero\n")
    fixture_repo.git("add", "zero.txt")
    fixture_repo.git("commit", "-q", "-m", "Add zero", "--", "zero.txt")
    fixture_repo.git("add", "one.txt")
    fixture_repo.git("commit", "-q", "-m", "Add one", "--", "one.txt")
    new_one = fixture_repo.text("rev-parse", "HEAD")

    result = store.load()
    assert result["remapped"] == [comment["id"]] and result["commits_added"] == 2 and result["commits_removed"] == 1
    remapped = store.list_comments()[0]
    assert remapped["anchor"] == line_anchor(new_one, "one.txt", 1) and remapped["snippet"] == "one"
    assert remapped["moved_from"] == {"commit": one, "line": 1}
    store.close()


# --------------------------------------------------------------------------- database files

def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_file_db_reopen_continues_version_and_keeps_ids_unique(fixture_repo, tmp_path):
    db = str(tmp_path / "review.sqlite")
    first = open_store(fixture_repo, db_path=db, worktree=False)
    sha = fixture_repo.sha(THREE_HUNKS)
    ids = {first.add_comment("c%d" % k, {"kind": "commit", "commit": sha})["id"] for k in range(5)}
    first.submit("comment", "round one")
    version = first.version
    assert _mode(db) == 0o600 and _mode(db + ".lock") == 0o600
    assert os.path.exists(db + "-wal"), "file databases run in WAL mode"

    with pytest.raises(StoreError) as info:
        ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=db)
    assert info.value.status == 409 and "db in use" in str(info.value)
    first.close()

    second = ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=db)
    assert second.version == version and second.generation == 0
    assert second.review()["version"] == version and second.review()["counts"]["total"] == 6
    with pytest.raises(StoreError) as info:
        second.list_comments()
    assert info.value.status == 503, "comments need the chain (outdated flags) - the server answers 503 while loading"
    second.load()
    assert second.version == version + 1 and second.generation == 1
    assert {c["id"] for c in second.list_comments() if c["parent_id"] is None} >= ids
    assert [r["number"] for r in second.review()["rounds"]] == [1]
    assert second.review()["rounds"][0]["summary"] == "round one"
    new_ids = {second.add_comment("n%d" % k, {"kind": "commit", "commit": sha})["id"] for k in range(40)}
    assert len(new_ids) == 40 and not new_ids & ids
    assert second.review()["counts"]["total"] == 46
    assert second.submit("approve", "")["number"] == 2
    second.close()

    conn = sqlite3.connect(db)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    conn.close()
    assert meta["schema_version"] == "1" and meta["repo"] == os.path.realpath(fixture_repo.path)
    assert int(meta["version"]) == version + 42


def test_default_db_path_lives_in_the_session_dir(fixture_repo, ccr_session_dir):
    expected = default_db_path(fixture_repo.path)
    assert os.path.dirname(expected) == str(ccr_session_dir) and expected.endswith(".sqlite")
    assert len(os.path.basename(expected)) == len("0123456789abcdef.sqlite")
    store = ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=None)
    assert store.db_path == expected and _mode(expected) == 0o600 and _mode(expected + ".lock") == 0o600
    store.close()
    memory = ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=":memory:")
    assert set(os.listdir(ccr_session_dir)) <= {os.path.basename(expected), os.path.basename(expected) + ".lock",
                                                os.path.basename(expected) + "-wal",
                                                os.path.basename(expected) + "-shm"}
    memory.close()


def test_db_created_for_another_repo_needs_force(fixture_repo, tmp_path):
    other = build_fixture_repo(tmp_path / "other")
    db = str(tmp_path / "shared.sqlite")
    ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=db).close()
    with pytest.raises(StoreError, match="pass --db-force to reuse") as info:
        ReviewStore(other.path, "main..feature", None, worktree=False, first_parent=False, db_path=db)
    assert fixture_repo.path in str(info.value)
    forced = ReviewStore(other.path, "main..feature", None, worktree=False, first_parent=False, db_path=db, db_force=True)
    forced.close()
    reopened = ReviewStore(other.path, "main..feature", None, worktree=False, first_parent=False, db_path=db)
    reopened.close()


def test_db_with_newer_schema_is_refused(fixture_repo, tmp_path):
    db = str(tmp_path / "future.sqlite")
    ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=db).close()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="db schema too new"):
        ReviewStore(fixture_repo.path, "main..feature", None, worktree=False, first_parent=False, db_path=db)
    fd = os.open(db + ".lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the refused store released its lock
    finally:
        os.close(fd)


def test_store_error_shapes():
    err = StoreError("boom")
    assert err.status == 400 and str(err) == "boom" and err.message == "boom"
    missing = NotFoundError("gone")
    assert missing.status == 404 and str(missing) == "gone" and isinstance(missing, KeyError)
    conflict = StoreError("thread has replies", 409)
    assert conflict.status == 409

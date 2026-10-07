"""Tests for restoring a review from its export (SPEC.md section 6.4): ccr.restore and ReviewStore.restore."""

from __future__ import annotations

import json

import pytest

from ccr import render
from ccr.restore import RestoreError, parse, parse_markdown
from ccr.store import COMBINED, StoreError
from conftest import FEATURE_SUBJECTS
from test_store import PR_URL, discussion, gh_comment, gh_thread, line_anchor, open_store, since_store

THREE_HUNKS, RENAME = FEATURE_SUBJECTS[:2]
STORED = ("id", "parent_id", "author", "body", "created_at", "updated_at", "state", "round", "resolved", "anchor",
          "snippet", "moved_from", "github")


def build_review(repo, store):
    """A review with every kind of anchor, replies, edits, a resolved thread and three rounds."""
    sha, renamed = repo.sha(THREE_HUNKS), repo.sha(RENAME)
    store.set_cover("# The change\n\nIt does things.")
    store.add_comment("Overview first.", {"kind": "review"}, author="claude")
    store.submit("approve", "")
    nit = store.add_comment("nit: 500?", line_anchor(sha, "src/app.py", 5))
    store.add_comment("Because the spec says so.", None, parent_id=nit["id"], author="claude")
    store.add_comment("# not a heading\nsecond line", line_anchor(sha, "src/app.py", 4, start_line=3))
    store.add_comment("Old side.", line_anchor(sha, "src/app.py", 5, side="old"))
    store.add_comment("The whole file.", {"kind": "file", "commit": renamed, "path": "src/utils.py"})
    store.add_comment("This commit.", {"kind": "commit", "commit": sha})
    combined = store.add_comment("Across the range.", line_anchor(COMBINED, "src/app.py", 5))
    store.submit("request_changes", "Two things.\nPlease fix.")
    store.edit_comment(combined["id"], resolved=True)
    later = store.add_comment("After round 2.", {"kind": "commit", "commit": sha}, author="claude")
    store.add_comment("Thanks.", None, parent_id=later["id"])
    store.edit_comment(nit["id"], body="nit: why 500?")
    store.submit("comment", "")
    store.add_comment("Still pending.", line_anchor(sha, "src/app.py", 6))
    return store


def export_json(store) -> dict:
    return json.loads(json.dumps(render.to_json(store.review(), store.list_comments())))


def export_md(store) -> str:
    return render.render_export(store.review(), store.list_comments(), store.file_diff, exported_at="2026-10-07T00:00:00Z")


def stored(comments, keys=STORED) -> list:
    return [{key: c[key] for key in keys} for c in comments]


def test_a_json_export_restores_the_review_exactly(fixture_repo):
    original = build_review(fixture_repo, open_store(fixture_repo))
    restored = open_store(fixture_repo)
    version, generation = restored.version, restored.generation
    report = restored.restore(parse(json.dumps(export_json(original))))
    assert report == {"source": "json", "dry_run": False, "comments": 12, "threads": 10, "rounds": 3, "outdated": 0,
                      "cover": "restored", "mirrored": {"matched": 0, "restored": 0, "left_to_sync": 0, "unmatched": []},
                      "dropped_copies": 0, "renamed": {}}
    assert stored(restored.list_comments()) == stored(original.list_comments())
    assert restored.review()["rounds"] == original.review()["rounds"]
    assert restored.review()["cover"] == original.review()["cover"]
    assert (restored.version, restored.generation) == (version + 1, generation + 1)
    assert export_md(restored) == export_md(original)


def test_a_markdown_export_restores_what_it_keeps(fixture_repo):
    original = build_review(fixture_repo, open_store(fixture_repo))
    restored = open_store(fixture_repo)
    report = restored.restore(parse(export_md(original)))
    assert (report["source"], report["comments"], report["threads"], report["rounds"]) == ("markdown", 12, 10, 3)
    assert export_md(restored) == export_md(original), "the restored review exports the same"
    keys = tuple(key for key in STORED if key != "created_at")
    for mine, theirs in zip(sorted(restored.list_comments(), key=lambda c: c["id"]),
                            sorted(original.list_comments(), key=lambda c: c["id"])):
        assert {k: mine[k] for k in keys if k != "updated_at"} == {k: theirs[k] for k in keys if k != "updated_at"}
        if mine["parent_id"]:
            assert mine["created_at"] == theirs["created_at"], "a reply keeps its time"
        assert (mine["updated_at"] > mine["created_at"]) == (theirs["updated_at"] > theirs["created_at"]), \
            "an edited comment stays edited, and only that"
    assert [{k: r[k] for k in ("number", "submitted_at", "verdict", "summary")} for r in restored.review()["rounds"]] == \
        [{k: r[k] for k in ("number", "submitted_at", "verdict", "summary")} for r in original.review()["rounds"]], \
        "a summary keeps its newline (taken from the comment submit made of it)"


def test_threads_since_your_last_review_come_back_from_either_export(rereview_repo):
    r = rereview_repo
    original = since_store(r, r.base2, r.v2)
    original.add_comment("Why the check?", line_anchor("since", "src/calc.py", 29))
    original.add_comment("Why did mul change?", line_anchor("since", "src/calc.py", 25, side="old"))
    original.add_comment("Overall?", {"kind": "commit", "commit": "since"})
    original.add_comment("The commit.", {"kind": "commit", "commit": r.v2})
    markdown = export_md(original)
    assert "## Since your last review\n" in markdown and markdown.index("## Since") < markdown.index("## Commit ")
    for text in (markdown, json.dumps(export_json(original))):
        restored = since_store(r, r.base2, r.v2)
        assert restored.restore(parse(text))["outdated"] == 0
        assert {c["body"]: (c["anchor"], c["snippet"]) for c in restored.list_comments()} == \
            {c["body"]: (c["anchor"], c["snippet"]) for c in original.list_comments()}


def test_restore_fills_only_an_empty_review_and_checks_first(fixture_repo):
    original = build_review(fixture_repo, open_store(fixture_repo))
    payload = parse(export_md(original))
    target = open_store(fixture_repo)
    dry = target.restore(payload, dry_run=True)
    assert dry["dry_run"] is True and dry["comments"] == 12 and target.list_comments() == [] and target.review()["rounds"] == []
    target.add_comment("mine", {"kind": "review"})
    with pytest.raises(StoreError, match=r"review #1 already has 1 comments and 0 rounds of its own") as info:
        target.restore(payload, dry_run=True)
    assert info.value.status == 409

    linked = open_store(fixture_repo)
    with pytest.raises(StoreError, match="the export is of pull request o/r#7; start ccr with --pr for it"):
        linked.restore(dict(payload, pr="o/r#7"))
    linked.set_pr("https://github.com/o/r/pull/8")
    with pytest.raises(StoreError, match="the export is of pull request o/r#7"):
        linked.restore(dict(payload, pr="o/r#7"))

    unknown = dict(payload, comments=[dict(c, anchor=dict(c["anchor"], commit="0123abcd"))
                                      if c["anchor"]["commit"] not in (None, COMBINED) else c for c in payload["comments"]])
    with pytest.raises(StoreError, match="commit 0123abcd of the export is not in this repository"):
        open_store(fixture_repo).restore(unknown)
    orphan = dict(payload, comments=[c for c in payload["comments"] if c["parent_id"] is not None])
    with pytest.raises(StoreError, match="which is not a thread of the export"):
        open_store(fixture_repo).restore(orphan)
    with pytest.raises(StoreError, match="the export has a comment id twice"):
        open_store(fixture_repo).restore(dict(payload, comments=payload["comments"] * 2))
    with pytest.raises(StoreError, match="the export has round 1 twice"):
        open_store(fixture_repo).restore(dict(payload, rounds=payload["rounds"] * 2))


def test_restored_ids_never_collide(fixture_repo, tmp_path):
    original = build_review(fixture_repo, open_store(fixture_repo))
    db = str(tmp_path / "two-reviews.sqlite")
    first = open_store(fixture_repo, db_path=db)
    first.restore(parse(json.dumps(export_json(original))))
    first.close()
    other = open_store(fixture_repo, spec="main~1..main", db_path=db)  # another change: a new review in the same db
    assert other.review()["review"]["id"] == 2
    report = other.restore(parse(json.dumps(export_json(original))))
    assert len(report["renamed"]) == 12 and len({c["id"] for c in other.list_comments()} & set(report["renamed"])) == 0
    replies = [c for c in other.list_comments() if c["parent_id"]]
    assert all(c["parent_id"] in report["renamed"].values() for c in replies)
    other.close()


# --------------------------------------------------------------------------- PR mode

def pr_review(repo, store):
    """A PR-mode review: a mirrored thread with Claude's answer to the user's question in it, a posted GitHub
    comment that GitHub answered, an unposted one, and a mirrored review body."""
    head, sha = repo.feature, repo.sha(THREE_HUNKS)
    store.set_pr(PR_URL)
    store.sync_github(discussion(head, [gh_thread("T1", [gh_comment("C1", "nyh", "Why 500?"),
                                                         gh_comment("C2", "radek", "Because.", reply_to="C1")])], [
        {"id": "R1", "database_id": 1, "body": "Please fix.", "url": PR_URL + "#pullrequestreview-1",
         "state": "CHANGES_REQUESTED", "submitted_at": "2026-09-01T12:00:00Z", "login": "nyh"}]))
    root = next(c for c in store.list_comments() if c["body"] == "Why 500?")
    question = store.add_comment("What does radek mean?", None, parent_id=root["id"])
    store.add_comment("The spec.", None, parent_id=root["id"], author="claude")
    store.edit_comment(root["id"], resolved=True)
    remark = store.add_comment("Why 500 here?", line_anchor(sha, "src/app.py", 5), github=True)
    store.add_comment("Not yet.", line_anchor(sha, "src/app.py", 6), github=True)
    store.submit("comment", "")
    store.record_github_post(remark["id"], {"url": PR_URL + "#discussion_r9", "comment_id": 9, "node_id": "C9",
                                            "thread_id": None})
    store.sync_github(discussion(head, pr_threads(), pr_reviews()))
    return store, question


def pr_threads():
    return [gh_thread("T1", [gh_comment("C1", "nyh", "Why 500?"), gh_comment("C2", "radek", "Because.", reply_to="C1")]),
            gh_thread("T9", [gh_comment("C9", "reviewer", "Why 500 here?", database_id=9, state="PENDING"),
                             gh_comment("C10", "nyh", "Good question.", reply_to="C9")])]


def pr_reviews():
    return [{"id": "R1", "database_id": 1, "body": "Please fix.", "url": PR_URL + "#pullrequestreview-1",
             "state": "CHANGES_REQUESTED", "submitted_at": "2026-09-01T12:00:00Z", "login": "nyh"}]


def threads_of(store) -> list:
    """Every comment as (author, body, the body of its root, resolved), in a stable order."""
    comments = store.list_comments()
    bodies = {c["id"]: c["body"] for c in comments}
    return sorted((c["author"], c["body"], bodies.get(c["parent_id"]), c["resolved"]) for c in comments)


def test_a_markdown_export_in_pr_mode_finds_its_mirrored_threads_after_a_sync(fixture_repo):
    original, question = pr_review(fixture_repo, open_store(fixture_repo))
    payload = parse(export_md(original))
    assert payload["pr"] == "o/r#7"
    restored = open_store(fixture_repo)
    restored.set_pr(PR_URL)
    restored.sync_github(discussion(fixture_repo.feature, pr_threads(), pr_reviews()))
    report = restored.restore(payload)
    assert report["mirrored"] == {"matched": 2, "restored": 0, "left_to_sync": 2, "unmatched": []}
    assert report["dropped_copies"] == 1, "the sync mirrored the posted comment it could not know yet"
    again = restored.sync_github(discussion(fixture_repo.feature, pr_threads(), pr_reviews()))
    assert (again["added"], again["removed"]) == (1, 0), "nyh's answer joins the restored comment"
    assert threads_of(restored) == threads_of(original)
    mine = next(c for c in restored.list_comments() if c["body"] == "Why 500 here?")
    assert {k: mine["github"].get(k) for k in ("status", "url", "comment_id", "thread_id", "github_state")} == {
        "status": "posted", "url": PR_URL + "#discussion_r9", "comment_id": 9, "thread_id": "T9", "github_state": "PENDING"}
    assert next(c for c in restored.list_comments() if c["id"] == question["id"])["parent_id"] == \
        next(c for c in restored.list_comments() if c["body"] == "Why 500?")["id"]


def test_a_json_export_in_pr_mode_needs_no_sync_first(fixture_repo):
    original, _ = pr_review(fixture_repo, open_store(fixture_repo))
    restored = open_store(fixture_repo)
    restored.set_pr(PR_URL)
    report = restored.restore(parse(json.dumps(export_json(original))))
    assert report["mirrored"] == {"matched": 0, "restored": 4, "left_to_sync": 0, "unmatched": []}
    assert stored(restored.list_comments()) == stored(original.list_comments())
    again = restored.sync_github(discussion(fixture_repo.feature, pr_threads(), pr_reviews()))
    assert (again["added"], again["updated"], again["removed"]) == (0, 0, 0), "the mirrored rows keep their node ids"


def test_a_markdown_mirrored_thread_gone_from_github_stays_for_its_replies(fixture_repo):
    original, _ = pr_review(fixture_repo, open_store(fixture_repo))
    restored = open_store(fixture_repo)
    restored.set_pr(PR_URL)
    restored.sync_github(discussion(fixture_repo.feature, pr_threads()[1:], ()))
    report = restored.restore(parse(export_md(original)))
    root = next(c for c in restored.list_comments() if c["body"] == "Why 500?")
    assert report["mirrored"]["unmatched"] == [root["id"]] and root["github"]["deleted"] is True
    assert [c["body"] for c in restored.list_comments() if c["parent_id"] == root["id"]] == \
        ["Because.", "What does radek mean?", "The spec."]


# --------------------------------------------------------------------------- the Markdown parser

def test_the_parser_reads_what_render_writes_and_refuses_the_rest():
    with pytest.raises(RestoreError, match="not a ccr export"):
        parse("# Review comments — repo (main..feature) — 0 threads")
    with pytest.raises(RestoreError, match="not a ccr JSON export"):
        parse("{]")
    with pytest.raises(RestoreError, match="has no review, rounds and comments"):
        parse("{}")
    text = "\n".join([
        "# Review — repo (main..feature, base 1111111111 → head 2222222222) — PR o/r#7 — exported 2026-10-07T00:00:00Z",
        "", "## Rounds", "- Round 1 · 2026-10-01T10:00:00Z · 1 comment", "",
        "## Outdated (anchored to commits no longer in the range)", "",
        "#### [id: aaaaaa] user · GitHub comment (posted: https://github.com/o/r/pull/7#discussion_r5; edited since, the "
        "update not posted yet) · 3333333333 docs/a b.md new:2-4 · R1 · unresolved · 1 reply · last: claude",
        "```", "x", "```", "Body", "", "  ↳ [id: bbbbbb] claude · 2026-10-01T11:00:00Z · R1",
        "  \\# kept", "", "  indented"])
    payload = parse_markdown(text)
    assert payload["pr"] == "o/r#7" and payload["cover"] is None
    root, reply = payload["comments"]
    assert root["anchor"] == {"kind": "line", "commit": "3333333333", "path": "docs/a b.md", "side": "new", "line": 4,
                              "start_line": 2}
    assert root["github"] == {"status": "posted", "url": "https://github.com/o/r/pull/7#discussion_r5", "comment_id": 5,
                              "edited": True}
    assert (root["body"], root["created_at"], root["state"]) == ("Body", "2026-10-01T09:59:00Z", "submitted")
    assert (reply["body"], reply["anchor"], reply["parent_id"]) == ("# kept\n\nindented", root["anchor"], "aaaaaa")

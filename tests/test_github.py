"""Tests for ccr.github (SPEC.md section 10): pull request references and the posting protocol.

Every GitHub request goes to the in-memory fake of ``tests/fake_gh.py`` (``PullRequest(pr, run=fake.run)``), which
behaves like GitHub where ccr depends on it: one pending review per user, review threads only on lines of the pull
request diff, review states and counts as the GraphQL API reports them.
"""

from __future__ import annotations

import json
import os

import pytest

from ccr import github
from ccr.github import GitHubError, PullRequest, parse_pr, post_comment
from fake_gh import FakeGitHub, new_state

HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
PR = parse_pr("https://github.com/o/r/pull/7")
DIFF = {"src/app.py": {"RIGHT": [3, 4, 5, 6, 7], "LEFT": [5]}, "README.md": {"RIGHT": [1]}}
TEXTS = {"src/app.py": {"RIGHT": {str(n): "value_%02d = %d" % (n, n) for n in range(3, 8)}, "LEFT": {"5": "value_05 = 5"}}}


def line_target(line=5, side="RIGHT", start_line=None, path="src/app.py"):
    text = TEXTS.get(path, {}).get(side, {}).get(str(line), "")
    return {"commit": HEAD, "base": BASE, "path": path, "subject_type": "LINE", "line": line, "side": side,
            "start_line": start_line, "start_side": side if start_line else None,
            "lines": [{"line": line, "text": text}]}


@pytest.fixture
def fake():
    return FakeGitHub(new_state(head=HEAD, base=BASE, diff=DIFF, texts=TEXTS))


@pytest.fixture
def remote(fake):
    return PullRequest(PR, run=fake.run)


# --------------------------------------------------------------------------- references

def test_parse_pr_accepts_urls_and_short_references():
    expected = {"url": "https://github.com/scylladb/scylladb/pull/30221", "host": "github.com", "owner": "scylladb",
                "repo": "scylladb", "number": 30221}
    for text in ("https://github.com/scylladb/scylladb/pull/30221", "https://github.com/scylladb/scylladb/pull/30221/",
                 "https://github.com/scylladb/scylladb/pull/30221/files", " scylladb/scylladb#30221 ",
                 "https://github.com/scylladb/scylladb/pull/30221#discussion_r1", "http://GitHub.com/scylladb/scylladb/pull/30221"):
        assert parse_pr(text) == expected, text
    assert parse_pr("https://ghe.example.com/team/tool.py/pull/3")["host"] == "ghe.example.com"
    assert github.pr_label(expected) == "scylladb/scylladb#30221"
    for bad in ("", None, 7, "scylladb/scylladb", "https://github.com/scylladb/scylladb/issues/3",
                "https://github.com/scylladb/scylladb/pull/0", "a/b#c", "a b/c#1", "o/r#1; rm -rf /"):
        with pytest.raises(ValueError):
            parse_pr(bad)


# --------------------------------------------------------------------------- posting

def test_post_starts_the_pending_review_then_adds_to_it(fake, remote):
    first = post_comment(remote, line_target(), "Why 500?")
    assert first["created_review"] is True and first["already"] is False
    assert first["problems"] == [] and first["notes"] == []
    review = fake.pending()
    assert review is not None and review["commit"] == HEAD and review["state"] == "PENDING"
    record = first["record"]
    comment = review["comments"][0]
    assert record == {"url": comment["url"], "comment_id": comment["databaseId"], "node_id": comment["id"],
                      "thread_id": comment["thread_id"], "review_id": review["id"], "path": "src/app.py",
                      "subject_type": "LINE", "line": 5, "side": "RIGHT", "start_line": None, "start_side": None,
                      "commit": HEAD}
    assert comment["body"] == "Why 500?" and comment["line"] == 5 and comment["side"] == "RIGHT"

    second = post_comment(remote, line_target(7, start_line=3), "This range.")
    assert second["created_review"] is False and second["problems"] == []
    assert len(fake.state["reviews"]) == 1 and len(review["comments"]) == 2
    assert (review["comments"][1]["startLine"], review["comments"][1]["line"]) == (3, 7)
    assert fake.operations().count("CcrStartReview") == 1
    assert fake.operations().count("CcrViewer") == 1, "the login is asked for once"
    assert all(call["hostname"] == "github.com" and "--hostname" not in call["args"] for call in fake.state["calls"])


def test_post_into_the_users_own_pending_review_keeps_their_comments(fake, remote):
    mine = fake.add_user_review(HEAD, [("src/app.py", 4, "My own remark")])
    result = post_comment(remote, line_target(5, side="LEFT"), "Was this deleted on purpose?")
    assert result["created_review"] is False and result["problems"] == []
    assert [c["body"] for c in mine["comments"]] == ["My own remark", "Was this deleted on purpose?"]
    assert mine["comments"][1]["side"] == "LEFT" and result["record"]["review_id"] == mine["id"]
    assert "CcrStartReview" not in fake.operations()


def test_file_comments_carry_no_line_or_side(fake, remote):
    target = {"commit": HEAD, "base": BASE, "path": "README.md", "subject_type": "FILE", "line": None, "side": None,
              "start_line": None, "start_side": None}
    result = post_comment(remote, target, "Why is this file here?")
    assert result["problems"] == []
    comment = fake.pending()["comments"][0]
    assert comment["subjectType"] == "FILE" and comment["line"] is None and comment["side"] is None


def test_an_identical_comment_already_in_the_review_is_not_posted_again(fake, remote):
    posted = post_comment(remote, line_target(), "Why 500?")["record"]
    again = post_comment(remote, line_target(), "Why 500?")
    assert again["already"] is True and again["record"]["comment_id"] == posted["comment_id"]
    assert again["record"]["thread_id"] is None and len(fake.pending()["comments"]) == 1
    assert fake.operations().count("CcrAddThread") == 1
    assert post_comment(remote, line_target(6), "Why 500?")["already"] is False, "same body, another line"


def test_refuses_a_pending_review_on_another_commit(fake, remote):
    fake.add_user_review(OTHER, [("src/app.py", 4, "Started on an older push")])
    with pytest.raises(GitHubError, match="is on commit cccccccccc but ccr reviews aaaaaaaaaa"):
        post_comment(remote, line_target(), "Why 500?")
    assert "CcrAddThread" not in fake.operations()


def test_refuses_when_the_pull_request_diff_starts_elsewhere(fake, remote):
    fake.state["merge_base"] = OTHER
    with pytest.raises(GitHubError, match="diff starts at cccccccccc but ccr reviews bbbbbbbbbb..aaaaaaaaaa"):
        post_comment(remote, line_target(), "Why 500?")
    assert fake.pending() is None and fake.operations()[-1] == "compare"


def test_the_merge_base_comes_from_the_local_repository_when_it_has_the_commits(fixture_repo, fake, remote):
    base, head = fixture_repo.main, fixture_repo.feature
    fake.state["pr"].update(head=head, base=base, commits=[head])
    fake.state["merge_base"] = OTHER  # would refuse, so a pass proves GitHub was not asked
    target = dict(line_target(), commit=head, base=base)
    assert post_comment(remote, target, "Why 500?", repo=fixture_repo.path)["problems"] == []
    assert "compare" not in fake.operations()


def test_github_refusing_the_line_posts_nothing_and_leaves_no_empty_review(fake, remote):
    with pytest.raises(GitHubError, match="^Pull request review thread line must be part of the diff$"):
        post_comment(remote, line_target(9), "Not in the diff")
    assert fake.pending() is None, "the review ccr started for the refused comment is gone again"
    assert fake.operations()[-2:] == ["CcrPendingReview", "CcrDiscardReview"]

    mine = fake.add_user_review(HEAD)
    with pytest.raises(GitHubError, match="^Pull request review thread line must be part of the diff$"):
        post_comment(remote, line_target(9), "Not in the diff")
    assert fake.pending() is mine and "CcrDiscardReview" not in fake.operations()[-3:], "the user's review stays"


def test_a_failed_cleanup_is_reported_with_the_refusal(fake, remote):
    real_add = remote.add_thread

    def refuse_after_the_user_commented(review_id, target, body):
        review = fake.pending()
        review["comments"].append(fake._new_comment(review, {"path": "README.md", "line": 1, "body": "typed in the UI"}))
        return real_add(review_id, dict(target, line=9), body)

    remote.add_thread = refuse_after_the_user_commented
    with pytest.raises(GitHubError, match=r"must be part of the diff \(and the pending review ccr started for it "
                                          r"stays: it is no longer an empty pending review\)"):
        post_comment(remote, line_target(), "Why 500?")
    assert fake.pending() is not None


def test_a_moved_pull_request_head_is_a_note_not_a_refusal(fake, remote):
    fake.state["pr"].update(head=OTHER, commits=[HEAD, OTHER])
    result = post_comment(remote, line_target(), "Why 500?")
    assert result["problems"] == [] and result["notes"] == [
        "the pull request head is cccccccccc now; the comment goes on aaaaaaaaaa, the commit ccr reviewed"]
    assert fake.pending()["commit"] == HEAD


def test_the_recheck_reports_what_changed_while_posting(fake, remote):
    mine = fake.add_user_review(HEAD, [("src/app.py", 4, "My own remark")])

    def submitted_meanwhile(state):
        state["reviews"][0]["state"] = "COMMENTED"

    fake.after_add_thread = submitted_meanwhile
    result = post_comment(remote, line_target(), "Why 500?")
    assert result["record"]["review_id"] == mine["id"]
    assert result["problems"] == ["the pending review %s is gone from GitHub (submitted or deleted meanwhile?)" % mine["id"],
                                  "you have 1 submitted reviews on the pull request now, 0 before"]

    fake.after_add_thread = None
    fake.state["reviews"].clear()
    review = fake.add_user_review(HEAD, [("src/app.py", 4, "Mine")])

    def edited_meanwhile(state):
        state["reviews"][0]["comments"][0]["body"] = "Mine, reworded in the GitHub UI"

    fake.after_add_thread = edited_meanwhile
    result = post_comment(remote, line_target(), "Why 500?")
    assert result["problems"] == ["comment %d of the pending review changed meanwhile" % review["comments"][0]["databaseId"]]


def test_thread_placement_is_checked_against_the_target(fake, remote):
    real_add = remote.add_thread
    remote.add_thread = lambda review_id, target, body: dict(real_add(review_id, target, body), line=6)
    result = post_comment(remote, line_target(4), "Moved by GitHub")
    assert result["problems"] == ["GitHub put the thread at line 6, not 4"]


def test_enterprise_hosts_are_passed_to_gh(fake):
    remote = PullRequest(dict(PR, host="ghe.example.com"), run=fake.run)
    post_comment(remote, line_target(), "Why 500?")
    assert all(call["hostname"] == "ghe.example.com" for call in fake.state["calls"])


def test_an_unknown_pull_request_is_an_error(fake):
    remote = PullRequest(parse_pr("o/r#8"), run=fake.run)
    with pytest.raises(GitHubError, match="Could not resolve to a PullRequest"):
        post_comment(remote, line_target(), "Why 500?")


# --------------------------------------------------------------------------- the gh process

def test_run_gh_reports_gh_missing_and_its_errors(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(GitHubError, match=r"GitHub CLI \(gh\) is not installed"):
        github.run_gh(["api", "graphql"])
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + path)
    script = tmp_path / "gh"
    script.write_text("#!/bin/sh\necho '%s'\necho 'gh: HTTP 401' >&2\nexit 1\n"
                      % json.dumps({"errors": [{"message": "Bad credentials"}, {"message": "and more"}]}))
    script.chmod(0o755)
    with pytest.raises(GitHubError, match="^Bad credentials; and more$"):
        github.run_gh(["api", "graphql"])
    script.write_text("#!/bin/sh\necho 'gh: not logged in' >&2\nexit 4\n")
    with pytest.raises(GitHubError, match="^gh: not logged in$"):
        github.run_gh(["api", "graphql"])
    script.write_text("#!/bin/sh\ncat\n")
    assert github.run_gh(["api", "graphql", "--input", "-"], b'{"data": {}}') == b'{"data": {}}'
    assert os.access(str(script), os.X_OK)


# --------------------------------------------------------------------------- what the review of PR mode found

def test_a_commit_outside_the_pull_request_is_refused(fake, remote):
    fake.state["pr"].update(head=OTHER, commits=[OTHER])
    with pytest.raises(GitHubError, match="has head cccccccccc and aaaaaaaaaa is not one of its commits; ccr may be "
                                          "linked to the wrong pull request"):
        post_comment(remote, line_target(), "Why 500?")
    fake.state["pr"]["commits"] = ["d" * 40] * 150 + [OTHER]
    with pytest.raises(GitHubError, match="cannot tell whether aaaaaaaaaa is one of its 151 commits"):
        post_comment(remote, line_target(), "Why 500?")
    assert fake.state["reviews"] == []


def test_a_review_github_starts_on_another_commit_is_undone(fake, remote):
    fake.state["pr"]["commits"] = [OTHER, HEAD]
    fake.state["start_review_on"] = OTHER
    with pytest.raises(GitHubError, match="^GitHub started the pending review on cccccccccc instead of aaaaaaaaaa$"):
        post_comment(remote, line_target(), "Why 500?")
    assert fake.state["reviews"] == [] and "CcrAddThread" not in fake.operations()


def test_a_failed_reread_after_posting_is_a_problem_not_an_error(fake, remote):
    fake.after_add_thread = lambda state: state["fail"].update(CcrPendingReview=1)
    result = post_comment(remote, line_target(), "Why 500?")
    comment = fake.pending()["comments"][0]
    assert result["record"]["url"] == comment["url"] and result["record"]["comment_id"] == comment["databaseId"]
    assert result["problems"] == ["could not re-read the pending review to confirm the comment: "
                                  "fake gh: CcrPendingReview failed as the test asked"]


def test_a_thread_without_its_comment_is_recorded_with_the_review_address(fake, remote):
    fake.state["thread_without_comment"] = True
    result = post_comment(remote, line_target(), "Why 500?")
    review = fake.pending()
    assert result["record"]["url"] == review["url"] and result["record"]["thread_id"] == review["comments"][0]["thread_id"]
    assert result["problems"] == ["GitHub did not return the new comment; ccr keeps the review's address for it"]


def test_an_answer_without_a_thread_undoes_the_review(fake, remote):
    fake.state["no_thread"] = True
    with pytest.raises(GitHubError, match="^GitHub answered without a review thread$"):
        post_comment(remote, line_target(), "Why 500?")
    assert fake.state["reviews"] == []


def test_the_same_body_on_the_other_side_is_no_duplicate(fake, remote):
    left = post_comment(remote, line_target(5, side="LEFT"), "Why?")
    right = post_comment(remote, line_target(5, side="RIGHT"), "Why?")
    assert right["already"] is False and right["record"]["comment_id"] != left["record"]["comment_id"]
    assert [(c["side"], c["line"]) for c in fake.pending()["comments"]] == [("LEFT", 5), ("RIGHT", 5)]
    again = post_comment(remote, line_target(5, side="RIGHT"), "Why?\r\n")
    assert again["already"] is True and again["record"]["comment_id"] == right["record"]["comment_id"], \
        "line endings are not a difference"


def test_long_pending_reviews_are_read_page_by_page(fake, remote, monkeypatch):
    monkeypatch.setattr(github, "PAGE_SIZE", 2)
    mine = fake.add_user_review(HEAD, [("src/app.py", 3, "one"), ("src/app.py", 4, "two"), ("README.md", 1, "three"),
                                       ("src/app.py", 6, "four")])
    first = post_comment(remote, line_target(7), "Why 7?")
    assert first["problems"] == [] and len(mine["comments"]) == 5
    assert fake.operations().count("CcrReviewComments") == 3, "one more page of 4 comments before posting, two of 5 after"
    again = post_comment(remote, line_target(7), "Why 7?")
    assert again["already"] is True, "the duplicate is found on the last page"


# --------------------------------------------------------------------------- the pull request's discussion (10.5)

def _discussion(fake):
    nyh = fake.add_user_review(HEAD, [("src/app.py", 5, "Why 500?"), ("src/app.py", 3, "Old remark")],
                               state="CHANGES_REQUESTED", author="nyh", body="Please fix the two things.")
    nyh["comments"][1].update(outdated=True, original_line=2)
    radek = fake.add_user_review(HEAD, [], state="COMMENTED", author="radek")
    fake.add_reply(radek, nyh["comments"][0], "Because of the spec.")
    fake.add_user_review(HEAD, [("README.md", 1, "A draft of someone else")], state="PENDING", author="eve")
    mine = fake.add_user_review(HEAD, [("src/app.py", 7, "My own draft")])
    return nyh, radek, mine


def test_the_discussion_is_read_whole_and_in_ccr_shape(fake, remote, monkeypatch):
    monkeypatch.setattr(github, "PAGE_SIZE", 1)
    nyh, radek, mine = _discussion(fake)
    payload = github.fetch_discussion(remote)
    assert payload["viewer"] == "reviewer" and payload["head"] == HEAD
    assert [[(c["login"], c["body"], c["state"]) for c in t["comments"]] for t in payload["threads"]] == [
        [("nyh", "Why 500?", "SUBMITTED"), ("radek", "Because of the spec.", "SUBMITTED")],
        [("nyh", "Old remark", "SUBMITTED")],
        [("reviewer", "My own draft", "PENDING")]], "eve's pending draft is hers alone"
    first, outdated = payload["threads"][0], payload["threads"][1]
    assert (first["path"], first["line"], first["side"], first["outdated"], first["subject_type"]) == ("src/app.py", 5, "RIGHT", False, "LINE")
    assert first["comments"][1]["reply_to"] == first["comments"][0]["id"] and first["id"] == nyh["comments"][0]["thread_id"]
    assert (outdated["outdated"], outdated["line"], outdated["original_line"]) == (True, None, 2)
    assert payload["reviews"] == [{"id": nyh["id"], "database_id": nyh["databaseId"], "body": "Please fix the two things.",
                                   "url": nyh["url"], "state": "CHANGES_REQUESTED", "submitted_at": nyh["submitted_at"],
                                   "login": "nyh"}], "reviews without a body carry nothing to show"
    assert {"CcrThreadComments", "CcrThreads", "CcrReviews"} <= set(fake.operations())


def reply_target(thread_id, reply_to):
    return {"commit": HEAD, "base": BASE, "path": "src/app.py", "subject_type": "REPLY", "line": None, "side": None,
            "start_line": None, "start_side": None, "lines": [], "thread_id": thread_id, "reply_to": reply_to}


def test_a_reply_goes_into_the_thread_inside_the_pending_review(fake, remote):
    nyh, _, _ = _discussion(fake)
    fake.state["reviews"].remove(fake.pending())          # no pending review yet: the reply starts one
    root = nyh["comments"][0]
    first = post_comment(remote, reply_target(root["thread_id"], root["id"]), "Agreed, see the design.")
    review = fake.pending()
    reply = review["comments"][0]
    assert first["created_review"] is True and first["problems"] == []
    assert (reply["reply_to"], reply["body"]) == (root["id"], "Agreed, see the design.")
    assert first["record"]["thread_id"] == root["thread_id"] and first["record"]["url"] == reply["url"]
    again = post_comment(remote, reply_target(root["thread_id"], root["id"]), "Agreed, see the design.")
    assert again["already"] is True and len(review["comments"]) == 1
    other = nyh["comments"][1]
    assert post_comment(remote, reply_target(other["thread_id"], other["id"]), "Agreed, see the design.")["already"] is False, \
        "the same words in another thread are another reply"
    assert "CcrAddThread" not in fake.operations()


def test_a_reply_to_an_unknown_thread_posts_nothing_and_undoes_the_review(fake, remote):
    with pytest.raises(GitHubError, match="Could not resolve to a node"):
        post_comment(remote, reply_target("PRRT_gone", "PRRC_gone"), "Hello?")
    assert fake.state["reviews"] == []


def test_a_reply_lands_in_a_pending_review_on_another_commit(fake, remote):
    nyh, _, mine = _discussion(fake)
    mine["commit"] = OTHER                                   # a reply is placed by its thread, not by a commit
    root = nyh["comments"][0]
    result = post_comment(remote, reply_target(root["thread_id"], root["id"]), "Agreed.")
    assert result["problems"] == [] and mine["comments"][-1]["reply_to"] == root["id"]

"""The GitHub side of PR mode (SPEC.md section 10): the pull request link and the user's pending review.

ccr reaches GitHub only through the ``gh`` CLI (``gh api``), as whatever account ``gh auth`` holds, and only
to read the pull request and the user's own pending review, to start that review when there is none, and to
add one review thread per GitHub comment.  It never sets a review body, never submits a review and never edits
or deletes anything on GitHub - except the pending review it started a moment earlier, while that is still
empty because the comment it was started for did not get in.  The user submits the review, with its verdict
and body, in the GitHub UI.

:func:`post_comment` is the whole posting protocol: snapshot the pending review, refuse when it, the commit or
the pull request diff is not the one ccr anchored the comment in, add the thread, then re-read the review and
check that exactly the intended comment arrived and nothing else changed or became public.
"""

from __future__ import annotations

import json
import re
import subprocess

from . import gitx
from .gitx import GitError

__all__ = ["GitHubError", "parse_pr", "pr_label", "run_gh", "PullRequest", "post_comment", "update_comment",
           "fetch_discussion"]

GH_TIMEOUT = 60
DEFAULT_HOST = "github.com"
PAGE_SIZE = 100
SUBMITTED_STATES = "[COMMENTED, APPROVED, CHANGES_REQUESTED, DISMISSED]"
_COMMENT_FIELDS = "id databaseId body path line startLine subjectType diffHunk url replyTo { id }"
_THREAD_COMMENT_FIELDS = "id databaseId body url createdAt lastEditedAt state author { login } replyTo { id }"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_URL_RE = re.compile(r"^https?://([A-Za-z0-9.-]+(?::\d+)?)/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/?#]\S*)?$")
_SHORT_RE = re.compile(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$")

_VIEWER = "query CcrViewer { viewer { login } }"
_SNAPSHOT = """query CcrPendingReview($owner: String!, $name: String!, $number: Int!, $login: String!, $page: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id url headRefOid baseRefOid
      commits(last: 100) { totalCount nodes { commit { oid } } }
      pending: reviews(author: $login, states: PENDING, first: 5) {
        nodes {
          id databaseId state url commit { oid }
          comments(first: $page) { totalCount pageInfo { hasNextPage endCursor } nodes { %s } }
        }
      }
      submitted: reviews(author: $login, states: %s) { totalCount }
    }
  }
}""" % (_COMMENT_FIELDS, SUBMITTED_STATES)
_MORE_COMMENTS = """query CcrReviewComments($review: ID!, $page: Int!, $after: String!) {
  node(id: $review) {
    ... on PullRequestReview {
      comments(first: $page, after: $after) { pageInfo { hasNextPage endCursor } nodes { %s } }
    }
  }
}""" % _COMMENT_FIELDS
_THREADS = """query CcrThreads($owner: String!, $name: String!, $number: Int!, $page: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefOid
      reviewThreads(first: $page, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line startLine originalLine originalStartLine diffSide subjectType
          comments(first: $page) { pageInfo { hasNextPage endCursor } nodes { %s } }
        }
      }
    }
  }
}""" % _THREAD_COMMENT_FIELDS
_THREAD_COMMENTS = """query CcrThreadComments($thread: ID!, $page: Int!, $after: String!) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: $page, after: $after) { pageInfo { hasNextPage endCursor } nodes { %s } }
    }
  }
}""" % _THREAD_COMMENT_FIELDS
_REVIEWS = """query CcrReviews($owner: String!, $name: String!, $number: Int!, $page: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: $page, after: $after, states: %s) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId body url state submittedAt author { login } }
      }
    }
  }
}""" % SUBMITTED_STATES
_START_REVIEW = """mutation CcrStartReview($input: AddPullRequestReviewInput!) {
  addPullRequestReview(input: $input) { pullRequestReview { id databaseId state url commit { oid } } }
}"""
_DISCARD_REVIEW = """mutation CcrDiscardReview($input: DeletePullRequestReviewInput!) {
  deletePullRequestReview(input: $input) { pullRequestReview { id } }
}"""
_ADD_REPLY = """mutation CcrAddReply($input: AddPullRequestReviewThreadReplyInput!) {
  addPullRequestReviewThreadReply(input: $input) { comment { id databaseId body url replyTo { id } } }
}"""
_UPDATE_COMMENT = """mutation CcrUpdateComment($input: UpdatePullRequestReviewCommentInput!) {
  updatePullRequestReviewComment(input: $input) { pullRequestReviewComment { id databaseId body url } }
}"""
_ADD_THREAD = """mutation CcrAddThread($input: AddPullRequestReviewThreadInput!) {
  addPullRequestReviewThread(input: $input) {
    thread {
      id path line startLine diffSide startDiffSide subjectType
      comments(first: 1) { nodes { id databaseId body url } }
    }
  }
}"""


class GitHubError(Exception):
    """A GitHub request that failed, or a pending review ccr refuses to post into."""


def parse_pr(text) -> dict:
    """``{"url", "host", "owner", "repo", "number"}`` of a pull request URL or an ``OWNER/REPO#N`` reference."""
    value = text.strip() if isinstance(text, str) else ""
    match = _URL_RE.match(value)
    host = None
    if match:
        host, owner, repo, number = match.groups()
    else:
        match = _SHORT_RE.match(value)
        if not match:
            raise ValueError("not a pull request URL or OWNER/REPO#N reference: %r" % (text,))
        owner, repo, number = match.groups()
    if not (_NAME_RE.match(owner) and _NAME_RE.match(repo)) or int(number) < 1:
        raise ValueError("not a pull request URL or OWNER/REPO#N reference: %r" % (text,))
    host = (host or DEFAULT_HOST).lower()
    return {"url": "https://%s/%s/%s/pull/%d" % (host, owner, repo, int(number)), "host": host,
            "owner": owner, "repo": repo, "number": int(number)}


def pr_label(pr: dict) -> str:
    """``owner/repo#N``, the short name of a pull request link."""
    return "%s/%s#%d" % (pr["owner"], pr["repo"], pr["number"])


def run_gh(args: list, stdin=None) -> bytes:
    """Run ``gh`` with ``args`` (stdin bytes optional); its stdout, or GitHubError with what it said."""
    try:
        proc = subprocess.run(["gh"] + list(args), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=None if stdin is not None else subprocess.DEVNULL, timeout=GH_TIMEOUT, check=False)
    except FileNotFoundError:
        raise GitHubError("the GitHub CLI (gh) is not installed; PR mode reaches GitHub through it") from None
    except subprocess.TimeoutExpired:
        raise GitHubError("gh %s did not answer within %d s" % (" ".join(args[:2]), GH_TIMEOUT)) from None
    if proc.returncode != 0:
        raise GitHubError(_gh_failure(proc))
    return proc.stdout


def _gh_failure(proc) -> str:
    try:
        errors = json.loads(proc.stdout.decode("utf-8")).get("errors")
    except (ValueError, AttributeError, UnicodeDecodeError):
        errors = None
    if errors:
        return "; ".join(str(error.get("message", error)) for error in errors)
    return proc.stderr.decode("utf-8", "replace").strip() or "gh exited with code %d" % proc.returncode


class PullRequest:
    """The linked pull request as seen through ``gh api`` (``run`` is :func:`run_gh` unless a test injects one)."""

    def __init__(self, pr: dict, run=run_gh):
        self.pr = pr
        self.run = run
        self._login = None

    def _host_args(self) -> list:
        return [] if self.pr["host"] == DEFAULT_HOST else ["--hostname", self.pr["host"]]

    def graphql(self, document: str, variables=None) -> dict:
        request = json.dumps({"query": document, "variables": variables or {}}).encode("utf-8")
        raw = self.run(["api", "graphql", "--input", "-"] + self._host_args(), request)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GitHubError("gh api graphql printed something that is not JSON") from None
        if payload.get("errors"):
            raise GitHubError("; ".join(str(error.get("message", error)) for error in payload["errors"]))
        return payload.get("data") or {}

    def login(self) -> str:
        if self._login is None:
            self._login = self.graphql(_VIEWER)["viewer"]["login"]
        return self._login

    def snapshot(self) -> dict:
        """``{"pr": {id, url, head, base, commits, commit_count}, "review": the user's pending review with all its
        comments, or None, "submitted": how many reviews the user has submitted on the pull request}``."""
        data = self.graphql(_SNAPSHOT, {"owner": self.pr["owner"], "name": self.pr["repo"],
                                        "number": self.pr["number"], "login": self.login(), "page": PAGE_SIZE})
        node = (data.get("repository") or {}).get("pullRequest")
        if node is None:
            raise GitHubError("pull request %s not found (or not visible to %s)" % (pr_label(self.pr), self.login()))
        pending = node["pending"]["nodes"]
        review = None
        if pending:
            raw = pending[0]
            review = {"id": raw["id"], "database_id": raw["databaseId"], "state": raw["state"], "url": raw["url"],
                      "commit": (raw.get("commit") or {}).get("oid"), "comments": self._all_comments(raw),
                      "comment_count": raw["comments"]["totalCount"]}
        commits = node["commits"]
        return {"pr": {"id": node["id"], "url": node["url"], "head": node["headRefOid"], "base": node["baseRefOid"],
                       "commits": [c["commit"]["oid"] for c in commits["nodes"]], "commit_count": commits["totalCount"]},
                "review": review, "submitted": node["submitted"]["totalCount"]}

    def _all_comments(self, review: dict) -> list:
        connection = review["comments"]
        comments = list(connection["nodes"])
        while connection["pageInfo"]["hasNextPage"]:
            more = self.graphql(_MORE_COMMENTS, {"review": review["id"], "page": PAGE_SIZE,
                                                 "after": connection["pageInfo"]["endCursor"]})
            connection = (more.get("node") or {}).get("comments")
            if connection is None:
                raise GitHubError("the pending review %s went away while ccr was reading it" % review["id"])
            comments += connection["nodes"]
        return comments

    def merge_base(self, base: str, head: str) -> str:
        """GitHub's merge base of two commits (the base of the pull request diff), from the compare API."""
        path = "repos/%s/%s/compare/%s...%s" % (self.pr["owner"], self.pr["repo"], base, head)
        raw = self.run(["api", path, "--jq", ".merge_base_commit.sha"] + self._host_args(), None)
        return raw.decode("utf-8", "replace").strip()

    def start_review(self, pr_id: str, commit: str) -> dict:
        """Open the user's pending review on ``commit``: no body and no event, so it stays PENDING."""
        data = self.graphql(_START_REVIEW, {"input": {"pullRequestId": pr_id, "commitOID": commit}})
        raw = data["addPullRequestReview"]["pullRequestReview"]
        return {"id": raw["id"], "database_id": raw["databaseId"], "state": raw["state"], "url": raw["url"],
                "commit": (raw.get("commit") or {}).get("oid"), "comments": [], "comment_count": 0}

    def discard_review(self, review_id: str) -> None:
        """Delete a pending review ccr has just started, provided it is still the user's empty pending review."""
        review = self.snapshot()["review"]
        if review is None or review["id"] != review_id or review["comment_count"]:
            raise GitHubError("it is no longer an empty pending review")
        self.graphql(_DISCARD_REVIEW, {"input": {"pullRequestReviewId": review_id}})

    def add_reply(self, review_id: str, thread_id: str, body: str) -> dict:
        """Add a reply to a review thread, inside the pending review; returns GitHub's comment."""
        payload = {"pullRequestReviewId": review_id, "pullRequestReviewThreadId": thread_id, "body": body}
        comment = (self.graphql(_ADD_REPLY, {"input": payload}).get("addPullRequestReviewThreadReply") or {}).get("comment")
        if not comment:
            raise GitHubError("GitHub answered without the reply")
        return comment

    def update_comment(self, comment_id: str, body: str) -> dict:
        """Replace the body of a comment in the user's pending review; returns GitHub's comment."""
        payload = {"pullRequestReviewCommentId": comment_id, "body": body}
        data = self.graphql(_UPDATE_COMMENT, {"input": payload}).get("updatePullRequestReviewComment") or {}
        comment = data.get("pullRequestReviewComment")
        if not comment:
            raise GitHubError("GitHub answered without the comment")
        return comment

    def _pages(self, document: str, variables: dict, connection_of) -> list:
        """Every node of a paged connection: ``connection_of(data)`` picks the connection out of each answer."""
        nodes, after = [], None
        while True:
            connection = connection_of(self.graphql(document, dict(variables, page=PAGE_SIZE, after=after)))
            nodes += connection["nodes"]
            if not connection["pageInfo"]["hasNextPage"]:
                return nodes
            after = connection["pageInfo"]["endCursor"]

    def discussion(self) -> dict:
        """The pull request's review threads (every comment) and the bodies of its submitted reviews."""
        where = {"owner": self.pr["owner"], "name": self.pr["repo"], "number": self.pr["number"]}
        head = {}

        def threads_of(data):
            pr = (data.get("repository") or {}).get("pullRequest")
            if pr is None:
                raise GitHubError("pull request %s not found (or not visible to %s)" % (pr_label(self.pr), self.login()))
            head["oid"] = pr["headRefOid"]
            return pr["reviewThreads"]

        threads = self._pages(_THREADS, where, threads_of)
        for thread in threads:
            connection = thread["comments"]
            comments = list(connection["nodes"])
            while connection["pageInfo"]["hasNextPage"]:
                data = self.graphql(_THREAD_COMMENTS, {"thread": thread["id"], "page": PAGE_SIZE,
                                                       "after": connection["pageInfo"]["endCursor"]})
                connection = (data.get("node") or {}).get("comments")
                if connection is None:
                    raise GitHubError("review thread %s went away while ccr was reading it" % thread["id"])
                comments += connection["nodes"]
            thread["comments"] = comments
        reviews = self._pages(_REVIEWS, where, lambda data: data["repository"]["pullRequest"]["reviews"])
        return {"head": head["oid"], "threads": threads, "reviews": reviews}

    def add_thread(self, review_id: str, target: dict, body: str) -> dict:
        """Add one review thread to the pending review; returns GitHub's thread (with its first comment)."""
        payload = {"pullRequestReviewId": review_id, "path": target["path"], "body": body,
                   "subjectType": target["subject_type"]}
        if target["subject_type"] == "LINE":
            payload.update(line=target["line"], side=target["side"])
            if target.get("start_line"):
                payload.update(startLine=target["start_line"], startSide=target["start_side"])
        thread = (self.graphql(_ADD_THREAD, {"input": payload}).get("addPullRequestReviewThread") or {}).get("thread")
        if not thread:
            raise GitHubError("GitHub answered without a review thread")
        return thread


def _normal(text) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def _hunk_ends_at(hunk: str, target: dict) -> bool:
    """True when a review comment's diff hunk ends on the target's last row, on the target's side.

    A review comment does not say which side of the diff it is on, but its hunk ends with the row it is on:
    ``+`` or `` `` on the new side, ``-`` or `` `` on the old one.
    """
    rows = [line.rstrip("\r") for line in hunk.split("\n") if line and not line.startswith("\\")]
    lines = target.get("lines") or []
    if not rows or not lines:
        return False
    return rows[-1][:1] in ("+ " if target["side"] == "RIGHT" else "- ") and rows[-1][1:] == lines[-1]["text"]


def _same_place(comment: dict, target: dict) -> bool:
    """True when an existing pending-review comment sits where ``target`` would put a new one."""
    if comment.get("path") != target["path"] or (comment.get("subjectType") or "LINE") != target["subject_type"]:
        return False
    if target["subject_type"] == "FILE":
        return True
    return (comment.get("line") == target["line"]
            and (comment.get("startLine") or None) == (target.get("start_line") or None)
            and _hunk_ends_at(comment.get("diffHunk") or "", target))


def _merge_base(client: PullRequest, repo, base: str, head: str) -> str:
    """The pull request's merge base: from the local repository when it has the base commit, else from GitHub."""
    try:
        local = gitx.merge_base(repo, base, head) if repo else None
    except GitError:
        local = None
    return local or client.merge_base(base, head)


def _check_commit(client: PullRequest, pr: dict, commit: str, notes: list) -> None:
    """Refuse a commit that is not part of the pull request; a head that moved on past it is only a note."""
    if pr["head"] == commit:
        return
    short = commit[:gitx.SHORT_SHA_LEN]
    if commit not in pr["commits"]:
        reason = ("ccr cannot tell whether %s is one of its %d commits" % (short, pr["commit_count"])
                  if pr["commit_count"] > len(pr["commits"]) else "%s is not one of its commits" % short)
        raise GitHubError("pull request %s has head %s and %s; ccr may be linked to the wrong pull request, or the "
                          "pull request was rewritten - start ccr on its head"
                          % (pr_label(client.pr), pr["head"][:gitx.SHORT_SHA_LEN], reason))
    notes.append("the pull request head is %s now; the comment goes on %s, the commit ccr reviewed"
                 % (pr["head"][:gitx.SHORT_SHA_LEN], short))


def _abandon(client: PullRequest, review: dict, reason: str) -> GitHubError:
    """Delete the empty pending review ccr just started for a comment that did not get in; the error to raise."""
    try:
        client.discard_review(review["id"])
    except GitHubError as cleanup:
        return GitHubError("%s (and the pending review ccr started for it stays: %s)" % (reason, cleanup))
    return GitHubError(reason)


def post_comment(client: PullRequest, target: dict, body: str, repo=None) -> dict:
    """Post ``body`` verbatim at ``target`` into the user's pending review on the pull request.

    ``target`` is the store's ``github_target``: a new thread (commit, base, path, subject type, line, side, lines,
    ...) or, with ``subject_type`` ``REPLY``, a reply in the review thread ``thread_id`` whose first comment is
    ``reply_to``.  Returns ``{"record", "created_review", "already", "notes", "problems"}``: ``record`` is what
    ccr remembers about the posted comment; ``already`` means an identical comment was found in the pending review
    and nothing was added; ``problems`` lists what could not be confirmed once the comment was on GitHub (it is
    posted anyway).  Raises GitHubError only when nothing was posted.
    """
    reply = target["subject_type"] == "REPLY"
    before = client.snapshot()
    pr, review = before["pr"], before["review"]
    notes, problems = [], []
    _check_commit(client, pr, target["commit"], notes)
    if not reply:  # a reply is placed by its thread, not by lines of the diff or the commit of the review
        merge_base = _merge_base(client, repo, pr["base"], target["commit"])
        if merge_base != target["base"]:
            raise GitHubError("the pull request diff starts at %s but ccr reviews %s..%s; restart ccr with --range "
                              "%s..HEAD" % (merge_base[:gitx.SHORT_SHA_LEN], target["base"][:gitx.SHORT_SHA_LEN],
                                            target["commit"][:gitx.SHORT_SHA_LEN], merge_base))
        if review is not None and review["commit"] != target["commit"]:
            raise GitHubError("your pending review on %s is on commit %s but ccr reviews %s; submit or discard it on "
                              "GitHub first, or review %s in ccr" % (pr_label(client.pr), (review["commit"] or "?")[:10],
                                                                     target["commit"][:10], (review["commit"] or "?")[:10]))
    if review is not None:
        same = (lambda c: (c.get("replyTo") or {}).get("id") == target["reply_to"]) if reply \
            else (lambda c: _same_place(c, target))
        duplicate = next((c for c in review["comments"] if _normal(c["body"]) == _normal(body) and same(c)), None)
        if duplicate is not None:
            return {"record": _record(review, None, duplicate, target), "created_review": False, "already": True,
                    "notes": notes, "problems": problems}
    created = review is None
    if created:
        review = client.start_review(pr["id"], target["commit"])
        if review["commit"] != target["commit"]:
            raise _abandon(client, review, "GitHub started the pending review on %s instead of %s"
                           % ((review["commit"] or "?")[:10], target["commit"][:10]))
    try:
        if reply:
            comment = client.add_reply(review["id"], target["thread_id"], body)
            thread = {"id": target["thread_id"], "comments": {"nodes": [comment]}}
        else:
            thread = client.add_thread(review["id"], target, body)
    except GitHubError as exc:
        if created:
            raise _abandon(client, review, str(exc)) from None
        raise
    # The comment is on GitHub now: whatever cannot be confirmed from here on is a problem, never an exception.
    nodes = (thread.get("comments") or {}).get("nodes") or []
    comment = nodes[0] if nodes else {"id": None, "databaseId": None, "url": review["url"], "body": body}
    if not nodes:
        problems.append("GitHub did not return the new comment; ccr keeps the review's address for it")
    if reply:
        if (comment.get("replyTo") or {}).get("id") != target["reply_to"]:
            problems.append("GitHub filed the reply under %r, not under the thread's first comment"
                            % (comment.get("replyTo") or {}).get("id"))
    elif pr["head"] == target["commit"]:  # on an older commit GitHub may report the place on the newer diff
        expected = {"path": target["path"], "subjectType": target["subject_type"]}
        if target["subject_type"] == "LINE":
            expected.update(line=target["line"], diffSide=target["side"], startLine=target.get("start_line") or None)
        for key, value in expected.items():
            if thread.get(key) != value:
                problems.append("GitHub put the thread at %s %r, not %r" % (key, thread.get(key), value))
    try:
        problems += _recheck(client, before, review, comment, body)
    except GitHubError as exc:
        problems.append("could not re-read the pending review to confirm the comment: %s" % exc)
    return {"record": _record(review, thread, comment, target), "created_review": created, "already": False,
            "notes": notes, "problems": problems}


def update_comment(client: PullRequest, node_id: str, body: str) -> dict:
    """Put ``body`` verbatim into the comment ``node_id`` of the user's pending review, which ccr posted earlier.

    Refused, changing nothing, when the comment is not in the pending review any more (the review was submitted
    or deleted, or the comment removed there).  Returns ``{"url", "already", "problems"}``: ``already`` means the
    comment had that body already; ``problems`` lists what the re-read could not confirm (it is updated anyway).
    """
    before = client.snapshot()
    review = before["review"]
    current = next((c for c in (review or {}).get("comments", []) if c["id"] == node_id), None)
    if current is None:
        raise GitHubError("the comment is not in your pending review on %s any more (the review submitted or "
                          "deleted, or the comment removed there); change it on GitHub" % pr_label(client.pr))
    if _normal(current["body"]) == _normal(body):
        return {"url": current["url"], "already": True, "problems": []}
    comment = client.update_comment(node_id, body)
    problems = []
    try:
        after = client.snapshot()
    except GitHubError as exc:
        return {"url": comment.get("url") or current["url"], "already": False,
                "problems": ["could not re-read the pending review to confirm the change: %s" % exc]}
    now = after["review"]
    if now is None or now["id"] != review["id"]:
        problems.append("the pending review %s is gone from GitHub (submitted or deleted meanwhile?)" % review["id"])
    else:
        by_id = {c["id"]: c for c in now["comments"]}
        stored = by_id.get(node_id)
        if stored is None or _normal(stored["body"]) != _normal(body):
            problems.append("GitHub does not show the new text in the pending review")
        for old in review["comments"]:
            if old["id"] != node_id and (old["id"] not in by_id or by_id[old["id"]]["body"] != old["body"]):
                problems.append("comment %s of the pending review changed meanwhile" % old["databaseId"])
    if after["submitted"] != before["submitted"]:
        problems.append("you have %d submitted reviews on the pull request now, %d before"
                        % (after["submitted"], before["submitted"]))
    return {"url": comment.get("url") or current["url"], "already": False, "problems": problems}


def fetch_discussion(client: PullRequest) -> dict:
    """What ``POST /api/github/sync`` takes: the viewer, the pull request head, its review threads and the
    non-empty bodies of its submitted reviews, in ccr's shape (section 10.5)."""
    raw = client.discussion()

    def comment(node):
        return {"id": node["id"], "database_id": node["databaseId"], "body": node["body"], "url": node["url"],
                "created_at": node["createdAt"], "edited_at": node.get("lastEditedAt"), "state": node["state"],
                "login": (node.get("author") or {}).get("login") or "ghost",
                "reply_to": (node.get("replyTo") or {}).get("id")}

    threads = [{"id": t["id"], "path": t["path"], "line": t["line"], "start_line": t["startLine"],
                "original_line": t["originalLine"], "original_start_line": t["originalStartLine"],
                "side": t["diffSide"], "subject_type": t["subjectType"], "outdated": t["isOutdated"],
                "resolved": t["isResolved"], "comments": [comment(c) for c in t["comments"]]}
               for t in raw["threads"] if t["comments"]]
    reviews = [{"id": r["id"], "database_id": r["databaseId"], "body": r["body"], "url": r["url"], "state": r["state"],
                "submitted_at": r["submittedAt"], "login": (r.get("author") or {}).get("login") or "ghost"}
               for r in raw["reviews"] if (r.get("body") or "").strip()]
    return {"viewer": client.login(), "head": raw["head"], "threads": threads, "reviews": reviews}


def _recheck(client: PullRequest, before: dict, review: dict, comment: dict, body: str) -> list:
    """What the re-read of the pending review does not confirm about the comment just added."""
    after = client.snapshot()
    problems = []
    now = after["review"]
    if now is None or now["id"] != review["id"]:
        problems.append("the pending review %s is gone from GitHub (submitted or deleted meanwhile?)" % review["id"])
    else:
        by_id = {c["id"]: c for c in now["comments"]}
        if comment["id"] is not None:
            stored = by_id.get(comment["id"])
            if stored is None:
                problems.append("the new comment is missing from the pending review")
            elif _normal(stored["body"]) != _normal(body):
                problems.append("GitHub stored a different body for the new comment")
        for old in (before["review"] or {}).get("comments", []):
            if old["id"] not in by_id or by_id[old["id"]]["body"] != old["body"]:
                problems.append("comment %s of the pending review changed meanwhile" % old["databaseId"])
    if after["submitted"] != before["submitted"]:
        problems.append("you have %d submitted reviews on the pull request now, %d before"
                        % (after["submitted"], before["submitted"]))
    return problems


def _record(review: dict, thread, comment: dict, target: dict) -> dict:
    """What the store keeps about a posted GitHub comment (``posted_at`` is added by the store)."""
    return {"url": comment["url"], "comment_id": comment["databaseId"], "node_id": comment["id"],
            "thread_id": thread["id"] if thread else target.get("thread_id"), "review_id": review["id"],
            "path": target["path"], "subject_type": target["subject_type"], "line": target.get("line"),
            "side": target.get("side"), "start_line": target.get("start_line"),
            "start_side": target.get("start_side"), "commit": target["commit"]}

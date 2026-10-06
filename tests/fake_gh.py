"""A fake ``gh`` CLI for the PR-mode tests: an in-memory GitHub answering exactly what ccr asks of ``gh api``.

It understands ccr's named GraphQL operations (``CcrViewer``, ``CcrPendingReview``, ``CcrReviewComments``,
``CcrThreads``, ``CcrThreadComments``, ``CcrReviews``, ``CcrStartReview``, ``CcrDiscardReview``, ``CcrAddThread``,
``CcrAddReply``, ``CcrUpdateComment``) sent as ``gh api graphql --input -`` and the REST compare call that yields a merge base, and it
enforces what GitHub enforces and ccr relies on: one pending review per user, reviews only on commits of the pull
request, threads only on lines of the pull request diff, other users' pending comments invisible, everything paged
``first``/``after``, and nothing but pending reviews touched.  Anything else fails loudly, so a test notices
when ccr starts asking GitHub something new.

In-process tests use :class:`FakeGitHub` (``PullRequest(pr, run=fake.run)``); the CLI tests put a ``gh`` shim on
PATH that runs :func:`main` with the state kept in the JSON file named by ``FAKE_GH_STATE``.  ``state["fail"]``
(``{operation: count}``) makes the next ``count`` requests of an operation fail, ``state["texts"]``
(``{path: {"RIGHT"|"LEFT": {line: text}}}``) gives the rows a new comment's ``diffHunk`` ends with.
"""

from __future__ import annotations

import json
import os
import re
import sys
from types import SimpleNamespace

STATE_ENV = "FAKE_GH_STATE"
_OPERATION_RE = re.compile(r"^\s*(query|mutation)\s+(\w+)")
_COMPARE_RE = re.compile(r"^repos/([^/]+)/([^/]+)/compare/([0-9a-f]+)\.\.\.([0-9a-f]+)$")
_COMMENT_KEYS = ("id", "databaseId", "body", "path", "line", "startLine", "subjectType", "diffHunk", "url")
_SUBMITTED = ("COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def new_state(owner="o", repo="r", number=7, head="", base="", merge_base=None, login="reviewer", diff=None,
              commits=None, texts=None) -> dict:
    """A pull request with no reviews; ``diff`` = ``{path: {"RIGHT": [lines], "LEFT": [lines]}}`` (None: any line)."""
    return {
        "login": login,
        "pr": {"owner": owner, "repo": repo, "number": number, "id": "PR_node_%d" % number,
               "url": "https://github.com/%s/%s/pull/%d" % (owner, repo, number), "head": head, "base": base,
               "commits": list(commits or [head])},
        "merge_base": merge_base or base,
        "diff": diff,
        "texts": texts or {},
        "reviews": [],
        "fail": {},
        "next_id": 1000,
        "calls": [],
    }


class FakeGitHub:
    """The fake's state plus the request handlers; ``run`` has :func:`ccr.github.run_gh`'s contract."""

    def __init__(self, state: dict, path=None):
        self.state = state
        self.path = path
        self.after_add_thread = None  # in-process test hook: called with the state right after a thread was added

    # ------------------------------------------------------------------ entry points

    def run(self, args, stdin=None) -> bytes:
        from ccr import github
        code, out, err = self.handle(list(args), stdin)
        if code != 0:
            raise github.GitHubError(github._gh_failure(SimpleNamespace(returncode=code, stdout=out, stderr=err)))
        return out

    def handle(self, args: list, stdin) -> tuple:
        """``(exit code, stdout bytes, stderr bytes)`` of ``gh <args>``."""
        if args[:1] != ["api"] or len(args) < 2:
            return 2, b"", b"fake gh: only 'gh api' is supported\n"
        hostname = args[args.index("--hostname") + 1] if "--hostname" in args else "github.com"
        self.state["calls"].append({"args": args, "hostname": hostname})
        try:
            if args[1] == "graphql":
                if args[2:4] != ["--input", "-"]:
                    return 2, b"", b"fake gh: graphql requests must come as --input -\n"
                request = json.loads(stdin.decode("utf-8"))
                result = self._graphql(request["query"], request.get("variables") or {})
            else:
                result = self._rest(args)
        except FakeError as exc:
            body = {"data": None, "errors": [{"message": str(exc)}]}
            return 1, json.dumps(body).encode("utf-8"), ("gh: %s\n" % exc).encode("utf-8")
        finally:
            self.save()
        if isinstance(result, str):
            return 0, (result + "\n").encode("utf-8"), b""
        return 0, json.dumps({"data": result}).encode("utf-8"), b""

    def save(self) -> None:
        if self.path:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle)

    # ------------------------------------------------------------------ helpers for tests

    def operations(self) -> list:
        """Names of the GraphQL operations (``"compare"`` for the REST call) asked for so far."""
        return [call.get("operation", "compare") for call in self.state["calls"]]

    def pending(self):
        return next((r for r in self.state["reviews"] if r["author"] == self.state["login"] and r["state"] == "PENDING"),
                    None)

    def add_user_review(self, commit: str, comments=(), state="PENDING", author=None, body="") -> dict:
        """A review the user (or ``author``) made in the GitHub UI: ``comments`` = ``[(path, line, body[, side])]``."""
        review = self._new_review(author or self.state["login"], commit, state, body)
        for path, line, text, *side in comments:
            review["comments"].append(self._new_comment(review, {"path": path, "line": line, "body": text,
                                                                 "side": side[0] if side else "RIGHT",
                                                                 "subjectType": "LINE"}))
        self.save()
        return review

    def add_reply(self, review: dict, root: dict, body: str) -> dict:
        """A reply in ``root``'s thread, made by ``review``'s author in the GitHub UI."""
        reply = self._new_comment(review, {"path": root["path"], "line": root["line"], "side": root["side"],
                                           "subjectType": root["subjectType"], "body": body})
        reply["reply_to"] = root["id"]
        review["comments"].append(reply)
        self.save()
        return reply

    # ------------------------------------------------------------------ GraphQL

    def _graphql(self, query: str, variables: dict) -> dict:
        match = _OPERATION_RE.match(query)
        if not match:
            raise FakeError("fake gh: anonymous GraphQL operation")
        name = match.group(2)
        self.state["calls"][-1]["operation"] = name
        if self.state["fail"].get(name):
            self.state["fail"][name] -= 1
            raise FakeError("fake gh: %s failed as the test asked" % name)
        handler = getattr(self, "_op_" + name, None)
        if handler is None:
            raise FakeError("fake gh: unknown operation %s" % name)
        return handler(variables)

    def _op_CcrViewer(self, variables: dict) -> dict:
        return {"viewer": {"login": self.state["login"]}}

    def _check_pr(self, owner, name, number) -> dict:
        pr = self.state["pr"]
        if (owner, name, number) != (pr["owner"], pr["repo"], pr["number"]):
            raise FakeError("Could not resolve to a PullRequest with the number of %s." % number)
        return pr

    def _op_CcrPendingReview(self, variables: dict) -> dict:
        pr = self._check_pr(variables["owner"], variables["name"], variables["number"])
        login = variables["login"]
        mine = [r for r in self.state["reviews"] if r["author"] == login]
        pending = [self._review_node(r, variables["page"]) for r in mine if r["state"] == "PENDING"]
        submitted = sum(1 for r in mine if r["state"] != "PENDING")
        return {"repository": {"pullRequest": {
            "id": pr["id"], "url": pr["url"], "headRefOid": pr["head"], "baseRefOid": pr["base"],
            "commits": {"totalCount": len(pr["commits"]), "nodes": [{"commit": {"oid": oid}} for oid in pr["commits"][-100:]]},
            "pending": {"nodes": pending}, "submitted": {"totalCount": submitted}}}}

    def _visible(self, review: dict) -> bool:
        return review["state"] != "PENDING" or review["author"] == self.state["login"]

    def _threads(self) -> list:
        """``[(root, [root, replies...])]`` of every thread the viewer can see, oldest first."""
        comments = [(r, c) for r in self.state["reviews"] if self._visible(r) for c in r["comments"]]
        roots = [(r, c) for r, c in comments if not c.get("reply_to")]
        return [(root, [(review, root)] + [(r, c) for r, c in comments if c.get("reply_to") == root["id"]])
                for review, root in sorted(roots, key=lambda rc: rc[1]["databaseId"])]

    @staticmethod
    def _cut(items: list, size: int, after) -> tuple:
        offset = int(after) if after else 0
        return items[offset:offset + size], {"hasNextPage": offset + size < len(items), "endCursor": str(offset + size)}

    def _thread_comment(self, review: dict, comment: dict) -> dict:
        return {"id": comment["id"], "databaseId": comment["databaseId"], "body": comment["body"], "url": comment["url"],
                "createdAt": comment["created_at"], "lastEditedAt": comment.get("edited_at"),
                "state": "PENDING" if review["state"] == "PENDING" else "SUBMITTED",
                "author": {"login": review["author"]}, "replyTo": {"id": comment["reply_to"]} if comment.get("reply_to") else None}

    def _thread_node(self, root: dict, members: list, page: int) -> dict:
        chunk, info = self._cut(members, page, None)
        outdated = bool(root.get("outdated"))
        return {"id": root["thread_id"], "isResolved": bool(root.get("resolved")), "isOutdated": outdated,
                "path": root["path"], "line": None if outdated else root["line"], "startLine": None if outdated else root["startLine"],
                "originalLine": root.get("original_line", root["line"]), "originalStartLine": None,
                "diffSide": root["side"], "subjectType": root["subjectType"],
                "comments": {"pageInfo": info, "nodes": [self._thread_comment(r, c) for r, c in chunk]}}

    def _op_CcrThreads(self, variables: dict) -> dict:
        pr = self._check_pr(variables["owner"], variables["name"], variables["number"])
        chunk, info = self._cut(self._threads(), variables["page"], variables.get("after"))
        return {"repository": {"pullRequest": {"headRefOid": pr["head"], "reviewThreads": {
            "pageInfo": info, "nodes": [self._thread_node(root, members, variables["page"]) for root, members in chunk]}}}}

    def _op_CcrThreadComments(self, variables: dict) -> dict:
        found = next(((root, members) for root, members in self._threads() if root["thread_id"] == variables["thread"]), None)
        if found is None:
            return {"node": None}
        chunk, info = self._cut(found[1], variables["page"], variables["after"])
        return {"node": {"comments": {"pageInfo": info, "nodes": [self._thread_comment(r, c) for r, c in chunk]}}}

    def _op_CcrReviews(self, variables: dict) -> dict:
        self._check_pr(variables["owner"], variables["name"], variables["number"])
        reviews = [r for r in self.state["reviews"] if r["state"] in _SUBMITTED]
        chunk, info = self._cut(reviews, variables["page"], variables.get("after"))
        return {"repository": {"pullRequest": {"reviews": {"pageInfo": info, "nodes": [
            {"id": r["id"], "databaseId": r["databaseId"], "body": r.get("body", ""), "url": r["url"], "state": r["state"],
             "submittedAt": r.get("submitted_at"), "author": {"login": r["author"]}} for r in chunk]}}}}

    def _op_CcrAddReply(self, variables: dict) -> dict:
        data = variables["input"]
        review = next((r for r in self.state["reviews"] if r["id"] == data.get("pullRequestReviewId")), None)
        if review is None or review["author"] != self.state["login"] or review["state"] != "PENDING":
            raise FakeError("fake gh: replies go into the user's own pending review only")
        found = next((root for root, _ in self._threads() if root["thread_id"] == data.get("pullRequestReviewThreadId")), None)
        if found is None:
            raise FakeError("Could not resolve to a node with the global id of '%s'" % data.get("pullRequestReviewThreadId"))
        reply = self.add_reply(review, found, data["body"])
        return {"addPullRequestReviewThreadReply": {"comment": {"id": reply["id"], "databaseId": reply["databaseId"],
                                                                "body": reply["body"], "url": reply["url"],
                                                                "replyTo": {"id": found["id"]}}}}

    def _op_CcrUpdateComment(self, variables: dict) -> dict:
        data = variables["input"]
        if set(data) != {"pullRequestReviewCommentId", "body"}:
            raise FakeError("fake gh: ccr updates a comment's body only: %s" % sorted(data))
        found = next(((r, c) for r in self.state["reviews"] for c in r["comments"]
                      if c["id"] == data["pullRequestReviewCommentId"]), None)
        if found is None:
            raise FakeError("Could not resolve to a node with the global id of '%s'" % data["pullRequestReviewCommentId"])
        review, comment = found
        if review["author"] != self.state["login"] or review["state"] != "PENDING":
            raise FakeError("fake gh: ccr updates comments of the user's own pending review only")
        comment["body"] = data["body"]
        self.save()
        return {"updatePullRequestReviewComment": {"pullRequestReviewComment": {
            "id": comment["id"], "databaseId": comment["databaseId"], "body": comment["body"], "url": comment["url"]}}}

    def _op_CcrReviewComments(self, variables: dict) -> dict:
        review = next((r for r in self.state["reviews"] if r["id"] == variables["review"]), None)
        if review is None:
            return {"node": None}
        return {"node": {"comments": self._page(review, variables["page"], int(variables["after"]))}}

    def _op_CcrStartReview(self, variables: dict) -> dict:
        data = variables["input"]
        if set(data) - {"pullRequestId", "commitOID"}:
            raise FakeError("fake gh: ccr must start a review with no body, event, comments or threads: %s"
                            % sorted(data))
        if data["pullRequestId"] != self.state["pr"]["id"]:
            raise FakeError("Could not resolve to a node with the global id of '%s'" % data["pullRequestId"])
        if data.get("commitOID") not in self.state["pr"]["commits"]:
            raise FakeError("fake gh: %s is not a commit of the pull request" % data.get("commitOID"))
        if self.pending() is not None:
            raise FakeError("User can only have one pending review per pull request")
        review = self._new_review(self.state["login"], self.state.get("start_review_on") or data["commitOID"], "PENDING")
        return {"addPullRequestReview": {"pullRequestReview": self._review_node(review, 0)}}

    def _op_CcrDiscardReview(self, variables: dict) -> dict:
        review = next((r for r in self.state["reviews"] if r["id"] == variables["input"]["pullRequestReviewId"]), None)
        if review is None or review["author"] != self.state["login"] or review["state"] != "PENDING" or review["comments"]:
            raise FakeError("fake gh: ccr may delete only its own empty pending review")
        self.state["reviews"].remove(review)
        return {"deletePullRequestReview": {"pullRequestReview": {"id": review["id"]}}}

    def _op_CcrAddThread(self, variables: dict) -> dict:
        data = variables["input"]
        review = next((r for r in self.state["reviews"] if r["id"] == data.get("pullRequestReviewId")), None)
        if review is None or review["author"] != self.state["login"] or review["state"] != "PENDING":
            raise FakeError("fake gh: threads go into the user's own pending review only")
        if data.get("subjectType", "LINE") == "LINE":
            side = data.get("side", "RIGHT")
            allowed = self._diff_lines(data["path"], side)
            for line in filter(None, (data.get("line"), data.get("startLine"))):
                if allowed is not None and line not in allowed:
                    raise FakeError("Pull request review thread line must be part of the diff")
        elif any(key in data for key in ("line", "side", "startLine", "startSide")):
            raise FakeError("fake gh: a file-level thread takes no line or side")
        if self.state.get("no_thread"):
            return {"addPullRequestReviewThread": {"thread": None}}
        comment = self._new_comment(review, data)
        review["comments"].append(comment)
        if self.after_add_thread is not None:
            self.after_add_thread(self.state)
        if self.state.get("thread_without_comment"):
            return {"addPullRequestReviewThread": {"thread": dict(self._added_thread(comment), comments={"nodes": []})}}
        return {"addPullRequestReviewThread": {"thread": self._added_thread(comment)}}

    def _diff_lines(self, path: str, side: str):
        diff = self.state["diff"]
        if diff is None:
            return None
        if path not in diff:
            raise FakeError("Path could not be resolved")
        return set(diff[path].get(side, []))

    # ------------------------------------------------------------------ REST

    def _rest(self, args: list):
        match = _COMPARE_RE.match(args[1])
        if not match or args[2:4] != ["--jq", ".merge_base_commit.sha"]:
            raise FakeError("fake gh: unexpected REST call %s" % args[1:])
        self._check_pr(match.group(1), match.group(2), self.state["pr"]["number"])
        return self.state["merge_base"]

    # ------------------------------------------------------------------ records

    def _next(self) -> int:
        self.state["next_id"] += 1
        return self.state["next_id"]

    def _new_review(self, author: str, commit: str, state: str, body: str = "") -> dict:
        number = self._next()
        review = {"id": "PRR_%d" % number, "databaseId": number, "state": state, "author": author, "commit": commit,
                  "body": body, "submitted_at": None if state == "PENDING" else "2026-09-01T10:%02d:00Z" % (number % 60),
                  "url": "%s#pullrequestreview-%d" % (self.state["pr"]["url"], number), "comments": []}
        self.state["reviews"].append(review)
        return review

    def _new_comment(self, review: dict, data: dict) -> dict:
        number = self._next()
        on_line = data.get("subjectType", "LINE") == "LINE"
        side = data.get("side", "RIGHT") if on_line else None
        text = ((self.state["texts"].get(data["path"]) or {}).get(side) or {}).get(str(data.get("line")), "")
        return {"id": "PRRC_%d" % number, "databaseId": number, "thread_id": "PRRT_%d" % number,
                "body": data["body"], "path": data["path"], "line": data.get("line") if on_line else None,
                "startLine": data.get("startLine"), "side": side,
                "startSide": data.get("startSide") if data.get("startLine") else None,
                "subjectType": data.get("subjectType", "LINE"),
                "diffHunk": "@@ -1,1 +1,1 @@\n%s%s" % ("+" if side == "RIGHT" else "-", text) if on_line else "",
                "url": "%s#discussion_r%d" % (self.state["pr"]["url"], number), "reply_to": None,
                "created_at": "2026-09-01T11:%02d:%02dZ" % (number // 60 % 60, number % 60)}

    def _added_thread(self, comment: dict) -> dict:
        return {"id": comment["thread_id"], "path": comment["path"], "line": comment["line"],
                "startLine": comment["startLine"], "diffSide": comment["side"], "startDiffSide": comment["startSide"],
                "subjectType": comment["subjectType"],
                "comments": {"nodes": [{key: comment[key] for key in ("id", "databaseId", "body", "url")}]}}

    @staticmethod
    def _page(review: dict, size: int, offset: int) -> dict:
        chunk = review["comments"][offset:offset + size]
        more = offset + size < len(review["comments"])
        return {"totalCount": len(review["comments"]), "pageInfo": {"hasNextPage": more, "endCursor": str(offset + size)},
                "nodes": [dict({key: c[key] for key in _COMMENT_KEYS}, replyTo={"id": c["reply_to"]} if c.get("reply_to") else None)
                          for c in chunk]}

    def _review_node(self, review: dict, page: int) -> dict:
        return {"id": review["id"], "databaseId": review["databaseId"], "state": review["state"],
                "url": review["url"], "commit": {"oid": review["commit"]}, "comments": self._page(review, page, 0)}


class FakeError(Exception):
    """A request the fake GitHub refuses (reported like GitHub's GraphQL errors)."""


def load(path) -> FakeGitHub:
    with open(path, encoding="utf-8") as handle:
        return FakeGitHub(json.load(handle), path)


def main(argv) -> int:
    fake = load(os.environ[STATE_ENV])
    stdin = sys.stdin.buffer.read() if "--input" in argv else None
    code, out, err = fake.handle(list(argv), stdin)
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

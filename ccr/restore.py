"""Reading a review back from its export (SPEC.md section 6.4), for ``ccr restore``.

``ccr export --json`` (also written by ``ccr stop``) is lossless; the Markdown export, the only one older ccr
versions wrote, is parsed as well.  Both become one *restore payload* that
:meth:`~ccr.store.ReviewStore.restore` loads into an empty review::

    {"source": "json" | "markdown", "pr": "owner/repo#N" | None, "cover": str | None,
     "rounds": [{"number", "submitted_at", "verdict", "summary", "base"?, "head"?, "commit_shas"?}],
     "comments": [{"id", "parent_id", "author", "body", "created_at", "updated_at", "state", "round", "resolved",
                   "anchor", "snippet"?, "github"}]}

A Markdown export loses a little (section 6.4): the creation time of a thread's first comment, the code snippets
(the store takes them from git again), a round's base, head and commits, every GitHub record but the URL of a
posted comment, and the node ids of the comments mirrored from GitHub.  Its anchors keep abbreviated shas, which
the store resolves.  Everything here is a pure function of the text.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .render import COMBINED, SINCE, WORKTREE

__all__ = ["RestoreError", "parse", "parse_json", "parse_markdown"]

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_TITLE_RE = re.compile(r"^# Review — .* — exported \S+$")
_PR_RE = re.compile(r" — PR (\S+/\S+#\d+) — exported \S+$")
_ROOT_RE = re.compile(r"^#### \[id: ([0-9a-z]+)\] (.*)$")
_REPLY_RE = re.compile(r"^  ↳ \[id: ([0-9a-z]+)\] (.*)$")
_ROUND_RE = re.compile(r"^- Round (\d+) · (?:([a-z_]+) · )?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) · \d+ comments?"
                       r"(?: · \"(.*)\")?(?:  \[id: ([0-9a-z]+)\])?$")
_LINES_RE = re.compile(r"^(new|old):(\d+)(?:-(\d+))?$")
_AUTHOR_RE = re.compile(r"^@(\S+) \(GitHub(, you)?\)$")
_POSTED_RE = re.compile(r"^GitHub (?:comment|reply) \(posted: (\S+?)(?:; edited since, the update not posted yet)?\)$")
_ROOT_INTENTS = ("question", "GitHub comment", "GitHub thread", "GitHub review")
_GROUPS = {"## All changes (combined)": COMBINED, "## Uncommitted changes": WORKTREE,
           "## Since your last review": SINCE, "## Review-level comments": "review",
           "## Outdated (anchored to commits no longer in the range)": "outdated"}
_NO_ANCHOR = {"kind": None, "commit": None, "path": None, "side": None, "line": None, "start_line": None}
_ROOT_LEAD_SECONDS = 60  # how long before its earliest known time a Markdown thread's first comment is dated


class RestoreError(ValueError):
    """An export that cannot be read back."""


def parse(text: str) -> dict:
    """The restore payload of an export, whichever of the two formats it is."""
    if text.lstrip().startswith("{"):
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise RestoreError("not a ccr JSON export: %s" % exc) from None
        return parse_json(document)
    return parse_markdown(text)


def parse_json(document) -> dict:
    """The restore payload of ``ccr export --json``: its stored fields, without what the server derives."""
    if not isinstance(document, dict) or not isinstance(document.get("review"), dict) \
            or not isinstance(document.get("comments"), list) or not isinstance(document.get("rounds"), list):
        raise RestoreError("not a ccr JSON export (it has no review, rounds and comments)")
    review = document["review"]
    pr = review.get("pr") or None
    keys = ("id", "parent_id", "author", "body", "created_at", "updated_at", "state", "round", "resolved",
            "anchor", "snippet", "moved_from", "github")
    round_keys = ("number", "submitted_at", "verdict", "summary", "base", "head", "commit_shas")
    return {
        "source": "json",
        "pr": "%s/%s#%s" % (pr.get("owner"), pr.get("repo"), pr.get("number")) if isinstance(pr, dict) else None,
        "cover": review.get("cover") or None,
        "rounds": [{key: r.get(key) for key in round_keys} for r in document["rounds"] if isinstance(r, dict)],
        "comments": [{key: c.get(key) for key in keys} for c in document["comments"] if isinstance(c, dict)],
    }


def _shift(stamp: str, seconds: int) -> str:
    return (datetime.strptime(stamp, _TIME_FORMAT) + timedelta(seconds=seconds)).strftime(_TIME_FORMAT)


def _unescape(lines: list, indent: str = "") -> str:
    """A body as ``render._body_lines`` wrote it: indented, ``#`` lines escaped as ``\\#``; trailing blanks dropped."""
    out = []
    for line in lines:
        if indent:
            line = line[len(indent):] if line.startswith(indent) else ("" if not line.strip() else line)
        out.append(line[1:] if line.startswith("\\#") else line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _author(text: str):
    """``(author, login, own)`` of an author label."""
    match = _AUTHOR_RE.match(text)
    return ("github", match.group(1), bool(match.group(2))) if match else (text, None, False)


def _github_record(intent, comment_id: str):
    """The GitHub record of the user's comment from its intent label; None for a question or a plain comment."""
    if intent is None or intent == "question":
        return None
    match = _POSTED_RE.match(intent)
    if match:
        url = match.group(1)
        number = re.search(r"discussion_r(\d+)$", url)
        record = {"status": "posted", "url": url, "comment_id": int(number.group(1)) if number else None}
        if "; edited since" in intent:
            record["edited"] = True
        return record
    if intent in ("GitHub comment (not posted)", "GitHub reply (not posted)"):
        return {"status": "local"}
    raise RestoreError("comment %s: unknown label %r" % (comment_id, intent))


def _mirror_record(intent, login: str, own: bool) -> dict:
    """What a Markdown export keeps of a mirrored comment's GitHub record: who wrote it and the flags it shows."""
    record = {"status": "remote", "login": login, "own": own}
    flags = re.search(r"\((.*)\)$", intent or "")
    flags = flags.group(1).split(", ") if flags else []
    if intent and intent.startswith("GitHub review"):
        record.update(kind="review", review_state=(flags[0] if flags else "").upper().replace(" ", "_") or None)
        return record
    if "deleted there" in flags:
        record["deleted"] = True
    if "resolved there" in flags:
        record["resolved_on_github"] = True
    if "pending" in flags:
        record["state"] = "PENDING"
    return record


def _round_number(tag: str, comment_id: str):
    if tag in ("pending", "on GitHub"):
        return None
    if re.match(r"^R\d+$", tag):
        return int(tag[1:])
    raise RestoreError("comment %s: unknown round %r" % (comment_id, tag))


def _anchor(text: str, group, path, comment_id: str) -> dict:
    """The anchor of a thread from its group heading, its path heading and the anchor part of its header line.

    Commits stay as the export wrote them (abbreviated shas); the store resolves them.
    """
    anchor = dict(_NO_ANCHOR)
    tokens = text.split(" → ")[0].split(" ")
    core = tokens[-1]
    if group[0] == "outdated":                  # "<short sha> [<path>] <core>"
        anchor["commit"] = tokens[0]
        anchor["path"] = " ".join(tokens[1:-1]) or None
    elif group[0] != "review":
        anchor["commit"] = group[1] if group[0] == "commit" else group[0]
        if path and path.startswith("(file) "):
            anchor["path"] = path[len("(file) "):]
        elif path != "(commit)":
            anchor["path"] = path
    lines = _LINES_RE.match(core)
    if lines:
        anchor.update(kind="line", side=lines.group(1))
        if lines.group(3):
            anchor.update(start_line=int(lines.group(2)), line=int(lines.group(3)))
        else:
            anchor["line"] = int(lines.group(2))
    elif core in ("file", "commit", "review"):
        anchor["kind"] = core
        if core != "file":
            anchor["path"] = None
        if core == "review":
            anchor["commit"] = None
    else:
        raise RestoreError("comment %s: unknown anchor %r" % (comment_id, text))
    if anchor["kind"] in ("line", "file") and not anchor["path"]:
        raise RestoreError("comment %s: no path for anchor %r" % (comment_id, text))
    return anchor


def parse_markdown(text: str) -> dict:
    """The restore payload of a Markdown export (``ccr export --md``, or what ``ccr stop`` wrote)."""
    lines = text.split("\n")
    if not lines or not _TITLE_RE.match(lines[0]):
        raise RestoreError("not a ccr export (the first line is not '# Review — … — exported …')")
    pr = _PR_RE.search(lines[0])

    def structural(i: int) -> bool:
        if i >= len(lines):
            return True
        line = lines[i]
        return line.startswith(("#### [id: ", "### ", "## ")) or _REPLY_RE.match(line) is not None

    def read_body(i: int, indent: str):
        body = []
        while i < len(lines) and not structural(i) and not (lines[i] == "" and structural(i + 1)):
            body.append(lines[i])
            i += 1
        return _unescape(body, indent), i

    cover, rounds, comments = None, [], []
    group, path = None, None
    i = 1
    while i < len(lines):
        line = lines[i]
        if line == "## Cover letter":
            end = i + 1
            while end < len(lines) and lines[end] != "## Rounds":
                end += 1
            cover, i = _unescape(lines[i + 1:end]).strip("\n"), end
            continue
        match = _ROUND_RE.match(line)
        if match and group is None:
            rounds.append({"number": int(match.group(1)), "verdict": match.group(2) or "comment",
                           "submitted_at": match.group(3), "summary": (match.group(4) or "").replace("␤", "\n"),
                           "summary_id": match.group(5)})
        elif line.startswith("## Commit "):
            group, path = ("commit", line.split()[2]), None
        elif line in _GROUPS:
            group, path = (_GROUPS[line],), None
        elif line.startswith("### ") and group is not None:
            path = line[4:]
        match = _ROOT_RE.match(line)
        if not match or group is None:
            i += 1
            continue
        root_id = match.group(1)
        parts = match.group(2).split(" · ")
        author, login, own = _author(parts.pop(0))
        intent = parts.pop(0) if parts and parts[0].startswith(_ROOT_INTENTS) else None
        if len(parts) < 3:
            raise RestoreError("comment %s: short header %r" % (root_id, line))
        anchor = _anchor(parts[0], group, path, root_id)
        edited = next((p[len("edited "):] for p in parts[3:] if p.startswith("edited ")), None)
        i += 1
        if anchor["kind"] == "line" and i < len(lines) and lines[i].startswith("```"):
            i += 1
            while i < len(lines) and lines[i] != "```":
                i += 1
            i += 1
        body, i = read_body(i, "")
        mirrored = author == "github"
        root = {"id": root_id, "parent_id": None, "author": author, "body": body, "created_at": None,
                "updated_at": edited, "round": _round_number(parts[1], root_id), "resolved": parts[2] == "resolved",
                "anchor": anchor,
                "github": _mirror_record(intent, login, own) if mirrored else _github_record(intent, root_id)}
        comments.append(root)
        while i + 1 < len(lines) and lines[i] == "" and _REPLY_RE.match(lines[i + 1]):
            reply = _REPLY_RE.match(lines[i + 1])
            reply_parts = reply.group(2).split(" · ")
            reply_author, reply_login, reply_own = _author(reply_parts.pop(0))
            reply_intent = reply_parts.pop(0) if reply_parts and not _TIME_RE.match(reply_parts[0]) else None
            if len(reply_parts) < 2:
                raise RestoreError("comment %s: short header %r" % (reply.group(1), lines[i + 1]))
            reply_edited = next((p[len("edited "):] for p in reply_parts[2:] if p.startswith("edited ")), None)
            reply_body, i = read_body(i + 2, "  ")
            comments.append({
                "id": reply.group(1), "parent_id": root_id, "author": reply_author, "body": reply_body,
                "created_at": reply_parts[0], "updated_at": reply_edited or reply_parts[0],
                "round": _round_number(reply_parts[1], reply.group(1)), "resolved": False, "anchor": anchor,
                "github": _mirror_record(reply_intent, reply_login, reply_own) if reply_author == "github"
                else _github_record(reply_intent, reply.group(1))})
    _date_roots(comments, rounds)
    bodies = {c["id"]: c["body"] for c in comments}
    for round_info in rounds:  # the summary line folds newlines; the comment submit made of it does not
        summary_id = round_info.pop("summary_id")
        if summary_id in bodies:
            round_info["summary"] = bodies[summary_id]
    for comment in comments:
        comment["state"] = "pending" if comment["round"] is None and comment["author"] != "github" else "submitted"
    return {"source": "markdown", "pr": pr.group(1) if pr else None, "cover": cover, "rounds": rounds,
            "comments": comments}


def _date_roots(comments: list, rounds: list) -> None:
    """Date each thread's first comment, which a Markdown export does not.

    A minute before the earliest of its first reply and its last edit, and, for the user's comment, of the
    submission of its round; Claude's comment of round N was written after round N and before round N + 1.
    """
    submitted = {r["number"]: r["submitted_at"] for r in rounds}
    replies = {}
    for comment in comments:
        if comment["parent_id"] is not None:
            replies.setdefault(comment["parent_id"], []).append(comment["created_at"])
    latest = max([r["submitted_at"] for r in rounds] + [t for times in replies.values() for t in times]
                 + [c["updated_at"] for c in comments if c["updated_at"]], default=None)
    for comment in comments:
        if comment["parent_id"] is not None:
            continue
        stamps = replies.get(comment["id"], []) + ([comment["updated_at"]] if comment["updated_at"] else [])
        after = None
        if comment["author"] == "claude":
            after = submitted.get(comment["round"])
            stamps += [submitted[comment["round"] + 1]] if comment["round"] is not None \
                and comment["round"] + 1 in submitted else []
        elif comment["author"] != "github" and comment["round"] in submitted:
            stamps.append(submitted[comment["round"]])
        if stamps:
            created = max(_shift(min(stamps), -_ROOT_LEAD_SECONDS), after or "")
        else:
            created = _shift(after, _ROOT_LEAD_SECONDS) if after else latest
        comment["created_at"] = created
        comment["updated_at"] = comment["updated_at"] or created

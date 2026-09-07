"""Markdown and JSON rendering of review comments (SPEC.md sections 6.2 and 6.3).

Everything here is a pure function over the plain dicts the HTTP API returns
(``/api/review``, ``/api/comments?locate=1`` and, for code snippets, one
untrimmed FileDiff per anchored file obtained through a caller-supplied
``fetch_file_diff(commit, path)`` callback), so the CLI can render without a
:class:`~ccr.store.ReviewStore`.

The Markdown is optimised for an LLM reader: a deterministic order (chain
order, then path, then end line, then creation time), explicit ``[id: …]``
tags, numbered code context with the anchored rows marked ``>``, HEAD-relative
locations and unambiguous anchors.  Every repository- or user-derived string
passes through :func:`clean`; body lines starting with ``#`` are escaped so a
body can never open a heading.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = [
    "clean",
    "build_threads",
    "sort_threads",
    "select_threads",
    "render_comments",
    "render_export",
    "to_json",
]

DEFAULT_CONTEXT = 3
MARK = "★"
SHORT_SHA_LEN = 10
COMBINED = "combined"
WORKTREE = "worktree"

# C0 (minus \t and \n) and C1 controls, line/paragraph separators, and the bidi embedding/isolate controls.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_FULL_SHA_RE = re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b")
_MARKERS = {"ctx": " ", "del": "-", "add": "+"}
_HEAD_LABELS = {"same": "", "live": " (live)", "moved": " (moved)", "changed": " (changed near)", "deleted": " (deleted)"}
_GROUP_REVIEW, _GROUP_COMMIT, _GROUP_COMBINED, _GROUP_WORKTREE, _GROUP_OUTDATED = range(5)


# --------------------------------------------------------------------------- text helpers

def clean(text, single_line: bool = False) -> str:
    """Strip C0/C1 controls (keeping ``\\n``/``\\t``), U+2028/9 and bidi controls; optionally fold newlines to ``␤``."""
    text = "" if text is None else str(text)
    if single_line:
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "␤")
    return _CONTROL_RE.sub("", text)


def _short(sha) -> str:
    return clean(sha, True)[:SHORT_SHA_LEN] if sha else ""


def _short_spec(spec) -> str:
    """A range spec with every full sha abbreviated (``<40hex>..HEAD`` → ``<10hex>..HEAD``)."""
    return _FULL_SHA_RE.sub(lambda m: m.group(0)[:SHORT_SHA_LEN], clean(spec, True))


def _plural(count: int, noun: str, plural: str = None) -> str:
    return "%d %s" % (count, noun if count == 1 else (plural or noun + "s"))


def _body_lines(body, indent: str = "") -> list:
    """Cleaned body lines; a line starting with ``#`` is emitted as ``\\#`` so it cannot open a heading."""
    return [indent + ("\\" + line if line.startswith("#") else line) for line in clean(body).split("\n")]


def _is_edited(comment: dict) -> bool:
    return (comment.get("updated_at") or "") > (comment.get("created_at") or "")


def _round_tag(comment: dict) -> str:
    return "pending" if comment.get("state") == "pending" else "R%s" % comment.get("round")


# --------------------------------------------------------------------------- threads

def build_threads(comments: list) -> list:
    """Group flat comments into ``{"root", "replies", "last_author", "answered"}`` threads.

    Replies keep their input order (the API returns creation order) after a
    stable sort by ``created_at``; a reply whose root is missing is dropped.
    """
    roots = [c for c in comments if c.get("parent_id") is None]
    by_root = {}
    for comment in comments:
        parent = comment.get("parent_id")
        if parent is not None:
            by_root.setdefault(parent, []).append(comment)
    threads = []
    for root in roots:
        replies = sorted(by_root.get(root["id"], []), key=lambda c: c.get("created_at") or "")
        last = replies[-1] if replies else root
        threads.append({"root": root, "replies": replies, "last_author": last.get("author"),
                        "answered": last.get("author") != "user"})
    return threads


def _group_of(root: dict) -> int:
    anchor = root["anchor"]
    if anchor["kind"] == "review":
        return _GROUP_REVIEW
    if root.get("outdated"):
        return _GROUP_OUTDATED
    if anchor["commit"] == COMBINED:
        return _GROUP_COMBINED
    if anchor["commit"] == WORKTREE:
        return _GROUP_WORKTREE
    return _GROUP_COMMIT


def _chain_index(review: dict) -> dict:
    return {c["sha"]: k for k, c in enumerate(review.get("commits") or [])}


def _thread_key(thread: dict, chain: dict):
    """Sort key: group, chain position, then (commit anchors last) path, end line, creation time."""
    root = thread["root"]
    anchor = root["anchor"]
    group = _group_of(root)
    commit = anchor["commit"] or ""
    position = chain.get(commit, len(chain)) if group == _GROUP_COMMIT else 0
    outdated_commit = commit if group == _GROUP_OUTDATED else ""
    kind_rank = 1 if anchor["kind"] == "commit" else 0
    return (group, position, outdated_commit, kind_rank, anchor["path"] or "", anchor["line"] or 0,
            root.get("created_at") or "")


def sort_threads(threads: list, review: dict) -> list:
    """Threads in the section 6.3 order (stable, so equal keys keep the API's creation order)."""
    chain = _chain_index(review)
    return sorted(threads, key=lambda t: _thread_key(t, chain))


def _commit_matches(anchor_commit, wanted: str) -> bool:
    if anchor_commit is None:
        return False
    if anchor_commit == wanted:
        return True
    return len(wanted) >= 4 and anchor_commit not in (COMBINED, WORKTREE) and anchor_commit.startswith(wanted)


def select_threads(threads: list, state=None, round=None, author=None, commit=None, path=None,
                   updated_since=None, unresolved: bool = False, unanswered: bool = False,
                   outdated: str = "include"):
    """Apply the ``ccr comments`` filters; returns ``(selected threads, ids of matching comments)``.

    ``state``/``round``/``author``/``commit``/``path``/``updated_since`` (an ISO
    timestamp; ``updated_at >= updated_since``) match individual comments and
    select the whole thread when any member matches; ``unresolved``,
    ``unanswered`` and ``outdated`` (``include`` | ``exclude`` | ``only``) apply to
    the thread.  The matching ids are empty when no per-comment filter is given.
    """
    per_comment = any(value is not None for value in (state, round, author, commit, path, updated_since))

    def matches(comment: dict) -> bool:
        anchor = comment["anchor"]
        return not (
            (state is not None and comment.get("state") != state)
            or (round is not None and comment.get("round") != round)
            or (author is not None and comment.get("author") != author)
            or (commit is not None and not _commit_matches(anchor.get("commit"), commit))
            or (path is not None and anchor.get("path") != path)
            or (updated_since is not None and (comment.get("updated_at") or "") < updated_since)
        )

    selected, matching = [], set()
    for thread in threads:
        root = thread["root"]
        is_outdated = bool(root.get("outdated"))
        if (outdated == "exclude" and is_outdated) or (outdated == "only" and not is_outdated):
            continue
        if unresolved and root.get("resolved"):
            continue
        if unanswered and (root.get("resolved") or is_outdated or thread["last_author"] != "user"):
            continue
        hits = [c["id"] for c in [root] + thread["replies"] if matches(c)] if per_comment else []
        if per_comment and not hits:
            continue
        selected.append(thread)
        matching.update(hits)
    return selected, matching


# --------------------------------------------------------------------------- headings and headers

def _group_heading(root: dict, commits_by_sha: dict) -> str:
    group = _group_of(root)
    if group == _GROUP_REVIEW:
        return "## Review-level comments"
    if group == _GROUP_OUTDATED:
        return "## Outdated (anchored to commits no longer in the range)"
    if group == _GROUP_COMBINED:
        return "## All changes (combined)"
    if group == _GROUP_WORKTREE:
        return "## Uncommitted changes"
    sha = root["anchor"]["commit"]
    subject = clean((commits_by_sha.get(sha) or {}).get("subject", ""), True)
    return '## Commit %s — "%s"' % (_short(sha), subject)


def _sub_heading(root: dict) -> str:
    """Per-path heading inside a commit group (none for review-level and outdated threads)."""
    if _group_of(root) in (_GROUP_REVIEW, _GROUP_OUTDATED):
        return ""
    anchor = root["anchor"]
    if anchor["kind"] == "line":
        return "### %s" % clean(anchor["path"], True)
    if anchor["kind"] == "file":
        return "### (file) %s" % clean(anchor["path"], True)
    return "### (commit)"


def _line_span(anchor: dict) -> str:
    if anchor.get("start_line"):
        return "%d-%d" % (anchor["start_line"], anchor["line"])
    return str(anchor["line"])


def _anchor_text(root: dict) -> str:
    anchor = root["anchor"]
    kind = anchor["kind"]
    if kind == "line":
        core = "%s:%s" % (anchor["side"], _line_span(anchor))
    else:
        core = kind
    if not root.get("outdated"):
        return core
    parts = [_short(anchor["commit"])] + ([clean(anchor["path"], True)] if anchor.get("path") else []) + [core]
    return " ".join(parts)


def _head_arrow(root: dict) -> str:
    location = root.get("head_location")
    anchor = root["anchor"]
    if not location or anchor["kind"] != "line" or location.get("status") == "unknown":
        return ""
    status = location["status"]
    if status == "file-deleted":
        return " → HEAD %s (file deleted)" % clean(anchor["path"], True)
    line_text = str(location["line"])
    if anchor.get("start_line") and status in ("same", "moved", "live"):
        line_text = "%d-%d" % (location["line"] - (anchor["line"] - anchor["start_line"]), location["line"])
    return " → HEAD %s:%s%s" % (clean(location["path"], True), line_text, _HEAD_LABELS.get(status, " (%s)" % status))


def _thread_header(thread: dict, matching: set, mark: str) -> str:
    root = thread["root"]
    parts = [
        "[id: %s] %s" % (root["id"], clean(root["author"], True)),
        _anchor_text(root) + _head_arrow(root),
        _round_tag(root),
        "resolved" if root.get("resolved") else "unresolved",
        _plural(len(thread["replies"]), "reply", "replies"),
        "last: %s" % clean(thread["last_author"], True),
    ]
    if _is_edited(root):
        parts.append("edited %s" % clean(root["updated_at"], True))
    if root["id"] in matching:
        parts.append(mark)
    return "#### " + " · ".join(parts)


def _reply_lines(reply: dict, matching: set, mark: str) -> list:
    parts = ["[id: %s] %s" % (reply["id"], clean(reply["author"], True)), clean(reply.get("created_at"), True),
             _round_tag(reply)]
    if _is_edited(reply):
        parts.append("edited %s" % clean(reply["updated_at"], True))
    if reply["id"] in matching:
        parts.append(mark)
    return ["  ↳ " + " · ".join(parts)] + _body_lines(reply.get("body"), "  ")


# --------------------------------------------------------------------------- snippets

def _flat_rows(file_diff: dict) -> list:
    """``[(hunk index, row)]`` over every hunk of a FileDiff, in file order."""
    return [(index, row) for index, hunk in enumerate(file_diff.get("hunks") or []) for row in hunk["lines"]]


def _hunk_header(hunk: dict) -> str:
    header = "@@ -%d,%d +%d,%d @@" % (hunk["old_start"], hunk["old_count"], hunk["new_start"], hunk["new_count"])
    section = clean(hunk.get("section") or "", True)
    return header + (" " + section if section else "")


def _diff_snippet(root: dict, file_diff: dict, context: int):
    """The fenced ``diff`` block around the anchored rows, or None when the anchor is not in the hunks."""
    anchor = root["anchor"]
    key = "o" if anchor["side"] == "old" else "n"
    start, end = anchor.get("start_line") or anchor["line"], anchor["line"]
    flat = _flat_rows(file_diff)
    anchored = [k for k, (_, row) in enumerate(flat) if row[key] is not None and start <= row[key] <= end]
    if not anchored:
        return None
    lo, hi = max(0, anchored[0] - context), min(len(flat), anchored[-1] + 1 + context)
    width = _number_width(row for _, row in flat[lo:hi])
    lines = ["```diff"]
    current_hunk = flat[lo][0]
    for k in range(lo, hi):
        hunk_index, row = flat[k]
        if hunk_index != current_hunk:
            lines.append(" " + _hunk_header(file_diff["hunks"][hunk_index]))
            current_hunk = hunk_index
        lines.append(_snippet_row(row, width, k in anchored))
    lines.append("```")
    return lines


def _number_width(rows) -> int:
    """Column width for line numbers: one blank column for the ``>`` mark plus the widest number (min 4)."""
    widest = max((len(str(number)) for row in rows for number in (row["o"], row["n"]) if number), default=0)
    return max(4, widest + 1)


def _snippet_row(row: dict, width: int, anchored: bool) -> str:
    """``<old#|blank> <new#|blank>   <marker><text>``; an anchored row carries ``>`` in its first column."""
    old = "" if row["o"] is None else str(row["o"])
    new = "" if row["n"] is None else str(row["n"])
    text = "%s %s   %s%s" % (old.rjust(width), new.rjust(width), _MARKERS.get(row["t"], " "), clean(row["s"]))
    return (">" + text[1:]) if anchored else text


def _stored_snippet(root: dict):
    """Fallback block showing the snippet captured at creation (no diff available)."""
    snippet = root.get("snippet")
    if not snippet:
        return None
    return ["```"] + clean(snippet).split("\n") + ["```"]


class _SnippetSource:
    """Memoising wrapper around the ``fetch_file_diff(commit, path)`` callback."""

    def __init__(self, fetch_file_diff):
        self._fetch = fetch_file_diff
        self._cache = {}

    def get(self, commit, path):
        if self._fetch is None or not commit or not path:
            return None
        key = (commit, path)
        if key not in self._cache:
            self._cache[key] = self._fetch(commit, path)
        return self._cache[key]


def _snippet_block(root: dict, source: _SnippetSource, context: int) -> list:
    anchor = root["anchor"]
    if anchor["kind"] != "line":
        return []
    file_diff = None if root.get("outdated") else source.get(anchor["commit"], anchor["path"])
    block = _diff_snippet(root, file_diff, context) if file_diff else None
    return block or _stored_snippet(root) or []


# --------------------------------------------------------------------------- documents

def _thread_lines(thread: dict, source: _SnippetSource, matching: set, mark: str, context: int, snippets: bool) -> list:
    root = thread["root"]
    lines = [_thread_header(thread, matching, mark)]
    if snippets:
        lines += _snippet_block(root, source, context)
    lines += _body_lines(root.get("body"))
    for reply in thread["replies"]:
        lines.append("")
        lines += _reply_lines(reply, matching, mark)
    return lines


def _threads_lines(threads: list, review: dict, source: _SnippetSource, matching: set, mark: str,
                   context: int, snippets: bool) -> list:
    """The grouped thread sections (blank-line separated) for already sorted threads."""
    commits_by_sha = {c["sha"]: c for c in review.get("commits") or []}
    lines = []
    current_group = current_sub = None
    for thread in threads:
        root = thread["root"]
        heading, sub = _group_heading(root, commits_by_sha), _sub_heading(root)
        if heading != current_group:
            lines += ["", heading]
            current_group, current_sub = heading, None
        if sub and sub != current_sub:
            lines += ["", sub]
            current_sub = sub
        lines.append("")
        lines += _thread_lines(thread, source, matching, mark, context, snippets)
    return lines


def _summary_comment_id(round_info: dict, comments: list):
    """Id of the review-level comment ``submit`` created from the round's summary, if any."""
    for comment in comments:
        if (comment.get("parent_id") is None and comment["anchor"]["kind"] == "review"
                and comment.get("round") == round_info["number"] and comment.get("author") == "user"
                and comment.get("body") == round_info.get("summary")):
            return comment["id"]
    return None


def _round_lines(review: dict, comments: list) -> list:
    lines = ["## Rounds"]
    rounds = review.get("rounds") or []
    if not rounds:
        return lines + ["- none yet"]
    for round_info in rounds:
        ids = round_info.get("comment_ids")
        if ids is None:
            ids = [c["id"] for c in comments if c.get("round") == round_info["number"]]
        parts = ["Round %d" % round_info["number"], clean(round_info.get("verdict"), True),
                 clean(round_info.get("submitted_at"), True), _plural(len(ids), "comment")]
        if round_info.get("summary"):
            parts.append('"%s"' % clean(round_info["summary"], True))
        line = "- " + " · ".join(parts)
        summary_id = _summary_comment_id(round_info, comments) if round_info.get("summary") else None
        lines.append(line + ("  [id: %s]" % summary_id if summary_id else ""))
    return lines


def _header_line(review: dict, threads: list) -> str:
    pending = sum(1 for t in threads if any(c.get("state") == "pending" for c in [t["root"]] + t["replies"]))
    unresolved = sum(1 for t in threads if not t["root"].get("resolved"))
    unanswered = sum(1 for t in threads
                     if not t["root"].get("resolved") and not t["root"].get("outdated") and t["last_author"] == "user")
    return "# Review comments — %s (%s) — %s (%d pending, %d unresolved, %d unanswered)" % (
        clean((review.get("repo") or {}).get("name"), True), _short_spec((review.get("range") or {}).get("spec")),
        _plural(len(threads), "thread"), pending, unresolved, unanswered)


def render_comments(review: dict, comments: list, fetch_file_diff=None, threads=None, matching=None,
                    mark: str = MARK, context: int = DEFAULT_CONTEXT, snippets: bool = True, **filters) -> str:
    """The ``ccr comments`` / ``ccr wait`` Markdown (section 6.3).

    ``comments`` is the full comment list (used for thread building and round
    ids).  ``filters`` are passed to :func:`select_threads`; alternatively
    ``threads`` (already selected and ordered) and ``matching`` (the ids that
    get the ``mark`` — ``★`` or e.g. ``★ new in round 2``) can be given
    directly.  ``fetch_file_diff`` is called as ``fetch_file_diff(commit, path)``
    and must return an untrimmed FileDiff or None; it is only used when
    ``snippets`` is true.
    """
    if threads is None:
        threads, selected_ids = select_threads(sort_threads(build_threads(comments), review), **filters)
        matching = selected_ids if matching is None else matching
    elif filters:
        raise TypeError("pass either pre-selected threads or filters, not both")
    source = _SnippetSource(fetch_file_diff if snippets else None)
    lines = [_header_line(review, threads), ""] + _round_lines(review, comments)
    lines += _threads_lines(threads, review, source, set(matching or ()), mark, context, snippets)
    return "\n".join(lines) + "\n"


def render_export(review: dict, comments: list, fetch_file_diff=None, exported_at=None,
                  context: int = DEFAULT_CONTEXT) -> str:
    """The ``ccr export --md`` document (section 6.2): every thread, outdated ones included."""
    rng = review.get("range") or {}
    exported_at = exported_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = "# Review — %s (%s, base %s → head %s) — exported %s" % (
        clean((review.get("repo") or {}).get("name"), True), _short_spec(rng.get("spec")),
        _short(rng.get("base")) or "root", _short(rng.get("head")), clean(exported_at, True))
    threads = sort_threads(build_threads(comments), review)
    source = _SnippetSource(fetch_file_diff)
    lines = [title, ""]
    if review.get("cover"):
        lines += ["## Cover letter", ""] + _body_lines(review["cover"]) + [""]
    lines += _round_lines(review, comments)
    lines += _threads_lines(threads, review, source, set(), MARK, context, True)
    return "\n".join(lines) + "\n"


def to_json(review: dict, comments: list, threads=None) -> dict:
    """The ``--json`` shape: review without file lists, rounds, raw comments and thread summaries."""
    if threads is None:
        threads = sort_threads(build_threads(comments), review)
    review_meta = dict(review)
    review_meta["commits"] = [{k: v for k, v in c.items() if k != "files"} for c in review.get("commits") or []]
    return {
        "review": review_meta,
        "rounds": review.get("rounds") or [],
        "comments": comments,
        "threads": [{"root": t["root"]["id"], "replies": [r["id"] for r in t["replies"]],
                     "last_author": t["last_author"], "answered": t["answered"]} for t in threads],
    }

"""Review state for ccr (SPEC.md section 4): comments, rounds and cached git data.

A :class:`ReviewStore` owns one ``sqlite3`` connection (comments, rounds and a
small ``meta`` table) guarded by a re-entrant lock, plus derived, in-memory
git data: the resolved range, the commit chain with per-commit file stats and
a cache of untrimmed CommitDiffs keyed by ``(view, ws_ignore)``.  Every
mutation bumps ``version`` (persisted in ``meta`` so a restarted server keeps
counting) and wakes the waiters blocked in :meth:`ReviewStore.wait`; every
successful :meth:`ReviewStore.load` bumps ``generation``.

Diff data is cached untrimmed and trimmed only when served (``commit_diff`` /
``compare``); anchor validation, snippet capture and re-anchoring always work
on the untrimmed, whitespace-sensitive diff.  Errors raised to callers are
:class:`StoreError` instances carrying an HTTP status (``NotFoundError`` for
404s); only :meth:`ReviewStore.load` re-raises :class:`gitx.GitError` so the
caller can keep serving the previous chain.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial

from . import __version__, gitx
from .gitx import GitError

__all__ = [
    "StoreError",
    "NotFoundError",
    "ReviewStore",
    "utcnow",
    "default_db_path",
    "COMBINED",
    "WORKTREE",
]

SCHEMA_VERSION = 1
BODY_MAX_BYTES = 64 << 10
SNIPPET_MAX_LINES = 32
SNIPPET_MAX_BYTES = 8 << 10
MAX_CONTEXT_LINE = 100000
REMAP_WINDOW = 20
UI_CONNECTED_SECONDS = 60.0
ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_LENGTH = 6
MIN_SHORT_SHA = 4
VERDICTS = ("approve", "request_changes", "comment")
AUTHORS = ("user", "claude")
ANCHOR_KINDS = ("line", "file", "commit", "review")
SIDES = ("old", "new")
COMBINED = "combined"
WORKTREE = "worktree"
COMPARE_PREFIX = "compare:"
SNIPPET_CUT_MARK = "…"
UNKNOWN_LOCATION = {"path": None, "line": None, "status": "unknown"}

_META_KEYS = ("sha", "short_sha", "kind", "parents", "is_merge", "shallow_boundary", "author",
              "author_date", "commit_date", "subject", "body")
_FILESTAT_KEYS = ("path", "old_path", "status", "score", "additions", "deletions", "binary",
                  "old_mode", "new_mode", "old_blob", "new_blob")
_ANCHOR_FIELDS = ("commit", "path", "side", "line", "start_line")
_ANCHOR_COLUMNS = {"kind": "kind", "commit": "commit_sha", "path": "path", "side": "side", "line": "line",
                   "start_line": "start_line"}
_ANCHOR_ASSIGNMENTS = ", ".join("%s = ?" % column for column in _ANCHOR_COLUMNS.values())
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, parent_id TEXT REFERENCES comments(id) ON DELETE CASCADE,
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  state TEXT NOT NULL, round INTEGER, resolved INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL, commit_sha TEXT, path TEXT, side TEXT, line INTEGER, start_line INTEGER,
  snippet TEXT NOT NULL DEFAULT '', moved_from TEXT);
CREATE TABLE IF NOT EXISTS rounds (number INTEGER PRIMARY KEY, submitted_at TEXT NOT NULL, verdict TEXT NOT NULL,
  summary TEXT NOT NULL, base TEXT, head TEXT NOT NULL, commit_shas TEXT NOT NULL);
"""


def utcnow() -> str:
    """Current UTC time as the ISO-8601 second-precision ``Z`` string used everywhere in ccr."""
    return datetime.now(timezone.utc).strftime(_TIME_FORMAT)


def _next_second(stamp: str) -> str:
    return (datetime.strptime(stamp, _TIME_FORMAT) + timedelta(seconds=1)).strftime(_TIME_FORMAT)


def default_db_path(repo: str) -> str:
    """``${CCR_SESSION_DIR:-~/.cache/ccr/sessions}/<key>.sqlite`` with ``key = sha1(realpath)[:16]``."""
    key = hashlib.sha1(os.path.realpath(repo).encode("utf-8", "surrogateescape")).hexdigest()[:16]
    session_dir = os.environ.get("CCR_SESSION_DIR") or os.path.expanduser("~/.cache/ccr/sessions")
    return os.path.join(session_dir, key + ".sqlite")


class StoreError(Exception):
    """A rejected store operation; ``status`` is the HTTP code the server should answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return self.message


class NotFoundError(StoreError, KeyError):
    """A 404: unknown comment, commit, view or path (also a ``KeyError`` for lookup-style callers)."""

    def __init__(self, message: str):
        super().__init__(message, 404)


# --------------------------------------------------------------------------- database plumbing

def _lock_file(path: str) -> int:
    """Take the exclusive advisory lock guarding a file database; the fd is held for the process lifetime."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise StoreError("db in use: %s is locked by another ccr server" % path, 409) from None
    return fd


def _create_private(path: str) -> None:
    """Create ``path`` with mode 0600 (no-op when it exists) before sqlite opens it."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", 0o700, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


def _open_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _meta_get(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _check_meta(conn: sqlite3.Connection, repo_real: str, db_force: bool) -> None:
    """Refuse databases written by a newer ccr or for another repository (unless forced)."""
    stored_schema = _meta_get(conn, "schema_version")
    if stored_schema is not None and int(stored_schema) > SCHEMA_VERSION:
        raise StoreError("db schema too new (%s > %d); upgrade ccr or use another --db" % (stored_schema, SCHEMA_VERSION))
    stored_repo = _meta_get(conn, "repo")
    if stored_repo is not None and stored_repo != repo_real and not db_force:
        raise StoreError("db was created for %s; pass --db-force to reuse" % stored_repo, 409)
    _meta_set(conn, "schema_version", str(SCHEMA_VERSION))
    _meta_set(conn, "repo", repo_real)
    conn.commit()


# --------------------------------------------------------------------------- pure helpers

MAX_TEXT_BYTES = 64 * 1024  # cap for comment bodies and the cover letter


def _validate_body(body) -> str:
    if not isinstance(body, str):
        raise StoreError("body must be a string")
    text = body.strip()
    if not text:
        raise StoreError("body must not be empty")
    if len(text.encode("utf-8")) > BODY_MAX_BYTES:
        raise StoreError("body exceeds 64 KiB")
    return text


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StoreError("%s must be a positive integer" % name)
    return value


def _is_full_sha(value) -> bool:
    return isinstance(value, str) and len(value) in (40, 64) and all(c in "0123456789abcdef" for c in value)


def _is_hex_prefix(value) -> bool:
    return isinstance(value, str) and MIN_SHORT_SHA <= len(value) < 40 and all(c in "0123456789abcdef" for c in value)


def _meta_from_diff(diff: dict) -> dict:
    """CommitMeta (with FileStat list and stats) of a pseudo-commit from its CommitDiff."""
    meta = {key: diff[key] for key in _META_KEYS}
    meta["stats"] = diff["stats"]
    meta["files"] = [{key: f[key] for key in _FILESTAT_KEYS} for f in diff["files"]]
    return meta


def _find_file(diff: dict, path: str):
    """The FileDiff whose ``path`` (then ``old_path``) equals ``path``, or None."""
    for key in ("path", "old_path"):
        for file_diff in diff["files"]:
            if file_diff[key] == path:
                return file_diff
    return None


def _side_rows(file_diff: dict, side: str) -> list:
    """``[(line_number, text)]`` of the rows that exist on ``side``, in file order."""
    key = "o" if side == "old" else "n"
    return [(row[key], row["s"]) for hunk in file_diff["hunks"] for row in hunk["lines"] if row[key] is not None]


def _cap_snippet(lines: list) -> str:
    """Join snippet lines, capped at 32 lines / 8 KiB with a trailing ``…`` line when cut."""
    cut = len(lines) > SNIPPET_MAX_LINES
    kept = list(lines[:SNIPPET_MAX_LINES - 1] if cut else lines)
    while kept and len("\n".join(kept + [SNIPPET_CUT_MARK]).encode("utf-8")) > SNIPPET_MAX_BYTES:
        kept.pop()
        cut = True
    return "\n".join(kept + ([SNIPPET_CUT_MARK] if cut else []))


def _capture_snippet(file_diff: dict, side: str, start: int, end: int):
    """Snippet text for lines ``start..end`` on ``side``, or None when an endpoint is not in the hunks."""
    rows = _side_rows(file_diff, side)
    numbers = {number for number, _ in rows}
    if start not in numbers or end not in numbers:
        return None
    return _cap_snippet([text for number, text in rows if start <= number <= end])


def _expected_lines(snippet: str, length: int) -> list:
    """Snippet lines a re-anchoring candidate must reproduce (a cut snippet drops its ``…`` marker)."""
    lines = snippet.split("\n")
    if len(lines) < length and lines[-1] == SNIPPET_CUT_MARK:
        lines.pop()
    return lines


def _following_match(rows: list, index: int, expected: list) -> bool:
    """True when the rows after ``rows[index]`` carry ``expected[1:]`` on consecutive line numbers."""
    first_number = rows[index][0]
    for offset, text in enumerate(expected[1:], 1):
        if index + offset >= len(rows):
            return False
        number, actual = rows[index + offset]
        if actual != text or number != first_number + offset:
            return False
    return True


def _match_line(rows: list, snippet: str, start_line: int, length: int):
    """Section 4.4 line search: the unique row matching the snippet, or the nearest one within ±20."""
    if not snippet:
        return None
    expected = _expected_lines(snippet, length)
    candidates = [k for k, (_, text) in enumerate(rows) if text == expected[0] and _following_match(rows, k, expected)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return rows[candidates[0]][0]
    nearest = min(candidates, key=lambda k: abs(rows[k][0] - start_line))
    if abs(rows[nearest][0] - start_line) <= REMAP_WINDOW:
        return rows[nearest][0]
    return None


def _anchor_params(anchor: dict) -> list:
    """SQL parameters for ``_ANCHOR_ASSIGNMENTS`` in column order."""
    return [anchor[field] for field in _ANCHOR_COLUMNS]


def _moved_from(anchor: dict) -> str:
    """The JSON ``moved_from`` record remembering where a moved comment used to be."""
    return json.dumps({"commit": anchor["commit"], "line": anchor["line"]})


def _valid_repo_path(path) -> bool:
    if not isinstance(path, str) or not path or "\0" in path or path.startswith("/"):
        return False
    return ".." not in path.split("/")


# --------------------------------------------------------------------------- the store

class ReviewStore:
    """Comments, rounds and cached git data of one review session (SPEC.md section 4)."""

    def __init__(self, repo, spec=None, n=None, worktree: bool = False, first_parent: bool = False,
                 db_path=None, db_force: bool = False):
        info = gitx.toplevel(repo)
        self.repo = info["path"]
        self.bare = info["bare"]
        self._range = gitx.resolve_range(self.repo, spec, n)
        self.worktree = bool(worktree)
        self.first_parent = bool(first_parent)

        self._lock = threading.RLock()
        self.cond = threading.Condition(self._lock)
        self._load_lock = threading.Lock()
        self.generation = 0
        self.loading = True
        self.stopping = False
        self._data = None
        self._diff_cache = {}
        self._server = {"pid": os.getpid(), "port": None, "started_at": utcnow(), "version": __version__}
        self._ui_last_seen = None
        self._ui_last_seen_mono = None
        self._open_polls = 0

        self.db_path = default_db_path(self.repo) if db_path is None else db_path
        self._lock_fd = None
        self._conn = None
        try:
            if self.db_path != ":memory:":
                self._lock_fd = _lock_file(self.db_path + ".lock")
                _create_private(self.db_path)
            self._conn = _open_connection(self.db_path)
            _check_meta(self._conn, os.path.realpath(self.repo), db_force)
        except Exception:
            self.close()
            raise
        self.version = int(_meta_get(self._conn, "version") or 0)
        self.cover = _meta_get(self._conn, "cover") or ""
        self._chain_memory = {sha: tuple(entry) for sha, entry in json.loads(_meta_get(self._conn, "chain") or "{}").items()}

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close the database and release the advisory lock (idempotent)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def stop(self) -> None:
        """Mark the store as stopping and wake every waiter."""
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def set_cover(self, text) -> str:
        """Set the cover letter (the change's description, shown above "All changes"); Markdown, ≤ 64 KiB.

        Bumps ``generation`` as well as ``version`` so open pages re-render their header.
        """
        if not isinstance(text, str):
            raise StoreError("cover must be a string")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise StoreError("cover exceeds %d bytes" % MAX_TEXT_BYTES)
        text = text.strip("\n")
        with self._mutate():
            _meta_set(self._conn, "cover", text)
            self.cover = text
            self.generation += 1
        return text

    def set_server_info(self, info: dict) -> None:
        """Record the ``server`` block of ``review()``/``state()`` (pid, port, started_at, version)."""
        with self._lock:
            self._server = dict(info)

    def touch_ui(self) -> None:
        """Record that a browser client was just seen (``ui.last_seen`` / ``ui.connected``)."""
        with self._lock:
            self._ui_last_seen = utcnow()
            self._ui_last_seen_mono = time.monotonic()

    @contextmanager
    def _mutate(self):
        """Run one mutation transactionally, then bump and persist ``version`` and wake waiters."""
        with self._lock:
            new_version = self.version + 1
            try:
                yield
                _meta_set(self._conn, "version", str(new_version))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            self.version = new_version
            self.cond.notify_all()

    # ------------------------------------------------------------------ loading

    def load(self, spec=None, n=None, worktree=None, first_parent=None) -> dict:
        """(Re)extract the chain; on success swap the data in, re-anchor comments and bump generation.

        Omitted options keep their current values; without ``spec``/``n`` the
        pinned range spec is re-resolved so ``HEAD`` moves.  A :class:`GitError`
        leaves the previous data (and version) untouched and propagates.
        """
        with self._load_lock:
            with self._lock:
                self.loading = True
                previous_range = self._range
                worktree_flag = self.worktree if worktree is None else bool(worktree)
                first_parent_flag = self.first_parent if first_parent is None else bool(first_parent)
                previous_shas = [c["sha"] for c in self._data["commits"]] if self._data else []
            try:
                fresh = self._extract(spec, n, previous_range, worktree_flag, first_parent_flag)
                with self._lock:
                    return self._install(fresh, previous_shas)
            finally:
                with self._lock:
                    self.loading = False

    def _extract(self, spec, n, previous_range, worktree: bool, first_parent: bool) -> dict:
        """All git work of a load (runs without the lock so the previous chain keeps being served)."""
        if spec is not None or n is not None:
            rng = gitx.resolve_range(self.repo, spec, n)
        else:
            rng = gitx.resolve_range(self.repo, previous_range["spec"], None)
            rng["given"] = previous_range["given"]
        commits = gitx.list_commits(self.repo, rng["base"], rng["head"], first_parent)
        if not commits and not worktree:
            raise GitError("range %s is empty" % (rng["given"] or rng["spec"]))
        stats = gitx.commit_stats(self.repo, commits)
        for commit in commits:
            commit["files"] = stats[commit["sha"]]
            commit["stats"] = gitx.stats_summary(commit["files"])
        combined = gitx.diff_range(self.repo, rng["base"], rng["head"])
        cache = {(COMBINED, False): combined}
        worktree_diff = worktree_head = None
        if worktree:
            worktree_diff = gitx.diff_worktree(self.repo)
            cache[(WORKTREE, False)] = worktree_diff
            worktree_head = next((f["old_rev"] for f in worktree_diff["files"]), None) or gitx.rev_parse(self.repo, "HEAD")
        paths = set()
        for files in [c["files"] for c in commits] + [combined["files"]] + ([worktree_diff["files"]] if worktree_diff else []):
            for f in files:
                paths.update(p for p in (f["path"], f["old_path"]) if p)
        return {
            "range": rng,
            "commits": commits,
            "by_sha": {c["sha"]: c for c in commits},
            "combined": _meta_from_diff(combined),
            "worktree": _meta_from_diff(worktree_diff) if worktree_diff else None,
            "worktree_head": worktree_head,
            "first_parent": first_parent,
            "branch": gitx.current_branch(self.repo),
            "paths": paths,
            "cache": cache,
        }

    def _install(self, fresh: dict, previous_shas: list) -> dict:
        """Re-anchor comments against the new chain, persist, and make ``fresh`` the current data.

        ``remapped`` lists the roots that found a new home (section 4.4),
        ``outdated`` every root that is (still) anchored outside the new chain.
        """
        new_shas = [c["sha"] for c in fresh["commits"]]
        listed = set(new_shas) | {COMBINED} | ({WORKTREE} if fresh["worktree"] else set())
        comments = self._all_comments()
        memory = dict(self._chain_memory)
        memory.update((c["sha"], (index, c["subject"])) for index, c in enumerate(fresh["commits"]))
        remapped, outdated = [], []
        with self._mutate():
            for root in [c for c in comments if c["parent_id"] is None]:
                commit = root["anchor"]["commit"]
                if commit is None or commit in listed:
                    continue
                moved = self._remap_root(root, fresh, memory)
                if moved is not None:
                    self._apply_remap(root, moved)
                    remapped.append(root["id"])
                else:
                    outdated.append(root["id"])
            _meta_set(self._conn, "chain", json.dumps({sha: list(entry) for sha, entry in memory.items()}))
            self._chain_memory = memory
            self._data = fresh
            self._diff_cache = fresh["cache"]
            self._range = fresh["range"]
            self.worktree = fresh["worktree"] is not None
            self.first_parent = fresh["first_parent"]
            self.generation += 1
        return {
            "remapped": remapped,
            "outdated": outdated,
            "commits_added": len(set(new_shas) - set(previous_shas)),
            "commits_removed": len(set(previous_shas) - set(new_shas)),
        }

    @staticmethod
    def _candidate_commit(old_sha: str, fresh: dict, memory: dict):
        """Section 4.4 candidate: same chain index and subject, else the unique commit with that subject."""
        remembered = memory.get(old_sha)
        if remembered is None:
            return None
        index, subject = remembered
        commits = fresh["commits"]
        if index < len(commits) and commits[index]["subject"] == subject:
            return commits[index]
        same_subject = [c for c in commits if c["subject"] == subject]
        return same_subject[0] if len(same_subject) == 1 else None

    def _remap_root(self, root: dict, fresh: dict, memory: dict):
        """New anchor fields for a would-be-outdated root, or None when it must stay outdated."""
        anchor = root["anchor"]
        candidate = self._candidate_commit(anchor["commit"], fresh, memory)
        if candidate is None:
            return None
        moved = dict(anchor, commit=candidate["sha"])
        if anchor["kind"] == "commit":
            return moved
        parent = candidate["parents"][0] if candidate["parents"] else None
        diff = self._cached_diff(fresh["cache"], (candidate["sha"], False),
                                 partial(gitx.diff_commit, self.repo, candidate["sha"], parent))
        file_diff = _find_file(diff, anchor["path"])
        if file_diff is None:
            return None
        moved["path"] = file_diff["path"]
        if anchor["kind"] == "file":
            return moved
        start = anchor["start_line"] or anchor["line"]
        length = anchor["line"] - start + 1
        new_start = _match_line(_side_rows(file_diff, anchor["side"]), root["snippet"], start, length)
        if new_start is None:
            return None
        moved["line"] = new_start + length - 1
        moved["start_line"] = new_start if anchor["start_line"] is not None else None
        return moved

    def _apply_remap(self, root: dict, moved: dict) -> None:
        self._conn.execute(
            "UPDATE comments SET %s, moved_from = ? WHERE id = ?" % _ANCHOR_ASSIGNMENTS,
            _anchor_params(moved) + [_moved_from(root["anchor"]), root["id"]])
        self._update_reply_anchors(root["id"], moved, root["snippet"])

    def _update_reply_anchors(self, root_id: str, anchor: dict, snippet: str) -> None:
        """Replies always carry their root's anchor (section 2.4) and, with it, its snippet."""
        self._conn.execute("UPDATE comments SET %s, snippet = ? WHERE parent_id = ?" % _ANCHOR_ASSIGNMENTS,
                           _anchor_params(anchor) + [snippet, root_id])

    # ------------------------------------------------------------------ read models

    def _require_data(self) -> dict:
        if self._data is None:
            raise StoreError("review is still loading" if self.loading else "review data is not available", 503)
        return self._data

    def _listed(self):
        """Non-outdated anchor commits, or None before the first successful load."""
        if self._data is None:
            return None
        listed = set(self._data["by_sha"]) | {COMBINED}
        if self._data["worktree"] is not None:
            listed.add(WORKTREE)
        return listed

    def _row_to_comment(self, row, listed) -> dict:
        anchor = {"kind": row["kind"], "commit": row["commit_sha"], "path": row["path"], "side": row["side"],
                  "line": row["line"], "start_line": row["start_line"]}
        return {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "author": row["author"],
            "body": row["body"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "state": row["state"],
            "round": row["round"],
            "resolved": bool(row["resolved"]),
            "anchor": anchor,
            "snippet": row["snippet"],
            "moved_from": json.loads(row["moved_from"]) if row["moved_from"] else None,
            "outdated": listed is not None and anchor["commit"] is not None and anchor["commit"] not in listed,
        }

    def _all_comments(self) -> list:
        listed = self._listed()
        rows = self._conn.execute("SELECT * FROM comments ORDER BY created_at, rowid").fetchall()
        return [self._row_to_comment(row, listed) for row in rows]

    def _fetch(self, comment_id) -> dict:
        row = None
        if isinstance(comment_id, str):
            row = self._conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            raise NotFoundError("comment %r not found" % (comment_id,))
        return self._row_to_comment(row, self._listed())

    def _rounds(self) -> list:
        ids_by_round = {}
        for row in self._conn.execute("SELECT id, round FROM comments WHERE round IS NOT NULL ORDER BY created_at, rowid"):
            ids_by_round.setdefault(row["round"], []).append(row["id"])
        rounds = []
        for row in self._conn.execute("SELECT * FROM rounds ORDER BY number"):
            rounds.append({
                "number": row["number"],
                "submitted_at": row["submitted_at"],
                "verdict": row["verdict"],
                "summary": row["summary"],
                "base": row["base"],
                "head": row["head"],
                "commit_shas": json.loads(row["commit_shas"]),
                "comment_ids": ids_by_round.get(row["number"], []),
            })
        return rounds

    def _round_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]

    def _counts(self) -> dict:
        comments = self._all_comments()
        roots = [c for c in comments if c["parent_id"] is None]
        return {
            "pending": sum(1 for c in comments if c["state"] == "pending"),
            "submitted": sum(1 for c in roots if c["state"] == "submitted"),
            "unresolved": sum(1 for c in roots if not c["resolved"]),
            "total": len(comments),
            "outdated": sum(1 for c in roots if c["outdated"]),
        }

    def _root_counts(self) -> dict:
        rows = self._conn.execute(
            "SELECT commit_sha, COUNT(*) AS n FROM comments WHERE parent_id IS NULL AND commit_sha IS NOT NULL GROUP BY commit_sha")
        return {row["commit_sha"]: row["n"] for row in rows}

    def _ui(self) -> dict:
        connected = self._ui_last_seen_mono is not None and time.monotonic() - self._ui_last_seen_mono < UI_CONNECTED_SECONDS
        return {"connected": connected, "last_seen": self._ui_last_seen, "open_polls": self._open_polls}

    def review(self) -> dict:
        """The section 2.6 Review document (``commits`` is empty until the first load completes)."""
        with self._lock:
            data = self._data
            commits = []
            if data is not None:
                counts = self._root_counts()
                metas = [data["combined"]] + data["commits"] + ([data["worktree"]] if data["worktree"] else [])
                commits = [dict(meta, comment_count=counts.get(meta["sha"], 0)) for meta in metas]
            return {
                "repo": {
                    "path": self.repo,
                    "name": os.path.basename(self.repo.rstrip("/")) or self.repo,
                    "branch": data["branch"] if data else None,
                    "bare": self.bare,
                },
                "range": dict(self._range, first_parent=self.first_parent),
                "options": {"worktree": self.worktree},
                "cover": self.cover,
                "commits": commits,
                "version": self.version,
                "generation": self.generation,
                "loading": self.loading,
                "now": utcnow(),
                "counts": self._counts(),
                "rounds": self._rounds(),
                "server": dict(self._server),
                "ui": self._ui(),
            }

    def state(self) -> dict:
        """The lightweight ``/api/state`` document (``commits`` counts real commits only)."""
        with self._lock:
            rounds = self._rounds()
            return {
                "version": self.version,
                "generation": self.generation,
                "loading": self.loading,
                "now": utcnow(),
                "server": dict(self._server),
                "counts": self._counts(),
                "rounds": len(rounds),
                "last_round": rounds[-1] if rounds else None,
                "commits": len(self._data["commits"]) if self._data else 0,
                "ui": self._ui(),
            }

    # ------------------------------------------------------------------ diffs

    def _cached_diff(self, cache: dict, key: tuple, produce):
        """Serve ``key`` from ``cache`` or run ``produce`` (a gitx call) and remember the result."""
        with self._lock:
            cached = cache.get(key)
            if cached is not None:
                return cached
            generation = self.generation
        try:
            diff = produce()
        except GitError as exc:
            raise StoreError(str(exc), exc.status) from exc
        with self._lock:
            if cache is not self._diff_cache or self.generation == generation:
                cache.setdefault(key, diff)
        return diff

    def _listed_sha(self, ref: str):
        """Full sha of a listed commit given in full or as a unique short prefix, else None."""
        data = self._require_data()
        if ref in data["by_sha"]:
            return ref
        if not _is_hex_prefix(ref):
            return None
        matches = [sha for sha in data["by_sha"] if sha.startswith(ref)]
        if len(matches) > 1:
            raise StoreError("short sha %r is ambiguous" % ref)
        return matches[0] if matches else None

    def _resolve_view(self, ref) -> str:
        """``combined`` / ``worktree`` (when enabled) / a listed sha (short allowed) → canonical name."""
        data = self._require_data()
        if not isinstance(ref, str) or not ref:
            raise StoreError("commit is required")
        if ref == COMBINED:
            return COMBINED
        if ref == WORKTREE:
            if data["worktree"] is None:
                raise NotFoundError("the worktree view is not enabled (start with --worktree)")
            return WORKTREE
        if ref.startswith(COMPARE_PREFIX):
            raise StoreError("compare views are not commentable")
        sha = self._listed_sha(ref)
        if sha is None:
            raise NotFoundError("unknown commit %r" % ref)
        return sha

    def _resolve_commit_ref(self, ref) -> str:
        """Section 4.1 commit resolution: views, listed shas, or any git rev naming a listed commit."""
        try:
            return self._resolve_view(ref)
        except NotFoundError:
            if ref == WORKTREE:
                raise
        try:
            sha = gitx.rev_parse(self.repo, ref)
        except GitError as exc:
            if exc.status == 404:
                raise NotFoundError("unknown commit %r" % ref) from None
            raise StoreError(str(exc), exc.status) from exc
        if sha not in self._require_data()["by_sha"]:
            raise NotFoundError("commit %s is not in the review" % sha[:gitx.SHORT_SHA_LEN])
        return sha

    def _view_diff(self, view: str, ws_ignore: bool) -> dict:
        """Untrimmed CommitDiff of a resolved view, from the cache when possible."""
        with self._lock:
            data = self._require_data()
            base, head = data["range"]["base"], data["range"]["head"]
            meta = data["by_sha"].get(view)
        if view == COMBINED:
            produce = partial(gitx.diff_range, self.repo, base, head, ws_ignore)
        elif view == WORKTREE:
            produce = partial(gitx.diff_worktree, self.repo, ws_ignore)
        else:
            parent = meta["parents"][0] if meta["parents"] else None
            produce = partial(gitx.diff_commit, self.repo, view, parent, ws_ignore)
        return self._cached_diff(self._diff_cache, (view, bool(ws_ignore)), produce)

    def commit_diff(self, sha, full: bool = False, ws_ignore: bool = False) -> dict:
        """Trimmed CommitDiff of a listed commit (full or short sha), ``combined`` or ``worktree``."""
        with self._lock:
            view = self._resolve_view(sha)
            count = self._root_counts().get(view, 0)
        diff = self._view_diff(view, ws_ignore)
        return dict(gitx.trim_commit_diff(diff, full), comment_count=count)

    def file_diff(self, sha, path, ws_ignore: bool = False) -> dict:
        """One untrimmed FileDiff of a view, looked up by ``path`` then ``old_path``."""
        with self._lock:
            view = self._resolve_view(sha)
        file_diff = _find_file(self._view_diff(view, ws_ignore), path)
        if file_diff is None:
            raise NotFoundError("path %r is not part of %s" % (path, view[:gitx.SHORT_SHA_LEN]))
        return file_diff

    def _compare_revs(self) -> set:
        data = self._require_data()
        revs = set(data["by_sha"]) | {data["range"]["head"]}
        revs.update(c["parents"][0] for c in data["commits"] if c["parents"])
        if data["range"]["base"]:
            revs.add(data["range"]["base"])
        return revs

    def compare(self, base, head, ws_ignore: bool = False, full: bool = False) -> dict:
        """CommitDiff of ``base..head`` (``base`` None = empty tree) as a read-only ``compare`` view."""
        with self._lock:
            allowed = self._compare_revs()
            if not _is_full_sha(head) or head not in allowed:
                raise StoreError("compare head must be a listed commit or a listed commit's parent")
            if base is not None and (not _is_full_sha(base) or base not in allowed):
                raise StoreError("compare base must be empty, a listed commit or a listed commit's parent")
        left = base if base is not None else gitx.empty_tree(self.repo)
        name = "%s%s..%s" % (COMPARE_PREFIX, left[:gitx.SHORT_SHA_LEN], head[:gitx.SHORT_SHA_LEN])
        diff = self._cached_diff(self._diff_cache, (name, bool(ws_ignore)),
                                 partial(gitx.diff_range, self.repo, base, head, ws_ignore))
        return dict(gitx.trim_commit_diff(diff, full), sha=name, short_sha=name, kind="compare",
                    subject="Compare %s" % name[len(COMPARE_PREFIX):], comment_count=0)

    def _file_revs(self) -> set:
        data = self._require_data()
        revs = self._compare_revs()
        if data["worktree"] is not None:
            revs.update((WORKTREE, data["worktree_head"]))
        return revs

    def file(self, rev, path) -> dict:
        """Full text of ``path`` at ``rev`` (section 5.1 checks: rev and path must belong to the review)."""
        with self._lock:
            data = self._require_data()
            if not isinstance(rev, str) or rev not in self._file_revs():
                raise StoreError("rev must be 'worktree' or a full sha that appears in the review")
            if not _valid_repo_path(path) or path not in data["paths"]:
                raise StoreError("path must be a file of the review")
        try:
            result = gitx.show_file(self.repo, rev, path)
        except GitError as exc:
            raise StoreError(str(exc), exc.status) from exc
        return dict(result, rev=rev, path=path)

    # ------------------------------------------------------------------ anchors

    def _anchor_file(self, commit: str, path) -> dict:
        if not isinstance(path, str) or not path:
            raise StoreError("anchor.path is required")
        file_diff = _find_file(self._view_diff(commit, False), path)
        if file_diff is None:
            raise NotFoundError("path %r is not part of %s" % (path, commit[:gitx.SHORT_SHA_LEN]))
        return file_diff

    @staticmethod
    def _require_null(fields: dict, names: tuple, kind: str) -> None:
        for name in names:
            if fields[name] is not None:
                raise StoreError("anchor.%s must be null for kind=%s" % (name, kind))

    def _validate_anchor(self, raw) -> tuple:
        """Normalise and validate an anchor (section 4.1); returns ``(anchor, snippet)``."""
        if not isinstance(raw, dict):
            raise StoreError("anchor must be an object")
        kind = raw.get("kind")
        if kind not in ANCHOR_KINDS:
            raise StoreError("anchor.kind must be one of line, file, commit, review")
        fields = {name: raw.get(name) for name in _ANCHOR_FIELDS}
        anchor = {"kind": kind, "commit": None, "path": None, "side": None, "line": None, "start_line": None}
        if kind == "review":
            self._require_null(fields, _ANCHOR_FIELDS, kind)
            return anchor, ""
        if fields["commit"] is None:
            raise StoreError("anchor.commit is required for kind=%s" % kind)
        anchor["commit"] = self._resolve_commit_ref(fields["commit"])
        if kind == "commit":
            self._require_null(fields, ("path", "side", "line", "start_line"), kind)
            return anchor, ""
        file_diff = self._anchor_file(anchor["commit"], fields["path"])
        anchor["path"] = file_diff["path"]
        if kind == "file":
            self._require_null(fields, ("side", "line", "start_line"), kind)
            return anchor, ""
        if fields["side"] not in SIDES:
            raise StoreError("anchor.side must be 'old' or 'new'")
        line = _positive_int(fields["line"], "anchor.line")
        start = None if fields["start_line"] is None else _positive_int(fields["start_line"], "anchor.start_line")
        if start is not None and start >= line:
            raise StoreError("anchor.start_line must be less than anchor.line")
        anchor.update(side=fields["side"], line=line, start_line=start)
        snippet = _capture_snippet(file_diff, fields["side"], start or line, line)
        if snippet is None:
            if line > MAX_CONTEXT_LINE:
                raise StoreError("anchor.line must be between 1 and %d" % MAX_CONTEXT_LINE)
            snippet = ""
        return anchor, snippet

    # ------------------------------------------------------------------ comments

    def _new_id(self) -> str:
        while True:
            candidate = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))
            if self._conn.execute("SELECT 1 FROM comments WHERE id = ?", (candidate,)).fetchone() is None:
                return candidate

    def _insert_comment(self, comment_id, parent_id, author, body, state, round_number, anchor, snippet) -> None:
        now = utcnow()
        self._conn.execute(
            "INSERT INTO comments (id, parent_id, author, body, created_at, updated_at, state, round, resolved,"
            " kind, commit_sha, path, side, line, start_line, snippet, moved_from)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (comment_id, parent_id, author, body, now, now, state, round_number, anchor["kind"], anchor["commit"],
             anchor["path"], anchor["side"], anchor["line"], anchor["start_line"], snippet))

    def add_comment(self, body, anchor=None, author: str = "user", parent_id=None) -> dict:
        """Create a root comment (validated anchor) or a reply (anchor copied from the root)."""
        text = _validate_body(body)
        if author not in AUTHORS:
            raise StoreError("author must be 'user' or 'claude'")
        with self._lock:
            self._require_data()
            if parent_id is not None:
                root = self._fetch(parent_id)
                if root["parent_id"] is not None:
                    raise StoreError("parent %s is a reply; replies must reference a root comment" % parent_id)
                anchor_fields, snippet = root["anchor"], root["snippet"]
            else:
                anchor_fields, snippet = self._validate_anchor(anchor)
            if author == "claude":
                state, round_number = "submitted", self._round_count()
            else:
                state, round_number = "pending", None
            comment_id = self._new_id()
            with self._mutate():
                self._insert_comment(comment_id, parent_id, author, text, state, round_number, anchor_fields, snippet)
            return self._fetch(comment_id)

    def edit_comment(self, comment_id, body=None, resolved=None, anchor=None) -> dict:
        """Change the body, the resolved flag (roots only) and/or the anchor (roots only; sets ``moved_from``)."""
        if body is None and resolved is None and anchor is None:
            raise StoreError("nothing to edit: pass body, resolved or anchor")
        with self._lock:
            current = self._fetch(comment_id)
            assignments, params, edited = [], [], False
            if body is not None:
                text = _validate_body(body)
                if text != current["body"]:
                    assignments.append("body = ?")
                    params.append(text)
                    edited = True
            if resolved is not None:
                if not isinstance(resolved, bool):
                    raise StoreError("resolved must be a boolean")
                if current["parent_id"] is not None:
                    raise StoreError("only root comments can be resolved")
                assignments.append("resolved = ?")
                params.append(int(resolved))
            new_anchor = snippet = None
            if anchor is not None:
                if current["parent_id"] is not None:
                    raise StoreError("replies follow their root's anchor and cannot be moved")
                new_anchor, snippet = self._validate_anchor(anchor)
                if new_anchor == current["anchor"]:
                    new_anchor = None
                else:
                    assignments += [_ANCHOR_ASSIGNMENTS, "snippet = ?", "moved_from = ?"]
                    params += _anchor_params(new_anchor) + [snippet, _moved_from(current["anchor"])]
                    edited = True
            if edited:
                # "edited" is defined as updated_at > created_at (2.4); with second precision an edit made in
                # the creation second must still become visible, hence the push to the next second.
                assignments.append("updated_at = ?")
                params.append(max(utcnow(), _next_second(current["created_at"])))
            if not assignments:
                return current
            with self._mutate():
                self._conn.execute("UPDATE comments SET %s WHERE id = ?" % ", ".join(assignments), params + [comment_id])
                if new_anchor is not None:
                    self._update_reply_anchors(comment_id, new_anchor, snippet)
            return self._fetch(comment_id)

    def delete_comment(self, comment_id, cascade: bool = False) -> None:
        """Delete a comment; a root with replies needs ``cascade`` (else 409) and takes its replies along."""
        with self._lock:
            current = self._fetch(comment_id)
            if current["parent_id"] is None:
                replies = self._conn.execute("SELECT COUNT(*) FROM comments WHERE parent_id = ?", (comment_id,)).fetchone()[0]
                if replies and not cascade:
                    raise StoreError("thread has replies", 409)
            with self._mutate():
                self._conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))

    def _locate_one(self, comment: dict, data: dict, head_sha: str) -> dict:
        """Section 4.5: where the anchored line lives at ``HEAD``."""
        anchor = comment["anchor"]
        commit, side = anchor["commit"], anchor["side"]
        if commit == WORKTREE:
            if side == "new":
                return {"path": anchor["path"], "line": anchor["line"], "status": "live"}
            from_rev = data["worktree_head"]
        elif commit == COMBINED:
            from_rev = data["range"]["head"] if side == "new" else data["range"]["base"]
        elif side == "new":
            from_rev = commit
        else:
            meta = data["by_sha"].get(commit)
            from_rev = meta["parents"][0] if meta and meta["parents"] else None
        if from_rev is None:
            return dict(UNKNOWN_LOCATION)
        try:
            return gitx.map_line(self.repo, from_rev, head_sha, anchor["path"], anchor["line"])
        except GitError:
            return dict(UNKNOWN_LOCATION)

    # ------------------------------------------------------------------ projection into another view

    def _view_revs(self, data: dict, view: str):
        """``(old_rev, new_rev)`` of a view; ``WORKTREE`` stands for the working tree, None for "no such side"."""
        if view == COMBINED:
            return data["range"]["base"], data["range"]["head"]
        if view == WORKTREE:
            return data["worktree_head"], WORKTREE
        meta = data["by_sha"].get(view)
        if meta is None:
            return None, None
        return (meta["parents"][0] if meta["parents"] else None), view

    def _map(self, from_rev, to_rev, path, line):
        try:
            return gitx.map_line(self.repo, from_rev, None if to_rev == WORKTREE else to_rev, path, line)
        except GitError:
            return None

    def _project_one(self, root: dict, data: dict, view: str, view_diff: dict):
        """Where ``root`` shows up in ``view``: its own anchor, a mapped anchor, or None (drawer only).

        Line anchors travel through ``git diff`` between the two views' revisions (so a comment written on
        a commit appears in "All changes" at the line the branch head has now); file anchors follow the
        path; commit anchors stay in their own view; review anchors belong to every view.
        """
        anchor = root["anchor"]
        kind = anchor["kind"]
        if kind == "review" or anchor["commit"] == view:
            return dict(anchor)
        if root["outdated"] or kind == "commit":
            return None
        if kind == "file":
            file_diff = _find_file(view_diff, anchor["path"])
            if file_diff is None:
                return None
            return {"kind": "file", "commit": view, "path": file_diff["path"], "side": None, "line": None, "start_line": None}
        side = anchor["side"]
        src_old, src_new = self._view_revs(data, anchor["commit"])
        dst_old, dst_new = self._view_revs(data, view)
        from_rev, to_rev = (src_new, dst_new) if side == "new" else (src_old, dst_old)
        if from_rev is None or to_rev is None or from_rev == WORKTREE:
            return None
        mapped = self._map(from_rev, to_rev, anchor["path"], anchor["line"])
        if not mapped or mapped["status"] not in ("same", "moved") or mapped["line"] is None:
            return None
        file_diff = _find_file(view_diff, mapped["path"])
        if file_diff is None or mapped["line"] not in {number for number, _ in _side_rows(file_diff, side)}:
            return None
        start = None
        if anchor.get("start_line"):
            first = self._map(from_rev, to_rev, anchor["path"], anchor["start_line"])
            if first and first["status"] in ("same", "moved") and first["path"] == mapped["path"] \
                    and first["line"] is not None and first["line"] < mapped["line"]:
                start = first["line"]
        return {"kind": "line", "commit": view, "path": file_diff["path"], "side": side, "line": mapped["line"],
                "start_line": start}

    def _project(self, comments: list, data: dict, project) -> None:
        """Attach ``view_anchor``/``projected`` to every comment for the view ``project`` (in place)."""
        with self._lock:
            view = self._resolve_view(project)
        view_diff = self._view_diff(view, False)
        anchors = {}  # root id -> view anchor (replies carry their root's anchor, so they map identically)
        for comment in comments:
            root_id = comment["parent_id"] or comment["id"]
            if root_id not in anchors:
                anchors[root_id] = self._project_one(comment, data, view, view_diff)
            view_anchor = anchors[root_id]
            comment["view_anchor"] = None if view_anchor is None else dict(view_anchor)
            comment["projected"] = view_anchor is not None and comment["anchor"]["commit"] != view \
                and comment["anchor"]["kind"] != "review"

    def _locate(self, comments: list, data: dict) -> None:
        """Attach ``head_location`` to every root line comment in ``comments`` (in place)."""
        targets = [c for c in comments if c["parent_id"] is None and c["anchor"]["kind"] == "line"]
        if not targets:
            return
        try:
            head_sha = gitx.rev_parse(self.repo, "HEAD")
        except GitError:
            head_sha = None
        for comment in targets:
            comment["head_location"] = self._locate_one(comment, data, head_sha) if head_sha else dict(UNKNOWN_LOCATION)

    def _filter_commit(self, ref) -> str:
        """Commit filter value: a resolvable commit ref, or a literal (possibly outdated) anchor commit."""
        try:
            return self._resolve_commit_ref(ref)
        except NotFoundError:
            if ref in (COMBINED, WORKTREE) or _is_full_sha(ref):
                return ref
            if _is_hex_prefix(ref):
                rows = self._conn.execute("SELECT DISTINCT commit_sha FROM comments WHERE commit_sha LIKE ?", (ref + "%",)).fetchall()
                if len(rows) == 1:
                    return rows[0]["commit_sha"]
            raise

    def list_comments(self, state=None, round=None, resolved=None, author=None, commit=None, path=None,
                      include_outdated: bool = True, outdated_only: bool = False, locate: bool = False,
                      project=None) -> list:
        """Comments in creation order matching every given filter (``resolved`` applies to the thread's root)."""
        with self._lock:
            data = self._require_data()
            commit_sha = None if commit is None else self._filter_commit(commit)
            comments = self._all_comments()
            roots = {c["id"]: c for c in comments if c["parent_id"] is None}

            def keep(comment: dict) -> bool:
                root = roots.get(comment["parent_id"], comment) if comment["parent_id"] else comment
                return not (
                    (state is not None and comment["state"] != state)
                    or (round is not None and comment["round"] != round)
                    or (resolved is not None and root["resolved"] != bool(resolved))
                    or (author is not None and comment["author"] != author)
                    or (commit_sha is not None and comment["anchor"]["commit"] != commit_sha)
                    or (path is not None and comment["anchor"]["path"] != path)
                    or (outdated_only and not comment["outdated"])
                    or (not include_outdated and comment["outdated"])
                )

            selected = [c for c in comments if keep(c)]
        if locate:
            self._locate(selected, data)
        if project is not None:
            self._project(selected, data, project)
        return selected

    # ------------------------------------------------------------------ rounds

    def submit(self, verdict, summary) -> dict:
        """Bundle every pending comment into a new round (section 2.5) and return it."""
        if verdict not in VERDICTS:
            raise StoreError("verdict must be one of approve, request_changes, comment")
        summary = "" if summary is None else summary
        if not isinstance(summary, str):
            raise StoreError("summary must be a string")
        summary = summary.strip()
        if len(summary.encode("utf-8")) > BODY_MAX_BYTES:
            raise StoreError("summary exceeds 64 KiB")
        with self._lock:
            data = self._require_data()
            pending = self._conn.execute("SELECT COUNT(*) FROM comments WHERE state = 'pending'").fetchone()[0]
            if not pending and not summary and verdict != "approve":
                raise StoreError("nothing to submit: no pending comments and no summary")
            number = self._round_count() + 1
            with self._mutate():
                self._conn.execute(
                    "INSERT INTO rounds (number, submitted_at, verdict, summary, base, head, commit_shas) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (number, utcnow(), verdict, summary, data["range"]["base"], data["range"]["head"],
                     json.dumps([c["sha"] for c in data["commits"]])))
                self._conn.execute("UPDATE comments SET state = 'submitted', round = ? WHERE state = 'pending'", (number,))
                if summary:
                    review_anchor = {"kind": "review", "commit": None, "path": None, "side": None, "line": None, "start_line": None}
                    self._insert_comment(self._new_id(), None, "user", summary, "submitted", number, review_anchor, "")
            return next(r for r in self._rounds() if r["number"] == number)

    # ------------------------------------------------------------------ waiting

    def wait(self, since_version: int, timeout: float) -> dict:
        """Block until ``version`` differs from ``since_version`` or the store stops; ``state()`` + ``changed``."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self.cond:
            self._open_polls += 1
            try:
                changed = self.cond.wait_for(
                    lambda: self.version != since_version or self.stopping,
                    timeout=max(0.0, deadline - time.monotonic()))
            finally:
                self._open_polls -= 1
            result = self.state()
        result["changed"] = bool(changed)
        return result

"""Review state for ccr (SPEC.md section 4): comments, rounds and cached git data.

A :class:`ReviewStore` owns one ``sqlite3`` connection (reviews, comments,
rounds and a small ``meta`` table) guarded by a re-entrant lock, plus derived,
in-memory git data: the resolved range, the commit chain with per-commit file
stats and a cache of untrimmed CommitDiffs keyed by ``(view, ws_ignore)``.
The database belongs to the repository, but every comment and round belongs to
one *review* of it (section 4.6): on open the store adopts the stored review
only when the range still shares commits with it, so a review of another change
never inherits its comments, its rounds or its round numbering.  Every
mutation bumps ``version`` (persisted in ``meta`` so a restarted server keeps
counting) and wakes the waiters blocked in :meth:`ReviewStore.wait`; every
successful :meth:`ReviewStore.load` bumps ``generation``.

A review linked to a GitHub pull request (PR mode, section 10) also records,
per GitHub comment, whether it has been posted to the user's pending review
and where; :meth:`ReviewStore.github_target` says where GitHub will anchor one.

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
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial

from . import __version__, github, gitx
from .gitx import GitError
from .render import clean

__all__ = [
    "StoreError",
    "NotFoundError",
    "ReviewStore",
    "SCHEMA_VERSION",
    "utcnow",
    "default_db_path",
    "COMBINED",
    "WORKTREE",
]

SCHEMA_VERSION = 3
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
NO_PR = "this review is not linked to a GitHub pull request (start ccr with --pr URL)"
GITHUB_LOCAL = {"status": "local"}
GITHUB_AUTHOR = "github"  # the author of comments mirrored from the pull request's discussion (10.5)
_GITHUB_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_GITHUB_RECORD_KEYS = ("url", "comment_id", "node_id", "thread_id", "review_id", "path", "subject_type", "line", "side",
                       "start_line", "start_side", "commit")

_META_KEYS = ("sha", "short_sha", "kind", "parents", "is_merge", "shallow_boundary", "author",
              "author_date", "commit_date", "subject", "body")
_FILESTAT_KEYS = ("path", "old_path", "status", "score", "additions", "deletions", "binary",
                  "old_mode", "new_mode", "old_blob", "new_blob")
_ANCHOR_FIELDS = ("commit", "path", "side", "line", "start_line")
_ANCHOR_COLUMNS = {"kind": "kind", "commit": "commit_sha", "path": "path", "side": "side", "line": "line",
                   "start_line": "start_line"}
_ANCHOR_ASSIGNMENTS = ", ".join("%s = ?" % column for column in _ANCHOR_COLUMNS.values())
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_ROUNDS_DDL = """CREATE TABLE IF NOT EXISTS rounds (review INTEGER NOT NULL DEFAULT 1, number INTEGER NOT NULL,
  submitted_at TEXT NOT NULL, verdict TEXT NOT NULL, summary TEXT NOT NULL, base TEXT, head TEXT NOT NULL,
  commit_shas TEXT NOT NULL, PRIMARY KEY (review, number));"""

_ROUND_COLUMNS = "number, submitted_at, verdict, summary, base, head, commit_shas"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
  range_spec TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '', chain TEXT NOT NULL DEFAULT '{}',
  pr TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, parent_id TEXT REFERENCES comments(id) ON DELETE CASCADE,
  review INTEGER NOT NULL DEFAULT 1,
  author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  state TEXT NOT NULL, round INTEGER, resolved INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL, commit_sha TEXT, path TEXT, side TEXT, line INTEGER, start_line INTEGER,
  snippet TEXT NOT NULL DEFAULT '', moved_from TEXT, github TEXT);
""" + _ROUNDS_DDL + "\n"


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


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _migrate(conn: sqlite3.Connection) -> None:
    """Schema 1 (one nameless review per repository) → 2 (reviews are rows and every comment carries one) → 3.

    Everything a schema-1 database holds belonged to a single review, so it becomes review 1; the cover
    letter and the remembered chain move out of ``meta`` into its row.  Schema 3 adds the pull request a
    review is linked to and the GitHub state of a comment (PR mode), both empty for existing rows.
    """
    if "pr" not in _columns(conn, "reviews"):
        conn.execute("ALTER TABLE reviews ADD COLUMN pr TEXT NOT NULL DEFAULT ''")
    if "github" not in _columns(conn, "comments"):
        conn.execute("ALTER TABLE comments ADD COLUMN github TEXT")
    if "review" not in _columns(conn, "comments"):
        conn.execute("ALTER TABLE comments ADD COLUMN review INTEGER NOT NULL DEFAULT 1")
    if "review" not in _columns(conn, "rounds"):
        conn.execute("ALTER TABLE rounds RENAME TO rounds_v1")
        conn.execute(_ROUNDS_DDL)
        conn.execute("INSERT INTO rounds (review, %s) SELECT 1, %s FROM rounds_v1" % (_ROUND_COLUMNS, _ROUND_COLUMNS))
        conn.execute("DROP TABLE rounds_v1")
    if conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0:
        cover, chain = _meta_get(conn, "cover") or "", _meta_get(conn, "chain") or "{}"
        started = conn.execute("SELECT MIN(created_at) FROM comments").fetchone()[0]
        if started or cover or chain != "{}":
            conn.execute("INSERT INTO reviews (id, started_at, cover, chain) VALUES (1, ?, ?, ?)",
                         (started or utcnow(), cover, chain))
        conn.execute("DELETE FROM meta WHERE key IN ('cover', 'chain')")


def _check_meta(conn: sqlite3.Connection, repo_real: str, db_force: bool) -> None:
    """Refuse databases written by a newer ccr or for another repository (unless forced); migrate older ones."""
    stored_schema = _meta_get(conn, "schema_version")
    if stored_schema is not None and int(stored_schema) > SCHEMA_VERSION:
        raise StoreError("db schema too new (%s > %d); upgrade ccr or use another --db" % (stored_schema, SCHEMA_VERSION))
    stored_repo = _meta_get(conn, "repo")
    if stored_repo is not None and stored_repo != repo_real and not db_force:
        raise StoreError("db was created for %s; pass --db-force to reuse" % stored_repo, 409)
    _migrate(conn)
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


def _posted_message(comment: dict) -> str:
    return "comment %s is posted to your pending GitHub review (%s); its place stays, only its text can change" % (
        comment["id"], comment["github"].get("url") or "no url")


def _published_message(comment: dict) -> str:
    return "comment %s is published on GitHub with your submitted review (%s); change it there" % (
        comment["id"], comment["github"].get("url") or "no url")


def _remote_message(comment: dict) -> str:
    return "comment %s comes from the pull request's discussion on GitHub (%s); it changes there" % (
        comment["id"], (comment["github"] or {}).get("url") or "no url")


def _github_time(value) -> str:
    """A GitHub timestamp as ccr keeps times (they already are ``YYYY-mm-ddTHH:MM:SSZ``), else now."""
    return value if isinstance(value, str) and _GITHUB_TIME_RE.match(value) else utcnow()


def _clip(text) -> str:
    """A GitHub body as a ccr body: never empty, at most 64 KiB."""
    text = (text if isinstance(text, str) else "").strip() or "(empty)"
    while len(text.encode("utf-8")) > BODY_MAX_BYTES:
        text = text[:len(text) * 9 // 10]
    return text


def _news(comment: dict, change: str) -> dict:
    """What a sync tells about a mirrored comment GitHub added, edited or deleted since the last sync."""
    github = comment["github"]
    return {"id": comment["id"], "change": change, "login": github.get("login"), "url": github.get("url"),
            "parent_id": comment["parent_id"], "anchor": comment["anchor"], "body": comment["body"]}


def _valid_repo_path(path) -> bool:
    if not isinstance(path, str) or not path or "\0" in path or path.startswith("/"):
        return False
    return ".." not in path.split("/")


# --------------------------------------------------------------------------- the store

class ReviewStore:
    """Comments, rounds and cached git data of one review session (SPEC.md section 4).

    ``review_id`` is the review the store serves; ``resumed`` says it was opened with comments or
    rounds already in it, and ``previous_review`` describes the unrelated review that was left behind
    in the database (both are meant for the ``ccr start`` / ``ccr serve`` banner).
    """

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
        self.version = 0
        self.review_id = None
        self.review_started_at = None
        self.resumed = False
        self.previous_review = None
        self.cover = ""
        self.pr = None
        self._chain_memory = {}
        try:
            if self.db_path != ":memory:":
                self._lock_fd = _lock_file(self.db_path + ".lock")
                _create_private(self.db_path)
            self._conn = _open_connection(self.db_path)
            _check_meta(self._conn, os.path.realpath(self.repo), db_force)
            self.version = int(_meta_get(self._conn, "version") or 0)
            self._select_review()
        except Exception:
            self.close()
            raise

    # ------------------------------------------------------------------ reviews

    def _review_size(self, review_id: int) -> dict:
        return {
            "comments": self._conn.execute("SELECT COUNT(*) FROM comments WHERE review = ?", (review_id,)).fetchone()[0],
            "rounds": self._conn.execute("SELECT COUNT(*) FROM rounds WHERE review = ?", (review_id,)).fetchone()[0],
        }

    def _listed_chain(self):
        """``(shas, subjects)`` of the range this store was opened with, or None when there is nothing to compare."""
        try:
            commits = gitx.list_commits(self.repo, self._range["base"], self._range["head"], self.first_parent)
        except GitError:
            return None
        if not commits:
            return None
        return {c["sha"] for c in commits}, {c["subject"] for c in commits}

    @staticmethod
    def _continues(row, chain) -> bool:
        """True when ``chain`` is the change ``row`` was reviewing (section 4.6).

        A review remembers every ``sha → (index, subject)`` it has ever listed, so an amend, a rebase or
        a widened range still shares a sha or a subject with it while a review of another change shares
        nothing.  Without a chain to compare (an unlistable or commit-less range) every review continues:
        that must never be the reason one is set aside.
        """
        if chain is None:
            return True
        memory = json.loads(row["chain"] or "{}")
        if not memory:
            return False
        shas, subjects = chain
        return bool(shas & set(memory)) or bool(subjects & {entry[1] for entry in memory.values()})

    def _adopt_review(self, row, resumed: bool) -> None:
        self.review_id, self.review_started_at, self.resumed = row["id"], row["started_at"], resumed
        self.cover = row["cover"] or ""
        self.pr = json.loads(row["pr"]) if row["pr"] else None
        self._chain_memory = {sha: tuple(entry) for sha, entry in json.loads(row["chain"] or "{}").items()}

    def _select_review(self) -> None:
        """Adopt the stored review of this change (newest first), else open a new one.

        One database serves one repository (``default_db_path``), so without this every review of that
        repository would inherit the previous one's comments, rounds and "outdated" threads.
        """
        chain = None
        newest_used = None
        for row in self._conn.execute("SELECT * FROM reviews ORDER BY id DESC"):
            size = self._review_size(row["id"])
            if not (size["comments"] or size["rounds"] or row["cover"]):
                self._adopt_review(row, False)          # nothing in it to inherit or to lose
                break
            if chain is None:
                chain = self._listed_chain()
            if self._continues(row, chain):
                self._adopt_review(row, True)
                break
            if newest_used is None:
                newest_used = dict(size, id=row["id"], started_at=row["started_at"], range=row["range_spec"] or None)
        else:
            self.previous_review = newest_used
            self.review_started_at = utcnow()
            self.review_id = self._conn.execute(
                "INSERT INTO reviews (started_at) VALUES (?)", (self.review_started_at,)).lastrowid
        self._conn.execute("UPDATE reviews SET range_spec = ? WHERE id = ?",
                           (self._range.get("given") or self._range["spec"] or "", self.review_id))
        self._conn.commit()

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
            self._conn.execute("UPDATE reviews SET cover = ? WHERE id = ?", (text, self.review_id))
            self.cover = text
            self.generation += 1
        return text

    def set_pr(self, reference) -> dict:
        """Link the review to a GitHub pull request (a URL or ``OWNER/REPO#N``), which turns on PR mode.

        Bumps ``generation`` as well as ``version`` so open pages re-render for the mode.
        """
        try:
            pr = github.parse_pr(reference)
        except ValueError as exc:
            raise StoreError(str(exc)) from None
        if self.pr and self.pr["url"] == pr["url"]:  # relinking the same pull request keeps its sync times
            pr.update((key, self.pr[key]) for key in ("synced_at", "first_synced_at") if key in self.pr)
        with self._mutate():
            self._conn.execute("UPDATE reviews SET pr = ? WHERE id = ?", (json.dumps(pr), self.review_id))
            self.pr = pr
            self.generation += 1
        return dict(pr)

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
            self._conn.execute("UPDATE reviews SET chain = ? WHERE id = ?",
                               (json.dumps({sha: list(entry) for sha, entry in memory.items()}), self.review_id))
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
            "github": json.loads(row["github"]) if row["github"] else None,
        }

    def _all_comments(self) -> list:
        listed = self._listed()
        rows = self._conn.execute("SELECT * FROM comments WHERE review = ? ORDER BY created_at, rowid",
                                  (self.review_id,)).fetchall()
        return [self._row_to_comment(row, listed) for row in rows]

    def _fetch(self, comment_id) -> dict:
        row = None
        if isinstance(comment_id, str):
            row = self._conn.execute("SELECT * FROM comments WHERE id = ? AND review = ?",
                                     (comment_id, self.review_id)).fetchone()
        if row is None:
            raise NotFoundError("comment %r not found" % (comment_id,))
        return self._row_to_comment(row, self._listed())

    def _rounds(self) -> list:
        ids_by_round = {}
        for row in self._conn.execute("SELECT id, round FROM comments WHERE review = ? AND round IS NOT NULL"
                                      " ORDER BY created_at, rowid", (self.review_id,)):
            ids_by_round.setdefault(row["round"], []).append(row["id"])
        rounds = []
        for row in self._conn.execute("SELECT * FROM rounds WHERE review = ? ORDER BY number", (self.review_id,)):
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
        return self._conn.execute("SELECT COUNT(*) FROM rounds WHERE review = ?", (self.review_id,)).fetchone()[0]

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
            "SELECT commit_sha, COUNT(*) AS n FROM comments WHERE review = ? AND parent_id IS NULL"
            " AND commit_sha IS NOT NULL GROUP BY commit_sha", (self.review_id,))
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
                "review": {"id": self.review_id, "started_at": self.review_started_at,
                           "resumed": self.resumed, "previous": self.previous_review},
                "options": {"worktree": self.worktree},
                "cover": self.cover,
                "pr": dict(self.pr) if self.pr else None,
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
                "pr_synced_at": (self.pr or {}).get("synced_at"),
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

    def _insert_comment(self, comment_id, parent_id, author, body, state, round_number, anchor, snippet,
                        github_state=None) -> None:
        now = utcnow()
        self._conn.execute(
            "INSERT INTO comments (id, parent_id, review, author, body, created_at, updated_at, state, round, resolved,"
            " kind, commit_sha, path, side, line, start_line, snippet, moved_from, github)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (comment_id, parent_id, self.review_id, author, body, now, now, state, round_number, anchor["kind"],
             anchor["commit"], anchor["path"], anchor["side"], anchor["line"], anchor["start_line"], snippet,
             json.dumps(github_state) if github_state else None))

    def _check_github(self, author: str, parent_id, anchor: dict, snippet: str, comment_id=None) -> None:
        """A GitHub comment is the user's: a root GitHub can anchor (10.2), or a reply in a thread that is or can
        become a review thread on GitHub (10.5)."""
        if author != "user":
            raise StoreError("GitHub comments are the user's to write")
        if parent_id is None:
            self._github_target({"anchor": anchor, "outdated": False, "snippet": snippet})
            return
        root = self._fetch(parent_id)
        if self._github_thread(root) is not None:
            return
        if self._github_starter(root, comment_id) is None:  # this reply starts the thread on GitHub
            self._github_target(root)

    @staticmethod
    def _thread_info(comment: dict, login=None) -> dict:
        github = comment["github"]
        return {"thread_id": github["thread_id"], "reply_to": github.get("node_id"), "url": github.get("url"),
                "login": login or github.get("login") or "you"}

    def _github_thread(self, root: dict):
        """The review thread a root is on GitHub - ``{"thread_id", "reply_to", "url", "login"}`` - or None when the
        thread is to start there (a question or the user's GitHub comment, on a line or a file); StoreError for a
        thread that cannot have one."""
        if self.pr is None:
            raise StoreError(NO_PR, 409)
        github = root.get("github") or {}
        if github.get("status") in ("remote", "posted") and github.get("thread_id") and not github.get("deleted"):
            return self._thread_info(root)
        if github.get("status") == "remote":
            raise StoreError("this thread is not a review thread on GitHub, so a reply to it stays in ccr")
        if root["anchor"]["kind"] not in ("line", "file"):
            raise StoreError("a GitHub reply goes into a thread on a line or a file; on a commit or the whole pull "
                             "request a reply stays in ccr")
        return None

    def _github_starter(self, root: dict, other_than=None):
        """The first GitHub comment of a thread with no review thread on GitHub yet - its root or a reply - other
        than ``other_than``: what starts the thread there, which later GitHub replies go into (10.5)."""
        row = self._conn.execute(
            "SELECT id FROM comments WHERE (id = ? OR parent_id = ?) AND id IS NOT ? AND author = 'user'"
            " AND github IS NOT NULL ORDER BY parent_id IS NOT NULL, created_at, rowid LIMIT 1",
            (root["id"], root["id"], other_than)).fetchone()
        return None if row is None else self._fetch(row["id"])

    def add_comment(self, body, anchor=None, author: str = "user", parent_id=None, github=False) -> dict:
        """Create a root comment (validated anchor) or a reply (anchor copied from the root).

        ``github`` makes the root a GitHub comment of PR mode: a draft for the user's pending GitHub review.
        """
        text = _validate_body(body)
        if author not in AUTHORS:
            raise StoreError("author must be 'user' or 'claude'")
        if github is not None and not isinstance(github, bool):
            raise StoreError("github must be a boolean")
        with self._lock:
            self._require_data()
            if parent_id is not None:
                root = self._fetch(parent_id)
                if root["parent_id"] is not None:
                    raise StoreError("parent %s is a reply; replies must reference a root comment" % parent_id)
                anchor_fields, snippet = root["anchor"], root["snippet"]
            else:
                anchor_fields, snippet = self._validate_anchor(anchor)
            if github:
                self._check_github(author, parent_id, anchor_fields, snippet)
            if author == "claude":
                state, round_number = "submitted", self._round_count()
            else:
                state, round_number = "pending", None
            comment_id = self._new_id()
            with self._mutate():
                self._insert_comment(comment_id, parent_id, author, text, state, round_number, anchor_fields, snippet,
                                     GITHUB_LOCAL if github else None)
            return self._fetch(comment_id)

    def edit_comment(self, comment_id, body=None, resolved=None, anchor=None, github=None) -> dict:
        """Change the body, the resolved flag (roots only), the anchor (roots only; sets ``moved_from``) and/or
        whether a root is a GitHub comment. A GitHub comment that is posted keeps its place and kind: a new body
        marks it ``edited``, for ``ccr gh-post`` to update in the pending review, until its review is submitted."""
        if body is None and resolved is None and anchor is None and github is None:
            raise StoreError("nothing to edit: pass body, resolved, anchor or github")
        if github is not None and not isinstance(github, bool):
            raise StoreError("github must be a boolean")
        with self._lock:
            current = self._fetch(comment_id)
            if current["author"] == GITHUB_AUTHOR and (body is not None or anchor is not None or github is not None):
                raise StoreError(_remote_message(current), 409)
            posted = (current["github"] or {}).get("status") == "posted"
            assignments, params, edited = [], [], False
            if body is not None:
                text = _validate_body(body)
                if text != current["body"]:
                    if posted and current["github"].get("github_state") == "SUBMITTED":
                        raise StoreError(_published_message(current), 409)
                    assignments.append("body = ?")
                    params.append(text)
                    if posted:  # the pending review still has the old text until gh-post updates it (10.1)
                        assignments.append("github = ?")
                        params.append(json.dumps(dict(current["github"], edited=True)))
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
                elif posted:
                    raise StoreError(_posted_message(current), 409)
                else:
                    assignments += [_ANCHOR_ASSIGNMENTS, "snippet = ?", "moved_from = ?"]
                    params += _anchor_params(new_anchor) + [snippet, _moved_from(current["anchor"])]
                    edited = True
            to_github = current["github"] is not None if github is None else github
            if to_github != (current["github"] is not None):
                if posted:
                    raise StoreError(_posted_message(current), 409)
                assignments.append("github = ?")
                params.append(json.dumps(GITHUB_LOCAL) if to_github else None)
                edited = True
            if to_github and not posted and (github or new_anchor is not None):
                self._check_github(current["author"], current["parent_id"], new_anchor or current["anchor"],
                                   current["snippet"] if new_anchor is None else snippet, comment_id)
            if to_github and edited and current["state"] == "submitted":
                # what the agent checked is not what would be posted any more: it is a draft again (10.1)
                assignments += ["state = 'pending'", "round = NULL"]
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
        """Delete a comment; a root with replies needs ``cascade`` (else 409) and takes its replies along, so a root
        whose thread holds comments mirrored from GitHub stays, as they do (409)."""
        with self._lock:
            current = self._fetch(comment_id)
            if current["author"] == GITHUB_AUTHOR:
                raise StoreError(_remote_message(current), 409)
            if current["parent_id"] is None:
                replies, mirrored = self._conn.execute(
                    "SELECT COUNT(*), COUNT(CASE WHEN author = ? THEN 1 END) FROM comments WHERE parent_id = ?",
                    (GITHUB_AUTHOR, comment_id)).fetchone()
                if mirrored:
                    raise StoreError("thread %s holds replies from the pull request's discussion on GitHub; they change "
                                     "there" % comment_id, 409)
                if replies and not cascade:
                    raise StoreError("thread has replies", 409)
            with self._mutate():
                self._conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))

    # ------------------------------------------------------------------ GitHub comments (PR mode)

    def _github_line(self, anchor: dict, data: dict, line: int):
        """``(path, line)`` of an anchored line in "All changes", or StoreError when it has no place there."""
        if anchor["commit"] == COMBINED:
            return anchor["path"], line
        src_old, src_new = self._view_revs(data, anchor["commit"])
        path = anchor["path"]
        if anchor["side"] == "old":  # the old side of a file renamed by the commit carries its old name
            path = (_find_file(self._view_diff(anchor["commit"], False), path) or {}).get("old_path") or path
        from_rev, to_rev = (src_new, data["range"]["head"]) if anchor["side"] == "new" else (src_old, data["range"]["base"])
        mapped = self._map(from_rev, to_rev, path, line) if from_rev else None
        if not mapped or mapped["status"] not in ("same", "moved") or mapped["line"] is None:
            raise StoreError("line %d of %s is changed again later in the pull request, so its diff has no place for "
                             "it; comment on it in All changes" % (line, anchor["path"]))
        return mapped["path"], mapped["line"]

    def _github_target(self, comment: dict) -> dict:
        """Where GitHub anchors a GitHub comment: a file, or a line range of the pull request diff (section 10.2).

        That diff is "All changes", and GitHub takes only the lines it shows (changed lines and their context),
        so a line anchored on a commit is carried there as projection carries it, and refused when it cannot be.
        """
        if self.pr is None:
            raise StoreError(NO_PR, 409)
        anchor = comment["anchor"]
        if anchor["kind"] not in ("line", "file"):
            raise StoreError("a GitHub review comment goes on a line or a file")
        if anchor["commit"] == WORKTREE:
            raise StoreError("uncommitted changes are not part of the pull request")
        if comment["outdated"]:
            raise StoreError("the comment is anchored to a commit that left the review", 409)
        data = self._require_data()
        if data["range"]["base"] is None:
            raise StoreError("the review has no base commit, so it is not the diff of a pull request", 409)
        target = {"commit": data["range"]["head"], "base": data["range"]["base"], "path": anchor["path"],
                  "subject_type": "FILE", "line": None, "side": None, "start_line": None, "start_side": None, "lines": []}
        combined = self._view_diff(COMBINED, False)
        if anchor["kind"] == "file":
            file_diff = _find_file(combined, anchor["path"])
            if file_diff is None:
                raise StoreError("%s is not part of the pull request diff" % anchor["path"])
            return dict(target, path=file_diff["path"])
        side = anchor["side"]
        path, end = self._github_line(anchor, data, anchor["line"])
        start = end
        if anchor["start_line"] is not None:
            start_path, start = self._github_line(anchor, data, anchor["start_line"])
            if start_path != path or start >= end:
                raise StoreError("the range %d-%d of %s does not stay one range in the pull request diff; comment on it "
                                 "in All changes" % (anchor["start_line"], anchor["line"], anchor["path"]))
        file_diff = _find_file(combined, path)
        key = "o" if side == "old" else "n"
        hunk_of = {} if file_diff is None else {row[key]: index for index, hunk in enumerate(file_diff["hunks"])
                                                for row in hunk["lines"] if row[key] is not None}
        if start not in hunk_of or end not in hunk_of:
            span = str(end) if start == end else "%d-%d" % (start, end)
            raise StoreError("%s:%s (%s side) is not in the pull request diff, and GitHub takes comments only on the "
                             "lines that diff shows" % (path, span, side))
        if hunk_of[start] != hunk_of[end]:
            raise StoreError("a GitHub comment range must stay within one hunk of the pull request diff")
        github_side = "RIGHT" if side == "new" else "LEFT"
        lines = [{"line": number, "text": text} for number, text in _side_rows(file_diff, side) if start <= number <= end]
        if comment.get("snippet") and _cap_snippet([row["text"] for row in lines]) != comment["snippet"]:
            raise StoreError("%s:%s (%s side) no longer reads as it did when the comment was written, so the pull "
                             "request moved under it; put the comment where it belongs again"
                             % (path, end if start == end else "%d-%d" % (start, end), side), 409)
        return dict(target, path=file_diff["path"], subject_type="LINE", line=end, side=github_side,
                    start_line=start if start < end else None, start_side=github_side if start < end else None,
                    lines=lines)

    def _github_comment(self, comment_id) -> dict:
        comment = self._fetch(comment_id)
        if comment["github"] is None or comment["author"] == GITHUB_AUTHOR:
            raise StoreError("comment %s is not a GitHub comment" % comment_id, 409)
        return comment

    def github_target(self, comment_id) -> dict:
        """A GitHub comment's body, state and pull request, plus - unless it is posted already - the place GitHub
        will anchor it at (section 10.2)."""
        with self._lock:
            comment = self._github_comment(comment_id)
            info = {"id": comment["id"], "body": comment["body"], "state": comment["state"],
                    "github": comment["github"], "pr": dict(self.pr) if self.pr else None}
            if comment["github"].get("status") == "posted":
                return info
            if comment["parent_id"] is None:
                return dict(self._github_target(comment), **info)
            data = self._require_data()
            root = self._fetch(comment["parent_id"])
            thread = self._github_thread(root)
            if thread is None:
                starter = self._github_starter(root)
                if starter["id"] == comment["id"]:  # the thread's first GitHub comment starts it on GitHub
                    return dict(self._github_target(root), **info)
                if starter["github"].get("status") != "posted":
                    raise StoreError("comment %s starts this thread on GitHub, so it is posted first" % starter["id"], 409)
                if not starter["github"].get("thread_id"):
                    raise StoreError("comment %s started this thread on GitHub, but ccr does not know the thread yet; "
                                     "run ccr gh-sync" % starter["id"], 409)
                thread = self._thread_info(starter, "you")
            return dict(info, commit=data["range"]["head"], base=data["range"]["base"], path=comment["anchor"]["path"],
                        subject_type="REPLY", line=None, side=None, start_line=None, start_side=None, lines=[],
                        thread_id=thread["thread_id"], reply_to=thread["reply_to"], thread_url=thread["url"],
                        thread_author=thread["login"])

    def record_github_update(self, comment_id, updated) -> dict:
        """Remember that a posted GitHub comment's new text is in the pending review now (``updated``: its URL)."""
        if not isinstance(updated, dict):
            raise StoreError("updated must be an object")
        with self._lock:
            comment = self._github_comment(comment_id)
            github = comment["github"]
            if github.get("status") != "posted" or not github.get("edited"):
                raise StoreError("comment %s has no edit waiting for your pending GitHub review" % comment_id, 409)
            record = {key: value for key, value in github.items() if key != "edited"}
            record["updated_at"] = utcnow()
            if isinstance(updated.get("url"), str) and updated["url"].startswith("https://"):
                record["url"] = updated["url"]
            with self._mutate():
                self._conn.execute("UPDATE comments SET github = ? WHERE id = ?", (json.dumps(record), comment_id))
            return self._fetch(comment_id)

    def record_github_post(self, comment_id, posted) -> dict:
        """Remember that a GitHub comment now lives in the user's pending review (``posted``: where and as what)."""
        if not isinstance(posted, dict):
            raise StoreError("posted must be an object")
        url = posted.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise StoreError("posted.url must be an https URL")
        record = {key: posted.get(key) for key in _GITHUB_RECORD_KEYS}
        with self._lock:
            comment = self._github_comment(comment_id)
            if comment["github"].get("status") == "posted":
                raise StoreError(_posted_message(comment), 409)
            record.update(status="posted", posted_at=utcnow())
            with self._mutate():
                self._conn.execute("UPDATE comments SET github = ? WHERE id = ?", (json.dumps(record), comment_id))
            return self._fetch(comment_id)

    # ------------------------------------------------------------------ the pull request's discussion (PR mode, 10.5)

    def _import_anchor(self, data: dict, thread: dict, pr_head) -> tuple:
        """Where a review thread from GitHub shows in ccr: ``(anchor, snippet, placement)``.

        Its lines in "All changes" while GitHub still has them there, else its file, else the whole review: the
        lines of an outdated thread belong to a version of the pull request that is gone.
        """
        review = {"kind": "review", "commit": None, "path": None, "side": None, "line": None, "start_line": None}
        file_diff = _find_file(self._view_diff(COMBINED, False), thread["path"]) if isinstance(thread.get("path"), str) else None
        fallback = (dict(review, kind="file", commit=COMBINED, path=file_diff["path"]), "", "file") if file_diff \
            else (review, "", "review")
        line, start = thread.get("line"), thread.get("start_line")
        if thread.get("subject_type") != "LINE" or thread.get("outdated") or not isinstance(line, int) or not file_diff:
            return fallback
        start = start if isinstance(start, int) and 0 < start < line else None  # GitHub repeats line as startLine
        side = "new" if thread.get("side") == "RIGHT" else "old"
        head = data["range"]["head"]
        if pr_head and pr_head != head:  # GitHub counts the lines of its head, ccr shows another
            if side == "old":  # of a base that may have moved as well
                return fallback
            ends = [self._map(pr_head, head, file_diff["path"], n) for n in (line, start or line)]
            if not all(m and m["status"] in ("same", "moved") and m["path"] == file_diff["path"] for m in ends):
                return fallback
            line, start = ends[0]["line"], ends[1]["line"] if start else None
            start = start if start is not None and start < line else None
        snippet = _capture_snippet(file_diff, side, start or line, line)
        if snippet is None:
            return fallback
        return (dict(review, kind="line", commit=COMBINED, path=file_diff["path"], side=side, line=line, start_line=start),
                snippet, "line")

    def _mirror(self, existing, parent, node: dict, viewer: str, anchor: dict, snippet: str, flags: dict,
                stats: dict) -> dict:
        """Insert or update the ccr comment that mirrors one GitHub comment or review body; returns it."""
        info = dict(flags, status="remote", node_id=node["id"], comment_id=node.get("database_id"), url=node.get("url"),
                    login=node.get("login") or "ghost", own=node.get("login") == viewer, state=node.get("state"))
        body = _clip(node.get("body"))
        created = _github_time(node.get("created_at"))
        updated = max(created, _github_time(node["edited_at"])) if node.get("edited_at") else created
        if existing is None:
            comment_id = self._new_id()
            self._conn.execute(
                "INSERT INTO comments (id, parent_id, review, author, body, created_at, updated_at, state, round, resolved,"
                " kind, commit_sha, path, side, line, start_line, snippet, moved_from, github)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (comment_id, parent["id"] if parent else None, self.review_id, GITHUB_AUTHOR, body, created, updated,
                 int(parent is None and bool(flags.get("resolved_on_github"))), anchor["kind"], anchor["commit"],
                 anchor["path"], anchor["side"], anchor["line"], anchor["start_line"], snippet, json.dumps(info)))
            stats["added"] += 1
            added = self._fetch(comment_id)
            stats["news"].append(_news(added, "added"))
            return added
        moved = parent is None and existing["anchor"] != anchor
        if not moved and (existing["body"], existing["updated_at"], existing["github"]) == (body, updated, info):
            return existing
        self._conn.execute("UPDATE comments SET body = ?, updated_at = ?, github = ? WHERE id = ?",
                           (body, updated, json.dumps(info), existing["id"]))
        if moved:
            self._conn.execute("UPDATE comments SET %s, snippet = ? WHERE id = ?" % _ANCHOR_ASSIGNMENTS,
                               _anchor_params(anchor) + [snippet, existing["id"]])
            self._update_reply_anchors(existing["id"], anchor, snippet)
        stats["updated"] += 1
        updated = self._fetch(existing["id"])
        if existing["body"] != body:
            stats["news"].append(_news(updated, "edited"))
        return updated

    def _merge_posted(self, comment: dict, extra: dict, stats: dict) -> None:
        """A comment posted from ccr learns its thread and GitHub's state of it from a sync."""
        merged = dict(comment["github"], **extra)
        if merged != comment["github"]:
            self._conn.execute("UPDATE comments SET github = ? WHERE id = ?", (json.dumps(merged), comment["id"]))
            stats["updated"] += 1

    def sync_github(self, payload) -> dict:
        """Mirror the pull request's discussion - its review threads and the bodies of its reviews - as comments by
        ``github`` (section 10.5); returns what changed.

        GitHub comments are matched by node id from one sync to the next: new ones are added, edited ones updated,
        threads placed again as GitHub moves them, and what GitHub no longer has is removed, except a root with
        replies in ccr, which stays marked deleted.  A thread whose first comment is gone from GitHub is a new thread
        in ccr.  A comment posted from ccr is not mirrored a second time: GitHub's replies to it join its ccr thread,
        also when it is a reply in ccr that started the thread on GitHub.
        A mirrored thread starts out resolved in ccr if it is resolved on GitHub; from then on that flag is the user's.
        ``news`` lists the comments GitHub added, edited or deleted since the last sync (``_news``).
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("viewer"), str) \
                or not isinstance(payload.get("threads"), list) or not isinstance(payload.get("reviews"), list):
            raise StoreError("sync takes {viewer, head, threads: [...], reviews: [...]}")
        with self._lock:
            if self.pr is None:
                raise StoreError(NO_PR, 409)
            data = self._require_data()
            comments = self._all_comments()
            mirrored = {c["github"]["node_id"]: c for c in comments
                        if c["author"] == GITHUB_AUTHOR and (c["github"] or {}).get("node_id")}
            posted = {c["github"]["comment_id"]: c for c in comments
                      if (c["github"] or {}).get("status") == "posted" and c["github"].get("comment_id") is not None}
            local_replies = {c["parent_id"] for c in comments if c["parent_id"] and c["author"] != GITHUB_AUTHOR}
            viewer, seen, now = payload["viewer"], set(), utcnow()  # seen: ids of the ccr comments GitHub still has

            def in_place(comment, parent):
                """``comment`` if it sits under ``parent`` (None: a root); a former reply can lead a thread now."""
                return comment if comment is not None and comment["parent_id"] == (parent["id"] if parent else None) \
                    else None

            stats = {"threads": 0, "reviews": 0, "added": 0, "updated": 0, "removed": 0, "news": []}
            review_anchor = {"kind": "review", "commit": None, "path": None, "side": None, "line": None, "start_line": None}
            with self._mutate():
                for thread in payload["threads"]:
                    nodes = [c for c in (thread.get("comments") or []) if isinstance(c, dict) and c.get("id")] \
                        if isinstance(thread, dict) and thread.get("id") else []
                    if not nodes:
                        continue
                    stats["threads"] += 1
                    anchor, snippet, placement = self._import_anchor(data, thread, payload.get("head"))
                    flags = {"thread_id": thread["id"], "resolved_on_github": bool(thread.get("resolved")),
                             "outdated": bool(thread.get("outdated")), "placement": placement, "path": thread.get("path"),
                             "side": thread.get("side"), "line": thread.get("line"),
                             "original_line": thread.get("original_line")}
                    first = nodes[0]
                    starter = posted.get(first.get("database_id"))
                    root = in_place(starter, None)
                    if root is not None:
                        self._merge_posted(root, {"thread_id": thread["id"], "github_state": first.get("state")}, stats)
                    elif starter is not None and self._fetch(starter["parent_id"])["author"] != GITHUB_AUTHOR:
                        # a GitHub reply that started the thread on GitHub (10.5): the thread's rest joins its ccr thread
                        self._merge_posted(starter, {"thread_id": thread["id"], "github_state": first.get("state")}, stats)
                        root = self._fetch(starter["parent_id"])
                    else:
                        root = self._mirror(in_place(mirrored.get(first["id"]), None), None, first, viewer, anchor,
                                            snippet, flags, stats)
                        seen.add(root["id"])
                    for node in nodes[1:]:
                        mine = in_place(posted.get(node.get("database_id")), root)
                        if mine is not None:
                            self._merge_posted(mine, {"github_state": node.get("state")}, stats)
                            continue
                        seen.add(self._mirror(in_place(mirrored.get(node["id"]), root), root, node, viewer,
                                              root["anchor"], root["snippet"], {}, stats)["id"])
                for review in payload["reviews"]:
                    if not isinstance(review, dict) or not review.get("id") or not (review.get("body") or "").strip():
                        continue
                    stats["reviews"] += 1
                    node = {"id": review["id"], "database_id": review.get("database_id"), "body": review["body"],
                            "url": review.get("url"), "created_at": review.get("submitted_at"), "state": "SUBMITTED",
                            "login": review.get("login")}
                    seen.add(self._mirror(in_place(mirrored.get(review["id"]), None), None, node, viewer, review_anchor,
                                          "", {"kind": "review", "review_state": review.get("state")}, stats)["id"])
                for comment in mirrored.values():
                    if comment["id"] in seen:
                        continue
                    if comment["parent_id"] is None and comment["id"] in local_replies:
                        if not comment["github"].get("deleted"):
                            self._conn.execute("UPDATE comments SET github = ? WHERE id = ?",
                                               (json.dumps(dict(comment["github"], deleted=True)), comment["id"]))
                            stats["updated"] += 1
                            stats["news"].append(_news(comment, "deleted"))
                    else:  # a root's mirrored replies may be gone with it already (ON DELETE CASCADE)
                        self._conn.execute("DELETE FROM comments WHERE id = ?", (comment["id"],))
                        stats["removed"] += 1
                        stats["news"].append(_news(comment, "deleted"))
                self.pr = dict(self.pr, synced_at=now, first_synced_at=self.pr.get("first_synced_at") or now)
                self._conn.execute("UPDATE reviews SET pr = ? WHERE id = ?", (json.dumps(self.pr), self.review_id))
            return dict(stats, synced_at=now)

    # ------------------------------------------------------------------ restoring a review from its export (6.4)

    def _restore_commit(self, ref, resolved: dict):
        """The full sha of an export's anchor commit (a view name, a full sha, or an abbreviated one)."""
        if ref is None or ref in (COMBINED, WORKTREE) or _is_full_sha(ref):
            return ref
        if ref not in resolved:
            try:
                resolved[ref] = gitx.rev_parse(self.repo, ref) if _is_hex_prefix(ref) else None
            except GitError:
                resolved[ref] = None
        if resolved[ref] is None:
            raise StoreError("commit %s of the export is not in this repository (fetch it first)" % ref, 409)
        return resolved[ref]

    def _restore_snippet(self, anchor: dict) -> str:
        """The snippet a Markdown export does not keep: from the view's diff as on creation, else the file at git."""
        if anchor["kind"] != "line":
            return ""
        commit, side, start, end = anchor["commit"], anchor["side"], anchor["start_line"] or anchor["line"], anchor["line"]
        if commit in self._listed():
            try:
                file_diff = _find_file(self._view_diff(commit, False), anchor["path"])
            except StoreError:
                file_diff = None
            snippet = _capture_snippet(file_diff, side, start, end) if file_diff else None
            if snippet is not None:
                return snippet
        data = self._require_data()
        try:
            if commit == WORKTREE:
                rev = WORKTREE if side == "new" else data["range"]["head"]
            elif commit == COMBINED:
                rev = data["range"]["head"] if side == "new" else data["range"]["base"]
            else:
                rev = commit if side == "new" else gitx.rev_parse(self.repo, commit + "^")
            rows = gitx.show_file(self.repo, rev, anchor["path"])["content"].split("\n") if rev else []
        except GitError:
            return ""
        return _cap_snippet(rows[start - 1:end])

    @staticmethod
    def _restore_row(comment, index: int) -> dict:
        """One comment of a restore payload, checked for the fields every row needs."""
        where = "comment %d of the export" % index
        if not isinstance(comment, dict) or not isinstance(comment.get("id"), str) \
                or not re.match(r"^[0-9a-z]+$", comment["id"]):
            raise StoreError("%s has no valid id" % where)
        where = "comment %s" % comment["id"]
        if comment.get("author") not in AUTHORS + (GITHUB_AUTHOR,) or not isinstance(comment.get("body"), str):
            raise StoreError("%s has no valid author and body" % where)
        if comment.get("parent_id") is not None and not isinstance(comment["parent_id"], str):
            raise StoreError("%s has an invalid parent_id" % where)
        anchor = comment.get("anchor")
        if not isinstance(anchor, dict) or anchor.get("kind") not in ANCHOR_KINDS:
            raise StoreError("%s has no valid anchor" % where)
        if comment.get("round") is not None and not isinstance(comment["round"], int):
            raise StoreError("%s has an invalid round" % where)
        if comment.get("github") is not None and not isinstance(comment["github"], dict):
            raise StoreError("%s has an invalid GitHub record" % where)
        return comment

    def _match_mirrored(self, comment: dict, candidates: list):
        """The mirrored root a Markdown export's mirrored root is now: same login and text, else also same place."""
        login, body = (comment["github"] or {}).get("login"), clean(comment["body"]).rstrip()
        found = [c for c in candidates if c["github"].get("login") == login and clean(c["body"]).rstrip() == body]
        if len(found) > 1:
            place = {key: comment["anchor"].get(key) for key in ("kind", "path", "side", "line")}
            found = [c for c in found if {key: c["anchor"][key] for key in place} == place]
        return found[0] if len(found) == 1 else None

    def _insert_restored(self, row: dict) -> None:
        a = row["anchor"]
        self._conn.execute(
            "INSERT INTO comments (id, parent_id, review, author, body, created_at, updated_at, state, round,"
            " resolved, kind, commit_sha, path, side, line, start_line, snippet, moved_from, github)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row["parent_id"], self.review_id, row["author"], row["body"], row["created_at"],
             row["updated_at"], row["state"], row["round"], row["resolved"], a["kind"], a["commit"], a["path"],
             a["side"], a["line"], a["start_line"], row["snippet"] or "",
             json.dumps(row["moved_from"]) if row["moved_from"] else None,
             json.dumps(row["github"]) if row["github"] else None))

    def restore(self, payload, dry_run: bool = False) -> dict:
        """Fill this review, which must have no comments and rounds of its own yet, from an export (section 6.4).

        ``payload`` is what :func:`ccr.restore.parse` makes of ``ccr export --json`` or ``--md``.  Comments keep their
        ids, authors, anchors, rounds, states, times, resolved flags and GitHub records; a Markdown export's snippets
        come from git again and its rounds take this review's base, head and commits.  In PR mode the pull request's
        discussion may already be mirrored (``gh-sync``): a mirrored comment of the export is that row when it has the
        same node id (JSON) or login and text (Markdown), and a mirrored copy of one of the user's posted comments is
        dropped, so the next sync joins its GitHub replies to the restored comment.  A Markdown export's mirrored
        comment that matches nothing is left to the sync unless a comment of the user or Claude replies to it; then it
        is restored, marked deleted on GitHub.  ``dry_run`` reports the same and changes nothing.
        """
        if not isinstance(payload, dict) or payload.get("source") not in ("json", "markdown") \
                or not isinstance(payload.get("comments"), list) or not isinstance(payload.get("rounds"), list):
            raise StoreError("restore takes {source: json|markdown, pr, cover, rounds: [...], comments: [...]}")
        incoming = [self._restore_row(c, index) for index, c in enumerate(payload["comments"])]
        markdown = payload["source"] == "markdown"
        with self._lock:
            data = self._require_data()
            existing = self._all_comments()
            own = sum(1 for c in existing if c["author"] != GITHUB_AUTHOR)
            if own or self._round_count():
                raise StoreError("review #%d already has %d comments and %d rounds of its own; a restore fills a review"
                                 " that has none" % (self.review_id, own, self._round_count()), 409)
            if payload.get("pr") and (self.pr is None or github.pr_label(self.pr) != payload["pr"]):
                raise StoreError("the export is of pull request %s; start ccr with --pr for it" % payload["pr"], 409)
            ids = {c["id"] for c in incoming}
            if len(ids) < len(incoming):
                raise StoreError("the export has a comment id twice")
            by_id = {c["id"]: c for c in incoming}
            for c in incoming:
                if c["parent_id"] is not None and (c["parent_id"] not in ids or by_id[c["parent_id"]]["parent_id"]):
                    raise StoreError("comment %s replies to %s, which is not a thread of the export" % (
                        c["id"], c["parent_id"]), 400)
            resolved_commits = {}
            anchors = {}
            for c in incoming:
                anchor = {key: c["anchor"].get(key) for key in ("kind",) + _ANCHOR_FIELDS}
                anchor["commit"] = self._restore_commit(anchor["commit"], resolved_commits)
                anchors[c["id"]] = anchor

            posted = {c["github"]["comment_id"] for c in incoming if c["author"] != GITHUB_AUTHOR and c["github"]
                      and c["github"].get("status") == "posted" and c["github"].get("comment_id") is not None}
            copies = {c["id"] for c in existing if c["author"] == GITHUB_AUTHOR and c["github"].get("comment_id") in posted}
            mirrored = [c for c in existing if c["author"] == GITHUB_AUTHOR
                        and c["id"] not in copies and c["parent_id"] not in copies]
            by_node = {c["github"]["node_id"]: c for c in mirrored if c["github"].get("node_id")}
            candidates = [c for c in mirrored if c["parent_id"] is None]
            replied = {c["parent_id"] for c in incoming if c["parent_id"] and c["author"] != GITHUB_AUTHOR}
            taken = {row["id"] for row in self._conn.execute("SELECT id FROM comments")}  # of every review
            new_ids, matched, skipped, inserts, resolved_flags, unmatched = {}, {}, set(), [], {}, []
            for c in sorted(incoming, key=lambda c: c["parent_id"] is not None):
                if c["author"] == GITHUB_AUTHOR:
                    if c["parent_id"] is not None and markdown and c["parent_id"] not in unmatched:
                        skipped.add(c["id"])        # a mirrored reply: the sync brings it
                        continue
                    node = (c["github"] or {}).get("node_id")
                    found = by_node.get(node) if node else \
                        self._match_mirrored(dict(c, anchor=anchors[c["id"]]), candidates) \
                        if c["parent_id"] is None else None
                    if found is not None:
                        matched[c["id"]] = found
                        if found in candidates:
                            candidates.remove(found)
                        if c["parent_id"] is None:
                            resolved_flags[found["id"]] = bool(c.get("resolved"))
                        continue
                    if markdown and c["parent_id"] is None and c["id"] not in replied:
                        skipped.add(c["id"])
                        continue
                    if markdown and c["parent_id"] is None:
                        unmatched.append(c["id"])
                if c["id"] in taken:
                    new_ids[c["id"]] = self._new_id()
                taken.add(new_ids.get(c["id"], c["id"]))
                inserts.append(c)

            def parent_of(c):
                parent = c["parent_id"]
                if parent is None:
                    return None
                return matched[parent]["id"] if parent in matched else new_ids.get(parent, parent)

            rows, outdated = [], 0
            listed = self._listed()
            position = {c["id"]: index for index, c in enumerate(incoming)}
            for c in sorted(inserts, key=lambda c: position[c["id"]]):
                parent = parent_of(c)
                if parent in {row["id"] for row in matched.values()}:
                    root = next(row for row in matched.values() if row["id"] == parent)
                    anchor, snippet = root["anchor"], root["snippet"]
                elif c["parent_id"] is not None:
                    anchor, snippet = None, None    # the root's, once it is known
                else:
                    anchor = anchors[c["id"]]
                    snippet = self._restore_snippet(anchor) if markdown or not isinstance(c.get("snippet"), str) \
                        else c["snippet"]
                    outdated += anchor["commit"] is not None and anchor["commit"] not in listed
                github_record = dict(c["github"]) if c["github"] else None
                if markdown and c["author"] == GITHUB_AUTHOR and c["id"] in unmatched:
                    github_record["deleted"] = True
                created = c.get("created_at") or self.review_started_at
                rows.append({"id": new_ids.get(c["id"], c["id"]), "parent_id": parent, "author": c["author"],
                             "body": c["body"], "created_at": created, "updated_at": c.get("updated_at") or created,
                             "state": "pending" if c.get("state") == "pending" else "submitted", "round": c.get("round"),
                             "resolved": int(bool(c.get("resolved")) and c["parent_id"] is None), "anchor": anchor,
                             "snippet": snippet, "moved_from": c.get("moved_from"), "github": github_record})
            by_row = {row["id"]: row for row in rows}
            for row in rows:
                if row["anchor"] is None:
                    root = by_row[row["parent_id"]]
                    row["anchor"], row["snippet"] = root["anchor"], root["snippet"]

            rounds = []
            for r in payload["rounds"]:
                if not isinstance(r, dict) or not isinstance(r.get("number"), int) \
                        or not isinstance(r.get("submitted_at"), str) or r.get("verdict") not in VERDICTS:
                    raise StoreError("the export has an invalid round: %r" % (r,))
                commit_shas = r.get("commit_shas") if isinstance(r.get("commit_shas"), list) \
                    else [c["sha"] for c in data["commits"]]
                if any(r["number"] == number for number, *_ in rounds):
                    raise StoreError("the export has round %d twice" % r["number"])
                rounds.append((r["number"], r["submitted_at"], r["verdict"], r.get("summary") or "",
                               r.get("base") if "base" in r else data["range"]["base"],
                               r.get("head") or data["range"]["head"], json.dumps(commit_shas)))
            cover = payload.get("cover") if isinstance(payload.get("cover"), str) else None
            report = {
                "source": payload["source"], "dry_run": bool(dry_run),
                "comments": len(rows), "threads": sum(1 for row in rows if row["parent_id"] is None),
                "rounds": len(rounds), "outdated": outdated,
                "cover": "restored" if cover and not self.cover else ("kept" if cover and cover != self.cover else None),
                "mirrored": {"matched": len(matched), "restored": sum(1 for r in rows if r["author"] == GITHUB_AUTHOR),
                             "left_to_sync": len(skipped), "unmatched": unmatched},
                "dropped_copies": len(copies), "renamed": new_ids,
            }
            if dry_run:
                return report
            with self._mutate():
                for comment_id in copies:
                    self._conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
                # in the export's order, which keeps the order of comments made in the same second; a GitHub reply
                # to the user's comment can be older than it
                self._conn.execute("PRAGMA defer_foreign_keys = ON")
                for row in rows:
                    self._insert_restored(row)
                for comment_id, flag in resolved_flags.items():
                    self._conn.execute("UPDATE comments SET resolved = ? WHERE id = ?", (int(flag), comment_id))
                for r in rounds:
                    self._conn.execute(
                        "INSERT INTO rounds (review, number, submitted_at, verdict, summary, base, head, commit_shas)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (self.review_id,) + r)
                if report["cover"] == "restored":
                    self._conn.execute("UPDATE reviews SET cover = ? WHERE id = ?", (cover, self.review_id))
                    self.cover = cover
                self.generation += 1
            return report

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
                rows = self._conn.execute("SELECT DISTINCT commit_sha FROM comments WHERE review = ? AND commit_sha LIKE ?",
                                          (self.review_id, ref + "%")).fetchall()
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
            pending = self._conn.execute("SELECT COUNT(*) FROM comments WHERE review = ? AND state = 'pending'",
                                         (self.review_id,)).fetchone()[0]
            if not pending and not summary and verdict != "approve":
                raise StoreError("nothing to submit: no pending comments and no summary")
            number = self._round_count() + 1
            with self._mutate():
                self._conn.execute(
                    "INSERT INTO rounds (review, number, submitted_at, verdict, summary, base, head, commit_shas)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.review_id, number, utcnow(), verdict, summary, data["range"]["base"], data["range"]["head"],
                     json.dumps([c["sha"] for c in data["commits"]])))
                self._conn.execute("UPDATE comments SET state = 'submitted', round = ? WHERE review = ? AND state = 'pending'",
                                   (number, self.review_id))
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

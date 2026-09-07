"""Session discovery, the background server lifecycle and the CLI's HTTP client (SPEC.md 6.1-6.2).

One review session exists per repository realpath.  It is keyed by ``sha1(realpath)[:16]`` and
described by ``<key>.json`` in the private session directory
(``${CCR_SESSION_DIR:-~/.cache/ccr/sessions}``, mode 0700) next to ``<key>.log``, ``<key>.sqlite``,
``<key>.lock`` (the ``ccr start`` serialisation lock) and Markdown exports ``<key>-<timestamp>.md``.

This module knows how to find, validate and clean up those records, how to spawn and stop the
background ``ccr serve`` process, and how to talk to a running server (:class:`Client`).  It has no
dependency on the store or the server so both the CLI and the server can import it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from . import __version__

__all__ = [
    "SessionError",
    "NoSessionError",
    "ApiError",
    "Client",
    "SessionPaths",
    "session_key",
    "session_dir",
    "paths_for",
    "write_private",
    "write_record",
    "read_record",
    "remove_record",
    "pid_alive",
    "default_port",
    "pick_port",
    "probe",
    "find_live",
    "find_session",
    "list_sessions",
    "start_lock",
    "start_background",
    "shutdown_server",
    "remove_session_files",
    "tail_lines",
]

SESSION_DIR_ENV = "CCR_SESSION_DIR"
DEFAULT_SESSION_DIR = "~/.cache/ccr/sessions"
TOKEN_ENV = "CCR_SERVE_TOKEN"
HOME_ENV = "CCR_HOME"
# ``python -c`` bootstrap that runs ``ccr`` with the package's parent directory first on sys.path (see serve_argv).
BOOTSTRAP = ("import os, runpy, sys; sys.path[0] = os.environ.pop('CCR_HOME'); "
             "runpy.run_module('ccr', run_name='__main__', alter_sys=True)")
USER_AGENT = "ccr-cli/" + __version__
HTTP_TIMEOUT = 5.0
PORT_BASE = 7700
PORT_SPAN = 300
PORT_SCAN = 20
LOCK_STALE_SECONDS = 30.0
LOCK_WAIT_SECONDS = 35.0
START_DEADLINE = 10.0
LOAD_DEADLINE = 120.0
POLL_INTERVAL = 0.1
STOP_WAIT = 5.0
LOG_TAIL_LINES = 20

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SessionError(Exception):
    """A session-level failure the CLI reports as ``ccr: <message>`` with exit code 1."""


class NoSessionError(SessionError):
    """No live session for the repository (exit code 3); stale files were already removed."""


class ApiError(Exception):
    """An HTTP error answer from the server: ``status`` and the ``{"error": …}`` message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return self.message


# --------------------------------------------------------------------------- HTTP client

class Client:
    """Minimal JSON client for one server: token header, ``ccr-cli/<version>`` agent, 5 s timeout.

    ``url`` may be the session URL including ``?t=<token>``; the token argument wins when given.
    Connection-level failures surface as :class:`urllib.error.URLError`, HTTP error statuses as
    :class:`ApiError`.
    """

    def __init__(self, url: str, token=None, timeout: float = HTTP_TIMEOUT):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise SessionError("invalid server url %r" % url)
        self.base = "%s://%s" % (parts.scheme, parts.netloc)
        if token is None:
            token = (parse_qs(parts.query).get("t") or [None])[0]
        if not token:
            raise SessionError("a session token is required (--token or CCR_TOKEN)")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, timeout=None):
        """Perform one request; returns ``(status, decoded JSON or None)``."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"X-CCR-Token": self.token, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw.decode("utf-8")) if raw else None)
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _error_message(exc)) from None
        except URLError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise URLError(exc) from None

    def get(self, path: str, timeout=None):
        return self.request("GET", path, timeout=timeout)[1]

    def post(self, path: str, body=None):
        return self.request("POST", path, body if body is not None else {})[1]

    def patch(self, path: str, body: dict):
        return self.request("PATCH", path, body)[1]

    def delete(self, path: str):
        return self.request("DELETE", path)[1]


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (ValueError, OSError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return "HTTP %d %s" % (exc.code, exc.reason)


# --------------------------------------------------------------------------- files

def session_key(repo) -> str:
    """``sha1(realpath(repo))[:16]`` — shared with ``store.default_db_path``."""
    return hashlib.sha1(os.path.realpath(repo).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def session_dir() -> str:
    """The private session directory, created 0700 and verified to be owned by us and not writable by others."""
    path = os.environ.get(SESSION_DIR_ENV) or os.path.expanduser(DEFAULT_SESSION_DIR)
    os.makedirs(path, 0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    info = os.stat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SessionError("session dir %s is not private" % path)
    return path


class SessionPaths:
    """Every file belonging to one session: ``record`` (json), ``log``, ``db`` (sqlite) and ``lock``."""

    def __init__(self, key: str, directory: str):
        self.key = key
        self.dir = directory
        self.record = os.path.join(directory, key + ".json")
        self.log = os.path.join(directory, key + ".log")
        self.db = os.path.join(directory, key + ".sqlite")
        self.lock = os.path.join(directory, key + ".lock")

    def export(self, stamp=None) -> str:
        """Path of a Markdown export: ``<dir>/<key>-<YYYYmmdd-HHMMSS>.md`` (UTC)."""
        stamp = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return os.path.join(self.dir, "%s-%s.md" % (self.key, stamp))

    def exports(self) -> list:
        prefix = self.key + "-"
        return sorted(os.path.join(self.dir, name) for name in os.listdir(self.dir)
                      if name.startswith(prefix) and name.endswith(".md"))


def paths_for(repo) -> SessionPaths:
    return SessionPaths(session_key(repo), session_dir())


def write_private(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with mode 0600 (exclusive temp file, fsync, rename)."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, ".%s.%s.tmp" % (os.path.basename(path), secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def write_record(paths: SessionPaths, record: dict) -> None:
    write_private(paths.record, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_record(path: str):
    """The session record at ``path``, or None when it is missing or unusable (a corrupt file is deleted)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _unlink(path)
        return None
    if (not isinstance(record, dict) or not isinstance(record.get("pid"), int)
            or not isinstance(record.get("url"), str) or not isinstance(record.get("token"), str)):
        _unlink(path)
        return None
    return record


def remove_record(path: str, pid=None) -> bool:
    """Delete a session record; with ``pid`` only when the record belongs to that process."""
    if pid is not None:
        record = read_record(path)
        if record is None or record.get("pid") != pid:
            return False
    return _unlink(path)


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def tail_lines(path: str, count: int = LOG_TAIL_LINES) -> list:
    """The last ``count`` lines of a text file (empty when it is missing)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return lines[-count:] if count > 0 else []


# --------------------------------------------------------------------------- processes and ports

def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmdline_is_ccr_serve(pid: int) -> bool:
    """True when ``/proc/<pid>/cmdline`` names a ``ccr … serve`` process (never signal anything else)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            parts = [p.decode("utf-8", "replace") for p in handle.read().split(b"\0")]
    except OSError:
        return False
    return any("ccr" in part for part in parts) and "serve" in parts


def default_port(key: str) -> int:
    return PORT_BASE + int(key, 16) % PORT_SPAN


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe_socket.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(key: str, explicit=None) -> int:
    """The session's port: ``explicit`` (error when busy), else the default, the next 20, then any free one."""
    if explicit is not None:
        if not port_is_free(explicit):
            raise SessionError("port %d in use" % explicit)
        return explicit
    base = default_port(key)
    for port in range(base, base + PORT_SCAN + 1):
        if port_is_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return probe_socket.getsockname()[1]


# --------------------------------------------------------------------------- discovery

def probe(record: dict):
    """``/api/state`` of the recorded server, or None when the session is stale (pid dead, unreachable, 401)."""
    if not pid_alive(record.get("pid")):
        return None
    try:
        return Client(record["url"], record["token"]).get("/api/state")
    except (URLError, SessionError):
        return None
    except ApiError as exc:
        if exc.status == 401:
            return None
        raise


def find_live(repo):
    """``(record, state)`` of the live session for ``repo``, or None (deleting a stale record)."""
    paths = paths_for(repo)
    record = read_record(paths.record)
    if record is None:
        return None
    state = probe(record)
    if state is None:
        remove_record(paths.record)
        return None
    return record, state


def find_session(repo):
    """Like :func:`find_live` but raises :class:`NoSessionError` listing the live sessions."""
    found = find_live(repo)
    if found is None:
        raise NoSessionError(no_session_message(repo))
    return found


def no_session_message(repo) -> str:
    live = ["%s → %s" % (record.get("repo"), record["url"]) for record, _ in list_sessions()]
    return "no running session for %s — use --repo PATH or run ccr start (live sessions: %s)" % (
        repo, ", ".join(live) or "none")


def list_sessions() -> list:
    """Every live session as ``(record, state)`` sorted by repository path; stale records are deleted."""
    directory = session_dir()
    result = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        record = read_record(path)
        if record is None:
            continue
        state = probe(record)
        if state is None:
            remove_record(path)
            continue
        result.append((record, state))
    result.sort(key=lambda item: item[0].get("repo") or "")
    return result


# --------------------------------------------------------------------------- background start

@contextmanager
def start_lock(paths: SessionPaths):
    """Hold ``<key>.lock`` (``O_CREAT|O_EXCL``) so concurrent ``ccr start`` runs serialise; locks older than 30 s are ignored."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(paths.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            break
        except FileExistsError:
            pass
        try:
            age = time.time() - os.stat(paths.lock).st_mtime
        except FileNotFoundError:
            continue
        if age > LOCK_STALE_SECONDS:
            _unlink(paths.lock)
            continue
        if time.monotonic() >= deadline:
            raise SessionError("another ccr start is in progress for this repository (%s)" % paths.lock)
        time.sleep(POLL_INTERVAL)
    try:
        yield
    finally:
        _unlink(paths.lock)


def serve_argv(repo: str, port: int, log_path: str, spec=None, n=None, worktree: bool = False,
               first_parent: bool = False, db=None, idle_timeout=None, cover=None) -> list:
    """Argument vector of the background ``ccr serve`` process (the token is never part of it).

    The child is started through :data:`BOOTSTRAP` rather than ``python -m ccr`` because ``-m`` puts the
    current directory first on ``sys.path``: a reviewed repository that itself contains a ``ccr/`` directory
    (this project, for one) would shadow the installed package.
    """
    argv = [sys.executable, "-c", BOOTSTRAP, "serve", "--repo", repo, "--port", str(port), "--log", log_path]
    if spec is not None:
        argv += ["--range", spec]
    if n is not None:
        argv += ["-n", str(n)]
    if worktree:
        argv.append("--worktree")
    if first_parent:
        argv.append("--first-parent")
    if db is not None:
        argv += ["--db", db]
    if idle_timeout is not None:
        argv += ["--idle-timeout", str(idle_timeout)]
    if cover is not None:
        argv += ["--cover", cover]
    return argv


def _child_env(token: str) -> dict:
    env = dict(os.environ)
    env[HOME_ENV] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env[TOKEN_ENV] = token
    return env


def _crash_error(code: int, log_path: str, prefix=None) -> SessionError:
    lines = tail_lines(log_path)
    head = prefix or "server exited with code %d" % code
    return SessionError("%s — last log lines:\n%s" % (head, "\n".join(lines) if lines else "(log is empty)"))


def _try_state(record: dict):
    try:
        return Client(record["url"], record["token"]).get("/api/state")
    except (URLError, ApiError, SessionError):
        return None


def _await_server(proc: subprocess.Popen, paths: SessionPaths, log_path: str):
    """Poll until the child wrote its session record and answers ``/api/state`` (≤ 10 s)."""
    deadline = time.monotonic() + START_DEADLINE
    while True:
        code = proc.poll()
        if code is not None:
            raise _crash_error(code, log_path)
        record = read_record(paths.record)
        if record is not None and record.get("pid") == proc.pid:
            state = _try_state(record)
            if state is not None:
                return record, state
        if time.monotonic() >= deadline:
            proc.terminate()
            try:
                code = proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            raise _crash_error(code, log_path, "server did not answer within %d s (exit code %d)" % (START_DEADLINE, code))
        time.sleep(POLL_INTERVAL)


def _await_loaded(proc: subprocess.Popen, record: dict, state: dict, log_path: str, err) -> dict:
    """Wait (≤ 120 s) for the initial extraction; prints ``ccr: extracting commits…`` once."""
    if not state.get("loading"):
        return state
    err.write("ccr: extracting commits…\n")
    err.flush()
    deadline = time.monotonic() + LOAD_DEADLINE
    while state.get("loading") and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        code = proc.poll()
        if code is not None:
            raise _crash_error(code, log_path)
        state = _try_state(record) or state
    return state


def start_background(repo: str, paths: SessionPaths, spec=None, n=None, worktree: bool = False,
                     first_parent: bool = False, port=None, db=None, log=None, idle_timeout=None,
                     cover=None, err=None):
    """Spawn ``ccr serve`` for ``repo`` (SPEC 6.2 steps 4-5) and return ``(record, state)`` once it serves.

    The token travels only through the ``CCR_SERVE_TOKEN`` environment variable; stdout and stderr of
    the child go to the (truncated, 0600) log file.  Failures raise :class:`SessionError` carrying the
    last log lines.
    """
    err = err or sys.stderr
    token = secrets.token_hex(16)
    log_path = os.path.abspath(log) if log else paths.log
    chosen_port = pick_port(paths.key, port)
    argv = serve_argv(repo, chosen_port, log_path, spec, n, worktree, first_parent, db, idle_timeout, cover)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(log_fd, 0o600)
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, cwd=repo,
                                env=_child_env(token), start_new_session=True, close_fds=True)
    finally:
        os.close(log_fd)
    record, state = _await_server(proc, paths, log_path)
    state = _await_loaded(proc, record, state, log_path, err)
    return record, state


# --------------------------------------------------------------------------- stop

def _wait_pid_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)
    return True


def shutdown_server(record: dict, client: Client) -> None:
    """SPEC 6.2 stop steps 3-5: ``POST /api/shutdown``, wait ≤ 5 s, then a guarded SIGTERM and another wait."""
    pid = record["pid"]
    try:
        client.post("/api/shutdown", {})
    except (URLError, ApiError):
        pass
    if _wait_pid_gone(pid, STOP_WAIT):
        return
    if cmdline_is_ccr_serve(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if _wait_pid_gone(pid, STOP_WAIT):
            return
    raise SessionError("server pid %d did not exit; session files were kept" % pid)


def remove_session_files(paths: SessionPaths, record: dict, keep_db: bool = False, purge: bool = False) -> None:
    """Step 6 of ``ccr stop``: drop the record, the default database (unless kept) and, with ``purge``, logs and exports."""
    _unlink(paths.record)
    if not keep_db and record.get("db") == paths.db:
        for suffix in ("", "-wal", "-shm", ".lock"):
            _unlink(paths.db + suffix)
    if purge:
        for path in [paths.log, record.get("log")] + paths.exports():
            if path:
                _unlink(path)

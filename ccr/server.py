"""HTTP server of ccr (SPEC.md section 5): routing, authentication, static files and the serve lifecycle.

A :class:`ReviewServer` (``ThreadingHTTPServer`` bound to ``127.0.0.1``) wraps one
:class:`~ccr.store.ReviewStore`.  :class:`RequestHandler` speaks HTTP/1.1 with a ``Content-Length`` on
every response, answers every error as ``{"error": …}`` JSON, enforces the section 5.1 rules (token,
Host/Origin/Referer allowlist, Sec-Fetch-Site, request-body limits, static path safety) and maps the
store's exceptions to HTTP statuses.  :func:`make_server` is the programmatic entry point used by the
tests; :func:`serve` is ``ccr serve``: umask, session record, background load, signals and cleanup.
"""

from __future__ import annotations

import errno
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, gitx, render, session
from .gitx import GitError
from .store import ReviewStore, StoreError, utcnow

__all__ = ["ReviewServer", "RequestHandler", "make_server", "serve", "content_type_for"]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_REAL = os.path.realpath(STATIC_DIR)
MAX_BODY_BYTES = 1048576
DEFAULT_POLL_SECONDS = 25.0
MAX_POLL_SECONDS = 30.0
MAX_WAITERS = 64
RETRY_AFTER_SECONDS = 5
DEFAULT_IDLE_TIMEOUT = 86400
CLI_AGENT_PREFIX = "ccr-cli/"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
JSON_TYPE = "application/json; charset=utf-8"
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": JSON_TYPE,
    ".woff2": "font/woff2",
}
DEFAULT_MIME = "application/octet-stream"
CACHE_IMMUTABLE = "max-age=31536000, immutable"
CACHE_NONE = "no-cache"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
)
HTML_HEADERS = (
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_STATES = ("pending", "submitted")
_OUTDATED_MODES = ("include", "exclude", "only")


class HttpError(Exception):
    """An error answer decided by the handler itself (``status``, message, optional connection close)."""

    def __init__(self, status: int, message: str, close: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.close = close


# --------------------------------------------------------------------------- small helpers

def content_type_for(path: str) -> str:
    """MIME type from the fixed table (section 5.1); unknown extensions are ``application/octet-stream``."""
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), DEFAULT_MIME)


def host_part(netloc: str) -> str:
    """Host of a ``host[:port]`` string, lower-cased, brackets kept for IPv6 (``[::1]:80`` → ``[::1]``)."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        end = netloc.find("]")
        return netloc[:end + 1] if end != -1 else netloc
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def _origin_allowed(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
    except ValueError:  # e.g. unbalanced IPv6 brackets — a malformed header is simply not allowed
        return False
    return parts.scheme in ("http", "https") and host_part(parts.netloc) in ALLOWED_HOSTS


def _split_target(target: str):
    """``urlsplit`` of the request target; a malformed target (``GET http://[::1x/``) is a 400, never a 500."""
    try:
        return urlsplit(target)
    except ValueError:
        raise HttpError(400, "bad request", close=True) from None


def _first(query: dict, name: str):
    values = query.get(name)
    return values[0] if values else None


def _flag(query: dict, name: str) -> bool:
    value = _first(query, name)
    return value is not None and value.strip().lower() in _TRUE_VALUES


def _int_param(query: dict, name: str, default=None):
    value = _first(query, name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise HttpError(400, "%s must be an integer" % name) from None


def _float_param(query: dict, name: str, default: float) -> float:
    value = _first(query, name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise HttpError(400, "%s must be a number" % name) from None


def _bool_param(query: dict, name: str):
    value = _first(query, name)
    if value is None or value == "":
        return None
    lowered = value.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise HttpError(400, "%s must be a boolean" % name)


def _optional(body: dict, name: str, kind, description: str):
    """``body[name]`` when present and of type ``kind`` (None allowed), else 400."""
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) and kind is not bool or not isinstance(value, kind):
        raise HttpError(400, "%s must be %s" % (name, description))
    return value


# --------------------------------------------------------------------------- the server

class ReviewServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` on 127.0.0.1 carrying the store, the token and the poll/idle bookkeeping."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, store: ReviewStore, token: str, verbose: bool = False, idle_timeout: float = 0):
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.store = store
        self.token = token
        self.verbose = verbose
        self.idle_timeout = idle_timeout
        self.max_waiters = MAX_WAITERS
        self.log_stream = sys.stderr
        self.last_activity = time.monotonic()
        self.shutdown_started = False
        self._lock = threading.Lock()
        self._waiters = 0
        self._in_flight = 0

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.port

    def request_started(self) -> None:
        with self._lock:
            self._in_flight += 1
            self.last_activity = time.monotonic()

    def request_finished(self) -> None:
        with self._lock:
            self._in_flight -= 1
            self.last_activity = time.monotonic()

    def idle_seconds(self):
        """Seconds since the last request finished, or None while a request is in flight."""
        with self._lock:
            return None if self._in_flight else time.monotonic() - self.last_activity

    def acquire_waiter(self) -> bool:
        with self._lock:
            if self._waiters >= self.max_waiters:
                return False
            self._waiters += 1
            return True

    def release_waiter(self) -> None:
        with self._lock:
            self._waiters -= 1

    def refresh_session_record(self) -> None:
        """Rewrite ``<key>.json`` so ``ccr sessions`` shows the range currently served (no-op without a record)."""
        paths, record = getattr(self, "session_paths", None), getattr(self, "session_record", None)
        if paths is None or record is None:
            return
        record["range"] = _range_label(self.store.review()["range"])
        session.write_record(paths, record)

    def begin_shutdown(self) -> None:
        """Wake every long-poll and stop ``serve_forever`` from a helper thread (idempotent)."""
        with self._lock:
            if self.shutdown_started:
                return
            self.shutdown_started = True
        self.store.stop()
        threading.Thread(target=self.shutdown, name="ccr-shutdown", daemon=True).start()

    def start_idle_watcher(self) -> None:
        """Shut down once no request has arrived for ``idle_timeout`` seconds (0 = never)."""
        if self.idle_timeout <= 0:
            return
        threading.Thread(target=self._watch_idle, name="ccr-idle", daemon=True).start()

    def _watch_idle(self) -> None:
        interval = max(0.05, min(1.0, self.idle_timeout / 4))
        while not self.shutdown_started:
            time.sleep(interval)
            idle = self.idle_seconds()
            if idle is not None and idle >= self.idle_timeout:
                self.log_stream.write("ccr: no request for %g s; shutting down\n" % self.idle_timeout)
                self.log_stream.flush()
                self.begin_shutdown()
                return


def make_server(store: ReviewStore, token: str, port: int = 0, verbose: bool = False,
                idle_timeout: float = 0) -> ReviewServer:
    """Bind a :class:`ReviewServer` (``port`` 0 = ephemeral); the caller runs ``serve_forever``."""
    return ReviewServer(port, store, token, verbose, idle_timeout)


# --------------------------------------------------------------------------- the handler

class RequestHandler(BaseHTTPRequestHandler):
    """One request: security checks, body rules, routing (section 5.2) and JSON/static responses."""

    protocol_version = "HTTP/1.1"
    timeout = 60
    server_version = "ccr/" + __version__
    sys_version = ""

    server: ReviewServer

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    # ------------------------------------------------------------------ plumbing

    def _dispatch(self) -> None:
        started = time.monotonic()
        self._status = 0
        self.server.request_started()
        try:
            self._handle()
        except HttpError as exc:
            self._send_json(exc.status, {"error": exc.message}, close=exc.close)
        except StoreError as exc:
            self._send_json(exc.status, {"error": "loading" if exc.status == 503 else exc.message})
        except GitError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            traceback.print_exc(file=self.server.log_stream)
            self._send_json(500, {"error": "internal server error"}, close=True)
        finally:
            self.server.request_finished()
            if self.server.verbose:
                path = self.path.split("?", 1)[0]
                self.log_message("%s %s %d %dms", self.command, path, self._status,
                                 int((time.monotonic() - started) * 1000))

    def _send(self, status: int, body: bytes, content_type: str, extra=(), close: bool = False) -> None:
        self._status = status
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in extra:
                self.send_header(name, value)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if body and getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def _send_json(self, status: int, payload, close: bool = False) -> None:
        body = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, JSON_TYPE, close=close)

    def send_error(self, code, message=None, explain=None) -> None:
        """Every error the base class produces (bad request line, 501, …) becomes ``{"error": …}`` JSON."""
        if message is None:
            try:
                message = HTTPStatus(code).phrase.lower()
            except ValueError:
                message = "error"
        self._send_json(code, {"error": message}, close=True)

    def version_string(self) -> str:
        """``ccr/<version>`` without the trailing space the base class adds for an empty ``sys_version``."""
        return self.server_version

    def log_request(self, code="-", size="-") -> None:
        """Silenced: ``_dispatch`` writes the single ``--verbose`` line itself."""

    def log_error(self, fmt, *args) -> None:
        if self.server.verbose:
            self.log_message(fmt, *args)

    def log_message(self, fmt, *args) -> None:
        self.server.log_stream.write(fmt % args + "\n")
        self.server.log_stream.flush()

    # ------------------------------------------------------------------ security

    def _check_host_headers(self, is_api: bool) -> None:
        host = self.headers.get("Host")
        if host is None or host_part(host) not in ALLOWED_HOSTS:
            raise HttpError(400, "bad host", close=True)
        for name in ("Origin", "Referer"):
            value = self.headers.get(name)
            if value is None:
                continue
            if value.strip().lower() == "null" or not _origin_allowed(value):
                raise HttpError(403, "bad %s" % name.lower(), close=True)
        if is_api and (self.headers.get("Sec-Fetch-Site") or "").strip().lower() in ("cross-site", "same-site"):
            raise HttpError(403, "cross-site request refused", close=True)

    def _check_token(self) -> None:
        provided = (self.headers.get("X-CCR-Token") or "").strip().encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(provided, self.server.token.encode("utf-8")):
            raise HttpError(401, "unauthorized", close=True)

    # ------------------------------------------------------------------ bodies

    def _read_body(self) -> dict:
        """The JSON object body of a POST/PATCH per section 5.0 (411 / 413-before-read / 415 / 400)."""
        if self.headers.get("Transfer-Encoding") is not None:
            raise HttpError(411, "Content-Length required (Transfer-Encoding is not supported)", close=True)
        declared = (self.headers.get("Content-Length") or "").strip()
        if not (declared.isascii() and declared.isdigit()):  # '²' is isdigit() but not an int
            raise HttpError(411, "Content-Length required", close=True)
        length = int(declared)
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "request body exceeds %d bytes" % MAX_BODY_BYTES, close=True)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HttpError(415, "Content-Type must be application/json")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):  # RecursionError: absurdly nested JSON
            raise HttpError(400, "body must be a UTF-8 encoded JSON object") from None
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        return data

    # ------------------------------------------------------------------ routing

    def _handle(self) -> None:
        url = _split_target(self.path)
        path = url.path
        is_api = path.startswith("/api/")
        self._check_host_headers(is_api)
        if is_api:
            self._check_token()
            self._route_api(path, parse_qs(url.query, keep_blank_values=True))
            return
        if self.command != "GET":
            raise HttpError(404, "not found", close=True)
        if path == "/":
            self._send_file(os.path.join(STATIC_DIR, "index.html"), CACHE_NONE)
        elif path == "/favicon.ico":
            self._send_file(os.path.join(STATIC_DIR, "favicon.svg"), CACHE_NONE)
        elif path.startswith("/static/"):
            self._serve_static(path)
        else:
            raise HttpError(404, "not found")

    def _route_api(self, path: str, query: dict) -> None:
        method = self.command
        parts = [unquote(part) for part in path[len("/api/"):].split("/")]
        body = self._read_body() if method in ("POST", "PATCH") else {}
        if any(part == "" for part in parts):
            raise HttpError(404, "not found")
        head, depth = parts[0], len(parts)
        route = None
        if depth == 1:
            route = self._single_segment_route(head, method, query, body)
        elif depth == 2 and head == "commits" and method == "GET":
            route = lambda: self._commit(parts[1], query)
        elif depth == 3 and head == "commits" and parts[2] == "file" and method == "GET":
            route = lambda: self._commit_file(parts[1], query)
        elif depth == 2 and head == "comments" and method == "PATCH":
            route = lambda: self._patch_comment(parts[1], body)
        elif depth == 2 and head == "comments" and method == "DELETE":
            route = lambda: self._delete_comment(parts[1], query)
        elif depth == 3 and head == "comments" and parts[2] == "github" and method == "GET":
            route = lambda: self._send_json(200, self.server.store.github_target(parts[1]))
        elif depth == 3 and head == "comments" and parts[2] == "github" and method == "POST":
            store = self.server.store
            route = (lambda: self._send_json(200, store.record_github_update(parts[1], body.get("updated")))) \
                if isinstance(body, dict) and "updated" in body \
                else (lambda: self._send_json(200, store.record_github_post(parts[1], body.get("posted"))))
        elif depth == 2 and head == "github" and parts[1] == "sync" and method == "POST":
            route = lambda: self._send_json(200, self.server.store.sync_github(body))
        if route is None:
            raise HttpError(404, "not found")
        route()

    def _single_segment_route(self, name: str, method: str, query: dict, body: dict):
        store = self.server.store
        get_routes = {
            "review": lambda: self._send_json(200, store.review()),
            "state": lambda: self._send_json(200, store.state()),
            "events": lambda: self._events(query),
            "compare": lambda: self._compare(query),
            "file": lambda: self._file(query),
            "comments": lambda: self._list_comments(query),
        }
        post_routes = {
            "comments": lambda: self._send_json(201, store.add_comment(
                body.get("body"), body.get("anchor"), body.get("author") or "user", body.get("parent_id"),
                github=body.get("github"))),
            "submit": lambda: self._send_json(201, store.submit(body.get("verdict"), body.get("summary"))),
            "reload": lambda: self._reload(body),
            "cover": lambda: self._send_json(200, {"cover": store.set_cover(body.get("text")), "version": store.version}),
            "pr": lambda: self._send_json(200, {"pr": store.set_pr(body.get("url")), "version": store.version}),
            "shutdown": self._shutdown,
        }
        if method == "GET":
            return get_routes.get(name)
        if method == "POST":
            return post_routes.get(name)
        return None

    # ------------------------------------------------------------------ routes

    def _events(self, query: dict) -> None:
        store = self.server.store
        if not (self.headers.get("User-Agent") or "").startswith(CLI_AGENT_PREFIX):
            store.touch_ui()
        since = _int_param(query, "since", store.version)
        timeout = min(max(_float_param(query, "timeout", DEFAULT_POLL_SECONDS), 0.0), MAX_POLL_SECONDS)
        if not self.server.acquire_waiter():
            state = store.state()
            state.update(changed=False, retry_after=RETRY_AFTER_SECONDS)
            self._send_json(200, state)
            return
        try:
            result = store.wait(since, timeout)
        finally:
            self.server.release_waiter()
        self._send_json(200, result)

    def _commit(self, ref: str, query: dict) -> None:
        diff = self.server.store.commit_diff(ref, full=_flag(query, "full"), ws_ignore=_first(query, "ws") == "ignore")
        self._send_json(200, diff)

    def _commit_file(self, ref: str, query: dict) -> None:
        path = _first(query, "path")
        if not path:
            raise HttpError(400, "path is required")
        self._send_json(200, self.server.store.file_diff(ref, path, ws_ignore=_first(query, "ws") == "ignore"))

    def _compare(self, query: dict) -> None:
        head = _first(query, "head")
        if not head:
            raise HttpError(400, "head is required")
        diff = self.server.store.compare(_first(query, "base") or None, head,
                                         ws_ignore=_first(query, "ws") == "ignore", full=_flag(query, "full"))
        self._send_json(200, diff)

    def _file(self, query: dict) -> None:
        rev, path = _first(query, "rev"), _first(query, "path")
        if not rev or not path:
            raise HttpError(400, "rev and path are required")
        self._send_json(200, self.server.store.file(rev, path))

    def _list_comments(self, query: dict) -> None:
        store = self.server.store
        state = _first(query, "state") or None
        if state is not None and state not in _STATES:
            raise HttpError(400, "state must be pending or submitted")
        outdated = _first(query, "outdated") or "include"
        if outdated not in _OUTDATED_MODES:
            raise HttpError(400, "outdated must be include, exclude or only")
        comments = store.list_comments(
            state=state, round=_int_param(query, "round"), resolved=_bool_param(query, "resolved"),
            author=_first(query, "author") or None, commit=_first(query, "commit") or None,
            path=_first(query, "path") or None, include_outdated=outdated != "exclude",
            outdated_only=outdated == "only", locate=_flag(query, "locate"), project=_first(query, "project") or None)
        self._send_json(200, {"version": store.version, "generation": store.generation, "now": utcnow(),
                              "comments": comments})

    def _patch_comment(self, comment_id: str, body: dict) -> None:
        comment = self.server.store.edit_comment(comment_id, body=body.get("body"), resolved=body.get("resolved"),
                                                 anchor=body.get("anchor"), github=body.get("github"))
        self._send_json(200, comment)

    def _delete_comment(self, comment_id: str, query: dict) -> None:
        self.server.store.delete_comment(comment_id, cascade=_flag(query, "cascade"))
        self._send_json(204, None)

    def _reload(self, body: dict) -> None:
        store = self.server.store
        spec = _optional(body, "range", str, "a string")
        n = _optional(body, "n", int, "an integer")
        worktree = _optional(body, "worktree", bool, "a boolean")
        first_parent = _optional(body, "first_parent", bool, "a boolean")
        try:
            result = store.load(spec=spec, n=n, worktree=worktree, first_parent=first_parent)
        except GitError as exc:
            raise HttpError(400, str(exc)) from None
        self.server.refresh_session_record()
        outdated_ids = set(result["outdated"])
        outdated = [c for c in store.list_comments() if c["id"] in outdated_ids] if outdated_ids else []
        self._send_json(200, {
            "review": store.review(),
            "remapped": result["remapped"],
            "outdated": outdated,
            "commits_added": result["commits_added"],
            "commits_removed": result["commits_removed"],
        })

    def _shutdown(self) -> None:
        self.server.begin_shutdown()
        self._send_json(202, {"stopping": True})

    # ------------------------------------------------------------------ static files

    def _serve_static(self, path: str) -> None:
        rel = unquote(path[len("/static/"):])
        if not rel or "\0" in rel or any(segment in ("", "..") for segment in rel.split("/")):
            raise HttpError(404, "not found")
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if os.path.commonpath([target, STATIC_REAL]) != STATIC_REAL or not os.path.isfile(target):
            raise HttpError(404, "not found")
        self._send_file(target, CACHE_IMMUTABLE if rel.startswith("vendor/") else CACHE_NONE)

    def _send_file(self, target: str, cache_control: str) -> None:
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            raise HttpError(404, "not found") from None
        content_type = content_type_for(target)
        extra = [("Cache-Control", cache_control)]
        if content_type.startswith("text/html"):
            extra.extend(HTML_HEADERS)
        self._send(200, body, content_type, extra)


# --------------------------------------------------------------------------- ccr serve

def _redirect_output(log_path: str) -> None:
    """Send this process's stdout/stderr to the log file (append, 0600)."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)


def _bind(store: ReviewStore, token: str, key: str, port, verbose: bool, idle_timeout: float) -> ReviewServer:
    chosen = session.pick_port(key, port)
    try:
        return make_server(store, token, chosen, verbose, idle_timeout)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise session.SessionError("port %d in use" % chosen) from None
        raise


def _range_label(rng: dict) -> str:
    return rng.get("given") or rng["spec"]


def _print_serving(store: ReviewStore, url: str, out) -> None:
    review = store.review()
    commits = sum(1 for c in review["commits"] if c["kind"] == "commit")
    suffix = ", +worktree" if review["options"]["worktree"] else ""
    out.write("ccr: serving %s  (%s, %d commits%s)\n" % (store.repo, _range_label(review["range"]), commits, suffix))
    for line in (render.review_line(review), render.pr_line(review)):
        if line:
            out.write(line + "\n")
    out.write("ccr: url %s\n" % url)
    if review["range"].get("note"):
        out.write("ccr: note: %s\n" % review["range"]["note"])
    out.flush()


def serve(repo, spec=None, n=None, worktree: bool = False, first_parent: bool = False, port=None, db=None,
          db_force: bool = False, token=None, log=None, verbose: bool = False,
          idle_timeout: float = DEFAULT_IDLE_TIMEOUT, open_browser: bool = False, cover=None, pr=None,
          out=None, err=None) -> int:
    """Run ``ccr serve`` in the foreground (section 5.2 lifecycle); returns the process exit code.

    Range and database problems raise :class:`GitError` / :class:`StoreError` /
    :class:`~ccr.session.SessionError` before anything is bound; the caller prints them.
    """
    out, err = out or sys.stdout, err or sys.stderr
    os.umask(0o077)
    from_env = token is None and bool(os.environ.get(session.TOKEN_ENV))
    token = token or os.environ.get(session.TOKEN_ENV) or secrets.token_hex(16)
    if log:
        log = os.path.abspath(log)
        _redirect_output(log)
    toplevel = gitx.toplevel(repo)["path"]
    paths = session.paths_for(toplevel)
    store = ReviewStore(toplevel, spec, n, worktree=worktree, first_parent=first_parent, db_path=db, db_force=db_force)
    try:
        if cover is not None:
            with open(cover, "r", encoding="utf-8") as handle:
                store.set_cover(handle.read())
        if pr is not None:
            store.set_pr(pr)
        httpd = _bind(store, token, paths.key, port, verbose, idle_timeout)
    except BaseException:
        store.close()
        raise
    started_at = utcnow()
    rng = store.review()["range"]
    record = {
        "pid": os.getpid(), "port": httpd.port, "token": token, "url": "%s?t=%s" % (httpd.url, token),
        "repo": store.repo, "range": _range_label(rng), "started_at": started_at, "log": log, "db": store.db_path,
    }
    session.write_record(paths, record)
    httpd.session_paths, httpd.session_record = paths, record
    store.set_server_info({"pid": os.getpid(), "port": httpd.port, "started_at": started_at, "version": __version__})
    printed_url = "%s?t=%s" % (httpd.url, "<redacted>" if from_env else token)
    outcome = {"code": 0}

    def load_initial() -> None:
        try:
            store.load()
        except (GitError, StoreError) as exc:
            err.write("ccr: %s\n" % exc)
            err.flush()
            outcome["code"] = 1
            httpd.begin_shutdown()
            return
        _print_serving(store, printed_url, out)
        if open_browser:
            webbrowser.open(record["url"])

    def on_signal(signum, frame) -> None:
        httpd.begin_shutdown()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=load_initial, name="ccr-load", daemon=True).start()
    httpd.start_idle_watcher()
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        store.close()
        session.remove_record(paths.record, os.getpid())
    return outcome["code"]

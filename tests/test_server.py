"""Tests for ccr.server (SPEC.md section 5) against a real ``ThreadingHTTPServer`` on an ephemeral port.

Every test talks to a live server over ``http.client`` so headers, bodies and connection handling are
exercised exactly as a browser or the CLI would; the store uses the fixture repository with an
in-memory database.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import threading
import time

import pytest

from ccr import __version__, server
from ccr.server import content_type_for, host_part, make_server
from ccr.store import COMBINED, WORKTREE, ReviewStore
from conftest import FEATURE_SUBJECTS

TOKEN = "0123456789abcdef0123456789abcdef"
THREE_HUNKS, RENAME, BINARY, EDIT_NONL, MERGE, EMPTY, BIG = FEATURE_SUBJECTS
JSON_HEADERS = {"Content-Type": "application/json"}


class Response:
    def __init__(self, raw: http.client.HTTPResponse):
        self.status = raw.status
        self.headers = {k.lower(): v for k, v in raw.getheaders()}
        self.data = raw.read()

    @property
    def json(self):
        return json.loads(self.data.decode("utf-8")) if self.data else None

    def header(self, name: str):
        return self.headers.get(name.lower())


class Live:
    """A running server plus helpers to issue raw requests against it."""

    def __init__(self, repo, store: ReviewStore, httpd):
        self.repo = repo
        self.store = store
        self.httpd = httpd
        self.port = httpd.port
        self.thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def request(self, method: str, path: str, body=None, headers=None, token=TOKEN, raw=None) -> Response:
        """Send one request on a fresh connection; ``body`` (dict) is JSON-encoded, ``raw`` bytes go as-is."""
        sent = dict(headers or {})
        if token is not None:
            sent.setdefault("X-CCR-Token", token)
        payload = raw
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=sent)
            return Response(conn.getresponse())
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    def head_only(self, method: str, path: str, headers: dict) -> Response:
        """Send just a request head (no body) — for the 411/413 rules."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path)
            conn.putheader("X-CCR-Token", TOKEN)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.endheaders()
            return Response(conn.getresponse())
        finally:
            conn.close()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()


def open_store(repo, load=True, **kw):
    options = dict(worktree=True, db_path=":memory:")
    options.update(kw)
    store = ReviewStore(repo.path, "main..feature", None, **options)
    if load:
        store.load()
    return store


@pytest.fixture
def live(fixture_repo):
    store = open_store(fixture_repo)
    srv = Live(fixture_repo, store, make_server(store, TOKEN, 0))
    yield srv
    srv.close()


@pytest.fixture
def loading(fixture_repo):
    """A server whose store has not finished (or started) its first load."""
    store = open_store(fixture_repo, load=False)
    srv = Live(fixture_repo, store, make_server(store, TOKEN, 0))
    yield srv
    srv.close()


def line_anchor(commit, path, line, side="new", start_line=None):
    return {"kind": "line", "commit": commit, "path": path, "side": side, "line": line, "start_line": start_line}


def add_comment(live: Live, body="Why?", anchor=None, **extra) -> dict:
    anchor = anchor or line_anchor(live.repo.sha(THREE_HUNKS), "src/app.py", 5)
    response = live.post("/api/comments", dict({"body": body, "anchor": anchor}, **extra))
    assert response.status == 201, response.data
    return response.json


# --------------------------------------------------------------------------- protocol basics

def test_every_response_has_content_length_and_nosniff(live):
    created = add_comment(live)
    responses = [
        live.get("/api/state"),
        live.get("/"),
        live.get("/nope"),
        live.get("/api/state", token=None),
        live.request("DELETE", "/api/comments/%s" % created["id"]),
    ]
    for response in responses:
        assert response.header("Content-Length") == str(len(response.data))
        assert response.header("X-Content-Type-Options") == "nosniff"
        assert response.header("Server") == "ccr/" + __version__
    assert responses[-1].status == 204 and responses[-1].data == b""


def test_errors_are_json_including_base_class_send_error(live):
    unknown = live.get("/nope")
    assert unknown.status == 404 and unknown.json == {"error": "not found"}
    assert unknown.header("Content-Type") == "application/json; charset=utf-8"
    unsupported = live.request("PUT", "/api/state")
    assert unsupported.status == 501
    assert "Unsupported method" in unsupported.json["error"]
    assert live.request("POST", "/", body={}).status == 404


def test_body_rules(live):
    chunked = live.head_only("POST", "/api/submit", {"Transfer-Encoding": "chunked"})
    assert chunked.status == 411
    missing_length = live.head_only("POST", "/api/submit", {})
    assert missing_length.status == 411 and "Content-Length" in missing_length.json["error"]
    started = time.monotonic()
    too_large = live.head_only("POST", "/api/comments", {"Content-Length": str(2 * 1024 * 1024),
                                                          "Content-Type": "application/json"})
    assert too_large.status == 413 and time.monotonic() - started < 5
    assert too_large.header("Connection") == "close"
    empty = live.request("POST", "/api/submit", raw=b"", headers={"Content-Type": "text/plain"})
    assert empty.status == 400 and "verdict" in empty.json["error"]
    wrong_type = live.request("POST", "/api/submit", raw=b'{"verdict": "approve"}', headers={"Content-Type": "text/plain"})
    assert wrong_type.status == 415
    upper_type = live.request("POST", "/api/submit", raw=b'{"verdict": "approve", "summary": ""}',
                              headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert upper_type.status == 201
    invalid = live.request("POST", "/api/comments", raw=b"{not json", headers=JSON_HEADERS)
    assert invalid.status == 400 and "JSON" in invalid.json["error"]
    array = live.request("POST", "/api/comments", raw=b"[1, 2]", headers=JSON_HEADERS)
    assert array.status == 400
    binary = live.request("POST", "/api/comments", raw=b"\xff\xfe", headers=JSON_HEADERS)
    assert binary.status == 400


def test_verbose_log_line_omits_the_query(live):
    stream = io.StringIO()
    live.httpd.verbose = True
    live.httpd.log_stream = stream
    try:
        assert live.get("/api/state?since=3&t=secret").status == 200
    finally:
        live.httpd.verbose = False
        live.httpd.log_stream = server.sys.stderr
    line = stream.getvalue().strip()
    assert line.startswith("GET /api/state 200 ") and line.endswith("ms")
    assert "secret" not in line and "since" not in line


# --------------------------------------------------------------------------- security

def test_token_required_on_api_and_query_token_rejected(live):
    for response in (live.get("/api/state", token=None), live.get("/api/state", token="wrong"),
                     live.get("/api/state?t=%s" % TOKEN, token=None), live.get("/api/review", token="")):
        assert response.status == 401 and response.json == {"error": "unauthorized"}
    assert live.get("/", token=None).status == 200
    assert live.get("/static/app.js", token=None).status == 200
    assert live.get("/api/state").status == 200


def test_host_allowlist_on_every_route(live):
    for path in ("/", "/static/app.js", "/api/state", "/nope"):
        response = live.get(path, headers={"Host": "evil.example:%d" % live.port})
        assert response.status == 400 and response.json == {"error": "bad host"}, path
    for host in ("localhost:1", "127.0.0.1", "[::1]:80", "LOCALHOST:7777"):
        assert live.get("/api/state", headers={"Host": host}).status == 200, host
    assert host_part("[::1]:80") == "[::1]" and host_part("localhost:7") == "localhost" and host_part("127.0.0.1") == "127.0.0.1"


def test_origin_and_referer_checks(live):
    ok = live.get("/api/state", headers={"Origin": "http://127.0.0.1:9999"})
    assert ok.status == 200, "a port mismatch (SSH forwarding) must be allowed"
    assert live.get("/api/state", headers={"Origin": "http://localhost:1234", "Referer": "http://[::1]:5/x"}).status == 200
    for origin in ("http://evil.example", "null", "https://127.0.0.1.evil.example", "garbage"):
        response = live.get("/api/state", headers={"Origin": origin})
        assert response.status == 403 and response.json == {"error": "bad origin"}, origin
    referer = live.get("/", headers={"Referer": "http://evil.example/page"})
    assert referer.status == 403 and referer.json == {"error": "bad referer"}
    for header in ("Access-Control-Allow-Origin", "Access-Control-Allow-Headers"):
        assert ok.header(header) is None


def test_sec_fetch_site_blocks_cross_site_api_calls_only(live):
    for value in ("cross-site", "same-site"):
        assert live.get("/api/state", headers={"Sec-Fetch-Site": value}).status == 403
    assert live.get("/api/state", headers={"Sec-Fetch-Site": "same-origin"}).status == 200
    assert live.get("/api/state", headers={"Sec-Fetch-Site": "none"}).status == 200
    assert live.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status == 200


# --------------------------------------------------------------------------- static files

def test_index_html_headers(live):
    response = live.get("/")
    assert response.status == 200
    assert response.header("Content-Type") == "text/html; charset=utf-8"
    assert response.header("Content-Security-Policy") == server.CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in response.header("Content-Security-Policy")
    assert response.header("X-Frame-Options") == "DENY"
    assert response.header("Referrer-Policy") == "no-referrer"
    assert response.header("Cross-Origin-Opener-Policy") == "same-origin"
    assert response.header("Cross-Origin-Resource-Policy") == "same-origin"
    assert response.header("Cache-Control") == "no-cache"
    assert b"<script" in response.data
    assert live.get("/?t=%s" % TOKEN).status == 200
    assert live.get("/index.html").status == 404


def test_static_mime_table_and_cache_headers(live):
    js = live.get("/static/app.js")
    assert js.status == 200 and js.header("Content-Type") == "text/javascript; charset=utf-8"
    assert js.header("Cache-Control") == "no-cache" and js.header("Content-Security-Policy") is None
    css = live.get("/static/style.css")
    assert css.header("Content-Type") == "text/css; charset=utf-8"
    vendor = live.get("/static/vendor/highlight.min.js?v=%s" % __version__)
    assert vendor.status == 200 and vendor.header("Cache-Control") == "max-age=31536000, immutable"
    vendor_css = live.get("/static/vendor/github.min.css?v=1")
    assert vendor_css.header("Content-Type") == "text/css; charset=utf-8"
    favicon = live.get("/favicon.ico")
    assert favicon.status == 200 and favicon.header("Content-Type") == "image/svg+xml"
    assert favicon.data == live.get("/static/favicon.svg").data
    assert content_type_for("x.woff2") == "font/woff2"
    assert content_type_for("x.json") == "application/json; charset=utf-8"
    assert content_type_for("x.mjs") == "text/javascript; charset=utf-8"
    assert content_type_for("x.HTML") == "text/html; charset=utf-8"
    assert content_type_for("x.bin") == "application/octet-stream"


def test_static_traversal_and_missing_files_are_404(live):
    for path in ("/static/", "/static/../server.py", "/static/%2e%2e/server.py", "/static//style.css",
                 "/static/vendor/../../gitx.py", "/static/nope.js", "/static/vendor", "/static/app.js%00"):
        response = live.get(path)
        assert response.status == 404 and response.json == {"error": "not found"}, path


# --------------------------------------------------------------------------- read routes

def test_review_and_state(live):
    review = live.get("/api/review").json
    assert review["loading"] is False and review["generation"] == 1
    assert [c["sha"] for c in review["commits"]] == [COMBINED] + live.repo.feature_chain + [WORKTREE]
    assert review["range"]["spec"] == "main..feature" and review["options"] == {"worktree": True}
    state = live.get("/api/state").json
    assert state["commits"] == 7 and state["rounds"] == 0 and state["last_round"] is None
    assert set(state) == {"version", "generation", "loading", "now", "server", "counts", "rounds", "last_round",
                          "commits", "ui", "pr_synced_at"}


def test_commit_routes(live):
    sha = live.repo.sha(THREE_HUNKS)
    full = live.get("/api/commits/%s" % sha).json
    assert full["sha"] == sha and full["kind"] == "commit" and full["hunk_count" if "hunk_count" in full else "sha"]
    assert [f["path"] for f in full["files"]] == ["src/app.py"] and len(full["files"][0]["hunks"]) == 3
    short = live.get("/api/commits/%s" % sha[:7]).json
    assert short["sha"] == sha
    combined = live.get("/api/commits/combined").json
    assert combined["kind"] == "combined" and combined["old_rev" if "old_rev" in combined else "kind"]
    worktree = live.get("/api/commits/worktree").json
    assert worktree["kind"] == "worktree" and any(f["path"] == "untracked.txt" for f in worktree["files"])
    big = live.get("/api/commits/%s" % live.repo.big).json
    generated = next(f for f in big["files"] if f["path"] == "big/generated.txt")
    assert generated["too_large"] is True and generated["hunks"] == []
    big_full = live.get("/api/commits/%s?full=1" % live.repo.big).json
    assert next(f for f in big_full["files"] if f["path"] == "big/generated.txt")["hunks"]
    ws = live.get("/api/commits/%s?ws=ignore" % live.repo.big).json
    assert next(f for f in ws["files"] if f["path"] == "data/config.ini")["ws_only"] is True
    assert live.get("/api/commits/0000000000").status == 404
    assert live.get("/api/commits/%s" % live.repo.root).status == 404
    assert live.get("/api/commits/compare:abc..def").status == 400


def test_commit_file_route(live):
    sha = live.repo.sha(RENAME)
    by_new = live.get("/api/commits/%s/file?path=src/utils.py" % sha).json
    assert by_new["path"] == "src/utils.py" and by_new["old_path"] == "src/util.py" and by_new["hunks"]
    by_old = live.get("/api/commits/%s/file?path=src/util.py" % sha[:10]).json
    assert by_old["path"] == "src/utils.py"
    big = live.get("/api/commits/%s/file?path=big/generated.txt" % live.repo.big).json
    assert len(big["hunks"][0]["lines"]) == 6000, "the file route is untrimmed"
    assert live.get("/api/commits/%s/file" % sha).status == 400
    assert live.get("/api/commits/%s/file?path=nope.py" % sha).status == 404
    assert live.get("/api/commits/%s/file?path=src/app.py" % live.repo.root).status == 404
    assert live.get("/api/commits//file?path=x").status == 404


def test_compare_route(live):
    repo = live.repo
    response = live.get("/api/compare?base=%s&head=%s" % (repo.main, repo.feature))
    assert response.status == 200
    diff = response.json
    assert diff["kind"] == "compare" and diff["sha"] == "compare:%s..%s" % (repo.main[:10], repo.feature[:10])
    assert diff["subject"].startswith("Compare ") and diff["comment_count"] == 0
    from_root = live.get("/api/compare?head=%s" % repo.sha(THREE_HUNKS)).json
    assert from_root["files"] and all(f["old_rev"] is None for f in from_root["files"])
    assert live.get("/api/compare?base=%s&head=%s" % (repo.root, repo.feature)).status == 400
    assert live.get("/api/compare?base=%s&head=%s" % (repo.main, "f" * 40)).status == 400
    assert live.get("/api/compare?base=%s&head=%s" % (repo.main, repo.feature[:10])).status == 400
    assert live.get("/api/compare?base=%s" % repo.main).status == 400


def test_file_route(live):
    repo = live.repo
    sha = repo.sha(THREE_HUNKS)
    text = live.get("/api/file?rev=%s&path=src/app.py" % sha).json
    assert text["rev"] == sha and text["path"] == "src/app.py" and text["lines"] == 31
    assert "value_05 = 500  # changed" in text["content"]
    worktree = live.get("/api/file?rev=worktree&path=untracked.txt").json
    assert worktree["content"].startswith("untracked line 1")
    binary = live.get("/api/file?rev=%s&path=assets/logo.png" % repo.sha(BINARY))
    assert binary.status == 415
    assert live.get("/api/file?rev=%s&path=src/app.py" % repo.root).status == 400
    assert live.get("/api/file?rev=HEAD&path=src/app.py").status == 400
    assert live.get("/api/file?rev=%s&path=../etc/passwd" % sha).status == 400
    assert live.get("/api/file?rev=%s&path=/etc/passwd" % sha).status == 400
    assert live.get("/api/file?rev=%s&path=not/in/review.py" % sha).status == 400
    assert live.get("/api/file?rev=%s" % sha).status == 400
    missing = live.get("/api/file?rev=%s&path=big/generated.txt" % sha)
    assert missing.status == 404, "path known to the review but absent at this revision"


def test_unknown_api_paths_and_methods_are_404(live):
    for method, path in (("GET", "/api/nope"), ("GET", "/api/submit"), ("POST", "/api/state"), ("GET", "/api/"),
                         ("PATCH", "/api/comments"), ("DELETE", "/api/comments"), ("GET", "/api/comments/x/y")):
        response = live.request(method, path, body={} if method in ("POST", "PATCH") else None)
        assert response.status == 404 and response.json == {"error": "not found"}, (method, path)


# --------------------------------------------------------------------------- comments and rounds

def test_comment_crud_and_filters(live):
    repo = live.repo
    version = live.get("/api/state").json["version"]
    root = add_comment(live, "Why 500?")
    assert root["author"] == "user" and root["state"] == "pending" and root["snippet"] == "value_05 = 500  # changed"
    claude = add_comment(live, "Heads-up", anchor={"kind": "commit", "commit": "HEAD"}, author="claude")
    assert claude["state"] == "submitted" and claude["round"] == 0 and claude["anchor"]["commit"] == repo.feature
    reply = live.post("/api/comments", {"body": "Because.", "parent_id": root["id"], "author": "claude"}).json
    assert reply["parent_id"] == root["id"] and reply["anchor"] == root["anchor"]

    listing = live.get("/api/comments").json
    assert set(listing) == {"version", "generation", "now", "comments"}
    assert listing["version"] == version + 3 and [c["id"] for c in listing["comments"]] == [root["id"], claude["id"], reply["id"]]
    assert [c["id"] for c in live.get("/api/comments?state=pending").json["comments"]] == [root["id"]]
    assert [c["id"] for c in live.get("/api/comments?author=claude").json["comments"]] == [claude["id"], reply["id"]]
    assert [c["id"] for c in live.get("/api/comments?commit=%s" % repo.feature[:8]).json["comments"]] == [claude["id"]]
    assert [c["id"] for c in live.get("/api/comments?path=src/app.py&round=0").json["comments"]] == [reply["id"]]
    assert live.get("/api/comments?outdated=only").json["comments"] == []
    assert len(live.get("/api/comments?outdated=exclude&resolved=false").json["comments"]) == 3
    located = live.get("/api/comments?locate=1").json["comments"]
    assert located[0]["head_location"] == {"path": "src/app.py", "line": 5, "status": "same"}
    assert "head_location" not in located[1] and "head_location" not in located[2]
    for bad in ("state=nope", "outdated=maybe", "round=x", "resolved=maybe"):
        assert live.get("/api/comments?" + bad).status == 400, bad

    edited = live.request("PATCH", "/api/comments/%s" % root["id"], body={"body": "Why 500 exactly?", "resolved": True}).json
    assert edited["body"] == "Why 500 exactly?" and edited["resolved"] is True and edited["updated_at"] > edited["created_at"]
    moved = live.request("PATCH", "/api/comments/%s" % root["id"],
                         body={"anchor": line_anchor(repo.sha(THREE_HUNKS), "src/app.py", 15, side="old")}).json
    assert moved["anchor"]["line"] == 15 and moved["moved_from"] == {"commit": repo.sha(THREE_HUNKS), "line": 5}
    assert live.request("PATCH", "/api/comments/%s" % root["id"], body={}).status == 400
    assert live.request("PATCH", "/api/comments/nope", body={"body": "x"}).status == 404
    assert live.request("PATCH", "/api/comments/%s" % reply["id"], body={"resolved": True}).status == 400

    conflict = live.request("DELETE", "/api/comments/%s" % root["id"])
    assert conflict.status == 409 and conflict.json == {"error": "thread has replies"}
    assert live.request("DELETE", "/api/comments/%s?cascade=1" % root["id"]).status == 204
    assert live.request("DELETE", "/api/comments/%s" % reply["id"]).status == 404
    assert live.request("DELETE", "/api/comments/%s" % claude["id"]).status == 204
    assert live.get("/api/comments").json["comments"] == []


def test_comment_validation_errors(live):
    assert live.post("/api/comments", {"body": "", "anchor": {"kind": "review"}}).status == 400
    assert live.post("/api/comments", {"body": "x", "anchor": {"kind": "line", "commit": "combined"}}).status == 400
    assert live.post("/api/comments", {"body": "x", "anchor": {"kind": "commit", "commit": "compare:a..b"}}).status == 400
    unknown = live.post("/api/comments", {"body": "x", "anchor": {"kind": "commit", "commit": live.repo.root}})
    assert unknown.status == 404
    assert live.post("/api/comments", {"body": "x", "anchor": {"kind": "review"}, "author": "bot"}).status == 400
    assert live.post("/api/comments", {"body": "x", "parent_id": "nope"}).status == 404
    assert live.post("/api/comments", {"body": "x"}).status == 400


def test_submit_route(live):
    nothing = live.post("/api/submit", {"verdict": "request_changes", "summary": ""})
    assert nothing.status == 400 and nothing.json["error"].startswith("nothing to submit")
    add_comment(live)
    response = live.post("/api/submit", {"verdict": "request_changes", "summary": "Please fix"})
    assert response.status == 201
    round_info = response.json
    assert round_info["number"] == 1 and round_info["verdict"] == "request_changes"
    assert round_info["head"] == live.repo.feature and len(round_info["comment_ids"]) == 2
    comments = live.get("/api/comments").json["comments"]
    assert all(c["state"] == "submitted" and c["round"] == 1 for c in comments)
    assert live.get("/api/state").json["last_round"]["number"] == 1
    assert live.post("/api/submit", {"verdict": "approve"}).status == 201
    assert live.post("/api/submit", {"verdict": "meh"}).status == 400


def test_reload_route(live):
    repo = live.repo
    before = live.get("/api/state").json
    comment = add_comment(live, "note", anchor={"kind": "commit", "commit": repo.feature})
    repo.git("reset", "-q")
    repo.git("commit", "-q", "--allow-empty", "-m", "Follow-up")
    response = live.post("/api/reload", {})
    assert response.status == 200
    result = response.json
    assert set(result) == {"review", "remapped", "outdated", "commits_added", "commits_removed"}
    assert result["commits_added"] == 1 and result["commits_removed"] == 0
    assert result["remapped"] == [] and result["outdated"] == []
    assert result["review"]["generation"] == before["generation"] + 1
    assert [c["subject"] for c in result["review"]["commits"]][-2] == "Follow-up"

    narrowed = live.post("/api/reload", {"range": "%s..feature" % repo.sha(BIG), "worktree": False}).json
    assert narrowed["commits_removed"] == 7 and narrowed["review"]["options"]["worktree"] is False
    assert [c["id"] for c in narrowed["outdated"]] == [comment["id"]]
    assert narrowed["outdated"][0]["outdated"] is True and narrowed["outdated"][0]["anchor"]["commit"] == repo.feature

    generation = live.get("/api/state").json["generation"]
    bad = live.post("/api/reload", {"range": "nope..HEAD"})
    assert bad.status == 400 and "nope" in bad.json["error"]
    assert live.get("/api/state").json["generation"] == generation, "previous data kept after a git error"
    for body in ({"n": "3"}, {"worktree": "yes"}, {"range": 5}, {"first_parent": 1}, {"range": "a..b", "n": 2}):
        assert live.post("/api/reload", body).status == 400, body


# --------------------------------------------------------------------------- long-polling

def test_events_early_returns(live):
    version = live.get("/api/state").json["version"]
    started = time.monotonic()
    behind = live.get("/api/events?since=%d&timeout=10" % (version - 1)).json
    assert behind["changed"] is True and behind["version"] == version and time.monotonic() - started < 2
    ahead = live.get("/api/events?since=%d&timeout=10" % (version + 100)).json
    assert ahead["changed"] is True, "a since from another server incarnation returns immediately"
    started = time.monotonic()
    quiet = live.get("/api/events?since=%d&timeout=0.3" % version).json
    elapsed = time.monotonic() - started
    assert quiet["changed"] is False and 0.25 <= elapsed < 3
    assert set(quiet) >= {"version", "generation", "counts", "rounds", "last_round", "commits", "ui", "changed"}
    assert live.get("/api/events?since=x").status == 400
    assert live.get("/api/events?timeout=abc").status == 400


def test_events_wake_on_mutation_and_track_the_ui(live):
    version = live.get("/api/state").json["version"]
    assert live.get("/api/state", headers={"User-Agent": "ccr-cli/0.1.0"}).json["ui"]["last_seen"] is None
    cli_poll = live.get("/api/events?since=%d&timeout=0" % (version - 1), headers={"User-Agent": "ccr-cli/0.1.0"}).json
    assert cli_poll["ui"]["last_seen"] is None, "CLI polls must not count as a connected UI"

    results = []

    def poll():
        results.append(live.get("/api/events?since=%d&timeout=10" % version, headers={"User-Agent": "Mozilla/5.0"}).json)

    thread = threading.Thread(target=poll)
    thread.start()
    deadline = time.monotonic() + 5
    while live.store.state()["ui"]["open_polls"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    add_comment(live)
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 3
    assert results[0]["changed"] is True and results[0]["version"] == version + 1
    ui = live.get("/api/state").json["ui"]
    assert ui["connected"] is True and ui["last_seen"] is not None


def test_events_waiter_cap(live):
    live.httpd.max_waiters = 0
    try:
        started = time.monotonic()
        response = live.get("/api/events?timeout=10").json
    finally:
        live.httpd.max_waiters = server.MAX_WAITERS
    assert time.monotonic() - started < 2
    assert response["changed"] is False and response["retry_after"] == 5 and "version" in response


# --------------------------------------------------------------------------- loading and lifecycle

def test_503_while_loading_except_whitelisted_routes(loading):
    review = loading.get("/api/review").json
    assert review["loading"] is True and review["commits"] == [] and review["range"]["spec"] == "main..feature"
    state = loading.get("/api/state").json
    assert state["loading"] is True and state["commits"] == 0
    events = loading.get("/api/events?timeout=0").json
    assert events["loading"] is True and events["changed"] is False
    for path in ("/api/commits/combined", "/api/commits/combined/file?path=src/app.py", "/api/compare?head=%s" % "a" * 40,
                 "/api/file?rev=worktree&path=x", "/api/comments"):
        response = loading.get(path)
        assert response.status == 503 and response.json == {"error": "loading"}, path
    assert loading.post("/api/comments", {"body": "x", "anchor": {"kind": "review"}}).status == 503
    assert loading.post("/api/submit", {"verdict": "approve"}).status == 503
    assert loading.get("/").status == 200

    loader = threading.Thread(target=loading.store.load)
    loader.start()
    loader.join(30)
    assert loading.get("/api/commits/combined").status == 200
    assert loading.get("/api/review").json["loading"] is False


def test_shutdown_route_wakes_polls_and_stops_the_server(fixture_repo):
    store = open_store(fixture_repo)
    srv = Live(fixture_repo, store, make_server(store, TOKEN, 0))
    try:
        version = srv.get("/api/state").json["version"]
        results = []
        poller = threading.Thread(target=lambda: results.append(srv.get("/api/events?since=%d&timeout=20" % version).json))
        poller.start()
        deadline = time.monotonic() + 5
        while store.state()["ui"]["open_polls"] == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        response = srv.post("/api/shutdown")
        assert response.status == 202 and response.json == {"stopping": True}
        poller.join(5)
        assert not poller.is_alive() and results[0]["changed"] is True
        srv.thread.join(5)
        assert not srv.thread.is_alive() and store.stopping is True
        srv.httpd.server_close()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", srv.port), timeout=1).close()
    finally:
        srv.close()


def test_idle_timeout_shuts_the_server_down(fixture_repo):
    store = open_store(fixture_repo)
    httpd = make_server(store, TOKEN, 0, idle_timeout=0.3)
    httpd.log_stream = io.StringIO()
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        httpd.start_idle_watcher()
        thread.join(5)
        assert not thread.is_alive() and store.stopping is True
        assert "shutting down" in httpd.log_stream.getvalue()
    finally:
        httpd.shutdown()
        httpd.server_close()
        store.close()


def test_make_server_binds_loopback_only(live):
    assert live.httpd.server_address[0] == "127.0.0.1" and live.httpd.port > 0
    assert live.httpd.url == "http://127.0.0.1:%d/" % live.port
    assert live.httpd.daemon_threads is True and live.httpd.allow_reuse_address is True
    assert server.RequestHandler.protocol_version == "HTTP/1.1" and server.RequestHandler.timeout == 60
    assert server.RequestHandler.server_version == "ccr/" + __version__ and server.RequestHandler.sys_version == ""
    assert os.path.isfile(os.path.join(server.STATIC_DIR, "index.html"))


# --------------------------------------------------------------------------- malformed inputs never 500

def test_malformed_origin_referer_and_target_are_client_errors(live):
    """Unbalanced IPv6 brackets make urlsplit raise; that must surface as 403/400, not a 500 + traceback."""
    import socket
    assert live.get("/", headers={"Origin": "http://[::1]evil.com"}, token=None).status == 403
    assert live.get("/static/app.js", headers={"Referer": "http://]::1["}, token=None).status == 403
    sock = socket.create_connection(("127.0.0.1", live.port), timeout=10)
    try:
        sock.sendall(b"GET http://[::1x/ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        head = sock.recv(4096)
    finally:
        sock.close()
    assert head.startswith(b"HTTP/1.1 400"), head[:80]


def test_non_ascii_content_length_and_absurd_json_nesting_are_client_errors(live):
    superscript_two = live.head_only("POST", "/api/comments", {"Content-Length": "²", "Content-Type": "application/json"})
    assert superscript_two.status == 411
    deep = live.request("POST", "/api/comments", raw=b"[" * 200000, headers={"Content-Type": "application/json"})
    assert deep.status == 400 and deep.json == {"error": "body must be a UTF-8 encoded JSON object"}


def test_cover_route(live):
    assert live.get("/api/review").json["cover"] == ""
    before = live.get("/api/state").json
    response = live.post("/api/cover", {"text": "# Retry logic\n\nWhy this change exists.\n"})
    assert response.status == 200 and response.json["cover"] == "# Retry logic\n\nWhy this change exists."
    review = live.get("/api/review").json
    assert review["cover"] == "# Retry logic\n\nWhy this change exists."
    assert review["version"] == before["version"] + 1 and review["generation"] == before["generation"] + 1
    assert live.post("/api/cover", {"text": 42}).status == 400


def test_comments_route_projects_into_a_view(live):
    created = add_comment(live)
    plain = live.get("/api/comments").json["comments"][0]
    assert "view_anchor" not in plain
    projected = live.get("/api/comments?project=combined").json["comments"][0]
    assert projected["id"] == created["id"] and projected["projected"] is True
    assert projected["view_anchor"]["commit"] == "combined" and projected["view_anchor"]["path"] == "src/app.py"
    assert live.get("/api/comments?project=nosuchview").status == 404


def test_pr_mode_routes(live):
    repo = live.repo
    anchor = line_anchor(repo.sha(THREE_HUNKS), "src/app.py", 5)
    outside = live.post("/api/comments", {"body": "x", "anchor": anchor, "github": True})
    assert outside.status == 409 and "not linked to a GitHub pull request" in outside.json["error"]
    assert live.post("/api/pr", {"url": "not a pr"}).status == 400
    before = live.get("/api/state").json
    linked = live.post("/api/pr", {"url": "https://github.com/o/r/pull/7/files"})
    assert linked.status == 200 and linked.json["pr"]["url"] == "https://github.com/o/r/pull/7"
    review = live.get("/api/review").json
    assert review["pr"]["number"] == 7 and review["generation"] == before["generation"] + 1

    root = add_comment(live, "Why 500?", anchor, github=True)
    assert root["github"] == {"status": "local"}
    assert live.post("/api/comments", {"body": "x", "anchor": anchor, "github": "yes"}).status == 400
    refused = live.post("/api/comments", {"body": "x", "anchor": line_anchor(repo.sha(THREE_HUNKS), "src/app.py", 10),
                                          "github": True})
    assert refused.status == 400 and "is not in the pull request diff" in refused.json["error"]

    target = live.get("/api/comments/%s/github" % root["id"])
    assert target.status == 200 and target.json["line"] == 5 and target.json["side"] == "RIGHT"
    assert target.json["body"] == "Why 500?" and target.json["commit"] == repo.feature
    question = add_comment(live, "What is this?")
    assert live.get("/api/comments/%s/github" % question["id"]).status == 409
    assert live.get("/api/comments/nope/github").status == 404
    switched = live.request("PATCH", "/api/comments/%s" % question["id"], body={"github": True})
    assert switched.status == 200 and switched.json["github"] == {"status": "local"}

    posted = live.post("/api/comments/%s/github" % root["id"],
                       {"posted": {"url": "https://github.com/o/r/pull/7#discussion_r1", "comment_id": 1}})
    assert posted.status == 200 and posted.json["github"]["status"] == "posted"
    assert posted.json["github"]["url"] == "https://github.com/o/r/pull/7#discussion_r1"
    again = live.post("/api/comments/%s/github" % root["id"], {"posted": {"url": "https://github.com/x"}})
    assert again.status == 409
    frozen = live.request("PATCH", "/api/comments/%s" % root["id"], body={"body": "Reworded"})
    assert frozen.status == 409 and "change it there" in frozen.json["error"]
    assert live.post("/api/comments/%s/github" % question["id"], {}).status == 400
    assert live.request("PUT", "/api/comments/%s/github" % root["id"]).status in (404, 501)


def test_github_sync_route_and_replies_to_mirrored_threads(live):
    payload = {"viewer": "reviewer", "head": live.repo.feature, "reviews": [], "threads": [
        {"id": "T1", "path": "src/app.py", "line": 5, "start_line": None, "original_line": 5, "original_start_line": None,
         "side": "RIGHT", "subject_type": "LINE", "outdated": False, "resolved": False,
         "comments": [{"id": "C1", "database_id": 1, "body": "Why 500?", "url": "https://github.com/o/r/pull/7#discussion_r1",
                       "created_at": "2026-09-01T10:00:00Z", "edited_at": None, "state": "SUBMITTED", "login": "nyh",
                       "reply_to": None}]}]}
    assert live.post("/api/github/sync", payload).status == 409, "not in PR mode"
    live.post("/api/pr", {"url": "o/r#7"})
    assert live.post("/api/github/sync", {"threads": "nope"}).status == 400
    synced = live.post("/api/github/sync", payload)
    assert synced.status == 200 and synced.json["added"] == 1 and synced.json["threads"] == 1
    assert live.get("/api/state").json["pr_synced_at"] == synced.json["synced_at"]
    root = next(c for c in live.get("/api/comments").json["comments"] if c["author"] == "github")
    assert live.request("DELETE", "/api/comments/%s" % root["id"]).status == 409
    reply = live.post("/api/comments", {"body": "Agreed.", "parent_id": root["id"], "github": True})
    assert reply.status == 201 and reply.json["github"] == {"status": "local"}
    target = live.get("/api/comments/%s/github" % reply.json["id"]).json
    assert (target["subject_type"], target["thread_id"], target["reply_to"]) == ("REPLY", "T1", "C1")

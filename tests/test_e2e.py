"""Browser end-to-end test (SPEC.md section 9, ``test_e2e.py``).

Starts a real ``ccr`` HTTP server in-process on a free port against the fixture repository, runs
``tests/e2e/driver.mjs`` (headless Chromium driven over the DevTools protocol from Node >= 22, no
dependencies) and checks the driver's JSON report plus the review state the browser left behind on
the server.  Skipped when no Chromium binary or ``node`` is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading

import pytest

from ccr import __version__
from ccr.server import make_server
from ccr.store import ReviewStore, utcnow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, "tests", "e2e", "driver.mjs")
TOKEN = "e2e0123456789abcdef0123456789abc"
COVER = "# Why\n\nThe **cover** letter set by the e2e driver.\n\n- retries\n- `backoff`"  # mirrors COVER in driver.mjs
PR_URL = "https://github.com/o/r/pull/7"  # mirrors PR_URL in driver.mjs
CHROME_CANDIDATES = ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable")
DRIVER_TIMEOUT = 240


def chromium_binary():
    """The Chromium executable the driver will use, or None (``CCR_CHROME`` overrides the search)."""
    explicit = os.environ.get("CCR_CHROME")
    if explicit:
        return explicit if shutil.which(explicit) else None
    return next((c for c in CHROME_CANDIDATES if shutil.which(c)), None)


pytestmark = pytest.mark.skipif(
    chromium_binary() is None or shutil.which("node") is None,
    reason="headless Chromium (chromium-browser/chromium/google-chrome) and node are required",
)


class LiveServer:
    """A serving ``ReviewServer`` on an ephemeral port with an in-memory store."""

    def __init__(self, repo, pr=None):
        self.store = ReviewStore(repo.path, "main..feature", None, worktree=True, db_path=":memory:")
        if pr:
            self.store.set_pr(pr)
        self.store.load()
        self.httpd = make_server(self.store, TOKEN, 0)
        self.store.set_server_info({"pid": os.getpid(), "port": self.httpd.port, "started_at": utcnow(),
                                    "version": __version__})
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "%s?t=%s" % (self.httpd.url, TOKEN)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()


@pytest.fixture
def live(fixture_repo):
    server = LiveServer(fixture_repo)
    yield server
    server.close()


def run_driver(url: str, shots_dir: str, scenario: str = "review") -> dict:
    """Run the CDP driver in its own process group (so a timeout also kills the browser) and parse its report."""
    proc = subprocess.Popen(["node", DRIVER, url, shots_dir, scenario], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=DRIVER_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        pytest.fail("driver timed out after %d s\nstdout:\n%s\nstderr:\n%s" % (DRIVER_TIMEOUT, stdout, stderr[-4000:]))
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert lines, "driver printed no JSON report (exit %d)\nstdout:\n%s\nstderr:\n%s" % (proc.returncode, stdout, stderr[-4000:])
    report = json.loads(lines[-1])
    report["exit_code"] = proc.returncode
    report["stderr"] = stderr[-4000:]
    return report


def test_browser_review_flow(live, tmp_path):
    shots_dir = tmp_path / "shots"
    report = run_driver(live.url, str(shots_dir))
    pretty = json.dumps({k: v for k, v in report.items() if k != "stderr"}, indent=1, ensure_ascii=False)

    failed = [s for s in report["steps"] if not s["ok"]]
    assert not failed, "failed steps: %s\n%s\n%s" % ([s["name"] for s in failed], pretty, report["stderr"])
    assert report["consoleErrors"] == [], pretty
    assert report["ok"] is True and report["exit_code"] == 0, pretty
    expected_steps = ["load page", "hljs languages", "first render shows All changes", "cover letter and review comment",
                      "click 2nd commit", "hover row and click [+]", "type and submit comment", "Write/Preview tabs, auto-grow and Cancel keeps draft",
                      "thread projected into All changes", "toggle split view keeps thread",
                      "drag 3-line range and comment", "submit round from the top bar", "claude reply → toast + New dot",
                      "reload keeps token"]
    assert [s["name"] for s in report["steps"]] == expected_steps, pretty
    assert len(report["screenshots"]) == 9 and all(os.path.getsize(p) > 1000 for p in report["screenshots"]), pretty

    # -- the state the browser left on the server: the cover letter, and one round (rounds carry no verdict: the UI
    #    always sends "comment" and no summary) bundling three user comments — a whole-series comment and two line
    #    comments — plus Claude's reply
    review = live.store.review()
    assert review["cover"] == COVER
    assert len(review["rounds"]) == 1, pretty
    rnd = review["rounds"][0]
    assert rnd["number"] == 1 and rnd["verdict"] == "comment" and rnd["summary"] == ""
    assert rnd["head"] == review["range"]["head"] and len(rnd["commit_shas"]) == 7

    comments = live.store.list_comments()
    by_author = {a: [c for c in comments if c["author"] == a] for a in ("user", "claude")}
    assert len(by_author["user"]) == 3 and len(by_author["claude"]) == 1, pretty
    assert review["counts"] == {"pending": 0, "submitted": 3, "unresolved": 3, "total": 4, "outdated": 0}
    assert set(rnd["comment_ids"]) == {c["id"] for c in comments}

    second = review["commits"][1]  # combined first, then the first real commit ("Modify app in three hunks")
    roots = sorted((c for c in by_author["user"] if c["anchor"]["kind"] == "line"), key=lambda c: c["anchor"]["line"])
    assert [(c["anchor"]["commit"], c["anchor"]["path"], c["anchor"]["side"], c["anchor"]["start_line"], c["anchor"]["line"])
            for c in roots] == [(second["sha"], "src/app.py", "new", 2, 4), (second["sha"], "src/app.py", "new", None, 5)]
    assert roots[0]["snippet"] == "value_02 = 2\nvalue_03 = 3\nvalue_04 = 4"
    assert roots[1]["snippet"] == "value_05 = 500  # changed" and roots[1]["body"] == "First **e2e** comment with `code`."
    assert all(c["state"] == "submitted" and c["round"] == 1 for c in comments)
    review_level = sorted((c for c in by_author["user"] if c["anchor"]["kind"] == "review"), key=lambda c: c["created_at"])
    assert [c["body"] for c in review_level] == ["Whole-series comment from the e2e driver."]
    assert all(c["anchor"] == {"kind": "review", "commit": None, "path": None, "side": None, "line": None, "start_line": None}
               and c["snippet"] == "" for c in review_level)
    reply = by_author["claude"][0]
    assert reply["parent_id"] == roots[0]["id"] and reply["anchor"] == roots[0]["anchor"]
    assert reply["body"] == "Fixed in the next commit."


@pytest.fixture
def live_pr(fixture_repo):
    server = LiveServer(fixture_repo, pr=PR_URL)
    yield server
    server.close()


def test_browser_pr_mode_flow(live_pr, tmp_path):
    report = run_driver(live_pr.url, str(tmp_path / "shots"), "pr")
    pretty = json.dumps({k: v for k, v in report.items() if k != "stderr"}, indent=1, ensure_ascii=False)
    failed = [s for s in report["steps"] if not s["ok"]]
    assert not failed, "failed steps: %s\n%s\n%s" % ([s["name"] for s in failed], pretty, report["stderr"])
    assert report["consoleErrors"] == [] and report["ok"] is True and report["exit_code"] == 0, pretty
    assert [s["name"] for s in report["steps"]] == [
        "load page in PR mode", "question about the whole pull request", "click 2nd commit",
        "gutter forks into Ask AI and GH comment", "split view: the buttons by the hovered side",
        "GitHub comment on a line", "question with the editor switch", "a draft keeps its kind",
        "a line outside the pull request diff stays a question",
        "file header forks too", "posted comment links to GitHub", "GitHub threads come into ccr",
        "GH reply in a question thread", "Bold and Italic on the selected text"], pretty
    assert len(report["screenshots"]) == 4, pretty

    # -- what the browser left: eight pending user comments, four of them for GitHub (one recorded as posted, one a
    #    reply in the mirrored thread, which holds a question too, one a reply in a question thread), next to the five
    #    comments mirrored from GitHub
    everything = live_pr.store.list_comments()
    comments = {c["body"]: c for c in everything if c["author"] == "user"}
    assert set(comments) == {"Why does the series need two commits?", "Why 500?", "What is value 6 for?",
                             "Unrelated to the change", "Please split this file.", "Agreed, see the design.",
                             "Which spec does radek mean?", "Should value 6 be named?"}, pretty
    assert all(c["state"] == "pending" for c in comments.values())
    mirrored = [c for c in everything if c["author"] == "github"]
    assert len(mirrored) == 5 and comments["Agreed, see the design."]["github"] == {"status": "local"}
    assert next(c for c in mirrored if c["github"]["node_id"] == "C4")["parent_id"] == comments["Why 500?"]["id"]
    assert comments["Agreed, see the design."]["parent_id"] == next(c["id"] for c in mirrored if c["github"]["node_id"] == "C1")
    assert comments["Which spec does radek mean?"]["github"] is None, "a question in a GitHub thread stays in ccr"
    assert comments["Which spec does radek mean?"]["parent_id"] == comments["Agreed, see the design."]["parent_id"]
    assert comments["Why 500?"]["github"]["status"] == "posted" and comments["Why 500?"]["anchor"]["line"] == 5
    assert comments["Please split this file."]["github"] == {"status": "local"}
    assert comments["Please split this file."]["anchor"]["kind"] == "file"
    for question in ("Why does the series need two commits?", "What is value 6 for?", "Unrelated to the change"):
        assert comments[question]["github"] is None, question
    assert comments["Unrelated to the change"]["anchor"]["line"] == 10
    assert comments["Should value 6 be named?"]["github"] == {"status": "local"}
    assert comments["Should value 6 be named?"]["parent_id"] == comments["What is value 6 for?"]["id"]

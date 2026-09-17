"""End-to-end tests for the ``ccr`` CLI (SPEC.md sections 6 and 9), driving ``bin/ccr`` as a subprocess.

Every test runs with ``CCR_SESSION_DIR`` pointing at a per-test directory (autouse fixture in
``conftest``) and a freshly built fixture repository.  Background servers started by ``ccr start`` are
tracked by the ``cli`` fixture, which kills any leftover ``ccr serve`` process at teardown.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from ccr import __version__
from conftest import FEATURE_SUBJECTS, run_git

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CCR = os.path.join(ROOT, "bin", "ccr")
THREE_HUNKS, RENAME, BINARY, EDIT_NONL, MERGE, EMPTY, BIG = FEATURE_SUBJECTS


def api(record: dict, method: str, path: str, body=None):
    """Direct API call with the session token (the browser's side of the conversation)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"X-CCR-Token": record["token"], "User-Agent": "test-browser/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    base = record["url"].split("?", 1)[0].rstrip("/")
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class Runner:
    """Runs ``bin/ccr`` for one repository and remembers the servers it started."""

    def __init__(self, repo, session_dir):
        self.repo = repo
        self.session_dir = session_dir
        self.pids = set()

    def run(self, *args, input=None, timeout=90, check=None, cwd=None, env=None) -> subprocess.CompletedProcess:
        argv = [CCR] + list(args)
        if "--repo" not in args and "--url" not in args:
            argv += ["--repo", self.repo.path]
        proc = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd or self.repo.path, env=env)
        if check is not None:
            assert proc.returncode == check, "ccr %s -> %d\nstdout:\n%s\nstderr:\n%s" % (
                " ".join(args), proc.returncode, proc.stdout, proc.stderr)
        return proc

    def start(self, *args) -> dict:
        proc = self.run("start", "--json", *args, check=0)
        record = json.loads(proc.stdout)
        self.pids.add(record["pid"])
        return record

    def popen(self, *args) -> subprocess.Popen:
        return subprocess.Popen([CCR] + list(args) + ["--repo", self.repo.path], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=self.repo.path)

    def leftover_pids(self) -> set:
        pids = set(self.pids)
        for name in os.listdir(self.session_dir):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(self.session_dir, name)) as handle:
                        pids.add(json.load(handle)["pid"])
                except (OSError, ValueError, KeyError):
                    pass
        return pids

    def cleanup(self) -> None:
        for pid in self.leftover_pids():
            if not pid_alive(pid):
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as handle:
                    cmdline = handle.read()
            except OSError:
                continue
            if b"ccr" in cmdline and b"serve" in cmdline:
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def cli(fixture_repo, ccr_session_dir):
    runner = Runner(fixture_repo, str(ccr_session_dir))
    yield runner
    runner.cleanup()


def comment_id(stdout: str) -> str:
    match = re.search(r"ccr: created comment ([0-9a-z]{6})", stdout)
    assert match, stdout
    return match.group(1)


# --------------------------------------------------------------------------- the full agent loop

def test_agent_loop_end_to_end(cli, fixture_repo, ccr_session_dir):
    repo = fixture_repo
    record = cli.start("--range", "main..feature", "--worktree")
    assert set(record) >= {"pid", "port", "token", "url", "repo", "range", "started_at", "log", "db", "counts", "commits"}
    assert record["commits"] == 7 and record["reused"] is False and record["repo"] == repo.path
    assert record["url"] == "http://127.0.0.1:%d/?t=%s" % (record["port"], record["token"])
    assert len(record["token"]) == 32

    # -- the token travels through the environment only; files are private
    with open("/proc/%d/cmdline" % record["pid"], "rb") as handle:
        cmdline = handle.read()
    assert record["token"].encode() not in cmdline
    assert b"serve" in cmdline and b"--range\0main..feature" in cmdline and b"--worktree" in cmdline
    session_file = os.path.join(str(ccr_session_dir), "%s.json" % os.path.basename(record["db"])[:-len(".sqlite")])
    assert os.path.isfile(session_file) and json.load(open(session_file))["pid"] == record["pid"]
    assert mode_of(session_file) == 0o600 and mode_of(record["log"]) == 0o600 and mode_of(record["db"]) == 0o600
    assert mode_of(str(ccr_session_dir)) == 0o700
    log_text = open(record["log"]).read()
    assert "ccr: serving %s  (main..feature, 7 commits, +worktree)" % repo.path in log_text
    assert "?t=<redacted>" in log_text and record["token"] not in log_text

    # -- status
    status = cli.run("status", check=0).stdout
    assert "ccr: url %s" % record["url"] in status
    assert "ccr: range main..feature (7 commits, +worktree)" in status
    assert "ccr: comments 0 pending" in status and "ccr: rounds 0" in status and "ccr: ui not opened yet" in status
    assert "ccr: log %s" % record["log"] in status and "ccr: db %s" % record["db"] in status
    status_json = json.loads(cli.run("status", "--json", check=0).stdout)
    assert status_json["session"]["pid"] == record["pid"] and status_json["review"]["counts"]["total"] == 0
    assert all("files" not in c for c in status_json["review"]["commits"])

    # -- comments created from the CLI
    three = repo.sha(THREE_HUNKS)
    user_line = comment_id(cli.run("comment", "--commit", three[:10], "--path", "src/app.py", "--line", "5",
                                   "--as", "user", "Why 500?", check=0).stdout)
    claude_range = comment_id(cli.run("comment", "--commit", "combined", "--path", "src/app.py", "--line", "5",
                                      "--start-line", "3", "Pre-annotation", check=0).stdout)
    review_level = comment_id(cli.run("comment", "--review", "--as", "user", "--file", "-", input="General remark\n",
                                      check=0).stdout)
    by_rev = json.loads(cli.run("comment", "--commit", "HEAD~1", "--json", "-", input="On the empty commit", check=0).stdout)
    assert by_rev["anchor"] == {"kind": "commit", "commit": repo.empty, "path": None, "side": None, "line": None,
                               "start_line": None}
    assert by_rev["author"] == "claude" and by_rev["state"] == "submitted" and by_rev["round"] == 0
    bad = cli.run("comment", "--commit", repo.root, "nope")
    assert bad.returncode == 1 and bad.stderr.startswith("ccr: ") and "not in the review" in bad.stderr
    usage = cli.run("comment", "--commit", three, "--line", "3", "text")
    assert usage.returncode == 1 and "--line requires --path" in usage.stderr

    # -- comments rendering: ids, numbered snippets, HEAD arrow
    listing = cli.run("comments", check=0).stdout
    assert listing.startswith("# Review comments — repo (main..feature) — 4 threads (2 pending, 4 unresolved, 2 unanswered)")
    assert "[id: %s] user · new:5 → HEAD src/app.py:5 · pending · unresolved · 0 replies · last: user" % user_line in listing
    assert "[id: %s] claude · new:3-5 → HEAD src/app.py:3-5 · R0 · unresolved" % claude_range in listing
    assert '## Commit %s — "%s"' % (three[:10], THREE_HUNKS) in listing and "## All changes (combined)" in listing
    assert ">       5   +value_05 = 500  # changed" in listing
    assert "   5        -value_05 = 5" in listing and ">  3    3    value_03 = 3" in listing
    assert "Why 500?" in listing and "General remark" in listing
    pending_only = cli.run("comments", "--pending", check=0).stdout
    assert user_line in pending_only and claude_range not in pending_only
    assert cli.run("comments", "--round", "7", check=0).stdout == "ccr: no comments match\n"
    no_snippets = cli.run("comments", "--no-snippets", "--path", "src/app.py", check=0).stdout
    assert "```diff" not in no_snippets and "★" in no_snippets and review_level not in no_snippets
    as_json = json.loads(cli.run("comments", "--json", "--author", "user", check=0).stdout)
    assert {t["root"] for t in as_json["threads"]} == {user_line, review_level}

    # -- wait in a thread, the browser submits a round
    results = {}

    def waiter():
        results["proc"] = cli.run("wait", "--timeout", "40", check=None)

    thread = threading.Thread(target=waiter)
    thread.start()
    assert wait_for(lambda: api(record, "GET", "/api/state")["ui"]["open_polls"] >= 1, timeout=15)
    submitted = api(record, "POST", "/api/submit", {"verdict": "request_changes", "summary": "Please fix"})
    assert submitted["number"] == 1
    thread.join(60)
    assert not thread.is_alive()
    waited = results["proc"]
    assert waited.returncode == 0, waited.stderr
    header, _, body = waited.stdout.partition("\n")
    assert header == "ccr: round 1 — request_changes — 3 new comments in 3 threads"
    assert body.startswith("# Review comments — repo (main..feature) — 3 threads")
    assert "★ new in round 1" in body and "[id: %s]" % user_line in body and claude_range not in body
    assert '- Round 1 · request_changes · ' in body and '"Please fix"' in body

    # -- wait times out with the exit code 2 message
    timed_out = cli.run("wait", "--timeout", "1")
    assert timed_out.returncode == 2 and timed_out.stdout == ""
    assert re.fullmatch(r"ccr: no new round after 1 s \(rounds: 1, pending unsubmitted: 0, version: \d+\)\n", timed_out.stderr)

    # -- replies: --resolve, duplicate refusal, --force, batch Markdown
    replied = cli.run("reply", user_line, "Fixed in abc1234", "--resolve", check=0).stdout
    assert re.fullmatch(r"%s: replied \([0-9a-z]{6}\), resolved\n" % user_line, replied)
    duplicate = cli.run("reply", user_line, "Fixed in abc1234")
    assert duplicate.returncode == 1 and duplicate.stdout == ""
    assert re.fullmatch(r"ccr: identical reply already exists on this thread \(id [0-9a-z]{6}\); use --force\n", duplicate.stderr)
    forced = cli.run("reply", user_line, "Fixed in abc1234", "--force", "--json", check=0)
    assert json.loads(forced.stdout)["parent_id"] == user_line
    batch = cli.run("reply", "--batch", "-", input="## %s [resolve]\nDone, see the docs.\n\n## nosuch\nNope\n" % review_level)
    assert batch.returncode == 1
    assert batch.stdout == "%s: replied, resolved\nnosuch: ERROR comment 'nosuch' not found\n" % review_level
    batch_json = cli.run("reply", "--batch", "-", "--json", input=json.dumps([{"id": claude_range, "body": "Ack"}]), check=0)
    assert json.loads(batch_json.stdout)[0]["ok"] is True

    # -- unanswered threads: the user's follow-up is listed, the resolved thread is not
    follow_up = comment_id(cli.run("comment", "--commit", three, "--path", "src/app.py", "--as", "user",
                                   "Also rename this file", check=0).stdout)
    unanswered = cli.run("comments", "--unanswered", check=0).stdout
    assert follow_up in unanswered and user_line not in unanswered and review_level not in unanswered
    assert "### (file) src/app.py" in unanswered
    assert "2 threads (1 pending, 2 unresolved, 2 unanswered)" in unanswered, "the round summary is unanswered too"
    assert "Please fix" in unanswered

    # -- resolve / unresolve / edit / move / delete
    assert cli.run("resolve", follow_up, check=0).stdout == "%s: resolved\n" % follow_up
    assert cli.run("unresolve", follow_up, check=0).stdout == "%s: unresolved\n" % follow_up
    assert cli.run("edit", follow_up, "Also rename this module", check=0).stdout == "%s: edited\n" % follow_up
    moved = cli.run("move", follow_up, "--commit", three, "--path", "src/app.py", "--line", "6", check=0).stdout
    assert moved == "%s: moved to %s src/app.py new:6\n" % (follow_up, three[:10])
    partial = cli.run("delete", follow_up, "nosuch")
    assert partial.returncode == 1 and partial.stdout == "%s: deleted\nnosuch: ERROR comment 'nosuch' not found\n" % follow_up
    cascade_refused = cli.run("delete", user_line)
    assert cascade_refused.returncode == 1 and "thread has replies" in cascade_refused.stdout

    # -- export
    exported = cli.run("export", check=0).stdout
    assert exported.startswith("# Review — repo (main..feature, base %s → head %s) — exported " % (repo.main[:10], repo.feature[:10]))
    assert "## Rounds" in exported and "[id: %s]" % user_line in exported
    target = os.path.join(str(ccr_session_dir), "..", "review.md")
    assert cli.run("export", "--md", "-o", target, check=0).stdout == "ccr: exported to %s\n" % target
    assert mode_of(target) == 0o600 and open(target).read().startswith("# Review — repo (main..feature, base ")
    export_json = json.loads(cli.run("export", "--json", check=0).stdout)
    assert set(export_json) == {"review", "rounds", "comments", "threads"} and len(export_json["rounds"]) == 1

    # -- new commit + reload, then an amend that keeps the subject → remap
    repo.git("reset", "-q")
    repo.git("commit", "-q", "--allow-empty", "-m", "Follow-up")
    reload_out = cli.run("reload", check=0).stdout
    assert reload_out == "ccr: 8 commits (was 7), +1 −0, 0 comments remapped, 0 now outdated\n"
    note = comment_id(cli.run("comment", "--commit", "HEAD", "Note on the follow-up", check=0).stdout)
    run_git(repo.path, ["commit", "-q", "--amend", "--no-edit", "--allow-empty"],
            extra_env={"GIT_COMMITTER_DATE": "@1800000000 +0000"})
    remapped = cli.run("reload", check=0).stdout
    assert remapped == ("ccr: 8 commits (was 8), +1 −1, 1 comments remapped, 0 now outdated\n"
                        "warning: 1 reviewed commits left the range\n")
    moved_note = next(c for c in api(record, "GET", "/api/comments")["comments"] if c["id"] == note)
    assert moved_note["anchor"]["commit"] == repo.text("rev-parse", "HEAD") and moved_note["moved_from"]["commit"] != moved_note["anchor"]["commit"]
    narrowed = json.loads(cli.run("reload", "--json", "--range", "%s..feature" % repo.sha(BIG), check=0).stdout)
    assert narrowed["commits_removed"] == 7
    outdated_lines = cli.run("reload", check=0).stdout.splitlines()
    assert outdated_lines[0].startswith("ccr: 1 commits (was 1), +0 −0, 0 comments remapped, ")
    assert any(line.startswith("  %s · %s src/app.py new:5 · \"value_05 = 500  # changed\"" % (user_line, three[:10]))
               for line in outdated_lines)
    assert cli.run("reload", "--range", "main..feature", "--json", check=0).returncode == 0

    # -- start again reuses (and reloads) the running session
    reused = cli.run("start", "--range", "main..feature", "--worktree", check=0).stdout.splitlines()
    assert reused[0] == "ccr: reusing running session (pid %d)" % record["pid"]
    assert reused[1].startswith("ccr: 8 commits (was 8), +0 −0, ")
    assert reused[-1] == "ccr: url %s" % record["url"]
    assert cli.start("--range", "main..feature")["reused"] is True

    # -- sessions
    sessions = cli.run("sessions", check=0).stdout
    assert sessions.startswith("%s → %s  (main..feature, alive, started " % (repo.path, record["url"]))
    sessions_json = json.loads(cli.run("sessions", "--json", check=0).stdout)
    assert sessions_json[0]["pid"] == record["pid"] and sessions_json[0]["alive"] is True

    # -- logs
    logs = cli.run("logs", "-n", "3", check=0).stdout
    assert "ccr: url http://127.0.0.1:%d/?t=<redacted>" % record["port"] in logs

    # -- stop: export written, files gone, process gone
    stopped = cli.run("stop", check=0).stdout.splitlines()
    assert re.fullmatch(r"ccr: exported to %s/%s-\d{8}-\d{6}\.md" % (re.escape(str(ccr_session_dir)), re.escape(os.path.basename(session_file)[:-5])), stopped[0])
    export_path = stopped[0][len("ccr: exported to "):]
    assert os.path.isfile(export_path) and mode_of(export_path) == 0o600
    assert open(export_path).read().startswith("# Review — repo (main..feature, base ")
    assert stopped[1] == "ccr: stopped %s (pid %d)" % (repo.path, record["pid"])
    assert not os.path.exists(session_file) and not os.path.exists(record["db"])
    assert os.path.exists(record["log"])
    assert wait_for(lambda: not pid_alive(record["pid"]), timeout=10)
    after = cli.run("status")
    assert after.returncode == 3 and after.stderr.startswith("ccr: no running session for %s" % repo.path)
    assert cli.run("sessions").returncode == 3


# --------------------------------------------------------------------------- start failure modes

def test_start_with_a_bad_range_fails_immediately(cli, ccr_session_dir):
    started = time.monotonic()
    proc = cli.run("start", "--range", "nosuchref..HEAD")
    assert proc.returncode == 1 and time.monotonic() - started < 8
    assert proc.stdout == "" and proc.stderr == "ccr: unknown revision 'nosuchref'\n"
    assert not any(name.endswith(".json") for name in os.listdir(str(ccr_session_dir)))
    empty = cli.run("start", "--range", "feature..feature")
    assert empty.returncode == 1 and "is empty" in empty.stderr


def test_start_reports_a_crashing_server_with_the_log_tail(cli, ccr_session_dir):
    proc = cli.run("start", "--range", "main..feature", "--db", "/nonexistent-ccr-dir/review.sqlite")
    assert proc.returncode == 1
    lines = proc.stderr.splitlines()
    assert lines[0] == "ccr: server exited with code 1 — last log lines:"
    assert any("nonexistent-ccr-dir" in line for line in lines[1:])
    assert not any(name.endswith(".json") for name in os.listdir(str(ccr_session_dir)))
    assert cli.leftover_pids() == set()


def test_start_with_an_explicit_busy_port_fails(cli):
    import socket
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        proc = cli.run("start", "--range", "main..feature", "--port", str(port))
    finally:
        blocker.close()
    assert proc.returncode == 1 and proc.stderr == "ccr: port %d in use\n" % port


def test_no_session_and_no_repository_messages(cli, tmp_path):
    proc = cli.run("status")
    assert proc.returncode == 3
    assert proc.stderr == "ccr: no running session for %s — use --repo PATH or run ccr start (live sessions: none)\n" % cli.repo.path
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = subprocess.run([CCR, "status"], cwd=str(outside), capture_output=True, text=True)
    assert proc.returncode == 1
    assert proc.stderr == "ccr: %s is not inside a git repository; pass --repo PATH\n" % os.path.realpath(str(outside))
    assert cli.run("comments", "--url", "http://127.0.0.1:1", "--token", "x").returncode == 3
    missing = cli.run("comments", "--url", "http://127.0.0.1:1")
    assert missing.returncode == 1 and "token" in missing.stderr
    assert subprocess.run([CCR], capture_output=True, text=True).returncode == 1
    version = subprocess.run([CCR, "--version"], capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.strip() == "ccr " + __version__


def test_stale_session_file_is_removed(cli, ccr_session_dir):
    record = cli.start("--range", "main..feature")
    session_file = os.path.join(str(ccr_session_dir), "%s.json" % os.path.basename(record["db"])[:-len(".sqlite")])
    os.kill(record["pid"], signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(record["pid"]))
    proc = cli.run("status")
    assert proc.returncode == 3 and not os.path.exists(session_file)
    fresh = cli.start("--range", "main..feature")
    assert fresh["pid"] != record["pid"] and fresh["reused"] is False


def test_wait_detects_a_vanished_server(cli, ccr_session_dir):
    record = cli.start("--range", "main..feature")
    proc = cli.popen("wait", "--timeout", "60")
    assert wait_for(lambda: api(record, "GET", "/api/state")["ui"]["open_polls"] >= 1, timeout=15)
    os.kill(record["pid"], signal.SIGKILL)
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 3 and stderr == "ccr: server gone\n" and stdout == ""
    assert not any(name.endswith(".json") for name in os.listdir(str(ccr_session_dir)))


def test_wait_any_reports_changes_and_ui_tracking(cli):
    record = cli.start("--range", "main..feature", "--worktree")
    proc = cli.popen("wait", "--any", "--timeout", "60")
    assert wait_for(lambda: api(record, "GET", "/api/state")["ui"]["open_polls"] >= 1, timeout=15)
    created = api(record, "POST", "/api/comments", {"body": "From the browser", "anchor": {"kind": "review"}})
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr
    first = stdout.splitlines()[0]
    assert re.fullmatch(r"ccr: version \d+→\d+ · pending 1 · unresolved 1 · rounds 0", first)
    assert "[id: %s]" % created["id"] in stdout and "★" in stdout
    assert "ccr: ui not opened yet" in cli.run("status", check=0).stdout, "CLI polls never count as a browser"
    api(record, "GET", "/api/events?timeout=0")
    status = cli.run("status", check=0).stdout
    assert "ccr: ui connected (last seen " in status
    assert cli.run("stop", check=0).returncode == 0


def test_serve_foreground_with_explicit_token(cli, fixture_repo, ccr_session_dir):
    proc = subprocess.Popen([CCR, "serve", "--repo", fixture_repo.path, "--range", "main..feature", "--port", "0",
                             "--token", "f" * 32, "--verbose", "--db", ":memory:"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        session_file = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and session_file is None:
            candidates = [n for n in os.listdir(str(ccr_session_dir)) if n.endswith(".json")]
            if candidates:
                session_file = os.path.join(str(ccr_session_dir), candidates[0])
            else:
                time.sleep(0.05)
        assert session_file is not None
        record = json.load(open(session_file))
        assert record["pid"] == proc.pid and record["token"] == "f" * 32 and record["db"] == ":memory:" and record["log"] is None
        cli.pids.add(proc.pid)
        assert wait_for(lambda: not api(record, "GET", "/api/state")["loading"], timeout=30)
        status = cli.run("status", check=0).stdout
        assert "ccr: db :memory:" in status and "ccr: log none" in status
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0
        assert "ccr: url http://127.0.0.1:%d/?t=%s" % (record["port"], "f" * 32) in stdout
        assert "GET /api/state 200 " in stderr and "?" not in stderr.split("GET /api/state")[1].split("\n")[0]
        assert not os.path.exists(session_file)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_python_module_entry_point_matches_the_shim(fixture_repo):
    proc = subprocess.run([sys.executable, "-m", "ccr", "sessions"], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=ROOT))
    assert proc.returncode == 3 and proc.stderr == "ccr: no running sessions\n"


def test_shim_and_server_ignore_a_ccr_package_in_the_cwd(cli, tmp_path):
    """A reviewed repository may contain its own ``ccr/`` directory (this project does); neither the shim nor
    the background server may import it instead of the real package."""
    from ccr import __version__, session

    decoy = tmp_path / "decoy"
    (decoy / "ccr").mkdir(parents=True)
    (decoy / "ccr" / "__init__.py").write_text('__version__ = "decoy"\n')
    (decoy / "ccr" / "__main__.py").write_text('raise SystemExit("decoy package executed")\n')

    version = cli.run("--version", cwd=str(decoy), check=0)
    assert version.stdout.strip() == "ccr " + __version__

    proc = cli.run("start", "--json", "--range", "main..feature", cwd=str(decoy), check=0)
    record = json.loads(proc.stdout)
    cli.pids.add(record["pid"])
    assert session.cmdline_is_ccr_serve(record["pid"])
    status = json.loads(cli.run("status", "--json", cwd=str(decoy), check=0).stdout)
    assert status["review"]["server"]["version"] == __version__
    cli.run("stop", check=0)
    assert not pid_alive(record["pid"])


def test_reload_refreshes_the_session_record(cli, ccr_session_dir):
    """`ccr sessions` reads the range from the session file, so a reload must rewrite it."""
    record = cli.start("--range", "main..feature")
    cli.run("reload", "-n", "1", check=0)
    names = [n for n in os.listdir(str(ccr_session_dir)) if n.endswith(".json")]
    with open(os.path.join(str(ccr_session_dir), names[0])) as handle:
        assert json.load(handle)["range"] == "-n 1"
    assert "-n 1" in cli.run("sessions", check=0).stdout
    cli.run("stop", check=0)
    assert not pid_alive(record["pid"])


def test_cover_letter_via_start_and_cover_command(cli, tmp_path):
    cover = tmp_path / "cover.md"
    cover.write_text("# Why\n\nThis chain adds retries.\n")
    record = cli.start("--range", "main..feature", "--cover", str(cover))
    assert api(record, "GET", "/api/review")["cover"] == "# Why\n\nThis chain adds retries."
    assert cli.run("cover", "-", input="Rewritten cover.", check=0).stdout == "ccr: cover letter set (16 characters)\n"
    exported = cli.run("export", check=0).stdout
    assert "## Cover letter\n\nRewritten cover.\n" in exported
    cli.run("stop", check=0)
    assert not pid_alive(record["pid"])


def test_wait_reports_a_replaced_server_and_keeps_its_record(cli, ccr_session_dir):
    """Restarting the server (stop --keep-db; start) while `ccr wait` polls must not look like a plain API error."""
    first = cli.start("--range", "main..feature")
    waiter = cli.popen("wait", "--since-round", "0", "--timeout", "30")
    time.sleep(1.0)
    cli.run("stop", "--keep-db", check=0)
    second = cli.start("--range", "main..feature")
    out, errtext = waiter.communicate(timeout=40)
    assert waiter.returncode == 3, (out, errtext)
    assert "server was replaced" in errtext or "server gone" in errtext
    names = [n for n in os.listdir(str(ccr_session_dir)) if n.endswith(".json")]
    with open(os.path.join(str(ccr_session_dir), names[0])) as handle:
        assert json.load(handle)["pid"] == second["pid"]        # the new server's record survived
    assert cli.run("status", check=0).returncode == 0
    cli.run("stop", check=0)
    assert not pid_alive(first["pid"]) and not pid_alive(second["pid"])


def test_a_new_review_does_not_inherit_the_one_left_in_the_database(cli, fixture_repo):
    """SPEC 4.6: a server that dies without `ccr stop` leaves its database behind; the next review starts clean."""
    first = cli.start("--range", "main..feature")
    cli.run("comment", "--commit", fixture_repo.sha(FEATURE_SUBJECTS[0]), "on the feature chain", check=0)
    assert api(first, "POST", "/api/submit", {"verdict": "comment", "summary": "round one"})["number"] == 1
    os.kill(first["pid"], signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(first["pid"]))

    second = cli.start("--range", "main~1..main")
    assert second["review"]["id"] == 2 and second["review"]["resumed"] is False
    assert second["review"]["previous"] == {"id": 1, "started_at": second["review"]["previous"]["started_at"],
                                            "range": "main..feature", "comments": 2, "rounds": 1}
    assert second["counts"]["total"] == 0 and second["rounds"] == 0
    assert cli.run("comments", check=0).stdout == "ccr: no comments match\n"
    reloaded = cli.run("reload", check=0).stdout
    assert "0 now outdated" in reloaded and "left the range" not in reloaded
    assert api(second, "POST", "/api/submit", {"verdict": "comment", "summary": "its own first round"})["number"] == 1

    cli.run("stop", "--keep-db", check=0)
    resumed = cli.start("--range", "main~1..main")
    assert resumed["review"]["id"] == 2 and resumed["review"]["resumed"] is True
    assert resumed["counts"]["total"] == 1 and resumed["rounds"] == 1
    cli.run("stop", check=0)
    assert not pid_alive(second["pid"]) and not pid_alive(resumed["pid"])

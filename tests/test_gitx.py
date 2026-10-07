"""Tests for ``ccr.gitx`` (SPEC.md section 3) against the section-9 fixture repository.

The first group re-verifies the git byte layouts the parsers rely on against the installed git, so
a git upgrade that changes them fails loudly here rather than deep inside a parser.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap

import pytest

from ccr import gitx
from ccr.gitx import GitError
from conftest import (
    APP_ORIGINAL,
    DOCUMENT_USAGE_BODY,
    EMPTY_TREE_SHA1,
    FEATURE_SUBJECTS,
    FIRST_EPOCH,
    HOTFIX_SUBJECT,
    MAIN_SUBJECTS,
    UNICODE_PATH,
    run_git,
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
F1, F2, F3, F4, MERGE, EMPTY, BIG = FEATURE_SUBJECTS
NUMSTAT_ARGS = ["diff", "--numstat", "-z", "-M", "-C", "--no-ext-diff", "--no-textconv", "--no-color"]


# --------------------------------------------------------------------------- helpers

def numstat(repo, old, new):
    """``[(path, additions, deletions, binary)]`` of ``git diff --numstat -z`` (renames report the new path)."""
    tokens = repo.git(*NUMSTAT_ARGS, old, new).split(b"\0")
    tokens.pop()
    out, i = [], 0
    while i < len(tokens):
        add, dele, path = tokens[i].split(b"\t", 2)
        if path == b"":
            path, i = tokens[i + 2], i + 3
        else:
            i += 1
        binary = add == b"-"
        out.append((path.decode(), 0 if binary else int(add), 0 if binary else int(dele), binary))
    return out


def name_status(repo, old, new):
    """``{new_path: status_letter}`` from ``git diff --name-status -z``."""
    tokens = repo.git("diff", "--name-status", "-z", "-M", "-C", old, new).split(b"\0")
    tokens.pop()
    out, i = {}, 0
    while i < len(tokens):
        status = tokens[i].decode()[0]
        if status in "RC":
            out[tokens[i + 2].decode()] = status
            i += 3
        else:
            out[tokens[i + 1].decode()] = status
            i += 2
    return out


def stat_tuple(f):
    return (f["path"], f["additions"], f["deletions"], f["binary"])


def rows(file_diff):
    return [row for hunk in file_diff["hunks"] for row in hunk["lines"]]


def by_path(diff):
    return {f["path"]: f for f in diff["files"]}


def parse_one(text):
    files = gitx.parse_patch(textwrap.dedent(text).lstrip("\n"))
    assert len(files) == 1
    return files[0]


def counting_git(monkeypatch):
    """Wrap ``gitx._git`` so tests can count subprocess invocations."""
    calls = []
    real = gitx._git

    def wrapper(repo, args, **kwargs):
        calls.append(list(args))
        return real(repo, args, **kwargs)

    monkeypatch.setattr(gitx, "_git", wrapper)
    return calls


# --------------------------------------------------------------------------- empirical layouts

class TestGitLayouts:
    """The byte layouts SPEC 3.2 asks implementers to verify against the installed git."""

    def test_raw_p_boundary_is_double_nul(self, fixture_repo):
        r = fixture_repo
        data = r.git("diff", "--raw", "-z", "-p", "-M", "-C", "--abbrev=40", r.sha(F1), r.sha(F2))
        boundary = data.find(b"\0\0")
        assert boundary > 0
        raw_only = r.git("diff", "--raw", "-z", "-M", "-C", "--abbrev=40", r.sha(F1), r.sha(F2))
        assert data[:boundary + 1] == raw_only
        assert data[boundary + 2:].startswith(b"diff --git ")
        assert data.count(b"\0\0") == 1

    def test_raw_p_zero_files_prints_nothing(self, fixture_repo):
        r = fixture_repo
        parent = r.text("rev-parse", r.empty + "^")
        assert r.git("diff", "--raw", "-z", "-p", parent, r.empty) == b""

    def test_raw_p_all_files_removed_by_w_prints_lone_nul(self, fixture_repo):
        r = fixture_repo
        data = r.git("diff", "--raw", "-z", "-p", "-w", r.empty, r.big, "--", "data/config.ini")
        assert data == b"\0"

    def test_type_change_has_one_raw_record_and_two_sections(self, fixture_repo):
        r = fixture_repo
        data = r.git("diff", "--raw", "-z", "-p", "--abbrev=40", r.sha(F3), r.sha(F4), "--", "CHANGELOG.md")
        raw, patch = data.split(b"\0\0", 1)
        assert raw.count(b"\0") == 1 and b" T\0CHANGELOG.md" in raw
        sections = [line for line in patch.split(b"\n") if line.startswith(b"diff --git ")]
        assert sections == [b"diff --git a/CHANGELOG.md b/CHANGELOG.md"] * 2
        assert b"deleted file mode 100644" in patch and b"new file mode 120000" in patch

    def test_diff_tree_stdin_layout(self, fixture_repo):
        r = fixture_repo
        commits = gitx.list_commits(r.path, None, r.feature)
        parent_of = {c["sha"]: c["parents"] for c in commits}
        lines = [("%s %s" % (c["sha"], c["parents"][0])) if c["parents"] else c["sha"] for c in commits]
        data = r.git("diff-tree", "--stdin", "-r", "--root", "-M", "-C", "--raw", "--numstat", "-z", "--abbrev=40",
                     stdin=("\n".join(lines) + "\n").encode())
        tokens = data.split(b"\0")
        assert tokens.pop() == b""
        blocks, i = [], 0
        while i < len(tokens):  # independent walk: raw records carry 1-2 path tokens, rename numstats 2
            token = tokens[i]
            if token.startswith(b":"):
                i += 3 if token.split(b" ")[4][:1] in (b"R", b"C") else 2
                blocks[-1][1].append("raw")
            elif b"\t" in token:
                i += 3 if token.split(b"\t", 2)[2] == b"" else 1
                blocks[-1][1].append("num")
            else:
                blocks.append((token.decode(), []))
                i += 1
        headers = [h for h, _ in blocks]
        # header = the sha alone (not the full "<sha> <parent>" input line); empty commits print nothing
        assert all(SHA_RE.match(h) for h in headers) and len(set(headers)) == len(headers)
        assert set(headers) == {c["sha"] for c in commits} - {r.empty}
        assert parent_of[r.merge][0] == r.sha(F4)
        for header, kinds in blocks:  # inside a block every raw record precedes every numstat record
            assert re.fullmatch(r"(raw)+(num)+", "".join(kinds)) and kinds.count("raw") == kinds.count("num"), header
        assert dict(blocks)[r.merge] == ["raw", "num"]  # the merge was diffed against its first parent only

    def test_diff_tree_merge_without_parent_prints_nothing(self, fixture_repo):
        r = fixture_repo
        data = r.git("diff-tree", "--stdin", "-r", "--root", "-M", "--raw", "--numstat", "-z", stdin=(r.merge + "\n").encode())
        assert data == b""

    def test_log_z_records_are_all_nul_terminated(self, fixture_repo):
        r = fixture_repo
        data = r.git("log", "-z", "--format=%H%x00%P%x00%an%x00%ae%x00%at%x00%ct%x00%B", r.feature)
        tokens = data.split(b"\0")
        assert tokens[-1] == b""
        assert (len(tokens) - 1) % 7 == 0
        assert (len(tokens) - 1) // 7 == 13
        assert tokens[6].endswith(b"\n")  # %B keeps its trailing newline

    def test_option_after_end_of_options_is_rejected_by_git(self, fixture_repo):
        proc = subprocess.run(["git", "-C", fixture_repo.path, "log", "--end-of-options", "-1", "HEAD"],
                              capture_output=True)
        assert proc.returncode != 0 and b"must come before" in proc.stderr


# --------------------------------------------------------------------------- invocation rules

class TestInvocation:
    def test_env_scrubs_git_variables(self, monkeypatch, fixture_repo):
        monkeypatch.setenv("GIT_DIR", "/nonexistent/git-dir")
        monkeypatch.setenv("GIT_WORK_TREE", "/nonexistent")
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        monkeypatch.setenv("GIT_TRACE_PERFORMANCE", "0")
        env = gitx._env()
        assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_SSH_COMMAND"] and env["GIT_TRACE_PERFORMANCE"] == "0"
        assert env["LC_ALL"] == "C" and env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_PAGER"] == "cat" and env["PAGER"] == "cat"
        # the bogus GIT_DIR must not leak into the subprocess
        assert gitx.rev_parse(fixture_repo.path, "HEAD") == fixture_repo.feature

    def test_argv_shape(self, monkeypatch, fixture_repo):
        captured = {}
        real_run = subprocess.run

        def fake_run(argv, **kwargs):
            captured["argv"], captured["kwargs"] = argv, kwargs
            return real_run(argv, **kwargs)

        monkeypatch.setattr(gitx.subprocess, "run", fake_run)
        gitx.rev_parse(fixture_repo.path, "HEAD")
        argv, kwargs = captured["argv"], captured["kwargs"]
        assert argv[:3] == ["git", "-C", fixture_repo.path]
        for key in ("core.quotepath=false", "color.ui=never", "diff.noprefix=false", "diff.mnemonicPrefix=false",
                    "diff.suppressBlankEmpty=false", "diff.submodule=short", "diff.relative=false",
                    "log.showSignature=false"):
            assert key in argv
        assert argv.index("--no-pager") < argv.index("rev-parse")
        assert argv[-2:] == ["--end-of-options", "HEAD^{commit}"]
        assert kwargs["shell"] is False and kwargs["cwd"] == fixture_repo.path
        assert kwargs["stdin"] is subprocess.DEVNULL and "GIT_DIR" not in kwargs["env"]

    def test_missing_git_executable(self, monkeypatch, fixture_repo):
        def raise_missing(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(gitx.subprocess, "run", raise_missing)
        with pytest.raises(GitError, match="git executable not found on PATH"):
            gitx.rev_parse(fixture_repo.path, "HEAD")

    def test_nonzero_exit_reports_stderr(self, fixture_repo):
        with pytest.raises(GitError) as exc:
            gitx.list_commits(fixture_repo.path, fixture_repo.main, "0" * 40)
        assert exc.value.status == 400 and "fatal" in str(exc.value)

    def test_git_error_default_status(self):
        assert GitError("x").status == 400
        assert GitError("x", status=404).status == 404


# --------------------------------------------------------------------------- simple queries

class TestQueries:
    def test_check_version(self, monkeypatch):
        version = gitx.check_version()
        assert isinstance(version, tuple) and version >= (2, 24)
        monkeypatch.setattr(gitx, "_git", lambda *a, **k: subprocess.CompletedProcess([], 0, b"git version 2.20.1\n", b""))
        with pytest.raises(GitError, match="too old"):
            gitx.check_version()

    def test_toplevel(self, fixture_repo, tmp_path):
        assert gitx.toplevel(fixture_repo.path) == {"path": fixture_repo.path, "bare": False}
        assert gitx.toplevel(os.path.join(fixture_repo.path, "src"))["path"] == fixture_repo.path
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(GitError, match="not inside a git repository"):
            gitx.toplevel(str(outside))
        with pytest.raises(GitError, match="not a directory"):
            gitx.toplevel(os.path.join(fixture_repo.path, "README.md"))
        bare = str(tmp_path / "bare.git")
        run_git(str(tmp_path), ["clone", "-q", "--bare", fixture_repo.path, bare])
        assert gitx.toplevel(bare) == {"path": bare, "bare": True}

    def test_rev_parse(self, fixture_repo):
        r = fixture_repo
        assert gitx.rev_parse(r.path, "HEAD") == r.feature
        assert gitx.rev_parse(r.path, "feature") == r.feature
        assert gitx.rev_parse(r.path, "main") == r.main
        assert gitx.rev_parse(r.path, r.root[:7]) == r.root
        assert gitx.rev_parse(r.path, "HEAD~2") == r.merge
        with pytest.raises(GitError) as exc:
            gitx.rev_parse(r.path, "no-such-branch")
        assert exc.value.status == 404
        for bad in ("-x", "--output=/tmp/x", "main HEAD", "a\tb", "a\0b", "", None):
            with pytest.raises(GitError) as exc:
                gitx.rev_parse(r.path, bad)
            assert exc.value.status == 400

    def test_merge_base(self, fixture_repo):
        r = fixture_repo
        assert gitx.merge_base(r.path, r.main, r.feature) == r.main
        assert gitx.merge_base(r.path, r.sha(F2), r.main) == r.branch_point
        r.git("checkout", "-q", "--orphan", "orphan")
        r.git("rm", "-rfq", "--cached", ".")
        r.write("orphan.txt", "o\n")
        r.git("add", "orphan.txt")
        run_git(r.path, ["commit", "-q", "-m", "orphan root", "--", "orphan.txt"],
                extra_env={"GIT_AUTHOR_DATE": "@1700009999 +0000", "GIT_COMMITTER_DATE": "@1700009999 +0000"})
        orphan = r.text("rev-parse", "HEAD")
        assert gitx.merge_base(r.path, orphan, r.feature) is None
        with pytest.raises(GitError):
            gitx.merge_base(r.path, "-x", r.feature)

    def test_current_branch(self, fixture_repo):
        assert gitx.current_branch(fixture_repo.path) == "feature"
        fixture_repo.git("checkout", "-q", "--detach")
        assert gitx.current_branch(fixture_repo.path) is None

    def test_empty_tree_is_cached(self, fixture_repo, monkeypatch):
        gitx._empty_tree_cache.clear()
        assert gitx.empty_tree(fixture_repo.path) == EMPTY_TREE_SHA1
        calls = counting_git(monkeypatch)
        assert gitx.empty_tree(fixture_repo.path) == EMPTY_TREE_SHA1
        assert calls == []


# --------------------------------------------------------------------------- resolve_range

class TestResolveRange:
    def test_a_dot_dot_b(self, fixture_repo):
        r = fixture_repo
        assert gitx.resolve_range(r.path, "main..feature", None) == {
            "base": r.main, "head": r.feature, "spec": "main..feature", "given": "main..feature", "note": None}

    def test_symmetric(self, fixture_repo):
        r = fixture_repo
        spec = "%s...main" % r.sha(F2)
        res = gitx.resolve_range(r.path, spec, None)
        assert res["base"] == r.branch_point and res["head"] == r.main
        assert res["spec"] == "%s..main" % r.branch_point and res["given"] == spec and res["note"] is None

    def test_bare_rev_and_open_sides(self, fixture_repo):
        r = fixture_repo
        assert gitx.resolve_range(r.path, "main", None) == {
            "base": r.main, "head": r.feature, "spec": "main..HEAD", "given": "main", "note": None}
        assert gitx.resolve_range(r.path, "main..", None)["spec"] == "main..HEAD"
        res = gitx.resolve_range(r.path, "..feature", None)
        assert res["base"] == r.feature and res["head"] == r.feature and res["spec"] == "%s..feature" % r.feature

    def test_depth(self, fixture_repo):
        r = fixture_repo
        res = gitx.resolve_range(r.path, None, 2)
        assert res == {"base": r.merge, "head": r.feature, "spec": "%s..HEAD" % r.merge, "given": "-n 2", "note": None}
        # -n counts first-parent steps: 3 steps back from the tip is F4, not the hotfix
        assert gitx.resolve_range(r.path, None, 3)["base"] == r.sha(F4)

    def test_depth_clamps_to_whole_history(self, fixture_repo):
        r = fixture_repo
        depth = int(r.text("rev-list", "--count", "--first-parent", "HEAD"))
        res = gitx.resolve_range(r.path, None, depth)
        assert res["base"] is None and res["head"] == r.feature and res["given"] == "-n %d" % depth
        again = gitx.resolve_range(r.path, res["spec"], None)  # the pinned spec re-resolves to the same range
        assert again["base"] is None and again["head"] == r.feature
        assert gitx.resolve_range(r.path, None, 100)["base"] is None
        assert gitx.resolve_range(r.path, None, depth - 1)["base"] == r.root

    def test_non_ancestor_base_uses_merge_base_with_note(self, fixture_repo):
        r = fixture_repo
        res = gitx.resolve_range(r.path, "%s..main" % r.sha(F2), None)
        assert res["base"] == r.branch_point and res["head"] == r.main
        assert res["note"] == "base %s is not an ancestor of head; using merge-base %s" % (r.sha(F2)[:10], r.branch_point[:10])

    def test_no_common_ancestor(self, fixture_repo):
        r = fixture_repo
        r.git("checkout", "-q", "--orphan", "orphan")
        r.git("rm", "-rfq", "--cached", ".")
        r.write("orphan.txt", "o\n")
        r.git("add", "orphan.txt")
        run_git(r.path, ["commit", "-q", "-m", "orphan root", "--", "orphan.txt"],
                extra_env={"GIT_AUTHOR_DATE": "@1700009999 +0000", "GIT_COMMITTER_DATE": "@1700009999 +0000"})
        with pytest.raises(GitError, match="no common ancestor"):
            gitx.resolve_range(r.path, "feature..HEAD", None)
        with pytest.raises(GitError, match="no common ancestor"):
            gitx.resolve_range(r.path, "feature...HEAD", None)

    def test_default_detection(self, fixture_repo):
        r = fixture_repo
        assert gitx.resolve_range(r.path, None, None) == {
            "base": r.main, "head": r.feature, "spec": "main..HEAD", "given": None, "note": None}
        r.git("branch", "--set-upstream-to=main", "feature")
        assert gitx.resolve_range(r.path, None, None)["spec"] == "@{upstream}..HEAD"
        r.git("checkout", "-q", "-f", "main")
        with pytest.raises(GitError, match="cannot infer a range"):
            gitx.resolve_range(r.path, None, None)

    def test_empty_range_is_returned_not_raised(self, fixture_repo):
        r = fixture_repo
        res = gitx.resolve_range(r.path, "feature..feature", None)
        assert res["base"] == res["head"] == r.feature

    def test_bad_specs(self, fixture_repo):
        r = fixture_repo
        for spec in ("-x", "-x..HEAD", "main.. HEAD", "main\0..HEAD", "", "   ", "nope..HEAD"):
            with pytest.raises(GitError):
                gitx.resolve_range(r.path, spec, None)
        for n in (0, -1, True, "3"):
            with pytest.raises(GitError):
                gitx.resolve_range(r.path, None, n)
        with pytest.raises(GitError, match="mutually exclusive"):
            gitx.resolve_range(r.path, "main", 2)

    def test_too_large(self, fixture_repo, monkeypatch):
        monkeypatch.setattr(gitx, "MAX_RANGE_COMMITS", 3)
        with pytest.raises(GitError, match="range too large"):
            gitx.resolve_range(fixture_repo.path, "main..feature", None)


# --------------------------------------------------------------------------- list_commits

class TestListCommits:
    def test_chain_order_and_fields(self, fixture_repo):
        r = fixture_repo
        commits = gitx.list_commits(r.path, r.main, r.feature)
        assert [c["subject"] for c in commits] == list(FEATURE_SUBJECTS)
        assert [c["sha"] for c in commits] == r.feature_chain
        for c in commits:
            assert SHA_RE.match(c["sha"]) and c["short_sha"] == c["sha"][:10] and c["kind"] == "commit"
            assert c["author"] == {"name": "Fixture Author", "email": "fixture@example.com"}
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", c["author_date"])
            assert c["shallow_boundary"] is False and "files" not in c and "comment_count" not in c
            assert all(SHA_RE.match(p) for p in c["parents"])
        dates = [c["commit_date"] for c in commits]
        assert dates == sorted(dates)
        merge = commits[4]
        assert merge["is_merge"] is True and merge["parents"] == [r.sha(F4), r.main]
        assert all(c["is_merge"] is False and len(c["parents"]) == 1 for c in commits if c is not merge)
        assert commits[5]["body"] == "" and commits[5]["subject"] == EMPTY

    def test_whole_history_and_message_split(self, fixture_repo):
        r = fixture_repo
        commits = gitx.list_commits(r.path, None, r.feature)
        assert len(commits) == 13
        assert commits[0]["subject"] == MAIN_SUBJECTS[0] and commits[0]["parents"] == []
        assert commits[0]["author_date"] == "2023-11-14T22:14:20Z"  # FIRST_EPOCH + 60 s
        assert {c["sha"] for c in commits} == set(r.text("rev-list", r.feature).split())
        seen = set()
        for c in commits:
            assert all(p in seen for p in c["parents"]), "parents must precede children"
            seen.add(c["sha"])
        doc = next(c for c in commits if c["subject"] == MAIN_SUBJECTS[3])
        assert doc["body"] == DOCUMENT_USAGE_BODY
        assert doc["author_date"] == gitx._iso(str(FIRST_EPOCH + 60 * 4))

    def test_first_parent(self, fixture_repo):
        r = fixture_repo
        full = {c["subject"] for c in gitx.list_commits(r.path, r.branch_point, r.feature)}
        first = {c["subject"] for c in gitx.list_commits(r.path, r.branch_point, r.feature, first_parent=True)}
        assert HOTFIX_SUBJECT in full and HOTFIX_SUBJECT not in first
        assert first == set(FEATURE_SUBJECTS)

    def test_shallow_boundary(self, fixture_repo):
        r = fixture_repo
        gitdir = os.path.join(r.path, r.text("rev-parse", "--git-common-dir"))
        with open(os.path.join(gitdir, "shallow"), "w") as fh:
            fh.write(r.root + "\n")
        commits = gitx.list_commits(r.path, None, r.feature)
        assert commits[0]["shallow_boundary"] is True
        assert all(c["shallow_boundary"] is False for c in commits[1:])

    def test_long_body_is_truncated(self, fixture_repo):
        r = fixture_repo
        run_git(r.path, ["commit", "-q", "--allow-empty", "-m", "Long body", "-m", "x" * 70000],
                extra_env={"GIT_AUTHOR_DATE": "@1700009999 +0000", "GIT_COMMITTER_DATE": "@1700009999 +0000"})
        commit = gitx.list_commits(r.path, r.feature, gitx.rev_parse(r.path, "HEAD"))[0]
        assert commit["subject"] == "Long body" and len(commit["body"].encode()) == 64 * 1024

    def test_rejects_option_like_revs(self, fixture_repo):
        with pytest.raises(GitError):
            gitx.list_commits(fixture_repo.path, "-x", fixture_repo.feature)
        with pytest.raises(GitError):
            gitx.list_commits(fixture_repo.path, None, "--all")


# --------------------------------------------------------------------------- commit_stats

class TestCommitStats:
    def test_matches_numstat_for_every_commit(self, fixture_repo, monkeypatch):
        r = fixture_repo
        commits = gitx.list_commits(r.path, None, r.feature)
        calls = counting_git(monkeypatch)
        stats = gitx.commit_stats(r.path, commits)
        assert len(calls) == 1 and calls[0][:2] == ["diff-tree", "--stdin"]
        assert set(stats) == {c["sha"] for c in commits}
        for c in commits:
            parent = c["parents"][0] if c["parents"] else EMPTY_TREE_SHA1
            expected = numstat(r, parent, c["sha"])
            assert [stat_tuple(f) for f in stats[c["sha"]]] == expected, c["subject"]
            statuses = name_status(r, parent, c["sha"])
            assert {f["path"]: f["status"] for f in stats[c["sha"]]} == statuses, c["subject"]

    def test_root_merge_and_empty(self, fixture_repo):
        r = fixture_repo
        commits = gitx.list_commits(r.path, None, r.feature)
        stats = gitx.commit_stats(r.path, commits)
        root = stats[r.root]
        assert [f["path"] for f in root] == [".gitignore", "README.md", "data/config.ini", "docs/guide.txt",
                                             "src/app.py", "src/util.py"]
        assert all(f["status"] == "A" and f["old_mode"] is None and f["old_blob"] is None for f in root)
        assert [stat_tuple(f) for f in stats[r.merge]] == [("hotfix.txt", 1, 0, False)]  # vs FIRST parent only
        assert [stat_tuple(f) for f in stats[r.merge]] == numstat(r, r.sha(F4), r.merge)
        assert stats[r.empty] == []

    def test_filestat_fields(self, fixture_repo):
        r = fixture_repo
        commits = gitx.list_commits(r.path, r.main, r.feature)
        stats = gitx.commit_stats(r.path, commits)
        f2 = {f["path"]: f for f in stats[r.sha(F2)]}
        rename = f2["src/utils.py"]
        assert rename["status"] == "R" and rename["old_path"] == "src/util.py" and 50 <= rename["score"] < 100
        assert rename["additions"] == 2 and rename["deletions"] == 2
        assert SHA_RE.match(rename["old_blob"]) and SHA_RE.match(rename["new_blob"]) and rename["old_blob"] != rename["new_blob"]
        pure = f2["lib/helper.py"]
        assert pure["status"] == "R" and pure["score"] == 100 and pure["old_path"] == "src/helper.py"
        assert pure["additions"] == pure["deletions"] == 0 and pure["old_blob"] == pure["new_blob"]
        deleted = f2["docs/guide.txt"]
        assert deleted["status"] == "D" and deleted["new_mode"] is None and deleted["new_blob"] is None
        assert deleted["old_mode"] == "100644" and deleted["deletions"] == 5
        f3 = {f["path"]: f for f in stats[r.sha(F3)]}
        assert f3["assets/logo.png"]["binary"] is True and f3["assets/logo.png"]["additions"] == 0
        assert f3["data/blob.dat"]["binary"] is True, "textconv must not turn the .dat file into text"
        assert f3[UNICODE_PATH]["additions"] == 2 and f3[UNICODE_PATH]["old_path"] is None
        f4 = {f["path"]: f for f in stats[r.sha(F4)]}
        assert f4["scripts/run.sh"] == dict(f4["scripts/run.sh"], status="M", old_mode="100644", new_mode="100755",
                                            additions=0, deletions=0, binary=False)
        assert f4["CHANGELOG.md"]["status"] == "T" and f4["CHANGELOG.md"]["new_mode"] == "120000"
        assert f4["link_to_readme"]["new_mode"] == "120000" and f4["link_to_readme"]["status"] == "A"
        assert all(f["score"] == 0 for c in commits for f in stats[c["sha"]] if f["status"] not in "RC")

    def test_no_commits(self, fixture_repo):
        assert gitx.commit_stats(fixture_repo.path, []) == {}


# --------------------------------------------------------------------------- diff_commit / diff_range

class TestDiffCommit:
    def test_three_hunks(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.sha(F1), r.branch_point)
        assert diff["sha"] == r.sha(F1) and diff["subject"] == F1 and diff["kind"] == "commit"
        assert diff["stats"] == {"files": 1, "additions": 4, "deletions": 3}
        (f,) = diff["files"]
        assert f["path"] == "src/app.py" and f["status"] == "M" and f["lang"] == "python"
        assert f["old_rev"] == r.branch_point and f["new_rev"] == r.sha(F1)
        assert f["hunk_count"] == 3 and f["line_count"] == 25 and f["too_large"] is False and f["ws_only"] is False
        assert f["additions"] == 4 and f["deletions"] == 3 and f["reason"] is None
        h1, h2, h3 = f["hunks"]
        assert (h1["old_start"], h1["old_count"], h1["new_start"], h1["new_count"], h1["section"]) == (2, 7, 2, 7, "value_01 = 1")
        assert h1["lines"][3] == {"t": "del", "o": 5, "n": None, "s": "value_05 = 5"}
        assert h1["lines"][4] == {"t": "add", "o": None, "n": 5, "s": "value_05 = 500  # changed"}
        assert [(row["t"], row["o"], row["n"]) for row in h2["lines"]] == [
            ("ctx", 12, 12), ("ctx", 13, 13), ("ctx", 14, 14), ("del", 15, None), ("del", 16, None),
            ("ctx", 17, 15), ("ctx", 18, 16), ("ctx", 19, 17)]
        assert (h3["old_start"], h3["old_count"], h3["new_start"], h3["new_count"]) == (23, 6, 21, 9)
        assert [row["s"] for row in h3["lines"] if row["t"] == "add"] == ["inserted_a = 'a'", "inserted_b = 'b'", "inserted_c = 'c'"]
        json.dumps(diff)

    def test_root_commit_against_empty_tree(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.root, None)
        assert diff["parents"] == [] and diff["subject"] == MAIN_SUBJECTS[0]
        assert [stat_tuple(f) for f in diff["files"]] == numstat(r, EMPTY_TREE_SHA1, r.root)
        assert all(f["status"] == "A" and f["old_rev"] is None and f["new_rev"] == r.root for f in diff["files"])
        app = by_path(diff)["src/app.py"]
        assert [row["s"] for row in rows(app)] == APP_ORIGINAL.split("\n")[:-1]
        assert app["hunks"][0]["old_start"] == 0 and app["hunks"][0]["old_count"] == 0

    def test_merge_uses_first_parent(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.merge, r.sha(F4))
        assert diff["is_merge"] is True and diff["parents"] == [r.sha(F4), r.main]
        assert [stat_tuple(f) for f in diff["files"]] == [("hotfix.txt", 1, 0, False)]
        assert diff["files"][0]["old_rev"] == r.sha(F4)
        assert len(numstat(r, r.main, r.merge)) > 1  # the second-parent diff would be much larger

    def test_empty_commit(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.empty, r.merge)
        assert diff["files"] == [] and diff["stats"] == {"files": 0, "additions": 0, "deletions": 0}

    def test_rename_delete_and_pure_rename(self, fixture_repo):
        r = fixture_repo
        files = by_path(gitx.diff_commit(r.path, r.sha(F2), r.sha(F1)))
        assert list(files) == ["docs/guide.txt", "lib/helper.py", "src/utils.py"]
        rename = files["src/utils.py"]
        assert rename["status"] == "R" and rename["old_path"] == "src/util.py" and 50 <= rename["score"] < 100
        assert rename["hunk_count"] == 1 and rename["additions"] == 2 and rename["deletions"] == 2
        pure = files["lib/helper.py"]
        assert pure["status"] == "R" and pure["score"] == 100 and pure["hunks"] == [] and pure["line_count"] == 0
        deleted = files["docs/guide.txt"]
        assert deleted["status"] == "D" and deleted["deletions"] == 5 and deleted["new_mode"] is None
        assert all(row["t"] == "del" for row in rows(deleted))

    def test_binary_unicode_crlf_nonl(self, fixture_repo):
        r = fixture_repo
        files = by_path(gitx.diff_commit(r.path, r.sha(F3), r.sha(F2)))
        png = files["assets/logo.png"]
        assert png["binary"] is True and png["hunks"] == [] and png["additions"] == png["deletions"] == 0
        assert png["status"] == "A" and png["new_mode"] == "100644" and png["lang"] is None
        dat = files["data/blob.dat"]
        assert dat["binary"] is True and dat["hunks"] == [], "textconv=xxd must be bypassed"
        uni = files[UNICODE_PATH]
        assert [row["s"] for row in rows(uni)] == ["héllo wörld", "second line"]
        nonl = files["notes/nonl.txt"]
        assert rows(nonl)[-1]["s"] == "last line without newline" and rows(nonl)[-1].get("nonl") is True
        assert "nonl" not in rows(nonl)[0]
        crlf = files["win/crlf.txt"]
        assert all(row["cr"] is True and "\r" not in row["s"] for row in rows(crlf))
        assert files[".gitattributes"]["lang"] is None

    def test_type_change_chmod_symlink_and_two_nonl_markers(self, fixture_repo):
        r = fixture_repo
        files = by_path(gitx.diff_commit(r.path, r.sha(F4), r.sha(F3)))
        tc = files["CHANGELOG.md"]
        assert tc["status"] == "T" and tc["old_mode"] == "100644" and tc["new_mode"] == "120000"
        assert tc["hunk_count"] == 2 and tc["additions"] == 1 and tc["deletions"] == 3
        assert [row["t"] for row in rows(tc)] == ["del", "del", "del", "add"]
        assert rows(tc)[-1] == {"t": "add", "o": None, "n": 1, "s": "README.md", "nonl": True}
        link = files["link_to_readme"]
        assert link["status"] == "A" and link["new_mode"] == "120000" and rows(link)[0]["s"] == "README.md"
        chmod = files["scripts/run.sh"]
        assert chmod["status"] == "M" and (chmod["old_mode"], chmod["new_mode"]) == ("100644", "100755")
        assert chmod["hunks"] == [] and chmod["old_blob"] == chmod["new_blob"] and chmod["ws_only"] is False
        nonl = files["notes/nonl.txt"]
        (hunk,) = nonl["hunks"]
        assert [(row["t"], row.get("nonl")) for row in hunk["lines"]] == [("ctx", None), ("del", True), ("add", True)]
        crlf = files["win/crlf.txt"]
        assert [(row["t"], row["s"], row["cr"]) for row in rows(crlf)] == [
            ("ctx", "alpha", True), ("del", "beta", True), ("add", "BETA", True), ("ctx", "gamma", True)]

    def test_big_file_untrimmed_with_counts(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.big, r.empty)
        big = by_path(diff)["big/generated.txt"]
        assert big["line_count"] == 6000 and big["hunk_count"] == 1 and big["too_large"] is False
        assert len(rows(big)) == 6000 and big["additions"] == 6000
        assert diff["stats"]["additions"] == 6002

    def test_ws_ignore(self, fixture_repo):
        r = fixture_repo
        plain = by_path(gitx.diff_commit(r.path, r.big, r.empty))
        assert plain["data/config.ini"]["hunk_count"] == 1 and plain["data/config.ini"]["ws_only"] is False
        ws = by_path(gitx.diff_commit(r.path, r.big, r.empty, ws_ignore=True))
        assert list(ws) == ["big/generated.txt", "data/config.ini"]  # dropped file re-inserted in path order
        cfg = ws["data/config.ini"]
        assert cfg["ws_only"] is True and cfg["hunks"] == [] and cfg["hunk_count"] == 0 and cfg["status"] == "M"
        assert cfg["old_blob"] != cfg["new_blob"] and cfg["additions"] == cfg["deletions"] == 0
        assert ws["big/generated.txt"]["ws_only"] is False and ws["big/generated.txt"]["hunk_count"] == 1
        # a diff without whitespace-only changes is unaffected by -w
        a = gitx.diff_commit(r.path, r.sha(F1), r.branch_point)["files"]
        b = gitx.diff_commit(r.path, r.sha(F1), r.branch_point, ws_ignore=True)["files"]
        assert a == b

    def test_ws_ignore_keeps_mode_changed_file_flagged(self, fixture_repo):
        r = fixture_repo
        path = os.path.join(r.path, "Makefile")
        with open(path, "r+", encoding="utf-8") as fh:
            text = fh.read()
            fh.seek(0)
            fh.write(text.replace("all:", "all:   "))
        os.chmod(path, 0o755)
        makefile = by_path(gitx.diff_worktree(r.path, ws_ignore=True))["Makefile"]
        assert makefile["ws_only"] is True and makefile["hunks"] == []
        assert (makefile["old_mode"], makefile["new_mode"]) == ("100644", "100755")

    def test_rejects_option_like_revs(self, fixture_repo):
        with pytest.raises(GitError):
            gitx.diff_commit(fixture_repo.path, "--output=/tmp/x", None)
        with pytest.raises(GitError):
            gitx.diff_range(fixture_repo.path, None, "-x")


class TestDiffRange:
    def test_combined(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_range(r.path, r.main, r.feature)
        assert diff["sha"] == "combined" and diff["short_sha"] == "combined" and diff["kind"] == "combined"
        assert diff["subject"] == "All changes" and diff["parents"] == [] and diff["is_merge"] is False
        assert diff["author_date"] is None and diff["commit_date"] is None and diff["body"] == ""
        assert [stat_tuple(f) for f in diff["files"]] == numstat(r, r.main, r.feature)
        assert {f["path"]: f["status"] for f in diff["files"]} == name_status(r, r.main, r.feature)
        assert all(f["old_rev"] == r.main and f["new_rev"] == r.feature for f in diff["files"])
        assert diff["stats"]["files"] == len(diff["files"]) == 15
        assert by_path(diff)["src/utils.py"]["old_path"] == "src/util.py"

    def test_null_base_is_empty_tree(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_range(r.path, None, r.root)
        assert [stat_tuple(f) for f in diff["files"]] == numstat(r, EMPTY_TREE_SHA1, r.root)
        assert all(f["old_rev"] is None for f in diff["files"])

    def test_ws_ignore(self, fixture_repo):
        r = fixture_repo
        ws = by_path(gitx.diff_range(r.path, r.empty, r.big, ws_ignore=True))
        assert ws["data/config.ini"]["ws_only"] is True


# --------------------------------------------------------------------------- diff_worktree

class TestDiffWorktree:
    def test_contents(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_worktree(r.path)
        assert diff["sha"] == "worktree" and diff["kind"] == "worktree" and diff["subject"] == "Uncommitted changes"
        files = by_path(diff)
        assert list(files) == ["README.md", "src/utils.py", "untracked.bin", "untracked.txt", "untracked_link"]
        assert "ignored.log" not in files and not any(p.startswith("nested") for p in files)
        for f in files.values():
            assert f["old_rev"] == r.feature and f["new_rev"] == "worktree" and f["new_blob"] is None
        unstaged = files["README.md"]
        assert unstaged["status"] == "M" and SHA_RE.match(unstaged["old_blob"])
        assert [row["s"] for row in rows(unstaged) if row["t"] == "add"] == ["", "Unstaged edit."]
        staged = files["src/utils.py"]
        assert staged["status"] == "M" and [row["s"] for row in rows(staged) if row["t"] == "add"] == ["", "", "STAGED = True"]
        text = files["untracked.txt"]
        assert text["status"] == "A" and text["new_mode"] == "100644" and text["old_mode"] is None
        assert text["old_path"] is None and text["old_blob"] is None and text["binary"] is False
        assert text["hunks"] == [{"old_start": 0, "old_count": 0, "new_start": 1, "new_count": 2, "section": "",
                                  "lines": [{"t": "add", "o": None, "n": 1, "s": "untracked line 1"},
                                            {"t": "add", "o": None, "n": 2, "s": "untracked line 2"}]}]
        assert text["additions"] == 2 and text["line_count"] == 2 and text["hunk_count"] == 1 and text["lang"] == "plaintext"
        binary = files["untracked.bin"]
        assert binary["binary"] is True and binary["hunks"] == [] and binary["additions"] == 0 and binary["status"] == "A"
        link = files["untracked_link"]
        assert link["new_mode"] == "120000" and link["status"] == "A"
        assert rows(link) == [{"t": "add", "o": None, "n": 1, "s": "README.md", "nonl": True}]
        assert diff["stats"]["files"] == 5

    def test_untracked_variants(self, fixture_repo):
        r = fixture_repo
        exe = r.write("tool.sh", "#!/bin/sh\necho hi")
        os.chmod(exe, 0o755)
        huge_lines = 40000
        r.write("huge.txt", "".join("%08d padding padding padding\n" % i for i in range(huge_lines)) + "tail")
        assert os.path.getsize(os.path.join(r.path, "huge.txt")) > gitx.UNTRACKED_MAX_BYTES
        r.write("empty.txt", "")
        files = by_path(gitx.diff_worktree(r.path))
        tool = files["tool.sh"]
        assert tool["new_mode"] == "100755" and tool["lang"] == "bash" and rows(tool)[-1].get("nonl") is True
        huge = files["huge.txt"]
        assert huge["too_large"] is True and huge["reason"] == "file" and huge["hunks"] == []
        assert huge["additions"] == huge["line_count"] == huge_lines + 1 and huge["binary"] is False
        assert gitx.trim_file_diff(huge, full=True) is huge
        empty = files["empty.txt"]
        assert empty["hunks"] == [] and empty["additions"] == 0 and empty["binary"] is False

    def test_skips_non_regular_entries(self, fixture_repo):
        r = fixture_repo
        os.mkfifo(os.path.join(r.path, "pipe"))
        assert "pipe" not in by_path(gitx.diff_worktree(r.path))

    def test_clean_worktree(self, fixture_repo):
        r = fixture_repo
        r.git("reset", "-q", "--hard")
        r.git("clean", "-qfdx", "-e", "ignored.log")
        diff = gitx.diff_worktree(r.path)
        assert diff["files"] == [] and diff["stats"] == {"files": 0, "additions": 0, "deletions": 0}

    def test_bare_repo(self, fixture_repo, tmp_path):
        bare = str(tmp_path / "bare.git")
        run_git(str(tmp_path), ["clone", "-q", "--bare", fixture_repo.path, bare])
        with pytest.raises(GitError, match="bare"):
            gitx.diff_worktree(bare)

    def test_index_lock_retry(self, fixture_repo, monkeypatch):
        r = fixture_repo
        state = {"failed": False}
        real = gitx._git

        def flaky(repo, args, **kwargs):
            if args[0] == "diff" and "-p" in args and not state["failed"]:
                state["failed"] = True
                raise GitError("fatal: Unable to create '.git/index.lock': File exists.")
            return real(repo, args, **kwargs)

        slept = []
        monkeypatch.setattr(gitx, "_git", flaky)
        monkeypatch.setattr(gitx.time, "sleep", slept.append)
        assert "README.md" in by_path(gitx.diff_worktree(r.path))
        assert slept == [gitx.INDEX_LOCK_RETRY_DELAY]
        with pytest.raises(GitError, match="index.lock"):  # a second failure is not retried again
            state["failed"] = False
            monkeypatch.setattr(gitx, "_git", lambda repo, args, **kw: (_ for _ in ()).throw(
                GitError("index.lock")) if args[0] == "diff" else real(repo, args, **kw))
            gitx.diff_worktree(r.path)


# --------------------------------------------------------------------------- show_file

class TestShowFile:
    def test_blob(self, fixture_repo):
        r = fixture_repo
        res = gitx.show_file(r.path, r.branch_point, "src/app.py")
        assert res == {"content": APP_ORIGINAL[:-1], "lines": 30, "truncated_lines": []}
        assert gitx.show_file(r.path, r.feature, "src/app.py")["lines"] == 31
        assert gitx.show_file(r.path, r.feature, "notes/nonl.txt") == {
            "content": "first line\nLAST line without newline", "lines": 2, "truncated_lines": []}
        assert gitx.show_file(r.path, r.feature, "win/crlf.txt")["content"] == "alpha\nBETA\ngamma"
        assert gitx.show_file(r.path, r.feature, "link_to_readme")["content"] == "README.md"
        assert gitx.show_file(r.path, r.feature, UNICODE_PATH)["content"] == "héllo wörld\nsecond line"

    def test_errors_at_rev(self, fixture_repo):
        r = fixture_repo
        for path, status in (("src", 404), ("missing.txt", 404), ("assets/logo.png", 415), ("data/blob.dat", 415)):
            with pytest.raises(GitError) as exc:
                gitx.show_file(r.path, r.feature, path)
            assert exc.value.status == status, path
        with pytest.raises(GitError) as exc:
            gitx.show_file(r.path, "feature", "src/app.py")  # only full shas or "worktree"
        assert exc.value.status == 400
        with pytest.raises(GitError) as exc:
            gitx.show_file(r.path, "0" * 40, "src/app.py")
        assert exc.value.status == 404
        with pytest.raises(GitError):
            gitx.show_file(r.path, r.feature, "a\0b")

    def test_worktree(self, fixture_repo, tmp_path):
        r = fixture_repo
        assert gitx.show_file(r.path, "worktree", "untracked_link") == {"content": "README.md", "lines": 1, "truncated_lines": []}
        assert gitx.show_file(r.path, "worktree", "untracked.txt")["content"] == "untracked line 1\nuntracked line 2"
        assert gitx.show_file(r.path, "worktree", "README.md")["lines"] == 5
        for path, status in (("src", 404), ("missing.txt", 404), ("untracked.bin", 415), ("nested", 404)):
            with pytest.raises(GitError) as exc:
                gitx.show_file(r.path, "worktree", path)
            assert exc.value.status == status, path
        (tmp_path / "outside.txt").write_text("secret\n")
        for escape in ("../outside.txt", "src/../../outside.txt"):
            with pytest.raises(GitError) as exc:
                gitx.show_file(r.path, "worktree", escape)
            assert exc.value.status == 403, escape
        os.symlink(str(tmp_path), os.path.join(r.path, "escape_dir"))
        with pytest.raises(GitError) as exc:
            gitx.show_file(r.path, "worktree", "escape_dir/outside.txt")
        assert exc.value.status == 403

    def test_long_lines_and_size_cap(self, fixture_repo):
        r = fixture_repo
        r.write("long.txt", "short\n" + "x" * 25000 + "\nend\n")
        res = gitx.show_file(r.path, "worktree", "long.txt")
        assert res["lines"] == 3 and res["truncated_lines"] == [2] and len(res["content"].split("\n")[1]) == 20000
        r.write("big.txt", ("y" * 1023 + "\n") * (8 * 1024 + 1))
        with pytest.raises(GitError) as exc:
            gitx.show_file(r.path, "worktree", "big.txt")
        assert exc.value.status == 413


# --------------------------------------------------------------------------- map_line

class TestMapLine:
    def test_statuses(self, fixture_repo):
        r = fixture_repo
        bp, f1 = r.branch_point, r.sha(F1)
        assert gitx.map_line(r.path, bp, bp, "src/app.py", 7) == {"path": "src/app.py", "line": 7, "status": "same"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 2) == {"path": "src/app.py", "line": 2, "status": "same"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 5) == {"path": "src/app.py", "line": 5, "status": "changed"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 15) == {"path": "src/app.py", "line": 12, "status": "deleted"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 16) == {"path": "src/app.py", "line": 12, "status": "deleted"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 19) == {"path": "src/app.py", "line": 17, "status": "same"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 20) == {"path": "src/app.py", "line": 18, "status": "moved"}
        assert gitx.map_line(r.path, bp, f1, "src/app.py", 30) == {"path": "src/app.py", "line": 31, "status": "moved"}
        assert gitx.map_line(r.path, bp, f1, "Makefile", 2) == {"path": "Makefile", "line": 2, "status": "same"}

    def test_rename_and_deletion(self, fixture_repo):
        r = fixture_repo
        assert gitx.map_line(r.path, r.branch_point, r.feature, "src/util.py", 5) == {
            "path": "src/utils.py", "line": 4, "status": "changed"}
        assert gitx.map_line(r.path, r.branch_point, r.feature, "src/util.py", 13) == {
            "path": "src/utils.py", "line": 13, "status": "same"}
        assert gitx.map_line(r.path, r.branch_point, r.feature, "docs/guide.txt", 1) == {
            "path": None, "line": None, "status": "file-deleted"}
        assert gitx.map_line(r.path, r.sha(F1), r.sha(F2), "src/helper.py", 2) == {
            "path": "lib/helper.py", "line": 2, "status": "same"}

    def test_cached_per_sha_pair(self, fixture_repo, monkeypatch):
        r = fixture_repo
        gitx._map_line_cache.clear()
        first = gitx.map_line(r.path, r.branch_point, r.sha(F1), "src/app.py", 30)
        calls = counting_git(monkeypatch)
        assert gitx.map_line(r.path, r.branch_point, r.sha(F1), "src/app.py", 30) == first
        assert gitx.map_line(r.path, r.branch_point, r.sha(F1), "src/app.py", 2)["status"] == "same"
        assert calls == []

    def test_rejects_bad_revs(self, fixture_repo):
        with pytest.raises(GitError):
            gitx.map_line(fixture_repo.path, "-x", fixture_repo.feature, "src/app.py", 1)


# --------------------------------------------------------------------------- parse_patch (pure)

class TestParsePatch:
    def test_rename_with_edits(self):
        f = parse_one("""
            diff --git a/src/util.py b/src/utils.py
            similarity index 69%
            rename from src/util.py
            rename to src/utils.py
            index 3b18e51..f0c1a2b 100644
            --- a/src/util.py
            +++ b/src/utils.py
            @@ -1,3 +1,3 @@ def add(a, b):
             keep
            -old
            +new
             tail
            """)
        assert f["status"] == "R" and f["path"] == "src/utils.py" and f["old_path"] == "src/util.py" and f["score"] == 69
        assert (f["old_mode"], f["new_mode"], f["old_blob"], f["new_blob"]) == ("100644", "100644", "3b18e51", "f0c1a2b")
        assert f["additions"] == 1 and f["deletions"] == 1 and f["lang"] == "python" and f["hunks"][0]["section"] == "def add(a, b):"
        assert f["line_count"] == 4 and f["hunk_count"] == 1 and f["binary"] is False

    def test_pure_rename_and_copy(self):
        f = parse_one("""
            diff --git a/a.txt b/b.txt
            similarity index 100%
            rename from a.txt
            rename to b.txt
            """)
        assert f == dict(f, status="R", path="b.txt", old_path="a.txt", score=100, hunks=[], additions=0, deletions=0,
                         line_count=0, hunk_count=0)
        c = parse_one("""
            diff --git a/a.txt b/c.txt
            similarity index 90%
            copy from a.txt
            copy to c.txt
            index 111..222 100644
            --- a/a.txt
            +++ b/c.txt
            @@ -1 +1 @@
            -x
            +y
            """)
        assert c["status"] == "C" and c["old_path"] == "a.txt" and c["path"] == "c.txt" and c["score"] == 90

    def test_binary_forms(self):
        files = gitx.parse_patch(textwrap.dedent("""
            diff --git a/img.png b/img.png
            new file mode 100644
            index 0000000..8352675
            Binary files /dev/null and b/img.png differ
            diff --git a/blob.bin b/blob.bin
            index 1111111..2222222 100644
            GIT binary patch
            literal 3
            KcmZQzWMT#Y01f~L

            literal 0
            HcmV?d00001

            diff --git a/after.txt b/after.txt
            index 3333333..4444444 100644
            --- a/after.txt
            +++ b/after.txt
            @@ -1 +1 @@
            -a
            +b
            """).lstrip("\n"))
        assert [(f["path"], f["status"], f["binary"], f["hunks"], f["additions"]) for f in files] == [
            ("img.png", "A", True, [], 0), ("blob.bin", "M", True, [], 0), ("after.txt", "M", False, files[2]["hunks"], 1)]
        assert files[0]["old_blob"] is None and files[0]["new_blob"] == "8352675" and files[0]["old_mode"] is None
        assert files[2]["hunk_count"] == 1

    def test_mode_only(self):
        f = parse_one("""
            diff --git a/run.sh b/run.sh
            old mode 100644
            new mode 100755
            """)
        assert f["status"] == "M" and f["path"] == "run.sh" and (f["old_mode"], f["new_mode"]) == ("100644", "100755")
        assert f["hunks"] == [] and f["old_blob"] is None and f["additions"] == 0

    def test_two_no_newline_markers_in_one_hunk(self):
        f = parse_one("""
            diff --git a/n.txt b/n.txt
            index 9ed40b4..530cc72 100644
            --- a/n.txt
            +++ b/n.txt
            @@ -1,2 +1,2 @@
             one
            -two
            \\ No newline at end of file
            +TWO
            \\ No newline at end of file
            """)
        assert rows(f) == [{"t": "ctx", "o": 1, "n": 1, "s": "one"},
                           {"t": "del", "o": 2, "n": None, "s": "two", "nonl": True},
                           {"t": "add", "o": None, "n": 2, "s": "TWO", "nonl": True}]
        assert f["line_count"] == 3

    def test_multi_hunk_numbering(self):
        f = parse_one("""
            diff --git a/m.txt b/m.txt
            index 1..2 100644
            --- a/m.txt
            +++ b/m.txt
            @@ -1,3 +1,2 @@
             a
            -b
             c
            @@ -10,2 +9,3 @@ section two
             j
            +J2
             k
            @@ -20 +20 @@
            -t
            +T
            """)
        assert f["hunk_count"] == 3 and f["additions"] == 2 and f["deletions"] == 2
        assert [h["section"] for h in f["hunks"]] == ["", "section two", ""]
        assert [(row["o"], row["n"]) for row in f["hunks"][1]["lines"]] == [(10, 9), (None, 10), (11, 11)]
        h3 = f["hunks"][2]
        assert (h3["old_start"], h3["old_count"], h3["new_start"], h3["new_count"]) == (20, 1, 20, 1)

    def test_dev_null_creation_and_deletion(self):
        files = gitx.parse_patch(textwrap.dedent("""
            diff --git a/new.py b/new.py
            new file mode 100644
            index 0000000..abcdef0
            --- /dev/null
            +++ b/new.py
            @@ -0,0 +1,2 @@
            +x = 1
            +y = 2
            diff --git a/old.c b/old.c
            deleted file mode 100755
            index 1234567..0000000
            --- a/old.c
            +++ /dev/null
            @@ -1 +0,0 @@
            -int main() {}
            """).lstrip("\n"))
        new, old = files
        assert new["status"] == "A" and new["path"] == "new.py" and new["old_path"] is None
        assert new["old_mode"] is None and new["new_mode"] == "100644" and new["old_blob"] is None and new["new_blob"] == "abcdef0"
        assert new["hunks"][0]["old_count"] == 0 and [row["n"] for row in rows(new)] == [1, 2] and new["additions"] == 2
        assert old["status"] == "D" and old["path"] == "old.c" and old["new_mode"] is None and old["old_mode"] == "100755"
        assert old["new_blob"] is None and old["deletions"] == 1 and old["lang"] == "c"

    def test_spaces_and_quoted_paths(self):
        files = gitx.parse_patch(textwrap.dedent("""
            diff --git a/dir with space/ünïcode.txt b/dir with space/ünïcode.txt
            index 1..2 100644
            --- a/dir with space/ünïcode.txt\t
            +++ b/dir with space/ünïcode.txt\t
            @@ -1 +1 @@
            -a
            +b
            diff --git "a/we\\tird\\"q\\\\b.txt" "b/we\\tird2.txt"
            similarity index 100%
            rename from "we\\tird\\"q\\\\b.txt"
            rename to "we\\tird2.txt"
            diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"
            new file mode 100644
            index 0000000..1111111
            --- /dev/null
            +++ "b/caf\\303\\251.txt"
            @@ -0,0 +1 @@
            +bonjour
            diff --git a/sp ace.bin b/sp ace.bin
            index 1..2 100644
            Binary files a/sp ace.bin and b/sp ace.bin differ
            """).lstrip("\n"))
        assert [f["path"] for f in files] == ["dir with space/ünïcode.txt", "we\tird2.txt", "café.txt", "sp ace.bin"]
        assert files[1]["old_path"] == 'we\tird"q\\b.txt' and files[1]["status"] == "R"
        assert files[2]["status"] == "A" and files[3]["binary"] is True

    def test_content_lines_that_look_like_headers(self):
        f = parse_one("""
            diff --git a/q.sql b/q.sql
            index 1..2 100644
            --- a/q.sql
            +++ b/q.sql
            @@ -1,3 +1,3 @@
             select 1;
            --- old comment
            +++ new comment
             select 2;
            """)
        assert [(row["t"], row["s"]) for row in rows(f)] == [
            ("ctx", "select 1;"), ("del", "-- old comment"), ("add", "++ new comment"), ("ctx", "select 2;")]
        assert f["lang"] == "sql"

    def test_empty_context_line(self):
        f = parse_one("""
            diff --git a/e.txt b/e.txt
            index 1..2 100644
            --- a/e.txt
            +++ b/e.txt
            @@ -1,3 +1,3 @@
             a

            -b
            +B
            """)
        assert rows(f)[1] == {"t": "ctx", "o": 2, "n": 2, "s": ""}
        assert [row["t"] for row in rows(f)] == ["ctx", "ctx", "del", "add"]

    def test_type_change_is_two_sections(self):
        files = gitx.parse_patch(textwrap.dedent("""
            diff --git a/t.txt b/t.txt
            deleted file mode 100644
            index eb5a316..0000000
            --- a/t.txt
            +++ /dev/null
            @@ -1 +0,0 @@
            -target
            diff --git a/t.txt b/t.txt
            new file mode 120000
            index 0000000..ce2e52a
            --- /dev/null
            +++ b/t.txt
            @@ -0,0 +1 @@
            +renamed.txt
            \\ No newline at end of file
            diff --git a/other.txt b/other.txt
            new file mode 100644
            index 0000000..1111111
            --- /dev/null
            +++ b/other.txt
            @@ -0,0 +1 @@
            +o
            """).lstrip("\n"))
        assert [f["path"] for f in files] == ["t.txt", "other.txt"]
        t = files[0]
        assert t["status"] == "T" and (t["old_mode"], t["new_mode"]) == ("100644", "120000")
        assert (t["old_blob"], t["new_blob"]) == ("eb5a316", "ce2e52a")
        assert t["hunk_count"] == 2 and t["additions"] == 1 and t["deletions"] == 1
        assert rows(t)[-1] == {"t": "add", "o": None, "n": 1, "s": "renamed.txt", "nonl": True}

    def test_cr_and_truncation(self):
        long = "x" * 20001
        f = parse_one("""
            diff --git a/c.txt b/c.txt
            index 1..2 100644
            --- a/c.txt
            +++ b/c.txt
            @@ -1,2 +1,2 @@
             keep\r
            -old\r
            +%s
            """ % long)
        keep, old, new = rows(f)
        assert keep == {"t": "ctx", "o": 1, "n": 1, "s": "keep", "cr": True}
        assert old == {"t": "del", "o": 2, "n": None, "s": "old", "cr": True}
        assert new["trunc"] is True and len(new["s"]) == 20000 and "cr" not in new
        trimmed = gitx.trim_file_diff(f)
        assert trimmed["too_large"] is True and trimmed["reason"] == "file" and trimmed["hunks"] == []
        assert trimmed["line_count"] == 3 and f["hunks"], "the input must not be mutated"

    def test_empty_and_noise(self):
        assert gitx.parse_patch("") == []
        assert gitx.parse_patch("not a patch\n\n") == []

    def test_malformed_hunks(self):
        header = "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n"
        for body in ("@@ -1,2 +1,2 @@\n a\n", "@@ -1 +1 @@\n+a\n+b\n", "@@ -1,2 +1,1 @@\n-a\n-b\n c\n",
                     "@@ -1 +1 @@\n?bad\n", "@@ garbage @@\n"):
            with pytest.raises(GitError, match="malformed hunk"):
                gitx.parse_patch(header + body)
        # lines after a completed hunk that are neither a hunk nor a section header are ignored (e-mail trailers)
        assert gitx.parse_patch(header + "@@ -1 +1 @@\n-a\n+b\n-- \n2.44.0\n")[0]["additions"] == 1
        with pytest.raises(GitError, match="unexpected line in diff header"):
            gitx.parse_patch("diff --git a/x b/x\nbogus header line\n")

    def test_parse_raw_and_patch(self, fixture_repo):
        r = fixture_repo
        data = r.git("diff", "--raw", "-z", "-p", "-M", "-C", "--abbrev=40", "--no-textconv", r.sha(F3), r.sha(F4))
        files = gitx.parse_raw_and_patch(data)
        assert [(f["path"], f["status"], f["hunk_count"]) for f in files] == [
            ("CHANGELOG.md", "T", 2), ("link_to_readme", "A", 1), ("notes/nonl.txt", "M", 1),
            ("scripts/run.sh", "M", 0), ("win/crlf.txt", "M", 1)]
        assert all(SHA_RE.match(f["new_blob"]) for f in files)
        assert gitx.parse_raw_and_patch(b"") == [] and gitx.parse_raw_and_patch(b"\0") == []
        # a raw record without a patch section gets hunks: []
        raw_only = b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0lonely.txt\0"
        (lonely,) = gitx.parse_raw_and_patch(raw_only)
        assert lonely["path"] == "lonely.txt" and lonely["hunks"] == [] and lonely["old_blob"] == "a" * 40
        with pytest.raises(GitError, match="unexpected token"):
            gitx.parse_raw_and_patch(b"garbage\0")


# --------------------------------------------------------------------------- trimming & misc

class TestTrimming:
    def test_per_file_and_full(self, fixture_repo):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.big, r.empty)
        trimmed = gitx.trim_commit_diff(diff)
        big, cfg = trimmed["files"]
        assert big["too_large"] is True and big["reason"] == "file" and big["hunks"] == [] and big["line_count"] == 6000
        assert cfg["too_large"] is False and cfg["hunks"]
        assert diff["files"][0]["hunks"], "the cached diff stays untrimmed"
        assert gitx.trim_commit_diff(diff, full=True)["files"][0]["hunks"]
        assert trimmed["stats"] == diff["stats"] and trimmed["sha"] == diff["sha"]

    def test_response_cap_trims_largest_first(self, fixture_repo, monkeypatch):
        r = fixture_repo
        diff = gitx.diff_commit(r.path, r.big, r.empty)
        monkeypatch.setattr(gitx, "FILE_LINE_CAP", 10000)
        monkeypatch.setattr(gitx, "RESPONSE_LINE_CAP", 100)
        big, cfg = gitx.trim_commit_diff(diff)["files"]
        assert big["too_large"] is True and big["reason"] == "response"
        assert cfg["too_large"] is False
        monkeypatch.setattr(gitx, "RESPONSE_LINE_CAP", 5)
        big, cfg = gitx.trim_commit_diff(diff)["files"]
        assert big["reason"] == "response" and cfg["reason"] == "response"
        assert all(f["too_large"] is False for f in gitx.trim_commit_diff(diff, full=True)["files"])


class TestMisc:
    @pytest.mark.parametrize("path, lang", [
        ("a.py", "python"), ("x/y.c", "c"), ("z.h", "c"), ("a.cc", "cpp"), ("a.hpp", "cpp"), ("a.rs", "rust"),
        ("a.go", "go"), ("a.mjs", "javascript"), ("a.jsx", "javascript"), ("a.tsx", "typescript"), ("A.java", "java"),
        ("a.kts", "kotlin"), ("a.scala", "scala"), ("a.rb", "ruby"), ("a.php", "php"), ("a.cs", "csharp"),
        ("a.swift", "swift"), ("a.zsh", "bash"), ("a.sql", "sql"), ("a.htm", "xml"), ("a.svg", "xml"), ("a.css", "css"),
        ("a.scss", "scss"), ("a.less", "less"), ("a.jsonc", "json"), ("a.yml", "yaml"), ("a.toml", "ini"),
        ("a.conf", "ini"), ("README.markdown", "markdown"), ("Makefile", "makefile"), ("makefile", "makefile"),
        ("rules.mk", "makefile"), ("CMakeLists.txt", "cmake"), ("x.cmake", "cmake"), ("Dockerfile", "dockerfile"),
        ("dev.dockerfile", "dockerfile"), ("a.proto", "protobuf"), ("a.lua", "lua"), ("a.pm", "perl"), ("a.R", "r"),
        ("a.r", "r"), ("a.mm", "objectivec"), ("a.hrl", "erlang"), ("a.hs", "haskell"), ("a.mli", "ocaml"),
        ("a.nix", "nix"), ("build.gradle", "groovy"), ("a.txt", "plaintext"), ("a.patch", "diff"), ("a.vb", "vbnet"),
        ("a.wat", "wasm"), ("a.gql", "graphql"), (".gitattributes", None), ("LICENSE", None), ("a.unknownext", None),
        ("", None), (None, None), ("dir.py/file", None),
    ])
    def test_guess_lang(self, path, lang):
        assert gitx.guess_lang(path) == lang

    def test_pseudo_meta_and_stats_summary(self):
        meta = gitx.pseudo_meta("worktree", "worktree", "Uncommitted changes")
        assert meta["sha"] == meta["short_sha"] == "worktree" and meta["parents"] == [] and meta["author_date"] is None
        assert gitx.stats_summary([{"additions": 1, "deletions": 2}, {"additions": 3, "deletions": 0}]) == {
            "files": 2, "additions": 4, "deletions": 2}

    def test_public_api_complete(self):
        for name in ("GitError", "check_version", "toplevel", "rev_parse", "merge_base", "current_branch", "empty_tree",
                     "resolve_range", "list_commits", "commit_stats", "diff_commit", "diff_range", "diff_worktree",
                     "show_file", "map_line", "parse_patch", "parse_raw_and_patch", "trim_file_diff", "trim_commit_diff"):
            assert callable(getattr(gitx, name)), name


class TestMapLineCopies:
    def test_a_copy_does_not_hijack_the_still_existing_original(self, tmp_path):
        """git orders -C entries by destination, so a copy sorted before the original used to win."""
        from conftest import run_git
        repo = tmp_path / "copyrepo"
        repo.mkdir()
        run_git(str(repo), ["init", "-q", "-b", "main"])
        author = ["-c", "user.name=T", "-c", "user.email=t@example.com"]
        (repo / "zz.txt").write_text("".join("z%d\n" % i for i in range(1, 11)))
        run_git(str(repo), ["add", "-A"])
        run_git(str(repo), author + ["commit", "-qm", "base"])
        c1 = run_git(str(repo), ["rev-parse", "HEAD"]).decode().strip()
        (repo / "zz.txt").write_text((repo / "zz.txt").read_text() + "z11\n")
        (repo / "aa.txt").write_text((repo / "zz.txt").read_text())
        run_git(str(repo), ["add", "-A"])
        run_git(str(repo), author + ["commit", "-qm", "copy"])
        c2 = run_git(str(repo), ["rev-parse", "HEAD"]).decode().strip()
        assert gitx.map_line(str(repo), c1, c2, "zz.txt", 5) == {"path": "zz.txt", "line": 5, "status": "same"}


# --------------------------------------------------------------------------- rebuild_tree / diff_since (3.2)

def changed_rows(file_diff):
    return [(row["t"], row["s"]) for hunk in file_diff["hunks"] for row in hunk["lines"] if row["t"] != "ctx"]


class TestSince:
    def test_the_reviewed_version_rebuilt_on_a_new_base_keeps_what_the_base_brought(self, rereview_repo):
        r = rereview_repo
        refs = r.git("for-each-ref")
        old_base = gitx.merge_base(r.path, r.reviewed, r.base2)
        assert old_base == r.base1
        rebuilt = gitx.rebuild_tree(r.path, old_base, r.base2, r.reviewed)
        assert SHA_RE.match(rebuilt["tree"]) and rebuilt["conflicts"] == []
        assert gitx.show_file(r.path, rebuilt["tree"], "src/upstream.py")["content"] == "VERSION = 2"
        assert "def divide(a, b):\n    return a / b" in gitx.show_file(r.path, rebuilt["tree"], "src/calc.py")["content"]
        assert r.git("for-each-ref") == refs, "only objects are written, no refs"

    def test_since_shows_only_what_the_author_changed(self, rereview_repo):
        r = rereview_repo
        rebuilt = gitx.rebuild_tree(r.path, r.base1, r.base2, r.reviewed)
        diff = gitx.diff_since(r.path, rebuilt["tree"], r.reviewed, r.v2, rebuilt["conflicts"])
        assert (diff["sha"], diff["kind"], diff["subject"]) == ("since", "since", "Since your last review")
        assert [f["path"] for f in diff["files"]] == ["src/calc.py", "tests/test_calc.py"], \
            "upstream.py and the new first line of calc.py came with the base, so they cancel out"
        calc = diff["files"][0]
        assert changed_rows(calc) == [("del", "    return a * b"), ("add", "    return a * b if b else 0"),
                                      ("add", "    if b == 0:"), ("add", '        raise ZeroDivisionError("b is zero")')]
        assert (calc["old_rev"], calc["new_rev"]) == (rebuilt["tree"], r.v2)
        assert diff["stats"]["files"] == 2

    def test_a_conflicting_path_is_compared_with_the_reviewed_commit(self, rereview_repo):
        r = rereview_repo
        rebuilt = gitx.rebuild_tree(r.path, r.base1, r.base3, r.reviewed)
        assert rebuilt["conflicts"] == ["src/shared.py"]
        diff = gitx.diff_since(r.path, rebuilt["tree"], r.reviewed, r.v3, rebuilt["conflicts"])
        assert [f["path"] for f in diff["files"]] == ["src/calc.py", "src/shared.py", "tests/test_calc.py"]
        shared = diff["files"][1]
        assert (shared["old_rev"], shared["new_rev"]) == (r.reviewed, r.v3)
        assert changed_rows(shared) == [("del", "MODE = 'fast'"), ("add", "MODE = 'safe'")], \
            "the base's own change of that file shows up: that is what the caller warns about"
        assert diff["files"][0]["old_rev"] == rebuilt["tree"]
        ws = gitx.diff_since(r.path, rebuilt["tree"], r.reviewed, r.v3, rebuilt["conflicts"], ws_ignore=True)
        assert [f["path"] for f in ws["files"]] == ["src/calc.py", "src/shared.py", "tests/test_calc.py"]

    def test_without_a_tree_the_reviewed_commit_is_compared_on_the_given_paths(self, rereview_repo):
        r = rereview_repo
        diff = gitx.diff_since(r.path, None, r.reviewed, r.v2, paths=["src/calc.py", "src/upstream.py"])
        assert [(f["path"], f["old_rev"]) for f in diff["files"]] == [("src/calc.py", r.reviewed),
                                                                      ("src/upstream.py", r.reviewed)]
        assert gitx.diff_since(r.path, None, r.reviewed, r.v2)["files"] == [], "no paths, nothing to compare"

    def test_bad_revisions_are_refused(self, rereview_repo):
        with pytest.raises(GitError):
            gitx.rebuild_tree(rereview_repo.path, "--output=x", rereview_repo.base2, rereview_repo.reviewed)
        with pytest.raises(GitError):
            gitx.rebuild_tree(rereview_repo.path, rereview_repo.base1, rereview_repo.base2, "0" * 40)

    def test_blame_names_the_commit_that_last_changed_each_line(self, rereview_repo):
        r = rereview_repo
        blamed = gitx.blame_lines(r.path, r.v2, "src/calc.py", [30, 24, 25, 29])
        assert [(b["line"], b["commit"], b["path"], b["orig_line"]) for b in blamed] == [
            (24, r.base1, "src/calc.py", 24), (25, r.v2, "src/calc.py", 25), (29, r.v2, "src/calc.py", 29),
            (30, r.v2, "src/calc.py", 30)]
        assert gitx.blame_lines(r.path, r.v2, "src/calc.py", [1])[0]["commit"] == r.base2
        with pytest.raises(GitError):
            gitx.blame_lines(r.path, r.v2, "src/calc.py", [0])
        with pytest.raises(GitError):
            gitx.blame_lines(r.path, r.v2, "no/such/file", [1])

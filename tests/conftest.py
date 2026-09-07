"""Shared pytest fixtures for ccr: a deterministic git repository covering every diff shape (SPEC 9).

``build_fixture_repo(path)`` creates the repository described in SPEC.md section 9 and returns a
:class:`FixtureRepo` handle; the ``fixture_repo`` fixture builds a fresh one per test so tests may
mutate it freely.  Every commit has a fixed author, committer and date, so shas are stable across
runs (they are still looked up by subject rather than hard-coded).

Layout produced (``main`` first, then ``feature``, which is checked out at the end):

    main:    Initial commit -> Add helper module -> Tweak config -> Document usage -> Add run script
             -> Hotfix on main (after ``feature`` branched off "Add run script")
    feature: Modify app in three hunks -> Rename utilities and drop the guide
             -> Add binary, unicode, CRLF and no-newline files
             -> Edit no-newline file, chmod, symlink and CRLF (incl. a file -> symlink type change)
             -> Merge main into feature -> Empty commit -> Generate a big file (+ whitespace-only edit)
    worktree: staged edit (src/utils.py), unstaged edit (README.md), untracked.txt, untracked.bin,
              untracked_link -> README.md, ignored.log (via .gitignore), nested/ (an inner git repo)
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

FIXTURE_ENV = {
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
FIRST_EPOCH = 1700000000  # 2023-11-14T22:13:20Z; each commit is one minute later
EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

MAIN_SUBJECTS = [
    "Initial commit",
    "Add helper module",
    "Tweak config",
    "Document usage",
    "Add run script",
]
HOTFIX_SUBJECT = "Hotfix on main"
FEATURE_SUBJECTS = [
    "Modify app in three hunks",
    "Rename utilities and drop the guide",
    "Add binary, unicode, CRLF and no-newline files",
    "Edit no-newline file, chmod, symlink and CRLF",
    "Merge main into feature",
    "Empty commit",
    "Generate a big file",
]
DOCUMENT_USAGE_BODY = "Explain the CLI flags in the guide.\n\nCloses #12"
BIG_FILE_LINES = 6000
UNICODE_PATH = "dir with space/ünïcode.txt"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"


def _app_lines(count: int = 30) -> list:
    return ["value_%02d = %d" % (i, i) for i in range(1, count + 1)]


APP_ORIGINAL = "\n".join(_app_lines()) + "\n"
UTIL_ORIGINAL = "\n".join(
    ["\"\"\"Utility helpers.\"\"\"", "", "", "def add(a, b):", "    return a + b", "", "",
     "def sub(a, b):", "    return a - b", "", "", "def mul(a, b):", "    return a * b", ""])
CONFIG_ORIGINAL = "[server]\nport = 8080\nhost = localhost\n\n[client]\nretries = 3\n"


def _three_hunk_edit(text: str) -> str:
    """Change line 5, delete lines 15-16 and insert three lines after line 25 (three separate hunks)."""
    lines = text.split("\n")[:-1]
    lines[4] = "value_05 = 500  # changed"
    del lines[14:16]
    insert_at = lines.index("value_25 = 25") + 1
    lines[insert_at:insert_at] = ["inserted_a = 'a'", "inserted_b = 'b'", "inserted_c = 'c'"]
    return "\n".join(lines) + "\n"


class FixtureRepo:
    """Handle to a built fixture repository.

    ``path`` is the absolute toplevel; ``shas`` maps every commit subject to its full sha.  The
    object is ``os.PathLike`` and ``str()``-able so it can be passed anywhere a path is expected.
    """

    def __init__(self, path: str, shas: dict):
        self.path = path
        self.shas = shas

    def __fspath__(self) -> str:
        return self.path

    def __str__(self) -> str:
        return self.path

    def sha(self, subject: str) -> str:
        return self.shas[subject]

    @property
    def root(self) -> str:
        return self.shas[MAIN_SUBJECTS[0]]

    @property
    def main(self) -> str:
        return self.shas[HOTFIX_SUBJECT]

    @property
    def branch_point(self) -> str:
        """The commit ``feature`` branched from (last of the five original ``main`` commits)."""
        return self.shas[MAIN_SUBJECTS[-1]]

    @property
    def feature(self) -> str:
        return self.shas[FEATURE_SUBJECTS[-1]]

    @property
    def merge(self) -> str:
        return self.shas["Merge main into feature"]

    @property
    def empty(self) -> str:
        return self.shas["Empty commit"]

    @property
    def big(self) -> str:
        return self.shas["Generate a big file"]

    @property
    def feature_chain(self) -> list:
        """Shas of ``main..feature`` in chain order (oldest first)."""
        return [self.shas[s] for s in FEATURE_SUBJECTS]

    def git(self, *args: str, check: bool = True, stdin: bytes = None) -> bytes:
        """Run git inside the repository with the isolated fixture environment; return stdout bytes."""
        return run_git(self.path, list(args), check=check, stdin=stdin)

    def text(self, *args: str) -> str:
        return self.git(*args).decode("utf-8", "replace").rstrip("\n")

    def write(self, rel: str, content, mode: str = "w") -> str:
        """Write ``content`` to ``rel`` (creating directories); returns the absolute path."""
        return write_file(self.path, rel, content, mode)


def fixture_env(extra: dict = None) -> dict:
    """Environment for fixture git calls: caller env minus ``GIT_*``, plus deterministic identity."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(FIXTURE_ENV)
    if extra:
        env.update(extra)
    return env


def run_git(cwd: str, args: list, check: bool = True, stdin: bytes = None, extra_env: dict = None) -> bytes:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"] + args,
        cwd=cwd, env=fixture_env(extra_env), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=None if stdin is not None else subprocess.DEVNULL, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed (%d): %s" % (" ".join(args), proc.returncode, proc.stderr.decode("utf-8", "replace")))
    return proc.stdout


def write_file(root: str, rel: str, content, mode: str = "w") -> str:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if "b" in mode:
        with open(full, mode) as fh:
            fh.write(content)
    else:
        with open(full, mode, encoding="utf-8", newline="") as fh:
            fh.write(content)
    return full


class _Builder:
    """Sequential commit builder with deterministic, monotonically increasing dates."""

    def __init__(self, path: str):
        self.path = path
        self.shas = {}
        self.count = 0

    def git(self, *args: str, stdin: bytes = None) -> bytes:
        return run_git(self.path, list(args), stdin=stdin)

    def write(self, rel: str, content, mode: str = "w") -> None:
        write_file(self.path, rel, content, mode)

    def remove(self, rel: str) -> None:
        os.remove(os.path.join(self.path, rel))

    def symlink(self, rel: str, target: str) -> None:
        full = os.path.join(self.path, rel)
        if os.path.lexists(full):
            os.remove(full)
        os.symlink(target, full)

    def commit(self, subject: str, body: str = "", extra: list = None) -> str:
        """``git add -A`` (unless ``extra`` says otherwise) and commit with the next fixture date."""
        self.count += 1
        date = "@%d +0000" % (FIRST_EPOCH + 60 * self.count)
        message = subject if not body else "%s\n\n%s" % (subject, body)
        args = ["commit", "-q", "-m", message] + (extra or [])
        run_git(self.path, args, extra_env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        sha = self.git("rev-parse", "HEAD").decode().strip()
        self.shas[subject] = sha
        return sha


def build_fixture_repo(path) -> FixtureRepo:
    """Build the section-9 fixture repository at ``path`` (created if needed) and return its handle."""
    path = os.path.abspath(os.fspath(path))
    os.makedirs(path, exist_ok=True)
    b = _Builder(path)
    b.git("init", "-q")
    b.git("symbolic-ref", "HEAD", "refs/heads/main")
    b.git("config", "user.name", FIXTURE_ENV["GIT_AUTHOR_NAME"])
    b.git("config", "user.email", FIXTURE_ENV["GIT_AUTHOR_EMAIL"])
    b.git("config", "core.autocrlf", "false")
    b.git("config", "diff.hex.textconv", "xxd")

    # --- main -------------------------------------------------------------------------------
    b.write("README.md", "# Fixture\n\nA repository used by the ccr test-suite.\n")
    b.write(".gitignore", "ignored.log\n*.tmp\n")
    b.write("src/app.py", APP_ORIGINAL)
    b.write("src/util.py", UTIL_ORIGINAL)
    b.write("data/config.ini", CONFIG_ORIGINAL)
    b.write("docs/guide.txt", "Guide\n=====\n\nRun the app with --help.\n")
    b.git("add", "-A")
    b.commit(MAIN_SUBJECTS[0])

    b.write("src/helper.py", "def helper():\n    return 'help'\n\n\ndef other():\n    return 'other'\n")
    b.write("Makefile", "all:\n\tpython3 -m app\n")
    b.write("CHANGELOG.md", "# Changelog\n\n- initial release\n")
    b.git("add", "-A")
    b.commit(MAIN_SUBJECTS[1])

    b.write("data/config.ini", CONFIG_ORIGINAL.replace("retries = 3", "retries = 5"))
    b.git("add", "-A")
    b.commit(MAIN_SUBJECTS[2])

    b.write("docs/guide.txt", "Guide\n=====\n\nRun the app with --help.\nUse --verbose for details.\n")
    b.git("add", "-A")
    b.commit(MAIN_SUBJECTS[3], body=DOCUMENT_USAGE_BODY)

    b.write("scripts/run.sh", "#!/bin/sh\nexec python3 -m app \"$@\"\n")
    b.git("add", "-A")
    b.commit(MAIN_SUBJECTS[4])

    b.git("branch", "feature")
    b.write("hotfix.txt", "hotfix applied on main\n")
    b.git("add", "-A")
    b.commit(HOTFIX_SUBJECT)

    # --- feature ----------------------------------------------------------------------------
    b.git("checkout", "-q", "feature")
    b.write("src/app.py", _three_hunk_edit(APP_ORIGINAL))
    b.git("add", "-A")
    b.commit(FEATURE_SUBJECTS[0])

    b.git("mv", "src/util.py", "src/utils.py")
    b.write("src/utils.py", UTIL_ORIGINAL.replace("def add(a, b):\n    return a + b",
                                                    "def add(a, b, c=0):\n    return a + b + c"))
    os.makedirs(os.path.join(path, "lib"))
    b.git("mv", "src/helper.py", "lib/helper.py")
    b.git("rm", "-q", "docs/guide.txt")
    b.git("add", "-A")
    b.commit(FEATURE_SUBJECTS[1])

    b.write("assets/logo.png", PNG_BYTES, "wb")
    b.write(UNICODE_PATH, "héllo wörld\nsecond line\n")
    b.write("notes/nonl.txt", "first line\nlast line without newline")
    b.write("win/crlf.txt", "alpha\r\nbeta\r\ngamma\r\n")
    b.write(".gitattributes", "*.dat diff=hex\n")
    b.write("data/blob.dat", b"\x00\x01\x02\x03binary payload\x00", "wb")
    b.git("add", "-A")
    b.commit(FEATURE_SUBJECTS[2])

    b.write("notes/nonl.txt", "first line\nLAST line without newline")
    os.chmod(os.path.join(path, "scripts/run.sh"), 0o755)
    b.symlink("link_to_readme", "README.md")
    b.remove("CHANGELOG.md")
    b.symlink("CHANGELOG.md", "README.md")
    b.write("win/crlf.txt", "alpha\r\nBETA\r\ngamma\r\n")
    b.git("add", "-A")
    b.commit(FEATURE_SUBJECTS[3])

    b.count += 1
    merge_date = "@%d +0000" % (FIRST_EPOCH + 60 * b.count)
    run_git(path, ["merge", "-q", "--no-ff", "--no-edit", "-m", FEATURE_SUBJECTS[4], "main"],
            extra_env={"GIT_AUTHOR_DATE": merge_date, "GIT_COMMITTER_DATE": merge_date})
    b.shas[FEATURE_SUBJECTS[4]] = b.git("rev-parse", "HEAD").decode().strip()

    b.commit(FEATURE_SUBJECTS[5], extra=["--allow-empty"])

    b.write("big/generated.txt", "".join("generated line %d\n" % i for i in range(1, BIG_FILE_LINES + 1)))
    b.write("data/config.ini", CONFIG_ORIGINAL.replace("retries = 3", "retries = 5")
            .replace("port = 8080", "port   =   8080").replace("host = localhost", "host = localhost   "))
    b.git("add", "-A")
    b.commit(FEATURE_SUBJECTS[6])

    # --- working tree -----------------------------------------------------------------------
    b.write("src/utils.py", UTIL_ORIGINAL.replace("def add(a, b):\n    return a + b",
                                                    "def add(a, b, c=0):\n    return a + b + c") + "\n\nSTAGED = True\n")
    b.git("add", "src/utils.py")
    b.write("README.md", "# Fixture\n\nA repository used by the ccr test-suite.\n\nUnstaged edit.\n")
    b.write("untracked.txt", "untracked line 1\nuntracked line 2\n")
    b.write("untracked.bin", b"\x00\x01\x02binary untracked\x00\xff", "wb")
    os.symlink("README.md", os.path.join(path, "untracked_link"))
    b.write("ignored.log", "this file is ignored\n")
    nested = os.path.join(path, "nested")
    os.makedirs(nested)
    run_git(nested, ["init", "-q"])
    write_file(nested, "inner.txt", "inside a nested repository\n")
    return FixtureRepo(path, dict(b.shas))


@pytest.fixture(scope="session", autouse=True)
def _isolated_home(tmp_path_factory):
    """Point HOME/XDG at a scratch dir so neither the fixture nor ccr sees the developer's git config."""
    home = tmp_path_factory.mktemp("home")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        mp.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            mp.delenv(key, raising=False)
        yield home


@pytest.fixture(autouse=True)
def ccr_session_dir(tmp_path, monkeypatch):
    """Every test gets a private ``CCR_SESSION_DIR`` so nothing touches ``~/.cache``."""
    session_dir = tmp_path / "ccr-sessions"
    session_dir.mkdir(mode=0o700)
    monkeypatch.setenv("CCR_SESSION_DIR", str(session_dir))
    return session_dir


@pytest.fixture
def fixture_repo(tmp_path) -> FixtureRepo:
    """A freshly built section-9 repository (see the module docstring for its layout)."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    return build_fixture_repo(tmp_path / "repo")

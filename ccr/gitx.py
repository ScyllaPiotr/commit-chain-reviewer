"""Git extraction layer for ccr (SPEC.md section 3).

Every public function shells out to ``git`` with a hardened argument vector
(section 3.1), parses the NUL-separated machine output into plain dicts
(CommitMeta / FileStat / FileDiff, sections 2.2-2.3) and never modifies the
repository.  ``parse_patch`` and ``parse_raw_and_patch`` are pure parsers of
unified-diff text; the other helpers wrap one git invocation each.

Byte layouts verified against git 2.55.0 (``tests/test_gitx.py`` re-checks them):

* ``git diff --raw -z -p``: the raw block is a sequence of NUL-terminated
  records followed by one extra NUL, so the patch text starts right after the
  first ``\\0\\0``.  A diff with no files prints nothing at all.  A type change
  (status ``T``) yields one raw record but two patch sections (deletion, then
  creation).  With ``-w`` a file whose content changes are all whitespace
  disappears from the raw block as well, unless its mode also changed, in
  which case the raw record stays and its section has no hunks.  Unquoted
  paths containing a space get a trailing TAB on the ``---``/``+++`` lines;
  paths with control characters, quotes or backslashes are C-quoted.
* ``git diff [--raw] --numstat -z`` and ``git diff-tree --stdin ...``: all raw
  records come first, then all numstat records.  With ``--stdin`` each commit
  block is introduced by the commit sha alone (not the full input line) plus
  NUL; commits whose diff is empty print nothing, and a merge given without
  its parent prints nothing either (hence ``<sha> <parent>`` input lines).
* ``git log -z``: every record, including the last, is NUL-terminated and
  ``%B`` keeps its trailing newline.
* ``git log`` rejects options such as ``-1``/``--first-parent`` after
  ``--end-of-options``; they must precede it.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone

__all__ = [
    "GitError",
    "check_version",
    "toplevel",
    "rev_parse",
    "merge_base",
    "current_branch",
    "empty_tree",
    "resolve_range",
    "list_commits",
    "commit_stats",
    "diff_commit",
    "diff_range",
    "diff_worktree",
    "show_file",
    "map_line",
    "parse_patch",
    "parse_raw_and_patch",
    "guess_lang",
    "stats_summary",
    "trim_file_diff",
    "trim_commit_diff",
    "pseudo_meta",
]

MIN_GIT_VERSION = (2, 24)
MAX_LINE_CHARS = 20000
FILE_LINE_CAP = 5000
RESPONSE_LINE_CAP = 30000
MAX_RANGE_COMMITS = 2000
BINARY_SNIFF_BYTES = 8000
UNTRACKED_MAX_BYTES = 1 << 20
SHOW_FILE_MAX_BYTES = 8 << 20
BODY_MAX_BYTES = 64 << 10
SHORT_SHA_LEN = 10
INDEX_LOCK_RETRY_DELAY = 0.2

_CONFIG_ARGS = [
    "-c", "core.quotepath=false",
    "-c", "color.ui=never",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.suppressBlankEmpty=false",
    "-c", "diff.submodule=short",
    "-c", "diff.relative=false",
    "-c", "log.showSignature=false",
]
_DIFF_FLAGS = [
    "--no-ext-diff", "--no-textconv", "--no-color", "-M", "-C",
    "--src-prefix=a/", "--dst-prefix=b/", "--submodule=short", "--abbrev=40",
]
_PATCH_CONTEXT = ["-U3"]  # only with -p: -U implies --patch, which would pollute --raw/--numstat-only output
_LOG_FLAGS = ["--encoding=UTF-8", "--no-show-signature"]
_LOG_FORMAT = "--format=%H%x00%P%x00%an%x00%ae%x00%at%x00%ct%x00%B"
_LOG_FIELDS = 7
_KEPT_GIT_ENV = ("GIT_CONFIG_NOSYSTEM", "GIT_SSH", "GIT_TRACE")
_DEFAULT_BASE_REFS = ("main", "master", "origin/main", "origin/master", "origin/HEAD")

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}

_cache_lock = threading.Lock()
_empty_tree_cache: dict = {}
_map_line_cache: dict = {}
_MAP_LINE_CACHE_MAX = 4096


class GitError(Exception):
    """A git invocation or parse failure; ``status`` is the matching HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- process plumbing

def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _env() -> dict:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") or key.startswith(_KEPT_GIT_ENV)
    }
    env.update(LC_ALL="C", GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat")
    return env


def _git(repo, args, stdin_data=None, ok_codes=(0,)) -> subprocess.CompletedProcess:
    """Run one git command per section 3.1 and return the completed process.

    ``repo`` may be None for repository-independent commands (``git --version``).
    ``stdin_data`` switches stdin from DEVNULL to a pipe fed with those bytes.
    Exit codes outside ``ok_codes`` raise GitError carrying git's stderr.
    """
    argv = ["git"]
    if repo is not None:
        if not os.path.isdir(repo):
            raise GitError("%s is not a directory" % repo)
        argv += ["-C", repo]
    argv += _CONFIG_ARGS + ["--no-pager"] + list(args)
    stdin = subprocess.DEVNULL if stdin_data is None else None
    try:
        proc = subprocess.run(
            argv, cwd=repo, env=_env(), shell=False, stdin=stdin, input=stdin_data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except FileNotFoundError:
        raise GitError("git executable not found on PATH") from None
    if proc.returncode not in ok_codes:
        message = _decode(proc.stderr).strip() or "git %s failed with exit code %d" % (args[0], proc.returncode)
        raise GitError(message)
    return proc


def _check_rev_arg(rev) -> str:
    """Reject revision strings that could be mistaken for options or contain whitespace/NUL."""
    if not isinstance(rev, str) or not rev or rev.startswith("-") or any(c.isspace() or c == "\0" for c in rev):
        raise GitError("invalid revision %r" % (rev,))
    return rev


def _short(sha: str) -> str:
    return sha[:SHORT_SHA_LEN]


def _is_sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


# --------------------------------------------------------------------------- simple queries

def check_version() -> tuple:
    """Return the installed git version as a tuple of ints; refuse anything older than 2.24."""
    out = _decode(_git(None, ["--version"]).stdout)
    match = _VERSION_RE.search(out)
    if not match:
        raise GitError("cannot parse git version from %r" % out.strip())
    version = tuple(int(part or 0) for part in match.groups())
    if version < MIN_GIT_VERSION:
        raise GitError("git %s is too old; ccr needs git >= %d.%d" % (".".join(map(str, version)), *MIN_GIT_VERSION))
    return version


def toplevel(path) -> dict:
    """Locate the repository containing ``path``: ``{"path": toplevel-or-gitdir, "bare": bool}``."""
    path = os.path.abspath(os.fspath(path))
    if not os.path.isdir(path):
        raise GitError("%s is not a directory" % path)
    try:
        bare_out = _git(path, ["rev-parse", "--is-bare-repository"]).stdout
    except GitError:
        raise GitError("%s is not inside a git repository" % path) from None
    bare = bare_out.strip() == b"true"
    flag = "--absolute-git-dir" if bare else "--show-toplevel"
    top = _decode(_git(path, ["rev-parse", flag]).stdout).rstrip("\n")
    return {"path": top, "bare": bare}


def rev_parse(repo, rev) -> str:
    """Full sha of ``rev^{commit}``; GitError(404) when the revision does not name a commit."""
    _check_rev_arg(rev)
    proc = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}"], ok_codes=(0, 1))
    sha = _decode(proc.stdout).strip()
    if proc.returncode != 0 or not _is_sha(sha):
        raise GitError("unknown revision %r" % rev, status=404)
    return sha


def merge_base(repo, a, b):
    """Merge base of two commits, or None when they share no history."""
    _check_rev_arg(a)
    _check_rev_arg(b)
    proc = _git(repo, ["merge-base", "--end-of-options", a, b], ok_codes=(0, 1))
    if proc.returncode != 0:
        return None
    return _decode(proc.stdout).strip() or None


def current_branch(repo):
    """Short name of the checked-out branch, or None when HEAD is detached."""
    proc = _git(repo, ["symbolic-ref", "--short", "-q", "HEAD"], ok_codes=(0, 1))
    if proc.returncode != 0:
        return None
    return _decode(proc.stdout).strip() or None


def empty_tree(repo) -> str:
    """Sha of the empty tree in this repository's hash algorithm (cached per repo)."""
    with _cache_lock:
        cached = _empty_tree_cache.get(repo)
    if cached is None:
        cached = _decode(_git(repo, ["hash-object", "-t", "tree", "--stdin"], stdin_data=b"").stdout).strip()
        with _cache_lock:
            _empty_tree_cache[repo] = cached
    return cached


def _git_common_dir(repo) -> str:
    out = _decode(_git(repo, ["rev-parse", "--git-common-dir"]).stdout).rstrip("\n")
    return os.path.normpath(os.path.join(repo, out))


def _rev_count(repo, rev_range: str, first_parent: bool = False) -> int:
    args = ["rev-list", "--count"] + (["--first-parent"] if first_parent else []) + ["--end-of-options", rev_range, "--"]
    return int(_decode(_git(repo, args).stdout).strip() or 0)


def _rev_exists(repo, rev) -> bool:
    try:
        rev_parse(repo, rev)
    except GitError as exc:
        if exc.status == 404:
            return False
        raise
    return True


# --------------------------------------------------------------------------- range resolution

def resolve_range(repo, spec, n) -> dict:
    """Resolve ``--range SPEC`` / ``-n N`` (section 2.1) into full shas.

    Returns ``{"base", "head", "spec", "given", "note"}`` where ``spec`` is the
    pinned form.  A whole-history range (``-n N`` with ``N >= depth``) is pinned
    as ``<empty tree sha>..HEAD``; that base side resolves back to ``base=None``.
    """
    if spec is not None and n is not None:
        raise GitError("--range and -n are mutually exclusive")
    if n is not None:
        base, head, pinned, given = _resolve_depth(repo, n)
    elif spec is not None:
        base, head, pinned, given = _resolve_spec(repo, spec)
    else:
        base, head, pinned, given = _resolve_default(repo)

    note = None
    if base is not None:
        mb = merge_base(repo, base, head)
        if mb is None:
            raise GitError("base and head have no common ancestor")
        if mb != base:
            note = "base %s is not an ancestor of head; using merge-base %s" % (_short(base), _short(mb))
            base = mb
    count = _rev_count(repo, "%s..%s" % (base, head) if base else head)
    if count > MAX_RANGE_COMMITS:
        raise GitError("range too large; narrow it with --range")
    return {"base": base, "head": head, "spec": pinned, "given": given, "note": note}


def _resolve_depth(repo, n):
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise GitError("-n must be a positive integer")
    head = rev_parse(repo, "HEAD")
    depth = _rev_count(repo, head, first_parent=True)
    if n >= depth:
        return None, head, "%s..HEAD" % empty_tree(repo), "-n %d" % n
    base = rev_parse(repo, "HEAD~%d" % n)
    return base, head, "%s..HEAD" % base, "-n %d" % n


def _resolve_spec(repo, spec):
    if not isinstance(spec, str) or not spec.strip():
        raise GitError("empty range spec")
    if "..." in spec:
        left, right = spec.split("...", 1)
        a = rev_parse(repo, left or "HEAD")
        head = rev_parse(repo, right or "HEAD")
        base = merge_base(repo, a, head)
        if base is None:
            raise GitError("base and head have no common ancestor")
        return base, head, "%s..%s" % (base, right or "HEAD"), spec
    if ".." in spec:
        left, right = spec.split("..", 1)
        head = rev_parse(repo, right or "HEAD")
        if left and left == _empty_tree_if_known(repo, left):
            return None, head, spec, spec
        base = rev_parse(repo, left or "HEAD")
        left_text = left or base
        return base, head, "%s..%s" % (left_text, right or "HEAD"), spec
    base = rev_parse(repo, spec)
    head = rev_parse(repo, "HEAD")
    return base, head, "%s..HEAD" % spec, spec


def _empty_tree_if_known(repo, candidate):
    """Return the empty tree sha when ``candidate`` is exactly it, else None (no git call otherwise)."""
    if _is_sha(candidate) and candidate == empty_tree(repo):
        return candidate
    return None


def _resolve_default(repo):
    head = rev_parse(repo, "HEAD")
    for ref in ("@{upstream}",) + _DEFAULT_BASE_REFS:
        if not _rev_exists(repo, ref):
            continue
        base = rev_parse(repo, ref)
        if _rev_count(repo, "%s..%s" % (base, head)) >= 1:
            return base, head, "%s..HEAD" % ref, None
    raise GitError("cannot infer a range; pass --range or -n")


# --------------------------------------------------------------------------- commits

def _iso(epoch: str) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_message(message: str):
    subject, _, rest = message.partition("\n")
    body = rest.strip("\n")
    if len(body.encode("utf-8")) > BODY_MAX_BYTES:
        body = body.encode("utf-8")[:BODY_MAX_BYTES].decode("utf-8", "ignore")
    return subject.rstrip(), body


def _log_records(repo, options, revs) -> list:
    """Run ``git log -z`` with the section 3.2 format and return CommitMeta dicts (no files/stats).

    ``options`` go before ``--end-of-options`` (git refuses them afterwards), ``revs`` after it.
    """
    args = ["log", "-z", "--reverse", "--topo-order"] + _LOG_FLAGS + [_LOG_FORMAT] + list(options) \
        + ["--end-of-options"] + list(revs) + ["--"]
    tokens = _git(repo, args).stdout.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    if len(tokens) % _LOG_FIELDS:
        raise GitError("unexpected git log output (%d fields)" % len(tokens))
    commits = []
    for i in range(0, len(tokens), _LOG_FIELDS):
        sha, parents, name, email, adate, cdate, message = (_decode(t) for t in tokens[i:i + _LOG_FIELDS])
        subject, body = _split_message(message)
        parent_list = parents.split()
        commits.append({
            "sha": sha,
            "short_sha": _short(sha),
            "kind": "commit",
            "parents": parent_list,
            "is_merge": len(parent_list) > 1,
            "shallow_boundary": False,
            "author": {"name": name, "email": email},
            "author_date": _iso(adate),
            "commit_date": _iso(cdate),
            "subject": subject,
            "body": body,
        })
    return commits


def _mark_shallow(repo, commits) -> None:
    if not any(not c["parents"] for c in commits):
        return
    shallow_file = os.path.join(_git_common_dir(repo), "shallow")
    try:
        with open(shallow_file, "r", encoding="utf-8", errors="replace") as fh:
            shallow = {line.strip() for line in fh}
    except OSError:
        return
    for commit in commits:
        if not commit["parents"] and commit["sha"] in shallow:
            commit["shallow_boundary"] = True


def list_commits(repo, base, head, first_parent=False) -> list:
    """Commits of ``base..head`` (or all of ``head`` when base is None), oldest first."""
    _check_rev_arg(head)
    if base is not None:
        _check_rev_arg(base)
    options = ["--first-parent"] if first_parent else []
    commits = _log_records(repo, options, ["%s..%s" % (base, head) if base else head])
    _mark_shallow(repo, commits)
    return commits


def _commit_meta(repo, sha) -> dict:
    commits = _log_records(repo, ["-1"], [sha])
    if len(commits) != 1:
        raise GitError("unknown commit %s" % sha, status=404)
    _mark_shallow(repo, commits)
    return commits[0]


def pseudo_meta(sha: str, kind: str, subject: str) -> dict:
    """CommitMeta skeleton for the ``combined`` / ``worktree`` pseudo-commits."""
    return {
        "sha": sha,
        "short_sha": sha,
        "kind": kind,
        "parents": [],
        "is_merge": False,
        "shallow_boundary": False,
        "author": {"name": "", "email": ""},
        "author_date": None,
        "commit_date": None,
        "subject": subject,
        "body": "",
    }


def stats_summary(files) -> dict:
    """``{"files", "additions", "deletions"}`` totals over FileStat/FileDiff dicts."""
    return {
        "files": len(files),
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
    }


# --------------------------------------------------------------------------- raw records

def _absent_to_none(value: str):
    """``000000`` modes and all-zero blob ids denote an absent (or unhashed worktree) side."""
    return None if not value or set(value) == {"0"} else value


def _parse_raw_record(tokens, i):
    """Parse the raw record at ``tokens[i]``; return ``(FileStat skeleton, next index)``."""
    head = tokens[i][1:].split(b" ")
    if len(head) != 5 or not head[4]:
        raise GitError("malformed raw diff record %r" % tokens[i][:80])
    old_mode, new_mode, old_blob, new_blob, xscore = (_decode(t) for t in head)
    status, score = xscore[0], int(xscore[1:] or 0)
    needed = 3 if status in "RC" else 2
    if i + needed > len(tokens):
        raise GitError("truncated raw diff record")
    if status in "RC":
        old_path, path = _decode(tokens[i + 1]), _decode(tokens[i + 2])
    else:
        old_path, path = None, _decode(tokens[i + 1])
    skeleton = {
        "path": path,
        "old_path": old_path,
        "status": status,
        "score": score,
        "additions": 0,
        "deletions": 0,
        "binary": False,
        "old_mode": _absent_to_none(old_mode),
        "new_mode": _absent_to_none(new_mode),
        "old_blob": _absent_to_none(old_blob),
        "new_blob": _absent_to_none(new_blob),
    }
    return skeleton, i + needed


def _parse_raw_block(data: bytes) -> list:
    tokens = data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    skeletons = []
    i = 0
    while i < len(tokens):
        if not tokens[i].startswith(b":"):
            raise GitError("unexpected token in raw diff output: %r" % tokens[i][:80])
        skeleton, i = _parse_raw_record(tokens, i)
        skeletons.append(skeleton)
    return skeletons


def _parse_numstat_record(tokens, i):
    """Parse the numstat record at ``tokens[i]``; return ``((additions, deletions, binary, path), next index)``."""
    parts = tokens[i].split(b"\t", 2)
    if len(parts) != 3:
        raise GitError("malformed numstat record %r" % tokens[i][:80])
    add, dele, path = parts
    if path == b"":
        if i + 3 > len(tokens):
            raise GitError("truncated numstat record")
        path, step = tokens[i + 2], 3
    else:
        step = 1
    if add == b"-":
        return (0, 0, True, _decode(path)), i + step
    return (int(add), int(dele), False, _decode(path)), i + step


def commit_stats(repo, commits) -> dict:
    """FileStat lists for many commits from ONE ``git diff-tree --stdin`` run.

    Each commit is diffed against its first parent (root commits against the
    empty tree via ``--root``).  Commits with an empty diff map to ``[]``.
    """
    result = {c["sha"]: [] for c in commits}
    if not commits:
        return result
    lines = []
    for commit in commits:
        _check_rev_arg(commit["sha"])
        parents = commit.get("parents") or []
        lines.append(("%s %s" % (commit["sha"], parents[0])) if parents else commit["sha"])
    args = ["diff-tree", "--stdin", "-r", "--root", "-M", "-C", "--raw", "--numstat", "-z", "--abbrev=40",
            "--no-ext-diff", "--no-textconv", "--no-color"]
    data = _git(repo, args, stdin_data=("\n".join(lines) + "\n").encode("utf-8")).stdout
    for sha, files in _parse_raw_numstat_blocks(data):
        if sha not in result:
            raise GitError("diff-tree reported unexpected commit %s" % sha)
        result[sha] = files
    return result


def _parse_raw_numstat_blocks(data: bytes) -> list:
    """Parse ``--raw --numstat -z`` output into ``[(header, [FileStat]), …]`` blocks.

    ``git diff-tree --stdin`` introduces each commit block with the commit sha
    (header token); plain ``git diff`` output has no header, giving a single
    block whose header is None.  State machine: ``:`` starts a raw record, a
    token containing TAB is a numstat record, anything else is a header.
    """
    tokens = data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    blocks = []
    header, raws, nums = None, [], []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith(b":"):
            skeleton, i = _parse_raw_record(tokens, i)
            raws.append(skeleton)
        elif b"\t" in token:
            record, i = _parse_numstat_record(tokens, i)
            nums.append(record)
        else:
            if raws or nums or header is not None:
                blocks.append((header, _zip_raw_numstat(header, raws, nums)))
            words = _decode(token).split()
            if not words:
                raise GitError("empty header in diff output")
            header, raws, nums = words[0], [], []
            i += 1
    if raws or nums or header is not None:
        blocks.append((header, _zip_raw_numstat(header, raws, nums)))
    return blocks


def _zip_raw_numstat(header, raws, nums) -> list:
    where = " for %s" % header if header else ""
    if len(raws) != len(nums):
        raise GitError("raw/numstat record count mismatch%s (%d vs %d)" % (where, len(raws), len(nums)))
    for skeleton, (add, dele, binary, path) in zip(raws, nums):
        if path != skeleton["path"]:
            raise GitError("raw/numstat path mismatch%s: %r vs %r" % (where, skeleton["path"], path))
        skeleton["additions"], skeleton["deletions"], skeleton["binary"] = add, dele, binary
    return raws


# --------------------------------------------------------------------------- patch parsing

def _unquote_c(token: str):
    """Unquote a C-style ``"..."`` token; return ``(text, remainder after the closing quote)``."""
    out = bytearray()
    i = 1
    while i < len(token):
        ch = token[i]
        if ch == '"':
            return out.decode("utf-8", "replace"), token[i + 1:]
        if ch == "\\" and i + 1 < len(token):
            nxt = token[i + 1]
            if nxt in "01234567":
                digits = re.match(r"[0-7]{1,3}", token[i + 1:]).group(0)
                out.append(int(digits, 8) & 0xFF)
                i += 1 + len(digits)
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
            else:
                out.extend(nxt.encode("utf-8", "replace"))
            i += 2
            continue
        out.extend(ch.encode("utf-8", "replace"))
        i += 1
    raise GitError("unterminated quoted path in diff header")


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix):] if path.startswith(prefix) else path


def _header_path(token: str, prefix: str):
    """Path from a ``--- ``/``+++ `` line (``/dev/null`` -> None); strips the a/ or b/ prefix."""
    if token == "/dev/null":
        return None
    if token.startswith('"'):
        text, _ = _unquote_c(token)
    else:
        text = token[:-1] if token.endswith("\t") else token
    return _strip_prefix(text, prefix)


def _diff_git_paths(header: str):
    """``(old, new)`` from ``diff --git a/X b/Y`` (handles identical, spaced and C-quoted paths)."""
    rest = header[len("diff --git "):]
    if rest.startswith('"'):
        a_text, remainder = _unquote_c(rest)
        b_token = remainder[1:] if remainder.startswith(" ") else remainder
        b_text = _unquote_c(b_token)[0] if b_token.startswith('"') else b_token
        return _strip_prefix(a_text, "a/"), _strip_prefix(b_text, "b/")
    half = len(rest) // 2
    if len(rest) % 2 == 1 and rest[half] == " " and rest[:half][2:] == rest[half + 1:][2:] \
            and rest.startswith("a/") and rest[half + 1:].startswith("b/"):
        same = rest[:half][2:]
        return same, same
    quoted_b = rest.find(' "b/')
    if quoted_b >= 0:
        return _strip_prefix(rest[:quoted_b], "a/"), _strip_prefix(_unquote_c(rest[quoted_b + 1:])[0], "b/")
    split = rest.find(" b/")
    if split < 0:
        raise GitError("malformed diff header: %r" % header[:120])
    return _strip_prefix(rest[:split], "a/"), rest[split + 3:]


def _quoted_or_plain(value: str) -> str:
    return _unquote_c(value)[0] if value.startswith('"') else value


def _new_section(header: str) -> dict:
    return {
        "header": header, "old_mode": None, "new_mode": None, "old_blob": None, "new_blob": None,
        "score": 0, "rename": None, "copy": None, "new_file": False, "deleted": False,
        "has_text_header": False, "minus": None, "plus": None, "binary": False, "hunks": [],
    }


def _parse_index_line(sec: dict, rest: str) -> None:
    blobs, _, mode = rest.partition(" ")
    old_blob, _, new_blob = blobs.partition("..")
    sec["old_blob"] = None if not old_blob or set(old_blob) == {"0"} else old_blob
    sec["new_blob"] = None if not new_blob or set(new_blob) == {"0"} else new_blob
    if mode:
        sec["old_mode"] = sec["old_mode"] or mode
        sec["new_mode"] = sec["new_mode"] or mode


def _parse_section_header(sec: dict, lines: list) -> int:
    """Consume header lines of one section; return the index of the first hunk (or end)."""
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            break
        if line.startswith("old mode "):
            sec["old_mode"] = line[9:].strip()
        elif line.startswith("new mode "):
            sec["new_mode"] = line[9:].strip()
        elif line.startswith("deleted file mode "):
            sec["deleted"], sec["old_mode"] = True, line[18:].strip()
        elif line.startswith("new file mode "):
            sec["new_file"], sec["new_mode"] = True, line[14:].strip()
        elif line.startswith("rename from "):
            sec["rename"] = (_quoted_or_plain(line[12:]), (sec["rename"] or (None, None))[1])
        elif line.startswith("rename to "):
            sec["rename"] = ((sec["rename"] or (None, None))[0], _quoted_or_plain(line[10:]))
        elif line.startswith("copy from "):
            sec["copy"] = (_quoted_or_plain(line[10:]), (sec["copy"] or (None, None))[1])
        elif line.startswith("copy to "):
            sec["copy"] = ((sec["copy"] or (None, None))[0], _quoted_or_plain(line[8:]))
        elif line.startswith("similarity index "):
            sec["score"] = int(line[17:].strip().rstrip("%") or 0)
        elif line.startswith("dissimilarity index "):
            pass
        elif line.startswith("index "):
            _parse_index_line(sec, line[6:].strip())
        elif line.startswith("--- "):
            sec["has_text_header"], sec["minus"] = True, _header_path(line[4:], "a/")
        elif line.startswith("+++ "):
            sec["has_text_header"], sec["plus"] = True, _header_path(line[4:], "b/")
        elif line.startswith("Binary files ") and line.rstrip("\r").endswith(" differ"):
            sec["binary"] = True
        elif line.rstrip("\r") == "GIT binary patch":
            sec["binary"] = True
            return len(lines)
        else:
            raise GitError("unexpected line in diff header: %r" % line[:120])
        i += 1
    return i


def _row(kind: str, old, new, text: str) -> dict:
    row = {"t": kind, "o": old, "n": new, "s": text}
    if text.endswith("\r"):
        row["s"] = text[:-1]
        row["cr"] = True
    if len(row["s"]) > MAX_LINE_CHARS:
        row["s"] = row["s"][:MAX_LINE_CHARS]
        row["trunc"] = True
    return row


def _parse_hunks(lines: list, i: int) -> list:
    """Parse consecutive hunks starting at ``lines[i]`` (section 3.2 hunk rules)."""
    hunks = []
    while i < len(lines) and lines[i].startswith("@@"):
        match = _HUNK_RE.match(lines[i].rstrip("\r"))
        if not match:
            raise GitError("malformed hunk header: %r" % lines[i][:120])
        old_start, new_start = int(match.group(1)), int(match.group(3))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        rows = []
        old_line, new_line = old_start, new_start
        old_left, new_left = old_count, new_count
        i += 1
        while old_left > 0 or new_left > 0:
            if i >= len(lines):
                raise GitError("malformed hunk: truncated")
            body = lines[i]
            i += 1
            marker = body[:1]
            if marker == "\\":
                if rows:
                    rows[-1]["nonl"] = True
            elif marker == " " or body == "":
                if old_left <= 0 or new_left <= 0:
                    raise GitError("malformed hunk: unexpected context line")
                rows.append(_row("ctx", old_line, new_line, body[1:]))
                old_line, new_line, old_left, new_left = old_line + 1, new_line + 1, old_left - 1, new_left - 1
            elif marker == "-":
                if old_left <= 0:
                    raise GitError("malformed hunk: too many deleted lines")
                rows.append(_row("del", old_line, None, body[1:]))
                old_line, old_left = old_line + 1, old_left - 1
            elif marker == "+":
                if new_left <= 0:
                    raise GitError("malformed hunk: too many added lines")
                rows.append(_row("add", None, new_line, body[1:]))
                new_line, new_left = new_line + 1, new_left - 1
            else:
                raise GitError("malformed hunk: %r" % body[:80])
        while i < len(lines) and lines[i].startswith("\\"):
            if rows:
                rows[-1]["nonl"] = True
            i += 1
        hunks.append({
            "old_start": old_start, "old_count": old_count,
            "new_start": new_start, "new_count": new_count,
            "section": match.group(5) or "", "lines": rows,
        })
    return hunks


def _split_sections(text: str) -> list:
    """Split patch text into parsed sections, one per ``diff --git`` header."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    groups = []
    for line in lines:
        if line.startswith("diff --git "):
            groups.append([line])
        elif groups:
            groups[-1].append(line)
    sections = []
    for group in groups:
        sec = _new_section(group[0])
        i = _parse_section_header(sec, group)
        if not sec["binary"]:
            sec["hunks"] = _parse_hunks(group, i)
        sections.append(sec)
    return sections


def _section_paths(sec: dict):
    """``(old_path, new_path)`` of a section; None marks an absent side."""
    if sec["rename"] and all(sec["rename"]):
        old, new = sec["rename"]
    elif sec["copy"] and all(sec["copy"]):
        old, new = sec["copy"]
    elif sec["has_text_header"]:
        old, new = sec["minus"], sec["plus"]
    else:
        old, new = _diff_git_paths(sec["header"])
    if sec["new_file"]:
        old = None
    if sec["deleted"]:
        new = None
    return old, new


def _section_key(sec: dict):
    """The path a raw record would carry for this section (new side, or old side for deletions)."""
    old, new = _section_paths(sec)
    return new if new is not None else old


def _skeleton_from_section(sec: dict) -> dict:
    old, new = _section_paths(sec)
    if sec["rename"] and all(sec["rename"]):
        status, path, old_path = "R", new, old
    elif sec["copy"] and all(sec["copy"]):
        status, path, old_path = "C", new, old
    elif sec["new_file"]:
        status, path, old_path = "A", new, None
    elif sec["deleted"]:
        status, path, old_path = "D", old, None
    else:
        status, path, old_path = "M", new if new is not None else old, None
    if path is None:
        raise GitError("cannot determine the path of a diff section: %r" % sec["header"][:120])
    return {
        "path": path, "old_path": old_path, "status": status, "score": sec["score"] if status in "RC" else 0,
        "additions": 0, "deletions": 0, "binary": False,
        "old_mode": None if status == "A" else sec["old_mode"],
        "new_mode": None if status == "D" else sec["new_mode"],
        "old_blob": None if status == "A" else sec["old_blob"],
        "new_blob": None if status == "D" else sec["new_blob"],
    }


def _make_file_diff(skeleton: dict, hunks: list, binary: bool) -> dict:
    """Complete a FileStat skeleton into an untrimmed FileDiff."""
    rows = [row for hunk in hunks for row in hunk["lines"]]
    file_diff = dict(skeleton)
    file_diff["binary"] = binary
    file_diff["additions"] = 0 if binary else sum(1 for r in rows if r["t"] == "add")
    file_diff["deletions"] = 0 if binary else sum(1 for r in rows if r["t"] == "del")
    file_diff.update({
        "lang": guess_lang(skeleton["path"]),
        "old_rev": None,
        "new_rev": None,
        "too_large": False,
        "reason": None,
        "line_count": len(rows),
        "hunk_count": len(hunks),
        "ws_only": False,
        "hunks": hunks,
    })
    return file_diff


def _merge_type_changes(files: list) -> list:
    """Fold a deletion immediately followed by a creation of the same path into one ``T`` FileDiff."""
    merged = []
    i = 0
    while i < len(files):
        cur = files[i]
        nxt = files[i + 1] if i + 1 < len(files) else None
        if nxt is not None and cur["status"] == "D" and nxt["status"] == "A" and cur["path"] == nxt["path"]:
            skeleton = dict(cur, status="T", new_mode=nxt["new_mode"], new_blob=nxt["new_blob"])
            merged.append(_make_file_diff(skeleton, cur["hunks"] + nxt["hunks"], cur["binary"] or nxt["binary"]))
            i += 2
        else:
            merged.append(cur)
            i += 1
    return merged


def parse_patch(text: str) -> list:
    """Parse standalone unified-diff text (no raw block) into FileDiff dicts."""
    files = [_make_file_diff(_skeleton_from_section(sec), sec["hunks"], sec["binary"]) for sec in _split_sections(text)]
    return _merge_type_changes(files)


def _assign_sections(skeletons: list, sections: list) -> list:
    """Attach patch sections to raw skeletons in order (a ``T`` skeleton takes two sections)."""
    files = []
    consumed = set()
    cursor = 0
    for skeleton in skeletons:
        index = next((k for k in range(cursor, len(sections))
                      if k not in consumed and _section_key(sections[k]) == skeleton["path"]), None)
        if index is None:
            files.append(_make_file_diff(skeleton, [], False))
            continue
        taken = [sections[index]]
        consumed.add(index)
        cursor = index + 1
        if skeleton["status"] == "T" and cursor < len(sections) and cursor not in consumed \
                and _section_key(sections[cursor]) == skeleton["path"]:
            taken.append(sections[cursor])
            consumed.add(cursor)
            cursor += 1
        hunks = [hunk for sec in taken for hunk in sec["hunks"]]
        files.append(_make_file_diff(skeleton, hunks, any(sec["binary"] for sec in taken)))
    return files


def parse_raw_and_patch(data: bytes) -> list:
    """Parse the output of ``git diff --raw -z -p`` (raw block, extra NUL, patch text).

    No files → empty output; every file removed by ``-w`` → a lone separator NUL.
    """
    if not data:
        return []
    if data.startswith(b"\0"):
        raw, patch = b"", data[1:]
    else:
        boundary = data.find(b"\0\0")
        raw, patch = (data, b"") if boundary < 0 else (data[:boundary + 1], data[boundary + 2:])
    skeletons = _parse_raw_block(raw)
    sections = _split_sections(_decode(patch)) if patch else []
    return _assign_sections(skeletons, sections)


# --------------------------------------------------------------------------- diffs

def _diff_args(old, new, output, ws_ignore=False, pathspec=()):
    """Argv of ``git diff`` between two revs (``new`` None → working tree) with the section 3.1 flags."""
    revs = [old] if new is None else [old, new]
    context = _PATCH_CONTEXT if "-p" in output else []
    return ["diff"] + list(output) + (["-w"] if ws_ignore else []) + _DIFF_FLAGS + context + ["--end-of-options"] \
        + revs + ["--"] + list(pathspec)


def _run_diff(repo, args, retry_lock) -> bytes:
    """Run one diff command; with ``retry_lock`` a transient ``index.lock`` failure is retried once."""
    try:
        return _git(repo, args).stdout
    except GitError as exc:
        if not retry_lock or "index.lock" not in str(exc):
            raise
    time.sleep(INDEX_LOCK_RETRY_DELAY)
    return _git(repo, args).stdout


def _mark_ws_only(ws_files: list, plain_stats: list) -> list:
    """Flag files whose every hunk ``-w`` removed, using the FileStats of the whitespace-sensitive diff.

    A file absent from the ``-w`` output is re-inserted (in path order) as a
    hunk-less ``ws_only`` FileDiff; one that is still present (e.g. because its
    mode changed too) but lost all hunks is flagged in place.
    """
    by_path = {f["path"]: f for f in ws_files}
    result = list(ws_files)
    for skeleton in plain_stats:
        present = by_path.get(skeleton["path"])
        text_changes = not skeleton["binary"] and skeleton["additions"] + skeleton["deletions"] > 0
        if present is None:
            file_diff = _make_file_diff(skeleton, [], skeleton["binary"])
            file_diff["ws_only"] = True
            position = next((k for k, f in enumerate(result) if f["path"] > skeleton["path"]), len(result))
            result.insert(position, file_diff)
        elif text_changes and not present["hunks"]:
            present["ws_only"] = True
    return result


def _diff_files(repo, old, new, ws_ignore, pathspec=(), retry_lock=False) -> list:
    """Parsed FileDiffs of ``git diff old [new]``; with ``ws_ignore`` whitespace-only files are flagged."""
    data = _run_diff(repo, _diff_args(old, new, ["--raw", "-z", "-p"], ws_ignore, pathspec), retry_lock)
    files = parse_raw_and_patch(data)
    if ws_ignore:
        plain = _run_diff(repo, _diff_args(old, new, ["--raw", "--numstat", "-z"], False, pathspec), retry_lock)
        blocks = _parse_raw_numstat_blocks(plain)
        files = _mark_ws_only(files, blocks[0][1] if blocks else [])
    return files


def _set_revs(files: list, old_rev, new_rev) -> list:
    for file_diff in files:
        file_diff["old_rev"], file_diff["new_rev"] = old_rev, new_rev
    return files


def _commit_diff(meta: dict, files: list) -> dict:
    diff = dict(meta)
    diff["stats"] = stats_summary(files)
    diff["files"] = files
    return diff


def diff_commit(repo, sha, parent, ws_ignore=False) -> dict:
    """CommitDiff of ``sha`` against ``parent`` (None -> empty tree, i.e. a root commit)."""
    _check_rev_arg(sha)
    if parent is not None:
        _check_rev_arg(parent)
    meta = _commit_meta(repo, sha)
    files = _diff_files(repo, parent if parent is not None else empty_tree(repo), sha, ws_ignore)
    return _commit_diff(meta, _set_revs(files, parent, sha))


def diff_range(repo, base, head, ws_ignore=False) -> dict:
    """CommitDiff of everything between ``base`` (None -> empty tree) and ``head`` (the ``combined`` view)."""
    _check_rev_arg(head)
    if base is not None:
        _check_rev_arg(base)
    files = _diff_files(repo, base if base is not None else empty_tree(repo), head, ws_ignore)
    return _commit_diff(pseudo_meta("combined", "combined", "All changes"), _set_revs(files, base, head))


def _untracked_paths(repo) -> list:
    tokens = _git(repo, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout.split(b"\0")
    return [_decode(t) for t in tokens if t and not t.endswith(b"/")]


def _split_text_lines(text: str):
    """Split file text into lines; returns ``(lines, ends_with_newline)``."""
    lines = text.split("\n")
    ends_with_newline = bool(text) and text.endswith("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, ends_with_newline


def _untracked_file_diff(repo, path, head_sha):
    """Build the FileDiff of an untracked path in Python; None for non-regular, non-symlink entries."""
    full = os.path.join(repo, path)
    try:
        st = os.lstat(full)
    except OSError:
        return None
    skeleton = {
        "path": path, "old_path": None, "status": "A", "score": 0, "additions": 0, "deletions": 0,
        "binary": False, "old_mode": None, "new_mode": None, "old_blob": None, "new_blob": None,
    }
    if stat.S_ISLNK(st.st_mode):
        skeleton["new_mode"] = "120000"
        data, total_lines = os.readlink(full).encode("utf-8", "surrogateescape"), None
    elif stat.S_ISREG(st.st_mode):
        skeleton["new_mode"] = "100755" if st.st_mode & 0o111 else "100644"
        try:
            data, total_lines = _read_capped(full)
        except OSError:
            return None
    else:
        return None
    binary = b"\0" in data[:BINARY_SNIFF_BYTES]
    lines, ends_with_newline = _split_text_lines(_decode(data))
    hunks = []
    if not binary and total_lines is None and lines:
        rows = [_row("add", None, k + 1, text) for k, text in enumerate(lines)]
        if not ends_with_newline:
            rows[-1]["nonl"] = True
        hunks = [{"old_start": 0, "old_count": 0, "new_start": 1, "new_count": len(rows),
                  "section": "", "lines": rows}]
    file_diff = _make_file_diff(skeleton, hunks, binary)
    if total_lines is not None and not binary:
        file_diff.update(too_large=True, reason="file", additions=total_lines, line_count=total_lines)
    file_diff["old_rev"], file_diff["new_rev"] = head_sha, "worktree"
    return file_diff


def _read_capped(full: str):
    """Read up to 1 MiB (+1 byte) of a file: ``(data, None)``, or ``(data, total_line_count)`` when larger.

    The remainder of an oversized file is only scanned for newlines, chunk by
    chunk, so the reported ``additions`` are exact without holding it in memory.
    """
    with open(full, "rb") as fh:
        data = fh.read(UNTRACKED_MAX_BYTES + 1)
        if len(data) <= UNTRACKED_MAX_BYTES:
            return data, None
        newlines, last = data.count(b"\n"), data[-1:]
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            newlines, last = newlines + chunk.count(b"\n"), chunk[-1:]
    return data, newlines + (0 if last == b"\n" else 1)


def diff_worktree(repo, ws_ignore=False) -> dict:
    """CommitDiff of uncommitted changes vs HEAD: tracked (staged + unstaged) plus untracked files.

    Tracked changes come from ``git diff HEAD`` (retried once on a transient
    ``index.lock``); untracked regular files and symlinks are read directly.
    The new side of every file is the working tree, so ``new_blob`` is null.
    """
    if toplevel(repo)["bare"]:
        raise GitError("a bare repository has no working tree")
    head = rev_parse(repo, "HEAD")
    tracked = _diff_files(repo, head, None, ws_ignore, retry_lock=True)
    for file_diff in tracked:
        file_diff["new_blob"] = None
    _set_revs(tracked, head, "worktree")
    untracked = [fd for fd in (_untracked_file_diff(repo, p, head) for p in _untracked_paths(repo)) if fd]
    files = sorted(tracked + untracked, key=lambda f: f["path"])
    return _commit_diff(pseudo_meta("worktree", "worktree", "Uncommitted changes"), files)


# --------------------------------------------------------------------------- file contents

def _text_lines(data: bytes):
    """Decode file bytes into display lines: CR stripped, long lines truncated (1-based indices returned)."""
    lines, _ = _split_text_lines(_decode(data))
    truncated = []
    out = []
    for number, line in enumerate(lines, 1):
        if line.endswith("\r"):
            line = line[:-1]
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS]
            truncated.append(number)
        out.append(line)
    return out, truncated


def _file_result(data: bytes) -> dict:
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        raise GitError("binary file", status=415)
    if len(data) > SHOW_FILE_MAX_BYTES:
        raise GitError("file too large", status=413)
    lines, truncated = _text_lines(data)
    return {"content": "\n".join(lines), "lines": len(lines), "truncated_lines": truncated}


def _worktree_file_bytes(repo, path) -> bytes:
    full = os.path.join(repo, path)
    root = os.path.realpath(repo)
    parent = os.path.realpath(os.path.dirname(full))
    if os.path.commonpath([root, parent]) != root:
        raise GitError("path escapes the repository", status=403)
    try:
        st = os.lstat(full)
    except OSError:
        raise GitError("no such file in the working tree", status=404) from None
    if stat.S_ISLNK(st.st_mode):
        return os.readlink(full).encode("utf-8", "surrogateescape")
    if not stat.S_ISREG(st.st_mode):
        raise GitError("not a regular file", status=404)
    with open(full, "rb") as fh:
        return fh.read(SHOW_FILE_MAX_BYTES + 1)


def _blob_bytes(repo, sha, path) -> bytes:
    spec = "%s:%s" % (sha, path)
    proc = _git(repo, ["cat-file", "-t", "--end-of-options", spec], ok_codes=(0, 128))
    if proc.returncode != 0:
        raise GitError("no such file at that revision", status=404)
    if _decode(proc.stdout).strip() != "blob":
        raise GitError("not a regular file", status=404)
    return _git(repo, ["cat-file", "blob", "--end-of-options", spec]).stdout


def show_file(repo, rev, path) -> dict:
    """Text of ``path`` at ``rev`` (a full sha, or ``"worktree"``): ``{"content", "lines", "truncated_lines"}``."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise GitError("invalid path")
    if rev == "worktree":
        return _file_result(_worktree_file_bytes(repo, path))
    if not _is_sha(rev):
        raise GitError("invalid revision %r" % (rev,))
    return _file_result(_blob_bytes(repo, rev, path))


# --------------------------------------------------------------------------- line mapping

def _name_status_entry(repo, from_rev, to_rev, path):
    """``(status, new_path)`` of ``path`` (as old path) in ``from..to``, or None when unchanged."""
    # Renames are followed, copies are not (-C would relocate a line into a copy sorted before the still-existing
    # original); everything else matches the flags of the diff itself.
    revs = [from_rev] if to_rev is None else [from_rev, to_rev]
    args = ["diff", "--name-status", "-z"] + [f for f in _DIFF_FLAGS if f != "-C"] + ["--end-of-options"] + revs + ["--"]
    tokens = _git(repo, args).stdout.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    i = 0
    while i < len(tokens):
        status = _decode(tokens[i])[:1]
        if status in "RC":
            old, new = _decode(tokens[i + 1]), _decode(tokens[i + 2])
            i += 3
        else:
            old = new = _decode(tokens[i + 1])
            i += 2
        if old == path:
            return status, new
    return None


def _changed_file(repo, from_rev, to_rev, path):
    """FileDiff of ``path`` between two revisions (following renames), "deleted", or None when unchanged."""
    entry = _name_status_entry(repo, from_rev, to_rev, path)
    if entry is None:
        return None
    status, new_path = entry
    if status == "D":
        return "deleted"
    pathspec = [path] if new_path == path else [path, new_path]
    for file_diff in _diff_files(repo, from_rev, to_rev, False, pathspec=pathspec):
        if file_diff["path"] == new_path:
            return file_diff
    return None


def _cached_changed_file(repo, from_rev, to_rev, path):
    key = (repo, from_rev, to_rev, path)
    with _cache_lock:
        if key in _map_line_cache:
            return _map_line_cache[key]
    result = _changed_file(repo, from_rev, to_rev, path)
    with _cache_lock:
        if len(_map_line_cache) >= _MAP_LINE_CACHE_MAX:
            _map_line_cache.clear()
        _map_line_cache[key] = result
    return result


def _map_through_hunks(hunks: list, line: int):
    """Apply the section 3.3 walk; returns ``(new_line, status)``."""
    offset = 0
    for hunk in hunks:
        old_start, old_count = hunk["old_start"], hunk["old_count"]
        above = old_start < line if old_count == 0 else old_start + old_count - 1 < line
        if above:
            offset += hunk["new_count"] - old_count
            continue
        if old_count and old_start <= line:
            row = next(r for r in hunk["lines"] if r["o"] == line)
            if row["t"] == "ctx":
                return row["n"], "same"
            first_add = next((r for r in hunk["lines"] if r["t"] == "add"), None)
            if first_add is None:
                return hunk["new_start"], "deleted"
            return first_add["n"], "changed"
        break
    return line + offset, "same" if offset == 0 else "moved"


def map_line(repo, from_rev, to_rev, path, line) -> dict:
    """Locate ``path:line`` of ``from_rev`` in ``to_rev``: ``{"path", "line", "status"}`` (section 3.3)."""
    if from_rev == to_rev:
        return {"path": path, "line": line, "status": "same"}
    _check_rev_arg(from_rev)
    if to_rev is not None:  # None = the working tree
        _check_rev_arg(to_rev)
    lookup = _cached_changed_file if _is_sha(from_rev) and to_rev is not None and _is_sha(to_rev) else _changed_file
    changed = lookup(repo, from_rev, to_rev, path)
    if changed is None:
        return {"path": path, "line": line, "status": "same"}
    if changed == "deleted":
        return {"path": None, "line": None, "status": "file-deleted"}
    new_line, status = _map_through_hunks(changed["hunks"], line)
    return {"path": changed["path"], "line": new_line, "status": status}


# --------------------------------------------------------------------------- trimming

def _has_long_line(file_diff: dict) -> bool:
    return any(row.get("trunc") for hunk in file_diff["hunks"] for row in hunk["lines"])


def _trimmed(file_diff: dict, reason: str) -> dict:
    return dict(file_diff, hunks=[], too_large=True, reason=reason)


def trim_file_diff(file_diff: dict, full: bool = False) -> dict:
    """Apply the per-file ``too_large`` rule (section 2.3) without mutating the input."""
    if full or file_diff["too_large"]:
        return file_diff
    if file_diff["line_count"] > FILE_LINE_CAP or _has_long_line(file_diff):
        return _trimmed(file_diff, "file")
    return file_diff


def trim_commit_diff(commit_diff: dict, full: bool = False) -> dict:
    """Apply the per-file and per-response caps to a CommitDiff (returns a new dict)."""
    files = [trim_file_diff(f, full) for f in commit_diff["files"]]
    if not full:
        served = sum(f["line_count"] for f in files if not f["too_large"])
        order = sorted((k for k, f in enumerate(files) if not f["too_large"]),
                       key=lambda k: files[k]["line_count"], reverse=True)
        for index in order:
            if served <= RESPONSE_LINE_CAP:
                break
            served -= files[index]["line_count"]
            files[index] = _trimmed(files[index], "response")
    return dict(commit_diff, files=files)


# --------------------------------------------------------------------------- language guessing

_LANG_BY_NAME = {
    "makefile": "makefile", "cmakelists.txt": "cmake", "dockerfile": "dockerfile",
}
_LANG_BY_EXT = {
    "py": "python", "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hh": "cpp", "hpp": "cpp",
    "hxx": "cpp", "rs": "rust", "go": "go", "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "javascript", "ts": "typescript", "tsx": "typescript", "java": "java", "kt": "kotlin", "kts": "kotlin",
    "scala": "scala", "rb": "ruby", "php": "php", "cs": "csharp", "swift": "swift", "sh": "bash", "bash": "bash",
    "zsh": "bash", "fish": "bash", "sql": "sql", "html": "xml", "htm": "xml", "xhtml": "xml", "xml": "xml",
    "xsd": "xml", "xsl": "xml", "svg": "xml", "css": "css", "scss": "scss", "less": "less", "json": "json",
    "jsonc": "json", "yaml": "yaml", "yml": "yaml", "toml": "ini", "ini": "ini", "cfg": "ini", "conf": "ini",
    "md": "markdown", "markdown": "markdown", "mk": "makefile", "cmake": "cmake", "dockerfile": "dockerfile",
    "proto": "protobuf", "lua": "lua", "pl": "perl", "pm": "perl", "r": "r", "m": "objectivec", "mm": "objectivec",
    "erl": "erlang", "hrl": "erlang", "hs": "haskell", "ml": "ocaml", "mli": "ocaml", "nix": "nix",
    "groovy": "groovy", "gradle": "groovy", "txt": "plaintext", "diff": "diff", "patch": "diff", "vb": "vbnet",
    "wasm": "wasm", "wat": "wasm", "graphql": "graphql", "gql": "graphql",
}
LANG_IDS = frozenset(_LANG_BY_EXT.values()) | frozenset(_LANG_BY_NAME.values())


def guess_lang(path):
    """highlight.js language id for a path, or None when unknown."""
    if not path:
        return None
    name = path.rsplit("/", 1)[-1]
    lowered = name.lower()
    if lowered in _LANG_BY_NAME:
        return _LANG_BY_NAME[lowered]
    _, dot, ext = name.rpartition(".")
    if not dot or not ext:
        return None
    return _LANG_BY_EXT.get(ext.lower())

"""Command-line interface of ccr (SPEC.md section 6).

``ccr <command> [options]`` talks to the background server through :class:`~ccr.session.Client`
(token header, ``ccr-cli/<version>`` agent, 5 s timeout — 35 s for ``wait`` polls) after discovering
the session for the repository (``--repo``, default: the git toplevel of the current directory) or
using ``--url``/``--token`` (``CCR_URL``/``CCR_TOKEN``) directly.  Exit codes: 0 ok, 1 error,
2 ``wait`` timeout, 3 no running session.  Every failure is printed as ``ccr: <message>`` — never a
traceback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser
from urllib.error import URLError
from urllib.parse import quote

from . import __version__, github, gitx, render, restore, server, session
from .github import GitHubError
from .gitx import GitError
from .session import ApiError, Client, NoSessionError, SessionError
from .store import StoreError

__all__ = ["main", "build_parser"]

EXIT_OK, EXIT_ERROR, EXIT_TIMEOUT, EXIT_NO_SESSION = 0, 1, 2, 3
DEFAULT_WAIT_TIMEOUT = 590.0
WAIT_POLL_SECONDS = 25.0
WAIT_REQUEST_TIMEOUT = 35.0
WAIT_RETRY_SECONDS = 1.0
WAIT_GONE_SECONDS = 30.0
UI_NOTICE_SECONDS = 30.0
DEFAULT_LOG_LINES = 50
AUTHORS = ("user", "claude")
COMMENT_AUTHORS = AUTHORS + ("github",)  # "github": mirrored from the pull request's discussion (PR mode)
DEFAULT_CLI_AUTHOR = "claude"
_FULL_SHA_RE = re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b")
_BATCH_HEADING_RE = re.compile(r"^##\s+(\S+?)(?:\s+\[resolve\])?\s*$")
SINCE_AT_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class CliError(Exception):
    """A usage or input problem reported as ``ccr: <message>`` with exit code 1."""


class Parser(argparse.ArgumentParser):
    """argparse with ccr's error convention: ``ccr: <message>`` on stderr and exit code 1."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("ccr: %s\n" % message)
        raise SystemExit(EXIT_ERROR)


# --------------------------------------------------------------------------- output helpers

def out(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def err(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def json_text(payload) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def print_json(payload) -> None:
    sys.stdout.write(json_text(payload))
    sys.stdout.flush()


def plural(count: int, noun: str) -> str:
    return "%d %s%s" % (count, noun, "" if count == 1 else "s")


def short_spec(spec) -> str:
    """A range spec with full shas abbreviated to 10 characters (for human-facing lines)."""
    return _FULL_SHA_RE.sub(lambda m: m.group(0)[:gitx.SHORT_SHA_LEN], render.clean(spec or "", True))


def range_label(rng: dict) -> str:
    return short_spec(rng.get("given") or rng.get("spec"))


def real_commits(review: dict) -> int:
    return sum(1 for c in review.get("commits") or [] if c.get("kind") == "commit")


def write_private_text(path: str, text: str) -> None:
    """Create/replace ``path`` with mode 0600 and write ``text``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(fd, 0o600)
        handle.write(text)


# --------------------------------------------------------------------------- argument parsing

def _add_global_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--repo", metavar="PATH", default=default, help="repository (default: git toplevel of the cwd)")
    parser.add_argument("--url", default=default, help="server url (bypasses session discovery; env CCR_URL)")
    parser.add_argument("--token", default=default, help="session token for --url (env CCR_TOKEN)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if suppress else False,
                        help="machine-readable output")


def _add_range_options(parser: argparse.ArgumentParser, reload: bool = False) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--range", metavar="SPEC", help="commit range (A..B, A...B, A)")
    group.add_argument("-n", type=int, metavar="N", help="N first-parent steps back from HEAD")
    tree = parser.add_mutually_exclusive_group()
    tree.add_argument("--worktree", dest="worktree", action="store_const", const=True, default=None,
                      help="include uncommitted changes as a pseudo-commit")
    tree.add_argument("--no-worktree", dest="worktree", action="store_const", const=False)
    first = parser.add_mutually_exclusive_group()
    first.add_argument("--first-parent", dest="first_parent", action="store_const", const=True, default=None,
                       help="follow only first parents through merges")
    if reload:
        first.add_argument("--no-first-parent", dest="first_parent", action="store_const", const=False)


def _add_server_options(parser: argparse.ArgumentParser) -> None:
    _add_range_options(parser)
    parser.add_argument("--port", type=int, metavar="N")
    parser.add_argument("--db", metavar="PATH", help="sqlite file (':memory:' for none)")
    parser.add_argument("--log", metavar="FILE")
    parser.add_argument("--open", action="store_true", help="open the UI in a browser")
    parser.add_argument("--cover", metavar="FILE", help="Markdown cover letter describing the whole change")
    parser.add_argument("--pr", metavar="URL", help="link the review to a GitHub pull request (PR mode: questions "
                        "for Claude and GitHub comments for your pending review); URL or OWNER/REPO#N")
    parser.add_argument("--since", metavar="REV", help="open the \"Since your last review\" view: what the head "
                        "changed since REV, the commit an earlier review was submitted on, was reviewed")
    parser.add_argument("--since-at", metavar="TIME", dest="since_at",
                        help="when that review was submitted (UTC, like 2026-10-07T12:34:56Z); needs --since")
    parser.add_argument("--idle-timeout", type=float, default=server.DEFAULT_IDLE_TIMEOUT, metavar="S",
                        help="stop after S seconds without requests (0 = never; default 86400)")


def _add_body_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("body", nargs="?", help="comment text ('-' reads stdin)")
    parser.add_argument("--file", metavar="F", help="read the text from a file ('-' = stdin)")


def _add_anchor_options(parser: argparse.ArgumentParser, review: bool) -> None:
    if review:
        parser.add_argument("--review", action="store_true", help="comment on the whole review")
    parser.add_argument("--commit", metavar="REV", required=not review)
    parser.add_argument("--path", metavar="P")
    parser.add_argument("--line", type=int, metavar="N")
    parser.add_argument("--side", choices=("new", "old"), default="new")
    parser.add_argument("--start-line", type=int, metavar="M", dest="start_line")


def build_parser() -> Parser:
    parser = Parser(prog="ccr", description="Commit Chain Reviewer — a local code review UI for agentic workflows.")
    parser.add_argument("--version", action="version", version="ccr " + __version__)
    _add_global_options(parser, suppress=False)
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str, **kwargs) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text, **kwargs)
        _add_global_options(sub, suppress=True)
        return sub

    start = add("start", "start a background server (or reload the running one) and print the URL")
    _add_server_options(start)

    serve = add("serve", "run the server in the foreground", conflict_handler="resolve")
    _add_server_options(serve)
    serve.add_argument("--token", default=argparse.SUPPRESS, metavar="T", help="server token (visible in ps; tests only)")
    serve.add_argument("--verbose", action="store_true", help="log every request")
    serve.add_argument("--db-force", action="store_true", dest="db_force", help="reuse a db created for another repo")

    stop = add("stop", "export the review, shut the server down and remove the session files")
    stop.add_argument("--all", action="store_true", help="stop every live session")
    stop.add_argument("--keep-db", action="store_true", dest="keep_db",
                      help="keep the review database (the default: it goes a week after its last change)")
    stop.add_argument("--purge", action="store_true", help="remove the review database, exports and logs now")

    add("status", "show the running session")
    add("sessions", "list every live session")
    logs = add("logs", "tail the server log")
    logs.add_argument("-n", type=int, default=DEFAULT_LOG_LINES, metavar="N")
    logs.add_argument("-f", action="store_true", dest="follow", help="follow the log")
    add("open", "open the UI in a browser")

    reload = add("reload", "re-extract the commit chain")
    _add_range_options(reload, reload=True)

    comments = add("comments", "print review threads as Markdown")
    which = comments.add_mutually_exclusive_group()
    which.add_argument("--pending", action="store_true")
    which.add_argument("--submitted", action="store_true")
    which.add_argument("--round", type=int, metavar="N")
    which.add_argument("--all", action="store_true", help="every thread (default)")
    comments.add_argument("--unresolved", action="store_true")
    comments.add_argument("--unanswered", action="store_true",
                          help="unresolved threads whose last comment is by user (and not posted to GitHub)")
    comments.add_argument("--author", choices=COMMENT_AUTHORS)
    comments.add_argument("--commit", metavar="SHA")
    comments.add_argument("--path", metavar="P")
    outdated = comments.add_mutually_exclusive_group()
    outdated.add_argument("--outdated", action="store_true", help="only outdated threads")
    outdated.add_argument("--no-outdated", action="store_true", dest="no_outdated", help="hide outdated threads")
    comments.add_argument("--context", type=int, default=render.DEFAULT_CONTEXT, metavar="N")
    comments.add_argument("--no-snippets", action="store_true", dest="no_snippets")

    wait = add("wait", "block until the next review round (or any change with --any)")
    wait.add_argument("--since-round", type=int, dest="since_round", metavar="N")
    wait.add_argument("--since-version", type=int, dest="since_version", metavar="V")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT, metavar="S", help="0 = forever")
    wait.add_argument("--any", action="store_true", dest="any_change", help="return on any change")

    reply = add("reply", "reply to a thread (or many with --batch)")
    reply.add_argument("id", nargs="?", help="thread (root comment) id")
    _add_body_options(reply)
    reply.add_argument("--resolve", action="store_true", help="resolve the thread after replying")
    reply.add_argument("--as", dest="author", choices=AUTHORS, default=DEFAULT_CLI_AUTHOR)
    reply.add_argument("--force", action="store_true", help="post even when an identical reply exists")
    reply.add_argument("--batch", metavar="FILE", help="JSON list or Markdown '## <id> [resolve]' sections ('-' = stdin)")

    comment = add("comment", "create a comment on the review, a commit, a file or a line")
    _add_anchor_options(comment, review=True)
    _add_body_options(comment)
    comment.add_argument("--as", dest="author", choices=AUTHORS, default=DEFAULT_CLI_AUTHOR)
    comment.add_argument("--github", action="store_true", help="a GitHub comment of PR mode (needs --as user)")

    add("resolve", "resolve threads").add_argument("ids", nargs="+", metavar="ID")
    add("unresolve", "unresolve threads").add_argument("ids", nargs="+", metavar="ID")
    edit = add("edit", "replace a comment body")
    edit.add_argument("id")
    _add_body_options(edit)
    delete = add("delete", "delete comments")
    delete.add_argument("ids", nargs="+", metavar="ID")
    delete.add_argument("--cascade", action="store_true", help="delete a root together with its replies")
    move = add("move", "re-anchor a comment")
    move.add_argument("id")
    _add_anchor_options(move, review=False)

    cover = add("cover", "set the cover letter (Markdown description of the whole change) of the running review")
    _add_body_options(cover)

    add("gh-sync", "mirror the linked pull request's review threads and review bodies from GitHub into the review")

    gh_post = add("gh-post", "re-read the linked pull request's discussion, then post GitHub comments and replies "
                  "verbatim into your pending review on it (starting it when there is none; it is never submitted); "
                  "nothing is posted while the re-read brings comments changed since the last sync")
    gh_post.add_argument("ids", nargs="+", metavar="ID")
    gh_post.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="re-read the discussion, show where each comment would go and what it says; post nothing")

    restore_cmd = add("restore", "fill the running review, which has no comments or rounds of its own yet, from an "
                      "export: the .json or .md `ccr stop` writes, or `ccr export`; in PR mode it syncs with GitHub "
                      "around the restore")
    restore_cmd.add_argument("file", metavar="FILE", help="the export ('-' = stdin)")
    restore_cmd.add_argument("--dry-run", action="store_true", dest="dry_run",
                             help="check the export and show what it would restore; change nothing")

    export = add("export", "dump rounds and every thread as Markdown (default) or JSON")
    export.add_argument("--md", action="store_true", help="Markdown (default)")
    export.add_argument("-o", metavar="FILE", dest="output", help="write to FILE (mode 0600)")
    return parser


# --------------------------------------------------------------------------- connection

def resolve_repo(args) -> str:
    """Toplevel of ``--repo`` or the cwd, with the section 6 message when the cwd is not a repository."""
    given = args.repo
    try:
        return gitx.toplevel(given or os.getcwd())["path"]
    except GitError:
        if given:
            raise
        raise CliError("%s is not inside a git repository; pass --repo PATH" % os.getcwd()) from None


def connect(args):
    """``(client, record|None, state|None)`` — direct with ``--url``, otherwise via session discovery."""
    url = args.url or os.environ.get("CCR_URL")
    if url:
        return Client(url, args.token or os.environ.get("CCR_TOKEN") or None), None, None
    record, state = session.find_session(resolve_repo(args))
    return Client(record["url"], record["token"]), record, state


def fetch_file_diff(client: Client):
    """``render`` snippet callback: the untrimmed FileDiff of ``(commit, path)`` or None."""
    def fetch(commit, path):
        try:
            return client.get("/api/commits/%s/file?path=%s" % (quote(commit, safe=""), quote(path, safe="")))
        except (ApiError, URLError):
            return None
    return fetch


def load_review(client: Client):
    """``(review, comments)`` — the two documents every rendering command needs."""
    return client.get("/api/review"), client.get("/api/comments?locate=1")["comments"]


def read_body(args) -> str:
    """The comment text from BODY, ``--file F`` or stdin (``-``)."""
    if args.file is not None and args.body is not None:
        raise CliError("give the text either as BODY or with --file, not both")
    if args.file is not None:
        text = sys.stdin.read() if args.file == "-" else open(args.file, "r", encoding="utf-8").read()
    elif args.body == "-":
        text = sys.stdin.read()
    elif args.body is not None:
        text = args.body
    else:
        raise CliError("a body is required (BODY, --file F or - for stdin)")
    if not text.strip():
        raise CliError("body must not be empty")
    return text


def anchor_from_args(args) -> dict:
    """Build the Anchor for ``comment``/``move`` from ``--review``/``--commit``/``--path``/``--line`` …"""
    if getattr(args, "review", False):
        if args.commit or args.path or args.line is not None:
            raise CliError("--review cannot be combined with --commit/--path/--line")
        return {"kind": "review"}
    if not args.commit:
        raise CliError("--review or --commit REV is required")
    if args.start_line is not None and args.line is None:
        raise CliError("--start-line requires --line")
    if args.line is not None and not args.path:
        raise CliError("--line requires --path")
    if args.line is not None:
        return {"kind": "line", "commit": args.commit, "path": args.path, "side": args.side, "line": args.line,
                "start_line": args.start_line}
    if args.path:
        return {"kind": "file", "commit": args.commit, "path": args.path}
    return {"kind": "commit", "commit": args.commit}


def describe_anchor(anchor: dict) -> str:
    kind = anchor.get("kind")
    if kind == "review":
        return "review"
    commit = render.clean(anchor.get("commit") or "", True)[:gitx.SHORT_SHA_LEN]
    if kind == "commit":
        return "%s commit" % commit
    path = render.clean(anchor.get("path") or "", True)
    if kind == "file":
        return "%s %s file" % (commit, path)
    span = str(anchor.get("line"))
    if anchor.get("start_line"):
        span = "%d-%d" % (anchor["start_line"], anchor["line"])
    return "%s %s %s:%s" % (commit, path, anchor.get("side"), span)


# --------------------------------------------------------------------------- start / serve / stop

def reload_body(args) -> dict:
    body = {"range": args.range, "n": args.n, "worktree": args.worktree, "first_parent": args.first_parent}
    return {key: value for key, value in body.items() if value is not None}


def print_reload(result: dict) -> None:
    """The ``ccr reload`` lines (section 6.2)."""
    review = result["review"]
    added, removed = result["commits_added"], result["commits_removed"]
    count = real_commits(review)
    out("ccr: %d commits (was %d), +%d −%d, %d comments remapped, %d now outdated" % (
        count, count - added + removed, added, removed, len(result["remapped"]), len(result["outdated"])))
    if removed:
        out("warning: %d reviewed commits left the range" % removed)
    for comment in result["outdated"]:
        if comment.get("parent_id") is not None:
            continue
        snippet = (comment.get("snippet") or "").split("\n")[0]
        line = "  %s · %s" % (comment["id"], describe_anchor(comment["anchor"]))
        if snippet:
            line += ' · "%s"' % render.clean(snippet, True)
        out(line)


def print_serving(record: dict, review: dict) -> None:
    rng = review["range"]
    suffix = ", +worktree" if review["options"]["worktree"] else ""
    out("ccr: serving %s  (%s, %d commits%s)" % (record["repo"], range_label(rng), real_commits(review), suffix))
    for line in (render.review_line(review), render.pr_line(review), render.since_line(review)):
        if line:
            out(line)
    out("ccr: url %s" % record["url"])
    if rng.get("note"):
        out("ccr: note: %s" % rng["note"])


def start_json(record: dict, review: dict, reused: bool) -> dict:
    return dict(record, counts=review["counts"], commits=real_commits(review), rounds=len(review["rounds"]),
                reused=reused, review=review.get("review"))


def check_pr(reference) -> None:
    """Fail fast (exit 1, nothing started) on a ``--pr`` value that names no pull request."""
    if reference is not None:
        try:
            github.parse_pr(reference)
        except ValueError as exc:
            raise CliError(str(exc)) from None


def check_since(repo: str, args) -> None:
    """Fail fast (exit 1, nothing started) on ``--since`` / ``--since-at`` values the review cannot take."""
    if args.since_at is not None:
        if args.since is None:
            raise CliError("--since-at needs --since")
        if not SINCE_AT_RE.match(args.since_at):
            raise CliError("--since-at must be a UTC time like 2026-10-07T12:34:56Z")
    if args.since is not None:
        try:
            gitx.rev_parse(repo, args.since)
        except GitError as exc:
            if exc.status == 404:
                raise CliError("commit %s is not in this repository; fetch it first" % args.since) from None
            raise


def cmd_start(args) -> int:
    repo = resolve_repo(args)
    check_pr(args.pr)
    check_since(repo, args)
    paths = session.paths_for(repo)
    with session.start_lock(paths):
        found = session.find_live(repo)
        if found is not None:
            record, _ = found
            client = Client(record["url"], record["token"])
            if args.cover is not None:
                with open(args.cover, "r", encoding="utf-8") as handle:
                    client.post("/api/cover", {"text": handle.read()})
            if args.pr is not None:
                client.post("/api/pr", {"url": args.pr})
            if args.since is not None:
                client.post("/api/since", {"reviewed": args.since, "at": args.since_at})
            result = client.post("/api/reload", reload_body(args))
            if args.json:
                print_json(start_json(record, result["review"], True))
            else:
                out("ccr: reusing running session (pid %d)" % record["pid"])
                print_reload(result)
                since = render.since_line(result["review"])
                if since:
                    out(since)
                out("ccr: url %s" % record["url"])
            if args.open:
                webbrowser.open(record["url"])
            return EXIT_OK
        gitx.resolve_range(repo, args.range, args.n)
        # never the database this start resumes, however old
        prune_stale_dbs(say=(lambda line: None) if args.json else None, keep=(paths.db,))
        record, _ = session.start_background(
            repo, paths, spec=args.range, n=args.n, worktree=bool(args.worktree), first_parent=bool(args.first_parent),
            port=args.port, db=args.db, log=args.log, idle_timeout=args.idle_timeout, cover=args.cover, pr=args.pr,
            since=args.since, since_at=args.since_at)
    review = Client(record["url"], record["token"]).get("/api/review")
    if args.json:
        print_json(start_json(record, review, False))
    else:
        print_serving(record, review)
    if args.open:
        webbrowser.open(record["url"])
    return EXIT_OK


def cmd_cover(args) -> int:
    text = read_body(args)
    client, _, _ = connect(args)
    result = client.post("/api/cover", {"text": text})
    out("ccr: cover letter set (%d characters)" % len(result["cover"]))
    return EXIT_OK


def cmd_serve(args) -> int:
    repo = resolve_repo(args)
    check_pr(args.pr)
    check_since(repo, args)
    return server.serve(repo, spec=args.range, n=args.n, worktree=bool(args.worktree),
                        first_parent=bool(args.first_parent), port=args.port, db=args.db, db_force=args.db_force,
                        token=args.token, log=args.log, verbose=args.verbose, idle_timeout=args.idle_timeout,
                        open_browser=args.open, cover=args.cover, pr=args.pr, since=args.since, since_at=args.since_at)


def stop_one(record: dict, state: dict, args) -> None:
    paths = session.paths_for(record["repo"])
    if (state.get("server") or {}).get("pid") != record["pid"]:
        session.remove_record(paths.record)
        raise NoSessionError("session record for %s is stale (server pid mismatch); removed" % record["repo"])
    client = Client(record["url"], record["token"])
    try:
        review, comments = load_review(client)
        text = render.render_export(review, comments, fetch_file_diff(client))
    except ApiError as exc:
        err("ccr: export skipped: %s" % exc)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = paths.export(stamp)
        session.write_private(path, text.encode("utf-8"))
        out("ccr: exported to %s" % path)
        # the lossless one, for `ccr restore`
        path = paths.export(stamp, "json")
        session.write_private(path, json_text(render.to_json(review, comments)).encode("utf-8"))
        out("ccr: exported to %s" % path)
    session.shutdown_server(record, client)
    session.remove_session_files(paths, record, purge=args.purge and not args.keep_db)
    out("ccr: stopped %s (pid %d)" % (record["repo"], record["pid"]))


def prune_stale_dbs(say=None, keep=()) -> None:
    """Remove the databases of sessions stopped more than a week ago (``session.STALE_DB_SECONDS``)."""
    for path in session.prune_stale_dbs(keep=keep):
        (say or out)("ccr: removed %s, the database of a session stopped and unchanged for 7 days" % path)


def cmd_stop(args) -> int:
    if args.all:
        targets = session.list_sessions()
        if not targets:
            raise NoSessionError("no running sessions")
    else:
        targets = [session.find_session(resolve_repo(args))]
    for record, state in targets:
        stop_one(record, state, args)
    prune_stale_dbs()
    return EXIT_OK


# --------------------------------------------------------------------------- status / sessions / logs / open

def cmd_status(args) -> int:
    client, record, _ = connect(args)
    review = client.get("/api/review")
    if args.json:
        print_json({"session": record, "review": render.to_json(review, [])["review"]})
        return EXIT_OK
    rng, counts, ui, info = review["range"], review["counts"], review["ui"], review["server"]
    url = record["url"] if record else client.base
    out("ccr: url %s" % url)
    branch = review["repo"].get("branch")
    out("ccr: repo %s (%s)" % (review["repo"]["path"], "branch %s" % branch if branch else "detached HEAD"))
    suffix = ", +worktree" if review["options"]["worktree"] else ""
    out("ccr: range %s (%d commits%s)%s" % (range_label(rng), real_commits(review), suffix,
                                            " — loading" if review["loading"] else ""))
    if rng.get("note"):
        out("ccr: note: %s" % rng["note"])
    if review.get("pr"):
        out("ccr: pr %s" % render.clean(review["pr"]["url"], True))
    if render.since_line(review):
        out(render.since_line(review))
    out("ccr: comments %d pending, %d submitted, %d unresolved, %d outdated (%d total)" % (
        counts["pending"], counts["submitted"], counts["unresolved"], counts["outdated"], counts["total"]))
    rounds = review["rounds"]
    if rounds:
        last = rounds[-1]
        verdict = last.get("verdict")
        detail = ("%s at %s" % (verdict, last["submitted_at"])) if verdict not in (None, "", "comment") else "at %s" % last["submitted_at"]
        out("ccr: rounds %d (last: %s)" % (len(rounds), detail))
    else:
        out("ccr: rounds 0")
    if ui.get("last_seen") is None:
        out("ccr: ui not opened yet")
    else:
        out("ccr: ui %s (last seen %s)" % ("connected" if ui.get("connected") else "disconnected", ui["last_seen"]))
    out("ccr: server pid %s, started %s, version %s" % (info.get("pid"), info.get("started_at"), info.get("version")))
    out("ccr: log %s" % ((record or {}).get("log") or "none"))
    out("ccr: db %s" % ((record or {}).get("db") or "unknown"))
    return EXIT_OK


def cmd_sessions(args) -> int:
    sessions = session.list_sessions()
    if not sessions:
        raise NoSessionError("no running sessions")
    if args.json:
        print_json([dict(record, alive=True, state=state) for record, state in sessions])
        return EXIT_OK
    for record, _ in sessions:
        out("%s → %s  (%s, alive, started %s)" % (record.get("repo"), record["url"], record.get("range"),
                                                  record.get("started_at")))
    return EXIT_OK


def cmd_logs(args) -> int:
    paths = session.paths_for(resolve_repo(args))
    record = session.read_record(paths.record)
    log_path = (record or {}).get("log") or paths.log
    if not os.path.exists(log_path):
        raise CliError("no log file for this session (%s)" % log_path)
    for line in session.tail_lines(log_path, args.n):
        out(line)
    if not args.follow:
        return EXIT_OK
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                elif record and not session.pid_alive(record.get("pid")):
                    return EXIT_OK
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return EXIT_OK


def cmd_open(args) -> int:
    client, record, _ = connect(args)
    url = record["url"] if record else client.base
    out("ccr: opening %s" % url)
    webbrowser.open(url)
    return EXIT_OK


def cmd_reload(args) -> int:
    client, _, _ = connect(args)
    result = client.post("/api/reload", reload_body(args))
    if args.json:
        print_json(result)
    else:
        print_reload(result)
    return EXIT_OK


# --------------------------------------------------------------------------- comments / wait

def cmd_comments(args) -> int:
    client, _, _ = connect(args)
    review, comments = load_review(client)
    state = "pending" if args.pending else "submitted" if args.submitted else None
    outdated = "only" if args.outdated else "exclude" if args.no_outdated else "include"
    threads = render.sort_threads(render.build_threads(comments), review)
    selected, matching = render.select_threads(
        threads, state=state, round=args.round, author=args.author, commit=args.commit, path=args.path,
        unresolved=args.unresolved, unanswered=args.unanswered, outdated=outdated)
    if not selected:
        out("ccr: no comments match")
        return EXIT_OK
    if args.json:
        print_json(render.to_json(review, comments, threads=selected))
        return EXIT_OK
    sys.stdout.write(render.render_comments(review, comments, fetch_file_diff(client), threads=selected,
                                            matching=matching, context=args.context, snippets=not args.no_snippets))
    sys.stdout.flush()
    return EXIT_OK


def _format_seconds(value: float) -> str:
    return "%d" % value if float(value).is_integer() else "%g" % value


def report_round(client: Client, number: int, as_json: bool) -> int:
    review, comments = load_review(client)
    round_info = next((r for r in review["rounds"] if r["number"] == number), None)
    if round_info is None:
        raise CliError("round %d is not available" % number)
    threads = render.sort_threads(render.build_threads(comments), review)
    selected, matching = render.select_threads(threads, round=number)
    if as_json:
        print_json(dict(render.to_json(review, comments, threads=selected), round=round_info))
        return EXIT_OK
    verdict = round_info.get("verdict")
    label = "" if verdict in (None, "", "comment") else " — %s" % verdict  # "comment" = no verdict
    to_post = sum(1 for t in selected for c in [t["root"]] + t["replies"]
                  if render.awaits_github(c) and c.get("state") != "pending")
    todo = " — %d GitHub comment%s to check and post" % (to_post, "" if to_post == 1 else "s") if to_post else ""
    out("ccr: round %d%s — %d new comments in %d threads%s" % (number, label, len(round_info["comment_ids"]),
                                                               len(selected), todo))
    sys.stdout.write(render.render_comments(review, comments, fetch_file_diff(client), threads=selected,
                                            matching=matching, mark="★ new in round %d" % number))
    sys.stdout.flush()
    return EXIT_OK


def report_change(client: Client, since_version: int, state: dict, call_time: str, as_json: bool) -> int:
    review, comments = load_review(client)
    threads = render.sort_threads(render.build_threads(comments), review)
    selected, matching = render.select_threads(threads, updated_since=call_time)
    if as_json:
        print_json(dict(render.to_json(review, comments, threads=selected), version_from=since_version,
                        version_to=state["version"]))
        return EXIT_OK
    counts = state["counts"]
    out("ccr: version %d→%d · pending %d · unresolved %d · rounds %d" % (
        since_version, state["version"], counts["pending"], counts["unresolved"], state["rounds"]))
    if selected:
        sys.stdout.write(render.render_comments(review, comments, fetch_file_diff(client), threads=selected,
                                                matching=matching))
        sys.stdout.flush()
    return EXIT_OK


class _Waiter:
    """State machine of ``ccr wait``: long-polls ``/api/events`` until a new round / change or the deadline."""

    def __init__(self, args, client: Client, record, state: dict):
        self.args = args
        self.client = client
        self.record = record
        self.state = state
        self.since_round = state["rounds"] if args.since_round is None else args.since_round
        self.since_version = state["version"] if args.since_version is None else args.since_version
        self.call_time = state["now"]
        self.started = time.monotonic()
        self.deadline = None if args.timeout <= 0 else self.started + args.timeout
        self.failing_since = None
        self.ui_notice_printed = False

    def run(self) -> int:
        version = self.state["version"]
        while True:
            remaining = None if self.deadline is None else self.deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self._timed_out()
            poll = WAIT_POLL_SECONDS if remaining is None else max(0.0, min(WAIT_POLL_SECONDS, remaining))
            if not self.ui_notice_printed:
                poll = min(poll, max(1.0, UI_NOTICE_SECONDS - (time.monotonic() - self.started)))
            try:
                self.state = self.client.get("/api/events?since=%d&timeout=%s" % (version, _format_seconds(poll)),
                                             timeout=WAIT_REQUEST_TIMEOUT)
            except URLError:
                if self._server_gone():
                    return EXIT_NO_SESSION
                time.sleep(WAIT_RETRY_SECONDS)
                continue
            except ApiError as exc:
                if exc.status == 401:  # another server (new token) answers on this port now
                    err("ccr: server was replaced while waiting (new token); run ccr status for the new URL")
                    return EXIT_NO_SESSION
                raise
            self.failing_since = None
            version = self.state["version"]
            if self.args.any_change:
                if version > self.since_version:
                    return report_change(self.client, self.since_version, self.state, self.call_time, self.args.json)
            elif self.state["rounds"] > self.since_round:
                return report_round(self.client, self.since_round + 1, self.args.json)
            self._maybe_ui_notice()

    def _server_gone(self) -> bool:
        now = time.monotonic()
        if self.failing_since is None:
            self.failing_since = now
        pid_dead = self.record is not None and not session.pid_alive(self.record.get("pid"))
        if not pid_dead and now - self.failing_since < WAIT_GONE_SECONDS:
            return False
        if self.record is not None:  # only our own record — a replacement server may have written a new one
            session.remove_record(session.paths_for(self.record["repo"]).record, self.record.get("pid"))
        err("ccr: server gone")
        return True

    def _maybe_ui_notice(self) -> None:
        if self.ui_notice_printed or time.monotonic() - self.started < UI_NOTICE_SECONDS:
            return
        self.ui_notice_printed = True
        if (self.state.get("ui") or {}).get("last_seen") is None:
            err("ccr: UI not opened yet")

    def _timed_out(self) -> int:
        what = "no change" if self.args.any_change else "no new round"
        err("ccr: %s after %s s (rounds: %d, pending unsubmitted: %d, version: %d)" % (
            what, _format_seconds(self.args.timeout), self.state["rounds"], self.state["counts"]["pending"],
            self.state["version"]))
        return EXIT_TIMEOUT


def cmd_wait(args) -> int:
    client, record, state = connect(args)
    if state is None:
        state = client.get("/api/state")
    return _Waiter(args, client, record, state).run()


# --------------------------------------------------------------------------- replies and comments

def find_duplicate(client: Client, thread_id: str, author: str, body: str):
    """The existing comment on ``thread_id`` with the same author and body, or None."""
    text = body.strip()
    for comment in client.get("/api/comments")["comments"]:
        if comment["id"] == thread_id or comment.get("parent_id") == thread_id:
            if comment["author"] == author and comment["body"] == text:
                return comment
    return None


def post_reply(client: Client, thread_id: str, body: str, author: str, resolve: bool, force: bool) -> dict:
    """Create one reply (refusing an identical one unless ``force``) and optionally resolve the thread."""
    if not force:
        duplicate = find_duplicate(client, thread_id, author, body)
        if duplicate is not None:
            raise CliError("identical reply already exists on this thread (id %s); use --force" % duplicate["id"])
    created = client.post("/api/comments", {"body": body, "parent_id": thread_id, "author": author})
    if resolve:
        client.patch("/api/comments/%s" % quote(thread_id, safe=""), {"resolved": True})
    return created


def parse_batch(text: str) -> list:
    """``[{"id", "body", "resolve"}]`` from a JSON list or Markdown ``## <id> [resolve]`` sections."""
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            items = json.loads(stripped)
        except ValueError as exc:
            raise CliError("invalid batch JSON: %s" % exc) from None
        result = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise CliError("every batch item must be an object with an \"id\"")
            result.append({"id": item["id"], "body": item.get("body") if isinstance(item.get("body"), str) else "",
                           "resolve": bool(item.get("resolve", False))})
        return result
    result, current = [], None
    for line in text.splitlines():
        match = _BATCH_HEADING_RE.match(line)
        if match:
            current = {"id": match.group(1), "body": "", "resolve": "[resolve]" in line}
            result.append(current)
        elif current is not None:
            current["body"] += line + "\n"
        elif line.strip():
            raise CliError("batch Markdown must start with a '## <id> [resolve]' heading")
    for item in result:
        item["body"] = item["body"].strip()
    return result


def cmd_reply_batch(args) -> int:
    text = sys.stdin.read() if args.batch == "-" else open(args.batch, "r", encoding="utf-8").read()
    items = parse_batch(text)
    if not items:
        raise CliError("the batch contains no replies")
    client, _, _ = connect(args)
    results, failed = [], False
    for item in items:
        entry = {"id": item["id"], "resolved": False}
        try:
            if not item["body"]:
                raise CliError("body must not be empty")
            created = post_reply(client, item["id"], item["body"], args.author, item["resolve"], args.force)
            entry.update(ok=True, comment=created, resolved=item["resolve"])
            if not args.json:
                out("%s: replied%s" % (item["id"], ", resolved" if item["resolve"] else ""))
        except (ApiError, CliError) as exc:
            failed = True
            entry.update(ok=False, error=str(exc))
            if not args.json:
                out("%s: ERROR %s" % (item["id"], exc))
        results.append(entry)
    if args.json:
        print_json(results)
    return EXIT_ERROR if failed else EXIT_OK


def cmd_reply(args) -> int:
    if args.batch is not None:
        return cmd_reply_batch(args)
    if not args.id:
        raise CliError("a thread id is required (or --batch FILE)")
    body = read_body(args)
    client, _, _ = connect(args)
    created = post_reply(client, args.id, body, args.author, args.resolve, args.force)
    if args.json:
        print_json(created)
    else:
        out("%s: replied (%s)%s" % (args.id, created["id"], ", resolved" if args.resolve else ""))
    return EXIT_OK


def cmd_comment(args) -> int:
    anchor = anchor_from_args(args)
    body = read_body(args)
    client, _, _ = connect(args)
    payload = {"body": body, "anchor": anchor, "author": args.author}
    if args.github:
        payload["github"] = True
    created = client.post("/api/comments", payload)
    if args.json:
        print_json(created)
    else:
        out("ccr: created comment %s (%s%s)" % (created["id"], describe_anchor(created["anchor"]),
                                                ", GitHub comment" if created.get("github") else ""))
    return EXIT_OK


def _for_each_id(args, action, verb: str) -> int:
    """Apply ``action(client, id)`` to every id, printing ``<id>: <verb>`` or ``<id>: ERROR …``; exit 1 if any failed."""
    client, _, _ = connect(args)
    results, failed = [], False
    for comment_id in args.ids:
        try:
            payload = action(client, comment_id)
            results.append({"id": comment_id, "ok": True, "result": payload})
            if not args.json:
                out("%s: %s" % (comment_id, verb))
        except ApiError as exc:
            failed = True
            results.append({"id": comment_id, "ok": False, "error": str(exc)})
            if not args.json:
                out("%s: ERROR %s" % (comment_id, exc))
    if args.json:
        print_json(results)
    return EXIT_ERROR if failed else EXIT_OK


def cmd_resolve(args) -> int:
    return _for_each_id(args, lambda c, i: c.patch("/api/comments/%s" % quote(i, safe=""), {"resolved": True}), "resolved")


def cmd_unresolve(args) -> int:
    return _for_each_id(args, lambda c, i: c.patch("/api/comments/%s" % quote(i, safe=""), {"resolved": False}), "unresolved")


def cmd_delete(args) -> int:
    suffix = "?cascade=1" if args.cascade else ""
    return _for_each_id(args, lambda c, i: c.delete("/api/comments/%s%s" % (quote(i, safe=""), suffix)), "deleted")


def cmd_edit(args) -> int:
    body = read_body(args)
    client, _, _ = connect(args)
    edited = client.patch("/api/comments/%s" % quote(args.id, safe=""), {"body": body})
    if args.json:
        print_json(edited)
    else:
        out("%s: edited" % args.id)
    return EXIT_OK


def cmd_move(args) -> int:
    anchor = anchor_from_args(args)
    client, _, _ = connect(args)
    moved = client.patch("/api/comments/%s" % quote(args.id, safe=""), {"anchor": anchor})
    if args.json:
        print_json(moved)
    else:
        out("%s: moved to %s" % (args.id, describe_anchor(moved["anchor"])))
    return EXIT_OK


def _github_where(target: dict) -> str:
    path = render.clean(target["path"] or "the pull request", True)
    if target["subject_type"] == "REPLY":
        return "reply to @%s on %s" % (render.clean(target.get("thread_author"), True), path)
    if target["subject_type"] == "FILE":
        return "%s (file)" % path
    span = "%d-%d" % (target["start_line"], target["line"]) if target.get("start_line") else str(target["line"])
    return "%s:%s (%s)" % (path, span, target["side"])


def print_github_plan(comment_id: str, target: dict, say) -> None:
    """``gh-post --dry-run``: where on the pull request diff a comment would go, the lines there, its verbatim body."""
    say("%s: would post to %s %s, commit %s" % (comment_id, github.pr_label(target["pr"]), _github_where(target),
                                                 target["commit"][:gitx.SHORT_SHA_LEN]))
    if target["subject_type"] == "REPLY":
        say("  thread: %s" % render.clean(target.get("thread_url"), True))
    for row in target["lines"]:
        say("  %6d | %s" % (row["line"], render.clean(row["text"])))
    say("  body:")
    for line in target["body"].split("\n"):
        say("    " + render.clean(line))


def post_one(client: Client, remote, review: dict, comment_id: str, dry_run: bool, say) -> dict:
    """Check one GitHub comment, then post it (or show where it would go); returns its ``--json`` entry."""
    target = client.get("/api/comments/%s/github" % quote(comment_id, safe=""))
    edited = target["github"]["status"] == "posted" and target["github"].get("edited")
    if target["github"]["status"] == "posted" and not edited:
        say("%s: already posted → %s" % (comment_id, target["github"]["url"]))
        return {"id": comment_id, "ok": True, "already": True, "github": target["github"]}
    if target["state"] != "submitted":
        raise CliError("comment %s is still pending in ccr; it can be posted once the user submits it" % comment_id)
    if edited:
        return update_one(client, remote, comment_id, target, dry_run, say)
    if dry_run:
        print_github_plan(comment_id, target, say)
        return {"id": comment_id, "ok": True, "target": target}
    result = github.post_comment(remote, target, target["body"], review["repo"]["path"])
    if result["created_review"]:
        say("ccr: started your pending review on %s" % github.pr_label(target["pr"]))
    try:
        updated = client.post("/api/comments/%s/github" % quote(comment_id, safe=""), {"posted": result["record"]})
    except (ApiError, URLError) as exc:
        raise CliError("posted to your pending review (%s) but ccr could not record it (%s); run ccr gh-post %s "
                       "again to record it" % (result["record"]["url"], exc, comment_id)) from None
    verb = "found already in your pending review" if result["already"] else "posted"
    say("%s: %s %s → %s" % (comment_id, verb, _github_where(target), result["record"]["url"]))
    for note in result["notes"]:
        say("  note: %s" % note)
    for problem in result["problems"]:
        say("  warning: %s" % problem)
    return {"id": comment_id, "ok": not result["problems"], "posted": True, "comment": updated,
            "notes": result["notes"], "problems": result["problems"]}


def print_github_news(news: list, say) -> None:
    """What a sync found added, edited or deleted on GitHub, one line per comment."""
    for item in news:
        where = describe_anchor(item["anchor"]) + ("" if item["parent_id"] is None else ", a reply")
        excerpt = render.clean(item["body"], True)
        say("  %-7s %s @%s on %s: %s" % (item["change"], item["id"], render.clean(item["login"], True), where,
                                        excerpt if len(excerpt) <= 72 else excerpt[:71] + "…"))


def update_one(client: Client, remote, comment_id: str, target: dict, dry_run: bool, say) -> dict:
    """Put the new text of a posted GitHub comment, edited in ccr since, into the pending review (10.3)."""
    url = target["github"]["url"]
    if dry_run:
        say("%s: would update %s in your pending review on %s; new body:" % (comment_id, url, github.pr_label(target["pr"])))
        for line in target["body"].split("\n"):
            say("    " + render.clean(line))
        return {"id": comment_id, "ok": True, "target": target}
    result = github.update_comment(remote, target["github"]["node_id"], target["body"])
    try:
        updated = client.post("/api/comments/%s/github" % quote(comment_id, safe=""), {"updated": {"url": result["url"]}})
    except (ApiError, URLError) as exc:
        raise CliError("updated in your pending review (%s) but ccr could not record it (%s); run ccr gh-post %s "
                       "again to record it" % (url, exc, comment_id)) from None
    say("%s: %s → %s" % (comment_id, "found updated already in your pending review" if result["already"] else "updated",
                         result["url"]))
    for problem in result["problems"]:
        say("  warning: %s" % problem)
    return {"id": comment_id, "ok": not result["problems"], "posted": True, "updated": True, "comment": updated,
            "problems": result["problems"]}


def cmd_gh_sync(args) -> int:
    client, _, _ = connect(args)
    pr = client.get("/api/review").get("pr")
    if not pr:
        raise CliError("the review is not linked to a GitHub pull request; start ccr with --pr URL")
    result = client.post("/api/github/sync", github.fetch_discussion(github.PullRequest(pr)))
    if args.json:
        print_json(result)
    else:
        out("ccr: %s: %d review threads, %d review bodies; %d comments added, %d updated, %d removed" % (
            github.pr_label(pr), result["threads"], result["reviews"], result["added"], result["updated"],
            result["removed"]))
    return EXIT_OK


def cmd_gh_post(args) -> int:
    client, _, _ = connect(args)
    review = client.get("/api/review")
    pr = review.get("pr")
    if not pr:
        raise CliError("the review is not linked to a GitHub pull request; start ccr with --pr URL")
    remote = github.PullRequest(pr)
    say = (lambda line: None) if args.json else out
    # GitHub's discussion as it is now, before anything goes into it: what came since the last sync is read first
    label = github.pr_label(pr)
    try:
        news = client.post("/api/github/sync", github.fetch_discussion(remote)).get("news")
    except (ApiError, GitHubError) as exc:
        raise CliError("could not re-read the discussion on %s, so nothing is posted: %s" % (label, exc)) from None
    if news is None:
        raise CliError("the running ccr server is older than this ccr and does not report what changed on GitHub, so "
                       "nothing is posted; it needs a restart first (ccr stop && ccr start keeps the review)")
    if news:
        say("ccr: %s: %d comments added, edited or deleted on GitHub since the last sync:" % (label, len(news)))
        print_github_news(news, say)
    unread = None
    if news and not args.dry_run:
        unread = "%d comments changed on GitHub since the last sync; read them (ccr comments), then run ccr gh-post " \
                 "again" % len(news)
    results, posted = [], False
    for comment_id in dict.fromkeys(args.ids):
        if unread:
            results.append({"id": comment_id, "ok": False, "error": unread, "news": news})
            say("%s: ERROR not posted: %s" % (comment_id, unread))
            continue
        try:
            entry = post_one(client, remote, review, comment_id, args.dry_run, say)
            posted = posted or entry.get("posted", False)
        except (ApiError, CliError, GitHubError) as exc:
            entry = {"id": comment_id, "ok": False, "error": str(exc)}
            say("%s: ERROR %s" % (comment_id, exc))
        results.append(entry)
    if posted:
        say("ccr: your pending review is on GitHub, to submit with a verdict there: %s/files" % pr["url"])
    if args.json:
        print_json(results)
    return EXIT_OK if all(entry["ok"] for entry in results) else EXIT_ERROR


def cmd_export(args) -> int:
    client, _, _ = connect(args)
    review, comments = load_review(client)
    if args.json and not args.md:
        text = json_text(render.to_json(review, comments))
    else:
        text = render.render_export(review, comments, fetch_file_diff(client))
    if args.output:
        write_private_text(args.output, text)
        out("ccr: exported to %s" % args.output)
    else:
        sys.stdout.write(text)
        sys.stdout.flush()
    return EXIT_OK


def sync_github(client: Client, pr: dict, why: str, say=out) -> None:
    result = client.post("/api/github/sync", github.fetch_discussion(github.PullRequest(pr)))
    say("ccr: %s: synced %s: %d comments added, %d updated, %d removed" % (
        github.pr_label(pr), why, result["added"], result["updated"], result["removed"]))


def print_restore(result: dict, name: str) -> None:
    would = "would restore" if result["dry_run"] else "restored"
    out("ccr: %s %s (%s) and %s from %s (%s export)%s" % (
        would, plural(result["comments"], "comment"), plural(result["threads"], "thread"),
        plural(result["rounds"], "round"), name, result["source"],
        {"restored": "; cover letter " + ("to restore" if result["dry_run"] else "restored"),
         "kept": "; the review's own cover letter kept"}.get(result["cover"], "")))
    mirrored = result["mirrored"]
    if mirrored["matched"] or mirrored["restored"] or mirrored["left_to_sync"] or result["dropped_copies"]:
        out("ccr: GitHub: %d mirrored comments matched, %d restored, %d left to the sync; %d mirrored copies of your "
            "posted comments dropped" % (mirrored["matched"], mirrored["restored"], mirrored["left_to_sync"],
                                         result["dropped_copies"]))
    for comment_id in mirrored["unmatched"]:
        out("ccr: GitHub thread %s matches nothing on GitHub any more; restored, marked deleted there, for its "
            "replies" % comment_id)
    if result["outdated"]:
        out("ccr: %s anchored to commits outside the range (outdated)" % plural(result["outdated"], "thread"))
    for old, new in sorted(result["renamed"].items()):
        out("ccr: comment %s restored as %s (its id is taken in this database)" % (old, new))


def cmd_restore(args) -> int:
    name = "stdin" if args.file == "-" else args.file
    text = sys.stdin.read() if args.file == "-" else open(args.file, "r", encoding="utf-8").read()
    try:
        payload = restore.parse(text)
    except restore.RestoreError as exc:
        raise CliError("%s: %s" % (name, exc)) from None
    client, _, _ = connect(args)
    pr = client.get("/api/review").get("pr")
    # refuses (a review with comments of its own, another pull request, unknown commits) before anything changes
    result = client.post("/api/restore", {"payload": payload, "dry_run": True})
    markdown_mirrors = payload["source"] == "markdown" and any(c["author"] == "github" for c in payload["comments"])
    if not args.dry_run:
        if pr and markdown_mirrors:  # a Markdown export's mirrored threads are found by their text among the sync's
            sync_github(client, pr, "before the restore", err if args.json else out)
        result = client.post("/api/restore", {"payload": payload})
    if args.json:
        print_json(result)
    else:
        print_restore(result, name)
        if args.dry_run and pr and markdown_mirrors:
            out("ccr: a restore syncs with GitHub first and matches the export's mirrored threads to what it brings")
    if pr and not args.dry_run:  # posted comments learn their threads and GitHub's state of them again
        sync_github(client, pr, "after the restore", err if args.json else out)
    return EXIT_OK


COMMANDS = {
    "start": cmd_start, "serve": cmd_serve, "stop": cmd_stop, "status": cmd_status, "sessions": cmd_sessions,
    "logs": cmd_logs, "open": cmd_open, "reload": cmd_reload, "comments": cmd_comments, "wait": cmd_wait,
    "cover": cmd_cover, "gh-post": cmd_gh_post, "gh-sync": cmd_gh_sync,
    "reply": cmd_reply, "comment": cmd_comment, "resolve": cmd_resolve, "unresolve": cmd_unresolve,
    "edit": cmd_edit, "delete": cmd_delete, "move": cmd_move, "export": cmd_export, "restore": cmd_restore,
}


def main(argv=None) -> int:
    """Entry point: parse, dispatch, and turn every expected failure into ``ccr: <message>`` + exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_ERROR
    try:
        return COMMANDS[args.command](args)
    except NoSessionError as exc:
        err("ccr: %s" % exc)
        return EXIT_NO_SESSION
    except URLError as exc:
        err("ccr: cannot reach the server: %s" % (exc.reason if exc.reason is not None else exc))
        return EXIT_NO_SESSION
    except BrokenPipeError:
        # The reader went away (`ccr status | head`): stop quietly, and point stdout at /dev/null so
        # the interpreter's final flush cannot raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_ERROR
    except (GitError, StoreError, SessionError, ApiError, CliError, GitHubError, OSError) as exc:
        err("ccr: %s" % exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        err("ccr: interrupted")
        return EXIT_ERROR

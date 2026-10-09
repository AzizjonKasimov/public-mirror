"""The `public-mirror` command line."""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
from pathlib import Path

from .build import Snapshot, build, plan, write_tree
from .classify import PRIVATE, PUBLIC, UNMATCHED
from .config import CONFIG_NAME, Config, ConfigError, load, write_template
from .git import GitError, Repo
from .remote import commit_and_push, fetch, pull, require_remote

LIST_LIMIT = 15


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Piped output goes to tools that expect UTF-8; a console handles Unicode on its own.
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = _parser().parse_args(argv)
    try:
        repo = Repo.discover(Path(args.directory))
        if args.command == "init":
            path = write_template(repo.path)
            print(f"Created {path}. List the public and private paths in it, then run `public-mirror check`.")
            return 0
        return COMMANDS[args.command](repo, load(repo.path), args)
    except (ConfigError, GitError) as error:
        print(f"public-mirror: {error}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="public-mirror",
        description="Publish a public copy of a private git repo, without its private files and private lines.")
    parser.add_argument("-C", dest="directory", default=".", metavar="PATH", help="run in this repo instead of the current one")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("init", help=f"create a starter {CONFIG_NAME}")
    check = commands.add_parser("check", help="build the public copy of HEAD in memory and report problems")
    check.add_argument("-v", "--verbose", action="store_true", help="also list removed lines and unscanned files")
    files = commands.add_parser("files", help="show whether each file is public, private, or unmatched, and why")
    files.add_argument("--status", choices=[PUBLIC, PRIVATE, UNMATCHED], help="show only files with this status")
    files.add_argument("paths", nargs="*", help="limit the list to these files or folders")
    export = commands.add_parser("export", help="write the public copy of HEAD to a folder for review")
    export.add_argument("target", help="an empty folder outside the repo")
    publish = commands.add_parser("publish", help="commit the public copy of HEAD to the public repo and push it")
    publish.add_argument("-m", "--message", help="commit message for the public repo")
    publish.add_argument("--dry-run", action="store_true", help="show what would change without committing or pushing")
    publish.add_argument("--overwrite-public-changes", action="store_true",
                         help="publish over commits made directly in the public repo instead of pulling them first")
    commands.add_parser("pull", help="apply commits made directly in the public repo to this repo's working tree")
    return parser


def cmd_check(repo: Repo, config: Config, args: argparse.Namespace) -> int:
    snapshot = build(repo, config)
    _report(repo, snapshot, args.verbose)
    return 1 if snapshot.errors else 0


def cmd_files(repo: Repo, config: Config, args: argparse.Namespace) -> int:
    commit = repo.resolve("HEAD^{commit}")
    if commit is None:
        raise GitError("the repo has no commits yet")
    result, _, _ = plan(repo, config, commit)
    prefixes = [p.replace("\\", "/").strip("/").removeprefix("./") for p in args.paths]
    rows = [(result.status[path], path, result.reason[path]) for path in sorted(result.status)
            if (not args.status or result.status[path] == args.status)
            and (not prefixes or any(path == p or path.startswith(p + "/") for p in prefixes))]
    width = max((len(path) for _, path, _ in rows), default=0)
    for status, path, reason in rows:
        print(f"{status:<9}  {path:<{width}}  {reason}")
    return 0


def cmd_export(repo: Repo, config: Config, args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    if target == repo.path.resolve() or repo.path.resolve() in target.parents:
        raise ConfigError("export to a folder outside the repo, so the copy is not committed by accident")
    if target.exists() and any(target.iterdir()):
        raise ConfigError(f"{target} is not empty")
    snapshot = build(repo, config)
    _report(repo, snapshot, verbose=False)
    tree = write_tree(repo, snapshot)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(repo.git("archive", "--format=tar", tree))) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(target, filter="data")
        else:
            archive.extractall(target)
    print(f"Exported {_count(len(snapshot.files), 'file')} to {target}")
    return 1 if snapshot.errors else 0


def cmd_publish(repo: Repo, config: Config, args: argparse.Namespace) -> int:
    require_remote(config)
    snapshot = build(repo, config)
    _report(repo, snapshot, verbose=False)
    if snapshot.errors:
        print("Nothing was published.")
        return 1
    if not snapshot.files:
        print("No file is public yet, so there is nothing to publish.")
        return 1
    state = fetch(repo, config)
    if state.foreign_pending(repo) and not args.overwrite_public_changes:
        print(f"\nThe public repo has {len(state.foreign)} commit(s) that public-mirror did not make:")
        for sha, subject in state.foreign[:LIST_LIMIT]:
            print(f"  {sha[:7]} {subject}")
        print("Publishing now would undo them. Run `public-mirror pull` to bring them into this repo,\n"
              "commit, and publish again. To replace them with this repo's files instead, run\n"
              "`public-mirror publish --overwrite-public-changes`.")
        return 1
    tree = write_tree(repo, snapshot)
    same_tree = state.tip is not None and repo.text("rev-parse", f"{state.tip}^{{tree}}") == tree
    if same_tree and not state.foreign:
        print("\nThe public repo already matches HEAD. Nothing to publish.")
        return 0
    if same_tree:
        # Still commit, so the public tip is a mirror commit again and no other machine
        # mistakes the pulled commits for new ones.
        print("\nNo file changes. The new commit marks the public repo's own commits as pulled.")
    elif state.tip:
        print("\nChanges to the public repo:")
        print(_indent(repo.text("-c", "core.quotePath=false", "diff", "--stat=100", state.tip, tree)))
    else:
        print(f"\nFirst publish: {_count(len(snapshot.files), 'file')}.")
    if args.dry_run:
        print("Dry run: nothing was committed or pushed.")
        return 0
    commit = commit_and_push(repo, config, tree, state.tip, snapshot.commit, args.message or config.message)
    print(f"Published {commit[:7]} to {config.remote} ({config.branch}).")
    return 0


def cmd_pull(repo: Repo, config: Config, args: argparse.Namespace) -> int:
    state = fetch(repo, config)
    if not state.foreign:
        print("Nothing to pull: every public commit came from public-mirror.")
        return 0
    if not state.foreign_pending(repo):
        print("Already pulled. Commit those changes, then run `public-mirror publish`.")
        return 0
    if state.last_mirror is None:
        print("The public repo has no public-mirror commit yet, so there is no common base to merge from.\n"
              "Copy anything worth keeping by hand, then run `public-mirror publish --overwrite-public-changes`.")
        return 1
    _, _, stand_ins = plan(repo, config, repo.resolve("HEAD^{commit}"))
    conflicts, output, skipped = pull(repo, state, set(stand_ins))
    if output:
        print(output)
    print(f"Applied {len(state.foreign)} public commit(s) to the working tree:")
    for sha, subject in state.foreign[:LIST_LIMIT]:
        print(f"  {sha[:7]} {subject}")
    if skipped:
        print("Not applied, because their public content comes from elsewhere. Copy any change by hand to:")
        for path in skipped:
            print(f"  {path} (from {stand_ins[path]})")
    if conflicts:
        print("Some files have conflicts. Resolve them, then commit.")
    print("Review the changes, commit them, then run `public-mirror publish`.")
    return 1 if conflicts else 0


COMMANDS = {"check": cmd_check, "files": cmd_files, "export": cmd_export, "publish": cmd_publish, "pull": cmd_pull}


def _report(repo: Repo, snapshot: Snapshot, verbose: bool) -> None:
    result = snapshot.classification
    public, private, unmatched = (result.paths(status) for status in (PUBLIC, PRIVATE, UNMATCHED))
    rewritten = sorted(set(snapshot.transforms) | set(snapshot.replaced))
    notes = [f"{len(snapshot.stand_ins)} filled from public versions or other repos"] if snapshot.stand_ins else []
    notes += [f"{len(rewritten)} rewritten"] if rewritten else []
    note = f" ({'; '.join(notes)})" if notes else ""
    print(f"Checked HEAD ({snapshot.commit[:7]}): {_count(len(public), 'public file')}{note}, "
          f"{len(private)} private, {len(unmatched)} unmatched.")
    if unmatched:
        print("Unmatched files stay private. Add them to [paths] to decide:")
        _print_list(unmatched, "public-mirror files --status unmatched")
    if verbose and rewritten:
        print("Rewritten in the public copy:")
        for path in rewritten:
            change, parts = snapshot.transforms.get(path), []
            if change and change.blocks:
                parts.append(f"{change.blocks} private block(s) removed")
            if change and change.dropped:
                parts.append("lines dropped for linking to unpublished files: "
                             + ", ".join(f"{line} ({target})" for line, target in change.dropped))
            if path in snapshot.replaced:
                parts.append(f"{_count(snapshot.replaced[path], 'term')} replaced")
            print(f"  {path}: {'; '.join(parts)}")
    if verbose and snapshot.unscanned:
        print("Not scanned (images and files the scanner cannot read). Check these yourself:")
        _print_list(snapshot.unscanned, None, limit=len(snapshot.unscanned))
    elif snapshot.unscanned:
        print(f"{len(snapshot.unscanned)} image or binary files could not be scanned; `check -v` lists them.")
    pending = repo.uncommitted_count()
    if pending:
        print(f"Note: {pending} uncommitted change(s) are left out. Only committed files are published.")
    for finding in snapshot.warnings:
        print(f"warning: {finding}")
    for finding in snapshot.errors:
        print(f"error: {finding}")
    print(f"{len(snapshot.errors)} problem(s) to fix before publishing." if snapshot.errors else "No problems found.")


def _print_list(items: list[str], more_command: str | None, limit: int = LIST_LIMIT) -> None:
    for item in items[:limit]:
        print(f"  {item}")
    if len(items) > limit:
        hint = f" (run `{more_command}`)" if more_command else ""
        print(f"  ... and {len(items) - limit} more{hint}")


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())

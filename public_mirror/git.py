"""Small wrappers around the git command line."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed. The message carries git's own error text."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str  # "blob", or "commit" for a submodule
    sha: str
    size: int  # 0 for submodules
    path: str


def run_git(args: list[str], cwd: Path | str, *, input: bytes | None = None,
            env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input,
        env={**os.environ, **env} if env else None,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
        raise GitError(f"git {args[0]} failed: {message}")
    return proc


def encode_path(path: str) -> bytes:
    # Paths are decoded with surrogateescape, so any byte sequence git gave us round-trips.
    return path.encode("utf-8", "surrogateescape")


def decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


class Repo:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def discover(cls, start: Path) -> Repo:
        top = run_git(["rev-parse", "--show-toplevel"], start).stdout.decode("utf-8").strip()
        return cls(Path(top))

    def git(self, *args: str, input: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
        return run_git(list(args), self.path, input=input, env=env).stdout

    def text(self, *args: str, input: bytes | None = None, env: dict[str, str] | None = None) -> str:
        return self.git(*args, input=input, env=env).decode("utf-8", "replace").strip()

    def try_git(self, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess:
        return run_git(list(args), self.path, input=input, check=False)

    def resolve(self, ref: str) -> str | None:
        proc = self.try_git("rev-parse", "--verify", "--quiet", ref)
        return proc.stdout.decode().strip() if proc.returncode == 0 else None

    def ls_tree(self, commit: str) -> list[TreeEntry]:
        entries = []
        for record in self.git("ls-tree", "-r", "-l", "-z", "--full-tree", commit).split(b"\0"):
            if not record:
                continue
            meta, path = record.split(b"\t", 1)
            mode, kind, sha, size = meta.decode().split()
            entries.append(TreeEntry(mode, kind, sha, 0 if size == "-" else int(size), decode_path(path)))
        return entries

    def read_blobs(self, shas: Iterable[str]) -> dict[str, bytes]:
        wanted = list(dict.fromkeys(shas))
        if not wanted:
            return {}
        out = self.git("cat-file", "--batch", input="".join(f"{sha}\n" for sha in wanted).encode())
        blobs, pos = {}, 0
        for sha in wanted:
            header_end = out.index(b"\n", pos)
            header = out[pos:header_end].split()
            if len(header) != 3:
                raise GitError(f"object {sha} is missing")
            start = header_end + 1
            size = int(header[2])
            blobs[sha] = out[start:start + size]
            pos = start + size + 1  # skip the newline after each object
        return blobs

    def write_blob(self, data: bytes) -> str:
        # Content from stdin is stored as is: no clean filters or line-ending conversion.
        return self.text("hash-object", "-w", "--stdin", input=data)

    def write_tree(self, entries: Iterable[tuple[str, str, str]]) -> str:
        """Build a tree object from (mode, sha, path) entries through a throwaway index."""
        records = b"".join(f"{mode} {sha}\t".encode() + encode_path(path) + b"\0" for mode, sha, path in entries)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp, "index"))}
            self.git("update-index", "-z", "--index-info", input=records, env=env)
            return self.text("write-tree", env=env)

    def commit_tree(self, tree: str, parents: list[str], message: str, env: dict[str, str] | None = None) -> str:
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        return self.text(*args, "-F", "-", input=message.encode("utf-8"), env=env)

    def remote_branch(self, remote: str, branch: str) -> str | None:
        """The commit a remote branch points to, or None when the branch does not exist."""
        ref = f"refs/heads/{branch}"
        for line in self.text("ls-remote", "--heads", remote, ref).splitlines():
            sha, name = line.split("\t", 1)
            if name == ref:
                return sha
        return None

    def first_parent_log(self, tip: str) -> list[tuple[str, str]]:
        """(sha, full message) for each commit on the first-parent line from tip, newest first."""
        out = self.git("log", "--first-parent", "--format=%H%x1f%B%x1e", tip).decode("utf-8", "replace")
        commits = []
        for record in out.split("\x1e"):
            if record.strip():
                sha, message = record.strip("\n").split("\x1f", 1)
                commits.append((sha, message))
        return commits

    def uncommitted_count(self) -> int:
        return len(self.text("status", "--porcelain").splitlines())

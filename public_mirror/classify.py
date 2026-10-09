"""Decide which files are public.

Patterns use .gitignore syntax and git itself matches them (`git check-ignore`), so they
behave exactly like .gitignore lines: a leading `/` anchors to the repo root, a trailing `/`
means a folder, `**` crosses folders, and `!` re-includes.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .git import GitError, decode_path, encode_path, run_git

PUBLIC, PRIVATE, UNMATCHED = "public", "private", "unmatched"


@dataclass
class Classification:
    status: dict[str, str] = field(default_factory=dict)
    reason: dict[str, str] = field(default_factory=dict)
    unused: list[tuple[str, str]] = field(default_factory=list)  # (list name, pattern) that matched nothing

    def paths(self, status: str) -> list[str]:
        return sorted(path for path, value in self.status.items() if value == status)


def classify(paths: list[str], public_patterns: list[str], private_patterns: list[str],
             always_private: set[str]) -> Classification:
    """A file is public when a `public` pattern selects it and no `private` pattern does."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # An empty repo with no templates, so no .gitignore or info/exclude can interfere.
        run_git(["init", "--quiet", "--template=", "matcher"], cwd=tmp)
        matcher = Path(tmp, "matcher")
        public = _match(matcher, Path(tmp, "public-patterns"), paths, public_patterns)
        private = _match(matcher, Path(tmp, "private-patterns"), paths, private_patterns)

    result = Classification()
    used: dict[str, set[int]] = {"public": set(), "private": set()}
    for path in paths:
        pub, priv = public[path], private[path]
        if pub:
            used["public"].add(pub[0])
        if priv:
            used["private"].add(priv[0])
        if path in always_private:
            result.status[path], result.reason[path] = PRIVATE, "always private (public-mirror settings)"
        elif priv and priv[1]:
            result.status[path], result.reason[path] = PRIVATE, f"private: {private_patterns[priv[0]]}"
        elif pub and pub[1]:
            result.status[path], result.reason[path] = PUBLIC, f"public: {public_patterns[pub[0]]}"
        else:
            result.status[path], result.reason[path] = UNMATCHED, "matches no pattern, so it stays private"
    for name, patterns in (("public", public_patterns), ("private", private_patterns)):
        result.unused += [(name, pattern) for index, pattern in enumerate(patterns) if index not in used[name]]
    return result


def _match(repo_dir: Path, pattern_file: Path, paths: list[str],
           patterns: list[str]) -> dict[str, tuple[int, bool] | None]:
    """Map each path to (index of the deciding pattern, selected?), or None when no pattern applies."""
    if not patterns:
        return dict.fromkeys(paths)
    pattern_file.write_text("".join(f"{pattern}\n" for pattern in patterns), encoding="utf-8")
    proc = run_git(
        ["-c", f"core.excludesFile={pattern_file.as_posix()}", "-c", "core.ignoreCase=false",
         "check-ignore", "--no-index", "--verbose", "--non-matching", "-z", "--stdin"],
        repo_dir,
        input=b"".join(encode_path(path) + b"\0" for path in paths),
        check=False,
    )
    if proc.returncode not in (0, 1):  # 1 only means "nothing matched"
        raise GitError("git check-ignore failed: " + proc.stderr.decode("utf-8", "replace").strip())
    fields = proc.stdout.split(b"\0")
    result: dict[str, tuple[int, bool] | None] = {}
    for i in range(0, len(fields) - 3, 4):
        _source, line, pattern, path = fields[i:i + 4]
        # A `!pattern` is reported as the match, but it means the path is not selected.
        result[decode_path(path)] = (int(line) - 1, not pattern.startswith(b"!")) if pattern else None
    return result

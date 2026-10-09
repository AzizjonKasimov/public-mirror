"""Turn a file's private version into its public version.

- Lines between a `private:start` marker line and a `private:end` marker line are dropped,
  together with the markers.
- In Markdown files, a line that links to a file or folder that is not published is dropped,
  so the public copy has no broken links and does not reveal private file names.
- Terms from the settings' replace table are rewritten, for example a client's name into
  "a client".
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote

# A marker line holds only the marker, optionally inside a comment of any common language:
# `<!-- private:start -->`, `# private:start`, `// private:end`, `<# private:end #>`, ...
# Requiring the whole line keeps prose that mentions the markers from triggering them.
MARKER = re.compile(r"^\s*(?:<!--|<#|#|//|/\*|--|;|%)?\s*private:(start|end)\s*(?:-->|#>|\*/)?\s*$", re.IGNORECASE)

MARKDOWN_SUFFIXES = (".md", ".markdown")
LINKS = (
    re.compile(r"\]\(\s*<([^>\n]+)>"),                  # [text](<target>)
    re.compile(r"\]\(\s*([^)\s]+)"),                    # [text](target "title") and images
    re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)"),   # [label]: target
    re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE),
)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")  # http:, mailto:, and Windows drives like E:


@dataclass
class Transformed:
    text: str = ""
    source_lines: list[int] = field(default_factory=list)  # source line number of each published line
    blocks: int = 0
    dropped: list[tuple[int, str]] = field(default_factory=list)  # (source line, unpublished link target)
    errors: list[tuple[int, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.blocks > 0 or bool(self.dropped)


def needs_transform(path: str, data: bytes, markers: bool, prune_links: bool) -> bool:
    return (markers and b"private:" in data.lower()) or (prune_links and path.lower().endswith(MARKDOWN_SUFFIXES))


def transform(text: str, path: str, *, markers: bool,
              is_unpublished: Callable[[str], bool] | None) -> Transformed:
    result = Transformed()
    prune = is_unpublished is not None and path.lower().endswith(MARKDOWN_SUFFIXES)
    base = posixpath.dirname(path)
    kept: list[str] = []
    open_line = 0
    for number, line in enumerate(text.split("\n"), 1):
        marker = MARKER.match(line) if markers else None
        if marker:
            if marker.group(1).lower() == "start":
                if open_line:
                    result.errors.append((number, f"private:start inside the block opened on line {open_line}"))
                else:
                    open_line = number
                    result.blocks += 1
            elif open_line:
                open_line = 0
            else:
                result.errors.append((number, "private:end without a private:start above it"))
            continue
        if open_line:
            continue
        if prune:
            target = _unpublished_link(line, base, is_unpublished)
            if target:
                result.dropped.append((number, target))
                continue
        kept.append(line)
        result.source_lines.append(number)
    if open_line:
        result.errors.append((open_line, "private:start is never closed"))
    result.text = "\n".join(kept)
    return result


def replace_terms(text: str, rules: list[tuple[re.Pattern[str], str]],
                  line_map: list[int] | None) -> tuple[str, int, list[int] | None]:
    """Apply (pattern, replacement) rules in order.

    Returns the new text, the number of replacements, and the line map, updated for matches
    that span a line break and so join lines. Replacements never contain a line break.
    """
    count = 0
    lines = line_map
    for regex, replacement in rules:
        matches = list(regex.finditer(text))
        if not matches:
            continue
        if lines is None:
            lines = list(range(1, text.count("\n") + 2))
        count += len(matches)
        for match in reversed(matches):  # from the end, so earlier line indexes stay valid
            joined = text.count("\n", match.start(), match.end())
            if joined:
                first = text.count("\n", 0, match.start())
                del lines[first + 1:first + 1 + joined]
        text = regex.sub(lambda _: replacement, text)
    return text, count, lines


def _unpublished_link(line: str, base: str, is_unpublished: Callable[[str], bool]) -> str | None:
    for pattern in LINKS:
        for match in pattern.finditer(line):
            target = link_target(match.group(1), base)
            if target and is_unpublished(target):
                return target
    return None


def link_target(raw: str, base: str) -> str | None:
    """The repo path a relative link points to, or None for web links, anchors, and paths outside the repo."""
    raw = raw.strip()
    if not raw or raw.startswith(("#", "//")) or SCHEME.match(raw):
        return None
    raw = unquote(raw.split("#", 1)[0].split("?", 1)[0])
    if not raw:
        return None
    # GitHub reads a leading slash in repo Markdown as the repo root.
    joined = raw.lstrip("/") if raw.startswith("/") else posixpath.join(base, raw)
    normal = posixpath.normpath(joined)
    if normal in (".", "..") or normal.startswith("../"):
        return None
    return normal


def unpublished_checker(all_paths: list[str], public_paths: set[str]) -> Callable[[str], bool]:
    """Answer "is this link target in the repo but not published?" for files and folders."""
    files = set(all_paths)
    all_dirs, public_dirs = _parent_dirs(files), _parent_dirs(public_paths)

    def is_unpublished(target: str) -> bool:
        target = target.rstrip("/")
        if target in files:
            return target not in public_paths
        if target in all_dirs:
            return target not in public_dirs
        return False  # a broken link or something outside git: not ours to judge

    return is_unpublished


def _parent_dirs(paths: set[str]) -> set[str]:
    dirs = set()
    for path in paths:
        parts = path.split("/")[:-1]
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return dirs

"""Build the public snapshot of a commit: pick the public files, transform them, scan the result."""

from __future__ import annotations

from dataclasses import dataclass, field

from .classify import PRIVATE, PUBLIC, Classification, classify
from .config import Config, ConfigError
from .git import GitError, Repo, run_git
from .scan import ERROR, WARNING, Finding, Scanner, is_binary, term_regex
from .transform import Transformed, needs_transform, replace_terms, transform, unpublished_checker

SYMLINK = "120000"


@dataclass(frozen=True)
class Planned:
    """One file of the public copy, and where its content comes from."""
    path: str  # the path in the public copy
    mode: str
    kind: str  # "blob", or "commit" for a submodule
    sha: str  # the source blob in this repo; empty for an included file
    size: int
    data: bytes | None = None  # the content of a file included from another repo


@dataclass
class PublicFile:
    mode: str
    sha: str  # the source object; replaced by the transformed blob once written
    path: str
    data: bytes | None = None  # set when the published content differs from the source


@dataclass
class Snapshot:
    commit: str
    classification: Classification
    files: list[PublicFile] = field(default_factory=list)
    transforms: dict[str, Transformed] = field(default_factory=dict)
    replaced: dict[str, int] = field(default_factory=dict)  # path -> number of replaced terms
    stand_ins: list[str] = field(default_factory=list)  # paths filled from the public-versions folder or an include
    findings: list[Finding] = field(default_factory=list)
    unscanned: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.level == WARNING]


def plan(repo: Repo, config: Config, commit: str) -> tuple[Classification, list[Planned], dict[str, str]]:
    """Decide every file of the public copy.

    Returns the classification, the planned public files, and the stand-ins: public paths whose
    content comes from the public-versions folder or from another repo, mapped to that source.
    """
    entries = repo.ls_tree(commit)
    present = {entry.path for entry in entries}
    result = classify([entry.path for entry in entries], config.public, config.private, config.always_private)
    planned: dict[str, Planned] = {}
    stand_ins: dict[str, str] = {}

    if config.public_versions:
        prefix = config.public_versions + "/"
        for entry in entries:
            if entry.path.startswith(prefix):
                target = entry.path[len(prefix):]
                result.status[entry.path] = PRIVATE
                result.reason[entry.path] = f"the public version of {target}"
                planned[target] = Planned(target, entry.mode, entry.kind, entry.sha, entry.size)
                stand_ins[target] = entry.path

    for target, (other, path) in config.include.items():
        if target in present or target in planned:
            raise ConfigError(f"[include] {target!r} already exists in this repo; publish one or the other")
        data = _read_other_repo(config, other, path)
        planned[target] = Planned(target, "100644", "blob", "", len(data), data)
        stand_ins[target] = f"{other}:{path}"

    for target, source in stand_ins.items():
        result.status[target], result.reason[target] = PUBLIC, f"public: content from {source}"
    planned.update({entry.path: Planned(entry.path, entry.mode, entry.kind, entry.sha, entry.size)
                    for entry in entries if result.status[entry.path] == PUBLIC and entry.path not in planned})
    return result, [planned[path] for path in sorted(planned)], stand_ins


def build(repo: Repo, config: Config, ref: str = "HEAD") -> Snapshot:
    commit = repo.resolve(f"{ref}^{{commit}}")
    if commit is None:
        raise GitError(f"{ref} is not a commit (does the repo have any commits yet?)")
    classification, planned, stand_ins = plan(repo, config, commit)
    snapshot = Snapshot(commit, classification, stand_ins=sorted(stand_ins))
    snapshot.findings += [Finding(WARNING, "", None, "pattern", f"{name} pattern {pattern!r} matches no file")
                          for name, pattern in classification.unused]

    every_path = list(classification.status)
    is_unpublished = (unpublished_checker(every_path, {item.path for item in planned})
                      if config.prune_private_links else None)
    scanner = Scanner(config.deny, config.allow, config.skip_detectors)
    replacements = [(term_regex(find), value) for find, value in config.replace.items()]
    limit = int(config.max_file_mb * 1024 * 1024)
    blobs = repo.read_blobs(item.sha for item in planned
                            if item.kind == "blob" and item.data is None and item.size <= limit)
    snapshot.findings += _csv_header_changes(repo, commit, stand_ins)

    for item in planned:
        if item.kind != "blob":  # a submodule pointer: nothing to read
            snapshot.files.append(PublicFile(item.mode, item.sha, item.path))
            continue
        if item.size > limit:
            snapshot.findings.append(Finding(ERROR, item.path, None, "size",
                                             f"{item.size / 1048576:.1f} MB is over the {config.max_file_mb:g} MB limit"))
            continue
        data = item.data if item.data is not None else blobs[item.sha]
        published = PublicFile(item.mode, item.sha, item.path, item.data)
        if is_binary(item.path, data):
            findings, scanned = scanner.scan_binary(item.path, data)
            snapshot.findings += findings
            if not scanned:
                snapshot.unscanned.append(item.path)
            snapshot.files.append(published)
            continue

        # surrogateescape keeps every byte of non-UTF-8 text intact through the round trip.
        text = data.decode("utf-8", "surrogateescape")
        line_map = None
        if item.mode != SYMLINK:  # a symlink's text is its target path: scan it, never rewrite it
            if needs_transform(item.path, data, config.private_markers, is_unpublished is not None):
                result = transform(text, item.path, markers=config.private_markers, is_unpublished=is_unpublished)
                snapshot.findings += [Finding(ERROR, item.path, line, "marker", message)
                                      for line, message in result.errors]
                if result.changed:
                    snapshot.transforms[item.path] = result
                    text, line_map = result.text, result.source_lines
            text, count, line_map = replace_terms(text, replacements, line_map)
            if count:
                snapshot.replaced[item.path] = count
            if item.path in snapshot.transforms or count:
                published.data = text.encode("utf-8", "surrogateescape")
        snapshot.findings += scanner.scan_text(item.path, text, line_map)
        snapshot.files.append(published)
    return snapshot


def write_tree(repo: Repo, snapshot: Snapshot) -> str:
    """Store the transformed files and the public tree in the repo's object database."""
    for file in snapshot.files:
        if file.data is not None:
            file.sha = repo.write_blob(file.data)
    return repo.write_tree((file.mode, file.sha, file.path) for file in snapshot.files)


def _read_other_repo(config: Config, other: str, path: str) -> bytes:
    """A committed file from another local repo, read at that repo's HEAD."""
    location = config.root / other
    proc = run_git(["cat-file", "blob", f"HEAD:{path}"], location, check=False) if location.is_dir() else None
    if proc is None or proc.returncode != 0:
        raise ConfigError(f"[include] cannot read {path!r} from the last commit of {location}")
    return proc.stdout


def _csv_header_changes(repo: Repo, commit: str, stand_ins: dict[str, str]) -> list[Finding]:
    """Warn when a public version of a CSV file no longer has the real file's columns."""
    findings = []
    for target, source in stand_ins.items():
        if not target.lower().endswith(".csv") or ":" in source:
            continue
        real = repo.resolve(f"{commit}:{target}")
        version = repo.resolve(f"{commit}:{source}")
        if real is None or version is None:
            continue
        headers = [_first_line(data) for data in repo.read_blobs([real, version]).values()]
        if len(headers) == 2 and headers[0] != headers[1]:
            findings.append(Finding(WARNING, source, 1, "columns",
                                    f"its columns differ from {target}; update the example to match"))
    return findings


def _first_line(data: bytes) -> bytes:
    return data.split(b"\n", 1)[0].rstrip(b"\r").removeprefix(b"\xef\xbb\xbf")

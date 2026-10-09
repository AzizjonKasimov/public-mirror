"""Read and check `.public-mirror.toml`, the settings file at the root of the private repo."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .scan import DETECTORS

CONFIG_NAME = ".public-mirror.toml"
AUTHOR = re.compile(r"\s*(.+?)\s*<([^<>\s]+)>\s*")

# Every key the file may hold, with its type. Unknown keys are errors: a misspelled key in a
# privacy setting must not be ignored without a word.
SCHEMA: dict[str, dict[str, type | tuple[type, ...]]] = {
    "public": {"remote": str, "branch": str, "message": str, "author": str},
    "paths": {"public": list, "private": list, "public_versions": str},
    "transform": {"private_markers": bool, "prune_private_links": bool, "replace": dict},
    "scan": {"deny": list, "deny_files": list, "allow": list, "skip_detectors": list, "max_file_mb": (int, float)},
}

TEMPLATE = """\
# public-mirror settings. This file is never published.

[public]
# The public repository: a URL, a local path, or the name of a git remote.
remote = ""
branch = "main"

[paths]
# .gitignore-style patterns. A file is published only when it matches `public` and does
# not match `private`. Files that match neither stay private; `check` lists them.
# Start patterns with "/" to anchor them to the repo root.
public = [
    # "/README.md",
    # "/src/",
]
private = [
    # "/notes/",
]

[transform.replace]
# Terms rewritten in every published text file, in order, for example:
# "Falcon Corp" = "a logistics client"

[scan]
# Words, names, or numbers that must never appear in a published file (case-insensitive).
deny = []
# Text the built-in detectors may publish anyway, as regular expressions,
# for example your public email address.
allow = []
"""


class ConfigError(Exception):
    """The settings file is missing or invalid."""


@dataclass
class Config:
    root: Path
    remote: str = ""
    branch: str = "main"
    message: str = "Update public mirror"
    author: str = ""
    public: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)
    public_versions: str = ""  # folder whose files replace or add files in the public copy
    include: dict[str, tuple[str, str]] = field(default_factory=dict)  # public path -> (other repo, path there)
    private_markers: bool = True
    prune_private_links: bool = True
    replace: dict[str, str] = field(default_factory=dict)
    deny: list[str] = field(default_factory=list)
    deny_files: list[Path] = field(default_factory=list)
    allow: list[re.Pattern[str]] = field(default_factory=list)
    skip_detectors: set[str] = field(default_factory=set)
    max_file_mb: float = 50

    @property
    def always_private(self) -> set[str]:
        """Repo paths that are never published: this settings file and deny-list files kept in the repo."""
        paths = {CONFIG_NAME}
        for file in self.deny_files:
            try:
                paths.add(file.resolve().relative_to(self.root.resolve()).as_posix())
            except ValueError:
                pass  # outside the repo
        return paths

    def author_env(self) -> dict[str, str]:
        match = AUTHOR.fullmatch(self.author)
        if not match:
            return {}
        name, email = match.groups()
        return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def write_template(root: Path) -> Path:
    path = root / CONFIG_NAME
    if path.exists():
        raise ConfigError(f"{CONFIG_NAME} already exists")
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def load(root: Path) -> Config:
    path = root / CONFIG_NAME
    if not path.exists():
        raise ConfigError(f"{CONFIG_NAME} not found in {root}. Run `public-mirror init` to create one.")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{CONFIG_NAME}: {error}") from error
    _check_schema(data)

    public, paths = data.get("public", {}), data.get("paths", {})
    transform, scan = data.get("transform", {}), data.get("scan", {})
    config = Config(root=root)
    config.remote = public.get("remote", "")
    config.branch = public.get("branch", config.branch)
    config.message = public.get("message", config.message)
    config.author = public.get("author", "")
    if config.author and not AUTHOR.fullmatch(config.author):
        raise ConfigError('[public] author must look like "Name <email>"')
    config.public = _patterns(paths, "public")
    config.private = _patterns(paths, "private")
    if "public_versions" in paths:
        config.public_versions = _repo_path("[paths] public_versions", paths["public_versions"])
    config.include = _includes(data.get("include", {}))
    config.private_markers = transform.get("private_markers", True)
    config.prune_private_links = transform.get("prune_private_links", True)
    config.replace = _replacements(transform.get("replace", {}))
    config.deny = _strings(scan, "deny")
    config.deny_files = [_resolve(root, value) for value in _strings(scan, "deny_files")]
    for file in config.deny_files:
        config.deny += _read_deny_file(file)
    # A replaced term must not survive anywhere, for example inside a Word file, which is not rewritten.
    config.deny += [term.strip() for term in config.replace]
    config.allow = [_regex(value) for value in _strings(scan, "allow")]
    config.skip_detectors = set(_strings(scan, "skip_detectors"))
    unknown = config.skip_detectors - DETECTORS.keys()
    if unknown:
        raise ConfigError(f"[scan] skip_detectors: unknown detector(s) {sorted(unknown)}; known: {sorted(DETECTORS)}")
    config.max_file_mb = scan.get("max_file_mb", config.max_file_mb)
    return config


def _check_schema(data: dict) -> None:
    for section, values in data.items():
        if section not in SCHEMA and section != "include":
            raise ConfigError(f"unknown section [{section}] in {CONFIG_NAME}; known: {sorted([*SCHEMA, 'include'])}")
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        if section == "include":  # free-form keys, checked by _includes
            continue
        for key, value in values.items():
            expected = SCHEMA[section].get(key)
            if expected is None:
                raise ConfigError(f"unknown key [{section}] {key}; known: {sorted(SCHEMA[section])}")
            if not isinstance(value, expected) or (expected is not bool and isinstance(value, bool)):
                raise ConfigError(f"[{section}] {key} has the wrong type")


def _strings(table: dict, key: str) -> list[str]:
    values = table.get(key, [])
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ConfigError(f"{key}: every entry must be a non-empty string")
    return [value.strip() for value in values]


def _patterns(table: dict, key: str) -> list[str]:
    patterns = _strings(table, key)
    for pattern in patterns:
        if "\n" in pattern or pattern.startswith("#"):
            raise ConfigError(f"[paths] {key}: {pattern!r} is not a usable pattern (write \\# for a leading #)")
    return patterns


def _repo_path(where: str, value: str) -> str:
    """A relative path inside the repo, written with forward slashes."""
    path = value.replace("\\", "/").strip("/")
    if not path or ":" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ConfigError(f"{where}: {value!r} must be a relative path inside the repo")
    return path


def _includes(table: dict) -> dict[str, tuple[str, str]]:
    includes = {}
    for target, source in table.items():
        if not isinstance(source, dict) or set(source) != {"repo", "path"} \
                or not all(isinstance(value, str) and value.strip() for value in source.values()):
            raise ConfigError(f'[include] {target!r} must look like {{ repo = "../other-repo", path = "file.md" }}')
        includes[_repo_path("[include]", target)] = (source["repo"], _repo_path("[include] path", source["path"]))
    return includes


def _replacements(table: dict) -> dict[str, str]:
    for find, value in table.items():
        if not find.strip() or not isinstance(value, str) or "\n" in value:
            raise ConfigError(f"[transform.replace] {find!r}: use a non-empty term and a one-line replacement")
    return dict(table)


def _regex(value: str) -> re.Pattern[str]:
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as error:
        raise ConfigError(f"[scan] allow: {value!r} is not a valid regular expression: {error}") from error


def _resolve(root: Path, value: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else root / path


def _read_deny_file(path: Path) -> list[str]:
    # A missing deny list would quietly weaken the scan, so it is an error rather than a warning.
    if not path.is_file():
        raise ConfigError(f"[scan] deny_files: {path} does not exist")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]

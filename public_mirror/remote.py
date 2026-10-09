"""The public repository: what it already has, publishing to it, and pulling changes made there.

Every commit public-mirror makes carries a `Public-Mirror-Source:` trailer. Commits without it
were made in the public repo directly, for example a merged pull request. Publishing over
them would silently undo them, so `publish` refuses until they are pulled into the private repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config, ConfigError
from .git import GitError, Repo

TRAILER = "Public-Mirror-Source"
TRAILER_LINE = re.compile(rf"^{TRAILER}:\s*\S+\s*$", re.MULTILINE)
PULLED_REF = "refs/public-mirror/pulled"


def tracking_ref(config: Config) -> str:
    return f"refs/public-mirror/{config.branch}"


@dataclass
class PublicState:
    tip: str | None = None
    last_mirror: str | None = None  # the newest commit public-mirror made
    foreign: list[tuple[str, str]] = field(default_factory=list)  # (sha, subject) made directly in the public repo

    def foreign_pending(self, repo: Repo) -> bool:
        """Foreign commits exist and `pull` has not brought them in yet."""
        return bool(self.foreign) and repo.resolve(PULLED_REF) != self.tip


def require_remote(config: Config) -> str:
    if not config.remote:
        raise ConfigError("set [public] remote in .public-mirror.toml first")
    return config.remote


def fetch(repo: Repo, config: Config) -> PublicState:
    remote = require_remote(config)
    if repo.remote_branch(remote, config.branch) is None:
        return PublicState()
    repo.git("fetch", "--quiet", "--no-tags", remote, f"+refs/heads/{config.branch}:{tracking_ref(config)}")
    state = PublicState(tip=repo.resolve(tracking_ref(config)))
    for sha, message in repo.first_parent_log(state.tip):
        if TRAILER_LINE.search(message):
            state.last_mirror = sha
            break
        state.foreign.append((sha, message.strip().splitlines()[0] if message.strip() else "(no message)"))
    return state


def commit_and_push(repo: Repo, config: Config, tree: str, parent: str | None, source: str, message: str) -> str:
    body = f"{message.strip()}\n\n{TRAILER}: {source}\n"
    commit = repo.commit_tree(tree, [parent] if parent else [], body, env=config.author_env())
    # A plain push: if the public branch moved since the fetch, git rejects it instead of overwriting.
    repo.git("push", "--quiet", config.remote, f"{commit}:refs/heads/{config.branch}")
    repo.git("update-ref", tracking_ref(config), commit)
    return commit


def pull(repo: Repo, state: PublicState, skip: set[str]) -> tuple[bool, str, list[str]]:
    """Apply the foreign public commits to the working tree with a 3-way merge.

    Returns (conflicts?, git's output, skipped paths). The base is the last mirror commit, so lines
    that exist only in the private repo, such as private blocks, merge like local changes. Paths in
    `skip` are left out: their public content comes from a public version or another repo, so a
    change to them must not land on the private file at the same path.
    """
    changed = repo.text("-c", "core.quotePath=false", "diff", "--name-only", state.last_mirror, state.tip)
    skipped = sorted(set(changed.splitlines()) & skip)
    pathspec = ["--", ".", *(f":(exclude,literal){path}" for path in skipped)]
    patch = repo.git("diff", "--binary", "--full-index", state.last_mirror, state.tip, *pathspec)
    output = ""
    conflicts = False
    if patch.strip():
        proc = repo.try_git("apply", "--3way", "--whitespace=nowarn", input=patch)
        output = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        conflicts = "with conflicts" in output
        if proc.returncode != 0 and not conflicts:
            raise GitError(f"git apply failed: {output}")
    repo.git("update-ref", PULLED_REF, state.tip)
    return conflicts, output, skipped

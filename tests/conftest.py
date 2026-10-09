import subprocess
from pathlib import Path

import pytest

from public_mirror.cli import main


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Keep the user's git settings (hooks, signing, line-ending conversion) out of the tests."""
    config = tmp_path_factory.mktemp("home") / "gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n",
                      encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          encoding="utf-8").stdout.strip()


class Project:
    """A private repo, plus an empty bare repo that stands in for the public GitHub repo."""

    def __init__(self, root: Path):
        self.root = root
        self.private = root / "private"
        self.public = root / "public.git"
        self.private.mkdir()
        git(self.private, "init", "-q", "-b", "main")
        git(root, "init", "-q", "--bare", "-b", "main", str(self.public))

    def write(self, path: str, content: str | bytes) -> None:
        file = self.private / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def config(self, body: str) -> None:
        self.write(".public-mirror.toml", f'[public]\nremote = "{self.public.as_posix()}"\n\n{body}')

    def commit(self, message: str = "Change") -> None:
        git(self.private, "add", "-A")
        git(self.private, "commit", "-q", "-m", message)

    def run(self, *args: str) -> int:
        return main(["-C", str(self.private), *args])

    def public_head(self) -> str | None:
        proc = subprocess.run(["git", "rev-parse", "--verify", "-q", "main"], cwd=self.public,
                              capture_output=True, encoding="utf-8")
        return proc.stdout.strip() or None

    def public_files(self) -> dict[str, bytes]:
        names = git(self.public, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "main").splitlines()
        return {name: subprocess.run(["git", "show", f"main:{name}"], cwd=self.public, capture_output=True,
                                     check=True).stdout for name in names}


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)

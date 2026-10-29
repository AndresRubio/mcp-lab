"""`scripts/review-package.sh` describes a branch: its commits and the diff those commits made."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review-package.sh"
GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
# The developer's global config (hooks path, commit signing) must not reach the temp repo.
GIT_ENV = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")


def test_package_describes_only_the_branch_commits_when_the_base_has_moved_on(tmp_path: Path) -> None:
    # A review package for `feature` must not show main's later work as deletions.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "shared.txt", "shared\n")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "feature.txt", "feature work\n")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "later.txt", "landed on main after the branch\n")

    out = tmp_path / "package.md"
    subprocess.run(
        [str(SCRIPT), "main", "feature", str(out)], cwd=repo, env=GIT_ENV, check=True, capture_output=True
    )
    package = out.read_text(encoding="utf-8")

    assert "add feature.txt" in package
    assert "add later.txt" not in package
    assert "feature.txt" in package
    assert "+feature work" in package
    assert "later.txt" not in package
    assert "landed on main" not in package


def test_package_warns_when_the_branch_and_base_have_more_than_one_merge_base(tmp_path: Path) -> None:
    # After main and feature each merged the other, `base...head` has two merge bases and git picks
    # one, so the diff can show work main already has. The package must say so where a reviewer reads.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "shared.txt", "shared\n")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f1.txt", "first feature work\n")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "m1.txt", "main work\n")
    m1 = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "merge", "-q", "--no-edit", "feature")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", m1)
    _commit(repo, "f2.txt", "second feature work\n")
    assert len(_git(repo, "merge-base", "--all", "main", "feature").split()) == 2

    out = tmp_path / "package.md"
    subprocess.run(
        [str(SCRIPT), "main", "feature", str(out)], cwd=repo, env=GIT_ENV, check=True, capture_output=True
    )
    package = out.read_text(encoding="utf-8")

    assert "add f2.txt" in package
    assert "2 merge bases" in package

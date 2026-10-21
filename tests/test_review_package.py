"""`scripts/review-package.sh` describes a branch: its commits and the diff those commits made."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review-package.sh"
GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
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
    subprocess.run([str(SCRIPT), "main", "feature", str(out)], cwd=repo, check=True, capture_output=True)
    package = out.read_text()

    assert "add feature.txt" in package
    assert "add later.txt" not in package
    assert "feature.txt" in package
    assert "+feature work" in package
    assert "later.txt" not in package
    assert "landed on main" not in package

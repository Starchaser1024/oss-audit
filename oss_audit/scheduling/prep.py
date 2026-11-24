"""Create an isolated Git worktree without switching or resetting the primary clone."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from oss_audit.core.common import git
from oss_audit.core.paths import WorkspacePaths


def prepare(
    repo: str,
    branch: str,
    base: str = "main",
    target: str | Path | None = None,
    fetch: bool = True,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Create a fresh detached/branch worktree under ``<workspace>/worktrees``."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Expected owner/repo")
    paths = WorkspacePaths.from_workspace(workspace)
    clone = paths.repos_dir / repo.split("/")[-1]
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", f"https://github.com/{repo}.git", str(clone)],
            check=True,
        )
    remote = git(clone, "remote", "get-url", "origin").removesuffix(".git").lower()
    if not (remote.endswith("/" + repo.lower()) or remote.endswith(":" + repo.lower())):
        raise ValueError("Main clone origin differs from requested repository")
    subprocess.run(["git", "check-ref-format", "--branch", branch], check=True, capture_output=True)
    if git(clone, "branch", "--list", branch):
        raise ValueError("Branch already exists; inspect and explicitly resume its original worktree")
    if fetch:
        subprocess.run(["git", "-C", str(clone), "fetch", "origin", base], check=True)
        revision = git(clone, "rev-parse", "FETCH_HEAD")
    else:
        revision = git(clone, "rev-parse", base)
    worktrees_root = paths.worktrees_dir.resolve()
    worktrees_root.mkdir(parents=True, exist_ok=True)
    target_path = Path(
        target or worktrees_root / (repo.replace("/", "--") + "--" + branch.replace("/", "-"))
    ).resolve()
    if not target_path.is_relative_to(worktrees_root) or target_path.exists():
        raise ValueError(f"Use a new path under {worktrees_root}")
    subprocess.run(
        ["git", "-C", str(clone), "worktree", "add", "-b", branch, str(target_path), revision],
        check=True,
    )
    return {"checkout": str(target_path), "base_sha": revision, "branch": branch}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--target")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            prepare(args.repo, args.branch, args.base, args.target, not args.no_fetch, args.workspace),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

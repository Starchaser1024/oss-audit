"""Shared atomic I/O and read-only Git / GitHub API primitives."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oss_audit.core.paths import REPO_ROOT, resolve_workspace

ROOT: Path = REPO_ROOT


def get_workspace(workspace: str | Path | None = None) -> Path:
    return resolve_workspace(workspace)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def git(checkout: str | Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if proc.returncode:
        raise ValueError(proc.stderr.decode("utf-8", "replace")[:800])
    return proc.stdout.decode("utf-8", "replace").strip()


def gh_get(endpoint: str) -> Any:
    """Perform a strictly read-only GET query against api.github.com via ``gh api``."""
    if endpoint.startswith(("http:", "https:")):
        if not endpoint.startswith("https://api.github.com/"):
            raise ValueError("Only api.github.com GET endpoints are supported")
    elif not endpoint.startswith(("repos/", "search/", "users/", "user")):
        raise ValueError("Unsupported API endpoint")
    proc = subprocess.run(
        ["gh", "api", "--method", "GET", endpoint],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip()[:600])
    return json.loads(proc.stdout)


def gh_pages(endpoint: str, max_pages: int = 30) -> list[Any]:
    """Return a complete paginated list; exhaustion or errors never masquerade as empty."""
    result: list[Any] = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in endpoint else "?"
        rows = gh_get(f"{endpoint}{sep}per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("Expected paginated GitHub list")
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise RuntimeError("Pagination budget exceeded; completeness UNKNOWN")


def candidate_id(repo: str, cause: str, scope: str) -> str:
    return repo.lower().replace("/", "--") + "--" + json_digest([cause.strip().lower(), scope])[:16]

"""Cross-platform dynamic repository and workspace path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def resolve_workspace(workspace: str | Path | None = None) -> Path:
    """Resolve the active workspace directory dynamically.

    Resolution order:
    1. Explicit ``workspace`` argument (e.g. ``--workspace`` CLI flag)
    2. ``OSS_AUDIT_HOME`` environment variable
    3. Repository root enclosing the ``oss_audit`` package
    """
    if workspace is not None and str(workspace).strip():
        return Path(workspace).expanduser().resolve()
    env_home = os.environ.get("OSS_AUDIT_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return REPO_ROOT


@dataclass(frozen=True)
class WorkspacePaths:
    """Typed path layout rooted at an active ``oss-audit`` workspace."""

    root: Path

    @classmethod
    def from_workspace(cls, workspace: str | Path | None = None) -> WorkspacePaths:
        return cls(root=resolve_workspace(workspace))

    @property
    def config_file(self) -> Path:
        candidate = self.root / "config" / "pipeline.json"
        if candidate.is_file():
            return candidate
        return REPO_ROOT / "config" / "pipeline.json"

    @property
    def policy_file(self) -> Path:
        candidate = self.root / "policies" / "repos.json"
        if candidate.is_file():
            return candidate
        return REPO_ROOT / "policies" / "repos.json"

    @property
    def repos_inventory_file(self) -> Path:
        candidate = self.root / "repos.yaml"
        if candidate.is_file():
            return candidate
        return REPO_ROOT / "repos.yaml"

    @property
    def ledger_file(self) -> Path:
        candidate = self.root / "ledger.json"
        if candidate.is_file():
            return candidate
        return REPO_ROOT / "ledger.json"

    @property
    def queue_db(self) -> Path:
        return self.root / "queue" / "pipeline.db"

    @property
    def repos_dir(self) -> Path:
        return self.root / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def evidence_journal_dir(self) -> Path:
        return self.evidence_dir / "journal"

    @property
    def scratch_dir(self) -> Path:
        return self.root / "scratch"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def report_dir(self) -> Path:
        return self.root / "report"

    @property
    def audit_dir(self) -> Path:
        return self.root / "audit"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"


def fixtures_dir() -> Path:
    """Return the checked-in test fixtures directory."""
    return REPO_ROOT / "tests" / "fixtures"

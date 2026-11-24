"""Pipeline configuration loading with safe defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oss_audit.core.paths import WorkspacePaths


def load_pipeline_config(
    path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Load ``config/pipeline.json`` and populate bounded scheduler defaults."""
    target = Path(path) if path is not None else WorkspacePaths.from_workspace(workspace).config_file
    cfg: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    cfg.setdefault("read_workers", 4)
    cfg.setdefault("heavy_workers", 2)
    cfg.setdefault("review_workers", 1)
    cfg.setdefault("repos", 2)
    cfg.setdefault("max_active", 40)
    cfg.setdefault("daily_research", 8)
    cfg.setdefault("max_task_minutes", 25)
    cfg.setdefault("cooldown_no_finding_hours", 72)
    cfg.setdefault("resources", {})
    cfg.setdefault("publication_quota", {"daily_new_contributions": 2})
    cfg.setdefault("lease", {})
    cfg["lease"].setdefault("ttl_seconds", 120)
    cfg["lease"].setdefault("heartbeat_seconds", 30)
    cfg["lease"].setdefault("quarantine_seconds", 60)
    cfg.setdefault("audit", {})
    cfg["audit"].setdefault("max_groups", 40)
    cfg["audit"].setdefault("timeout_seconds", 300)
    cfg.setdefault("agent", {})
    return cfg

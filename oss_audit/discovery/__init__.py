"""Repository discovery, external contributor readiness, reachability, and shadow scoring."""

from oss_audit.discovery.discover import assess, discover, module_tasks, repo_names
from oss_audit.discovery.reach import find_callers
from oss_audit.discovery.score import rank

__all__ = [
    "assess",
    "discover",
    "find_callers",
    "module_tasks",
    "rank",
    "repo_names",
]

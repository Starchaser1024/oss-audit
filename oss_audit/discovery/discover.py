"""Discover, assess, and queue bounded local module audits without automatic cloning or publishing."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from oss_audit.core.common import candidate_id, gh_get, git, utcnow, write_json
from oss_audit.core.paths import WorkspacePaths

SEARCHES = [
    "topic:ai-agents language:Python archived:false stars:>500",
    "topic:mcp language:Python archived:false stars:>500",
]


def repo_names(workspace: str | Path | None = None) -> list[str]:
    """Load discovery repository names from ``repos.json`` (or legacy ``repos.yaml``)."""
    inventory = WorkspacePaths.from_workspace(workspace).repos_inventory_file
    text = inventory.read_text(encoding="utf-8")
    try:
        return [r["name"] for r in json.loads(text)["repos"]]
    except ValueError:
        return re.findall(r"^\s*- name:\s*([^\s#]+)", text, re.M)


def discover(
    limit: int = 40,
    workspace: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Combine local inventory with read-only GitHub topic searches."""
    names = {r.lower(): {"repo": r, "source": "local_inventory"} for r in repo_names(workspace)}
    snapshots: list[dict[str, Any]] = []
    for query in SEARCHES:
        try:
            raw = gh_get(
                "search/repositories?q=" + quote(query) + "&sort=updated&order=desc&per_page=100&page=1"
            )
            snapshots.append(
                {
                    "query": query,
                    "checked_at": utcnow(),
                    "total_count": raw["total_count"],
                    "incomplete_results": raw.get("incomplete_results", True),
                    "sample_size": len(raw["items"]),
                    "pages": [1],
                }
            )
            for row in raw["items"]:
                if row.get("archived") or row.get("fork"):
                    continue
                key = row["full_name"].lower()
                names.setdefault(key, {"repo": row["full_name"], "source": "github_search", "query": query})
        except (OSError, ValueError, RuntimeError) as exc:
            snapshots.append({"query": query, "status": "UNKNOWN", "error": str(exc)})
    return list(names.values())[:limit], snapshots


def assess(row: dict[str, Any], workspace: str | Path | None = None) -> dict[str, Any]:
    """Assess upstream maintenance signals, policy snapshot, and local checkout state."""
    from oss_audit.policy.router import load_policy

    paths = WorkspacePaths.from_workspace(workspace)
    repo = row["repo"]
    result = dict(row, assessed_at=utcnow())
    try:
        meta = gh_get(f"repos/{repo}")
        repo = meta["full_name"]
        result.update(
            repo=repo,
            default_branch=meta["default_branch"],
            language=meta.get("language"),
            archived=meta["archived"],
        )
        if meta["archived"] or meta["disabled"] or meta["fork"]:
            return dict(result, decision="no_fit", reason="archived/disabled/fork", cooldown_days=30)
        pulls = gh_get(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=60")
        merged = [p for p in pulls if p.get("merged_at")]
        associations: dict[str, int] = {}
        for pr in merged:
            key = pr.get("author_association", "UNKNOWN")
            associations[key] = associations.get(key, 0) + 1
        result["maintenance_sample"] = {
            "kind": "60 most recently updated closed PRs; not an acceptance probability",
            "closed_sample": len(pulls),
            "merged_sample": len(merged),
            "author_associations": associations,
            "urls": [p["html_url"] for p in pulls[:6]],
        }
        policy = load_policy(repo, paths.policy_file)
        result["policy"] = policy
        choices = [
            paths.repos_dir / repo.split("/")[-1],
            paths.repos_dir / repo.lower().replace("/", "--"),
        ]
        checkout = next((p for p in choices if (p / ".git").exists()), None)
        if checkout:
            remote = git(checkout, "remote", "get-url", "origin").removesuffix(".git").lower()
            canonical = repo.lower()
            aliases = (
                {canonical, "modelscope/agentscope"}
                if canonical == "agentscope-ai/agentscope"
                else {canonical}
            )
            if not any(remote.endswith("/" + name) or remote.endswith(":" + name) for name in aliases):
                return dict(result, decision="hold", reason="local checkout remote mismatch")
            result.update(
                checkout=str(checkout),
                base_sha=git(checkout, "rev-parse", "HEAD"),
                decision="read_ready",
            )
        else:
            result.update(
                decision="needs_checkout",
                reason="No existing checkout; use `oss-audit prep` in a separate explicit run",
            )
        return result
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return dict(result, decision="UNKNOWN", reason=str(exc))


def module_tasks(rows: list[dict[str, Any]], max_modules: int = 2) -> list[dict[str, Any]]:
    """Build bounded module audit task envelopes for read-ready checkouts."""
    tasks: list[dict[str, Any]] = []
    for row in rows:
        if row.get("decision") != "read_ready":
            continue
        checkout = Path(row["checkout"])
        directories: list[str] = []
        for name in ("models", "tools", "storage", "memory", "providers", "serialization"):
            for path in checkout.rglob(name):
                if path.is_dir() and not any(
                    part in {".git", ".venv", "node_modules", "tests", "docs"} for part in path.parts
                ):
                    directories.append(path.relative_to(checkout).as_posix())
                if len(directories) >= max_modules:
                    break
            if len(directories) >= max_modules:
                break
        for scope in directories or ["."]:
            tasks.append(
                {
                    "candidate_id": candidate_id(row["repo"], "module-contract-audit", scope),
                    "repo": row["repo"],
                    "root_cause": "module-contract-audit",
                    "scope": scope,
                    "kind": "audit",
                    "checkout": row["checkout"],
                    "base_sha": row["base_sha"],
                    "payload": {
                        "purpose": "investigate",
                        "policy": row["policy"],
                        "budget_seconds": 120,
                    },
                }
            )
    return tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None, help="Workspace root (or OSS_AUDIT_HOME)")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--assess", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--queue-output", type=Path, default=None)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 0 <= args.assess <= args.limit or not 1 <= args.workers <= 4:
        parser.error("limit 1..100, assess 0..limit, workers 1..4")
    paths = WorkspacePaths.from_workspace(args.workspace)
    output = args.output or (paths.state_dir / "portfolio.json")
    queue_output = args.queue_output or (paths.root / "queue" / "discovered.json")
    rows, snapshots = discover(args.limit, args.workspace)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        assessed = list(pool.map(lambda r: assess(r, args.workspace), rows[: args.assess]))
    tasks = module_tasks(assessed)
    report = {
        "checked_at": utcnow(),
        "discovered": rows,
        "searches": snapshots,
        "assessed": assessed,
        "queue_size": len(tasks),
        "publication_authorized": False,
    }
    write_json(output, report)
    write_json(queue_output, tasks)
    print(
        json.dumps(
            {
                "discovered": len(rows),
                "assessed": len(assessed),
                "audit_tasks": len(tasks),
                "unknown": sum(r["decision"] == "UNKNOWN" for r in assessed),
                "output": str(output),
                "queue": str(queue_output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

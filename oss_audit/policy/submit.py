"""Fail-closed publication preflight gate. Performs zero GitHub writes, even with ``--yes``."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from oss_audit.core.common import digest, gh_get, gh_pages, git, json_digest, read_json, utcnow, write_json
from oss_audit.core.paths import WorkspacePaths
from oss_audit.evidence import runner as evidence_runner
from oss_audit.policy.claim_collision import search_claim_collision
from oss_audit.policy.claimcheck import check
from oss_audit.policy.router import canonical_repo, load_policy, route


def inspect(
    repo: str,
    bundle_path: str | Path,
    branch: str,
    title: str,
    body_path: str | Path,
    issue: int | None = None,
    actor: str | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Validate execution bundle, clean Git head, claim/collision status, and upstream policy."""
    checked = evidence_runner.validate_bundle(bundle_path, workspace=workspace)
    if not checked["ok"]:
        return {"ok": False, "route": "hold", "reasons": checked["errors"], "phase": "evidence"}
    bundle = read_json(bundle_path)
    card = read_json(bundle["contract"])
    green = read_json(bundle["receipts"]["green"])
    checkout = Path(green["spec"]["checkout"])
    review = read_json(bundle["review"])
    if canonical_repo(card["repo"]) != canonical_repo(repo):
        return {"ok": False, "reasons": ["repo_does_not_match_evidence"]}
    if git(checkout, "rev-parse", branch) != green["after"]["head"] or git(checkout, "status", "--porcelain"):
        return {"ok": False, "reasons": ["branch_or_clean_checkout_does_not_match_tested_head"]}
    publication = {"title_sha256": json_digest(title), "body_sha256": digest(body_path)}
    if review.get("publication") != publication:
        return {"ok": False, "reasons": ["title_and_body_not_reviewed_for_this_revision"]}
    body = Path(body_path).read_text(encoding="utf-8")
    if not title.strip() or not body.strip():
        return {"ok": False, "reasons": ["empty_publication_text"]}
    if issue and not re.search(rf"(?i)\b(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#{issue}\b", body):
        return {"ok": False, "reasons": ["existing_issue_needs_closing_link"]}
    resolved_actor = actor or gh_get("user")["login"]
    open_prs = gh_pages(f"repos/{repo}/pulls?state=open")
    ours = [p for p in open_prs if p.get("user", {}).get("login", "").lower() == resolved_actor.lower()]
    cap = 1 if canonical_repo(repo) == "deepset-ai/haystack" else 3
    if len(ours) >= cap:
        return {
            "ok": False,
            "route": "hold",
            "reasons": [f"repository_open_pr_capacity:{len(ours)}/{cap}"],
            "resume_when": ["existing PR closes or policy/capacity is explicitly revised"],
        }
    if any(p["head"]["ref"] == branch for p in ours):
        return {
            "ok": False,
            "route": "hold",
            "reasons": ["branch_already_has_open_pr; inspect before update"],
        }
    if issue:
        claim = check(repo, issue, actor=resolved_actor)
        raw = gh_get(f"repos/{repo}/issues/{issue}")
        if "pull_request" in raw:
            return {"ok": False, "route": "hold", "reasons": ["issue_argument_is_a_pull_request"]}
        labels = [x["name"] for x in raw["labels"]]
        assignees = [x["login"] for x in raw["assignees"]]
    else:
        queries = card.get("claim_collision_queries") or card.get("collision_queries") or []
        claim = search_claim_collision(repo, queries, client=gh_get)
        labels, assignees = [], []
    ctx = {
        "claim": claim["status"],
        "issue_number": issue,
        "labels": labels,
        "assignees": assignees,
        "own_open_pr_count": len(ours),
        "actor": resolved_actor,
        "open_pr_count": 0 if claim["status"] == "FREE" else 1 if claim["status"] == "TAKEN" else 0,
        "checked_at": utcnow(),
    }
    candidate = {
        "defect_proven": True,
        "fix_verified": True,
        "design_uncertainties": card.get("design_uncertainties", []),
        "contribution_type": "bugfix",
        "report_only": False,
    }
    routing = route(candidate, load_policy(repo, workspace=workspace), ctx)
    return {
        "ok": routing["pr_allowed"],
        **routing,
        "checked_at": utcnow(),
        "repo": repo,
        "branch": branch,
        "head_sha": green["after"]["head"],
        "candidate_id": card["candidate_id"],
        "title": title,
        "body_file": str(Path(body_path).resolve()),
        "publication": publication,
        "evidence": checked,
        "claim_collision": claim,
        "publication_authorized": False,
        "next_step": "Review this plan; publishing is a separate explicitly authorized operation",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--bundle", help="Required evidence bundle; unverified calls are rejected")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--actor")
    parser.add_argument("--base", default="main")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Unsupported in this read-only pipeline")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)
    output = args.output or (paths.state_dir / "publication-plan.json")
    if args.yes:
        result: dict[str, Any] = {
            "ok": False,
            "reasons": ["publication_disabled_this_revision"],
            "writes_performed": 0,
        }
    elif not args.bundle:
        result = {
            "ok": False,
            "reasons": ["contract_execution_and_independent_review_bundle_required"],
            "writes_performed": 0,
        }
    else:
        try:
            result = inspect(
                args.repo,
                args.bundle,
                args.branch,
                args.title,
                args.body_file,
                args.issue,
                args.actor,
                args.workspace,
            )
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            result = {"ok": False, "route": "hold", "reasons": [f"UNKNOWN:{exc}"], "writes_performed": 0}
    write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Scan upstream open issues or local source trees for triage and AST asymmetry leads."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from oss_audit.analyzers.contract_audit import audit_repo
from oss_audit.analyzers.guard_asymmetry import (
    scan_async_guard_asymmetry,
    scan_hardcoded_from_dict,
    scan_sync_async_divergence,
)
from oss_audit.core.paths import WorkspacePaths
from oss_audit.policy.claimcheck import check

NONFIXABLE_TITLE = re.compile(
    r"^\s*(\[?(rfc|proposal|discussion|question|feature|enhancement|docs?|idea)\]?)\b"
    r"|^\s*(rfc|proposal|discussion)\b",
    re.I,
)
STRONG_EVIDENCE = [
    (re.compile(r"Traceback \(most recent call last\)"), 5),
    (re.compile(r"\b[\w/]+\.(py|ts|tsx|js|go|rs):\d+\b"), 4),
    (re.compile(r'^\s*File "[^"]+", line \d+', re.M), 3),
    (re.compile(r"\b(Error|Exception|AttributeError|TypeError|KeyError|ValueError|RuntimeError)\b"), 2),
]
WEAK_EVIDENCE = [
    (re.compile(r"steps to reproduce|reproduction|repro\b|minimal repro", re.I), 3),
    (re.compile(r"expected( behavior)?:.*\n.*(actual|observed)", re.I | re.S), 3),
    (re.compile(r"```"), 1),
    (re.compile(r"\b(should|instead|but|however)\b", re.I), 1),
]
BUG_LABEL_RE = re.compile(r"^(bug|type/bug|kind/bug|bugfix|regression|crash)$", re.I)
SKIP_LABEL_RE = re.compile(
    r"^(good first issue|help wanted|duplicate|wontfix|invalid|stale|question|"
    r"needs-design|design|rfc|epic|tracking)$",
    re.I,
)


def _gh_json(*args: str) -> Any:
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "null")


def load_inventory(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError as exc:
        raise RuntimeError("repos.yaml is neither JSON-compatible YAML nor readable via PyYAML") from exc


def score_issue(issue: dict[str, Any], cfg: dict[str, Any], now: datetime) -> tuple[int, list[str]]:
    """Compute a deterministic discovery triage score from issue diagnostics."""
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    labels = [label["name"] for label in issue.get("labels", [])]
    reasons: list[str] = []
    score = 0

    if any(BUG_LABEL_RE.match(label) for label in labels):
        score += 3
        reasons.append("bug_label +3")
    if any(SKIP_LABEL_RE.match(label) for label in labels):
        score -= 6
        reasons.append("non_bugfix_label -6")

    for rx, weight in STRONG_EVIDENCE:
        if rx.search(body):
            score += weight
            reasons.append(f"strong_evidence({rx.pattern[:18]}) +{weight}")

    weak = sum(weight for rx, weight in WEAK_EVIDENCE if rx.search(body))
    if weak:
        capped = min(weak, 5)
        score += capped
        reasons.append(f"reproduction_signal +{capped}")

    paths = set(re.findall(r"\b[\w/]+\.(?:py|ts|tsx|js|go|rs)\b", body))
    in_scope = [p for p in paths if any(p.startswith(pre) or pre in p for pre in cfg.get("paths", []))]
    if paths:
        bonus = min(len(paths), 4)
        score += bonus
        reasons.append(f"named_files({len(paths)}) +{bonus}")
    if in_scope:
        score += 2
        reasons.append("priority_path_match +2")

    ncomments = issue.get("comments", 0)
    if isinstance(ncomments, list):
        ncomments = len(ncomments)
    if ncomments == 0:
        score += 2
        reasons.append("zero_comments +2")
    elif ncomments <= 2:
        score += 1
        reasons.append("low_comments +1")
    elif ncomments >= 5:
        score -= 2
        reasons.append("high_comments -2")

    age = now - datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
    if age < timedelta(hours=36):
        score += 1
        reasons.append("recent +1")

    if len(body) < 200:
        score -= 4
        reasons.append("short_body -4")
    elif len(body) > 1500:
        score += 1
        reasons.append("detailed_body +1")

    if NONFIXABLE_TITLE.search(title):
        score -= 8
        reasons.append("non_defect_title -8")

    return score, reasons


def scan_repo(repo: str, cfg: dict[str, Any], days: int, limit: int, now: datetime) -> list[dict[str, Any]]:
    """Fetch open issues and retain only unclaimed (FREE) defect leads."""
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _gh_json(
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit * 4),
        "--search",
        f"created:>={since} sort:created-desc",
        "--json",
        "number,title,body,labels,comments,createdAt,updatedAt,assignees,url,author",
    )
    stats = {"raw": len(raw or []), "assigned": 0, "nonfixable": 0, "claimed": 0, "unknown": 0}
    out: list[dict[str, Any]] = []
    for issue in raw or []:
        if issue.get("assignees"):
            stats["assigned"] += 1
            continue
        if NONFIXABLE_TITLE.search(issue["title"]):
            stats["nonfixable"] += 1
            continue
        claim = check(repo, issue["number"])
        if claim["status"] != "FREE":
            key = "unknown" if claim["status"] == "UNKNOWN" else "claimed"
            stats[key] = stats.get(key, 0) + 1
            continue
        pts, reasons = score_issue(issue, cfg, now)
        out.append(
            {
                "repo": repo,
                "number": issue["number"],
                "title": issue["title"],
                "url": issue["url"],
                "author": issue["author"]["login"],
                "labels": [label["name"] for label in issue["labels"]],
                "comments": issue["comments"],
                "createdAt": issue["createdAt"],
                "score": pts,
                "decision": "investigate",
                "quality_verified": False,
                "claimcheck": claim,
                "reasons": reasons,
                "body_chars": len(issue.get("body") or ""),
                "policy_source": "policies/repos.json",
            }
        )
    print(f"  stats: {stats}", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run static AST analyzers on a local source tree or scan open upstream issues "
            "(scores are discovery hints only; FREE requires complete claimcheck)."
        )
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Repository filter (for --mode issues) or local source path (for AST modes)",
    )
    parser.add_argument(
        "--mode",
        choices=["issues", "contracts", "async-guards", "sync-async", "from-dict"],
        default="issues",
        help="Analysis mode (default: issues)",
    )
    parser.add_argument("--days", type=int, default=None, help="Override lookback window in days")
    parser.add_argument("--max", type=int, default=40, help="Max leads for --mode contracts")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.mode != "issues":
        source_root = Path(args.target or ".").resolve()
        if not source_root.is_dir():
            parser.error(f"Source directory does not exist: {source_root}")
        if args.mode == "contracts":
            payload: Any = audit_repo(source_root, max_groups=args.max)
        elif args.mode == "async-guards":
            payload = scan_async_guard_asymmetry(source_root)
        elif args.mode == "sync-async":
            payload = scan_sync_async_divergence(source_root)
        else:
            payload = scan_hardcoded_from_dict(source_root)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0

    paths = WorkspacePaths.from_workspace(args.workspace)
    conf = load_inventory(paths.repos_inventory_file)
    defaults = conf.get("defaults", {})
    now = datetime.now(timezone.utc)
    report_dir = args.output or paths.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = paths.ledger_file
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"seen": {}}
    handled = set(ledger.get("seen", {})) | set(ledger.get("claimed", {}))

    all_rows: list[dict[str, Any]] = []
    for cfg in conf["repos"]:
        repo = cfg["name"]
        if args.target and args.target not in repo:
            continue
        merged = {**defaults, **cfg}
        days = args.days or int(merged.get("max_age_days", 14))
        print(f"[scan] {repo} (last {days}d, policy=policies/repos.json)", file=sys.stderr)
        try:
            rows = scan_repo(repo, merged, days, int(merged.get("max_candidates", 12)), now)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {repo}: {exc}", file=sys.stderr)
            continue
        for row in rows:
            if f"{repo}#{row['number']}" not in handled:
                all_rows.append(row)

    all_rows.sort(key=lambda r: r["score"], reverse=True)
    stamp = now.strftime("%Y-%m-%d")
    (report_dir / f"{stamp}.json").write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"count": len(all_rows), "output": str(report_dir / f"{stamp}.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

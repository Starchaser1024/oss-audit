"""Read-only local SQLite funnel and remote GitHub cohort outcome metrics."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from oss_audit.core.common import gh_get, read_json, utcnow, write_json
from oss_audit.core.paths import WorkspacePaths
from oss_audit.evidence import runner as evidence_runner


def local_snapshot(
    db_path: str | Path,
    portfolio_path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Read one SQLite queue cohort; never infer publication from local preparation."""
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tasks = [dict(r) for r in db.execute("SELECT * FROM tasks")]
        events = [dict(r) for r in db.execute("SELECT * FROM events")]
        runs = [dict(r) for r in db.execute("SELECT * FROM runs")]
        leases = db.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    states = Counter(t["state"] for t in tasks)

    def completed(state: str) -> int:
        return len({e["candidate_id"] for e in events if e["event"] == f"finished:{state}"})

    valid_prepared: list[str] = []
    invalid_prepared: list[dict[str, Any]] = []

    for task in tasks:
        if task["state"] != "prepared":
            continue
        try:
            result = evidence_runner.validate_bundle(task["output_path"], workspace=workspace)
        except (OSError, TypeError, KeyError, ValueError) as exc:
            result = {"ok": False, "errors": [str(exc)]}
        if result["ok"]:
            valid_prepared.append(task["candidate_id"])
        else:
            invalid_prepared.append({"candidate_id": task["candidate_id"], "errors": result["errors"]})

    portfolio = read_json(portfolio_path) if portfolio_path else None
    actual_runs: dict[str, dict[str, Any]] = {}
    for run_row in sorted(runs, key=lambda r: (r["run_pid"] is not None, r["started_at"], r["id"])):
        actual_runs[run_row["token"]] = run_row
    elapsed = [
        r["ended_at"] - r["started_at"]
        for r in actual_runs.values()
        if r["ended_at"] is not None and r["run_pid"] is not None
    ]
    waiting = [
        t for t in tasks if t["state"] in {"waiting", "review_pending", "inspect_required", "needs_model"}
    ]
    return {
        "observed_at": utcnow(),
        "source": str(Path(db_path).resolve()),
        "cohort": "Candidates in this SQLite database; real audits and fixture databases must not be combined",
        "funnel": {
            "discovered": len(portfolio["discovered"]) if portfolio else None,
            "assessed": len(portfolio["assessed"]) if portfolio else None,
            "queued": len(tasks),
            "investigated": completed("investigated"),
            "verified_current_prepared": len(valid_prepared),
            "prepared_current": len(valid_prepared),
            "submitted": None,
            "merged": None,
            "issue_reported": None,
            "maintainer_confirmed": None,
        },
        "states": dict(states),
        "active_leases": leases,
        "no_finding": completed("no_finding"),
        "rejected_reviews": completed("rejected"),
        "failed_attempts": sum(e["event"] == "finished:failed" for e in events),
        "explicit_continuations": sum(e["event"] == "advanced" for e in events),
        "invalid_prepared": invalid_prepared,
        "waiting": [
            {
                "candidate_id": t["candidate_id"],
                "state": t["state"],
                "seconds_in_state": max(0, round(time.time() - t["updated_at"], 2)),
            }
            for t in waiting
        ],
        "execution": {
            "registration_records": len(runs),
            "recorded_runs": len(actual_runs),
            "completed_runs": len(elapsed),
            "sum_run_seconds": round(sum(elapsed), 3),
            "model_tokens": None,
            "human_hours": None,
        },
        "limitations": [
            "AST investigated is a navigation lead, not a verified defect",
            "Verified is a conservative subset with currently valid prepared bundles; unregistered bundles are excluded",
            "Publication/adoption require upstream records; absent tracking is unknown, not zero",
            "Run seconds may overlap and are neither human hours nor priced model cost",
        ],
    }


def inventory(actor: str, since: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    total: int | None = None
    query = f"is:pr author:{actor} created:>={since}"
    for page in range(1, 11):
        raw = gh_get("search/issues?q=" + quote(query) + f"&per_page=100&page={page}")
        if raw.get("incomplete_results", True):
            raise RuntimeError("GitHub search incomplete; outcome inventory UNKNOWN")
        total = raw["total_count"]
        if total > 1000:
            raise RuntimeError("Search exceeds 1000; partition the cohort by date")
        for row in raw["items"]:
            repo = row["repository_url"].split("/repos/")[-1]
            rows[f"{repo.lower()}#{row['number']}"] = {"repo": repo, "number": row["number"]}
        if len(rows) >= total:
            break
    if len(rows) != total:
        raise RuntimeError("Search pages did not reconcile")
    return list(rows.values())


def outcome(row: dict[str, Any]) -> dict[str, Any]:
    try:
        pr = gh_get(f"repos/{row['repo']}/pulls/{row['number']}")
        status = "merged" if pr["merged_at"] else "open" if pr["state"] == "open" else "closed_unmerged"
        return {
            **row,
            "url": pr["html_url"],
            "status": status,
            "created_at": pr["created_at"],
            "merged_at": pr["merged_at"],
            "closed_at": pr["closed_at"],
            "head_sha": pr["head"]["sha"],
        }
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        return dict(row, status="UNKNOWN", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="Required creation-cohort start (YYYY-MM-DD) for remote outcomes")
    parser.add_argument("--local-db", type=Path, help="Read-only local funnel instead of querying GitHub")
    parser.add_argument("--portfolio", type=Path, help="Discovery snapshot belonging to --local-db")
    parser.add_argument("--actor", default=None, help="GitHub login (resolved via `gh api user` if omitted)")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)
    output = args.output or (paths.state_dir / "metrics.json")

    if args.local_db:
        try:
            result = local_snapshot(args.local_db, args.portfolio, args.workspace)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            result = {"observed_at": utcnow(), "status": "UNKNOWN", "reason": str(exc)}
        write_json(output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "UNKNOWN" or result.get("invalid_prepared") else 0

    if not args.since or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        parser.error("since must be YYYY-MM-DD")
    ledger = read_json(paths.ledger_file)
    try:
        actor = args.actor or gh_get("user")["login"]
        found = inventory(actor, args.since)
        external = [r for r in found if r["repo"].split("/")[0].lower() != actor.lower()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(outcome, external))
        discrepancies = []
        for row in rows:
            key = f"{row['repo']}#{row['number']}"
            old = next((v for k, v in ledger.get("submitted", {}).items() if k.lower() == key.lower()), None)
            if not old:
                discrepancies.append(
                    {"key": key, "reason": "remote contribution absent from submitted ledger"}
                )
            else:
                previous = old.get("latest_revision", old)
                if previous.get("state") and previous["state"] != row["status"]:
                    discrepancies.append({"key": key, "ledger": previous["state"], "remote": row["status"]})
        counts = {
            s: sum(r["status"] == s for r in rows) for s in ("merged", "open", "closed_unmerged", "UNKNOWN")
        }
        result = {
            "observed_at": utcnow(),
            "creation_cohort_since": args.since,
            "actor": actor,
            "opened": len(rows),
            "counts": counts,
            "definition": "Repositories not owned by actor; open remains censored, not failed",
            "prepared_to_merged_median_days": None,
            "prepared_timestamp_missing": sum(r["status"] == "merged" for r in rows),
            "limitations": [
                "Legacy prepared entries have no candidate-level timestamps; no invented lead times",
                "Issue-only adoption is separate from merged PRs and requires explicit upstream evidence",
            ],
            "items": rows,
            "ledger_discrepancies": discrepancies,
        }
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        result = {"observed_at": utcnow(), "status": "UNKNOWN", "reason": str(exc)}
    write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") == "UNKNOWN" or result.get("counts", {}).get("UNKNOWN") else 0


if __name__ == "__main__":
    raise SystemExit(main())

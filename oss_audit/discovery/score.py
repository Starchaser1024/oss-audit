"""Evidence-first, explainable shadow ranking for eligible candidates."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from oss_audit.core.common import read_json, utcnow, write_json
from oss_audit.core.paths import WorkspacePaths


def rank(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compute an explainable priority score only when contract and value evidence exist."""
    assessment = candidate.get("value_assessment", {})
    source = assessment.get("evidence_path")
    reasons: list[str] = []
    if candidate.get("decision") != "eligible" or not source or not Path(source).is_file():
        return {
            "candidate_id": candidate.get("candidate_id"),
            "status": "unscored",
            "score": None,
            "reasons": ["Contract gate or value evidence missing"],
        }
    numerator = 0
    for name, weight in (("impact", 4), ("reach", 2), ("confidence", 2), ("maintainer_demand", 3)):
        val = assessment.get(name)
        if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 4:
            raise ValueError(f"{name} must be 0..4 with evidence")
        numerator += val * weight
        reasons.append(f"{name}={val} x {weight}")
    cost = assessment.get("estimated_hours")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0:
        raise ValueError("estimated_hours must be positive and finite")
    return {
        "candidate_id": candidate["candidate_id"],
        "status": "ranked_not_authorized",
        "score": numerator / max(cost, 1),
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--backtest", help="Date window YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)
    output = args.output or (paths.state_dir / "shadow-ranking.json")
    if args.backtest:
        result: dict[str, Any] = {
            "status": "insufficient_history",
            "window": args.backtest,
            "reason": (
                "No complete timestamped candidate/feature snapshots with fixed outcome windows. "
                "Later merges cannot reconstruct historical features."
            ),
            "precision_at_3": None,
            "recall_at_3": None,
        }
    elif args.input:
        rows = [rank(c) for c in read_json(args.input)]
        rows.sort(key=lambda r: r["score"] if r["score"] is not None else -1, reverse=True)
        result = {"observed_at": utcnow(), "candidates": rows, "mode": "prospective_shadow"}
    else:
        parser.error("input or --backtest required")
    write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only upstream duplicate-work and active-PR collision detection for code-audit candidates.

When a candidate defect is discovered through static or runtime code auditing rather
than an existing upstream issue, ``search_claim_collision`` checks whether an open
community PR, tracked issue, or historical fix already addresses the same module or
symptom so maintainers never receive duplicate pull requests.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable
from urllib.parse import quote

from oss_audit.core.common import gh_get, utcnow


def search_claim_collision(
    repo: str,
    queries: list[str],
    client: Callable[[str], dict[str, Any]] = gh_get,
) -> dict[str, Any]:
    """Run bounded read-only GitHub searches to detect upstream issue/PR collisions."""
    result: dict[str, Any] = {
        "repo": repo,
        "status": "UNKNOWN",
        "object_type": "code_audit",
        "checked_at": utcnow(),
        "queries": [],
        "reasons": [],
    }
    if (
        not isinstance(queries, list)
        or len(set(queries)) < 2
        or any(not isinstance(q, str) or len(q.strip()) < 6 or ":" in q for q in queries)
    ):
        result["reasons"] = [
            "At least two specific root-cause/symptom search phrases are required; no query qualifiers"
        ]
        return result
    try:
        for phrase in queries:
            full_query = f"repo:{repo} {phrase}"
            raw = client("search/issues?q=" + quote(full_query) + "&per_page=100&page=1")
            if raw.get("incomplete_results", True) or raw.get("total_count") != len(raw.get("items", [])):
                result["reasons"].append("search_incomplete; inspect and partition query")
                return result
            result["queries"].append(
                {
                    "query": full_query,
                    "matches": [
                        {"url": x["html_url"], "title": x["title"], "state": x["state"]}
                        for x in raw["items"]
                    ],
                }
            )
        matches = [m for q in result["queries"] for m in q["matches"]]
        if matches:
            result["reasons"] = [
                "Inspect matching upstream issues and PRs, including historical fixes; "
                "text similarity alone cannot rule out duplicate work"
            ]
        else:
            result.update(
                status="FREE",
                reasons=[
                    "No matches in the complete bounded queries; independent semantic review still required"
                ],
            )
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        result["reasons"] = [f"GitHub read failed: {exc}"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="Target repository (owner/repo)")
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        default=[],
        help="Root-cause or symptom phrase (supply at least two distinct phrases)",
    )
    args = parser.parse_args(argv)
    report = search_claim_collision(args.repo, args.queries)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "FREE" else 2


if __name__ == "__main__":
    raise SystemExit(main())

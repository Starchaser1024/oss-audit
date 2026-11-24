"""Validate evidence bundles or replay historical selector revisions against counterexample suites."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from oss_audit.core.common import read_json, utcnow, write_json
from oss_audit.core.paths import WorkspacePaths, fixtures_dir
from oss_audit.evidence.runner import validate_bundle, validate_receipt


def _load_selector_revisions(workspace: str | Path | None = None) -> list[tuple[str, str, bytes]]:
    """Load the 4 immutable ``deer-flow`` selector revisions from Git or pinned SHA-256 fixtures."""
    paths = WorkspacePaths.from_workspace(workspace)
    repo = paths.worktrees_dir / "deerflow-7600"
    source_path = "backend/packages/harness/deerflow/models/selection.py"
    revisions = {
        "original": "68c9fce5c424b0e96986a0ca365db73a1eef8196",
        "article_patch": "d67512b050eee17470adf168cc16e8576c92c81c",
        "wordlist_patch": "2bc504b9ba670de04b2e8b78fe66e93141e906da",
        "catalog_fix": "3c510c8552d278013b9304899d88d68ea06751d6",
    }
    if (repo / ".git").exists():
        loaded: list[tuple[str, str, bytes]] = []
        for label, revision in revisions.items():
            sha = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", revision], text=True
            ).strip()
            source = subprocess.check_output(["git", "-C", str(repo), "show", f"{sha}:{source_path}"])
            loaded.append((label, sha, source))
        return loaded

    fixture_root = fixtures_dir() / "historical_selector"
    manifest = read_json(fixture_root / "manifest.json")
    loaded = []
    for label, expected_sha in revisions.items():
        entry = manifest["revisions"][label]
        if entry["sha"] != expected_sha:
            raise ValueError(f"Fixture SHA mismatch for {label}: {entry['sha']} != {expected_sha}")
        raw = (fixture_root / entry["file"]).read_bytes()
        actual_digest = hashlib.sha256(raw).hexdigest()
        if actual_digest != entry["sha256"]:
            raise ValueError(f"Fixture SHA-256 tampered for {label}: {actual_digest}")
        loaded.append((label, entry["sha"], raw))
    return loaded


def historical(
    output: str | Path | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Replay 13 counterexamples across 4 historical selector revisions.

    Verifies that the buggy baseline, naive determiner/article patch, and static
    word-list patch are all rejected by counterexamples, while the catalog-bound
    fix passes every case.
    """
    cases: list[tuple[str, str, str, str, str | None]] = [
        ("ordinary_" + name, f"Use {phrase} model to analyze the report", "plain", "none", None)
        for name, phrase in [
            ("regression", "a regression"),
            ("data", "a data"),
            ("attached", "the attached"),
            ("causal", "a causal"),
            ("econometric", "an econometric"),
            ("linear", "a linear"),
        ]
    ]
    cases += [
        ("explicit_the", "Use the Claude model for this turn: summarize", "plain", "unavailable", "turn"),
        ("explicit_my", "Use my Claude model for this task: summarize", "plain", "unavailable", "turn"),
        ("explicit_conversation", "From now on use the Claude model", "plain", "unavailable", "conversation"),
        ("shared_token", "Use a data model to design a database", "data", "none", None),
        ("case_variant", "USE A REGRESSION MODEL TO analyze", "plain", "none", None),
        ("whitespace_variant", "Use   a regression model   to analyze", "plain", "none", None),
        ("ordinary_python", "Use Python to analyze", "plain", "none", None),
    ]

    def model(name: str, display: str) -> SimpleNamespace:
        return SimpleNamespace(name=name, model=name, display_name=display, aliases=[])

    catalogs = {
        "plain": [model("gpt-default", "GPT Default"), model("claude-sonnet", "Claude Sonnet")],
        "data": [model("gpt-default", "GPT Default"), model("data-chat", "Data Chat")],
    }

    source_path = "backend/packages/harness/deerflow/models/selection.py"
    results: list[dict[str, Any]] = []
    for label, sha, source in _load_selector_revisions(workspace):
        ns: dict[str, Any] = {"__name__": "selector_" + label, "__file__": f"{sha}:{source_path}"}
        exec(compile(source, ns["__file__"], "exec"), ns)
        rows = []
        for name, text, catalog, status, scope in cases:
            actual = ns["resolve_model_control"](text, catalogs[catalog])
            ok = (
                actual["status"] == status
                and actual["scope"] == scope
                and (status != "none" or actual["task_offset"] == 0)
            )
            rows.append(
                {
                    "name": name,
                    "input": text,
                    "expected": {"status": status, "scope": scope},
                    "actual": actual,
                    "passed": ok,
                }
            )
        failed = [r["name"] for r in rows if not r["passed"]]
        results.append(
            {
                "version": label,
                "sha": sha,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "decision": "rejected" if failed else "accepted_for_this_selector_contract",
                "failed": failed,
                "cases": rows,
            }
        )

    ok = all(r["failed"] for r in results[:3]) and not results[3]["failed"]
    report = {
        "checked_at": utcnow(),
        "ok": ok,
        "scope": "Production selector revisions only; not API/frontend/E2E or general PR approval",
        "basis": [
            "https://github.com/bytedance/deer-flow/issues/7598",
            "https://github.com/bytedance/deer-flow/pull/7600",
        ],
        "results": results,
    }
    if output is not None:
        write_json(output, report)
    return {
        "ok": ok,
        "evidence": str(output) if output is not None else "stdout",
        "versions": [{k: r[k] for k in ("version", "sha", "decision", "failed")} for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?")
    ap.add_argument("--suite", choices=["historical-counterexamples"])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=None, help="Optional JSON output path")
    args = ap.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)
    if args.suite:
        result = historical(args.output, args.workspace)
    elif args.all:
        bundle_paths = sorted(paths.evidence_dir.glob("**/bundle.json"))[: args.limit]
        rows: list[dict[str, Any]] = [
            {"path": str(p), **validate_bundle(p, workspace=args.workspace)} for p in bundle_paths
        ]
        if not bundle_paths:
            ledger = read_json(paths.ledger_file)
            rows = [
                {
                    "candidate": k,
                    "status": "legacy_unverified",
                    "reason": "No runner-bound bundle and independent review; historical prose is not an execution receipt",
                }
                for k in list(ledger.get("submitted", {}))[: args.limit]
            ]
        result = {"ok": bool(bundle_paths) and all(r["ok"] for r in rows), "items": rows}
    elif args.path:
        target = Path(args.path)
        result = (
            validate_receipt(target, workspace=args.workspace)
            if target.name == "receipt.json"
            else validate_bundle(target, workspace=args.workspace)
        )
        result.pop("receipt", None)
    else:
        ap.error("Supply a bundle/receipt, --all, or --suite")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

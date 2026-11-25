"""Fail-closed schema and lifecycle validator for candidate investigation dossiers."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from oss_audit.core.common import read_json
from oss_audit.core.paths import WorkspacePaths

CONTRACT_SOURCE_TYPES: tuple[str, ...] = (
    "docstring",
    "typing_protocol",
    "public_docs",
    "sibling_implementation_symmetry",
    "rfc_or_spec",
)

TERMINAL_VERDICTS: tuple[str, ...] = (
    "confirmed",
    "excluded",
    "insufficient_evidence",
    "covered_upstream",
    "design_tradeoff_issue_only",
)

MAINTAINER_FEEDBACK_STATUSES: tuple[str, ...] = (
    "merged",
    "open_under_review",
    "changes_requested",
    "closed_by_design",
    "published_issue",
    "not_submitted_prevented",
)

ENTRYPOINT_KINDS: tuple[str, ...] = (
    "public_api",
    "cli",
    "runtime_pipeline",
    "protocol_adapter",
)

REACHABILITY_LEVELS: tuple[str, ...] = ("proven", "partial", "unproven")
TEST_OUTCOMES: tuple[str, ...] = ("passed", "failed", "error", "not_run", "not_applicable")

FALSIFICATION_BOOLEAN_CHECKS: tuple[str, ...] = (
    "shadowed_import_checked",
    "overfitted_patch_checked",
    "faulty_test_assertion_checked",
    "stale_evidence_checked",
    "upstream_collision_checked",
)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_LINE_ANCHOR_RE = re.compile(r"^L([1-9]\d*)(?:-L?([1-9]\d*))?$")


def is_test_path(path_or_url: str) -> bool:
    """Return True when a path or URL points inside a test suite or test module.

    A failing test authored by an investigator is never itself a product contract.
    """
    normalized = path_or_url.strip().replace("\\", "/")
    normalized = normalized.split("#", 1)[0].split("?", 1)[0]
    parts = [p.lower() for p in PurePosixPath(normalized).parts if p not in {"/", "."}]
    if not parts:
        return False
    if any(part in {"test", "tests", "__tests__", "testing"} for part in parts[:-1]):
        return True
    leaf = parts[-1]
    return (
        leaf == "conftest.py"
        or leaf.startswith("test_")
        or leaf.endswith(("_test.py", "_tests.py", ".spec.ts", ".test.ts"))
    )


def is_valid_line_anchor(anchor: str) -> bool:
    """Validate an explicit ``L<start>`` or ``L<start>-L<end>`` line anchor."""
    if not isinstance(anchor, str):
        return False
    match = _LINE_ANCHOR_RE.match(anchor.strip())
    if not match:
        return False
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    return end >= start


def _require_nonempty_str(mapping: dict[str, Any], key: str, prefix: str, errors: list[str]) -> str:
    val = mapping.get(key)
    if not isinstance(val, str) or not val.strip():
        errors.append(f"missing_or_empty:{prefix}{key}")
        return ""
    return val.strip()


def validate_dossier_schema(dossier: dict[str, Any]) -> dict[str, Any]:
    """Validate structural schema and lifecycle invariants of a candidate dossier."""
    errors: list[str] = []
    if not isinstance(dossier, dict):
        return {"ok": False, "candidate_id": None, "errors": ["dossier_must_be_object"]}

    cid = _require_nonempty_str(dossier, "candidate_id", "", errors)
    repo = _require_nonempty_str(dossier, "upstream_repo", "", errors)
    if repo and not _REPO_RE.match(repo):
        errors.append(f"invalid_upstream_repo:{repo}")

    commit = _require_nonempty_str(dossier, "pinned_commit", "", errors)
    if commit and not _SHA_RE.match(commit):
        errors.append(f"invalid_pinned_commit:{commit}")

    verdict = _require_nonempty_str(dossier, "terminal_verdict", "", errors)
    if verdict and verdict not in TERMINAL_VERDICTS:
        errors.append(f"invalid_terminal_verdict:{verdict}")

    # 1. Contract provenance
    provenance = dossier.get("contract_provenance")
    if not isinstance(provenance, list) or not provenance:
        errors.append("missing_or_empty:contract_provenance")
    else:
        for idx, item in enumerate(provenance):
            pfx = f"contract_provenance[{idx}]."
            if not isinstance(item, dict):
                errors.append(f"invalid_object:{pfx[:-1]}")
                continue
            stype = _require_nonempty_str(item, "source_type", pfx, errors)
            if stype and stype not in CONTRACT_SOURCE_TYPES:
                errors.append(f"invalid_contract_source_type:{stype}")
            src_path = _require_nonempty_str(item, "path", pfx, errors)
            if src_path and is_test_path(src_path):
                errors.append(f"investigator_test_is_not_a_contract:{src_path}")
            anchor = _require_nonempty_str(item, "line_anchor", pfx, errors)
            if anchor and not is_valid_line_anchor(anchor):
                errors.append(f"invalid_line_anchor:{anchor}")
            _require_nonempty_str(item, "claim", pfx, errors)

    # 2. Production entrypoint
    entrypoint = dossier.get("production_entrypoint")
    if not isinstance(entrypoint, dict):
        errors.append("missing_or_empty:production_entrypoint")
    else:
        kind = _require_nonempty_str(entrypoint, "kind", "production_entrypoint.", errors)
        if kind and kind not in ENTRYPOINT_KINDS:
            errors.append(f"invalid_entrypoint_kind:{kind}")
        _require_nonempty_str(entrypoint, "symbol", "production_entrypoint.", errors)
        chain = entrypoint.get("call_chain")
        if not isinstance(chain, list) or not chain or not all(isinstance(s, str) and s.strip() for s in chain):
            errors.append("missing_or_empty:production_entrypoint.call_chain")
        reach = _require_nonempty_str(entrypoint, "reachability", "production_entrypoint.", errors)
        if reach and reach not in REACHABILITY_LEVELS:
            errors.append(f"invalid_reachability:{reach}")

    # 3. Minimal reproduction
    repro = dossier.get("minimal_reproduction")
    if not isinstance(repro, dict):
        errors.append("missing_or_empty:minimal_reproduction")
    else:
        for field in ("test_nodeid", "input_summary", "expected_on_contract", "observed_on_base"):
            _require_nonempty_str(repro, field, "minimal_reproduction.", errors)
        for field in ("base_outcome", "patched_outcome"):
            outcome = _require_nonempty_str(repro, field, "minimal_reproduction.", errors)
            if outcome and outcome not in TEST_OUTCOMES:
                errors.append(f"invalid_test_outcome:minimal_reproduction.{field}:{outcome}")

    # 4. Normal-behavior counterexample
    normal = dossier.get("normal_behavior_counterexample")
    if not isinstance(normal, dict):
        errors.append("missing_or_empty:normal_behavior_counterexample")
    else:
        for field in ("test_nodeid", "normal_input_summary", "rationale"):
            _require_nonempty_str(normal, field, "normal_behavior_counterexample.", errors)
        for field in ("base_outcome", "patched_outcome"):
            outcome = _require_nonempty_str(normal, field, "normal_behavior_counterexample.", errors)
            if outcome and outcome not in TEST_OUTCOMES:
                errors.append(f"invalid_test_outcome:normal_behavior_counterexample.{field}:{outcome}")

    # 5. Falsification log
    flog = dossier.get("falsification_log")
    if not isinstance(flog, dict):
        errors.append("missing_or_empty:falsification_log")
    else:
        _require_nonempty_str(flog, "reviewer", "falsification_log.", errors)
        for check_key in FALSIFICATION_BOOLEAN_CHECKS:
            if not isinstance(flog.get(check_key), bool):
                errors.append(f"missing_or_nonboolean:falsification_log.{check_key}")
        signals = flog.get("overfitting_signals", [])
        if not isinstance(signals, list) or not all(isinstance(s, str) and s.strip() for s in signals):
            errors.append("invalid_list:falsification_log.overfitting_signals")
        collision = flog.get("upstream_collision")
        if collision is not None and (not isinstance(collision, str) or not collision.strip()):
            errors.append("invalid_field:falsification_log.upstream_collision")
        disproved = flog.get("disproved_reason")
        if disproved is not None and not isinstance(disproved, str):
            errors.append("invalid_field:falsification_log.disproved_reason")
        if verdict == "excluded" and (not isinstance(disproved, str) or not disproved.strip()):
            errors.append("excluded_verdict_requires_disproved_reason")

    # 6. Maintainer feedback loop
    feedback = dossier.get("maintainer_feedback")
    if not isinstance(feedback, dict):
        errors.append("missing_or_empty:maintainer_feedback")
    else:
        fstatus = _require_nonempty_str(feedback, "status", "maintainer_feedback.", errors)
        if fstatus and fstatus not in MAINTAINER_FEEDBACK_STATUSES:
            errors.append(f"invalid_maintainer_feedback_status:{fstatus}")
        _require_nonempty_str(feedback, "maintainer_notes", "maintainer_feedback.", errors)
        req_ev = feedback.get("requested_evidence")
        if not isinstance(req_ev, list) or not all(isinstance(s, str) and s.strip() for s in req_ev):
            errors.append("invalid_list:maintainer_feedback.requested_evidence")
        rule_update = feedback.get("rule_update_derived")
        if not isinstance(rule_update, str):
            errors.append("missing_or_nonstring:maintainer_feedback.rule_update_derived")
        elif fstatus in {"changes_requested", "closed_by_design"} and not rule_update.strip():
            errors.append(f"feedback_status_requires_rule_update:{fstatus}")

        # Lifecycle transition consistency between terminal_verdict and maintainer_feedback.status
        if verdict in {"excluded", "insufficient_evidence", "covered_upstream"} and fstatus in {
            "merged",
            "open_under_review",
            "changes_requested",
        }:
            errors.append(f"incompatible_verdict_and_feedback:{verdict}->{fstatus}")
        if verdict == "design_tradeoff_issue_only" and fstatus in {
            "merged",
            "open_under_review",
            "changes_requested",
        }:
            errors.append(f"incompatible_verdict_and_feedback:{verdict}->{fstatus}")

    return {
        "ok": not errors,
        "candidate_id": cid or None,
        "terminal_verdict": verdict or None,
        "errors": errors,
    }


def load_dossiers(path: str | Path) -> list[dict[str, Any]]:
    """Load candidate dossiers from a single dossier JSON or a ledger JSON."""
    payload = read_json(path)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("dossiers"), list):
            return payload["dossiers"]
        if "candidate_id" in payload:
            return [payload]
    raise ValueError(f"Unrecognized dossier payload in {path}")


def validate_dossier_ledger(path: str | Path) -> dict[str, Any]:
    """Validate every candidate dossier in ``path`` and enforce unique ``candidate_id``s."""
    dossiers = load_dossiers(path)
    if not dossiers:
        return {"ok": False, "count": 0, "errors": ["empty_dossier_ledger"], "items": []}
    seen_ids: set[str] = set()
    duplicate_errors: list[str] = []
    items: list[dict[str, Any]] = []
    for entry in dossiers:
        res = validate_dossier_schema(entry)
        cid = res.get("candidate_id")
        if cid:
            if cid in seen_ids:
                duplicate_errors.append(f"duplicate_candidate_id:{cid}")
            seen_ids.add(cid)
        items.append(res)
    ok = not duplicate_errors and all(item["ok"] for item in items)
    return {
        "ok": ok,
        "count": len(items),
        "errors": duplicate_errors,
        "items": items,
    }


def summarize_dossiers(dossiers: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate candidate verdicts, prevented PR noise, and maintainer feedback loops."""
    by_verdict: dict[str, int] = {v: 0 for v in TERMINAL_VERDICTS}
    by_feedback: dict[str, int] = {s: 0 for s in MAINTAINER_FEEDBACK_STATUSES}
    rule_updates: list[dict[str, str]] = []
    prevented = 0

    for dossier in dossiers:
        verdict = str(dossier.get("terminal_verdict", ""))
        if verdict in by_verdict:
            by_verdict[verdict] += 1
        feedback = dossier.get("maintainer_feedback") or {}
        status = str(feedback.get("status", ""))
        if status in by_feedback:
            by_feedback[status] += 1
        if status == "not_submitted_prevented":
            prevented += 1
        derived = feedback.get("rule_update_derived")
        if isinstance(derived, str) and derived.strip():
            rule_updates.append(
                {
                    "candidate_id": str(dossier.get("candidate_id", "")),
                    "status": status,
                    "rule_update_derived": derived.strip(),
                }
            )

    return {
        "total_candidates": len(dossiers),
        "by_terminal_verdict": by_verdict,
        "by_maintainer_feedback": by_feedback,
        "prevented_unhelpful_prs": prevented,
        "derived_rule_updates_count": len(rule_updates),
        "derived_rule_updates": rule_updates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    sub = parser.add_subparsers(dest="command")

    val_p = sub.add_parser("validate", help="Validate candidate dossier schema and lifecycle rules")
    val_p.add_argument("path", type=Path, help="Path to a dossier JSON or dossier ledger JSON")

    sum_p = sub.add_parser("summary", help="Summarize candidate verdicts and maintainer feedback")
    sum_p.add_argument("path", nargs="?", type=Path, default=None, help="Optional dossier ledger path")
    sum_p.add_argument("--verdict", choices=TERMINAL_VERDICTS, default=None)

    list_p = sub.add_parser("list", help="List candidate dossiers with optional verdict filter")
    list_p.add_argument("path", nargs="?", type=Path, default=None, help="Optional dossier ledger path")
    list_p.add_argument("--verdict", choices=TERMINAL_VERDICTS, default=None)

    args = parser.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)
    cmd = args.command or "summary"

    try:
        if cmd == "validate":
            result = validate_dossier_ledger(args.path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2

        target = args.path if getattr(args, "path", None) is not None else paths.dossiers_file
        if not Path(target).is_file():
            print(json.dumps({"ok": False, "error": f"Dossier file not found: {target}"}, indent=2))
            return 2
        validation = validate_dossier_ledger(target)
        dossiers = load_dossiers(target)
        verdict_filter = getattr(args, "verdict", None)
        if verdict_filter:
            dossiers = [d for d in dossiers if d.get("terminal_verdict") == verdict_filter]

        if cmd == "list":
            rows = [
                {
                    "candidate_id": d.get("candidate_id"),
                    "upstream_repo": d.get("upstream_repo"),
                    "pinned_commit": d.get("pinned_commit"),
                    "terminal_verdict": d.get("terminal_verdict"),
                    "feedback_status": (d.get("maintainer_feedback") or {}).get("status"),
                }
                for d in dossiers
            ]
            out = {"ok": validation["ok"], "count": len(rows), "candidates": rows}
        else:
            out = {"ok": validation["ok"], **summarize_dossiers(dossiers)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if validation["ok"] else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

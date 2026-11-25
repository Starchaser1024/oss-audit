"""Execute bounded pytest checks and validate SHA-256-bound contribution evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from oss_audit.core.common import digest, git, read_json, utcnow, write_json
from oss_audit.core.paths import WorkspacePaths

HARNESS_PATH = Path(__file__).resolve().with_name("pytest_harness.py")

PARSER_CASES = {
    "original_bug",
    "explicit_valid",
    "ordinary_similar",
    "unknown",
    "adjacent_boundary",
    "metamorphic",
    "preservation",
}


def _preserve_executable_path(raw: str | Path) -> str:
    """Canonicalize parent directories while preserving leaf virtualenv symlinks."""
    expanded = Path(os.path.expanduser(str(raw)))
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return str(expanded.parent.resolve() / expanded.name)


def resolve_python_executable(raw: str | Path | None = None) -> str:
    """Resolve an interpreter path without stripping its virtualenv leaf symlink."""
    current_python = _preserve_executable_path(sys.executable)
    if raw is None:
        return current_python
    text = str(raw).strip()
    if not text or text.lower() in {"python", "python3", "python.exe", "python3.exe"}:
        return current_python
    has_sep = os.path.sep in text or (os.path.altsep is not None and os.path.altsep in text)
    expanded = Path(os.path.expanduser(text))
    if has_sep or expanded.exists():
        return _preserve_executable_path(expanded)
    sibling = shutil.which(text, path=str(Path(current_python).parent))
    if sibling:
        return _preserve_executable_path(sibling)
    found = shutil.which(text)
    if found:
        return _preserve_executable_path(found)
    return _preserve_executable_path(expanded)


def resolve_tool_argv(argv: list[str], python_executable: str | None = None) -> list[str]:
    """Bind bare python/pytest/ruff commands to the target or active interpreter environment."""
    bound_py = resolve_python_executable(python_executable)
    host_py = _preserve_executable_path(sys.executable)
    head = argv[0].strip()
    lower_head = head.lower()
    if lower_head in {"python", "python3", "python.exe", "python3.exe"}:
        return [bound_py, *argv[1:]]
    has_sep = os.path.sep in head or (os.path.altsep is not None and os.path.altsep in head)
    if has_sep:
        expanded = Path(os.path.expanduser(head))
        if expanded.exists():
            return [_preserve_executable_path(expanded), *argv[1:]]
        return list(argv)
    if lower_head in {"pytest", "py.test", "pytest.exe", "py.test.exe", "ruff", "ruff.exe"}:
        module_name = "pytest" if ("pytest" in lower_head or "py.test" in lower_head) else "ruff"
        if Path(bound_py) == Path(host_py) and importlib.util.find_spec(module_name) is not None:
            return [host_py, "-m", module_name, *argv[1:]]
        sibling_bound = shutil.which(head, path=str(Path(bound_py).parent))
        if sibling_bound:
            if module_name == "pytest":
                return [bound_py, "-m", "pytest", *argv[1:]]
            return [_preserve_executable_path(sibling_bound), *argv[1:]]
        if importlib.util.find_spec(module_name) is not None:
            return [host_py, "-m", module_name, *argv[1:]]
        sibling_host = shutil.which(head, path=str(Path(host_py).parent))
        if sibling_host:
            return [_preserve_executable_path(sibling_host), *argv[1:]]
    else:
        for bin_dir in (Path(bound_py).parent, Path(host_py).parent):
            sibling = shutil.which(head, path=str(bin_dir))
            if sibling:
                return [_preserve_executable_path(sibling), *argv[1:]]
    return list(argv)


def inside(checkout: str | Path, relative: str | Path) -> Path:
    checkout_root = Path(checkout).resolve()
    result = (checkout_root / relative).resolve()
    if not result.is_relative_to(checkout_root):
        raise ValueError(f"Path escapes checkout: {relative}")
    return result


def fingerprint(checkout: str | Path) -> dict[str, Any]:
    """Bind tracked diff and all non-ignored untracked content, including new tests."""
    checkout_root = Path(checkout).resolve()
    diff = subprocess.check_output(
        ["git", "-C", str(checkout_root), "diff", "--binary", "HEAD"],
        timeout=60,
    )
    untracked = git(checkout_root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return {
        "head": git(checkout_root, "rev-parse", "HEAD"),
        "diff": hashlib.sha256(diff).hexdigest(),
        "untracked": {p: digest(inside(checkout_root, p)) for p in untracked if p},
    }


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(spec)
    checkout = Path(normalized["checkout"]).resolve()
    if (
        git(checkout, "rev-parse", "--show-toplevel").replace("\\", "/").lower()
        != checkout.as_posix().lower()
    ):
        raise ValueError("checkout must be a Git root")
    for key in (
        "candidate_id",
        "phase",
        "revision",
        "python",
        "sources",
        "tests",
        "imports",
        "pythonpath",
        "pytest_args",
        "locks",
    ):
        if key not in normalized:
            raise ValueError(f"Missing {key}")
    if normalized["phase"] not in ("red", "green", "adjacent", "lint", "format", "whitespace"):
        raise ValueError("Unsupported phase")
    if (
        not normalized["sources"]
        or not normalized["tests"]
        or not normalized["imports"]
        or not normalized["pytest_args"]
    ):
        raise ValueError("Sources, tests, imports and pytest args must be nonempty")
    if git(checkout, "rev-parse", normalized["revision"]) != git(checkout, "rev-parse", "HEAD"):
        raise ValueError("Wrong checkout revision")
    source_files = {str(inside(checkout, p)) for p in normalized["sources"]}
    normalized["imports"] = {k: str(inside(checkout, v)) for k, v in normalized["imports"].items()}
    if not set(normalized["imports"].values()) <= source_files:
        raise ValueError("Import proofs must point to declared source files")
    normalized["pythonpath"] = [str(inside(checkout, p)) for p in normalized["pythonpath"]]
    normalized["cwd"] = str(inside(checkout, normalized.get("cwd", ".")))
    normalized["checkout"] = str(checkout)
    normalized["python"] = resolve_python_executable(normalized["python"])
    if normalized["phase"] == "red":
        changed = set(filter(None, git(checkout, "diff", "--name-only", "HEAD").splitlines()))
        untracked = set(
            filter(None, git(checkout, "ls-files", "--others", "--exclude-standard").splitlines())
        )
        if (changed | untracked) - set(normalized["tests"]):
            raise ValueError("RED must use unchanged production code; only declared test overlays allowed")
    return normalized


def test_results(out: str | Path) -> dict[str, Any]:
    out_dir = Path(out)
    rows = read_json(out_dir / "outcomes.json")
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "xfail": 0}
    nodes: dict[str, str] = {}
    for row in rows:
        if row["wasxfail"]:
            counts["xfail"] += 1
        if row["when"] == "call":
            nodes[row["nodeid"]] = row["outcome"]
            counts[row["outcome"]] += 1
        elif row["outcome"] == "skipped":
            counts["skipped"] += 1
        elif row["outcome"] == "failed":
            counts["errors"] += 1
    xml = ET.parse(out_dir / "junit.xml")
    failures = list(xml.iter("failure"))
    assertion_failures = all(
        "assert" in ((f.get("message") or "") + (f.text or "")).lower() for f in failures
    )
    return {"counts": counts, "nodes": nodes, "assertion_failures": bool(failures) and assertion_failures}


def run(
    spec_path: str | Path,
    output: str | Path,
    workspace: str | Path | None = None,
) -> Path:
    paths = WorkspacePaths.from_workspace(workspace)
    spec = normalize_spec(read_json(spec_path))
    output_dir = Path(output).resolve()
    if output_dir.is_relative_to(Path(spec["checkout"])):
        raise ValueError("Evidence output must be outside the tested checkout")
    output_dir.mkdir(parents=True, exist_ok=False)
    normalized = output_dir / "spec.json"
    write_json(normalized, spec)
    before = fingerprint(spec["checkout"])
    bindings = {
        key: {p: digest(inside(Path(spec["checkout"]), p)) for p in spec[key]}
        for key in ("sources", "tests", "locks")
    }
    is_check = spec["phase"] in {"lint", "format", "whitespace"}
    raw_cmd = (
        spec["check_argv"]
        if is_check
        else [spec["python"], "-B", str(HARNESS_PATH), str(normalized), str(output_dir)]
    )
    if not isinstance(raw_cmd, list) or not raw_cmd or not all(isinstance(s, str) for s in raw_cmd):
        raise ValueError("Command must be a nonempty argv array")
    cmd = resolve_tool_argv(raw_cmd, spec["python"]) if is_check else raw_cmd
    tmp_dir = paths.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    path_entries: list[str] = []
    for interp in (spec["python"], resolve_python_executable()):
        bin_dir = str(Path(interp).parent)
        if bin_dir and bin_dir not in path_entries:
            path_entries.append(bin_dir)
    if env.get("PATH"):
        path_entries.append(env["PATH"])
    env.update(
        PATH=os.pathsep.join(path_entries),
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(spec["pythonpath"]),
        TEMP=str(tmp_dir),
        TMP=str(tmp_dir),
    )
    allowed_env = spec.get("environment", {})
    if any(k not in {"DEER_FLOW_HOME", "NO_COLOR", "TZ"} for k in allowed_env):
        raise ValueError("Unsupported recorded environment variable")
    env.update(allowed_env)
    receipt: dict[str, Any] = {
        "schema": 1,
        "run_id": str(uuid.uuid4()),
        "started_at": utcnow(),
        "spec": spec,
        "before": before,
        "bindings": bindings,
        "argv": cmd,
        "runner_sha256": digest(HARNESS_PATH),
    }
    with (output_dir / "stdout.log").open("wb") as stdout, (output_dir / "stderr.log").open("wb") as stderr:
        try:
            proc = subprocess.run(
                cmd,
                cwd=spec["cwd"],
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=min(int(spec.get("timeout_seconds", 300)), 1800),
                check=False,
            )
            receipt["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            receipt.update(exit_code=124, timed_out=True)
    receipt.update(finished_at=utcnow(), after=fingerprint(spec["checkout"]))
    receipt["files"] = {p.name: digest(p) for p in output_dir.iterdir() if p.is_file()}
    if not is_check:
        try:
            receipt["results"] = test_results(output_dir)
        except (OSError, ValueError, ET.ParseError) as exc:
            receipt["results_error"] = str(exc)
    receipt_path = output_dir / "receipt.json"
    write_json(receipt_path, receipt)
    journal = paths.evidence_journal_dir / (receipt["run_id"] + ".json")
    write_json(journal, {"receipt": str(receipt_path), "sha256": digest(receipt_path), "created_at": utcnow()})
    return receipt_path


def validate_receipt(
    path: str | Path,
    check_current: bool = True,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    paths = WorkspacePaths.from_workspace(workspace)
    receipt_path = Path(path).resolve()
    errors: list[str] = []
    r = read_json(receipt_path)
    try:
        journal = read_json(paths.evidence_journal_dir / (r["run_id"] + ".json"))
        if journal["sha256"] != digest(receipt_path) or Path(journal["receipt"]).resolve() != receipt_path:
            errors.append("receipt_journal_mismatch")
        for name, sha in r["files"].items():
            if digest(receipt_path.parent / name) != sha:
                errors.append(f"artifact_changed:{name}")
        if r["before"] != r["after"]:
            errors.append("checkout_changed_during_run")
        if check_current and fingerprint(r["spec"]["checkout"]) != r["after"]:
            errors.append("checkout_changed_since_run")
        for key, binding in r["bindings"].items():
            for rel, sha in binding.items():
                if check_current and digest(inside(Path(r["spec"]["checkout"]), rel)) != sha:
                    errors.append(f"{key}_changed:{rel}")
        if r["spec"]["phase"] in {"lint", "format", "whitespace"}:
            if r["exit_code"] != 0:
                errors.append("check_failed")
            return {"ok": not errors, "errors": errors, "receipt": r, "counts": {}}
        actual = test_results(receipt_path.parent)
        if actual != r.get("results"):
            errors.append("counts_or_nodes_changed")
        imports = read_json(receipt_path.parent / "imports.json")
        if imports["imports"] != r["spec"]["imports"]:
            errors.append("import_provenance_mismatch")
        counts = actual["counts"]
        if counts["errors"] or counts["skipped"] or counts["xfail"]:
            errors.append("errors_skips_or_xfail")
        phase = r["spec"]["phase"]
        if phase == "red":
            if r["exit_code"] != 1 or not counts["failed"] or not actual["assertion_failures"]:
                errors.append("not_assertion_red")
        elif r["exit_code"] != 0 or not counts["passed"] or counts["failed"]:
            errors.append("not_green")
    except (OSError, KeyError, ValueError, ET.ParseError) as exc:
        errors.append(f"missing_or_invalid_evidence:{exc}")
    return {
        "ok": not errors,
        "errors": errors,
        "receipt": r,
        "counts": r.get("results", {}).get("counts", {}),
    }


def validate_contract(card: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in (
        "candidate_id",
        "repo",
        "base_sha",
        "user_scenario",
        "expected_behavior",
        "observed_behavior",
        "root_cause",
        "preserved_behaviors",
        "entry_path",
        "contract_sources",
    ):
        if not card.get(key):
            errors.append(f"missing_contract:{key}")
    for source in card.get("contract_sources", []):
        if not all(source.get(k) for k in ("url_or_path", "revision", "location")):
            errors.append("contract_source_without_location_or_revision")
    if card.get("reachability") != "proven":
        errors.append("reachability_not_proven")
    if card.get("design_uncertainties"):
        errors.append("needs_design_decision")
    cases = card.get("counterexample_cases", [])
    required = (
        PARSER_CASES
        if card.get("defect_class") == "parser"
        else {"original_bug", "preservation", "adjacent_boundary"}
    )
    if not required <= {c.get("category") for c in cases}:
        errors.append("counterexample_categories_missing")
    for case in cases:
        if not case.get("nodeid") or not case.get("basis"):
            errors.append("counterexample_without_node_or_basis")
    return errors


def bundle_binding(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": digest(bundle["contract"]),
        "receipts": {k: digest(v) for k, v in sorted(bundle["receipts"].items())},
    }


def validate_report(
    path: str | Path,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Validate an issue-only report bundle (proven RED receipt + independent review)."""
    errors: list[str] = []
    counts: dict[str, Any] = {}
    try:
        bundle = read_json(path)
        card = read_json(bundle["contract"])
        for key in (
            "candidate_id",
            "repo",
            "base_sha",
            "user_scenario",
            "expected_behavior",
            "observed_behavior",
            "root_cause",
            "entry_path",
            "contract_sources",
        ):
            if not card.get(key):
                errors.append(f"missing_contract:{key}")
        if card.get("reachability") != "proven":
            errors.append("reachability_not_proven")
        red = validate_receipt(bundle["receipts"]["red"], workspace=workspace)
        errors += red["errors"]
        r = red["receipt"]
        counts["red"] = red["counts"]
        if (
            r["spec"]["phase"] != "red"
            or r["spec"]["candidate_id"] != card["candidate_id"]
            or r["before"]["head"] != card["base_sha"]
        ):
            errors.append("report_execution_identity_mismatch")
        review = read_json(bundle["review"])
        if (
            review.get("binding") != bundle_binding(bundle)
            or not review.get("reviewer")
            or review.get("decision") != "approved_report"
        ):
            errors.append("report_review_missing_or_stale")
        if not review.get("semantic_basis") or not review.get("causal_checks"):
            errors.append("report_review_missing_reasoning")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"invalid_report:{exc}")
    return {"ok": not errors, "errors": errors, "counts": counts}


def validate_bundle(
    path: str | Path,
    require_review: bool = True,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    counts: dict[str, Any] = {}
    try:
        bundle = read_json(path)
        card = read_json(bundle["contract"])
        errors += validate_contract(card)
        validated = {
            name: validate_receipt(p, workspace=workspace) for name, p in bundle["receipts"].items()
        }
        for name, result in validated.items():
            errors += [f"{name}:{e}" for e in result["errors"]]
            counts[name] = result["counts"]
            if result["receipt"]["spec"]["phase"] != name:
                errors.append(f"receipt_phase_mismatch:{name}")
        if not {"adjacent", "lint", "format", "whitespace"} <= validated.keys():
            errors.append("adjacent_lint_format_whitespace_required")
        if not {"red", "green"} <= validated.keys():
            errors.append("red_green_pair_required")
        else:
            red, green = (validated[k]["receipt"] for k in ("red", "green"))
            if red["spec"]["phase"] != "red" or green["spec"]["phase"] != "green":
                errors.append("phase_mismatch")
            if any(r["spec"]["candidate_id"] != card["candidate_id"] for r in (red, green)):
                errors.append("candidate_mismatch")
            if red["before"]["head"] != card["base_sha"]:
                errors.append("base_mismatch")
            if red["bindings"]["tests"] != green["bindings"]["tests"]:
                errors.append("different_regression_tests")
            if red["bindings"]["locks"] != green["bindings"]["locks"]:
                errors.append("different_dependency_locks")
            for name, value in validated.items():
                if name in {"red", "green"}:
                    continue
                other = value["receipt"]
                if other["spec"]["candidate_id"] != card["candidate_id"] or other["after"] != green["after"]:
                    errors.append(f"{name}:different_candidate_or_source_snapshot")
            rn, gn = red.get("results", {}).get("nodes", {}), green.get("results", {}).get("nodes", {})
            if not rn or set(rn) != set(gn):
                errors.append("different_collected_nodes")
            for case in card.get("counterexample_cases", []):
                node = case.get("nodeid")
                if gn.get(node) != "passed":
                    errors.append(f"counterexample_not_passed:{node}")
                if case.get("category") == "preservation" and rn.get(node) != "passed":
                    errors.append(f"preservation_not_green_on_base:{node}")
                if case.get("category") == "original_bug" and rn.get(node) != "failed":
                    errors.append(f"original_bug_not_red:{node}")
        if require_review:
            review = read_json(bundle["review"])
            if review.get("binding") != bundle_binding(bundle):
                errors.append("review_stale")
            if review.get("reviewer") != "Codex" or review.get("decision") != "approved":
                errors.append("independent_review_not_approved")
            if (
                not review.get("semantic_basis")
                or not review.get("falsification_cases")
                or not review.get("causal_checks")
            ):
                errors.append("review_missing_independent_reasoning")
    except (OSError, KeyError, ValueError, TypeError) as exc:
        errors.append(f"invalid_bundle:{exc}")
    return {"ok": not errors, "errors": errors, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Run pytest spec and create immutable execution receipt")
    run_p.add_argument("--spec", required=True)
    run_p.add_argument("--output", required=True)
    chk_p = sub.add_parser("check", help="Validate contract, execution, and independent review")
    chk_p.add_argument("bundle")
    chk_p.add_argument("--execution-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            receipt_path = run(args.spec, args.output, workspace=args.workspace)
            result = validate_receipt(receipt_path, workspace=args.workspace)
            print(
                json.dumps(
                    {
                        "receipt": str(receipt_path),
                        "ok": result["ok"],
                        "errors": result["errors"],
                        "counts": result["counts"],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            result = validate_bundle(args.bundle, not args.execution_only, workspace=args.workspace)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

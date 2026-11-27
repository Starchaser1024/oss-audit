"""Structured upstream contribution-policy router.

Reads a pinned policy snapshot in ``policies/repos.json`` and routes a verified
candidate to one of:

``direct_pr``, ``issue_first``, ``issue_then_wait``, ``issue_only``, ``hold``.

Missing or stale policy/context fields fail closed as ``hold``. Root discovery
inventory (``repos.json`` / legacy ``repos.yaml``) is intentionally never
consulted as a policy fallback.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from oss_audit.core.paths import WorkspacePaths

ROUTES = ("direct_pr", "issue_first", "issue_then_wait", "issue_only", "hold")
CONTRIBUTION_TYPES = ("bugfix", "feature")
CLAIM_STATES = ("FREE", "TAKEN", "UNKNOWN")

_REQUIRED_CANDIDATE = (
    "defect_proven",
    "fix_verified",
    "design_uncertainties",
    "contribution_type",
    "report_only",
)
_REQUIRED_CONTEXT = (
    "claim",
    "issue_number",
    "labels",
    "assignees",
    "actor",
    "open_pr_count",
    "own_open_pr_count",
    "checked_at",
)

_APPROVAL_ASSOCIATIONS = {"MEMBER", "OWNER", "COLLABORATOR"}
_APPROVAL_DECISION_RE = re.compile(
    r"\b(?:approved|approve|ready\s+for\s+implementation|ready\s+to\s+implement|go\s+ahead|lgtm|agreed|accepted|proceed|welcome)\b",
    re.IGNORECASE,
)


def default_policy_path(workspace: str | Path | None = None) -> Path:
    return WorkspacePaths.from_workspace(workspace).policy_file


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_repo(repo: Any, aliases: dict[str, str] | None = None) -> str | None:
    """Return the canonical ``owner/name`` for a known alias, else the input."""
    if not isinstance(repo, str):
        return None
    cleaned = repo.strip()
    if cleaned.count("/") != 1 or any(part in ("", ".", "..") for part in cleaned.split("/")):
        return None
    if aliases:
        return aliases.get(cleaned.lower(), cleaned)
    return cleaned


def _unknown_policy(repo: Any, reason: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "canonical_repo": repo if isinstance(repo, str) else None,
        "reason": reason,
        "sources": [],
        "rules": None,
    }


def load_policy(
    repo: str,
    path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Load a policy record from ``policies/repos.json``."""
    policy_path = Path(path) if path is not None else default_policy_path(workspace)
    try:
        with policy_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        return _unknown_policy(repo, f"policy_file_unreadable:{type(exc).__name__}")

    if not isinstance(document, dict) or not isinstance(document.get("repos"), dict):
        return _unknown_policy(repo, "policy_file_malformed")

    aliases = document.get("aliases") if isinstance(document.get("aliases"), dict) else {}
    canonical = canonical_repo(repo, aliases)
    if canonical is None:
        return _unknown_policy(repo, "repo_name_invalid")

    record = document["repos"].get(canonical)
    if not isinstance(record, dict):
        lower_map = {str(key).casefold(): key for key in document["repos"]}
        record = document["repos"].get(lower_map.get(canonical.casefold()))
    if not isinstance(record, dict):
        return _unknown_policy(canonical, "repo_not_in_policy_snapshot")

    loaded = dict(record)
    loaded["status"] = "known" if record.get("source_status") == "ok" else "unknown"
    loaded.setdefault("canonical_repo", canonical)
    defaults = document.get("defaults") if isinstance(document.get("defaults"), dict) else {}
    for key in ("max_policy_age_hours", "max_context_age_minutes"):
        if key not in loaded and key in defaults:
            loaded[key] = defaults[key]
    return loaded


def _validate_candidate(candidate: Any) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    values: dict[str, Any] = {}
    if not isinstance(candidate, dict):
        return ["candidate_not_object"], values
    for key in _REQUIRED_CANDIDATE:
        if key not in candidate:
            problems.append(f"missing_candidate_field:{key}")
            continue
        values[key] = candidate[key]
    if "defect_proven" in values and not isinstance(values["defect_proven"], bool):
        problems.append("invalid_candidate_field:defect_proven")
    if "fix_verified" in values and not isinstance(values["fix_verified"], bool):
        problems.append("invalid_candidate_field:fix_verified")
    if "report_only" in values and not isinstance(values["report_only"], bool):
        problems.append("invalid_candidate_field:report_only")
    uncertainties = values.get("design_uncertainties")
    if "design_uncertainties" in values and not isinstance(uncertainties, list):
        problems.append("invalid_candidate_field:design_uncertainties")
    ctype = values.get("contribution_type")
    if "contribution_type" in values and ctype not in CONTRIBUTION_TYPES:
        problems.append("invalid_candidate_field:contribution_type")
    return problems, values


def _validate_context(context: Any) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    values: dict[str, Any] = {}
    if not isinstance(context, dict):
        return ["context_not_object"], values
    values = dict(context)
    for key in _REQUIRED_CONTEXT:
        if key not in context:
            problems.append(f"missing_context_field:{key}")
            continue
        values[key] = context[key]

    claim = values.get("claim")
    if "claim" in values and claim not in CLAIM_STATES:
        problems.append("invalid_context_field:claim")
    issue_number = values.get("issue_number")
    if "issue_number" in values and issue_number is not None:
        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
            problems.append("invalid_context_field:issue_number")
    labels = values.get("labels")
    if "labels" in values and not (
        isinstance(labels, list) and all(isinstance(item, str) for item in labels)
    ):
        problems.append("invalid_context_field:labels")
    assignees = values.get("assignees")
    if "assignees" in values and not (
        isinstance(assignees, list) and all(isinstance(item, str) for item in assignees)
    ):
        problems.append("invalid_context_field:assignees")
    actor = values.get("actor")
    if "actor" in values and actor is not None and not isinstance(actor, str):
        problems.append("invalid_context_field:actor")
    for key in ("open_pr_count", "own_open_pr_count"):
        count = values.get(key)
        if key in values:
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                problems.append(f"invalid_context_field:{key}")
    if "checked_at" in values and _parse_ts(values["checked_at"]) is None:
        problems.append("invalid_context_field:checked_at")
    return problems, values


def _approval_problems(approval: Any, now: datetime, max_age: timedelta) -> list[str]:
    if not isinstance(approval, dict):
        return ["maintainer_approval_missing"]
    problems: list[str] = []
    url = approval.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        problems.append("maintainer_approval_url_invalid")
    author = approval.get("author")
    if not isinstance(author, str) or not author.strip():
        problems.append("maintainer_approval_author_missing")
    association = approval.get("author_association")
    if not isinstance(association, str) or association.upper() not in _APPROVAL_ASSOCIATIONS:
        problems.append("maintainer_approval_association_not_maintainer")
    body = approval.get("body")
    if not isinstance(body, str) or not body.strip():
        problems.append("maintainer_approval_body_missing")
    elif not _APPROVAL_DECISION_RE.search(body):
        problems.append("maintainer_approval_decision_not_explicit")
    freshness = _freshness(approval.get("checked_at"), now, max_age, "maintainer_approval")
    problems.extend(freshness)
    return problems


def _freshness(value: Any, now: datetime, max_age: timedelta, label: str) -> list[str]:
    parsed = _parse_ts(value)
    if parsed is None:
        return [f"{label}_timestamp_unknown"]
    age = now - parsed
    if age < timedelta(minutes=-5):
        return [f"{label}_timestamp_in_future"]
    if age > max_age:
        return [f"{label}_stale"]
    return []


def _labels_lower(labels: list[str]) -> set[str]:
    return {item.strip().casefold() for item in labels if isinstance(item, str) and item.strip()}


def _base_result(route: str, reasons: list[str], resume_when: list[str], stage: str) -> dict[str, Any]:
    return {
        "route": route,
        "implementation_allowed": False,
        "pr_allowed": False,
        "reasons": reasons,
        "resume_when": resume_when,
        "stage": stage,
        "stages": [],
        "requirements": [],
        "evidence": {},
    }


def _hold(reasons: list[str], resume: list[str], stage: str = "hold") -> dict[str, Any]:
    return _base_result("hold", reasons, resume, stage)


def _capacity_problems(rules: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    configured = rules.get("max_open_prs_per_contributor")
    if configured is None:
        return []
    if configured == "unknown":
        return ["own_pr_capacity_unknown"]
    if not isinstance(configured, int) or isinstance(configured, bool) or configured < 0:
        return ["own_pr_capacity_malformed"]
    if not isinstance(ctx.get("actor"), str) or not ctx["actor"].strip():
        return ["own_pr_capacity_actor_unknown"]
    if ctx["own_open_pr_count"] >= configured:
        return [f"own_open_pr_capacity_reached:{configured}"]
    return []


def route(
    candidate: dict[str, Any],
    policy: dict[str, Any],
    context: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Route a candidate according to a pinned policy record and fresh claim context."""
    clock = now if isinstance(now, datetime) else _utc_now()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    clock = clock.astimezone(timezone.utc)

    candidate_problems, cand = _validate_candidate(candidate)
    context_problems, ctx = _validate_context(context)
    if candidate_problems or context_problems:
        result = _hold(
            candidate_problems + context_problems,
            ["supply the missing or malformed fields and re-run policy.route"],
        )
        result["evidence"] = {"now": _fmt_ts(clock)}
        return result

    if not isinstance(policy, dict):
        return _hold(["policy_not_object"], ["load a pinned policy snapshot"])
    if policy.get("status") != "known" or not isinstance(policy.get("rules"), dict):
        reasons = ["policy_unknown"]
        if isinstance(policy.get("reason"), str):
            reasons.append(f"policy_reason:{policy['reason']}")
        return _hold(reasons, ["refresh and verify the current upstream policy source"])

    rules = policy["rules"]
    if rules.get("paused") is True:
        resume = rules.get("resume_conditions") if isinstance(rules.get("resume_conditions"), list) else []
        return _hold(
            ["policy_paused"]
            + ([f"pause_reason:{rules['pause_reason']}"] if rules.get("pause_reason") else []),
            [str(item) for item in resume] or ["maintainer resumes contributions"],
        )
    if rules.get("closed") is True:
        return _hold(
            ["policy_closed"],
            ["recheck the upstream policy; do not publish while closed"],
            stage="closed",
        )

    policy_checked = policy.get("checked_at")
    max_policy_age = timedelta(hours=float(policy.get("max_policy_age_hours", 168)))
    policy_freshness = _freshness(policy_checked, clock, max_policy_age, "policy")
    if policy_freshness:
        return _hold(policy_freshness, ["refresh the policy source snapshot and record a new checked_at"])

    max_context_age = timedelta(minutes=float(policy.get("max_context_age_minutes", 120)))
    context_freshness = _freshness(ctx["checked_at"], clock, max_context_age, "context")
    if context_freshness:
        return _hold(context_freshness, ["re-run claimcheck and refresh context.checked_at"])

    if cand["defect_proven"] is not True:
        return _hold(["defect_not_proven"], ["provide a reproducible contract-backed defect"])

    claim = ctx["claim"]
    if claim == "TAKEN":
        return _hold(
            ["claim_taken"],
            ["wait for the current assignee/PR to resolve, or pick another candidate"],
            stage="claim_collision",
        )
    if claim == "UNKNOWN":
        return _hold(
            ["claim_unknown"],
            ["re-run claimcheck after API reads complete; do not assume FREE"],
            stage="claim_collision",
        )

    assignees = [item for item in ctx["assignees"] if isinstance(item, str)]
    actor = ctx["actor"]
    actor_key = actor.strip().casefold() if isinstance(actor, str) else None
    assignee_keys = {item.strip().casefold() for item in assignees if item.strip()}
    assigned_to_actor = actor_key is not None and actor_key in assignee_keys
    assigned_to_other = bool(assignee_keys) and not assigned_to_actor
    if assigned_to_other:
        return _hold(
            ["assigned_to_other"],
            ["wait for the assignee or select another candidate"],
            stage="claim_collision",
        )
    if ctx["open_pr_count"] > 0:
        return _hold(
            ["open_pr_conflict"],
            ["inspect the existing PR; do not open a duplicate"],
            stage="claim_collision",
        )

    labels = _labels_lower(ctx["labels"])
    exempt_labels = {
        str(item).strip().casefold()
        for item in rules.get("assignment_exempt_labels", [])
        if isinstance(item, str) and item.strip()
    }
    assignment_exempt = bool(labels & exempt_labels)
    required_labels = {
        str(item).strip().casefold()
        for item in rules.get("required_labels", [])
        if isinstance(item, str) and item.strip()
    }
    missing_required = sorted(required_labels - labels)
    required_any_labels = {
        str(item).strip().casefold()
        for item in rules.get("required_any_labels", [])
        if isinstance(item, str) and item.strip()
    }
    missing_any_required = required_any_labels and not (required_any_labels & labels)

    ctype = cand["contribution_type"]
    rule = rules.get(ctype)
    if not isinstance(rule, dict):
        return _hold(
            [f"policy_missing_{ctype}_rule"], ["refresh the policy snapshot for this contribution type"]
        )

    evidence = {
        "now": _fmt_ts(clock),
        "policy_repo": policy.get("canonical_repo"),
        "policy_checked_at": policy_checked,
        "context_checked_at": ctx["checked_at"],
        "source_revisions": [
            source.get("revision")
            for source in policy.get("sources", [])
            if isinstance(source, dict) and source.get("revision")
        ],
        "claim": claim,
    }
    requirements: list[str] = []
    if rules.get("cla_required") is True:
        requirements.append("cla_signature")
    if rules.get("ai_disclosure_required") is True:
        requirements.append("ai_assistance_disclosure")

    if rules.get("issue_reporting_allowed") is False:
        return _hold(
            ["issue_reporting_not_allowed_by_policy"],
            ["do not publish an issue report while the source policy forbids it"],
            stage="report",
        )

    if rules.get("issue_only") is True or cand["report_only"] is True:
        reason = "explicit_issue_only_policy" if rules.get("issue_only") is True else "report_only_requested"
        result = _base_result(
            "issue_only",
            [reason, "defect_proven", "no_implementation_authorized"] + requirements,
            ["publish a verified issue report only if explicitly authorized"],
            stage="report",
        )
        result["implementation_allowed"] = False
        result["pr_allowed"] = False
        result["evidence"] = evidence
        return result

    if cand["design_uncertainties"]:
        return _hold(
            ["design_uncertainty"],
            ["resolve the listed product/contract decisions with maintainers first"],
            stage="design",
        )

    if missing_required:
        return _hold(
            [f"required_label_missing:{label}" for label in missing_required],
            ["wait for the required issue labels"],
            stage="labels",
        )

    issue_number = ctx["issue_number"]
    issue_present = issue_number is not None
    if issue_present:
        evidence["issue_number"] = issue_number

    if rule.get("issue_required") is not True:
        capacity = _capacity_problems(rules, ctx)
        if capacity:
            return _hold(capacity, ["wait until the actor's own open PR slot is available"], stage="capacity")
        if cand["fix_verified"] is not True:
            result = _base_result(
                "direct_pr",
                ["policy_source_verified", f"{ctype}_direct_pr_allowed", "fix_not_verified", "claim_free"]
                + (["link_existing_issue"] if issue_present else ["no_issue_required"])
                + requirements,
                ["produce an evidence-bound verified fix before publishing"],
                stage="implementation",
            )
            result["implementation_allowed"] = True
            result["pr_allowed"] = False
            result["stages"] = ["contract", "implementation", "verified_fix", "dry_run_publish"]
            result["evidence"] = evidence
            return result
        result = _base_result(
            "direct_pr",
            ["policy_source_verified", f"{ctype}_direct_pr_allowed", "fix_verified", "claim_free"]
            + (["link_existing_issue"] if issue_present else ["no_issue_required"])
            + requirements,
            ["prepare a dry-run publication plan; user authorization is still required"],
            stage="publish",
        )
        result["implementation_allowed"] = True
        result["pr_allowed"] = True
        result["stages"] = ["contract", "verified_fix", "dry_run_publish"]
        result["evidence"] = evidence
        return result

    stages = ["issue"]
    if rule.get("ready_label_required") is True:
        stages.append("ready_label")
    if rule.get("assignment_required") is True:
        stages.append("assignment")
    if rules.get("claim_comment_required") is True:
        stages.append("claim")
    if rule.get("design_approval_required") is True:
        stages.append("design_approval")
    stages.extend(["implementation", "verified_fix", "dry_run_publish"])

    if not issue_present:
        if rule.get("ready_label_required") is True:
            result = _base_result(
                "issue_then_wait",
                ["policy_requires_issue", "issue_absent", f"ready_label_required:{rule.get('ready_label')}"]
                + requirements,
                [
                    "open an issue with the reproduction, expected behavior and contract source",
                    f"wait for maintainer label: {rule.get('ready_label')}",
                ],
                stage="issue",
            )
        else:
            result = _base_result(
                "issue_first",
                ["policy_requires_issue", "issue_absent", f"{ctype}_issue_first"] + requirements,
                ["open an issue with the reproduction, expected behavior and contract source"],
                stage="issue",
            )
        result["stages"] = stages
        result["evidence"] = evidence
        return result

    capacity = _capacity_problems(rules, ctx)
    if capacity:
        return _hold(capacity, ["wait until the actor's own open PR slot is available"], stage="capacity")

    if missing_any_required:
        result = _base_result(
            "issue_then_wait",
            ["issue_present", "required_any_labels_missing", "implementation_blocked"] + requirements,
            ["wait for one of the required issue labels: " + ", ".join(sorted(required_any_labels))],
            stage="labels",
        )
        result["stages"] = stages
        result["evidence"] = evidence
        return result

    ready_label = rule.get("ready_label")
    ready_ok = True
    if rule.get("ready_label_required") is True:
        ready_ok = isinstance(ready_label, str) and ready_label.strip().casefold() in labels
    if not ready_ok:
        result = _base_result(
            "issue_then_wait",
            ["issue_present", f"ready_label_missing:{ready_label}", "implementation_blocked"] + requirements,
            [f"wait for maintainer label: {ready_label}"],
            stage="ready_label",
        )
        result["stages"] = stages
        result["evidence"] = evidence
        return result

    if rule.get("design_approval_required") is True:
        approval = ctx.get("maintainer_approval")
        approval_ok = ready_ok and rule.get("ready_label_required") is True
        approval_problems: list[str] = []
        if not approval_ok:
            approval_problems = _approval_problems(approval, clock, max_context_age)
            approval_ok = not approval_problems
        if not approval_ok:
            result = _base_result(
                "issue_then_wait",
                ["issue_present", "design_approval_required", "implementation_blocked"]
                + approval_problems
                + requirements,
                ["obtain a provenance-bearing maintainer approval, or the required ready label"],
                stage="design_approval",
            )
            result["stages"] = stages
            result["evidence"] = evidence
            return result
        evidence["maintainer_approval"] = {
            "author": approval.get("author") if isinstance(ctx.get("maintainer_approval"), dict) else None,
            "author_association": (
                ctx["maintainer_approval"].get("author_association")
                if isinstance(ctx.get("maintainer_approval"), dict)
                else None
            ),
            "checked_at": (
                ctx["maintainer_approval"].get("checked_at")
                if isinstance(ctx.get("maintainer_approval"), dict)
                else None
            ),
        }

    if rule.get("assignment_required") is True and not (assigned_to_actor or assignment_exempt):
        if rule.get("linked_pr_before_assignment_allowed") is True:
            result = _base_result(
                "issue_first",
                [
                    "issue_present",
                    "assignment_absent",
                    "linked_pr_before_assignment_allowed",
                    "do_not_claim_assigned",
                    f"{ctype}_issue_first",
                ]
                + requirements,
                [
                    "maintainer assigns the actor",
                    "or the issue carries an assignment-exempt label",
                    "linked PR stays gate-waiting until then",
                ],
                stage="linked_pr_waiting",
            )
            result["implementation_allowed"] = True
            result["pr_allowed"] = cand["fix_verified"] is True
            result["stages"] = stages
            result["evidence"] = evidence
            if cand["fix_verified"] is not True:
                result["resume_when"].append("produce an evidence-bound verified fix")
            return result
        result = _base_result(
            "issue_then_wait",
            ["issue_present", "assignment_absent", "linked_pr_before_assignment_disallowed"] + requirements,
            ["maintainer assigns the actor to the issue"],
            stage="assignment",
        )
        result["stages"] = stages
        result["evidence"] = evidence
        return result

    if rules.get("claim_comment_required") is True and not assigned_to_actor:
        result = _base_result(
            "issue_first",
            ["issue_present", "claim_comment_required", "claim_not_recorded"] + requirements,
            ["post a claim on the issue and wait for maintainer acknowledgement"],
            stage="claim",
        )
        result["stages"] = stages
        result["evidence"] = evidence
        return result

    result = _base_result(
        "issue_first",
        ["issue_present", f"{ctype}_issue_first", "claim_free"]
        + (["assigned_to_actor"] if assigned_to_actor else [])
        + (["assignment_exempt_label"] if assignment_exempt else [])
        + requirements,
        ["prepare the scoped change against the linked issue"],
        stage="implementation",
    )
    result["implementation_allowed"] = True
    result["pr_allowed"] = cand["fix_verified"] is True
    result["stages"] = stages
    result["evidence"] = evidence
    if cand["fix_verified"] is not True:
        result["resume_when"].append("produce an evidence-bound verified fix before opening the PR")
    return result


def check_samples(
    path: str | Path | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Route the pinned sample set and compare each result with its expectation."""
    policy_path = Path(path) if path is not None else default_policy_path(workspace)
    try:
        with policy_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"policy_file_unreadable:{type(exc).__name__}",
            "results": [],
        }
    samples = document.get("samples")
    if not isinstance(samples, list):
        return {"ok": False, "error": "samples_missing", "results": []}
    sample_clock = _parse_ts(document.get("sample_clock"))
    if sample_clock is None:
        return {"ok": False, "error": "sample_clock_missing", "results": []}

    results: list[dict[str, Any]] = []
    all_ok = True
    for sample in samples:
        if not isinstance(sample, dict):
            results.append({"name": None, "ok": False, "error": "sample_malformed"})
            all_ok = False
            continue
        name = sample.get("name")
        repo = sample.get("repo")
        expected = sample.get("expected_route")
        policy_obj = load_policy(repo, policy_path)
        routed = route(sample.get("candidate"), policy_obj, sample.get("context"), now=sample_clock)
        ok = routed["route"] == expected
        all_ok = all_ok and ok
        results.append(
            {
                "name": name,
                "repo": policy_obj.get("canonical_repo"),
                "expected_route": expected,
                "actual_route": routed["route"],
                "ok": ok,
                "implementation_allowed": routed["implementation_allowed"],
                "pr_allowed": routed["pr_allowed"],
                "stage": routed["stage"],
                "reasons": routed["reasons"],
                "resume_when": routed["resume_when"],
            }
        )
    return {
        "ok": all_ok,
        "sample_clock": _fmt_ts(sample_clock),
        "policy_generated_at": document.get("generated_at"),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Route contribution candidates using pinned upstream policy snapshots."
    )
    parser.add_argument(
        "--check-samples", action="store_true", help="route the pinned sample set and verify expected routes"
    )
    parser.add_argument("--workspace", type=Path, default=None, help="Workspace root")
    parser.add_argument("--policy", default=None, help="Policy snapshot path (default: policies/repos.json)")
    parser.add_argument("--json", action="store_true", help="emit JSON (default for --check-samples)")
    args = parser.parse_args(argv)

    if not args.check_samples:
        parser.print_help()
        return 2

    report = check_samples(args.policy, args.workspace)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

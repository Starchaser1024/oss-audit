import copy
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from oss_audit.policy import router as policy

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "policies" / "repos.json"
SAMPLE_NOW = datetime(2026, 10, 27, 9, 27, 19, tzinfo=timezone.utc)


def candidate(**overrides):
    value = {
        "candidate_id": "test-candidate",
        "repo": "livekit/agents",
        "base_sha": "a" * 40,
        "defect_proven": True,
        "fix_verified": True,
        "design_uncertainties": [],
        "contribution_type": "bugfix",
        "report_only": False,
    }
    value.update(overrides)
    return value


def context(**overrides):
    value = {
        "claim": "FREE",
        "issue_number": None,
        "labels": [],
        "assignees": [],
        "actor": "tester",
        "open_pr_count": 0,
        "own_open_pr_count": 0,
        "checked_at": "2026-10-27T09:25:00Z",
    }
    value.update(overrides)
    return value


def approval(**overrides):
    value = {
        "url": "https://github.com/bytedance/deer-flow/issues/9001#issuecomment-1",
        "author": "maintainer",
        "author_association": "MEMBER",
        "body": "Approved. Ready for implementation.",
        "checked_at": "2026-10-27T09:25:00Z",
    }
    value.update(overrides)
    return value


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy.load_policy("livekit/agents", POLICY_PATH)

    def test_alias_canonicalized(self):
        loaded = policy.load_policy("modelscope/agentscope", POLICY_PATH)
        self.assertEqual(loaded["status"], "known")
        self.assertEqual(loaded["canonical_repo"], "agentscope-ai/agentscope")

    def test_unknown_repo_is_not_inferred_from_repos_json(self):
        loaded = policy.load_policy("Skyvern-AI/skyvern", POLICY_PATH)
        self.assertEqual(loaded["status"], "unknown")
        self.assertIn("repo_not_in_policy_snapshot", loaded["reason"])

    def test_livekit_bugfix_direct_pr(self):
        result = policy.route(candidate(), self.policy, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "direct_pr")
        self.assertTrue(result["implementation_allowed"])
        self.assertTrue(result["pr_allowed"])
        self.assertIn("bugfix_direct_pr_allowed", result["reasons"])

    def test_livekit_bugfix_without_verified_fix_allows_implementation_only(self):
        result = policy.route(candidate(fix_verified=False), self.policy, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "direct_pr")
        self.assertTrue(result["implementation_allowed"])
        self.assertFalse(result["pr_allowed"])
        self.assertIn("fix_not_verified", result["reasons"])

    def test_livekit_feature_waits_for_design(self):
        loaded = policy.load_policy("livekit/agents", POLICY_PATH)
        result = policy.route(
            candidate(contribution_type="feature"),
            loaded,
            context(issue_number=10),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_then_wait")
        self.assertFalse(result["implementation_allowed"])
        self.assertIn("design_approval_required", result["reasons"])

    def test_fastmcp_allows_linked_pr_waiting_without_claiming_assignment(self):
        loaded = policy.load_policy("PrefectHQ/fastmcp", POLICY_PATH)
        result = policy.route(
            candidate(repo="PrefectHQ/fastmcp"),
            loaded,
            context(issue_number=5842, labels=["bug"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_first")
        self.assertTrue(result["implementation_allowed"])
        self.assertTrue(result["pr_allowed"])
        self.assertIn("assignment_absent", result["reasons"])
        self.assertIn("linked_pr_before_assignment_allowed", result["reasons"])
        self.assertIn("do_not_claim_assigned", result["reasons"])
        self.assertFalse(any("assigned_to_actor" in reason for reason in result["reasons"]))

    def test_fastmcp_assignment_exempt_label(self):
        loaded = policy.load_policy("PrefectHQ/fastmcp", POLICY_PATH)
        result = policy.route(
            candidate(repo="PrefectHQ/fastmcp"),
            loaded,
            context(issue_number=5842, labels=["prs welcome"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_first")
        self.assertTrue(result["implementation_allowed"])
        self.assertIn("assignment_exempt_label", result["reasons"])

    def test_fastmcp_assigned_to_actor(self):
        loaded = policy.load_policy("PrefectHQ/fastmcp", POLICY_PATH)
        result = policy.route(
            candidate(repo="PrefectHQ/fastmcp"),
            loaded,
            context(issue_number=5842, assignees=["tester"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_first")
        self.assertIn("assigned_to_actor", result["reasons"])

    def test_agno_waits_for_ready_label(self):
        loaded = policy.load_policy("agno-agi/agno", POLICY_PATH)
        result = policy.route(
            candidate(repo="agno-agi/agno"),
            loaded,
            context(issue_number=11137, labels=["bug"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_then_wait")
        self.assertFalse(result["implementation_allowed"])
        self.assertIn("ready_label_missing:ready", result["reasons"])

    def test_agno_ready_label_unblocks(self):
        loaded = policy.load_policy("agno-agi/agno", POLICY_PATH)
        result = policy.route(
            candidate(repo="agno-agi/agno"),
            loaded,
            context(issue_number=11137, labels=["bug", "ready"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_first")
        self.assertTrue(result["implementation_allowed"])
        self.assertTrue(result["pr_allowed"])

    def test_agentscope_requires_claim_comment(self):
        loaded = policy.load_policy("modelscope/agentscope", POLICY_PATH)
        result = policy.route(
            candidate(repo="modelscope/agentscope"),
            loaded,
            context(issue_number=7000, labels=["Help Wanted"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_first")
        self.assertFalse(result["implementation_allowed"])
        self.assertIn("claim_comment_required", result["reasons"])

    def test_report_only_is_issue_only(self):
        loaded = policy.load_policy("deepset-ai/haystack", POLICY_PATH)
        result = policy.route(
            candidate(repo="deepset-ai/haystack", report_only=True, fix_verified=False),
            loaded,
            context(),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_only")
        self.assertFalse(result["pr_allowed"])
        self.assertFalse(result["implementation_allowed"])

    def test_missing_candidate_fields_hold(self):
        broken = candidate()
        del broken["fix_verified"]
        result = policy.route(broken, self.policy, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("missing_candidate_field:fix_verified", result["reasons"])

    def test_missing_context_fields_hold(self):
        broken = context()
        del broken["claim"]
        result = policy.route(candidate(), self.policy, broken, now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("missing_context_field:claim", result["reasons"])

    def test_stale_context_holds(self):
        stale = context(checked_at="2026-10-25T09:25:00Z")
        result = policy.route(candidate(), self.policy, stale, now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("context_stale", result["reasons"])

    def test_stale_policy_holds(self):
        stale_policy = copy.deepcopy(self.policy)
        stale_policy["checked_at"] = "2026-01-01T00:00:00Z"
        result = policy.route(candidate(), stale_policy, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("policy_stale", result["reasons"])

    def test_policy_source_error_holds(self):
        failed = copy.deepcopy(self.policy)
        failed["status"] = "unknown"
        failed["source_status"] = "error"
        result = policy.route(candidate(), failed, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("policy_unknown", result["reasons"])

    def test_claim_taken_or_unknown_holds(self):
        for claim in ("TAKEN", "UNKNOWN"):
            result = policy.route(candidate(), self.policy, context(claim=claim), now=SAMPLE_NOW)
            self.assertEqual(result["route"], "hold")
            self.assertIn(f"claim_{claim.lower()}", result["reasons"])
            self.assertEqual(result["stage"], "claim_collision")

    def test_open_pr_conflict_holds(self):
        result = policy.route(candidate(), self.policy, context(open_pr_count=1), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("open_pr_conflict", result["reasons"])
        self.assertEqual(result["stage"], "claim_collision")

    def test_design_uncertainty_holds(self):
        result = policy.route(
            candidate(design_uncertainties=["product decision required"]),
            self.policy,
            context(),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "hold")
        self.assertIn("design_uncertainty", result["reasons"])

    def test_agno_issue_absent_waits_for_issue_and_ready(self):
        loaded = policy.load_policy("agno-agi/agno", POLICY_PATH)
        result = policy.route(candidate(repo="agno-agi/agno"), loaded, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "issue_then_wait")
        self.assertFalse(result["implementation_allowed"])
        self.assertIn("issue_absent", result["reasons"])
        self.assertIn("wait for maintainer label: ready", result["resume_when"])

    def test_haystack_required_any_labels(self):
        loaded = policy.load_policy("deepset-ai/haystack", POLICY_PATH)
        allowed = policy.route(
            candidate(repo="deepset-ai/haystack"),
            loaded,
            context(issue_number=9002, labels=["good first issue"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(allowed["route"], "issue_first")
        blocked = policy.route(
            candidate(repo="deepset-ai/haystack"),
            loaded,
            context(issue_number=9002, labels=["bug"]),
            now=SAMPLE_NOW,
        )
        self.assertEqual(blocked["route"], "issue_then_wait")
        self.assertIn("required_any_labels_missing", blocked["reasons"])

    def test_haystack_own_pr_capacity_holds(self):
        loaded = policy.load_policy("deepset-ai/haystack", POLICY_PATH)
        result = policy.route(
            candidate(repo="deepset-ai/haystack"),
            loaded,
            context(issue_number=9002, labels=["good first issue"], own_open_pr_count=1),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "hold")
        self.assertIn("own_open_pr_capacity_reached:1", result["reasons"])

    def test_unknown_capacity_when_configured_holds(self):
        configured = copy.deepcopy(self.policy)
        configured["rules"]["max_open_prs_per_contributor"] = "unknown"
        result = policy.route(candidate(), configured, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("own_pr_capacity_unknown", result["reasons"])

    def test_design_approval_with_provenance_unlocks(self):
        loaded = policy.load_policy("bytedance/deer-flow", POLICY_PATH)
        result = policy.route(
            candidate(repo="bytedance/deer-flow", contribution_type="feature"),
            loaded,
            context(issue_number=9001, maintainer_approval=approval()),
            now=SAMPLE_NOW,
        )
        self.assertEqual(result["route"], "issue_first")
        self.assertTrue(result["implementation_allowed"])
        self.assertTrue(result["pr_allowed"])
        self.assertEqual(result["evidence"]["maintainer_approval"]["author"], "maintainer")

    def test_unprovenanced_or_automatic_approval_does_not_unlock(self):
        loaded = policy.load_policy("bytedance/deer-flow", POLICY_PATH)
        cases = [
            approval(author_association="NONE"),
            approval(body="Automated triage added labels; no decision."),
            approval(body="I'll take a look."),
            approval(checked_at="2026-10-25T09:25:00Z"),
            {"url": "only words", "body": "approved"},
        ]
        for value in cases:
            result = policy.route(
                candidate(repo="bytedance/deer-flow", contribution_type="feature"),
                loaded,
                context(issue_number=9001, maintainer_approval=value),
                now=SAMPLE_NOW,
            )
            self.assertEqual(result["route"], "issue_then_wait", value)
            self.assertFalse(result["implementation_allowed"])

    def test_explicit_issue_only_policy_and_reporting_restriction(self):
        issue_only = copy.deepcopy(self.policy)
        issue_only["rules"]["issue_only"] = True
        result = policy.route(candidate(), issue_only, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "issue_only")
        self.assertIn("explicit_issue_only_policy", result["reasons"])

        forbidden = copy.deepcopy(issue_only)
        forbidden["rules"]["issue_reporting_allowed"] = False
        result = policy.route(candidate(report_only=True), forbidden, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("issue_reporting_not_allowed_by_policy", result["reasons"])

    def test_closed_policy_holds_even_report_only(self):
        closed = copy.deepcopy(self.policy)
        closed["rules"]["closed"] = True
        result = policy.route(candidate(report_only=True), closed, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("policy_closed", result["reasons"])

    def test_paused_policy_holds_even_report_only(self):
        paused = copy.deepcopy(self.policy)
        paused["rules"]["paused"] = True
        result = policy.route(candidate(report_only=True), paused, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("policy_paused", result["reasons"])

    def test_defect_not_proven_holds(self):
        result = policy.route(candidate(defect_proven=False), self.policy, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("defect_not_proven", result["reasons"])

    def test_paused_policy_holds(self):
        paused = copy.deepcopy(self.policy)
        paused["rules"]["paused"] = True
        paused["rules"]["pause_reason"] = "maintainer pause"
        result = policy.route(candidate(), paused, context(), now=SAMPLE_NOW)
        self.assertEqual(result["route"], "hold")
        self.assertIn("policy_paused", result["reasons"])

    def test_route_result_is_json_safe(self):
        result = policy.route(candidate(), self.policy, context(), now=SAMPLE_NOW)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(encoded, str)
        for key in ("route", "implementation_allowed", "pr_allowed", "reasons", "resume_when"):
            self.assertIn(key, result)

    def test_check_samples_all_expected(self):
        report = policy.check_samples(POLICY_PATH)
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False, indent=2))
        routes = {item["name"]: item["actual_route"] for item in report["results"]}
        self.assertEqual(routes["livekit_bugfix_direct_pr"], "direct_pr")
        self.assertEqual(routes["fastmcp_bugfix_linked_pr_waiting"], "issue_first")
        self.assertEqual(routes["agno_bugfix_wait_for_ready"], "issue_then_wait")
        self.assertEqual(routes["haystack_report_only_issue"], "issue_only")
        self.assertEqual(routes["unknown_repo_hold"], "hold")
        self.assertEqual(routes["agentscope_bugfix_claim_required"], "issue_first")

    def test_cli_check_samples(self):
        completed = subprocess.run(
            [sys.executable, "-m", "oss_audit", "policy", "--check-samples"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()

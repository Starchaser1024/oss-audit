"""Unit and CLI tests for candidate lifecycle schemas and independent falsification gates."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from oss_audit import cli
from oss_audit.core.common import write_json
from oss_audit.core.paths import REPO_ROOT
from oss_audit.verification.lifecycle import (
    CONTRACT_SOURCE_TYPES,
    FALSIFICATION_BOOLEAN_CHECKS,
    is_test_path,
    is_valid_line_anchor,
    summarize_dossiers,
    validate_dossier_ledger,
    validate_dossier_schema,
)
from oss_audit.verification.verify import (
    verify_candidate_dossier,
    verify_candidate_falsification_suite,
)


def _valid_confirmed_dossier() -> dict[str, Any]:
    return {
        "candidate_id": "haystack--async-retriever-sync-store-guard",
        "upstream_repo": "deepset-ai/haystack",
        "pinned_commit": "34a1a09",
        "contract_provenance": [
            {
                "source_type": "typing_protocol",
                "path": "haystack/document_stores/types/protocol.py",
                "line_anchor": "L28-L78",
                "claim": "DocumentStore requires synchronous filter_documents; async variant is optional.",
            },
            {
                "source_type": "sibling_implementation_symmetry",
                "path": "haystack/components/writers/document_writer.py",
                "line_anchor": "L142-L165",
                "claim": "Sibling async components guard missing async store methods explicitly.",
            },
        ],
        "production_entrypoint": {
            "kind": "public_api",
            "symbol": "FilterRetriever.run_async",
            "call_chain": [
                "AsyncPipeline.run_async",
                "FilterRetriever.run_async",
                "_call_with_async_fallback",
            ],
            "reachability": "proven",
        },
        "minimal_reproduction": {
            "test_nodeid": "test/components/retrievers/test_filter_retriever_async.py::test_sync_only_store",
            "input_summary": "Invoke FilterRetriever.run_async against a sync-only DocumentStore.",
            "expected_on_contract": "Documents filtered via worker-thread fallback without AttributeError.",
            "observed_on_base": "AttributeError: 'MockedDocumentStore' object has no attribute 'filter_documents_async'.",
            "base_outcome": "failed",
            "patched_outcome": "passed",
        },
        "normal_behavior_counterexample": {
            "test_nodeid": "test/components/retrievers/test_filter_retriever_async.py::test_native_async_store",
            "normal_input_summary": "Invoke FilterRetriever.run_async with an async-capable InMemoryDocumentStore.",
            "base_outcome": "passed",
            "patched_outcome": "passed",
            "rationale": "Ensures async-capable stores still dispatch native filter_documents_async directly.",
        },
        "falsification_log": {
            "reviewer": "Codex",
            "shadowed_import_checked": True,
            "overfitted_patch_checked": True,
            "faulty_test_assertion_checked": True,
            "stale_evidence_checked": True,
            "upstream_collision_checked": True,
            "overfitting_signals": [],
            "upstream_collision": None,
            "disproved_reason": None,
        },
        "terminal_verdict": "confirmed",
        "maintainer_feedback": {
            "status": "merged",
            "maintainer_notes": "Maintainer requested shared worker-thread helper and release note adjustment; merged 76fc2d3d7.",
            "requested_evidence": [
                "Cover FilterRetriever, AutoMergingRetriever, SentenceWindowRetriever, CacheChecker, and DocumentWriter",
                "Add Reno release note for async fallback helper",
            ],
            "rule_update_derived": "Verify whether sibling protocol guards prefer thread-pool fallback over TypeError before drafting fixes.",
        },
    }


class LifecycleHelperTests(unittest.TestCase):
    def test_is_test_path_identifies_unix_windows_and_url_test_paths(self):
        self.assertTrue(is_test_path("tests/test_parser.py"))
        self.assertTrue(is_test_path("test\\components\\test_retriever.py"))
        self.assertTrue(is_test_path("src/pkg/rag_parser_test.py"))
        self.assertTrue(is_test_path("conftest.py"))
        self.assertTrue(
            is_test_path("https://github.com/org/repo/blob/abc1234/tests/unit/test_model.py#L42")
        )
        self.assertFalse(is_test_path("src/agentscope/rag/_parser/_word.py"))
        self.assertFalse(is_test_path("haystack/utils/async_utils.py"))
        self.assertFalse(is_test_path("CONTRIBUTING.md"))

    def test_is_valid_line_anchor_enforces_ordered_positive_ranges(self):
        self.assertTrue(is_valid_line_anchor("L1"))
        self.assertTrue(is_valid_line_anchor("L54-L72"))
        self.assertTrue(is_valid_line_anchor("L178-190"))
        self.assertFalse(is_valid_line_anchor("L0"))
        self.assertFalse(is_valid_line_anchor("L80-L20"))
        self.assertFalse(is_valid_line_anchor("lines 10-20"))
        self.assertFalse(is_valid_line_anchor(""))


class CandidateFalsificationGateTests(unittest.TestCase):
    def test_valid_confirmed_dossier_passes_schema_and_falsification(self):
        dossier = _valid_confirmed_dossier()
        self.assertTrue(validate_dossier_schema(dossier)["ok"])
        audit = verify_candidate_dossier(dossier)
        self.assertTrue(audit["ok"], audit)
        self.assertEqual(audit["valid_production_contract_sources"], 2)

    def test_all_five_contract_source_types_accepted_on_production_files(self):
        for stype in CONTRACT_SOURCE_TYPES:
            dossier = _valid_confirmed_dossier()
            dossier["contract_provenance"] = [
                {
                    "source_type": stype,
                    "path": "src/package/module.py",
                    "line_anchor": "L10-L25",
                    "claim": f"Explicit contract via {stype}",
                }
            ]
            res = verify_candidate_dossier(dossier)
            self.assertTrue(res["ok"], f"{stype} failed: {res}")

    def test_rejects_test_only_contract_provenance_fail_closed(self):
        dossier = _valid_confirmed_dossier()
        dossier["contract_provenance"] = [
            {
                "source_type": "docstring",
                "path": "tests/test_filter_retriever_async.py",
                "line_anchor": "L12-L30",
                "claim": "Investigator wrote a failing unit test.",
            }
        ]
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn(
            "investigator_test_is_not_a_contract:tests/test_filter_retriever_async.py",
            res["errors"],
        )
        self.assertIn("contract_provenance_cites_test_file", res["errors"])
        self.assertIn("contract_provenance_missing_production_source", res["errors"])

    def test_rejects_test_file_smuggled_alongside_valid_production_contract(self):
        dossier = _valid_confirmed_dossier()
        dossier["contract_provenance"].append(
            {
                "source_type": "sibling_implementation_symmetry",
                "path": "tests/rag_parser_test.py",
                "line_anchor": "L1-L20",
                "claim": "Test assertion treated as contract.",
            }
        )
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("contract_provenance_cites_test_file", res["errors"])

    def test_rejects_unknown_contract_source_type(self):
        dossier = _valid_confirmed_dossier()
        dossier["contract_provenance"][0]["source_type"] = "llm_intuition"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("invalid_contract_source_type:llm_intuition", res["errors"])

    def test_rejects_confirmed_without_proven_entrypoint_reachability(self):
        dossier = _valid_confirmed_dossier()
        dossier["production_entrypoint"]["reachability"] = "partial"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("confirmed_requires_proven_entrypoint", res["errors"])

    def test_rejects_confirmed_when_base_reproduction_did_not_fail(self):
        dossier = _valid_confirmed_dossier()
        dossier["minimal_reproduction"]["base_outcome"] = "passed"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("confirmed_requires_failing_base_reproduction", res["errors"])

    def test_rejects_confirmed_when_patched_reproduction_did_not_pass(self):
        dossier = _valid_confirmed_dossier()
        dossier["minimal_reproduction"]["patched_outcome"] = "failed"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("confirmed_requires_passing_patched_reproduction", res["errors"])

    def test_rejects_confirmed_when_normal_behavior_fails_on_base(self):
        dossier = _valid_confirmed_dossier()
        dossier["normal_behavior_counterexample"]["base_outcome"] = "failed"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("normal_behavior_not_passing_on_base", res["errors"])

    def test_rejects_confirmed_when_normal_behavior_regresses_on_patch(self):
        dossier = _valid_confirmed_dossier()
        dossier["normal_behavior_counterexample"]["patched_outcome"] = "failed"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("normal_behavior_not_preserved_on_patch", res["errors"])

    def test_rejects_confirmed_when_normal_counterexample_reuses_bug_nodeid(self):
        dossier = _valid_confirmed_dossier()
        dossier["normal_behavior_counterexample"]["test_nodeid"] = dossier["minimal_reproduction"][
            "test_nodeid"
        ]
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("normal_counterexample_must_be_distinct_from_bug_repro", res["errors"])

    def test_each_missing_falsification_boolean_check_blocks_confirmation(self):
        for check_key in FALSIFICATION_BOOLEAN_CHECKS:
            dossier = _valid_confirmed_dossier()
            dossier["falsification_log"][check_key] = False
            res = verify_candidate_dossier(dossier)
            self.assertFalse(res["ok"], f"Expected {check_key}=False to block confirmation")
            self.assertIn(f"incomplete_falsification_check:{check_key}", res["errors"])

    def test_rejects_confirmed_when_upstream_collision_detected(self):
        dossier = _valid_confirmed_dossier()
        dossier["falsification_log"]["upstream_collision"] = "bytedance/deer-flow#7493"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("confirmed_blocked_by_upstream_collision", res["errors"])

    def test_rejects_confirmed_when_overfitting_signals_or_heuristic_strategy_present(self):
        dossier = _valid_confirmed_dossier()
        dossier["falsification_log"]["overfitting_signals"] = ["hardcoded_input_literal"]
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("overfitted_patch_blocked_from_confirmation", res["errors"])

        dossier2 = _valid_confirmed_dossier()
        dossier2["patch_strategy"] = {"kind": "static_denylist"}
        res2 = verify_candidate_dossier(dossier2)
        self.assertFalse(res2["ok"])
        self.assertIn("overfitted_patch_strategy:static_denylist", res2["errors"])

    def test_counterexample_replay_blocks_naive_selector_patches_and_accepts_catalog_fix(self):
        cache: dict[str, dict[str, Any]] = {}
        for bad_rev in ("original", "article_patch", "wordlist_patch"):
            dossier = _valid_confirmed_dossier()
            dossier["counterexample_replay"] = {
                "suite": "historical-counterexamples",
                "evaluated_revision": bad_rev,
            }
            res = verify_candidate_dossier(dossier, _replay_cache=cache)
            self.assertFalse(res["ok"])
            self.assertIn(f"counterexample_replay_rejected:{bad_rev}", res["errors"])

        good = _valid_confirmed_dossier()
        good["counterexample_replay"] = {
            "suite": "historical-counterexamples",
            "evaluated_revision": "catalog_fix",
        }
        res_good = verify_candidate_dossier(good, _replay_cache=cache)
        self.assertTrue(res_good["ok"], res_good)

    def test_valid_excluded_dossier_verified_against_rejected_counterexample_replay(self):
        excluded = _valid_confirmed_dossier()
        excluded["candidate_id"] = "deer-flow--naive-wordlist-selector"
        excluded["upstream_repo"] = "bytedance/deer-flow"
        excluded["pinned_commit"] = "68c9fce5c424b0e96986a0ca365db73a1eef8196"
        excluded["normal_behavior_counterexample"]["patched_outcome"] = "failed"
        excluded["falsification_log"]["overfitting_signals"] = [
            "static_noun_denylist_fails_unseen_causal_and_econometric_prompts"
        ]
        excluded["falsification_log"]["disproved_reason"] = (
            "Static word-list patch 2bc504b9ba failed 4 of 13 counterexamples on unseen adjectives and token overlap."
        )
        excluded["terminal_verdict"] = "excluded"
        excluded["maintainer_feedback"] = {
            "status": "not_submitted_prevented",
            "maintainer_notes": "Blocked locally by historical-counterexamples replay prior to publication.",
            "requested_evidence": [],
            "rule_update_derived": "Require 7 parser counterexample categories when a runtime catalog exists.",
        }
        excluded["counterexample_replay"] = {
            "suite": "historical-counterexamples",
            "evaluated_revision": "wordlist_patch",
        }
        res = verify_candidate_dossier(excluded)
        self.assertTrue(res["ok"], res)

    def test_excluded_dossier_cannot_falsely_claim_passing_revision_was_rejected(self):
        bogus_excluded = _valid_confirmed_dossier()
        bogus_excluded["terminal_verdict"] = "excluded"
        bogus_excluded["falsification_log"]["disproved_reason"] = "Claimed disproved without failure."
        bogus_excluded["maintainer_feedback"]["status"] = "not_submitted_prevented"
        bogus_excluded["counterexample_replay"] = {
            "suite": "historical-counterexamples",
            "evaluated_revision": "catalog_fix",
        }
        res = verify_candidate_dossier(bogus_excluded)
        self.assertFalse(res["ok"])
        self.assertIn("excluded_replay_was_not_rejected:catalog_fix", res["errors"])

    def test_excluded_verdict_requires_disproved_reason_and_forbids_merged_status(self):
        dossier = _valid_confirmed_dossier()
        dossier["terminal_verdict"] = "excluded"
        dossier["falsification_log"]["disproved_reason"] = ""
        dossier["maintainer_feedback"]["status"] = "merged"
        res = verify_candidate_dossier(dossier)
        self.assertFalse(res["ok"])
        self.assertIn("excluded_verdict_requires_disproved_reason", res["errors"])
        self.assertIn("incompatible_verdict_and_feedback:excluded->merged", res["errors"])

    def test_covered_upstream_and_issue_only_dossiers_enforce_lifecycle_boundaries(self):
        covered = _valid_confirmed_dossier()
        covered["candidate_id"] = "deer-flow--doc-read-cancel-7491"
        covered["terminal_verdict"] = "covered_upstream"
        covered["falsification_log"]["upstream_collision"] = "bytedance/deer-flow#7493"
        covered["maintainer_feedback"]["status"] = "not_submitted_prevented"
        self.assertTrue(verify_candidate_dossier(covered)["ok"])

        issue_only = _valid_confirmed_dossier()
        issue_only["candidate_id"] = "skyvern--split-pdf-token-budget-9098"
        issue_only["terminal_verdict"] = "design_tradeoff_issue_only"
        issue_only["minimal_reproduction"]["patched_outcome"] = "not_applicable"
        issue_only["normal_behavior_counterexample"]["patched_outcome"] = "not_applicable"
        issue_only["maintainer_feedback"]["status"] = "published_issue"
        self.assertTrue(verify_candidate_dossier(issue_only)["ok"])

        bad_issue_only = copy.deepcopy(issue_only)
        bad_issue_only["minimal_reproduction"]["patched_outcome"] = "passed"
        bad_issue_only["maintainer_feedback"]["status"] = "merged"
        bad_res = verify_candidate_dossier(bad_issue_only)
        self.assertFalse(bad_res["ok"])
        self.assertIn("issue_only_must_not_claim_verified_patch", bad_res["errors"])
        self.assertIn(
            "incompatible_verdict_and_feedback:design_tradeoff_issue_only->merged", bad_res["errors"]
        )

    def test_changes_requested_and_closed_by_design_require_rule_update_derived(self):
        for status in ("changes_requested", "closed_by_design"):
            dossier = _valid_confirmed_dossier()
            dossier["maintainer_feedback"]["status"] = status
            dossier["maintainer_feedback"]["rule_update_derived"] = "   "
            res = validate_dossier_schema(dossier)
            self.assertFalse(res["ok"])
            self.assertIn(f"feedback_status_requires_rule_update:{status}", res["errors"])

    def test_duplicate_candidate_ids_rejected_in_ledger_and_suite(self):
        d1 = _valid_confirmed_dossier()
        d2 = _valid_confirmed_dossier()
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "dossiers.json"
            write_json(ledger_path, {"schema_version": 1, "dossiers": [d1, d2]})
            ledger_res = validate_dossier_ledger(ledger_path)
            self.assertFalse(ledger_res["ok"])
            self.assertIn(f"duplicate_candidate_id:{d1['candidate_id']}", ledger_res["errors"])

            suite_res = verify_candidate_falsification_suite(ledger_path)
            self.assertFalse(suite_res["ok"])
            self.assertIn(f"duplicate_candidate_id:{d1['candidate_id']}", suite_res["errors"])

    def test_cli_dossier_and_verify_dossier_roundtrip(self):
        d1 = _valid_confirmed_dossier()
        d2 = _valid_confirmed_dossier()
        d2["candidate_id"] = "agno--union-field-ready-gate-11144"
        d2["terminal_verdict"] = "covered_upstream"
        d2["maintainer_feedback"]["status"] = "not_submitted_prevented"

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "dossiers.json"
            out_path = Path(tmp) / "report.json"
            write_json(ledger_path, {"schema_version": 1, "dossiers": [d1, d2]})

            self.assertEqual(cli.main(["dossier", "validate", str(ledger_path)]), 0)
            self.assertEqual(cli.main(["candidates", "list", str(ledger_path), "--verdict", "confirmed"]), 0)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "oss_audit",
                    "verify",
                    "--dossier",
                    str(ledger_path),
                    "--output",
                    str(out_path),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(out_path.is_file())
            summary = summarize_dossiers([d1, d2])
            self.assertEqual(summary["prevented_unhelpful_prs"], 1)
            self.assertEqual(summary["by_terminal_verdict"]["confirmed"], 1)


class CheckedInDossierFixtureTests(unittest.TestCase):
    def test_checked_in_seven_candidate_fixture_passes_falsification_audit(self):
        report = verify_candidate_falsification_suite()
        self.assertTrue(report["ok"], report)
        summary = report["summary"]
        self.assertEqual(summary["total_candidates"], 7)
        self.assertEqual(
            summary["by_terminal_verdict"],
            {
                "confirmed": 3,
                "excluded": 1,
                "insufficient_evidence": 0,
                "covered_upstream": 2,
                "design_tradeoff_issue_only": 1,
            },
        )
        self.assertEqual(summary["by_maintainer_feedback"]["merged"], 3)
        self.assertEqual(summary["by_maintainer_feedback"]["published_issue"], 1)
        self.assertEqual(summary["prevented_unhelpful_prs"], 3)
        self.assertEqual(summary["derived_rule_updates_count"], 7)

    def test_cli_candidate_falsification_suite_and_summary_exit_cleanly(self):
        verify_proc = subprocess.run(
            [sys.executable, "-m", "oss_audit", "verify", "--suite", "candidate-falsification"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verify_proc.returncode, 0, verify_proc.stderr)
        self.assertTrue(json.loads(verify_proc.stdout)["ok"])

        summary_proc = subprocess.run(
            [sys.executable, "-m", "oss_audit", "dossier", "summary"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(summary_proc.returncode, 0, summary_proc.stderr)
        summary_payload = json.loads(summary_proc.stdout)
        self.assertTrue(summary_payload["ok"])
        self.assertEqual(summary_payload["total_candidates"], 7)


if __name__ == "__main__":
    unittest.main()

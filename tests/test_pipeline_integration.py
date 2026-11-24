import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oss_audit.analyzers.contract_audit import audit_repo
from oss_audit.core.common import ROOT, digest
from oss_audit.discovery.score import rank
from oss_audit.policy import submit
from oss_audit.policy.claim_collision import search_claim_collision


class PipelineBoundaries(unittest.TestCase):
    def test_submit_without_evidence_never_reaches_network(self):
        with tempfile.TemporaryDirectory() as folder:
            for extra in ([], ["--yes"]):
                with patch.object(
                    submit, "gh_get", side_effect=AssertionError("network must not be reached")
                ):
                    rc = submit.main(
                        [
                            "o/r",
                            "--branch",
                            "fix/x",
                            "--title",
                            "fix: x",
                            "--body-file",
                            "nonexistent.md",
                            "--output",
                            str(Path(folder) / "plan.json"),
                            *extra,
                        ]
                    )
                    self.assertEqual(rc, 2)

    def test_same_method_names_are_not_a_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            p.joinpath("a.py").write_text(
                "from abc import ABC\nclass Contract(ABC):\n def save(self): ...\n def load(self): ...\n"
                "class UnrelatedA:\n def save(self): pass\n def load(self): pass\n"
                "class UnrelatedB:\n def save(self): pass\n def load(self): pass\n",
                encoding="utf-8",
            )
            self.assertEqual(audit_repo(p), [])

    def test_api_error_not_free(self):
        def fail(_):
            raise RuntimeError("429")

        self.assertEqual(
            search_claim_collision("o/r", ["numeric comparison", "metadata filter"], client=fail)["status"],
            "UNKNOWN",
        )

    def test_closed_search_match_requires_inspection(self):
        def client(_):
            return {
                "incomplete_results": False,
                "total_count": 1,
                "items": [
                    {
                        "html_url": "https://github.com/o/r/pull/1",
                        "title": "old related patch",
                        "state": "closed",
                    }
                ],
            }

        self.assertEqual(
            search_claim_collision("o/r", ["numeric comparison", "metadata filter"], client=client)["status"],
            "UNKNOWN",
        )

    def test_no_score_from_prose_or_stars(self):
        self.assertEqual(
            rank({"candidate_id": "x", "stars": 900000, "body": "very detailed"})["status"], "unscored"
        )

    def test_historical_ledger_is_not_rewritten(self):
        before = digest(ROOT / "ledger.json")
        result = subprocess.run(
            [sys.executable, "-m", "oss_audit", "gate", "--status"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["probe_holds_lease"])
        self.assertEqual(digest(ROOT / "ledger.json"), before)


if __name__ == "__main__":
    unittest.main()

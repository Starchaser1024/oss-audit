"""Exercise the subprocess evidence runner and its tamper/rejection boundaries."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oss_audit.core.common import read_json, write_json
from oss_audit.evidence import runner as evidence

PYTHON = Path(sys.executable)


class EvidenceIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("pytest") is None:
            raise unittest.SkipTest("pytest is required for subprocess evidence harness tests")
        cls.folder = Path(tempfile.mkdtemp(prefix="oss-audit-evidence-"))
        cls.workspace = cls.folder / "workspace"
        cls.workspace.mkdir()
        cls.repo = cls.folder / "repo"
        cls.repo.mkdir()

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(cls.repo), *args], stderr=subprocess.DEVNULL, text=True
            ).strip()

        git("init")
        git("config", "user.name", "Local fixture")
        git("config", "user.email", "fixture@example.invalid")
        (cls.repo / "sample.py").write_text(
            "def normalize(value):\n    return value.lower()\n", encoding="utf-8"
        )
        git("add", ".")
        git("commit", "-m", "fixture base")
        cls.base = git("rev-parse", "HEAD")
        cls.red = cls.folder / "red"
        git("worktree", "add", "--detach", str(cls.red), cls.base)
        (cls.repo / "sample.py").write_text(
            "def normalize(value):\n    return value.strip().lower()\n", encoding="utf-8"
        )
        git("add", ".")
        git("commit", "-m", "fixture fix")
        cls.head = git("rev-parse", "HEAD")
        tests = (
            "from sample import normalize\n\n\n"
            "def test_bug():\n"
            "    assert normalize(' A ') == 'a'\n\n\n"
            "def test_preserve():\n"
            "    assert normalize('B') == 'b'\n\n\n"
            "def test_empty():\n"
            "    assert normalize('') == ''\n"
        )
        for checkout in (cls.red, cls.repo):
            (checkout / "test_sample.py").write_text(tests, encoding="utf-8")
        has_ruff = importlib.util.find_spec("ruff") is not None
        cls.receipts = {}
        for phase in ("red", "green", "adjacent", "lint", "format", "whitespace"):
            checkout = cls.red if phase == "red" else cls.repo
            spec = cls.spec(checkout, phase)
            if phase in {"lint", "format"}:
                if has_ruff:
                    spec["check_argv"] = [
                        str(PYTHON),
                        "-m",
                        "ruff",
                        "check" if phase == "lint" else "format",
                        *(["--check"] if phase == "format" else []),
                        "sample.py",
                        "test_sample.py",
                        "--no-cache",
                    ]
                    if phase == "format":
                        spec["check_argv"] += ["--config", "format.quote-style='single'"]
                else:
                    spec["check_argv"] = [str(PYTHON), "-m", "py_compile", "sample.py", "test_sample.py"]
            elif phase == "whitespace":
                spec["check_argv"] = ["git", "diff", "--check"]
            path = cls.folder / f"{phase}.json"
            write_json(path, spec)
            cls.receipts[phase] = str(
                evidence.run(path, cls.folder / ("run-" + phase), workspace=cls.workspace)
            )
        card = {
            "candidate_id": "local-evidence-fixture",
            "repo": "fixture/local",
            "base_sha": cls.base,
            "user_scenario": "Deterministic local fixture; not a real contribution",
            "expected_behavior": "Trim and lowercase",
            "observed_behavior": "Whitespace persists",
            "root_cause": "Missing strip before lower",
            "preserved_behaviors": ["B becomes b"],
            "entry_path": [
                {"caller": "pytest test_bug", "callee": "sample.normalize", "evidence": "actual import proof"}
            ],
            "reachability": "proven",
            "contract_sources": [
                {"url_or_path": "local-fixture-spec", "revision": cls.base, "location": "fixture acceptance"}
            ],
            "design_uncertainties": [],
            "counterexample_cases": [
                {
                    "category": category,
                    "nodeid": f"test_sample.py::test_{name}",
                    "basis": "local fixture contract",
                }
                for category, name in [
                    ("original_bug", "bug"),
                    ("preservation", "preserve"),
                    ("adjacent_boundary", "empty"),
                ]
            ],
        }
        cls.card_path = cls.folder / "contract.json"
        write_json(cls.card_path, card)
        cls.bundle = {
            "contract": str(cls.card_path),
            "receipts": cls.receipts,
            "review": str(cls.folder / "review.json"),
        }
        write_json(
            cls.bundle["review"],
            {
                "reviewer": "Codex",
                "decision": "approved",
                "binding": evidence.bundle_binding(cls.bundle),
                "semantic_basis": "Fixture acceptance only",
                "falsification_cases": ["empty", "uppercase"],
                "causal_checks": ["direct imported production function"],
            },
        )
        cls.bundle_path = cls.folder / "bundle.json"
        write_json(cls.bundle_path, cls.bundle)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "repo") and cls.repo.exists():
            subprocess.run(
                ["git", "-C", str(cls.repo), "worktree", "remove", "--force", str(cls.red)],
                capture_output=True,
                check=False,
            )
        if hasattr(cls, "folder") and cls.folder.exists():
            shutil.rmtree(cls.folder, ignore_errors=True)

    @classmethod
    def spec(cls, checkout, phase):
        return {
            "candidate_id": "local-evidence-fixture",
            "phase": phase,
            "checkout": str(checkout),
            "revision": cls.base if phase == "red" else cls.head,
            "python": str(PYTHON),
            "sources": ["sample.py"],
            "tests": ["test_sample.py"],
            "imports": {"sample": "sample.py"},
            "pythonpath": ["."],
            "pytest_args": ["test_sample.py"],
            "locks": [],
            "timeout_seconds": 60,
        }

    def test_valid_pair_passes(self):
        result = evidence.validate_bundle(self.bundle_path, workspace=self.workspace)
        self.assertTrue(result["ok"], result)
        expected = {"test_sample.py::test_bug", "test_sample.py::test_preserve", "test_sample.py::test_empty"}
        for phase in ("red", "green"):
            self.assertEqual(set(read_json(self.receipts[phase])["results"]["nodes"]), expected)

    def test_forged_count_rejected(self):
        path = Path(self.receipts["green"])
        old = path.read_bytes()
        try:
            changed = read_json(path)
            changed["results"]["counts"]["passed"] = 10000
            write_json(path, changed)
            result = evidence.validate_receipt(path, workspace=self.workspace)
            self.assertFalse(result["ok"])
            self.assertIn("counts_or_nodes_changed", result["errors"])
        finally:
            path.write_bytes(old)

    def test_log_tamper_rejected(self):
        path = Path(self.receipts["green"]).parent / "stdout.log"
        old = path.read_bytes()
        try:
            path.write_text("10000 passed", encoding="utf-8")
            self.assertIn(
                "artifact_changed:stdout.log",
                evidence.validate_receipt(self.receipts["green"], workspace=self.workspace)["errors"],
            )
        finally:
            path.write_bytes(old)

    def test_source_change_invalidates_review_and_execution(self):
        path = self.repo / "sample.py"
        old = path.read_bytes()
        try:
            path.write_bytes(old + b"\n# edit after acceptance\n")
            self.assertFalse(evidence.validate_bundle(self.bundle_path, workspace=self.workspace)["ok"])
        finally:
            path.write_bytes(old)

    def test_contract_change_invalidates_review(self):
        old = self.card_path.read_bytes()
        try:
            card = read_json(self.card_path)
            card["root_cause"] = "Different semantic claim"
            write_json(self.card_path, card)
            self.assertIn(
                "review_stale",
                evidence.validate_bundle(self.bundle_path, workspace=self.workspace)["errors"],
            )
        finally:
            self.card_path.write_bytes(old)

    def test_wrong_import_source_rejected(self):
        spec = self.spec(self.repo, "green")
        spec["pythonpath"] = [str(self.red)]
        p = self.folder / "wrong-import.json"
        write_json(p, spec)
        with self.assertRaisesRegex(ValueError, "escapes checkout"):
            evidence.run(p, self.folder / "wrong-import", workspace=self.workspace)

    def test_zero_tests_are_not_green(self):
        spec = self.spec(self.repo, "green")
        spec["pytest_args"] += ["-k", "not_existing_case"]
        p = self.folder / "zero.json"
        write_json(p, spec)
        receipt = evidence.run(p, self.folder / "zero", workspace=self.workspace)
        self.assertIn(
            "not_green", evidence.validate_receipt(receipt, workspace=self.workspace)["errors"]
        )

    def test_all_skips_are_not_green(self):
        skip = self.repo / "test_all_skip.py"
        try:
            skip.write_text(
                "import pytest\n\ndef test_skip():\n    pytest.skip('not supported')\n", encoding="utf-8"
            )
            spec = self.spec(self.repo, "green")
            spec["tests"] = [skip.name]
            spec["pytest_args"] = [skip.name]
            p = self.folder / "skip.json"
            write_json(p, spec)
            receipt = evidence.run(p, self.folder / "run-skip", workspace=self.workspace)
            self.assertIn(
                "errors_skips_or_xfail",
                evidence.validate_receipt(receipt, workspace=self.workspace)["errors"],
            )
        finally:
            skip.unlink(missing_ok=True)

    def test_wrong_import_inside_checkout_is_rejected_at_runtime(self):
        shadow = self.repo / "shadow"
        shadow.mkdir()
        source = shadow / "sample.py"
        try:
            source.write_text("def normalize(value):\n    return 'fake'\n", encoding="utf-8")
            spec = self.spec(self.repo, "green")
            spec["pythonpath"] = ["shadow", "."]
            p = self.folder / "shadow.json"
            write_json(p, spec)
            receipt = evidence.run(p, self.folder / "run-shadow", workspace=self.workspace)
            self.assertFalse(evidence.validate_receipt(receipt, workspace=self.workspace)["ok"])
            self.assertIn(
                "Wrong import source", (receipt.parent / "stderr.log").read_text(encoding="utf-8")
            )
        finally:
            source.unlink(missing_ok=True)
            shadow.rmdir()

    def test_collection_error_is_not_red(self):
        spec = self.spec(self.red, "red")
        spec["pytest_args"] = ["no_such_test_file.py"]
        p = self.folder / "collection.json"
        write_json(p, spec)
        receipt = evidence.run(p, self.folder / "collection", workspace=self.workspace)
        self.assertFalse(evidence.validate_receipt(receipt, workspace=self.workspace)["ok"])

    def test_parser_needs_independent_categories(self):
        card = read_json(self.card_path)
        card["defect_class"] = "parser"
        self.assertIn("counterexample_categories_missing", evidence.validate_contract(card))

    def test_resolve_python_executable_preserves_venv_symlink(self):
        expected = str(PYTHON.parent.resolve() / PYTHON.name)
        self.assertEqual(evidence.resolve_python_executable(PYTHON), expected)
        self.assertEqual(evidence.resolve_python_executable("python"), expected)
        self.assertEqual(evidence.resolve_python_executable("python3"), expected)

    def test_bare_python_and_ruff_work_without_venv_on_path(self):
        git_bin = shutil.which("git")
        self.assertIsNotNone(git_bin)
        minimal_path = str(Path(git_bin).parent)
        with patch.dict(os.environ, {"PATH": minimal_path}, clear=False):
            green_spec = self.spec(self.repo, "green")
            green_spec["python"] = "python"
            green_spec_path = self.folder / "no-path-green.json"
            write_json(green_spec_path, green_spec)
            green_receipt = evidence.run(
                green_spec_path, self.folder / "run-no-path-green", workspace=self.workspace
            )
            self.assertTrue(evidence.validate_receipt(green_receipt, workspace=self.workspace)["ok"])

            if importlib.util.find_spec("ruff") is not None:
                lint_spec = self.spec(self.repo, "lint")
                lint_spec["python"] = "python"
                lint_spec["check_argv"] = ["ruff", "check", "sample.py", "test_sample.py", "--no-cache"]
                lint_spec_path = self.folder / "no-path-lint.json"
                write_json(lint_spec_path, lint_spec)
                lint_receipt = evidence.run(
                    lint_spec_path, self.folder / "run-no-path-lint", workspace=self.workspace
                )
                validated = evidence.validate_receipt(lint_receipt, workspace=self.workspace)
                self.assertTrue(validated["ok"], validated)
                self.assertEqual(
                    validated["receipt"]["argv"][:3],
                    [evidence.resolve_python_executable(), "-m", "ruff"],
                )


if __name__ == "__main__":
    unittest.main()

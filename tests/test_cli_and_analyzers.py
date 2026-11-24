"""Unit tests for the unified ``oss_audit`` CLI, dynamic workspace paths, AST analyzers, and replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oss_audit import __version__, cli
from oss_audit.analyzers.guard_asymmetry import (
    scan_async_guard_asymmetry,
    scan_hardcoded_from_dict,
    scan_sync_async_divergence,
)
from oss_audit.core.paths import REPO_ROOT, WorkspacePaths, resolve_workspace
from oss_audit.discovery.reach import find_callers
from oss_audit.verification.verify import historical


class CliAndWorkspaceTests(unittest.TestCase):
    def test_cli_top_level_help_and_version(self):
        help_proc = subprocess.run(
            [sys.executable, "-m", "oss_audit", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        for subcmd in ("discover", "tick", "policy", "claimcheck", "evidence", "verify", "submit", "metrics", "scan"):
            self.assertIn(subcmd, help_proc.stdout)

        ver_proc = subprocess.run(
            [sys.executable, "-m", "oss_audit", "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ver_proc.returncode, 0)
        self.assertIn(__version__, ver_proc.stdout)

    def test_resolve_workspace_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp) / "env-home"
            arg_dir = Path(tmp) / "arg-home"
            env_dir.mkdir()
            arg_dir.mkdir()
            with patch.dict(os.environ, {"OSS_AUDIT_HOME": str(env_dir)}, clear=False):
                self.assertEqual(resolve_workspace(), env_dir.resolve())
                self.assertEqual(resolve_workspace(arg_dir), arg_dir.resolve())
                paths = WorkspacePaths.from_workspace()
                # Falls back cleanly to REPO_ROOT for static policy/config when workspace lacks overrides
                self.assertEqual(paths.policy_file, REPO_ROOT / "policies" / "repos.json")

    def test_historical_counterexample_suite_rejects_naive_patches_and_accepts_catalog_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "historical.json"
            summary = historical(output=out)
            self.assertTrue(summary["ok"], summary)
            self.assertTrue(out.is_file())
            decisions = {row["version"]: row["decision"] for row in summary["versions"]}
            self.assertEqual(decisions["original"], "rejected")
            self.assertEqual(decisions["article_patch"], "rejected")
            self.assertEqual(decisions["wordlist_patch"], "rejected")
            self.assertEqual(decisions["catalog_fix"], "accepted_for_this_selector_contract")

    def test_cli_verify_historical_suite_without_writing_repo_root(self):
        proc = subprocess.run(
            [sys.executable, "-m", "oss_audit", "verify", "--suite", "historical-counterexamples"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse((REPO_ROOT / "evidence").exists())


class StaticAnalyzerTests(unittest.TestCase):
    def test_async_guard_asymmetry_and_divergence_detect_unprotected_async_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "retriever.py"
            src.write_text(
                "class Retriever:\n"
                "    def run(self, items):\n"
                "        if not items:\n"
                "            raise ValueError('empty')\n"
                "        try:\n"
                "            return items[0]\n"
                "        except IndexError:\n"
                "            return None\n\n"
                "    async def run_async(self, items):\n"
                "        return items[0]\n",
                encoding="utf-8",
            )
            asym = scan_async_guard_asymmetry(tmp)
            self.assertEqual(len(asym), 1)
            self.assertEqual(asym[0]["sync_name"], "run")
            self.assertEqual(asym[0]["async_name"], "run_async")
            self.assertGreaterEqual(asym[0]["score"], 4)

            div = scan_sync_async_divergence(tmp)
            self.assertEqual(len(div), 1)
            self.assertEqual(div[0]["sync_name"], "run")

    def test_hardcoded_from_dict_detects_broken_subclass_deserialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "models.py"
            src.write_text(
                "class BaseComponent:\n"
                "    @classmethod\n"
                "    def from_dict(cls, data):\n"
                "        return BaseComponent()\n\n"
                "class GoodComponent:\n"
                "    @classmethod\n"
                "    def from_dict(cls, data):\n"
                "        return cls()\n",
                encoding="utf-8",
            )
            findings = scan_hardcoded_from_dict(tmp)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["class"], "BaseComponent")
            self.assertEqual(findings[0]["hardcoded_return"], "BaseComponent")

    def test_reachability_finds_static_call_sites_and_cli_scan_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            pkg.mkdir()
            (pkg / "service.py").write_text(
                "def resolve_model_control(text):\n"
                "    return text\n\n"
                "def handle_request(body):\n"
                "    return resolve_model_control(body)\n",
                encoding="utf-8",
            )
            reach = find_callers(tmp, "resolve_model_control")
            self.assertEqual(reach["reachability"], "partial")
            self.assertEqual(len(reach["call_sites"]), 1)
            self.assertEqual(reach["call_sites"][0]["caller"], "handle_request")

            rc = cli.main(["scan", "--mode", "contracts", str(tmp)])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

"""Schema and hygiene tests for historical ``ledger.json`` and ``repos.json`` inventory."""

from __future__ import annotations

import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path

from oss_audit.analyzers.issue_scanner import load_inventory
from oss_audit.core.paths import REPO_ROOT, WorkspacePaths
from oss_audit.discovery.discover import repo_names

EXPECTED_LEDGER_SECTIONS = {"seen", "claimed", "submitted", "prepared"}
CJK_AND_FULLWIDTH_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
LOCAL_MACHINE_ARTIFACT_RE = re.compile(
    r"(?:/home/|/Users/|[A-Za-z]:\\\\|~/|\.zh\.|_zh\b|NanaZip)",
)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iter_strings(obj: object, prefix: str = ""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(key, str):
                yield key_path, key
            yield from _iter_strings(value, key_path)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            yield from _iter_strings(item, f"{prefix}[{index}]")
    elif isinstance(obj, str):
        yield prefix, obj


class LedgerAndConfigHygieneTests(unittest.TestCase):
    def test_ledger_top_level_schema_and_entry_fields(self):
        ledger_path = REPO_ROOT / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertIsInstance(ledger, dict)
        self.assertEqual(set(ledger.keys()), EXPECTED_LEDGER_SECTIONS)
        self.assertIn("agno-agi/agno#csvreader-page-size-zero", ledger["prepared"])

        for section_name in EXPECTED_LEDGER_SECTIONS:
            section = ledger[section_name]
            self.assertIsInstance(section, dict, f"{section_name} must be a JSON object")
            for entry_key, entry in section.items():
                self.assertIsInstance(entry, dict, f"{section_name}.{entry_key} must be an object")
                state = entry.get("state")
                date_str = entry.get("date")
                self.assertIsInstance(state, str, f"{section_name}.{entry_key} missing string state")
                self.assertTrue(state.strip(), f"{section_name}.{entry_key} has empty state")
                self.assertIsInstance(date_str, str, f"{section_name}.{entry_key} missing string date")
                self.assertRegex(
                    date_str,
                    ISO_DATE_RE,
                    f"{section_name}.{entry_key} date must be YYYY-MM-DD",
                )
                datetime.date.fromisoformat(date_str)

    def test_ledger_contains_zero_cjk_or_local_machine_artifacts(self):
        ledger_path = REPO_ROOT / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        for field_path, text in _iter_strings(ledger):
            self.assertIsNone(
                CJK_AND_FULLWIDTH_RE.search(text),
                f"CJK or fullwidth character found at {field_path}: {text!r}",
            )
            self.assertIsNone(
                LOCAL_MACHINE_ARTIFACT_RE.search(text),
                f"Local-machine or bilingual scratch artifact found at {field_path}: {text!r}",
            )

    def test_repos_json_inventory_and_legacy_yaml_workspace_fallback(self):
        repos_json_path = REPO_ROOT / "repos.json"
        self.assertTrue(repos_json_path.is_file())
        self.assertFalse((REPO_ROOT / "repos.yaml").exists())

        parsed = json.loads(repos_json_path.read_text(encoding="utf-8"))
        self.assertIn("defaults", parsed)
        self.assertIn("repos", parsed)
        self.assertGreater(len(parsed["repos"]), 0)
        names = repo_names()
        self.assertIn("bytedance/deer-flow", names)
        self.assertEqual(load_inventory(repos_json_path)["repos"][0]["name"], "bytedance/deer-flow")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            legacy_yaml = ws / "repos.yaml"
            legacy_yaml.write_text(
                json.dumps({"defaults": {"max_age_days": 7}, "repos": [{"name": "acme/legacy-repo"}]}),
                encoding="utf-8",
            )
            paths = WorkspacePaths.from_workspace(ws)
            self.assertEqual(paths.repos_inventory_file, legacy_yaml.resolve())
            self.assertEqual(repo_names(ws), ["acme/legacy-repo"])
            self.assertEqual(load_inventory(paths.repos_inventory_file)["defaults"]["max_age_days"], 7)

            preferred_json = ws / "repos.json"
            preferred_json.write_text(
                json.dumps({"defaults": {"max_age_days": 14}, "repos": [{"name": "acme/preferred-repo"}]}),
                encoding="utf-8",
            )
            self.assertEqual(paths.repos_inventory_file, preferred_json.resolve())
            self.assertEqual(repo_names(ws), ["acme/preferred-repo"])


if __name__ == "__main__":
    unittest.main()

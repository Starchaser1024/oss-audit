"""Unit tests for Protocol/ABC signature-drift and unawaited-coroutine AST analyzers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from oss_audit.analyzers import (
    audit_repo,
    scan_async_guard_asymmetry,
    scan_hardcoded_from_dict,
    scan_protocol_signature_drift,
    scan_unawaited_async_calls,
)
from oss_audit.core.paths import REPO_ROOT


class ProtocolSignatureDriftTests(unittest.TestCase):
    def test_detects_dropped_params_defaults_kwonly_and_async_flips(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "stores.py"
            src.write_text(
                "from abc import ABC, abstractmethod\n"
                "from typing import Protocol\n\n"
                "class DocumentReader(Protocol):\n"
                "    def read(self, path: str, page_size: int = 10, *, encoding: str = 'utf-8') -> list[str]: ...\n"
                "    async def aclose(self) -> None: ...\n\n"
                "class BaseStore(ABC):\n"
                "    @abstractmethod\n"
                "    def query(self, text: str, top_k: int = 5) -> list[str]:\n"
                "        raise NotImplementedError\n\n"
                "class DroppedOptionalAndKwonlyReader(DocumentReader):\n"
                "    def read(self, path: str) -> list[str]:\n"
                "        return [path]\n"
                "    def aclose(self) -> None:\n"
                "        return None\n\n"
                "class NarrowedStore(BaseStore):\n"
                "    def query(self, text: str, *, top_k: int) -> list[str]:\n"
                "        return [text][:top_k]\n\n"
                "class FlippedSyncToAsyncStore(BaseStore):\n"
                "    async def query(self, text: str, top_k: int = 5) -> list[str]:\n"
                "        return [text][:top_k]\n",
                encoding="utf-8",
            )
            findings = scan_protocol_signature_drift(tmp)
            by_cls = {}
            for row in findings:
                by_cls.setdefault(row["class"], []).append((row["method"], row["kind"], row["parameter"]))

            self.assertIn(("read", "dropped_parameter", "page_size"), by_cls["DroppedOptionalAndKwonlyReader"])
            self.assertIn(
                ("read", "keyword_only_incompatibility", "encoding"),
                by_cls["DroppedOptionalAndKwonlyReader"],
            )
            self.assertIn(("aclose", "async_mismatch", None), by_cls["DroppedOptionalAndKwonlyReader"])

            self.assertIn(("query", "keyword_only_incompatibility", "top_k"), by_cls["NarrowedStore"])
            self.assertIn(("query", "dropped_default", "top_k"), by_cls["NarrowedStore"])

            self.assertIn(("query", "async_mismatch", None), by_cls["FlippedSyncToAsyncStore"])

            groups = audit_repo(Path(tmp))
            contracts = {g["contract"]: g for g in groups if "signature_drift_count" in g}
            self.assertIn("DocumentReader", contracts)
            self.assertGreater(contracts["DocumentReader"]["signature_drift_count"], 0)

    def test_detects_added_required_params_and_transitive_inheritance(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "hierarchy.py"
            src.write_text(
                "from abc import ABC\n\n"
                "class PlanStore(ABC):\n"
                "    def add_item(self, item_id: str, payload: dict | None = None) -> None: ...\n\n"
                "class SqlPlanStore(PlanStore):\n"
                "    pass\n\n"
                "class BrokenSqlitePlanStore(SqlPlanStore):\n"
                "    def add_item(self, item_id: str, payload: dict, *, tenant_id: str) -> None:\n"
                "        pass\n",
                encoding="utf-8",
            )
            findings = scan_protocol_signature_drift(tmp)
            kinds = {(f["class"], f["kind"], f["parameter"]) for f in findings}
            self.assertIn(("BrokenSqlitePlanStore", "dropped_default", "payload"), kinds)
            self.assertIn(("BrokenSqlitePlanStore", "added_required_kwonly_parameter", "tenant_id"), kinds)

    def test_clean_counterexamples_overload_passthrough_and_coroutine_return_produce_zero_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "clean_contracts.py"
            src.write_text(
                "from typing import Any, Awaitable, Coroutine, Protocol, overload\n\n"
                "class CacheProtocol(Protocol):\n"
                "    def get(self, key: str, default: Any = None, *, namespace: str = 'default') -> Any: ...\n"
                "    async def refresh(self, key: str) -> bool: ...\n"
                "    def compute(self, key: str) -> Awaitable[int]: ...\n\n"
                "class OverloadedMemoryCache(CacheProtocol):\n"
                "    @overload\n"
                "    def get(self, key: str) -> Any: ...\n"
                "    @overload\n"
                "    def get(self, key: str, default: int, *, namespace: str = 'default') -> int: ...\n"
                "    def get(self, key: str, default: Any = None, *, namespace: str = 'default', trace: bool = False) -> Any:\n"
                "        return default\n"
                "    async def refresh(self, key: str, *, force: bool = False) -> bool:\n"
                "        return True\n"
                "    async def compute(self, key: str) -> int:\n"
                "        return len(key)\n\n"
                "class PassthroughProxyCache(CacheProtocol):\n"
                "    def get(self, *args: Any, **kwargs: Any) -> Any:\n"
                "        return None\n"
                "    def refresh(self, key: str) -> Coroutine[Any, Any, bool]:\n"
                "        return self._refresh_async(key)\n"
                "    async def _refresh_async(self, key: str) -> bool:\n"
                "        return True\n"
                "    async def compute(self, *args: Any, **kwargs: Any) -> int:\n"
                "        return 0\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_protocol_signature_drift(tmp), [])


class UnawaitedAsyncAndWrapperAsymmetryTests(unittest.TestCase):
    def test_detects_unawaited_sibling_and_collaborator_coroutines(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "pipeline.py"
            src.write_text(
                "async def flush_metrics_async() -> None:\n"
                "    return None\n\n"
                "class Retriever:\n"
                "    async def _warm_cache(self) -> None:\n"
                "        return None\n\n"
                "    async def _fetch_docs_async(self, query: str) -> list[str]:\n"
                "        return [query]\n\n"
                "    async def run_async(self, query: str) -> list[str]:\n"
                "        self._warm_cache()\n"
                "        flush_metrics_async()\n"
                "        self.store.filter_documents_async(query)\n"
                "        return self._fetch_docs_async(query)\n",
                encoding="utf-8",
            )
            findings = scan_unawaited_async_calls(tmp)
            by_callee = {f["callee"]: f["kind"] for f in findings}
            self.assertEqual(by_callee["self._warm_cache"], "unawaited_coroutine_statement")
            self.assertEqual(by_callee["flush_metrics_async"], "unawaited_coroutine_statement")
            self.assertEqual(by_callee["self.store.filter_documents_async"], "unawaited_coroutine_statement")
            self.assertEqual(by_callee["self._fetch_docs_async"], "unawaited_coroutine_return")

    def test_detects_sync_fallback_wrapper_missing_return_and_exception_swallowing(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "writers.py"
            src.write_text(
                "import asyncio\n\n"
                "class CleanWriter:\n"
                "    def write(self, docs: list[str]) -> int:\n"
                "        if not docs:\n"
                "            raise ValueError('empty')\n"
                "        return len(docs)\n\n"
                "    async def write_async(self, *args, **kwargs) -> int:\n"
                "        return await asyncio.to_thread(self.write, *args, **kwargs)\n\n"
                "class DroppedReturnWriter:\n"
                "    def write(self, docs: list[str]) -> int:\n"
                "        return len(docs)\n\n"
                "    async def write_async(self, docs: list[str]) -> int:\n"
                "        await asyncio.to_thread(self.write, docs)\n\n"
                "class SwallowingWrapperWriter:\n"
                "    def write(self, docs: list[str]) -> int:\n"
                "        if not docs:\n"
                "            raise ValueError('empty')\n"
                "        return len(docs)\n\n"
                "    async def write_async(self, docs: list[str]) -> int:\n"
                "        try:\n"
                "            return await asyncio.to_thread(self.write, docs)\n"
                "        except ValueError:\n"
                "            return 0\n",
                encoding="utf-8",
            )
            asym = scan_async_guard_asymmetry(tmp)
            classes = {row["class"]: row for row in asym}
            self.assertNotIn("CleanWriter", classes)
            self.assertIn("DroppedReturnWriter", classes)
            self.assertTrue(
                any("missing return propagation" in note for note in classes["DroppedReturnWriter"]["notes"])
            )
            self.assertIn("SwallowingWrapperWriter", classes)
            self.assertTrue(
                any("exception symmetry" in note for note in classes["SwallowingWrapperWriter"]["notes"])
            )

    def test_clean_async_patterns_produce_zero_false_positives(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "clean_async.py"
            src.write_text(
                "import asyncio\n"
                "from typing import Any, Coroutine, overload\n\n"
                "class Service:\n"
                "    @overload\n"
                "    def run(self, x: int) -> int: ...\n"
                "    @overload\n"
                "    def run(self, x: str) -> str: ...\n"
                "    def run(self, x: Any) -> Any:\n"
                "        return x\n\n"
                "    @overload\n"
                "    async def run_async(self, x: int) -> int: ...\n"
                "    @overload\n"
                "    async def run_async(self, x: str) -> str: ...\n"
                "    async def run_async(self, *args: Any, **kwargs: Any) -> Any:\n"
                "        return await asyncio.to_thread(self.run, *args, **kwargs)\n\n"
                "    def close(self) -> None:\n"
                "        pass\n\n"
                "    async def close_async(self) -> None:\n"
                "        await asyncio.to_thread(self.close)\n\n"
                "    async def _step_async(self, item: str) -> str:\n"
                "        return item\n\n"
                "    async def stream_async(self, items: list[str]):\n"
                "        for item in items:\n"
                "            yield item\n\n"
                "    def build_coro_sync(self, item: str) -> Coroutine[Any, Any, str]:\n"
                "        return self._step_async(item)\n\n"
                "    async def build_coro_factory(self, item: str) -> Coroutine[Any, Any, str]:\n"
                "        return self._step_async(item)\n\n"
                "    async def orchestrate(self, items: list[str]) -> list[str]:\n"
                "        bg = asyncio.create_task(self._step_async('warmup'))\n"
                "        await bg\n"
                "        deferred = self._step_async('first')\n"
                "        first = await deferred\n"
                "        batch = [self._step_async(i) for i in items]\n"
                "        rest = await asyncio.gather(*batch)\n"
                "        collected = [first, *rest]\n"
                "        async for chunk in self.stream_async(items):\n"
                "            collected.append(chunk)\n"
                "        return collected\n\n"
                "    @classmethod\n"
                "    @overload\n"
                "    def from_dict(cls, data: dict[str, Any]) -> 'Service': ...\n"
                "    @classmethod\n"
                "    def from_dict(cls, data: dict[str, Any]) -> 'Service':\n"
                "        return cls()\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_unawaited_async_calls(tmp), [])
            self.assertEqual(scan_async_guard_asymmetry(tmp), [])
            self.assertEqual(scan_hardcoded_from_dict(tmp), [])

    def test_cli_scan_modes_signature_drift_and_unawaited_async(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "sample.py"
            src.write_text(
                "from typing import Protocol\n\n"
                "class Runner(Protocol):\n"
                "    async def execute(self, payload: dict, retries: int = 3) -> dict: ...\n\n"
                "class BadRunner(Runner):\n"
                "    async def _notify_async(self) -> None:\n"
                "        return None\n"
                "    async def execute(self, payload: dict) -> dict:\n"
                "        self._notify_async()\n"
                "        return payload\n",
                encoding="utf-8",
            )
            drift_proc = subprocess.run(
                [sys.executable, "-m", "oss_audit", "scan", "--mode", "signature-drift", str(tmp)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(drift_proc.returncode, 0, drift_proc.stderr)
            drift_rows = json.loads(drift_proc.stdout)
            self.assertEqual(len(drift_rows), 1)
            self.assertEqual(drift_rows[0]["kind"], "dropped_parameter")
            self.assertEqual(drift_rows[0]["parameter"], "retries")

            unawaited_proc = subprocess.run(
                [sys.executable, "-m", "oss_audit", "scan", "--mode", "unawaited-async", str(tmp)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(unawaited_proc.returncode, 0, unawaited_proc.stderr)
            unawaited_rows = json.loads(unawaited_proc.stdout)
            self.assertEqual(len(unawaited_rows), 1)
            self.assertEqual(unawaited_rows[0]["callee"], "self._notify_async")


if __name__ == "__main__":
    unittest.main()

"""Child process: prove module import provenance and record per-node pytest outcomes."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    sys.path[:0] = spec["pythonpath"]
    proof: dict[str, Any] = {"executable": sys.executable, "version": sys.version, "imports": {}}
    for name, expected in spec["imports"].items():
        mod = importlib.import_module(name)
        mod_file = getattr(mod, "__file__", None)
        if mod_file is None:
            raise RuntimeError(f"Imported module {name} has no __file__")
        actual = Path(mod_file).resolve()
        if actual != Path(expected).resolve():
            raise RuntimeError(f"Wrong import source for {name}: {actual}; expected {expected}")
        proof["imports"][name] = str(actual)
    (out / "imports.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    import pytest

    rows: list[dict[str, Any]] = []

    class Recorder:
        def pytest_runtest_logreport(self, report: Any) -> None:
            rows.append(
                {
                    "nodeid": report.nodeid,
                    "when": report.when,
                    "outcome": report.outcome,
                    "wasxfail": bool(getattr(report, "wasxfail", False)),
                }
            )

        def pytest_collectreport(self, report: Any) -> None:
            if report.failed:
                rows.append(
                    {"nodeid": report.nodeid, "when": "collect", "outcome": "failed", "wasxfail": False}
                )

    try:
        code = pytest.main(
            [
                *spec["pytest_args"],
                # Identical tests in separate worktrees must produce identical node IDs.
                f"--rootdir={spec['cwd']}",
                f"--confcutdir={spec['checkout']}",
                "-q",
                "--tb=short",
                "-o",
                "junit_family=xunit2",
                "-o",
                f"cache_dir={out / 'cache'}",
                f"--junitxml={out / 'junit.xml'}",
            ],
            plugins=[Recorder()],
        )
    finally:
        (out / "outcomes.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(int(code))


if __name__ == "__main__":
    main()

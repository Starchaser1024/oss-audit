"""Extract static call-site leads; dynamic dispatch and reachability remain unproven until tested."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

from oss_audit.analyzers.contract_audit import iter_py


def find_callers(repo: str | Path, symbol: str) -> dict[str, Any]:
    """Scan Python modules under ``repo`` for static call sites matching ``symbol``."""
    repo_root = Path(repo)
    rows: list[dict[str, Any]] = []
    for path in iter_py(repo_root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        name = ast.unparse(call.func)
                        if name.split(".")[-1] == symbol:
                            rows.append(
                                {
                                    "caller": node.name,
                                    "callee_expression": name,
                                    "file": path.relative_to(repo_root).as_posix(),
                                    "line": call.lineno,
                                    "decorators": [ast.unparse(d) for d in node.decorator_list],
                                    "binding": "unknown",
                                    "branch_conditions": "requires_review",
                                }
                            )
    return {
        "reachability": "partial" if rows else "unknown",
        "call_sites": rows,
        "limitation": "Static spelling matches do not prove callable identity, guards, or user-visible behavior",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("symbol")
    args = parser.parse_args(argv)
    print(json.dumps(find_callers(args.repo, args.symbol), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

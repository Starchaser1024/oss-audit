"""Structural AST analyzers for N-1 guard asymmetries and subclass factory defects."""

from __future__ import annotations

import ast
import difflib
from pathlib import Path
from typing import Any

SKIP_DIRS = {".venv", ".git", "node_modules", "build", "dist", "__pycache__", "cookbook", "tests"}


def _guard_profile(node: ast.AST) -> dict[str, Any]:
    raises: list[str] = []
    handlers: list[str] = []
    returns = 0
    guards: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise):
            exc = sub.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            raises.append(ast.unparse(exc) if exc is not None else "raise")
        elif isinstance(sub, ast.ExceptHandler):
            handlers.append(ast.unparse(sub.type) if sub.type else "bare")
        elif isinstance(sub, ast.Return):
            returns += 1
        elif isinstance(sub, ast.If):
            body = sub.body
            if len(body) == 1 and isinstance(body[0], (ast.Raise, ast.Return)):
                guards.append(ast.unparse(sub.test)[:80])
    return {"raises": raises, "handlers": handlers, "returns": returns, "guards": guards}


def _collect_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def scan_async_guard_asymmetry(root: str | Path) -> list[dict[str, Any]]:
    """Rank same-file sync/async function pairs by exception-handler and guard asymmetry."""
    base_root = Path(root).resolve()
    rows: list[dict[str, Any]] = []
    for path in base_root.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        funcs = _collect_functions(path)
        for name, node in funcs.items():
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            base: str | None = None
            if name.endswith("_async") and name[: -len("_async")] in funcs:
                base = name[: -len("_async")]
            elif name.startswith("a") and len(name) > 2 and name[1:] in funcs:
                base = name[1:]
            if base is None:
                continue
            sync = funcs[base]
            if isinstance(sync, ast.AsyncFunctionDef):
                continue
            sp, ap = _guard_profile(sync), _guard_profile(node)
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            delegating = len(body) <= 3 and any(
                isinstance(sub, ast.Attribute) and sub.attr == base for sub in ast.walk(node)
            )
            if delegating:
                continue
            score = 0
            notes: list[str] = []
            if set(map(str, sp["handlers"])) != set(map(str, ap["handlers"])):
                score += 3
                notes.append(f"handlers sync={sp['handlers']} async={ap['handlers']}")
            only_sync = [r for r in sp["raises"] if r not in ap["raises"]]
            only_async = [r for r in ap["raises"] if r not in sp["raises"]]
            if only_sync or only_async:
                score += 2
                if only_sync:
                    notes.append(f"raise only in sync: {sorted(set(only_sync))}")
                if only_async:
                    notes.append(f"raise only in async: {sorted(set(only_async))}")
            if set(sp["guards"]) != set(ap["guards"]):
                score += 2
                gs, ga = set(sp["guards"]), set(ap["guards"])
                if gs - ga:
                    notes.append(f"guard only in sync: {sorted(gs - ga)[:3]}")
                if ga - gs:
                    notes.append(f"guard only in async: {sorted(ga - gs)[:3]}")
            if score:
                rows.append(
                    {
                        "score": score,
                        "file": path.relative_to(base_root).as_posix(),
                        "sync_name": base,
                        "async_name": name,
                        "sync_line": sync.lineno,
                        "async_line": node.lineno,
                        "notes": notes,
                    }
                )
    rows.sort(key=lambda r: (-r["score"], r["file"], r["sync_line"]))
    return rows


def _normalized_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    filtered = [
        stmt
        for stmt in body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    return ast.unparse(ast.Module(body=filtered, type_ignores=[]))


def scan_sync_async_divergence(root: str | Path) -> list[dict[str, Any]]:
    """Find sync/async pairs whose normalized AST bodies diverge beyond ``await``."""
    base_root = Path(root).resolve()
    findings: list[dict[str, Any]] = []
    for path in base_root.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        funcs = _collect_functions(path)
        for name, node in funcs.items():
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            base: str | None = None
            if name.endswith("_async") and name[: -len("_async")] in funcs:
                base = name[: -len("_async")]
            elif name.startswith("a") and len(name) > 2 and name[1:] in funcs:
                base = name[1:]
            if base is None or isinstance(funcs[base], ast.AsyncFunctionDef):
                continue
            sync_node = funcs[base]
            sync_body = _normalized_body(sync_node)
            async_body = _normalized_body(node)
            if sync_body == async_body:
                continue
            diff = list(
                difflib.unified_diff(
                    sync_body.splitlines(),
                    async_body.splitlines(),
                    fromfile=f"{base}:{sync_node.lineno}",
                    tofile=f"{name}:{node.lineno}",
                    lineterm="",
                    n=1,
                )
            )
            interesting = [d for d in diff if d.startswith(("+", "-")) and not d.startswith(("+++", "---"))]
            if interesting and len(interesting) % 2 == 0 and all(
                a[1:].replace("await ", "") == b[1:].replace("await ", "")
                for a, b in zip(interesting[::2], interesting[1::2])
            ):
                continue
            findings.append(
                {
                    "file": path.relative_to(base_root).as_posix(),
                    "sync_name": base,
                    "async_name": name,
                    "sync_line": sync_node.lineno,
                    "async_line": node.lineno,
                    "diff": diff[:40],
                }
            )
    return findings


def scan_hardcoded_from_dict(root: str | Path) -> list[dict[str, Any]]:
    """Find ``from_dict(cls, ...)`` classmethods returning a hardcoded class instead of ``cls``."""
    base_root = Path(root).resolve()
    findings: list[dict[str, Any]] = []
    for path in base_root.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        module_classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef) or fn.name != "from_dict":
                    continue
                if not fn.args.args or fn.args.args[0].arg != "cls":
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Return) or node.value is None:
                        continue
                    calls = [n for n in ast.walk(node.value) if isinstance(n, ast.Call)]
                    for call in calls:
                        func = call.func
                        name = (
                            func.id
                            if isinstance(func, ast.Name)
                            else func.attr
                            if isinstance(func, ast.Attribute)
                            else None
                        )
                        if (
                            name
                            and name in module_classes
                            and name not in {"cls", "super", "default_from_dict", "from_dict"}
                        ):
                            findings.append(
                                {
                                    "file": path.relative_to(base_root).as_posix(),
                                    "class": cls.name,
                                    "method_line": fn.lineno,
                                    "return_line": node.lineno,
                                    "hardcoded_return": name,
                                }
                            )
    return findings

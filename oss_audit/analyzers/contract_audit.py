"""Static AST contract, Protocol/ABC signature-drift, and multi-implementation lead generator.

Identifies ``Protocol`` / ``ABC`` interfaces implemented across concrete classes,
surfacing missing contract methods, parameter/default-arity incompatibilities,
keyword-only compatibility breaks, and sync/async coroutine mismatches alongside
multi-implementation navigation leads. Outputs are bounded investigation leads
requiring explicit contract proof and execution receipts.
"""

from __future__ import annotations

import argparse
import ast
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from oss_audit.core.paths import WorkspacePaths

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "tests",
    "test",
    "docs",
    "migrations",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}
PROTO_BASES = {"Protocol", "ABC", "ABCMeta"}
AWAITABLE_RETURN_NAMES = {"Awaitable", "Coroutine", "Future", "Task"}


def iter_py(repo: Path) -> Iterator[Path]:
    for path in repo.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("test_"):
            continue
        yield path


def _walk_shallow(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """Yield AST nodes inside ``fn.body`` without descending into nested functions or lambdas."""
    stack: list[ast.AST] = list(reversed(fn.body))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        children = list(ast.iter_child_nodes(current))
        stack.extend(reversed(children))


def _decorator_parts(expr: ast.expr) -> list[str]:
    if isinstance(expr, ast.Call):
        return _decorator_parts(expr.func)
    if isinstance(expr, ast.Name):
        return [expr.id]
    if isinstance(expr, ast.Attribute):
        return _decorator_parts(expr.value) + [expr.attr]
    return []


def _is_overload(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(parts and parts[-1] == "overload" for parts in (_decorator_parts(d) for d in fn.decorator_list))


def _is_property_mutator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        parts = _decorator_parts(dec)
        if len(parts) >= 2 and parts[-1] in {"setter", "deleter"}:
            return True
    return False


def _is_property(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        parts = _decorator_parts(dec)
        if parts and parts[-1] in {"property", "cached_property", "abstractproperty"}:
            return True
        if len(parts) >= 2 and parts[-1] == "getter":
            return True
    return False


def _is_staticmethod(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        parts and parts[-1] in {"staticmethod", "abstractstaticmethod"}
        for parts in (_decorator_parts(d) for d in fn.decorator_list)
    )


def _is_classmethod(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        parts and parts[-1] in {"classmethod", "abstractclassmethod"}
        for parts in (_decorator_parts(d) for d in fn.decorator_list)
    )


def _annotation_mentions(annotation: ast.expr | None, names: set[str]) -> bool:
    if annotation is None:
        return False
    for sub in ast.walk(annotation):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in names:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(token in sub.value for token in names):
                return True
    return False


def _call_looks_awaitable(expr: ast.expr) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Name):
        return func.id.endswith("_async") or (func.id.startswith("a") and len(func.id) > 2 and func.id[1].islower())
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "asyncio":
            return True
        return func.attr.endswith("_async")
    return False


def _sync_returns_coroutine_intentionally(fn: ast.FunctionDef) -> bool:
    if _annotation_mentions(fn.returns, AWAITABLE_RETURN_NAMES):
        return True
    non_none_returns: list[ast.expr] = []
    for node in _walk_shallow(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                continue
            non_none_returns.append(node.value)
    return bool(non_none_returns) and all(_call_looks_awaitable(val) for val in non_none_returns)


def extract_method_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    """Extract normalized parameter and coroutine metadata for a method definition."""
    is_static = _is_staticmethod(fn)
    positional_nodes = list(fn.args.posonlyargs) + list(fn.args.args)
    num_posonly = len(fn.args.posonlyargs)
    num_defaults = len(fn.args.defaults)
    first_default_idx = len(positional_nodes) - num_defaults

    posonly: list[dict[str, Any]] = []
    pos_or_kw: list[dict[str, Any]] = []
    for idx, arg_node in enumerate(positional_nodes):
        if idx == 0 and not is_static:
            # Strip receiver (`self` or `cls`)
            continue
        param = {"name": arg_node.arg, "has_default": idx >= first_default_idx}
        if idx < num_posonly:
            posonly.append(param)
        else:
            pos_or_kw.append(param)

    kwonly: list[dict[str, Any]] = []
    for kw_arg, kw_default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        kwonly.append({"name": kw_arg.arg, "has_default": kw_default is not None})

    is_async = isinstance(fn, ast.AsyncFunctionDef)
    is_async_gen = is_async and any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _walk_shallow(fn))
    awaitable_return = _annotation_mentions(
        fn.returns,
        AWAITABLE_RETURN_NAMES | {"AsyncIterator", "AsyncGenerator", "AsyncIterable"},
    )
    intentional_coro_return = (not is_async) and isinstance(fn, ast.FunctionDef) and (
        _sync_returns_coroutine_intentionally(fn)
    )
    return {
        "posonly": posonly,
        "pos_or_kw": pos_or_kw,
        "kwonly": kwonly,
        "has_vararg": fn.args.vararg is not None,
        "has_kwarg": fn.args.kwarg is not None,
        "is_async": is_async,
        "is_async_gen": is_async_gen,
        "is_staticmethod": is_static,
        "is_classmethod": _is_classmethod(fn),
        "is_property": _is_property(fn),
        "awaitable_return_annotation": awaitable_return,
        "intentional_coroutine_return": intentional_coro_return,
    }


def _is_stub_body(body: list[ast.stmt]) -> bool:
    if (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is Ellipsis
    ):
        return True
    if all(
        isinstance(s, ast.Pass)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        for s in body
    ):
        return True
    # Also treat `raise NotImplementedError(...)` (with optional docstring) as an abstract contract stub
    stmts = [
        s
        for s in body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
    ]
    if len(stmts) == 1 and isinstance(stmts[0], ast.Raise) and stmts[0].exc is not None:
        exc = stmts[0].exc.func if isinstance(stmts[0].exc, ast.Call) else stmts[0].exc
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
    return False


def class_info(node: ast.ClassDef, rel_file: str = "") -> dict[str, Any]:
    bases = [ast.unparse(b) for b in node.bases]
    grouped: dict[str, dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]]] = {}
    order: list[str] = []

    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_property_mutator(item):
            continue
        if item.name not in grouped:
            grouped[item.name] = {"overloads": [], "concrete": []}
            order.append(item.name)
        if _is_overload(item):
            grouped[item.name]["overloads"].append(item)
        else:
            grouped[item.name]["concrete"].append(item)

    methods: list[dict[str, Any]] = []
    for name in order:
        bucket = grouped[name]
        concrete_defs = bucket["concrete"]
        overload_defs = bucket["overloads"]
        if concrete_defs:
            chosen = concrete_defs[-1]
            overload_only = False
        else:
            chosen = overload_defs[-1]
            overload_only = True
        sig = extract_method_signature(chosen)
        methods.append(
            {
                "name": chosen.name,
                "async": sig["is_async"],
                "line": chosen.lineno,
                "end": getattr(chosen, "end_lineno", chosen.lineno),
                "stub": _is_stub_body(chosen.body),
                "has_overloads": bool(overload_defs),
                "overload_only": overload_only,
                "signature": sig,
            }
        )
    return {
        "name": node.name,
        "bases": bases,
        "line": node.lineno,
        "end": getattr(node, "end_lineno", node.lineno),
        "docstring": (ast.get_docstring(node) or "").strip(),
        "methods": methods,
    }


def compare_method_signatures(proto_method: dict[str, Any], impl_method: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare a concrete implementation method against its ``Protocol`` / ``ABC`` contract method."""
    if impl_method.get("overload_only"):
        return []
    psig = proto_method.get("signature")
    isig = impl_method.get("signature")
    if not psig or not isig:
        return []
    if psig["is_property"] or isig["is_property"]:
        if psig["is_property"] != isig["is_property"]:
            return [
                {
                    "kind": "property_callable_mismatch",
                    "method": proto_method["name"],
                    "parameter": None,
                    "contract_line": proto_method["line"],
                    "impl_line": impl_method["line"],
                    "detail": (
                        f"method '{proto_method['name']}' flips @property vs callable method "
                        "relative to base contract"
                    ),
                }
            ]
        return []

    name = proto_method["name"]
    c_line = proto_method["line"]
    i_line = impl_method["line"]
    drifts: list[dict[str, Any]] = []

    # 1. Sync `def` vs `async def` contract mismatch
    if psig["is_async"] and not isig["is_async"]:
        if not isig["intentional_coroutine_return"] and not (psig["is_async_gen"] and isig["awaitable_return_annotation"]):
            drifts.append(
                {
                    "kind": "async_mismatch",
                    "method": name,
                    "parameter": None,
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": f"base contract declares 'async def {name}' but implementation is synchronous 'def {name}'",
                }
            )
    elif not psig["is_async"] and isig["is_async"]:
        if not psig["awaitable_return_annotation"]:
            drifts.append(
                {
                    "kind": "async_mismatch",
                    "method": name,
                    "parameter": None,
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": f"base contract declares synchronous 'def {name}' but implementation is 'async def {name}'",
                }
            )

    if proto_method.get("overload_only"):
        return drifts

    # 2. Positional capacity & default-arity drift
    p_pos = psig["posonly"] + psig["pos_or_kw"]
    i_pos = isig["posonly"] + isig["pos_or_kw"]
    p_min = sum(not p["has_default"] for p in p_pos)
    p_max = len(p_pos)
    i_min = sum(not p["has_default"] for p in i_pos)
    i_max = len(i_pos)

    i_kwonly_map = {k["name"]: k for k in isig["kwonly"]}
    i_pos_or_kw_map = {p["name"]: p for p in isig["pos_or_kw"]}
    i_posonly_names = {p["name"] for p in isig["posonly"]}
    reported_params: set[str] = set()

    if psig["has_vararg"] and not isig["has_vararg"]:
        drifts.append(
            {
                "kind": "dropped_varargs",
                "method": name,
                "parameter": "*args",
                "contract_line": c_line,
                "impl_line": i_line,
                "detail": f"implementation of '{name}' drops variadic '*args' accepted by base contract",
            }
        )

    # Check positional-or-keyword parameters moved to keyword-only or positional-only
    for p in psig["pos_or_kw"]:
        pname = p["name"]
        if pname in i_kwonly_map and not isig["has_vararg"]:
            reported_params.add(pname)
            drifts.append(
                {
                    "kind": "keyword_only_incompatibility",
                    "method": name,
                    "parameter": pname,
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": (
                        f"parameter '{pname}' in '{name}' is positional-or-keyword in base contract "
                        "but keyword-only in implementation"
                    ),
                }
            )
            if p["has_default"] and not i_kwonly_map[pname]["has_default"]:
                drifts.append(
                    {
                        "kind": "dropped_default",
                        "method": name,
                        "parameter": pname,
                        "contract_line": c_line,
                        "impl_line": i_line,
                        "detail": f"optional parameter '{pname}' in '{name}' lost its default value in implementation",
                    }
                )
        elif pname in i_posonly_names:
            reported_params.add(pname)
            drifts.append(
                {
                    "kind": "positional_only_incompatibility",
                    "method": name,
                    "parameter": pname,
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": (
                        f"parameter '{pname}' in '{name}' accepts keyword calls in base contract "
                        "but is positional-only in implementation"
                    ),
                }
            )

    # Check dropped positional parameters when implementation lacks *args
    if not isig["has_vararg"] and i_max < p_max:
        i_all_pos_names = {p["name"] for p in i_pos}
        for idx, p in enumerate(p_pos):
            pname = p["name"]
            if pname in reported_params:
                continue
            if idx >= i_max or pname not in i_all_pos_names:
                reported_params.add(pname)
                opt_or_req = "optional" if p["has_default"] else "required"
                drifts.append(
                    {
                        "kind": "dropped_parameter",
                        "method": name,
                        "parameter": pname,
                        "contract_line": c_line,
                        "impl_line": i_line,
                        "detail": f"implementation of '{name}' drops {opt_or_req} parameter '{pname}'",
                    }
                )

    # Check narrowed defaults or added required positional parameters
    if i_min > p_min:
        for idx, ip in enumerate(i_pos):
            if ip["has_default"]:
                continue
            ip_name = ip["name"]
            if idx < len(p_pos) and p_pos[idx]["has_default"]:
                reported_params.add(ip_name)
                drifts.append(
                    {
                        "kind": "dropped_default",
                        "method": name,
                        "parameter": ip_name,
                        "contract_line": c_line,
                        "impl_line": i_line,
                        "detail": (
                            f"optional contract parameter '{p_pos[idx]['name']}' in '{name}' "
                            "is required without a default in implementation"
                        ),
                    }
                )
            elif idx >= len(p_pos) and not psig["has_vararg"]:
                reported_params.add(ip_name)
                drifts.append(
                    {
                        "kind": "added_required_parameter",
                        "method": name,
                        "parameter": ip_name,
                        "contract_line": c_line,
                        "impl_line": i_line,
                        "detail": (
                            f"implementation of '{name}' adds required positional parameter '{ip_name}' "
                            "absent from base contract"
                        ),
                    }
                )

    # Check renamed/missing keyword-callable parameters when positional arity matched but **kwargs is absent
    if not isig["has_kwarg"]:
        for p in psig["pos_or_kw"]:
            pname = p["name"]
            if pname in reported_params or pname in i_pos_or_kw_map:
                continue
            reported_params.add(pname)
            drifts.append(
                {
                    "kind": "dropped_parameter",
                    "method": name,
                    "parameter": pname,
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": (
                        f"contract parameter '{pname}' in '{name}' is missing or renamed in implementation "
                        "without '**kwargs'"
                    ),
                }
            )

    # 3. Keyword-only contract parameters
    for k in psig["kwonly"]:
        kname = k["name"]
        if kname in i_kwonly_map:
            if k["has_default"] and not i_kwonly_map[kname]["has_default"]:
                drifts.append(
                    {
                        "kind": "dropped_default",
                        "method": name,
                        "parameter": kname,
                        "contract_line": c_line,
                        "impl_line": i_line,
                        "detail": (
                            f"optional keyword-only parameter '{kname}' in '{name}' "
                            "lost its default value in implementation"
                        ),
                    }
                )
        elif kname in i_pos_or_kw_map:
            if k["has_default"] and not i_pos_or_kw_map[kname]["has_default"]:
                drifts.append(
                    {
                        "kind": "dropped_default",
                        "method": name,
                        "parameter": kname,
                        "contract_line": c_line,
                        "impl_line": i_line,
                        "detail": (
                            f"optional keyword-only parameter '{kname}' in '{name}' "
                            "lost its default value in implementation"
                        ),
                    }
                )
        elif not isig["has_kwarg"]:
            drifts.append(
                {
                    "kind": "keyword_only_incompatibility",
                    "method": name,
                    "parameter": kname,
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": (
                        f"keyword-only contract parameter '{kname}' in '{name}' "
                        "is dropped in implementation without '**kwargs'"
                    ),
                }
            )

    # Check added required keyword-only parameters in implementation
    proto_all_param_names = {p["name"] for p in p_pos} | {k["name"] for k in psig["kwonly"]}
    for ik in isig["kwonly"]:
        if not ik["has_default"] and ik["name"] not in proto_all_param_names:
            drifts.append(
                {
                    "kind": "added_required_kwonly_parameter",
                    "method": name,
                    "parameter": ik["name"],
                    "contract_line": c_line,
                    "impl_line": i_line,
                    "detail": (
                        f"implementation of '{name}' adds required keyword-only parameter '{ik['name']}' "
                        "absent from base contract"
                    ),
                }
            )

    return drifts


def _short_base_names(bases: list[str]) -> set[str]:
    return {b.split("[")[0].split(".")[-1].strip() for b in bases if b.strip()}


def _build_ancestor_closure(all_classes: list[dict[str, Any]]) -> dict[str, set[str]]:
    direct: dict[str, set[str]] = {}
    for cls in all_classes:
        direct.setdefault(cls["name"], set()).update(_short_base_names(cls["bases"]))
    closure: dict[str, set[str]] = {}
    for name in direct:
        seen: set[str] = set()
        stack = list(direct[name])
        while stack:
            cur = stack.pop()
            if cur in seen or cur == name:
                continue
            seen.add(cur)
            stack.extend(direct.get(cur, ()))
        closure[name] = seen
    return closure


def scan_protocol_signature_drift(repo: str | Path) -> list[dict[str, Any]]:
    """Detect concrete ``Protocol`` / ``ABC`` implementations whose signatures drift from base contracts."""
    repo_root = Path(repo).resolve()
    protocols: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    all_classes: list[dict[str, Any]] = []

    for path in iter_py(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (SyntaxError, OSError, UnicodeError):
            continue
        rel = path.relative_to(repo_root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            info = class_info(node, rel)
            info["file"] = rel
            all_classes.append(info)
            bases_short = _short_base_names(info["bases"])
            if bases_short & PROTO_BASES:
                protocols.append(info)
            elif info["methods"]:
                classes.append(info)

    ancestors = _build_ancestor_closure(all_classes)
    findings: list[dict[str, Any]] = []
    for proto in protocols:
        proto_methods = {
            m["name"]: m for m in proto["methods"] if not m["name"].startswith("_")
        }
        if not proto_methods:
            continue
        for cls in classes:
            if proto["name"] not in ancestors.get(cls["name"], _short_base_names(cls["bases"])):
                continue
            if _short_base_names(cls["bases"]) & PROTO_BASES:
                continue
            cls_methods = {m["name"]: m for m in cls["methods"]}
            for mname, pmethod in sorted(proto_methods.items()):
                imethod = cls_methods.get(mname)
                if imethod is None:
                    continue
                for drift in compare_method_signatures(pmethod, imethod):
                    findings.append(
                        {
                            "contract": proto["name"],
                            "contract_file": proto["file"],
                            "contract_line": drift["contract_line"],
                            "class": cls["name"],
                            "file": cls["file"],
                            "line": drift["impl_line"],
                            "method": mname,
                            "kind": drift["kind"],
                            "parameter": drift["parameter"],
                            "detail": drift["detail"],
                        }
                    )
    findings.sort(key=lambda r: (r["file"], r["line"], r["method"], r["kind"]))
    return findings


def audit_repo(repo: Path, max_groups: int = 40) -> list[dict[str, Any]]:
    """Return investigation leads; never asserts a proven contract violation."""
    protocols: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    all_classes: list[dict[str, Any]] = []
    surfaces: list[dict[str, Any]] = []
    for path in iter_py(repo):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (SyntaxError, OSError, UnicodeError):
            continue
        rel = path.relative_to(repo).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                interesting = any(
                    term in node.name.lower()
                    for term in (
                        "from_dict",
                        "to_dict",
                        "serializ",
                        "stream",
                        "cancel",
                        "close",
                        "parse",
                        "decode",
                    )
                )
                if interesting and len(node.body) > 1:
                    surfaces.append(
                        {
                            "contract": f"unverified function: {node.name}",
                            "contract_file": rel,
                            "contract_line": node.lineno,
                            "contract_doc": (ast.get_docstring(node) or "")[:400],
                            "methods": [node.name],
                            "implementations": [],
                            "lead_kind": "function_surface",
                            "reachability": "unknown",
                            "decision": "investigate",
                            "contract_status": "not_established",
                            "reason": "Conversion/stream/lifecycle spelling is a navigation hint only; no defect is asserted",
                        }
                    )
            if not isinstance(node, ast.ClassDef):
                continue
            info = class_info(node, rel)
            info["file"] = rel
            all_classes.append(info)
            bases_short = _short_base_names(info["bases"])
            if bases_short & PROTO_BASES:
                protocols.append(info)
            elif info["methods"]:
                classes.append(info)

    ancestors = _build_ancestor_closure(all_classes)
    groups: list[dict[str, Any]] = []
    for proto in protocols:
        proto_method_map = {m["name"]: m for m in proto["methods"] if not m["name"].startswith("_")}
        wanted = set(proto_method_map)
        if not wanted:
            continue
        impls = []
        for cls in classes:
            # Method-name overlap alone is not evidence of a shared contract.
            named_bases = ancestors.get(cls["name"], _short_base_names(cls["bases"]))
            if proto["name"] not in named_bases:
                continue
            cls_method_map = {m["name"]: m for m in cls["methods"]}
            have = set(cls_method_map)
            overlap = wanted & have
            sig_drifts: list[dict[str, Any]] = []
            for mname in sorted(overlap):
                sig_drifts.extend(compare_method_signatures(proto_method_map[mname], cls_method_map[mname]))
            min_overlap = max(2, len(wanted) // 2) if len(wanted) >= 2 else 1
            if len(overlap) >= min_overlap or sig_drifts:
                impls.append(
                    {
                        "class": cls["name"],
                        "file": cls["file"],
                        "line": cls["line"],
                        "end": cls["end"],
                        "covered": sorted(overlap),
                        "missing": sorted(wanted - have),
                        "signature_drift": sig_drifts,
                        "relation": "explicit_base_syntax; binding requires review",
                        "base_expressions": cls["bases"],
                    }
                )
        total_drift = sum(len(impl["signature_drift"]) for impl in impls)
        multi_impl_contract = len(proto["methods"]) >= 2 and len(impls) >= 2
        if multi_impl_contract or total_drift > 0:
            groups.append(
                {
                    "contract": proto["name"],
                    "contract_file": proto["file"],
                    "contract_line": proto["line"],
                    "contract_doc": proto["docstring"][:400],
                    "methods": sorted(wanted),
                    "implementations": impls,
                    "signature_drift_count": total_drift,
                    "reachability": "unknown",
                    "decision": "investigate",
                    "contract_status": "unverified_lead",
                }
            )

    groups.sort(
        key=lambda g: (g.get("signature_drift_count", 0) > 0, len(g["implementations"]), len(g["methods"])),
        reverse=True,
    )
    return (groups + surfaces)[:max_groups]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--repo-path", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--max", type=int, default=40)
    args = parser.parse_args(argv)
    paths = WorkspacePaths.from_workspace(args.workspace)

    if args.repo_path:
        repo = args.repo_path.resolve()
    else:
        repos_root = paths.repos_dir
        matches = (
            [p for p in repos_root.iterdir() if p.is_dir() and args.name and args.name.lower() in p.name.lower()]
            if repos_root.is_dir()
            else []
        )
        if len(matches) != 1:
            parser.error("Use an unambiguous repository name or --repo-path")
        repo = matches[0]
    if not repo.is_dir():
        parser.error("Repository path does not exist")

    groups = audit_repo(repo, args.max)
    output = args.output_dir or paths.audit_dir
    stamp = datetime.now().strftime("%Y-%m-%d")
    output.mkdir(parents=True, exist_ok=True)
    slug = repo.name
    (output / f"{slug}-{stamp}.json").write_text(
        json.dumps(groups, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        f"# Contract Audit Leads: {slug} ({stamp})",
        "",
        f"Found {len(groups)} explicit inheritance or conversion/stream/lifecycle leads. "
        "Contract binding and production reachability remain unverified.",
        "",
        "Boundary checklist: empty input / None / duplicate keys / case folding / path separators / "
        "cancellation / expiry / off-by-one / validate-before-write / exception propagation.",
        "",
    ]
    for idx, group in enumerate(groups, 1):
        lines.append(f"## {idx}. `{group['contract']}` @ `{group['contract_file']}:{group['contract_line']}`")
        if group["contract_doc"]:
            lines.append(f"> {group['contract_doc'].splitlines()[0][:200]}")
        lines.append("")
        lines.append(f"Methods: {', '.join(f'`{m}`' for m in group['methods'])}")
        lines.append("")
        for impl in group["implementations"]:
            miss = f"  (missing: {', '.join(impl['missing'])})" if impl["missing"] else ""
            drifts = impl.get("signature_drift", [])
            drift_note = (
                f"  (signature drift: {', '.join(d['method'] + ':' + d['kind'] for d in drifts)})"
                if drifts
                else ""
            )
            lines.append(f"- `{impl['class']}` - `{impl['file']}:{impl['line']}-{impl['end']}`{miss}{drift_note}")
        lines.append("")
    (output / f"{slug}-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"{len(groups)} investigation leads -> {output / (slug + '-' + stamp + '.md')}")
    for group in groups[:12]:
        print(f"  {len(group['implementations'])} impls  {group['contract']:36s} {group['contract_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

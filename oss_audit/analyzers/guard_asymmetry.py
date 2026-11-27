"""Structural AST analyzers for N-1 guard asymmetries, unawaited coroutines, and subclass factories."""

from __future__ import annotations

import ast
import difflib
from pathlib import Path
from typing import Any, Iterator

SKIP_DIRS = {".venv", ".git", "node_modules", "build", "dist", "__pycache__", "cookbook", "tests"}
AWAITABLE_RETURN_NAMES = {"Awaitable", "Coroutine", "Future", "Task"}
COROUTINE_SCHEDULERS = {
    "create_task",
    "ensure_future",
    "gather",
    "wait",
    "wait_for",
    "shield",
    "as_completed",
    "run_coroutine_threadsafe",
}
SYNC_THREAD_WRAPPERS = {"to_thread", "run_in_executor", "run_in_threadpool"}


def _walk_function_scope(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """Walk statements inside ``fn.body`` without descending into nested functions, lambdas, or classes."""
    stack: list[ast.AST] = list(reversed(fn.body))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        children = list(ast.iter_child_nodes(current))
        stack.extend(reversed(children))


def _decorator_terminal_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _is_overload(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return "overload" in _decorator_terminal_names(fn)


def _annotation_is_awaitable(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    for sub in ast.walk(annotation):
        if isinstance(sub, ast.Name) and sub.id in AWAITABLE_RETURN_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in AWAITABLE_RETURN_NAMES:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(tok in sub.value for tok in AWAITABLE_RETURN_NAMES):
                return True
    return False


def _is_async_generator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return isinstance(fn, ast.AsyncFunctionDef) and any(
        isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _walk_function_scope(fn)
    )


def _is_non_none_value(expr: ast.expr | None) -> bool:
    if expr is None:
        return False
    return not (isinstance(expr, ast.Constant) and expr.value is None)


def _guard_profile(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    raises: list[str] = []
    handlers: list[str] = []
    returns = 0
    value_returns = 0
    guards: list[str] = []
    for sub in _walk_function_scope(node):
        if isinstance(sub, ast.Raise):
            exc = sub.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            raises.append(ast.unparse(exc) if exc is not None else "raise")
        elif isinstance(sub, ast.ExceptHandler):
            handlers.append(ast.unparse(sub.type) if sub.type else "bare")
        elif isinstance(sub, ast.Return):
            returns += 1
            if _is_non_none_value(sub.value):
                value_returns += 1
        elif isinstance(sub, ast.If):
            body = sub.body
            if len(body) == 1 and isinstance(body[0], (ast.Raise, ast.Return)):
                guards.append(ast.unparse(sub.test)[:80])
    return {
        "raises": raises,
        "handlers": handlers,
        "returns": returns,
        "value_returns": value_returns,
        "guards": guards,
    }


def _extract_scope_functions(
    body: list[ast.stmt],
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for item in body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_overload(item):
            continue
        out[item.name] = item
    return out


def _iter_function_scopes(
    tree: ast.Module,
) -> list[tuple[str | None, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]]:
    scopes: list[tuple[str | None, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]] = []
    module_funcs = _extract_scope_functions(tree.body)
    if module_funcs:
        scopes.append((None, module_funcs))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            cls_funcs = _extract_scope_functions(node.body)
            if cls_funcs:
                scopes.append((node.name, cls_funcs))
    return scopes


def _sync_base_name(
    name: str,
    funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> str | None:
    if name.endswith("_async") and name[: -len("_async")] in funcs:
        return name[: -len("_async")]
    if name.startswith("a") and len(name) > 2 and name[1:] in funcs:
        return name[1:]
    return None


def _expr_references_sync_target(expr: ast.AST, base: str, in_class: bool) -> bool:
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Attribute) and sub.attr == base:
            return True
        if not in_class and isinstance(sub, ast.Name) and sub.id == base:
            return True
    return False


def _delegates_to_sync(
    async_node: ast.AsyncFunctionDef,
    base: str,
    in_class: bool,
) -> tuple[bool, bool]:
    """Return ``(delegates_to_base, discarded_in_expr_stmt)``."""
    delegates = False
    discarded = False
    for sub in _walk_function_scope(async_node):
        if isinstance(sub, ast.Call):
            func = sub.func
            call_name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if call_name in SYNC_THREAD_WRAPPERS and any(
                _expr_references_sync_target(arg, base, in_class) for arg in sub.args
            ):
                delegates = True
            elif isinstance(func, ast.Attribute) and func.attr == base:
                delegates = True
            elif not in_class and isinstance(func, ast.Name) and func.id == base:
                delegates = True
        elif isinstance(sub, ast.Expr) and _expr_references_sync_target(sub.value, base, in_class):
            delegates = True
            discarded = True
    return delegates, discarded


def _callee_descriptor(call: ast.Call) -> tuple[str | None, str | None, str]:
    """Return ``(receiver_kind, method_or_func_name, unparsed_callee)``."""
    func = call.func
    callee_text = ast.unparse(func)
    if isinstance(func, ast.Name):
        return ("bare", func.id, callee_text)
    if isinstance(func, ast.Attribute):
        val = func.value
        if isinstance(val, ast.Name) and val.id in {"self", "cls"}:
            return ("self", func.attr, callee_text)
        if (
            isinstance(val, ast.Call)
            and isinstance(val.func, ast.Name)
            and val.func.id == "super"
        ):
            return ("super", func.attr, callee_text)
        if isinstance(val, ast.Name) and val.id == "asyncio":
            return ("asyncio", func.attr, callee_text)
        return ("attr", func.attr, callee_text)
    return (None, None, callee_text)


def _build_parent_map(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    stack: list[ast.AST] = list(fn.body)
    for stmt in fn.body:
        parents[id(stmt)] = fn
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(current):
            parents[id(child)] = current
            stack.append(child)
    return parents


def _collect_consumed_coro_vars(fn: ast.AsyncFunctionDef, returns_coroutine: bool) -> set[str]:
    """Identify local variable names later awaited or passed to a coroutine scheduler."""
    consumed: set[str] = set()
    for node in _walk_function_scope(fn):
        if isinstance(node, ast.Await):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Name):
                    consumed.add(sub.id)
        elif isinstance(node, ast.Call):
            _, call_name, _ = _callee_descriptor(node)
            if call_name in COROUTINE_SCHEDULERS:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name):
                            consumed.add(sub.id)
        elif returns_coroutine and isinstance(node, ast.Return) and node.value is not None:
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Name):
                    consumed.add(sub.id)
    return consumed


def _unawaited_status_for_call(
    call: ast.Call,
    fn: ast.AsyncFunctionDef,
    parents: dict[int, ast.AST],
    consumed_vars: set[str],
    returns_coroutine: bool,
) -> str | None:
    """Return finding kind if ``call`` is unawaited inside ``fn``, else ``None``."""
    cur: ast.AST = call
    while id(cur) in parents:
        parent = parents[id(cur)]
        if parent is fn:
            break
        if isinstance(parent, ast.Await):
            return None
        if isinstance(parent, ast.AsyncFor) and cur is parent.iter:
            return None
        if isinstance(parent, ast.comprehension) and parent.is_async and cur is parent.iter:
            return None
        if isinstance(parent, ast.withitem) and cur is parent.context_expr:
            grandparent = parents.get(id(parent))
            if isinstance(grandparent, ast.AsyncWith):
                return None
        if isinstance(parent, ast.Call):
            # Only treat `call` as scheduled if it is an argument (not `call().something()`)
            if cur is not parent.func:
                _, sched_name, _ = _callee_descriptor(parent)
                if sched_name in COROUTINE_SCHEDULERS:
                    return None
        if isinstance(parent, ast.Return):
            if returns_coroutine:
                return None
            return "unawaited_coroutine_return"
        if isinstance(parent, ast.Assign):
            target_names = [t.id for t in parent.targets if isinstance(t, ast.Name)]
            if target_names and all(name in consumed_vars for name in target_names):
                return None
        if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
            if parent.target.id in consumed_vars:
                return None
        if isinstance(parent, ast.Expr) and cur is parent.value:
            return "unawaited_coroutine_statement"
        cur = parent
    return "unawaited_coroutine_call"


def _build_repo_symbol_index(paths: list[Path]) -> tuple[
    dict[Path, ast.Module],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    """Parse repository files and index class coroutine methods, sync methods, and inheritance."""
    trees: dict[Path, ast.Module] = {}
    class_bases: dict[str, set[str]] = {}
    class_coros: dict[str, set[str]] = {}
    class_syncs: dict[str, set[str]] = {}

    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        trees[path] = tree
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {ast.unparse(b).split("[")[0].split(".")[-1].strip() for b in node.bases}
            class_bases.setdefault(node.name, set()).update(b for b in bases if b)
            coros = class_coros.setdefault(node.name, set())
            syncs = class_syncs.setdefault(node.name, set())
            for fn in _extract_scope_functions(node.body).values():
                if isinstance(fn, ast.AsyncFunctionDef):
                    if _is_async_generator(fn):
                        syncs.add(fn.name)
                    else:
                        coros.add(fn.name)
                elif _annotation_is_awaitable(fn.returns):
                    coros.add(fn.name)
                else:
                    syncs.add(fn.name)

    # Propagate inherited coroutine/sync method knowledge across base classes
    for cls_name in list(class_bases):
        seen: set[str] = set()
        stack = list(class_bases[cls_name])
        while stack:
            base = stack.pop()
            if base in seen or base == cls_name:
                continue
            seen.add(base)
            stack.extend(class_bases.get(base, ()))
            for m in class_coros.get(base, ()):
                if m not in class_syncs.get(cls_name, ()):
                    class_coros.setdefault(cls_name, set()).add(m)
            for m in class_syncs.get(base, ()):
                if m not in class_coros.get(cls_name, ()):
                    class_syncs.setdefault(cls_name, set()).add(m)

    return trees, class_bases, class_coros, class_syncs


def _is_known_coroutine_call(
    call: ast.Call,
    enclosing_class: str | None,
    module_coros: set[str],
    module_syncs: set[str],
    class_coros: dict[str, set[str]],
    class_syncs: dict[str, set[str]],
    unambiguous_repo_coros: set[str],
) -> bool:
    kind, target_name, _ = _callee_descriptor(call)
    if not kind or not target_name:
        return False
    if kind in {"self", "super"}:
        if enclosing_class:
            if target_name in class_coros.get(enclosing_class, ()):
                return True
            if target_name in class_syncs.get(enclosing_class, ()):
                return False
        return target_name.endswith("_async")
    if kind == "asyncio":
        return target_name in {"sleep"}
    if kind == "attr":
        if target_name.endswith("_async"):
            return True
        return target_name in unambiguous_repo_coros
    if kind == "bare":
        if target_name in module_coros:
            return True
        if target_name in module_syncs:
            return False
        return target_name.endswith("_async")
    return False


def _find_unawaited_in_async_fn(
    fn: ast.AsyncFunctionDef,
    rel_file: str,
    enclosing_class: str | None,
    module_coros: set[str],
    module_syncs: set[str],
    class_coros: dict[str, set[str]],
    class_syncs: dict[str, set[str]],
    unambiguous_repo_coros: set[str],
) -> list[dict[str, Any]]:
    returns_coro = _annotation_is_awaitable(fn.returns)
    parents = _build_parent_map(fn)
    consumed_vars = _collect_consumed_coro_vars(fn, returns_coro)
    findings: list[dict[str, Any]] = []
    for sub in _walk_function_scope(fn):
        if not isinstance(sub, ast.Call):
            continue
        if not _is_known_coroutine_call(
            sub,
            enclosing_class,
            module_coros,
            module_syncs,
            class_coros,
            class_syncs,
            unambiguous_repo_coros,
        ):
            continue
        status = _unawaited_status_for_call(sub, fn, parents, consumed_vars, returns_coro)
        if status is None:
            continue
        _, _, callee_text = _callee_descriptor(sub)
        findings.append(
            {
                "file": rel_file,
                "class": enclosing_class,
                "function": fn.name,
                "line": sub.lineno,
                "callee": callee_text,
                "kind": status,
                "detail": f"coroutine '{callee_text}' called inside 'async def {fn.name}' without await",
            }
        )
    return findings


def scan_unawaited_async_calls(root: str | Path) -> list[dict[str, Any]]:
    """Detect ``async def`` functions/methods that invoke coroutine targets without ``await``."""
    base_root = Path(root).resolve()
    py_files = [p for p in base_root.rglob("*.py") if not (SKIP_DIRS & set(p.parts))]
    trees, _, class_coros, class_syncs = _build_repo_symbol_index(py_files)

    all_coro_methods = set().union(*class_coros.values()) if class_coros else set()
    all_sync_methods = set().union(*class_syncs.values()) if class_syncs else set()
    unambiguous_repo_coros = all_coro_methods - all_sync_methods

    findings: list[dict[str, Any]] = []
    for path, tree in trees.items():
        rel = path.relative_to(base_root).as_posix()
        mod_funcs = _extract_scope_functions(tree.body)
        module_coros = {
            f.name
            for f in mod_funcs.values()
            if (isinstance(f, ast.AsyncFunctionDef) and not _is_async_generator(f))
            or (isinstance(f, ast.FunctionDef) and _annotation_is_awaitable(f.returns))
        }
        module_syncs = {f.name for f in mod_funcs.values() if f.name not in module_coros}

        def visit_block(stmts: list[ast.stmt], cls_name: str | None) -> None:
            for item in stmts:
                if isinstance(item, ast.ClassDef):
                    visit_block(item.body, item.name)
                elif isinstance(item, ast.AsyncFunctionDef):
                    if not _is_overload(item):
                        findings.extend(
                            _find_unawaited_in_async_fn(
                                item,
                                rel,
                                cls_name,
                                module_coros,
                                module_syncs,
                                class_coros,
                                class_syncs,
                                unambiguous_repo_coros,
                            )
                        )
                    visit_block(item.body, cls_name)
                elif isinstance(item, ast.FunctionDef):
                    visit_block(item.body, cls_name)

        visit_block(tree.body, None)

    findings.sort(key=lambda r: (r["file"], r["line"], r["function"]))
    return findings


def scan_async_guard_asymmetry(root: str | Path) -> list[dict[str, Any]]:
    """Rank same-scope sync/async pairs by guard, exception, return-propagation, and await asymmetry."""
    base_root = Path(root).resolve()
    py_files = [p for p in base_root.rglob("*.py") if not (SKIP_DIRS & set(p.parts))]
    trees, _, class_coros, class_syncs = _build_repo_symbol_index(py_files)
    all_coro_methods = set().union(*class_coros.values()) if class_coros else set()
    all_sync_methods = set().union(*class_syncs.values()) if class_syncs else set()
    unambiguous_repo_coros = all_coro_methods - all_sync_methods

    rows: list[dict[str, Any]] = []
    for path, tree in trees.items():
        rel = path.relative_to(base_root).as_posix()
        mod_funcs = _extract_scope_functions(tree.body)
        module_coros = {
            f.name
            for f in mod_funcs.values()
            if (isinstance(f, ast.AsyncFunctionDef) and not _is_async_generator(f))
            or (isinstance(f, ast.FunctionDef) and _annotation_is_awaitable(f.returns))
        }
        module_syncs = {f.name for f in mod_funcs.values() if f.name not in module_coros}

        for cls_name, funcs in _iter_function_scopes(tree):
            for name, node in funcs.items():
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                base = _sync_base_name(name, funcs)
                if base is None:
                    continue
                sync = funcs[base]
                if isinstance(sync, ast.AsyncFunctionDef):
                    continue
                sp, ap = _guard_profile(sync), _guard_profile(node)
                body = [
                    s
                    for s in node.body
                    if not (
                        isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant)
                        and isinstance(s.value.value, str)
                    )
                ]
                delegates, discarded_delegation = _delegates_to_sync(node, base, cls_name is not None)
                unawaited = _find_unawaited_in_async_fn(
                    node,
                    rel,
                    cls_name,
                    module_coros,
                    module_syncs,
                    class_coros,
                    class_syncs,
                    unambiguous_repo_coros,
                )

                # Clean thin delegating wrapper (e.g. `return await asyncio.to_thread(self.run, *args, **kwargs)`)
                if (
                    len(body) <= 3
                    and delegates
                    and not discarded_delegation
                    and not ap["handlers"]
                    and not ap["raises"]
                    and not ap["guards"]
                    and (sp["value_returns"] == 0 or ap["value_returns"] > 0)
                    and not unawaited
                ):
                    continue

                score = 0
                notes: list[str] = []

                if delegates:
                    # Wrapper around sync fallback: inspect wrapper-introduced return/exception asymmetry
                    if sp["value_returns"] > 0 and (discarded_delegation or ap["value_returns"] == 0):
                        score += 4
                        notes.append(
                            "missing return propagation: sync returns a value while async wrapper "
                            "discards delegated result"
                        )
                    if ap["handlers"] and set(map(str, ap["handlers"])) != set(map(str, sp["handlers"])):
                        score += 3
                        notes.append(
                            f"async wrapper alters exception symmetry: sync={sp['handlers']} async={ap['handlers']}"
                        )
                    only_async_w = [r for r in ap["raises"] if r not in sp["raises"]]
                    if only_async_w:
                        score += 2
                        notes.append(f"raise only in async wrapper: {sorted(set(only_async_w))}")
                else:
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
                    if not _is_async_generator(node) and bool(sp["value_returns"]) != bool(ap["value_returns"]):
                        score += 3
                        notes.append(
                            f"return propagation mismatch: sync value_returns={sp['value_returns']} "
                            f"async value_returns={ap['value_returns']}"
                        )

                if unawaited:
                    score += 4
                    callees = [u["callee"] for u in unawaited]
                    notes.append(f"unawaited coroutine in async path: {callees}")

                if score:
                    row: dict[str, Any] = {
                        "score": score,
                        "file": rel,
                        "sync_name": base,
                        "async_name": name,
                        "sync_line": sync.lineno,
                        "async_line": node.lineno,
                        "notes": notes,
                    }
                    if cls_name is not None:
                        row["class"] = cls_name
                    rows.append(row)
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
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for cls_name, funcs in _iter_function_scopes(tree):
            for name, node in funcs.items():
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                base = _sync_base_name(name, funcs)
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
                row: dict[str, Any] = {
                    "file": path.relative_to(base_root).as_posix(),
                    "sync_name": base,
                    "async_name": name,
                    "sync_line": sync_node.lineno,
                    "async_line": node.lineno,
                    "diff": diff[:40],
                }
                if cls_name is not None:
                    row["class"] = cls_name
                findings.append(row)
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
                if not isinstance(fn, ast.FunctionDef) or fn.name != "from_dict" or _is_overload(fn):
                    continue
                if not fn.args.args or fn.args.args[0].arg != "cls":
                    continue
                for node in _walk_function_scope(fn):
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

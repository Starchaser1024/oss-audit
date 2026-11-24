"""Static AST contract & multi-implementation lead generator.

Identifies ``Protocol`` / ``ABC`` interfaces implemented across multiple concrete
classes (where N-1 implementations enforce a boundary guard and one omits it) as
well as serialization, streaming, and lifecycle entry points. Outputs are bounded
navigation leads requiring explicit contract proof and execution receipts.
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


def iter_py(repo: Path) -> Iterator[Path]:
    for path in repo.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("test_"):
            continue
        yield path


def class_info(node: ast.ClassDef, rel_file: str = "") -> dict[str, Any]:
    bases = [ast.unparse(b) for b in node.bases]
    methods: list[dict[str, Any]] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = item.body
            is_stub = (
                len(body) == 1
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and body[0].value.value is Ellipsis
            )
            methods.append(
                {
                    "name": item.name,
                    "async": isinstance(item, ast.AsyncFunctionDef),
                    "line": item.lineno,
                    "end": getattr(item, "end_lineno", item.lineno),
                    "stub": is_stub
                    or all(
                        isinstance(s, ast.Pass)
                        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                        for s in body
                    ),
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


def audit_repo(repo: Path, max_groups: int = 40) -> list[dict[str, Any]]:
    """Return investigation leads; never asserts a proven contract violation."""
    protocols: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
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
            bases_short = {b.split(".")[-1] for b in info["bases"]}
            if bases_short & PROTO_BASES:
                protocols.append(info)
            elif info["methods"]:
                classes.append(info)

    groups: list[dict[str, Any]] = []
    for proto in protocols:
        if len(proto["methods"]) < 2:
            continue
        wanted = {m["name"] for m in proto["methods"] if not m["name"].startswith("_")}
        if not wanted:
            continue
        impls = []
        for cls in classes:
            # Method-name overlap alone is not evidence of a shared contract.
            named_bases = {b.split("[")[0].split(".")[-1] for b in cls["bases"]}
            if proto["name"] not in named_bases:
                continue
            have = {m["name"] for m in cls["methods"]}
            overlap = wanted & have
            if len(overlap) >= max(2, len(wanted) // 2):
                impls.append(
                    {
                        "class": cls["name"],
                        "file": cls["file"],
                        "line": cls["line"],
                        "end": cls["end"],
                        "covered": sorted(overlap),
                        "missing": sorted(wanted - have),
                        "relation": "explicit_base_syntax; binding requires review",
                        "base_expressions": cls["bases"],
                    }
                )
        if len(impls) >= 2:
            groups.append(
                {
                    "contract": proto["name"],
                    "contract_file": proto["file"],
                    "contract_line": proto["line"],
                    "contract_doc": proto["docstring"][:400],
                    "methods": sorted(wanted),
                    "implementations": impls,
                    "reachability": "unknown",
                    "decision": "investigate",
                    "contract_status": "unverified_lead",
                }
            )

    groups.sort(key=lambda g: (len(g["implementations"]), len(g["methods"])), reverse=True)
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
            lines.append(f"- `{impl['class']}` - `{impl['file']}:{impl['line']}-{impl['end']}`{miss}")
        lines.append("")
    (output / f"{slug}-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"{len(groups)} investigation leads -> {output / (slug + '-' + stamp + '.md')}")
    for group in groups[:12]:
        print(f"  {len(group['implementations'])} impls  {group['contract']:36s} {group['contract_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

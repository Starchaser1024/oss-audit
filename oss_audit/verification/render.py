"""Render local issue or PR review drafts strictly from verified execution receipts."""

from __future__ import annotations

import argparse
from pathlib import Path

from oss_audit.core.common import read_json
from oss_audit.evidence import runner as evidence_runner


def render(
    bundle_path: str | Path,
    kind: str,
    workspace: str | Path | None = None,
) -> str:
    """Render an issue or PR draft from SHA-bound receipts; never accepts manual test counts."""
    bundle = read_json(bundle_path)
    card = read_json(bundle["contract"])
    if kind == "pr":
        checked = evidence_runner.validate_bundle(bundle_path, workspace=workspace)
    else:
        checked = evidence_runner.validate_report(bundle_path, workspace=workspace)
    if not checked["ok"]:
        raise ValueError("Evidence rejected: " + "; ".join(checked["errors"]))
    lines = [
        "## Problem",
        "",
        card["user_scenario"],
        "",
        "## Expected and observed behavior",
        "",
        card["expected_behavior"],
        "",
        card["observed_behavior"],
        "",
        "## Cause and evidence",
        "",
        card["root_cause"],
        "",
    ]
    for name, path in bundle["receipts"].items():
        receipt = read_json(path)
        counts = receipt.get("results", {}).get("counts")
        lines.append(
            f"- {name}: "
            + (", ".join(f"{v} {k}" for k, v in counts.items()) if counts else f"exit {receipt['exit_code']}")
        )
    lines += [
        "",
        "## Scope and limitations",
        "",
        *card.get("limitations", ["Only the recorded commands were executed."]),
    ]
    if card.get("design_uncertainties"):
        lines += [
            "",
            "Design questions requiring maintainer input:",
            "",
            *[f"- {q}" for q in card["design_uncertainties"]],
        ]
    lines += ["", "## AI assistance", "", "Codex assisted with the audit and verification.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--kind", choices=["issue", "pr"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(argv)
    text = render(args.bundle, args.kind, workspace=args.workspace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("Choose a new output path; existing drafts are not overwritten")
    args.output.write_text(text, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

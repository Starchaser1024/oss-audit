"""Compatibility lease status probe: persistent leases belong exclusively to ``tick``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from oss_audit.core.config import load_pipeline_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    config = load_pipeline_config(args.config, args.workspace)
    if args.status:
        print(
            json.dumps(
                {
                    "coordinator": "oss_audit.scheduling.tick",
                    "read_workers": config["read_workers"],
                    "heavy_workers": config["heavy_workers"],
                    "daily_research": config["daily_research"],
                    "publication_quota": config["publication_quota"],
                    "probe_holds_lease": False,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            "SKIP legacy gate cannot grant a persistent lease. Run `python -m oss_audit tick`; "
            "it owns and renews atomic task leases for the actual execution lifetime."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Module entry point enabling ``python -m oss_audit <subcommand>``."""

from __future__ import annotations

import sys

from oss_audit.cli import main

if __name__ == "__main__":
    sys.exit(main())

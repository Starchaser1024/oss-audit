"""Host disk and memory admission probes before scheduling bounded tasks."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any


def available_memory() -> int:
    """Return available physical memory in bytes on Windows, Linux, or macOS."""
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                (name, ctypes.c_uint64)
                for name in (
                    "total",
                    "available",
                    "page_total",
                    "page_available",
                    "virtual_total",
                    "virtual_available",
                    "extended_available",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.available)
        raise OSError("GlobalMemoryStatusEx failed")

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024

    if sys.platform == "darwin":
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            avail_pages = os.sysconf("SC_PHYS_PAGES")
            if page_size > 0 and avail_pages > 0:
                return int(page_size * avail_pages)
        except (ValueError, OSError, AttributeError):
            pass

    raise OSError("available memory is unknown on this host")


def admission(config: dict[str, Any], root: str | Path) -> dict[str, Any]:
    """Check whether disk and physical memory meet configured watermarks."""
    limits = config.get("resources", {})
    observed: dict[str, Any] = {}
    reasons: list[str] = []
    target_root = Path(root)
    if not target_root.exists():
        target_root = target_root.resolve().parent
    probes = {
        "disk": lambda: shutil.disk_usage(target_root).free,
        "memory": available_memory,
    }
    for name, probe in probes.items():
        minimum = float(limits.get(f"min_available_{name}_gib", 0))
        if minimum <= 0:
            continue
        try:
            observed[f"available_{name}_gib"] = round(probe() / 1024**3, 3)
            if observed[f"available_{name}_gib"] < minimum:
                reasons.append(f"low_{name}")
        except OSError:
            observed[f"available_{name}_gib"] = None
            reasons.append(f"unknown_{name}")
    return {"ok": not reasons, "reasons": reasons, **observed}

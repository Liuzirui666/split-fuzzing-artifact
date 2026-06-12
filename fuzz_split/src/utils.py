"""Shared utilities for the split-fuzzing framework."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List


def get_total_ram_gb() -> float:
    """Get total system RAM in GB."""
    import psutil
    return psutil.virtual_memory().total / (1024 ** 3)


def get_available_ram_gb() -> float:
    """Get available system RAM in GB."""
    import psutil
    return psutil.virtual_memory().available / (1024 ** 3)


def check_ram_safety(min_available_gb: float = 20.0) -> bool:
    """Return True if enough RAM is available."""
    avail = get_available_ram_gb()
    if avail < min_available_gb:
        print(f"WARNING: Only {avail:.1f} GB RAM available (need {min_available_gb})")
        return False
    return True


def kill_all_fuzzbench_containers(prefix: str = "") -> int:
    """Stop and remove FuzzBench dispatcher containers for a specific experiment.

    CRITICAL: NEVER use without a prefix. Only kills containers matching
    'dispatcher-d-{prefix}*'. Never touches unrelated containers.
    """
    if not prefix:
        raise ValueError("SAFETY: must provide a prefix to avoid killing unrelated containers")
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    killed = 0
    for name in result.stdout.strip().split("\n"):
        if not name:
            continue
        if name.startswith(f"dispatcher-d-{prefix}"):
            subprocess.run(["docker", "rm", "-f", name],
                          capture_output=True, timeout=30)
            killed += 1
    return killed


def check_no_conflicting_containers(experiment_name: str) -> List[str]:
    """Check if any containers from this experiment are still running."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    conflicts = []
    for name in result.stdout.strip().split("\n"):
        if experiment_name in name:
            conflicts.append(name)
    return conflicts

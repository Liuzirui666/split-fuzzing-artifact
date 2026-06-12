"""Split plan definition: when to split and how many descendants.

Supports:
  - Fixed-time plans (offline mode, from sparsity CSV)
  - No-split baseline (single stage covering full duration)
  - Dynamic plans will be handled by online_sparsity.py at runtime
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Stage:
    """One stage of a split experiment.

    level 0 = initial run (before any split).
    level k = the k-th split has happened; trials at this level are descendants.
    """
    level: int              # 0, 1, 2, ...
    start_hour: float
    end_hour: float
    branching_factor: int   # how many descendants each parent spawns at the END of this stage

    @property
    def duration_seconds(self) -> int:
        return max(1, int(round((self.end_hour - self.start_hour) * 3600)))

    @property
    def duration_hours(self) -> float:
        return self.end_hour - self.start_hour


@dataclass(frozen=True)
class SplitPlan:
    """Ordered list of stages for one (benchmark, fuzzer) pair.

    checkpoints = [t0, t1, t2, ..., T]
    Stage k runs from checkpoints[k] to checkpoints[k+1].
    """
    checkpoints_hours: List[float]
    branching_factor: int = 2

    def stages(self) -> List[Stage]:
        cps = self.checkpoints_hours
        if len(cps) < 2:
            raise ValueError(f"Need >= 2 checkpoints, got {cps}")
        out: List[Stage] = []
        for i in range(len(cps) - 1):
            out.append(Stage(
                level=i,
                start_hour=cps[i],
                end_hour=cps[i + 1],
                branching_factor=self.branching_factor,
            ))
        return out

    @property
    def num_levels(self) -> int:
        return len(self.checkpoints_hours) - 1

    @property
    def total_duration_hours(self) -> float:
        return self.checkpoints_hours[-1] - self.checkpoints_hours[0]

    def trials_at_level(self, level: int) -> int:
        """Number of trials running at a given level (for one root trial)."""
        return self.branching_factor ** level


def no_split_plan(total_hours: float = 23.0) -> SplitPlan:
    """Baseline: single stage, no splitting."""
    return SplitPlan(checkpoints_hours=[0.0, total_hours], branching_factor=1)


def fixed_split_plan(
    split_times: List[float],
    total_hours: float = 23.0,
    branching_factor: int = 2,
) -> SplitPlan:
    """Build plan from explicit split times.

    Args:
        split_times: hours at which splits occur (e.g. [8.5, 14.0, 18.0]).
        total_hours: total experiment duration.
        branching_factor: descendants per parent at each split.
    """
    checkpoints = [0.0] + sorted(split_times) + [total_hours]
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for c in checkpoints:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return SplitPlan(checkpoints_hours=deduped, branching_factor=branching_factor)


# ---------------------------------------------------------------------------
# Load split plan from sparsity CSV (offline mode)
# ---------------------------------------------------------------------------

_ZONE_COLS = ("zone1_h", "zone2_h", "zone3_h")


def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    t = str(s).strip()
    if t == "" or t.lower() in ("none", "nan"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def load_sparsity_csv(csv_path: Path) -> Dict[Tuple[str, str], Dict[str, str]]:
    """Load sparsity_split_times_summary.csv → {(fuzzer, benchmark): row_dict}."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Sparsity CSV not found: {csv_path}")

    out: Dict[Tuple[str, str], Dict[str, str]] = {}
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bm = (row.get("benchmark") or "").strip()
            fz = (row.get("fuzzer") or "").strip()
            if not bm or not fz:
                continue
            out[(fz, bm)] = {k: (v if v is not None else "") for k, v in row.items()}
    return out


def plan_from_sparsity_csv(
    csv_path: Path,
    fuzzer: str,
    benchmark: str,
    total_hours: float = 23.0,
    branching_factor: int = 2,
) -> SplitPlan:
    """Build a SplitPlan for one (fuzzer, benchmark) pair from sparsity CSV.

    Rules:
      - Read zone1_h, zone2_h, zone3_h columns
      - Non-empty values become split times
      - If ALL empty → no split (single stage)
    """
    data = load_sparsity_csv(csv_path)
    key = (fuzzer, benchmark)
    row = data.get(key)
    if row is None:
        avail_fuzzers = sorted({fz for (fz, bm) in data if bm == benchmark})
        raise KeyError(
            f"({fuzzer}, {benchmark}) not in {csv_path}. "
            f"Available fuzzers for this benchmark: {avail_fuzzers}"
        )

    zones: List[float] = []
    for col in _ZONE_COLS:
        v = _to_float(row.get(col))
        if v is not None:
            zones.append(v)

    if not zones:
        # No split indicated by sparsity analysis
        return no_split_plan(total_hours)

    return fixed_split_plan(zones, total_hours, branching_factor)


def plan_from_sparsity_csv_batch(
    csv_path: Path,
    fuzzer: str,
    benchmarks: Sequence[str],
    total_hours: float = 23.0,
    branching_factor: int = 2,
) -> Dict[str, SplitPlan]:
    """Load plans for multiple benchmarks. Returns {benchmark: SplitPlan}."""
    return {
        bm: plan_from_sparsity_csv(csv_path, fuzzer, bm, total_hours, branching_factor)
        for bm in benchmarks
    }

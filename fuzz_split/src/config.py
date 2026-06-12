"""Central configuration for the split-fuzzing framework.

All tuneable parameters live here. CLI / YAML overrides happen in the
orchestrator; this module provides defaults and the Config dataclass.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent          # repo root
FUZZBENCH_DIR = PROJECT_ROOT / "fuzzbench"
RUN_EXPERIMENT_PY = FUZZBENCH_DIR / "experiment" / "run_experiment.py"

DEFAULT_EXPERIMENT_FILESTORE = PROJECT_ROOT / "results" / "experiment-data"
DEFAULT_REPORT_FILESTORE = PROJECT_ROOT / "results" / "report-data"
DEFAULT_TMP_DIR = PROJECT_ROOT / "tmp"

# ---------------------------------------------------------------------------
# FuzzBench / Docker defaults
# ---------------------------------------------------------------------------
DOCKER_REGISTRY = "gcr.io/fuzzbench"
CONCURRENT_BUILDS = 5
ALLOW_UNCOMMITTED = True

# ---------------------------------------------------------------------------
# Splitting defaults (paper Algorithm 2)
# ---------------------------------------------------------------------------
DEFAULT_BRANCHING_FACTOR = 2          # n_t descendants per split
DEFAULT_TOTAL_DURATION_HOURS = 23.0   # T
DEFAULT_SNAPSHOT_SECONDS = 360        # 6 min (matches FuzzBench patch)

# ---------------------------------------------------------------------------
# Sparsity defaults (paper Appendix C)
# ---------------------------------------------------------------------------
RATE_MODE = "window"                  # "tail" or "window"
WINDOW_HOURS = 1.0
PERSIST_HOURS = 0.0
THRESHOLDS = [0.5, 0.3, 0.15]
BUG_TRIGGER_AFTER_ZONE = True
NO_BUG_AFTER_ZONE_FALLBACK = "none"   # "none" | "zone" | "end"
MIN_GAP_HOURS = 1.5
NO_SPLIT_LAST_HOURS = 2.0
MAX_SPLITS = 3

# Richness-adaptive start hours
RICHNESS_METRIC = "bugs_end"
RICHNESS_TRANSFORM = "none"
Q_LOW = 0.2
Q_HIGH = 0.6
START_HOUR_MIN = 8.0
START_HOUR_MAX = 16.0
AUTO_FIT_POWER = True
PIVOT_Q = 0.50
PIVOT_HOUR = 12.0

# ---------------------------------------------------------------------------
# Online sparsity defaults
# ---------------------------------------------------------------------------
ONLINE_WINDOW_HOURS = 1.0
ONLINE_PERSIST_HOURS = 1.0


@dataclass
class SplitConfig:
    """Complete configuration for one split experiment."""

    # Identity
    experiment_name: str
    fuzzer: str
    benchmark: str                    # ONE benchmark per config (per-benchmark scheduling)

    # Paths
    project_root: Path = PROJECT_ROOT
    fuzzbench_dir: Path = FUZZBENCH_DIR
    experiment_filestore: Path = DEFAULT_EXPERIMENT_FILESTORE
    report_filestore: Path = DEFAULT_REPORT_FILESTORE
    tmp_dir: Path = DEFAULT_TMP_DIR

    # Split parameters
    mode: str = "offline"             # "offline" or "online"
    branching_factor: int = DEFAULT_BRANCHING_FACTOR
    total_duration_hours: float = DEFAULT_TOTAL_DURATION_HOURS
    split_times_hours: Optional[List[float]] = None  # offline: from sparsity CSV

    # CPU allocation
    runners_cpus: int = 20
    measurers_cpus: int = 8
    cpu_offset: int = 0
    runner_num_cpu_cores: int = 1

    # FuzzBench knobs
    docker_registry: str = DOCKER_REGISTRY
    concurrent_builds: int = CONCURRENT_BUILDS
    allow_uncommitted: bool = ALLOW_UNCOMMITTED
    snapshot_seconds: int = DEFAULT_SNAPSHOT_SECONDS

    # Seed management
    custom_seed_corpus_dir: Optional[Path] = None   # if None, use oss-fuzz default seeds
    seed_selection: str = "oss_fuzz"                 # "oss_fuzz" | "default" | "custom"

    # Resume
    start_from_level: int = 0         # 0 = start from scratch
    num_initial_trials: int = 1       # M: number of independent root trials

    @property
    def run_experiment_py(self) -> Path:
        return self.fuzzbench_dir / "experiment" / "run_experiment.py"

    @property
    def work_dir(self) -> Path:
        """Per-experiment working directory (avoids src.tar.gz collisions)."""
        return self.tmp_dir / self.experiment_name

    @property
    def seed_store_dir(self) -> Path:
        return self.work_dir / "seed-store"

    @property
    def tree_state_dir(self) -> Path:
        return self.work_dir / "tree-state"

    def __post_init__(self):
        self._name_cache = {}

    def level_experiment_name(self, root_trial: int, level: int, branch_id: str) -> str:
        """Unique FuzzBench experiment name for a specific branch at a level.

        Must match ^[a-z0-9-]{0,30}$.
        We compress the branch_id: "b0-1-0" -> "b010" to save chars.
        If truncation causes collision, append a hash suffix.
        """
        import hashlib
        bid_short = branch_id.replace("-", "")
        suffix = f"-r{root_trial}l{level}{bid_short}"
        max_prefix = 30 - len(suffix)

        if max_prefix >= 1:
            prefix = self.experiment_name[:max_prefix].rstrip("-")
            name = f"{prefix}{suffix}"
        else:
            # Suffix alone is too long, use hash
            full_id = f"{self.experiment_name}-r{root_trial}-L{level}-{branch_id}"
            h = hashlib.md5(full_id.encode()).hexdigest()[:12]
            name = f"s-{h}"

        name = name[:30]

        # Collision detection
        full_key = (root_trial, level, branch_id)
        if self._name_cache is not None:
            for existing_key, existing_name in self._name_cache.items():
                if existing_name == name and existing_key != full_key:
                    # Collision! Append hash to disambiguate
                    full_id = f"{self.experiment_name}-{root_trial}-{level}-{branch_id}"
                    h = hashlib.md5(full_id.encode()).hexdigest()[:6]
                    name = f"{name[:23]}-{h}"
                    break
            self._name_cache[full_key] = name

        return name

    def level_seed_dir(self, root_trial: int, level: int, branch_id: str) -> Path:
        """Directory holding seeds for a specific branch."""
        return self.work_dir / "seeds" / f"r{root_trial}" / f"L{level}" / branch_id

    def level_filestore(self, root_trial: int, level: int = None, branch_id: str = None) -> Path:
        """Per-branch filestore so each dispatcher gets its own SQLite .db.

        CRITICAL: Each branch MUST have its own experiment_filestore directory.
        The path includes the experiment_name so different experiment RUNS
        can NEVER share a local.db file. Without this, stale data from
        previous runs pollutes the DB and causes incorrect trial scheduling.

        Path: experiment_filestore/<experiment_name>/r<N>/l<L>-<branch>/
        """
        base = self.experiment_filestore / self.experiment_name / f"r{root_trial}"
        if level is not None and branch_id is not None:
            bid_short = branch_id.replace("-", "")
            return base / f"l{level}-{bid_short}"
        return base

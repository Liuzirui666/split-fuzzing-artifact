"""Split experiment orchestrator.

Runs a split experiment for ONE (benchmark, fuzzer) pair:
  1. Level 0: Run initial trial(s) for [0, split_time_1]
  2. Harvest corpus from each trial
  3. Level 1: For each parent trial, spawn branching_factor children
     ALL children across ALL parents run in PARALLEL
  4. Repeat until last level
  5. Final level: run until T

Wall-clock time = sum of stage durations (all branches at same level run in parallel).

Each branch gets its own FuzzBench experiment instance with:
  - Unique experiment name
  - Its parent's corpus as seed
  - Dedicated CPU allocation
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from .config import SplitConfig
from .split_plan import SplitPlan, Stage
from .seed_manager import (
    SeedManager,
    root_branch_id,
    child_branch_ids,
    all_branch_ids_at_level,
)


@dataclass
class BranchRun:
    """Tracks one branch's FuzzBench experiment."""
    root_trial: int
    level: int
    branch_id: str
    experiment_name: str
    seed_dir: Path
    cpu_offset: int
    parent_branch_id: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    return_code: Optional[int] = None


def _write_experiment_config(
    cfg: SplitConfig,
    stage: Stage,
    experiment_name: str,
    filestore: Path,
) -> Path:
    """Write a FuzzBench YAML config for one branch run."""
    cfg_dir = cfg.work_dir / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "trials": 1,  # always 1 trial per branch (the branch IS the trial)
        "max_total_time": stage.duration_seconds,
        "docker_registry": cfg.docker_registry,
        "experiment_filestore": str(filestore),
        "report_filestore": str(cfg.report_filestore),
        "local_experiment": True,
        "snapshot_period": cfg.snapshot_seconds,
        "runner_num_cpu_cores": cfg.runner_num_cpu_cores,
    }

    out_path = cfg_dir / f"{experiment_name}.yaml"
    out_path.write_text(yaml.safe_dump(config, sort_keys=False))
    return out_path


def _run_branch(
    cfg: SplitConfig,
    branch: BranchRun,
    config_path: Path,
) -> subprocess.Popen:
    """Launch a FuzzBench experiment for one branch."""
    workdir = cfg.work_dir / "workdirs" / branch.experiment_name
    workdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(cfg.run_experiment_py),
        "--experiment-config", str(config_path),
        "--experiment-name", branch.experiment_name,
        "--fuzzers", cfg.fuzzer,
        "--benchmarks", cfg.benchmark,
        "--concurrent-builds", str(cfg.concurrent_builds),
        "--runners-cpus", str(cfg.runners_cpus),
        "--measurers-cpus", str(cfg.measurers_cpus),
        "--cpu-offset", str(branch.cpu_offset),
    ]

    # Add custom seed corpus if we have one
    # Otherwise FuzzBench uses benchmark's default seeds (in benchmarks/<name>/seeds/)
    if branch.seed_dir.exists() and any(branch.seed_dir.iterdir()):
        cmd.extend(["--custom-seed-corpus-dir", str(branch.seed_dir)])

    if cfg.allow_uncommitted:
        cmd.append("--allow-uncommitted-changes")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(cfg.fuzzbench_dir)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(workdir),
        env=env,
    )
    return proc


def _pump_output(prefix: str, pipe, log_path: Optional[Path] = None) -> None:
    """Read subprocess stdout and echo with prefix. Optionally log to file."""
    log_file = None
    try:
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "w")
        for line in iter(pipe.readline, ""):
            if not line:
                break
            tagged = f"[{prefix}] {line}"
            sys.stdout.write(tagged)
            sys.stdout.flush()
            if log_file:
                log_file.write(tagged)
                log_file.flush()
    except Exception:
        pass
    finally:
        if log_file:
            log_file.close()


def _compute_cpu_offsets(
    base_offset: int,
    num_branches: int,
    runners_per_branch: int,
    measurers_per_branch: int,
) -> List[int]:
    """Compute CPU offsets for parallel branches.

    Each branch needs (runners + measurers) cores.
    """
    cores_per_branch = runners_per_branch + measurers_per_branch
    return [base_offset + i * cores_per_branch for i in range(num_branches)]


def _wait_all(branches: List[BranchRun], fail_fast: bool = True) -> bool:
    """Wait for all branch processes to complete. Returns True if all succeeded."""
    alive = [b for b in branches if b.process is not None]
    all_ok = True

    while alive:
        finished = []
        for b in list(alive):
            rc = b.process.poll()
            if rc is not None:
                b.return_code = rc
                finished.append(b)

        for b in finished:
            alive.remove(b)
            if b.return_code != 0:
                all_ok = False
                print(f"[orchestrator] FAILED: {b.experiment_name} (exit={b.return_code})")
                if fail_fast:
                    for a in alive:
                        try:
                            a.process.terminate()
                        except Exception:
                            pass
                    return False

        if alive:
            time.sleep(2)

    return all_ok


class SplitOrchestrator:
    """Orchestrates a complete split experiment for one (benchmark, fuzzer) pair."""

    def __init__(self, cfg: SplitConfig, plan: SplitPlan):
        self.cfg = cfg
        self.plan = plan
        self.seed_mgr = SeedManager(cfg.seed_store_dir, cfg.work_dir)
        self.stages = plan.stages()

        # Track seeds per branch: {(root_trial, branch_id): set_of_hashes}
        self.branch_seeds: Dict[Tuple[int, str], Set[str]] = {}

    def _initialize_root_seeds(self, root_trial: int) -> Path:
        """Set up seeds for the root trial (level 0).

        If custom_seed_corpus_dir is set, use those seeds.
        Otherwise, let FuzzBench use its default (oss-fuzz) seeds.
        """
        branch_id = root_branch_id()
        seed_dir = self.cfg.level_seed_dir(root_trial, 0, branch_id)

        if self.cfg.custom_seed_corpus_dir:
            # Copy custom seeds
            src = self.cfg.custom_seed_corpus_dir / self.cfg.benchmark
            if src.exists():
                dst = seed_dir / self.cfg.benchmark
                if dst.exists():
                    import shutil
                    shutil.rmtree(dst)
                dst.mkdir(parents=True, exist_ok=True)
                for f in src.iterdir():
                    if f.is_file():
                        import shutil
                        shutil.copy2(f, dst / f.name)
        else:
            # No custom seeds — FuzzBench will use default/oss-fuzz seeds
            seed_dir.mkdir(parents=True, exist_ok=True)

        return seed_dir

    def _harvest_and_branch(
        self,
        root_trial: int,
        level: int,
        parent_branches: List[BranchRun],
        branching_factor: int,
    ) -> List[BranchRun]:
        """Harvest corpus from completed parent branches, create child branches.

        Each parent's corpus goes ONLY to its own children (no cross-branch union).
        """
        next_level = level + 1
        child_runs: List[BranchRun] = []

        # Compute CPU offsets for all children at next level
        total_children = len(parent_branches) * branching_factor
        offsets = _compute_cpu_offsets(
            self.cfg.cpu_offset,
            total_children,
            self.cfg.runners_cpus,
            self.cfg.measurers_cpus,
        )

        child_idx = 0
        for parent in parent_branches:
            # Harvest parent's corpus (from its per-branch filestore)
            try:
                filestore = self.cfg.level_filestore(
                    root_trial, parent.level, parent.branch_id)
                trial_corpus_dir = self.seed_mgr.get_trial_corpus_dir(
                    experiment_filestore=filestore,
                    experiment_name=parent.experiment_name,
                    benchmark=self.cfg.benchmark,
                    fuzzer=self.cfg.fuzzer,
                    trial_num=0,  # always trial 0 (1 trial per branch)
                )

                tmp_dir = self.cfg.work_dir / "tmp-extract" / parent.experiment_name
                tmp_dir.mkdir(parents=True, exist_ok=True)

                parent_hashes = self.seed_mgr.harvest_trial_corpus(
                    benchmark=self.cfg.benchmark,
                    fuzzer=self.cfg.fuzzer,
                    trial_corpus_dir=trial_corpus_dir,
                    tmp_dir=tmp_dir,
                )

                # Merge with parent's previous seeds
                prev_key = (root_trial, parent.branch_id)
                prev_hashes = self.branch_seeds.get(prev_key, set())
                combined = prev_hashes | parent_hashes

                print(
                    f"[orchestrator] Harvested {parent.experiment_name}: "
                    f"{len(parent_hashes)} new seeds, "
                    f"{len(combined)} total"
                )

            except Exception as e:
                print(f"[orchestrator] WARNING: harvest failed for {parent.experiment_name}: {e}")
                combined = self.branch_seeds.get((root_trial, parent.branch_id), set())

            # Create children
            children = child_branch_ids(parent.branch_id, branching_factor)
            for child_id in children:
                # Store seeds for this child
                self.branch_seeds[(root_trial, child_id)] = combined.copy()

                # Materialize seeds to disk
                child_seed_dir = self.cfg.level_seed_dir(root_trial, next_level, child_id)
                self.seed_mgr.materialize_seeds(
                    benchmark=self.cfg.benchmark,
                    hashes=combined,
                    dest_dir=child_seed_dir,
                )

                exp_name = self.cfg.level_experiment_name(root_trial, next_level, child_id)
                child_runs.append(BranchRun(
                    root_trial=root_trial,
                    level=next_level,
                    branch_id=child_id,
                    experiment_name=exp_name,
                    seed_dir=child_seed_dir,
                    cpu_offset=offsets[child_idx],
                    parent_branch_id=parent.branch_id,
                ))
                child_idx += 1

        return child_runs

    def run(self, root_trial: int = 0) -> bool:
        """Execute the full split experiment for one root trial.

        Returns True on success.
        """
        print(f"\n{'='*60}")
        print(f"Split Experiment: {self.cfg.experiment_name}")
        print(f"Benchmark: {self.cfg.benchmark}  Fuzzer: {self.cfg.fuzzer}")
        print(f"Plan: {self.plan.checkpoints_hours} (branching={self.plan.branching_factor})")
        print(f"Root trial: {root_trial}")
        print(f"{'='*60}\n")

        current_branches: List[BranchRun] = []

        for stage_idx, stage in enumerate(self.stages):
            level = stage.level
            is_last = (stage_idx == len(self.stages) - 1)

            print(f"\n--- Level {level}: [{stage.start_hour}h - {stage.end_hour}h] "
                  f"({stage.duration_hours:.1f}h) ---")

            if level == 0:
                # Initialize root
                branch_id = root_branch_id()
                seed_dir = self._initialize_root_seeds(root_trial)
                exp_name = self.cfg.level_experiment_name(root_trial, 0, branch_id)

                current_branches = [BranchRun(
                    root_trial=root_trial,
                    level=0,
                    branch_id=branch_id,
                    experiment_name=exp_name,
                    seed_dir=seed_dir,
                    cpu_offset=self.cfg.cpu_offset,
                )]
            # else: current_branches already set by _harvest_and_branch

            print(f"Running {len(current_branches)} branch(es) in parallel...")

            # Launch all branches at this level in parallel
            for branch in current_branches:
                # Each branch gets its own filestore (isolates SQLite .db)
                filestore = self.cfg.level_filestore(
                    root_trial, branch.level, branch.branch_id)
                filestore.mkdir(parents=True, exist_ok=True)

                config_path = _write_experiment_config(
                    self.cfg, stage, branch.experiment_name, filestore
                )
                proc = _run_branch(self.cfg, branch, config_path)
                branch.process = proc

                log_path = self.cfg.work_dir / "logs" / f"{branch.experiment_name}.log"
                t = threading.Thread(
                    target=_pump_output,
                    args=(branch.experiment_name, proc.stdout, log_path),
                    daemon=True,
                )
                t.start()

                print(f"  Started {branch.experiment_name} (cpu_offset={branch.cpu_offset})")

            # Wait for all branches at this level to complete
            ok = _wait_all(current_branches)
            if not ok:
                print(f"[orchestrator] Level {level} FAILED. Aborting.")
                return False

            print(f"Level {level} completed successfully.")

            # Harvest and branch (unless this is the last level)
            if not is_last:
                current_branches = self._harvest_and_branch(
                    root_trial=root_trial,
                    level=level,
                    parent_branches=current_branches,
                    branching_factor=stage.branching_factor,
                )

        print(f"\n[orchestrator] Experiment {self.cfg.experiment_name} COMPLETED.")
        return True


def run_experiment(
    cfg: SplitConfig,
    plan: SplitPlan,
    root_trials: Optional[List[int]] = None,
) -> bool:
    """Run split experiment with multiple independent root trials.

    Root trials run SEQUENTIALLY (each uses the full CPU allocation).
    """
    if root_trials is None:
        root_trials = list(range(cfg.num_initial_trials))

    all_ok = True
    for rt in root_trials:
        orchestrator = SplitOrchestrator(cfg, plan)
        ok = orchestrator.run(root_trial=rt)
        if not ok:
            all_ok = False
            print(f"[experiment] Root trial {rt} failed.")

    return all_ok

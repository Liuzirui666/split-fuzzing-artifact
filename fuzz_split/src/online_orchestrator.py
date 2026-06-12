"""Online split orchestrator: branches decide INDEPENDENTLY when to split.

Unlike the offline orchestrator (fixed split times for all branches),
online mode gives each branch its own OnlineSparsityTracker. Each branch
runs until either:
  (a) its tracker detects sparsity + new bug => split, or
  (b) total time budget exhausted => no split, run to completion.

Different branches may be at different levels simultaneously:
  - Branch A might still be running Level 0
  - Branch B (same root trial) might already have split and its children
    are running at Level 1

CPU cores are allocated at the granularity of the MAXIMUM branches that
could exist at any point (branching_factor^max_splits * num_trials).
When a parent splits, its cores are redistributed to its children.

Usage:
  python -m src.online_orchestrator \\
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \\
    --total-hours 23 --max-splits 3 --branching-factor 2 \\
    --num-trials 5 --total-cores 188 --experiment-name exp1
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml

from .config import SplitConfig
from .online_sparsity import OnlineSparsityTracker, OnlineMonitor
from .seed_manager import (
    SeedManager,
    root_branch_id,
    child_branch_ids,
)
from .orchestrator import (
    BranchRun,
    _write_experiment_config,
    _run_branch,
    _pump_output,
)
from .split_plan import Stage
from .parallel_runner import CPULayout


# ---------------------------------------------------------------------------
# CPU layout for online mode
# ---------------------------------------------------------------------------

def compute_online_cpu_layout(
    total_cores: int,
    num_trials: int,
    branching_factor: int,
    max_splits: int,
    min_measurers: int = 1,
) -> CPULayout:
    """Compute CPU allocation for online mode.

    We must reserve for the worst case: every branch has split max_splits
    times, so each trial has branching_factor^max_splits leaves.
    """
    max_branches_per_trial = branching_factor ** max_splits
    total_concurrent = num_trials * max_branches_per_trial
    cores_per_branch = total_cores // total_concurrent

    if cores_per_branch < 2:
        raise ValueError(
            f"Not enough cores: {total_cores} total / {total_concurrent} branches = "
            f"{cores_per_branch} per branch (need >= 2). "
            f"Reduce --num-trials ({num_trials}) or --max-splits ({max_splits})."
        )

    runners = 1
    measurers = max(1, min(min_measurers, cores_per_branch - 1))

    layout = CPULayout(
        total_cores=total_cores,
        num_trials=num_trials,
        max_branches_per_trial=max_branches_per_trial,
        cores_per_branch=cores_per_branch,
        runners_per_branch=runners,
        measurers_per_branch=measurers,
    )
    layout.validate()
    return layout


# ---------------------------------------------------------------------------
# Live branch: a branch with an active FuzzBench experiment + tracker
# ---------------------------------------------------------------------------

@dataclass
class LiveBranch:
    """A branch that is currently running with its own sparsity tracker."""
    run: BranchRun
    tracker: OnlineSparsityTracker
    monitor: Optional[OnlineMonitor] = None
    # The set of core slot indices this branch occupies (for reuse by children)
    slot_indices: List[int] = field(default_factory=list)
    # How many splits this branch has already done (depth in the tree)
    depth: int = 0
    # Wall-clock time (hours) when this branch started
    start_wall_hours: float = 0.0
    # Elapsed experiment hours at start (for resuming time budget)
    elapsed_at_start: float = 0.0
    # Total time budget for this branch (hours)
    time_budget_hours: float = 0.0
    # Log pump thread
    log_thread: Optional[threading.Thread] = None
    # Finished flag
    finished: bool = False
    # Split flag
    split_triggered: bool = False


# ---------------------------------------------------------------------------
# Online orchestrator
# ---------------------------------------------------------------------------

class OnlineSplitOrchestrator:
    """Orchestrates online split experiments with independent per-branch splitting."""

    def __init__(
        self,
        cfg: SplitConfig,
        num_trials: int,
        layout: CPULayout,
        total_hours: float,
        max_splits: int,
        branching_factor: int,
        poll_interval_seconds: int = 60,
    ):
        self.cfg = cfg
        self.num_trials = num_trials
        self.layout = layout
        self.total_hours = total_hours
        self.max_splits = max_splits
        self.branching_factor = branching_factor
        self.poll_interval = poll_interval_seconds

        # Per-trial seed managers
        self.seed_mgrs: Dict[int, SeedManager] = {}
        for rt in range(num_trials):
            store = cfg.seed_store_dir / f"r{rt}"
            self.seed_mgrs[rt] = SeedManager(store, cfg.work_dir)

        # Per-branch seed hashes: {(root_trial, branch_id): set_of_hashes}
        self.branch_seeds: Dict[Tuple[int, str], Set[str]] = {}

        # All live (currently running) branches
        self.live_branches: List[LiveBranch] = []

        # Lock for modifying live_branches (poll thread vs main thread)
        self._lock = threading.Lock()

        # Global shutdown flag
        self._shutdown = False

        # Track wall-clock start
        self._experiment_start: float = 0.0

    # ----- helpers -----

    def _wall_hours_elapsed(self) -> float:
        """Hours since experiment start."""
        return (time.time() - self._experiment_start) / 3600.0

    def _make_cfg_for_branch(self, branch: BranchRun) -> SplitConfig:
        """Create a SplitConfig with per-branch CPU offset."""
        return SplitConfig(
            experiment_name=self.cfg.experiment_name,
            fuzzer=self.cfg.fuzzer,
            benchmark=self.cfg.benchmark,
            project_root=self.cfg.project_root,
            fuzzbench_dir=self.cfg.fuzzbench_dir,
            experiment_filestore=self.cfg.experiment_filestore,
            report_filestore=self.cfg.report_filestore,
            tmp_dir=self.cfg.tmp_dir,
            runners_cpus=1,
            measurers_cpus=self.layout.measurers_per_branch,
            cpu_offset=branch.cpu_offset,
            runner_num_cpu_cores=1,
            concurrent_builds=self.cfg.concurrent_builds,
            allow_uncommitted=self.cfg.allow_uncommitted,
            snapshot_seconds=self.cfg.snapshot_seconds,
            docker_registry=self.cfg.docker_registry,
        )

    def _slot_indices_for_trial_branch(
        self, trial_idx: int, branch_depth: int, branch_id: str,
    ) -> List[int]:
        """Compute which slot indices (in the global pool) a branch occupies.

        At depth 0: the trial owns max_branches_per_trial slots.
        At depth d: each branch owns max_branches_per_trial / branching_factor^d slots.
        The slot range is determined by the branch's position in the tree.
        """
        total_slots = self.layout.max_branches_per_trial
        slots_per_branch = total_slots // (self.branching_factor ** branch_depth)
        if slots_per_branch < 1:
            slots_per_branch = 1

        # Determine position from branch_id: "b0" -> 0, "b0-1" -> 1, "b0-0-1" -> 1, etc.
        # The branch index within its level is encoded in the last segment
        # But we need the GLOBAL offset within the trial's slot range.
        # Parse the branch path to get the absolute slot offset.
        parts = branch_id.split("-")
        # parts[0] = "b0", parts[1:] = child indices at each depth
        offset = 0
        remaining = total_slots
        for i, part in enumerate(parts[1:], 1):
            remaining = remaining // self.branching_factor
            child_idx = int(part)
            offset += child_idx * remaining

        base = trial_idx * total_slots
        return list(range(base + offset, base + offset + slots_per_branch))

    def _cpu_offset_for_slots(self, slot_indices: List[int]) -> int:
        """CPU offset for a set of slots (use the first slot)."""
        return slot_indices[0] * self.layout.cores_per_branch

    def _init_root_seeds(self, root_trial: int) -> Path:
        """Initialize seeds for root trial."""
        branch_id = root_branch_id()
        seed_dir = self.cfg.level_seed_dir(root_trial, 0, branch_id)
        seed_dir.mkdir(parents=True, exist_ok=True)

        if self.cfg.custom_seed_corpus_dir:
            src = self.cfg.custom_seed_corpus_dir / self.cfg.benchmark
            if src.exists():
                dst = seed_dir / self.cfg.benchmark
                if dst.exists():
                    shutil.rmtree(dst)
                dst.mkdir(parents=True, exist_ok=True)
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst / f.name)

        return seed_dir

    def _launch_branch(self, live: LiveBranch) -> None:
        """Launch a FuzzBench experiment for a LiveBranch."""
        branch = live.run
        duration_seconds = int(live.time_budget_hours * 3600)
        if duration_seconds < 60:
            print(f"  [online] Skipping {branch.experiment_name}: "
                  f"only {duration_seconds}s remaining")
            live.finished = True
            return

        # Create a Stage for this branch's duration
        stage = Stage(
            level=branch.level,
            start_hour=live.elapsed_at_start,
            end_hour=live.elapsed_at_start + live.time_budget_hours,
            branching_factor=self.branching_factor,
        )

        # Each branch gets its own filestore
        filestore = self.cfg.level_filestore(
            branch.root_trial, branch.level, branch.branch_id)
        filestore.mkdir(parents=True, exist_ok=True)

        branch_cfg = self._make_cfg_for_branch(branch)
        config_path = _write_experiment_config(
            branch_cfg, stage, branch.experiment_name, filestore,
        )

        proc = _run_branch(branch_cfg, branch, config_path)
        branch.process = proc

        log_path = self.cfg.work_dir / "logs" / f"{branch.experiment_name}.log"
        t = threading.Thread(
            target=_pump_output,
            args=(branch.experiment_name, proc.stdout, log_path),
            daemon=True,
        )
        t.start()
        live.log_thread = t

        # Set up the online monitor
        monitor_filestore = self.cfg.level_filestore(
            branch.root_trial, branch.level, branch.branch_id)
        live.monitor = OnlineMonitor(
            experiment_filestore=monitor_filestore,
            experiment_name=branch.experiment_name,
            benchmark=self.cfg.benchmark,
            fuzzer=self.cfg.fuzzer,
            tracker=live.tracker,
            poll_interval_seconds=self.poll_interval,
        )

        live.start_wall_hours = self._wall_hours_elapsed()

        print(f"  [online] Started {branch.experiment_name} "
              f"(trial={branch.root_trial}, depth={live.depth}, "
              f"budget={live.time_budget_hours:.1f}h, "
              f"cores {branch.cpu_offset}-"
              f"{branch.cpu_offset + self.layout.cores_per_branch * len(live.slot_indices) - 1})")

    def _stop_branch(self, live: LiveBranch) -> None:
        """Stop a running branch's FuzzBench experiment."""
        branch = live.run
        if branch.process is None:
            return

        print(f"  [online] Stopping {branch.experiment_name}...")

        # Send SIGTERM, then wait up to 30s, then SIGKILL
        try:
            branch.process.terminate()
        except Exception:
            pass

        try:
            branch.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                branch.process.kill()
                branch.process.wait(timeout=10)
            except Exception:
                pass

        branch.return_code = branch.process.returncode

        # Also stop any Docker containers for this experiment
        from .utils import kill_all_fuzzbench_containers
        kill_all_fuzzbench_containers(branch.experiment_name)

        if live.monitor:
            live.monitor.stop()

        live.finished = True
        print(f"  [online] Stopped {branch.experiment_name} (rc={branch.return_code})")

    def _harvest_and_spawn_children(self, live: LiveBranch) -> List[LiveBranch]:
        """Harvest corpus from a stopped branch and spawn children.

        Returns list of new LiveBranch instances (one per child).
        """
        branch = live.run
        root_trial = branch.root_trial
        mgr = self.seed_mgrs[root_trial]
        next_depth = live.depth + 1

        # Compute how much time has elapsed for this branch
        branch_elapsed = self._wall_hours_elapsed() - live.start_wall_hours
        total_elapsed = live.elapsed_at_start + branch_elapsed
        remaining_hours = self.total_hours - total_elapsed

        if remaining_hours < 0.1:
            print(f"  [online] No time remaining after split of {branch.experiment_name}")
            return []

        # Harvest parent corpus
        combined: Set[str] = set()
        try:
            filestore = self.cfg.level_filestore(
                root_trial, branch.level, branch.branch_id)
            trial_corpus_dir = mgr.get_trial_corpus_dir(
                experiment_filestore=filestore,
                experiment_name=branch.experiment_name,
                benchmark=self.cfg.benchmark,
                fuzzer=self.cfg.fuzzer,
                trial_num=0,
            )
            tmp_dir = self.cfg.work_dir / "tmp-extract" / branch.experiment_name
            tmp_dir.mkdir(parents=True, exist_ok=True)

            parent_hashes = mgr.harvest_trial_corpus(
                benchmark=self.cfg.benchmark,
                fuzzer=self.cfg.fuzzer,
                trial_corpus_dir=trial_corpus_dir,
                tmp_dir=tmp_dir,
            )
            prev = self.branch_seeds.get((root_trial, branch.branch_id), set())
            combined = prev | parent_hashes
            print(f"  [online] Harvested {branch.experiment_name}: "
                  f"{len(parent_hashes)} new, {len(combined)} total seeds")
        except Exception as e:
            print(f"  [online] WARNING: harvest failed for {branch.experiment_name}: {e}")
            combined = self.branch_seeds.get((root_trial, branch.branch_id), set())

        # Distribute parent's slots among children
        parent_slots = live.slot_indices
        slots_per_child = len(parent_slots) // self.branching_factor
        if slots_per_child < 1:
            # Not enough slots to split further; each child gets 1 slot
            slots_per_child = 1

        # Create children
        child_ids = child_branch_ids(branch.branch_id, self.branching_factor)
        children: List[LiveBranch] = []

        for i, child_id in enumerate(child_ids):
            # Assign slots
            child_slots = parent_slots[i * slots_per_child : (i + 1) * slots_per_child]
            if not child_slots:
                child_slots = [parent_slots[i % len(parent_slots)]]

            # Store seeds
            self.branch_seeds[(root_trial, child_id)] = combined.copy()

            # Materialize seeds
            next_level = branch.level + 1
            child_seed_dir = self.cfg.level_seed_dir(root_trial, next_level, child_id)
            mgr.materialize_seeds(self.cfg.benchmark, combined, child_seed_dir)

            # Create BranchRun
            exp_name = self.cfg.level_experiment_name(root_trial, next_level, child_id)
            cpu_offset = self._cpu_offset_for_slots(child_slots)

            child_run = BranchRun(
                root_trial=root_trial,
                level=next_level,
                branch_id=child_id,
                experiment_name=exp_name,
                seed_dir=child_seed_dir,
                cpu_offset=cpu_offset,
                parent_branch_id=branch.branch_id,
            )

            # Create tracker for child (independent)
            child_tracker = OnlineSparsityTracker(
                total_hours=self.total_hours,
            )

            child_live = LiveBranch(
                run=child_run,
                tracker=child_tracker,
                slot_indices=child_slots,
                depth=next_depth,
                elapsed_at_start=total_elapsed,
                time_budget_hours=remaining_hours,
            )
            children.append(child_live)

        return children

    def _prebuild_images(self) -> None:
        """Pre-build Docker images (same as ParallelSplitRunner)."""
        fuzzbench_dir = self.cfg.fuzzbench_dir
        target = f"build-{self.cfg.fuzzer}-{self.cfg.benchmark}"
        cmd = ["make", "-f", "docker/generated.mk", target]
        print(f"  Running: {' '.join(cmd)}")
        result = subprocess.run(
            cmd, cwd=str(fuzzbench_dir),
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            print(f"  Build returned {result.returncode}, trying base-image only...")
            subprocess.run(
                ["make", "-f", "docker/generated.mk", "base-image"],
                cwd=str(fuzzbench_dir), capture_output=True, timeout=600,
            )

    # ----- main loop -----

    def run(self) -> bool:
        """Execute the online split experiment.

        Main loop:
          1. Launch Level 0 branches (one per trial)
          2. Poll each branch's bugs_covered
          3. When a tracker triggers split:
             - Stop branch
             - Harvest corpus
             - Spawn children (each with own tracker)
          4. When a branch's process exits naturally => mark finished
          5. Repeat until all branches finished or time exhausted
        """
        layout = self.layout

        print(f"\n{'='*70}")
        print(f"ONLINE SPLIT EXPERIMENT: {self.cfg.experiment_name}")
        print(f"Benchmark: {self.cfg.benchmark}  Fuzzer: {self.cfg.fuzzer}")
        print(f"Total hours: {self.total_hours}  Max splits: {self.max_splits}")
        print(f"Branching factor: {self.branching_factor}")
        print(f"Root trials: {self.num_trials}")
        print(f"CPU layout: {layout.total_cores} cores, "
              f"{layout.cores_per_branch} per branch "
              f"({layout.runners_per_branch}R + {layout.measurers_per_branch}M)")
        print(f"Max concurrent branches: {self.num_trials * layout.max_branches_per_trial}")
        print(f"Total cores used: {layout.total_used} / {layout.total_cores}")
        print(f"Poll interval: {self.poll_interval}s")
        print(f"{'='*70}\n")

        # --- PRE-FLIGHT ---

        # RAM safety
        max_concurrent = self.num_trials * layout.max_branches_per_trial
        RAM_PER_BRANCH_GB = 5.0
        estimated_ram = max_concurrent * RAM_PER_BRANCH_GB
        try:
            from .utils import get_total_ram_gb, get_available_ram_gb
            total_ram = get_total_ram_gb()
            avail_ram = get_available_ram_gb()
            print(f"RAM check: estimated {estimated_ram:.0f} GB needed, "
                  f"{avail_ram:.0f} GB available / {total_ram:.0f} GB total")
            if estimated_ram > total_ram * 0.85:
                print(f"FATAL: Estimated RAM {estimated_ram:.0f} GB > 85% of "
                      f"{total_ram:.0f} GB. Reduce --num-trials or --max-splits.")
                return False
        except Exception as e:
            print(f"WARNING: RAM check failed: {e}")

        # Conflicting containers
        from .utils import check_no_conflicting_containers, kill_all_fuzzbench_containers
        conflicts = check_no_conflicting_containers(self.cfg.experiment_name)
        if conflicts:
            print(f"WARNING: Found {len(conflicts)} conflicting containers")
            kill_all_fuzzbench_containers(self.cfg.experiment_name)
            time.sleep(3)

        # Clean old filestore
        exp_filestore = self.cfg.experiment_filestore / self.cfg.experiment_name
        if exp_filestore.exists():
            print(f"Cleaning old filestore: {exp_filestore}")
            shutil.rmtree(exp_filestore)
        exp_filestore.mkdir(parents=True, exist_ok=True)

        # Pre-build Docker images
        print("\nPre-building Docker images...")
        self._prebuild_images()
        print("Docker images ready.\n")

        # --- LAUNCH LEVEL 0 ---
        self._experiment_start = time.time()

        for rt in range(self.num_trials):
            seed_dir = self._init_root_seeds(rt)
            branch_id = root_branch_id()
            slots = self._slot_indices_for_trial_branch(rt, 0, branch_id)
            cpu_offset = self._cpu_offset_for_slots(slots)

            exp_name = self.cfg.level_experiment_name(rt, 0, branch_id)
            branch_run = BranchRun(
                root_trial=rt,
                level=0,
                branch_id=branch_id,
                experiment_name=exp_name,
                seed_dir=seed_dir,
                cpu_offset=cpu_offset,
            )

            tracker = OnlineSparsityTracker(total_hours=self.total_hours)
            live = LiveBranch(
                run=branch_run,
                tracker=tracker,
                slot_indices=slots,
                depth=0,
                elapsed_at_start=0.0,
                time_budget_hours=self.total_hours,
            )
            self._launch_branch(live)
            self.live_branches.append(live)

        print(f"\nLaunched {len(self.live_branches)} root branches. "
              f"Entering poll loop (interval={self.poll_interval}s)...\n")

        # --- POLL LOOP ---
        try:
            while not self._shutdown:
                active = [lb for lb in self.live_branches if not lb.finished]
                if not active:
                    break

                # Check wall-clock budget
                elapsed_wall = self._wall_hours_elapsed()
                if elapsed_wall > self.total_hours + 0.5:
                    print(f"\n[online] Wall-clock budget exhausted "
                          f"({elapsed_wall:.1f}h > {self.total_hours}h). "
                          f"Stopping all remaining branches.")
                    for lb in active:
                        self._stop_branch(lb)
                    break

                # Check each active branch
                to_split: List[LiveBranch] = []
                newly_finished: List[LiveBranch] = []

                for lb in active:
                    proc = lb.run.process
                    if proc is None:
                        continue

                    # Check if process exited naturally
                    rc = proc.poll()
                    if rc is not None:
                        lb.run.return_code = rc
                        lb.finished = True
                        newly_finished.append(lb)
                        status = "OK" if rc == 0 else f"FAILED (rc={rc})"
                        print(f"  [online] {lb.run.experiment_name} finished: {status}")
                        continue

                    # Check if this branch can still split
                    if lb.depth >= self.max_splits:
                        continue  # already at max depth, just let it run

                    # Poll for bugs_covered
                    if lb.monitor:
                        result = lb.monitor._get_latest_bugs()
                        if result:
                            t_h, bugs = result
                            lb.monitor.tracker.update(t_h, bugs)
                            # Log every 10th poll to avoid spam
                            poll_count = getattr(lb, '_poll_count', 0) + 1
                            lb._poll_count = poll_count
                            if poll_count % 10 == 1:
                                rho = lb.monitor.tracker._compute_rho(t_h, bugs) if t_h > lb.monitor.tracker.window_hours else 0
                                print(f"  [poll] {lb.run.experiment_name}: t={t_h:.2f}h bugs={bugs:.0f} rho={rho:.3f} stage={lb.monitor.tracker._current_stage}")
                        split_time = lb.monitor.tracker.check_split()
                        if split_time is not None:
                            print(f"\n  [online] SPLIT TRIGGERED for "
                                  f"{lb.run.experiment_name} at t={split_time:.2f}h")
                            lb.split_triggered = True
                            to_split.append(lb)

                # Process splits
                for lb in to_split:
                    self._stop_branch(lb)
                    children = self._harvest_and_spawn_children(lb)
                    for child in children:
                        self._launch_branch(child)
                        self.live_branches.append(child)
                    if children:
                        print(f"  [online] Spawned {len(children)} children "
                              f"from {lb.run.experiment_name}")

                # Sleep before next poll
                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            print("\n[online] Interrupted! Stopping all branches...")
            for lb in self.live_branches:
                if not lb.finished:
                    self._stop_branch(lb)
            return False

        # --- SUMMARY ---
        print(f"\n{'='*70}")
        print(f"ONLINE EXPERIMENT {self.cfg.experiment_name} COMPLETED")
        print(f"Wall time: {self._wall_hours_elapsed():.1f}h")
        print(f"Total branches launched: {len(self.live_branches)}")

        # Per-trial summary
        for rt in range(self.num_trials):
            trial_branches = [lb for lb in self.live_branches if lb.run.root_trial == rt]
            max_depth = max(lb.depth for lb in trial_branches) if trial_branches else 0
            splits = sum(1 for lb in trial_branches if lb.split_triggered)
            finished_ok = sum(1 for lb in trial_branches
                              if lb.finished and lb.run.return_code == 0)
            print(f"  Trial {rt}: {len(trial_branches)} branches, "
                  f"max_depth={max_depth}, splits={splits}, "
                  f"finished_ok={finished_ok}/{len(trial_branches)}")

        failed = [lb for lb in self.live_branches
                  if lb.finished and lb.run.return_code not in (None, 0, -15)]
        if failed:
            print(f"\nWARNING: {len(failed)} branch(es) failed:")
            for lb in failed:
                print(f"  {lb.run.experiment_name} rc={lb.run.return_code}")

        print(f"Results: {self.cfg.experiment_filestore}")
        print(f"{'='*70}")

        # Consider SIGTERM exits (-15) as OK (we intentionally stopped them)
        real_failures = [lb for lb in self.live_branches
                         if lb.finished and lb.run.return_code not in (None, 0, -15)]
        return len(real_failures) == 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    p = argparse.ArgumentParser(
        description="Online split experiment runner (per-branch independent splitting)")

    p.add_argument("--experiment-name", required=True)
    p.add_argument("--fuzzer", required=True)
    p.add_argument("--benchmark", required=True)

    p.add_argument("--total-hours", type=float, default=23.0)
    p.add_argument("--max-splits", type=int, default=3,
                    help="Maximum number of splits per branch (tree depth limit)")
    p.add_argument("--branching-factor", type=int, default=2)

    p.add_argument("--num-trials", type=int, default=5,
                    help="N parallel root trials")
    p.add_argument("--total-cores", type=int, default=188,
                    help="Total CPU cores available")
    p.add_argument("--min-measurers", type=int, default=1)
    p.add_argument("--poll-interval", type=int, default=60,
                    help="Seconds between sparsity polls")

    # Online sparsity tuning
    p.add_argument("--window-hours", type=float, default=None,
                    help="Online sparsity window (hours)")
    p.add_argument("--persist-hours", type=float, default=None,
                    help="Online sparsity persistence horizon (hours)")
    p.add_argument("--thresholds", type=float, nargs="*", default=None,
                    help="Sparsity thresholds (e.g. 0.5 0.3 0.15)")

    p.add_argument("--concurrent-builds", type=int, default=5)
    p.add_argument("--snapshot-seconds", type=int, default=360)
    p.add_argument("--custom-seeds", default=None)
    p.add_argument("--experiment-filestore", default=None)
    p.add_argument("--report-filestore", default=None)

    args = p.parse_args()

    from .config import PROJECT_ROOT

    # Compute CPU layout
    layout = compute_online_cpu_layout(
        total_cores=args.total_cores,
        num_trials=args.num_trials,
        branching_factor=args.branching_factor,
        max_splits=args.max_splits,
        min_measurers=args.min_measurers,
    )

    # Build config
    cfg = SplitConfig(
        experiment_name=args.experiment_name,
        fuzzer=args.fuzzer,
        benchmark=args.benchmark,
        mode="online",
        experiment_filestore=Path(args.experiment_filestore) if args.experiment_filestore
            else PROJECT_ROOT / "results" / "experiment-data",
        report_filestore=Path(args.report_filestore) if args.report_filestore
            else PROJECT_ROOT / "results" / "report-data",
        concurrent_builds=args.concurrent_builds,
        snapshot_seconds=args.snapshot_seconds,
        branching_factor=args.branching_factor,
        total_duration_hours=args.total_hours,
    )
    if args.custom_seeds:
        cfg.custom_seed_corpus_dir = Path(args.custom_seeds)

    # Override online sparsity defaults if specified
    import src.config as C
    if args.window_hours is not None:
        C.ONLINE_WINDOW_HOURS = args.window_hours
    if args.persist_hours is not None:
        C.ONLINE_PERSIST_HOURS = args.persist_hours

    orchestrator = OnlineSplitOrchestrator(
        cfg=cfg,
        num_trials=args.num_trials,
        layout=layout,
        total_hours=args.total_hours,
        max_splits=args.max_splits,
        branching_factor=args.branching_factor,
        poll_interval_seconds=args.poll_interval,
    )

    # Handle signals
    def _sigterm(sig, frame):
        print("\n[SIGTERM] Shutting down...")
        orchestrator._shutdown = True
    signal.signal(signal.SIGTERM, _sigterm)

    try:
        ok = orchestrator.run()
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        sys.exit(130)


if __name__ == "__main__":
    main()

"""Parallel runner: manages N independent root trials with proper CPU allocation.

Given 188 cores (configurable), N root trials, and a split plan with branching
factor B and K split points:
  - Maximum branches at any level = B^K
  - Total concurrent branches at deepest level = N * B^K
  - Each branch needs (runners_cpus + measurers_cpus) cores
  - Must not exceed total_cores

The runner automatically computes per-branch CPU allocation and offsets:
  cores_per_branch = total_cores // (N * max_branches_per_trial)
  runners = cores_per_branch - measurers  (at least 1 measurer)

All N root trials run their levels IN SYNC: all N Level-0 trials run together,
then all harvest together, then all N*B Level-1 branches run together, etc.
This maximizes core utilization at every level.

Usage:
  python -m src.parallel_runner \
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \
    --split-times 8.0 14.0 18.0 --total-hours 23 \
    --num-trials 5 --total-cores 188 --experiment-name exp1
"""
from __future__ import annotations

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
from .split_plan import SplitPlan, Stage, fixed_split_plan, no_split_plan, plan_from_sparsity_csv
from .seed_manager import (
    SeedManager,
    root_branch_id,
    child_branch_ids,
    all_branch_ids_at_level,
)
from .orchestrator import (
    BranchRun,
    _write_experiment_config,
    _run_branch,
    _pump_output,
    _wait_all,
)


# =====================================================================
# HARD RULES — written directly here, not in a separate module
# =====================================================================
MIN_FREE_DISK_GB = 300


def _check_disk_or_abort(context: str = "") -> int:
    """Check free disk. ABORT if below 300GB. Returns free GB."""
    result = subprocess.run(
        ["df", "--output=avail", "-BG", "/"],
        capture_output=True, text=True,
    )
    free_gb = int(result.stdout.strip().split("\n")[-1].replace("G", ""))
    if free_gb < MIN_FREE_DISK_GB:
        print(f"\n!!!! ABORT: DISK {free_gb}GB < {MIN_FREE_DISK_GB}GB !!!!")
        print(f"  Context: {context}")
        sys.exit(1)
    return free_gb


def _kill_only_our_containers(experiment_prefix: str) -> int:
    """Kill ONLY dispatcher-d-{prefix}* containers. Nothing else."""
    if not experiment_prefix:
        print("!!!! ABORT: must provide experiment prefix !!!!")
        sys.exit(1)
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    killed = 0
    for name in result.stdout.strip().split("\n"):
        if not name:
            continue
        if name.startswith(f"dispatcher-d-{experiment_prefix}"):
            subprocess.run(["docker", "rm", "-f", name],
                          capture_output=True, timeout=30)
            killed += 1
    return killed


@dataclass
class CPULayout:
    """CPU allocation for the experiment."""
    total_cores: int
    num_trials: int
    max_branches_per_trial: int   # B^K at deepest level
    cores_per_branch: int
    runners_per_branch: int
    measurers_per_branch: int
    base_cpu_offset: int = 0      # global offset to avoid collisions with other experiments

    def offset_for(self, trial_idx: int, branch_idx_in_trial: int) -> int:
        """Compute CPU offset for a specific branch."""
        global_idx = trial_idx * self.max_branches_per_trial + branch_idx_in_trial
        return self.base_cpu_offset + global_idx * self.cores_per_branch

    @property
    def total_used(self) -> int:
        return self.num_trials * self.max_branches_per_trial * self.cores_per_branch

    def validate(self):
        if self.total_used > self.total_cores:
            raise ValueError(
                f"CPU overcommit: {self.total_used} needed > {self.total_cores} available. "
                f"Reduce --num-trials or --branching-factor."
            )
        if self.runners_per_branch < 1:
            raise ValueError(
                f"Not enough cores: runners_per_branch={self.runners_per_branch}. "
                f"Reduce --num-trials or --branching-factor."
            )
        if self.measurers_per_branch < 1:
            raise ValueError(
                f"Not enough cores: measurers_per_branch={self.measurers_per_branch}. "
                f"Reduce --num-trials or --branching-factor."
            )


def compute_cpu_layout(
    total_cores: int,
    num_trials: int,
    plan: SplitPlan,
    min_measurers: int = 1,
    base_cpu_offset: int = 0,
) -> CPULayout:
    """Compute CPU allocation that never exceeds total_cores.

    Each branch needs 2 cores minimum (1 runner + 1 measurer).
    FuzzBench fuzzers (AFL, libfuzzer, etc.) are single-threaded,
    so 1 runner core per branch is correct.

    At the deepest level, we have N * B^K concurrent branches.
    Each branch gets floor(total_cores / (N * B^K)) cores.
    """
    max_level = plan.num_levels - 1
    max_branches_per_trial = plan.trials_at_level(max_level)
    total_concurrent = num_trials * max_branches_per_trial

    cores_per_branch = total_cores // total_concurrent
    if cores_per_branch < 2:
        raise ValueError(
            f"Not enough cores: {total_cores} total / {total_concurrent} branches = "
            f"{cores_per_branch} per branch (need >= 2). "
            f"Reduce --num-trials ({num_trials}) or splits ({max_level})."
        )

    # 1 runner core (single-threaded fuzzer), rest to measurer (min 1)
    runners = 1
    measurers = max(1, min(min_measurers, cores_per_branch - 1))

    layout = CPULayout(
        total_cores=total_cores,
        num_trials=num_trials,
        max_branches_per_trial=max_branches_per_trial,
        cores_per_branch=cores_per_branch,
        runners_per_branch=runners,
        measurers_per_branch=measurers,
        base_cpu_offset=base_cpu_offset,
    )
    layout.validate()
    return layout


class ParallelSplitRunner:
    """Run N root trials in sync, all sharing the CPU pool."""

    def __init__(
        self,
        cfg: SplitConfig,
        plan: SplitPlan,
        num_trials: int,
        layout: CPULayout,
    ):
        self.cfg = cfg
        self.plan = plan
        self.num_trials = num_trials
        self.layout = layout
        self.stages = plan.stages()

        # Per-trial seed managers
        self.seed_mgrs: Dict[int, SeedManager] = {}
        for rt in range(num_trials):
            store = cfg.seed_store_dir / f"r{rt}"
            self.seed_mgrs[rt] = SeedManager(store, cfg.work_dir)

        # Per-trial branch seeds: {(root_trial, branch_id): set_of_hashes}
        self.branch_seeds: Dict[Tuple[int, str], Set[str]] = {}

    def _make_branch(
        self,
        root_trial: int,
        level: int,
        branch_id: str,
        branch_idx_in_trial: int,
    ) -> BranchRun:
        """Create a BranchRun with proper CPU offset."""
        offset = self.layout.offset_for(root_trial, branch_idx_in_trial)
        exp_name = self.cfg.level_experiment_name(root_trial, level, branch_id)
        seed_dir = self.cfg.level_seed_dir(root_trial, level, branch_id)
        return BranchRun(
            root_trial=root_trial,
            level=level,
            branch_id=branch_id,
            experiment_name=exp_name,
            seed_dir=seed_dir,
            cpu_offset=offset,
        )

    def _init_root_seeds(self, root_trial: int) -> Path:
        """Initialize seeds for root trial."""
        branch_id = root_branch_id()
        seed_dir = self.cfg.level_seed_dir(root_trial, 0, branch_id)
        seed_dir.mkdir(parents=True, exist_ok=True)

        if self.cfg.custom_seed_corpus_dir:
            import shutil
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

    def _harvest_and_branch_trial(
        self,
        root_trial: int,
        level: int,
        parent_branches: List[BranchRun],
        branching_factor: int,
    ) -> List[BranchRun]:
        """Harvest one trial's branches and create children."""
        next_level = level + 1
        child_runs: List[BranchRun] = []
        mgr = self.seed_mgrs[root_trial]

        child_idx = 0
        for parent in parent_branches:
            # Harvest parent corpus (from its per-branch filestore)
            try:
                filestore = self.cfg.level_filestore(
                    root_trial, parent.level, parent.branch_id)
                trial_corpus_dir = mgr.get_trial_corpus_dir(
                    experiment_filestore=filestore,
                    experiment_name=parent.experiment_name,
                    benchmark=self.cfg.benchmark,
                    fuzzer=self.cfg.fuzzer,
                    trial_num=0,
                )
                tmp_dir = self.cfg.work_dir / "tmp-extract" / parent.experiment_name
                tmp_dir.mkdir(parents=True, exist_ok=True)

                parent_hashes = mgr.harvest_trial_corpus(
                    benchmark=self.cfg.benchmark,
                    fuzzer=self.cfg.fuzzer,
                    trial_corpus_dir=trial_corpus_dir,
                    tmp_dir=tmp_dir,
                )
                prev = self.branch_seeds.get((root_trial, parent.branch_id), set())
                combined = prev | parent_hashes
                print(f"  [r{root_trial}] Harvested {parent.branch_id}: "
                      f"{len(parent_hashes)} new, {len(combined)} total seeds")
            except Exception as e:
                print(f"  [r{root_trial}] WARNING harvest {parent.branch_id}: {e}")
                combined = self.branch_seeds.get((root_trial, parent.branch_id), set())

            # Create children
            children = child_branch_ids(parent.branch_id, branching_factor)
            for cid in children:
                self.branch_seeds[(root_trial, cid)] = combined.copy()
                child_seed_dir = self.cfg.level_seed_dir(root_trial, next_level, cid)
                mgr.materialize_seeds(self.cfg.benchmark, combined, child_seed_dir)

                child_runs.append(self._make_branch(
                    root_trial, next_level, cid, child_idx,
                ))
                child_idx += 1

        return child_runs

    def _prebuild_images(self) -> None:
        """Pre-build Docker images for the fuzzer/benchmark combo.

        RULES HARDCODED:
        - --no-cache is baked into generated.mk (every docker build has it)
        - Check disk BEFORE build, ABORT if <300GB free
        - Check disk AFTER build, ABORT if <300GB free
        - SKIP if runner image already exists (avoid redundant rebuilds)
        """
        fuzzbench_dir = self.cfg.fuzzbench_dir
        runner_image = (f"{self.cfg.docker_registry}/runners/"
                        f"{self.cfg.fuzzer}/{self.cfg.benchmark}")

        # SKIP if runner image already exists
        check = subprocess.run(
            ["docker", "image", "inspect", runner_image],
            capture_output=True, timeout=10,
        )
        if check.returncode == 0:
            print(f"  Runner image {runner_image} already exists. Skipping build.")
            _check_disk_or_abort("pre-build (skipped, images exist)")
            return

        target = f"build-{self.cfg.fuzzer}-{self.cfg.benchmark}"

        # CHECK DISK BEFORE BUILD
        free_gb = _check_disk_or_abort("before building " + target)
        print(f"  Disk: {free_gb}GB free. Building {target}...")

        cmd = ["make", "-j1", "-f", "docker/generated.mk", target]
        result = subprocess.run(
            cmd, cwd=str(fuzzbench_dir),
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            print(f"  Build {target} FAILED (rc={result.returncode})")
            # Show last few lines of error
            for line in result.stderr.split('\n')[-5:]:
                if line.strip():
                    print(f"    {line}")

        # CHECK DISK AFTER BUILD
        _check_disk_or_abort("after building " + target)

    def _delete_pair_images(self, fuzzer: str, benchmark: str) -> None:
        """Delete the 4 per-fuzzer×benchmark images after experiment.

        RULES:
        - ONLY delete gcr.io/fuzzbench/* images
        - ONLY these 4 specific images per pair
        - Keep base-image and benchmark image
        """
        images = [
            f"gcr.io/fuzzbench/builders/{fuzzer}/{benchmark}-intermediate",
            f"gcr.io/fuzzbench/builders/{fuzzer}/{benchmark}",
            f"gcr.io/fuzzbench/runners/{fuzzer}/{benchmark}-intermediate",
            f"gcr.io/fuzzbench/runners/{fuzzer}/{benchmark}",
        ]
        for img in images:
            # VALIDATE: must start with gcr.io/fuzzbench/
            assert img.startswith("gcr.io/fuzzbench/"), f"REFUSING to delete non-fuzzbench image: {img}"
            subprocess.run(["docker", "rmi", "-f", img],
                          capture_output=True, timeout=30)
        print(f"  [cleanup] Deleted 4 pair images for {fuzzer}×{benchmark}")

    def _delete_benchmark_image(self, benchmark: str) -> None:
        """Delete benchmark image ONLY after ALL fuzzers verified for this benchmark."""
        images = [
            f"gcr.io/fuzzbench/builders/benchmark/{benchmark}",
            f"gcr.io/fuzzbench/builders/coverage/{benchmark}",
            f"gcr.io/fuzzbench/builders/coverage/{benchmark}-intermediate",
        ]
        for img in images:
            assert img.startswith("gcr.io/fuzzbench/"), f"REFUSING to delete: {img}"
            subprocess.run(["docker", "rmi", "-f", img],
                          capture_output=True, timeout=30)
        print(f"  [cleanup] Deleted benchmark images for {benchmark}")

    def run(self) -> bool:
        """Execute the full parallel split experiment."""
        layout = self.layout

        print(f"\n{'='*70}")
        print(f"PARALLEL SPLIT EXPERIMENT: {self.cfg.experiment_name}")
        print(f"Benchmark: {self.cfg.benchmark}  Fuzzer: {self.cfg.fuzzer}")
        print(f"Plan: {self.plan.checkpoints_hours} (branching={self.plan.branching_factor})")
        print(f"Root trials: {self.num_trials}")
        print(f"CPU layout: {layout.total_cores} cores, "
              f"{layout.cores_per_branch} per branch "
              f"({layout.runners_per_branch}R + {layout.measurers_per_branch}M)")
        print(f"Max concurrent branches: {self.num_trials * layout.max_branches_per_trial}")
        print(f"Total cores used: {layout.total_used} / {layout.total_cores}")
        print(f"{'='*70}\n")

        # --- PRE-FLIGHT CHECKS ---

        # 1. DISK CHECK (ABORT if <300GB)
        free_gb = _check_disk_or_abort("before experiment start")
        print(f"Disk: {free_gb}GB free (minimum {MIN_FREE_DISK_GB}GB)")

        # 2. RAM check
        max_concurrent = self.num_trials * layout.max_branches_per_trial
        RAM_PER_BRANCH_GB = 5.0
        estimated_ram = max_concurrent * RAM_PER_BRANCH_GB
        try:
            import psutil
            total_ram = psutil.virtual_memory().total / (1024**3)
            avail_ram = psutil.virtual_memory().available / (1024**3)
            print(f"RAM: estimated {estimated_ram:.0f}GB needed, "
                  f"{avail_ram:.0f}GB available / {total_ram:.0f}GB total")
            if estimated_ram > total_ram * 0.85:
                print(f"ABORT: RAM {estimated_ram:.0f}GB > 85% of {total_ram:.0f}GB")
                return False
        except Exception as e:
            print(f"WARNING: RAM check failed: {e}")

        # 3. Kill ONLY our conflicting containers (filtered by experiment name)
        killed = _kill_only_our_containers(self.cfg.experiment_name)
        if killed:
            print(f"Killed {killed} old containers for {self.cfg.experiment_name}")
            time.sleep(3)

        # 4. Clean old filestore for THIS experiment name only
        import shutil
        exp_filestore = self.cfg.experiment_filestore / self.cfg.experiment_name
        if exp_filestore.exists():
            print(f"Cleaning old filestore: {exp_filestore}")
            shutil.rmtree(exp_filestore)
        exp_filestore.mkdir(parents=True, exist_ok=True)

        # 5. Pre-build Docker images (disk checked inside _prebuild_images)
        print("\nPre-building Docker images...")
        self._prebuild_images()
        print("Docker images ready.\n")

        # Track current branches per trial
        trial_branches: Dict[int, List[BranchRun]] = {}

        for stage_idx, stage in enumerate(self.stages):
            level = stage.level
            is_last = (stage_idx == len(self.stages) - 1)
            branches_at_level = self.plan.trials_at_level(level)

            print(f"\n{'─'*70}")
            print(f"LEVEL {level}: [{stage.start_hour}h – {stage.end_hour}h] "
                  f"({stage.duration_hours:.1f}h)")
            print(f"Branches per trial: {branches_at_level}, "
                  f"Total: {self.num_trials * branches_at_level}")
            print(f"{'─'*70}")

            all_branches: List[BranchRun] = []

            if level == 0:
                for rt in range(self.num_trials):
                    seed_dir = self._init_root_seeds(rt)
                    branch = self._make_branch(rt, 0, root_branch_id(), 0)
                    trial_branches[rt] = [branch]
                    all_branches.append(branch)
            else:
                for rt in range(self.num_trials):
                    trial_branches[rt] = self._harvest_and_branch_trial(
                        rt, level - 1, trial_branches[rt],
                        stage.branching_factor,
                    )
                    all_branches.extend(trial_branches[rt])

            # Launch ALL branches across ALL trials in parallel
            for branch in all_branches:
                # Each branch gets its own filestore (isolates SQLite .db)
                filestore = self.cfg.level_filestore(
                    branch.root_trial, branch.level, branch.branch_id)
                filestore.mkdir(parents=True, exist_ok=True)

                # Override runners/measurers from layout
                # Each branch = 1 trial. AFL/libfuzzer use 1 core per instance,
                # so runner_num_cpu_cores=1 and runners_cpus=1.
                # The CPU offset still reserves the block to avoid overlap.
                cfg_copy = SplitConfig(
                    experiment_name=self.cfg.experiment_name,
                    fuzzer=self.cfg.fuzzer,
                    benchmark=self.cfg.benchmark,
                    project_root=self.cfg.project_root,
                    fuzzbench_dir=self.cfg.fuzzbench_dir,
                    experiment_filestore=self.cfg.experiment_filestore,
                    report_filestore=self.cfg.report_filestore,
                    tmp_dir=self.cfg.tmp_dir,
                    runners_cpus=1,
                    measurers_cpus=layout.measurers_per_branch,
                    cpu_offset=branch.cpu_offset,
                    runner_num_cpu_cores=1,
                    concurrent_builds=self.cfg.concurrent_builds,
                    allow_uncommitted=self.cfg.allow_uncommitted,
                    snapshot_seconds=self.cfg.snapshot_seconds,
                    docker_registry=self.cfg.docker_registry,
                )

                config_path = _write_experiment_config(
                    cfg_copy, stage, branch.experiment_name, filestore,
                )
                proc = _run_branch(cfg_copy, branch, config_path)
                branch.process = proc

                log_path = self.cfg.work_dir / "logs" / f"{branch.experiment_name}.log"
                t = threading.Thread(
                    target=_pump_output,
                    args=(branch.experiment_name, proc.stdout, log_path),
                    daemon=True,
                )
                t.start()

            print(f"\nLaunched {len(all_branches)} branches:")
            for b in all_branches:
                print(f"  r{b.root_trial} {b.branch_id} → {b.experiment_name} "
                      f"(cores {b.cpu_offset}–{b.cpu_offset + layout.cores_per_branch - 1})")

            # Wait for ALL branches at this level
            ok = _wait_all(all_branches)
            if not ok:
                print(f"\n[FATAL] Level {level} FAILED.")
                return False

            print(f"\nLevel {level} COMPLETED ({len(all_branches)} branches)")

            # CHECK DISK after each level
            _check_disk_or_abort(f"after level {level}")

        # CLEANUP: delete per-pair images after experiment completes
        self._delete_pair_images(self.cfg.fuzzer, self.cfg.benchmark)

        print(f"\n{'='*70}")
        print(f"EXPERIMENT {self.cfg.experiment_name} COMPLETED SUCCESSFULLY")
        print(f"Results: {self.cfg.experiment_filestore}")
        print(f"{'='*70}")
        return True


def main():
    import argparse

    p = argparse.ArgumentParser(description="Parallel split experiment runner")

    p.add_argument("--experiment-name", required=True)
    p.add_argument("--fuzzer", required=True)
    p.add_argument("--benchmark", required=True)

    p.add_argument("--mode", choices=["fixed", "offline", "nosplit", "online"], default="fixed")
    p.add_argument("--split-times", type=float, nargs="*")
    p.add_argument("--sparsity-csv", default=None)
    p.add_argument("--total-hours", type=float, default=23.0)
    p.add_argument("--branching-factor", type=int, default=2)
    p.add_argument("--max-splits", type=int, default=3,
                    help="Max splits per branch (online mode only)")
    p.add_argument("--poll-interval", type=int, default=60,
                    help="Seconds between sparsity polls (online mode only)")

    p.add_argument("--num-trials", type=int, default=5, help="N parallel root trials")
    p.add_argument("--total-cores", type=int, default=188, help="Total CPU cores available")
    p.add_argument("--cpu-offset", type=int, default=0, help="Start CPU allocation from this core")
    p.add_argument("--min-measurers", type=int, default=1)

    p.add_argument("--concurrent-builds", type=int, default=5)
    p.add_argument("--snapshot-seconds", type=int, default=360)
    p.add_argument("--custom-seeds", default=None)
    p.add_argument("--experiment-filestore", default=None)
    p.add_argument("--report-filestore", default=None)

    args = p.parse_args()

    from .config import PROJECT_ROOT

    # Online mode delegates to OnlineSplitOrchestrator
    if args.mode == "online":
        from .online_orchestrator import OnlineSplitOrchestrator, compute_online_cpu_layout

        layout = compute_online_cpu_layout(
            total_cores=args.total_cores,
            num_trials=args.num_trials,
            branching_factor=args.branching_factor,
            max_splits=args.max_splits,
            min_measurers=args.min_measurers,
        )

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

        orchestrator = OnlineSplitOrchestrator(
            cfg=cfg,
            num_trials=args.num_trials,
            layout=layout,
            total_hours=args.total_hours,
            max_splits=args.max_splits,
            branching_factor=args.branching_factor,
            poll_interval_seconds=args.poll_interval,
        )

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
        return  # never reached, but for clarity

    # Build plan (offline modes)
    if args.mode == "nosplit":
        plan = no_split_plan(args.total_hours)
    elif args.mode == "fixed":
        if not args.split_times:
            p.error("--split-times required for fixed mode")
        plan = fixed_split_plan(args.split_times, args.total_hours, args.branching_factor)
    elif args.mode == "offline":
        if not args.sparsity_csv:
            p.error("--sparsity-csv required for offline mode")
        plan = plan_from_sparsity_csv(
            Path(args.sparsity_csv), args.fuzzer, args.benchmark,
            args.total_hours, args.branching_factor,
        )

    # Compute CPU layout
    layout = compute_cpu_layout(
        total_cores=args.total_cores,
        num_trials=args.num_trials,
        plan=plan,
        min_measurers=args.min_measurers,
        base_cpu_offset=args.cpu_offset,
    )

    # Build config
    cfg = SplitConfig(
        experiment_name=args.experiment_name,
        fuzzer=args.fuzzer,
        benchmark=args.benchmark,
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

    runner = ParallelSplitRunner(cfg, plan, args.num_trials, layout)

    # Handle signals
    def _sigterm(sig, frame):
        print("\n[SIGTERM] Shutting down...")
        sys.exit(1)
    signal.signal(signal.SIGTERM, _sigterm)

    try:
        ok = runner.run()
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")
        sys.exit(130)


if __name__ == "__main__":
    main()

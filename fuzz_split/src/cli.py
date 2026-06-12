"""Main CLI for the split-fuzzing framework.

Usage examples:

  # Compute sparsity split times from baseline data
  python -m src.cli compute-sparsity --input data/baseline.csv --output data/

  # Run offline split experiment (one benchmark)
  python -m src.cli run --mode offline \
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \
    --sparsity-csv data/sparsity_split_times_summary.csv \
    --experiment-name my-split-exp \
    --cpu-offset 0 --runners-cpus 4 --measurers-cpus 2

  # Run with fixed split times (for testing)
  python -m src.cli run --mode fixed \
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \
    --split-times 8.0 14.0 18.0 \
    --experiment-name test-split \
    --total-hours 23 --branching-factor 2

  # Run no-split baseline
  python -m src.cli run --mode nosplit \
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \
    --experiment-name baseline-exp

  # Generate report from completed experiment
  python -m src.cli report \
    --experiment-name my-split-exp \
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \
    --output results/report.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .config import SplitConfig, PROJECT_ROOT
from .split_plan import SplitPlan, fixed_split_plan, no_split_plan, plan_from_sparsity_csv
from .orchestrator import run_experiment
from .report import generate_report_csv


def cmd_run(args: argparse.Namespace) -> int:
    """Run a split experiment."""

    # Build split plan
    if args.mode == "nosplit":
        plan = no_split_plan(args.total_hours)
    elif args.mode == "fixed":
        if not args.split_times:
            print("ERROR: --split-times required for fixed mode", file=sys.stderr)
            return 1
        plan = fixed_split_plan(args.split_times, args.total_hours, args.branching_factor)
    elif args.mode == "offline":
        if not args.sparsity_csv:
            print("ERROR: --sparsity-csv required for offline mode", file=sys.stderr)
            return 1
        plan = plan_from_sparsity_csv(
            Path(args.sparsity_csv), args.fuzzer, args.benchmark,
            args.total_hours, args.branching_factor,
        )
    elif args.mode == "online":
        # Online mode: start with no-split plan; orchestrator will detect splits dynamically
        plan = no_split_plan(args.total_hours)
        # TODO: integrate online sparsity tracker into orchestrator
        print("WARNING: online mode not fully implemented yet. Running as no-split.")
    else:
        print(f"ERROR: unknown mode '{args.mode}'", file=sys.stderr)
        return 1

    print(f"Plan: checkpoints={plan.checkpoints_hours}, branching={plan.branching_factor}")
    print(f"Stages: {len(plan.stages())}, Total trials at final level: {plan.trials_at_level(plan.num_levels - 1)}")

    # Build config
    cfg = SplitConfig(
        experiment_name=args.experiment_name,
        fuzzer=args.fuzzer,
        benchmark=args.benchmark,
        experiment_filestore=Path(args.experiment_filestore),
        report_filestore=Path(args.report_filestore),
        tmp_dir=Path(args.tmp_dir) if args.tmp_dir else PROJECT_ROOT / "tmp",
        mode=args.mode,
        branching_factor=args.branching_factor,
        total_duration_hours=args.total_hours,
        runners_cpus=args.runners_cpus,
        measurers_cpus=args.measurers_cpus,
        cpu_offset=args.cpu_offset,
        runner_num_cpu_cores=args.runner_num_cpu_cores,
        concurrent_builds=args.concurrent_builds,
        allow_uncommitted=not args.no_allow_uncommitted,
        snapshot_seconds=args.snapshot_seconds,
        num_initial_trials=args.num_trials,
    )

    if args.custom_seeds:
        cfg.custom_seed_corpus_dir = Path(args.custom_seeds)

    # Run
    root_trials = list(range(args.num_trials))
    ok = run_experiment(cfg, plan, root_trials)
    return 0 if ok else 1


def cmd_compute_sparsity(args: argparse.Namespace) -> int:
    """Compute sparsity split times from baseline CSV."""
    import pandas as pd
    from .sparsity import compute_split_times

    df = pd.read_csv(args.input)
    _, summary = compute_split_times(
        df,
        fuzzers=args.fuzzers,
        benchmarks=args.benchmarks,
        max_time_seconds=args.max_time,
    )

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sparsity_split_times_summary.csv"
    summary.to_csv(out_path, index=False)
    print(f"Wrote {out_path} ({len(summary)} rows)")
    return 0


def cmd_parallel(args: argparse.Namespace) -> int:
    """Run N parallel root trials with proper CPU allocation."""
    from .parallel_runner import ParallelSplitRunner, compute_cpu_layout

    if args.mode == "nosplit":
        plan = no_split_plan(args.total_hours)
    elif args.mode == "fixed":
        if not args.split_times:
            print("ERROR: --split-times required for fixed mode", file=sys.stderr)
            return 1
        plan = fixed_split_plan(args.split_times, args.total_hours, args.branching_factor)
    elif args.mode == "offline":
        if not args.sparsity_csv:
            print("ERROR: --sparsity-csv required for offline mode", file=sys.stderr)
            return 1
        plan = plan_from_sparsity_csv(
            Path(args.sparsity_csv), args.fuzzer, args.benchmark,
            args.total_hours, args.branching_factor,
        )
    else:
        print(f"ERROR: unknown mode '{args.mode}'", file=sys.stderr)
        return 1

    cpu_offset = getattr(args, 'cpu_offset', 0) or 0
    layout = compute_cpu_layout(args.total_cores, args.num_trials, plan, args.min_measurers, cpu_offset)

    cfg = SplitConfig(
        experiment_name=args.experiment_name,
        fuzzer=args.fuzzer,
        benchmark=args.benchmark,
        experiment_filestore=Path(args.experiment_filestore),
        report_filestore=Path(args.report_filestore),
        tmp_dir=Path(args.tmp_dir) if args.tmp_dir else PROJECT_ROOT / "tmp",
        concurrent_builds=args.concurrent_builds,
        snapshot_seconds=args.snapshot_seconds,
        branching_factor=args.branching_factor,
        total_duration_hours=args.total_hours,
        allow_uncommitted=True,
    )
    if args.custom_seeds:
        cfg.custom_seed_corpus_dir = Path(args.custom_seeds)

    runner = ParallelSplitRunner(cfg, plan, args.num_trials, layout)
    ok = runner.run()
    return 0 if ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    """Generate report from completed experiment."""
    from .split_plan import fixed_split_plan, no_split_plan

    # Reconstruct plan from args
    if args.split_times:
        plan = fixed_split_plan(args.split_times, args.total_hours, args.branching_factor)
    else:
        plan = no_split_plan(args.total_hours)

    root_trials = list(range(args.num_trials))

    generate_report_csv(
        experiment_name=args.experiment_name,
        benchmark=args.benchmark,
        fuzzer=args.fuzzer,
        plan=plan,
        experiment_filestore=Path(args.experiment_filestore),
        root_trials=root_trials,
        output_path=Path(args.output),
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split-fuzzing framework based on FuzzBench",
        prog="python -m src.cli",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = sub.add_parser("run", help="Run a split experiment")
    p_run.add_argument("--mode", choices=["offline", "online", "fixed", "nosplit"], default="fixed")
    p_run.add_argument("--experiment-name", required=True)
    p_run.add_argument("--fuzzer", required=True)
    p_run.add_argument("--benchmark", required=True)
    p_run.add_argument("--split-times", type=float, nargs="*", help="Fixed split times (hours)")
    p_run.add_argument("--sparsity-csv", help="Path to sparsity summary CSV (offline mode)")
    p_run.add_argument("--total-hours", type=float, default=23.0)
    p_run.add_argument("--branching-factor", type=int, default=2)
    p_run.add_argument("--num-trials", type=int, default=1, help="Number of independent root trials")
    p_run.add_argument("--cpu-offset", type=int, default=0)
    p_run.add_argument("--runners-cpus", type=int, default=20)
    p_run.add_argument("--measurers-cpus", type=int, default=8)
    p_run.add_argument("--runner-num-cpu-cores", type=int, default=1)
    p_run.add_argument("--concurrent-builds", type=int, default=5)
    p_run.add_argument("--snapshot-seconds", type=int, default=360)
    p_run.add_argument("--experiment-filestore", default=str(PROJECT_ROOT / "results" / "experiment-data"))
    p_run.add_argument("--report-filestore", default=str(PROJECT_ROOT / "results" / "report-data"))
    p_run.add_argument("--tmp-dir", default=None)
    p_run.add_argument("--custom-seeds", default=None, help="Custom seed corpus directory")
    p_run.add_argument("--no-allow-uncommitted", action="store_true")
    p_run.set_defaults(func=cmd_run)

    # --- parallel (the main way to run experiments) ---
    p_par = sub.add_parser("parallel", help="Run N parallel root trials with CPU management")
    p_par.add_argument("--mode", choices=["offline", "fixed", "nosplit"], default="fixed")
    p_par.add_argument("--experiment-name", required=True)
    p_par.add_argument("--fuzzer", required=True)
    p_par.add_argument("--benchmark", required=True)
    p_par.add_argument("--split-times", type=float, nargs="*")
    p_par.add_argument("--sparsity-csv", default=None)
    p_par.add_argument("--total-hours", type=float, default=23.0)
    p_par.add_argument("--branching-factor", type=int, default=2)
    p_par.add_argument("--num-trials", type=int, default=5, help="N parallel root trials")
    p_par.add_argument("--total-cores", type=int, default=188, help="Total CPU cores")
    p_par.add_argument("--cpu-offset", type=int, default=0, help="Start CPU allocation from this core")
    p_par.add_argument("--min-measurers", type=int, default=1)
    p_par.add_argument("--concurrent-builds", type=int, default=5)
    p_par.add_argument("--snapshot-seconds", type=int, default=360)
    p_par.add_argument("--experiment-filestore", default=str(PROJECT_ROOT / "results" / "experiment-data"))
    p_par.add_argument("--report-filestore", default=str(PROJECT_ROOT / "results" / "report-data"))
    p_par.add_argument("--tmp-dir", default=None)
    p_par.add_argument("--custom-seeds", default=None)
    p_par.set_defaults(func=cmd_parallel)

    # --- compute-sparsity ---
    p_sp = sub.add_parser("compute-sparsity", help="Compute split times from baseline CSV")
    p_sp.add_argument("--input", required=True, help="Baseline FuzzBench CSV")
    p_sp.add_argument("--output", default="data", help="Output directory")
    p_sp.add_argument("--fuzzers", nargs="*", default=None)
    p_sp.add_argument("--benchmarks", nargs="*", default=None)
    p_sp.add_argument("--max-time", type=int, default=None)
    p_sp.set_defaults(func=cmd_compute_sparsity)

    # --- report ---
    p_rep = sub.add_parser("report", help="Generate report from completed experiment")
    p_rep.add_argument("--experiment-name", required=True)
    p_rep.add_argument("--fuzzer", required=True)
    p_rep.add_argument("--benchmark", required=True)
    p_rep.add_argument("--split-times", type=float, nargs="*")
    p_rep.add_argument("--total-hours", type=float, default=23.0)
    p_rep.add_argument("--branching-factor", type=int, default=2)
    p_rep.add_argument("--num-trials", type=int, default=1)
    p_rep.add_argument("--experiment-filestore", default=str(PROJECT_ROOT / "results" / "experiment-data"))
    p_rep.add_argument("--output", required=True)
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv or sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

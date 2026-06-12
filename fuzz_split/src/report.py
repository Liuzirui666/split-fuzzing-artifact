"""Report generation: merge split experiment data into FuzzBench-compatible CSV.

The output CSV matches the structure of data_nosplit_24h.csv:
  benchmark, fuzzer, trial_id, time, edges_covered, bugs_covered, crash_key, ...

Additional split metadata columns:
  split_level, branch_id, root_trial, k_t (active paths), weight (1/k_t)

The key insight: each branch at each level is a "virtual trial" in the
final report. Times are shifted so level-0 starts at t=0, level-1 continues
from the split point, etc. This creates a continuous timeline per branch.
"""
from __future__ import annotations

import glob as glob_mod
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from .split_plan import SplitPlan
from .seed_manager import all_branch_ids_at_level


def _load_experiment_csv(
    experiment_filestore: Path,
    experiment_name: str,
) -> Optional[pd.DataFrame]:
    """Load a FuzzBench experiment's measurement data."""
    # Try multiple possible locations
    candidates = [
        experiment_filestore / experiment_name / "report" / "data.csv.gz",
        experiment_filestore / experiment_name / "report" / "data.csv",
    ]

    for path in candidates:
        if path.exists():
            try:
                return pd.read_csv(path)
            except Exception as e:
                print(f"Warning: failed to read {path}: {e}")

    # Try to find experiment data in the experiment-folders structure
    # and read from the sqlite database directly
    return None


def _load_from_db(
    experiment_filestore: Path,
    experiment_name: str,
) -> Optional[pd.DataFrame]:
    """Load data directly from FuzzBench's SQLite database."""
    db_path = experiment_filestore / experiment_name / "report" / "data.db"
    if not db_path.exists():
        return None

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        df = pd.read_sql_query("SELECT * FROM experiment_data", conn)
        conn.close()
        return df
    except Exception as e:
        print(f"Warning: failed to read {db_path}: {e}")
        return None


def merge_split_data(
    experiment_name: str,
    benchmark: str,
    fuzzer: str,
    plan: SplitPlan,
    experiment_filestore: Path,
    root_trials: List[int],
) -> pd.DataFrame:
    """Merge data from all branches across all levels into a single DataFrame.

    Each branch becomes a separate "trial" with continuous time accounting.
    """
    stages = plan.stages()
    all_rows = []

    for root_trial in root_trials:
        filestore = experiment_filestore / f"r{root_trial}"

        for level in range(plan.num_levels):
            stage = stages[level]
            branch_ids = all_branch_ids_at_level(level, plan.branching_factor)
            k_t = len(branch_ids)  # number of active paths at this level
            weight = 1.0 / k_t

            for branch_id in branch_ids:
                exp_name = f"{experiment_name}-r{root_trial}-L{level}-{branch_id}"

                df = _load_experiment_csv(filestore, exp_name)
                if df is None:
                    df = _load_from_db(filestore, exp_name)
                if df is None:
                    print(f"Warning: no data for {exp_name}")
                    continue

                # Filter to our benchmark/fuzzer
                mask = pd.Series([True] * len(df))
                if "benchmark" in df.columns:
                    mask &= df["benchmark"] == benchmark
                if "fuzzer" in df.columns:
                    mask &= df["fuzzer"] == fuzzer
                df = df[mask].copy()

                if df.empty:
                    continue

                # Shift time to global timeline
                # Level 0 starts at checkpoint[0], level 1 at checkpoint[1], etc.
                time_offset_seconds = stage.start_hour * 3600
                if "time" in df.columns:
                    df["time"] = df["time"].astype(float) + time_offset_seconds

                # Add split metadata
                df["split_level"] = level
                df["branch_id"] = branch_id
                df["root_trial"] = root_trial
                df["k_t"] = k_t
                df["weight"] = weight

                # Create unique trial_id across all branches
                df["trial_id"] = f"r{root_trial}_{branch_id}"

                all_rows.append(df)

    if not all_rows:
        return pd.DataFrame()

    merged = pd.concat(all_rows, ignore_index=True)
    merged = merged.sort_values(["root_trial", "branch_id", "time"])
    return merged


def generate_report_csv(
    experiment_name: str,
    benchmark: str,
    fuzzer: str,
    plan: SplitPlan,
    experiment_filestore: Path,
    root_trials: List[int],
    output_path: Path,
) -> pd.DataFrame:
    """Generate the final report CSV (compatible with FuzzBench format + split metadata)."""
    df = merge_split_data(
        experiment_name, benchmark, fuzzer, plan,
        experiment_filestore, root_trials,
    )

    if df.empty:
        print(f"Warning: no data to report for {experiment_name}")
        return df

    # Ensure standard columns exist
    for col in ["edges_covered", "bugs_covered", "crash_key", "fuzzer_stats"]:
        if col not in df.columns:
            df[col] = "" if col in ("crash_key", "fuzzer_stats") else 0

    # Select and order columns
    standard_cols = [
        "experiment", "fuzzer", "benchmark", "trial_id", "time",
        "edges_covered", "bugs_covered", "crash_key", "fuzzer_stats",
    ]
    split_cols = ["split_level", "branch_id", "root_trial", "k_t", "weight"]

    # Add experiment name if missing
    if "experiment" not in df.columns:
        df["experiment"] = experiment_name

    # Keep only columns that exist
    out_cols = [c for c in standard_cols + split_cols if c in df.columns]
    df = df[out_cols]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Report written to {output_path} ({len(df)} rows)")
    return df

"""Weighted estimators for split experiments (paper Section 4 + Theorem 2).

Under splitting, the bug detection rate estimator is:
  P_hat(T) = (1/T) * sum_t (1/k_t) * sum_{s=1}^{k_t} Y_{s,t}

where k_t = number of active paths at time t.

The sample variance estimator accounts for CPU cost:
  CPU(T) = sum_m sum_t k_t^m
  sigma_hat^2(T) = CPU(T) * S_hat(T)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class SplitEstimates:
    """Results of weighted estimation for one (benchmark, fuzzer) pair."""
    benchmark: str
    fuzzer: str

    # Bug detection rate (weighted)
    bdr_weighted: float          # P_hat(T) weighted by 1/k_t
    bdr_unweighted: float        # Simple average across all branches

    # Bugs found
    total_bugs: int              # Total unique bugs across all branches
    bugs_per_level: Dict[int, float]  # Average bugs by level

    # Variance
    variance_weighted: float     # Effort-normalized variance sigma_hat^2
    variance_unweighted: float   # Standard MC variance

    # CPU accounting
    total_cpu_hours: float       # sum of k_t * delta_t
    num_root_trials: int
    num_leaf_branches: int


def compute_weighted_estimates(
    df: pd.DataFrame,
    total_hours: float = 23.0,
    snapshot_seconds: int = 360,
) -> SplitEstimates:
    """Compute weighted estimators from merged split data.

    Args:
        df: Merged DataFrame with columns: benchmark, fuzzer, trial_id, time,
            bugs_covered, split_level, branch_id, root_trial, k_t, weight
        total_hours: Total experiment duration
        snapshot_seconds: Measurement interval
    """
    if df.empty:
        raise ValueError("Empty DataFrame")

    benchmark = df["benchmark"].iloc[0]
    fuzzer = df["fuzzer"].iloc[0]

    # Time grid (seconds)
    T_seconds = total_hours * 3600
    delta = snapshot_seconds
    time_grid = np.arange(0, T_seconds + delta, delta)

    root_trials = sorted(df["root_trial"].unique())
    num_roots = len(root_trials)

    # Per-root-trial weighted BDR
    bdr_per_root = []
    cpu_per_root = []

    for rt in root_trials:
        rt_df = df[df["root_trial"] == rt]
        branches = rt_df["trial_id"].unique()

        # For each time point, compute weighted bugs
        weighted_sum = 0.0
        cpu_sum = 0.0
        count = 0

        for t in time_grid:
            if t == 0:
                continue

            # Find all branches active at time t and their measurements
            # closest to (but not after) time t
            branch_bugs = []
            k_t = 0

            for bid in branches:
                b_df = rt_df[rt_df["trial_id"] == bid]
                b_before_t = b_df[b_df["time"] <= t]
                if b_before_t.empty:
                    continue
                k_t += 1
                latest = b_before_t.iloc[-1]
                branch_bugs.append(float(latest["bugs_covered"]))

            if k_t > 0:
                # Weighted contribution: (1/k_t) * sum(Y_s)
                weighted_bugs = sum(branch_bugs) / k_t
                weighted_sum += weighted_bugs
                cpu_sum += k_t * (delta / 3600.0)
                count += 1

        if count > 0:
            bdr = weighted_sum / (T_seconds / delta)  # Normalize by total time steps
            bdr_per_root.append(bdr)
            cpu_per_root.append(cpu_sum)

    # Aggregate across root trials
    bdr_weighted = float(np.mean(bdr_per_root)) if bdr_per_root else 0.0
    total_cpu = sum(cpu_per_root)

    # Variance
    if len(bdr_per_root) > 1:
        variance_weighted = float(np.var(bdr_per_root, ddof=1))
        sigma_hat_sq = total_cpu * variance_weighted  # Effort-normalized
    else:
        variance_weighted = 0.0
        sigma_hat_sq = 0.0

    # Unweighted (simple) estimates for comparison
    # Group by trial_id, get max bugs_covered per trial
    trial_max_bugs = df.groupby("trial_id")["bugs_covered"].max()
    bdr_unweighted = float(trial_max_bugs.mean()) if len(trial_max_bugs) > 0 else 0.0
    var_unweighted = float(trial_max_bugs.var(ddof=1)) if len(trial_max_bugs) > 1 else 0.0

    # Bugs per level
    bugs_per_level = {}
    for level in df["split_level"].unique():
        level_df = df[df["split_level"] == level]
        level_max = level_df.groupby("trial_id")["bugs_covered"].max()
        bugs_per_level[int(level)] = float(level_max.mean())

    # Total unique bugs (by crash_key if available)
    if "crash_key" in df.columns and df["crash_key"].notna().any():
        total_bugs = int(df[df["crash_key"].notna() & (df["crash_key"] != "")]["crash_key"].nunique())
    else:
        total_bugs = int(trial_max_bugs.max()) if len(trial_max_bugs) > 0 else 0

    # Count leaf branches
    max_level = int(df["split_level"].max())
    leaf_branches = len(df[df["split_level"] == max_level]["trial_id"].unique())

    return SplitEstimates(
        benchmark=benchmark,
        fuzzer=fuzzer,
        bdr_weighted=bdr_weighted,
        bdr_unweighted=bdr_unweighted,
        total_bugs=total_bugs,
        bugs_per_level=bugs_per_level,
        variance_weighted=sigma_hat_sq,
        variance_unweighted=var_unweighted,
        total_cpu_hours=total_cpu,
        num_root_trials=num_roots,
        num_leaf_branches=leaf_branches,
    )


def compare_split_vs_nosplit(
    split_df: pd.DataFrame,
    nosplit_df: pd.DataFrame,
    total_hours: float = 23.0,
) -> pd.DataFrame:
    """Compare split vs no-split estimates side by side."""
    rows = []

    for (bm, fz), grp in split_df.groupby(["benchmark", "fuzzer"]):
        split_est = compute_weighted_estimates(grp, total_hours)

        # No-split baseline
        ns = nosplit_df[
            (nosplit_df["benchmark"] == bm) &
            (nosplit_df["fuzzer"] == fz)
        ]
        if ns.empty:
            ns_bugs = 0.0
            ns_var = 0.0
        else:
            trial_bugs = ns.groupby("trial_id")["bugs_covered"].max()
            ns_bugs = float(trial_bugs.mean())
            ns_var = float(trial_bugs.var(ddof=1)) if len(trial_bugs) > 1 else 0.0

        rows.append({
            "benchmark": bm,
            "fuzzer": fz,
            "split_bdr_weighted": round(split_est.bdr_weighted, 4),
            "split_bugs_total": split_est.total_bugs,
            "split_variance": round(split_est.variance_weighted, 6),
            "split_cpu_hours": round(split_est.total_cpu_hours, 2),
            "nosplit_bugs_avg": round(ns_bugs, 4),
            "nosplit_variance": round(ns_var, 6),
            "bug_improvement": round(split_est.bdr_weighted - ns_bugs, 4),
            "variance_ratio": round(
                split_est.variance_weighted / max(ns_var, 1e-12), 4
            ) if ns_var > 0 else float("inf"),
        })

    return pd.DataFrame(rows)

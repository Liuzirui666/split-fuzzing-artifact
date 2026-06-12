#!/usr/bin/env python3
"""Magma survival analysis over split + nosplit experiments.

Reads the long-format CSV produced by magma_report.py and computes:
  - Per (trial, bug): first-trigger and first-reached time (with global time
    reconstructed across split branches)
  - Per (fuzzer, target, mode, bug): list of N trial times (with censoring)
  - Kaplan-Meier survival and restricted mean survival time (RMST) per bug
  - Summary table: for each bug, mean time-to-trigger split vs nosplit

Splitting aggregation rule for a trial:
  trigger_time = min { global_time_sec : triggered > 0 for any branch in the trial }
If no branch triggered the bug, the trial is censored at total_duration.

Output:
  - summary.csv: per (fuzzer, target, mode, bug) RMST reached/triggered, CI, N triggered
  - trial_events.csv: per (fuzzer, target, mode, trial, bug) time-to-reach, time-to-trigger
  - split_vs_nosplit.csv: for each bug, RMST ratio split/nosplit (< 1 means split is faster)
"""
from __future__ import annotations

import argparse
import sys
from math import sqrt
from pathlib import Path

import pandas as pd


def compute_trial_events(df: pd.DataFrame, total_duration_sec: int) -> pd.DataFrame:
    """For each (experiment, fuzzer, target, mode, trial_id, bug_id), compute
    time to first reach and first trigger, aggregated across all branches."""
    # Take earliest global_time where the metric is positive, per trial x bug.
    df = df.copy()
    df["reached"] = df["reached"].astype(int)
    df["triggered"] = df["triggered"].astype(int)

    group_cols = ["experiment", "fuzzer", "target", "mode", "trial_id", "bug_id"]

    reached_df = (
        df[df["reached"] > 0]
        .groupby(group_cols)["global_time_sec"].min()
        .rename("time_reached_sec")
        .reset_index()
    )
    triggered_df = (
        df[df["triggered"] > 0]
        .groupby(group_cols)["global_time_sec"].min()
        .rename("time_triggered_sec")
        .reset_index()
    )

    # Full set of (trial, bug) present in the data (also bugs only observed
    # without triggering): use the entire unique combos from df.
    full = df[group_cols].drop_duplicates()

    out = full.merge(reached_df, on=group_cols, how="left")
    out = out.merge(triggered_df, on=group_cols, how="left")

    # Censor at total_duration_sec if NaN.
    out["censored_reached"] = out["time_reached_sec"].isna()
    out["censored_triggered"] = out["time_triggered_sec"].isna()
    out["time_reached_sec"] = out["time_reached_sec"].fillna(total_duration_sec).astype(int)
    out["time_triggered_sec"] = out["time_triggered_sec"].fillna(total_duration_sec).astype(int)

    return out


def km_rmst(times, events, horizon) -> tuple[float, float, int]:
    """Kaplan-Meier restricted mean survival time (RMST) with 95% CI half-width.

    `times`: observed event or censoring times (numpy-like).
    `events`: 1 if event (trigger/reach), 0 if censored.
    Returns: (rmst_seconds, ci_half_width_seconds, n_events).
    """
    try:
        import numpy as np
    except ImportError:
        return (float("nan"), float("nan"), int(sum(events)))

    t = sorted(zip(times, events))
    T = [x[0] for x in t]
    E = [x[1] for x in t]
    n = len(T)
    if n == 0:
        return (horizon, float("nan"), 0)

    # Step through unique event times, update survival
    at_risk = n
    S = 1.0  # survival prob
    variance_term = 0.0  # Greenwood's formula accumulator
    last_t = 0
    rmst = 0.0
    var_rmst = 0.0
    unique_times = sorted(set(T))
    idx = 0
    for ut in unique_times:
        # contribute area under S from last_t to ut
        rmst += S * (min(ut, horizon) - last_t)
        # compute d_i (events at ut), n_i (at risk just before ut)
        d_i = sum(1 for tt, ee in zip(T, E) if tt == ut and ee == 1)
        c_i = sum(1 for tt, ee in zip(T, E) if tt == ut and ee == 0)
        n_i = at_risk
        if n_i > 0 and d_i > 0:
            S *= (1 - d_i / n_i)
            if n_i - d_i > 0:
                variance_term += d_i / (n_i * (n_i - d_i))
        at_risk -= (d_i + c_i)
        last_t = ut
        if ut >= horizon:
            break
    if last_t < horizon:
        rmst += S * (horizon - last_t)

    # rough CI via Greenwood-like formula at horizon
    var_at_end = (S ** 2) * variance_term
    ci_half = 1.96 * sqrt(max(0.0, var_at_end)) * horizon

    return (rmst, ci_half, int(sum(E)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="long-format CSV from magma_report.py")
    p.add_argument("--total-hours", type=float, required=True,
                   help="campaign duration (horizon for survival)")
    p.add_argument("--outdir", required=True, help="output directory")
    args = p.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    total_sec = int(args.total_hours * 3600)
    df = pd.read_csv(args.input)
    print(f"Loaded {len(df):,} rows from {args.input}")

    events = compute_trial_events(df, total_sec)
    events_path = outdir / "trial_events.csv"
    events.to_csv(events_path, index=False)
    print(f"Wrote {len(events):,} trial-events → {events_path}")

    # Per (fuzzer, target, mode, bug) survival summary
    summary_rows = []
    g_cols = ["fuzzer", "target", "mode", "bug_id"]
    for key, grp in events.groupby(g_cols):
        fuzzer, target, mode, bug = key
        n_trials = grp["trial_id"].nunique()
        T_r = grp["time_reached_sec"].to_list()
        E_r = (~grp["censored_reached"]).astype(int).to_list()
        T_t = grp["time_triggered_sec"].to_list()
        E_t = (~grp["censored_triggered"]).astype(int).to_list()

        rmst_r, ci_r, n_r_ev = km_rmst(T_r, E_r, total_sec)
        rmst_t, ci_t, n_t_ev = km_rmst(T_t, E_t, total_sec)

        summary_rows.append({
            "fuzzer": fuzzer, "target": target, "mode": mode, "bug_id": bug,
            "n_trials": n_trials,
            "n_trials_reached": n_r_ev,
            "n_trials_triggered": n_t_ev,
            "rmst_reached_sec": rmst_r, "ci_reached_sec": ci_r,
            "rmst_triggered_sec": rmst_t, "ci_triggered_sec": ci_t,
        })

    summary = pd.DataFrame(summary_rows)
    summary_path = outdir / "summary.csv"
    summary.to_csv(summary_path, index=False)
    print(f"Wrote {len(summary):,} summary rows → {summary_path}")

    # Split vs nosplit comparison per (fuzzer, target, bug)
    wide = summary.pivot_table(
        index=["fuzzer", "target", "bug_id"], columns="mode",
        values=["rmst_triggered_sec", "n_trials_triggered"],
        aggfunc="first",
    )
    # Flatten columns
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.reset_index()
    if "rmst_triggered_sec_split" in wide.columns and "rmst_triggered_sec_nosplit" in wide.columns:
        wide["rmst_ratio_split_over_nosplit"] = (
            wide["rmst_triggered_sec_split"] / wide["rmst_triggered_sec_nosplit"]
        )
    split_path = outdir / "split_vs_nosplit.csv"
    wide.to_csv(split_path, index=False)
    print(f"Wrote {len(wide):,} split-vs-nosplit rows → {split_path}")

    # Print a quick summary to stdout
    filter_cols = [c for c in ("n_trials_triggered_split", "n_trials_triggered_nosplit")
                   if c in wide.columns]
    if filter_cols:
        mask = False
        for c in filter_cols:
            mask = mask | (wide[c].fillna(0) > 0)
        triggered_any = wide[mask]
        print(f"\nBugs triggered in at least one mode: {len(triggered_any)}")
        if len(triggered_any) > 0:
            print(triggered_any.to_string(index=False))


if __name__ == "__main__":
    main()

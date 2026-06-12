#!/usr/bin/env python3
"""Comprehensive figures and CSV tables for Magma + splitting experiments.

Covers all three Magma metrics (Reached, Triggered, Detected) on both time and
execs axes, plus additional metrics (raw crash counts, CPU-hours, bitmap
coverage, exec rates). Uses paper's weighted 1/k_t estimator for BDR, BDR
rate, and variance.

Output layout:
  figures/
    cumulative/{metric}_{axis}/<fuzzer>_<target>.png
    bdr_rate/{metric}_{axis}/<fuzzer>_<target>.png
    variance/{metric}_{axis}/<fuzzer>_<target>.png
    survival/{metric}/<fuzzer>_<target>_<bug>.png
    coverage/<fuzzer>_<target>.png
    crash_count/<fuzzer>_<target>.png
    cpu_effort/cpu_hours_per_campaign.png
    splits_histogram.png
    tables/
      summary_per_bug.csv           — per (fuzzer, bug, mode)
      weighted_cumulative_time.csv  — long format, triggered metric
      weighted_cumulative_execs.csv — long format, triggered metric
      weighted_per_bug_time.csv     — per-bug breakdown
      cpu_effort.csv                — per (fuzzer, target, mode, trial)
      exec_rates.csv                — per (fuzzer, target, mode, trial)
  where {metric} ∈ {reached, triggered, detected}
        {axis}   ∈ {time, execs}
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from math import sqrt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


MODE_STYLE = {
    "split":   {"ls": "-",  "color": "#2a9d8f"},
    "online":  {"ls": "-",  "color": "#e76f51"},
    "nosplit": {"ls": "--", "color": "#4a4a4a"},
}


# ============================================================
# KM utility (for survival curves)
# ============================================================

def kaplan_meier(times, events, horizon):
    t_arr = np.array(times, dtype=float)
    e_arr = np.array(events, dtype=int)
    order = np.argsort(t_arr)
    t_arr, e_arr = t_arr[order], e_arr[order]
    n = len(t_arr)
    if n == 0:
        return np.array([0, horizon]), np.array([1.0, 1.0]), np.array([0.0, 0.0])
    ts, S, V = [], [], []
    at_risk, surv, vt = n, 1.0, 0.0
    i = 0
    while i < n:
        tt = t_arr[i]; d = c = 0; j = i
        while j < n and t_arr[j] == tt:
            if e_arr[j] == 1: d += 1
            else: c += 1
            j += 1
        if d > 0 and at_risk > 0:
            surv *= (1.0 - d / at_risk)
            if at_risk - d > 0:
                vt += d / (at_risk * (at_risk - d))
        at_risk -= d + c
        ts.append(tt); S.append(surv); V.append(vt); i = j
    ts = np.array(ts); S = np.array(S); V = np.array(V)
    ci = 1.96 * S * np.sqrt(V)
    t_out = np.concatenate(([0.0], ts, [horizon]))
    S_out = np.concatenate(([1.0], S, [S[-1] if len(S) else 1.0]))
    ci_out = np.concatenate(([0.0], ci, [ci[-1] if len(ci) else 0.0]))
    return t_out, S_out, ci_out


# ============================================================
# Loaders
# ============================================================

def load_branch_lifetimes(df: pd.DataFrame) -> pd.DataFrame:
    grp = ["fuzzer", "target", "mode", "trial_id", "branch_id"]
    lf = df.groupby(grp).agg(
        branch_start=("global_time_sec", "min"),
        branch_end=("global_time_sec", "max"),
        level=("level", "first"),
    ).reset_index()
    lf["branch_start"] = np.maximum(0, lf["branch_start"] - 60).astype(int)
    return lf


def _parse_execs_log(path: Path) -> List[Tuple[int, int, int]]:
    rows = []
    try:
        for line in path.read_text().splitlines():
            p = line.split()
            if len(p) == 3:
                try:
                    rows.append((int(p[0]), int(p[1]), int(p[2])))
                except ValueError:
                    pass
    except Exception:
        pass
    rows.sort()
    return rows


def load_execs_traces(execs_root: Path):
    out = {}
    if not execs_root.exists():
        return out
    for exp_dir in sorted(execs_root.iterdir()):
        if not exp_dir.is_dir():
            continue
        name = exp_dir.name
        parts = name.rsplit("_", 2)
        if len(parts) < 3:
            continue
        mode = parts[-1]
        if mode not in ("nosplit", "online", "split"):
            continue
        rest = name[: -(len(mode) + 1)]
        r = rest.rsplit("_", 1)
        if len(r) != 2:
            continue
        fuzzer, target = r
        for trial_dir in sorted(exp_dir.glob("trial-*")):
            trial = trial_dir.name
            s = trial_dir / "S" / "execs.log"
            if s.exists():
                rows = _parse_execs_log(s)
                if rows:
                    out[(fuzzer, target, mode, trial, "r")] = rows
            for level_dir in trial_dir.glob("L[0-9]*"):
                if not level_dir.is_dir() or "_seeds" in level_dir.name:
                    continue
                for bdir in level_dir.iterdir():
                    if not bdir.is_dir():
                        continue
                    ef = bdir / "execs.log"
                    if ef.exists():
                        rows = _parse_execs_log(ef)
                        if rows:
                            out[(fuzzer, target, mode, trial, bdir.name)] = rows
    return out


def load_detected_events(pocs_csv: Path, exp_root: Path,
                         horizon_sec: int) -> pd.DataFrame:
    """For each trial & bug: first-detected time via PoC replay.
    Time is inferred from crash file mtime relative to the trial's earliest
    monitor file (proxy for container_start)."""
    if not pocs_csv.exists():
        return pd.DataFrame(columns=["fuzzer", "target", "mode", "trial_id",
                                     "bug_id", "t_detected"])
    pocs = pd.read_csv(pocs_csv)
    pocs = pocs[pocs["bug_id"].notna() & (pocs["bug_id"].astype(str) != "")]
    if pocs.empty:
        return pd.DataFrame(columns=["fuzzer", "target", "mode", "trial_id",
                                     "bug_id", "t_detected"])

    # Container-start proxy: for each (fuzzer, target, mode, trial, branch),
    # find the earliest monitor file mtime and the earliest t_sec label.
    # Then t_detected = crash_mtime - monitor_mtime + monitor_label.
    def _start_epoch(row):
        cf = Path(str(row["crash_file"]))
        if not cf.exists():
            return None
        # Find the branch's monitor dir (sibling findings dir)
        parent = cf.parent
        # navigate up to branch dir
        branch_dir = None
        cur = parent
        for _ in range(5):
            if (cur / "monitor").is_dir():
                branch_dir = cur
                break
            cur = cur.parent
        if branch_dir is None:
            return None
        mon = branch_dir / "monitor"
        if not mon.exists():
            return None
        files = sorted([f for f in mon.iterdir() if f.name.isdigit()],
                       key=lambda p: int(p.name))
        if not files:
            return None
        first = files[0]
        label = int(first.name)
        try:
            mon_mtime = first.stat().st_mtime
            cr_mtime = cf.stat().st_mtime
        except Exception:
            return None
        # container_start_epoch ≈ mon_mtime - label
        start_epoch = mon_mtime - label
        return cr_mtime - start_epoch

    pocs = pocs.copy()
    pocs["t_detected"] = pocs.apply(_start_epoch, axis=1)
    pocs = pocs[pocs["t_detected"].notna()]
    pocs["t_detected"] = pocs["t_detected"].clip(lower=1, upper=horizon_sec).astype(int)
    return (pocs.groupby(["fuzzer", "target", "mode", "trial_id", "bug_id"])
            ["t_detected"].min().reset_index())


def load_crash_counts(exp_root: Path) -> pd.DataFrame:
    """Raw crash file count per (fuzzer, target, mode, trial). Not bug-deduped —
    this is the 'number of crashing inputs the fuzzer saved'."""
    rows = []
    for exp_dir in sorted(exp_root.iterdir()):
        if not exp_dir.is_dir():
            continue
        name = exp_dir.name
        parts = name.rsplit("_", 2)
        if len(parts) < 3:
            continue
        mode = parts[-1]
        if mode not in ("nosplit", "online", "split"):
            continue
        rest = name[: -(len(mode) + 1)]
        r = rest.rsplit("_", 1)
        if len(r) != 2:
            continue
        fuzzer, target = r
        for trial_dir in sorted(exp_dir.glob("trial-*")):
            n = 0
            for cdir in trial_dir.rglob("crashes"):
                for f in cdir.rglob("*"):
                    if f.is_file() and not f.name.startswith("."):
                        n += 1
            for hdir in trial_dir.rglob("findings"):  # honggfuzz .fuzz
                for f in hdir.glob("*.fuzz"):
                    n += 1
            rows.append({"fuzzer": fuzzer, "target": target, "mode": mode,
                         "trial_id": trial_dir.name, "crashes": n})
    return pd.DataFrame(rows)


def load_fuzzer_stats(exp_root: Path) -> pd.DataFrame:
    """For AFL-family, read final fuzzer_stats per branch. Returns per-trial
    aggregated CPU-hours, total execs, mean bitmap_cvg."""
    rows = []
    for exp_dir in sorted(exp_root.iterdir()):
        if not exp_dir.is_dir():
            continue
        name = exp_dir.name
        parts = name.rsplit("_", 2)
        if len(parts) < 3:
            continue
        mode = parts[-1]
        if mode not in ("nosplit", "online", "split"):
            continue
        rest = name[: -(len(mode) + 1)]
        r = rest.rsplit("_", 1)
        if len(r) != 2:
            continue
        fuzzer, target = r
        for trial_dir in sorted(exp_dir.glob("trial-*")):
            runtime_sum = 0.0
            execs_sum = 0
            bcov_list = []
            branches = 0
            stats_paths = list(trial_dir.rglob("fuzzer_stats"))
            for sp in stats_paths:
                try:
                    d = {}
                    for line in sp.read_text().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            d[k.strip()] = v.strip()
                    if "run_time" in d:
                        runtime_sum += float(d["run_time"])
                    if "execs_done" in d:
                        execs_sum += int(d["execs_done"])
                    if "bitmap_cvg" in d:
                        v = d["bitmap_cvg"].rstrip("%").strip()
                        try:
                            bcov_list.append(float(v))
                        except ValueError:
                            pass
                    branches += 1
                except Exception:
                    pass
            rows.append({
                "fuzzer": fuzzer, "target": target, "mode": mode,
                "trial_id": trial_dir.name,
                "cpu_seconds": runtime_sum,
                "total_execs": execs_sum,
                "mean_bitmap_cvg_pct": float(np.mean(bcov_list)) if bcov_list else float("nan"),
                "n_branches": branches,
            })
    return pd.DataFrame(rows)


# ============================================================
# Core weighted math
# ============================================================

def compute_per_branch_firsts(df: pd.DataFrame, det: pd.DataFrame
                              ) -> Dict[Tuple, Dict[str, Tuple[int, int, int]]]:
    """For each (fuzzer, target, mode, trial, branch_id, bug_id): return
    {reached_t, triggered_t, detected_t} using branch-local global_time_sec.
    Missing means not reached/triggered/detected in this branch.
    """
    grp = ["fuzzer", "target", "mode", "trial_id", "branch_id", "bug_id"]
    r_t = df[df["reached"] > 0].groupby(grp)["global_time_sec"].min().rename("r").reset_index()
    t_t = df[df["triggered"] > 0].groupby(grp)["global_time_sec"].min().rename("t").reset_index()
    out = {}
    for _, row in r_t.iterrows():
        key = (row["fuzzer"], row["target"], row["mode"], row["trial_id"])
        out.setdefault(key, {})
        out[key].setdefault(row["branch_id"], {}).setdefault(row["bug_id"], {})["r"] = int(row["r"])
    for _, row in t_t.iterrows():
        key = (row["fuzzer"], row["target"], row["mode"], row["trial_id"])
        out.setdefault(key, {})
        out[key].setdefault(row["branch_id"], {}).setdefault(row["bug_id"], {})["t"] = int(row["t"])
    # Detected is trial-level; propagate to all branches of that trial
    if not det.empty:
        for _, row in det.iterrows():
            key = (row["fuzzer"], row["target"], row["mode"], row["trial_id"])
            # Attach to the root-most branch of the trial for trial-level detection
            for bid in out.get(key, {"r": None}):
                pass
            # We'll handle detected at the trial-level separately
    return out


def compute_weighted_trajectory(
    df: pd.DataFrame,
    lifetimes: pd.DataFrame,
    metric_col: str,           # "reached" or "triggered"
    horizon_sec: int,
    poll_sec: int = 60,
) -> pd.DataFrame:
    """For each (fuzzer, target, mode, trial), compute Σ_{t'≤t} (1/k_t') Σ_s Y^m_{s,t'}
    where Y=1 if branch s saw a NEW event (reached-count or triggered-count
    increased) during the interval ending at t'.
    Returns time-indexed long DataFrame.
    """
    grid = np.arange(poll_sec, horizon_sec + 1, poll_sec)
    grp_cols = ["fuzzer", "target", "mode", "trial_id"]
    rows = []
    for key, sub in df.groupby(grp_cols):
        fuzzer, target, mode, trial = key
        lt = lifetimes[(lifetimes["fuzzer"] == fuzzer)
                       & (lifetimes["target"] == target)
                       & (lifetimes["mode"] == mode)
                       & (lifetimes["trial_id"] == trial)]
        branches = sub["branch_id"].unique()
        # For each branch: at each grid point, count distinct bugs with metric>0
        per_br = {}
        for bid in branches:
            rr = sub[sub["branch_id"] == bid]
            evt = rr[rr[metric_col] > 0]
            first = evt.groupby("bug_id")["global_time_sec"].min().values
            per_br[bid] = np.array([int((first <= t).sum()) for t in grid])
        lt_arr = lt[["branch_id", "branch_start", "branch_end"]].values
        last_cum = {bid: 0 for bid in branches}
        N_w = 0.0
        for i, t in enumerate(grid):
            alive = [row[0] for row in lt_arr if row[1] <= t <= row[2]]
            k_t = max(1, len(alive))
            Y_sum = 0
            for bid in alive:
                cur = per_br[bid][i]
                if cur > last_cum[bid]:
                    Y_sum += 1
                    last_cum[bid] = cur
            N_w += Y_sum / k_t
            rows.append({
                "fuzzer": fuzzer, "target": target, "mode": mode,
                "trial_id": trial, "t_sec": int(t),
                "k_t": k_t, "Y_sum": Y_sum, "N_weighted": float(N_w),
            })
    return pd.DataFrame(rows)


def compute_detected_weighted_trajectory(
    det: pd.DataFrame,       # trial-level detected events
    lifetimes: pd.DataFrame,
    horizon_sec: int,
    poll_sec: int = 60,
) -> pd.DataFrame:
    """Weighted cumulative for Detected. Detection is trial-level, so we treat
    k_t = 1 for nosplit and = number of active branches for split modes."""
    if det.empty:
        return pd.DataFrame(columns=["fuzzer", "target", "mode", "trial_id",
                                     "t_sec", "k_t", "Y_sum", "N_weighted"])
    grid = np.arange(poll_sec, horizon_sec + 1, poll_sec)
    rows = []
    grp_cols = ["fuzzer", "target", "mode", "trial_id"]
    keys = det.groupby(grp_cols).size().reset_index()[grp_cols].values
    # Also enumerate trials without any detected (to provide zero curves)
    all_trials = lifetimes[grp_cols].drop_duplicates().values
    seen = {tuple(k) for k in keys}
    all_keys = list(seen) + [tuple(k) for k in all_trials if tuple(k) not in seen]

    for key in all_keys:
        fuzzer, target, mode, trial = key
        sub = det[(det["fuzzer"] == fuzzer) & (det["target"] == target)
                  & (det["mode"] == mode) & (det["trial_id"] == trial)]
        lt = lifetimes[(lifetimes["fuzzer"] == fuzzer) & (lifetimes["target"] == target)
                       & (lifetimes["mode"] == mode) & (lifetimes["trial_id"] == trial)]
        lt_arr = lt[["branch_id", "branch_start", "branch_end"]].values
        first_det = {row["bug_id"]: int(row["t_detected"]) for _, row in sub.iterrows()}
        last_cum_bugs = set()
        N_w = 0.0
        for t in grid:
            alive = [row[0] for row in lt_arr if row[1] <= t <= row[2]]
            k_t = max(1, len(alive))
            Y_sum = 0
            for bug, t_d in first_det.items():
                if t_d <= t and bug not in last_cum_bugs:
                    Y_sum += 1
                    last_cum_bugs.add(bug)
            N_w += Y_sum / k_t
            rows.append({
                "fuzzer": fuzzer, "target": target, "mode": mode,
                "trial_id": trial, "t_sec": int(t),
                "k_t": k_t, "Y_sum": Y_sum, "N_weighted": float(N_w),
            })
    return pd.DataFrame(rows)


def trajectory_to_summary(traj: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, sub in traj.groupby(["fuzzer", "target", "mode", "t_sec"]):
        f, tg, m, t_sec = key
        vals = sub["N_weighted"].values
        if len(vals) == 0:
            continue
        mu = vals.mean()
        sem = vals.std(ddof=1) / sqrt(len(vals)) if len(vals) > 1 else 0.0
        rows.append({
            "fuzzer": f, "target": tg, "mode": m, "t_sec": t_sec,
            "mean_N": mu, "sem_N": sem, "n_trials": len(vals),
            "var_P": (vals / max(t_sec, 1)).var(ddof=1) if len(vals) > 1 else 0.0,
        })
    return pd.DataFrame(rows)


# ============================================================
# Execs-axis weighted
# ============================================================

def compute_weighted_trajectory_execs(
    df: pd.DataFrame,
    lifetimes: pd.DataFrame,
    metric_col: str,
    execs_traces: Dict,
    horizon_sec: int,
    poll_sec: int = 60,
) -> pd.DataFrame:
    """Weighted trajectory indexed by trial-averaged execs per alive branch.

    Matches the Y-axis 1/k_t weighting: if branches share CPU work equally, the
    per-branch-equivalent exec count at wall time t is
        trial_exec(t) = (Σ_s e_s(t)) / k_t(t)
    For nosplit (k_t=1) this equals the single-branch execs. For online it
    shrinks by up to k_t=8, putting both modes on the same per-CPU scale.
    """
    grid_t = np.arange(poll_sec, horizon_sec + 1, poll_sec)
    grp_cols = ["fuzzer", "target", "mode", "trial_id"]
    rows = []
    for key, sub in df.groupby(grp_cols):
        fuzzer, target, mode, trial = key
        lt = lifetimes[(lifetimes["fuzzer"] == fuzzer)
                       & (lifetimes["target"] == target)
                       & (lifetimes["mode"] == mode)
                       & (lifetimes["trial_id"] == trial)]
        branches = sub["branch_id"].unique()

        # Per-branch cumulative event counts on the wall-time grid.
        per_br_N = {}
        for bid in branches:
            rr = sub[sub["branch_id"] == bid]
            evt = rr[rr[metric_col] > 0]
            first = evt.groupby("bug_id")["global_time_sec"].min().values
            per_br_N[bid] = np.array([int((first <= t).sum()) for t in grid_t])

        # Per-branch execs-vs-time (monotone; 0 before branch first log).
        per_br_ex = {}
        for bid in branches:
            rows_ex = execs_traces.get((fuzzer, target, mode, trial, bid), [])
            if rows_ex:
                t_arr = np.array([r[0] for r in rows_ex])
                e_arr = np.array([r[2] for r in rows_ex])
                idx = np.searchsorted(t_arr, grid_t, side="right") - 1
                exec_grid = np.where(idx >= 0, e_arr[np.clip(idx, 0, len(e_arr)-1)], 0)
            else:
                exec_grid = np.zeros(len(grid_t), dtype=np.int64)
            per_br_ex[bid] = exec_grid

        lt_arr = lt[["branch_id", "branch_start", "branch_end"]].values
        last_cum = {bid: 0 for bid in branches}
        N_w = 0.0
        for i, t in enumerate(grid_t):
            alive = [row[0] for row in lt_arr if row[1] <= t <= row[2]]
            # Fallback: if no lifetime info, treat every branch with nonzero execs as alive.
            if not alive:
                alive = [b for b in branches if per_br_ex.get(b, np.zeros(1))[i] > 0]
            k_t = max(1, len(alive))

            # Trial-averaged execs (x-axis)
            exec_sum = 0
            for bid in alive:
                exec_sum += int(per_br_ex.get(bid, np.zeros(len(grid_t)))[i])
            x_exec = exec_sum / k_t

            # Weighted bug increment (y-axis) — identical to time-axis formula
            Y_sum = 0
            for bid in alive:
                cur = int(per_br_N.get(bid, np.zeros(len(grid_t)))[i])
                if cur > last_cum[bid]:
                    Y_sum += 1
                    last_cum[bid] = cur
            N_w += Y_sum / k_t
            rows.append({
                "fuzzer": fuzzer, "target": target, "mode": mode,
                "trial_id": trial, "t_sec": int(t),
                "exec_avg": int(x_exec),
                "k_t": k_t, "Y_sum": Y_sum, "N_weighted": float(N_w),
            })
    return pd.DataFrame(rows)


def exec_trajectory_summary(traj: pd.DataFrame) -> pd.DataFrame:
    """Across-trial mean/variance on a common exec_avg grid.

    Each trial has its own (exec_avg, N_weighted) trajectory.  We build one
    grid PER (fuzzer, target, mode) (0 → max trial-finishing exec_avg) and
    step-interpolate each trial onto it.  Mean and variance are across trials
    at each grid point.
    """
    rows = []
    grp = ["fuzzer", "target", "mode"]
    for (f, tg, m), sub in traj.groupby(grp):
        trials = sorted(sub["trial_id"].unique())
        if not trials:
            continue
        last_exec_per_trial = []
        trial_curves = []
        for tr in trials:
            r = sub[sub["trial_id"] == tr].sort_values("t_sec")
            ex = r["exec_avg"].values.astype(np.int64)
            N = r["N_weighted"].values.astype(float)
            if len(ex) == 0:
                continue
            # enforce monotone exec_avg (should already be)
            ex = np.maximum.accumulate(ex)
            trial_curves.append((ex, N))
            last_exec_per_trial.append(int(ex[-1]))
        if not trial_curves:
            continue
        # Common grid: 0 .. median of trial max exec_avg.  Using median tolerates
        # the occasional failed trial (max much smaller than others) without
        # collapsing the grid to that trial's range.  For any grid point beyond
        # a trial's own last exec_avg we forward-fill its final N_weighted
        # (valid because N_weighted is monotone non-decreasing).
        max_grid = int(np.median(last_exec_per_trial))
        if max_grid < 1:
            max_grid = max(last_exec_per_trial)
        if max_grid < 1:
            continue
        grid = np.linspace(1, max_grid, 200).astype(np.int64)
        N_mat = np.zeros((len(trial_curves), len(grid)))
        for i, (ex, N) in enumerate(trial_curves):
            idx = np.searchsorted(ex, grid, side="right") - 1
            idx = np.clip(idx, 0, len(N) - 1)
            N_mat[i, :] = N[idx]
            # extrapolate beyond this trial's range with its final N
            beyond = grid > ex[-1]
            if beyond.any():
                N_mat[i, beyond] = N[-1]
        for j, g in enumerate(grid):
            vals = N_mat[:, j]
            mu = vals.mean()
            sem = vals.std(ddof=1) / sqrt(len(vals)) if len(vals) > 1 else 0.0
            var_P = (vals / max(g, 1)).var(ddof=1) if len(vals) > 1 else 0.0
            rows.append({
                "fuzzer": f, "target": tg, "mode": m, "t_sec": int(g),
                "mean_N": mu, "sem_N": sem,
                "n_trials": len(vals), "var_P": var_P,
            })
    return pd.DataFrame(rows)


# ============================================================
# Figure helpers
# ============================================================

def _plot_cumulative(summary, horizon_x, outdir, title_prefix,
                      x_label, x_scale=1.0):
    outdir.mkdir(parents=True, exist_ok=True)
    if summary is None or len(summary) == 0 or "fuzzer" not in summary.columns:
        print(f"  (skip cumulative {title_prefix}: empty summary)")
        return
    for (f, tgt), grp in summary.groupby(["fuzzer", "target"]):
        fig, ax = plt.subplots(figsize=(6.5, 3.8))
        for mode in sorted(grp["mode"].unique()):
            m = grp[grp["mode"] == mode].sort_values("t_sec")
            if len(m) == 0: continue
            style = MODE_STYLE.get(mode, {"ls": "-", "color": "gray"})
            ax.plot(m["t_sec"] / x_scale, m["mean_N"], ls=style["ls"],
                    color=style["color"], lw=2,
                    label=f"{mode} (N={int(m['n_trials'].iloc[0])})")
            ax.fill_between(m["t_sec"] / x_scale,
                            m["mean_N"] - 1.96 * m["sem_N"],
                            m["mean_N"] + 1.96 * m["sem_N"],
                            color=style["color"], alpha=0.18)
        ax.set_xlim(0, horizon_x / x_scale)
        ax.set_xlabel(x_label)
        ax.set_ylabel("Weighted cumulative events")
        ax.set_title(f"{f} · {tgt} · {title_prefix}")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=9, loc="lower right")
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tgt}.png", dpi=140)
        plt.close(fig)


def _plot_bdr_rate(summary, horizon_x, outdir, title_prefix,
                    x_label, x_scale=1.0):
    outdir.mkdir(parents=True, exist_ok=True)
    if summary is None or len(summary) == 0 or "fuzzer" not in summary.columns:
        print(f"  (skip bdr_rate {title_prefix}: empty summary)")
        return
    for (f, tgt), grp in summary.groupby(["fuzzer", "target"]):
        fig, ax = plt.subplots(figsize=(6.5, 3.8))
        for mode in sorted(grp["mode"].unique()):
            m = grp[grp["mode"] == mode].sort_values("t_sec")
            m = m[m["t_sec"] >= (60 if x_scale <= 3600 else 1)]
            if len(m) < 2: continue
            rate = (m["mean_N"] / m["t_sec"])
            if x_scale <= 3600:
                rate = rate * 3600  # bugs/hour on time axis
                ylabel = "rate (bugs/hour)"
            else:
                rate = rate * 1_000_000  # bugs/Mexec on execs axis
                ylabel = "rate (bugs / million execs)"
            style = MODE_STYLE.get(mode, {"ls": "-", "color": "gray"})
            ax.plot(m["t_sec"] / x_scale, rate, ls=style["ls"],
                    color=style["color"], lw=2, label=mode)
        ax.set_xlim(0, horizon_x / x_scale)
        ax.set_xlabel(x_label)
        ax.set_ylabel(ylabel)
        ax.set_title(f"{f} · {tgt} · {title_prefix} rate")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=9)
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tgt}.png", dpi=140)
        plt.close(fig)


def _plot_variance(summary, horizon_x, outdir, title_prefix,
                    x_label, x_scale=1.0):
    outdir.mkdir(parents=True, exist_ok=True)
    if summary is None or len(summary) == 0 or "fuzzer" not in summary.columns:
        print(f"  (skip variance {title_prefix}: empty summary)")
        return
    for (f, tgt), grp in summary.groupby(["fuzzer", "target"]):
        fig, ax = plt.subplots(figsize=(6.5, 3.8))
        for mode in sorted(grp["mode"].unique()):
            m = grp[grp["mode"] == mode].sort_values("t_sec")
            if len(m) < 2: continue
            norm = m["var_P"].values * m["t_sec"].values
            style = MODE_STYLE.get(mode, {"ls": "-", "color": "gray"})
            ax.plot(m["t_sec"] / x_scale, norm, ls=style["ls"],
                    color=style["color"], lw=2, label=mode)
        ax.set_xlim(0, horizon_x / x_scale)
        ax.set_xlabel(x_label)
        ax.set_ylabel("Var(P̂(t)) × effort")
        ax.set_title(f"{f} · {tgt} · {title_prefix} variance")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=9)
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tgt}.png", dpi=140)
        plt.close(fig)


def fig_survival(df: pd.DataFrame, horizon_sec: int,
                  metric_col: str, outdir: Path, metric_name: str):
    """KM survival per (fuzzer, target, bug) using the specified metric."""
    outdir.mkdir(parents=True, exist_ok=True)
    grp = ["fuzzer", "target", "mode", "trial_id", "bug_id"]
    ev = df[df[metric_col] > 0].groupby(grp)["global_time_sec"].min().reset_index()
    full = df[grp].drop_duplicates()
    joined = full.merge(ev, on=grp, how="left")
    joined["t"] = joined["global_time_sec"].fillna(horizon_sec).astype(int)
    joined["e"] = joined["global_time_sec"].notna().astype(int)
    n = 0
    for (f, tg, b), sub in joined.groupby(["fuzzer", "target", "bug_id"]):
        if sub["e"].sum() == 0:
            continue
        fig, ax = plt.subplots(figsize=(5.5, 3.2))
        for mode in sorted(sub["mode"].unique()):
            s = sub[sub["mode"] == mode]
            ts, S, ci = kaplan_meier(s["t"].tolist(), s["e"].tolist(), horizon_sec)
            st = MODE_STYLE.get(mode, {"ls": "-", "color": "gray"})
            ax.step(ts / 60.0, S, where="post", ls=st["ls"], color=st["color"], lw=1.8,
                    label=f"{mode} ({s['e'].sum()}/{len(s)})")
            ax.fill_between(ts / 60.0, np.clip(S - ci, 0, 1), np.clip(S + ci, 0, 1),
                            step="post", color=st["color"], alpha=0.12)
        ax.set_xlim(0, horizon_sec / 60.0)
        ax.set_ylim(0, 1.02)
        ax.set_xlabel("Time (minutes)")
        ax.set_ylabel(f"P(bug not {metric_name})")
        ax.set_title(f"{f} · {tg} · {b}  ({metric_name})")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="lower left")
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tg}_{b}.png", dpi=140)
        plt.close(fig)
        n += 1
    return n


def fig_detected_survival(df_base: pd.DataFrame, det: pd.DataFrame,
                          horizon_sec: int, outdir: Path) -> int:
    """KM for Detected using t_detected with censoring at horizon."""
    outdir.mkdir(parents=True, exist_ok=True)
    if det is None or len(det) == 0 or "fuzzer" not in det.columns:
        print("  (skip detected-survival: no detection events)")
        return 0
    grp = ["fuzzer", "target", "mode", "trial_id", "bug_id"]
    full = df_base[grp].drop_duplicates()
    merged = full.merge(det, on=grp, how="left")
    merged["t"] = merged["t_detected"].fillna(horizon_sec).astype(int)
    merged["e"] = merged["t_detected"].notna().astype(int)
    n = 0
    for (f, tg, b), sub in merged.groupby(["fuzzer", "target", "bug_id"]):
        if sub["e"].sum() == 0:
            continue
        fig, ax = plt.subplots(figsize=(5.5, 3.2))
        for mode in sorted(sub["mode"].unique()):
            s = sub[sub["mode"] == mode]
            ts, S, ci = kaplan_meier(s["t"].tolist(), s["e"].tolist(), horizon_sec)
            st = MODE_STYLE.get(mode, {"ls": "-", "color": "gray"})
            ax.step(ts / 60.0, S, where="post", ls=st["ls"], color=st["color"], lw=1.8,
                    label=f"{mode} ({s['e'].sum()}/{len(s)})")
            ax.fill_between(ts / 60.0, np.clip(S - ci, 0, 1), np.clip(S + ci, 0, 1),
                            step="post", color=st["color"], alpha=0.12)
        ax.set_xlim(0, horizon_sec / 60.0)
        ax.set_ylim(0, 1.02)
        ax.set_xlabel("Time (minutes)")
        ax.set_ylabel("P(bug not detected)")
        ax.set_title(f"{f} · {tg} · {b}  (Detected via replay)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="lower left")
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tg}_{b}.png", dpi=140)
        plt.close(fig)
        n += 1
    return n


def fig_splits_histogram(logdir: Path, outpath: Path):
    if not logdir.exists(): return
    by_fuzzer = defaultdict(list)
    for log in logdir.glob("*_online.log"):
        fuzzer = log.stem.replace("_online", "")
        trial_splits = defaultdict(int)
        for line in log.read_text().splitlines():
            if "SPLIT at t=" in line and "trial-" in line:
                tid = line.split("trial-")[1].split(" ")[0].split("]")[0]
                trial_splits[tid] += 1
        for k in range(5):
            trial_splits.setdefault(str(k), 0)
        by_fuzzer[fuzzer].extend(trial_splits.values())
    if not by_fuzzer: return
    fig, ax = plt.subplots(figsize=(6, 3.5))
    keys = sorted(by_fuzzer.keys())
    ax.boxplot([by_fuzzer[f] for f in keys], tick_labels=keys, showmeans=True, widths=0.5)
    ax.axhline(7, color="gray", ls=":", alpha=0.5, label="max (K=3 stages, B=2)")
    ax.set_ylabel("split events per trial")
    ax.set_title("Online split count per trial")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(outpath, dpi=140)
    plt.close(fig)


def fig_crash_counts(crashes: pd.DataFrame, outdir: Path):
    if crashes.empty: return
    outdir.mkdir(parents=True, exist_ok=True)
    for (f, tg), grp in crashes.groupby(["fuzzer", "target"]):
        fig, ax = plt.subplots(figsize=(6, 3.5))
        modes = sorted(grp["mode"].unique())
        data = [grp[grp["mode"] == m]["crashes"].values for m in modes]
        bp = ax.boxplot(data, tick_labels=modes, showmeans=True, widths=0.5)
        ax.set_ylabel("Raw crash files per trial")
        ax.set_title(f"{f} · {tg} · raw crash count")
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tg}.png", dpi=140)
        plt.close(fig)


def _compute_bitmap_union_coverage(exp_root: Path, fuzzer: str, target: str,
                                    mode: str, trial_dir: Path) -> Optional[Dict[str, float]]:
    """For one trial, OR together every branch's AFL fuzz_bitmap.
    Returns {'union_pct': float, 'max_pct': float, 'root_pct': float or None}.
    AFL's fuzz_bitmap is a 65536-byte file; an edge is 'covered' if that byte != 255
    (AFL convention). Returns None for non-AFL-family fuzzers or if no bitmaps found.
    """
    if fuzzer == "honggfuzz":
        return None
    bitmaps = []
    root_bm = None
    for bm_path in trial_dir.rglob("fuzz_bitmap"):
        try:
            data = bm_path.read_bytes()
        except Exception:
            continue
        if len(data) != 65536:
            continue
        bitmaps.append(data)
        # L0 root branch's bitmap: path contains /L0/r/ for split, or /S/ for nosplit
        if "/L0/r/" in str(bm_path) or "/S/" in str(bm_path):
            root_bm = data
    if not bitmaps:
        return None

    # AFL convention: byte != 255 means that edge was hit
    def pct(bm: bytes) -> float:
        arr = np.frombuffer(bm, dtype=np.uint8)
        return float((arr != 255).sum()) / len(arr) * 100.0

    per = [pct(bm) for bm in bitmaps]
    # Union: OR-combine inverse (a byte is "unhit" iff all branches had 255)
    stacked = np.vstack([np.frombuffer(bm, dtype=np.uint8) for bm in bitmaps])
    # Edge e is covered iff any branch has stacked[i,e] != 255
    union_hit = (stacked != 255).any(axis=0)
    union_pct = float(union_hit.sum()) / 65536.0 * 100.0
    max_pct = max(per) if per else 0.0
    root_pct = pct(root_bm) if root_bm is not None else None
    return {"union_pct": union_pct, "max_pct": max_pct, "root_pct": root_pct,
            "mean_pct": float(np.mean(per))}


def fig_coverage(exp_root: Path, outdir: Path):
    """Trial-level coverage per (fuzzer, target, mode): union of branches' AFL
    fuzz_bitmap files. Also shows max-branch and L0-root for reference.
    honggfuzz is skipped (no compatible bitmap file)."""
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for exp_dir in sorted(exp_root.iterdir()):
        if not exp_dir.is_dir(): continue
        name = exp_dir.name
        parts = name.rsplit("_", 2)
        if len(parts) < 3: continue
        mode = parts[-1]
        if mode not in ("nosplit", "online", "split"): continue
        rest = name[: -(len(mode) + 1)]
        r = rest.rsplit("_", 1)
        if len(r) != 2: continue
        fuzzer, target = r
        if fuzzer == "honggfuzz": continue
        for trial_dir in sorted(exp_dir.glob("trial-*")):
            cov = _compute_bitmap_union_coverage(exp_root, fuzzer, target, mode, trial_dir)
            if cov is None: continue
            rows.append({
                "fuzzer": fuzzer, "target": target, "mode": mode,
                "trial_id": trial_dir.name,
                **cov,
            })
    if not rows:
        print("  coverage: no AFL-family bitmaps found"); return
    df = pd.DataFrame(rows)
    df.to_csv(outdir.parent / "tables" / "coverage_trial_level.csv", index=False)

    for (f, tg), grp in df.groupby(["fuzzer", "target"]):
        fig, ax = plt.subplots(figsize=(7, 3.8))
        modes = sorted(grp["mode"].unique())
        x = np.arange(len(modes))
        width = 0.22
        # For each mode, three boxes: union, max, root
        for i, metric in enumerate(["union_pct", "max_pct", "root_pct"]):
            data = [grp[grp["mode"] == m][metric].dropna().values for m in modes]
            if not any(len(d) > 0 for d in data): continue
            positions = x + (i - 1) * width
            bp = ax.boxplot(data, positions=positions, widths=width * 0.9,
                            showmeans=True, patch_artist=True,
                            tick_labels=modes if i == 1 else [""] * len(modes))
            color = ["#2a9d8f", "#e9c46a", "#4a4a4a"][i]
            for patch in bp["boxes"]:
                patch.set_facecolor(color)
                patch.set_alpha(0.6)
        ax.set_xticks(x)
        ax.set_xticklabels(modes)
        ax.set_ylabel("bitmap coverage (%)")
        ax.set_title(f"{f} · {tg} · coverage — union (teal) / max-branch (yellow) / L0-root (gray)")
        ax.grid(axis="y", alpha=0.3)
        # Custom legend via proxy artists
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(facecolor="#2a9d8f", alpha=0.6, label="Union-of-branches (trial total)"),
                           Patch(facecolor="#e9c46a", alpha=0.6, label="Max single branch"),
                           Patch(facecolor="#4a4a4a", alpha=0.6, label="L0 root only")],
                  fontsize=7, loc="lower right")
        fig.tight_layout()
        fig.savefig(outdir / f"{f}_{tg}.png", dpi=140)
        plt.close(fig)
    print(f"  coverage: fair trial-level union coverage saved to {outdir}")


def fig_cpu_hours(stats: pd.DataFrame, outpath: Path):
    if stats.empty: return
    outpath.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4))
    keys, vals = [], []
    for (f, tg, m), grp in stats.groupby(["fuzzer", "target", "mode"]):
        keys.append(f"{f}\n{tg}\n{m}")
        vals.append(grp["cpu_seconds"].sum() / 3600.0)
    x = np.arange(len(keys))
    ax.bar(x, vals, color="#556270")
    ax.set_xticks(x)
    ax.set_xticklabels(keys, rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("Total CPU-hours across trials")
    ax.set_title("Effort consumed per (fuzzer × target × mode)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(outpath, dpi=140)
    plt.close(fig)


# ============================================================
# Per-bug summary table
# ============================================================

def per_bug_summary_table(df: pd.DataFrame, det: pd.DataFrame,
                           horizon_sec: int, outpath: Path):
    """Per (fuzzer, target, mode, bug): count of trials that reached /
    triggered / detected, plus mean first-event times (with censoring)."""
    grp = ["fuzzer", "target", "mode", "trial_id", "bug_id"]
    all_trials = df[grp].drop_duplicates()
    first_r = df[df["reached"] > 0].groupby(grp)["global_time_sec"].min().rename("t_reached").reset_index()
    first_t = df[df["triggered"] > 0].groupby(grp)["global_time_sec"].min().rename("t_triggered").reset_index()
    joined = all_trials.merge(first_r, on=grp, how="left").merge(first_t, on=grp, how="left")
    if not det.empty:
        joined = joined.merge(det.rename(columns={"t_detected": "t_det"}),
                              on=grp, how="left")
    else:
        joined["t_det"] = np.nan

    # Group per (fuzzer, target, mode, bug_id)
    rows = []
    for (f, tg, m, b), sub in joined.groupby(["fuzzer", "target", "mode", "bug_id"]):
        n = len(sub)
        r_ev = int(sub["t_reached"].notna().sum())
        t_ev = int(sub["t_triggered"].notna().sum())
        d_ev = int(sub["t_det"].notna().sum())
        rows.append({
            "fuzzer": f, "target": tg, "mode": m, "bug_id": b,
            "n_trials": n,
            "n_reached": r_ev,
            "n_triggered": t_ev,
            "n_detected": d_ev,
            "mean_reach_sec": sub["t_reached"].dropna().mean() if r_ev else None,
            "mean_trig_sec": sub["t_triggered"].dropna().mean() if t_ev else None,
            "mean_det_sec": sub["t_det"].dropna().mean() if d_ev else None,
        })
    pd.DataFrame(rows).to_csv(outpath, index=False)
    return pd.DataFrame(rows)


# ============================================================
# Main
# ============================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="finalize/all_runs.csv")
    p.add_argument("--pocs", default=None)
    p.add_argument("--execs-root", default=None)
    p.add_argument("--total-hours", type=float, required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--logdir", default=None)
    args = p.parse_args()

    horizon_sec = int(args.total_hours * 3600)
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "tables").mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(args.input)
    print(f"Loaded {len(df):,} rows")
    lt = load_branch_lifetimes(df)
    print(f"Branches: {len(lt):,}")

    det = load_detected_events(Path(args.pocs), Path(args.execs_root),
                               horizon_sec) if args.pocs and args.execs_root else pd.DataFrame()
    print(f"Detected trial-bug events: {len(det):,}")

    # ------------------ weighted trajectories ------------------
    print("Weighted trajectories…")
    traj_reached = compute_weighted_trajectory(df, lt, "reached", horizon_sec)
    traj_triggered = compute_weighted_trajectory(df, lt, "triggered", horizon_sec)
    traj_detected = compute_detected_weighted_trajectory(det, lt, horizon_sec)

    # time-axis summaries
    sum_r = trajectory_to_summary(traj_reached)
    sum_t = trajectory_to_summary(traj_triggered)
    sum_d = trajectory_to_summary(traj_detected)
    sum_r.to_csv(outdir / "tables" / "weighted_reached_time.csv", index=False)
    sum_t.to_csv(outdir / "tables" / "weighted_triggered_time.csv", index=False)
    sum_d.to_csv(outdir / "tables" / "weighted_detected_time.csv", index=False)

    # execs axis (requires execs traces)
    sum_r_e = sum_t_e = sum_d_e = pd.DataFrame()
    execs_traces = {}
    if args.execs_root:
        execs_traces = load_execs_traces(Path(args.execs_root))
        print(f"Exec traces: {len(execs_traces):,} branches")
        if execs_traces:
            e_r = compute_weighted_trajectory_execs(
                df, lt, "reached", execs_traces, horizon_sec)
            e_t = compute_weighted_trajectory_execs(
                df, lt, "triggered", execs_traces, horizon_sec)
            sum_r_e = exec_trajectory_summary(e_r)
            sum_t_e = exec_trajectory_summary(e_t)
            sum_r_e.to_csv(outdir / "tables" / "weighted_reached_execs.csv", index=False)
            sum_t_e.to_csv(outdir / "tables" / "weighted_triggered_execs.csv", index=False)
            # Detected has no fine-grained execs time; approximate via (t_det → execs at that time)
            # For now, reuse time-axis summary for detected on execs axis by skipping.

    # ------------------ cumulative figures (4 per metric) ------------------
    print("Cumulative figures…")
    _plot_cumulative(sum_r, horizon_sec, outdir / "cumulative" / "reached_time",
                      "Reached (weighted)", "Time (minutes)", 60.0)
    _plot_cumulative(sum_t, horizon_sec, outdir / "cumulative" / "triggered_time",
                      "Triggered (weighted)", "Time (minutes)", 60.0)
    _plot_cumulative(sum_d, horizon_sec, outdir / "cumulative" / "detected_time",
                      "Detected (weighted)", "Time (minutes)", 60.0)
    if len(sum_r_e):
        maxex = int(sum_r_e["t_sec"].max())
        _plot_cumulative(sum_r_e, maxex, outdir / "cumulative" / "reached_execs",
                          "Reached (weighted, execs axis)", "Executions (M)", 1_000_000.0)
        _plot_cumulative(sum_t_e, maxex, outdir / "cumulative" / "triggered_execs",
                          "Triggered (weighted, execs axis)", "Executions (M)", 1_000_000.0)

    # ------------------ BDR rate figures ------------------
    print("BDR-rate figures…")
    _plot_bdr_rate(sum_r, horizon_sec, outdir / "bdr_rate" / "reached_time",
                    "Reached", "Time (minutes)", 60.0)
    _plot_bdr_rate(sum_t, horizon_sec, outdir / "bdr_rate" / "triggered_time",
                    "Triggered", "Time (minutes)", 60.0)
    _plot_bdr_rate(sum_d, horizon_sec, outdir / "bdr_rate" / "detected_time",
                    "Detected", "Time (minutes)", 60.0)
    if len(sum_r_e):
        maxex = int(sum_r_e["t_sec"].max())
        _plot_bdr_rate(sum_r_e, maxex, outdir / "bdr_rate" / "reached_execs",
                        "Reached", "Executions (M)", 1_000_000.0)
        _plot_bdr_rate(sum_t_e, maxex, outdir / "bdr_rate" / "triggered_execs",
                        "Triggered", "Executions (M)", 1_000_000.0)

    # ------------------ variance figures ------------------
    print("Variance figures…")
    _plot_variance(sum_r, horizon_sec, outdir / "variance" / "reached_time",
                    "Reached", "Time (minutes)", 60.0)
    _plot_variance(sum_t, horizon_sec, outdir / "variance" / "triggered_time",
                    "Triggered", "Time (minutes)", 60.0)
    _plot_variance(sum_d, horizon_sec, outdir / "variance" / "detected_time",
                    "Detected", "Time (minutes)", 60.0)
    if len(sum_r_e):
        maxex = int(sum_r_e["t_sec"].max())
        _plot_variance(sum_r_e, maxex, outdir / "variance" / "reached_execs",
                        "Reached", "Executions (M)", 1_000_000.0)
        _plot_variance(sum_t_e, maxex, outdir / "variance" / "triggered_execs",
                        "Triggered", "Executions (M)", 1_000_000.0)

    # ------------------ survival curves ------------------
    print("Survival curves…")
    n_r = fig_survival(df, horizon_sec, "reached",
                        outdir / "survival" / "reached", "reached")
    n_t = fig_survival(df, horizon_sec, "triggered",
                        outdir / "survival" / "triggered", "triggered")
    n_d = fig_detected_survival(df, det, horizon_sec,
                                  outdir / "survival" / "detected")
    print(f"  reached: {n_r}, triggered: {n_t}, detected: {n_d}")

    # ------------------ additional metrics ------------------
    print("Extra metrics (crashes, coverage, cpu-hours)…")
    if args.execs_root:
        crashes = load_crash_counts(Path(args.execs_root))
        fig_crash_counts(crashes, outdir / "crash_count")
        crashes.to_csv(outdir / "tables" / "crash_counts.csv", index=False)

        stats = load_fuzzer_stats(Path(args.execs_root))
        fig_coverage(Path(args.execs_root), outdir / "coverage")
        fig_cpu_hours(stats, outdir / "cpu_effort" / "cpu_hours_per_campaign.png")
        (outdir / "cpu_effort").mkdir(exist_ok=True, parents=True)
        stats.to_csv(outdir / "tables" / "cpu_effort_exec_rates.csv", index=False)

    # ------------------ per-bug summary table ------------------
    print("Per-bug summary table…")
    per_bug_summary_table(df, det, horizon_sec,
                          outdir / "tables" / "summary_per_bug.csv")

    # ------------------ splits histogram ------------------
    if args.logdir:
        fig_splits_histogram(Path(args.logdir), outdir / "splits_histogram.png")

    print(f"\nDone. {outdir}")


if __name__ == "__main__":
    main()

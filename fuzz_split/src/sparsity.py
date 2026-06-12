"""Offline sparsity-based split-time computation.

Given a non-split baseline CSV (FuzzBench output), computes optimal split
times for each (benchmark, fuzzer) pair using the algorithm from the paper
(Appendix C.1).

Output: sparsity_split_times_summary.csv with columns:
  benchmark, fuzzer, n_trials, t_end_h, bugs_end_avg, richness_raw,
  richness_used, start_h, zone1_h, zone2_h, zone3_h

Usage:
  python -m src.sparsity --input data/baseline.csv --output data/
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import config as C


@dataclass
class PairResult:
    benchmark: str
    fuzzer: str
    n_trials: int
    t_end_h: float
    bugs_end_avg: float
    richness_raw: float
    richness_used: float
    start_h: float
    zone_times_h: List[Optional[float]]
    split_times_h: List[Optional[float]]


def _interp_at(times: np.ndarray, values: np.ndarray, t: float) -> float:
    if len(times) == 0:
        return 0.0
    if t <= times[0]:
        return float(values[0])
    if t >= times[-1]:
        return float(values[-1])
    return float(np.interp(t, times, values))


def _compute_ratio_series(
    times_h: np.ndarray,
    n_h: np.ndarray,
    t_end_h: float,
    n_end: float,
    rate_mode: str = C.RATE_MODE,
    window_h: float = C.WINDOW_HOURS,
    eps: float = 1e-12,
) -> np.ndarray:
    """Compute sparsity ratio rho(t) on each grid time."""
    if t_end_h <= 0 or n_end <= 0:
        return np.zeros_like(times_h, dtype=float)

    total_rate = n_end / max(t_end_h, eps)
    ratios = np.zeros_like(times_h, dtype=float)

    for i, t in enumerate(times_h):
        if t < 0:
            continue
        if rate_mode == "tail":
            dt = max(t_end_h - t, eps)
            rate = (n_end - n_h[i]) / dt
        elif rate_mode == "window":
            t2 = min(t + window_h, t_end_h)
            n2 = _interp_at(times_h, n_h, t2)
            dt = max(t2 - t, eps)
            rate = (n2 - n_h[i]) / dt
        else:
            raise ValueError(f"Unknown rate_mode: {rate_mode}")
        ratios[i] = rate / max(total_rate, eps)

    return np.clip(ratios, 0.0, 10.0)


def _future_window_max(x: np.ndarray, window_len: int) -> np.ndarray:
    n = len(x)
    out = np.empty_like(x)
    wl = max(window_len, 1)
    for i in range(n):
        j2 = min(n, i + wl)
        out[i] = float(np.max(x[i:j2]))
    return out


def _first_increase_after(times_h: np.ndarray, n_h: np.ndarray, after_h: float) -> Optional[float]:
    if len(times_h) < 2:
        return None
    n_mon = np.maximum.accumulate(n_h)
    for i in range(1, len(times_h)):
        if times_h[i] <= after_h:
            continue
        if n_mon[i] > n_mon[i - 1]:
            return float(times_h[i])
    return None


def _find_zone_and_split_times(
    times_h: np.ndarray,
    ratios: np.ndarray,
    thresholds: Sequence[float],
    start_h: float,
    persist_h: float,
    min_gap_h: float,
    t_end_h: float,
    no_split_last_h: float,
    n_h: np.ndarray,
    bug_trigger: bool = C.BUG_TRIGGER_AFTER_ZONE,
    fallback: str = C.NO_BUG_AFTER_ZONE_FALLBACK,
    snapshot_seconds: int = C.DEFAULT_SNAPSHOT_SECONDS,
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """Compute (zone_times, split_times) sequentially with constraints."""
    if len(times_h) == 0:
        return ([None] * len(thresholds), [None] * len(thresholds))

    cutoff_h = float(t_end_h) - float(no_split_last_h)

    dt = float(np.median(np.diff(times_h))) if len(times_h) > 1 else (snapshot_seconds / 3600.0)
    win_len = max(1, int(math.ceil(persist_h / max(dt, 1e-6))))
    future_max = _future_window_max(ratios, win_len)

    zone_times: List[Optional[float]] = []
    split_times: List[Optional[float]] = []
    prev_split: Optional[float] = None

    for theta in thresholds:
        earliest = float(start_h)
        if prev_split is not None:
            earliest = max(earliest, float(prev_split) + float(min_gap_h))

        if earliest > cutoff_h + 1e-12:
            zone_times.append(None)
            split_times.append(None)
            continue

        z: Optional[float] = None
        for i, t in enumerate(times_h):
            if t < earliest:
                continue
            if t > cutoff_h + 1e-12:
                break
            if future_max[i] <= theta:
                z = float(t)
                break

        zone_times.append(z)

        if z is None:
            split_times.append(None)
            continue

        if bug_trigger:
            s = _first_increase_after(times_h, n_h, float(z))
            if s is None:
                if fallback == "none":
                    s = None
                elif fallback == "zone":
                    s = float(z)
                elif fallback == "end":
                    s = float(t_end_h)
        else:
            s = float(z)

        if s is None or float(s) > cutoff_h + 1e-12:
            split_times.append(None)
            continue

        if prev_split is not None and float(s) < float(prev_split) + float(min_gap_h) - 1e-12:
            split_times.append(None)
            continue

        split_times.append(float(s))
        prev_split = float(s)

    return zone_times, split_times


def _richness_score(n_end: float) -> Tuple[float, float]:
    raw = float(n_end)
    if C.RICHNESS_TRANSFORM == "log1p":
        used = float(np.log1p(max(raw, 0.0)))
    else:
        used = raw
    return raw, used


def _compute_start_hours(
    richness_by_pair: Dict[Tuple[str, str], float],
) -> Dict[Tuple[str, str], float]:
    """Compute richness-adaptive start hours per (bench, fuzzer)."""
    by_fuzzer: Dict[str, List[Tuple[Tuple[str, str], float]]] = {}
    for key, val in richness_by_pair.items():
        _, f = key
        by_fuzzer.setdefault(f, []).append((key, float(val)))

    start_h_by_pair: Dict[Tuple[str, str], float] = {}

    for fuzzer, items in by_fuzzer.items():
        vals = np.array([v for _, v in items], dtype=float)
        if len(vals) == 0:
            continue

        q_low = float(np.quantile(vals, C.Q_LOW))
        q_high = float(np.quantile(vals, C.Q_HIGH))
        denom = max(q_high - q_low, 1e-12)

        p_use = 1.0
        if C.AUTO_FIT_POWER and len(vals) >= 3:
            v_pivot = float(np.quantile(vals, C.PIVOT_Q))
            pivot_u = float(np.clip((v_pivot - q_low) / denom, 0.0, 1.0))
            if 0.0 < pivot_u < 1.0:
                y = (C.PIVOT_HOUR - C.START_HOUR_MIN) / max(C.START_HOUR_MAX - C.START_HOUR_MIN, 1e-12)
                if 0.0 < y < 1.0:
                    try:
                        p_use = float(math.log(y) / math.log(pivot_u))
                        if not (math.isfinite(p_use) and p_use > 0):
                            p_use = 1.0
                    except (ValueError, ZeroDivisionError):
                        p_use = 1.0

        for key, v in items:
            u = float(np.clip((v - q_low) / denom, 0.0, 1.0))
            sh = C.START_HOUR_MIN + (C.START_HOUR_MAX - C.START_HOUR_MIN) * (u ** p_use)
            start_h_by_pair[key] = float(np.clip(sh, C.START_HOUR_MIN, C.START_HOUR_MAX))

    return start_h_by_pair


def compute_split_times(
    df: pd.DataFrame,
    fuzzers: Optional[List[str]] = None,
    benchmarks: Optional[List[str]] = None,
    max_time_seconds: Optional[int] = None,
) -> Tuple[List[PairResult], pd.DataFrame]:
    """Compute split times from a FuzzBench baseline CSV.

    Args:
        df: DataFrame with columns: benchmark, fuzzer, trial_id, time, bugs_covered
        fuzzers: filter to these fuzzers (None = all)
        benchmarks: filter to these benchmarks (None = all)
        max_time_seconds: clip time to this value (None = use max available)

    Returns:
        (results, summary_df) where summary_df has the CSV-ready format.
    """
    required = ["benchmark", "fuzzer", "trial_id", "time", "bugs_covered"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}. Have: {list(df.columns)}")

    df = df.copy()
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df["bugs_covered"] = pd.to_numeric(df["bugs_covered"], errors="coerce")
    df = df.dropna(subset=["time", "bugs_covered", "benchmark", "fuzzer", "trial_id"])

    if fuzzers:
        df = df[df["fuzzer"].isin(fuzzers)]
    if benchmarks:
        df = df[df["benchmark"].isin(benchmarks)]
    if max_time_seconds:
        df = df[df["time"] <= max_time_seconds]

    # Dedup within trial-time
    df = (
        df.groupby(["benchmark", "fuzzer", "trial_id", "time"], as_index=False)["bugs_covered"]
        .max()
        .sort_values(["benchmark", "fuzzer", "trial_id", "time"])
    )

    # Mean curve per (benchmark, fuzzer, time)
    mean_df = (
        df.groupby(["benchmark", "fuzzer", "time"], as_index=False)["bugs_covered"]
        .mean()
        .sort_values(["benchmark", "fuzzer", "time"])
    )

    # Precompute richness
    t_end_seconds = max_time_seconds or int(df["time"].max())
    t_end_h = t_end_seconds / 3600.0

    pair_info: Dict[Tuple[str, str], Dict[str, float]] = {}
    richness_by_pair: Dict[Tuple[str, str], float] = {}

    for (bench, fuzzer), g in mean_df.groupby(["benchmark", "fuzzer"], sort=True):
        g = g.sort_values("time")
        times_h = g["time"].to_numpy(dtype=float) / 3600.0
        n_h = g["bugs_covered"].to_numpy(dtype=float)
        n_end = _interp_at(times_h, n_h, t_end_h)
        rich_raw, rich_used = _richness_score(float(n_end))

        pair_info[(bench, fuzzer)] = {
            "t_end_h": t_end_h,
            "n_end": n_end,
            "rich_raw": rich_raw,
            "rich_used": rich_used,
        }
        richness_by_pair[(bench, fuzzer)] = rich_used

    start_h_by_pair = _compute_start_hours(richness_by_pair)

    # Compute split times
    results: List[PairResult] = []

    for (bench, fuzzer), g in mean_df.groupby(["benchmark", "fuzzer"], sort=True):
        g = g.sort_values("time")
        times_h = g["time"].to_numpy(dtype=float) / 3600.0
        n_h = g["bugs_covered"].to_numpy(dtype=float)

        info = pair_info[(bench, fuzzer)]
        n_end = info["n_end"]
        rich_raw = info["rich_raw"]
        rich_used = info["rich_used"]

        start_h = start_h_by_pair.get((bench, fuzzer), C.START_HOUR_MIN)
        n_trials = int(df[(df["benchmark"] == bench) & (df["fuzzer"] == fuzzer)]["trial_id"].nunique())

        thresholds = list(C.THRESHOLDS)[:C.MAX_SPLITS]

        ratios = _compute_ratio_series(times_h, n_h, t_end_h, n_end)
        zone_times, split_times = _find_zone_and_split_times(
            times_h, ratios, thresholds, start_h, C.PERSIST_HOURS,
            C.MIN_GAP_HOURS, t_end_h, C.NO_SPLIT_LAST_HOURS, n_h,
        )

        results.append(PairResult(
            benchmark=bench, fuzzer=fuzzer, n_trials=n_trials,
            t_end_h=t_end_h, bugs_end_avg=n_end,
            richness_raw=rich_raw, richness_used=rich_used,
            start_h=start_h, zone_times_h=zone_times, split_times_h=split_times,
        ))

    # Build summary DataFrame
    rows = []
    for r in results:
        row = {
            "benchmark": r.benchmark,
            "fuzzer": r.fuzzer,
            "n_trials": r.n_trials,
            "t_end_h": r.t_end_h,
            "bugs_end_avg": round(r.bugs_end_avg, 4),
            "richness_raw": round(r.richness_raw, 4),
            "richness_used": round(r.richness_used, 4),
            "start_h": round(r.start_h, 4),
        }
        for i, (z, s) in enumerate(zip(r.zone_times_h, r.split_times_h)):
            row[f"zone{i+1}_h"] = round(z, 4) if z is not None else ""
            row[f"split{i+1}_h"] = round(s, 4) if s is not None else ""
        rows.append(row)

    summary_df = pd.DataFrame(rows)
    return results, summary_df


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Compute sparsity-based split times from baseline CSV")
    parser.add_argument("--input", required=True, help="Baseline FuzzBench CSV")
    parser.add_argument("--output", default="data", help="Output directory")
    parser.add_argument("--fuzzers", nargs="*", default=None)
    parser.add_argument("--benchmarks", nargs="*", default=None)
    parser.add_argument("--max-time", type=int, default=None, help="Max time in seconds")
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    results, summary = compute_split_times(
        df, fuzzers=args.fuzzers, benchmarks=args.benchmarks,
        max_time_seconds=args.max_time,
    )

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sparsity_split_times_summary.csv"
    summary.to_csv(out_path, index=False)
    print(f"Wrote {out_path} ({len(summary)} rows)")


if __name__ == "__main__":
    main()

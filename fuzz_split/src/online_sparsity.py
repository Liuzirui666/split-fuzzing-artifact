"""Online per-trial sparsity detection (paper Appendix C.2).

Unlike offline mode which pre-computes split times from baseline data,
online mode monitors each running trial's bug discovery rate in real-time
and triggers splits dynamically.

For a running trial i with window length w:
  r_avg,i(t) = N_i(t) / (t + eps)         (running average bug rate)
  r_win,i(t) = (N_i(t) - N_i(t-w)) / w    (windowed recent bug rate)
  rho_on,i(t) = r_win,i(t) / (r_avg,i(t) + eps)  (online sparsity ratio)

Uses past persistence horizon:
  rho_H,i(t) = max over [t-H, t] of rho_on,i(u)

Zone-entry and splitting follow the same threshold/gap protocol as offline.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from . import config as C


@dataclass
class OnlineSparsityTracker:
    """Tracks sparsity for one running trial.

    Call update() periodically with the current bugs_covered count.
    Call should_split() to check if a split should be triggered.
    """
    window_hours: float = C.ONLINE_WINDOW_HOURS
    persist_hours: float = C.ONLINE_PERSIST_HOURS
    thresholds: List[float] = field(default_factory=lambda: list(C.THRESHOLDS))
    min_gap_hours: float = C.MIN_GAP_HOURS
    no_split_last_hours: float = C.NO_SPLIT_LAST_HOURS
    total_hours: float = C.DEFAULT_TOTAL_DURATION_HOURS

    # Internal state
    _history: List[Tuple[float, float]] = field(default_factory=list)  # (time_h, bugs)
    _rho_history: Deque[Tuple[float, float]] = field(default_factory=deque)  # (time_h, rho)
    _zone_entered: List[bool] = field(default_factory=list)
    _split_triggered: List[bool] = field(default_factory=list)
    _split_times: List[Optional[float]] = field(default_factory=list)
    _last_split_time: Optional[float] = None
    _current_stage: int = 0

    def __post_init__(self):
        n = len(self.thresholds)
        if not self._zone_entered:
            self._zone_entered = [False] * n
        if not self._split_triggered:
            self._split_triggered = [False] * n
        if not self._split_times:
            self._split_times = [None] * n

    def update(self, time_hours: float, bugs_covered: float) -> None:
        """Record a new measurement."""
        self._history.append((time_hours, bugs_covered))

    def _compute_rho(self, t: float, bugs: float) -> float:
        """Compute online sparsity ratio at time t."""
        eps = 1e-12
        r_avg = bugs / max(t, eps)

        # Find bugs at t - window
        t_prev = t - self.window_hours
        bugs_prev = 0.0
        if t_prev > 0 and self._history:
            for th, bh in reversed(self._history):
                if th <= t_prev:
                    bugs_prev = bh
                    break

        r_win = (bugs - bugs_prev) / max(self.window_hours, eps)
        return r_win / max(r_avg, eps)

    def _persist_rho(self, t: float) -> float:
        """Compute rho_H(t) = max over [t-H, t] of rho(u)."""
        if not self._rho_history:
            return 0.0

        cutoff = t - self.persist_hours
        max_rho = 0.0
        for rho_t, rho_val in self._rho_history:
            if rho_t >= cutoff:
                max_rho = max(max_rho, rho_val)
        return max_rho

    def check_split(self) -> Optional[float]:
        """Check if a split should happen now.

        Returns the split time (hours) if triggered, None otherwise.
        Call this after update().
        """
        if not self._history:
            return None

        t, bugs = self._history[-1]

        # Don't check too early (need at least window + persist of data)
        min_t = max(self.window_hours, self.persist_hours)
        if t < min_t:
            return None

        # No splits in the last N hours
        cutoff = self.total_hours - self.no_split_last_hours
        if t > cutoff:
            return None

        # Compute and store rho
        rho = self._compute_rho(t, bugs)
        self._rho_history.append((t, rho))

        # Trim old rho history
        trim_cutoff = t - self.persist_hours - 1.0
        while self._rho_history and self._rho_history[0][0] < trim_cutoff:
            self._rho_history.popleft()

        rho_h = self._persist_rho(t)

        # Check each threshold stage
        if self._current_stage >= len(self.thresholds):
            return None  # All stages exhausted

        theta = self.thresholds[self._current_stage]

        # Enforce min gap from last split
        if self._last_split_time is not None:
            if t < self._last_split_time + self.min_gap_hours:
                return None

        # Zone entry check
        if not self._zone_entered[self._current_stage]:
            if rho_h <= theta:
                self._zone_entered[self._current_stage] = True
                print(f"  [online-sparsity] Zone {self._current_stage + 1} entered at t={t:.2f}h "
                      f"(rho_H={rho_h:.4f} <= theta={theta})")
            return None

        # Zone entered — check for bug trigger
        if len(self._history) >= 2:
            _, prev_bugs = self._history[-2]
            if bugs > prev_bugs:
                # New bug after zone entry → SPLIT
                self._split_triggered[self._current_stage] = True
                self._split_times[self._current_stage] = t
                self._last_split_time = t
                self._current_stage += 1
                print(f"  [online-sparsity] SPLIT triggered at t={t:.2f}h "
                      f"(new bug: {prev_bugs} -> {bugs})")
                return t

        return None

    @property
    def splits_so_far(self) -> List[Optional[float]]:
        return list(self._split_times)


class OnlineMonitor:
    """Monitors a running FuzzBench experiment's stats for online split decisions.

    Polls the experiment's measurement database or stats files to get
    bugs_covered updates.
    """

    def __init__(
        self,
        experiment_filestore: Path,
        experiment_name: str,
        benchmark: str,
        fuzzer: str,
        tracker: OnlineSparsityTracker,
        poll_interval_seconds: int = 60,
    ):
        self.experiment_filestore = experiment_filestore
        self.experiment_name = experiment_name
        self.benchmark = benchmark
        self.fuzzer = fuzzer
        self.tracker = tracker
        self.poll_interval = poll_interval_seconds
        self._stop = False

    def _get_latest_bugs(self) -> Optional[Tuple[float, float]]:
        """Read latest (time_hours, bugs_covered) from experiment data.

        Reads directly from the SQLite local.db — this is the most reliable
        source since the dispatcher writes to it in real-time.
        Falls back to data.csv.gz in report_filestore.
        """
        import sqlite3

        # Primary: read from SQLite local.db
        db_path = self.experiment_filestore / "local.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                # Get latest snapshot time and count distinct crash keys up to that time
                row = conn.execute(
                    "SELECT MAX(s.time), COUNT(DISTINCT c.crash_key) "
                    "FROM snapshot s "
                    "LEFT JOIN crash c ON s.time = c.time AND s.trial_id = c.trial_id "
                    "WHERE s.trial_id IN (SELECT id FROM trial WHERE experiment = ?)",
                    (self.experiment_name,)
                ).fetchone()
                conn.close()

                if row and row[0] is not None:
                    time_h = float(row[0]) / 3600.0
                    bugs = float(row[1]) if row[1] else 0.0
                    return (time_h, bugs)
            except Exception:
                pass

        # Fallback: try data.csv.gz in report_filestore
        for suffix in ["data.csv.gz", "data.csv"]:
            # Check multiple possible locations
            for base in [
                self.experiment_filestore / self.experiment_name / "report",
                Path(str(self.experiment_filestore).replace("experiment-data", "report-data").rsplit("/", 2)[0]) / self.experiment_name,
            ]:
                data_file = base / suffix
                if data_file.exists():
                    try:
                        import pandas as pd
                        df = pd.read_csv(data_file)
                        mask = (
                            (df["benchmark"] == self.benchmark) &
                            (df["fuzzer"] == self.fuzzer)
                        )
                        sub = df[mask].sort_values("time")
                        if not sub.empty:
                            last = sub.iloc[-1]
                            return (float(last["time"]) / 3600.0, float(last["bugs_covered"]))
                    except Exception:
                        pass

        return None

    def poll_once(self) -> Optional[float]:
        """Poll once and return split time if triggered."""
        result = self._get_latest_bugs()
        if result is None:
            return None

        t_h, bugs = result
        self.tracker.update(t_h, bugs)
        return self.tracker.check_split()

    def stop(self):
        self._stop = True

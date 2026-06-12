#!/usr/bin/env python3
"""Magma + online splitting orchestrator (paper §7.4 zone-then-bug protocol).

Each trial starts as one container. Per-branch background threads poll the
Magma monitor for that branch, compute the online sparsity ratio

    rho_on(t) = r_win(t) / (r_avg(t) + eps),
    r_avg(t)  = N(t) / (t + eps),
    r_win(t)  = (N(t) - N(t - w)) / w,

and track rho_H(t) = max over [t-H, t] of rho_on(u). When rho_H(t) ≤ θ_k a
zone is entered. Inside zone k, the first subsequent time step where N(t)
strictly increases (= new canary triggered) triggers a split: the branch's
container is stopped, its queue is harvested, and B children are launched
with the queue as seed corpus, each on its own CPU core.

The trial's CPU block is laid out as a binary-tree cut on [cpu_base, cpu_base+B^K).
A branch with id like 'r01' at level 2 (K=3) owns core offset
int('01', 2) * 2^(K-2) = 1 * 2 = 2, so cpu_base + 2.

Lineage-level cap: a branch has `zones_left = K - level` splits remaining, so
no lineage exceeds K splits and all cores stay inside the trial's block.

Nosplit mode: a single container for the full duration.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FUZZER_QUEUE_PATH = {
    "afl": "findings/queue",
    "aflfast": "findings/queue",
    "aflplusplus": "findings/default/queue",
    "moptafl": "findings/queue",
    "honggfuzz": "output",
    "libfuzzer": "corpus",
}

# Where to read execs_done from inside each branch's SHARED dir.
FUZZER_STATS_FILE = {
    "afl": "findings/fuzzer_stats",
    "aflfast": "findings/fuzzer_stats",
    "aflplusplus": "findings/default/fuzzer_stats",
    "moptafl": "findings/fuzzer_stats",
    "honggfuzz": None,  # handled via log parser below
    "libfuzzer": None,  # handled via log parser below
}


def _read_execs(fuzzer: str, shared: Path) -> Optional[int]:
    """Current execs_done for this branch, or None if unavailable."""
    if fuzzer in FUZZER_STATS_FILE and FUZZER_STATS_FILE[fuzzer] is not None:
        path = shared / FUZZER_STATS_FILE[fuzzer]
        if not path.exists():
            return None
        try:
            for line in path.read_text().splitlines():
                if line.startswith("execs_done"):
                    # format: "execs_done        : 12345"
                    return int(line.split(":", 1)[1].strip())
        except Exception:
            return None
        return None
    if fuzzer == "honggfuzz":
        # Honggfuzz writes stats lines into its log (via multilog).
        # Lines look like: "Mutations/run: 2 : 14334/1454s [..]"
        # or include "iters:" or similar. Best signal: grep for 'iters:' or
        # 'Mutations'. Fallback: count files in output/ as proxy.
        log = shared / "log" / "current"
        last = None
        if log.exists():
            try:
                text = log.read_text(errors="replace")
                # Look for "Iterations:" or the final "iters:" line
                for line in reversed(text.splitlines()):
                    if "Iterations:" in line:
                        # "[*] Iterations: 123456"
                        try:
                            return int(line.split("Iterations:")[1].strip().split()[0])
                        except Exception:
                            pass
                    if "iters:" in line:
                        try:
                            # "...iters: 123456..."
                            tok = line.split("iters:")[1].strip().split()[0]
                            return int(tok.replace(",", ""))
                        except Exception:
                            pass
            except Exception:
                pass
        # Fallback: use size of output/ as a monotonic proxy for exec work.
        out_dir = shared / "output"
        if out_dir.exists():
            try:
                return sum(1 for _ in out_dir.iterdir())
            except Exception:
                pass
        return None
    if fuzzer == "libfuzzer":
        # libFuzzer prints periodic stats lines beginning with the running
        # execution count, e.g. "#1024  pulse  cov: 30 ft: 45 exec/s: 512".
        # The fuzzer's stdout is captured by the entrypoint into log/current.
        log = shared / "log" / "current"
        if log.exists():
            try:
                for line in reversed(log.read_text(errors="replace").splitlines()):
                    s = line.strip()
                    if s.startswith("#"):
                        # fork mode prints "#11570: cov: ...": strip the colon
                        tok = s[1:].split(None, 1)[0].rstrip(":")
                        if tok.isdigit():
                            return int(tok)
            except Exception:
                pass
        return None
    return None


# ============================================================
# Sparsity tracker (paper §7.4 + Appendix C.2)
# ============================================================

class OnlineSparsityTracker:
    """Per-branch sparsity tracker. All times in SECONDS (not hours)."""

    def __init__(
        self,
        thresholds: List[float],
        window_sec: float,
        persist_sec: float,
        min_gap_sec: float,
        no_split_last_sec: float,
        total_sec: float,
    ):
        self.thresholds = list(thresholds)
        self.window_sec = window_sec
        self.persist_sec = persist_sec
        self.min_gap_sec = min_gap_sec
        self.no_split_last_sec = no_split_last_sec
        self.total_sec = total_sec
        self._history: List[Tuple[float, int]] = []      # (t_sec, N)
        self._rho_history: List[Tuple[float, float]] = []
        self._zone_entered = [False] * len(thresholds)
        self._last_split_time: Optional[float] = None
        self._current_stage = 0

    def update(self, t_sec: float, N: int) -> None:
        self._history.append((t_sec, N))

    def _compute_rho(self, t: float, N: int) -> float:
        eps = 1e-9
        r_avg = N / max(t, eps)
        t_prev = t - self.window_sec
        N_prev = 0
        if t_prev > 0:
            for th, nh in reversed(self._history):
                if th <= t_prev:
                    N_prev = nh
                    break
        r_win = (N - N_prev) / max(self.window_sec, eps)
        return r_win / max(r_avg, eps)

    def _persist_rho(self, t: float) -> float:
        if not self._rho_history:
            return 0.0
        cutoff = t - self.persist_sec
        return max((v for (tt, v) in self._rho_history if tt >= cutoff),
                   default=0.0)

    def check_split(self) -> bool:
        """Return True if a split should be triggered RIGHT NOW.

        Caller must have called update() with the most recent (t, N).
        """
        if not self._history:
            return False
        t, N = self._history[-1]

        if self._current_stage >= len(self.thresholds):
            return False

        # Warmup: need at least window + persist of data
        min_t = max(self.window_sec, self.persist_sec)
        if t < min_t:
            return False

        # No splits in last N seconds
        if t > self.total_sec - self.no_split_last_sec:
            return False

        # Min gap between splits
        if self._last_split_time is not None:
            if t < self._last_split_time + self.min_gap_sec:
                return False

        # Compute rho, record, compute rho_H
        rho = self._compute_rho(t, N)
        self._rho_history.append((t, rho))
        # Trim old rho_history
        self._rho_history = [
            (tt, vv) for (tt, vv) in self._rho_history
            if tt >= t - self.persist_sec - 1.0
        ]
        rho_h = self._persist_rho(t)

        theta = self.thresholds[self._current_stage]

        # Zone entry — may happen this iteration
        just_entered = False
        if not self._zone_entered[self._current_stage]:
            if rho_h <= theta:
                self._zone_entered[self._current_stage] = True
                just_entered = True

        # Not in zone yet → nothing to do
        if not self._zone_entered[self._current_stage]:
            return False

        # N at the last split (or trial start).
        ref_t = self._last_split_time or 0.0
        N_ref = 0
        for (hth, hn) in self._history:
            if hth <= ref_t:
                N_ref = hn
            else:
                break

        # Paper's strict condition is "new bug AFTER zone entry". For short
        # campaigns where all bugs arrive during warmup, that never fires.
        # We relax to: split on zone entry if any bug has been triggered
        # since the last split (or trial start), OR on a new bug while in
        # the zone. Both match the spirit of "split when we enter a sparse
        # region AND we have material to split from".
        if just_entered and N > N_ref:
            self._last_split_time = t
            self._current_stage += 1
            return True

        if not just_entered and len(self._history) >= 2:
            _, N_prev = self._history[-2]
            if N > N_prev:
                self._last_split_time = t
                self._current_stage += 1
                return True

        return False


# ============================================================
# Magma monitor reader (returns latest triggered-count)
# ============================================================

def _read_monitor_latest(shared: Path) -> Optional[Tuple[float, int]]:
    """Read the latest monitor/<counter> file.

    Returns (t_sec, N) where N is the count of distinct bug IDs with _T > 0,
    or None if no monitor files yet.
    """
    mon = shared / "monitor"
    if not mon.exists():
        return None
    files = []
    for f in mon.iterdir():
        if f.name.isdigit():
            files.append((int(f.name), f))
    if not files:
        return None
    files.sort()
    t_sec, path = files[-1]
    try:
        text = path.read_text()
    except Exception:
        return None
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return (float(t_sec), 0)
    header = lines[0].split(",")
    data = lines[1].split(",")
    if len(header) != len(data):
        return (float(t_sec), 0)
    n_unique = 0
    for h, d in zip(header, data):
        if h.endswith("_T"):
            try:
                if int(d) > 0:
                    n_unique += 1
            except ValueError:
                pass
    return (float(t_sec), n_unique)


def _read_coverage(fuzzer: str, shared: Path) -> Optional[float]:
    """Final edge coverage for a branch, in each fuzzer's NATIVE metric (so
    online-vs-nosplit is comparable within a fuzzer). afl-family -> fuzzer_stats
    'bitmap_cvg' (percent of the AFL bitmap); honggfuzz -> log
    'branch_coverage_percent'; libfuzzer -> log 'cov:' (SanitizerCoverage edge
    count, captured now that run.sh appends 2>&1). None if not yet available."""
    if fuzzer in FUZZER_STATS_FILE and FUZZER_STATS_FILE[fuzzer] is not None:
        path = shared / FUZZER_STATS_FILE[fuzzer]
        if not path.exists():
            return None
        try:
            for line in path.read_text().splitlines():
                if line.startswith("bitmap_cvg"):
                    return float(line.split(":", 1)[1].strip().rstrip("%"))
        except Exception:
            return None
        return None
    log = shared / "log" / "current"
    if not log.exists():
        return None
    try:
        lines = log.read_text(errors="replace").splitlines()
    except Exception:
        return None
    if fuzzer == "honggfuzz":
        for line in reversed(lines):
            if "branch_coverage_percent:" in line:
                try:
                    return float(line.split("branch_coverage_percent:")[1].split()[0])
                except Exception:
                    pass
        return None
    if fuzzer == "libfuzzer":
        for line in reversed(lines):
            if "cov:" in line:
                try:
                    return float(line.split("cov:")[1].split()[0])
                except Exception:
                    pass
        return None
    return None


# ============================================================
# Docker container launch + lifecycle helpers
# ============================================================

def launch_container(
    fuzzer: str, target: str, program: str, args_str: str,
    timeout_seconds: int, shared: Path, affinity: str,
    seed_corpus: Optional[Path] = None,
    poll_seconds: int = 60,
) -> str:
    image = f"magma/{fuzzer}/{target}"
    shared = Path(shared).resolve()
    shared.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(shared, 0o777)
    except Exception:
        pass

    timeout_str = f"{int(timeout_seconds)}s"

    cmd = [
        "docker", "run", "-d", "--rm",
        "--cap-add=SYS_PTRACE",
        f"--cpuset-cpus={affinity}",
        f"--env=PROGRAM={program}",
        f"--env=ARGS={args_str}",
        f"--env=POLL={poll_seconds}",
        f"--env=TIMEOUT={timeout_str}",
        f"--env=AFFINITY={affinity}",
        "--network=none",
        "-v", f"{shared}:/magma_shared",
    ]
    # Hard per-container memory cap (safety net: NO single fuzzer/benchmark may
    # consume all host RAM and reboot the box). env CONTAINER_MEM_GB = GB limit.
    # memory-swap == memory  =>  container may NOT spill to swap. Unset => no cap (legacy behavior).
    _mem_gb = os.environ.get("CONTAINER_MEM_GB", "").strip()
    if _mem_gb:
        cmd += [f"--memory={_mem_gb}g", f"--memory-swap={_mem_gb}g"]
    # Pass through extra honggfuzz args (e.g. -N <iters> to restart the persistent
    # worker periodically and bound RSS growth on leaky harnesses like libpng).
    _fuzzargs = os.environ.get("FUZZARGS", "").strip()
    if _fuzzargs:
        cmd += [f"--env=FUZZARGS={_fuzzargs}"]
    # Overlay corrected run.sh for the fuzzers we patched (avoids rebuilding the
    # images; content is identical to a rebuild from source):
    #   libfuzzer: writes its evolving corpus to $SHARED/corpus for harvest.
    #   honggfuzz: adds -s (stdin) for non-persistent empty-ARGS targets (lua).
    #   moptafl:   adds -t 1000+ so a transient slow seed is skipped (not a fatal
    #              dry-run abort) on heavy targets like poppler.
    if fuzzer in ("libfuzzer", "honggfuzz", "moptafl"):
        run_sh = (Path(__file__).resolve().parent.parent
                  / "magma" / "fuzzers" / fuzzer / "run.sh")
        try:
            os.chmod(run_sh, 0o755)
        except Exception:
            pass
        cmd += ["-v", f"{run_sh}:/magma/fuzzers/{fuzzer}/run.sh:ro"]
    if seed_corpus is not None:
        sc = Path(seed_corpus).resolve()
        try:
            os.chmod(sc, 0o755)
            for f in sc.iterdir():
                if f.is_file():
                    os.chmod(f, 0o644)
        except Exception:
            pass
        mount_target = f"/magma/targets/{target}/corpus/{program}"
        cmd += ["-v", f"{sc}:{mount_target}:ro"]
    cmd.append(image)

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"docker run failed: {r.stderr}")
    return r.stdout.strip()[:12]


def stop_container(cid: str, timeout_s: int = 10) -> None:
    """SIGTERM, then SIGKILL after timeout. Idempotent."""
    subprocess.run(["docker", "stop", "--time", str(timeout_s), cid],
                   capture_output=True, timeout=timeout_s + 5)


def wait_container(cid: str, logfile: Path) -> int:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "wb") as f:
        try:
            lp = subprocess.Popen(["docker", "logs", "-f", cid],
                                  stdout=f, stderr=subprocess.STDOUT)
        except Exception:
            lp = None
        r = subprocess.run(["docker", "wait", cid],
                           capture_output=True, text=True)
        if lp is not None:
            try:
                lp.wait(timeout=15)
            except Exception:
                lp.kill()
    try:
        return int(r.stdout.strip() or 1)
    except ValueError:
        return 1


# ============================================================
# CPU allocation within a trial
# ============================================================

def branch_cpu(bid: str, level: int, cpu_base: int, K: int) -> int:
    """Core for branch `bid` at `level` inside a trial whose block starts at
    cpu_base, covers 2^K cores.

    bid is 'r' (root) or 'r<bits>' where <bits> is a K-bit path from the root.
    A branch at level L owns 2^(K-L) cores; its single running core is the
    first one in its owned block.
    """
    suffix = bid[1:] if bid.startswith("r") else bid
    if suffix == "":
        return cpu_base
    idx = int(suffix, 2)
    span = 2 ** (K - level)
    return cpu_base + idx * span


# ============================================================
# Queue harvest
# ============================================================

def harvest_queue(fuzzer: str, src_shared: Path, dst_seeds: Path) -> int:
    q = src_shared / FUZZER_QUEUE_PATH[fuzzer]
    if not q.exists():
        return 0
    dst_seeds.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in q.iterdir():
        if f.is_file():
            try:
                shutil.copy2(f, dst_seeds / f.name)
                n += 1
            except Exception:
                pass
    return n


# ============================================================
# Online trial runner
# ============================================================

@dataclass
class TrialConfig:
    fuzzer: str
    target: str
    program: str
    args_str: str
    total_sec: int
    trial_workdir: Path
    cpu_base: int
    K: int                       # max splits in any lineage
    branching: int
    thresholds: List[float]
    window_sec: float
    persist_sec: float
    min_gap_sec: float
    no_split_last_sec: float
    poll_sec: int = 30


def _branch_run(cfg: TrialConfig, level: int, bid: str,
                seed_corpus: Optional[Path],
                trial_start_wall: float,
                zones_left: int) -> bool:
    """Run one branch: launch container, poll, maybe split, wait.

    Returns True on clean finish.
    """
    shared = cfg.trial_workdir / f"L{level}" / bid
    shared.mkdir(parents=True, exist_ok=True)
    cpu = branch_cpu(bid, level, cfg.cpu_base, cfg.K)
    log = cfg.trial_workdir / f"L{level}_{bid}.log"

    # Remaining budget for THIS branch = total_sec - (now - trial_start)
    elapsed = time.time() - trial_start_wall
    branch_budget = max(1, int(cfg.total_sec - elapsed))

    try:
        cid = launch_container(
            cfg.fuzzer, cfg.target, cfg.program, cfg.args_str,
            branch_budget, shared, str(cpu),
            seed_corpus=seed_corpus,
        )
    except Exception as e:
        print(f"[t{cfg.trial_workdir.name} L{level} {bid}] launch failed: {e}", flush=True)
        return False

    tracker: Optional[OnlineSparsityTracker] = None
    if zones_left > 0:
        tracker = OnlineSparsityTracker(
            thresholds=cfg.thresholds[-zones_left:],  # next `zones_left` thresholds
            window_sec=cfg.window_sec,
            persist_sec=cfg.persist_sec,
            min_gap_sec=cfg.min_gap_sec,
            no_split_last_sec=cfg.no_split_last_sec,
            total_sec=cfg.total_sec,
        )

    branch_start = time.time()
    split_decided = False

    while not split_decided:
        time.sleep(cfg.poll_sec)

        # Check container is still alive
        alive = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"id={cid}"],
            capture_output=True, text=True,
        ).stdout.strip()
        if not alive:
            break

        # Compute time since trial start (for sparsity calc)
        t_sec = time.time() - trial_start_wall

        # End of budget? Let container finish naturally.
        if t_sec >= cfg.total_sec - cfg.no_split_last_sec:
            # Still snapshot execs even if we no longer check for splits.
            execs = _read_execs(cfg.fuzzer, shared)
            if execs is not None:
                latest = _read_monitor_latest(shared)
                N_now = latest[1] if latest is not None else 0
                try:
                    with open(shared / "execs.log", "a") as f:
                        f.write(f"{int(t_sec)} {N_now} {execs}\n")
                except Exception:
                    pass
            continue

        if tracker is None:
            # Still snapshot execs in zone-exhausted branches.
            execs = _read_execs(cfg.fuzzer, shared)
            if execs is not None:
                latest = _read_monitor_latest(shared)
                N_now = latest[1] if latest is not None else 0
                try:
                    with open(shared / "execs.log", "a") as f:
                        f.write(f"{int(t_sec)} {N_now} {execs}\n")
                except Exception:
                    pass
            continue

        latest = _read_monitor_latest(shared)
        if latest is None:
            continue
        _, N = latest

        # Record execs snapshot (fuzzer-specific). Appends to execs.log as
        # "t_sec_global N_unique_bugs_triggered execs_done".
        execs = _read_execs(cfg.fuzzer, shared)
        if execs is not None:
            try:
                with open(shared / "execs.log", "a") as f:
                    f.write(f"{int(t_sec)} {N} {execs}\n")
            except Exception:
                pass

        tracker.update(t_sec, N)
        if tracker.check_split():
            split_decided = True
            print(f"[t{cfg.trial_workdir.name} L{level} {bid}] "
                  f"SPLIT at t={t_sec:.0f}s (N={N}, execs={execs})", flush=True)
            break

    if not split_decided:
        # Wait for natural finish. Magma's entrypoint exits non-zero when
        # the timeout fires even on a clean run, so rc is not a reliable
        # success signal — treat a branch that produced any monitor output
        # as successful.
        _ = wait_container(cid, log)
        mon = shared / "monitor"
        return mon.exists() and any(f.name.isdigit() for f in mon.iterdir())

    # Split path: stop container, harvest queue, spawn children
    stop_container(cid)
    _ = wait_container(cid, log)   # capture logs

    seeds_dir = cfg.trial_workdir / f"L{level+1}_seeds" / bid
    n_seeds = harvest_queue(cfg.fuzzer, shared, seeds_dir)
    if n_seeds == 0:
        print(f"[t{cfg.trial_workdir.name} L{level+1} {bid}*] "
              f"no queue to inherit — not splitting", flush=True)
        return False

    children_ids = [f"{bid}{i}" for i in range(cfg.branching)]
    child_zones = max(0, zones_left - 1)

    results = []
    with ThreadPoolExecutor(max_workers=len(children_ids)) as ex:
        futs = [
            ex.submit(_branch_run, cfg, level + 1, cbid, seeds_dir,
                      trial_start_wall, child_zones)
            for cbid in children_ids
        ]
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                print(f"child exception: {e}", flush=True)
                results.append(False)

    # A successfully-split parent is itself a success if at least one child
    # produced monitor output. We also counted the parent's own monitor above.
    return len(results) > 0 and any(results)


def run_online_trial(cfg: TrialConfig) -> bool:
    trial_start_wall = time.time()
    return _branch_run(cfg, 0, "r", None, trial_start_wall, cfg.K)


def run_nosplit_trial(cfg: TrialConfig) -> bool:
    """Simple single-container run for full duration with execs snapshots."""
    shared = cfg.trial_workdir / "S"
    shared.mkdir(parents=True, exist_ok=True)
    cpu = cfg.cpu_base
    try:
        cid = launch_container(
            cfg.fuzzer, cfg.target, cfg.program, cfg.args_str,
            cfg.total_sec, shared, str(cpu),
        )
    except Exception as e:
        print(f"[{cfg.trial_workdir.name}/S] launch failed: {e}", flush=True)
        return False
    log = cfg.trial_workdir / "campaign.log"

    # Poller thread for execs snapshots
    start = time.time()
    def _poller():
        while True:
            time.sleep(cfg.poll_sec)
            alive = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"id={cid}"],
                capture_output=True, text=True,
            ).stdout.strip()
            if not alive:
                return
            t_sec = time.time() - start
            latest = _read_monitor_latest(shared)
            N = latest[1] if latest is not None else 0
            execs = _read_execs(cfg.fuzzer, shared)
            if execs is not None:
                try:
                    with open(shared / "execs.log", "a") as f:
                        f.write(f"{int(t_sec)} {N} {execs}\n")
                except Exception:
                    pass

    poller = threading.Thread(target=_poller, daemon=True)
    poller.start()

    _ = wait_container(cid, log)
    mon = shared / "monitor"
    return mon.exists() and any(f.name.isdigit() for f in mon.iterdir())


# ============================================================
# CLI / main
# ============================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fuzzer", required=True, choices=list(FUZZER_QUEUE_PATH))
    p.add_argument("--target", required=True)
    p.add_argument("--program", required=True)
    p.add_argument("--args", default="")
    p.add_argument("--mode", choices=["online", "nosplit"], required=True)
    p.add_argument("--total-hours", type=float, required=True)
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--workdir", required=True)
    p.add_argument("--cpu-base", type=int, default=0)
    p.add_argument("--branching", type=int, default=2)
    p.add_argument("--max-splits", type=int, default=3,
                   help="K: max splits in any lineage")
    # Sparsity params in HOURS for convenience
    p.add_argument("--thresholds", type=float, nargs="*",
                   default=[0.5, 0.3, 0.15])
    p.add_argument("--window-hours", type=float, default=0.25)
    p.add_argument("--persist-hours", type=float, default=0.167)
    p.add_argument("--min-gap-hours", type=float, default=0.333)
    p.add_argument("--no-split-last-hours", type=float, default=0.333)
    p.add_argument("--poll-sec", type=int, default=30)
    args = p.parse_args()

    # lua is excluded from campaigns. Exit cleanly
    # (rc=0) so the orchestrator marks the campaign done and moves on with no
    # retry. No-op for every other target; container launch is below this point.
    if args.target == "lua":
        print("lua is excluded from campaigns; skipping.",
              flush=True)
        raise SystemExit(0)

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    total_sec = int(args.total_hours * 3600)
    K = args.max_splits if args.mode == "online" else 0
    cpus_per_trial = (args.branching ** K) if args.mode == "online" else 1

    def _run_trial(trial_idx: int) -> bool:
        trial_workdir = workdir / f"trial-{trial_idx}"
        cpu_base = args.cpu_base + trial_idx * cpus_per_trial
        cfg = TrialConfig(
            fuzzer=args.fuzzer, target=args.target, program=args.program,
            args_str=args.args, total_sec=total_sec,
            trial_workdir=trial_workdir, cpu_base=cpu_base,
            K=K, branching=args.branching,
            thresholds=list(args.thresholds[:K]),
            window_sec=args.window_hours * 3600,
            persist_sec=args.persist_hours * 3600,
            min_gap_sec=args.min_gap_hours * 3600,
            no_split_last_sec=args.no_split_last_hours * 3600,
            poll_sec=args.poll_sec,
        )
        if args.mode == "nosplit":
            return run_nosplit_trial(cfg)
        return run_online_trial(cfg)

    print(f"=== {args.fuzzer}/{args.target}/{args.program} mode={args.mode} "
          f"trials={args.trials} T={args.total_hours}h K={K} ===", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.trials) as ex:
        futs = {ex.submit(_run_trial, i): i for i in range(args.trials)}
        oks = 0
        for f in as_completed(futs):
            i = futs[f]
            try:
                ok = f.result()
            except Exception as e:
                print(f"  trial {i}: EXCEPTION {e}", flush=True)
                ok = False
            print(f"  trial {i}: {'OK' if ok else 'FAIL'}", flush=True)
            if ok:
                oks += 1
    dur = time.time() - t0
    print(f"=== done: {oks}/{args.trials} trials OK in {dur:.0f}s ===", flush=True)
    sys.exit(0 if oks == args.trials else 1)


if __name__ == "__main__":
    main()

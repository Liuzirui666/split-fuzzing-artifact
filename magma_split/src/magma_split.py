#!/usr/bin/env python3
"""Magma + splitting orchestrator.

Runs Magma fuzzing campaigns (4 supported fuzzers x targets) with optional
splitting. Each trial either runs one container for T hours (nosplit), or
runs K+1 stages where each stage spawns B children that inherit the parent
stage's queue as their seed corpus (split).

Per branch a fresh $SHARED dir is created so canary storage is independent.
Post-processing stitches per-branch monitor CSVs into global counts.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

# Where each fuzzer writes its working queue (from which we harvest seeds for
# child branches). Honggfuzz uses --output for new interesting inputs.
FUZZER_QUEUE_PATH = {
    "afl": "findings/queue",
    "aflfast": "findings/queue",
    "aflplusplus": "findings/default/queue",
    "moptafl": "findings/queue",
    "honggfuzz": "output",
}


def launch_container(
    fuzzer: str,
    target: str,
    program: str,
    args_str: str,
    timeout_seconds: int,
    shared: Path,
    affinity: str,
    seed_corpus: Optional[Path] = None,
    poll_seconds: int = 60,
) -> str:
    image = f"magma/{fuzzer}/{target}"
    shared = Path(shared).resolve()
    shared.mkdir(parents=True, exist_ok=True)
    os.chmod(shared, 0o777)

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
    if seed_corpus is not None:
        sc = Path(seed_corpus).resolve()
        # Make sure container (uid 1000) can read
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

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker run failed: {result.stderr}")
    container_id = result.stdout.strip()[:12]
    return container_id


def wait_container(container_id: str, logfile: Path) -> int:
    """Block until container finishes. Stream logs to logfile. Return exit code."""
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "wb") as f:
        log_proc = subprocess.Popen(
            ["docker", "logs", "-f", container_id],
            stdout=f, stderr=subprocess.STDOUT,
        )
        result = subprocess.run(
            ["docker", "wait", container_id],
            capture_output=True, text=True,
        )
        try:
            log_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log_proc.kill()
    try:
        return int(result.stdout.strip() or 1)
    except ValueError:
        return 1


def _extract_queue(fuzzer: str, shared: Path, dest: Path) -> int:
    """Copy seed files from a completed campaign's queue into dest.
    Returns number of files copied."""
    queue_src = shared / FUZZER_QUEUE_PATH[fuzzer]
    if not queue_src.exists():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in queue_src.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
            n += 1
    return n


def run_nosplit(
    fuzzer: str, target: str, program: str, args_str: str,
    total_seconds: int, trial_workdir: Path, affinity: str,
) -> bool:
    shared = trial_workdir / "S"
    shared.mkdir(parents=True, exist_ok=True)
    try:
        cid = launch_container(
            fuzzer, target, program, args_str,
            total_seconds, shared, affinity,
        )
    except Exception as e:
        print(f"[{trial_workdir.name}] launch failed: {e}")
        return False
    log = trial_workdir / "campaign.log"
    rc = wait_container(cid, log)
    return rc == 0


def run_split(
    fuzzer: str, target: str, program: str, args_str: str,
    stage_seconds: List[int],
    trial_workdir: Path, cpu_base: int, branching: int,
) -> bool:
    """Run one split trial. stage_seconds has len K+1 (stages 0..K)."""
    # Level 0: single root branch
    L0_shared = trial_workdir / "L0" / "r"
    L0_affinity = str(cpu_base)
    try:
        cid = launch_container(
            fuzzer, target, program, args_str,
            stage_seconds[0], L0_shared, L0_affinity,
        )
    except Exception as e:
        print(f"[{trial_workdir.name}/L0] launch failed: {e}")
        return False
    rc = wait_container(cid, trial_workdir / "L0_r.log")
    if rc != 0:
        print(f"[{trial_workdir.name}/L0] rc={rc}, continuing")

    current = [("r", L0_shared)]

    for level in range(1, len(stage_seconds)):
        next_level = []
        # Plan children
        child_jobs = []
        for parent_bid, parent_shared in current:
            seeds_dir = trial_workdir / f"L{level}_seeds" / parent_bid
            n = _extract_queue(fuzzer, parent_shared, seeds_dir)
            if n == 0:
                print(f"[{trial_workdir.name}/L{level}] no queue from parent {parent_bid}")
                continue
            for i in range(branching):
                child_bid = f"{parent_bid}{i}"
                child_shared = trial_workdir / f"L{level}" / child_bid
                child_jobs.append((child_bid, child_shared, seeds_dir))

        if not child_jobs:
            return False

        # Assign CPUs and launch all in parallel
        def _work(idx_bid_shared_seeds):
            idx, (bid, shared, seeds) = idx_bid_shared_seeds
            aff = str(cpu_base + idx)
            try:
                cid = launch_container(
                    fuzzer, target, program, args_str,
                    stage_seconds[level], shared, aff, seed_corpus=seeds,
                )
            except Exception as e:
                print(f"[{trial_workdir.name}/L{level}/{bid}] launch failed: {e}")
                return (bid, shared, False)
            log = trial_workdir / f"L{level}_{bid}.log"
            rc = wait_container(cid, log)
            return (bid, shared, rc == 0)

        results = []
        with ThreadPoolExecutor(max_workers=len(child_jobs)) as ex:
            futures = [ex.submit(_work, (i, j)) for i, j in enumerate(child_jobs)]
            for fut in as_completed(futures):
                results.append(fut.result())

        for bid, shared, ok in results:
            if not ok:
                print(f"[{trial_workdir.name}/L{level}/{bid}] failed")
            next_level.append((bid, shared))
        current = next_level

    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fuzzer", required=True, choices=list(FUZZER_QUEUE_PATH))
    p.add_argument("--target", required=True)
    p.add_argument("--program", required=True)
    p.add_argument("--args", default="")
    p.add_argument("--mode", choices=["split", "nosplit"], required=True)
    p.add_argument("--total-hours", type=float, required=True)
    p.add_argument("--split-hours", nargs="*", type=float, default=[],
                   help="Split times in hours from t=0 (e.g. 0.5 for K=1 split at 30min).")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--workdir", required=True)
    p.add_argument("--cpu-base", type=int, default=0,
                   help="First CPU to use; trials/branches consume consecutive CPUs from here")
    p.add_argument("--branching", type=int, default=2)
    args = p.parse_args()

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    total_sec = int(args.total_hours * 3600)

    if args.mode == "split":
        K = len(args.split_hours)
        if K == 0:
            print("--split-hours required for mode=split")
            sys.exit(2)
        sp = sorted(args.split_hours)
        stage_sec = []
        prev = 0.0
        for s in sp:
            stage_sec.append(int((s - prev) * 3600))
            prev = s
        stage_sec.append(int((args.total_hours - prev) * 3600))
        cpus_per_trial = args.branching ** K
    else:
        cpus_per_trial = 1
        stage_sec = None

    def _run_trial(trial_idx):
        trial_workdir = workdir / f"trial-{trial_idx}"
        trial_cpu_base = args.cpu_base + trial_idx * cpus_per_trial
        if args.mode == "nosplit":
            return run_nosplit(
                args.fuzzer, args.target, args.program, args.args,
                total_sec, trial_workdir, str(trial_cpu_base),
            )
        else:
            return run_split(
                args.fuzzer, args.target, args.program, args.args,
                stage_sec, trial_workdir, trial_cpu_base, args.branching,
            )

    print(f"=== {args.fuzzer}/{args.target}/{args.program} mode={args.mode} "
          f"trials={args.trials} T={args.total_hours}h ===")
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.trials) as ex:
        futs = {ex.submit(_run_trial, i): i for i in range(args.trials)}
        oks = 0
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                ok = fut.result()
            except Exception as e:
                print(f"  trial {i}: EXCEPTION {e}")
                ok = False
            print(f"  trial {i}: {'OK' if ok else 'FAIL'}")
            if ok:
                oks += 1

    dur = time.time() - start
    print(f"=== done: {oks}/{args.trials} trials OK in {dur:.0f}s ===")
    sys.exit(0 if oks == args.trials else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Per-branch PoC (proof-of-vulnerability) extraction = Magma's 'Detected' metric.

For each branch in a finished campaign, this script:
  1. Enumerates crashing inputs the fuzzer saved (via each fuzzer's findings
     convention -- matches magma/fuzzers/<name>/findings.sh).
  2. Spawns a docker container from the matching magma image and replays each
     crash through magma/runonce.sh, which runs it under the canary-instrumented
     binary with a fresh storage file and reports which bug (if any) triggered.
  3. Writes a CSV: per (fuzzer, target, mode, trial, branch, crash_file), the
     bug_id the crash triggers on replay (or NEW if it crashes but no known
     canary fired).

This is the workflow implemented by magma/tools/captain/extract.sh, but adapted
to our per-branch directory structure.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
from pathlib import Path
from typing import List, Tuple

FUZZER_CRASH_PATHS = {
    "afl": ["findings/crashes"],
    "aflfast": ["findings/crashes"],
    "aflplusplus": ["findings/default/crashes"],
    "moptafl": ["findings/crashes"],
    "honggfuzz": ["findings"],
}

FUZZER_CRASH_PATTERN = {
    "afl": ["id:*"],
    "aflfast": ["id:*"],
    "aflplusplus": ["id:*"],
    "moptafl": ["id:*"],
    "honggfuzz": ["*.fuzz"],
}


def list_crash_files(fuzzer: str, shared: Path) -> List[Path]:
    dirs = [shared / d for d in FUZZER_CRASH_PATHS[fuzzer]]
    patterns = FUZZER_CRASH_PATTERN[fuzzer]
    out = []
    for d in dirs:
        if not d.exists():
            continue
        for pat in patterns:
            for p in d.rglob(pat):
                if p.is_file():
                    out.append(p)
    return out


def replay_one(
    fuzzer: str, target: str, program: str, args: str,
    crash_file: Path, container_name: str = None,
) -> Tuple[int, str]:
    """Run one crash file through magma/runonce.sh inside a fresh container.
    Returns (exit_code_from_runonce, bug_id_or_empty_or_NEW)."""
    image = f"magma/{fuzzer}/{target}"
    # Mount the crash file
    crash_abs = str(crash_file.resolve())
    # Each replay uses a temporary SHARED dir so canaries.raw is fresh
    import tempfile
    tmp = tempfile.mkdtemp(prefix="magma_replay_")
    os.chmod(tmp, 0o777)
    try:
        cmd = [
            "docker", "run", "--rm",
            "--cap-add=SYS_PTRACE",
            f"--env=PROGRAM={program}",
            f"--env=ARGS={args}",
            "--network=none",
            "-v", f"{tmp}:/magma_shared",
            "-v", f"{crash_abs}:/crash.in:ro",
            "--entrypoint", "bash",
            image, "-c",
            '"$MAGMA"/runonce.sh /crash.in',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # runonce output format: "exit_code N [bug X]"
        out = (r.stdout + r.stderr).strip().splitlines()
        last = out[-1] if out else ""
        bug = ""
        if "bug" in last:
            parts = last.split()
            for i, tok in enumerate(parts):
                if tok == "bug" and i + 1 < len(parts):
                    bug = parts[i + 1]
                    break
        return (r.returncode, bug)
    except subprocess.TimeoutExpired:
        return (124, "TIMEOUT")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def walk_experiment(workdir: Path, fuzzer: str, target: str, program: str, mode: str):
    for trial_dir in sorted(workdir.glob("trial-*")):
        trial_id = trial_dir.name
        if mode == "nosplit":
            s = trial_dir / "S"
            if s.exists():
                yield (trial_id, 0, "r", s)
        else:
            for level_dir in sorted(trial_dir.glob("L[0-9]*")):
                if "_seeds" in level_dir.name:
                    continue
                try:
                    level = int(level_dir.name[1:])
                except ValueError:
                    continue
                for bdir in sorted(level_dir.iterdir()):
                    if bdir.is_dir():
                        yield (trial_id, level, bdir.name, bdir)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workdir", required=True)
    p.add_argument("--fuzzer", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--program", required=True)
    p.add_argument("--args", default="")
    p.add_argument("--mode", choices=["split", "nosplit", "online"], required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--parallel", type=int, default=8)
    args = p.parse_args()

    workdir = Path(args.workdir).resolve()
    rows = []

    # Flatten tasks
    tasks = []
    for trial_id, level, branch_id, bdir in walk_experiment(
        workdir, args.fuzzer, args.target, args.program, args.mode
    ):
        for cf in list_crash_files(args.fuzzer, bdir):
            tasks.append((trial_id, level, branch_id, cf))

    print(f"{args.fuzzer}/{args.target}/{args.mode}: {len(tasks)} crash files to replay")

    if not tasks:
        with open(args.output, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["fuzzer", "target", "mode", "trial_id", "level",
                        "branch_id", "crash_file", "exit_code", "bug_id"])
        print(f"wrote 0 rows to {args.output}")
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _work(item):
        tid, lvl, bid, cf = item
        ec, bug = replay_one(args.fuzzer, args.target, args.program, args.args, cf)
        return (tid, lvl, bid, cf, ec, bug)

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(_work, t) for t in tasks]
        results = []
        for i, fut in enumerate(as_completed(futs)):
            results.append(fut.result())
            if (i + 1) % 20 == 0:
                print(f"  {i+1}/{len(tasks)} replayed")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fuzzer", "target", "mode", "trial_id", "level",
                    "branch_id", "crash_file", "exit_code", "bug_id"])
        for (tid, lvl, bid, cf, ec, bug) in sorted(results):
            w.writerow([args.fuzzer, args.target, args.mode,
                        tid, lvl, bid, str(cf), ec, bug])

    # Summary: unique bugs detected per (trial, mode)
    detected = {}
    for (tid, lvl, bid, cf, ec, bug) in results:
        if bug and bug != "NEW" and bug != "TIMEOUT":
            detected.setdefault(tid, set()).add(bug)
    print(f"Detected bug counts per trial:")
    for tid in sorted(detected):
        print(f"  {tid}: {sorted(detected[tid])}")
    print(f"wrote {len(results)} rows to {args.output}")


if __name__ == "__main__":
    main()

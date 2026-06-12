#!/usr/bin/env python3
"""Magma+splitting report generator.

Walks a workdir produced by magma_split.py and emits a long-format CSV:

    trial_id, mode, level, branch_id, local_time_sec, global_time_sec,
    bug_id, reached, triggered

Directory conventions (from magma_split.py):

  workdir/trial-<N>/S/                  -> nosplit, single branch (level=0, branch_id='r')
  workdir/trial-<N>/L<K>/<branch_id>/   -> split, level K, branch_id e.g. 'r', 'r0', 'r01'

Each branch directory contains:
  monitor/<counter>        CSV timestamped by seconds-since-branch-start
  findings/queue/          (AFL-family) or output/     (honggfuzz)
  canaries.raw             mmap'd canary storage
  log/current              fuzzer log

Global time reconstruction:
  level 0:  global = local
  level k:  global = sum(parent stage durations) + local

A trial's branch_id length equals its level (except level 0 = 'r').
Branch 'r01' at level 2 = parent 'r0' at level 1 = grandparent 'r' at level 0.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


def read_monitor_file(path: Path) -> Dict[str, Tuple[int, int]]:
    """Parse one Magma monitor CSV. Returns {bug_id: (reached, triggered)}."""
    try:
        lines = [ln.rstrip("\n") for ln in path.read_text().splitlines() if ln.strip()]
    except Exception:
        return {}
    if len(lines) < 2:
        return {}
    header = lines[0].split(",")
    data = lines[1].split(",")
    if len(header) != len(data) or len(header) % 2 != 0:
        return {}
    result = {}
    for i in range(0, len(header), 2):
        r_key = header[i]
        t_key = header[i + 1]
        if not r_key.endswith("_R") or not t_key.endswith("_T"):
            continue
        bug = r_key[:-2]
        if bug + "_T" != t_key:
            continue
        try:
            result[bug] = (int(data[i]), int(data[i + 1]))
        except ValueError:
            continue
    return result


def walk_branch_monitor(branch_dir: Path) -> List[Tuple[int, Dict[str, Tuple[int, int]]]]:
    """Return ordered [(local_time_sec, {bug: (r, t)}), ...]. Forward-fill to keep each bug once seen."""
    mon_dir = branch_dir / "monitor"
    if not mon_dir.exists():
        return []
    snaps = []
    last: Dict[str, Tuple[int, int]] = {}
    for f in sorted(mon_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        try:
            t = int(f.name)
        except ValueError:
            continue
        new = read_monitor_file(f)
        # forward-fill: any bug seen before but not in this file keeps its last value
        for k, v in new.items():
            last[k] = v
        # clone last for this timestamp
        snaps.append((t, dict(last)))
    return snaps


def discover_trials(workdir: Path, fuzzer: str, target: str, mode: str) -> List[dict]:
    """Discover trials and their branches inside one workdir.

    Returns a list of dicts: {trial_id, branches: [{branch_id, level, path, stage_start_sec}]}
    For nosplit, each trial has one branch at level 0, stage_start=0.
    For split, branches at level k start at the sum of their ancestor stage durations.
    """
    trials = []
    for trial_dir in sorted(workdir.glob("trial-*")):
        trial_id = trial_dir.name
        branches = []
        if mode == "nosplit":
            s_dir = trial_dir / "S"
            if s_dir.exists():
                branches.append({
                    "branch_id": "r", "level": 0, "path": s_dir, "stage_start_sec": 0,
                })
        else:  # split
            # Enumerate all L<k>/<bid>/ dirs
            for level_dir in sorted(trial_dir.glob("L*")):
                if not level_dir.is_dir() or level_dir.name.startswith("L") is False:
                    continue
                try:
                    level = int(level_dir.name[1:])
                except ValueError:
                    continue
                if "_seeds" in level_dir.name:
                    continue
                for bdir in sorted(level_dir.iterdir()):
                    if bdir.is_dir():
                        branches.append({
                            "branch_id": bdir.name,
                            "level": level,
                            "path": bdir,
                            "stage_start_sec": None,  # fill in after
                        })
            # Compute stage_start_sec per branch from its ancestors' monitor durations.
            # A branch's stage_start = parent_branch.stage_start + parent_branch.last_local_time
            by_id = {(b["level"], b["branch_id"]): b for b in branches}
            # Level 0 branch is 'r', stage_start=0
            if (0, "r") in by_id:
                by_id[(0, "r")]["stage_start_sec"] = 0
            # For higher levels, parent id = branch_id[:-1], level-1
            for level in sorted(set(b["level"] for b in branches)):
                if level == 0:
                    continue
                for b in branches:
                    if b["level"] != level:
                        continue
                    parent_bid = b["branch_id"][:-1]
                    parent_key = (level - 1, parent_bid)
                    if parent_key not in by_id:
                        b["stage_start_sec"] = 0
                        continue
                    parent = by_id[parent_key]
                    # parent's final local time = last monitor timestamp
                    parent_snaps = walk_branch_monitor(parent["path"])
                    if parent_snaps:
                        parent_end = parent_snaps[-1][0]
                    else:
                        parent_end = 0
                    if parent["stage_start_sec"] is None:
                        parent["stage_start_sec"] = 0
                    b["stage_start_sec"] = parent["stage_start_sec"] + parent_end

        if branches:
            trials.append({"trial_id": trial_id, "branches": branches})
    return trials


def emit_rows(
    workdir: Path, fuzzer: str, target: str, mode: str, out_writer,
    experiment_tag: str = "",
) -> int:
    """Write rows to the CSV writer. Returns number of rows emitted."""
    n = 0
    trials = discover_trials(workdir, fuzzer, target, mode)
    for trial in trials:
        for b in trial["branches"]:
            snaps = walk_branch_monitor(b["path"])
            for local_t, bugs in snaps:
                global_t = b["stage_start_sec"] + local_t
                for bug_id, (reached, triggered) in bugs.items():
                    out_writer.writerow({
                        "experiment": experiment_tag,
                        "fuzzer": fuzzer, "target": target, "mode": mode,
                        "trial_id": trial["trial_id"],
                        "level": b["level"], "branch_id": b["branch_id"],
                        "local_time_sec": local_t,
                        "global_time_sec": global_t,
                        "bug_id": bug_id,
                        "reached": reached, "triggered": triggered,
                    })
                    n += 1
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workdir", required=True,
                   help="Path to a single-experiment workdir OR a parent dir with sub-experiments")
    p.add_argument("--fuzzer", help="Only used for a single workdir")
    p.add_argument("--target", help="Only used for a single workdir")
    p.add_argument("--mode", choices=["split", "nosplit", "online"], help="Only used for a single workdir")
    p.add_argument("--output", required=True, help="Output CSV path")
    p.add_argument("--multi", action="store_true",
                   help="Treat --workdir as a parent; auto-discover subdirs named <fuzzer>_<target>_<mode>")
    args = p.parse_args()

    workdir = Path(args.workdir).resolve()
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "experiment", "fuzzer", "target", "mode",
        "trial_id", "level", "branch_id",
        "local_time_sec", "global_time_sec",
        "bug_id", "reached", "triggered",
    ]

    total = 0
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        if args.multi:
            for subdir in sorted(workdir.iterdir()):
                if not subdir.is_dir():
                    continue
                name = subdir.name
                parts = name.rsplit("_", 2)
                if len(parts) != 3:
                    continue
                fuzzer, target, mode = parts[0], parts[1], parts[2]
                if mode not in ("split", "nosplit", "online"):
                    continue
                n = emit_rows(subdir, fuzzer, target, mode, w, experiment_tag=name)
                print(f"{name}: {n} rows")
                total += n
        else:
            if not (args.fuzzer and args.target and args.mode):
                p.error("Without --multi, --fuzzer/--target/--mode are required")
            n = emit_rows(workdir, args.fuzzer, args.target, args.mode, w)
            print(f"{args.fuzzer}/{args.target}/{args.mode}: {n} rows")
            total += n

    print(f"=== {total} rows written to {out_path} ===")


if __name__ == "__main__":
    main()

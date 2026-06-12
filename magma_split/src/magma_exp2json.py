#!/usr/bin/env python3
"""Emit exp2json-compatible JSON so our experiments can be fed into Magma's
own tools/benchd/survival_analysis.py unchanged.

Output schema matches Magma's exp2json.py:

    {
      "results": {
        "<fuzzer>": {
          "<target>": {
            "<program>": {
              "<trial_index>": {
                "reached": {"<bug_id>": <time_seconds>, ...},
                "triggered": {"<bug_id>": <time_seconds>, ...}
              },
              ...
            }
          }
        }
      }
    }

For split mode, we include both:
  - Per-trial-aggregated times (min across branches in the trial) under key
    "<N>" (numeric trial index), matching Magma's format exactly.
  - Per-branch times under extended keys "<N>_<branch_id>" if --per-branch is set.

Usage:
  python magma_exp2json.py --input long.csv --output summary.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="long CSV from magma_report.py")
    p.add_argument("--output", required=True, help="output JSON path")
    p.add_argument("--program", default=None,
                   help="program name to embed; if omitted, read per row (not present -> 'default')")
    args = p.parse_args()

    df = pd.read_csv(args.input)
    df["reached"] = df["reached"].astype(int)
    df["triggered"] = df["triggered"].astype(int)

    # Trial-level first-event times, aggregated across branches (min global time).
    key_cols = ["fuzzer", "target", "mode", "trial_id", "bug_id"]
    reach_times = (
        df[df["reached"] > 0].groupby(key_cols)["global_time_sec"].min().reset_index()
    )
    trig_times = (
        df[df["triggered"] > 0].groupby(key_cols)["global_time_sec"].min().reset_index()
    )

    # Nested dict tree using defaultdict so we can assign deeply.
    def nested():
        return defaultdict(nested)

    # We separate split and nosplit as different pseudo-fuzzers so the JSON
    # is consumable by survival_analysis.py which expects flat fuzzer keys.
    # Key = "<fuzzer>_<mode>"
    out = {"results": nested()}

    def trial_idx(trial_id: str) -> str:
        # magma expects integer run keys; we strip "trial-" prefix
        return trial_id.replace("trial-", "").lstrip("0") or "0"

    program = args.program or "default"

    for _, r in reach_times.iterrows():
        fkey = f"{r['fuzzer']}_{r['mode']}"
        run = trial_idx(r["trial_id"])
        node = out["results"][fkey][r["target"]][program][run]
        node.setdefault("reached", {})[r["bug_id"]] = int(r["global_time_sec"])
        node.setdefault("triggered", {})

    for _, r in trig_times.iterrows():
        fkey = f"{r['fuzzer']}_{r['mode']}"
        run = trial_idx(r["trial_id"])
        node = out["results"][fkey][r["target"]][program][run]
        node.setdefault("triggered", {})[r["bug_id"]] = int(r["global_time_sec"])
        node.setdefault("reached", {})

    # Also fill in empty trials that exist in df but had no events
    all_trials = df[["fuzzer", "target", "mode", "trial_id"]].drop_duplicates()
    for _, r in all_trials.iterrows():
        fkey = f"{r['fuzzer']}_{r['mode']}"
        run = trial_idx(r["trial_id"])
        node = out["results"][fkey][r["target"]][program][run]
        node.setdefault("reached", {})
        node.setdefault("triggered", {})

    # Convert defaultdicts to regular dicts for json
    def to_regular(d):
        if isinstance(d, defaultdict):
            d = {k: to_regular(v) for k, v in d.items()}
        elif isinstance(d, dict):
            d = {k: to_regular(v) for k, v in d.items()}
        return d

    out = to_regular(out)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)

    # Summary
    n_fuzzer_keys = len(out["results"])
    n_trials = sum(
        len(p) for fkey in out["results"].values()
        for tgt in fkey.values()
        for p in tgt.values()
    )
    n_reached = sum(
        len(r.get("reached", {})) for fkey in out["results"].values()
        for tgt in fkey.values() for p in tgt.values() for r in p.values()
    )
    n_trig = sum(
        len(r.get("triggered", {})) for fkey in out["results"].values()
        for tgt in fkey.values() for p in tgt.values() for r in p.values()
    )
    print(f"Wrote JSON to {args.output}")
    print(f"  fuzzer_mode keys: {n_fuzzer_keys}")
    print(f"  total trials: {n_trials}")
    print(f"  total reach events: {n_reached}")
    print(f"  total trigger events: {n_trig}")


if __name__ == "__main__":
    main()

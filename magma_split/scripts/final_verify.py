#!/usr/bin/env python3
"""FINAL pre-launch verification of ALL 54 fuzzer x benchmark pairs.

One container per pair, fanned out across the whole machine. A pair PASSES if it
either reaches a bug, or is genuinely fuzzing (queue/corpus grows over the run) --
the latter covers slow-but-working targets like lua. A pair that neither reaches a bug nor grows its queue is BROKEN.
Uses the production launch path (driver_for + run.sh overlays), so it exercises
exactly what the real campaign will run."""
import sys
import time
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import magma_online_split as M  # noqa: E402
from preflight_smoke import (FUZZERS, BENCH_PROG, driver_for,  # noqa: E402
                             read_monitor, count_queue)

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 900   # 15 min
CORES_PER = 3
NCORES = 186
SMOKE_DIR = ROOT / "experiments" / "final_verify"


def run_pair(idx, fuzzer, bench):
    prog, args = driver_for(fuzzer, bench)
    base = (idx * CORES_PER) % (NCORES - CORES_PER + 1)
    affinity = f"{base}-{base + CORES_PER - 1}"
    shared = SMOKE_DIR / f"{fuzzer}__{bench}"
    if shared.exists():
        shutil.rmtree(shared, ignore_errors=True)
    shared.mkdir(parents=True, exist_ok=True)
    try:
        cid = M.launch_container(fuzzer, bench, prog, args, SECS,
                                 shared, affinity, poll_seconds=20)
    except Exception as e:
        return dict(f=fuzzer, b=bench, prog=prog, verdict="LAUNCH_FAIL",
                    reached=0, q_early=0, q_late=0, err=str(e)[:70])
    time.sleep(150)
    q_early = count_queue(fuzzer, shared)
    time.sleep(SECS - 150 + 20)
    try:
        M.stop_container(cid)
    except Exception:
        pass
    time.sleep(2)
    reached, trig = read_monitor(shared)
    q_late = count_queue(fuzzer, shared)
    grew = q_late - q_early
    if reached > 0:
        verdict = "OK_BUG"
    elif grew > 3 or q_late > q_early:
        verdict = "FUZZING"     # working; slow to bug
    else:
        verdict = "BROKEN"
    return dict(f=fuzzer, b=bench, prog=prog, verdict=verdict,
                reached=reached, q_early=q_early, q_late=q_late, err="")


def main():
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    pairs = [(f, b) for b in BENCH_PROG for f in FUZZERS]
    print(f"=== FINAL VERIFY: {len(pairs)} pairs x {CORES_PER} cores "
          f"({len(pairs)*CORES_PER} cores) x {SECS}s ===", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=len(pairs)) as ex:
        futs = {ex.submit(run_pair, i, f, b): (f, b)
                for i, (f, b) in enumerate(pairs)}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"  [{r['verdict']:11}] {r['f']:12} {r['b']:11} "
                  f"reached={r['reached']:2} q {r['q_early']}->{r['q_late']} "
                  f"{r['err']}", flush=True)

    results.sort(key=lambda r: (r["b"], r["f"]))
    print("\n=== FINAL VERIFY SUMMARY (all 54) ===", flush=True)
    nbug = nfuzz = 0
    broken = []
    for r in results:
        if r["verdict"] == "OK_BUG":
            nbug += 1
        elif r["verdict"] == "FUZZING":
            nfuzz += 1
        else:
            broken.append(r)
        flag = "  <<< BROKEN" if r["verdict"] in ("BROKEN", "LAUNCH_FAIL") else ""
        print(f"  {r['f']:12} {r['b']:11} {r['verdict']:11} "
              f"reached={r['reached']:2} q={r['q_late']:6}{flag}", flush=True)
    print(f"\n  reached-bug: {nbug}/54 | fuzzing-ok(slow): {nfuzz}/54 | "
          f"BROKEN: {len(broken)}/54", flush=True)
    print(f"  ALL 54 WORKING: {'YES' if not broken else 'NO -> '+str([(r['f'],r['b']) for r in broken])}",
          flush=True)
    print("FINAL_VERIFY_DONE", flush=True)


if __name__ == "__main__":
    main()

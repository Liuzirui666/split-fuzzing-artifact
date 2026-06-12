#!/usr/bin/env python3
"""Verify a specified subset of fuzzer/benchmark combos (production launch path).
Usage: verify_subset.py <secs> <fuzzer/bench> [<fuzzer/bench> ...]
PASS = reaches a bug OR queue grows over the run."""
import sys
import time
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import magma_online_split as M  # noqa
from preflight_smoke import driver_for, read_monitor, count_queue  # noqa

SECS = int(sys.argv[1])
COMBOS = [tuple(c.split("/")) for c in sys.argv[2:]]
CORES_PER = 6
SMOKE_DIR = ROOT / "experiments" / "verify_subset"


def run(idx, fuzzer, bench):
    prog, args = driver_for(fuzzer, bench)
    base = idx * CORES_PER
    aff = f"{base}-{base + CORES_PER - 1}"
    sh = SMOKE_DIR / f"{fuzzer}__{bench}"
    if sh.exists():
        shutil.rmtree(sh, ignore_errors=True)
    sh.mkdir(parents=True, exist_ok=True)
    try:
        cid = M.launch_container(fuzzer, bench, prog, args, SECS, sh, aff,
                                 poll_seconds=20)
    except Exception as e:
        return (fuzzer, bench, prog, args, "LAUNCH_FAIL", 0, 0, 0, str(e)[:60])
    time.sleep(150)
    qe = count_queue(fuzzer, sh)
    time.sleep(SECS - 150 + 20)
    try:
        M.stop_container(cid)
    except Exception:
        pass
    time.sleep(2)
    r, t = read_monitor(sh)
    ql = count_queue(fuzzer, sh)
    v = "OK_BUG" if r > 0 else ("FUZZING" if ql - qe > 3 else "BROKEN")
    return (fuzzer, bench, prog, args, v, r, qe, ql, "")


def main():
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== VERIFY SUBSET: {COMBOS} x {SECS}s ===", flush=True)
    res = []
    with ThreadPoolExecutor(max_workers=len(COMBOS)) as ex:
        futs = {ex.submit(run, i, f, b): (f, b) for i, (f, b) in enumerate(COMBOS)}
        for fut in as_completed(futs):
            r = fut.result()
            res.append(r)
            print(f"  [{r[4]:11}] {r[0]:12} {r[1]:9} prog={r[2]:22} args='{r[3]}' "
                  f"reached={r[5]:2} q {r[6]}->{r[7]} {r[8]}", flush=True)
    broken = [r for r in res if r[4] in ("BROKEN", "LAUNCH_FAIL")]
    print(f"\n  PASS: {len(res)-len(broken)}/{len(res)} | "
          f"BROKEN: {[(r[0],r[1]) for r in broken] or 'NONE'}", flush=True)
    print("VERIFY_SUBSET_DONE", flush=True)


if __name__ == "__main__":
    main()

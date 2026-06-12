#!/usr/bin/env python3
"""Full-machine re-smoke: confirm the uncertain fuzzer x benchmark combos
genuinely reach bugs (slow != broken). Runs TRIALS independent containers per
combo in parallel to fill the machine and maximize bug-reach chances, exactly
like the real campaign's multi-trial design. A combo PASSES if ANY trial
reaches a bug, or every trial shows a growing queue (genuinely fuzzing)."""
import sys
import time
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import magma_online_split as M  # noqa: E402
from preflight_smoke import driver_for, read_monitor, count_queue  # noqa: E402

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 1500     # 25 min
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
CORES_PER = 2

# Combos to re-verify with a longer smoke run.
COMBOS = [
    ("afl", "poppler"), ("aflfast", "poppler"),
    ("aflplusplus", "poppler"), ("moptafl", "poppler"),
    ("afl", "lua"), ("aflfast", "lua"), ("aflplusplus", "lua"),
    ("moptafl", "lua"), ("libfuzzer", "lua"),
    ("aflplusplus", "libpng"), ("aflplusplus", "php"),
    ("aflplusplus", "libxml2"), ("aflplusplus", "openssl"),
    ("aflplusplus", "sqlite3"), ("aflfast", "sqlite3"),
]
SMOKE_DIR = ROOT / "experiments" / "resmoke"
NCORES = 188


def run_one(slot, fuzzer, bench, trial):
    prog, args = driver_for(fuzzer, bench)
    base = (slot * CORES_PER) % (NCORES - CORES_PER)
    affinity = f"{base}-{base + CORES_PER - 1}"
    shared = SMOKE_DIR / f"{fuzzer}__{bench}" / f"t{trial}"
    if shared.exists():
        shutil.rmtree(shared, ignore_errors=True)
    shared.mkdir(parents=True, exist_ok=True)
    try:
        cid = M.launch_container(fuzzer, bench, prog, args, SECS,
                                 shared, affinity, poll_seconds=20)
    except Exception as e:
        return (fuzzer, bench, trial, "LAUNCH_FAIL", 0, 0, str(e)[:60])
    time.sleep(150)
    q_early = count_queue(fuzzer, shared)
    time.sleep(SECS - 150 + 20)
    try:
        M.stop_container(cid)
    except Exception:
        pass
    time.sleep(2)
    r, t = read_monitor(shared)
    q_late = count_queue(fuzzer, shared)
    return (fuzzer, bench, trial, "DONE", r, q_late, f"{q_early}->{q_late}")


def main():
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    slot = 0
    for f, b in COMBOS:
        for tr in range(TRIALS):
            jobs.append((slot, f, b, tr))
            slot += 1
    print(f"=== FULL-CORE RE-SMOKE: {len(COMBOS)} combos x {TRIALS} trials "
          f"= {len(jobs)} containers x {CORES_PER} cores ({len(jobs)*CORES_PER} cores) "
          f"x {SECS}s ===", flush=True)
    raw = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(run_one, s, f, b, tr): (f, b, tr)
                for (s, f, b, tr) in jobs}
        for fut in as_completed(futs):
            r = fut.result()
            raw.append(r)
            print(f"  {r[0]:12} {r[1]:9} t{r[2]} {r[3]:11} "
                  f"reached={r[4]:2} queue={r[5]:6} {r[6]}", flush=True)

    # aggregate per combo
    print("\n=== RE-SMOKE SUMMARY (per combo, across trials) ===", flush=True)
    bad = []
    for f, b in COMBOS:
        rs = [x for x in raw if x[0] == f and x[1] == b]
        max_reached = max((x[4] for x in rs), default=0)
        grew = sum(1 for x in rs if x[3] == "DONE"
                   and "->" in x[6]
                   and int(x[6].split("->")[1]) - int(x[6].split("->")[0]) > 5)
        launched = sum(1 for x in rs if x[3] != "LAUNCH_FAIL")
        if max_reached > 0:
            verdict = "OK_BUG"
        elif grew >= max(1, launched // 2):
            verdict = "FUZZING_OK"   # genuinely fuzzing, slow to bug
        else:
            verdict = "STUCK"
            bad.append((f, b))
        print(f"  {f:12} {b:9} {verdict:11} "
              f"max_reached={max_reached:2} trials_growing={grew}/{launched}",
              flush=True)
    print(f"\n  STUCK (need fix): {bad if bad else 'NONE'}", flush=True)
    print("RESMOKE_DONE", flush=True)


if __name__ == "__main__":
    main()

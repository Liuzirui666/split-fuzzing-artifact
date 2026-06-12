#!/usr/bin/env python3
"""Pre-flight smoke: prove EVERY fuzzer x EVERY benchmark launches, fuzzes,
and reaches bugs BEFORE committing to the real multi-day campaign.

Uses the runner's real launch_container() (so the libfuzzer run.sh overlay is
exercised exactly as in production). For libfuzzer it additionally verifies the
evolving corpus lands under $SHARED and is harvestable (the split inherits it).
"""
import sys
import time
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import magma_online_split as M  # noqa: E402

FUZZERS = ["afl", "aflfast", "aflplusplus", "moptafl", "honggfuzz", "libfuzzer"]
BENCH_PROG = {
    "poppler":    "pdf_fuzzer",
    "sqlite3":    "sqlite3_fuzz",
    "openssl":    "server",
    "libsndfile": "sndfile_fuzzer",
    "libxml2":    "libxml2_xml_read_memory_fuzzer",
    "php":        "exif",
    "libtiff":    "tiffcp",
    "libpng":     "libpng_read_fuzzer",
    "lua":        "lua",
}
ARGS_BY_BENCH = {"libsndfile": "@@", "libtiff": "-M @@ tmp.out"}


def driver_for(fuzzer, bench):
    """(program, args) for a fuzzer x benchmark.

    libFuzzer harnesses read input in-memory, so they take NO @@/CLI args, and
    cannot run CLI-only programs. libtiff's most-bug driver tiffcp is CLI-only;
    libFuzzer must use the in-memory harness tiff_read_rgba_fuzzer instead.
    afl-family + honggfuzz keep the most-bug drivers (honggfuzz maps @@->___FILE___).
    """
    prog = BENCH_PROG[bench]
    args = ARGS_BY_BENCH.get(bench, "")
    if fuzzer == "libfuzzer":
        args = ""
        if bench == "libtiff":
            prog = "tiff_read_rgba_fuzzer"
    elif fuzzer == "aflplusplus" and not args and bench != "lua":
        # afl++'s persistent shmem delivery gives frozen coverage on the
        # libFuzzer-style harnesses (map stays ~2); file-input (@@) works, as it
        # does for the other AFL fuzzers. lua is a stdin interpreter (works as
        # is); libsndfile/libtiff already carry @@ via ARGS_BY_BENCH.
        args = "@@"
    return prog, args

SMOKE_SEC = int(sys.argv[1]) if len(sys.argv) > 1 else 480   # 8 min default
CORES_PER = 2
SMOKE_DIR = ROOT / "experiments" / "smoke_preflight"


def read_monitor(shared: Path):
    """Return (reached, triggered) distinct-bug counts from latest monitor row."""
    mon = shared / "monitor"
    if not mon.exists():
        return (0, 0)
    files = sorted((int(f.name), f) for f in mon.iterdir() if f.name.isdigit())
    if not files:
        return (0, 0)
    try:
        text = files[-1][1].read_text()
    except Exception:
        return (0, 0)
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return (0, 0)
    header, data = lines[0].split(","), lines[1].split(",")
    if len(header) != len(data):
        return (0, 0)
    reached = triggered = 0
    for h, d in zip(header, data):
        try:
            v = int(d)
        except ValueError:
            continue
        if h.endswith("_R") and v > 0:
            reached += 1
        elif h.endswith("_T") and v > 0:
            triggered += 1
    return (reached, triggered)


def count_queue(fuzzer: str, shared: Path) -> int:
    q = shared / M.FUZZER_QUEUE_PATH[fuzzer]
    if not q.exists():
        return 0
    return sum(1 for f in q.iterdir() if f.is_file())


def run_combo(idx, fuzzer, bench):
    prog, args = driver_for(fuzzer, bench)
    base = idx * CORES_PER
    affinity = f"{base}-{base + CORES_PER - 1}"
    shared = SMOKE_DIR / f"{fuzzer}__{bench}"
    if shared.exists():
        shutil.rmtree(shared, ignore_errors=True)
    shared.mkdir(parents=True, exist_ok=True)
    rec = {"fuzzer": fuzzer, "bench": bench, "status": "?", "launch": "",
           "reached": 0, "triggered": 0, "queue": 0, "harvest": "", "err": ""}
    try:
        cid = M.launch_container(fuzzer, bench, prog, args, SMOKE_SEC,
                                 shared, affinity, poll_seconds=15)
        rec["launch"] = "OK"
    except Exception as e:
        rec["status"] = "LAUNCH_FAIL"
        rec["err"] = str(e)[:120]
        return rec
    time.sleep(SMOKE_SEC + 25)
    try:
        M.stop_container(cid)
    except Exception:
        pass
    time.sleep(3)
    rec["reached"], rec["triggered"] = read_monitor(shared)
    rec["queue"] = count_queue(fuzzer, shared)
    if fuzzer == "libfuzzer":
        tmp = shared / "_harvest_check"
        n = M.harvest_queue("libfuzzer", shared, tmp)
        rec["harvest"] = f"{n}"
        shutil.rmtree(tmp, ignore_errors=True)
    rec["status"] = "OK" if rec["reached"] > 0 else (
        "RUNS_NO_BUG" if rec["queue"] > 0 else "DEAD")
    return rec


def main():
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    combos = [(f, b) for b in BENCH_PROG for f in FUZZERS]
    print(f"=== PRE-FLIGHT SMOKE: {len(combos)} combos x {SMOKE_SEC}s, "
          f"{CORES_PER} cores each ===", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=len(combos)) as ex:
        futs = {ex.submit(run_combo, i, f, b): (f, b)
                for i, (f, b) in enumerate(combos)}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"  [{r['status']:11}] {r['fuzzer']:12} {r['bench']:11} "
                  f"reached={r['reached']:2} trig={r['triggered']:2} "
                  f"queue={r['queue']:5} "
                  f"{'harvest='+r['harvest'] if r['harvest'] else ''} "
                  f"{r['err']}", flush=True)

    results.sort(key=lambda r: (r["bench"], r["fuzzer"]))
    print("\n=== SUMMARY (fuzzer x benchmark) ===", flush=True)
    ok = sum(1 for r in results if r["status"] == "OK")
    runs = sum(1 for r in results if r["status"] == "RUNS_NO_BUG")
    bad = [r for r in results if r["status"] in ("DEAD", "LAUNCH_FAIL")]
    for r in results:
        flag = "" if r["status"] in ("OK", "RUNS_NO_BUG") else "  <<< BROKEN"
        print(f"  {r['fuzzer']:12} {r['bench']:11} {r['status']:11} "
              f"reached={r['reached']:2} queue={r['queue']:5}"
              f"{(' harvest='+r['harvest']) if r['harvest'] else ''}{flag}",
              flush=True)
    print(f"\n  reached-bugs: {ok}/{len(results)} | "
          f"runs-no-bug-yet: {runs} | BROKEN: {len(bad)}", flush=True)
    lf = [r for r in results if r["fuzzer"] == "libfuzzer"]
    lf_harv = sum(1 for r in lf if r["harvest"] and r["harvest"].isdigit()
                  and int(r["harvest"]) > 0)
    print(f"  libfuzzer corpus-harvest works: {lf_harv}/{len(lf)} benchmarks",
          flush=True)
    if bad:
        print("\n  !!! BROKEN combos (must fix before launch):", flush=True)
        for r in bad:
            print(f"      {r['fuzzer']}/{r['bench']}: {r['status']} {r['err']}",
                  flush=True)
    print("\nPREFLIGHT_DONE", flush=True)


if __name__ == "__main__":
    main()

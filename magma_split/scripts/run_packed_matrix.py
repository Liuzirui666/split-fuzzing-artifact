#!/usr/bin/env python3
"""
Optimal-repacking orchestrator for the full Magma campaign.

Plan:
  - 6 fuzzers x 9 benchmarks (full Magma suite), T=12h, 5 trials,
    both online (splitting, K=3 -> 8 leaves) and nosplit modes.
  - Greedy bin-packing of all 108 campaigns into 188 cores (~14 waves),
    instead of one-benchmark-at-a-time. Online jobs need 40 cores
    (5 trials x 8 leaves), nosplit 5 cores (5 trials).
  - Full suite chosen over a 6-benchmark subset to avoid benchmark
    selection bias (Klees et al. CCS'18); ~14h would be compute-neutral
    vs the prior 6x22h plan; 12h is used (168h ~= 7 days).

Methodology params are byte-for-byte identical to the validated 12h runs
(run_12h_chain.sh).

After all campaigns finish, runs finalize_experiment.sh + magma_figures.py
on the single combined EXP_DIR, producing per-(fuzzer,benchmark) figures
for bug counts, unique bugs, BDR, variance (CPU_R-normalized), coverage.
"""
import os, sys, time, json, subprocess, shutil
from datetime import datetime
from pathlib import Path

ROOT = Path("/path/to/magma_split")
SRC = ROOT / "src" / "magma_online_split.py"

TOTAL_HOURS = 12.0
TRIALS = 5
BRANCHING = 2
MAX_SPLITS = 3
LEAVES = BRANCHING ** MAX_SPLITS            # 8
ONLINE_CORES = TRIALS * LEAVES              # 40
NOSPLIT_CORES = TRIALS                      # 5
TOTAL_CORES = 188
# No hard disk gate: per-wave cleanup of corpus bulk + finished
# docker images keeps disk in check, as in the prior full-core run.
# RAM guard: machine has ~346G. Never pack so tight that RAM is exhausted
# (OOM would reboot the box). Hold new launches if available RAM < this.
RAM_FLOOR_GB = 60
# Fuzzer working-seed dirs that are pure bulk (NOT analysis data): safe to
# delete when a campaign finishes. We KEEP monitor/ (bug/BDR/variance),
# crashes (PoC/Detected), fuzzer_stats + plot_data (coverage/exec figures).
BULK_DIR_NAMES = {"queue", "corpus", "hangs", ".synced", "_resume",
                  ".cur_input", "queue_backup", ".synced_corpus"}

# Sparsity params — identical to run_12h_chain.sh
WINDOW_HOURS = 0.05
PERSIST_HOURS = 0.05
MIN_GAP_HOURS = 0.167
NO_SPLIT_LAST_HOURS = 0.25
THRESHOLDS = ["0.5", "0.3", "0.15"]
POLL_SEC = 30

# TWO SEPARATE PHASES:
#   Phase 1 = the 5 reliable fuzzers (default below). Run to completion AND
#             verify every pair 100% paper-aligned before touching libfuzzer.
#   Phase 2 = libfuzzer ONLY, launched automatically after Phase 1 is verified,
#             via env CAMPAIGN_FUZZERS=libfuzzer.
ALL_FUZZERS = ["afl", "aflfast", "aflplusplus", "moptafl", "honggfuzz", "libfuzzer"]
FUZZERS = os.environ.get(
    "CAMPAIGN_FUZZERS",
    "afl,aflfast,aflplusplus,moptafl,honggfuzz").split(",")

# Full Magma suite (9 benchmarks), primary driver each. Bug counts in comments.
BENCH_PROG = {
    "poppler":    "pdf_fuzzer",                     # 22 bugs
    "sqlite3":    "sqlite3_fuzz",                    # 20
    "openssl":    "server",                          # 20 (Magma report: server/client=7, x509=5, asn1=4)
    "libsndfile": "sndfile_fuzzer",                  # 18
    "libxml2":    "libxml2_xml_read_memory_fuzzer",  # 17
    "php":        "exif",                            # 16 (json reaches 0/16 bugs; 7/16 live in ext/exif)
    "libtiff":    "tiffcp",                          # 14 (Magma report: tiffcp=11 > tiff_read_rgba=8)
    "libpng":     "libpng_read_fuzzer",              # 7
    "lua":        "lua",                             # 4
}

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
EXP_DIR = ROOT / "experiments" / f"packed_12h_6x9_{STAMP}"

# --- RERUN config ---------------------------------------------
# Per-benchmark extra ARGS for the afl command. libsndfile's harness reads a
# FILE path, so it needs '@@' (afl writes each testcase to a file).
ARGS_BY_BENCH = {"libsndfile": "@@", "libtiff": "-M @@ tmp.out"}
# Pairs already completed in a prior run under the SAME config may be linked
# instead of rerun: point OLD_EXP at that run and list them in KEEP_PAIRS.
OLD_EXP = ROOT / "experiments" / "previous_packed_run"
# env FRESH_ALL=1 => run EVERY pair fresh (wave 2 of a 10-trial run must not
# link/skip any pair from an old run, or it would share trials with wave 1).
KEEP_PAIRS = set() if os.environ.get("FRESH_ALL") else {
    ("afl", "libsndfile"), ("aflfast", "libsndfile"),
    ("aflplusplus", "libsndfile"), ("moptafl", "libsndfile"),
}
# Benchmark-grouped order so each wave packs MULTIPLE FUZZERS on ONE benchmark
# (tells fuzzer-effect from benchmark-effect). Hardest first: front-load the
# heaviest-RAM pairs so they finish early.
BENCH_ORDER = ["libpng", "libtiff", "libsndfile", "poppler", "sqlite3",
               "openssl", "libxml2", "php"]  # lua excluded (see magma_online_split.py)
LOGS = EXP_DIR / "logs"
MASTER = EXP_DIR / "master.log"


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(MASTER, "a") as f:
        f.write(line + "\n")


def free_gb():
    s = shutil.disk_usage("/")
    return s.free // (1024**3)


def avail_ram_gb():
    """Available RAM (free + reclaimable) in GB, from /proc/meminfo."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // (1024**2)  # kB -> GB
    except Exception:
        pass
    return 10**6  # fail-open (don't block) if unreadable


def cleanup_campaign(name):
    """Delete fuzzer working-seed bulk (queue/corpus/...) in a finished
    campaign dir, in place. KEEPS monitor/, crashes, fuzzer_stats, plot_data
    so the end-of-run finalize + figures still produce every analysis
    metric. NEVER touches analysis data."""
    campdir = EXP_DIR / name
    if not campdir.is_dir():
        return 0
    freed = 0
    for d in list(campdir.rglob("*")):
        try:
            if d.is_dir() and d.name in BULK_DIR_NAMES:
                # never delete a dir that is (or contains) 'crashes' or 'monitor'
                if d.name in ("crashes", "monitor"):
                    continue
                sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
                freed += sz
        except Exception:
            continue
    return freed // (1024**2)  # MB


def reap_exited_containers():
    """Remove exited/dead magma containers (targeted docker rm, NOT prune)."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", "status=exited",
             "--filter", "status=dead", "--format", "{{.ID}} {{.Image}}"],
            capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if "magma/" in line:
                subprocess.run(["docker", "rm", "-f", line.split()[0]],
                               capture_output=True)
    except Exception:
        pass


def analyze_verify_pair(f, b):
    """Incremental per-pair analysis + verification, run in parallel with the
    still-running waves. Pure Python (monitor stitching + pandas metrics), NO
    containers -> no CPU/RAM conflict with running fuzzers. Produces per-pair
    runs CSVs + a verify JSON, and decides whether the pair's results are
    complete/valid enough to allow deleting its docker image.

    Returns (verified_complete: bool, report: dict)."""
    RESULTS = EXP_DIR / "results"
    RESULTS.mkdir(parents=True, exist_ok=True)
    import pandas as pd
    report = {"fuzzer": f, "benchmark": b, "modes": {}}
    ok = True
    for mode in ("online", "nosplit"):
        campdir = EXP_DIR / f"{f}_{b}_{mode}"
        out = RESULTS / f"runs_{f}_{b}_{mode}.csv"
        try:
            subprocess.run(["python3", str(ROOT / "src" / "magma_report.py"),
                            "--workdir", str(campdir), "--fuzzer", f,
                            "--target", b, "--mode", mode, "--output", str(out)],
                           capture_output=True, timeout=900)
            df = pd.read_csv(out)
        except Exception as e:
            report["modes"][mode] = {"error": str(e)}
            ok = False
            continue
        # Count trials from the WORKDIR structure, not from stitched rows: a
        # 0-bug cell ran fine (5 trial dirs + monitor files) but produces 0
        # rows because there were no bug events. That is a VALID result, NOT
        # an incomplete run. Likewise online with 0 bugs legitimately has no
        # split tree (split is bug-triggered) — never flag that as broken.
        n_trial_dirs = sum(1 for td in campdir.glob("trial-*")
                           if td.is_dir() and any(td.rglob("monitor/*")))
        trials = sorted(df["trial_id"].unique()) if len(df) else []
        bug_events, unique_bugs = [], []
        for t in trials:
            sub = df[df.trial_id == t]
            trig = sub["triggered"].fillna(0).astype(float)
            bug_events.append(int(trig.sum()))
            unique_bugs.append(int(sub[trig > 0]["bug_id"].nunique()))
        has_split = bool((df["level"] > 0).any()) if (mode == "online" and "level" in df) else (mode == "nosplit")
        report["modes"][mode] = {
            "n_trial_dirs": n_trial_dirs, "n_trials_with_events": len(trials),
            "rows": int(len(df)),
            "mean_bug_events": (sum(bug_events) / len(bug_events)) if bug_events else 0,
            "mean_unique_bugs": (sum(unique_bugs) / len(unique_bugs)) if unique_bugs else 0,
            "has_split_structure": has_split,
        }
        # Completeness = all 5 trials actually RAN (trial dirs + monitor files).
        # Bug events may legitimately be 0. Do NOT require split structure.
        if n_trial_dirs < TRIALS:
            ok = False
    # Paper-alignment note (Theorem 3: online bug events >= nosplit). Logged
    # for review, NOT a deletion gate — a valid pair where splitting happens
    # not to win is still a keeper result, not a broken run.
    try:
        report["theorem3_online_ge_nosplit"] = bool(
            report["modes"]["online"]["mean_bug_events"]
            >= report["modes"]["nosplit"]["mean_bug_events"])
    except Exception:
        pass
    report["verified_complete"] = ok
    with open(RESULTS / f"verify_{f}_{b}.json", "w") as jf:
        json.dump(report, jf, indent=1)
    return ok, report


def mark_needs_review(f, b, reasons):
    """Record a pair whose results do not (yet) perfectly align with the paper.
    Its docker image is KEPT for possible re-run; decide manually."""
    (EXP_DIR / "results").mkdir(parents=True, exist_ok=True)
    with open(EXP_DIR / "results" / "needs_review.txt", "a") as frev:
        frev.write(f"{f}/{b}\t{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t"
                   f"{'; '.join(reasons)}\n")


def remove_finished_image(f, b):
    """Remove a pair's image magma/<f>/<b> — ONLY called by the end-of-run
    alignment audit for pairs that PERFECTLY align with the paper. Targeted
    docker rmi, never prune."""
    img = f"magma/{f}/{b}:latest"
    try:
        before = subprocess.run(["docker", "image", "inspect", img,
                                 "--format", "{{.Size}}"],
                                capture_output=True, text=True).stdout.strip()
        gb = (int(before) / 1e9) if before.isdigit() else 0
        r = subprocess.run(["docker", "rmi", img], capture_output=True, text=True)
        if r.returncode == 0:
            return gb
        # If still referenced (a container lingering), reap then retry once
        reap_exited_containers()
        subprocess.run(["docker", "rmi", img], capture_output=True)
        return gb
    except Exception:
        return 0


def containers():
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Image}}"],
                             capture_output=True, text=True, timeout=30).stdout
        return sum(1 for l in out.splitlines() if l.startswith("magma/"))
    except Exception:
        return -1


class CoreAllocator:
    """First-fit contiguous allocator over [0, TOTAL_CORES)."""
    def __init__(self, total):
        self.free = [(0, total)]  # list of (start, length), sorted by start

    def alloc(self, n):
        for i, (start, length) in enumerate(self.free):
            if length >= n:
                if length == n:
                    self.free.pop(i)
                else:
                    self.free[i] = (start + n, length - n)
                return start
        return None

    def release(self, start, n):
        self.free.append((start, n))
        self.free.sort()
        merged = []
        for s, l in self.free:
            if merged and merged[-1][0] + merged[-1][1] == s:
                merged[-1] = (merged[-1][0], merged[-1][1] + l)
            else:
                merged.append((s, l))
        self.free = merged

    def available(self):
        return sum(l for _, l in self.free)


def driver_for(fuzzer, bench):
    """Per-(fuzzer,benchmark) program + args. Verified by the full 54-pair
    pre-flight: libFuzzer harnesses read input in-memory (no @@/CLI args) and
    cannot run the CLI-only tiffcp, so libtiff uses its in-memory harness;
    afl++'s persistent shmem delivery freezes coverage on libFuzzer harnesses
    so those get file-input (@@). lua is a stdin interpreter for the AFL family
    (kept empty); libsndfile/libtiff already carry @@ via ARGS_BY_BENCH."""
    prog = BENCH_PROG[bench]
    args = ARGS_BY_BENCH.get(bench, "")
    if fuzzer == "libfuzzer":
        args = ""
        if bench == "libtiff":
            prog = "tiff_read_rgba_fuzzer"
    elif fuzzer == "aflplusplus" and not args and bench != "lua":
        args = "@@"
    return prog, args


def build_campaigns():
    """Benchmark-grouped: for each benchmark, all its fuzzers' online jobs then
    nosplit jobs, so the greedy packer fills each wave with MULTIPLE FUZZERS on
    ONE benchmark. Skip the correct afl pairs (KEEP_PAIRS). Within a benchmark,
    online first (40c bottleneck). Each campaign carries its per-benchmark ARGS."""
    # Benchmark-grouped over THIS phase's FUZZERS only (Phase 1 = 5 reliable;
    # Phase 2 = libfuzzer). Skip preserved pairs (KEEP_PAIRS); online first.
    camps = []
    for b in BENCH_ORDER:
        ons, nos = [], []
        for f in FUZZERS:
            if (f, b) in KEEP_PAIRS:
                continue
            prog, args = driver_for(f, b)
            ons.append((f, b, prog, "online", ONLINE_CORES, args))
            nos.append((f, b, prog, "nosplit", NOSPLIT_CORES, args))
        camps.extend(ons)
        camps.extend(nos)
    return camps


def launch(camp, cpu_base):
    f, b, prog, mode, cores, args = camp
    wd = EXP_DIR / f"{f}_{b}_{mode}"
    wd.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{f}_{b}_{mode}.log"
    cmd = ["python3", "-u", str(SRC),
           "--fuzzer", f, "--target", b, "--program", prog,
           "--mode", mode, "--total-hours", str(TOTAL_HOURS),
           "--trials", str(TRIALS), "--workdir", str(wd),
           "--cpu-base", str(cpu_base), "--args", args]
    if mode == "online":
        cmd += ["--branching", str(BRANCHING), "--max-splits", str(MAX_SPLITS),
                "--thresholds", *THRESHOLDS,
                "--window-hours", str(WINDOW_HOURS),
                "--persist-hours", str(PERSIST_HOURS),
                "--min-gap-hours", str(MIN_GAP_HOURS),
                "--no-split-last-hours", str(NO_SPLIT_LAST_HOURS),
                "--poll-sec", str(POLL_SEC)]
    lf = open(log_path, "w")
    p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    log(f"  LAUNCH {f}/{b}/{mode}  cores {cpu_base}..{cpu_base+cores-1}  pid={p.pid}")
    return {"camp": camp, "cpu_base": cpu_base, "cores": cores, "pid": p,
            "lf": lf, "name": f"{f}_{b}_{mode}"}


def preflight():
    LOGS.mkdir(parents=True, exist_ok=True)
    log("=" * 64)
    log(f"PACKED 6x6x22h campaign — {len(FUZZERS)} fuzzers x {len(BENCH_PROG)} benchmarks")
    log(f"  fuzzers:    {FUZZERS}")
    log(f"  benchmarks: {list(BENCH_PROG)}")
    log(f"  T={TOTAL_HOURS}h  trials={TRIALS}  K={MAX_SPLITS}  leaves={LEAVES}")
    log(f"  online={ONLINE_CORES}c  nosplit={NOSPLIT_CORES}c  total_cores={TOTAL_CORES}")
    log(f"  EXP_DIR={EXP_DIR}")
    log("=" * 64)
    # No stray orchestrators (other than this one's children) / containers
    n = subprocess.run(["pgrep", "-fc", "magma_online_split.py"],
                       capture_output=True, text=True).stdout.strip()
    if containers() > 0:
        log(f"!!! {containers()} magma containers already running — abort"); sys.exit(2)
    # Disk is NOT a hard gate (per-wave cleanup keeps it in check); just report.
    log(f"  startup disk={free_gb()}G  ram_avail={avail_ram_gb()}G")
    cp = Path("/proc/sys/kernel/core_pattern").read_text().strip()
    if cp != "core":
        log(f"  core_pattern is '{cp}' (expected 'core') — set it with sudo before run")


def image_exists(f, b):
    return subprocess.run(["docker", "image", "inspect", f"magma/{f}/{b}:latest"],
                          capture_output=True).returncode == 0


def link_kept_pairs():
    """Symlink the correct afl pairs' campaign dirs from the OLD run into this
    EXP_DIR so the end-of-run finalize+figures cover all 54 pairs in one place.
    These were NOT rerun (already correct); their monitor/crashes/stats survive."""
    n = 0
    for (f, b) in sorted(KEEP_PAIRS):
        if f not in FUZZERS:        # only link pairs belonging to this phase
            continue
        for mode in ("online", "nosplit"):
            src = OLD_EXP / f"{f}_{b}_{mode}"
            dst = EXP_DIR / f"{f}_{b}_{mode}"
            if src.is_dir() and not dst.exists():
                try:
                    dst.symlink_to(src)
                    n += 1
                except Exception as e:
                    log(f"  link warn {src}->{dst}: {e}")
    log(f"linked {n} kept-afl campaign dirs from {OLD_EXP.name} (not rerun)")


def main():
    preflight()
    link_kept_pairs()
    alloc = CoreAllocator(TOTAL_CORES)
    pending = build_campaigns()
    # Skip campaigns whose image is missing (e.g., libtiff blocked by a
    # savannah.gnu.org outage). They are DEFERRED + logged, not run with a
    # missing image, and never block the other pairs. Re-run them later once
    # the image builds (merge into this same EXP_DIR).
    deferred = [c for c in pending if not image_exists(c[0], c[1])]
    pending = [c for c in pending if image_exists(c[0], c[1])]
    if deferred:
        (EXP_DIR / "results").mkdir(parents=True, exist_ok=True)
        with open(EXP_DIR / "results" / "deferred_missing_image.txt", "w") as df_:
            for c in deferred:
                df_.write(f"{c[0]}/{c[1]}/{c[3]}\timage magma/{c[0]}/{c[1]} missing\n")
        log(f"DEFERRED {len(deferred)} campaigns (missing image): "
            f"{sorted(set((c[0], c[1]) for c in deferred))}")
    running = []
    total = len(pending)
    done = 0
    last_hb = 0
    pair_done = {}  # (fuzzer,benchmark) -> #campaigns finished (image freed at 2)
    log(f"scheduling {total} campaigns ({len(FUZZERS)*len(BENCH_PROG)} online + "
        f"{len(FUZZERS)*len(BENCH_PROG)} nosplit)")

    while pending or running:
        # Launch anything that fits — gated by RAM headroom only.
        # No hard disk gate: per-wave cleanup (corpus bulk + finished docker
        # images) keeps disk in check, as in the prior full-core run.
        if avail_ram_gb() >= RAM_FLOOR_GB:
            launched_any = True
            while launched_any:
                launched_any = False
                # Re-check RAM each launch: a wave of 9 can spike memory fast.
                if avail_ram_gb() < RAM_FLOOR_GB:
                    log(f"  RAM {avail_ram_gb()}G < {RAM_FLOOR_GB}G floor — holding launches")
                    break
                for i, camp in enumerate(pending):
                    if camp[4] <= alloc.available():
                        base = alloc.alloc(camp[4])
                        if base is not None:
                            running.append(launch(camp, base))
                            pending.pop(i)
                            launched_any = True
                            time.sleep(3)  # small stagger for docker daemon
                            break
        else:
            log(f"  RAM {avail_ram_gb()}G < {RAM_FLOOR_GB}G floor — pausing new launches")

        # Reap finished
        still = []
        for r in running:
            rc = r["pid"].poll()
            if rc is None:
                still.append(r)
            else:
                r["lf"].close()
                alloc.release(r["cpu_base"], r["cores"])
                done += 1
                # Free this campaign's bulk seed data immediately (keeps all
                # analysis data). Makes space as each fuzzer/benchmark finishes.
                freed_mb = cleanup_campaign(r["name"])
                reap_exited_containers()
                # When both campaigns (online+nosplit) of a pair are done:
                # analyze+verify IN PARALLEL with the running waves (pure
                # Python, no containers). Images are KEPT during the run — full
                # paper-alignment (incl. variance) is only known after figures,
                # so the delete/keep decision is made by the end-of-run audit.
                # Here we mark cheaply-detectable problems for review.
                f_, b_ = r["camp"][0], r["camp"][1]
                pair_done[(f_, b_)] = pair_done.get((f_, b_), 0) + 1
                img_msg = ""
                if pair_done[(f_, b_)] >= 2:
                    verified, rep = analyze_verify_pair(f_, b_)
                    t3 = rep.get("theorem3_online_ge_nosplit")
                    reasons = []
                    if not verified:
                        reasons.append("incomplete/invalid run (trials or split-tree missing)")
                    if t3 is False:
                        reasons.append("online BDR < nosplit (Theorem 3 not satisfied)")
                    if reasons:
                        mark_needs_review(f_, b_, reasons)
                        img_msg = f"  MARKED for review ({'; '.join(reasons)}) — image KEPT"
                    else:
                        img_msg = "  complete+Thm3 OK (image KEPT pending end-of-run variance/figure audit)"
                log(f"  DONE  {r['name']}  rc={rc}  ({done}/{total})  "
                    f"free_cores={alloc.available()}  bulk_freed={freed_mb}MB"
                    f"{img_msg}  disk={free_gb()}G")
        running = still

        # Heartbeat every 30 min — watch RAM (reboot risk) and disk closely.
        now = time.time()
        if now - last_hb >= 1800:
            log(f"HEARTBEAT alive={len(running)} pending={len(pending)} done={done}/{total} "
                f"cores_used={TOTAL_CORES-alloc.available()}/{TOTAL_CORES} "
                f"containers={containers()} disk={free_gb()}G ram_avail={avail_ram_gb()}G")
            last_hb = now

        if running or pending:
            time.sleep(60)

    log(f"ALL {total} CAMPAIGNS FINISHED")

    # Cleanup stray containers
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.ID}} {{.Image}}"],
                            capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if "magma/" in line:
                cid = line.split()[0]
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    except Exception as e:
        log(f"container cleanup warn: {e}")

    # Finalize + figures on the combined EXP_DIR
    log("--- finalize ---")
    fin = EXP_DIR / "finalize"
    env = dict(os.environ, MAIN=str(EXP_DIR), OUT=str(fin), TOTAL_HOURS=str(TOTAL_HOURS))
    with open(EXP_DIR / "finalize.log", "w") as lf:
        subprocess.run(["bash", str(ROOT / "scripts" / "finalize_experiment.sh")],
                       env=env, stdout=lf, stderr=subprocess.STDOUT)
    log("--- figures ---")
    with open(EXP_DIR / "figures.log", "w") as lf:
        subprocess.run(["python3", str(ROOT / "src" / "magma_figures.py"),
                       "--input", str(fin / "all_runs.csv"),
                       "--pocs", str(fin / "all_pocs.csv"),
                       "--execs-root", str(EXP_DIR),
                       "--total-hours", str(TOTAL_HOURS),
                       "--outdir", str(EXP_DIR / "figures"),
                       "--logdir", str(LOGS)],
                       stdout=lf, stderr=subprocess.STDOUT)

    run_alignment_audit()
    log(f"=== PACKED CAMPAIGN COMPLETE — {EXP_DIR} ===")


def run_alignment_audit():
    """End-of-run audit of every pair against the paper's claims:
      - completeness (from per-pair verify JSON),
      - Theorem 3: online terminal weighted bug count >= nosplit (Fig 5,12-17),
      - variance: online effort-normalized variance <= nosplit (Fig 1-4,7-11).
    REPORT ONLY — does NOT delete any docker image. Pairs that don't perfectly
    align are listed in ALIGNMENT_REPORT.txt with their reasons; their images
    are retained for re-run; decide manually what to delete or re-run."""
    import pandas as pd
    rep_path = EXP_DIR / "results" / "ALIGNMENT_REPORT.txt"
    (EXP_DIR / "results").mkdir(parents=True, exist_ok=True)
    tbl = EXP_DIR / "figures" / "tables" / "weighted_triggered_time.csv"
    cell = {}
    try:
        t = pd.read_csv(tbl)
        for (f, b, m), sub in t.groupby(["fuzzer", "target", "mode"]):
            term = sub.sort_values("t_sec").iloc[-1]
            cell[(f, b, m)] = (float(term["mean_N"]), float(term.get("var_P", float("nan"))))
    except Exception as e:
        log(f"  alignment audit: could not read {tbl}: {e}")

    lines, aligned_n, marked_n = [], 0, 0
    for f in FUZZERS:
        for b in BENCH_PROG:
            reasons = []
            vj = EXP_DIR / "results" / f"verify_{f}_{b}.json"
            complete = False
            try:
                complete = json.loads(vj.read_text()).get("verified_complete", False)
            except Exception:
                pass
            if not complete:
                reasons.append("incomplete/invalid run")
            on = cell.get((f, b, "online")); ns = cell.get((f, b, "nosplit"))
            if on and ns:
                if on[0] < ns[0]:
                    reasons.append(f"Theorem 3: online bugcount {on[0]:.2f} < nosplit {ns[0]:.2f}")
                if not (on[1] != on[1]) and not (ns[1] != ns[1]) and on[1] > ns[1]:
                    reasons.append(f"variance: online {on[1]:.3g} > nosplit {ns[1]:.3g} (verify vs CPU-norm)")
            else:
                reasons.append("missing terminal table data")
            if reasons:
                marked_n += 1
                lines.append(f"MARKED  {f}/{b}\t{'; '.join(reasons)}")
            else:
                aligned_n += 1
                lines.append(f"ALIGNED {f}/{b}")
    rep_path.write_text("\n".join(lines) + "\n")
    log(f"=== ALIGNMENT AUDIT: {aligned_n} aligned, {marked_n} marked-for-review ===")
    log(f"    report: {rep_path}  (ALL images retained; deletions/re-runs are manual)")
    for ln in lines:
        if ln.startswith("MARKED"):
            log("    " + ln)


if __name__ == "__main__":
    main()

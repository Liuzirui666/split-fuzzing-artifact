# magma_split — Splitting on Magma

Runs the splitting framework on the [Magma](https://hexhive.epfl.ch/magma/)
ground-truth fuzzing benchmark to validate the paper's bug-detection claims
against canary-verified bug counts.

## Layout

```
magma_split/
├── README.md                  — this file
├── src/                       — orchestrators + analysis (Python)
│   ├── magma_split.py         — offline-split orchestrator: launches Magma
│   │                            containers per (fuzzer, target, trial, branch),
│   │                            seed harvesting at split points, per-branch
│   │                            SHARED volumes for isolated canary storage
│   ├── magma_online_split.py  — online-split orchestrator: one campaign per
│   │                            (fuzzer, target, mode); detects coverage
│   │                            sparsity live and splits at runtime
│   ├── magma_report.py        — stitches per-branch monitor/ CSV dumps into a
│   │                            long-format CSV with global time reconstructed
│   │                            across split stages
│   ├── magma_survival.py      — Kaplan-Meier survival + RMST per (fuzzer,
│   │                            target, mode, bug); split-vs-nosplit comparison
│   ├── magma_poc_extract.py   — replays each saved crash through Magma's
│   │                            runonce.sh to compute the Detected metric
│   ├── magma_exp2json.py      — emits Magma's exp2json JSON schema so the data
│   │                            can be fed into tools/benchd/survival_analysis.py
│   └── magma_figures.py       — per-(fuzzer, benchmark) figures: bug counts,
│                                unique bugs, BDR, effort-normalized variance,
│                                coverage
├── scripts/
│   ├── build_magma_full.sh    — build all fuzzer × target Docker images
│   ├── preflight_smoke.py     — short smoke of every pair before a campaign
│   ├── final_verify.py        — pre-launch verification of all pairs
│   ├── resmoke.py             — longer re-verification of selected pairs
│   ├── verify_subset.py       — verify a chosen subset of pairs
│   ├── run_packed_matrix.py   — MAIN campaign runner (see "Run" below)
│   ├── finalize_experiment.sh — post-experiment pipeline: monitor stitching →
│   │                            PoC extraction → survival analysis → JSON
│   ├── combine_12h_batches.sh — merge two finished batches into one trial space
│   └── run_*.sh, sanity_1h.sh — single-benchmark / fixed-duration runners and
│                                a 1-hour end-to-end sanity check
├── monitor_watchdog/
│   └── watchdog.sh            — independent health logger (RAM / disk /
│                                container count) while a campaign runs
├── magma/                     — vendored HexHive/magma v1.2.1 with patches applied
└── experiments/               — created at runtime; one subdir per run
```

## Requirements

- Docker (builds use `DOCKER_BUILDKIT=0`).
- Python 3 with `pandas`, `numpy`, `matplotlib`.
- AFL-based fuzzers need the host core pattern set once:
  `echo core | sudo tee /proc/sys/kernel/core_pattern`.

## Run

```bash
# 1. Build all fuzzer × target images (sequential; logs in build_logs/)
bash scripts/build_magma_full.sh

# 2. Verify every pair fuzzes correctly before committing to a campaign
python3 scripts/final_verify.py

# 3. Full packed campaign: each (fuzzer, benchmark) runs an online (splitting,
#    branching 2, up to 3 splits → 8 leaves) and a nosplit campaign, 5 trials,
#    12 hours, greedily bin-packed onto the available cores.
python3 scripts/run_packed_matrix.py
#    Knobs (env): CAMPAIGN_FUZZERS=comma,list   fuzzer roster override
#                 CONTAINER_MEM_GB=4            per-container RAM cap
#                 FRESH_ALL=1                   rerun every pair fresh
#    Edit TOTAL_CORES in the script to match your machine.

# 4. Post-experiment pipeline (CSV + PoC + survival + exp2json JSON) and
#    figures — run automatically at the end of run_packed_matrix.py, or
#    standalone on any experiment directory:
MAIN=experiments/<your_run> bash scripts/finalize_experiment.sh
python3 src/magma_figures.py --help
```

The campaign covers the Magma v1.2.1 suite (libpng, libsndfile, libtiff,
libxml2, openssl, php, poppler, sqlite3; lua is excluded at runtime) with
afl, aflfast, aflplusplus, moptafl, and honggfuzz by default; libfuzzer can
be added via `CAMPAIGN_FUZZERS`.

## Metrics (all three from the Magma paper)

- **Reached**: canary line executed. Monitor counter `<BUG>_R`.
- **Triggered**: canary's boolean condition evaluated true. Monitor counter
  `<BUG>_T`. Bit-exact ground truth.
- **Detected**: replay a saved crash against the canary-instrumented binary
  via `runonce.sh`; bug is detected if a canary triggers during replay.

## Splitting in the Magma setting

Each branch of a split trial runs in its own container with its own
`$SHARED/canaries.raw` (per-branch SHARED volume). Canary counters are
therefore isolated per branch; the orchestrator bind-mounts the parent
stage's queue at `/magma/targets/<t>/corpus/<p>` so each child starts from
the parent's corpus (pruned again by Magma's `run.sh` at startup).

Post-processing (`magma_report.py`) reconstructs global time across stages by
adding each branch's stage-start offset to its local monitor timestamps.
`magma_survival.py` aggregates to the trial level by taking the minimum
first-trigger time across all branches within a trial.

## Patches applied to Magma v1.2.1 (already vendored in `magma/`)

- `magma/fuzzers/honggfuzz/run.sh`, `magma/fuzzers/libfuzzer/run.sh`,
  `magma/fuzzers/moptafl/run.sh`: run-script fixes so coverage/execs logging
  and corpus export work under the split runner (libfuzzer logs redirected
  with `2>&1`, honggfuzz stdin mode for empty-ARGS targets, moptafl seed
  timeout raised so a transient slow seed is not a fatal dry-run abort).
- `targets/libsndfile/build.sh`, `targets/libtiff/build.sh`,
  `targets/lua/build.sh`, `targets/php/patches/setup/setup.patch`: build
  fixes (savannah-independent libtiff autogen, php libstdc++ harness, lua
  fuzzer build).
- `targets/lua/src/lua_fuzzer.c`: lua harness mirroring the canonical
  OSS-Fuzz lua_fuzzer.

No other modifications to Magma. If the default Ubuntu archives time out
from your host, swap `archive.ubuntu.com` for a regional mirror in
`magma/magma/preinstall.sh`.

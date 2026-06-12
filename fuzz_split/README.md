# Split-Fuzzing Framework

A simulation-based evaluation framework for mutation-based fuzzing. We model fuzzers as Markov chains, construct statistically sound estimators for bug detection rates, and validate the theory on 18 real-world benchmarks with 9 representative fuzzers.

## Overview

This framework implements:
- **Splitting** — detects coverage sparsity during fuzzing and branches campaigns into multiple independent trials at sparsity points
- **Simulation-based evaluation** — Monte Carlo replications with weighted (1/k_t) estimators
- **Data-driven metrics** — D_R(T), E(T), and spectral parameter estimation

### Workflow

1. **Build** — Docker images for 18 benchmarks × 9 fuzzers (from FuzzBench commit `90e59b6`)
2. **Baseline** — No-split experiments (up to 72h) to collect coverage and bug data
3. **Sparsity** — Compute per-(fuzzer, benchmark) split times from baseline data
4. **Split** — Run split experiments using computed split times with branching factor 2
5. **Analysis** — Compare split vs baseline: coverage, bugs, variance, estimator convergence

## Benchmarks (18, from FuzzBench `90e59b6`)

arrow, aspell, ffmpeg, grok, harfbuzz, libgit2, libhevc, libhtp, libxml2, matio, njs, openh264, php, poppler, quickjs, stb, systemd, wireshark

## Fuzzers (9)

AFL, AFLFast, AFL++, AFLSmart, Entropic, FairFuzz, Honggfuzz, LibFuzzer, MOpt

## Project Structure

```
fuzz_split/
├── src/                              # Core split framework
│   ├── config.py                     # SplitConfig, experiment naming
│   ├── split_plan.py                 # Stage/SplitPlan, sparsity CSV reader
│   ├── seed_manager.py               # Per-branch seed management
│   ├── orchestrator.py               # Single-trial split orchestrator
│   ├── parallel_runner.py            # N parallel root trials with CPU layout
│   ├── online_orchestrator.py        # Online mode (dynamic sparsity)
│   ├── online_sparsity.py            # Per-trial sparsity tracker
│   ├── sparsity.py                   # Offline sparsity analysis
│   ├── report.py                     # FuzzBench CSV + split metadata
│   ├── estimator.py                  # Weighted 1/k_t estimators
│   ├── cli.py                        # CLI: run / parallel / compute-sparsity / report
│   └── utils.py                      # Safety checks, container cleanup
├── scripts/
│   ├── run_one_benchmark_parallel.sh # Build+test: honggfuzz first, then 8 parallel
│   ├── run_one_benchmark.sh          # Build+test: all 9 sequential
│   ├── run_one_fuzzer.sh             # Rerun a single fuzzer
│   ├── run_split.sh                  # Run a split experiment
│   ├── compute_sparsity.sh           # Compute split times from baseline
│   ├── generate_figures.py           # Paper-quality figure generation
│   └── patch_generated_mk.py        # Patch FuzzBench generated.mk
├── fuzzbench/                        # FuzzBench (latest infra, benchmarks from 90e59b6)
├── configs/                          # Experiment YAML configs
├── data/                             # Baseline CSVs, sparsity summaries
└── results/                          # Experiment outputs, figures
```

## Quick Start

### Environment

```bash
conda activate fuzz_split  # Python 3.10
```

### 1. Build & Verify (Preliminary)

```bash
# Build + test all 9 fuzzers for one benchmark
# Strategy: honggfuzz first (sequential), then 8 fuzzers in parallel
./scripts/run_one_benchmark_parallel.sh arrow_parquet-arrow-fuzz bt

# Rerun a single failed fuzzer
./scripts/run_one_fuzzer.sh aflsmart arrow_parquet-arrow-fuzz
```

### 2. Compute Sparsity

```bash
./scripts/compute_sparsity.sh --input data/baseline_24h.csv --output data/
```

### 3. Run Split Experiment

```bash
python -m src.cli parallel --mode offline \
    --fuzzer afl --benchmark stb_stbi_read_fuzzer \
    --sparsity-csv data/sparsity_split_times_summary.csv \
    --total-hours 23 --num-trials 5 --total-cores 188 \
    --experiment-name split-afl-stb
```

### 4. Generate Figures

```bash
python scripts/generate_figures.py \
    --baseline data/baseline_24h.csv \
    --split results/split_report.csv \
    --output figures/ \
    --figure-types coverage bugs dashboard summary estimator variance
```

## FuzzBench Patches

- CPU offset allocation for parallel experiments
- 6-minute snapshot period
- Honggfuzz: pinned commit, --threads 1, --timeout 120
- Dispatcher: reduced wait times, zombie process fix
- Benchmark fixes: ffmpeg (autoconf 2.71, git URLs), njs (pcre tarball), openh264 (corpus fix), grok (full clone), systemd (apt meson), aflsmart (stub Pin, default gcc)

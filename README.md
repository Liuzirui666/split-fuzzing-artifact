# Split-Fuzzing Framework

This repository contains the two implementations of the campaign-splitting framework:

- [`fuzz_split/`](fuzz_split/) — splitting on **FuzzBench**: coverage-sparsity detection, split orchestration, and weighted (1/k_t) estimators across 18 benchmarks × 9 fuzzers. The FuzzBench tree (infrastructure plus benchmarks pinned to commit `90e59b6`) is vendored at `fuzz_split/fuzzbench/`.
- [`magma_split/`](magma_split/) — splitting on the **Magma** ground-truth benchmark: bug-level Reached / Triggered / Detected metrics and survival analysis. The Magma v1.2.1 tree, with the fuzzer/target patches already applied, is vendored at `magma_split/magma/` (see `magma_split/README.md` for the patch list).

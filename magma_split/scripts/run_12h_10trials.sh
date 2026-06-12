#!/bin/bash
# Run TWO back-to-back 5-trial 12-hour chains on {poppler, openssl}.
# Total wall-clock: ~48h (4 × 12h benchmark runs, sequential).
#
# Each invocation of run_12h_chain.sh produces two timestamped experiment
# directories (one per benchmark). We run it twice. Resulting layout:
#
#   experiments/main_12h_online_poppler_<ts_A>/    (batch A, 5 trials)
#   experiments/main_12h_online_openssl_<ts_A>/    (batch A, 5 trials)
#   experiments/main_12h_online_poppler_<ts_B>/    (batch B, 5 more trials)
#   experiments/main_12h_online_openssl_<ts_B>/    (batch B, 5 more trials)
#
# After both batches finish, a combined finalize/figures pass is run for
# each benchmark by symlinking 10 trials into a single combined workdir.
set -uo pipefail

ROOT=/path/to/magma_split
CHAIN="$ROOT/scripts/run_12h_chain.sh"
LOG_ROOT="$ROOT/experiments/run_12h_10trials_$(date +%Y%m%d_%H%M%S).log"

echo "===================================================="
echo "= 12h chain × 2 batches (10 trials total)"
echo "= master log: $LOG_ROOT"
echo "= start: $(date)"
echo "===================================================="

echo; echo ">>>> BATCH A start $(date)"
bash "$CHAIN"
echo ">>>> BATCH A end   $(date)"

echo; echo ">>>> BATCH B start $(date)"
bash "$CHAIN"
echo ">>>> BATCH B end   $(date)"

echo; echo "= Combine 10-trial datasets and regenerate figures"
bash "$ROOT/scripts/combine_12h_batches.sh" || echo "!!! combine step failed (non-fatal; per-batch figures already exist)"

echo "===================================================="
echo "= ALL 12h 10-trial campaigns done at $(date)"
echo "===================================================="

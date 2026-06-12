#!/usr/bin/env bash
# Compute sparsity-based split times from a baseline CSV.
#
# Usage:
#   ./scripts/compute_sparsity.sh --input data/baseline.csv --output data/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

eval "$(conda shell.bash hook)"
conda activate fuzz_split

cd "$PROJECT_DIR"
export PYTHONPATH="${PROJECT_DIR}/fuzzbench:${PYTHONPATH:-}"

python -m src.cli compute-sparsity "$@"

#!/usr/bin/env bash
# Run a split experiment with the given parameters.
#
# Usage:
#   ./scripts/run_split.sh --fuzzer afl --benchmark stb_stbi_read_fuzzer \
#     --split-times 8.0 14.0 18.0 --total-hours 23 --experiment-name test1
#
# For full options, run: python -m src.cli run --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Activate conda env
eval "$(conda shell.bash hook)"
conda activate fuzz_split

cd "$PROJECT_DIR"

# Ensure PYTHONPATH includes fuzzbench
export PYTHONPATH="${PROJECT_DIR}/fuzzbench:${PYTHONPATH:-}"

echo "=== Split Experiment ==="
echo "Project: $PROJECT_DIR"
echo "Args: $*"
echo "========================"

python -m src.cli run "$@"

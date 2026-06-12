#!/bin/bash
# Main experiment: 4 fuzzers x 2 targets x {split, nosplit} x N=10 x 24h.
#
# Config B: 2 fuzzers concurrent per target, split+nosplit concurrent.
# Per round: 2 fuzzers x 10 trials x (8 branches split + 1 branch nosplit) = 180 cores.
# 4 rounds sequential = 96h = 4 days wall-clock on 188 cores.
#
# Split plan: branching=2, K=3 splits → 8 leaf branches per trial.
# Default split times = T/4, T/2, 3T/4 (even quartiles). Override via SPLIT_HOURS env.
#
# Usage:
#   bash run_main_experiment.sh <round_num>   (1..4, sequential)
#   bash run_main_experiment.sh all           (all 4, one after another)
set -uo pipefail

ROOT=/path/to/magma_split
EXP_DIR="$ROOT/experiments/main"
TOTAL_HOURS=${TOTAL_HOURS:-24.0}
TRIALS=${TRIALS:-10}
BRANCHING=${BRANCHING:-2}
# K=3 splits → leaf branches = 2^3 = 8
SPLIT_HOURS=${SPLIT_HOURS:-"6.0 12.0 18.0"}

mkdir -p "$EXP_DIR/logs"

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1

# Per-round (fuzzer list, target)
ROUNDS=(
    "poppler:afl,moptafl"
    "poppler:aflfast,honggfuzz"
    "openssl:afl,moptafl"
    "openssl:aflfast,honggfuzz"
)

leaf_branches=1
for _s in $SPLIT_HOURS; do leaf_branches=$((leaf_branches * BRANCHING)); done

run_round() {
    local round_idx=$1
    local spec=${ROUNDS[$((round_idx - 1))]}
    local target=${spec%%:*}
    local fuzzers_csv=${spec##*:}
    local prog=${PROG[$target]}
    IFS=',' read -ra ROUND_FUZZERS <<< "$fuzzers_csv"

    echo "=========================================================="
    echo "ROUND $round_idx  target=$target  fuzzers=${ROUND_FUZZERS[*]}"
    echo "T=${TOTAL_HOURS}h  trials=${TRIALS}  split_hours=$SPLIT_HOURS  leaf=$leaf_branches"
    echo "Start: $(date)"
    echo "=========================================================="

    local declare_pids=()
    local declare_names=()
    declare -a R_PIDS R_NAMES

    # CPU layout for this round:
    #   split: 2 fuzzers × TRIALS × leaf cores (at peak), allocate contiguous
    #   nosplit: 2 fuzzers × TRIALS = 2*N cores, allocate AFTER split block
    local cpu=0
    for f in "${ROUND_FUZZERS[@]}"; do
        local wd="$EXP_DIR/${f}_${target}_split"
        rm -rf "$wd" && mkdir -p "$wd"
        python3 -u "$ROOT/src/magma_split.py" \
            --fuzzer "$f" --target "$target" --program "$prog" \
            --mode split --total-hours "$TOTAL_HOURS" \
            --split-hours $SPLIT_HOURS --branching "$BRANCHING" \
            --trials "$TRIALS" --workdir "$wd" --cpu-base "$cpu" \
            > "$EXP_DIR/logs/${f}_${target}_split.log" 2>&1 &
        R_PIDS+=($!)
        R_NAMES+=("split:${f}/${target} cpu_base=$cpu")
        # Split trials use leaf cores each at peak
        cpu=$((cpu + TRIALS * leaf_branches))
    done
    for f in "${ROUND_FUZZERS[@]}"; do
        local wd="$EXP_DIR/${f}_${target}_nosplit"
        rm -rf "$wd" && mkdir -p "$wd"
        python3 -u "$ROOT/src/magma_split.py" \
            --fuzzer "$f" --target "$target" --program "$prog" \
            --mode nosplit --total-hours "$TOTAL_HOURS" \
            --trials "$TRIALS" --workdir "$wd" --cpu-base "$cpu" \
            > "$EXP_DIR/logs/${f}_${target}_nosplit.log" 2>&1 &
        R_PIDS+=($!)
        R_NAMES+=("nosplit:${f}/${target} cpu_base=$cpu")
        cpu=$((cpu + TRIALS))
    done

    echo "Launched ${#R_PIDS[@]} campaigns, using $cpu CPUs total"
    for i in "${!R_PIDS[@]}"; do echo "  pid=${R_PIDS[i]} ${R_NAMES[i]}"; done

    if [ $cpu -gt 188 ]; then
        echo "!!! WARNING: CPU overcommit ($cpu > 188) !!!"
    fi

    echo "Waiting for round $round_idx to finish..."
    for p in "${R_PIDS[@]}"; do wait $p; done
    echo "Round $round_idx finished at $(date)"

    # Per-campaign report
    for f in "${ROUND_FUZZERS[@]}"; do
        for m in split nosplit; do
            tag="${f}_${target}_${m}"
            wd="$EXP_DIR/$tag"
            if grep -q "trials OK" "$EXP_DIR/logs/${tag}.log" 2>/dev/null; then
                status=$(grep "trials OK" "$EXP_DIR/logs/${tag}.log" | tail -1)
                python3 "$ROOT/src/magma_report.py" \
                    --workdir "$wd" --fuzzer "$f" --target "$target" --mode "$m" \
                    --output "$EXP_DIR/${tag}.csv" > /dev/null 2>&1
                echo "OK   $tag   $status   csv=$EXP_DIR/${tag}.csv"
            else
                echo "FAIL $tag"
            fi
        done
    done
}

case "${1:-}" in
    1|2|3|4) run_round "$1" ;;
    all)
        for r in 1 2 3 4; do
            run_round $r
            echo "Round $r done; checking disk..."
            df -BG / | tail -1
        done
        ;;
    *)
        echo "Usage: $0 {1|2|3|4|all}"
        echo ""
        echo "Rounds:"
        for i in "${!ROUNDS[@]}"; do
            r=$((i + 1))
            spec=${ROUNDS[i]}
            echo "  $r: $spec"
        done
        exit 1
        ;;
esac

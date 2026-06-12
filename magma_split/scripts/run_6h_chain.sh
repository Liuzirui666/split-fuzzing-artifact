#!/bin/bash
# Run 6h online+nosplit experiments on BOTH benchmarks sequentially:
# poppler (T=6h) → openssl (T=6h). Full pipeline + figures after each.
#
# Uses paper's K=3 stages (max 8 leaves per split trial), 4 fuzzers × 5 trials
# × {online, nosplit}.  All three Magma metrics (Reached, Triggered, Detected)
# produced on both time and execs axes.
set -uo pipefail

ROOT=/path/to/magma_split
TOTAL_HOURS=${TOTAL_HOURS:-6.0}
TRIALS=${TRIALS:-5}
BRANCHING=2
MAX_SPLITS=3
LEAVES=$((BRANCHING ** MAX_SPLITS))

# Tight params so splits fire on early-bug fuzzers
WINDOW_HOURS=0.05
PERSIST_HOURS=0.05
MIN_GAP_HOURS=0.167
NO_SPLIT_LAST_HOURS=0.25
THRESHOLDS="0.5 0.3 0.15"
POLL_SEC=30

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1

FUZZERS=(afl moptafl aflfast honggfuzz)

run_one_benchmark() {
    local TARGET=$1
    local prog=${PROG[$TARGET]}
    local EXP_DIR="$ROOT/experiments/main_6h_online_${TARGET}_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$EXP_DIR/logs"

    echo
    echo "################################################################"
    echo "# ${TARGET}  T=${TOTAL_HOURS}h  N=${TRIALS}  K=${MAX_SPLITS}"
    echo "# start: $(date)"
    echo "# dir:   $EXP_DIR"
    echo "################################################################"

    # Pre-flight
    if [ "$(docker ps --format '{{.Image}}' | grep -c '^magma/')" -gt 0 ]; then
        echo "!!! magma containers running" >&2; return 2
    fi
    if [ "$(pgrep -f 'python3.*magma_online_split\.py' | wc -l)" -gt 0 ]; then
        echo "!!! orchestrator procs running" >&2; return 3
    fi
    local free_gb=$(df -BG / | tail -1 | awk '{gsub("G","",$4); print $4}')
    if [ "$free_gb" -lt 300 ]; then echo "!!! disk ${free_gb}G<300" >&2; return 4; fi
    if [ "$(cat /proc/sys/kernel/core_pattern)" != "core" ]; then
        echo core | sudo tee /proc/sys/kernel/core_pattern
    fi

    local -a PIDS NAMES

    launch() {
        local fuzzer=$1 mode=$2 cpu_base=$3
        local wd="$EXP_DIR/${fuzzer}_${TARGET}_${mode}"
        mkdir -p "$wd"
        local span=$TRIALS extra=""
        if [ "$mode" = "online" ]; then
            span=$((TRIALS * LEAVES))
            extra="--branching $BRANCHING --max-splits $MAX_SPLITS \
                   --thresholds $THRESHOLDS \
                   --window-hours $WINDOW_HOURS --persist-hours $PERSIST_HOURS \
                   --min-gap-hours $MIN_GAP_HOURS \
                   --no-split-last-hours $NO_SPLIT_LAST_HOURS \
                   --poll-sec $POLL_SEC"
        fi
        python3 -u "$ROOT/src/magma_online_split.py" \
            --fuzzer "$fuzzer" --target "$TARGET" --program "$prog" \
            --mode "$mode" --total-hours "$TOTAL_HOURS" \
            --trials "$TRIALS" --workdir "$wd" --cpu-base "$cpu_base" \
            $extra \
            > "$EXP_DIR/logs/${fuzzer}_${mode}.log" 2>&1 &
        local pid=$!
        PIDS+=($pid); NAMES+=("${fuzzer}_${mode}:cpu=${cpu_base}..$((cpu_base+span-1))")
        echo "  pid=$pid  ${NAMES[${#NAMES[@]}-1]}"
    }

    local cpu=0
    for f in "${FUZZERS[@]}"; do launch "$f" online "$cpu"; cpu=$((cpu+TRIALS*LEAVES)); done
    for f in "${FUZZERS[@]}"; do launch "$f" nosplit "$cpu"; cpu=$((cpu+TRIALS)); done

    echo "launched ${#PIDS[@]} campaigns, cores 0..$((cpu-1))  (${cpu}/188)"

    local LAST=$(date +%s)
    while true; do
        local alive=0
        for p in "${PIDS[@]}"; do
            if kill -0 "$p" 2>/dev/null; then alive=$((alive+1)); fi
        done
        if [ $alive -eq 0 ]; then break; fi
        local now=$(date +%s)
        if [ $((now - LAST)) -ge 1800 ]; then
            echo "[$(date)] alive=$alive/${#PIDS[@]}  containers=$(docker ps --format '{{.Image}}' | grep -c '^magma/')"
            local splits=$(grep -h "SPLIT at t=" $EXP_DIR/logs/*_online.log 2>/dev/null | wc -l)
            echo "  splits fired: $splits"
            LAST=$now
        fi
        sleep 60
    done

    echo "[$(date)] ${TARGET} campaigns finished"

    echo; echo "--- finalize ---"
    MAIN="$EXP_DIR" OUT="$EXP_DIR/finalize" TOTAL_HOURS="$TOTAL_HOURS" \
        bash "$ROOT/scripts/finalize_experiment.sh" 2>&1 | tee "$EXP_DIR/finalize.log" | tail -20

    echo; echo "--- figures ---"
    python3 "$ROOT/src/magma_figures.py" \
        --input "$EXP_DIR/finalize/all_runs.csv" \
        --pocs "$EXP_DIR/finalize/all_pocs.csv" \
        --execs-root "$EXP_DIR" \
        --total-hours "$TOTAL_HOURS" \
        --outdir "$EXP_DIR/figures" \
        --logdir "$EXP_DIR/logs" 2>&1 | tee "$EXP_DIR/figures.log" | tail -10

    # Clean up containers just in case
    for cid in $(docker ps --format '{{.ID}} {{.Image}}' | awk '$2 ~ /^magma\// {print $1}'); do
        docker rm -f $cid >/dev/null 2>&1
    done

    echo "=== ${TARGET} done: $EXP_DIR ==="
}

echo "============ 6h chain: poppler then openssl ============"
echo "global start: $(date)"
run_one_benchmark poppler
run_one_benchmark openssl
echo "============ ALL DONE at $(date) ============"

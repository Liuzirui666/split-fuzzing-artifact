#!/bin/bash
# 3-hour single-benchmark full experiment.
# Runs 4 fuzzers × N=5 trials × {split, nosplit} CONCURRENTLY on one benchmark.
#
# CPU layout (188-core host, B=2, K=3 → 8 leaves per split trial):
#   afl        split   trials 0..4 → cpus   0..39   (5×8)
#   moptafl    split   trials 0..4 → cpus  40..79
#   aflfast    split   trials 0..4 → cpus  80..119
#   honggfuzz  split   trials 0..4 → cpus 120..159
#   afl        nosplit trials 0..4 → cpus 160..164  (5×1)
#   moptafl    nosplit trials 0..4 → cpus 165..169
#   aflfast    nosplit trials 0..4 → cpus 170..174
#   honggfuzz  nosplit trials 0..4 → cpus 175..179
# Total: 180 / 188 cores used, zero overlap.
#
# Usage:  bash run_3h_single_bench.sh <target>  (poppler|openssl)
set -uo pipefail

TARGET=${1:-poppler}
ROOT=/path/to/magma_split
EXP_DIR="$ROOT/experiments/main_3h_${TARGET}_$(date +%Y%m%d_%H%M%S)"
TOTAL_HOURS=${TOTAL_HOURS:-3.0}
TRIALS=${TRIALS:-5}
BRANCHING=2
# K=3 splits at 3h*1/4, 2/4, 3/4 of run → 45min stages
SPLIT_HOURS="0.75 1.5 2.25"
LEAVES=8   # 2^3

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1
prog=${PROG[$TARGET]}
if [ -z "$prog" ]; then
    echo "unknown target: $TARGET"; exit 1
fi

FUZZERS=(afl moptafl aflfast honggfuzz)

mkdir -p "$EXP_DIR/logs"
echo "=== 3h experiment started at $(date) ==="
echo "target:        $TARGET ($prog)"
echo "fuzzers:       ${FUZZERS[*]}"
echo "trials:        $TRIALS"
echo "duration:      ${TOTAL_HOURS}h per trial"
echo "split_hours:   $SPLIT_HOURS (K=3, leaves=$LEAVES)"
echo "experiment:    $EXP_DIR"
echo

# ------------------- safety: make sure nothing is running -------------------
pre_running=$(docker ps --format '{{.Image}}' | grep -c "^magma/" || true)
if [ "$pre_running" -gt 0 ]; then
    echo "!!!! ERROR: $pre_running magma/* container(s) already running. Stop them first." >&2
    docker ps --format '{{.ID}} {{.Image}}' | grep '^.* magma/' >&2
    exit 2
fi
pre_py=$(ps ax | grep magma_split.py | grep -v grep | wc -l)
if [ "$pre_py" -gt 0 ]; then
    echo "!!!! ERROR: $pre_py magma_split.py process(es) already running." >&2
    exit 3
fi
free_gb=$(df -BG / | tail -1 | awk '{gsub("G","",$4); print $4}')
if [ "$free_gb" -lt 300 ]; then
    echo "!!!! ERROR: disk ${free_gb}G < 300G free" >&2
    exit 4
fi
echo "pre-flight OK: disk=${free_gb}G, 0 magma containers, 0 magma_split procs"

# ------------------- core_pattern for AFL -------------------
if [ "$(cat /proc/sys/kernel/core_pattern)" != "core" ]; then
    echo "setting core_pattern=core (requires sudo)"
    echo core | sudo tee /proc/sys/kernel/core_pattern
fi

# ------------------- launch all 8 campaigns -------------------
declare -a PIDS
declare -a NAMES

launch() {
    local fuzzer=$1 mode=$2 cpu_base=$3
    local wd="$EXP_DIR/${fuzzer}_${TARGET}_${mode}"
    mkdir -p "$wd"
    local extra=""
    local span=$TRIALS
    if [ "$mode" = "split" ]; then
        extra="--split-hours $SPLIT_HOURS --branching $BRANCHING"
        span=$((TRIALS * LEAVES))
    fi
    python3 -u "$ROOT/src/magma_split.py" \
        --fuzzer "$fuzzer" --target "$TARGET" --program "$prog" \
        --mode "$mode" --total-hours "$TOTAL_HOURS" $extra \
        --trials "$TRIALS" --workdir "$wd" --cpu-base "$cpu_base" \
        > "$EXP_DIR/logs/${fuzzer}_${mode}.log" 2>&1 &
    local pid=$!
    PIDS+=($pid)
    NAMES+=("${fuzzer}_${mode}:cpu=${cpu_base}..$((cpu_base + span - 1))")
    echo "  pid=$pid  ${NAMES[${#NAMES[@]}-1]}"
}

cpu=0
for f in "${FUZZERS[@]}"; do
    launch "$f" split "$cpu"
    cpu=$((cpu + TRIALS * LEAVES))
done
for f in "${FUZZERS[@]}"; do
    launch "$f" nosplit "$cpu"
    cpu=$((cpu + TRIALS))
done

echo
echo "launched ${#PIDS[@]} campaigns, cores 0..$((cpu-1)) reserved (${cpu}/188)"
echo "logs:   $EXP_DIR/logs/"

# ------------------- wait with periodic status -------------------
trap 'echo "interrupted, killing PIDS"; kill ${PIDS[@]} 2>/dev/null; exit 130' INT TERM

LAST_STATUS=$(date +%s)
while true; do
    alive=0
    for p in "${PIDS[@]}"; do
        if kill -0 "$p" 2>/dev/null; then alive=$((alive+1)); fi
    done
    if [ $alive -eq 0 ]; then break; fi
    now=$(date +%s)
    # Status every 15 min
    if [ $((now - LAST_STATUS)) -ge 900 ]; then
        echo "=== $(date) ==="
        echo "  alive campaigns: $alive / ${#PIDS[@]}"
        echo "  containers:      $(docker ps --format '{{.Image}}' | grep -c '^magma/')"
        LAST_STATUS=$now
    fi
    sleep 30
done

echo "=== all campaigns finished at $(date) ==="
for i in "${!PIDS[@]}"; do
    log="$EXP_DIR/logs/$(echo ${NAMES[$i]} | cut -d: -f1).log"
    if grep -q "trials OK" "$log" 2>/dev/null; then
        status="OK ($(grep -oP '\d+/\d+ trials OK' $log | tail -1))"
    else
        status="FAIL"
    fi
    printf "  %-40s %s\n" "${NAMES[$i]}" "$status"
done

# ------------------- auto-finalize -------------------
echo
echo "=== running finalize pipeline ==="
MAIN="$EXP_DIR" OUT="$EXP_DIR/finalize" TOTAL_HOURS="$TOTAL_HOURS" \
    bash "$ROOT/scripts/finalize_experiment.sh" 2>&1 | tee "$EXP_DIR/finalize.log"

echo
echo "=== ALL DONE at $(date) ==="
echo "experiment dir: $EXP_DIR"
echo "finalize dir:   $EXP_DIR/finalize"

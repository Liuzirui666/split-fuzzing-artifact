#!/bin/bash
# 3-hour ONLINE splitting experiment on one benchmark.
# Uses the paper's zone-then-bug protocol (§7.4): per-branch sparsity ρ_on(t)
# drives split decisions; a split fires when ρ_H enters a zone AND a new bug
# is triggered on the running trial.
#
# 4 fuzzers × N=5 trials × {online, nosplit} concurrent.
#
# CPU layout (188-core host, B=2, K=3 → up to 8 cores per split trial):
#   afl        online  trials 0..4 → cpus   0..39
#   moptafl    online  trials 0..4 → cpus  40..79
#   aflfast    online  trials 0..4 → cpus  80..119
#   honggfuzz  online  trials 0..4 → cpus 120..159
#   afl        nosplit trials 0..4 → cpus 160..164
#   moptafl    nosplit trials 0..4 → cpus 165..169
#   aflfast    nosplit trials 0..4 → cpus 170..174
#   honggfuzz  nosplit trials 0..4 → cpus 175..179
# Total: 180 / 188 cores reserved, zero overlap.
#
# Usage:  bash run_3h_online.sh <target>  (poppler|openssl)
set -uo pipefail

TARGET=${1:-poppler}
ROOT=/path/to/magma_split
EXP_DIR="$ROOT/experiments/main_3h_online_${TARGET}_$(date +%Y%m%d_%H%M%S)"
TOTAL_HOURS=${TOTAL_HOURS:-3.0}
TRIALS=${TRIALS:-5}
BRANCHING=2
MAX_SPLITS=3    # K
LEAVES=$((BRANCHING ** MAX_SPLITS))  # 8

# Sparsity params scaled for 3h budget (paper defaults were for 23h)
WINDOW_HOURS=0.25          # 15 min windowed rate
PERSIST_HOURS=0.167        # 10 min persistence for zone entry
MIN_GAP_HOURS=0.333        # 20 min min gap between splits
NO_SPLIT_LAST_HOURS=0.333  # no new splits in last 20 min
THRESHOLDS="0.5 0.3 0.15"
POLL_SEC=30

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1
prog=${PROG[$TARGET]}
if [ -z "$prog" ]; then echo "unknown target: $TARGET"; exit 1; fi

FUZZERS=(afl moptafl aflfast honggfuzz)

mkdir -p "$EXP_DIR/logs"
echo "=== 3h ONLINE experiment started at $(date) ==="
echo "target:         $TARGET ($prog)"
echo "fuzzers:        ${FUZZERS[*]}"
echo "trials:         $TRIALS"
echo "duration:       ${TOTAL_HOURS}h per trial"
echo "max splits (K): $MAX_SPLITS  leaves=$LEAVES"
echo "thresholds:     $THRESHOLDS"
echo "window:         ${WINDOW_HOURS}h   persist: ${PERSIST_HOURS}h"
echo "min gap:        ${MIN_GAP_HOURS}h   no-split last: ${NO_SPLIT_LAST_HOURS}h"
echo "experiment:     $EXP_DIR"
echo

# --- pre-flight ---
pre=$(docker ps --format '{{.Image}}' | grep -c '^magma/' || true)
if [ "$pre" -gt 0 ]; then
    echo "!!!! ERROR: $pre magma/* containers already running." >&2; exit 2
fi
py=$(pgrep -f 'python3.*magma_(online_)?split\.py' | wc -l)
if [ "$py" -gt 0 ]; then
    echo "!!!! ERROR: $py orchestrator processes already running." >&2
    pgrep -af 'python3.*magma_(online_)?split\.py' >&2
    exit 3
fi
free_gb=$(df -BG / | tail -1 | awk '{gsub("G","",$4); print $4}')
if [ "$free_gb" -lt 300 ]; then
    echo "!!!! ERROR: disk ${free_gb}G < 300G free." >&2; exit 4
fi
if [ "$(cat /proc/sys/kernel/core_pattern)" != "core" ]; then
    echo "setting core_pattern=core"
    echo core | sudo tee /proc/sys/kernel/core_pattern
fi
echo "pre-flight OK"

# --- launch 8 campaigns ---
declare -a PIDS NAMES

launch() {
    local fuzzer=$1 mode=$2 cpu_base=$3
    local wd="$EXP_DIR/${fuzzer}_${TARGET}_${mode}"
    mkdir -p "$wd"
    local span=$TRIALS
    local extra=""
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
    PIDS+=($pid)
    NAMES+=("${fuzzer}_${mode}:cpu=${cpu_base}..$((cpu_base + span - 1))")
    echo "  pid=$pid  ${NAMES[${#NAMES[@]}-1]}"
}

cpu=0
for f in "${FUZZERS[@]}"; do
    launch "$f" online "$cpu"
    cpu=$((cpu + TRIALS * LEAVES))
done
for f in "${FUZZERS[@]}"; do
    launch "$f" nosplit "$cpu"
    cpu=$((cpu + TRIALS))
done

echo
echo "launched ${#PIDS[@]} campaigns, cores 0..$((cpu-1)) reserved (${cpu}/188)"

# --- wait with periodic status ---
trap 'echo "interrupted, stopping all PIDS"; kill ${PIDS[@]} 2>/dev/null; exit 130' INT TERM

LAST=$(date +%s)
while true; do
    alive=0
    for p in "${PIDS[@]}"; do
        if kill -0 "$p" 2>/dev/null; then alive=$((alive+1)); fi
    done
    if [ $alive -eq 0 ]; then break; fi
    now=$(date +%s)
    if [ $((now - LAST)) -ge 900 ]; then
        echo "=== $(date) ==="
        echo "  alive: $alive/${#PIDS[@]}  containers: $(docker ps --format '{{.Image}}' | grep -c '^magma/')"
        # count splits that have fired so far
        splits=$(grep -c "SPLIT at t=" $EXP_DIR/logs/*.log 2>/dev/null | awk -F: '{s+=$2}END{print s}')
        echo "  splits fired so far: ${splits:-0}"
        LAST=$now
    fi
    sleep 30
done

echo "=== all campaigns finished at $(date) ==="
for i in "${!PIDS[@]}"; do
    name_only=$(echo ${NAMES[$i]} | cut -d: -f1)
    log="$EXP_DIR/logs/${name_only}.log"
    if grep -q "trials OK" "$log" 2>/dev/null; then
        status=$(grep "trials OK" "$log" | tail -1)
        splits=$(grep -c "SPLIT at t=" "$log" 2>/dev/null || echo 0)
        echo "  OK   ${NAMES[$i]}  $status  splits=$splits"
    else
        echo "  FAIL ${NAMES[$i]}"
    fi
done

# --- auto-finalize ---
echo
echo "=== running finalize pipeline ==="
MAIN="$EXP_DIR" OUT="$EXP_DIR/finalize" TOTAL_HOURS="$TOTAL_HOURS" \
    bash "$ROOT/scripts/finalize_experiment.sh" 2>&1 | tee "$EXP_DIR/finalize.log"

echo
echo "=== ALL DONE at $(date) ==="
echo "experiment:  $EXP_DIR"
echo "finalize:    $EXP_DIR/finalize"

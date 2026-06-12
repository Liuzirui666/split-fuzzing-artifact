#!/bin/bash
# 5-hour ONLINE splitting experiment on openssl.
# Uses paper §7.4 zone-then-bug protocol (relaxed: split on zone-entry if any
# bug was found since last split).
#
# 4 fuzzers × N=5 trials × {online, nosplit} concurrent.
# CPU layout (K=3 → 8 leaves):
#   afl        online  trials 0..4 → cpus   0..39
#   moptafl    online  trials 0..4 → cpus  40..79
#   aflfast    online  trials 0..4 → cpus  80..119
#   honggfuzz  online  trials 0..4 → cpus 120..159
#   afl        nosplit trials 0..4 → cpus 160..164
#   moptafl    nosplit trials 0..4 → cpus 165..169
#   aflfast    nosplit trials 0..4 → cpus 170..174
#   honggfuzz  nosplit trials 0..4 → cpus 175..179
# 180/188 cores, no overlap.
set -uo pipefail

ROOT=/path/to/magma_split
TARGET=${TARGET:-openssl}
EXP_DIR="$ROOT/experiments/main_5h_online_${TARGET}_$(date +%Y%m%d_%H%M%S)"
TOTAL_HOURS=${TOTAL_HOURS:-5.0}
TRIALS=${TRIALS:-5}
BRANCHING=2
MAX_SPLITS=3
LEAVES=$((BRANCHING ** MAX_SPLITS))

# Tighter online params so splits fire on early-bug fuzzers too.
WINDOW_HOURS=0.05         # 3 min
PERSIST_HOURS=0.05        # 3 min
MIN_GAP_HOURS=0.167       # 10 min
NO_SPLIT_LAST_HOURS=0.25  # 15 min (~5% of T=5h)
THRESHOLDS="0.5 0.3 0.15"
POLL_SEC=30

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1
prog=${PROG[$TARGET]}
if [ -z "$prog" ]; then echo "unknown target: $TARGET"; exit 1; fi

FUZZERS=(afl moptafl aflfast honggfuzz)

mkdir -p "$EXP_DIR/logs"
echo "=== 5h ONLINE experiment on $TARGET started at $(date) ==="
echo "target=$TARGET  prog=$prog  T=${TOTAL_HOURS}h  N=$TRIALS  K=$MAX_SPLITS"
echo "online params: window=${WINDOW_HOURS}h persist=${PERSIST_HOURS}h"
echo "               min_gap=${MIN_GAP_HOURS}h  no_split_last=${NO_SPLIT_LAST_HOURS}h"
echo "               thresholds=$THRESHOLDS"
echo "experiment: $EXP_DIR"

# Pre-flight
pre=$(docker ps --format '{{.Image}}' | grep -c '^magma/' || true)
if [ "$pre" -gt 0 ]; then echo "!!! $pre magma containers running, abort" >&2; exit 2; fi
py=$(pgrep -f 'python3.*magma_(online_)?split\.py' | wc -l)
if [ "$py" -gt 0 ]; then echo "!!! $py orchestrator procs running" >&2; exit 3; fi
free_gb=$(df -BG / | tail -1 | awk '{gsub("G","",$4); print $4}')
if [ "$free_gb" -lt 300 ]; then echo "!!! disk ${free_gb}G<300G" >&2; exit 4; fi
if [ "$(cat /proc/sys/kernel/core_pattern)" != "core" ]; then
    echo core | sudo tee /proc/sys/kernel/core_pattern
fi
echo "pre-flight OK"

declare -a PIDS NAMES
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
    PIDS+=($pid)
    NAMES+=("${fuzzer}_${mode}:cpu=${cpu_base}..$((cpu_base + span - 1))")
    echo "  pid=$pid  ${NAMES[${#NAMES[@]}-1]}"
}

cpu=0
for f in "${FUZZERS[@]}"; do launch "$f" online "$cpu"; cpu=$((cpu + TRIALS * LEAVES)); done
for f in "${FUZZERS[@]}"; do launch "$f" nosplit "$cpu"; cpu=$((cpu + TRIALS)); done

echo "launched ${#PIDS[@]} campaigns, cores 0..$((cpu-1))  (${cpu}/188)"

trap 'echo "interrupted"; kill ${PIDS[@]} 2>/dev/null; exit 130' INT TERM
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
        echo "  alive $alive/${#PIDS[@]}  containers $(docker ps --format '{{.Image}}' | grep -c '^magma/')"
        splits=$(grep -h "SPLIT at t=" $EXP_DIR/logs/*_online.log 2>/dev/null | wc -l)
        echo "  total splits fired: $splits"
        LAST=$now
    fi
    sleep 30
done

echo "=== all done at $(date) ==="
for i in "${!PIDS[@]}"; do
    name_only=$(echo ${NAMES[$i]} | cut -d: -f1)
    log="$EXP_DIR/logs/${name_only}.log"
    if grep -q "trials OK" "$log" 2>/dev/null; then
        echo "  OK   ${NAMES[$i]}  $(grep 'trials OK' $log | tail -1)"
    else
        echo "  FAIL ${NAMES[$i]}"
    fi
done

echo; echo "=== finalize pipeline ==="
MAIN="$EXP_DIR" OUT="$EXP_DIR/finalize" TOTAL_HOURS="$TOTAL_HOURS" \
    bash "$ROOT/scripts/finalize_experiment.sh" 2>&1 | tee "$EXP_DIR/finalize.log"

echo; echo "=== paper-style figures (weighted 1/k_t) ==="
python3 "$ROOT/src/magma_figures.py" \
    --input "$EXP_DIR/finalize/all_runs.csv" \
    --pocs "$EXP_DIR/finalize/all_pocs.csv" \
    --execs-root "$EXP_DIR" \
    --total-hours "$TOTAL_HOURS" \
    --outdir "$EXP_DIR/figures" \
    --logdir "$EXP_DIR/logs" 2>&1 | tee "$EXP_DIR/figures.log"

echo; echo "=== ALL DONE $(date) ==="
echo "experiment: $EXP_DIR"

#!/bin/bash
# Run the missing 5-trial 12h openssl batch B, then merge with the
# already-stitched batch A CSVs (combined_12h_openssl_batchA)
# to produce a 10-trial combined directory with trial_id's 0..9.
#
# Batch A is available as finalize/ CSVs only,
# so we cannot use combine_12h_batches.sh; instead
# we concatenate at the CSV level, remapping batch-B trial-{0..4} to
# trial-{5..9} so the two batches share a contiguous trial_id space.
set -uo pipefail

ROOT=/path/to/magma_split
BATCH_A_COMB=/path/to/magma_split/experiments/combined_12h_openssl_batchA
TOTAL_HOURS=12.0
TRIALS=5
BRANCHING=2
MAX_SPLITS=3
LEAVES=$((BRANCHING ** MAX_SPLITS))

WINDOW_HOURS=0.05
PERSIST_HOURS=0.05
MIN_GAP_HOURS=0.167
NO_SPLIT_LAST_HOURS=0.25
THRESHOLDS="0.5 0.3 0.15"
POLL_SEC=30

TARGET=openssl
prog=asn1
FUZZERS=(afl moptafl aflfast honggfuzz)

EXP_DIR="$ROOT/experiments/main_12h_online_${TARGET}_batchB_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$EXP_DIR/logs"

echo "################################################################"
echo "# ${TARGET} batch B — T=${TOTAL_HOURS}h N=${TRIALS} K=${MAX_SPLITS}"
echo "# start: $(date)"
echo "# dir:   $EXP_DIR"
echo "################################################################"

# Pre-flight
if [ "$(docker ps --format '{{.Image}}' | grep -c '^magma/')" -gt 0 ]; then
    echo "!!! magma containers running" >&2; exit 2
fi
if [ "$(pgrep -f 'python3.*magma_online_split\.py' | wc -l)" -gt 0 ]; then
    echo "!!! orchestrator procs running" >&2; exit 3
fi
free_gb=$(df -BG / | tail -1 | awk '{gsub("G","",$4); print $4}')
if [ "$free_gb" -lt 300 ]; then echo "!!! disk ${free_gb}G<300" >&2; exit 4; fi
if [ "$(cat /proc/sys/kernel/core_pattern)" != "core" ]; then
    echo core | sudo tee /proc/sys/kernel/core_pattern
fi

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
    PIDS+=($pid); NAMES+=("${fuzzer}_${mode}:cpu=${cpu_base}..$((cpu_base+span-1))")
    echo "  pid=$pid  ${NAMES[${#NAMES[@]}-1]}"
}

cpu=0
for f in "${FUZZERS[@]}"; do launch "$f" online "$cpu"; cpu=$((cpu+TRIALS*LEAVES)); done
for f in "${FUZZERS[@]}"; do launch "$f" nosplit "$cpu"; cpu=$((cpu+TRIALS)); done
echo "launched ${#PIDS[@]} campaigns, cores 0..$((cpu-1)) (${cpu}/188)"

LAST=$(date +%s)
while true; do
    alive=0
    for p in "${PIDS[@]}"; do
        if kill -0 "$p" 2>/dev/null; then alive=$((alive+1)); fi
    done
    if [ $alive -eq 0 ]; then break; fi
    now=$(date +%s)
    if [ $((now - LAST)) -ge 1800 ]; then
        echo "[$(date)] alive=$alive/${#PIDS[@]}  containers=$(docker ps --format '{{.Image}}' | grep -c '^magma/')"
        splits=$(grep -h "SPLIT at t=" $EXP_DIR/logs/*_online.log 2>/dev/null | wc -l)
        echo "  splits fired: $splits"
        LAST=$now
    fi
    sleep 60
done
echo "[$(date)] ${TARGET} batch B campaigns finished"

# Clean up any stray containers
for cid in $(docker ps --format '{{.ID}} {{.Image}}' | awk '$2 ~ /^magma\// {print $1}'); do
    docker rm -f $cid >/dev/null 2>&1
done

# Finalize batch B alone
echo; echo "--- finalize batch B ---"
MAIN="$EXP_DIR" OUT="$EXP_DIR/finalize" TOTAL_HOURS="$TOTAL_HOURS" \
    bash "$ROOT/scripts/finalize_experiment.sh" 2>&1 | tee "$EXP_DIR/finalize.log" | tail -20

# Merge with batch A's CSVs at the CSV level
echo; echo "--- merge batch A + B into combined 10-trial dir ---"
COMB_FINAL="$ROOT/experiments/combined_12h_openssl_10trials_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$COMB_FINAL/finalize"

python3 - <<PY
import pandas as pd
from pathlib import Path

batch_a_dir = Path("$BATCH_A_COMB/finalize")
batch_b_dir = Path("$EXP_DIR/finalize")
out_dir = Path("$COMB_FINAL/finalize")

def remap(df, col="trial_id"):
    # trial-0..trial-4 in batch B => trial-5..trial-9
    df = df.copy()
    mapping = {f"trial-{i}": f"trial-{i+5}" for i in range(5)}
    df[col] = df[col].map(lambda v: mapping.get(v, v))
    return df

# all_runs
a = pd.read_csv(batch_a_dir / "all_runs.csv")
b = pd.read_csv(batch_b_dir / "all_runs.csv")
b = remap(b, "trial_id")
merged = pd.concat([a, b], ignore_index=True)
merged.to_csv(out_dir / "all_runs.csv", index=False)
print(f"  all_runs: A={len(a):,} + B={len(b):,} = {len(merged):,}")

# all_pocs
a = pd.read_csv(batch_a_dir / "all_pocs.csv")
b = pd.read_csv(batch_b_dir / "all_pocs.csv")
b = remap(b, "trial_id")
merged = pd.concat([a, b], ignore_index=True)
merged.to_csv(out_dir / "all_pocs.csv", index=False)
print(f"  all_pocs: A={len(a):,} + B={len(b):,} = {len(merged):,}")
PY

# Exec-axis figures require raw trial dirs; a batch available as CSVs only
# cannot contribute them. Skip --execs-root for the merged figure run;
# time-axis figures remain valid.

# Survival + summary.json for merged
MAIN="$COMB_FINAL" OUT="$COMB_FINAL/finalize" TOTAL_HOURS=12.0 \
    python3 "$ROOT/src/magma_survival.py" \
    --input "$COMB_FINAL/finalize/all_runs.csv" --total-hours 12.0 \
    --outdir "$COMB_FINAL/finalize/surv" 2>&1 | tail -5
python3 "$ROOT/src/magma_exp2json.py" \
    --input "$COMB_FINAL/finalize/all_runs.csv" \
    --output "$COMB_FINAL/finalize/summary.json" 2>&1 | tail -5

# Figures on merged 10-trial set (time-axis only)
echo "--- figures on merged 10-trial set ---"
python3 "$ROOT/src/magma_figures.py" \
    --input "$COMB_FINAL/finalize/all_runs.csv" \
    --pocs "$COMB_FINAL/finalize/all_pocs.csv" \
    --total-hours 12.0 \
    --outdir "$COMB_FINAL/figures" \
    --logdir "$EXP_DIR/logs" 2>&1 | tee "$COMB_FINAL/figures.log" | tail -10

echo; echo "=== batch B + merged 10-trial complete ==="
echo "  batch B raw:   $EXP_DIR"
echo "  merged 10-tr:  $COMB_FINAL"
echo "  done at $(date)"

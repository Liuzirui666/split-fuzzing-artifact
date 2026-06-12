#!/bin/bash
# Batch C: 5-trial 12h poppler + openssl (4 fuzzers × {online,nosplit}),
# then CSV-level merge with existing 10-trial combined dirs to produce
# 15-trial merged dirs for both benchmarks.
#
# Existing 10-trial combined dirs (trials 0..9):
#   combined_12h_poppler_10trials
#   combined_12h_openssl_10trials
# Batch C adds trials 10..14 (batch C trial-0..trial-4 remapped +10).
#
# Time-axis figures work for the merged 15-trial set; exec-axis figures
# require raw trial dirs and cover only the trials whose raw dirs exist.
set -uo pipefail

ROOT=/path/to/magma_split
POPPLER_10="$ROOT/experiments/combined_12h_poppler_10trials"
OPENSSL_10="$ROOT/experiments/combined_12h_openssl_10trials"

MASTER_LOG="$ROOT/experiments/batchC_master_$(date +%Y%m%d_%H%M%S).log"
WRAPPER_START_EPOCH=$(date +%s)

echo "################################################################" | tee "$MASTER_LOG"
echo "# batch C master launcher (5 trials × 12h × {poppler, openssl})" | tee -a "$MASTER_LOG"
echo "# start: $(date)"                                                 | tee -a "$MASTER_LOG"
echo "#   poppler 10-trial: $POPPLER_10"                                | tee -a "$MASTER_LOG"
echo "#   openssl 10-trial: $OPENSSL_10"                                | tee -a "$MASTER_LOG"
echo "#   master log:       $MASTER_LOG"                                | tee -a "$MASTER_LOG"
echo "################################################################" | tee -a "$MASTER_LOG"

# Sanity: both prior 10-trial dirs must exist
for d in "$POPPLER_10" "$OPENSSL_10"; do
    if [ ! -f "$d/finalize/all_runs.csv" ]; then
        echo "!!! missing $d/finalize/all_runs.csv" >&2
        exit 5
    fi
done

# Run the 5-trial 12h chain on both benchmarks sequentially (~24h wall time)
echo | tee -a "$MASTER_LOG"
echo "=== launching run_12h_chain.sh (poppler then openssl) ===" | tee -a "$MASTER_LOG"
bash "$ROOT/scripts/run_12h_chain.sh" 2>&1 | tee -a "$MASTER_LOG"
CHAIN_RC=${PIPESTATUS[0]}
echo "=== chain exit rc=$CHAIN_RC at $(date) ===" | tee -a "$MASTER_LOG"

# Locate batch C EXP_DIRs (created during this wrapper's run)
POP_BATCHC=$(find "$ROOT/experiments" -maxdepth 1 -type d \
    -name "main_12h_online_poppler_*" -newer "$MASTER_LOG" -print 2>/dev/null \
    | sort | tail -1)
OSSL_BATCHC=$(find "$ROOT/experiments" -maxdepth 1 -type d \
    -name "main_12h_online_openssl_*" -newer "$MASTER_LOG" -print 2>/dev/null \
    | sort | tail -1)

# Fallback: newest-mtime match if -newer missed
[ -z "$POP_BATCHC" ] && POP_BATCHC=$(ls -1dt "$ROOT/experiments/main_12h_online_poppler_"* 2>/dev/null | head -1)
[ -z "$OSSL_BATCHC" ] && OSSL_BATCHC=$(ls -1dt "$ROOT/experiments/main_12h_online_openssl_"* 2>/dev/null | head -1)

echo | tee -a "$MASTER_LOG"
echo "batch C poppler raw: $POP_BATCHC"  | tee -a "$MASTER_LOG"
echo "batch C openssl raw: $OSSL_BATCHC" | tee -a "$MASTER_LOG"

merge_batch() {
    local target=$1 batchc=$2 prev10=$3
    local out="$ROOT/experiments/combined_12h_${target}_15trials_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$out/finalize"
    echo                                                               | tee -a "$MASTER_LOG"
    echo "--- merging $target ---"                                     | tee -a "$MASTER_LOG"
    echo "  batch C:  $batchc"                                         | tee -a "$MASTER_LOG"
    echo "  prev 10:  $prev10"                                         | tee -a "$MASTER_LOG"
    echo "  output:   $out"                                            | tee -a "$MASTER_LOG"

    if [ ! -f "$batchc/finalize/all_runs.csv" ]; then
        echo "!!! $batchc/finalize/all_runs.csv missing — skip merge" | tee -a "$MASTER_LOG"
        return 1
    fi

    python3 - <<PY 2>&1 | tee -a "$MASTER_LOG"
import pandas as pd
from pathlib import Path
a = Path("$prev10/finalize")
c = Path("$batchc/finalize")
o = Path("$out/finalize")

def remap(df, col="trial_id"):
    df = df.copy()
    # batch C trial-0..trial-4  ->  trial-10..trial-14
    mapping = {f"trial-{i}": f"trial-{i+10}" for i in range(5)}
    df[col] = df[col].map(lambda v: mapping.get(v, v))
    return df

for name in ("all_runs.csv", "all_pocs.csv"):
    da = pd.read_csv(a / name)
    dc = pd.read_csv(c / name)
    dc = remap(dc)
    m = pd.concat([da, dc], ignore_index=True)
    m.to_csv(o / name, index=False)
    print(f"  {name}: prev10={len(da):,} + batchC={len(dc):,} = {len(m):,}")

r = pd.read_csv(o / "all_runs.csv")
print("  trial_ids in merged:", sorted(r['trial_id'].unique()))
PY

    # Survival + summary on the merged 15-trial CSV
    python3 "$ROOT/src/magma_survival.py" \
        --input "$out/finalize/all_runs.csv" --total-hours 12.0 \
        --outdir "$out/finalize/surv" 2>&1 | tail -5 | tee -a "$MASTER_LOG"
    python3 "$ROOT/src/magma_exp2json.py" \
        --input "$out/finalize/all_runs.csv" \
        --output "$out/finalize/summary.json" 2>&1 | tail -5 | tee -a "$MASTER_LOG"

    # Time-axis figures on merged 15-trial set
    python3 "$ROOT/src/magma_figures.py" \
        --input "$out/finalize/all_runs.csv" \
        --pocs "$out/finalize/all_pocs.csv" \
        --total-hours 12.0 \
        --outdir "$out/figures" \
        --logdir "$batchc/logs" 2>&1 | tee "$out/figures.log" | tail -10 | tee -a "$MASTER_LOG"

    echo "  done: $out" | tee -a "$MASTER_LOG"
}

merge_batch poppler "$POP_BATCHC"  "$POPPLER_10"
merge_batch openssl "$OSSL_BATCHC" "$OPENSSL_10"

echo                                                          | tee -a "$MASTER_LOG"
echo "=== batch C + 15-trial merges DONE at $(date) ==="      | tee -a "$MASTER_LOG"
echo "wall time: $(( ($(date +%s) - WRAPPER_START_EPOCH) / 3600 ))h" | tee -a "$MASTER_LOG"
echo "batch C raw:    $POP_BATCHC"                            | tee -a "$MASTER_LOG"
echo "                $OSSL_BATCHC"                           | tee -a "$MASTER_LOG"
echo "merged 15-tr:   (grep 'done:' above for paths)"         | tee -a "$MASTER_LOG"

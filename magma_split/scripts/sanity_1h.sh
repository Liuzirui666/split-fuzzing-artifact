#!/bin/bash
# 1h sanity: 4 fuzzers x 2 targets, 1 trial each, BOTH split + nosplit.
# CPU plan:
#   - nosplit: 1 CPU/pair * 8 pairs = cores 0..7
#   - split (K=1, branching=2): 2 CPUs/pair at peak * 8 pairs = cores 8..23
# All 16 campaigns launch concurrently; wall clock ~= 1h.
set -uo pipefail

ROOT=/path/to/magma_split
EXP_DIR="$ROOT/experiments/sanity_1h"
mkdir -p "$EXP_DIR/logs"

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1

FUZZERS=(afl aflfast aflplusplus honggfuzz)
TARGETS=(poppler openssl)

declare -a PIDS
declare -a NAMES

start_one() {
    local fuzzer=$1 target=$2 mode=$3 cpu_base=$4
    local prog=${PROG[$target]}
    local wd="$EXP_DIR/${fuzzer}_${target}_${mode}"
    rm -rf "$wd"
    mkdir -p "$wd"
    local extra=""
    if [ "$mode" = "split" ]; then
        extra="--split-hours 0.5"
    fi
    python3 -u "$ROOT/src/magma_split.py" \
        --fuzzer "$fuzzer" --target "$target" --program "$prog" \
        --mode "$mode" --total-hours 1.0 $extra \
        --trials 1 --workdir "$wd" --cpu-base "$cpu_base" \
        > "$EXP_DIR/logs/${fuzzer}_${target}_${mode}.log" 2>&1 &
    PIDS+=($!)
    NAMES+=("${fuzzer}_${target}_${mode}:cpu=${cpu_base}")
}

# cpu 0..7: nosplit
cpu=0
for f in "${FUZZERS[@]}"; do
  for t in "${TARGETS[@]}"; do
    start_one "$f" "$t" nosplit "$cpu"
    cpu=$((cpu+1))
  done
done

# cpu 8..23: split (peak 2 CPUs per pair)
for f in "${FUZZERS[@]}"; do
  for t in "${TARGETS[@]}"; do
    start_one "$f" "$t" split "$cpu"
    cpu=$((cpu+2))
  done
done

echo "Launched ${#PIDS[@]} campaigns:"
for i in "${!PIDS[@]}"; do echo "  pid=${PIDS[i]} ${NAMES[i]}"; done

echo "Waiting for all to complete (~1h)..."
for p in "${PIDS[@]}"; do wait $p; done
echo "=== sanity done $(date) ==="

# Summary
for f in "${FUZZERS[@]}"; do
  for t in "${TARGETS[@]}"; do
    for m in nosplit split; do
      tag="${f}_${t}_${m}"
      wd="$EXP_DIR/$tag"
      if grep -q "1/1 trials OK" "$EXP_DIR/logs/${tag}.log" 2>/dev/null; then
        monfiles=$(find "$wd" -path '*/monitor/*' -type f 2>/dev/null | wc -l)
        bugs_triggered=$(python3 "$ROOT/src/magma_report.py" \
            --workdir "$wd" --fuzzer "$f" --target "$t" --mode "$m" \
            --output "$EXP_DIR/${tag}.csv" 2>&1 | tail -1)
        echo "OK    $tag   monitor_files=$monfiles   $bugs_triggered"
      else
        echo "FAIL  $tag   $(tail -3 $EXP_DIR/logs/${tag}.log | tr '\n' ' ')"
      fi
    done
  done
done

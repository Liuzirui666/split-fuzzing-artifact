#!/bin/bash
# End-to-end post-experiment pipeline.
# For every subdir <fuzzer>_<target>_<mode> in main/, run:
#   1. monitor CSV stitching  (magma_report.py)
#   2. PoC extraction (Detected metric)  (magma_poc_extract.py)
# Then combine across all:
#   3. survival analysis (magma_survival.py)
#   4. exp2json JSON for Magma's own tool (magma_exp2json.py)
set -uo pipefail

ROOT=/path/to/magma_split
MAIN=${MAIN:-$ROOT/experiments/main}
OUT=${OUT:-$MAIN/finalize}
TOTAL_HOURS=${TOTAL_HOURS:-24.0}
POC_PARALLEL=${POC_PARALLEL:-8}
mkdir -p "$OUT"

declare -A PROG
PROG[poppler]=pdf_fuzzer
PROG[openssl]=asn1
PROG[sqlite3]=sqlite3_fuzz
PROG[libsndfile]=sndfile_fuzzer
PROG[libxml2]=libxml2_xml_read_memory_fuzzer
PROG[php]=json
PROG[libtiff]=tiff_read_rgba_fuzzer
PROG[libpng]=libpng_read_fuzzer
PROG[lua]=lua

echo "=== [1/4] Monitor stitching (all runs) ==="
python3 "$ROOT/src/magma_report.py" --workdir "$MAIN" --multi \
    --output "$OUT/all_runs.csv"

echo
echo "=== [2/4] PoC extraction per (fuzzer,target,mode) ==="
for d in "$MAIN"/*_*/; do
    name=$(basename "$d")
    parts=(${name//_/ })
    # handle fuzzer names with no underscores in our set
    mode=${name##*_}
    rest=${name%_*}
    target=${rest##*_}
    fuzzer=${rest%_*}
    prog=${PROG[$target]}
    if [ -z "$prog" ]; then
        echo "  skip $name (unknown target $target)"
        continue
    fi
    echo "  $name"
    python3 "$ROOT/src/magma_poc_extract.py" \
        --workdir "$d" --fuzzer "$fuzzer" --target "$target" \
        --program "$prog" --mode "$mode" \
        --output "$OUT/poc_${name}.csv" --parallel "$POC_PARALLEL" \
        > "$OUT/poc_${name}.log" 2>&1 &
done
wait
cat "$OUT"/poc_*.csv | awk 'NR==1 || FNR!=1' > "$OUT/all_pocs.csv"

echo
echo "=== [3/4] Survival analysis ==="
python3 "$ROOT/src/magma_survival.py" \
    --input "$OUT/all_runs.csv" --total-hours "$TOTAL_HOURS" \
    --outdir "$OUT/surv"

echo
echo "=== [4/4] exp2json JSON (Magma-compatible) ==="
python3 "$ROOT/src/magma_exp2json.py" \
    --input "$OUT/all_runs.csv" --output "$OUT/summary.json"

echo
echo "=== DONE ==="
echo "Monitor CSV:      $OUT/all_runs.csv"
echo "PoCs CSV:         $OUT/all_pocs.csv"
echo "Survival:         $OUT/surv/{summary,trial_events,split_vs_nosplit}.csv"
echo "Magma JSON:       $OUT/summary.json"
echo ""
echo "To run Magma's own survival analysis on our data:"
echo "  python3 $ROOT/magma/tools/benchd/survival_analysis.py \\"
trial_length_sec=$(python3 -c "print(int($TOTAL_HOURS*3600))")
echo "    --num-trials 10 --trial-length $trial_length_sec \\"
echo "    $OUT/summary.json > $OUT/magma_surv.csv"

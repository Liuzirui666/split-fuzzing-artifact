#!/bin/bash
# Build the full magma matrix per captainrc:
#   FUZZERS = afl aflfast moptafl aflplusplus fairfuzz honggfuzz
#   PROGRAMS = libpng libsndfile libtiff libxml2 lua openssl php poppler sqlite3
# 6 × 9 = 54 pairs. Skip pairs whose image already exists.
#
# - Sequential builds, one pair at a time (no parallel docker build pressure).
# - DOCKER_BUILDKIT=0 (no build cache).
# - Per-pair log under build_logs/.
# - Disk safety: abort before each build if free < 350G.
# - Failures recorded in failures.txt, do not stop the run.
set -uo pipefail

ROOT=/path/to/magma_split
LOGDIR="$ROOT/build_logs"
mkdir -p "$LOGDIR"

# Paper roster: AFL, AFLFast, AFL++, MOpt, LibFuzzer,
# Honggfuzz are buildable here; AFLSmart has no recipe in this checkout.
# fairfuzz dropped — not in the paper's fuzzer set.
FUZZERS=(afl aflfast moptafl aflplusplus honggfuzz libfuzzer)
PROGRAMS=(libpng libsndfile libtiff libxml2 lua openssl php poppler sqlite3)

SUMMARY="$LOGDIR/summary_$(date +%Y%m%d_%H%M%S).log"
FAILURES="$LOGDIR/failures.txt"
: > "$FAILURES"

START_EPOCH=$(date +%s)

echo "################################################################" | tee "$SUMMARY"
echo "# magma full-matrix build (6 × 9 = 54 pairs)"                     | tee -a "$SUMMARY"
echo "# start: $(date)"                                                 | tee -a "$SUMMARY"
echo "# log dir: $LOGDIR"                                               | tee -a "$SUMMARY"
echo "# summary: $SUMMARY"                                              | tee -a "$SUMMARY"
echo "################################################################" | tee -a "$SUMMARY"

built=0; skipped=0; failed=0; idx=0
total=$((${#FUZZERS[@]} * ${#PROGRAMS[@]}))

for f in "${FUZZERS[@]}"; do
    for t in "${PROGRAMS[@]}"; do
        idx=$((idx + 1))
        img="magma/$f/$t"

        # Skip if image already exists
        if docker image inspect "$img:latest" >/dev/null 2>&1; then
            echo "[$idx/$total] SKIP $img (already built)" | tee -a "$SUMMARY"
            skipped=$((skipped + 1))
            continue
        fi

        # Disk safety
        free_gb=$(df -BG / | tail -1 | awk '{gsub("G","",$4); print $4}')
        if [ "$free_gb" -lt 350 ]; then
            echo "[$idx/$total] ABORT: disk ${free_gb}G < 350G floor" | tee -a "$SUMMARY"
            echo "  remaining pairs not attempted; run again with more disk." | tee -a "$SUMMARY"
            break 2
        fi

        log="$LOGDIR/${f}_${t}.log"
        echo "[$idx/$total] BUILD $img  (disk=${free_gb}G)" | tee -a "$SUMMARY"
        echo "  start: $(date +%H:%M:%S)  log: $log"        | tee -a "$SUMMARY"

        t0=$(date +%s)
        DOCKER_BUILDKIT=0 FUZZER="$f" TARGET="$t" \
            bash "$ROOT/magma/tools/captain/build.sh" \
            > "$log" 2>&1
        rc=$?
        dt=$(( $(date +%s) - t0 ))

        if [ $rc -eq 0 ] && docker image inspect "$img:latest" >/dev/null 2>&1; then
            size=$(docker image inspect "$img:latest" --format '{{.Size}}' 2>/dev/null \
                | awk '{printf "%.1fGB", $1/1e9}')
            echo "  OK  ${dt}s  ${size}" | tee -a "$SUMMARY"
            built=$((built + 1))
        else
            echo "  FAIL rc=$rc  ${dt}s  see $log" | tee -a "$SUMMARY"
            echo "$f $t  rc=$rc  $log" >> "$FAILURES"
            failed=$((failed + 1))
        fi
    done
done

elapsed_h=$(( ($(date +%s) - START_EPOCH) / 3600 ))
elapsed_m=$(( (($(date +%s) - START_EPOCH) % 3600) / 60 ))

echo                                                                     | tee -a "$SUMMARY"
echo "################################################################" | tee -a "$SUMMARY"
echo "# DONE at $(date) — wall ${elapsed_h}h${elapsed_m}m"               | tee -a "$SUMMARY"
echo "#   built:   $built"                                               | tee -a "$SUMMARY"
echo "#   skipped: $skipped (already had image)"                         | tee -a "$SUMMARY"
echo "#   failed:  $failed"                                              | tee -a "$SUMMARY"
echo "#   final disk:"                                                   | tee -a "$SUMMARY"
df -BG / | tail -1                                                       | tee -a "$SUMMARY"
echo "################################################################" | tee -a "$SUMMARY"

if [ $failed -gt 0 ]; then
    echo "Failures listed in $FAILURES:" | tee -a "$SUMMARY"
    cat "$FAILURES" | tee -a "$SUMMARY"
fi

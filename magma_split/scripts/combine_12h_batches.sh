#!/bin/bash
# After run_12h_10trials.sh produces two batches (A, B) per benchmark,
# symlink their trial dirs into a combined_12h_<benchmark>/ workdir with
# renamed trial indices 0..4 (batch A) and 5..9 (batch B), and run the
# finalize + figures pipeline on the combined data.
set -uo pipefail

ROOT=/path/to/magma_split
EXPS="$ROOT/experiments"
FUZZERS=(afl moptafl aflfast honggfuzz)
MODES=(online nosplit)
BENCHMARKS=(poppler openssl)

for target in "${BENCHMARKS[@]}"; do
    # Find the two most recent 12h experiment dirs for this benchmark.
    mapfile -t dirs < <(ls -1d "$EXPS"/main_12h_online_${target}_* 2>/dev/null | sort)
    if [ "${#dirs[@]}" -lt 2 ]; then
        echo "!!! ${target}: need 2 batches, found ${#dirs[@]}; skipping" >&2
        continue
    fi
    BATCH_A="${dirs[0]}"
    BATCH_B="${dirs[1]}"
    COMB="$EXPS/combined_12h_${target}_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$COMB/logs"
    echo "== ${target}: combining"
    echo "   batch A: $BATCH_A"
    echo "   batch B: $BATCH_B"
    echo "   output : $COMB"

    for fz in "${FUZZERS[@]}"; do
        for mode in "${MODES[@]}"; do
            sub="${fz}_${target}_${mode}"
            dst="$COMB/$sub"
            mkdir -p "$dst"
            # batch A: trials 0..4 copied as-is
            for i in 0 1 2 3 4; do
                src="$BATCH_A/$sub/trial-$i"
                [ -d "$src" ] && ln -s "$src" "$dst/trial-$i"
            done
            # batch B: trials 0..4 renamed to 5..9
            for i in 0 1 2 3 4; do
                src="$BATCH_B/$sub/trial-$i"
                tgt=$((i+5))
                [ -d "$src" ] && ln -s "$src" "$dst/trial-$tgt"
            done
        done
    done

    # logs directory: just copy both (per-batch)
    [ -d "$BATCH_A/logs" ] && cp -a "$BATCH_A/logs/"* "$COMB/logs/" 2>/dev/null || true
    [ -d "$BATCH_B/logs" ] && for f in "$BATCH_B/logs/"*; do
        bn=$(basename "$f")
        cp -a "$f" "$COMB/logs/${bn%.log}_B.log" 2>/dev/null || true
    done

    echo "== finalize ${target}"
    MAIN="$COMB" OUT="$COMB/finalize" TOTAL_HOURS=12.0 \
        bash "$ROOT/scripts/finalize_experiment.sh" 2>&1 | tee "$COMB/finalize.log" | tail -15

    echo "== figures ${target}"
    python3 "$ROOT/src/magma_figures.py" \
        --input "$COMB/finalize/all_runs.csv" \
        --pocs "$COMB/finalize/all_pocs.csv" \
        --execs-root "$COMB" \
        --total-hours 12.0 \
        --outdir "$COMB/figures" \
        --logdir "$COMB/logs" 2>&1 | tee "$COMB/figures.log" | tail -10

    echo "== ${target} combined done: $COMB"
done

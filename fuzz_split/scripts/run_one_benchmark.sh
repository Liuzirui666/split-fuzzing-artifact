#!/usr/bin/env bash
# =============================================================================
# run_one_benchmark.sh — Build & verify ALL 9 fuzzers for ONE benchmark.
#
# Runs each fuzzer SEQUENTIALLY: build → short test via basic FuzzBench
# run_experiment.py (NOT our split wrapper) → verify → cleanup → next fuzzer.
#
# HARD-CODED RULES (NEVER CHANGE):
#   1. ONE benchmark at a time. NEVER run multiple benchmarks in parallel.
#   2. For each benchmark: build and test all 9 fuzzers ONE BY ONE.
#   3. Test builds using basic FuzzBench run_experiment.py directly.
#      NEVER use our split wrapper (src.cli / parallel_runner) for testing.
#   4. ALWAYS build with --no-cache (in generated.mk) + DOCKER_BUILDKIT=0.
#   5. Check disk before AND after every build/experiment. ABORT if <300GB.
#   6. After each fuzzer×benchmark pair finishes: delete its 4 pair images.
#   7. After ALL 9 fuzzers succeed: delete the benchmark image.
#   8. NEVER run any docker prune command (builder/image/system prune).
#   9. NEVER touch non-fuzzbench images/containers.
#  10. ALWAYS keep base-image (gcr.io/fuzzbench/base-image).
#  11. Container cleanup: ONLY kill dispatcher-d-{experiment}* containers.
#
# Usage:
#   ./scripts/run_one_benchmark.sh <benchmark> [experiment_prefix] [test_seconds]
#
# Example:
#   ./scripts/run_one_benchmark.sh arrow_parquet-arrow-fuzz ar 1800
# =============================================================================
set -euo pipefail

# =====================================================================
# HARD-CODED CONSTANTS — DO NOT CHANGE
# =====================================================================
MIN_FREE_DISK_GB=300

# DISABLE BUILDKIT: legacy builder creates ZERO build cache.
# BuildKit stores cache entries even with --no-cache, wasting disk.
# NEVER change this back to 1.
export DOCKER_BUILDKIT=0

# The ONLY 9 fuzzers we use. Sequential, one at a time.
FUZZERS=(
    afl
    aflfast
    aflplusplus
    aflsmart
    entropic
    fairfuzz
    honggfuzz
    libfuzzer
    mopt
)

# The 18 paper benchmarks (from 90e59b6, matching the paper)
VALID_BENCHMARKS=(
    arrow_parquet-arrow-fuzz
    aspell_aspell_fuzzer
    ffmpeg_ffmpeg_demuxer_fuzzer
    grok_grk_decompress_fuzzer
    harfbuzz-1.3.2
    libgit2_objects_fuzzer
    libhevc_hevc_dec_fuzzer
    libhtp_fuzz_htp
    libxml2_libxml2_xml_reader_for_file_fuzzer
    matio_matio_fuzzer
    njs_njs_process_script_fuzzer
    openh264_decoder_fuzzer
    php_php-fuzz-parser-2020-07-25
    poppler_pdf_fuzzer
    quickjs_eval-2020-01-05
    stb_stbi_read_fuzzer
    systemd_fuzz-link-parser
    wireshark_fuzzshark_ip
)

# =====================================================================
# SETUP
# =====================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FUZZBENCH_DIR="${PROJECT_DIR}/fuzzbench"
CONFIG_DIR="${PROJECT_DIR}/configs"
TIMESTAMP="$(date +%Y%m%d-%H%M)"

# Arguments
BENCHMARK="${1:?Usage: $0 <benchmark> [experiment_prefix] [test_seconds]}"
EXP_PREFIX="${2:-bt}"
TEST_SECONDS="${3:-900}"

# Conda
eval "$(conda shell.bash hook)"
conda activate fuzz_split
export PYTHONPATH="${FUZZBENCH_DIR}:${PYTHONPATH:-}"

# =====================================================================
# SAFETY FUNCTIONS — HARD-CODED, NO BYPASSES
# =====================================================================

abort_msg() {
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "!!!! ABORT: $1"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo ""
    exit 1
}

check_disk_or_abort() {
    local context="$1"
    local free_gb
    free_gb=$(df --output=avail -BG / | tail -1 | tr -d ' G')
    if [ "$free_gb" -lt "$MIN_FREE_DISK_GB" ]; then
        abort_msg "DISK ${free_gb}GB < ${MIN_FREE_DISK_GB}GB — ${context}"
    fi
    echo "[disk-check] ${free_gb}GB free (min ${MIN_FREE_DISK_GB}GB) — ${context}"
}

validate_benchmark() {
    local bm="$1"
    for valid in "${VALID_BENCHMARKS[@]}"; do
        if [ "$bm" = "$valid" ]; then
            return 0
        fi
    done
    abort_msg "Invalid benchmark: ${bm}. Must be one of: ${VALID_BENCHMARKS[*]}"
}

# Delete ONLY the 4 per-pair images. Nothing else.
delete_pair_images() {
    local fuzzer="$1"
    local benchmark="$2"
    local images=(
        "gcr.io/fuzzbench/builders/${fuzzer}/${benchmark}-intermediate"
        "gcr.io/fuzzbench/builders/${fuzzer}/${benchmark}"
        "gcr.io/fuzzbench/runners/${fuzzer}/${benchmark}-intermediate"
        "gcr.io/fuzzbench/runners/${fuzzer}/${benchmark}"
    )
    for img in "${images[@]}"; do
        if [[ ! "$img" == gcr.io/fuzzbench/* ]]; then
            abort_msg "REFUSING to delete non-fuzzbench image: ${img}"
        fi
        docker rmi -f "$img" 2>/dev/null || true
    done
    echo "[cleanup] Deleted 4 pair images for ${fuzzer}×${benchmark}"
}

# Delete benchmark + coverage images ONLY after ALL fuzzers verified.
delete_benchmark_image() {
    local benchmark="$1"
    local images=(
        "gcr.io/fuzzbench/builders/benchmark/${benchmark}"
        "gcr.io/fuzzbench/builders/coverage/${benchmark}"
        "gcr.io/fuzzbench/builders/coverage/${benchmark}-intermediate"
    )
    for img in "${images[@]}"; do
        if [[ ! "$img" == gcr.io/fuzzbench/* ]]; then
            abort_msg "REFUSING to delete: ${img}"
        fi
        docker rmi -f "$img" 2>/dev/null || true
    done
    echo "[cleanup] Deleted benchmark images for ${benchmark}"
}

# Also clean dispatcher-image and worker images built by experiments
delete_experiment_infra_images() {
    for img in "gcr.io/fuzzbench/dispatcher-image" "gcr.io/fuzzbench/worker"; do
        docker rmi -f "$img" 2>/dev/null || true
    done
}

# Clean dangling images that belong to this project (by env var + label inspection).
# NEVER use "docker image prune" — it deletes indiscriminately.
# Delete: [FUZZBENCH] (has FUZZING_ENGINE/SRC/OUT) and [UBUNTU-BASE] (ubuntu labels, our build layers)
# Skip: [UNKNOWN] — anything without fuzzbench markers or ubuntu labels
clean_dangling_fuzzbench_images() {
    local deleted=0
    local skipped=0
    for id in $(docker images --filter "dangling=true" --format '{{.ID}}' 2>/dev/null); do
        local envs labels
        envs=$(docker image inspect "$id" --format '{{.Config.Env}}' 2>/dev/null)
        labels=$(docker image inspect "$id" --format '{{.Config.Labels}}' 2>/dev/null)
        if echo "$envs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
            docker rmi -f "$id" >/dev/null 2>&1 && deleted=$((deleted + 1))
        elif echo "$labels" | grep -q "ubuntu"; then
            docker rmi -f "$id" >/dev/null 2>&1 && deleted=$((deleted + 1))
        else
            skipped=$((skipped + 1))
        fi
    done
    if [ "$deleted" -gt 0 ]; then
        echo "[cleanup] Deleted ${deleted} dangling project images"
    fi
    if [ "$skipped" -gt 0 ]; then
        echo "[cleanup] Skipped ${skipped} unknown dangling images (not ours)"
    fi
}

# Kill ONLY our experiment's containers. Nothing else.
kill_our_containers() {
    local prefix="$1"
    if [ -z "$prefix" ]; then
        abort_msg "Must provide container prefix"
    fi
    local killed=0
    for name in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
        if [[ "$name" == dispatcher-d-${prefix}* ]]; then
            docker rm -f "$name" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done
    if [ "$killed" -gt 0 ]; then
        echo "[cleanup] Killed ${killed} containers matching dispatcher-d-${prefix}*"
    fi
}

# Build ONE fuzzer×benchmark pair
build_pair() {
    local fuzzer="$1"
    local benchmark="$2"
    local target="build-${fuzzer}-${benchmark}"

    check_disk_or_abort "before building ${target}"

    echo "[build] Building ${target} (--no-cache, BUILDKIT=0)..."
    if ! make -j1 -f docker/generated.mk "$target" 2>&1 | tail -20; then
        echo "[build] WARNING: Build may have failed. Checking image..."
    fi

    # Verify the runner image exists
    if ! docker image inspect "gcr.io/fuzzbench/runners/${fuzzer}/${benchmark}" >/dev/null 2>&1; then
        abort_msg "Build FAILED for ${target} — runner image not found"
    fi

    check_disk_or_abort "after building ${target}"
    echo "[build] ${target} OK"
}

# Write per-fuzzer experiment config YAML
write_test_config() {
    local config_path="$1"
    local filestore="$2"
    local report_store="$3"
    cat > "$config_path" <<YAML
trials: 1
max_total_time: ${TEST_SECONDS}
docker_registry: gcr.io/fuzzbench
experiment_filestore: ${filestore}
report_filestore: ${report_store}
local_experiment: true
snapshot_period: 360
runner_num_cpu_cores: 1
YAML
}

# Run basic FuzzBench experiment (NOT our split wrapper!)
# 1 dispatcher + 1 runner container only.
run_basic_fuzzbench_test() {
    local fuzzer="$1"
    local benchmark="$2"
    local exp_name="$3"

    local filestore="${PROJECT_DIR}/results/experiment-data/${exp_name}"
    local report_store="${PROJECT_DIR}/results/report-data/${exp_name}"
    mkdir -p "$filestore" "$report_store"

    local config_path="${CONFIG_DIR}/${exp_name}.yaml"
    write_test_config "$config_path" "$filestore" "$report_store"

    echo "[run] Basic FuzzBench: ${exp_name}"
    echo "[run]   fuzzer=${fuzzer} benchmark=${benchmark}"
    echo "[run]   trials=1 time=${TEST_SECONDS}s config=${config_path}"

    cd "$FUZZBENCH_DIR"
    python experiment/run_experiment.py \
        --experiment-config "$config_path" \
        --experiment-name "$exp_name" \
        --fuzzers "$fuzzer" \
        --benchmarks "$benchmark" \
        --runners-cpus 4 \
        --measurers-cpus 4 \
        --concurrent-builds 1 \
        --allow-uncommitted-changes
    local rc=$?
    cd "$PROJECT_DIR"

    return $rc
}

# =====================================================================
# MAIN
# =====================================================================

echo "============================================================"
echo "  ONE-BENCHMARK BUILD & VERIFY"
echo "  Benchmark:    ${BENCHMARK}"
echo "  Fuzzers:      ${FUZZERS[*]}"
echo "  Prefix:       ${EXP_PREFIX}"
echo "  Test seconds: ${TEST_SECONDS}"
echo "  Timestamp:    ${TIMESTAMP}"
echo "  Method:       basic FuzzBench run_experiment.py (1 trial)"
echo "============================================================"
echo ""

# --- Validate ---
validate_benchmark "$BENCHMARK"
check_disk_or_abort "before starting"

# --- Track successes ---
SUCCEEDED_FUZZERS=()
FAILED_FUZZERS=()

# --- Run each fuzzer SEQUENTIALLY: build → test → cleanup ---
for fuzzer in "${FUZZERS[@]}"; do
    echo ""
    echo "=========================================================="
    echo "  FUZZER: ${fuzzer}  ×  BENCHMARK: ${BENCHMARK}"
    echo "  (${#SUCCEEDED_FUZZERS[@]}/${#FUZZERS[@]} done so far)"
    echo "=========================================================="

    check_disk_or_abort "before fuzzer ${fuzzer}"

    # Experiment name: must match ^[a-z0-9-]{0,30}$
    EXP_NAME="${EXP_PREFIX}-${fuzzer}-${TIMESTAMP}"
    # Validate length <=30 and pattern
    if [ "${#EXP_NAME}" -gt 30 ]; then
        abort_msg "Experiment name too long (${#EXP_NAME} > 30): ${EXP_NAME}"
    fi

    # Clean leftover containers
    kill_our_containers "${EXP_PREFIX}"

    # Build fuzzer×benchmark pair (benchmark image built automatically as dependency)
    cd "$FUZZBENCH_DIR"
    build_pair "$fuzzer" "$BENCHMARK"
    cd "$PROJECT_DIR"

    # Run basic FuzzBench test (1 dispatcher + 1 runner = 2 containers)
    set +e
    run_basic_fuzzbench_test "$fuzzer" "$BENCHMARK" "$EXP_NAME"
    rc=$?
    set -e

    # Kill leftover containers
    kill_our_containers "${EXP_NAME}"

    if [ "$rc" -eq 0 ]; then
        echo "[OK] ${fuzzer}×${BENCHMARK} SUCCEEDED"
        SUCCEEDED_FUZZERS+=("$fuzzer")
    else
        echo "[FAIL] ${fuzzer}×${BENCHMARK} FAILED (exit=${rc})"
        FAILED_FUZZERS+=("$fuzzer")
    fi

    # === FULL CLEANUP (hardcoded, every step) ===

    # 1. Kill containers that are CERTAINLY ours
    #    a) dispatcher-d-{prefix}* by name
    kill_our_containers "${EXP_NAME}"
    kill_our_containers "${EXP_PREFIX}"
    #    b) Any exited container verified as fuzzbench by env vars
    for cname in $(docker ps -a --filter "status=exited" --format '{{.Names}}' 2>/dev/null); do
        cenvs=$(docker inspect "$cname" --format '{{.Config.Env}}' 2>/dev/null)
        if echo "$cenvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
            docker rm -f "$cname" >/dev/null 2>&1
        fi
    done

    # 3. Delete 4 per-pair images (gcr.io/fuzzbench/ prefix enforced)
    delete_pair_images "$fuzzer" "$BENCHMARK"

    # 4. Delete infra images built by experiment (dispatcher-image, worker)
    delete_experiment_infra_images

    # 5. Clean dangling FUZZBENCH images (verified by env var inspection)
    clean_dangling_fuzzbench_images

    # 6. Clean experiment data (root-owned files from Docker containers need sudo)
    sudo rm -rf "${PROJECT_DIR}/results/experiment-data/${EXP_NAME}" 2>/dev/null || \
        rm -rf "${PROJECT_DIR}/results/experiment-data/${EXP_NAME}" 2>/dev/null
    sudo rm -rf "${PROJECT_DIR}/results/report-data/${EXP_NAME}" 2>/dev/null || \
        rm -rf "${PROJECT_DIR}/results/report-data/${EXP_NAME}" 2>/dev/null
    rm -f "${CONFIG_DIR}/${EXP_NAME}.yaml"

    check_disk_or_abort "after fuzzer ${fuzzer} cleanup"
    echo ""
done

# --- All fuzzers done for this benchmark ---
echo ""
echo "============================================================"
echo "  BENCHMARK ${BENCHMARK} COMPLETE"
echo "  Succeeded: ${SUCCEEDED_FUZZERS[*]:-none}"
echo "  Failed:    ${FAILED_FUZZERS[*]:-none}"
echo "============================================================"

# Delete benchmark image ONLY if ALL 9 fuzzers succeeded
if [ "${#SUCCEEDED_FUZZERS[@]}" -eq "${#FUZZERS[@]}" ]; then
    echo "[cleanup] ALL fuzzers succeeded — deleting benchmark image"
    delete_benchmark_image "$BENCHMARK"
else
    echo "[cleanup] NOT all fuzzers succeeded — KEEPING benchmark image for reruns"
    echo "  To rerun failed fuzzers, use: ./scripts/run_one_fuzzer.sh <fuzzer> ${BENCHMARK}"
fi

check_disk_or_abort "after benchmark ${BENCHMARK} complete"

# Write summary
SUMMARY_FILE="${PROJECT_DIR}/results/${BENCHMARK}_${TIMESTAMP}_summary.txt"
mkdir -p "$(dirname "$SUMMARY_FILE")"
cat > "$SUMMARY_FILE" <<SUMMARY
Benchmark: ${BENCHMARK}
Timestamp: ${TIMESTAMP}
Test seconds: ${TEST_SECONDS}
Succeeded: ${SUCCEEDED_FUZZERS[*]:-none}
Failed: ${FAILED_FUZZERS[*]:-none}
SUMMARY
echo "[done] Summary written to ${SUMMARY_FILE}"

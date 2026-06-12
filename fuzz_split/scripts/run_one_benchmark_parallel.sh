#!/usr/bin/env bash
# =============================================================================
# run_one_benchmark_parallel.sh — Build & verify ALL 9 fuzzers for ONE benchmark.
#
# STRATEGY (HARDCODED, NEVER CHANGE):
#   Step 1: Build + test honggfuzz SEQUENTIALLY (catches benchmark issues)
#   Step 2: Build all 8 remaining fuzzers SEQUENTIALLY (disk safe)
#   Step 3: Run all 8 tests IN PARALLEL (saves time, 188 CPUs)
#   Step 4: Full cleanup
#
# HARD-CODED RULES:
#   1. ONE benchmark at a time.
#   2. honggfuzz FIRST, sequential.
#   3. Then build remaining 8 fuzzers sequentially.
#   4. Then run 8 experiments IN PARALLEL.
#   5. DOCKER_BUILDKIT=0 (zero cache).
#   6. Disk check before/after builds. ABORT if <300GB.
#   7. Basic FuzzBench run_experiment.py directly (NOT split wrapper).
#   8. 1 trial, 900s (15 min).
#   9. Cleanup: only touch what is CERTAINLY ours (positive ID).
#  10. NEVER run any docker prune command.
#
# Usage:
#   ./scripts/run_one_benchmark_parallel.sh <benchmark> [prefix]
# =============================================================================
set -uo pipefail
# NOTE: NOT using set -e. We handle errors explicitly.
# set -e with pipefail causes false failures in grep/docker inspect pipelines.

MIN_FREE_DISK_GB=300
export DOCKER_BUILDKIT=0
TEST_SECONDS=900

FUZZERS_ALL=(afl aflfast aflplusplus aflsmart entropic fairfuzz honggfuzz libfuzzer mopt)
FUZZERS_PARALLEL=(afl aflfast aflplusplus aflsmart entropic fairfuzz libfuzzer mopt)

VALID_BENCHMARKS=(
    arrow_parquet-arrow-fuzz aspell_aspell_fuzzer ffmpeg_ffmpeg_demuxer_fuzzer
    grok_grk_decompress_fuzzer harfbuzz-1.3.2 libgit2_objects_fuzzer
    libhevc_hevc_dec_fuzzer libhtp_fuzz_htp
    libxml2_libxml2_xml_reader_for_file_fuzzer matio_matio_fuzzer
    njs_njs_process_script_fuzzer openh264_decoder_fuzzer
    php_php-fuzz-parser-2020-07-25 poppler_pdf_fuzzer quickjs_eval-2020-01-05
    stb_stbi_read_fuzzer systemd_fuzz-link-parser wireshark_fuzzshark_ip
)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FUZZBENCH_DIR="${PROJECT_DIR}/fuzzbench"
TIMESTAMP="$(date +%Y%m%d-%H%M)"

BENCHMARK="${1:?Usage: $0 <benchmark> [prefix]}"
PREFIX="${2:-bt}"

eval "$(conda shell.bash hook)"
conda activate fuzz_split
export PYTHONPATH="${FUZZBENCH_DIR}:${PYTHONPATH:-}"

# =====================================================================
# SAFETY FUNCTIONS
# =====================================================================
abort_msg() { echo "!!!! ABORT: $1"; exit 1; }

check_disk_or_abort() {
    local free_gb
    free_gb=$(df --output=avail -BG / | tail -1 | tr -d ' G')
    [ "$free_gb" -lt "$MIN_FREE_DISK_GB" ] && abort_msg "DISK ${free_gb}GB < ${MIN_FREE_DISK_GB}GB — $1"
    echo "[disk] ${free_gb}GB free — $1"
}

validate_benchmark() {
    local bm="$1"
    for valid in "${VALID_BENCHMARKS[@]}"; do [ "$bm" = "$valid" ] && return 0; done
    abort_msg "Invalid benchmark: ${bm}"
}

# Cleanup: ONLY touch what is CERTAINLY ours (positive identification)
full_cleanup() {
    local fuzzer="$1" bm="$2" exp_name="$3"
    # Containers: dispatcher-d-{name}* and verified fuzzbench containers
    for cname in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
        if [[ "$cname" == dispatcher-d-${exp_name}* ]] || [[ "$cname" == dispatcher-d-${PREFIX}* ]]; then
            docker rm -f "$cname" >/dev/null 2>&1
        else
            local cenvs
            cenvs=$(docker inspect "$cname" --format '{{.Config.Env}}' 2>/dev/null)
            local cimg
            cimg=$(docker inspect "$cname" --format '{{.Config.Image}}' 2>/dev/null)
            if echo "$cenvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
                docker rm -f "$cname" >/dev/null 2>&1
            elif echo "$cimg" | grep -q "gcr.io/fuzzbench"; then
                docker rm -f "$cname" >/dev/null 2>&1
            fi
        fi
    done
    # Images: exact gcr.io/fuzzbench/ names
    for img in \
        "gcr.io/fuzzbench/builders/${fuzzer}/${bm}-intermediate" \
        "gcr.io/fuzzbench/builders/${fuzzer}/${bm}" \
        "gcr.io/fuzzbench/runners/${fuzzer}/${bm}-intermediate" \
        "gcr.io/fuzzbench/runners/${fuzzer}/${bm}" \
        "gcr.io/fuzzbench/dispatcher-image" \
        "gcr.io/fuzzbench/worker"; do
        docker rmi -f "$img" 2>/dev/null || true
    done
    # Dangling: verified by env/labels
    for did in $(docker images --filter "dangling=true" --format '{{.ID}}' 2>/dev/null); do
        local denvs dlabels
        denvs=$(docker image inspect "$did" --format '{{.Config.Env}}' 2>/dev/null)
        dlabels=$(docker image inspect "$did" --format '{{.Config.Labels}}' 2>/dev/null)
        if echo "$denvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
            docker rmi -f "$did" >/dev/null 2>&1
        elif echo "$dlabels" | grep -q "ubuntu"; then
            docker rmi -f "$did" >/dev/null 2>&1
        fi
    done
    # Data
    sudo rm -rf "${PROJECT_DIR}/results/experiment-data/${exp_name}" \
                "${PROJECT_DIR}/results/report-data/${exp_name}" 2>/dev/null
    rm -f "${PROJECT_DIR}/configs/${exp_name}.yaml" 2>/dev/null
}

run_experiment() {
    local fuzzer="$1" bm="$2" exp_name="$3"
    local filestore="${PROJECT_DIR}/results/experiment-data/${exp_name}"
    local report="${PROJECT_DIR}/results/report-data/${exp_name}"
    mkdir -p "$filestore" "$report" "${PROJECT_DIR}/configs"
    cat > "${PROJECT_DIR}/configs/${exp_name}.yaml" <<YAML
trials: 1
max_total_time: ${TEST_SECONDS}
docker_registry: gcr.io/fuzzbench
experiment_filestore: ${filestore}
report_filestore: ${report}
local_experiment: true
snapshot_period: 360
runner_num_cpu_cores: 1
YAML
    # Each experiment gets its own working directory to avoid race conditions
    # when running in parallel (config dir creation conflicts).
    local workdir="/tmp/fuzzbench_work_${fuzzer}_${bm}"
    mkdir -p "$workdir"
    cd "$workdir"
    export PYTHONPATH="$FUZZBENCH_DIR"
    python "$FUZZBENCH_DIR/experiment/run_experiment.py" \
        --experiment-config "${PROJECT_DIR}/configs/${exp_name}.yaml" \
        --experiment-name "$exp_name" \
        --fuzzers "$fuzzer" \
        --benchmarks "$bm" \
        --runners-cpus 4 --measurers-cpus 4 --concurrent-builds 1 \
        --allow-uncommitted-changes > "/tmp/exp_${fuzzer}_${bm}.log" 2>&1
    local rc=$?
    tail -5 "/tmp/exp_${fuzzer}_${bm}.log"
    cd "$PROJECT_DIR"
    rm -rf "$workdir"
    return $rc
}

# =====================================================================
# MAIN
# =====================================================================
validate_benchmark "$BENCHMARK"
check_disk_or_abort "before starting"

echo "============================================================"
echo "  BENCHMARK: ${BENCHMARK}"
echo "  Strategy:  honggfuzz first, then 8 fuzzers parallel"
echo "  Timestamp: ${TIMESTAMP}"
echo "============================================================"

# ---- STEP 1: honggfuzz sequential ----
echo ""
echo "========== STEP 1: honggfuzz (sequential) =========="
check_disk_or_abort "before honggfuzz build"

cd "$FUZZBENCH_DIR"
make -j1 -f docker/generated.mk "build-honggfuzz-${BENCHMARK}" > /tmp/build_honggfuzz.log 2>&1
tail -5 /tmp/build_honggfuzz.log
if ! docker image inspect "gcr.io/fuzzbench/runners/honggfuzz/${BENCHMARK}" >/dev/null 2>&1; then
    abort_msg "honggfuzz build FAILED"
fi
echo "[OK] honggfuzz built"
check_disk_or_abort "after honggfuzz build"

EXP_HF="${PREFIX}-hfuzz-${TIMESTAMP}"
if [ "${#EXP_HF}" -gt 30 ]; then abort_msg "Exp name too long: ${EXP_HF}"; fi

run_experiment honggfuzz "$BENCHMARK" "$EXP_HF"
hf_files=$(find "${PROJECT_DIR}/results/experiment-data/${EXP_HF}" -type f 2>/dev/null | wc -l)
if [ "$hf_files" -gt 0 ]; then
    echo "[OK] honggfuzz PASSED (${hf_files} files)"
else
    echo "[FAIL] honggfuzz — no data files"
fi

full_cleanup honggfuzz "$BENCHMARK" "$EXP_HF"
check_disk_or_abort "after honggfuzz cleanup"

# ---- STEP 2: Build remaining 8 fuzzers IN PARALLEL ----
echo ""
echo "========== STEP 2: Build 8 fuzzers IN PARALLEL =========="
check_disk_or_abort "before parallel builds"
BUILT_FUZZERS=()
FAILED_BUILD=()
BUILD_PIDS=()
cd "$FUZZBENCH_DIR"

for fuzzer in "${FUZZERS_PARALLEL[@]}"; do
    echo "[build-start] ${fuzzer}"
    make -j1 -f docker/generated.mk "build-${fuzzer}-${BENCHMARK}" > /tmp/build_${fuzzer}.log 2>&1 &
    BUILD_PIDS+=("${fuzzer}:$!")
done

echo "Waiting for ${#BUILD_PIDS[@]} parallel builds..."
for entry in "${BUILD_PIDS[@]}"; do
    fuzzer="${entry%%:*}"
    pid="${entry##*:}"
    wait "$pid" 2>/dev/null
    if docker image inspect "gcr.io/fuzzbench/runners/${fuzzer}/${BENCHMARK}" >/dev/null 2>&1; then
        echo "[OK] ${fuzzer} built"
        BUILT_FUZZERS+=("$fuzzer")
    else
        echo "[FAIL BUILD] ${fuzzer}"
        tail -3 /tmp/build_${fuzzer}.log
        FAILED_BUILD+=("$fuzzer")
    fi
done

check_disk_or_abort "after all builds"
echo "Built: ${BUILT_FUZZERS[*]:-none}"
echo "Failed: ${FAILED_BUILD[*]:-none}"

if [ "${#BUILT_FUZZERS[@]}" -eq 0 ]; then
    abort_msg "No fuzzers built successfully"
fi

# ---- STEP 3: Run all built fuzzers IN PARALLEL ----
echo ""
echo "========== STEP 3: Run ${#BUILT_FUZZERS[@]} experiments IN PARALLEL =========="

PIDS=()
EXP_NAMES=()
for fuzzer in "${BUILT_FUZZERS[@]}"; do
    EXP_NAME="${PREFIX}-${fuzzer:0:5}-${TIMESTAMP}"
    if [ "${#EXP_NAME}" -gt 30 ]; then
        EXP_NAME="${PREFIX}-${fuzzer:0:4}-${TIMESTAMP}"
    fi
    EXP_NAMES+=("${fuzzer}:${EXP_NAME}")

    echo "[launch] ${fuzzer} → ${EXP_NAME}"
    (
        run_experiment "$fuzzer" "$BENCHMARK" "$EXP_NAME" > /tmp/test_${fuzzer}.log 2>&1
    ) &
    PIDS+=($!)
done

echo "Waiting for ${#PIDS[@]} parallel experiments..."
PASSED_FUZZERS=()
FAILED_TEST=()
for i in "${!PIDS[@]}"; do
    wait "${PIDS[$i]}" 2>/dev/null
    rc=$?
    entry="${EXP_NAMES[$i]}"
    fuzzer="${entry%%:*}"
    exp_name="${entry##*:}"
    files=$(find "${PROJECT_DIR}/results/experiment-data/${exp_name}" -type f 2>/dev/null | wc -l)
    if [ "$files" -gt 0 ]; then
        echo "[OK] ${fuzzer} PASSED (${files} files)"
        PASSED_FUZZERS+=("$fuzzer")
    else
        echo "[FAIL] ${fuzzer} (rc=${rc}, 0 files)"
        FAILED_TEST+=("$fuzzer")
    fi
done

# ---- STEP 4: Full cleanup ----
echo ""
echo "========== STEP 4: Cleanup =========="

# Clean all pair images for built fuzzers
for fuzzer in "${BUILT_FUZZERS[@]}"; do
    for img in \
        "gcr.io/fuzzbench/builders/${fuzzer}/${BENCHMARK}-intermediate" \
        "gcr.io/fuzzbench/builders/${fuzzer}/${BENCHMARK}" \
        "gcr.io/fuzzbench/runners/${fuzzer}/${BENCHMARK}-intermediate" \
        "gcr.io/fuzzbench/runners/${fuzzer}/${BENCHMARK}"; do
        docker rmi -f "$img" 2>/dev/null || true
    done
done
docker rmi -f "gcr.io/fuzzbench/dispatcher-image" "gcr.io/fuzzbench/worker" 2>/dev/null || true

# Containers (positive ID only)
for cname in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    if [[ "$cname" == dispatcher-d-${PREFIX}* ]]; then
        docker rm -f "$cname" >/dev/null 2>&1
    else
        cenvs=$(docker inspect "$cname" --format '{{.Config.Env}}' 2>/dev/null)
        cimg=$(docker inspect "$cname" --format '{{.Config.Image}}' 2>/dev/null)
        if echo "$cenvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
            docker rm -f "$cname" >/dev/null 2>&1
        elif echo "$cimg" | grep -q "gcr.io/fuzzbench"; then
            docker rm -f "$cname" >/dev/null 2>&1
        fi
    fi
done

# Dangling (positive ID)
for did in $(docker images --filter "dangling=true" --format '{{.ID}}' 2>/dev/null); do
    denvs=$(docker image inspect "$did" --format '{{.Config.Env}}' 2>/dev/null)
    dlabels=$(docker image inspect "$did" --format '{{.Config.Labels}}' 2>/dev/null)
    if echo "$denvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
        docker rmi -f "$did" >/dev/null 2>&1
    elif echo "$dlabels" | grep -q "ubuntu"; then
        docker rmi -f "$did" >/dev/null 2>&1
    fi
done

# Data
for entry in "${EXP_NAMES[@]}"; do
    exp_name="${entry##*:}"
    sudo rm -rf "${PROJECT_DIR}/results/experiment-data/${exp_name}" \
                "${PROJECT_DIR}/results/report-data/${exp_name}" 2>/dev/null
    rm -f "${PROJECT_DIR}/configs/${exp_name}.yaml" 2>/dev/null
done

# Delete benchmark + coverage images if ALL 9 passed
total_passed=$(( ${#PASSED_FUZZERS[@]} + 1 ))  # +1 for honggfuzz
if [ "$total_passed" -eq 9 ] && [ "${#FAILED_BUILD[@]}" -eq 0 ] && [ "${#FAILED_TEST[@]}" -eq 0 ]; then
    echo "[cleanup] ALL 9 PASSED — deleting benchmark + coverage images"
    for img in \
        "gcr.io/fuzzbench/builders/benchmark/${BENCHMARK}" \
        "gcr.io/fuzzbench/builders/coverage/${BENCHMARK}" \
        "gcr.io/fuzzbench/builders/coverage/${BENCHMARK}-intermediate"; do
        docker rmi -f "$img" 2>/dev/null || true
    done
else
    echo "[cleanup] NOT all passed — keeping benchmark image"
fi

check_disk_or_abort "after cleanup"

echo ""
echo "============================================================"
echo "  BENCHMARK ${BENCHMARK} COMPLETE"
echo "  honggfuzz: PASSED"
echo "  Parallel passed: ${PASSED_FUZZERS[*]:-none}"
echo "  Build failed:    ${FAILED_BUILD[*]:-none}"
echo "  Test failed:     ${FAILED_TEST[*]:-none}"
echo "  Total: $((${#PASSED_FUZZERS[@]} + 1))/9"
echo "============================================================"

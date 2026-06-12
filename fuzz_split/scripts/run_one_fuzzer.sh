#!/usr/bin/env bash
# =============================================================================
# run_one_fuzzer.sh — Build & test ONE fuzzer for ONE benchmark.
#
# Uses basic FuzzBench run_experiment.py directly (NOT our split wrapper).
# Same safety rules as run_one_benchmark.sh.
#
# Usage:
#   ./scripts/run_one_fuzzer.sh <fuzzer> <benchmark> [experiment_prefix] [test_seconds]
# =============================================================================
set -euo pipefail

MIN_FREE_DISK_GB=300
export DOCKER_BUILDKIT=0

VALID_FUZZERS=(afl aflfast aflplusplus aflsmart entropic fairfuzz honggfuzz libfuzzer mopt)
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
CONFIG_DIR="${PROJECT_DIR}/configs"
TIMESTAMP="$(date +%Y%m%d-%H%M)"

FUZZER="${1:?Usage: $0 <fuzzer> <benchmark> [experiment_prefix] [test_seconds]}"
BENCHMARK="${2:?Usage: $0 <fuzzer> <benchmark> [experiment_prefix] [test_seconds]}"
EXP_PREFIX="${3:-bt}"
TEST_SECONDS="${4:-900}"

eval "$(conda shell.bash hook)"
conda activate fuzz_split
export PYTHONPATH="${FUZZBENCH_DIR}:${PYTHONPATH:-}"

abort_msg() { echo "!!!! ABORT: $1"; exit 1; }

check_disk_or_abort() {
    local free_gb
    free_gb=$(df --output=avail -BG / | tail -1 | tr -d ' G')
    [ "$free_gb" -lt "$MIN_FREE_DISK_GB" ] && abort_msg "DISK ${free_gb}GB < ${MIN_FREE_DISK_GB}GB — $1"
    echo "[disk-check] ${free_gb}GB free — $1"
}

# Validate
found=0; for v in "${VALID_FUZZERS[@]}"; do [ "$v" = "$FUZZER" ] && found=1; done
[ "$found" -eq 0 ] && abort_msg "Invalid fuzzer: ${FUZZER}"
found=0; for v in "${VALID_BENCHMARKS[@]}"; do [ "$v" = "$BENCHMARK" ] && found=1; done
[ "$found" -eq 0 ] && abort_msg "Invalid benchmark: ${BENCHMARK}"

check_disk_or_abort "before starting"

echo "============================================================"
echo "  SINGLE FUZZER: ${FUZZER} × ${BENCHMARK}"
echo "  Method: basic FuzzBench run_experiment.py (1 trial)"
echo "============================================================"

EXP_NAME="${EXP_PREFIX}-${FUZZER}-${TIMESTAMP}"
if [ "${#EXP_NAME}" -gt 30 ]; then
    abort_msg "Experiment name too long (${#EXP_NAME} > 30): ${EXP_NAME}"
fi

# Build
cd "$FUZZBENCH_DIR"
check_disk_or_abort "before build"
echo "[build] Building ${FUZZER}×${BENCHMARK}..."
make -j1 -f docker/generated.mk "build-${FUZZER}-${BENCHMARK}" 2>&1 | tail -20
if ! docker image inspect "gcr.io/fuzzbench/runners/${FUZZER}/${BENCHMARK}" >/dev/null 2>&1; then
    abort_msg "Build FAILED — runner image not found"
fi
check_disk_or_abort "after build"
cd "$PROJECT_DIR"

# Write config
FILESTORE="${PROJECT_DIR}/results/experiment-data/${EXP_NAME}"
REPORT_STORE="${PROJECT_DIR}/results/report-data/${EXP_NAME}"
mkdir -p "$FILESTORE" "$REPORT_STORE"
CONFIG_PATH="${CONFIG_DIR}/${EXP_NAME}.yaml"
cat > "$CONFIG_PATH" <<YAML
trials: 1
max_total_time: ${TEST_SECONDS}
docker_registry: gcr.io/fuzzbench
experiment_filestore: ${FILESTORE}
report_filestore: ${REPORT_STORE}
local_experiment: true
snapshot_period: 360
runner_num_cpu_cores: 1
YAML

# Run basic FuzzBench (1 dispatcher + 1 runner = 2 containers)
echo "[run] Basic FuzzBench: ${EXP_NAME}"
cd "$FUZZBENCH_DIR"
set +e
python experiment/run_experiment.py \
    --experiment-config "$CONFIG_PATH" \
    --experiment-name "$EXP_NAME" \
    --fuzzers "$FUZZER" \
    --benchmarks "$BENCHMARK" \
    --runners-cpus 4 \
    --measurers-cpus 4 \
    --concurrent-builds 1 \
    --allow-uncommitted-changes
rc=$?
set -e
cd "$PROJECT_DIR"

# === FULL CLEANUP (hardcoded, every step) ===

# 1. Kill containers that are CERTAINLY ours
#    a) dispatcher-d-{prefix}* by name
for name in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    [[ "$name" == dispatcher-d-${EXP_NAME}* ]] && docker rm -f "$name" >/dev/null 2>&1
    [[ "$name" == dispatcher-d-${EXP_PREFIX}* ]] && docker rm -f "$name" >/dev/null 2>&1
done
#    b) Any exited container verified as fuzzbench by env vars
for cname in $(docker ps -a --filter "status=exited" --format '{{.Names}}' 2>/dev/null); do
    cenvs=$(docker inspect "$cname" --format '{{.Config.Env}}' 2>/dev/null)
    if echo "$cenvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
        docker rm -f "$cname" >/dev/null 2>&1
    fi
done

# 3. Delete 4 per-pair images (gcr.io/fuzzbench/ prefix enforced)
for img in \
    "gcr.io/fuzzbench/builders/${FUZZER}/${BENCHMARK}-intermediate" \
    "gcr.io/fuzzbench/builders/${FUZZER}/${BENCHMARK}" \
    "gcr.io/fuzzbench/runners/${FUZZER}/${BENCHMARK}-intermediate" \
    "gcr.io/fuzzbench/runners/${FUZZER}/${BENCHMARK}"; do
    [[ "$img" == gcr.io/fuzzbench/* ]] && docker rmi -f "$img" 2>/dev/null || true
done

# 4. Delete infra images built by experiment
docker rmi -f "gcr.io/fuzzbench/dispatcher-image" 2>/dev/null || true
docker rmi -f "gcr.io/fuzzbench/worker" 2>/dev/null || true

# 5. Clean dangling project images (verified by env var + label inspection, NOT docker prune)
for did in $(docker images --filter "dangling=true" --format '{{.ID}}' 2>/dev/null); do
    denvs=$(docker image inspect "$did" --format '{{.Config.Env}}' 2>/dev/null)
    dlabels=$(docker image inspect "$did" --format '{{.Config.Labels}}' 2>/dev/null)
    if echo "$denvs" | grep -q "FUZZING_ENGINE\|FUZZBENCH\|OSS_FUZZ"; then
        docker rmi -f "$did" >/dev/null 2>&1
    elif echo "$dlabels" | grep -q "ubuntu"; then
        docker rmi -f "$did" >/dev/null 2>&1
    fi
done

# 6. Clean experiment data (root-owned files from Docker containers need sudo)
sudo rm -rf "$FILESTORE" "$REPORT_STORE" 2>/dev/null || rm -rf "$FILESTORE" "$REPORT_STORE" 2>/dev/null
rm -f "$CONFIG_PATH"

echo "[cleanup] Full cleanup done (containers, images, data)"
check_disk_or_abort "after cleanup"

if [ "$rc" -eq 0 ]; then
    echo "[OK] ${FUZZER}×${BENCHMARK} SUCCEEDED"
else
    echo "[FAIL] ${FUZZER}×${BENCHMARK} FAILED (exit=${rc})"
    exit "$rc"
fi

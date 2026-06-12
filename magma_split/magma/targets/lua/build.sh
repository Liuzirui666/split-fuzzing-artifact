#!/bin/bash
set -e

##
# Pre-requirements:
# - env TARGET: path to target work dir
# - env OUT: path to directory where artifacts are stored
# - env CC, CXX, FLAGS, LIBS, etc...
##

if [ ! -d "$TARGET/repo" ]; then
    echo "fetch.sh must be executed first."
    exit 1
fi

# build lua library
cd "$TARGET/repo"
make -j$(nproc) clean
make -j$(nproc) liblua.a

cp liblua.a "$OUT/"

# build driver
#
# For in-process fuzzers (libFuzzer) the standalone interpreter cannot be
# driven: libFuzzer needs an LLVMFuzzerTestOneInput entry point linked against
# the fuzzing engine. The libfuzzer instrument.sh drops a driver.o (main() +
# LLVMFuzzerRunDriver) and libFuzzer.a into $OUT and adds them to $LIBS. Detect
# that file (exactly as the libsndfile target does) and, when present, build the
# OSS-Fuzz-style harness as $OUT/lua so PROGRAMS=(lua) and run.sh are unchanged.
# Otherwise build the regular standalone interpreter for AFL/honggfuzz/MOpt,
# which feed inputs as Lua scripts via stdin.
if [ -f "$OUT/driver.o" ]; then
    $CC $CFLAGS -I"$TARGET/repo" \
        -c "$TARGET/src/lua_fuzzer.c" -o "$OUT/lua_fuzzer.o"
    $CXX $CXXFLAGS \
        "$OUT/lua_fuzzer.o" "$OUT/liblua.a" \
        -o "$OUT/lua" \
        $LDFLAGS $LIBS -lm -ldl -lreadline
else
    make -j$(nproc) lua
    cp lua "$OUT/"
fi

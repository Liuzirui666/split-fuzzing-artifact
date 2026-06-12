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

# For the libFuzzer build, select libsndfile's *engine* link path instead of its
# standalone main(). libsndfile's configure picks FUZZ_LDADD=$(LIB_FUZZING_ENGINE)
# only when `test -f "$LIB_FUZZING_ENGINE"` is true; otherwise it links
# ossfuzz/libstandaloneengine.la, whose strong main() hijacks the binary (it then
# treats argv as filenames instead of fuzzing). Point LIB_FUZZING_ENGINE at
# Magma's driver.o (a single file): driver.o provides main()+LLVMFuzzerRunDriver
# exactly as for every other libFuzzer target, and libFuzzer.a/magma.o come from
# $LIBS. Guarded on driver.o so afl/honggfuzz builds of libsndfile are unchanged.
CONFIGURE_ENGINE=""
if [ -f "$OUT/driver.o" ]; then
    export LIB_FUZZING_ENGINE="$OUT/driver.o"
    CONFIGURE_ENGINE="LIB_FUZZING_ENGINE=$OUT/driver.o"
fi

cd "$TARGET/repo"
./autogen.sh
./configure --disable-shared --enable-ossfuzzers $CONFIGURE_ENGINE
make -j$(nproc) clean
make -j$(nproc)

cp -v ossfuzz/sndfile_fuzzer $OUT/
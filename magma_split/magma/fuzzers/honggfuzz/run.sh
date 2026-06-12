#!/bin/bash

##
# Pre-requirements:
# - env FUZZER: path to fuzzer work dir
# - env TARGET: path to target work dir
# - env OUT: path to directory where artifacts are stored
# - env SHARED: path to directory shared with host (to store results)
# - env PROGRAM: name of program to run (should be found in $OUT)
# - env ARGS: extra arguments to pass to the program
# - env FUZZARGS: extra arguments to pass to the fuzzer
##

mkdir -p "$SHARED/findings" "$SHARED/output"

# replace AFL-style input file parameter with honggfuzz-style one
ARGS="${ARGS/@@/___FILE___}"

# If the target takes no file-input placeholder (empty ARGS, e.g. lua) and is
# not a persistent libFuzzer harness, honggfuzz must be told to feed the input
# via stdin (-s); otherwise it aborts in cmdlineVerify(). Persistent harnesses
# are auto-detected by honggfuzz and ignore -s, so this is safe for them too.
STDIN_FLAG=()
if [[ "$ARGS" != *"___FILE___"* ]]; then
    STDIN_FLAG=(-s)
fi

"$FUZZER/repo/honggfuzz" -n 1 -z "${STDIN_FLAG[@]}" --input "$TARGET/corpus/$PROGRAM" \
    --output "$SHARED/output" --workspace "$SHARED/findings" \
    $FUZZARGS -- "$OUT/$PROGRAM" $ARGS 2>&1

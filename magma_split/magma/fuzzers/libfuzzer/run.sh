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

mkdir -p "$SHARED/findings" "$SHARED/corpus"

# libFuzzer saves newly-discovered inputs to its FIRST corpus directory.
# Point that at $SHARED/corpus so the splitting harvester (which inherits
# each branch's on-disk queue from $SHARED) can read the evolving corpus,
# mirroring AFL's findings/queue. Seed it from the target's initial corpus
# (for split children this is the read-only harvested parent corpus).
cp -r "$TARGET/corpus/$PROGRAM/." "$SHARED/corpus/" 2>/dev/null || true

# 2>&1: libFuzzer prints its progress (incl. "cov: N" edge coverage and exec
# counts) to STDERR. The entrypoint captures only stdout via `run.sh | multilog`,
# so without this redirect the log is empty and coverage/execs are lost. Mirror
# the afl/honggfuzz/mopt run.sh, which all end in 2>&1.
"$OUT/$PROGRAM" -rss_limit_mb=100 \
	-fork=1 -ignore_timeouts=1 -ignore_crashes=1 -ignore_ooms=1 \
	-artifact_prefix="$SHARED/findings/" $FUZZARGS \
    "$SHARED/corpus" $ARGS 2>&1

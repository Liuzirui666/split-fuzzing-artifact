#!/usr/bin/env python3
"""Patch docker/generated.mk after regeneration.

Removes -t/-it flags from Docker commands and adds EXPERIMENT_FILESTORE env.
This must be run after `make generate-makefile` in the fuzzbench directory.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

FUZZBENCH_DIR = Path(__file__).resolve().parent.parent / "fuzzbench"
GENERATED_MK = FUZZBENCH_DIR / "docker" / "generated.mk"


def patch(path: Path = GENERATED_MK) -> None:
    if not path.exists():
        print(f"Warning: {path} does not exist (not generated yet?)")
        return

    text = path.read_text()
    original = text

    # Remove -ti and -t flags from docker run
    text = re.sub(r'docker run\s+-ti\b', 'docker run', text)
    text = re.sub(r'docker run\s+-t\b', 'docker run', text)

    # Add EXPERIMENT_FILESTORE=local if not present
    if "-e EXPERIMENT_FILESTORE=local" not in text:
        text = text.replace(
            "docker run ",
            "docker run -e EXPERIMENT_FILESTORE=local ",
        )

    if text != original:
        path.write_text(text)
        print(f"Patched {path}")
    else:
        print(f"No changes needed in {path}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else GENERATED_MK
    patch(target)

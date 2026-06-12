"""Per-branch seed management for split experiments.

Key design: NO cross-branch union. Each trial's descendants get ONLY
that trial's corpus. Seeds are tracked per-branch via a tree structure.

Branch ID convention:
  - Root trial r, level 0: branch_id = "b0"
  - After split at level 0 with branching_factor=2: "b0-0", "b0-1"
  - After split at level 1: "b0-0-0", "b0-0-1", "b0-1-0", "b0-1-1"
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


_ARCH_RE = re.compile(r"^corpus-archive-(\d{4})\.tar\.gz$")


def sha256_file(p: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def safe_extract_tar_gz(tar_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:gz") as tf:
        for m in tf.getmembers():
            target = (dest_dir / m.name).resolve()
            if not str(target).startswith(str(dest_dir.resolve()) + os.sep) and target != dest_dir.resolve():
                raise RuntimeError(f"Unsafe path in tar: {m.name}")
        tf.extractall(dest_dir)


def flatten_if_single_topdir(dest_dir: Path) -> None:
    entries = list(dest_dir.iterdir())
    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]
    if len(dirs) == 1 and len(files) == 0:
        top = dirs[0]
        for child in top.iterdir():
            shutil.move(str(child), str(dest_dir / child.name))
        top.rmdir()


def list_archives_desc(corpus_dir: Path) -> List[Tuple[int, Path]]:
    """List corpus-archive-XXXX.tar.gz files, newest first."""
    found: List[Tuple[int, Path]] = []
    if not corpus_dir.exists():
        return found
    for p in corpus_dir.iterdir():
        if not p.is_file():
            continue
        m = _ARCH_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort(key=lambda x: x[0], reverse=True)
    return found


def seed_files_from_extracted(extracted_root: Path, fuzzer: str) -> List[Path]:
    """Normalize seed file locations by fuzzer type."""
    f = fuzzer.lower()

    if f.startswith("afl"):
        cand = extracted_root / "queue"
        if not cand.exists():
            cand = extracted_root
        files = [p for p in cand.rglob("*") if p.is_file()]
        # AFL queue: real inputs start with "id:"
        id_files = [p for p in files if p.name.startswith("id:")]
        return id_files if id_files else files

    if f in {"libfuzzer", "entropic", "fairfuzz"}:
        cand = extracted_root / "corpus"
        if not cand.exists():
            cand = extracted_root
        return [p for p in cand.rglob("*") if p.is_file()]

    if f == "honggfuzz":
        cand = extracted_root / "corpus"
        if not cand.exists():
            cand = extracted_root
        return [p for p in cand.rglob("*") if p.is_file()]

    # Unknown fuzzer: all files
    return [p for p in extracted_root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# Branch ID helpers
# ---------------------------------------------------------------------------

def root_branch_id() -> str:
    return "b0"


def child_branch_ids(parent_id: str, branching_factor: int) -> List[str]:
    """Generate child branch IDs for a parent."""
    return [f"{parent_id}-{i}" for i in range(branching_factor)]


def parent_branch_id(branch_id: str) -> Optional[str]:
    """Get parent branch ID, or None if root."""
    if branch_id == "b0":
        return None
    parts = branch_id.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else None


def all_branch_ids_at_level(level: int, branching_factor: int) -> List[str]:
    """Generate all branch IDs at a given level."""
    if level == 0:
        return [root_branch_id()]
    parents = all_branch_ids_at_level(level - 1, branching_factor)
    children = []
    for p in parents:
        children.extend(child_branch_ids(p, branching_factor))
    return children


# ---------------------------------------------------------------------------
# SeedManager: per-branch seed handling
# ---------------------------------------------------------------------------

class SeedManager:
    """Manages seeds per branch in the split tree.

    Directory layout:
      seed_store/<benchmark>/<sha256_hash>    -- deduplicated seed files
      seeds/r<root>/L<level>/<branch_id>/<benchmark>/  -- materialized seeds for FuzzBench
    """

    def __init__(self, seed_store_root: Path, work_dir: Path):
        self.seed_store_root = seed_store_root
        self.work_dir = work_dir
        self.seed_store_root.mkdir(parents=True, exist_ok=True)

    def _store_dir(self, benchmark: str) -> Path:
        d = self.seed_store_root / benchmark
        d.mkdir(parents=True, exist_ok=True)
        return d

    def store_seed(self, benchmark: str, src: Path) -> str:
        """Store a seed file, return its SHA256 hash."""
        h = sha256_file(src)
        dst = self._store_dir(benchmark) / h
        if not dst.exists():
            shutil.copy2(src, dst)
        return h

    def harvest_trial_corpus(
        self,
        benchmark: str,
        fuzzer: str,
        trial_corpus_dir: Path,
        tmp_dir: Path,
        max_backtrack: int = 50,
    ) -> Set[str]:
        """Extract seeds from a trial's corpus archive.

        Returns set of SHA256 hashes of discovered seeds.
        """
        archives = list_archives_desc(trial_corpus_dir)
        if not archives:
            raise FileNotFoundError(f"No corpus archives in {trial_corpus_dir}")

        tried = 0
        for idx, ap in archives:
            tried += 1
            if tried > max_backtrack:
                break

            work = tmp_dir / f"{benchmark}-try-{idx}"
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(parents=True, exist_ok=True)

            try:
                safe_extract_tar_gz(ap, work)
                flatten_if_single_topdir(work)

                seed_files = seed_files_from_extracted(work, fuzzer)
                if not seed_files:
                    continue

                discovered: Set[str] = set()
                for sf in seed_files:
                    h = self.store_seed(benchmark, sf)
                    discovered.add(h)

                return discovered
            finally:
                shutil.rmtree(work, ignore_errors=True)

        raise RuntimeError(
            f"All archives empty for {trial_corpus_dir} "
            f"(tried {min(tried, max_backtrack)})"
        )

    def materialize_seeds(
        self,
        benchmark: str,
        hashes: Set[str],
        dest_dir: Path,
    ) -> int:
        """Write seed files (by hash) to dest_dir/<benchmark>/. Returns count."""
        bench_dir = dest_dir / benchmark
        if bench_dir.exists():
            shutil.rmtree(bench_dir)
        bench_dir.mkdir(parents=True, exist_ok=True)

        store = self._store_dir(benchmark)
        count = 0
        for h in hashes:
            src = store / h
            if src.exists():
                shutil.copy2(src, bench_dir / h)
                count += 1
        return count

    def get_trial_corpus_dir(
        self,
        experiment_filestore: Path,
        experiment_name: str,
        benchmark: str,
        fuzzer: str,
        trial_num: int,
    ) -> Path:
        """Locate a trial's corpus directory in the FuzzBench filestore."""
        bench_root = (
            experiment_filestore
            / experiment_name
            / "experiment-folders"
            / f"{benchmark}-{fuzzer}"
        )
        # FuzzBench uses trial-1, trial-2, etc. but actual IDs may vary
        # Look for trial directories and pick by index
        trial_dirs = sorted(
            [p for p in bench_root.glob("trial-*") if p.is_dir()],
            key=lambda p: int(p.name.split("-", 1)[1])
        )
        if trial_num >= len(trial_dirs):
            raise FileNotFoundError(
                f"Trial {trial_num} not found in {bench_root}. "
                f"Available: {[p.name for p in trial_dirs]}"
            )
        corpus_dir = trial_dirs[trial_num] / "corpus"
        if not corpus_dir.exists():
            raise FileNotFoundError(f"Corpus dir missing: {corpus_dir}")
        return corpus_dir

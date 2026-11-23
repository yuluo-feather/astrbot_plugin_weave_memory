"""Mirror the dev working tree to the AstrBot runtime plugin directory.

The plugin market installs a GitHub archive snapshot, while developer testing
uses a full mirror of the dev working tree. This script copies dev files
(including gitignored changelogs/docs/tests) into the runtime directory and
then verifies every non-excluded file by md5.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".claude", "logs", "log", "dist",
}


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.is_file():
            files.append(rel)
    return sorted(files)


def _digest(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    return {rel.as_posix(): _digest(root / rel) for rel in _iter_files(root)}


def _sync_paths(src: Path, entries: list[str]) -> list[Path]:
    result: list[Path] = []
    for entry in entries:
        rel = Path(entry)
        if rel.is_absolute() or ".." in rel.parts:
            raise SystemExit(f"refusing unsafe sync path: {entry}")
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if not (src / rel).exists():
            raise SystemExit(f"sync path does not exist: {entry}")
        result.append(rel)
    return result


def _copy(src: Path, dst: Path, rels: list[Path]) -> None:
    for rel in rels:
        source = src / rel
        if source.is_dir():
            for path in source.rglob("*"):
                sub = path.relative_to(src)
                if any(part in EXCLUDED_DIRS for part in sub.parts) or not path.is_file():
                    continue
                target = dst / sub
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dst", required=True, type=Path)
    parser.add_argument("--sync", nargs="*", default=None, metavar="PATH")
    parser.add_argument(
        "--prune", action="store_true",
        help="delete files in dst that do not exist in src",
    )
    args = parser.parse_args()

    src = args.src.resolve()
    dst = args.dst.resolve()
    if not src.is_dir() or not dst.is_dir():
        raise SystemExit("--src and --dst must be existing directories")

    if args.sync is not None:
        if not args.sync:
            raise SystemExit("--sync requires at least one path (an empty list copies nothing)")
        _copy(src, dst, _sync_paths(src, args.sync))

    if args.prune:
        src_files = {rel.as_posix() for rel in _iter_files(src)}
        for rel in _iter_files(dst):
            if rel.as_posix() not in src_files:
                (dst / rel).unlink()

    left = _snapshot(src)
    right = _snapshot(dst)
    only_src = sorted(set(left) - set(right))
    only_dst = sorted(set(right) - set(left))
    mismatched = sorted(key for key in set(left) & set(right) if left[key] != right[key])
    if only_src or only_dst or mismatched:
        print(f"only in src: {len(only_src)}")
        for key in only_src[:20]:
            print(f"  {key}")
        print(f"only in dst: {len(only_dst)}")
        for key in only_dst[:20]:
            print(f"  {key}")
        print(f"content mismatch: {len(mismatched)}")
        for key in mismatched[:20]:
            print(f"  {key}")
        return 1
    print(f"ok: {len(left)} files match")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Weave Memory preflight: package size, residue, credentials, paths and versions.

Localized from Star Feather's ``preflight.py``. The generic checks are kept;
tarot assets / bilingual README checks are replaced with Weave's version
holders (metadata.yaml, pyproject.toml, README badge/alt) and an optional
public-distribution surface check.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

LIMIT_BYTES = 16 * 1024 * 1024
JUNK_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".claude", "logs", "log", "dist"}
CACHE_DIRS = (".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__")
JUNK_FILES = {"commit_msg.txt"}
JUNK_SUFFIXES = (".bak", ".pyc")
TEXT_EXTS = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql", ".csv"}
# Dev-only trees are not published; skip them for secrets/IDs/paths.
DEV_ONLY_PREFIXES = ("docs/", "decisions/", "eval/", "tests/evaluation/")
DEV_ONLY_FILES = {
    "AGENTS.md", "CLAUDE.md", "AGENTS.generic.md",
    "scripts/preflight.py",  # contains the detection regexes themselves
}
PUBLIC_FORBIDDEN = (
    "docs", "decisions", "eval", "tests/evaluation",
    "scripts/build_astrbot_plugin.py", "scripts/sync_runtime.py",
    "scripts/check_hygiene.py", "scripts/evaluate_v14.py",
    "scripts/evaluate_v15_extraction.py", "scripts/injection_rubric.py",
)

SECRET_PATTERNS = (
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub token"),
    (r"sk-[A-Za-z0-9]{20,}", "API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----", "private key"),
)
ABS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/d/astrbot|D:/astrbot", re.IGNORECASE)
LONG_ID = re.compile(r"(?<![\w/])\d{9,12}(?!\w)")


def _text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _files(root: Path) -> list[Path]:
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in JUNK_DIRS]
        for name in filenames:
            result.append(Path(dirpath) / name)
    return result


def check_size(root: Path) -> bool:
    total = sum(path.stat().st_size for path in _files(root) if path.is_file())
    ok = total <= LIMIT_BYTES
    print(f"[包体] {total / 1e6:.2f} MB / 16 MB —— {'通过' if ok else '超限'}")
    return ok


def check_junk(root: Path) -> bool:
    hits = [path for path in _files(root)
            if path.name in JUNK_FILES or path.name.endswith(JUNK_SUFFIXES)]
    hits += [Path(dirpath) / name for dirpath, dirnames, _ in os.walk(root)
             for name in dirnames if name in CACHE_DIRS]
    if hits:
        print("[残留] 发现残留：")
        for path in hits[:20]:
            print(f"   {path}")
        return False
    print("[残留] 干净")
    return True


def check_content(root: Path) -> bool:
    hits: list[str] = []
    warns: list[str] = []
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() not in TEXT_EXTS:
            continue
        if rel.startswith(DEV_ONLY_PREFIXES) or rel in DEV_ONLY_FILES:
            continue
        text = _text(path)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for pattern, label in SECRET_PATTERNS:
                if re.search(pattern, line):
                    hits.append(f"{rel}:{line_number}: {label}")
            if ABS_PATH.search(line):
                warns.append(f"{rel}:{line_number}: local absolute path")
            if path.suffix.lower() in {".md", ".txt"} and LONG_ID.search(line):
                warns.append(f"{rel}:{line_number}: long numeric ID")
    if hits:
        print("[密钥] 高置信凭据泄露：")
        for item in hits:
            print(f"   x {item}")
    if warns:
        print("[密钥] 需人工确认：")
        for item in warns:
            print(f"   ? {item}")
    if not hits and not warns:
        print("[密钥] 干净")
    return not hits and not warns


def _normalize_version(raw: str) -> str:
    value = raw.strip().lstrip("vV").replace("--", "-")
    return re.sub(r"\.dev(\d+)", r"-dev.\1", value)


def check_versions(root: Path) -> bool:
    metadata = _text(root / "metadata.yaml") or ""
    pyproject = _text(root / "pyproject.toml") or ""
    readme = _text(root / "README.md") or ""
    values = {
        "metadata": re.search(r"(?m)^version:\s*(\S+)", metadata),
        "pyproject": re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject),
        "badge": re.search(r"version-v([0-9][0-9.]*(?:--dev\.[0-9]+)?)", readme),
        "alt": re.search(r'alt="v([0-9][^"]*)"', readme),
    }
    found = {key: _normalize_version(match.group(1)) if match else None for key, match in values.items()}
    print("[版本] " + " / ".join(f"{key}={value}" for key, value in found.items()))
    if any(value is None for value in found.values()):
        print("   x 版本载体缺失")
        return False
    if len(set(found.values())) != 1:
        print("   x 版本不一致")
        return False
    return True


def check_public_surface(root: Path) -> bool:
    marker = root / ".public-repo"
    if not marker.exists():
        return True
    hits = [path for path in PUBLIC_FORBIDDEN if (root / path).exists()]
    if hits:
        print("[公开面] 发现不应分发的内容：")
        for path in hits:
            print(f"   x {path}")
        return False
    required = ("main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md")
    missing = [path for path in required if not (root / path).exists()]
    if missing:
        print("[公开面] 缺少必要文件：")
        for path in missing:
            print(f"   x {path}")
        return False
    print("[公开面] 通过")
    return True


def check_git(dev: Path | None, repo: Path | None) -> bool:
    ok = True
    heads: dict[str, str] = {}
    for label, root in (("dev", dev), ("repo", repo)):
        if root is None:
            continue
        try:
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[git {label}] 检查失败：{exc}")
            ok = False
            continue
        heads[label] = head
        print(f"[git {label}] {head}")
        tracked = [line for line in dirty.splitlines() if not line.startswith("??")]
        if tracked:
            print(f"   x 已跟踪文件有未提交改动：{tracked[:5]}")
            ok = False
    if len(heads) == 2 and len(set(heads.values())) != 1:
        print("   x dev 与 repos 不同源")
        ok = False
    return ok


def _tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/"):
            continue
        result[rel] = hashlib.md5(path.read_bytes()).hexdigest()
    return result


def check_runtime(dev: Path, run: Path) -> bool:
    left, right = _tree_digest(dev), _tree_digest(run)
    only_dev = sorted(set(left) - set(right))
    only_run = sorted(set(right) - set(left))
    mismatch = sorted(key for key in set(left) & set(right) if left[key] != right[key])
    if only_dev or only_run or mismatch:
        print(f"[双目录] 不一致：仅 dev {len(only_dev)} / 仅运行目录 {len(only_run)} / 内容 {len(mismatch)}")
        for key in (only_dev + only_run + mismatch)[:10]:
            print(f"   {key}")
        return False
    print(f"[双目录] {len(left)} 文件一致")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dev", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--ci", action="store_true", help="only run static checks")
    args = parser.parse_args()

    root = args.root.resolve()
    target = args.repo.resolve() if args.repo else root
    ok = True
    ok &= check_size(target)
    ok &= check_junk(target)
    ok &= check_content(target)
    ok &= check_versions(target)
    ok &= check_public_surface(target)
    if not args.ci:
        ok &= check_git(args.dev, args.repo)
        if args.dev and args.run:
            ok &= check_runtime(args.dev.resolve(), args.run.resolve())
    print("[结果] " + ("通过" if ok else "存在阻断项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

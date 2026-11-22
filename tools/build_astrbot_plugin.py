"""Assemble a self-contained AstrBot plugin directory from the canonical package."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "plugin" / "astrbot_plugin_weave_memory"
PACKAGE_SOURCE = ROOT / "weavemem"
DIST = ROOT / "dist"
PLUGIN_NAME = "astrbot_plugin_weave_memory"


def main() -> None:
    DIST.mkdir(exist_ok=True)
    output_dir = DIST / PLUGIN_NAME
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    for filename in ("main.py", "metadata.yaml", "requirements.txt"):
        shutil.copy2(PLUGIN_SOURCE / filename, output_dir / filename)
    shutil.copytree(
        PACKAGE_SOURCE,
        output_dir / "weavemem",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    archive = DIST / f"{PLUGIN_NAME}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(output_dir.parent))
    print(f"Plugin directory: {output_dir}")
    print(f"Plugin archive: {archive}")


if __name__ == "__main__":
    main()

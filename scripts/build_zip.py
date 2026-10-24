"""Build the AstrBot-installable zip.

Usage:
    python scripts/build_zip.py

The archive must keep metadata.yaml at its root and must not contain tests,
.git, virtualenvs or caches (see AstrBot plugin packaging rules).
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FILES = [
    "metadata.yaml",
    "main.py",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
]
REQUIRED_METADATA = ("name", "desc", "version", "author")


def metadata_version() -> str:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    version = re.search(r"(?m)^version:\s*['\"]?(.+?)['\"]?\s*$", text)
    name = re.search(r"(?m)^name:\s*['\"]?(.+?)['\"]?\s*$", text)
    if not version or not name:
        raise SystemExit("metadata.yaml 缺少 name 或 version")
    for field in REQUIRED_METADATA:
        if not re.search(rf"(?m)^{field}:\s*\S", text):
            raise SystemExit(f"metadata.yaml 缺少字段 {field}")
    return version.group(1).strip().lstrip("v")


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    version = metadata_version()
    target = DIST / f"astrbot_plugin_bili_media-v{version}.zip"

    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"缺少文件：{', '.join(missing)}")

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, arcname=name)

    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        if "metadata.yaml" not in names:
            raise SystemExit("打包后的 zip 根目录没有 metadata.yaml")

    print(f"built: {target}")
    print(f"size:  {target.stat().st_size} bytes")
    print("files:")
    for name in FILES:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

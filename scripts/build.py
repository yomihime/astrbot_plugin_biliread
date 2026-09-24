#!/usr/bin/env python3
"""Build a deterministic AstrBot plugin ZIP and its SHA-256 checksum."""

import argparse
import ast
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PACKAGE_FILES = (
    "_conf_schema.json",
    "bcut_asr.py",
    "bili_login.py",
    "images/QQ_1775891891399.png",
    "LICENSE",
    "logo.png",
    "main.py",
    "metadata.yaml",
    "README.md",
    "requirements.txt",
)
VERSION_PATTERN = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\Z")


def metadata_value(source: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(\S.*?)\s*$", source, re.MULTILINE)
    if not match:
        raise ValueError(f"metadata.yaml 缺少 {key}")
    return match.group(1).strip("\"'")


def release_notes(tag: str) -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(rf"^## \[{re.escape(tag)}\] - .+$", changelog, re.MULTILINE)
    if not heading:
        raise ValueError(f"CHANGELOG.md 缺少 {tag} 版本小节")
    next_heading = re.search(r"^## ", changelog[heading.end() :], re.MULTILINE)
    end = heading.end() + next_heading.start() if next_heading else len(changelog)
    return changelog[heading.start() : end].strip() + "\n"


def validate_sources(tag: str) -> tuple[str, str]:
    for relative in PACKAGE_FILES:
        if not (ROOT / relative).is_file():
            raise ValueError(f"缺少打包文件: {relative}")

    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    plugin_name = metadata_value(metadata, "name")
    version = metadata_value(metadata, "version")
    if not re.fullmatch(r"astrbot_plugin_[a-z0-9_]+", plugin_name):
        raise ValueError(f"无效的插件名: {plugin_name}")
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"无效的版本号: {version}")
    if tag != version:
        raise ValueError(f"tag {tag} 与 metadata.yaml 版本 {version} 不一致")

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("_conf_schema.json 必须是 JSON 对象")
    for relative in ("main.py", "bili_login.py", "bcut_asr.py"):
        ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    versions = [
        decorator.args[3].value
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BiliRead"
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "register"
        and len(decorator.args) >= 4
        and isinstance(decorator.args[3], ast.Constant)
    ]
    if versions != [version.removeprefix("v")]:
        raise ValueError("main.py 的 @register 版本与 metadata.yaml 不一致")
    return plugin_name, version


def build(tag: str) -> Path:
    plugin_name, version = validate_sources(tag)
    notes = release_notes(version)
    DIST.mkdir(exist_ok=True)
    archive_path = DIST / f"{plugin_name}-{version}.zip"

    with zipfile.ZipFile(archive_path, "w") as archive:
        for relative in sorted(PACKAGE_FILES):
            entry = zipfile.ZipInfo(f"{plugin_name}/{relative}", (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o644 << 16
            archive.writestr(
                entry,
                (ROOT / relative).read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )

    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP 校验失败")
        expected = {f"{plugin_name}/{name}" for name in PACKAGE_FILES}
        if set(archive.namelist()) != expected:
            raise ValueError("ZIP 文件清单与预期不符")

    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (DIST / f"{plugin_name}-{version}.sha256").write_text(
        f"{checksum}  {archive_path.name}\n", encoding="utf-8"
    )
    (DIST / "release-notes.md").write_text(notes, encoding="utf-8")
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="要求与 metadata.yaml 完全一致的版本 tag")
    args = parser.parse_args()
    try:
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        tag = args.tag or metadata_value(metadata, "version")
        archive_path = build(tag)
    except (OSError, ValueError, SyntaxError, json.JSONDecodeError) as error:
        print(f"构建失败: {error}", file=sys.stderr)
        return 1
    print(f"构建完成: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

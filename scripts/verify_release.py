"""Verify checksums and exact runtime sources in a source-only release archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

from write_build_info import ROOT, runtime_manifest

sys.path.insert(0, str(ROOT))
from spark_assistant import __version__


def verify(app_dir: Path, installer: Path) -> dict:
    expected = runtime_manifest()
    resources = app_dir / ("Contents/Resources" if app_dir.suffix == ".app" else "_internal")
    packaged = json.loads((resources / "build-info.json").read_text(encoding="utf-8"))
    if packaged != expected:
        raise RuntimeError("Packaged runtime differs from current source")
    source = ROOT / "release" / f"SparkAssistant-{__version__}-source.zip"
    prefix = f"SparkAssistant-{__version__}/"
    for artifact in (source, installer):
        recorded = artifact.with_suffix(artifact.suffix + ".sha256").read_text(encoding="ascii").strip()
        actual = f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}"
        if recorded != actual:
            raise RuntimeError(f"Checksum does not match: {artifact.name}")
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise RuntimeError("Source archive integrity failed")
        for name in names:
            path = Path(name[len(prefix):])
            if (not name.startswith(prefix) or path.is_absolute() or ".." in path.parts
                    or set(path.parts).intersection({".local", ".venv", "__pycache__", "profiles", "accounts", "logs"})
                    or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pyc"}):
                raise RuntimeError("Unexpected private or generated path in source archive")
        for name, digest in expected["files"].items():
            if hashlib.sha256(archive.read(prefix + name)).hexdigest() != digest:
                raise RuntimeError(f"Source archive runtime is stale: {name}")
        if prefix + "LICENSE" not in names or prefix + f"docs/{__version__}测试说明.md" not in names:
            raise RuntimeError("Release license or test notes missing")
    return {"version": __version__, "source_sha256": expected["source_sha256"],
            "source_files": len(names), "runtime_files": len(expected["files"]),
            "archive_scope": "source-only", "checksum_and_runtime_match": "passed",
            "installer_bytes": installer.stat().st_size, "source_bytes": source.stat().st_size}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=ROOT / "dist/SparkAssistant")
    parser.add_argument("--installer", type=Path,
                        default=ROOT / f"release/SparkAssistant-Setup-{__version__}-windows-x64.exe")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(verify(args.app_dir.resolve(), args.installer.resolve()), indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)

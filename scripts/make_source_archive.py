"""Create a source-only archive using an explicit allowlist, never local profiles."""
from pathlib import Path
import hashlib
import zipfile
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from spark_assistant import __version__

single_files = ["README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore",
                "requirements.txt", "requirements-build.txt", "desktop_main.py",
                "douyin_demo.py", "selectors.json", "docs/桌面版验收记录.md", "docs/0.1.2测试说明.md",
                "docs/0.2.0测试说明.md", "docs/0.2.1测试说明.md"]
source_dirs = ["spark_assistant", "tests", "packaging", ".github", "third_party", "assets"]
scripts = ["build-windows.ps1", "build-macos.sh", "make_icons.py", "collect_licenses.py",
           "make_source_archive.py", "smoke_release.py", "test-installer-directory.ps1", "write_build_info.py",
           "make_uninstall_manifest.py", "test-installer-safety.ps1", "test-installer-autostart.ps1",
           "verify_release.py"]
paths = [ROOT / name for name in single_files]
paths += [ROOT / "scripts" / name for name in scripts]
for directory in source_dirs:
    paths += [path for path in (ROOT / directory).rglob("*")
              if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
output = ROOT / "release" / f"SparkAssistant-{__version__}-source.zip"
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise RuntimeError(f"Refusing source path outside workspace: {path}")
        archive.write(path, Path(f"SparkAssistant-{__version__}") / path.relative_to(ROOT))
print(output)
output.with_suffix(output.suffix + ".sha256").write_text(
    f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n", encoding="ascii")

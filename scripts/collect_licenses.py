"""Collect redistribution notices without collecting any user data."""
from importlib import metadata
from pathlib import Path
import shutil
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "build/third-party-licenses"
TARGET.mkdir(parents=True, exist_ok=True)
packages = ["playwright", "pyee", "greenlet", "typing_extensions", "PySide6-Essentials", "shiboken6"]
inventory = []
for name in packages:
    dist = metadata.distribution(name)
    inventory.append(f"{name}=={dist.version}")
    for item in dist.files or []:
        if any(word in item.name.upper() for word in ("LICENSE", "COPYING", "NOTICE")):
            source = Path(dist.locate_file(item))
            if source.is_file():
                dest = TARGET / name / item.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)

python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.exists():
    shutil.copy2(python_license, TARGET / "Python-LICENSE.txt")
# The PySide wheels contain the commercial notice but not all open-source license
# texts. Keep copies of the licenses chosen for redistribution alongside them.
urls = {
    "LGPL-3.0.txt": "https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/LGPL-3.0-only.txt",
    "GPL-3.0.txt": "https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/GPL-3.0-only.txt",
}
for filename, url in urls.items():
    dest = TARGET / filename
    vendored = ROOT / "third_party" / filename
    if vendored.exists():
        shutil.copy2(vendored, dest)
    if not dest.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            dest.write_bytes(response.read())
(TARGET / "versions.txt").write_text("\n".join(inventory) + "\n", encoding="utf-8")
print(f"Collected notices: {TARGET}")

"""Fingerprint runtime sources before packaging, without local account data."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from spark_assistant import __version__


def runtime_manifest():
    paths = [ROOT / name for name in ("desktop_main.py", "douyin_demo.py", "selectors.json", "requirements.txt")]
    paths += sorted((ROOT / "spark_assistant").glob("*.py"))
    hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(paths)}
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"version": __version__, "scope": "runtime-source",
            "source_sha256": hashlib.sha256(encoded).hexdigest(), "files": hashes}


if __name__ == "__main__":
    destination = ROOT / "build/build-info.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(runtime_manifest(), indent=2) + "\n", encoding="utf-8")
    print(destination)

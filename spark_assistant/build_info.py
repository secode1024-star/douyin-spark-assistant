"""Read the optional packaged source fingerprint; never access the network."""
import json

from .paths import resource_path


def packaged_build_info() -> dict:
    path = resource_path("build-info.json")
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

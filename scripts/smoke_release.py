"""Run the frozen app and bundled browser against disposable local fixtures."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from write_build_info import runtime_manifest

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("--app-dir", type=Path, default=ROOT / "dist/SparkAssistant")
parser.add_argument("--output-dir", type=Path, default=ROOT / ".local/release-check")
args = parser.parse_args()
folder = args.app_dir.resolve()
output = args.output_dir.resolve()
output.mkdir(parents=True, exist_ok=True)
if sys.platform == "win32":
    gui = folder / "SparkAssistant.exe"
    worker = folder / "SparkWorker.exe"
else:
    gui = folder / "Contents/MacOS/SparkAssistant"
    worker = gui
env = os.environ.copy()
env["QT_QPA_PLATFORM"] = "offscreen"
env["SPARK_SMOKE_TEST"] = "1"
for key in ("PYTHONPATH", "PYTHONHOME", "SPARK_BROWSER_CHANNEL", "PLAYWRIGHT_BROWSERS_PATH"):
    env.pop(key, None)
if sys.platform == "win32":
    env["PATH"] = str(Path(env.get("WINDIR", "C:/Windows")) / "System32")
flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
expected_build = runtime_manifest()
for executable, option, filename in ((gui, "--smoke-test", "ui.json"),
                                     (worker, "--browser-smoke-test", "browser.json")):
    destination = output / filename
    destination.unlink(missing_ok=True)
    completed = subprocess.run([str(executable), option, str(destination)], env=env,
                               cwd=output, timeout=120, capture_output=True,
                               creationflags=flags)
    if completed.returncode != 0 or not destination.exists():
        diagnostic = completed.stderr.decode("utf-8", errors="replace")[-2500:]
        raise RuntimeError(f"{filename} failed with exit {completed.returncode}: {diagnostic}")
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report.get("version") == expected_build["version"], "Frozen application version is stale"
    assert report.get("build", {}).get("source_sha256") == expected_build["source_sha256"], "Frozen runtime sources are stale"
    if filename == "ui.json":
        assert report["ui"] == "ok" and report["worker_protocol"] == "logged_out", report
        assert len(report.get("v020_state", {})) >= 12, report
        assert all(value == "passed" for value in report["v020_state"].values()), report
    else:
        assert report["status"] == "passed", report
        for check in ("login_button_variants", "login_restore_wait", "login_multiple_tabs",
                      "incoming_cannot_confirm_send", "missing_id_stays_uncertain", "submission_storage_fault"):
            assert report.get(check) == "passed", (check, report)
        for check in ("identity_routing", "identity_mismatch_before_fill", "official_editor_placeholder"):
            assert report.get(check) is True, (check, report)
    summary = {key: value for key, value in report.items() if key != "build"}
    summary["source_sha256"] = report["build"]["source_sha256"]
    print(json.dumps({filename: summary}, ensure_ascii=False))
print("Frozen application, worker IPC, bundled browser and profile persistence passed.")

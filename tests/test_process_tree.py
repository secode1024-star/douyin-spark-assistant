"""Windows-only offline cleanup proof using a temporary locked file."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from spark_assistant.process_tree import ProcessTreeGuard


@unittest.skipUnless(sys.platform == "win32", "Windows Job Objects")
class WindowsProcessTreeTests(unittest.TestCase):
    def test_close_kills_only_owned_tree_and_releases_child_lock(self):
        import msvcrt

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            locked = root / "fixture.lock"
            ready = root / "child-ready.txt"
            child_code = """
import msvcrt,pathlib,sys,time
locked=pathlib.Path(sys.argv[1]);ready=pathlib.Path(sys.argv[2])
with locked.open('w+b') as handle:
    handle.write(b'0');handle.flush();handle.seek(0)
    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    ready.write_text('ready')
    time.sleep(60)
"""
            parent_code = """
import subprocess,sys,time
sys.stdin.buffer.readline()
subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]],
                 creationflags=subprocess.CREATE_NO_WINDOW)
time.sleep(60)
"""
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_code, child_code, str(locked), str(ready)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            guard = None
            try:
                guard = ProcessTreeGuard(parent.pid)
                parent.stdin.write(b"start\n")
                parent.stdin.flush()
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "child never acquired its temporary lock")
                with locked.open("r+b") as handle:
                    with self.assertRaises(OSError):
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                guard.close()
                guard.close()  # Idempotent; cannot close a newly reused handle.
                parent.wait(timeout=5)
                self.assertIsNone(unrelated.poll(), "an unrelated process was terminated")
                deadline = time.monotonic() + 5
                while True:
                    with locked.open("r+b") as handle:
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise
                        else:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                            break
                    time.sleep(0.02)
            finally:
                if guard:
                    guard.close()
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                if parent.stdin:
                    parent.stdin.close()
                unrelated.kill()
                unrelated.wait(timeout=5)

    def test_invalid_process_is_rejected(self):
        with self.assertRaises(ValueError):
            ProcessTreeGuard(0)
        with self.assertRaises(OSError):
            ProcessTreeGuard(0xFFFFFFFE)


if __name__ == "__main__":
    unittest.main()

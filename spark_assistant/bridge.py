"""Qt bridge for one isolated browser operation at a time.

The worker protocol contains application events, never browser credentials.
No browser or Playwright object is shared with the GUI process.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .process_tree import ProcessTreeGuard


class WorkerBridge(QObject):
    progress = Signal(str)
    result = Signal(dict)
    busy_changed = Signal(bool)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.process = None
        self.command = ""
        self._buffer = bytearray()
        self._payload = b""
        self._final = None
        self._reason = ""
        self._completed = False
        self._tree_guard = None
        self._request_written = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill_current)

    @property
    def busy(self):
        return self.process is not None

    def start(self, command: str, **kwargs):
        if self.busy:
            raise RuntimeError("另一个操作正在进行，请稍候。")
        if command not in {"login", "check", "contacts", "sync_friends", "send", "logout"}:
            raise ValueError("未知操作。")
        self.command = command
        self._buffer.clear()
        self._final = None
        self._reason = ""
        self._completed = False
        self._request_written = False
        self._payload = (json.dumps({
            **kwargs, "command": command, "data_dir": str(self.paths.data_dir),
        }, ensure_ascii=False) + "\n").encode("utf-8")
        process = QProcess(self)
        self.process = process
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(env)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.started.connect(self._write_request)
        process.readyReadStandardOutput.connect(self._read_output)
        # Drain stderr without including arbitrary browser diagnostics in UI or logs.
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        program = sys.executable
        if getattr(sys, "frozen", False) and sys.platform == "win32":
            # Windowed PyInstaller applications do not have Python stdio streams.
            # The separate console-mode helper receives redirected QProcess pipes.
            program = str(Path(sys.executable).with_name("SparkWorker.exe"))
        process.setProgram(program)
        if getattr(sys, "frozen", False):
            process.setArguments(["--worker"])
        else:
            entry = Path(self.paths.resource_dir) / "desktop_main.py"
            process.setArguments(["-u", str(entry), "--worker"])
        process.setWorkingDirectory(str(self.paths.data_dir))
        self.busy_changed.emit(True)
        process.start()
        timeout_minutes = 10 if command == "login" else (6 if command == "sync_friends" else 3)
        self._timeout.start(timeout_minutes * 60_000)

    def _write_request(self):
        if self.process:
            try:
                # The worker has not read a command yet, so no browser exists.
                # Attach before writing stdin; descendants inherit the job.
                self._tree_guard = ProcessTreeGuard(int(self.process.processId()))
            except (OSError, ValueError) as exc:
                self._final = {
                    "event": "result", "status": "failed",
                    "detail": f"无法建立浏览器进程清理保护，本次操作未开始。请重新启动软件后再试。系统信息：{exc}",
                }
                self.process.kill()
                return
            self.process.write(self._payload)
            self._request_written = True
            self.process.closeWriteChannel()
            self._payload = b""

    def _read_output(self):
        if self.process is None:
            return
        self._buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            self._consume_line(line)
        if len(self._buffer) > 1_000_000:
            self._buffer.clear()

    def _consume_line(self, line):
        try:
            event = json.loads(line.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return
        if event.get("event") == "progress":
            self.progress.emit(str(event.get("message", ""))[:2000])
        elif event.get("event") == "result":
            self._final = event

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._reason = "后台工作进程未能启动，请检查安装是否完整。"
            self._finish()

    def _finished(self, exit_code, exit_status):
        self._read_output()
        if self._buffer:
            self._consume_line(bytes(self._buffer))
            self._buffer.clear()
        if exit_code != 0 and not self._reason:
            self._reason = "后台工作进程意外退出。"
        self._finish()

    def _finish(self):
        if self._completed:
            return
        self._completed = True
        self._timeout.stop()
        self._kill_timer.stop()
        process = self.process
        self.process = None
        if self._tree_guard:
            self._tree_guard.close()
            self._tree_guard = None
        result = self._final
        if result is None:
            result = {
                "event": "result",
                "status": "uncertain" if self.command == "send" and self._request_written else "failed",
                "detail": self._reason or "工作进程未返回完整结果。",
            }
        result = {**result, "command": self.command}
        if process is not None:
            process.deleteLater()
        self.busy_changed.emit(False)
        self.result.emit(result)

    def cancel(self):
        if not self.process:
            return
        self._reason = "操作已停止；发送操作需要查看记录核对结果。"
        self.process.terminate()
        self._kill_timer.start(5000)

    def _timed_out(self):
        if self.process:
            self._reason = "操作等待超时。请检查网络、登录状态或抖音页面。"
            self.process.terminate()
            self._kill_timer.start(5000)

    def _kill_current(self):
        if self.process:
            self.process.kill()

    def shutdown(self):
        """Called only after UI has durably marked any active send uncertain."""
        self._timeout.stop()
        self._kill_timer.stop()
        if self.process:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(2000)
        if self._tree_guard:
            self._tree_guard.close()
            self._tree_guard = None

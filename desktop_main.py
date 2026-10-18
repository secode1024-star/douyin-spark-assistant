"""Desktop and isolated worker entry point. Import Qt only for the GUI."""
from __future__ import annotations

import argparse
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import tempfile

from spark_assistant import __version__
from spark_assistant.paths import AppPaths, configure_browser, data_directory, resource_path


def main() -> int:
    parser = argparse.ArgumentParser(description="火花助手 · 本地定时消息")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tray", action="store_true", help="启动后驻留托盘")
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--browser-smoke-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    configure_browser()
    if args.browser_smoke_test:
        from spark_assistant.diagnostics import browser_smoke
        return browser_smoke(args.browser_smoke_test)
    if args.worker:
        from spark_assistant.worker import run_worker
        return run_worker()

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtGui import QIcon, QFontDatabase, QFont
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
    from spark_assistant.store import Store
    from spark_assistant.scheduler import Scheduler
    from spark_assistant.ui import MainWindow
    from spark_assistant.instance import InstallerMutex

    app = QApplication(sys.argv[:1])
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and sys.platform == "win32":
        # Qt's offscreen plugin may not discover Windows fonts. This is only a
        # local QA aid; system fonts are never copied into release artifacts.
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
        if font_path.exists():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                app.setFont(QFont(families[0], 10))
    app.setApplicationName("SparkAssistant")
    app.setApplicationDisplayName("火花助手")
    app.setOrganizationName("SparkAssistant")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(resource_path("assets/icon.png"))))
    smoke_folder = tempfile.TemporaryDirectory(prefix="spark-ui-smoke-") if args.smoke_test else None
    paths = AppPaths(Path(smoke_folder.name) if smoke_folder else data_directory())
    paths.prepare()
    lock = QLockFile(str(paths.data_dir / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "火花助手已在运行", "请从系统托盘或菜单栏打开已运行的火花助手。")
        return 0
    installer_mutex = InstallerMutex() if not args.smoke_test else None
    handler = RotatingFileHandler(paths.data_dir / "logs/app.log", maxBytes=500_000,
                                 backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store(paths.db_path)
    scheduler = Scheduler(store)
    scheduler.recover_interrupted()
    # Persisted readiness is advisory. A fresh process must check before dispatching.
    store.set_setting("account_ready", False)
    v020_checks = {}
    if args.smoke_test:
        from spark_assistant.release_checks import check_v020_state, fixture_directory
        v020_checks = check_v020_state()
        store.apply_friend_sync(fixture_directory(), account_epoch=store.get_account()["account_epoch"])
        contacts = store.list_contacts()
        store.save_rule("同名测试好友", "今天也记得开心", "09:00", True, contact_id=contacts[0]["id"])
        store.save_rule("同名测试好友", "晚安，早点休息", "21:00", True, contact_id=contacts[0]["id"])
        store.save_rule("同名测试好友", "这是本地演示内容", None, False, contact_id=contacts[1]["id"])
        store.set_setting("paused", True)
        store.set_setting("account_ready", False)
        os.environ["SPARK_SMOKE_TEST"] = "1"
    window = MainWindow(store, scheduler, paths)
    if args.smoke_test:
        # Test must not contact Douyin or run jobs. The UI honors this paused state.
        window._navigate(1)
        window.show()
        def finish_smoke(result):
            from spark_assistant.build_info import packaged_build_info
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            args.smoke_test.write_text(json.dumps({"version": __version__, "ui": "ok",
                "build": packaged_build_info(), "v020_state": v020_checks,
                "friend_rows": window.rules_table.rowCount(),
                "worker_protocol": result.get("status"),
                "browser_bundled": resource_path("browsers").is_dir()},
                ensure_ascii=False, indent=2), encoding="utf-8")
            app.exit(0 if result.get("status") == "logged_out" else 1)
        def start_smoke_worker():
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.smoke_test.with_suffix(".png")))
            window.bridge.result.connect(finish_smoke)
            # A brand-new disposable directory has no account to log out of.
            # This checks the frozen QProcess helper and cleanup without network.
            window.bridge.start("logout")
        QTimer.singleShot(1200, start_smoke_worker)
        QTimer.singleShot(30_000, lambda: finish_smoke({"status": "timeout"}))
    elif not args.tray or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    app.setQuitOnLastWindowClosed(False)
    try:
        return app.exec()
    finally:
        window.timer.stop()
        window.bridge.shutdown()
        store.close()
        lock.unlock()
        if installer_mutex:
            installer_mutex.close()
        logging.shutdown()
        if smoke_folder:
            smoke_folder.cleanup()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())

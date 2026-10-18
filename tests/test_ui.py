"""Offline UI and QProcess checks: these fixtures cannot access Douyin."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["SPARK_SMOKE_TEST"] = "1"

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QSystemTrayIcon

from spark_assistant.bridge import WorkerBridge
from spark_assistant.paths import AppPaths
from spark_assistant.scheduler import Scheduler
from spark_assistant.store import CHINA_TZ, Store
from spark_assistant.ui import MainWindow, RuleDialog, FriendPicker


def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


class DesktopUiTests(unittest.TestCase):
    fixture_time = datetime(2030, 1, 2, 8, 0, tzinfo=CHINA_TZ)
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(Path(self.temp.name))
        self.paths.prepare()
        self.store = Store(self.paths.db_path)
        self._friends = {}
        self.scheduler = Scheduler(self.store)
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window = MainWindow(self.store, self.scheduler, self.paths)
        self.window.timer.stop()

    def tearDown(self):
        self.window._quitting = True
        self.window.bridge.shutdown()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.store.close()
        self.temp.cleanup()

    def _due_rule(self, recipient, send_time="08:01"):
        friend = self._contact(recipient)
        with patch("spark_assistant.store.local_now", return_value=self.fixture_time):
            return self.store.save_rule(recipient, "offline fixture", send_time, enabled=True, contact_id=friend["id"])

    def _contact(self, name, identity=None):
        identity = identity or "fixture-" + str(len(self._friends))
        self._friends[identity] = {"platform_id": identity, "profile_url": f"https://www.douyin.com/user/{identity}",
                                   "display_name": name, "relationship": "existing_chat", "source": "official_chat_ui",
                                   "identity_verified": True, "dm_verified": True, "conversation_id": "chat-" + identity,
                                   "conversation_type": "one_to_one"}
        self.store.apply_friend_sync({"account_validated": True,
                                      "account": {"platform_id": "fixture-self", "verification": "verified"},
                                      "friends": list(self._friends.values()),
                                      "coverage": {"status": "complete", "directory_kind": "existing_chats"}, "detail": "offline fixture"})
        return next(friend for friend in self.store.list_contacts() if friend["platform_id"] == identity)

    def _ready(self):
        self.window._on_result({"command": "check", "status": "ready", "detail": "fixture ready"})
        # Most regression tests target dispatch, not the separate auto-import
        # lifecycle. No fixture may accidentally open the real browser.
        self.window._pending_friend_sync = False
        self.window._friend_sync_attempted = True

    def test_four_pages_and_no_update_controls(self):
        self.assertEqual(self.window.pages.count(), 4)
        self.assertEqual(self.window.windowTitle(), "火花助手 · 源码运行")
        from PySide6.QtWidgets import QPushButton
        self.assertFalse(any("更新" in button.text() for button in self.window.findChildren(QPushButton)))

    def test_duplicate_friend_rules_are_distinct_rows(self):
        friend = self._contact("测试好友")
        first = self.store.save_rule("测试好友", "早上好", "08:00", enabled=True, contact_id=friend["id"])
        second = self.store.save_rule("测试好友", "晚安", "22:00", enabled=True, contact_id=friend["id"])
        self.window.refresh()
        self.assertNotEqual(first, second)
        self.assertEqual(self.window.rules_table.rowCount(), 2)
        self.assertEqual(self.window.stat_values[1].text(), "2")

    def test_pause_and_resume_are_persistent(self):
        self.window._toggle_pause()
        self.assertTrue(self.store.get_setting("paused"))
        self.assertIn("继续", self.window.pause_button.text())
        self.window._toggle_pause()
        self.assertFalse(self.store.get_setting("paused"))

    def test_login_result_and_logout_update_account(self):
        self.window._on_result({"command": "login", "status": "ready", "detail": "登录已核验", "contacts": ["测试好友"]})
        self.assertTrue(self.store.get_setting("account_ready"))
        self.assertEqual(self.store.list_contacts(), [], "bare chat names must not populate the verified directory")
        self.window._on_result({"command": "logout", "status": "logged_out", "detail": "已退出"})
        self.assertFalse(self.store.get_setting("account_ready"))
        self.assertFalse(any(friend["sendable"] for friend in self.store.list_contacts()))

    def test_initial_login_state_is_unverified_not_expired(self):
        self.assertEqual(self.window.account_badge.text(), "登录状态未验证")
        self.assertFalse(self.store.get_setting("account_ready"))
        self.assertNotIn("需要登录", self.window.account_badge.text())

    def test_check_in_progress_is_visible_and_does_not_authorize_dispatch(self):
        self.window._on_result({"command": "check", "status": "ready", "detail": "已核验"})
        with patch.object(self.window.bridge, "start") as start:
            self.window._start_operation("check")
        start.assert_called_once_with("check")
        self.assertEqual(self.window.account_badge.text(), "正在检查登录")
        self.assertFalse(self.store.get_setting("account_ready"))
        self.assertFalse(self.window._session_validated)

    def test_unavailable_or_failed_does_not_claim_login_expired(self):
        for status in ["unavailable", "failed"]:
            with self.subTest(status=status):
                self.window._on_result({"command": "check", "status": "ready", "detail": "先前已核验"})
                detail = "聊天页面加载超时，当前尚不能确认登录状态。"
                self.window._on_result({"command": "check", "status": status, "detail": detail})
                self.assertEqual(self.window.account_badge.text(), "登录状态待核验")
                self.assertIn(detail, self.window.account_description.text())
                self.assertEqual(self.window.status_strip.text(), detail)
                self.assertFalse(self.store.get_setting("account_ready"))
                self.assertFalse(self.window.sync_button.isEnabled())

    def test_only_explicit_login_required_requests_login(self):
        self.window._on_result({"command": "check", "status": "login_required", "detail": "页面显示官方登录入口"})
        self.assertEqual(self.window.account_badge.text(), "需要登录")
        self.assertIn("页面显示官方登录入口", self.window.account_description.text())
        self.assertFalse(self.store.get_setting("account_ready"))
        self.window._on_result({"command": "check", "status": "ready", "detail": "已核验"})
        self.assertEqual(self.window.account_badge.text(), "已连接")
        self.assertTrue(self.store.get_setting("account_ready"))
        self.assertTrue(self.window.sync_button.isEnabled())

    def test_check_launch_failure_remains_unverified_with_detail(self):
        with patch.object(self.window.bridge, "start", side_effect=OSError("工作进程暂时无法启动")):
            self.window._start_operation("check")
        self.assertEqual(self.window.account_badge.text(), "登录状态待核验")
        self.assertIn("工作进程暂时无法启动", self.window.account_description.text())
        self.assertFalse(self.store.get_setting("account_ready"))

    def test_closing_login_window_starts_check_without_claiming_ready(self):
        self.store.set_setting("account_ready", True)
        self.window._session_validated = True
        with patch.object(self.window.bridge, "start") as start:
            self.window._on_result({"command": "login", "status": "login_closed", "detail": "浏览器已关闭"})
            self.assertFalse(self.store.get_setting("account_ready"))
            self.assertFalse(self.window._session_validated)
            self.assertEqual(self.window.status_strip.text(), "登录窗口已关闭，正在检查登录状态…")
            start.assert_not_called()
            loop = QEventLoop()
            QTimer.singleShot(400, loop.quit)
            loop.exec()
        start.assert_called_once_with("check")
        self.assertFalse(self.store.get_setting("account_ready"))
        self.window._on_result({"command": "check", "status": "ready", "detail": "登录已核验"})
        self.assertTrue(self.store.get_setting("account_ready"))

    def test_login_closed_status_from_other_commands_does_not_launch_check(self):
        with patch("spark_assistant.ui.QTimer.singleShot") as scheduled:
            for command in ["check", "contacts", "logout", "send"]:
                with self.subTest(command=command):
                    self.window._on_result({"command": command, "status": "login_closed"})
            scheduled.assert_not_called()
        self.assertFalse(self.store.get_setting("account_ready"))

    def test_delayed_login_check_respects_quit_and_other_active_operation(self):
        with patch("spark_assistant.ui.QTimer.singleShot") as scheduled:
            self.window._on_result({"command": "login", "status": "login_closed"})
        callback = scheduled.call_args.args[1]
        with patch.object(self.window, "_start_operation") as start:
            self.window._quitting = True
            callback()
            start.assert_not_called()
            self.window._quitting = False
            with patch.object(WorkerBridge, "busy", new_callable=PropertyMock, return_value=True):
                callback()
                start.assert_not_called()

    def test_failed_login_does_not_automatically_check_or_claim_ready(self):
        with patch("spark_assistant.ui.QTimer.singleShot") as scheduled:
            self.window._on_result({"command": "login", "status": "failed", "detail": "登录窗口未能打开"})
            scheduled.assert_not_called()
        self.assertFalse(self.store.get_setting("account_ready"))

    def test_smoke_mode_does_not_start_browser(self):
        self.assertFalse(self.window.bridge.busy)
        self.assertFalse(self.store.get_setting("account_ready"))
        self.window._tick()
        self.assertFalse(self.window.bridge.busy)

    def test_active_rule_cannot_be_edited_or_deleted(self):
        rule_id = self.store.save_rule("测试好友", "测试", "09:00")
        self.window.refresh()
        self.window.rules_table.selectRow(0)
        self.window._active_run = {"id": "fixture", "rule_id": rule_id}
        self.window._update_selection_buttons()
        self.assertFalse(self.window.edit_button.isEnabled())
        self.assertFalse(self.window.delete_button.isEnabled())

    def test_dispatch_carries_date_gate_to_worker(self):
        run = {"id": "fixture", "rule_id": "rule", "recipient": "测试好友", "message": "测试",
               "scheduled_at": "2026-10-16T09:00:00+08:00", "scheduled_date": "2026-10-16",
               "platform_id": "peer-identity", "conversation_id": "verified-chat",
               "account_id": "owner-identity", "account_epoch": 7}
        with patch.object(self.scheduler, "tick", return_value=run), patch.object(self.window.bridge, "start") as start:
            self.window._tick()
        self.assertEqual(start.call_args.kwargs["scheduled_date"], "2026-10-16")
        self.assertEqual(start.call_args.kwargs["request_id"], "fixture")
        for key in ("platform_id", "conversation_id", "account_id", "account_epoch"):
            self.assertEqual(start.call_args.kwargs[key], run[key])

    def test_same_day_policy_is_fixed(self):
        self.assertEqual(self.store.get_setting("late_policy"), "same_day")

    def test_pre_send_login_failure_defers_original_run(self):
        self.window._active_run = {"id": "fixture", "rule_id": "rule"}
        self.store.set_setting("account_ready", True)
        with patch.object(self.scheduler, "defer") as defer, patch.object(self.scheduler, "complete") as complete:
            self.window._on_result({"command": "send", "status": "login_required", "detail": "请重新扫码",
                                   "scope": "account", "submission": "not_submitted", "account_validated": False})
        defer.assert_called_once_with("fixture", "请重新扫码", submission="not_submitted", code="")
        complete.assert_not_called()
        self.assertFalse(self.store.get_setting("account_ready"))
        self.assertIsNone(self.window._active_run)

    def test_uncertain_send_is_terminal_not_deferred(self):
        self.window._active_run = {"id": "fixture", "rule_id": "rule"}
        with patch.object(self.scheduler, "defer") as defer, patch.object(self.scheduler, "complete") as complete:
            self.window._on_result({"command": "send", "status": "uncertain", "detail": "需要核对"})
        complete.assert_called_once_with("fixture", "uncertain", "需要核对", submission=None, code="")
        defer.assert_not_called()

    def test_rule_dialog_rejects_blank_and_allows_existing_recipient(self):
        friend = self._contact("测试好友")
        dialog = RuleDialog([friend], parent=self.window)
        dialog._validate()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.error.text())
        dialog.set_contact(friend)
        dialog.message.setText("你好")
        dialog._validate()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.values()["recipient"], "测试好友")
        dialog.deleteLater()

    def test_login_pauses_existing_rules_until_verified_manual_resume(self):
        self._due_rule("old account recipient")
        self._ready()
        with patch.object(self.window.bridge, "start"):
            self.window._start_operation("login")
        self.assertTrue(self.store.get_setting("paused"))
        self.assertTrue(self.store.get_setting("login_review_required"))
        self.assertFalse(any(friend["sendable"] for friend in self.store.list_contacts()))
        self.window._toggle_pause()
        self.assertTrue(self.store.get_setting("paused"), "cannot bypass review while login is still unverified")
        self._ready()
        self.assertTrue(self.store.get_setting("paused"), "ready must not resume an earlier account's rules")
        self.assertIn("核对", self.window.pause_button.text())
        self.assertIn("已有任务保持暂停", self.window.account_description.text())
        self._friends.clear()
        self._due_rule("new rule created while paused", "08:02")
        with patch.object(self.window.bridge, "start") as start, patch(
            "spark_assistant.scheduler.local_now", return_value=self.fixture_time + timedelta(minutes=2)
        ):
            self.window._tick()
            start.assert_not_called()
            self.window._toggle_pause()
            self.assertFalse(self.store.get_setting("paused"))
            self.assertFalse(self.store.get_setting("login_review_required"))
            self.window._tick()
            start.assert_called_once()

    def test_first_login_without_old_rules_does_not_force_pause(self):
        with patch.object(self.window.bridge, "start"):
            self.window._start_operation("login")
        self._ready()
        self.assertFalse(self.store.get_setting("paused", False))
        self.assertFalse(self.store.get_setting("login_review_required", False))

    def test_saved_login_review_requirement_survives_window_restart(self):
        self.store.set_setting("login_review_required", True)
        self.store.set_setting("paused", False)
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            restarted = MainWindow(self.store, self.scheduler, self.paths)
        restarted.timer.stop()
        try:
            restarted._on_result({"command": "check", "status": "ready", "detail": "fixture"})
            self.assertTrue(self.store.get_setting("paused"))
            self.assertTrue(self.store.get_setting("login_review_required"))
        finally:
            restarted._quitting = True
            restarted.close()
            restarted.deleteLater()
            self.app.processEvents()

    def test_recipient_failure_does_not_block_another_due_friend(self):
        self._due_rule("missing recipient")
        self._due_rule("valid recipient", "08:02")
        self._ready()
        run = self.scheduler.tick(self.fixture_time + timedelta(minutes=1))
        self.window._active_run = run
        self.window._on_result({"command": "send", "status": "failed", "detail": "未找到好友",
                               "code": "recipient_not_found", "scope": "task",
                               "submission": "not_submitted", "account_validated": True})
        self.assertTrue(self.store.get_setting("account_ready"))
        self.assertEqual(self.window.account_badge.text(), "已连接")
        self.assertEqual(self.store.list_runs()[0]["status"], "failed")
        with patch.object(self.window.bridge, "start") as start, patch(
            "spark_assistant.scheduler.local_now", return_value=self.fixture_time + timedelta(minutes=2)
        ):
            self.window._tick()
        self.assertEqual(start.call_args.kwargs["recipient"], "valid recipient")

    def test_unknown_or_account_failure_still_closes_account_gate(self):
        variants = [
            {},
            {"scope": "account", "submission": "not_submitted", "account_validated": False},
            {"scope": "task", "submission": "possible", "account_validated": True},
            {"scope": "task", "submission": "not_submitted", "account_validated": False},
        ]
        for fields in variants:
            with self.subTest(fields=fields):
                self._ready()
                self.window._on_result({"command": "send", "status": "failed", "detail": "fixture", **fields})
                self.assertFalse(self.store.get_setting("account_ready"))

    def test_task_failure_does_not_authorize_previously_unverified_account(self):
        self.window._on_result({"command": "send", "status": "failed", "scope": "task",
                               "submission": "not_submitted", "account_validated": True})
        self.assertFalse(self.store.get_setting("account_ready"))

    def test_unavailable_without_submission_proof_is_terminal_uncertain(self):
        self.window._active_run = {"id": "fixture", "rule_id": "rule"}
        with patch.object(self.scheduler, "defer") as defer, patch.object(self.scheduler, "complete") as complete:
            self.window._on_result({"command": "send", "status": "unavailable", "detail": "unknown stage"})
        defer.assert_not_called()
        complete.assert_called_once_with("fixture", "uncertain", "unknown stage", submission=None, code="")

    def test_ordinary_modal_does_not_pause_other_due_tasks(self):
        self._due_rule("independent friend")
        self._ready()
        dialog = QDialog(self.window)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.show()
        self.app.processEvents()
        try:
            self.assertIs(QApplication.activeModalWidget(), dialog)
            with patch.object(self.window.bridge, "start") as start, patch(
                "spark_assistant.scheduler.local_now", return_value=self.fixture_time + timedelta(minutes=1)
            ):
                self.window._tick()
            start.assert_called_once()
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_editing_one_rule_dispatches_another_and_releases_hold_on_cancel(self):
        editing = self._due_rule("editing friend")
        self._due_rule("independent friend", "08:02")
        self._ready()
        rule = next(item for item in self.store.list_rules() if item["id"] == editing)

        def while_editing():
            self.assertIn(editing, self.window._held_rule_ids)
            with patch.object(self.window.bridge, "start") as start, patch(
                "spark_assistant.scheduler.local_now", return_value=self.fixture_time + timedelta(minutes=2)
            ):
                self.window._tick()
            self.assertEqual(start.call_args.kwargs["recipient"], "independent friend")
            pending = next(run for run in self.store.list_runs() if run["rule_id"] == editing)
            self.assertEqual(pending["status"], "pending")
            return QDialog.DialogCode.Rejected

        with patch.object(RuleDialog, "exec", side_effect=while_editing):
            self.window._rule_dialog(rule)
        self.assertNotIn(editing, self.window._held_rule_ids)

    def test_delete_confirmation_only_holds_its_own_rule(self):
        deleting = self._due_rule("deleting friend")
        self._due_rule("independent friend", "08:02")
        self._ready()
        self.window.refresh()
        for row in range(self.window.rules_table.rowCount()):
            if self.window.rules_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == deleting:
                self.window.rules_table.selectRow(row)
                break

        def while_confirming(*args):
            self.assertIn(deleting, self.window._held_rule_ids)
            self.window._tick()
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "question", side_effect=while_confirming), patch.object(
            self.window.bridge, "start"
        ) as start, patch("spark_assistant.scheduler.local_now", return_value=self.fixture_time + timedelta(minutes=2)):
            self.window._delete_rule()
        self.assertEqual(start.call_args.kwargs["recipient"], "independent friend")
        self.assertNotIn(deleting, self.window._held_rule_ids)
        self.assertEqual(len(self.store.list_rules()), 2)

    def test_logout_confirmation_holds_dispatch_and_releases_on_cancel(self):
        self._due_rule("friend")
        self._ready()

        def while_confirming(*args):
            self.assertGreater(self.window._dispatch_hold_depth, 0)
            self.window._tick()
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "question", side_effect=while_confirming), patch.object(
            self.window.bridge, "start"
        ) as start, patch("spark_assistant.scheduler.local_now", return_value=self.fixture_time + timedelta(minutes=1)):
            self.window._logout()
            start.assert_not_called()
            self.assertEqual(self.window._dispatch_hold_depth, 0)
            self.window._tick()
            start.assert_called_once()

    def test_exit_confirmation_blocks_new_job_even_if_current_job_finishes(self):
        self._due_rule("friend")
        self._ready()

        def while_confirming(*args):
            with patch.object(WorkerBridge, "busy", new_callable=PropertyMock, return_value=False):
                self.window._tick()
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "question", side_effect=while_confirming), patch.object(
            WorkerBridge, "busy", new_callable=PropertyMock, return_value=True
        ), patch.object(self.window.bridge, "start") as start:
            self.window.quit_app()
            start.assert_not_called()
        self.assertFalse(self.window._quitting)
        self.assertEqual(self.window._dispatch_hold_depth, 0)

    def test_rule_dialog_preserves_message_spaces_and_validates_recipient_length(self):
        friend = self._contact("测试好友")
        dialog = RuleDialog([friend], parent=self.window)
        dialog.set_contact(friend)
        dialog.message.setText("  你好  ")
        self.assertEqual(dialog.values()["message"], "  你好  ")
        dialog.set_contact({**friend, "display_name": "长" * 201})
        dialog._validate()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.error.text())
        dialog.deleteLater()

    def test_message_limit_does_not_silently_cut_emoji_or_pasted_text(self):
        friend = self._contact("测试好友")
        dialog = RuleDialog([friend], parent=self.window)
        dialog.set_contact(friend)
        emojis = "🙂" * 500
        dialog.message.setText(emojis)
        self.assertEqual(dialog.message.text(), emojis)
        self.assertEqual(dialog.values()["message"], emojis)
        oversized = "字" * 501
        dialog.message.setText(oversized)
        self.assertEqual(dialog.message.text(), oversized)
        dialog._validate()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.error.text())
        dialog.deleteLater()

    def test_friend_picker_search_keeps_same_name_identities_separate(self):
        self._contact("同名好友", "friend-a")
        self._contact("同名好友", "friend-b")
        picker = FriendPicker(self.store.list_contacts(), self.window)
        try:
            self.assertEqual(picker.table.rowCount(), 2)
            picker.search.setText("friend-b")
            self.assertEqual(picker.table.rowCount(), 1)
            picker.table.selectRow(0)
            picker._accept_friend()
            self.assertEqual(picker.selected_contact["platform_id"], "friend-b")
        finally:
            picker.deleteLater()

    def test_unconfigured_chat_friend_has_actions_but_does_not_create_task(self):
        self._contact("已读取的聊天好友")
        self.window.refresh()
        self.assertEqual(self.store.list_rules(), [])
        self.assertEqual(self.window.rules_table.rowCount(), 1)
        from PySide6.QtWidgets import QPushButton
        actions = self.window.rules_table.cellWidget(0, 6)
        self.assertEqual([button.text() for button in actions.findChildren(QPushButton)], ["填写并发送", "设置定时"])
        item = self.window.rules_table.item(0, 0)
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_rule_dialog_has_visible_picker_and_manual_only_default(self):
        friend = self._contact("好友")
        dialog = RuleDialog([friend], contact=friend, parent=self.window)
        try:
            dialog.message.setText("问候")
            self.assertEqual(dialog.choose_friend_button.text(), "选择好友")
            self.assertTrue(dialog.recipient.isReadOnly())
            self.assertFalse(dialog.values()["enabled"])
            self.assertIsNone(dialog.values()["send_time"])
            self.assertEqual(dialog.values()["contact_id"], friend["id"])
            dialog.enabled.setChecked(True)
            self.assertEqual(dialog.values()["send_time"], "09:00")
        finally:
            dialog.deleteLater()

    def test_old_name_binding_requires_explicit_verified_friend_choice(self):
        friend = self._contact("原备注")
        dialog = RuleDialog([friend], {"id": "legacy", "recipient": "原备注", "message": "问候",
                            "send_time": "09:00", "enabled": True, "needs_review": True}, self.window)
        try:
            with self.assertRaisesRegex(ValueError, "选择好友"):
                dialog.values()
            dialog.set_contact(friend)
            self.assertEqual(dialog.values()["contact_id"], friend["id"])
        finally:
            dialog.deleteLater()

    def test_ready_with_zero_chats_auto_syncs_once_and_send_does_not_resync(self):
        self.window._on_result({"command": "check", "status": "ready", "contacts": []})
        self.assertTrue(self.store.get_setting("account_ready"))
        self.assertTrue(self.window._pending_friend_sync)
        with patch.object(self.window.bridge, "start") as start:
            self.window._tick()
            start.assert_called_once_with("sync_friends")
            self.window._on_result({"command": "sync_friends", "status": "ready", "friends": [],
                                    "coverage": {"status": "complete"},
                                    "account_validated": True,
                                    "account": {"platform_id": "fixture-self", "verification": "verified"}})
            self.window._on_result({"command": "send", "status": "uncertain"})
            self.window._on_result({"command": "check", "status": "ready", "contacts": ["partial chat"]})
            self.window._tick()
            start.assert_called_once()
        self.assertFalse(self.window._pending_friend_sync)
        self.assertEqual(self.store.list_contacts(), [])

    def test_name_only_chat_check_never_replaces_identity_directory(self):
        friend = self._contact("目录好友")
        self.window._on_result({"command": "check", "status": "ready", "contacts": ["最近会话"]})
        self.assertEqual([item["id"] for item in self.store.list_contacts()], [friend["id"]])

    def test_partial_and_unsupported_sync_are_visible_without_claiming_complete(self):
        self._ready()
        for coverage, expected in [("partial", "部分"), ("unsupported", "暂未支持"), ("verification_required", "需要官网验证")]:
            self.window._sync_epoch = self.store.get_account()["account_epoch"]
            self.window._on_result({"command": "sync_friends", "status": "unavailable", "friends": [],
                                    "coverage": {"status": coverage}, "detail": "只读网页来源暂未核验"})
            self.assertIn(expected, self.window.friend_sync_status.text())
            self.assertNotIn("本次会话读取完成", self.window.friend_sync_status.text())

    def test_empty_chat_directory_explains_manual_chat_then_refresh(self):
        self.window.refresh()
        self.assertIn("请先在抖音里手动发一条消息，再回来刷新", self.window.chat_scope_hint.text())
        self.assertIn("本次没有读到", self.window.empty_tasks.text())
        self.assertNotIn("从未聊过", self.window.empty_tasks.text())
        self.assertEqual(self.window.sync_button.text(), "刷新聊天好友")
        self.assertNotIn("互关", self.window.friend_sync_status.text())

    def test_group_or_unverified_loaded_chat_cannot_be_selected(self):
        picker = FriendPicker([{"id": "group", "platform_id": "group-id", "display_name": "一个群聊",
                                "sendable": False, "blocked_reason": "群聊暂不支持"}], self.window)
        try:
            picker.table.selectRow(0)
            picker._accept_friend()
            self.assertIsNone(picker.selected_contact)
            self.assertIn("群聊", picker.hint.text())
        finally:
            picker.deleteLater()

    def test_manual_send_works_with_schedule_off_and_uses_same_day_instance(self):
        friend = self._contact("手动好友")
        with patch("spark_assistant.store.local_now", return_value=self.fixture_time):
            rule_id = self.store.save_rule("手动好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        with patch("spark_assistant.scheduler.local_now", return_value=self.fixture_time), patch(
                "spark_assistant.store.local_now", return_value=self.fixture_time):
            self.window._send_rule(rule_id)
            self.window._send_rule(rule_id)
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["message"], "你好")
        self.assertEqual(runs[0]["rule_id"], rule_id)
        self.assertFalse(self.store.list_rules()[0]["enabled"])

    def test_batch_sends_only_checked_rows_with_own_content(self):
        first = self._contact("第一个", "first")
        second = self._contact("第二个", "second")
        a = self.store.save_rule("第一个", "甲", None, enabled=False, contact_id=first["id"])
        b = self.store.save_rule("第二个", "乙", None, enabled=False, contact_id=second["id"])
        self._ready()
        self.window.refresh()
        self.window._checked_rule_ids = {b}
        self.window._send_selected()
        runs = self.store.list_runs()
        self.assertEqual([(run["rule_id"], run["message"]) for run in runs], [(b, "乙")])
        self.assertNotEqual(a, b)

    def test_global_pause_keeps_manual_requests_queued(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.store.set_setting("paused", True)
        self.window._send_rule(rule_id)
        self.assertIn("暂停", self.window.status_strip.text())
        with patch.object(self.window.bridge, "start") as start:
            self.window._tick()
        start.assert_not_called()
        self.assertEqual(self.store.list_runs()[0]["status"], "pending")

    def test_uncertain_today_can_be_selected_but_requires_explicit_confirmation(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.scheduler.enqueue_manual([rule_id])
        run = self.scheduler.tick()
        self.scheduler.complete(run["id"], "uncertain", submission="possible")
        self.window.refresh()
        from PySide6.QtWidgets import QPushButton
        send = self.window.rules_table.cellWidget(0, 6).findChild(QPushButton, f"send-{rule_id}")
        self.assertTrue(send.isEnabled())
        self.assertEqual(send.text(), "核对后再次发送")
        self.assertTrue(self.window.rules_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as ask:
            self.window._send_rule(rule_id)
        ask.assert_called_once()
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertIn("未提交", self.window.status_strip.text())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), patch.object(
                self.scheduler, "enqueue_manual", wraps=self.scheduler.enqueue_manual) as enqueue:
            self.window._send_rule(rule_id)
        self.assertEqual(enqueue.call_args.kwargs["acknowledge_uncertain_ids"], (run["id"],))
        self.assertTrue(enqueue.call_args.kwargs["action_id"])
        self.assertEqual(len(self.store.list_runs()), 2)
        old = next(item for item in self.store.list_runs() if item["id"] == run["id"])
        self.assertEqual(old["status"], "uncertain")

    def test_known_success_enables_explicit_new_message_without_confirmation(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "第一条", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.scheduler.enqueue_manual([rule_id])
        original = self.scheduler.tick()
        self.scheduler.complete(original["id"], "persisted_in_chat", submission="confirmed")
        self.store.save_rule("好友", "再次主动发送", None, enabled=False, rule_id=rule_id, contact_id=friend["id"])
        self.window.refresh()
        from PySide6.QtWidgets import QPushButton
        button = self.window.rules_table.cellWidget(0, 6).findChild(QPushButton, f"send-{rule_id}")
        self.assertTrue(button.isEnabled())
        self.assertEqual(button.text(), "再次发送")
        with patch.object(QMessageBox, "question") as ask:
            self.window._send_rule(rule_id)
        ask.assert_not_called()
        self.assertEqual(len(self.store.list_runs()), 2)
        repeated = next(run for run in self.store.list_runs() if run["id"] != original["id"])
        self.assertEqual(repeated["message"], "再次主动发送")
        self.assertFalse(repeated["is_daily"])

    def test_pending_manual_disables_duplicate_click_until_finished(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.window._send_rule(rule_id)
        from PySide6.QtWidgets import QPushButton
        button = self.window.rules_table.cellWidget(0, 6).findChild(QPushButton, f"send-{rule_id}")
        self.assertFalse(button.isEnabled())
        self.assertIn("排队", button.toolTip())
        with patch.object(self.scheduler, "enqueue_manual") as enqueue:
            self.window._send_rule(rule_id)
        enqueue.assert_not_called()
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_batch_uncertain_confirmation_is_once_and_acknowledges_exact_results(self):
        first = self._contact("甲", "first")
        second = self._contact("乙", "second")
        ids = [self.store.save_rule("甲", "消息甲", None, enabled=False, contact_id=first["id"]),
               self.store.save_rule("乙", "消息乙", None, enabled=False, contact_id=second["id"])]
        self._ready()
        self.scheduler.enqueue_manual(ids)
        uncertain_ids = []
        for _ in ids:
            run = self.scheduler.tick()
            uncertain_ids.append(run["id"])
            self.scheduler.complete(run["id"], "uncertain", submission="possible")
        self.window.refresh()
        self.window._checked_rule_ids = set(ids)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as ask, patch.object(
                self.scheduler, "enqueue_manual", wraps=self.scheduler.enqueue_manual) as enqueue:
            self.window._send_selected()
        ask.assert_called_once()
        self.assertEqual(set(enqueue.call_args.kwargs["acknowledge_uncertain_ids"]), set(uncertain_ids))
        self.assertEqual(len(self.store.list_runs()), 4)

    def test_background_operation_labels_do_not_offer_a_new_browser_window(self):
        self._ready()
        with patch.object(self.window.bridge, "start"):
            self.window._start_operation("sync_friends")
        self.assertIn("后台", self.window.status_strip.text())
        self.assertNotIn("打开浏览器", self.window.status_strip.text())

    def test_resume_batches_requires_explicit_button_and_keeps_global_pause(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.scheduler.enqueue_manual([rule_id])
        self.scheduler.recover_interrupted()
        self.store.set_setting("paused", True)
        self.window.refresh()
        self.assertFalse(self.window.batch_resume_button.isHidden())
        self.window._resume_batches()
        self.assertTrue(self.store.get_setting("paused"))
        self.assertEqual(self.scheduler.list_resumable_batches(), [])

    def test_different_account_check_invalidates_old_queue_before_dispatch(self):
        friend = self._contact("旧账号好友")
        rule_id = self.store.save_rule("旧账号好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.scheduler.enqueue_manual([rule_id])
        epoch = self.store.get_account()["account_epoch"]
        self.window._on_result({"command": "check", "status": "ready", "account_validated": True,
                               "account": {"platform_id": "different-account", "verification": "verified"}})
        self.assertGreater(self.store.get_account()["account_epoch"], epoch)
        self.assertEqual(self.store.list_runs()[0]["status"], "cancelled")
        self.assertTrue(self.store.get_setting("paused"))
        self.assertTrue(self.window._pending_friend_sync)
        with patch.object(self.window.bridge, "start") as start:
            self.window._tick()
        start.assert_called_once_with("sync_friends")
        self.assertFalse(self.store.list_rules()[0]["sendable"])

    def test_worker_account_mismatch_invalidates_queued_items(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.scheduler.enqueue_manual([rule_id])
        epoch = self.store.get_account()["account_epoch"]
        self.window._on_result({"command": "send", "status": "failed", "scope": "account",
                               "code": "account_mismatch", "submission": "not_submitted", "account_validated": False})
        self.assertGreater(self.store.get_account()["account_epoch"], epoch)
        self.assertEqual(self.store.list_runs()[0]["status"], "cancelled")
        self.assertFalse(self.store.get_setting("account_ready"))

    def test_explicit_unsubmitted_failure_retry_reuses_run_id(self):
        friend = self._contact("好友")
        rule_id = self.store.save_rule("好友", "你好", None, enabled=False, contact_id=friend["id"])
        self._ready()
        self.scheduler.enqueue_manual([rule_id])
        run = self.scheduler.tick()
        self.scheduler.complete(run["id"], "failed", submission="not_submitted", code="recipient_not_found")
        self.store.save_rule("好友", "以后使用的新内容", None, enabled=False, rule_id=rule_id, contact_id=friend["id"])
        self.window.refresh()
        from PySide6.QtWidgets import QPushButton
        button = self.window.rules_table.cellWidget(0, 6).findChild(QPushButton, f"send-{rule_id}")
        self.assertEqual(button.text(), "重试原消息")
        self.assertIn("你好", button.toolTip())
        self.window._send_rule(rule_id)
        self.assertEqual(len(self.store.list_runs()), 1)
        self.assertEqual(self.store.list_runs()[0]["id"], run["id"])
        self.assertEqual(self.store.list_runs()[0]["status"], "pending")
        self.assertEqual(self.store.list_runs()[0]["message"], "你好")


class WorkerBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = SimpleNamespace(data_dir=root, resource_dir=root)
        self.bridge = WorkerBridge(self.paths)

    def tearDown(self):
        self.bridge.shutdown()
        self.bridge.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def _run_fixture(self, code, command="check"):
        (self.paths.resource_dir / "desktop_main.py").write_text(code, encoding="utf-8")
        events = []
        loop = QEventLoop()
        self.bridge.result.connect(events.append)
        self.bridge.result.connect(loop.quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(5000)
        self.bridge.start(command)
        loop.exec()
        timer.stop()
        self.assertTrue(events, "fixture worker did not complete")
        self.assertFalse(self.bridge.busy)
        return events[-1]

    def test_protocol_reads_utf8_and_waits_for_exit(self):
        result = self._run_fixture('''import json,sys
request=json.loads(sys.stdin.buffer.readline().decode("utf-8"))
print(json.dumps({"event":"progress","message":"正在核验"},ensure_ascii=False),flush=True)
print(json.dumps({"event":"result","status":"ready","detail":"已连接","contacts":["测试好友"]},ensure_ascii=False),flush=True)
''')
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["contacts"], ["测试好友"])
        self.assertEqual(result["command"], "check")

    def test_send_crash_is_uncertain_and_not_retried(self):
        result = self._run_fixture("import sys\nsys.stdin.buffer.readline()\nsys.exit(9)\n", command="send")
        self.assertEqual(result["status"], "uncertain")

    def test_non_send_missing_result_is_failed(self):
        result = self._run_fixture('import sys\nsys.stdin.buffer.readline()\nprint("not JSON")\n')
        self.assertEqual(result["status"], "failed")

    def test_job_assignment_failure_never_writes_send_request(self):
        marker = self.paths.data_dir / "request-received.txt"
        code = f'''import pathlib,sys
request=sys.stdin.buffer.readline()
if request: pathlib.Path({str(marker)!r}).write_text("received")
'''
        with patch("spark_assistant.bridge.ProcessTreeGuard", side_effect=OSError("fixture denied")):
            result = self._run_fixture(code, command="send")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(marker.exists())

    def test_malformed_events_ignored_before_final(self):
        result = self._run_fixture('''import sys
sys.stdin.buffer.readline()
print("[]")
print("garbage")
print('{"event":"result","status":"ready","detail":"fixture"}')
''')
        self.assertEqual(result["status"], "ready")

    def test_friend_sync_has_six_minute_worker_timeout(self):
        observed = []
        original = self.bridge._timeout.start

        def record_timeout(interval):
            observed.append(interval)
            original(interval)

        with patch.object(self.bridge._timeout, "start", side_effect=record_timeout):
            result = self._run_fixture('''import sys
sys.stdin.buffer.readline()
print('{"event":"result","status":"ready","friends":[],"coverage":{"status":"complete"}}')
''', command="sync_friends")
        self.assertEqual(result["command"], "sync_friends")
        self.assertEqual(observed, [360000])


if __name__ == "__main__":
    unittest.main()

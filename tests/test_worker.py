"""Offline desktop-worker tests: no real Douyin account or network access."""
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from douyin_demo import DemoError, SendLedger, read_selectors
from spark_assistant import worker
from spark_assistant.paths import configure_browser


class FakeLocator:
    def __init__(self, elements=()):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class FakeElement:
    def __init__(self, text="", visible=True, children=()):
        self.text = text
        self.visible = visible
        self.children = children

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text

    def locator(self, _selector):
        return FakeLocator(self.children)


class FakePage:
    def __init__(self, names=(), login=False, shell=True, title="抖音"):
        self.names = names
        self.login = login
        self.shell = shell
        self.page_title = title

    def title(self):
        return self.page_title

    def is_closed(self):
        return False

    def locator(self, _selector):
        if _selector == "iframe[src]":
            return FakeLocator()
        return FakeLocator([FakeElement(children=[FakeElement(name) for name in self.names])]
                           if self.shell else [])

    def get_by_role(self, *_args, **_kwargs):
        return FakeLocator([FakeElement("登录")] if self.login else [])

    def get_by_text(self, *_args, **_kwargs):
        return FakeLocator()

    def wait_for_timeout(self, _milliseconds):
        pass


SELECTORS = {"conversation_list": ".list", "conversation_name": ".name"}
IDENTITY = {"platform_id": "fixture_peer", "profile_url": "https://www.douyin.com/user/fixture_peer",
            "conversation_id": "0:1:fixture:conversation", "account_id": "fixture_account", "account_epoch": 1}
VERIFIED_ACCOUNT = {"platform_id": IDENTITY["account_id"], "verification": "verified"}


class AccountReadinessTests(unittest.TestCase):
    def login_context(self, *pages):
        context = MagicMock()
        context.pages = list(pages)
        return context

    def test_closing_login_window_requests_check_without_claiming_ready(self):
        page = MagicMock()
        page.is_closed.return_value = True
        outcome = worker.wait_for_login_window(self.login_context(page))
        self.assertEqual("login_closed", outcome["status"])
        self.assertFalse(outcome["account_validated"])
        page.goto.assert_not_called()

    def test_login_window_closing_during_wait_is_not_a_browser_failure(self):
        page = MagicMock()
        page.is_closed.side_effect = [False, True, True]
        page.wait_for_timeout.side_effect = PlaywrightError("Target closed")
        self.assertEqual("login_closed", worker.wait_for_login_window(self.login_context(page))["status"])

    def test_login_window_timeout_does_not_claim_ready(self):
        page = MagicMock()
        page.is_closed.return_value = False
        outcome = worker.wait_for_login_window(self.login_context(page), timeout=0)
        self.assertEqual("login_required", outcome["status"])
        self.assertEqual("account", outcome["scope"])
        self.assertFalse(outcome["account_validated"])

    def test_closing_initial_tab_preserves_a_live_login_popup(self):
        initial, popup = MagicMock(), MagicMock()
        initial.is_closed.return_value = True
        popup.is_closed.return_value = False
        context = self.login_context(initial, popup)

        def close_popup(_delay):
            popup.is_closed.return_value = True

        popup.wait_for_timeout.side_effect = close_popup
        outcome = worker.wait_for_login_window(context)
        popup.wait_for_timeout.assert_called_once()
        initial.wait_for_timeout.assert_not_called()
        self.assertEqual("login_closed", outcome["status"])
        self.assertFalse(outcome["account_validated"])

    def test_popup_opened_during_wait_is_observed_after_initial_tab_closes(self):
        initial, popup = MagicMock(), MagicMock()
        initial.is_closed.return_value = False
        popup.is_closed.return_value = False
        context = self.login_context(initial)

        def open_popup_then_close_initial(_delay):
            context.pages.append(popup)
            initial.is_closed.return_value = True
            raise PlaywrightError("initial tab closed")

        def close_popup(_delay):
            popup.is_closed.return_value = True

        initial.wait_for_timeout.side_effect = open_popup_then_close_initial
        popup.wait_for_timeout.side_effect = close_popup
        self.assertEqual("login_closed", worker.wait_for_login_window(context)["status"])
        popup.wait_for_timeout.assert_called_once()

    def test_closing_popup_first_keeps_initial_tab_alive(self):
        initial, popup = MagicMock(), MagicMock()
        initial.is_closed.return_value = False
        popup.is_closed.return_value = True
        context = self.login_context(initial, popup)
        outcome = worker.wait_for_login_window(context, timeout=0)
        self.assertEqual("login_required", outcome["status"])
        self.assertNotEqual("login_closed", outcome["status"])

    def test_whole_context_close_during_wait_finishes_without_account_claim(self):
        page = MagicMock()
        page.is_closed.return_value = False
        context = self.login_context(page)

        def close_context(_delay):
            context.on.call_args.args[1](context)
            raise PlaywrightError("Browser context closed")

        page.wait_for_timeout.side_effect = close_context
        outcome = worker.wait_for_login_window(context)
        self.assertEqual("login_closed", outcome["status"])
        self.assertFalse(outcome["account_validated"])
        context.remove_listener.assert_called_once()

    def test_ready_without_a_requested_recipient(self):
        outcome = worker.wait_for_account(FakePage(["另一个朋友"]), SELECTORS,
                                          login=False, timeout=0)
        self.assertEqual("ready", outcome["status"])
        self.assertEqual(["另一个朋友"], outcome["contacts"])
        self.assertTrue(outcome["account_validated"])

    def test_contacts_preserve_duplicate_remarks(self):
        self.assertEqual(["955", "955"], worker.visible_contacts(FakePage(["955", "955"]), SELECTORS))

    def test_explicit_login_prompt_is_login_required(self):
        outcome = worker.wait_for_account(FakePage(login=True), SELECTORS,
                                          login=False, timeout=0)
        self.assertEqual("login_required", outcome["status"])
        self.assertEqual("account", outcome["scope"])
        self.assertEqual("not_submitted", outcome["submission"])

    def test_transient_login_prompt_waits_for_loaded_account(self):
        page = FakePage(login=True)

        def finish_loading(_milliseconds):
            page.names = ["已加载的朋友"]
            page.login = False

        page.wait_for_timeout = MagicMock(side_effect=finish_loading)
        # The page progresses when polled; no real clock or browser wait occurs.
        with patch.object(worker.time, "monotonic", return_value=100):
            outcome = worker.wait_for_account(page, SELECTORS, login=False, timeout=45)
        self.assertEqual("ready", outcome["status"])
        self.assertEqual(["已加载的朋友"], outcome["contacts"])
        page.wait_for_timeout.assert_called_once()

    def test_empty_chat_does_not_claim_logged_in_or_logged_out(self):
        outcome = worker.wait_for_account(FakePage(), SELECTORS, login=False, timeout=0)
        self.assertEqual("unavailable", outcome["status"])

    def test_observed_verification_page_requires_manual_login_even_with_chat_shell(self):
        outcome = worker.wait_for_account(FakePage(["955"], title="验证码中间页"), SELECTORS,
                                          login=False, timeout=0)
        self.assertEqual("login_required", outcome["status"])
        self.assertEqual("verification_required", outcome["code"])
        self.assertIn("手动打开", outcome["detail"])
        self.assertFalse(outcome["account_validated"])

    def test_verified_account_allows_empty_chat_shell_but_not_empty_document(self):
        ready = worker.wait_for_account(FakePage(), SELECTORS, login=False, timeout=0, allow_empty=True)
        self.assertEqual("ready", ready["status"])
        self.assertEqual([], ready["contacts"])
        blank = worker.wait_for_account(FakePage(shell=False), SELECTORS,
                                        login=False, timeout=0, allow_empty=True)
        self.assertEqual("unavailable", blank["status"])

    def test_closed_browser_is_unavailable(self):
        page = FakePage()
        page.is_closed = lambda: True
        self.assertEqual("unavailable", worker.wait_for_account(page, SELECTORS,
                                                                login=True, timeout=0)["status"])


class WorkerOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.request = {"command": "send", "data_dir": str(self.root),
                        "recipient": "955", "message": "你好", "request_id": "task-1:2026-10-16",
                        "scheduled_date": worker.beijing_today(), **IDENTITY}
        account_patch = patch.object(worker.ChatDirectory, "read_account", return_value=VERIFIED_ACCOUNT)
        account_patch.start()
        self.addCleanup(account_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def mock_browser(self):
        playwright_context = MagicMock()
        context = MagicMock()
        context.pages = [MagicMock()]
        playwright_context.__enter__.return_value.chromium.launch_persistent_context.return_value = context
        return playwright_context, context

    def test_pre_submit_error_is_failed_and_not_recorded_as_sent(self):
        playwright_context, context = self.mock_browser()
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            ui.return_value.send_once.side_effect = DemoError("备注存在重名，未发送。")
            outcome = worker.execute_request(self.request)
        self.assertEqual("failed", outcome["status"])
        self.assertIn("重名", outcome["detail"])
        self.assertEqual("task", outcome["scope"])
        self.assertEqual("not_submitted", outcome["submission"])
        self.assertTrue(outcome["account_validated"])
        context.close.assert_called_once()
        ledger = SendLedger(self.root / "accounts/default/sends.sqlite3")
        try:
            self.assertIsNone(ledger.get(self.request["request_id"]))
        finally:
            ledger.close()

    def test_exception_after_ledger_begin_is_uncertain_and_never_retried(self):
        playwright_context, _context = self.mock_browser()

        def submit_then_fail(recipient, message, request_id, ledger):
            ledger.begin(request_id, recipient, message)
            raise PlaywrightError("fixture disconnect after keypress")

        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            ui.return_value.send_once.side_effect = submit_then_fail
            self.assertEqual("uncertain", worker.execute_request(self.request)["status"])
        with patch.object(worker, "sync_playwright") as launch:
            self.assertEqual("uncertain", worker.execute_request(self.request)["status"])
            launch.assert_not_called()

    def test_post_submit_storage_read_and_cleanup_failures_stay_uncertain_in_protocol(self):
        playwright_context, _context = self.mock_browser()

        def submit_then_lose_database(recipient, message, request_id, ledger):
            ledger.begin(request_id, recipient, message)
            ledger.db.close()
            ledger.close = MagicMock(side_effect=sqlite3.OperationalError("cleanup also failed"))
            raise PlaywrightError("fixture disconnect after Enter")

        output = io.StringIO()
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui, \
             patch.object(worker.sys, "stdin", io.StringIO(json.dumps(self.request) + "\n")), \
             patch.object(worker.sys, "stdout", output):
            ui.return_value.send_once.side_effect = submit_then_lose_database
            self.assertEqual(1, worker.run_worker())
        results = [json.loads(line) for line in output.getvalue().splitlines()
                   if json.loads(line).get("event") == "result"]
        self.assertEqual(1, len(results))
        self.assertEqual("uncertain", results[0]["status"])
        self.assertEqual("possible", results[0]["submission"])
        self.assertTrue(results[0]["account_validated"])
        ledger = SendLedger(self.root / "accounts/default/sends.sqlite3")
        try:
            self.assertEqual("uncertain", ledger.get(self.request["request_id"]))
        finally:
            ledger.close()

    def test_post_submit_cancellation_is_uncertain(self):
        ledger = SendLedger(self.root / "cancel.sqlite3")
        try:
            ledger.begin("cancelled-send", "955", "fixture")
            outcome = worker._error_result(
                DemoError("操作已停止。", code="operation_cancelled", scope="operation"),
                ledger, "cancelled-send", account_validated=True)
            self.assertEqual("uncertain", outcome["status"])
            self.assertEqual("possible", outcome["submission"])
        finally:
            ledger.close()

    def test_unreadable_existing_ledger_never_claims_not_submitted(self):
        ledger = MagicMock()
        ledger.submission_started = False
        ledger.get.side_effect = sqlite3.OperationalError("cannot read")
        outcome = worker._error_result(PlaywrightError("before new submission"), ledger, "may-exist")
        self.assertEqual("uncertain", outcome["status"])
        self.assertEqual("possible", outcome["submission"])

    def test_safe_unverified_message_explanation_is_preserved(self):
        explanation = "网页未提供可核对的消息标识，请在手机检查；不会自动重发。"
        outcome = worker._error_result(
            DemoError(explanation, code="message_unverified", submission="possible"),
            None, "unknown", account_validated=True)
        self.assertEqual("uncertain", outcome["status"])
        self.assertEqual("message_unverified", outcome["code"])
        self.assertEqual(explanation, outcome["detail"])
        self.assertTrue(outcome["account_validated"])

    def test_raw_browser_diagnostic_is_not_exposed(self):
        outcome = worker._error_result(PlaywrightError("sensitive page text fixture"), None, None)
        self.assertNotIn("sensitive", outcome["detail"])
        self.assertEqual("account", outcome["scope"])
        self.assertFalse(outcome["account_validated"])

    def test_known_submission_reason_and_safe_stage_survive_without_raw_diagnostics(self):
        error = DemoError("sensitive page text and message fixture", code="editor_text_changed", submission="possible")
        error.stage = "validating_submission"
        error.cause_type = "DemoError"
        outcome = worker._error_result(error, None, "fixture", account_validated=True)
        self.assertEqual("uncertain", outcome["status"])
        self.assertEqual("possible", outcome["submission"])
        self.assertEqual("editor_text_changed", outcome["code"])
        self.assertEqual("validating_submission", outcome["stage"])
        self.assertEqual("DemoError", outcome["cause_type"])
        self.assertIn("正文发生变化", outcome["detail"])
        self.assertNotIn("sensitive", json.dumps(outcome))

    def test_unknown_exception_metadata_cannot_expose_page_or_message_data(self):
        error = PlaywrightError("private page fixture")
        error.stage = "private-stage-fixture"
        error.cause_type = "private-type-fixture"
        error.submission = "possible"
        outcome = worker._error_result(error, None, "fixture")
        self.assertEqual("uncertain", outcome["status"])
        self.assertNotIn("stage", outcome)
        self.assertNotIn("cause_type", outcome)
        self.assertNotIn("private", json.dumps(outcome))

    def test_adapter_challenge_is_manual_login_before_submit_and_uncertain_after(self):
        error = DemoError("fixture", code="verification_required", scope="account")
        before = worker._error_result(error, None, None)
        self.assertEqual("login_required", before["status"])
        self.assertIn("手动打开", before["detail"])
        error.submission = "possible"
        after = worker._error_result(error, None, None, account_validated=True)
        self.assertEqual("uncertain", after["status"])
        self.assertEqual("verification_required", after["code"])
        self.assertEqual("possible", after["submission"])
        self.assertFalse(after["account_validated"])

    def test_background_commands_remain_headless_and_challenge_never_launches_fallback(self):
        for command in ("check", "contacts", "sync_friends", "send"):
            with self.subTest(command=command):
                playwright_context, context = self.mock_browser()
                context.pages[0].title.return_value = "验证码中间页"
                with patch.object(worker, "sync_playwright", return_value=playwright_context), \
                     patch.object(worker.ChatDirectory, "identity_ui") as ui:
                    outcome = worker.execute_request({**self.request, "command": command})
                self.assertEqual("login_required", outcome["status"])
                self.assertEqual("verification_required", outcome["code"])
                launch = playwright_context.__enter__.return_value.chromium.launch_persistent_context
                launch.assert_called_once()
                self.assertTrue(launch.call_args.kwargs["headless"])
                ui.assert_not_called()

    def test_browser_exception_before_submission_is_unavailable(self):
        playwright_context, _context = self.mock_browser()
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            ui.return_value.send_once.side_effect = PlaywrightError("fixture: selector timeout")
            outcome = worker.execute_request(self.request)
        self.assertEqual("unavailable", outcome["status"])
        self.assertEqual("account", outcome["scope"])
        self.assertEqual("not_submitted", outcome["submission"])
        self.assertFalse(outcome["account_validated"])

    def test_old_day_is_skipped_without_launching_browser(self):
        with patch.object(worker, "sync_playwright") as launch:
            outcome = worker.execute_request({**self.request, "scheduled_date": "2020-01-01"})
            self.assertEqual("missed", outcome["status"])
            launch.assert_not_called()

    def test_date_rechecked_at_ledger_begin_not_just_browser_launch(self):
        ledger = worker.DailySendLedger(self.root / "date.sqlite3", "2026-10-16")
        try:
            with patch.object(worker, "beijing_today", return_value="2026-10-17"):
                with self.assertRaises(worker.MissedDayError):
                    ledger.begin("cross-midnight", "955", "hello")
            self.assertIsNone(ledger.get("cross-midnight"))
        finally:
            ledger.close()

    def test_persisted_request_is_returned_without_sending_again(self):
        account = self.root / "accounts/default"
        account.mkdir(parents=True)
        ledger = SendLedger(account / "sends.sqlite3")
        ledger.identity = worker.send_identity(self.request)
        ledger.begin(self.request["request_id"], "955", "你好")
        ledger.finish(self.request["request_id"], "persisted_in_chat")
        ledger.close()
        with patch.object(worker, "sync_playwright") as launch:
            self.assertEqual("persisted_in_chat", worker.execute_request(self.request)["status"])
            launch.assert_not_called()

    def test_existing_id_with_different_payload_is_not_reported_as_success(self):
        account = self.root / "accounts/default"
        account.mkdir(parents=True)
        ledger = SendLedger(account / "sends.sqlite3")
        ledger.begin(self.request["request_id"], self.request["recipient"], "older payload")
        ledger.finish(self.request["request_id"], "persisted_in_chat")
        ledger.close()
        with patch.object(worker, "sync_playwright") as launch:
            outcome = worker.execute_request(self.request)
            launch.assert_not_called()
        self.assertEqual("failed", outcome["status"])
        self.assertEqual("idempotency_conflict", outcome["code"])
        self.assertEqual("not_submitted", outcome["submission"])

    def test_failed_payload_lookup_cannot_reuse_a_status_only_success(self):
        account = self.root / "accounts/default"
        account.mkdir(parents=True)
        ledger = SendLedger(account / "sends.sqlite3")
        ledger.begin(self.request["request_id"], self.request["recipient"], "older payload")
        ledger.finish(self.request["request_id"], "persisted_in_chat")
        ledger.close()
        with patch.object(worker.DailySendLedger, "lookup", side_effect=sqlite3.OperationalError("fixture read failure")), \
             patch.object(worker, "sync_playwright") as launch:
            outcome = worker.execute_request(self.request)
            launch.assert_not_called()
        self.assertEqual("uncertain", outcome["status"])
        self.assertEqual("possible", outcome["submission"])

    def test_midnight_error_does_not_clear_replacement_editor_protected_by_core(self):
        playwright_context, _context = self.mock_browser()
        header = MagicMock()
        header.is_visible.return_value = True
        header.inner_text.return_value = self.request["recipient"]
        replacement = MagicMock()
        replacement.is_visible.return_value = True
        replacement.evaluate.return_value = self.request["message"]
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            # The core has already refused rollback: this is a replacement
            # editor containing identical text, not its original owned editor.
            ui.return_value.send_once.side_effect = worker.MissedDayError(
                "本次未提交，但新输入框不属于本次操作，需人工处理。", code="draft_cleanup_required")
            ui.return_value.required.side_effect = lambda key: key
            ui.return_value.page.locator.side_effect = lambda selector: FakeLocator(
                [header if selector == "conversation_header" else replacement])
            outcome = worker.execute_request(self.request)
        self.assertEqual("missed", outcome["status"])
        self.assertEqual("not_submitted", outcome["submission"])
        replacement.fill.assert_not_called()

    def test_login_uses_visible_managed_profile_and_full_chromium_by_default(self):
        playwright_context, context = self.mock_browser()
        with patch.dict("os.environ", {"SPARK_BROWSER_CHANNEL": ""}), \
             patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_login_window", return_value={"status": "login_closed"}) as wait_login, \
             patch.object(worker, "wait_for_account") as check:
            outcome = worker.execute_request({"command": "login", "data_dir": str(self.root)})
        self.assertEqual("login_closed", outcome["status"])
        context.pages[0].goto.assert_called_once_with(worker.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        check.assert_not_called()
        launch = playwright_context.__enter__.return_value.chromium.launch_persistent_context
        self.assertFalse(launch.call_args.kwargs["headless"])
        self.assertEqual("chromium", launch.call_args.kwargs["channel"])
        self.assertEqual(str(self.root / "profiles/default"), launch.call_args.args[0])
        self.assertTrue((self.root / "profiles/default" / worker.PROFILE_MARKER).is_file())
        context.close.assert_called_once()
        wait_login.assert_called_once_with(context)

    def test_logged_out_read_never_calls_send(self):
        playwright_context, _context = self.mock_browser()
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "login_required", "detail": "请登录"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            self.assertEqual("login_required", worker.execute_request(self.request)["status"])
            ui.assert_not_called()

    def test_logout_only_deletes_marked_profile_and_preserves_ledger(self):
        profile = self.root / "profiles/default"
        profile.mkdir(parents=True)
        (profile / worker.PROFILE_MARKER).touch()
        (profile / "fixture-session").write_text("fixture")
        account = self.root / "accounts/default"
        account.mkdir(parents=True)
        ledger = SendLedger(account / "sends.sqlite3")
        ledger.close()
        outcome = worker.execute_request({"command": "logout", "data_dir": str(self.root)})
        self.assertEqual("logged_out", outcome["status"])
        self.assertFalse(profile.exists())
        self.assertTrue((account / "sends.sqlite3").is_file())

    def test_logout_refuses_unmarked_profile(self):
        profile = self.root / "profiles/default"
        profile.mkdir(parents=True)
        (profile / "keep").touch()
        with self.assertRaises(DemoError):
            worker.clear_managed_profile(self.root)
        self.assertTrue((profile / "keep").exists())

    def test_path_traversal_and_redirected_profile_are_rejected(self):
        with self.assertRaises(DemoError):
            worker.managed_path(self.root, "..", "outside")
        profile = self.root / "profiles/default"
        with patch.object(Path, "is_symlink", lambda path: path == profile):
            with self.assertRaises(DemoError):
                worker.clear_managed_profile(self.root)

    def test_invalid_message_and_request_id_stop_before_browser(self):
        for changes in ({"message": "hi\nthere"}, {"message": " "}, {"request_id": "../bad"},
                        {"recipient": ""}, {"recipient": "x" * 201}, {"data_dir": "relative"}, {"scheduled_date": "2026-99-01"},
                        {"scheduled_date": None}):
            with self.subTest(changes=changes), patch.object(worker, "sync_playwright") as launch:
                with self.assertRaises(DemoError):
                    worker.execute_request({**self.request, **changes})
                launch.assert_not_called()

    def test_recipient_is_normalized_without_changing_message(self):
        clean = worker.validate_request({**self.request, "recipient": " 955 ", "message": " hello "})
        self.assertEqual("955", clean["recipient"])
        self.assertEqual(" hello ", clean["message"])

    def test_name_only_and_malformed_identity_requests_stop_before_browser(self):
        changes = [{key: None} for key in IDENTITY]
        changes += [{"platform_id": "self"}, {"account_id": "self"},
                    {"account_epoch": True}, {"account_epoch": 0},
                    {"conversation_id": "../other"},
                    {"profile_url": "https://example.com/user/fixture_peer"},
                    {"profile_url": "https://www.douyin.com/user/another_peer"},
                    {"account_platform_id": "another_account"}]
        for change in changes:
            with self.subTest(change=change), patch.object(worker, "sync_playwright") as launch:
                with self.assertRaises(DemoError):
                    worker.execute_request({**self.request, **change})
                launch.assert_not_called()

    def test_account_platform_alias_normalizes_to_stable_ledger_binding(self):
        request = {**self.request, "account_platform_id": IDENTITY["account_id"]}
        del request["account_id"]
        clean = worker.validate_request(request)
        self.assertEqual(IDENTITY["account_id"], clean["account_id"])
        self.assertEqual(worker.send_identity(self.request), worker.send_identity(clean))

    def test_changed_account_stops_before_selecting_or_filling_a_chat(self):
        playwright_context, _context = self.mock_browser()
        actual = {"platform_id": "different_account", "verification": "verified"}
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "read_account", return_value=actual), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            outcome = worker.execute_request(self.request)
        self.assertEqual("account_identity_mismatch", outcome["code"])
        self.assertEqual("account", outcome["scope"])
        self.assertFalse(outcome["account_validated"])
        self.assertEqual("not_submitted", outcome["submission"])
        self.assertEqual(actual, outcome["account"])
        ui.assert_not_called()

    def test_unknown_account_stops_even_with_a_visible_chat_list(self):
        playwright_context, _context = self.mock_browser()
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "read_account", return_value={"verification": "unknown"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            outcome = worker.execute_request(self.request)
        self.assertEqual("account_identity_unverified", outcome["code"])
        self.assertFalse(outcome["account_validated"])
        ui.assert_not_called()

    def test_check_returns_verified_account_and_headless_browser(self):
        playwright_context, _context = self.mock_browser()
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}):
            outcome = worker.execute_request({"command": "check", "data_dir": str(self.root)})
        self.assertEqual(VERIFIED_ACCOUNT, outcome["account"])
        self.assertTrue(outcome["account_validated"])
        launch = playwright_context.__enter__.return_value.chromium.launch_persistent_context
        self.assertTrue(launch.call_args.kwargs["headless"])

    def test_sync_returns_empty_partial_directory_without_claiming_no_history(self):
        playwright_context, _context = self.mock_browser()
        snapshot = {"account": VERIFIED_ACCOUNT, "friends": [],
                    "coverage": {"status": "partial", "directory_kind": "existing_chats",
                                 "reason_code": "load_incomplete", "observed_count": 0}}
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker.ChatDirectory, "sync", return_value=snapshot) as sync, \
             patch.object(worker, "wait_for_account") as old_gate, \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            outcome = worker.execute_request({"command": "sync_friends", "data_dir": str(self.root)})
        self.assertEqual(snapshot["coverage"], outcome["coverage"])
        self.assertEqual([], outcome["friends"])
        self.assertEqual(VERIFIED_ACCOUNT, outcome["account"])
        self.assertIn("尚未加载", outcome["detail"])
        sync.assert_called_once()
        old_gate.assert_not_called()
        ui.assert_not_called()

    def test_sync_never_promotes_unknown_account_to_ready(self):
        playwright_context, _context = self.mock_browser()
        snapshot = {"account": {"verification": "unknown"}, "friends": [],
                    "coverage": {"status": "verification_required", "directory_kind": "existing_chats"}}
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker.ChatDirectory, "sync", return_value=snapshot):
            outcome = worker.execute_request({"command": "sync_friends", "data_dir": str(self.root)})
        self.assertEqual("unavailable", outcome["status"])
        self.assertEqual("account_identity_unverified", outcome["code"])
        self.assertFalse(outcome["account_validated"])
        self.assertEqual(snapshot["coverage"], outcome["coverage"])

    def test_sync_failure_preserves_coverage_and_verified_account_without_claiming_complete(self):
        playwright_context, _context = self.mock_browser()
        snapshot = {"account": VERIFIED_ACCOUNT, "friends": [],
                    "coverage": {"status": "failed", "reason_code": "chat_ui_unavailable",
                                 "directory_kind": "existing_chats"}}
        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker.ChatDirectory, "sync", return_value=snapshot):
            outcome = worker.execute_request({"command": "sync_friends", "data_dir": str(self.root)})
        self.assertEqual("unavailable", outcome["status"])
        self.assertEqual("chat_ui_unavailable", outcome["code"])
        self.assertTrue(outcome["account_validated"])
        self.assertEqual(snapshot["coverage"], outcome["coverage"])
        self.assertNotIn("读取完成", outcome["detail"])

    def test_sync_partial_limits_and_account_switch_have_explicit_explanations(self):
        for reason, phrase in (("sync_time_limit", "时间上限"), ("scan_interrupted", "被中断"),
                               ("scroll_limit_reached", "滚动读取达到上限"),
                               ("account_identity_mismatch", "账号发生变化")):
            with self.subTest(reason=reason):
                playwright_context, _context = self.mock_browser()
                changed = reason == "account_identity_mismatch"
                snapshot = {"account": VERIFIED_ACCOUNT, "friends": [] if changed else [{"platform_id": "fixture_peer"}],
                            "coverage": {"status": "failed" if changed else "partial", "reason_code": reason,
                                         "directory_kind": "existing_chats"}}
                with patch.object(worker, "sync_playwright", return_value=playwright_context), \
                     patch.object(worker.ChatDirectory, "sync", return_value=snapshot):
                    outcome = worker.execute_request({"command": "sync_friends", "data_dir": str(self.root)})
                self.assertEqual(reason, outcome["code"])
                self.assertIn(phrase, outcome["detail"])
                self.assertIn("重新读取", outcome["detail"])
                self.assertNotIn("读取完成", outcome["detail"])
                if not changed:
                    self.assertIn("已保留读到的 1 条", outcome["detail"])
                    self.assertEqual("ready", outcome["status"])
                else:
                    self.assertEqual("unavailable", outcome["status"])

    def test_identity_reaches_adapter_and_ledger_before_submission(self):
        playwright_context, _context = self.mock_browser()
        captured = {}

        def capture(_recipient, _message, _request_id, ledger):
            captured.update(ledger.identity)
            return {"status": "uncertain"}

        with patch.object(worker, "sync_playwright", return_value=playwright_context), \
             patch.object(worker, "wait_for_account", return_value={"status": "ready"}), \
             patch.object(worker.ChatDirectory, "identity_ui") as ui:
            ui.return_value.send_once.side_effect = capture
            worker.execute_request(self.request)
        self.assertEqual(worker.send_identity(self.request), captured)
        routed = ui.call_args.args[0]
        for field in IDENTITY:
            self.assertEqual(self.request[field], routed[field])

    def test_same_request_id_cannot_be_rebound_to_new_recipient_or_account(self):
        account = self.root / "accounts/default"
        account.mkdir(parents=True)
        ledger = SendLedger(account / "sends.sqlite3")
        ledger.identity = worker.send_identity(self.request)
        ledger.begin(self.request["request_id"], self.request["recipient"], self.request["message"])
        ledger.finish(self.request["request_id"], "persisted_in_chat")
        ledger.close()
        for changes in ({"account_id": "another_account"}, {"account_epoch": 2},
                        {"conversation_id": "another_conversation"},
                        {"platform_id": "another_peer", "profile_url": "https://www.douyin.com/user/another_peer"}):
            with self.subTest(changes=changes), patch.object(worker, "sync_playwright") as launch:
                outcome = worker.execute_request({**self.request, **changes})
                self.assertEqual("idempotency_conflict", outcome["code"])
                self.assertEqual("not_submitted", outcome["submission"])
                launch.assert_not_called()

    def test_old_name_only_ledger_record_is_not_reused_for_identity_bound_task(self):
        account = self.root / "accounts/default"
        account.mkdir(parents=True)
        ledger = SendLedger(account / "sends.sqlite3")
        ledger.begin(self.request["request_id"], self.request["recipient"], self.request["message"])
        ledger.finish(self.request["request_id"], "persisted_in_chat")
        ledger.close()
        with patch.object(worker, "sync_playwright") as launch:
            outcome = worker.execute_request(self.request)
        self.assertEqual("idempotency_conflict", outcome["code"])
        launch.assert_not_called()


class WorkerProtocolTests(unittest.TestCase):
    def test_posix_termination_handler_is_temporary(self):
        output = io.StringIO()
        with patch.object(worker.sys, "platform", "darwin"), \
             patch.object(worker.sys, "stdin", io.StringIO("not-json\n")), \
             patch.object(worker.sys, "stdout", output), \
             patch.object(worker.signal, "getsignal", return_value="previous") as get_handler, \
             patch.object(worker.signal, "signal") as install:
            self.assertEqual(1, worker.run_worker())
        get_handler.assert_called_once_with(worker.signal.SIGTERM)
        self.assertEqual(2, install.call_count)
        handler = install.call_args_list[0].args[1]
        with self.assertRaises(DemoError):
            handler(worker.signal.SIGTERM, None)
        self.assertEqual((worker.signal.SIGTERM, "previous"), install.call_args_list[-1].args)

    def test_invalid_input_emits_one_final_result(self):
        output = io.StringIO()
        with patch.object(worker.sys, "stdin", io.StringIO("not-json\n")), \
             patch.object(worker.sys, "stdout", output):
            code = worker.run_worker()
        lines = output.getvalue().splitlines()
        self.assertEqual(1, code)
        self.assertEqual(1, len(lines))
        self.assertEqual("result", json.loads(lines[0])["event"])
        self.assertEqual("failed", json.loads(lines[0])["status"])

    def test_progress_then_one_final_result(self):
        output = io.StringIO()

        def operation(_request, progress):
            progress("读取中")
            return {"status": "ready", "detail": "已读取", "contacts": ["955"]}

        with tempfile.TemporaryDirectory() as folder:
            payload = json.dumps({"command": "check", "data_dir": folder}) + "\n"
            with patch.object(worker.sys, "stdin", io.StringIO(payload)), \
                 patch.object(worker.sys, "stdout", output), \
                 patch.object(worker, "execute_request", side_effect=operation):
                code = worker.run_worker()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(0, code)
        self.assertEqual(["progress", "result"], [item["event"] for item in events])
        self.assertEqual(["955"], events[-1]["contacts"])


FIXTURE_HTML = b'''<!doctype html><meta charset="utf-8">
<aside class="conversationConversationListwrapper"><button class="conversationConversationItemwrapper" onclick="document.querySelector('h2').textContent='Fixture Friend'">
<span class="conversationConversationItemtitle">Fixture Friend</span></button></aside>
<main class="componentsRightPanelwrapper">
<h2 class="RightPanelHeadertitle"></h2><section class="messageMessageListlist"></section>
<div class="messageEditorinputArea" contenteditable="true"></div></main>
<script>
const editor=document.querySelector('[contenteditable]');
const conversation={id:'0:1:fixture:conversation',toParticipantSecUserId:'fixture_peer',type:1,participantCount:2};
document.querySelector('.conversationConversationItemwrapper').__reactFiber$fixture={memoizedProps:{conversation}};
const current={memoizedProps:{curConversation:conversation}};
document.querySelector('h2').__reactFiber$fixture=current;
editor.__reactFiber$fixture=current;
const messages=JSON.parse(localStorage.getItem('fixtureMessages')||'[]');
function render(){document.querySelector('section').replaceChildren();for(const [index,text] of messages.entries()){
const box=document.createElement('div');box.className='messageMessageBoxmessageBox';
box.setAttribute('data-message-id','fixture-message-'+index);
const mine=document.createElement('div');mine.className='messageMessageBoxisFromMe';
const el=document.createElement('p');el.className='TextMessageTextpureText';el.textContent=text;
mine.append(el);box.append(mine);document.querySelector('section').append(box);}}
editor.innerText=localStorage.getItem('fixtureDraft')||'';render();
editor.addEventListener('input',()=>localStorage.setItem('fixtureDraft',editor.innerText));
editor.addEventListener('keydown',event=>{if(event.key==='Enter'){
event.preventDefault();messages.push(editor.innerText);editor.innerText='';
localStorage.setItem('fixtureDraft','');localStorage.setItem('fixtureMessages',JSON.stringify(messages));render();}});
</script>'''


def fixture_selectors(path):
    """Only this local fixture supplies stable IDs; no production ID is invented."""
    return {**read_selectors(path),
            "message_container": ".messageMessageBoxmessageBox",
            "outgoing_message": ".messageMessageBoxisFromMe",
            "message_text_in_container": ".TextMessageTextpureText",
            "message_id_attribute": "data-message-id"}


class WorkerFixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(FIXTURE_HTML)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.environ.get("SPARK_BROWSER_INTEGRATION") == "1",
                     "Set SPARK_BROWSER_INTEGRATION=1 for bundled Chromium loopback tests")
class BundledChromiumWorkerTests(unittest.TestCase):
    def setUp(self):
        account_patch = patch.object(worker.ChatDirectory, "read_account", return_value=VERIFIED_ACCOUNT)
        account_patch.start()
        self.addCleanup(account_patch.stop)
        # Diagnostics are fixture-only and never sent through the worker JSON
        # protocol. Keep the raw browser cause if an assertion fails, instead of
        # losing a transient launch/navigation error behind "unavailable".
        self.browser_errors = []
        original_error_result = worker._error_result

        def record_error(exc, *args, **kwargs):
            if isinstance(exc, PlaywrightError):
                self.browser_errors.append(repr(exc))
            return original_error_result(exc, *args, **kwargs)

        error_patch = patch.object(worker, "_error_result", side_effect=record_error)
        error_patch.start()
        self.addCleanup(error_patch.stop)

    def assert_worker_status(self, expected, outcome):
        self.assertEqual(expected, outcome["status"], {
            "fixture_url": self.url, "outcome": outcome, "browser_errors": self.browser_errors,
        })

    @classmethod
    def setUpClass(cls):
        configure_browser()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), WorkerFixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_login_labels_with_slashes_use_valid_browser_locators(self):
        with worker.sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            try:
                page = browser.new_page()
                for label in ("登录 / 注册", "登录/注册"):
                    with self.subTest(label=label):
                        page.set_content(f"<button>{label}</button>")
                        self.assertTrue(worker.login_prompt_visible(page))
                page.set_content("<p>这是没有登录入口的离线测试页面。</p>")
                self.assertFalse(worker.login_prompt_visible(page))
                page.set_content('<button style="display:none">登录/注册</button>')
                self.assertFalse(worker.login_prompt_visible(page))
            finally:
                browser.close()

    def test_only_observed_visible_challenge_frame_requires_manual_verification(self):
        with worker.sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            try:
                page = browser.new_page()
                # Render only synthetic frame documents; never load the real host.
                page.route("**/*", lambda route: route.fulfill(status=200, body="offline fixture"))
                for src, style, expected in (
                    ("https://rmc.bytedance.com/verifycenter/captcha/v2?fixture=1", "", True),
                    ("https://rmc.bytedance.com/verifycenter/captcha/v2", "display:none", False),
                    ("https://rmc.bytedance.com/nocaptcha", "", False),
                    ("https://example.com/verifycenter/captcha/v2", "", False),
                ):
                    with self.subTest(src=src, style=style):
                        page.set_content(f'<iframe src="{src}" style="{style}"></iframe>')
                        self.assertEqual(expected, worker.verification_required(page))
            finally:
                browser.close()

    def test_login_wait_uses_all_real_context_pages(self):
        with worker.sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            try:
                context = browser.new_context()
                initial = context.new_page()
                popup = context.new_page()
                initial.close()
                outcome = worker.wait_for_login_window(context, timeout=0)
                self.assertEqual("login_required", outcome["status"])
                self.assertFalse(popup.is_closed())
                popup.close()
                outcome = worker.wait_for_login_window(context, timeout=0)
                self.assertEqual("login_closed", outcome["status"])
                self.assertFalse(outcome["account_validated"])
            finally:
                browser.close()

    def test_send_reload_and_duplicate_request_with_full_bundled_chromium(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(worker, "CHAT_URL", self.url), \
             patch.object(worker, "read_selectors", side_effect=fixture_selectors), \
             patch.dict("os.environ", {"SPARK_BROWSER_CHANNEL": ""}):
            request = {"command": "send", "data_dir": folder, "recipient": "Fixture Friend",
                       "message": "fixture-only", "request_id": "fixture-1",
                       "scheduled_date": worker.beijing_today(), **IDENTITY}
            self.assert_worker_status("persisted_in_chat", worker.execute_request(request))
            with patch.object(worker, "sync_playwright") as launch:
                self.assertEqual("persisted_in_chat", worker.execute_request(request)["status"])
                launch.assert_not_called()
            self.assertEqual("ready", worker.execute_request({"command": "check", "data_dir": folder})["status"])

    def test_midnight_skip_clears_only_its_own_draft_and_never_sends(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(worker, "CHAT_URL", self.url), \
             patch.object(worker, "read_selectors", side_effect=fixture_selectors), \
             patch.dict("os.environ", {"SPARK_BROWSER_CHANNEL": ""}):
            request = {"command": "send", "data_dir": folder, "recipient": "Fixture Friend",
                       "message": "fixture-midnight", "request_id": "fixture-midnight",
                       "scheduled_date": "2026-10-16", **IDENTITY}
            with patch.object(worker, "beijing_today", side_effect=["2026-10-16", "2026-10-17"]):
                self.assert_worker_status("missed", worker.execute_request(request))
            with worker.sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(Path(folder) / "profiles/default"), channel="chromium", headless=True)
                try:
                    page = context.pages[0]
                    page.goto(self.url)
                    self.assertEqual("", page.evaluate("localStorage.getItem('fixtureDraft') || ''").strip())
                    self.assertEqual([], page.evaluate("JSON.parse(localStorage.getItem('fixtureMessages') || '[]')"))
                finally:
                    context.close()

    def test_account_change_after_draft_fill_stops_before_submission_and_rolls_back(self):
        changed = {"platform_id": "changed_account", "verification": "verified"}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(worker, "CHAT_URL", self.url), \
             patch.object(worker, "read_selectors", side_effect=fixture_selectors), \
             patch.object(worker.ChatDirectory, "read_account", side_effect=[VERIFIED_ACCOUNT, changed]), \
             patch.dict("os.environ", {"SPARK_BROWSER_CHANNEL": ""}):
            request = {"command": "send", "data_dir": folder, "recipient": "Fixture Friend",
                       "message": "must-not-send-after-account-change", "request_id": "changed-after-fill",
                       "scheduled_date": worker.beijing_today(), **IDENTITY}
            outcome = worker.execute_request(request)
            self.assert_worker_status("failed", outcome)
            self.assertEqual("account_identity_mismatch", outcome["code"])
            self.assertEqual("not_submitted", outcome["submission"])
            self.assertFalse(outcome["account_validated"])
            ledger = SendLedger(Path(folder) / "accounts/default/sends.sqlite3")
            try:
                self.assertIsNone(ledger.get(request["request_id"]))
            finally:
                ledger.close()
            with worker.sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(Path(folder) / "profiles/default"), channel="chromium", headless=True)
                try:
                    page = context.pages[0]
                    page.goto(self.url)
                    self.assertEqual("", page.evaluate("localStorage.getItem('fixtureDraft') || ''").strip())
                    self.assertEqual([], page.evaluate("JSON.parse(localStorage.getItem('fixtureMessages') || '[]')"))
                finally:
                    context.close()

    def test_no_stable_message_id_is_uncertain_and_not_repeated(self):
        def no_id_selectors(path):
            return {**fixture_selectors(path), "message_id_attribute": ""}

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(worker, "CHAT_URL", self.url), \
             patch.object(worker, "read_selectors", side_effect=no_id_selectors), \
             patch.dict("os.environ", {"SPARK_BROWSER_CHANNEL": ""}):
            request = {"command": "send", "data_dir": folder, "recipient": "Fixture Friend",
                       "message": "fixture-without-id", "request_id": "fixture-without-id",
                       "scheduled_date": worker.beijing_today(), **IDENTITY}
            outcome = worker.execute_request(request)
            self.assert_worker_status("uncertain", outcome)
            self.assertEqual("message_unverified", outcome["code"])
            self.assertEqual("possible", outcome["submission"])
            self.assertTrue(outcome["account_validated"])
            with patch.object(worker, "sync_playwright") as launch:
                self.assertEqual("uncertain", worker.execute_request(request)["status"])
                launch.assert_not_called()
            with worker.sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(Path(folder) / "profiles/default"), channel="chromium", headless=True)
                try:
                    page = context.pages[0]
                    page.goto(self.url)
                    self.assertEqual(["fixture-without-id"], page.evaluate(
                        "JSON.parse(localStorage.getItem('fixtureMessages') || '[]')"))
                finally:
                    context.close()


if __name__ == "__main__":
    unittest.main()

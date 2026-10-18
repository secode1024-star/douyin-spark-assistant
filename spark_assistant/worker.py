"""One browser operation in a child process; JSON lines are the only stdout output.

The browser profile stays local. A matching message after page reload is UI
evidence, not a server delivery receipt. Once submission may have happened,
the durable ledger prevents an automatic retry of the same request.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from douyin_demo import (
    CHAT_URL, DemoError, SendLedger, profile_lock, read_selectors,
)
from spark_assistant.chat_directory import ChatDirectory
from spark_assistant.paths import resource_path
from spark_assistant.validation import normalize_recipient, validate_message


PROFILE_MARKER = ".spark-assistant-managed"
LOGIN_URL = "https://www.douyin.com/"
COMMANDS = {"login", "check", "contacts", "sync_friends", "send", "logout"}
LOGIN_BUTTON_LABELS = ("登录", "立即登录", "扫码登录", "登录 / 注册", "登录/注册")
CONTACTS_NOTE = "仅列出当前聊天页已加载的会话；未出现的好友可能尚未加载，请先在抖音手动发消息后刷新。"
BEIJING = timezone(timedelta(hours=8))
SAFE_SEND_STAGES = {"selecting_recipient", "validating_draft", "filling_message", "rechecking_account",
                    "recording_submission", "validating_submission", "dispatching_submission",
                    "checking_message", "reloading_chat", "verifying_persistence"}
SAFE_CAUSE_TYPES = {"DemoError", "MissedDayError", "Error", "PlaywrightError", "TimeoutError",
                    "OperationalError", "DatabaseError", "IntegrityError", "ProgrammingError"}
SAFE_UNCERTAIN_CODES = {"message_unverified", "submission_uncertain", "submission_context_changed",
                        "recipient_identity_unverified", "recipient_changed", "recipient_ambiguous",
                        "account_identity_unverified", "account_identity_mismatch", "scheduled_day_expired",
                        "page_changed", "operation_cancelled", "already_attempted", "draft_cleanup_required",
                        "editor_text_changed", "scheduled_day_expired_before_submit", "editor_detached",
                        "submission_guard_blocked", "verification_required"}
UNCERTAIN_DETAILS = {
    "submission_context_changed": "提交前核对发现正文、日期或会话变化，已停止后续操作；结果待人工核对，未自动重试。",
    "recipient_identity_unverified": "提交过程中无法核验收件人身份，已停止后续操作；结果待人工核对，未自动重试。",
    "account_identity_mismatch": "提交过程中登录账号发生变化，已停止后续操作；结果待人工核对，未自动重试。",
    "account_identity_unverified": "提交过程中无法再次核验登录账号，已停止后续操作；结果待人工核对，未自动重试。",
    "editor_text_changed": "提交前发现输入框正文发生变化，已停止后续操作；结果待人工核对，未自动重试。",
    "scheduled_day_expired_before_submit": "提交前任务已跨过北京时间当天，已停止后续操作；结果待人工核对，未自动重试。",
    "editor_detached": "提交前输入框已被页面替换，已停止后续操作；结果待人工核对，未自动重试。",
    "submission_guard_blocked": "提交时页面状态检查未通过，已停止后续操作；结果待人工核对，未自动重试。",
    "verification_required": "抖音官网要求安全验证，请手动打开“登录抖音”处理并核对消息；未自动重试。",
}


class MissedDayError(DemoError):
    pass


def beijing_today() -> str:
    return datetime.now(BEIJING).date().isoformat()


def require_same_day(scheduled_date: str) -> None:
    if scheduled_date != beijing_today():
        raise MissedDayError("任务已不属于北京时间当天，已跳过，没有发送。",
                             code="scheduled_day_expired")


class DailySendLedger(SendLedger):
    """Recheck the day at the durable submission boundary, just before Enter."""
    def __init__(self, path: Path, scheduled_date: str | None):
        super().__init__(path)
        self.scheduled_date = scheduled_date

    def begin(self, request_id: str, contact: str, message: str):
        if self.scheduled_date is None:
            raise DemoError("发送任务缺少约定日期，未发送。", code="invalid_request",
                            scope="operation")
        require_same_day(self.scheduled_date)
        super().begin(request_id, contact, message)


def emit(event: str, **values) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def result(status: str, detail: str, *, code: str | None = None,
           scope: str = "operation", submission: str = "not_submitted",
           account_validated: bool = False, **values) -> dict:
    """Stable UI protocol; an unverified account is not necessarily logged out."""
    return {"status": status, "detail": detail, "code": code or status,
            "scope": scope, "submission": submission,
            "account_validated": account_validated, **values}


def invalid_request(detail: str) -> DemoError:
    return DemoError(detail, code="invalid_request", scope="operation")


def send_identity(request: dict) -> dict:
    """Bind every durable send to both accounts and the exact conversation.

    Display names are labels only. In particular, an old name-only request may
    not be upgraded implicitly to a newly discovered recipient.
    """
    peer_id = request.get("platform_id")
    account_id = request.get("account_id", request.get("account_platform_id"))
    for value in (peer_id, account_id):
        if (not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
                or value == "self"):
            raise invalid_request("发送任务缺少已核验的账号或好友标识，请重新同步聊天列表。")
    if (request.get("account_platform_id") is not None
            and request.get("account_platform_id") != account_id):
        raise invalid_request("发送任务的账号绑定不一致，请重新确认任务。")
    if request.get("profile_url") != f"https://www.douyin.com/user/{peer_id}":
        raise invalid_request("好友主页与已核验的标识不一致，请重新同步聊天列表。")
    conversation_id = request.get("conversation_id")
    if (not isinstance(conversation_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", conversation_id)):
        raise invalid_request("发送任务缺少已核验的会话标识，请重新同步聊天列表。")
    epoch = request.get("account_epoch")
    if type(epoch) is not int or epoch < 1:
        raise invalid_request("发送任务缺少有效的账号绑定版本，请重新确认任务。")
    return {"account_id": account_id, "account_epoch": epoch,
            "platform_id": peer_id, "conversation_id": conversation_id}


def verified_account(account: object) -> bool:
    if not isinstance(account, dict) or account.get("verification") != "verified":
        return False
    platform_id = account.get("platform_id")
    return (isinstance(platform_id, str) and platform_id != "self"
            and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", platform_id) is not None)


def validate_request(request: object) -> dict:
    if not isinstance(request, dict) or request.get("command") not in COMMANDS:
        raise invalid_request("不支持的浏览器操作。")
    raw_root = request.get("data_dir")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise invalid_request("缺少本地数据目录。")
    root = Path(raw_root).expanduser()
    if not root.is_absolute() or root == Path(root.anchor):
        raise invalid_request("本地数据目录必须是应用专用的绝对路径。")
    if request["command"] == "send":
        recipient = request.get("recipient")
        message = request.get("message")
        request_id = request.get("request_id")
        try:
            recipient = normalize_recipient(recipient)
            message = validate_message(message)
        except ValueError as exc:
            raise invalid_request(str(exc)) from exc
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request_id):
            raise invalid_request("缺少有效的任务执行编号。")
        scheduled_date = request.get("scheduled_date")
        if not isinstance(scheduled_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scheduled_date):
            raise invalid_request("发送任务缺少有效的北京时间约定日期。")
        try:
            date.fromisoformat(scheduled_date)
        except ValueError as exc:
            raise invalid_request("发送任务的约定日期无效。") from exc
        identity = send_identity(request)
        request = {**request, **identity, "recipient": recipient, "message": message}
    return {**request, "data_dir": str(root)}


def managed_path(root: Path, *parts: str) -> Path:
    """Reject redirected child paths before opening profiles or deleting files."""
    root = root.resolve()
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise DemoError("应用数据子目录存在链接，请先检查本地目录；操作已停止。")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise DemoError("应用数据目录不在允许范围内。")
    return path


def clear_managed_profile(root: Path) -> None:
    """Delete only this app's marked profile; keep task history and send ledger."""
    profile = managed_path(root, "profiles", "default")
    if not profile.exists():
        return
    marker = profile / PROFILE_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise DemoError("该目录缺少应用标记，未清除登录资料。")
    # Verify the complete absolute target immediately before recursive deletion.
    target = managed_path(root, "profiles", "default")
    if target.resolve() != (root.resolve() / "profiles" / "default"):
        raise DemoError("登录资料目录发生变化，已停止清除。")
    shutil.rmtree(target)


def visible_contacts(page, selectors: dict) -> list[str] | None:
    """None means the observed chat UI is unavailable; duplicates are retained."""
    containers = page.locator(selectors["conversation_list"])
    visible = [containers.nth(i) for i in range(containers.count())
               if containers.nth(i).is_visible()]
    if len(visible) != 1:
        return None
    names = visible[0].locator(selectors["conversation_name"])
    return [name for i in range(names.count()) if names.nth(i).is_visible()
            if (name := names.nth(i).inner_text().strip())]


def login_prompt_visible(page) -> bool:
    """Use visible public UI wording, never inspect cookies to infer login."""
    # Exact accessible names avoid passing unescaped slashes through Playwright's
    # role-selector regex serialization ("登录 / 注册" previously raised a parse error).
    prompts = [page.get_by_role("button", name=label, exact=True)
               for label in LOGIN_BUTTON_LABELS]
    prompts += [page.get_by_text("扫码登录", exact=True),
                page.get_by_text("请先登录", exact=True)]
    return any(prompt.nth(i).is_visible() for prompt in prompts
               for i in range(prompt.count()))


def verification_required(page) -> bool:
    """Only an observed official challenge marker is treated as verification."""
    if page.title().strip() == "验证码中间页":
        return True
    frames = page.locator("iframe[src]")
    for index in range(frames.count()):
        frame = frames.nth(index)
        if not frame.is_visible():
            continue
        src = frame.get_attribute("src")
        if not isinstance(src, str):
            continue
        parsed = urlsplit(src)
        if (parsed.scheme == "https" and parsed.netloc == "rmc.bytedance.com"
                and parsed.path == "/verifycenter/captcha/v2"):
            return True
    return False


def verification_result() -> dict:
    return result("login_required", "抖音官网要求安全验证。请手动打开“登录抖音”完成验证后再检查；程序不会自动弹出浏览器或重试发送。",
                  code="verification_required", scope="account")


def wait_for_login_window(context, *, timeout: float = 480) -> dict:
    """Wait for all pages in this dedicated context, including login popups.

    The homepage is not the chat UI. Neither a disappearing login button nor
    closing the browser proves authentication, so only the later check command
    can mark the account ready.
    """
    deadline = time.monotonic() + timeout
    context_closed = False

    def on_close(_context):
        nonlocal context_closed
        context_closed = True

    context.on("close", on_close)
    try:
        while True:
            pages = [] if context_closed else [page for page in context.pages if not page.is_closed()]
            if not pages:
                return result("login_closed", "登录窗口已全部关闭，将检查保存的登录状态。",
                              code="login_windows_closed")
            if time.monotonic() >= deadline:
                return result("login_required", "登录窗口等待超时，请重新打开官网登录。",
                              code="login_timeout", scope="account")
            try:
                # A Playwright wait dispatches context/page events. Always select
                # a live page again after a tab closes; another popup may remain.
                pages[0].wait_for_timeout(500)
            except PlaywrightError:
                if not context_closed and not pages[0].is_closed():
                    raise
    finally:
        context.remove_listener("close", on_close)


def wait_for_account(page, selectors: dict, *, login: bool,
                     timeout: float | None = None, allow_empty: bool = False) -> dict:
    """Login readiness does not depend on any one selected friend's name."""
    deadline = time.monotonic() + (timeout if timeout is not None else (600 if login else 45))
    saw_login = False
    while True:
        if page.is_closed():
            return result("unavailable", "浏览器已关闭，操作结束。",
                          code="browser_closed", scope="account")
        if verification_required(page):
            return verification_result()
        contacts = visible_contacts(page, selectors)
        if contacts is not None and (contacts or allow_empty):
            return result("ready", "已读取到聊天会话。" + CONTACTS_NOTE,
                          code="account_ready", scope="account", account_validated=True,
                          contacts=contacts)
        saw_login = login_prompt_visible(page)
        # The page can briefly show a login button while restoring its session.
        # Keep waiting for the observed chat UI before concluding login is needed.
        if time.monotonic() >= deadline:
            if saw_login:
                return result("login_required", "尚未完成登录，请再次打开扫码登录。",
                              code="login_required", scope="account")
            return result("unavailable", "未读到会话列表，可能是网络异常、暂无会话或网页结构变化。",
                          code="account_unavailable", scope="account")
        page.wait_for_timeout(500)


def _error_result(exc: Exception, ledger: SendLedger | None, request_id: str | None,
                  *, account_validated: bool = False) -> dict:
    """Error classification must survive a second storage failure after submission."""
    code = getattr(exc, "code", "operation_failed")
    diagnostics = {}
    if isinstance(getattr(exc, "stage", None), str) and exc.stage in SAFE_SEND_STAGES:
        diagnostics["stage"] = exc.stage
    cause_type = getattr(exc, "cause_type", type(exc).__name__)
    if isinstance(cause_type, str) and cause_type in SAFE_CAUSE_TYPES:
        diagnostics["cause_type"] = cause_type
    # A result for a different payload must never be reported as this message's
    # success, even if the old request has a persisted_in_chat ledger entry.
    if code == "idempotency_conflict":
        return result("failed", str(exc), code=code, scope="task",
                      account_validated=account_validated, **diagnostics)
    possible = (getattr(ledger, "submission_started", False) is True
                or getattr(exc, "submission", "not_submitted") == "possible")
    existing = None
    ledger_unreadable = False
    if ledger is not None and request_id:
        try:
            existing = ledger.get(request_id)
        except Exception:
            # This request may already exist durably, including from a previous
            # worker. Failure to read it is never proof of non-submission.
            ledger_unreadable = True
    if existing:
        # A status-only recovery read does not prove the stored payload matches
        # this request. Only the explicit lookup() success branch may reuse a
        # prior verified outcome; errors remain conservative, even for persisted.
        possible = True
    if possible or ledger_unreadable:
        # These adapter messages are intentional user-facing explanations, not
        # raw Playwright diagnostics (which can contain page text or URLs).
        known = isinstance(exc, DemoError) and code in SAFE_UNCERTAIN_CODES
        explanation = (str(exc) if known and code == "message_unverified"
                       else UNCERTAIN_DETAILS.get(code) if known else None)
        return result("uncertain", explanation or "消息可能已提交，结果待核对；未自动重试。",
                      code=code if known else "submission_uncertain", scope="task", submission="possible",
                      account_validated=account_validated and code not in {
                          "verification_required", "account_identity_mismatch", "account_identity_unverified"},
                      **diagnostics)
    if isinstance(exc, MissedDayError):
        return result("missed", str(exc), code="scheduled_day_expired", scope="task",
                      account_validated=account_validated, **diagnostics)
    if isinstance(exc, DemoError) and code == "verification_required":
        return {**verification_result(), **diagnostics}
    if isinstance(exc, DemoError):
        scope = getattr(exc, "scope", "task")
        return result("failed", str(exc), code=code, scope=scope,
                      account_validated=account_validated if scope == "task" else False, **diagnostics)
    if isinstance(exc, PlaywrightError):
        return result("unavailable", "浏览器或网页检查未完成，暂时无法确认登录状态。请重试检查。",
                      code="browser_unavailable", scope="account", **diagnostics)
    return result("failed", "本地浏览器操作失败，请查看应用状态后重试。",
                  code="operation_failed", **diagnostics)


def execute_request(request: dict, progress: Callable[[str], None] | None = None) -> dict:
    request = validate_request(request)
    progress = progress or (lambda _message: None)
    root = Path(request["data_dir"]).resolve()
    account = managed_path(root, "accounts", "default")
    account.mkdir(parents=True, exist_ok=True)
    command = request["command"]
    ledger = None
    context = None
    account_validated = False
    try:
        with profile_lock(account):
            if command == "logout":
                clear_managed_profile(root)
                return result("logged_out", "已清除本应用保存的登录资料，执行记录仍保留。",
                              scope="account")
            ledger = DailySendLedger(account / "sends.sqlite3", request.get("scheduled_date"))
            request_id = request.get("request_id")
            if command == "send":
                ledger.identity = send_identity(request)
                previous = ledger.lookup(request_id, request["recipient"], request["message"])
                if previous:
                    if previous == "persisted_in_chat":
                        return result("persisted_in_chat", "此执行编号及消息已有网页刷新核对记录，未重复发送。",
                                      code="already_verified", scope="task", submission="confirmed")
                    return result("uncertain", "此执行编号已有发送记录，结果待核对；未自动重试。",
                                  code="request_already_recorded", scope="task", submission="possible")
                require_same_day(request["scheduled_date"])
            profile = managed_path(root, "profiles", "default")
            profile.mkdir(parents=True, exist_ok=True)
            marker = profile / PROFILE_MARKER
            if marker.is_symlink():
                raise DemoError("登录资料标记存在链接，操作已停止。")
            marker.touch(exist_ok=True)
            selectors = read_selectors(Path(resource_path("selectors.json")))
            channel = os.environ.get("SPARK_BROWSER_CHANNEL", "").strip()
            if channel not in {"", "chrome", "msedge", "chromium"}:
                raise DemoError("开发用浏览器通道无效。")
            # Keep background operations headless; only the explicit login
            # action opens a visible browser. Never retry with a visible fallback.
            options = {"channel": channel or "chromium"}
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), headless=command != "login",
                        viewport={"width": 1280, "height": 900}, **options,
                    )
                    progress("正在打开抖音官网。请点击官网的登录入口；完成后关闭本次浏览器的全部窗口和标签页，程序会自动检查。" if command == "login"
                             else "正在后台读取本机保存的抖音会话并核验登录状态。")
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(5000)
                    page.goto(LOGIN_URL if command == "login" else CHAT_URL,
                              wait_until="domcontentloaded", timeout=60000)
                    if command == "login":
                        return wait_for_login_window(context)
                    if verification_required(page):
                        return verification_result()
                    directory = ChatDirectory(page, selectors)
                    directory.progress = progress
                    if command == "sync_friends":
                        progress("正在读取已有聊天会话，并核验账号和会话标识。")
                        synced = directory.sync()
                        if verification_required(page):
                            return {**verification_result(), "account": synced.get("account", {}),
                                    "friends": [], "coverage": {"directory_kind": "existing_chats",
                                    "status": "verification_required", "reason_code": "verification_required"}}
                        verified = verified_account(synced.get("account"))
                        coverage = synced.get("coverage", {})
                        reason = coverage.get("reason_code")
                        directory_available = coverage.get("status") in {"complete", "partial"}
                        if not verified:
                            detail = "尚未核验当前抖音账号，请重新登录后刷新聊天列表。"
                            code = "account_identity_unverified"
                        elif reason == "account_identity_mismatch":
                            detail = "读取期间抖音登录账号发生变化，本次列表未保存。请确认当前账号后重新读取。"
                            code = "account_identity_mismatch"
                        elif reason in {"sync_time_limit", "scan_interrupted", "scroll_limit_reached"}:
                            explanation = {"sync_time_limit": "读取达到本次时间上限",
                                           "scan_interrupted": "读取被中断",
                                           "scroll_limit_reached": "本次列表滚动读取达到上限"}[reason]
                            count = len(synced.get("friends", []))
                            detail = f"{explanation}，已保留读到的 {count} 条聊天会话，可重新读取。" + CONTACTS_NOTE
                            code = reason
                        elif not directory_available:
                            detail = "当前聊天列表读取未完成，请检查官网页面后重新刷新。" + CONTACTS_NOTE
                            code = reason or "chat_directory_unavailable"
                        else:
                            detail = "聊天列表读取完成。" + CONTACTS_NOTE
                            code = "chat_directory_synced"
                        return result("ready" if verified and directory_available else "unavailable", detail,
                                      code=code,
                                      scope="account", account_validated=verified,
                                      account=synced.get("account", {}), friends=synced.get("friends", []),
                                      coverage=coverage)
                    progress("聊天页面已打开，正在等待会话加载并核验登录状态。")
                    observed_account = directory.read_account()
                    # A verified own-profile identity plus the observed chat
                    # shell can be ready with zero loaded rows. A blank page
                    # remains unavailable and cannot open this gate.
                    ready = wait_for_account(page, selectors, login=False,
                                             allow_empty=verified_account(observed_account))
                    if ready["status"] != "ready":
                        return ready
                    if not verified_account(observed_account):
                        return result("unavailable", "无法核验当前抖音账号，请重新登录后检查。",
                                      code="account_identity_unverified", scope="account",
                                      account=observed_account)
                    if command != "send":
                        return {**ready, "account": observed_account, "account_validated": True}
                    if observed_account["platform_id"] != request["account_id"]:
                        return result("failed", "当前登录的抖音账号与任务绑定账号不同，已停止发送。请刷新聊天列表并重新确认任务。",
                                      code="account_identity_mismatch", scope="account",
                                      account=observed_account)
                    account_validated = True
                    progress("正在核对会话标识、收件人和输入框。")
                    ui = directory.identity_ui(request)
                    # The core alone owns draft rollback: it retains the exact
                    # original editor identity, which this outer layer does not.
                    outcome = ui.send_once(request["recipient"], request["message"], request_id, ledger)
                    if outcome["status"] == "persisted_in_chat":
                        return result("persisted_in_chat", "网页刷新后仍可见本次发送的我方消息；这不是服务端送达回执。",
                                      code="message_verified", scope="task", submission="confirmed",
                                      account_validated=True)
                    # A non-success outcome from the adapter is never promoted to
                    # verification simply because no Python exception occurred.
                    return result("uncertain", outcome.get("detail") or "消息可能已提交，未能核对本次消息；请人工检查，不自动重发。",
                                  code=outcome.get("code") or "submission_uncertain", scope="task", submission="possible",
                                  account_validated=True)
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            # Cleanup cannot erase a known submission result. The
                            # isolated worker's process guard handles descendants.
                            pass
    except Exception as exc:
        return _error_result(exc, ledger, request.get("request_id"),
                             account_validated=account_validated)
    finally:
        if ledger is not None:
            try:
                ledger.close()
            except Exception:
                # A second close/connection failure must not escape to run_worker
                # and mislabel a possibly submitted message as a plain failure.
                pass


def run_worker() -> int:
    """Read exactly one operation from stdin, then emit exactly one final result."""
    previous_sigterm = None
    try:
        if sys.platform != "win32" and threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)

            def cancel(_signum, _frame):
                raise DemoError("操作已停止。", code="operation_cancelled", scope="operation")

            signal.signal(signal.SIGTERM, cancel)
        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        line = sys.stdin.readline(65537)
        if len(line) > 65536:
            raise invalid_request("浏览器操作请求过长。")
        request = validate_request(json.loads(line))
        outcome = execute_request(request, lambda message: emit("progress", message=message))
    except (json.JSONDecodeError, UnicodeDecodeError):
        outcome = result("failed", "无法读取浏览器操作请求。", code="invalid_request")
    except Exception as exc:
        outcome = _error_result(exc, None, None)
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    emit("result", **outcome)
    return 0 if outcome["status"] in {"ready", "persisted_in_chat", "logged_out", "login_closed"} else 1


if __name__ == "__main__":
    raise SystemExit(run_worker())

"""Local Douyin chat proof of concept. Uses official UI, never extracts cookies."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from spark_assistant.validation import normalize_recipient, validate_message

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

# The official editor renders its final line terminator as a separate U+200B
# leaf. Only an explicitly configured, visible final placeholder leaf is
# excluded. User-authored zero-width characters and whitespace remain intact.
EDITOR_TEXT_JS = r"""(editor, placeholderSelector) => {
    if (editor instanceof HTMLInputElement || editor instanceof HTMLTextAreaElement) return editor.value;
    const raw = editor.innerText ?? '';
    if (!placeholderSelector || !editor.isContentEditable || !raw.endsWith('\u200b')) return raw;
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let last = null;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (node.data.length) last = node;
    const placeholder = last?.parentElement?.closest(placeholderSelector);
    if (!placeholder || !editor.contains(placeholder) || placeholder.childNodes.length !== 1 ||
        placeholder.firstChild !== last || last.data !== '\u200b' ||
        !placeholder.getClientRects().length || getComputedStyle(placeholder).visibility === 'hidden') return raw;
    return raw.slice(0, -1);
}"""


class DemoError(RuntimeError):
    """A user-readable failure with machine-readable recovery information."""
    def __init__(self, message: str, *, code: str = "task_error",
                 scope: str = "task", submission: str = "not_submitted"):
        super().__init__(message)
        self.code = code
        self.scope = scope
        self.submission = submission


def normalize_message(message: str) -> str:
    """Validate with the shared contract, preserving the user's exact text."""
    try:
        return validate_message(message)
    except ValueError as exc:
        raise DemoError(str(exc), code="invalid_message") from exc


def normalize_contact(contact: str) -> str:
    try:
        return normalize_recipient(contact)
    except ValueError as exc:
        raise DemoError(str(exc), code="invalid_recipient") from exc


def account_dir(account: str, root: Path = ROOT / ".local") -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", account):
        raise DemoError("账号目录名只能包含 1 至 40 个英文字母、数字、下划线和短横线。")
    if account.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }:
        raise DemoError("账号目录名不能使用 Windows 保留名称。")
    path = root / "accounts" / account
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def profile_lock(path: Path):
    """OS lock releases automatically on process exit; no stale-PID deletion."""
    with (path / "session.lock").open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DemoError("该账号已有浏览器任务在运行，请先关闭原任务。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SendLedger:
    """A durable request ID prevents blindly repeating an uncertain UI send."""
    def __init__(self, path: Path):
        # Never reset this on finish/close: error classification must not depend
        # on a second database read after submission may already have happened.
        self.submission_started = False
        self.identity: dict | None = None
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sends (
            request_id TEXT PRIMARY KEY, contact_hash TEXT NOT NULL,
            message_hash TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, request_id: str):
        row = self.db.execute(
            "SELECT status FROM sends WHERE request_id = ?", (request_id,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _contact_digest(self, contact: str) -> str:
        # Display names can change. Identity-bound desktop requests are tied to
        # the account epoch and exact conversation, never to a mutable remark.
        value = ("identity-v2\x00" + json.dumps(self.identity, sort_keys=True, separators=(",", ":"))
                 if self.identity is not None else normalize_contact(contact))
        return self._digest(value)

    def lookup(self, request_id: str, contact: str, message: str):
        """Reuse an outcome only when the immutable request payload also matches."""
        row = self.db.execute(
            "SELECT status,contact_hash,message_hash FROM sends WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if (row[1] != self._contact_digest(contact)
                or row[2] != self._digest(normalize_message(message))):
            raise DemoError("执行编号已用于不同的收件人或消息，本次操作已停止。",
                            code="idempotency_conflict")
        return row[0]

    def begin(self, request_id: str, contact: str, message: str):
        contact = normalize_contact(contact)
        message = normalize_message(message)
        now = time.time()
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO sends VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, self._contact_digest(contact), self._digest(message), "uncertain", now, now),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.lookup(request_id, contact, message)
            raise DemoError(
                f"请求 {request_id} 已执行或结果待核对（{existing}），禁止重复发送。",
                code="already_attempted", submission="possible",
            ) from exc
        self.submission_started = True

    def finish(self, request_id: str, status: str):
        with self.db:
            self.db.execute(
                "UPDATE sends SET status = ?, updated_at = ? WHERE request_id = ?",
                (status, time.time(), request_id),
            )


def read_selectors(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def visible_one(locator, description: str):
    visible = [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]
    if len(visible) != 1:
        raise DemoError(f"{description}匹配到 {len(visible)} 个可见元素，需要唯一匹配；未发送。")
    return visible[0]


def editor_has_draft(editor) -> bool:
    # Douyin's empty editor contains U+200B; whitespace trimming alone is insufficient.
    return editor.evaluate(
        r"e => /[^\s\u200b\ufeff]/u.test((e instanceof HTMLInputElement || e instanceof HTMLTextAreaElement) ? e.value : (e.innerText ?? ''))"
    )


class ChatUI:
    EVIDENCE_TIMEOUT = 15
    RELOAD_SETTLE_MS = 5000

    def __init__(self, page, selectors: dict):
        self.page = page
        self.selectors = selectors

    def required(self, key: str) -> str:
        value = self.selectors.get(key, "").strip()
        if not value:
            raise DemoError(f"缺少页面选择器 {key}，请先 inspect 并适配 selectors.json。",
                            code="page_changed", scope="account")
        return value

    def contact(self, name: str):
        container = visible_one(
            self.page.locator(self.required("conversation_list")), "会话列表"
        )
        names = container.locator(self.required("conversation_name"))
        matches = []
        for i in range(names.count()):
            candidate = names.nth(i)
            if candidate.is_visible() and candidate.inner_text().strip() == name:
                matches.append(candidate)
        if len(matches) != 1:
            raise DemoError(f"列表内备注 {name!r} 匹配到 {len(matches)} 项；不会猜测收件人。",
                            code="recipient_ambiguous" if len(matches) > 1 else "recipient_not_found")
        return matches[0]

    def ready(self, contact: str) -> bool:
        try:
            self.contact(contact)
            return True
        except (DemoError, PlaywrightError):
            return False

    def wait_ready(self, contact: str, seconds: int):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise DemoError("浏览器已关闭。")
            if self.ready(contact):
                return
            self.page.wait_for_timeout(500)
        raise DemoError("未找到指定会话：可能未登录、备注不符、会话未加载或页面选择器变化。")

    def select_contact(self, name: str):
        self.contact(name).click()
        header = self.page.locator(self.required("conversation_header"))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                title = visible_one(header, "当前会话标题")
                if title.inner_text().strip() == name:
                    editor = visible_one(self.page.locator(self.required("editor")), "消息输入框")
                    return editor
            except (DemoError, PlaywrightError):
                pass
            self.page.wait_for_timeout(200)
        raise DemoError("当前会话标题与目标备注不一致，或没有唯一输入框；未发送。")

    def exact_message_count(self, message: str) -> int:
        """Read-only text inspection; counts are never used to confirm a send."""
        message = normalize_message(message)
        bubbles = self.page.locator(self.required("message_text"))
        return sum(
            1 for i in range(bubbles.count())
            if bubbles.nth(i).is_visible() and bubbles.nth(i).text_content() == message
        )

    def message_snapshot(self) -> dict | None:
        """Read visible message identity/direction using explicitly configured DOM.

        A list position (for example data-index) is not a stable message ID. The
        production configuration intentionally leaves the ID attribute empty
        until a real stable attribute has been verified on the official page.
        """
        config = {key: self.selectors.get(key, "").strip() for key in (
            "message_container", "outgoing_message", "message_text_in_container",
            "message_id_attribute", "message_failed", "message_pending",
        )}
        if not all(config[key] for key in (
                "message_container", "outgoing_message", "message_text_in_container")):
            return None
        rows = self.page.locator(config["message_container"]).evaluate_all("""(elements, config) => {
            const marked = (element, selector) => !!selector &&
                (element.matches(selector) || !!element.querySelector(selector));
            return elements.filter(element => element.getClientRects().length > 0 &&
                getComputedStyle(element).visibility !== 'hidden').map(element => {
                const texts = element.matches(config.message_text_in_container) ? [element] :
                    [...element.querySelectorAll(config.message_text_in_container)];
                return {
                    id: config.message_id_attribute ? element.getAttribute(config.message_id_attribute) : null,
                    outgoing: marked(element, config.outgoing_message),
                    text: texts.length === 1 ? (texts[0].textContent ?? '') : null,
                    blocked: marked(element, config.message_failed) || marked(element, config.message_pending)
                };
            });
        }""", config)
        ids = [row["id"] for row in rows if row["id"]]
        outgoing = [row for row in rows if row["outgoing"]]
        identified = bool(config["message_id_attribute"] and config["message_id_attribute"] != "data-index"
                          and len(ids) == len(set(ids))
                          and all(row["id"] and row["text"] is not None for row in outgoing))
        return {
            "identified": identified,
            "known_ids": set(ids),
            "outgoing": {row["id"]: row["text"] for row in outgoing
                         if row["id"] and row["text"] is not None and not row["blocked"]},
            "visible_outgoing_texts": [row["text"] for row in outgoing
                                       if row["text"] is not None and not row["blocked"]],
        }

    @staticmethod
    def _new_outgoing_id(before: dict | None, after: dict | None, message: str) -> str | None:
        if before is None or after is None or not before["identified"] or not after["identified"]:
            return None
        candidates = [key for key, text in after["outgoing"].items()
                      if key not in before["known_ids"] and text == message]
        return candidates[0] if len(candidates) == 1 else None

    def _clear_owned_draft(self, contact: str, message: str, original_editor) -> bool:
        """Only undo this operation's exact text in the same live editor/conversation."""
        try:
            current = visible_one(self.page.locator(self.required("editor")), "消息输入框")
            self.verify_recipient(contact, current)
            if not current.evaluate("(element, original) => element === original && element.isConnected", original_editor):
                return False
            raw = self.editor_text(current)
            if not re.search(r"[^\s\u200b\ufeff]", raw):
                return True
            if raw != message:
                return False
            had_placeholder = current.evaluate("element => element.innerText ?? ''") != raw
            current.fill("")
            if not editor_has_draft(current):
                return True
            if not had_placeholder:
                return False
            # The observed rich editor ignores fill('') while accepting native
            # Select All / Backspace. Use it only for our unchanged original
            # editor, conversation and exact message, never a user's new draft.
            self.verify_recipient(contact, current)
            if (not current.evaluate("(element, original) => element === original && element.isConnected", original_editor)
                    or self.editor_text(current) != message):
                return False
            config = self.draft_guard_config(message)
            guard_js = self.submission_guard_js()
            original_editor.evaluate("""(editor, config) => {
                const verify = """ + guard_js + """;
                const guard = event => {
                    if (event.key !== 'Backspace') return;
                    if (event.target !== editor || !verify(editor, config)) {
                        event.preventDefault(); event.stopImmediatePropagation();
                    }
                    document.removeEventListener('keydown', guard, true);
                };
                editor.__sparkDraftGuard = guard;
                document.addEventListener('keydown', guard, true);
            }""", config)
            try:
                original_editor.press('ControlOrMeta+A', timeout=5000)
                self.verify_recipient(contact, current)
                if (not current.evaluate("(element, original) => element === original && element.isConnected", original_editor)
                        or self.editor_text(current) != message):
                    return False
                original_editor.press('Backspace', timeout=5000)
                self.page.wait_for_timeout(350)
                return not editor_has_draft(current)
            finally:
                try:
                    original_editor.evaluate("e => {if(e.__sparkDraftGuard) document.removeEventListener('keydown',e.__sparkDraftGuard,true); delete e.__sparkDraftGuard;}")
                except PlaywrightError:
                    pass
        except (DemoError, PlaywrightError):
            return False

    def verify_recipient(self, contact: str, editor) -> None:
        """Legacy demo name check; desktop identity adapter overrides this."""
        header = visible_one(self.page.locator(self.required("conversation_header")), "当前会话标题")
        if header.inner_text().strip() != contact:
            raise DemoError("发送前会话已变化，已停止；未发送。", code="recipient_changed")

    def editor_text(self, editor) -> str:
        return editor.evaluate(EDITOR_TEXT_JS, self.selectors.get('editor_placeholder', ''))

    def draft_guard_config(self, message: str) -> dict:
        return {'message': message, 'scheduled_date': None,
                'editor_placeholder': self.selectors.get('editor_placeholder', '')}

    def submit(self, editor, button) -> None:
        self.send_stage = 'validating_submission'
        config = self.submission_guard_config()
        guard_js = self.submission_guard_js()
        if not editor.evaluate(guard_js, config):
            observed = editor.evaluate("""(element, config) => ({
                connected: element.isConnected,
                text_matches: (""" + EDITOR_TEXT_JS + """)(element, config.editor_placeholder) === config.message,
                date_matches: !config.scheduled_date || new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10) === config.scheduled_date
            })""", config)
            code = ('editor_detached' if not observed['connected'] else
                    'editor_text_changed' if not observed['text_matches'] else
                    'scheduled_day_expired_before_submit' if not observed['date_matches'] else
                    'submission_context_changed')
            raise DemoError("发送前正文、日期或会话校验未通过，已停止提交。", code=code)
        button_handle = button.element_handle() if button is not None else None
        # Check at the actual input event as well as immediately before it.
        # Browser/UI activity between the ledger write and the key/click must
        # not change the recipient, approved body or permitted calendar day.
        editor.evaluate("""(editor, data) => {
            const verify = """ + guard_js + """;
            const eventName = data.button ? 'click' : 'keydown';
            const state = {eventName, guard: null, blocked: false};
            const guard = event => {
                if (data.button ? !data.button.contains(event.target) : event.key !== 'Enter') return;
                if ((!data.button && event.target !== editor) || !verify(editor, data.config)) {
                    state.blocked = true;
                    event.preventDefault(); event.stopImmediatePropagation();
                }
                document.removeEventListener(eventName, guard, true);
            };
            state.guard = guard;
            document.addEventListener(eventName, guard, true);
            editor.__sparkSubmissionGuard = state;
        }""", {'config': config, 'button': button_handle})
        try:
            self.send_stage = 'dispatching_submission'
            if button is not None:
                button.click(timeout=5000)
            else:
                editor.press("Enter", timeout=5000)
            if editor.evaluate("e => e.__sparkSubmissionGuard?.blocked === true"):
                raise DemoError("实际提交事件被保护检查拦截；正文、日期或会话发生变化。", code="submission_guard_blocked")
        finally:
            try:
                editor.evaluate("""e => {
                    const value = e.__sparkSubmissionGuard;
                    if (value) document.removeEventListener(value.eventName, value.guard, true);
                    delete e.__sparkSubmissionGuard;
                }""")
            except PlaywrightError:
                pass
            if button_handle is not None:
                try:
                    button_handle.dispose()
                except PlaywrightError:
                    pass

    def submission_guard_config(self) -> dict:
        if not hasattr(self, '_submission_context'):
            raise DemoError("发送上下文缺失，未提交。", code="invalid_request")
        return {**self._submission_context, 'editor_placeholder': self.selectors.get('editor_placeholder', '')}

    def submission_guard_js(self) -> str:
        return """(editor, config) => editor.isConnected &&
            (""" + EDITOR_TEXT_JS + """)(editor, config.editor_placeholder) === config.message &&
            (!config.scheduled_date || new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10) === config.scheduled_date)
        """

    def before_submit(self, contact: str, editor) -> None:
        """Desktop adapter rechecks the account after potentially long navigation."""

    def send_once(self, contact: str, message: str, request_id: str, ledger: SendLedger,
                  verify_after_reload: bool = True):
        contact = normalize_contact(contact)
        message = normalize_message(message)
        previous = ledger.lookup(request_id, contact, message)
        if previous:
            raise DemoError(f"该请求已存在（{previous}），不能再次发送。",
                            code="already_attempted", submission="possible")
        method = self.selectors.get("send_method", "enter")
        if method not in {"enter", "button"}:
            raise DemoError("send_method 仅支持 enter 或 button。", code="page_changed", scope="account")
        self.send_stage = 'selecting_recipient'
        editor = self.select_contact(contact)
        self.verify_recipient(contact, editor)
        # Do not overwrite a draft the user may have left in this browser profile.
        self.send_stage = 'validating_draft'
        if editor_has_draft(editor):
            raise DemoError("输入框已有草稿，请用户自行处理草稿后再试；未发送。", code="existing_draft")
        button = None
        if method == "button":
            button = visible_one(self.page.locator(self.required("send_button")), "发送按钮")
        baseline = self.message_snapshot()
        original_editor = editor.element_handle()
        filled = False
        submission_possible = False
        try:
            self.send_stage = 'filling_message'
            filled = True
            editor.fill(message)
            self.verify_recipient(contact, editor)
            self.send_stage = 'rechecking_account'
            self.before_submit(contact, editor)
            self._submission_context = {
                'message': message,
                'scheduled_date': getattr(ledger, 'scheduled_date', None),
            }
            self.send_stage = 'recording_submission'
            ledger.begin(request_id, contact, message)
            submission_possible = True
            # After this point every exception is uncertain: never retry automatically.
            self.submit(editor, button)
            self.send_stage = 'checking_message'
            can_correlate = baseline is not None and baseline["identified"]
            deadline = time.monotonic() + self.EVIDENCE_TIMEOUT
            while time.monotonic() < deadline:
                current = self.message_snapshot()
                candidate = self._new_outgoing_id(baseline, current, message)
                cleared = not editor_has_draft(editor)
                if cleared and (candidate is not None or not can_correlate):
                    if verify_after_reload:
                        self.page.wait_for_timeout(self.RELOAD_SETTLE_MS)
                        # Optimistic IDs can be replaced by the server-assigned ID.
                        # Correlate the settled outgoing record before reloading.
                        candidate = self._new_outgoing_id(baseline, self.message_snapshot(), message)
                        self.send_stage = 'reloading_chat'
                        self.page.reload(wait_until="domcontentloaded", timeout=60000)
                        self.wait_ready(contact, 45)
                        self.select_contact(contact)
                        self.send_stage = 'verifying_persistence'
                        if not can_correlate:
                            after = self.message_snapshot()
                            own_visible = after is not None and message in after["visible_outgoing_texts"]
                            detail = ("已执行一次发送并刷新页面；可见我方同文消息，但页面缺少可靠的稳定消息标识，"
                                      "无法唯一关联本次发送，请在手机端核对。" if own_visible else
                                      "已执行一次发送并刷新页面，但没有获得可唯一关联的我方消息证据，请在手机端核对。")
                            raise DemoError(detail, code="message_unverified", submission="possible")
                        verify_deadline = time.monotonic() + self.EVIDENCE_TIMEOUT
                        while time.monotonic() < verify_deadline:
                            after = self.message_snapshot()
                            if (candidate is not None and after is not None and after["identified"]
                                    and after["outgoing"].get(candidate) == message):
                                ledger.finish(request_id, "persisted_in_chat")
                                detail = "已核对同一条新增我方消息的稳定标识及刷新后记录；这不是服务端送达回执。"
                                return {
                                    "status": "persisted_in_chat", "request_id": request_id,
                                    "meaning": detail, "detail": detail,
                                }
                            self.page.wait_for_timeout(250)
                        raise DemoError("刷新后未能核对同一条我方消息，结果待核对，没有重发。",
                                        code="message_unverified", submission="possible")
                    if candidate is None:
                        raise DemoError("已执行一次发送，但页面缺少可唯一关联的我方消息证据，请在手机端核对。",
                                        code="message_unverified", submission="possible")
                    ledger.finish(request_id, "visible_in_chat")
                    detail = "页面出现可唯一关联的新增我方消息；尚未刷新核对，也不是服务端送达证明。"
                    return {
                        "status": "visible_in_chat", "request_id": request_id,
                        "meaning": detail, "detail": detail,
                    }
                self.page.wait_for_timeout(200)
            raise DemoError("发送后的页面证据不足，结果待核对；没有自动重试。",
                            code="message_unverified", submission="possible")
        except Exception as exc:
            if isinstance(exc, DemoError):
                exc.stage = self.send_stage
                exc.cause_type = type(exc).__name__
            if submission_possible:
                if isinstance(exc, DemoError):
                    exc.submission = "possible"
                    raise
                uncertain = DemoError("发送操作后页面或记录异常，结果待核对；请勿换请求编号直接重发。",
                                      code="submission_uncertain", submission="possible")
                uncertain.stage = self.send_stage
                uncertain.cause_type = type(exc).__name__
                raise uncertain from exc
            if filled and not self._clear_owned_draft(contact, message, original_editor):
                detail = "本次未提交消息，但无法安全清除程序留下的文字；请打开对应聊天核对草稿后再继续。"
                if isinstance(exc, DemoError):
                    exc.args = (detail,)
                    exc.code = "draft_cleanup_required"
                    raise
                raise DemoError(detail, code="draft_cleanup_required") from exc
            raise
        finally:
            if original_editor is not None:
                try:
                    original_editor.dispose()
                except PlaywrightError:
                    # Browser closure must not hide the primary outcome.
                    pass


def inspect_page(page, contact: str | None):
    """Export element metadata, without cookie values or other message text."""
    return page.evaluate("""contact => {
        const info = e => ({tag:e.tagName, id:e.id, cls:e.className,
            role:e.getAttribute('role'), testid:e.getAttribute('data-testid'),
            placeholder:e.getAttribute('placeholder'),
            rect:{x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y,
                width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}});
        const visible = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
        return {url:location.href,
            editors:[...document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]')]
                .filter(visible).map(info),
            matches:contact ? [...document.querySelectorAll('*')]
                .filter(e=>visible(e)&&e.children.length===0&&e.textContent.trim()===contact)
                .slice(0,10).map(e=>({element:info(e),ancestors:[e.parentElement,
                    e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement,
                    e.parentElement?.parentElement?.parentElement?.parentElement]
                    .filter(Boolean).map(info)})) : []};
    }""", contact)


def main():
    parser = argparse.ArgumentParser(description="抖音登录持久化与单条消息 demo")
    parser.add_argument("command", choices=["login", "check", "inspect", "send"])
    parser.add_argument("--account", default="demo", help="独立浏览器目录标识")
    parser.add_argument("--contact", required=True, help="聊天列表显示的准确备注")
    parser.add_argument("--message", help="发送的单行文字，仅 send 使用")
    parser.add_argument("--request-id", help="一次发送的唯一编号，复用编号不会重发")
    parser.add_argument("--selectors", type=Path, default=ROOT / "selectors.json")
    parser.add_argument("--channel", choices=["chrome", "msedge", "chromium"], default="chrome")
    parser.add_argument("--timeout", type=int, default=45, help="等待会话出现的秒数；login 至少 600 秒")
    parser.add_argument("--headless", action="store_true", help="check/send 可用于验证服务器环境")
    parser.add_argument("--allow-send", action="store_true", help="明确执行发送；不传则只定位会话")
    args = parser.parse_args()
    if args.command == "send" and (not args.message or not args.request_id):
        parser.error("send 必须提供 --message 和 --request-id")
    if args.command == "login" and args.headless:
        parser.error("扫码登录需可见浏览器，不支持 login --headless")
    if args.timeout < 1:
        parser.error("--timeout 必须大于零")
    path = account_dir(args.account)
    selectors = read_selectors(args.selectors)
    profile = ROOT / ".local" / "profiles" / args.account
    profile.mkdir(parents=True, exist_ok=True)
    with profile_lock(path), sync_playwright() as p:
        options = {} if args.channel == "chromium" else {"channel": args.channel}
        context = p.chromium.launch_persistent_context(
            str(profile), headless=args.headless, viewport={"width":1280,"height":900}, **options
        )
        ledger = SendLedger(path / "sends.sqlite3")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(5000)
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            ui = ChatUI(page, selectors)
            print("正在读取保存的浏览器会话。" if args.headless else
                  "已打开独立浏览器；若未登录，请在官方页面完成扫码。", flush=True)
            if args.command == "inspect":
                page.wait_for_timeout(3000)
                result = inspect_page(page, args.contact)
                (path / "page-structure.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            ui.wait_ready(args.contact, max(600, args.timeout) if args.command == "login" else args.timeout)
            if args.command in {"login", "check"}:
                result = {"status":"chat_ready", "profile":str(profile),
                    "meaning":"登录后指定会话可见；关闭后可用 check 验证重启复用。"}
                if args.command == "check" and args.message:
                    ui.select_contact(args.contact)
                    text = page.locator(ui.required("message_text")).filter(
                        has_text=re.compile("^" + re.escape(args.message) + "$")
                    )
                    text.first.wait_for(timeout=15000)
                    count = ui.exact_message_count(args.message)
                    result.update(status="message_visible_after_relaunch", matching_messages=count,
                                  meaning="浏览器重启后聊天记录仍能读到指定文字；此命令不会发送。")
                    (path / "verification.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    text.last.screenshot(path=str(path / "verified-message.png"))
                print(json.dumps(result,ensure_ascii=False))
            elif args.command == "send" and not args.allow_send:
                ui.select_contact(args.contact)
                print("已定位并核对目标会话。未传 --allow-send，因此没有填写或发送消息。")
            else:
                result = ui.send_once(args.contact, args.message, args.request_id, ledger)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            ledger.close()
            context.close()


if __name__ == "__main__":
    try:
        main()
    except (DemoError, PlaywrightError, FileNotFoundError, ValueError) as exc:
        print(f"停止：{exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    except KeyboardInterrupt:
        print("用户中止；发送途中中止时请先核对聊天记录，不要直接重发。",file=sys.stderr)
        sys.exit(130)

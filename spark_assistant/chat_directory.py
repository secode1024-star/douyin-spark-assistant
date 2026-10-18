"""Read only official existing-chat metadata; route sends by observed identities.

React property paths below were verified on the official desktop chat page.
They are implementation details, not a public API. Missing or changed evidence
fails closed. No credentials, message history or private API requests are read.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit
from playwright.sync_api import Error as PlaywrightError
from douyin_demo import CHAT_URL, ChatUI, DemoError, visible_one

# Bound traversal to ancestors of observed DOM nodes, never the application store.
MODEL_JS = r"""
const model = (node, field) => {
    const key = Object.keys(node || {}).find(k => k.startsWith('__reactFiber$'));
    for (let fiber = node?.[key], i = 0; fiber && i < 14; fiber = fiber.return, i++) {
        const value = fiber.memoizedProps?.[field];
        if (value) return value;
    }
    return null;
};
const identity = c => c ? {
    conversation_id: typeof c.id === 'string' ? c.id : '',
    platform_id: typeof c.toParticipantSecUserId === 'string' ? c.toParticipantSecUserId : '',
    type: c.type, participant_count: c.participantCount
} : null;
"""
ROWS_JS = """(nodes, nameSelector) => {""" + MODEL_JS + """
return nodes.map((node, index) => ({index, ...identity(model(node, 'conversation')),
    display_name: node.querySelector(nameSelector)?.innerText?.trim() || ''}));
}"""
VERIFY_JS = """(editor, config) => {""" + MODEL_JS + """
const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
const headers = [...document.querySelectorAll(config.header)].filter(visible);
const allEditors = [...document.querySelectorAll(config.editor)].filter(visible);
const h = headers.length === 1 ? headers[0] : null;
const hId = identity(model(h, 'curConversation'));
const eId = identity(model(editor, 'curConversation'));
const same = value => value && value.conversation_id === config.conversation_id &&
    value.platform_id === config.platform_id && value.type === 1 && value.participant_count === 2;
return !!(h && editor.isConnected && editor.isContentEditable &&
    allEditors.length === 1 && allEditors[0] === editor &&
    h.closest(config.panel)?.contains(editor) && same(hId) && same(eId));
}"""


def valid_id(value: object) -> bool:
    return isinstance(value, str) and value != 'self' and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value))


def canonical_account(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'www.douyin.com' or parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(r'/user/([A-Za-z0-9_-]{1,160})', parsed.path)
    return match[1] if match and valid_id(match[1]) else None


def classify(row: dict) -> dict:
    peer = row.get('platform_id', '')
    conversation = row.get('conversation_id', '')
    stable = valid_id(peer) and isinstance(conversation, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,240}', conversation))
    kind = ('one_to_one' if row.get('type') == 1 and row.get('participant_count') == 2
            else 'group' if row.get('type') == 2 else 'unknown')
    ready = stable and kind == 'one_to_one' and bool(row.get('display_name'))
    return {'platform_id': peer, 'conversation_id': conversation,
            'profile_url': f'https://www.douyin.com/user/{peer}' if valid_id(peer) else '',
            'display_name': row.get('display_name', ''), 'conversation_type': kind,
            'source': 'official_chat_ui', 'relationship': 'existing_chat',
            'sync_state': 'current', 'identity_verified': bool(stable),
            'dm_verified': bool(ready), 'sendable': bool(ready),
            'blocked_reason': '' if ready else 'group_chat' if kind == 'group' else 'recipient_identity_unverified'}


class ChatDirectory:
    MAX_SCROLLS = 60
    SCROLL_WAIT_MS = 350

    def __init__(self, page, selectors: dict):
        self.page, self.selectors = page, selectors
        self.progress = lambda message: None

    def read_account(self, deadline: float | None = None) -> dict:
        """Self's canonical is public identity evidence, not the /user/self alias."""
        deadline = deadline or time.monotonic() + 80
        if deadline <= time.monotonic():
            return {'platform_id': '', 'verification': 'unknown'}
        tab = self.page.context.new_page()
        try:
            tab.goto('https://www.douyin.com/user/self', wait_until='domcontentloaded', timeout=max(1, min(45000, int((deadline-time.monotonic())*1000))))
            deadline = min(deadline, time.monotonic() + 35)
            while time.monotonic() < deadline:
                from spark_assistant.worker import verification_required
                if verification_required(tab):
                    raise DemoError('抖音要求完成验证，请手动打开登录窗口处理后再试。',
                                    code='verification_required', scope='account')
                url = tab.locator('link[rel="canonical"]').first.get_attribute('href') if tab.locator('link[rel="canonical"]').count() else None
                platform_id = canonical_account(url)
                # The route must still be self; another profile is not account proof.
                if platform_id and urlsplit(tab.url).path == '/user/self':
                    return {'platform_id': platform_id, 'verification': 'verified'}
                if tab.is_closed():
                    break
                tab.wait_for_timeout(300)
            return {'platform_id': '', 'verification': 'unknown'}
        finally:
            if not tab.is_closed():
                tab.close()

    def rows(self) -> list[dict]:
        return self.page.locator(self.selectors['conversation_row']).evaluate_all(ROWS_JS, self.selectors['conversation_name'])

    def container(self):
        return visible_one(self.page.locator(self.selectors['conversation_list']), '会话列表')

    def scan(self, deadline: float | None = None) -> tuple[list[dict], bool, str]:
        deadline = deadline or time.monotonic() + 220
        gathered, stable_bottom = {}, 0
        reason = 'scroll_limit_reached'
        try:
            container = self.container()
            container.evaluate('e => { e.scrollTop = 0; }')
            self.page.wait_for_timeout(self.SCROLL_WAIT_MS)
            for _ in range(self.MAX_SCROLLS):
                if time.monotonic() >= deadline:
                    reason = 'sync_time_limit'
                    break
                rows = self.rows()
                before = len(gathered)
                for row in rows:
                    key = row.get('conversation_id')
                    if key:
                        old = gathered.get(key)
                        if old and old.get('platform_id') != row.get('platform_id'):
                            raise DemoError('会话身份发生变化，已停止读取，请重新刷新。', code='recipient_identity_unverified')
                        gathered[key] = row
                if len(gathered) != before:
                    self.progress(f'已读取 {len(gathered)} 条已有聊天会话，正在继续核验。')
                metrics = container.evaluate('e => ({top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight})')
                bottom = metrics['top'] + metrics['height'] >= metrics['total'] - 2
                stable_bottom = stable_bottom + 1 if bottom and len(gathered) == before else 0
                if stable_bottom >= 3:
                    reason = 'loaded_existing_chats'
                    break
                container.evaluate('e => { e.scrollTop += Math.max(1, e.clientHeight * 0.8); }')
                self.page.wait_for_timeout(self.SCROLL_WAIT_MS)
        except PlaywrightError:
            reason = 'scan_interrupted'
        return list(gathered.values()), stable_bottom >= 3, reason

    def sync(self) -> dict:
        deadline = time.monotonic() + 300
        account = self.read_account(deadline=deadline)
        coverage = {'directory_kind': 'existing_chats', 'status': 'partial',
                    'reason_code': 'loaded_existing_chats', 'observed_count': 0}
        if account['verification'] != 'verified':
            return {'account': account, 'friends': [], 'coverage': {**coverage, 'status': 'failed', 'reason_code': 'account_identity_unverified'}}
        try:
            self.page.locator(self.selectors['conversation_list']).first.wait_for(state='visible', timeout=30000)
            rows, reached_bottom, reason = self.scan(deadline=deadline-45)
        except PlaywrightError:
            rows, reached_bottom, reason = [], False, 'chat_directory_unavailable'
        try:
            final_account = self.read_account(deadline=deadline)
        except PlaywrightError:
            final_account = {'platform_id': '', 'verification': 'unknown'}
        if final_account.get('verification') != 'verified' or final_account.get('platform_id') != account['platform_id']:
            code = 'account_identity_mismatch' if final_account.get('verification') == 'verified' else 'account_identity_unverified'
            return {'account': final_account, 'friends': [], 'coverage': {**coverage, 'status': 'failed', 'reason_code': code}}
        friends = [classify(row) for row in rows]
        peers = {}
        for friend in friends:
            peers.setdefault(friend['platform_id'], []).append(friend)
        for matches in peers.values():
            if len(matches) > 1:
                for friend in matches:
                    friend.update(sendable=False, dm_verified=False, blocked_reason='conversation_ambiguous')
        coverage.update(observed_count=len(friends), reached_loaded_bottom=reached_bottom,
                        sendable_count=sum(f['sendable'] for f in friends), reason_code=reason)
        if not rows and reason not in {'loaded_existing_chats', 'scroll_limit_reached'}:
            coverage['status'] = 'failed'
        # A stable bottom proves only the loaded web list, never all friendships.
        return {'account': final_account, 'friends': friends, 'coverage': coverage}

    def identity_ui(self, request: dict):
        return IdentityChatUI(self.page, self.selectors, request)


class IdentityChatUI(ChatUI):
    def __init__(self, page, selectors: dict, request: dict):
        super().__init__(page, selectors)
        self.target = {'platform_id': request['platform_id'], 'conversation_id': request['conversation_id']}
        self.directory = ChatDirectory(page, selectors)
        self.account_id = request.get('account_id', request.get('account_platform_id', ''))

    def contact(self, name: str):
        # The name is display-only. Never pass it to the legacy matching route.
        rows = self.directory.rows()
        matches = [row for row in rows if row.get('conversation_id') == self.target['conversation_id'] and row.get('platform_id') == self.target['platform_id']]
        if len(matches) != 1 or not classify(matches[0])['sendable']:
            raise DemoError('未找到已核验的一对一会话，请先在抖音手动聊天后刷新列表。', code='recipient_identity_unverified')
        candidate = self.page.locator(self.required('conversation_row')).nth(matches[0]['index'])
        current = candidate.evaluate('node => {' + MODEL_JS + "return identity(model(node, 'conversation'));}")
        if not current or any(current.get(key) != value for key, value in self.target.items()):
            raise DemoError('列表发生变化，已停止定位；未发送。', code='recipient_identity_unverified')
        return candidate

    def ready(self, contact: str) -> bool:
        try:
            self.directory.container()
            return True
        except (DemoError, PlaywrightError):
            return False

    def select_contact(self, name: str):
        container = self.directory.container()
        container.evaluate('e => { e.scrollTop = 0; }')
        self.page.wait_for_timeout(self.directory.SCROLL_WAIT_MS)
        stable_bottom = 0
        for _ in range(self.directory.MAX_SCROLLS):
            try:
                candidate = self.contact(name)
            except DemoError:
                candidate = None
            if candidate is not None:
                candidate.click(timeout=5000)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        editor = visible_one(self.page.locator(self.required('editor')), '消息输入框')
                        self.verify_recipient(name, editor)
                        return editor
                    except (DemoError, PlaywrightError):
                        self.page.wait_for_timeout(200)
                raise DemoError('当前会话与所选账号身份不一致，或无法输入消息；未发送。', code='recipient_identity_unverified')
            metrics = container.evaluate('e => ({top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight})')
            bottom = metrics['top'] + metrics['height'] >= metrics['total'] - 2
            stable_bottom = stable_bottom + 1 if bottom else 0
            if stable_bottom >= 3:
                break
            container.evaluate('e => { e.scrollTop += Math.max(1, e.clientHeight * 0.8); }')
            self.page.wait_for_timeout(self.directory.SCROLL_WAIT_MS)
        raise DemoError('当前网页没有找到此会话，请在抖音手动聊天后刷新列表。', code='recipient_identity_unverified')

    def _verification_config(self):
        return {**self.target, 'header': self.required('conversation_header'),
                'editor': self.required('editor'), 'panel': self.required('conversation_panel')}

    def verify_recipient(self, contact: str, editor) -> None:
        if not editor.evaluate(VERIFY_JS, self._verification_config()):
            raise DemoError('当前聊天身份无法核验或已切换，未发送。', code='recipient_identity_unverified')

    def before_submit(self, contact: str, editor) -> None:
        if not valid_id(self.account_id):
            raise DemoError('任务缺少稳定账号绑定，未发送。', code='account_identity_unverified', scope='account')
        current = self.directory.read_account()
        if current.get('verification') != 'verified':
            raise DemoError('发送前无法再次核验当前抖音账号，未发送。', code='account_identity_unverified', scope='account')
        if current.get('platform_id') != self.account_id:
            raise DemoError('当前登录账号已变化，未发送。请刷新聊天列表。', code='account_identity_mismatch', scope='account')
        self.verify_recipient(contact, editor)

    def submission_guard_config(self) -> dict:
        return {**super().submission_guard_config(), **self._verification_config()}

    def draft_guard_config(self, message: str) -> dict:
        return {**super().draft_guard_config(message), **self._verification_config()}

    def submission_guard_js(self) -> str:
        return '(editor, config) => (' + super().submission_guard_js() + ')(editor, config) && (' + VERIFY_JS + ')(editor, config)'

    def submit(self, editor, button) -> None:
        self.send_stage = 'validating_submission'
        self.verify_recipient('', editor)
        if button is not None:
            raise DemoError('身份绑定发送仅支持已验证的回车路径。', code='recipient_identity_unverified')
        super().submit(editor, button)

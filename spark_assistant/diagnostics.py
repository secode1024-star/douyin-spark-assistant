"""Offline acceptance check for source and frozen browser distributions.

Only a loopback fixture and disposable profile are used. This module never
opens Douyin, reads a real browser profile, or sends a message to a person.
"""
from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from douyin_demo import ChatUI, DemoError, SendLedger
from spark_assistant.paths import configure_browser
from spark_assistant.paths import resource_path
from spark_assistant.build_info import packaged_build_info
from spark_assistant import __version__
from spark_assistant.chat_directory import IdentityChatUI
from spark_assistant.worker import _error_result, login_prompt_visible, wait_for_account, wait_for_login_window


FIXTURE = b'''<!doctype html><meta charset="utf-8">
<aside id="contacts"><button onclick="document.querySelector('#title').textContent='955'">
<span class="name">955</span></button></aside>
<h2 id="title"></h2><section id="messages"></section><textarea id="editor"></textarea>
<script>
const messages=JSON.parse(localStorage.getItem('smoke-messages')||'[]');
function render(){document.querySelector('#messages').replaceChildren();for(const [index,text] of messages.entries()){
const el=document.createElement('p');el.className='message outgoing';el.dataset.messageId='fixture-'+index;
const body=document.createElement('span');body.className='text';body.textContent=text;el.append(body);
document.querySelector('#messages').append(el);}}
render();document.querySelector('#editor').addEventListener('keydown',event=>{
if(event.key==='Enter'){event.preventDefault();messages.push(event.target.value);
localStorage.setItem('smoke-messages',JSON.stringify(messages));event.target.value='';render();}});
</script>'''

SELECTORS = {
    "conversation_list": "#contacts", "conversation_name": ".name",
    "conversation_header": "#title", "editor": "#editor",
    "message_text": "#messages .text", "send_method": "enter",
    "message_container": "#messages .message", "outgoing_message": ".outgoing",
    "message_text_in_container": ".text", "message_id_attribute": "data-message-id",
}

IDENTITY_FIXTURE = b'''<!doctype html><meta charset="utf-8">
<aside class="conversationConversationListwrapper">
<button class="conversationConversationItemwrapper" onclick="selectChat(0)"><span class="conversationConversationItemtitle">955</span></button>
<button class="conversationConversationItemwrapper" onclick="selectChat(1)"><span class="conversationConversationItemtitle">955</span></button>
</aside><main class="componentsRightPanelwrapper">
<h2 class="RightPanelHeadertitle"></h2><section class="messageMessageListlist"></section>
<div class="messageEditorinputArea" contenteditable="true"></div></main>
<script>
const conversations=[
{id:'fixture:other',toParticipantSecUserId:'fixture_other',type:1,participantCount:2},
{id:'fixture:target',toParticipantSecUserId:'fixture_target',type:1,participantCount:2}];
const editor=document.querySelector('[contenteditable]');
const header=document.querySelector('h2');
let current=null;
document.querySelectorAll('.conversationConversationItemwrapper').forEach((row,index)=>{
row.__reactFiber$fixture={memoizedProps:{conversation:conversations[index]}};});
const messages=JSON.parse(localStorage.getItem('identity-messages')||'[]');
function render(){const root=document.querySelector('section');root.replaceChildren();
for(const [index,item] of messages.entries()){
if(item.peer!==current?.toParticipantSecUserId)continue;
const box=document.createElement('div');box.className='messageMessageBoxmessageBox';box.dataset.messageId='identity-'+index;
const mine=document.createElement('div');mine.className='messageMessageBoxisFromMe';
const text=document.createElement('p');text.className='TextMessageTextpureText';text.textContent=item.text;
mine.append(text);box.append(mine);root.append(box);}}
function selectChat(index){current=conversations[index];header.textContent='955';
header.__reactFiber$fixture={memoizedProps:{curConversation:current}};
editor.__reactFiber$fixture={memoizedProps:{curConversation:localStorage.getItem('identity-mismatch')?conversations[0]:current}};render();}
editor.addEventListener('keydown',event=>{if(event.key==='Enter'){
event.preventDefault();messages.push({peer:current.toParticipantSecUserId,text:editor.innerText});
localStorage.setItem('identity-messages',JSON.stringify(messages));editor.innerText='';render();}});
</script>'''


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(IDENTITY_FIXTURE if self.path == "/identity" else FIXTURE)

    def log_message(self, *_args):
        pass


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def _identity_routing_smoke(page, url: str, selectors: dict, ledger: SendLedger) -> None:
    """Exercise the packaged production adapter on synthetic observed node shapes."""
    page.goto(url + "/identity", wait_until="domcontentloaded")
    request = {"platform_id": "fixture_target", "conversation_id": "fixture:target",
               "account_id": "fixture_account", "account_epoch": 1}
    old_identity = ledger.identity
    ledger.identity = request
    try:
        # Stable message identifiers exist only in this loopback fixture.
        ui = IdentityChatUI(page, {**selectors, "message_id_attribute": "data-message-id"}, request)
        # Account canonical discovery has separate adapter tests. This diagnostic
        # is strictly loopback-only, including the final account recheck hook.
        ui.directory.read_account = lambda: {"platform_id": "fixture_account", "verification": "verified"}
        outcome = ui.send_once("955", "identity-fixture", "identity-send", ledger)
        _require(outcome["status"] == "persisted_in_chat", "identity_routing_verified")
        _require(page.evaluate("JSON.parse(localStorage.getItem('identity-messages')).every(m=>m.peer==='fixture_target')"),
                 "identity_routing_same_name_sibling")
        page.evaluate("localStorage.setItem('identity-mismatch','1')")
        blocked = False
        try:
            ui.send_once("955", "must-not-send", "identity-mismatch", ledger)
        except DemoError as exc:
            blocked = exc.code == "recipient_identity_unverified"
        _require(blocked and ledger.get("identity-mismatch") is None
                 and page.locator(selectors["editor"]).inner_text() == ""
                 and page.evaluate("JSON.parse(localStorage.getItem('identity-messages')).length") == 1,
                 "identity_mismatch_before_fill")
    finally:
        ledger.identity = old_identity


def _official_editor_placeholder_smoke(page, url: str, selectors: dict) -> None:
    """Exercise the observed official rich-editor leaf using packaged selectors."""
    page.evaluate("localStorage.removeItem('identity-mismatch')")
    page.goto(url + '/identity', wait_until='domcontentloaded')
    ui = IdentityChatUI(page, selectors, {
        'platform_id': 'fixture_target', 'conversation_id': 'fixture:target',
        'account_id': 'fixture_account', 'account_epoch': 1,
    })
    editor = ui.select_contact('955')
    for message in ('fixture-text', 'fixture\u200btext', 'fixture-text\u200b'):
        editor.evaluate("""(e, text) => {
            e.replaceChildren(); const line = document.createElement('div'); line.className = 'ace-line';
            const body = document.createElement('span');
            body.dataset.string = 'true'; body.dataset.leaf = 'true'; body.textContent = text;
            const end = document.createElement('span');
            end.dataset.string = 'true'; end.dataset.leaf = 'true'; end.dataset.enter = 'true'; end.textContent = '\u200b';
            line.append(body, end); e.append(line);
        }""", message)
        ui._submission_context = {'message': message, 'scheduled_date': None}
        _require(ui.editor_text(editor) == message and
                 editor.evaluate(ui.submission_guard_js(), ui.submission_guard_config()),
                 'official_placeholder_guard')
        handle = editor.element_handle()
        try:
            _require(ui._clear_owned_draft('955', message, handle), 'official_placeholder_owned_cleanup')
        finally:
            handle.dispose()
    editor.evaluate("e => { e.innerText = 'fixture-text\u200b'; }")
    ui._submission_context = {'message': 'fixture-text', 'scheduled_date': None}
    _require(not editor.evaluate(ui.submission_guard_js(), ui.submission_guard_config()),
             'unmarked_body_zero_width_preserved')


def browser_smoke(output: Path) -> int:
    """Return 0 only if the bundled browser and persistent send logic pass.

    Report contains check results and browser version, never cookie values,
    message contents, profile paths, or real account information.
    """
    report: dict = {"status": "failed", "network": "loopback-only", "version": __version__,
                   "build": packaged_build_info()}
    server = None
    thread = None
    try:
        configure_browser()
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory(prefix="spark-browser-smoke-") as folder:
            profile = Path(folder) / "profile"
            ledger = SendLedger(Path(folder) / "sends.sqlite3")
            failed_ledger = SendLedger(Path(folder) / "fault.sqlite3")
            failed_ledger.begin("storage-fault", "fixture", "fixture-only")
            failed_ledger.close()
            failure = _error_result(PlaywrightError("offline fixture"), failed_ledger, "storage-fault",
                                    account_validated=True)
            _require(failure["status"] == "uncertain" and failure["submission"] == "possible",
                     "submission_storage_fault")
            report["submission_storage_fault"] = "passed"
            try:
                with sync_playwright() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), channel="chromium", headless=True,
                    )
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.set_default_timeout(5000)
                        # Exercise compiled login code and production contact selectors.
                        for label in ("登录 / 注册", "登录/注册"):
                            page.set_content(f"<button>{label}</button>")
                            _require(login_prompt_visible(page), "login_button_variants")
                        report["login_button_variants"] = "passed"
                        production_selectors = json.loads(resource_path("selectors.json").read_text(encoding="utf-8"))
                        page.evaluate("""() => setTimeout(() => {
                            document.body.innerHTML='<aside class="conversationConversationListwrapper"><span class="conversationConversationItemtitle">Fixture</span></aside>';
                        }, 200)""")
                        ready = wait_for_account(page, production_selectors, login=False, timeout=3)
                        _require(ready["status"] == "ready", "login_restore_wait")
                        report["login_restore_wait"] = "passed"
                        other = context.new_page()
                        page.close()
                        _require(wait_for_login_window(context, timeout=0.05)["status"] != "login_closed",
                                 "login_multiple_tabs")
                        report["login_multiple_tabs"] = "passed"
                        page = other
                        page.goto(url, wait_until="domcontentloaded")
                        report["browser_version"] = context.browser.version if context.browser else "unknown"
                        context.add_cookies([{
                            "name": "smoke-session", "value": "fixture-only", "url": url,
                            "httpOnly": True, "expires": 4102444800,
                        }])
                        ui = ChatUI(page, SELECTORS)
                        outcome = ui.send_once("955", "offline-fixture", "smoke-send", ledger)
                        _require(outcome["status"] == "persisted_in_chat", "send_reload_evidence")
                        report["send_after_reload"] = "persisted_in_chat"
                    finally:
                        context.close()
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), channel="chromium", headless=True,
                    )
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        page.set_default_timeout(5000)
                        page.goto(url, wait_until="domcontentloaded")
                        _require(any(cookie["name"] == "smoke-session" and cookie["value"] == "fixture-only"
                                     for cookie in context.cookies()), "cookie_after_relaunch")
                        report["session_after_relaunch"] = "persisted"
                        ui = ChatUI(page, SELECTORS)
                        ui.select_contact("955")
                        _require(ui.exact_message_count("offline-fixture") == 1, "message_after_relaunch")
                        report["message_after_relaunch"] = "persisted"
                        blocked = False
                        try:
                            ui.send_once("955", "offline-fixture", "smoke-send", ledger)
                        except DemoError:
                            blocked = True
                        _require(blocked and ui.exact_message_count("offline-fixture") == 1, "duplicate_request_blocked")
                        report["duplicate_request"] = "blocked"
                        # An incoming echo can never prove this app sent a message.
                        page.evaluate("""() => {
                            localStorage.removeItem('smoke-messages');
                            document.querySelector('#messages').replaceChildren();
                            const old=document.querySelector('#editor');
                            const editor=old.cloneNode(); old.replaceWith(editor);
                            editor.addEventListener('keydown', event => {
                              if(event.key==='Enter'){
                                event.preventDefault();
                                const root=document.createElement('p');root.className='message incoming';
                                root.dataset.messageId='incoming-only';
                                const body=document.createElement('span');body.className='text';
                                body.textContent=editor.value;root.append(body);
                                document.querySelector('#messages').append(root);editor.value='';
                              }
                            });
                        }""")
                        rejected = False
                        try:
                            ChatUI(page, SELECTORS).send_once("955", "incoming-fixture", "incoming-only", ledger)
                        except DemoError:
                            rejected = True
                        _require(rejected and ledger.get("incoming-only") == "uncertain", "incoming_cannot_confirm_send")
                        report["incoming_cannot_confirm_send"] = "passed"
                        page.goto(url, wait_until="domcontentloaded")
                        unverified = False
                        try:
                            ChatUI(page, {**SELECTORS, "message_id_attribute": ""}).send_once(
                                "955", "missing-id-fixture", "missing-id", ledger)
                        except DemoError as exc:
                            unverified = getattr(exc, "code", "") == "message_unverified"
                        _require(unverified and ledger.get("missing-id") == "uncertain"
                                 and ChatUI(page, SELECTORS).exact_message_count("missing-id-fixture") == 1,
                                 "missing_id_stays_uncertain")
                        report["missing_id_stays_uncertain"] = "passed"
                        _identity_routing_smoke(page, url, production_selectors, ledger)
                        report["identity_routing"] = True
                        report["identity_mismatch_before_fill"] = True
                        _official_editor_placeholder_smoke(page, url, production_selectors)
                        report['official_editor_placeholder'] = True
                    finally:
                        context.close()
            finally:
                ledger.close()
        report["status"] = "passed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report["failed_check"] = str(exc)
        else:
            report["failed_check"] = "browser_runtime_or_fixture"
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1

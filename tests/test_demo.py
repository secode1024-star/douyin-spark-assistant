"""Offline browser regressions; all recipients and messages are local fixtures."""
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Error as PlaywrightError, sync_playwright
from douyin_demo import (ChatUI, DemoError, SendLedger, account_dir, profile_lock,
                        editor_has_draft, normalize_message)
from spark_assistant.paths import configure_browser

HTML = b'''<!doctype html><meta charset="utf-8">
<style>.text{white-space:pre-wrap}</style>
<aside id="contacts"><button onclick="document.querySelector('#title').textContent='955'">
<span class="name">955</span></button></aside>
<main><h2 id="title"></h2><section id="messages"></section><textarea id="editor"></textarea></main>
<script>
const options=new URLSearchParams(location.search), mode=options.get('mode')||'normal';
const persist=options.get('persist')==='1';
let rows=JSON.parse(localStorage.getItem('rows')||'[]');
let serial=Number(localStorage.getItem('serial')||0);
if(!rows.length && ['virtual','history'].includes(mode))
  rows=[{id:'old-history',text:'hello',outgoing:true}];
function render(){
 const target=document.querySelector('#messages');target.replaceChildren();
 for(const row of rows){
  const node=document.createElement('p');node.className='message '+(row.outgoing?'outgoing':'incoming');
  if(row.state)node.classList.add(row.state);
  node.dataset.messageId=row.id;node.dataset.index=String(target.children.length);
  const text=document.createElement('span');text.className='text';text.textContent=row.text;
  node.append(text);target.append(node);
 }
}
render();
document.querySelector('#editor').addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;e.preventDefault();
 sessionStorage.setItem('submissions',String(Number(sessionStorage.getItem('submissions')||0)+1));
 const text=e.target.value;
 const row={id:'fixture-'+(++serial),text,outgoing:mode!=='incoming',state:
     ['failed','pending'].includes(mode)?mode:''};
 if(mode==='virtual')rows=[row];
 else if(mode!=='history')rows.push(row);
 if(mode==='duplicate')rows.push({...row,id:'fixture-'+(++serial)});
 render();e.target.value='';
 if(persist){localStorage.setItem('rows',JSON.stringify(rows));localStorage.setItem('serial',String(serial));}
});
</script>'''

SELECTORS = {
    'conversation_list': '#contacts', 'conversation_name': '.name',
    'conversation_header': '#title', 'editor': '#editor',
    'message_text': '#messages .text', 'send_method': 'enter',
    'message_container': '#messages .message', 'outgoing_message': '.outgoing',
    'message_text_in_container': '.text', 'message_id_attribute': 'data-message-id',
    'message_failed': '.failed', 'message_pending': '.pending',
}


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *args):
        pass


class LedgerTests(unittest.TestCase):
    def test_path_rejects_traversal_and_windows_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('../other', 'a/b', 'CON', 'NUL', '', 'x'*41):
                with self.assertRaises(DemoError):
                    account_dir(name, Path(folder))

    def test_uncertain_send_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.sqlite3'
            ledger = SendLedger(path)
            ledger.begin('one', '955', 'hello')
            ledger.close()
            ledger = SendLedger(path)
            try:
                with self.assertRaises(DemoError) as error:
                    ledger.begin('one', '955', 'hello')
                self.assertEqual('already_attempted', error.exception.code)
                self.assertEqual('uncertain', ledger.lookup('one', '955', 'hello'))
            finally:
                ledger.close()

    def test_payload_conflict_never_reuses_or_overwrites_success(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SendLedger(Path(folder) / 'ledger.sqlite3')
            try:
                ledger.begin('one', '955', 'hello')
                ledger.finish('one', 'persisted_in_chat')
                for recipient, message in [('other', 'hello'), ('955', 'different'), ('955', ' hello ')]:
                    with self.subTest(recipient=recipient, message=message):
                        with self.assertRaises(DemoError) as error:
                            ledger.lookup('one', recipient, message)
                        self.assertEqual('idempotency_conflict', error.exception.code)
                with self.assertRaises(DemoError) as error:
                    ledger.begin('one', 'other', 'hello')
                self.assertEqual('idempotency_conflict', error.exception.code)
                self.assertEqual('persisted_in_chat', ledger.lookup('one', '955', 'hello'))
            finally:
                ledger.close()

    def test_phase_remains_available_after_finish_and_close(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SendLedger(Path(folder) / 'ledger.sqlite3')
            self.assertFalse(ledger.submission_started)
            ledger.begin('one', '955', 'hello')
            self.assertTrue(ledger.submission_started)
            ledger.finish('one', 'persisted_in_chat')
            ledger.close()
            self.assertTrue(ledger.submission_started)

    def test_failed_begin_does_not_mark_submission_started(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SendLedger(Path(folder) / 'ledger.sqlite3')
            ledger.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                ledger.begin('one', '955', 'hello')
            self.assertFalse(ledger.submission_started)

    def test_text_validation_preserves_exact_valid_payload(self):
        for value in (' hello ', '\ttext\t', '\u200btext\ufeff', '你好', 'x'*500):
            self.assertEqual(value, normalize_message(value))
        for value in ('', '  ', '\u200b\ufeff ', 'x\ny', 'x\ry', 'x'*501, None):
            with self.subTest(value=value):
                with self.assertRaises(DemoError) as error:
                    normalize_message(value)
                self.assertEqual('invalid_message', error.exception.code)

    def test_lock_released_after_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with self.assertRaises(RuntimeError):
                with profile_lock(path):
                    raise RuntimeError('test')
            with profile_lock(path):
                pass


@unittest.skipUnless(os.environ.get('SPARK_BROWSER_INTEGRATION') == '1',
                     'Set SPARK_BROWSER_INTEGRATION=1 to run offline Chromium fixtures')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_browser()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='chromium', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.goto(self.url)
        self.ui = ChatUI(self.page, dict(SELECTORS))
        self.ui.EVIDENCE_TIMEOUT = .6
        self.ui.RELOAD_SETTLE_MS = 40
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = SendLedger(Path(self.temp.name) / 'ledger.sqlite3')

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()
        self.context.close()

    def mode(self, mode='normal', persist=True):
        self.page.goto(f'{self.url}?mode={mode}&persist={int(persist)}')

    def submissions(self):
        return self.page.evaluate("Number(sessionStorage.getItem('submissions')||0)")

    def assert_uncertain(self, request='unknown', message='hello'):
        with self.assertRaises(DemoError) as error:
            self.ui.send_once('955', message, request, self.ledger)
        self.assertEqual('possible', error.exception.submission)
        self.assertEqual('uncertain', self.ledger.get(request))
        self.assertEqual(1, self.submissions())
        with self.assertRaises(DemoError):
            self.ui.send_once('955', message, request, self.ledger)
        self.assertEqual(1, self.submissions())
        return error.exception

    def test_send_once_and_reject_duplicate(self):
        result = self.ui.send_once('955', 'hello', 'one', self.ledger, verify_after_reload=False)
        self.assertEqual('visible_in_chat', result['status'])
        with self.assertRaises(DemoError):
            self.ui.send_once('955', 'hello', 'one', self.ledger)
        self.assertEqual(1, self.page.locator('.outgoing').count())

    def test_same_outgoing_identity_survives_reload(self):
        self.mode()
        result = self.ui.send_once('955', 'hello', 'saved', self.ledger)
        self.assertEqual('persisted_in_chat', result['status'])
        self.assertEqual('persisted_in_chat', self.ledger.get('saved'))
        self.assertEqual(1, self.submissions())
        self.assertEqual('fixture-1', self.page.locator('.outgoing').get_attribute('data-message-id'))

    def test_incoming_same_text_never_counts_as_our_send(self):
        self.mode('incoming')
        self.assert_uncertain()
        self.assertEqual(1, self.page.locator('.incoming').count())
        self.assertEqual(0, self.page.locator('.outgoing').count())

    def test_unchanged_old_outgoing_message_is_not_new_evidence(self):
        self.mode('history')
        self.assert_uncertain()
        self.assertEqual('old-history', self.page.locator('.outgoing').get_attribute('data-message-id'))

    def test_virtual_window_constant_count_uses_identity_instead(self):
        self.mode('virtual')
        self.assertEqual(1, self.page.locator('.outgoing').count())
        result = self.ui.send_once('955', 'hello', 'virtual', self.ledger)
        self.assertEqual('persisted_in_chat', result['status'])
        self.assertEqual(1, self.page.locator('.outgoing').count())
        self.assertEqual('fixture-1', self.page.locator('.outgoing').get_attribute('data-message-id'))

    def test_two_new_same_text_outgoing_messages_are_ambiguous(self):
        self.mode('duplicate')
        self.assert_uncertain()
        self.assertEqual(2, self.page.locator('.outgoing').count())

    def test_failed_and_pending_messages_are_not_confirmed(self):
        for state in ('failed', 'pending'):
            with self.subTest(state=state):
                self.page.evaluate('localStorage.clear();sessionStorage.clear()')
                self.mode(state)
                self.assert_uncertain(request=state)

    def test_no_stable_id_sends_once_then_reports_uncertain_after_reload(self):
        self.mode()
        self.ui.selectors['message_id_attribute'] = ''
        error = self.assert_uncertain()
        self.assertEqual('message_unverified', error.code)
        self.assertIn('稳定消息标识', str(error))
        self.assertEqual(1, self.page.locator('.outgoing').count())
        with self.assertRaises(DemoError):
            self.ui.send_once('955', 'different', 'next-task', self.ledger)
        self.assertEqual(2, self.submissions())

    def test_list_position_is_not_accepted_as_stable_message_id(self):
        self.mode()
        self.ui.selectors['message_id_attribute'] = 'data-index'
        self.assert_uncertain()

    def test_missing_outgoing_mapping_cannot_produce_success(self):
        self.mode()
        self.ui.selectors['outgoing_message'] = ''
        self.assert_uncertain()

    def test_outer_whitespace_is_sent_and_matched_without_trimming(self):
        self.mode()
        message = '  hello  '
        result = self.ui.send_once('955', message, 'spaces', self.ledger)
        self.assertEqual('persisted_in_chat', result['status'])
        self.assertEqual(message, self.page.locator('.text').text_content())
        self.assertEqual(1, self.ui.exact_message_count(message))
        self.assertEqual(0, self.ui.exact_message_count('hello'))
        with self.assertRaises(DemoError) as error:
            self.ui.send_once('955', 'hello', 'spaces', self.ledger)
        self.assertEqual('idempotency_conflict', error.exception.code)
        self.assertEqual(1, self.submissions())

    def test_duplicate_contact_never_sends(self):
        self.page.evaluate("document.querySelector('#contacts').innerHTML += '<span class=name>955</span>'")
        with self.assertRaises(DemoError):
            self.ui.send_once('955', 'hello', 'ambiguous', self.ledger)
        self.assertEqual(0, self.submissions())
        self.assertIsNone(self.ledger.get('ambiguous'))

    def test_existing_draft_not_overwritten(self):
        self.page.locator('#editor').fill('unfinished draft')
        with self.assertRaises(DemoError) as error:
            self.ui.send_once('955', 'hello', 'draft', self.ledger)
        self.assertEqual('existing_draft', error.exception.code)
        self.assertEqual('unfinished draft', self.page.locator('#editor').input_value())
        self.assertEqual(0, self.submissions())

    def test_zero_width_editor_placeholder_is_not_a_draft(self):
        self.page.set_content('<div id="editor" contenteditable="true">\u200b</div>')
        editor = self.page.locator('#editor')
        self.assertFalse(editor_has_draft(editor))
        editor.fill('real draft')
        self.assertTrue(editor_has_draft(editor))

    def test_database_failure_before_submission_cleans_only_owned_draft(self):
        with patch.object(self.ledger, 'begin', side_effect=sqlite3.OperationalError('fixture disk failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.ui.send_once('955', 'hello', 'disk-error', self.ledger)
        self.assertEqual('', self.page.locator('#editor').input_value())
        self.assertEqual(0, self.submissions())
        self.assertIsNone(self.ledger.get('disk-error'))
        result = self.ui.send_once('955', 'next', 'next', self.ledger, verify_after_reload=False)
        self.assertEqual('visible_in_chat', result['status'])

    def test_changed_user_text_is_never_cleared_during_recovery(self):
        def fail_after_user_edit(*args):
            self.page.locator('#editor').fill('user revision')
            raise sqlite3.OperationalError('fixture failure')
        with patch.object(self.ledger, 'begin', side_effect=fail_after_user_edit):
            with self.assertRaises(DemoError) as error:
                self.ui.send_once('955', 'hello', 'edited', self.ledger)
        self.assertEqual('draft_cleanup_required', error.exception.code)
        self.assertEqual('not_submitted', error.exception.submission)
        self.assertEqual('user revision', self.page.locator('#editor').input_value())
        self.assertEqual(0, self.submissions())

    def test_replaced_editor_with_same_text_is_not_cleared(self):
        def fail_after_replacement(*args):
            self.page.evaluate("const old=document.querySelector('#editor');const other=old.cloneNode();other.value='hello';old.replaceWith(other)")
            raise sqlite3.OperationalError('fixture failure')
        with patch.object(self.ledger, 'begin', side_effect=fail_after_replacement):
            with self.assertRaises(DemoError) as error:
                self.ui.send_once('955', 'hello', 'replaced', self.ledger)
        self.assertEqual('draft_cleanup_required', error.exception.code)
        self.assertEqual('hello', self.page.locator('#editor').input_value())
        self.assertEqual(0, self.submissions())

    def test_pre_submission_domain_error_keeps_type_after_draft_cleanup(self):
        class DayEnded(DemoError):
            pass
        with patch.object(self.ledger, 'begin', side_effect=DayEnded('day ended', code='missed')):
            with self.assertRaises(DayEnded) as error:
                self.ui.send_once('955', 'hello', 'midnight', self.ledger)
        self.assertEqual('missed', error.exception.code)
        self.assertEqual('', self.page.locator('#editor').input_value())
        self.assertEqual(0, self.submissions())

    def test_ui_exception_after_submission_keeps_request_uncertain(self):
        original = self.ui.message_snapshot
        calls = 0
        def evidence():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise PlaywrightError('fixture connection lost after submit')
            return original()
        self.ui.message_snapshot = evidence
        self.assert_uncertain()

    def test_optimistic_message_disappearing_after_reload_is_not_success(self):
        self.assert_uncertain(request='optimistic')
        self.assertEqual(0, self.page.locator('.outgoing').count())

    def test_persistent_cookie_and_local_storage_survive_relaunch(self):
        profile = Path(self.temp.name) / 'browser'
        for first in (True, False):
            context = self.playwright.chromium.launch_persistent_context(
                str(profile), channel='chromium', headless=True,
            )
            try:
                page = context.pages[0]
                page.goto(self.url)
                if first:
                    context.add_cookies([{
                        'name': 'demo_session', 'value': 'fixture-only', 'url': self.url,
                        'httpOnly': True, 'expires': 4102444800,
                    }])
                    page.evaluate("localStorage.setItem('fixture', 'saved')")
                else:
                    self.assertTrue(any(c['name'] == 'demo_session' for c in context.cookies()))
                    self.assertEqual('saved', page.evaluate("localStorage.getItem('fixture')"))
            finally:
                context.close()


if __name__ == '__main__':
    unittest.main()

"""Loopback-only tests of identity-bound existing-chat navigation."""
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from douyin_demo import DemoError, SendLedger, read_selectors
from spark_assistant.chat_directory import ChatDirectory, IdentityChatUI, canonical_account, classify
from spark_assistant.paths import configure_browser

HTML = r'''<!doctype html><meta charset="utf-8">
<style>.conversationConversationListwrapper{height:90px;overflow-y:scroll}.conversationConversationItemwrapper{height:40px}.messageEditorinputArea{min-height:30px}</style>
<div class="conversationConversationListwrapper"></div>
<div class="componentsRightPanelwrapper"><h2 class="RightPanelHeadertitle"></h2>
<div class="messageMessageListlist"></div><div class="messageEditorinputArea" contenteditable="true"></div></div>
<script>
const list=document.querySelector('.conversationConversationListwrapper');
const title=document.querySelector('.RightPanelHeadertitle'),editor=document.querySelector('.messageEditorinputArea');
let current=null;
const models=[{id:'c:1',toParticipantSecUserId:'peer_1',type:1,participantCount:2},
{id:'c:2',toParticipantSecUserId:'peer_2',type:1,participantCount:2},
{id:'c:3',toParticipantSecUserId:'',type:2,participantCount:5}];
function bind(e,key,value){e.__reactFiber$fixture={memoizedProps:{[key]:value},return:null};}
window.switchTo=i=>{current=models[i];title.textContent='Same remark';bind(title,'curConversation',current);bind(editor,'curConversation',current);};
for(let i=0;i<models.length;i++){
 const row=document.createElement('div');row.className='conversationConversationItemwrapper';
 row.innerHTML='<span class="conversationConversationItemtitle">Same remark</span>';
 bind(row,'conversation',models[i]);row.onclick=()=>switchTo(i);list.append(row);
}
const messages=document.querySelector('.messageMessageListlist');
function render(){messages.innerHTML='';for(const row of JSON.parse(localStorage.getItem('messages')||'[]')){
 const p=document.createElement('p');p.className='messageMessageBoxmessageBox messageMessageBoxisFromMe';p.dataset.messageId=row.id;
 const t=document.createElement('span');t.className='TextMessageTextpureText';t.textContent=row.text;p.append(t);messages.append(p);
}}
render();editor.addEventListener('keydown',event=>{
 if(event.key!=='Enter')return;event.preventDefault();
 const rows=JSON.parse(localStorage.getItem('messages')||'[]');rows.push({id:'m'+(rows.length+1),text:editor.innerText,peer:current.toParticipantSecUserId});
 localStorage.setItem('messages',JSON.stringify(rows));editor.innerText='';render();
});
</script>'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(HTML.encode())
    def log_message(self,*args):pass

class IdentityRulesTests(unittest.TestCase):
    def test_self_alias_and_foreign_origin_never_account_identity(self):
        self.assertEqual('peer',canonical_account('https://www.douyin.com/user/peer'))
        for url in ['https://www.douyin.com/user/self','https://evil.example/user/peer','http://www.douyin.com/user/peer','https://www.douyin.com/user/peer?x=1']:
            self.assertIsNone(canonical_account(url))
    def test_unknown_and_group_never_sendable(self):
        row={'platform_id':'peer','conversation_id':'c:1','display_name':'x','type':1,'participant_count':2}
        self.assertTrue(classify(row)['sendable'])
        for value in [{**row,'type':2},{**row,'participant_count':3},{**row,'platform_id':''},{**row,'conversation_id':''}]:
            self.assertFalse(classify(value)['sendable'])
    def test_identity_ledger_allows_renamed_label_but_rejects_account_or_target_change(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=SendLedger(Path(folder)/'ledger.db')
            try:
                binding={'account_id':'me','account_epoch':1,'platform_id':'peer','conversation_id':'c:1'}
                ledger.identity=binding;ledger.begin('one','old','hello')
                self.assertEqual('uncertain',ledger.lookup('one','new','hello'))
                for field,value in [('account_id','other'),('account_epoch',2),('platform_id','other'),('conversation_id','c:2')]:
                    ledger.identity={**binding,field:value}
                    with self.assertRaises(DemoError) as raised:ledger.lookup('one','new','hello')
                    self.assertEqual('idempotency_conflict',raised.exception.code)
            finally:ledger.close()

@unittest.skipUnless(os.environ.get('SPARK_BROWSER_INTEGRATION')=='1','Set SPARK_BROWSER_INTEGRATION=1 for loopback Chromium')
class ChatDirectoryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure_browser();cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
        cls.p=sync_playwright().start();cls.browser=cls.p.chromium.launch(channel='chromium',headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.p.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join(5)
    def setUp(self):
        self.context=self.browser.new_context();self.page=self.context.new_page();self.page.goto(self.url)
        self.selectors=read_selectors(Path(__file__).resolve().parents[1]/'selectors.json')
        self.selectors['message_id_attribute']='data-message-id'
        self.target={'platform_id':'peer_2','conversation_id':'c:2','account_id':'account_fixture'}
        self.ui=IdentityChatUI(self.page,self.selectors,self.target);self.ui.RELOAD_SETTLE_MS=10;self.ui.EVIDENCE_TIMEOUT=.3
        self.ui.directory.SCROLL_WAIT_MS=5
        self.ui.directory.read_account=lambda: {'platform_id':'account_fixture','verification':'verified'}
        self.temp=tempfile.TemporaryDirectory();self.ledger=SendLedger(Path(self.temp.name)/'ledger.db')
    def tearDown(self):
        self.ledger.close();self.temp.cleanup();self.context.close()
    def messages(self):return self.page.evaluate("JSON.parse(localStorage.getItem('messages')||'[]')")
    def test_same_name_contacts_retained_and_group_disabled(self):
        directory=ChatDirectory(self.page,self.selectors);directory.SCROLL_WAIT_MS=5
        with patch.object(directory,'read_account',return_value={'platform_id':'me','verification':'verified'}):result=directory.sync()
        self.assertEqual(3,len(result['friends']));self.assertEqual(2,sum(f['sendable'] for f in result['friends']))
        self.assertEqual('partial',result['coverage']['status']);self.assertEqual('existing_chats',result['coverage']['directory_kind'])
        self.assertTrue(all(f['source']=='official_chat_ui' for f in result['friends']))
    def test_same_name_exact_target_survives_refresh(self):
        outcome=self.ui.send_once('obsolete name','hello','exact',self.ledger)
        self.assertEqual('persisted_in_chat',outcome['status']);self.assertEqual('peer_2',self.messages()[0]['peer'])
    def test_missing_identity_never_falls_back_to_same_name(self):
        self.page.evaluate("delete document.querySelectorAll('.conversationConversationItemwrapper')[1].__reactFiber$fixture")
        with self.assertRaises(DemoError):self.ui.send_once('Same remark','hello','missing',self.ledger)
        self.assertEqual([],self.messages());self.assertIsNone(self.ledger.get('missing'))
    def test_editor_disagrees_with_header_stops_before_fill(self):
        original=self.ui.verify_recipient
        def changed(name,editor):
            self.page.evaluate("document.querySelector('.messageEditorinputArea').__reactFiber$fixture.memoizedProps.curConversation={id:'c:1',toParticipantSecUserId:'peer_1',type:1,participantCount:2}")
            original(name,editor)
        with patch.object(self.ui,'verify_recipient',side_effect=changed):
            with self.assertRaises(DemoError):self.ui.send_once('Same remark','hello','changed',self.ledger)
        self.assertEqual('',self.page.locator('.messageEditorinputArea').inner_text().strip());self.assertEqual([],self.messages())
    def test_identity_switch_after_fill_does_not_clear_other_conversation(self):
        original=self.ui.verify_recipient;calls=0
        def changed(name,editor):
            nonlocal calls
            calls+=1
            if calls==3:self.page.evaluate('switchTo(0)')
            original(name,editor)
        with patch.object(self.ui,'verify_recipient',side_effect=changed):
            with self.assertRaises(DemoError) as error:self.ui.send_once('Same remark','hello','switched',self.ledger)
        self.assertEqual('draft_cleanup_required',error.exception.code)
        self.assertEqual([],self.messages());self.assertIsNone(self.ledger.get('switched'))
    def test_event_time_identity_guard_blocks_last_moment_switch(self):
        editor=self.ui.select_contact('ignored');editor.fill('hello')
        self.ui._submission_context={'message':'hello','scheduled_date':None}
        # This document listener runs before the production guard, simulating
        # the user switching a same-name conversation immediately before Enter.
        self.page.evaluate("document.addEventListener('keydown',()=>switchTo(0),{capture:true,once:true})")
        with self.assertRaises(DemoError) as error:self.ui.submit(editor,None)
        self.assertEqual('submission_guard_blocked',error.exception.code)
        self.assertEqual([],self.messages())
    def test_read_account_uses_self_canonical_without_touching_chat(self):
        self.context.route('https://www.douyin.com/user/self',lambda route:route.fulfill(status=200,content_type='text/html',body='<link rel="canonical" href="https://www.douyin.com/user/account_fixture">'))
        old_url=self.page.url
        result=ChatDirectory(self.page,self.selectors).read_account()
        self.assertEqual({'platform_id':'account_fixture','verification':'verified'},result)
        self.assertEqual(old_url,self.page.url);self.assertEqual(1,len(self.context.pages))

    def test_sync_account_switch_discards_collected_rows(self):
        directory=ChatDirectory(self.page,self.selectors);directory.SCROLL_WAIT_MS=5
        for final in [{'platform_id':'other','verification':'verified'},{'platform_id':'','verification':'unknown'}]:
            with patch.object(directory,'read_account',side_effect=[{'platform_id':'me','verification':'verified'},final]):result=directory.sync()
            self.assertEqual([],result['friends']);self.assertEqual('failed',result['coverage']['status'])
            self.assertEqual(final,result['account'])
    def test_scan_error_retains_partial_only_after_account_reverification(self):
        directory=ChatDirectory(self.page,self.selectors);directory.SCROLL_WAIT_MS=5
        rows=directory.rows();progress=[];directory.progress=progress.append
        with patch.object(directory,'rows',side_effect=[rows,PlaywrightError('fixture navigation')]),patch.object(directory,'read_account',return_value={'platform_id':'me','verification':'verified'}):
            result=directory.sync()
        self.assertEqual(3,len(result['friends']));self.assertEqual('partial',result['coverage']['status'])
        self.assertEqual('scan_interrupted',result['coverage']['reason_code']);self.assertTrue(progress)
    def test_account_change_after_fill_stops_before_ledger(self):
        self.ui.directory.read_account=lambda:{'platform_id':'other','verification':'verified'}
        with self.assertRaises(DemoError) as error:self.ui.send_once('Same remark','approved','account-changed',self.ledger)
        self.assertEqual('account_identity_mismatch',error.exception.code)
        self.assertIsNone(self.ledger.get('account-changed'));self.assertEqual([],self.messages())
        self.assertEqual('',self.page.locator('.messageEditorinputArea').inner_text().strip())
    def test_body_change_after_ledger_cannot_send_changed_text(self):
        begin=self.ledger.begin
        def changed(*args):
            begin(*args);self.page.locator('.messageEditorinputArea').fill('user-changed')
        with patch.object(self.ledger,'begin',side_effect=changed):
            with self.assertRaises(DemoError) as error:self.ui.send_once('label','approved','body-changed',self.ledger)
        self.assertEqual('possible',error.exception.submission);self.assertEqual([],self.messages())
        self.assertEqual('uncertain',self.ledger.get('body-changed'))
    def test_midnight_after_ledger_cannot_submit_next_day(self):
        from spark_assistant.worker import beijing_today
        self.ledger.scheduled_date=beijing_today();begin=self.ledger.begin
        def midnight(*args):
            begin(*args);self.page.evaluate('Date.now = () => '+str(__import__('time').time_ns()//1000000+86400000))
        with patch.object(self.ledger,'begin',side_effect=midnight):
            with self.assertRaises(DemoError) as error:self.ui.send_once('label','approved','midnight',self.ledger)
        self.assertEqual('possible',error.exception.submission);self.assertEqual([],self.messages())
    def test_event_time_body_change_is_blocked(self):
        editor=self.ui.select_contact('ignored');editor.fill('approved')
        self.ui._submission_context={'message':'approved','scheduled_date':None}
        self.page.evaluate("document.addEventListener('keydown',()=>document.querySelector('[contenteditable]').innerText='user-change',{capture:true,once:true})")
        with self.assertRaises(DemoError) as error:self.ui.submit(editor,None)
        self.assertEqual('submission_guard_blocked',error.exception.code)
        self.assertEqual([],self.messages())

    def test_contenteditable_value_expando_does_not_override_rendered_message(self):
        self.page.locator('.messageEditorinputArea').evaluate("e => {e.value = ''}")
        result=self.ui.send_once('label','hello','expando-value',self.ledger)
        self.assertEqual('persisted_in_chat',result['status'])
        self.assertEqual('hello',self.messages()[0]['text'])
    def test_contenteditable_value_expando_does_not_hide_user_draft(self):
        editor=self.page.locator('.messageEditorinputArea');editor.fill('unfinished')
        editor.evaluate("e => {e.value = ''}")
        with self.assertRaises(DemoError) as error:self.ui.send_once('label','approved','existing',self.ledger)
        self.assertEqual('existing_draft',error.exception.code);self.assertEqual([],self.messages())
        self.assertEqual('unfinished',editor.inner_text())
    def test_changed_body_reports_specific_safe_stage_and_preserves_payload(self):
        begin=self.ledger.begin
        def changed(*args):
            begin(*args);self.page.locator('.messageEditorinputArea').evaluate("e => e.innerText += '\u200b'")
        with patch.object(self.ledger,'begin',side_effect=changed):
            with self.assertRaises(DemoError) as error:self.ui.send_once('label','approved','body-marker',self.ledger)
        self.assertEqual('editor_text_changed',error.exception.code)
        self.assertEqual('validating_submission',error.exception.stage)
        self.assertEqual('DemoError',error.exception.cause_type)
        self.assertEqual([],self.messages());self.assertEqual('uncertain',self.ledger.get('body-marker'))
    def test_actual_submission_exception_retains_stage_without_raw_page_text(self):
        original=self.ui.submit
        def erroring(editor,button):
            self.ui.send_stage='dispatching_submission'
            raise PlaywrightError('SECRET: fixture page text and private endpoint')
        with patch.object(self.ui,'submit',side_effect=erroring):
            with self.assertRaises(DemoError) as error:self.ui.send_once('label','approved','dispatch-error',self.ledger)
        self.assertEqual('dispatching_submission',error.exception.stage)
        self.assertEqual('Error',error.exception.cause_type)
        self.assertNotIn('SECRET',str(error.exception));self.assertEqual([],self.messages())

    def test_verified_terminal_placeholder_is_not_message_content(self):
        self.ui.selectors['editor_placeholder']='.ace-line > span[data-enter][data-leaf][data-string]'
        editor=self.ui.select_contact('ignored')
        for message in ['approved','left\u200bright','ends\u200b']:
            editor.evaluate("""(e,text)=>{
                e.replaceChildren();const line=document.createElement('div');line.className='ace-line';
                const body=document.createElement('span');body.dataset.string='true';body.dataset.leaf='true';body.textContent=text;
                const sentinel=document.createElement('span');sentinel.dataset.string='true';sentinel.dataset.leaf='true';sentinel.dataset.enter='true';sentinel.textContent='\u200b';
                line.append(body,sentinel);e.append(line);
            }""",message)
            self.ui._submission_context={'message':message,'scheduled_date':None}
            self.assertEqual(message,self.ui.editor_text(editor))
            self.assertTrue(editor.evaluate(self.ui.submission_guard_js(),self.ui.submission_guard_config()))
            handle=editor.element_handle()
            try:self.assertTrue(self.ui._clear_owned_draft('ignored',message,handle))
            finally:handle.dispose()
    def test_hidden_or_unmarked_zero_width_is_never_removed_from_body(self):
        self.ui.selectors['editor_placeholder']='.ace-line > span[data-enter][data-leaf][data-string]'
        editor=self.ui.select_contact('ignored')
        self.ui._submission_context={'message':'approved','scheduled_date':None}
        for markup in [
            '<div class="ace-line"><span>approved\u200b</span></div>',
            '<div class="ace-line"><span>approved\u200b</span><span data-enter data-leaf data-string style="display:none">\u200b</span></div>',
            '<div class="ace-line"><span data-enter data-leaf data-string>approved\u200b</span></div>',
            '<div class="ace-line"><span data-enter data-leaf data-string>\u200b</span><span>approved</span></div>',
        ]:
            editor.evaluate('(e,markup)=>e.innerHTML=markup',markup)
            self.assertFalse(editor.evaluate(self.ui.submission_guard_js(),self.ui.submission_guard_config()))

    def official_draft(self,message='approved'):
        editor=self.ui.select_contact('ignored')
        editor.evaluate("""(e,text)=>{
            e.replaceChildren();const line=document.createElement('div');line.className='ace-line';
            const body=document.createElement('span');body.dataset.string='true';body.dataset.leaf='true';body.textContent=text;
            const sentinel=document.createElement('span');sentinel.dataset.string='true';sentinel.dataset.leaf='true';sentinel.dataset.enter='true';sentinel.textContent='\u200b';
            line.append(body,sentinel);e.append(line);
        }""",message)
        return editor
    def test_native_draft_cleanup_handles_official_editor_ignoring_empty_fill(self):
        from playwright.sync_api import Locator
        editor=self.official_draft();original=editor.element_handle()
        try:
            with patch.object(Locator,'fill',return_value=None):
                self.assertTrue(self.ui._clear_owned_draft('ignored','approved',original))
            self.assertEqual('',self.ui.editor_text(editor).strip());self.assertEqual([],self.messages())
        finally:original.dispose()
    def test_native_cleanup_never_removes_user_edit_after_empty_fill(self):
        from playwright.sync_api import Locator
        editor=self.official_draft();original=editor.element_handle()
        def changed(*args,**kwargs):editor.evaluate("e=>e.innerText='user revision'")
        try:
            with patch.object(Locator,'fill',side_effect=changed):
                self.assertFalse(self.ui._clear_owned_draft('ignored','approved',original))
            self.assertEqual('user revision',editor.inner_text())
        finally:original.dispose()
    def test_native_cleanup_guard_keeps_event_time_user_edit(self):
        from playwright.sync_api import Locator
        editor=self.official_draft();original=editor.element_handle()
        self.page.evaluate("""()=>{const handler=event=>{if(event.key==='Backspace'){
            document.querySelector('[contenteditable]').innerText='user revision';
            document.removeEventListener('keydown',handler,true);
        }};document.addEventListener('keydown',handler,true);} """)
        try:
            with patch.object(Locator,'fill',return_value=None):
                self.assertFalse(self.ui._clear_owned_draft('ignored','approved',original))
            self.assertEqual('user revision',editor.inner_text())
        finally:original.dispose()

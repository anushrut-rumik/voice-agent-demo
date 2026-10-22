"""Deterministic contract tests, isolated from the live UI and real services."""
import json
import asyncio
from unittest.mock import AsyncMock
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from fastapi.testclient import TestClient
from payment_reminder.harness import create_harness, now, IST, AGENT, readable_transcript

SECRET='test-only-'+40*'x'
HEADERS={'X-Demo-Console':'1'}

class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_harness(Path(self.tmp.name)/'test.sqlite',SECRET,background=False)
        self.h=self.app.state.harness
        self.client=TestClient(self.app)
        self.tools=TestClient(self.h.tool_app)
    def tearDown(self):
        self.tools.close();self.client.close();self.tmp.cleanup()
    def post(self,path,body=None):return self.client.post('/api/'+path,json=body or {},headers=HEADERS)
    def customer(self,cid='rahul'):return next(c for c in self.h.state()['customers'] if c['id']==cid)
    def save(self,outcome='promise_to_pay',**fields):
        token,_=self.h.prepare('rahul','contract-call')
        body={'case_token':token,'call_id':'contract-call','outcome':outcome,**fields}
        r=self.tools.post('/voice/reminders/outcomes',json=body,headers={'Authorization':'Bearer '+SECRET})
        self.assertEqual(r.status_code,200,r.text)
        with self.h.store.connect() as db:db.execute("UPDATE demo_calls SET status='completed' WHERE id='contract-call'")
        self.h.process('contract-call')
    def test_promise_schedules_real_date_then_payment_cancels(self):
        with self.h.store.connect() as db:db.execute("UPDATE demo_meta SET value='2050-01-01T10:00:00+05:30' WHERE key='clock'")
        promised=(now().astimezone(IST)+timedelta(days=2)).date().isoformat()
        self.save(promised_amount='18000',promised_date=promised)
        self.h.tick();c=self.customer()
        self.assertEqual(c['calls'][0]['outcome'],'promise_to_pay')
        self.assertEqual(c['job']['status'],'scheduled');self.assertEqual(c['job']['clock_mode'],'live')
        self.assertTrue(c['job']['due_at'].startswith(str(now().year)))
        self.assertNotIn('case_token',c['calls'][0]['saved_outcomes'][0])
        body={'customer_id':'rahul','amount':'18000','reference':'PAY-1'}
        self.assertEqual(self.post('payments',body).status_code,200)
        self.assertEqual(self.post('payments',body).json()['status'],'duplicate')
        self.assertEqual(self.customer()['balance'],0);self.assertEqual(self.customer()['job']['status'],'cancelled')
        self.h.process('contract-call');self.assertEqual(self.customer()['job']['status'],'cancelled')
    def test_claim_pauses_and_does_not_clear_ledger(self):
        self.save('payment_claimed');c=self.customer()
        self.assertEqual(c['balance'],18000);self.assertEqual(c['status'],'paused')
        self.assertFalse(c['can_retry_setup']);self.assertEqual(c['job']['status'],'cancelled')
    def test_stop_survives_new_invoice(self):
        self.save('do_not_call')
        self.post('invoices',{'customer_id':'rahul','amount':'100','reference':'NEW'})
        self.assertEqual(self.customer()['status'],'suppressed')
        self.assertEqual(self.post('retry-setup',{'customer_id':'rahul'}).status_code,409)
    def test_ledger_validation(self):
        body={'customer_id':'neha','amount':'1600','reference':'NEW'}
        self.assertEqual(self.post('invoices',body).status_code,200)
        self.assertEqual(self.customer('neha')['balance'],5100)
        self.post('payments',{'customer_id':'neha','amount':'100','reference':'PART'})
        self.assertEqual(self.customer('neha')['job']['status'],'cancelled')
        self.assertEqual(self.post('invoices',body|{'amount':'2000'}).status_code,409)
        for amount in ['NaN','-1','0','1.001','Infinity','18001']:
            self.assertEqual(self.post('payments',{'customer_id':'rahul','amount':amount,'reference':'BAD'}).status_code,422)
    def test_local_protection_and_no_simulation_routes(self):
        self.assertEqual(self.client.post('/api/live',json={}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={'Host':'public.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/live',json={},headers=HEADERS|{'Origin':'https://evil.example'}).status_code,403)
        for route in ['simulate','advance','reset','connection']:
            self.assertEqual(self.post(route).status_code,404)
    def test_context_handle_resolves_correct_selected_customer(self):
        self.h.prepare('priya','priya-call')
        r=self.tools.get('/voice/reminders/context',params={'agent_id':'ua_afe624fa','call_id':'priya-call'},headers={'Authorization':'Bearer '+SECRET})
        self.assertEqual(r.json()['customer_name'],'Priya Mehta');self.assertEqual(r.json()['outstanding_amount'],'32500.00')
        self.assertIn('DEMO-PRIYA',r.json()['invoice_summary'])
        self.assertTrue(self.customer('priya')['calls'][0]['context_loaded'])
        r=self.tools.get('/voice/reminders/context',params={'agent_id':'ua_other','call_id':'priya-call'},headers={'Authorization':'Bearer '+SECRET})
        self.assertEqual(r.json()['context_status'],'unavailable')
        r=self.tools.get('/voice/reminders/context',params={'agent_id':'ua_afe624fa','call_id':'unknown'},headers={'Authorization':'Bearer '+SECRET})
        self.assertEqual(r.json()['context_status'],'unavailable')
    def mock_upstream(self,load_context):
        self.h.api_key='test-key'
        async def upstream(method,path,body=None,auth=True):
            if path=='/v1/register-call':return {'call_id':'live-1','access_token':'single-use'}
            if path=='/v1/webcall':
                self.assertFalse(auth)
                if load_context:
                    r=self.tools.get('/voice/reminders/context',params={'agent_id':'ua_afe624fa','call_id':'live-1'},headers={'Authorization':'Bearer '+SECRET})
                    self.assertEqual(r.json()['customer_name'],'Rahul Sharma')
                return {'callId':'live-1','host':'wss://example.com','token':'room-token'}
            return {'status':'completed','transcript':[{'role':'assistant','content':'Test transcript'}]}
        self.h.upstream=upstream
    def test_audio_credentials_only_after_actual_context_receipt(self):
        self.mock_upstream(True)
        r=self.post('live',{'customer_id':'rahul'})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['context_loaded'])
        self.assertEqual(self.post('live',{'customer_id':'priya'}).status_code,409)
        self.post('poll');c=self.customer()
        self.assertEqual(c['calls'][0]['outcome'],'call_completed')
        self.assertFalse(c['can_retry_setup'])
    def test_missing_context_blocks_audio_and_can_reopen_after_finish(self):
        self.mock_upstream(False)
        r=self.post('live',{'customer_id':'rahul'})
        self.assertEqual(r.status_code,502);self.assertNotIn('room-token',r.text)
        self.assertEqual(self.post('retry-setup',{'customer_id':'rahul'}).status_code,409)
        self.post('poll');self.assertTrue(self.customer()['can_retry_setup'])
        self.assertEqual(self.post('retry-setup',{'customer_id':'rahul'}).status_code,200)
        self.assertEqual(self.customer()['status'],'active')
        self.assertEqual(len(self.customer()['calls']),1)
    def test_completed_call_does_not_block_its_customer(self):
        self.h.prepare('rahul','previous')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET status='completed' WHERE id='previous'")
        self.mock_upstream(True)
        allowed=self.post('live',{'customer_id':'rahul'})
        self.assertEqual(allowed.status_code,200,allowed.text)

    def test_analysis_routes_and_state_are_removed(self):
        self.assertEqual(self.post('calls/old/analyze').status_code,404)
        self.assertEqual(self.client.get('/api/calls/old/analysis').status_code,404)
        self.h.prepare('rahul','simple-call')
        state=self.client.get('/api/state').json()
        self.assertNotIn('gemini_configured',state)
        self.assertNotIn('analysis',self.customer()['calls'][0])
        self.assertFalse(hasattr(self.h,'analysis'))

    def test_nonterminal_calls_still_block_other_customers(self):
        self.h.prepare('priya','active-call')
        self.h.upstream=AsyncMock()
        for status in ('starting','running','in_progress','unknown'):
            with self.subTest(status=status):
                with self.h.store.connect() as db:
                    db.execute("UPDATE demo_calls SET status=? WHERE id='active-call'",(status,))
                blocked=self.post('live',{'customer_id':'rahul'})
                self.assertEqual(blocked.status_code,409)
                self.assertIn('still active',blocked.json()['detail'])
        self.h.upstream.assert_not_awaited()
    def test_completed_saved_call_finishes_without_external_analysis(self):
        self.h.api_key='fake-key';self.h.prepare('rahul','finished')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET status='completed',transcript=? WHERE id='finished'",(json.dumps([{'role':'user','content':'Later.'}]),))
            db.execute("INSERT INTO tool_receipts(call_id,tool,status,created_at) VALUES('finished','context','ready',?)",(now().isoformat(),))
        self.h.upstream=AsyncMock(side_effect=AssertionError('No external request needed'))
        asyncio.run(self.h.poll())
        self.h.upstream.assert_not_awaited()
        call=self.customer()['calls'][0]
        self.assertEqual(call['processed'],1);self.assertEqual(call['outcome'],'call_completed')
        self.assertEqual(self.customer()['status'],'active')
        self.assertEqual(self.customer()['balance'],18000)

    def test_legacy_analysis_holds_are_removed_without_clearing_customer_requests(self):
        self.h.prepare('arjun','old-analysis')
        token,_=self.h.prepare('priya','customer-dispute')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET status='completed',processed=1,outcome='needs_review'")
            db.execute("UPDATE demo_customers SET status='review' WHERE id IN ('arjun','priya')")
            db.execute("DELETE FROM demo_meta WHERE key='simple_outcomes_v1'")
        self.tools.post('/voice/reminders/outcomes',headers={'Authorization':'Bearer '+SECRET},json={'case_token':token,'call_id':'customer-dispute','outcome':'dispute'})
        self.h.remove_legacy_analysis_holds()
        self.assertEqual(self.customer('arjun')['status'],'active')
        self.assertEqual(self.customer('priya')['status'],'paused')
        self.assertEqual(self.customer('arjun')['balance'],8500)
        self.assertEqual(self.customer('arjun')['calls'][0]['outcome'],'call_completed')

    def test_payment_during_call_changes_tool_balance(self):
        token,_=self.h.prepare('rahul','live-2')
        self.post('payments',{'customer_id':'rahul','amount':'18000','reference':'LIVEPAY'})
        r=self.tools.get('/voice/reminders/balance',params={'case_token':token,'call_id':'live-2'},headers={'Authorization':'Bearer '+SECRET})
        self.assertEqual(r.json()['payment_status'],'cleared')
    def test_escaped_hindi_is_readable_without_rewriting_other_text(self):
        source=[{'role':'user','content':r'\u0939\u093e\u0901, yes. C:\temp'}]
        self.assertEqual(readable_transcript(source)[0]['content'],'हाँ, yes. C:\\temp')
        self.assertEqual(source[0]['content'],r'\u0939\u093e\u0901, yes. C:\temp')

    def test_history_is_only_saved_live_notes(self):
        with self.h.store.connect() as db:self.h.log(db,'rahul','Old scripted activity','Invented promise by tomorrow')
        self.assertNotIn('Invented',self.customer()['context_preview']['prior_contact_summary'])
        self.save('no_commitment',summary='No date confirmed.')
        self.assertIn('No date confirmed.',self.customer()['context_preview']['prior_contact_summary'])
    def test_demo_invoice_brief_explains_purchase_and_preserves_remaining_balance(self):
        self.post('payments',{'customer_id':'arjun','amount':'1000','reference':'PARTIAL-ARJUN'})
        ctx=self.customer('arjun')['context_preview']
        self.assertEqual(ctx['outstanding_amount'],'7500.00')
        self.assertIn('Patel Distributors',ctx['invoice_summary'])
        self.assertIn('packing boxes',ctx['invoice_summary'])
        self.assertIn('original total INR 8500.00',ctx['invoice_summary'])
        self.assertIn('due 2026-10-23',ctx['invoice_summary'])
        self.assertIn('fictional demo',ctx['invoice_summary'])
    def test_demo_details_do_not_fill_unknown_or_mismatched_invoices(self):
        self.post('invoices',{'customer_id':'arjun','amount':'100','reference':'UNKNOWN-INVOICE'})
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_ledger SET amount=900000 WHERE reference='DEMO-ARJUN'")
        summary=self.customer('arjun')['context_preview']['invoice_summary']
        self.assertNotIn('packing boxes',summary)
        self.assertNotIn('2026-10-23',summary)
        self.assertEqual(summary.count('purchase description and due date not supplied'),2)
    def test_history_uses_dated_saved_notes_and_customer_words(self):
        token,_=self.h.prepare('arjun','prior')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET status='completed',transcript=?,created_at='2026-10-19T10:00:00+05:30' WHERE id='prior'",(json.dumps([{'role':'user','content':'Can I pay tomorrow?'}]),))
        r=self.tools.post('/voice/reminders/outcomes',headers={'Authorization':'Bearer '+SECRET},json={
            'case_token':token,'call_id':'prior','outcome':'no_commitment','summary':'Customer asked about tomorrow.'})
        self.assertEqual(r.status_code,200)
        history=self.customer('arjun')['context_preview']['prior_contact_summary']
        self.assertIn('2026-10-19T10:00:00+05:30',history)
        self.assertIn('Customer asked about tomorrow.',history)
        self.assertIn('Can I pay tomorrow?',history)
        self.assertNotIn('review',history)
    def test_new_brief_does_not_rewrite_previous_call_snapshot(self):
        self.h.prepare('arjun','immutable')
        with self.h.store.connect() as db:
            snapshot=db.execute("SELECT context_snapshot FROM demo_calls WHERE id='immutable'").fetchone()[0]
        self.post('payments',{'customer_id':'arjun','amount':'1000','reference':'AFTER-CALL'})
        self.customer('arjun')
        with self.h.store.connect() as db:
            self.assertEqual(db.execute("SELECT context_snapshot FROM demo_calls WHERE id='immutable'").fetchone()[0],snapshot)

class BackgroundProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_finishes_automatically_without_a_reviewer(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_harness(Path(folder)/'test.sqlite',SECRET,api_key='fake-key')
            h=app.state.harness;h.prepare('rahul','automatic')
            with h.store.connect() as db:
                db.execute("INSERT INTO tool_receipts(call_id,tool,status,created_at) VALUES('automatic','context','ready',?)",(now().isoformat(),))
            polled=asyncio.Event()
            async def upstream(method,path):
                polled.set()
                return {'status':'completed','transcript':[{'role':'user','content':'Later.'}]}
            h.upstream=upstream
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(polled.wait(),2)
                with h.store.connect() as db:
                    self.assertEqual(db.execute("SELECT processed FROM demo_calls WHERE id='automatic'").fetchone()[0],1)
                    self.assertEqual(h.balance(db,'rahul'),1800000)
                self.assertFalse(hasattr(h,'analysis'))

if __name__=='__main__':unittest.main()

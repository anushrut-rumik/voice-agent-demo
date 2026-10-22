import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock,patch

import httpx
from fastapi.testclient import TestClient
from payment_reminder.app import now
from payment_reminder.analysis import Result,TemporaryFailure,Gemini,validate
from payment_reminder.harness import create_harness,IST

SECRET='test-only-'+40*'x'

def answer():
    day=now().astimezone(IST).date()
    return Result(summary='Customer will pay INR 18000 in two to three days.',outcome='promise_to_pay',
        promised_amount='18000',payment_date_start=(day+timedelta(days=2)).isoformat(),
        payment_date_end=(day+timedelta(days=3)).isoformat(),payment_timeframe='In two to three days',
        callback_at='',payment_reference='',evidence=[
            {'turn':1,'quote':'18000','supports':'amount'},
            *({'turn':2,'quote':'I will pay 18000 in two to three days.','supports':kind} for kind in ('outcome','amount','payment_window'))])

class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite'
        self.app=create_harness(self.path,SECRET,background=False,gemini_key='fake-key')
        self.h=self.app.state.harness;self.p=self.h.extraction
        self.client=TestClient(self.app)
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def call(self,cid='rahul',call_id='call'):
        self.h.prepare(cid,call_id)
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET status='completed',transcript=? WHERE id=?",(json.dumps([
                {'role':'assistant','content':'The amount is 18000.'},
                {'role':'user','content':'I will pay 18000 in two to three days.'}]),call_id))
            db.execute("INSERT INTO tool_receipts(call_id,tool,status,created_at) VALUES(?,'context','ready',?)",(call_id,now().isoformat()))
        self.h.process(call_id)
    def customer(self):return next(c for c in self.h.state()['customers'] if c['id']=='rahul')
    def status(self,call_id='call'):
        return self.client.get('/api/calls/'+call_id+'/extraction').json()
    def run_result(self):
        self.p.client.extract=AsyncMock(return_value=(answer(),{'response_id':'real-schema-mock'}))
        self.p.discover();asyncio.run(self.p.run_next())

    def test_automatic_discovery_extracts_range_and_schedules_without_changing_balance(self):
        self.call();self.run_result();data=self.status();c=self.customer()
        self.assertEqual(data['status'],'completed')
        self.assertEqual(c['calls'][0]['outcome'],'promise_to_pay')
        self.assertEqual(c['calls'][0]['payment_details']['promised_amount'],'18000')
        self.assertEqual(c['balance'],18000);self.assertEqual(c['status'],'active')
        expected=answer().payment_date_end
        self.assertGreater(c['job']['due_at'][:10],expected)
        self.assertEqual(c['calls'][0]['processed'],1)
        self.assertNotIn('analysis',c['calls'][0])
        self.p.discover();self.p.queue('call');asyncio.run(self.p.run_next())
        self.assertEqual(self.p.client.extract.await_count,1)

    def test_transient_failure_retries_and_does_not_hold_customer(self):
        self.call();self.p.discover()
        self.p.client.extract=AsyncMock(side_effect=[TemporaryFailure('Timed out'),(answer(),{})])
        asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'retry_wait')
        self.assertEqual(self.customer()['status'],'active')
        self.assertEqual(self.customer()['calls'][0]['processed'],1)
        asyncio.run(self.p.run_next());self.assertEqual(self.p.client.extract.await_count,1)
        with self.h.store.connect() as db:db.execute("UPDATE conversation_analyses SET next_attempt_at='' ")
        asyncio.run(self.p.run_next());self.assertEqual(self.status()['status'],'completed')

    def test_no_commitment_without_quote_saves_conservative_brief_once(self):
        self.call();self.p.discover()
        result=answer().model_copy(update={'outcome':'no_commitment','promised_amount':'',
            'payment_date_start':'','payment_date_end':'','payment_timeframe':'','evidence':[],
            'summary':'Unsupported model interpretation','next_step':'Unsupported action'})
        self.p.client.extract=AsyncMock(return_value=(result,{}))
        asyncio.run(self.p.run_next());asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'completed')
        self.assertEqual(self.p.client.extract.await_count,1)
        self.assertEqual(self.customer()['calls'][0]['conversation_summary'],
            'No confirmed payment plan was established in the available transcript.')

    def test_positive_outcome_still_requires_customer_quote(self):
        self.call();self.p.discover();result=answer()
        result.evidence=[e for e in result.evidence if e.supports!='outcome']
        self.p.client.extract=AsyncMock(return_value=(result,{}))
        asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'retry_wait')
        self.assertEqual(self.customer()['calls'][0]['outcome'],'call_completed')

    def test_no_commitment_cannot_keep_conflicting_payment_plan(self):
        self.call();self.p.discover();result=answer();result.outcome='no_commitment'
        self.p.client.extract=AsyncMock(return_value=(result,{}))
        asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'retry_wait')

    def test_failures_stop_at_three_attempts_without_a_review_gate(self):
        self.call();self.p.discover();self.p.client.extract=AsyncMock(side_effect=TemporaryFailure('Timed out'))
        for _ in range(4):
            with self.h.store.connect() as db:db.execute("UPDATE conversation_analyses SET next_attempt_at='' ")
            asyncio.run(self.p.run_next())
        self.assertEqual(self.p.client.extract.await_count,3)
        self.assertEqual(self.status()['status'],'failed')
        self.assertEqual(self.customer()['status'],'active')

    def test_completed_calls_are_discovered_after_restart(self):
        self.call()
        restarted=create_harness(self.path,SECRET,background=False).state.harness
        restarted.extraction.discover()
        self.assertEqual(self.status()['status'],'queued')

    def test_missing_transcript_skips_model(self):
        self.call()
        with self.h.store.connect() as db:db.execute("UPDATE demo_calls SET transcript='[]'")
        self.p.client.extract=AsyncMock();self.p.discover();asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'skipped');self.p.client.extract.assert_not_awaited()

    def test_customer_can_confirm_the_amount_stated_by_the_assistant(self):
        self.call();self.p.discover()
        result=answer()
        result.evidence=[e for e in result.evidence if not(e.supports=='amount' and e.turn==2)]
        self.p.client.extract=AsyncMock(return_value=(result,{}))
        asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'completed')
        self.assertEqual(self.customer()['calls'][0]['payment_details']['promised_amount'],'18000')

    def test_unmatched_evidence_never_changes_outcome_or_schedule(self):
        self.call();due=self.customer()['job']['due_at']
        bad=answer();bad.evidence[1].quote='Invented promise'
        self.p.client.extract=AsyncMock(return_value=(bad,{}));self.p.discover();asyncio.run(self.p.run_next())
        self.assertEqual(self.status()['status'],'retry_wait')
        self.assertEqual(self.customer()['job']['due_at'],due)
        self.assertEqual(self.customer()['calls'][0]['outcome'],'call_completed')

    def test_payment_during_extraction_still_controls_balance(self):
        self.call();self.p.discover()
        async def provider(source):
            self.client.post('/api/payments',headers={'X-Demo-Console':'1'},json={'customer_id':'rahul','amount':'18000','reference':'paid-during-analysis'})
            return answer(),{}
        self.p.client.extract=provider;asyncio.run(self.p.run_next());c=self.customer()
        self.assertEqual(c['balance'],0);self.assertEqual(c['status'],'cleared');self.assertEqual(c['job']['status'],'cancelled')

    def test_old_result_never_changes_newer_customer_schedule(self):
        self.call(call_id='old');self.p.queue('old');self.call(call_id='new')
        due=self.customer()['job']['due_at']
        self.p.client.extract=AsyncMock(return_value=(answer(),{}));asyncio.run(self.p.run_next())
        self.assertEqual(self.customer()['job']['due_at'],due)
        self.assertEqual(self.customer()['calls'][0]['outcome'],'call_completed')

    def test_secret_tokens_are_not_sent_to_gemini(self):
        self.call();self.p.discover()
        with self.h.store.connect() as db:
            source=db.execute('SELECT input_json FROM conversation_analyses').fetchone()[0]
        self.assertNotIn('case_token',source);self.assertNotIn('fake-key',source)
        self.assertNotIn(SECRET,source)

    def test_callback_window_keeps_the_followup_in_the_requested_week(self):
        self.call()
        with self.h.store.connect() as db:db.execute("UPDATE demo_calls SET transcript=? WHERE id='call'",(json.dumps([{'role':'user','content':'Please call next week.'}]),))
        self.p.discover()
        today=now().astimezone(IST).date();start=today+timedelta(days=7-today.weekday())
        result=Result(summary='Customer asked for next week.',next_step='Call in the requested week.',outcome='callback_requested',promised_amount='',payment_date_start='',payment_date_end='',payment_timeframe='',callback_at='',callback_date_start=start.isoformat(),callback_date_end=(start+timedelta(days=6)).isoformat(),callback_timeframe='Next week',payment_reference='',evidence=[{'turn':1,'quote':'Please call next week.','supports':'outcome'},{'turn':1,'quote':'Please call next week.','supports':'callback'}])
        self.p.client.extract=AsyncMock(return_value=(result,{}))
        asyncio.run(self.p.run_next())
        self.assertGreaterEqual(self.customer()['job']['due_at'][:10],start.isoformat())
        self.assertLessEqual(self.customer()['job']['due_at'][:10],(start+timedelta(days=6)).isoformat())

    def test_provider_schema_and_error_handling(self):
        def success(request):
            body=json.loads(request.content)
            self.assertIn('responseJsonSchema',body['generationConfig'])
            self.assertIn('customer_details',body['generationConfig']['responseJsonSchema']['properties'])
            self.assertEqual(body['generationConfig']['thinkingConfig']['thinkingLevel'],'LOW')
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':answer().model_dump_json()}]}}],'responseId':'provider-test'})
        with patch('payment_reminder.analysis.httpx.AsyncClient',return_value=httpx.AsyncClient(transport=httpx.MockTransport(success))):
            result,metadata=asyncio.run(Gemini('fake','gemini-3.8-flash').extract({}))
        self.assertEqual(result.outcome,'promise_to_pay');self.assertEqual(metadata['response_id'],'provider-test')
        for code in (401,429,503):
            with patch('payment_reminder.analysis.httpx.AsyncClient',return_value=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(code)))):
                with self.assertRaises(RuntimeError) as caught:asyncio.run(Gemini('fake','model').extract({}))
            self.assertEqual(isinstance(caught.exception,TemporaryFailure),code!=401)

class BackgroundAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_extraction_does_not_delay_next_call_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_harness(Path(folder)/'test.sqlite',SECRET,api_key='fake')
            h=app.state.harness;h.prepare('rahul','first');h.prepare('priya','second')
            for cid in ('first','second'):
                with h.store.connect() as db:db.execute("INSERT INTO tool_receipts(call_id,tool,status,created_at) VALUES(?,'context','ready',?)",(cid,now().isoformat()))
            extracting=asyncio.Event();second_ended=asyncio.Event();release=asyncio.Event()
            async def upstream(method,path):
                if path.endswith('second') and not extracting.is_set():return {'status':'running'}
                if path.endswith('second'):second_ended.set()
                return {'status':'completed','transcript':[{'role':'assistant','content':'The amount is 18000.'},{'role':'user','content':'I will pay 18000 in two to three days.'}]}
            async def extract(source):
                extracting.set();await release.wait();return answer(),{}
            h.upstream=upstream;h.extraction.client.extract=extract
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(extracting.wait(),3)
                await asyncio.wait_for(second_ended.wait(),5)
                with h.store.connect() as db:
                    self.assertEqual(db.execute("SELECT processed FROM demo_calls WHERE id='second'").fetchone()[0],1)
                    self.assertEqual(db.execute("SELECT status FROM demo_customers WHERE id='rahul'").fetchone()[0],'active')
                release.set()

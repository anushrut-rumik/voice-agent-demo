"""Phone contract tests. Every provider response is local; no number is dialed."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from payment_reminder.harness import create_harness, UpstreamRejected

SECRET='test-only-'+40*'x'
HEADERS={'X-Demo-Console':'1'}
NUMBER='+919876543210'
CALL='01a14689-91b5-7010-853d-641d487819f3'


class PhoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'phone.sqlite'
        self.app=create_harness(self.path,SECRET,'test-key',background=False)
        self.h=self.app.state.harness
        self.client=TestClient(self.app)
        self.tools=TestClient(self.h.tool_app)
        self.body={'customer_id':'rahul','to_number':NUMBER,'request_id':str(uuid4())}
        self.dials=[]

    def tearDown(self):
        self.tools.close();self.client.close();self.tmp.cleanup()

    def context(self, number=NUMBER, call_id=''):
        return self.tools.get('/voice/reminders/context',params={'phone_number':number,'agent_id':'ua_afe624fa','call_id':call_id},headers={'Authorization':'Bearer '+SECRET})

    def post(self, body=None):
        return self.client.post('/api/phone',json=body or self.body,headers=HEADERS)

    def upstream(self, error=None):
        async def call(method,path,body=None,auth=True):
            if method=='POST':
                self.assertEqual(path,'/v1/calls');self.dials.append(body)
                # Silk's phone tool runs BEFORE its run/call ID is allocated.
                ctx=self.context().json()
                self.assertEqual(ctx['customer_name'],'Rahul Sharma')
                self.assertEqual(ctx['context_status'],'ready')
                if error:raise error
                return {'callId':CALL,'toNumber':NUMBER,'status':'calling'}
            return {'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        self.h.upstream=call

    def customer(self, cid='rahul'):
        return next(c for c in self.h.state()['customers'] if c['id']==cid)

    def test_phone_uses_deployed_agent_and_exact_selected_brief(self):
        self.upstream();r=self.post()
        self.assertEqual(r.status_code,202,r.text)
        self.assertEqual(r.json()['callId'],CALL)
        self.assertNotIn('token',r.json());self.assertNotIn('host',r.json())
        call=self.customer()['calls'][0]
        self.assertEqual(call['channel'],'phone');self.assertEqual(call['to_number'],NUMBER)
        self.assertTrue(call['context_loaded'])
        with self.h.store.connect() as db:
            self.assertEqual(db.execute('SELECT call_id FROM dispatches').fetchone()[0],CALL)

    def test_number_validation_before_any_dial(self):
        self.upstream()
        for number in ['9876543210','+00000000','+91<script>','+123','+1234567890123456']:
            self.assertEqual(self.post(self.body|{'to_number':number}).status_code,422)
        self.assertEqual(self.dials,[])
        self.assertEqual(self.customer()['calls'],[])
        self.assertEqual(self.post(self.body|{'to_number':'+91 (98765) 43210'}).status_code,202)
        self.assertEqual(self.dials[0]['toNumber'],NUMBER)

    def test_repeated_request_never_dials_twice_even_after_completion(self):
        self.upstream();self.post();asyncio.run(self.h.poll())
        self.assertEqual(self.customer()['calls'][0]['status'],'completed')
        self.assertEqual(self.post().json()['callId'],CALL)
        self.assertEqual(len(self.dials),1)
        self.assertEqual(self.post(self.body|{'customer_id':'priya'}).status_code,409)

    def test_active_phone_blocks_phone_and_browser_calls(self):
        self.upstream();self.post()
        self.assertEqual(self.post(self.body|{'request_id':str(uuid4())}).status_code,409)
        self.assertEqual(self.client.post('/api/live',json={'customer_id':'priya'},headers=HEADERS).status_code,409)
        self.assertEqual(len(self.dials),1)

    def test_suppressed_number_cannot_be_reused_for_another_account(self):
        with self.h.store.connect() as db:
            db.execute('INSERT INTO contact_blocks VALUES(?,?,?)',('demo-supplies',NUMBER,'do_not_call'))
        self.upstream();self.assertEqual(self.post().status_code,409);self.assertEqual(self.dials,[])

    def test_held_or_low_balance_accounts_cannot_dial(self):
        self.upstream()
        self.assertEqual(self.post(self.body|{'customer_id':'neha'}).status_code,409)
        with self.h.store.connect() as db:db.execute("UPDATE demo_customers SET status='paused' WHERE id='rahul'")
        self.assertEqual(self.post().status_code,409);self.assertEqual(self.dials,[])

    def test_timeout_is_durable_and_duplicate_request_does_not_redial(self):
        self.upstream(HTTPException(502,'Timeout'))
        r=self.post();self.assertEqual(r.json()['status'],'uncertain')
        self.assertEqual(self.post().json()['status'],'uncertain');self.assertEqual(len(self.dials),1)
        restored=create_harness(self.path,SECRET,'test-key',background=False).state.harness
        self.assertEqual(restored.state()['customers'][0]['calls'][0]['status'],'uncertain')
        async def never(*args,**kwargs):raise AssertionError('Must not redial after restart')
        restored.upstream=never
        from payment_reminder.harness import PhoneSelection
        result=asyncio.run(restored.phone(PhoneSelection(**self.body)))
        self.assertEqual(result['status'],'uncertain')

    def test_explicit_rejection_releases_reservation(self):
        self.upstream(UpstreamRejected(502,'Rumik rejected the call'))
        self.assertEqual(self.post().status_code,502)
        call=self.customer()['calls'][0]
        self.assertEqual(call['status'],'failed');self.assertEqual(call['processed'],1)
        self.assertEqual(self.post().json()['status'],'failed');self.assertEqual(len(self.dials),1)

    def test_server_error_keeps_reservation(self):
        self.upstream(HTTPException(502,'Server error'))
        self.assertEqual(self.post().json()['status'],'uncertain')
        self.assertEqual(self.post(self.body|{'request_id':str(uuid4())}).status_code,409)

    def test_exact_token_history_recovers_a_lost_dial_response(self):
        self.upstream(HTTPException(502,'Timeout'));self.post()
        with self.h.store.connect() as db:token=db.execute('SELECT token FROM demo_calls').fetchone()[0]
        async def history(method,path,body=None,auth=True):
            if '?' in path:return {'items':[
                {'id':str(uuid4()),'channel':'phone','agentSlug':'ua_afe624fa','systemInstruction':'A nearby unrelated call'},
                {'id':CALL,'channel':'phone','agentSlug':'ua_afe624fa','systemInstruction':'Internal case token: '+token}]}
            return {'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        self.h.upstream=history;asyncio.run(self.h.poll())
        call=self.customer()['calls'][0]
        self.assertEqual(call['id'],CALL);self.assertEqual(call['status'],'completed')
        self.assertTrue(call['context_loaded']);self.assertEqual(call['outcome'],'call_completed')
        self.assertEqual(self.post().json()['callId'],CALL);self.assertEqual(len(self.dials),1)

    def test_unrelated_history_never_resolves_an_uncertain_attempt(self):
        self.upstream(HTTPException(502,'Timeout'));self.post()
        async def history(*args,**kwargs):return {'items':[{'id':CALL,'channel':'phone','agentSlug':'ua_afe624fa','systemInstruction':'Different customer'}]}
        self.h.upstream=history;asyncio.run(self.h.poll())
        self.assertEqual(self.customer()['calls'][0]['status'],'uncertain')

    def test_during_call_outcome_can_recover_provider_id(self):
        self.upstream(HTTPException(502,'Timeout'));self.post()
        with self.h.store.connect() as db:token=db.execute('SELECT token FROM demo_calls').fetchone()[0]
        r=self.tools.post('/voice/reminders/outcomes',json={'case_token':token,'call_id':CALL,'outcome':'do_not_call'},headers={'Authorization':'Bearer '+SECRET})
        self.assertEqual(r.status_code,200)
        async def ended(*args,**kwargs):return {'status':'completed','transcript':[{'role':'user','content':'Stop calling.'}]}
        self.h.upstream=ended;asyncio.run(self.h.poll())
        self.assertEqual(self.customer()['status'],'suppressed')
        self.assertEqual(self.customer()['calls'][0]['id'],CALL)

    def test_reusing_number_gets_only_the_new_dispatch(self):
        self.upstream();self.post();asyncio.run(self.h.poll())
        second=str(uuid4())
        self.h.prepare('priya','phone-'+second,to_number=NUMBER,request_id=second)
        result=self.context().json()
        self.assertEqual(result['customer_name'],'Priya Mehta')
        self.assertNotEqual(result['case_token'],self.customer()['calls'][0].get('token'))

    def test_optional_caller_id_is_sent_from_server_settings(self):
        self.h.from_number='+912233445566';self.upstream();self.post()
        self.assertEqual(self.dials[0]['fromNumber'],'+912233445566')

    def test_phone_route_uses_console_access_protection(self):
        r=self.client.post('/api/phone',json=self.body)
        self.assertEqual(r.status_code,403);self.assertEqual(self.customer()['calls'],[])


if __name__=='__main__':unittest.main()

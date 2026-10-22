"""CRM runs use a fake carrier and isolated database. No live numbers are dialed."""
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from payment_reminder.analysis import Result
from payment_reminder.harness import create_harness,IST,Entry,UpstreamRejected
from payment_reminder.runs import RunRequest

SECRET='test-only-'+40*'x'
HEADERS={'X-Demo-Console':'1'}


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'runs.sqlite'
        self.app=create_harness(self.path,SECRET,'fake-carrier',background=False,gemini_key='fake-gemini')
        self.h=self.app.state.harness;self.r=self.h.runs;self.at=datetime(2026,10,22,11,tzinfo=IST)
        self.h.clock=lambda db:self.at
        self.client=TestClient(self.app);self.tools=TestClient(self.h.tool_app)
        self.r.check_setup=AsyncMock(return_value={'status':'ready'})
        self.dials=[];self.remote={}
        for cid,num in [('rahul','+12025550101'),('priya','+12025550102'),('arjun','+12025550103')]:self.r.contact(cid,num)
        with self.h.store.connect() as db:db.execute("UPDATE demo_jobs SET status='ready',due_at=?",((self.at-timedelta(hours=1)).isoformat(),))
        async def carrier(method,path,body=None,auth=True):
            if method=='POST':
                ctx=self.tools.get('/voice/reminders/context',params={'agent_id':'ua_afe624fa','phone_number':body['toNumber']},headers={'Authorization':'Bearer '+SECRET})
                self.assertEqual(ctx.json()['context_status'],'ready')
                cid=str(uuid4());self.dials.append((cid,body,ctx.json()))
                self.remote[cid]={'status':'running','transcript':[]}
                return {'callId':cid,'toNumber':body['toNumber'],'status':'calling'}
            return self.remote[path.rsplit('/',1)[1]]
        self.h.upstream=carrier
        async def extract(source):
            return Result(summary='Customer is not sure when they can pay.',next_step='Follow up on the next working day.',outcome='no_commitment',promised_amount='',payment_date_start='',payment_date_end='',payment_timeframe='',callback_at='',payment_reference='',evidence=[{'turn':1,'quote':'Not sure yet.','supports':'outcome'}]),{'model':'test-model'}
        self.h.extraction.client.extract=extract

    async def asyncTearDown(self):
        self.client.close();self.tools.close();self.tmp.cleanup()

    async def start(self):return await self.r.start(RunRequest(request_id=uuid4()))
    def snapshot(self):
        with self.h.store.connect() as db:return self.r.state(db)
    async def finish(self):
        cid=self.dials[-1][0];self.remote[cid]={'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        await self.h.poll();self.h.extraction.discover();await self.h.extraction.run_next()

    async def test_one_click_calls_each_due_customer_with_their_own_context_and_brief(self):
        run=await self.start();self.assertEqual(run['queued'],3)
        for n in range(3):
            await self.r.step();self.assertEqual(len(self.dials),n+1)
            await self.r.step();self.assertEqual(len(self.dials),n+1)
            await self.finish()
        await self.r.step();state=self.snapshot()['run']
        self.assertEqual(state['status'],'completed');self.assertEqual(state['finished'],3)
        self.assertEqual({d[2]['customer_name'] for d in self.dials},{'Rahul Sharma','Priya Mehta','Arjun Patel'})
        with self.h.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_analyses WHERE status='completed'").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_jobs WHERE status='scheduled'").fetchone()[0],3)
        await self.r.step();self.assertEqual(len(self.dials),3)
        self.assertEqual(next(c for c in self.h.state()['customers'] if c['id']=='priya')['calls'][0]['next_step'],'Follow up on the next working day.')

    async def test_rapid_repeated_start_and_restart_do_not_duplicate_a_dial(self):
        body=RunRequest(request_id=uuid4());first=await self.r.start(body);again=await self.r.start(body)
        self.assertEqual(first['id'],again['id']);await self.r.step()
        restored=create_harness(self.path,SECRET,'fake',background=False,gemini_key='fake').state.harness
        restored.clock=lambda db:self.at
        restored.phone=AsyncMock(side_effect=AssertionError('Must not redial an active saved attempt'))
        await restored.runs.step();restored.phone.assert_not_awaited()
        self.assertEqual(self.snapshot()['run']['total'],3)

    async def test_next_number_does_not_wait_for_previous_brief(self):
        await self.start();await self.r.step()
        cid=self.dials[-1][0]
        self.remote[cid]={'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        await self.h.poll();self.h.extraction.discover()
        await self.r.step()
        items=self.snapshot()['run']['items']
        self.assertEqual(items[0]['status'],'summarizing')
        self.assertEqual(len(self.dials),2)
        self.assertNotEqual(self.dials[0][1]['toNumber'],self.dials[1][1]['toNumber'])
        await self.r.step();self.assertEqual(len(self.dials),2)

    async def test_pending_brief_holds_same_number_but_not_other_numbers(self):
        self.r.contact('priya','+12025550101')
        await self.start();await self.r.step()
        cid=self.dials[-1][0]
        self.remote[cid]={'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        await self.h.poll();self.h.extraction.discover();await self.r.step()
        self.assertEqual([d[2]['customer_name'] for d in self.dials],['Rahul Sharma','Arjun Patel'])
        await self.finish();await self.r.step()
        # finish() saved Rahul's earlier queued brief. Priya is now safe to call.
        self.assertEqual(self.dials[-1][2]['customer_name'],'Priya Mehta')

    async def test_individual_call_can_repeat_before_the_next_follow_up(self):
        body=RunRequest(request_id=uuid4(),customer_id='rahul')
        await self.r.start(body);await self.r.step();await self.finish();await self.r.step()
        self.assertEqual(self.snapshot()['run']['status'],'completed')
        with self.h.store.connect() as db:
            scheduled=dict(db.execute("SELECT * FROM demo_jobs WHERE customer='rahul'").fetchone())
        self.assertGreater(datetime.fromisoformat(scheduled['due_at']),self.at)
        repeat=RunRequest(request_id=uuid4(),customer_id='rahul')
        result=await self.r.start(repeat)
        self.assertEqual(result['queued'],1)
        with self.h.store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM demo_jobs WHERE customer='rahul'").fetchone()),scheduled)
        await self.r.step();await self.r.step()
        self.assertEqual(len(self.dials),2)
        self.assertEqual([d[2]['customer_name'] for d in self.dials],['Rahul Sharma','Rahul Sharma'])
        self.assertNotEqual(self.dials[0][0],self.dials[1][0])
        self.assertIn('Not sure yet.',self.dials[1][2]['prior_contact_summary'])
        self.assertEqual((await self.r.start(repeat))['id'],result['id'])
        self.assertEqual(len(self.dials),2)

    async def test_individual_future_call_survives_restart_without_calling_other_customers(self):
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_jobs SET due_at=? WHERE customer='priya'",((self.at+timedelta(days=3)).isoformat(),))
        body=RunRequest(request_id=uuid4(),customer_id='priya')
        first=await self.r.start(body)
        restored=create_harness(self.path,SECRET,'fake',background=False,gemini_key='fake').state.harness
        restored.clock=lambda db:self.at;restored.upstream=self.h.upstream
        restored.runs.check_setup=AsyncMock(return_value={'status':'ready'})
        self.assertEqual((await restored.runs.start(body))['id'],first['id'])
        await restored.runs.step();await restored.runs.step()
        self.assertEqual(len(self.dials),1)
        self.assertEqual(self.dials[0][2]['customer_name'],'Priya Mehta')
        self.assertEqual(self.snapshot()['run']['total'],1)

    async def test_individual_call_still_respects_balance_stops_and_missing_contact(self):
        self.h.entry(Entry(customer_id='rahul',amount='18000',reference='paid-individual'),'payment')
        with self.assertRaises(HTTPException):
            await self.r.start(RunRequest(request_id=uuid4(),customer_id='rahul'))
        for status in ('suppressed','paused'):
            with self.h.store.connect() as db:db.execute("UPDATE demo_customers SET status=? WHERE id='priya'",(status,))
            with self.assertRaises(HTTPException):
                await self.r.start(RunRequest(request_id=uuid4(),customer_id='priya'))
        self.r.contact('arjun','')
        with self.assertRaisesRegex(HTTPException,'phone number'):
            await self.r.start(RunRequest(request_id=uuid4(),customer_id='arjun'))
        self.r.contact('arjun','+12025550103')
        with self.h.store.connect() as db:
            db.execute('INSERT INTO contact_blocks VALUES(?,?,?)',('demo-supplies','+12025550103','do_not_call'))
        with self.assertRaises(HTTPException):
            await self.r.start(RunRequest(request_id=uuid4(),customer_id='arjun'))
        self.r.contact('neha','+12025550104')
        with self.assertRaises(HTTPException):
            await self.r.start(RunRequest(request_id=uuid4(),customer_id='neha'))
        self.assertEqual(self.dials,[])

    async def test_individual_schedule_change_before_dial_is_rechecked(self):
        await self.r.start(RunRequest(request_id=uuid4(),customer_id='priya'))
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_jobs SET due_at=? WHERE customer='priya'",((self.at+timedelta(days=3)).isoformat(),))
        await self.r.step();await self.r.step()
        self.assertEqual(self.dials,[])
        self.assertEqual(self.snapshot()['run']['status'],'completed')
        self.assertEqual(self.snapshot()['run']['items'][0]['status'],'skipped')

    async def test_bulk_includes_future_follow_ups(self):
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_jobs SET due_at=? WHERE customer='priya'",((self.at+timedelta(days=3)).isoformat(),))
        run=await self.start();self.assertEqual(run['queued'],3)
        self.assertIn('priya',[i['customer'] for i in self.snapshot()['run']['items']])

    async def test_completed_bulk_run_can_repeat_every_customer_when_none_are_due(self):
        first=await self.start()
        for _ in range(3):
            await self.r.step();await self.finish()
        await self.r.step()
        self.assertEqual(self.snapshot()['run']['status'],'completed')
        self.assertEqual(self.snapshot()['ready_count'],0)
        self.assertEqual(self.snapshot()['callable_count'],3)
        second=await self.start()
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(second['queued'],3)
        for _ in range(3):
            await self.r.step();await self.finish()
        await self.r.step()
        self.assertEqual(len(self.dials),6)
        self.assertEqual(len({d[0] for d in self.dials}),6)
        for name in ('Rahul Sharma','Priya Mehta','Arjun Patel'):
            self.assertEqual(sum(d[2]['customer_name']==name for d in self.dials),2)
        self.assertEqual(self.snapshot()['run']['status'],'completed')

    async def test_individual_api_queues_one_saved_contact_without_dialing_on_request(self):
        self.r.contact('rahul','')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_jobs SET due_at=? WHERE customer='priya'",((self.at+timedelta(days=3)).isoformat(),))
        body={'customer_id':'priya','request_id':str(uuid4())}
        response=self.client.post('/api/reminders/start',json=body,headers=HEADERS)
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(response.json()['queued'],1)
        self.assertEqual(self.dials,[])
        await self.r.step()
        self.assertEqual(self.dials[0][1]['toNumber'],'+12025550102')
        self.assertEqual(self.client.post('/api/reminders/start',json=body,headers=HEADERS).json()['id'],response.json()['id'])

    async def test_individual_request_cannot_be_reused_for_another_customer(self):
        request_id=uuid4()
        await self.r.start(RunRequest(request_id=request_id,customer_id='priya'))
        with self.assertRaises(HTTPException):
            await self.r.start(RunRequest(request_id=request_id,customer_id='rahul'))

    async def test_finishing_run_blocks_a_new_individual_call(self):
        run=await self.start();await self.r.step();await self.r.control(run['id'],'cancel')
        with self.assertRaises(HTTPException):
            await self.r.start(RunRequest(request_id=uuid4(),customer_id='priya'))
        self.assertEqual(len(self.dials),1)

    async def test_stopped_and_missing_contacts_are_excluded_but_future_accounts_are_included(self):
        self.r.contact('rahul','')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_customers SET status='suppressed' WHERE id='priya'")
            db.execute("UPDATE demo_jobs SET status='scheduled',due_at=? WHERE customer='arjun'",((self.at+timedelta(days=2)).isoformat(),))
        run=await self.start()
        self.assertEqual(run['queued'],1)
        self.assertEqual(self.snapshot()['run']['items'][0]['customer'],'arjun')
        self.assertEqual(self.snapshot()['missing_phone_count'],1)
        self.assertEqual(self.dials,[])

    async def test_payment_and_contact_change_before_dial_are_rechecked(self):
        await self.start()
        self.h.entry(Entry(customer_id='rahul',amount='18000',reference='paid'),'payment')
        await self.r.step();self.assertEqual(self.dials,[])
        self.r.contact('priya','+12025550104');await self.r.step();self.assertEqual(self.dials,[])
        await self.r.step();self.assertEqual(len(self.dials),1)
        self.assertEqual(self.dials[0][2]['customer_name'],'Arjun Patel')

    async def test_pause_finishes_active_call_and_waits_before_next_customer(self):
        run=await self.start();await self.r.step();await self.r.control(run['id'],'pause')
        await self.finish();await self.r.step();self.assertEqual(len(self.dials),1)
        self.assertEqual(self.snapshot()['run']['status'],'paused')
        await self.r.control(run['id'],'resume');await self.r.step();self.assertEqual(len(self.dials),2)

    async def test_cancelling_run_never_starts_remaining_customers(self):
        run=await self.start();await self.r.step();await self.r.control(run['id'],'cancel')
        await self.finish();await self.r.step()
        self.assertEqual(len(self.dials),1);self.assertEqual(self.snapshot()['run']['status'],'completed')
        self.assertEqual([i['status'] for i in self.snapshot()['run']['items']].count('cancelled'),2)

    async def test_uncertain_dial_blocks_the_rest_without_retry(self):
        async def failed(*args,**kwargs):self.dials.append('uncertain');raise HTTPException(502,'Timeout')
        self.h.upstream=failed
        await self.start();await self.r.step();await self.r.step();await self.r.step()
        self.assertEqual(self.dials,['uncertain']);self.assertEqual(self.snapshot()['run']['items'][0]['status'],'uncertain')

    async def test_setup_failure_pauses_without_dialing(self):
        self.r.check_setup=AsyncMock(side_effect=HTTPException(409,'Tunnel unavailable'))
        run=await self.start();await self.r.step()
        self.assertEqual(self.snapshot()['run']['status'],'paused');self.assertEqual(self.dials,[])
        self.r.check_setup=AsyncMock();await self.r.control(run['id'],'resume');await self.r.step()
        self.assertEqual(len(self.dials),1)

    async def test_unfunded_provider_pauses_the_remaining_run(self):
        async def rejected(*args,**kwargs):raise UpstreamRejected(502,'Insufficient credit',402)
        self.h.upstream=rejected;await self.start();await self.r.step()
        self.assertEqual(self.snapshot()['run']['status'],'paused')
        self.assertEqual(self.snapshot()['run']['items'][1]['status'],'queued')

    async def test_demo_click_starts_immediately_after_hours(self):
        self.at=self.at.replace(hour=21);await self.start();await self.r.step()
        self.assertEqual(len(self.dials),1)
        start=datetime.fromisoformat(self.snapshot()['run']['starts_at'])
        self.assertEqual(start,self.at)

    async def test_failed_brief_is_visible_and_does_not_block_remaining_customers(self):
        await self.start();await self.r.step();await self.finish()
        with self.h.store.connect() as db:db.execute("UPDATE conversation_analyses SET status='failed',error='Provider unavailable'")
        await self.r.step();self.assertEqual(len(self.dials),2)
        call=next(c for c in self.h.state()['customers'] if c['id']=='rahul')['calls'][0]
        self.assertEqual(call['brief_status'],'failed')

    async def test_add_customer_requires_a_bill_before_they_can_enter_a_run(self):
        response=self.client.post('/api/customers',json={'name':'New customer','company':'New company','phone':'+12025550109'},headers=HEADERS)
        self.assertEqual(response.status_code,201)
        cid=response.json()['id']
        account=next(c for c in self.h.state()['customers'] if c['id']==cid)
        self.assertEqual(account['balance'],0);self.assertFalse(account['ready_for_reminder'])
        self.h.entry(Entry(customer_id=cid,amount='6000',reference='new-invoice'),'invoice')
        self.assertTrue(next(c for c in self.h.state()['customers'] if c['id']==cid)['ready_for_reminder'])

    async def test_transcript_stop_blocks_same_number_for_other_customer(self):
        self.r.contact('priya','+12025550101')
        await self.start();await self.r.step()
        cid=self.dials[0][0]
        self.remote[cid]={'status':'completed','transcript':[{'role':'user','content':'Stop calling this number.'}]}
        await self.h.poll();self.h.extraction.discover()
        result=Result(summary='Customer requested no more calls.',next_step='Stop future reminders.',outcome='do_not_call',promised_amount='',payment_date_start='',payment_date_end='',payment_timeframe='',callback_at='',payment_reference='',evidence=[{'turn':1,'quote':'Stop calling this number.','supports':'outcome'}])
        self.h.extraction.client.extract=AsyncMock(return_value=(result,{}))
        await self.h.extraction.run_next();await self.r.step()
        self.assertEqual(len(self.dials),1)
        self.assertEqual(self.snapshot()['run']['items'][1]['status'],'skipped')
        self.assertTrue(next(c for c in self.h.state()['customers'] if c['id']=='priya')['contact_blocked'])

    async def test_pause_during_setup_prevents_the_pending_dial(self):
        run=await self.start()
        async def setup():await self.r.control(run['id'],'pause')
        self.r.check_setup=setup
        await self.r.step()
        self.assertEqual(self.dials,[]);self.assertEqual(self.snapshot()['run']['status'],'paused')
        self.assertEqual(self.snapshot()['run']['items'][0]['status'],'queued')

    async def test_contact_changed_during_setup_is_not_called_at_old_number(self):
        await self.start()
        async def setup():self.r.contact('rahul','+12025550109')
        self.r.check_setup=setup
        await self.r.step()
        self.assertEqual(self.dials,[])
        self.assertEqual(self.snapshot()['run']['items'][0]['status'],'skipped')

    async def test_cancelling_during_setup_prevents_every_dial(self):
        run=await self.start()
        async def setup():await self.r.control(run['id'],'cancel')
        self.r.check_setup=setup
        await self.r.step();await self.r.step()
        self.assertEqual(self.dials,[]);self.assertEqual(self.snapshot()['run']['status'],'completed')

    async def test_cancel_recovers_an_interrupted_reservation_before_any_dial(self):
        run=await self.start()
        with self.h.store.connect() as db:db.execute("UPDATE reminder_run_items SET status='dialing' WHERE rowid=(SELECT MIN(rowid) FROM reminder_run_items)")
        await self.r.control(run['id'],'cancel');await self.r.step()
        self.assertEqual(self.dials,[]);self.assertEqual(self.snapshot()['run']['status'],'completed')
        self.assertTrue(all(i['status']=='cancelled' for i in self.snapshot()['run']['items']))

    async def test_local_preview_cannot_start_a_run_or_a_phone_call(self):
        from payment_reminder.harness import PhoneSelection
        self.h.callbacks_enabled=False
        with self.assertRaises(HTTPException):await self.start()
        with self.assertRaises(HTTPException):await self.h.phone(PhoneSelection(customer_id='rahul',to_number='+12025550101',request_id=uuid4()))
        self.assertEqual(self.dials,[])
        self.assertFalse(self.snapshot()['configured'])

    async def test_contact_endpoint_does_not_call_and_existing_test_number_is_not_a_contact(self):
        self.r.contact('rahul','')
        self.assertEqual(next(c for c in self.h.state()['customers'] if c['id']=='rahul')['phone'],'')
        response=self.client.post('/api/customers/rahul/contact',json={'phone':'+1 (202) 555-0101'},headers=HEADERS)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['phone'],'+12025550101')
        self.assertEqual(self.dials,[])
        self.assertEqual(self.client.post('/api/reminders/start',json={'request_id':str(uuid4())}).status_code,403)


if __name__=='__main__':unittest.main()

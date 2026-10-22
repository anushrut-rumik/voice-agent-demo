"""Local scheduling and connection regressions. No provider calls are made."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from payment_reminder.harness import create_harness
from payment_reminder.readiness import check_setup


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_harness(Path(self.tmp.name)/'test.sqlite','test-only-'+40*'x',
                                'fake-key',background=True)
        self.h=self.app.state.harness

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_thread_wake_runs_worker_before_periodic_sleep_expires(self):
        entered=asyncio.Event()
        self.h.poll=AsyncMock()
        self.h.extraction.discover=Mock()
        self.h.extraction.run_next=AsyncMock()
        self.h.runs.step=AsyncMock(side_effect=entered.set)
        async with self.app.router.lifespan_context(self.app):
            await asyncio.wait_for(entered.wait(),1)
            entered.clear()
            await asyncio.to_thread(self.h.wake,self.h.runs_wake)
            await asyncio.wait_for(entered.wait(),.5)
            self.assertGreaterEqual(self.h.runs.step.await_count,2)

    async def test_server_reuses_client_and_closes_it_after_workers_stop(self):
        self.h.poll=AsyncMock();self.h.runs.step=AsyncMock()
        self.h.extraction.discover=Mock();self.h.extraction.run_next=AsyncMock()
        async with self.app.router.lifespan_context(self.app):
            client=self.h._upstream_client
            with patch.object(client,'request',new=AsyncMock(return_value=httpx.Response(200,json={'ok':True}))) as request:
                await self.h.upstream('GET','/v1/agents/test')
                await self.h.upstream('GET','/v1/tools')
                self.assertEqual(request.await_count,2)
                self.assertIs(self.h._upstream_client,client)
                self.assertFalse(client.is_closed)
        self.assertTrue(client.is_closed)
        self.assertIsNone(self.h._upstream_client)

    async def test_readiness_starts_both_independent_reads_together(self):
        started=set();both=asyncio.Event()
        async def upstream(method,path):
            started.add(path)
            if len(started)==2:both.set()
            await asyncio.wait_for(both.wait(),.5)
            return {'deployed':False} if '/agents/' in path else {'items':[]}
        self.h.upstream=upstream
        with self.assertRaises(HTTPException) as caught:
            await check_setup(self.h)
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(len(started),2)

    async def test_active_completion_is_processed_while_old_transcript_waits(self):
        self.h.prepare('rahul','old');self.h.prepare('priya','active')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET mode='live',status=CASE id WHEN 'old' THEN 'completed' ELSE 'running' END")
        old_started=asyncio.Event();release_old=asyncio.Event();active_processed=asyncio.Event()
        process=self.h.process
        def processed(call_id):
            process(call_id)
            if call_id=='active':active_processed.set()
        self.h.process=processed
        async def upstream(method,path):
            if path.endswith('/old'):
                old_started.set();await release_old.wait()
            return {'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        self.h.upstream=upstream
        task=asyncio.create_task(self.h.poll())
        try:
            await asyncio.wait_for(old_started.wait(),1)
            await asyncio.wait_for(active_processed.wait(),.5)
            with self.h.store.connect() as db:
                self.assertEqual(db.execute("SELECT processed FROM demo_calls WHERE id='active'").fetchone()[0],1)
        finally:
            release_old.set();await task
            await asyncio.gather(*self.h._call_polls.values())

    async def test_later_active_status_checks_do_not_wait_for_old_request(self):
        self.h.prepare('rahul','old');self.h.prepare('priya','active')
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_calls SET mode='live',status=CASE id WHEN 'old' THEN 'completed' ELSE 'running' END")
        release_old=asyncio.Event();reads={'old':0,'active':0}
        async def upstream(method,path):
            cid=path.rsplit('/',1)[1];reads[cid]+=1
            if cid=='old':await release_old.wait()
            return {'status':'running' if cid=='active' and reads[cid]==1 else 'completed',
                    'transcript':[{'role':'user','content':'Not sure yet.'}]}
        self.h.upstream=upstream
        try:
            await self.h.poll();await self.h.poll()
            self.assertEqual(reads,{'old':1,'active':2})
            with self.h.store.connect() as db:
                self.assertEqual(db.execute("SELECT processed FROM demo_calls WHERE id='active'").fetchone()[0],1)
        finally:
            release_old.set();await asyncio.gather(*self.h._call_polls.values())

    async def test_old_requests_leave_capacity_for_a_new_active_call(self):
        for n in range(4):
            self.h.prepare('rahul','old-'+str(n))
            with self.h.store.connect() as db:
                db.execute("UPDATE demo_calls SET mode='live',status='completed'")
        release=asyncio.Event();active_read=asyncio.Event();history_reads=0
        async def upstream(method,path):
            nonlocal history_reads
            if '/old-' in path:
                history_reads+=1;await release.wait()
            else:active_read.set()
            return {'status':'completed','transcript':[{'role':'user','content':'Not sure yet.'}]}
        self.h.upstream=upstream
        try:
            await self.h.poll()
            self.assertEqual(history_reads,2)
            self.h.prepare('priya','active')
            with self.h.store.connect() as db:
                db.execute("UPDATE demo_calls SET mode='live',status='running' WHERE id='active'")
            await self.h.poll()
            self.assertTrue(active_read.is_set())
        finally:
            release.set();await asyncio.gather(*self.h._call_polls.values())

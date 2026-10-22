"""Durable, one-pass reminder runs. The browser starts a run, never carries audio."""
import asyncio
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel,Field
from uuid import UUID

from payment_reminder.app import now


class Contact(BaseModel):
    phone: str


class NewCustomer(Contact):
    name: str = Field(min_length=1,max_length=100)
    company: str = Field(min_length=1,max_length=100)


class RunRequest(BaseModel):
    request_id: UUID
    customer_id: str | None = None


ITEM_END = ('completed', 'failed', 'skipped', 'cancelled')


class ReminderRuns:
    def __init__(self, harness):
        self.h = harness
        self.lock = asyncio.Lock()
        with self.h.store.connect() as db:
            if 'phone' not in {r[1] for r in db.execute('PRAGMA table_info(demo_customers)')}:
                db.execute("ALTER TABLE demo_customers ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS reminder_runs(
                    id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,scope TEXT NOT NULL,
                    status TEXT NOT NULL,starts_at TEXT NOT NULL,created_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
                    setup_checked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reminder_run_items(
                    id TEXT PRIMARY KEY,run_id TEXT NOT NULL,customer TEXT NOT NULL,
                    phone TEXT NOT NULL,job_due_at TEXT NOT NULL,status TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,error TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id,customer));
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_reminder_run
                    ON reminder_runs((1)) WHERE status IN ('running','paused');
            ''')
            # Initial bills used to be hidden until the next business-hours
            # window. The demo now starts these calls only when the admin clicks.
            if not db.execute("SELECT 1 FROM demo_meta WHERE key='immediate_demo_runs_v1'").fetchone():
                db.execute("UPDATE demo_jobs SET due_at=MIN(due_at,?) WHERE reason='Balance exceeds the INR 5,000 reminder threshold' AND status IN ('ready','scheduled')",(self.h.clock(db).isoformat(),))
                db.execute("INSERT INTO demo_meta VALUES('immediate_demo_runs_v1','1')")

    def add_customer(self, body):
        from payment_reminder.harness import phone_number
        if not body.name.strip() or not body.company.strip():raise HTTPException(422,'Enter the customer name and business.')
        number=phone_number(body.phone) if body.phone.strip() else ''
        cid='customer_'+uuid4().hex
        with self.h.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO demo_customers(id,name,company,threshold,status,phone) VALUES(?,?,?,?,?,?)',
                       (cid,body.name.strip(),body.company.strip(),0,'active',number))
            self.h.log(db,cid,'Customer added','Add an invoice to begin payment follow-ups.')
            self.h.reconcile(db,cid)
        return {'id':cid,'status':'saved'}

    def contact(self, cid, value):
        from payment_reminder.harness import phone_number, TERMINAL
        number = phone_number(value) if value.strip() else ''
        with self.h.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.h.customer(db, cid)
            if db.execute('SELECT 1 FROM demo_calls WHERE customer=? AND status NOT IN (?,?,?)',
                          (cid, *TERMINAL)).fetchone():
                raise HTTPException(409, 'Wait for this customer’s current call to finish before changing their number.')
            db.execute('UPDATE demo_customers SET phone=? WHERE id=?', (number, cid))
            self.h.log(db, cid, 'Contact updated', 'Customer phone number saved.' if number else 'Customer phone number removed.')
        return {'status': 'saved', 'phone': number}

    def due(self, db, at, cid=None, *, include_future=False):
        rows = db.execute('''SELECT c.*,j.due_at FROM demo_customers c JOIN demo_jobs j
            ON j.customer=c.id WHERE c.status='active' AND j.status IN ('ready','scheduled')
            ORDER BY j.due_at,c.rowid''').fetchall()
        return [r for r in rows if (cid is None or r['id'] == cid)
                and (include_future or datetime.fromisoformat(r['due_at']) <= at)
                and self.h.balance(db,r['id']) > r['threshold']
                and not db.execute('SELECT 1 FROM contact_blocks WHERE business=? AND phone=?',
                                   ('demo-supplies',r['phone'])).fetchone()]

    def state(self, db):
        at = self.h.clock(db)
        due = self.due(db, at)
        run = db.execute("SELECT * FROM reminder_runs ORDER BY rowid DESC LIMIT 1").fetchone()
        result = {'ready_count':sum(bool(r['phone']) for r in due),
                  'missing_phone_count':sum(not r['phone'] for r in due),
                  'configured':bool(self.h.api_key and self.h.extraction.client.key and self.h.callbacks_enabled),
                  'local_only':not self.h.callbacks_enabled,
                  'run':None, 'model':self.h.extraction.client.model}
        if run:
            item = dict(run)
            item['items'] = [dict(r) for r in db.execute('''SELECT i.customer,c.name,i.status,i.error,
                d.id AS call_id FROM reminder_run_items i JOIN demo_customers c ON c.id=i.customer
                LEFT JOIN demo_calls d ON d.request_id=i.request_id WHERE i.run_id=? ORDER BY i.rowid''',(run['id'],))]
            item['total'] = len(item['items'])
            item['finished'] = sum(r['status'] in ITEM_END for r in item['items'])
            result['run'] = item
        return result

    async def start(self, body):
        scope = body.customer_id or ''
        async with self.lock:
            self.h.tick()
            with self.h.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                prior = db.execute('SELECT * FROM reminder_runs WHERE request_id=?',(str(body.request_id),)).fetchone()
                if prior:
                    if prior['scope'] != scope:raise HTTPException(409,'That request was used for a different reminder run.')
                    return {'id':prior['id'],'status':prior['status']}
                if db.execute("SELECT 1 FROM reminder_runs WHERE status IN ('running','paused','stopping')").fetchone():
                    raise HTTPException(409,'A reminder run is already open. Resume or finish it before starting another.')
                from payment_reminder.harness import TERMINAL
                if db.execute('SELECT 1 FROM demo_calls WHERE status NOT IN (?,?,?)',TERMINAL).fetchone():
                    raise HTTPException(409,'A call is active or its connection is still being checked. Wait for it to finish before starting another.')
                if not (self.h.api_key and self.h.extraction.client.key):
                    raise HTTPException(409,'Connect the calling agent and Gemini before starting reminders.')
                if not self.h.callbacks_enabled:raise HTTPException(409,'Phone reminders are not enabled in local-only mode. Enable the public agent connection first.')
                if scope:
                    customer=self.h.customer(db,scope)
                    if not customer['phone']:raise HTTPException(409,'Add this customer’s phone number before calling.')
                # Selecting one customer explicitly authorizes an early call.
                # Keep the saved schedule intact until that call actually starts.
                # Bulk runs continue to include only customers due now.
                due = [r for r in self.due(db,self.h.clock(db),body.customer_id,include_future=bool(scope)) if r['phone']]
                if not due:
                    if scope:raise HTTPException(409,'This customer cannot receive a reminder. Check their balance, account status and any saved stop request.')
                    raise HTTPException(409,'No customers are due yet. Use Call now on an individual customer to call before their next follow-up.')
                rid = str(uuid4())
                db.execute('INSERT INTO reminder_runs(id,request_id,scope,status,starts_at,created_at) VALUES(?,?,?,?,?,?)',
                           (rid,str(body.request_id),scope,'running',self.h.clock(db).isoformat(),now().isoformat()))
                for customer in due:
                    db.execute('''INSERT INTO reminder_run_items(id,run_id,customer,phone,job_due_at,status,request_id)
                        VALUES(?,?,?,?,?,'queued',?)''',(str(uuid4()),rid,customer['id'],customer['phone'],customer['due_at'],str(uuid4())))
                return {'id':rid,'status':'running','queued':len(due)}

    async def control(self, rid, action):
        with self.h.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM reminder_runs WHERE id=?',(rid,)).fetchone()
            if not run:raise HTTPException(404,'Reminder run not found.')
            if run['status'] not in ('running','paused'):return {'status':run['status']}
            if action == 'cancel':
                db.execute("""UPDATE reminder_run_items SET status='cancelled' WHERE run_id=?
                    AND status IN ('queued','dialing') AND NOT EXISTS
                    (SELECT 1 FROM demo_calls d WHERE d.request_id=reminder_run_items.request_id)""",(rid,))
                db.execute("UPDATE reminder_runs SET status='stopping',error='' WHERE id=?",(rid,))
            else:
                db.execute('UPDATE reminder_runs SET status=?,error=?,setup_checked=0 WHERE id=?',
                           ('paused' if action=='pause' else 'running','',rid))
            return {'status':'stopping' if action=='cancel' else 'paused' if action=='pause' else 'running'}

    async def check_setup(self):
        if not self.h.callbacks_enabled:raise HTTPException(409,'The public agent connection is not enabled in local preview.')
        from payment_reminder.readiness import check_setup
        return await check_setup(self.h)

    async def step(self):
        from payment_reminder.harness import PhoneSelection, TERMINAL
        async with self.lock:
            # Calling is idempotent by the item's saved request ID. An interrupted
            # worker resumes that attempt rather than generating another dial.
            with self.h.store.connect() as db:
                run = db.execute("SELECT * FROM reminder_runs WHERE status IN ('running','paused','stopping') ORDER BY rowid LIMIT 1").fetchone()
                if not run:return
                items = db.execute('SELECT * FROM reminder_run_items WHERE run_id=? ORDER BY rowid',(run['id'],)).fetchall()
                for item in items:
                    call = db.execute('SELECT * FROM demo_calls WHERE request_id=?',(item['request_id'],)).fetchone()
                    if call:
                        if call['status'] not in TERMINAL:
                            status='uncertain' if call['status']=='uncertain' else 'calling'
                        elif not call['processed']:status='summarizing'
                        else:
                            brief = db.execute('SELECT status FROM conversation_analyses WHERE call_id=?',(call['id'],)).fetchone()
                            status='completed' if brief and brief['status'] in ('completed','failed','skipped') else 'summarizing'
                        db.execute('UPDATE reminder_run_items SET status=? WHERE id=?',(status,item['id']))
                remaining = db.execute('SELECT * FROM reminder_run_items WHERE run_id=? AND status NOT IN (?,?,?,?) ORDER BY rowid',
                                       (run['id'],*ITEM_END)).fetchall()
                if not remaining:
                    db.execute("UPDATE reminder_runs SET status='completed',finished_at=? WHERE id=?",(now().isoformat(),run['id']))
                    return
                if run['status'] != 'running':return
                if any(r['status'] in ('calling','uncertain','summarizing') for r in remaining):return
                at=self.h.clock(db)
                if at < datetime.fromisoformat(run['starts_at']):return
                if db.execute('SELECT 1 FROM demo_calls WHERE status NOT IN (?,?,?)',TERMINAL).fetchone():return
                next_item=next((r for r in remaining if r['status'] in ('queued','dialing')),None)
                if not next_item:return
                customer=self.h.customer(db,next_item['customer'])
                job=db.execute('SELECT * FROM demo_jobs WHERE customer=?',(customer['id'],)).fetchone()
                # A persisted dialing item is recovered via the saved phone
                # request before considering an updated schedule.
                call=db.execute('SELECT 1 FROM demo_calls WHERE request_id=?',(next_item['request_id'],)).fetchone()
                eligible=(customer['status']=='active' and self.h.balance(db,customer['id'])>customer['threshold']
                    and customer['phone']==next_item['phone'] and job and job['due_at']==next_item['job_due_at']
                    and job['status'] in ('ready','scheduled')
                    and not db.execute('SELECT 1 FROM contact_blocks WHERE business=? AND phone=?',('demo-supplies',next_item['phone'])).fetchone())
                if not call and not eligible:
                    db.execute("UPDATE reminder_run_items SET status='skipped',error='Account, contact or schedule changed before calling.' WHERE id=?",(next_item['id'],))
                    return
            if not run['setup_checked']:
                try:await self.check_setup()
                except Exception as exc:
                    with self.h.store.connect() as db:
                        reason=str(exc.detail) if isinstance(exc,HTTPException) else 'The calling connection is unavailable. Restart the demo and try again.'
                        db.execute("UPDATE reminder_runs SET status='paused',error=? WHERE id=?",(reason,run['id']))
                    return
                with self.h.store.connect() as db:db.execute('UPDATE reminder_runs SET setup_checked=1 WHERE id=?',(run['id'],))
            with self.h.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT status FROM reminder_runs WHERE id=?',(run['id'],)).fetchone()
                if current['status']!='running':return
                db.execute("UPDATE reminder_run_items SET status='dialing' WHERE id=?",(next_item['id'],))
            try:
                await self.h.phone(PhoneSelection(customer_id=next_item['customer'],to_number=next_item['phone'],request_id=next_item['request_id']),run_id=run['id'],expected_due=next_item['job_due_at'])
            except HTTPException as exc:
                with self.h.store.connect() as db:
                    call=db.execute('SELECT * FROM demo_calls WHERE request_id=?',(next_item['request_id'],)).fetchone()
                    current=db.execute('SELECT status FROM reminder_runs WHERE id=?',(run['id'],)).fetchone()
                    status='summarizing' if call else 'queued' if current['status']=='paused' else 'cancelled' if current['status']=='stopping' else 'skipped'
                    db.execute('UPDATE reminder_run_items SET status=?,error=? WHERE id=?',(status,str(exc.detail),next_item['id']))
                    if getattr(exc,'provider_status',None) in (401,402,403,404,429):
                        db.execute("UPDATE reminder_runs SET status='paused',setup_checked=0,error='Calling needs attention. Check agent access, credit and call limits, then resume.' WHERE id=?",(run['id'],))
            # Unexpected errors leave a durable dialing item. Its request ID is
            # reused on recovery, so even a lost local response cannot redial.

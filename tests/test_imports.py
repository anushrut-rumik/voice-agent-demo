"""Spreadsheet-to-phone integration uses an isolated database and fake carrier."""
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile
from xml.sax.saxutils import escape

from fastapi.testclient import TestClient
from payment_reminder.harness import create_harness, Entry, IST
from payment_reminder.runs import RunRequest

SECRET='test-only-'+40*'x'
HEADERS={'X-Demo-Console':'1','X-Filename':'customers.csv'}


def csv_bytes(rows):
    stream=io.StringIO();csv.writer(stream).writerows(rows);return stream.getvalue().encode('utf-8-sig')


def xlsx_bytes(rows):
    """Small OOXML test input. Not a user-facing authored workbook."""
    stream=io.BytesIO()
    with ZipFile(stream,'w') as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Customers" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        body=''
        for n,row in enumerate(rows,1):
            cells=''
            for i,value in enumerate(row):
                coordinate=chr(65+i)+str(n)
                cells+=f'<c r="{coordinate}"><v>{value}</v></c>' if isinstance(value,(int,float)) else f'<c r="{coordinate}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            body+=f'<row r="{n}">{cells}</row>'
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+body+'</sheetData></worksheet>')
    return stream.getvalue()


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_harness(Path(self.tmp.name)/'import.sqlite',SECRET,'fake-carrier',gemini_key='fake-gemini',background=False)
        self.h=self.app.state.harness;self.client=TestClient(self.app)
        self.at=datetime(2026,10,23,0,30,tzinfo=IST);self.h.clock=lambda db:self.at
        with self.h.store.connect() as db:db.execute("UPDATE demo_jobs SET status='cancelled'")
        self.header=['Customer name','Phone','Company','Invoice number','Outstanding amount','Due date','Purchase description','Notes']
        self.row=['Test recipient','9876543210','Example shop','TEST-1','1,250.50','2026-10-22','12 boxes of tea','Prefers Hindi; ask about delayed shipment.']

    async def asyncTearDown(self):self.client.close();self.tmp.cleanup()

    def upload(self,rows,filename='customers.csv'):
        return self.client.post('/api/imports',content=csv_bytes(rows),headers={**HEADERS,'X-Filename':filename})

    def account(self,response):
        self.assertEqual(response.status_code,200,response.text)
        cid=response.json()['customer_ids'][0]
        return next(c for c in self.h.state()['customers'] if c['id']==cid)

    def test_csv_loads_phone_bill_extra_context_and_is_ready_after_hours(self):
        r=self.upload([self.header,self.row]);c=self.account(r)
        self.assertEqual(c['phone'],'+919876543210');self.assertEqual(c['balance'],1250.5)
        self.assertTrue(c['ready_for_reminder']);self.assertEqual(c['threshold'],0)
        self.assertIn('12 boxes of tea',c['context_preview']['invoice_summary'])
        self.assertIn('Prefers Hindi',c['context_preview']['invoice_summary'])
        self.assertEqual(c['imported_details'][0]['fields']['Due date'],'2026-10-22')
        with self.h.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM demo_calls').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reminder_runs').fetchone()[0],0)

    def test_excel_numeric_phone_and_title_rows(self):
        content=xlsx_bytes([['October reminders'],[],self.header,[*self.row[:1],9876543210,*self.row[2:]]])
        r=self.client.post('/api/imports',content=content,headers={**HEADERS,'X-Filename':'customers.xlsx'})
        self.assertEqual(self.account(r)['phone'],'+919876543210')

    def test_reupload_does_not_duplicate_or_restore_paid_balance(self):
        rows=[self.header,self.row];r=self.upload(rows);c=self.account(r)
        self.h.entry(Entry(customer_id=c['id'],amount='1250.50',reference='reconciled'),'payment')
        again=self.upload(rows,'renamed.csv');self.assertTrue(again.json()['already_loaded'])
        self.assertEqual(self.account(again)['balance'],0)
        self.assertFalse(self.account(again)['ready_for_reminder'])

    def test_changed_file_updates_notes_but_does_not_duplicate_invoice(self):
        first=self.account(self.upload([self.header,self.row]));self.row[-1]='New note'
        second=self.upload([self.header,self.row]);self.assertEqual(second.json()['invoice_count'],0)
        self.assertEqual(self.account(second)['balance'],first['balance'])
        self.assertIn('New note',self.account(second)['context_preview']['invoice_summary'])

    def test_multiple_invoices_merge_and_earliest_due_date_wins(self):
        future=self.row.copy();future[3]='TEST-FUTURE';future[5]='2026-11-01'
        c=self.account(self.upload([self.header,future,self.row]))
        self.assertEqual(len(c['ledger']),2);self.assertEqual(c['balance'],2501)
        self.assertTrue(c['ready_for_reminder'])
        self.assertEqual(len(c['imported_details']),2)

    def test_invalid_rows_report_errors_and_valid_rows_still_load(self):
        bad=self.row.copy();bad[1]='not a number';bad[3]='BAD'
        r=self.upload([self.header,bad,self.row]);self.account(r)
        self.assertEqual(r.json()['skipped_count'],1);self.assertEqual(r.json()['issues'][0]['row'],2)
        self.assertIn('phone',r.json()['issues'][0]['message'])

    def test_missing_phone_loads_and_future_bill_is_not_due(self):
        self.row[1]='';self.row[5]='2026-11-01'
        c=self.account(self.upload([self.header,self.row]))
        self.assertEqual(c['phone'],'');self.assertFalse(c['ready_for_reminder'])
        self.assertTrue(c['job']['due_at'].startswith('2026-11-01'))

    def test_new_invoice_does_not_override_a_saved_callback_or_stop(self):
        c=self.account(self.upload([self.header,self.row]))
        with self.h.store.connect() as db:
            db.execute("UPDATE demo_jobs SET due_at='2026-11-09T10:00:00+05:30',reason='Customer asked for a callback' WHERE customer=?",(c['id'],))
            db.execute("UPDATE demo_customers SET status='suppressed' WHERE id=?",(c['id'],))
        self.row[3]='SECOND';c=self.account(self.upload([self.header,self.row]))
        self.assertEqual(c['status'],'suppressed');self.assertFalse(c['ready_for_reminder'])

    def test_changed_amount_conflict_does_not_leave_an_extra_bill(self):
        self.account(self.upload([self.header,self.row]));self.row[4]='999'
        r=self.upload([self.header,self.row]);self.assertEqual(r.status_code,422)
        self.assertIn('already loaded',r.json()['detail'])
        with self.h.store.connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM demo_ledger WHERE reference='TEST-1'").fetchone()[0],1)

    def test_unrecognized_or_broken_files_fail_without_records(self):
        for content,filename in [(b'wrong,headers\n1,2','wrong.csv'),(b'broken','bad.xlsx')]:
            r=self.client.post('/api/imports',content=content,headers={**HEADERS,'X-Filename':filename})
            self.assertEqual(r.status_code,422)
        with self.h.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM customer_imports').fetchone()[0],0)

    async def test_import_then_one_click_dispatches_exact_spreadsheet_context(self):
        c=self.account(self.upload([self.header,self.row]));calls=[]
        self.h.runs.check_setup=AsyncMock(return_value={'status':'ready'})
        tools=TestClient(self.h.tool_app)
        async def carrier(method,path,body=None,auth=True):
            self.assertEqual(method,'POST');self.assertEqual(body['toNumber'],c['phone'])
            ctx=tools.get('/voice/reminders/context',params={'agent_id':'ua_afe624fa','phone_number':body['toNumber']},headers={'Authorization':'Bearer '+SECRET}).json()
            self.assertEqual(ctx['customer_name'],'Test recipient');self.assertEqual(ctx['outstanding_amount'],'1250.50')
            self.assertIn('delayed shipment',ctx['invoice_summary']);calls.append(body)
            return {'callId':str(uuid4()),'toNumber':body['toNumber'],'status':'calling'}
        self.h.upstream=carrier
        r=self.client.post('/api/reminders/start',json={'request_id':str(uuid4())},headers=HEADERS)
        self.assertEqual(r.status_code,202,r.text);self.assertEqual(r.json()['queued'],1)
        await self.h.runs.step();await self.h.runs.step();self.assertEqual(len(calls),1)
        tools.close()

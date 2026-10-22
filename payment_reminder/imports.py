"""Load customer spreadsheets locally. Importing never creates a call or run."""
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

from fastapi import HTTPException
from python_calamine import CalamineWorkbook

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000

ALIASES = {
    'name': 'customer customername client clientname name contactname fullname party partyname debtor debtorname'.split(),
    'phone': 'phone phonenumber mobile mobilenumber contactnumber telephone whatsapp whatsappnumber contact customerphone'.split(),
    'company': 'company companyname business businessname organisation organization accountname'.split(),
    'customer_id': 'customerid clientid accountid partyid'.split(),
    'reference': 'invoice invoicenumber invoiceno invoiceid bill billnumber billno billid reference invoicereference'.split(),
    'balance': 'outstanding outstandingamount outstandingbalance balance remainingbalance unpaid unpaidamount amountdue dueamount pendingamount totaldue'.split(),
    'amount': 'amount invoiceamount billamount total totalamount invoicevalue'.split(),
    'due_date': 'duedate paymentduedate paymentdue due'.split(),
    'invoice_date': 'invoicedate billdate date'.split(),
    'currency': ['currency'],
    'threshold': 'threshold reminderthreshold minimumreminderamount'.split(),
}
LOOKUP = {alias: field for field, aliases in ALIASES.items() for alias in aliases}


def text(value):
    if value is None:return ''
    if isinstance(value, (datetime, date)):return value.isoformat()
    if isinstance(value, float) and value.is_integer():return str(int(value))
    return str(value).strip()


def key(value):return re.sub(r'[^a-z0-9]', '', text(value).lower())


def amount(value):
    raw = re.sub(r'(?i)(INR|Rs\.?|₹|,|\s)', '', text(value))
    try:
        n = Decimal(raw)
        if not n.is_finite() or n < 0 or n > 100000000 or n * 100 != (n * 100).to_integral_value():raise InvalidOperation
        return int(n * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Use a non-negative INR amount with at most two decimal places.')


def phone(value):
    from payment_reminder.harness import phone_number
    raw = re.sub(r'[\s().-]', '', text(value))
    if not raw:return ''
    if raw.startswith('00'):raw = '+' + raw[2:]
    if re.fullmatch(r'[6-9]\d{9}', raw):raw = '+91' + raw
    elif re.fullmatch(r'91\d{10}', raw):raw = '+' + raw
    try:return phone_number(raw)
    except HTTPException:raise ValueError('Check the phone number. Use an Indian mobile or include + and the country code.') from None


def due_date(value):
    raw = text(value)
    if not raw:return None
    try:return datetime.fromisoformat(raw).date()
    except ValueError:pass
    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%d %b %Y', '%d %B %Y'):
        try:return datetime.strptime(raw, fmt).date()
        except ValueError:pass
    raise ValueError('Use a due date like 2026-10-23 or 23/10/2026.')


def sheets(content, filename):
    suffix = Path(filename).suffix.lower()
    if suffix not in ('.xlsx', '.xls', '.csv'):raise HTTPException(422, 'Choose an Excel (.xlsx or .xls) or CSV file.')
    if not content or len(content) > MAX_BYTES:raise HTTPException(422, 'Choose a non-empty file smaller than 5 MB.')
    try:
        if suffix == '.csv':
            try:decoded = content.decode('utf-8-sig')
            except UnicodeDecodeError:decoded = content.decode('cp1252')
            try:dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=',;\t')
            except csv.Error:dialect = csv.excel
            rows = list(csv.reader(io.StringIO(decoded), dialect))
            result = [(filename, rows)]
        else:
            if suffix == '.xlsx':
                with ZipFile(io.BytesIO(content)) as archive:
                    if sum(f.file_size for f in archive.infolist()) > 40 * 1024 * 1024:
                        raise HTTPException(422, 'This workbook is too large. Export a smaller customer sheet.')
            with CalamineWorkbook.from_filelike(io.BytesIO(content)) as workbook:
                result = [(name, workbook.get_sheet_by_name(name).to_python(skip_empty_area=False))
                          for name in workbook.sheet_names]
        if sum(len(rows) for _, rows in result) > MAX_ROWS + 100:
            raise HTTPException(422, 'Import up to 2,000 rows at a time.')
        if any(len(row) > 100 for _, rows in result for row in rows):
            raise HTTPException(422, 'Keep the customer sheet to 100 columns or fewer.')
        return result
    except HTTPException:raise
    except Exception:
        raise HTTPException(422, 'This file could not be read. Save it as an Excel workbook or CSV and try again.') from None


def parse_rows(content, filename):
    records, issues = [], []
    for sheet, rows in sheets(content, filename):
        found = None
        for index, row in enumerate(rows[:20]):
            mapping = [LOOKUP.get(key(cell)) for cell in row]
            if 'name' in mapping and ('amount' in mapping or 'balance' in mapping):
                found = (index, row, mapping);break
        if found is None:continue
        index, headers, mapping = found
        for number, row in enumerate(rows[index+1:], index+2):
            if not any(text(v) for v in row):continue
            try:
                data = {}
                fields = {}
                for i, header in enumerate(headers):
                    value = text(row[i]) if i < len(row) else ''
                    if not value:continue
                    if len(value) > 4000:raise ValueError('A cell is too long. Keep each note under 4,000 characters.')
                    title = text(header) or f'Column {i+1}'
                    if title in fields:title += f' ({i+1})'
                    fields[title] = value
                    if mapping[i]:
                        if mapping[i] in data and data[mapping[i]] != value:
                            raise ValueError(f'Two columns disagree about {mapping[i].replace("_", " ")}.')
                        data[mapping[i]] = value
                if not data.get('name'):raise ValueError('Add a customer name.')
                if len(data['name']) > 100 or len(data.get('company', '')) > 100:raise ValueError('Keep customer and company names under 100 characters.')
                if data.get('currency', 'INR').upper() not in ('INR', '₹', 'RS', 'RS.'):raise ValueError('This demo supports INR bills. Convert this row to INR first.')
                data['balance_paise'] = amount(data.get('balance', data.get('amount', '')))
                data['phone'] = phone(data.get('phone', ''))
                data['due'] = due_date(data.get('due_date', ''))
                data['threshold_paise'] = amount(data.get('threshold', '0'))
                if len(data.get('reference', '')) > 100:raise ValueError('Keep invoice references under 100 characters.')
                records.append({'data': data, 'fields': fields, 'sheet': sheet, 'row': number})
            except ValueError as exc:issues.append({'sheet': sheet, 'row': number, 'message': str(exc)})
    if not records and not issues:
        raise HTTPException(422, 'No customer table found. Include Customer name and Outstanding amount (or Amount) columns. Phone, invoice, due date and notes are picked up automatically.')
    if len(records) + len(issues) > MAX_ROWS:raise HTTPException(422, 'Import up to 2,000 rows at a time.')
    return records, issues


class CustomerImports:
    def __init__(self, harness):
        self.h = harness
        with self.h.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS customer_imports(
                    fingerprint TEXT PRIMARY KEY,filename TEXT NOT NULL,report TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS import_customer_keys(key TEXT PRIMARY KEY,customer TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imported_bills(
                    reference TEXT PRIMARY KEY,customer TEXT NOT NULL,initial_amount INTEGER NOT NULL,fields TEXT NOT NULL,due_date TEXT NOT NULL DEFAULT '');
            ''')

    def last(self, db):
        row = db.execute('SELECT report FROM customer_imports ORDER BY rowid DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def load(self, content, filename):
        from payment_reminder.harness import IST, TERMINAL
        filename = Path(filename.replace('\\', '/')).name[:180]
        fingerprint = hashlib.sha256(content).hexdigest()
        records, issues = parse_rows(content, filename)
        customers, created, bills, duplicates = set(), set(), 0, 0
        with self.h.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT report FROM customer_imports WHERE fingerprint=?', (fingerprint,)).fetchone()
            if previous:return {**json.loads(previous[0]), 'already_loaded': True}
            for record in records:
                db.execute('SAVEPOINT import_row')
                data = record['data']
                try:
                    identity = ('external:' + data['customer_id']) if data.get('customer_id') else ('phone:' + data['phone']) if data['phone'] else ('name:' + data['name'].casefold() + '|' + data.get('company', '').casefold())
                    cid = 'customer_' + hashlib.sha256(identity.encode()).hexdigest()[:28]
                    match = db.execute('SELECT customer FROM import_customer_keys WHERE key=?', (identity,)).fetchone()
                    existing = self.h.customer(db, match[0]) if match else None
                    if existing is None and data['phone']:
                        matches = db.execute('SELECT * FROM demo_customers WHERE phone=?', (data['phone'],)).fetchall()
                        if len(matches) > 1:raise ValueError('This phone number belongs to several accounts. Give each customer a unique contact number.')
                        if matches:existing = matches[0]
                    if existing is None:
                        matches = db.execute('SELECT * FROM demo_customers WHERE lower(name)=lower(?) AND lower(company)=lower(?)',
                                             (data['name'], data.get('company', ''))).fetchall()
                        if len(matches) == 1:existing = matches[0]
                    if existing:
                        cid = existing['id']
                        if existing['name'].casefold() != data['name'].casefold():raise ValueError('This phone or customer ID already belongs to ' + existing['name'] + '. Check the name and number.')
                        if db.execute('SELECT 1 FROM demo_calls WHERE customer=? AND status NOT IN (?,?,?)', (cid, *TERMINAL)).fetchone():raise ValueError('This customer is on a call. Import their changes after it finishes.')
                    if data['phone'] and db.execute('SELECT 1 FROM demo_customers WHERE phone=? AND id<>?',(data['phone'],cid)).fetchone():
                        raise ValueError('This number already belongs to another customer. Check the contact number.')
                    reference = data.get('reference') or 'IMPORT-' + hashlib.sha256(identity.encode()).hexdigest()[:20]
                    prior = db.execute('SELECT * FROM demo_ledger WHERE reference=?', (reference,)).fetchone()
                    if prior:
                        if (prior['customer'], prior['kind'], prior['amount']) != (cid, 'invoice', data['balance_paise']):
                            raise ValueError('Invoice ' + reference + ' is already loaded with different details. Record a payment to reduce its balance.')
                    if not existing:
                        db.execute('INSERT INTO demo_customers(id,name,company,threshold,status,phone) VALUES(?,?,?,?,?,?)',
                            (cid, data['name'], data.get('company', ''), data['threshold_paise'], 'active', data['phone']))
                    elif data['phone'] and data['phone'] != existing['phone']:
                        db.execute('UPDATE demo_customers SET phone=? WHERE id=?', (data['phone'], cid))
                    db.execute('INSERT OR IGNORE INTO import_customer_keys VALUES(?,?)', (identity, cid))
                    added = not prior and data['balance_paise'] > 0
                    if added:
                        db.execute('INSERT INTO demo_ledger(customer,kind,amount,reference,created_at) VALUES(?,?,?,?,?)',
                            (cid, 'invoice', data['balance_paise'], reference, self.h.clock(db).isoformat()))
                    db.execute('INSERT OR REPLACE INTO imported_bills VALUES(?,?,?,?,?)',
                        (reference, cid, data['balance_paise'], json.dumps(record['fields'], ensure_ascii=False),data['due'].isoformat() if data['due'] else ''))
                    previous_job = db.execute('SELECT * FROM demo_jobs WHERE customer=?', (cid,)).fetchone()
                    self.h.reconcile(db, cid)
                    # Never move a previously agreed callback or payment follow-up.
                    if added and (not previous_job or previous_job['status']=='cancelled' or previous_job['reason'] in ('Imported bill ready for reminder','Initial unpaid balance ready for reminder')):
                        at = self.h.clock(db)
                        dates=[r[0] for r in db.execute('SELECT due_date FROM imported_bills WHERE customer=? AND initial_amount>0',(cid,))]
                        earliest=min(dates)
                        future=date.fromisoformat(earliest) if earliest else None
                        due = datetime.combine(future, datetime.min.time(), IST).replace(hour=10) if future and future > at.date() else at
                        db.execute("UPDATE demo_jobs SET due_at=?,reason='Imported bill ready for reminder' WHERE customer=?", (due.isoformat(), cid))
                    self.h.log(db, cid, 'Spreadsheet imported', filename + ' · row ' + str(record['row']))
                    db.execute('RELEASE import_row')
                    customers.add(cid)
                    if not existing:created.add(cid)
                    bills += int(added)
                    duplicates += int(bool(prior))
                except ValueError as exc:
                    db.execute('ROLLBACK TO import_row');db.execute('RELEASE import_row')
                    issues.append({'sheet': record['sheet'], 'row': record['row'], 'message': str(exc)})
            if not customers:
                raise HTTPException(422, 'No rows were loaded. ' + (f"Row {issues[0]['row']}: {issues[0]['message']}" if issues else 'Check the file and try again.'))
            report = {'filename': filename, 'customer_count': len(customers), 'created_count': len(created),
                      'invoice_count': bills, 'duplicate_count': duplicates, 'skipped_count': len(issues),
                      'issues': issues[:30], 'customer_ids': sorted(customers), 'already_loaded': False}
            db.execute('INSERT INTO customer_imports VALUES(?,?,?,?)', (fingerprint, filename, json.dumps(report), self.h.clock(db).isoformat()))
            return report

"""Authenticated tools backed by local SQLite; intended for a single demo business."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

UTC = timezone.utc
FIELDS = ("context_status", "case_status", "case_token", "customer_name", "outstanding_amount",
          "currency", "invoice_summary", "prior_contact_summary", "current_local_datetime",
          "customer_timezone", "balance_as_of", "payment_instructions", "callback_windows")


def now():
    return datetime.now(UTC)


def demo_context():
    return {"customer_name": "Rahul Sharma", "outstanding_amount": "18000.00", "currency": "INR",
            "invoice_summary": f"Fictional invoice DEMO-1042: INR 18000 outstanding, due {(now()-timedelta(days=7)).date()}, overdue.",
            "prior_contact_summary": "First reminder conversation. No previous promise.",
            "customer_timezone": "Asia/Kolkata", "balance_as_of": now().isoformat(),
            "payment_instructions": "Demo only. Do not make a real payment.",
            "callback_windows": "Monday to Friday, 10:00 to 18:00, Asia/Kolkata."}


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=2000)
    case_token: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["promise_to_pay", "callback_requested", "payment_claimed", "dispute",
                     "wrong_number", "do_not_call", "human_requested", "no_commitment", "balance_cleared"]
    promised_amount: str = ""
    promised_date: str = ""
    callback_date: str = ""
    callback_time: str = ""
    customer_statement: str = ""
    payment_reference: str = ""
    summary: str = ""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS dispatches (
              token TEXT PRIMARY KEY, business TEXT NOT NULL, agent TEXT NOT NULL,
              phone TEXT NOT NULL, call_id TEXT, expires_at TEXT NOT NULL,
              context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE UNIQUE INDEX IF NOT EXISTS unique_call
              ON dispatches(business,call_id) WHERE call_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, token TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL,
              payload TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contact_blocks (
              business TEXT NOT NULL, phone TEXT NOT NULL, reason TEXT NOT NULL,
              PRIMARY KEY(business,phone));
            CREATE TABLE IF NOT EXISTS tool_receipts (
              id INTEGER PRIMARY KEY, call_id TEXT NOT NULL, tool TEXT NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            if 'case_token' not in {r[1] for r in db.execute('PRAGMA table_info(tool_receipts)')}:
                db.execute("ALTER TABLE tool_receipts ADD COLUMN case_token TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed(self, business, agent, phone, context, call_id=None):
        token = secrets.token_urlsafe(24)
        with self.connect() as db:
            blocked = phone and db.execute("SELECT 1 FROM contact_blocks WHERE business=? AND phone=?", (business,phone)).fetchone()
            db.execute("INSERT INTO dispatches(token,business,agent,phone,call_id,expires_at,context,status) VALUES(?,?,?,?,?,?,?,?)",
                       (token, business, agent, phone, call_id, (now()+timedelta(hours=2)).isoformat(), json.dumps(context), "suppressed" if blocked else "active"))
        return token


def create_app(db_path=None, tool_token=None, business=None, agent_aliases=None):
    token = tool_token or os.environ.get("REMINDER_TOOL_TOKEN", "")
    tenant = business or os.environ.get("REMINDER_BUSINESS_ID", "demo-supplies")
    if len(token) < 32:
        raise RuntimeError("Set REMINDER_TOOL_TOKEN to a random secret of at least 32 characters")
    store = Store(db_path or os.environ.get("REMINDER_DB", ".local/reminders.sqlite"))
    app = FastAPI(title="Payment Reminder Demo Tools", docs_url=None, redoc_url=None)
    app.state.store = store
    agent_aliases = agent_aliases or {}

    def authenticate(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Unauthorized")

    def load(db, case_token):
        row = db.execute("SELECT * FROM dispatches WHERE token=? AND business=?", (case_token, tenant)).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= now():
            raise HTTPException(404, "No current dispatch")
        return row

    def bind(db, row, call_id):
        if not call_id or "{" in call_id or len(call_id) > 128:
            raise HTTPException(422, "A resolved call ID is required")
        if row["call_id"] and row["call_id"] != call_id:
            raise HTTPException(409, "Call does not match dispatch")
        try:
            db.execute("UPDATE dispatches SET call_id=? WHERE token=?", (call_id, row["token"]))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Call already bound to another dispatch")

    def context(row):
        data = json.loads(row["context"])
        data.update(context_status="ready", case_status=row["status"], case_token=row["token"],
                    current_local_datetime=now().astimezone(ZoneInfo(data["customer_timezone"])).isoformat())
        required = ["customer_name", "outstanding_amount", "currency", "balance_as_of"]
        if not all(data.get(k) for k in required):
            data["context_status"] = "unavailable"
        return {k: str(data.get(k, "")) for k in FIELDS}

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "demo", "outbound_calls_enabled": False}

    @app.get("/voice/reminders/context", dependencies=[Depends(authenticate)])
    def get_context(phone_number: str = "", agent_id: str = "", call_id: str = ""):
        # Rumik's context parameter is the public handle, while dispatches use
        # the immutable agent UUID. Only explicitly configured aliases qualify.
        agent_id = agent_aliases.get(agent_id, agent_id)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if phone_number and agent_id:
                # A phone before-call tool runs before Silk allocates its run ID.
                # Retired dispatches must never make a subsequent call ambiguous.
                rows = db.execute("SELECT * FROM dispatches WHERE business=? AND agent=? AND phone=? AND expires_at>? AND (call_id IS NULL OR call_id=?)",
                                  (tenant, agent_id, phone_number, now().isoformat(), call_id)).fetchall()
                if not rows and call_id:
                    bound = db.execute("SELECT 1 FROM dispatches WHERE business=? AND agent=? AND phone=? AND expires_at>? AND call_id IS NOT NULL",
                                       (tenant,agent_id,phone_number,now().isoformat())).fetchone()
                    if bound:raise HTTPException(409, "Call does not match dispatch")
            elif call_id and agent_id:
                rows = db.execute("SELECT * FROM dispatches WHERE business=? AND agent=? AND call_id=? AND expires_at>?",
                                  (tenant, agent_id, call_id, now().isoformat())).fetchall()
            else:
                rows = []
            if len(rows) != 1:
                db.execute('INSERT INTO tool_receipts(call_id,tool,status,created_at) VALUES(?,?,?,?)',
                           (call_id,'context','unavailable',now().isoformat()))
                return {k: ("unavailable" if k in ("context_status", "case_status") else "") for k in FIELDS}
            row = rows[0]
            if call_id:
                bind(db, row, call_id)
            result = context(row)
            db.execute('INSERT INTO tool_receipts(call_id,tool,status,created_at,case_token) VALUES(?,?,?,?,?)',
                       (call_id,'context',result['context_status'],now().isoformat(),row['token']))
            return result

    @app.get("/voice/reminders/balance", dependencies=[Depends(authenticate)])
    def balance(case_token: str, call_id: str):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = load(db, case_token)
            bind(db, row, call_id)
            data = context(row)
            result = {k: data[k] for k in ("context_status", "case_status", "outstanding_amount", "currency", "balance_as_of")}
            result["payment_status"] = "cleared" if Decimal(data["outstanding_amount"]) <= 0 else "not_cleared"
            return result

    @app.post("/voice/reminders/outcomes", dependencies=[Depends(authenticate)])
    def record(payload: Outcome):
        body = payload.model_dump()
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = load(db, payload.case_token)
            bind(db, row, payload.call_id)
            prior = db.execute("SELECT result FROM events WHERE fingerprint=?", (digest,)).fetchone()
            if prior:
                result = json.loads(prior["result"])
                result.update(save_status="duplicate", case_status=row["status"])
                return result
            data = context(row)
            local_today = now().astimezone(ZoneInfo(data["customer_timezone"])).date()
            for field in ("promised_date", "callback_date"):
                value = body[field]
                if value:
                    try:
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value) < local_today:
                            raise ValueError()
                    except ValueError:
                        raise HTTPException(422, f"{field} must be an unambiguous current or future date")
            if payload.callback_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", payload.callback_time):
                raise HTTPException(422, "callback_time must be HH:MM")
            if payload.promised_amount:
                try:
                    amount = Decimal(payload.promised_amount)
                    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", payload.promised_amount) or not amount.is_finite() or amount <= 0:
                        raise InvalidOperation()
                except InvalidOperation:
                    raise HTTPException(422, "Invalid promised amount")
                if amount > Decimal(data["outstanding_amount"]):
                    raise HTTPException(409, "Promise exceeds the current balance; refresh the account")
            if payload.outcome == "promise_to_pay" and not (payload.promised_amount and payload.promised_date):
                raise HTTPException(422, "A firm promise requires an amount and date")
            if payload.outcome == "balance_cleared" and Decimal(data["outstanding_amount"]) > 0:
                raise HTTPException(409, "Accounting records do not show a cleared balance")
            status, action = row["status"], "recorded"
            if payload.outcome in ("do_not_call", "wrong_number"):
                status, action = "suppressed", "suppressed"
                if row["phone"]:
                    db.execute("INSERT OR REPLACE INTO contact_blocks VALUES(?,?,?)", (tenant,row["phone"],payload.outcome))
                    db.execute("UPDATE dispatches SET status='suppressed' WHERE business=? AND phone=?", (tenant,row["phone"]))
            elif payload.outcome in ("dispute", "payment_claimed", "human_requested"):
                if status not in ("closed", "suppressed"):
                    status = "paused"
                action = "paused"
            elif payload.outcome == "balance_cleared" and status != "suppressed":
                status = "closed"
            # Ordinary outcomes never reopen a closed case or clear any hold.
            result = {"save_status": "saved", "case_status": status, "action_status": action, "scheduled_callback_at": ""}
            db.execute("UPDATE dispatches SET status=? WHERE token=?", (status, row["token"]))
            db.execute("INSERT INTO events(token,fingerprint,payload,result,created_at) VALUES(?,?,?,?,?)",
                       (row["token"], digest, canonical, json.dumps(result), now().isoformat()))
            return result

    return app

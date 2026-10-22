"""Prepare ONE fictional dispatch for a chosen agent and authorized test destination."""
import argparse
import json
import os
from datetime import timedelta
from payment_reminder.app import Store, now

parser = argparse.ArgumentParser()
parser.add_argument("--agent-id",required=True,help="Agent UUID, matching the tool's agent_id context")
parser.add_argument("--phone",default="",help="Only an explicitly authorized test number; seeding does not dial")
parser.add_argument("--call-id",help="For a browser test, register the call first and pass its ID")
args=parser.parse_args()
if not args.phone and not args.call_id:
    parser.error("Supply a test phone or registered browser call ID")
store=Store(os.environ.get("REMINDER_DB",".local/reminders.sqlite"))
with store.connect() as db:
    count=db.execute("SELECT COUNT(*) FROM dispatches WHERE business=? AND agent=? AND phone=? AND expires_at>?",
                     (os.environ.get("REMINDER_BUSINESS_ID","demo-supplies"),args.agent_id,args.phone,now().isoformat())).fetchone()[0]
if count:
    raise SystemExit("An unexpired dispatch already exists; do not create ambiguous test context")
context={"customer_name":"Rahul Sharma","outstanding_amount":"18000.00","currency":"INR",
         "invoice_summary":f"Fictional invoice DEMO-1042: INR 18000 outstanding, due {(now()-timedelta(days=7)).date()}, overdue.",
         "prior_contact_summary":"First reminder conversation. No previous promise.","customer_timezone":"Asia/Kolkata",
         "balance_as_of":now().isoformat(),"payment_instructions":"Demo only. Do not make a real payment.",
         "callback_windows":"Monday to Friday, 10:00 to 18:00, Asia/Kolkata."}
token=store.seed(os.environ.get("REMINDER_BUSINESS_ID","demo-supplies"),args.agent_id,args.phone,context,args.call_id)
print("Fictional dispatch prepared; expires in two hours. No call placed.")

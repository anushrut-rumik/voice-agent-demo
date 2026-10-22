import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from payment_reminder.app import create_app, now

SECRET = "test-only-" + "x" * 40


def fixture():
    return {"customer_name": "Test Customer", "outstanding_amount": "18000.00", "currency": "INR",
            "invoice_summary": "Fictional test invoice", "prior_contact_summary": "", "customer_timezone": "Asia/Kolkata",
            "balance_as_of": now().isoformat(), "payment_instructions": "Fictional demo only", "callback_windows": "Weekdays"}


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name)/"test.sqlite", SECRET, "demo")
        self.store = self.app.state.store
        self.case = self.store.seed("demo", "agent-1", "+12025550123", fixture())
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer " + SECRET}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def context(self, **kwargs):
        return self.client.get("/voice/reminders/context", params={"phone_number":"+12025550123", "agent_id":"agent-1", **kwargs}, headers=self.headers)

    def outcome(self, kind, **kwargs):
        return self.client.post("/voice/reminders/outcomes", json={"case_token":self.case,"call_id":"call-1","outcome":kind,**kwargs}, headers=self.headers)

    def test_auth_required(self):
        self.assertEqual(self.client.get("/voice/reminders/context").status_code,401)

    def test_context_and_binding(self):
        self.assertEqual(self.context(call_id="call-1").json()["case_token"], self.case)
        self.assertEqual(self.context(call_id="different-call").status_code,409)

    def test_before_call_can_resolve_before_call_id_is_known(self):
        self.assertEqual(self.context().json()["context_status"],"ready")
        self.assertEqual(self.outcome("no_commitment").status_code,200)

    def test_ambiguous_lookup_fails_closed(self):
        self.store.seed("demo", "agent-1", "+12025550123", fixture())
        response = self.context().json()
        self.assertEqual(response["context_status"],"unavailable")
        self.assertEqual(response["customer_name"],"")

    def test_other_business_cannot_access_dispatch(self):
        other = self.store.seed("other", "agent-1", "+12025550123", fixture())
        response=self.client.get("/voice/reminders/balance",params={"case_token":other,"call_id":"call-1"},headers=self.headers)
        self.assertEqual(response.status_code,404)

    def test_expired_dispatch(self):
        with self.store.connect() as db:
            db.execute("UPDATE dispatches SET expires_at=?",((now()-timedelta(hours=1)).isoformat(),))
        self.assertEqual(self.context().json()["context_status"],"unavailable")
        self.assertEqual(self.outcome("no_commitment").status_code,404)

    def test_claim_does_not_clear_balance(self):
        self.assertEqual(self.outcome("payment_claimed").json()["case_status"],"paused")
        response=self.client.get("/voice/reminders/balance",params={"case_token":self.case,"call_id":"call-1"},headers=self.headers)
        self.assertEqual(response.json()["outstanding_amount"],"18000.00")
        self.assertEqual(self.outcome("balance_cleared").status_code,409)

    def test_duplicate_and_unicode_are_preserved(self):
        note='उन्होंने कहा: "अगले हफ्ते"\nNo firm promise.'
        self.assertEqual(self.outcome("no_commitment",customer_statement=note).json()["save_status"],"saved")
        self.assertEqual(self.outcome("no_commitment",customer_statement=note).json()["save_status"],"duplicate")
        with self.store.connect() as db:
            rows=db.execute("SELECT payload FROM events").fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0]["payload"])["customer_statement"],note)

    def test_suppression_is_sticky(self):
        self.assertEqual(self.outcome("do_not_call").json()["action_status"],"suppressed")
        self.assertEqual(self.outcome("no_commitment").json()["case_status"],"suppressed")
        self.assertEqual(self.outcome("dispute").json()["case_status"],"suppressed")

    def test_dispute_hold_is_sticky(self):
        self.outcome("dispute")
        self.assertEqual(self.outcome("no_commitment").json()["case_status"],"paused")

    def test_suppressed_number_stays_blocked_for_future_dispatches(self):
        self.outcome("do_not_call")
        next_case=self.store.seed("demo","agent-1","+12025550123",fixture())
        with self.store.connect() as db:
            row=db.execute("SELECT status FROM dispatches WHERE token=?",(next_case,)).fetchone()
        self.assertEqual(row["status"],"suppressed")

    def test_callback_is_only_recorded(self):
        data=self.outcome("callback_requested",customer_statement="Tomorrow afternoon").json()
        self.assertEqual(data["action_status"],"recorded")
        self.assertEqual(data["scheduled_callback_at"],"")

    def test_invalid_promises(self):
        self.assertEqual(self.outcome("promise_to_pay").status_code,422)
        future=(now()+timedelta(days=2)).date().isoformat()
        self.assertEqual(self.outcome("promise_to_pay",promised_amount="NaN",promised_date=future).status_code,422)
        self.assertEqual(self.outcome("promise_to_pay",promised_amount="19000",promised_date=future).status_code,409)
        self.assertEqual(self.outcome("promise_to_pay",promised_amount="8000",promised_date="2020-01-01").status_code,422)
        self.assertEqual(self.outcome("promise_to_pay",promised_amount="8000",promised_date=future).status_code,200)

    def test_cleared_case_does_not_reopen(self):
        data=fixture(); data["outstanding_amount"]="0.00"
        with self.store.connect() as db:
            db.execute("UPDATE dispatches SET context=? WHERE token=?",(json.dumps(data),self.case))
        self.assertEqual(self.outcome("balance_cleared").json()["case_status"],"closed")
        self.assertEqual(self.outcome("no_commitment").json()["case_status"],"closed")

    def test_missing_case_token_or_unknown_outcome(self):
        self.assertEqual(self.client.post("/voice/reminders/outcomes",json={"outcome":"no_commitment"},headers=self.headers).status_code,422)
        self.assertEqual(self.outcome("make_payment").status_code,422)

    def test_unknown_browser_call_never_creates_a_fallback_customer(self):
        with TestClient(self.app) as client:
            data=client.get('/voice/reminders/context',params={'agent_id':'agent-1','call_id':'unknown'},headers=self.headers).json()
            self.assertEqual(data['context_status'],'unavailable')
            self.assertEqual(data['customer_name'],'')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0],1)

if __name__ == '__main__':unittest.main()

"""Customer context uses actual saved turns and reconciled ledger receipts."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from payment_reminder.analysis import Result, TemporaryFailure, validate
from payment_reminder.customer_context import build_customer_brief, render_customer_brief, MAX_BRIEF_CHARS
from payment_reminder.harness import create_harness, Entry, IST

SECRET = 'test-only-' + 40 * 'x'


class CustomerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = create_harness(Path(self.tmp.name) / 'context.sqlite', SECRET, background=False).state.harness
        self.at = datetime(2026, 10, 23, 10, tzinfo=IST)
        self.h.clock = lambda db: self.at
        self.tools = TestClient(self.h.tool_app)

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def saved_call(self, call_id='promise', cid='rahul', *, amount='5000', quote=None, details=None):
        _, context = self.h.prepare(cid, call_id)
        quote = quote or 'I will pay 5000 on 25 October.'
        turns = [{'role': 'assistant', 'content': 'Your unpaid balance is INR ' + context['outstanding_amount']},
                 {'role': 'user', 'content': quote}]
        with self.h.store.connect() as db:
            db.execute('UPDATE demo_calls SET status=\'completed\',created_at=?,transcript=? WHERE id=?',
                       (self.at.isoformat(), json.dumps(turns), call_id))
            db.execute("INSERT INTO tool_receipts(call_id,tool,status,created_at) VALUES(?,'context','ready',?)", (call_id, self.at.isoformat()))
        self.h.process(call_id)
        self.h.extraction.queue(call_id)
        result = Result(summary=quote, next_step='Check the account after the agreed date.',
                        outcome='promise_to_pay' if amount else 'no_commitment',
                        promised_amount=amount, payment_date_start='2026-10-25' if amount else '',
                        payment_date_end='2026-10-25' if amount else '', payment_timeframe='',
                        callback_at='', payment_reference='',
                        evidence=[{'turn': 2, 'quote': quote, 'supports': kind}
                                  for kind in (('outcome', 'amount', 'payment_window') if amount else ('outcome',))],
                        customer_details=details or [])
        with self.h.store.connect() as db:
            source = json.loads(db.execute('SELECT input_json FROM conversation_analyses WHERE call_id=?', (call_id,)).fetchone()[0])
        validate(result, source)
        self.h.extraction.apply(call_id, result, {'model': 'test'})
        return result, source

    def brief(self, cid='rahul'):
        with self.h.store.connect() as db:
            return build_customer_brief(self.h, db, cid)

    def payment(self, amount, reference):
        self.h.entry(Entry(customer_id='rahul', amount=amount, reference=reference), 'payment')

    def test_a_promise_is_not_a_delivered_payment(self):
        self.saved_call()
        brief = self.brief()
        comparison = brief['latest_plan_compared_with_ledger']
        self.assertEqual(comparison['promised_amount'], '5000.00')
        self.assertEqual(comparison['reconciled_payments_recorded_since_call'], '0.00')
        self.assertFalse(comparison['due_date_has_passed'])
        self.assertEqual(brief['current_unpaid_amount'], '18000.00')
        self.assertEqual(brief['total_reconciled_payments'], '0.00')

    def test_partial_receipt_at_same_timestamp_uses_ledger_checkpoint(self):
        self.saved_call()
        self.payment('2000', 'actual-payment')
        comparison = self.brief()['latest_plan_compared_with_ledger']
        self.assertEqual(comparison['reconciled_payments_recorded_since_call'], '2000.00')
        self.assertEqual(comparison['promised_amount_not_covered_by_recorded_receipts'], '3000.00')
        self.assertEqual(comparison['comparison_basis'], 'ledger IDs')

    def test_prior_receipts_do_not_count_as_delivery_of_a_new_promise(self):
        self.payment('2000', 'before-promise')
        self.saved_call()
        self.payment('1000', 'after-promise')
        brief = self.brief()
        self.assertEqual(brief['total_reconciled_payments'], '3000.00')
        self.assertEqual(brief['current_unpaid_amount'], '15000.00')
        self.assertEqual(brief['latest_plan_compared_with_ledger']['reconciled_payments_recorded_since_call'], '1000.00')

    def test_revised_promises_do_not_reuse_old_receipts(self):
        self.saved_call('older')
        self.payment('2000', 'between-promises')
        self.saved_call('newer', amount='3000', quote='I will pay the remaining 3000 on 25 October.')
        self.payment('500', 'after-new-promise')
        brief = self.brief()
        self.assertEqual(len(brief['historical_payment_plans_newest_first']), 2)
        self.assertEqual(brief['latest_plan_compared_with_ledger']['promised_amount'], '3000.00')
        self.assertEqual(brief['latest_plan_compared_with_ledger']['reconciled_payments_recorded_since_call'], '500.00')

    def test_customer_payment_claim_is_not_counted_as_a_receipt(self):
        self.saved_call()
        result, source = self.saved_call('claimed-payment', amount='', quote='I already paid the 5000.')
        result.outcome = 'payment_claimed'
        validate(result, source)
        self.h.extraction.apply('claimed-payment', result, {})
        brief = self.brief()
        self.assertEqual(brief['recent_conversations_newest_first'][0]['outcome'], 'payment_claimed')
        self.assertEqual(brief['latest_plan_compared_with_ledger']['reconciled_payments_recorded_since_call'], '0.00')
        self.assertEqual(brief['current_unpaid_amount'], '18000.00')

    def test_late_receipts_are_separate_from_receipts_by_the_due_date(self):
        self.saved_call()
        self.at += timedelta(days=1)
        self.payment('2000', 'on-time')
        self.at += timedelta(days=3)
        self.payment('3000', 'late')
        comparison = self.brief()['latest_plan_compared_with_ledger']
        self.assertEqual(comparison['reconciled_payments_recorded_since_call'], '5000.00')
        self.assertEqual(comparison['reconciled_payments_recorded_by_due_date'], '2000.00')
        self.assertTrue(comparison['due_date_has_passed'])

    def test_old_calls_without_checkpoint_use_explicit_recording_time_basis(self):
        self.saved_call()
        with self.h.store.connect() as db:
            snapshot = json.loads(db.execute("SELECT context_snapshot FROM demo_calls WHERE id='promise'").fetchone()[0])
            snapshot.pop('_ledger_checkpoint')
            db.execute("UPDATE demo_calls SET context_snapshot=? WHERE id='promise'", (json.dumps(snapshot),))
        self.at += timedelta(days=1)
        self.payment('1000', 'old-snapshot-receipt')
        comparison = self.brief()['latest_plan_compared_with_ledger']
        self.assertEqual(comparison['comparison_basis'], 'ledger entry recording times')
        self.assertEqual(comparison['reconciled_payments_recorded_since_call'], '1000.00')

    def test_new_bills_are_disclosed_without_changing_the_original_promise(self):
        self.saved_call()
        self.h.entry(Entry(customer_id='rahul', amount='2000', reference='new-bill'), 'invoice')
        brief = self.brief()
        self.assertEqual(brief['current_unpaid_amount'], '20000.00')
        self.assertEqual(brief['latest_plan_compared_with_ledger']['promised_amount'], '5000.00')
        self.assertTrue(brief['latest_plan_compared_with_ledger']['new_bills_recorded_since_call'])

    def test_personal_context_is_dated_updated_and_customer_specific(self):
        quote = 'My salary arrives on the first.'
        self.saved_call('older', amount='', quote=quote,
                        details=[{'category': 'payment_timing', 'turn': 2, 'quote': quote}])
        self.at += timedelta(hours=1)
        updated = 'My salary now arrives on the fifth.'
        self.saved_call('newer', amount='', quote=updated,
                        details=[{'category': 'payment_timing', 'turn': 2, 'quote': updated}])
        self.saved_call('other-customer', cid='priya', amount='', quote='I run a grocery shop.',
                        details=[{'category': 'work_context', 'turn': 2, 'quote': 'I run a grocery shop.'}])
        brief = self.brief()
        self.assertEqual(brief['customer_details'], [{'category': 'payment_timing', 'stated_at': self.at.isoformat(), 'customer_quote': updated}])
        self.assertNotIn('grocery', render_customer_brief(brief))

    def test_personal_context_rejects_fabricated_and_assistant_quotes(self):
        result, source = self.saved_call()
        for detail in [{'category': 'work_context', 'turn': 2, 'quote': 'Invented job'},
                       {'category': 'work_context', 'turn': 1, 'quote': source['transcript'][0]['text']}]:
            changed = result.model_copy(deep=True)
            from payment_reminder.analysis import CustomerDetail
            changed.customer_details = [CustomerDetail(**detail)]
            with self.assertRaises(TemporaryFailure):
                validate(changed, source)
        self.assertNotIn('_ledger_checkpoint', source['context'])

    def test_relevant_customer_detail_survives_beyond_the_recent_six_calls(self):
        quote = 'Please speak in Hindi.'
        self.saved_call('preference', amount='', quote=quote,
                        details=[{'category': 'language_preference', 'turn': 2, 'quote': quote}])
        for n in range(7):
            self.saved_call('later-' + str(n), amount='', quote='I do not have a payment date yet.')
        brief = self.brief()
        self.assertEqual(len(brief['recent_conversations_newest_first']), 6)
        self.assertEqual(brief['customer_details'][0]['customer_quote'], quote)

    def test_prepared_call_delivers_brief_through_existing_bound_field(self):
        self.saved_call()
        self.payment('1000', 'recorded')
        _, context = self.h.prepare('rahul', 'next-call')
        response = self.tools.get('/voice/reminders/context', params={'agent_id': 'ua_afe624fa', 'call_id': 'next-call'},
                                  headers={'Authorization': 'Bearer ' + SECRET})
        self.assertEqual(response.status_code, 200)
        brief = json.loads(response.json()['prior_contact_summary'])
        self.assertEqual(brief['latest_plan_compared_with_ledger']['reconciled_payments_recorded_since_call'], '1000.00')
        self.assertEqual(response.json()['prior_contact_summary'], context['prior_contact_summary'])
        self.assertNotIn('_ledger_checkpoint', response.json())

    def test_brief_is_bounded_and_keeps_the_latest_plan(self):
        self.saved_call()
        brief = self.brief()
        record = brief['recent_conversations_newest_first'][0]
        brief['recent_conversations_newest_first'] = [record | {'summary': 'Long history ' * 70}] * 30
        text = render_customer_brief(brief)
        self.assertLessEqual(len(text), MAX_BRIEF_CHARS)
        self.assertEqual(json.loads(text)['latest_plan_compared_with_ledger']['promised_amount'], '5000.00')
        self.assertEqual(len(brief['recent_conversations_newest_first']), 30)


if __name__ == '__main__':
    unittest.main()

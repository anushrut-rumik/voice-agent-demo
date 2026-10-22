The previous customer sidebar and account workspace have been restored at the user’s request. Import Excel and immediate demo calling remain available; the table redesign described in the following historical verification is not the current UI.

# Spreadsheet-first demo update — 23 October 2026

Drop Excel or CSV → customers, bills and notes load together → Start reminders → eligible calls begin immediately → briefs and next steps appear in the customer list. Future follow-ups and stop requests are respected. New customers do not require a minimum INR 5,000 balance unless a threshold is supplied. The earlier manual-entry, business-hours-only workflow below is historical.

Verification: 106 backend tests and 9 interface tests passed. An isolated browser run imported two fictional customers through the real import API, then completed two simulated carrier calls with saved briefs and next steps. Excel parsing and upload handling were also tested independently. No new real calls or reminder runs were created. The actual agent/callback connection passed its no-call check.

---

# Current CRM acceptance flow

The current workflow is documented in README.md. Start the full setup with `.venv/bin/python scripts/run_crm.py`, save actual customer contact numbers, add their invoices, and click **Start reminders** for one pass through due accounts. Use `--local-only` to inspect data and UI without a public callback or phone calls.

Verify each customer receives their own bill context, that each queued request produces at most one dial, and that the completed call saves a transcript, Gemini brief, current status and next step. Confirm pause, changed contacts, payment cancellation, stop suppression and recovery after an uncertain response. Listen to an authorized live call before claiming spoken quality.

During the CRM upgrade, 95 backend and 6 console tests passed using an isolated fake carrier. The requested Gemini 3.5 Flash endpoint was verified with a synthetic transcript and returned Gemini 3.6 Flash. No additional live phone calls were placed. The persistent callback connection was explicitly approved and verified: public health, authentication, unknown-call denial and all three deployed Silk tool URLs passed. The CRM is enabled, with no run started. Local-preview mode remains available and explicitly disables phone calls.

The earlier browser and phone acceptance notes below are historical evidence, not the current administrator workflow.

---

# Asha: live payment-reminder console

Open http://127.0.0.1:8766 in Chrome or Helium. The in-app browser may not complete microphone permission. The console does not start a Rumik call until microphone permission succeeds.

1. Select a fictional customer. The page shows their balance and invoice references.
2. Click **Talk to Asha**. The server registers a Rumik call, binds the selected account to that call ID, then starts the agent. It returns audio credentials only after the context endpoint records a successful lookup for that exact call.
3. Speak naturally in Hinglish. Confirm the selected identity, then discuss the balance or give a payment date. There are no predefined conversation scenarios.
4. Finish the call. The server polls Rumik for completion and the transcript. It processes the outcome saved by Asha's tool and displays the next reminder.
5. After a payment has actually been reconciled in this fictional account, use **Record payment**. Clearing the balance cancels the pending reminder. A spoken claim of payment does not clear the ledger.

The scheduler uses the actual India Standard Time clock. It marks reminders due; browser calls require the user to start them. Automatic telephone dialing is not enabled.

For a remote visitor, use the password-protected sharing instructions in `README.md`. The console accepts only the configured HTTPS tunnel address and local addresses. Visitors share the demo accounts and can edit their invoices/payments; only one conversation can run at a time. Keep both the console tunnel and Rumik's separate authenticated tool tunnel running.

## What Asha knows

Each call receives the selected name, current balance, currency, invoice references and amounts, recorded payments, saved notes from previous live calls, current date/time, and callback hours. Seeded invoices are enriched with approved fictional purchase descriptions and due dates from `config/demo-invoice-details.json`. Unknown or mismatched invoices remain explicitly missing those details. Real payment instructions are not supplied; no real transfers are requested.

## Why the earlier call had no details

Rumik's tool context passes the agent's public handle `ua_afe624fa`. Our dispatch table stored its UUID. Comparing these directly rejected the prepared account. The tool server now maps only that known handle to its UUID. Unknown agents and unregistered calls return unavailable; the running tool server does not create a generic fallback customer.

The failed Priya conversation is preserved in the database. A narrowly scoped **Reopen after setup failure** action is available only for a completed call with no successful context lookup and no saved outcome. It cannot clear a dispute, payment-claim hold, or stop request.

## Running locally

The Rumik API key is loaded from the gitignored, owner-readable `.env` file. It is not supplied through a browser form or returned to the browser.

```sh
.venv/bin/python scripts/run_demo.py
.venv/bin/python scripts/run_harness.py
```

Keep the authorized HTTPS tunnel to port 8765 running. Its current URL is in `config/deployment.json`; all three Rumik tool URLs must use it. Port 8766 remains local. Temporary tunnel URLs can change on restart.

## Verification boundary

The automated tests use isolated temporary databases and mocked provider responses. They are contract tests, not live conversations. The real startup check on 2026-10-19 verified that Rumik called the context tool for call `01a15282-9633-7133-9c8c-320fc0c3f91a`. No browser joined that diagnostic session. A complete voice acceptance test additionally requires real user speech, a useful response, a saved tool outcome, and the resulting schedule.


## Observed live result, 19 October 2026

Call `01a15289-d1ff-7405-83d4-1fb91e1e39e9` received the selected Priya context. Its actual transcript includes Priya Mehta, invoice DEMO-PRIYA, and INR 32,500. The user declined to commit to payment. Asha saved `no_commitment` through the real outcome tool, Rumik ended the call with `agent_ended`, and post-call processing scheduled 20 October 2026 at 10:33 AM IST. The balance stayed INR 32,500. The transcript and tool/schedule evidence are preserved in `.local/live-acceptance-2026-10-19.json`.

This proves the live lookup → conversation → tool save → follow-up path for no commitment. It does not prove the promise-to-pay branch with a real spoken date. During the test, Asha also saved an unnecessary identity-only outcome before the substantive response. Prompt version 2 now explicitly forbids that; the refinement is deployed but has not had another spoken test. The latest substantive outcome is the one shown on the console and carried into future call notes.

## Simple call history

Select a customer and open a recent call to see the saved outcome, transcript and recording. Calls finish immediately using the voice agent's saved outcome. Gemini then extracts the final payment plan from the transcript in the background, including when no outcome was saved. The normal call summary and follow-up date update when extraction succeeds. There is no review badge, evidence audit or analysis retry button. Completed calls do not wait for extraction before another conversation can start.


## Conversational prompt revision — 19 October 2026

Prompt version 3 is deployed to `ua_afe624fa`. It retains natural Hinglish, Indian pronunciation, feminine self-reference and the existing voice settings. The greeting and system prompt now prioritize answering the recipient, understanding their situation, handling interruptions and carrying emotional context across turns. A saved outcome no longer directs Asha to immediately end the conversation, and callbacks require an actual request or agreement. Privacy, payment verification, stop requests and tool-result requirements remain in place.

Source: `config/system-prompt.txt` and `config/greeting.txt`; generated copy: `config/playground-config.json`. Version 2 is retained in `config/prompt-history/`. Deployment evidence: `config/prompt-v3-deployed.jpg`. The 38 existing tests passed, and generated prompt/greeting parity, the 8,000-character limit, all 13 context variables and the existing tool references were checked. This revision has not had a spoken retest; earlier live-call evidence does not qualify its conversational quality.

For the next spoken review, try asking why the balance is that amount, giving a tentative payment answer while continuing to explain, interrupting with a new question, and asking a question after an outcome save. Asha should answer the current point without repeating the introduction, forcing a callback or ending prematurely. Confirm that uncertainty stays tentative, Hinglish stays natural, and a clear goodbye or stop request is respected.


## Payment-led conversation correction — prompt version 4

Version 4 is deployed to `ua_afe624fa`. The user's review and saved transcript of call `01a152b4-fd29-7321-9203-b156ba3f9ee8` supersede the pending version-3 review above: version 3 asked generic clarification questions repeatedly, treated availability as identity confirmation, and closed after the customer declined clarification without discussing a payment plan. This diagnosis uses transcript content and user feedback, not an independent recording review.

Version 4 makes the reminder objective explicit: confirm identity, state the payment purpose and amount, ask about payment, and follow the actual answer. No doubts is neither refusal nor completion. Confusion about the agent's wording calls for a plain explanation, not an invented invoice dispute. New questions must be answered before closing, including the late-payment question ignored in the older Arjun call `01a152a8-66a0-7e67-a111-0f898fc0b7c1`. There is no minimum turn count and clear refusals/goodbyes remain respected. Hinglish and voice settings are unchanged.

Checks: 38 existing tests passed; generated prompt parity, existing context variables/tools and the 8,000-character limit checked. Deployment confirmed in Playground. Version 4 has not had a spoken retest. Version 3 is preserved under `config/prompt-history/`.

Spoken acceptance cases:
- Yes to availability: confirm identity before disclosing financial details.
- Normal pickup: after identity confirmation, state purpose/amount and ask about payment rather than offering generic clarification.
- No doubts: return to the unanswered payment question; do not save no_commitment or hang up on that basis.
- Who are you / doubt matlab: explain who is calling and why; do not invent a dispute or repeat the confusing question.
- Need time: discuss the proposed timing without repeated confirmations or promising an extension.
- A question after a promise/tool save: answer it before closing, without inventing late fees.
- Cannot give a date, refusal, busy or goodbye: accept the actual meaning without repeated pressure or forcing a callback.


## Clear speech and silence handling — prompt version 5

Version 5 is deployed to `ua_afe624fa`. The user feedback and saved transcript of Arjun call `01a152c4-7754-763d-8251-5ede2a5d869c` supersede the pending version-4 review: consecutive assistant turns, progression without a recorded identity reply, and repeated explanations using "best estimate", "settled" and "outstanding amount" were observed. This is transcript/user evidence, not an independent audio review or proof of the runtime trigger.

The revised greeting names Demo Supplies and Asha clearly. The prompt uses simple Hindi-based Hinglish, short turns, a clear bill reminder, and explicit waiting after questions. "Kaunsa payment?" gets an explanation and a comprehension question before another payment-date question. Silence is not permission to speak again, confirm identity or save an outcome.

The Playground silence-nudge setting (`idle_ladder.enabled`) is disabled and the local renderer matches it. The UI describes this control as nudging during silence and hanging up after no response; disabling it also disables that silence-specific automatic hangup. A silent browser test may therefore need the user to press End. Other provider limits are unchanged. The setting was enabled during the failing call, but its contribution to individual turns is unproven.

Verified: deployed UI, Hinglish/existing voice retained, exact generated prompt/greeting parity, 7,813-character prompt, unchanged 13 context variables and three tool references, and disabled silence control. A spoken retest remains pending. Check a silent identity response, silence after the payment question, "hello", "kaunsa payment", an interruption, no doubts, and a clear goodbye. Preserve factual limits and do not demand an invented outcome from silence. Version 4 sources remain under `config/prompt-history/`.


## Warm follow-up and complete customer brief — version 6

Version 6 is deployed. The user's rejection of the version-5 Arjun call `01a152cb-28a9-7d22-87d3-be28a2860bb0` supersedes its pending review: it was too terse, lacked purchase/due-date facts, and treated “कल, कल कर दूँ?” as a firm promise before ending. The transcript contains the question and premature closing. The saved tool note also incorrectly asserted a promise; that historical evidence is preserved.

The user explicitly approved realistic fictional demo invoice details. `config/demo-invoice-details.json` provides purchases, issue/supply dates and due dates for the four seeded invoices. Enrichment requires exact customer/reference/original-amount match. Remaining balance stays ledger-derived; payment allocations are not invented. The brief includes customer business and dated recent history, newest first. Each call uses its own reviewed summary or unverified tool note; a recent note is no longer hidden by older reviews. Unverified notes include recent customer quotations and are explicitly not proof of commitment. Earlier snapshots and ledger values were not rewritten.

The prompt removes the sentence quota and directs a warm, capable sales-follow-up style in natural Hinglish, with adequate explanation before asking for payment. It distinguishes due date, remaining balance and promised date; acknowledges earlier plans; and confirms proposals before saving them. Voice direction now requests an approachable, expressive account representative. Silence nudges remain off. Financial/privacy/stop rules remain.

Validation: 45 tests passed, including exact fixture matching, partial balances, dated history precedence, unverified-note quotations, immutable snapshots and customer-isolated review failures. The harness was restarted and its live read-only state verified. Prompt/config parity, all original variables/tools, 7,919 characters, and Playground deployment were checked. No spoken version-6 test has been performed.

Latency remains unresolved. The saved transcript has turn offsets and the provider exposes total call duration and a recording, but inspected call details do not expose per-stage speech-detection/model/playback timings. Turn-start differences include speech duration and cannot establish response latency. No model, transport or sampling setting was changed as an unverified latency fix. No recording was sent to an external model for this investigation.

## Phone acceptance flow

1. Run both local services, expose the authenticated tool service through an approved HTTPS tunnel, and update all three remote reminder-tool URLs.
2. Run `scripts/check_phone_setup.py`. This verifies setup without dialing.
3. Select an eligible fictional account. Open **Call phone** and enter the authorized test recipient's number with its country code.
4. Submit once. Answer on the phone, confirm the intended fictional customer, discuss the payment plan and hang up.
5. Inspect Recent calls for the correlated provider ID, customer context receipt, transcript, recording, saved outcome and follow-up. Listen to the recording before making speech-quality claims.
6. If the dial result is uncertain, leave the attempt reserved. The server checks for its exact case token or a bound tool ID and never automatically retries the dial. A `202` response alone does not pass phone acceptance.

The browser option remains in the phone sheet's **Try in browser** disclosure. No browser microphone is used for a phone call. Scheduled jobs become ready for a user-started call; they never auto-dial.

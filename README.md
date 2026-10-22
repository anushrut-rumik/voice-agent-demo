# Asha customer follow-ups

A small CRM for an administrator whose agent follows up on outstanding bills. The admin saves customer contacts and invoices, then clicks **Call all**. Asha calls every eligible customer with a saved number and unpaid balance, using each customer's own account context. After each call, a background Gemini worker saves a conversation brief, outcome and next step.

The included accounts and purchase details are fictional. Do not use the demo to request real transfers.

## Start the whole setup

Use the existing Python 3.13 virtual environment. Keep `RUMIK_API_KEY` and `GEMINI_API_KEY` in the private `.env` file, then run:

```sh
.venv/bin/python scripts/run_crm.py
```

Open [the workspace](http://127.0.0.1:8766/). The launcher starts the authenticated tool service, opens its temporary Cloudflare HTTPS connection, updates only the three existing Asha tool URLs, and starts the console. Agent prompts, voices and inbound number routing are left as configured. The first startup may take up to two minutes while the new callback hostname becomes reachable. If local DNS cannot resolve a quick tunnel, callback checks use Cloudflare HTTPS DNS while retaining certificate verification for the original hostname. Stopping the launcher stops the processes it owns. If a service stops unexpectedly, restart the launcher; saved run and call identities remain in the database.

The default launcher exposes only the authenticated tool callbacks; the console stays local. It requires authorization to maintain that public connection. To work on customer data and inspect history without any public connection or dialing, use:

```sh
.venv/bin/python scripts/run_crm.py --local-only
```

The existing deployed Asha agent is `ua_afe624fa`, UUID `01a14689-91b5-7010-853d-641d487819f2`. A Cloudflare client is expected at `.local/cloudflared` or on PATH. API keys and the callback credential remain on the server. Logs are under `.local/crm-logs`; the ledger and conversation history are in `.local/reminders.sqlite`.

## Administrator workflow

1. Drop an Excel (`.xlsx` or `.xls`) or CSV file into the page, or click to choose it. Customer name and an outstanding amount (or amount) column are required. Common phone, company, invoice and due-date headings are recognized automatically. Indian mobile numbers receive +91. Every additional column, including purchase descriptions, notes and previous-contact context, is saved with its bill and supplied to Asha.
2. Customers load into the existing sidebar. Select an account to see its balance, current status and latest conversation. The import result reports rows that need correction. Importing never starts calls. A downloadable CSV example shows a supported layout.
3. Click **Call all** to make one pass through all eligible customers, including anyone whose next follow-up is scheduled for later. You can repeat the full run after it finishes. Or select a customer and click **Call now** to call only that person, including before their scheduled follow-up. Both actions begin immediately, even outside normal business hours for this demo. Asha uses that customer's own imported context, then saves a conversation brief and next step automatically.
4. The customer workspace updates as calls finish. Invoice details, reconciled payments, contact edits, transcripts and recordings remain in their previous places. Pause stops the next call; ending a paused run cancels its remaining customers.

New imported and manually added customers are eligible for any positive unpaid balance, unless the file supplies a reminder threshold. Historical seeded accounts keep their existing thresholds. Both **Call all** and **Call now** are explicit requests to call immediately, regardless of future invoice or follow-up dates. Saved follow-ups do not change unless a call actually starts. Paid accounts, paused accounts and saved stop requests remain excluded from both actions. Eligibility, phone number, balance and the saved schedule are checked again before dialing. One customer gets at most one dial attempt per run, and another deliberate **Call now** after completion creates a new attempt. A lost response reuses its existing request rather than redialing.

**Call all** stays available even when no follow-ups are due. Click it again after a completed run to call everyone again, or use **Call now** for one customer. A run or active call temporarily prevents overlapping calls. The previous calls, transcripts and briefs stay saved.

The importer handles up to 2,000 rows and 5 MB per file. Repeat uploads do not duplicate bills or restore paid balances. Give multiple bills for the same customer separate invoice references. If a reference already exists with a different amount, that row is reported for correction; imported files do not rewrite reconciled payments. A missing phone number is shown in the list and never guessed from an earlier call. Uploaded files are parsed locally; fields enter the approved call context when the admin starts calls.

The original customer sidebar and account workspace are preserved. Use the small **Import Excel** action beside **Call all**, or drop a file anywhere on the page. Import results appear only after an upload.

## Customer context and outcomes

The backend owns the ledger and builds a customer brief before each call. It includes the current balance, six recent conversation summaries, the customer's actual words, historical payment plans, and relevant dated details such as salary timing, business context or language preference. New customer details are saved only when their quote matches a customer turn. The newest statement in each category replaces an older one, while useful details can survive beyond the six recent calls. Ira receives this brief through the existing `pm_prior_contact_summary` field; no extra model request is needed before dialing.

The brief compares the latest confirmed payment plan with reconciled receipts recorded since that call, including how much was recorded by the agreed date. It separates a spoken payment claim from money in the ledger. Receipts are account-level records, so the agent cannot treat them as allocation to a particular promise or reuse them against several older promises. New calls save a ledger checkpoint; older calls use entry recording times and identify that limitation. New invoices are also shown so an increased balance is not mistaken for a failed promise.

`prepare()` saves the exact brief for the selected recipient. Silk's phone before-call tool looks up the unbound dispatch by destination number; the dispatch is bound to the provider's call ID as it becomes available. Balance and outcome tools must match that identity. Updating later customer context does not rewrite old call snapshots.

The spoken prompt is payment-focused version 10 in `config/system-prompt.txt`. Asha explains the purchase, unpaid balance and supplied due date, asks when the customer can pay, understands the delay, and discusses a realistic date or partial payment. She uses relevant customer context without changing the reminder objective. When the customer proposes a payment, she reads back the amount and date or window and waits for confirmation. She then saves the outcome, says a natural farewell, and leaves room for the customer's acknowledgement before ending. A proposed plan is not an authorized extension or concession. The greeting checks the intended customer. The continuation prompt explicitly treats that opening as already spoken, even if it is absent from model history. A clear identity confirmation proceeds directly to the bill; an unclear reply gets only a brief clarification, without another introduction. This is a prompt-level workaround for the hosted greeting-history issue; spoken verification of version 10 is pending. Previous prompts and greetings remain in `config/prompt-history/`.

The calling architecture remains the existing hosted service. Its current agent API exposes background-noise and idle-ladder settings, but no Silero parameters or customer-first opening mode. This workspace does not control its microphone processing. Prompt changes do not establish that Silero is active, repair speech detection, or remove the hosted opening. Those changes require access to the actual hosted runtime. No replacement browser voice engine is installed. Prompt deployment and local tests also do not prove the timing of a spoken goodbye on a handset; that requires an authorized call.

After the provider finishes a call, the console saves the transcript and the outcome tool's result. Gemini then produces a concise brief, next step and evidence-backed payment or callback details. Later calls receive these saved summaries as historical context. The voice model does not write ledger payments or control the dialing queue.

- An agreed payment date or date range creates a later balance check.
- A callback request preserves its requested date or window; a broad week is not silently turned into a next-day callback.
- A reported payment or disputed bill pauses payment pursuit for assistance; it does not clear the ledger.
- A stop or wrong-number outcome also suppresses the actual called number.
- A reconciled payment controls the remaining balance and cancels reminders when appropriate.
- A missing transcript or unavailable brief is displayed honestly. The call remains saved, and a failed brief can be retried from call details.

## Gemini model

The brief worker defaults to `gemini-3.5-flash`, as requested. Google currently [routes that model ID to Gemini 3.6 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash.md). Each extraction records the actual returned model in its metadata. The local `.env` may override `GEMINI_MODEL`.

Briefs use structured JSON. Transcript quotes, amounts, dates and outcome consistency are validated before applying a result. Relative dates use the call date in India. Transient failures retry after 10 and 30 seconds, with at most three attempts. After a call ends, the next different phone number can proceed while its brief is saved. The same phone number waits for that brief so transcript-only stop requests are respected. The run finishes after all briefs settle; a final extraction failure never silently becomes a customer commitment or an indefinite run hold. A no-commitment result without an outcome quote uses conservative fixed wording rather than retrying solely for a missing quote. Older ordinary results cannot overwrite newer schedules.

Local workers wake when runs start, calls finish or briefs are saved, with periodic polling for recovery. Provider requests reuse HTTP connections, independent readiness reads run together, and call-status reads use at most four concurrent requests. The console refreshes every second during a run, every three seconds when idle and every fifteen seconds in a hidden tab, without overlapping state requests. These changes reduce local scheduling and display delays; hosted Silk speech generation and conversation history remain separate.

## Recovery and proof boundaries

A run and each of its dial requests are saved before contacting Silk. Repeating a start request, refreshing the page or restarting a worker reuses the same identities. An uncertain dial reserves the call slot; it never triggers an automatic redial. Recovery requires a bound tool ID or an exact dispatch-token match in provider history. If no match is found, the run remains held for checking.

Agent access, credit or call-limit rejections pause the remaining run. A callback setup failure pauses before any dial. **Resume run** checks setup again. Pausing allows an already active call to finish; ending a run cancels queued customers.

The tests use a fake carrier and isolated databases. The Gemini schema was also checked against the live API using a synthetic fictional transcript; it returned `gemini-3.6-flash`. These checks do not prove the spoken quality of a new telephone conversation. No extra live calls are required just to start or inspect the CRM.

```sh
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_console.cjs
```

## Optional console sharing

The console still supports one exact HTTPS public address with password protection. Start a separate console tunnel and pass its address to `scripts/run_crm.py --public-url https://YOUR-CONSOLE-HOST`. Visitors use the username `demo` and the password in `.local/share-password`, or an explicitly configured `DEMO_SHARE_PASSWORD` of at least 16 characters. Never put credentials in the URL.

Sharing grants the full workspace, including customer contacts, ledger controls, starting reminder runs and recordings. All visitors share the same accounts and run queue. Exact Host, Origin and console-header checks remain enforced. The laptop and launcher must stay running; temporary addresses can change on restart.

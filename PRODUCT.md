# Purpose

Let an administrator's agent follow up with customers about their outstanding bills. The admin drops an Excel or CSV file containing customers, bills and context, then starts one pass through all eligible customer contacts. The agent owns the conversation; the admin monitors progress and results without joining audio.

The complete flow is customer contact + invoices → eligible reminder → Call all → recipient-specific phone conversation → saved transcript and brief → current status and next action → scheduled follow-up → reconciled payment → reminders stop.

# Operating behavior

Call all includes all eligible customers with a saved contact, regardless of their next follow-up date, and can be repeated after a completed run. Call now on a selected account creates a one-customer run and explicitly permits calling before its follow-up date. Both make one attempt per customer and begin immediately when the admin clicks, including outside business hours for this demo. Paid balances, paused accounts, saved stops and reminder thresholds still apply. Both actions explicitly allow calls before future follow-up dates. The queue survives restart. Before every dial, recheck balance, account status, contact suppression and the original contact and schedule. Never blindly retry an uncertain call.

Gemini creates concise conversation briefs, next steps and supported payment/callback facts. Use the requested gemini-3.5-flash model ID and retain the actual returned model, currently Gemini 3.6 Flash. The backend owns money, dates, suppression and scheduling. Spoken payment claims never clear the ledger.

# Administrator experience

Keep the established quiet light interface: customer sidebar, selected account balance, current status, last conversation and recent calls. Do not replace it with a table or a large upload area. Excel import is a small action alongside Call all. Call now sits with the selected customer's next action. Future follow-up dates never disable Call all or redirect the admin to individual calling. Contacts are saved once; prior test numbers never become customer contacts. Technical connection controls stay behind disclosure. There are no quality scores, review queues or approval badges in the customer workflow.

The seeded business data is fictional. Preserve real call evidence and earlier prompt versions. Local tests and provider acceptance are separate from an answered, useful handset conversation.

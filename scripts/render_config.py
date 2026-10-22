"""Render Playground/API configuration. Does not contact Rumik or deploy anything."""
import argparse
import json
from pathlib import Path
from payment_reminder.app import FIELDS

ROOT = Path(__file__).resolve().parents[1]
PARAMS = {
    "outcome": "One of promise_to_pay, callback_requested, payment_claimed, dispute, wrong_number, do_not_call, human_requested, no_commitment, balance_cleared.",
    "promised_amount": "Confirmed amount as decimal digits, no currency symbol. Empty if unknown or not applicable.",
    "promised_date": "Confirmed payment date YYYY-MM-DD. Empty if tentative, ambiguous, or not applicable.",
    "callback_date": "Requested local callback date YYYY-MM-DD; empty if unknown.",
    "callback_time": "Requested local time HH:MM in the supplied timezone; empty if no exact time given.",
    "customer_statement": "Short faithful excerpt. Preserve uncertainty and broad time windows. Empty if none.",
    "payment_reference": "Reference voluntarily supplied by the customer. Empty if none.",
    "summary": "Brief factual summary. Do not claim verification unless the balance tool confirmed it."
}


def render(base):
    def tool(name, kind, method, path, description, params, outputs, llm=None, body=""):
        return {"name": name, "kind": kind, "description": description, "config": {
            "action": method.lower(), "method": method, "url": base.rstrip("/")+path,
            "auth": {"type": "bearer", "token": "REPLACE_WITH_REMINDER_TOOL_TOKEN"},
            "timeoutMs": 5000, "waitForResponse": True,
            "headers": [{"name":"Content-Type", "value":"application/json"}] if method == "POST" else [],
            "params": [{"name": k, "value": v} for k,v in params.items()], "body": body,
            "llmParams": [{"name":k,"type":"string","description":v,"required":True} for k,v in (llm or {}).items()],
            "outputs": [{"name":x,"path":x} for x in outputs]}}
    ctx = tool("pm_get_reminder_context", "before_call", "GET", "/voice/reminders/context",
               "Load verified context for the prepared payment reminder dispatch; return unavailable on an ambiguous or missing match.",
               {"phone_number":"{phone_number}","agent_id":"{agent_id}","call_id":"{call_id}"}, FIELDS)
    refresh = tool("pm_refresh_reminder_balance", "during_call", "GET", "/voice/reminders/balance",
                   "Read the latest recorded balance when the customer reports a payment or questions the current amount. Never creates a payment.",
                   {"case_token":"{pm_case_token}","call_id":"{call_id}"},
                   ["context_status","case_status","outstanding_amount","currency","balance_as_of","payment_status"])
    body = {"case_token":"{pm_case_token}","call_id":"{call_id}",**{k:"{"+k+"}" for k in PARAMS}}
    record = tool("pm_record_reminder_outcome", "during_call", "POST", "/voice/reminders/outcomes",
                  "Record established conversation facts promptly. Unknown fields are empty. This does not verify payment or guarantee a callback. Read the result before claiming success.",
                  {}, ["save_status","case_status","action_status","scheduled_callback_at"], PARAMS, json.dumps(body))
    prompt = (ROOT/"config/system-prompt.txt").read_text().strip()
    if len(prompt)>8000:
        raise ValueError(f"Prompt too long: {len(prompt)}")
    return {"agent": {"name":"Payment Reminder Demo - Asha (Hinglish)","systemInstruction":prompt,
                      "greeting":(ROOT/"config/greeting.txt").read_text().strip(),
                      "ttsConfig":{"model":"mulberry","voice":"speaker_1","description":"A warm, approachable Indian female voice. Use natural Hinglish, an attentive conversational rhythm, gentle expressiveness and comfortable pauses. Sound like a friendly, confident account representative."},
                      "language":"hinglish","callSettings":{"background_noise":{"enabled":False},"idle_ladder":{"enabled":False}}},
            "tools":[ctx,refresh,record],
            "variables":[{"name":"pm_"+k,"defaultValue":"unavailable" if k in ("context_status","case_status") else "",
                          "toolId":"REPLACE_WITH_CONTEXT_TOOL_ID","toolOutput":k} for k in FIELDS]}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--base-url",default="https://YOUR_BACKEND")
    args=parser.parse_args()
    config=render(args.base_url)
    dest=ROOT/"config/playground-config.json"
    dest.write_text(json.dumps(config,ensure_ascii=False,indent=2)+"\n")
    print(f"Wrote {dest}; prompt {len(config['agent']['systemInstruction'])} characters. No external changes.")

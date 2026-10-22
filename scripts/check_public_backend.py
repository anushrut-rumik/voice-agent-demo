"""Exercise our authenticated demo endpoints over HTTPS. Makes no Rumik calls."""
import argparse
import json
import secrets
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from payment_reminder.app import Store, demo_context

parser=argparse.ArgumentParser()
parser.add_argument("base_url")
args=parser.parse_args()
base=args.base_url.rstrip("/")
secret=Path(".local/tool-token").read_text().strip()

def request(path,body=None,auth=True):
    headers={"Content-Type":"application/json"}
    if auth: headers["Authorization"]="Bearer "+secret
    req=Request(base+path,data=json.dumps(body,ensure_ascii=False).encode() if body else None,headers=headers)
    with urlopen(req,timeout=30) as response:
        return json.load(response)

assert request("/health")["status"]=="ok"
try:
    request("/voice/reminders/context",auth=False)
    raise AssertionError("Unauthenticated request was accepted")
except HTTPError as exc:
    assert exc.code==401
call_id="http-smoke-"+secrets.token_hex(8)
# Isolated endpoint fixture; never add a synthetic conversation to demo_calls.
Store('.local/reminders.sqlite').seed('demo-supplies','01a14689-91b5-7010-853d-641d487819f2','',demo_context(),call_id=call_id)
context=request("/voice/reminders/context?agent_id=ua_afe624fa&call_id="+call_id)
assert context["context_status"]=="ready"
result=request("/voice/reminders/balance?case_token="+context["case_token"]+"&call_id="+call_id)
assert result["payment_status"]=="not_cleared"
body={"case_token":context["case_token"],"call_id":call_id,"outcome":"no_commitment",
      "customer_statement":'उन्होंने कहा: "अगले हफ्ते"\nNo firm commitment.',"summary":"Synthetic HTTP test, not a voice call."}
assert request("/voice/reminders/outcomes",body)["save_status"]=="saved"
assert request("/voice/reminders/outcomes",body)["save_status"]=="duplicate"
print("PASS: public HTTPS health, authentication, context, balance, Unicode JSON outcome, duplicate protection. No voice call placed.")

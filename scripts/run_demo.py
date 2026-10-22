"""Run the tools on loopback only. Public access requires a separately authorized tunnel."""
import os
import secrets
from pathlib import Path
import uvicorn
from payment_reminder.app import create_app

root=Path(__file__).resolve().parents[1]
local=root/".local"
local.mkdir(mode=0o700,exist_ok=True)
secret_path=local/"tool-token"
if not secret_path.exists():
    fd=os.open(secret_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as f:
        f.write(secrets.token_urlsafe(48))
app=create_app(local/"reminders.sqlite",secret_path.read_text().strip(),"demo-supplies",
               agent_aliases={"ua_afe624fa":"01a14689-91b5-7010-853d-641d487819f2"})
uvicorn.run(app,host="127.0.0.1",port=8765,access_log=False)

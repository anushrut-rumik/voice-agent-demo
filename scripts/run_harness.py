"""Start the console, optionally allowing one password-protected tunnel URL."""
import argparse
import os
import secrets
from pathlib import Path
import uvicorn
from dotenv import load_dotenv
from payment_reminder.harness import create_harness

root=Path(__file__).resolve().parents[1]
load_dotenv(root / '.env', override=False)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--public-url',default=os.environ.get('DEMO_PUBLIC_URL',''),
                    help='Exact HTTPS tunnel URL; omit to keep the console local only.')
args=parser.parse_args()
password=os.environ.get('DEMO_SHARE_PASSWORD','')
if args.public_url and not password:
    password_path=root/'.local/share-password'
    password_path.parent.mkdir(mode=0o700,exist_ok=True)
    if not password_path.exists():
        fd=os.open(password_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    password_path.chmod(0o600)
    password=password_path.read_text().strip()
    print(f'Sharing username: demo. Password saved in {password_path}',flush=True)
secret=root/'.local/tool-token'
if not secret.exists():
    raise SystemExit('Run scripts/run_demo.py once to create the tool credential first.')
app=create_harness(root/'.local/reminders.sqlite',secret.read_text().strip(),os.environ.get('RUMIK_API_KEY',''),
                   gemini_key=os.environ.get('GEMINI_API_KEY',''),gemini_model=os.environ.get('GEMINI_MODEL'),
                   public_url=args.public_url,share_password=password,from_number=os.environ.get('RUMIK_FROM_NUMBER',''))
uvicorn.run(app,host='127.0.0.1',port=8766,access_log=False)

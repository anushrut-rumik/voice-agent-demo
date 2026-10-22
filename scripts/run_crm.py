"""Start the CRM, its authenticated tools and the HTTPS callback connection."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx
from dotenv import load_dotenv
from payment_reminder.setup import connect_tools
from payment_reminder.callback_http import callback_get

ROOT=Path(__file__).resolve().parents[1]


async def public_health(base):
    async with httpx.AsyncClient(timeout=3,follow_redirects=False) as client:
        return await callback_get(client,base+'/health')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-url',default='',help='Optional password-protected public console address.')
    parser.add_argument('--local-only',action='store_true',help='Start local services without a public tunnel or remote changes.')
    args=parser.parse_args();load_dotenv(ROOT/'.env',override=False)
    if args.local_only:os.environ['DEMO_CRM_LOCAL_ONLY']='1'
    if not os.environ.get('RUMIK_API_KEY') or not os.environ.get('GEMINI_API_KEY'):
        raise SystemExit('Set RUMIK_API_KEY and GEMINI_API_KEY in the private .env file first.')
    tunnel=ROOT/'.local/cloudflared'
    binary=str(tunnel) if tunnel.is_file() else shutil.which('cloudflared')
    if not binary and not args.local_only:raise SystemExit('Install Cloudflare Tunnel, or restore .local/cloudflared, before running the CRM.')
    for port in (8765,8766):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise SystemExit(f'Port {port} is already in use. Stop the previous demo launcher before starting this one.')
    logs=ROOT/'.local/crm-logs';logs.mkdir(parents=True,exist_ok=True,mode=0o700)
    children=[];handles=[]
    def stop(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    def log_file(name):
        path=logs/name
        fd=os.open(path,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
        handle=os.fdopen(fd,'w');handles.append(handle);return handle
    def launch(command,log):
        child=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        children.append(child);return child
    def wait_local(url,child,auth=None):
        for _ in range(60):
            if child.poll() is not None:raise RuntimeError('A local service stopped. See .local/crm-logs.')
            try:
                if httpx.get(url,timeout=1,auth=auth).is_success:return
            except httpx.HTTPError:pass
            time.sleep(.25)
        raise RuntimeError('A local service did not become ready. See .local/crm-logs.')
    try:
        tools=launch([sys.executable,'scripts/run_demo.py'],log_file('tools.log'))
        wait_local('http://127.0.0.1:8765/health',tools)
        if not args.local_only:
            public_urls=queue.Queue()
            cloud=subprocess.Popen([binary,'tunnel','--url','http://127.0.0.1:8765','--protocol','http2','--no-autoupdate'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            children.append(cloud);tunnel_log=log_file('tunnel.log')
            def read_tunnel():
                for line in cloud.stdout:
                    tunnel_log.write(line);tunnel_log.flush()
                    match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
                    if match:public_urls.put(match[0])
            threading.Thread(target=read_tunnel,daemon=True).start()
            try:base=public_urls.get(timeout=40)
            except queue.Empty:raise RuntimeError('The HTTPS callback tunnel did not start. See .local/crm-logs/tunnel.log.') from None
            # New tunnel DNS can take a minute to propagate. Wait only for
            # read-only health; remote writes are submitted once.
            print('Connecting Asha’s customer tools. A new callback address can take up to two minutes to become reachable.',flush=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                try:
                    response=asyncio.run(public_health(base))
                    if response.is_success and response.json().get('status')=='ok':break
                except (httpx.HTTPError,ValueError):pass
                if cloud.poll() is not None:raise RuntimeError('The callback tunnel stopped.')
                time.sleep(1)
            else:raise RuntimeError('The HTTPS callback is not reachable. No calls were started.')
            asyncio.run(connect_tools(base,os.environ['RUMIK_API_KEY'],(ROOT/'.local/tool-token').read_text().strip()))
            path=ROOT/'config/deployment.json';deployment=json.loads(path.read_text());deployment['backendUrl']=base
            deployment['tunnelRenewedAt']=time.strftime('%Y-%m-%d',time.gmtime());path.write_text(json.dumps(deployment,ensure_ascii=False,indent=2)+'\n')
            path=ROOT/'config/playground-config.json';config=json.loads(path.read_text())
            for tool in config['tools']:
                if tool['name'] in ('pm_get_reminder_context','pm_refresh_reminder_balance','pm_record_reminder_outcome'):
                    tool['config']['url']=base+'/voice/reminders/'+tool['config']['url'].rsplit('/',1)[1]
            path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
        command=[sys.executable,'scripts/run_harness.py']
        if args.public_url:command+=['--public-url',args.public_url]
        console=launch(command,log_file('console.log'))
        auth=None
        if args.public_url or os.environ.get('DEMO_PUBLIC_URL'):
            for _ in range(40):
                password=os.environ.get('DEMO_SHARE_PASSWORD','')
                password_file=ROOT/'.local/share-password'
                if not password and password_file.exists():password=password_file.read_text().strip()
                if password:auth=('demo',password);break
                time.sleep(.1)
        wait_local('http://127.0.0.1:8766/api/state',console,auth)
        print('Asha CRM is ready at http://127.0.0.1:8766',flush=True)
        print('Local-only mode: no public connection or remote settings were changed.' if args.local_only else 'Customer tools are connected. Save customer numbers, then click Call all due for one pass. No run was started by this launcher.',flush=True)
        while all(child.poll() is None for child in children):time.sleep(1)
        raise RuntimeError('A CRM service stopped. See .local/crm-logs. Saved attempts remain available for recovery.')
    except KeyboardInterrupt:pass
    except (RuntimeError,httpx.HTTPError) as exc:raise SystemExit(str(exc)) from None
    finally:
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        for handle in handles:handle.close()


if __name__=='__main__':main()

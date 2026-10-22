"""Read-only phone setup check. Never places a call or changes remote settings."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
AGENT='ua_afe624fa'
EXPECTED={
    'pm_get_reminder_context':('/voice/reminders/context','before_call'),
    'pm_refresh_reminder_balance':('/voice/reminders/balance','during_call'),
    'pm_record_reminder_outcome':('/voice/reminders/outcomes','during_call'),
}


async def main():
    load_dotenv(ROOT/'.env',override=False)
    key=os.environ.get('RUMIK_API_KEY','')
    if not key:raise SystemExit('NOT READY: RUMIK_API_KEY is missing.')
    token_path=ROOT/'.local/tool-token'
    if not token_path.exists():raise SystemExit('NOT READY: Run scripts/run_demo.py first.')
    token=token_path.read_text().strip()
    issues=[]
    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
        headers={'Authorization':'Bearer '+key}
        agent=await client.get('https://silk-api.rumik.ai/v1/agents/'+AGENT,headers=headers)
        agent.raise_for_status();agent=agent.json()
        if agent.get('deployed') is not True:issues.append('Asha is not deployed.')
        if agent.get('ttsConfig',{}).get('model') not in ('muga','mulberry','mulberry-1.6'):
            issues.append('The agent voice model has not been qualified for phone calls.')
        response=await client.get('https://silk-api.rumik.ai/v1/tools',headers=headers)
        response.raise_for_status()
        tools=response.json().get('items',[])
        origins=set()
        for name,(path,kind) in EXPECTED.items():
            matches=[t for t in tools if t.get('name')==name]
            if len(matches)!=1:
                issues.append(name+': expected exactly one tool.');continue
            tool=matches[0];config=tool.get('config',{})
            url=urlsplit(config.get('url',''))
            if url.scheme!='https' or url.path!=path or url.query or url.fragment or url.username:
                issues.append(name+': incorrect HTTPS tool address.');continue
            origins.add(url.scheme+'://'+url.netloc)
            if tool.get('kind')!=kind or config.get('waitForResponse') is not True:
                issues.append(name+': must wait for its result with the expected tool kind.')
            auth=config.get('auth',{})
            # Silk deliberately omits credentials from GET responses. A view
            # can establish that a secret exists, not compare its plaintext.
            if auth.get('type')!='bearer' or auth.get('hasSecret') is not True:
                issues.append(name+': a stored bearer credential is required.')
            if name=='pm_get_reminder_context':
                params={p.get('name'):p.get('value') for p in config.get('params',[])}
                if any(params.get(k)!='{'+k+'}' for k in ('phone_number','agent_id','call_id')):
                    issues.append(name+': missing phone, agent or call binding parameter.')
        if len(origins)!=1:issues.append('All three tools must use the same public service.')
        for origin in origins:
            try:
                health=await client.get(origin+'/health');health.raise_for_status()
                if health.json().get('status')!='ok':raise ValueError('Invalid health response')
                denied=await client.get(origin+'/voice/reminders/context')
                if denied.status_code!=401:issues.append('The public tool service must reject unauthenticated access.')
                # No dispatch is created and no customer or phone number is sent.
                context=await client.get(origin+'/voice/reminders/context',params={'agent_id':AGENT,'call_id':'phone-setup-check'},headers={'Authorization':'Bearer '+token})
                if context.status_code!=200 or context.json().get('context_status')!='unavailable':
                    issues.append('The public service did not safely reject an unknown call.')
                print('Tool service:',origin)
            except (httpx.HTTPError,ValueError):issues.append('The configured public tool service is unreachable or invalid.')
    if issues:
        for issue in issues:print('NOT READY:',issue)
        raise SystemExit(1)
    print('PASS: deployed phone-capable agent, tool settings, stored credentials and public callback access. No call placed. Stored credentials are redacted; their actual use, carrier funding, destination support and handset audio still need an authorized live test.')


if __name__=='__main__':
    try:asyncio.run(main())
    except httpx.HTTPError as exc:raise SystemExit('NOT READY: Rumik could not be checked ('+type(exc).__name__+').') from None

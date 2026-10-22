"""Check the configured agent and its approved callback service without dialing."""
import json
from urllib.parse import urlsplit

import httpx
from payment_reminder.callback_http import callback_get
from fastapi import HTTPException


async def check_setup(harness):
    from payment_reminder.harness import AGENT, ROOT
    agent=await harness.upstream('GET','/v1/agents/'+AGENT)
    if not agent.get('deployed'):
        raise HTTPException(409,'Deploy the Asha agent before starting reminders.')
    if agent.get('ttsConfig',{}).get('model') not in ('muga','mulberry','mulberry-1.6'):
        raise HTTPException(409,'The selected voice cannot take phone calls.')
    expected=urlsplit(json.loads((ROOT/'config/deployment.json').read_text())['backendUrl'])
    origin=expected.scheme+'://'+expected.netloc
    if expected.scheme!='https':raise HTTPException(409,'A public HTTPS tool connection is required.')
    response=await harness.upstream('GET','/v1/tools')
    for name,path in {'pm_get_reminder_context':'context','pm_refresh_reminder_balance':'balance','pm_record_reminder_outcome':'outcomes'}.items():
        tools=[t for t in response.get('items',[]) if t.get('name')==name]
        if len(tools)!=1:raise HTTPException(409,'Asha’s reminder tools are not configured.')
        conf=tools[0].get('config',{})
        if conf.get('url')!=origin+'/voice/reminders/'+path or not conf.get('waitForResponse'):
            raise HTTPException(409,'Asha’s tools must use the current callback connection.')
        if conf.get('auth',{}).get('type')!='bearer' or not conf.get('auth',{}).get('hasSecret'):
            raise HTTPException(409,'Asha’s tools need their saved access credential.')
    async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
        health=await callback_get(client,origin+'/health');health.raise_for_status()
        if health.json().get('status')!='ok':raise HTTPException(409,'The tool service is unavailable.')
        denied=await callback_get(client,origin+'/voice/reminders/context')
        if denied.status_code!=401:raise HTTPException(409,'The tool service must require authentication.')
        check=await callback_get(client,origin+'/voice/reminders/context',
            params={'agent_id':AGENT,'call_id':'reminder-setup-check'},
            headers={'Authorization':'Bearer '+harness.tool_token})
        check.raise_for_status()
        if check.json().get('context_status')!='unavailable':
            raise HTTPException(409,'The tool service did not reject an unknown call.')
    return {'status':'ready'}

"""Connect the existing deployed agent to this tool service; never dial."""
from urllib.parse import urlsplit

import httpx
from payment_reminder.callback_http import callback_get

PATHS={'pm_get_reminder_context':'context','pm_refresh_reminder_balance':'balance','pm_record_reminder_outcome':'outcomes'}


async def connect_tools(base, api_key, tool_token, client=None):
    parsed=urlsplit(base)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Use one exact HTTPS tool origin.')
    base=base.rstrip('/')
    if client is None:
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as session:
            return await connect_tools(base,api_key,tool_token,session)
    health=await callback_get(client,base+'/health');health.raise_for_status()
    if health.json().get('status')!='ok':raise RuntimeError('The public tool service is not healthy.')
    denied=await callback_get(client,base+'/voice/reminders/context')
    if denied.status_code!=401:raise RuntimeError('The public tool service must require authentication.')
    response=await callback_get(client,base+'/voice/reminders/context',params={'agent_id':'ua_afe624fa','call_id':'crm-setup-check'},headers={'Authorization':'Bearer '+tool_token})
    response.raise_for_status()
    if response.json().get('context_status')!='unavailable':raise RuntimeError('Unknown calls must not receive customer data.')
    headers={'Authorization':'Bearer '+api_key}
    response=await client.get('https://silk-api.rumik.ai/v1/tools',headers=headers);response.raise_for_status()
    tools=response.json().get('items',[])
    for name,path in PATHS.items():
        matches=[t for t in tools if t.get('name')==name]
        if len(matches)!=1:raise RuntimeError('Expected one existing '+name+' tool.')
        tool=matches[0];config=dict(tool['config']);config['url']=base+'/voice/reminders/'+path
        endpoint='https://silk-api.rumik.ai/v1/tools/'+tool['id']
        if tool['config'].get('url')!=config['url']:
            # Omitting the redacted token preserves Silk's stored credential.
            # A failed/uncertain PATCH is not automatically retried.
            response=await client.patch(endpoint,headers=headers,json={'config':config});response.raise_for_status()
        response=await client.get(endpoint,headers=headers);response.raise_for_status()
        if response.json()['config']!=config:raise RuntimeError('A reminder tool update did not verify.')
    return {'status':'ready','tool_base_url':base}

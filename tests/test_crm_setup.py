"""Setup performs only callback checks and existing-tool URL updates."""
import unittest
import httpx
from payment_reminder.setup import connect_tools,PATHS


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_preserves_stored_secrets_and_never_calls(self):
        origin='https://crm-tools.example'
        tools=[{'id':name,'name':name,'config':{'url':'https://old.example/voice/reminders/'+path,'auth':{'type':'bearer','hasSecret':True},'waitForResponse':True}} for name,path in PATHS.items()]
        writes=[]
        def handler(request):
            import json
            if request.url.path=='/health':return httpx.Response(200,json={'status':'ok'})
            if request.url.host=='crm-tools.example':
                if 'authorization' not in request.headers:return httpx.Response(401)
                self.assertEqual(request.headers['authorization'],'Bearer local-tool-secret')
                return httpx.Response(200,json={'context_status':'unavailable'})
            self.assertNotIn('/calls',request.url.path)
            if request.url.path=='/v1/tools':return httpx.Response(200,json={'items':tools})
            tool=next(t for t in tools if t['id']==request.url.path.rsplit('/',1)[1])
            if request.method=='PATCH':
                conf=json.loads(request.content)['config'];self.assertNotIn('token',conf['auth'])
                self.assertTrue(conf['auth']['hasSecret']);tool['config']=conf;writes.append(tool['id'])
            return httpx.Response(200,json=tool)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await connect_tools(origin,'api-secret','local-tool-secret',client)
            await connect_tools(origin,'api-secret','local-tool-secret',client)
        self.assertEqual(len(writes),3)

    async def test_public_unauthenticated_context_blocks_remote_changes(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={'status':'ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RuntimeError):await connect_tools('https://crm-tools.example','api','local',client)
        self.assertTrue(all(r.url.host=='crm-tools.example' for r in requests))

    async def test_non_https_origin_is_refused_before_any_network(self):
        with self.assertRaises(ValueError):await connect_tools('http://example.com','api','local')

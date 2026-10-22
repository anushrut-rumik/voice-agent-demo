"""Tunnel access contracts; no real calls or changes to the demo account database."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from payment_reminder.access import ConsoleAccess
from payment_reminder.harness import create_harness


PUBLIC_URL = 'https://asha-demo.example'
PASSWORD = 'test-only-share-password-12345'
TOOL_SECRET = 'test-only-tool-secret-' + 'x' * 32
HEADERS = {'Origin': PUBLIC_URL, 'X-Demo-Console': '1'}


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_harness(Path(self.tmp.name) / 'test.sqlite', TOOL_SECRET,
                                  background=False, public_url=PUBLIC_URL, share_password=PASSWORD)
        self.h = self.app.state.harness
        self.client = TestClient(self.app, base_url=PUBLIC_URL)
        self.client.auth = ('demo', PASSWORD)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_all_console_content_requires_sign_in(self):
        for path in ('/', '/static/app.js', '/api/state', '/api/calls/example/recording',
                     '/api/calls/example/extraction'):
            with self.subTest(path=path):
                response = self.client.get(path, auth=None)
                self.assertEqual(response.status_code, 401)
                self.assertIn('Basic', response.headers['WWW-Authenticate'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
        for auth in (('demo', 'incorrect'), ('someone', PASSWORD)):
            self.assertEqual(self.client.get('/api/state', auth=auth).status_code, 401)
        for value in ('Bearer unrelated', 'Basic !!!', 'Basic ' + base64.b64encode(b'no-colon').decode()):
            self.assertEqual(self.client.get('/api/state', auth=None,
                                            headers={'Authorization': value}).status_code, 401)
        # A rewritten Host or a forged forwarded host cannot bypass the password.
        self.assertEqual(self.client.get('/api/state', auth=None,
                                        headers={'Host': 'localhost:8766'}).status_code, 401)
        self.assertEqual(self.client.get('/api/state',
                                        headers={'Host': 'unknown.example',
                                                 'X-Forwarded-Host': 'asha-demo.example'}).status_code, 403)

    def test_signed_in_workspace_and_ledger_actions(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 200)
        body = {'customer_id': 'rahul', 'amount': '100', 'reference': 'SHARED-PAYMENT'}
        self.assertEqual(self.client.post('/api/payments', json=body, headers=HEADERS).status_code, 200)
        customer = next(c for c in self.h.state()['customers'] if c['id'] == 'rahul')
        self.assertEqual(customer['balance'], 17900)
        local = self.client.get('/api/state', headers={'Host': 'localhost:8766'})
        self.assertEqual(local.status_code, 200)

    def test_bad_origin_missing_header_and_unauthorized_actions_do_not_write(self):
        self.h.upstream = AsyncMock()
        body = {'customer_id': 'rahul', 'amount': '100', 'reference': 'BLOCKED'}
        self.assertEqual(self.client.post('/api/payments', json=body,
                                         headers=HEADERS, auth=None).status_code, 401)
        for headers in ({'X-Demo-Console': '1'}, {'Origin': PUBLIC_URL},
                        HEADERS | {'Origin': 'https://evil.example'},
                        HEADERS | {'Origin': 'http://localhost:8766'}):
            self.assertEqual(self.client.post('/api/payments', json=body, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/live', json={'customer_id': 'rahul'},
                                         headers=HEADERS, auth=None).status_code, 401)
        self.h.upstream.assert_not_awaited()
        self.assertEqual(next(c for c in self.h.state()['customers'] if c['id'] == 'rahul')['balance'], 18000)

    def test_shared_call_still_requires_context_and_keeps_single_call_limit(self):
        tools = TestClient(self.h.tool_app)

        async def upstream(method, path, body=None, auth=True):
            if path == '/v1/register-call':
                return {'call_id': 'shared-call', 'access_token': 'one-use-token'}
            if path == '/v1/webcall':
                response = tools.get('/voice/reminders/context',
                                     params={'agent_id': 'ua_afe624fa', 'call_id': 'shared-call'},
                                     headers={'Authorization': 'Bearer ' + TOOL_SECRET})
                self.assertEqual(response.json()['customer_name'], 'Rahul Sharma')
                return {'callId': 'shared-call', 'host': 'wss://audio.example', 'token': 'audio-token'}
            raise AssertionError('Unexpected provider request')

        self.h.upstream = upstream
        try:
            response = self.client.post('/api/live', json={'customer_id': 'rahul'}, headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['context_loaded'])
            self.assertEqual(response.json()['token'], 'audio-token')
            self.assertEqual(self.client.post('/api/live', json={'customer_id': 'priya'},
                                             headers=HEADERS).status_code, 409)
        finally:
            tools.close()

    def test_invalid_sharing_config_is_rejected(self):
        for url in ('http://demo.example', 'https://demo.example/path', 'https://*.example',
                    'https://user:pass@demo.example', 'https://demo.example?x=1',
                    'https://demo.example#fragment', 'https://demo.example:invalid'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ConsoleAccess(url, PASSWORD)
        for url, password in ((PUBLIC_URL, ''), (PUBLIC_URL, 'short'), ('', PASSWORD)):
            with self.assertRaises(ValueError):
                ConsoleAccess(url, password)


if __name__ == '__main__':
    unittest.main()

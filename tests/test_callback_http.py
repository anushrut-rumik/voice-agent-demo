import socket
import unittest

import httpx

from payment_reminder.callback_http import callback_get


class CallbackDnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_fallback_preserves_hostname_and_keeps_secret_out_of_dns(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == 'new.trycloudflare.com':
                raise httpx.ConnectError('DNS unavailable') from httpx.ConnectError(socket.gaierror())
            if request.url.host == 'cloudflare-dns.com':
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, json={'Answer': [{'type': 1, 'data': '104.16.230.132'}]})
            self.assertEqual(request.url.host, '104.16.230.132')
            self.assertEqual(request.headers['host'], 'new.trycloudflare.com')
            self.assertEqual(request.extensions['sni_hostname'], 'new.trycloudflare.com')
            self.assertEqual(request.headers['authorization'], 'Bearer private')
            self.assertEqual(request.url.params['call_id'], 'unknown')
            return httpx.Response(200, json={'context_status': 'unavailable'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await callback_get(client, 'https://new.trycloudflare.com/voice/reminders/context',
                headers={'Authorization': 'Bearer private'}, params={'call_id': 'unknown'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 3)

    async def test_does_not_retry_connection_failures_or_other_hosts(self):
        for url, cause in [('https://new.trycloudflare.com/health', ConnectionRefusedError()),
                           ('https://example.com/health', socket.gaierror())]:
            requests = []
            def handler(request):
                requests.append(request)
                raise httpx.ConnectError('Unavailable') from cause
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(httpx.ConnectError):await callback_get(client, url)
            self.assertEqual(len(requests), 1)

    async def test_refuses_private_dns_result(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == 'new.trycloudflare.com':
                raise httpx.ConnectError('DNS unavailable') from socket.gaierror()
            return httpx.Response(200, json={'Answer': [{'type': 1, 'data': '127.0.0.1'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.ConnectError):
                await callback_get(client, 'https://new.trycloudflare.com/health')
        self.assertEqual(len(requests), 2)

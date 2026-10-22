"""Read callback endpoints when local DNS cannot resolve a new quick tunnel."""
import ipaddress
import socket

import httpx


async def callback_get(client, url, **kwargs):
    try:
        return await client.get(url, **kwargs)
    except httpx.ConnectError as error:
        pending, seen, dns_error = [error], set(), False
        while pending:
            cause = pending.pop()
            if not isinstance(cause, BaseException) or id(cause) in seen:continue
            seen.add(id(cause))
            dns_error |= isinstance(cause, socket.gaierror)
            pending.extend([cause.__cause__, cause.__context__, *cause.args])
        target = httpx.URL(url)
        if (not dns_error or target.scheme != 'https' or target.port not in (None, 443)
                or not target.host.endswith('.trycloudflare.com') or target.userinfo):
            raise
        # Resolve only the public hostname. Never send callback headers to DNS.
        result = await client.get('https://cloudflare-dns.com/dns-query',
            params={'name': target.host, 'type': 'A'}, headers={'Accept': 'application/dns-json'})
        result.raise_for_status()
        addresses = [ipaddress.IPv4Address(answer['data'])
                     for answer in result.json().get('Answer', []) if answer.get('type') == 1]
        if not addresses or not addresses[0].is_global:
            raise error
        # The connection uses the resolved IP, but TLS still verifies the original
        # hostname and Cloudflare receives that hostname in the HTTP Host header.
        headers = dict(kwargs.pop('headers', {}))
        headers['Host'] = target.host
        extensions = dict(kwargs.pop('extensions', {}))
        extensions['sni_hostname'] = target.host
        return await client.get(target.copy_with(host=str(addresses[0])),
                                headers=headers, extensions=extensions, **kwargs)

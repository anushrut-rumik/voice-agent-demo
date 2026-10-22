"""Explicit tunnel address and password protection for the demo console."""
import base64
import binascii
import secrets
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse


class ConsoleAccess:
    def __init__(self, public_url='', password=''):
        self.public_host = ''
        self.public_origin = ''
        self.password = password
        if public_url:
            try:
                url = urlsplit(public_url)
                valid = (url.scheme == 'https' and url.hostname
                         and url.username is None and url.password is None
                         and url.path in ('', '/') and not url.query and not url.fragment
                         and '*' not in url.netloc and not any(c.isspace() for c in public_url)
                         and url.port in (None, 443))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('DEMO_PUBLIC_URL must be an HTTPS origin, such as https://your-demo.example, without a path.')
            if len(password) < 16:
                raise ValueError('Public sharing requires a password of at least 16 characters.')
            self.public_host = url.hostname.lower()
            self.public_origin = 'https://' + self.public_host
        elif password:
            raise ValueError('Set DEMO_PUBLIC_URL when configuring a sharing password.')

    def check(self, request):
        host = request.headers.get('host', '').lower()
        local = host in ('127.0.0.1:8766', 'localhost:8766', 'testserver')
        public = bool(self.public_host and host == self.public_host)
        if not (local or public):
            return JSONResponse({'detail': 'This console address is not allowed.'}, status_code=403)
        # Protect local access too while sharing, including tunnels that rewrite Host.
        if self.public_host and not self.authenticated(request):
            return JSONResponse({'detail': 'Sign in to use this demo.'}, status_code=401,
                                headers={'WWW-Authenticate': 'Basic realm="Asha demo", charset="UTF-8"'})
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('origin')
            allowed = (self.public_origin,) if public else ('http://127.0.0.1:8766', 'http://localhost:8766')
            if (public and not origin) or (origin and origin not in allowed):
                return JSONResponse({'detail': 'Cross-origin writes are blocked.'}, status_code=403)
            if request.headers.get('x-demo-console') != '1':
                return JSONResponse({'detail': 'Console request header required.'}, status_code=403)
        return None

    def authenticated(self, request):
        try:
            scheme, encoded = request.headers.get('authorization', '').split(' ', 1)
            if scheme.lower() != 'basic':
                return False
            username, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
        except (ValueError, UnicodeError, binascii.Error):
            return False
        return (secrets.compare_digest(username.encode(), b'demo')
                & secrets.compare_digest(password.encode(), self.password.encode()))

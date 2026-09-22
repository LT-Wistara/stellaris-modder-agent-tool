"""Stateless Streamable HTTP MCP: JSON responses; optional SSE is not offered.

There is no authentication layer: the server is for one person on one machine.
Safety therefore comes from *where* it binds, not from a secret. Loopback is
reachable only from this machine, and binding anywhere else is refused unless the
operator passes ``allow_remote`` explicitly. Put a TLS reverse proxy in front of
that when the endpoint has to be reachable from somewhere else.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import sys
import time

SSE_KEEPALIVE_SECONDS = 15
SSE_MAX_SECONDS = 600
from urllib.parse import urlparse

from .index import Database
from .server import Server

LOOPBACK_NAMES = ('localhost', '127.0.0.1', '::1')


def is_loopback(host):
    """True when a bind address cannot be reached from other machines."""
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_http_server(database, host='127.0.0.1', port=8765, allow_remote=False):
    """Refuse to publish an unauthenticated server to the network by accident."""
    if not is_loopback(host) and not allow_remote:
        raise ValueError(
            'Refusing to bind ' + str(host) + ': there is no authentication, so this would '
            'expose the server to every machine that can reach this port. Keep the default '
            'loopback address (only this machine can connect), or pass --allow-remote when '
            'you really mean it and put a TLS reverse proxy in front.')
    loopback = is_loopback(host)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def send(self, status, payload=None):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else b''
            self.send_response(status)
            if payload is not None:
                self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if self.path != '/mcp':
                self.send(404)
                return False
            # DNS-rebinding guard: a browser page on another site must not reach
            # this local server. A request with no Origin (the normal case for a
            # local MCP client) is allowed; only a foreign Origin or Host is not.
            if loopback:
                origin = self.headers.get('Origin')
                hostname = urlparse('//' + self.headers.get('Host', '')).hostname
                foreign_origin = bool(origin) and urlparse(origin).hostname not in LOOPBACK_NAMES
                if hostname not in LOOPBACK_NAMES or foreign_origin:
                    self.send(403)
                    return False
            return True

        def do_POST(self):
            if not self.authorized():
                self.close_connection = True
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8_000_000:
                    self.close_connection = True
                    self.send(413)
                    return
                request = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                self.send(400, Server.error(None, -32700, 'Invalid JSON request'))
                return
            server = Server(database)
            # Stateless transport has no per-client state or session id.
            server.initialized = True
            response = server.dispatch(request)
            self.send(202 if response is None else 200, response)

        def do_GET(self):
            # A Streamable HTTP client that wants the optional server-to-client stream
            # announces it with `Accept: text/event-stream`; answer with a valid, already
            # closed stream instead of 405, because clients such as AstrBot report a 405
            # as a failed connection. Plain probes still get the spec's 405.
            if not self.authorized():
                return
            accept = self.headers.get('Accept') or ''
            if 'text/event-stream' not in accept:
                self.send(405)  # No optional standalone SSE stream.
                return
            # Keep the optional server-to-client stream open with comments: the official
            # MCP clients (and AstrBot's) fail the whole connection when this stream ends
            # immediately ("Connection task exited early"). Bounded so no thread lives
            # forever; a client that hangs up is noticed on the next write.
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            deadline = time.monotonic() + SSE_MAX_SECONDS
            try:
                while time.monotonic() < deadline:
                    self.wfile.write(b': keep-alive\n\n')
                    self.wfile.flush()
                    time.sleep(SSE_KEEPALIVE_SECONDS)
            except OSError:
                pass

        def do_HEAD(self):
            if self.authorized():
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', '0')
                self.end_headers()

        def do_DELETE(self):
            if self.authorized():
                self.send(405)  # No sessions to delete.

        def log_message(self, format, *args):
            print('MCP HTTP: ' + format % args, file=sys.stderr)

    return ThreadingHTTPServer((host, port), Handler)


def serve_http(database=None, host='127.0.0.1', port=8765):
    if database is None:
        # Same default as the stdio server: a hosted process reads game/mod data
        # when it can find it, and degrades to CWT-only when it cannot.
        database = Database(game_data=True)
        if not os.environ.get('STELLARIS_SKIP_WARMUP'):
            try:
                database.warm_game_data()
            except Exception as error:  # noqa: BLE001 - a cold server must still answer
                print('Game/mod data unavailable: ' + str(error), file=sys.stderr, flush=True)
    # A non-loopback bind is an explicit operator decision (STELLARIS_ALLOW_REMOTE=1);
    # without it create_http_server refuses rather than exposing an open port.
    allow_remote = os.environ.get('STELLARIS_ALLOW_REMOTE', '').strip().lower() in ('1', 'true', 'yes')
    server = create_http_server(database, host, port, allow_remote=allow_remote)
    print(f'Stellaris MCP HTTP ready: http://{host}:{server.server_port}/mcp', file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

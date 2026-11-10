from contextlib import contextmanager
from urllib.parse import urlsplit
import ssl
import threading

import pytest
import trustme
import urllib3
from werkzeug.serving import make_server, WSGIRequestHandler

from friend_circle.app import create_app
from friend_circle.config import Config, origin_of
from friend_circle.errors import FederationError
from friend_circle.network import SafeHTTP, Reply
from friend_circle.service import Federation


class MemoryMesh(SafeHTTP):
    def __init__(self):
        self.apps = {}
        self.offline = set()
        self.sent = []

    def send(self, method, url, headers=None, body=None, max_bytes=262144):
        origin = origin_of(url)
        if origin not in self.apps or origin in self.offline:
            raise FederationError("remote_unavailable", 502)
        p = urlsplit(url)
        self.sent.append((method, url, dict(headers or {})))
        with self.apps[origin].test_client() as client:
            r = client.open(
                p.path + ("?" + p.query if p.query else ""),
                method=method,
                base_url=origin,
                headers=headers,
                data=body,
            )
        if len(r.data) > max_bytes:
            raise FederationError("response_limit", 502)
        return Reply(r.status_code, r.data, dict(r.headers))


@pytest.fixture
def nodes(tmp_path):
    mesh = MemoryMesh()
    services = []
    for name in ("a", "b", "c"):
        service = Federation(Config("https://" + name + ".test", tmp_path / name), mesh)
        service.person = service.identity.create("alice")["id"]
        service.ai = service.identity.create("nova", "ai", service.person)["id"]
        mesh.apps[service.config.origin] = create_app(service)
        services.append(service)
    return (*services, mesh)


class QuietHandler(WSGIRequestHandler):
    def log(self, type, message, *args):
        pass


class LocalTLSMesh(SafeHTTP):
    """Explicit test dependency: .test names -> real local TLS sockets. No production flag."""

    def __init__(self, ca):
        self.routes = {}
        self.offline = set()
        self.context = ssl.create_default_context()
        ca.configure_trust(self.context)

    def addresses(self, url):
        origin_of(url)
        if origin_of(url) in self.offline:
            raise FederationError("remote_unavailable", 502)
        if urlsplit(url).hostname not in self.routes:
            raise FederationError("test_host_not_registered")
        return ["127.0.0.1"]

    def pool(self, ip, host):
        return urllib3.HTTPSConnectionPool(
            ip,
            port=self.routes[host],
            server_hostname=host,
            assert_hostname=host,
            ssl_context=self.context,
            retries=False,
            maxsize=1,
        )


@contextmanager
def tls_nodes(path, names=("a", "b")):
    ca = trustme.CA()
    mesh = LocalTLSMesh(ca)
    servers, services = [], []
    try:
        for name in names:
            host = name + ".test"
            service = Federation(Config("https://" + host, path / name), mesh)
            service.person = service.identity.create("alice")["id"]
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ca.issue_cert(host).configure_cert(tls)
            server = make_server(
                "127.0.0.1",
                0,
                create_app(service),
                threaded=True,
                ssl_context=tls,
                request_handler=QuietHandler,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            mesh.routes[host] = server.server_port
            servers.append((server, thread))
            services.append(service)
        yield (*services, mesh)
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

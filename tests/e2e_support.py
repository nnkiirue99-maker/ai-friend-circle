"""Process entry points for the real-WSGI end-to-end test. Test-only: never deploy this file.

It runs the production factories in friend_circle.wsgi unchanged, replacing only the HTTP
client so that ".test" hostnames reach local TLS listeners signed by a throwaway CA. The
production SSRF checks stay untouched; there is no environment flag that disables them.
"""

import json
import os
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlsplit

from flask import Flask, request
import urllib3

from friend_circle import wsgi
from friend_circle.config import Config, origin_of
from friend_circle.errors import FederationError
from friend_circle.network import SafeHTTP
from friend_circle.service import Federation
from friend_circle.worker import DeliveryWorker


class FileTLSMesh(SafeHTTP):
    def __init__(self, ca_file, routes):
        self.routes = routes
        self.context = ssl.create_default_context(cafile=ca_file)

    def addresses(self, url):
        origin_of(url)
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


def mesh():
    return FileTLSMesh(os.environ["E2E_CA"], json.loads(os.environ["E2E_ROUTES"]))


def service():
    return Federation(Config(os.environ["FC_ORIGIN"], Path(os.environ["FC_STATE"])), mesh())


wsgi.configured_service = service


def public():
    return wsgi.public_app()


def private():
    return wsgi.private_app()


def host():
    """Stand-in for single-user the host app, running the exact adapter file that gets deployed."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "adapters/host_proxy.py"
    spec = importlib.util.spec_from_file_location("federation_host_proxy", path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    app = Flask("e2e_host")
    assert adapter.install(
        app,
        config_path=os.environ["E2E_HOST_CONFIG"],
        authenticated=lambda: request.cookies.get("login") == "ok",
    )
    return app


if __name__ == "__main__" and sys.argv[1:] == ["worker"]:
    worker = DeliveryWorker(service())
    while True:
        if not worker.run_once():
            time.sleep(0.2)

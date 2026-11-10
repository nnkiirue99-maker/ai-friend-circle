"""Two nodes, each as real OS processes: gunicorn public TLS listener, gunicorn private
gateway on a Unix socket, a host app forwarding through that socket, and a delivery worker."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

import pytest
import trustme

from e2e_support import FileTLSMesh
from friend_circle.config import Config
from friend_circle.errors import FederationError
from friend_circle.gateway import UnixConnection
from friend_circle.service import Federation

pytest.importorskip("gunicorn")
UI = "https://ui.test"
SCOPE = {"visibility": "federated_friends", "audience": {"mode": "all"}}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Node:
    def __init__(self, name, root, ca, routes):
        self.host = name + ".test"
        self.origin = "https://" + self.host
        self.dir = root / name
        self.dir.mkdir()
        self.state = self.dir / "state"
        self.socket = self.dir / "gw.sock"
        self.token = self.dir / "token"
        self.token.write_text(secrets.token_urlsafe(32))
        self.token.chmod(0o600)
        cert = ca.issue_cert(self.host)
        self.cert, self.key = self.dir / "cert.pem", self.dir / "key.pem"
        cert.cert_chain_pems[0].write_to_path(self.cert)
        cert.private_key_pem.write_to_path(self.key)
        self.public_port, self.host_port = routes[self.host], free_port()

    def env(self, ca_file, routes):
        return {
            **os.environ,
            "FC_ORIGIN": self.origin,
            "FC_STATE": str(self.state),
            "FC_GATEWAY_TOKEN_FILE": str(self.token),
            "FC_UI_ORIGIN": UI,
            "E2E_CA": str(ca_file),
            "E2E_ROUTES": json.dumps(routes),
            "E2E_HOST_CONFIG": str(self.host_config),
            "FC_TRUSTED_PROXY": "1",
        }

    def call(self, method, path, body=None, login=True, origin=UI):
        conn = http.client.HTTPConnection("127.0.0.1", self.host_port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if login:
            headers["Cookie"] = "login=ok"
        if origin:
            headers["Origin"] = origin
        try:
            conn.request(
                method,
                "/api/federation" + path,
                json.dumps(body).encode() if body is not None else None,
                headers,
            )
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            conn.close()


def wait(check, what, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, http.client.HTTPException, FederationError):
            pass
        time.sleep(0.6)
    raise AssertionError("timed out waiting for " + what)


@pytest.fixture
def cluster():
    # Unix socket paths are length-limited; pytest's tmp_path is too deep on macOS.
    root = Path(tempfile.mkdtemp(prefix="rf"))
    ca = trustme.CA()
    ca_file = root / "ca.pem"
    ca.cert_pem.write_to_path(ca_file)
    routes = {"a.test": free_port(), "b.test": free_port()}
    mesh = FileTLSMesh(str(ca_file), routes)
    nodes, procs = [], []
    tests = Path(__file__).parent
    try:
        for name in ("a", "b"):
            node = Node(name, root, ca, routes)
            setup = Federation(Config(node.origin, node.state), mesh)
            node.person = setup.identity.create("alice")["id"]
            node.ai = setup.identity.create("nova", "ai", node.person)["id"]
            node.host_config = node.dir / "host.json"
            node.host_config.write_text(
                json.dumps(
                    {
                        "socket": str(node.socket),
                        "token_file": str(node.token),
                        "person": node.person,
                        "ui_origin": UI,
                    }
                )
            )
            env = node.env(ca_file, routes)
            base = [
                sys.executable,
                "-m",
                "gunicorn",
                "--chdir",
                str(tests),
                "--log-level",
                "warning",
            ]
            for extra in (
                [
                    "--bind",
                    f"127.0.0.1:{node.public_port}",
                    "--workers",
                    "2",
                    "--threads",
                    "4",
                    "--certfile",
                    str(node.cert),
                    "--keyfile",
                    str(node.key),
                    "e2e_support:public()",
                ],
                [
                    "--bind",
                    f"unix:{node.socket}",
                    "--umask",
                    "0o077",
                    "--workers",
                    "2",
                    "--threads",
                    "4",
                    "e2e_support:private()",
                ],
                ["--bind", f"127.0.0.1:{node.host_port}", "--threads", "8", "e2e_support:host()"],
            ):
                log = open(node.dir / f"proc{len(procs)}.log", "wb")
                procs.append(subprocess.Popen(base + extra, env=env, stdout=log, stderr=log))
            log = open(node.dir / "worker.log", "wb")
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(tests / "e2e_support.py"), "worker"],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
            )
            nodes.append(node)
        for node in nodes:
            wait(lambda n=node: mesh.send("GET", n.origin + "/healthz").status == 200, "public")
            wait(lambda n=node: n.call("GET", "/me")[0] == 200, "host proxy")
        yield (*nodes, mesh)
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if os.environ.get("E2E_KEEP") != "1":
            shutil.rmtree(root, ignore_errors=True)


def test_real_wsgi_unix_socket_two_node_flow(cluster):
    a, b, mesh = cluster

    # Isolation: socket is private, the host needs its own login, public has no admin API.
    assert stat.S_IMODE(a.socket.stat().st_mode) & 0o077 == 0
    assert a.call("GET", "/me", login=False)[0] == 401
    assert a.call("POST", "/invitations", {}, origin="https://evil.test")[0] == 403
    assert mesh.send("GET", a.origin + "/api/federation/me").status == 404
    conn = UnixConnection(a.socket)
    conn.request("GET", "/api/federation/me", headers={"X-FC-Local-Principal": a.person})
    assert conn.getresponse().status == 401  # socket access alone is not enough
    conn.close()
    assert a.call("GET", "/me")[1]["actor_id"] == a.person

    # Invitation -> pending request -> explicit acceptance, all through separate processes.
    status, invitation = a.call("POST", "/invitations", {})
    assert status == 201
    status, redeemed = b.call("POST", "/invitations/redeem", {"invitation": invitation["code"]})
    assert status == 202 and redeemed["status"] == "pending"
    pending = wait(
        lambda: [
            c
            for c in a.call("GET", "/contacts")[1]["items"]
            if c["status"] == "pending" and not c["initiated"]
        ],
        "request to reach A",
    )
    assert pending[0]["remote_human"] == b.person
    assert a.call("POST", "/friend-requests/accept", {"group_id": pending[0]["id"]})[0] == 202
    wait(
        lambda: [c for c in b.call("GET", "/contacts")[1]["items"] if c["status"] == "active"],
        "acceptance to reach B",
    )

    # Post -> remote feed; comment and like require B's server to call back into A's.
    status, post = a.call("POST", "/social/posts", {"text": "来自真实进程的动态", "scope": SCOPE})
    assert status == 201
    wait(
        lambda: [i for i in b.call("GET", "/social/feed")[1]["items"] if i["id"] == post["id"]],
        "post to reach B",
    )
    status, _ = b.call(
        "POST", "/social/interact", {"root": post["id"], "kind": "comment", "text": "收到啦"}
    )
    assert status == 201
    assert b.call("POST", "/social/interact", {"root": post["id"], "kind": "like"})[0] == 201
    wait(
        lambda: (
            "收到啦"
            in json.dumps(
                a.call("POST", "/social/thread/refresh", {"root": post["id"]})[1],
                ensure_ascii=False,
            )
        ),
        "comment to reach A",
    )

    # Both sides act at once: cross-server callbacks must not deadlock the WSGI workers.
    def burst(i):
        node = (a, b)[i % 2]
        if i % 3 == 0:
            return node.call("POST", "/social/thread/refresh", {"root": post["id"]})[0]
        if i % 3 == 1:
            return node.call(
                "POST",
                "/social/interact",
                {"root": post["id"], "kind": "comment", "text": f"并发 {i}"},
            )[0]
        return node.call("GET", "/social/feed")[0]

    started = time.time()
    with ThreadPoolExecutor(12) as pool:
        codes = list(pool.map(burst, range(24)))
    assert all(code in {200, 201} for code in codes), codes
    assert time.time() - started < 20
    thread = wait(
        lambda: (lambda t: t if all(f"并发 {i}" in t for i in range(24) if i % 3 == 1) else None)(
            json.dumps(
                a.call("POST", "/social/thread/refresh", {"root": post["id"], "limit": 20})[1],
                ensure_ascii=False,
            )
        ),
        "all concurrent comments to converge on A",
    )
    assert thread

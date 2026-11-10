import re
import secrets
from flask import Flask, session
from friend_circle.gateway import create_gateway, make_host_proxy
from test_relationships import connect, drain
from test_social import post


def test_private_gateway_refuses_unsigned_and_actor_spoofing(nodes, tmp_path):
    a, b, _, mesh = nodes
    path = tmp_path / "gateway-token"
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    path.chmod(0o600)
    app = create_gateway(a, token_path=path, ui_origin="https://ui.test")
    with app.test_client() as client:
        assert client.get("/api/federation/me").status_code == 401
        headers = {"Authorization": "Bearer " + token, "X-FC-Local-Principal": a.person}
        assert client.get("/api/federation/me", headers=headers).json["actor_id"] == a.person
        assert (
            client.get(
                "/api/federation/me", headers={**headers, "X-FC-Local-Principal": a.ai}
            ).status_code
            == 403
        )
        assert (
            client.post("/api/federation/invitations", headers=headers, json={}).status_code == 403
        )
        response = client.post(
            "/api/federation/invitations", headers={**headers, "Origin": "https://ui.test"}, json={}
        )
        assert response.status_code == 201
    # Dedicated credential cannot turn the public listener into a management API.
    assert mesh.send("GET", a.config.origin + "/api/federation/me", headers).status == 404


def test_proxy_uses_authenticated_mapping_not_browser_claims(nodes, tmp_path):
    a, b, _, _ = nodes
    path = tmp_path / "gateway-token"
    path.write_text(secrets.token_urlsafe(32))
    path.chmod(0o600)
    gateway = create_gateway(a, token_path=path, ui_origin="https://ui.test")
    sent = []

    class FakeConnection:
        def __init__(self, path):
            pass

        def request(self, method, url, body, headers):
            sent.append(headers)
            with gateway.test_client() as client:
                self.response = client.open(url, method=method, data=body, headers=headers)

        def getresponse(self):
            return self

        @property
        def status(self):
            return self.response.status_code

        def read(self, n):
            return self.response.data[:n]

        def getheader(self, name, default=None):
            return self.response.headers.get(name, default)

        def close(self):
            pass

    host = Flask("host")
    host.secret_key = "isolated-test-only"
    host.register_blueprint(
        make_host_proxy(
            socket_path=tmp_path / "socket",
            token_path=path,
            principal=lambda: session.get("actor"),
            ui_origin="https://ui.test",
            connection_factory=FakeConnection,
        )
    )
    with host.test_client() as client:
        assert client.get("/api/federation/me").status_code == 401
        with client.session_transaction() as state:
            state["actor"] = a.person
        response = client.get(
            "/api/federation/me",
            headers={"X-FC-Local-Principal": b.person, "Authorization": "attacker"},
        )
        assert response.json["actor_id"] == a.person
        assert sent[-1]["X-FC-Local-Principal"] == a.person
        assert (
            client.post(
                "/api/federation/invitations", json={}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )


def test_local_only_posts_and_cached_thread_read_do_not_contact_remote(nodes):
    a, b, _, mesh = nodes
    connect(a, b)
    local = a.social.publish(a.ai, a.ai, "local", {"visibility": "local"})
    assert a.social.profile(a.person, a.ai)["items"][0]["id"] == local["id"]
    root = post(a)
    drain(a, b)
    comment = b.social.interact(b.person, b.person, root["id"], "comment", "cached")
    drain(a, b)
    mesh.offline.add(b.config.origin)
    before = len(mesh.sent)
    assert comment["id"] in str(a.social.thread(a.person, root["id"]))
    assert len(mesh.sent) == before


def test_hostapp_adapter_is_inert_without_config_and_strict_with_it(tmp_path):
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "adapters/host_proxy.py"
    spec = importlib.util.spec_from_file_location("federation_host_proxy", path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    source = path.read_text()
    assert "friend_circle" not in source.split('"""', 2)[2]  # standalone: no package import

    host = Flask("host")
    host.secret_key = "isolated-test-only"
    assert adapter.install(host, config_path=str(tmp_path / "missing.json")) is False
    with host.test_client() as client:
        assert client.get("/api/federation/me").status_code == 404

    token = tmp_path / "token"
    token.write_text(secrets.token_urlsafe(32))
    token.chmod(0o644)
    config = tmp_path / "host.json"
    config.write_text(
        json.dumps(
            {
                "socket": str(tmp_path / "absent.sock"),
                "token_file": str(token),
                "person": "https://a.test/federation/actors/x",
                "ui_origin": "https://ui.test",
            }
        )
    )
    import pytest

    with pytest.raises(ValueError):  # world-readable credential is refused
        adapter.install(Flask("h2"), config_path=str(config))
    token.chmod(0o600)
    host = Flask("host3")
    host.secret_key = "isolated-test-only"
    assert adapter.install(host, config_path=str(config)) is True
    with host.test_client() as client:
        assert client.get("/api/federation/me").status_code == 401  # host login required
        with client.session_transaction() as state:
            state["unlocked"] = True
        ok = {"Origin": "https://ui.test"}
        assert client.post("/api/federation/invitations", json={}).status_code == 403
        assert client.get("/api/federation/a/../b").status_code in {400, 404}
        # Services down: a clean 502, never an exception inside the host app.
        assert client.get("/api/federation/me").status_code == 502
        assert client.post("/api/federation/invitations", json={}, headers=ok).status_code == 502


def test_bundled_web_ui_needs_login_and_serves_only_fixed_files(nodes, tmp_path):
    a, b, _, mesh = nodes
    connect(a, b)
    theirs = post(b, "来自好友")
    mine = post(a, "我的动态")
    drain(a, b)
    b.social.interact(b.person, b.person, mine["id"], "comment", "看到啦")
    drain(a, b)
    path = tmp_path / "gateway-token"
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    path.chmod(0o600)
    app = create_gateway(a, token_path=path, ui_origin="https://ui.test")
    headers = {"Authorization": "Bearer " + token, "X-FC-Local-Principal": a.person}
    with app.test_client() as client:
        assert client.get("/api/federation/ui").status_code == 401
        assert client.get("/api/federation/ui/snapshot").status_code == 401
        for name, media in (
            ("ui", "text/html"),
            ("ui/app.js", "text/javascript"),
            ("ui/sdk.js", "text/javascript"),
            ("ui/live.js", "text/javascript"),
            ("ui/styles.css", "text/css"),
            ("ui/assets/lin.svg", "image/svg+xml"),
        ):
            response = client.get("/api/federation/" + name, headers=headers)
            assert response.status_code == 200 and response.content_type.startswith(media), name
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
            assert "script-src 'self';" in response.headers["Content-Security-Policy"]
        for name in (
            "ui/secret.pem",
            "ui/..%2fapi.md",
            "ui/assets/x.png",
            "ui/assets/..%2f..%2fapi.md",
        ):
            assert client.get("/api/federation/" + name, headers=headers).status_code == 404
        live = client.get("/api/federation/ui/live.js", headers=headers).get_data(as_text=True)
        view = client.get("/api/federation/ui/snapshot", headers=headers).json
    assert "innerHTML" not in live and "eval(" not in live and "localStorage" not in live
    assert "http://" not in live and "https://" not in live  # same-origin only
    # The browser never names an acting identity in what it sends. The one exception is the
    # target of a profile edit (a human changing the avatar/cover of an AI it controls), which
    # the server authorizes as administration, never as speaking for that AI.
    start = live.index("async saveProfile(")
    end = live.index("async deleteComment(", start)
    assert "/social/profile" in live[start:end] and "/social/posts" not in live[start:end]
    outside = live[:start] + live[end:]
    assert not re.search(r"(?<![a-z_.])actor_id\s*:", outside)
    assert view["me"] == a.person and view["mode"] == "live"
    assert view["capabilities"] == {
        "public": False,
        "groups": True,
        "notifications": True,
        "maxImages": 2,
    }
    assert {b.person, b.ai} <= set(view["friends"])
    by_id = {p["id"]: p for p in view["posts"]}
    assert by_id[theirs["id"]]["text"] == "来自好友" and by_id[mine["id"]]["scope"]["mode"] == "all"
    assert [c["text"] for c in by_id[mine["id"]]["comments"]] == ["看到啦"]
    assert [p["published"] for p in view["posts"]] == sorted(
        (p["published"] for p in view["posts"]), reverse=True
    )
    assert (
        view["notifications"][0]["actor"] == b.person
        and view["notifications"][0]["post"] == mine["id"]
    )
    assert {x["id"] for x in view["actors"]} >= {a.person, a.ai, b.person, b.ai}
    assert mesh.send("GET", a.config.origin + "/api/federation/ui").status == 404  # never public

from friend_circle.onboarding import install_prompt, mcp_prompt, upgrade_prompt
from test_local_api import local_app


def test_uninstalled_browser_landing_keeps_secret_in_fragment(nodes):
    a, b, _, mesh = nodes
    inv = a.invitations.create(a.person)
    response = mesh.send("GET", inv["url"].split("#")[0])
    html = response.body.decode()
    assert response.status == 200
    assert "首次接入" in html and a.person in html
    assert inv["code"] not in html
    assert "<form" not in html and "password" not in html
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    script = mesh.send("GET", a.config.origin + "/federation/onboarding.js").body.decode()
    assert "location.hash" in script and "localStorage" not in script
    assert "innerHTML" not in script
    assert b.config.origin not in install_prompt(a.config.origin)
    for path in ("protocol.md", "api.md", "integration.md", "compatibility.md"):
        assert mesh.send("GET", a.config.origin + "/federation/docs/v1/" + path).status == 200
    assert mesh.send("GET", a.config.origin + "/federation/docs/v1/secrets.pem").status == 404
    assert mesh.send("GET", a.config.origin + "/federation/sdk/v1/client.js").status == 200
    a.invitations.revoke(a.person, inv["id"])
    assert mesh.send("GET", inv["url"].split("#")[0]).status == 404


def test_invitation_api_requires_local_login_and_stays_independent(nodes):
    a, b, _, _ = nodes
    headers = {"Origin": "https://ui.test"}
    with local_app(a).test_client() as ca, local_app(b).test_client() as cb:
        assert ca.post("/api/federation/invitations", json={}, headers=headers).status_code == 401
        for client, node in ((ca, a), (cb, b)):
            with client.session_transaction() as session:
                session["actor_id"] = node.person
        inv = ca.post("/api/federation/invitations", json={}, headers=headers).json
        preview = cb.post(
            "/api/federation/invitations/resolve", json={"invitation": inv["url"]}, headers=headers
        )
        assert preview.json["actor_id"] == a.person
        result = cb.post(
            "/api/federation/invitations/redeem", json={"invitation": inv["code"]}, headers=headers
        )
        assert result.status_code == 202 and result.json["status"] == "pending"
        assert (
            cb.post("/api/federation/invitations", json={}, headers=headers)
            .json["url"]
            .startswith(b.config.origin)
        )


def test_reference_bundle_is_self_hosting_and_verifiable(nodes, tmp_path, monkeypatch):
    import hashlib
    import importlib.util
    import io
    import json
    import shutil
    import zipfile
    from pathlib import Path

    from friend_circle import onboarding

    a, _, _, mesh = nodes
    project = Path(__file__).resolve().parents[1]
    base = a.config.origin + "/federation/sdk/v1/"

    # A node installed from the bundle has no generated assets yet: clean 404, not a crash.
    empty = tmp_path / "resources"
    empty.mkdir()
    monkeypatch.setattr(onboarding, "RESOURCES", empty)
    assert mesh.send("GET", base + "reference.zip").status == 404
    assert mesh.send("GET", base + "bundle.json").status == 404
    assert mesh.send("GET", a.config.origin + "/federation/docs/v1/integration.md").status == 200

    # Build from a copy of the sources, as a fresh install would.
    copy = tmp_path / "copy"
    for name in ("src", "tests", "adapters", "sdk", "scripts", "deploy", "native", "docs"):
        shutil.copytree(
            project / name, copy / name, ignore=shutil.ignore_patterns("__pycache__", "*.zip")
        )
    for name in ("README.md", "LICENSE", "quickstart.sh", "pyproject.toml", "uv.lock"):
        shutil.copy(project / name, copy / name)
    (copy / "src/friend_circle/resources/bundle.json").unlink(missing_ok=True)
    spec = importlib.util.spec_from_file_location("build_bundle", copy / "scripts/build_bundle.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    first = builder.build(copy)
    assert builder.build(copy)["sha256"] == first["sha256"]  # deterministic

    built = copy / "src/friend_circle/resources"
    monkeypatch.setattr(onboarding, "RESOURCES", built)
    archive = mesh.send("GET", base + "reference.zip", max_bytes=1048576)
    manifest = json.loads(mesh.send("GET", base + "bundle.json").body)
    assert archive.status == 200
    assert manifest["sha256"] == hashlib.sha256(archive.body).hexdigest() == first["sha256"]
    names = set(zipfile.ZipFile(io.BytesIO(archive.body)).namelist())
    prefix = "ai-friend-circle/"
    for required in (
        "scripts/build_bundle.py",
        "quickstart.sh",
        "LICENSE",
        "docs/pitfalls.md",
        "docs/do-not-modify.md",
        "deploy/setup.sh",
        "native/README.md",
        "native/FriendCircleKit/FederationHost.swift",
        "native/FriendCircleKit/FederationViews.swift",
        "src/friend_circle/resources/webui/app.js",
        "adapters/host_proxy.py",
        "src/friend_circle/onboarding.py",
        "src/friend_circle/resources/integration.md",
        "pyproject.toml",
        "uv.lock",
    ):
        assert prefix + required in names
    assert not any(n.endswith((".zip", ".db", ".pem", "bundle.json")) for n in names)

    # A tampered archive must not be served under a manifest that no longer matches.
    (built / "reference.zip").write_bytes(archive.body + b"x")
    assert mesh.send("GET", base + "reference.zip").status == 404


def test_invite_page_offers_one_click_for_installed_friends_and_three_steps_for_new(nodes):
    a, _, _, mesh = nodes
    inv = a.invitations.create(a.person)
    html = mesh.send("GET", inv["url"].split("#")[0]).body.decode()
    script = mesh.send("GET", a.config.origin + "/federation/onboarding.js").body.decode()
    assert 'id="open-mine"' in html and "/api/federation/ui#" in script
    assert "fetch(" not in script and "XMLHttpRequest" not in script  # the code is never sent
    prompt = install_prompt(a.config.origin)
    for needed in (
        "setup.sh --check",
        "setup.sh --doctor",
        "/api/federation/ui",
        "SHA-256",
        "host-kit",
    ):
        assert needed in prompt, needed
    assert "curl | sh" not in prompt and "| bash" not in prompt  # read before running
    assert len(prompt) < 1500
    assert mesh.send("GET", a.config.origin + "/federation/docs/v1/host-contract.md").status == 200
    # Friends who installed an older release get an in-place upgrade that keeps their identity.
    upgrade = upgrade_prompt(a.config.origin)
    assert (
        'id="upgrade"' in html and "upgrade in place" in upgrade and "/api/federation/ui" in upgrade
    )
    assert "--human" in upgrade and "不要重新创建账号" in upgrade and "| bash" not in upgrade
    # One download carries everything; the AI's MCP stays on the friend's own server.
    assert "下载完整部署包" in html and 'id="mcp"' in html and "FriendCircleKit" in html
    mcp = mcp_prompt(a.config.origin)
    assert "127.0.0.1:5092" in mcp and "mcp-entry.json" in mcp and "federation_list_friends" in mcp
    assert "不要发给任何其他站点" in mcp and len(mcp) < 1500


def test_doctor_reports_public_reachability_and_misrouting(nodes):
    from friend_circle.cli import doctor

    a, b, _, mesh = nodes
    assert all(item["ok"] for item in doctor(a)) and len(doctor(a)) == 3
    mesh.offline.add(a.config.origin)
    down = doctor(a)
    assert not any(item["ok"] for item in down) and down[0]["problem"] == "remote_unavailable"
    mesh.offline.clear()
    mesh.apps[a.config.origin], kept = mesh.apps[b.config.origin], mesh.apps[a.config.origin]
    assert not doctor(a)[0]["ok"]  # the domain answers, but it is some other server
    mesh.apps[a.config.origin] = kept

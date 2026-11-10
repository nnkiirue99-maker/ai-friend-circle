from flask import Flask, session

from friend_circle.local_api import make_blueprint


def local_app(service):
    app = Flask(__name__)
    app.secret_key = "test-only-session-secret"
    app.register_blueprint(
        make_blueprint(service, lambda: session.get("actor_id"), "https://ui.test")
    )
    return app


def test_no_anonymous_or_cross_site_admin(nodes):
    a, b, _, mesh = nodes
    app = local_app(a)
    with app.test_client() as client:
        assert client.get("/api/federation/contacts").status_code == 401
        with client.session_transaction() as sess:
            sess["actor_id"] = a.person
        payload = {"remote_actor_id": b.person}
        assert client.post("/api/federation/friend-requests", json=payload).status_code == 403
        assert (
            client.post(
                "/api/federation/friend-requests",
                json=payload,
                headers={"Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        payload["actor"] = a.ai
        assert (
            client.post(
                "/api/federation/friend-requests",
                json=payload,
                headers={"Origin": "https://ui.test"},
            ).status_code
            == 400
        )
    assert not a.store.one("SELECT 1 FROM friendship_groups")
    assert mesh.send("POST", a.config.origin + "/api/federation/friend-requests").status == 404


def test_authenticated_adapter_invites_with_companions_and_local_note(nodes):
    a, b, _, _ = nodes
    app = local_app(a)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["actor_id"] = a.person
        headers = {"Origin": "https://ui.test"}
        response = client.post(
            "/api/federation/friend-requests", json={"remote_actor_id": b.person}, headers=headers
        )
        assert response.status_code == 202
        assert (
            client.post(
                "/api/federation/contacts/note",
                headers=headers,
                json={"remote_actor_id": b.person, "note": "好朋友"},
            ).status_code
            == 200
        )
        contacts = client.get("/api/federation/contacts").json["items"]
        assert contacts[0]["note"] == "好朋友"
        assert a.ai in contacts[0]["local_ai"]
        assert (
            client.get("/api/federation/contacts").headers["Cache-Control"] == "private, no-store"
        )


def test_foreign_or_ai_session_cannot_manage_human_friends(nodes):
    a, b, _, _ = nodes
    with local_app(a).test_client() as client:
        for actor in (a.ai, b.person):
            with client.session_transaction() as sess:
                sess["actor_id"] = actor
            assert client.get("/api/federation/contacts").status_code == 403


def test_global_account_policy_api(nodes):
    a, b, _, _ = nodes
    with local_app(a).test_client() as client:
        with client.session_transaction() as sess:
            sess["actor_id"] = a.person
        headers = {"Origin": "https://ui.test"}
        response = client.put(
            "/api/federation/visibility-policy",
            headers=headers,
            json={"visible_days": 3, "expected_revision": 0},
        )
        assert response.status_code == 200 and response.json["revision"] == 1
        assert client.get("/api/federation/visibility-policy").json["visible_days"] == 3
        response = client.put(
            "/api/federation/visibility-policy",
            headers=headers,
            json={"visible_days": None, "expected_revision": 1},
        )
        assert response.status_code == 200 and response.json["visible_days"] is None
        response = client.put(
            "/api/federation/visibility-policy",
            headers=headers,
            json={"actor_id": b.person, "visible_days": 3, "expected_revision": 0},
        )
        assert response.status_code == 403


def test_contact_pagination_and_companions_are_json_arrays(nodes):
    a, b, c, _ = nodes
    a.relationships.request(a.person, b.person)
    a.relationships.request(a.person, c.person)
    with local_app(a).test_client() as client:
        with client.session_transaction() as sess:
            sess["actor_id"] = a.person
        first = client.get("/api/federation/contacts?limit=1").json
        assert len(first["items"]) == 1 and first["next_cursor"]
        second = client.get(
            "/api/federation/contacts?limit=1&before=" + str(first["next_cursor"])
        ).json
        assert len(second["items"]) == 1 and second["next_cursor"] is None
        assert first["items"][0]["id"] != second["items"][0]["id"]
        assert isinstance(first["items"][0]["local_ai"], list)

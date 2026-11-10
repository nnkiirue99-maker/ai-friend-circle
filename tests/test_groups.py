import pytest

from friend_circle.errors import FederationError
from friend_circle.service import Federation
from friend_circle.projection import snapshot
from test_local_api import local_app
from test_relationships import connect, drain
from test_social import SCOPE, post


def group_scope(*groups):
    return {
        "visibility": "federated_friends",
        "audience": {"mode": "group", "groups": [g["id"] for g in groups]},
    }


def test_private_group_crud_revision_and_validation(nodes):
    a, b, c, _ = nodes
    connect(a, b)
    g = a.groups.save(a.person, "  朋友  ", [b.person, b.ai])
    assert g["name"] == "朋友" and g["revision"] == 1
    assert a.groups.list(a.person) == [g]
    other = a.identity.create("other")["id"]
    assert a.groups.list(other) == []
    for principal in (other, a.ai):
        with pytest.raises(FederationError):
            a.groups.save(principal, "偷改", [], g["id"], 1)
        with pytest.raises(FederationError):
            a.groups.delete(principal, g["id"], 1)
    for name, members in [
        ("", []),
        ("x" * 31, []),
        ("a\nb", []),
        ("ok", [c.person]),
        ("ok", [b.person, b.person]),
        ("ok", [a.person]),
    ]:
        with pytest.raises(FederationError):
            a.groups.save(a.person, name, members)
    renamed = a.groups.save(a.person, "好友", g["members"], g["id"], 1)
    assert renamed["revision"] == 2
    for revision in (None, True, 1):
        with pytest.raises(FederationError):
            a.groups.save(a.person, "过期", [], g["id"], revision)
        with pytest.raises(FederationError):
            a.groups.delete(a.person, g["id"], revision)
    a.groups.delete(a.person, g["id"], 2)
    assert a.groups.list(a.person) == []
    assert "好友" not in str([dict(r) for r in a.store.all("SELECT body FROM outbox")])


def test_group_posts_revoke_at_source_and_clear_peer_cache_without_regrant(nodes):
    a, b, c, _ = nodes
    connect(a, b)
    connect(a, c)
    g = a.groups.save(a.person, "圈子", [b.person, b.ai])
    scope = group_scope(g)
    shared = a.social.publish(a.person, a.person, "给圈子", scope)
    drain(a, b, c)
    assert shared["id"] in str(b.social.feed(b.person))
    assert shared["id"] in str(b.social.feed(b.ai))
    with pytest.raises(FederationError):
        a.social.object(c.person, shared["id"])
    comment = b.social.interact(b.person, b.person, shared["id"], "comment", "评论")
    drain(a, b, c)
    b.social.thread(b.person, shared["id"], refresh=True)
    g = a.groups.save(a.person, "圈子", [b.ai, c.person], g["id"], g["revision"])
    # Immediate source denial for removed B; adding C never gives old posts.
    for reader in (b.person, c.person):
        with pytest.raises(FederationError):
            a.social.object(reader, shared["id"])
    with pytest.raises(FederationError):
        b.social.object(b.person, comment["id"])
    assert a.social.object(b.ai, shared["id"])
    drain(a, b, c)
    assert shared["id"] not in str(b.social.feed(b.person))
    assert not b.store.one(
        "SELECT 1 FROM object_cache WHERE reader=? AND root=?", (b.person, shared["id"])
    )
    # Re-add B, edit text, and attempt history grant: still no old access.
    g = a.groups.save(a.person, "圈子", [b.person, b.ai, c.person], g["id"], g["revision"])
    rev = a.social.object(a.person, shared["id"])["revision"]
    a.social.update(a.person, shared["id"], "修改文字", scope, rev)
    for reader in (b.person, c.person):
        with pytest.raises(FederationError):
            a.social.object(reader, shared["id"])
        with pytest.raises(FederationError):
            a.social.history_grant(a.person, a.person, reader, [shared["id"]])
    fresh = a.social.publish(a.person, a.person, "新动态", scope)
    for reader in (b.person, b.ai, c.person):
        assert a.social.object(reader, fresh["id"])
    a.groups.delete(a.person, g["id"], g["revision"])
    for obj in (shared, fresh):
        for reader in (b.person, b.ai, c.person):
            with pytest.raises(FederationError):
                a.social.object(reader, obj["id"])
    drain(a, b, c)
    assert fresh["id"] not in str(b.social.feed(b.ai))


def test_multiple_groups_overlap_exclusions_and_scope_replacement(nodes):
    a, b, c, _ = nodes
    connect(a, b)
    connect(a, c)
    first = a.groups.save(a.person, "第一组", [b.person, b.ai])
    second = a.groups.save(a.person, "第二组", [b.person, c.person])
    scope = group_scope(first, second)
    scope["audience"]["exclude"] = [b.ai]
    shared = a.social.publish(a.person, a.person, "重叠", scope)
    with pytest.raises(FederationError):
        a.social.object(b.ai, shared["id"])
    a.groups.delete(a.person, first["id"], 1)
    assert a.social.object(b.person, shared["id"])
    second = a.groups.save(a.person, "第二组", [c.person], second["id"], 1)
    with pytest.raises(FederationError):
        a.social.object(b.person, shared["id"])
    assert a.social.object(c.person, shared["id"])
    # Explicitly replacing the audience with all-friends detaches it from group changes.
    rev = a.social.object(a.person, shared["id"])["revision"]
    a.social.update(a.person, shared["id"], "改为所有好友", SCOPE, rev)
    a.groups.delete(a.person, second["id"], second["revision"])
    assert a.social.object(b.person, shared["id"])
    assert a.social.object(c.person, shared["id"])


def test_friend_removal_prunes_groups_and_readding_does_not_restore_members(nodes):
    a, b, _, _ = nodes
    connect(a, b)
    g = a.groups.save(a.person, "旧好友", [b.person, b.ai])
    shared = a.social.publish(a.person, a.person, "旧帖", group_scope(g))
    a.relationships.remove(a.person, b.person, block=True)
    drain(a, b)
    assert a.groups.list(a.person)[0]["members"] == []
    with pytest.raises(FederationError):
        a.social.object(b.person, shared["id"])
    a.relationships.unblock(a.person, b.person)
    connect(a, b)
    assert a.groups.list(a.person)[0]["members"] == []
    with pytest.raises(FederationError):
        a.social.publish(a.person, a.person, "空分组", group_scope(g))


def test_groups_local_api_and_snapshot_never_leak_groups_to_recipients(nodes):
    a, b, c, _ = nodes
    connect(a, b)
    with local_app(a).test_client() as client:
        assert client.get("/api/federation/groups").status_code == 401
        with client.session_transaction() as session:
            session["actor_id"] = a.person
        assert (
            client.post("/api/federation/groups", json={"name": "朋友", "members": []}).status_code
            == 403
        )
        headers = {"Origin": "https://ui.test"}

        def save(body):
            return client.post("/api/federation/groups", json=body, headers=headers)

        assert save({"name": "圈子", "members": [], "actor_id": a.ai}).status_code == 400
        g = save({"name": "私人组名", "members": [b.person]}).json
        listing = client.get("/api/federation/groups").json
        assert listing["items"] == [g] and set(listing["members"]) == {b.person, b.ai}
        shared = client.post(
            "/api/federation/social/posts",
            json={"text": "分组发布", "scope": group_scope(g)},
            headers=headers,
        )
        assert shared.status_code == 201
        view = client.get("/api/federation/ui/snapshot").json
        assert view["capabilities"]["groups"] and view["groups"] == [g]
        assert next(p for p in view["posts"] if p["id"] == shared.json["id"])["scope"] == {
            "mode": "group",
            "ids": [g["id"]],
        }
        assert "私人组名" not in str(a.social.object(b.person, shared.json["id"]))
        for invalid in [
            group_scope({"id": "missing"}),
            {"visibility": "self", "audience": {"mode": "group", "groups": [g["id"]]}},
            group_scope(),
            {
                "visibility": "federated_friends",
                "audience": {"mode": "group", "groups": [g["id"], g["id"]]},
            },
        ]:
            assert client.post(
                "/api/federation/social/posts",
                json={"text": "x", "scope": invalid},
                headers=headers,
            ).status_code in (400, 404)
        result = client.post(
            "/api/federation/groups/delete",
            json={"id": g["id"], "expected_revision": 1},
            headers=headers,
        )
        assert result.status_code == 200
        assert client.get("/api/federation/groups").json["items"] == []
    assert snapshot(b, b.person)["groups"] == []


def test_v5_migration_keeps_identity_friendship_posts_and_credentials(nodes):
    from friend_circle.credentials import Credentials

    a, b, _, _ = nodes
    connect(a, b)
    shared = post(a, "迁移前")
    token = Credentials(a.store).issue(a.ai)
    with a.store.connect(write=True) as db:
        for table in ("post_groups", "post_scopes", "audience_group_members", "audience_groups"):
            db.execute("DROP TABLE " + table)
        db.execute("UPDATE metadata SET value='5' WHERE key='schema'")
    restored = Federation(a.config, a.http)
    assert restored.store.one("SELECT value FROM metadata WHERE key='schema'")[0] == "6"
    assert restored.social.object(b.person, shared["id"])["content"]["text"] == "迁移前"
    assert restored.relationships.is_friend(a.person, b.person)
    assert restored.identity.private_key(a.ai)
    assert restored.store.one("SELECT id FROM mcp_tokens WHERE id=?", (token["id"],))
    assert restored.groups.list(a.person) == []

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from friend_circle.errors import FederationError
from friend_circle.migrations import LATEST
from friend_circle.invitations import parse
from friend_circle.service import Federation
from test_relationships import drain
from test_transport import send_event
from conftest import tls_nodes


def invited(a, b):
    invitation = a.invitations.create(a.person)
    result = b.invitations.redeem(b.person, invitation["url"])
    drain(a, b)
    assert not b.relationships.is_friend(b.person, a.person)
    a.relationships.accept(a.person, result["group_id"])
    drain(a, b)
    assert a.relationships.is_friend(a.person, b.person)
    return invitation, result


def test_a_invites_b_b_invites_c_and_reverse_has_no_duplicate(nodes):
    a, b, c, _ = nodes
    invitation, result = invited(a, b)
    assert a.relationships.is_friend(a.ai, b.ai)
    assert parse(invitation["url"]) == parse(invitation["code"])
    reverse = b.invitations.create(b.person)
    assert a.invitations.redeem(a.person, reverse["code"])["status"] == "active"
    assert b.store.one("SELECT count(*) FROM friendship_groups")[0] == 1
    invited(b, c)
    assert b.relationships.is_friend(b.ai, c.ai)
    assert not a.relationships.is_friend(a.person, c.person)
    a.invitations.revoke(a.person, invitation["id"])
    assert a.relationships.is_friend(a.person, b.person)
    assert b.invitations.redeem(b.person, invitation["code"])["status"] == "active"


def test_preview_does_not_use_and_duplicate_request_uses_once(nodes):
    a, b, _, _ = nodes
    inv = a.invitations.create(a.person, max_uses=1)
    assert b.invitations.resolve(b.person, inv["code"])["requires_confirmation"]
    assert a.store.one("SELECT uses FROM invitation_tokens")[0] == 0
    r1 = b.invitations.redeem(b.person, inv["code"])
    r2 = b.invitations.redeem(b.person, inv["code"])
    assert r1 == r2
    drain(a, b)
    assert a.store.one("SELECT uses FROM invitation_tokens")[0] == 1
    assert b.invitations.redeem(b.person, inv["code"]) == r1


def test_expiry_revocation_and_origin_binding(nodes):
    a, b, _, _ = nodes
    inv = a.invitations.create(a.person)
    payload = parse(inv["code"])
    assert payload["token"] not in str(a.invitations.list(a.person))
    assert payload["token"] not in a.store.one("SELECT token_hash FROM invitation_tokens")[0]
    with pytest.raises(FederationError):
        parse(inv["url"].replace("https://a.test/invite", "https://b.test/invite", 1))
    with a.store.connect(write=True) as db:
        db.execute("UPDATE invitation_tokens SET expires=?", (time.time() - 1,))
    with pytest.raises(FederationError):
        b.invitations.resolve(b.person, inv["code"])
    inv = a.invitations.create(a.person)
    a.invitations.revoke(a.person, inv["id"])
    with pytest.raises(FederationError):
        b.invitations.redeem(b.person, inv["code"])


def test_atomic_max_use_and_failed_follow_rolls_back(nodes):
    a, b, c, _ = nodes
    inv = a.invitations.create(a.person, max_uses=1)
    b_result = b.invitations.redeem(b.person, inv["code"])
    c_result = c.invitations.redeem(c.person, inv["code"])
    from friend_circle.store import decode

    events = [
        (
            node,
            decode(node.store.one("SELECT body FROM outbox WHERE id=?", (result["group_id"],))[0]),
        )
        for node, result in ((b, b_result), (c, c_result))
    ]
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda pair: send_event(pair[0], a, pair[1])[0].status, events))
    assert sorted(replies) == [202, 404]
    assert a.store.one("SELECT uses FROM invitation_tokens")[0] == 1
    assert a.store.one("SELECT count(*) FROM friendship_groups")[0] == 1


def test_migration_preserves_actor_notes_and_relationships(nodes):
    a, b, _, _ = nodes
    invited(a, b)
    a.relationships.note(a.person, b.person, "朋友")
    # Recreate an actual v2 layout, retaining all pre-extension tables.
    with a.store.connect(write=True) as db:
        for table in (
            "post_groups",
            "post_scopes",
            "audience_group_members",
            "audience_groups",
            "actor_profiles",
            "object_audience",
            "social_objects",
            "object_cache",
            "profile_cache",
            "social_references",
            "cache_tombstones",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("DROP TABLE mcp_tokens")
        db.execute("DROP TABLE invitation_uses")
        db.execute("DROP TABLE invitation_tokens")
        db.execute("UPDATE metadata SET value='2' WHERE key='schema'")
    restored = Federation(a.config, a.http)
    assert restored.relationships.is_friend(a.person, b.person)
    assert restored.relationships.contacts(a.person)[0]["note"] == "朋友"
    assert restored.identity.private_key(a.person)
    assert restored.store.one("SELECT value FROM metadata WHERE key='schema'")[0] == str(LATEST)


def test_real_tls_bidirectional_invitation(tmp_path):
    with tls_nodes(tmp_path) as (a, b, _):
        invited(a, b)
        reverse = b.invitations.create(b.person)
        assert a.invitations.redeem(a.person, reverse["code"])["status"] == "active"


def test_delete_and_clear_invitation_records_keep_friendships(nodes):
    a, b, c, _ = nodes
    used, _ = invited(a, b)  # used once; friendship made through it
    revoked = a.invitations.create(a.person)
    a.invitations.revoke(a.person, revoked["id"])
    with a.store.connect(write=True) as db:
        db.execute("UPDATE invitation_tokens SET max_uses=1 WHERE id=?", (used["id"],))
    live = a.invitations.create(a.person)
    other = a.invitations.create(a.person)
    # Clearing removes revoked, expired and used-up records only.
    assert a.invitations.clear_inactive(a.person) == 2
    assert {r["id"] for r in a.invitations.list(a.person)} == {live["id"], other["id"]}
    assert a.relationships.is_friend(a.person, b.person)  # unaffected
    # Deleting a still-valid invitation makes its code stop working at once.
    a.invitations.delete(a.person, live["id"])
    assert [r["id"] for r in a.invitations.list(a.person)] == [other["id"]]
    with pytest.raises(FederationError):
        c.invitations.redeem(c.person, live["code"])
    # Only the owner can delete, and only existing records.
    with pytest.raises(FederationError):
        b.invitations.delete(b.person, other["id"])
    with pytest.raises(FederationError):
        a.invitations.delete(a.person, live["id"])

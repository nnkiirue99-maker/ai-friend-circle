import json

import pytest

from friend_circle.errors import FederationError
from friend_circle.store import decode
from friend_circle.worker import DeliveryWorker

from test_transport import send_event
from conftest import tls_nodes


def drain(*nodes):
    for _ in range(200):
        worked = False
        for node in nodes:
            worked |= DeliveryWorker(node).run_once()
        if not worked:
            return
    raise AssertionError("unbounded membership loop")


def connect(a, b, include_ai=True):
    group = a.relationships.request(a.person, b.person, include_ai=include_ai)
    drain(a, b)
    b.relationships.accept(b.person, group, include_ai=include_ai)
    drain(a, b)
    return group


def test_human_confirmation_then_companion_graph(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person)
    drain(a, b)
    assert not a.relationships.is_friend(a.person, b.person)
    assert not b.relationships.is_friend(b.person, a.person)
    assert b.store.one("SELECT count(*) FROM follows")[0] == 1
    b.relationships.accept(b.person, group)
    drain(a, b)
    for local in [a.person, a.ai]:
        for remote in [b.person, b.ai]:
            assert a.relationships.is_friend(local, remote)
            assert b.relationships.is_friend(remote, local)
    assert a.store.one("SELECT count(*) FROM follows")[0] == 8
    assert b.store.one("SELECT count(*) FROM follows")[0] == 8
    assert not a.store.one(
        "SELECT 1 FROM deliveries WHERE status NOT IN ('acknowledged','cancelled')"
    )
    # AI membership is not a posting/reading grant. There is no AI model/tool execution path.
    assert a.capabilities()["ai_automation_enabled"] is False


def test_local_notes_never_leave_server(nodes):
    a, b, _, mesh = nodes
    a.relationships.note(a.person, b.person, "月亮家的朋友")
    connect(a, b)
    assert a.relationships.contacts(a.person)[0]["note"] == "月亮家的朋友"
    assert b.relationships.contacts(b.person)[0]["note"] == ""
    assert not a.store.one("SELECT 1 FROM outbox WHERE body LIKE '%月亮%'")
    assert not b.store.one("SELECT 1 FROM inbox WHERE body LIKE '%月亮%'")
    assert "月亮" not in json.dumps(mesh.get_json(a.person), ensure_ascii=False)


def test_remove_and_block_cascade_to_ai(nodes):
    a, b, _, _ = nodes
    group = connect(a, b)
    a.relationships.remove(a.person, b.person, block=True)
    for local in [a.person, a.ai]:
        for remote in [b.person, b.ai]:
            assert not a.relationships.is_friend(local, remote)
    drain(a, b)
    for local in [a.person, a.ai]:
        for remote in [b.person, b.ai]:
            assert not b.relationships.is_friend(remote, local)
    assert (
        b.store.one("SELECT status FROM friendship_groups WHERE id=?", (group,))[0] == "cancelled"
    )
    a.relationships.unblock(a.person, b.person)
    assert not a.relationships.is_friend(a.ai, b.ai)
    assert a.relationships.request(a.person, b.person) != group


def test_no_ai_join_before_owner_accepts_or_if_excluded(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person, include_ai=False)
    drain(a, b)
    b.relationships.accept(b.person, group, include_ai=False)
    drain(a, b)
    assert a.relationships.is_friend(a.person, b.person)
    assert not a.relationships.is_friend(a.ai, b.ai)
    assert a.store.one("SELECT count(*) FROM follows")[0] == 2


def test_reject_is_terminal_and_idempotent(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person)
    drain(a, b)
    b.relationships.reject(b.person, group)
    b.relationships.reject(b.person, group)
    drain(a, b)
    assert a.store.one("SELECT status FROM friendship_groups")[0] == "rejected"
    assert not a.relationships.is_friend(a.person, b.person)
    with pytest.raises(FederationError):
        b.relationships.accept(b.person, group)


def test_late_accept_cannot_restore_cancelled_group(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person)
    drain(a, b)
    b.relationships.accept(b.person, group)
    accept = decode(b.store.one('SELECT body FROM outbox WHERE body LIKE \'%"type":"Accept"%\'')[0])
    a.relationships.remove(a.person, b.person)
    r, _ = send_event(b, a, accept)
    assert r.status == 202
    drain(a, b)
    assert not a.relationships.is_friend(a.person, b.person)
    assert not b.relationships.is_friend(b.ai, a.ai)


def test_reverse_follow_arrives_before_accept(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person)
    drain(a, b)
    b.relationships.accept(b.person, group)
    reverse = decode(
        b.store.one('SELECT body FROM outbox WHERE body LIKE \'%"type":"Follow"%\'')[0]
    )
    assert send_event(b, a, reverse)[0].status == 202
    assert not a.relationships.is_friend(a.person, b.person)
    drain(a, b)
    assert a.relationships.is_friend(a.ai, b.ai)


def test_revocation_arrives_before_original_invitation(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person)
    follow = decode(a.store.one("SELECT body FROM outbox WHERE id=?", (group,))[0])
    a.relationships.remove(a.person, b.person)
    revoke = decode(a.store.one("SELECT body FROM outbox WHERE body LIKE '%rb:RevokeGrant%'")[0])
    assert send_event(a, b, revoke)[0].status == 202
    assert send_event(a, b, follow)[0].status == 202
    assert b.store.one("SELECT status FROM friendship_groups")[0] == "cancelled"
    with pytest.raises(FederationError):
        b.relationships.accept(b.person, group)


def test_forged_accept_and_uninvited_ai_rejected(nodes):
    a, b, c, _ = nodes
    group = connect(a, b)
    fake = c.event(c.person, a.person, "Accept", object=group, **{"rb:group": group})
    assert send_event(c, a, fake)[0].status == 403
    stranger_ai = b.identity.create("stranger", "ai", b.person)["id"]
    fake = b.event(stranger_ai, a.person, "Follow", object=a.person, **{"rb:group": group})
    assert send_event(b, a, fake, signer=stranger_ai)[0].status == 403


def test_many_servers_and_simultaneous_requests(nodes):
    a, b, c, _ = nodes
    g1 = a.relationships.request(a.person, b.person)
    g2 = b.relationships.request(b.person, a.person)
    g3 = a.relationships.request(a.person, c.person)
    assert g1 == a.relationships.request(a.person, b.person)
    drain(a, b, c)
    b.relationships.accept(b.person, g1)
    a.relationships.accept(a.person, g2)
    c.relationships.accept(c.person, g3)
    drain(a, b, c)
    assert a.relationships.is_friend(a.ai, b.ai)
    assert a.relationships.is_friend(a.ai, c.ai)
    a.relationships.remove(a.person, b.person)
    drain(a, b, c)
    assert not a.relationships.is_friend(a.ai, b.ai)
    assert a.relationships.is_friend(a.ai, c.ai)


def test_human_cannot_accept_others_invitation(nodes):
    a, b, _, _ = nodes
    group = a.relationships.request(a.person, b.person)
    drain(a, b)
    other = b.identity.create("someone")["id"]
    with pytest.raises(FederationError):
        b.relationships.accept(other, group)


def test_real_https_friend_and_companion_handshake(tmp_path):
    with tls_nodes(tmp_path) as (a, b, _):
        a.ai = a.identity.create("nova", "ai", a.person)["id"]
        b.ai = b.identity.create("companion", "ai", b.person)["id"]
        group = connect(a, b)
        assert a.relationships.is_friend(a.ai, b.ai)
        a.relationships.note(a.person, b.person, "朋友")
        assert a.relationships.contacts(a.person)[0]["note"] == "朋友"
        b.relationships.remove(b.person, a.person)
        drain(a, b)
        assert not a.relationships.is_friend(a.ai, b.ai)
        assert (
            a.store.one("SELECT status FROM friendship_groups WHERE id=?", (group,))[0]
            == "cancelled"
        )


def _all_linked(a, b, a_members, b_members):
    return all(
        a.relationships.is_friend(x, y) and b.relationships.is_friend(y, x)
        for x in a_members
        for y in b_members
    )


def test_ais_follow_their_humans_friendships_even_when_added_later(nodes):
    a, b, _, _ = nodes
    # An older client accepted without AIs: the humans are friends, the AIs are not.
    connect(a, b, include_ai=False)
    assert not a.relationships.is_friend(a.ai, b.person)
    a.relationships.sync_companions(force=True)
    drain(a, b)
    b.relationships.sync_companions(force=True)
    drain(a, b)
    assert _all_linked(a, b, [a.person, a.ai], [b.person, b.ai])
    # Nothing left to do: a normal run sends nothing.
    assert a.relationships.sync_companions() == 0 and b.relationships.sync_companions() == 0
    # An AI created after the friendship joins it automatically.
    late = a.identity.create("orion", "ai", a.person)["id"]
    assert a.relationships.sync_companions() == 1
    drain(a, b)
    assert _all_linked(a, b, [a.person, a.ai, late], [b.person, b.ai])


def test_ai_follow_rejected_by_old_peer_is_resent_after_it_learns_the_ais(nodes, monkeypatch):
    a, b, _, _ = nodes
    connect(a, b, include_ai=False)
    # b cannot hear about a's AIs yet, but a already links them: their Follows are refused.
    monkeypatch.setattr(b.relationships, "_peer_supports_companions", lambda remote: False)
    group = a.store.one("SELECT * FROM friendship_groups")
    with a.store.connect(write=True) as db:
        a.relationships._set_members(db, group, "local_ai", [a.ai])
    drain(a, b)
    assert not a.relationships.is_friend(a.ai, b.person)
    a.relationships.sync_companions(force=True)
    drain(a, b)
    assert _all_linked(a, b, [a.person, a.ai], [b.person])


def test_companion_sync_skips_peers_that_do_not_understand_it(nodes, monkeypatch):
    a, b, _, _ = nodes
    connect(a, b, include_ai=False)
    monkeypatch.setattr(a.relationships, "_peer_supports_companions", lambda remote: False)
    before = a.store.one("SELECT count(*) FROM outbox")[0]
    assert a.relationships.sync_companions(force=True) == 0
    assert a.store.one("SELECT count(*) FROM outbox")[0] == before
    assert a.store.one("SELECT local_ai FROM friendship_groups")[0] == "[]"


def test_companion_sync_requires_both_follow_directions(nodes):
    a, b, _, _ = nodes
    connect(a, b)
    group = a.store.one("SELECT * FROM friendship_groups")
    assert a.relationships._linked(group)
    with a.store.connect(write=True) as db:
        db.execute(
            "UPDATE follows SET status='pending' WHERE actor=? AND target=?", (b.person, a.ai)
        )
    assert not a.relationships._linked(group)
    assert a.relationships.sync_companions(force=True) == 1
    drain(a, b)
    assert a.relationships._linked(group)


@pytest.mark.parametrize("block", [False, True])
def test_removing_from_ai_card_removes_its_human_and_all_their_ais(nodes, block):
    from friend_circle import projection

    a, b, _, _ = nodes
    group = connect(a, b)
    assert {b.person, b.ai} <= set(projection.snapshot(a, a.person)["friends"])
    a.relationships.remove(a.person, b.ai, block=block)
    drain(a, b)
    assert not {b.person, b.ai} & set(projection.snapshot(a, a.person)["friends"])
    assert not {a.person, a.ai} & set(projection.snapshot(b, b.person)["friends"])
    assert a.store.one("SELECT status FROM friendship_groups WHERE id=?", (group,))[0] == "cancelled"
    blocked = a.store.all("SELECT remote FROM contact_blocks WHERE owner=?", (a.person,))
    assert [r[0] for r in blocked] == ([b.person] if block else [])
    if block:
        a.relationships.unblock(a.person, b.ai)
        assert not a.store.one("SELECT 1 FROM contact_blocks")

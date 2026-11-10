from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from friend_circle.config import Config, MAX_BODY
from friend_circle.errors import FederationError
from friend_circle.service import Federation
from friend_circle.signatures import sign
from friend_circle.store import encode
from friend_circle.worker import DeliveryWorker

from conftest import tls_nodes


def send_event(sender, receiver, event, *, url=None, signer=None, **kwargs):
    url = url or receiver.config.origin + "/federation/inbox"
    msg = sign(
        sender.identity, signer or sender.person, "POST", url, encode(event).encode(), **kwargs
    )
    return sender.http.send("POST", url, dict(msg.headers), msg.body), msg


def test_identity_and_discovery(nodes):
    a, b, c, mesh = nodes
    found = a.identity.discover("@alice@b.test")
    assert found["id"] == b.person
    assert found["id"] != a.person != c.person
    ai = a.identity.discover("@nova@b.test")
    assert ai["type"] == "Service" and ai["rb:kind"] == "ai"
    assert "controller" not in ai  # ownership is local, not accidentally public
    assert b.person not in json.dumps(ai)
    caps = mesh.get_json("https://a.test/federation/capabilities")
    assert caps["private_social_enabled"] is True
    assert caps["server_actor"] != a.person
    assert "PRIVATE KEY" not in json.dumps(found)


def test_ai_requires_local_human_controller(nodes):
    a, b, _, _ = nodes
    for owner in (None, a.ai, b.person):
        with pytest.raises(FederationError, match="controller"):
            a.identity.create("invalid", "ai", owner)


def test_private_key_permissions_and_stable_restart(nodes):
    a, _, _, mesh = nodes
    private = next(a.identity.key_dir.glob("*.pem"))
    assert private.stat().st_mode & 0o077 == 0
    restarted = Federation(a.config, mesh)
    assert restarted.identity.local_document(a.person) == a.identity.local_document(a.person)
    assert restarted.identity.private_key(a.person).public_key() is not None
    with pytest.raises(ValueError, match="different origin"):
        Federation(Config("https://different.test", a.config.state_dir), mesh)


def test_signed_probe_delivery_and_idempotence(nodes):
    a, b, _, _ = nodes
    event = a.probe(a.person, b.person)
    assert DeliveryWorker(a).run_once()
    assert a.store.one("SELECT status FROM deliveries")[0] == "acknowledged"
    assert b.store.one("SELECT count(*) FROM inbox")[0] == 1
    response, msg = send_event(a, b, event)
    assert response.status == 202 and response.json()["duplicate"]
    replay = a.http.send("POST", msg.url, dict(msg.headers), msg.body)
    assert replay.status == 409 and replay.json()["error"] == "replayed_signature"
    assert b.store.one("SELECT count(*) FROM inbox")[0] == 1


def test_replay_survives_restart(nodes):
    a, b, _, _ = nodes
    event = a.probe(a.person, b.person)
    _, msg = send_event(a, b, event)
    restarted = Federation(b.config, b.http)
    with pytest.raises(FederationError, match="replayed_signature"):
        restarted.receive(msg)


@pytest.mark.parametrize("mutation", ["body", "actor", "target", "profile", "content_type"])
def test_tampered_signature_rejected(nodes, mutation):
    a, b, c, mesh = nodes
    event = a.probe(a.person, b.person)
    url = b.config.origin + "/federation/inbox"
    msg = sign(a.identity, a.person, "POST", url, encode(event).encode())
    if mutation == "body":
        msg.body += b" "
    elif mutation == "actor":
        msg.headers["X-Rue-Actor"] = c.person
    elif mutation == "target":
        url = b.person + "/inbox"
    elif mutation == "profile":
        msg.headers["X-Rue-Profile"] = "other/1"
    else:
        msg.headers["Content-Type"] = "application/json"
    response = mesh.send("POST", url, dict(msg.headers), msg.body)
    assert response.status == 401
    assert b.store.one("SELECT count(*) FROM inbox")[0] == 0


def test_signed_actor_forgery_and_local_impersonation(nodes):
    a, b, c, _ = nodes
    event = a.probe(a.person, b.person)
    event["actor"] = c.person
    r, _ = send_event(a, b, event)
    assert r.status == 403
    event["actor"] = b.person
    r, _ = send_event(a, b, event)
    assert r.status == 403


def test_expired_signature_and_activity(nodes):
    a, b, _, _ = nodes
    event = a.probe(a.person, b.person)
    r, _ = send_event(a, b, event, created=datetime.now(timezone.utc) - timedelta(minutes=10))
    assert r.status == 401
    event["published"] = "2000-01-01T00:00:00+00:00"
    r, _ = send_event(a, b, event)
    assert r.status == 400


def test_conflicting_event_id(nodes):
    a, b, _, _ = nodes
    event = a.probe(a.person, b.person)
    assert send_event(a, b, event)[0].status == 202
    event["published"] = datetime.now(timezone.utc).isoformat()
    assert send_event(a, b, event)[0].status == 409


def test_no_unknown_or_nonlocal_recipients(nodes):
    a, b, c, _ = nodes
    event = a.probe(a.person, b.person)
    event["to"] = [c.person]
    assert send_event(a, b, event)[0].status == 403
    event["to"] = []
    assert send_event(a, b, event)[0].status == 403


def test_unsupported_content_fails_closed(nodes):
    a, b, _, _ = nodes
    event = a.probe(a.person, b.person)
    event["type"] = "Create"
    event["object"] = {"content": "must not be accepted before ACL phase"}
    assert send_event(a, b, event)[0].status == 422
    assert not b.store.one("SELECT 1 FROM inbox")


def test_outbox_requires_signature_and_recipient_filter(nodes):
    a, b, c, mesh = nodes
    event = a.probe(a.person, b.person)
    url = a.person + "/outbox?limit=1"
    assert mesh.send("GET", url).status >= 400
    msg = sign(b.identity, b.person, "GET", url)
    r = mesh.send("GET", url, dict(msg.headers))
    assert r.status == 200 and r.json()["orderedItems"][0]["id"] == event["id"]
    msg = sign(c.identity, c.person, "GET", url)
    assert mesh.send("GET", url, dict(msg.headers)).json()["orderedItems"] == []


def test_queue_retry_restart_and_recovery(nodes):
    a, b, _, mesh = nodes
    a.probe(a.person, b.person)
    mesh.offline.add(b.config.origin)
    assert DeliveryWorker(a).run_once()
    row = a.store.one("SELECT * FROM deliveries")
    assert row["status"] == "retry" and row["attempts"] == 1 and row["next_at"] > time.time()
    a2 = Federation(a.config, mesh)
    with a.store.connect(write=True) as db:
        db.execute("UPDATE deliveries SET next_at=0")
    mesh.offline.clear()
    assert DeliveryWorker(a2).run_once()
    assert a.store.one("SELECT status,attempts FROM deliveries")[:] == ("acknowledged", 2)


def test_leases_atomic_and_old_worker_cannot_finish(nodes):
    a, b, _, _ = nodes
    a.probe(a.person, b.person)
    w = DeliveryWorker(a)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda _: DeliveryWorker(a).claim(), range(8)))
    jobs = [j for j in claims if j]
    assert len(jobs) == 1
    with a.store.connect(write=True) as db:
        db.execute("UPDATE deliveries SET lease_until=0")
    recovered = w.claim()
    assert recovered["lease_token"] != jobs[0]["lease_token"]
    assert w.finish(jobs[0], "acknowledged") is False
    assert w.finish(recovered, "acknowledged") is True


def test_faulty_peer_does_not_block_other_peer(nodes):
    a, b, c, mesh = nodes
    a.probe(a.person, b.person)
    a.probe(a.person, c.person)
    mesh.offline.add(b.config.origin)
    w = DeliveryWorker(a)
    assert w.run_once() and w.run_once()
    rows = a.store.all("SELECT recipient,status FROM deliveries ORDER BY id")
    assert [(r[0], r[1]) for r in rows] == [(b.person, "retry"), (c.person, "acknowledged")]


def test_dead_letter_and_block_cancellation(nodes):
    a, b, c, mesh = nodes
    a.probe(a.person, b.person)
    mesh.offline.add(b.config.origin)
    with a.store.connect(write=True) as db:
        db.execute("UPDATE deliveries SET attempts=7")
    assert DeliveryWorker(a).run_once()
    assert a.store.one("SELECT status FROM deliveries")[0] == "dead"
    a.probe(a.person, c.person)
    with a.store.connect(write=True) as db:
        db.execute("INSERT INTO blocks VALUES ('origin',?)", (c.config.origin,))
    assert DeliveryWorker(a).run_once()
    assert (
        a.store.one("SELECT status FROM deliveries WHERE recipient=?", (c.person,))[0]
        == "cancelled"
    )


def test_nonce_uniqueness_under_race(nodes):
    a, _, _, _ = nodes

    def insert(_):
        try:
            a.store.consume_nonce("key", "nonce", time.time() + 300)
            return True
        except FederationError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(insert, range(8))) == 1


def test_body_limits_and_bad_json(nodes):
    a, b, _, mesh = nodes
    url = b.config.origin + "/federation/inbox"
    assert mesh.send("POST", url, body=b"x" * (MAX_BODY + 1)).status == 413
    event = a.probe(a.person, b.person)
    raw = encode(event)[:-1] + ',"actor":"duplicate"}'
    msg = sign(a.identity, a.person, "POST", url, raw.encode())
    assert mesh.send("POST", url, dict(msg.headers), msg.body).status == 400


def test_rate_limits_persist(nodes):
    a, _, _, mesh = nodes
    a.store.rate_limit("test", 1)
    with pytest.raises(FederationError, match="rate_limited"):
        Federation(a.config, mesh).store.rate_limit("test", 1)


def test_two_real_https_servers(tmp_path):
    with tls_nodes(tmp_path) as (a, b, mesh):
        remote = a.identity.discover("@alice@b.test")
        assert remote["id"] == b.person
        a.probe(a.person, b.person)
        b.probe(b.person, a.person)
        assert DeliveryWorker(a).run_once() and DeliveryWorker(b).run_once()
        assert a.store.one("SELECT count(*) FROM inbox")[0] == 1
        assert b.store.one("SELECT count(*) FROM inbox")[0] == 1
        assert a.store.one("SELECT status FROM deliveries")[0] == "acknowledged"
        assert b.store.one("SELECT status FROM deliveries")[0] == "acknowledged"


def test_refuse_unrelated_state_directory(tmp_path):
    unrelated = tmp_path / "existing-app"
    unrelated.mkdir()
    original = unrelated / "keep.txt"
    original.write_text("untouched")
    with pytest.raises(ValueError, match="dedicated empty"):
        Federation(Config("https://a.test", unrelated))
    assert original.read_text() == "untouched"
    assert not (unrelated / "federation.sqlite3").exists()

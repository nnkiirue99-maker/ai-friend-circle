"""Confirmed two-way Follow graph, grouped human/AI invitations and local-only notes.

Every companion signs its own Follow/Accept. Human consent selects the group, not a
blanket right to impersonate its AI. No automated posting grant is created here.
All state transitions and resulting outbox entries share the caller's transaction.
"""

import time

from .config import origin_of
from .errors import FederationError
from .store import encode, decode

TYPES = {"Follow", "Accept", "Reject", "Undo", "rb:RevokeGrant", "rb:Companions"}
# A human's AIs follow that human's friendships. rb:Companions tells the other side which
# AIs currently belong to an existing friendship, so AIs added later are linked too.
COMPANIONS_CAPABILITY = "companions/1"
COMPANIONS_RETRY_SECONDS = 600


def array(raw):
    # Use the same bounded parser as wire objects.
    return decode('{"items":' + raw + "}")["items"]


class Relationships:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def _human(self, actor):
        row = self.store.one("SELECT kind FROM actors WHERE id=?", (actor,))
        if not row or row[0] != "person":
            raise FederationError("local_human_required", 403)

    def _companions(self, human, include_ai):
        if not include_ai:
            return []
        rows = self.store.all(
            "SELECT id FROM actors WHERE controller=? AND kind='ai' ORDER BY id", (human,)
        )
        if len(rows) > 8:
            raise FederationError("select_at_most_eight_companions")
        return [r[0] for r in rows]

    def _blocked(self, db, local, remote):
        return bool(
            db.execute(
                "SELECT 1 FROM contact_blocks WHERE owner=? AND remote=?", (local, remote)
            ).fetchone()
        )

    def note(self, owner, remote, note):
        self._human(owner)
        origin_of(remote)
        if not isinstance(note, str) or len(note) > 128 or any(ord(c) < 32 for c in note):
            raise FederationError("invalid_contact_note")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO contact_notes VALUES (?,?,?) ON CONFLICT(owner,remote) "
                "DO UPDATE SET note=excluded.note",
                (owner, remote, note),
            )

    def request(self, local, remote, *, include_ai=True, invitation=None):
        self._human(local)
        doc = self.service.identity.remote(remote)
        if doc["type"] != "Person":
            raise FederationError("invite_human_and_companions")
        companions = self._companions(local, include_ai)
        event = self.service.event(local, remote, "Follow", object=remote)
        event.update({"rb:group": event["id"], "rb:companions": companions})
        if invitation is not None:
            event["rb:invite"] = invitation
        with self.store.connect(write=True) as db:
            if self._blocked(db, local, remote):
                raise FederationError("contact_blocked", 403)
            existing = db.execute(
                "SELECT id FROM friendship_groups WHERE local_human=? "
                "AND remote_human=? AND status IN ('pending','active') "
                "AND (initiated=1 OR status='active')",
                (local, remote),
            ).fetchone()
            if existing:
                return existing[0]
            db.execute(
                "INSERT INTO friendship_groups VALUES (?,?,?,?,?,'pending',1,?)",
                (event["id"], local, remote, encode(companions), "[]", time.time()),
            )
            self._record_follow(db, event, "out")
            self.service.enqueue(db, event, remote)
        return event["id"]

    def _record_follow(self, db, event, direction):
        existing = db.execute(
            "SELECT id FROM follows WHERE group_id=? AND actor=? AND target=?",
            (event["rb:group"], event["actor"], event["object"]),
        ).fetchone()
        if existing:
            if existing[0] != event["id"]:
                raise FederationError("conflicting_follow", 409)
            return
        db.execute(
            "INSERT INTO follows VALUES (?,?,?,?,?,'pending')",
            (event["id"], event["rb:group"], event["actor"], event["object"], direction),
        )

    def accept(self, local, group_id, *, include_ai=True):
        self._human(local)
        companions = self._companions(local, include_ai)
        with self.store.connect(write=True) as db:
            group = db.execute("SELECT * FROM friendship_groups WHERE id=?", (group_id,)).fetchone()
            if (
                not group
                or group["local_human"] != local
                or group["initiated"]
                or group["status"] not in {"pending", "active"}
            ):
                raise FederationError("invitation_not_pending", 409)
            if self._blocked(db, local, group["remote_human"]):
                raise FederationError("contact_blocked", 403)
            if db.execute(
                "SELECT 1 FROM follows WHERE group_id=? AND direction='out' AND actor=?",
                (group_id, local),
            ).fetchone():
                return  # idempotent human action; do not mutate previously approved AI list
            db.execute(
                "UPDATE friendship_groups SET local_ai=? WHERE id=?", (encode(companions), group_id)
            )
            follow = db.execute("SELECT * FROM follows WHERE id=?", (group_id,)).fetchone()
            self._accept_follow(db, follow)
            reverse = self.service.event(
                local,
                group["remote_human"],
                "Follow",
                object=group["remote_human"],
                **{"rb:group": group_id, "rb:companions": companions},
            )
            self._record_follow(db, reverse, "out")
            self.service.enqueue(db, reverse, group["remote_human"])
            self._reconcile(db, group_id)

    def reject(self, local, group_id):
        self._human(local)
        with self.store.connect(write=True) as db:
            group = db.execute("SELECT * FROM friendship_groups WHERE id=?", (group_id,)).fetchone()
            if not group or group["local_human"] != local or group["initiated"]:
                raise FederationError("invitation_not_found", 404)
            if group["status"] == "rejected":
                return
            if group["status"] != "pending":
                raise FederationError("invitation_not_pending", 409)
            if db.execute(
                "SELECT 1 FROM follows WHERE group_id=? AND status='accepted'", (group_id,)
            ).fetchone():
                raise FederationError("already_accepted_use_remove", 409)
            event = self.service.event(
                local, group["remote_human"], "Reject", object=group_id, **{"rb:group": group_id}
            )
            self.service.enqueue(db, event, group["remote_human"])
            self._close(db, group_id, "rejected")

    def _accept_follow(self, db, follow):
        if follow["status"] != "pending":
            return
        db.execute("UPDATE follows SET status='accepted' WHERE id=?", (follow["id"],))
        event = self.service.event(
            follow["target"],
            follow["actor"],
            "Accept",
            object=follow["id"],
            **{"rb:group": follow["group_id"]},
        )
        self.service.enqueue(db, event, follow["actor"])

    def _reconcile(self, db, group_id):
        group = db.execute("SELECT * FROM friendship_groups WHERE id=?", (group_id,)).fetchone()
        if group["status"] not in {"pending", "active"}:
            return
        local, remote = group["local_human"], group["remote_human"]
        outgoing = db.execute(
            "SELECT * FROM follows WHERE group_id=? AND actor=? AND target=?",
            (group_id, local, remote),
        ).fetchone()
        incoming = db.execute(
            "SELECT * FROM follows WHERE group_id=? AND actor=? AND target=?",
            (group_id, remote, local),
        ).fetchone()
        if group["initiated"] and outgoing and outgoing["status"] == "accepted" and incoming:
            self._accept_follow(db, incoming)
            incoming = db.execute("SELECT * FROM follows WHERE id=?", (incoming["id"],)).fetchone()
        if not (outgoing and incoming and outgoing["status"] == incoming["status"] == "accepted"):
            return
        db.execute("UPDATE friendship_groups SET status='active' WHERE id=?", (group_id,))
        local_members = [local] + array(group["local_ai"])
        remote_members = [remote] + array(group["remote_ai"])
        for actor in local_members:
            for target in remote_members:
                if actor == local and target == remote:
                    continue
                if not db.execute(
                    "SELECT 1 FROM follows WHERE group_id=? AND actor=? AND target=?",
                    (group_id, actor, target),
                ).fetchone():
                    event = self.service.event(
                        actor, target, "Follow", object=target, **{"rb:group": group_id}
                    )
                    self._record_follow(db, event, "out")
                    self.service.enqueue(db, event, target)
        for pending in db.execute(
            "SELECT * FROM follows WHERE group_id=? AND direction='in' AND status='pending'",
            (group_id,),
        ).fetchall():
            if pending["target"] in local_members and pending["actor"] in remote_members:
                self._accept_follow(db, pending)

    def _close(self, db, group_id, status="cancelled"):
        db.execute("UPDATE friendship_groups SET status=? WHERE id=?", (status, group_id))
        db.execute("UPDATE follows SET status='cancelled' WHERE group_id=?", (group_id,))
        group = db.execute("SELECT * FROM friendship_groups WHERE id=?", (group_id,)).fetchone()
        if group:
            for local in [group["local_human"]] + array(group["local_ai"]):
                for remote in [group["remote_human"]] + array(group["remote_ai"]):
                    retained = db.execute(
                        "SELECT 1 FROM follows a JOIN follows b ON a.group_id=b.group_id AND a.actor=b.target AND a.target=b.actor JOIN friendship_groups g ON g.id=a.group_id WHERE a.actor=? AND a.target=? AND a.status='accepted' AND b.status='accepted' AND g.status='active'",
                        (local, remote),
                    ).fetchone()
                    if not retained:
                        self.service.groups.remove_member(db, local, remote)
                        db.execute(
                            "DELETE FROM profile_cache WHERE reader=? AND actor=?", (local, remote)
                        )
                        db.execute(
                            "DELETE FROM object_cache WHERE reader=? AND (actor=? OR root LIKE ?)",
                            (local, remote, remote + "/objects/%"),
                        )
                        db.execute(
                            "DELETE FROM object_audience WHERE reader=? AND object_id IN (SELECT id FROM social_objects WHERE actor=?)",
                            (remote, local),
                        )
                        db.execute(
                            "DELETE FROM social_references WHERE actor=? AND root IN (SELECT id FROM social_objects WHERE actor=?)",
                            (remote, local),
                        )

        # Unsent membership messages must not re-open the group after a local revoke.
        rows = db.execute(
            "SELECT o.id,o.body FROM outbox o JOIN deliveries d ON o.id=d.event_id "
            "WHERE d.status IN ('pending','retry','leased') "
            "AND json_extract(o.body,'$.\"rb:group\"')=?",
            (group_id,),
        ).fetchall()
        for row in rows:
            body = decode(row["body"])
            if body.get("rb:group") == group_id and body["type"] in {"Follow", "Accept"}:
                db.execute(
                    "UPDATE deliveries SET status='cancelled',lease_token=NULL WHERE event_id=?",
                    (row["id"],),
                )

    def _owner(self, db, local, remote):
        """A friend's AI belongs to that friend: acting on the AI acts on its human's friendship."""
        row = db.execute(
            "SELECT g.remote_human FROM friendship_groups g, json_each(g.remote_ai) j "
            "WHERE g.local_human=? AND j.value=? ORDER BY g.status='active' DESC, g.rowid DESC LIMIT 1",
            (local, remote),
        ).fetchone()
        return row[0] if row else remote

    def remove(self, local, remote, *, block=False):
        """Unfriend or block a human together with all of their AIs (removing an AI means its human)."""
        self._human(local)
        with self.store.connect(write=True) as db:
            remote = self._owner(db, local, remote)
            if block:
                db.execute("INSERT OR IGNORE INTO contact_blocks VALUES (?,?)", (local, remote))
            groups = db.execute(
                "SELECT * FROM friendship_groups WHERE local_human=? AND remote_human=? "
                "AND status IN ('pending','active')",
                (local, remote),
            ).fetchall()
            for group in groups:
                for follow in db.execute(
                    "SELECT * FROM follows WHERE group_id=? AND direction='out' "
                    "AND status IN ('pending','accepted')",
                    (group["id"],),
                ).fetchall():
                    undo = self.service.event(
                        follow["actor"],
                        follow["target"],
                        "Undo",
                        object=follow["id"],
                        **{"rb:group": group["id"]},
                    )
                    self.service.enqueue(db, undo, follow["target"])
                revoke = self.service.event(
                    local, remote, "rb:RevokeGrant", object=group["id"], **{"rb:group": group["id"]}
                )
                self.service.enqueue(db, revoke, remote)
                self._close(db, group["id"])

    def unblock(self, local, remote):
        self._human(local)
        with self.store.connect(write=True) as db:
            remote = self._owner(db, local, remote)
            db.execute("DELETE FROM contact_blocks WHERE owner=? AND remote=?", (local, remote))

    def prepare(self, event):
        """Resolve all untrusted companion identities before entering a write transaction."""
        if event["type"] == "Follow":
            if not isinstance(event.get("object"), str) or event["to"] != [event["object"]]:
                raise FederationError("invalid_follow_target")
            if "rb:invite" in event and event.get("rb:group") != event.get("id"):
                raise FederationError("invite_only_on_root_follow")
        elif event["type"] == "rb:Companions":
            if (
                not isinstance(event.get("rb:group"), str)
                or event.get("object") != event["rb:group"]
                or len(event["to"]) != 1
            ):
                raise FederationError("invalid_companions_update")
        else:
            return
        companions = event.get("rb:companions", [])
        if (
            not isinstance(companions, list)
            or len(companions) > 8
            or any(not isinstance(a, str) for a in companions)
            or len(set(companions)) != len(companions)
        ):
            raise FederationError("invalid_companions")
        for actor_id in companions:
            if origin_of(actor_id) != origin_of(event["actor"]):
                raise FederationError("cross_origin_companion", 403)
            doc = self.service.identity.remote(actor_id)
            if doc["type"] != "Service" or doc["rb:kind"] != "ai":
                raise FederationError("companion_must_be_ai")

    def receive(self, db, event):
        kind, actor, target = event["type"], event["actor"], event.get("object")
        group_id = event.get("rb:group")
        if not isinstance(group_id, str) or not isinstance(target, str):
            raise FederationError("invalid_relationship_event")
        group = db.execute("SELECT * FROM friendship_groups WHERE id=?", (group_id,)).fetchone()
        if kind == "Follow":
            if group is None:
                if group_id != event["id"]:
                    raise FederationError("group_not_yet_known", 503)
                local = db.execute("SELECT kind FROM actors WHERE id=?", (target,)).fetchone()
                remote = decode(
                    db.execute(
                        "SELECT document FROM remote_actors WHERE id=?", (actor,)
                    ).fetchone()[0]
                )
                if not local or local[0] != "person" or remote["type"] != "Person":
                    raise FederationError("group_requires_humans")
                if self._blocked(db, target, actor):
                    raise FederationError("contact_blocked", 403)
                db.execute(
                    "INSERT INTO friendship_groups VALUES (?,?,?,?,?,'pending',0,?)",
                    (
                        group_id,
                        target,
                        actor,
                        "[]",
                        encode(event.get("rb:companions", [])),
                        time.time(),
                    ),
                )
                if "rb:invite" in event:
                    self.service.invitations.consume(db, event)
                self._record_follow(db, event, "in")
                return
            if group["status"] not in {"pending", "active"}:
                return  # tombstone: valid delayed events never resurrect a closed group
            local_members = [group["local_human"]] + array(group["local_ai"])
            remote_members = [group["remote_human"]] + array(group["remote_ai"])
            if actor == group["remote_human"] and target == group["local_human"]:
                if "rb:companions" not in event:
                    raise FederationError("human_follow_requires_companions")
                existing = db.execute(
                    "SELECT 1 FROM follows WHERE group_id=? AND actor=? AND target=?",
                    (group_id, actor, target),
                ).fetchone()
                if not existing:
                    db.execute(
                        "UPDATE friendship_groups SET remote_ai=? WHERE id=?",
                        (encode(event["rb:companions"]), group_id),
                    )
            elif (
                actor not in remote_members
                or target not in local_members
                or "rb:companions" in event
            ):
                raise FederationError("actor_not_invited_to_group", 403)
            self._record_follow(db, event, "in")
            self._reconcile(db, group_id)
            return
        if kind == "rb:Companions":
            if not group:
                raise FederationError("group_not_yet_known", 503)
            if (
                actor != group["remote_human"]
                or target != group_id
                or event["to"] != [group["local_human"]]
            ):
                raise FederationError("invalid_companions_update", 403)
            if group["status"] in {"pending", "active"}:
                self._set_members(db, group, "remote_ai", event["rb:companions"])
            return
        if not group:
            # Revocation can overtake original Follow. Persist only a valid human-signed root tombstone.
            if kind == "rb:RevokeGrant" and group_id == target and len(event["to"]) == 1:
                local = event["to"][0]
                self._human(local)
                if (
                    decode(
                        db.execute(
                            "SELECT document FROM remote_actors WHERE id=?", (actor,)
                        ).fetchone()[0]
                    )["type"]
                    != "Person"
                ):
                    raise FederationError("human_revoke_required", 403)
                if not group_id.startswith(actor + "/activities/"):
                    raise FederationError("invalid_group_origin", 403)
                db.execute(
                    "INSERT INTO friendship_groups VALUES (?,?,?,?,?,'cancelled',0,?)",
                    (group_id, local, actor, "[]", "[]", time.time()),
                )
                return
            raise FederationError("group_not_yet_known", 503)
        if kind == "rb:RevokeGrant":
            if (
                actor != group["remote_human"]
                or target != group_id
                or event["to"] != [group["local_human"]]
            ):
                raise FederationError("invalid_group_revocation", 403)
            self._close(db, group_id)
            return
        follow = db.execute(
            "SELECT * FROM follows WHERE id=? AND group_id=?", (target, group_id)
        ).fetchone()
        if not follow:
            if group["status"] in {"cancelled", "rejected"}:
                return
            raise FederationError("unknown_follow", 503)
        if kind in {"Accept", "Reject"}:
            if (
                actor != follow["target"]
                or follow["direction"] != "out"
                or event["to"] != [follow["actor"]]
            ):
                raise FederationError("invalid_follow_response", 403)
            if group["status"] not in {"pending", "active"} or follow["status"] != "pending":
                return
            db.execute(
                "UPDATE follows SET status=? WHERE id=?",
                ("accepted" if kind == "Accept" else "rejected", target),
            )
            if (
                kind == "Reject"
                and follow["actor"] == group["local_human"]
                and actor == group["remote_human"]
            ):
                self._close(db, group_id, "rejected")
            else:
                self._reconcile(db, group_id)
        elif kind == "Undo":
            if (
                actor != follow["actor"]
                or follow["direction"] != "in"
                or event["to"] != [follow["target"]]
            ):
                raise FederationError("invalid_follow_undo", 403)
            db.execute("UPDATE follows SET status='cancelled' WHERE id=?", (target,))
            if actor == group["remote_human"] and follow["target"] == group["local_human"]:
                self._close(db, group_id)

    def _set_members(self, db, group, column, members):
        """Replace one side's AI list in a friendship and link or unlink the difference."""
        old = array(group[column])
        db.execute(
            f"UPDATE friendship_groups SET {column}=? WHERE id=?", (encode(members), group["id"])
        )
        for gone in set(old) - set(members):
            db.execute(
                "UPDATE follows SET status='cancelled' WHERE group_id=? AND (actor=? OR target=?)",
                (group["id"], gone, gone),
            )
        self._reconcile(db, group["id"])

    def _linked(self, group):
        """Every AI pair across this friendship has accepted Follows in both directions."""
        local = [group["local_human"]] + array(group["local_ai"])
        remote = [group["remote_human"]] + array(group["remote_ai"])
        for actor in local:
            for target in remote:
                if actor == group["local_human"] and target == group["remote_human"]:
                    continue
                if not self.store.one(
                    "SELECT 1 FROM follows a JOIN follows b ON a.group_id=b.group_id "
                    "AND a.actor=b.target AND a.target=b.actor WHERE a.group_id=? "
                    "AND a.actor=? AND a.target=? AND a.status='accepted' AND b.status='accepted'",
                    (group["id"], actor, target),
                ):
                    return False
        return True

    def _peer_supports_companions(self, remote):
        try:
            doc = self.service.identity.remote(remote)
            caps = self.service.http.get_json(doc["rb:capabilities"])
        except Exception:  # offline or old peer: try again later
            return False
        return COMPANIONS_CAPABILITY in caps.get("capabilities", [])

    def sync_companions(self, *, force=False):
        """Keep every friendship's AI list equal to the AIs each local human controls.

        Runs from the delivery worker. Peers that do not understand rb:Companions yet are
        skipped without sending anything, and retried after they upgrade.
        """
        sent = 0
        now = time.time()
        for group in self.store.all("SELECT * FROM friendship_groups WHERE status='active'"):
            current = self._companions(group["local_human"], True)
            changed = current != array(group["local_ai"])
            if not changed and self._linked(group):
                continue
            key = "companions-sync:" + group["id"]
            last = self.store.one("SELECT value FROM metadata WHERE key=?", (key,))
            # A changed AI list goes out at once; re-sending for a slow or old peer is throttled.
            if last and not force and now - float(last[0]) < COMPANIONS_RETRY_SECONDS:
                if not changed:
                    continue
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(now)),
                )
            if not self._peer_supports_companions(group["remote_human"]):
                continue
            with self.store.connect(write=True) as db:
                fresh = db.execute(
                    "SELECT * FROM friendship_groups WHERE id=?", (group["id"],)
                ).fetchone()
                if fresh["status"] != "active":
                    continue
                # Follows the peer rejected before it understood our AIs are sent again.
                for row in db.execute(
                    "SELECT f.id FROM follows f JOIN deliveries d ON d.event_id=f.id "
                    "WHERE f.group_id=? AND f.direction='out' AND f.status='pending' "
                    "AND d.status='dead' AND f.actor<>?",
                    (group["id"], group["local_human"]),
                ).fetchall():
                    db.execute("DELETE FROM follows WHERE id=?", (row[0],))
                event = self.service.event(
                    fresh["local_human"],
                    fresh["remote_human"],
                    "rb:Companions",
                    object=fresh["id"],
                    **{"rb:group": fresh["id"], "rb:companions": current},
                )
                self.service.enqueue(db, event, fresh["remote_human"])
                self._set_members(db, fresh, "local_ai", current)
            sent += 1
        return sent

    def is_friend(self, local, remote):
        if self.store.blocked(remote, origin_of(remote)):
            return False
        return bool(
            self.store.one(
                "SELECT 1 FROM follows a JOIN follows b ON a.group_id=b.group_id "
                "AND a.actor=b.target AND a.target=b.actor "
                "JOIN friendship_groups g ON g.id=a.group_id WHERE a.actor=? AND a.target=? "
                "AND a.status='accepted' AND b.status='accepted' AND g.status='active' "
                "AND NOT EXISTS(SELECT 1 FROM contact_blocks x WHERE x.owner=g.local_human "
                "AND x.remote=g.remote_human)",
                (local, remote),
            )
        )

    def contacts(self, local, before=None, limit=50):
        self._human(local)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise FederationError("invalid_limit")
        if before is not None and (type(before) is not int or not 0 < before < 2**63):
            raise FederationError("invalid_cursor")
        items = [
            dict(r)
            for r in self.store.all(
                "SELECT g.rowid AS cursor,g.id,g.remote_human,g.local_ai,g.remote_ai,g.status,g.initiated,COALESCE(n.note,'') AS note "
                "FROM friendship_groups g LEFT JOIN contact_notes n ON n.owner=g.local_human "
                "AND n.remote=g.remote_human WHERE g.local_human=? AND g.rowid<? ORDER BY g.rowid DESC LIMIT ?",
                (local, before or 2**63 - 1, limit),
            )
        ]
        for item in items:
            item["local_ai"] = array(item["local_ai"])
            item["remote_ai"] = array(item["remote_ai"])
        return items

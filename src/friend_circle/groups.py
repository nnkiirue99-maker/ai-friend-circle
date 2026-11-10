"""Private named audiences. Membership removals shrink old grants, never expand them."""

import uuid

from .audience import identifiers
from .errors import FederationError
from .store import encode


class Groups:
    def __init__(self, service):
        self.s, self.store = service, service.store

    def list(self, owner):
        self.s.relationships._human(owner)
        with self.store.connect() as db:
            return [
                self._item(db, row)
                for row in db.execute(
                    "SELECT * FROM audience_groups WHERE owner=? AND deleted=0 ORDER BY rowid",
                    (owner,),
                ).fetchall()
            ]

    def _item(self, db, row):
        return {
            "id": row["id"],
            "name": row["name"],
            "revision": row["revision"],
            "members": [
                r[0]
                for r in db.execute(
                    "SELECT actor FROM audience_group_members WHERE group_id=? ORDER BY actor",
                    (row["id"],),
                )
            ],
        }

    def _owned(self, db, owner, identifier, revision=None):
        if not isinstance(identifier, str):
            raise FederationError("invalid_group")
        row = db.execute(
            "SELECT * FROM audience_groups WHERE id=? AND owner=? AND deleted=0",
            (identifier, owner),
        ).fetchone()
        if not row:
            raise FederationError("group_not_found", 404)
        if revision is not None and (type(revision) is not int or revision != row["revision"]):
            raise FederationError("group_revision_conflict", 409)
        return row

    def save(self, owner, name, members, identifier=None, revision=None):
        self.s.relationships._human(owner)
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 30
            or any(ord(c) < 32 for c in name)
        ):
            raise FederationError("invalid_group_name")
        members = identifiers(members, "group_members")
        if len(members) > 1000:
            raise FederationError("too_many_group_members")
        with self.store.connect(write=True) as db:
            if members - set(self.s.social.friends(owner)):
                raise FederationError("group_member_not_friend", 403)
            if identifier is None:
                if (
                    db.execute(
                        "SELECT count(*) FROM audience_groups WHERE owner=? AND deleted=0", (owner,)
                    ).fetchone()[0]
                    >= 100
                ):
                    raise FederationError("too_many_groups")
                identifier = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO audience_groups VALUES (?,?,?,1,0)",
                    (identifier, owner, name.strip()),
                )
            else:
                if revision is None:
                    raise FederationError("group_revision_conflict", 409)
                self._owned(db, owner, identifier, revision)
                db.execute(
                    "UPDATE audience_groups SET name=?,revision=revision+1 WHERE id=?",
                    (name.strip(), identifier),
                )
            db.execute("DELETE FROM audience_group_members WHERE group_id=?", (identifier,))
            db.executemany(
                "INSERT INTO audience_group_members VALUES (?,?)",
                [(identifier, a) for a in sorted(members)],
            )
            self._revoke(db, identifier)
            return self._item(db, self._owned(db, owner, identifier))

    def delete(self, owner, identifier, revision):
        self.s.relationships._human(owner)
        with self.store.connect(write=True) as db:
            if revision is None:
                raise FederationError("group_revision_conflict", 409)
            self._owned(db, owner, identifier, revision)
            db.execute(
                "UPDATE audience_groups SET deleted=1,revision=revision+1 WHERE id=?", (identifier,)
            )
            db.execute("DELETE FROM audience_group_members WHERE group_id=?", (identifier,))
            self._revoke(db, identifier)

    def expand(self, db, owner, proposal):
        if not isinstance(proposal, dict):
            raise FederationError("invalid_publish_proposal")
        audience = proposal.get("audience", {})
        if not isinstance(audience, dict) or audience.get("mode") != "group":
            return proposal
        if proposal.get("visibility") != "federated_friends" or set(audience) - {
            "mode",
            "groups",
            "exclude",
        }:
            raise FederationError("invalid_group_audience")
        groups = identifiers(audience.get("groups"), "groups")
        if not 1 <= len(groups) <= 20:
            raise FederationError("select_one_to_twenty_groups")
        members = set()
        for identifier in groups:
            self._owned(db, owner, identifier)
            members.update(
                r[0]
                for r in db.execute(
                    "SELECT actor FROM audience_group_members WHERE group_id=?", (identifier,)
                )
            )
        members &= set(self.s.social.friends(owner))
        exclude = identifiers(audience.get("exclude", []), "exclude")
        if exclude - members:
            raise FederationError("audience_contains_unauthorized_actor", 403)
        if not members - exclude:
            raise FederationError("empty_group_audience")
        return {
            **proposal,
            "audience": {
                "mode": "selected",
                "include": sorted(members),
                "exclude": sorted(exclude),
            },
        }

    def record(self, db, identifier, proposal):
        db.execute(
            "INSERT INTO post_scopes VALUES (?,?) ON CONFLICT(object_id) DO UPDATE SET body=excluded.body",
            (identifier, encode(proposal)),
        )
        db.execute("DELETE FROM post_groups WHERE object_id=?", (identifier,))
        audience = proposal.get("audience", {})
        if audience.get("mode") == "group":
            db.executemany(
                "INSERT INTO post_groups VALUES (?,?)",
                [(identifier, g) for g in audience["groups"]],
            )

    def _revoke(self, db, group):
        for post in db.execute(
            "SELECT o.id,o.actor FROM social_objects o JOIN post_groups p ON p.object_id=o.id WHERE p.group_id=? AND o.deleted=0",
            (group,),
        ).fetchall():
            allowed = {
                r[0]
                for r in db.execute(
                    "SELECT m.actor FROM post_groups p JOIN audience_groups g ON g.id=p.group_id "
                    "JOIN audience_group_members m ON m.group_id=g.id WHERE p.object_id=? AND g.deleted=0",
                    (post["id"],),
                )
            }
            previous = {
                r[0]
                for r in db.execute(
                    "SELECT reader FROM object_audience WHERE object_id=?", (post["id"],)
                )
            }
            removed = previous - allowed
            if not removed:
                continue
            db.executemany(
                "DELETE FROM object_audience WHERE object_id=? AND reader=?",
                [(post["id"], a) for a in removed],
            )
            db.execute("UPDATE social_objects SET revision=revision+1 WHERE id=?", (post["id"],))
            self.s.social._notify(db, post["actor"], post["id"], "post", previous)
            for reader in removed:
                db.execute(
                    "DELETE FROM object_cache WHERE reader=? AND (id=? OR root=?)",
                    (reader, post["id"], post["id"]),
                )

    def remove_member(self, db, owner, actor):
        groups = [
            r[0]
            for r in db.execute(
                "SELECT g.id FROM audience_groups g JOIN audience_group_members m ON m.group_id=g.id WHERE g.owner=? AND m.actor=? AND g.deleted=0",
                (owner, actor),
            )
        ]
        for group in groups:
            db.execute(
                "DELETE FROM audience_group_members WHERE group_id=? AND actor=?", (group, actor)
            )
            db.execute("UPDATE audience_groups SET revision=revision+1 WHERE id=?", (group,))
            self._revoke(db, group)

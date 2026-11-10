"""Actor-specific profiles and authorized canonical objects with reader-partitioned caches.

Notifications carry references only. Private bodies/media are fetched using the
requesting Actor's existing HTTP signature. Interactions are fetched from their
actual author's origin, never attributed using another server's assertion.
"""

from datetime import datetime, timezone
import time
import uuid

from .audience import calculate
from .config import origin_of
from .errors import FederationError
from .media import plain, raster
from .signatures import sign
from .store import encode, decode

CAPABILITY = "private-profiles/1"
SOCIAL_TYPES = {"Create", "Update", "Delete", "Like", "Undo"}
THREAD_RESYNC_SECONDS = 120


def actor_of(identifier):
    if not isinstance(identifier, str) or "/objects/" not in identifier:
        raise FederationError("invalid_object_id")
    actor, tail = identifier.rsplit("/objects/", 1)
    origin_of(actor)
    try:
        if str(uuid.UUID(tail)) != tail:
            raise ValueError
    except ValueError as exc:
        raise FederationError("invalid_object_id") from exc
    return actor


def page(before, limit):
    if (
        type(limit) is not int
        or not 1 <= limit <= 20
        or (before is not None and (type(before) is not int or not 0 < before < 2**63))
    ):
        raise FederationError("invalid_pagination")
    return before or 2**63 - 1


class Social:
    def __init__(self, service):
        self.s, self.store = service, service.store

    def authorize(self, principal, actor):
        self.s.visibility._authorize(principal, actor)

    def speak(self, principal, actor):
        self.s.visibility._authorize_voice(principal, actor)

    def friends(self, actor):
        candidates = self.store.all(
            "SELECT DISTINCT target FROM follows WHERE actor=? AND direction='out' AND status='accepted'",
            (actor,),
        )
        return [r[0] for r in candidates if self.s.relationships.is_friend(actor, r[0])]

    def controller(self, actor):
        row = self.store.one(
            "SELECT a.controller FROM actors a JOIN actors h ON h.id=a.controller "
            "WHERE a.id=? AND a.kind='ai' AND h.kind='person'",
            (actor,),
        )
        return row[0] if row else None

    def local(self, actor):
        return bool(self.store.one("SELECT 1 FROM actors WHERE id=?", (actor,)))

    def controls(self, reader, actor):
        """Supervision: the local human who controls a local AI may always read that AI."""
        return bool(
            self.store.one(
                "SELECT 1 FROM actors WHERE id=? AND kind='ai' AND controller=?", (actor, reader)
            )
        )

    def may_see_interaction(self, author, root, reader):
        """WeChat rule for likes and comments, enforced by the author's own server: under a
        post you see only your own friends' interactions. The post's author sees all of
        them; a human and the AIs it controls see each other's."""
        if reader == author or reader == actor_of(root):
            return True
        if self.controls(reader, author) or self.controls(author, reader):
            return True
        return self.s.relationships.is_friend(author, reader)

    def own_authors(self, reader):
        """The identities whose posts belong in this reader's own timeline: itself, plus the
        AIs it controls, or the associated human when the reader is an AI."""
        row = self.store.one("SELECT kind FROM actors WHERE id=?", (reader,))
        if not row:
            return []
        if row[0] != "person":
            human = self.controller(reader)
            return [reader, human] if human else [reader]
        return [reader] + [
            r[0]
            for r in self.store.all(
                "SELECT id FROM actors WHERE controller=? AND kind='ai' ORDER BY id", (reader,)
            )
        ]

    def _profile(self, actor):
        doc = self.s.identity.local_document(actor)
        row = self.store.one("SELECT * FROM actor_profiles WHERE actor=?", (actor,))
        body = (
            decode(row["body"])
            if row
            else {
                "name": doc["preferredUsername"],
                "bio": "",
                "avatar": None,
                "cover": None,
                "links": [],
            }
        )
        return {
            **body,
            "actor_id": actor,
            "kind": doc["rb:kind"],
            "handle": "@" + doc["preferredUsername"] + "@" + origin_of(actor)[8:],
            "revision": row["revision"] if row else 0,
        }

    def set_profile(self, principal, actor, body, expected_revision, lease_seconds=300):
        self.authorize(principal, actor)
        if (
            not isinstance(body, dict)
            or set(body) - {"name", "bio", "avatar", "cover", "links"}
            or type(expected_revision) is not int
        ):
            raise FederationError("invalid_profile")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise FederationError("invalid_cache_lease")
        links = body.get("links", [])
        if (
            not isinstance(links, list)
            or len(links) > 8
            or any(not isinstance(x, str) for x in links)
            or len(set(links)) != len(links)
        ):
            raise FederationError("invalid_profile_links")
        # Explicit owner choice; default is hidden. Only real locally controlled relationships.
        for link in links:
            if link == actor:
                raise FederationError("invalid_profile_links")
            self.authorize(principal, link)
        clean = {
            "name": plain(body.get("name", ""), 80),
            "bio": plain(body.get("bio", ""), 500),
            "avatar": raster(body.get("avatar")),
            "cover": raster(body.get("cover")),
            "links": links,
        }
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT revision FROM actor_profiles WHERE actor=?", (actor,)
            ).fetchone()
            if expected_revision != (row[0] if row else 0):
                raise FederationError("profile_revision_conflict", 409)
            db.execute(
                "INSERT INTO actor_profiles VALUES (?,?,?,?) ON CONFLICT(actor) DO UPDATE SET body=excluded.body,revision=excluded.revision,lease_seconds=excluded.lease_seconds",
                (actor, encode(clean), expected_revision + 1, lease_seconds),
            )
            self.invalidate_profile(db, actor)
        return self._profile(actor)

    def _lease(self, actor, published=None):
        now = time.time()
        row = self.store.one("SELECT lease_seconds FROM actor_profiles WHERE actor=?", (actor,))
        expiry = now + (row[0] if row else 300)
        policy = self.store.one(
            "SELECT visible_days FROM account_visibility WHERE actor=?", (actor,)
        )
        if published is not None and policy and policy[0] is not None:
            expiry = min(expiry, published + policy[0] * 86400)
        return expiry

    def _open_to_new_friends(self, object_id, reader, author):
        """WeChat rule: a post shared with "all friends" is also visible to friends added
        later (still within the account's visible-days window). Selected-friend and group posts,
        and readers the author excluded, stay as they were. A friend who was removed and added
        again does not get history back (removal revokes; re-adding never restores it)."""
        human = self.controller(author) or author
        if self.store.one(
            "SELECT 1 FROM friendship_groups WHERE local_human=? AND status='cancelled' "
            "AND (remote_human=? OR remote_ai LIKE ?)",
            (human, reader, '%"' + reader + '"%'),
        ):
            return False
        scope = self.store.one("SELECT body FROM post_scopes WHERE object_id=?", (object_id,))
        if scope is None:
            # Posts from before scopes were recorded: only one that reached nobody (it was
            # shared with "all friends" while there were none yet) is treated as "all".
            return not self.store.one(
                "SELECT 1 FROM object_audience WHERE object_id=?", (object_id,)
            )
        audience = decode(scope[0]).get("audience", {"mode": "all"})
        if not isinstance(audience, dict) or audience.get("mode", "all") != "all":
            return False
        return reader not in (audience.get("exclude") or [])

    def _allowed(self, row, reader):
        if row["deleted"]:
            return False
        if reader == row["actor"] or self.controls(reader, row["actor"]):
            return True
        if self.controller(reader) == row["actor"]:
            # Binding grants a read of the human's shared posts, including older ones.
            # It never grants their private posts or the right to speak as that human.
            allowed = row["visibility"] != "self"
        elif not self.store.one(
            "SELECT 1 FROM object_audience WHERE object_id=? AND reader=?", (row["id"], reader)
        ) and not (
            row["visibility"] == "federated_friends"
            and self._open_to_new_friends(row["id"], reader, row["actor"])
        ):
            return False
        elif row["visibility"] == "local":
            allowed = self.local(reader)
        else:
            allowed = self.s.relationships.is_friend(row["actor"], reader)
        policy = self.store.one(
            "SELECT visible_days FROM account_visibility WHERE actor=?", (row["actor"],)
        )
        return allowed and (
            not policy or policy[0] is None or row["published"] > time.time() - policy[0] * 86400
        )

    def _payload(self, row, reader):
        return {
            "id": row["id"],
            "actor": row["actor"],
            "kind": row["kind"],
            "root": row["root"],
            "parent": row["parent"],
            "content": decode(row["body"]),
            "published": row["published"],
            "revision": row["revision"],
            "source_seq": row["seq"],
            "lease_until": self._lease(row["actor"], row["published"]),
        }

    def _rpc(self, reader, actor, path, body):
        doc = self.s.identity.remote(actor)
        if CAPABILITY not in doc.get("rb:features", []):
            raise FederationError("peer_profiles_not_supported", 422)
        msg = sign(
            self.s.identity,
            reader,
            "POST",
            origin_of(actor) + "/federation/social/" + path,
            encode(body).encode(),
        )
        reply = self.s.http.send(msg.method, msg.url, msg.headers, msg.body)
        if reply.status != 200:
            raise FederationError(
                "remote_social_denied"
                if reply.status in {403, 404, 410}
                else "remote_social_unavailable",
                reply.status if reply.status in {403, 404, 410} else 502,
            )
        return reply.json()

    def access(self, caller, root, reader):
        # Private boolean authorization oracle; only a currently authorized participant can ask.
        row = self.store.one(
            "SELECT rowid AS seq,* FROM social_objects WHERE id=? AND kind='post'", (root,)
        )
        if not row or not self._allowed(row, caller) or not self._allowed(row, reader):
            raise FederationError("object_forbidden", 403)
        return {
            "allowed": True,
            "root": root,
            "reader": reader,
            "lease_until": self._lease(row["actor"], row["published"]),
        }

    def root_access(self, caller, root, reader):
        owner = actor_of(root)
        if self.local(owner):
            return self.access(caller, root, reader)
        result = self._rpc(caller, owner, "access", {"root": root, "reader": reader})
        if (
            result.get("allowed") is not True
            or result.get("root") != root
            or result.get("reader") != reader
        ):
            raise FederationError("invalid_access_response", 502)
        return result

    def object(self, reader, identifier):
        row = self.store.one("SELECT rowid AS seq,* FROM social_objects WHERE id=?", (identifier,))
        if not row:
            raise FederationError("object_not_found", 404)
        if row["kind"] == "post":
            if not self._allowed(row, reader):
                raise FederationError("object_forbidden", 403)
            return self._payload(row, reader)
        if row["deleted"]:
            raise FederationError("object_not_found", 404)
        if not self.may_see_interaction(row["actor"], row["root"], reader):
            raise FederationError("object_forbidden", 403)
        access = self.root_access(row["actor"], row["root"], reader)
        result = self._payload(row, reader)
        result["lease_until"] = min(result["lease_until"], access["lease_until"])
        return result

    def history(self, reader, actor, before=None, limit=10):
        before = page(before, limit)
        if reader != actor and not (
            self.local(reader) or self.s.relationships.is_friend(actor, reader)
        ):
            raise FederationError("profile_forbidden", 403)
        policy = self.store.one(
            "SELECT visible_days FROM account_visibility WHERE actor=?", (actor,)
        )
        owner_view = reader == actor or self.controls(reader, actor)
        minimum = (
            0 if owner_view or not policy or policy[0] is None else time.time() - policy[0] * 86400
        )
        friend = reader != actor and self.s.relationships.is_friend(actor, reader)
        candidates = self.store.all(
            "SELECT o.rowid AS seq,o.id,EXISTS(SELECT 1 FROM object_audience a WHERE a.object_id=o.id AND a.reader=?) AS listed FROM social_objects o WHERE o.actor=? AND o.kind='post' AND o.deleted=0 AND o.rowid<? AND o.published>? AND (? OR (? AND o.visibility<>'self') OR EXISTS(SELECT 1 FROM object_audience a WHERE a.object_id=o.id AND a.reader=?) OR (? AND o.visibility='federated_friends')) ORDER BY o.rowid DESC LIMIT ?",
            (
                reader,
                actor,
                before,
                minimum,
                int(owner_view),
                self.controller(reader) == actor,
                reader,
                int(friend),
                (limit + 1) * 4,
            ),
        )
        # Posts this reader reaches only through the "all friends" rule are checked one by one.
        rows = [
            r
            for r in candidates
            if owner_view
            or self.controller(reader) == actor
            or r["listed"]
            or self._open_to_new_friends(r["id"], reader, actor)
        ][: limit + 1]
        profile = self._profile(actor)
        # Links remain opt-in, and must independently be visible to this reader.
        profile["links"] = [
            x
            for x in profile["links"]
            if reader == actor or self.s.relationships.is_friend(x, reader)
        ]
        return {
            "profile": profile,
            "items": [{"id": r["id"], "source_seq": r["seq"]} for r in rows[:limit]],
            "next_cursor": rows[limit - 1]["seq"] if len(rows) > limit else None,
            "lease_until": self._lease(actor),
        }

    def _notify(self, db, actor, identifier, kind, recipients, event_type="Update"):
        for recipient in set(recipients):
            if self.local(recipient):
                continue
            event = self.s.event(
                actor,
                recipient,
                event_type,
                object={"id": identifier, "actor": actor, "kind": kind},
                **{"rb:social": True},
            )
            self.s.enqueue(db, event, recipient)

    def invalidate_profile(self, db, actor):
        # Notification contains no private body. Receiver invalidates all cached posts of this actor.
        self._notify(db, actor, actor, "profile", self.friends(actor))

    def _audience(self, actor, proposal, db=None):
        if db is not None:
            proposal = self.s.groups.expand(db, actor, proposal)
        return calculate(
            actor,
            proposal,
            friends=self.friends(actor),
            local_actors=[
                r[0] for r in self.store.all("SELECT id FROM actors WHERE kind IN ('person','ai')")
            ],
            blocked=set(),
            now=datetime.now(timezone.utc),
        )

    def publish(self, principal, actor, text, proposal, media=None):
        self.speak(principal, actor)
        pictures = media or []
        if not isinstance(pictures, list) or len(pictures) > 2:
            raise FederationError("too_many_images")
        content = {"text": plain(text), "media": [raster(x) for x in pictures]}
        identifier = actor + "/objects/" + str(uuid.uuid4())
        with self.store.connect(write=True) as db:
            scope = self._audience(actor, proposal, db)
            db.execute(
                "INSERT INTO social_objects(id,actor,kind,root,parent,body,published,revision,visibility) VALUES (?,?,'post',?,NULL,?,?,1,?)",
                (
                    identifier,
                    actor,
                    identifier,
                    encode(content),
                    scope.published_at.timestamp(),
                    scope.visibility,
                ),
            )
            db.executemany(
                "INSERT INTO object_audience VALUES (?,?)",
                [(identifier, r) for r in scope.recipients],
            )
            self.s.groups.record(db, identifier, proposal)
            self._notify(db, actor, identifier, "post", scope.recipients, "Create")
        return self.object(actor, identifier)

    def update(self, principal, identifier, text, proposal, expected_revision, media=None):
        row = self.store.one("SELECT * FROM social_objects WHERE id=?", (identifier,))
        if not row or row["kind"] != "post":
            raise FederationError("post_not_found", 404)
        self.speak(principal, row["actor"])
        pictures = media or []
        if not isinstance(pictures, list) or len(pictures) > 2:
            raise FederationError("too_many_images")
        content = {"text": plain(text), "media": [raster(x) for x in pictures]}
        with self.store.connect(write=True) as db:
            scope = self._audience(row["actor"], proposal, db)
            recipients = set(scope.recipients)
            old_groups = {
                r[0]
                for r in db.execute(
                    "SELECT group_id FROM post_groups WHERE object_id=?", (identifier,)
                )
            }
            new_groups = set(proposal.get("audience", {}).get("groups", []))
            if old_groups and old_groups == new_groups:
                # Editing text never grants old posts to newly added/re-added members.
                recipients &= {
                    r[0]
                    for r in db.execute(
                        "SELECT reader FROM object_audience WHERE object_id=?", (identifier,)
                    )
                }
            if (
                type(expected_revision) is not int
                or not db.execute(
                    "UPDATE social_objects SET body=?,revision=revision+1,visibility=? WHERE id=? AND revision=? AND deleted=0",
                    (encode(content), scope.visibility, identifier, expected_revision),
                ).rowcount
            ):
                raise FederationError("object_revision_conflict", 409)
            previous = [
                r[0]
                for r in db.execute(
                    "SELECT reader FROM object_audience WHERE object_id=?", (identifier,)
                )
            ]
            db.execute("DELETE FROM object_audience WHERE object_id=?", (identifier,))
            db.executemany(
                "INSERT INTO object_audience VALUES (?,?)",
                [(identifier, r) for r in recipients],
            )
            self.s.groups.record(db, identifier, proposal)
            self._notify(db, row["actor"], identifier, "post", set(previous) | recipients)
        return self.object(row["actor"], identifier)

    def delete(self, principal, identifier):
        row = self.store.one("SELECT * FROM social_objects WHERE id=?", (identifier,))
        if not row:
            raise FederationError("object_not_found", 404)
        self.authorize(principal, row["actor"])
        with self.store.connect(write=True) as db:
            if not db.execute(
                "UPDATE social_objects SET deleted=1,revision=revision+1,body=? WHERE id=? AND deleted=0",
                (encode({"text": "", "media": []}), identifier),
            ).rowcount:
                return
            db.execute("DELETE FROM object_cache WHERE id=? OR root=?", (identifier, identifier))
            if row["kind"] == "post":
                recipients = [
                    r[0]
                    for r in db.execute(
                        "SELECT reader FROM object_audience WHERE object_id=?", (identifier,)
                    )
                ]
            else:
                recipients = [actor_of(row["root"])]
            self._notify(
                db,
                row["actor"],
                identifier,
                row["kind"],
                recipients,
                "Undo" if row["kind"] == "like" else "Delete",
            )

    def history_grant(self, principal, actor, reader, identifiers):
        self.speak(principal, actor)
        if (
            not self.s.relationships.is_friend(actor, reader)
            or not isinstance(identifiers, list)
            or not 1 <= len(identifiers) <= 50
            or any(not isinstance(i, str) for i in identifiers)
        ):
            raise FederationError("invalid_history_grant", 403)
        with self.store.connect(write=True) as db:
            for identifier in identifiers:
                row = db.execute(
                    "SELECT actor FROM social_objects WHERE id=? AND kind='post' AND deleted=0 AND visibility='federated_friends'",
                    (identifier,),
                ).fetchone()
                if not row or row[0] != actor:
                    raise FederationError("history_grant_forbidden", 403)
                if db.execute(
                    "SELECT 1 FROM post_groups WHERE object_id=?", (identifier,)
                ).fetchone():
                    raise FederationError("group_post_requires_republish", 403)
                db.execute(
                    "INSERT OR IGNORE INTO object_audience VALUES (?,?)", (identifier, reader)
                )
                self._notify(db, actor, identifier, "post", [reader])

    def interact(self, principal, actor, root, kind, text="", parent=None):
        self.speak(principal, actor)
        if kind not in {"comment", "like"}:
            raise FederationError("invalid_interaction")
        self.store.rate_limit("social-write:" + actor, 30)
        access = self.root_access(actor, root, actor)
        if parent:
            parent_obj = self.fetch(actor, parent)
            if parent_obj["root"] != root or parent_obj["kind"] != "comment":
                raise FederationError("invalid_reply_parent")
            # Fixed hard safety ceiling; AI automation grants may be stricter.
            if parent_obj["content"].get("depth", 0) >= 8:
                raise FederationError("maximum_reply_depth")
        else:
            parent_obj = None
        identifier = actor + "/objects/" + str(uuid.uuid4())
        content = {
            "text": plain(text) if kind == "comment" else "",
            "media": [],
            "depth": parent_obj["content"].get("depth", 0) + 1 if parent_obj else 0,
        }
        owner = actor_of(root)
        if not self.local(owner):
            self.s.identity.remote(owner)
        with self.store.connect(write=True) as db:
            if kind == "like":
                existing = db.execute(
                    "SELECT id FROM social_objects WHERE actor=? AND root=? AND kind='like' AND deleted=0",
                    (actor, root),
                ).fetchone()
                if existing:
                    return {"id": existing[0], "duplicate": True}
            db.execute(
                "INSERT INTO social_objects(id,actor,kind,root,parent,body,published,revision,visibility) VALUES (?,?,?,?,?,?,?,1,'thread')",
                (identifier, actor, kind, root, parent, encode(content), time.time()),
            )
            if self.local(owner):
                db.execute(
                    "INSERT INTO social_references VALUES (?,?,?,?)",
                    (root, identifier, actor, kind),
                )
                self._notify_root(db, root)
            else:
                self._notify(
                    db, actor, identifier, kind, [owner], "Like" if kind == "like" else "Create"
                )
        row = self.store.one("SELECT rowid AS seq,* FROM social_objects WHERE id=?", (identifier,))
        obj = self._payload(row, actor)
        obj["lease_until"] = min(obj["lease_until"], access["lease_until"])
        with self.store.connect(write=True) as db:
            self._cache(db, actor, obj)
        return {"id": identifier, "duplicate": False, "object": obj}

    def _notify_root(self, db, root):
        row = db.execute(
            "SELECT actor,deleted FROM social_objects WHERE id=? AND kind='post'", (root,)
        ).fetchone()
        if not row or row["deleted"]:
            return
        recipients = [
            r[0]
            for r in db.execute("SELECT reader FROM object_audience WHERE object_id=?", (root,))
        ]
        self._notify(db, row["actor"], root, "post", recipients)

    def thread_references(self, reader, root, before=None, limit=20):
        self.access(reader, root, reader)
        before = page(before, limit)
        rows = self.store.all(
            "SELECT rowid AS seq,* FROM social_references WHERE root=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
            (root, before, limit + 1),
        )
        return {
            "root": root,
            "items": [
                {"id": r["id"], "actor": r["actor"], "kind": r["kind"]} for r in rows[:limit]
            ],
            "next_cursor": rows[limit - 1]["seq"] if len(rows) > limit else None,
        }

    def _validated_object(self, result, identifier):
        actor = actor_of(identifier)
        if not isinstance(result, dict) or set(result) != {
            "id",
            "actor",
            "kind",
            "root",
            "parent",
            "content",
            "published",
            "revision",
            "source_seq",
            "lease_until",
        }:
            raise FederationError("invalid_remote_object", 502)
        if (
            result["id"] != identifier
            or result["actor"] != actor
            or result["kind"] not in {"post", "comment", "like"}
        ):
            raise FederationError("object_author_mismatch", 403)
        if (
            type(result["revision"]) is not int
            or result["revision"] < 1
            or type(result["source_seq"]) is not int
            or not 0 < result["source_seq"] < 2**63
        ):
            raise FederationError("invalid_object_revision", 502)
        if (
            type(result["published"]) not in {int, float}
            or not 0 < result["published"] <= time.time() + 30
        ):
            raise FederationError("invalid_object_time", 502)
        expiry = result["lease_until"]
        if type(expiry) not in {int, float} or not 0 < expiry <= time.time() + 3605:
            raise FederationError("invalid_cache_lease", 502)
        actor_of(result["root"])
        if result["kind"] == "post" and (
            result["root"] != identifier or result["parent"] is not None
        ):
            raise FederationError("invalid_root", 502)
        if result["parent"] is not None:
            actor_of(result["parent"])
        content = result["content"]
        if not isinstance(content, dict) or set(content) - {"text", "media", "depth"}:
            raise FederationError("invalid_content", 502)
        pictures = content.get("media", [])
        if not isinstance(pictures, list) or len(pictures) > 2:
            raise FederationError("invalid_content", 502)
        depth = content.get("depth", 0)
        if type(depth) is not int or not 0 <= depth <= 8:
            raise FederationError("invalid_reply_depth", 502)
        result = {
            **result,
            "content": {
                "text": plain(content.get("text", "")),
                "media": [raster(x) for x in pictures],
                "depth": depth,
            },
        }
        return result

    def _cache(self, db, reader, obj):
        tombstone = db.execute(
            "SELECT 1 FROM cache_tombstones WHERE reader=? AND id=?", (reader, obj["id"])
        ).fetchone()
        if tombstone:
            return
        db.execute(
            "INSERT INTO object_cache VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(reader,id) DO UPDATE SET body=excluded.body,expires=excluded.expires,synced=excluded.synced,revision=excluded.revision,source_seq=excluded.source_seq WHERE excluded.revision>=object_cache.revision",
            (
                reader,
                obj["id"],
                obj["actor"],
                obj["root"],
                obj["kind"],
                encode(obj),
                obj["lease_until"],
                time.time(),
                obj["revision"],
                obj["source_seq"],
            ),
        )

    def fetch(self, reader, identifier):
        owner = actor_of(identifier)
        if self.local(owner):
            return self.object(reader, identifier)
        result = self._validated_object(
            self._rpc(reader, owner, "object", {"id": identifier}), identifier
        )
        if result["kind"] == "post" and not self.s.relationships.is_friend(reader, owner):
            raise FederationError("profile_forbidden", 403)
        with self.store.connect(write=True) as db:
            self._cache(db, reader, result)
        return result

    def _validated_profile(self, actor, result):
        profile = result.get("profile", {})
        if profile.get("actor_id") != actor or profile.get("kind") not in {"person", "ai"}:
            raise FederationError("profile_identity_mismatch", 502)
        doc = self.s.identity.remote(actor)
        if profile["kind"] != doc["rb:kind"]:
            raise FederationError("profile_kind_mismatch", 502)
        links = profile.get("links", [])
        if (
            not isinstance(links, list)
            or len(links) > 8
            or any(not isinstance(x, str) or origin_of(x) != origin_of(actor) for x in links)
        ):
            raise FederationError("invalid_profile_links", 502)
        clean = {
            "actor_id": actor,
            "kind": profile["kind"],
            "name": plain(profile.get("name", ""), 80),
            "bio": plain(profile.get("bio", ""), 500),
            "avatar": raster(profile.get("avatar")),
            "cover": raster(profile.get("cover")),
            "links": links,
            "handle": "@" + doc["preferredUsername"] + "@" + origin_of(actor)[8:],
        }
        expiry = result.get("lease_until")
        if type(expiry) not in {int, float} or not 0 < expiry <= time.time() + 3605:
            raise FederationError("invalid_cache_lease", 502)
        return clean

    def refresh(self, reader, actor, before=None, limit=10):
        page(before, limit)
        if self.local(actor):
            return self.history(reader, actor, before, limit)
        if not self.s.relationships.is_friend(reader, actor):
            raise FederationError("profile_forbidden", 403)
        result = self._rpc(
            reader, actor, "history", {"actor": actor, "before": before, "limit": limit}
        )
        clean = self._validated_profile(actor, result)
        items = result.get("items")
        if not isinstance(items, list) or len(items) > limit:
            raise FederationError("invalid_history_page", 502)
        expiry = result.get("lease_until")
        if type(expiry) not in {int, float} or not 0 < expiry <= time.time() + 3605:
            raise FederationError("invalid_cache_lease", 502)
        next_cursor = result.get("next_cursor")
        page(next_cursor, limit)
        # Do not extend old bodies' leases from a profile-only response.
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO profile_cache VALUES (?,?,?,?,?,?) ON CONFLICT(reader,actor) DO UPDATE SET body=excluded.body,expires=excluded.expires,synced=excluded.synced,next_cursor=excluded.next_cursor",
                (reader, actor, encode(clean), expiry, time.time(), next_cursor),
            )
        for item in items:
            if not isinstance(item, dict) or actor_of(item.get("id")) != actor:
                raise FederationError("invalid_history_author", 502)
            try:
                self.fetch(reader, item["id"])
            except FederationError as exc:
                if exc.status not in {403, 404, 410}:
                    raise
                self.evict(reader, item["id"])
        return result

    def evict(self, reader, identifier):
        with self.store.connect(write=True) as db:
            db.execute(
                "DELETE FROM object_cache WHERE reader=? AND (id=? OR root=?)",
                (reader, identifier, identifier),
            )

    def profile(self, reader, actor, before=None, limit=10):
        cursor = page(before, limit)
        if self.local(actor):
            history = self.history(reader, actor, before, limit)
            return {
                **history,
                "items": [self.object(reader, x["id"]) for x in history["items"]],
                "last_synced": time.time(),
                "source": "local",
                "relationship": "self" if reader == actor else "active",
            }
        doc = self.store.one("SELECT document FROM remote_actors WHERE id=?", (actor,))
        if not doc:
            raise FederationError("actor_not_discovered", 404)
        public = decode(doc[0])
        status = "active" if self.s.relationships.is_friend(reader, actor) else "none"
        if status == "none":
            if self.store.one(
                "SELECT 1 FROM contact_blocks WHERE owner=? AND remote=?", (reader, actor)
            ):
                status = "blocked"
            elif self.store.one(
                "SELECT 1 FROM friendship_groups WHERE local_human=? AND remote_human=? AND status='pending'",
                (reader, actor),
            ):
                status = "pending"

        profile = {
            "actor_id": actor,
            "kind": public["rb:kind"],
            "name": public["preferredUsername"],
            "avatar": None,
            "cover": None,
            "bio": "",
            "links": [],
        }
        cached = self.store.one(
            "SELECT * FROM profile_cache WHERE reader=? AND actor=?", (reader, actor)
        )
        now = time.time()
        if status != "active":
            return {
                "profile": profile,
                "items": [],
                "next_cursor": None,
                "last_synced": cached["synced"] if cached else None,
                "source": "cache",
                "relationship": status,
                "expired": True,
            }
        rows = self.store.all(
            "SELECT body FROM object_cache WHERE reader=? AND actor=? AND kind='post' AND expires>? AND source_seq<? ORDER BY source_seq DESC LIMIT ?",
            (reader, actor, now, cursor, limit + 1),
        )
        if cached and cached["expires"] > now:
            profile = {**decode(cached["body"]), "lease_until": cached["expires"]}
        objects = [decode(r[0]) for r in rows[:limit]]
        return {
            "profile": profile,
            "items": objects,
            "next_cursor": objects[-1]["source_seq"]
            if len(rows) > limit
            else (cached["next_cursor"] if cached else None),
            "last_synced": cached["synced"] if cached else None,
            "source": "cache",
            "relationship": status,
            "expired": not cached or cached["expires"] <= now,
        }

    def thread(self, reader, root, *, refresh=False, before=None, limit=20):
        page(before, limit)
        owner = actor_of(root)
        if self.local(owner):
            self.access(reader, root, reader)
        else:
            post = self.store.one(
                "SELECT 1 FROM object_cache WHERE reader=? AND id=? AND kind='post' AND expires>?",
                (reader, root, time.time()),
            )
            if not self.s.relationships.is_friend(reader, owner) or not post:
                raise FederationError("thread_forbidden", 403)
        if not refresh and before is None and self._thread_resync_due(reader, root):
            # Friends' likes and comments are cached with a short lease; reading only the cache
            # made them vanish a few minutes later. Re-sync here, at most every couple of minutes;
            # if a peer is offline, fall back to whatever is still cached.
            try:
                return self.thread(reader, root, refresh=True, limit=limit)
            except FederationError as exc:
                if exc.status < 500:
                    raise
            except OSError:
                pass
        next_cursor = None
        fetched_items = []
        if refresh:
            refs = (
                self.thread_references(reader, root, before, limit)
                if self.local(owner)
                else self._rpc(
                    reader, owner, "thread", {"root": root, "before": before, "limit": limit}
                )
            )
            if (
                refs.get("root") != root
                or not isinstance(refs.get("items"), list)
                or len(refs["items"]) > limit
            ):
                raise FederationError("invalid_thread", 502)
            next_cursor = refs.get("next_cursor")
            page(next_cursor, limit)
            for item in refs["items"]:
                if not isinstance(item, dict) or actor_of(item.get("id")) != item.get("actor"):
                    raise FederationError("invalid_interaction_source", 502)
                try:
                    obj = self.fetch(reader, item["id"])
                    if obj["root"] != root or obj["kind"] not in {"comment", "like"}:
                        self.evict(reader, obj["id"])
                        raise FederationError("interaction_root_mismatch", 403)
                    fetched_items.append(obj)
                except FederationError as exc:
                    if exc.status in {403, 404, 410}:
                        self.evict(reader, item["id"])
                    elif exc.status < 500:
                        raise
                    else:
                        cached = self.store.one(
                            "SELECT body FROM object_cache WHERE reader=? AND id=? AND root=? AND expires>?",
                            (reader, item["id"], root, time.time()),
                        )
                        if cached:
                            fetched_items.append(decode(cached[0]))
        if refresh:
            # The reader's own comment or like exists locally before the origin has received
            # and indexed it; without this it would vanish from view right after sending.
            # The same holds for the AIs this human controls.
            known = {x["id"] for x in fetched_items}
            authors = self.own_authors(reader) or [reader]
            marks = ",".join("?" * len(authors))
            if before is None:
                for row in self.store.all(
                    f"SELECT rowid AS seq,* FROM social_objects WHERE root=? AND actor IN ({marks}) "
                    "AND kind IN ('comment','like') AND deleted=0 ORDER BY published DESC LIMIT ?",
                    (root, *authors, limit),
                ):
                    if row["id"] not in known:
                        fetched_items.append(self._payload(row, reader))
                fetched_items.sort(key=lambda x: x["published"], reverse=True)
            return {"root": root, "items": fetched_items, "next_cursor": next_cursor}
        rows = self.store.all(
            "SELECT body FROM object_cache WHERE reader=? AND root=? AND kind IN ('comment','like') AND expires>? ORDER BY synced DESC LIMIT ?",
            (reader, root, time.time(), limit),
        )
        objects = [decode(r[0]) for r in rows]
        authors = self.own_authors(reader) or [reader]
        mine = "" if self.local(owner) else f" AND actor IN ({','.join('?' * len(authors))})"
        for row in self.store.all(
            "SELECT rowid AS seq,* FROM social_objects WHERE root=?" + mine + " AND kind IN "
            "('comment','like') AND deleted=0 ORDER BY published DESC LIMIT ?",
            (root, limit) if self.local(owner) else (root, *authors, limit),
        ):
            objects.append(self._payload(row, reader))
        unique = {x["id"]: x for x in objects}
        return {
            "root": root,
            "items": sorted(unique.values(), key=lambda x: x["published"], reverse=True)[:limit],
            "next_cursor": next_cursor,
        }

    def prepare(self, event):
        ref = event.get("object")
        if (
            event.get("rb:social") is not True
            or len(event["to"]) != 1
            or not isinstance(ref, dict)
            or set(ref) != {"id", "actor", "kind"}
            or ref["actor"] != event["actor"]
        ):
            raise FederationError("invalid_social_event")
        reader = event["to"][0]
        if ref["kind"] == "profile":
            if ref["id"] != event["actor"] or event["type"] != "Update":
                raise FederationError("invalid_profile_event")
            if not self.s.relationships.is_friend(reader, event["actor"]):
                return None
            try:
                data = self._rpc(
                    reader, event["actor"], "history", {"actor": event["actor"], "limit": 1}
                )
                return {
                    "profile_data": self._validated_profile(event["actor"], data),
                    "lease_until": data["lease_until"],
                }
            except FederationError as exc:
                if exc.status in {403, 404, 410}:
                    return None
                raise
        if ref["kind"] not in {"post", "comment", "like"} or actor_of(ref["id"]) != event["actor"]:
            raise FederationError("invalid_object_author", 403)
        if event["type"] in {"Delete", "Undo"}:
            return None
        try:
            obj = self._validated_object(
                self._rpc(reader, event["actor"], "object", {"id": ref["id"]}), ref["id"]
            )
            if obj["kind"] != ref["kind"]:
                raise FederationError("object_kind_mismatch", 403)
            if obj["kind"] == "post" and not self.s.relationships.is_friend(reader, event["actor"]):
                raise FederationError("profile_forbidden", 403)
            if obj["kind"] != "post":
                if actor_of(obj["root"]) != reader:
                    raise FederationError("interaction_must_target_root_author", 403)
                self.access(event["actor"], obj["root"], event["actor"])
            return obj
        except FederationError as exc:
            if exc.status in {403, 404, 410}:
                return None  # fail closed; revoke local cached body, acknowledge notification
            raise

    def receive(self, db, event, obj):
        reader = event["to"][0]
        ref = event["object"]
        identifier = ref["id"]
        if ref["kind"] == "profile":
            db.execute(
                "DELETE FROM profile_cache WHERE reader=? AND actor=?", (reader, event["actor"])
            )
            db.execute(
                "UPDATE object_cache SET expires=0 WHERE reader=? AND actor=?",
                (reader, event["actor"]),
            )
            if obj:
                db.execute(
                    "INSERT INTO profile_cache VALUES (?,?,?,?,?,NULL) ON CONFLICT(reader,actor) DO UPDATE SET body=excluded.body,expires=excluded.expires,synced=excluded.synced",
                    (
                        reader,
                        event["actor"],
                        encode(obj["profile_data"]),
                        obj["lease_until"],
                        time.time(),
                    ),
                )
            return
        if event["type"] in {"Delete", "Undo"}:
            db.execute(
                "INSERT OR IGNORE INTO cache_tombstones VALUES (?,?,1)", (reader, identifier)
            )
        if obj is None:
            db.execute(
                "DELETE FROM object_cache WHERE reader=? AND (id=? OR root=?)",
                (reader, identifier, identifier),
            )
            existing = db.execute(
                "SELECT root FROM social_references WHERE id=? AND actor=?",
                (identifier, event["actor"]),
            ).fetchone()
            if existing:
                db.execute(
                    "DELETE FROM social_references WHERE id=? AND actor=?",
                    (identifier, event["actor"]),
                )
                self._notify_root(db, existing["root"])
            return
        if db.execute(
            "SELECT 1 FROM cache_tombstones WHERE reader=? AND id=?", (reader, identifier)
        ).fetchone():
            return
        self._cache(db, reader, obj)
        if obj["kind"] == "post":
            db.execute(
                "UPDATE object_cache SET expires=0 WHERE reader=? AND root=? AND kind!='post'",
                (reader, identifier),
            )
        if obj["kind"] != "post":
            db.execute(
                "INSERT OR IGNORE INTO social_references VALUES (?,?,?,?)",
                (obj["root"], identifier, obj["actor"], obj["kind"]),
            )
            self._notify_root(db, obj["root"])

    def _thread_resync_due(self, reader, root):
        key = "thread-sync:" + reader + " " + root
        now = time.time()
        with self.store.connect(write=True) as db:
            last = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if last and now - float(last[0]) < THREAD_RESYNC_SECONDS:
                return False
            db.execute(
                "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(now)),
            )
        return True

    def cleanup(self):
        """Remove expired private caches; retain source originals and permanent delete tombstones."""
        now = time.time()
        with self.store.connect(write=True) as db:
            db.execute(
                "DELETE FROM metadata WHERE key LIKE 'thread-sync:%' AND CAST(value AS REAL)<?",
                (now - 86400,),
            )
            objects = db.execute("DELETE FROM object_cache WHERE expires<=?", (now,)).rowcount
            profiles = db.execute("DELETE FROM profile_cache WHERE expires<=?", (now,)).rowcount
        return {"expired_objects_removed": objects, "expired_profiles_removed": profiles}

    def feed(self, reader, before=None, limit=20):
        cursor = page(before, limit)
        # Newest first by the author's publish time (ms), independent of when we cached it:
        # background renewal re-inserts rows, so cache order says nothing about recency.
        stamp = "CAST(json_extract(body,'$.published')*1000 AS INTEGER)"
        rows = self.store.all(
            f"SELECT {stamp} AS cursor,* FROM object_cache WHERE reader=? AND kind='post' "
            f"AND expires>? AND {stamp}<? ORDER BY cursor DESC,id DESC LIMIT ?",
            (reader, time.time(), cursor, limit + 1),
        )
        # Local bound identities are read from originals. Filter the human's private
        # and out-of-window posts before pagination so hidden rows cannot fill a page.
        authors = self.own_authors(reader)
        own = []
        if authors:
            marks = ",".join("?" * len(authors))
            own = self.store.all(
                "SELECT CAST(o.published*1000 AS INTEGER) AS cursor,o.id FROM social_objects o "
                "LEFT JOIN account_visibility p ON p.actor=o.actor "
                f"WHERE o.actor IN ({marks}) AND o.kind='post' AND o.deleted=0 "
                "AND (o.actor<>? OR (o.visibility<>'self' AND "
                "(p.visible_days IS NULL OR o.published>?-p.visible_days*86400))) "
                "AND CAST(o.published*1000 AS INTEGER)<? ORDER BY cursor DESC,o.id DESC LIMIT ?",
                (*authors, self.controller(reader) or "", time.time(), cursor, limit + 1),
            )
        candidates = [(r["cursor"], r["id"], "cache", r) for r in rows] + [
            (r["cursor"], r["id"], "own", r) for r in own
        ]
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        items = []
        for _, identifier, source, row in candidates[:limit]:
            if source == "own":
                try:
                    items.append(self.object(reader, identifier))
                except FederationError:
                    continue
            elif self.s.relationships.is_friend(reader, row["actor"]):
                items.append(decode(row["body"]))
        return {
            "items": items,
            "next_cursor": candidates[limit - 1][0] if len(candidates) > limit else None,
        }

    def sync_feeds(self, reader=None, budget=20.0, soon=120):
        """Renew friends' first pages shortly before their cache leases lapse.

        Every renewal is a fresh signed read, so the origin re-checks friendship, audience,
        blocks and its visibility window each time; nothing is extended locally. Bounded by a
        time budget, least recently synced first, and one failure skips that server for the
        rest of the pass so an offline friend cannot starve the others.
        """
        deadline, now = time.monotonic() + budget, time.time()
        readers = (
            [reader]
            if reader
            else [
                r[0] for r in self.store.all("SELECT id FROM actors WHERE kind IN ('person','ai')")
            ]
        )
        due = []
        for local in readers:
            for friend in self.friends(local):
                row = self.store.one(
                    "SELECT expires,synced FROM profile_cache WHERE reader=? AND actor=?",
                    (local, friend),
                )
                if reader or not row or row["expires"] - now < soon:
                    due.append((row["synced"] if row else 0, local, friend))
        renewed = failed = skipped = 0
        down = set()
        for _, local, friend in sorted(due):
            if time.monotonic() > deadline or origin_of(friend) in down:
                skipped += 1
                continue
            try:
                self.refresh(local, friend)
                renewed += 1
            except FederationError as exc:
                failed += 1
                if exc.status >= 500 or exc.status == 429:
                    down.add(origin_of(friend))
        return {"renewed": renewed, "failed": failed, "skipped": skipped}

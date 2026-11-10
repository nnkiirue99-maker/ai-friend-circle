from datetime import datetime, timezone
import hashlib
import time
import uuid
import sqlite3

from .config import AS_CONTEXT, RB_CONTEXT, PROFILE, ACTIVITY_MAX_AGE, MAX_BODY, origin_of
from .errors import FederationError
from .identity import Identity
from .network import SafeHTTP
from .signatures import verify
from .store import Store, encode, decode
from .relationships import Relationships, TYPES, COMPANIONS_CAPABILITY
from .audience import AccountVisibility
from .invitations import Invitations, CAPABILITY as INVITATIONS_CAPABILITY
from .groups import Groups
from .social import Social, SOCIAL_TYPES, CAPABILITY as SOCIAL_CAPABILITY


class Federation:
    def __init__(self, config, http=None):
        self.config = config
        self.store = Store(config)
        self.http = http or SafeHTTP()
        self.identity = Identity(self.store, self.http)
        self.relationships = Relationships(self)
        self.visibility = AccountVisibility(self.store)
        self.invitations = Invitations(self)
        self.social = Social(self)
        self.groups = Groups(self)
        self.visibility.on_change = self.social.invalidate_profile
        if not self.store.one("SELECT id FROM actors WHERE kind='server'"):
            try:
                self.identity.create("server", "server")
            except sqlite3.IntegrityError:
                if not self.store.one(
                    "SELECT id FROM actors WHERE username='server' AND kind='server'"
                ):
                    raise

    def capabilities(self):
        return {
            "profile": PROFILE,
            "stage": "profiles",
            "server_actor": self.store.one("SELECT id FROM actors WHERE kind='server'")[0],
            "capabilities": [
                "webfinger",
                "rfc9421-ed25519",
                "durable-inbox",
                "durable-outbox",
                "confirmed-friends",
                "companion-invitations",
                COMPANIONS_CAPABILITY,
                INVITATIONS_CAPABILITY,
                SOCIAL_CAPABILITY,
            ],
            "activity_types": ["rb:Probe"] + sorted(TYPES | SOCIAL_TYPES),
            "limits": {"max_event_bytes": MAX_BODY, "max_signature_age_seconds": 300},
            "private_social_enabled": True,
            # True once any local AI holds a live MCP credential and may act on its own.
            "ai_automation_enabled": bool(
                self.store.one(
                    "SELECT 1 FROM mcp_tokens WHERE revoked=0 AND (expires IS NULL OR expires>?)",
                    (time.time(),),
                )
            ),
        }

    def probe(self, actor_id, recipient):
        """Local administrative diagnostic. Never publishes posts or private data."""
        self.identity.local_document(actor_id)
        self.identity.remote(recipient)
        event = self.event(actor_id, recipient, "rb:Probe")
        with self.store.connect(write=True) as db:
            self.enqueue(db, event, recipient)
        return event

    def event(self, actor_id, recipient, kind, **fields):
        return {
            "@context": [AS_CONTEXT, RB_CONTEXT],
            "id": actor_id + "/activities/" + str(uuid.uuid4()),
            "type": kind,
            "actor": actor_id,
            "to": [recipient],
            "published": datetime.now(timezone.utc).isoformat(),
            **fields,
        }

    def enqueue(self, db, event, recipient):
        # Caller discovers recipients BEFORE opening the transaction; no network while holding a lock.
        row = db.execute("SELECT document FROM remote_actors WHERE id=?", (recipient,)).fetchone()
        if not row:
            raise FederationError("recipient_not_discovered", 409)
        doc = decode(row[0])
        endpoint = doc["endpoints"]["sharedInbox"]
        origin = origin_of(recipient)
        now = time.time()
        db.execute(
            "INSERT INTO outbox VALUES (?,?,?,?)", (event["id"], event["actor"], encode(event), now)
        )
        db.execute("INSERT OR IGNORE INTO peers(origin) VALUES (?)", (origin,))
        db.execute(
            "INSERT INTO deliveries(event_id,recipient,origin,endpoint,next_at) VALUES (?,?,?,?,?)",
            (event["id"], recipient, origin, endpoint, now),
        )

    def receive(self, msg, recipient=None):
        if len(msg.body) > MAX_BODY:
            raise FederationError("message_too_large", 413)
        event = decode(msg.body)
        doc = verify(self.identity, msg)
        actor = doc["id"]
        if event.get("actor") != actor:
            raise FederationError("actor_mismatch", 403)
        self.store.rate_limit("actor:" + actor, self.config.inbox_per_minute)
        if (
            not isinstance(event.get("type"), str)
            or event["type"] not in {"rb:Probe"} | TYPES | SOCIAL_TYPES
        ):
            raise FederationError("activity_not_supported", 422)
        required = {"@context", "id", "type", "actor", "to", "published"}
        allowed = required.copy()
        social = event.get("rb:social") is True
        if event["type"] in SOCIAL_TYPES - TYPES and not social:
            raise FederationError("activity_not_supported", 422)
        if social:
            if event["type"] not in SOCIAL_TYPES:
                raise FederationError("invalid_social_event")
            required |= {"object", "rb:social"}
            allowed |= {"object", "rb:social"}
        elif event["type"] in TYPES:
            required |= {"object", "rb:group"}
            allowed |= {"object", "rb:group"}
            if event["type"] == "Follow":
                allowed |= {"rb:companions", "rb:invite"}
            elif event["type"] == "rb:Companions":
                required |= {"rb:companions"}
                allowed |= {"rb:companions"}
        if not required <= set(event) <= allowed:
            raise FederationError("invalid_event_fields")
        if event["@context"] != [AS_CONTEXT, RB_CONTEXT]:
            raise FederationError("unsupported_context")
        eid = event.get("id")
        if not isinstance(eid, str) or origin_of(eid) != origin_of(actor):
            raise FederationError("event_origin_mismatch", 403)
        if not eid.startswith(actor + "/activities/"):
            raise FederationError("invalid_activity_id")
        try:
            published = datetime.fromisoformat(event["published"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError
            age = time.time() - published.timestamp()
            if not -30 <= age <= ACTIVITY_MAX_AGE:
                raise ValueError
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise FederationError("invalid_activity_time") from exc
        recipients = event.get("to")
        if (
            not isinstance(recipients, list)
            or not 1 <= len(recipients) <= 100
            or any(not isinstance(r, str) for r in recipients)
            or len(set(recipients)) != len(recipients)
            or (recipient and recipient not in recipients)
        ):
            raise FederationError("invalid_recipients", 403)
        for r in recipients:
            if not self.store.one("SELECT 1 FROM actors WHERE id=?", (r,)):
                raise FederationError("recipient_not_local", 403)
        digest = hashlib.sha256(msg.body).hexdigest()
        existing = self.store.one("SELECT digest FROM inbox WHERE actor=? AND id=?", (actor, eid))
        if existing:
            if existing[0] != digest:
                raise FederationError("event_id_conflict", 409)
            return {"accepted": True, "duplicate": True}
        prepared = self.social.prepare(event) if social else None
        if not social and event["type"] in TYPES:
            self.relationships.prepare(event)
        with self.store.connect(write=True) as db:
            existing = db.execute(
                "SELECT digest FROM inbox WHERE actor=? AND id=?", (actor, eid)
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise FederationError("event_id_conflict", 409)
                return {"accepted": True, "duplicate": True}
            db.execute(
                "INSERT INTO inbox(actor,id,body,digest,received,status) "
                "VALUES (?,?,?,?,?,'processed')",
                (actor, eid, msg.body.decode(), digest, time.time()),
            )
            if social:
                self.social.receive(db, event, prepared)
            elif event["type"] in TYPES:
                self.relationships.receive(db, event)
        return {"accepted": True, "duplicate": False}

    def outbox(self, owner, reader, before=None, limit=20):
        # Opaque local rowid pagination; filter authorization before limit.
        self.identity.local_document(owner)
        if not 1 <= limit <= 50:
            raise FederationError("invalid_limit")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT o.rowid AS seq,o.body FROM outbox o "
                "WHERE o.actor=? AND o.rowid<? AND EXISTS(SELECT 1 FROM deliveries d "
                "WHERE d.event_id=o.id AND d.recipient=?) ORDER BY o.rowid DESC LIMIT ?",
                (owner, before or 2**63 - 1, reader, limit + 1),
            ).fetchall()
        return {
            "@context": AS_CONTEXT,
            "type": "OrderedCollectionPage",
            "orderedItems": [decode(r["body"]) for r in rows[:limit]],
            "next_cursor": rows[limit - 1]["seq"] if len(rows) > limit else None,
        }

"""Origin-addressed invitations. Tokens authorize a request, never a friendship."""

import base64
import hashlib
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit

from .config import origin_of
from .errors import FederationError
from .signatures import sign
from .store import decode, encode

CAPABILITY = "invitations/1"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def parse(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise FederationError("invalid_invitation")
    value = value.strip()
    link = None
    if value.startswith("https://"):
        link = urlsplit(value)
        origin_of(value.split("#", 1)[0])
        if link.query or not link.fragment.startswith("rfi1."):
            raise FederationError("invalid_invitation")
        value = link.fragment
    if not re.fullmatch(r"rfi1\.[A-Za-z0-9_-]+", value):
        raise FederationError("invalid_invitation")
    try:
        encoded = value[5:]
        payload = decode(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if set(payload) != {"v", "origin", "actor", "id", "token"} or payload["v"] != 1:
            raise ValueError
        origin = origin_of(payload["origin"])
        if origin != payload["origin"] or origin_of(payload["actor"]) != origin:
            raise ValueError
        if str(uuid.UUID(payload["id"])) != payload["id"] or not TOKEN.fullmatch(payload["token"]):
            raise ValueError
        if link and (
            origin_of(link.geturl().split("#", 1)[0]) != origin
            or link.path != "/invite/" + payload["id"]
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FederationError("invalid_invitation") from exc
    return payload


class Invitations:
    def __init__(self, service):
        self.s, self.store = service, service.store

    def create(self, owner, *, lifetime=604800, max_uses=10):
        self.s.relationships._human(owner)
        if type(lifetime) is not int or not 60 <= lifetime <= 2592000:
            raise FederationError("invalid_invitation_lifetime")
        if type(max_uses) is not int or not 1 <= max_uses <= 1000:
            raise FederationError("invalid_invitation_max_uses")
        self.store.rate_limit("invitation-create:" + owner, 10)
        token, identifier, now = secrets.token_urlsafe(32), str(uuid.uuid4()), time.time()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO invitation_tokens(id,owner,token_hash,created,expires,max_uses) VALUES (?,?,?,?,?,?)",
                (
                    identifier,
                    owner,
                    hashlib.sha256(token.encode()).hexdigest(),
                    now,
                    now + lifetime,
                    max_uses,
                ),
            )
        payload = {
            "v": 1,
            "origin": self.s.config.origin,
            "actor": owner,
            "id": identifier,
            "token": token,
        }
        code = "rfi1." + base64.urlsafe_b64encode(encode(payload).encode()).decode().rstrip("=")
        return {
            "id": identifier,
            "code": code,
            "url": self.s.config.origin + "/invite/" + identifier + "#" + code,
            "expires_at": now + lifetime,
            "max_uses": max_uses,
        }

    def list(self, owner):
        self.s.relationships._human(owner)
        return [
            dict(r)
            for r in self.store.all(
                "SELECT id,created,expires,revoked,max_uses,uses FROM invitation_tokens WHERE owner=? ORDER BY created DESC LIMIT 100",
                (owner,),
            )
        ]

    def revoke(self, owner, identifier):
        self.s.relationships._human(owner)
        with self.store.connect(write=True) as db:
            if not db.execute(
                "UPDATE invitation_tokens SET revoked=1 WHERE id=? AND owner=?", (identifier, owner)
            ).rowcount:
                raise FederationError("invitation_not_found", 404)

    def delete(self, owner, identifier):
        """Remove one invitation from the owner's list. A still-valid code stops working at
        once (like revoke). Friendships already made through it are not affected."""
        self.s.relationships._human(owner)
        with self.store.connect(write=True) as db:
            if not db.execute(
                "SELECT 1 FROM invitation_tokens WHERE id=? AND owner=?", (identifier, owner)
            ).fetchone():
                raise FederationError("invitation_not_found", 404)
            db.execute("DELETE FROM invitation_uses WHERE invitation_id=?", (identifier,))
            db.execute("DELETE FROM invitation_tokens WHERE id=?", (identifier,))

    def clear_inactive(self, owner):
        """Remove every revoked, expired or used-up invitation of this owner."""
        self.s.relationships._human(owner)
        with self.store.connect(write=True) as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM invitation_tokens WHERE owner=? AND "
                    "(revoked=1 OR expires<=? OR uses>=max_uses)",
                    (owner, time.time()),
                )
            ]
            for identifier in ids:
                db.execute("DELETE FROM invitation_uses WHERE invitation_id=?", (identifier,))
                db.execute("DELETE FROM invitation_tokens WHERE id=?", (identifier,))
        return len(ids)

    def public(self, identifier):
        row = self.store.one("SELECT * FROM invitation_tokens WHERE id=?", (identifier,))
        if (
            not row
            or row["revoked"]
            or row["expires"] <= time.time()
            or row["uses"] >= row["max_uses"]
        ):
            raise FederationError("invitation_unavailable", 404)
        actor = self.s.identity.local_document(row["owner"])
        return {
            "id": identifier,
            "actor_id": actor["id"],
            "username": actor["preferredUsername"],
            "origin": self.s.config.origin,
            "expires_at": row["expires"],
        }

    def _check(self, db, identifier, token, requester):
        if (
            not isinstance(identifier, str)
            or not isinstance(token, str)
            or not TOKEN.fullmatch(token)
        ):
            raise FederationError("invitation_unavailable", 404)
        row = db.execute("SELECT * FROM invitation_tokens WHERE id=?", (identifier,)).fetchone()
        if not row or not secrets.compare_digest(
            row["token_hash"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise FederationError("invitation_unavailable", 404)
        used = db.execute(
            "SELECT * FROM invitation_uses WHERE invitation_id=? AND requester=?",
            (identifier, requester),
        ).fetchone()
        if (
            row["revoked"]
            or row["expires"] <= time.time()
            or (row["uses"] >= row["max_uses"] and not used)
        ):
            raise FederationError("invitation_unavailable", 404)
        if self.s.relationships._blocked(db, row["owner"], requester):
            raise FederationError("contact_blocked", 403)
        return row, used

    def validate(self, requester, identifier, token):
        self.store.rate_limit("invitation-verify:" + requester, 20)
        with self.store.connect() as db:
            row, _ = self._check(db, identifier, token, requester)
        return {
            "valid": True,
            "actor_id": row["owner"],
            "id": identifier,
            "origin": self.s.config.origin,
            "expires_at": row["expires"],
            "requires_confirmation": True,
        }

    def consume(self, db, event):
        credential = event.get("rb:invite")
        if (
            not isinstance(credential, dict)
            or set(credential) != {"id", "token"}
            or event["rb:group"] != event["id"]
        ):
            raise FederationError("invalid_invitation_reference")
        row, used = self._check(db, credential["id"], credential["token"], event["actor"])
        if row["owner"] != event["object"]:
            raise FederationError("invitation_target_mismatch", 403)
        if used:
            if used["event_id"] != event["id"]:
                raise FederationError("invitation_already_used_by_requester", 409)
            return
        db.execute(
            "INSERT INTO invitation_uses VALUES (?,?,?,?)",
            (row["id"], event["actor"], event["id"], time.time()),
        )
        db.execute("UPDATE invitation_tokens SET uses=uses+1 WHERE id=?", (row["id"],))

    def resolve(self, local, value):
        self.s.relationships._human(local)
        payload = parse(value)
        self.store.rate_limit("invitation-resolve:" + local, 20)
        doc = self.s.identity.remote(payload["actor"])
        if doc["type"] != "Person":
            raise FederationError("invitation_requires_human")
        caps = self.s.http.get_json(doc["rb:capabilities"])
        if CAPABILITY not in caps.get("capabilities", []):
            raise FederationError("peer_invitation_not_supported", 422)
        body = encode({"id": payload["id"], "token": payload["token"]}).encode()
        msg = sign(
            self.s.identity,
            local,
            "POST",
            payload["origin"] + "/federation/invitations/verify",
            body,
        )
        response = self.s.http.send(msg.method, msg.url, msg.headers, msg.body)
        if response.status != 200:
            raise FederationError("invitation_unavailable", 400)
        result = response.json()
        if (
            result.get("valid") is not True
            or result.get("actor_id") != payload["actor"]
            or result.get("id") != payload["id"]
            or result.get("origin") != payload["origin"]
        ):
            raise FederationError("invitation_origin_mismatch", 502)
        return result

    def redeem(self, local, value, *, include_ai=True):
        payload = parse(value)
        # An established friendship does not depend on the current invitation lifetime.
        if self.s.relationships.is_friend(local, payload["actor"]):
            return {"status": "active", "actor_id": payload["actor"], "group_id": None}
        result = self.resolve(local, value)
        group = self.s.relationships.request(
            local,
            result["actor_id"],
            include_ai=include_ai,
            invitation={"id": payload["id"], "token": payload["token"]},
        )
        return {"status": "pending", "actor_id": result["actor_id"], "group_id": group}

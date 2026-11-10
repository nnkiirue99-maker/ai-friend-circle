"""Local MCP credentials: one secret per AI identity, never exchanged between servers.

A credential names exactly one local AI and a set of scopes. Only its SHA-256 is stored, so
the database cannot be used to recover a working token. Friendship with another server never
creates or shares one of these; each server issues its own, for its own AIs, on its own disk.
"""

import hashlib
import hmac
import json
import secrets
import time
import uuid

from .errors import FederationError

SCOPES = ("read", "post", "interact", "settings")
PREFIX = "rfm1"


class Credentials:
    def __init__(self, store):
        self.store = store

    def _ai(self, actor):
        row = self.store.one("SELECT kind FROM actors WHERE id=?", (actor,))
        if not row or row[0] != "ai":
            # Humans act through the host login. An AI credential for a person would let
            # automation speak as that person.
            raise FederationError("mcp_credential_requires_local_ai", 403)

    def issue(self, actor, scopes=SCOPES, label="", lifetime=None):
        self._ai(actor)
        scopes = sorted(set(scopes))
        if not scopes or set(scopes) - set(SCOPES):
            raise FederationError("invalid_mcp_scopes")
        if not isinstance(label, str) or len(label) > 64 or any(ord(c) < 32 for c in label):
            raise FederationError("invalid_mcp_label")
        if lifetime is not None and (
            type(lifetime) is not int or not 60 <= lifetime <= 366 * 86400
        ):
            raise FederationError("invalid_mcp_lifetime")
        identifier, secret, now = uuid.uuid4().hex, secrets.token_urlsafe(32), time.time()
        with self.store.connect(write=True) as db:
            if (
                db.execute(
                    "SELECT COUNT(*) FROM mcp_tokens WHERE actor=? AND revoked=0", (actor,)
                ).fetchone()[0]
                >= 8
            ):
                raise FederationError("too_many_active_mcp_credentials", 409)
            db.execute(
                "INSERT INTO mcp_tokens VALUES (?,?,?,?,?,?,?,NULL,0)",
                (
                    identifier,
                    actor,
                    hashlib.sha256(secret.encode()).hexdigest(),
                    json.dumps(scopes),
                    label,
                    now,
                    now + lifetime if lifetime else None,
                ),
            )
        return {
            "id": identifier,
            "actor_id": actor,
            "scopes": scopes,
            "token": f"{PREFIX}.{identifier}.{secret}",  # shown once; not recoverable later
        }

    def verify(self, token):
        """Return (credential id, actor, scopes) or raise. Identical error for every failure."""
        denied = FederationError("mcp_credential_invalid", 401)
        if not isinstance(token, str) or len(token) > 256:
            raise denied
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != PREFIX:
            raise denied
        row = self.store.one("SELECT * FROM mcp_tokens WHERE id=?", (parts[1],))
        digest = hashlib.sha256(parts[2].encode()).hexdigest()
        # Compare even when the id is unknown, so timing does not reveal which ids exist.
        if not hmac.compare_digest(digest, row["token_hash"] if row else "0" * 64) or not row:
            raise denied
        now = time.time()
        if row["revoked"] or (row["expires"] is not None and row["expires"] <= now):
            raise denied
        actor = self.store.one("SELECT kind FROM actors WHERE id=?", (row["actor"],))
        if not actor or actor[0] != "ai":
            raise denied
        if not row["last_used"] or now - row["last_used"] > 60:
            with self.store.connect(write=True) as db:
                db.execute("UPDATE mcp_tokens SET last_used=? WHERE id=?", (now, row["id"]))
        return row["id"], row["actor"], frozenset(json.loads(row["scopes"]))

    def revoke(self, identifier):
        with self.store.connect(write=True) as db:
            if not db.execute(
                "UPDATE mcp_tokens SET revoked=1 WHERE id=? AND revoked=0", (identifier,)
            ).rowcount:
                raise FederationError("mcp_credential_not_found", 404)

    def rotate(self, identifier):
        """Issue a replacement with the same actor, scopes and label, then revoke the old one."""
        row = self.store.one("SELECT * FROM mcp_tokens WHERE id=? AND revoked=0", (identifier,))
        if not row:
            raise FederationError("mcp_credential_not_found", 404)
        remaining = int(row["expires"] - time.time()) if row["expires"] else None
        if remaining is not None and remaining < 60:
            raise FederationError("mcp_credential_not_found", 404)
        self.revoke(identifier)  # first, so the active-credential cap cannot block rotation
        return self.issue(row["actor"], json.loads(row["scopes"]), row["label"], remaining)

    def list(self, actor=None):
        rows = self.store.all(
            "SELECT id,actor,scopes,label,created,expires,last_used,revoked FROM mcp_tokens "
            + ("WHERE actor=? " if actor else "")
            + "ORDER BY created DESC LIMIT 200",
            (actor,) if actor else (),
        )
        return [{**dict(r), "scopes": json.loads(r["scopes"])} for r in rows]

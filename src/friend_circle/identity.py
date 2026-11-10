import os
import re
import time
import uuid
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .config import AS_CONTEXT, RB_CONTEXT, PROFILE, origin_of
from .errors import FederationError
from .store import encode, decode

HANDLE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class Identity:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.origin = store.config.origin
        self.key_dir = store.config.state_dir / "keys"
        self.key_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.key_dir, 0o700)

    def create(self, username, kind="person", controller=None):
        if not HANDLE.fullmatch(username) or kind not in {"person", "ai", "server"}:
            raise FederationError("invalid_actor")
        if kind == "ai":
            owner = self.store.one("SELECT kind FROM actors WHERE id=?", (controller,))
            if not owner or owner[0] != "person":
                raise FederationError("ai_requires_local_human_controller")
        elif controller is not None:
            raise FederationError("unexpected_controller")
        key = Ed25519PrivateKey.generate()
        key_name = uuid.uuid4().hex + ".pem"
        path = self.key_dir / key_name
        data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        actor_id = self.origin + "/federation/actors/" + str(uuid.uuid4())
        pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        try:
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO actors VALUES (?,?,?,?,?,?,?)",
                    (actor_id, username, kind, controller, pem, key_name, time.time()),
                )
        except BaseException:
            path.unlink()
            raise
        return self.local_document(actor_id)

    def local_document(self, actor_id):
        row = self.store.one("SELECT * FROM actors WHERE id=?", (actor_id,))
        if row is None:
            raise FederationError("actor_not_found", 404)
        return {
            "@context": [AS_CONTEXT, RB_CONTEXT],
            "id": row["id"],
            "type": {"person": "Person", "ai": "Service", "server": "Application"}[row["kind"]],
            "preferredUsername": row["username"],
            "rb:kind": row["kind"],
            "inbox": actor_id + "/inbox",
            "outbox": actor_id + "/outbox",
            "endpoints": {"sharedInbox": self.origin + "/federation/inbox"},
            "publicKey": {
                "id": actor_id + "#main-key",
                "owner": actor_id,
                "publicKeyPem": row["public_pem"],
            },
            "rb:capabilities": self.origin + "/federation/capabilities",
        }

    def private_key(self, actor_id):
        row = self.store.one("SELECT key_name FROM actors WHERE id=?", (actor_id,))
        if not row:
            raise FederationError("not_a_local_actor", 403)
        path = self.key_dir / row[0]
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise FederationError("unsafe_key_permissions", 500)
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise FederationError("invalid_private_key", 500)
        return key

    def discover(self, handle):
        if not isinstance(handle, str):
            raise FederationError("full_federation_id_required")
        handle = handle.removeprefix("@").removeprefix("acct:")
        if handle.count("@") != 1:
            raise FederationError("full_federation_id_required")
        username, domain = handle.split("@")
        if not HANDLE.fullmatch(username):
            raise FederationError("invalid_handle")
        origin = origin_of("https://" + domain)
        if self.store.blocked("", origin):
            raise FederationError("blocked", 403)
        subject = "acct:" + handle
        doc = self.http.get_json(
            origin + "/.well-known/webfinger?resource=" + quote(subject, safe="")
        )
        if doc.get("subject") != subject or not isinstance(doc.get("links"), list):
            raise FederationError("webfinger_subject_mismatch")
        links = [
            x
            for x in doc["links"]
            if isinstance(x, dict)
            and x.get("rel") == "self"
            and x.get("type") == "application/activity+json"
        ]
        if len(links) != 1 or origin_of(links[0].get("href", "")) != origin:
            raise FederationError("webfinger_actor_mismatch")
        actor = self.remote(links[0]["href"], refresh=True)
        if actor["preferredUsername"] != username:
            raise FederationError("webfinger_username_mismatch")
        return actor

    def remote(self, actor_id, *, refresh=False):
        origin = origin_of(actor_id)
        if (
            origin == self.origin
            or urlsplit(actor_id).query
            or self.store.blocked(actor_id, origin)
        ):
            raise FederationError("invalid_or_blocked_remote_actor", 403)
        cached = self.store.one("SELECT * FROM remote_actors WHERE id=?", (actor_id,))
        if cached and not refresh and cached["fetched"] > time.time() - 3600:
            return decode(cached["document"])
        self.store.rate_limit("discovery:" + origin, 30)
        doc = self.http.get_json(actor_id)
        public = doc.get("publicKey", {})
        if (
            doc.get("id") != actor_id
            or doc.get("type") not in {"Person", "Service", "Application"}
            or not isinstance(public, dict)
            or public.get("owner") != actor_id
            or public.get("id") != actor_id + "#main-key"
            or not isinstance(doc.get("preferredUsername"), str)
            or not HANDLE.fullmatch(doc["preferredUsername"])
        ):
            raise FederationError("invalid_actor_document", 502)
        kind = {"Person": "person", "Service": "ai", "Application": "server"}[doc["type"]]
        if doc.get("rb:kind") != kind:
            raise FederationError("actor_kind_mismatch", 502)
        for field in ["inbox", "outbox", "rb:capabilities"]:
            if origin_of(doc.get(field, "")) != origin:
                raise FederationError("cross_origin_actor_endpoint", 502)
        endpoints = doc.get("endpoints", {})
        if not isinstance(endpoints, dict):
            raise FederationError("invalid_actor_endpoints", 502)
        if endpoints.get("sharedInbox") and origin_of(endpoints["sharedInbox"]) != origin:
            raise FederationError("cross_origin_shared_inbox", 502)
        try:
            key = serialization.load_pem_public_key(public["publicKeyPem"].encode())
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError
        except (KeyError, AttributeError, ValueError, TypeError) as exc:
            raise FederationError("invalid_actor_key", 502) from exc
        caps = self.http.get_json(doc["rb:capabilities"])
        if (
            caps.get("profile") != PROFILE
            or not isinstance(caps.get("capabilities"), list)
            or "rfc9421-ed25519" not in caps["capabilities"]
        ):
            raise FederationError("unsupported_peer_profile", 422)
        # Store a whitelist, never arbitrary remote extensions/HTML/profile data.
        clean = {
            k: doc[k]
            for k in [
                "id",
                "type",
                "preferredUsername",
                "rb:kind",
                "inbox",
                "outbox",
                "publicKey",
                "rb:capabilities",
            ]
        }
        clean["rb:features"] = caps["capabilities"]
        clean["endpoints"] = {"sharedInbox": endpoints.get("sharedInbox", doc["inbox"])}
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO remote_actors VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "document=excluded.document,fetched=excluded.fetched",
                (actor_id, encode(clean), time.time()),
            )
        return clean

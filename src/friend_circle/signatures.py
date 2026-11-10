from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets

from cryptography.hazmat.primitives import serialization
from http_message_signatures import (
    HTTPMessageSigner,
    HTTPMessageVerifier,
    MappingKeyResolver,
    HTTPMessageSignaturesException,
    algorithms,
)
from requests.structures import CaseInsensitiveDict

from .config import CONTENT_TYPE, MAX_AGE, PROFILE
from .errors import FederationError

COMPONENTS = (
    "@method",
    "@target-uri",
    "content-type",
    "content-digest",
    "x-rue-actor",
    "x-rue-profile",
)


@dataclass
class Message:
    method: str
    url: str
    headers: CaseInsensitiveDict
    body: bytes = b""


def sign(identity, actor_id, method, url, body=b"", *, created=None, nonce=None):
    created = created or datetime.now(timezone.utc)
    msg = Message(
        method,
        url,
        CaseInsensitiveDict(
            {
                "Content-Type": CONTENT_TYPE,
                "X-Rue-Actor": actor_id,
                "X-Rue-Profile": PROFILE,
            }
        ),
        body,
    )
    key_id = actor_id + "#main-key"
    signer = HTTPMessageSigner(
        signature_algorithm=algorithms.ED25519,
        key_resolver=MappingKeyResolver(
            public_keys={}, private_keys={key_id: identity.private_key(actor_id)}
        ),
    )
    signer.sign(
        msg,
        key_id=key_id,
        body=body,
        created=created,
        expires=created + timedelta(seconds=MAX_AGE),
        nonce=nonce or secrets.token_urlsafe(24),
        tag=PROFILE,
        covered_component_ids=COMPONENTS,
    )
    return msg


def verify(identity, msg):
    if (
        not msg.headers.get("Signature")
        or not msg.headers.get("Signature-Input")
        or msg.headers.get("X-Rue-Profile") != PROFILE
        or msg.headers.get("Content-Type") != CONTENT_TYPE
    ):
        raise FederationError("invalid_signature", 401)
    actor_id = msg.headers.get("X-Rue-Actor", "")
    doc = identity.remote(actor_id)
    key_id = doc["publicKey"]["id"]
    pem = doc["publicKey"]["publicKeyPem"].encode()
    key = serialization.load_pem_public_key(pem)
    verifier = HTTPMessageVerifier(
        signature_algorithm=algorithms.ED25519,
        key_resolver=MappingKeyResolver(public_keys={key_id: key}, private_keys={}),
    )
    verifier.max_clock_skew = timedelta(seconds=30)
    try:
        results = verifier.verify(
            msg, body=msg.body, max_age=timedelta(seconds=MAX_AGE), require_components=COMPONENTS
        )
        if len(results) != 1:
            raise ValueError
        params = results[0].parameters
        if (
            params.get("keyid") != key_id
            or params.get("tag") != PROFILE
            or not isinstance(params.get("nonce"), str)
            or not 16 <= len(params["nonce"]) <= 128
            or type(params.get("expires")) is not int
            or not 0 < params["expires"] - params["created"] <= MAX_AGE
            or msg.headers.get("X-Rue-Profile") != PROFILE
            or msg.headers.get("Content-Type") != CONTENT_TYPE
        ):
            raise ValueError
    except (HTTPMessageSignaturesException, ValueError, KeyError, TypeError) as exc:
        raise FederationError("invalid_signature", 401) from exc
    identity.store.consume_nonce(key_id, params["nonce"], params["expires"])
    return doc

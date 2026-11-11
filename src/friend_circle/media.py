"""Bounded raster-only inline media; never SVG, EXIF or HTML.

Pictures are stored inline. The only way they leave as a URL is a short-lived signed link
(``media_link``) handed to the reader's own AI, re-checked against access on every open.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from .errors import FederationError


def raster(value):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 100000
        or not value.startswith("data:image/")
        or ";base64," not in value
    ):
        raise FederationError("invalid_media")
    prefix, encoded = value.split(",", 1)
    if prefix not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
        raise FederationError("invalid_media")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(raw), formats=["PNG", "JPEG", "WEBP"]) as image:
            if image.width * image.height > 4_000_000:
                raise ValueError
            if getattr(image, "n_frames", 1) != 1:
                # iPhone photos are often MPO: a JPEG primary image plus an HDR gain map or
                # depth frame. Keep the primary still; animations (PNG/WebP) stay refused.
                if image.format != "MPO":
                    raise ValueError
                image.seek(0)
            image.load()
            image.thumbnail((1024, 1024))
            clean = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                clean.paste(image, mask=image.getchannel("A"))
            else:
                clean.paste(image.convert("RGB"))
            # Re-encoding must not reject a picture the client already shrank to fit: step
            # quality, then size, down until it fits the stored-image budget.
            result = None
            for edge, quality in (
                (1024, 82),
                (1024, 72),
                (1024, 62),
                (860, 58),
                (720, 54),
                (600, 50),
                (480, 46),
            ):
                candidate = clean
                if max(clean.size) > edge:
                    candidate = clean.copy()
                    candidate.thumbnail((edge, edge))
                out = BytesIO()
                candidate.save(out, format="JPEG", quality=quality)
                if out.tell() <= 64000:
                    result = out.getvalue()
                    break
            if result is None:
                raise ValueError
        return "data:image/jpeg;base64," + base64.b64encode(result).decode()
    except (
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        binascii.Error,
    ) as exc:
        raise FederationError("invalid_media") from exc


def plain(value, maximum=4000):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\t" for c in value)
    ):
        raise FederationError("invalid_plain_text")
    # Stored and transported as text/plain. Consumers MUST use textContent, never innerHTML.
    return value


LINK_TTL = 600  # seconds a picture link handed to an AI is promised to stay usable
LINK_BUCKET = 3600  # links are stable within an hour, so browsers and apps can cache them
MAX_PICTURES = 4
PREFIX = "/federation/media/"


def _split(item):
    head, _, data = item.partition(";base64,")
    if head in {"data:image/jpeg", "data:image/png", "data:image/webp"} and data:
        return head[5:], data
    return None


def _digest(data):
    return hashlib.sha256(data.encode()).hexdigest()[:24]


def _visible_object(service, reader, identifier, network):
    """The post as `reader` may see it now: original for local, lease-bound cache for remote."""
    social = service.social
    if social.local(identifier.split("/objects/")[0]):
        return social.object(reader, identifier)
    row = service.store.one(
        "SELECT actor,body FROM object_cache WHERE reader=? AND id=? AND expires>?",
        (reader, identifier, time.time()),
    )
    if row and service.relationships.is_friend(reader, row["actor"]):
        from .store import decode

        return decode(row["body"])
    if not network:
        raise FederationError("media_not_found", 404)
    return social.fetch(reader, identifier)


def readable_pictures(service, reader, identifier, network=True):
    """(post, [(mime, base64)]) for a post the reader may see; access re-checked each call."""
    obj = _visible_object(service, reader, identifier, network)
    pictures = [p for p in map(_split, obj["content"].get("media", [])[:MAX_PICTURES]) if p]
    return obj, pictures


def _link_key(service):
    path = service.config.state_dir / "keys" / "media-link.key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process created it first
            return path.read_bytes()
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
        return path.read_bytes()


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign(service, claims):
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_link_key(service), payload.encode(), hashlib.sha256).digest())
    return f"{service.config.origin}{PREFIX}{payload}.{sig}.jpg"


def _expiry(now=None):
    now = time.time() if now is None else now
    return int((now + LINK_TTL) // LINK_BUCKET + 1) * LINK_BUCKET


def media_link(service, reader, identifier, data, now=None):
    """Signed link to one picture (its base64 `data`) of a post, as seen by `reader`.

    Bound to the picture's content hash, so an edited post never serves a different image
    under an old URL, and the same picture keeps the same URL for the current hour.
    """
    expires = _expiry(now)
    claims = {"r": reader, "o": identifier, "h": _digest(data), "e": expires}
    return _sign(service, claims), expires


def profile_link(service, reader, actor, data, now=None):
    """Signed link to an avatar or cover of `actor`, as `reader` currently sees that profile."""
    expires = _expiry(now)
    claims = {"r": reader, "o": actor, "h": _digest(data), "e": expires, "k": "p"}
    return _sign(service, claims), expires


def _visible_profile(service, reader, actor):
    if service.social.local(actor):
        return service.social._profile(actor)
    row = service.store.one(
        "SELECT body FROM profile_cache WHERE reader=? AND actor=? AND expires>?",
        (reader, actor, time.time()),
    )
    if not row:
        raise FederationError("media_not_found", 404)
    from .store import decode

    return decode(row[0])


def _claims(service, token, now=None):
    payload, sig = token.removesuffix(".jpg").split(".")
    expected = _b64(hmac.new(_link_key(service), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims["e"] < (time.time() if now is None else now):
        raise ValueError
    return claims


def open_media_link(service, token, now=None):
    """(bytes, mime, expires) behind a signed link, or FederationError(404) for anything wrong."""
    try:
        claims = _claims(service, token, now)
        if claims.get("k") == "p":
            profile = _visible_profile(service, claims["r"], claims["o"])
            pictures = [p for p in (_split(profile.get(f) or "") for f in ("avatar", "cover")) if p]
        else:
            _, pictures = readable_pictures(service, claims["r"], claims["o"], network=False)
        for mime, data in pictures:
            if _digest(data) == claims["h"]:
                return base64.b64decode(data, validate=True), mime, claims["e"]
        raise ValueError
    except (ValueError, KeyError, TypeError, binascii.Error, FederationError) as exc:
        raise FederationError("media_not_found", 404) from exc


def _links(service, reader, identifier, media):
    out = []
    for item in media:
        part = _split(item) if isinstance(item, str) else None
        out.append(media_link(service, reader, identifier, part[1])[0] if part else item)
    return out


def link_post_media(service, reader, obj):
    """Copy of a post-shaped dict with inline pictures replaced by signed links.

    Handles both shapes the local API returns: objects (content.media) and the web UI
    snapshot's flattened posts (media).
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
        return obj
    content = obj.get("content")
    if isinstance(content, dict) and isinstance(content.get("media"), list):
        media = _links(service, reader, obj["id"], content["media"])
        return {**obj, "content": {**content, "media": media}}
    if isinstance(obj.get("media"), list):
        return {**obj, "media": _links(service, reader, obj["id"], obj["media"])}
    return obj


def link_profile_media(service, reader, obj):
    """Copy of a profile-shaped dict with inline avatar/cover replaced by signed links."""
    if not isinstance(obj, dict) or not ({"avatar", "cover"} & set(obj)):
        return obj
    actor = obj.get("actor_id") or obj.get("id")
    if not isinstance(actor, str) or "/federation/actors/" not in actor:
        return obj
    out = dict(obj)
    for field in ("avatar", "cover"):
        part = _split(obj[field]) if isinstance(obj.get(field), str) else None
        if part:
            out[field] = profile_link(service, reader, actor, part[1])[0]
    return out


def linkify(service, reader, value):
    """Walk an API response and swap post and profile pictures for links."""
    if isinstance(value, list):
        return [linkify(service, reader, x) for x in value]
    if isinstance(value, dict):
        value = link_profile_media(service, reader, link_post_media(service, reader, value))
        return {k: linkify(service, reader, v) for k, v in value.items()}
    return value


def resolve_link(service, readers, identifier, item, kind=None):
    """For edits: one link to a picture of this same post/profile, minted for `readers`,
    back to an inline picture. Anything that is not one of our links is returned as is."""
    base = service.config.origin + PREFIX
    if not (isinstance(item, str) and item.startswith(base)):
        return item
    token = item[len(base) :]
    try:
        claims = _claims(service, token)
        if claims["o"] != identifier or claims["r"] not in readers or claims.get("k") != kind:
            raise ValueError
        raw, mime, _ = open_media_link(service, token)
    except (ValueError, KeyError, TypeError, FederationError) as exc:
        raise FederationError("invalid_media") from exc
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def resolve_links(service, readers, identifier, media):
    """For edits: turn picture links of this same post, minted for `readers`, back inline."""
    if not isinstance(media, list):
        return media
    return [resolve_link(service, readers, identifier, item) for item in media]

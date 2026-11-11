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


LINK_TTL = 600  # seconds a picture link handed to an AI stays usable
MAX_PICTURES = 4


def readable_pictures(service, reader, identifier):
    """(post, [(mime, base64)]) for a post the reader may see; access re-checked each call."""
    obj = (
        service.social.object(reader, identifier)
        if service.social.local(identifier.split("/objects/")[0])
        else service.social.fetch(reader, identifier)
    )
    pictures = []
    for item in obj["content"].get("media", [])[:MAX_PICTURES]:
        head, _, data = item.partition(";base64,")
        if head in {"data:image/jpeg", "data:image/png", "data:image/webp"} and data:
            pictures.append((head[5:], data))
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


def media_link(service, reader, identifier, index, now=None):
    expires = int((time.time() if now is None else now) + LINK_TTL)
    payload = _b64(
        json.dumps(
            {"r": reader, "o": identifier, "i": index, "e": expires}, separators=(",", ":")
        ).encode()
    )
    sig = _b64(hmac.new(_link_key(service), payload.encode(), hashlib.sha256).digest())
    return f"{service.config.origin}/federation/media/{payload}.{sig}.jpg", expires


def open_media_link(service, token, now=None):
    """Bytes and mime type behind a signed link, or FederationError(404) for anything wrong."""
    try:
        payload, sig = token.removesuffix(".jpg").split(".")
        expected = _b64(hmac.new(_link_key(service), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims["e"] < (time.time() if now is None else now):
            raise ValueError
        _, pictures = readable_pictures(service, claims["r"], claims["o"])
        mime, data = pictures[claims["i"]]
        return base64.b64decode(data, validate=True), mime
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error, FederationError) as exc:
        raise FederationError("media_not_found", 404) from exc

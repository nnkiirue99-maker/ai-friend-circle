"""Bounded raster-only inline media; never public URLs, SVG, EXIF or HTML."""

import base64
import binascii
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

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import re

from .errors import FederationError

PROFILE = "rue-federation-dev/0.3"
AS_CONTEXT = "https://www.w3.org/ns/activitystreams"
RB_CONTEXT = {"rb": "urn:ruebase:federation:0.3#"}
CONTENT_TYPE = "application/activity+json"
MAX_BODY = 256 * 1024
MAX_AGE = 300
ACTIVITY_MAX_AGE = 7 * 86400


def origin_of(url: str) -> str:
    try:
        if not isinstance(url, str):
            raise ValueError
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username
            or p.password
            or p.fragment
            or p.port not in (None, 443)
            or len(url) > 2048
            or re.search(r"[\s\\\x00-\x1f\x7f]", url)
        ):
            raise ValueError
        host = p.hostname.encode("idna").decode("ascii")
        # Canonical ASCII hostnames only: no IP literals, trailing dots or alternate encodings.
        if (
            host != p.hostname
            or host.endswith(".")
            or ":" in host
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
        ):
            raise ValueError
        if any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in host.split(".")
        ):
            raise ValueError
        if p.netloc != host:  # canonical URI; explicit :443 is not accepted in this profile
            raise ValueError
        return "https://" + host
    except (ValueError, UnicodeError, AttributeError, TypeError) as exc:
        raise FederationError("invalid_https_url") from exc


@dataclass(frozen=True)
class Config:
    origin: str
    state_dir: Path
    max_attempts: int = 8
    lease_seconds: int = 120
    inbox_per_minute: int = 120

    def __post_init__(self):
        if origin_of(self.origin) != self.origin:
            raise FederationError("origin_must_be_canonical")
        object.__setattr__(self, "state_dir", Path(self.state_dir).resolve())
        if self.max_attempts < 1 or self.lease_seconds < 20:
            raise ValueError("invalid worker settings")

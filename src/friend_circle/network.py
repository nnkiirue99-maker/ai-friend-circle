"""HTTPS with a validated DNS snapshot, pinned TCP destination and original TLS identity.

No requests.Session proxies, redirects, cookies, compression or ambient credentials.
The resolver/connector injection points exist for tests, never environment flags.
"""

from dataclasses import dataclass
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit

import urllib3

from .config import MAX_BODY, origin_of
from .errors import FederationError
from .store import decode


@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict

    def json(self):
        return decode(self.body)


class SafeHTTP:
    def resolve(self, host):
        return list({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})

    def addresses(self, url):
        origin_of(url)
        host = urlsplit(url).hostname
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise FederationError("unsafe_destination")
        try:
            addresses = self.resolve(host)
            if not addresses:
                raise ValueError
            for value in addresses:
                ip = ipaddress.ip_address(value)
                if (
                    not ip.is_global
                    or ip.is_multicast
                    or ip.is_reserved
                    or getattr(ip, "ipv4_mapped", None) is not None
                    or (ip.version == 6 and (ip.sixtofour or ip.teredo))
                ):
                    raise ValueError
            return sorted(addresses)
        except (ValueError, OSError) as exc:
            raise FederationError("unsafe_or_unresolved_destination", 502) from exc

    def pool(self, ip, host):
        return urllib3.HTTPSConnectionPool(
            ip,
            port=443,
            server_hostname=host,
            assert_hostname=host,
            ssl_context=ssl.create_default_context(),
            maxsize=1,
            block=True,
            retries=False,
        )

    def send(self, method, url, headers=None, body=None, max_bytes=MAX_BODY):
        ips = self.addresses(url)
        p = urlsplit(url)
        safe_headers = dict(headers or {})
        safe_headers["Host"] = p.netloc
        safe_headers["Accept-Encoding"] = "identity"
        # A connection attempt uses only this validated address. Retry re-resolves.
        pool = self.pool(ips[0], p.hostname)
        response = None
        try:
            start = time.monotonic()
            response = pool.urlopen(
                method,
                p.path + ("?" + p.query if p.query else ""),
                body=body,
                headers=safe_headers,
                redirect=False,
                retries=False,
                preload_content=False,
                decode_content=False,
                timeout=urllib3.Timeout(connect=3, read=5, total=10),
                pool_timeout=2,
            )
            if 300 <= response.status < 400:
                raise FederationError("redirect_forbidden", 502)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise FederationError("compressed_response_forbidden", 502)
            chunks, size = [], 0
            while True:
                chunk = response.read(8192, decode_content=False)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes or time.monotonic() - start > 10:
                    raise FederationError("response_limit", 502)
                chunks.append(chunk)
            return Reply(response.status, b"".join(chunks), dict(response.headers))
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise FederationError("remote_unavailable", 502) from exc
        finally:
            if response:
                response.close()
            pool.close()

    def get_json(self, url):
        response = self.send("GET", url, {"Accept": "application/activity+json, application/json"})
        if response.status != 200:
            raise FederationError("discovery_failed", 502)
        media = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if media not in {
            "application/activity+json",
            "application/ld+json",
            "application/jrd+json",
            "application/json",
        }:
            raise FederationError("discovery_content_type", 502)
        return response.json()

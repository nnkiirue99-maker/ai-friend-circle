import pytest

from friend_circle.config import origin_of
from friend_circle.errors import FederationError
from friend_circle.network import SafeHTTP


@pytest.mark.parametrize(
    "url",
    [
        "http://a.test",
        "https://u:p@a.test/",
        "https://a.test:8080/",
        "https://a.test/#x",
        "https://[::1]/",
        "https://a.test./",
        "https://a.test\\@evil.test/",
        "https://a.test/\n",
        "https://a.test:443/",
        "file:///etc/passwd",
        None,
        {},
        "https://a..test/",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(FederationError):
        origin_of(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:8.8.8.8",
        "2002:0808:0808::1",
        "192.0.2.1",
        "255.255.255.255",
    ],
)
def test_private_reserved_and_tunnel_addresses_rejected(address, monkeypatch):
    client = SafeHTTP()
    monkeypatch.setattr(client, "resolve", lambda host: [address])
    with pytest.raises(FederationError):
        client.addresses("https://remote.test/actor")


def test_every_dns_record_checked(monkeypatch):
    client = SafeHTTP()
    monkeypatch.setattr(client, "resolve", lambda host: ["8.8.8.8", "127.0.0.1"])
    with pytest.raises(FederationError):
        client.addresses("https://remote.test/actor")


def test_validated_address_is_pinned_not_resolved_again(monkeypatch):
    client = SafeHTTP()
    calls = []
    monkeypatch.setattr(client, "resolve", lambda host: ["8.8.8.8"])

    class Response:
        status = 200
        headers = {}

        def read(self, *args, **kwargs):
            return b""

        def close(self):
            pass

    class Pool:
        def urlopen(self, method, path, **kwargs):
            assert kwargs["redirect"] is False and kwargs["retries"] is False
            assert kwargs["headers"]["Host"] == "remote.test"
            return Response()

        def close(self):
            pass

    def connect(ip, host):
        calls.append((ip, host))
        # Simulate DNS changing after validation. Connection must not use the new DNS value.
        monkeypatch.setattr(client, "resolve", lambda host: ["127.0.0.1"])
        return Pool()

    monkeypatch.setattr(client, "pool", connect)
    assert client.send("GET", "https://remote.test/actor").status == 200
    assert calls == [("8.8.8.8", "remote.test")]


@pytest.mark.parametrize(
    "status,headers,body,expected",
    [
        (302, {"Location": "https://127.0.0.1/"}, b"", "redirect_forbidden"),
        (200, {"Content-Encoding": "gzip"}, b"", "compressed_response_forbidden"),
        (200, {}, b"x" * 100, "response_limit"),
    ],
)
def test_response_hazards(monkeypatch, status, headers, body, expected):
    client = SafeHTTP()
    monkeypatch.setattr(client, "resolve", lambda host: ["8.8.8.8"])

    class Response:
        def __init__(self):
            self.status, self.headers = status, headers

        def read(self, *args, **kwargs):
            return body

        def close(self):
            pass

    class Pool:
        def urlopen(self, *args, **kwargs):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(client, "pool", lambda *args: Pool())
    with pytest.raises(FederationError, match=expected):
        client.send("GET", "https://remote.test/", max_bytes=10)

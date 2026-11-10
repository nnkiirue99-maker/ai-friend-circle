"""Independent normative-source vector, not a sign-then-verify round trip.

RFC 9421 Appendix B.1.4 / B.2.6:
https://www.rfc-editor.org/rfc/rfc9421.html#appendix-B.2.6
The RFC sample has no digest/nonce/expires, so this tests the crypto dependency,
not acceptance by Alice's stricter wire profile. No test private key is needed.
"""

from http_message_signatures import HTTPMessageVerifier, MappingKeyResolver, algorithms
from requests.structures import CaseInsensitiveDict

from friend_circle.signatures import Message


def test_rfc9421_appendix_b26_ed25519():
    public = (
        b"-----BEGIN PUBLIC KEY-----\n"
        b"MCowBQYDK2VwAyEAJrQLj5P/89iXES9+vFgrIy29clF9CC/oPPsw3c5D0bs=\n"
        b"-----END PUBLIC KEY-----\n"
    )
    message = Message(
        "POST",
        "https://example.com/foo?param=Value&Pet=dog",
        CaseInsensitiveDict(
            {
                "Date": "Tue, 20 Apr 2021 02:07:55 GMT",
                "Content-Type": "application/json",
                "Content-Length": "18",
                "Signature-Input": 'sig-b26=("date" "@method" "@path" "@authority" '
                '"content-type" "content-length");created=1618884473;keyid="test-key-ed25519"',
                "Signature": "sig-b26=:wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1"
                "u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw==:",
            }
        ),
    )
    verifier = HTTPMessageVerifier(
        signature_algorithm=algorithms.ED25519,
        key_resolver=MappingKeyResolver(public_keys={"test-key-ed25519": public}, private_keys={}),
    )
    assert len(verifier.verify(message, max_age=None)) == 1
